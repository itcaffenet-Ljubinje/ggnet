"""Provisioner on FakeHost: order of operations and recovery from errors."""

from __future__ import annotations

import pytest

from app.services.provisioning import ProvisioningError

MASTER = "tank/ggnet/images/cs2"
SNAP = f"{MASTER}@base"
CLONE = "tank/ggnet/writebacks/pc01"
IQN = "iqn.1991-05.com.microsoft:pc01"
TARGET = "iqn.2025-05.net.ggnet:storage"
DEV = f"/dev/zvol/{CLONE}"


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
    assert host.visible(IQN) == [DEV]


def test_provision_rolls_back_clone_when_iscsi_fails(host, prov, published):
    host.fail_on[("targetcli", f"/iscsi/{TARGET}/tpg1/acls", "create")] = "Could not create NodeACL"
    with pytest.raises(ProvisioningError, match="Could not create NodeACL"):
        prov.provision("pc01", IQN, SNAP)
    assert CLONE not in host.datasets
    assert not host.backstores and host.visible(IQN) == []


def test_provision_refuses_leftover_clone(host, prov, published):
    host.add_dataset(CLONE, origin=SNAP)
    with pytest.raises(ProvisioningError, match="deprovision"):
        prov.provision("pc01", IQN, SNAP)


def test_reset_order(host, prov, published):
    """Detach (ACL + backstore) → destroy clone → clone → attach."""
    prov.provision("pc01", IQN, SNAP)
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP)
    i_detach = host.calls.index(["targetcli", "/backstores/block", "delete", "pc01-game"])
    i_destroy = next(i for i, c in enumerate(host.calls) if c[:2] == ["zfs", "destroy"])
    i_clone = next(i for i, c in enumerate(host.calls) if c[:2] == ["zfs", "clone"])
    i_attach = next(i for i, c in enumerate(host.calls)
                    if c[:3] == ["targetcli", "/backstores/block", "create"])
    assert i_detach < i_destroy < i_clone < i_attach
    assert host.visible(IQN) == [DEV]


def test_reset_sends_few_targetcli_commands(host, prov, published):
    """A restarting PC waits for this at iPXE, and each targetcli call loads all of LIO."""
    prov.provision("pc01", IQN, SNAP)
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP, target_iqn=TARGET)
    tcli = [c[1:] for c in host.calls if c[0] == "targetcli"]
    assert tcli == [
        [f"/iscsi/{TARGET}/tpg1/acls", "delete", IQN],
        ["/backstores/block", "delete", "pc01-os"],
        ["/backstores/block", "delete", "pc01-game"],
        ["/backstores/block", "ls", "depth=1"],                  # both slots checked at once
        [f"/iscsi/{TARGET}/tpg1/luns", "ls", "depth=1"],         # the shared target is known by now
        ["/backstores/block", "create", "name=pc01-game", f"dev={DEV}"],
        [f"/iscsi/{TARGET}/tpg1/luns", "create", "/backstores/block/pc01-game", "lun=0", "add_mapped_luns=false"],
        [f"/iscsi/{TARGET}/tpg1/acls", "create", IQN, "add_mapped_luns=false"],
        [f"/iscsi/{TARGET}/tpg1/acls/{IQN}", "create", "mapped_lun=0", "tpg_lun_or_backstore=lun0",
         "write_protect=false"],
        ["saveconfig"],                                           # saved once
    ]
    assert host.visible(IQN) == [DEV]


def test_new_game_disks_get_64k_blocks_and_lz4(host, prov):
    prov.create_disk("cs2", 10)
    prov.create_disk("win11", 10, os_image=True)
    creates = {c[-1]: c for c in host.calls if c[:2] == ["zfs", "create"]}
    game, os_ = creates["tank/ggnet/images/cs2"], creates["tank/ggnet/images/win11"]
    assert "volblocksize=64k" in game and "compression=lz4" in game and "sync=standard" in game
    assert "volblocksize=16k" in os_ and "compression=lz4" in os_


def test_reset_of_an_old_per_client_machine_still_cleans_up(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP, target_iqn="iqn.2025-05.net.ggnet:client-pc01")
    assert ["targetcli", "/iscsi", "delete", "iqn.2025-05.net.ggnet:client-pc01"] in host.calls
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP)                           # unknown target: same
    assert ["targetcli", "/backstores/block", "delete", "client-pc01"] in host.calls


def test_reset_leaves_other_machines_alone(host, prov, published):
    """With one shared target, resetting pc01 must not touch pc02's session."""
    iqn2 = "iqn.1991-05.com.microsoft:pc02"
    prov.provision("pc01", IQN, SNAP)
    prov.provision("pc02", iqn2, SNAP)
    host.calls.clear()
    prov.reset("pc01", IQN, CLONE, SNAP)
    assert host.visible(iqn2) == ["/dev/zvol/tank/ggnet/writebacks/pc02"]
    assert not any(iqn2 in c or "pc02-game" in c for c in host.calls)
    assert ["targetcli", "/iscsi", "delete", TARGET] not in host.calls


def test_reset_switches_to_new_snapshot(host, prov, published):
    """A reset onto @v2 makes the clone from @v2."""
    prov.provision("pc01", IQN, SNAP)
    host.snapshots[f"{MASTER}@v2"] = set()
    cd = prov.reset("pc01", IQN, CLONE, f"{MASTER}@v2")
    assert cd.clone_snapshot == f"{MASTER}@v2"
    assert host.datasets[CLONE]["origin"] == f"{MASTER}@v2"


def test_reset_stops_if_backstore_survives(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    host.fail_on[("targetcli", "/backstores/block", "delete")] = "Storage object in use"
    with pytest.raises(ProvisioningError, match="iSCSI disk was not removed"):
        prov.reset("pc01", IQN, CLONE, SNAP)
    assert CLONE in host.datasets          # clone untouched


def test_reset_stops_if_backstores_cannot_be_listed(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    host.fail_on[("targetcli", "/backstores/block", "ls")] = "targetcli crashed"
    with pytest.raises(ProvisioningError, match="iSCSI disk was not removed"):
        prov.reset("pc01", IQN, CLONE, SNAP)
    assert CLONE in host.datasets


def test_deprovision_removes_everything(host, prov, published):
    prov.provision("pc01", IQN, SNAP)
    prov.deprovision("pc01", IQN, CLONE)
    assert CLONE not in host.datasets
    assert not host.backstores and host.visible(IQN) == []
    assert TARGET in host.targets          # the shared target stays


def test_deprovision_is_idempotent(host, prov, published):
    prov.deprovision("pc01", IQN, None)
    prov.deprovision("pc01", IQN, CLONE)


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
