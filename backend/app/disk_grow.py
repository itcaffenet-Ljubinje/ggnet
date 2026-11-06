"""
Grow the data partition of a disk image after its zvol got bigger, offline on
the server, so Windows sees the new space without Disk Management.

A game image ends with its data partition: it is grown to the end of the disk.
An OS image ends with its Recovery partition right behind C:, so Windows
could not extend C: either. The Recovery partition is moved to the end of the
disk first (its data copied and compared, same GUID, type, name and
attributes, so Windows still finds WinRE), then C: is grown up to it:

  sgdisk -e <dev>                        backup GPT to the new end of the disk
  sfdisk --json <dev>                    partitions, read back
  dd ... ; cmp ...                       Recovery copied to the end, compared
  sgdisk -d .. -n .. -t .. -u .. -c .. -A ..   the partition(s) at their new place
  blockdev --rereadpt <dev>; udevadm settle
  ntfsresize --no-action --force <part>  check, then the real run
  ntfsresize --force <part>

The disk must not be in use: the caller grows a fresh clone. Until the
partition table is rewritten nothing on the disk changes but free space.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger("ggnet.grow")

BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
RECOVERY = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
SECTOR = 512
ALIGN = 2048                 # 1 MiB, as Windows aligns partitions
# Less free space than this behind the last partition is not worth a resize.
MIN_FREE_SECTORS = ALIGN
# A gap between C: and Recovery bigger than this is not the usual layout.
MAX_GAP_SECTORS = 16 * ALIGN
_PART_NODE = re.compile(r"p(\d+)$")
# sfdisk's names for GPT attribute bits 0-2; others are listed as GUID:<bit>,...
_ATTR_BITS = {"RequiredPartition": 0, "NoBlockIOProtocol": 1, "LegacyBIOSBootable": 2}


def _real_device(path: str) -> str:
    return os.path.realpath(path)


@dataclass
class GrowResult:
    extended: bool
    note: str        # what was done, or why the partition was left as it is


@dataclass
class _Part:
    number: int
    start: int
    size: int
    type: str
    uuid: str
    name: str | None
    attr_bits: list[int]

    @property
    def end(self) -> int:
        return self.start + self.size - 1

    def create_args(self, start: int, end: int) -> list[str]:
        """sgdisk options that recreate this partition from `start` to `end` (0: the end of the disk)."""
        n = self.number
        args = ["-n", f"{n}:{start}:{end}", "-t", f"{n}:{self.type}", "-u", f"{n}:{self.uuid}"]
        if self.name:
            args += ["-c", f"{n}:{self.name}"]
        for bit in self.attr_bits:
            args += ["-A", f"{n}:set:{bit}"]
        return args


def _attr_bits(attrs: str | None) -> list[int] | None:
    """GPT attribute bits from sfdisk's text, or None when it is not understood."""
    bits: list[int] = []
    for token in (attrs or "").split():
        if token in _ATTR_BITS:
            bits.append(_ATTR_BITS[token])
        elif token.startswith("GUID:") and all(b.isdigit() for b in token[5:].split(",")):
            bits += [int(b) for b in token[5:].split(",")]
        else:
            return None
    return sorted(bits)


class PartitionGrower:
    def __init__(self, runner):
        self.runner = runner

    def _run(self, cmd: list[str], timeout: int | None = None) -> tuple[bool, str, str]:
        return self.runner.run(cmd, timeout=timeout)

    def _parts(self, device: str) -> tuple[list[_Part], int] | str:
        ok, out, err = self._run(["sfdisk", "--json", device])
        if not ok:
            return f"the partition table could not be read: {err}"
        try:
            table = json.loads(out)["partitiontable"]
            if table.get("label") != "gpt" or not table.get("partitions"):
                return "the disk has no GPT partitions"
            parts = []
            for p in table["partitions"]:
                m = _PART_NODE.search(str(p.get("node", "")))
                bits = _attr_bits(p.get("attrs"))
                if m is None or bits is None:
                    return f"unexpected partition entry {p.get('node')!r} ({p.get('attrs')})"
                parts.append(_Part(int(m.group(1)), int(p["start"]), int(p["size"]),
                                   str(p["type"]).upper(), str(p["uuid"]), p.get("name"), bits))
            return sorted(parts, key=lambda p: p.start), int(table["lastlba"])
        except (ValueError, KeyError, TypeError) as e:
            return f"the partition table could not be read: {e}"

    def extend_last(self, device: str) -> GrowResult:
        """Grow the disk's data partition (and its NTFS) into the new space at its end."""
        # /dev/zvol/<pool>/... is a link to /dev/zdN, whose partitions are /dev/zdNpK.
        device = _real_device(device)
        ok, _, err = self._run(["sgdisk", "-e", device])
        if not ok:
            return GrowResult(False, f"the partition table could not be moved to the new end: {err}")
        read = self._parts(device)
        if isinstance(read, str):
            return GrowResult(False, read)
        parts, last_lba = read
        last = parts[-1]
        if last_lba - last.end < MIN_FREE_SECTORS:
            return GrowResult(False, "there is no new space behind the last partition")

        if last.type == BASIC_DATA:
            data, table_args = last, last.create_args(last.start, 0)
            moved = ""
        elif last.type == RECOVERY and len(parts) > 1 and parts[-2].type == BASIC_DATA \
                and last.start - parts[-2].end <= MAX_GAP_SECTORS:
            data, rec = parts[-2], last
            new_start = (last_lba + 1 - rec.size) // ALIGN * ALIGN
            if new_start <= rec.end:
                return GrowResult(False, "the new space is too small to move the Recovery partition behind it")
            problem = self._copy(device, rec, new_start)
            if problem:
                return GrowResult(False, problem)
            table_args = (["-d", str(rec.number)] + rec.create_args(new_start, new_start + rec.size - 1)
                          + data.create_args(data.start, new_start - 1))
            moved = "the Recovery partition was moved to the end and "
        else:
            return GrowResult(False, "the last partition is neither a data partition nor Recovery behind one; "
                                     "extend the volume in Windows instead")

        ok, _, err = self._run(["sgdisk", "-d", str(data.number)] + table_args + [device])
        if not ok:
            return GrowResult(False, f"the partitions could not be rewritten: {err}")
        self._run(["blockdev", "--rereadpt", device])
        self._run(["udevadm", "settle"])

        part = f"{device}p{data.number}"
        ok, _, err = self._run(["ntfsresize", "--no-action", "--force", part], timeout=600)
        if not ok:
            return GrowResult(False, f"the partition is bigger, but NTFS refused the check: {err}")
        ok, _, err = self._run(["ntfsresize", "--force", part], timeout=3600)
        if not ok:
            return GrowResult(False, f"the partition is bigger, but NTFS could not be grown: {err}")
        logger.info("Partition %s of %s and its NTFS grown%s", data.number, device,
                    " (Recovery moved to the end)" if moved else "")
        return GrowResult(True, f"{moved}the data partition was extended to the new size")

    def _copy(self, device: str, part: _Part, new_start: int) -> str | None:
        """Copy a partition's data to `new_start` (free space) and compare; why not, or None."""
        src, dst, size = part.start * SECTOR, new_start * SECTOR, part.size * SECTOR
        ok, _, err = self._run([
            "dd", f"if={device}", f"of={device}", "bs=4M",
            f"skip={src}", f"seek={dst}", f"count={size}",
            "iflag=skip_bytes,count_bytes", "oflag=seek_bytes", "conv=notrunc,fsync", "status=none",
        ], timeout=3600)
        if not ok:
            return f"the Recovery partition could not be copied: {err}"
        ok, _, err = self._run(["cmp", "-n", str(size), "-i", f"{src}:{dst}", device, device], timeout=3600)
        if not ok:
            return f"the copy of the Recovery partition differs from the original: {err or 'mismatch'}"
        return None
