#!/usr/bin/env bash
# ggNet installer for Proxmox VE
#
#  - Software goes on the Proxmox system disk:  /opt/ggnet       (code + venv)
#                                               /var/lib/ggnet   (db, tftp, http root)
#                                               /etc/ggnet/config.toml
#  - ZFS operations (images, writebacks, snapshots, iscsi_targets, imports) go to the pool you pick
#  - iSCSI / DHCP / TFTP / HTTP bind to the interface/IP you pick (vmbr* or manual)
#
# Install from GitHub (downloads the latest release):
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/itcaffenet-Ljubinje/ggnet/main/scripts/install_ggnet.sh)"
#
# From a local checkout:  bash scripts/install_ggnet.sh
# Unattended:             bash install_ggnet.sh --pool tank --iface vmbr1 --yes

set -euo pipefail

GGNET_REPO="itcaffenet-Ljubinje/ggnet"

usage() {
    cat <<'EOF'
ggNet installer for Proxmox VE

Options:
  --source DIR      directory with ggNet code (default: the repo this script is in;
                    if there is none, the release is downloaded from GitHub)
  --version TAG     release to download, e.g. v0.1.0 (default: latest)
  --pool NAME       ZFS pool for data
  --dataset NAME    root dataset inside the pool (default: ggnet)
  --iface NAME      interface for services (e.g. vmbr1)
  --ip ADDR         IP for services (must exist on the host)
  --web-port N      web UI port (default: 8088)
  --web-bind MODE   "service" (service IP only) or "all" (0.0.0.0) (default: service)
  --app MODULE      uvicorn entry point (default: app.main:app)
  --yes             no prompts (for CI / automation)
EOF
}

# ---------- defaults ----------
SRC_DIR=""
VERSION="latest"
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
    if [[ -d "$SCRIPT_DIR/../backend" ]]; then
        SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    elif [[ -d "$SCRIPT_DIR/backend" ]]; then
        SRC_DIR="$SCRIPT_DIR"
    fi
fi
POOL=""
DATASET="ggnet"
IFACE=""
SERVICE_IP=""
SERVICE_CIDR=""
WEB_PORT="8088"
WEB_BIND_MODE="service"
APP_MODULE="app.main:app"
ASSUME_YES=0

INSTALL_DIR="/opt/ggnet"
DATA_DIR="/var/lib/ggnet"
CONF_DIR="/etc/ggnet"
CONF_FILE="$CONF_DIR/config.toml"
UNIT_FILE="/etc/systemd/system/ggnet.service"
PXE_UNIT_FILE="/etc/systemd/system/ggnet-pxe.service"
PXE_CONF_FILE="$CONF_DIR/dnsmasq-pxe.conf"
SB_DIR="$CONF_DIR/secureboot"
SB_SIGN="/usr/local/sbin/ggnet-sign-ipxe"
SB_APT_HOOK="/etc/apt/apt.conf.d/90ggnet-secureboot"
LAN_UNIT_FILE="/etc/systemd/system/ggnet-lan-route.service"
LAN_SYSCTL_FILE="/etc/sysctl.d/90-ggnet-lan.conf"
RESERVED_PORTS=(22 85 111 3128 3260 8006 8007 8008)   # Proxmox, PBS, ProxMenux, iSCSI

TITLE="ggNet installer"

# ---------- helpers ----------
red()   { printf '\e[31m%s\e[0m\n' "$*"; }
green() { printf '\e[32m%s\e[0m\n' "$*"; }
info()  { printf '\e[36m==>\e[0m %s\n' "$*"; }
die()   { red "ERROR: $*"; exit 1; }

interactive() { [[ $ASSUME_YES -eq 0 && -t 0 ]]; }

wt() { whiptail --title "$TITLE" "$@" 3>&1 1>&2 2>&3; }

confirm() {
    local msg="$1"
    interactive || return 0
    wt --yesno "$msg" 14 74
}

# ---------- args ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)   SRC_DIR="$2"; shift 2 ;;
        --version)  VERSION="$2"; shift 2 ;;
        --pool)     POOL="$2"; shift 2 ;;
        --dataset)  DATASET="$2"; shift 2 ;;
        --iface)    IFACE="$2"; shift 2 ;;
        --ip)       SERVICE_IP="$2"; shift 2 ;;
        --web-port) WEB_PORT="$2"; shift 2 ;;
        --web-bind) WEB_BIND_MODE="$2"; shift 2 ;;
        --app)      APP_MODULE="$2"; shift 2 ;;
        --yes|-y)   ASSUME_YES=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ---------- 1. prerequisites ----------
[[ $EUID -eq 0 ]] || die "Run as root."
command -v pveversion >/dev/null || die "This is not a Proxmox VE host (pveversion not found)."
PVE_MAJOR="$(pveversion | head -n1 | sed -n 's#^pve-manager/\([0-9]*\)\..*#\1#p')"
[[ "$PVE_MAJOR" =~ ^[0-9]+$ ]] || die "Cannot read the Proxmox VE version from pveversion."
(( PVE_MAJOR >= 9 )) || die "ggNet needs Proxmox VE 9 or newer (found $PVE_MAJOR)."
command -v zpool >/dev/null || die "ZFS tools are not installed."
if interactive && ! command -v whiptail >/dev/null; then
    apt-get install -y whiptail >/dev/null
fi

if [[ -z "$SRC_DIR" || ! -d "$SRC_DIR/backend" ]]; then
    if [[ "$VERSION" == "latest" ]]; then
        PKG_URL="https://github.com/$GGNET_REPO/releases/latest/download/ggnet.tar.gz"
    else
        PKG_URL="https://github.com/$GGNET_REPO/releases/download/$VERSION/ggnet.tar.gz"
    fi
    info "Downloading ggNet ($VERSION) from GitHub..."
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    wget -qO "$TMP_DIR/ggnet.tar.gz" "$PKG_URL" \
        || die "Cannot download $PKG_URL (does the release exist?)."
    tar -xzf "$TMP_DIR/ggnet.tar.gz" -C "$TMP_DIR"
    SRC_DIR="$TMP_DIR/ggnet"
fi
[[ -d "$SRC_DIR/backend" ]] || die "$SRC_DIR/backend not found — use --source DIR."
REQ_FILE=""
for f in "$SRC_DIR/backend/requirements.txt" "$SRC_DIR/requirements.txt"; do
    [[ -f "$f" ]] && { REQ_FILE="$f"; break; }
done
[[ -n "$REQ_FILE" ]] || die "No requirements.txt in backend/ or the root directory."

info "Proxmox: $(pveversion | head -n1)"

# ---------- 2. system disk (where the software goes) ----------
ROOT_SRC="$(findmnt -n -o SOURCE /)"
ROOT_FS="$(findmnt -n -o FSTYPE /)"
OS_POOL=""
[[ "$ROOT_FS" == "zfs" ]] && OS_POOL="${ROOT_SRC%%/*}"

ROOT_FREE_MB="$(df -Pm / | awk 'NR==2{print $4}')"
(( ROOT_FREE_MB >= 2048 )) || die "Only ${ROOT_FREE_MB} MB free on the system disk (/), at least 2 GB required."
info "Software goes on the system disk: $ROOT_SRC ($ROOT_FS), ${ROOT_FREE_MB} MB free"

# ---------- 3. ZFS pool selection ----------
mapfile -t POOL_LINES < <(zpool list -H -o name,size,free,health)
(( ${#POOL_LINES[@]} > 0 )) || die "No ZFS pool found on this host. Create a pool and run again."

if [[ -z "$POOL" ]]; then
    if interactive; then
        args=(); first=1
        for line in "${POOL_LINES[@]}"; do
            read -r name size free health <<<"$line"
            label="$size, $free free, $health"
            [[ "$name" == "$OS_POOL" ]] && label="$label  [OS pool]"
            state=OFF
            if [[ $first -eq 1 && "$name" != "$OS_POOL" ]]; then state=ON; first=0; fi
            args+=("$name" "$label" "$state")
        done
        POOL="$(wt --radiolist "Select the ZFS pool where ggNet will create its datasets and zvols (images, writebacks, snapshots, iscsi_targets, imports):" \
            18 78 8 "${args[@]}")" || die "Aborted."
    elif (( ${#POOL_LINES[@]} == 1 )); then
        POOL="$(awk '{print $1}' <<<"${POOL_LINES[0]}")"
    else
        die "Multiple pools found — specify --pool."
    fi
fi

zpool list -H "$POOL" >/dev/null 2>&1 || die "Pool '$POOL' does not exist."
POOL_HEALTH="$(zpool list -H -o health "$POOL")"
[[ "$POOL_HEALTH" == "ONLINE" ]] || confirm "Pool $POOL is $POOL_HEALTH (not ONLINE). Continue?" || die "Aborted."

if [[ "$POOL" == "$OS_POOL" ]]; then
    confirm "You selected the OS pool ($POOL) that Proxmox itself runs on.\n\nGame images and writebacks will share the disk with the system. A separate pool is recommended. Continue?" \
        || die "Aborted."
fi

# Pool settings for many PCs on SSDs. autotrim: ZFS tells the SSDs about
# freed blocks as it goes (Proxmox's monthly TRIM cron skips pools that are
# not all NVMe), which keeps writes fast as writebacks come and go.
POOL_ASHIFT="$(zpool get -H -o value ashift "$POOL")"
POOL_AUTOTRIM="$(zpool get -H -o value autotrim "$POOL")"
POOL_SSD=1
while read -r dev; do
    [[ "$(lsblk -dno ROTA "$(readlink -f "$dev")" 2>/dev/null | tr -d ' ')" == "0" ]] || POOL_SSD=0
done < <(zpool list -v -H -P "$POOL" | awk '$1 ~ /^\/dev\// {print $1}')
SET_AUTOTRIM=0
if [[ "$POOL_AUTOTRIM" == "off" && $POOL_SSD -eq 1 ]]; then
    confirm "Pool $POOL is on SSDs but autotrim is off.\n\nTurn autotrim on? ZFS then tells the SSDs about freed blocks as it goes, so writes stay fast (recommended)." \
        && SET_AUTOTRIM=1
fi
[[ "$POOL_ASHIFT" =~ ^[0-9]+$ && "$POOL_ASHIFT" -lt 12 && "$POOL_ASHIFT" -ne 0 ]] \
    && red "Warning: pool $POOL has ashift=$POOL_ASHIFT; SSDs work best with 12 (4K) or more (set when the pool is created)."

DS_ROOT="$POOL/$DATASET"
if zfs list -H "$DS_ROOT" >/dev/null 2>&1; then
    info "Dataset $DS_ROOT already exists — existing data will not be touched."
fi

# ---------- 4. interface / IP selection ----------
# list: iface ip/cidr  (vmbr* first)
mapfile -t ADDR_LINES < <(ip -4 -o addr show scope global | awk '{print $2, $4}' \
    | sort -k1,1 | awk '$1 ~ /^vmbr/ {print; next} {rest = rest $0 "\n"} END {printf "%s", rest}')

ip_on_host() { ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | grep -qx "$1"; }
cidr_of_ip() { ip -4 -o addr show scope global | awk -v ip="$1" '{split($4,a,"/"); if (a[1]==ip) print $4}' | head -n1; }
iface_of_ip() { ip -4 -o addr show scope global | awk -v ip="$1" '{split($4,a,"/"); if (a[1]==ip) print $2}' | head -n1; }

if [[ -z "$SERVICE_IP" && -n "$IFACE" ]]; then
    SERVICE_CIDR="$(ip -4 -o addr show dev "$IFACE" scope global | awk '{print $4}' | head -n1)"
    [[ -n "$SERVICE_CIDR" ]] || die "Interface $IFACE has no IPv4 address."
    SERVICE_IP="${SERVICE_CIDR%/*}"
fi

if [[ -z "$SERVICE_IP" ]]; then
    interactive || die "Specify --iface or --ip."
    args=(); i=0
    for line in "${ADDR_LINES[@]}"; do
        read -r ifc cidr <<<"$line"
        state=OFF; [[ $i -eq 0 ]] && state=ON
        args+=("$i" "$ifc  $cidr" "$state"); i=$((i+1))
    done
    args+=("manual" "Enter IP address manually" OFF)
    choice="$(wt --radiolist "Which interface/IP should iSCSI, DHCP, TFTP and HTTP run on?\n\nThis should be the network the gaming PCs are on." \
        20 78 10 "${args[@]}")" || die "Aborted."
    if [[ "$choice" == "manual" ]]; then
        SERVICE_IP="$(wt --inputbox "IP address for services:" 10 60)" || die "Aborted."
    else
        read -r IFACE SERVICE_CIDR <<<"${ADDR_LINES[$choice]}"
        SERVICE_IP="${SERVICE_CIDR%/*}"
    fi
fi

[[ "$SERVICE_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "Invalid IP address: $SERVICE_IP"
if ! ip_on_host "$SERVICE_IP"; then
    die "IP $SERVICE_IP is not assigned to any interface on this host — services could not bind to it. Add it in /etc/network/interfaces and run again."
fi
SERVICE_CIDR="${SERVICE_CIDR:-$(cidr_of_ip "$SERVICE_IP")}"
IFACE="${IFACE:-$(iface_of_ip "$SERVICE_IP")}"

# ---------- 5. web UI port and bind address ----------
if interactive; then
    WEB_PORT="$(wt --inputbox "Port for the ggNet web UI:" 10 60 "$WEB_PORT")" || die "Aborted."
    WEB_BIND_MODE="$(wt --radiolist "Where should the web UI listen?" 12 74 2 \
        service "Only on $SERVICE_IP ($IFACE)" ON \
        all     "On all interfaces (0.0.0.0)" OFF)" || die "Aborted."
fi
[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && (( WEB_PORT > 0 && WEB_PORT < 65536 )) || die "Invalid port: $WEB_PORT"
for p in "${RESERVED_PORTS[@]}"; do
    [[ "$WEB_PORT" == "$p" ]] && die "Port $WEB_PORT is reserved (Proxmox/ProxMenux/iSCSI)."
done
if ss -ltnH "sport = :$WEB_PORT" | grep -q . && ! systemctl is-active --quiet ggnet; then
    die "Port $WEB_PORT is already in use."
fi
case "$WEB_BIND_MODE" in
    service) WEB_BIND="$SERVICE_IP" ;;
    all)     WEB_BIND="0.0.0.0" ;;
    *) die "--web-bind must be 'service' or 'all'." ;;
esac

# ---------- 6. summary ----------
SUMMARY="Software:     $INSTALL_DIR  (system disk)
App data:     $DATA_DIR
Config:       $CONF_FILE

ZFS pool:     $POOL  ($POOL_HEALTH, ashift $POOL_ASHIFT, autotrim $POOL_AUTOTRIM$([[ $SET_AUTOTRIM -eq 1 ]] && echo " -> on"))
Datasets:     $DS_ROOT/{images,writebacks,snapshots,iscsi_targets,imports}

Service IF:   $IFACE  ($SERVICE_CIDR)
iSCSI portal: $SERVICE_IP:3260
PXE:          $SERVICE_IP  (proxyDHCP 67/4011 + TFTP 69 UDP, service ggnet-pxe;
              the router stays the DHCP server; only one PXE server per LAN)
HTTP boot:    $SERVICE_IP:80
Web UI:       http://$WEB_BIND:$WEB_PORT"

if interactive; then
    wt --yesno "$SUMMARY\n\nStart installation?" 24 78 || die "Aborted."
else
    echo "$SUMMARY"
fi

# ---------- 7. packages ----------
info "Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# dnsmasq-base: the binary only (no dnsmasq.service that would also serve DNS);
# ggNet runs its own instance, ggnet-pxe. ipxe: snponly.efi / ipxe.efi for TFTP.
# Secure Boot: shim-signed (shim signed by Microsoft, and its MokManager),
# sbsigntool and openssl to sign iPXE with the ggNet key.
# Windows images edited on the server: ntfs-3g (mount, ntfsresize), libwin-hivex-perl
# (hivexregedit), gdisk and fdisk (sgdisk, sfdisk) to grow a disk's data partition.
apt-get install -y -qq python3 python3-venv python3-pip targetcli-fb python3-rtslib-fb dnsmasq-base ipxe \
    shim-signed sbsigntool openssl ntfs-3g libwin-hivex-perl gdisk fdisk >/dev/null

# ---------- 8. ZFS datasets ----------
if [[ $SET_AUTOTRIM -eq 1 ]]; then
    zpool set autotrim=on "$POOL" && info "Autotrim turned on for $POOL."
fi
info "Preparing ZFS datasets on $POOL..."
# checksum=blake3: a strong checksum lets ZFS skip rewriting a block with the
# same data (nop-write), e.g. a registry hive rewritten whole for a few changed
# keys when a writeback is set up (OpenZFS 2.2+, PVE 9 has it).
if ! zfs list -H "$DS_ROOT" >/dev/null 2>&1; then
    zfs create -o compression=lz4 -o atime=off -o checksum=blake3 "$DS_ROOT"
fi
# imports: image files waiting to be imported (Images -> Import), on the pool:
# a VHDX of 100 GB would fill the Proxmox system disk.
for ds in images snapshots iscsi_targets imports; do
    zfs list -H "$DS_ROOT/$ds" >/dev/null 2>&1 || zfs create "$DS_ROOT/$ds"
done
# writebacks: thrown away at every restart, so no waiting for the disk on
# Windows' flushes (a PC with Keep Writeback gets sync=standard while it keeps it).
zfs list -H "$DS_ROOT/writebacks" >/dev/null 2>&1 || zfs create -o sync=disabled "$DS_ROOT/writebacks"

# ---------- 9. code + venv (system disk) ----------
info "Copying ggNet to $INSTALL_DIR..."
systemctl stop ggnet >/dev/null 2>&1 || true
mkdir -p "$INSTALL_DIR" "$DATA_DIR"/{tftp,http,logs,agent} "$CONF_DIR"
rm -rf "$INSTALL_DIR/backend" "$INSTALL_DIR/frontend"
cp -a "$SRC_DIR/backend" "$INSTALL_DIR/backend"
if [[ -d "$SRC_DIR/frontend/dist" ]]; then
    mkdir -p "$INSTALL_DIR/frontend"
    cp -a "$SRC_DIR/frontend/dist" "$INSTALL_DIR/frontend/dist"
else
    red "Warning: frontend/dist not found — build the React app (npm run build) before installing."
fi

[[ -f "$SRC_DIR/VERSION" ]] && cp "$SRC_DIR/VERSION" "$INSTALL_DIR/VERSION"
if [[ -f "$SRC_DIR/scripts/uninstall_ggnet.sh" ]]; then
    install -m 755 "$SRC_DIR/scripts/uninstall_ggnet.sh" /usr/local/sbin/ggnet-uninstall
fi
# Sign-in from the shell: the setup code of the first admin, a password reset.
cat >/usr/local/sbin/ggnet-admin <<EOF
#!/bin/sh
# Generated by install_ggnet.sh: python -m app.cli (setup-code | reset-password USER | create-admin USER)
cd "$INSTALL_DIR/backend" && GGNET_CONFIG="$CONF_FILE" exec "$INSTALL_DIR/venv/bin/python" -m app.cli "\$@"
EOF
chmod 755 /usr/local/sbin/ggnet-admin

if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$REQ_FILE"

# ---------- 10. configuration ----------
if [[ -f "$CONF_FILE" ]]; then
    cp -a "$CONF_FILE" "$CONF_FILE.bak.$(date +%Y%m%d%H%M%S)"
fi
cat >"$CONF_FILE" <<EOF
# Generated by install_ggnet.sh $(date -Iseconds)

[storage]
pool = "$POOL"
root_dataset = "$DS_ROOT"
images = "$DS_ROOT/images"
writebacks = "$DS_ROOT/writebacks"
snapshots = "$DS_ROOT/snapshots"
iscsi_targets = "$DS_ROOT/iscsi_targets"
imports = "$DS_ROOT/imports"

[network]
interface = "$IFACE"
server_ip = "$SERVICE_IP"
cidr = "$SERVICE_CIDR"

[services]
iscsi_portal = "$SERVICE_IP:3260"
# One shared target for all clients: <iscsi_iqn_prefix>:<iscsi_target_name>
iscsi_target_name = "storage"
# Map and unmap a PC's disks in one rtslib run (python3-rtslib-fb) instead of
# a targetcli call per step; false uses targetcli only.
iscsi_batch = true
dhcp_mode = "proxy"
tftp_root = "$DATA_DIR/tftp"
http_root = "$DATA_DIR/http"
http_port = 80

[boot]
# iBFT gateway for Windows: "none" (0.0.0.0, the default), "dhcp" or an IP.
gateway = "none"
# A PC whose MAC is not registered: "wait" on screen until it is added in the
# web UI (Machines -> New PCs), or "local" to boot its own disk.
unregistered = "wait"
# Both are defaults: once saved in the web UI (Settings -> Network boot), the
# saved values win. The PXE service the web UI shows and can restart:
pxe_service = "ggnet-pxe"
# The Secure Boot key (made by the installer; never leaves this server).
secureboot_dir = "$SB_DIR"
# Put the server's agent (Settings -> Client app) into OS images when they
# are prepared (Images -> Prepare image, and every Apply Writebacks of an OS).
inject_agent = true

[writebacks]
# Discard each client's writeback after it disconnects (shutdown/restart),
# unless the machine has Keep Writeback. grace_seconds rides out network blips.
# boot_window_seconds: after a Boot Mode PC's iPXE request, its iSCSI session
# drops while Windows takes over from iPXE; no discard during that window.
# spares: keep a spare of each Boot Mode PC's writebacks ready (<name>_spare),
# so a restart only swaps it in instead of cloning and setting Windows up.
auto_discard = true
grace_seconds = 30
poll_seconds = 5
boot_window_seconds = 300
spares = true

[web]
bind = "$WEB_BIND"
port = $WEB_PORT
frontend_dist = "$INSTALL_DIR/frontend/dist"

[paths]
data_dir = "$DATA_DIR"
database = "sqlite:///$DATA_DIR/ggnet.db"
# Unpacked ggnet-agent-win-x64.zip, offered in the web UI (Settings → Client app).
agent_dir = "$DATA_DIR/agent"
EOF
chmod 640 "$CONF_FILE"

# ---------- 10b. database schema ----------
info "Applying database migrations..."
(cd "$INSTALL_DIR/backend" && GGNET_CONFIG="$CONF_FILE" "$INSTALL_DIR/venv/bin/alembic" upgrade head)

# ---------- 10c. PXE: proxyDHCP + TFTP (ggnet-pxe) ----------
info "Setting up PXE (proxyDHCP + TFTP) on $IFACE..."
# UEFI PCs get snponly.efi (iPXE on the board's own network driver), then
# iPXE asks ggNet for the PC's script by MAC. The router keeps handing out
# addresses; dnsmasq only adds the boot information (proxy mode, no DNS).
install -m 644 /usr/lib/ipxe/snponly.efi /usr/lib/ipxe/ipxe.efi "$DATA_DIR/tftp/"

# Secure Boot: the PCs load shim (signed by Microsoft) from sb/, and shim
# loads sb/grubx64.efi, which is snponly.efi signed with the ggNet key. A PC
# with Secure Boot on accepts that key once in MokManager (the certificate is
# in the web UI, Settings -> Network boot); with Secure Boot off, shim starts
# iPXE without checking. The key is made once and kept on reinstall.
install -d -m 700 "$SB_DIR"
if [[ ! -f "$SB_DIR/ggnet-sb.key" ]]; then
    info "Creating the ggNet Secure Boot key in $SB_DIR..."
    openssl req -new -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
        -subj "/CN=ggNet Secure Boot ($(hostname -s))/" \
        -keyout "$SB_DIR/ggnet-sb.key" -out "$SB_DIR/ggnet-sb.crt" 2>/dev/null
    chmod 600 "$SB_DIR/ggnet-sb.key"
fi
openssl x509 -in "$SB_DIR/ggnet-sb.crt" -outform DER -out "$SB_DIR/ggnet-sb.cer"
chmod 644 "$SB_DIR/ggnet-sb.crt" "$SB_DIR/ggnet-sb.cer"
# Signs again whenever apt updates ipxe or shim (the hook below), so sb/ never
# keeps an old iPXE or an unsigned one.
cat >"$SB_SIGN" <<EOF
#!/bin/sh
# Generated by install_ggnet.sh: shim, MokManager and the signed iPXE for Secure Boot.
set -e
[ -f "$SB_DIR/ggnet-sb.key" ] || exit 0
T="$DATA_DIR/tftp/sb"
install -d -m 755 "\$T"
install -m 644 /usr/lib/shim/shimx64.efi.signed "\$T/shimx64.efi"
install -m 644 /usr/lib/shim/mmx64.efi.signed "\$T/mmx64.efi"
sbsign --key "$SB_DIR/ggnet-sb.key" --cert "$SB_DIR/ggnet-sb.crt" \
    --output "\$T/grubx64.efi.new" /usr/lib/ipxe/snponly.efi >/dev/null 2>&1
sbverify --cert "$SB_DIR/ggnet-sb.crt" "\$T/grubx64.efi.new" >/dev/null
chmod 644 "\$T/grubx64.efi.new"
mv "\$T/grubx64.efi.new" "\$T/grubx64.efi"
EOF
chmod 755 "$SB_SIGN"
cat >"$SB_APT_HOOK" <<EOF
// Generated by install_ggnet.sh: sign iPXE again after apt updates ipxe or shim.
DPkg::Post-Invoke { "if [ -x $SB_SIGN ]; then $SB_SIGN || echo 'ggnet: signing iPXE for Secure Boot failed' >&2; fi"; };
EOF
"$SB_SIGN" || die "Signing iPXE for Secure Boot failed; run $SB_SIGN to see why."
PXE_RANGE="$(python3 -c 'import ipaddress, sys; n = ipaddress.ip_interface(sys.argv[1]).network; print(f"{n.network_address},proxy,{n.netmask}")' "$SERVICE_CIDR")"
# ${net0/mac} is filled in by iPXE, not by this script.
PXE_URL="http://$SERVICE_IP:$WEB_PORT/api/v1/boot/ipxe?mac="'${net0/mac}'
cat >"$PXE_CONF_FILE" <<EOF
# Generated by install_ggnet.sh $(date -Iseconds): ggNet proxyDHCP + TFTP.
port=0
interface=$IFACE
bind-interfaces
dhcp-range=$PXE_RANGE
log-dhcp
enable-tftp
tftp-root=$DATA_DIR/tftp

# 1st round: the UEFI PXE ROM gets shim over TFTP, and shim the signed iPXE
# from the same folder (Secure Boot; without it shim just starts iPXE).
# 2nd round: iPXE (DHCP option 175) gets its script from ggNet over HTTP.
dhcp-match=set:ipxe,175
pxe-prompt="ggNet",1
pxe-service=tag:!ipxe,X86-64_EFI,"ggNet",sb/shimx64.efi
pxe-service=tag:ipxe,X86-64_EFI,"ggNet",$PXE_URL
dhcp-boot=tag:ipxe,$PXE_URL,,$SERVICE_IP
EOF
chmod 644 "$PXE_CONF_FILE"

# iPXE runs autoexec.ipxe from the TFTP server it came from before anything
# else. With it, iPXE only needs an address from the router and then asks
# ggNet directly, instead of waiting for the second ProxyDHCP reply (port
# 4011), which iPXE often missed when several PCs booted at once ("No more
# network devices"). It tries again until ggNet answers; a key boots the
# local disk.
# Before it asks, it reads the boot NIC's PCI IDs (device/vendor at 0,
# class at 9, subsystem at 0x2c, revision at 8) for Add hardware; a read that
# fails only leaves them empty.
cat >"$DATA_DIR/tftp/autoexec.ipxe" <<EOF
#!ipxe
# Generated by install_ggnet.sh $(date -Iseconds)
:retry
dhcp || goto again
set ggnet_pci \${pci/\${net0/busloc}.0.4} ||
set ggnet_cls \${pci/\${net0/busloc}.9.3} ||
set ggnet_sub \${pci/\${net0/busloc}.44.4} ||
set ggnet_rev \${pci/\${net0/busloc}.8.1} ||
chain --replace --autofree $PXE_URL&pci=\${ggnet_pci}&cls=\${ggnet_cls}&sub=\${ggnet_sub}&rev=\${ggnet_rev} && exit ||
:again
echo ggNet: no answer from $SERVICE_IP:$WEB_PORT yet.
prompt --timeout 3000 Trying again in 3 s. Press any key to boot from the local disk. && exit || goto retry
EOF
chmod 644 "$DATA_DIR/tftp/autoexec.ipxe"
if ! /usr/sbin/dnsmasq --test --conf-file="$PXE_CONF_FILE" >/dev/null 2>&1; then
    die "dnsmasq rejects $PXE_CONF_FILE; check it with: dnsmasq --test --conf-file=$PXE_CONF_FILE"
fi

cat >"$PXE_UNIT_FILE" <<EOF
[Unit]
Description=ggNet PXE (proxyDHCP + TFTP, dnsmasq)
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/usr/sbin/dnsmasq --test --conf-file=$PXE_CONF_FILE
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=$PXE_CONF_FILE --log-facility=-
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# ---------- 10d. service IP routing (another interface on the same subnet) ----------
# A Proxmox host often has its management bridge (e.g. vmbr0 on a 1G port) in
# the same subnet as the service interface (e.g. vmbr1 on a 10G bond). Linux
# then sends everything for that subnet out the first bridge, the service's
# iSCSI and HTTP replies included, and both bridges answer ARP for the
# service IP: the PCs boot and load games over the slow port. A source rule
# sends the service IP's traffic out its own interface, and each interface
# answers ARP only for its own address.
SHARED_IFACES="$(ip -4 -o addr show scope global | python3 -c '
import ipaddress, sys
service = ipaddress.ip_interface(sys.argv[1])
others = {f[1] for f in (l.split() for l in sys.stdin)
          if f[1] != sys.argv[2] and ipaddress.ip_interface(f[3]).network == service.network}
print(" ".join(sorted(others)))' "$SERVICE_CIDR" "$IFACE")"
if [[ -n "$SHARED_IFACES" ]]; then
    info "$SHARED_IFACES shares $SERVICE_CIDR with $IFACE: routing $SERVICE_IP out $IFACE..."
    LAN_NET="$(python3 -c 'import ipaddress, sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$SERVICE_CIDR")"
    LAN_GW="$(ip -4 route show default | awk '{print $3; exit}')"
    LAN_DEFAULT=""
    if [[ -n "$LAN_GW" ]] && python3 -c 'import ipaddress, sys; sys.exit(ipaddress.ip_address(sys.argv[1]) not in ipaddress.ip_interface(sys.argv[2]).network)' "$LAN_GW" "$SERVICE_CIDR"; then
        LAN_DEFAULT=" && ip route add default via $LAN_GW dev $IFACE table 140"
    fi
    cat >"$LAN_SYSCTL_FILE" <<EOF
# Generated by install_ggnet.sh: $SHARED_IFACES and $IFACE share $LAN_NET.
# Each interface answers ARP only for its own address, so the PCs reach the
# ggNet service IP $SERVICE_IP on $IFACE.
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
EOF
    sysctl -q -p "$LAN_SYSCTL_FILE"
    cat >"$LAN_UNIT_FILE" <<EOF
[Unit]
Description=ggNet: traffic from the service IP $SERVICE_IP leaves on $IFACE
# $SHARED_IFACES shares $LAN_NET with $IFACE, and the main routing table picks
# one interface for the whole subnet; this source rule keeps the service on $IFACE.
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip rule del priority 1040 2>/dev/null; ip route flush table 140 2>/dev/null; ip route add $LAN_NET dev $IFACE src $SERVICE_IP table 140$LAN_DEFAULT && ip rule add from $SERVICE_IP lookup 140 priority 1040'
ExecStop=/bin/sh -c 'ip rule del priority 1040; ip route flush table 140'

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable ggnet-lan-route >/dev/null 2>&1
    systemctl restart ggnet-lan-route || red "ggnet-lan-route failed. Logs: journalctl -u ggnet-lan-route -n 50"
elif [[ -f "$LAN_UNIT_FILE" ]]; then
    # The service interface no longer shares its subnet: drop the old rule.
    systemctl disable --now ggnet-lan-route >/dev/null 2>&1 || true
    rm -f "$LAN_UNIT_FILE" "$LAN_SYSCTL_FILE"
fi

# ---------- 11. systemd ----------
cat >"$UNIT_FILE" <<EOF
[Unit]
Description=ggNet diskless boot manager
After=network-online.target zfs.target zfs-mount.service
Wants=network-online.target

[Service]
Type=simple
Environment=GGNET_CONFIG=$CONF_FILE
WorkingDirectory=$INSTALL_DIR/backend
ExecStart=$INSTALL_DIR/venv/bin/uvicorn $APP_MODULE --host $WEB_BIND --port $WEB_PORT
Restart=on-failure
RestartSec=3
# On stop or restart only uvicorn gets SIGTERM: a writeback reset under way
# (zfs, LIO) finishes before it exits, instead of being cut off mid-way.
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ggnet
systemctl enable ggnet-pxe >/dev/null 2>&1
systemctl restart ggnet-pxe || red "ggnet-pxe failed to start. Logs: journalctl -u ggnet-pxe -n 50"

sleep 2
if systemctl is-active --quiet ggnet; then
    green "ggNet is running:  http://$([[ $WEB_BIND == 0.0.0.0 ]] && echo "$SERVICE_IP" || echo "$WEB_BIND"):$WEB_PORT"
    # The first admin is created in the web UI with this one-time code.
    if SETUP_CODE="$(/usr/local/sbin/ggnet-admin setup-code 2>/dev/null)"; then
        green "Create the first admin in the web UI with setup code:  $SETUP_CODE"
        echo "   (shown again by: ggnet-admin setup-code)"
    fi
else
    red "The service failed to start. Logs: journalctl -u ggnet -n 50"
    exit 1
fi

# ---------- 12. firewall note ----------
if pve-firewall status 2>/dev/null | grep -q "enabled"; then
    echo
    info "Proxmox firewall is enabled. Add rules for $IFACE (Datacenter/Node → Firewall):"
    echo "   TCP $WEB_PORT   (web UI)"
    echo "   TCP 3260        (iSCSI)"
    echo "   UDP 67, 69, 4011 (proxyDHCP, TFTP)"
    echo "   TCP 80          (HTTP boot)"
fi
