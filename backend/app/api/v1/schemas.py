"""Pydantic schemas for API v1."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The disk name becomes part of a ZFS path, so only [a-z0-9-].
_DISK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


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
