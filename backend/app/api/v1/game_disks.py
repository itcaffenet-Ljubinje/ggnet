"""
API for game disks (master images).

Flow: POST (empty zvol, draft) → POST /edit (the draft becomes one PC's
game disk; partition and fill it there) → shut that PC down → POST
/finish-edit → POST /publish (@base, hold, readonly) → assign to machines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    ActiveSnapshot,
    GameDiskCreate,
    GameDiskEdit,
    GameDiskOut,
    SnapshotOut,
    WritebackOut,
)
from app.db.models import GameDisk, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.api.game_disks")

router = APIRouter(prefix="/game-disks", tags=["game-disks"])


def _get(db: Session, disk_id: int) -> GameDisk:
    disk = db.get(GameDisk, disk_id)
    if disk is None:
        not_found("Game disk", disk_id)
    return disk


@router.get("", response_model=list[GameDiskOut])
def list_disks(db: Session = Depends(get_db)):
    return db.scalars(select(GameDisk).order_by(GameDisk.name)).all()


@router.get("/{disk_id}", response_model=GameDiskOut)
def get_disk(disk_id: int, db: Session = Depends(get_db)):
    return _get(db, disk_id)


@router.post("", response_model=GameDiskOut, status_code=status.HTTP_201_CREATED)
def create_disk(
    body: GameDiskCreate,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    if db.scalar(select(GameDisk).where(GameDisk.name == body.name)):
        conflict(f"Game disk '{body.name}' already exists")
    try:
        path = prov.create_disk(body.name, body.size_gb)
    except ProvisioningError as e:
        host_failed(str(e))

    disk = GameDisk(name=body.name, zvol_path=path, size_gb=body.size_gb)
    db.add(disk)
    try:
        db.commit()
    except IntegrityError:
        # A parallel request with the same name: do not leave an orphan on the host.
        db.rollback()
        try:
            prov.delete_disk(path, published=False)
        except ProvisioningError:
            logger.error("Zvol %s left on the host without a database record", path)
        conflict(f"Game disk '{body.name}' already exists")
    return disk


def _not_edited(disk: GameDisk) -> None:
    if disk.editor is not None:
        conflict(f"Game disk '{disk.name}' is being edited on '{disk.editor.name}'; finish editing first")


@router.post("/{disk_id}/edit", response_model=GameDiskOut)
def start_edit(
    disk_id: int,
    body: GameDiskEdit,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Map the draft master as the game disk of one machine, so it can be
    partitioned and filled on that PC. The machine must have no game disk.
    A published disk is changed with Keep Writeback + Apply Writebacks instead.
    """
    disk = _get(db, disk_id)
    if disk.published:
        conflict(f"Game disk '{disk.name}' is published; change it with Keep Writeback "
                 "and Apply Writebacks")
    machine = db.get(Machine, body.machine_id)
    if machine is None:
        not_found("Machine", body.machine_id)
    if disk.editor is not None:
        if disk.editor.id == machine.id:
            return disk
        _not_edited(disk)
    if machine.mode is not MachineMode.DISK:
        conflict(f"Machine '{machine.name}' is in {machine.mode.value} mode")
    if machine.status is not MachineStatus.IDLE or machine.game_disk_id is not None:
        conflict(f"Machine '{machine.name}' has a game disk or is busy; remove its disk first")

    try:
        iqn = prov.start_edit(machine.name, machine.initiator_iqn, disk.zvol_path)
    except ProvisioningError as e:
        host_failed(str(e), machine_id=machine.id)
    machine.status = MachineStatus.EDITING
    machine.editing_disk_id = disk.id
    machine.iscsi_target_iqn = iqn
    machine.last_error = None
    machine.session_active = machine.session_changed_at = None
    db.commit()
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/finish-edit", response_model=GameDiskOut)
def finish_edit(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Unmap the master from its editing machine; that PC must be shut down first."""
    disk = _get(db, disk_id)
    machine = disk.editor
    if machine is None:
        conflict(f"Game disk '{disk.name}' is not being edited")
    session = prov.session_active(machine.initiator_iqn, machine.iscsi_target_iqn)
    if session is None:
        host_failed(f"Cannot read the iSCSI session state of '{machine.name}'", machine_id=machine.id)
    if session:
        conflict(f"'{machine.name}' is still connected; shut it down first")

    try:
        prov.finish_edit(machine.name, machine.initiator_iqn)
    except ProvisioningError as e:
        host_failed(str(e), machine_id=machine.id)
    machine.status = MachineStatus.IDLE
    machine.editing_disk_id = None
    machine.iscsi_target_iqn = None
    machine.session_active = machine.session_changed_at = None
    db.commit()
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/publish", response_model=GameDiskOut)
def publish_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    disk = _get(db, disk_id)
    if disk.published:
        conflict(f"Game disk '{disk.name}' is already published (@{disk.snapshot})")
    _not_edited(disk)
    try:
        disk.snapshot = prov.publish_disk(disk.zvol_path)
    except ProvisioningError as e:
        host_failed(str(e))
    db.commit()
    return disk


def _published(disk: GameDisk) -> None:
    if not disk.published:
        conflict(f"Game disk '{disk.name}' is not published; it has no versions yet")


def _versions(prov: Provisioner, disk: GameDisk) -> list[dict]:
    try:
        return prov.snapshots(disk.zvol_path)
    except ProvisioningError as e:
        host_failed(str(e))


def _machines_by_clone(db: Session) -> dict[str, Machine]:
    return {m.clone_zvol: m for m in db.scalars(select(Machine).where(Machine.clone_zvol.is_not(None)))}


@router.get("/{disk_id}/snapshots", response_model=list[SnapshotOut])
def list_snapshots(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Versions of the disk, oldest first, read live from ZFS."""
    disk = _get(db, disk_id)
    if not disk.published:
        return []
    by_clone = _machines_by_clone(db)
    return [
        SnapshotOut(
            name=v["name"],
            created_at=datetime.fromtimestamp(v["creation"], timezone.utc),
            used_bytes=v["used"],
            referenced_bytes=v["referenced"],
            active=v["name"] == disk.snapshot,
            machines=sorted(by_clone[c].name if c in by_clone else c.rsplit("/", 1)[-1]
                            for c in v["clones"]),
            pinned=sorted(m.name for m in disk.machines if m.pinned_snapshot == v["name"]),
        )
        for v in _versions(prov, disk)
    ]


@router.put("/{disk_id}/active-snapshot", response_model=GameDiskOut)
def set_active_snapshot(
    disk_id: int,
    body: ActiveSnapshot,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Make another version active (e.g. roll everyone back to an older one).
    Nothing changes on the host: each PC moves to it at its next discard;
    PCs with Keep Writeback stay where they are.
    """
    disk = _get(db, disk_id)
    _published(disk)
    if body.snapshot not in {v["name"] for v in _versions(prov, disk)}:
        not_found("Snapshot", f"{disk.name}@{body.snapshot}")
    disk.snapshot = body.snapshot
    db.commit()
    return disk


@router.delete("/{disk_id}/snapshots/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_snapshot(
    disk_id: int,
    name: str,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Delete a version that is not active and that no PC's writeback was cloned from."""
    disk = _get(db, disk_id)
    _published(disk)
    version = next((v for v in _versions(prov, disk) if v["name"] == name), None)
    if version is None:
        not_found("Snapshot", f"{disk.name}@{name}")
    if name == disk.snapshot:
        conflict(f"{disk.name}@{name} is the active version; make another one active first")
    pinned = sorted(m.name for m in disk.machines if m.pinned_snapshot == name)
    if pinned:
        conflict(f"{disk.name}@{name} is pinned on {', '.join(pinned)}; unpin it first")
    if version["clones"]:
        by_clone = _machines_by_clone(db)
        users = sorted(by_clone[c].name if c in by_clone else c for c in version["clones"])
        conflict(f"{disk.name}@{name} is still used by {', '.join(users)}; "
                 "they move off it at their next reboot")
    try:
        prov.delete_snapshot(disk.zvol_path, name)
    except ProvisioningError as e:
        host_failed(str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{disk_id}/writebacks", response_model=list[WritebackOut])
def list_writebacks(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Every PC writeback cloned from any version of this disk, read live from ZFS."""
    disk = _get(db, disk_id)
    by_clone = _machines_by_clone(db)
    prefix = f"{disk.zvol_path}@"
    out = []
    for c in prov.writebacks():
        if not c["cloned_from"].startswith(prefix):
            continue
        m = by_clone.get(c["zvol"])
        snapshot = c["cloned_from"].partition("@")[2]
        out.append(WritebackOut(
            machine_id=m.id if m else None,
            machine_name=m.name if m else c["zvol"].rsplit("/", 1)[-1],
            zvol=c["zvol"],
            snapshot=snapshot,
            used_bytes=c["used"],
            keep_writeback=m.keep_writeback if m else False,
            pinned_snapshot=m.pinned_snapshot if m else None,
            session_active=m.session_active if m else None,
            outdated=m.outdated if m else snapshot != disk.snapshot,
        ))
    return sorted(out, key=lambda w: w.machine_name)


@router.delete("/{disk_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    disk = _get(db, disk_id)
    _not_edited(disk)
    assigned = db.scalar(
        select(func.count()).select_from(Machine).where(Machine.game_disk_id == disk.id)
    )
    if assigned:
        conflict(f"Game disk '{disk.name}' is assigned to {assigned} machine(s); move them first")
    try:
        prov.delete_disk(disk.zvol_path, disk.published)
    except ProvisioningError as e:
        host_failed(str(e))
    db.delete(disk)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
