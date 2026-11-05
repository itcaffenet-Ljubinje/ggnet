"""
Hardware scan: a new PC tells the server its network card itself.

Booting an OS image over iSCSI needs the PC's boot network card installed in
the image under the card's exact instance ID (app.services.toolchain). iPXE
reports the card's hardware ID, but the instance ID only Windows knows. So
the PC the Add new hardware wizard takes (app.services.toolchain.HardwareWizard)
gets a WinPE boot instead of Windows
(wimboot, the Windows PE files from `[paths] winpe_dir`, with
`ggnet-scan.exe` from the agent folder as its only program):

  1. wimboot injects into WinPE's System32: winpeshl.ini (starts the
     scanner), the scanner, its ggnet-scan.json (this server, the PC's MAC)
     and a signed driver package for the card from `[paths] winpe_drivers`
     (DriverLibrary: the vendors' packages, e.g. Intel's PRO1000 NDIS68
     folder; WinPE has few network drivers, and Windows' own inbox
     drivers cannot be loaded there, as they are signed only by the
     catalogs of the Windows they come with);
  2. the scanner loads the driver, starts the network and sends the PC's
     PCI devices to POST /api/v1/boot/scan, which keeps the card's instance
     ID; then it restarts the PC;
  3. at the next iPXE request the server adds the card to a new version
     of the image and the PC waits on screen until it is active, then
     boots it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.windows_setup import NET_CLASS, NIC_ID_RE, NIC_INSTANCE_RE

logger = logging.getLogger("ggnet.scan")

SCANNER_EXE = "ggnet-scan.exe"
# The Windows PE files (from Windows installation media or the ADK) and wimboot.
WINPE_FILES = ("wimboot", "BCD", "boot.sdi", "boot.wim")
WINPESHL_INI = "[LaunchApps]\r\n%SYSTEMROOT%\\System32\\ggnet-scan.exe\r\n"
_DRIVER_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CHIP = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}$")


def winpe_dir(cfg: dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    return Path(paths.get("winpe_dir") or Path(paths.get("data_dir", "/var/lib/ggnet")) / "winpe")


def winpe_file(folder: Path, name: str) -> Path | None:
    """One of WINPE_FILES in `folder`, whatever its case (bcd, BCD)."""
    if name not in WINPE_FILES:
        return None
    exact = folder / name
    if exact.is_file():
        return exact
    try:
        return next((p for p in folder.iterdir() if p.name.lower() == name.lower() and p.is_file()), None)
    except OSError:
        return None


def winpe_ready(folder: Path) -> bool:
    return all(winpe_file(folder, n) is not None for n in WINPE_FILES)


def chip_key(nic: str) -> str:
    """'8086-15bc' for VEN_8086&DEV_15BC&...: one driver folder per chip."""
    m = NIC_ID_RE.match(nic)
    if m is None:
        raise ValueError(f"Invalid PCI hardware ID: {nic!r}")
    return f"{m.group(1)}-{m.group(2)}".lower()


def ipxe_scan_script(base: str, mac: str, chip: str, package: str | None, drivers: list[str]) -> str:
    """
    The iPXE script that boots WinPE with the scanner. `base` is this server's
    URL with a trailing slash. index=1: Windows PE, not Windows Setup, in a
    boot.wim from installation media.
    """
    boot, scan = f"{base}api/v1/boot/winpe", f"{base}api/v1/boot/scan"
    lines = [
        "#!ipxe",
        f"echo ggNet: this PC's network card is new to its Windows image (MAC {mac}).",
        "echo ggNet: starting the hardware scan (Windows PE); the PC restarts by itself.",
        f"kernel {boot}/wimboot index=1 || goto failed",
        f"initrd -n BCD {boot}/BCD || goto failed",
        f"initrd -n boot.sdi {boot}/boot.sdi || goto failed",
        f"initrd -n winpeshl.ini {scan}/winpeshl.ini || goto failed",
        f"initrd -n {SCANNER_EXE} {scan}/{SCANNER_EXE} || goto failed",
        f"initrd -n ggnet-scan.json {scan}/config?mac={mac}&chip={chip} || goto failed",
    ]
    for name in drivers:
        lines.append(f"initrd -n {name} {scan}/driver/{package}/{name} || goto failed")
    lines += [
        f"initrd -n boot.wim {boot}/boot.wim || goto failed",
        "boot || goto failed",
        ":failed",
        "echo ggNet: the hardware scan could not start; booting from the local disk in 10 s.",
        "sleep 10",
        "exit",
    ]
    return "\n".join(lines) + "\n"


def scan_config(base: str, mac: str, infs: list[str]) -> str:
    """ggnet-scan.json: this server, the PC's MAC and the driver's INF files to load."""
    return json.dumps({"server": base.rstrip("/"), "mac": mac, "drivers": infs})


def is_network(device: dict) -> bool:
    """A network controller: Net class, or (no driver in WinPE, so no class) PCI class code 02."""
    if str(device.get("class_guid") or "").lower() == NET_CLASS:
        return True
    ids = [str(i) for i in (device.get("hardware_ids") or []) + (device.get("compatible_ids") or [])]
    return any("CC_02" in i.upper() for i in ids)


@dataclass
class ChosenCard:
    nic: str          # PCI hardware ID without PCI\
    instance: str


def choose_card(devices: list[dict], boot_nic: str | None) -> ChosenCard | None:
    """
    The PC's boot network card among the PCI devices the scanner sent: the
    one with the hardware ID iPXE reported, else the only network card with
    that chip, else the only network card.
    """
    cards = []
    for d in devices:
        if not is_network(d):
            continue
        parts = str(d.get("instance_id") or "").split("\\")
        if len(parts) != 3 or parts[0].upper() != "PCI":
            continue
        nic, instance = parts[1].upper(), parts[2].upper()
        if NIC_ID_RE.match(nic) and NIC_INSTANCE_RE.match(instance):
            cards.append(ChosenCard(nic, instance))
    if boot_nic:
        exact = [c for c in cards if c.nic == boot_nic.upper()]
        if exact:
            return exact[0]
        chip = boot_nic.upper().split("&SUBSYS_")[0]
        same = [c for c in cards if c.nic.startswith(chip + "&")]
        if len(same) == 1:
            return same[0]
    return cards[0] if len(cards) == 1 else None


def winpe_drivers_dir(cfg: dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    return Path(paths.get("winpe_drivers") or Path(paths.get("data_dir", "/var/lib/ggnet")) / "winpe-drivers")


def _inf_text(path: Path) -> str:
    data = path.read_bytes()
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    return data.decode("latin-1")


class DriverLibrary:
    """
    Signed network driver packages for the hardware scan's Windows PE, one
    folder per package under `root` (INF, SYS, CAT and what the INF copies,
    as the vendor ships them; only top-level files, as wimboot injects them
    into one folder). A package fits a chip when one of its INF files names
    the chip's PCI\\VEN_xxxx&DEV_yyyy.
    """

    def __init__(self, root: Path):
        self.root = root
        self._texts: dict[Path, tuple[int, str]] = {}

    def packages(self) -> list[Path]:
        try:
            return sorted(p for p in self.root.iterdir() if p.is_dir() and _PACKAGE.match(p.name))
        except OSError:
            return []

    def _text(self, path: Path) -> str:
        stamp = path.stat().st_mtime_ns
        cached = self._texts.get(path)
        if cached is None or cached[0] != stamp:
            cached = self._texts[path] = (stamp, _inf_text(path))
        return cached[1]

    def match(self, chip: str) -> tuple[str, list[str]] | None:
        """(package, its INF files that name the chip) for a chip key like 8086-15bc; None when none fits."""
        if not _CHIP.match(chip):
            return None
        ven, dev = chip.upper().split("-")
        wanted = f"PCI\\VEN_{ven}&DEV_{dev}"
        for package in self.packages():
            infs = []
            for inf in sorted(package.glob("*.inf")) + sorted(package.glob("*.INF")):
                try:
                    if wanted in self._text(inf).upper() and inf.name not in infs:
                        infs.append(inf.name)
                except OSError:
                    continue
            if infs:
                return package.name, infs
        return None

    def files(self, package: str) -> list[str]:
        folder = self.root / package
        if not _PACKAGE.match(package) or not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir()
                      if p.is_file() and _DRIVER_FILE.match(p.name) and p.suffix.lower() != ".txt")

    def file(self, package: str, name: str) -> Path | None:
        if name not in self.files(package):
            return None
        return self.root / package / name


# ── Which cards a version of an image starts (cached; a version never changes) ──

_STATUS: dict[tuple[str, str], dict[tuple[str, str | None], str]] = {}
_STATUS_LOCK = threading.Lock()


def nic_status(prov, master: str, snapshot: str, cards: list[tuple[str, str | None]]
               ) -> dict[tuple[str, str | None], str] | str:
    """
    Provisioner.check_hardware() for the cards not checked on this version
    yet; one check at a time, so PCs that boot together share it.
    """
    with _STATUS_LOCK:
        known = _STATUS.setdefault((master, snapshot), {})
        todo = sorted({c for c in cards if c not in known}, key=lambda c: (c[0], c[1] or ""))
        if todo:
            found = prov.check_hardware(master, snapshot, todo)
            if isinstance(found, str):
                return found
            known.update(found)
        return {c: known.get(c, "unknown") for c in cards}
