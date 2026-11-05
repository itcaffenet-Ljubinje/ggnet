"""
API for game disks (master images).

Flow: POST (empty zvol, draft) → POST /edit (the draft becomes one PC's
game disk; partition and fill it there) → shut that PC down → POST
/finish-edit → POST /publish (@base, hold, readonly) → assign to machines.

A disk of kind `os` is the Windows image of Boot Mode machines. It is not
filled with Edit on PC; importing a .vhdx into it comes with Boot Mode.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_disk_copier, get_hardware_adder, get_provisioner
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    ActiveSnapshot,
    AddHardwareIn,
    AddHardwareJobOut,
    AddHardwareStatusOut,
    AddHardwareWizardOut,
    GameDiskCopy,
    GameDiskCreate,
    GameDiskGrow,
    GameDiskGrown,
    GameDiskEdit,
    GameDiskOut,
    GameDiskRename,
    HardwareOut,
    NicSupportOut,
    PrepareImage,
    SnapshotComment,
    SnapshotLock,
    SnapshotOut,
    WritebackOut,
)
from app.db.models import DiskKind, GameDisk, ImageHardware, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services.agent_package import default_package
from app.services.os_setup import image_prep
from app.services.provisioning import Provisioner, ProvisioningError
from app.services.scan import nic_status
from app.services.toolchain import activate_when_done

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
    # A renamed disk keeps its zvol, so its old name stays taken on the host.
    owner = db.scalar(select(GameDisk).where(GameDisk.zvol_path == prov.image_path(body.name)))
    if owner is not None:
        conflict(f"'{body.name}' is still the zvol name of '{owner.name}' (renamed); choose another name")
    try:
        path = prov.create_disk(body.name, body.size_gb, os_image=body.kind is DiskKind.OS)
    except ProvisioningError as e:
        host_failed(str(e))

    disk = GameDisk(name=body.name, zvol_path=path, size_gb=body.size_gb, kind=body.kind)
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


@router.patch("/{disk_id}", response_model=GameDiskOut)
def rename_disk(disk_id: int, body: GameDiskRename, db: Session = Depends(get_db)):
    """
    Rename a disk in ggNet. Only the name changes: the zvol, its versions and
    every PC's writeback stay where they are, so this is safe while PCs run.
    """
    disk = _get(db, disk_id)
    if body.name == disk.name:
        return disk
    if db.scalar(select(GameDisk).where(GameDisk.name == body.name)):
        conflict(f"Game disk '{body.name}' already exists")
    old, disk.name = disk.name, body.name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conflict(f"Game disk '{body.name}' already exists")
    logger.info("Disk '%s' renamed to '%s' (zvol %s unchanged)", old, disk.name, disk.zvol_path)
    return disk


@router.post("/{disk_id}/copy", response_model=GameDiskOut, status_code=status.HTTP_201_CREATED)
def copy_disk(
    disk_id: int,
    body: GameDiskCopy,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    copier=Depends(get_disk_copier),
):
    """
    Copy one version of this image (default: the active one) into a new,
    independent image of the same kind. The copy runs in the background;
    the new disk shows its progress and is published at @base when done.
    """
    src = _get(db, disk_id)
    _published(src)
    snap = body.snapshot or src.snapshot
    if snap not in {v["name"] for v in _versions(prov, src)}:
        not_found("Snapshot", f"{src.name}@{snap}")
    if db.scalar(select(GameDisk).where(GameDisk.name == body.name)):
        conflict(f"Game disk '{body.name}' already exists")
    path = prov.image_path(body.name)
    owner = db.scalar(select(GameDisk).where(GameDisk.zvol_path == path))
    if owner is not None:
        conflict(f"'{body.name}' is still the zvol name of '{owner.name}' (renamed); choose another name")
    if prov.zfs.dataset_exists(path):
        conflict(f"Zvol {path} already exists on the host")
    disk = GameDisk(name=body.name, zvol_path=path, size_gb=src.size_gb, kind=src.kind,
                    copy_source=f"{src.zvol_path}@{snap}", copy_percent=0)
    # The copy knows the cards the copied version has.
    disk.hardware = [ImageHardware(nic=h.nic, instance=h.instance, name=h.name, snapshot=h.snapshot)
                     for h in src.hardware if _version_number(h.snapshot) <= _version_number(snap)]
    db.add(disk)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        conflict(f"Game disk '{body.name}' already exists")
    logger.info("Copy of %s@%s to '%s' started", src.name, snap, disk.name)
    copier.start(disk.id)
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/grow", response_model=GameDiskGrown)
def grow_disk(
    disk_id: int,
    body: GameDiskGrow,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Make a disk bigger (never smaller). A published disk gets a new active
    version with the bigger size; PCs move to it at their next reboot. Its
    data partition is grown on the server too (`extend`, app.disk_grow): a
    game disk's last partition, or an OS image's C: after its Recovery
    partition was moved to the end. Any other layout is extended in Windows.
    """
    disk = _get(db, disk_id)
    _not_copying(disk)
    _not_edited(disk)
    if body.size_gb <= disk.size_gb:
        conflict(f"'{disk.name}' is already {disk.size_gb} GB; a disk can only grow")
    if disk.published:
        names = [v["name"] for v in _versions(prov, disk)]
        if names and names[-1] != disk.snapshot:
            conflict(f"@{disk.snapshot} is active but @{names[-1]} is newer; "
                     f"make @{names[-1]} active first (a new version always follows the newest)")
    old = disk.size_gb
    try:
        version, result = prov.grow_disk(disk.zvol_path, body.size_gb, disk.published, body.extend)
    except ProvisioningError as e:
        host_failed(str(e))
    disk.size_gb = body.size_gb
    if version:
        disk.snapshot = version
    db.commit()

    if result is not None and result.extended:
        note = "The data partition was extended too: Windows sees the new space at the next reboot."
    elif not disk.published:
        note = "The draft is bigger; the new space shows in Disk Management while it is filled."
    else:
        why = f" ({result.note})" if result is not None else ""
        note = ("Windows shows the new space as unallocated" + why + ". To use it, on one PC with Keep "
                "Writeback: Disk Management → Extend Volume (it needs the free space right behind the "
                "volume), shut down, then Apply Writebacks.")
    if version:
        comment = f"ggNet: grown from {old} to {body.size_gb} GB" + (
            ", data partition extended" if result is not None and result.extended else "")
        try:
            prov.set_comment(disk.zvol_path, version, comment)
        except ProvisioningError as e:   # the version exists; only its comment is missing
            logger.warning("%s", e)
    logger.info("'%s' grown from %s to %s GB%s", disk.name, old, body.size_gb, f" as @{version}" if version else "")
    return GameDiskGrown(disk=GameDiskOut.model_validate(disk), version=version,
                         extended=bool(result and result.extended), note=note)


@router.get("/{disk_id}/hardware", response_model=HardwareOut)
def hardware(disk_id: int, db: Session = Depends(get_db), prov: Provisioner = Depends(get_provisioner)):
    """
    Which PCs the OS image's active version can boot: each boot network card
    the PCs reported (iPXE, and the agent or the hardware scan its instance
    ID) is "ready" (installed in the image) or "missing" (those PCs need Add
    new hardware). PCs that never booted over the network are "unknown".
    """
    disk = _get(db, disk_id)
    if disk.kind is not DiskKind.OS:
        conflict(f"'{disk.name}' is a game disk; only OS images are checked")
    if not disk.published or disk.copying:
        return HardwareOut(snapshot=None, problem="not published yet", nics=[])
    by_card: dict[tuple[str, str | None] | None, list[str]] = {}
    for m in db.scalars(select(Machine).order_by(Machine.name)):
        card = (m.boot_nic, m.nic_instance) if m.boot_nic else None
        by_card.setdefault(card, []).append(m.name)
    cards = tuple(sorted((c for c in by_card if c), key=lambda c: (c[0], c[1] or "")))
    found = nic_status(prov, disk.zvol_path, disk.snapshot, list(cards)) if cards else {}
    if isinstance(found, str):
        return HardwareOut(snapshot=disk.snapshot, problem=found, nics=[])
    out = [NicSupportOut(nic=c[0], instance=c[1], status=found.get(c, "unknown"), machines=by_card[c])
           for c in cards]
    if None in by_card:
        out.append(NicSupportOut(nic=None, status="unknown", machines=by_card[None]))
    return HardwareOut(snapshot=disk.snapshot, problem=None, nics=out)


def _job_out(job) -> AddHardwareJobOut | None:
    return None if job is None else AddHardwareJobOut.model_validate(job.out())


def _status_out(adder, master: str) -> AddHardwareStatusOut:
    wizard = adder.wizard(master)
    return AddHardwareStatusOut(
        wizard=AddHardwareWizardOut.model_validate(wizard.out()) if wizard else None,
        job=_job_out(adder.job(master)),
        waiting=adder.waiting_machines(master),
    )


@router.get("/{disk_id}/add-hardware", response_model=AddHardwareStatusOut)
def add_hardware_status(disk_id: int, db: Session = Depends(get_db), adder=Depends(get_hardware_adder)):
    """The image's Add new hardware wizard and last run (since ggNet started), and the PCs waiting for it."""
    return _status_out(adder, _get(db, disk_id).zvol_path)


@router.post("/{disk_id}/add-hardware/wizard", response_model=AddHardwareStatusOut)
def open_hardware_wizard(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    adder=Depends(get_hardware_adder),
):
    """
    Open the Add new hardware wizard (app.services.toolchain.HardwareWizard):
    the next PC that boots this image and whose network card it cannot start
    gets the card added to a new version, then boots it.
    """
    disk = _get(db, disk_id)
    _published(disk)
    _not_copying(disk)
    if disk.kind is not DiskKind.OS:
        conflict(f"'{disk.name}' is a game disk; only OS images get new hardware")
    names = [v["name"] for v in _versions(prov, disk)]
    if names and names[-1] != disk.snapshot:
        conflict(f"@{disk.snapshot} is active but @{names[-1]} is newer; "
                 f"make @{names[-1]} active first (a new version always follows the newest)")
    try:
        adder.start_wizard(disk.zvol_path, disk.id, disk.snapshot)
    except ValueError as e:
        conflict(str(e))
    return _status_out(adder, disk.zvol_path)


@router.delete("/{disk_id}/add-hardware/wizard", response_model=AddHardwareStatusOut)
def close_hardware_wizard(disk_id: int, db: Session = Depends(get_db), adder=Depends(get_hardware_adder)):
    """Finish the wizard, or cancel it (a running install is stopped)."""
    disk = _get(db, disk_id)
    adder.close_wizard(disk.zvol_path)
    return _status_out(adder, disk.zvol_path)


@router.post("/{disk_id}/add-hardware", response_model=AddHardwareJobOut, status_code=status.HTTP_202_ACCEPTED)
def add_hardware(
    disk_id: int,
    body: AddHardwareIn,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    adder=Depends(get_hardware_adder),
):
    """
    Add new hardware (app.services.toolchain): Windows installs these boot
    network cards in a clone of the image's newest version, booted in a
    temporary VM on this server; the clone becomes the next version and is
    made active. Runs in the background (a few minutes); GET shows how far.
    """
    disk = _get(db, disk_id)
    _published(disk)
    _not_copying(disk)
    if disk.kind is not DiskKind.OS:
        conflict(f"'{disk.name}' is a game disk; only OS images get new hardware")
    names = [v["name"] for v in _versions(prov, disk)]
    if names and names[-1] != disk.snapshot:
        conflict(f"@{disk.snapshot} is active but @{names[-1]} is newer; "
                 f"make @{names[-1]} active first (a new version always follows the newest)")
    done = activate_when_done(sessionmaker(bind=db.get_bind(), expire_on_commit=False), disk_id, disk.snapshot)

    try:
        job = adder.start(disk.zvol_path, [(c.nic, c.instance) for c in body.cards], done)
    except ValueError as e:
        conflict(str(e))
    logger.info("Add new hardware on '%s': %s", disk.name, ", ".join(f"{c.nic}\\{c.instance}" for c in body.cards))
    return _job_out(job)


@router.post("/{disk_id}/add-hardware/cancel", response_model=AddHardwareJobOut | None)
def cancel_add_hardware(disk_id: int, db: Session = Depends(get_db), adder=Depends(get_hardware_adder)):
    """Stop a running Add new hardware: the VM is stopped and the clone removed."""
    disk = _get(db, disk_id)
    if not adder.cancel(disk.zvol_path):
        conflict(f"Add new hardware is not running for '{disk.name}'")
    return _job_out(adder.job(disk.zvol_path))


def _version_number(snapshot: str | None) -> int:
    """base → 1, vN → N (a copy made later from @v3 has no card added at @v5); unknown counts as 1."""
    if snapshot and snapshot.startswith("v") and snapshot[1:].isdigit():
        return int(snapshot[1:])
    return 1


def _not_copying(disk: GameDisk) -> None:
    if disk.copying:
        conflict(f"'{disk.name}' is still being copied ({disk.copy_percent or 0}%); wait until it is done")


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
    _not_copying(disk)
    if disk.kind is DiskKind.OS:
        conflict(f"'{disk.name}' is an OS image; it cannot be edited on a Disk Mode PC")
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
    if disk.copy_source is not None:
        conflict(f"'{disk.name}' is a copy; it is published by itself when the copy ends")
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
    """Machines by the zvol of each of their writebacks (game, and OS in Boot Mode)."""
    out: dict[str, Machine] = {}
    for m in db.scalars(select(Machine).where(
            or_(Machine.clone_zvol.is_not(None), Machine.os_clone_zvol.is_not(None)))):
        for clone in (m.clone_zvol, m.os_clone_zvol):
            if clone:
                out[clone] = m
    return out


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
            comment=v.get("comment"),
            locked=bool(v.get("locked")),
        )
        for v in _versions(prov, disk)
    ]


@router.put("/{disk_id}/snapshots/{name}/comment", response_model=SnapshotOut)
def set_snapshot_comment(
    disk_id: int,
    name: str,
    body: SnapshotComment,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Set (or with "" remove) a version's comment: who, when, what changed."""
    disk = _get(db, disk_id)
    if not disk.published or name not in {v["name"] for v in _versions(prov, disk)}:
        not_found("Version", name)
    try:
        prov.set_comment(disk.zvol_path, name, body.comment)
    except ProvisioningError as e:
        host_failed(str(e))
    return next(v for v in list_snapshots(disk_id, db, prov) if v.name == name)


@router.put("/{disk_id}/snapshots/{name}/lock", response_model=SnapshotOut)
def set_snapshot_lock(
    disk_id: int,
    name: str,
    body: SnapshotLock,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Lock a version (never deleted, by retention or by hand) or unlock it."""
    disk = _get(db, disk_id)
    if not disk.published or name not in {v["name"] for v in _versions(prov, disk)}:
        not_found("Version", name)
    try:
        prov.set_locked(disk.zvol_path, name, body.locked)
    except ProvisioningError as e:
        host_failed(str(e))
    return next(v for v in list_snapshots(disk_id, db, prov) if v.name == name)


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
    if version.get("locked"):
        conflict(f"{disk.name}@{name} is locked; unlock it first")
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


@router.post("/{disk_id}/prepare", response_model=GameDiskOut)
def prepare_image(
    disk_id: int,
    body: PrepareImage | None = None,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    OS images: a new active version with what every PC needs made part of
    the image (the server's agent and its service, Fast Startup off, DHCP,
    stale letters forgotten; app.windows_setup.prepare_image). PCs move to it
    at their next reboot. Apply Writebacks of an OS image does this already.
    """
    disk = _get(db, disk_id)
    _published(disk)
    if disk.kind is not DiskKind.OS:
        conflict(f"'{disk.name}' is a game disk; only OS images are prepared")
    names = [v["name"] for v in _versions(prov, disk)]
    if names and names[-1] != disk.snapshot:
        conflict(f"@{disk.snapshot} is active but @{names[-1]} is newer; "
                 f"make @{names[-1]} active first (a new version always follows the newest)")
    prep = image_prep(defender_off=bool(body and body.defender_off), services_off=bool(body and body.services_off))
    try:
        new = prov.prepare_image(disk.zvol_path, prep)
    except ProvisioningError as e:
        host_failed(str(e))
    disk.snapshot = new
    db.commit()
    version = default_package().info()
    agent = f", agent {version.version}" if prep.agent and version and version.version else ""
    try:
        defender = (", Defender off" if prep.defender_off else "") + \
            (", background services off" if prep.services_off else "")
        prov.set_comment(disk.zvol_path, new, f"ggNet: prepared for all PCs{agent}{defender}")
    except ProvisioningError as e:   # the version exists; only its comment is missing
        logger.warning("%s", e)
    return disk


@router.post("/{disk_id}/snapshots/{name}/merge-into-base", response_model=GameDiskOut)
def merge_into_base(
    disk_id: int,
    name: str,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """
    Make a version the new @base: every older version is deleted and this one
    is renamed to `base` (e.g. @base, @v2, @v3, @v4 → @base with @v4's content).
    Refused while an older version is active, pinned or still used by a PC.
    """
    disk = _get(db, disk_id)
    _published(disk)
    versions = _versions(prov, disk)
    names = [v["name"] for v in versions]
    if name not in names:
        not_found("Snapshot", f"{disk.name}@{name}")
    older = versions[: names.index(name)]
    if not older and name == "base":
        conflict(f"{disk.name}@base is already the only base version")
    older_names = {v["name"] for v in older}
    if disk.snapshot in older_names:
        conflict(f"{disk.name}@{disk.snapshot} is the active version and would be deleted; "
                 f"make @{name} or a newer version active first")
    pinned = sorted(f"{m.name} (@{m.pinned_snapshot})" for m in disk.machines
                    if m.pinned_snapshot in older_names)
    if pinned:
        conflict(f"Older versions are pinned on {', '.join(pinned)}; unpin them first")
    locked = [v["name"] for v in older if v.get("locked")]
    if locked:
        conflict(f"Older versions are locked ({', '.join('@' + n for n in locked)}); unlock them first")
    if any(v["clones"] for v in older):
        by_clone = _machines_by_clone(db)
        users = sorted({by_clone[c].name if c in by_clone else c
                        for v in older for c in v["clones"]})
        conflict(f"Older versions are still used by {', '.join(users)}; "
                 "they move off them at their next reboot")
    try:
        prov.merge_into_base(disk.zvol_path, name)
    except ProvisioningError as e:
        host_failed(str(e))

    if name != "base":
        old_path, new_path = f"{disk.zvol_path}@{name}", f"{disk.zvol_path}@base"
        if disk.snapshot == name:
            disk.snapshot = "base"
        for m in db.scalars(select(Machine).where(or_(
                Machine.clone_snapshot == old_path, Machine.os_clone_snapshot == old_path))):
            if m.clone_snapshot == old_path:
                m.clone_snapshot = new_path
            if m.os_clone_snapshot == old_path:
                m.os_clone_snapshot = new_path
        for m in disk.machines:
            if m.pinned_snapshot == name:
                m.pinned_snapshot = "base"
    db.commit()
    logger.info("%s: @%s merged into @base", disk.name, name)
    return disk


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
    _not_copying(disk)
    assigned = db.scalar(
        select(func.count()).select_from(Machine).where(
            or_(Machine.game_disk_id == disk.id, Machine.os_disk_id == disk.id))
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
