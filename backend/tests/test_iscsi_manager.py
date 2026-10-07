"""ISCSIManager tests: fake runner only, no host."""

from __future__ import annotations

import pytest

from app.iscsi_manager import DEFAULT_IQN_PREFIX, ISCSIConfig, ISCSIManager, parse_portal

from .conftest import FakeRunner

DEV = "/dev/zvol/tank/ggnet/writebacks/pc01"
INITIATOR = "iqn.1991-05.com.microsoft:pc01"
TARGET = f"{DEFAULT_IQN_PREFIX}:client-pc01"
PORTAL = "192.168.10.1:3260"
PORTALS = f"/iscsi/{TARGET}/tpg1/portals"


@pytest.fixture
def iscsi(runner: FakeRunner) -> ISCSIManager:
    return ISCSIManager(config=ISCSIConfig(portal=PORTAL), runner=runner)


# ── Configuration ─────────────────────────────────────────────────────

def test_prefix_default_without_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GGNET_CONFIG", str(tmp_path / "missing.toml"))
    assert ISCSIManager(runner=FakeRunner()).target_iqn("pc01") == TARGET


def test_prefix_from_config_toml(monkeypatch, tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text(
        '[services]\n'
        'iscsi_portal = "192.168.10.1:3260"\n'
        'iscsi_iqn_prefix = "iqn.2026-10.ba.kafic"\n'
    )
    monkeypatch.setenv("GGNET_CONFIG", str(conf))
    iscsi = ISCSIManager(runner=FakeRunner())
    assert iscsi.target_iqn("pc01") == "iqn.2026-10.ba.kafic:client-pc01"
    assert (iscsi.portal_ip, iscsi.portal_port) == ("192.168.10.1", 3260)


def test_prefix_default_when_key_missing():
    cfg = ISCSIConfig.from_config({"services": {"iscsi_portal": PORTAL}})
    assert cfg.iqn_prefix == DEFAULT_IQN_PREFIX


@pytest.mark.parametrize("cfg", [{}, {"services": {}}, {"services": {"iscsi_portal": ""}}])
def test_missing_portal_raises(cfg):
    with pytest.raises(ValueError):
        ISCSIConfig.from_config(cfg)


@pytest.mark.parametrize("portal, expected", [
    ("192.168.10.1:3260", ("192.168.10.1", 3260)),
    ("127.0.0.1:3260", ("127.0.0.1", 3260)),
    ("10.0.0.5:3261", ("10.0.0.5", 3261)),
    ("[fd00::1]:3260", ("fd00::1", 3260)),
])
def test_parse_portal(portal, expected):
    assert parse_portal(portal) == expected


@pytest.mark.parametrize("portal", [
    "0.0.0.0:3260",          # wildcard: would listen on every interface
    "[::]:3260",
    "192.168.10.1",          # no port
    "192.168.10.1:",
    "192.168.10.1:0",
    "192.168.10.1:70000",
    "server:3260",           # not an IP
    "192.168.10.1:32a0",
    "",
])
def test_invalid_portal_raises(portal):
    with pytest.raises(ValueError):
        ISCSIManager(config=ISCSIConfig(portal=portal), runner=FakeRunner())


@pytest.mark.parametrize("prefix", [
    "iqn.2026-10.Ba.Kafic",        # uppercase
    "iqn.2026-10.ba.kafic:x",      # ':' is added by target_iqn
    "eui.0123456789abcdef",
    "ggnet",
])
def test_invalid_prefix_raises(prefix):
    with pytest.raises(ValueError):
        ISCSIManager(config=ISCSIConfig(portal=PORTAL, iqn_prefix=prefix), runner=FakeRunner())


# ── Invalid input sends NO commands ───────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda i: i.create_target("PC01", DEV, INITIATOR),            # uppercase
    lambda i: i.create_target("pc 01", DEV, INITIATOR),
    lambda i: i.create_target("-pc01", DEV, INITIATOR),
    lambda i: i.create_target("pc/01", DEV, INITIATOR),
    lambda i: i.create_target("", DEV, INITIATOR),
    lambda i: i.create_target(None, DEV, INITIATOR),
    lambda i: i.create_target("pc01", "/dev/sda", INITIATOR),     # not a zvol
    lambda i: i.create_target("pc01", "/dev/zvol/../sda", INITIATOR),
    lambda i: i.create_target("pc01", "/dev/zvol/tank/x y", INITIATOR),
    lambda i: i.create_target("pc01", DEV, "pc01"),
    lambda i: i.create_target("pc01", DEV, "iqn.1991-05.com.microsoft:PC01"),
    lambda i: i.create_target("pc01", DEV, f"{INITIATOR} clearconfig"),
    lambda i: i.delete_target("pc 01"),
    lambda i: i.target_status("../x"),
])
def test_invalid_input_sends_no_commands(iscsi, runner, call):
    assert call(iscsi) in (None, False, "")
    assert runner.calls == []


# ── Valid input ───────────────────────────────────────────────────────

def test_create_target_order(iscsi, runner):
    assert iscsi.create_target("pc01", DEV, INITIATOR) == TARGET
    assert runner.calls == [
        ["targetcli", "/backstores/block", "create", "name=client-pc01", f"dev={DEV}"],
        ["targetcli", "/iscsi", "create", TARGET],
        ["targetcli", PORTALS, "delete", "0.0.0.0", "3260"],
        ["targetcli", PORTALS, "create", "192.168.10.1", "3260"],
        ["targetcli", f"/iscsi/{TARGET}/tpg1/luns", "create", "/backstores/block/client-pc01"],
        ["targetcli", f"/iscsi/{TARGET}/tpg1/acls", "create", INITIATOR],
        ["targetcli", f"/iscsi/{TARGET}/tpg1", "set", "attribute",
         "generate_node_acls=0", "authentication=0"],
        ["targetcli", "saveconfig"],
    ]


def test_create_target_cleans_up_and_keeps_real_error(iscsi, runner):
    runner.on("targetcli", "/iscsi", "create", result=(False, "", "Could not create Target"))
    runner.on("targetcli", "/iscsi", "delete", result=(False, "", "No such Target"))
    assert iscsi.create_target("pc01", DEV, INITIATOR) is None
    assert runner.calls[-3:] == [
        ["targetcli", "/iscsi", "delete", TARGET],
        ["targetcli", "/backstores/block", "delete", "client-pc01"],
        ["targetcli", "saveconfig"],
    ]
    assert not any(c[1].endswith("/luns") for c in runner.calls)
    assert runner.last_error == "Could not create Target"


def test_create_target_without_default_portal(iscsi, runner):
    # auto_add_default_portal=false: there is no 0.0.0.0 portal to delete.
    runner.on("targetcli", PORTALS, "delete", result=(False, "", "No such NetworkPortal"))
    assert iscsi.create_target("pc01", DEV, INITIATOR) == TARGET
    assert ["targetcli", PORTALS, "create", "192.168.10.1", "3260"] in runner.calls


def test_create_target_fails_when_portal_cannot_bind(iscsi, runner):
    runner.on("targetcli", PORTALS, "create", result=(False, "", "Could not create NetworkPortal"))
    assert iscsi.create_target("pc01", DEV, INITIATOR) is None
    assert not any(c[1].endswith("/luns") for c in runner.calls)
    assert ["targetcli", "/iscsi", "delete", TARGET] in runner.calls
    assert runner.last_error == "Could not create NetworkPortal"


def test_delete_target(iscsi, runner):
    assert iscsi.delete_target("pc01")
    assert runner.calls == [
        ["targetcli", "/iscsi", "delete", TARGET],
        ["targetcli", "/backstores/block", "delete", "client-pc01"],
        ["targetcli", "saveconfig"],
    ]


def test_delete_target_returns_true_even_when_nothing_existed(iscsi, runner):
    runner.on("targetcli", "/iscsi", "delete", result=(False, "", "No such Target"))
    runner.on("targetcli", "/backstores/block", "delete", result=(False, "", "No storage object"))
    assert iscsi.delete_target("pc01")


def test_target_status(iscsi, runner):
    runner.on("targetcli", f"/iscsi/{TARGET}", "status", result=(True, "TPGs: 1", ""))
    assert iscsi.target_status("pc01") == "TPGs: 1"
    runner.on("targetcli", f"/iscsi/{TARGET}", "status", result=(False, "", "No such path"))
    assert iscsi.target_status("pc01") == "No such path"


def test_list_targets(iscsi, runner):
    runner.on("targetcli", "/iscsi", "ls", result=(True, "o- iscsi [Targets: 0]", ""))
    assert iscsi.list_targets() == "o- iscsi [Targets: 0]"
    runner.on("targetcli", "/iscsi", "ls", result=(False, "", "error"))
    assert iscsi.list_targets() == ""
