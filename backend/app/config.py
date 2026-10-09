"""Load configuration from /etc/ggnet/config.toml (path overridable via GGNET_CONFIG)."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/ggnet/config.toml"

# Values for local development when config.toml does not exist.
# Dev mode always uses the separate "-dev" tree, never the production ggnet tree.
DEV_DEFAULTS: dict[str, Any] = {
    "storage": {
        "pool": "tank",
        "root_dataset": "tank/ggnet-dev",
        "images": "tank/ggnet-dev/images",
        "writebacks": "tank/ggnet-dev/writebacks",
        "snapshots": "tank/ggnet-dev/snapshots",
        "iscsi_targets": "tank/ggnet-dev/iscsi_targets",
    },
    "network": {"interface": "lo", "server_ip": "127.0.0.1", "cidr": "127.0.0.1/8"},
    "services": {"iscsi_portal": "127.0.0.1:3260", "iscsi_target_name": "storage-dev", "dhcp_mode": "proxy"},
    "web": {"bind": "127.0.0.1", "port": 8088, "frontend_dist": "../frontend/dist"},
    "paths": {"data_dir": "./data", "database": "sqlite:///./data/ggnet.db"},
    # Dev mode never discards anything on its own.
    "writebacks": {"auto_discard": False, "grace_seconds": 30, "poll_seconds": 5},
    # ... and never runs the retention job (it deletes versions and writebacks).
    "retention": {"job": False},
}


@lru_cache
def get_config() -> dict[str, Any]:
    path = Path(os.environ.get("GGNET_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.is_file():
        return DEV_DEFAULTS
    with path.open("rb") as f:
        return tomllib.load(f)
