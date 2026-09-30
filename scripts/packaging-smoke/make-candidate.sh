#!/bin/bash
# make-candidate.sh — derive candidate Cask/Portfile metadata from the
# production templates, adapted ONLY to the exact CI-built artifact bytes.
#
# Usage:
#   make-candidate.sh --version VER --dist-dir DIR --out-dir DIR
#
# Inputs: DIR holds nanodictate-VER-macos-{arm64,x86_64}.{tar.gz,zip} (the
# exact bytes built once per architecture and passed to the smoke jobs).
# Outputs in OUT-DIR:
#   candidate-cask.rb                 (temporary Cask, file:// source)
#   candidate-ports/audio/nanodictate/Portfile (+ config.example.toml copy)
#
# Production install behavior is preserved: only version/source/SHA fields are
# adapted, never the install/service/launchd logic.

set -euo pipefail

VERSION=""
DIST_DIR=""
OUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --dist-dir) DIST_DIR="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$VERSION" ] && [ -n "$DIST_DIR" ] && [ -n "$OUT_DIR" ] || {
  echo "Usage: make-candidate.sh --version VER --dist-dir DIR --out-dir DIR" >&2
  exit 2
}

for asset in \
  "nanodictate-$VERSION-macos-arm64.tar.gz" \
  "nanodictate-$VERSION-macos-x86_64.tar.gz" \
  "nanodictate-$VERSION-macos-arm64.zip" \
  "nanodictate-$VERSION-macos-x86_64.zip"; do
  [ -f "$DIST_DIR/$asset" ] || { echo "Missing candidate artifact: $DIST_DIR/$asset" >&2; exit 1; }
done

TARBALL_ARM_SHA="$(shasum -a 256 "$DIST_DIR/nanodictate-$VERSION-macos-arm64.tar.gz" | awk '{print $1}')"
TARBALL_X86_SHA="$(shasum -a 256 "$DIST_DIR/nanodictate-$VERSION-macos-x86_64.tar.gz" | awk '{print $1}')"
ZIP_ARM_SHA="$(shasum -a 256 "$DIST_DIR/nanodictate-$VERSION-macos-arm64.zip" | awk '{print $1}')"
ZIP_X86_SHA="$(shasum -a 256 "$DIST_DIR/nanodictate-$VERSION-macos-x86_64.zip" | awk '{print $1}')"
echo "[smoke] candidate $VERSION tarball arm64=$TARBALL_ARM_SHA x86_64=$TARBALL_X86_SHA"
echo "[smoke] candidate $VERSION zip arm64=$ZIP_ARM_SHA x86_64=$ZIP_X86_SHA"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$OUT_DIR" "$OUT_DIR/candidate-ports/audio/nanodictate"

# --- Candidate Cask: production logic, file:// source + exact zip SHAs --------
sed -e "s/__VERSION__/$VERSION/g" \
  -e "s/__ZIP_SHA256_ARM64__/$ZIP_ARM_SHA/g" \
  -e "s/__ZIP_SHA256_X86_64__/$ZIP_X86_SHA/g" \
  "$ROOT/packaging/homebrew/Casks/nanodictate.rb.tpl" > "$OUT_DIR/candidate-cask.rb"
# Point the interpolated download at the exact local bytes. The template URL
# ends with `nanodictate-__VERSION__-macos-#{arch}.zip` (already version-filled
# above); only the host prefix is swapped for a file:// directory.
DIST_ABS="$(cd "$DIST_DIR" && pwd -P)"
python3 - "$OUT_DIR/candidate-cask.rb" "$DIST_ABS" <<'EOF'
import sys
path, dist = sys.argv[1], sys.argv[2]
src = open(path).read()
prefix = 'url "https://github.com/kodmial/nanodictate/releases/download/v'
assert prefix in src, "candidate cask template URL shape changed"
start = src.index(prefix)
end = src.index('.zip"', start) + len('.zip"')
replacement = 'url "file://' + dist + '/'
# Keep the version/arch-suffixed filename the template already carries.
tail = src[start + len(prefix):end]
# tail looks like `0.1.7/nanodictate-0.1.7-macos-#{arch}.zip"`.
filename = tail.split('/', 1)[1]
src = src[:start] + replacement + filename + src[end:]
open(path, 'w').write(src)
EOF
ruby -c "$OUT_DIR/candidate-cask.rb"
echo "[smoke] wrote $OUT_DIR/candidate-cask.rb"

# --- Candidate Portfile: production logic, file:// source + exact tarball SHAs --
sed -e "s/__VERSION__/$VERSION/g" \
  -e "s/__SHA256_ARM64__/$TARBALL_ARM_SHA/g" \
  -e "s/__SHA256_X86_64__/$TARBALL_X86_SHA/g" \
  -e "s/__MAINTAINERS__/@kodmial/g" \
  -e "s/__REVISION__/0/g" \
  "$ROOT/packaging/macports/Portfile.tpl" > "$OUT_DIR/candidate-ports/audio/nanodictate/Portfile"
python3 - "$OUT_DIR/candidate-ports/audio/nanodictate/Portfile" "$DIST_ABS" <<'EOF'
import sys
path, dist = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True)
for i, line in enumerate(lines):
    if line.startswith('master_sites'):
        lines[i] = 'master_sites        file://' + dist + '\n'
        break
else:
    raise SystemExit("candidate Portfile template lost its master_sites line")
open(path, 'w').write(''.join(lines))
EOF
cp "$ROOT/config.example.toml" "$OUT_DIR/candidate-ports/audio/nanodictate/config.example.toml"
grep -q "github.setup.*kodmial nanodictate $VERSION" "$OUT_DIR/candidate-ports/audio/nanodictate/Portfile"
grep -q "$TARBALL_ARM_SHA" "$OUT_DIR/candidate-ports/audio/nanodictate/Portfile"
grep -q "$TARBALL_X86_SHA" "$OUT_DIR/candidate-ports/audio/nanodictate/Portfile"
echo "[smoke] wrote $OUT_DIR/candidate-ports/audio/nanodictate/Portfile"
