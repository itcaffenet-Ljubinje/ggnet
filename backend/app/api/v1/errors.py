"""Mapping errors to HTTP responses."""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException


def not_found(what: str, id_: int | str) -> NoReturn:
    raise HTTPException(404, detail={"error": f"{what} {id_} does not exist"})


def conflict(msg: str) -> NoReturn:
    raise HTTPException(409, detail={"error": msg})


def host_failed(msg: str, **extra) -> NoReturn:
    """502: an operation on the Proxmox host failed. Never report a fake success."""
    raise HTTPException(502, detail={"error": msg, **extra})
