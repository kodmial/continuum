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
# Only use local templates when installing the default working-tree version.
# An explicit ref must fetch that revision, even when run from a local clone.
LOCAL_STUBS_DIR="$(dirname "${BASH_SOURCE[0]:-$0}")/.github/caller-stubs"
for f in "${STUBS[@]}"; do
  if [[ $# -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
    template="$(cat "$LOCAL_STUBS_DIR/$f")"
  else
    template="$(curl -fsSL "$BASE/$f")"
  fi
  # Use the same revision for the workflow and its fallback scripts.
  printf '%s\n' "$template" | sed \
    -e "s|kodmial/continuum/\\(.github/workflows/[^@ ]*\\)@main|kodmial/continuum/\\1@$REF|g" \
    -e "s|continuum_ref: main|continuum_ref: '$REF'|" \
    > "$DEST/.github/workflows/$f"
done
echo "Continuum callers installed to $DEST/.github/workflows/ (ref: $REF)"
