"""
Per-machine iSCSI traffic (ggRock's Sent, Received and Speed columns).

LIO counts the megabytes each initiator reads (Sent: server → PC) and writes
(Received: the PC's writeback) on its game disk. The counters live as long
as the ACL, so they restart at every new clone; a counter that goes down is
taken as such a restart. Speed is the change between two reads of the
counters (the UI asks every few seconds), so it has a resolution of 1 MB per
interval.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

MB = 1024 * 1024


@dataclass
class Traffic:
    sent_bytes: int
    received_bytes: int
    sent_bps: float | None       # None until there are two reads to compare
    received_bps: float | None


class TrafficMonitor:
    """Remembers the last counters per machine to turn them into speeds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: dict[int, tuple[float, int, int]] = {}
        self._lock = threading.Lock()

    def sample(self, machine_id: int, read_mb: int, write_mb: int) -> Traffic:
        now = self._clock()
        with self._lock:
            last = self._last.get(machine_id)
            self._last[machine_id] = (now, read_mb, write_mb)
        sent_bps = received_bps = None
        if last is not None:
            t, r, w = last
            elapsed = now - t
            if elapsed > 0 and read_mb >= r and write_mb >= w:   # else: the ACL was recreated
                sent_bps = (read_mb - r) * MB / elapsed
                received_bps = (write_mb - w) * MB / elapsed
        return Traffic(read_mb * MB, write_mb * MB, sent_bps, received_bps)

    def forget(self, keep: set[int]) -> None:
        """Drop machines that are gone or no longer mapped."""
        with self._lock:
            for machine_id in set(self._last) - keep:
                del self._last[machine_id]
