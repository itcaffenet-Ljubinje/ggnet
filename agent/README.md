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
4. in Boot Mode, once per start: takes the PC's own disks offline (NVMe, SATA, SAS, RAID, ...; not iSCSI, USB,
   or the disk Windows boots from), so games are not installed on a local disk that only this PC sees.
   The offline state lives in the booted Windows (the writeback), not on the disk, so the PC's local
   Windows still sees its disk. Turn off with `HideLocalDisks`.
5. in Boot Mode, once per start: turns off Windows' startup scans and clean-up tasks (compatibility appraiser,
   telemetry, defragmentation, disk diagnostics, WinSAT…), in the booted Windows only. Their work is thrown away
   with the writeback at every restart, so they only slow the start. Turn off with `DisableIdleTasks`.
6. in Boot Mode, once per start, while the server asks for it (Settings → Network boot): checks that the
   PC trusts the ggNet Secure Boot key (shim's `MokListRT`), else asks shim to enroll it as
   `mokutil --import` does (`MokNew` + `MokAuth` UEFI variables). At the next boot MokManager asks to
   confirm Enroll MOK with the password from the UI. The state (enrolled / requested / failed) goes to
   the server with each heartbeat.

If the server is unreachable the disk stays connected, so players keep playing.
When the service stops the disk stays connected, so restarting or updating the agent does not pull it
from running games (Windows shutdown ends the session; `uninstall.ps1` disconnects it).
`install.ps1` turns Fast Startup off: with it, "Shut down" is a hibernation and the PC would come back
with the old boot time.
Errors are logged and retried on the next heartbeat.

Logs: Event Viewer → Windows Logs → Application, source `ggnet-agent`.

## Install on a client PC

1. Register the PC in the ggNet web UI (its Windows computer name, lowercase) and assign a game disk.
2. Easiest: in an Administrator PowerShell on the PC, run the line shown in the web UI under
   Settings → Client app:

   ```powershell
   irm http://<ggnet-server-ip>:8088/api/v1/agent/bootstrap.ps1 | iex
   ```

   It downloads the agent from the server, checks the SHA-256 of each file and runs `install.ps1`
   with the server's URL. The server offers what is in `[paths] agent_dir` (the unpacked
   `ggnet-agent-win-x64.zip` of a release). Or by hand: copy `ggnet-agent.exe` and `scripts/install.ps1`
   (Settings → Client app → Download .zip) into one folder on the PC and continue with step 3.
3. In an elevated PowerShell in that folder:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\install.ps1 -ServerUrl http://<ggnet-server-ip>:8088
   ```

   The game disk letter is set per machine in the web UI (Machines → Drive, or "Apply to all"); the agent
   moves a connected disk when it changes. `-DriveLetter` is only used with a server that sends no letter.
   If the wanted letter is taken on a PC (DVD drive, second partition, USB stick), the agent keeps the letter
   Windows already gave the game disk, or takes the first free one (wanted..Z, then D upward), logs a
   warning and reports it; the UI then shows "Got E:". Keep the same letter on every PC where you can:
   games and launchers (e.g. Steam library folders) remember the path they were installed to.
   Boot Mode machines always get the game disk as D: (Windows itself is C:).

The same command updates an existing install. `scripts/uninstall.ps1` removes it.

Settings (`C:\Program Files\ggnet-agent\appsettings.json`, section `Agent`):
`ServerUrl`, `DriveLetter` (D-Z), `HeartbeatSeconds` (5-3600), `ManageInitiatorName`, `HideLocalDisks`.

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
  "booted_at": "2026-10-08T07:58:12.4810000+00:00",
  "drive_letter": "D",
  "inventory": {
    "ip_address": "192.168.0.21",
    "mac_address": "AA-BB-CC-DD-EE-01",
    "link_speed_mbps": 1000,
    "nic": "Intel(R) Ethernet Connection I219-V",
    "cpu": "AMD Ryzen 5 5600X 6-Core Processor",
    "gpus": ["NVIDIA GeForce RTX 3060"],
    "motherboard": "ASUSTeK COMPUTER INC. TUF GAMING B550-PLUS",
    "memory_bytes": 17179869184
  }
}
```

- `name`: the Windows computer name (1-15 characters `[a-z0-9-]`, sent lowercase).
- `initiator_iqn`: the client's actual iSCSI initiator name, shown in the UI so an
  IQN that does not match the server's ACL is visible.
- `iscsi_connected`: whether the game disk target is currently connected.
- `drive_letter`: the letter the game disk actually got (only while connected).
- `stopping`: `true` only in a goodbye heartbeat, so the server shows the PC as offline at once instead
  of after 90 s without heartbeats. The agent sends it right before it runs a Shutdown / Reboot command
  (then stays quiet for 2 minutes) and when its service is stopped (e.g. an update), giving up after 3 s.
  During a Windows shutdown the service is not given time to stop, so a shutdown from the Start menu
  is caught by the server instead: its iSCSI session drops after the last heartbeat.
- `inventory`: the network adapter the PC reaches the server on (IP, MAC, link speed) and its hardware,
  read with CIM at start and then every 10 minutes. The server ignores an inventory it cannot parse;
  the heartbeat itself never fails because of it.
- `booted_at`: when Windows booted (restarting the agent service keeps it). When it changes the server
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
  "game_disk": "steam-main",
  "drive_letter": "D",
  "command": null
}
```

- Connect only when `iscsi_target_iqn` is not null (the machine is `provisioned`, or
  `editing` a draft game disk). It is null while the machine has no disk or is in `error`.
- `drive_letter` (D-Z) is the letter the game disk should get; when it changes while
  connected, move the disk to it (no reconnect).
- `command` is `"shutdown"` or `"reboot"` when the admin clicked Shutdown / Reboot in the UI
  (sent once; the server drops it after 2 minutes). The agent runs `shutdown.exe` with a
  10-second notice and ignores any other value.
- The server's ACL allows only `initiator_iqn`; the agent must use that initiator name.
- If `iscsi_target_iqn` changes or becomes null (disk switched, removed or reset in
  the UI), disconnect the old target before connecting a new one.

The server finds the machine by `inventory.mac_address` (the MAC set for it in the UI),
else by `name`, so the name in the UI may differ from the Windows computer name.

Errors: `404` when the machine is not registered in ggNet (the PC then shows up under
Discovered PCs in the UI, where it is added), `422` for an invalid request body.
