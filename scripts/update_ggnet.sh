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
bash "$TMP_DIR/ggnet/scripts/install_ggnet.sh" --update --source "$TMP_DIR/ggnet"

# The release's agent for the PCs (Settings -> Client app): Disk Mode agents
# update themselves from it, OS images get it with Prepare image. A problem
# here leaves the old agent package and does not undo the server update.
update_agent() {
    local conf="${GGNET_CONFIG:-/etc/ggnet/config.toml}" dir
    dir="$(python3 - "$conf" <<'PY'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
p = c.get("paths", {})
print(p.get("agent_dir") or (p.get("data_dir", "/var/lib/ggnet") + "/agent"))
PY
)" || { echo "WARNING: cannot read the agent folder from $conf; the agent package is unchanged." >&2; return; }
    if ! wget -qO "$TMP_DIR/ggnet-agent-win-x64.zip" "$BASE/ggnet-agent-win-x64.zip" \
        || ! wget -qO "$TMP_DIR/ggnet-agent-win-x64.zip.sha256" "$BASE/ggnet-agent-win-x64.zip.sha256"; then
        echo "WARNING: the release has no checked agent package; the PCs keep their agent." >&2
        return
    fi
    if ! (cd "$TMP_DIR" && sha256sum -c --status ggnet-agent-win-x64.zip.sha256); then
        echo "WARNING: SHA-256 mismatch for the agent package; the PCs keep their agent." >&2
        return
    fi
    # Unpack beside the folder, then replace each file by a rename: a PC
    # downloading during the update gets the old file or the new, never half.
    mkdir -p "$dir"
    python3 - "$TMP_DIR/ggnet-agent-win-x64.zip" "$dir" <<'PY' || { echo "WARNING: unpacking the agent package failed; the PCs keep their agent." >&2; return; }
import os, sys, zipfile
zip_path, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as z:
    for info in z.infolist():
        parts = info.filename.split("/")
        # Only the files right in ggnet-agent/: never a path out of the folder.
        if info.is_dir() or len(parts) != 2 or parts[0] != "ggnet-agent" or parts[1] in ("", ".", ".."):
            continue
        tmp = os.path.join(target, "." + parts[1] + ".new")
        with z.open(info) as src, open(tmp, "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, os.path.join(target, parts[1]))
PY
    echo "==> Agent package $(cat "$dir/VERSION" 2>/dev/null || echo "?") in $dir."
}
echo "==> Updating the agent package for the PCs..."
update_agent
echo "==> ggNet updated."
