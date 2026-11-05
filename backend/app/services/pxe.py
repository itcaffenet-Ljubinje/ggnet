"""
Network boot (PXE) for the Settings page: the boot settings the admin
changes in the web UI, and the state of the PXE service (proxyDHCP + TFTP).

Boot settings: `[boot] gateway` and `[boot] unregistered` in config.toml are
the defaults; once the admin saves them in the web UI, the saved values win
(table `settings`, key "boot"). They are read at every iPXE request, so a
change applies to the next PC that boots, with no restart.

The PXE service is ggNet's own dnsmasq (`ggnet-pxe`, scripts/install_ggnet.sh;
`[boot] pxe_service` names another unit, e.g. on the dev instance). The web
UI shows its state and can restart it; it never changes its configuration.

Secure Boot: the PCs load shim (signed by Microsoft) from `sb/` in the TFTP
root, and shim loads iPXE signed with the ggNet key, which each PC accepts
once (MOK). The installer makes the key in `[boot] secureboot_dir` (default:
`secureboot/` next to config.toml); the web UI offers its certificate
(ggnet-sb.cer) for that, and shows whether the iPXE in `sb/` is signed with it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import DEFAULT_CONFIG_PATH, get_config
from app.db.models import AppSetting
from app.runner import CommandRunner

logger = logging.getLogger("ggnet.pxe")

SETTINGS_KEY = "boot"
DEFAULT_UNIT = "ggnet-pxe"
# What the PCs fetch over TFTP: iPXE (snponly.efi uses the board's own network
# driver, ipxe.efi its built-in ones) and the script that sends iPXE to ggNet.
TFTP_FILES = ("snponly.efi", "ipxe.efi", "autoexec.ipxe",
              # Secure Boot: shim, its MOK manager, iPXE signed with the ggNet key.
              "sb/shimx64.efi", "sb/mmx64.efi", "sb/grubx64.efi")
SB_CERT = "ggnet-sb.cer"      # DER, what a PC enrolls
SB_CERT_PEM = "ggnet-sb.crt"  # the same certificate, for checking signatures


@dataclass
class BootSettings:
    # iBFT gateway for Windows: "none" (0.0.0.0), "dhcp" or an IPv4 address.
    gateway: str = "none"
    # A PC whose MAC is not registered: "wait" on screen, or boot its "local" disk.
    unregistered: str = "wait"
    # Agents ask shim to enroll the ggNet Secure Boot key (MokManager password).
    mok_enroll: bool = False
    mok_password: str = "ggnet"
    # New PC template: an unknown PC that boots while Add new hardware is open
    # is registered with this name ({n} or {nn}: the first free number) and this
    # game disk, in Boot Mode on the wizard's image. No game disk: it is not.
    new_pc_name: str = "pc-{nn}"
    new_pc_game_disk_id: int | None = None


def new_pc_name(pattern: str, taken: set[str]) -> str | None:
    """The first free name from a pattern like komp-{nn} (komp-01, komp-02 …) or pc{n}."""
    for n in range(1, 1000):
        name = pattern.replace("{nn}", f"{n:02d}").replace("{n}", str(n)).lower()
        if name not in taken:
            return name
    return None


def _defaults(cfg: dict[str, Any]) -> BootSettings:
    boot = cfg.get("boot") or {}
    return BootSettings(
        gateway=str(boot.get("gateway") or "none").strip() or "none",
        unregistered=str(boot.get("unregistered") or "wait"),
        mok_enroll=bool(boot.get("mok_enroll", False)),
        mok_password=str(boot.get("mok_password") or "ggnet"),
        new_pc_name=str(boot.get("new_pc_name") or "pc-{nn}"),
        new_pc_game_disk_id=boot.get("new_pc_game_disk_id"),
    )


def load_settings(db: Session, cfg: dict[str, Any] | None = None) -> tuple[BootSettings, bool]:
    """The saved boot settings and whether the admin saved them (else config.toml's)."""
    s = _defaults(cfg if cfg is not None else get_config())
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        return s, False
    known = BootSettings.__dataclass_fields__
    saved = {k: v for k, v in json.loads(row.value).items() if k in known}
    return BootSettings(**{**asdict(s), **saved}), True


def save_settings(db: Session, s: BootSettings) -> None:
    row = db.get(AppSetting, SETTINGS_KEY)
    if row is None:
        db.add(AppSetting(key=SETTINGS_KEY, value=json.dumps(asdict(s))))
    else:
        row.value = json.dumps(asdict(s))
    db.commit()


def effective_config(db: Session, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The config with `[boot]` replaced by the settings saved in the web UI, if any."""
    cfg = cfg if cfg is not None else get_config()
    s, saved = load_settings(db, cfg)
    if not saved:
        return cfg
    return {**cfg, "boot": {**(cfg.get("boot") or {}), **asdict(s)}}


@dataclass
class PxeStatus:
    unit: str
    installed: bool
    # systemd's ActiveState (active, inactive, failed, activating, ...) and SubState.
    state: str
    sub_state: str
    since: datetime | None
    tftp_root: str
    # Which of TFTP_FILES are in the TFTP root.
    files: dict[str, bool]
    secure_boot: "SecureBootKey | None" = None


@dataclass
class SecureBootKey:
    """The ggNet Secure Boot certificate the PCs enroll."""

    subject: str
    fingerprint: str          # SHA-256, as MokManager shows it
    expires: datetime | None
    # Whether sb/grubx64.efi is signed with this key (None: not there or not checked).
    ipxe_signed: bool | None


class PxeService:
    def __init__(self, unit: str, tftp_root: Path, runner: CommandRunner | None = None,
                 secureboot_dir: Path | None = None):
        self.unit = unit
        self.tftp_root = tftp_root
        self.runner = runner or CommandRunner(timeout=30)
        self.secureboot_dir = secureboot_dir

    @property
    def certificate(self) -> Path | None:
        """The DER certificate a PC enrolls, if the installer made the key."""
        if self.secureboot_dir is None:
            return None
        path = self.secureboot_dir / SB_CERT
        return path if path.is_file() else None

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None = None) -> "PxeService":
        cfg = cfg if cfg is not None else get_config()
        root = (cfg.get("services") or {}).get("tftp_root") or Path(cfg["paths"]["data_dir"]) / "tftp"
        boot = cfg.get("boot") or {}
        unit = str(boot.get("pxe_service") or DEFAULT_UNIT)
        sb_dir = boot.get("secureboot_dir") or (
            Path(os.environ.get("GGNET_CONFIG", DEFAULT_CONFIG_PATH)).parent / "secureboot")
        return cls(unit, Path(root), secureboot_dir=Path(sb_dir))

    def status(self) -> PxeStatus | None:
        """The service's state, or None when systemctl cannot be asked."""
        ok, out, _ = self.runner.run(
            ["systemctl", "show", self.unit,
             "--property=LoadState,ActiveState,SubState,ActiveEnterTimestamp", "--timestamp=unix"],
        )
        if not ok:
            return None
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return PxeStatus(
            unit=self.unit,
            installed=props.get("LoadState") == "loaded",
            state=props.get("ActiveState") or "unknown",
            sub_state=props.get("SubState") or "",
            since=_unix_time(props.get("ActiveEnterTimestamp", "")),
            tftp_root=str(self.tftp_root),
            files={f: (self.tftp_root / f).is_file() for f in TFTP_FILES},
            secure_boot=self.secure_boot_key(),
        )

    def secure_boot_key(self) -> SecureBootKey | None:
        """The certificate's details; None without a key or when openssl cannot read it."""
        cert = self.certificate
        if cert is None:
            return None
        ok, out, _ = self.runner.run(
            ["openssl", "x509", "-inform", "DER", "-in", str(cert), "-noout",
             "-subject", "-enddate", "-fingerprint", "-sha256", "-nameopt", "oneline"], quiet=True)
        if not ok:
            return None
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        ipxe = self.tftp_root / "sb" / "grubx64.efi"
        pem = cert.with_name(SB_CERT_PEM)
        signed = None
        if ipxe.is_file() and pem.is_file():
            signed = self.runner.run(["sbverify", "--cert", str(pem), str(ipxe)], quiet=True)[0]
        return SecureBootKey(
            subject=props.get("subject", "").strip(),
            fingerprint=next((v for k, v in props.items() if k.lower().endswith("fingerprint")), "").strip(),
            expires=_openssl_time(props.get("notAfter", "")),
            ipxe_signed=signed,
        )

    def restart(self) -> bool:
        ok, _, err = self.runner.run(["systemctl", "restart", self.unit])
        if ok:
            logger.info("PXE service %s restarted from the web UI", self.unit)
        else:
            logger.error("Restarting the PXE service %s failed: %s", self.unit, err)
        return ok


def _openssl_time(value: str) -> datetime | None:
    """openssl's `Oct 16 00:33:03 2036 GMT`."""
    try:
        return datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _unix_time(value: str) -> datetime | None:
    """systemd's `@1760814131` (with --timestamp=unix); empty when never started."""
    try:
        return datetime.fromtimestamp(int(value.lstrip("@")), tz=timezone.utc) if value else None
    except ValueError:
        return None
