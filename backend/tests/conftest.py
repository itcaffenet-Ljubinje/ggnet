"""
Shared test fixtures.

FakeRunner replaces CommandRunner: it records every command and returns
canned responses, so manager logic is tested without a host.
"""

from __future__ import annotations

import os
from typing import Callable, Optional, Union

import pytest

from app.config import get_config
from app.zfs_manager import ZFSLayout, ZFSManager

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
    return ZFSManager(layout=make_layout(), runner=runner)


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
