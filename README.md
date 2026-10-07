# ggNet

Diskless boot and game disk management for gaming cafes, installed directly on a **Proxmox VE** host.

- **Disk Mode** — clients keep a local Windows 11 install; the server serves a shared game disk over iSCSI (with per-client writeback zvols).
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
- **Web UI port** — default `8088`

The software itself goes on the Proxmox system disk (`/opt/ggnet`, `/etc/ggnet`, `/var/lib/ggnet`).

Unattended:

```bash
bash install_ggnet.sh --pool tank --iface vmbr1 --web-port 8088 --yes
```

Then open `http://<server-ip>:8088`.

## Service

```bash
systemctl status ggnet
journalctl -u ggnet -n 50
systemctl restart ggnet
```

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8088 | TCP | Web UI + API |
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

Releases: bump `VERSION`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow builds `ggnet.tar.gz`, which the installer downloads.

## License

GPL-3.0 — see [LICENSE](LICENSE).
