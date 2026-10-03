#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${CONTINUUM_ENGINE_ROOT:?CONTINUUM_ENGINE_ROOT is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

child_id="${1:-}"
target_repository="$GITHUB_REPOSITORY"
delegated=false

if [[ -n "$child_id" ]]; then
  resolver="$CONTINUUM_ENGINE_ROOT/.github/scripts/delegation_repository.sh"
  [[ -f "$resolver" ]] || {
    echo "::error::PR-Agent delegated target resolver is unavailable." >&2
    exit 2
  }

  resolve_rc=0
  target_repository="$(
    PARENT_CONFIG="${PARENT_CONFIG:-.continuum.yml}" \
    CHILD_REPOSITORIES="${CHILD_REPOSITORIES:-}" \
      bash "$resolver" resolve "$child_id"
  )" || resolve_rc=$?
  if [[ "$resolve_rc" -ne 0 || -z "$target_repository" ]]; then
    echo "::error::PR-Agent delegated target resolution failed closed." >&2
    exit "${resolve_rc:-2}"
  fi

  if ! PARENT_CONFIG="${PARENT_CONFIG:-.continuum.yml}" \
       CHILD_REPOSITORIES="${CHILD_REPOSITORIES:-}" \
       bash "$resolver" verify "$child_id" "$target_repository" >/dev/null; then
    echo "::error::PR-Agent delegated target verification failed closed." >&2
    exit 2
  fi
  delegated=true
fi

if ! [[ "$target_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "::error::PR-Agent target repository identity is invalid." >&2
  exit 2
fi

target_owner="${target_repository%%/*}"
target_repo="${target_repository#*/}"

if [[ "$delegated" == true && "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "::add-mask::$target_repository"
  echo "::add-mask::$target_repo"
fi

{
  printf 'CONTINUUM_PR_AGENT_TARGET_REPOSITORY=%s\n' "$target_repository"
  printf 'CONTINUUM_PR_AGENT_TARGET_OWNER=%s\n' "$target_owner"
  printf 'CONTINUUM_PR_AGENT_TARGET_REPO=%s\n' "$target_repo"
  printf 'CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED=%s\n' "$delegated"
} >> "$GITHUB_ENV"

if [[ "$delegated" == true ]]; then
  echo "PR-Agent target context resolved through verified delegated relationship."
else
  echo "PR-Agent target context resolved locally."
fi
