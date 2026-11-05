"""
Shared test fixtures.

FakeRunner replaces CommandRunner: it records every command and returns
canned responses, so manager logic is tested without a host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Union

import pytest

from app.config import get_config
from app.iscsi_manager import ISCSIConfig, ISCSIManager
from app.services.provisioning import Provisioner
from app.zfs_manager import ZFSLayout, ZFSManager

from .fakehost import FakeHost

Response = tuple[bool, str, str]


class FakeRunner:
    """
    Fake runner with the same signature as CommandRunner.run.

    `responses` maps a command prefix (tuple) to a response, or to a function
    that takes the command and returns a response. The longest matching
    prefix wins; with no match, `default` is returned.
    """

    def __init__(self, default: Response = (True, "", "")):
        self.default = default
        self.responses: dict[tuple[str, ...], Union[Response, Callable[[list[str]], Response]]] = {}
        self.calls: list[list[str]] = []
        self.last_error = ""

    def on(self, *prefix: str, result: Union[Response, Callable[[list[str]], Response]]) -> None:
        self.responses[tuple(prefix)] = result

    def run(self, cmd: list[str], timeout: Optional[int] = None, quiet: bool = False) -> Response:
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        best: Optional[tuple[str, ...]] = None
        for prefix in self.responses:
            if tuple(cmd[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            response = self.default
        else:
            result = self.responses[best]
            response = result(cmd) if callable(result) else result
        if not response[0]:
            self.last_error = response[2]
        return response


def make_layout(pool: str = "tank", root: str = "tank/ggnet", **overrides: str) -> ZFSLayout:
    names = {
        "pool": pool,
        "root_dataset": root,
        "images": f"{root}/images",
        "writebacks": f"{root}/writebacks",
        "snapshots": f"{root}/snapshots",
        "iscsi_targets": f"{root}/iscsi_targets",
    }
    names.update(overrides)
    return ZFSLayout(**names)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def zfs(runner: FakeRunner) -> ZFSManager:
    z = ZFSManager(layout=make_layout(), runner=runner)
    z.sleep = lambda seconds: None          # destroy()'s busy retries do not wait in tests
    return z


@pytest.fixture(autouse=True)
def clean_env(request, monkeypatch):
    """Tests must not depend on GGNET_* variables from the shell or a cached config."""
    if request.node.get_closest_marker("destructive") is None:
        for name in list(os.environ):
            if name.startswith("GGNET_") and name != "GGNET_ALLOW_DESTRUCTIVE_TESTS":
                monkeypatch.delenv(name)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


def pytest_collection_modifyitems(config, items):
    """Tests marked `destructive` are skipped without explicit permission."""
    if os.environ.get("GGNET_ALLOW_DESTRUCTIVE_TESTS") == "yes":
        return
    skip = pytest.mark.skip(reason="touches the real host; set GGNET_ALLOW_DESTRUCTIVE_TESTS=yes")
    for item in items:
        if "destructive" in item.keywords:
            item.add_marker(skip)


# ── Service + API (FakeHost, one SQLite database per test) ────────────

BACKEND_DIR = Path(__file__).resolve().parent.parent


def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(autouse=True)
def _fresh_hardware_status():
    """Which cards an image version starts is cached per process; every test starts empty."""
    from app.services import scan

    scan._STATUS.clear()
    yield
    scan._STATUS.clear()


@pytest.fixture
def host() -> FakeHost:
    h = FakeHost()
    # The tree setup_dataset_tree() leaves on the host.
    layout = make_layout()
    for ds in (layout.root_dataset, layout.images, layout.writebacks,
               layout.snapshots, layout.iscsi_targets):
        h.add_dataset(ds)
    return h


@pytest.fixture
def prov(host: FakeHost) -> Provisioner:
    p = Provisioner(
        ZFSManager(layout=make_layout(), runner=host),
        ISCSIManager(config=ISCSIConfig(portal="192.168.10.1:3260"), runner=host),
    )
    p.zfs.sleep = lambda seconds: None      # destroy()'s busy retries do not wait in tests
    return p


@pytest.fixture
def db_url(tmp_path) -> str:
    from alembic import command

    url = f"sqlite:///{tmp_path / 'test.db'}"
    command.upgrade(alembic_config(url), "head")   # schema ALWAYS from migrations
    return url


def _client(db_url: str, prov: Provisioner, signed_in: bool):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.api.deps import get_provisioner, require_user
    from app.db.models import User, UserRole
    from app.db.session import get_db, make_engine
    from app.main import app

    engine = make_engine(db_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_provisioner] = lambda: prov
    if signed_in:
        # Most tests are about the API, not about signing in (tests/test_auth.py).
        app.dependency_overrides[require_user] = lambda: User(
            id=0, username="test-admin", role=UserRole.ADMIN, disabled=False)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def client(db_url: str, prov: Provisioner):
    """A test client signed in as an admin."""
    yield from _client(db_url, prov, signed_in=True)


@pytest.fixture
def anon_client(db_url: str, prov: Provisioner):
    """A test client that has to sign in like a browser."""
    yield from _client(db_url, prov, signed_in=False)
