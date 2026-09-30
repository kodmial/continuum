#!/bin/bash
# ensure-macports.sh — install a supported official MacPorts release on the
# runner when `port` is absent. The installer version is pinned (not a mutable
# "latest" payload) and the package signature is verified before installation.
#
# Usage: ensure-macports.sh [--version VER]
# Env: MACPORTS_VERSION overrides the default pin.
#
# All comments, logs and errors are in English.

set -euo pipefail

# Pinned MacPorts version. Bump deliberately after checking
# https://www.macports.org/install.php and the macports-base releases.
DEFAULT_MACPORTS_VERSION="2.12.6"
MACPORTS_VERSION="${MACPORTS_VERSION:-$DEFAULT_MACPORTS_VERSION}"
while [ $# -gt 0 ]; do
  case "$1" in
    --version) MACPORTS_VERSION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if command -v port >/dev/null 2>&1; then
  echo "[smoke] MacPorts already present: $(port version 2>&1 | head -n1)"
  exit 0
fi

MACOS_MAJOR="$(/usr/bin/sw_vers -productVersion | cut -d. -f1)"
case "$MACOS_MAJOR" in
  26) DARWIN_TAG="Tahoe" ;;
  15) DARWIN_TAG="Sequoia" ;;
  14) DARWIN_TAG="Sonoma" ;;
  13) DARWIN_TAG="Ventura" ;;
  *) echo "[smoke] Unsupported macOS major $MACOS_MAJOR for the pinned MacPorts installer" >&2; exit 1 ;;
esac

PKG_NAME="MacPorts-$MACPORTS_VERSION-$MACOS_MAJOR-$DARWIN_TAG.pkg"
PKG_URL="https://distfiles.macports.org/MacPorts/$PKG_NAME"
PKG_PATH="/tmp/$PKG_NAME"
echo "[smoke] Downloading pinned MacPorts $MACPORTS_VERSION ($PKG_URL)"
curl -fsSL "$PKG_URL" -o "$PKG_PATH"

echo "[smoke] Verifying installer signature"
SIGNATURE="$(pkgutil --check-signature "$PKG_PATH" 2>&1)" || {
  echo "$SIGNATURE" >&2
  echo "[smoke] pkgutil rejected the MacPorts pkg signature" >&2
  exit 1
}
echo "$SIGNATURE"
echo "$SIGNATURE" | grep -q 'Status: signed by a developer certificate issued by Apple' || {
  echo "[smoke] MacPorts pkg is not signed by a trusted Apple developer certificate; refusing to install" >&2
  exit 1
}
# Official MacPorts .pkg releases are signed "Developer ID Installer: Joshua Root"
# (the MacPorts lead maintainer) — the identity string does not contain "MacPorts".
echo "$SIGNATURE" | grep -q -E 'Developer ID Installer: .*(MacPorts|Joshua Root)' || {
  echo "[smoke] MacPorts pkg signature does not reference the expected MacPorts signer; refusing to install" >&2
  exit 1
}

echo "[smoke] Installing MacPorts $MACPORTS_VERSION"
sudo installer -pkg "$PKG_PATH" -target /
export PATH="/opt/local/bin:/opt/local/sbin:$PATH"
port version || { echo "[smoke] port is not functional after install" >&2; exit 1; }
echo "[smoke] MacPorts ready: $(port version 2>&1 | head -n1)"
