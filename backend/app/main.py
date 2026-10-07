"""ggNet FastAPI application.

The API lives under /api; the React build (frontend/dist) is served from the same port.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1 import agent, game_disks, machines
from app.config import get_config

VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "dev"


app = FastAPI(title="ggNet", version=read_version())
app.include_router(game_disks.router, prefix="/api/v1")
app.include_router(machines.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")


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
