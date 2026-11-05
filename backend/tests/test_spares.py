"""Spare writebacks: made ahead, swapped in by a reset (Provisioner, writebacks.refresh_spares)."""

from __future__ import annotations

import pytest

from app.services.provisioning import SETUP_PROP, setup_tag
from app.windows_setup import OsSetup

GAME = "tank/ggnet/images/cs2"
OS = "tank/ggnet/images/win11"
CLONE = "tank/ggnet/writebacks/pc01"
OS_CLONE = "tank/ggnet/writebacks/pc01-os"
IQN = "iqn.2025-05.net.ggnet:pc-aabbccddee01"
TARGET = "iqn.2025-05.net.ggnet:storage"
SETUP = OsSetup("pc01", {"GGNET_MACHINE_ID": "1"})


class FakeWindows:
    def __init__(self, problem: str | None = None):
        self.problem = problem
        self.personalized: list[str] = []

    def personalize(self, device, setup, label=None):
        self.personalized.append(device)
        self.labels = getattr(self, "labels", []) + [label]
        return self.problem


@pytest.fixture
def boot(host, prov):
    """A Boot Mode PC with both writebacks, from @base of both images."""
    for disk in ("cs2", "win11"):
        prov.create_disk(disk, 10)
        prov.publish_disk(f"tank/ggnet/images/{disk}")
    prov.windows = FakeWindows()
    prov.provision("pc01", IQN, f"{GAME}@base", f"{OS}@base", SETUP)
    prov.windows.personalized.clear()
    return prov


def _reset(prov, setup=SETUP, os_snap=f"{OS}@base"):
    return prov.reset("pc01", IQN, CLONE, f"{GAME}@base", OS_CLONE, os_snap, setup, target_iqn=TARGET)


def test_spares_are_made_once_and_set_up_as_the_pc(host, boot):
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP) == [
        f"{CLONE}_spare", f"{OS_CLONE}_spare"]
    assert host.datasets[f"{OS_CLONE}_spare"]["origin"] == f"{OS}@base"
    assert host.datasets[f"{OS_CLONE}_spare"]["props"][SETUP_PROP] == setup_tag(SETUP)
    assert boot.windows.personalized == [f"/dev/zvol/{OS_CLONE}_spare"]
    # Current spares are kept, not made again.
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP) == []
    assert len(boot.windows.personalized) == 1
    # Spares never show up as writebacks.
    assert not [c for c in boot.writebacks() if c["zvol"].endswith("_spare")]


def test_a_reset_swaps_the_spares_in_without_cloning_or_setting_up(host, boot):
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    boot.windows.personalized.clear()
    host.calls.clear()
    cd = _reset(boot)
    assert not [c for c in host.calls if c[:2] == ["zfs", "clone"]]
    assert ["zfs", "rename", f"{OS_CLONE}_spare", OS_CLONE] in host.calls
    assert ["zfs", "rename", f"{CLONE}_spare", CLONE] in host.calls
    assert boot.windows.personalized == []                      # set up ahead of time
    assert cd.os_setup_problem is None
    assert f"{OS_CLONE}_spare" not in host.datasets
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{CLONE}"]
    # Without spares, the next reset clones and sets Windows up as before.
    host.calls.clear()
    _reset(boot)
    assert [c[-1] for c in host.calls if c[:2] == ["zfs", "clone"]] == [CLONE, OS_CLONE]
    assert boot.windows.personalized == [f"/dev/zvol/{OS_CLONE}"]


def test_a_stale_os_spare_is_not_used_and_is_renewed(host, boot):
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    renamed = OsSetup("pc01", {"GGNET_MACHINE_ID": "2"})          # e.g. the PC registered anew
    host.calls.clear()
    _reset(boot, setup=renamed)
    assert ["zfs", "rename", f"{CLONE}_spare", CLONE] in host.calls          # the game spare still fits
    assert [c[-1] for c in host.calls if c[:2] == ["zfs", "clone"]] == [OS_CLONE]
    assert f"{OS_CLONE}_spare" in host.datasets                             # left for the renewal
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", renamed) == [
        f"{CLONE}_spare", f"{OS_CLONE}_spare"]
    assert host.datasets[f"{OS_CLONE}_spare"]["props"][SETUP_PROP] == setup_tag(renamed)


def test_a_new_image_version_makes_the_spares_stale(host, boot):
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    host.snapshots[f"{OS}@v2"] = set()
    host.calls.clear()
    cd = _reset(boot, os_snap=f"{OS}@v2")
    assert ["zfs", "clone", "-o", "sync=disabled", f"{OS}@v2", OS_CLONE] in host.calls
    assert host.datasets[OS_CLONE]["origin"] == f"{OS}@v2" and cd.os_clone_snapshot == f"{OS}@v2"


def test_a_spare_that_cannot_be_set_up_is_removed_and_not_retried_at_once(host, boot, monkeypatch):
    from app.services import provisioning

    boot.windows.problem = "no Windows partition found"
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP) == [
        f"{CLONE}_spare"]
    assert f"{OS_CLONE}_spare" not in host.datasets
    assert boot.windows.labels[-1] == "pc01-spare"          # never the PC's own mount and .reg names
    host.calls.clear()
    boot.windows.problem = None
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP) == []
    assert host.calls == []                                 # backing off, not every round
    monkeypatch.setattr(provisioning, "SPARE_RETRY", 0.0)
    assert boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP) == [
        f"{OS_CLONE}_spare"]


def test_spares_go_with_the_machine(host, boot):
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    boot.drop_spares("pc01", CLONE, OS_CLONE)
    assert f"{CLONE}_spare" not in host.datasets and f"{OS_CLONE}_spare" not in host.datasets
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    boot.deprovision("pc01", IQN, CLONE, OS_CLONE)
    assert not [d for d in host.datasets if "pc01" in d]


def test_spares_never_keep_an_old_version_alive(host, boot):
    boot.make_spares("pc01", CLONE, f"{GAME}@base", OS_CLONE, f"{OS}@base", SETUP)
    host.snapshots[f"{OS}@v2"] = set()
    _reset(boot, os_snap=f"{OS}@v2")                       # the PC moved on; its old OS spare is left
    assert host.datasets[f"{OS_CLONE}_spare"]["origin"] == f"{OS}@base"
    base = next(v for v in boot.snapshots(OS) if v["name"] == "base")
    assert base["clones"] == []                            # a spare is not a PC using the version
    assert boot.merge_into_base(OS, "v2") == ["base"]      # and does not stop the merge
    assert f"{OS_CLONE}_spare" not in host.datasets
