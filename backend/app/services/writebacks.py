"""
Automatic writeback discard.

A machine's writeback is thrown away by the SERVER whenever the client goes
away, so every boot starts from a clean copy of the game disk's active
snapshot. Nobody clicks "Reset". Three triggers:

  watcher   LIO shows no iSCSI session for the machine for `grace` seconds
            (shutdown, restart, cable pulled). The grace period keeps a short
            network blip from wiping a disk Windows still has mounted.
  boot      ggnet-agent reports a newer Windows boot time while the machine
            has no session yet. This covers a restart that reconnects
            faster than the grace period.
  iPXE      a Boot Mode PC asks for its boot script (app.api.v1.boot).

A Boot Mode PC is booting for `boot_window` after its iPXE request: iPXE's
iSCSI session ends when Windows takes over, and Windows logs in by itself
only later (tens of seconds on a busy server). The watcher leaves it alone
in that window, or it would remove the PC's ACL just as Windows logs in
(INACCESSIBLE_BOOT_DEVICE).

Machines with `keep_writeback` are never discarded. A clean writeback (not
written since it was cloned) on the active snapshot is left alone, so an idle
PC does not get re-cloned over and over.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Machine, MachineMode, MachineStatus
from app.services.os_setup import os_setup_for
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.writebacks")

# Windows boot times from two heartbeats of the same boot differ by
# rounding; only a jump larger than this is a new boot.
BOOT_TOLERANCE = timedelta(seconds=120)
# Sizes in binary units, as ZFS and the UI show them.
GB = 1 << 30


@dataclass(frozen=True)
class WritebackSettings:
    auto_discard: bool = True
    grace: timedelta = timedelta(seconds=30)
    poll: timedelta = timedelta(seconds=5)
    boot_window: timedelta = timedelta(seconds=300)
    # Boot Mode: keep a spare of each writeback ready, so a restart only swaps it in.
    spares: bool = True

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "WritebackSettings":
        wb = cfg.get("writebacks") or {}
        return cls(
            auto_discard=bool(wb.get("auto_discard", True)),
            grace=timedelta(seconds=int(wb.get("grace_seconds", 30))),
            poll=timedelta(seconds=int(wb.get("poll_seconds", 5))),
            boot_window=timedelta(seconds=int(wb.get("boot_window_seconds", 300))),
            spares=bool(wb.get("spares", True)),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _managed(m: Machine) -> bool:
    """Machines whose writebacks this module may discard."""
    if m.status is not MachineStatus.PROVISIONED or m.game_disk is None or not m.game_disk.published:
        return False
    if m.mode is MachineMode.BOOT:
        return m.os_disk is not None and m.os_disk.published and m.os_clone_zvol is not None
    return True


def _needs_discard(m: Machine) -> bool:
    return not m.keep_writeback and (m.writeback_dirty or m.outdated)


def discard(db: Session, prov: Provisioner, m: Machine, reason: str) -> bool:
    """
    Re-clone the machine's writeback from the version it should run (its
    pin, else the disk's active snapshot); in Boot Mode its OS writeback too,
    from the OS image's active snapshot.
    Commits. On a host error the machine goes to `error` with the reason.
    """
    clone = m.clone_zvol or prov.client_path(m.name)
    os_clone = m.os_clone_zvol if m.mode is MachineMode.BOOT else None
    try:
        cd = prov.reset(m.name, m.initiator_iqn, clone, m.target_snapshot_path,
                        os_clone, m.os_snapshot_path if os_clone else None,
                        os_setup_for(m) if os_clone else None, target_iqn=m.iscsi_target_iqn)
    except ProvisioningError as e:
        m.status = MachineStatus.ERROR
        m.last_error = f"Automatic writeback discard failed: {e}"
        db.commit()
        return False
    m.clone_zvol, m.clone_snapshot = cd.clone_zvol, cd.clone_snapshot
    m.os_clone_zvol, m.os_clone_snapshot = cd.os_clone_zvol, cd.os_clone_snapshot
    m.iscsi_target_iqn = cd.iscsi_target_iqn
    m.writeback_dirty = False
    m.writeback_bytes, m.writeback_large = 0, False
    m.last_error = None
    db.commit()
    logger.info("Discarded writeback of %s (%s)", m.name, reason)
    return True


def observe(db: Session, prov: Provisioner, m: Machine, settings: WritebackSettings,
            now: datetime | None = None) -> None:
    """One watcher step for one machine: record its session state, discard if due."""
    editing = m.status is MachineStatus.EDITING
    if not (_managed(m) or editing):
        return
    now = now or _now()
    active = prov.session_active(m.initiator_iqn, m.iscsi_target_iqn)
    if active is None:
        return          # unknown is never treated as disconnected
    if active != m.session_active:
        m.session_active = active
        m.session_changed_at = now
    if editing:
        # The PC is filling a draft master: only show whether it is connected.
        db.commit()
        return
    if active:
        m.writeback_dirty = True
        db.commit()
        return
    db.commit()
    if _booting(m, settings, now):
        return
    if (settings.auto_discard and _needs_discard(m)
            and m.session_changed_at is not None
            and now - m.session_changed_at >= settings.grace):
        discard(db, prov, m, "client disconnected")


def _booting(m: Machine, settings: WritebackSettings, now: datetime) -> bool:
    """A Boot Mode PC between its iPXE request and Windows' own iSCSI login."""
    return (m.mode is MachineMode.BOOT and m.ipxe_at is not None
            and now - m.ipxe_at < settings.boot_window)


def on_heartbeat(db: Session, prov: Provisioner, m: Machine, booted_at: datetime | None,
                 iscsi_connected: bool, settings: WritebackSettings) -> None:
    """
    Agent heartbeat: a newer Windows boot time means the PC restarted; its
    writeback is discarded before the agent logs in again.
    """
    new_boot = booted_at is not None and (
        m.booted_at is None or booted_at - m.booted_at > BOOT_TOLERANCE
    )
    values: dict = {}
    if iscsi_connected and _managed(m) and not m.writeback_dirty:
        values["writeback_dirty"] = True
    if new_boot:
        values["booted_at"] = booted_at
    if values:
        # Agent-reported state, not an edit of the machine: keep updated_at.
        db.execute(update(Machine).where(Machine.id == m.id)
                   .values(**values, updated_at=Machine.updated_at))
        db.commit()
        db.refresh(m)
    if not (new_boot and settings.auto_discard and _managed(m) and _needs_discard(m)):
        return
    # The agent connects only after this answer; a live session means the
    # disk is still in use (e.g. an agent older than 0.1.5, whose boot time
    # was its service start, restarted without Windows).
    if prov.session_active(m.initiator_iqn, m.iscsi_target_iqn) is not False:
        return
    discard(db, prov, m, "client rebooted")


def on_ipxe(db: Session, prov: Provisioner, m: Machine, settings: WritebackSettings,
            now: datetime | None = None, boot_nic: str | None = None) -> bool:
    """
    Boot Mode: iPXE asks for its script, so the PC is booting. Its writebacks
    are discarded before it logs in, unless it keeps them or LIO still shows
    a session (the old one has not timed out yet; booting on the current
    writeback is the safe choice then). The request time starts the boot
    window in which the watcher leaves the PC alone. False only when a
    discard failed.

    `boot_nic` is the network card iPXE read; it is kept for the image's
    hardware check and Add new hardware (app.services.toolchain).
    """
    values: dict = {"ipxe_at": now or _now()}
    new_nic = bool(boot_nic) and boot_nic != m.boot_nic
    if new_nic:
        values["boot_nic"] = boot_nic
        logger.info("%s boots from network card PCI\\%s", m.name, boot_nic)
    # Observed state, not an edit of the machine: keep updated_at.
    db.execute(update(Machine).where(Machine.id == m.id)
               .values(**values, updated_at=Machine.updated_at))
    db.commit()
    db.refresh(m)
    due = _needs_discard(m)
    if not (settings.auto_discard and _managed(m) and due):
        return True
    if prov.session_active(m.initiator_iqn, m.iscsi_target_iqn) is not False:
        return True
    return discard(db, prov, m, "client booting (iPXE)")


def tick(make_session: Callable[[], Session], prov: Provisioner,
         settings: WritebackSettings, now: datetime | None = None) -> None:
    """One watcher round over all machines."""
    with make_session() as db:
        ids = db.scalars(
            select(Machine.id).where(
                Machine.status.in_([MachineStatus.PROVISIONED, MachineStatus.EDITING])
            )
        ).all()
        for machine_id in ids:
            m = db.get(Machine, machine_id)
            if m is None:
                continue
            try:
                observe(db, prov, m, settings, now)
            except Exception:       # one bad machine must not stop the others
                logger.exception("Writeback watcher failed for %s", m.name)
                db.rollback()


def measure(db: Session, prov: Provisioner, warning_bytes: int) -> None:
    """
    Store how much each PC has written into its writebacks (OS and game disk)
    and flag the ones over `warning_bytes` (0: none). A kept writeback is
    meant to grow (installs, updates), so it is never flagged. Nothing is
    done about a large one: a PC is never restarted for it. A new warning is
    logged once.
    """
    used = {c["zvol"]: c["used"] for c in prov.writebacks()}
    if not used:
        return      # nothing listed (ZFS unreadable): keep the last values
    machines = db.scalars(select(Machine).where(Machine.status.in_(
        [MachineStatus.PROVISIONED, MachineStatus.EDITING]))).all()
    for m in machines:
        zvols = [z for z in (m.clone_zvol, m.os_clone_zvol) if z]
        if not any(z in used for z in zvols):
            continue
        size = sum(used.get(z, 0) for z in zvols)
        large = bool(warning_bytes) and size > warning_bytes and not m.keep_writeback
        if large and not m.writeback_large:
            logger.warning("%s has written %.1f GB into its writeback (warning at %.0f GB)",
                           m.name, size / GB, warning_bytes / GB)
        if (size, large) != (m.writeback_bytes, m.writeback_large):
            # Observed state, not an edit of the machine: keep updated_at.
            db.execute(update(Machine).where(Machine.id == m.id).values(
                writeback_bytes=size, writeback_large=large, updated_at=Machine.updated_at))
    db.commit()


class WritebackSizer:
    """Background thread running measure() every `interval`."""

    def __init__(self, make_session: Callable[[], Session], prov: Provisioner,
                 interval: timedelta = timedelta(seconds=60)):
        self.make_session = make_session
        self.prov = prov
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ggnet-writeback-sizes", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        from app.services import retention     # retention imports this module

        while not self._stop.wait(self.interval.total_seconds()):
            try:
                with self.make_session() as db:
                    s, _ = retention.load_settings(db)
                    measure(db, self.prov, s.writeback_warning_gb * GB)
            except Exception:
                logger.exception("Measuring writebacks failed")


def refresh_spares(db: Session, prov: Provisioner) -> None:
    """
    Boot Mode PCs whose writebacks are reset on every restart get spares of
    them (Provisioner.make_spares); a PC that keeps its writebacks needs none.
    """
    for m in db.scalars(select(Machine).where(Machine.status == MachineStatus.PROVISIONED)).all():
        if m.clone_zvol is None:
            continue
        try:
            if m.mode is MachineMode.BOOT and _managed(m) and not m.keep_writeback:
                prov.make_spares(m.name, m.clone_zvol, m.target_snapshot_path,
                                 m.os_clone_zvol, m.os_snapshot_path, os_setup_for(m))
            else:
                prov.drop_spares(m.name, m.clone_zvol, m.os_clone_zvol)
        except Exception:             # one bad machine must not stop the others
            logger.exception("Spare writebacks of %s failed", m.name)


class SpareMaker:
    """Background thread running refresh_spares() every `interval`."""

    def __init__(self, make_session: Callable[[], Session], prov: Provisioner,
                 interval: timedelta = timedelta(seconds=30)):
        self.make_session = make_session
        self.prov = prov
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ggnet-spares", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval.total_seconds()):
            try:
                with self.make_session() as db:
                    refresh_spares(db, self.prov)
            except Exception:
                logger.exception("Making spare writebacks failed")


class WritebackWatcher:
    """Background thread running tick() every `settings.poll`."""

    def __init__(self, make_session: Callable[[], Session], prov: Provisioner,
                 settings: WritebackSettings):
        self.make_session = make_session
        self.prov = prov
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ggnet-writebacks", daemon=True)
        self._thread.start()
        logger.info("Writeback watcher started (grace %ss)", self.settings.grace.total_seconds())

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            # Let a discard under way finish (the unit stops with KillMode=mixed).
            self._thread.join(timeout=45)

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.poll.total_seconds()):
            try:
                tick(self.make_session, self.prov, self.settings)
            except Exception:
                logger.exception("Writeback watcher round failed")
