#!/usr/bin/env bash
set -euo pipefail
started="$SECONDS"
root="$HOME/.cache/continuum/toolchains"
mkdir -p "$root"
all_cached=true
valid_bin() {
  local binary="$1" stamp="$2" version="$3"
  test -x "$binary" && test -f "$stamp" || return 1
  (cd "$(dirname "$binary")" && sha256sum -c "$(basename "$stamp")" >/dev/null) || return 1
  local got
  got="$("$binary" --version 2>&1)" || return 1
  local safe="${version//./\\.}"
  [[ "$got" =~ (^|[^0-9.])${safe}([^0-9.]|$) ]] || return 1
}
if [[ "$CONTINUUM_COMPONENTS" == opencode || "$CONTINUUM_COMPONENTS" == both ]]; then
  location="$root/opencode/$CONTINUUM_OPENCODE_VERSION/bin"
  exe="$location/opencode"
  stamp="$location/opencode.sha256"
  if valid_bin "$exe" "$stamp" "$CONTINUUM_OPENCODE_VERSION"; then
    echo "Continuum OpenCode verified cache hit: $CONTINUUM_OPENCODE_VERSION"
  else
    all_cached=false
    rm -rf "$location"
    mkdir -p "$location"
    case "$RUNNER_OS/$RUNNER_ARCH" in
      Linux/X64) asset="opencode-linux-x64.tar.gz" ;;
      Linux/ARM64) asset="opencode-linux-arm64.tar.gz" ;;
      *) echo "::error::Unsupported OpenCode platform" >&2; exit 2 ;;
    esac
    tag="v$CONTINUUM_OPENCODE_VERSION"
    release="https://api.github.com/repos/anomalyco/opencode/releases/tags/$tag"
    metadata="$(mktemp)"
    archive="$(mktemp)"
    trap 'rm -f "$metadata" "$archive"' EXIT
    if [[ -z "${CONTINUUM_GITHUB_TOKEN:-}" ]]; then
      echo "::error::GitHub workflow token is required for authenticated release metadata" >&2
      exit 2
    fi
    if ! curl -fSL --retry 3 --retry-delay 2 \
      --header "Authorization: Bearer $CONTINUUM_GITHUB_TOKEN" \
      --header "Accept: application/vnd.github+json" \
      "$release" -o "$metadata"; then
      echo "::error::Failed to fetch authenticated OpenCode release metadata" >&2
      exit 1
    fi
    if ! digest="$(python3 - "$metadata" "$asset" <<'PY'
import json,re,sys
data=json.load(open(sys.argv[1]))
matches=[a for a in data.get("assets",[]) if a.get("name")==sys.argv[2]]
if len(matches)!=1 or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}",str(matches[0].get("digest",""))):
    raise SystemExit("Missing verified GitHub release-asset digest")
print(matches[0]["digest"].split(":",1)[1])
PY
)"; then
      echo "::error::Cannot verify OpenCode release asset digest" >&2
      exit 1
    fi
    echo "OpenCode $tag: downloading the pinned release asset $asset on cache miss"
    curl -fSL --retry 3 --retry-delay 2 "https://github.com/anomalyco/opencode/releases/download/$tag/$asset" -o "$archive"
    echo "$digest  $archive" | sha256sum -c - >/dev/null || {
      echo "::error::OpenCode official release asset SHA256 mismatch" >&2; exit 1;
    }
    # Extract only the known single binary; never unpack untrusted tar paths.
    if tar -tzf "$archive" | grep -qx 'opencode'; then
      tar -xOzf "$archive" opencode > "$exe"
    elif tar -tzf "$archive" | grep -qx './opencode'; then
      tar -xOzf "$archive" ./opencode > "$exe"
    else
      echo "::error::Pinned OpenCode archive does not contain expected binary" >&2
      exit 1
    fi
    chmod 755 "$exe"
    "$exe" --version | grep -Fx "$CONTINUUM_OPENCODE_VERSION" >/dev/null || {
      echo "::error::Installed OpenCode has unexpected version" >&2; exit 1;
    }
    (cd "$location" && sha256sum opencode > opencode.sha256)
  fi
  echo "$location" >> "$GITHUB_PATH"
  export PATH="$location:$PATH"
fi
if [[ "$CONTINUUM_COMPONENTS" == pr-agent || "$CONTINUUM_COMPONENTS" == both ]]; then
  location="$root/pr-agent/$CONTINUUM_PR_AGENT_VERSION/venv"
  exe="$location/bin/pr-agent"
  stamp="$location/bin/pr-agent.sha256"
  if valid_bin "$exe" "$stamp" "$CONTINUUM_PR_AGENT_VERSION"; then
    echo "Continuum PR-Agent verified cache hit: $CONTINUUM_PR_AGENT_VERSION"
  else
    all_cached=false
    rm -rf "$location"
    python3 -m venv "$location"
    # The top-level release wheel is explicitly trusted by its reviewed PyPI SHA256.
    # Overrides require a reviewed wheel hash in the checked-in action manifest.
    wheel_digest="$(python3 - "$GITHUB_ACTION_PATH/toolchain.json" "$CONTINUUM_PR_AGENT_VERSION" <<'PY'
import json,re,sys
pins=json.load(open(sys.argv[1])).get("pr_agent_wheel_sha256",{})
digest=pins.get(sys.argv[2],"")
if not re.fullmatch(r"[0-9a-f]{64}",digest):
    raise SystemExit("PR-Agent release wheel SHA256 not pinned for version "+sys.argv[2])
print(digest)
PY
)" || {
      echo "::error::PR-Agent wheel digest is missing or invalid for pinned version" >&2
      exit 2
    }
    wheel_dir="$(mktemp -d)"
    if ! "$location/bin/python" -m pip download --disable-pip-version-check --quiet \
      --no-deps --only-binary=:all: --dest "$wheel_dir" \
      "pr-agent==$CONTINUUM_PR_AGENT_VERSION"; then
      rm -rf "$wheel_dir"
      echo "::error::Failed to download pinned PR-Agent release wheel" >&2
      exit 1
    fi
    wheel="$wheel_dir/pr_agent-$CONTINUUM_PR_AGENT_VERSION-py3-none-any.whl"
    if [[ ! -f "$wheel" ]] || ! echo "$wheel_digest  $wheel" | sha256sum -c - >/dev/null; then
      rm -rf "$wheel_dir"
      echo "::error::PR-Agent official PyPI wheel SHA256 mismatch" >&2
      exit 1
    fi
    # Dependencies are installed by pip after top-level wheel verification.
    "$location/bin/python" -m pip install --disable-pip-version-check --quiet "$wheel" || {
      rm -rf "$wheel_dir"
      echo "::error::PR-Agent dependency installation failed" >&2
      exit 1
    }
    rm -rf "$wheel_dir"
    "$exe" --version 2>&1 | grep -F "$CONTINUUM_PR_AGENT_VERSION" >/dev/null || {
      echo "::error::PR-Agent installed executable version mismatch" >&2; exit 1;
    }
    (cd "$location/bin" && sha256sum pr-agent > pr-agent.sha256)
  fi
  echo "$location/bin" >> "$GITHUB_PATH"
  export PATH="$location/bin:$PATH"
fi
if [[ "$all_cached" == true && "$CONTINUUM_CACHED_RESTORE" == true ]]; then
  echo 'cache_hit=true' >> "$GITHUB_OUTPUT"
else
  echo 'cache_hit=false' >> "$GITHUB_OUTPUT"
fi
printf 'Continuum toolchain ready after %ss (restored=%s; network-install=%s).\n' "$((SECONDS-started))" "$CONTINUUM_CACHED_RESTORE" "$([[ "$all_cached" == true ]] && echo no || echo yes)"
