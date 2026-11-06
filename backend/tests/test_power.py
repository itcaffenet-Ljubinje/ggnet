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


def test_goodbye_heartbeat_marks_the_pc_offline_at_once(client):
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    _beat(client)
    assert client.get(f"/api/v1/machines/{m['id']}").json()["agent_stopped"] is False
    client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "reboot"})

    r = client.post("/api/v1/agent/heartbeat",
                    json={"name": "pc01", "agent_version": "0.1.8", "stopping": True})
    assert r.status_code == 200 and r.json()["command"] is None        # not handed out while stopping
    got = client.get(f"/api/v1/machines/{m['id']}").json()
    assert (got["agent_stopped"], got["pending_command"]) == (True, "reboot")
    r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "shutdown"})
    assert r.status_code == 409 and "offline" in r.text

    # The next start beats normally again.
    assert _beat(client)["command"] == "reboot"
    assert client.get(f"/api/v1/machines/{m['id']}").json()["agent_stopped"] is False


def test_session_drop_after_the_last_heartbeat_means_offline(client, host, prov, db_url):
    from sqlalchemy.orm import sessionmaker

    from app.db.session import make_engine
    from app.services import writebacks
    from app.services.writebacks import WritebackSettings

    d = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10}).json()
    client.post(f"/api/v1/images/{d['id']}/publish")
    m = client.post("/api/v1/machines", json={"name": "pc01", "game_image_id": d["id"]}).json()
    engine = make_engine(db_url)
    make_session = sessionmaker(bind=engine, expire_on_commit=False)
    watch = WritebackSettings(auto_discard=False)
    iqn = "iqn.1991-05.com.microsoft:pc01"
    try:
        host.sessions.add(iqn)
        writebacks.tick(make_session, prov, watch)
        _beat(client)
        assert client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "reboot"}).status_code == 200
        _beat(client)                                         # takes the reboot

        # Windows shuts down from the Start menu: no goodbye, but the session drops.
        host.sessions.discard(iqn)
        writebacks.tick(make_session, prov, watch)
        r = client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "shutdown"})
        assert r.status_code == 409 and "offline" in r.text

        # Back up: the next heartbeat is newer than the drop.
        _beat(client)
        assert client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "shutdown"}).status_code == 200
    finally:
        engine.dispose()
