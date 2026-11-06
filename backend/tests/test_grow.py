"""Growing a disk: PartitionGrower (scripted runner) and POST /images/{id}/grow (FakeHost)."""

from __future__ import annotations

import json

import pytest

from app.disk_grow import BASIC_DATA, RECOVERY, GrowResult, PartitionGrower

MASTER = "tank/ggnet/images/cs2"
DEV = "/dev/zd896"


class Script:
    """A runner that answers by command name and records every call."""

    def __init__(self, table: dict, fail: tuple[str, ...] = ()):
        self.table, self.fail, self.calls = table, fail, []

    def run(self, cmd, timeout=None, quiet=False):
        self.calls.append(cmd)
        if tuple(cmd[: len(self.fail)]) == self.fail and self.fail:
            return False, "", "refused"
        return True, self.table.get(cmd[0], ""), ""


def _table(*parts, last_lba=614366):
    return json.dumps({"partitiontable": {"label": "gpt", "lastlba": last_lba, "partitions": [
        {"node": f"{DEV}p{n}", "start": start, "size": size, "type": t, "uuid": f"UUID-{n}",
         **({"name": name} if name else {})}
        for n, start, size, t, name in parts]}})


@pytest.fixture(autouse=True)
def real_path(monkeypatch):
    monkeypatch.setattr("app.disk_grow._real_device", lambda p: DEV if p.startswith("/dev/zvol/") else p)


def test_grows_the_last_data_partition_and_its_ntfs():
    r = Script({"sfdisk": _table((1, 2048, 32768, "E3C9E316-0B5C-4DB8-817D-F92DF00215AE", None),
                                 (2, 34816, 374751, BASIC_DATA, "Basic data partition"))})
    result = PartitionGrower(r).extend_last("/dev/zvol/tank/ggnet/writebacks/cs2_ggnet_grow")
    assert result.extended
    assert r.calls == [
        ["sgdisk", "-e", DEV],
        ["sfdisk", "--json", DEV],
        ["sgdisk", "-d", "2", "-n", "2:34816:0", "-t", f"2:{BASIC_DATA}", "-u", "2:UUID-2",
         "-c", "2:Basic data partition", DEV],
        ["blockdev", "--rereadpt", DEV],
        ["udevadm", "settle"],
        ["ntfsresize", "--no-action", "--force", f"{DEV}p2"],
        ["ntfsresize", "--force", f"{DEV}p2"],
    ]


def test_an_os_image_moves_recovery_to_the_end_and_grows_c():
    r = Script({"sfdisk": _table((1, 2048, 409600, "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", None),
                                 (3, 450560, 160000000, BASIC_DATA, "Basic data partition"),
                                 (4, 160450560, 1826816, RECOVERY, None),
                                 last_lba=314572766)})
    data = json.loads(r.table["sfdisk"])
    data["partitiontable"]["partitions"][2]["attrs"] = "RequiredPartition GUID:63"
    r.table["sfdisk"] = json.dumps(data)
    result = PartitionGrower(r).extend_last(DEV)
    assert result.extended and "Recovery partition was moved" in result.note
    new = (314572766 + 1 - 1826816) // 2048 * 2048        # aligned, at the end of the disk
    src, dst, size = 160450560 * 512, new * 512, 1826816 * 512
    assert ["dd", f"if={DEV}", f"of={DEV}", "bs=4M", f"skip={src}", f"seek={dst}", f"count={size}",
            "iflag=skip_bytes,count_bytes", "oflag=seek_bytes", "conv=notrunc,fsync", "status=none"] in r.calls
    assert ["cmp", "-n", str(size), "-i", f"{src}:{dst}", DEV, DEV] in r.calls
    assert ["sgdisk", "-d", "3", "-d", "4",
            "-n", f"4:{new}:{new + 1826816 - 1}", "-t", f"4:{RECOVERY}", "-u", "4:UUID-4",
            "-A", "4:set:0", "-A", "4:set:63",
            "-n", f"3:450560:{new - 1}", "-t", f"3:{BASIC_DATA}", "-u", "3:UUID-3", "-c", "3:Basic data partition",
            DEV] in r.calls
    assert ["ntfsresize", "--force", f"{DEV}p3"] in r.calls


def test_a_bad_recovery_copy_leaves_the_table_alone():
    r = Script({"sfdisk": _table((3, 450560, 160000000, BASIC_DATA, None),
                                 (4, 160450560, 1826816, RECOVERY, None), last_lba=314572766)},
               fail=("cmp",))
    result = PartitionGrower(r).extend_last(DEV)
    assert not result.extended and "differs" in result.note
    assert not any(c[0] == "sgdisk" and "-d" in c for c in r.calls)


def test_an_unknown_layout_is_left_for_windows():
    r = Script({"sfdisk": _table((3, 450560, 160000000, BASIC_DATA, None),
                                 (4, 160450560, 1826816, "0FC63DAF-8483-4772-8E79-3D69D8477DE4", None),
                                 last_lba=314572766)})
    result = PartitionGrower(r).extend_last(DEV)
    assert not result.extended and "in Windows" in result.note
    assert not any(c[0] in ("dd", "ntfsresize") for c in r.calls)


def test_nothing_to_do_without_new_space():
    r = Script({"sfdisk": _table((2, 34816, 579551, BASIC_DATA, None))})
    assert PartitionGrower(r).extend_last(DEV) == GrowResult(False, "there is no new space behind the last partition")


def test_ntfs_check_failure_stops_before_the_real_resize():
    r = Script({"sfdisk": _table((2, 34816, 374751, BASIC_DATA, None))}, fail=("ntfsresize", "--no-action"))
    result = PartitionGrower(r).extend_last(DEV)
    assert not result.extended and "NTFS refused" in result.note
    assert ["ntfsresize", "--force", f"{DEV}p2"] not in r.calls


# ── API ───────────────────────────────────────────────────────────────

class FakeGrower:
    def __init__(self, result: GrowResult):
        self.result, self.devices = result, []

    def extend_last(self, device):
        self.devices.append(device)
        return self.result


def _disk(client, publish=True) -> dict:
    d = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 100}).json()
    return client.post(f"/api/v1/images/{d['id']}/publish").json() if publish else d


def test_growing_a_published_disk_makes_a_new_active_version(client, host, prov):
    prov.grower = FakeGrower(GrowResult(True, "done"))
    d = _disk(client)
    r = client.post(f"/api/v1/images/{d['id']}/grow", json={"size_gb": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["version"], body["extended"], body["disk"]["size_gb"], body["disk"]["snapshot"]) == \
        ("v2", True, 200, "v2")
    assert "extended too" in body["note"]
    assert prov.grower.devices == ["/dev/zvol/tank/ggnet/writebacks/cs2_ggnet_grow"]
    assert ["zfs", "set", "volsize=200G", "tank/ggnet/writebacks/cs2_ggnet_grow"] in host.calls
    # The clone became the master; nothing is left in writebacks.
    assert f"{MASTER}@v2" in host.snapshots and "tank/ggnet/writebacks/cs2_ggnet_grow" not in host.datasets
    assert host.snap_props[f"{MASTER}@v2"]["ggnet:comment"] == "ggNet: grown from 100 to 200 GB, data partition extended"


def test_an_os_image_grows_and_says_to_extend_c_in_windows(client, prov):
    prov.grower = FakeGrower(GrowResult(False, "the last partition is not a data partition"))
    d = _disk(client)
    r = client.post(f"/api/v1/images/{d['id']}/grow", json={"size_gb": 150})
    assert r.status_code == 200, r.text
    assert r.json()["extended"] is False and "Extend Volume" in r.json()["note"]


def test_extend_off_only_grows_the_zvol(client, prov):
    prov.grower = FakeGrower(GrowResult(True, "done"))
    d = _disk(client)
    r = client.post(f"/api/v1/images/{d['id']}/grow", json={"size_gb": 150, "extend": False})
    assert r.status_code == 200 and prov.grower.devices == []


def test_a_draft_grows_in_place(client, host):
    d = _disk(client, publish=False)
    r = client.post(f"/api/v1/images/{d['id']}/grow", json={"size_gb": 150})
    assert r.status_code == 200, r.text
    assert (r.json()["version"], r.json()["disk"]["size_gb"]) == (None, 150)
    assert ["zfs", "set", "volsize=150G", MASTER] in host.calls


def test_a_disk_never_shrinks_and_follows_the_newest_version(client, host):
    d = _disk(client)
    url = f"/api/v1/images/{d['id']}/grow"
    assert client.post(url, json={"size_gb": 100}).status_code == 409
    assert client.post(url, json={"size_gb": 50}).status_code == 409
    assert client.post(url, json={"size_gb": 99999}).status_code == 422
    host.snapshots[f"{MASTER}@v2"] = {"ggnet:protected"}
    r = client.post(url, json={"size_gb": 200})
    assert r.status_code == 409 and "make @v2 active first" in r.json()["detail"]["error"]


def test_a_failed_resize_changes_nothing(client, host, prov):
    prov.grower = FakeGrower(GrowResult(True, "done"))
    host.fail_on[("zfs", "set", "volsize=200G")] = "out of space"
    d = _disk(client)
    r = client.post(f"/api/v1/images/{d['id']}/grow", json={"size_gb": 200})
    assert r.status_code == 502
    after = client.get(f"/api/v1/images/{d['id']}").json()
    assert (after["size_gb"], after["snapshot"]) == (100, "base")
    assert "tank/ggnet/writebacks/cs2_ggnet_grow" not in host.datasets
