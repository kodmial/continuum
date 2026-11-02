#!/usr/bin/env bash
set -euo pipefail

ruby -E UTF-8 scripts/test-continuum.rb
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m continuum.trigger_manifest --check

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"

# Changed-line coverage gate for the Python engine. coverage.py is optional in
# a consumer environment: when it is absent the suite still runs unchanged.
# A missing base ref is reported explicitly instead of silently passing.
coverage_base="${CONTINUUM_COVERAGE_BASE:-origin/main}"
if python3 -c 'import coverage' >/dev/null 2>&1; then
  if git rev-parse --verify --quiet "${coverage_base}^{commit}" >/dev/null; then
    coverage_file="$PWD/.opencode-tmp/.coverage"
    rm -f "$coverage_file" "$PWD/.opencode-tmp/coverage.json"
    COVERAGE_FILE="$coverage_file" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
      python3 -m coverage run --source=src -m unittest discover -s tests
    COVERAGE_FILE="$coverage_file" python3 -m coverage json \
      -o "$PWD/.opencode-tmp/coverage.json" >/dev/null
    python3 - "$coverage_base" "$PWD/.opencode-tmp/coverage.json" <<'COVERAGE_PY'
import json
import re
import subprocess
import sys

base, report_path = sys.argv[1], sys.argv[2]
merge_base = subprocess.check_output(
    ["git", "merge-base", base, "HEAD"], text=True
).strip()
diff = subprocess.check_output(
    ["git", "diff", "-U0", merge_base, "HEAD", "--", "src"], text=True
)
changed = {}
current = None
for line in diff.splitlines():
    if line.startswith("+++ b/"):
        current = line[len("+++ b/"):]
        changed.setdefault(current, set())
    elif current and line.startswith("@@"):
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            changed[current].update(range(start, start + count))

with open(report_path, encoding="utf-8") as handle:
    report = json.load(handle)
executed = 0
measured = 0
for path, lines in changed.items():
    entry = report["files"].get(path)
    if not entry:
        continue
    runnable = set(entry["executed_lines"]) | set(entry["missing_lines"])
    considered = lines & runnable
    measured += len(considered)
    executed += len(considered & set(entry["executed_lines"]))
if measured == 0:
    print("coverage: no executable changed Python lines")
else:
    percent = 100.0 * executed / measured
    print(f"coverage: {executed}/{measured} changed Python lines = {percent:.1f}%")
    if percent < 80.0:
        sys.exit("coverage: changed-line coverage below the 80% threshold")
COVERAGE_PY
  else
    echo "coverage: base '$coverage_base' is unavailable; skipping changed-line gate" >&2
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
  fi
else
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
fi
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

mkdir -p "$PWD/.opencode-tmp/bin"
# Pin both the installer URL and its content: the tag can move and a
# compromised download would otherwise execute with this script's privileges.
actionlint_installer="$PWD/.opencode-tmp/download-actionlint.bash"
actionlint_installer_sha256=72fa3e45ac20f3c3a512d6747b4fcf719e21f890e8c43e78d48a41fdfb900c4e
curl -fsSL --retry 3 -o "$actionlint_installer" \
  https://raw.githubusercontent.com/rhysd/actionlint/914e7df21a07ef503a81201c76d2b11c789d3fca/scripts/download-actionlint.bash
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s  %s\n' "$actionlint_installer_sha256" "$actionlint_installer" | sha256sum -c -
else
  actual_sha256="$(shasum -a 256 "$actionlint_installer" | awk '{print $1}')"
  if [[ "$actual_sha256" != "$actionlint_installer_sha256" ]]; then
    echo "actionlint installer checksum mismatch" >&2
    exit 1
  fi
fi
bash "$actionlint_installer" 1.7.12 "$PWD/.opencode-tmp/bin"
.opencode-tmp/bin/actionlint -shellcheck= -pyflakes= \
  .github/workflows/*.yml \
  .github/caller-stubs/*.yml \
  .github/caller-stubs/tech/*.yml \
  .github/caller-stubs/parent/*.yml
