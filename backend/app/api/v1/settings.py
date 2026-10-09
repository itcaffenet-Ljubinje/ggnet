"""
API for settings changed in the web UI: retention (automated removal of old
versions and inactive writebacks) and the pool's reserved space.

Nothing touches the host until the admin saves the settings: GET returns the
defaults with `saved: false`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner
from app.api.v1.errors import conflict, host_failed
from app.api.v1.schemas import (
    RetentionActionOut,
    RetentionIn,
    RetentionOut,
    RetentionReportOut,
    StorageOut,
)
from app.db.session import get_db
from app.services import retention
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.api.settings")

router = APIRouter(tags=["settings"])


def _out(db: Session) -> RetentionOut:
    s, saved = retention.load_settings(db)
    return RetentionOut(**asdict(s), saved=saved, last_run=retention.last_report(db))


@router.get("/settings/retention", response_model=RetentionOut)
def get_retention(db: Session = Depends(get_db)):
    return _out(db)


@router.put("/settings/retention", response_model=RetentionOut)
def put_retention(
    body: RetentionIn,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Save the settings; a new reserved percentage is applied to the pool right away."""
    old, saved = retention.load_settings(db)
    if not saved or body.reserved_percent != old.reserved_percent:
        try:
            prov.set_reserved_percent(body.reserved_percent)
        except ProvisioningError as e:
            host_failed(str(e))
    retention.save_settings(db, retention.RetentionSettings(**body.model_dump()))
    return _out(db)


@router.post("/settings/retention/preview", response_model=list[RetentionActionOut])
def preview_retention(
    body: RetentionIn,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """What these settings (saved or not) would delete now. Changes nothing."""
    try:
        actions = retention.plan(db, prov, retention.RetentionSettings(**body.model_dump()))
    except ProvisioningError as e:
        host_failed(str(e))
    return [RetentionActionOut(**asdict(a)) for a in actions]


@router.post("/settings/retention/run", response_model=RetentionReportOut)
def run_retention(
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Delete now what the SAVED settings allow, whether or not the hourly job is on."""
    s, saved = retention.load_settings(db)
    if not saved:
        conflict("Save the retention settings first")
    retention.run(db, prov, s, dry_run=False)
    return retention.last_report(db)


@router.get("/storage", response_model=StorageOut)
def get_storage(
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    """Space of the whole pool (it also holds data that is not ggNet's)."""
    try:
        space = prov.pool_space()
    except ProvisioningError as e:
        host_failed(str(e))
    s, _ = retention.load_settings(db)
    percent = round(100 * space["used"] / space["total"], 1) if space["total"] else 0.0
    return StorageOut(
        pool=prov.zfs.layout.pool,
        total_bytes=space["total"],
        used_bytes=space["used"],
        available_bytes=space["available"],
        used_percent=percent,
        reserved_bytes=prov.reserved_bytes(),
        warning_percent=s.warning_percent,
        warning=percent >= s.warning_percent,
    )
