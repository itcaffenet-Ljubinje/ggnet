"""API v1 game images, through TestClient and FakeHost."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import make_engine

MASTER = "tank/ggnet/images/cs2"
SNAP = f"{MASTER}@base"


def _disk(client, name="cs2", publish=True) -> dict:
    r = client.post("/api/v1/images", json={"name": name, "size_gb": 10})
    assert r.status_code == 201, r.text
    if publish:
        r = client.post(f"/api/v1/images/{r.json()['id']}/publish")
        assert r.status_code == 200, r.text
    return r.json()


def _assign_in_db(db_url: str, image_id: int) -> None:
    """Assign the disk to a machine directly in the database (no machines API yet)."""
    engine = make_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO machines (name, mode, initiator_iqn, game_image_id, status, "
            "created_at, updated_at) VALUES ('pc01', 'disk', 'iqn.1991-05.com.microsoft:pc01', "
            ":id, 'idle', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"id": image_id})
    engine.dispose()


def test_create_and_publish_disk(client, host):
    r = client.post("/api/v1/images", json={"name": "CS2", "size_gb": 10})
    assert r.status_code == 201
    d = r.json()
    assert (d["name"], d["zvol_path"], d["published"]) == ("cs2", MASTER, False)
    assert host.datasets[MASTER]["readonly"] is False

    r = client.post(f"/api/v1/images/{d['id']}/publish")
    assert r.json()["snapshot"] == "base" and r.json()["published"]
    assert host.snapshots[SNAP] == {"ggnet:protected"}
    assert host.datasets[MASTER]["readonly"] is True

    assert client.post(f"/api/v1/images/{d['id']}/publish").status_code == 409


def test_list_and_get(client):
    a = _disk(client, "valorant", publish=False)
    b = _disk(client, "cs2", publish=False)
    assert [d["name"] for d in client.get("/api/v1/images").json()] == ["cs2", "valorant"]
    assert client.get(f"/api/v1/images/{a['id']}").json()["name"] == "valorant"
    assert client.get(f"/api/v1/images/{b['id'] + 100}").status_code == 404


def test_create_disk_duplicate_name(client):
    _disk(client, publish=False)
    r = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 409


def test_create_disk_existing_zvol_on_host(client, host):
    host.add_dataset(MASTER)
    r = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 502
    assert "already exists" in r.json()["detail"]["error"]


def test_rename_disk_keeps_zvol(client, host):
    d = _disk(client)
    r = client.patch(f"/api/v1/images/{d['id']}", json={"name": " Steam-Main "})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["zvol_path"]) == ("steam-main", MASTER)
    assert host.datasets[MASTER]["readonly"] is True
    assert client.get(f"/api/v1/images/{d['id']}").json()["name"] == "steam-main"


def test_rename_disk_refuses_taken_or_invalid_name(client):
    a = _disk(client, "cs2", publish=False)
    _disk(client, "valorant", publish=False)
    assert client.patch(f"/api/v1/images/{a['id']}", json={"name": "valorant"}).status_code == 409
    assert client.patch(f"/api/v1/images/{a['id']}", json={"name": "a b"}).status_code == 422
    assert client.patch(f"/api/v1/images/{a['id'] + 100}", json={"name": "x"}).status_code == 404
    assert client.patch(f"/api/v1/images/{a['id']}", json={"name": "cs2"}).status_code == 200


def test_create_disk_refuses_old_name_of_renamed_disk(client):
    d = _disk(client, "cs2", publish=False)
    client.patch(f"/api/v1/images/{d['id']}", json={"name": "cs2-old"})
    r = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 409
    assert "cs2-old" in r.json()["detail"]["error"]


@pytest.mark.parametrize("body", [
    {"name": "../x", "size_gb": 10},
    {"name": "-r", "size_gb": 10},
    {"name": "a b", "size_gb": 10},
    {"name": "x" * 33, "size_gb": 10},
    {"name": "ok", "size_gb": 0},
    {"name": "ok", "size_gb": 16385},
])
def test_create_disk_validation(client, host, body):
    assert client.post("/api/v1/images", json=body).status_code == 422
    assert host.calls == []


def test_create_disk_host_failure_is_502_and_not_saved(client, host):
    host.fail_on[("zfs", "create")] = "out of space"
    r = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == ("Creating the image cs2 failed: not enough free space on the pool "
                                           "(ZFS: out of space)")
    assert client.get("/api/v1/images").json() == []


def test_publish_host_failure_is_502_and_stays_draft(client, host):
    d = _disk(client, publish=False)
    host.fail_on[("zfs", "snapshot")] = "pool is full"
    r = client.post(f"/api/v1/images/{d['id']}/publish")
    assert r.status_code == 502
    assert client.get(f"/api/v1/images/{d['id']}").json()["published"] is False


def test_delete_disk_refused_while_assigned(client, db_url):
    d = _disk(client)
    _assign_in_db(db_url, d["id"])
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 409


def test_delete_published_disk(client, host):
    d = _disk(client)
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 204
    assert MASTER not in host.datasets and SNAP not in host.snapshots
    assert client.get(f"/api/v1/images/{d['id']}").status_code == 404


def test_delete_draft_disk(client, host):
    d = _disk(client, publish=False)
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 204
    assert MASTER not in host.datasets


def test_delete_host_failure_keeps_record(client, host):
    d = _disk(client)
    host.fail_on[("zfs", "destroy")] = "dataset is busy"
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 502
    assert client.get(f"/api/v1/images/{d['id']}").status_code == 200
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

    r = client.post(f"/api/v1/images/{d['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["editor_id"] == m["id"]
    # The draft zvol itself is the PC's game image; nothing is cloned.
    assert host.visible(IQN) == [f"/dev/zvol/{MASTER}"]
    m = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (m["status"], m["editing_image_id"], m["iscsi_target_iqn"]) == ("editing", d["id"], TARGET)

    # The agent gets the target, so the PC connects the master as D:.
    r = client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.1"})
    assert (r.json()["iscsi_target_iqn"], r.json()["game_image"]) == (TARGET, "cs2")

    # Nothing may pull the master away while it is being edited.
    assert client.post(f"/api/v1/images/{d['id']}/publish").status_code == 409
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 409
    assert client.post(f"/api/v1/machines/{m['id']}/assign", json={"game_image_id": None}).status_code == 409
    assert client.delete(f"/api/v1/machines/{m['id']}").status_code == 409

    host.sessions.add(IQN)
    r = client.post(f"/api/v1/images/{d['id']}/finish-edit")
    assert r.status_code == 409 and "shut it down" in r.text
    assert host.visible(IQN) == [f"/dev/zvol/{MASTER}"]

    host.sessions.discard(IQN)
    r = client.post(f"/api/v1/images/{d['id']}/finish-edit")
    assert r.status_code == 200 and r.json()["editor_id"] is None
    assert host.visible(IQN) == [] and not host.backstores
    m = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (m["status"], m["editing_image_id"], m["iscsi_target_iqn"]) == ("idle", None, None)

    r = client.post(f"/api/v1/images/{d['id']}/publish")
    assert r.status_code == 200 and r.json()["snapshot"] == "base"


def test_edit_is_idempotent_and_one_editor_per_disk(client, host):
    d = _disk(client, publish=False)
    m1 = _machine(client)
    m2 = _machine(client, name="pc02")
    url = f"/api/v1/images/{d['id']}/edit"
    assert client.post(url, json={"machine_id": m1["id"]}).status_code == 200
    assert client.post(url, json={"machine_id": m1["id"]}).status_code == 200
    r = client.post(url, json={"machine_id": m2["id"]})
    assert r.status_code == 409 and "pc01" in r.text
    assert host.visible("iqn.1991-05.com.microsoft:pc02") == []


def test_edit_refused_for_published_disk_or_busy_machine(client):
    published = _disk(client)
    draft = _disk(client, name="draft", publish=False)
    m = _machine(client, game_image_id=published["id"])
    r = client.post(f"/api/v1/images/{published['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 409 and "Apply Writebacks" in r.text
    r = client.post(f"/api/v1/images/{draft['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 409 and "remove its disk first" in r.text
    r = client.post(f"/api/v1/images/{draft['id']}/edit", json={"machine_id": 999})
    assert r.status_code == 404


def test_edit_host_failure_leaves_machine_idle(client, host):
    d = _disk(client, publish=False)
    m = _machine(client)
    host.fail_on[("targetcli", "/backstores/block", "create")] = "device busy"
    r = client.post(f"/api/v1/images/{d['id']}/edit", json={"machine_id": m["id"]})
    assert r.status_code == 502 and "device busy" in r.text
    assert client.get(f"/api/v1/machines/{m['id']}").json()["status"] == "idle"
    assert client.get(f"/api/v1/images/{d['id']}").json()["editor_id"] is None


def test_finish_edit_without_editor(client):
    d = _disk(client, publish=False)
    assert client.post(f"/api/v1/images/{d['id']}/finish-edit").status_code == 409


# ── Default images and the Images table's numbers ─────────────────────

def test_one_default_per_kind_and_only_when_published(client, host):
    def make(name, kind="game"):
        d = client.post("/api/v1/images", json={"name": name, "size_gb": 10, "kind": kind}).json()
        return d

    cs2, valorant, win11 = make("cs2"), make("valorant"), make("win11", "os")
    r = client.put(f"/api/v1/images/{cs2['id']}/default", json={"default": True})
    assert r.status_code == 409 and "not published" in r.text
    for d in (cs2, valorant, win11):
        assert client.post(f"/api/v1/images/{d['id']}/publish").status_code == 200
    for d in (cs2, valorant, win11):
        assert client.put(f"/api/v1/images/{d['id']}/default", json={"default": True}).status_code == 200
    defaults = {d["name"]: d["is_default"] for d in client.get("/api/v1/images").json()}
    assert defaults == {"cs2": False, "valorant": True, "win11": True}     # one game image, one OS image
    assert client.put(f"/api/v1/images/{win11['id']}/default", json={"default": False}).json()["is_default"] is False


def test_the_images_table_reads_size_compression_and_versions(client, host):
    d = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10}).json()
    client.post(f"/api/v1/images/{d['id']}/publish")
    zvol = d["zvol_path"]
    host.datasets[zvol].setdefault("props", {}).update({"volsize": str(10 << 30), "compressratio": "1.65x"})
    host.sizes[zvol] = (3 << 30, 2 << 30)
    host.snapshots[f"{zvol}@v2"] = set()
    [u] = client.get("/api/v1/images/usage").json()
    assert u == {"image_id": d["id"], "used_bytes": 3 << 30, "volume_bytes": 10 << 30,
                 "compress_ratio": 1.65, "versions": 2}


def test_an_image_bigger_than_the_free_space_is_refused_in_plain_words(client, host):
    host.datasets["tank/ggnet/images"].setdefault("props", {})["avail"] = str(500 * 1024 ** 3)
    r = client.post("/api/v1/images", json={"name": "game-test", "size_gb": 10000})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == ("Not enough space for a 10000 GB image: 500 GB is free on the pool. "
                                           "Choose a smaller size; an image can be grown later (Edit).")
    assert "tank/ggnet/images/game-test" not in host.datasets
    assert client.post("/api/v1/images", json={"name": "game-test", "size_gb": 400}).status_code == 201


def test_host_errors_are_shown_in_plain_words():
    from app.services.provisioning import plain_error

    assert plain_error("cannot create 'tank/ggnet/images/x': out of space") == (
        "not enough free space on the pool (ZFS: out of space)")
    assert plain_error("cannot destroy 'x': dataset is busy").startswith("it is in use right now")
    assert plain_error("something new") == "something new"
