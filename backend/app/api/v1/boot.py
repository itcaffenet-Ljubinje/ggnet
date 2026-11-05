"""
Boot Mode: the iPXE script for one PC, by its MAC address.

iPXE (with the script embedded at build time, docs/architecture.md section 4)
asks `GET /api/v1/boot/ipxe?mac=aa-bb-cc-dd-ee-ff` right after DHCP. The
answer is always a script, never an HTTP error, so a PC that ggNet does not
boot falls through to its next boot device instead of stopping in iPXE:

  Boot Mode PC with its disks   sanboot from the shared target, LUN 0 (OS);
                                Windows sees LUN 1 (game disk) on the same session
  Boot Mode PC without disks    message, then exit
  Disk Mode PC                  exit (boot from the local disk)
  unknown MAC                   listed in Discovery (Machines → New PCs) and,
                                waits on screen until the admin
                                registers it, asking iPXE again every 10 s; a
                                key press boots the local disk instead.
                                `[boot] unregistered = "local"` exits at once
                                (also set in the web UI: Settings → Network boot).

autoexec.ipxe (scripts/install_ggnet.sh) also sends the boot NIC's PCI IDs
(pci, cls, sub, rev); the server keeps them as the PC's `boot_nic`. A PC
whose card its OS image cannot start waits on screen until the Add new
hardware wizard has installed the card in the image (_new_hardware).

Before it answers a provisioned PC, the server discards its writebacks like
the watcher does after a disconnect (app.services.writebacks), unless the PC
keeps its writeback. No authentication yet (planned with JWT); the script
only names the PC's own initiator IQN, which the ACL is bound to.
"""

from __future__ import annotations

import ipaddress
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    get_driver_library,
    get_hardware_adder,
    get_provisioner,
    get_pxe_service,
    get_writeback_settings,
)
from app.api.v1.errors import not_found
from app.config import get_config
from app.api.v1.schemas import _mac
from app.db.models import DiscoverySource, Machine, MachineMode, MachineStatus
from app.db.session import get_db
from app.services import discovery, pxe, scan
from app.services.toolchain import activate_when_done
from app.services.provisioning import Provisioner
from app.services.pxe import PxeService
from app.services.writebacks import WritebackSettings, on_ipxe
from app.windows_setup import NIC_ID_RE

logger = logging.getLogger("ggnet.api.boot")

router = APIRouter(prefix="/boot", tags=["boot"])


def _script(*lines: str) -> PlainTextResponse:
    return PlainTextResponse("#!ipxe\n" + "\n".join(lines) + "\n")


def exit_script(message: str) -> PlainTextResponse:
    """Leave iPXE: the firmware goes on with its next boot device."""
    return _script(f"echo {message}", "sleep 5", "exit")


WAIT_SECONDS = 10


def wait_script(mac: str, again_url: str, message: list[str] | None = None,
                waiting: str = "Waiting for registration") -> PlainTextResponse:
    """
    An unregistered PC waits until it is registered: every WAIT_SECONDS it
    asks for its script again (the same URL), so it boots as soon as the admin
    adds it, with no reboot. Any key leaves iPXE for the local disk.
    """
    message = message or [
        f"ggNet: this PC is not registered (MAC {mac}).",
        "Register it in the ggNet web UI: Machines -> New PCs. It boots as soon as it is registered.",
    ]
    return _script(
        *(f"echo {line}" for line in message),
        ":wait",
        f"prompt --timeout {WAIT_SECONDS * 1000} {waiting}. Press any key to boot from the local disk. "
        "&& goto local || goto again",
        ":again",
        f"chain --replace --autofree {again_url} || goto wait",
        ":local",
        "echo Booting from the local disk.",
        "exit",
    )


def unregistered_mode(cfg: dict | None = None) -> str:
    """`[boot] unregistered`: "wait" (the default) or "local"."""
    value = str(((cfg if cfg is not None else get_config()).get("boot") or {}).get("unregistered") or "wait")
    if value not in ("wait", "local"):
        logger.error("Ignoring invalid [boot] unregistered %r; using wait", value)
        return "wait"
    return value


NO_GATEWAY = "0.0.0.0"


def boot_gateway(cfg: dict | None = None) -> str | None:
    """
    The gateway iPXE puts into the iBFT for Windows; None keeps the DHCP one.

    By default 0.0.0.0. With the router's gateway in the iBFT, Windows lost
    its iSCSI disk as it took over from iPXE (INACCESSIBLE_BOOT_DEVICE;
    vip-05, 2026-10-16, A/B on the same image); with 0.0.0.0 it boots, and
    Windows still gets its default route and internet afterwards.
    `[boot] gateway` in config.toml: "none" (the default), "dhcp" (the
    router's gateway), or an IP (e.g. the ggNet server with IP forwarding,
    the usual setup). An invalid value falls back to the default.
    """
    value = str(((cfg if cfg is not None else get_config()).get("boot") or {}).get("gateway") or "").strip()
    if value in ("", "none"):
        return NO_GATEWAY
    if value == "dhcp":
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        logger.error("Ignoring invalid [boot] gateway %r; using %s", value, NO_GATEWAY)
        return NO_GATEWAY


def sanboot_script(m: Machine, portal_ip: str, portal_port: int, target_iqn: str,
                   gateway: str | None = None) -> PlainTextResponse:
    """
    No `--no-describe`: Windows needs the iBFT table iPXE writes to keep
    booting from iSCSI. `initiator-iqn` must be the PC's ACL, or LIO refuses
    the login. `gateway` (boot_gateway()) replaces the DHCP gateway in it.
    """
    host = f"[{portal_ip}]" if ":" in portal_ip else portal_ip
    os_disk, game = m.os_disk, m.game_disk
    os_version = (m.os_clone_snapshot or "").partition("@")[2]
    game_version = (m.clone_snapshot or "").partition("@")[2]
    return _script(
        *([f"set netX/gateway {gateway}"] if gateway else []),
        f"set initiator-iqn {m.initiator_iqn}",
        f"set root-path iscsi:{host}::{portal_port}:0:{target_iqn}",
        f"echo ggNet: booting {m.name} ({os_disk.name} {os_version}, {game.name} {game_version})",
        "sanboot ${root-path} || goto failed",
        ":failed",
        "echo Boot failed, retrying in 10 s",
        "sleep 10",
        "reboot",
    )


def boot_nic_id(pci: str, cls: str, sub: str, rev: str) -> str | None:
    """
    The boot NIC's hardware ID as Windows names it, from PCI config space as
    iPXE reads it (hexraw, highest byte first): `pci` offset 0, 4 bytes
    (device, vendor), `cls` offset 9, 3 bytes (class, subclass, interface),
    `sub` offset 0x2c, 4 bytes (subsystem, subsystem vendor), `rev` offset 8.
    None unless it is a network controller (class 02), so a misread device
    never gets a network card's driver.
    """
    parts = [v.strip().lower().replace(":", "") for v in (pci, cls, sub, rev)]
    if [len(v) for v in parts] != [8, 6, 8, 2] or not all(
            all(c in "0123456789abcdef" for c in v) for v in parts):
        return None
    pci, cls, sub, rev = (v.upper() for v in parts)
    if not cls.startswith("02") or pci[4:] in ("FFFF", "0000"):
        return None
    return f"VEN_{pci[4:]}&DEV_{pci[:4]}&SUBSYS_{sub}&REV_{rev}"


def _find(db: Session, mac: str) -> Machine | None:
    """The machine with this MAC set by the admin, else the one whose agent reported it."""
    return (db.scalar(select(Machine).where(Machine.mac == mac))
            or db.scalar(select(Machine).where(Machine.reported_mac == mac).order_by(Machine.id)))


# What autoexec.ipxe (scripts/install_ggnet.sh) reads from the boot NIC's PCI
# config space before it asks for the script; empty when it could not.
NIC_QUERY = "&pci=${ggnet_pci}&cls=${ggnet_cls}&sub=${ggnet_sub}&rev=${ggnet_rev}"


@router.get("/ipxe", response_class=PlainTextResponse)
def ipxe_script(
    request: Request,
    mac: str = "",
    pci: str = "",
    cls: str = "",
    sub: str = "",
    rev: str = "",
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    settings: WritebackSettings = Depends(get_writeback_settings),
    adder=Depends(get_hardware_adder),
    drivers: scan.DriverLibrary = Depends(get_driver_library),
):
    try:
        mac = _mac(mac)
    except ValueError:
        mac = None
    if mac is None:
        return exit_script("ggNet: no valid MAC address in the request. Booting from local disk.")

    # [boot] settings: config.toml's, or the ones saved in the web UI.
    cfg = pxe.effective_config(db)
    nic = boot_nic_id(pci, cls, sub, rev)
    m = _find(db, mac)
    if m is None:
        logger.info("iPXE request from unregistered MAC %s", mac)
        discovery.record(db, mac, DiscoverySource.PXE,
                         ip_address=request.client.host if request.client else None, boot_nic=nic)
        if unregistered_mode(cfg) == "local":
            return exit_script(f"This PC is not registered in ggNet ({mac}). Booting from local disk.")
        # ${net0/mac} is iPXE's, so the PC asks with its own MAC like the first time.
        return wait_script(mac, f"{request.base_url}api/v1/boot/ipxe?mac=${{net0/mac}}{NIC_QUERY}")
    if m.mode is not MachineMode.BOOT:
        return exit_script(f"ggNet: {m.name} is in Disk Mode. Booting from local disk.")
    if m.status is not MachineStatus.PROVISIONED or m.os_clone_zvol is None:
        reason = m.last_error if m.status is MachineStatus.ERROR else "it has no OS image and game disk"
        logger.warning("iPXE request from %s, which cannot boot: %s", m.name, reason)
        return exit_script(f"ggNet: {m.name} cannot boot from the network: {reason}")

    again = f"{request.base_url}api/v1/boot/ipxe?mac=${{net0/mac}}{NIC_QUERY}"
    held = _new_hardware(request, db, prov, adder, drivers, m, nic or m.boot_nic, mac, again, cfg)
    if held is not None:
        return held
    if not on_ipxe(db, prov, m, settings, boot_nic=nic):
        logger.error("iPXE request from %s: %s", m.name, m.last_error)
        return exit_script(f"ggNet: {m.name} cannot boot from the network: {m.last_error}")
    logger.info("iPXE: %s boots %s + %s", m.name, m.os_clone_snapshot, m.clone_snapshot)
    return sanboot_script(m, prov.iscsi.portal_ip, prov.iscsi.portal_port, m.iscsi_target_iqn,
                          boot_gateway(cfg))


@router.get("/secureboot.cer")
def secure_boot_certificate(service: PxeService = Depends(get_pxe_service)):
    """
    The ggNet Secure Boot certificate (DER), public: each PC enrolls it once in
    MokManager (Enroll key from disk) so shim starts ggNet's signed iPXE.
    """
    cert = service.certificate
    if cert is None:
        not_found("Secure Boot certificate in", str(service.secureboot_dir))
    return FileResponse(cert, media_type="application/pkix-cert", filename="ggnet-sb.cer")


# ── New hardware: hardware scan (WinPE) and Add new hardware ──────────

def _new_hardware(request: Request, db: Session, prov: Provisioner, adder, drivers: scan.DriverLibrary,
                  m: Machine, nic: str | None, mac: str, again: str, cfg: dict) -> PlainTextResponse | None:
    """
    What a PC gets instead of its Windows: Add new hardware, or a wait screen
    while its image cannot start its boot network card. None: boot as usual.

    The card is added to the image only by the Add new hardware wizard
    (app.services.toolchain.HardwareWizard), once per board type. While the
    admin has it open, the first PC that boots the image is taken, whatever
    the image knows: it reads its hardware in Windows PE, waits while its card
    is installed in a new version, and boots that. Other PCs boot as usual,
    or wait on screen while the image cannot start their card.
    """
    disk = m.os_disk
    if disk is None or prov.windows is None or nic is None or not NIC_ID_RE.match(nic):
        return None
    zvol = disk.zvol_path
    job = adder.job(zvol)
    wizard = adder.wizard(zvol)
    if wizard is not None and wizard.active and wizard.machine is None:
        wizard.machine = m.name                # the PC the admin powered on
        logger.info("Add new hardware wizard on %s takes %s", disk.name, m.name)
    if wizard is not None and wizard.active and wizard.machine == m.name:
        held = _wizard_step(request, db, prov, adder, drivers, wizard, m, nic, mac, again)
        if held is not None:
            return held
    card = (nic, m.nic_instance)
    status = scan.nic_status(prov, zvol, disk.snapshot, [card])
    if isinstance(status, str):
        logger.warning("iPXE: cannot check whether %s can boot %s: %s", m.name, disk.name, status)
        return None
    if status[card] == "ready":
        return None
    if job is not None and job.running:
        return _hardware_wait(mac, again, disk.name)
    busy = wizard.machine if wizard is not None and wizard.active else None
    if m.name not in adder.waiting_machines(zvol):
        logger.info("iPXE: %s waits: its network card PCI\\%s is not in %s", m.name, nic, disk.name)
    adder.note_waiting(zvol, m.name)
    return _needs_hardware(mac, again, m.name, nic, disk.name, busy=busy)


def _wizard_step(request: Request, db: Session, prov: Provisioner, adder, drivers: scan.DriverLibrary,
                 wizard, m: Machine, nic: str, mac: str, again: str) -> PlainTextResponse | None:
    """The wizard's PC asks iPXE: scan it, add its card, or (new version made) let it boot."""
    disk = m.os_disk
    if wizard.step in ("waiting", "scanning"):
        folder = scan.winpe_dir(get_config())
        if scan.winpe_ready(folder) and _scanner().is_file():
            chip = scan.chip_key(nic)
            match = drivers.match(chip)
            package, infs = match if match else (None, [])
            wizard.step = "scanning"
            logger.info("iPXE: %s gets the hardware scan for %s (PCI\\%s; WinPE driver %s)",
                        m.name, disk.name, nic, f"{package}: {', '.join(infs)}" if package else "none in the library")
            return PlainTextResponse(scan.ipxe_scan_script(str(request.base_url), mac, chip, package,
                                                           drivers.files(package) if package else []))
        if m.nic_instance is None:
            wizard.fail(f"the hardware scan needs Windows PE in {folder} and {scan.SCANNER_EXE}")
            logger.warning("iPXE: %s needs the hardware scan: %s", m.name, wizard.error)
            return exit_script(f"ggNet: {wizard.error}. Booting from the local disk.")
        wizard.step, wizard.card = "collected", (nic, m.nic_instance)      # known already: no scan
    if wizard.step == "collected":
        card = wizard.card or (nic, m.nic_instance)
        versions = prov.zfs.list_snapshots(disk.zvol_path) or []
        if not versions or versions[-1] != disk.snapshot:
            wizard.fail(f"@{disk.snapshot} is active but not the newest version of {disk.name}; "
                        "make the newest active first")
            return exit_script(f"ggNet: Add new hardware stopped: {wizard.error}.")
        done = activate_when_done(sessionmaker(bind=db.get_bind(), expire_on_commit=False), disk.id, disk.snapshot)
        wizard.card, wizard.step = card, "adding"
        try:
            adder.start(disk.zvol_path, [card], done)
            logger.info("iPXE: Add new hardware started for %s: PCI\\%s\\%s into %s",
                        m.name, card[0], card[1], disk.name)
        except ValueError:
            pass                                   # started meanwhile
        if not wizard.active:                      # failed at once (tests run the job here)
            return exit_script(f"ggNet: adding the network card of {m.name} to {disk.name} failed: {wizard.error}.")
        return _hardware_wait(mac, again, disk.name)
    if wizard.step == "adding":
        return _hardware_wait(mac, again, disk.name)
    if wizard.step == "rebooting":
        wizard.step, wizard.finished = "done", time.time()
        logger.info("Add new hardware wizard on %s: %s boots the new version", disk.name, m.name)
    return None


def _hardware_wait(mac: str, again: str, image: str) -> PlainTextResponse:
    return wait_script(mac, again, [
        f"ggNet: adding this PC's network card to the image {image} (a few minutes).",
        "ggNet: the PC boots by itself when it is done.",
    ], waiting="Adding the network card")


def _needs_hardware(mac: str, again: str, name: str, nic: str, image: str,
                    busy: str | None = None) -> PlainTextResponse:
    """The image cannot start this PC's card: say what to do, and go on once it is done."""
    todo = (f"ggNet: Add new hardware is adding the card of {busy} now; then start it again for this PC."
            if busy else f"ggNet: in the ggNet web UI open Images -> {image} -> Add new hardware.")
    return wait_script(mac, again, [
        # No '&' on an iPXE command line: it could be read as an operator.
        f"ggNet: the network card of {name} ({nic.replace('&', ' ')}) is not in the image {image}.",
        todo,
        "ggNet: this PC goes on by itself.",
    ], waiting="Waiting for Add new hardware")


def _scanner():
    from pathlib import Path

    from app.services.agent_package import default_package

    return Path(default_package().directory) / scan.SCANNER_EXE


@router.get("/winpe/{name}")
def winpe_file(name: str):
    """wimboot and the Windows PE files of the hardware scan ([paths] winpe_dir)."""
    path = scan.winpe_file(scan.winpe_dir(get_config()), name)
    if path is None:
        not_found("Windows PE file", name)
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/scan/winpeshl.ini", response_class=PlainTextResponse)
def scan_winpeshl():
    """Windows PE runs only the scanner."""
    return PlainTextResponse(scan.WINPESHL_INI)


@router.get(f"/scan/{scan.SCANNER_EXE}")
def scan_exe():
    path = _scanner()
    if not path.is_file():
        not_found("Hardware scanner", scan.SCANNER_EXE)
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/scan/config")
def scan_config(request: Request, mac: str = "", chip: str = "",
                drivers: scan.DriverLibrary = Depends(get_driver_library)):
    """ggnet-scan.json for one PC: this server, its MAC and the network driver to load (for its chip)."""
    try:
        mac = _mac(mac)
    except ValueError:
        not_found("PC with MAC", mac)
    found = drivers.match(chip)
    return Response(scan.scan_config(str(request.base_url), mac, found[1] if found else []),
                    media_type="application/json")


@router.get("/scan/driver/{package}/{name}")
def scan_driver(package: str, name: str, drivers: scan.DriverLibrary = Depends(get_driver_library)):
    path = drivers.file(package, name)
    if path is None:
        not_found("Driver file", f"{package}/{name}")
    return FileResponse(path, media_type="application/octet-stream")


class ScanIn(BaseModel):
    mac: str
    scanner_version: str = ""
    devices: list[dict] = Field(default_factory=list, max_length=1000)
    adapters: list[dict] = Field(default_factory=list, max_length=100)


@router.post("/scan")
def scan_report(body: ScanIn, db: Session = Depends(get_db), adder=Depends(get_hardware_adder)):
    """
    The hardware scan's report: the PC's boot network card and its instance ID
    are kept, so its next boot adds the card to its OS image.
    """
    try:
        mac = _mac(body.mac)
    except ValueError:
        return {"message": f"ggNet: '{body.mac}' is not a MAC address."}
    m = _find(db, mac)
    if m is None:
        logger.warning("Hardware scan from unregistered MAC %s", mac)
        return {"message": f"ggNet: this PC ({mac}) is not registered; register it in the web UI."}
    card = scan.choose_card(body.devices, m.boot_nic)
    nets = [d.get("instance_id") for d in body.devices if scan.is_network(d)]
    if card is None:
        logger.warning("Hardware scan of %s: boot network card not found among %s", m.name, nets)
        return {"message": "ggNet: the boot network card was not found among: " + ", ".join(map(str, nets))}
    m.boot_nic = m.boot_nic or card.nic
    m.nic_instance = card.instance
    db.commit()
    wizard = adder.wizard(m.os_disk.zvol_path) if m.os_disk else None
    if wizard is not None and wizard.active and wizard.machine == m.name:
        wizard.step, wizard.card = "collected", (card.nic, card.instance)
    logger.info("Hardware scan of %s (scanner %s): boot network card PCI\\%s\\%s; %d PCI devices",
                m.name, body.scanner_version or "?", card.nic, card.instance, len(body.devices))
    image = m.os_disk.name if m.os_disk else "its image"
    return {"message": f"ggNet: network card PCI\\{card.nic}\\{card.instance} saved for {m.name}; "
                       f"it is added to {image} after the restart."}
