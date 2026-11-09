#!/usr/bin/env bash
# Update ggNet to a release (installed as /usr/local/sbin/ggnet-update).
#
#   ggnet-update [--version vX.Y.Z]
#   ggnet-update --version vX.Y.Z --restore-db /var/lib/ggnet/db-backups/ggnet-…-from-X.Y.Z.dump
#
# --restore-db goes back to an older version (Settings -> Updates -> Downgrade):
# after the package is checked, ggnet stops, the database is put back from the
# dump the installer took before it updated away from that version, and the
# older release is installed. If the dump cannot be put back, nothing else is
# changed and ggnet starts again.
#
# Downloads ggnet.tar.gz of the release (default: the latest) from GitHub,
# checks it against the release's ggnet.tar.gz.sha256, and runs the
# release's own installer with --update: every setting comes from the
# installed /etc/ggnet/config.toml, which is kept as it is. The web UI
# (Settings -> Updates) runs this as the transient unit ggnet-update, so it
# goes on while the installer restarts the ggnet service.
set -euo pipefail

GGNET_REPO="itcaffenet-Ljubinje/ggnet"
VERSION="latest"
RESTORE_DB=""

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --repo)    GGNET_REPO="$2"; shift 2 ;;
        --restore-db) RESTORE_DB="$2"; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "Run as root."
[[ "$VERSION" == "latest" || "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]] \
    || die "Invalid version: $VERSION (e.g. v0.2.0)"

if [[ "$VERSION" == "latest" ]]; then
    BASE="https://github.com/$GGNET_REPO/releases/latest/download"
else
    BASE="https://github.com/$GGNET_REPO/releases/download/$VERSION"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "==> Downloading ggNet $VERSION..."
wget -qO "$TMP_DIR/ggnet.tar.gz" "$BASE/ggnet.tar.gz" || die "Cannot download $BASE/ggnet.tar.gz"
wget -qO "$TMP_DIR/ggnet.tar.gz.sha256" "$BASE/ggnet.tar.gz.sha256" \
    || die "The release has no ggnet.tar.gz.sha256; not installing an unchecked package."
echo "==> Checking its SHA-256..."
(cd "$TMP_DIR" && sha256sum -c --status ggnet.tar.gz.sha256) || die "SHA-256 mismatch: the download is damaged."

tar -xzf "$TMP_DIR/ggnet.tar.gz" -C "$TMP_DIR"
[[ -f "$TMP_DIR/ggnet/scripts/install_ggnet.sh" ]] || die "The package has no installer."
if [[ -n "$RESTORE_DB" ]]; then
    [[ "$VERSION" != "latest" ]] || die "--restore-db needs --version."
    [[ -f "$RESTORE_DB" ]] || die "Database backup $RESTORE_DB not found."
    CONF="${GGNET_CONFIG:-/etc/ggnet/config.toml}"
    DB_NAME="$(python3 - "$CONF" <<'PY'
import sys, tomllib
from urllib.parse import urlparse
url = tomllib.load(open(sys.argv[1], "rb")).get("paths", {}).get("database", "")
print(urlparse(url).path.lstrip("/") if url.startswith("postgresql") else "")
PY
)"
    [[ -n "$DB_NAME" ]] || die "The database is not PostgreSQL; a downgrade needs one."
    echo "==> Stopping ggnet and putting the database back from $(basename "$RESTORE_DB")..."
    systemctl stop ggnet
    if ! pg_restore -h /run/postgresql --clean --if-exists --single-transaction -d "$DB_NAME" "$RESTORE_DB"; then
        systemctl start ggnet || true
        die "Putting the database back failed; ggNet keeps its current version."
    fi
fi
echo "==> Installing $(cat "$TMP_DIR/ggnet/VERSION" 2>/dev/null || echo "$VERSION")..."
# The installer also brings the release's agent package for the PCs.
bash "$TMP_DIR/ggnet/scripts/install_ggnet.sh" --update --source "$TMP_DIR/ggnet"

echo "==> ggNet updated."
