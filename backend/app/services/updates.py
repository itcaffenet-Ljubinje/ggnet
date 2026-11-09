"""
Updates of ggNet from the web UI (Settings -> Updates).

  * The newest release is read from GitHub (`[updates] repo`, cached for an
    hour) and compared with the installed VERSION.
  * An update runs `ggnet-update --version <tag>` (scripts/update_ggnet.sh)
    as the transient systemd unit `ggnet-update`: it downloads the release,
    checks its SHA-256 and runs the release's installer with --update, which
    keeps config.toml and restarts the ggnet service. Being its own unit, it
    goes on while this service restarts; its log and exit code are files in
    the data directory, read again by the new ggnet.
  * Only an installed ggNet updates itself: one that runs from a source
    checkout (development) is updated with git, never by this.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("ggnet.updates")

DEFAULT_REPO = "itcaffenet-Ljubinje/ggnet"
INSTALL_DIR = Path("/opt/ggnet")
UPDATER = Path("/usr/local/sbin/ggnet-update")
UNIT = "ggnet-update"
CHECK_EVERY = 3600
LOG_LINES = 40
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[.-]([A-Za-z0-9.]+))?$")


def development_checkout(cfg: dict[str, Any], app_dir: Path) -> bool:
    """
    Whether this ggNet runs from a source checkout (development: updated with
    git) rather than from an install in `[updates] install_dir` (/opt/ggnet,
    made by install_ggnet.sh from a release and updated in Settings -> Updates).
    """
    install_dir = Path((cfg.get("updates") or {}).get("install_dir") or INSTALL_DIR)
    try:
        app_dir.resolve().relative_to(install_dir.resolve())
    except ValueError:
        return True
    return False


class UpdateError(Exception):
    """Shown to the admin as is."""


def parse(version: str) -> tuple[int, int, int, int, str] | None:
    """0.2.0 / v0.2.0 / 0.2.0-rc1 → sortable; a pre-release sorts before its release."""
    m = _TAG.match(version.strip())
    if m is None:
        return None
    pre = m.group(4) or ""
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre


def newer(candidate: str, current: str) -> bool:
    a, b = parse(candidate), parse(current)
    return a is not None and (b is None or a > b)


@dataclass
class Release:
    tag: str
    version: str
    name: str
    notes: str
    url: str
    published_at: str
    checked: bool          # it has ggnet.tar.gz.sha256 (ggnet-update needs it)


class Updates:
    def __init__(self, cfg: dict[str, Any], runner, version: str, app_dir: Path):
        updates = cfg.get("updates") or {}
        self.repo = str(updates.get("repo") or DEFAULT_REPO)
        self.install_dir = Path(updates.get("install_dir") or INSTALL_DIR)
        data = Path((cfg.get("paths") or {}).get("data_dir", "/var/lib/ggnet"))
        self.state_file = data / "update.json"
        self.log_file = data / "logs" / "update.log"
        self.runner = runner
        self.version = version
        self.app_dir = app_dir
        self._latest: tuple[float, Release | None, str | None] | None = None
        self._lock = threading.Lock()

    # ── What is installed ──

    def installed(self) -> str | None:
        """None when this ggNet can update itself, else why not."""
        try:
            self.app_dir.resolve().relative_to(self.install_dir.resolve())
        except ValueError:
            return (f"this ggNet runs from {self.app_dir.parent}, not from {self.install_dir}: "
                    "a development checkout is updated with git")
        if not UPDATER.is_file():
            return f"{UPDATER} is missing; run the installer once more"
        return None

    # ── What GitHub has ──

    def latest(self, force: bool = False) -> tuple[Release | None, str | None]:
        """The newest release and why it could not be read (cached for an hour)."""
        with self._lock:
            if not force and self._latest and time.monotonic() - self._latest[0] < CHECK_EVERY:
                return self._latest[1], self._latest[2]
            try:
                release, error = self._fetch(), None
            except UpdateError as e:
                release, error = None, str(e)
            self._latest = (time.monotonic(), release, error)
            return release, error

    def _fetch(self) -> Release | None:
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "ggnet-updater"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None            # no release yet
            raise UpdateError(f"GitHub answered {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise UpdateError(f"cannot reach GitHub: {getattr(e, 'reason', e)}") from None
        tag = str(data.get("tag_name") or "")
        assets = {a.get("name") for a in data.get("assets") or []}
        return Release(tag=tag, version=tag.lstrip("v"), name=str(data.get("name") or tag),
                       notes=str(data.get("body") or ""), url=str(data.get("html_url") or ""),
                       published_at=str(data.get("published_at") or ""),
                       checked="ggnet.tar.gz.sha256" in assets)

    # ── Running one ──

    def running(self) -> bool:
        ok, out, _ = self.runner.run(["systemctl", "is-active", UNIT], quiet=True)
        return out.strip() in ("active", "activating")

    def start(self, tag: str) -> None:
        if parse(tag) is None:
            raise UpdateError(f"{tag!r} is not a release tag (e.g. v0.2.0)")
        problem = self.installed()
        if problem:
            raise UpdateError(problem)
        if self.running():
            raise UpdateError("an update is already running")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "version": tag, "from": self.version, "started_at": _now(), "exit": None}))
        status = self.state_file.with_suffix(".exit")
        status.unlink(missing_ok=True)
        script = (f"{UPDATER} --version {tag} > {self.log_file} 2>&1; echo $? > {status}")
        ok, _, err = self.runner.run(["systemd-run", f"--unit={UNIT}", "--collect", "--quiet",
                                      "--description=ggNet update", "/bin/bash", "-c", script])
        if not ok:
            raise UpdateError(f"cannot start the update: {err.strip()}")
        logger.warning("Updating ggNet %s to %s (unit %s, log %s)", self.version, tag, UNIT, self.log_file)

    def status(self) -> dict | None:
        """The last update: its version, state (running, done, failed) and the end of its log."""
        try:
            state = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None
        exit_file = self.state_file.with_suffix(".exit")
        code = None
        try:
            code = int(exit_file.read_text().strip())
        except (OSError, ValueError):
            pass
        if self.running():
            result = "running"
        elif code is None:
            result = "failed"          # it ended without an exit code: killed or interrupted
        else:
            result = "done" if code == 0 else "failed"
        try:
            log = self.log_file.read_text(errors="replace").splitlines()[-LOG_LINES:]
        except OSError:
            log = []
        return {"version": state.get("version"), "from": state.get("from"), "started_at": state.get("started_at"),
                "state": result, "exit": code, "log": log}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
