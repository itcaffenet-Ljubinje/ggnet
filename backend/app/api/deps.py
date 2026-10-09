"""Shared FastAPI dependencies. Tests replace them via dependency_overrides."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_config
from app.iscsi_manager import ISCSIManager
from app.services.provisioning import Provisioner
from app.services.traffic import TrafficMonitor
from app.services.writebacks import WritebackSettings
from app.zfs_manager import ZFSManager


@lru_cache(maxsize=1)
def get_provisioner() -> Provisioner:
    """One Provisioner (and one lock) for the whole process; config from config.toml."""
    return Provisioner(ZFSManager(), ISCSIManager())


def get_writeback_settings() -> WritebackSettings:
    return WritebackSettings.from_config(get_config())


@lru_cache(maxsize=1)
def get_traffic_monitor() -> TrafficMonitor:
    return TrafficMonitor()


def get_wol_sender():
    """Sends a magic packet on the clients' network, from the service IP."""
    from app.services.power import broadcast_address, send_wol

    net = get_config()["network"]
    broadcast = broadcast_address(net["cidr"])
    return lambda mac: send_wol(mac, broadcast, net.get("server_ip"))
