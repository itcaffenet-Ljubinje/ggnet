"""CommandRunner: runs privileged commands (zfs, zpool, targetcli) on the host.

ggNet runs directly on the Proxmox host as root (see scripts/install_ggnet.sh),
so commands are executed locally, without ssh or sudo. Managers accept any
object with the same `run()` signature, which is how tests inject a fake.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger("ggnet.exec")

DEFAULT_TIMEOUT = 60


class CommandRunner:
    """Thin command executor; knows nothing about ZFS or iSCSI."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        # stderr of the last FAILED command, for API error messages,
        # since managers only return bool.
        self.last_error: str = ""

    def run(
        self,
        cmd: list[str],
        timeout: Optional[int] = None,
        quiet: bool = False,
    ) -> tuple[bool, str, str]:
        """
        Run a command. Returns (success, stdout, stderr).

        quiet=True suppresses error logging, for checks where failure is an
        expected outcome (e.g. "does this dataset exist"), not a real error.
        """
        timeout = timeout or self.timeout
        str_cmd = [str(c) for c in cmd]

        try:
            result = subprocess.run(
                str_cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            logger.error("Command timed out (%ss): %s", timeout, " ".join(str_cmd))
            self.last_error = f"timeout: {' '.join(str_cmd)}"
            return False, "", "timeout"
        except FileNotFoundError as e:
            logger.error("Executable not found: %s", e)
            self.last_error = str(e)
            return False, "", str(e)

        ok = result.returncode == 0
        if not ok:
            self.last_error = result.stderr.strip()
            if not quiet:
                logger.error(
                    "Command failed [%s]: %s", " ".join(str_cmd), result.stderr.strip()
                )
        return ok, result.stdout.strip(), result.stderr.strip()
