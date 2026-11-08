"""
Copy of an image: one version of a master becomes a new, independent master
(ZFSManager.copy_snapshot), or the master with all its versions does
(ZFSManager.copy_image), in the background because a large game image takes
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

from app.db.models import Image
from app.services.provisioning import Provisioner

logger = logging.getLogger("ggnet.copies")

# The database is updated at most this often with the progress.
PROGRESS_EVERY = 2.0
# copy_source of a copy with every version: "versions:<master>@<newest>#<active>".
ALL_VERSIONS = "versions:"


class DiskCopier:
    def __init__(self, make_session: sessionmaker[Session], prov: Provisioner, background: bool = True):
        self.make_session = make_session
        self.prov = prov
        self.background = background     # tests run the copy in the request
        self._threads: dict[int, threading.Thread] = {}

    def start(self, image_id: int) -> None:
        if not self.background:
            self.run(image_id)
            return
        t = threading.Thread(target=self.run, args=(image_id,), name=f"ggnet-copy-{image_id}", daemon=True)
        self._threads[image_id] = t
        t.start()

    def running(self, image_id: int) -> bool:
        t = self._threads.get(image_id)
        return t is not None and t.is_alive()

    def run(self, image_id: int) -> None:
        with self.make_session() as db:
            disk = db.get(Image, image_id)
            if disk is None or not disk.copying:
                return
            source, target = disk.copy_source, disk.zvol_path
        logger.info("Copying %s to %s", source, target)
        started = time.monotonic()
        everything = source.startswith(ALL_VERSIONS)
        if everything:
            newest, _, active = source[len(ALL_VERSIONS):].partition("#")
            ok = self.prov.copy_all_versions(newest, target, self._progress(image_id))
        else:
            ok = self.prov.copy_disk(source, target, self._progress(image_id))
        with self.make_session() as db:
            disk = db.get(Image, image_id)
            if disk is None:
                return
            if ok:
                disk.snapshot = (active or newest.partition("@")[2]) if everything else "base"
                disk.copy_source, disk.copy_percent = None, None
                logger.info("Copy of %s to %s done in %.0f s", source, target, time.monotonic() - started)
            else:
                disk.copy_percent = None
                disk.copy_error = (self.prov.zfs.runner.last_error or "the copy failed")[:500]
            db.commit()

    def _progress(self, image_id: int) -> Callable[[int, int], None]:
        last = [0.0, -1]

        def update(done: int, total: int) -> None:
            percent = int(100 * done / total) if total else 0
            now = time.monotonic()
            if percent == last[1] or (now - last[0] < PROGRESS_EVERY and percent < 100):
                return
            last[0], last[1] = now, percent
            with self.make_session() as db:
                disk = db.get(Image, image_id)
                if disk is not None and disk.copying:
                    disk.copy_percent = percent
                    db.commit()

        return update


def recover(make_session: sessionmaker[Session], prov: Provisioner) -> None:
    """Copies that were running when ggNet stopped: failed, partial zvol removed."""
    with make_session() as db:
        for disk in db.scalars(select(Image).where(Image.copy_source.is_not(None),
                                                      Image.copy_error.is_(None))):
            disk.copy_error = "Interrupted: ggNet stopped while it ran. Remove this disk and start again."
            disk.copy_percent = None
            if prov.zfs.dataset_exists(disk.zvol_path) and not prov.zfs.destroy(disk.zvol_path, recursive=True):
                logger.error("Could not remove the partial copy %s", disk.zvol_path)
            logger.warning("Copy or import into %s was interrupted", disk.zvol_path)
        db.commit()
