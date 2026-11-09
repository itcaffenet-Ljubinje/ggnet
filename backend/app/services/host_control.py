"""
Settings -> General: this server at a glance, its services, and a reboot.

  * Services: ggNet itself (whatever unit runs this process), the PXE service
    (`[boot] pxe_service`), PostgreSQL (when it is the database) and the
    iSCSI target's saved configuration (rtslib-fb-targetctl). Each shows its
    state; all but the iSCSI target can be restarted. Restarting the target
    would drop every PC's disk at once, so the web UI never offers it.
  * Restarting ggNet itself, and rebooting the server, go through
    `systemd-run --on-active`: the answer reaches the browser before the
    service (or the host) goes down.
  * The server is the Proxmox VE host: a reboot also stops its VMs and
    containers, and every running PC loses its disks. The API wants the word
    REBOOT, and says what is running, before it does it.
"""

from __future__ import annotations

import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.runner import CommandRunner

logger = logging.getLogger("ggnet.host")

ISCSI_UNIT = "rtslib-fb-targetctl"
REBOOT_WORD = "REBOOT"
DETECT: Any = object()     # HostControl(unit=...): find it in /proc/self/cgroup
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,200}\.service$")


class HostError(Exception):
    """Shown to the admin as is."""


@dataclass
class Service:
    unit: str
    label: str
    purpose: str
    state: str                 # active, inactive, failed, activating, … ("unknown" if unreadable)
    sub_state: str
    since: datetime | None
    restartable: bool
    note: str | None = None    # why it cannot be restarted from here


def own_unit(cgroup: Path = Path("/proc/self/cgroup")) -> str | None:
    """The systemd service running this process (ggnet.service on an install), if any."""
    try:
        text = cgroup.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name = line.rsplit("/", 1)[-1]
        if name.endswith(".service") and _UNIT_RE.match(name):
            return name
    return None


class HostControl:
    def __init__(self, cfg: dict[str, Any], runner: CommandRunner | None = None,
                 unit: str | None = DETECT):
        """`unit`: the service running ggNet; by default read from this process's cgroup, None for none."""
        self.cfg = cfg
        self.runner = runner or CommandRunner(timeout=20)
        self.unit = own_unit() if unit is DETECT else unit

    # ── Services ──

    def _units(self) -> list[tuple[str, str, str, bool, str | None]]:
        """(unit, label, purpose, restartable, note) of every service ggNet depends on."""
        boot = self.cfg.get("boot") or {}
        units = []
        if self.unit:
            units.append((self.unit.removesuffix(".service"), "ggNet", "web UI, API, the PCs' boot and agent", True, None))
        units.append((str(boot.get("pxe_service") or "ggnet-pxe"), "Network boot",
                      "proxyDHCP and TFTP for PXE", True, None))
        db = str((self.cfg.get("paths") or {}).get("database", ""))
        if db.startswith("postgresql"):
            units.append(("postgresql", "Database", "PostgreSQL, ggNet's records", True, None))
        units.append((ISCSI_UNIT, "iSCSI target", "restores the PCs' disks (LIO) at boot", False,
                      "Restarting it would drop every PC's disk at once; it is never restarted from here."))
        return units

    def services(self) -> list[Service]:
        out = []
        for unit, label, purpose, restartable, note in self._units():
            ok, text, _ = self.runner.run(
                ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState",
                 "-p", "ActiveEnterTimestamp", "--timestamp=unix"], quiet=True)
            props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line) if ok else {}
            since = None
            stamp = props.get("ActiveEnterTimestamp", "").lstrip("@")
            if stamp.isdigit():
                since = datetime.fromtimestamp(int(stamp), timezone.utc)
            out.append(Service(unit=unit, label=label, purpose=purpose,
                               state=props.get("ActiveState") or "unknown",
                               sub_state=props.get("SubState") or "",
                               since=since, restartable=restartable, note=note))
        return out

    def restart(self, unit: str) -> bool:
        """Restart one of the services above; True when it is this ggNet (it goes down in a second)."""
        known = {u: r for u, _, _, r, _ in self._units()}
        if unit not in known:
            raise HostError(f"{unit} is not one of ggNet's services")
        if not known[unit]:
            raise HostError(f"{unit} is never restarted from the web UI: every PC would lose its disk")
        self_restart = self.unit is not None and unit == self.unit.removesuffix(".service")
        if self_restart:
            # From a separate unit, a second later: this answer still reaches the browser.
            cmd = ["systemd-run", "--on-active=2", "--collect", "--quiet",
                   f"--unit=ggnet-restart-{os.getpid()}", "/bin/systemctl", "restart", f"{unit}.service"]
        else:
            cmd = ["systemctl", "restart", f"{unit}.service"]
        ok, _, err = self.runner.run(cmd, timeout=90)
        if not ok:
            raise HostError(f"Restarting {unit} failed: {err.strip() or 'see journalctl -u ' + unit}")
        logger.warning("Service %s restarted from the web UI", unit)
        return self_restart

    # ── The server ──

    def running_guests(self) -> list[str]:
        """Proxmox VMs and containers that are running (a reboot stops them)."""
        guests = []
        # qm list: VMID NAME STATUS …; pct list: VMID Status [Lock] Name.
        for cmd, kind, status_at, name_at in ((["qm", "list"], "VM", 2, 1), (["pct", "list"], "CT", 1, -1)):
            ok, text, _ = self.runner.run(cmd, quiet=True)
            if not ok:
                continue
            for line in text.splitlines()[1:]:
                parts = line.split()
                if len(parts) > status_at and parts[status_at] == "running":
                    guests.append(f"{kind} {parts[0]} {parts[name_at]}")
        return guests

    def reboot(self, word: str) -> None:
        if word.strip() != REBOOT_WORD:
            raise HostError(f"Type {REBOOT_WORD} to reboot the server")
        ok, _, err = self.runner.run(["systemd-run", "--on-active=5", "--collect", "--quiet",
                                      "--unit=ggnet-reboot", "/bin/systemctl", "reboot"])
        if not ok:
            raise HostError(f"Cannot reboot the server: {err.strip()}")
        logger.warning("Server reboot requested from the web UI (in 5 s)")

    def overview(self) -> dict[str, Any]:
        """Host name, uptime, kernel and Proxmox VE version."""
        uptime = None
        try:
            uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
        ok, pve, _ = self.runner.run(["pveversion"], quiet=True)
        return {
            "hostname": socket.gethostname(),
            "uptime_seconds": uptime,
            "kernel": os.uname().release,
            "proxmox": pve.splitlines()[0].strip() if ok and pve else None,
        }
