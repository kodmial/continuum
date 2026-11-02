#!/usr/bin/env bash
set -euo pipefail

ruby -E UTF-8 scripts/test-continuum.rb
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m continuum.trigger_manifest --check

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

mkdir -p "$PWD/.opencode-tmp/bin"
# Pin both the installer URL and its content: the tag can move and a
# compromised download would otherwise execute with this script's privileges.
actionlint_installer="$PWD/.opencode-tmp/download-actionlint.bash"
actionlint_installer_sha256=72fa3e45ac20f3c3a512d6747b4fcf719e21f890e8c43e78d48a41fdfb900c4e
curl -fsSL --retry 3 -o "$actionlint_installer" \
  https://raw.githubusercontent.com/rhysd/actionlint/914e7df21a07ef503a81201c76d2b11c789d3fca/scripts/download-actionlint.bash
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s  %s\n' "$actionlint_installer_sha256" "$actionlint_installer" | sha256sum -c -
else
  actual_sha256="$(shasum -a 256 "$actionlint_installer" | awk '{print $1}')"
  if [[ "$actual_sha256" != "$actionlint_installer_sha256" ]]; then
    echo "actionlint installer checksum mismatch" >&2
    exit 1
  fi
fi
bash "$actionlint_installer" 1.7.12 "$PWD/.opencode-tmp/bin"
.opencode-tmp/bin/actionlint -shellcheck= -pyflakes= \
  .github/workflows/*.yml \
  .github/caller-stubs/*.yml \
  .github/caller-stubs/tech/*.yml \
  .github/caller-stubs/parent/*.yml
