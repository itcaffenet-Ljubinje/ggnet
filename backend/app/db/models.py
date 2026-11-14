"""
ORM models. Relations are real foreign keys, never IDs stored as strings.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Stable constraint names; Alembic cannot reliably alter unnamed ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """
    Timezone-aware datetime that always comes back in UTC. SQLite drops the
    offset, so without this a value read back from the database is naive and
    the API would send it without "Z" (browsers then read it as local time);
    PostgreSQL returns it in the server's time zone (+01:00), the same moment.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(timezone.utc) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _str_enum(cls: type[enum.Enum]) -> Enum:
    """Store an enum by its value as a plain string column (portable across databases)."""
    return Enum(cls, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ImageKind(str, enum.Enum):
    GAME = "game"    # game image: Disk Mode D:, Boot Mode LUN 1
    OS = "os"        # Windows system disk of Boot Mode machines (LUN 0)


def version_number(snapshot: str | None) -> int:
    """An image version's order: base → 1, vN → N; unknown counts as 1."""
    if snapshot and snapshot.startswith("v") and snapshot[1:].isdigit():
        return int(snapshot[1:])
    return 1


class Image(Base):
    """
    Master image (shared disk). `kind` says whether it holds games or a
    Boot Mode Windows install. Until `snapshot` is set the disk is a draft
    and must not be assigned to machines. After publish_master(), `snapshot`
    is the name of the current snapshot (`base`, later `vN`).

    A draft is filled through one machine (`editor`): the draft zvol itself
    is mapped as that machine's game image until editing is finished.
    """

    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    zvol_path: Mapped[str] = mapped_column(String(255), unique=True)
    size_gb: Mapped[int] = mapped_column(Integer)
    kind: Mapped[ImageKind] = mapped_column(
        _str_enum(ImageKind), default=ImageKind.GAME, server_default=ImageKind.GAME.value
    )
    snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    # Copy: while set, the zvol is being filled from that version (master@v)
    # and the disk is neither a draft to fill nor published yet.
    copy_source: Mapped[str | None] = mapped_column(String(255), default=None)
    copy_percent: Mapped[int | None] = mapped_column(Integer, default=None)
    copy_error: Mapped[str | None] = mapped_column(String(500), default=None)
    # The default of its kind: new PCs get the default OS image and game image
    # (the New PC template, the Machines and Discovered forms). One per kind.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())

    machines: Mapped[list["Machine"]] = relationship(
        back_populates="game_image", foreign_keys="Machine.game_image_id"
    )
    editor: Mapped["Machine | None"] = relationship(
        back_populates="editing_image", foreign_keys="Machine.editing_image_id"
    )
    # OS image: the network cards Add new hardware installed in it.
    hardware: Mapped[list["ImageHardware"]] = relationship(
        back_populates="disk", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ImageHardware.id",
    )

    @property
    def nic_models(self) -> list[str]:
        """
        The network card models the image's active version supports, each once
        (an OS image without any needs hardware): a card added at a newer
        version than the active one is not in it.
        """
        active = version_number(self.snapshot)
        return sorted({h.name for h in self.hardware if version_number(h.snapshot) <= active})

    @property
    def editor_id(self) -> int | None:
        return self.editor.id if self.editor else None

    @property
    def published(self) -> bool:
        return self.snapshot is not None

    @property
    def copying(self) -> bool:
        return self.copy_source is not None and self.copy_error is None

    @property
    def snapshot_path(self) -> str | None:
        return f"{self.zvol_path}@{self.snapshot}" if self.snapshot else None


class MachineMode(str, enum.Enum):
    DISK = "disk"    # local Windows on C:, game image over iSCSI
    BOOT = "boot"    # diskless UEFI PXE boot: OS image (LUN 0) + game image (LUN 1)


class MachineStatus(str, enum.Enum):
    IDLE = "idle"                  # no disk assigned
    PROVISIONED = "provisioned"    # clone exists and is mapped on the shared target
    EDITING = "editing"            # a draft master is mapped as its game image
    ERROR = "error"                # a host operation failed; see last_error


class ImageHardware(Base):
    """
    A network card the Add new hardware wizard installed in an OS image: the
    card's PCI hardware ID and instance ID on that board type, and its model
    as Windows names it (from the driver). An OS image without any is not
    ready: no PC can boot it yet.
    """

    __tablename__ = "image_hardware"
    __table_args__ = (UniqueConstraint("image_id", "nic", "instance", name="uq_image_hardware_card"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"))
    nic: Mapped[str] = mapped_column(String(64))           # VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10
    instance: Mapped[str] = mapped_column(String(64))      # 3&11583659&0&FE
    name: Mapped[str] = mapped_column(String(255))         # Intel(R) Ethernet Connection (2) I219-V
    snapshot: Mapped[str | None] = mapped_column(String(64), default=None)   # the version it came with
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)

    disk: Mapped[Image] = relationship(back_populates="hardware")


class Machine(Base):
    """
    Client PC. The iSCSI ACL is bound to `initiator_iqn`. The MAC identifies
    the PC (iPXE asks by MAC, the agent reports it) and is used for WoL; with
    a MAC the default IQN is derived from it (app.services.identity), so it
    does not depend on the Windows computer name.

    `clone_zvol`/`iscsi_target_iqn` are recorded at provisioning time; they
    are the source of truth for cleanup even if the config changes later.
    """

    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(15), unique=True)
    mode: Mapped[MachineMode] = mapped_column(_str_enum(MachineMode), default=MachineMode.DISK)
    initiator_iqn: Mapped[str] = mapped_column(String(223), unique=True)
    mac: Mapped[str | None] = mapped_column(String(17), unique=True, default=None)

    game_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="RESTRICT"), default=None, index=True
    )
    game_image: Mapped[Image | None] = relationship(
        back_populates="machines", foreign_keys=[game_image_id]
    )
    # The draft master this machine is filling (status `editing`); at most
    # one editor per disk.
    editing_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="RESTRICT"), default=None, unique=True
    )
    editing_image: Mapped[Image | None] = relationship(
        back_populates="editor", foreign_keys=[editing_image_id]
    )

    # Boot Mode: the OS image (kind `os`) and this machine's writeback of it.
    # It follows the image's active version (no pin yet) and is discarded
    # together with the game writeback.
    os_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="RESTRICT"), default=None, index=True
    )
    os_image: Mapped[Image | None] = relationship(foreign_keys=[os_image_id])
    os_clone_zvol: Mapped[str | None] = mapped_column(String(255), default=None)
    os_clone_snapshot: Mapped[str | None] = mapped_column(String(320), default=None)

    # Game image letter on the PC (Disk Mode; Boot Mode is always D:).
    drive_letter: Mapped[str] = mapped_column(String(1), default="D", server_default="D")

    status: Mapped[MachineStatus] = mapped_column(
        _str_enum(MachineStatus), default=MachineStatus.IDLE
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    clone_zvol: Mapped[str | None] = mapped_column(String(255), default=None)
    clone_snapshot: Mapped[str | None] = mapped_column(String(320), default=None)
    iscsi_target_iqn: Mapped[str | None] = mapped_column(String(223), default=None)

    # Snapshot pin (machine settings → version): the version of the
    # game image this machine runs instead of the active one; None follows it.
    pinned_snapshot: Mapped[str | None] = mapped_column(String(64), default=None)

    # Writeback lifecycle: the server discards the writeback
    # after every disconnect unless keep_writeback is set.
    keep_writeback: Mapped[bool] = mapped_column(Boolean, default=False)
    # Kept out of the Machines list (Show hidden lists it), e.g. while it is
    # repaired; it still boots and reports like any other PC.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    # A virtual machine on this Proxmox host (app.services.vms): its
    # writebacks are the VM's drives, not iSCSI LUNs. None for a PC.
    vm_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, default=None)
    # The client wrote (was connected) since the clone was made; a clean
    # clone is never discarded again.
    writeback_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bytes the PC has written into its writebacks (OS and game image), as
    # ZFS last measured them (app.services.writebacks.measure); None = not yet.
    writeback_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # Over the size warning of Settings → Retention (never for a kept writeback).
    writeback_large: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last iSCSI session state seen in LIO (None = never read) and since when.
    session_active: Mapped[bool | None] = mapped_column(Boolean, default=None)
    session_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Windows boot time reported by the agent; a newer value means a new boot.
    booted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Boot Mode: when iPXE last asked for this PC's script. iPXE's own iSCSI
    # session ends when Windows takes over, before Windows logs in itself;
    # the watcher must not take that gap for a shutdown.
    ipxe_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    # Boot Mode: the boot network card's PCI hardware ID as iPXE read it
    # (VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16), for Add new hardware.
    boot_nic: Mapped[str | None] = mapped_column(String(64), default=None)
    # The boot network card's device instance ID as Windows names it on this
    # PC (01000000684CE00000, 6&95d5d6a&0&0000020A), reported by the agent or
    # the hardware scan: Add new hardware installs the card under this ID.
    nic_instance: Mapped[str | None] = mapped_column(String(64), default=None)
    # Boot Mode: the ggNet Secure Boot key on this PC as its agent reports it:
    # "enrolled", "requested" (confirm in MokManager at the next boot), "failed".
    mok_state: Mapped[str | None] = mapped_column(String(16), default=None)

    # Reported by ggnet-agent in its heartbeat; never used for provisioning.
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    agent_version: Mapped[str | None] = mapped_column(String(32), default=None)
    reported_iqn: Mapped[str | None] = mapped_column(String(223), default=None)
    iscsi_connected: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # The agent's last heartbeat said its service is stopping (Windows shutting
    # down): the PC is offline now, not only after missed heartbeats.
    agent_stopped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    reported_drive_letter: Mapped[str | None] = mapped_column(String(1), default=None)
    # Inventory from the agent: the network it reaches the server on and the
    # hardware (IP and Link Speed columns, Hardware tab). JSON text.
    # Shutdown / reboot for the agent: handed out once in the next heartbeat
    # answer, and dropped when older than POWER_COMMAND_TTL (app.services.power).
    pending_command: Mapped[str | None] = mapped_column(String(16), default=None)
    pending_command_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    reported_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    reported_mac: Mapped[str | None] = mapped_column(String(17), default=None)
    link_speed_mbps: Mapped[int | None] = mapped_column(Integer, default=None)
    hardware: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
    )

    @property
    def hardware_info(self) -> dict | None:
        return json.loads(self.hardware) if self.hardware else None

    @property
    def target_snapshot_path(self) -> str | None:
        """The version this machine is cloned from: its pin, else the disk's active one."""
        if self.game_image is None or not self.game_image.published:
            return None
        if self.pinned_snapshot:
            return f"{self.game_image.zvol_path}@{self.pinned_snapshot}"
        return self.game_image.snapshot_path

    @property
    def os_snapshot_path(self) -> str | None:
        """The OS version a Boot Mode machine is cloned from: the image's active one."""
        if self.mode is not MachineMode.BOOT or self.os_image is None:
            return None
        return self.os_image.snapshot_path

    @property
    def outdated(self) -> bool:
        """A clone was made from another snapshot than the one it should run."""
        game = (
            self.clone_snapshot is not None
            and self.target_snapshot_path is not None
            and self.clone_snapshot != self.target_snapshot_path
        )
        os_ = (
            self.os_clone_snapshot is not None
            and self.os_snapshot_path is not None
            and self.os_clone_snapshot != self.os_snapshot_path
        )
        return game or os_

    @property
    def image_change_pending(self) -> bool:
        """
        The writebacks still come from another image than the assigned one:
        the image was changed while the PC was on, and its disks are never
        taken from under a running Windows. They are replaced at the PC's
        next restart, like a move to a newer version.
        """
        def other(clone_snapshot: str | None, image: "Image | None") -> bool:
            return (clone_snapshot is not None and image is not None
                    and not clone_snapshot.startswith(f"{image.zvol_path}@"))

        return other(self.clone_snapshot, self.game_image) or (
            self.mode is MachineMode.BOOT and other(self.os_clone_snapshot, self.os_image))


class AppSetting(Base):
    """
    Settings the admin changes in the web UI (Settings page), one JSON value
    per key. Settings that need a restart stay in config.toml.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, onupdate=_now)


class DiscoverySource(str, enum.Enum):
    PXE = "pxe"      # asked /boot/ipxe for a script
    AGENT = "agent"  # its ggnet-agent sent a heartbeat


class DiscoveredMachine(Base):
    """
    A PC that knocked but is not registered: an unknown MAC asked for an iPXE
    script, or an agent reported from an unknown PC. The admin adds it as a
    machine (claim) or ignores it. Rows are keyed by MAC, so a PC that keeps
    knocking updates its row instead of adding new ones.
    """

    __tablename__ = "discovered_machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mac: Mapped[str] = mapped_column(String(17), unique=True)
    source: Mapped[DiscoverySource] = mapped_column(_str_enum(DiscoverySource))
    hostname: Mapped[str | None] = mapped_column(String(64), default=None)     # agent only
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    agent_version: Mapped[str | None] = mapped_column(String(32), default=None)
    boot_nic: Mapped[str | None] = mapped_column(String(64), default=None)   # iPXE only
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class UserRole(str, enum.Enum):
    ADMIN = "admin"    # everything, including users and settings
    STAFF = "staff"    # sees everything; may only turn PCs on, off and restart them


class User(Base):
    """Someone who signs in to the web UI (cafe owner or staff)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    # scrypt, as app.services.auth.hash_password writes it; never the password itself.
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(_str_enum(UserRole), default=UserRole.STAFF)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)


class AuthSession(Base):
    """A signed-in browser or script. Only a hash of its token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())

    user: Mapped[User] = relationship()


class Activity(Base):
    """Activity log: every change made through the web UI or API, by whom."""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, index=True)
    username: Mapped[str | None] = mapped_column(String(32), default=None)
    action: Mapped[str] = mapped_column(String(200))     # "POST /api/v1/machines/3/power"
    status_code: Mapped[int] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)


class BackupJob(Base):
    """
    One backup of an image into a folder of a backup location, or one
    restore of such a backup into a new image (app.services.backups). The
    rows are the History tab of Images -> Backup / Restore.
    """

    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))                 # backup | restore | export
    # Backup: the image backed up; restore: the new image (gone if it failed).
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id", ondelete="SET NULL"), default=None)
    image_name: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128))            # usb:<partition> or dir:<n>
    location_label: Mapped[str] = mapped_column(String(255), default="")
    folder: Mapped[str | None] = mapped_column(String(512), default=None)    # an export: the file
    # An export: the version and the file format (vhdx, vhd).
    snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    format: Mapped[str | None] = mapped_column(String(8), default=None)
    status: Mapped[str] = mapped_column(String(16), default="queued")   # queued running done failed cancelled
    done_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
