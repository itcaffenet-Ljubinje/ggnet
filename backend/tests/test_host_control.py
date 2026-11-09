"""Settings -> General: services, restart, reboot (app.services.host_control) and the activity CSV."""

from __future__ import annotations

import pytest

from app.api.deps import get_host_control
from app.main import app
from app.services.host_control import HostControl, HostError, own_unit

CFG = {"boot": {"pxe_service": "ggnet-pxe"},
       "paths": {"database": "postgresql+psycopg:///ggnet?host=/run/postgresql"}}


class Runner:
    """Answers systemctl show / qm list / pct list; records everything else."""

    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[list[str]] = []
        self.fail = fail

    def run(self, cmd, timeout=None, quiet=False):
        self.calls.append(list(cmd))
        if cmd[0] in self.fail:
            return False, "", f"{cmd[0]} failed"
        if cmd[:2] == ["systemctl", "show"]:
            state = "failed" if cmd[2] == "ggnet-pxe" else "active"
            return True, f"ActiveState={state}\nSubState=running\nActiveEnterTimestamp=@1794100000", ""
        if cmd[:2] == ["qm", "list"]:
            return True, ("      VMID NAME                 STATUS     MEM(MB)\n"
                          "       100 Debian-13            stopped    4096\n"
                          "       106 ccDISK               running    32768\n"), ""
        if cmd[:2] == ["pct", "list"]:
            return True, "VMID       Status     Lock         Name\n102        running                 dns\n", ""
        if cmd[0] == "pveversion":
            return True, "pve-manager/9.1.1/abc (running kernel: 7.0.14-28-pve)", ""
        return True, "", ""


def test_own_unit_from_the_cgroup(tmp_path):
    f = tmp_path / "cgroup"
    f.write_text("0::/system.slice/ggnet.service\n")
    assert own_unit(f) == "ggnet.service"
    f.write_text("0::/user.slice/user-0.slice/session-8.scope\n")
    assert own_unit(f) is None


def test_its_own_unit_is_found_unless_given(monkeypatch):
    import app.services.host_control as hc

    monkeypatch.setattr(hc, "own_unit", lambda: "ggnet.service")
    assert HostControl(CFG, Runner()).unit == "ggnet.service"
    assert HostControl(CFG, Runner(), unit=None).unit is None      # e.g. the tests, a CI runner


def test_services_with_their_state_and_the_iscsi_target_locked():
    host = HostControl(CFG, Runner(), unit="ggnet.service")
    got = {s.unit: s for s in host.services()}
    assert list(got) == ["ggnet", "ggnet-pxe", "postgresql", "rtslib-fb-targetctl"]
    assert got["ggnet-pxe"].state == "failed"
    assert got["ggnet"].since is not None and got["ggnet"].since.year == 2026
    assert got["rtslib-fb-targetctl"].restartable is False and "every PC" in got["rtslib-fb-targetctl"].note


def test_no_postgresql_line_on_sqlite():
    host = HostControl({"paths": {"database": "sqlite:///x.db"}}, Runner(), unit=None)
    assert [s.unit for s in host.services()] == ["ggnet-pxe", "rtslib-fb-targetctl"]


def test_restart_another_service_right_away_and_ggnet_itself_a_moment_later():
    runner = Runner()
    host = HostControl(CFG, runner, unit="ggnet.service")
    assert host.restart("ggnet-pxe") is False
    assert runner.calls[-1] == ["systemctl", "restart", "ggnet-pxe.service"]
    assert host.restart("ggnet") is True
    assert runner.calls[-1][0] == "systemd-run" and "--on-active=2" in runner.calls[-1]
    assert runner.calls[-1][-2:] == ["restart", "ggnet.service"]


@pytest.mark.parametrize("unit, msg", [("rtslib-fb-targetctl", "never restarted"), ("sshd", "not one of")])
def test_restart_refuses_the_target_and_anything_else(unit, msg):
    with pytest.raises(HostError, match=msg):
        HostControl(CFG, Runner(), unit="ggnet.service").restart(unit)


def test_running_guests():
    assert HostControl(CFG, Runner()).running_guests() == ["VM 106 ccDISK", "CT 102 dns"]


def test_reboot_wants_the_word():
    runner = Runner()
    host = HostControl(CFG, runner)
    with pytest.raises(HostError, match="Type REBOOT"):
        host.reboot("reboot please")
    assert not any(c[0] == "systemd-run" for c in runner.calls)
    host.reboot("REBOOT")
    assert runner.calls[-1][:2] == ["systemd-run", "--on-active=5"] and runner.calls[-1][-1] == "reboot"
    assert "--timer-property=AccuracySec=100ms" in runner.calls[-1]      # not up to a minute late


@pytest.fixture
def host_runner():
    runner = Runner()
    app.dependency_overrides[get_host_control] = lambda: HostControl(CFG, runner, unit="ggnet.service")
    yield runner
    app.dependency_overrides.pop(get_host_control, None)


def test_general_page(client, host_runner):
    client.post("/api/v1/machines", json={"name": "pc01"})
    client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.9"})
    got = client.get("/api/v1/server/general").json()
    assert got["proxmox"].startswith("pve-manager/9")
    assert got["guests"] == ["VM 106 ccDISK", "CT 102 dns"]
    assert got["online_pcs"] == ["pc01"] and got["running_tasks"] == []
    assert got["database"]["kind"] in ("sqlite", "postgresql") and got["database"]["size_bytes"] > 0
    assert [s["unit"] for s in got["services"]][:2] == ["ggnet", "ggnet-pxe"]


def test_restart_and_reboot_api(client, host_runner):
    r = client.post("/api/v1/server/services/ggnet-pxe/restart")
    assert r.status_code == 200 and r.json()["restarting_self"] is False
    r = client.post("/api/v1/server/services/rtslib-fb-targetctl/restart")
    assert r.status_code == 409
    assert client.post("/api/v1/server/reboot", json={"confirm": "yes"}).status_code == 409
    assert client.post("/api/v1/server/reboot", json={"confirm": "REBOOT"}).status_code == 202


def test_activity_csv(client):
    client.post("/api/v1/machines", json={"name": "pc01"})
    r = client.get("/api/v1/activity/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="ggnet-activity-' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "time_utc,user,action,status,ip_address"
