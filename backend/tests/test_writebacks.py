"""Automatic writeback discard, Keep Writeback and Apply Writebacks (FakeHost)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_writeback_settings
from app.db.models import Machine
from app.db.session import make_engine
from app.main import app
from app.runner import CommandRunner
from app.services import writebacks
from app.services.provisioning import next_version
from app.services.writebacks import WritebackSettings

MASTER = "tank/ggnet/images/cs2"
SETTINGS = WritebackSettings(auto_discard=True, grace=timedelta(seconds=30))
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _iqn(name: str) -> str:
    return f"iqn.1991-05.com.microsoft:{name}"


@pytest.fixture
def make_session(db_url):
    engine = make_engine(db_url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def disk(client) -> dict:
    d = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10}).json()
    return client.post(f"/api/v1/game-disks/{d['id']}/publish").json()


def _machine(client, disk, name="pc01") -> dict:
    r = client.post("/api/v1/machines", json={"name": name, "game_disk_id": disk["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def _get(client, m) -> dict:
    return client.get(f"/api/v1/machines/{m['id']}").json()


def _tick(make_session, prov, at):
    writebacks.tick(make_session, prov, SETTINGS, now=at)


def _clones_made(host, name="pc01") -> int:
    return sum(1 for c in host.calls if c[:2] == ["zfs", "clone"] and c[-1].endswith(f"/{name}"))


# ── next_version ──────────────────────────────────────────────────────

@pytest.mark.parametrize("snaps, expected", [
    ([], "v1"),
    (["base"], "v2"),
    (["base", "v2", "v3"], "v4"),
    (["base", "v7", "ggnet-apply", "manual"], "v8"),
])
def test_next_version(snaps, expected):
    assert next_version(snaps) == expected


# ── Session state from LIO ────────────────────────────────────────────

def test_session_state_from_configfs(client, host, prov, disk):
    _machine(client, disk)
    assert prov.session_active(_iqn("pc01")) is False
    host.sessions.add(_iqn("pc01"))
    assert prov.session_active(_iqn("pc01")) is True
    # No ACL for this initiator: unknown, never "disconnected".
    assert prov.session_active(_iqn("pc99")) is None


# ── Watcher ───────────────────────────────────────────────────────────

def test_discard_after_disconnect_and_grace(client, host, prov, disk, make_session):
    m = _machine(client, disk)
    host.sessions.add(_iqn("pc01"))
    _tick(make_session, prov, T0)
    assert _get(client, m)["writeback_dirty"] is True

    host.sessions.discard(_iqn("pc01"))
    host.calls.clear()
    _tick(make_session, prov, T0 + timedelta(seconds=5))           # blip: within grace
    assert _clones_made(host) == 0
    _tick(make_session, prov, T0 + timedelta(seconds=40))
    assert _clones_made(host) == 1
    after = _get(client, m)
    assert (after["writeback_dirty"], after["status"]) == (False, "provisioned")
    assert host.visible(_iqn("pc01")) == ["/dev/zvol/tank/ggnet/writebacks/pc01"]

    host.calls.clear()
    _tick(make_session, prov, T0 + timedelta(minutes=10))         # clean: left alone
    assert _clones_made(host) == 0


def test_reconnect_within_grace_keeps_writeback(client, host, prov, disk, make_session):
    _machine(client, disk)
    host.sessions.add(_iqn("pc01"))
    _tick(make_session, prov, T0)
    host.sessions.discard(_iqn("pc01"))
    _tick(make_session, prov, T0 + timedelta(seconds=5))
    host.sessions.add(_iqn("pc01"))
    host.calls.clear()
    _tick(make_session, prov, T0 + timedelta(seconds=60))
    assert _clones_made(host) == 0


def test_keep_writeback_is_never_discarded(client, host, prov, disk, make_session):
    m = _machine(client, disk)
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    host.sessions.add(_iqn("pc01"))
    _tick(make_session, prov, T0)
    host.sessions.discard(_iqn("pc01"))
    host.calls.clear()
    _tick(make_session, prov, T0 + timedelta(hours=5))
    _tick(make_session, prov, T0 + timedelta(hours=6))
    assert _clones_made(host) == 0


def test_unknown_session_never_discards(client, host, prov, disk, make_session):
    _machine(client, disk)
    host.fail_on[("cat",)] = "Permission denied"
    host.calls.clear()
    _tick(make_session, prov, T0)
    _tick(make_session, prov, T0 + timedelta(hours=1))
    assert _clones_made(host) == 0


def test_discard_failure_marks_error_and_others_continue(client, host, prov, disk, make_session):
    m1, m2 = _machine(client, disk, "pc01"), _machine(client, disk, "pc02")
    for n in ("pc01", "pc02"):
        host.sessions.add(_iqn(n))
    _tick(make_session, prov, T0)
    host.sessions.clear()
    _tick(make_session, prov, T0 + timedelta(seconds=1))
    host.fail_on[("targetcli", "/backstores/block", "delete", "pc01-game")] = "in use"
    _tick(make_session, prov, T0 + timedelta(seconds=40))
    assert _get(client, m1)["status"] == "error"
    assert "Automatic writeback discard failed" in _get(client, m1)["last_error"]
    assert _get(client, m2)["writeback_dirty"] is False


def test_legacy_target_machine_is_watched_and_migrated(client, host, prov, disk, make_session):
    """A machine still on its old per-client target is read there and moved."""
    m = _machine(client, disk)
    legacy = "iqn.2025-05.net.ggnet:client-pc01"
    with make_session() as db:
        row = db.get(Machine, m["id"])
        row.iscsi_target_iqn = legacy
        db.commit()
    host.targets[legacy] = {"luns": {}, "acls": {_iqn("pc01"): {}}, "portals": set(), "attrs": {}}
    host.sessions.add(_iqn("pc01"))
    _tick(make_session, prov, T0)
    host.sessions.clear()
    _tick(make_session, prov, T0 + timedelta(seconds=1))
    _tick(make_session, prov, T0 + timedelta(seconds=40))
    assert legacy not in host.targets
    assert _get(client, m)["iscsi_target_iqn"] == "iqn.2025-05.net.ggnet:storage"


# ── Heartbeat: reboot detection ───────────────────────────────────────

@pytest.fixture
def auto(client):
    app.dependency_overrides[get_writeback_settings] = lambda: SETTINGS


def _beat(client, booted_at, connected=False):
    r = client.post("/api/v1/agent/heartbeat", json={
        "name": "pc01", "agent_version": "0.2.0", "initiator_iqn": _iqn("pc01"),
        "iscsi_connected": connected, "booted_at": booted_at.isoformat(),
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_reboot_discards_before_agent_connects(client, host, disk, auto):
    m = _machine(client, disk)
    _beat(client, T0, connected=True)                 # boot 1, in use → dirty
    host.calls.clear()
    _beat(client, T0 + timedelta(seconds=30), connected=True)   # same boot
    assert _clones_made(host) == 0

    _beat(client, T0 + timedelta(minutes=20))          # boot 2, not connected yet
    assert _clones_made(host) == 1
    assert _get(client, m)["writeback_dirty"] is False


def test_reboot_with_live_session_is_not_discarded(client, host, disk, auto):
    """Agent service restarted inside a running Windows: the disk is in use."""
    _machine(client, disk)
    _beat(client, T0, connected=True)
    host.sessions.add(_iqn("pc01"))
    host.calls.clear()
    _beat(client, T0 + timedelta(minutes=20))
    assert _clones_made(host) == 0


def test_heartbeat_does_not_discard_when_auto_discard_is_off(client, host, disk):
    _machine(client, disk)
    _beat(client, T0, connected=True)
    host.calls.clear()
    _beat(client, T0 + timedelta(minutes=20))
    assert _clones_made(host) == 0


# ── Keep Writeback ────────────────────────────────────────────────────

def test_keep_writeback_sets_sync_and_is_exclusive(client, host, disk):
    m1, m2 = _machine(client, disk, "pc01"), _machine(client, disk, "pc02")
    r = client.put(f"/api/v1/machines/{m1['id']}/keep-writeback", json={"enabled": True})
    assert r.status_code == 200 and r.json()["keep_writeback"] is True
    assert host.datasets["tank/ggnet/writebacks/pc01"]["props"]["sync"] == "standard"

    r = client.put(f"/api/v1/machines/{m2['id']}/keep-writeback", json={"enabled": True})
    assert r.status_code == 409 and "pc01" in r.json()["detail"]["error"]

    client.put(f"/api/v1/machines/{m1['id']}/keep-writeback", json={"enabled": False})
    assert host.datasets["tank/ggnet/writebacks/pc01"]["props"]["sync"] == "disabled"
    r = client.put(f"/api/v1/machines/{m2['id']}/keep-writeback", json={"enabled": True})
    assert r.status_code == 200


# ── Apply Writebacks ──────────────────────────────────────────────────

def _keeper(client, disk, name="pc01") -> dict:
    m = _machine(client, disk, name)
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    return m


def test_apply_writebacks_creates_next_version(client, host, disk):
    m = _keeper(client, disk)
    other = _machine(client, disk, "pc02")

    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 200, r.text
    assert r.json()["clone_snapshot"] == f"{MASTER}@v2"
    assert host.snapshots[f"{MASTER}@v2"] == {"ggnet:protected"}
    assert f"{MASTER}@base" in host.snapshots                      # old version kept
    assert "tank/ggnet/writebacks/pc01@ggnet-apply" not in host.snapshots
    assert host.datasets["tank/ggnet/writebacks/pc01"]["origin"] == f"{MASTER}@v2"
    assert host.datasets["tank/ggnet/writebacks/pc01"]["props"]["sync"] == "standard"
    assert client.get(f"/api/v1/game-disks/{disk['id']}").json()["snapshot"] == "v2"
    assert _get(client, other)["outdated"] is True                 # moves on next reboot

    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.json()["clone_snapshot"] == f"{MASTER}@v3"


def test_outdated_machine_moves_to_new_version_after_disconnect(
        client, host, prov, disk, make_session):
    m = _keeper(client, disk)
    other = _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    _tick(make_session, prov, T0)
    _tick(make_session, prov, T0 + timedelta(seconds=40))
    assert _get(client, other)["clone_snapshot"] == f"{MASTER}@v2"
    assert _get(client, other)["outdated"] is False


def test_apply_requires_keep_writeback(client, disk):
    m = _machine(client, disk)
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 409


def test_apply_refused_while_connected(client, host, disk):
    m = _keeper(client, disk)
    host.sessions.add(_iqn("pc01"))
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 409 and "shut it down" in r.json()["detail"]["error"]
    assert f"{MASTER}@v2" not in host.snapshots


def test_apply_refused_when_master_moved_on(client, host, disk):
    """Another version appeared on the master after the clone was made."""
    m = _keeper(client, disk)
    host.snapshots[f"{MASTER}@manual"] = set()
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 502 and "not the newest version" in r.json()["detail"]["error"]
    assert "tank/ggnet/writebacks/pc01@ggnet-apply" not in host.snapshots


def test_apply_send_failure_cleans_up(client, host, disk):
    m = _keeper(client, disk)
    host.fail_on[("zfs", "recv")] = "out of space"
    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 502 and "out of space" in r.json()["detail"]["error"]
    assert "tank/ggnet/writebacks/pc01@ggnet-apply" not in host.snapshots
    assert client.get(f"/api/v1/game-disks/{disk['id']}").json()["snapshot"] == "base"


# ── Runner pipe (real processes, no zfs) ──────────────────────────────

def test_run_pipe():
    r = CommandRunner(timeout=10)
    assert r.run_pipe(["printf", "abc"], ["tr", "a-z", "A-Z"]) == (True, "ABC", "")
    ok, _, err = r.run_pipe(["false"], ["cat"])
    assert not ok and err
    ok, _, err = r.run_pipe(["printf", "x"], ["sh", "-c", "cat >/dev/null; echo bad >&2; exit 3"])
    assert (ok, err) == (False, "bad")


# ── Editing a draft master ────────────────────────────────────────────

def test_watcher_tracks_editing_machine_but_never_discards(client, host, prov, make_session):
    d = client.post("/api/v1/game-disks", json={"name": "draft", "size_gb": 10}).json()
    m = client.post("/api/v1/machines", json={"name": "pc01"}).json()
    assert client.post(f"/api/v1/game-disks/{d['id']}/edit", json={"machine_id": m["id"]}).status_code == 200
    calls = len(host.calls)

    host.sessions.add(_iqn("pc01"))
    _tick(make_session, prov, T0)
    assert _get(client, m)["session_active"] is True

    host.sessions.discard(_iqn("pc01"))
    _tick(make_session, prov, T0 + timedelta(minutes=5))
    got = _get(client, m)
    assert (got["session_active"], got["status"]) == (False, "editing")
    # Only session reads; the draft stays mapped and nothing is cloned.
    assert all(c[0] == "cat" for c in host.calls[calls:])
    assert host.visible(_iqn("pc01")) == ["/dev/zvol/tank/ggnet/images/draft"]
