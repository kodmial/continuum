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
telemetry_dir="${CONTINUUM_OPENCODE_TELEMETRY_DIR:-$runner_temp/continuum-opencode-telemetry}"
telemetry_sequence=0

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

write_telemetry() {
  local mode="$1" log_path="$2" status="$3" classification="$4" diagnostics="${5:-}"
  local output_dir output model timestamp
  output_dir="$telemetry_dir"
  if ! mkdir -p "$output_dir" 2>/dev/null; then
    echo "::warning::Cannot create OpenCode telemetry directory; provider classification and exit status are unchanged." >&2
    return 0
  fi
  telemetry_sequence=$((telemetry_sequence + 1))
  timestamp="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo unknown)"
  output="$output_dir/opencode-${mode}-${timestamp}-$$-${telemetry_sequence}.json"
  # ``run`` executors export OPENCODE_MODEL; the interactive ``github run``
  # path exports the same frozen route as MODEL. Persist whichever is set.
  model="${OPENCODE_MODEL:-${MODEL:-}}"
  local args=(
    telemetry --mode "$mode" --log "$log_path" --exit "$status"
    --classification "$classification" --model "$model" --output "$output"
    --print-summary
  )
  if [[ -n "$diagnostics" ]]; then
    args+=(--diagnostics "$diagnostics")
  fi
  if python3 "$classifier" "${args[@]}" >&2; then
    echo "::notice::OpenCode telemetry persisted as $(basename "$output")." >&2
  else
    echo "::warning::OpenCode telemetry summary could not be persisted; provider classification and exit status are unchanged." >&2
  fi
  return 0
}

# An interrupted attempt must still leave exactly one telemetry record, because
# #454 compares provider-429 attempts including the ones that were cancelled.
# The handlers are installed only after the telemetry mode and log paths exist,
# and the normal completion paths clear them first so a finished attempt keeps
# its existing classification and never writes a second record.
telemetry_mode=""
telemetry_log=""
telemetry_diagnostics=""
# Each entry is "<role> <pid>". Roles drive the interruption order: producers
# (the CLI) stop first so the event pipe reaches EOF, readers (the renderer or
# the tee pair) then drain every buffered event into the logs telemetry reads,
# and the live-status watcher is last because it can be parked in an HTTP call.
telemetry_children=()
telemetry_fifo_paths=()

track_telemetry_child() {
  telemetry_children+=("$1 $2")
}

untrack_telemetry_child() {
  local target="$1" entry
  local remaining=()
  for entry in "${telemetry_children[@]+"${telemetry_children[@]}"}"; do
    if [[ "${entry#* }" != "$target" ]]; then
      remaining+=("$entry")
    fi
  done
  telemetry_children=("${remaining[@]+"${remaining[@]}"}")
}

wait_telemetry_child() {
  local pid="$1" child_status
  wait "$pid" 2>/dev/null
  child_status=$?
  untrack_telemetry_child "$pid"
  return "$child_status"
}

# A child that already exited but has not been reaped is a zombie, for which
# ``kill -0`` still succeeds; a bounded reap must inspect the process state.
telemetry_child_running() {
  local pid="$1" state
  if ! state="$(ps -o stat= -p "$pid" 2>/dev/null)"; then
    # Without process-state information, assume a live child so the caller's
    # bounded escalation still applies instead of an unbounded wait.
    kill -0 "$pid" 2>/dev/null
    return
  fi
  [[ -n "$state" && "$state" != *Z* ]]
}

# Reap one child without letting it postpone the interruption record forever.
# Cooperative children exit on TERM; a child stuck in a request is escalated.
reap_telemetry_child_bounded() {
  local pid="$1" limit="${2:-2}" waited=0
  while telemetry_child_running "$pid"; do
    if ((waited >= limit)); then
      kill -KILL "$pid" 2>/dev/null || true
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" 2>/dev/null || true
  untrack_telemetry_child "$pid"
}

# Give a reader a bounded grace period to flush the pipe buffer after the
# producer stopped. A CLI descendant that inherited the write end can keep the
# reader waiting for an EOF that never arrives, so the reader is stopped after
# the grace period instead of blocking the interruption record.
drain_telemetry_reader() {
  local pid="$1" limit="${2:-2}" waited=0
  while telemetry_child_running "$pid"; do
    if ((waited >= limit)); then
      kill "$pid" 2>/dev/null || true
      reap_telemetry_child_bounded "$pid" 1
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" 2>/dev/null || true
  untrack_telemetry_child "$pid"
}

# Commands run in the background so a TERM/INT/HUP addressed only to this
# wrapper interrupts ``wait`` immediately. Stopping the producer first lets the
# readers drain events already buffered in the FIFO, so an interrupted attempt
# keeps its evidence instead of truncating it; no writer is left mutating the
# logs telemetry reads. Invoked by the trap handler below, which shellcheck
# cannot follow.
# shellcheck disable=SC2329
stop_telemetry_children() {
  local entry role pid
  # 1. Stop the producer so every reader sees EOF.
  for entry in "${telemetry_children[@]+"${telemetry_children[@]}"}"; do
    role="${entry%% *}"
    pid="${entry#* }"
    if [[ "$role" == "producer" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for entry in "${telemetry_children[@]+"${telemetry_children[@]}"}"; do
    role="${entry%% *}"
    pid="${entry#* }"
    if [[ "$role" == "producer" ]]; then
      reap_telemetry_child_bounded "$pid" 5
    fi
  done
  # 2. Let the readers flush buffered events into the logs telemetry reads.
  for entry in "${telemetry_children[@]+"${telemetry_children[@]}"}"; do
    role="${entry%% *}"
    pid="${entry#* }"
    if [[ "$role" == "reader" ]]; then
      drain_telemetry_reader "$pid" 2
    fi
  done
  # 3. The watcher may be inside an HTTP request; never wait on it indefinitely.
  for entry in "${telemetry_children[@]+"${telemetry_children[@]}"}"; do
    role="${entry%% *}"
    pid="${entry#* }"
    if [[ "$role" == "watcher" ]]; then
      kill "$pid" 2>/dev/null || true
      reap_telemetry_child_bounded "$pid" 2
    fi
  done
  telemetry_children=()
}

new_telemetry_fifo() {
  local fifo
  fifo="$(mktemp -u "$runner_temp/continuum-opencode-pipe.XXXXXX")"
  mkfifo "$fifo" || return 1
  printf '%s\n' "$fifo"
}

remove_telemetry_fifos() {
  local fifo
  for fifo in "${telemetry_fifo_paths[@]+"${telemetry_fifo_paths[@]}"}"; do
    rm -f "$fifo"
  done
  telemetry_fifo_paths=()
}

# Invoked indirectly by the TERM/INT/HUP traps below, which shellcheck cannot
# follow through the quoted trap actions.
# shellcheck disable=SC2329
record_interruption() {
  local signal="$1" signal_status="$2"
  # A second cancellation signal must not terminate the wrapper before the
  # record exists, so ignore further TERM/INT/HUP while this handler runs.
  trap '' TERM INT HUP
  stop_telemetry_children
  if [[ -n "$telemetry_mode" && -n "$telemetry_log" ]]; then
    echo "::warning::OpenCode attempt interrupted by SIG${signal}; recording an inconclusive telemetry record." >&2
    write_telemetry "$telemetry_mode" "$telemetry_log" "$signal_status" "inconclusive" "$telemetry_diagnostics"
  fi
  if [[ -n "$telemetry_log" ]]; then rm -f "$telemetry_log"; fi
  if [[ -n "$telemetry_diagnostics" ]]; then rm -f "$telemetry_diagnostics"; fi
  remove_telemetry_fifos
  exit "$signal_status"
}

install_signal_handlers() {
  trap 'record_interruption TERM 143' TERM
  trap 'record_interruption INT 130' INT
  trap 'record_interruption HUP 129' HUP
}

clear_signal_handlers() {
  trap - TERM INT HUP
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
  telemetry_mode="run"
  telemetry_log="$log"
  telemetry_diagnostics=""
  install_signal_handlers
  if ! render_pipe="$(new_telemetry_fifo)"; then
    rm -f "$log"
    echo "::error::Cannot create OpenCode event pipe; refusing provider-error classification." >&2
    exit 2
  fi
  telemetry_fifo_paths+=("$render_pipe")
  # Live status contains only safe JSON event metadata, never source or prompts.
  # The shared workflow already copies src/continuum into this wrapper's root.
  # Reuse the existing job credential without changing protected workflow YAML.
  live_script="$script_dir/src/continuum/opencode_live_status.py"
  live_issue="${ISSUE_NUMBER:-${PR_NUMBER:-${QUALIFICATION_NUMBER:-}}}"
  live_token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  live_pid=""
  if [[ -f "$live_script" && -n "$live_token" && -n "$live_issue" ]]; then
    CONTINUUM_LIVE_LOG_TOKEN="$live_token" CONTINUUM_LIVE_LOG_ISSUE="$live_issue" python3 "$live_script" watch --log "$log" >/dev/null 2>&1 &
    live_pid=$!
    track_telemetry_child watcher "$live_pid"
  fi
  set +e
  # A FIFO keeps both pipeline members as direct children, so the interrupt
  # handler can stop and reap the CLI and renderer before writing telemetry.
  python3 "$classifier" render --log "$log" <"$render_pipe" &
  render_pid=$!
  track_telemetry_child reader "$render_pid"
  env -u CONTINUUM_LIVE_LOG_TOKEN -u CONTINUUM_LIVE_LOG_ISSUE opencode "${args[@]}" >"$render_pipe" 2>&1 &
  opencode_pid=$!
  track_telemetry_child producer "$opencode_pid"
  wait_telemetry_child "$opencode_pid"
  status=$?
  wait_telemetry_child "$render_pid"
  render_status=$?
  set -e
  remove_telemetry_fifos
  if [[ -n "$live_pid" ]]; then
    kill "$live_pid" 2>/dev/null || true
    wait_telemetry_child "$live_pid" || true
    CONTINUUM_LIVE_LOG_TOKEN="$live_token" CONTINUUM_LIVE_LOG_ISSUE="$live_issue" python3 "$live_script" once --log "$log" --state finished >/dev/null 2>&1 || true
  fi
  if [[ "$render_status" -ne 0 ]]; then
    clear_signal_handlers
    write_telemetry "run" "$log" "$status" "inconclusive"
    rm -f "$log"
    echo "::error::OpenCode event rendering failed; refusing to infer provider health." >&2
    exit "$render_status"
  fi
  set +e
  classification="$(python3 "$classifier" classify --mode run --log "$log" --exit "$status")"
  classifier_status=$?
  set -e
  if [[ "$classifier_status" -ne 0 ]]; then
    clear_signal_handlers
    write_telemetry "run" "$log" "$status" "inconclusive"
    rm -f "$log"
    echo "::error::OpenCode event classification failed; refusing to infer provider health." >&2
    exit "$classifier_status"
  fi
  clear_signal_handlers
  write_telemetry "run" "$log" "$status" "$classification"
  rm -f "$log"
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
  telemetry_mode="github"
  telemetry_log="$stdout_log"
  telemetry_diagnostics="$stderr_log"
  install_signal_handlers
  if ! stdout_pipe="$(new_telemetry_fifo)"; then
    rm -f "$stdout_log" "$stderr_log"
    echo "::error::Cannot create OpenCode output pipe; refusing provider-error classification." >&2
    exit 2
  fi
  telemetry_fifo_paths+=("$stdout_pipe")
  if ! stderr_pipe="$(new_telemetry_fifo)"; then
    remove_telemetry_fifos
    rm -f "$stdout_log" "$stderr_log"
    echo "::error::Cannot create OpenCode diagnostics pipe; refusing provider-error classification." >&2
    exit 2
  fi
  telemetry_fifo_paths+=("$stderr_pipe")
  set +e
  # Tee readers are reaped explicitly so telemetry only reads fully flushed
  # logs, and a wrapper-only signal cannot leave the CLI running unattended.
  tee "$stdout_log" <"$stdout_pipe" &
  stdout_tee_pid=$!
  track_telemetry_child reader "$stdout_tee_pid"
  tee "$stderr_log" >&2 <"$stderr_pipe" &
  stderr_tee_pid=$!
  track_telemetry_child reader "$stderr_tee_pid"
  opencode "$@" >"$stdout_pipe" 2>"$stderr_pipe" &
  opencode_pid=$!
  track_telemetry_child producer "$opencode_pid"
  wait_telemetry_child "$opencode_pid"
  status=$?
  wait_telemetry_child "$stdout_tee_pid" || true
  wait_telemetry_child "$stderr_tee_pid" || true
  set -e
  remove_telemetry_fifos
  set +e
  classification="$(python3 "$classifier" classify --mode github --log "$stdout_log" --diagnostics "$stderr_log" --exit "$status")"
  classifier_status=$?
  set -e
  if [[ "$classifier_status" -ne 0 ]]; then
    clear_signal_handlers
    write_telemetry "github" "$stdout_log" "$status" "inconclusive" "$stderr_log"
    rm -f "$stdout_log" "$stderr_log"
    echo "::error::OpenCode diagnostics classification failed; refusing to infer provider health." >&2
    exit "$classifier_status"
  fi
  clear_signal_handlers
  write_telemetry "github" "$stdout_log" "$status" "$classification" "$stderr_log"
  rm -f "$stdout_log" "$stderr_log"
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
