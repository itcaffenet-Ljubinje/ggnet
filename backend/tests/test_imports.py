"""Import of image files (app.services.imports, /api/v1/imports), through TestClient and FakeHost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_disk_importer, get_usb_disks
from app.db.session import make_engine
from app.main import app
from app.runner import CommandRunner
from app.services import imports
from app.services.imports import DiskImporter, UsbDisks, list_images, qemu_percent, usb_partitions

GiB = 1024 ** 3
IMPORTS = "tank/ggnet/imports"


@pytest.fixture
def folder(host, tmp_path) -> Path:
    """The imports dataset, mounted at a temporary folder."""
    path = tmp_path / "imports"
    path.mkdir()
    host.add_dataset(IMPORTS)
    host.datasets[IMPORTS]["props"] = {"mountpoint": str(path)}
    return path


@pytest.fixture
def importer(client, db_url, prov):
    engine = make_engine(db_url)
    i = DiskImporter(sessionmaker(bind=engine, expire_on_commit=False), prov, background=False)
    app.dependency_overrides[get_disk_importer] = lambda: i
    yield i
    engine.dispose()


def _file(host, folder: Path, name: str, size: int = 60 * GiB + 1) -> Path:
    f = folder / name
    f.write_bytes(b"image")
    host.image_files[str(f.resolve())] = size
    host.image_files[str(f)] = size
    return f


# ── Files, USB disks, progress ────────────────────────────────────────

def test_list_images_finds_image_files_only(tmp_path):
    (tmp_path / "pc1").mkdir()
    (tmp_path / "pc1" / "C.VHDX").write_bytes(b"x" * 10)
    (tmp_path / "games.qcow2").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".games.vhdx.part").write_bytes(b"x")           # an upload in progress
    found = list_images(tmp_path)
    assert [(Path(f.path).name, f.format, f.size) for f in found] == [("games.qcow2", "qcow2", 1), ("C.VHDX", "vhdx", 10)]


def test_usb_partitions_offer_only_free_usb_file_systems():
    lsblk = {"blockdevices": [
        {"name": "sda", "path": "/dev/sda", "tran": "sas", "type": "disk", "size": 1, "children": [
            {"name": "sda1", "path": "/dev/sda1", "fstype": "zfs_member", "type": "part", "size": 1}]},
        {"name": "sdj", "path": "/dev/sdj", "tran": "usb", "type": "disk", "model": "SanDisk ", "size": 2, "children": [
            {"name": "sdj1", "path": "/dev/sdj1", "fstype": "exfat", "label": "IMAGES", "type": "part", "size": 2}]},
        {"name": "sdk", "path": "/dev/sdk", "tran": "usb", "type": "disk", "size": 3, "children": [
            {"name": "sdk1", "path": "/dev/sdk1", "fstype": "ntfs", "type": "part", "size": 3,
             "mountpoint": "/run/ggnet/usb/sdk1"}]},
        {"name": "sdl", "path": "/dev/sdl", "tran": "usb", "type": "disk", "size": 4, "children": [
            {"name": "sdl1", "path": "/dev/sdl1", "fstype": "vfat", "type": "part", "size": 4,
             "mountpoint": "/boot/efi"}]},                                         # in use by the host
        {"name": "sdm", "path": "/dev/sdm", "tran": "usb", "type": "disk", "size": 5, "children": [
            {"name": "sdm1", "path": "/dev/sdm1", "fstype": "ntfs", "type": "part", "size": 5},
            {"name": "sdm2", "path": "/dev/sdm2", "fstype": "LVM2_member", "type": "part", "size": 5}]},
    ]}
    parts = usb_partitions(json.dumps(lsblk), Path("/run/ggnet/usb"))
    assert [(p["name"], p["fstype"], p["model"], p["mounted"]) for p in parts] == [
        ("sdj1", "exfat", "SanDisk", None),
        ("sdk1", "ntfs", None, "/run/ggnet/usb/sdk1"),
    ]
    assert usb_partitions("garbage") == []


def test_usb_open_is_read_only_and_eject_unmounts(tmp_path):
    class Runner:
        calls: list = []

        def run(self, cmd, timeout=None, quiet=False):
            self.calls.append(cmd)
            if cmd[0] == "lsblk":
                mounted = str(tmp_path / "sdj1") if any(c[0] == "ntfs-3g" for c in self.calls) else None
                return True, json.dumps({"blockdevices": [
                    {"name": "sdj", "tran": "usb", "type": "disk", "size": 1, "children": [
                        {"name": "sdj1", "path": "/dev/sdj1", "fstype": "ntfs", "type": "part", "size": 1,
                         "mountpoint": mounted}]}]}), ""
            return True, "", ""

    r = Runner()
    usb = UsbDisks(r, tmp_path)
    assert usb.mount("sdj1") == tmp_path / "sdj1"
    assert ["ntfs-3g", "-o", "ro", "/dev/sdj1", str(tmp_path / "sdj1")] in r.calls
    usb.unmount("sdj1")
    assert ["umount", str(tmp_path / "sdj1")] in r.calls
    with pytest.raises(ValueError):
        usb.mount("sda1")


def test_qemu_progress_lines():
    assert qemu_percent("(42.17/100%)") == 42.17
    assert qemu_percent("Image resized.") is None


def test_run_stream_reports_progress_and_errors():
    seen = []
    ok, _, err = CommandRunner().run_stream(
        ["sh", "-c", 'printf "    (0.00/100%%)\\r    (100.00/100%%)\\n"; echo note >&2'], seen.append)
    assert (ok, err, seen) == (True, "note", ["(0.00/100%)", "(100.00/100%)"])
    ok, _, err = CommandRunner().run_stream(["sh", "-c", "echo broken >&2; exit 3"], seen.append)
    assert (ok, err) == (False, "broken")


# ── API ───────────────────────────────────────────────────────────────

def test_sources_list_the_folder_and_usb(client, host, folder):
    _file(host, folder, "win11.vhdx")
    app.dependency_overrides[get_usb_disks] = lambda: type("U", (), {"list": lambda self: [], "mounts": Path("/x")})()
    r = client.get("/api/v1/imports")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["folder"] == str(folder) and [f["name"] for f in body["files"]] == ["win11.vhdx"]


def test_import_makes_a_published_image_of_the_files_size(client, host, folder, importer):
    f = _file(host, folder, "win11.vhdx", size=60 * GiB + 1)
    r = client.post("/api/v1/imports", json={"name": "Win11-Real", "kind": "os", "path": str(f)})
    assert r.status_code == 201, r.text
    d = client.get(f"/api/v1/game-disks/{r.json()['id']}").json()
    assert (d["name"], d["kind"], d["size_gb"], d["snapshot"], d["copy_source"]) == ("win11-real", "os", 61, "base", None)
    master = "tank/ggnet/images/win11-real"
    assert host.datasets[master]["readonly"] is True and f"{master}@base" in host.snapshots
    assert ["qemu-img", "convert", "-p", "-n", "--target-is-zero", "-f", "vhdx", "-O", "raw",
            str(f.resolve()), f"/dev/zvol/{master}"] in host.calls


def test_a_failed_import_keeps_the_error_and_removes_the_zvol(client, host, folder, importer):
    f = _file(host, folder, "broken.vhd")
    host.fail_on[("qemu-img", "convert")] = "qemu-img: Could not read image: Invalid argument"
    r = client.post("/api/v1/imports", json={"name": "broken", "kind": "game", "path": str(f)})
    d = client.get(f"/api/v1/game-disks/{r.json()['id']}").json()
    assert d["published"] is False and "Invalid argument" in d["copy_error"]
    assert "tank/ggnet/images/broken" not in host.datasets


def test_import_refuses_files_outside_the_folder_or_unknown(client, host, folder, importer, tmp_path):
    outside = tmp_path / "secret.vhdx"
    outside.write_bytes(b"x")
    host.image_files[str(outside)] = GiB
    assert client.post("/api/v1/imports", json={"name": "x", "path": str(outside)}).status_code == 404
    (folder / "notes.txt").write_text("x")
    assert client.post("/api/v1/imports", json={"name": "x", "path": str(folder / "notes.txt")}).status_code == 404
    unreadable = folder / "empty.vhdx"
    unreadable.write_bytes(b"")
    r = client.post("/api/v1/imports", json={"name": "x", "path": str(unreadable)})
    assert r.status_code == 409 and "not a readable" in r.text
    f = _file(host, folder, "ok.vhdx")
    client.post("/api/v1/game-disks", json={"name": "taken", "size_gb": 1})
    assert client.post("/api/v1/imports", json={"name": "taken", "path": str(f)}).status_code == 409


def test_upload_writes_the_file_into_the_folder(client, folder):
    r = client.put("/api/v1/imports/upload/pc07.vhdx", content=b"VHDX data")
    assert r.status_code == 201, r.text
    assert (folder / "pc07.vhdx").read_bytes() == b"VHDX data"
    assert not list(folder.glob(".*.part"))
    assert client.put("/api/v1/imports/upload/pc07.vhdx", content=b"again").status_code == 409
    assert client.put("/api/v1/imports/upload/notes.txt", content=b"x").status_code == 409
    # A name with a path in it never writes outside the folder (refused or not even routed).
    assert client.put("/api/v1/imports/upload/..%2Fx.vhdx", content=b"x").status_code >= 400
    assert client.put("/api/v1/imports/upload/a b%2F..%2F..%2Fx.vhdx", content=b"x").status_code >= 400
    assert not (folder.parent / "x.vhdx").exists()


def test_a_file_in_the_folder_can_be_deleted(client, folder):
    (folder / "old.vhdx").write_bytes(b"x")
    assert client.delete("/api/v1/imports/file", params={"path": str(folder / "old.vhdx")}).status_code == 204
    assert not (folder / "old.vhdx").exists()
    assert client.delete("/api/v1/imports/file", params={"path": "/etc/passwd"}).status_code == 404


def test_without_the_imports_dataset_the_folder_is_none(client, host):
    app.dependency_overrides[get_usb_disks] = lambda: type("U", (), {"list": lambda self: []})()
    assert client.get("/api/v1/imports").json()["folder"] is None
    assert client.put("/api/v1/imports/upload/a.vhdx", content=b"x").status_code == 502


def test_usb_mounts_stay_under_run_ggnet():
    assert imports.USB_MOUNTS == Path("/run/ggnet/usb")


# ── After the import: Prepare image; cancel ───────────────────────────

def test_an_imported_os_image_is_prepared_as_its_next_version(client, host, folder, importer, prov):
    from tests.test_boot_mode import FakeWindows

    prov.windows = FakeWindows(host)
    prov.windows.prep_problem = None
    f = _file(host, folder, "pc07.vhdx")
    r = client.post("/api/v1/imports", json={"name": "win11-pc07", "path": str(f), "defender_off": True})
    d = client.get(f"/api/v1/game-disks/{r.json()['id']}").json()
    assert (d["snapshot"], d["copy_source"]) == ("v2", None)
    [(_, prep, _)] = prov.windows.prepared
    assert prep.defender_off is True
    versions = {v["name"]: v["comment"] for v in client.get(f"/api/v1/game-disks/{d['id']}/snapshots").json()}
    assert versions["base"] == "ggNet: imported from pc07.vhdx"
    assert versions["v2"] == "ggNet: prepared for all PCs after the import, Defender off"


def test_prepare_can_be_skipped_and_a_failed_prepare_keeps_base(client, host, folder, importer, prov):
    f = _file(host, folder, "a.vhdx")
    r = client.post("/api/v1/imports", json={"name": "plain", "path": str(f), "prepare": False})
    assert client.get(f"/api/v1/game-disks/{r.json()['id']}").json()["snapshot"] == "base"
    prov.windows = None                                   # Windows setup not available here
    f = _file(host, folder, "b.vhdx")
    r = client.post("/api/v1/imports", json={"name": "unprepared", "path": str(f)})
    d = client.get(f"/api/v1/game-disks/{r.json()['id']}").json()
    assert (d["snapshot"], d["copy_error"]) == ("base", None)
    base = client.get(f"/api/v1/game-disks/{d['id']}/snapshots").json()[0]
    assert "Prepare image failed" in base["comment"]


def test_a_game_disk_import_is_never_prepared(client, host, folder, importer, prov):
    f = _file(host, folder, "games.qcow2")
    r = client.post("/api/v1/imports", json={"name": "games2", "kind": "game", "path": str(f)})
    assert client.get(f"/api/v1/game-disks/{r.json()['id']}").json()["snapshot"] == "base"


def test_a_cancelled_import_fails_and_removes_its_zvol(host, prov, folder):
    import threading

    f = _file(host, folder, "big.vhdx")
    host.add_dataset("tank/ggnet/images/big")
    flag = threading.Event()
    flag.set()
    assert prov.import_disk(str(f), "vhdx", "tank/ggnet/images/big", cancel=flag) is False
    assert host.last_error == "Cancelled" and "tank/ggnet/images/big" not in host.datasets


def test_cancel_needs_a_running_import(client, host, folder, importer):
    f = _file(host, folder, "done.vhdx")
    r = client.post("/api/v1/imports", json={"name": "done", "kind": "game", "path": str(f)})
    assert client.post(f"/api/v1/imports/{r.json()['id']}/cancel").status_code == 409
    assert client.post("/api/v1/imports/999/cancel").status_code == 404
    assert imports.cancel(12345) is False
