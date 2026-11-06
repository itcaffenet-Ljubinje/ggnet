"""Backup and restore of images (app.services.backups, app.api.v1.backups)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.db.models import BackupJob, Image, ImageHardware
from app.db.session import make_engine
from app.main import app
from app.services import backups

PART = 1000


class FakeStream:
    """A 'zfs send -R' stream: the image's versions as JSON, padded; receive makes them on the fake host."""

    def __init__(self, host, padding: int = 2500):
        self.host, self.padding = host, padding

    def _head(self, snapshot: str) -> bytes:
        zvol = snapshot.partition("@")[0]
        versions = [s.partition("@")[2] for s in self.host.snapshots if s.partition("@")[0] == zvol]
        props = {v: self.host.snap_props.get(f"{zvol}@{v}", {}) for v in versions}
        head = json.dumps({"versions": versions, "props": props}).encode()
        return len(head).to_bytes(4, "big") + head

    def _data(self, snapshot: str) -> bytes:
        return self._head(snapshot) + b"x" * self.padding

    def send_size(self, snapshot):
        return len(self._head(snapshot)) + self.padding

    def send(self, snapshot, cancel):
        data = self._data(snapshot)
        for i in range(0, len(data), 700):
            if cancel.is_set():
                raise backups.Cancelled("cancelled")
            yield data[i:i + 700]

    def receive(self, target, chunks, cancel):
        data = b"".join(chunks)
        n = int.from_bytes(data[:4], "big")
        head = json.loads(data[4:4 + n])
        self.host.add_dataset(target)
        for v in head["versions"]:
            self.host.snapshots[f"{target}@{v}"] = set()
            if head["props"].get(v):
                self.host.snap_props[f"{target}@{v}"] = dict(head["props"][v])


@pytest.fixture
def place(tmp_path):
    folder = tmp_path / "usb"
    folder.mkdir()
    return folder


@pytest.fixture
def runner(client, host, prov, db_url, place, monkeypatch):
    engine = make_engine(db_url)
    r = backups.BackupRunner(sessionmaker(bind=engine, expire_on_commit=False), prov, FakeStream(host),
                             background=False, part_bytes=PART)
    locs = backups.BackupLocations(type("NoUsb", (), {"list": lambda self: []})(), [place])
    app.dependency_overrides[deps.get_backup_locations] = lambda: locs
    app.dependency_overrides[deps.get_backup_runner] = lambda: setattr(r, "open_location", locs.open) or r
    yield r
    engine.dispose()


def _image(client, host, name="win11", kind="os", versions=("v2",)) -> dict:
    d = client.post("/api/v1/images", json={"name": name, "size_gb": 80, "kind": kind}).json()
    client.post(f"/api/v1/images/{d['id']}/publish")
    for v in versions:
        host.snapshots[f"{d['zvol_path']}@{v}"] = set()
    host.snap_props[f"{d['zvol_path']}@base"] = {"ggnet:comment": "AB clean install"}
    return d


def _add_card(db_url, image_id):
    engine = make_engine(db_url)
    with sessionmaker(bind=engine)() as db:
        db.add(ImageHardware(image_id=image_id, nic="VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10",
                             instance="3&11583659&0&FE", name="Intel I219-V", snapshot="base"))
        image = db.get(Image, image_id)
        image.snapshot = "v2"
        db.commit()
    engine.dispose()


# ── Pieces ────────────────────────────────────────────────────────────

def test_the_stream_is_written_in_parts_and_read_back_checked(tmp_path):
    writer = backups.PartWriter(tmp_path, part_bytes=10)
    for piece in (b"0123456", b"789abcdefghij", b"klmnopqrstuvwxyz"):
        writer.write(piece)
    writer.finish()
    assert [p["bytes"] for p in writer.parts] == [10, 10, 10, 6]
    manifest = {"stream": {"parts": writer.parts}}
    seen = []
    assert b"".join(backups.read_parts(tmp_path, manifest, seen.append)) == b"0123456789abcdefghijklmnopqrstuvwxyz"
    assert seen[-1] == 36
    (tmp_path / "stream.002").write_bytes(b"XXXXXXXXXX")
    with pytest.raises(backups.BackupError, match="stream.002 is damaged"):
        list(backups.read_parts(tmp_path, manifest, lambda n: None))


# ── Backup, then restore under a new name ─────────────────────────────

def test_an_image_is_backed_up_with_every_version_and_restored_as_a_new_image(client, host, db_url, runner, place):
    win11 = _image(client, host)
    _add_card(db_url, win11["id"])
    assert client.get(f"/api/v1/backups/estimate?image_id={win11['id']}").json()["bytes"] > 2500

    r = client.post("/api/v1/backups", json={"image_ids": [win11["id"]], "location": "dir:0"})
    assert r.status_code == 201, r.text
    [job] = client.get("/api/v1/backups/jobs").json()
    assert (job["kind"], job["status"], job["error"]) == ("backup", "done", None)
    folder = Path(job["folder"])
    assert folder.parent == place / "ggnet-backups" and folder.name.startswith("win11-")
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["versions"] == ["base", "v2"]
    assert manifest["image"]["kind"] == "os" and manifest["image"]["snapshot"] == "v2"
    assert manifest["image"]["hardware"][0]["name"] == "Intel I219-V"
    assert len(manifest["stream"]["parts"]) == 3                  # 1000-byte parts in this test
    assert sorted(p.name for p in folder.iterdir()) == ["manifest.json", "stream.000", "stream.001", "stream.002"]

    opened = client.post("/api/v1/backups/locations/dir:0/open").json()
    [found] = opened["backups"]
    assert (found["image"], found["complete"], found["versions"]) == ("win11", True, ["base", "v2"])

    taken = client.post("/api/v1/backups/restore", json={"location": "dir:0", "folder": found["folder"],
                                                         "name": "win11"})
    assert taken.status_code == 409 and "already exists" in taken.text
    r = client.post("/api/v1/backups/restore", json={"location": "dir:0", "folder": found["folder"],
                                                     "name": "win11-restored"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "done", r.json()
    restored = next(d for d in client.get("/api/v1/images").json() if d["name"] == "win11-restored")
    assert (restored["kind"], restored["snapshot"], restored["published"]) == ("os", "v2", True)
    assert restored["copy_source"] is None
    assert restored["nic_models"] == ["Intel I219-V"]
    zvol = "tank/ggnet/images/win11-restored"
    assert host.datasets[zvol]["readonly"] is True
    assert all("ggnet:protected" in host.snapshots[f"{zvol}@{v}"] for v in ("base", "v2"))
    assert host.snap_props[f"{zvol}@base"]["ggnet:comment"] == "AB clean install"


def test_a_damaged_backup_leaves_nothing_behind(client, host, runner, place):
    game = _image(client, host, "cs2", "game", versions=())
    client.post("/api/v1/backups", json={"image_ids": [game["id"]], "location": "dir:0"})
    [folder] = (place / "ggnet-backups").iterdir()
    part = folder / "stream.001"
    part.write_bytes(b"y" * part.stat().st_size)                  # same size, other bytes
    r = client.post("/api/v1/backups/restore", json={"location": "dir:0", "folder": str(folder), "name": "cs2-b"})
    assert r.json()["status"] == "failed" and "stream.001 is damaged" in r.json()["error"]
    assert all(d["name"] != "cs2-b" for d in client.get("/api/v1/images").json())
    assert "tank/ggnet/images/cs2-b" not in host.datasets


def test_a_backup_that_does_not_fit_is_not_started(client, host, runner, place, monkeypatch):
    game = _image(client, host, "cs2", "game", versions=())
    runner.stream.padding = 10 ** 15
    client.post("/api/v1/backups", json={"image_ids": [game["id"]], "location": "dir:0"})
    [job] = client.get("/api/v1/backups/jobs").json()
    assert job["status"] == "failed" and "not enough free space" in job["error"]
    assert not (place / "ggnet-backups").exists() or not any((place / "ggnet-backups").iterdir())


def test_an_unfinished_backup_shows_no_backup_and_is_cleaned_up_after_a_restart(client, host, db_url, place):
    unfinished = place / "ggnet-backups" / "cs2-20261106-120000"
    unfinished.mkdir(parents=True)
    (unfinished / "stream.000").write_bytes(b"x")
    assert backups.find_backups(place) == []
    engine = make_engine(db_url)
    make = sessionmaker(bind=engine, expire_on_commit=False)
    with make() as db:
        db.add(BackupJob(kind="backup", image_name="cs2", location="dir:0", folder=str(unfinished), status="running"))
        db.commit()
    backups.recover(make)
    with make() as db:
        job = db.query(BackupJob).one()
        assert job.status == "failed" and "interrupted" in job.error
    engine.dispose()
    assert not unfinished.exists()


def test_a_running_backup_can_be_cancelled(client, host, db_url, prov, place):
    engine = make_engine(db_url)
    make = sessionmaker(bind=engine, expire_on_commit=False)
    game = _image(client, host, "cs2", "game", versions=())
    r = backups.BackupRunner(make, prov, FakeStream(host), background=False, part_bytes=PART,
                             open_location=lambda loc, w: place)
    with make() as db:
        db.add(BackupJob(kind="backup", image_id=game["id"], image_name="cs2", location="dir:0"))
        db.commit()
        job_id = db.query(BackupJob).one().id
    event = threading.Event()
    event.set()
    r._cancel[job_id] = event                  # cancelled as soon as it starts
    r.run()
    with make() as db:
        assert db.get(BackupJob, job_id).status == "cancelled"
    engine.dispose()
    assert not any((place / "ggnet-backups").iterdir())
