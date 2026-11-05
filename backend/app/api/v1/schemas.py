"""Pydantic schemas for API v1."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import DiscoverySource, DiskKind, MachineMode, MachineStatus, UserRole
from app.services.auth import PASSWORD_MIN, USERNAME_RE
from app.zfs_manager import COMMENT_MAX, valid_comment

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
    # <writebacks>/<name>-os is the OS writeback of machine <name> (Boot Mode).
    if v.endswith("-os"):
        raise ValueError("name must not end in '-os'")
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

def _disk_name(v: str) -> str:
    v = v.strip().lower()
    if not _DISK_NAME_RE.match(v):
        raise ValueError("name: 1-32 characters [a-z0-9-]")
    return v


class GameDiskCreate(BaseModel):
    name: str
    size_gb: int = Field(gt=0, le=16384)
    kind: DiskKind = DiskKind.GAME     # `os`: a Boot Mode Windows image

    check_name = field_validator("name")(_disk_name)


class ImageFileOut(BaseModel):
    path: str
    name: str
    size: int
    format: str           # qemu-img's name: vpc (VHD), vhdx, qcow2, vmdk, raw


class UsbPartOut(BaseModel):
    name: str             # the partition, e.g. sdj1
    path: str
    size: int
    fstype: str
    label: str | None
    model: str | None
    mounted: str | None   # where ggNet opened it (read-only), or None


class ImportSources(BaseModel):
    folder: str | None    # the imports folder, None when its dataset is missing
    files: list[ImageFileOut]
    usb: list[UsbPartOut]


class UsbFolderOut(BaseModel):
    part: str
    folder: str
    files: list[ImageFileOut]


class ImportIn(BaseModel):
    """A new disk from an image file in the imports folder or on an opened USB disk."""

    name: str
    kind: DiskKind = DiskKind.OS
    path: str
    prepare: bool = True          # an OS image: Prepare image right after the import
    defender_off: bool = False    # ... with Microsoft Defender turned off
    services_off: bool = False    # ... with background services a cafe PC does not need turned off

    check_name = field_validator("name")(_disk_name)


class NicSupportOut(BaseModel):
    """Whether an OS image's active version can boot the PCs with one boot network card."""

    nic: str | None              # PCI hardware ID (VEN_..&DEV_..&SUBSYS_..&REV_..); None: never booted over the network
    instance: str | None = None  # the card's instance ID on these PCs, when their agent reported it
    status: str                  # ready | added | missing | unknown
    machines: list[str]


class HardwareOut(BaseModel):
    snapshot: str | None
    problem: str | None          # why the image could not be checked
    nics: list[NicSupportOut]


class HardwareCard(BaseModel):
    """A PC's boot network card for Add new hardware."""

    nic: str                     # PCI hardware ID: VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10
    instance: str                # its instance ID on that PC: 3&11583659&0&FE

    @field_validator("nic")
    @classmethod
    def _nic(cls, v: str) -> str:
        from app.windows_setup import NIC_ID_RE

        v = v.strip().upper().removeprefix("PCI\\")
        if not NIC_ID_RE.match(v):
            raise ValueError("must be a PCI hardware ID like VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10")
        return v

    @field_validator("instance")
    @classmethod
    def _instance(cls, v: str) -> str:
        from app.windows_setup import NIC_INSTANCE_RE

        v = v.strip().upper()
        if not NIC_INSTANCE_RE.match(v):
            raise ValueError("must be the device instance ID, the part after the hardware ID (e.g. 3&11583659&0&FE)")
        return v


class AddHardwareIn(BaseModel):
    cards: list[HardwareCard] = Field(min_length=1, max_length=8)


class AddHardwareJobOut(BaseModel):
    cards: list[HardwareCard]
    step: str                    # starting | job | vm | result | prepare | apply | done
    running: bool
    error: str | None
    snapshot: str | None         # the new version, when done
    devices: list[dict[str, Any]]
    started: float
    finished: float | None


class AddHardwareWizardOut(BaseModel):
    step: str                    # waiting | scanning | collected | adding | rebooting | done | failed
    active: bool
    machine: str | None          # the PC the wizard works on
    card: HardwareCard | None
    error: str | None
    started: float
    finished: float | None


class AddHardwareStatusOut(BaseModel):
    wizard: AddHardwareWizardOut | None
    job: AddHardwareJobOut | None
    waiting: list[str]           # PCs on screen waiting for Add new hardware for this image


class GameDiskCopy(BaseModel):
    """A new image from one version of this one (default: the active version)."""

    name: str
    snapshot: str | None = None

    check_name = field_validator("name")(_disk_name)


class PrepareImage(BaseModel):
    """Options of Prepare image beyond what every image gets."""

    defender_off: bool = False     # Microsoft Defender's services and drivers disabled
    services_off: bool = False     # background services a cafe PC does not need disabled (faster startup)


class GameDiskGrow(BaseModel):
    """A bigger size for a disk; `extend` also grows its last data partition."""

    size_gb: int = Field(gt=0, le=16384)
    extend: bool = True


class GameDiskGrown(BaseModel):
    disk: GameDiskOut
    version: str | None        # the new version (None for a draft, grown in place)
    extended: bool             # the data partition was grown on the server too
    note: str                  # what to do in Windows, if anything


class GameDiskRename(BaseModel):
    """A new name for the console; the zvol on the host keeps its path."""

    name: str

    check_name = field_validator("name")(_disk_name)


class GameDiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    zvol_path: str
    size_gb: int
    kind: DiskKind
    snapshot: str | None
    published: bool
    editor_id: int | None
    created_at: datetime
    copy_source: str | None = None    # being copied (or failed) from this master@version
    copy_percent: int | None = None
    copy_error: str | None = None
    # OS image: the network card models Add new hardware installed; none: it needs hardware.
    nic_models: list[str] = []


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
    comment: str | None = None   # who, when, what changed
    locked: bool = False         # never deleted (retention or by hand) until unlocked


def _comment(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not valid_comment(v):
        raise ValueError(f"comment: one line of at most {COMMENT_MAX} printable characters")
    return v


class SnapshotLock(BaseModel):
    locked: bool


class SnapshotComment(BaseModel):
    comment: str    # "" removes it

    @field_validator("comment")
    @classmethod
    def _check(cls, v: str) -> str:
        return _comment(v) or ""


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
    # Empty: derived from the MAC (<iqn_prefix>:pc-<mac>), or without a MAC
    # the Windows default iqn.1991-05.com.microsoft:<name>.
    initiator_iqn: str | None = None
    mac: str | None = None
    game_disk_id: int | None = None    # set → provision right away
    os_disk_id: int | None = None      # Boot Mode: OS image, needed with game_disk_id
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
    def check_letter(self):
        if self.mode is MachineMode.BOOT and self.drive_letter != "D":
            raise ValueError(BOOT_MODE_LETTER)
        return self


class MachineUpdate(BaseModel):
    """
    Partial update: only the fields sent are changed (`"mac": null` clears
    the MAC; `"initiator_iqn": ""` resets the IQN to its default, see
    MachineCreate). Name, IQN and mode only while the machine has no disk; the
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
            raise ValueError("IQN cannot be null; send \"\" for the default")
        return _iqn(v) if v.strip() else ""

    @field_validator("mac")
    @classmethod
    def check_mac(cls, v: str | None) -> str | None:
        return _mac(v)


class KeepWriteback(BaseModel):
    enabled: bool


class ApplyWritebacks(BaseModel):
    """Which kept writebacks become versions; `os` only counts in Boot Mode."""
    os: bool = True
    game: bool = True
    comment: str | None = None   # set on every version this makes

    @field_validator("comment")
    @classmethod
    def _check(cls, v: str | None) -> str | None:
        return _comment(v) or None


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
    game_disk_id: int | None   # None → remove the disks (deprovision)
    # Boot Mode: the OS image. Not sent → the machine keeps its current one.
    os_disk_id: int | None = None


class MachineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    mode: MachineMode
    initiator_iqn: str
    mac: str | None
    game_disk_id: int | None
    os_disk_id: int | None
    editing_disk_id: int | None
    drive_letter: str
    reported_drive_letter: str | None
    pinned_snapshot: str | None
    pending_command: str | None
    reported_ip: str | None
    reported_mac: str | None
    link_speed_mbps: int | None
    hardware: MachineHardware | None = Field(validation_alias="hardware_info")
    boot_nic: str | None
    nic_instance: str | None = None   # the boot card's instance ID on this PC (agent)
    mok_state: str | None
    status: MachineStatus
    last_error: str | None
    clone_zvol: str | None
    clone_snapshot: str | None
    os_clone_zvol: str | None
    os_clone_snapshot: str | None
    iscsi_target_iqn: str | None
    outdated: bool
    last_seen_at: datetime | None
    agent_version: str | None
    reported_iqn: str | None
    iscsi_connected: bool | None
    agent_stopped: bool
    keep_writeback: bool
    writeback_dirty: bool
    writeback_bytes: int | None
    writeback_large: bool
    session_active: bool | None
    session_changed_at: datetime | None
    booted_at: datetime | None
    ipxe_at: datetime | None             # Boot Mode: when iPXE last asked for the boot script
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
    stopping: bool = False              # last beat: the agent's service is stopping
    # The ggNet Secure Boot key on this PC (agent 0.1.13+, Boot Mode, once asked for).
    mok: Literal["enrolled", "requested", "failed"] | None = None

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
    # Windows booted with UEFI Secure Boot on (null: not UEFI, or an older agent).
    secure_boot: bool | None = None
    # The network card's Plug and Play ID: PCI\\<hardware ID>\\<instance ID>.
    nic_pnp_id: str | None = Field(None, max_length=200)

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
    secure_boot: bool | None = None


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
    command: str | None = None          # "shutdown" | "reboot", handed out once
    # Boot Mode, while Settings -> Network boot asks the PCs to enroll the key.
    secure_boot: "AgentSecureBoot | None" = None


class AgentSecureBoot(BaseModel):
    """The ggNet Secure Boot key a Boot Mode PC's agent asks shim to enroll."""

    certificate: str                    # DER, base64
    password: str                       # typed in MokManager to confirm


# ── Settings: retention and storage ───────────────────────────────────

class RetentionIn(BaseModel):
    """Automated snapshot and writeback removal (Settings → Storage)."""

    enabled: bool = False
    dry_run: bool = True
    reserved_percent: int = Field(15, ge=0, le=50)
    warning_percent: int = Field(80, ge=50, le=99)
    unused_snapshot_days: int = Field(14, ge=1, le=3650)
    keep_newest_snapshots: int = Field(3, ge=1, le=100)
    inactive_writeback_hours: int = Field(24, ge=1, le=24 * 365)
    # A PC's writeback (not kept) larger than this is flagged on Machines; 0 = never.
    writeback_warning_gb: int = Field(20, ge=0, le=10_000)


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


class PowerAction(BaseModel):
    action: Literal["on", "shutdown", "reboot"]   # on = Wake-on-LAN; the others go through the agent


class AgentPackageFileOut(BaseModel):
    name: str
    size_bytes: int
    sha256: str


class AgentPackageOut(BaseModel):
    """Settings → Client app: what a PC needs to install ggnet-agent."""

    available: bool                  # False: the files are not on the server yet
    directory: str                   # where the server looks for them
    version: str | None
    server_url: str
    install_command: str             # one line for an admin PowerShell on the PC
    files: list[AgentPackageFileOut]


BulkKind = Literal["on", "shutdown", "reboot", "assign", "pin", "drive_letter", "stop_keep_writeback"]


class BulkAction(BaseModel):
    """One action on several machines (bulk operations / Edit Selected)."""

    machine_ids: list[int] = Field(min_length=1, max_length=500)
    action: BulkKind
    game_disk_id: int | None = None   # assign: the disk, or None to remove it
    snapshot: str | None = None       # pin: a version, or None to follow the active one
    drive_letter: str | None = None   # drive_letter: D-Z

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str | None) -> str | None:
        return _drive_letter(v) if v is not None else None

    @model_validator(mode="after")
    def needs_letter(self):
        if self.action == "drive_letter" and self.drive_letter is None:
            raise ValueError("drive_letter: required for the drive_letter action")
        return self


class BulkResult(BaseModel):
    machine_id: int
    name: str | None
    ok: bool
    error: str | None = None


# ── Discovery ─────────────────────────────────────────────────────────

class DiscoveredOut(BaseModel):
    """A PC that knocked but is not registered (app.services.discovery)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    mac: str
    source: DiscoverySource
    hostname: str | None
    ip_address: str | None
    agent_version: str | None
    boot_nic: str | None
    seen_count: int
    ignored: bool
    first_seen_at: datetime
    last_seen_at: datetime


class DiscoveryClaim(BaseModel):
    """Add a discovered PC as a machine; its MAC comes from the discovery."""

    name: str
    mode: MachineMode = MachineMode.DISK
    game_disk_id: int | None = None
    os_disk_id: int | None = None
    drive_letter: str = "D"

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        return _machine_name(v)

    @field_validator("drive_letter")
    @classmethod
    def check_drive_letter(cls, v: str) -> str:
        return _drive_letter(v)


class BootSettingsIn(BaseModel):
    # "none" (0.0.0.0 in the iBFT), "dhcp" (the router's gateway) or an IPv4 address.
    gateway: str = "none"
    unregistered: Literal["wait", "local"] = "wait"
    # Agents of Boot Mode PCs ask shim to enroll the ggNet Secure Boot key; at
    # the next boot MokManager asks to confirm it with this password. Off by
    # default: until it is confirmed, every boot shows MokManager for 10 s.
    mok_enroll: bool = False
    # MokManager's keyboard is US: lower-case letters and digits only.
    mok_password: str = Field("ggnet", pattern=r"^[a-z0-9]{4,16}$")

    @field_validator("gateway")
    @classmethod
    def _gateway(cls, v: str) -> str:
        v = v.strip()
        if v in ("none", "dhcp"):
            return v
        try:
            return str(ipaddress.IPv4Address(v))
        except ValueError:
            raise ValueError('gateway must be "none", "dhcp" or an IPv4 address') from None


class BootSettingsOut(BootSettingsIn):
    # False: config.toml's values, never saved in the web UI.
    saved: bool


class SecureBootKeyOut(BaseModel):
    subject: str
    fingerprint: str
    expires: datetime | None
    ipxe_signed: bool | None


class PxeStatusOut(BaseModel):
    unit: str
    installed: bool
    state: str
    sub_state: str
    since: datetime | None
    tftp_root: str
    files: dict[str, bool]
    # None: no Secure Boot key on this server yet (scripts/install_ggnet.sh makes it).
    secure_boot: SecureBootKeyOut | None = None


# ── Sign-in, users, activity log ──────────────────────────────────────

def _username(v: str) -> str:
    v = v.strip().lower()
    if not USERNAME_RE.match(v):
        raise ValueError("username: 2-32 of a-z, 0-9, '.', '_', '-', starting with a letter or digit")
    return v


def _password(v: str) -> str:
    if len(v) < PASSWORD_MIN or len(v) > 128:
        raise ValueError(f"password: {PASSWORD_MIN} to 128 characters")
    return v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    disabled: bool
    created_at: datetime
    last_login_at: datetime | None


class AuthState(BaseModel):
    setup_required: bool                # no user yet: create the first admin with the setup code
    user: UserOut | None
    token: str | None = None            # for scripts (Authorization: Bearer); browsers use the cookie


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class SetupIn(BaseModel):
    setup_code: str = Field(max_length=32)
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        return _username(v)

    @field_validator("password")
    @classmethod
    def check_password(cls, v: str) -> str:
        return _password(v)


class UserCreate(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.STAFF

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        return _username(v)

    @field_validator("password")
    @classmethod
    def check_password(cls, v: str) -> str:
        return _password(v)


class UserUpdate(BaseModel):
    role: UserRole | None = None
    password: str | None = None
    disabled: bool | None = None

    @field_validator("password")
    @classmethod
    def check_password(cls, v: str | None) -> str | None:
        return _password(v) if v is not None else v


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def check_password(cls, v: str) -> str:
        return _password(v)


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    username: str | None
    action: str
    status_code: int
    ip_address: str | None


# ── Server ────────────────────────────────────────────────────────────

class PoolScanOut(BaseModel):
    function: str | None                     # scrub | resilver
    state: str | None                        # scanning | finished | canceled
    start_time: datetime | None
    end_time: datetime | None
    errors: int
    percent: float | None                    # while scanning
    paused: bool


class PoolGroupOut(BaseModel):
    name: str                                # mirror-0
    type: str                                # mirror | raidz1 | disk
    state: str
    disks: list[str]


class PoolDiskOut(BaseModel):
    name: str
    group: str
    state: str                               # ONLINE | DEGRADED | FAULTED | ...
    read_errors: int
    write_errors: int
    checksum_errors: int
    trim_state: str | None                   # ACTIVE while a TRIM runs
    trim_percent: float | None               # while a TRIM runs
    trim_time: datetime | None               # last TRIM of this disk
    trim_supported: bool
    # SMART, as last read (None: not read yet, or the disk does not report it)
    smart_passed: bool | None
    model: str | None
    serial: str | None
    capacity_bytes: int | None
    temperature: int | None
    wear_percent: int | None                 # endurance used
    power_on_hours: int | None


class PoolOut(BaseModel):
    name: str
    state: str                               # ONLINE | DEGRADED | ...
    size_bytes: int | None
    allocated_bytes: int | None
    free_bytes: int | None
    fragmentation: int | None                # percent
    capacity: int | None                     # percent
    errors: int                              # data errors
    scan: PoolScanOut | None
    last_trim: datetime | None               # oldest last TRIM over the disks
    autotrim: bool | None                    # ZFS trims freed blocks by itself (no need for a manual TRIM)
    groups: list[PoolGroupOut]
    disks: list[PoolDiskOut]


class ArcOut(BaseModel):
    size_bytes: int
    max_bytes: int
    min_bytes: int
    hit_percent: float | None                # since boot
    # What the maximum may be set to (Server → RAM cache), from the RAM and zfs_arc_min.
    allowed_min_bytes: int | None = None
    allowed_max_bytes: int | None = None


class ArcIn(BaseModel):
    max_gb: int = Field(gt=0, le=65536)


class MemoryOut(BaseModel):
    total_bytes: int
    available_bytes: int


class ServerOut(BaseModel):
    pool: PoolOut | None                     # None: zpool could not be read
    arc: ArcOut | None
    memory: MemoryOut | None
    writebacks_bytes: int                    # every PC's writebacks together
    writebacks_count: int
    smart_read_at: datetime | None           # when SMART was last read (on request only)


class PoolActionIn(BaseModel):
    """Start or stop a scrub or a TRIM."""

    action: Literal["start", "stop"]


class MaintenanceIn(BaseModel):
    """Weekly TRIM and scrub in a window when the cafe is closed (server local time)."""

    enabled: bool = False
    weekday: int = Field(6, ge=0, le=6)               # 0 Monday … 6 Sunday
    start: str = Field("03:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")   # before start: next morning
    trim: bool = True
    scrub: bool = True
    order: Literal["trim_first", "scrub_first"] = "trim_first"


class MaintenanceStepOut(BaseModel):
    name: str                                         # trim | scrub
    status: str                                       # waiting | running | paused | done | stopped | failed
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class MaintenanceRunOut(BaseModel):
    window_start: datetime
    window_end: datetime
    closed: bool
    steps: list[MaintenanceStepOut]


class MaintenanceOut(MaintenanceIn):
    next_window: datetime | None                      # when the next one starts, if enabled
    last_run: MaintenanceRunOut | None
