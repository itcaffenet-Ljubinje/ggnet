"""ISCSIManager tests: fake runner only, no host."""

from __future__ import annotations

import pytest

from app.iscsi_manager import DEFAULT_IQN_PREFIX, Disk, ISCSIConfig, ISCSIManager, parse_portal

from .conftest import FakeRunner
from .fakehost import FakeHost

DEV = "/dev/zvol/tank/ggnet/writebacks/pc01"
INITIATOR = "iqn.1991-05.com.microsoft:pc01"
TARGET = f"{DEFAULT_IQN_PREFIX}:storage"
LEGACY = f"{DEFAULT_IQN_PREFIX}:client-pc01"
PORTAL = "192.168.10.1:3260"
TPG = f"/iscsi/{TARGET}/tpg1"
PORTALS = f"{TPG}/portals"
GAME = [Disk("game", DEV)]


@pytest.fixture
def iscsi(runner: FakeRunner) -> ISCSIManager:
    return ISCSIManager(config=ISCSIConfig(portal=PORTAL), runner=runner)


# ── Configuration ─────────────────────────────────────────────────────

def test_prefix_default_without_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GGNET_CONFIG", str(tmp_path / "missing.toml"))
    # Dev defaults use a separate -dev target, never the production one.
    assert ISCSIManager(runner=FakeRunner()).target_iqn() == f"{DEFAULT_IQN_PREFIX}:storage-dev"


def test_prefix_from_config_toml(monkeypatch, tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text(
        '[services]\n'
        'iscsi_portal = "192.168.10.1:3260"\n'
        'iscsi_iqn_prefix = "iqn.2026-10.ba.kafic"\n'
        'iscsi_target_name = "games"\n'
    )
    monkeypatch.setenv("GGNET_CONFIG", str(conf))
    iscsi = ISCSIManager(runner=FakeRunner())
    assert iscsi.target_iqn() == "iqn.2026-10.ba.kafic:games"
    assert (iscsi.portal_ip, iscsi.portal_port) == ("192.168.10.1", 3260)


def test_prefix_default_when_key_missing():
    cfg = ISCSIConfig.from_config({"services": {"iscsi_portal": PORTAL}})
    assert (cfg.iqn_prefix, cfg.target_name) == (DEFAULT_IQN_PREFIX, "storage")


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


@pytest.mark.parametrize("name", ["Storage", "-x", "a b", "a/b", "a:b", ""])
def test_invalid_target_name_raises(name):
    with pytest.raises(ValueError):
        ISCSIManager(config=ISCSIConfig(portal=PORTAL, target_name=name), runner=FakeRunner())


# ── Invalid input sends NO commands ───────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda i: i.attach("PC01", INITIATOR, GAME),                  # uppercase
    lambda i: i.attach("pc 01", INITIATOR, GAME),
    lambda i: i.attach("-pc01", INITIATOR, GAME),
    lambda i: i.attach("pc/01", INITIATOR, GAME),
    lambda i: i.attach("", INITIATOR, GAME),
    lambda i: i.attach(None, INITIATOR, GAME),
    lambda i: i.attach("pc01", INITIATOR, [Disk("game", "/dev/sda")]),     # not a zvol
    lambda i: i.attach("pc01", INITIATOR, [Disk("game", "/dev/zvol/../sda")]),
    lambda i: i.attach("pc01", INITIATOR, [Disk("game", "/dev/zvol/tank/x y")]),
    lambda i: i.attach("pc01", INITIATOR, [Disk("swap", DEV)]),            # unknown slot
    lambda i: i.attach("pc01", INITIATOR, [Disk("game", DEV), Disk("game", DEV)]),
    lambda i: i.attach("pc01", INITIATOR, []),
    lambda i: i.attach("pc01", "pc01", GAME),
    lambda i: i.attach("pc01", "iqn.1991-05.com.microsoft:PC01", GAME),
    lambda i: i.attach("pc01", f"{INITIATOR} clearconfig", GAME),
    lambda i: i.detach("pc 01", INITIATOR),
    lambda i: i.detach("pc01", "not-an-iqn"),
])
def test_invalid_input_sends_no_commands(iscsi, runner, call):
    assert call(iscsi) in (None, False)
    assert runner.calls == []


# ── Shared target ─────────────────────────────────────────────────────

def test_ensure_target_creates_it_once(runner):
    host = FakeHost()
    iscsi = ISCSIManager(config=ISCSIConfig(portal=PORTAL), runner=host)
    assert iscsi.ensure_target()
    t = host.targets[TARGET]
    assert t["portals"] == {"192.168.10.1:3260"}           # default 0.0.0.0 removed
    assert t["attrs"] == {"generate_node_acls": "0", "authentication": "0"}

    host.calls.clear()
    assert iscsi.ensure_target()
    assert all(c[2] == "ls" for c in host.calls)            # existing target untouched


def test_ensure_target_refuses_when_targetcli_cannot_list(iscsi, runner):
    runner.on("targetcli", "/iscsi", "ls", result=(False, "", "targetcli crashed"))
    assert not iscsi.ensure_target()
    assert all(c[2] == "ls" for c in runner.calls)


def test_ensure_target_removes_half_created_target(runner):
    host = FakeHost()
    host.fail_on[("targetcli", PORTALS, "create")] = "Could not create NetworkPortal"
    iscsi = ISCSIManager(config=ISCSIConfig(portal=PORTAL), runner=host)
    assert not iscsi.ensure_target()
    assert TARGET not in host.targets
    assert host.last_error == "Could not create NetworkPortal"


# ── attach / detach on FakeHost ───────────────────────────────────────

@pytest.fixture
def lio() -> tuple[FakeHost, ISCSIManager]:
    host = FakeHost()
    for pc in ("pc01", "pc02", "pc10"):
        host.add_dataset(f"tank/ggnet/writebacks/{pc}")
    return host, ISCSIManager(config=ISCSIConfig(portal=PORTAL), runner=host)


def _dev(pc: str) -> str:
    return f"/dev/zvol/tank/ggnet/writebacks/{pc}"


def _iqn(pc: str) -> str:
    return f"iqn.1991-05.com.microsoft:{pc}"


def test_every_machine_sees_only_its_own_disk(lio):
    host, iscsi = lio
    for pc in ("pc01", "pc02", "pc10"):
        assert iscsi.attach(pc, _iqn(pc), [Disk("game", _dev(pc))]) == TARGET
    assert list(host.targets) == [TARGET]                   # one target for all
    for pc in ("pc01", "pc02", "pc10"):
        assert host.visible(_iqn(pc)) == [_dev(pc)]         # mapped LUN 0 = own clone
    assert host.visible("iqn.1991-05.com.microsoft:intruder") == []


def test_new_luns_are_never_auto_mapped(lio):
    host, iscsi = lio
    iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))])
    host.calls.clear()
    iscsi.attach("pc02", _iqn("pc02"), [Disk("game", _dev("pc02"))])
    creates = [c for c in host.calls if c[1].endswith(("/luns", "/acls")) and c[2] == "create"]
    assert creates and all("add_mapped_luns=false" in c for c in creates)


def test_boot_mode_maps_os_then_game(lio):
    host, iscsi = lio
    host.add_dataset("tank/ggnet/writebacks/pc01-game")
    disks = [Disk("os", _dev("pc01")), Disk("game", _dev("pc01-game"))]
    assert iscsi.attach("pc01", _iqn("pc01"), disks) == TARGET
    assert host.visible(_iqn("pc01")) == [_dev("pc01"), _dev("pc01-game")]


def test_detach_touches_only_that_machine(lio):
    host, iscsi = lio
    for pc in ("pc01", "pc10"):
        iscsi.attach(pc, _iqn(pc), [Disk("game", _dev(pc))])
    assert iscsi.detach("pc01", _iqn("pc01"))
    assert TARGET in host.targets
    assert host.visible(_iqn("pc01")) == []
    assert host.visible(_iqn("pc10")) == [_dev("pc10")]     # pc1x untouched
    assert iscsi.backstore_exists("pc01", "game") is False
    assert iscsi.backstore_exists("pc10", "game") is True


def test_detach_is_idempotent(lio):
    _, iscsi = lio
    assert iscsi.detach("pc01", _iqn("pc01"))
    assert iscsi.detach("pc01", _iqn("pc01"))


def test_tpg_lun_numbers_are_reused(lio):
    host, iscsi = lio
    for pc in ("pc01", "pc02"):
        iscsi.attach(pc, _iqn(pc), [Disk("game", _dev(pc))])
    iscsi.detach("pc01", _iqn("pc01"))
    iscsi.attach("pc10", _iqn("pc10"), [Disk("game", _dev("pc10"))])
    assert sorted(host.targets[TARGET]["luns"]) == [0, 1]


def test_attach_replaces_leftovers(lio):
    host, iscsi = lio
    iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))])
    assert iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))]) == TARGET
    assert host.visible(_iqn("pc01")) == [_dev("pc01")]
    assert len(host.targets[TARGET]["luns"]) == 1


def test_attach_removes_legacy_per_client_target(lio):
    host, iscsi = lio
    host.targets[LEGACY] = {"luns": {0: "client-pc01"}, "acls": {_iqn("pc01"): {0: 0}},
                            "portals": set(), "attrs": {}}
    host.backstores["client-pc01"] = _dev("pc01")
    assert iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))]) == TARGET
    assert LEGACY not in host.targets and "client-pc01" not in host.backstores
    assert host.visible(_iqn("pc01")) == [_dev("pc01")]


def test_attach_failure_cleans_up_and_keeps_real_error(lio):
    host, iscsi = lio
    iscsi.attach("pc02", _iqn("pc02"), [Disk("game", _dev("pc02"))])
    host.fail_on[("targetcli", f"{TPG}/acls", "create")] = "Could not create NodeACL"
    assert iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))]) is None
    assert host.last_error == "Could not create NodeACL"
    assert "pc01-game" not in host.backstores
    assert host.visible(_iqn("pc02")) == [_dev("pc02")]     # neighbour untouched
    assert TARGET in host.targets


def test_attach_refuses_when_luns_cannot_be_listed(lio):
    host, iscsi = lio
    host.fail_on[("targetcli", f"{TPG}/luns", "ls")] = "boom"
    assert iscsi.attach("pc01", _iqn("pc01"), [Disk("game", _dev("pc01"))]) is None
    assert ["targetcli", "/backstores/block", "create"] not in [c[:3] for c in host.calls]


def test_backstore_exists_is_exact_match(lio):
    host, iscsi = lio
    iscsi.attach("pc10", _iqn("pc10"), [Disk("game", _dev("pc10"))])
    assert iscsi.backstore_exists("pc1", "game") is False
    assert iscsi.backstore_exists("pc10", "game") is True


def test_backstore_exists_unknown_when_targetcli_fails(iscsi, runner):
    runner.on("targetcli", "/backstores/block", "ls", result=(False, "", "boom"))
    assert iscsi.backstore_exists("pc01", "game") is None


def test_list_targets(iscsi, runner):
    runner.on("targetcli", "/iscsi", "ls", result=(True, "o- iscsi [Targets: 0]", ""))
    assert iscsi.list_targets() == "o- iscsi [Targets: 0]"
    runner.on("targetcli", "/iscsi", "ls", result=(False, "", "error"))
    assert iscsi.list_targets() == ""
