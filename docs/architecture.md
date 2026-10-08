# ggNet architecture

ggNet is a storage and boot orchestrator for gaming cafes, in the style of ggRock. It runs directly on the
Proxmox VE host (systemd service, no VM, no container) and drives ZFS and the LIO iSCSI target there.
There is no billing, no timers and no user accounts for players: ggNet only manages disks and boot.

Two client modes, built in this order:

1. **Disk Mode**: Windows 11 boots from the PC's own SSD. `ggnet-agent` logs in to iSCSI and mounts the game
   disk (e.g. `D:`). Games are installed and updated once, on the server.
2. **Boot Mode**: the PC has no disk. UEFI PXE → iPXE → iSCSI `sanboot`. LUN 0 is the PC's Windows writeback,
   LUN 1 its game disk writeback.

Status of each part is tracked in [Roadmap](#roadmap).

---

## 1. ZFS

### Layout

Names come from `[storage]` in `/etc/ggnet/config.toml`; nothing is hard-coded. Production uses
`<pool>/ggnet`, development uses `<pool>/ggnet-dev`.

```
<pool>/ggnet/
├── images/                 masters, read-only after publish
│   ├── cs2                 game disk zvol      @base @v2 @v3 ...  (holds: ggnet:protected)
│   └── win11-os            OS zvol (Boot Mode) @v1 @v2 ...
├── writebacks/             per-PC clones, disposable (sync=disabled)
│   ├── pc01                clone of images/cs2@v3      → Disk Mode, mapped LUN 0
│   ├── pc02-os             clone of images/win11-os@v2 → Boot Mode, mapped LUN 0
│   └── pc02-game           clone of images/cs2@v3      → Boot Mode, mapped LUN 1
├── snapshots/
└── iscsi_targets/
```

A clone shares every unchanged block with its master snapshot, so 17 PCs reading the same game files hit the
same ARC blocks. A clone only costs the space of what that PC wrote since its last reset.

### Properties

| Dataset | volblocksize | compression | sync | other |
|---|---|---|---|---|
| game master (`images/<game>`) | `64k` | `lz4` | `standard` | `readonly=on` after publish |
| OS master (`images/<os>`) | `16k` | `lz4` | `standard` | `readonly=on` after publish |
| PC writeback (`writebacks/*`) | inherited from origin | inherited | **`disabled`** | discarded on every disconnect unless Keep Writeback |

Why these values:

- `64k` for games: game files are large and read sequentially. Format the game disk in Windows as **NTFS with a
  64K allocation unit** so one NTFS cluster is one ZFS block (no read-modify-write).
- `16k` for the OS (the OpenZFS 2.2+ default): Windows does small random I/O. `4k`/`8k` zvols on a mirror waste
  metadata and compress badly; on RAIDZ they waste a lot of space. On this host's ZFS RAID10 `16k` is the right
  trade-off.
- `lz4`: costs almost nothing and gives up early on data that is already compressed, so it is safe for game
  files too. `zstd` saves a little more space on OS images for more CPU; not worth it for hot game data.
- `sync=disabled` on writebacks: they are thrown away on every disconnect anyway. A PC with **Keep Writeback**
  is switched to `sync=standard` while the flag is on, because the admin's game installs on it must survive a
  host crash. Masters keep `sync=standard`.
- `volblocksize` is fixed at creation and a clone inherits it from its origin. Changing it later means a new
  master and copying the data.

`create_zvol()` currently creates game masters with `compression=off` and `sync=disabled`; this changes to the
table above with the image-versioning work.

### ARC

The host has 192 GB RAM. Proxmox installs set `zfs_arc_max` to 10% of RAM (max 16 GiB), far too small for a
game server. With no VMs that need the memory, give ARC 128 GiB:

```
# /etc/modprobe.d/zfs.conf          (template: deploy/zfs/zfs.conf)
options zfs zfs_arc_max=137438953472
options zfs zfs_arc_min=34359738368
```

Apply with `update-initramfs -u -k all` and a reboot, or live with
`echo 137438953472 > /sys/module/zfs/parameters/zfs_arc_max`. Leave `primarycache=all` (the default) on
masters. An L2ARC is not needed: the pool is all SSD.

---

## 2. iSCSI: one shared target (implemented)

All PCs log in to **one** target, `<iscsi_iqn_prefix>:<iscsi_target_name>` (default
`iqn.2025-05.net.ggnet:storage`). Each PC gets its own ACL, keyed by its **initiator IQN**, with its own mapped
LUNs. A PC can only see the LUNs in its own ACL.

```
/iscsi/iqn.2025-05.net.ggnet:storage/tpg1
├── attributes   generate_node_acls=0 authentication=0     (ACL-only, no demo mode)
├── portals      192.168.0.50:3260                          (service IP only, never 0.0.0.0)
├── luns         lun0 → block/pc01-game   lun1 → block/pc02-os   lun2 → block/pc02-game
└── acls
    ├── iqn.1991-05.com.microsoft:pc01   mapped_lun0 → lun0
    └── iqn.1991-05.com.microsoft:pc02   mapped_lun0 → lun1, mapped_lun1 → lun2
```

This is what `backend/app/iscsi_manager.py` does. For one PC, `attach()` runs:

```bash
T=/iscsi/iqn.2025-05.net.ggnet:storage/tpg1
targetcli /backstores/block create name=pc01-game dev=/dev/zvol/tank/ggnet/writebacks/pc01
targetcli $T/luns create /backstores/block/pc01-game lun=0 add_mapped_luns=false
targetcli $T/acls create iqn.1991-05.com.microsoft:pc01 add_mapped_luns=false
targetcli $T/acls/iqn.1991-05.com.microsoft:pc01 create mapped_lun=0 tpg_lun_or_backstore=lun0 write_protect=false
targetcli saveconfig
```

`add_mapped_luns=false` is the critical part. targetcli's default (`auto_add_mapped_luns=true`) maps every new
LUN into **every** existing ACL, so every PC would see every other PC's disk.

`detach()` deletes the PC's ACL and its backstores (LIO removes the TPG LUN with its backstore). The shared
target itself is never deleted, so resetting one PC never drops another PC's session. Every call runs under
the provisioner lock, so there are no parallel targetcli runs and no half-built targets left behind: a failed
attach removes what it created and reports the real error.

### Migrating from per-PC targets

The previous version made one target per PC (`<prefix>:client-<name>`, backstore `client-<name>`). `detach()`
also removes those, so a PC moves to the shared target the next time it is reset or reassigned:

1. Make sure the PC is powered off (or the agent disconnected).
2. Click **Reset** on the machine. Its old target is removed, a fresh clone is attached to the shared target.
3. The agent picks up the new target IQN from its next heartbeat and reconnects. No agent change is needed.

Check with `targetcli /iscsi ls`: after all PCs are reset only the `:storage` target remains.

### Why the game disk is not one shared read-only LUN

Exposing the master itself read-only to all PCs looks simpler but breaks in practice: Steam, Riot and Epic write
into the game folder on launch (logs, shader caches, update checks), and NTFS on a write-protected disk fails
those writes. Each PC therefore gets a ZFS clone of the master as its game disk. The clone is created instantly,
shares all blocks with the master, takes writes, and is discarded on reset. The master never changes. This is
also how ggRock and CCBoot behave, and it gives the same read performance as a shared LUN because the blocks
in ARC are the same.

---

## 3. Writebacks and image versions (ggRock model, planned)

There is no Super Client mode and no manual Reset button. A PC's writeback is discarded **by the server** every
time the PC disconnects, shuts down or restarts. The only exception is a PC with **Keep Writeback** ticked: its
writeback survives reboots, and the admin turns its changes into a new image version with **Apply Writebacks**.

### Automatic reset

The server decides from the PC's iSCSI session, which LIO exposes in configfs:

```
/sys/kernel/config/target/iscsi/<target>/tpgt_1/acls/<initiator>/info
    "No active iSCSI Session for Initiator Endpoint: ..."   → disconnected
    "InitiatorName: ... Session State: TARG_SESS_STATE_LOGGED_IN"   → connected
```

A watcher in the backend reads this for every PC every few seconds and keeps the last state in the database
(`session_connected`, `session_since`). Rules:

| Event | Keep Writeback off | Keep Writeback on |
|---|---|---|
| Session ends (shutdown, restart, cable) and stays down for the grace period (default 30 s) | discard writeback: detach → `zfs destroy` → `zfs clone` from the image's active snapshot → attach | nothing |
| Agent starts on Windows boot (`POST /api/v1/agent/boot`, Disk Mode) and has no session | same discard, before the agent logs in | nothing |
| iPXE asks for its script (Boot Mode) | same discard, before `sanboot` | nothing |
| Writeback of a powered-off Keep Writeback PC is older than "Inactive writebacks" hours | n/a | discard (retention, below) |

The grace period stops a short network blip from wiping a disk that Windows still has mounted; the agent's boot
call covers a fast restart that reconnects within the grace period. A discard also moves the PC to the image's
current active snapshot, or to its pinned snapshot (below). All of this runs under the provisioner lock and only
ever touches that one PC's ACL, backstore and clone.

### Image versions

Masters are versioned with ZFS snapshots: `@base`, `@v2`, `@v3`, ... Each image has one **active** snapshot.
PCs are cloned from it at their next discard, so a new version reaches every PC at its next reboot.

```
images/cs2@base  @v2  @v3(active)
                         ├── writebacks/pc01 ... pc16      discarded on every disconnect
                         └── writebacks/pc17               Keep Writeback: admin installs/updates games here
```

**Apply Writebacks** (overflow menu of a powered-off PC with Keep Writeback):

```bash
zfs snapshot tank/ggnet/writebacks/pc17@apply
zfs send -i tank/ggnet/images/cs2@v3 tank/ggnet/writebacks/pc17@apply | zfs recv tank/ggnet/images/cs2
zfs rename tank/ggnet/images/cs2@apply tank/ggnet/images/cs2@v4
zfs hold ggnet:protected tank/ggnet/images/cs2@v4
# active = v4; pc17's writeback is re-cloned from @v4 (same content), Keep Writeback stays on
```

The incremental send writes only the changed blocks into the master, so `@v4` is a normal snapshot of the master
with no dependency on pc17, and `@v3` stays intact. `zfs recv` works on a `readonly=on` zvol, so the master is
never writable. Apply is refused when `@v3` is no longer the newest snapshot of the master (someone applied
in between, as ggRock does); the admin then discards the PC's writeback and redoes the change.

PCs that are running when a new version is applied keep their current snapshot until they reboot. The Machines
page shows this with the ggRock status icons: on the active snapshot; on an older one and will move on reboot;
pinned or Keep Writeback, will not move.

**Per-PC snapshot pin** (Settings → Advanced): a PC can be pinned to a non-active snapshot of its image, for
testing a version or rolling one PC back. **Rollback for everyone** = make an older snapshot active.

### Retention (Settings → Array)

As in ggRock's "Automated Snapshot and Writeback Removal", an hourly job in the backend:

| Setting | Default | Effect |
|---|---|---|
| Reserved disk space | 15 % | `refreservation` on `<pool>/ggnet/reserved`, so writebacks can never fill the SSDs completely |
| Warning threshold | 80 % | banner in the UI when the pool is fuller than this |
| Unutilized snapshots | 14 days | a snapshot not active, not pinned and not the origin of any clone for this long is deleted… |
| Unprotected snapshots | 3 | …except the newest N snapshots of each image, which are always kept |
| Inactive writebacks | 24 hours | a Keep Writeback PC that has been off this long gets its writeback discarded |

---

## 4. Boot Mode: proxyDHCP and iPXE (planned)

The router stays the DHCP server for IP addresses. dnsmasq on the Proxmox host only answers PXE requests
(proxyDHCP) and serves the iPXE binary over TFTP. Only one PXE server may run in the LAN.

`deploy/dnsmasq/ggnet-proxydhcp.conf`:

```ini
port=0                                  # no DNS
interface=vmbr1
bind-interfaces
dhcp-range=192.168.0.0,proxy,255.255.255.0
log-dhcp

enable-tftp
tftp-root=/var/lib/ggnet/tftp

# UEFI x64 clients (option 93 = 7 or 9) get iPXE with the embedded script below.
pxe-service=X86-64_EFI,"ggNet",ggnet-ipxe.efi
pxe-service=BC_EFI,"ggNet",ggnet-ipxe.efi
```

The router's DHCP range must hold two addresses per PC (one for iPXE, one for Windows). On the switch: PortFast
/ STP edge on client ports, DHCP snooping off.

`ggnet-ipxe.efi` is iPXE (`snponly.efi` target) built with `deploy/ipxe/embed.ipxe` embedded. The installer
replaces `@SERVICE_IP@` with the service IP before building, because `${next-server}` is not reliable when the
router and the proxyDHCP both answer:

```
#!ipxe
dhcp || goto retry
chain http://@SERVICE_IP@:8088/api/v1/boot/ipxe?mac=${net0/mac:hexhyp} || goto retry
:retry
sleep 5
reboot
```

Because the script is embedded, iPXE never loops back into PXE and dnsmasq needs no iPXE user-class tagging.

Secure Boot: stock iPXE is not signed by Microsoft. Until a shim-signed build is in place, Boot Mode PCs need
Secure Boot off. Disk Mode PCs are not affected.

### Per-MAC iPXE script

`GET /api/v1/boot/ipxe?mac=aa-bb-cc-dd-ee-ff` returns one of three scripts. iSCSI root path format:
`iscsi:<server>::<port>:<lun>:<target-iqn>`.

Every boot uses the same script. Which clone is behind LUN 0 (a fresh writeback, a kept one, or a pinned
snapshot) is decided by the server before it answers:

```
#!ipxe
set initiator-iqn iqn.1991-05.com.microsoft:pc02
set root-path iscsi:192.168.0.50::3260:0:iqn.2025-05.net.ggnet:storage
echo ggNet: booting pc02 (win11-os v2, cs2 v3)
sanboot ${root-path} || goto failed
:failed
echo Boot failed, retrying in 10 s
sleep 10
reboot
```

- No `--no-describe`: Windows needs the iBFT table iPXE writes to continue booting from iSCSI.
- `initiator-iqn` must equal the PC's ACL IQN, otherwise LIO refuses the login.
- Only LUN 0 is named in the root path. Windows sees LUN 1 (the game disk) through the same iSCSI session.

Unknown MAC (not registered, or registration window open):

```
#!ipxe
echo This PC is not registered in ggNet (${net0/mac}). Booting from local disk.
sleep 5
exit
```

The request is recorded so the PC shows up under "Unregistered" (and in the Add Machines wizard).

Disk Mode PC that PXE-boots by mistake: the same `exit` script, so it falls through to its local disk.

---

## 5. Web UI

React + Vite, served by the backend on port 8088. Layout follows ggRock's Machines and Images tabs.

**Machines**
- Columns: Name, Status (online/off, Keep Writeback, agent missing, no image set, link below 1 Gbit/s),
  IP, Game Image (+ OS image in Boot Mode) with the snapshot status icon, Uptime, Sent, Received, Speed,
  Link Speed; MAC optional. Sent/Received come from LIO's per-LUN statistics in configfs, link speed from the
  agent.
- Overflow menu: Turn On (Wake-on-LAN), Shutdown, Reboot (through the agent), Apply Writebacks (only when the PC
  is off and has Keep Writeback), Settings, Delete. No Reset: discarding is automatic.
- Settings dialog: Main (name, read-only IP and MAC, mode, game image, OS image, hide), Hardware (NIC, GPU, CPU,
  motherboard, reported by the agent), Advanced (Keep Writeback, snapshot pin per image).
- Bulk: select PCs → Turn On, Turn Off, Reboot, Edit Selected (images, Keep Writeback, pins, hide).

**Images**
- Create a game image (name, size, drive letter, make default), import `.vhd`/`.vhdx` (Boot Mode system image).
- Per image: active snapshot, snapshot list (date, size, PCs using it), make active, delete.
- Backup / restore: `zfs send` of the image with its snapshots to a file on a local disk or over SSH to another
  ggNet server, and back. Same idea as ggRock's `ggrock-img send/receive`.

**Server**: pool size/used/free/health and last scrub, ARC size and hit ratio, IOPS and throughput per zvol,
network throughput of the iSCSI interface.

**Settings**: service IP and portal, iSCSI target name, default drive letter, proxyDHCP on/off, retention.

---

## Roadmap

| Step | State |
|---|---|
| ZFS manager, game disks API, machines API, agent heartbeat, Disk Mode UI | done |
| One shared iSCSI target with per-PC ACLs and mapped LUNs | done |
| Automatic writeback discard, Keep Writeback, Apply Writebacks, image versions, snapshot pin | next |
| Retention job, Machines/Images UI per the ggRock docs, WoL / shutdown / reboot | next |
| ZFS property defaults from section 1, Server monitor page | next |
| Boot Mode: OS images, proxyDHCP, iPXE build, per-MAC script, Add Machines wizard | after Disk Mode |
| JWT auth + TLS | last |
