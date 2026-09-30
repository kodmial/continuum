#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: bash continuum/install.sh [path-to-consumer-repo]
DEST="${1:-.}"
if [[ ! -d "$DEST/.github/workflows" ]]; then
  mkdir -p "$DEST/.github/workflows"
fi
# Fetch caller stubs from continuum
BASE="https://raw.githubusercontent.com/kodmial/continuum/main/.github/caller-stubs"
# Fallback: generate from local if BASE unavailable (local install)
if [[ -d "$(dirname "$0")/.github/caller-stubs" ]]; then
  cp "$(dirname "$0")/.github/caller-stubs"/*.yml "$DEST/.github/workflows/"
else
  for f in $(curl -fsSL "https://api.github.com/repos/kodmial/continuum/contents/.github/caller-stubs" | grep '"name"' | cut -d'"' -f4); do
    curl -fsSL "$BASE/$f" -o "$DEST/.github/workflows/$f"
  done
fi
echo "Continuum callers installed to $DEST/.github/workflows/"
