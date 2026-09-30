#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash -s -- [path] [ref]
#    or: bash continuum/install.sh [path-to-consumer-repo] [ref]
# ref defaults to main.
DEST="${1:-.}"
REF="${2:-main}"
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid ref: $REF" >&2; exit 1; }
if [[ ! -d "$DEST/.github/workflows" ]]; then
  mkdir -p "$DEST/.github/workflows"
fi
BASE="https://raw.githubusercontent.com/kodmial/continuum/${REF}/.github/caller-stubs"
STUBS=(
  add-review-label.yml
  auto-merge.yml
  bootstrap-runtime-secret.yml
  ci.yml
  coderabbit-retry.yml
  coderabbit-unresolved.yml
  issue-scheduler.yml
  opencode-repair.yml
  opencode-unresolved.yml
  opencode.yml
  packaging-smoke.yml
  pr-agent.yml
  release-automation-merge.yml
  release-pr.yml
  release.yml
  remove-review-label.yml
)
LOCAL_STUBS_DIR="$(dirname "$0")/.github/caller-stubs"
if [[ -d "$LOCAL_STUBS_DIR" ]]; then
  shopt -s nullglob
  files=("$LOCAL_STUBS_DIR"/*.yml)
  shopt -u nullglob
  if (( ${#files[@]} )); then
    cp "${files[@]}" "$DEST/.github/workflows/"
  fi
else
  for f in "${STUBS[@]}"; do
    curl -fsSL "$BASE/$f" -o "$DEST/.github/workflows/$f"
  done
fi
echo "Continuum callers installed to $DEST/.github/workflows/ (ref: $REF)"
