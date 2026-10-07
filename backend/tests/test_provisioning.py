"""Provisioner on FakeHost: order of operations and recovery from errors."""

from __future__ import annotations

import pytest

from app.services.provisioning import ProvisioningError

MASTER = "tank/ggnet/images/cs2"
SNAP = f"{MASTER}@base"
CLONE = "tank/ggnet/writebacks/pc01"
IQN = "iqn.1991-05.com.microsoft:pc01"
TARGET = "iqn.2025-05.net.ggnet:client-pc01"


@pytest.fixture
def published(host, prov):
    prov.create_disk("cs2", 10)
    prov.publish_disk(MASTER)
    return SNAP


def test_paths_come_from_layout(prov):
    assert prov.image_path("cs2") == MASTER
    assert prov.client_path("pc01") == CLONE


def test_publish_protects_master(host, published):
    assert host.snapshots[SNAP] == {"ggnet:protected"}
    assert host.datasets[MASTER]["readonly"]


def test_create_disk_refuses_existing(host, prov):
    host.add_dataset(MASTER)
    with pytest.raises(ProvisioningError, match="already exists"):
        prov.create_disk("cs2", 10)


def test_create_disk_host_failure(host, prov):
    host.fail_on[("zfs", "create")] = "out of space"
    with pytest.raises(ProvisioningError, match="out of space"):
        prov.create_disk("cs2", 10)


def test_provision_creates_clone_and_target(host, prov, published):
    cd = prov.provision("pc01", IQN, SNAP)
    assert (cd.clone_zvol, cd.clone_snapshot, cd.iscsi_target_iqn) == (CLONE, SNAP, TARGET)
    assert host.datasets[CLONE]["origin"] == SNAP
    assert TARGET in host.targets


def test_provision_rolls_back_clone_when_target_fails(host, prov, published):
    host.fail_on[("targetcli", "/iscsi", "create")] = "Could not create Target"
    with pytest.raises(ProvisioningError, match="Could not create Target"):
        prov.provision("pc01", IQN, SNAP)
    assert CLONE not in host.datasets
    assert not host.backstores and not host.targets


def test_provision_refuses_leftover_clone(host, prov, published):
    host.add_dataset(CLONE, origin=SNAP)
    with pytest.raises(ProvisioningError, match="deprovision"):
        prov.provision("pc01", IQN, SNAP)


def test_reset_order(host, prov, published):
    """Delete target → destroy clone → clone → create target."""
    prov.provision("pc01", IQN, SNAP)
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP)
    steps = [c[:3] for c in host.calls]
    i_del_target = steps.index(["targetcli", "/iscsi", "delete"])
    i_destroy = next(i for i, c in enumerate(host.calls) if c[:2] == ["zfs", "destroy"])
    i_clone = next(i for i, c in enumerate(host.calls) if c[:2] == ["zfs", "clone"])
    i_new_target = steps.index(["targetcli", "/iscsi", "create"])
    assert i_del_target < i_destroy < i_clone < i_new_target
    assert TARGET in host.targets


def test_reset_switches_to_new_snapshot(host, prov, published):
    """A reset onto @v2 makes the clone from @v2."""
    prov.provision("pc01", IQN, SNAP)
    host.snapshots[f"{MASTER}@v2"] = set()
    cd = prov.reset("pc01", IQN, CLONE, f"{MASTER}@v2")
    assert cd.clone_snapshot == f"{MASTER}@v2"
    assert host.datasets[CLONE]["origin"] == f"{MASTER}@v2"


def test_reset_stops_if_target_survives(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    host.fail_on[("targetcli", "/iscsi", "delete")] = "Target in use"
    with pytest.raises(ProvisioningError, match="target was not deleted"):
        prov.reset("pc01", IQN, CLONE, SNAP)
    assert CLONE in host.datasets          # clone untouched


def test_target_exists_is_exact_match(host, prov, published):
    """client-pc1 must not match client-pc10."""
    host.targets.add("iqn.2025-05.net.ggnet:client-pc10")
    assert not prov._target_exists("pc1")
    assert prov._target_exists("pc10")


def test_deprovision_removes_everything(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    prov.deprovision("pc01", CLONE)
    assert CLONE not in host.datasets
    assert not host.targets and not host.backstores


def test_deprovision_is_idempotent(host, prov, published):
    prov.deprovision("pc01", None)
    prov.deprovision("pc01", CLONE)


def test_delete_published_disk_with_clone_refused(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    with pytest.raises(ProvisioningError):
        prov.delete_disk(MASTER, published=True)
    assert host.snapshots[SNAP] == {"ggnet:protected"}   # hold kept


def test_delete_published_disk(host, prov, published):
    prov.delete_disk(MASTER, published=True)
    assert MASTER not in host.datasets and SNAP not in host.snapshots


def test_delete_draft_disk(host, prov):
    prov.create_disk("cs2", 10)
    prov.delete_disk(MASTER, published=False)
    assert MASTER not in host.datasets


def test_delete_missing_disk_is_noop(host, prov):
    host.calls.clear()
    prov.delete_disk(MASTER, published=True)
    assert all(c[:2] == ["zfs", "list"] for c in host.calls)
