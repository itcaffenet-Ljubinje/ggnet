"""
End-to-end test of Provisioner against real ZFS and LIO on the host.

Runs real zfs and targetcli commands, so it is skipped unless both are set:
  GGNET_ALLOW_DESTRUCTIVE_TESTS=yes
  GGNET_CONFIG=<config whose storage.root_dataset ends in "-dev">

Full Disk Mode cycle on the -dev tree: 1G master → publish → provision a
machine (clone + target) → reset → deprovision → delete the master. The
target's ACL allows only a made-up initiator, so no real client can log in.
The test refuses to start if anything it would create already exists, so
cleanup can never delete something it did not create.
"""

from __future__ import annotations

import os

import pytest

from app.config import get_config
from app.iscsi_manager import ISCSIManager
from app.services.provisioning import Provisioner
from app.zfs_manager import ZFSManager

pytestmark = pytest.mark.destructive

NAME = "pytest-e2e"          # used as both the disk name and the machine name
FAKE_INITIATOR = "iqn.2026-10.test.ggnet:pytest-e2e"


@pytest.fixture
def prov() -> Provisioner:
    if not os.environ.get("GGNET_CONFIG"):
        pytest.fail("GGNET_CONFIG must point to the dev config")
    get_config.cache_clear()
    zfs = ZFSManager()
    if not zfs.layout.root_dataset.endswith("-dev"):
        pytest.fail(f"Refusing to run: {zfs.layout.root_dataset!r} is not a -dev dataset")
    return Provisioner(zfs, ISCSIManager())


def _portals(prov: Provisioner, target: str) -> str:
    ok, out, err = prov.iscsi.runner.run(["targetcli", f"/iscsi/{target}/tpg1/portals", "ls"])
    assert ok, err
    return out


def test_full_disk_mode_cycle(prov):
    zfs, iscsi = prov.zfs, prov.iscsi
    master = prov.image_path(NAME)
    clone = prov.client_path(NAME)
    target = iscsi.target_iqn(NAME)

    # An empty listing means targetcli failed; never assume the target is absent.
    assert iscsi.list_targets(), "targetcli /iscsi ls failed"
    assert not prov._target_exists(NAME), f"{target} left over from an earlier run"
    assert not zfs.dataset_exists(master), f"{master} left over from an earlier run"
    assert not zfs.dataset_exists(clone), f"{clone} left over from an earlier run"

    assert prov.create_disk(NAME, 1) == master
    try:
        snapshot = f"{master}@{prov.publish_disk(master)}"
        assert zfs.get_property(master, "readonly") == "on"

        cd = prov.provision(NAME, FAKE_INITIATOR, snapshot)
        assert (cd.clone_zvol, cd.clone_snapshot, cd.iscsi_target_iqn) == (clone, snapshot, target)
        assert prov._target_exists(NAME)
        portals = _portals(prov, target)
        assert f"{iscsi.portal_ip}:{iscsi.portal_port}" in portals
        assert "0.0.0.0" not in portals

        # The master cannot be deleted while a machine's clone depends on it.
        with pytest.raises(Exception):
            prov.delete_disk(master, published=True)
        assert zfs.dataset_exists(master)

        # Reset: delete target → destroy clone → clone → create target.
        cd = prov.reset(NAME, FAKE_INITIATOR, clone, snapshot)
        assert cd.iscsi_target_iqn == target
        assert prov._target_exists(NAME)
        assert zfs.dataset_exists(clone)

        prov.deprovision(NAME, clone)
        assert not prov._target_exists(NAME)
        assert not zfs.dataset_exists(clone)
    finally:
        # Best-effort cleanup; deprovision is safe when nothing exists.
        prov.deprovision(NAME, clone)
        prov.delete_disk(master, published=zfs.dataset_exists(f"{master}@base"))

    assert not zfs.dataset_exists(master)
