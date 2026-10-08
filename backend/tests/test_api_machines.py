"""API v1 machines, through TestClient and FakeHost."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import make_engine

SNAP = "tank/ggnet/images/cs2@base"
CLONE = "tank/ggnet/writebacks/pc01"
TARGET = "iqn.2025-05.net.ggnet:storage"
IQN = "iqn.1991-05.com.microsoft:pc01"


def _disk(client, name="cs2", publish=True) -> dict:
    r = client.post("/api/v1/game-disks", json={"name": name, "size_gb": 10})
    assert r.status_code == 201, r.text
    if publish:
        r = client.post(f"/api/v1/game-disks/{r.json()['id']}/publish")
        assert r.status_code == 200, r.text
    return r.json()


def _machine(client, **body) -> dict:
    r = client.post("/api/v1/machines", json={"name": "pc01", **body})
    assert r.status_code == 201, r.text
    return r.json()


# ── Create ────────────────────────────────────────────────────────────

def test_create_machine_without_disk(client, host):
    r = client.post("/api/v1/machines", json={"name": "PC01", "mac": "AA-BB-CC-DD-EE-FF"})
    assert r.status_code == 201
    m = r.json()
    assert m["name"] == "pc01"
    assert m["mode"] == "disk"
    assert m["initiator_iqn"] == "iqn.1991-05.com.microsoft:pc01"
    assert m["mac"] == "aa:bb:cc:dd:ee:ff"
    assert m["status"] == "idle"
    assert host.calls == []


def test_create_machine_auto_provisions(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    assert m["status"] == "provisioned"
    assert m["clone_zvol"] == CLONE
    assert m["clone_snapshot"] == SNAP
    assert m["iscsi_target_iqn"] == TARGET
    assert m["outdated"] is False
    assert host.visible(IQN) == [f"/dev/zvol/{CLONE}"]


def test_create_machine_with_unpublished_disk(client):
    d = _disk(client, publish=False)
    r = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": d["id"]})
    assert r.status_code == 409
    assert client.get("/api/v1/machines").json() == []


def test_create_machine_unknown_disk(client):
    r = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": 42})
    assert r.status_code == 404


def test_create_boot_mode_machine_cannot_get_disk(client, host):
    d = _disk(client)
    host.calls.clear()
    r = client.post("/api/v1/machines",
                    json={"name": "pc01", "mode": "boot", "game_disk_id": d["id"]})
    assert r.status_code == 409
    assert host.calls == []
    assert _machine(client, mode="boot")["mode"] == "boot"


def test_create_machine_host_failure_saved_as_error(client, host):
    d = _disk(client)
    host.fail_on[("zfs", "clone")] = "pool is full"
    r = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": d["id"]})
    assert r.status_code == 502
    mid = r.json()["detail"]["machine_id"]
    m = client.get(f"/api/v1/machines/{mid}").json()
    assert m["status"] == "error" and "pool is full" in m["last_error"]
    assert m["game_disk_id"] == d["id"]

    # Assigning again after the host is fixed recovers the machine.
    del host.fail_on[("zfs", "clone")]
    r = client.post(f"/api/v1/machines/{mid}/assign", json={"game_disk_id": d["id"]})
    assert r.status_code == 200 and r.json()["status"] == "provisioned"


@pytest.mark.parametrize("body", [
    {"name": "a-very-long-pc-name"},
    {"name": "123"},
    {"name": "pc_01"},
    {"name": "pc01", "mode": "pxe"},
    {"name": "pc01", "initiator_iqn": "not-an-iqn"},
    {"name": "pc01", "initiator_iqn": "iqn.1991-05.com.microsoft:pc_01"},
    {"name": "pc01", "mac": "zz:zz:zz:zz:zz:zz"},
])
def test_create_machine_validation(client, host, body):
    assert client.post("/api/v1/machines", json=body).status_code == 422
    assert host.calls == []


def test_create_machine_duplicates(client):
    _machine(client, mac="aa:bb:cc:dd:ee:01")
    for body in ({"name": "pc01"},
                 {"name": "pc02", "initiator_iqn": "iqn.1991-05.com.microsoft:pc01"},
                 {"name": "pc03", "mac": "AA:BB:CC:DD:EE:01"}):
        assert client.post("/api/v1/machines", json=body).status_code == 409


# ── Update ────────────────────────────────────────────────────────────

def test_update_host_bound_fields_only_while_idle(client):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    for body in ({"name": "pc99"}, {"initiator_iqn": "iqn.1991-05.com.microsoft:pc99"},
                 {"mode": "boot"}):
        assert client.patch(f"/api/v1/machines/{m['id']}", json=body).status_code == 409
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"mac": "aa:bb:cc:dd:ee:ff"})
    assert r.status_code == 200 and r.json()["mac"] == "aa:bb:cc:dd:ee:ff"
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"mac": None})
    assert r.json()["mac"] is None


def test_update_idle_machine(client):
    m = _machine(client)
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"name": "pc02", "mode": "boot"})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["mode"]) == ("pc02", "boot")


@pytest.mark.parametrize("body", [{"name": None}, {"mode": None}, {"initiator_iqn": None}])
def test_update_rejects_null_for_required_fields(client, body):
    m = _machine(client)
    assert client.patch(f"/api/v1/machines/{m['id']}", json=body).status_code == 422


# ── Assign / reset ────────────────────────────────────────────────────

def test_assign_switch_and_unassign(client, host):
    d1 = _disk(client, "cs2")
    d2 = _disk(client, "valorant")
    m = _machine(client, game_disk_id=d1["id"])

    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": d2["id"]})
    assert r.status_code == 200
    assert r.json()["clone_snapshot"] == "tank/ggnet/images/valorant@base"
    assert host.datasets[CLONE]["origin"].endswith("valorant@base")

    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": None})
    assert r.json()["status"] == "idle" and r.json()["game_disk_id"] is None
    assert CLONE not in host.datasets
    assert host.visible(IQN) == [] and not host.backstores


def test_assign_same_disk_is_noop(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    host.calls.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": d["id"]})
    assert r.status_code == 200 and host.calls == []


def test_assign_to_boot_mode_machine_refused(client, host):
    d = _disk(client)
    m = _machine(client, mode="boot")
    host.calls.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": d["id"]})
    assert r.status_code == 409 and host.calls == []


def test_reset_machine(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    host.calls.clear()
    r = client.post(f"/api/v1/machines/{m['id']}/reset")
    assert r.status_code == 200 and r.json()["status"] == "provisioned"
    assert any(c[:2] == ["zfs", "destroy"] for c in host.calls)
    assert any(c[:2] == ["zfs", "clone"] for c in host.calls)


def test_reset_without_disk(client):
    m = _machine(client)
    assert client.post(f"/api/v1/machines/{m['id']}/reset").status_code == 409


def test_reset_failure_is_502_and_recorded(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    host.fail_on[("zfs", "destroy")] = "dataset is busy"
    r = client.post(f"/api/v1/machines/{m['id']}/reset")
    assert r.status_code == 502
    m = client.get(f"/api/v1/machines/{m['id']}").json()
    assert m["status"] == "error" and "busy" in m["last_error"]

    del host.fail_on[("zfs", "destroy")]
    r = client.post(f"/api/v1/machines/{m['id']}/reset")
    assert r.status_code == 200 and r.json()["status"] == "provisioned"
    assert r.json()["last_error"] is None


def test_outdated_after_new_snapshot(client, db_url):
    """When the disk gets @v2, a machine on @base is outdated until a reset."""
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE game_disks SET snapshot='v2' WHERE id=:id"), {"id": d["id"]})
    engine.dispose()
    assert client.get(f"/api/v1/machines/{m['id']}").json()["outdated"] is True


# ── Delete ────────────────────────────────────────────────────────────

def test_delete_machine_cleans_host(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 204
    assert CLONE not in host.datasets
    assert host.visible(IQN) == [] and not host.backstores
    assert client.get(f"/api/v1/machines/{m['id']}").status_code == 404


def test_delete_machine_host_failure_keeps_record(client, host):
    d = _disk(client)
    m = _machine(client, game_disk_id=d["id"])
    host.fail_on[("zfs", "destroy")] = "dataset is busy"
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 502
    assert client.get(f"/api/v1/machines/{m['id']}").json()["status"] == "error"


def test_game_disk_delete_refused_while_assigned_via_api(client):
    d = _disk(client)
    _machine(client, game_disk_id=d["id"])
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 409


def test_list_machines_sorted(client):
    _machine(client)
    client.post("/api/v1/machines", json={"name": "pc00"})
    assert [m["name"] for m in client.get("/api/v1/machines").json()] == ["pc00", "pc01"]


def test_timestamps_read_back_from_database_are_utc(client):
    """SQLite drops the offset; the API must still send UTC ("Z") after a reload."""
    m = _machine(client)
    again = client.get(f"/api/v1/machines/{m['id']}").json()
    for key in ("created_at", "updated_at"):
        assert again[key].endswith("Z"), again[key]
        assert again[key] == m[key]
