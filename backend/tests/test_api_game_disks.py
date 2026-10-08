"""API v1 game disks, through TestClient and FakeHost."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import make_engine

MASTER = "tank/ggnet/images/cs2"
SNAP = f"{MASTER}@base"


def _disk(client, name="cs2", publish=True) -> dict:
    r = client.post("/api/v1/game-disks", json={"name": name, "size_gb": 10})
    assert r.status_code == 201, r.text
    if publish:
        r = client.post(f"/api/v1/game-disks/{r.json()['id']}/publish")
        assert r.status_code == 200, r.text
    return r.json()


def _assign_in_db(db_url: str, disk_id: int) -> None:
    """Assign the disk to a machine directly in the database (no machines API yet)."""
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO machines (name, mode, initiator_iqn, game_disk_id, status, "
            "created_at, updated_at) VALUES ('pc01', 'disk', 'iqn.1991-05.com.microsoft:pc01', "
            ":id, 'idle', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"id": disk_id})
    engine.dispose()


def test_create_and_publish_disk(client, host):
    r = client.post("/api/v1/game-disks", json={"name": "CS2", "size_gb": 10})
    assert r.status_code == 201
    d = r.json()
    assert (d["name"], d["zvol_path"], d["published"]) == ("cs2", MASTER, False)
    assert host.datasets[MASTER]["readonly"] is False

    r = client.post(f"/api/v1/game-disks/{d['id']}/publish")
    assert r.json()["snapshot"] == "base" and r.json()["published"]
    assert host.snapshots[SNAP] == {"ggnet:protected"}
    assert host.datasets[MASTER]["readonly"] is True

    assert client.post(f"/api/v1/game-disks/{d['id']}/publish").status_code == 409


def test_list_and_get(client):
    a = _disk(client, "valorant", publish=False)
    b = _disk(client, "cs2", publish=False)
    assert [d["name"] for d in client.get("/api/v1/game-disks").json()] == ["cs2", "valorant"]
    assert client.get(f"/api/v1/game-disks/{a['id']}").json()["name"] == "valorant"
    assert client.get(f"/api/v1/game-disks/{b['id'] + 100}").status_code == 404


def test_create_disk_duplicate_name(client):
    _disk(client, publish=False)
    r = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 409


def test_create_disk_existing_zvol_on_host(client, host):
    host.add_dataset(MASTER)
    r = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 502
    assert "already exists" in r.json()["detail"]["error"]


@pytest.mark.parametrize("body", [
    {"name": "../x", "size_gb": 10},
    {"name": "-r", "size_gb": 10},
    {"name": "a b", "size_gb": 10},
    {"name": "x" * 33, "size_gb": 10},
    {"name": "ok", "size_gb": 0},
    {"name": "ok", "size_gb": 16385},
])
def test_create_disk_validation(client, host, body):
    assert client.post("/api/v1/game-disks", json=body).status_code == 422
    assert host.calls == []


def test_create_disk_host_failure_is_502_and_not_saved(client, host):
    host.fail_on[("zfs", "create")] = "out of space"
    r = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 502
    assert "out of space" in r.json()["detail"]["error"]
    assert client.get("/api/v1/game-disks").json() == []


def test_publish_host_failure_is_502_and_stays_draft(client, host):
    d = _disk(client, publish=False)
    host.fail_on[("zfs", "snapshot")] = "pool is full"
    r = client.post(f"/api/v1/game-disks/{d['id']}/publish")
    assert r.status_code == 502
    assert client.get(f"/api/v1/game-disks/{d['id']}").json()["published"] is False


def test_delete_disk_refused_while_assigned(client, db_url):
    d = _disk(client)
    _assign_in_db(db_url, d["id"])
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 409


def test_delete_published_disk(client, host):
    d = _disk(client)
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 204
    assert MASTER not in host.datasets and SNAP not in host.snapshots
    assert client.get(f"/api/v1/game-disks/{d['id']}").status_code == 404


def test_delete_draft_disk(client, host):
    d = _disk(client, publish=False)
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 204
    assert MASTER not in host.datasets


def test_delete_host_failure_keeps_record(client, host):
    d = _disk(client)
    host.fail_on[("zfs", "destroy")] = "dataset is busy"
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 502
    assert client.get(f"/api/v1/game-disks/{d['id']}").status_code == 200
    assert host.snapshots[SNAP] == {"ggnet:protected"}   # hold restored


# ── Edit master (fill a draft on one PC) ─────────────────────────────

IQN = "iqn.1991-05.com.microsoft:pc01"
TARGET = "iqn.2025-05.net.ggnet:storage"


def _machine(client, name="pc01", **body) -> dict:
    r = client.post("/api/v1/machines", json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_edit_master_full_cycle(client, host):
    d = _disk(client, publish=False)
    m = _machine(client)

    r = client.post(f"/api/v1/game-disks/{d['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["editor_id"] == m["id"]
    # The draft zvol itself is the PC's game disk; nothing is cloned.
    assert host.visible(IQN) == [f"/dev/zvol/{MASTER}"]
    m = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (m["status"], m["editing_disk_id"], m["iscsi_target_iqn"]) == ("editing", d["id"], TARGET)

    # The agent gets the target, so the PC connects the master as D:.
    r = client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.1"})
    assert (r.json()["iscsi_target_iqn"], r.json()["game_disk"]) == (TARGET, "cs2")

    # Nothing may pull the master away while it is being edited.
    assert client.post(f"/api/v1/game-disks/{d['id']}/publish").status_code == 409
    assert client.delete(f"/api/v1/game-disks/{d['id']}").status_code == 409
    assert client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_disk_id": None}).status_code == 409
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 409

    host.sessions.add(IQN)
    r = client.post(f"/api/v1/game-disks/{d['id']}/finish-edit")
    assert r.status_code == 409 and "shut it down" in r.text
    assert host.visible(IQN) == [f"/dev/zvol/{MASTER}"]

    host.sessions.discard(IQN)
    r = client.post(f"/api/v1/game-disks/{d['id']}/finish-edit")
    assert r.status_code == 200 and r.json()["editor_id"] is None
    assert host.visible(IQN) == [] and not host.backstores
    m = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (m["status"], m["editing_disk_id"], m["iscsi_target_iqn"]) == ("idle", None, None)

    r = client.post(f"/api/v1/game-disks/{d['id']}/publish")
    assert r.status_code == 200 and r.json()["snapshot"] == "base"


def test_edit_is_idempotent_and_one_editor_per_disk(client, host):
    d = _disk(client, publish=False)
    m1 = _machine(client)
    m2 = _machine(client, name="pc02")
    url = f"/api/v1/game-disks/{d['id']}/edit"
    assert client.post(url, json={"machine_id": m1["id"]}).status_code == 200
    assert client.post(url, json={"machine_id": m1["id"]}).status_code == 200
    r = client.post(url, json={"machine_id": m2["id"]})
    assert r.status_code == 409 and "pc01" in r.text
    assert host.visible("iqn.1991-05.com.microsoft:pc02") == []


def test_edit_refused_for_published_disk_or_busy_machine(client):
    published = _disk(client)
    draft = _disk(client, name="draft", publish=False)
    m = _machine(client, game_disk_id=published["id"])
    r = client.post(f"/api/v1/game-disks/{published['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 409 and "Apply Writebacks" in r.text
    r = client.post(f"/api/v1/game-disks/{draft['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 409 and "remove its disk first" in r.text
    r = client.post(f"/api/v1/game-disks/{draft['id']}/edit", json={"machine_id": 999})
    assert r.status_code == 404


def test_edit_host_failure_leaves_machine_idle(client, host):
    d = _disk(client, publish=False)
    m = _machine(client)
    host.fail_on[("targetcli", "/backstores/block", "create")] = "device busy"
    r = client.post(f"/api/v1/game-disks/{d['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 502 and "device busy" in r.text
    assert client.get(f"/api/v1/machines/{m['id']}").json()["status"] == "idle"
    assert client.get(f"/api/v1/game-disks/{d['id']}").json()["editor_id"] is None


def test_finish_edit_without_editor(client):
    d = _disk(client, publish=False)
    assert client.post(f"/api/v1/game-disks/{d['id']}/finish-edit").status_code == 409
