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
    # The kept writeback itself became the master: read-only, no writeback sync,
    # same name; the old master head is gone and pc02 still runs on @base.
    assert host.datasets[MASTER]["readonly"] is True and host.datasets[MASTER]["origin"] is None
    assert "sync" not in host.datasets[MASTER].get("props", {})
    assert f"{MASTER}_ggnet_old" not in host.datasets
    assert host.snapshots[f"{MASTER}@base"] == {"ggnet:protected"}
    assert host.datasets["tank/ggnet/writebacks/pc02"]["origin"] == f"{MASTER}@base"
    assert host.visible(_iqn("pc01")) == ["/dev/zvol/tank/ggnet/writebacks/pc01"]

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


@pytest.mark.parametrize("step", [
    ("zfs", "snapshot"),
    ("zfs", "promote", "tank/ggnet/writebacks/pc01"),
    ("zfs", "rename", MASTER),
    ("zfs", "rename", "tank/ggnet/writebacks/pc01"),
])
def test_apply_failure_rolls_back_and_maps_the_clone_again(client, host, disk, step):
    m = _keeper(client, disk)
    other = _machine(client, disk, "pc02")
    before_snaps = dict(host.snapshots)
    host.fail_on[step] = "boom"

    r = client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    assert r.status_code == 502 and "boom" in r.json()["detail"]["error"]
    assert host.snapshots == before_snaps                       # no @v2, holds intact
    assert host.datasets[MASTER]["origin"] is None              # still the master
    assert host.datasets[MASTER]["readonly"] is True
    clone = host.datasets["tank/ggnet/writebacks/pc01"]
    assert (clone["origin"], clone["readonly"]) == (f"{MASTER}@base", False)
    assert f"{MASTER}_ggnet_old" not in host.datasets
    assert host.visible(_iqn("pc01")) == ["/dev/zvol/tank/ggnet/writebacks/pc01"]   # PC keeps its disk
    assert host.visible(_iqn("pc02")) == ["/dev/zvol/tank/ggnet/writebacks/pc02"]
    assert client.get(f"/api/v1/game-disks/{disk['id']}").json()["snapshot"] == "base"
    assert _get(client, other)["outdated"] is False


def test_apply_does_not_use_send_recv(client, host, disk):
    """A clone stream cannot be received into the existing master (real host error)."""
    m = _keeper(client, disk)
    assert client.post(f"/api/v1/machines/{m['id']}/apply-writebacks").status_code == 200
    assert not any("|" in c for c in host.calls)


# ── Runner pipe (real processes, no zfs) ──────────────────────────────

def test_run_pipe():
    r = CommandRunner(timeout=10)
    assert r.run_pipe(["printf", "abc"], ["tr", "a-z", "A-Z"]) == (True, "ABC", "")
    ok, _, err = r.run_pipe(["false"], ["cat"])
    assert not ok and err
    ok, _, err = r.run_pipe(["printf", "x"], ["sh", "-c", "cat >/dev/null; echo bad >&2; exit 3"])
    assert (ok, err) == (False, "bad")


def test_run_pipe_reports_the_consumer_error_when_the_producer_got_sigpipe():
    # Real host: zfs recv refused the stream at once, zfs send died of SIGPIPE
    # and the error was only "exit code -13/1".
    r = CommandRunner(timeout=10)
    ok, _, err = r.run_pipe(["yes"], ["sh", "-c", "echo 'cannot receive: refused' >&2; exit 1"])
    assert (ok, err) == (False, "cannot receive: refused")


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


# ── Images: versions and writebacks ───────────────────────────────────

def _versions(client, disk) -> list[dict]:
    r = client.get(f"/api/v1/game-disks/{disk['id']}/snapshots")
    assert r.status_code == 200, r.text
    return r.json()


def test_snapshots_list_versions_with_sizes_and_users(client, host, disk):
    m = _keeper(client, disk)
    _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    host.sizes[f"{MASTER}@base"] = (4096, 1 << 30)

    v = _versions(client, disk)
    assert [(s["name"], s["active"], s["machines"]) for s in v] == [
        ("base", False, ["pc02"]),
        ("v2", True, ["pc01"]),
    ]
    assert (v[0]["used_bytes"], v[0]["referenced_bytes"]) == (4096, 1 << 30)
    assert v[0]["created_at"] < v[1]["created_at"]


def test_draft_disk_has_no_versions(client):
    d = client.post("/api/v1/game-disks", json={"name": "draft", "size_gb": 10}).json()
    assert client.get(f"/api/v1/game-disks/{d['id']}/snapshots").json() == []


def test_make_an_older_version_active(client, host, disk):
    m = _keeper(client, disk)
    other = _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    url = f"/api/v1/game-disks/{disk['id']}/active-snapshot"

    r = client.put(url, json={"snapshot": "base"})
    assert r.status_code == 200 and r.json()["snapshot"] == "base"
    assert _get(client, other)["outdated"] is False      # pc02 is on @base already
    assert _get(client, m)["outdated"] is True           # pc01 keeps @v2 (Keep Writeback)
    assert host.snapshots[f"{MASTER}@v2"] == {"ggnet:protected"}   # nothing deleted
    assert client.put(url, json={"snapshot": "v9"}).status_code == 404


def test_delete_a_version(client, host, disk):
    m = _keeper(client, disk)
    other = _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    url = f"/api/v1/game-disks/{disk['id']}/snapshots"

    r = client.delete(f"{url}/v2")
    assert r.status_code == 409 and "active version" in r.text
    r = client.delete(f"{url}/base")
    assert r.status_code == 409 and "pc02" in r.text     # pc02 still runs on it
    assert host.snapshots[f"{MASTER}@base"] == {"ggnet:protected"}
    assert client.delete(f"{url}/v7").status_code == 404

    client.post(f"/api/v1/machines/{other['id']}/discard-writeback")   # pc02 moves to @v2
    assert client.delete(f"{url}/base").status_code == 204
    assert f"{MASTER}@base" not in host.snapshots
    assert [s["name"] for s in _versions(client, disk)] == ["v2"]


def test_delete_version_failure_keeps_the_hold(client, host, disk):
    m = _keeper(client, disk)
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    host.fail_on[("zfs", "destroy", f"{MASTER}@base")] = "dataset is busy"
    r = client.delete(f"/api/v1/game-disks/{disk['id']}/snapshots/base")
    assert r.status_code == 502 and "dataset is busy" in r.text
    assert host.snapshots[f"{MASTER}@base"] == {"ggnet:protected"}


def test_writebacks_of_a_disk(client, host, disk):
    m = _keeper(client, disk)
    _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{m['id']}/apply-writebacks")
    host.sizes["tank/ggnet/writebacks/pc02"] = (5 << 20, 1 << 30)
    host.add_dataset("tank/ggnet/writebacks/stray", origin=f"{MASTER}@base")   # no machine record

    r = client.get(f"/api/v1/game-disks/{disk['id']}/writebacks")
    assert r.status_code == 200, r.text
    rows = {w["machine_name"]: w for w in r.json()}
    assert set(rows) == {"pc01", "pc02", "stray"}
    assert (rows["pc01"]["snapshot"], rows["pc01"]["keep_writeback"], rows["pc01"]["outdated"]) == ("v2", True, False)
    assert (rows["pc02"]["snapshot"], rows["pc02"]["used_bytes"], rows["pc02"]["outdated"]) == ("base", 5 << 20, True)
    assert rows["stray"]["machine_id"] is None


def test_discard_writeback_of_a_keeper(client, host, disk):
    m = _keeper(client, disk)
    host.sessions.add(_iqn("pc01"))
    url = f"/api/v1/machines/{m['id']}/discard-writeback"
    r = client.post(url)
    assert r.status_code == 409 and "shut it down" in r.text

    host.sessions.discard(_iqn("pc01"))
    clones = _clones_made(host)
    r = client.post(url)
    assert r.status_code == 200, r.text
    assert _clones_made(host) == clones + 1
    got = r.json()
    assert (got["keep_writeback"], got["writeback_dirty"], got["status"]) == (True, False, "provisioned")
    assert host.datasets["tank/ggnet/writebacks/pc01"]["props"]["sync"] == "standard"
    assert host.visible(_iqn("pc01")) == ["/dev/zvol/tank/ggnet/writebacks/pc01"]


def test_discard_writeback_needs_a_provisioned_machine(client):
    m = client.post("/api/v1/machines", json={"name": "pc09"}).json()
    assert client.post(f"/api/v1/machines/{m['id']}/discard-writeback").status_code == 409


# ── Snapshot pin per machine ──────────────────────────────────────────

def _two_versions(client, disk):
    """cs2@base and cs2@v2 (active); pc01 keeps its writeback and made v2, pc02 is on @base."""
    keeper = _keeper(client, disk)
    other = _machine(client, disk, "pc02")
    client.post(f"/api/v1/machines/{keeper['id']}/apply-writebacks")
    return keeper, other


def test_pin_keeps_a_machine_on_an_older_version(client, host, prov, disk, make_session):
    _, other = _two_versions(client, disk)
    r = client.put(f"/api/v1/machines/{other['id']}/pin", json={"snapshot": "base"})
    assert r.status_code == 200 and r.json()["pinned_snapshot"] == "base"
    assert r.json()["outdated"] is False              # on @base and pinned to it

    # After a reboot the pinned PC is cloned from @base, not the active @v2.
    _tick(make_session, prov, T0)
    _tick(make_session, prov, T0 + timedelta(seconds=40))
    assert host.datasets["tank/ggnet/writebacks/pc02"]["origin"] == f"{MASTER}@base"

    # Unpinned it follows the active version again.
    r = client.put(f"/api/v1/machines/{other['id']}/pin", json={"snapshot": None})
    assert r.json()["outdated"] is True
    clones = _clones_made(host, "pc02")
    client.post(f"/api/v1/machines/{other['id']}/discard-writeback")
    assert _clones_made(host, "pc02") == clones + 1
    assert host.datasets["tank/ggnet/writebacks/pc02"]["origin"] == f"{MASTER}@v2"


def test_pin_to_an_older_version_moves_the_pc_at_reboot(client, host, disk):
    keeper, _ = _two_versions(client, disk)
    client.put(f"/api/v1/machines/{keeper['id']}/keep-writeback", json={"enabled": False})
    r = client.put(f"/api/v1/machines/{keeper['id']}/pin", json={"snapshot": "base"})
    assert r.json()["outdated"] is True               # runs @v2, should run @base
    client.post(f"/api/v1/machines/{keeper['id']}/discard-writeback")
    assert host.datasets["tank/ggnet/writebacks/pc01"]["origin"] == f"{MASTER}@base"


def test_pin_validation(client, disk):
    m = _machine(client, disk)
    assert client.put(f"/api/v1/machines/{m['id']}/pin", json={"snapshot": "v9"}).status_code == 404
    bare = client.post("/api/v1/machines", json={"name": "pc09"}).json()
    assert client.put(f"/api/v1/machines/{bare['id']}/pin", json={"snapshot": "base"}).status_code == 409


def test_pinned_version_is_listed_and_cannot_be_deleted(client, host, disk):
    _, other = _two_versions(client, disk)
    client.post(f"/api/v1/machines/{other['id']}/discard-writeback")   # pc02 moves to @v2
    client.put(f"/api/v1/machines/{other['id']}/pin", json={"snapshot": "base"})

    base = _versions(client, disk)[0]
    assert (base["machines"], base["pinned"]) == ([], ["pc02"])
    r = client.delete(f"/api/v1/game-disks/{disk['id']}/snapshots/base")
    assert r.status_code == 409 and "pinned on pc02" in r.text
    assert f"{MASTER}@base" in host.snapshots


def test_switching_the_disk_or_applying_clears_the_pin(client, host, disk):
    keeper, other = _two_versions(client, disk)
    client.put(f"/api/v1/machines/{keeper['id']}/pin", json={"snapshot": "v2"})
    r = client.post(f"/api/v1/machines/{keeper['id']}/apply-writebacks")
    assert r.json()["pinned_snapshot"] is None and r.json()["clone_snapshot"] == f"{MASTER}@v3"

    client.put(f"/api/v1/machines/{other['id']}/pin", json={"snapshot": "base"})
    r = client.post(f"/api/v1/machines/{other['id']}/assign", json={"game_disk_id": None})
    assert r.json()["pinned_snapshot"] is None


def test_writebacks_show_the_pin(client, disk):
    _, other = _two_versions(client, disk)
    client.put(f"/api/v1/machines/{other['id']}/pin", json={"snapshot": "base"})
    rows = {w["machine_name"]: w for w in client.get(f"/api/v1/game-disks/{disk['id']}/writebacks").json()}
    assert (rows["pc02"]["pinned_snapshot"], rows["pc02"]["outdated"]) == ("base", False)
