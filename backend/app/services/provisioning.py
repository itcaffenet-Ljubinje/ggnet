"""
Provisioner: host operations for game disks and machines, always in the
order the rules require. It knows nothing about the database: it takes
values and returns a result, or raises ProvisioningError with the reason.

All operations go through one lock: one backend process, ~17 machines, and
parallel zfs/targetcli calls on the same zvol would collide.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.iscsi_manager import Disk, ISCSIManager
from app.zfs_manager import ZFSManager

logger = logging.getLogger("ggnet.provisioning")


class ProvisioningError(Exception):
    """A host operation failed. The message is shown to the user."""


@dataclass
class ClientDisk:
    clone_zvol: str
    clone_snapshot: str
    iscsi_target_iqn: str


class Provisioner:
    def __init__(self, zfs: ZFSManager, iscsi: ISCSIManager):
        self.zfs = zfs
        self.iscsi = iscsi
        self.lock = threading.Lock()

    # ── Helpers ───────────────────────────────────────────────────────

    def _fail(self, msg: str, runner=None) -> ProvisioningError:
        detail = (runner or self.zfs.runner).last_error
        full = f"{msg}: {detail}" if detail else msg
        logger.error(full)
        return ProvisioningError(full)

    def _clear(self) -> None:
        self.zfs.runner.last_error = ""
        self.iscsi.runner.last_error = ""

    def image_path(self, disk_name: str) -> str:
        return f"{self.zfs.layout.images}/{disk_name}"

    def client_path(self, machine_name: str) -> str:
        return f"{self.zfs.layout.writebacks}/{machine_name}"

    def _detach(self, machine_name: str, initiator_iqn: str) -> None:
        """
        Remove the machine's ACL and backstores and verify the backstore is
        really gone: LIO keeps the zvol open while it exists, so destroying
        the clone would fail with "dataset is busy" (or, worse, the client
        would keep a session to a disk that is being replaced).
        """
        self.iscsi.detach(machine_name, initiator_iqn)
        if self.iscsi.backstore_exists(machine_name, "game") is not False:
            raise self._fail("The iSCSI disk was not removed", self.iscsi.runner)

    # ── Game disk (master) ────────────────────────────────────────────

    def create_disk(self, name: str, size_gb: int) -> str:
        """Empty zvol for a master (draft). Returns the ZFS path."""
        path = self.image_path(name)
        with self.lock:
            self._clear()
            if self.zfs.dataset_exists(path):
                raise ProvisioningError(f"Zvol {path} already exists on the host")
            if not self.zfs.create_zvol(path, size_gb):
                raise self._fail(f"Creating zvol {path} failed")
        return path

    def publish_disk(self, zvol_path: str, snap_name: str = "base") -> str:
        """publish_master(): snapshot → hold → readonly."""
        with self.lock:
            self._clear()
            if not self.zfs.publish_master(zvol_path, snap_name):
                raise self._fail(f"Publishing master {zvol_path}@{snap_name} failed")
        return snap_name

    def delete_disk(self, zvol_path: str, published: bool) -> None:
        """
        A published master goes only through destroy_master(); a draft has
        no snapshots or holds, so a plain destroy is enough.
        """
        with self.lock:
            self._clear()
            if not self.zfs.dataset_exists(zvol_path):
                logger.warning("Zvol %s does not exist on the host; deleting the record only", zvol_path)
                return
            ok = (self.zfs.destroy_master(zvol_path) if published
                  else self.zfs.destroy(zvol_path, recursive=True))
            if not ok:
                raise self._fail(f"Deleting {zvol_path} failed")

    # ── Machines ──────────────────────────────────────────────────────

    def provision(self, machine_name: str, initiator_iqn: str, snapshot_path: str) -> ClientDisk:
        """
        Clone of the snapshot, exposed on the shared iSCSI target as mapped
        LUN 0 of an ACL for the machine's initiator IQN.
        """
        clone = self.client_path(machine_name)
        with self.lock:
            self._clear()
            if self.zfs.dataset_exists(clone):
                raise ProvisioningError(
                    f"Clone {clone} already exists on the host; deprovision first"
                )
            if not self.zfs.clone(snapshot_path, clone):
                raise self._fail(f"Clone {snapshot_path} -> {clone} failed")
            iqn = self.iscsi.attach(
                machine_name, initiator_iqn, [Disk("game", self.zfs.zvol_device_path(clone))]
            )
            if iqn is None:
                err = self._fail("Exposing the disk over iSCSI failed", self.iscsi.runner)
                # attach() cleaned up its own iSCSI state; roll back the clone.
                if not self.zfs.destroy(clone, recursive=True):
                    logger.error("Rollback: clone %s left on the host", clone)
                raise err
        return ClientDisk(clone, snapshot_path, iqn)

    def reset(
        self, machine_name: str, initiator_iqn: str, clone_zvol: str, snapshot_path: str
    ) -> ClientDisk:
        """
        Order: detach (ACL + backstore) → destroy clone → clone → attach.
        `snapshot_path` is the disk's CURRENT snapshot, so a reset also moves
        the machine to the newest version of the master.
        The client must be powered off or disconnected.
        """
        with self.lock:
            self._clear()
            self._detach(machine_name, initiator_iqn)
            if not self.zfs.reset_clone(clone_zvol, snapshot_path):
                raise self._fail(f"Resetting clone {clone_zvol} failed")
            iqn = self.iscsi.attach(
                machine_name, initiator_iqn, [Disk("game", self.zfs.zvol_device_path(clone_zvol))]
            )
            if iqn is None:
                raise self._fail("Exposing the disk over iSCSI failed", self.iscsi.runner)
        return ClientDisk(clone_zvol, snapshot_path, iqn)

    def deprovision(self, machine_name: str, initiator_iqn: str, clone_zvol: str | None) -> None:
        """Detach from iSCSI, then delete the clone. Safe to call when nothing exists."""
        clone = clone_zvol or self.client_path(machine_name)
        with self.lock:
            self._clear()
            self._detach(machine_name, initiator_iqn)
            if self.zfs.dataset_exists(clone) and not self.zfs.destroy(clone, recursive=True):
                raise self._fail(f"Deleting clone {clone} failed")
