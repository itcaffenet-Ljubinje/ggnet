"""
Unfinished Windows updates in OS image versions.

A Windows update that was installed but still waits for a restart (Windows
finishes it at the next boot: "Working on updates", TiWorker) must not end
up in an image version: every PC would redo it on every boot, as its
writeback is reset, and its writeback would grow by hundreds of MB each
time. So:

  * Apply Writebacks checks the PC's OS writeback first (app.api.v1.machines)
    and asks to restart the PC once more instead;
  * every OS image's active version is checked once in the background
    (UpdateChecker) and the result kept on the version as a ZFS property
    (ZFSManager.set_snapshot_update): Images shows "Update pending".

What counts as a sign: app.windows_setup.update_signs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.updates")

# A version that could not be read is tried again after this long.
RETRY_SECONDS = 6 * 3600

# The state of one version, as the API reports it.
NONE, PENDING, CHECKING, UNKNOWN = "none", "pending", "checking", "unknown"


class UpdateChecker:
    """Checks versions one at a time in a background thread; one per process."""

    def __init__(self, prov: Provisioner, background: bool = True):
        self.prov = prov
        self.background = background
        self._queue: deque[tuple[str, str]] = deque()
        self._failed: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def state(self, master: str, snapshot: str) -> tuple[str, list[str], str | None]:
        """(NONE | PENDING | CHECKING | UNKNOWN, the signs, why unknown); queues an unchecked version."""
        signs = self.prov.zfs.snapshot_update(f"{master}@{snapshot}")
        if signs is not None:
            return (PENDING if signs else NONE), signs, None
        key = (master, snapshot)
        with self._lock:
            failed = self._failed.get(key)
            if failed is not None and time.monotonic() - failed[0] < RETRY_SECONDS:
                return UNKNOWN, [], failed[1]
            if key not in self._queue:
                self._queue.append(key)
            self._start()
        return CHECKING, [], None

    def _start(self) -> None:
        if not self.background:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.run, name="ggnet-update-check", daemon=True)
            self._thread.start()

    def run(self) -> None:
        """Check every queued version (the thread's body; tests call it directly)."""
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                master, snapshot = self._queue[0]
            try:
                found = self.prov.check_update(master, snapshot)
            except Exception as e:
                logger.exception("Checking %s@%s for an unfinished Windows update failed", master, snapshot)
                found = f"unexpected error: {e}"
            with self._lock:
                self._queue.popleft()
                if isinstance(found, str):
                    self._failed[(master, snapshot)] = (time.monotonic(), found)
                    logger.warning("%s@%s: cannot check for an unfinished Windows update: %s",
                                   master, snapshot, found)
                else:
                    self._failed.pop((master, snapshot), None)
                    if found:
                        logger.warning("%s@%s has an unfinished Windows update (%s)",
                                       master, snapshot, ", ".join(found))
