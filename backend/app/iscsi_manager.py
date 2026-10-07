"""
ISCSIManager: manages iSCSI targets through LIO/targetcli.

Each client (Disk Mode) gets its own iSCSI target that exposes exactly one
zvol (its writeback clone). The ACL is bound to the client's INITIATOR IQN,
not its MAC address. The MAC is for DHCP/WoL; the iSCSI initiator has its own
IQN (Windows: iqn.1991-05.com.microsoft:<pc-name>, lowercase).

Target names follow `<iqn_prefix>:client-<machine_id>`. The prefix comes from
`services.iscsi_iqn_prefix` in config.toml (app.config.get_config).

Each target listens only on `services.iscsi_portal` (the service IP chosen at
install time), never on LIO's default 0.0.0.0:3260 portal.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.config import get_config
from app.runner import CommandRunner

logger = logging.getLogger("ggnet.iscsi")

DEFAULT_IQN_PREFIX = "iqn.2025-05.net.ggnet"

# iqn.YYYY-MM.<reversed domain>, RFC 3720. Lowercase, no ':' (target_iqn()
# adds the target suffix).
_IQN_PREFIX_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*$")

# A full initiator IQN, e.g. iqn.1991-05.com.microsoft:pc01.
_INITIATOR_IQN_RE = re.compile(r"^iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*(:[a-z0-9][a-z0-9.:-]*)?$")

# targetcli re-parses its arguments as a command line, so names that end up
# in a target path must not contain spaces, '/', or start with '-'.
_MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_BLOCK_DEVICE_RE = re.compile(r"^/dev/zvol/[A-Za-z0-9_][A-Za-z0-9_.:/-]*$")


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

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]] = None) -> "ISCSIConfig":
        services = (cfg if cfg is not None else get_config()).get("services") or {}
        if not services.get("iscsi_portal"):
            raise ValueError("config.toml is missing services.iscsi_portal")
        return cls(
            portal=str(services["iscsi_portal"]),
            iqn_prefix=str(services.get("iscsi_iqn_prefix") or DEFAULT_IQN_PREFIX),
        )


class ISCSIManager:
    def __init__(
        self,
        config: Optional[ISCSIConfig] = None,
        runner: Optional[CommandRunner] = None,
    ):
        self.cfg = config or ISCSIConfig.from_config()
        if not _IQN_PREFIX_RE.match(self.cfg.iqn_prefix):
            raise ValueError(f"Invalid IQN prefix: {self.cfg.iqn_prefix!r}")
        self.portal_ip, self.portal_port = parse_portal(self.cfg.portal)
        self.runner = runner or CommandRunner()

    def _run(self, cmd: list[str], quiet: bool = False) -> tuple[bool, str, str]:
        return self.runner.run(cmd, quiet=quiet)

    def target_iqn(self, machine_id: str) -> str:
        return f"{self.cfg.iqn_prefix}:client-{machine_id}"

    @staticmethod
    def _valid_machine_id(machine_id: str) -> bool:
        if isinstance(machine_id, str) and _MACHINE_ID_RE.match(machine_id):
            return True
        logger.error("Rejected invalid machine id: %r", machine_id)
        return False

    # ── CRUD ──────────────────────────────────────────────────────────

    def create_target(
        self,
        machine_id: str,
        block_device: str,
        initiator_iqn: str,
    ) -> Optional[str]:
        """
        Create a complete iSCSI target for one client:
        block backstore → target IQN → portal on the service IP → LUN →
        ACL bound to that machine's initiator IQN.

        Returns the target IQN, or None on error (after cleaning up any
        partially created state). Invalid input sends no command.
        """
        if not self._valid_machine_id(machine_id):
            return None
        if not isinstance(block_device, str) or not _BLOCK_DEVICE_RE.match(block_device) \
                or "/../" in block_device or block_device.endswith("/.."):
            logger.error("Rejected invalid block device: %r", block_device)
            return None
        if not isinstance(initiator_iqn, str) or not _INITIATOR_IQN_RE.match(initiator_iqn):
            logger.error("Rejected invalid initiator IQN: %r", initiator_iqn)
            return None

        iqn = self.target_iqn(machine_id)
        backstore_name = f"client-{machine_id}"

        portals = f"/iscsi/{iqn}/tpg1/portals"
        steps = [
            ["targetcli", "/backstores/block", "create",
             f"name={backstore_name}", f"dev={block_device}"],

            ["targetcli", "/iscsi", "create", iqn],

            # LIO adds a 0.0.0.0:3260 portal to new targets by default
            # (auto_add_default_portal). Remove it so the target listens only
            # on the service IP; failure just means there was none.
            ["targetcli", portals, "delete", "0.0.0.0", "3260"],

            ["targetcli", portals, "create", self.portal_ip, str(self.portal_port)],

            ["targetcli", f"/iscsi/{iqn}/tpg1/luns", "create",
             f"/backstores/block/{backstore_name}"],

            ["targetcli", f"/iscsi/{iqn}/tpg1/acls", "create", initiator_iqn],

            # Only the registered initiator may connect, no demo mode.
            # CHAP is off in v1; the ACL is the only access control.
            ["targetcli", f"/iscsi/{iqn}/tpg1", "set", "attribute",
             "generate_node_acls=0", "authentication=0"],
        ]

        optional = steps[2]
        for step in steps:
            ok, _, err = self._run(step, quiet=step is optional)
            if not ok and step is not optional:
                logger.error("iSCSI provisioning failed at step %s: %s", step, err)
                self.delete_target(machine_id)
                # Cleanup is expected to fail on parts that do not exist;
                # keep the REAL failure reason for the caller.
                self.runner.last_error = err
                return None

        self.save_config()
        logger.info(
            "iSCSI target created: %s -> %s on %s:%s (ACL: %s)",
            iqn, block_device, self.portal_ip, self.portal_port, initiator_iqn,
        )
        return iqn

    def delete_target(self, machine_id: str) -> bool:
        """
        Delete the target and its backstore. Does not fail if they are
        already gone, so the return value is NOT proof that they were deleted.
        Returns False only for an invalid machine id (no command is sent).
        """
        if not self._valid_machine_id(machine_id):
            return False
        iqn = self.target_iqn(machine_id)
        backstore_name = f"client-{machine_id}"

        self._run(["targetcli", "/iscsi", "delete", iqn])
        self._run(["targetcli", "/backstores/block", "delete", backstore_name])
        self.save_config()
        return True

    def list_targets(self) -> str:
        ok, out, _ = self._run(["targetcli", "/iscsi", "ls"])
        return out if ok else ""

    def target_status(self, machine_id: str) -> str:
        if not self._valid_machine_id(machine_id):
            return ""
        iqn = self.target_iqn(machine_id)
        ok, out, err = self._run(["targetcli", f"/iscsi/{iqn}", "status"])
        return out if ok else err

    def save_config(self) -> bool:
        """Persist the configuration so it survives a server reboot."""
        ok, _, _ = self._run(["targetcli", "saveconfig"])
        return ok


if __name__ == "__main__":
    # Manual smoke test (runs `targetcli /iscsi ls` on the host): python -m app.iscsi_manager
    logging.basicConfig(level=logging.INFO)
    print(ISCSIManager().list_targets())
