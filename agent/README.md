# ggnet-agent

Windows service for Disk Mode: on startup it connects the iSCSI game disk (D:) and sends heartbeats to the server.

## Server protocol

The agent talks to the ggNet backend over HTTP (no authentication yet; JWT is planned).

`POST /api/v1/agent/heartbeat`, at startup and every 30 seconds:

```json
{
  "name": "pc01",
  "agent_version": "0.1.0",
  "initiator_iqn": "iqn.1991-05.com.microsoft:pc01",
  "iscsi_connected": true
}
```

- `name`: the Windows computer name (1-15 characters `[a-z0-9-]`, sent lowercase).
- `initiator_iqn`: the client's actual iSCSI initiator name, shown in the UI so an
  IQN that does not match the server's ACL is visible.
- `iscsi_connected`: whether the game disk target is currently connected.

Response `200`:

```json
{
  "machine_id": 1,
  "name": "pc01",
  "mode": "disk",
  "status": "provisioned",
  "initiator_iqn": "iqn.1991-05.com.microsoft:pc01",
  "iscsi_target_iqn": "iqn.2025-05.net.ggnet:client-pc01",
  "portal_ip": "192.168.0.10",
  "portal_port": 3260,
  "game_disk": "steam-main"
}
```

- Connect only when `iscsi_target_iqn` is not null (the machine is `provisioned`).
  It is null while the machine has no disk or is in `error`.
- The server's ACL allows only `initiator_iqn`; the agent must use that initiator name.
- If `iscsi_target_iqn` changes or becomes null (disk switched, removed or reset in
  the UI), disconnect the old target before connecting a new one.

Errors: `404` when the machine is not registered in ggNet (register it in the UI
first), `422` for an invalid request body.
