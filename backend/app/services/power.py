"""
Power control of client PCs (ggRock's Turn On, Shutdown and Reboot).

Turn On is Wake-on-LAN: a magic packet (6 × 0xFF, then the MAC 16 times) as a
UDP broadcast on the clients' network, sent from the service IP so it leaves
on the right interface. The PC's BIOS and network adapter must have WoL on.

Shutdown and reboot go through ggnet-agent: the command waits on the machine
and is handed out once, in the answer to its next heartbeat. A command older
than POWER_COMMAND_TTL is dropped, so a PC that was off does not shut down
by surprise the next time someone turns it on.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.db.models import Machine

logger = logging.getLogger("ggnet.power")

AGENT_COMMANDS = ("shutdown", "reboot")
POWER_COMMAND_TTL = timedelta(minutes=2)
WOL_PORT = 9

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def magic_packet(mac: str) -> bytes:
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def broadcast_address(cidr: str) -> str:
    return str(ipaddress.ip_network(cidr, strict=False).broadcast_address)


def send_wol(mac: str, broadcast: str, source_ip: str | None = None) -> None:
    """Send one magic packet; raises OSError if the socket refuses."""
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if source_ip:
            sock.bind((source_ip, 0))
        sock.sendto(packet, (broadcast, WOL_PORT))
    logger.info("Wake-on-LAN sent to %s via %s", mac, broadcast)


WolSender = Callable[[str], None]


def take_command(m: Machine, now: datetime | None = None) -> str | None:
    """The machine's pending command if still fresh; clears it either way (at most once)."""
    if m.pending_command is None:
        return None
    now = now or datetime.now(timezone.utc)
    fresh = m.pending_command_at is not None and now - m.pending_command_at <= POWER_COMMAND_TTL
    command = m.pending_command if fresh else None
    if not fresh:
        logger.info("Dropped stale %s for %s", m.pending_command, m.name)
    m.pending_command = m.pending_command_at = None
    return command
