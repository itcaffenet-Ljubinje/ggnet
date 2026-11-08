"""
API for Images -> Backup / Restore (app.services.backups):

  GET  /backups/locations               USB disks and backup folders
  POST /backups/locations/{id}/open     mount it (writable=true for a backup); free space and its backups
  POST /backups/locations/{id}/eject    unmount a USB disk
  GET  /backups/estimate?image_id=      a backup's size, with every version
  POST /backups                         back up images (one job each, run one after another)
  POST /backups/restore                 restore a backup into a new image
  POST /backups/export                  one version into a VHDX or VHD file
  GET  /backups/jobs                    the history
  POST /backups/jobs/{id}/cancel        stop a queued or running job
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_backup_locations, get_backup_runner, get_provisioner
from app.api.v1.errors import conflict, not_found
from app.api.v1.schemas import (
    BackupEstimateOut,
    BackupJobOut,
    BackupLocationOpenOut,
    BackupLocationOut,
    BackupStartIn,
    ExportIn,
    FoundBackupOut,
    RestoreIn,
)
from app.db.models import BackupJob, Image
from app.db.session import get_db
from app.services import backups
from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.api.backups")

router = APIRouter(prefix="/backups", tags=["backups"])

JOBS_SHOWN = 100


def _found(b: backups.FoundBackup) -> FoundBackupOut:
    image = b.manifest.get("image") or {}
    return FoundBackupOut(folder=str(b.folder), image=str(image.get("name", "?")), kind=str(image.get("kind", "game")),
                          created_at=str(b.manifest.get("created_at", "")),
                          versions=[str(v) for v in b.manifest.get("versions") or []],
                          active=image.get("snapshot"), bytes=b.size, complete=b.complete, problem=b.problem)


@router.get("/locations", response_model=list[BackupLocationOut])
def locations(exports: bool = False, locs=Depends(get_backup_locations)):
    """Where backups go; with exports=true also the imports folder (an exported file can be imported again)."""
    return [BackupLocationOut(**x) for x in locs.list(exports=exports)]


@router.post("/locations/{location}/open", response_model=BackupLocationOpenOut)
def open_location(location: str, writable: bool = False, locs=Depends(get_backup_locations)):
    try:
        path = locs.open(location, writable)
    except backups.BackupError as e:
        conflict(str(e))
    return BackupLocationOpenOut(id=location, path=str(path), free_bytes=shutil.disk_usage(path).free,
                                 backups=[_found(b) for b in backups.find_backups(path)])


@router.post("/locations/{location}/eject", status_code=status.HTTP_204_NO_CONTENT)
def eject(location: str, db: Session = Depends(get_db), locs=Depends(get_backup_locations)):
    busy = db.scalar(select(BackupJob).where(BackupJob.location == location,
                                             BackupJob.status.in_(("queued", "running"))))
    if busy is not None:
        conflict(f"a {busy.kind} of {busy.image_name} still uses it; wait or cancel it first")
    try:
        locs.eject(location)
    except backups.BackupError as e:
        conflict(str(e))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/estimate", response_model=BackupEstimateOut)
def estimate(image_id: int, db: Session = Depends(get_db), runner=Depends(get_backup_runner)):
    image = db.get(Image, image_id)
    if image is None:
        not_found("Image", image_id)
    return BackupEstimateOut(image_id=image_id, bytes=runner.estimate(image) if image.published else None)


@router.post("", response_model=list[BackupJobOut], status_code=status.HTTP_201_CREATED)
def start_backup(body: BackupStartIn, db: Session = Depends(get_db), runner=Depends(get_backup_runner),
                 locs=Depends(get_backup_locations)):
    """One backup job per image, in the order given; they run one after another."""
    if not body.image_ids:
        conflict("Choose the images to back up")
    images = []
    for image_id in body.image_ids:
        image = db.get(Image, image_id)
        if image is None:
            not_found("Image", image_id)
        if not image.published:
            conflict(f"'{image.name}' has no versions yet; publish it first")
        images.append(image)
    try:
        locs.open(body.location, True)      # mounted writable now: a wrong disk fails here, not later
    except backups.BackupError as e:
        conflict(str(e))
    label = locs.label(body.location)
    jobs = [BackupJob(kind="backup", image_id=i.id, image_name=i.name, location=body.location,
                      location_label=label) for i in images]
    db.add_all(jobs)
    db.commit()
    logger.info("Backup of %s to %s queued", ", ".join(i.name for i in images), label)
    runner.kick()
    for job in jobs:
        db.refresh(job)
    return jobs


@router.post("/restore", response_model=BackupJobOut, status_code=status.HTTP_201_CREATED)
def start_restore(body: RestoreIn, db: Session = Depends(get_db), runner=Depends(get_backup_runner),
                  prov: Provisioner = Depends(get_provisioner), locs=Depends(get_backup_locations)):
    """A new image from a backup; it is in Images as "Restoring" until the job is done."""
    try:
        path = locs.open(body.location, False)
    except backups.BackupError as e:
        conflict(str(e))
    backup = next((b for b in backups.find_backups(path) if str(b.folder) == body.folder), None)
    if backup is None:
        not_found("Backup", body.folder)
    if not backup.complete:
        conflict(f"{Path(body.folder).name}: {backup.problem}")
    if db.scalar(select(Image).where(Image.name == body.name)):
        conflict(f"Image '{body.name}' already exists; restore it under another name")
    zvol = prov.image_path(body.name)
    if db.scalar(select(Image).where(Image.zvol_path == zvol)) or prov.zfs.dataset_exists(zvol):
        conflict(f"'{body.name}' is still taken on the server (a renamed image's zvol); choose another name")
    info = backup.manifest.get("image") or {}
    image = Image(name=body.name, zvol_path=zvol, size_gb=int(info.get("size_gb") or 1),
                  kind=backups.kind_of(backup.manifest), copy_source=f"{backups.BACKUP_SOURCE}{body.folder}",
                  copy_percent=0)
    db.add(image)
    db.flush()
    job = BackupJob(kind="restore", image_id=image.id, image_name=body.name, location=body.location,
                    location_label=locs.label(body.location), folder=body.folder, total_bytes=backup.size)
    db.add(job)
    db.commit()
    logger.info("Restore of %s into '%s' queued", body.folder, body.name)
    runner.kick()
    db.refresh(job)
    return job


@router.post("/export", response_model=BackupJobOut, status_code=status.HTTP_201_CREATED)
def start_export(body: ExportIn, db: Session = Depends(get_db), runner=Depends(get_backup_runner),
                 prov: Provisioner = Depends(get_provisioner), locs=Depends(get_backup_locations)):
    """
    One version of an image into a dynamic VHDX or VHD file; the image is only
    read. Refused before it starts when the file cannot fit: VHD holds at most
    2040 GB, FAT32 takes no file over 4 GB, and the space must be free.
    """
    image = db.get(Image, body.image_id)
    if image is None:
        not_found("Image", body.image_id)
    if not image.published:
        conflict(f"'{image.name}' has no versions yet; publish it first")
    snapshot = body.snapshot or image.snapshot
    details = prov.zfs.snapshot_details(image.zvol_path) or []
    if snapshot not in {d["name"] for d in details}:
        not_found("Snapshot", f"{image.name}@{snapshot}")
    if not body.file_name.lower().endswith(f".{body.format}"):
        conflict(f"The file name must end in .{body.format}")
    volume = image.size_gb * 1024 ** 3
    if body.format == "vhd" and volume > backups.VHD_MAX_BYTES:
        conflict(f"A VHD holds at most 2040 GB and {image.name} is {image.size_gb} GB; choose VHDX")
    # The file holds the version's data, uncompressed (it is at most the disk's size).
    used = prov.zfs.get_bytes(f"{image.zvol_path}@{snapshot}", "logicalreferenced")
    size = min(volume, used) if used is not None else volume
    if locs.fstype(body.location) == "vfat" and size > backups.FAT32_MAX_BYTES:
        conflict(f"The file would be about {size / 1024 ** 3:.0f} GB, and this USB disk has FAT32, which takes "
                 "no file over 4 GB. Use a disk with exFAT, NTFS or ext4, or the imports folder.")
    try:
        path = locs.open(body.location, True)
    except backups.BackupError as e:
        conflict(str(e))
    out = path / body.file_name
    if out.exists():
        conflict(f"{body.file_name} already exists there; choose another name")
    if size + backups.SPARE_BYTES > shutil.disk_usage(path).free:
        conflict(f"Not enough free space: the file can take up to {size / 1024 ** 3:.1f} GB, "
                 f"{shutil.disk_usage(path).free / 1024 ** 3:.1f} GB is free there")
    job = BackupJob(kind="export", image_id=image.id, image_name=image.name, location=body.location,
                    location_label=locs.label(body.location), folder=str(out), snapshot=snapshot,
                    format=body.format, total_bytes=size)
    db.add(job)
    db.commit()
    logger.info("Export of %s@%s to %s queued", image.name, snapshot, out)
    runner.kick()
    db.refresh(job)
    return job


@router.get("/jobs", response_model=list[BackupJobOut])
def jobs(db: Session = Depends(get_db)):
    return db.scalars(select(BackupJob).order_by(BackupJob.id.desc()).limit(JOBS_SHOWN)).all()


@router.post("/jobs/{job_id}/cancel", response_model=BackupJobOut)
def cancel(job_id: int, db: Session = Depends(get_db), runner=Depends(get_backup_runner)):
    if db.get(BackupJob, job_id) is None:
        not_found("Backup job", job_id)
    if not runner.cancel(job_id):
        conflict("The job is not running or queued")
    db.expire_all()
    return db.get(BackupJob, job_id)
