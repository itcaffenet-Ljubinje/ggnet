"""
Setting up the hardware scan's Windows PE from the web UI (Settings ->
Hardware scan), see app.services.scan for how a PC boots it:

  * Windows PE itself (BCD, boot.sdi, boot.wim) comes from a Windows
    installation ISO the admin already has on the server, in Proxmox's ISO
    storage or in ggNet's imports folder: the ISO is mounted read-only and
    the three files are copied into `[paths] winpe_dir`. Microsoft's files
    are never shipped with ggNet. wimboot (GPL, iPXE project) is downloaded
    by scripts/install_ggnet.sh.
  * Network drivers for Windows PE: one folder of a vendor's driver package
    (INF, SYS, CAT, as the vendor ships it) per library package, uploaded
    file by file into a hidden staging folder and checked before it is
    added (a network INF for 64-bit Windows and its signed catalog).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.services import scan

logger = logging.getLogger("ggnet.winpe")

# Proxmox's ISO storage on the host ("local", ISO Images).
DEFAULT_ISO_DIRS = ("/var/lib/vz/template/iso",)
ISO_MOUNT = Path("/run/ggnet/winpe-iso")
# Where Windows installation media keeps what wimboot needs (any case).
ISO_FILES = {
    "BCD": ("boot/bcd", "efi/microsoft/boot/bcd"),
    "boot.sdi": ("boot/boot.sdi",),
    "boot.wim": ("sources/boot.wim",),
}
ISO_X64 = "efi/boot/bootx64.efi"
NET_CLASS_GUID = "{4d36e972-e325-11ce-bfc1-08002be10318}"
# Limits for one uploaded driver package.
MAX_FILES = 64
MAX_FILE_BYTES = 64 * 1024 ** 2
MAX_PACKAGE_BYTES = 256 * 1024 ** 2
VENDORS = {"8086": "Intel", "10ec": "Realtek", "14e4": "Broadcom", "1969": "Qualcomm Atheros",
           "1d6a": "Aquantia", "1fc9": "Tehuti", "15b3": "Mellanox", "11ab": "Marvell",
           "1af4": "Red Hat (virtio)", "8088": "Wangxun"}

_ISO_NAME = re.compile(r"^[^/\x00]{1,255}\.iso$", re.IGNORECASE)
_PCI = re.compile(r"PCI\\VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", re.IGNORECASE)


class WinPeError(Exception):
    """Something the admin can fix: shown as is."""


# ── Windows PE from an ISO ────────────────────────────────────────────

@dataclass(frozen=True)
class IsoFile:
    path: str
    name: str
    size: int


def list_isos(folders: list[Path]) -> list[IsoFile]:
    """The .iso files directly in `folders` (not hidden), by name."""
    found: dict[str, IsoFile] = {}
    for folder in folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith(".") or not _ISO_NAME.match(p.name):
                continue
            try:
                if p.is_file():
                    found.setdefault(str(p), IsoFile(str(p), p.name, p.stat().st_size))
            except OSError:
                continue
    return sorted(found.values(), key=lambda f: (f.name.lower(), f.path))


def find_ci(root: Path, relative: str) -> Path | None:
    """The file `relative` (a/b/c) under `root`, whatever the case of each part."""
    part, _, rest = relative.partition("/")
    try:
        matches = sorted(p for p in root.iterdir() if p.name.lower() == part.lower())
    except OSError:
        return None
    for p in matches:
        found = find_ci(p, rest) if rest else (p if p.is_file() else None)
        if found is not None:
            return found
    return None


def files_on_media(root: Path) -> dict[str, Path]:
    """
    BCD, boot.sdi and boot.wim on mounted Windows installation media; raises
    WinPeError when it is not 64-bit Windows media.
    """
    found: dict[str, Path] = {}
    for name, candidates in ISO_FILES.items():
        path = next((p for p in (find_ci(root, c) for c in candidates) if p is not None), None)
        if path is None:
            raise WinPeError(f"this is not Windows installation media: {candidates[0]} is missing")
        found[name] = path
    if find_ci(root, ISO_X64) is None:
        raise WinPeError(f"this is not 64-bit Windows installation media: {ISO_X64} is missing")
    return found


class WinPeSetup:
    """Copies Windows PE out of a Windows ISO (one at a time)."""

    _lock = threading.Lock()

    def __init__(self, runner, folder: Path, mounts: Path = ISO_MOUNT):
        self.runner = runner
        self.folder = folder
        self.mounts = mounts

    def from_iso(self, iso: Path) -> list[str]:
        """Mount `iso` read-only, copy its Windows PE into the folder; the names copied."""
        if not self._lock.acquire(blocking=False):
            raise WinPeError("Windows PE is already being copied from an ISO")
        try:
            target = self.mounts
            target.mkdir(parents=True, exist_ok=True)
            ok, _, err = self.runner.run(["mount", "-o", "loop,ro,nosuid,nodev,noexec", str(iso), str(target)],
                                         timeout=120)
            if not ok:
                raise WinPeError(f"cannot open {iso.name}: {err.strip() or 'mount failed'}")
            try:
                return self._copy(files_on_media(target))
            finally:
                ok, _, err = self.runner.run(["umount", str(target)], timeout=120)
                if not ok:
                    logger.warning("Cannot unmount %s: %s", target, err.strip())
        finally:
            self._lock.release()

    def _copy(self, files: dict[str, Path]) -> list[str]:
        self.folder.mkdir(parents=True, exist_ok=True)
        for name, src in files.items():
            part = self.folder / f".{name}.part"
            try:
                shutil.copyfile(src, part)
                os.replace(part, self.folder / name)
            except OSError as e:
                part.unlink(missing_ok=True)
                raise WinPeError(f"copying {name} failed: {e.strerror or e}") from e
            # An older copy under another case (bcd) would hide nothing but confuse.
            for other in self.folder.iterdir():
                if other.name != name and other.name.lower() == name.lower():
                    other.unlink(missing_ok=True)
            logger.info("Windows PE: %s copied from the ISO (%d bytes)", name, (self.folder / name).stat().st_size)
        return list(files)


# ── Network drivers for Windows PE ────────────────────────────────────

def _sections(text: str) -> dict[str, list[str]]:
    """An INF's sections (lower-case names) and their lines, comments removed."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            current = out.setdefault(line[1:line.index("]")].strip().lower(), [])
        elif current is not None:
            current.append(line)
    return out


def _strip_comment(line: str) -> str:
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _pairs(lines: list[str]) -> list[tuple[str, str]]:
    return [(k.strip(), v.strip()) for k, _, v in (ln.partition("=") for ln in lines) if _]


@dataclass
class InfInfo:
    name: str
    network: bool = False
    x64: bool = False
    catalog: str | None = None
    devices: dict[str, str] = field(default_factory=dict)   # chip (8086-15bc) -> device name


def read_inf(name: str, text: str) -> InfInfo:
    """What a driver INF installs: its class, its catalog and its 64-bit devices."""
    sections = _sections(text)
    version = {k.lower(): v.strip('"') for k, v in _pairs(sections.get("version", []))}
    info = InfInfo(name=name)
    info.network = (version.get("class", "").lower() == "net"
                    or version.get("classguid", "").lower() == NET_CLASS_GUID)
    info.catalog = version.get("catalogfile.ntamd64") or version.get("catalogfile")
    strings = {k.lower(): v.strip('"') for k, v in _pairs(sections.get("strings", []))}
    models: list[str] = []             # the model sections for 64-bit Windows
    for _, value in _pairs(sections.get("manufacturer", [])):
        base, *decorations = [p.strip() for p in value.split(",")]
        if not decorations:
            models.append(base.lower())  # undecorated: every platform
        models += [f"{base}.{d}".lower() for d in decorations if d.lower().startswith("ntamd64")]
    for model in models:
        for key, value in _pairs(sections.get(model, [])):
            m = _PCI.search(value)
            if m is None:
                continue
            chip = f"{m.group(1)}-{m.group(2)}".lower()
            desc = strings.get(key.strip("%").lower(), key) if key.startswith("%") else key.strip('"')
            info.devices.setdefault(chip, " ".join(desc.split()))
    info.x64 = bool(info.devices)
    return info


@dataclass
class PackageInfo:
    name: str
    files: list[str]
    size: int
    infs: list[InfInfo]

    @property
    def devices(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for inf in self.infs:
            for chip, desc in inf.devices.items():
                out.setdefault(chip, desc)
        return out


def package_info(folder: Path) -> PackageInfo:
    files = sorted(p for p in folder.iterdir() if p.is_file())
    infs = []
    for p in files:
        if p.suffix.lower() == ".inf":
            try:
                infs.append(read_inf(p.name, scan._inf_text(p)))
            except OSError:
                continue
    return PackageInfo(folder.name, [p.name for p in files], sum(p.stat().st_size for p in files), infs)


def check_package(info: PackageInfo) -> None:
    """Raises WinPeError unless `info` holds a 64-bit network driver with its catalog."""
    if not info.infs:
        raise WinPeError("the folder has no .inf file; choose the folder with the driver's INF, SYS and CAT files")
    usable = [i for i in info.infs if i.network and i.x64]
    if not usable:
        raise WinPeError("no INF in the folder installs a network card for 64-bit Windows "
                         "(choose the folder for Windows x64, e.g. Winx64\\NDIS68 or WinPE\\64)")
    names = {f.lower() for f in info.files}
    for inf in usable:
        if not inf.catalog or inf.catalog.lower() not in names:
            raise WinPeError(f"{inf.name} is signed by {inf.catalog or 'no catalog'}, which is not in the folder")


class DriverUploads:
    """Adds and removes packages in the hardware scan's driver library."""

    def __init__(self, library: scan.DriverLibrary):
        self.root = library.root

    def _package(self, package: str) -> Path:
        if not scan._PACKAGE.match(package):
            raise WinPeError("a package name has lower-case letters, digits, '.', '_' and '-' only, e.g. intel-pro1000")
        return self.root / package

    def _staging(self, package: str) -> Path:
        self._package(package)
        return self.root / f".{package}.upload"

    def start(self, package: str) -> None:
        if self._package(package).exists():
            raise WinPeError(f"the library already has a package named {package}")
        staging = self._staging(package)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

    def write(self, package: str, name: str, chunks) -> int:
        """Write one file of the package being uploaded; `chunks` yields bytes. Its size."""
        staging = self._staging(package)
        if not staging.is_dir():
            raise WinPeError(f"no upload of {package} was started")
        if not scan._DRIVER_FILE.match(name):
            raise WinPeError(f"{name!r}: driver file names have letters, digits, '.', '_' and '-' only "
                             "(at most 64 characters)")
        present = [p for p in staging.iterdir() if p.is_file()]
        if len(present) >= MAX_FILES:
            raise WinPeError(f"a driver package has at most {MAX_FILES} files")
        target, size = staging / name, 0
        with target.open("wb") as f:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise WinPeError(f"{name} is larger than {MAX_FILE_BYTES // 1024 ** 2} MB")
                f.write(chunk)
        return size

    def finish(self, package: str) -> PackageInfo:
        """Check the uploaded files and add them as the package."""
        staging, folder = self._staging(package), self._package(package)
        if not staging.is_dir():
            raise WinPeError(f"no upload of {package} was started")
        try:
            info = package_info(staging)
            if info.size > MAX_PACKAGE_BYTES:
                raise WinPeError(f"a driver package is at most {MAX_PACKAGE_BYTES // 1024 ** 2} MB")
            check_package(info)
            if folder.exists():
                raise WinPeError(f"the library already has a package named {package}")
            staging.rename(folder)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        logger.info("Windows PE drivers: added %s (%d files, %d network chips)",
                    package, len(info.files), len(info.devices))
        return package_info(folder)

    def remove(self, package: str) -> bool:
        folder = self._package(package)
        shutil.rmtree(self._staging(package), ignore_errors=True)
        if not folder.is_dir():
            return False
        shutil.rmtree(folder)
        logger.info("Windows PE drivers: removed %s", package)
        return True


def vendor(chip: str) -> str:
    return VENDORS.get(chip.split("-")[0], chip.split("-")[0].upper())
