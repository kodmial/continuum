#!/bin/bash
# homebrew-lifecycle.sh — reusable black-box Homebrew Cask lifecycle smoke.
#
# Covers the real production channel `kodmial/nanodictate` (never the official
# homebrew/cask tree) and, in candidate mode, a temporary Cask derived from the
# production Cask logic with only version/source/SHA adapted to the exact
# CI-built artifact bytes.
#
# Lifecycle: install -> verify files/CLI -> start -> verify live launchd job ->
# duplicate-start idempotency -> stop -> uninstall -> verify package-owned
# cleanup. Persistent user config/logs are never deleted.
#
# Usage:
#   homebrew-lifecycle.sh --mode candidate|production --expected-version VER
#     [--cask-file PATH]        (candidate only: temporary Caskick)
#     [--result-dir DIR]
#
# Exit 0 on full green, non-zero with bounded diagnostics on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/packaging-smoke/common.sh
source "$SCRIPT_DIR/common.sh"

MODE=""
EXPECTED=""
CASK_FILE=""
SMOKE_RESULT_DIR="./smoke-results-homebrew"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --expected-version) EXPECTED="$2"; shift 2 ;;
    --cask-file) CASK_FILE="$2"; shift 2 ;;
    --result-dir) SMOKE_RESULT_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$MODE" = "candidate" ] || [ "$MODE" = "production" ] || { echo "Need --mode candidate|production" >&2; exit 2; }
[ -n "$EXPECTED" ] || { echo "Need --expected-version" >&2; exit 2; }
if [ "$MODE" = "candidate" ] && [ -z "$CASK_FILE" ]; then
  echo "Candidate mode needs --cask-file" >&2
  exit 2
fi

mkdir -p "$SMOKE_RESULT_DIR"
NANODICTATE_BIN="nanodictate"
export NANODICTATE_BIN SMOKE_RESULT_DIR

APP_PATH="/Applications/NanoDictate.app"
USER_PLIST="$HOME/Library/LaunchAgents/com.nanodictate.agent.plist"
LABEL="com.nanodictate.agent"
# Temporary local tap for candidate mode. Current Homebrew rejects arbitrary
# cask file paths (`brew install --cask <path-to-rb>`), so the exact candidate
# bytes are placed in a real tap and installed by token.
CANDIDATE_TAP="nanodictate-candidate/smoke"

cleanup_candidate_tap() {
  brew untap "$CANDIDATE_TAP" >/dev/null 2>&1 || true
}

on_failure() {
  cleanup_candidate_tap || true
  smoke_collect_diagnostics
  smoke_write_metadata "$MODE" "homebrew" "$EXPECTED" "failure" "$SMOKE_PHASE"
  {
    echo "# Homebrew smoke failure ($MODE mode)"
    echo ""
    echo "- Expected version: $EXPECTED"
    echo "- Failed phase: $SMOKE_PHASE"
    echo "- Failed check: ${SMOKE_CHECK:-<none>}"
    echo "- Arch: $(/usr/bin/arch 2>/dev/null || uname -m)"
    echo "- macOS: $(/usr/bin/sw_vers -productVersion 2>/dev/null || echo unknown)"
    echo "- Brew: $(brew --version 2>/dev/null | head -n1 || echo absent)"
    echo "- Tap rev: $(git -C "$(brew --repository 2>/dev/null)/Library/Taps/kodmial/homebrew-nanodictate" rev-parse HEAD 2>/dev/null || echo n/a)"
    echo "- Run: ${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-<repo>}/actions/runs/${GITHUB_RUN_ID:-<id>}"
  } > "$SMOKE_RESULT_DIR/failure-summary.md"
}
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    on_failure || true
  fi
  exit "$rc"
}
trap on_exit EXIT

# --- 1. Precondition: fresh runner has no NanoDictate state -------------------
smoke_phase "precondition"
smoke_set_check "no pre-existing cask install"
if brew list --cask nanodictate >/dev/null 2>&1; then
  smoke_fail "cask nanodictate is already installed on a supposedly fresh runner"
fi
smoke_set_check "no pre-existing app/CLI/service state"
[ ! -d "$APP_PATH" ] || smoke_fail "$APP_PATH already exists on a fresh runner"
if /bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" >/dev/null 2>&1; then
  smoke_fail "$LABEL is already registered on a fresh runner"
fi
if pgrep -x NanoDictateAgent >/dev/null 2>&1; then
  smoke_fail "NanoDictateAgent is already running on a fresh runner"
fi

# --- 2. Install through the real Homebrew Cask mechanism ----------------------
smoke_phase "install"
if [ "$MODE" = "production" ]; then
  smoke_set_check "brew install --cask kodmial/nanodictate/nanodictate"
  brew install --cask kodmial/nanodictate/nanodictate
else
  smoke_set_check "temporary local tap carrying the exact candidate cask bytes"
  [ -f "$CASK_FILE" ] || smoke_fail "candidate cask file missing: $CASK_FILE"
  # A fresh runner never carries the throwaway tap; drop it first so a retry
  # on a reused machine starts clean.
  brew untap "$CANDIDATE_TAP" >/dev/null 2>&1 || true
  brew tap-new "$CANDIDATE_TAP" >/dev/null || smoke_fail "brew tap-new $CANDIDATE_TAP failed"
  TAP_PATH="$(brew --repo "$CANDIDATE_TAP")" || smoke_fail "brew --repo $CANDIDATE_TAP failed"
  mkdir -p "$TAP_PATH/Casks" || smoke_fail "could not create $TAP_PATH/Casks"
  # Preserve the exact candidate bytes: copy only, never regenerate or edit.
  cp "$CASK_FILE" "$TAP_PATH/Casks/nanodictate.rb" || smoke_fail "could not stage the candidate cask in $TAP_PATH/Casks"
  smoke_set_check "brew install --cask $CANDIDATE_TAP/nanodictate"
  brew install --cask "$CANDIDATE_TAP/nanodictate"
fi

# --- 3. Package verification ---------------------------------------------------
smoke_phase "verify-package"
smoke_set_check "brew reports the cask installed"
brew list --cask nanodictate >/dev/null || smoke_fail "brew list --cask nanodictate failed"
smoke_set_check "NanoDictate.app exists in the app directory"
[ -d "$APP_PATH" ] || smoke_fail "$APP_PATH missing after install"
smoke_set_check "nanodictate CLI resolves to the installed package"
command -v nanodictate >/dev/null || smoke_fail "nanodictate not on PATH after install"
CLI_REAL="$(readlink "$(command -v nanodictate)" 2>/dev/null || command -v nanodictate)"
case "$CLI_REAL" in
  *NanoDictate.app*|*homebrew*|*brew*) smoke_log "CLI resolves to installed package: $CLI_REAL" ;;
  *) smoke_fail "CLI resolves outside the installed package: $CLI_REAL" ;;
esac
smoke_assert_version "$EXPECTED"
smoke_set_check "nanodictate --help exits 0"
nanodictate --help >/dev/null 2>&1 || smoke_fail "nanodictate --help exited non-zero"
smoke_set_check "codesign --verify --deep --strict succeeds"
codesign --verify --deep --strict "$APP_PATH" || smoke_fail "codesign verification failed"
smoke_set_check "quarantine behavior matches the production cask contract"
if xattr -p com.apple.quarantine "$APP_PATH" >/dev/null 2>&1; then
  smoke_fail "com.apple.quarantine is still present: the cask postflight must strip it"
fi
smoke_log "package verification ok"

# --- 4. Start and live-service verification ------------------------------------
smoke_phase "start"
smoke_set_check "nanodictate start"
nanodictate start || smoke_fail "nanodictate start failed"

smoke_phase "launchd-health"
smoke_set_check "poll until healthy (deadline 90s)"
smoke_poll 90 nanodictate status || smoke_fail "nanodictate status never succeeded within 90s"
smoke_assert_live_job "$LABEL" >/dev/null
AGENT_PID="$(cat "$SMOKE_RESULT_DIR/agent-pid.txt")"
smoke_set_check "registered executable resolves to the installed package"
PRINTOUT="$(/bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" 2>&1)" || \
  smoke_fail "launchctl print failed right after live check"
case "$PRINTOUT" in
  *NanoDictate.app*) smoke_log "executable resolves to the installed bundle" ;;
  *) smoke_fail "registered program does not point at the installed bundle: $PRINTOUT" ;;
esac
ps -p "$AGENT_PID" -o comm= 2>/dev/null | grep -q NanoDictateAgent || \
  smoke_fail "pid $AGENT_PID is not a NanoDictateAgent process"

# --- 5. Duplicate-start idempotency: still one canonical service ---------------
smoke_phase "duplicate-start"
smoke_set_check "second nanodictate start keeps a single service"
nanodictate start || smoke_fail "second nanodictate start failed"
COUNT="$(/bin/launchctl list 2>/dev/null | grep -c "$LABEL" || true)"
[ "$COUNT" -le 1 ] || smoke_fail "duplicate daemon: $COUNT jobs carry $LABEL"
smoke_assert_live_job "$LABEL" >/dev/null
smoke_log "idempotency ok (one canonical service)"

# --- 6. Stop -------------------------------------------------------------------
smoke_phase "stop"
smoke_set_check "nanodictate stop"
nanodictate stop || smoke_fail "nanodictate stop failed"
smoke_set_check "poll until the gui-domain service is gone (deadline 60s)"
STOPPED=0
for _ in $(seq 1 30); do
  if ! /bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" >/dev/null 2>&1; then
    STOPPED=1
    break
  fi
  sleep 2
done
[ "$STOPPED" = "1" ] || smoke_fail "service still registered 60s after stop"

# --- 7. Uninstall through the real brew path ------------------------------------
smoke_phase "uninstall"
smoke_set_check "brew uninstall --cask nanodictate"
brew uninstall --cask nanodictate || smoke_fail "brew uninstall --cask failed"
# Documented manual residue: the cask leaves the canonical plist behind, and a
# leftover plist pointing at the now-removed bundle would try to start a
# missing binary at next login. Remove it when it references the removed
# install (user config under ~/.config is never touched).
if [ -f "$USER_PLIST" ]; then
  if grep -q "NanoDictate" "$USER_PLIST" 2>/dev/null; then
    /bin/launchctl bootout "gui/$(smoke_gui_uid)/$LABEL" >/dev/null 2>&1 || true
    rm -f "$USER_PLIST"
    smoke_log "removed stale package-owned LaunchAgent plist $USER_PLIST"
  fi
fi
if [ "$MODE" = "candidate" ]; then
  smoke_set_check "remove the temporary local tap"
  cleanup_candidate_tap
  smoke_log "temporary candidate tap removed"
fi

# --- 8. Package-owned cleanup verification --------------------------------------
smoke_phase "verify-cleanup"
smoke_set_check "cask no longer installed"
brew list --cask nanodictate >/dev/null 2>&1 && smoke_fail "cask still reported installed after uninstall"
smoke_set_check "app removed"
[ ! -d "$APP_PATH" ] || smoke_fail "$APP_PATH still exists after uninstall"
smoke_set_check "CLI link removed"
# Bash hashes command locations: after `brew uninstall` removes the binary
# symlink, `command -v` still reports the stale hashed path until the table
# is cleared (reproduced locally). Drop the hash so the check probes the
# real PATH instead of failing on a removed link.
hash -r 2>/dev/null || true
if command -v nanodictate >/dev/null 2>&1; then
  smoke_fail "nanodictate still on PATH after uninstall: $(command -v nanodictate)"
fi
smoke_assert_no_job "$LABEL"
smoke_set_check "no stale package-owned LaunchAgent state for a missing binary"
if [ -f "$USER_PLIST" ] && grep -q "NanoDictate" "$USER_PLIST" 2>/dev/null; then
  smoke_fail "stale package-owned plist $USER_PLIST still references the removed install"
fi
smoke_log "cleanup ok; user config/logs under ~/.config and ~/Library/Logs were preserved (never deleted)"

smoke_write_metadata "$MODE" "homebrew" "$EXPECTED" "success" ""
smoke_log "HOMEBREW LIFECYCLE GREEN ($MODE, expected $EXPECTED)"
