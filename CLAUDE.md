# ggNet — context for Claude Code

ggNet is a diskless boot and game disk management system for gaming cafes, modeled after ggRock.
It is installed **directly on a Proxmox VE 9+ host** (ProxMenux-style: install script + systemd service on its own port)
and manages ZFS and the LIO iSCSI target on that host.

## Rules

- **Everything in the repo is written in English**: code, comments, docstrings, log/error messages, commit messages, docs.
- The development machine IS the production hypervisor of a live gaming cafe (17 PCs). Before any command that
  touches ZFS, iSCSI (targetcli), DHCP/dnsmasq, networking or systemd, state what it does and ask first.
- Never run `zfs destroy`, `zpool` changes, `targetcli clearconfig`, or edit `/etc/network/interfaces` without explicit approval.
- Develop and test against a dedicated dataset (e.g. `<pool>/ggnet-dev`), never against production images.
- Never commit secrets (`.env`, keys, tokens, passwords).

## Two modes

- **Disk Mode** (implement first): clients have local Windows 11 on C:. The server exposes a shared game image as D:
  over iSCSI, with a per-client writeback zvol as overlay. `ggnet-agent` (Windows service) connects iSCSI on boot
  using the built-in Windows iSCSI Initiator and sends heartbeats.
- **Boot Mode** (later): fully diskless UEFI PXE boot. proxyDHCP with option 93 (arch detection), signed iPXE.efi
  (shim / MOK), iSCSI LUNs on ZFS zvols, TPM 2.0 handling, Sysprep master images, UWF/EWF write filters.

Target clients: Windows 11, UEFI Secure Boot, TPM 2.0.

## Priority order

1. `zfs_manager.py` (datasets, zvols, snapshots, clones, scrub)
2. `iscsi_manager.py` (LIO via targetcli / rtslib)
3. Game disks API
4. Disk Mode UI + `ggnet-agent`
5. Boot Mode (iPXE + proxyDHCP UEFI)
6. JWT auth + SSL

## Data model (Disk Mode)

- `Machine`: add `mode` enum (disk/boot), `game_disk_id` FK, `iscsi_target_iqn`
- New `GameDisk` model

## Layout

| Path | Contents |
|------|----------|
| `backend/` | FastAPI + SQLAlchemy, Python 3.13+ (`app.main:app`) |
| `frontend/` | React + TypeScript (Vite), dark mode UI, built to `frontend/dist/` and served by the backend |
| `agent/` | `ggnet-agent` Windows service |
| `scripts/` | `install_ggnet.sh`, `uninstall_ggnet.sh` |

## Runtime

- Config: `/etc/ggnet/config.toml` (override with `GGNET_CONFIG`). Read pool, datasets, service IP from it —
  never hard-code them. `backend/app/config.py` falls back to dev defaults when the file is missing.
- ZFS layout: `<pool>/ggnet/{images,writebacks,snapshots,iscsi_targets}`
- iSCSI: ONE shared target `<iscsi_iqn_prefix>:<iscsi_target_name>`; each client is an ACL (its initiator IQN)
  with its own mapped LUNs. Never delete the shared target to fix one client. Design: `docs/architecture.md`.
- Code: `/opt/ggnet`, data: `/var/lib/ggnet`, service: `ggnet.service`
- Ports: 8088 web UI/API, 3260 iSCSI, 67/4011 UDP proxyDHCP, 69 UDP TFTP, 80 HTTP boot.
  Do not use 8006/8007 (Proxmox/PBS) or 8008 (ProxMenux).
- ZFS scrub: weekly, off-hours (02:00–06:00).

## Hardware

HPE DL380 Gen9, 192 GB RAM, 2× 8-core CPU, 4× 1.65 TB + 4× 3.8 TB enterprise SSD (ZFS RAID10),
256 GB NVMe for Proxmox OS, 2× SFP+ 10G.

## Checks

```bash
cd backend && pytest
cd frontend && npm ci && npm run build && npm test
cd agent && dotnet test GgnetAgent.slnx
shellcheck -S warning scripts/*.sh
```
