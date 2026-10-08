"""Pydantic schemas for API v1."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import MachineMode, MachineStatus

# The disk name becomes part of a ZFS path, so only [a-z0-9-].
_DISK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Windows computer name: at most 15 characters (NetBIOS), not only digits.
# The name also goes into the clone path and the target IQN, so only [a-z0-9-].
_MACHINE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,14}$")
_IQN_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.:-]*)?$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$")

WINDOWS_IQN_PREFIX = "iqn.1991-05.com.microsoft:"


def _machine_name(v: str) -> str:
    v = v.strip().lower()
    if not _MACHINE_NAME_RE.match(v) or v.isdigit():
        raise ValueError("name: 1-15 characters [a-z0-9-], not only digits")
    return v


def _iqn(v: str) -> str:
    v = v.strip().lower()
    if not _IQN_RE.match(v):
        raise ValueError("invalid IQN (expected e.g. iqn.1991-05.com.microsoft:pc01)")
    return v


def _mac(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    v = v.strip().lower()
    if not _MAC_RE.match(v):
        raise ValueError("invalid MAC address")
    digits = re.sub(r"[^0-9a-f]", "", v)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


# ── Game disk ─────────────────────────────────────────────────────────

class GameDiskCreate(BaseModel):
    name: str
    size_gb: int = Field(gt=0, le=16384)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not _DISK_NAME_RE.match(v):
            raise ValueError("name: 1-32 characters [a-z0-9-]")
        return v


class GameDiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    zvol_path: str
    size_gb: int
    snapshot: str | None
    published: bool
    created_at: datetime


# ── Machine ───────────────────────────────────────────────────────────

class MachineCreate(BaseModel):
    name: str
    mode: MachineMode = MachineMode.DISK
    initiator_iqn: str | None = None   # empty → iqn.1991-05.com.microsoft:<name>
    mac: str | None = None
    game_disk_id: int | None = None    # set → provision right away

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _machine_name(v)

    @field_validator("initiator_iqn")
    @classmethod
    def check_iqn(cls, v: str | None) -> str | None:
        return _iqn(v) if v else None

    @field_validator("mac")
    @classmethod
    def check_mac(cls, v: str | None) -> str | None:
        return _mac(v)

    @model_validator(mode="after")
    def default_iqn(self):
        if self.initiator_iqn is None:
            self.initiator_iqn = WINDOWS_IQN_PREFIX + self.name
        return self


class MachineUpdate(BaseModel):
    """
    Partial update: only the fields sent are changed (`"mac": null` clears
    the MAC). Name, IQN and mode only while the machine has no disk.
    """

    name: str | None = None
    mode: MachineMode | None = None
    initiator_iqn: str | None = None
    mac: str | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("name cannot be empty")
        return _machine_name(v)

    @field_validator("mode")
    @classmethod
    def check_mode(cls, v: MachineMode | None) -> MachineMode | None:
        if v is None:
            raise ValueError("mode cannot be empty")
        return v

    @field_validator("initiator_iqn")
    @classmethod
    def check_iqn(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("IQN cannot be empty")
        return _iqn(v)

    @field_validator("mac")
    @classmethod
    def check_mac(cls, v: str | None) -> str | None:
        return _mac(v)


class KeepWriteback(BaseModel):
    enabled: bool


class MachineAssign(BaseModel):
    game_disk_id: int | None   # None → remove the disk (deprovision)


class MachineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    mode: MachineMode
    initiator_iqn: str
    mac: str | None
    game_disk_id: int | None
    status: MachineStatus
    last_error: str | None
    clone_zvol: str | None
    clone_snapshot: str | None
    iscsi_target_iqn: str | None
    outdated: bool
    last_seen_at: datetime | None
    agent_version: str | None
    reported_iqn: str | None
    iscsi_connected: bool | None
    keep_writeback: bool
    writeback_dirty: bool
    session_active: bool | None
    session_changed_at: datetime | None
    booted_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── Agent ─────────────────────────────────────────────────────────────

class AgentHeartbeat(BaseModel):
    """Sent by ggnet-agent at boot and periodically."""

    name: str                           # Windows computer name
    agent_version: str = Field(max_length=32)
    initiator_iqn: str | None = None    # the client's actual initiator IQN
    iscsi_connected: bool = False
    booted_at: datetime | None = None   # Windows boot time (UTC); newer = the PC restarted

    @field_validator("booted_at")
    @classmethod
    def utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _machine_name(v)

    @field_validator("initiator_iqn")
    @classmethod
    def check_iqn(cls, v: str | None) -> str | None:
        # Stored only for display, so any IQN-looking value is accepted as is.
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if len(v) > 223 or not v.startswith("iqn."):
            raise ValueError("invalid IQN")
        return v


class AgentConfig(BaseModel):
    """What the agent should connect. `iscsi_target_iqn` is null until provisioned."""

    machine_id: int
    name: str
    mode: MachineMode
    status: MachineStatus
    initiator_iqn: str                  # the IQN the server's ACL expects
    iscsi_target_iqn: str | None
    portal_ip: str
    portal_port: int
    game_disk: str | None
