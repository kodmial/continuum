#!/usr/bin/env bash
set -euo pipefail

: "${CONTINUUM_ENGINE_ROOT:?CONTINUUM_ENGINE_ROOT is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

relation="$CONTINUUM_ENGINE_ROOT/.github/scripts/delegation_runtime.py"
parent_config="${PARENT_CONFIG:-.continuum.yml}"
repository_map_json="${CHILD_REPOSITORIES:-}"
parent_role="${PARENT_ROLE:-}"
parent_children="${PARENT_CHILDREN:-}"

parent_child_ids() {
  if [[ -n "$parent_role" || -n "$parent_children" ]]; then
    python3 "$relation" parent-variable-ids       --role "$parent_role"       --children-json "$parent_children"
  else
    python3 "$relation" parent-child-ids --config "$parent_config"
  fi
}

assert_parent_allows_child() {
  local child_id="$1"
  if [[ -n "$parent_role" || -n "$parent_children" ]]; then
    parent_child_ids | grep -Fxq "$child_id" || {
      echo "::error::Child id is not allowed by CONTINUUM_CHILDREN." >&2
      return 2
    }
  else
    python3 "$relation" parent-allows-child       --config "$parent_config"       --child-id "$child_id"
  fi
}

repository_variables() {
  local repository="$1"
  gh api "repos/$repository/actions/variables?per_page=100" 2>/dev/null || true
}

variable_value() {
  local json="$1" name="$2"
  jq -r --arg name "$name"     '[.variables[]? | select(.name == $name) | .value][0] // ""'     <<<"$json" 2>/dev/null || true
}

verify_child_variables_json() {
  local variables_json="$1" child_id="$2"
  local role declared_id declared_parent
  role="$(variable_value "$variables_json" CONTINUUM_ROLE)"
  declared_id="$(variable_value "$variables_json" CONTINUUM_CHILD_ID)"
  declared_parent="$(variable_value "$variables_json" CONTINUUM_PARENT)"

  [[ -n "$role" || -n "$declared_id" || -n "$declared_parent" ]] || return 3

  python3 "$relation" verify-child-variables     --role "$role"     --declared-id "$declared_id"     --declared-parent "$declared_parent"     --child-id "$child_id"     --parent-repository "$GITHUB_REPOSITORY"
}

verify_child_legacy_config() {
  local repository="$1" child_id="$2"
  local config_file="$RUNNER_TEMP/continuum-child-legacy-${child_id}.yml"
  if ! gh api "repos/$repository/contents/.continuum.yml" --jq '.content' 2>/dev/null     | tr -d '\n' | base64 -d >"$config_file" 2>/dev/null; then
    rm -f "$config_file"
    return 2
  fi
  python3 "$relation" verify-child     --config "$config_file"     --child-id "$child_id"     --parent-repository "$GITHUB_REPOSITORY"
  local rc=$?
  rm -f "$config_file"
  return "$rc"
}

verify_child_repository() {
  local repository="$1" child_id="$2"
  local variables_json
  variables_json="$(repository_variables "$repository")"
  if [[ -n "$variables_json" ]] && verify_child_variables_json "$variables_json" "$child_id" >/dev/null 2>&1; then
    return 0
  fi

  # Backward-compatible migration path. Once repository variables are present
  # this branch is not used; it exists so consumers can migrate without a
  # flag-day update across parent and child repositories.
  verify_child_legacy_config "$repository" "$child_id" >/dev/null 2>&1
}

resolve_by_roles() {
  local child_id="$1"
  local candidate matched=""
  local count=0

  assert_parent_allows_child "$child_id"

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" == "$GITHUB_REPOSITORY" ]] && continue

    if verify_child_repository "$candidate" "$child_id"; then
      matched="$candidate"
      count=$((count + 1))
      if [[ "$count" -gt 1 ]]; then
        echo "::error::More than one repository declares the same child relationship." >&2
        return 2
      fi
    fi
  done < <(
    gh api --paginate '/user/repos?affiliation=owner&per_page=100'       --jq '.[].full_name' 2>/dev/null
  )

  if [[ "$count" -ne 1 ]]; then
    echo "::error::No unique repository declares the requested child relationship." >&2
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

validation_script() {
  local repository="$1"
  local variables_json value
  variables_json="$(repository_variables "$repository")"
  value="$(variable_value "$variables_json" CONTINUUM_VALIDATION_SCRIPT)"
  if [[ -n "$value" ]]; then
    python3 "$relation" validation-script-value --value "$value"
    return 0
  fi

  local config_file="$RUNNER_TEMP/continuum-child-validation.yml"
  if gh api "repos/$repository/contents/.continuum.yml" --jq '.content' 2>/dev/null     | tr -d '\n' | base64 -d >"$config_file" 2>/dev/null; then
    python3 "$relation" validation-script --config "$config_file"
    rm -f "$config_file"
    return 0
  fi
  rm -f "$config_file"
  printf '\n'
}

case "${1:-}" in
  resolve)
    [[ -n "${2:-}" ]] || { echo "::error::child id is required" >&2; exit 2; }
    resolve_child "$2"
    ;;
  verify)
    [[ -n "${2:-}" && -n "${3:-}" ]] || {
      echo "::error::usage: delegation_repository.sh verify CHILD_ID REPOSITORY" >&2
      exit 2
    }
    assert_parent_allows_child "$2"
    verify_child_repository "$3" "$2"
    ;;
  validation)
    [[ -n "${2:-}" ]] || {
      echo "::error::usage: delegation_repository.sh validation REPOSITORY" >&2
      exit 2
    }
    validation_script "$2"
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
    done < <(parent_child_ids)
    printf ']\n'
    ;;
  *)
    echo "::error::usage: delegation_repository.sh {resolve CHILD_ID|verify CHILD_ID REPOSITORY|validation REPOSITORY|plan}" >&2
    exit 2
    ;;
esac
