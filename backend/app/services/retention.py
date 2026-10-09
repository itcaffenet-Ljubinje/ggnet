"""
Automated snapshot and writeback removal (ggRock "Array and Images").

Without it, every Apply Writebacks leaves another held version of a game
disk, and kept writebacks of PCs that stay off keep their space, until the
pool is full. Settings come from the web UI (the `settings` table):

  reserved_percent         part of the pool kept free (a refreservation)
  warning_percent          the UI warns when the pool is fuller than this
  unused_snapshot_days     a version not active, not pinned, not the origin
                           of any PC's writeback and older than this ...
  keep_newest_snapshots    ... is deleted, except the newest N of each disk
  inactive_writeback_hours a Keep Writeback PC that has been off this long
                           gets its writeback discarded

The hourly job does nothing until the admin turns it on, and with dry_run it
only records what it would delete. Every deletion is re-checked on the host
right before it runs and logged.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, GameDisk, Machine, MachineMode, MachineStatus
from app.services.provisioning import Provisioner, ProvisioningError
from app.services.writebacks import discard

logger = logging.getLogger("ggnet.retention")

SETTINGS_KEY = "retention"
LAST_RUN_KEY = "retention_last_run"


@dataclass(frozen=True)
class RetentionSettings:
    enabled: bool = False
    dry_run: bool = True
    reserved_percent: int = 15
    warning_percent: int = 80
    unused_snapshot_days: int = 14
    keep_newest_snapshots: int = 3
    inactive_writeback_hours: int = 24


@dataclass
class Action:
    kind: str           # "snapshot" | "writeback"
    target: str         # disk@version, or the machine name
    reason: str
    done: bool = False
    error: str | None = None


@dataclass
class Report:
    at: datetime
    dry_run: bool
    actions: list[Action] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        d["at"] = self.at.isoformat()
        return json.dumps(d)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Settings ──────────────────────────────────────────────────────────

def load_settings(db: Session) -> tuple[RetentionSettings, bool]:
    """The saved settings and whether the admin ever saved them (else defaults)."""
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        return RetentionSettings(), False
    known = RetentionSettings.__dataclass_fields__
    values = {k: v for k, v in json.loads(row.value).items() if k in known}
    return RetentionSettings(**values), True


def save_settings(db: Session, s: RetentionSettings) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(asdict(s))))
    else:
        row.value = json.dumps(asdict(s))
    db.commit()


def last_report(db: Session) -> dict[str, Any] | None:
    row = db.get(AppSetting, LAST_RUN_KEY)
    return json.loads(row.value) if row else None


def _store_report(db: Session, report: Report) -> None:
    row = db.get(AppSetting, LAST_RUN_KEY)
    if row is None:
        db.add(AppSetting(key=LAST_RUN_KEY, value=report.to_json()))
    else:
        row.value = report.to_json()
    db.commit()


# ── Plan and run ──────────────────────────────────────────────────────

def plan(db: Session, prov: Provisioner, s: RetentionSettings, now: datetime | None = None) -> list[Action]:
    """What the current settings would delete right now; changes nothing."""
    now = now or _now()
    actions: list[Action] = []

    for disk in db.scalars(select(GameDisk).where(GameDisk.snapshot.is_not(None)).order_by(GameDisk.name)):
        versions = prov.snapshots(disk.zvol_path)   # oldest first
        pinned = {m.pinned_snapshot for m in disk.machines if m.pinned_snapshot}
        newest = {v["name"] for v in versions[-s.keep_newest_snapshots:]} if s.keep_newest_snapshots else set()
        for v in versions:
            if v["name"] == disk.snapshot or v["name"] in pinned or v["clones"] or v["name"] in newest:
                continue
            age = now - datetime.fromtimestamp(v["creation"], timezone.utc)
            if age >= timedelta(days=s.unused_snapshot_days):
                actions.append(Action("snapshot", f"{disk.name}@{v['name']}",
                                      f"not used, {age.days} days old"))

    keepers = db.scalars(select(Machine).where(
        Machine.keep_writeback.is_(True),
        Machine.status == MachineStatus.PROVISIONED,
        Machine.mode == MachineMode.DISK,
    ).order_by(Machine.name))
    for m in keepers:
        # Only a writeback with changes, of a PC known to be off since a known time.
        if not (m.writeback_dirty and m.session_active is False and m.session_changed_at
                and m.game_disk is not None and m.game_disk.published):
            continue
        off = now - m.session_changed_at
        if off >= timedelta(hours=s.inactive_writeback_hours):
            actions.append(Action("writeback", m.name,
                                  f"kept writeback, PC off for {int(off.total_seconds() // 3600)} hours"))
    return actions


def run(db: Session, prov: Provisioner, s: RetentionSettings, dry_run: bool,
        now: datetime | None = None) -> Report:
    """Plan, then (unless dry_run) delete, re-checking each item on the host first."""
    report = Report(at=now or _now(), dry_run=dry_run)
    try:
        report.actions = plan(db, prov, s, report.at)
    except ProvisioningError as e:
        report.actions = [Action("error", "-", "planning failed", error=str(e))]
        _store_report(db, report)
        return report

    for a in report.actions if not dry_run else []:
        try:
            if a.kind == "snapshot":
                disk_name, _, version = a.target.partition("@")
                disk = db.scalar(select(GameDisk).where(GameDisk.name == disk_name))
                # Re-check: the disk or a pin may have changed since planning.
                if disk is None or disk.snapshot == version or any(
                        m.pinned_snapshot == version for m in disk.machines):
                    a.error = "now in use; skipped"
                    continue
                prov.delete_snapshot(disk.zvol_path, version)   # refuses if a clone appeared
                a.done = True
            else:
                m = db.scalar(select(Machine).where(Machine.name == a.target))
                if m is None or not m.keep_writeback or m.game_disk is None:
                    a.error = "no longer applies; skipped"
                    continue
                if prov.session_active(m.initiator_iqn, m.iscsi_target_iqn) is not False:
                    a.error = "PC is connected (or its state is unknown); skipped"
                    continue
                a.done = discard(db, prov, m, "retention: inactive kept writeback")
                if not a.done:
                    a.error = m.last_error
        except ProvisioningError as e:
            a.error = str(e)
        logger.info("Retention %s %s (%s): %s", a.kind, a.target, a.reason,
                    "done" if a.done else a.error)
    _store_report(db, report)
    return report


def tick(make_session: Callable[[], Session], prov: Provisioner, now: datetime | None = None) -> Report | None:
    """One hourly round: nothing unless the admin turned retention on."""
    with make_session() as db:
        s, _ = load_settings(db)
        if not s.enabled:
            return None
        return run(db, prov, s, dry_run=s.dry_run, now=now)


@dataclass(frozen=True)
class JobSettings:
    job: bool = True
    interval: timedelta = timedelta(hours=1)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "JobSettings":
        r = cfg.get("retention") or {}
        return cls(job=bool(r.get("job", True)),
                   interval=timedelta(seconds=int(r.get("interval_seconds", 3600))))


class RetentionJob:
    """Background thread running tick() every `interval`; it never runs at startup."""

    def __init__(self, make_session: Callable[[], Session], prov: Provisioner, interval: timedelta):
        self.make_session = make_session
        self.prov = prov
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ggnet-retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval.total_seconds()):
            try:
                tick(self.make_session, self.prov)
            except Exception:
                logger.exception("Retention round failed")
