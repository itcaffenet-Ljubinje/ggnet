# Contributing to ggNet

## Language

All code, comments, docstrings, log and error messages, commit messages, issues and documentation in this repository are written in **English**, so anyone can read and contribute.

## Layout

| Path | Contents |
|------|----------|
| `backend/` | FastAPI + SQLAlchemy service (Python 3.13+, Proxmox VE 9+) |
| `frontend/` | React + TypeScript web UI, built to `frontend/dist/` |
| `agent/` | `ggnet-agent`, the Windows client service for Disk Mode |
| `scripts/` | Proxmox install / uninstall scripts |

## Before opening a PR

```bash
cd backend && pytest
cd frontend && npm ci && npm run build && npm test
cd agent && dotnet test GgnetAgent.slnx
shellcheck -S warning scripts/*.sh
```

CI runs the same checks on every push and pull request.

## Configuration and secrets

Runtime configuration lives in `/etc/ggnet/config.toml` on the host (path overridable with `GGNET_CONFIG`). Never commit passwords, tokens, keys or `.env` files.

## Releases

1. Bump `VERSION` (e.g. `0.2.0`).
2. `git tag v0.2.0 && git push origin v0.2.0`
3. The Release workflow builds `ggnet.tar.gz`, which `install_ggnet.sh` downloads.
