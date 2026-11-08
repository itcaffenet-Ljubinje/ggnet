"""
API for the Server page: the ZFS pool, its disks, the RAM cache and the
writebacks' total (app.services.server_status). Read-only, except starting
or stopping a scrub or a TRIM, reading SMART on request and setting the RAM
cache's maximum.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner, get_server_monitor, get_updates
from app.api.v1.errors import conflict, host_failed
from app.api.v1.schemas import (
    ReleaseOut,
    UpdateIn,
    UpdateJobOut,
    UpdateOut,
    ArcIn,
    ArcOut,
    MaintenanceIn,
    MaintenanceOut,
    MemoryOut,
    PoolDiskOut,
    PoolGroupOut,
    PoolOut,
    PoolScanOut,
    PoolActionIn,
    ServerOut,
)
from app.db.session import get_db
from app.services import maintenance
from app.services.provisioning import Provisioner
from app.services.server_status import GiB, ServerMonitor, arc_limits, disk_device

router = APIRouter(prefix="/server", tags=["server"])


def _time(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def _pool(mon: ServerMonitor) -> PoolOut | None:
    p = mon.pool()
    if p is None:
        return None
    disks = []
    for d in p["disks"]:
        device = disk_device(d["path"])
        s = mon.smart(device) if device else {}
        disks.append(PoolDiskOut(
            name=d["name"], group=d["group"], state=d["state"],
            read_errors=d["read_errors"], write_errors=d["write_errors"],
            checksum_errors=d["checksum_errors"],
            trim_state=d["trim_state"], trim_percent=d["trim_percent"], trim_time=_time(d["trim_time"]),
            trim_supported=not d["trim_notsup"],
            smart_passed=s.get("passed"), model=s.get("model"), serial=s.get("serial"),
            capacity_bytes=s.get("capacity"), temperature=s.get("temperature"),
            wear_percent=s.get("wear_percent"), power_on_hours=s.get("power_on_hours"),
        ))
    trims = [d.trim_time for d in disks if d.trim_supported]
    scan = p["scan"]
    return PoolOut(
        name=p["name"], state=p["state"], size_bytes=p["size"], allocated_bytes=p["allocated"],
        free_bytes=p["free"], fragmentation=p["fragmentation"], capacity=p["capacity"],
        errors=p["errors"],
        scan=PoolScanOut(**{**scan, "start_time": _time(scan["start_time"]),
                            "end_time": _time(scan["end_time"])}) if scan else None,
        # The pool counts as trimmed when its least recently trimmed disk was.
        last_trim=None if not trims or None in trims else min(trims),
        autotrim=p.get("autotrim"),
        groups=[PoolGroupOut(**g) for g in p["groups"]],
        disks=disks,
    )


@router.get("", response_model=ServerOut)
def get_server(
    mon: ServerMonitor = Depends(get_server_monitor),
    prov: Provisioner = Depends(get_provisioner),
):
    arc = mon.arc()
    mem = mon.memory()
    clones = prov.writebacks()
    lookups = arc["hits"] + arc["misses"] if arc else 0
    low, high = arc_limits(mem["total"], arc["c_min"]) if arc and mem else (None, None)
    return ServerOut(
        pool=_pool(mon),
        arc=ArcOut(size_bytes=arc["size"], max_bytes=arc["c_max"], min_bytes=arc["c_min"],
                   hit_percent=round(100 * arc["hits"] / lookups, 1) if lookups else None,
                   allowed_min_bytes=low, allowed_max_bytes=high) if arc else None,
        memory=MemoryOut(total_bytes=mem["total"], available_bytes=mem["available"]) if mem else None,
        writebacks_bytes=sum(c["used"] for c in clones),
        writebacks_count=len(clones),
        smart_read_at=_time(mon.smart_read_at),
    )


@router.put("/arc", response_model=ServerOut)
def set_arc(
    body: ArcIn,
    mon: ServerMonitor = Depends(get_server_monitor),
    prov: Provisioner = Depends(get_provisioner),
):
    """The RAM cache's (ZFS ARC) maximum: applied now and kept for the next boot."""
    arc, mem = mon.arc(), mon.memory()
    if arc is None or mem is None:
        host_failed("The RAM cache or the server's memory cannot be read")
    low, high = arc_limits(mem["total"], arc["c_min"])
    nbytes = body.max_gb * GiB
    if not low <= nbytes <= high:
        conflict(f"The RAM cache must be {-(-low // GiB)}–{high // GiB} GB on this server "
                 "(the rest stays for Proxmox and its VMs)")
    problem = mon.set_arc_max(nbytes)
    if problem:
        host_failed(problem)
    return get_server(mon, prov)


@router.post("/smart", response_model=ServerOut)
def read_smart(
    mon: ServerMonitor = Depends(get_server_monitor),
    prov: Provisioner = Depends(get_provisioner),
):
    """Read every pool disk's SMART data now (smartctl); the page shows the last reading."""
    p = mon.pool()
    if p is None:
        host_failed(f"Reading pool {mon.pool_name} failed: {mon.last_error}")
    mon.read_smart([dev for d in p["disks"] if (dev := disk_device(d["path"]))])
    return get_server(mon, prov)


def _pool_action(mon: ServerMonitor, prov: Provisioner, what: str, start: bool) -> ServerOut:
    ok = mon.scrub(start) if what == "scrub" else mon.trim(start)
    if not ok:
        host_failed(f"{'Starting' if start else 'Stopping'} the {what} failed: {mon.last_error}")
    return get_server(mon, prov)


@router.post("/scrub", response_model=ServerOut)
def scrub(
    body: PoolActionIn,
    mon: ServerMonitor = Depends(get_server_monitor),
    prov: Provisioner = Depends(get_provisioner),
):
    """Start or stop a scrub of the pool. A scrub reads every block: PCs are slower meanwhile."""
    return _pool_action(mon, prov, "scrub", body.action == "start")


@router.post("/trim", response_model=ServerOut)
def trim(
    body: PoolActionIn,
    mon: ServerMonitor = Depends(get_server_monitor),
    prov: Provisioner = Depends(get_provisioner),
):
    """Start or stop a TRIM: ZFS tells each SSD which blocks are free, so writes stay fast."""
    return _pool_action(mon, prov, "trim", body.action == "start")


def _schedule(db: Session) -> MaintenanceOut:
    s = maintenance.load_settings(db)
    now = datetime.now()
    start, _ = maintenance.window_at(s, now)
    nxt = start + timedelta(days=7) if start <= now else start
    return MaintenanceOut(**asdict(s), next_window=nxt if s.enabled else None,
                          last_run=maintenance.last_run(db))


@router.get("/schedule", response_model=MaintenanceOut)
def get_schedule(db: Session = Depends(get_db)):
    """The weekly TRIM and scrub window, and what it did last."""
    return _schedule(db)


@router.put("/schedule", response_model=MaintenanceOut)
def put_schedule(body: MaintenanceIn, db: Session = Depends(get_db)):
    maintenance.save_settings(db, maintenance.MaintenanceSettings(**body.model_dump()))
    return _schedule(db)


# ── Updates of ggNet (app.services.updates) ───────────────────────────

def _update_out(updates, check: bool = False) -> UpdateOut:
    from app.services.updates import newer

    release, error = updates.latest(force=check)
    job = updates.status()
    problem = updates.installed()
    available = release is not None and newer(release.version, updates.version)
    reason = problem
    if reason is None and release is not None and available and not release.checked:
        reason = f"{release.tag} has no ggnet.tar.gz.sha256; it cannot be installed from here"
    if reason is None and job is not None and job["state"] == "running":
        reason = "an update is running"
    return UpdateOut(
        current=updates.version,
        latest=ReleaseOut(tag=release.tag, version=release.version, name=release.name, notes=release.notes,
                          url=release.url, published_at=release.published_at) if release else None,
        available=available, can_update=available and reason is None, reason=reason, error=error,
        job=UpdateJobOut(version=job["version"], from_version=job.get("from"), started_at=job["started_at"],
                         state=job["state"], exit=job["exit"], log=job["log"]) if job else None,
    )


@router.get("/update", response_model=UpdateOut)
def update_status(check: bool = False, updates=Depends(get_updates)):
    """The installed and the newest version; check=true asks GitHub again now."""
    return _update_out(updates, check)


@router.post("/update", response_model=UpdateOut)
def start_update(body: UpdateIn, updates=Depends(get_updates)):
    """Install a release: ggnet-update runs as its own unit and restarts ggnet (PCs keep running)."""
    from app.services.updates import UpdateError

    release, _ = updates.latest()
    if release is None or release.tag != body.version:
        conflict(f"{body.version} is not the newest release")
    try:
        updates.start(body.version)
    except UpdateError as e:
        conflict(str(e))
    return _update_out(updates)

