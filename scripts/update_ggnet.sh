#!/usr/bin/env bash
# Update ggNet to a release (installed as /usr/local/sbin/ggnet-update).
#
#   ggnet-update [--version vX.Y.Z]
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

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --repo)    GGNET_REPO="$2"; shift 2 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
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
echo "==> Installing $(cat "$TMP_DIR/ggnet/VERSION" 2>/dev/null || echo "$VERSION")..."
# The installer also brings the release's agent package for the PCs.
bash "$TMP_DIR/ggnet/scripts/install_ggnet.sh" --update --source "$TMP_DIR/ggnet"

echo "==> ggNet updated."
