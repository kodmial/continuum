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
# The three layers, each stored under the exact name it installs as. There is
# one naming rule in Continuum and no exception list: a caller is stored as
# `continuum-<name>.yml` and installed verbatim, so the repository file name and
# the consumer file name are always the same string.
CORE_STUBS=(
  continuum-add-review-label.yml
  continuum-auto-merge.yml
  continuum-bootstrap-runtime-secret.yml
  continuum-coderabbit-retry.yml
  continuum-coderabbit-unresolved.yml
  continuum-docker-qualification.yml
  continuum-issue-scheduler.yml
  continuum-opencode-repair.yml
  continuum-opencode-unresolved.yml
  continuum-opencode-watchdog.yml
  continuum-opencode.yml
  continuum-pr-agent.yml
  continuum-remove-review-label.yml
  continuum-render-executor.yml
)
TECH_STUBS=(
  continuum-tech-swift-ci.yml
  continuum-tech-swift-packaging-smoke.yml
  continuum-tech-swift-release-automation-merge.yml
  continuum-tech-swift-release-pr.yml
  continuum-tech-swift-release.yml
)
PARENT_STUBS=(
  continuum-child-dispatcher.yml
  continuum-child-pr-review.yml
  continuum-child-review.yml
  continuum-child-worker.yml
)
case "$SET" in
  core)
    STUB_PATH=".github/caller-stubs"
    STUBS=("${CORE_STUBS[@]}")
    ;;
  tech)
    # The opt-in technology library (the `continuum-tech-` prefix marks it as
    # library/opt-in); Continuum itself never triggers these.
    STUB_PATH=".github/caller-stubs/tech"
    STUBS=("${TECH_STUBS[@]}")
    ;;
  parent)
    STUB_PATH=".github/caller-stubs/parent"
    STUBS=("${PARENT_STUBS[@]}")
    ;;
esac
BASE="https://raw.githubusercontent.com/kodmial/continuum/${REF}/$STUB_PATH"
# Only use local templates when installing the default working-tree version.
# An explicit ref must fetch that revision, even when run from a local clone.
LOCAL_STUBS_DIR="$(dirname "${BASH_SOURCE[0]:-$0}")/$STUB_PATH"
# The stored stub name is the installed name, verbatim: the loop variable is
# written straight to the destination, so a repository file name and the
# consumer file name are always the same string and cannot drift apart.
for f in "${STUBS[@]}"; do
  if [[ $# -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
    template="$(cat "$LOCAL_STUBS_DIR/$f")"
  else
    template="$(curl -fsSL "$BASE/$f")"
  fi
  # Use the same revision for the workflow and its fallback scripts. Both
  # patterns are anchored to the end of a line so an unrelated `main`
  # elsewhere in the template is never rewritten, and the ref is written as a
  # double-quoted YAML scalar rather than a bare or single-quoted one.
  printf '%s\n' "$template" | sed -E \
    -e "s|^([[:space:]]*uses:[[:space:]]*kodmial/continuum/\.github/workflows/[^@[:space:]]*)@main[[:space:]]*$|\1@$REF|" \
    -e "s|^([[:space:]]*(continuum_ref\|engine_ref):[[:space:]]*)main[[:space:]]*$|\1\"$REF\"|" \
    > "$DEST/.github/workflows/$f"
done
# Supersession: a renamed or dropped stub must not leave a stale caller behind
# in the consumer, or the old file keeps running next to its replacement. Only
# names this installer owns are ever removed: every `continuum-*.yml` that no
# layer ships any more is deleted, while the other two layers' callers and
# every project-owned workflow are left alone. Installing into Continuum's own
# checkout is never a consumer install, so pruning is skipped there and the
# repository's own workflows stay untouched.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"
TARGET_DIR="$(cd "$DEST/.github/workflows" && pwd)"
ALL_STUBS=("${CORE_STUBS[@]}" "${TECH_STUBS[@]}" "${PARENT_STUBS[@]}")
if [[ "$TARGET_DIR" != "$SELF_DIR/.github/workflows" ]]; then
  for installed in "$DEST"/.github/workflows/continuum-*.yml; do
    [[ -f "$installed" ]] || continue
    stale="${installed##*/}"
    known=no
    for candidate in "${ALL_STUBS[@]}"; do
      if [[ "$candidate" == "$stale" ]]; then known=yes; break; fi
    done
    [[ "$known" == yes ]] && continue
    rm -f "$installed"
    echo "removed superseded Continuum caller: $stale"
  done
fi
echo "Continuum callers installed to $DEST/.github/workflows/ (set: $SET, ref: $REF)"
