#!/usr/bin/env bash
# ggNet uninstaller for Proxmox VE
#
# Removes the service and the software. ZFS datasets (game images, writebacks)
# are NEVER deleted automatically — the command to delete them is printed instead.
#
#   ggnet-uninstall            # asks whether to delete config/data too
#   ggnet-uninstall --purge    # also deletes /etc/ggnet and /var/lib/ggnet, no prompt
#   ggnet-uninstall --yes      # no prompts, config/data are kept

set -euo pipefail

INSTALL_DIR="/opt/ggnet"
DATA_DIR="/var/lib/ggnet"
CONF_DIR="/etc/ggnet"
CONF_FILE="$CONF_DIR/config.toml"
UNIT_FILE="/etc/systemd/system/ggnet.service"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge)  PURGE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

ask() {
    [[ $ASSUME_YES -eq 1 || ! -t 0 ]] && return 1
    local reply
    read -r -p "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Root dataset from the config, so we know what to print at the end
ROOT_DS=""
if [[ -f "$CONF_FILE" ]]; then
    ROOT_DS="$(awk -F'"' '/^root_dataset[[:space:]]*=/{print $2; exit}' "$CONF_FILE")"
fi

echo "==> Stopping ggNet service..."
systemctl disable --now ggnet >/dev/null 2>&1 || true
rm -f "$UNIT_FILE"
systemctl daemon-reload

echo "==> Removing $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"

if [[ $PURGE -eq 1 ]] || ask "Also delete configuration ($CONF_DIR) and application data ($DATA_DIR)?"; then
    rm -rf "$CONF_DIR" "$DATA_DIR"
    echo "==> Configuration and data deleted."
else
    echo "==> $CONF_DIR and $DATA_DIR were kept (a reinstall will reuse them)."
fi

rm -f /usr/local/sbin/ggnet-uninstall

echo
echo "ggNet has been removed."
if [[ -n "$ROOT_DS" ]] && zfs list -H "$ROOT_DS" >/dev/null 2>&1; then
    echo
    echo "ZFS datasets were left untouched: $ROOT_DS"
    echo "To delete them (IRREVERSIBLE, all images are lost):"
    echo "   zfs destroy -r $ROOT_DS"
fi
