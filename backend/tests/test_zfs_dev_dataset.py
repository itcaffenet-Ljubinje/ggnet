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
        assert [c["zvol"] for c in zfs.list_clients()] == [client]
        assert zfs.list_clients()[0]["cloned_from"] == snap

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
