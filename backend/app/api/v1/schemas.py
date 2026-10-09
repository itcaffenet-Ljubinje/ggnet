"""Pydantic schemas for API v1."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Annotated, Any

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

# Game disk drive letter on the client; A-C are floppy and system drives.
_DRIVE_LETTER_RE = re.compile(r"^[D-Z]$")
# Boot Mode: Windows itself is C:, the game disk is always D:.
BOOT_MODE_LETTER = "Boot Mode machines always get the game disk as D:"


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


def _drive_letter(v: str) -> str:
    v = v.strip().rstrip(":").upper()
    if not _DRIVE_LETTER_RE.match(v):
        raise ValueError("drive letter: one letter D-Z")
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
    editor_id: int | None
    created_at: datetime


class GameDiskEdit(BaseModel):
    machine_id: int   # the PC the draft is filled on


class SnapshotOut(BaseModel):
    """One version of a game disk (a ZFS snapshot of its master)."""

    name: str                 # base, v2, ...
    created_at: datetime
    used_bytes: int           # space only this version holds
    referenced_bytes: int     # data the version contains
    active: bool              # PCs are cloned from it at their next discard
    machines: list[str]       # PCs whose writeback was cloned from it
    pinned: list[str]         # PCs pinned to it (they move to it at their next reboot)


class ActiveSnapshot(BaseModel):
    snapshot: str


class WritebackOut(BaseModel):
    """A PC's writeback (clone) of a game disk."""

    machine_id: int | None    # None: a clone on the host with no machine record
    machine_name: str
    zvol: str
    snapshot: str             # the version it was cloned from
    used_bytes: int           # what the PC has written
    keep_writeback: bool
    pinned_snapshot: str | None
    session_active: bool | None
    outdated: bool            # not on the version it should run (its pin, else the active one)


# ── Machine ───────────────────────────────────────────────────────────

class MachineCreate(BaseModel):
    name: str
    mode: MachineMode = MachineMode.DISK
    initiator_iqn: str | None = None   # empty → iqn.1991-05.com.microsoft:<name>
    mac: str | None = None
    game_disk_id: int | None = None    # set → provision right away
    drive_letter: str = "D"            # game disk letter on the PC (Disk Mode)

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _machine_name(v)

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str) -> str:
        return _drive_letter(v)

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
        if self.mode is MachineMode.BOOT and self.drive_letter != "D":
            raise ValueError(BOOT_MODE_LETTER)
        return self


class MachineUpdate(BaseModel):
    """
    Partial update: only the fields sent are changed (`"mac": null` clears
    the MAC). Name, IQN and mode only while the machine has no disk; the
    drive letter at any time (the agent moves the disk on its next heartbeat).
    """

    name: str | None = None
    mode: MachineMode | None = None
    initiator_iqn: str | None = None
    mac: str | None = None
    drive_letter: str | None = None

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("drive letter cannot be empty")
        return _drive_letter(v)

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


class MachinePin(BaseModel):
    snapshot: str | None   # a version of the machine's game disk; None follows the active one


class DriveLetterAll(BaseModel):
    """Set the game disk letter of every Disk Mode machine at once."""

    drive_letter: str

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str) -> str:
        return _drive_letter(v)


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
    editing_disk_id: int | None
    drive_letter: str
    reported_drive_letter: str | None
    pinned_snapshot: str | None
    reported_ip: str | None
    reported_mac: str | None
    link_speed_mbps: int | None
    hardware: MachineHardware | None = Field(validation_alias="hardware_info")
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
    drive_letter: str | None = None     # the letter the game disk actually got
    # Network and hardware (AgentInventory); parsed leniently by the endpoint so
    # a bad inventory never fails the heartbeat itself.
    inventory: dict[str, Any] | None = None

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str | None) -> str | None:
        return _drive_letter(v) if v else None

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


class AgentInventory(BaseModel):
    """What the agent reports about the PC (refreshed every few minutes)."""

    ip_address: str | None = None
    mac_address: str | None = None
    link_speed_mbps: int | None = Field(None, ge=0, le=1_000_000)
    nic: str | None = Field(None, max_length=128)
    cpu: str | None = Field(None, max_length=128)
    gpus: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=8)
    motherboard: str | None = Field(None, max_length=128)
    memory_bytes: int | None = Field(None, ge=0)

    @field_validator("ip_address")
    @classmethod
    def check_ip(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_address(v)) if v else None

    @field_validator("mac_address")
    @classmethod
    def check_mac(cls, v: str | None) -> str | None:
        return _mac(v)


class MachineHardware(BaseModel):
    nic: str | None = None
    cpu: str | None = None
    gpus: list[str] = []
    motherboard: str | None = None
    memory_bytes: int | None = None


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
    drive_letter: str                   # the letter the game disk should get


# ── Settings: retention and storage ───────────────────────────────────

class RetentionIn(BaseModel):
    """Automated snapshot and writeback removal (ggRock Settings → Array and Images)."""

    enabled: bool = False
    dry_run: bool = True
    reserved_percent: int = Field(15, ge=0, le=50)
    warning_percent: int = Field(80, ge=50, le=99)
    unused_snapshot_days: int = Field(14, ge=1, le=3650)
    keep_newest_snapshots: int = Field(3, ge=1, le=100)
    inactive_writeback_hours: int = Field(24, ge=1, le=24 * 365)


class RetentionActionOut(BaseModel):
    kind: str
    target: str
    reason: str
    done: bool
    error: str | None


class RetentionReportOut(BaseModel):
    at: datetime
    dry_run: bool
    actions: list[RetentionActionOut]


class RetentionOut(RetentionIn):
    saved: bool                              # False: defaults, never applied to the host
    last_run: RetentionReportOut | None


class StorageOut(BaseModel):
    pool: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float
    reserved_bytes: int
    warning_percent: int
    warning: bool                            # used_percent >= warning_percent


class MachineTrafficOut(BaseModel):
    """iSCSI traffic of a mapped machine since its writeback was last cloned."""

    machine_id: int
    sent_bytes: int                 # read by the PC from its game disk
    received_bytes: int             # written by the PC (its writeback)
    sent_bps: float | None          # bytes/s since the previous read; None on the first
    received_bps: float | None
