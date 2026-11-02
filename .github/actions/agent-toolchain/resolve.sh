#!/usr/bin/env bash
set -euo pipefail
# Source only the checked-out, versioned Continuum action manifest.
manifest="$GITHUB_ACTION_PATH/toolchain.json"
test -f "$manifest"
case "$RUNNER_OS/$RUNNER_ARCH" in
  Linux/X64|Linux/ARM64) ;;
  *) echo "::error::Cached Continuum toolchain is not qualified for $RUNNER_OS/$RUNNER_ARCH"; exit 2 ;;
esac
case "$CONTINUUM_COMPONENTS" in
  opencode|pr-agent|both) ;;
  *) echo "::error::Invalid toolchain component selection"; exit 2 ;;
esac
resolve_version() {
  local override="$1" field="$2"
  local resolved="$override"
  if [[ -z "$resolved" ]]; then
    resolved="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$manifest" "$field")"
  fi
  if ! [[ "$resolved" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "::error::Invalid exact SemVer: $field" >&2
    return 2
  fi
  printf '%s' "$resolved"
}
oc="$(resolve_version "$CONTINUUM_OPENCODE_VERSION_OVERRIDE" opencode_version)"
pa="$(resolve_version "$CONTINUUM_PR_AGENT_VERSION_OVERRIDE" pr_agent_version)"
epoch="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cache_epoch"])' "$manifest")"
if ! [[ "$epoch" =~ ^[a-zA-Z0-9-]+$ ]]; then exit 2; fi
python_tag="$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
if [[ "$CONTINUUM_COMPONENTS" != "opencode" && "$python_tag" != "3.12" ]]; then
  echo "::error::PR-Agent venv requires Python 3.12; got $python_tag" >&2
  exit 2
fi
{
  echo "opencode_version=$oc"
  echo "pr_agent_version=$pa"
  echo "cache_key=continuum-toolchains-$epoch-$RUNNER_OS-$RUNNER_ARCH-py$python_tag-$CONTINUUM_COMPONENTS-oc$oc-pa$pa"
} >> "$GITHUB_OUTPUT"
