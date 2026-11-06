"""Attach and detach through one lio_batch.py run (ISCSIManager with batch on)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.iscsi_manager import ISCSIConfig, ISCSIManager
from app.services.provisioning import Provisioner
from app.zfs_manager import ZFSManager

from .conftest import make_layout

MASTER = "tank/ggnet/images/cs2"
SNAP = f"{MASTER}@base"
CLONE = "tank/ggnet/writebacks/pc01"
IQN = "iqn.1991-05.com.microsoft:pc01"
TARGET = "iqn.2025-05.net.ggnet:storage"
DEV = f"/dev/zvol/{CLONE}"


@pytest.fixture
def bprov(host) -> Provisioner:
    p = Provisioner(ZFSManager(layout=make_layout(), runner=host),
                    ISCSIManager(config=ISCSIConfig(portal="192.168.10.1:3260", batch=True), runner=host))
    p.create_disk("cs2", 10)
    p.publish_disk(MASTER)
    return p


def _batches(host) -> list[list[dict]]:
    return [json.loads(c[3]) for c in host.calls if len(c) == 4 and c[2].endswith("lio_batch.py")]


def _tcli(host) -> list[list[str]]:
    return [c for c in host.calls if c[0] == "targetcli"]


def test_a_reset_is_two_batches_and_no_mapping_command(host, bprov):
    bprov.provision("pc01", IQN, SNAP)
    assert host.visible(IQN) == [DEV]
    host.calls.clear()
    bprov.reset("pc01", IQN, CLONE, SNAP, target_iqn=TARGET)
    assert _tcli(host) == []                            # the shared target is known by now
    unmap, mapping = _batches(host)
    assert [s["op"] for s in unmap] == ["delete_acl", "delete_backstore", "delete_backstore", "list_backstores"]
    assert [s["op"] for s in mapping] == ["create_backstore", "create_lun", "create_acl", "map_lun", "save"]
    assert mapping[0] == {"op": "create_backstore", "name": "pc01-game", "dev": DEV}
    assert host.visible(IQN) == [DEV]


def test_boot_mode_disks_keep_their_lun_order(host, bprov):
    bprov.create_disk("win11", 10, is_os=True)
    bprov.publish_disk("tank/ggnet/images/win11")
    bprov.provision("pc01", IQN, SNAP, "tank/ggnet/images/win11@base")
    assert host.visible(IQN) == ["/dev/zvol/tank/ggnet/writebacks/pc01-os", DEV]


def test_a_failed_step_is_cleaned_up_and_reported(host, bprov):
    host.fail_on[("lio_batch", "create_acl")] = "Could not create NodeACL"
    from app.services.provisioning import ProvisioningError
    with pytest.raises(ProvisioningError, match="Could not create NodeACL"):
        bprov.provision("pc01", IQN, SNAP)
    assert not host.backstores and host.visible(IQN) == []
    assert CLONE not in host.datasets


def test_without_rtslib_it_falls_back_to_targetcli_for_good(host, bprov):
    host.lio_batch_missing = True
    bprov.provision("pc01", IQN, SNAP)
    assert host.visible(IQN) == [DEV]
    assert ["targetcli", "saveconfig"] in host.calls
    host.calls.clear()
    bprov.reset("pc01", IQN, CLONE, SNAP, target_iqn=TARGET)
    assert _batches(host) == []                          # not tried again
    assert host.visible(IQN) == [DEV]


def test_the_script_reports_a_missing_rtslib_or_bad_input(tmp_path):
    """The real lio_batch.py, without LIO: bad JSON, or rtslib-fb not importable."""
    from app import iscsi_manager

    script = str(iscsi_manager.BATCH_SCRIPT)
    r = subprocess.run([sys.executable, "-I", script, "not json"], capture_output=True, text=True)
    assert r.returncode == 2 and json.loads(r.stdout)["ok"] is False
    r = subprocess.run([sys.executable, "-I", script, "[]"], capture_output=True, text=True)
    answer = json.loads(r.stdout)
    if r.returncode == 3:                                # no rtslib-fb here (CI)
        assert answer["unavailable"] is True
    else:                                                # a host with rtslib-fb: nothing to do
        assert answer == {"ok": True, "results": []}
