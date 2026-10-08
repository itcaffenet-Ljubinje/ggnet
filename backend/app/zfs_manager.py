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
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.config import get_config
from app.runner import CommandRunner

logger = logging.getLogger("ggnet.zfs")

# Allowed characters in one component of a ZFS name. Deliberately narrower
# than what ZFS accepts (no spaces, no '%'), and a component must not start
# with '-' so it cannot be parsed as a command option.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")

# Datasets under root_dataset that ggNet creates and manages.
_CHILD_KEYS = ("images", "writebacks", "snapshots", "iscsi_targets")


@dataclass(frozen=True)
class ZFSLayout:
    """ZFS names from the [storage] section of config.toml."""

    pool: str
    root_dataset: str
    images: str
    writebacks: str           # per-client clones (the old code's "clients")
    snapshots: str
    iscsi_targets: str

    @classmethod
    def from_config(cls, cfg: Optional[Mapping[str, Any]] = None) -> "ZFSLayout":
        storage = (cfg if cfg is not None else get_config()).get("storage")
        if not isinstance(storage, Mapping):
            raise ValueError("config.toml has no [storage] section")
        keys = ("pool", "root_dataset", *_CHILD_KEYS)
        missing = [k for k in keys if not storage.get(k)]
        if missing:
            raise ValueError(f"[storage] is missing: {', '.join(missing)}")
        return cls(**{k: str(storage[k]) for k in keys})


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
        ok, _, _ = self._run(cmd)
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

    def apply_clone(self, clone: str, master: str, base_snap: str, new_snap: str) -> bool:
        """
        Turn a client's writeback into a new version of its master
        (ggRock "Apply Writebacks"):

            clone@ggnet-apply → zfs send -i master@base_snap | zfs recv master
            → master@ggnet-apply renamed to master@new_snap → hold

        Only the changed blocks are written into the master; master@new_snap
        is an ordinary snapshot with no dependency on the clone. Refused when
        base_snap is not the master's newest snapshot (another version was
        applied in between). The master stays readonly=on throughout; zfs recv
        is not blocked by it.
        """
        tmp = "ggnet-apply"
        base = f"{master}@{base_snap}"
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

        # A leftover from an interrupted apply; it is ours, so drop it.
        self._run(["zfs", "destroy", f"{clone}@{tmp}"], quiet=True)
        if not self.snapshot(clone, tmp):
            return False
        ok, _, err = self.runner.run_pipe(
            ["zfs", "send", "-i", base, f"{clone}@{tmp}"],
            ["zfs", "recv", master],
        )
        if not ok:
            logger.error("Applying %s to %s failed: %s", clone, master, err)
            self._run(["zfs", "destroy", f"{clone}@{tmp}"], quiet=True)
            self.runner.last_error = err
            return False
        steps = [
            ["zfs", "rename", f"{master}@{tmp}", f"{master}@{new_snap}"],
            ["zfs", "hold", "ggnet:protected", f"{master}@{new_snap}"],
        ]
        for step in steps:
            ok, _, err = self._run(step)
            if not ok:
                logger.error("Apply of %s left %s in place: %s", clone, step, err)
                return False
        self._run(["zfs", "destroy", f"{clone}@{tmp}"], quiet=True)
        logger.info("Applied writeback %s to %s as @%s", clone, master, new_snap)
        return True

    def zvol_device_path(self, zvol_path: str) -> str:
        """Path to the block device, input for the iSCSI backstore."""
        return f"/dev/zvol/{zvol_path}"

    # ── Pool status / monitoring ──────────────────────────────────────

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
        """All per-client clones under storage.writebacks, with size and origin."""
        ok, out, _ = self._run([
            "zfs", "list", "-r", "-H",
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
                    "used": parts[1],
                    "referenced": parts[2],
                    "cloned_from": parts[3],
                })
        return clients


if __name__ == "__main__":
    # Manual smoke test (runs `zpool list` on the host): python -m app.zfs_manager
    logging.basicConfig(level=logging.INFO)
    print(ZFSManager().pool_list())
