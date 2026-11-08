"""
GET /tasks: what runs on the server in the background, for the web UI's
Background tasks panel: backups and restores (app.services.backups), copies
and imports of images (Image.copy_source), Add new hardware
(app.services.toolchain). Jobs that ended in the last RECENT minutes are
listed too, so the panel can say how they ended.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_hardware_adder
from app.api.v1.schemas import TaskOut
from app.db.models import BackupJob, Image
from app.db.session import get_db
from app.services.backups import BACKUP_SOURCE

router = APIRouter(prefix="/tasks", tags=["tasks"])

RECENT = timedelta(minutes=15)
_HARDWARE_STEPS = {
    "starting": "starting", "job": "copying the newest version", "vm": "Windows installs the card in a VM",
    "result": "reading what Windows did", "prepare": "preparing the new version", "apply": "making it the next version",
    "done": "new version made",
}


def _utc(t: float | None) -> datetime | None:
    return datetime.fromtimestamp(t, timezone.utc) if t else None


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _copy_detail(source: str) -> str:
    from app.services.copies import ALL_VERSIONS

    if source.startswith(ALL_VERSIONS):
        return f"from {source[len(ALL_VERSIONS):].partition('@')[0].rsplit('/', 1)[-1]}, all versions"
    return f"from {source.rsplit('/', 1)[-1]}"


@router.get("", response_model=list[TaskOut])
def tasks(db: Session = Depends(get_db), adder=Depends(get_hardware_adder)):
    now = datetime.now(timezone.utc)
    out: list[TaskOut] = []
    for job in db.scalars(select(BackupJob).where(or_(BackupJob.status.in_(("queued", "running")),
                                                      BackupJob.finished_at >= now - RECENT))
                          .order_by(BackupJob.id)):
        total = job.total_bytes or None
        percent = min(100, int(100 * job.done_bytes / total)) if total else None
        out.append(TaskOut(
            id=f"backup-{job.id}", kind=job.kind,
            title=(f"Export of {job.image_name}@{job.snapshot} ({(job.format or '').upper()})"
                   if job.kind == "export" else
                   f"{'Backup' if job.kind == 'backup' else 'Restore'} of {job.image_name}"),
            detail=job.location_label, status=job.status,
            percent=100 if job.status == "done" else percent, done_bytes=job.done_bytes, total_bytes=total,
            started_at=_aware(job.started_at), finished_at=_aware(job.finished_at), error=job.error,
            cancel=f"/api/v1/backups/jobs/{job.id}/cancel" if job.status in ("queued", "running") else None,
            image_id=job.image_id))
    for image in db.scalars(select(Image).where(Image.copy_source.is_not(None), Image.copy_error.is_(None))):
        source = image.copy_source or ""
        if source.startswith(BACKUP_SOURCE):
            continue                     # its restore job is listed above
        importing = source.startswith("/")
        out.append(TaskOut(
            id=f"{'import' if importing else 'copy'}-{image.id}", kind="import" if importing else "copy",
            title=f"{'Import' if importing else 'Copy'} into {image.name}",
            detail=source.rsplit("/", 1)[-1] if importing else _copy_detail(source),
            status="running", percent=image.copy_percent,
            cancel=f"/api/v1/imports/{image.id}/cancel" if importing else None, image_id=image.id))
    names = {i.zvol_path: i for i in db.scalars(select(Image))}
    for master, job in list(adder.jobs.items()):
        ended = _utc(job.finished)
        if not job.running and (ended is None or now - ended > RECENT):
            continue
        image = names.get(master)
        status = "running" if job.running else ("failed" if job.error else "done")
        out.append(TaskOut(
            id=f"hardware-{image.id if image else master}", kind="hardware",
            title=f"Add new hardware to {image.name if image else master.rsplit('/', 1)[-1]}",
            detail=_HARDWARE_STEPS.get(job.step, job.step), status=status,
            started_at=_utc(job.started), finished_at=ended, error=job.error,
            cancel=f"/api/v1/images/{image.id}/add-hardware/cancel" if image and job.running else None,
            image_id=image.id if image else None))
    return out
