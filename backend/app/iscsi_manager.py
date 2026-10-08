"""
ISCSIManager: manages the LIO iSCSI target through targetcli.

ggNet uses ONE shared target for all clients (like ggRock), not a target per
client:

    <iqn_prefix>:<target_name>            e.g. iqn.2025-05.net.ggnet:storage
      tpg1
        portals   <service IP>:3260       (never LIO's default 0.0.0.0)
        luns      lun0 → block/pc01-game, lun1 → block/pc02-game, ...
        acls      one ACL per client INITIATOR IQN, each with its own
                  mapped LUNs: mapped_lun0 → that client's TPG LUN only

So every client logs in to the same target IQN and sees only its own disks.
Adding, resetting or removing one client touches only its ACL, its TPG LUNs
and its backstores; the target and the other clients' sessions are never
touched.

Every new TPG LUN and ACL is created with `add_mapped_luns=false`. Without it,
targetcli's default (auto_add_mapped_luns) would map each new client's disk
into EVERY existing ACL, i.e. every PC would see every other PC's disk.

A slot is one disk of a machine and the backstore is named `<machine>-<slot>`:
Disk Mode uses slot `game` (mapped LUN 0); Boot Mode will use `os` (mapped
LUN 0) and `game` (mapped LUN 1).

Before this design each client had its own target `<iqn_prefix>:client-<id>`
with backstore `client-<id>`. detach() also removes those, so resetting or
deprovisioning a machine migrates it to the shared target.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.config import get_config
from app.runner import CommandRunner

logger = logging.getLogger("ggnet.iscsi")

DEFAULT_IQN_PREFIX = "iqn.2025-05.net.ggnet"
DEFAULT_TARGET_NAME = "storage"

# Slots a machine can have, in the order of their mapped LUN numbers in Boot Mode.
SLOTS = ("os", "game")

# iqn.YYYY-MM.<reversed domain>, RFC 3720. Lowercase, no ':' (target_iqn()
# adds the target suffix).
_IQN_PREFIX_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*$")

# A full initiator IQN, e.g. iqn.1991-05.com.microsoft:pc01.
_INITIATOR_IQN_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.:-]*)?$")

# targetcli re-parses its arguments as a command line, so names that end up
# in a target path must not contain spaces, '/', or start with '-'.
_MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TARGET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_BLOCK_DEVICE_RE = re.compile(r"^/dev/zvol/[A-Za-z0-9_][A-Za-z0-9_.:/-]*$")

# targetcli `ls` lines, e.g. "  o- lun3 ..... [block/pc01-game (/dev/zvol/...) ...]"
_LUN_LINE_RE = re.compile(r"o- lun(\d+)\b")
_NODE_LINE_RE = re.compile(r"^\s*o- (\S+)")


def parse_portal(portal: str) -> tuple[str, int]:
    """
    Split "IP:port" (or "[IPv6]:port") into (ip, port). Raises ValueError for
    anything that is not a specific IP address with a valid port; the
    wildcard addresses (0.0.0.0, ::) are rejected on purpose.
    """
    host, sep, port_s = str(portal).rpartition(":")
    if not sep or not host or not port_s.isdigit():
        raise ValueError(f"iSCSI portal must be IP:port, got {portal!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"iSCSI portal host must be an IP address, got {portal!r}") from None
    if ip.is_unspecified:
        raise ValueError(f"iSCSI portal must be a specific IP, not {ip}")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid iSCSI portal port: {port}")
    return str(ip), port


@dataclass(frozen=True)
class ISCSIConfig:
    portal: str                           # services.iscsi_portal, "IP:port"
    iqn_prefix: str = DEFAULT_IQN_PREFIX
    target_name: str = DEFAULT_TARGET_NAME

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]] = None) -> "ISCSIConfig":
        services = (cfg if cfg is not None else get_config()).get("services") or {}
        if not services.get("iscsi_portal"):
            raise ValueError("config.toml is missing services.iscsi_portal")
        return cls(
            portal=str(services["iscsi_portal"]),
            iqn_prefix=str(services.get("iscsi_iqn_prefix") or DEFAULT_IQN_PREFIX),
            target_name=str(services.get("iscsi_target_name") or DEFAULT_TARGET_NAME),
        )


@dataclass(frozen=True)
class Disk:
    """One disk to expose to a machine: its slot and its zvol device."""

    slot: str            # one of SLOTS
    device: str          # /dev/zvol/...


class ISCSIManager:
    def __init__(
        self,
        config: Optional[ISCSIConfig] = None,
        runner: Optional[CommandRunner] = None,
    ):
        self.cfg = config or ISCSIConfig.from_config()
        if not _IQN_PREFIX_RE.match(self.cfg.iqn_prefix):
            raise ValueError(f"Invalid IQN prefix: {self.cfg.iqn_prefix!r}")
        if not _TARGET_NAME_RE.match(self.cfg.target_name):
            raise ValueError(f"Invalid iSCSI target name: {self.cfg.target_name!r}")
        self.portal_ip, self.portal_port = parse_portal(self.cfg.portal)
        self.runner = runner or CommandRunner()

    def _run(self, cmd: list[str], quiet: bool = False) -> tuple[bool, str, str]:
        return self.runner.run(cmd, quiet=quiet)

    # ── Names ─────────────────────────────────────────────────────────

    def target_iqn(self) -> str:
        """The one shared target every client logs in to."""
        return f"{self.cfg.iqn_prefix}:{self.cfg.target_name}"

    def legacy_target_iqn(self, machine_id: str) -> str:
        """Per-client target of the old design; only ever deleted."""
        return f"{self.cfg.iqn_prefix}:client-{machine_id}"

    @staticmethod
    def backstore_name(machine_id: str, slot: str) -> str:
        return f"{machine_id}-{slot}"

    @property
    def _tpg(self) -> str:
        return f"/iscsi/{self.target_iqn()}/tpg1"

    # ── Validation (invalid input sends no command) ───────────────────

    @staticmethod
    def _valid_machine_id(machine_id: str) -> bool:
        if isinstance(machine_id, str) and _MACHINE_ID_RE.match(machine_id):
            return True
        logger.error("Rejected invalid machine id: %r", machine_id)
        return False

    @staticmethod
    def _valid_initiator(iqn: str) -> bool:
        if isinstance(iqn, str) and _INITIATOR_IQN_RE.match(iqn):
            return True
        logger.error("Rejected invalid initiator IQN: %r", iqn)
        return False

    @staticmethod
    def _valid_disks(disks: Sequence[Disk]) -> bool:
        slots = [d.slot for d in disks]
        if not disks or len(set(slots)) != len(slots):
            logger.error("Rejected disk list: %r", disks)
            return False
        for d in disks:
            if d.slot not in SLOTS:
                logger.error("Rejected unknown slot: %r", d.slot)
                return False
            if not isinstance(d.device, str) or not _BLOCK_DEVICE_RE.match(d.device) \
                    or "/../" in d.device or d.device.endswith("/.."):
                logger.error("Rejected invalid block device: %r", d.device)
                return False
        return True

    # ── Listings ──────────────────────────────────────────────────────

    def list_targets(self) -> str:
        ok, out, _ = self._run(["targetcli", "/iscsi", "ls"])
        return out if ok else ""

    def target_exists(self) -> Optional[bool]:
        """Exact match on the shared target IQN; None if targetcli cannot list."""
        ok, out, _ = self._run(["targetcli", "/iscsi", "ls", "depth=1"])
        if not ok:
            return None
        return self._has_node(out, self.target_iqn())

    def backstore_exists(self, machine_id: str, slot: str) -> Optional[bool]:
        """Exact match (pc1-game must not match pc10-game); None if targetcli cannot list."""
        ok, out, _ = self._run(["targetcli", "/backstores/block", "ls", "depth=1"])
        if not ok:
            return None
        return self._has_node(out, self.backstore_name(machine_id, slot))

    @staticmethod
    def _has_node(listing: str, name: str) -> bool:
        for line in listing.splitlines():
            m = _NODE_LINE_RE.match(line)
            if m and m.group(1) == name:
                return True
        return False

    def _used_tpg_luns(self) -> Optional[set[int]]:
        ok, out, _ = self._run(["targetcli", f"{self._tpg}/luns", "ls", "depth=1"])
        if not ok:
            return None
        return {int(n) for n in _LUN_LINE_RE.findall(out)}

    # ── Shared target ─────────────────────────────────────────────────

    def ensure_target(self) -> bool:
        """
        Create the shared target if it does not exist: target → portal on
        the service IP only → ACL-only access. Safe to call repeatedly; an
        existing target is left exactly as it is.
        """
        exists = self.target_exists()
        if exists is None:
            logger.error("targetcli cannot list targets; not touching the shared target")
            return False
        if exists:
            return True

        iqn = self.target_iqn()
        portals = f"{self._tpg}/portals"
        steps = [
            ["targetcli", "/iscsi", "create", iqn],
            # LIO adds a 0.0.0.0:3260 portal to new targets by default
            # (auto_add_default_portal). Remove it so the target listens only
            # on the service IP; failure just means there was none.
            ["targetcli", portals, "delete", "0.0.0.0", "3260"],
            ["targetcli", portals, "create", self.portal_ip, str(self.portal_port)],
            # Only initiators with an ACL may log in (no demo mode).
            # CHAP is off in v1; the ACL is the only access control.
            ["targetcli", self._tpg, "set", "attribute",
             "generate_node_acls=0", "authentication=0"],
        ]
        optional = steps[1]
        for step in steps:
            ok, _, err = self._run(step, quiet=step is optional)
            if not ok and step is not optional:
                logger.error("Creating the shared target failed at %s: %s", step, err)
                # The target is new and has no clients yet, so removing it is safe.
                self._run(["targetcli", "/iscsi", "delete", iqn], quiet=True)
                self.runner.last_error = err
                return False
        self.save_config()
        logger.info("Shared iSCSI target %s created on %s:%s", iqn, self.portal_ip, self.portal_port)
        return True

    # ── Per-machine attach / detach ───────────────────────────────────

    def attach(self, machine_id: str, initiator_iqn: str, disks: Sequence[Disk]) -> Optional[str]:
        """
        Expose `disks` to one machine on the shared target:
        backstore → TPG LUN (not mapped to anyone) → ACL for the machine's
        initiator → mapped LUN 0, 1, ... in the order of `disks`.

        Leftovers of this machine (an earlier failed attach, the old
        per-client target) are removed first. Returns the target IQN, or None
        after cleaning up whatever this call created. Invalid input sends no
        command.
        """
        if not (self._valid_machine_id(machine_id) and self._valid_initiator(initiator_iqn)
                and self._valid_disks(disks)):
            return None
        if not self.ensure_target():
            return None
        self.detach(machine_id, initiator_iqn, save=False)

        used = self._used_tpg_luns()
        if used is None:
            self.runner.last_error = self.runner.last_error or "targetcli cannot list LUNs"
            return None

        acl = f"{self._tpg}/acls/{initiator_iqn}"
        steps: list[list[str]] = []
        tpg_luns: list[int] = []
        for disk in disks:
            name = self.backstore_name(machine_id, disk.slot)
            index = next(i for i in range(len(used) + len(disks) + 1)
                         if i not in used and i not in tpg_luns)
            tpg_luns.append(index)
            steps += [
                ["targetcli", "/backstores/block", "create", f"name={name}", f"dev={disk.device}"],
                ["targetcli", f"{self._tpg}/luns", "create", f"/backstores/block/{name}",
                 f"lun={index}", "add_mapped_luns=false"],
            ]
        steps.append(["targetcli", f"{self._tpg}/acls", "create", initiator_iqn,
                      "add_mapped_luns=false"])
        for mapped, index in enumerate(tpg_luns):
            steps.append(["targetcli", acl, "create", f"mapped_lun={mapped}",
                          f"tpg_lun_or_backstore=lun{index}", "write_protect=false"])

        for step in steps:
            ok, _, err = self._run(step)
            if not ok:
                logger.error("iSCSI attach of %s failed at %s: %s", machine_id, step, err)
                self.detach(machine_id, initiator_iqn)
                # Cleanup is expected to fail on parts that do not exist;
                # keep the REAL failure reason for the caller.
                self.runner.last_error = err
                return None

        self.save_config()
        logger.info(
            "iSCSI: %s (ACL %s) -> %s",
            machine_id, initiator_iqn,
            ", ".join(f"LUN {i}={d.device}" for i, d in enumerate(disks)),
        )
        return self.target_iqn()

    def detach(self, machine_id: str, initiator_iqn: str, save: bool = True) -> bool:
        """
        Remove everything of one machine: its ACL (with its mapped LUNs), its
        backstores (LIO removes their TPG LUNs with them) and the old
        per-client target. The shared target is never deleted.

        Does not fail if parts are already gone, so the return value is NOT
        proof that they were removed; use backstore_exists() for that.
        Returns False only for invalid input (no command is sent).
        """
        if not (self._valid_machine_id(machine_id) and self._valid_initiator(initiator_iqn)):
            return False
        self._run(["targetcli", f"{self._tpg}/acls", "delete", initiator_iqn], quiet=True)
        for slot in SLOTS:
            self._run(["targetcli", "/backstores/block", "delete",
                       self.backstore_name(machine_id, slot)], quiet=True)
        # Old design: one target per client.
        self._run(["targetcli", "/iscsi", "delete", self.legacy_target_iqn(machine_id)], quiet=True)
        self._run(["targetcli", "/backstores/block", "delete", f"client-{machine_id}"], quiet=True)
        if save:
            self.save_config()
        return True

    def save_config(self) -> bool:
        """Persist the configuration so it survives a server reboot."""
        ok, _, _ = self._run(["targetcli", "saveconfig"])
        return ok


if __name__ == "__main__":
    # Manual smoke test (runs `targetcli /iscsi ls` on the host): python -m app.iscsi_manager
    logging.basicConfig(level=logging.INFO)
    print(ISCSIManager().list_targets())
