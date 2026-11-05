"""
Import of an image file as a new master (Images -> Import).

Where the files come from:
  - the imports folder: the mountpoint of storage.imports (<pool>/ggnet/imports,
    on the pool because a 100 GB VHDX would fill the Proxmox system disk),
    filled over the network or by an upload from the browser;
  - a USB disk plugged into the server: only USB disks are offered, never
    one that holds a pool, LVM or a mounted file system, and it is mounted
    read-only under /run/ggnet/usb/<partition> until it is ejected.

The import itself (DiskImporter) runs in the background like a copy
(app.services.copies), with the same database fields: the new disk's
`copy_source` is the file's path, `copy_percent` its progress and
`copy_error` why it failed. The zvol is filled with

    qemu-img convert -p -n --target-is-zero -f <format> -O raw <file> <zvol>

which skips the blocks that are empty, so the zvol only holds the data, and
is published at @base when done. An OS image is then prepared for every PC
(Prepare image, optionally with Defender off) as its next version, so it is
ready to boot. A running import can be cancelled (cancel()); recover() in
copies covers an import that ran when ggNet stopped.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DiskKind, GameDisk
from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.imports")

# Image file extensions and qemu-img's name for their format.
FORMATS = {".vhd": "vpc", ".vhdx": "vhdx", ".qcow2": "qcow2", ".vmdk": "vmdk", ".raw": "raw", ".img": "raw"}
USB_MOUNTS = Path("/run/ggnet/usb")
# File systems a USB disk with image files may have; read-only mounts.
READABLE_FS = {"ntfs", "exfat", "vfat", "ext4", "ext3", "ext2", "xfs", "btrfs"}
# How deep the image files are looked for in a folder or on a USB disk.
MAX_DEPTH = 4
# The database is updated at most this often with the progress.
PROGRESS_EVERY = 2.0
IMPORT_TIMEOUT = 24 * 3600
_PERCENT = re.compile(r"\((\d+(?:\.\d+)?)/100%\)")
_PART_NAME = re.compile(r"^[a-z0-9]+$")
# Imports running in this process: disk id -> its cancel flag.
_RUNNING: dict[int, threading.Event] = {}


def cancel(disk_id: int) -> bool:
    """Stop a running import; False when none runs for that disk here."""
    flag = _RUNNING.get(disk_id)
    if flag is None:
        return False
    flag.set()
    return True


@dataclass
class ImportOptions:
    prepare: bool = True           # an OS image: Prepare image right after the import
    defender_off: bool = False     # ... with Microsoft Defender turned off
    services_off: bool = False     # ... with background services a cafe PC does not need turned off


@dataclass
class ImageFile:
    path: str
    name: str
    size: int
    format: str


def image_format(path: Path) -> str | None:
    return FORMATS.get(path.suffix.lower())


def list_images(root: Path, max_depth: int = MAX_DEPTH) -> list[ImageFile]:
    """Image files under `root` (not hidden, not being uploaded), by path."""
    found: list[ImageFile] = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir() and not entry.is_symlink() and depth < max_depth:
                walk(entry, depth + 1)
            elif entry.is_file() and (fmt := image_format(entry)):
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                found.append(ImageFile(str(entry), entry.name, size, fmt))

    walk(root, 0)
    return found


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` (symlinks resolved) is in `folder`."""
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


# ── USB disks ─────────────────────────────────────────────────────────

def usb_partitions(lsblk_json: str, mounts: Path = USB_MOUNTS) -> list[dict]:
    """
    File systems on USB disks that may be opened read-only: from
    `lsblk -J -b -o NAME,PATH,TRAN,SIZE,MODEL,FSTYPE,LABEL,MOUNTPOINT,TYPE`.
    A disk is left out when any part of it holds a pool, LVM, RAID or swap,
    and a file system mounted anywhere but under `mounts` is left out too.
    """
    try:
        devices = json.loads(lsblk_json)["blockdevices"]
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for disk in devices:
        if disk.get("tran") != "usb" or disk.get("type") != "disk":
            continue
        parts = disk.get("children") or [disk]
        fstypes = {p.get("fstype") for p in parts} | {disk.get("fstype")}
        if fstypes & {"zfs_member", "LVM2_member", "linux_raid_member", "swap"}:
            continue
        for p in parts:
            name, fstype, mounted = p.get("name", ""), p.get("fstype"), p.get("mountpoint")
            if fstype not in READABLE_FS or not _PART_NAME.match(name):
                continue
            ours = mounted is not None and inside(Path(mounted), mounts)
            if mounted and not ours:
                continue
            out.append({
                "name": name, "path": p.get("path") or f"/dev/{name}", "size": int(p.get("size") or 0),
                "fstype": fstype, "label": p.get("label"), "model": (disk.get("model") or "").strip() or None,
                "mounted": mounted if ours else None,
            })
    return out


class UsbDisks:
    def __init__(self, runner, mounts: Path = USB_MOUNTS):
        self.runner = runner
        self.mounts = mounts

    def list(self) -> list[dict]:
        ok, out, _ = self.runner.run(["lsblk", "-J", "-b", "-o",
                                      "NAME,PATH,TRAN,SIZE,MODEL,FSTYPE,LABEL,MOUNTPOINT,TYPE"])
        return usb_partitions(out, self.mounts) if ok else []

    def _find(self, name: str) -> dict:
        part = next((p for p in self.list() if p["name"] == name), None)
        if part is None:
            raise ValueError(f"{name} is not a USB file system that can be opened")
        return part

    def mount(self, name: str) -> Path:
        """Mount a USB file system read-only; its folder."""
        part = self._find(name)
        if part["mounted"]:
            return Path(part["mounted"])
        target = self.mounts / name
        target.mkdir(parents=True, exist_ok=True)
        cmd = (["ntfs-3g", "-o", "ro", part["path"], str(target)] if part["fstype"] == "ntfs"
               else ["mount", "-o", "ro", part["path"], str(target)])
        ok, _, err = self.runner.run(cmd)
        if not ok:
            raise RuntimeError(f"{part['path']} could not be opened: {err}")
        logger.info("USB %s (%s) opened read-only at %s", part["path"], part["fstype"], target)
        return target

    def unmount(self, name: str) -> None:
        part = self._find(name)
        if not part["mounted"]:
            return
        ok, _, err = self.runner.run(["umount", part["mounted"]])
        if not ok:
            raise RuntimeError(f"{part['path']} could not be ejected: {err}")
        try:
            Path(part["mounted"]).rmdir()
        except OSError:
            pass
        logger.info("USB %s ejected", part["path"])


def virtual_size(runner, path: str, fmt: str) -> int | None:
    """The disk size inside an image file (qemu-img info), in bytes."""
    ok, out, _ = runner.run(["qemu-img", "info", "--output=json", "-f", fmt, path], timeout=120)
    if not ok:
        return None
    try:
        return int(json.loads(out)["virtual-size"])
    except (ValueError, KeyError, TypeError):
        return None


# ── Import job ────────────────────────────────────────────────────────

class DiskImporter:
    def __init__(self, make_session: sessionmaker[Session], prov: Provisioner, background: bool = True):
        self.make_session = make_session
        self.prov = prov
        self.background = background     # tests run the import in the request

    def start(self, disk_id: int, options: ImportOptions | None = None) -> None:
        options = options or ImportOptions()
        _RUNNING[disk_id] = threading.Event()
        if not self.background:
            self.run(disk_id, options)
            return
        threading.Thread(target=self.run, args=(disk_id, options), name=f"ggnet-import-{disk_id}",
                         daemon=True).start()

    def run(self, disk_id: int, options: ImportOptions | None = None) -> None:
        try:
            self._run(disk_id, options or ImportOptions())
        finally:
            _RUNNING.pop(disk_id, None)

    def _run(self, disk_id: int, options: ImportOptions) -> None:
        with self.make_session() as db:
            disk = db.get(GameDisk, disk_id)
            if disk is None or not disk.copying:
                return
            source, target = disk.copy_source, disk.zvol_path
        fmt = image_format(Path(source)) or "raw"
        logger.info("Importing %s (%s) into %s", source, fmt, target)
        started = time.monotonic()
        ok = self.prov.import_disk(source, fmt, target, self._progress(disk_id), cancel=_RUNNING.get(disk_id))
        with self.make_session() as db:
            disk = db.get(GameDisk, disk_id)
            if disk is None:
                return
            if not ok:
                disk.copy_percent = None
                disk.copy_error = (self.prov.zfs.runner.last_error or "the import failed")[:500]
                db.commit()
                return
            os_image = disk.kind is DiskKind.OS
            disk.copy_percent = 99 if os_image and options.prepare else None
            db.commit()
        logger.info("Import of %s into %s done in %.0f s", source, target, time.monotonic() - started)
        snapshot, note = "base", f"ggNet: imported from {Path(source).name}"
        if os_image and options.prepare:
            from app.services.os_setup import image_prep
            from app.services.provisioning import ProvisioningError

            try:
                snapshot = self.prov.prepare_image(target, image_prep(defender_off=options.defender_off,
                                                                      services_off=options.services_off))
                defender = (", Defender off" if options.defender_off else "") + \
                    (", background services off" if options.services_off else "")
                self.prov.set_comment(target, snapshot, f"ggNet: prepared for all PCs after the import{defender}")
            except ProvisioningError as e:
                note += f"; Prepare image failed: {e}"[:150]
                logger.warning("Preparing %s after its import failed: %s", target, e)
        try:
            self.prov.set_comment(target, "base", note[:200])
        except Exception as e:      # the image is there; only its comment is missing
            logger.warning("%s", e)
        with self.make_session() as db:
            disk = db.get(GameDisk, disk_id)
            if disk is not None:
                disk.snapshot, disk.copy_source, disk.copy_percent = snapshot, None, None
                db.commit()

    def _progress(self, disk_id: int) -> Callable[[float], None]:
        last = [0.0, -1]

        def update(percent: float) -> None:
            whole, now = int(percent), time.monotonic()
            if whole == last[1] or (now - last[0] < PROGRESS_EVERY and whole < 100):
                return
            last[0], last[1] = now, whole
            with self.make_session() as db:
                disk = db.get(GameDisk, disk_id)
                if disk is not None and disk.copying:
                    disk.copy_percent = whole
                    db.commit()

        return update


def qemu_percent(line: str) -> float | None:
    """The percentage in a `qemu-img convert -p` line such as '    (42.17/100%)'."""
    m = _PERCENT.search(line)
    return float(m.group(1)) if m else None
