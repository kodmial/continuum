"""Fail-closed contract for the scheduler's local OpenCode inventory.

A failed PR or Actions-run lookup must never read as an empty inventory: the
scheduler would then admit a delegated worker for a task a local run already
owns. Both lookups carry the legacy dispatcher's ``__unknown__`` sentinel and
``local_override_active`` treats it as active.

The tests execute the real bash extracted from the checked-in workflow with a
stubbed ``gh``, so the decision is observed rather than mirrored.
"""

from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"

INVENTORY_START = '            if ! local_opencode_pr_tasks="$('
INVENTORY_END = '            child_owner="$(gh api'
GATE_START = '            local_override_active() {'
GATE_END = '              [[ -n "$child_owner" ]] || return 1'


def _extract(body: str, start: str, end: str) -> str:
    start_index = body.index(start)
    return textwrap.dedent(body[start_index:body.index(end, start_index)])


def _harness(gh_body: str, snapshot_body: str = '  : > "$4"', task_number: int = 42) -> str:
    body = WORKFLOW.read_text(encoding="utf-8")
    return "\n".join(
        [
            "set -euo pipefail",
            'child_id="opaque-child"',
            'child_repo="owner/child"',
            'RUNNER_TEMP="$(mktemp -d)"',
            'trap \'rm -rf "$RUNNER_TEMP"\' EXIT',
            'active_script="/fake/delegated_active_runs.sh"',
            "gh() {",
            gh_body,
            "}",
            "bash() {",
            '  [[ "$GITHUB_REPOSITORY" == "$child_repo" && "$1" == "$active_script" && "$2" == "snapshot" && "$3" == "--output" ]] || return 9',
            snapshot_body,
            "}",
            _extract(body, INVENTORY_START, INVENTORY_END),
            # The extracted function stops before the live API section; close it
            # with an idle result so only the inventory gate is under test.
            _extract(body, GATE_START, GATE_END) + "  return 1\n}\n",
            f'if local_override_active {task_number}; then echo ACTIVE; else echo IDLE; fi',
            'printf "PR=%s\\n" "$local_opencode_pr_tasks"',
            'printf "RUN=%s\\n" "$local_opencode_run_tasks"',
        ]
    )


def _run(gh_body: str, snapshot_body: str = '  : > "$4"', task_number: int = 42) -> dict[str, str | list[str]]:
    completed = subprocess.run(
        ["bash", "-c", _harness(gh_body, snapshot_body, task_number)],
        check=True,
        capture_output=True,
        text=True,
    )
    result: dict[str, str | list[str]] = {"decision": "", "PR": [], "RUN": []}
    for line in completed.stdout.splitlines():
        if line in ("ACTIVE", "IDLE"):
            result["decision"] = line
            continue
        key, separator, value = line.partition("=")
        if separator and key in ("PR", "RUN") and value:
            result[key].append(value)  # type: ignore[union-attr]
    return result


class LocalOpenCodeInventoryTests(unittest.TestCase):
    def test_failed_pr_lookup_marks_inventory_unknown(self):
        result = _run(
            'if [[ "$1" == "pr" ]]; then return 1; fi; return 0'
        )
        self.assertEqual(["__unknown__"], result["PR"])
        self.assertEqual("ACTIVE", result["decision"])

    def test_failed_run_lookup_marks_inventory_unknown_on_any_page(self):
        result = _run('return 0', '  return 4')
        self.assertEqual(["__unknown__"], result["RUN"])
        self.assertEqual("ACTIVE", result["decision"])

    def test_completed_history_does_not_hide_ready_child_issue(self):
        # The prior all-status third page was full in CodeMy (>5600 runs),
        # even when zero OpenCode runs were active. An empty active-only
        # snapshot must not be treated as an unknown inventory.
        result = _run('return 0')
        self.assertEqual([], result['RUN'])
        self.assertEqual('IDLE', result['decision'])
        inventory = _extract(WORKFLOW.read_text(encoding='utf-8'), INVENTORY_START, INVENTORY_END)
        self.assertIn('GITHUB_REPOSITORY="$child_repo" bash "$active_script" snapshot', inventory)
        self.assertNotIn('actions/runs?per_page=100&page=', inventory)

    def test_both_lookups_failing_never_reads_as_idle(self):
        result = _run("return 1", "  return 4")
        self.assertEqual(["__unknown__"], result["PR"])
        self.assertEqual(["__unknown__"], result["RUN"])
        self.assertEqual("ACTIVE", result["decision"])

    def test_empty_successful_inventory_stays_idle(self):
        result = _run("return 0")
        self.assertEqual([], result["PR"])
        self.assertEqual([], result["RUN"])
        self.assertEqual("IDLE", result["decision"])

    def test_successful_lookup_still_matches_the_owning_task(self):
        gh_body = (
            'if [[ "$1" == "pr" ]]; then echo "opencode/issue42-fix"; return 0; fi; '
            'return 0'
        )
        snapshot = '  printf "in_progress\\tOpenCode issue #42\\n" > "$4"'
        owning = _run(gh_body, snapshot, task_number=42)
        self.assertEqual(["42"], owning["PR"])
        self.assertEqual(["42"], owning["RUN"])
        self.assertEqual("ACTIVE", owning["decision"])
        other = _run(gh_body, snapshot, task_number=7)
        self.assertEqual("IDLE", other["decision"])


if __name__ == "__main__":
    unittest.main()
