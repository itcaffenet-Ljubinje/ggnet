"""Bulk operations: one action on several machines, results per machine."""

from __future__ import annotations

import pytest

from app.api.deps import get_wol_sender
from app.main import app


@pytest.fixture
def wol():
    sent: list[str] = []
    app.dependency_overrides[get_wol_sender] = lambda: sent.append
    yield sent
    app.dependency_overrides.pop(get_wol_sender, None)


@pytest.fixture
def disk(client) -> dict:
    d = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10}).json()
    return client.post(f"/api/v1/images/{d['id']}/publish").json()


def _machines(client, *names, **body) -> list[dict]:
    return [client.post("/api/v1/machines", json={"name": n, **body}).json() for n in names]


def _bulk(client, ids, action, **extra) -> list[dict]:
    r = client.post("/api/v1/machines/bulk", json={"machine_ids": ids, "action": action, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _beat(client, name):
    client.post("/api/v1/agent/heartbeat", json={"name": name, "agent_version": "0.1.9"})


def test_turn_on_reports_each_machine(client, wol):
    a, b = _machines(client, "pc01", "pc02")
    client.patch(f"/api/v1/machines/{a['id']}", json={"mac": "aa:bb:cc:dd:ee:01"})
    res = _bulk(client, [a["id"], b["id"], 999], "on")
    assert [(r["name"], r["ok"]) for r in res] == [("pc01", True), ("pc02", False), (None, False)]
    assert "no MAC" in res[1]["error"] and "does not exist" in res[2]["error"]
    assert wol == ["aa:bb:cc:dd:ee:01"]


def test_shutdown_only_reaches_online_pcs(client):
    a, b = _machines(client, "pc01", "pc02")
    _beat(client, "pc01")
    res = _bulk(client, [a["id"], b["id"]], "shutdown")
    assert [r["ok"] for r in res] == [True, False] and "offline" in res[1]["error"]
    assert client.get(f"/api/v1/machines/{a['id']}").json()["pending_command"] == "shutdown"


def test_assign_and_remove_a_disk_on_several(client, host, disk):
    ms = _machines(client, "pc01", "pc02", "pc03")
    res = _bulk(client, [m["id"] for m in ms], "assign", game_image_id=disk["id"])
    assert all(r["ok"] for r in res)
    assert {f"tank/ggnet/writebacks/pc0{i}" for i in (1, 2, 3)} <= set(host.datasets)

    res = _bulk(client, [m["id"] for m in ms], "assign", game_image_id=None)
    assert all(r["ok"] for r in res)
    assert not any(d.startswith("tank/ggnet/writebacks/pc") for d in host.datasets)


def test_one_host_failure_does_not_stop_the_others(client, host, disk):
    ms = _machines(client, "pc01", "pc02")
    host.fail_on[("zfs", "clone", "-o", "sync=disabled", "tank/ggnet/images/cs2@base",
                  "tank/ggnet/writebacks/pc01")] = "out of space"
    res = _bulk(client, [m["id"] for m in ms], "assign", game_image_id=disk["id"])
    assert [r["ok"] for r in res] == [False, True] and "out of space" in res[0]["error"]
    assert client.get(f"/api/v1/machines/{ms[0]['id']}").json()["status"] == "error"
    assert client.get(f"/api/v1/machines/{ms[1]['id']}").json()["status"] == "provisioned"


def test_pin_drive_letter_and_stop_keeping(client, disk):
    ms = _machines(client, "pc01", "pc02", game_image_id=disk["id"])
    ids = [m["id"] for m in ms]
    assert all(r["ok"] for r in _bulk(client, ids, "pin", snapshot="base"))
    assert all(r["ok"] for r in _bulk(client, ids, "drive_letter", drive_letter="g"))
    client.put(f"/api/v1/machines/{ids[0]}/keep-writeback", json={"enabled": True})
    assert all(r["ok"] for r in _bulk(client, ids, "stop_keep_writeback"))
    got = [client.get(f"/api/v1/machines/{i}").json() for i in ids]
    assert [(g["pinned_snapshot"], g["drive_letter"], g["keep_writeback"]) for g in got] == [("base", "G", False)] * 2

    res = _bulk(client, ids, "pin", snapshot="v9")
    assert not any(r["ok"] for r in res)


@pytest.mark.parametrize("body", [
    {"machine_ids": [], "action": "on"},
    {"machine_ids": [1], "action": "format"},
    {"machine_ids": [1], "action": "drive_letter"},
    {"machine_ids": [1], "action": "drive_letter", "drive_letter": "C"},
])
def test_bulk_validation(client, body):
    assert client.post("/api/v1/machines/bulk", json=body).status_code == 422


def test_each_machine_once(client, wol):
    (a,) = _machines(client, "pc01", mac="aa:bb:cc:dd:ee:01")
    assert len(_bulk(client, [a["id"], a["id"]], "on")) == 1 and wol == ["aa:bb:cc:dd:ee:01"]
