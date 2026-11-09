"""Updates of ggNet from the web UI (app.services.updates, /api/v1/server/update)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api import deps
from app.main import app
from app.services import updates as upd


class Runner:
    def __init__(self):
        self.calls, self.active = [], False

    def run(self, cmd, quiet=False, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return self.active, "active" if self.active else "inactive", ""
        if cmd[0] == "systemd-run":
            self.active = True                  # systemd starts the unit at once
        return True, "", ""


RELEASE = upd.Release(tag="v0.2.0", version="0.2.0", name="ggNet 0.2.0", notes="- Backup / Restore",
                      url="https://github.com/x/y/releases/tag/v0.2.0", published_at="2026-11-08T10:00:00Z",
                      checked=True)


OLD = upd.Release(tag="v0.0.9", version="0.0.9", name="ggNet 0.0.9", notes="- First", url="", published_at="",
                  checked=True)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    install = tmp_path / "opt" / "ggnet"
    (install / "backend").mkdir(parents=True)
    updater = tmp_path / "ggnet-update"
    updater.write_text("#!/bin/sh\n")
    monkeypatch.setattr(upd, "UPDATER", updater)
    runner = Runner()
    u = upd.Updates({"updates": {"install_dir": str(install)}, "paths": {"data_dir": str(tmp_path / "data")}},
                    runner, "0.1.0", install / "backend")
    u._fetch = lambda: RELEASE
    u._fetch_all = lambda: [RELEASE, OLD]
    return u, runner


def test_versions_compare_as_releases():
    assert upd.newer("0.2.0", "0.1.0") and upd.newer("v0.10.0", "0.9.9")
    assert not upd.newer("0.1.0", "0.1.0") and not upd.newer("0.1.0-rc1", "0.1.0")
    assert upd.newer("0.1.0", "0.1.0-rc1") and upd.newer("0.1.0", "dev")
    assert not upd.newer("garbage", "0.1.0")


def test_only_an_installed_ggnet_updates_itself(tmp_path, installed, monkeypatch):
    u, _ = installed
    assert u.installed() is None
    dev = upd.Updates({}, Runner(), "0.1.0", tmp_path / "src" / "ggnet" / "backend")
    assert "development checkout" in dev.installed()
    monkeypatch.setattr(upd, "UPDATER", tmp_path / "missing")
    assert "missing" in u.installed()


def test_an_update_runs_as_its_own_unit_and_reports_how_it_ended(installed):
    u, runner = installed
    with pytest.raises(upd.UpdateError, match="not a release tag"):
        u.start("latest; rm -rf /")
    u.start("v0.2.0")
    [cmd] = [c for c in runner.calls if c[0] == "systemd-run"]
    assert cmd[:5] == ["systemd-run", "--unit=ggnet-update", "--collect", "--quiet", "--description=ggNet update"]
    assert cmd[-1].startswith(f"{upd.UPDATER} --version v0.2.0 > ")
    runner.active = True
    assert u.status()["state"] == "running"
    with pytest.raises(upd.UpdateError, match="already running"):
        u.start("v0.2.0")
    runner.active = False
    job_log = Path(u.history()[0]["log"])
    assert job_log.name.startswith("update-") and cmd[-1].startswith(f"{upd.UPDATER} --version v0.2.0 > {job_log}")
    job_log.write_text("==> Downloading ggNet v0.2.0...\n==> ggNet updated.\n")
    u.state_file.with_suffix(".exit").write_text("0\n")
    status = u.status()
    assert (status["state"], status["exit"], status["from"]) == ("done", 0, "0.1.0")
    assert status["log"][-1] == "==> ggNet updated."
    u.state_file.with_suffix(".exit").write_text("1\n")
    assert u.status()["state"] == "failed"


def test_the_ui_offers_a_newer_release_and_starts_it(client, installed):
    u, runner = installed
    app.dependency_overrides[deps.get_updates] = lambda: u
    out = client.get("/api/v1/server/update").json()
    assert (out["current"], out["latest"]["tag"], out["available"], out["can_update"]) == ("0.1.0", "v0.2.0", True, True)
    assert client.post("/api/v1/server/update", json={"version": "v0.1.9"}).status_code == 409
    r = client.post("/api/v1/server/update", json={"version": "v0.2.0"})
    assert r.status_code == 200, r.text
    assert any(c[0] == "systemd-run" for c in runner.calls)


def test_a_development_checkout_is_never_updated_from_the_ui(client, tmp_path):
    u = upd.Updates({}, Runner(), "0.1.0", tmp_path / "src" / "backend")
    u._fetch = lambda: RELEASE
    u._fetch_all = lambda: [RELEASE]
    app.dependency_overrides[deps.get_updates] = lambda: u
    out = client.get("/api/v1/server/update").json()
    assert out["available"] is True and out["can_update"] is False and "git" in out["reason"]
    assert client.post("/api/v1/server/update", json={"version": "v0.2.0"}).status_code == 409


def test_no_release_yet_and_github_errors_are_shown(client, installed):
    u, _ = installed
    u._fetch = lambda: None
    app.dependency_overrides[deps.get_updates] = lambda: u
    out = client.get("/api/v1/server/update?check=true").json()
    assert out["latest"] is None and out["available"] is False

    def broken():
        raise upd.UpdateError("cannot reach GitHub: timed out")

    u._fetch = broken
    out = client.get("/api/v1/server/update?check=true").json()
    assert out["error"] == "cannot reach GitHub: timed out"


def test_the_history_keeps_every_update_with_its_log(client, installed):
    u, runner = installed
    app.dependency_overrides[deps.get_updates] = lambda: u
    assert client.post("/api/v1/server/update", json={"version": "v0.2.0"}).status_code == 200
    out = client.get("/api/v1/server/update").json()
    (entry,) = out["history"]
    assert (entry["kind"], entry["version"], entry["from_version"], entry["state"]) == ("update", "v0.2.0", "0.1.0", "running")
    assert entry["by"] == "test-admin"
    Path(u.history()[0]["log"]).write_text("line 1\n==> ggNet updated.\n")
    u.state_file.with_suffix(".exit").write_text("0\n")
    runner.active = False
    (entry,) = client.get("/api/v1/server/update").json()["history"]
    assert entry["state"] == "done" and entry["finished_at"] and entry["exit"] == 0
    log = client.get(f"/api/v1/server/update/history/{entry['id']}/log").json()
    assert log == ["line 1", "==> ggNet updated."]
    assert client.get("/api/v1/server/update/history/nope/log").status_code == 404


def test_releases_carry_their_notes_and_mark_the_installed_one(client, installed):
    u, _ = installed
    u.version = "0.2.0"
    app.dependency_overrides[deps.get_updates] = lambda: u
    rel = client.get("/api/v1/server/update").json()["releases"]
    assert [(r["tag"], r["installed"], r["notes"]) for r in rel] == [("v0.2.0", True, "- Backup / Restore"),
                                                                    ("v0.0.9", False, "- First")]


def test_a_downgrade_needs_the_database_from_before_the_update(client, installed):
    u, runner = installed
    u.version = "0.2.0"
    app.dependency_overrides[deps.get_updates] = lambda: u
    rel = {r["tag"]: r for r in client.get("/api/v1/server/update").json()["releases"]}
    assert rel["v0.0.9"]["can_downgrade"] is False                  # no backup from 0.0.9
    r = client.post("/api/v1/server/update/downgrade", json={"version": "v0.0.9"})
    assert r.status_code == 409 and "no database backup" in r.json()["detail"]["error"]

    u.db_backups.mkdir(parents=True)
    dump = u.db_backups / "ggnet-20261109120000-from-0.0.9.dump"
    dump.write_bytes(b"PGDMP")
    (u.db_backups / "ggnet-20261108120000-from-0.0.8.dump").write_bytes(b"PGDMP")
    rel = {r["tag"]: r for r in client.get("/api/v1/server/update").json()["releases"]}
    assert rel["v0.0.9"]["can_downgrade"] is True and rel["v0.2.0"]["can_downgrade"] is False
    r = client.post("/api/v1/server/update/downgrade", json={"version": "v0.0.9"})
    assert r.status_code == 200, r.text
    cmd = [c for c in runner.calls if c[0] == "systemd-run"][-1]
    assert f"--version v0.0.9 --restore-db {dump} > " in cmd[-1]
    (entry,) = r.json()["history"]
    assert (entry["kind"], entry["database"]) == ("downgrade", dump.name)


def test_a_downgrade_goes_only_down(installed):
    u, _ = installed
    with pytest.raises(upd.UpdateError, match="not older"):
        u.start("v0.2.0", downgrade=True)


def test_a_job_is_closed_from_its_own_exit_file_whatever_version_runs_later(client, installed):
    """After a downgrade the older ggNet never closes the job; the next look at the history does."""
    u, runner = installed
    u.start("v0.2.0")
    entry = u.history()[0]
    cmd = [c for c in runner.calls if c[0] == "systemd-run"][-1]
    job_exit = Path(entry["log"]).with_suffix(".exit")
    assert f"echo $code > {job_exit}" in cmd[-1]
    # An older version took over: update.json no longer names this job.
    u.state_file.write_text('{"version": "v0.0.9"}')
    runner.active = False
    assert u.history()[0]["state"] == "unknown"           # ended, its result not recorded
    u._save_history([{**entry, "state": "running"}])
    job_exit.write_text("0\n")
    closed = u.history()[0]
    assert (closed["state"], closed["exit"]) == ("done", 0) and closed["finished_at"]
    job_exit.write_text("3\n")
    u._save_history([{**entry, "state": "running"}])
    assert u.history()[0]["state"] == "failed"


def test_a_running_job_stays_running(installed):
    u, runner = installed
    u.start("v0.2.0")
    assert runner.active and u.history()[0]["state"] == "running"
