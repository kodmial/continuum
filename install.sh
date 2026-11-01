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
# An optional `--dry-run` prints the exact proposed add/update/keep/
# ownership-conflict/prune plan and changes nothing. A remote-ref preview needs
# network access but never credentials.
# Installing into Continuum's own checkout is refused outright.
# ref defaults to main. `core` is the task-domain layer every project needs;
# `tech` is the opt-in consumer-neutral technology library (`continuum-tech-<tech>-`);
# `parent` adds only child-execution callers.
# Continuum never triggers its own technology library: consumers opt into it.
#
# The install is transactional for controlled errors: every template is fetched
# and validated before the target is touched, uploads go through a temporary
# file plus rename, and any overwritten or pruned file is backed up. If a
# controlled apply/prune step then fails, the pre-install bytes are restored.
# Atomic replacement of the whole multi-file directory across a process kill or
# host crash is not promised; re-running the installer converges safely.
DEST="${1:-.}"
REF="${2:-main}"
SET="${3:-core}"
# `--yes` and `--dry-run` are accepted in any position and removed before
# positional parsing, so the documented argument shape stays
# `[path] [ref] [set]` and an existing invocation is unaffected. `--yes` is the
# only way to skip the prune confirmation.
# POSITIONAL counts the non-flag arguments, because the local-vs-remote
# template choice below must ignore the flags.
POSITIONAL=0
ASSUME_YES=no
DRY_RUN=no
ARGV=()
for arg in "$@"; do
  case "$arg" in
    --yes) ASSUME_YES=yes ;;
    --dry-run) DRY_RUN=yes ;;
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
# Reject an unusable target shape before creating anything. A preview must not
# even make the directory, so the write-side mkdirs happen only after preflight.
if [[ -e "$DEST" && ! -d "$DEST" ]]; then
  echo "invalid target: $DEST is not a directory" >&2
  exit 1
fi
if [[ -e "$DEST/.github" && ! -d "$DEST/.github" ]]; then
  echo "invalid target: $DEST/.github is not a directory" >&2
  exit 1
fi
if [[ -e "$DEST/.github/workflows" && ! -d "$DEST/.github/workflows" ]]; then
  echo "invalid target: $DEST/.github/workflows is not a directory" >&2
  exit 1
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
  # CodeRabbit owns UNRESOLVED batching; only the canonical OpenCode
  # executor owns model routing, runtime, commit/push and verification.
  continuum-coderabbit-unresolved.yml
  continuum-docker-qualification.yml
  continuum-issue-scheduler.yml
  continuum-opencode-repair.yml
  continuum-opencode-watchdog.yml
  continuum-opencode.yml
  continuum-pr-agent-canary.yml
  continuum-pr-agent-recovery.yml
  continuum-pr-agent.yml
  continuum-pr-agent-repair.yml
  continuum-pr-agent-router.yml
  continuum-pr-agent-auto-merge.yml
  continuum-remove-review-label.yml
  continuum-render-executor.yml
)
TECH_STUBS=(
  continuum-tech-swift-ci.yml
)
PARENT_STUBS=(
  continuum-child-pr-review.yml
  continuum-child-review.yml
  continuum-child-run-cleanup.yml
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
DEST_ABS=""
if [[ -d "$DEST" ]]; then
  DEST_ABS="$(cd "$DEST" && pwd)"
fi
TARGET_DIR=""
if [[ -n "$DEST_ABS" ]]; then
  TARGET_DIR="$DEST_ABS/.github/workflows"
fi
# Installing into Continuum's own checkout is never a consumer install, and it
# is not a harmless one. This repository's `.github/workflows/` holds the real
# core workflows; the files this script writes are thin caller stubs that
# `uses:` them. A `install.sh . --yes` here therefore replaces each core
# workflow with a caller to itself — measured at 14 files changed, 461
# insertions, 6413 deletions, i.e. the loss of every core workflow body in the
# repository.
#
# This refuses outright, before a single byte is written, and says why.
# Refusing loudly is the only safe answer: silently installing nothing would
# look like a success and leave the operator no signal that their repository
# was skipped.
if [[ -n "$SELF_DIR" && -n "$DEST_ABS" &&
      ( "$DEST_ABS" == "$SELF_DIR" || "$TARGET_DIR" == "$SELF_DIR/.github/workflows" ) ]]; then
  cat >&2 <<EOF
refusing to install: the target is the directory containing the installer
(Continuum's own checkout): $TARGET_DIR.

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
# A Continuum-owned caller is one this project wrote. The marker is the
# repository reference on a real `uses:` line, not the file name: every caller
# this installer writes carries that call, while a hand-written file that
# merely borrows the `continuum-` prefix or cites the path in a comment does
# not. The pinned revision is deliberately irrelevant, so a caller installed at
# an older ref is still recognised and remains upgradable.
is_continuum_artifact() {
  local file="$1" base="${1##*/}"
  [[ "$base" == continuum-* ]] || return 1
  grep -qE '^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*["'"'"']?kodmial/continuum/\.github/workflows/' \
    "$file" 2>/dev/null || return 1
  return 0
}
# Stage templates and backups outside the live target files. The directory is
# private to this run and is removed on exit, including after a failed apply.
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/continuum-install.XXXXXX")" ||
  { echo "unable to create a staging directory" >&2; exit 1; }
cleanup_stage() {
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap cleanup_stage EXIT
mkdir -p "$STAGE_DIR/raw" "$STAGE_DIR/prepared" "$STAGE_DIR/backup"
# Preflight: fetch and prepare every requested template before the target is
# touched. A failed download, an empty/corrupt body, or a template that does not
# look like a Continuum caller stops the install with no partial write.
validate_template() {
  local file="$1" name="$2"
  if ! grep -qE '[^[:space:]]' "$file"; then
    echo "invalid template: $name is empty" >&2
    return 1
  fi
  if ! grep -qE '^[[:space:]]*jobs:[[:space:]]*(#.*)?$' "$file"; then
    echo "invalid template: $name has no jobs block" >&2
    return 1
  fi
  if ! grep -qE '^[[:space:]]*-?[[:space:]]*uses:[[:space:]]*["'"'"']?kodmial/continuum/\.github/workflows/' "$file"; then
    echo "invalid template: $name does not call a Continuum workflow" >&2
    return 1
  fi
  return 0
}
REMOTE_USED=no
for f in "${STUBS[@]}"; do
  raw="$STAGE_DIR/raw/$f"
  prepared="$STAGE_DIR/prepared/$f"
  # Canonical main installs are byte copies. Do not pass them through command
  # substitution: POSIX shells strip trailing newlines from $(...), which would
  # manufacture a diff even when the source and destination are the same
  # canonical caller.
  if [[ "$REF" == "main" ]]; then
    if [[ $POSITIONAL -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
      if ! cat "$LOCAL_STUBS_DIR/$f" > "$raw"; then
        echo "failed to read local template: $f" >&2
        exit 1
      fi
    else
      REMOTE_USED=yes
      if ! curl -fsSL "$BASE/$f" > "$raw"; then
        echo "failed to fetch template: $f ($BASE/$f)" >&2
        exit 1
      fi
    fi
    if ! cp "$raw" "$prepared"; then
      echo "failed to prepare template: $f" >&2
      exit 1
    fi
  else
    if [[ $POSITIONAL -lt 2 && -f "$LOCAL_STUBS_DIR/$f" ]]; then
      if ! cat "$LOCAL_STUBS_DIR/$f" > "$raw"; then
        echo "failed to read local template: $f" >&2
        exit 1
      fi
    else
      REMOTE_USED=yes
      if ! curl -fsSL "$BASE/$f" > "$raw"; then
        echo "failed to fetch template: $f ($BASE/$f)" >&2
        exit 1
      fi
    fi
    template="$(cat "$raw")"
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
    # Non-main refs are test/development overrides. The ref is written as a
    # double-quoted YAML scalar rather than a bare or single-quoted one.
    if ! printf '%s\n' "$template" | sed -E \
      -e "s|^([[:space:]]*-?[[:space:]]*uses:[[:space:]]*[\"']?kodmial/continuum/\.github/workflows/[^@[:space:]\"']*)@main([\"']?)([[:space:]]+#.*)?([[:space:]]*)$|\1@$REF\2\3\4|" \
      -e "s|^([[:space:]]*-?[[:space:]]*(continuum_ref\|engine_ref):[[:space:]]*)main([[:space:]]+#.*)?([[:space:]]*)$|\1\"$REF\"\3\4|" \
      > "$prepared"; then
      echo "failed to rewrite template: $f" >&2
      exit 1
    fi
  fi
  validate_template "$prepared" "$f" || exit 1
done
if [[ "$DRY_RUN" == yes && "$REMOTE_USED" == yes ]]; then
  echo "preview: templates fetched from $BASE (network access required; no credentials are used)"
fi
# Classify each target before any mutation. A target this installer did not
# write is an ownership conflict, never an implicit overwrite: fail closed and
# tell the operator how to resolve it. `cmp` prevents a same-version reinstall
# from touching bytes at all.
TARGET_WORKFLOWS="$DEST/.github/workflows"
ADD_FILES=()
UPDATE_FILES=()
KEEP_FILES=()
CONFLICT_FILES=()
CONFLICT_DETAILS=()
PLAN_ACTIONS=()
for f in "${STUBS[@]}"; do
  target="$TARGET_WORKFLOWS/$f"
  if [[ -L "$target" ]]; then
    PLAN_ACTIONS+=("ownership-conflict")
    CONFLICT_FILES+=("$f")
    CONFLICT_DETAILS+=("$f is a symlink, not a regular installer-owned file")
  elif [[ -e "$target" ]]; then
    if [[ ! -f "$target" ]]; then
      PLAN_ACTIONS+=("ownership-conflict")
      CONFLICT_FILES+=("$f")
      CONFLICT_DETAILS+=("$f exists but is not a regular file")
    elif cmp -s "$STAGE_DIR/prepared/$f" "$target"; then
      PLAN_ACTIONS+=("keep")
      KEEP_FILES+=("$f")
    elif is_continuum_artifact "$target"; then
      PLAN_ACTIONS+=("update")
      UPDATE_FILES+=("$f")
    else
      PLAN_ACTIONS+=("ownership-conflict")
      CONFLICT_FILES+=("$f")
      CONFLICT_DETAILS+=("$f is not recognised as a Continuum-owned caller (no Continuum uses: line)")
    fi
  else
    PLAN_ACTIONS+=("add")
    ADD_FILES+=("$f")
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
#    repository's workflows on a `uses:` line. Neither the prefix nor a
#    comment mention alone is ownership evidence.
# 2. Intent. The exact list is printed and must be confirmed at a terminal.
#    Non-interactive runs delete nothing and say so. `--yes` or
#    CONTINUUM_INSTALL_ASSUME_YES=1 is the explicit opt-out for an unattended
#    install that does want the prune. A preview only lists the candidates.
# 3. Self-install. Installing into Continuum's own checkout is refused before
#    this point, so its own workflows are never candidates.
#
# The other layers' callers are never candidates: ALL_STUBS spans all three
# layers, so installing one set cannot delete another set's files.
ALL_STUBS=("${CORE_STUBS[@]}" "${TECH_STUBS[@]}" "${PARENT_STUBS[@]}")
SUPERSEDED=()
if [[ -d "$TARGET_WORKFLOWS" ]]; then
  for installed in "$TARGET_WORKFLOWS"/continuum-*.yml; do
    [[ -f "$installed" ]] || continue
    stale="${installed##*/}"
    known=no
    for candidate in "${ALL_STUBS[@]}"; do
      if [[ "$candidate" == "$stale" ]]; then known=yes; break; fi
    done
    [[ "$known" == yes ]] && continue
    is_continuum_artifact "$installed" || continue
    SUPERSEDED+=("$stale")
  done
fi
# Print the exact plan. It is the whole user-visible contract of `--dry-run`,
# and it is equally truthful during a real install.
for i in "${!STUBS[@]}"; do
  echo "${PLAN_ACTIONS[$i]}: ${STUBS[$i]}"
done
for stale in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do
  echo "prune: $stale"
done
if [[ ${#CONFLICT_FILES[@]} -gt 0 ]]; then
  for i in "${!CONFLICT_FILES[@]}"; do
    echo "ownership-conflict: ${CONFLICT_FILES[$i]}: ${CONFLICT_DETAILS[$i]}" >&2
  done
  cat >&2 <<EOF
refusing to install: ${#CONFLICT_FILES[@]} target file(s) are not Continuum-owned.

The installer never overwrites a file it cannot prove it wrote. Move or rename
the conflicting file(s) above, then re-run the installer. There is no implicit
force flag: a consumer-owned workflow stays untouched.
EOF
  exit 1
fi
# Prune intent is decided before the transactional apply so a declined prompt
# cannot leave a half-finished run.
PRUNE=no
if [[ ${#SUPERSEDED[@]} -gt 0 ]]; then
  if [[ "$DRY_RUN" == yes ]]; then
    if [[ "$ASSUME_YES" != yes ]]; then
      echo "preview: these prune entries need --yes or terminal confirmation when applying"
    fi
  elif [[ "$ASSUME_YES" == yes ]]; then
    PRUNE=yes
  elif [[ -t 0 && -t 1 ]]; then
    echo "Continuum no longer ships these callers; they are superseded and can be removed:"
    printf '  %s\n' "${SUPERSEDED[@]}"
    read -r -p "Remove these ${#SUPERSEDED[@]} file(s)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
      PRUNE=yes
    else
      echo "left in place (re-run with --yes to prune non-interactively)"
    fi
  else
    echo "non-interactive install: not removing superseded Continuum callers."
    printf '  would remove: %s\n' "${SUPERSEDED[@]}"
    echo "re-run with --yes (or CONTINUUM_INSTALL_ASSUME_YES=1) to prune"
  fi
fi
if [[ "$DRY_RUN" == yes ]]; then
  echo "dry-run: no files were changed"
  exit 0
fi
# Transactional apply. Every overwritten/pruned file is backed up first; each
# write lands in a temporary file next to the target and is then renamed, so a
# controlled failure cannot leave a half-written caller. If any step fails, the
# original bytes (including pruned files) are restored.
CREATED_DIRS=()
ADDED_FILES=()
TEMP_FILES=()
RESTORE_FILES=(${UPDATE_FILES[@]+"${UPDATE_FILES[@]}"})
if [[ "$PRUNE" == yes ]]; then
  RESTORE_FILES+=(${SUPERSEDED[@]+"${SUPERSEDED[@]}"})
fi
ensure_dir() {
  local dir="$1"
  [[ -d "$dir" ]] && return 0
  mkdir -p "$dir" || return 1
  CREATED_DIRS+=("$dir")
  return 0
}
rollback() {
  local f i rollback_failed=no
  for f in ${ADDED_FILES[@]+"${ADDED_FILES[@]}"}; do
    rm -f "$TARGET_WORKFLOWS/$f" 2>/dev/null || rollback_failed=yes
  done
  for f in ${TEMP_FILES[@]+"${TEMP_FILES[@]}"}; do
    rm -f "$TARGET_WORKFLOWS/$f" 2>/dev/null || rollback_failed=yes
  done
  for f in ${RESTORE_FILES[@]+"${RESTORE_FILES[@]}"}; do
    if [[ -f "$STAGE_DIR/backup/$f" ]]; then
      cp -p "$STAGE_DIR/backup/$f" "$TARGET_WORKFLOWS/$f" 2>/dev/null || rollback_failed=yes
    fi
  done
  for ((i = ${#CREATED_DIRS[@]} - 1; i >= 0; i--)); do
    rmdir "${CREATED_DIRS[$i]}" 2>/dev/null || true
  done
  [[ "$rollback_failed" == no ]] || echo "warning: rollback could not fully restore the target" >&2
}
apply_ok=yes
if ! ensure_dir "$DEST" || ! ensure_dir "$DEST/.github" || ! ensure_dir "$TARGET_WORKFLOWS"; then
  apply_ok=no
fi
if [[ "$apply_ok" == yes ]]; then
  for f in ${RESTORE_FILES[@]+"${RESTORE_FILES[@]}"}; do
    if ! cp -p "$TARGET_WORKFLOWS/$f" "$STAGE_DIR/backup/$f"; then
      echo "failed to back up: $f" >&2
      apply_ok=no
      break
    fi
  done
fi
write_target() {
  local f="$1" tmp
  tmp="$TARGET_WORKFLOWS/.continuum-install-${f}.$$"
  TEMP_FILES+=("$(basename "$tmp")")
  cp "$STAGE_DIR/prepared/$f" "$tmp" || return 1
  mv -f "$tmp" "$TARGET_WORKFLOWS/$f" || return 1
  return 0
}
if [[ "$apply_ok" == yes ]]; then
  for f in ${ADD_FILES[@]+"${ADD_FILES[@]}"}; do
    ADDED_FILES+=("$f")
    if ! write_target "$f"; then
      echo "failed to install: $f" >&2
      apply_ok=no
      break
    fi
  done
fi
if [[ "$apply_ok" == yes ]]; then
  for f in ${UPDATE_FILES[@]+"${UPDATE_FILES[@]}"}; do
    if ! write_target "$f"; then
      echo "failed to update: $f" >&2
      apply_ok=no
      break
    fi
  done
fi
if [[ "$apply_ok" == yes && "$PRUNE" == yes ]]; then
  for stale in "${SUPERSEDED[@]}"; do
    if ! rm -f "$TARGET_WORKFLOWS/$stale"; then
      echo "failed to prune: $stale" >&2
      apply_ok=no
      break
    fi
    echo "removed superseded Continuum caller: $stale"
  done
fi
if [[ "$apply_ok" != yes ]]; then
  rollback
  echo "installation failed; restored the pre-install target files" >&2
  exit 1
fi
echo "Continuum callers installed to $DEST/.github/workflows/ (set: $SET, ref: $REF)"
