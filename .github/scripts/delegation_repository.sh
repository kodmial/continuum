#!/usr/bin/env bash
set -euo pipefail

: "${CONTINUUM_ENGINE_ROOT:?CONTINUUM_ENGINE_ROOT is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

relation="$CONTINUUM_ENGINE_ROOT/.github/scripts/delegation_runtime.py"
parent_config="${PARENT_CONFIG:-.continuum.yml}"
repository_map_json="${CHILD_REPOSITORIES:-}"
parent_role="${PARENT_ROLE:-}"
parent_children="${PARENT_CHILDREN:-}"

# Discovery can fail in two different ways and the caller must tell them
# apart. Exit status 4 means delegated discovery itself is unavailable: the
# GitHub API could not be reached or refused the request (for example when
# the token's rate budget is exhausted by a post-completion wake storm). It
# is returned instead of a verification verdict so an infrastructure failure
# is never mistaken for "no repository declares this relationship".
# Raw API errors stay hidden on purpose: they embed request URLs that would
# disclose the private child repository name in public parent logs.
UNAVAILABLE=4

# Bounded, classified GitHub REST read for delegated discovery
# (kodmial/continuum#326). The retry policy is authoritative in
# delegation_runtime.py: a transient 5xx/timeout or a rate-limited response
# retries a bounded number of times honouring Retry-After / x-ratelimit-reset
# (clamped), while an authentication/permission rejection never retries. On
# exhaustion the shared unavailable verdict is reported once and the function
# returns UNAVAILABLE, so a discovery outage can neither spin nor leak a raw
# API error (which would name the private child) into public parent logs.
api_read() {
  local max_attempts="${CONTINUUM_DISCOVERY_MAX_ATTEMPTS:-2}"
  local base_delay="${CONTINUUM_DISCOVERY_BASE_DELAY:-1}"
  local attempt=1 rc classification verdict delay backoff
  local raw err
  raw="$(mktemp "${RUNNER_TEMP:-.}/continuum-api-raw.XXXXXX")"
  err="$(mktemp "${RUNNER_TEMP:-.}/continuum-api-err.XXXXXX")"
  while :; do
    set +e
    gh api --include "$@" >"$raw" 2>"$err"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      python3 "$relation" strip-headers <"$raw"
      rm -f "$raw" "$err"
      return 0
    fi
    cat "$err" >>"$raw"
    classification="$(python3 "$relation" classify-response --raw-file "$raw" 2>/dev/null || true)"
    verdict="$(sed -n 's/^verdict=//p' <<<"$classification")"
    delay="$(sed -n 's/^retry-delay=//p' <<<"$classification")"
    if { [[ "$verdict" == "rate_limited" ]] || [[ "$verdict" == "transient" ]]; } &&
       [[ "$attempt" -lt "$max_attempts" ]]; then
      if [[ -n "$delay" && "$delay" != "0" ]]; then
        sleep "$delay" 2>/dev/null || true
      else
        # Match read_discovery: without a usable Retry-After, fall back to
        # exponential backoff (base * 2^(attempt-1)) rather than a fixed wait.
        backoff="$(
          python3 -c 'import sys; print("%g" % (float(sys.argv[1]) * (2 ** (int(sys.argv[2]) - 1))))' \
            "$base_delay" "$attempt" 2>/dev/null || printf '%s' "$base_delay"
        )"
        sleep "$backoff" 2>/dev/null || true
      fi
      attempt=$((attempt + 1))
      continue
    fi
    break
  done
  rm -f "$raw" "$err"
  echo "::error::Delegated child discovery is unavailable; refusing to guess." >&2
  return "$UNAVAILABLE"
}

parent_child_ids() {
  if [[ -n "$parent_role" || -n "$parent_children" ]]; then
    python3 "$relation" parent-variable-ids       --role "$parent_role"       --children-json "$parent_children"
    return
  fi

  local variables_json role children
  if ! variables_json="$(repository_variables "$GITHUB_REPOSITORY")"; then
    # The API read already reported the outage. Falling back to the tracked
    # file here would resolve a possibly stale child set, so refuse instead.
    return "$UNAVAILABLE"
  fi
  role="$(variable_value "$variables_json" CONTINUUM_ROLE)"
  children="$(variable_value "$variables_json" CONTINUUM_CHILDREN)"
  if [[ -n "$role" || -n "$children" ]]; then
    python3 "$relation" parent-variable-ids       --role "$role"       --children-json "$children"
    return
  fi

  python3 "$relation" parent-child-ids --config "$parent_config"
}

assert_parent_allows_child() {
  local child_id="$1"
  local child_ids lookup_rc=0
  if ! child_ids="$(parent_child_ids)"; then
    lookup_rc=$?
    return "$lookup_rc"
  fi
  if grep -Fxq "$child_id" <<<"$child_ids"; then
    return 0
  fi
  echo "::error::Child id is not allowed by CONTINUUM_CHILDREN." >&2
  return 2
}

repository_variables() {
  local repository="$1"
  local payload

  # Repository variables are paginated. A single first-page read can silently
  # miss CONTINUUM_ROLE / CONTINUUM_CHILDREN / child binding variables once a
  # repository has more than 100 variables, which turns a valid relationship
  # into an empty plan and strands otherwise eligible child work.
  #
  # Slurp every page into one normalized payload. Keep API stderr suppressed so
  # public parent logs never disclose a private child repository name; pipefail
  # still propagates an API or jq failure as UNAVAILABLE.
  if ! payload="$(
    api_read --paginate "repos/$repository/actions/variables?per_page=100" |
      jq -cs '{variables: [.[].variables[]?]}'
  )"; then
    return "$UNAVAILABLE"
  fi
  printf '%s' "$payload"
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
  if ! variables_json="$(repository_variables "$repository")"; then
    return "$UNAVAILABLE"
  fi
  if [[ -n "$(variable_value "$variables_json" CONTINUUM_ROLE)" ||
        -n "$(variable_value "$variables_json" CONTINUUM_CHILD_ID)" ||
        -n "$(variable_value "$variables_json" CONTINUUM_PARENT)" ]]; then
    # An explicit relationship must never fall back to stale tracked settings.
    verify_child_variables_json "$variables_json" "$child_id"
    return $?
  fi

  # Backward-compatible migration path. Once repository variables are present
  # this branch is not used; it exists so consumers can migrate without a
  # flag-day update across parent and child repositories. This branch runs
  # only after a successful variables read, so a failed file fetch means the
  # candidate declares no usable legacy relationship and stays a non-match.
  verify_child_legacy_config "$repository" "$child_id" >/dev/null 2>&1
}

resolve_by_roles() {
  local child_id="$1"
  local candidate matched=""
  local count=0

  assert_parent_allows_child "$child_id"

  local listing
  if ! listing="$(api_read --paginate '/user/repos?affiliation=owner&per_page=100' --jq '.[].full_name')"; then
    return "$UNAVAILABLE"
  fi

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    [[ "$candidate" == "$GITHUB_REPOSITORY" ]] && continue

    local candidate_rc=0
    verify_child_repository "$candidate" "$child_id" >/dev/null 2>&1 || candidate_rc=$?
    if [[ "$candidate_rc" -eq 0 ]]; then
      matched="$candidate"
      count=$((count + 1))
      if [[ "$count" -gt 1 ]]; then
        echo "::error::More than one repository declares the same child relationship." >&2
        return 2
      fi
    elif [[ "$candidate_rc" -eq "$UNAVAILABLE" ]]; then
      # Verification never ran for this candidate. Counting it as a
      # non-match would turn an API outage into a misleading zero-match
      # verdict, so abort the whole discovery instead. The per-candidate
      # probe suppressed its stderr, so report the shared verdict here.
      echo "::error::Delegated child discovery is unavailable; refusing to guess." >&2
      return "$UNAVAILABLE"
    fi
  done <<<"$listing"

  if [[ "$count" -ne 1 ]]; then
    echo "::error::No unique repository declares the requested child relationship." >&2
    return 2
  fi

  printf '%s\n' "$matched"
}

resolve_child() {
  local child_id="$1"
  if [[ -n "$repository_map_json" ]]; then
    local child_ids allowed_ids_json
    child_ids="$(parent_child_ids)"
    allowed_ids_json="$(
      printf '%s\n' "$child_ids" |
        jq -Rsc 'split("\n") | map(select(length > 0))'
    )"
    python3 "$relation" resolve-child-ids       --repository-map-json "$repository_map_json"       --allowed-ids-json "$allowed_ids_json"       --child-id "$child_id"
  else
    resolve_by_roles "$child_id"
  fi
}

validation_script() {
  local repository="$1"
  local variables_json value
  if ! variables_json="$(repository_variables "$repository")"; then
    return "$UNAVAILABLE"
  fi
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
    # Capture failures before process substitution can hide a rejected parent.
    child_ids="$(parent_child_ids)"
    allowlist_json="$(
      printf '%s\n' "$child_ids" |
        jq -Rsc 'split("\n") | map(select(length > 0))'
    )"
    cache_file="${CONTINUUM_DELEGATION_CACHE:-}"
    cache_ttl="${CONTINUUM_DELEGATION_CACHE_TTL:-900}"
    now_epoch="$(date +%s)"
    # Reuse an inventory already trusted for this exact parent and allowlist
    # within its TTL. The cache is authenticated (parent + allowlist
    # fingerprint), strictly scoped, TTL-limited and never persisted outside
    # the runner workspace, so a private child name cannot leak.
    if [[ -n "$cache_file" ]]; then
      if cached_plan="$(python3 "$relation" inventory-read \
        --cache "$cache_file" --parent "$GITHUB_REPOSITORY" \
        --allowlist-json "$allowlist_json" --now "$now_epoch" \
        --ttl "$cache_ttl" 2>/dev/null)"; then
        printf '%s\n' "$cached_plan"
        exit 0
      fi
    fi
    entries=()
    while IFS= read -r child_id; do
      [[ -n "$child_id" ]] || continue
      repository="$(resolve_child "$child_id")"
      entries+=(
        "$(jq -cn --arg id "$child_id" --arg repository "$repository" \
          '{id:$id,repository:$repository}')"
      )
    done <<<"$child_ids"
    plan_json="$(printf '%s\n' "${entries[@]:-}" | jq -cs '.')"
    printf '%s\n' "$plan_json"
    if [[ -n "$cache_file" ]]; then
      plan_tmp="${RUNNER_TEMP:-.}/continuum-inventory-plan.$$.json"
      printf '%s\n' "$plan_json" >"$plan_tmp"
      python3 "$relation" inventory-write \
        --cache "$cache_file" --parent "$GITHUB_REPOSITORY" \
        --allowlist-json "$allowlist_json" --plan-file "$plan_tmp" \
        --now "$now_epoch" --ttl "$cache_ttl" 2>/dev/null || true
      rm -f "$plan_tmp"
    fi
    ;;
  *)
    echo "::error::usage: delegation_repository.sh {resolve CHILD_ID|verify CHILD_ID REPOSITORY|validation REPOSITORY|plan}" >&2
    exit 2
    ;;
esac
