"""
Backup and restore of images (Images -> Backup / Restore).

A backup is one image with ALL its versions, as a ZFS replication stream
(`zfs send -R -c <image>@<newest>`: every version, the comments, locks and
other properties, compressed blocks as they are on the pool), written into a
folder on a USB disk or another folder of the server:

    <location>/ggnet-backups/<image>-<YYYYMMDD-HHMMSS>/
        stream.000, stream.001, ...   the stream in parts of PART_BYTES
        manifest.json                 what it is, the parts' sizes and SHA-256

Parts, because USB sticks usually have FAT32, which takes no file over 4 GB.
manifest.json is written last: a folder without one is an unfinished backup.
It also keeps what lives only in ggNet's database: the image's kind, its
active version and the network cards Add new hardware installed in it.

A restore always makes a NEW image (`zfs recv -u <pool>/ggnet/images/<name>`),
never over an existing one; each part's SHA-256 is checked as it is read, and
a damaged backup leaves nothing behind. While it runs, the new image is in
the database with `copy_source = "backup:<folder>"` like a copy, so Images
shows its progress and copies.recover() cleans up after an interruption.

Jobs run one at a time in a background thread; each is a BackupJob row (the
History tab).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import BackupJob, Image, ImageHardware, ImageKind
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.backups")

FORMAT = "ggnet-backup"
FORMAT_VERSION = 1
ROOT = "ggnet-backups"
MANIFEST = "manifest.json"
PART_BYTES = 1024 ** 3
CHUNK = 4 * 1024 ** 2
# Written data is flushed to the disk this often: the progress shows what is
# really on the (often slow) USB disk, and a cancel never waits for a whole
# part to be flushed.
FLUSH_BYTES = 64 * 1024 ** 2
PROGRESS_EVERY = 2.0
# Free space kept on the backup disk besides the backup itself.
SPARE_BYTES = 64 * 1024 ** 2
BACKUP_SOURCE = "backup:"
# Export formats and qemu-img's names for them; VHD holds at most 2040 GB.
QEMU_FORMATS = {"vhdx": "vhdx", "vhd": "vpc"}
VHD_MAX_BYTES = 2040 * 1024 ** 3
FAT32_MAX_BYTES = 4 * 1024 ** 3 - 1

_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}-\d{8}-\d{6}$")
_PART = re.compile(r"^stream\.\d{3,5}$")


class BackupError(Exception):
    """Something the admin can act on: shown as is."""


class Cancelled(BackupError):
    pass


# ── The ZFS stream ────────────────────────────────────────────────────

class ZfsStream:
    """`zfs send` into Python and from Python into `zfs recv`; tests replace it."""

    def send_size(self, snapshot: str) -> int | None:
        """The stream's size in bytes (`zfs send -nP`), None when ZFS cannot tell."""
        try:
            out = subprocess.run(["zfs", "send", "-n", "-P", "-R", "-c", snapshot],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in (out.stdout + "\n" + out.stderr).splitlines():
            parts = line.split("\t")
            if parts[0] == "size" and len(parts) > 1 and parts[1].strip().isdigit():
                return int(parts[1])
        return None

    def send(self, snapshot: str, cancel: threading.Event) -> Iterator[bytes]:
        proc = subprocess.Popen(["zfs", "send", "-R", "-c", snapshot],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                if cancel.is_set():
                    raise Cancelled("cancelled")
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                yield chunk
            err = proc.stderr.read().decode(errors="replace").strip()
            if proc.wait() != 0:
                raise BackupError(f"zfs send failed: {err or _ended(proc.returncode)}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def receive(self, target: str, chunks: Iterator[bytes], cancel: threading.Event) -> None:
        proc = subprocess.Popen(["zfs", "recv", "-u", target],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for chunk in chunks:
                if cancel.is_set():
                    raise Cancelled("cancelled")
                proc.stdin.write(chunk)
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace").strip()
            if proc.wait() != 0:
                raise BackupError(f"zfs recv failed: {err or _ended(proc.returncode)}")
        except BrokenPipeError:
            err = proc.stderr.read().decode(errors="replace").strip()
            proc.wait()
            raise BackupError(f"zfs recv failed: {err or 'it stopped reading'}") from None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _ended(code: int) -> str:
    """How a process ended, in words: -15 is "it was stopped (signal 15)"."""
    return f"it was stopped (signal {-code})" if code < 0 else f"exit code {code}"


# ── Backup folders ────────────────────────────────────────────────────

@dataclass
class FoundBackup:
    folder: Path
    manifest: dict
    complete: bool          # every part is there with its size
    problem: str | None = None

    @property
    def size(self) -> int:
        return int(self.manifest.get("stream", {}).get("bytes", 0))


def find_backups(location: Path) -> list[FoundBackup]:
    """The backups in `location`/ggnet-backups, newest first."""
    root = location / ROOT
    found = []
    try:
        folders = sorted((p for p in root.iterdir() if p.is_dir() and _FOLDER.match(p.name)), reverse=True)
    except OSError:
        return []
    for folder in folders:
        try:
            manifest = json.loads((folder / MANIFEST).read_text())
        except (OSError, ValueError):
            continue                     # unfinished or not ours
        if manifest.get("format") != FORMAT:
            continue
        problem = None
        if manifest.get("version", 0) > FORMAT_VERSION:
            problem = "made by a newer ggNet"
        for part in manifest.get("stream", {}).get("parts", []):
            path = folder / str(part.get("file", ""))
            if not _PART.match(path.name) or not path.is_file() or path.stat().st_size != part.get("bytes"):
                problem = problem or f"{path.name} is missing or incomplete"
        found.append(FoundBackup(folder, manifest, problem is None, problem))
    found.sort(key=lambda b: b.manifest.get("created_at", ""), reverse=True)
    return found


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PartWriter:
    """Writes the stream into stream.NNN parts of PART_BYTES, with their SHA-256."""

    def __init__(self, folder: Path, part_bytes: int = PART_BYTES):
        self.folder, self.part_bytes = folder, part_bytes
        self.parts: list[dict] = []
        self.total = 0
        self._file = None
        self._hash = None
        self._size = 0
        self._unflushed = 0

    def _open(self) -> None:
        name = f"stream.{len(self.parts):03d}"
        self._file = (self.folder / name).open("wb")
        self._hash, self._size = hashlib.sha256(), 0
        self.parts.append({"file": name, "bytes": 0, "sha256": ""})

    def _close(self) -> None:
        if self._file is None:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self.parts[-1].update(bytes=self._size, sha256=self._hash.hexdigest())
        self._file = None

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self._file is None:
                self._open()
            room = self.part_bytes - self._size
            piece = view[:room]
            self._file.write(piece)
            self._hash.update(piece)
            self._size += len(piece)
            self.total += len(piece)
            self._unflushed += len(piece)
            view = view[len(piece):]
            if self._size >= self.part_bytes:
                self._close()
                self._unflushed = 0
            elif self._unflushed >= FLUSH_BYTES:
                self._file.flush()
                os.fdatasync(self._file.fileno())
                self._unflushed = 0

    def finish(self) -> None:
        self._close()

    def abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def read_parts(folder: Path, manifest: dict, progress: Callable[[int], None]) -> Iterator[bytes]:
    """The stream back from its parts; raises BackupError when a part's SHA-256 differs."""
    done = 0
    for part in manifest["stream"]["parts"]:
        path = folder / part["file"]
        if not _PART.match(path.name):
            raise BackupError(f"{path.name}: not a backup part")
        digest = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                digest.update(chunk)
                done += len(chunk)
                progress(done)
                yield chunk
        if digest.hexdigest() != part["sha256"]:
            raise BackupError(f"{path.name} is damaged (its SHA-256 differs from the backup's)")


# ── Jobs ──────────────────────────────────────────────────────────────

class BackupRunner:
    """Runs BackupJob rows one at a time in a background thread."""

    def __init__(self, make_session: sessionmaker[Session], prov: Provisioner, stream: ZfsStream | None = None,
                 open_location: Callable[[str, bool], Path] | None = None, background: bool = True,
                 part_bytes: int = PART_BYTES):
        self.make_session = make_session
        self.prov = prov
        self.stream = stream or ZfsStream()
        self.open_location = open_location
        self.background = background
        self.part_bytes = part_bytes
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel: dict[int, threading.Event] = {}

    # The newest version of an image: what `zfs send -R` starts from.
    def newest(self, zvol: str) -> str:
        versions = self.prov.zfs.list_snapshots(zvol) or []
        if not versions:
            raise BackupError(f"{zvol} has no versions to back up")
        return versions[-1]

    def estimate(self, image: Image) -> int | None:
        try:
            return self.stream.send_size(f"{image.zvol_path}@{self.newest(image.zvol_path)}")
        except BackupError:
            return None

    def kick(self) -> None:
        """Run the queued jobs (in a thread, unless background is off)."""
        if not self.background:
            self.run()
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self.run, name="ggnet-backups", daemon=True)
                self._thread.start()

    def cancel(self, job_id: int) -> bool:
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            if job is None or job.status not in ("queued", "running"):
                return False
            if job.status == "queued":
                job.status, job.error, job.finished_at = "cancelled", "cancelled by the admin", _now()
                if job.kind == "restore":
                    self._drop_restore_image(db, job)
                db.commit()
                return True
        event = self._cancel.get(job_id)
        if event is not None:
            event.set()
        return True

    def run(self) -> None:
        while True:
            with self.make_session() as db:
                job = db.scalars(select(BackupJob).where(BackupJob.status == "queued")
                                 .order_by(BackupJob.id)).first()
                if job is None:
                    return
                job.status, job.started_at = "running", _now()
                db.commit()
                job_id, kind = job.id, job.kind
            cancel = self._cancel.setdefault(job_id, threading.Event())
            try:
                if kind == "backup":
                    self._backup(job_id, cancel)
                elif kind == "export":
                    self._export(job_id, cancel)
                else:
                    self._restore(job_id, cancel)
                status, error = "done", None
            except Cancelled:
                status, error = "cancelled", "cancelled by the admin"
            except BackupError as e:
                status, error = "failed", str(e)
            except Exception as e:
                logger.exception("Backup job %s failed", job_id)
                status, error = "failed", f"unexpected error: {e}"
            self._cancel.pop(job_id, None)
            with self.make_session() as db:
                job = db.get(BackupJob, job_id)
                job.status, job.error, job.finished_at = status, error, _now()
                if job.kind == "restore" and status != "done":
                    self._drop_restore_image(db, job)
                db.commit()
                logger.info("%s of %s %s%s", job.kind.capitalize(), job.image_name, status,
                            f": {error}" if error else "")

    def _progress(self, job_id: int) -> Callable[[int], None]:
        last = [0.0]

        def update(done: int) -> None:
            now = time.monotonic()
            if now - last[0] < PROGRESS_EVERY:
                return
            last[0] = now
            with self.make_session() as db:
                job = db.get(BackupJob, job_id)
                if job is not None:
                    job.done_bytes = done
                    if job.kind == "restore" and job.image_id is not None:
                        image = db.get(Image, job.image_id)
                        if image is not None and job.total_bytes:
                            image.copy_percent = min(99, int(100 * done / job.total_bytes))
                    db.commit()

        return update

    # ── backup ──

    def _backup(self, job_id: int, cancel: threading.Event) -> None:
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            image = db.get(Image, job.image_id) if job.image_id else None
            if image is None or not image.published:
                raise BackupError("the image is gone or not published")
            info = {"name": image.name, "kind": image.kind.value, "size_gb": image.size_gb,
                    "snapshot": image.snapshot,
                    "hardware": [{"nic": h.nic, "instance": h.instance, "name": h.name, "snapshot": h.snapshot}
                                 for h in image.hardware]}
            zvol, location_id = image.zvol_path, job.location
        location = self.open_location(location_id, True)
        newest = self.newest(zvol)
        total = self.stream.send_size(f"{zvol}@{newest}")
        free = shutil.disk_usage(location).free
        if total is not None and total + SPARE_BYTES > free:
            raise BackupError(f"not enough free space: the backup needs {_gb(total)}, "
                              f"{_gb(free)} is free on the backup disk")
        folder = location / ROOT / f"{info['name']}-{_now().strftime('%Y%m%d-%H%M%S')}"
        folder.mkdir(parents=True)
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            job.folder, job.total_bytes = str(folder), total
            db.commit()
        logger.info("Backing up %s (%s, every version up to @%s) to %s", info["name"], _gb(total), newest, folder)
        writer = PartWriter(folder, self.part_bytes)
        progress = self._progress(job_id)
        try:
            for chunk in self.stream.send(f"{zvol}@{newest}", cancel):
                writer.write(chunk)
                progress(writer.total)
            writer.finish()
            versions = self.prov.zfs.list_snapshots(zvol) or []
            manifest = {
                "format": FORMAT, "version": FORMAT_VERSION,
                "created_at": _now().isoformat(timespec="seconds"),
                "image": info, "versions": versions,
                "stream": {"command": "zfs send -R -c", "snapshot": newest,
                           "bytes": writer.total, "parts": writer.parts},
            }
            tmp = folder / f".{MANIFEST}.tmp"
            tmp.write_text(json.dumps(manifest, indent=2))
            os.replace(tmp, folder / MANIFEST)
            os.sync()
        except BaseException:
            writer.abort()
            shutil.rmtree(folder, ignore_errors=True)
            raise
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            job.done_bytes = job.total_bytes = writer.total
            db.commit()

    # ── export ──

    def _export(self, job_id: int, cancel: threading.Event) -> None:
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            image = db.get(Image, job.image_id) if job.image_id else None
            if image is None or not image.published:
                raise BackupError("the image is gone or not published")
            zvol, snapshot, fmt, out = image.zvol_path, job.snapshot, job.format, Path(job.folder)
            location_id, total = job.location, job.total_bytes
        location = self.open_location(location_id, True)
        out = location / out.name                    # the file always lands in the location itself
        if out.exists():
            raise BackupError(f"{out.name} already exists there")
        free = shutil.disk_usage(location).free
        if total and total + SPARE_BYTES > free:
            raise BackupError(f"not enough free space: the file can take up to {_gb(total)}, "
                              f"{_gb(free)} is free there")
        logger.info("Exporting %s@%s as %s to %s", zvol, snapshot, fmt, out)
        update = self._progress(job_id)
        try:
            self.prov.export_version(zvol, snapshot, out, QEMU_FORMATS[fmt],
                                     lambda percent: update(int((total or 0) * percent / 100)), cancel)
        except ProvisioningError as e:
            if cancel.is_set():
                raise Cancelled("cancelled") from None
            raise BackupError(str(e)) from None
        os.sync()
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            job.folder, job.done_bytes = str(out), out.stat().st_size
            job.total_bytes = job.done_bytes
            db.commit()

    # ── restore ──

    def _restore(self, job_id: int, cancel: threading.Event) -> None:
        with self.make_session() as db:
            job = db.get(BackupJob, job_id)
            image = db.get(Image, job.image_id) if job.image_id else None
            if image is None:
                raise BackupError("the new image's record is gone")
            target, location_id, folder, image_id = image.zvol_path, job.location, Path(job.folder), image.id
        location = self.open_location(location_id, False)
        backup = next((b for b in find_backups(location) if b.folder == folder), None)
        if backup is None:
            raise BackupError(f"{folder.name} is not a backup on this disk any more")
        if not backup.complete:
            raise BackupError(f"{folder.name}: {backup.problem}")
        if self.prov.zfs.dataset_exists(target):
            raise BackupError(f"{target} already exists")
        logger.info("Restoring %s into %s", folder, target)
        progress = self._progress(job_id)
        try:
            self.stream.receive(target, read_parts(folder, backup.manifest, progress), cancel)
            versions = self.prov.zfs.list_snapshots(target) or []
            if versions != list(backup.manifest.get("versions") or versions):
                raise BackupError(f"the restored versions {versions} differ from the backup's")
            for v in versions:
                if not self.prov.zfs.protect_snapshot(f"{target}@{v}"):
                    raise BackupError(f"protecting {target}@{v} failed: {self.prov.zfs.runner.last_error}")
            if not self.prov.zfs.set_readonly(target, True):
                raise BackupError(f"making {target} read-only failed: {self.prov.zfs.runner.last_error}")
        except BaseException:
            if self.prov.zfs.dataset_exists(target) and not self.prov.zfs.destroy(target, recursive=True):
                logger.error("Could not remove the partial restore %s", target)
            raise
        info = backup.manifest["image"]
        active = info.get("snapshot") if info.get("snapshot") in versions else versions[-1]
        with self.make_session() as db:
            image = db.get(Image, image_id)
            image.snapshot, image.copy_source, image.copy_percent = active, None, None
            image.hardware.extend(
                ImageHardware(nic=h["nic"], instance=h["instance"], name=h["name"][:255],
                              snapshot=h.get("snapshot"))
                for h in info.get("hardware") or [] if h.get("nic") and h.get("instance"))
            job = db.get(BackupJob, job_id)
            job.done_bytes = job.total_bytes
            db.commit()

    @staticmethod
    def _drop_restore_image(db: Session, job: BackupJob) -> None:
        """A restore that did not finish: its new image's record goes (the zvol is already removed)."""
        image = db.get(Image, job.image_id) if job.image_id else None
        if image is not None and image.copy_source and image.copy_source.startswith(BACKUP_SOURCE):
            db.delete(image)
        job.image_id = None


# ── Where backups go ──────────────────────────────────────────────────

class BackupLocations:
    """
    Where backups are written and read: the server's USB disks (usb:<partition>,
    mounted writable for a backup) and the folders in `[paths] backup_dirs`
    (dir:<n>), e.g. a second disk or a NAS mounted on the server.
    """

    def __init__(self, usb, dirs: list[Path], imports: Path | None = None):
        self.usb = usb
        self.dirs = dirs
        self.imports = imports          # the imports folder on the pool (exports only)

    def list(self, exports: bool = False) -> list[dict]:
        out = []
        for part in self.usb.list():
            label = " ".join(x for x in (part.get("label"), part.get("model")) if x) or part["name"]
            out.append({"id": f"usb:{part['name']}", "kind": "usb", "label": f"USB {label} ({part['fstype']})",
                        "size": part["size"], "path": part["mounted"], "fstype": part["fstype"]})
        for i, folder in enumerate(self.dirs):
            out.append({"id": f"dir:{i}", "kind": "dir", "label": str(folder), "size": None,
                        "path": str(folder) if folder.is_dir() else None, "fstype": None})
        if exports and self.imports is not None and self.imports.is_dir():
            out.append({"id": "imports", "kind": "imports", "label": f"Imports folder ({self.imports})",
                        "size": None, "path": str(self.imports), "fstype": None})
        return out

    def fstype(self, location: str) -> str | None:
        return next((x["fstype"] for x in self.list(exports=True) if x["id"] == location), None)

    def label(self, location: str) -> str:
        return next((x["label"] for x in self.list(exports=True) if x["id"] == location), location)

    def open(self, location: str, writable: bool = False) -> Path:
        kind, _, key = location.partition(":")
        if kind == "usb":
            try:
                return self.usb.mount(key, writable=writable)
            except ValueError:
                raise BackupError(f"{key} is not a USB disk on the server (unplugged?)") from None
            except RuntimeError as e:
                raise BackupError(str(e)) from None
        if location == "imports" and self.imports is not None and self.imports.is_dir():
            return self.imports
        if kind == "dir" and key.isdigit() and int(key) < len(self.dirs):
            folder = self.dirs[int(key)]
            if not folder.is_dir():
                raise BackupError(f"{folder} does not exist on the server")
            return folder
        raise BackupError(f"unknown backup location {location!r}")

    def eject(self, location: str) -> None:
        kind, _, key = location.partition(":")
        if kind != "usb":
            raise BackupError("only a USB disk can be ejected")
        os.sync()
        try:
            self.usb.unmount(key)
        except (ValueError, RuntimeError) as e:
            raise BackupError(str(e)) from None


def recover(make_session: sessionmaker[Session]) -> None:
    """Jobs that were running when ggNet stopped: failed; an unfinished backup folder is removed."""
    with make_session() as db:
        for job in db.scalars(select(BackupJob).where(BackupJob.status.in_(("queued", "running")))):
            if job.kind == "backup" and job.folder and not (Path(job.folder) / MANIFEST).exists():
                shutil.rmtree(job.folder, ignore_errors=True)
            if job.kind == "export" and job.folder:
                Path(job.folder + ".part").unlink(missing_ok=True)
            job.status, job.error, job.finished_at = "failed", "interrupted: ggNet stopped while it ran", _now()
            logger.warning("%s of %s was interrupted", job.kind.capitalize(), job.image_name)
        db.commit()


def _gb(n: int | None) -> str:
    return "an unknown size" if n is None else f"{n / 1024 ** 3:.1f} GB"


def kind_of(manifest: dict) -> ImageKind:
    return ImageKind.OS if manifest.get("image", {}).get("kind") == "os" else ImageKind.GAME
