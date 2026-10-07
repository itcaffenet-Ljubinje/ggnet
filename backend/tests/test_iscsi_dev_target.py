"""
Integration test against the real LIO target on the host.

Runs real zfs and targetcli commands, so it is skipped unless both are set:
  GGNET_ALLOW_DESTRUCTIVE_TESTS=yes
  GGNET_CONFIG=<config whose storage.root_dataset ends in "-dev">

It creates a 1G zvol under storage.writebacks and an iSCSI target for it on
services.iscsi_portal, whose ACL allows only a made-up initiator, so no real
client can log in. Both are
removed at the end. The test refuses to run if the target already exists, so
cleanup can never delete a target it did not create.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_config
from app.iscsi_manager import ISCSIManager
from app.zfs_manager import ZFSManager

pytestmark = pytest.mark.destructive

MACHINE_ID = "pytest-dev"
FAKE_INITIATOR = "iqn.2026-10.test.ggnet:pytest-dev"


@pytest.fixture
def dev_managers() -> tuple[ZFSManager, ISCSIManager]:
    if not os.environ.get("GGNET_CONFIG"):
        pytest.fail("GGNET_CONFIG must point to the dev config")
    get_config.cache_clear()
    zfs = ZFSManager()
    if not zfs.layout.root_dataset.endswith("-dev"):
        pytest.fail(f"Refusing to run: {zfs.layout.root_dataset!r} is not a -dev dataset")
    return zfs, ISCSIManager()


def test_create_and_delete_target(dev_managers):
    zfs, iscsi = dev_managers
    zvol = f"{zfs.layout.writebacks}/{MACHINE_ID}"
    target = iscsi.target_iqn(MACHINE_ID)

    listing = iscsi.list_targets()
    # An empty listing means targetcli failed; never assume the target is absent.
    assert listing, "targetcli /iscsi ls failed"
    assert target not in listing, f"{target} left over from an earlier run"
    assert not zfs.dataset_exists(zvol), f"{zvol} left over from an earlier run"

    assert zfs.create_zvol(zvol, 1)
    try:
        assert iscsi.create_target(MACHINE_ID, zfs.zvol_device_path(zvol), FAKE_INITIATOR) == target
        assert target in iscsi.list_targets()

        # The target listens only on the configured portal, not on 0.0.0.0.
        ok, portals, err = iscsi.runner.run(["targetcli", f"/iscsi/{target}/tpg1/portals", "ls"])
        assert ok, err
        assert f"{iscsi.portal_ip}:{iscsi.portal_port}" in portals
        assert "0.0.0.0" not in portals

        # LIO holds the zvol open while the target exists.
        assert not zfs.destroy(zvol)
    finally:
        iscsi.delete_target(MACHINE_ID)
        assert target not in iscsi.list_targets()
        assert zfs.destroy(zvol)
