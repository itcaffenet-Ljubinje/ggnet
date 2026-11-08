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

Releases: bump `VERSION`, then `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow builds `ggnet.tar.gz`, which the installer downloads.

## License

GPL-3.0 — see [LICENSE](LICENSE).
