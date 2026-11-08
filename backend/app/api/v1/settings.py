"""
API for settings changed in the web UI: retention (automated removal of old
versions and inactive writebacks), the pool's reserved space, and network
boot (the boot settings and the PXE service's state, app.services.pxe).

Nothing touches the host until the admin saves the settings: GET returns the
defaults with `saved: false`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner, get_pxe_service
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    BootSettingsIn,
    BootSettingsOut,
    PxeStatusOut,
    RetentionActionOut,
    RetentionIn,
    RetentionOut,
    RetentionReportOut,
    StorageOut,
    TlsOut,
)
from app.db.session import get_db
from app.services import pxe, retention
from app.services.pxe import PxeService
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


@router.get("/settings/boot", response_model=BootSettingsOut)
def get_boot_settings(db: Session = Depends(get_db)):
    s, saved = pxe.load_settings(db)
    return BootSettingsOut(**asdict(s), saved=saved)


@router.put("/settings/boot", response_model=BootSettingsOut)
def put_boot_settings(body: BootSettingsIn, db: Session = Depends(get_db)):
    """Applies to the next PC that asks for its iPXE script; nothing is restarted."""
    pxe.save_settings(db, pxe.BootSettings(**body.model_dump()))
    return get_boot_settings(db)


@router.get("/settings/tls", response_model=TlsOut)
def tls_status():
    """Whether the web UI runs on HTTPS, and its certificate."""
    from app import serve
    from app.config import get_config
    from app.services.tls import cert_info

    cfg = get_config()
    secure = serve.tls(cfg)
    if secure is None:
        return TlsOut(enabled=False)
    port, cert, _ = secure
    info = cert_info(cert) or {}
    return TlsOut(enabled=True, url=f"https://{cfg['network']['server_ip']}:{port}", **info)


@router.get("/settings/tls/certificate")
def tls_certificate():
    """The certificate (no key), to add to the admin PCs' trusted root certificates."""
    from fastapi.responses import FileResponse

    from app import serve

    secure = serve.tls()
    if secure is None:
        not_found("HTTPS certificate", "ggnet.crt")
    return FileResponse(secure[1], media_type="application/x-x509-ca-cert", filename="ggnet.crt")


@router.get("/pxe", response_model=PxeStatusOut)
def get_pxe_status(service: PxeService = Depends(get_pxe_service)):
    status = service.status()
    if status is None:
        host_failed(f"Cannot read the state of {service.unit}: {service.runner.last_error}")
    return PxeStatusOut(**asdict(status))


@router.post("/pxe/restart", response_model=PxeStatusOut)
def restart_pxe(service: PxeService = Depends(get_pxe_service)):
    """Restart the proxyDHCP + TFTP service, e.g. after a network change. PCs booting now retry."""
    if not service.restart():
        host_failed(f"Restarting {service.unit} failed: {service.runner.last_error}")
    return get_pxe_status(service)
