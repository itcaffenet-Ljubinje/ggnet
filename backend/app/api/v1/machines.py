"""
API for client machines, with auto-provisioning.

`game_disk_id` is the WANTED assignment, `status` is the actual host state:
  idle         no disk, nothing on the host
  provisioned  clone exists and is mapped on the shared iSCSI target
  editing      a draft master is mapped instead (game disk /edit); no
               assign or delete until /finish-edit
  error       an operation failed; `last_error` says why, and
               assign/reset/DELETE retry and clean up leftovers

Writebacks are discarded automatically by the server after every disconnect
(app.services.writebacks); `keep_writeback` keeps them, and
/apply-writebacks turns a kept writeback into the disk's next version.

Only Disk Mode machines can be provisioned; Boot Mode comes later.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner, get_traffic_monitor
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    BOOT_MODE_LETTER,
    DriveLetterAll,
    KeepWriteback,
    MachinePin,
    MachineTrafficOut,
    MachineAssign,
    MachineCreate,
    MachineOut,
    MachineUpdate,
)
from app.db.models import GameDisk, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services.provisioning import ClientDisk, Provisioner, ProvisioningError
from app.services.traffic import TrafficMonitor
from app.services.writebacks import discard

logger = logging.getLogger("ggnet.api.machines")

router = APIRouter(prefix="/machines", tags=["machines"])

DUPLICATE = "A machine with that name, IQN or MAC address already exists"


# ── Helpers ───────────────────────────────────────────────────────────

def _get(db: Session, machine_id: int) -> Machine:
    machine = db.get(Machine, machine_id)
    if machine is None:
        not_found("Machine", machine_id)
    return machine


def _published_disk(db: Session, disk_id: int) -> GameDisk:
    disk = db.get(GameDisk, disk_id)
    if disk is None:
        not_found("Game disk", disk_id)
    if not disk.published:
        conflict(f"Game disk '{disk.name}' is not published; call /publish first")
    return disk


def _require_disk_mode(mode: MachineMode, name: str) -> None:
    if mode is not MachineMode.DISK:
        conflict(f"Machine '{name}' is in {mode.value} mode; only disk mode can get a game disk")


def _not_editing(m: Machine) -> None:
    if m.status is MachineStatus.EDITING:
        conflict(f"'{m.name}' is editing game disk '{m.editing_disk.name}'; finish editing first")


def _has_host_state(m: Machine) -> bool:
    """Whether the host may hold something for this machine (clone/target)."""
    return m.status is not MachineStatus.IDLE or m.clone_zvol is not None


def _set_provisioned(m: Machine, cd: ClientDisk) -> None:
    m.status = MachineStatus.PROVISIONED
    m.last_error = None
    m.clone_zvol = cd.clone_zvol
    m.clone_snapshot = cd.clone_snapshot
    m.iscsi_target_iqn = cd.iscsi_target_iqn
    m.writeback_dirty = False


def _keep_sync(db: Session, prov: Provisioner, m: Machine) -> None:
    """A fresh clone is created with sync=disabled; a kept one needs sync=standard."""
    if m.keep_writeback and m.clone_zvol:
        try:
            prov.set_keep_writeback(m.clone_zvol, True)
        except ProvisioningError as e:
            _fail(db, m, e)


def _set_idle(m: Machine) -> None:
    m.status = MachineStatus.IDLE
    m.last_error = None
    m.clone_zvol = m.clone_snapshot = m.iscsi_target_iqn = None


def _fail(db: Session, m: Machine, e: ProvisioningError) -> None:
    """Save the error BEFORE answering 502; the state must outlive the request."""
    m.status = MachineStatus.ERROR
    m.last_error = str(e)
    db.commit()
    host_failed(str(e), machine_id=m.id)


def _deprovision(db: Session, prov: Provisioner, m: Machine) -> None:
    try:
        prov.deprovision(m.name, m.initiator_iqn, m.clone_zvol)
    except ProvisioningError as e:
        _fail(db, m, e)
    _set_idle(m)


def _provision(db: Session, prov: Provisioner, m: Machine, disk: GameDisk) -> None:
    m.game_disk_id = disk.id
    try:
        cd = prov.provision(m.name, m.initiator_iqn, disk.snapshot_path)
    except ProvisioningError as e:
        # The clone may be left over (rollback failed); remember its path for cleanup.
        m.clone_zvol = prov.client_path(m.name)
        _fail(db, m, e)
    _set_provisioned(m, cd)
    _keep_sync(db, prov, m)


# ── Routes ────────────────────────────────────────────────────────────

@router.get("", response_model=list[MachineOut])
def list_machines(db: Session = Depends(get_db)):
    return db.scalars(select(Machine).order_by(Machine.name)).all()


@router.put("/drive-letter", response_model=list[MachineOut])
def set_drive_letter_all(body: DriveLetterAll, db: Session = Depends(get_db)):
    """
    Give every Disk Mode machine the same game disk letter. The agents move
    the disk on their next heartbeat; nothing changes on the server host.
    """
    for m in db.scalars(select(Machine).where(Machine.mode == MachineMode.DISK)):
        m.drive_letter = body.drive_letter
    db.commit()
    return db.scalars(select(Machine).order_by(Machine.name)).all()


@router.get("/traffic", response_model=list[MachineTrafficOut])
def machine_traffic(
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    monitor: TrafficMonitor = Depends(get_traffic_monitor),
):
    """Sent / Received / Speed of every mapped machine, read live from LIO."""
    out = []
    mapped = db.scalars(select(Machine).where(
        Machine.status.in_([MachineStatus.PROVISIONED, MachineStatus.EDITING]),
        Machine.iscsi_target_iqn.is_not(None),
    ))
    for m in mapped:
        counters = prov.iscsi.acl_traffic(m.initiator_iqn, m.iscsi_target_iqn)
        if counters is not None:
            t = monitor.sample(m.id, *counters)
            out.append(MachineTrafficOut(machine_id=m.id, **asdict(t)))
    monitor.forget({t.machine_id for t in out})
    return out


@router.get("/{machine_id}", response_model=MachineOut)
def get_machine(machine_id: int, db: Session = Depends(get_db)):
    return _get(db, machine_id)


@router.post("", response_model=MachineOut, status_code=status.HTTP_201_CREATED)
def create_machine(
    body: MachineCreate,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Register a machine; with `game_disk_id` it is provisioned right away."""
    disk = None
    if body.game_disk_id is not None:
        _require_disk_mode(body.mode, body.name)
        disk = _published_disk(db, body.game_disk_id)

    machine = Machine(name=body.name, mode=body.mode, initiator_iqn=body.initiator_iqn, mac=body.mac,
                      drive_letter=body.drive_letter)
    db.add(machine)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conflict(DUPLICATE)

    if disk is not None:
        _provision(db, prov, machine, disk)
        db.commit()
    return machine


@router.patch("/{machine_id}", response_model=MachineOut)
def update_machine(machine_id: int, body: MachineUpdate, db: Session = Depends(get_db)):
    machine = _get(db, machine_id)
    changes = body.model_dump(exclude_unset=True)

    # The name is in the clone path and target name, the IQN in the ACL;
    # they cannot change on the host while the machine has a disk.
    host_bound = {k for k in ("name", "initiator_iqn", "mode") if k in changes
                  and changes[k] != getattr(machine, k)}
    if host_bound and (_has_host_state(machine) or machine.game_disk_id is not None):
        conflict(f"{', '.join(sorted(host_bound))} can only change while the machine has no disk")

    if changes.get("mode", machine.mode) is MachineMode.BOOT:
        if changes.get("drive_letter", "D") != "D":
            conflict(BOOT_MODE_LETTER)
        changes["drive_letter"] = "D"

    for key, value in changes.items():
        setattr(machine, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conflict(DUPLICATE)
    return machine


@router.post("/{machine_id}/assign", response_model=MachineOut)
def assign_disk(
    machine_id: int,
    body: MachineAssign,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Assign (or with `null` remove) a game disk. Changing the disk destroys
    the old clone, so everything the client wrote is lost. The client must
    be powered off or disconnected.
    """
    machine = _get(db, machine_id)
    _not_editing(machine)
    disk = None
    if body.game_disk_id is not None:
        _require_disk_mode(machine.mode, machine.name)
        disk = _published_disk(db, body.game_disk_id)

    if (disk is not None and machine.status is MachineStatus.PROVISIONED
            and machine.game_disk_id == disk.id):
        return machine   # already so; nothing is touched on the host

    if _has_host_state(machine):
        _deprovision(db, prov, machine)
    machine.game_disk_id = None
    machine.pinned_snapshot = None   # a pin names a version of the old disk

    if disk is not None:
        _provision(db, prov, machine, disk)
    db.commit()
    return machine


@router.post("/{machine_id}/reset", response_model=MachineOut)
def reset_machine(
    machine_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Return the machine's disk to a clean state of the disk's CURRENT
    snapshot. The client must be powered off or disconnected.

    Not in the UI: the server discards writebacks on its own after every
    disconnect. Kept for recovery (e.g. after an `error`).
    """
    machine = _get(db, machine_id)
    if machine.game_disk_id is None:
        conflict(f"Machine '{machine.name}' has no disk assigned")
    disk = _published_disk(db, machine.game_disk_id)

    clone = machine.clone_zvol or prov.client_path(machine.name)
    try:
        cd = prov.reset(machine.name, machine.initiator_iqn, clone, machine.target_snapshot_path)
    except ProvisioningError as e:
        machine.clone_zvol = clone
        _fail(db, machine, e)
    _set_provisioned(machine, cd)
    _keep_sync(db, prov, machine)
    db.commit()
    return machine


@router.put("/{machine_id}/keep-writeback", response_model=MachineOut)
def keep_writeback(
    machine_id: int,
    body: KeepWriteback,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Keep (or stop keeping) the machine's writeback across reboots. Only one
    machine at a time may keep its writeback: it is the PC where games are
    installed and updated before Apply Writebacks.

    Turning it off does not discard anything right away; the writeback is
    discarded at the next disconnect, like everyone else's.
    """
    machine = _get(db, machine_id)
    if body.enabled == machine.keep_writeback:
        return machine
    if body.enabled:
        other = db.scalar(select(Machine).where(Machine.keep_writeback.is_(True),
                                                Machine.id != machine.id))
        if other is not None:
            conflict(f"'{other.name}' already keeps its writeback; turn it off there first")
    if machine.clone_zvol and machine.status is MachineStatus.PROVISIONED:
        try:
            prov.set_keep_writeback(machine.clone_zvol, body.enabled)
        except ProvisioningError as e:
            _fail(db, machine, e)
    machine.keep_writeback = body.enabled
    db.commit()
    return machine


@router.put("/{machine_id}/pin", response_model=MachineOut)
def pin_snapshot(
    machine_id: int,
    body: MachinePin,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Run this machine on a chosen version of its game disk instead of the
    active one (`null` follows the active version again). Nothing changes on
    the host now: the PC moves to it at its next discard (reboot).
    """
    machine = _get(db, machine_id)
    if machine.game_disk_id is None:
        conflict(f"'{machine.name}' has no game disk")
    disk = _published_disk(db, machine.game_disk_id)
    if body.snapshot is not None:
        try:
            versions = prov.snapshots(disk.zvol_path)
        except ProvisioningError as e:
            host_failed(str(e), machine_id=machine.id)
        if body.snapshot not in {v["name"] for v in versions}:
            not_found("Snapshot", f"{disk.name}@{body.snapshot}")
    machine.pinned_snapshot = body.snapshot
    db.commit()
    return machine


@router.post("/{machine_id}/discard-writeback", response_model=MachineOut)
def discard_writeback(
    machine_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Throw away the machine's writeback now and re-clone it from the disk's
    active version, also when it keeps its writeback (e.g. an install that
    went wrong). The PC must be powered off.
    """
    machine = _get(db, machine_id)
    if machine.status is not MachineStatus.PROVISIONED or machine.game_disk_id is None:
        conflict(f"'{machine.name}' has no provisioned game disk")
    _published_disk(db, machine.game_disk_id)
    session = prov.session_active(machine.initiator_iqn, machine.iscsi_target_iqn)
    if session is None:
        host_failed(f"Cannot read the iSCSI session state of '{machine.name}'")
    if session:
        conflict(f"'{machine.name}' is still connected; shut it down first")
    if not discard(db, prov, machine, "discarded by the admin"):
        host_failed(machine.last_error or "Discarding the writeback failed", machine_id=machine.id)
    _keep_sync(db, prov, machine)
    db.commit()
    return machine


@router.post("/{machine_id}/apply-writebacks", response_model=MachineOut)
def apply_writebacks(
    machine_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    ggRock "Apply Writebacks": the machine's kept writeback becomes the next
    version of its game disk (base → v2 → v3 ...) and that version becomes
    active. Every other machine moves to it at its next discard, i.e. its
    next reboot. The machine must keep its writeback and be powered off.
    """
    machine = _get(db, machine_id)
    if not machine.keep_writeback:
        conflict(f"'{machine.name}' does not keep its writeback; nothing to apply")
    if machine.status is not MachineStatus.PROVISIONED or machine.game_disk_id is None:
        conflict(f"'{machine.name}' has no provisioned game disk")
    disk = _published_disk(db, machine.game_disk_id)
    if machine.clone_snapshot != disk.snapshot_path:
        conflict(
            f"'{machine.name}' runs {machine.clone_snapshot}, but the active version is "
            f"{disk.snapshot_path}; changes made on an older version cannot be applied"
        )
    session = prov.session_active(machine.initiator_iqn, machine.iscsi_target_iqn)
    if session is None:
        host_failed(f"Cannot read the iSCSI session state of '{machine.name}'")
    if session:
        conflict(f"'{machine.name}' is still connected; shut it down first")

    try:
        disk.snapshot = prov.apply_writebacks(machine.name, machine.initiator_iqn,
                                              machine.clone_zvol, disk.zvol_path, disk.snapshot)
    except ProvisioningError as e:
        host_failed(str(e), machine_id=machine.id)
    # The PC made the new version; it continues on it, not on an old pin.
    machine.pinned_snapshot = None
    db.commit()

    # The kept writeback now equals the new version; re-clone it from there
    # so the next round of changes starts from the active version.
    try:
        cd = prov.reset(machine.name, machine.initiator_iqn, machine.clone_zvol,
                        disk.snapshot_path)
    except ProvisioningError as e:
        _fail(db, machine, e)
    _set_provisioned(machine, cd)
    _keep_sync(db, prov, machine)
    db.commit()
    return machine


@router.delete("/{machine_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_machine(
    machine_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    machine = _get(db, machine_id)
    _not_editing(machine)
    if _has_host_state(machine):
        _deprovision(db, prov, machine)
    db.delete(machine)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
