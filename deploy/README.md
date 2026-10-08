# deploy/

Configuration templates for the Proxmox host. **None of these are installed automatically yet**; see
[docs/architecture.md](../docs/architecture.md) for what each one does and when to apply it.

| File | Goes to | Used by |
|---|---|---|
| `zfs/zfs.conf` | `/etc/modprobe.d/zfs.conf` | ARC size (both modes) |
| `dnsmasq/ggnet-proxydhcp.conf` | `/etc/dnsmasq.d/ggnet-proxydhcp.conf` | Boot Mode |
| `ipxe/embed.ipxe` | embedded into `ggnet-ipxe.efi` at build time | Boot Mode |
