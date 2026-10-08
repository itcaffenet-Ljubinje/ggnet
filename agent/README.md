# ggnet-agent

Windows service for Disk Mode: on startup it connects the iSCSI game disk (D:) and sends heartbeats to the server.
C#/.NET 10, published as one self-contained `ggnet-agent.exe` (no .NET install on clients).

## What it does

Every 30 seconds (and at startup) the agent:

1. reads the PC's iSCSI initiator name and checks that its game disk session is still up;
2. sends a heartbeat to the server and gets back the wanted target;
3. makes the PC match it, using the built-in Windows iSCSI initiator (PowerShell cmdlets):
   - **connect**: add the portal, log in (non-persistent), bring the disk online and
     writable (the SAN policy keeps it offline), and give its data partition the drive letter;
   - **disconnect** a target the server no longer assigns (disk removed or switched in the UI);
   - **rename the initiator** to the IQN the server's ACL expects, before connecting
     (domain PCs use `iqn.1991-05.com.microsoft:<fqdn>`). Turn off with `ManageInitiatorName`.

If the server is unreachable the disk stays connected, so players keep playing.
When the service stops (including Windows shutdown) it disconnects the disk.
Errors are logged and retried on the next heartbeat.

Logs: Event Viewer → Windows Logs → Application, source `ggnet-agent`.

## Install on a client PC

1. Register the PC in the ggNet web UI (its Windows computer name, lowercase) and assign a game disk.
2. Copy `ggnet-agent.exe` and `scripts/install.ps1` into one folder on the PC.
3. In an elevated PowerShell in that folder:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\install.ps1 -ServerUrl http://<ggnet-server-ip>:8088
   ```

   Use `-DriveLetter G` if `D:` is already taken (DVD drive, second partition).

The same command updates an existing install. `scripts/uninstall.ps1` removes it.

Settings (`C:\Program Files\ggnet-agent\appsettings.json`, section `Agent`):
`ServerUrl`, `DriveLetter` (D-Z), `HeartbeatSeconds` (5-3600), `ManageInitiatorName`.

## Build and test

```bash
cd agent
dotnet test GgnetAgent.slnx
dotnet publish src/GgnetAgent -c Release -r win-x64 -o publish   # publish/ggnet-agent.exe
```

The tests run on Linux too: the iSCSI and PowerShell parts sit behind interfaces,
and the scripts are tested for the values they embed.

## Server protocol

The agent talks to the ggNet backend over HTTP (no authentication yet; JWT is planned).

`POST /api/v1/agent/heartbeat`, at startup and every 30 seconds:

```json
{
  "name": "pc01",
  "agent_version": "0.1.0",
  "initiator_iqn": "iqn.1991-05.com.microsoft:pc01",
  "iscsi_connected": true,
  "booted_at": "2026-10-08T07:58:12.4810000+00:00"
}
```

- `name`: the Windows computer name (1-15 characters `[a-z0-9-]`, sent lowercase).
- `initiator_iqn`: the client's actual iSCSI initiator name, shown in the UI so an
  IQN that does not match the server's ACL is visible.
- `iscsi_connected`: whether the game disk target is currently connected.
- `booted_at`: when the agent service started, i.e. this Windows boot. When it changes the server
  discards the PC's old writeback before answering, so every boot gets a clean game disk
  (unless the machine has Keep Writeback).

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
