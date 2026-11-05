"""Add new hardware: the toolchain VM and the API (app.services.toolchain)."""

from __future__ import annotations

import threading

import pytest

from app.api.deps import get_hardware_adder
from app.main import app
from app.services.toolchain import HardwareAdder, ToolchainVm
from app.windows_setup import ImagePrep

from .conftest import FakeRunner
from .test_boot_mode import FakeWindows, _disk

I219 = "VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10"
DEVICE = "/dev/zvol/tank/ggnet/writebacks/win11_ggnet_hwadd"


# ── The VM ────────────────────────────────────────────────────────────

def _vm_runner(statuses: list[str]) -> FakeRunner:
    r = FakeRunner()
    r.on("pvesh", "get", "/cluster/nextid", result=(True, "105\n", ""))
    left = list(statuses)
    r.on("qm", "status", result=lambda cmd: (True, f"status: {left.pop(0) if len(left) > 1 else left[0]}\n", ""))
    return r


def test_the_vm_boots_the_clone_and_is_removed_when_windows_shuts_down():
    runner = _vm_runner(["running", "running", "stopped"])
    vm = ToolchainVm(runner, sleep=lambda s: None)

    assert vm.run(DEVICE, "win11") is None

    create = next(c for c in runner.calls if c[:2] == ["qm", "create"])
    assert create[2] == "105"
    assert create[create.index("--sata0") + 1] == f"{DEVICE},cache=none"
    assert create[create.index("--bios") + 1] == "ovmf" and create[create.index("--cpu") + 1] == "host"
    assert "--net0" not in create                           # Windows needs no network for the install
    assert ["qm", "start", "105"] in runner.calls
    # The clone is detached before the VM goes, so removing the VM never touches it.
    tail = [c for c in runner.calls if c[:2] in (["qm", "set"], ["qm", "destroy"])]
    assert tail == [["qm", "set", "105", "--delete", "sata0"], ["qm", "destroy", "105", "--skiplock", "1"]]
    assert ["qm", "stop", "105", "--skiplock", "1"] not in runner.calls


def test_a_vm_that_does_not_finish_is_stopped_and_removed():
    runner = _vm_runner(["running"])
    vm = ToolchainVm(runner, timeout=20, poll=5, sleep=lambda s: None)
    assert "did not finish" in vm.run(DEVICE, "win11")
    assert ["qm", "stop", "105", "--skiplock", "1"] in runner.calls
    assert ["qm", "destroy", "105", "--skiplock", "1"] in runner.calls


def test_a_cancelled_run_stops_the_vm():
    runner = _vm_runner(["running"])
    cancel = threading.Event()
    cancel.set()
    assert ToolchainVm(runner, sleep=lambda s: None).run(DEVICE, "win11", cancel) == "cancelled"
    assert ["qm", "destroy", "105", "--skiplock", "1"] in runner.calls


def test_a_vm_that_cannot_be_created_leaves_nothing():
    runner = _vm_runner(["running"])
    runner.on("qm", "create", result=(False, "", "unable to create VM 105"))
    assert "unable to create" in ToolchainVm(runner, sleep=lambda s: None).run(DEVICE, "win11")
    assert not any(c[:2] == ["qm", "start"] for c in runner.calls)


# ── The API ───────────────────────────────────────────────────────────

class ToolchainWindows(FakeWindows):
    result: dict | str = {"ok": True, "devices": [{"instance_id": f"PCI\\{I219}\\3&1&0&FE", "ok": True,
                                                   "driver": "Intel Ethernet Connection (2) I219-V"}]}

    def start_toolchain(self, device, nics, prep, label="image"):
        self.started = (device, list(nics))
        return None

    def toolchain_result(self, device, label="image"):
        return self.result


class FakeVm:
    problem: str | None = None

    def run(self, device, label, cancel=None):
        self.booted = device
        return self.problem


@pytest.fixture
def adder(prov, host):
    prov.windows = ToolchainWindows(host)
    vm = FakeVm()
    a = HardwareAdder(prov, vm, lambda: ImagePrep(), background=False)
    app.dependency_overrides[get_hardware_adder] = lambda: a
    return a


def _os_disk(client) -> dict:
    return _disk(client, "win11", "os")


def test_add_hardware_makes_a_new_active_version(client, host, adder, prov):
    disk = _os_disk(client)
    r = client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware",
                    json={"cards": [{"nic": f"pci\\{I219.lower()}", "instance": "3&1&0&fe"}]})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["error"] is None and job["step"] == "done" and job["snapshot"] == "v2"
    assert job["cards"] == [{"nic": I219, "instance": "3&1&0&FE"}]
    assert prov.windows.started == (DEVICE, [(I219, "3&1&0&FE")])
    assert adder.vm.booted == DEVICE
    [(device, _, _)] = prov.windows.prepared                  # the new driver marked for a network boot
    assert device == DEVICE
    assert client.get(f"/api/v1/game-disks/{disk['id']}").json()["snapshot"] == "v2"
    versions = client.get(f"/api/v1/game-disks/{disk['id']}/snapshots").json()
    assert versions[-1]["comment"] == f"ggNet: added PCI\\{I219}"
    assert client.get(f"/api/v1/game-disks/{disk['id']}/add-hardware").json()["job"]["snapshot"] == "v2"


def test_a_failed_install_changes_nothing(client, host, adder, prov):
    disk = _os_disk(client)
    prov.windows.result = {"ok": False, "devices": [{"instance_id": "x", "ok": False, "error": "no driver"}]}
    r = client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware",
                    json={"cards": [{"nic": I219, "instance": "3&1&0&FE"}]})
    job = r.json()
    assert "no driver" in job["error"] and job["snapshot"] is None and not job["running"]
    assert client.get(f"/api/v1/game-disks/{disk['id']}").json()["snapshot"] == "base"
    assert not host.exists("tank/ggnet/writebacks/win11_ggnet_hwadd")


def test_a_vm_failure_is_reported(client, host, adder):
    disk = _os_disk(client)
    adder.vm.problem = "Windows did not finish within 30 minutes"
    job = client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware",
                      json={"cards": [{"nic": I219, "instance": "3&1&0&FE"}]}).json()
    assert job["error"] == "Windows did not finish within 30 minutes"
    assert not host.exists("tank/ggnet/writebacks/win11_ggnet_hwadd")


def test_add_hardware_refuses_bad_cards_and_game_disks(client, host, adder):
    disk = _os_disk(client)
    for card in ({"nic": "VEN_8086&DEV_15BC", "instance": "3&1&0&FE"}, {"nic": I219, "instance": "a\\b"}):
        assert client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware", json={"cards": [card]}).status_code == 422
    assert client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware", json={"cards": []}).status_code == 422
    game = _disk(client, "cs2")
    r = client.post(f"/api/v1/game-disks/{game['id']}/add-hardware",
                    json={"cards": [{"nic": I219, "instance": "3&1&0&FE"}]})
    assert r.status_code == 409
    assert client.get(f"/api/v1/game-disks/{disk['id']}/add-hardware").json() == {
        "wizard": None, "job": None, "waiting": []}
    assert client.post(f"/api/v1/game-disks/{disk['id']}/add-hardware/cancel").status_code == 409
