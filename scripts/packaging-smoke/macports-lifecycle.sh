#!/bin/bash
# macports-lifecycle.sh — reusable black-box MacPorts lifecycle smoke.
#
# Covers the real project channel `kodmial/macports-nanodictate` wired by
# scripts/install-macports.sh (never the official MacPorts ports tree) and, in
# candidate mode, a temporary local ports tree containing the production
# Portfile logic adapted only to the exact CI-built artifact/checksum.
#
# Lifecycle: establish ports source -> install via the real `port` path ->
# verify files/CLI -> verify post-activate service WITHOUT a prior manual
# start -> stop via the documented CLI lifecycle -> uninstall via
# `sudo port uninstall` -> verify package-owned cleanup. Per-user config/logs
# are never deleted.
#
# Usage:
#   macports-lifecycle.sh --mode candidate|production --expected-version VER
#     [--ports-dir DIR]       (candidate only: temp tree holding audio/nanodictate/Portfile)
#     [--dist-dir DIR]        (candidate only: dir holding the exact tarball, for file:// master_sites)
#     [--result-dir DIR]
#
# Exit 0 on full green, non-zero with bounded diagnostics on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/packaging-smoke/common.sh
source "$SCRIPT_DIR/common.sh"

MODE=""
EXPECTED=""
PORTS_DIR=""
DIST_DIR=""
SMOKE_RESULT_DIR="./smoke-results-macports"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --expected-version) EXPECTED="$2"; shift 2 ;;
    --ports-dir) PORTS_DIR="$2"; shift 2 ;;
    --dist-dir) DIST_DIR="$2"; shift 2 ;;
    --result-dir) SMOKE_RESULT_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$MODE" = "candidate" ] || [ "$MODE" = "production" ] || { echo "Need --mode candidate|production" >&2; exit 2; }
[ -n "$EXPECTED" ] || { echo "Need --expected-version" >&2; exit 2; }
if [ "$MODE" = "candidate" ] && { [ -z "$PORTS_DIR" ] || [ -z "$DIST_DIR" ]; }; then
  echo "Candidate mode needs --ports-dir and --dist-dir" >&2
  exit 2
fi

mkdir -p "$SMOKE_RESULT_DIR"
PREFIX="/opt/local"
NANODICTATE_BIN="$PREFIX/bin/nanodictate"
export NANODICTATE_BIN SMOKE_RESULT_DIR PATH="$PREFIX/bin:$PREFIX/sbin:$PATH"

GLOBAL_PLIST="/Library/LaunchAgents/com.nanodictate.agent.plist"
LABEL="com.nanodictate.agent"
SOURCES_CONF="$PREFIX/etc/macports/sources.conf"
CANDIDATE_SOURCE="file://$PORTS_DIR"
SOURCES_BACKUP=""
STAGE_BASE=""

on_failure() {
  cleanup_staging || true
  smoke_collect_diagnostics
  smoke_write_metadata "$MODE" "macports" "$EXPECTED" "failure" "$SMOKE_PHASE"
  {
    echo "# MacPorts smoke failure ($MODE mode)"
    echo ""
    echo "- Expected version: $EXPECTED"
    echo "- Failed phase: $SMOKE_PHASE"
    echo "- Failed check: ${SMOKE_CHECK:-<none>}"
    echo "- Arch: $(/usr/bin/arch 2>/dev/null || uname -m)"
    echo "- macOS: $(/usr/bin/sw_vers -productVersion 2>/dev/null || echo unknown)"
    echo "- Port: $(port version 2>/dev/null || echo absent)"
    echo "- Tree rev: $(git -C /Users/Shared/macports-nanodictate rev-parse HEAD 2>/dev/null || echo n/a)"
    echo "- Run: ${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-<repo>}/actions/runs/${GITHUB_RUN_ID:-<id>}"
  } > "$SMOKE_RESULT_DIR/failure-summary.md"
}

on_exit() {
  local rc=$?
  restore_sources || true
  if [ "$rc" -ne 0 ]; then
    on_failure || true
  fi
  exit "$rc"
}
trap on_exit EXIT

restore_sources() {
  if [ -n "$SOURCES_BACKUP" ] && [ -f "$SOURCES_BACKUP" ]; then
    sudo cp "$SOURCES_BACKUP" "$SOURCES_CONF"
    smoke_log "restored $SOURCES_CONF from backup"
  fi
}

cleanup_staging() {
  if [ -n "$STAGE_BASE" ] && [ -d "$STAGE_BASE" ]; then
    rm -rf "$STAGE_BASE"
    smoke_log "removed candidate staging $STAGE_BASE"
  fi
}

# --- 0. Supported MacPorts distribution from the official source ---------------
smoke_phase "macports-base"
smoke_set_check "supported MacPorts installed (pinned, signature-verified)"
"$SCRIPT_DIR/ensure-macports.sh"
port version || smoke_fail "port is not functional"

# --- 1. Establish the mode-appropriate ports source -----------------------------
smoke_phase "ports-source"
if [ "$MODE" = "production" ]; then
  smoke_set_check "documented project installer path (curl install-macports.sh | bash)"
  TMP_INSTALLER="$(mktemp /tmp/install-macports.XXXXXX)"
  curl -fsSL https://raw.githubusercontent.com/kodmial/nanodictate/main/scripts/install-macports.sh -o "$TMP_INSTALLER"
  RC=0
  bash "$TMP_INSTALLER" || RC=$?
  rm -f "$TMP_INSTALLER"
  [ "$RC" -eq 0 ] || smoke_fail "install-macports.sh exited $RC"
else
  smoke_set_check "temporary local ports tree with the production Portfile logic"
  [ -f "$PORTS_DIR/audio/nanodictate/Portfile" ] || smoke_fail "candidate Portfile missing in $PORTS_DIR"
  # Stage the candidate tree and tarballs outside the runner home. `sudo port`
  # drops privileges to the `macports` user while reading the Portfile and
  # fetching the file:// distfiles, and that user cannot read back into a
  # home directory current runner images lock down: arm64 `macos-15` fails
  # with `Permission denied` opening the Portfile while the older Intel
  # image happens to allow it. /tmp stays traversable for every user.
  STAGE_BASE="$(mktemp -d /tmp/nanodictate-smoke.XXXXXX)"
  STAGE_PORTS="$STAGE_BASE/ports"
  STAGE_DIST="$STAGE_BASE/dist"
  mkdir -p "$STAGE_PORTS" "$STAGE_DIST"
  cp -R "$PORTS_DIR/." "$STAGE_PORTS/"
  cp "$DIST_DIR"/nanodictate-*.tar.gz "$STAGE_DIST/"
  # The staged Portfile baked a file:// master_sites pointing at the original
  # dist dir; repoint the staged copy at the staged dist dir.
  python3 - "$STAGE_PORTS/audio/nanodictate/Portfile" "$STAGE_DIST" <<'EOF'
import sys
path, dist = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True)
for i, line in enumerate(lines):
    if line.split()[:1] == ['master_sites']:
        lines[i] = 'master_sites        file://' + dist + '\n'
        break
else:
    raise SystemExit("staged Portfile lost its master_sites line")
open(path, 'w').write(''.join(lines))
EOF
  chmod -R a+rX "$STAGE_BASE"
  PORTS_DIR="$STAGE_PORTS"
  DIST_DIR="$STAGE_DIST"
  CANDIDATE_SOURCE="file://$PORTS_DIR"
  # The candidate file:// source must be registered BEFORE lint/test/install:
  # MacPorts resolves `nanodictate` through sources.conf, otherwise it reports
  # `Port nanodictate not found`.
  SOURCES_BACKUP="$(mktemp /tmp/sources.conf.XXXXXX)"
  sudo cp "$SOURCES_CONF" "$SOURCES_BACKUP"
  if ! grep -qxF "$CANDIDATE_SOURCE" "$SOURCES_CONF" 2>/dev/null; then
    if grep -qE '^[^#].*\[default\]' "$SOURCES_CONF" 2>/dev/null; then
      sudo sed -i "" "/^[^#].*\[default\]/i\\
$CANDIDATE_SOURCE
" "$SOURCES_CONF"
    else
      echo "$CANDIDATE_SOURCE" | sudo tee -a "$SOURCES_CONF" >/dev/null
    fi
  fi
  smoke_set_check "PortIndex for the candidate tree"
  (cd "$PORTS_DIR" && portindex) || smoke_fail "portindex failed on the candidate tree"
  smoke_set_check "port lint --nitpick on the candidate Portfile"
  port lint --nitpick nanodictate 2>&1 | tee "$SMOKE_RESULT_DIR/port-lint.log" || \
    smoke_fail "port lint --nitpick failed"
  smoke_set_check "sudo port install nanodictate from the candidate tree"
  sudo port install nanodictate || smoke_fail "port install nanodictate failed"
  smoke_set_check "port test nanodictate (existing test phase)"
  sudo port test nanodictate 2>&1 | tee "$SMOKE_RESULT_DIR/port-test.log" || \
    smoke_fail "port test nanodictate failed"
fi

# --- 2. Package verification ----------------------------------------------------
smoke_phase "verify-package"
smoke_set_check "MacPorts registry reports nanodictate installed/active"
port installed nanodictate 2>&1 | tee "$SMOKE_RESULT_DIR/port-installed.txt" | grep -q -E 'nanodictate.*active' || \
  smoke_fail "port installed does not show an active nanodictate"
smoke_set_check "CLI and agent files exist under the MacPorts prefix"
[ -x "$PREFIX/bin/nanodictate" ] || smoke_fail "$PREFIX/bin/nanodictate missing"
[ -x "$PREFIX/bin/NanoDictateAgent" ] || smoke_fail "$PREFIX/bin/NanoDictateAgent missing"
smoke_assert_version "$EXPECTED"
smoke_set_check "nanodictate --help exits 0"
"$NANODICTATE_BIN" --help >/dev/null 2>&1 || smoke_fail "nanodictate --help exited non-zero"
smoke_set_check "signature verification succeeds"
codesign --verify --strict "$PREFIX/bin/nanodictate" || smoke_fail "codesign failed for CLI"
codesign --verify --strict "$PREFIX/bin/NanoDictateAgent" || smoke_fail "codesign failed for agent"

# --- 3. post-activate service: NO manual start before this assertion ------------
smoke_phase "post-activate-health"
smoke_set_check "poll post-activate service with a bounded deadline (120s)"
POST_OK=0
for _ in $(seq 1 60); do
  if /bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" >/dev/null 2>&1; then
    POST_OK=1
    break
  fi
  sleep 2
done
[ "$POST_OK" = "1" ] || smoke_fail "post-activate never registered $LABEL within 120s (the Portfile post-activate behavior may be broken)"
smoke_assert_live_job "$LABEL" >/dev/null
AGENT_PID="$(cat "$SMOKE_RESULT_DIR/agent-pid.txt")"
smoke_set_check "executable path points to the installed MacPorts agent"
PRINTOUT="$(/bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" 2>&1)" || \
  smoke_fail "launchctl print failed right after live check"
case "$PRINTOUT" in
  */opt/local/bin/NanoDictateAgent*) smoke_log "executable points at the MacPorts agent" ;;
  *) smoke_fail "registered program does not point at $PREFIX/bin/NanoDictateAgent: $PRINTOUT" ;;
esac
smoke_set_check "CLI status agrees with the healthy service"
"$NANODICTATE_BIN" status >/dev/null 2>&1 || smoke_fail "nanodictate status failed while the launchd job is live"
ps -p "$AGENT_PID" -o comm= 2>/dev/null | grep -q NanoDictateAgent || \
  smoke_fail "pid $AGENT_PID is not a NanoDictateAgent process"

# --- 4. Stop via the documented lifecycle ----------------------------------------
smoke_phase "stop"
smoke_set_check "nanodictate stop"
"$NANODICTATE_BIN" stop || smoke_fail "nanodictate stop failed"
smoke_set_check "poll until the service is gone (deadline 60s)"
STOPPED=0
for _ in $(seq 1 30); do
  if ! /bin/launchctl print "gui/$(smoke_gui_uid)/$LABEL" >/dev/null 2>&1; then
    STOPPED=1
    break
  fi
  sleep 2
done
[ "$STOPPED" = "1" ] || smoke_fail "service still registered 60s after stop"

# --- 5. Uninstall through the real port path --------------------------------------
smoke_phase "uninstall"
smoke_set_check "sudo port uninstall nanodictate"
sudo port uninstall nanodictate || smoke_fail "port uninstall failed"
restore_sources
cleanup_staging

# --- 6. Cleanup verification ------------------------------------------------------
smoke_phase "verify-cleanup"
smoke_set_check "port no longer installed/active"
if port installed nanodictate 2>/dev/null | grep -q -E 'nanodictate.*active'; then
  smoke_fail "port still reports nanodictate active after uninstall"
fi
smoke_set_check "CLI/agent files absent from the prefix"
[ ! -e "$PREFIX/bin/nanodictate" ] || smoke_fail "$PREFIX/bin/nanodictate still exists after uninstall"
[ ! -e "$PREFIX/bin/NanoDictateAgent" ] || smoke_fail "$PREFIX/bin/NanoDictateAgent still exists after uninstall"
smoke_set_check "package-owned global plist absent"
[ ! -e "$GLOBAL_PLIST" ] || smoke_fail "$GLOBAL_PLIST still exists after uninstall (package-owned by the Portfile)"
smoke_assert_no_job "$LABEL"
smoke_log "cleanup ok; per-user config/logs were preserved (never deleted)"

smoke_write_metadata "$MODE" "macports" "$EXPECTED" "success" ""
smoke_log "MACPORTS LIFECYCLE GREEN ($MODE, expected $EXPECTED)"
