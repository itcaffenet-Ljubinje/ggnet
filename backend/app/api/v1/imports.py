"""
API for importing image files as new masters (Images -> Import), see
app.services.imports:

  GET    /imports                    the imports folder's image files and the USB disks
  POST   /imports/usb/{part}/open    mount a USB file system read-only; its image files
  POST   /imports/usb/{part}/eject   unmount it
  PUT    /imports/upload/{filename}  upload a file into the imports folder (the body is the file)
  DELETE /imports/file?path=...      delete a file from the imports folder
  POST   /imports                    import a file: a new disk, filled in the background
  POST   /imports/{disk_id}/cancel   stop a running import
"""

from __future__ import annotations

import logging
import math
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_disk_importer, get_provisioner, get_usb_disks
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import GameDiskOut, ImageFileOut, ImportIn, ImportSources, UsbFolderOut
from app.db.models import DiskKind, GameDisk
from app.db.session import get_db
from app.services import imports
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.api.imports")

router = APIRouter(prefix="/imports", tags=["imports"])

_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,199}$")
GiB = 1024 ** 3


def _folder(prov: Provisioner) -> Path | None:
    """The imports folder: the mountpoint of storage.imports, when it exists."""
    mount = prov.zfs.get_property(prov.zfs.layout.imports, "mountpoint")
    return Path(mount) if mount.startswith("/") else None


def _files(found: list[imports.ImageFile]) -> list[ImageFileOut]:
    return [ImageFileOut(**vars(f)) for f in found]


@router.get("", response_model=ImportSources)
def sources(prov: Provisioner = Depends(get_provisioner), usb=Depends(get_usb_disks)):
    folder = _folder(prov)
    return ImportSources(
        folder=str(folder) if folder else None,
        files=_files(imports.list_images(folder)) if folder and folder.is_dir() else [],
        usb=usb.list(),
    )


@router.post("/usb/{part}/open", response_model=UsbFolderOut)
def open_usb(part: str, usb=Depends(get_usb_disks)):
    try:
        path = usb.mount(part)
    except ValueError:
        not_found("USB file system", part)
    except RuntimeError as e:
        host_failed(str(e))
    return UsbFolderOut(part=part, folder=str(path), files=_files(imports.list_images(path)))


@router.post("/usb/{part}/eject", status_code=status.HTTP_204_NO_CONTENT)
def eject_usb(part: str, db: Session = Depends(get_db), usb=Depends(get_usb_disks)):
    busy = db.scalar(select(GameDisk).where(GameDisk.copy_source.like(f"{usb.mounts / part}/%"),
                                            GameDisk.copy_error.is_(None)))
    if busy is not None:
        conflict(f"'{busy.name}' is still being imported from it ({busy.copy_percent or 0}%)")
    try:
        usb.unmount(part)
    except ValueError:
        not_found("USB file system", part)
    except RuntimeError as e:
        host_failed(str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/upload/{filename}", response_model=ImageFileOut, status_code=status.HTTP_201_CREATED)
async def upload(filename: str, request: Request, prov: Provisioner = Depends(get_provisioner)):
    """
    The request body is the file. It is written as .<name>.part (not listed)
    and renamed when complete, so a broken upload never looks like an image.
    """
    folder = _folder(prov)
    if folder is None or not folder.is_dir():
        host_failed(f"The imports folder ({prov.zfs.layout.imports}) does not exist on the host")
    if not _FILE_NAME.match(filename) or imports.image_format(Path(filename)) is None:
        conflict(f"{filename!r}: the name must end in {', '.join(imports.FORMATS)} and have no path in it")
    target = folder / filename
    if target.exists():
        conflict(f"{filename} is already in the imports folder")
    size = int(request.headers.get("content-length") or 0)
    if size and size > shutil.disk_usage(folder).free:
        conflict(f"{filename} ({size // GiB} GB) does not fit in the imports folder")
    part = folder / f".{filename}.part"
    try:
        with part.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        part.rename(target)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    logger.info("Uploaded %s (%d bytes) into %s", filename, target.stat().st_size, folder)
    return ImageFileOut(path=str(target), name=filename, size=target.stat().st_size,
                        format=imports.image_format(target))


@router.delete("/file", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(path: str, db: Session = Depends(get_db), prov: Provisioner = Depends(get_provisioner)):
    folder = _folder(prov)
    file = Path(path)
    if folder is None or not imports.inside(file, folder) or not file.is_file():
        not_found("File in the imports folder", path)
    if db.scalar(select(GameDisk).where(GameDisk.copy_source == str(file), GameDisk.copy_error.is_(None))):
        conflict(f"{file.name} is being imported")
    file.unlink()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", response_model=GameDiskOut, status_code=status.HTTP_201_CREATED)
def start_import(
    body: ImportIn,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
    importer=Depends(get_disk_importer),
):
    """
    A new disk of `kind` filled from an image file in the imports folder or
    on an opened USB disk; it shows its progress and is published at @base.
    """
    file = Path(body.path)
    folder = _folder(prov)
    allowed = (folder is not None and imports.inside(file, folder)) or imports.inside(file, imports.USB_MOUNTS)
    fmt = imports.image_format(file)
    if not allowed or fmt is None or not file.is_file():
        not_found("Image file", body.path)
    if db.scalar(select(GameDisk).where(GameDisk.name == body.name)):
        conflict(f"Game disk '{body.name}' already exists")
    path = prov.image_path(body.name)
    owner = db.scalar(select(GameDisk).where(GameDisk.zvol_path == path))
    if owner is not None:
        conflict(f"'{body.name}' is still the zvol name of '{owner.name}' (renamed); choose another name")
    size = imports.virtual_size(prov.zfs.runner, str(file), fmt)
    if not size:
        conflict(f"{file.name} is not a readable {fmt} image")
    size_gb = math.ceil(size / GiB)
    try:
        prov.create_disk(body.name, size_gb, os_image=body.kind is DiskKind.OS)
    except ProvisioningError as e:
        host_failed(str(e))
    disk = GameDisk(name=body.name, zvol_path=path, size_gb=size_gb, kind=body.kind,
                    copy_source=str(file.resolve()), copy_percent=0)
    db.add(disk)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prov.delete_disk(path, published=False)
        conflict(f"Game disk '{body.name}' already exists")
    logger.info("Import of %s (%s, %d GB) into '%s' started", file, fmt, size_gb, disk.name)
    importer.start(disk.id, imports.ImportOptions(prepare=body.prepare, defender_off=body.defender_off,
                                                  services_off=body.services_off))
    db.refresh(disk)
    return disk


@router.post("/{disk_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_import(disk_id: int, db: Session = Depends(get_db)):
    """Stop a running import; the disk then shows "Cancelled" and can be removed."""
    disk = db.get(GameDisk, disk_id)
    if disk is None:
        not_found("Game disk", disk_id)
    if not disk.copying or not str(disk.copy_source).startswith("/"):
        conflict(f"'{disk.name}' is not being imported")
    if not imports.cancel(disk_id):
        conflict(f"The import of '{disk.name}' does not run in this server process")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
