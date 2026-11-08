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
        return True, "", ""


RELEASE = upd.Release(tag="v0.2.0", version="0.2.0", name="ggNet 0.2.0", notes="- Backup / Restore",
                      url="https://github.com/x/y/releases/tag/v0.2.0", published_at="2026-11-08T10:00:00Z",
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
    u.log_file.write_text("==> Downloading ggNet v0.2.0...\n==> ggNet updated.\n")
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
