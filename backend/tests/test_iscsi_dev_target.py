"""
Integration test against the real LIO target on the host.

Runs real zfs and targetcli commands, so it is skipped unless both are set:
  GGNET_ALLOW_DESTRUCTIVE_TESTS=yes
  GGNET_CONFIG=<config whose storage.root_dataset AND services.iscsi_target_name
               end in "-dev">

It creates two 1G zvols under storage.writebacks and attaches them to the
dev shared target (`<prefix>:<name>-dev`, never the production one) with
ACLs for two made-up initiators, so no real client can log in. Everything it
created is removed at the end, including the dev target if this test created
it. The test refuses to run if its zvols or backstores already exist, so
cleanup can never delete something it did not create.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_config
from app.iscsi_manager import Disk, ISCSIManager
from app.zfs_manager import ZFSManager

pytestmark = pytest.mark.destructive

MACHINES = {
    "pytest-dev1": "iqn.2026-10.test.ggnet:pytest-dev1",
    "pytest-dev2": "iqn.2026-10.test.ggnet:pytest-dev2",
}


@pytest.fixture
def dev_managers() -> tuple[ZFSManager, ISCSIManager]:
    if not os.environ.get("GGNET_CONFIG"):
        pytest.fail("GGNET_CONFIG must point to the dev config")
    get_config.cache_clear()
    zfs = ZFSManager()
    if not zfs.layout.root_dataset.endswith("-dev"):
        pytest.fail(f"Refusing to run: {zfs.layout.root_dataset!r} is not a -dev dataset")
    iscsi = ISCSIManager()
    if not iscsi.cfg.target_name.endswith("-dev"):
        pytest.fail(f"Refusing to run: target {iscsi.target_iqn()!r} is not a -dev target")
    return zfs, iscsi


def _tpg_ls(iscsi: ISCSIManager, sub: str) -> str:
    ok, out, err = iscsi.runner.run(["targetcli", f"/iscsi/{iscsi.target_iqn()}/tpg1/{sub}", "ls"])
    assert ok, err
    return out


def test_shared_target_with_per_machine_acls(dev_managers):
    zfs, iscsi = dev_managers
    zvols = {m: f"{zfs.layout.writebacks}/{m}" for m in MACHINES}

    target_existed = iscsi.target_exists()
    # None means targetcli failed; never assume the target is absent.
    assert target_existed is not None, "targetcli /iscsi ls failed"
    for m, zvol in zvols.items():
        assert iscsi.backstore_exists(m, "game") is False, f"{m}-game left over from an earlier run"
        assert not zfs.dataset_exists(zvol), f"{zvol} left over from an earlier run"

    created = []
    try:
        for m, zvol in zvols.items():
            assert zfs.create_zvol(zvol, 1)
            created.append(m)
            disks = [Disk("game", zfs.zvol_device_path(zvol))]
            assert iscsi.attach(m, MACHINES[m], disks) == iscsi.target_iqn()

        portals = _tpg_ls(iscsi, "portals")
        assert f"{iscsi.portal_ip}:{iscsi.portal_port}" in portals
        assert "0.0.0.0" not in portals

        # Each ACL has exactly one mapped LUN: its own disk.
        for m, initiator in MACHINES.items():
            acl = _tpg_ls(iscsi, f"acls/{initiator}")
            assert f"block/{m}-game" in acl
            other = next(o for o in MACHINES if o != m)
            assert f"block/{other}-game" not in acl

        # LIO holds the zvol open while its backstore exists.
        assert not zfs.destroy(zvols["pytest-dev1"])

        assert iscsi.detach("pytest-dev1", MACHINES["pytest-dev1"])
        assert iscsi.backstore_exists("pytest-dev1", "game") is False
        assert iscsi.backstore_exists("pytest-dev2", "game") is True
    finally:
        for m in created:
            iscsi.detach(m, MACHINES[m])
            assert zfs.destroy(zvols[m])
        if target_existed is False:
            iscsi.runner.run(["targetcli", "/iscsi", "delete", iscsi.target_iqn()])
            iscsi.save_config()
