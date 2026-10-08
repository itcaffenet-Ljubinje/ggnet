"""
Automatic writeback discard (ggRock model).

A machine's writeback is thrown away by the SERVER whenever the client goes
away, so every boot starts from a clean copy of the game disk's active
snapshot. Nobody clicks "Reset". Two triggers:

  watcher   LIO shows no iSCSI session for the machine for `grace` seconds
            (shutdown, restart, cable pulled). The grace period keeps a short
            network blip from wiping a disk Windows still has mounted.
  boot      ggnet-agent reports a newer Windows boot time while the machine
            has no session yet. This covers a restart that reconnects
            faster than the grace period.

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
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.writebacks")

# Windows boot times from two heartbeats of the same boot differ by
# rounding; only a jump larger than this is a new boot.
BOOT_TOLERANCE = timedelta(seconds=120)


@dataclass(frozen=True)
class WritebackSettings:
    auto_discard: bool = True
    grace: timedelta = timedelta(seconds=30)
    poll: timedelta = timedelta(seconds=5)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "WritebackSettings":
        wb = cfg.get("writebacks") or {}
        return cls(
            auto_discard=bool(wb.get("auto_discard", True)),
            grace=timedelta(seconds=int(wb.get("grace_seconds", 30))),
            poll=timedelta(seconds=int(wb.get("poll_seconds", 5))),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _managed(m: Machine) -> bool:
    """Machines whose writeback this module may discard."""
    return (
        m.mode is MachineMode.DISK
        and m.status is MachineStatus.PROVISIONED
        and m.game_disk is not None
        and m.game_disk.published
    )


def _needs_discard(m: Machine) -> bool:
    return not m.keep_writeback and (m.writeback_dirty or m.outdated)


def discard(db: Session, prov: Provisioner, m: Machine, reason: str) -> bool:
    """
    Re-clone the machine's writeback from its disk's active snapshot.
    Commits. On a host error the machine goes to `error` with the reason.
    """
    clone = m.clone_zvol or prov.client_path(m.name)
    try:
        cd = prov.reset(m.name, m.initiator_iqn, clone, m.game_disk.snapshot_path)
    except ProvisioningError as e:
        m.status = MachineStatus.ERROR
        m.last_error = f"Automatic writeback discard failed: {e}"
        db.commit()
        return False
    m.clone_zvol, m.clone_snapshot = cd.clone_zvol, cd.clone_snapshot
    m.iscsi_target_iqn = cd.iscsi_target_iqn
    m.writeback_dirty = False
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
    if (settings.auto_discard and _needs_discard(m)
            and m.session_changed_at is not None
            and now - m.session_changed_at >= settings.grace):
        discard(db, prov, m, "client disconnected")


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
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.poll.total_seconds()):
            try:
                tick(self.make_session, self.prov, self.settings)
            except Exception:
                logger.exception("Writeback watcher round failed")
