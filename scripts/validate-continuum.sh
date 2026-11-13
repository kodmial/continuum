#!/usr/bin/env bash
set -euo pipefail

ruby -E UTF-8 scripts/test-continuum.rb
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m continuum.trigger_manifest --check

mkdir -p .opencode-tmp
export TMPDIR="$PWD/.opencode-tmp"

# Changed-line coverage gate for the Python engine. The gate is mandatory: a
# shallow checkout or a missing coverage.py must fail loudly instead of
# turning the check into an inert skip.
coverage_base="${CONTINUUM_COVERAGE_BASE:-origin/main}"
if ! git rev-parse --verify --quiet "${coverage_base}^{commit}" >/dev/null; then
  if [[ "$coverage_base" == origin/* ]]; then
    base_branch="${coverage_base#origin/}"
    echo "coverage: base '$coverage_base' is missing; fetching it from origin" >&2
    if ! git fetch --no-tags origin \
      "+refs/heads/${base_branch}:refs/remotes/origin/${base_branch}" >&2; then
      echo "coverage: cannot fetch base '$coverage_base' from origin; refusing to skip the changed-line gate" >&2
      exit 1
    fi
  fi
fi
if ! git rev-parse --verify --quiet "${coverage_base}^{commit}" >/dev/null; then
  echo "coverage: base '$coverage_base' is unavailable; a shallow checkout cannot prove the changed-line gate. Fetch full history (actions/checkout fetch-depth: 0) or set CONTINUUM_COVERAGE_BASE to a reachable base commit." >&2
  exit 1
fi
if ! python3 -c 'import coverage' >/dev/null 2>&1; then
  echo "coverage: coverage.py is required for the changed-line gate; installing it" >&2
  if ! python3 -m pip install --quiet --disable-pip-version-check 'coverage>=7' >&2; then
    echo "coverage: cannot install coverage.py; refusing to skip the changed-line gate" >&2
    exit 1
  fi
fi
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
try:
    merge_base = subprocess.check_output(
        ["git", "merge-base", base, "HEAD"], text=True, stderr=subprocess.PIPE
    ).strip()
except subprocess.CalledProcessError as error:
    sys.exit(
        f"coverage: cannot compute merge-base for {base} and HEAD; "
        "fetch the full history (actions/checkout fetch-depth: 0)"
        + (f": {error.stderr.strip()}" if error.stderr else "")
    )
# Diff the merge base against the working tree, not HEAD: validation runs on
# uncommitted changes too, and a HEAD-only diff would silently measure the
# committed engine instead of the code coverage actually executed below.
diff = subprocess.check_output(
    ["git", "diff", "-U0", merge_base, "--", "src"], text=True
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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s .github/scripts -p 'test_delegation_runtime.py'

# Print the shared CodeRabbit chat-quota regression by name in the exact-HEAD
# gate log. CodeRabbit excludes tests/** from review, so the named result is
# the only reviewable evidence that a heading-only 90-minute reply on one PR
# still blocks a chat send from another PR.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_queue_lifecycle.CodeRabbitChatBudgetRegressionTests.test_account_wide_scan_uses_shared_heading_only_deadline

# Print the CodeRabbit chat-retry author-trust cases by name in the exact-HEAD
# gate log. CodeRabbit excludes tests/** from review, so the named
# trusted/untrusted/missing-author results are the only reviewable evidence
# that a copied or anonymous acknowledgment marker cannot suppress a retry.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_chat_quota.ChatQuotaParserTests.test_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head \
  tests.test_coderabbit_chat_quota.ChatQuotaRuntimeParityTests.test_js_chat_retry_acknowledgement_accepts_trusted_author_and_rejects_untrusted_author_and_missing_author_at_exact_head \
  tests.test_coderabbit_chat_quota.AutoMergeThreadVerifierTests.test_forged_verification_marker_cannot_bypass_chat_quota

# Print the negated-quota regression by name in the exact-HEAD gate log.
# CodeRabbit excludes tests/** from review, so the named results are the only
# reviewable evidence that a finding which merely discusses an unexceeded chat
# quota is still forwarded to repair instead of being treated as a rate limit.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v \
  tests.test_coderabbit_chat_quota.ChatQuotaParserTests.test_negated_hourly_quota_in_a_finding_is_not_a_rate_limit \
  tests.test_coderabbit_chat_quota.ChatQuotaRuntimeParityTests.test_js_classifier_matches_python_for_real_and_edge_vectors \
  tests.test_coderabbit_chat_quota.UnresolvedBatcherChatQuotaTests.test_negated_quota_explanation_still_dispatches

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
