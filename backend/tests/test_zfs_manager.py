"""ZFSManager tests: fake runner only, no host."""

from __future__ import annotations

import pytest

from app.config import DEV_DEFAULTS
from app.zfs_manager import ZFSLayout, ZFSManager

from .conftest import make_layout

CLIENT = "tank/ggnet/writebacks/pc01"
MASTER = "tank/ggnet/images/steam"
MASTER_SNAP = f"{MASTER}@base"


# ── Configuration ─────────────────────────────────────────────────────

def test_layout_from_dev_defaults_without_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GGNET_CONFIG", str(tmp_path / "missing.toml"))
    layout = ZFSLayout.from_config()
    assert layout.pool == DEV_DEFAULTS["storage"]["pool"]
    assert layout.writebacks == DEV_DEFAULTS["storage"]["writebacks"]


def test_layout_from_config_toml(monkeypatch, tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text(
        '[storage]\n'
        'pool = "STORAGE-01"\n'
        'root_dataset = "STORAGE-01/ggnet-dev"\n'
        'images = "STORAGE-01/ggnet-dev/images"\n'
        'writebacks = "STORAGE-01/ggnet-dev/writebacks"\n'
        'snapshots = "STORAGE-01/ggnet-dev/snapshots"\n'
        'iscsi_targets = "STORAGE-01/ggnet-dev/iscsi_targets"\n'
    )
    monkeypatch.setenv("GGNET_CONFIG", str(conf))
    zfs = ZFSManager()
    assert zfs.layout == make_layout("STORAGE-01", "STORAGE-01/ggnet-dev")


def test_manager_uses_configured_names(runner):
    zfs = ZFSManager(layout=make_layout("STORAGE-01", "STORAGE-01/ggnet-dev"), runner=runner)
    zfs.pool_list()
    zfs.list_clients()
    assert runner.calls[0][-1] == "STORAGE-01"
    assert runner.calls[1][-1] == "STORAGE-01/ggnet-dev/writebacks"


@pytest.mark.parametrize("storage", [
    None,
    {"pool": "tank"},
    {**DEV_DEFAULTS["storage"], "images": ""},
])
def test_layout_missing_keys_raises(storage):
    cfg = {} if storage is None else {"storage": storage}
    with pytest.raises(ValueError):
        ZFSLayout.from_config(cfg)


@pytest.mark.parametrize("pool, root", [
    ("tank", "tank"),                   # root must not be the pool itself
    ("tank", "other/ggnet"),            # root outside the pool
    ("tank", "tank2/ggnet"),            # prefix trap at pool level
    ("tank", "tank/ggnet@snap"),
    ("tank", "tank/../ggnet"),
    ("tank/x", "tank/x/ggnet"),         # pool must not contain '/'
    ("-o", "-o/ggnet"),
])
def test_invalid_root_raises(pool, root):
    with pytest.raises(ValueError):
        ZFSManager(layout=make_layout(pool, root))


@pytest.mark.parametrize("key, value", [
    ("images", "tank/ggnet"),                  # the root itself
    ("images", "tank/other/images"),           # outside the root
    ("writebacks", "tank/ggnet2/writebacks"),  # prefix trap
    ("snapshots", "tank/ggnet/snaps@x"),
    ("iscsi_targets", "tank/ggnet/../vm"),
])
def test_child_dataset_outside_root_raises(key, value):
    with pytest.raises(ValueError):
        ZFSManager(layout=make_layout(**{key: value}))


# ── _is_managed ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "tank/ggnet/images/steam",
    "tank/ggnet/writebacks/pc01",
    "tank/ggnet/images/steam@base",
    "tank/ggnet/images/steam@v2",
])
def test_is_managed_accepts_paths_under_root(zfs, path):
    assert zfs._is_managed(path)


@pytest.mark.parametrize("path", [
    "tank",
    "tank/vm-100-disk-0",
    "tank/ggnet2",                     # prefix trap
    "tank/ggnet2/images/x",
    "tank/ggnetx",
    "tank/ggnet",                      # root without allow_base
    "tank/ggnet@snap",                 # snapshot of the root itself
    "tank/ggnet/../vm-100-disk-0",
    "tank/ggnet/./images",
    "tank/ggnet/",
    "tank/ggnet//images",
    "/tank/ggnet/images/x",
    "tank/ggnet/images/x@",
    "tank/ggnet/images/x@a@b",
    "tank/ggnet/images/-r",
    "tank/ggnet/images/x@-r",
    "tank/ggnet/images/x y",
    "tank/ggnet/images/x;rm",
    "",
    None,
])
def test_is_managed_rejects_foreign_and_malformed(zfs, path):
    assert not zfs._is_managed(path)


def test_is_managed_root_only_when_allowed(zfs):
    assert zfs._is_managed("tank/ggnet", allow_base=True)
    assert not zfs._is_managed("tank/ggnet@x", allow_base=True)


def test_is_managed_snapshot_only_when_allowed(zfs):
    assert not zfs._is_managed(MASTER_SNAP, allow_snapshot=False)


def test_dev_tree_is_separate_from_production_tree(runner):
    dev = ZFSManager(layout=make_layout("tank", "tank/ggnet-dev"), runner=runner)
    assert not dev._is_managed("tank/ggnet/images/steam")
    assert dev._is_managed("tank/ggnet-dev/images/steam")


# ── Rejected paths send NO commands ──────────────────────────────────

FOREIGN = "tank/vm-100-disk-0"


@pytest.mark.parametrize("call", [
    lambda z: z.dataset_exists(FOREIGN),
    lambda z: z.create_zvol(FOREIGN, 10),
    lambda z: z.create_zvol("tank/ggnet", 10),
    lambda z: z.create_zvol(MASTER_SNAP, 10),
    lambda z: z.snapshot(FOREIGN, "base"),
    lambda z: z.snapshot(MASTER, "-r"),
    lambda z: z.snapshot(MASTER_SNAP, "x"),
    lambda z: z.protect_snapshot(f"{FOREIGN}@base"),
    lambda z: z.protect_snapshot(MASTER),              # not a snapshot
    lambda z: z.release_snapshot(f"{FOREIGN}@base"),
    lambda z: z.set_readonly(FOREIGN),
    lambda z: z.publish_master(FOREIGN),
    lambda z: z.clone(f"{FOREIGN}@base", CLIENT),
    lambda z: z.clone(MASTER, CLIENT),                 # source is not a snapshot
    lambda z: z.clone(MASTER_SNAP, FOREIGN),
    lambda z: z.clone(MASTER_SNAP, "tank/ggnet"),
    lambda z: z.reset_clone(FOREIGN, MASTER_SNAP),
    lambda z: z.destroy(FOREIGN, recursive=True),
    lambda z: z.destroy("tank/ggnet", recursive=True),
    lambda z: z.destroy("tank", recursive=True),
    lambda z: z.destroy_master(FOREIGN),
    lambda z: z.get_property(FOREIGN, "used"),
    lambda z: z.get_property(MASTER, "-r"),
    lambda z: z.set_property(FOREIGN, "readonly", "off"),
])
def test_rejected_paths_send_no_commands(zfs, runner, call):
    result = call(zfs)
    assert result in (False, "")
    assert runner.calls == []


# ── Valid paths ───────────────────────────────────────────────────────

def test_setup_dataset_tree_creates_missing(zfs, runner):
    runner.on("zfs", "list", result=(False, "", "does not exist"))
    assert zfs.setup_dataset_tree()
    created = [c[-1] for c in runner.calls if c[:2] == ["zfs", "create"]]
    assert created == [
        "tank/ggnet",
        "tank/ggnet/images",
        "tank/ggnet/writebacks",
        "tank/ggnet/snapshots",
        "tank/ggnet/iscsi_targets",
    ]


def test_setup_dataset_tree_skips_existing(zfs, runner):
    assert zfs.setup_dataset_tree()
    assert not any(c[:2] == ["zfs", "create"] for c in runner.calls)


def test_setup_dataset_tree_stops_on_failure(zfs, runner):
    runner.on("zfs", "list", result=(False, "", "does not exist"))
    runner.on("zfs", "create", result=(False, "", "out of space"))
    assert not zfs.setup_dataset_tree()
    assert sum(c[:2] == ["zfs", "create"] for c in runner.calls) == 1


def test_create_zvol(zfs, runner):
    assert zfs.create_zvol(MASTER, 50)
    assert runner.calls == [[
        "zfs", "create", "-V", "50G",
        "-o", "volblocksize=64k", "-o", "compression=off", "-o", "sync=disabled",
        MASTER,
    ]]


def test_publish_master_order(zfs, runner):
    assert zfs.publish_master(MASTER)
    assert runner.calls == [
        ["zfs", "snapshot", MASTER_SNAP],
        ["zfs", "hold", "ggnet:protected", MASTER_SNAP],
        ["zfs", "set", "readonly=on", MASTER],
    ]


def test_publish_master_stops_when_hold_fails(zfs, runner):
    runner.on("zfs", "hold", result=(False, "", "tag already exists"))
    assert not zfs.publish_master(MASTER)
    assert not any(c[:2] == ["zfs", "set"] for c in runner.calls)


def test_clone(zfs, runner):
    assert zfs.clone(MASTER_SNAP, CLIENT)
    assert runner.calls == [["zfs", "clone", "-o", "sync=disabled", MASTER_SNAP, CLIENT]]


def test_reset_clone_destroys_then_clones(zfs, runner):
    assert zfs.reset_clone(CLIENT, MASTER_SNAP)
    assert runner.calls == [
        ["zfs", "list", "-H", "-o", "name", CLIENT],
        ["zfs", "destroy", "-r", CLIENT],
        ["zfs", "clone", "-o", "sync=disabled", MASTER_SNAP, CLIENT],
    ]


def test_reset_clone_without_existing_clone(zfs, runner):
    runner.on("zfs", "list", result=(False, "", "does not exist"))
    assert zfs.reset_clone(CLIENT, MASTER_SNAP)
    assert not any(c[:2] == ["zfs", "destroy"] for c in runner.calls)
    assert runner.calls[-1][:2] == ["zfs", "clone"]


def test_reset_clone_stops_when_destroy_fails(zfs, runner):
    runner.on("zfs", "destroy", result=(False, "", "dataset is busy"))
    assert not zfs.reset_clone(CLIENT, MASTER_SNAP)
    assert not any(c[:2] == ["zfs", "clone"] for c in runner.calls)


def test_destroy_master_releases_holds_first(zfs, runner):
    runner.on("zfs", "list", "-H", "-t", "snapshot",
              result=(True, f"{MASTER}@base\t-\n{MASTER}@v2\t", ""))
    assert zfs.destroy_master(MASTER)
    assert runner.calls[1:] == [
        ["zfs", "release", "ggnet:protected", f"{MASTER}@base"],
        ["zfs", "release", "ggnet:protected", f"{MASTER}@v2"],
        ["zfs", "destroy", "-r", MASTER],
    ]


def test_destroy_master_refuses_while_clones_exist(zfs, runner):
    runner.on("zfs", "list", "-H", "-t", "snapshot",
              result=(True, f"{MASTER}@base\t{CLIENT}", ""))
    assert not zfs.destroy_master(MASTER)
    # The hold stays in place, nothing is destroyed.
    assert len(runner.calls) == 1


def test_destroy_master_refuses_when_listing_fails(zfs, runner):
    runner.on("zfs", "list", "-H", "-t", "snapshot", result=(False, "", "timeout"))
    assert not zfs.destroy_master(MASTER)
    assert len(runner.calls) == 1


def test_destroy_master_restores_hold_when_destroy_fails(zfs, runner):
    runner.on("zfs", "list", "-H", "-t", "snapshot",
              result=(True, f"{MASTER}@base\t-", ""))
    runner.on("zfs", "destroy", result=(False, "", "dataset is busy"))
    assert not zfs.destroy_master(MASTER)
    assert runner.calls[-1] == ["zfs", "hold", "ggnet:protected", f"{MASTER}@base"]


def test_destroy_master_does_not_hold_snapshots_that_had_no_hold(zfs, runner):
    runner.on("zfs", "list", "-H", "-t", "snapshot",
              result=(True, f"{MASTER}@base\t-", ""))
    runner.on("zfs", "release", result=(False, "", "no such tag"))
    runner.on("zfs", "destroy", result=(False, "", "dataset is busy"))
    assert not zfs.destroy_master(MASTER)
    assert not any(c[:2] == ["zfs", "hold"] for c in runner.calls)


def test_dataset_exists(zfs, runner):
    assert zfs.dataset_exists(CLIENT)
    runner.on("zfs", "list", result=(False, "", "does not exist"))
    assert not zfs.dataset_exists(CLIENT)


def test_list_clients_parses_output_and_skips_parent(zfs, runner):
    runner.on("zfs", "list", result=(True,
        "tank/ggnet/writebacks\t1G\t96K\t-\n"
        f"{CLIENT}\t2G\t50G\t{MASTER_SNAP}", ""))
    assert zfs.list_clients() == [
        {"zvol": CLIENT, "used": "2G", "referenced": "50G", "cloned_from": MASTER_SNAP},
    ]


def test_list_clients_empty_on_failure(zfs, runner):
    runner.on("zfs", "list", result=(False, "", "does not exist"))
    assert zfs.list_clients() == []


def test_pool_commands_use_configured_pool(zfs, runner):
    zfs.pool_status()
    zfs.scrub_start()
    zfs.scrub_stop()
    assert [c[-1] for c in runner.calls] == ["tank", "tank", "tank"]
