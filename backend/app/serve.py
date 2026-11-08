"""
Run ggNet: `python -m app.serve` (the systemd unit's ExecStart).

Two listeners in one process, on the same app:

  * HTTP  `[web] bind`:`[web] port` (8088): what the PCs use: iPXE's boot
    scripts and Windows PE (/api/v1/boot), the agent (/api/v1/agent) and
    /api/health. A browser that opens it is sent to HTTPS (app.main).
  * HTTPS `[web] bind`:`[web] https_port` (8443) with `[web] tls_cert` and
    `[web] tls_key`: the web UI and its API.

Without a certificate (development, or a config from before HTTPS) only HTTP
runs, as before. The background workers start once, with the HTTP server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path
from typing import Any

import uvicorn

from app.config import get_config

logger = logging.getLogger("ggnet.serve")


def tls(cfg: dict[str, Any] | None = None) -> tuple[int, Path, Path] | None:
    """(HTTPS port, certificate, key) when HTTPS is set up and its files exist, else None."""
    web = (cfg if cfg is not None else get_config()).get("web") or {}
    port, cert, key = web.get("https_port"), web.get("tls_cert"), web.get("tls_key")
    if not (port and cert and key):
        return None
    cert, key = Path(cert), Path(key)
    if not (cert.is_file() and key.is_file()):
        logger.warning("HTTPS is configured but %s or %s is missing; the web UI stays on HTTP", cert, key)
        return None
    return int(port), cert, key


def configs(cfg: dict[str, Any] | None = None) -> list[uvicorn.Config]:
    cfg = cfg if cfg is not None else get_config()
    web = cfg.get("web") or {}
    host, port = str(web.get("bind", "127.0.0.1")), int(web.get("port", 8088))
    app = os.environ.get("GGNET_APP", "app.main:app")
    out = [uvicorn.Config(app, host=host, port=port, log_level="info")]
    secure = tls(cfg)
    if secure is not None:
        https_port, cert, key = secure
        out.append(uvicorn.Config(app, host=host, port=https_port, log_level="info",
                                  ssl_certfile=str(cert), ssl_keyfile=str(key), lifespan="off"))
    return out


class _Server(uvicorn.Server):
    """A server that leaves SIGTERM/SIGINT to _serve(), which stops both."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _serve() -> None:
    servers = [_Server(c) for c in configs()]
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
