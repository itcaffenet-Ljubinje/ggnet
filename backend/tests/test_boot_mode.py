"""Boot Mode: OS images, OS + game writebacks per PC, and the iPXE script (FakeHost)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_writeback_settings
from app.db.session import make_engine
from app.main import app
from app.services import writebacks
from app.services.writebacks import WritebackSettings

SETTINGS = WritebackSettings(auto_discard=True, grace=timedelta(seconds=30))
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
IQN = "iqn.2025-05.net.ggnet:pc-aabbccddee05"   # derived from MAC
MAC = "aa:bb:cc:dd:ee:05"
OS_CLONE = "tank/ggnet/writebacks/vip-05-os"
GAME_CLONE = "tank/ggnet/writebacks/vip-05"
TARGET = "iqn.2025-05.net.ggnet:storage"


def _disk(client, name: str, kind: str = "game") -> dict:
    r = client.post("/api/v1/game-disks", json={"name": name, "size_gb": 10, "kind": kind})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/game-disks/{r.json()['id']}/publish")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def disks(client) -> tuple[dict, dict]:
    return _disk(client, "win11", "os"), _disk(client, "cs2")


def _boot_machine(client, disks, **body) -> dict:
    os_disk, game = disks
    r = client.post("/api/v1/machines", json={
        "name": "vip-05", "mode": "boot", "mac": MAC,
        "game_disk_id": game["id"], "os_disk_id": os_disk["id"], **body,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _get(client, m) -> dict:
    return client.get(f"/api/v1/machines/{m['id']}").json()


def _ipxe(client, mac: str = "aa-bb-cc-dd-ee-05") -> str:
    r = client.get("/api/v1/boot/ipxe", params={"mac": mac})
    assert r.status_code == 200, r.text
    assert r.text.startswith("#!ipxe\n")
    return r.text


def _mark_dirty(db_url) -> None:
    """The PC wrote to its writebacks (the watcher saw it connected)."""
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE machines SET writeback_dirty = 1"))
    engine.dispose()


def _clones_made(host) -> list[str]:
    """Clones made for PCs (not the image's temporary hardware-check clone)."""
    return [c[-1] for c in host.calls if c[:2] == ["zfs", "clone"] and not c[-1].endswith("_ggnet_hw")]


@pytest.fixture
def auto(client):
    app.dependency_overrides[get_writeback_settings] = lambda: SETTINGS


# ── OS images ─────────────────────────────────────────────────────────

def test_os_image_is_created_for_windows_io(client, host):
    r = client.post("/api/v1/game-disks", json={"name": "win11", "size_gb": 80, "kind": "os"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "os"
    create = next(c for c in host.calls if c[:2] == ["zfs", "create"])
    assert {"volblocksize=16k", "compression=lz4", "sync=standard"} <= set(create)


def test_game_disk_is_the_default_kind(client, host):
    assert _disk(client, "cs2")["kind"] == "game"
    create = next(c for c in host.calls if c[:2] == ["zfs", "create"])
    assert "volblocksize=64k" in create


def test_os_image_cannot_be_edited_on_a_pc(client):
    r = client.post("/api/v1/game-disks", json={"name": "win11", "size_gb": 80, "kind": "os"})
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    r = client.post(f"/api/v1/game-disks/{r.json()['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 409


def test_os_image_in_use_cannot_be_deleted(client, disks):
    _boot_machine(client, disks)
    r = client.delete(f"/api/v1/game-disks/{disks[0]['id']}")
    assert r.status_code == 409


# ── Provisioning ──────────────────────────────────────────────────────

def test_boot_machine_gets_os_lun0_and_game_lun1(client, host, disks):
    m = _boot_machine(client, disks)
    assert m["status"] == "provisioned"
    assert (m["os_disk_id"], m["os_clone_zvol"], m["os_clone_snapshot"]) == (
        disks[0]["id"], OS_CLONE, "tank/ggnet/images/win11@base")
    assert (m["clone_zvol"], m["clone_snapshot"]) == (GAME_CLONE, "tank/ggnet/images/cs2@base")
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]


def test_boot_machine_needs_an_os_image(client, host, disks):
    r = client.post("/api/v1/machines", json={
        "name": "vip-05", "mode": "boot", "game_disk_id": disks[1]["id"]})
    assert r.status_code == 409
    assert "OS image" in r.json()["detail"]["error"]


def test_disks_must_be_of_the_right_kind(client, disks):
    os_disk, game = disks
    r = client.post("/api/v1/machines", json={
        "name": "vip-05", "mode": "boot", "game_disk_id": os_disk["id"], "os_disk_id": game["id"]})
    assert r.status_code == 409


def test_disk_mode_machine_gets_no_os_image(client, disks):
    os_disk, game = disks
    r = client.post("/api/v1/machines", json={
        "name": "pc01", "game_disk_id": game["id"], "os_disk_id": os_disk["id"]})
    assert r.status_code == 409


def test_machine_name_must_not_end_in_os(client):
    r = client.post("/api/v1/machines", json={"name": "vip-05-os"})
    assert r.status_code == 422


def test_assign_keeps_the_os_image_when_not_sent(client, host, disks):
    m = _boot_machine(client, disks)
    other = _disk(client, "valorant")
    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": other["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["os_disk_id"] == disks[0]["id"]
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]
    assert host.datasets[GAME_CLONE]["origin"] == "tank/ggnet/images/valorant@base"


def test_remove_disks_deletes_both_writebacks(client, host, disks):
    m = _boot_machine(client, disks)
    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": None})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["os_disk_id"], out["os_clone_zvol"]) == ("idle", None, None)
    assert not host.exists(OS_CLONE) and not host.exists(GAME_CLONE)
    assert host.visible(IQN) == []


def test_failed_os_clone_rolls_back_the_game_clone(client, host, disks):
    host.fail_on[("zfs", "clone", "-o", "sync=disabled", "tank/ggnet/images/win11@base")] = "out of space"
    r = client.post("/api/v1/machines", json={
        "name": "vip-05", "mode": "boot",
        "game_disk_id": disks[1]["id"], "os_disk_id": disks[0]["id"]})
    assert r.status_code == 502
    assert not host.exists(GAME_CLONE)
    m = client.get("/api/v1/machines").json()[0]
    assert m["status"] == "error"
    # The leftovers are recorded, so removing the disks cleans up.
    del host.fail_on[("zfs", "clone", "-o", "sync=disabled", "tank/ggnet/images/win11@base")]
    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": None})
    assert r.status_code == 200, r.text


def test_keep_writeback_keeps_both(client, host, disks):
    m = _boot_machine(client, disks)
    r = client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    assert r.status_code == 200, r.text
    synced = {c[-1] for c in host.calls if c[:3] == ["zfs", "set", "sync=standard"]}
    assert synced == {OS_CLONE, GAME_CLONE}


def test_rename_a_boot_pc_moves_both_writebacks(client, host, disks):
    m = _boot_machine(client, disks)
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"name": "vip-15"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["clone_zvol"], out["os_clone_zvol"]) == (
        "tank/ggnet/writebacks/vip-15", "tank/ggnet/writebacks/vip-15-os")
    assert not host.exists(OS_CLONE) and not host.exists(GAME_CLONE)
    assert host.visible(IQN) == ["/dev/zvol/tank/ggnet/writebacks/vip-15-os",
                                 "/dev/zvol/tank/ggnet/writebacks/vip-15"]
    assert "booting vip-15" in _ipxe(client)


# ── Apply Writebacks ──────────────────────────────────────────────────

def _boot_keeper(client, disks) -> dict:
    m = _boot_machine(client, disks)
    r = client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    assert r.status_code == 200, r.text
    return m


def test_apply_writebacks_makes_both_next_versions(client, host, disks, windows):
    m = _boot_keeper(client, disks)
    windows.done.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["os_clone_snapshot"] == "tank/ggnet/images/win11@v2"
    assert out["clone_snapshot"] == "tank/ggnet/images/cs2@v2"
    assert client.get(f"/api/v1/game-disks/{disks[0]['id']}").json()["snapshot"] == "v2"
    assert client.get(f"/api/v1/game-disks/{disks[1]['id']}").json()["snapshot"] == "v2"
    assert host.datasets[OS_CLONE]["origin"] == "tank/ggnet/images/win11@v2"
    assert host.datasets[GAME_CLONE]["origin"] == "tank/ggnet/images/cs2@v2"
    # Both writebacks are still kept, and the fresh OS one is this PC again.
    assert {host.datasets[c]["props"]["sync"] for c in (OS_CLONE, GAME_CLONE)} == {"standard"}
    assert [d for d, _, _ in windows.done] == [f"/dev/zvol/{OS_CLONE}"]
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]


def test_apply_only_the_os_writeback_keeps_the_game_one(client, host, disks, windows):
    m = _boot_keeper(client, disks)
    windows.done.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"game": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["os_clone_snapshot"] == "tank/ggnet/images/win11@v2"
    assert out["clone_snapshot"] == "tank/ggnet/images/cs2@base"
    assert client.get(f"/api/v1/game-disks/{disks[1]['id']}").json()["snapshot"] == "base"
    # The game writeback was mapped again as it was, not re-cloned.
    assert ["zfs", "destroy", "-r", GAME_CLONE] not in host.calls
    assert host.datasets[GAME_CLONE]["origin"] == "tank/ggnet/images/cs2@base"
    assert [d for d, _, _ in windows.done] == [f"/dev/zvol/{OS_CLONE}"]
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]


def test_apply_only_the_game_writeback_keeps_the_os_one(client, host, disks, windows):
    m = _boot_keeper(client, disks)
    windows.done.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": False})
    assert r.status_code == 200, r.text
    assert r.json()["os_clone_snapshot"] == "tank/ggnet/images/win11@base"
    assert r.json()["clone_snapshot"] == "tank/ggnet/images/cs2@v2"
    assert ["zfs", "destroy", "-r", OS_CLONE] not in host.calls
    assert windows.done == []          # the kept OS writeback is already this PC


def test_apply_nothing_is_refused(client, host, disks):
    m = _boot_keeper(client, disks)
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": False, "game": False})
    assert r.status_code == 409
    assert "tank/ggnet/images/win11@v2" not in host.snapshots


def test_apply_os_refused_when_the_image_moved_on(client, host, disks):
    m = _boot_keeper(client, disks)
    host.snapshots["tank/ggnet/images/win11@manual"] = set()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"game": False})
    assert r.status_code == 502 and "not the newest version" in r.json()["detail"]["error"]
    # Nothing was applied, so the PC gets its writebacks back.
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]


def test_apply_failing_on_the_second_disk_records_the_first(client, host, disks):
    m = _boot_keeper(client, disks)
    host.snapshots["tank/ggnet/images/win11@manual"] = set()   # the OS apply fails
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 502 and "Reset the machine" in r.json()["detail"]["error"]
    assert client.get(f"/api/v1/game-disks/{disks[1]['id']}").json()["snapshot"] == "v2"
    assert _get(client, m)["status"] == "error"


# ── Writeback discard ─────────────────────────────────────────────────

def test_watcher_discards_both_writebacks(client, host, prov, disks, db_url):
    m = _boot_machine(client, disks)
    engine = make_engine(db_url)
    make_session = sessionmaker(bind=engine, expire_on_commit=False)
    host.sessions.add(IQN)
    writebacks.tick(make_session, prov, SETTINGS, now=T0)
    host.sessions.discard(IQN)
    writebacks.tick(make_session, prov, SETTINGS, now=T0 + timedelta(seconds=1))
    host.calls.clear()
    writebacks.tick(make_session, prov, SETTINGS, now=T0 + timedelta(seconds=40))
    engine.dispose()
    assert sorted(_clones_made(host)) == [GAME_CLONE, OS_CLONE]
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]
    assert _get(client, m)["writeback_dirty"] is False


def _set_ipxe_at(db_url, when: datetime) -> None:
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE machines SET ipxe_at = :t"), {"t": when})
    engine.dispose()


def _boot_then_handover(make_session, prov, host, start: datetime) -> None:
    """iPXE's session comes and goes; Windows has not logged in yet."""
    host.sessions.add(IQN)
    writebacks.tick(make_session, prov, SETTINGS, now=start + timedelta(seconds=1))
    host.sessions.discard(IQN)
    writebacks.tick(make_session, prov, SETTINGS, now=start + timedelta(seconds=10))


def test_watcher_leaves_a_booting_pc_alone(client, host, prov, disks, db_url):
    """The gap between iPXE's session and Windows' own login is not a shutdown."""
    _boot_machine(client, disks)
    _set_ipxe_at(db_url, T0)
    engine = make_engine(db_url)
    make_session = sessionmaker(bind=engine, expire_on_commit=False)
    _boot_then_handover(make_session, prov, host, T0)
    host.calls.clear()
    writebacks.tick(make_session, prov, SETTINGS, now=T0 + timedelta(seconds=60))
    engine.dispose()
    assert _clones_made(host) == []
    assert ["targetcli", "/iscsi/" + TARGET + "/tpg1/acls", "delete", IQN] not in host.calls
    assert host.visible(IQN) == [f"/dev/zvol/{OS_CLONE}", f"/dev/zvol/{GAME_CLONE}"]


def test_watcher_discards_a_boot_pc_after_the_boot_window(client, host, prov, disks, db_url):
    _boot_machine(client, disks)
    _set_ipxe_at(db_url, T0)
    engine = make_engine(db_url)
    make_session = sessionmaker(bind=engine, expire_on_commit=False)
    _boot_then_handover(make_session, prov, host, T0 + timedelta(minutes=10))
    host.calls.clear()
    writebacks.tick(make_session, prov, SETTINGS, now=T0 + timedelta(minutes=11))
    engine.dispose()
    assert sorted(_clones_made(host)) == [GAME_CLONE, OS_CLONE]


def test_ipxe_request_starts_the_boot_window(client, host, disks, db_url):
    _boot_machine(client, disks)
    _ipxe(client)
    engine = make_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT ipxe_at FROM machines")).scalar() is not None
    engine.dispose()


# ── iPXE script ───────────────────────────────────────────────────────

def test_ipxe_sanboots_a_boot_machine(client, disks):
    _boot_machine(client, disks)
    script = _ipxe(client)
    assert f"set initiator-iqn {IQN}" in script
    assert f"set root-path iscsi:192.168.10.1::3260:0:{TARGET}" in script
    assert "sanboot ${root-path} || goto failed" in script
    assert "--no-describe" not in script


def test_ipxe_finds_the_mac_the_agent_reported(client, disks):
    m = _boot_machine(client, disks, mac=None)
    r = client.post("/api/v1/agent/heartbeat", json={
        "name": "vip-05", "agent_version": "0.2.0", "initiator_iqn": IQN,
        "iscsi_connected": True, "inventory": {"mac_address": MAC}})
    assert r.status_code == 200, r.text
    assert _get(client, m)["reported_mac"] == MAC
    assert "sanboot" in _ipxe(client)


@pytest.mark.parametrize("mac", ["not-a-mac", ""])
def test_ipxe_without_a_valid_mac_boots_locally(client, disks, mac):
    _boot_machine(client, disks)
    script = _ipxe(client, mac)
    assert script.rstrip().endswith("exit")
    assert "sanboot" not in script and "prompt" not in script


def test_ipxe_unknown_pc_waits_for_registration(client, disks):
    """An unregistered PC stays on screen until the admin adds it."""
    _boot_machine(client, disks)
    script = _ipxe(client, "aa-bb-cc-dd-ee-99")
    assert "not registered (MAC aa:bb:cc:dd:ee:99)" in script
    assert "Machines -> New PCs" in script
    assert "prompt --timeout 10000 " in script and "&& goto local || goto again" in script
    # It asks again with its own MAC, iPXE filling it in, and boots as soon as it is known.
    assert ("chain --replace --autofree http://testserver/api/v1/boot/ipxe?mac=${net0/mac}"
            "&pci=${ggnet_pci}&cls=${ggnet_cls}&sub=${ggnet_sub}&rev=${ggnet_rev} || goto wait") in script
    assert script.rstrip().endswith("exit")       # a key press boots the local disk
    assert "sanboot" not in script
    # Once registered as a Boot Mode PC, the same request boots it.
    r = client.post("/api/v1/machines", json={
        "name": "pc-99", "mode": "boot", "mac": "aa:bb:cc:dd:ee:99",
        "game_disk_id": disks[1]["id"], "os_disk_id": disks[0]["id"]})
    assert r.status_code == 201, r.text
    assert "sanboot" in _ipxe(client, "aa-bb-cc-dd-ee-99")


def test_ipxe_unknown_pc_boots_locally_when_configured(client, disks, monkeypatch):
    from app.api.v1 import boot
    monkeypatch.setattr(boot, "unregistered_mode", lambda cfg=None: "local")
    script = _ipxe(client, "aa-bb-cc-dd-ee-99")
    assert "not registered" in script and "prompt" not in script
    assert script.rstrip().endswith("exit")


@pytest.mark.parametrize("value, mode", [(None, "wait"), ("wait", "wait"), ("local", "local"), ("nope", "wait")])
def test_unregistered_mode(value, mode):
    from app.api.v1.boot import unregistered_mode
    assert unregistered_mode({} if value is None else {"boot": {"unregistered": value}}) == mode


def test_ipxe_disk_mode_pc_boots_locally(client, disks):
    client.post("/api/v1/machines", json={"name": "pc01", "mac": MAC})
    script = _ipxe(client)
    assert "Disk Mode" in script and "sanboot" not in script


def test_ipxe_boot_machine_without_disks_boots_locally(client):
    client.post("/api/v1/machines", json={"name": "vip-05", "mode": "boot", "mac": MAC})
    script = _ipxe(client)
    assert "cannot boot" in script and "sanboot" not in script


def test_ipxe_discards_a_dirty_writeback_before_boot(client, host, disks, auto, db_url):
    m = _boot_machine(client, disks)
    _mark_dirty(db_url)
    host.calls.clear()
    assert "sanboot" in _ipxe(client)
    assert sorted(_clones_made(host)) == [GAME_CLONE, OS_CLONE]
    assert _get(client, m)["writeback_dirty"] is False


def test_ipxe_does_not_discard_while_a_session_is_live(client, host, disks, auto, db_url):
    _boot_machine(client, disks)
    _mark_dirty(db_url)
    host.sessions.add(IQN)
    host.calls.clear()
    assert "sanboot" in _ipxe(client)
    assert _clones_made(host) == []


def test_ipxe_discard_failure_boots_locally(client, host, disks, auto, db_url):
    m = _boot_machine(client, disks)
    _mark_dirty(db_url)
    host.fail_on[("zfs", "clone")] = "pool is full"
    script = _ipxe(client)
    assert "sanboot" not in script and "pool is full" in script
    assert _get(client, m)["status"] == "error"


def test_boot_traffic_sums_both_luns(client, host, disks):
    m = _boot_machine(client, disks)
    host.traffic[IQN] = (100, 7)
    out = client.get("/api/v1/machines/traffic").json()
    row = next(t for t in out if t["machine_id"] == m["id"])
    assert (row["received_bytes"], row["sent_bytes"]) in {(14 << 20, 200 << 20), (200 << 20, 14 << 20)}


# ── The PC's identity in its OS writeback (app.windows_setup) ─────────

class FakeWindows:
    """Records each personalize() and what was on the target at that moment."""

    def __init__(self, host, problem: str | None = None):
        self.host = host
        self.problem = problem
        self.done: list[tuple[str, object, bool]] = []

    def personalize(self, device, setup, label=None):
        mapped = any(c[0] == "targetcli" and OS_CLONE.split("/")[-1] in " ".join(c)
                     for c in self.host.calls)
        self.done.append((device, setup, mapped))
        return self.problem

    prep_problem: str | None = None

    def nic_support(self, device, nics):
        self.checked = getattr(self, "checked", []) + [(device, list(nics))]
        if getattr(self, "check_problem", None):
            return self.check_problem
        known = {"VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15": "ready",
                 "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16": "ready"}
        # A reported instance the image does not have is added at boot.
        # Installed under another board's instance only: that PC needs Add new hardware.
        return {(n, i): "missing" if i == "6&95d5d6a&0&0000020A" else known.get(n, "missing") for n, i in nics}

    def prepare_image(self, device, prep, label="image"):
        # Whether the clone was already made a version when it was prepared.
        promoted = any(c[:2] == ["zfs", "promote"] for c in self.host.calls)
        self.prepared = getattr(self, "prepared", []) + [(device, prep, promoted)]
        return self.prep_problem


@pytest.fixture
def windows(prov, host):
    prov.windows = FakeWindows(host)
    return prov.windows


def test_a_new_os_writeback_gets_the_pc_identity_before_it_is_mapped(client, host, disks, windows):
    host.calls.clear()
    m = _boot_machine(client, disks)
    [(device, setup, mapped)] = windows.done
    assert device == f"/dev/zvol/{OS_CLONE}"
    assert mapped is False
    assert setup.computer_name == "vip-05"
    assert setup.env == {"GGNET_SERVER": "http://127.0.0.1:8088",
                         "GGNET_MACHINE_ID": str(m["id"]), "GGNET_MACHINE_NAME": "vip-05"}


def test_every_discard_sets_it_up_again(client, host, disks, auto, db_url, windows):
    _boot_machine(client, disks)
    _mark_dirty(db_url)
    windows.done.clear()
    assert "sanboot" in _ipxe(client)
    assert [d for d, _, _ in windows.done] == [f"/dev/zvol/{OS_CLONE}"]


def test_a_setup_problem_does_not_stop_the_boot(client, host, disks, windows):
    windows.problem = "no Windows partition found"
    m = _boot_machine(client, disks)
    assert m["status"] == "provisioned"
    assert "sanboot" in _ipxe(client)


def test_disk_mode_pcs_are_never_set_up(client, disks, windows):
    _, game = disks
    r = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": game["id"]})
    assert r.status_code == 201
    assert windows.done == []


# ── [boot] gateway ───────────────────────────────────────────────────

@pytest.mark.parametrize("value, line", [
    (None, "set netX/gateway 0.0.0.0"),      # the default: the router's gateway broke the boot
    ("", "set netX/gateway 0.0.0.0"),
    ("none", "set netX/gateway 0.0.0.0"),
    ("dhcp", None),                          # keep the router's gateway
    ("192.168.10.1", "set netX/gateway 192.168.10.1"),
    ("not-an-ip", "set netX/gateway 0.0.0.0"),
])
def test_boot_gateway(value, line):
    from app.api.v1.boot import boot_gateway
    cfg = {} if value is None else {"boot": {"gateway": value}}
    assert boot_gateway(cfg) == (line.split()[-1] if line else None)


def test_the_gateway_goes_before_the_sanboot(client, disks, monkeypatch):
    from app.api.v1 import boot
    monkeypatch.setattr(boot, "boot_gateway", lambda cfg=None: "0.0.0.0")
    _boot_machine(client, disks)
    lines = _ipxe(client).splitlines()
    assert lines[1] == "set netX/gateway 0.0.0.0"
    assert lines.index("set netX/gateway 0.0.0.0") < next(i for i, l in enumerate(lines) if l.startswith("sanboot"))


def test_settings_saved_in_the_web_ui_apply_to_the_next_boot(client, disks):
    """Settings → Network boot overrides [boot] in config.toml, with no restart."""
    _boot_machine(client, disks)
    assert "set netX/gateway 0.0.0.0" in _ipxe(client)
    r = client.put("/api/v1/settings/boot", json={"gateway": "192.168.0.40", "unregistered": "local"})
    assert r.status_code == 200, r.text
    assert "set netX/gateway 192.168.0.40" in _ipxe(client)
    unknown = _ipxe(client, "aa-bb-cc-dd-ee-99")
    assert "not registered" in unknown and "prompt" not in unknown
    client.put("/api/v1/settings/boot", json={"gateway": "dhcp", "unregistered": "wait"})
    assert "netX/gateway" not in _ipxe(client)
    assert "prompt" in _ipxe(client, "aa-bb-cc-dd-ee-99")


# ── Add hardware: the boot NIC iPXE reads ────────────────────────────

# vip-05's Realtek 8168 as iPXE reads its PCI config space (hexraw, high byte first)
NIC = {"pci": "816810ec", "cls": "020000", "sub": "e0001458", "rev": "16"}
NIC_ID = "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16"


def _ipxe_nic(client, mac: str = "aa-bb-cc-dd-ee-05", **nic) -> str:
    r = client.get("/api/v1/boot/ipxe", params={"mac": mac, **(nic or NIC)})
    assert r.status_code == 200, r.text
    return r.text


@pytest.mark.parametrize("pci, cls, sub, rev, nic_id", [
    ("816810ec", "020000", "e0001458", "16", NIC_ID),
    ("81:68:10:ec", "02:00:00", "E0:00:14:58", "16", NIC_ID),   # hex with colons
    ("15f38086", "020000", "00008086", "03", "VEN_8086&DEV_15F3&SUBSYS_00008086&REV_03"),
    ("816810ec", "060400", "e0001458", "16", None),       # a PCI bridge, not a network card
    ("ffffffff", "020000", "ffffffff", "ff", None),       # nothing at that address
    ("", "", "", "", None),                               # iPXE could not read it
    ("816810e", "020000", "e0001458", "16", None),
    ("816810eg", "020000", "e0001458", "16", None),
])
def test_boot_nic_id(pci, cls, sub, rev, nic_id):
    from app.api.v1.boot import boot_nic_id
    assert boot_nic_id(pci, cls, sub, rev) == nic_id


def test_a_waiting_pc_reports_its_nic_and_keeps_it_when_added(client, disks, windows):
    script = _ipxe_nic(client, "aa-bb-cc-dd-ee-99")
    # It asks again with what autoexec.ipxe read.
    assert "&pci=${ggnet_pci}&cls=${ggnet_cls}&sub=${ggnet_sub}&rev=${ggnet_rev} || goto wait" in script
    [d] = client.get("/api/v1/discovery").json()
    assert d["boot_nic"] == NIC_ID
    r = client.post(f"/api/v1/discovery/{d['id']}/claim", json={
        "name": "pc-99", "mode": "boot", "game_disk_id": disks[1]["id"], "os_disk_id": disks[0]["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["boot_nic"] == NIC_ID


def test_a_new_nic_is_recorded_and_never_written_into_the_writeback(client, host, disks, auto, windows):
    m = _boot_machine(client, disks)
    assert _get(client, m)["boot_nic"] is None
    windows.done.clear()
    host.calls.clear()
    assert "sanboot" in _ipxe_nic(client)
    assert _get(client, m)["boot_nic"] == NIC_ID
    assert _clones_made(host) == []                     # the card is the image's business, not the writeback's
    assert windows.done == []


def test_a_new_nic_never_touches_a_kept_writeback(client, host, disks, auto):
    m = _boot_machine(client, disks)
    assert client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True}).status_code == 200
    host.calls.clear()
    assert "sanboot" in _ipxe_nic(client)
    assert _get(client, m)["boot_nic"] == NIC_ID
    assert _clones_made(host) == []


def test_an_unreadable_nic_changes_nothing(client, host, disks, auto):
    m = _boot_machine(client, disks)
    host.calls.clear()
    assert "sanboot" in _ipxe_nic(client, pci="", cls="", sub="", rev="")
    assert _get(client, m)["boot_nic"] is None
    assert _clones_made(host) == []



# ── Image-wide setup, once per OS version ────────────────────────────

def test_applying_the_os_writeback_prepares_it_first(client, host, disks, windows, db_url):
    m = _boot_machine(client, disks)
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    host.calls.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": True, "game": False})
    assert r.status_code == 200, r.text
    [(device, prep, promoted)] = windows.prepared
    assert device == f"/dev/zvol/{OS_CLONE}"
    assert promoted is False                   # prepared before it became the version


def test_applying_only_the_game_writeback_prepares_nothing(client, host, disks, windows):
    m = _boot_machine(client, disks)
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks", json={"os": False, "game": True})
    assert r.status_code == 200, r.text
    assert getattr(windows, "prepared", []) == []


def test_prepare_image_makes_a_new_active_version(client, host, disks, windows):
    os_disk, game = disks
    host.calls.clear()
    r = client.post(f"/api/v1/game-disks/{os_disk['id']}/prepare")
    assert r.status_code == 200, r.text
    assert r.json()["snapshot"] == "v2"
    tmp = "tank/ggnet/writebacks/win11_ggnet_prep"
    assert any(c[:2] == ["zfs", "clone"] and c[-2:] == ["tank/ggnet/images/win11@base", tmp]
               for c in host.calls)
    [(device, prep, promoted)] = windows.prepared
    assert device == f"/dev/zvol/{tmp}" and promoted is False
    versions = client.get(f"/api/v1/game-disks/{os_disk['id']}/snapshots").json()
    assert [v["name"] for v in versions] == ["base", "v2"]
    assert versions[-1]["comment"].startswith("ggNet: prepared for all PCs")
    # Game disks are not Windows images.
    assert client.post(f"/api/v1/game-disks/{game['id']}/prepare").status_code == 409


def test_prepare_image_can_turn_defender_off(client, host, disks, windows):
    os_disk, _ = disks
    r = client.post(f"/api/v1/game-disks/{os_disk['id']}/prepare", json={"defender_off": True})
    assert r.status_code == 200, r.text
    [(_, prep, _)] = windows.prepared
    assert prep.defender_off is True
    versions = client.get(f"/api/v1/game-disks/{os_disk['id']}/snapshots").json()
    assert versions[-1]["comment"].endswith(", Defender off")
    client.post(f"/api/v1/game-disks/{os_disk['id']}/prepare")       # without a body: left as it is
    assert windows.prepared[-1][1].defender_off is False


def test_a_failed_prepare_changes_nothing(client, host, disks, windows):
    os_disk, _ = disks
    windows.prep_problem = "no Program Files folder in the image"
    r = client.post(f"/api/v1/game-disks/{os_disk['id']}/prepare")
    assert r.status_code == 502
    assert "no Program Files folder" in r.text
    assert client.get(f"/api/v1/game-disks/{os_disk['id']}").json()["snapshot"] == "base"
    assert ["zfs", "destroy", "tank/ggnet/writebacks/win11_ggnet_prep"] in [c[:3] for c in host.calls]


def test_hardware_check_tells_which_pcs_the_image_can_boot(client, host, disks, windows, db_url):
    os_disk, game = disks
    for name, nic, inst in (("komp-01", "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16", None),
                            ("vip-01", "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15", "01000000684CE00000"),
                            ("asus-01", "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15", "6&95d5d6a&0&0000020A"),
                            ("new-pc", "VEN_8086&DEV_15B8&SUBSYS_86721043&REV_00", None),
                            ("disk-pc", None, None)):
        client.post("/api/v1/machines", json={"name": name, "game_disk_id": game["id"]})
        if nic:
            engine = make_engine(db_url)
            with engine.begin() as conn:
                conn.execute(text("UPDATE machines SET boot_nic = :n, nic_instance = :i WHERE name = :m"),
                             {"n": nic, "i": inst, "m": name})
            engine.dispose()
    host.calls.clear()
    r = client.get(f"/api/v1/game-disks/{os_disk['id']}/hardware")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot"] == "base" and body["problem"] is None
    assert [(n["status"], n["instance"], n["machines"]) for n in body["nics"]] == [
        ("ready", "01000000684CE00000", ["vip-01"]),
        ("missing", "6&95d5d6a&0&0000020A", ["asus-01"]),    # same card, another board: Add new hardware
        ("ready", None, ["komp-01"]),
        ("missing", None, ["new-pc"]),
        ("unknown", None, ["disk-pc"]),
    ]
    tmp = "tank/ggnet/writebacks/win11_ggnet_hw"
    assert windows.checked == [(f"/dev/zvol/{tmp}", [
        ("VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15", "01000000684CE00000"),
        ("VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15", "6&95d5d6a&0&0000020A"),
        ("VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16", None),
        ("VEN_8086&DEV_15B8&SUBSYS_86721043&REV_00", None)])]
    assert tmp not in host.datasets                                   # the temporary clone is gone
    client.get(f"/api/v1/game-disks/{os_disk['id']}/hardware")
    assert len(windows.checked) == 1                                  # a version is checked once
    assert client.get(f"/api/v1/game-disks/{game['id']}/hardware").status_code == 409


def test_a_failed_hardware_check_is_reported_and_retried(client, host, disks, windows, db_url):
    os_disk, game = disks
    client.post("/api/v1/machines", json={"name": "komp-01", "game_disk_id": game["id"]})
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE machines SET boot_nic = 'VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16'"))
    engine.dispose()
    windows.check_problem = "no Windows partition found"
    body = client.get(f"/api/v1/game-disks/{os_disk['id']}/hardware").json()
    assert (body["problem"], body["nics"]) == ("no Windows partition found", [])
    windows.check_problem = None
    assert client.get(f"/api/v1/game-disks/{os_disk['id']}/hardware").json()["nics"][0]["status"] == "ready"


# ── Spare writebacks ──────────────────────────────────────────────────

def test_a_restarting_pc_boots_on_its_spares(client, host, prov, disks, windows, auto, db_url):
    from app.services.writebacks import refresh_spares

    m = _boot_machine(client, disks)
    engine = make_engine(db_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        refresh_spares(db, prov)
    assert {f"{GAME_CLONE}_spare", f"{OS_CLONE}_spare"} <= set(host.datasets)
    # The PC restarts: iPXE asks while its writebacks are dirty.
    _mark_dirty(db_url)
    windows.done.clear()
    host.calls.clear()
    _ipxe(client)
    assert _clones_made(host) == []                       # swapped in, nothing cloned
    assert windows.done == []                             # nor set up again
    assert host.datasets[OS_CLONE]["origin"].endswith("@base")
    # Keep writeback: no spares.
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    with Session() as db:
        refresh_spares(db, prov)
    assert not [d for d in host.datasets if d.endswith("_spare")]
    engine.dispose()
