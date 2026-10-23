#!/usr/bin/env bash
# Continuum publish/checkpoint staging boundary (kodmial/continuum#413).
#
# Workflow-owned checkout directories and recovery metadata must never leak
# into an implementation PR. This library is invoked only by the reusable
# workflows, never by an agent, and is deliberately independent of agent
# instructions and `.gitignore`.
#
# Two staging modes exist:
#   * checkpoint staging keeps the verified 429 recovery metadata and progress
#     file, because those are authoritative on the checkpoint ref;
#   * publish staging removes every workflow-owned artifact before the task or
#     repair commit is created.
#
# Both modes stage the real working tree with `git add -A` first, then remove
# only the exact workflow-owned paths below. A Gitlink left in the index after
# that boundary is accepted only when a matching `.gitmodules` path+URL proves
# it is a legitimate declared submodule; any other Gitlink fails closed.

CONTINUUM_PUBLISH_EXCLUDED_PATHS=(
  ".continuum-cancel-engine"
  ".continuum-429-engine"
  ".continuum-admission-engine"
  ".continuum-429-recovery.json"
  ".continuum-429-stage"
)

CONTINUUM_CHECKPOINT_EXCLUDED_PATHS=(
  ".continuum-cancel-engine"
  ".continuum-429-engine"
  ".continuum-admission-engine"
)

continuum_staging_error() {
  echo "::error::Continuum staging: $*" >&2
}

# Remove exactly one workflow-owned path from the index and the worktree.
# `git rm` handles normal files, directories, and Gitlinks; the update-index
# fallback covers a Gitlink whose worktree checkout is already absent. The
# trailing rm removes an untracked/ignored checkout that `git add -A` never
# staged.
continuum_remove_workflow_path() {
  local path="$1"
  git rm -r -f --ignore-unmatch -- "$path" >/dev/null 2>&1 || true
  git update-index --force-remove -- "$path" >/dev/null 2>&1 || true
  rm -rf -- "$path"
}

# Print every submodule path from `.gitmodules` that also has a non-empty URL.
# A path without a URL is not a valid declared submodule.
continuum_configured_submodule_paths() {
  local modules="${1:-.gitmodules}"
  [[ -f "$modules" ]] || return 0
  local line key path name url
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    key="${line%% *}"
    path="${line#* }"
    name="${key#submodule.}"
    name="${name%.path}"
    url="$(git config -f "$modules" --get "submodule.${name}.url" 2>/dev/null || true)"
    [[ -n "$path" && -n "$url" ]] && printf '%s\n' "$path"
  done < <(git config -f "$modules" --get-regexp '^submodule\..*\.path$' 2>/dev/null || true)
}

continuum_path_is_declared_submodule() {
  local candidate="$1" declared
  while IFS= read -r declared; do
    [[ "$declared" == "$candidate" ]] && return 0
  done < <(continuum_configured_submodule_paths)
  return 1
}

# Reject unexpected unconfigured Gitlinks (mode 160000) in the index. This is
# the fail-closed boundary immediately before any checkpoint or PR push.
continuum_assert_configured_gitlinks() {
  local entry mode path failures=0
  while IFS= read -r -d '' entry; do
    mode="${entry%% *}"
    [[ "$mode" == "160000" ]] || continue
    path="${entry#*$'\t'}"
    if continuum_path_is_declared_submodule "$path"; then
      continue
    fi
    continuum_staging_error "unexpected unconfigured Gitlink '$path' (mode 160000) has no matching .gitmodules path with a URL; refusing to publish."
    failures=$((failures + 1))
  done < <(git ls-files --stage -z)
  [[ "$failures" -eq 0 ]]
}

continuum_stage_checkpoint() {
  git add -A
  local path
  for path in "${CONTINUUM_CHECKPOINT_EXCLUDED_PATHS[@]}"; do
    continuum_remove_workflow_path "$path"
  done
  continuum_assert_configured_gitlinks
}

continuum_stage_publishable() {
  git add -A
  local path
  for path in "${CONTINUUM_PUBLISH_EXCLUDED_PATHS[@]}"; do
    continuum_remove_workflow_path "$path"
  done
  continuum_assert_configured_gitlinks
}
