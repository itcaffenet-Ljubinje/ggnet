"""Shared FastAPI dependencies. Tests replace them via dependency_overrides."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_config, server_url
from app.db.models import User
from app.db.session import get_db
from app.iscsi_manager import ISCSIManager
from app.services.provisioning import Provisioner
from app.services.traffic import TrafficMonitor
from app.services.writebacks import WritebackSettings
from app.windows_setup import WindowsSetup
from app.zfs_manager import ZFSManager


@lru_cache(maxsize=1)
def get_provisioner() -> Provisioner:
    """One Provisioner (and one lock) for the whole process; config from config.toml."""
    return Provisioner(ZFSManager(), ISCSIManager(), WindowsSetup())


def get_disk_copier(db: Session = Depends(get_db), prov: Provisioner = Depends(get_provisioner)):
    """Runs image copies in the background, on the request's database."""
    from sqlalchemy.orm import sessionmaker

    from app.services.copies import DiskCopier

    return DiskCopier(sessionmaker(bind=db.get_bind(), expire_on_commit=False), prov)


def get_disk_importer(db: Session = Depends(get_db), prov: Provisioner = Depends(get_provisioner)):
    """Runs image imports in the background, on the request's database."""
    from sqlalchemy.orm import sessionmaker

    from app.services.imports import DiskImporter

    return DiskImporter(sessionmaker(bind=db.get_bind(), expire_on_commit=False), prov)


@lru_cache(maxsize=4)
def _hardware_adder(prov: Provisioner):
    from app.services.os_setup import image_prep
    from app.services.toolchain import HardwareAdder, ToolchainVm

    return HardwareAdder(prov, ToolchainVm(prov.zfs.runner), image_prep)


def get_hardware_adder(prov: Provisioner = Depends(get_provisioner)):
    """Add new hardware jobs (one per image), kept in memory for the whole process."""
    return _hardware_adder(prov)


@lru_cache(maxsize=1)
def get_driver_library():
    """Signed network driver packages for the hardware scan's Windows PE ([paths] winpe_drivers)."""
    from app.services.scan import DriverLibrary, winpe_drivers_dir

    return DriverLibrary(winpe_drivers_dir(get_config()))


def get_usb_disks(prov: Provisioner = Depends(get_provisioner)):
    """The server's USB disks that image files can be imported from."""
    from app.services.imports import UsbDisks

    return UsbDisks(prov.zfs.runner)


def get_writeback_settings() -> WritebackSettings:
    return WritebackSettings.from_config(get_config())


@lru_cache(maxsize=1)
def get_server_monitor():
    """The Server page's view of the pool named in config.toml (caches SMART)."""
    from app.services.server_status import ServerMonitor

    return ServerMonitor(get_config()["storage"]["pool"])


@lru_cache(maxsize=1)
def get_traffic_monitor() -> TrafficMonitor:
    return TrafficMonitor()


def get_wol_sender():
    """Sends a magic packet on the clients' network, from the service IP."""
    from app.services.power import broadcast_address, send_wol

    net = get_config()["network"]
    broadcast = broadcast_address(net["cidr"])
    return lambda mac: send_wol(mac, broadcast, net.get("server_ip"))


def get_agent_package():
    """The agent package in [paths] agent_dir (default <data_dir>/agent)."""
    from app.services.agent_package import default_package

    return default_package()


def get_server_url() -> str:
    """The URL agents use to reach this server (service IP and web port)."""
    return server_url()


def get_pxe_service():
    """The PXE service (proxyDHCP + TFTP) named in config.toml."""
    from app.services.pxe import PxeService

    return PxeService.from_config()


def _token(request) -> str | None:
    """The session token: the browser's cookie, or `Authorization: Bearer` from a script."""
    from app.services.auth import COOKIE

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(COOKIE)


def require_user(request: Request, db: Session = Depends(get_db)) -> Iterator[User]:
    """
    Router dependency: the request must come from a signed-in user whose role
    allows it (app.services.auth.allowed). Every change (anything but GET) is
    written to the activity log with its outcome.
    """
    from app.services import auth

    user = auth.user_for(db, _token(request))
    if user is None:
        raise HTTPException(401, detail={"error": "Sign in first"})
    method, path = request.method, request.url.path
    if not auth.allowed(user, method, path):
        raise HTTPException(403, detail={"error": f"Your role ({user.role.value}) cannot do this"})
    if method in ("GET", "HEAD"):
        yield user
        return
    ip = request.client.host if request.client else None
    status = 500
    try:
        yield user
        status = 200
    except HTTPException as e:
        status = e.status_code
        raise
    finally:
        # Never commit what a failed request left behind; write only the log entry.
        db.rollback()
        auth.record(db, user, method, path, status, ip)
