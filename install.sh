#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash -s -- [path] [ref] [nanodictate|parent]
#    or: bash continuum/install.sh [path-to-consumer-repo] [ref] [nanodictate|parent]
# ref defaults to main.
DEST="${1:-.}"
REF="${2:-main}"
PROFILE="${3:-nanodictate}"
case "$PROFILE" in
  nanodictate|parent) ;;
  *) echo "invalid profile: $PROFILE" >&2; exit 1 ;;
esac
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid ref: $REF" >&2; exit 1; }
if [[ ! -d "$DEST/.github/workflows" ]]; then
  mkdir -p "$DEST/.github/workflows"
fi
STUB_PATH=".github/caller-stubs"
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
if [[ "$PROFILE" == parent ]]; then
  STUB_PATH="$STUB_PATH/parent"
  STUBS=(child-dispatcher.yml child-worker.yml child-review.yml child-pr-review.yml)
fi
BASE="https://raw.githubusercontent.com/kodmial/continuum/${REF}/$STUB_PATH"
# Only use local templates when installing the default working-tree version.
# An explicit ref must fetch that revision, even when run from a local clone.
LOCAL_STUBS_DIR="$(dirname "${BASH_SOURCE[0]:-$0}")/$STUB_PATH"
# Every installed caller carries the `continuum-` prefix. This is the strict
# contract: a workflow named continuum-*.yml in a consumer repository comes
# from Continuum and must not be hand-edited, while any other workflow in the
# same directory is project-owned. Continuum's own dispatchers rely on these
# exact names, so the prefix is part of the interface, not a cosmetic label.
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
    -e "s|engine_ref: main|engine_ref: '$REF'|" \
    > "$DEST/.github/workflows/continuum-$f"
done
echo "Continuum callers installed to $DEST/.github/workflows/ (profile: $PROFILE, ref: $REF)"
