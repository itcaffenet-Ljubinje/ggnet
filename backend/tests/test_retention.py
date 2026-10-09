"""Retention (automated snapshot and writeback removal) and reserved space, on FakeHost."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import DEV_DEFAULTS
from app.db.session import make_engine
from app.services import retention, writebacks
from app.services.retention import JobSettings, RetentionSettings
from app.services.writebacks import WritebackSettings

MASTER = "tank/ggnet/images/cs2"
RESERVED = "tank/ggnet/reserved"
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
SAVE = {"enabled": True, "dry_run": False, "reserved_percent": 15, "warning_percent": 80,
        "unused_snapshot_days": 14, "keep_newest_snapshots": 3, "inactive_writeback_hours": 24}


@pytest.fixture
def make_session(db_url):
    engine = make_engine(db_url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def disk(client) -> dict:
    d = client.post("/api/v1/game-disks", json={"name": "cs2", "size_gb": 10}).json()
    return client.post(f"/api/v1/game-disks/{d['id']}/publish").json()


def _machine(client, disk, name) -> dict:
    r = client.post("/api/v1/machines", json={"name": name, "game_disk_id": disk["id"]})
    assert r.status_code == 201, r.text
    return r.json()


def _keeper(client, disk, name="pc01") -> dict:
    m = _machine(client, disk, name)
    client.put(f"/api/v1/machines/{m['id']}/keep-writeback", json={"enabled": True})
    return m


def _versions(client, disk) -> list[str]:
    return [v["name"] for v in client.get(f"/api/v1/game-disks/{disk['id']}/snapshots").json()]


def _five_versions(client, disk) -> dict:
    """cs2@base, v2..v5 (active v5); pc01 keeps its writeback and made them, pc02 runs @base."""
    keeper = _keeper(client, disk)
    _machine(client, disk, "pc02")
    for _ in range(4):
        assert client.post(f"/api/v1/machines/{keeper['id']}/apply-writebacks").status_code == 200
    assert _versions(client, disk) == ["base", "v2", "v3", "v4", "v5"]
    return keeper


# ── Settings and reserved space ───────────────────────────────────────

def test_defaults_are_not_applied_until_saved(client, host):
    r = client.get("/api/v1/settings/retention")
    assert r.status_code == 200
    got = r.json()
    assert (got["saved"], got["enabled"], got["dry_run"], got["reserved_percent"]) == (False, False, True, 15)
    assert got["last_run"] is None
    assert RESERVED not in host.datasets          # nothing on the host yet


def test_saving_reserves_part_of_the_pool(client, host):
    r = client.put("/api/v1/settings/retention", json=SAVE)
    assert r.status_code == 200 and r.json()["saved"] is True
    total = host.pool["used"] + host.pool["avail"]
    props = host.datasets[RESERVED]["props"]
    assert props["refreservation"] == str(total * 15 // 100)
    assert (props["canmount"], props["mountpoint"]) == ("off", "none")

    storage = client.get("/api/v1/storage").json()
    assert storage["reserved_bytes"] == total * 15 // 100
    assert storage["total_bytes"] == total

    # The same percentage again does not touch the host; 0 removes the reservation.
    calls = len(host.calls)
    client.put("/api/v1/settings/retention", json={**SAVE, "enabled": False})
    assert not any(c[:2] == ["zfs", "set"] for c in host.calls[calls:])
    client.put("/api/v1/settings/retention", json={**SAVE, "reserved_percent": 0})
    assert host.datasets[RESERVED]["props"]["refreservation"] == "none"


def test_reservation_failure_is_502_and_not_saved(client, host):
    host.fail_on[("zfs", "create")] = "out of space"
    r = client.put("/api/v1/settings/retention", json=SAVE)
    assert r.status_code == 502 and "out of space" in r.text
    assert client.get("/api/v1/settings/retention").json()["saved"] is False


@pytest.mark.parametrize("field,value", [
    ("reserved_percent", 60), ("warning_percent", 10), ("unused_snapshot_days", 0),
    ("keep_newest_snapshots", 0), ("inactive_writeback_hours", 0),
])
def test_settings_validation(client, field, value):
    assert client.put("/api/v1/settings/retention", json={**SAVE, field: value}).status_code == 422


def test_storage_warns_above_the_threshold(client, host):
    host.pool.update(used=9 << 40, avail=1 << 40)
    s = client.get("/api/v1/storage").json()
    assert (s["pool"], s["used_percent"], s["warning"]) == ("tank", 90.0, True)
    client.put("/api/v1/settings/retention", json={**SAVE, "warning_percent": 95, "reserved_percent": 0})
    assert client.get("/api/v1/storage").json()["warning"] is False


# ── Old versions ──────────────────────────────────────────────────────

def test_preview_and_run_delete_only_unused_old_versions(client, host, disk):
    _five_versions(client, disk)
    # @base: pc02 runs on it. v3..v5: the newest 3. Only v2 may go.
    r = client.post("/api/v1/settings/retention/preview", json=SAVE)
    assert r.status_code == 200, r.text
    assert [(a["kind"], a["target"], a["done"]) for a in r.json()] == [("snapshot", "cs2@v2", False)]
    assert f"{MASTER}@v2" in host.snapshots                       # preview changes nothing

    assert client.post("/api/v1/settings/retention/run").status_code == 409   # never saved
    client.put("/api/v1/settings/retention", json=SAVE)
    r = client.post("/api/v1/settings/retention/run")
    assert r.status_code == 200, r.text
    assert [(a["target"], a["done"]) for a in r.json()["actions"]] == [("cs2@v2", True)]
    assert _versions(client, disk) == ["base", "v3", "v4", "v5"]
    assert client.get("/api/v1/settings/retention").json()["last_run"]["actions"][0]["target"] == "cs2@v2"


def test_pinned_and_young_versions_are_kept(client, disk):
    _five_versions(client, disk)
    pc02 = next(m for m in client.get("/api/v1/machines").json() if m["name"] == "pc02")
    client.put(f"/api/v1/machines/{pc02['id']}/pin", json={"snapshot": "v2"})
    assert client.post("/api/v1/settings/retention/preview", json=SAVE).json() == []
    # FakeHost versions are from 2025; with a 5-year limit none is old enough.
    client.put(f"/api/v1/machines/{pc02['id']}/pin", json={"snapshot": None})
    assert client.post("/api/v1/settings/retention/preview",
                       json={**SAVE, "unused_snapshot_days": 3650}).json() == []


# ── Inactive kept writebacks ──────────────────────────────────────────

def _connected_then_off_at(make_session, prov, host, name, t):
    """Let the watcher see the PC connected (writes) and then off since `t`."""
    s = WritebackSettings(auto_discard=False)
    host.sessions.add(f"iqn.1991-05.com.microsoft:{name}")
    writebacks.tick(make_session, prov, s, now=t - timedelta(minutes=5))
    host.sessions.discard(f"iqn.1991-05.com.microsoft:{name}")
    writebacks.tick(make_session, prov, s, now=t)


def test_inactive_kept_writeback_is_discarded(client, host, prov, disk, make_session):
    keeper = _keeper(client, disk)
    _connected_then_off_at(make_session, prov, host, "pc01", T0)
    s = RetentionSettings(**SAVE)

    with make_session() as db:
        assert retention.plan(db, prov, s, now=T0 + timedelta(hours=23)) == []
        clones = sum(1 for c in host.calls if c[:2] == ["zfs", "clone"])
        report = retention.run(db, prov, s, dry_run=False, now=T0 + timedelta(hours=25))
    assert [(a.kind, a.target, a.done) for a in report.actions] == [("writeback", "pc01", True)]
    assert sum(1 for c in host.calls if c[:2] == ["zfs", "clone"]) == clones + 1
    got = client.get(f"/api/v1/machines/{keeper['id']}").json()
    assert (got["keep_writeback"], got["writeback_dirty"]) == (True, False)

    # A clean writeback is not discarded again every hour.
    with make_session() as db:
        assert retention.plan(db, prov, s, now=T0 + timedelta(hours=50)) == []


def test_connected_pc_is_skipped_at_run_time(client, host, prov, disk, make_session):
    _keeper(client, disk)
    _connected_then_off_at(make_session, prov, host, "pc01", T0)
    host.sessions.add("iqn.1991-05.com.microsoft:pc01")   # came back after the plan's data
    with make_session() as db:
        report = retention.run(db, prov, RetentionSettings(**SAVE), dry_run=False,
                               now=T0 + timedelta(hours=25))
    assert report.actions[0].done is False and "connected" in report.actions[0].error


# ── Hourly job ────────────────────────────────────────────────────────

def test_job_does_nothing_until_enabled_and_dry_run_deletes_nothing(client, host, prov, disk, make_session):
    _five_versions(client, disk)
    assert retention.tick(make_session, prov) is None              # never saved = off

    client.put("/api/v1/settings/retention", json={**SAVE, "dry_run": True})
    report = retention.tick(make_session, prov)
    assert report is not None and report.dry_run is True
    assert [(a.target, a.done) for a in report.actions] == [("cs2@v2", False)]
    assert f"{MASTER}@v2" in host.snapshots


def test_job_is_off_in_dev_defaults():
    assert JobSettings.from_config(DEV_DEFAULTS).job is False
    assert JobSettings.from_config({}).job is True
