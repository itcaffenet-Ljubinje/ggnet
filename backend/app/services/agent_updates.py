"""
Automatic ggnet-agent updates (Settings -> Client app).

The server offers its agent package (app.services.agent_package) in the
heartbeat answer to every Disk Mode PC whose agent is older; the agent
downloads the files from this server, checks each one's SHA-256 and runs the
package's install.ps1, which keeps the PC's settings and restarts the service.

Boot Mode PCs are never offered one: their agent lives in the OS image, so an
update would only reach that PC's writeback and be gone at the next boot. They
get a new agent from the image (Images -> Prepare image puts the server's
agent into it).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.db.models import AppSetting, Machine, MachineMode
from app.services.agent_package import Package
from app.services.updates import parse

SETTINGS_KEY = "agent"


@dataclass(frozen=True)
class AgentSettings:
    auto_update: bool = True


def load_settings(db: Session) -> AgentSettings:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        return AgentSettings()
    known = AgentSettings.__dataclass_fields__
    try:
        saved = {k: v for k, v in json.loads(row.value).items() if k in known}
    except (ValueError, AttributeError):
        saved = {}
    return AgentSettings(**saved)


def save_settings(db: Session, s: AgentSettings) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(asdict(s))))
    else:
        row.value = json.dumps(asdict(s))
    db.commit()


def is_older(agent_version: str | None, package_version: str | None) -> bool:
    """Whether the package is newer than the agent; never for a version either side cannot read."""
    current, offered = parse(agent_version or ""), parse(package_version or "")
    return current is not None and offered is not None and offered > current


def offer(machine: Machine, agent_version: str | None, package: Package | None,
          settings: AgentSettings, stopping: bool = False) -> Package | None:
    """The package this machine's agent should update to, or None."""
    if (not settings.auto_update or stopping or package is None
            or machine.mode is not MachineMode.DISK):
        return None
    return package if is_older(agent_version, package.version) else None
