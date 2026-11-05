"""
Copy of an image: one version of a master becomes a new, independent master
(ZFSManager.copy_snapshot), in the background because a large game disk takes
a long time.

The new disk is in the database from the start with `copy_source` set and
`copy_percent` following the bytes sent; it cannot be filled, published or
assigned until the copy ends. Then it is published at @base. A failed copy
keeps its record with `copy_error` until it is removed. A copy that was
running when ggNet stopped is marked failed at the next start (recover()).

The provisioner's lock is not held during the copy: it would block every
other host operation for hours. The target's name is reserved by its database
record, so nothing else creates it meanwhile.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import GameDisk
from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.copies")

# The database is updated at most this often with the progress.
PROGRESS_EVERY = 2.0


class DiskCopier:
    def __init__(self, make_session: sessionmaker[Session], prov: Provisioner, background: bool = True):
        self.make_session = make_session
        self.prov = prov
        self.background = background     # tests run the copy in the request
        self._threads: dict[int, threading.Thread] = {}

    def start(self, disk_id: int) -> None:
        if not self.background:
            self.run(disk_id)
            return
        t = threading.Thread(target=self.run, args=(disk_id,), name=f"ggnet-copy-{disk_id}", daemon=True)
        self._threads[disk_id] = t
        t.start()

    def running(self, disk_id: int) -> bool:
        t = self._threads.get(disk_id)
        return t is not None and t.is_alive()

    def run(self, disk_id: int) -> None:
        with self.make_session() as db:
            disk = db.get(GameDisk, disk_id)
            if disk is None or not disk.copying:
                return
            source, target = disk.copy_source, disk.zvol_path
        logger.info("Copying %s to %s", source, target)
        started = time.monotonic()
        ok = self.prov.copy_disk(source, target, self._progress(disk_id))
        with self.make_session() as db:
            disk = db.get(GameDisk, disk_id)
            if disk is None:
                return
            if ok:
                disk.snapshot, disk.copy_source, disk.copy_percent = "base", None, None
                logger.info("Copy of %s to %s done in %.0f s", source, target, time.monotonic() - started)
            else:
                disk.copy_percent = None
                disk.copy_error = (self.prov.zfs.runner.last_error or "the copy failed")[:500]
            db.commit()

    def _progress(self, disk_id: int) -> Callable[[int, int], None]:
        last = [0.0, -1]

        def update(done: int, total: int) -> None:
            percent = int(100 * done / total) if total else 0
            now = time.monotonic()
            if percent == last[1] or (now - last[0] < PROGRESS_EVERY and percent < 100):
                return
            last[0], last[1] = now, percent
            with self.make_session() as db:
                disk = db.get(GameDisk, disk_id)
                if disk is not None and disk.copying:
                    disk.copy_percent = percent
                    db.commit()

        return update


def recover(make_session: sessionmaker[Session], prov: Provisioner) -> None:
    """Copies that were running when ggNet stopped: failed, partial zvol removed."""
    with make_session() as db:
        for disk in db.scalars(select(GameDisk).where(GameDisk.copy_source.is_not(None),
                                                      GameDisk.copy_error.is_(None))):
            disk.copy_error = "Interrupted: ggNet stopped while it ran. Remove this disk and start again."
            disk.copy_percent = None
            if prov.zfs.dataset_exists(disk.zvol_path) and not prov.zfs.destroy(disk.zvol_path, recursive=True):
                logger.error("Could not remove the partial copy %s", disk.zvol_path)
            logger.warning("Copy or import into %s was interrupted", disk.zvol_path)
        db.commit()
