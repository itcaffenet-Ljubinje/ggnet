"""
Provisioner: host operations for game disks and machines, always in the
order the rules require. It knows nothing about the database: it takes
values and returns a result, or raises ProvisioningError with the reason.

All operations go through one lock: one backend process, ~17 machines, and
parallel zfs/targetcli calls on the same zvol would collide.

Spare writebacks: a Boot Mode PC that restarts waits at iPXE while its
writebacks are reset. So a spare of each, `<clone>_spare`, is made ahead in
the background (make_spares: cloned from the version the PC should run and,
for the OS, already set up as that PC), and a reset only swaps it in
(destroy the old clone, rename the spare) instead of cloning and setting up
Windows. A spare is used only while its origin and, for the OS, its setup
tag (SETUP_PROP) still match; otherwise the reset clones as before. Spares
are made under the machine's own lock only, so making one never holds up
another PC's reset.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.disk_grow import GrowResult, PartitionGrower
from app.iscsi_manager import SLOTS, Disk, ISCSIManager
from app.windows_setup import ImagePrep, OsSetup, WindowsSetup
from app.zfs_manager import ZFSManager

logger = logging.getLogger("ggnet.provisioning")


class ProvisioningError(Exception):
    """A host operation failed. The message is shown to the user."""


class PartialApplyError(ProvisioningError):
    """
    Apply Writebacks failed after some writebacks already became versions.
    `applied` maps each such master to its new snapshot name; those clones
    are gone (they are the masters now), so the machine needs a reset.
    """

    def __init__(self, msg: str, applied: dict[str, str]):
        super().__init__(msg)
        self.applied = applied


@dataclass(frozen=True)
class WritebackApply:
    """One kept writeback and the master whose next version it becomes."""
    clone_zvol: str
    master: str
    base_snap: str     # the master's active snapshot the clone was made from


_VERSION_RE = re.compile(r"^v(\d+)$")

SPARE_SUFFIX = "_spare"            # '_' never appears in a machine name
SETUP_PROP = "ggnet:setup"         # on an OS spare: which setup it was made with
SPARE_RETRY = 600.0                # seconds before spares that failed are tried again


def setup_tag(setup: OsSetup | None) -> str:
    """A short fingerprint of what personalize() writes, to tell a stale OS spare."""
    if setup is None:
        return "none"
    data = json.dumps([setup.computer_name, sorted(setup.env.items())])
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def next_version(snapshots: list[str]) -> str:
    """Name of the next image version: `base` counts as v1, so base → v2 → v3 ..."""
    numbers = [1 if s == "base" else int(m.group(1))
               for s in snapshots if s == "base" or (m := _VERSION_RE.match(s))]
    return f"v{max(numbers, default=0) + 1}"


@dataclass
class ClientDisk:
    clone_zvol: str
    clone_snapshot: str
    iscsi_target_iqn: str
    # Boot Mode only: the machine's writeback of its OS image (mapped LUN 0).
    os_clone_zvol: str | None = None
    os_clone_snapshot: str | None = None
    # Why the fresh OS writeback could not be set up as this PC (it boots
    # with the image's name then); None when it was, or nothing was asked.
    os_setup_problem: str | None = None


class Provisioner:
    def __init__(self, zfs: ZFSManager, iscsi: ISCSIManager, windows: WindowsSetup | None = None):
        self.zfs = zfs
        self.iscsi = iscsi
        # Boot Mode: writes the PC's identity into its fresh OS writeback.
        self.windows = windows
        # Grows a disk's data partition after its zvol got bigger (grow_disk).
        self.grower = PartitionGrower(zfs.runner)
        self.lock = threading.Lock()
        # Per machine: a reset and the making of its spares never overlap.
        self._machine_locks: dict[str, threading.Lock] = {}
        self._machine_locks_guard = threading.Lock()
        # Per machine: when making its spares last failed (retried after SPARE_RETRY).
        self._spare_failed: dict[str, float] = {}

    def _machine_lock(self, name: str) -> threading.Lock:
        with self._machine_locks_guard:
            return self._machine_locks.setdefault(name, threading.Lock())

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

    def os_client_path(self, machine_name: str) -> str:
        """Boot Mode OS writeback; machine names never end in `-os`, so no game clone collides."""
        return f"{self.zfs.layout.writebacks}/{machine_name}-os"

    def _disks(self, clone: str, os_clone: str | None) -> list[Disk]:
        """The machine's LUNs: Disk Mode game = LUN 0; Boot Mode OS = LUN 0, game = LUN 1."""
        game = Disk("game", self.zfs.zvol_device_path(clone))
        if os_clone is None:
            return [game]
        return [Disk("os", self.zfs.zvol_device_path(os_clone)), game]

    def _set_up_os(self, os_clone: str | None, os_setup: OsSetup | None,
                   label: str | None = None) -> str | None:
        """Personalize a fresh OS clone before it is mapped (app.windows_setup)."""
        if os_clone is None or os_setup is None or self.windows is None:
            return None
        device = self.zfs.zvol_device_path(os_clone)
        problem = (self.windows.personalize(device, os_setup, label=label) if label
                   else self.windows.personalize(device, os_setup))
        if problem:
            logger.warning("Windows setup of %s is incomplete: %s", os_setup.computer_name, problem)
        return problem

    def _detach(self, machine_name: str, initiator_iqn: str, save: bool = True, legacy: bool = True) -> None:
        """
        Remove the machine's ACL and backstores and verify the backstores are
        really gone: LIO keeps a zvol open while its backstore exists, so
        destroying the clone would fail with "dataset is busy" (or, worse, the
        client would keep a session to a disk that is being replaced).
        `save=False` when the disks are mapped again right after (attach saves);
        `legacy=False` when the machine is known to be on the shared target.
        """
        names = self.iscsi.remove(machine_name, initiator_iqn, save=save, legacy=legacy)
        if names is None or any(self.iscsi.backstore_name(machine_name, slot) in names for slot in SLOTS):
            raise self._fail("The iSCSI disk was not removed", self.iscsi.runner)

    # ── Spare writebacks ──────────────────────────────────────────────

    @staticmethod
    def spare_path(clone: str) -> str:
        return clone + SPARE_SUFFIX

    def _spare_ok(self, spare: str, snapshot: str, tag: str | None = None) -> bool:
        """Whether `spare` exists, comes from `snapshot` and (OS) was set up with `tag`."""
        if not self.zfs.dataset_exists(spare) or self.zfs.get_property(spare, "origin") != snapshot:
            return False
        return tag is None or self.zfs.get_property(spare, SETUP_PROP) == tag

    def _swap_in_spare(self, clone: str) -> bool:
        """Replace the clone with its spare (destroy, rename); False leaves no spare behind."""
        spare = self.spare_path(clone)
        if self.zfs.dataset_exists(clone) and not self.zfs.destroy(clone, recursive=True):
            return False
        if self.zfs.rename_dataset(spare, clone):
            return True
        self.zfs.destroy(spare, recursive=True)
        return False

    def make_spares(
        self, machine_name: str, clone_zvol: str, snapshot_path: str,
        os_clone_zvol: str | None = None, os_snapshot_path: str | None = None,
        os_setup: OsSetup | None = None,
    ) -> list[str]:
        """
        Make (or renew) the spares of a machine's writebacks; a current one is
        kept. Runs in the background, under the machine's lock only. Returns
        the spares made. A spare that cannot be set up completely is removed:
        a reset then clones as before.
        """
        wanted = [(clone_zvol, snapshot_path, None)]
        if os_clone_zvol is not None and os_snapshot_path is not None:
            wanted.append((os_clone_zvol, os_snapshot_path, os_setup))
        made = []
        failed_at = self._spare_failed.get(machine_name)
        if failed_at is not None and time.monotonic() - failed_at < SPARE_RETRY:
            return made
        failed = False
        with self._machine_lock(machine_name):
            for clone, snap, setup in wanted:
                spare = self.spare_path(clone)
                tag = setup_tag(setup) if clone == os_clone_zvol else None
                if self._spare_ok(spare, snap, tag):
                    continue
                if self.zfs.dataset_exists(spare) and not self.zfs.destroy(spare, recursive=True):
                    logger.warning("Could not remove the stale spare %s", spare)
                    failed = True
                    continue
                if not self.zfs.clone(snap, spare):
                    logger.warning("Could not make the spare %s", spare)
                    failed = True
                    continue
                if tag is not None:
                    problem = self._set_up_os(spare, setup, label=f"{machine_name}-spare")
                    if problem or not self.zfs.set_property(spare, SETUP_PROP, tag):
                        self.zfs.destroy(spare, recursive=True)
                        failed = True
                        continue
                made.append(spare)
        if failed:
            # Not again every round: a reset clones as before meanwhile.
            self._spare_failed[machine_name] = time.monotonic()
            logger.warning("Spare writebacks of %s failed; next try in %d min", machine_name, SPARE_RETRY // 60)
        else:
            self._spare_failed.pop(machine_name, None)
        if made:
            logger.info("Spare writebacks ready for %s", machine_name)
        return made

    def drop_spares(self, machine_name: str, *clones: str | None) -> None:
        """Remove a machine's spares (it keeps its writebacks, or is gone)."""
        with self._machine_lock(machine_name):
            for clone in filter(None, clones):
                spare = self.spare_path(clone)
                if self.zfs.dataset_exists(spare) and not self.zfs.destroy(spare, recursive=True):
                    logger.warning("Could not remove the spare %s", spare)

    # ── Game disk (master) ────────────────────────────────────────────

    def create_disk(self, name: str, size_gb: int, os_image: bool = False) -> str:
        """
        Empty zvol for a master (draft). Returns the ZFS path. An OS image
        gets 16k blocks for Windows' small random I/O, a game disk 64k for
        large game files read in one go; both lz4 and sync=standard (a
        master must survive a host crash). See docs/architecture.md, "Properties".
        """
        path = self.image_path(name)
        props = dict(volblocksize="16k" if os_image else "64k", compression="lz4", sync="standard")
        with self.lock:
            self._clear()
            if self.zfs.dataset_exists(path):
                raise ProvisioningError(f"Zvol {path} already exists on the host")
            if not self.zfs.create_zvol(path, size_gb, **props):
                raise self._fail(f"Creating zvol {path} failed")
        return path

    def grow_disk(self, master: str, size_gb: int, published: bool, extend: bool = True
                  ) -> tuple[str | None, GrowResult | None]:
        """
        Make a disk bigger. A draft grows in place. A published master gets a
        new version, as Prepare image does: its newest version is cloned, the
        clone grown (and its data partition with it, `extend`, see
        app.disk_grow) and made the next version. Returns (the new version or None, what was done to
        the partition or None). Nothing changes on failure.
        """
        size = f"{size_gb}G"
        if not published:
            with self.lock:
                self._clear()
                if not self.zfs.set_property(master, "volsize", size):
                    raise self._fail(f"Growing {master} to {size} failed")
            return None, None
        tmp = f"{self.zfs.layout.writebacks}/{master.rsplit('/', 1)[-1]}_ggnet_grow"   # '_': never a PC
        with self.lock:
            self._clear()
            snaps = self.zfs.list_snapshots(master)
            if not snaps:
                raise self._fail(f"Listing versions of {master} failed")
            base, new = snaps[-1], next_version(snaps)
            if self.zfs.dataset_exists(tmp) and not self.zfs.destroy(tmp):
                raise self._fail(f"{tmp} is left over from an earlier resize and cannot be removed")
            if not self.zfs.clone(f"{master}@{base}", tmp):
                raise self._fail(f"Cloning {master}@{base} failed")
            result: GrowResult | None = None
            ok = self.zfs.set_property(tmp, "volsize", size)
            if ok and extend:
                self.zfs.runner.run(["udevadm", "settle"])
                result = self.grower.extend_last(self.zfs.zvol_device_path(tmp))
            if ok and self.zfs.apply_clone(tmp, master, base, new):
                return new, result
            err = self._fail(f"Growing {master} to {size} failed")
            if not self.zfs.destroy(tmp):
                logger.error("Could not remove %s after a failed resize", tmp)
            raise err

    def _on_version(self, master: str, snapshot: str, what: str, work: Callable[[str], object]):
        """
        `work(device)` on a temporary clone of one version of an image (a
        snapshot has no device of its own), removed after. A string result
        says why the clone could not be made.
        """
        tmp = f"{self.zfs.layout.writebacks}/{master.rsplit('/', 1)[-1]}_ggnet_hw"   # '_': never a PC
        with self.lock:
            self._clear()
            if self.zfs.dataset_exists(tmp) and not self.zfs.destroy(tmp):
                return f"{tmp} is left over from an earlier check and cannot be removed"
            if not self.zfs.clone(f"{master}@{snapshot}", tmp):
                return f"Cloning {master}@{snapshot} failed: {self.zfs.runner.last_error}"
            try:
                self.zfs.runner.run(["udevadm", "settle"])
                return work(self.zfs.zvol_device_path(tmp))
            finally:
                if not self.zfs.destroy(tmp):
                    logger.error("Could not remove %s after the %s", tmp, what)

    def check_hardware(self, master: str, snapshot: str, nics: list[tuple[str, str | None]]
                       ) -> dict[tuple[str, str | None], str] | str:
        """WindowsSetup.nic_support() on one version of an OS image."""
        if self.windows is None:
            return "Windows setup is not available on this server"
        windows = self.windows
        return self._on_version(master, snapshot, "hardware check", lambda device: windows.nic_support(device, nics))

    def import_disk(self, source: str, fmt: str, target: str, progress=None, cancel=None) -> bool:
        """
        Fill the new, empty master `target` from an image file and publish it
        at @base. Without the lock while the data is written (it can take an
        hour). The zvol is removed again when anything fails.
        """
        from app.services.imports import IMPORT_TIMEOUT, qemu_percent

        def line(text: str) -> None:
            percent = qemu_percent(text)
            if progress and percent is not None:
                progress(percent)

        runner = self.zfs.runner
        ok, _, err = runner.run_stream(
            ["qemu-img", "convert", "-p", "-n", "--target-is-zero", "-f", fmt, "-O", "raw",
             source, self.zfs.zvol_device_path(target)],
            line, timeout=IMPORT_TIMEOUT, cancel=cancel,
        )
        if cancel is not None and cancel.is_set():
            err = "Cancelled"
        if ok:
            try:
                self.publish_disk(target)
                return True
            except ProvisioningError as e:
                err = str(e)
        logger.error("Importing %s into %s failed: %s", source, target, err)
        if self.zfs.dataset_exists(target) and not self.zfs.destroy(target, recursive=True):
            logger.error("Could not remove %s after a failed import", target)
        runner.last_error = err
        return False

    def copy_disk(self, snapshot: str, target: str, progress=None) -> bool:
        """
        A full, independent copy of one version as the master `target`, at
        @base (ZFSManager.copy_snapshot). Without the lock: it can take hours.
        """
        return self.zfs.copy_snapshot(snapshot, target, progress)

    def publish_disk(self, zvol_path: str, snap_name: str = "base") -> str:
        """publish_master(): snapshot → hold → readonly."""
        with self.lock:
            self._clear()
            if not self.zfs.publish_master(zvol_path, snap_name):
                raise self._fail(f"Publishing master {zvol_path}@{snap_name} failed")
        return snap_name

    def snapshots(self, zvol_path: str) -> list[dict]:
        """Versions of a master with their sizes and clones (see ZFSManager.snapshot_details)."""
        details = self.zfs.snapshot_details(zvol_path)
        if details is None:
            raise self._fail(f"Listing versions of {zvol_path} failed")
        for d in details:            # a spare is not a PC using the version
            d["clones"] = [c for c in d["clones"] if not c.endswith(SPARE_SUFFIX)]
        return details

    def _drop_spares_from(self, snapshot: str) -> None:
        """Remove the spares made from a version about to be deleted (the maker renews them)."""
        for c in self.zfs.list_clients():
            if c["zvol"].endswith(SPARE_SUFFIX) and c["cloned_from"] == snapshot:
                if not self.zfs.destroy(c["zvol"], recursive=True):
                    logger.warning("Could not remove the spare %s", c["zvol"])

    def writebacks(self) -> list[dict]:
        """Every client clone on the host with its size and origin (spares not included)."""
        return [c for c in self.zfs.list_clients() if not c["zvol"].endswith(SPARE_SUFFIX)]

    def set_comment(self, zvol_path: str, name: str, comment: str) -> None:
        """Set (or with "" remove) the comment of one version."""
        with self.lock:
            self._clear()
            if not self.zfs.set_snapshot_comment(f"{zvol_path}@{name}", comment):
                raise self._fail(f"Setting the comment of {zvol_path}@{name} failed")

    def set_locked(self, zvol_path: str, name: str, locked: bool) -> None:
        """Lock one version against deletion, or unlock it."""
        with self.lock:
            self._clear()
            if not self.zfs.set_snapshot_lock(f"{zvol_path}@{name}", locked):
                raise self._fail(f"{'Locking' if locked else 'Unlocking'} {zvol_path}@{name} failed")

    def delete_snapshot(self, zvol_path: str, name: str) -> None:
        with self.lock:
            self._clear()
            self._drop_spares_from(f"{zvol_path}@{name}")
            if not self.zfs.destroy_snapshot(f"{zvol_path}@{name}"):
                raise self._fail(f"Deleting {zvol_path}@{name} failed")

    def merge_into_base(self, zvol_path: str, name: str) -> list[str]:
        """
        Make version `name` the new @base of a master: every older version is
        deleted, then `name` is renamed to `base`. No data is copied: a ZFS
        snapshot is a whole disk, so `name` already holds every change.
        Returns the deleted versions, oldest first.

        The caller checks first that no PC uses an older version; ZFS refuses
        to destroy a snapshot with clones anyway, so a PC that started on one
        in between stops the merge there. A failure after some deletions
        leaves `name` under its old name, and running it again finishes.
        """
        with self.lock:
            self._clear()
            snaps = self.zfs.list_snapshots(zvol_path)
            if snaps is None:
                raise self._fail(f"Listing versions of {zvol_path} failed")
            if name not in snaps:
                raise ProvisioningError(f"{zvol_path}@{name} does not exist")
            older = snaps[: snaps.index(name)]
            for old in older:
                self._drop_spares_from(f"{zvol_path}@{old}")
                if not self.zfs.destroy_snapshot(f"{zvol_path}@{old}"):
                    raise self._fail(f"Deleting {zvol_path}@{old} failed")
            if name != "base" and not self.zfs.rename_snapshot(zvol_path, name, "base"):
                raise self._fail(f"Renaming {zvol_path}@{name} to @base failed")
        return older

    def pool_space(self) -> dict:
        space = self.zfs.pool_space()
        if space is None:
            raise self._fail(f"Reading the space of pool {self.zfs.layout.pool} failed")
        return space

    def reserved_bytes(self) -> int:
        return self.zfs.reserved_bytes()

    def set_reserved_percent(self, percent: int) -> int:
        """Reserve `percent` of the pool's usable space; returns the bytes reserved."""
        with self.lock:
            self._clear()
            space = self.pool_space()
            nbytes = space["total"] * percent // 100
            if not self.zfs.set_reserved(nbytes):
                raise self._fail(f"Reserving {percent}% of pool {self.zfs.layout.pool} failed")
        return nbytes

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

    def start_edit(self, machine_name: str, initiator_iqn: str, zvol_path: str) -> str:
        """
        Map a DRAFT master as the machine's game disk so it can be filled on
        that PC. Returns the target IQN. The caller makes sure the disk is a
        draft and the machine has nothing else mapped.
        """
        with self.lock:
            self._clear()
            if self.zfs.get_property(zvol_path, "readonly") != "off":
                raise self._fail(f"{zvol_path} is not a writable draft")
            iqn = self.iscsi.attach(
                machine_name, initiator_iqn, [Disk("game", self.zfs.zvol_device_path(zvol_path))]
            )
            if iqn is None:
                raise self._fail("Exposing the master over iSCSI failed", self.iscsi.runner)
        return iqn

    def finish_edit(self, machine_name: str, initiator_iqn: str) -> None:
        """Unmap the master from the editing machine. The client must be shut down."""
        with self.lock:
            self._clear()
            self._detach(machine_name, initiator_iqn)

    # ── Machines ──────────────────────────────────────────────────────

    def provision(
        self, machine_name: str, initiator_iqn: str, snapshot_path: str,
        os_snapshot_path: str | None = None, os_setup: OsSetup | None = None,
    ) -> ClientDisk:
        """
        Clone of the game disk snapshot, exposed on the shared iSCSI target
        as mapped LUN 0 of an ACL for the machine's initiator IQN. With
        `os_snapshot_path` (Boot Mode) the OS image is cloned too and becomes
        LUN 0, the game disk LUN 1; `os_setup` is written into the OS clone
        before it is mapped.
        """
        clones = [(snapshot_path, self.client_path(machine_name))]
        if os_snapshot_path is not None:
            clones.append((os_snapshot_path, self.os_client_path(machine_name)))
        with self.lock:
            self._clear()
            for _, clone in clones:
                if self.zfs.dataset_exists(clone):
                    raise ProvisioningError(
                        f"Clone {clone} already exists on the host; deprovision first"
                    )
            made: list[str] = []

            def rollback() -> None:
                for clone in made:
                    if not self.zfs.destroy(clone, recursive=True):
                        logger.error("Rollback: clone %s left on the host", clone)

            for snap, clone in clones:
                if not self.zfs.clone(snap, clone):
                    err = self._fail(f"Clone {snap} -> {clone} failed")
                    rollback()
                    raise err
                made.append(clone)
            os_clone = clones[1][1] if os_snapshot_path is not None else None
            problem = self._set_up_os(os_clone, os_setup)
            iqn = self.iscsi.attach(machine_name, initiator_iqn, self._disks(clones[0][1], os_clone))
            if iqn is None:
                err = self._fail("Exposing the disk over iSCSI failed", self.iscsi.runner)
                # attach() cleaned up its own iSCSI state; roll back the clones.
                rollback()
                raise err
        return ClientDisk(clones[0][1], snapshot_path, iqn, os_clone, os_snapshot_path, problem)

    def reset(
        self, machine_name: str, initiator_iqn: str, clone_zvol: str, snapshot_path: str,
        os_clone_zvol: str | None = None, os_snapshot_path: str | None = None,
        os_setup: OsSetup | None = None, keep: frozenset[str] = frozenset(),
        target_iqn: str | None = None,
    ) -> ClientDisk:
        """
        Order: detach (ACL + backstores) → destroy clones → clone → attach.
        `snapshot_path` is the disk's CURRENT snapshot, so a reset also moves
        the machine to the newest version of the master. Boot Mode passes the
        OS clone and snapshot too; both writebacks are reset together, and
        `os_setup` is written into the fresh OS clone before it is mapped.
        `keep` names writebacks ("game", "os") that are only mapped again,
        not re-cloned; their snapshot argument is then the one they came from.
        The client must be powered off or disconnected. `target_iqn` is the
        machine's recorded target: only one of the old per-client design
        (or an unknown one) is looked for and removed.

        This runs while a restarting PC waits at iPXE, so it sends as few
        targetcli commands as it can: each one loads the whole LIO
        configuration.
        """
        if (os_clone_zvol is None) != (os_snapshot_path is None):
            raise ProvisioningError("An OS writeback needs both its zvol and its snapshot")
        legacy = target_iqn != self.iscsi.target_iqn()
        with self._machine_lock(machine_name), self.lock:
            t0 = time.monotonic()
            self._clear()
            self._detach(machine_name, initiator_iqn, save=False, legacy=legacy)
            t_detach = time.monotonic()
            swapped = False
            if "game" not in keep:
                if self._spare_ok(self.spare_path(clone_zvol), snapshot_path) and self._swap_in_spare(clone_zvol):
                    swapped = True
                elif not self.zfs.reset_clone(clone_zvol, snapshot_path):
                    raise self._fail(f"Resetting clone {clone_zvol} failed")
            reset_os = os_clone_zvol is not None and "os" not in keep
            problem = None
            if reset_os:
                spare = self.spare_path(os_clone_zvol)
                if self._spare_ok(spare, os_snapshot_path, setup_tag(os_setup)) \
                        and self._swap_in_spare(os_clone_zvol):
                    swapped = True                      # already set up as this PC
                else:
                    if not self.zfs.reset_clone(os_clone_zvol, os_snapshot_path):
                        raise self._fail(f"Resetting clone {os_clone_zvol} failed")
                    problem = self._set_up_os(os_clone_zvol, os_setup)
            if swapped:
                # A renamed zvol gets its /dev/zvol link from udev a moment later.
                self.zfs.runner.run(["udevadm", "settle", "--timeout=10"], quiet=True)
            t_disks = time.monotonic()
            iqn = self.iscsi.attach(machine_name, initiator_iqn, self._disks(clone_zvol, os_clone_zvol),
                                    fresh=True)
            if iqn is None:
                raise self._fail("Exposing the disk over iSCSI failed", self.iscsi.runner)
            t_end = time.monotonic()
            logger.info("Reset of %s in %.1f s (unmap %.1f, writebacks %.1f%s, map %.1f)",
                        machine_name, t_end - t0, t_detach - t0, t_disks - t_detach,
                        " from spares" if swapped else "", t_end - t_disks)
        return ClientDisk(clone_zvol, snapshot_path, iqn, os_clone_zvol, os_snapshot_path, problem)

    def rename(
        self, old_name: str, new_name: str, initiator_iqn: str, clone_zvol: str,
        os_clone_zvol: str | None = None,
    ) -> tuple[str, str | None]:
        """
        Give a provisioned machine a new name on the host: its writebacks are
        renamed (zfs rename, nothing is copied) and mapped again under the new
        backstore names; the ACL is the initiator's, so it is the same. The
        client must be powered off. On failure everything goes back to the
        old name. Returns the new clone paths (game, OS).
        """
        new_clone = self.client_path(new_name)
        new_os = self.os_client_path(new_name) if os_clone_zvol is not None else None
        pairs = [(clone_zvol, new_clone)] + ([(os_clone_zvol, new_os)] if os_clone_zvol else [])
        self.drop_spares(old_name, clone_zvol, os_clone_zvol)    # made for the old name
        with self.lock:
            self._clear()
            for _, new in pairs:
                if self.zfs.dataset_exists(new):
                    raise ProvisioningError(f"{new} already exists on the host")
            self._detach(old_name, initiator_iqn)
            done: list[tuple[str, str]] = []
            err: ProvisioningError | None = None
            for old, new in pairs:
                if not self.zfs.rename_dataset(old, new):
                    err = self._fail(f"Renaming {old} to {new} failed")
                    break
                done.append((old, new))
            if err is None:
                if self.iscsi.attach(new_name, initiator_iqn, self._disks(new_clone, new_os)) is not None:
                    return new_clone, new_os
                err = self._fail("Exposing the renamed disks over iSCSI failed", self.iscsi.runner)
            for old, new in reversed(done):
                if not self.zfs.rename_dataset(new, old):
                    logger.error("Rollback: %s left as %s", old, new)
            if self.iscsi.attach(old_name, initiator_iqn, self._disks(clone_zvol, os_clone_zvol)) is None:
                logger.error("Rollback: the disks of %s could not be mapped again", old_name)
            raise err

    def prepare_image(self, master: str, prep: ImagePrep) -> str:
        """
        A new version of an OS image that is ready for every PC: its newest
        version is cloned, prepared (app.windows_setup.prepare_image) and
        made the next version, as Apply Writebacks does with a PC's writeback.
        Returns the new snapshot name. Nothing changes on failure.
        """
        if self.windows is None:
            raise ProvisioningError("Windows setup is not available on this server")
        tmp = f"{self.zfs.layout.writebacks}/{master.rsplit('/', 1)[-1]}_ggnet_prep"   # '_': never a PC
        with self.lock:
            self._clear()
            snaps = self.zfs.list_snapshots(master)
            if not snaps:
                raise self._fail(f"Listing versions of {master} failed")
            base, new = snaps[-1], next_version(snaps)
            if self.zfs.dataset_exists(tmp) and not self.zfs.destroy(tmp):
                raise self._fail(f"{tmp} is left over from an earlier prepare and cannot be removed")
            if not self.zfs.clone(f"{master}@{base}", tmp):
                raise self._fail(f"Cloning {master}@{base} failed")
            problem = self.windows.prepare_image(self.zfs.zvol_device_path(tmp), prep,
                                                 label=master.rsplit("/", 1)[-1])
            if problem is None and self.zfs.apply_clone(tmp, master, base, new):
                return new
            err = ProvisioningError(f"Preparing {master} failed: {problem}") if problem \
                else self._fail(f"Making {tmp} the next version of {master} failed")
            if not self.zfs.destroy(tmp):
                logger.error("Could not remove %s after a failed prepare", tmp)
            raise err

    def add_hardware(self, master: str, nics: list[tuple[str, str]], prep: ImagePrep,
                     boot: Callable[[str], str | None], step: Callable[[str], None] = lambda s: None,
                     ) -> tuple[str, list[dict]]:
        """
        Add new hardware: a new version of an OS image with these network
        cards (PCI hardware ID, instance ID) installed by Windows itself. Its
        newest version is cloned, the agent and a job are written into the
        clone, `boot(device)` runs the clone in the toolchain VM until the
        agent has installed the cards and shut it down (None, or why it
        failed), the clone is prepared again (the new card's driver marked
        for a network boot) and made the next version. Returns the new
        snapshot and the agent's per-device results. Nothing changes on
        failure. The lock is not held while the VM runs.
        """
        if self.windows is None:
            raise ProvisioningError("Windows setup is not available on this server")
        label = master.rsplit("/", 1)[-1]
        tmp = f"{self.zfs.layout.writebacks}/{label}_ggnet_hwadd"   # '_': never a PC
        with self.lock:
            self._clear()
            snaps = self.zfs.list_snapshots(master)
            if not snaps:
                raise self._fail(f"Listing versions of {master} failed")
            base = snaps[-1]
            if self.zfs.dataset_exists(tmp) and not self.zfs.destroy(tmp):
                raise self._fail(f"{tmp} is left over from an earlier run and cannot be removed")
            if not self.zfs.clone(f"{master}@{base}", tmp):
                raise self._fail(f"Cloning {master}@{base} failed")
        device = self.zfs.zvol_device_path(tmp)
        try:
            step("job")
            problem = self.windows.start_toolchain(device, nics, prep, label=label)
            if problem:
                raise ProvisioningError(f"Preparing the toolchain run failed: {problem}")
            step("vm")
            problem = boot(device)
            if problem:
                raise ProvisioningError(problem)
            step("result")
            result = self.windows.toolchain_result(device, label=label)
            if isinstance(result, str):
                raise ProvisioningError(result)
            devices = [d for d in result.get("devices", []) if isinstance(d, dict)]
            if not result.get("ok"):
                failed = "; ".join(f"{d.get('instance_id')}: {d.get('error')}" for d in devices if not d.get("ok"))
                raise ProvisioningError(f"Windows could not install the hardware: {failed or result.get('error')}")
            step("prepare")
            problem = self.windows.prepare_image(device, prep, label=f"hwadd-{label}")
            if problem:
                raise ProvisioningError(f"Preparing the new version failed: {problem}")
            step("apply")
            with self.lock:
                self._clear()
                snaps = self.zfs.list_snapshots(master) or [base]
                new = next_version(snaps)
                if not self.zfs.apply_clone(tmp, master, base, new):
                    raise self._fail(f"Making {tmp} the next version of {master} failed")
            return new, devices
        except BaseException:
            if self.zfs.dataset_exists(tmp) and not self.zfs.destroy(tmp):
                logger.error("Could not remove %s after a failed Add new hardware", tmp)
            raise

    def deprovision(
        self, machine_name: str, initiator_iqn: str, clone_zvol: str | None,
        os_clone_zvol: str | None = None,
    ) -> None:
        """
        Detach from iSCSI, then delete the clones. Safe to call when nothing
        exists. The OS clone is only deleted when it is named: a Disk Mode
        machine has none.
        """
        clones = [clone_zvol or self.client_path(machine_name)]
        if os_clone_zvol is not None:
            clones.append(os_clone_zvol)
        with self._machine_lock(machine_name), self.lock:
            self._clear()
            self._detach(machine_name, initiator_iqn)
            for clone in clones:
                if self.zfs.dataset_exists(clone) and not self.zfs.destroy(clone, recursive=True):
                    raise self._fail(f"Deleting clone {clone} failed")
                spare = self.spare_path(clone)
                if self.zfs.dataset_exists(spare) and not self.zfs.destroy(spare, recursive=True):
                    logger.warning("Could not remove the spare %s", spare)

    # ── Writeback lifecycle ───────────────────────────────────────────

    def session_active(self, initiator_iqn: str, target_iqn: str | None = None) -> bool | None:
        """Live LIO session state of a machine; None when unknown."""
        return self.iscsi.session_active(initiator_iqn, target_iqn)

    def set_keep_writeback(self, clone_zvol: str, keep: bool, os_clone_zvol: str | None = None) -> None:
        """
        A kept writeback holds the admin's installs, so it gets sync=standard;
        a disposable one sync=disabled (it is thrown away anyway). Boot Mode
        keeps the OS writeback too.
        """
        with self.lock:
            self._clear()
            for clone in filter(None, (clone_zvol, os_clone_zvol)):
                if self.zfs.dataset_exists(clone) and not self.zfs.set_property(
                    clone, "sync", "standard" if keep else "disabled"
                ):
                    raise self._fail(f"Setting sync on {clone} failed")

    def apply_writebacks(
        self, machine_name: str, initiator_iqn: str, applies: list[WritebackApply],
        clone_zvol: str, os_clone_zvol: str | None = None, os_prep: ImagePrep | None = None,
    ) -> list[str]:
        """
        Make each clone's content the next version of its master (
        "Apply Writebacks"); Boot Mode applies the OS writeback, the game
        writeback or both. Returns the new snapshot names, in the order of
        `applies`. The machine must be powered off. A clone becomes its
        master, so the machine's disks (`clone_zvol`, `os_clone_zvol`) are
        unmapped first; the caller re-clones the machine from the new
        versions. When the first apply fails, the disks are mapped again;
        when a later one fails, PartialApplyError says what was applied.
        With `os_prep`, the OS writeback is prepared for every PC first
        (app.windows_setup.prepare_image), so the new version is ready as it is.
        """
        with self.lock:
            self._clear()
            news = []
            for a in applies:
                snaps = self.zfs.list_snapshots(a.master)
                if snaps is None:
                    raise self._fail(f"Listing versions of {a.master} failed")
                news.append(next_version(snaps))
            self._detach(machine_name, initiator_iqn)
            applied: dict[str, str] = {}
            for a, new in zip(applies, news):
                if os_prep is not None and a.clone_zvol == os_clone_zvol and self.windows is not None:
                    problem = self.windows.prepare_image(self.zfs.zvol_device_path(a.clone_zvol), os_prep,
                                                         label=machine_name)
                    if problem:
                        logger.warning("The OS version from %s is not fully prepared: %s", machine_name, problem)
                if self.zfs.apply_clone(a.clone_zvol, a.master, a.base_snap, new):
                    applied[a.master] = new
                    continue
                err = self._fail(f"Applying {a.clone_zvol} to {a.master} failed")
                if applied:
                    raise PartialApplyError(
                        f"{err}; applied before the failure: "
                        + ", ".join(f"{m}@{s}" for m, s in applied.items())
                        + ". Reset the machine.", applied)
                if self.iscsi.attach(machine_name, initiator_iqn,
                                     self._disks(clone_zvol, os_clone_zvol)) is None:
                    logger.error("Apply failed and the writebacks of %s could not be mapped again",
                                 machine_name)
                raise err
        return news
