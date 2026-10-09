"""ggNet FastAPI application.

The API lives under /api; the React build (frontend/dist) is served from the same port.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1 import agent, game_disks, machines, settings
from app.config import get_config

VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


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
    from app.api.deps import get_provisioner
    from app.db.session import get_sessionmaker
    from app.services.retention import JobSettings, RetentionJob
    from app.services.writebacks import WritebackSettings, WritebackWatcher

    cfg = get_config()
    settings = WritebackSettings.from_config(cfg)
    workers = []
    if settings.auto_discard:
        workers.append(WritebackWatcher(get_sessionmaker(), get_provisioner(), settings))
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
app.include_router(game_disks.router, prefix="/api/v1")
app.include_router(machines.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")


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
