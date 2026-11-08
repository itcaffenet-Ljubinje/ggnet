"""Copy of an image (POST /images/{id}/copy), through TestClient and FakeHost."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_disk_copier
from app.db.models import Image
from app.db.session import make_engine
from app.main import app
from app.services import copies
from app.services.copies import DiskCopier

MASTER = "tank/ggnet/images/cs2"
COPY = "tank/ggnet/images/cs2-copy"


@pytest.fixture
def make_session(db_url):
    engine = make_engine(db_url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def copier(client, make_session, prov):
    """Copies run inside the request, so the test sees the result."""
    c = DiskCopier(make_session, prov, background=False)
    app.dependency_overrides[get_disk_copier] = lambda: c
    return c


@pytest.fixture
def disk(client) -> dict:
    d = client.post("/api/v1/images", json={"name": "cs2", "size_gb": 10}).json()
    return client.post(f"/api/v1/images/{d['id']}/publish").json()


def test_copy_makes_an_independent_published_image(client, host, disk, copier):
    host.snap_props[f"{MASTER}@base"] = {"ggnet:comment": "first install"}
    r = client.post(f"/api/v1/images/{disk['id']}/copy", json={"name": "CS2-Copy"})
    assert r.status_code == 201, r.text
    assert (r.json()["name"], r.json()["zvol_path"]) == ("cs2-copy", COPY)

    d = client.get(f"/api/v1/images/{r.json()['id']}").json()
    assert (d["published"], d["snapshot"], d["copy_source"], d["copy_error"]) == (True, "base", None, None)
    assert (d["kind"], d["size_gb"]) == ("game", 10)
    # Its own data, held and read-only like any published master; no clone of the source.
    assert host.datasets[COPY]["origin"] is None and host.datasets[COPY]["readonly"] is True
    assert host.snapshots[f"{COPY}@base"] == {"ggnet:protected"}
    assert ["zfs", "send", "-c", "-P", "-v", f"{MASTER}@base", "|", "zfs", "recv", "-u", COPY] in host.calls


def test_copy_of_another_version_becomes_base(client, host, disk, copier):
    host.snapshots[f"{MASTER}@v2"] = {"ggnet:protected"}
    r = client.post(f"/api/v1/images/{disk['id']}/copy", json={"name": "cs2-copy", "snapshot": "v2"})
    assert r.status_code == 201, r.text
    assert f"{COPY}@base" in host.snapshots and f"{COPY}@v2" not in host.snapshots
    assert ["zfs", "rename", f"{COPY}@v2", f"{COPY}@base"] in host.calls


def test_a_copy_can_keep_every_version_and_the_active_one(client, host, disk, copier, db_url):
    for v in ("v2", "v3"):
        host.snapshots[f"{MASTER}@{v}"] = {"ggnet:protected"}
    host.snap_props[f"{MASTER}@v2"] = {"ggnet:comment": "AB new drivers"}
    client.put(f"/api/v1/images/{disk['id']}/active-snapshot", json={"snapshot": "v2"})
    r = client.post(f"/api/v1/images/{disk['id']}/copy", json={"name": "cs2-copy", "all_versions": True})
    assert r.status_code == 201, r.text
    d = client.get(f"/api/v1/images/{r.json()['id']}").json()
    assert (d["published"], d["snapshot"], d["copy_source"]) == (True, "v2", None)      # same active version
    assert [s for s in host.snapshots if s.startswith(f"{COPY}@")] == [f"{COPY}@base", f"{COPY}@v2", f"{COPY}@v3"]
    assert all(host.snapshots[f"{COPY}@{v}"] == {"ggnet:protected"} for v in ("base", "v2", "v3"))
    assert host.snap_props[f"{COPY}@v2"]["ggnet:comment"] == "AB new drivers"
    assert host.datasets[COPY]["readonly"] is True
    assert ["zfs", "send", "-R", "-c", "-P", "-v", f"{MASTER}@v3", "|", "zfs", "recv", "-u", COPY] in host.calls
    versions = client.get(f"/api/v1/images/{r.json()['id']}/snapshots").json()
    assert [(v["name"], v["active"]) for v in versions] == [("base", False), ("v2", True), ("v3", False)]


def test_copy_reports_progress(make_session, prov, host, client, disk):
    seen = []
    c = DiskCopier(make_session, prov, background=False)
    real = c._progress

    def spy(image_id):
        update = real(image_id)

        def wrapped(done, total):
            seen.append((done, total))
            update(done, total)

        return wrapped

    c._progress = spy
    app.dependency_overrides[get_disk_copier] = lambda: c
    host.copy_bytes = 4000
    assert client.post(f"/api/v1/images/{disk['id']}/copy", json={"name": "cs2-copy"}).status_code == 201
    assert seen == [(2000, 4000), (4000, 4000)]


def test_copy_refuses_bad_requests(client, host, disk, copier):
    url = f"/api/v1/images/{disk['id']}/copy"
    assert client.post(url, json={"name": "cs2"}).status_code == 409             # taken
    assert client.post(url, json={"name": "x", "snapshot": "v9"}).status_code == 404
    assert client.post(url, json={"name": "a b"}).status_code == 422
    host.add_dataset(COPY)
    assert client.post(url, json={"name": "cs2-copy"}).status_code == 409        # zvol on the host
    draft = client.post("/api/v1/images", json={"name": "draft", "size_gb": 1}).json()
    assert client.post(f"/api/v1/images/{draft['id']}/copy", json={"name": "y"}).status_code == 409


def test_a_failed_copy_is_kept_with_its_error_and_can_be_removed(client, host, disk, copier):
    host.fail_on[("zfs", "recv")] = "cannot receive: out of space"
    r = client.post(f"/api/v1/images/{disk['id']}/copy", json={"name": "cs2-copy"})
    assert r.status_code == 201
    d = client.get(f"/api/v1/images/{r.json()['id']}").json()
    assert d["published"] is False and "out of space" in d["copy_error"]
    assert COPY not in host.datasets
    # A failed copy is not a draft to publish; it can only be removed.
    assert client.post(f"/api/v1/images/{d['id']}/publish").status_code == 409
    assert client.delete(f"/api/v1/images/{d['id']}").status_code == 204


def test_a_running_copy_cannot_be_edited_published_or_removed(client, make_session, disk):
    with make_session() as db:
        d = Image(name="busy", zvol_path="tank/ggnet/images/busy", size_gb=10,
                     copy_source=f"{MASTER}@base", copy_percent=42)
        db.add(d)
        db.commit()
        image_id = d.id
    assert client.get(f"/api/v1/images/{image_id}").json()["copy_percent"] == 42
    assert client.post(f"/api/v1/images/{image_id}/publish").status_code == 409
    assert client.delete(f"/api/v1/images/{image_id}").status_code == 409


def test_recover_marks_an_interrupted_copy_failed_and_removes_the_partial_zvol(
        client, host, make_session, prov):
    host.add_dataset("tank/ggnet/images/busy")
    with make_session() as db:
        db.add(Image(name="busy", zvol_path="tank/ggnet/images/busy", size_gb=10,
                        copy_source=f"{MASTER}@base", copy_percent=42))
        db.commit()
    copies.recover(make_session, prov)
    with make_session() as db:
        d = db.query(Image).filter_by(name="busy").one()
        assert d.copy_error.startswith("Interrupted") and d.copy_percent is None
    assert "tank/ggnet/images/busy" not in host.datasets


def test_progress_of_a_copy_with_all_versions_adds_up_the_versions(prov, host):
    host.snapshots["tank/ggnet/images/cs2@base"] = set()
    host.add_dataset("tank/ggnet/images/cs2")
    seen = []

    def pipe(producer, consumer, timeout=None, on_producer_line=None):
        for line in ("size\t300", "00:00:01\t100\tcs2@base", "00:00:02\t150\tcs2@base",   # base: 150
                     "00:00:03\t50\tcs2@v2", "00:00:04\t150\tcs2@v2"):                    # v2 starts at 0 again
            on_producer_line(line)
        host.add_dataset(consumer[-1])
        host.snapshots[f"{consumer[-1]}@base"] = set()
        return True, "", ""

    host.run_pipe = pipe
    assert prov.zfs.copy_image("tank/ggnet/images/cs2@base", "tank/ggnet/images/cs2-b", lambda d, t: seen.append(d))
    assert seen == [100, 150, 200, 300, 300]
