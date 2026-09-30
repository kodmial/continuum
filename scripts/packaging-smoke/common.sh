#!/bin/bash
# common.sh — shared helpers for the NanoDictate packaging lifecycle smoke.
#
# Sourced (not executed) by homebrew-lifecycle.sh and macports-lifecycle.sh.
# All probes are black-box and low-side-effect: exit status, receipts, file
# existence, launchd registration and live PIDs. No microphone, TCC grants,
# STT providers, network calls by NanoDictate itself, GUI automation or
# keyboard simulation. No exact localized text matching and no unbounded
# retries — every wait is a bounded poll with a deadline.

set -euo pipefail

# Current lifecycle phase (used in failure summaries).
SMOKE_PHASE="${SMOKE_PHASE:-init}"
# Failing command/check of the current phase (used in failure summaries).
SMOKE_CHECK="${SMOKE_CHECK:-}"
# Directory where metadata, summaries and diagnostics are written.
SMOKE_RESULT_DIR="${SMOKE_RESULT_DIR:-./smoke-results}"

smoke_log() {
  printf '[smoke] %s\n' "$*"
}

smoke_phase() {
  SMOKE_PHASE="$1"
  SMOKE_CHECK=""
  smoke_log "=== phase: $1 ==="
}

smoke_set_check() {
  SMOKE_CHECK="$1"
  smoke_log "check: $1"
}

# Mark a phase as failed and exit non-zero. Never prints secrets: callers pass
# only the command line and exit status, never env dumps or config contents.
smoke_fail() {
  local message="$1"
  printf '[smoke][FAIL] phase=%s check=%s msg=%s\n' \
    "$SMOKE_PHASE" "${SMOKE_CHECK:-<none>}" "$message" >&2
  exit 1
}

# Extract the version from Sources/NanoDictateCore/Version.swift.
smoke_repo_version() {
  grep -m1 -oE '[0-9]+\.[0-9]+\.[0-9]+' Sources/NanoDictateCore/Version.swift
}

# Assert that `nanodictate --version` exits 0 and carries $1 as a whole token.
# A longer number that merely contains the version (0.1.01, 10.1.0) does not
# match: the output is split on every non-digit/non-dot character first.
smoke_assert_version() {
  local expected="$1"
  local out
  smoke_set_check "nanodictate --version carries $expected"
  out="$("$NANODICTATE_BIN" --version 2>&1)" || \
    smoke_fail "nanodictate --version exited non-zero: $out"
  printf '%s\n' "$out" | /usr/bin/tr -c '0-9.' '\n' | /usr/bin/grep -Fxq "$expected" || \
    smoke_fail "nanodictate --version output does not carry $expected as a whole token: $out"
  smoke_log "version ok: $out"
  printf '%s' "$out" > "$SMOKE_RESULT_DIR/nanodictate-version.txt"
}

# Bounded poll: run "$@" until it exits 0 or $1 seconds elapse. Arg 1 is the
# deadline in seconds, the rest is the command. Returns 0 on success, 1 on
# timeout. Never a bare `sleep N` as the correctness condition.
smoke_poll() {
  local deadline="$1"
  shift
  local waited=0
  while [ "$waited" -lt "$deadline" ]; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

# GUI uid for launchctl gui/<uid> addressing. Prefers the console owner and
# falls back to the current user id (headless runners have no console user).
smoke_gui_uid() {
  local uid
  if uid="$(/usr/bin/stat -f %u /dev/console 2>/dev/null)" && [ -n "$uid" ] && [ "$uid" != "0" ]; then
    printf '%s' "$uid"
  else
    /usr/bin/id -u
  fi
}

# Assert the canonical agent job is live: registered in gui/<uid>, state
# running with a pid, and backed by a real process. Prints "pid=<n>" on
# stdout on success. A registered-but-dead/throttled job (no pid) fails.
smoke_assert_live_job() {
  local label="${1:-com.nanodictate.agent}"
  local uid
  uid="$(smoke_gui_uid)"
  local printout
  smoke_set_check "launchctl print gui/$uid/$label shows a live process"
  printout="$(/bin/launchctl print "gui/$uid/$label" 2>&1)" || \
    smoke_fail "launchctl print gui/$uid/$label failed: $printout"
  local state pid prog
  state="$(printf '%s\n' "$printout" | sed -n 's/^[[:space:]]*state = //p' | head -n1)"
  pid="$(printf '%s\n' "$printout" | sed -n 's/^[[:space:]]*pid = //p' | head -n1)"
  prog="$(printf '%s\n' "$printout" | sed -n 's/^[[:space:]]*program = //p' | head -n1)"
  [ -n "$pid" ] && [ "$pid" != "0" ] || \
    smoke_fail "job $label is registered but has no live pid (state=${state:-unknown}): $printout"
  case "$state" in
    *running*) ;;
    *) smoke_fail "job $label has pid $pid but state is not running (state=${state:-unknown})" ;;
  esac
  ps -p "$pid" -o pid=,comm= >/dev/null 2>&1 || \
    smoke_fail "job $label reports pid $pid but no such process exists"
  smoke_log "live job ok: label=$label pid=$pid state=$state program=${prog:-<unknown>}"
  printf '%s' "$pid" > "$SMOKE_RESULT_DIR/agent-pid.txt"
  printf '%s\n' "$printout" > "$SMOKE_RESULT_DIR/launchctl-print.txt"
  printf 'pid=%s\n' "$pid"
}

# Assert no active gui-domain job and no live agent process remain.
smoke_assert_no_job() {
  local label="${1:-com.nanodictate.agent}"
  local uid
  uid="$(smoke_gui_uid)"
  smoke_set_check "launchctl print gui/$uid/$label fails (service gone)"
  if /bin/launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
    smoke_fail "job $label is still registered after stop/uninstall"
  fi
  if pgrep -x NanoDictateAgent >/dev/null 2>&1; then
    smoke_fail "NanoDictateAgent process is still alive after stop/uninstall"
  fi
  smoke_log "no-job ok: $label absent in gui/$uid and no agent process"
}

# Collect bounded diagnostics into $SMOKE_RESULT_DIR/diagnostics.log. Safe to
# call on failure before teardown: every probe is best-effort (never fails the
# script) and prints no secrets, tokens, full configs or unrelated env vars.
smoke_collect_diagnostics() {
  local dir="$SMOKE_RESULT_DIR"
  mkdir -p "$dir"
  {
    echo "--- sw_vers ---"
    /usr/bin/sw_vers 2>&1 || true
    echo "--- arch ---"
    /usr/bin/arch 2>&1 || true
    /usr/bin/uname -a 2>&1 || true
    echo "--- runner image (osrelease) ---"
    /usr/bin/sw_vers -productVersion 2>&1 || true
    echo "--- package managers ---"
    command -v brew >/dev/null 2>&1 && brew --version 2>&1 || echo "brew: absent"
    command -v port >/dev/null 2>&1 && port version 2>&1 || echo "port: absent"
    echo "--- nanodictate --version ---"
    [ -n "${NANODICTATE_BIN:-}" ] && "$NANODICTATE_BIN" --version 2>&1 || echo "nanodictate: not resolvable"
    echo "--- launchctl print ---"
    /bin/launchctl print "gui/$(smoke_gui_uid)/com.nanodictate.agent" 2>&1 || true
    echo "--- launchctl list (filtered) ---"
    /bin/launchctl list 2>&1 | grep -i -E 'nanodictate|com.nanodictate' || echo "no nanodictate job in list"
    echo "--- plist paths ---"
    ls -l ~/Library/LaunchAgents/com.nanodictate.agent.plist 2>&1 || true
    ls -l /Library/LaunchAgents/com.nanodictate.agent.plist 2>&1 || true
    echo "--- user plist content (first 40 lines) ---"
    head -n 40 ~/Library/LaunchAgents/com.nanodictate.agent.plist 2>&1 || true
    echo "--- global plist content (first 40 lines) ---"
    head -n 40 /Library/LaunchAgents/com.nanodictate.agent.plist 2>&1 || true
    echo "--- processes ---"
    ps aux 2>&1 | grep -i -E 'nanodictate' | grep -v grep || echo "no nanodictate process"
    echo "--- package receipts ---"
    command -v brew >/dev/null 2>&1 && brew list --cask nanodictate 2>&1 || true
    command -v brew >/dev/null 2>&1 && brew list nanodictate 2>&1 || true
    command -v port >/dev/null 2>&1 && port installed nanodictate 2>&1 || true
    echo "--- installed files ---"
    ls -l /Applications/NanoDictate.app 2>&1 || true
    ls -l /opt/local/bin/nanodictate /opt/local/bin/NanoDictateAgent 2>&1 || true
    command -v nanodictate >/dev/null 2>&1 && ls -l "$(command -v nanodictate)" 2>&1 || true
    echo "--- codesign ---"
    [ -d /Applications/NanoDictate.app ] && codesign --verify --deep --strict /Applications/NanoDictate.app 2>&1 || true
    [ -x /opt/local/bin/NanoDictateAgent ] && codesign --verify --strict /opt/local/bin/NanoDictateAgent 2>&1 || true
    command -v nanodictate >/dev/null 2>&1 && codesign -dv --verbose=2 "$(command -v nanodictate 2>/dev/null || echo /nonexistent)" 2>&1 | head -n 20 || true
    echo "--- xattr ---"
    [ -d /Applications/NanoDictate.app ] && xattr -l /Applications/NanoDictate.app 2>&1 || true
    echo "--- agent log tail (bounded, 50 lines) ---"
    tail -n 50 ~/Library/Logs/NanoDictate/agent.log 2>&1 || echo "no agent log"
    echo "--- channel revisions ---"
    [ -d "$(brew --repository 2>/dev/null)/Library/Taps/kodmial/homebrew-nanodictate" ] && git -C "$(brew --repository)/Library/Taps/kodmial/homebrew-nanodictate" rev-parse HEAD 2>&1 || echo "tap rev: n/a"
    [ -d /Users/Shared/macports-nanodictate/.git ] && git -C /Users/Shared/macports-nanodictate rev-parse HEAD 2>&1 || echo "tree rev: n/a"
  } > "$dir/diagnostics.log" 2>&1 || true
  smoke_log "diagnostics written to $dir/diagnostics.log"
}

# Escape a string for inclusion inside a double-quoted JSON string value.
# Handles backslashes, double quotes, and every C0 control character
# (U+0000 through U+001F) so arbitrary metadata values cannot produce invalid
# JSON or alter the decoded value. Short escapes are used for backspace,
# form feed, newline, CR and tab; remaining controls use \u00XX. NUL cannot
# occur in a shell variable (the shell strips it), so it needs no case here.
smoke_json_escape() {
  local str="$1"
  str=${str//\\/\\\\}
  str=${str//\"/\\\"}
  str=${str//$'\b'/\\b}
  str=${str//$'\f'/\\f}
  str=${str//$'\n'/\\n}
  str=${str//$'\r'/\\r}
  str=${str//$'\t'/\\t}
  local out="" i ch code
  local len=${#str}
  for (( i = 0; i < len; i++ )); do
    ch=${str:i:1}
    printf -v code '%d' "'$ch"
    if (( code < 32 )); then
      printf -v ch '\\u%04x' "$code"
    fi
    out+=$ch
  done
  printf '%s' "$out"
}

# Write the machine-readable result record consumed by the incident reporter.
# Args: mode channel expected_version result failed_phase.
smoke_write_metadata() {
  local mode="$1" channel="$2" expected="$3" result="$4" failed_phase="$5"
  local arch macos
  mkdir -p "$SMOKE_RESULT_DIR"
  arch="$(/usr/bin/arch 2>/dev/null || uname -m)"
  macos="$(/usr/bin/sw_vers -productVersion 2>/dev/null || echo unknown)"
  {
    printf '{\n'
    printf '  "mode": "%s",\n' "$(smoke_json_escape "$mode")"
    printf '  "channel": "%s",\n' "$(smoke_json_escape "$channel")"
    printf '  "expected_version": "%s",\n' "$(smoke_json_escape "$expected")"
    printf '  "result": "%s",\n' "$(smoke_json_escape "$result")"
    printf '  "failed_phase": "%s",\n' "$(smoke_json_escape "$failed_phase")"
    printf '  "arch": "%s",\n' "$(smoke_json_escape "$arch")"
    printf '  "macos": "%s",\n' "$(smoke_json_escape "$macos")"
    printf '  "run_id": "%s",\n' "$(smoke_json_escape "${GITHUB_RUN_ID:-local}")"
    printf '  "run_attempt": "%s"\n' "$(smoke_json_escape "${GITHUB_RUN_ATTEMPT:-1}")"
    printf '}\n'
  } > "$SMOKE_RESULT_DIR/smoke-metadata.json"
}
