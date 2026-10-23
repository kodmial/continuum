#!/usr/bin/env bash
# Authoritative delegated active-run discovery (kodmial/continuum#387).
#
# A global newest-100 *all-status* Actions listing silently loses an older
# active SubTask once unrelated completed workflows churn past it. That made
# the Parent scheduler dispatch a second job for the same task. This helper
# reads active execution states through explicit, server-side status filters,
# so the result depends only on the number of *active* runs, never on
# completed history. Every failure (HTTP 4xx/5xx, timeout, malformed payload,
# or truncation at the page cap) is reported as unavailable and the caller
# must fail closed for new delegated coding dispatch instead of assuming
# "none active".
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

# GitHub Actions REST status values that mean work still owns its
# concurrency identity. `requested` and `pending` cover jobs that have not
# been assigned a runner yet.
ACTIVE_STATUSES=(in_progress queued pending waiting requested)
PER_PAGE="${CONTINUUM_ACTIVE_RUNS_PER_PAGE:-100}"
MAX_PAGES="${CONTINUUM_ACTIVE_RUNS_MAX_PAGES:-5}"
UNAVAILABLE=4

# Raw API output can embed private request URLs, so it is always discarded.
# The diagnostic names only the public capability and the recovery action.
unavailable() {
  echo "::error::Delegated active-run discovery is unavailable; refusing to guess for new delegated coding dispatch (the next scheduler wake retries automatically)." >&2
  exit "$UNAVAILABLE"
}

usage() {
  echo "::error::usage: delegated_active_runs.sh snapshot --output FILE | task-active --file FILE --child-id ID --kind task|review --task NUMBER | check --child-id ID --task NUMBER | wip-count --file FILE" >&2
  exit 2
}

title_column() {
  local file="$1"
  [[ -f "$file" ]] || return "$UNAVAILABLE"
  cut -f2- "$file"
}

snapshot() {
  local output="" arg
  while (($#)); do
    case "$1" in
      --output) output="${2:-}"; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "$output" ]] || usage

  local tmp
  tmp="$(mktemp "${RUNNER_TEMP:-.}/continuum-active-runs.XXXXXX")" || unavailable
  : >"$tmp"
  # Never leave a stale successful snapshot behind on failure.
  rm -f "$output"

  local status page count raw total
  for status in "${ACTIVE_STATUSES[@]}"; do
    page=1
    while :; do
      if ! raw="$(gh api \
        "repos/$GITHUB_REPOSITORY/actions/runs?status=$status&per_page=$PER_PAGE&page=$page" \
        2>/dev/null)"; then
        rm -f "$tmp"
        unavailable
      fi

      # A malformed payload is an outage, not an empty active inventory.
      if ! count="$(jq -er \
        'if (.workflow_runs | type) == "array" then (.workflow_runs | length) else error("bad workflow_runs") end' \
        <<<"$raw" 2>/dev/null)"; then
        rm -f "$tmp"
        unavailable
      fi

      # Trust only the requested active status. Terminal historical states
      # (completed/cancelled/timed_out/etc.) can never occupy WIP.
      if ! jq -r --arg status "$status" '
        .workflow_runs[]
        | select((.status // "") == $status)
        | [(.status // ""), ((.display_title // "") | gsub("[\\r\\n\\t]"; " "))]
        | @tsv
      ' <<<"$raw" >>"$tmp" 2>/dev/null; then
        rm -f "$tmp"
        unavailable
      fi

      if (( count < PER_PAGE )); then
        break
      fi
      if (( page >= MAX_PAGES )); then
        # A full page at the explicit cap is truncated evidence: fail closed
        # rather than silently dropping an older active run.
        rm -f "$tmp"
        unavailable
      fi
      page=$((page + 1))
    done
  done

  # Deduplicate across status transitions and sort for deterministic output.
  if ! sort -u "$tmp" >"$output"; then
    rm -f "$tmp" "$output"
    unavailable
  fi
  rm -f "$tmp"
}

task_active() {
  local file="" child_id="" kind="task" task=""
  while (($#)); do
    case "$1" in
      --file) file="${2:-}"; shift 2 ;;
      --child-id) child_id="${2:-}"; shift 2 ;;
      --kind) kind="${2:-}"; shift 2 ;;
      --task) task="${2:-}"; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "$file" && -n "$child_id" && -n "$task" ]] || usage
  [[ "$task" =~ ^[0-9]+$ ]] || usage

  local titles neutral_title
  titles="$(title_column "$file")" || unavailable
  if [[ "$kind" == "task" ]]; then
    neutral_title="SubTask #$task"
  elif [[ "$kind" == "review" ]]; then
    neutral_title="SubTask review #$task"
  else
    usage
  fi
  grep -Fxq "$neutral_title" <<<"$titles" ||
    grep -Fxq "Child $kind [$child_id] #$task" <<<"$titles"
}

check() {
  local child_id="" task=""
  while (($#)); do
    case "$1" in
      --child-id) child_id="${2:-}"; shift 2 ;;
      --task) task="${2:-}"; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "$child_id" && -n "$task" ]] || usage

  local tmp
  tmp="$(mktemp "${RUNNER_TEMP:-.}/continuum-active-check.XXXXXX")" || unavailable
  # Reuse the exact same bounded, status-filtered discovery for a fresh
  # pre-dispatch recheck. A failure here is likewise fail-closed.
  snapshot --output "$tmp" || {
    local rc=$?
    rm -f "$tmp"
    exit "$rc"
  }

  if task_active --file "$tmp" --child-id "$child_id" --kind task --task "$task"; then
    rm -f "$tmp"
    return 0
  fi
  if task_active --file "$tmp" --child-id "$child_id" --kind review --task "$task"; then
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

wip_count() {
  local file=""
  while (($#)); do
    case "$1" in
      --file) file="${2:-}"; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "$file" ]] || usage

  local titles title key count=0
  local -A seen=()
  titles="$(title_column "$file")" || unavailable
  while IFS= read -r title; do
    key=""
    if [[ "$title" =~ ^SubTask[[:space:]]#([0-9]+)$ ]]; then
      key="subtask#${BASH_REMATCH[1]}"
    elif [[ "$title" =~ ^Child[[:space:]]task[[:space:]]\[([^]]+)\][[:space:]]#([0-9]+)$ ]]; then
      key="${BASH_REMATCH[1]}#${BASH_REMATCH[2]}"
    fi
    if [[ -n "$key" && -z "${seen[$key]:-}" ]]; then
      seen["$key"]=1
      count=$((count + 1))
    fi
  done <<<"$titles"
  printf '%s\n' "$count"
}

command="${1:-}"
[[ -n "$command" ]] || usage
shift
case "$command" in
  snapshot) snapshot "$@" ;;
  task-active) task_active "$@" ;;
  check) check "$@" ;;
  wip-count) wip_count "$@" ;;
  *) usage ;;
esac
