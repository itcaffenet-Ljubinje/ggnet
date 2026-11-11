"""
Virtual machines for working on images, on the Proxmox host ggNet runs on.

A VM is a machine like a PC (Boot Mode: an OS writeback and a game
writeback), but its writebacks are the VM's local drives instead of LUNs
on the shared iSCSI target: OS writeback = sata0, game writeback = sata1.
Everything else is the same as for a PC: the writebacks are discarded at
shutdown unless the VM keeps them, Apply writebacks makes them the images'
next versions, and the machine's Windows is personalized with its name.

The Provisioner reaches the VM's drives through VmDisks, which answers the
calls it makes on the iSCSI manager (attach, remove, session_active, ...)
for machines whose initiator IQN is a VM's (`<iqn_prefix>:vm-<vmid>`); a
VM is "connected" while it runs.

The VM itself is plain Proxmox (ProxmoxHost, `qm` and `pvesh`): OVMF with
Secure Boot keys enrolled, TPM 2.0, q35, an e1000e network card on a bridge
the admin chooses. ggNet never creates or changes bridges or storages; it
only lists the ones the host has. Every value that differs between hosts
(node name, VM ID, bridge, storage for the EFI and TPM state, cores,
memory) is read from the host or chosen in Settings.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Sequence

from sqlalchemy.orm import Session

from app.db.models import AppSetting

logger = logging.getLogger("ggnet.vms")

VM_TAG = "ggnet-vm"
VM_NAME_PREFIX = "ggnet-"
# Where the VM's "session" lives instead of an iSCSI target.
VM_TARGET = "proxmox-vm"
VM_IQN_RE = re.compile(r":vm-(\d+)$")
# Drive per writeback: the OS boots from sata0.
DRIVES = {"os": "sata0", "game": "sata1"}
# How long Windows may take to shut down when asked (then qm gives up; the VM keeps running).
SHUTDOWN_TIMEOUT = 600
# qm status of a VM that does not exist (removed outside ggNet).
MISSING = "missing"
ACTIONS = ("start", "shutdown", "stop", "reset", "reboot")
_STATUS = re.compile(r"^status:\s*(\w+)", re.MULTILINE)
_MAC = re.compile(r"=([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

SETTINGS_KEY = "vm_settings"


def vm_iqn(iqn_prefix: str, vmid: int) -> str:
    return f"{iqn_prefix}:vm-{vmid}"


def vmid_of(initiator_iqn: str | None) -> int | None:
    """The VM ID in a VM machine's IQN; None for a PC."""
    m = VM_IQN_RE.search(initiator_iqn or "")
    return int(m.group(1)) if m else None


# ── Settings ──────────────────────────────────────────────────────────

@dataclass
class VmSettings:
    # None: the bridge of ggNet's own interface, else the host's first.
    bridge: str | None = None
    # None: a ZFS storage on ggNet's pool, else the host's first for VM disks.
    storage: str | None = None
    # Memory the VMs may never take from the host (Proxmox, ZFS ARC, ggNet).
    reserved_memory_gb: int = 16


def load_settings(db: Session) -> VmSettings:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        return VmSettings()
    known = VmSettings.__dataclass_fields__
    return VmSettings(**{k: v for k, v in json.loads(row.value).items() if k in known})


def save_settings(db: Session, s: VmSettings) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(asdict(s))))
    else:
        row.value = json.dumps(asdict(s))
    db.commit()


# ── The Proxmox host ──────────────────────────────────────────────────

class ProxmoxHost:
    """`qm` and `pvesh` on the local Proxmox node."""

    def __init__(self, runner):
        self.runner = runner
        self._node: str | None = None
        # Runs a slow action (a Windows shutdown) without holding up the request.
        self.spawn: Callable[[Callable[[], None]], None] = _in_thread

    def _json(self, *args: str):
        ok, out, _ = self.runner.run(["pvesh", "get", *args, "--output-format", "json"], quiet=True)
        if not ok:
            return None
        try:
            return json.loads(out or "null")
        except ValueError:
            return None

    def node(self) -> str:
        """This host's node name (the cluster's view of it, else the host name)."""
        if self._node is None:
            status = self._json("/cluster/status") or []
            local = [s for s in status if isinstance(s, dict) and s.get("type") == "node" and s.get("local")]
            self._node = str(local[0]["name"]) if local else socket.gethostname().split(".")[0]
        return self._node

    def available(self) -> str | None:
        """None when VMs can be made here, else why not."""
        if self._json(f"/nodes/{self.node()}/status") is None:
            return f"This host is not a Proxmox VE node (or pvesh failed: {self.runner.last_error})"
        return None

    def resources(self) -> dict:
        """CPU threads and memory (MB) of the host."""
        s = self._json(f"/nodes/{self.node()}/status") or {}
        cpu = s.get("cpuinfo") or {}
        mem = s.get("memory") or {}
        return {"cpus": int(cpu.get("cpus") or 0), "memory_mb": int(mem.get("total") or 0) // 2**20}

    def bridges(self) -> list[dict]:
        rows = self._json(f"/nodes/{self.node()}/network", "--type", "any_bridge") or []
        return sorted(({"name": r["iface"], "cidr": r.get("cidr") or r.get("address"),
                        "ports": r.get("bridge_ports"), "active": bool(r.get("active")),
                        "comments": (r.get("comments") or "").strip() or None}
                       for r in rows if isinstance(r, dict) and r.get("iface")),
                      key=lambda b: b["name"])

    def storages(self) -> list[dict]:
        """Storages that hold VM disks here, with the ZFS pool of a zfspool storage."""
        rows = self._json(f"/nodes/{self.node()}/storage", "--content", "images", "--enabled", "1") or []
        pools = {s.get("storage"): s.get("pool") for s in (self._json("/storage") or [])
                 if isinstance(s, dict)}
        return sorted(({"name": r["storage"], "type": r.get("type"), "pool": pools.get(r["storage"]),
                        "avail": r.get("avail")}
                       for r in rows if isinstance(r, dict) and r.get("storage")),
                      key=lambda s: s["name"])

    def next_id(self) -> int | None:
        ok, out, _ = self.runner.run(["pvesh", "get", "/cluster/nextid"])
        try:
            return int(out.strip().strip('"')) if ok else None
        except ValueError:
            return None

    def create(self, vmid: int, name: str, cores: int, memory_mb: int, bridge: str, storage: str,
               netboot: bool = False) -> bool:
        """
        A VM shell: q35, OVMF with Secure Boot keys enrolled, TPM 2.0, an
        e1000e card. `netboot` boots it from the network (like a diskless PC,
        its writebacks on the shared iSCSI target); otherwise its writebacks
        are attached as local SATA drives and it boots from sata0.
        """
        return self.runner.run([
            "qm", "create", str(vmid), "--name", f"{VM_NAME_PREFIX}{name}"[:63],
            "--description", "ggNet virtual machine: its drives are ggNet writebacks. "
                              "Manage it in ggNet (Machines), not here.",
            "--tags", VM_TAG, "--ostype", "win11", "--machine", "q35", "--bios", "ovmf",
            "--efidisk0", f"{storage}:1,efitype=4m,pre-enrolled-keys=1",
            "--tpmstate0", f"{storage}:1,version=v2.0",
            "--cpu", "host", "--cores", str(cores), "--memory", str(memory_mb), "--balloon", "0",
            "--net0", f"e1000e,bridge={bridge}", "--vga", "std", "--onboot", "0",
            *(["--boot", "order=net0"] if netboot else []),
        ])[0]

    def config(self, vmid: int) -> dict | None:
        return self._json(f"/nodes/{self.node()}/qemu/{vmid}/config")

    def mac(self, vmid: int) -> str | None:
        net = (self.config(vmid) or {}).get("net0") or ""
        m = _MAC.search(net)
        return m.group(1).lower() if m else None

    def set_hardware(self, vmid: int, cores: int | None = None, memory_mb: int | None = None,
                     bridge: str | None = None) -> bool:
        args = ["qm", "set", str(vmid)]
        if cores is not None:
            args += ["--cores", str(cores)]
        if memory_mb is not None:
            args += ["--memory", str(memory_mb)]
        if bridge is not None:
            mac = self.mac(vmid)
            args += ["--net0", f"e1000e{'=' + mac.upper() if mac else ''},bridge={bridge}"]
        return len(args) == 3 or self.runner.run(args)[0]

    def rename(self, vmid: int, name: str) -> bool:
        return self.runner.run(["qm", "set", str(vmid), "--name", f"{VM_NAME_PREFIX}{name}"[:63]])[0]

    def status(self, vmid: int) -> str | None:
        """running / stopped / ..., MISSING when the VM does not exist, None when it cannot be read."""
        ok, out, err = self.runner.run(["qm", "status", str(vmid)], quiet=True)
        if not ok and "does not exist" in (err or ""):
            return MISSING
        m = _STATUS.search(out) if ok else None
        return m.group(1) if m else None

    def statuses(self) -> dict[int, dict]:
        """
        VM ID -> {status, cpus, memory_mb} of every VM, in one call. Proxmox
        updates the status here every few seconds; `status()` is live.
        """
        rows = self._json("/cluster/resources", "--type", "vm") or []
        return {int(r["vmid"]): {"status": r.get("status"), "cpus": int(r.get("maxcpu") or 0),
                                 "memory_mb": int(r.get("maxmem") or 0) // 2**20,
                                 "node": r.get("node")}
                for r in rows if isinstance(r, dict) and r.get("vmid") is not None}

    def action(self, vmid: int, action: str) -> bool:
        """
        start / stop (power off) / reset are done when this returns. shutdown
        and reboot ask Windows (ACPI) and are only started: Windows may take
        minutes, and `qm` waits for it (up to SHUTDOWN_TIMEOUT).
        """
        if action not in ACTIONS:
            raise ValueError(action)
        if action in ("start", "stop", "reset"):
            return self.runner.run(["qm", action, str(vmid)])[0]

        def slow() -> None:
            cmd = ["qm", action, str(vmid), "--timeout", str(SHUTDOWN_TIMEOUT)]
            if not self.runner.run(cmd, timeout=SHUTDOWN_TIMEOUT + 60)[0]:
                logger.warning("VM %s: %s did not finish: %s", vmid, action, self.runner.last_error)

        self.spawn(slow)
        return True

    def set_drives(self, vmid: int, drives: dict[str, str]) -> bool:
        args = ["qm", "set", str(vmid)]
        for drive, device in sorted(drives.items()):
            args += [f"--{drive}", f"{device},cache=none,discard=on,ssd=1"]
        if DRIVES["os"] in drives:
            args += ["--boot", f"order={DRIVES['os']}"]
        return self.runner.run(args)[0]

    def remove_drives(self, vmid: int) -> bool:
        """
        Detach the writebacks Proxmox only had attached (Local Drives); it never
        deletes them. A Network VM has none (its writebacks are iSCSI LUNs), so
        only the drives the VM really has are detached.
        """
        config = self.config(vmid) or {}
        drives = [d for d in DRIVES.values() if d in config]
        if not drives:
            return True
        return self.runner.run(["qm", "set", str(vmid), "--delete", ",".join(drives)])[0]

    def destroy(self, vmid: int) -> bool:
        """Stop the VM and remove it with its EFI and TPM state; detach the writebacks first."""
        state = self.status(vmid)
        if state == MISSING:
            return True                       # already gone
        if state != "stopped":
            self.runner.run(["qm", "stop", str(vmid), "--skiplock", "1"])
        if not self.remove_drives(vmid):
            return False
        return self.runner.run(["qm", "destroy", str(vmid), "--purge", "1", "--skiplock", "1"])[0]


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="ggnet-vm-action", daemon=True).start()


def default_bridge(bridges: list[dict], interface: str | None) -> str | None:
    """ggNet's own interface when it is a bridge, else the host's first bridge."""
    names = [b["name"] for b in bridges]
    if interface in names:
        return interface
    return names[0] if names else None


def default_storage(storages: list[dict], pool: str) -> str | None:
    """A ZFS storage on ggNet's pool, else the first one for VM disks."""
    for s in storages:
        if s.get("type") == "zfspool" and str(s.get("pool") or "").split("/")[0] == pool:
            return s["name"]
    return storages[0]["name"] if storages else None


# ── Console ───────────────────────────────────────────────────────────

CONSOLE_TICKET_SECONDS = 30
_PASSWORD_CHARS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class ConsoleTicket:
    vmid: int
    password: str          # the VM's VNC password for this one connection (VNC uses 8 characters)
    expires: float


class ConsoleTickets:
    """
    One-time tickets for the console: a signed-in admin asks for one, the
    browser opens the console websocket with it within CONSOLE_TICKET_SECONDS.
    The websocket itself cannot carry the sign-in, so the ticket is its proof.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._tickets: dict[str, ConsoleTicket] = {}
        self._lock = threading.Lock()

    def issue(self, vmid: int) -> tuple[str, str]:
        key = secrets.token_urlsafe(24)
        password = "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(8))
        now = self.clock()
        with self._lock:
            self._tickets = {k: t for k, t in self._tickets.items() if t.expires > now}
            self._tickets[key] = ConsoleTicket(vmid, password, now + CONSOLE_TICKET_SECONDS)
        return key, password

    def take(self, key: str) -> ConsoleTicket | None:
        with self._lock:
            t = self._tickets.pop(key, None)
        return t if t is not None and t.expires > self.clock() else None


def console_command(vmid: int) -> list[str]:
    """VNC of the VM on stdin/stdout; LC_PVE_TICKET (the ticket's password) is its password for 30 s."""
    return ["qm", "vncproxy", str(vmid)]


# ── The VM's drives in place of iSCSI LUNs ────────────────────────────

class VmDisks:
    """
    The part of the iSCSI manager the Provisioner uses, for VM machines:
    their writebacks become the VM's drives, and "logged in" means running.
    """

    def __init__(self, runner, host: ProxmoxHost | None = None):
        self.runner = runner
        self.host = host or ProxmoxHost(runner)

    @staticmethod
    def target_iqn() -> str:
        return VM_TARGET

    @staticmethod
    def backstore_name(machine_id: str, slot: str) -> str:
        return f"{machine_id}-{slot}"

    def attach(self, machine_id: str, initiator_iqn: str, disks: Sequence, fresh: bool = False) -> str | None:
        vmid = vmid_of(initiator_iqn)
        if vmid is None:
            return None
        drives = {DRIVES[d.slot]: d.device for d in disks}
        if not fresh and not self.host.remove_drives(vmid):
            return None
        return VM_TARGET if self.host.set_drives(vmid, drives) else None

    def remove(self, machine_id: str, initiator_iqn: str, save: bool = True,
               legacy: bool = True) -> set[str] | None:
        vmid = vmid_of(initiator_iqn)
        if vmid is None:
            return None
        if self.host.status(vmid) == MISSING:
            return set()                      # no VM, nothing attached
        return set() if self.host.remove_drives(vmid) else None

    def session_active(self, initiator_iqn: str, target_iqn: str | None = None) -> bool | None:
        vmid = vmid_of(initiator_iqn)
        status = self.host.status(vmid) if vmid is not None else None
        if status is None:
            return None
        return status not in ("stopped", MISSING)
