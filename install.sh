#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash -s -- [path] [ref] [core|parent|tech]
#    or: bash continuum/install.sh [path-to-consumer-repo] [ref] [core|parent|tech]
# An optional `--yes` (or CONTINUUM_INSTALL_ASSUME_YES=1) removes superseded
# Continuum callers without the interactive confirmation.
# ref defaults to main. `core` is the task-domain layer every project needs;
# `tech` is the opt-in technology library (the `continuum-tech-<tech>-` prefix marks the library);
# `parent` adds only child-execution callers.
# Continuum never triggers its own technology library: consumers opt into it.
DEST="${1:-.}"
REF="${2:-main}"
SET="${3:-core}"
# `--yes` is accepted in any position and removed before positional parsing, so
# the documented argument shape stays `[path] [ref] [set]` and an existing
# invocation is unaffected. It is the only way to skip the prune confirmation.
# POSITIONAL counts the non-flag arguments, because the local-vs-remote
# template choice below must ignore the flag.
POSITIONAL=0
ASSUME_YES=no
ARGV=()
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=yes ;;
    *) ARGV+=("$arg"); POSITIONAL=$((POSITIONAL + 1)) ;;
  esac
done
[[ "${CONTINUUM_INSTALL_ASSUME_YES:-}" =~ ^(1|true|yes)$ ]] && ASSUME_YES=yes
set -- ${ARGV[@]+"${ARGV[@]}"}
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
  if [[ $POSITIONAL -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
    template="$(cat "$LOCAL_STUBS_DIR/$f")"
  else
    template="$(curl -fsSL "$BASE/$f")"
  fi
  # Use the same revision for the workflow and its fallback scripts.
  #
  # A `uses:` line is legal at two levels: job-level (`  call:` then
  # `    uses: ...`) and step-level (`      - uses: ...`). The optional `-`
  # is what makes the step form match; without it the ref silently stays
  # `main` on any caller that composes another workflow as a step, which is
  # exactly the guarantee the ref parameter exists to provide.
  #
  # Each pattern is anchored so an unrelated `main` elsewhere in the template
  # is never rewritten, and the anchor tolerates a trailing YAML comment:
  # `@main # pinned` pins the requested ref and keeps its comment, instead of
  # being skipped. The ref is written as a double-quoted YAML scalar rather
  # than a bare or single-quoted one.
  printf '%s\n' "$template" | sed -E \
    -e "s|^([[:space:]]*-?[[:space:]]*uses:[[:space:]]*kodmial/continuum/\.github/workflows/[^@[:space:]]*)@main([[:space:]]+#.*)?$|\1@$REF\2|" \
    -e "s|^([[:space:]]*-?[[:space:]]*(continuum_ref\|engine_ref):[[:space:]]*)main([[:space:]]+#.*)?$|\1\"$REF\"\3|" \
    > "$DEST/.github/workflows/$f"
done
# Supersession: a renamed or dropped stub must not leave a stale caller behind
# in the consumer, or the old file keeps running next to its replacement.
#
# Deletion is the only destructive thing an installer does, so it is fenced
# three ways.
#
# 1. Ownership. A file is a Continuum artifact only if its name carries the
#    uniform `continuum-` prefix *and* its body references this repository
#    (`kodmial/continuum/.github/workflows/`). Every caller this installer
#    writes carries that reference, so the test never rejects a real artifact;
#    a hand-written `continuum-experiment.yml` that merely borrows the prefix
#    fails it and is left alone. The prefix alone is not ownership evidence.
# 2. Intent. The exact list is printed and must be confirmed at a terminal.
#    Non-interactive runs delete nothing and say so, so a CI install can never
#    silently drop a file. `--yes` or CONTINUUM_INSTALL_ASSUME_YES=1 is the
#    explicit opt-out for an unattended install that does want the prune.
# 3. Self-install. Installing into Continuum's own checkout is never a consumer
#    install, so pruning is skipped entirely and the repository's own
#    workflows stay untouched.
#
# The other two layers' callers are never candidates: ALL_STUBS spans all three
# layers, so installing one set cannot delete another set's files.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"
TARGET_DIR="$(cd "$DEST/.github/workflows" && pwd)"
ALL_STUBS=("${CORE_STUBS[@]}" "${TECH_STUBS[@]}" "${PARENT_STUBS[@]}")
# A Continuum-owned caller is one this project wrote. The marker is the
# repository reference inside it, not the file name.
is_continuum_artifact() {
  local file="$1" base="${1##*/}" candidate
  [[ "$base" == continuum-* ]] || return 1
  grep -q 'kodmial/continuum/\.github/workflows/' "$file" 2>/dev/null || return 1
  return 0
}
if [[ "$TARGET_DIR" == "$SELF_DIR/.github/workflows" ]]; then
  echo "target is Continuum's own checkout: skipping superseded-caller prune"
else
  superseded=()
  for installed in "$DEST"/.github/workflows/continuum-*.yml; do
    [[ -f "$installed" ]] || continue
    stale="${installed##*/}"
    known=no
    for candidate in "${ALL_STUBS[@]}"; do
      if [[ "$candidate" == "$stale" ]]; then known=yes; break; fi
    done
    [[ "$known" == yes ]] && continue
    is_continuum_artifact "$installed" || continue
    superseded+=("$stale")
  done
  if [[ ${#superseded[@]} -eq 0 ]]; then
    :
  elif [[ "$ASSUME_YES" == yes ]]; then
    for stale in "${superseded[@]}"; do
      rm -f "$DEST/.github/workflows/$stale"
      echo "removed superseded Continuum caller: $stale"
    done
  elif [[ -t 0 && -t 1 ]]; then
    echo "Continuum no longer ships these callers; they are superseded and can be removed:"
    printf '  %s\n' "${superseded[@]}"
    read -r -p "Remove these ${#superseded[@]} file(s)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
      for stale in "${superseded[@]}"; do
        rm -f "$DEST/.github/workflows/$stale"
        echo "removed superseded Continuum caller: $stale"
      done
    else
      echo "left in place (re-run with --yes to prune non-interactively)"
    fi
  else
    echo "non-interactive install: not removing superseded Continuum callers."
    printf '  would remove: %s\n' "${superseded[@]}"
    echo "re-run with --yes (or CONTINUUM_INSTALL_ASSUME_YES=1) to prune"
  fi
fi
echo "Continuum callers installed to $DEST/.github/workflows/ (set: $SET, ref: $REF)"
