# ggNet

Diskless boot and game image management for gaming cafes, installed directly on a **Proxmox VE** host.

- **Disk Mode** — clients keep a local Windows 11 install; the server serves a shared game image over iSCSI (with per-client writeback zvols).
- **Boot Mode** — fully diskless UEFI PXE boot (iPXE + proxyDHCP) from ZFS zvols.

ggNet runs as a systemd service next to Proxmox and manages ZFS and the LIO iSCSI target on the host itself — no VM or container needed.

## Install

Run on the Proxmox host as root:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/itcaffenet-Ljubinje/ggnet/main/scripts/install_ggnet.sh)"
```

The installer asks for:

- **ZFS pool** — where images, writebacks, snapshots and iSCSI targets are created (`<pool>/ggnet/...`)
- **Interface / IP** (`vmbr*` or manual) — where iSCSI, proxyDHCP, TFTP and HTTP boot listen
- **Ports** — `8443` for the web UI (HTTPS) and `8088` for the PCs (HTTP: iPXE, the agent)

The software itself goes on the Proxmox system disk (`/opt/ggnet`, `/etc/ggnet`, `/var/lib/ggnet`).

Unattended:

```bash
bash install_ggnet.sh --pool tank --iface vmbr1 --web-port 8088 --https-port 8443 --yes
```

Then open `https://<server-ip>:8443`. The installer makes a self-signed certificate (`/etc/ggnet/tls`), so
the browser warns once; Settings → Network boot offers the certificate to trust on the admin PCs, or put your
own certificate and key in `[web] tls_cert` / `tls_key`. `http://<server-ip>:8088` sends a browser to HTTPS.
ggNet is meant for the cafe's LAN: do not expose it to the internet.

## Updates

Settings → Updates shows the newest release and installs it: `ggnet-update` downloads `ggnet.tar.gz`, checks it
against `ggnet.tar.gz.sha256` and runs the release's installer with `--update`, which keeps
`/etc/ggnet/config.toml` as it is. The PCs keep running; ggNet restarts once. From the shell:

```bash
ggnet-update                    # the newest release
ggnet-update --version v0.2.0   # a given one
```

Before its migrations, an update dumps the database (`pg_dump`) into `/var/lib/ggnet/db-backups` (the
newest 5 are kept, named after the version they were taken from) and puts it back if a migration fails.
Settings → Updates lists every release with its notes and the updates done on this server with their logs,
and can go back to a version this server ran before: it puts back the database dumped before the update
away from that version (whatever was changed since is lost) and installs that release
(`ggnet-update --version vX.Y.Z --restore-db <dump>`).

The installer and every update also put the release's agent (`ggnet-agent-win-x64.zip`, checked against its
`.sha256`) into `/var/lib/ggnet/agent`, which Settings → Client app offers to the PCs.

A release is made by pushing a tag that matches `VERSION` (`git tag v0.2.0 && git push origin v0.2.0`); the
Release workflow builds the packages and their checksums.

## Database

ggNet keeps its records (machines, images, users, settings, activity) in PostgreSQL on the same server: the
installer installs the `postgresql` package (Debian 13: PostgreSQL 17) and creates the database `ggnet`.
ggNet signs in over the Unix socket as the role `root` (peer authentication): there is no password, and no
TCP port is opened for it. An install from before PostgreSQL is moved on its next update: its SQLite database
is copied (`ggnet-admin copy-database`) and stays in `/var/lib/ggnet` as it was.

```bash
psql ggnet                                          # look around (as root)
pg_restore --clean -d ggnet /var/lib/ggnet/db-backups/ggnet-<date>.dump   # an update's backup, by hand
```

## Service

```bash
systemctl status ggnet
journalctl -u ggnet -n 50
systemctl restart ggnet
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8443 | TCP | Web UI + API (HTTPS) |
| 8088 | TCP | PCs: iPXE boot scripts, Windows PE, agent (HTTP); browsers are sent to 8443 |
| 3260 | TCP | iSCSI |
| 67, 4011 | UDP | proxyDHCP |
| 69 | UDP | TFTP |
| 80 | TCP | HTTP boot |

## Uninstall

```bash
ggnet-uninstall
```

ZFS datasets are never deleted automatically.

## Development

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload --port 8088
```

Development and the tests use SQLite (`./data/ggnet.db`). To run the tests on PostgreSQL as well, point
`GGNET_TEST_POSTGRES` at an empty database the tests may wipe:
`GGNET_TEST_POSTGRES=postgresql+psycopg:///ggnet_test?host=/run/postgresql pytest` (CI does both).

Releases: bump `VERSION`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow builds `ggnet.tar.gz`, which the installer downloads.

## License

GPL-3.0 — see [LICENSE](LICENSE).
