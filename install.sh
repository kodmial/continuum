#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash -s -- [path] [ref] [core|parent|tech]
#    or: bash continuum/install.sh [path-to-consumer-repo] [ref] [core|parent|tech]
# ref defaults to main. `core` is the task-domain layer every project needs;
# `tech` is the opt-in technology library (the `continuum-tech-<tech>-` prefix marks the library);
# `parent` adds only child-execution callers.
# Continuum never triggers its own technology library: consumers opt into it.
DEST="${1:-.}"
REF="${2:-main}"
SET="${3:-core}"
case "$SET" in
  core|parent|tech) ;;
  *) echo "invalid set: $SET (the old 'swift' set is now 'tech')" >&2; exit 1 ;;
esac
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid ref: $REF" >&2; exit 1; }
if [[ ! -d "$DEST/.github/workflows" ]]; then
  mkdir -p "$DEST/.github/workflows"
fi
STUB_PATH=".github/caller-stubs"
# The core layer: task-domain callers every project needs.
STUBS=(
  add-review-label.yml
  auto-merge.yml
  bootstrap-runtime-secret.yml
  coderabbit-retry.yml
  coderabbit-unresolved.yml
  issue-scheduler.yml
  opencode-repair.yml
  opencode-unresolved.yml
  opencode.yml
  continuum-opencode-watchdog.yml
  pr-agent.yml
  remove-review-label.yml
)
if [[ "$SET" == tech ]]; then
  # The opt-in technology library (the `continuum-tech-` prefix marks it as
  # library/opt-in); Continuum itself never triggers these.
  STUB_PATH="$STUB_PATH/tech"
  STUBS=(
    continuum-tech-swift-ci.yml
    continuum-tech-swift-packaging-smoke.yml
    continuum-tech-swift-release-pr.yml
    continuum-tech-swift-release.yml
    continuum-tech-swift-release-automation-merge.yml
  )
elif [[ "$SET" == parent ]]; then
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
# Tech stubs are already named `continuum-tech-<tech>-<name>.yml` (the
# `continuum-tech-` prefix marks an opt-in library); they keep their name verbatim.
for f in "${STUBS[@]}"; do
  if [[ $# -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
    template="$(cat "$LOCAL_STUBS_DIR/$f")"
  else
    template="$(curl -fsSL "$BASE/$f")"
  fi
  name="$f"
  [[ "$name" == continuum-* ]] || name="continuum-$name"
  # Use the same revision for the workflow and its fallback scripts.
  printf '%s\n' "$template" | sed \
    -e "s|kodmial/continuum/\\(.github/workflows/[^@ ]*\\)@main|kodmial/continuum/\\1@$REF|g" \
    -e "s|continuum_ref: main|continuum_ref: '$REF'|" \
    -e "s|engine_ref: main|engine_ref: '$REF'|" \
    > "$DEST/.github/workflows/$name"
done
echo "Continuum callers installed to $DEST/.github/workflows/ (set: $SET, ref: $REF)"
