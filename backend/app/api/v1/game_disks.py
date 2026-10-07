"""
API for game disks (master images).

Flow: POST (empty zvol, draft) → fill with data (upload, a later step) →
POST /publish (@base, hold, readonly) → assign to machines.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_provisioner
from app.api.v1.errors import conflict, host_failed, not_found
from app.api.v1.schemas import GameDiskCreate, GameDiskOut
from app.db.models import GameDisk, Machine
from app.db.session import get_db
from app.services.provisioning import Provisioner, ProvisioningError

logger = logging.getLogger("ggnet.api.game_disks")

router = APIRouter(prefix="/game-disks", tags=["game-disks"])


def _get(db: Session, disk_id: int) -> GameDisk:
    disk = db.get(GameDisk, disk_id)
    if disk is None:
        not_found("Game disk", disk_id)
    return disk


@router.get("", response_model=list[GameDiskOut])
def list_disks(db: Session = Depends(get_db)):
    return db.scalars(select(GameDisk).order_by(GameDisk.name)).all()


@router.get("/{disk_id}", response_model=GameDiskOut)
def get_disk(disk_id: int, db: Session = Depends(get_db)):
    return _get(db, disk_id)


@router.post("", response_model=GameDiskOut, status_code=status.HTTP_201_CREATED)
def create_disk(
    body: GameDiskCreate,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    if db.scalar(select(GameDisk).where(GameDisk.name == body.name)):
        conflict(f"Game disk '{body.name}' already exists")
    try:
        path = prov.create_disk(body.name, body.size_gb)
    except ProvisioningError as e:
        host_failed(str(e))

    disk = GameDisk(name=body.name, zvol_path=path, size_gb=body.size_gb)
    db.add(disk)
    try:
        db.commit()
    except IntegrityError:
        # A parallel request with the same name: do not leave an orphan on the host.
        db.rollback()
        try:
            prov.delete_disk(path, published=False)
        except ProvisioningError:
            logger.error("Zvol %s left on the host without a database record", path)
        conflict(f"Game disk '{body.name}' already exists")
    return disk


@router.post("/{disk_id}/publish", response_model=GameDiskOut)
def publish_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    disk = _get(db, disk_id)
    if disk.published:
        conflict(f"Game disk '{disk.name}' is already published (@{disk.snapshot})")
    try:
        disk.snapshot = prov.publish_disk(disk.zvol_path)
    except ProvisioningError as e:
        host_failed(str(e))
    db.commit()
    return disk


@router.delete("/{disk_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_disk(
    disk_id: int,
    db: Session = Depends(get_db),
    prov: Provisioner = Depends(get_provisioner),
):
    disk = _get(db, disk_id)
    assigned = db.scalar(
        select(func.count()).select_from(Machine).where(Machine.game_disk_id == disk.id)
    )
    if assigned:
        conflict(f"Game disk '{disk.name}' is assigned to {assigned} machine(s); move them first")
    try:
        prov.delete_disk(disk.zvol_path, disk.published)
    except ProvisioningError as e:
        host_failed(str(e))
    db.delete(disk)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
