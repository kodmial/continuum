#!/usr/bin/env bash
set -euo pipefail

: "${CONTINUUM_ENGINE_ROOT:?CONTINUUM_ENGINE_ROOT is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

relation="$CONTINUUM_ENGINE_ROOT/.github/scripts/delegation_runtime.py"
parent_config="${PARENT_CONFIG:-.continuum.yml}"
repository_map_json="${CHILD_REPOSITORIES:-}"

resolve_by_roles() {
  local child_id="$1"
  local candidate config_file matched=""
  local count=0

  python3 "$relation" parent-allows-child     --config "$parent_config"     --child-id "$child_id"

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" == "$GITHUB_REPOSITORY" ]] && continue

    config_file="$RUNNER_TEMP/continuum-child-discovery-${child_id}-${count}.yml"
    if ! gh api "repos/$candidate/contents/.continuum.yml" --jq '.content' 2>/dev/null       | tr -d '\n' | base64 -d >"$config_file" 2>/dev/null; then
      rm -f "$config_file"
      continue
    fi

    if python3 "$relation" verify-child       --config "$config_file"       --child-id "$child_id"       --parent-repository "$GITHUB_REPOSITORY"       >/dev/null 2>&1; then
      matched="$candidate"
      count=$((count + 1))
      if [[ "$count" -gt 1 ]]; then
        echo "::error::More than one repository declares child id '$child_id' for this parent." >&2
        return 2
      fi
    fi
    rm -f "$config_file"
  done < <(
    gh api --paginate '/user/repos?affiliation=owner&per_page=100'       --jq '.[].full_name' 2>/dev/null
  )

  if [[ "$count" -ne 1 ]]; then
    echo "::error::No unique repository declares child id '$child_id' for this parent." >&2
    return 2
  fi

  printf '%s\n' "$matched"
}

resolve_child() {
  local child_id="$1"
  if [[ -n "$repository_map_json" ]]; then
    python3 "$relation" resolve-child       --config "$parent_config"       --repository-map-json "$repository_map_json"       --child-id "$child_id"
  else
    resolve_by_roles "$child_id"
  fi
}

case "${1:-}" in
  resolve)
    [[ -n "${2:-}" ]] || { echo "::error::child id is required" >&2; exit 2; }
    resolve_child "$2"
    ;;
  plan)
    first=true
    printf '['
    while IFS= read -r child_id; do
      [[ -n "$child_id" ]] || continue
      repository="$(resolve_child "$child_id")"
      if [[ "$first" == true ]]; then
        first=false
      else
        printf ','
      fi
      jq -cn --arg id "$child_id" --arg repository "$repository"         '{id:$id,repository:$repository}'
    done < <(
      python3 "$relation" parent-child-ids --config "$parent_config"
    )
    printf ']\n'
    ;;
  *)
    echo "::error::usage: delegation_repository.sh {resolve CHILD_ID|plan}" >&2
    exit 2
    ;;
esac
