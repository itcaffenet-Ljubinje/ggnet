"""
API for the hardware scan's Windows PE (Settings -> Hardware scan), see
app.services.winpe:

  GET    /winpe                               status: Windows PE files, the ISOs it can come from,
                                              the driver library and the PCs' network chips
  POST   /winpe/from-iso                      copy Windows PE out of a Windows ISO
  POST   /winpe/drivers/{package}/upload      start uploading a driver package
  PUT    /winpe/drivers/{package}/files/{name}  one file of it (the body is the file)
  POST   /winpe/drivers/{package}             check the uploaded files and add the package
  DELETE /winpe/drivers/{package}             remove a package
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_driver_library, get_provisioner
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import (
    IsoOut,
    WinPeChipOut,
    WinPeDeviceOut,
    WinPeFileOut,
    WinPeFromIsoIn,
    WinPeOut,
    WinPePackageOut,
)
from app.config import get_config
from app.db.models import Machine
from app.db.session import get_db
from app.services import scan, winpe
from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.api.winpe")

router = APIRouter(prefix="/winpe", tags=["winpe"])


def iso_folders(prov: Provisioner) -> list[Path]:
    """Where Windows ISOs are looked for: `[paths] iso_dirs` (Proxmox's ISO storage) and the imports folder."""
    paths = get_config().get("paths") or {}
    folders = [Path(p) for p in paths.get("iso_dirs", winpe.DEFAULT_ISO_DIRS)]
    try:
        mount = prov.zfs.get_property(prov.zfs.layout.imports, "mountpoint")
    except Exception:
        mount = ""
    if mount.startswith("/"):
        folders.append(Path(mount))
    return folders


def get_iso_folders(prov: Provisioner = Depends(get_provisioner)) -> list[Path]:
    return iso_folders(prov)


def get_winpe_setup(prov: Provisioner = Depends(get_provisioner)) -> winpe.WinPeSetup:
    return winpe.WinPeSetup(prov.zfs.runner, scan.winpe_dir(get_config()))


def _uploads(library: scan.DriverLibrary = Depends(get_driver_library)) -> winpe.DriverUploads:
    return winpe.DriverUploads(library)


def _package_out(info: winpe.PackageInfo) -> WinPePackageOut:
    return WinPePackageOut(name=info.name, files=len(info.files), size=info.size,
                           devices=[WinPeDeviceOut(chip=c, vendor=winpe.vendor(c), name=n)
                                    for c, n in sorted(info.devices.items())])


def _out(db: Session, library: scan.DriverLibrary, folders: list[Path]) -> WinPeOut:
    from app.api.v1.boot import _scanner

    folder = scan.winpe_dir(get_config())
    files = []
    for name in scan.WINPE_FILES:
        path = scan.winpe_file(folder, name)
        files.append(WinPeFileOut(name=name, present=path is not None,
                                  size=path.stat().st_size if path is not None else None))
    packages = []
    for p in library.packages():
        try:
            packages.append(winpe.package_info(p))
        except OSError:
            continue
    covered = {chip: (info.name, desc) for info in packages for chip, desc in info.devices.items()}
    chips: Counter[str] = Counter()
    for nic in db.scalars(select(Machine.boot_nic).where(Machine.boot_nic.is_not(None))):
        try:
            chips[scan.chip_key(nic)] += 1
        except ValueError:
            continue
    scanner = _scanner().is_file()
    return WinPeOut(
        folder=str(folder),
        ready=all(f.present for f in files) and scanner,
        files=files,
        scanner=scanner,
        isos=[IsoOut(**vars(i)) for i in winpe.list_isos(folders)],
        iso_folders=[str(f) for f in folders],
        drivers_folder=str(library.root),
        packages=[_package_out(p) for p in packages],
        pc_chips=[WinPeChipOut(chip=chip, vendor=winpe.vendor(chip), name=covered.get(chip, (None, None))[1],
                               package=covered.get(chip, (None, None))[0], pcs=n)
                  for chip, n in sorted(chips.items())],
    )


@router.get("", response_model=WinPeOut)
def status_(db: Session = Depends(get_db), library=Depends(get_driver_library),
            folders: list[Path] = Depends(get_iso_folders)):
    return _out(db, library, folders)


@router.post("/from-iso", response_model=WinPeOut)
def from_iso(body: WinPeFromIsoIn, db: Session = Depends(get_db), library=Depends(get_driver_library),
             folders: list[Path] = Depends(get_iso_folders), setup=Depends(get_winpe_setup)):
    """Copy BCD, boot.sdi and boot.wim out of a Windows ISO listed by GET /winpe (takes a minute)."""
    if body.path not in {i.path for i in winpe.list_isos(folders)}:
        not_found("ISO", body.path)
    try:
        setup.from_iso(Path(body.path))
    except winpe.WinPeError as e:
        conflict(f"{Path(body.path).name}: {e}")
    except OSError as e:
        host_failed(f"Copying Windows PE failed: {e}")
    return _out(db, library, folders)


@router.post("/drivers/{package}/upload", status_code=status.HTTP_204_NO_CONTENT)
def start_upload(package: str, uploads: winpe.DriverUploads = Depends(_uploads)):
    try:
        uploads.start(package)
    except winpe.WinPeError as e:
        conflict(str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/drivers/{package}/files/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_file(package: str, name: str, request: Request,
                      uploads: winpe.DriverUploads = Depends(_uploads)):
    size = int(request.headers.get("content-length") or 0)
    if size > winpe.MAX_FILE_BYTES:
        conflict(f"{name} is larger than {winpe.MAX_FILE_BYTES // 1024 ** 2} MB")
    body = await request.body()
    try:
        uploads.write(package, name, [body])
    except winpe.WinPeError as e:
        conflict(str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/drivers/{package}", response_model=WinPePackageOut, status_code=status.HTTP_201_CREATED)
def finish_upload(package: str, uploads: winpe.DriverUploads = Depends(_uploads)):
    try:
        return _package_out(uploads.finish(package))
    except winpe.WinPeError as e:
        conflict(str(e))


@router.delete("/drivers/{package}", status_code=status.HTTP_204_NO_CONTENT)
def remove_package(package: str, uploads: winpe.DriverUploads = Depends(_uploads)):
    try:
        removed = uploads.remove(package)
    except winpe.WinPeError:
        removed = False
    if not removed:
        not_found("Driver package", package)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
