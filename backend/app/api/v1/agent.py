"""
API for ggnet-agent, the Windows service on each client.

The agent posts a heartbeat at boot and periodically; the answer tells it
which iSCSI target to connect and on which portal. Machines must be
registered first (UI / /machines); unknown names get 404.

No authentication yet (planned with JWT). The heartbeat records what the
agent reports; the only host change it can cause is the automatic writeback
discard after a reboot (app.services.writebacks), before the agent logs in.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner, get_writeback_settings
from app.api.v1.errors import not_found
from app.api.v1.schemas import AgentConfig, AgentHeartbeat, AgentInventory
from app.db.models import Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services.provisioning import Provisioner
from app.services.writebacks import WritebackSettings, on_heartbeat

logger = logging.getLogger("ggnet.api.agent")

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/heartbeat", response_model=AgentConfig)
def heartbeat(
    body: AgentHeartbeat,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    settings: WritebackSettings = Depends(get_writeback_settings),
):
    machine = db.scalar(select(Machine).where(Machine.name == body.name))
    if machine is None:
        not_found("Machine", body.name)

    inventory: dict = {}
    if body.inventory is not None:
        try:
            inv = AgentInventory.model_validate(body.inventory)
        except ValidationError as e:
            logger.warning("Ignoring the inventory of %s: %s", body.name, e.errors()[0]["msg"])
        else:
            hw = inv.model_dump(include={"nic", "cpu", "gpus", "motherboard", "memory_bytes"})
            inventory = {
                "reported_ip": inv.ip_address,
                "reported_mac": inv.mac_address,
                "link_speed_mbps": inv.link_speed_mbps,
                "hardware": json.dumps(hw),
            }

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
            reported_drive_letter=body.drive_letter if body.iscsi_connected else None,
            **inventory,   # an old agent sends none: the last inventory stays
            updated_at=Machine.updated_at,
        )
    )
    db.commit()
    db.refresh(machine)
    on_heartbeat(db, prov, machine, body.booted_at, body.iscsi_connected, settings)

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
    )
