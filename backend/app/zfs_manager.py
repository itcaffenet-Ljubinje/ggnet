"""
ZFSManager: manages ZFS zvols for ggNet Disk Mode.

Principle: a master image is ALWAYS read-only after its @base snapshot.
Each client gets a zfs clone of that snapshot; the clone IS the writeback.
Resetting a client = destroy the clone + a new clone of the same snapshot.
The master never changes.

Pool and dataset names come from the [storage] section of config.toml
(app.config.get_config). The pool also holds data that is not ggNet's, so
every operation is restricted to the tree under `storage.root_dataset`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from app.config import get_config
from app.runner import CommandRunner

logger = logging.getLogger("ggnet.zfs")

# Allowed characters in one component of a ZFS name. Deliberately narrower
# than what ZFS accepts (no spaces, no '%'), and a component must not start
# with '-' so it cannot be parsed as a command option.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
# A version's comment (who, when, what changed), kept as a user property on
# its snapshot so it travels with the snapshot (rename, promote, zfs send).
# A copy of a large game disk can take hours.
COPY_TIMEOUT = 24 * 3600
COMMENT_PROP = "ggnet:comment"
COMMENT_MAX = 200
# A locked version is never deleted, by retention or by hand, until unlocked.
LOCK_PROP = "ggnet:locked"

# Datasets under root_dataset that ggNet creates and manages.
_CHILD_KEYS = ("images", "writebacks", "snapshots", "iscsi_targets", "imports")
# Older config.toml files have no `imports`: it defaults to <root_dataset>/imports.
_OPTIONAL_KEYS = ("imports",)


@dataclass(frozen=True)
class ZFSLayout:
    """ZFS names from the [storage] section of config.toml."""

    pool: str
    root_dataset: str
    images: str
    writebacks: str           # per-client clones (the old code's "clients")
    snapshots: str
    iscsi_targets: str
    imports: str = ""         # image files waiting to be imported (a filesystem, not zvols)

    def __post_init__(self) -> None:
        if not self.imports:
            object.__setattr__(self, "imports", f"{self.root_dataset}/imports")

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]] = None) -> "ZFSLayout":
        storage = (cfg if cfg is not None else get_config()).get("storage")
        if not isinstance(storage, Mapping):
            raise ValueError("config.toml has no [storage] section")
        keys = ("pool", "root_dataset", *_CHILD_KEYS)
        missing = [k for k in keys if not storage.get(k) and k not in _OPTIONAL_KEYS]
        if missing:
            raise ValueError(f"[storage] is missing: {', '.join(missing)}")
        return cls(**{k: str(storage[k]) for k in keys if storage.get(k)})


def valid_comment(comment: str) -> bool:
    """One printable line (zfs list prints it between tabs), at most COMMENT_MAX characters."""
    return len(comment) <= COMMENT_MAX and comment.isprintable()


def _int(value: str) -> int:
    """`zfs list -p` prints bytes and times as integers, or '-' when not set."""
    return int(value) if value.isdigit() else 0


def _valid_name(name: str) -> bool:
    """Whether `name` is a syntactically valid ZFS name (dataset or dataset@snap)."""
    if not name or name.count("@") > 1:
        return False
    dataset, _, snap = name.partition("@")
    if "@" in name and not _COMPONENT_RE.match(snap):
        return False
    parts = dataset.split("/")
    return all(p not in (".", "..") and _COMPONENT_RE.match(p) for p in parts)


def _validate_layout(layout: ZFSLayout) -> None:
    """Raise ValueError for a layout that could reach outside the ggNet tree."""
    pool, root = layout.pool, layout.root_dataset
    if not _valid_name(pool) or "/" in pool or "@" in pool:
        raise ValueError(f"Invalid pool name: {pool!r}")
    if "@" in root or not _valid_name(root) or not root.startswith(pool + "/"):
        raise ValueError(
            f"storage.root_dataset ({root!r}) must be a dataset inside "
            f"pool {pool!r}, not the pool itself"
        )
    for key in _CHILD_KEYS:
        ds = getattr(layout, key)
        if "@" in ds or not _valid_name(ds) or not ds.startswith(root + "/"):
            raise ValueError(
                f"storage.{key} ({ds!r}) must be a dataset under "
                f"storage.root_dataset ({root!r})"
            )


# destroy(): how often and how long to wait for a zvol still held open after umount.
BUSY_RETRIES = 4
BUSY_WAIT = 1.0


class ZFSManager:
    def __init__(
        self,
        layout: Optional[ZFSLayout] = None,
        runner: Optional[CommandRunner] = None,
    ):
        self.layout = layout or ZFSLayout.from_config()
        # A config error is fatal right away: better to fail at startup than
        # to later let an operation run outside the ggNet tree.
        _validate_layout(self.layout)
        self.runner = runner or CommandRunner()
        self.sleep: Callable[[float], None] = time.sleep

    def _run(self, cmd: list[str], quiet: bool = False) -> tuple[bool, str, str]:
        return self.runner.run(cmd, quiet=quiet)

    # ── Guard: ggNet only touches its own tree ────────────────────────

    def _is_managed(
        self,
        path: str,
        allow_base: bool = False,
        allow_snapshot: bool = True,
    ) -> bool:
        """
        Whether `path` belongs to the ggNet tree (under root_dataset). The pool
        also holds other data, so everything else is rejected BEFORE any
        command reaches the host.

        allow_base=True also allows root_dataset itself (e.g. setup, reads);
        allow_snapshot=False rejects the `dataset@snap` form (e.g. for a
        create/clone target, where a snapshot makes no sense).
        """
        if not isinstance(path, str) or not _valid_name(path):
            logger.error("Rejected invalid ZFS path: %r", path)
            return False

        dataset, sep, _ = path.partition("@")
        if sep and not allow_snapshot:
            logger.error("Rejected path, snapshot not allowed here: %s", path)
            return False

        base = self.layout.root_dataset
        if dataset == base:
            # A snapshot of root_dataset itself (base@x) is not part of the tree.
            if allow_base and not sep:
                return True
        elif dataset.startswith(base + "/"):
            return True

        logger.error("Rejected path outside the ggNet tree (%s): %s", base, path)
        return False

    def dataset_exists(self, path: str) -> bool:
        """Whether a dataset/zvol exists. Failure is an expected outcome here, not an error."""
        if not self._is_managed(path, allow_base=True):
            return False
        ok, _, _ = self._run(["zfs", "list", "-H", "-o", "name", path], quiet=True)
        return ok

    # ── Initial setup ─────────────────────────────────────────────────

    def setup_dataset_tree(self) -> bool:
        """
        Create the base dataset structure. Safe to call repeatedly;
        existing datasets are silently skipped.
        """
        datasets = [self.layout.root_dataset] + [
            getattr(self.layout, key) for key in _CHILD_KEYS
        ]
        for ds in datasets:
            if self.dataset_exists(ds):
                continue
            ok, _, err = self._run([
                "zfs", "create",
                "-o", "compression=lz4",
                "-o", "atime=off",
                ds,
            ])
            if not ok:
                logger.error("Could not create dataset %s: %s", ds, err)
                return False
        logger.info("ZFS dataset tree ready under %s", self.layout.root_dataset)
        return True

    # ── Master image (shared disk) ────────────────────────────────────

    def create_zvol(
        self,
        path: str,
        size_gb: int,
        volblocksize: str = "64k",
        compression: str = "off",
        sync: str = "disabled",
    ) -> bool:
        """`path` is the full ZFS path, e.g. `tank/ggnet/images/steam-main`."""
        if not self._is_managed(path, allow_snapshot=False):
            return False
        ok, _, err = self._run([
            "zfs", "create",
            "-V", f"{size_gb}G",
            "-o", f"volblocksize={volblocksize}",
            "-o", f"compression={compression}",
            "-o", f"sync={sync}",
            path,
        ])
        if ok:
            logger.info("Zvol created: %s (%sGB)", path, size_gb)
        else:
            logger.error("Zvol creation failed %s: %s", path, err)
        return ok

    def snapshot(self, zvol_path: str, snap_name: str) -> bool:
        snap = f"{zvol_path}@{snap_name}"
        if "@" in zvol_path or not self._is_managed(snap):
            return False
        ok, _, _ = self._run(["zfs", "snapshot", snap])
        return ok

    def protect_snapshot(self, snapshot: str, tag: str = "ggnet:protected") -> bool:
        """zfs hold: prevent accidental deletion of a snapshot."""
        if "@" not in snapshot or not self._is_managed(snapshot):
            return False
        ok, _, _ = self._run(["zfs", "hold", tag, snapshot])
        return ok

    def release_snapshot(self, snapshot: str, tag: str = "ggnet:protected") -> bool:
        """
        zfs release: remove the hold from a snapshot. The inverse of
        protect_snapshot(). Fails quietly if the hold does not exist.
        """
        if "@" not in snapshot or not self._is_managed(snapshot):
            return False
        ok, _, _ = self._run(["zfs", "release", tag, snapshot], quiet=True)
        return ok

    def set_readonly(self, path: str, readonly: bool = True) -> bool:
        if not self._is_managed(path, allow_snapshot=False):
            return False
        value = "on" if readonly else "off"
        ok, _, _ = self._run(["zfs", "set", f"readonly={value}", path])
        return ok

    def publish_master(self, zvol_path: str, snap_name: str = "base") -> bool:
        """
        Finalize a master image after its data has been imported:
        snapshot → hold → readonly. The master is not touched after this.
        """
        if not self.snapshot(zvol_path, snap_name):
            return False
        if not self.protect_snapshot(f"{zvol_path}@{snap_name}"):
            return False
        if not self.set_readonly(zvol_path, True):
            return False
        logger.info("Master '%s' protected at @%s, read-only.", zvol_path, snap_name)
        return True

    # ── Per-client clone (= writeback) ────────────────────────────────

    def clone(self, source_snapshot: str, target_path: str) -> bool:
        """zfs clone source@snap target: target is a NEW zvol that captures writes."""
        if "@" not in source_snapshot or not self._is_managed(source_snapshot):
            return False
        if not self._is_managed(target_path, allow_snapshot=False):
            return False
        ok, _, err = self._run([
            "zfs", "clone",
            "-o", "sync=disabled",
            source_snapshot, target_path,
        ])
        if not ok:
            logger.error("Clone failed %s -> %s: %s", source_snapshot, target_path, err)
        return ok

    def reset_clone(self, client_zvol: str, source_snapshot: str) -> bool:
        """
        Reset to a clean state: destroy the existing clone and create a new
        one from the same master snapshot. The master stays untouched.

        The client's iSCSI target must be deleted BEFORE calling this:
        LIO keeps the zvol open and destroy otherwise fails with
        "dataset is busy".
        """
        if not self._is_managed(client_zvol, allow_snapshot=False):
            return False
        if self.dataset_exists(client_zvol):
            if not self.destroy(client_zvol, recursive=True):
                logger.error(
                    "Reset aborted, old clone %s was not destroyed "
                    "(is its iSCSI target still active?)", client_zvol
                )
                return False
        return self.clone(source_snapshot, client_zvol)

    def destroy(self, path: str, recursive: bool = False) -> bool:
        # Never root_dataset itself: `destroy -r root` would wipe the whole ggNet tree.
        if not self._is_managed(path):
            return False
        cmd = ["zfs", "destroy"]
        if recursive:
            cmd.append("-r")
        cmd.append(path)
        ok, _, err = self._run(cmd)
        # A zvol that was just mounted with ntfs-3g stays open for a moment
        # after umount (FUSE closes it later): "dataset is busy". Retry briefly.
        for _ in range(BUSY_RETRIES):
            if ok or "dataset is busy" not in err:
                break
            self.sleep(BUSY_WAIT)
            self._run(["udevadm", "settle", "--timeout=5"], quiet=True)
            ok, _, err = self._run(cmd)
        return ok

    def destroy_master(self, zvol_path: str) -> bool:
        """
        Remove a master zvol completely. publish_master() protected its
        snapshots with a hold, so the holds are released first and the zvol
        is then destroyed recursively.

        All client clones of the master must be destroyed BEFORE this; ZFS
        does not allow destroying a snapshot that has dependent clones. So
        clones are checked BEFORE releasing holds: otherwise a failed destroy
        would leave the master snapshot unprotected.
        """
        if not self._is_managed(zvol_path, allow_snapshot=False):
            return False
        ok, out, err = self._run([
            "zfs", "list", "-H", "-t", "snapshot",
            "-o", "name,clones", "-d", "1", zvol_path,
        ])
        if not ok:
            logger.error("Could not list snapshots of master %s: %s", zvol_path, err)
            return False

        snapshots = []
        for line in out.splitlines():
            name, _, clones = line.partition("\t")
            if clones not in ("", "-"):
                logger.error(
                    "Master %s not destroyed, %s still has clones: %s",
                    zvol_path, name, clones,
                )
                return False
            snapshots.append(name)

        released = [s for s in snapshots if self.release_snapshot(s)]
        if self.destroy(zvol_path, recursive=True):
            return True

        # Destroy failed: restore the holds we removed.
        for snap in released:
            self.protect_snapshot(snap)
        logger.error("Destroying master %s failed, holds restored", zvol_path)
        return False

    def list_snapshots(self, dataset: str) -> Optional[list[str]]:
        """
        Snapshot names (the part after '@') of one dataset, oldest first.
        None if they cannot be listed.
        """
        if not self._is_managed(dataset, allow_snapshot=False):
            return None
        ok, out, _ = self._run([
            "zfs", "list", "-H", "-t", "snapshot", "-o", "name",
            "-s", "createtxg", "-d", "1", dataset,
        ])
        if not ok:
            return None
        return [line.partition("@")[2] for line in out.splitlines() if "@" in line]

    def rename_dataset(self, old: str, new: str) -> bool:
        """
        zfs rename old new, for a client clone (a writeback). Its origin, data
        and snapshots stay; it must not be in use (no iSCSI backstore).
        """
        if not (self._is_managed(old, allow_snapshot=False) and self._is_managed(new, allow_snapshot=False)):
            return False
        ok, _, err = self._run(["zfs", "rename", old, new])
        if not ok:
            logger.error("Renaming %s to %s failed: %s", old, new, err)
            return False
        logger.info("Dataset %s renamed to %s", old, new)
        return True

    def rename_snapshot(self, dataset: str, old: str, new: str) -> bool:
        """
        zfs rename dataset@old dataset@new. Holds stay on the snapshot and
        clones made from it follow the new name (their origin is renamed too).
        """
        if not (self._is_managed(dataset, allow_snapshot=False)
                and _COMPONENT_RE.match(old) and _COMPONENT_RE.match(new)):
            return False
        ok, _, err = self._run(["zfs", "rename", f"{dataset}@{old}", f"{dataset}@{new}"])
        if not ok:
            logger.error("Renaming %s@%s to @%s failed: %s", dataset, old, new, err)
            return False
        logger.info("Snapshot %s@%s renamed to @%s", dataset, old, new)
        return True

    def copy_snapshot(
        self,
        snapshot: str,
        target: str,
        progress: Callable[[int, int], None] | None = None,
        timeout: int = COPY_TIMEOUT,
    ) -> bool:
        """
        A full, independent copy of one version as a new master:

            zfs send -c -P -v <master>@<v> | zfs recv -u <target>

        The target shares no blocks with the source, so either can be deleted
        later. Its only version is renamed to @base, held and made read-only,
        as publish_master() leaves a master. `progress(done, total)` follows
        the bytes sent. On failure nothing is left behind.
        """
        if "@" not in snapshot or not self._is_managed(snapshot):
            return False
        if not self._is_managed(target, allow_snapshot=False) or self.dataset_exists(target):
            return False
        total = 0

        def line(text: str) -> None:
            nonlocal total
            parts = text.split("\t")
            if parts[0] == "size" and len(parts) > 1:
                total = _int(parts[1])
            elif progress and total and len(parts) > 1 and parts[0][:1].isdigit():
                progress(min(_int(parts[1]), total), total)

        ok, _, err = self.runner.run_pipe(
            ["zfs", "send", "-c", "-P", "-v", snapshot],
            ["zfs", "recv", "-u", target],
            timeout=timeout,
            on_producer_line=line,
        )
        if not ok:
            logger.error("Copying %s to %s failed: %s", snapshot, target, err)
            if self.dataset_exists(target) and not self.destroy(target, recursive=True):
                logger.error("Could not remove the partial copy %s", target)
            self.runner.last_error = err     # the copy's reason, not the cleanup's
            return False
        name = snapshot.partition("@")[2]
        if not ((name == "base" or self.rename_snapshot(target, name, "base"))
                and self.protect_snapshot(f"{target}@base")
                and self.set_readonly(target, True)):
            err = self.runner.last_error
            self.destroy(target, recursive=True)
            self.runner.last_error = err
            return False
        if progress and total:
            progress(total, total)
        logger.info("Copied %s to %s@base", snapshot, target)
        return True

    def apply_clone(self, clone: str, master: str, base_snap: str, new_snap: str) -> bool:
        """
        Turn a client's writeback into a new version of its master
        (Apply Writebacks), without copying any data:

            clone: readonly=on → @new_snap → hold
            zfs promote clone      master's snapshots up to base_snap move to
                                   the clone; the old master head becomes a
                                   clone of clone@base_snap
            master → <master>_ggnet_old, clone → master
            destroy <master>_ggnet_old   (an empty head nobody depends on)

        `zfs send -i master@base clone@x | zfs recv master` cannot do this:
        an incremental from a clone's origin is a clone stream, which zfs recv
        only takes as a NEW dataset ("cannot receive new filesystem stream").

        Every snapshot keeps its name and holds, so other clients' clones are
        untouched. The clone must not be in use (no iSCSI backstore): it is
        renamed. Refused when base_snap is not the master's newest snapshot.
        Until the renames everything is rolled back on failure.
        """
        old = f"{master}_ggnet_old"   # '_' never appears in a disk name
        base = f"{master}@{base_snap}"
        new = f"{clone}@{new_snap}"
        if not (self._is_managed(clone, allow_snapshot=False)
                and self._is_managed(master, allow_snapshot=False)
                and self._is_managed(base) and _COMPONENT_RE.match(new_snap)):
            return False

        snaps = self.list_snapshots(master)
        if snaps is None:
            return False
        if not snaps or snaps[-1] != base_snap:
            self.runner.last_error = (
                f"{base} is not the newest version of {master} (newest: "
                f"{snaps[-1] if snaps else 'none'}); discard this writeback and redo the change"
            )
            logger.error(self.runner.last_error)
            return False
        if new_snap in snaps:
            self.runner.last_error = f"{master}@{new_snap} already exists"
            return False
        if self.get_property(clone, "origin") != base:
            self.runner.last_error = f"{clone} is not a clone of {base}"
            return False
        if self.dataset_exists(old):
            self.runner.last_error = f"{old} is left over from an earlier apply; remove it first"
            return False

        def fail(step: str, undo: list[list[str]]) -> bool:
            err = self.runner.last_error
            logger.error("Applying %s to %s failed at %s: %s", clone, master, step, err)
            for cmd in undo:
                ok, _, uerr = self._run(cmd, quiet=True)
                if not ok:
                    logger.error("Rollback step %s failed: %s", cmd, uerr)
            self.runner.last_error = err
            return False

        # A leftover of an interrupted apply on the clone; it is ours.
        if new_snap in (self.list_snapshots(clone) or []):
            self._run(["zfs", "release", "ggnet:protected", new], quiet=True)
            self._run(["zfs", "destroy", new], quiet=True)

        undo: list[list[str]] = [["zfs", "set", "readonly=off", clone]]
        if not self.set_readonly(clone, True):
            return fail("readonly", undo)
        if not self.snapshot(clone, new_snap):
            return fail("snapshot", undo)
        undo.insert(0, ["zfs", "destroy", new])
        if not self.protect_snapshot(new):
            return fail("hold", undo)
        undo.insert(0, ["zfs", "release", "ggnet:protected", new])

        ok, _, _ = self._run(["zfs", "promote", clone])
        if not ok:
            return fail("promote", undo)
        # Promoting the old master back returns base_snap and older to it.
        undo.insert(0, ["zfs", "promote", master])

        ok, _, _ = self._run(["zfs", "rename", master, old])
        if not ok:
            return fail("rename master", undo)
        undo.insert(0, ["zfs", "rename", old, master])

        ok, _, _ = self._run(["zfs", "rename", clone, master])
        if not ok:
            return fail("rename clone", undo)

        # Applied. What is left only tidies up.
        self._run(["zfs", "inherit", "sync", master], quiet=True)   # the writeback's sync
        if not self.destroy(old):
            logger.warning("Applied, but %s was left on the host", old)
        logger.info("Applied writeback %s to %s as @%s", clone, master, new_snap)
        return True

    def zvol_device_path(self, zvol_path: str) -> str:
        """Path to the block device, input for the iSCSI backstore."""
        return f"/dev/zvol/{zvol_path}"

    # ── Pool status / monitoring ──────────────────────────────────────

    # ── Pool space and the reserved-space dataset ─────────────────────

    @property
    def reserved_path(self) -> str:
        """An empty, unmounted dataset whose refreservation keeps part of the pool free."""
        return f"{self.layout.root_dataset}/reserved"

    def pool_space(self) -> Optional[dict]:
        """
        Usable space of the whole pool in bytes (used + available of its root
        dataset, so mirrors and raidz parity are already accounted for).
        Read-only; the pool also holds data that is not ggNet's.
        """
        ok, out, _ = self._run(["zfs", "list", "-H", "-p", "-o", "used,avail", self.layout.pool])
        parts = out.split("\t") if ok else []
        if len(parts) != 2:
            return None
        used, avail = _int(parts[0]), _int(parts[1])
        return {"total": used + avail, "used": used, "available": avail}

    def reserved_bytes(self) -> int:
        if not self.dataset_exists(self.reserved_path):
            return 0
        ok, out, _ = self._run(["zfs", "get", "-H", "-p", "-o", "value",
                                "refreservation", self.reserved_path])
        return _int(out) if ok else 0

    def set_reserved(self, nbytes: int) -> bool:
        """
        Keep `nbytes` of the pool free for nobody ("Reserved disk space"):
        SSDs slow down when they are nearly full, and writebacks must never
        fill the pool. 0 removes the reservation (the empty dataset stays).
        """
        path = self.reserved_path
        if not self._is_managed(path, allow_snapshot=False) or nbytes < 0:
            return False
        value = str(nbytes) if nbytes else "none"
        if self.dataset_exists(path):
            ok, _, _ = self._run(["zfs", "set", f"refreservation={value}", path])
        elif nbytes:
            ok, _, _ = self._run(["zfs", "create", "-o", "canmount=off", "-o", "mountpoint=none",
                                  "-o", f"refreservation={value}", path])
        else:
            ok = True
        if ok:
            logger.info("Reserved space under %s set to %s", path, value)
        return ok

    def pool_status(self) -> str:
        ok, out, err = self._run(["zpool", "status", "-v", self.layout.pool])
        return out if ok else err

    def pool_list(self) -> str:
        _, out, _ = self._run([
            "zpool", "list", "-H", "-o", "name,size,alloc,free,health", self.layout.pool,
        ])
        return out

    def scrub_start(self) -> bool:
        ok, _, _ = self._run(["zpool", "scrub", self.layout.pool])
        return ok

    def scrub_stop(self) -> bool:
        ok, _, _ = self._run(["zpool", "scrub", "-s", self.layout.pool])
        return ok

    # ── Dataset properties ────────────────────────────────────────────

    def get_property(self, dataset: str, prop: str) -> str:
        if not self._is_managed(dataset, allow_base=True) or prop.startswith("-"):
            return ""
        ok, out, _ = self._run(["zfs", "get", "-H", "-o", "value", prop, dataset])
        return out if ok else ""

    def set_property(self, dataset: str, prop: str, value: str) -> bool:
        if not self._is_managed(dataset, allow_base=True) or prop.startswith("-"):
            return False
        ok, _, _ = self._run(["zfs", "set", f"{prop}={value}", dataset])
        return ok

    def list_clients(self) -> list[dict]:
        """All per-client clones under storage.writebacks, with size (bytes) and origin."""
        ok, out, _ = self._run([
            "zfs", "list", "-r", "-H", "-p",
            "-o", "name,used,refer,origin",
            self.layout.writebacks,
        ])
        if not ok or not out:
            return []
        clients = []
        for line in out.splitlines():
            parts = line.split("\t")
            # Skip the writebacks dataset itself; only its children are clients.
            if len(parts) == 4 and parts[0] != self.layout.writebacks:
                clients.append({
                    "zvol": parts[0],
                    "used": _int(parts[1]),
                    "referenced": _int(parts[2]),
                    "cloned_from": parts[3],
                })
        return clients

    def snapshot_details(self, dataset: str) -> Optional[list[dict]]:
        """
        Snapshots of one master, oldest first: name (after '@'), creation
        (Unix time), used and referenced (bytes), the clones made from it and
        its comment (None when unset) and whether it is locked. None if they
        cannot be listed.
        """
        if not self._is_managed(dataset, allow_snapshot=False):
            return None
        ok, out, _ = self._run([
            "zfs", "list", "-H", "-p", "-t", "snapshot",
            "-o", f"name,creation,used,refer,clones,{COMMENT_PROP},{LOCK_PROP}",
            "-s", "createtxg", "-d", "1", dataset,
        ])
        if not ok:
            return None
        snaps = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 7 or "@" not in parts[0]:
                continue
            snaps.append({
                "name": parts[0].partition("@")[2],
                "creation": _int(parts[1]),
                "used": _int(parts[2]),
                "referenced": _int(parts[3]),
                "clones": [c for c in parts[4].split(",") if c and c != "-"],
                "comment": None if parts[5] in ("", "-") else parts[5],
                "locked": parts[6] == "on",
            })
        return snaps

    def set_snapshot_comment(self, snapshot: str, comment: str) -> bool:
        """Set a version's comment; an empty one removes it."""
        if "@" not in snapshot or not self._is_managed(snapshot) or not valid_comment(comment):
            return False
        cmd = (["zfs", "set", f"{COMMENT_PROP}={comment}", snapshot] if comment
               else ["zfs", "inherit", COMMENT_PROP, snapshot])
        ok, _, err = self._run(cmd)
        if not ok:
            logger.error("Setting the comment of %s failed: %s", snapshot, err)
        return ok

    def set_snapshot_lock(self, snapshot: str, locked: bool) -> bool:
        """Lock a version against deletion, or unlock it."""
        if "@" not in snapshot or not self._is_managed(snapshot):
            return False
        cmd = (["zfs", "set", f"{LOCK_PROP}=on", snapshot] if locked
               else ["zfs", "inherit", LOCK_PROP, snapshot])
        ok, _, err = self._run(cmd)
        if not ok:
            logger.error("%s %s failed: %s", "Locking" if locked else "Unlocking", snapshot, err)
        return ok

    def destroy_snapshot(self, snapshot: str, tag: str = "ggnet:protected") -> bool:
        """
        Delete one version of a master: release its hold, then destroy it.
        Refused while clones depend on it (checked first, so the hold is never
        dropped for nothing); if the destroy still fails, the hold is restored.
        """
        if "@" not in snapshot or not self._is_managed(snapshot):
            return False
        dataset, _, name = snapshot.partition("@")
        details = self.snapshot_details(dataset)
        snap = next((d for d in details or [] if d["name"] == name), None)
        if snap is None:
            self.runner.last_error = f"{snapshot} does not exist"
            return False
        if snap["clones"]:
            self.runner.last_error = f"{snapshot} is still used by: {', '.join(snap['clones'])}"
            return False
        self.release_snapshot(snapshot, tag)
        ok, _, err = self._run(["zfs", "destroy", snapshot])
        if not ok:
            self.protect_snapshot(snapshot, tag)
            self.runner.last_error = err
            return False
        logger.info("Snapshot %s deleted", snapshot)
        return True

if __name__ == "__main__":
    # Manual smoke test (runs `zpool list` on the host): python -m app.zfs_manager
    logging.basicConfig(level=logging.INFO)
    print(ZFSManager().pool_list())
