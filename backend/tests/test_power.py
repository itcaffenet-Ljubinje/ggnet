"""Turn On (Wake-on-LAN), Shutdown and Reboot through the agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.deps import get_wol_sender
from app.db.models import Machine
from app.main import app
from app.services.power import POWER_COMMAND_TTL, broadcast_address, magic_packet, take_command


@pytest.fixture
def wol():
    sent: list[str] = []
    app.dependency_overrides[get_wol_sender] = lambda: sent.append
    yield sent
    app.dependency_overrides.pop(get_wol_sender, None)


def _beat(client, name="pc01"):
    r = client.post("/api/v1/agent/heartbeat", json={"name": name, "agent_version": "0.1.7"})
    assert r.status_code == 200, r.text
    return r.json()


def test_magic_packet_and_broadcast():
    p = magic_packet("aa:bb:cc:dd:ee:01")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("aabbccddee01")
    assert p[6:12] * 16 == p[6:]
    assert broadcast_address("192.168.0.0/24") == "192.168.0.255"
    assert broadcast_address("192.168.0.40/24") == "192.168.0.255"
    with pytest.raises(ValueError):
        magic_packet("aa:bb")


def test_turn_on_uses_the_set_or_the_reported_mac(client, wol):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"})
    assert r.status_code == 409 and "no MAC" in r.text
    assert wol == []

    client.post("/api/v1/agent/heartbeat", json={
        "name": "pc01", "agent_version": "0.1.7", "inventory": {"mac_address": "AA-BB-CC-DD-EE-01"}})
    assert client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"}).status_code == 200
    client.patch(f"/api/v1/machines/{m['id']}", json={"mac": "aa:bb:cc:dd:ee:02"})
    client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"})
    assert wol == ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]


def test_turn_on_socket_error_is_502(client):
    def broken(_mac):
        raise OSError("Network is unreachable")
    app.dependency_overrides[get_wol_sender] = lambda: broken
    try:
        m = client.post("/api/v1/machines", json={"name": "pc01", "mac": "aa:bb:cc:dd:ee:01"}).json()
        r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"})
        assert r.status_code == 502 and "unreachable" in r.text
    finally:
        app.dependency_overrides.pop(get_wol_sender, None)


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_command_reaches_the_agent_once(client, action):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": action})
    assert r.status_code == 409 and "offline" in r.text          # no agent yet

    _beat(client)
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": action})
    assert r.status_code == 200 and r.json()["pending_command"] == action
    assert _beat(client)["command"] == action
    assert _beat(client)["command"] is None                       # at most once
    assert client.get(f"/api/v1/machines/{m['id']}").json()["pending_command"] is None


def test_power_validation(client):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    assert client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "format"}).status_code == 422


def test_stale_command_is_dropped():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    m = Machine(name="pc01", initiator_iqn="iqn.x:pc01", pending_command="shutdown",
                pending_command_at=now - POWER_COMMAND_TTL - timedelta(seconds=1))
    assert take_command(m, now) is None and m.pending_command is None
    m.pending_command, m.pending_command_at = "reboot", now - timedelta(seconds=30)
    assert take_command(m, now) == "reboot" and m.pending_command is None
