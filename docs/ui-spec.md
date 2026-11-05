# ggNet web UI — specification

What the web UI (`frontend/`) should offer, screen by screen, and where each item stands. It is the target for the
UI pull requests; `docs/architecture.md` describes what the backend actually does.

Sources: the workflow of commercial diskless systems for gaming cafes, and the ggNet2 prototype
(`itcaffenet-Ljubinje/ggNet2`, React UI on a simulated backend: its layout, components and feature list are reused,
its backend is not). ggNet has its own code and look.

Status: **done** (backend and UI), **backend** (API exists, UI missing or partial), **todo** (neither).

## Principles

- A PC is identified by its **MAC address**. The iSCSI initiator IQN is derived from it and handled by the server
  and the agent only; it is never shown or asked for in the UI.
- Dark theme, one look for every page (colors and spacing as tokens in `styles.css`).
- Usable from a phone: bottom navigation bar on small screens, tables scroll sideways inside their own wrapper,
  touch targets at least 42 px, modals and drawers full width on phones.
- Every action that touches ZFS or iSCSI says what it does and asks for confirmation; errors from the host are
  shown as they come (`last_error`), not hidden.

## Layout

| Item | Status |
|---|---|
| Header: navigation (Machines, Images, Server, Settings), storage widget (used %, color by threshold) | done |
| Mobile bottom navigation with counts (machines, images) | done |
| Shared styles from ggNet2 (`styles.css` tokens, cards, badges, modals, drawers) | done |

## Machines

| Item | Status |
|---|---|
| KPI cards: total, online, offline, writebacks / dirty | done |
| Table (ggNet2 layout): status dot, name (opens Settings), mode pill, IP, MAC, disks (C: / D: in Boot Mode; letter, game disk, version in Disk Mode), writeback, network I/O, agent, icon actions | done |
| Search (name, IP, MAC, disk) and filters All / Online / Offline / Writeback with counts | done |
| Boot state for Boot Mode PCs: booting (after iPXE), running, off | todo (`ipxe_at`, session state exist) |
| Live traffic per PC (bps and totals) | backend |
| Hardware drawer: CPU, GPU, RAM, board, NIC, link, agent, IP, MAC, iSCSI session | partial |
| Settings per PC (dialog, tabs General / Disks / Network / Hardware): name and mode (also with disks, PC powered off), OS image, game disk, drive letter, version pin, Keep Writeback; MAC fixed | done |
| Add a PC (dialog): name, **MAC (required)**, mode, OS image (Boot Mode), game disk. No IQN field | done |
| Discovery: PCs that knocked (PXE or agent) — Claim (name, mode, disks) and Ignore | done |
| Per PC: Wake-on-LAN, Shutdown, Reboot | done |
| Per PC: Keep Writeback, Discard Writeback, Pin version | done |
| Per PC: Apply Writebacks with a choice: OS only / game only / both (Boot Mode) | done |
| Writeback state: Clean / In use / Starting (Boot Mode, iPXE handed over to Windows) / Pending reset / Kept, with the size the PC has written (measured every minute); orange over the large-writeback warning of Settings → Retention (not for Keep, no automatic action) | done |
| Bulk dock at the bottom: quick select (all / online / offline), power, assign disk, pin, drive letter, stop keeping writebacks | done |
| Bulk delete | todo (backend) |
| Zones (e.g. Main Arena, VIP) for filtering and bulk actions | todo |
| Fixed IP per PC (as a DHCP reservation hint, not in Windows) | todo |
| Add new hardware wizard (Images → ⋮): the first PC that boots the image reads its hardware in Windows PE (signed vendor NIC drivers from `winpe-drivers`), Windows installs the card in a temporary Proxmox VM, the image gets a new version; once per board type. The writeback never gets a card | done |
| Prepare image (once per OS version, also on Apply Writebacks of an OS image): the server's agent and its service, Fast Startup off, fixed IPs to DHCP, stale letters forgotten, Windows tuned for diskless (High performance, no sleep/hibernation, display on, PCIe ASPM/USB suspend/NIC EEE off, fixed 4 GB page file, no crash dumps, no automatic Windows Update/Store updates/maintenance, WSearch and SysMain off, iSCSI initiator loaded after the network stack with WaitForNetworkAtBoot 0: about 25 s less per boot); each PC's writeback then only gets its name, variables and NIC (`[boot] inject_agent`) | done |

## Images

| Item | Status |
|---|---|
| Game disks and OS images in separate sections; Create asks for the kind | done |
| Create: name, kind (game / OS), size | done |
| Import: `.vhdx` / `.vhd` / `.raw` / `.qcow2` from a server folder or a USB disk; new GUIDs, boot-start iSCSI and NDIS NIC driver applied | todo (done by hand for `win11-vip05`) |
| Upload from the browser with progress | todo |
| Versions: list with size, date, PCs on each version; Make active, Rollback, Merge into base, Delete (active / pinned / in use protected) | done |
| Comment on a version (who, when, what changed): asked at Apply Writebacks, editable per version, kept on the ZFS snapshot (`ggnet:comment`) | done |
| Edit on a PC (draft game disk) and Finish Edit | done |
| Writebacks: per PC, size, origin version, session | done |
| Edit an OS image in the maintenance VM ("Local Drives") | todo |

## Settings

| Item | Status |
|---|---|
| Client app: agent download and one-line install | done |
| Retention: reserved space, warning threshold, unused version age, newest versions kept, dry-run preview, run now | done |
| Network boot: PXE service state and restart, TFTP files (`snponly.efi`, `ipxe.efi`, `autoexec.ipxe`), iBFT gateway, unregistered PCs wait / boot locally (saved values override `[boot]` in config.toml) | done |
| Restart services: ZFS, iSCSI target (PXE: done, in Network boot) | todo |
| Secure Boot: shim (signed by Microsoft) + iPXE signed with the ggNet key (made and re-signed by the installer), certificate download with fingerprint and MokManager how-to, per-PC Secure Boot state from the agent (lock in Machines) | done |
| Secure Boot: key enrollment prepared by the agent (MokNew/MokAuth, no USB), switched on in Network boot with a MokManager password; per-PC key state (enrolled / confirm at next boot) | done |
| Secure Boot: BIOS how-to per vendor | todo |
| Sign-in: users with roles (admin: everything; staff: view + power), first admin with a one-time setup code (`ggnet-admin setup-code`), sessions in an HttpOnly cookie or Bearer token, scrypt passwords, wrong-password lockout; Settings → Users and Account | done |
| HTTPS | todo |

## Server (new page)

| Item | Status |
|---|---|
| Pool: size, used, free, fragmentation, health, layout, per-disk state and errors | done |
| Scrub and TRIM: last result or progress, start / stop (admin); warning when older than 35 days; with autotrim on, no TRIM warning (the installer offers autotrim on SSD pools) | done |
| Disks: SMART passed, temperature, wear (endurance used), power-on hours, read only on "Read SMART" | done |
| ARC size, target max and hit ratio; server RAM; all writebacks' total | done |
| Schedule: weekly window (day, from, until) for TRIM and / or scrub in a chosen order, one after the other; paused at the end of the window and resumed the next week | done |
| ARC maximum set from the UI | todo |
| IOPS and throughput per zvol, network per interface | todo |
| iSCSI sessions live (initiator, IP, since when) | todo |
| Activity log (who did what, when, result; failed sign-ins too) in Settings → Activity | done |
| Schedule: power PCs on / off at set times | todo |
| Image backup / restore (`zfs send` to a USB disk or another server) | todo |

## Order of work

1. Shared styles and layout, then Machines (MAC instead of IQN, Boot Mode columns and state, Apply choice, KPI cards).
2. Images (OS images apart, version comment, who is on which version).
3. Settings (boot, services) and the Server page as their backends land.
