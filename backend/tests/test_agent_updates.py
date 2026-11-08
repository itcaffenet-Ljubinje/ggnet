"""Automatic agent updates: the heartbeat offers the server's package to older Disk Mode agents."""

from __future__ import annotations

import hashlib

import pytest

from app.api.deps import get_agent_package, get_server_url
from app.main import app
from app.services.agent_package import AgentPackage
from app.services.agent_updates import is_older

CONTENT = {"ggnet-agent.exe": b"MZ" + b"\x00" * 100, "install.ps1": b"param()\n", "uninstall.ps1": b"# remove\n"}
BEAT = {"name": "pc01", "agent_version": "0.1.20",
        "initiator_iqn": "iqn.1991-05.com.microsoft:pc01", "iscsi_connected": False}


@pytest.fixture
def package(tmp_path, client):
    d = tmp_path / "agent"
    d.mkdir()
    for name, data in CONTENT.items():
        (d / name).write_bytes(data)
    (d / "VERSION").write_text("0.1.21\n")
    app.dependency_overrides[get_agent_package] = lambda: AgentPackage(d)
    app.dependency_overrides[get_server_url] = lambda: "http://192.168.0.40:8088"
    yield d
    app.dependency_overrides.pop(get_agent_package, None)
    app.dependency_overrides.pop(get_server_url, None)


def _machine(client, mode="disk"):
    r = client.post("/api/v1/machines", json={"name": "pc01", "mode": mode})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("agent, package, older", [
    ("0.1.20", "0.1.21", True),
    ("0.1.21", "0.1.21", False),
    ("0.1.22", "0.1.21", False),     # never a downgrade
    ("0.2.0-rc1", "0.2.0", True),
    ("0.2.0", "0.2.0-rc1", False),
    ("0.1.9", "0.1.10", True),       # numbers, not text
    ("dev", "0.1.21", False),        # an unreadable version is never replaced
    ("0.1.20", None, False),
    (None, "0.1.21", False),
])
def test_is_older(agent, package, older):
    assert is_older(agent, package) is older


def test_an_older_disk_mode_agent_is_offered_the_package(client, package):
    _machine(client)
    got = client.post("/api/v1/agent/heartbeat", json=BEAT).json()["update"]
    assert got == {"version": "0.1.21", "files": [
        {"name": n, "size_bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()} for n, d in CONTENT.items()]}


def test_a_current_agent_is_not(client, package):
    _machine(client)
    assert client.post("/api/v1/agent/heartbeat", json={**BEAT, "agent_version": "0.1.21"}).json()["update"] is None


def test_boot_mode_agents_update_with_their_image(client, package):
    _machine(client, mode="boot")
    assert client.post("/api/v1/agent/heartbeat", json=BEAT).json()["update"] is None


def test_not_while_the_agent_stops(client, package):
    _machine(client)
    assert client.post("/api/v1/agent/heartbeat", json={**BEAT, "stopping": True}).json()["update"] is None


def test_not_without_a_complete_package(client, package):
    _machine(client)
    (package / "install.ps1").unlink()
    assert client.post("/api/v1/agent/heartbeat", json=BEAT).json()["update"] is None


def test_turned_off_in_settings(client, package):
    _machine(client)
    assert client.get("/api/v1/settings/agent").json() == {"auto_update": True}
    assert client.put("/api/v1/settings/agent", json={"auto_update": False}).json() == {"auto_update": False}
    assert client.post("/api/v1/agent/heartbeat", json=BEAT).json()["update"] is None
    assert client.get("/api/v1/agent/package").json()["auto_update"] is False


def test_package_info_lists_outdated_disk_mode_pcs(client, package):
    _machine(client)
    client.post("/api/v1/agent/heartbeat", json=BEAT)
    client.post("/api/v1/machines", json={"name": "pc02", "mode": "boot"})
    got = client.get("/api/v1/agent/package").json()
    assert (got["auto_update"], got["outdated"]) == (True, ["pc01"])
