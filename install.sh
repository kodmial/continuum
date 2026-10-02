#!/usr/bin/env bash
set -euo pipefail
# Install Continuum reusable workflows into a consumer repo (one command).
# Usage: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash
#    or: curl -fsSL https://raw.githubusercontent.com/kodmial/continuum/main/install.sh | bash -s -- [path] [ref] [core|parent|tech]
#    or: bash continuum/install.sh [path-to-consumer-repo] [ref] [core|parent|tech]
# An optional `--yes` (or CONTINUUM_INSTALL_ASSUME_YES=1) removes superseded
# Continuum callers without the interactive confirmation. The variable is
# honoured only for an exact `1`, `true` or `yes`; any other value, including
# empty, `0` and `false`, leaves the confirmation in place.
# Installing into Continuum's own checkout is refused outright.
# ref defaults to main. `core` is the task-domain layer every project needs;
# `tech` is the opt-in consumer-neutral technology library (`continuum-tech-<tech>-`);
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
)
PARENT_STUBS=(
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
# The script's own path. It is empty when the script is not run as a file, which
# is exactly the documented `curl … | bash -s` invocation: there `BASH_SOURCE` is
# unset and `$0` is the shell itself, so `dirname` of either collapses to `.`
# and names the *consumer's* directory. Falling back to `$0` therefore made a
# piped install look like a self-install, and the guard below refused it.
#
# A piped run has no checkout behind it, so it neither reads local templates nor
# has a checkout to protect: both fall back to the remote ref, which is the
# correct behaviour for the documented one-liner.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "$SCRIPT_PATH" ]]; then
  LOCAL_STUBS_DIR="$(dirname "$SCRIPT_PATH")/$STUB_PATH"
  # Only use local templates when installing the default working-tree version.
  # An explicit ref must fetch that revision, even when run from a local clone.
  SELF_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd || echo "$PWD")"
else
  LOCAL_STUBS_DIR=""
  SELF_DIR=""
fi
TARGET_DIR="$(cd "$DEST/.github/workflows" && pwd)"
# Installing into Continuum's own checkout is never a consumer install, and it
# is not a harmless one. This repository's `.github/workflows/` holds the real
# core workflows; the files this script writes are thin caller stubs that
# `uses:` them. A `install.sh . --yes` here therefore replaces each core
# workflow with a caller to itself — measured at 14 files changed, 461
# insertions, 6413 deletions, i.e. the loss of every core workflow body in the
# repository.
#
# Skipping the prune (below) is not enough, because the WRITE is the
# destructive half and it happens first. So this refuses outright, before a
# single byte is written, and says why. Refusing loudly is the only safe
# answer: silently installing nothing would look like a success and leave the
# operator no signal that their repository was skipped.
if [[ -n "$SELF_DIR" && "$TARGET_DIR" == "$SELF_DIR/.github/workflows" ]]; then
  cat >&2 <<EOF
refusing to install: the target is Continuum's own checkout ($TARGET_DIR).

This repository ships the core workflows themselves; the files this installer
writes are thin caller stubs that call them. Installing here would overwrite
every core workflow with a stub pointing back at Continuum, destroying the
workflow bodies this repository exists to provide.

Continuum's own workflows are already in place. To install callers, point the
installer at a consumer repository instead:

  bash install.sh /path/to/consumer-repo [ref] [core|parent|tech]
EOF
  exit 1
fi
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
  # is never rewritten, and the anchor tolerates the things a real line can
  # carry after the ref without treating any of them as part of it:
  #   - a trailing YAML comment (`@main # pinned`), re-emitted verbatim;
  #   - surrounding quotes, which are part of the YAML scalar and not of the
  #     ref, re-emitted verbatim;
  #   - trailing whitespace, including the CR of a CRLF checkout, re-emitted
  #     verbatim.
  # Each of those ends the match, so a line that merely *contains* `main` — a
  # third-party action, an `@branch-main` tag, a `run:` body — never matches.
  # `@main` with no space before a `#` is deliberately NOT rewritten: YAML
  # requires whitespace to open a comment, so `#` there is part of the ref.
  # `main` is the canonical stored form. Reinstalling the canonical ref must
  # therefore write the template byte-for-byte; otherwise an update to the
  # same revision creates representational drift in every consumer. Non-main
  # refs are test/development overrides and are rewritten deliberately.
  if [[ "$REF" == "main" ]]; then
    printf '%s\n' "$template" > "$DEST/.github/workflows/$f"
  else
    # A non-main ref is written as a double-quoted YAML scalar rather than a
    # bare or single-quoted one.
    printf '%s\n' "$template" | sed -E \
      -e "s|^([[:space:]]*-?[[:space:]]*uses:[[:space:]]*[\"']?kodmial/continuum/\.github/workflows/[^@[:space:]\"']*)@main([\"']?)([[:space:]]+#.*)?([[:space:]]*)$|\1@$REF\2\3\4|" \
      -e "s|^([[:space:]]*-?[[:space:]]*(continuum_ref\|engine_ref):[[:space:]]*)main([[:space:]]+#.*)?([[:space:]]*)$|\1\"$REF\"\3\4|" \
      > "$DEST/.github/workflows/$f"
  fi
done
# Supersession: a renamed or dropped stub must not leave a stale caller behind
# in the consumer, or the old file keeps running next to its replacement.
#
# Deletion is the only destructive thing an installer does, so it is fenced
# three ways.
#
# 1. Ownership. A file is a Continuum artifact only if its name carries the
#    uniform `continuum-` prefix *and* it really calls one of this
#    repository's workflows on a `uses:` line. Every caller this installer
#    writes carries that call, so the test never rejects a real artifact; a
#    hand-written `continuum-experiment.yml` that merely borrows the prefix —
#    or that only mentions the repository path in a comment — fails it and is
#    left alone. Neither the prefix nor the mention alone is ownership
#    evidence.
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
# The self-install case never reaches this point: it is refused before the
# write loop above, because writing caller stubs over this repository's own
# core workflows is the destructive half. The branch below remains as a
# second, independent fence on the DELETE half only.
ALL_STUBS=("${CORE_STUBS[@]}" "${TECH_STUBS[@]}" "${PARENT_STUBS[@]}")
# A Continuum-owned caller is one this project wrote. The marker is the
# repository reference inside it, not the file name.
is_continuum_artifact() {
  local file="$1" base="${1##*/}"
  [[ "$base" == continuum-* ]] || return 1
  # The marker must be a real `uses:` call, not merely the string somewhere in
  # the body. A bare `grep` for the repository path also matches a hand-written
  # file that only cites Continuum in a comment, and such a file is still
  # project-owned work the installer never wrote. Anchoring on a `uses:` line
  # is what makes this ownership evidence: every caller this installer writes
  # calls one of this repository's workflows, and nothing else does.
  grep -qE '^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*["'"'"']?kodmial/continuum/\.github/workflows/' \
    "$file" 2>/dev/null || return 1
  return 0
}
if [[ -n "$SELF_DIR" && "$TARGET_DIR" == "$SELF_DIR/.github/workflows" ]]; then
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
