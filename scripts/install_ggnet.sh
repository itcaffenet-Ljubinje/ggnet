#!/usr/bin/env bash
# ggNet installer for Proxmox VE
#
#  - Software goes on the Proxmox system disk:  /opt/ggnet       (code + venv)
#                                               /var/lib/ggnet   (db, tftp, http root)
#                                               /etc/ggnet/config.toml
#  - ZFS operations (images, writebacks, snapshots, iscsi_targets) go to the pool you pick
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
        POOL="$(wt --radiolist "Select the ZFS pool where ggNet will create its datasets and zvols (images, writebacks, snapshots, iscsi_targets):" \
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

ZFS pool:     $POOL  ($POOL_HEALTH)
Datasets:     $DS_ROOT/{images,writebacks,snapshots,iscsi_targets}

Service IF:   $IFACE  ($SERVICE_CIDR)
iSCSI portal: $SERVICE_IP:3260
DHCP/TFTP:    $SERVICE_IP  (proxyDHCP, 67/69 UDP)
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
apt-get install -y -qq python3 python3-venv python3-pip targetcli-fb dnsmasq >/dev/null
# ggNet runs its own dnsmasq instance; the default service must not interfere with Proxmox
systemctl disable --now dnsmasq >/dev/null 2>&1 || true

# ---------- 8. ZFS datasets ----------
info "Preparing ZFS datasets on $POOL..."
if ! zfs list -H "$DS_ROOT" >/dev/null 2>&1; then
    zfs create -o compression=lz4 -o atime=off "$DS_ROOT"
fi
for ds in images writebacks snapshots iscsi_targets; do
    zfs list -H "$DS_ROOT/$ds" >/dev/null 2>&1 || zfs create "$DS_ROOT/$ds"
done

# ---------- 9. code + venv (system disk) ----------
info "Copying ggNet to $INSTALL_DIR..."
systemctl stop ggnet >/dev/null 2>&1 || true
mkdir -p "$INSTALL_DIR" "$DATA_DIR"/{tftp,http,logs} "$CONF_DIR"
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

[network]
interface = "$IFACE"
server_ip = "$SERVICE_IP"
cidr = "$SERVICE_CIDR"

[services]
iscsi_portal = "$SERVICE_IP:3260"
# One shared target for all clients: <iscsi_iqn_prefix>:<iscsi_target_name>
iscsi_target_name = "storage"
dhcp_mode = "proxy"
tftp_root = "$DATA_DIR/tftp"
http_root = "$DATA_DIR/http"
http_port = 80

[writebacks]
# Discard each client's writeback after it disconnects (shutdown/restart),
# unless the machine has Keep Writeback. grace_seconds rides out network blips.
auto_discard = true
grace_seconds = 30
poll_seconds = 5

[web]
bind = "$WEB_BIND"
port = $WEB_PORT
frontend_dist = "$INSTALL_DIR/frontend/dist"

[paths]
data_dir = "$DATA_DIR"
database = "sqlite:///$DATA_DIR/ggnet.db"
EOF
chmod 640 "$CONF_FILE"

# ---------- 10b. database schema ----------
info "Applying database migrations..."
(cd "$INSTALL_DIR/backend" && GGNET_CONFIG="$CONF_FILE" "$INSTALL_DIR/venv/bin/alembic" upgrade head)

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

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ggnet

sleep 2
if systemctl is-active --quiet ggnet; then
    green "ggNet is running:  http://$([[ $WEB_BIND == 0.0.0.0 ]] && echo "$SERVICE_IP" || echo "$WEB_BIND"):$WEB_PORT"
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
