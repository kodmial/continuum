#!/usr/bin/env bash
# Continuum OpenCode wrapper: provider health is classified only from
# authoritative provider error channels, never from a whole-transcript grep.
#
# `run` is invoked with `--format json`; the shared classifier reads only
# top-level type=error events. `github run` has no JSON mode on the pinned CLI,
# so only its stderr `Agent error:` diagnostic block is trusted. Agent prose,
# tool output, source files, and test fixtures can contain quota tokens without
# ever affecting classification.
set -uo pipefail

runner_temp="${RUNNER_TEMP:-/tmp}"
marker="$runner_temp/continuum-opencode-429"
upstream_marker="$runner_temp/continuum-opencode-upstream-unavailable"
classification_file="${CONTINUUM_429_CLASSIFICATION_FILE:-$runner_temp/continuum-opencode-classification}"

if [[ -f "$marker" ]]; then
  echo "::error::CONTINUUM_OPENCODE_429_RESTART_REQUIRED (runner already burned; refusing a second model invocation on this VM)."
  exit 75
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${CONTINUUM_429_CLASSIFIER:-}" ]]; then
  classifier="$CONTINUUM_429_CLASSIFIER"
elif [[ -f "$script_dir/opencode_429_classify.py" ]]; then
  classifier="$script_dir/opencode_429_classify.py"
elif [[ -f "$script_dir/.github/scripts/opencode_429_classify.py" ]]; then
  classifier="$script_dir/.github/scripts/opencode_429_classify.py"
else
  echo "::error::Continuum 429 classifier is missing; refusing unstructured provider-error classification."
  exit 2
fi

write_classification() {
  local value="$1"
  printf '%s\n' "$value" >"$classification_file" 2>/dev/null || true
}

fail_provider() {
  local classification="$1" status="$2"
  write_classification "$classification"
  case "$classification" in
    quota_429)
      : >"$marker"
      echo "::error::OpenCode 429: retire this runner and recover on a fresh runner."
      exit 75
      ;;
    unavailable|upstream|timeout)
      : >"$upstream_marker"
      echo "::error::CONTINUUM_OPENCODE_UPSTREAM_UNAVAILABLE_RESTART_REQUIRED"
      exit 76
      ;;
    fail_closed)
      echo "::error::OpenCode provider rejected the request deterministically; failing closed."
      if [[ "$status" -eq 0 ]]; then exit 1; fi
      exit "$status"
      ;;
    provider_error)
      echo "::error::OpenCode reported an authenticated provider error."
      if [[ "$status" -eq 0 ]]; then exit 1; fi
      exit "$status"
      ;;
  esac
}

command="${1:-}"
if [[ "$command" == "run" ]]; then
  args=("$@")
  explicit_format=""
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "--format" ]]; then
      next=$((i + 1))
      explicit_format="${!next:-}"
    elif [[ "${!i}" == --format=* ]]; then
      explicit_format="${!i#--format=}"
    fi
  done
  if [[ -n "$explicit_format" && "$explicit_format" != "json" ]]; then
    echo "::error::OpenCode was invoked without structured JSON events; refusing unstructured provider-error classification." >&2
    exit 2
  fi
  if [[ -z "$explicit_format" ]]; then
    args=(run --format json "${@:2}")
  fi

  log="$(mktemp "$runner_temp/continuum-opencode-run.XXXXXX")"
  set +e
  opencode "${args[@]}" 2>&1 | python3 "$classifier" render --log "$log"
  pipe_status=("${PIPESTATUS[@]}")
  status="${pipe_status[0]:-1}"
  render_status="${pipe_status[1]:-1}"
  set -e
  if [[ "$render_status" -ne 0 ]]; then
    rm -f "$log"
    echo "::error::OpenCode event rendering failed; refusing to infer provider health." >&2
    exit "$render_status"
  fi
  set +e
  classification="$(python3 "$classifier" classify --mode run --log "$log" --exit "$status")"
  classifier_status=$?
  set -e
  rm -f "$log"
  if [[ "$classifier_status" -ne 0 ]]; then
    echo "::error::OpenCode event classification failed; refusing to infer provider health." >&2
    exit "$classifier_status"
  fi
  write_classification "$classification"
  case "$classification" in
    quota_429|unavailable|upstream|timeout|fail_closed|provider_error)
      fail_provider "$classification" "$status"
      ;;
    inconclusive)
      if [[ "$status" -ne 0 ]]; then
        : >"$runner_temp/continuum-opencode-inconclusive"
        echo "::warning::OpenCode failed without an authenticated provider error; leaving the failure classified as a task failure." >&2
      fi
      ;;
  esac
  exit "$status"
fi

if [[ "$command" == "github" && "${2:-}" == "run" ]]; then
  # No argument rewriting: the pinned CLI does not accept --format here.
  # stdout carries the agent response, stderr carries runtime diagnostics.
  stdout_log="$(mktemp "$runner_temp/continuum-opencode-github-out.XXXXXX")"
  stderr_log="$(mktemp "$runner_temp/continuum-opencode-github-err.XXXXXX")"
  set +e
  opencode "$@" > >(tee "$stdout_log") 2> >(tee "$stderr_log" >&2)
  status=$?
  wait
  set -e
  set +e
  classification="$(python3 "$classifier" classify --mode github --log "$stdout_log" --diagnostics "$stderr_log" --exit "$status")"
  classifier_status=$?
  set -e
  rm -f "$stdout_log" "$stderr_log"
  if [[ "$classifier_status" -ne 0 ]]; then
    echo "::error::OpenCode diagnostics classification failed; refusing to infer provider health." >&2
    exit "$classifier_status"
  fi
  write_classification "$classification"
  case "$classification" in
    quota_429|unavailable|upstream|timeout|fail_closed|provider_error)
      fail_provider "$classification" "$status"
      ;;
    inconclusive)
      if [[ "$status" -ne 0 ]]; then
        : >"$runner_temp/continuum-opencode-inconclusive"
        echo "::warning::OpenCode github run failed without an authenticated provider error; leaving the failure classified as a task failure." >&2
      fi
      ;;
  esac
  exit "$status"
fi

# Unknown invocation: execute unchanged and never infer provider health.
opencode "$@" 2>&1 | tee "$runner_temp/continuum-opencode-unclassified.log"
status=${PIPESTATUS[0]}
rm -f "$runner_temp/continuum-opencode-unclassified.log"
exit "$status"
