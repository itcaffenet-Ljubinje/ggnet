"""
API for ggnet-agent, the Windows service on each client.

The agent posts a heartbeat at boot and periodically; the answer tells it
which iSCSI target to connect and on which portal. Machines must be
registered first (UI / /machines). The PC is found by the MAC in its
inventory (the machine's MAC set in the UI), else by its Windows computer
name. An unknown PC gets 404 and shows up in Discovery for the admin to add.

No authentication yet (planned with JWT). The heartbeat records what the
agent reports; the only host change it can cause is the automatic writeback
discard after a reboot (app.services.writebacks), before the agent logs in.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import (
    get_agent_package,
    get_provisioner,
    get_pxe_service,
    get_server_url,
    get_writeback_settings,
)
from app.api.v1.errors import not_found
from app.api.v1.schemas import (
    AgentConfig,
    AgentHeartbeat,
    AgentInventory,
    AgentPackageFileOut,
    AgentPackageOut,
    AgentSecureBoot,
)
from app.db.models import DiscoverySource, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services import discovery, pxe
from app.services.agent_package import AgentPackage, bootstrap_script
from app.services.power import take_command
from app.services.pxe import PxeService
from app.services.provisioning import Provisioner
from app.services.writebacks import WritebackSettings, on_heartbeat
from app.windows_setup import NIC_ID_RE, NIC_INSTANCE_RE

def split_pnp_id(pnp_id: str | None) -> tuple[str, str] | None:
    """PCI\\<hardware ID>\\<instance ID> as (hardware ID, instance ID); None for anything else."""
    if not pnp_id:
        return None
    bus, _, rest = pnp_id.partition("\\")
    hwid, _, instance = rest.partition("\\")
    hwid = hwid.upper()
    if bus.upper() != "PCI" or not NIC_ID_RE.match(hwid) or not NIC_INSTANCE_RE.match(instance):
        return None
    return hwid, instance


logger = logging.getLogger("ggnet.api.agent")

router = APIRouter(prefix="/agent", tags=["agent"])


def identify(db: Session, name: str, mac: str | None) -> Machine | None:
    """
    The machine a heartbeat comes from: the one with the reported MAC, else
    the one with the PC's name. A name match is refused when that machine has
    a MAC and the PC reports another one: it is a different PC with the same
    Windows name (a cloned Windows), which must not get that machine's disk.
    """
    if mac:
        machine = db.scalar(select(Machine).where(Machine.mac == mac))
        if machine is not None:
            return machine
    machine = db.scalar(select(Machine).where(Machine.name == name))
    if machine is not None and machine.mac and mac and machine.mac != mac:
        logger.warning("Heartbeat from %s with MAC %s, but machine %s has MAC %s; ignoring it",
                       name, mac, machine.name, machine.mac)
        return None
    return machine


@router.post("/heartbeat", response_model=AgentConfig)
def heartbeat(
    body: AgentHeartbeat,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    settings: WritebackSettings = Depends(get_writeback_settings),
    pxe_service: PxeService = Depends(get_pxe_service),
):
    inv = None
    if body.inventory is not None:
        try:
            inv = AgentInventory.model_validate(body.inventory)
        except ValidationError as e:
            logger.warning("Ignoring the inventory of %s: %s", body.name, e.errors()[0]["msg"])

    mac = inv.mac_address if inv else None
    machine = identify(db, body.name, mac)
    if machine is None:
        if mac:
            discovery.record(db, mac, DiscoverySource.AGENT, hostname=body.name,
                             ip_address=inv.ip_address, agent_version=body.agent_version)
        not_found("Machine", body.name)

    inventory: dict = {}
    if inv is not None:
        hw = inv.model_dump(include={"nic", "cpu", "gpus", "motherboard", "memory_bytes", "secure_boot"})
        inventory = {
            "reported_ip": inv.ip_address,
            "reported_mac": inv.mac_address,
            "link_speed_mbps": inv.link_speed_mbps,
            "hardware": json.dumps(hw),
        }
        card = split_pnp_id(inv.nic_pnp_id)
        # The card's instance on this board, for Add new hardware: only for the
        # card the PC boots from (iPXE's), or any card while iPXE has not
        # reported one (a Disk Mode PC), which then becomes the boot card.
        if card is not None and machine.boot_nic in (None, card[0]):
            inventory["nic_instance"] = card[1]
            if machine.boot_nic is None:
                inventory["boot_nic"] = card[0]

    # A Core UPDATE that keeps updated_at: a heartbeat every few seconds is
    # not a change to the machine record.
    db.execute(
        update(Machine)
        .where(Machine.id == machine.id)
        .values(
            last_seen_at=datetime.now(timezone.utc),
            agent_version=body.agent_version,
            reported_iqn=body.initiator_iqn,
            iscsi_connected=body.iscsi_connected,
            agent_stopped=body.stopping,
            reported_drive_letter=body.drive_letter if body.iscsi_connected else None,
            **inventory,   # an old agent sends none: the last inventory stays
            **({"mok_state": body.mok} if body.mok else {}),
            updated_at=Machine.updated_at,
        )
    )
    db.commit()
    db.refresh(machine)
    on_heartbeat(db, prov, machine, body.booted_at, body.iscsi_connected, settings)
    # A PC that is shutting down gets no command; it waits for the next start
    # (and expires there if it is too old).
    command = None if body.stopping else take_command(machine)
    db.commit()

    # While editing, the draft master is the machine's game disk.
    mapped = machine.status in (MachineStatus.PROVISIONED, MachineStatus.EDITING)
    disk = machine.game_disk or machine.editing_disk
    return AgentConfig(
        machine_id=machine.id,
        name=machine.name,
        mode=machine.mode,
        status=machine.status,
        initiator_iqn=machine.initiator_iqn,
        iscsi_target_iqn=machine.iscsi_target_iqn if mapped else None,
        portal_ip=prov.iscsi.portal_ip,
        portal_port=prov.iscsi.portal_port,
        game_disk=disk.name if disk else None,
        drive_letter=machine.drive_letter if machine.mode is MachineMode.DISK else "D",
        command=command,
        secure_boot=_secure_boot_key(db, pxe_service) if machine.mode is MachineMode.BOOT else None,
    )


def _secure_boot_key(db: Session, service: PxeService) -> AgentSecureBoot | None:
    """The key to enroll, while Settings -> Network boot asks for it and the key exists."""
    s, _ = pxe.load_settings(db)
    cert = service.certificate
    if not s.mok_enroll or cert is None:
        return None
    return AgentSecureBoot(certificate=base64.b64encode(cert.read_bytes()).decode(), password=s.mok_password)


# ── Client app download (Settings → Client app) ──────────────────────

@router.get("/package", response_model=AgentPackageOut)
def package_info(
    package: AgentPackage = Depends(get_agent_package),
    server_url: str = Depends(get_server_url),
):
    info = package.info()
    return AgentPackageOut(
        available=info is not None,
        directory=str(package.directory),
        version=info.version if info else None,
        server_url=server_url,
        install_command=f"irm {server_url}/api/v1/agent/bootstrap.ps1 | iex",
        files=[AgentPackageFileOut(name=f.name, size_bytes=f.size, sha256=f.sha256)
               for f in (info.files if info else ())],
    )


@router.get("/download.zip")
def download_zip(package: AgentPackage = Depends(get_agent_package)):
    info, path = package.info(), package.zip()
    if info is None or path is None:
        not_found("Agent package in", str(package.directory))
    return FileResponse(path, media_type="application/zip",
                        filename=f"ggnet-agent-{info.version or 'latest'}.zip")


@router.get("/download/{name}")
def download_file(name: str, package: AgentPackage = Depends(get_agent_package)):
    path = package.path(name)
    if path is None:
        not_found("Agent file", name)
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.get("/bootstrap.ps1", response_class=PlainTextResponse)
def bootstrap(
    package: AgentPackage = Depends(get_agent_package),
    server_url: str = Depends(get_server_url),
):
    """One-line install on a PC: `irm <server>/api/v1/agent/bootstrap.ps1 | iex` (admin PowerShell)."""
    info = package.info()
    if info is None:
        not_found("Agent package in", str(package.directory))
    return PlainTextResponse(bootstrap_script(server_url, info))
