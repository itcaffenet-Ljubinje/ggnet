"""
API for client machines, with auto-provisioning.

`game_image_id` is the WANTED assignment, `status` is the actual host state:
  idle         no disk, nothing on the host
  provisioned  clone exists and is mapped on the shared iSCSI target
  editing      a draft master is mapped instead (game image /edit); no
               assign or delete until /finish-edit
  error       an operation failed; `last_error` says why, and
               assign/reset/DELETE retry and clean up leftovers

Writebacks are discarded automatically by the server after every disconnect
(app.services.writebacks); `keep_writeback` keeps them, and
/apply-writebacks turns a kept writeback into the disk's next version.

A Boot Mode machine gets two writebacks: one of its OS image (`os_image_id`,
mapped LUN 0) and one of its game image (mapped LUN 1). Both are provisioned,
discarded and removed together; /apply-writebacks applies either or both.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_provisioner,
    get_traffic_monitor,
    get_wol_sender,
    get_writeback_settings,
    require_user,
)
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    BOOT_MODE_LETTER,
    ApplyWritebacks,
    BulkAction,
    BulkResult,
    DriveLetterAll,
    KeepWriteback,
    MachinePin,
    PowerAction,
    VmAction,
    MachineTrafficOut,
    MachineAssign,
    MachineCreate,
    MachineOut,
    MachineUpdate,
    WINDOWS_IQN_PREFIX,
)
from app.db.models import ImageKind, Image, Machine, MachineMode, MachineStatus, User, UserRole
from app.db.session import get_db
from app.services import discovery
from app.services.os_setup import image_prep, os_setup_for
from app.services.provisioning import (
    ClientDisk,
    PartialApplyError,
    Provisioner,
    ProvisioningError,
    WritebackApply,
)
from app.services.traffic import TrafficMonitor
from app.services.vms import vmid_of
from app.services.writebacks import discard, is_booting

logger = logging.getLogger("ggnet.api.machines")

router = APIRouter(prefix="/machines", tags=["machines"])

DUPLICATE = "A machine with that name, IQN or MAC address already exists"
VM_IQN = "IQNs ending in ':vm-<number>' belong to virtual machines (Machines -> Create VM)"

# The agent beats every 30 s; like the UI, 90 s without one counts as offline.
AGENT_ONLINE = timedelta(seconds=90)


def agent_online(m: Machine, now: datetime | None = None) -> bool:
    """
    Whether the PC is up, the same rule as the UI's isOnline(): a recent
    heartbeat, no goodbye from the agent, and no iSCSI session that dropped
    after the last heartbeat (Windows closes it as it shuts down, which the
    writeback watcher sees within seconds, also when the agent cannot say so).
    """
    now = now or datetime.now(timezone.utc)
    if m.last_seen_at is None or m.agent_stopped or now - m.last_seen_at >= AGENT_ONLINE:
        return False
    return not (m.session_active is False and m.session_changed_at is not None
                and m.session_changed_at > m.last_seen_at)


# ── Helpers ───────────────────────────────────────────────────────────

def default_iqn(prov: Provisioner, name: str, mac: str | None) -> str:
    """A machine's IQN when none is given: from its MAC, else the Windows default."""
    return prov.iscsi.mac_initiator_iqn(mac) if mac else WINDOWS_IQN_PREFIX + name


def _get(db: Session, machine_id: int) -> Machine:
    machine = db.get(Machine, machine_id)
    if machine is None:
        not_found("Machine", machine_id)
    return machine


def _published_disk(db: Session, image_id: int, kind: ImageKind = ImageKind.GAME) -> Image:
    disk = db.get(Image, image_id)
    if disk is None:
        not_found("Game image", image_id)
    if disk.kind is not kind:
        conflict(f"'{disk.name}' is not a{'n OS image' if kind is ImageKind.OS else ' game image'}")
    if not disk.published:
        conflict(f"Image '{disk.name}' is not published; call /publish first")
    return disk


def _os_image(db: Session, mode: MachineMode, name: str, game_image_id: int | None,
             os_image_id: int | None) -> Image | None:
    """
    The OS image a machine gets with its game image: required in Boot Mode,
    refused in Disk Mode (Windows is on the PC's own SSD there).
    """
    if mode is MachineMode.DISK:
        if os_image_id is not None:
            conflict(f"Machine '{name}' is in disk mode; only boot mode gets an OS image")
        return None
    if game_image_id is None:
        return None
    if os_image_id is None:
        conflict(f"Machine '{name}' is in boot mode; it needs an OS image with its game image")
    return _published_disk(db, os_image_id, ImageKind.OS)


def _not_editing(m: Machine) -> None:
    if m.status is MachineStatus.EDITING:
        conflict(f"'{m.name}' is editing game image '{m.editing_image.name}'; finish editing first")


def _has_host_state(m: Machine) -> bool:
    """Whether the host may hold something for this machine (clone/target)."""
    return m.status is not MachineStatus.IDLE or m.clone_zvol is not None


def _shut_down_first(prov: Provisioner, m: Machine) -> None:
    """Refused while the PC runs: deleting it would pull its disks from under Windows."""
    if agent_online(m):
        conflict(f"'{m.name}' is on (its agent reports); shut it down first")
    if _has_host_state(m) and m.iscsi_target_iqn:
        session = prov.session_active(m.initiator_iqn, m.iscsi_target_iqn)
        if session is None:
            host_failed(f"Cannot read the iSCSI session state of '{m.name}'")
        if session:
            conflict(f"'{m.name}' is still connected; shut it down first")


def _in_use(prov: Provisioner, m: Machine, booting: bool = True) -> bool:
    """
    Whether the PC may be running on its disks right now: an iSCSI session,
    a reporting agent, or (`booting`) a Boot Mode PC between iPXE and
    Windows' own login, when it has no session for a moment. Its disks must
    not be replaced then.
    """
    if not _has_host_state(m):
        return False
    if agent_online(m) or (booting and is_booting(m, get_writeback_settings())):
        return True
    return bool(m.iscsi_target_iqn) and prov.session_active(m.initiator_iqn, m.iscsi_target_iqn) is True


def _set_provisioned(m: Machine, cd: ClientDisk) -> None:
    m.status = MachineStatus.PROVISIONED
    m.last_error = None
    m.clone_zvol = cd.clone_zvol
    m.clone_snapshot = cd.clone_snapshot
    m.os_clone_zvol = cd.os_clone_zvol
    m.os_clone_snapshot = cd.os_clone_snapshot
    m.iscsi_target_iqn = cd.iscsi_target_iqn
    m.writeback_dirty = False
    m.writeback_bytes, m.writeback_large = 0, False


def _keep_sync(db: Session, prov: Provisioner, m: Machine) -> None:
    """A fresh clone is created with sync=disabled; a kept one needs sync=standard."""
    if m.keep_writeback and m.clone_zvol:
        try:
            prov.set_keep_writeback(m.clone_zvol, True, m.os_clone_zvol)
        except ProvisioningError as e:
            _fail(db, m, e)


def _set_idle(m: Machine) -> None:
    m.status = MachineStatus.IDLE
    m.last_error = None
    m.clone_zvol = m.clone_snapshot = m.iscsi_target_iqn = None
    m.os_clone_zvol = m.os_clone_snapshot = None


def _fail(db: Session, m: Machine, e: ProvisioningError) -> None:
    """Save the error BEFORE answering 502; the state must outlive the request."""
    m.status = MachineStatus.ERROR
    m.last_error = str(e)
    db.commit()
    host_failed(str(e), machine_id=m.id)


def _deprovision(db: Session, prov: Provisioner, m: Machine) -> None:
    try:
        prov.deprovision(m.name, m.initiator_iqn, m.clone_zvol, m.os_clone_zvol)
    except ProvisioningError as e:
        _fail(db, m, e)
    _set_idle(m)


def _provision(db: Session, prov: Provisioner, m: Machine, disk: Image,
               os_image: Image | None = None) -> None:
    m.game_image_id = disk.id
    m.os_image_id = os_image.id if os_image else None
    try:
        cd = prov.provision(m.name, m.initiator_iqn, disk.snapshot_path,
                            os_image.snapshot_path if os_image else None, os_setup_for(m))
    except ProvisioningError as e:
        # The clones may be left over (rollback failed); remember their paths for cleanup.
        m.clone_zvol = prov.client_path(m.name)
        m.os_clone_zvol = prov.os_client_path(m.name) if os_image else None
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
    Give every Disk Mode machine the same game image letter. The agents move
    the disk on their next heartbeat; nothing changes on the server host.
    """
    for m in db.scalars(select(Machine).where(Machine.mode == MachineMode.DISK)):
        m.drive_letter = body.drive_letter
    db.commit()
    return db.scalars(select(Machine).order_by(Machine.name)).all()


@router.post("/bulk", response_model=list[BulkResult])
def bulk(
    body: BulkAction,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    wol=Depends(get_wol_sender),
    user: User = Depends(require_user),
):
    """
    Run one action on several machines, each exactly like its single-machine
    route (same checks, same messages). One machine failing does not stop the
    others; the answer says per machine what happened. Staff may only turn
    PCs on, shut them down and restart them. `discard` leaves PCs that keep
    their writeback alone; `delete` and `discard` refuse PCs that are on.
    """
    if user.role is not UserRole.ADMIN and body.action not in ("on", "shutdown", "reboot"):
        raise HTTPException(403, detail={"error": f"Your role ({user.role.value}) cannot do this"})
    def run(machine_id: int) -> None:
        if body.action in ("on", "shutdown", "reboot"):
            power(machine_id, PowerAction(action=body.action), db, wol, prov)
        elif body.action == "assign":
            assign_disk(machine_id, MachineAssign(game_image_id=body.game_image_id), db, prov)
        elif body.action == "assign_os":
            m = _get(db, machine_id)
            if m.mode is not MachineMode.BOOT:
                conflict(f"'{m.name}' is in Disk Mode: it runs its own Windows, no OS image")
            if m.game_image_id is None:
                conflict(f"'{m.name}' needs a game image with its OS image; set both in its settings")
            assign_disk(machine_id, MachineAssign(game_image_id=m.game_image_id, os_image_id=body.os_image_id),
                        db, prov)
        elif body.action == "pin":
            pin_snapshot(machine_id, MachinePin(snapshot=body.snapshot), db, prov)
        elif body.action == "drive_letter":
            update_machine(machine_id, MachineUpdate(drive_letter=body.drive_letter), db, prov)
        elif body.action in ("hide", "show"):
            update_machine(machine_id, MachineUpdate(hidden=body.action == "hide"), db, prov)
        elif body.action == "mode":
            update_machine(machine_id, MachineUpdate(mode=body.mode), db, prov)
        elif body.action == "discard":
            m = _get(db, machine_id)
            # A kept writeback is someone's install in progress: only on its own.
            if m.keep_writeback:
                conflict(f"'{m.name}' keeps its writeback; discard it on its own (Writeback -> Discard)")
            discard_writeback(machine_id, db, prov)
        elif body.action == "delete":
            delete_machine(machine_id, db, prov)
        else:
            keep_writeback(machine_id, KeepWriteback(enabled=False), db, prov)

    results = []
    for machine_id in dict.fromkeys(body.machine_ids):   # each once, in order
        m = db.get(Machine, machine_id)
        name = m.name if m else None
        try:
            run(machine_id)
        except HTTPException as e:
            db.rollback()
            detail = e.detail.get("error") if isinstance(e.detail, dict) else str(e.detail)
            results.append(BulkResult(machine_id=machine_id, name=name, ok=False, error=detail))
            continue
        results.append(BulkResult(machine_id=machine_id, name=name, ok=True))
    logger.info("Bulk %s on %d machine(s): %d failed", body.action, len(results),
                sum(not r.ok for r in results))
    return results


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
        Machine.vm_id.is_(None),                 # a VM's drives are not on the iSCSI target
    ))
    for m in mapped:
        counters = prov.iscsi.acl_traffic(m.initiator_iqn, m.iscsi_target_iqn,
                                          mapped_luns=2 if m.os_clone_zvol else 1)
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
    """
    Register a machine; with `game_image_id` (and `os_image_id` in Boot Mode)
    it is provisioned right away.
    """
    if body.initiator_iqn and vmid_of(body.initiator_iqn) is not None:
        conflict(VM_IQN)
    disk = None
    os_image = _os_image(db, body.mode, body.name, body.game_image_id, body.os_image_id)
    if body.game_image_id is not None:
        disk = _published_disk(db, body.game_image_id)
    elif body.os_image_id is not None:
        conflict(f"Machine '{body.name}' needs a game image with its OS image")

    machine = Machine(name=body.name, mode=body.mode, mac=body.mac, drive_letter=body.drive_letter,
                      initiator_iqn=body.initiator_iqn or default_iqn(prov, body.name, body.mac),
                      # Known when it waited on the iPXE screen: shown in the image's hardware check.
                      boot_nic=discovery.boot_nic_of(db, body.mac))
    db.add(machine)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conflict(DUPLICATE)
    discovery.forget(db, body.mac)
    db.commit()

    if disk is not None:
        _provision(db, prov, machine, disk, os_image)
        db.commit()
    return machine


@router.patch("/{machine_id}", response_model=MachineOut)
def update_machine(
    machine_id: int,
    body: MachineUpdate,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    machine = _get(db, machine_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("initiator_iqn") == "":
        changes["initiator_iqn"] = default_iqn(prov, changes.get("name", machine.name),
                                               changes.get("mac", machine.mac))

    # The IQN is in the ACL and the mode decides the disks; they cannot change
    # on the host while the machine has a disk. The name is in the writeback
    # paths and backstore names: a powered-off PC is renamed on the host too.
    if machine.vm_id is not None:
        fixed = sorted(k for k in ("initiator_iqn", "mode", "mac", "drive_letter")
                       if k in changes and changes[k] != getattr(machine, k))
        if fixed:
            conflict(f"{', '.join(fixed)} of a virtual machine cannot change")
    elif changes.get("initiator_iqn") and vmid_of(changes["initiator_iqn"]) is not None:
        conflict(VM_IQN)

    host_bound = {k for k in ("initiator_iqn", "mode") if k in changes
                  and changes[k] != getattr(machine, k)}
    if host_bound and (_has_host_state(machine) or machine.game_image_id is not None):
        conflict(f"{', '.join(sorted(host_bound))} can only change while the machine has no disk")
    if "name" in changes and changes["name"] != machine.name and _has_host_state(machine):
        _rename_on_host(db, prov, machine, changes["name"])
    if "name" in changes and changes["name"] != machine.name and machine.vm_id is not None:
        # Only the name Proxmox shows; the VM is found by its ID.
        if not prov.vms.host.rename(machine.vm_id, changes["name"]):
            logger.warning("VM %s keeps its old name: %s", machine.vm_id, prov.vms.host.runner.last_error)

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
    if changes.get("mac"):
        discovery.forget(db, changes["mac"])
        db.commit()
    return machine


def _rename_on_host(db: Session, prov: Provisioner, machine: Machine, new_name: str) -> None:
    """Rename a provisioned machine's writebacks; the caller saves the new name."""
    if machine.status is not MachineStatus.PROVISIONED or machine.clone_zvol is None:
        conflict(f"'{machine.name}' can only be renamed while it is provisioned or has no disk")
    if db.scalar(select(Machine).where(Machine.name == new_name, Machine.id != machine.id)):
        conflict(DUPLICATE)
    session = prov.session_active(machine.initiator_iqn, machine.iscsi_target_iqn)
    if session is None:
        host_failed(f"Cannot read the iSCSI session state of '{machine.name}'")
    if session:
        conflict(f"'{machine.name}' is still connected; shut it down first")
    try:
        machine.clone_zvol, machine.os_clone_zvol = prov.rename(
            machine.name, new_name, machine.initiator_iqn, machine.clone_zvol, machine.os_clone_zvol)
    except ProvisioningError as e:
        host_failed(str(e), machine_id=machine.id)


@router.post("/{machine_id}/assign", response_model=MachineOut)
def assign_disk(
    machine_id: int,
    body: MachineAssign,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Assign (or with `null` remove) a game image, and in Boot Mode the OS
    image with it (`os_image_id`; not sent keeps the current one). Changing a
    disk destroys the old clones, so everything the client wrote is lost.

    A PC that is on keeps running on its disks: the new images are only
    recorded, and its writebacks are made from them at its next restart (the
    automatic discard, as for a new version). Removing the disks, and
    changing them on a PC that keeps its writeback, need the PC off.
    """
    machine = _get(db, machine_id)
    _not_editing(machine)
    os_image_id = body.os_image_id if "os_image_id" in body.model_fields_set else machine.os_image_id
    if body.game_image_id is None and "os_image_id" in body.model_fields_set \
            and body.os_image_id is not None:
        conflict(f"Machine '{machine.name}' needs a game image with its OS image")
    os_image = _os_image(db, machine.mode, machine.name, body.game_image_id, os_image_id)
    disk = None
    if body.game_image_id is not None:
        disk = _published_disk(db, body.game_image_id)

    if (disk is not None and machine.status is MachineStatus.PROVISIONED
            and machine.game_image_id == disk.id
            and machine.os_image_id == (os_image.id if os_image else None)):
        return machine   # already so; nothing is touched on the host

    if _in_use(prov, machine):
        if disk is None:
            conflict(f"'{machine.name}' is on; shut it down first to remove its disks")
        if machine.status is not MachineStatus.PROVISIONED:
            conflict(f"'{machine.name}' is on; shut it down first")
        if machine.keep_writeback:
            conflict(f"'{machine.name}' is on and keeps its writeback, which a new image discards; "
                     "shut it down first")
        if machine.game_image_id != disk.id:
            machine.pinned_snapshot = None   # a pin names a version of the old disk
        machine.game_image_id = disk.id
        machine.os_image_id = os_image.id if os_image else None
        db.commit()
        logger.info("%s is on: it gets %s at its next restart", machine.name,
                    " + ".join(i.name for i in (os_image, disk) if i is not None))
        return machine

    if _has_host_state(machine):
        _deprovision(db, prov, machine)
    machine.game_image_id = machine.os_image_id = None
    machine.pinned_snapshot = None   # a pin names a version of the old disk

    if disk is not None:
        _provision(db, prov, machine, disk, os_image)
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
    snapshot. Refused while the PC is on.

    Not in the UI: the server discards writebacks on its own after every
    disconnect. Kept for recovery (e.g. after an `error`).
    """
    machine = _get(db, machine_id)
    if machine.game_image_id is None:
        conflict(f"Machine '{machine.name}' has no disk assigned")
    # A recovery tool: the boot window alone (a PC that failed to start) does not stop it.
    if _in_use(prov, machine, booting=False):
        conflict(f"'{machine.name}' is on; shut it down first")
    disk = _published_disk(db, machine.game_image_id)

    clone = machine.clone_zvol or prov.client_path(machine.name)
    os_clone = None
    if machine.mode is MachineMode.BOOT:
        if machine.os_image_id is None:
            conflict(f"Machine '{machine.name}' has no OS image assigned")
        _published_disk(db, machine.os_image_id, ImageKind.OS)
        os_clone = machine.os_clone_zvol or prov.os_client_path(machine.name)
    try:
        cd = prov.reset(machine.name, machine.initiator_iqn, clone, machine.target_snapshot_path,
                        os_clone, machine.os_snapshot_path, os_setup_for(machine))
    except ProvisioningError as e:
        machine.clone_zvol, machine.os_clone_zvol = clone, os_clone
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
            prov.set_keep_writeback(machine.clone_zvol, body.enabled, machine.os_clone_zvol)
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
    Run this machine on a chosen version of its game image instead of the
    active one (`null` follows the active version again). Nothing changes on
    the host now: the PC moves to it at its next discard (reboot).
    """
    machine = _get(db, machine_id)
    if machine.game_image_id is None:
        conflict(f"'{machine.name}' has no game image")
    disk = _published_disk(db, machine.game_image_id)
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


@router.post("/{machine_id}/power", response_model=MachineOut)
def power(
    machine_id: int,
    body: PowerAction,
    db: Session = Depends(get_db),
    wol=Depends(get_wol_sender),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Turn On sends Wake-on-LAN now. Shutdown and Reboot wait for the agent's
    next heartbeat (up to its interval) and expire after a few minutes.
    A virtual machine is started, shut down and rebooted by Proxmox instead.
    """
    machine = _get(db, machine_id)
    if machine.vm_id is not None:
        from app.api.v1.vms import vm_action      # vms imports this module

        vm_action(machine_id, VmAction(action="start" if body.action == "on" else body.action),
                  db, prov.vms.host)
        return machine
    if body.action == "on":
        mac = machine.mac or machine.reported_mac
        if mac is None:
            conflict(f"'{machine.name}' has no MAC address; set one or let the agent report it")
        try:
            wol(mac)
        except OSError as e:
            host_failed(f"Sending Wake-on-LAN to {mac} failed: {e}", machine_id=machine.id)
        return machine

    if not agent_online(machine):
        conflict(f"'{machine.name}' is offline; its agent cannot receive a {body.action}")
    machine.pending_command = body.action
    machine.pending_command_at = datetime.now(timezone.utc)
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
    if machine.status is not MachineStatus.PROVISIONED or machine.game_image_id is None:
        conflict(f"'{machine.name}' has no provisioned game image")
    _published_disk(db, machine.game_image_id)
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
    body: ApplyWritebacks | None = None,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Apply Writebacks: the machine's kept writeback becomes the next
    version of its game image (base → v2 → v3 ...) and that version becomes
    active. Every other machine moves to it at its next discard, i.e. its
    next reboot. The machine must keep its writeback and be powered off.

    A Boot Mode machine has two writebacks; `os` and `game` choose which of
    them become versions (both by default). One that is not applied stays
    kept as it is.
    """
    body = body or ApplyWritebacks()
    machine = _get(db, machine_id)
    boot = machine.mode is MachineMode.BOOT
    if not machine.keep_writeback:
        conflict(f"'{machine.name}' does not keep its writeback; nothing to apply")
    if machine.status is not MachineStatus.PROVISIONED or machine.game_image_id is None:
        conflict(f"'{machine.name}' has no provisioned game image")
    if not body.game and not (boot and body.os):
        conflict("Nothing to apply; choose the OS writeback, the game writeback or both")

    # (disk, writeback) pairs; each writeback must come from its disk's active version.
    targets: list[tuple[Image, str]] = []
    if body.game:
        targets.append((_published_disk(db, machine.game_image_id), machine.clone_zvol))
    if boot and body.os:
        if machine.os_image_id is None or machine.os_clone_zvol is None:
            conflict(f"'{machine.name}' has no provisioned OS image")
        targets.append((_published_disk(db, machine.os_image_id, ImageKind.OS), machine.os_clone_zvol))
    runs = {machine.clone_zvol: machine.clone_snapshot, machine.os_clone_zvol: machine.os_clone_snapshot}
    for disk, clone in targets:
        if runs[clone] != disk.snapshot_path:
            conflict(
                f"'{machine.name}' runs {runs[clone]}, but the active version is "
                f"{disk.snapshot_path}; changes made on an older version cannot be applied"
            )
    session = prov.session_active(machine.initiator_iqn, machine.iscsi_target_iqn)
    if session is None:
        host_failed(f"Cannot read the iSCSI session state of '{machine.name}'")
    if session:
        conflict(f"'{machine.name}' is still connected; shut it down first")

    # Windows turns on Device encryption where it finds a TPM and Secure Boot
    # (a VM has both). An encrypted version could no longer be prepared or set
    # up per PC, and every PC running it would go on encrypting into its writeback.
    for disk, clone in targets:
        encrypted = prov.bitlocker(clone)
        if encrypted:
            raise HTTPException(409, detail={
                "error": f"The {'OS' if disk.kind is ImageKind.OS else 'game'} writeback of '{machine.name}' is "
                         "encrypted with BitLocker (Windows turned on Device encryption). Start it, turn "
                         "encryption off (Settings -> Privacy & security -> Device encryption, or "
                         "'manage-bde -off C:' and 'manage-bde -off D:'), wait until 'manage-bde -status' "
                         "says Fully Decrypted, shut it down and apply again.",
                "bitlocker": encrypted,
            })

    # An unfinished Windows update would be redone by every PC at every boot.
    update: list[str] | None = None
    if boot and body.os:
        found = prov.writeback_update(machine.os_clone_zvol)
        if isinstance(found, str):
            logger.warning("Cannot check '%s' for an unfinished Windows update: %s", machine.name, found)
        else:
            update = found
        if update and not body.ignore_update:
            raise HTTPException(409, detail={
                "error": f"Windows on '{machine.name}' has not finished an update ({', '.join(update)}). "
                         "Start the PC once more and let Windows finish it, shut it down, then apply; "
                         "otherwise every PC redoes the update at every boot.",
                "update_pending": update,
            })

    applies = [WritebackApply(clone, disk.zvol_path, disk.snapshot) for disk, clone in targets]
    try:
        news = prov.apply_writebacks(machine.name, machine.initiator_iqn, applies,
                                     machine.clone_zvol, machine.os_clone_zvol if boot else None,
                                     os_prep=image_prep() if boot and body.os else None)
    except PartialApplyError as e:
        for disk, _ in targets:
            disk.snapshot = e.applied.get(disk.zvol_path, disk.snapshot)
        _fail(db, machine, e)
    except ProvisioningError as e:
        host_failed(str(e), machine_id=machine.id)
    for (disk, _), new in zip(targets, news):
        disk.snapshot = new
        if update is not None and disk.kind is ImageKind.OS:
            prov.zfs.set_snapshot_update(f"{disk.zvol_path}@{new}", update)
        if body.comment:
            try:
                prov.set_comment(disk.zvol_path, new, body.comment)
            except ProvisioningError as e:   # the version exists; only its comment is missing
                logger.warning("%s", e)
    if body.game:
        # The PC made the new version; it continues on it, not on an old pin.
        machine.pinned_snapshot = None
    db.commit()

    # An applied writeback now equals the new version; re-clone it from there
    # so the next round of changes starts from the active version. One that
    # was not applied is only mapped again.
    keep = frozenset(
        name for name, clone in (("game", machine.clone_zvol), ("os", machine.os_clone_zvol))
        if clone is not None and clone not in {c for _, c in targets}
    )
    game_snap = machine.clone_snapshot if "game" in keep else machine.target_snapshot_path
    os_clone = machine.os_clone_zvol if boot else None
    os_snap = None
    if os_clone is not None:
        os_snap = machine.os_clone_snapshot if "os" in keep else machine.os_snapshot_path
    try:
        cd = prov.reset(machine.name, machine.initiator_iqn, machine.clone_zvol, game_snap,
                        os_clone, os_snap, os_setup_for(machine) if os_clone else None, keep)
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
    _shut_down_first(prov, machine)
    if _has_host_state(machine):
        _deprovision(db, prov, machine)
    if machine.vm_id is not None and not prov.vms.host.destroy(machine.vm_id):
        db.commit()        # the writebacks are gone; the machine stays until its VM is removed
        host_failed(f"Removing VM {machine.vm_id} failed: {prov.vms.host.runner.last_error}",
                    machine_id=machine.id)
    db.delete(machine)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
