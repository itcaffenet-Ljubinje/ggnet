"""
API for ggnet-agent, the Windows service on each client.

The agent posts a heartbeat at boot and periodically; the answer tells it
which iSCSI target to connect and on which portal. Machines must be
registered first (UI / /machines); unknown names get 404.

No authentication yet (planned with JWT); the heartbeat only records what
the agent reports and never changes anything on the host.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner
from app.api.v1.errors import not_found
from app.api.v1.schemas import AgentConfig, AgentHeartbeat
from app.db.models import Machine, MachineStatus
from app.db.session import get_db
from app.services.provisioning import Provisioner

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/heartbeat", response_model=AgentConfig)
def heartbeat(
    body: AgentHeartbeat,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    machine = db.scalar(select(Machine).where(Machine.name == body.name))
    if machine is None:
        not_found("Machine", body.name)

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
            updated_at=Machine.updated_at,
        )
    )
    db.commit()
    db.refresh(machine)

    provisioned = machine.status is MachineStatus.PROVISIONED
    return AgentConfig(
        machine_id=machine.id,
        name=machine.name,
        mode=machine.mode,
        status=machine.status,
        initiator_iqn=machine.initiator_iqn,
        iscsi_target_iqn=machine.iscsi_target_iqn if provisioned else None,
        portal_ip=prov.iscsi.portal_ip,
        portal_port=prov.iscsi.portal_port,
        game_disk=machine.game_disk.name if machine.game_disk else None,
    )
