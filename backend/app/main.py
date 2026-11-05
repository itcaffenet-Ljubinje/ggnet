"""ggNet FastAPI application.

The API lives under /api; the React build (frontend/dist) is served from the same port.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from app.api.deps import require_user
from app.api.v1 import agent, auth, boot, discovery, game_disks, imports, machines, server, settings
from app.config import get_config

VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


class QuietAccessLog(logging.Filter):
    """
    Keep the journal readable: the web UI polls every 10 s and each agent beats
    every 30 s, which uvicorn would log line by line. Successful reads and
    heartbeats are dropped; changes, errors and the PCs' iPXE requests (useful
    when a PC does not boot) stay. Who changed what is also in the activity log.
    """

    KEEP_PATHS = ("/api/v1/boot/",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client, method, path, _version, status = record.args  # uvicorn.access's format
            status = int(status)
        except (TypeError, ValueError):
            return True
        if status >= 400 or str(path).startswith(self.KEEP_PATHS):
            return True
        if method in ("GET", "HEAD"):
            return False
        return not str(path).startswith("/api/v1/agent/heartbeat")


logging.getLogger("uvicorn.access").addFilter(QuietAccessLog())

# ggNet's own messages (writebacks discarded, disks mapped, images prepared,
# maintenance) go to the journal next to uvicorn's; without a handler Python
# would drop everything below WARNING.
_ggnet_log = logging.getLogger("ggnet")
if not _ggnet_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _ggnet_log.addHandler(_handler)
    _ggnet_log.setLevel(logging.INFO)


def read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "dev"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Run the writeback watcher and the retention job next to the API (both off
    in dev defaults and tests). The retention job itself does nothing until
    the admin turns retention on in Settings.
    """
    from app.api.deps import get_provisioner, get_server_monitor
    from app.db.session import get_sessionmaker
    from app.services.maintenance import MaintenanceJob
    from app.services.retention import JobSettings, RetentionJob
    from app.services.writebacks import SpareMaker, WritebackSettings, WritebackSizer, WritebackWatcher

    from app.services.copies import recover as recover_copies

    cfg = get_config()
    # A copy that ran when ggNet stopped cannot go on: mark it failed.
    try:
        recover_copies(get_sessionmaker(), get_provisioner())
    except OperationalError as e:      # no schema yet (a fresh install before its first start)
        logging.getLogger("ggnet.copies").warning("Checking for interrupted copies skipped: %s", e.orig)
    except Exception:                  # never keeps the API from starting
        logging.getLogger("ggnet.copies").exception("Checking for interrupted copies failed")
    settings = WritebackSettings.from_config(cfg)
    workers = [WritebackSizer(get_sessionmaker(), get_provisioner()),
               MaintenanceJob(get_sessionmaker(), get_server_monitor())]
    if settings.auto_discard:
        workers.append(WritebackWatcher(get_sessionmaker(), get_provisioner(), settings))
        if settings.spares:
            workers.append(SpareMaker(get_sessionmaker(), get_provisioner()))
    job = JobSettings.from_config(cfg)
    if job.job:
        workers.append(RetentionJob(get_sessionmaker(), get_provisioner(), job.interval))
    for w in workers:
        w.start()
    try:
        yield
    finally:
        for w in workers:
            w.stop()


app = FastAPI(title="ggNet", version=read_version(), lifespan=lifespan)
# The web UI's API needs a signed-in user (app.services.auth); what the PCs
# use (agent, boot) and signing in itself do not.
signed_in = [Depends(require_user)]
app.include_router(game_disks.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(imports.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(machines.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(settings.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(discovery.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(server.router, prefix="/api/v1", dependencies=signed_in)
app.include_router(auth.users, prefix="/api/v1")
app.include_router(auth.activity, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(boot.router, prefix="/api/v1")


@app.get("/api/health")
def health() -> dict[str, str]:
    cfg = get_config()
    return {
        "status": "ok",
        "version": app.version,
        "pool": cfg["storage"]["pool"],
        "server_ip": cfg["network"]["server_ip"],
    }


# Mount the React build last so it does not shadow the /api routes.
_dist = Path(get_config()["web"].get("frontend_dist", ""))
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
