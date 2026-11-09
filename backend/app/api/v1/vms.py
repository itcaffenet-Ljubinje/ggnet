"""
Virtual machines for working on images (app.services.vms).

A VM is a machine (it is listed, kept, discarded and applied like a PC)
whose OS and game writebacks are its local drives on this Proxmox host.
These routes make and remove the VM, change its cores and memory, and run
it; everything about its writebacks is the machine's routes
(/machines/{id}/...).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.machines import DUPLICATE, _published_disk, _provision, delete_machine
from app.api.v1.schemas import MachineOut, VmAction, VmCreate, VmOut, VmSettingsIn, VmUpdate
from app.config import get_config
from app.db.models import ImageKind, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services import vms
from app.services.provisioning import Provisioner
from app.services.vms import ProxmoxHost, VmSettings

logger = logging.getLogger("ggnet.api.vms")

router = APIRouter(prefix="/vms", tags=["vms"])


def get_vm_host(prov: Provisioner = Depends(get_provisioner)) -> ProxmoxHost:
    return prov.vms.host


def _pool(prov: Provisioner) -> str:
    return prov.zfs.layout.root_dataset.split("/")[0]


def _interface() -> str | None:
    return (get_config().get("network") or {}).get("interface")


def _choices(db: Session, host: ProxmoxHost, prov: Provisioner) -> dict:
    """The host's bridges and storages, the saved settings and what they come to."""
    saved = vms.load_settings(db)
    bridges, storages = host.bridges(), host.storages()
    names = {b["name"] for b in bridges}
    stores = {s["name"] for s in storages}
    return {
        "bridges": bridges,
        "storages": storages,
        "settings": saved,
        "bridge": saved.bridge if saved.bridge in names else vms.default_bridge(bridges, _interface()),
        "storage": saved.storage if saved.storage in stores else vms.default_storage(storages, _pool(prov)),
    }


def _get_vm(db: Session, machine_id: int) -> Machine:
    m = db.get(Machine, machine_id)
    if m is None:
        not_found("Machine", machine_id)
    if m.vm_id is None:
        conflict(f"'{m.name}' is a PC, not a virtual machine")
    return m


def _check_resources(host: ProxmoxHost, db: Session, cores: int | None, memory_mb: int | None) -> None:
    res = host.resources()
    if cores is not None and res["cpus"] and cores > res["cpus"]:
        conflict(f"This host has {res['cpus']} CPU threads; a VM can have at most that many cores")
    if memory_mb is not None and res["memory_mb"]:
        reserved = vms.load_settings(db).reserved_memory_gb * 1024
        most = res["memory_mb"] - reserved
        if memory_mb > most:
            conflict(f"At most {max(most, 0)} MB: the host has {res['memory_mb']} MB and "
                     f"{reserved} MB are kept for the host (Settings -> Virtual machines)")


# ── Host and settings ─────────────────────────────────────────────────

@router.get("/host")
def vm_host(db: Session = Depends(get_db), host: ProxmoxHost = Depends(get_vm_host),
            prov: Provisioner = Depends(get_provisioner)):
    """Whether VMs can be made here, the host's size, bridges and storages, and the settings."""
    problem = host.available()
    if problem:
        return {"available": False, "problem": problem}
    c = _choices(db, host, prov)
    res = host.resources()
    s: VmSettings = c["settings"]
    return {
        "available": True, "problem": None, "node": host.node(),
        "cpus": res["cpus"], "memory_mb": res["memory_mb"],
        "bridges": c["bridges"], "storages": c["storages"],
        "bridge": c["bridge"], "storage": c["storage"],
        "settings": {"bridge": s.bridge, "storage": s.storage, "reserved_memory_gb": s.reserved_memory_gb},
        "service_interface": _interface(),
    }


@router.put("/settings")
def save_vm_settings(body: VmSettingsIn, db: Session = Depends(get_db),
                     host: ProxmoxHost = Depends(get_vm_host)):
    """Bridge and storage for new VMs (null: chosen automatically) and the memory kept for the host."""
    if body.bridge is not None and body.bridge not in {b["name"] for b in host.bridges()}:
        conflict(f"This host has no bridge '{body.bridge}'")
    if body.storage is not None and body.storage not in {s["name"] for s in host.storages()}:
        conflict(f"This host has no storage '{body.storage}' for VM disks")
    vms.save_settings(db, VmSettings(bridge=body.bridge, storage=body.storage,
                                     reserved_memory_gb=body.reserved_memory_gb))
    return {"ok": True}


# ── VMs ───────────────────────────────────────────────────────────────

@router.get("", response_model=list[VmOut])
def list_vms(db: Session = Depends(get_db), host: ProxmoxHost = Depends(get_vm_host)):
    """Every VM machine with its live state; one host call for all of them."""
    machines = db.scalars(select(Machine).where(Machine.vm_id.is_not(None)).order_by(Machine.name)).all()
    if not machines:
        return []
    live = host.statuses()
    out = []
    for m in machines:
        vm = live.get(m.vm_id) or {}
        out.append(VmOut(machine_id=m.id, vm_id=m.vm_id, status=vm.get("status"),
                         cores=vm.get("cpus"), memory_mb=vm.get("memory_mb"),
                         node=vm.get("node") or host.node()))
    return out


@router.post("", response_model=MachineOut, status_code=status.HTTP_201_CREATED)
def create_vm(body: VmCreate, db: Session = Depends(get_db),
              prov: Provisioner = Depends(get_provisioner), host: ProxmoxHost = Depends(get_vm_host)):
    """
    Make a VM on this host and a machine for it, then give it its OS and
    game writebacks as drives. It is not started.
    """
    problem = host.available()
    if problem:
        conflict(problem)
    os_image = _published_disk(db, body.os_image_id, ImageKind.OS)
    disk = _published_disk(db, body.game_image_id)
    if db.scalar(select(Machine).where(Machine.name == body.name)):
        conflict(DUPLICATE)
    if body.keep_writeback:
        other = db.scalar(select(Machine).where(Machine.keep_writeback.is_(True)))
        if other is not None:
            conflict(f"'{other.name}' already keeps its writeback; turn it off there first, "
                     "or make the VM without keeping its writeback")
    _check_resources(host, db, body.cores, body.memory_mb)
    c = _choices(db, host, prov)
    if c["bridge"] is None:
        conflict("This host has no network bridge for the VM")
    if c["storage"] is None:
        conflict("This host has no storage for VM disks (the VM's EFI and TPM state)")

    vmid = host.next_id()
    if vmid is None:
        host_failed(f"No free VM ID: {host.runner.last_error}")
    if not host.create(vmid, body.name, body.cores, body.memory_mb, c["bridge"], c["storage"]):
        host_failed(f"Creating VM {vmid} failed: {host.runner.last_error}")
    logger.info("VM %s created for '%s' on %s (%s)", vmid, body.name, c["bridge"], c["storage"])

    machine = Machine(name=body.name, mode=MachineMode.BOOT, vm_id=vmid, mac=host.mac(vmid),
                      initiator_iqn=vms.vm_iqn(prov.iscsi.cfg.iqn_prefix, vmid),
                      keep_writeback=body.keep_writeback)
    db.add(machine)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        host.destroy(vmid)
        conflict(DUPLICATE)
    _provision(db, prov, machine, disk, os_image)
    db.commit()
    return machine


@router.patch("/{machine_id}", response_model=MachineOut)
def update_vm(machine_id: int, body: VmUpdate, db: Session = Depends(get_db),
              host: ProxmoxHost = Depends(get_vm_host)):
    """Cores and memory; the VM must be off."""
    m = _get_vm(db, machine_id)
    state = host.status(m.vm_id)
    if state == vms.MISSING:
        conflict(f"VM {m.vm_id} of '{m.name}' no longer exists on this host; delete the machine")
    if state != "stopped":
        conflict(f"'{m.name}' is not off; shut it down first")
    _check_resources(host, db, body.cores, body.memory_mb)
    if not host.set_hardware(m.vm_id, cores=body.cores, memory_mb=body.memory_mb):
        host_failed(f"Changing VM {m.vm_id} failed: {host.runner.last_error}", machine_id=m.id)
    return m


@router.post("/{machine_id}/action", response_model=MachineOut)
def vm_action(machine_id: int, body: VmAction, db: Session = Depends(get_db),
              host: ProxmoxHost = Depends(get_vm_host)):
    """Start, Shutdown (Windows shuts down), Stop (power off), Reset, Reboot; not waited for."""
    m = _get_vm(db, machine_id)
    state = host.status(m.vm_id)
    if state is None:
        host_failed(f"VM {m.vm_id} of '{m.name}' cannot be read on this host", machine_id=m.id)
    if state == vms.MISSING:
        conflict(f"VM {m.vm_id} of '{m.name}' no longer exists on this host; delete the machine")
    if body.action == "start":
        if state != "stopped":
            conflict(f"'{m.name}' is already running")
        if m.status is not MachineStatus.PROVISIONED:
            conflict(f"'{m.name}' has no writebacks to start from ({m.status.value})")
    elif state == "stopped":
        conflict(f"'{m.name}' is off")
    if not host.action(m.vm_id, body.action):
        host_failed(f"{body.action.capitalize()} of VM {m.vm_id} failed: {host.runner.last_error}",
                    machine_id=m.id)
    logger.info("VM %s of '%s': %s", m.vm_id, m.name, body.action)
    return m


@router.delete("/{machine_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vm(machine_id: int, db: Session = Depends(get_db),
              prov: Provisioner = Depends(get_provisioner)):
    """The machine with its writebacks and the VM; it must be off (same as DELETE /machines/{id})."""
    _get_vm(db, machine_id)
    return delete_machine(machine_id, db, prov)
