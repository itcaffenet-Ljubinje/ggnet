"""API v1 agent heartbeat, through TestClient and FakeHost."""

from __future__ import annotations

import pytest

TARGET = "iqn.2025-05.net.ggnet:storage"
BEAT = {"name": "PC01", "agent_version": "0.1.0",
        "initiator_iqn": "iqn.1991-05.com.microsoft:pc01", "iscsi_connected": False}


def _provisioned_machine(client) -> dict:
    d = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10}).json()
    client.post(f"/api/v1/game-disks/{d['id']}/publish")
    r = client.post("/api/v1/machines", json={"name": "pc01", "game_disk_id": d["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def test_heartbeat_returns_target_and_portal(client, host):
    m = _provisioned_machine(client)
    host.calls.clear()
    r = client.post("/api/v1/agent/heartbeat", json=BEAT)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "machine_id": m["id"],
        "name": "pc01",
        "mode": "disk",
        "status": "provisioned",
        "initiator_iqn": "iqn.1991-05.com.microsoft:pc01",
        "iscsi_target_iqn": TARGET,
        "portal_ip": "192.168.10.1",
        "portal_port": 3260,
        "game_disk": "cs2",
        "drive_letter": "D",
    }
    assert host.calls == []   # a heartbeat never touches the host


def test_heartbeat_records_agent_state_without_bumping_updated_at(client):
    m = _provisioned_machine(client)
    beat = {**BEAT, "initiator_iqn": "IQN.1991-05.com.microsoft:pc01.cafe.local",
            "iscsi_connected": True}
    assert client.post("/api/v1/agent/heartbeat", json=beat).status_code == 200

    after = client.get(f"/api/v1/machines/{m['id']}").json()
    assert after["last_seen_at"] is not None
    assert after["agent_version"] == "0.1.0"
    assert after["reported_iqn"] == "iqn.1991-05.com.microsoft:pc01.cafe.local"
    assert after["iscsi_connected"] is True
    assert after["updated_at"] == m["updated_at"]


def test_heartbeat_without_disk_has_no_target(client):
    client.post("/api/v1/machines", json={"name": "pc01"})
    r = client.post("/api/v1/agent/heartbeat", json=BEAT)
    assert r.status_code == 200
    assert r.json()["status"] == "idle"
    assert r.json()["iscsi_target_iqn"] is None
    assert r.json()["game_disk"] is None


def test_heartbeat_hides_target_while_in_error(client, host):
    m = _provisioned_machine(client)
    host.fail_on[("zfs", "destroy")] = "dataset is busy"
    assert client.post(f"/api/v1/machines/{m['id']}/reset").status_code == 502
    r = client.post("/api/v1/agent/heartbeat", json=BEAT)
    assert r.json()["status"] == "error"
    assert r.json()["iscsi_target_iqn"] is None


def test_heartbeat_unknown_machine(client):
    r = client.post("/api/v1/agent/heartbeat", json=BEAT)
    assert r.status_code == 404
    assert "pc01" in r.json()["detail"]["error"]


@pytest.mark.parametrize("body", [
    {**BEAT, "name": "pc 01"},
    {**BEAT, "name": "a-very-long-pc-name"},
    {**BEAT, "agent_version": "x" * 33},
    {**BEAT, "initiator_iqn": "not-an-iqn"},
    {k: v for k, v in BEAT.items() if k != "agent_version"},
])
def test_heartbeat_validation(client, body):
    client.post("/api/v1/machines", json={"name": "pc01"})
    assert client.post("/api/v1/agent/heartbeat", json=body).status_code == 422


def test_new_machine_has_no_agent_state(client):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    assert (m["last_seen_at"], m["agent_version"], m["reported_iqn"], m["iscsi_connected"]) == (
        None, None, None, None)


# ── Game disk drive letter ────────────────────────────────────────────

def test_heartbeat_sends_the_machine_letter_and_records_the_one_used(client):
    m = _provisioned_machine(client)
    assert client.patch(f"/api/v1/machines/{m['id']}", json={"drive_letter": "g:"}).json()["drive_letter"] == "G"

    r = client.post("/api/v1/agent/heartbeat", json={
        "name": "pc01", "agent_version": "0.1.2", "iscsi_connected": True, "drive_letter": "h",
    })
    assert r.json()["drive_letter"] == "G"
    # The PC had G: taken, so the disk got H:; the UI shows the difference.
    assert client.get(f"/api/v1/machines/{m['id']}").json()["reported_drive_letter"] == "H"

    client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.2"})
    assert client.get(f"/api/v1/machines/{m['id']}").json()["reported_drive_letter"] is None


@pytest.mark.parametrize("letter", ["C", "A", "DD", "1", ""])
def test_drive_letter_validation(client, letter):
    r = client.post("/api/v1/machines", json={"name": "pc01", "drive_letter": letter})
    assert r.status_code == 422


def test_set_drive_letter_for_all_disk_mode_machines(client, host):
    client.post("/api/v1/machines", json={"name": "pc01"})
    client.post("/api/v1/machines", json={"name": "pc02", "drive_letter": "E"})
    client.post("/api/v1/machines", json={"name": "boot01", "mode": "boot"})

    r = client.put("/api/v1/machines/drive-letter", json={"drive_letter": "F"})
    assert r.status_code == 200
    assert {m["name"]: m["drive_letter"] for m in r.json()} == {"boot01": "D", "pc01": "F", "pc02": "F"}
    assert host.calls == []   # only the agents act on it


def test_boot_mode_game_disk_is_always_d(client):
    r = client.post("/api/v1/machines", json={"name": "boot01", "mode": "boot", "drive_letter": "G"})
    assert r.status_code == 422 and "always get the game disk as D:" in r.text

    m = client.post("/api/v1/machines", json={"name": "pc01", "drive_letter": "G"}).json()
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"mode": "boot"})
    assert r.status_code == 200 and r.json()["drive_letter"] == "D"
    r = client.patch(f"/api/v1/machines/{m['id']}", json={"drive_letter": "E"})
    assert r.status_code == 409


# ── Inventory (IP, link speed, hardware) ──────────────────────────────

INVENTORY = {
    "ip_address": "192.168.0.21", "mac_address": "AA-BB-CC-DD-EE-01", "link_speed_mbps": 1000,
    "nic": "Intel(R) Ethernet I219-V", "cpu": "AMD Ryzen 5 5600X", "gpus": ["NVIDIA GeForce RTX 3060"],
    "motherboard": "ASUSTeK TUF GAMING B550-PLUS", "memory_bytes": 17179869184,
}


def test_heartbeat_stores_the_inventory(client):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    r = client.post("/api/v1/agent/heartbeat",
                    json={"name": "pc01", "agent_version": "0.1.6", "inventory": INVENTORY})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (got["reported_ip"], got["reported_mac"], got["link_speed_mbps"]) == (
        "192.168.0.21", "aa:bb:cc:dd:ee:01", 1000)
    assert got["hardware"] == {
        "nic": "Intel(R) Ethernet I219-V", "cpu": "AMD Ryzen 5 5600X", "gpus": ["NVIDIA GeForce RTX 3060"],
        "motherboard": "ASUSTeK TUF GAMING B550-PLUS", "memory_bytes": 17179869184,
    }

    # An agent without inventory (older version) keeps the last one.
    client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.5"})
    assert client.get(f"/api/v1/machines/{m['id']}").json()["reported_ip"] == "192.168.0.21"


@pytest.mark.parametrize("bad", [
    {"ip_address": "not-an-ip"},
    {"mac_address": "zz"},
    {"link_speed_mbps": -5},
    {"gpus": ["x"] * 20},
    {"cpu": "x" * 500},
])
def test_a_bad_inventory_never_fails_the_heartbeat(client, bad):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    r = client.post("/api/v1/agent/heartbeat",
                    json={"name": "pc01", "agent_version": "0.1.6", "inventory": {**INVENTORY, **bad}})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (got["reported_ip"], got["hardware"], got["last_seen_at"] is not None) == (None, None, True)
