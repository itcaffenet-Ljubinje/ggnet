"""
Integration test against a real ZFS dev dataset on the host.

Runs real zfs commands, so it is skipped unless both are set:
  GGNET_ALLOW_DESTRUCTIVE_TESTS=yes
  GGNET_CONFIG=<config whose storage.root_dataset ends in "-dev">

The "-dev" check keeps this test away from the production ggNet tree.
It creates a 1G zvol under storage.images and removes everything it created.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_config
from app.zfs_manager import ZFSManager

pytestmark = pytest.mark.destructive

MASTER_NAME = "pytest-master"
CLIENT_NAME = "pytest-pc01"


@pytest.fixture
def dev_zfs() -> ZFSManager:
    if not os.environ.get("GGNET_CONFIG"):
        pytest.fail("GGNET_CONFIG must point to the dev config")
    get_config.cache_clear()
    zfs = ZFSManager()
    if not zfs.layout.root_dataset.endswith("-dev"):
        pytest.fail(f"Refusing to run: {zfs.layout.root_dataset!r} is not a -dev dataset")
    return zfs


def test_master_clone_reset_cycle(dev_zfs):
    zfs = dev_zfs
    master = f"{zfs.layout.images}/{MASTER_NAME}"
    snap = f"{master}@base"
    client = f"{zfs.layout.writebacks}/{CLIENT_NAME}"

    assert zfs.setup_dataset_tree()
    assert not zfs.dataset_exists(master), f"{master} left over from an earlier run"

    try:
        assert zfs.create_zvol(master, 1)
        assert zfs.publish_master(master)
        assert zfs.get_property(master, "readonly") == "on"

        assert zfs.clone(snap, client)
        # Other clones (e.g. from real-PC tests) may live in the dev tree too.
        ours = [c for c in zfs.list_clients() if c["zvol"] == client]
        assert len(ours) == 1
        assert ours[0]["cloned_from"] == snap

        # The master must not be destroyable while a clone exists.
        assert not zfs.destroy_master(master)
        assert zfs.dataset_exists(master)

        assert zfs.reset_clone(client, snap)
        assert zfs.dataset_exists(client)
    finally:
        if zfs.dataset_exists(client):
            zfs.destroy(client, recursive=True)
        if zfs.dataset_exists(master):
            assert zfs.destroy_master(master)

    assert not zfs.dataset_exists(client)
    assert not zfs.dataset_exists(master)


def test_apply_clone_on_real_zfs(dev_zfs):
    """
    Apply Writebacks with real zfs: a kept writeback becomes master@v2 while
    another client's clone stays on @base. (A send|recv of the clone stream
    into the master was refused by the real host; only FakeHost accepted it.)
    """
    zfs = dev_zfs
    master = f"{zfs.layout.images}/{MASTER_NAME}"
    keeper = f"{zfs.layout.writebacks}/{CLIENT_NAME}"
    other = f"{zfs.layout.writebacks}/pytest-pc02"
    old = f"{master}_ggnet_old"
    for ds in (master, keeper, other, old):
        assert not zfs.dataset_exists(ds), f"{ds} left over from an earlier run"

    try:
        assert zfs.create_zvol(master, 1)
        assert zfs.publish_master(master)
        assert zfs.clone(f"{master}@base", keeper)
        assert zfs.clone(f"{master}@base", other)
        # The admin's change on the kept writeback (wait for udev's /dev/zvol link).
        zfs.runner.run(["udevadm", "settle"])
        ok, _, err = zfs.runner.run(["dd", "if=/dev/urandom", f"of={zfs.zvol_device_path(keeper)}",
                                     "bs=1M", "count=4", "oflag=direct"])
        assert ok, err

        assert zfs.apply_clone(keeper, master, "base", "v2"), zfs.runner.last_error

        assert zfs.list_snapshots(master) == ["base", "v2"]
        assert zfs.get_property(master, "readonly") == "on"
        assert zfs.get_property(master, "origin") == "-"
        assert zfs.get_property(other, "origin") == f"{master}@base"
        assert not zfs.dataset_exists(keeper) and not zfs.dataset_exists(old)
        ok, out, _ = zfs.runner.run(["zfs", "holds", "-H", f"{master}@base", f"{master}@v2"])
        assert ok and out.count("ggnet:protected") == 2

        # Images page: versions with their clones; a used version cannot be deleted.
        details = {d["name"]: d for d in zfs.snapshot_details(master)}
        assert details["base"]["clones"] == [other] and details["v2"]["clones"] == []
        assert details["v2"]["referenced"] > 0 and details["base"]["creation"] <= details["v2"]["creation"]
        assert not zfs.destroy_snapshot(f"{master}@base")
        ok, out, _ = zfs.runner.run(["zfs", "holds", "-H", f"{master}@base"])
        assert ok and "ggnet:protected" in out              # hold kept after the refusal
        assert zfs.destroy(other)
        assert zfs.destroy_snapshot(f"{master}@base"), zfs.runner.last_error
        assert zfs.list_snapshots(master) == ["v2"]
    finally:
        zfs.runner.run(["zfs", "release", "ggnet:protected", f"{keeper}@v2"], quiet=True)
        for ds in (other, keeper):
            if zfs.dataset_exists(ds):
                zfs.destroy(ds, recursive=True)
        if zfs.dataset_exists(old):
            zfs.destroy(old)
        if zfs.dataset_exists(master):
            assert zfs.destroy_master(master)

    for ds in (master, keeper, other, old):
        assert not zfs.dataset_exists(ds)
