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
