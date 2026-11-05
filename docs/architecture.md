# ggNet architecture

ggNet is a storage and boot orchestrator for gaming cafes, in the style of commercial diskless systems. It runs directly on the
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
│   └── pc02                clone of images/cs2@v3      → Boot Mode, mapped LUN 1
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

The installer creates the `writebacks` dataset with `sync=disabled` and the ggNet root with `checksum=blake3`: a
strong checksum lets ZFS skip a block rewritten with the same data (nop-write), so a writeback's registry hive,
rewritten whole for the PC's name, costs only the changed blocks. Images written before keep their checksum
until they are copied. (LIO's `emulate_write_cache` cannot be set on a block backstore: the zvol reports its
own write cache, and setting it fails with EINVAL.)

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

### Initiator IQN: the PC's identity on the target

A PC with a MAC gets an initiator IQN derived from it, `<iscsi_iqn_prefix>:pc-<mac without separators>`
(`iqn.2025-05.net.ggnet:pc-aabbccddee02`). It does not depend on the Windows computer name, so a renamed or
cloned Windows keeps its own disk, and in Boot Mode, where every PC boots the same Windows, iPXE sets it from
the server. A machine without a MAC
keeps the Windows default `iqn.1991-05.com.microsoft:<name>`. `PATCH /machines/{id}` with `"initiator_iqn": ""`
resets a machine to its default (only while it has no disk, like every ACL change); the agent renames the
Windows initiator to the IQN in its heartbeat answer before it connects.

The agent's heartbeat finds its machine by the MAC in its inventory (the machine's MAC set in the UI), else by
the Windows computer name. A name match is refused when that machine has a MAC and the PC reports another one:
it is a different PC with the same Windows name, which must not get that machine's disk.

### Discovery

A PC that knocks but is not registered is listed under Discovered PCs on the Machines page (a PC is added on
its first network boot): an unknown MAC that asks for an iPXE script, or a ggnet-agent whose heartbeat matches no
machine (with its Windows name, IP and agent version). The admin adds it with the name it should have (its MAC
comes along, so its IQN is derived from it), ignores it (hidden also when it knocks again) or removes it from the
list. Both sources are open to the LAN, so the list is capped at 500 PCs; when full, new MACs are only logged.

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
also how other diskless systems behave, and it gives the same read performance as a shared LUN because the blocks
in ARC are the same.

---

## 3. Writebacks and image versions

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
time covers a fast restart that reconnects within the grace period. That boot time is when Windows booted
(`booted_at` in the heartbeat), so restarting or updating the agent service is not a reboot; the agent also keeps
the disk connected when its service stops. `install.ps1` turns Windows Fast Startup off, because with it "Shut down"
hibernates the kernel and the next start keeps the old boot time. A newer `booted_at` with no live iSCSI session discards the writeback before the agent logs in again. A discard also moves the PC to the image's
current active snapshot, or to its pinned snapshot (below). All of this runs under the provisioner lock and only
ever touches that one PC's ACL, backstore and clone.

### Filling a new master (Edit on PC)

A new game disk starts as an empty, writable **draft** zvol. To fill it, **Edit on PC** maps the draft zvol itself
(no clone) as the game disk of one Disk Mode PC that has no game disk; the machine's status becomes `editing`.
ggnet-agent connects it as usual; on first use the admin initializes and formats it in Disk Management (the agent
assigns the drive letter once a data partition exists) and installs the games. Then the PC is shut down and
**Finish editing** unmaps the draft (refused while the PC still has an iSCSI session), and **Publish** takes `@base`,
holds it and sets `readonly=on`.

While a draft is being edited, it cannot be published or deleted, and the editing PC cannot get a game disk or be
deleted. The writeback watcher only records the editing PC's session state; it never discards anything there,
because there is no writeback: every write goes straight into the draft.

### Image versions

Masters are versioned with ZFS snapshots: `@base`, `@v2`, `@v3`, ... Each image has one **active** snapshot.
PCs are cloned from it at their next discard, so a new version reaches every PC at its next reboot.

```
images/cs2@base  @v2  @v3(active)
                         ├── writebacks/pc01 ... pc16      discarded on every disconnect
                         └── writebacks/pc17               Keep Writeback: admin installs/updates games here
```

**Apply Writebacks** (overflow menu of a powered-off PC with Keep Writeback). The kept writeback itself becomes
the master, so no data is copied:

```bash
# pc17 is unmapped from iSCSI first (its zvol is renamed)
zfs set readonly=on tank/ggnet/writebacks/pc17
zfs snapshot tank/ggnet/writebacks/pc17@v4
zfs hold ggnet:protected tank/ggnet/writebacks/pc17@v4
zfs promote tank/ggnet/writebacks/pc17          # @base..@v3 (with their holds) move to pc17
zfs rename tank/ggnet/images/cs2 tank/ggnet/images/cs2_ggnet_old
zfs rename tank/ggnet/writebacks/pc17 tank/ggnet/images/cs2
zfs inherit sync tank/ggnet/images/cs2
zfs destroy tank/ggnet/images/cs2_ggnet_old      # the old head: an empty clone of @v3
# active = v4; pc17 gets a new writeback cloned from @v4 (same content), Keep Writeback stays on
```

Every snapshot keeps its name and hold, so the other PCs' clones of `@v3` (or older) are not touched and move to
`@v4` at their next reboot. Until the two renames succeed, a failure rolls everything back (promote the old master
again, drop `@v4`) and pc17 is mapped again. `zfs send -i cs2@v3 pc17@x | zfs recv cs2` does not work: an
incremental from a clone's origin is a clone stream, which `zfs recv` only accepts as a new dataset. Apply is
refused when `@v3` is no longer the newest snapshot of the master (someone applied in between);
the admin then discards the PC's writeback and redoes the change.

PCs that are running when a new version is applied keep their current snapshot until they reboot. The Machines
page shows this as the image status, next to the game disk:

| Badge | Icon | Meaning |
|---|---|---|
| `@v2` (green) | green circle | runs the active version and follows it |
| `→ @v2 at reboot` | red arrow → green circle | runs an older version, moves to the active one at its next reboot |
| `→ @base at reboot` | green arrow → red circle | was just pinned to another version, moves to it at its next reboot |
| `Pinned @base` (red) | red circle | pinned: stays on that version whatever is active |
| `Keep writeback` (red) | red circle | keeps its writeback, so it never moves on its own |

**Per-PC snapshot pin** (the Version drop-down under the game disk; `machines.pinned_snapshot`): a PC runs a chosen
version of its image instead of the active one, for testing a version on one PC or rolling one PC back. Nothing
changes on the host when the pin is set: the PC is cloned from the pinned version at its next discard. A pinned
version cannot be deleted; switching the PC's game disk or applying its writeback clears the pin.
**Rollback for everyone** = make an older snapshot active.

**Merge into base** (`POST /game-disks/{id}/snapshots/{name}/merge-into-base`): after a few Windows or game
updates (`@base @v2 @v3 @v4`), the chosen version becomes the new `@base` and every older one is deleted. Nothing
is merged or copied: a ZFS snapshot is the whole disk, so `@v4` already holds every change.

```bash
zfs release ggnet:protected tank/ggnet/images/cs2@base && zfs destroy tank/ggnet/images/cs2@base   # same for @v2, @v3
zfs rename tank/ggnet/images/cs2@v4 tank/ggnet/images/cs2@base   # hold kept; clones of @v4 follow the new name
```

Refused while an older version is active, pinned, or the origin of any PC's writeback (those PCs move off it at
their next reboot). Versions newer than the chosen one are kept. The database follows the rename (active version,
pins, each PC's `clone_snapshot`), so PCs on `@v4` stay up to date. The next change is `@v2` again. If a step fails
after some deletions, the chosen version keeps its old name and running the merge again finishes it.

### Retention (Settings → Retention)

Automated snapshot and writeback removal. The settings live in the database (`settings` table, set in
the web UI); `app/services/retention.py` runs every hour.

| Setting | Default | Effect |
|---|---|---|
| Reserved disk space | 15 % | `refreservation` on `<root>/reserved` (empty, unmounted), so nothing can fill the SSDs completely |
| Warning threshold | 80 % | banner in the UI when the pool is fuller than this |
| Unused versions | 14 days | a version not active, not pinned and not the origin of any PC's writeback, older than this, is deleted… |
| Always keep newest versions | 3 | …except the newest N versions of each disk, which are always kept |
| Inactive writebacks | 24 hours | a Keep Writeback PC that has been off this long (and wrote something) gets its writeback discarded |

Safety: nothing touches the host until the admin saves the settings. The pool is shared with Proxmox VMs and
containers, so the reservation applies to them too, and saving it asks first. The job is off until "Run
automatically" is ticked, and in dry-run mode it only records what it would delete. **Preview** shows what the
settings would delete right now; **Run now** deletes what the saved settings allow. Every item is re-checked on
the host right before it is deleted (the version is still unused, the PC is still off) and the last run is shown
on the Settings page. The age of a version is its creation time (ggNet does not record when a version stopped
being active). The job thread is off in dev defaults (`[retention] job = false`).

---|---|---|
| Reserved disk space | 15 % | `refreservation` on `<pool>/ggnet/reserved`, so writebacks can never fill the SSDs completely |
| Warning threshold | 80 % | banner in the UI when the pool is fuller than this |
| Unutilized snapshots | 14 days | a snapshot not active, not pinned and not the origin of any clone for this long is deleted… |
| Unprotected snapshots | 3 | …except the newest N snapshots of each image, which are always kept |
| Inactive writebacks | 24 hours | a Keep Writeback PC that has been off this long gets its writeback discarded |

---

## 4. Boot Mode: proxyDHCP and iPXE

*Done: OS images, a Boot Mode PC's OS and game writebacks, and the per-MAC iPXE script. Planned: the
dnsmasq proxyDHCP, the iPXE build, importing a `.vhdx` into an OS image, and Secure Boot.*

An OS image is a disk of kind `os` (`POST /game-disks` with `"kind": "os"`; created with the OS properties
above). A Boot Mode machine is assigned an OS image and a game disk together (`os_disk_id` + `game_disk_id`) and
gets two writebacks: `<writebacks>/<name>-os` (mapped LUN 0) and `<writebacks>/<name>` (mapped LUN 1; the same
path as a Disk Mode game writeback). Machine names may not end in `-os`, so the two never collide. Both
writebacks are discarded together, by the same rules as in Disk Mode, and the OS writeback always follows the OS
image's active version (no pin yet). Apply Writebacks of a Boot Mode PC with Keep Writeback turns its OS
writeback into the OS image's next version and its game writeback into the game disk's next version
(`{"os": false}` or `{"game": false}` applies only one; the other stays kept as it is). The fresh OS writeback
is set up as the PC again.

The router stays the DHCP server for IP addresses. dnsmasq on the Proxmox host only answers PXE requests
(proxyDHCP) and serves the iPXE binary over TFTP. Only one PXE server may run in the LAN.

The installer sets this up as the `ggnet-pxe` service (`dnsmasq-base`, config `/etc/ggnet/dnsmasq-pxe.conf`,
TFTP root `/var/lib/ggnet/tftp`). It answers every PC: UEFI x64 PCs get `snponly.efi` (iPXE on the board's
own UEFI network driver; iPXE's built-in Realtek driver stalled after ~30 MB on the RTL8168). iPXE first runs
`autoexec.ipxe` from the same TFTP root, which the installer writes: get an address from the router (`dhcp`)
and chain `/api/v1/boot/ipxe?mac=${net0/mac}` directly, trying again every 3 s until ggNet answers. Relying on
the second ProxyDHCP reply (port 4011) instead failed often: with several PCs booting at once iPXE missed it
and stopped with "No more network devices" (2026-10-18). What the PC gets depends on ggNet, not on dnsmasq:

- a registered Boot Mode PC boots its OS image;
- a Disk Mode PC, or a PC without a valid MAC, leaves iPXE and boots its local disk;
- an unregistered PC is listed under Machines → New PCs and waits on screen: every 10 s it
  asks for its script again, so it boots as soon as it is registered; a key press boots the local disk.
  `[boot] unregistered = "local"` makes it boot the local disk at once instead.

A PC whose BIOS tries the network before its disk therefore stops at the waiting screen until it is
registered (or a key is pressed).

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
set initiator-iqn iqn.2025-05.net.ggnet:pc-aabbccddee02
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
- The iBFT gateway is 0.0.0.0 by default (`set netX/gateway 0.0.0.0` before `sanboot`). With the router's gateway in the iBFT, Windows lost its iSCSI disk as it took over from iPXE (0x7B on vip-05, A/B tested on the same image on 2026-10-16); with 0.0.0.0 it boots and still gets its default route and internet afterwards. `[boot] gateway` in config.toml: `"none"` (the default), `"dhcp"` (keep the router's gateway) or an IP (e.g. the server with IP forwarding).

Unknown MAC (not registered, or registration window open):

```
#!ipxe
echo This PC is not registered in ggNet (${net0/mac}). Booting from local disk.
sleep 5
exit
```

The MAC is looked up in the machine's own MAC, then in the MAC its agent reported. An unknown MAC is listed
in Discovery (section 2) for the admin to add.

Boot Mode PC without its disks (not provisioned, or in `error`): a message with the reason, then `exit`.

### The PC's identity in its OS writeback

Every Boot Mode PC boots the same Windows image, so on its own every PC would have the image's computer name.
The server edits each fresh OS
writeback offline before it is mapped, so Windows comes up as that PC without an extra reboot
(`backend/app/windows_setup.py`):

1. Find the NTFS partitions of the clone (`lsblk`, largest first) and mount them in turn with `ntfs-3g` under
   `/run/ggnet/windows/<name>` until one has `Windows\System32\config\SYSTEM`.
2. Read the control set Windows boots with (`SYSTEM\Select\Current`, not a fixed `ControlSet001`).
3. Merge with `hivexregedit --merge` (ntfs-3g and hivexregedit ship with Proxmox VE):
   - `Control\ComputerName\ComputerName`: `ComputerName` = the machine name in capitals (PC-07)
   - `Services\Tcpip\Parameters`: `Hostname`, `NV Hostname` = the machine name (pc-07)
   - `Control\Session Manager\Environment`: `GGNET_SERVER`, `GGNET_MACHINE_ID`, `GGNET_MACHINE_NAME`, so the
     agent and other software on the PC need no configuration in the image
   - `Services\Tcpip\Parameters\Interfaces\{...}` with a fixed IPv4 address (`EnableDHCP` 0): `EnableDHCP` = 1,
     `IPAddress`, `SubnetMask`, `DefaultGateway` removed. An image captured from a PC with a fixed address
     (vip-05: 192.168.0.25) gave every PC that address, and the PC owning it lost its iSCSI disk (0x7B) as soon
     as another one booted. The boot NIC gets its address from the iBFT anyway; a PC that must keep one address
     gets a DHCP reservation on the router.
   - `MountedDevices`: every `\DosDevices\X:` but C: is removed. The image remembered the letters of the PC it
     was made on (vip-05's local D:, E:, F:); elsewhere those volumes do not exist, yet Windows kept the letters,
     so the agent could not give the game disk D: (`Set-Partition` 42012). Windows letters present volumes again
     at boot, and the agent then gives the game disk its letter on every PC.
4. Unmount.

It runs whenever an OS writeback is cloned fresh (assign, reset, every discard) and never touches the image.
A failure (an image without Windows yet, a hibernated Windows that ntfs-3g refuses) is logged and the PC boots
with the image's name: better than a PC that does not boot. One Windows account per image: auto-logon is set
in the image itself, not per PC.

Disk Mode PC that PXE-boots by mistake: the same `exit` script, so it falls through to its local disk.

---

## 5. Web UI

React + Vite, served by the backend on port 8088. Layout: Machines, Images, Server and Settings tabs.

**Machines**
- Columns: Name, Status (online/off, Keep Writeback, agent missing, no image set, link below 1 Gbit/s),
  IP, Game Image (+ OS image in Boot Mode) with the snapshot status icon, Uptime, Sent, Received, Speed,
  Link Speed; MAC optional. Sent/Received come from LIO's per-ACL statistics in configfs
  (`acls/<initiator>/lun_0/statistics/scsi_auth_intr/{read,write}_mbytes`: they restart at every new clone),
  Speed from the change between two reads; IP, link speed and hardware from the agent's inventory. *(done:
  IP, uptime, link speed, hardware, Sent/Received/Speed)*
- Overflow menu: Turn On (Wake-on-LAN), Shutdown, Reboot (through the agent), Apply Writebacks (only when the PC
  is off and has Keep Writeback), Settings, Delete. No Reset: discarding is automatic. *(Turn On / Shutdown /
  Reboot done: WoL is a magic packet broadcast on `[network] cidr` from the service IP, using the machine's MAC
  or the one its agent reported; Shutdown / Reboot wait on the machine and the agent gets them once in its next
  heartbeat answer, dropped after 2 minutes, then runs `shutdown.exe` with a 10 s notice.)*
- Discovered PCs: unregistered PCs that asked for a disk, with Add (name, mode) and Ignore. *(done)*
- Settings dialog: Main (name, read-only IP and MAC, mode, game image, OS image, hide), Hardware (NIC, GPU, CPU,
  motherboard, reported by the agent), Advanced (Keep Writeback, snapshot pin per image).
- Bulk: select PCs → Turn On, Turn Off, Reboot, Edit Selected (images, Keep Writeback, pins, hide). *(done:
  Turn on, Shutdown, Reboot, game disk, version pin (when the PCs share a disk), drive letter, stop keeping
  writeback; `POST /machines/bulk` runs each machine like its single route and reports per machine)*

**Images**
- Create a game image (name, size, drive letter, make default), import `.vhd`/`.vhdx` (Boot Mode system image).
- Per image (Details): versions with date, data size, space only that version holds and the PCs running on it,
  with Make active and Delete (not the active one, not one a PC runs on); writebacks with the PC, the version it was
  cloned from, the space it has written, Keep Writeback and whether it is connected, with Discard for a PC that is
  off (also one that keeps its writeback). Read live from ZFS. *(done)*
- Backup / restore: `zfs send` of the image with its snapshots to a file on a local disk or over SSH to another
  ggNet server, and back.

**Server**: pool size/used/free/health and last scrub, ARC size and hit ratio, IOPS and throughput per zvol,
network throughput of the iSCSI interface.

**Settings**: service IP and portal, iSCSI target name, default drive letter, proxyDHCP on/off, retention.

---

## Roadmap

| Step | State |
|---|---|
| ZFS manager, game disks API, machines API, agent heartbeat, Disk Mode UI | done |
| One shared iSCSI target with per-PC ACLs and mapped LUNs | done |
| Automatic writeback discard, Keep Writeback, Apply Writebacks (zfs promote), image versions | done |
| Edit on PC for drafts, game disk drive letter per machine, Images page: versions and writebacks | done |
| Snapshot pin per machine and the image status | done |
| Retention job, reserved space and warning threshold | done |
| Machines UI (IP, uptime, link speed, hardware, Sent/Received), WoL / shutdown / reboot, bulk | done |
| Settings → Client app: agent download and one-line install | done |
| Boot Mode: OS images, OS + game writebacks per PC, per-MAC iPXE script | done |
| PC identity by MAC: MAC-derived initiator IQN, heartbeat by MAC, Discovery | done |
| Boot Mode: per-PC OS writeback setup before boot (computer name, environment variables) | done |
| Boot Mode: Apply Writebacks for the OS image (OS only / game only / both), watcher boot window after iPXE | done |
| Boot Mode: fixed IPv4 → DHCP and old drive letters forgotten in each OS writeback; agent hides local disks | done |
| Boot Mode: agent installed into the OS image by the server | planned |
| Maintenance VM: the installer offers one Proxmox VM (UEFI, TPM, no disk, stopped; its ID in config.toml); Images → OS image → Edit in VM attaches the image, Finish makes a new version | planned |
| Boot Mode: first diskless boot on real PCs (dev dnsmasq + `snponly.efi`, 4 PCs, 2 board types) | done |
| Boot Mode: proxyDHCP / TFTP set up by the installer (`ggnet-pxe`, answers every PC); unregistered PCs wait for registration | done |
| Boot Mode: import `.vhdx` into an OS image (new GUIDs, boot-start iSCSI, NDIS NIC driver), Add new hardware wizard, Secure Boot (shim + MOK) | planned |
| ZFS property defaults from section 1, Server monitor page | next |
| Boot Mode: OS images, proxyDHCP, iPXE build, per-MAC script, Add Machines wizard | after Disk Mode |
| Sign-in (users, roles, sessions, activity log) | done |
| TLS | last |
