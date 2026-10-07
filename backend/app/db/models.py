"""
ORM models. Relations are real foreign keys, never IDs stored as strings.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
)
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
    the API would send it without "Z" (browsers then read it as local time).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(timezone.utc) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _str_enum(cls: type[enum.Enum]) -> Enum:
    """Store an enum by its value as a plain string column (portable across databases)."""
    return Enum(cls, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class GameDisk(Base):
    """
    Master image (shared disk). Until `snapshot` is set the disk is a draft
    and must not be assigned to machines. After publish_master(), `snapshot`
    is the name of the current snapshot (`base`, later `vN`).
    """

    __tablename__ = "game_disks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    zvol_path: Mapped[str] = mapped_column(String(255), unique=True)
    size_gb: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)

    machines: Mapped[list["Machine"]] = relationship(back_populates="game_disk")

    @property
    def published(self) -> bool:
        return self.snapshot is not None

    @property
    def snapshot_path(self) -> str | None:
        return f"{self.zvol_path}@{self.snapshot}" if self.snapshot else None


class MachineMode(str, enum.Enum):
    DISK = "disk"    # local Windows on C:, game disk over iSCSI
    BOOT = "boot"    # diskless UEFI PXE boot (later)


class MachineStatus(str, enum.Enum):
    IDLE = "idle"                  # no disk assigned
    PROVISIONED = "provisioned"    # clone + iSCSI target exist
    ERROR = "error"                # a host operation failed; see last_error


class Machine(Base):
    """
    Client PC. The iSCSI ACL is bound to `initiator_iqn`; the MAC is only
    for DHCP/WoL.

    `clone_zvol`/`iscsi_target_iqn` are recorded at provisioning time; they
    are the source of truth for cleanup even if the config changes later.
    """

    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(15), unique=True)
    mode: Mapped[MachineMode] = mapped_column(_str_enum(MachineMode), default=MachineMode.DISK)
    initiator_iqn: Mapped[str] = mapped_column(String(223), unique=True)
    mac: Mapped[str | None] = mapped_column(String(17), unique=True, default=None)

    game_disk_id: Mapped[int | None] = mapped_column(
        ForeignKey("game_disks.id", ondelete="RESTRICT"), default=None, index=True
    )
    game_disk: Mapped[GameDisk | None] = relationship(back_populates="machines")

    status: Mapped[MachineStatus] = mapped_column(
        _str_enum(MachineStatus), default=MachineStatus.IDLE
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    clone_zvol: Mapped[str | None] = mapped_column(String(255), default=None)
    clone_snapshot: Mapped[str | None] = mapped_column(String(320), default=None)
    iscsi_target_iqn: Mapped[str | None] = mapped_column(String(223), default=None)

    # Reported by ggnet-agent in its heartbeat; never used for provisioning.
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    agent_version: Mapped[str | None] = mapped_column(String(32), default=None)
    reported_iqn: Mapped[str | None] = mapped_column(String(223), default=None)
    iscsi_connected: Mapped[bool | None] = mapped_column(Boolean, default=None)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
    )

    @property
    def outdated(self) -> bool:
        """The clone was made from an older snapshot than the disk now offers."""
        return (
            self.game_disk is not None
            and self.clone_snapshot is not None
            and self.clone_snapshot != self.game_disk.snapshot_path
        )
