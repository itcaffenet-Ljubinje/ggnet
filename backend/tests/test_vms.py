"""Virtual machines for working on images (app.services.vms, /api/v1/vms)."""

from __future__ import annotations

import pytest

from app.services.vms import VmDisks, default_bridge, default_storage, vm_iqn, vmid_of

from .test_boot_mode import _disk

WB = "tank/ggnet/writebacks"


@pytest.fixture
def disks(client) -> tuple[dict, dict]:
    return _disk(client, "win11", "os"), _disk(client, "cs2")


def _vm(client, disks, **body) -> dict:
    os_image, game = disks
    r = client.post("/api/v1/vms", json={"name": "work-vm", "os_image_id": os_image["id"],
                                         "game_image_id": game["id"], **body})
    assert r.status_code == 201, r.text
    return r.json()


def _action(client, m, action):
    return client.post(f"/api/v1/vms/{m['id']}/action", json={"action": action})


# ── Pieces ────────────────────────────────────────────────────────────

def test_vm_iqn_round_trip():
    assert vmid_of(vm_iqn("iqn.2025-05.net.ggnet", 104)) == 104
    assert vmid_of("iqn.2025-05.net.ggnet:pc-aabbccddeeff") is None
    assert vmid_of("iqn.1991-05.com.microsoft:vip-05") is None
    assert vmid_of(None) is None


def test_defaults_follow_the_host_not_fixed_names():
    bridges = [{"name": "vmbr0"}, {"name": "vmbr1"}]
    assert default_bridge(bridges, "vmbr1") == "vmbr1"          # ggNet's own interface
    assert default_bridge(bridges, "eno1") == "vmbr0"           # not a bridge: the first one
    assert default_bridge([{"name": "br-lan"}], None) == "br-lan"   # a host with one bridge
    assert default_bridge([], "vmbr0") is None
    storages = [{"name": "local-zfs", "type": "zfspool", "pool": "rpool/data"},
                {"name": "fast", "type": "zfspool", "pool": "STORAGE-01/vm"},
                {"name": "local-lvm", "type": "lvmthin", "pool": None}]
    assert default_storage(storages, "STORAGE-01") == "fast"
    assert default_storage(storages, "tank") == "local-zfs"
    assert default_storage([], "tank") is None


def test_vm_disks_running_means_connected(host):
    host.add_vm(105, status="running")
    host.add_vm(106)
    d = VmDisks(host)
    assert d.session_active(vm_iqn("iqn.x", 105)) is True
    assert d.session_active(vm_iqn("iqn.x", 106)) is False
    assert d.session_active(vm_iqn("iqn.x", 107)) is False    # removed outside ggNet: not running
    host.fail_on[("qm", "status", "106")] = "ipcc_send_rec failed: Connection refused"
    assert d.session_active(vm_iqn("iqn.x", 106)) is None     # unknown is never "off"


# ── Host and settings ─────────────────────────────────────────────────

def test_host_lists_bridges_and_storages(client, host):
    r = client.get("/api/v1/vms/host")
    assert r.status_code == 200, r.text
    h = r.json()
    assert h["available"] and h["node"] == "pve1" and h["cpus"] == 32
    assert [b["name"] for b in h["bridges"]] == ["vmbr0", "vmbr1"]
    assert h["bridge"] == "vmbr0"                # dev config's interface is lo: the first bridge
    assert h["storage"] == "tank-vm"             # the storage on ggNet's pool


def test_settings_only_take_what_the_host_has(client, host):
    assert client.put("/api/v1/vms/settings", json={"bridge": "vmbr9"}).status_code == 409
    assert client.put("/api/v1/vms/settings", json={"storage": "nope"}).status_code == 409
    r = client.put("/api/v1/vms/settings", json={"bridge": "vmbr1", "storage": "local-zfs",
                                                 "reserved_memory_gb": 32})
    assert r.status_code == 200, r.text
    h = client.get("/api/v1/vms/host").json()
    assert (h["bridge"], h["storage"]) == ("vmbr1", "local-zfs")
    assert h["settings"]["reserved_memory_gb"] == 32


def test_not_a_proxmox_host(client, host):
    host.fail_on[("pvesh", "get", "/nodes/pve1/status")] = "pvesh: command not found"
    assert client.get("/api/v1/vms/host").json()["available"] is False


# ── VMs ───────────────────────────────────────────────────────────────

def test_create_vm_gives_it_both_writebacks_as_drives(client, host, disks):
    m = _vm(client, disks, cores=6, memory_mb=16384)
    assert m["vm_id"] == 100 and m["mode"] == "boot" and m["status"] == "provisioned"
    assert m["initiator_iqn"].endswith(":vm-100") and m["keep_writeback"] is True
    assert m["mac"] == "bc:24:11:00:00:64"                      # read back from the VM
    cfg = host.vms[100]["config"]
    assert cfg["sata0"].startswith(f"/dev/zvol/{WB}/work-vm-os,")
    assert cfg["sata1"].startswith(f"/dev/zvol/{WB}/work-vm,")
    assert cfg["boot"] == "order=sata0"
    assert cfg["bios"] == "ovmf" and cfg["machine"] == "q35"
    assert "pre-enrolled-keys=1" in cfg["efidisk0"] and cfg["efidisk0"].startswith("tank-vm:")
    assert cfg["tpmstate0"] == "tank-vm:1,version=v2.0"
    assert cfg["net0"].endswith(",bridge=vmbr0") and cfg["net0"].startswith("e1000e=")
    assert (cfg["cores"], cfg["memory"]) == ("6", "16384")
    # Nothing of it is on the iSCSI target.
    assert not any(c[:1] == ["targetcli"] for c in host.calls)
    assert host.vms[100]["status"] == "stopped"


def test_vm_list_shows_live_state(client, host, disks):
    m = _vm(client, disks)
    [vm] = client.get("/api/v1/vms").json()
    assert (vm["machine_id"], vm["vm_id"], vm["status"], vm["cores"]) == (m["id"], 100, "stopped", 4)
    assert vm["node"] == "pve1"


def test_create_checks_names_resources_and_one_kept_writeback(client, host, disks):
    _vm(client, disks)
    os_image, game = disks
    body = {"os_image_id": os_image["id"], "game_image_id": game["id"]}
    assert client.post("/api/v1/vms", json={"name": "work-vm", **body}).status_code == 409
    r = client.post("/api/v1/vms", json={"name": "vm2", **body})
    assert r.status_code == 409 and "already keeps its writeback" in r.json()["detail"]["error"]
    r = client.post("/api/v1/vms", json={"name": "vm2", "cores": 64, "keep_writeback": False, **body})
    assert r.status_code == 409 and "32 CPU threads" in r.json()["detail"]["error"]
    r = client.post("/api/v1/vms", json={"name": "vm2", "memory_mb": 190 * 1024, "keep_writeback": False,
                                         **body})
    assert r.status_code == 409 and "kept for the host" in r.json()["detail"]["error"]
    assert list(host.vms) == [100]                 # refused before anything was made


def test_create_needs_published_images_of_the_right_kind(client, host, disks):
    os_image, game = disks
    r = client.post("/api/v1/vms", json={"name": "v", "os_image_id": game["id"], "game_image_id": game["id"]})
    assert r.status_code == 409
    assert host.vms == {}


def test_actions_follow_the_vm_state(client, host, disks):
    m = _vm(client, disks)
    assert _action(client, m, "shutdown").status_code == 409          # it is off
    assert _action(client, m, "start").status_code == 200
    assert host.vms[100]["status"] == "running"
    assert _action(client, m, "start").status_code == 409
    assert _action(client, m, "stop").status_code == 200
    assert host.vms[100]["status"] == "stopped"


def test_shutdown_and_restart_are_asked_of_windows_with_a_timeout(client, host, disks):
    m = _vm(client, disks)
    _action(client, m, "start")
    assert _action(client, m, "reboot").status_code == 200
    assert ["qm", "reboot", "100", "--timeout", "600"] in host.calls
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "shutdown"})
    assert r.status_code == 200, r.text
    assert ["qm", "shutdown", "100", "--timeout", "600"] in host.calls
    assert host.vms[100]["status"] == "stopped"


def test_shutdown_goes_through_the_agent_while_it_reports(client, host, disks):
    m = _vm(client, disks)
    _action(client, m, "start")
    beat = client.post("/api/v1/agent/heartbeat", json={"name": "work-vm", "agent_version": "0.1.21"})
    assert beat.status_code == 200, beat.text
    host.calls.clear()
    r = _action(client, m, "shutdown")
    assert r.status_code == 200, r.text
    assert r.json()["pending_command"] == "shutdown"
    assert not any(c[:2] == ["qm", "shutdown"] for c in host.calls)   # not the power button
    nxt = client.post("/api/v1/agent/heartbeat", json={"name": "work-vm", "agent_version": "0.1.21"})
    assert nxt.json()["command"] == "shutdown"                          # shutdown.exe in the VM


def test_list_shows_the_live_state_not_the_cluster_cache(client, host, disks, monkeypatch):
    m = _vm(client, disks)
    _action(client, m, "start")
    real = host._pvesh

    def stale(args):                              # /cluster/resources still says stopped
        ok, out, err = real(args)
        return (ok, out.replace('"running"', '"stopped"'), err) if args[1] == "/cluster/resources" else (ok, out, err)

    monkeypatch.setattr(host, "_pvesh", stale)
    assert client.get("/api/v1/vms").json()[0]["status"] == "running"


def test_power_on_a_vm_starts_it_instead_of_wake_on_lan(client, host, disks):
    m = _vm(client, disks)
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"})
    assert r.status_code == 200, r.text
    assert host.vms[100]["status"] == "running"


def test_cores_and_memory_change_only_while_off(client, host, disks):
    m = _vm(client, disks)
    _action(client, m, "start")
    assert client.patch(f"/api/v1/vms/{m['id']}", json={"cores": 8}).status_code == 409
    _action(client, m, "stop")
    assert client.patch(f"/api/v1/vms/{m['id']}", json={"cores": 8, "memory_mb": 4096}).status_code == 200
    assert (host.vms[100]["config"]["cores"], host.vms[100]["config"]["memory"]) == ("8", "4096")


def test_running_vm_is_connected_for_the_writeback_rules(client, host, disks):
    m = _vm(client, disks)
    _action(client, m, "start")
    r = client.post(f"/api/v1/machines/{m['id']}/discard-writeback")
    assert r.status_code == 409 and "still connected" in r.json()["detail"]["error"]
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 409
    assert 100 in host.vms


def test_discard_re_clones_and_attaches_the_drives_again(client, host, disks):
    m = _vm(client, disks)
    host.vms[100]["config"].pop("sata1")
    r = client.post(f"/api/v1/machines/{m['id']}/discard-writeback")
    assert r.status_code == 200, r.text
    assert host.vms[100]["config"]["sata1"].startswith(f"/dev/zvol/{WB}/work-vm,")


def test_apply_writebacks_from_a_vm(client, host, disks):
    os_image, game = disks
    m = _vm(client, disks)
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": True, "game": True})
    assert r.status_code == 200, r.text
    images = {d["id"]: d for d in client.get("/api/v1/images").json()}
    assert images[os_image["id"]]["snapshot"] == "v2" and images[game["id"]]["snapshot"] == "v2"
    assert host.vms[100]["config"]["sata0"].startswith(f"/dev/zvol/{WB}/work-vm-os,")


def test_rename_renames_the_writebacks_and_the_vm(client, host, disks):
    m = _vm(client, disks)
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"name": "img-vm"})
    assert r.status_code == 200, r.text
    cfg = host.vms[100]["config"]
    assert cfg["name"] == "ggnet-img-vm"
    assert cfg["sata0"].startswith(f"/dev/zvol/{WB}/img-vm-os,")


def test_what_ties_a_vm_to_its_machine_cannot_change(client, host, disks):
    m = _vm(client, disks)
    for change in ({"mode": "disk"}, {"mac": "aa:bb:cc:dd:ee:ff"}, {"initiator_iqn": "iqn.2000-01.x:y"}):
        r = client.patch(f"/api/v1/machines/{m['id']}", json=change)
        assert r.status_code == 409, change


def test_a_pc_cannot_take_a_vm_iqn(client):
    r = client.post("/api/v1/machines", json={"name": "pc1", "initiator_iqn": "iqn.2025-05.net.ggnet:vm-100"})
    assert r.status_code == 409 and "virtual machines" in r.json()["detail"]["error"]


def test_delete_removes_writebacks_and_the_vm(client, host, disks):
    m = _vm(client, disks)
    assert client.delete(f"/api/v1/vms/{m['id']}").status_code == 204
    assert host.vms == {}
    assert f"{WB}/work-vm" not in host.datasets and f"{WB}/work-vm-os" not in host.datasets
    # The drives were detached before the VM went, so Proxmox never touched the writebacks.
    sets = [c for c in host.calls if c[:3] == ["qm", "set", "100"] and "--delete" in c]
    destroy = next(i for i, c in enumerate(host.calls) if c[:2] == ["qm", "destroy"])
    assert sets and host.calls.index(sets[-1]) < destroy


def test_delete_of_a_vm_already_gone_in_proxmox(client, host, disks):
    m = _vm(client, disks)
    del host.vms[100]
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 204


def test_traffic_skips_vms(client, host, disks):
    _vm(client, disks)
    assert client.get("/api/v1/machines/traffic").json() == []


def test_vm_routes_refuse_a_pc(client):
    r = client.post("/api/v1/machines", json={"name": "pc1"})
    assert client.post(f"/api/v1/vms/{r.json()['id']}/action", json={"action": "start"}).status_code == 409


# ── Console ───────────────────────────────────────────────────────────

def test_console_tickets_are_one_time_and_short_lived():
    from app.services.vms import ConsoleTickets

    now = [1000.0]
    t = ConsoleTickets(clock=lambda: now[0])
    key, password = t.issue(104)
    assert len(password) == 8                    # VNC passwords are 8 characters
    assert t.take(key).vmid == 104
    assert t.take(key) is None                   # used
    key, _ = t.issue(104)
    now[0] += 31
    assert t.take(key) is None                   # expired
    assert t.take("made-up") is None


def test_console_needs_a_running_vm(client, host, disks):
    m = _vm(client, disks)
    assert client.post(f"/api/v1/vms/{m['id']}/console").status_code == 409


def test_console_websocket_pipes_the_vnc_proxy(client, host, disks, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from app.services import vms

    seen = {}

    def fake_proxy(vmid):
        seen["vmid"] = vmid
        # Stands in for `qm vncproxy`: says hello, shows the password it got, echoes the rest.
        return ["sh", "-c", 'printf "RFB $LC_PVE_TICKET\\n"; exec cat']

    monkeypatch.setattr(vms, "console_command", fake_proxy)
    m = _vm(client, disks)
    _action(client, m, "start")
    r = client.post(f"/api/v1/vms/{m['id']}/console")
    assert r.status_code == 200, r.text
    c = r.json()
    with client.websocket_connect(c["path"], subprotocols=["binary"]) as ws:
        assert ws.receive_bytes() == f"RFB {c['password']}\n".encode()
        ws.send_bytes(b"key-press")
        assert ws.receive_bytes() == b"key-press"
    assert seen["vmid"] == 100
    # The ticket was used up.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(c["path"]) as ws:
            ws.receive_bytes()


# ── The agent in the VM ───────────────────────────────────────────────

def test_the_agent_in_a_vm_connects_nothing_and_hides_no_disk(client, host, disks):
    m = _vm(client, disks)
    r = client.post("/api/v1/agent/heartbeat", json={
        "name": "work-vm", "agent_version": "0.1.21",
        "initiator_iqn": "iqn.2025-05.net.ggnet:pc-10ffe066bdec"})   # the image's PC
    assert r.status_code == 200, r.text
    c = r.json()
    # Agents hide local disks only in "boot" and connect only a target they are sent:
    # the game writeback is the VM's SATA disk, so it must stay online.
    assert c["machine_id"] == m["id"] and c["mode"] == "vm"
    assert c["iscsi_target_iqn"] is None and c["secure_boot"] is None and c["update"] is None


# ── Writebacks follow the VM's power state (production watcher rules) ─

@pytest.fixture
def make_session(db_url):
    from sqlalchemy.orm import sessionmaker

    from app.db.session import make_engine

    engine = make_engine(db_url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _watch(make_session, prov, at):
    from app.services import writebacks
    from app.services.writebacks import WritebackSettings

    writebacks.tick(make_session, prov, WritebackSettings(auto_discard=True), now=at)


def _clones(host, name):
    return sum(1 for c in host.calls if c[:2] == ["zfs", "clone"] and c[-1].endswith(f"/{name}"))


@pytest.mark.parametrize("keep", [False, True])
def test_a_vm_writeback_is_discarded_after_shutdown_unless_kept(client, host, prov, disks, make_session, keep):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    m = _vm(client, disks, keep_writeback=keep)
    _action(client, m, "start")
    _watch(make_session, prov, t0)                                  # running: written to
    assert client.get(f"/api/v1/machines/{m['id']}").json()["writeback_dirty"] is True
    _action(client, m, "shutdown")
    host.calls.clear()
    _watch(make_session, prov, t0 + timedelta(seconds=5))
    _watch(make_session, prov, t0 + timedelta(seconds=60))          # off, after the grace time
    made = (_clones(host, "work-vm"), _clones(host, "work-vm-os"))
    assert made == ((0, 0) if keep else (1, 1))
    # Discarded or kept, the VM has its drives again.
    assert host.vms[100]["config"]["sata0"].startswith(f"/dev/zvol/{WB}/work-vm-os,")
    assert host.vms[100]["config"]["sata1"].startswith(f"/dev/zvol/{WB}/work-vm,")


# ── Network (PXE) boot VMs ────────────────────────────────────────────

@pytest.fixture
def pxe_bridge(monkeypatch):
    """ggNet's own interface is a bridge the host has, so network boot is allowed."""
    monkeypatch.setattr("app.api.v1.vms._interface", lambda: "vmbr1")


def test_network_vm_boots_over_iscsi_like_a_diskless_pc(client, host, disks, pxe_bridge):
    m = _vm(client, disks, drives="network")
    # A diskless PC: its IQN comes from the MAC, so iPXE and the ACL match.
    assert m["vm_id"] == 100 and m["mode"] == "boot"
    digits = m["mac"].replace(":", "")
    assert m["initiator_iqn"].endswith(f":pc-{digits}")
    cfg = host.vms[100]["config"]
    assert cfg["boot"] == "order=net0"
    assert "sata0" not in cfg and "sata1" not in cfg       # no local drives
    # The writebacks are on the shared iSCSI target (OS = LUN 0, game = LUN 1).
    assert host.visible(m["initiator_iqn"]) == [
        f"/dev/zvol/{WB}/work-vm-os", f"/dev/zvol/{WB}/work-vm"]
    [vm] = client.get("/api/v1/vms").json()
    assert vm["drives"] == "network"


def test_network_boot_needs_ggnet_on_a_bridge(client, host, disks, monkeypatch):
    monkeypatch.setattr("app.api.v1.vms._interface", lambda: "lo")   # not a bridge
    os_image, game = disks
    r = client.post("/api/v1/vms", json={"name": "netvm", "os_image_id": os_image["id"],
                                         "game_image_id": game["id"], "drives": "network"})
    assert r.status_code == 409 and "Network boot needs" in r.json()["detail"]["error"]
    assert host.vms == {}


def test_local_vm_stays_on_local_drives(client, host, disks):
    m = _vm(client, disks, drives="local")
    assert m["initiator_iqn"].endswith(":vm-100")
    assert host.vms[100]["config"]["sata0"].startswith(f"/dev/zvol/{WB}/work-vm-os,")
    assert host.visible(m["initiator_iqn"]) == []           # nothing on the iSCSI target
    assert client.get("/api/v1/vms").json()[0]["drives"] == "local"


def test_network_vm_pxe_script_matches_its_acl(client, host, disks, pxe_bridge):
    m = _vm(client, disks, drives="network")
    r = client.get(f"/api/v1/boot/ipxe?mac={m['mac'].replace(':', '-')}")
    assert r.status_code == 200, r.text
    assert f"set initiator-iqn {m['initiator_iqn']}" in r.text
    assert ":storage" in r.text                             # the shared target


def test_deleting_a_network_vm_removes_the_target_and_the_vm(client, host, disks, pxe_bridge):
    m = _vm(client, disks, drives="network")
    assert host.visible(m["initiator_iqn"])
    assert client.delete(f"/api/v1/vms/{m['id']}").status_code == 204
    assert host.vms == {}
    assert host.visible(m["initiator_iqn"]) == []
    assert f"{WB}/work-vm" not in host.datasets and f"{WB}/work-vm-os" not in host.datasets


# ── BitLocker (Device encryption) ─────────────────────────────────────

@pytest.mark.parametrize("which", ["work-vm-os", "work-vm"])
def test_apply_refuses_a_bitlocker_writeback(client, host, disks, which):
    m = _vm(client, disks)
    host.bitlocker.add(f"{WB}/{which}")
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": True, "game": True})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert "BitLocker" in d["error"] and "manage-bde -off" in d["error"]
    assert d["bitlocker"] == [f"/dev/zvol/{WB}/{which}-part1"]
    images = client.get("/api/v1/images").json()
    assert {i["snapshot"] for i in images} == {"base"}                 # nothing applied


def test_every_os_writeback_forbids_device_encryption():
    from app.windows_setup import OsSetup, image_patch, registry_patch

    for text in (registry_patch(OsSetup("vip-05", {}), 1), image_patch(1)):
        assert '[\\ControlSet001\\Control\\BitLocker]\n"PreventDeviceEncryption"=dword:00000001' in text
