"""Authoritative delegated active-run discovery (kodmial/continuum#387).

The production defect was a newest-100 *all-status* Actions listing: an
older still-active SubTask disappeared behind 100+ newer completed
PR-Agent/recovery runs, so the schedule selected and dispatched it a second
time. These tests execute the shipped shell helper used by the scheduler
workflow against a fake GitHub API and pin:

- status-filtered discovery finds older active work regardless of completed
  history size and counts it toward WIP;
- every active status variant is admitted, while terminal historical runs
  never occupy WIP;
- API errors, malformed payloads and page-cap truncation fail closed (never
  an empty "none active" inventory);
- a fresh recheck observes a late-arriving identical run;
- the workflow consumes the helper and rechecks immediately before dispatch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "delegated_active_runs.sh"
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
ACTIVE_STATUSES = ("in_progress", "queued", "pending", "waiting", "requested")


def run(status: str, title: str, **extra) -> dict:
    payload = {"status": status, "display_title": title}
    payload.update(extra)
    return payload


class ActiveRunDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls.log"
        self.calls.write_text("")
        self.fixture = self.root / "fixture.json"
        self.marker = self.root / "late-run"
        self.fixture.write_text("{}")

        self.bin = self.root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

args = sys.argv[1:]
url = next((arg for arg in args if arg.startswith(("repos/", "/repos/"))), "")
if not url:
    print("unexpected gh invocation", file=sys.stderr)
    sys.exit(2)
calls = os.environ.get("FIXTURE_CALLS", "")
if calls:
    with open(calls, "a", encoding="utf-8") as handle:
        handle.write(url + "\\n")

data = json.loads(Path(os.environ["FIXTURE"]).read_text())
query = parse_qs(urlparse(url).query)
status = (query.get("status") or [""])[0]
page = int((query.get("page") or ["1"])[0])
per_page = int((query.get("per_page") or ["100"])[0])

if data.get("fail_all") or status == data.get("fail_status"):
    print(data.get("failure_message", "gh: HTTP 500"), file=sys.stderr)
    sys.exit(1)
if status == data.get("malformed_status"):
    print("this is not json")
    sys.exit(0)

truncated = status == data.get("truncate_status")
if truncated:
    runs = [{"status": status, "display_title": f"SubTask #{i + 1}"}
            for i in range(per_page)]
else:
    runs = data.get("runs", {}).get(status, [])

late = data.get("late_run")
if late and status == late.get("status", "in_progress"):
    if Path(os.environ["FIXTURE_MARKER"]).exists():
        runs = [{"status": status, "display_title": late["title"]}]

batch = runs if truncated else runs[(page - 1) * per_page: page * per_page]
print(json.dumps({"total_count": len(runs), "workflow_runs": batch}))
"""
        )
        gh.chmod(0o755)

    def run_script(self, *args, env_extra=None):
        env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            FIXTURE=str(self.fixture),
            FIXTURE_CALLS=str(self.calls),
            FIXTURE_MARKER=str(self.marker),
            RUNNER_TEMP=str(self.root),
            GITHUB_REPOSITORY="owner/parent",
        )
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def write_fixture(self, **data):
        self.fixture.write_text(json.dumps(data))

    def snapshot(self, env_extra=None):
        output = self.root / "active.tsv"
        result = self.run_script("snapshot", "--output", str(output), env_extra=env_extra)
        return result, output

    def task_active(self, output, task=153, kind="task", child_id="alpha"):
        return self.run_script(
            "task-active",
            "--file",
            str(output),
            "--child-id",
            child_id,
            "--kind",
            kind,
            "--task",
            str(task),
        )

    def test_older_active_subtask_survives_completed_run_churn(self):
        completed = [run("completed", f"PR Agent #{i}") for i in range(300)]
        self.write_fixture(
            runs={
                "in_progress": [run("in_progress", "SubTask #153")],
                "queued": [],
                "pending": [],
                "waiting": [],
                "requested": [],
                "completed": completed,
            }
        )

        result, output = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("in_progress\tSubTask #153", output.read_text().splitlines())

        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 5, calls)
        self.assertTrue(all("status=" in call for call in calls), calls)
        self.assertFalse(any("status=completed" in call for call in calls), calls)

        # The previous newest-100 all-status view would contain only newer
        # completed runs, so the active SubTask would be invisible.
        old_view = completed[:100]
        old_active_titles = [entry["display_title"] for entry in old_view]
        self.assertNotIn("SubTask #153", old_active_titles)

        # The patched admission path sees it, keeps it in WIP, and still lets
        # a genuinely free task use the remaining capacity.
        self.assertEqual(0, self.task_active(output, 153).returncode)
        self.assertEqual(1, self.task_active(output, 200).returncode)
        wip = self.run_script("wip-count", "--file", str(output))
        self.assertEqual(wip.returncode, 0, wip.stderr)
        self.assertEqual(wip.stdout.strip(), "1")

    def test_all_active_status_variants_count_toward_wip(self):
        self.write_fixture(
            runs={
                "queued": [run("queued", "SubTask #201")],
                "in_progress": [run("in_progress", "SubTask #202")],
                "pending": [run("pending", "SubTask #203", runner_id=None)],
                "waiting": [run("waiting", "SubTask #204")],
                "requested": [run("requested", "SubTask #205", runner_id=None)],
                "completed": [run("completed", "SubTask #299")],
                "cancelled": [run("cancelled", "SubTask #298")],
                "timed_out": [run("timed_out", "SubTask #297")],
            }
        )

        result, output = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = output.read_text().splitlines()
        for status in ACTIVE_STATUSES:
            self.assertTrue(
                any(line.startswith(status + "\t") for line in lines),
                f"missing {status}: {lines}",
            )
        for task in range(201, 206):
            self.assertEqual(0, self.task_active(output, task).returncode)
        for terminal in (299, 298, 297):
            self.assertEqual(1, self.task_active(output, terminal).returncode)
        wip = self.run_script("wip-count", "--file", str(output))
        self.assertEqual(wip.stdout.strip(), "5")

    def test_calls_are_bounded_and_completed_history_is_never_scanned(self):
        self.write_fixture(
            runs={
                "in_progress": [run("in_progress", "SubTask #153")],
                "queued": [],
                "pending": [],
                "waiting": [],
                "requested": [],
                "completed": [run("completed", f"CI #{i}") for i in range(1000)],
            }
        )
        result, output = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 5, "one short page per active status")
        self.assertFalse(any("status=completed" in call for call in calls))

        # Reusing the snapshot for many candidates adds no REST calls at all:
        # there is no per-child N+1 discovery.
        before = len(self.calls.read_text().splitlines())
        for task in range(1, 101):
            self.task_active(output, task)
        after = len(self.calls.read_text().splitlines())
        self.assertEqual(before, after)

    def test_discovery_failures_fail_closed_without_output(self):
        cases = [
            ("failure", {"fail_status": "queued", "failure_message": "gh: HTTP 403 Forbidden"}),
            ("rate-limit", {"fail_status": "waiting", "failure_message": "HTTP 429 rate limit"}),
            ("timeout", {"fail_status": "requested", "failure_message": "request timed out"}),
            ("malformed", {"malformed_status": "pending"}),
        ]
        for name, fixture in cases:
            with self.subTest(case=name):
                self.calls.write_text("")
                self.write_fixture(runs={}, **fixture)
                result, output = self.snapshot()
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertFalse(output.exists(), "no partial active inventory")
                self.assertIn(
                    "Delegated active-run discovery is unavailable",
                    result.stderr,
                )
                self.assertNotIn("owner/parent", result.stderr)
                self.assertNotIn("child", result.stderr.lower())

    def test_page_cap_truncation_fails_closed(self):
        self.write_fixture(runs={}, truncate_status="in_progress")
        result, output = self.snapshot(
            env_extra={"CONTINUUM_ACTIVE_RUNS_MAX_PAGES": "2"}
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertFalse(output.exists())
        in_progress_calls = [
            call for call in self.calls.read_text().splitlines()
            if "status=in_progress" in call
        ]
        self.assertEqual(len(in_progress_calls), 2)
        self.assertIn("page=2", in_progress_calls[1])

    def test_empty_active_inventory_is_a_real_success(self):
        self.write_fixture(runs={})
        result, output = self.snapshot()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), "")
        wip = self.run_script("wip-count", "--file", str(output))
        self.assertEqual(wip.stdout.strip(), "0")

    def test_fresh_check_observes_a_late_arriving_run(self):
        self.write_fixture(late_run={"title": "SubTask #307", "status": "in_progress"})
        before = self.run_script("check", "--child-id", "alpha", "--task", "307")
        self.assertEqual(before.returncode, 1, before.stderr)

        self.marker.touch()
        after = self.run_script("check", "--child-id", "alpha", "--task", "307")
        self.assertEqual(after.returncode, 0, after.stderr)

        # A fresh-check outage is uncertainty, never permission to dispatch.
        self.write_fixture(fail_all=True)
        outage = self.run_script("check", "--child-id", "alpha", "--task", "307")
        self.assertEqual(outage.returncode, 4, outage.stderr)

    def test_fresh_check_matches_review_runs(self):
        self.write_fixture(
            runs={
                "queued": [run("queued", "SubTask review #401")],
                "in_progress": [],
                "pending": [],
                "waiting": [],
                "requested": [],
            }
        )
        result = self.run_script("check", "--child-id", "alpha", "--task", "401")
        self.assertEqual(result.returncode, 0, result.stderr)


class SchedulerWorkflowWiringTests(unittest.TestCase):
    def test_scheduler_uses_status_filtered_discovery_not_newest_100(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        self.assertIn("delegated_active_runs.sh", body)
        self.assertIn(
            'bash "$active_script" snapshot --output "$active_runs_file"', body
        )
        self.assertIn(
            'bash "$active_script" task-active --file "$active_runs_file"', body
        )
        self.assertIn("active_runs_state=degraded", body)
        self.assertIn("CHILD_ACTIVE_RUNS_STATE", body)
        self.assertNotIn(
            'gh run list --repo "$GITHUB_REPOSITORY" --limit 100', body
        )
        self.assertNotIn("--json status,displayTitle", body)

    def test_dispatch_is_guarded_by_a_fresh_authoritative_recheck(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        dispatch = body.index("await dispatchWorkflow(childWorkerWorkflow")
        recheck = body.rindex("freshDelegatedTaskActive", 0, dispatch)
        self.assertLess(recheck, dispatch)
        self.assertIn("dispatchedTaskKeys.has(taskKey)", body)
        self.assertIn("dispatchedTaskKeys.add(taskKey);", body)
        self.assertIn("if (delegationState === 'ok') {", body)
        self.assertIn("if (activeRunsState === 'degraded') {", body)
        # Uncertainty must suppress rather than admit coding dispatch.
        helper = body[
            body.index("function freshDelegatedTaskActive"):
            body.index("function positiveInt")
        ]
        self.assertIn("return true;", helper)
        self.assertIn("skipping dispatch for this pass", helper)

    def test_recheck_reuses_one_pass_snapshot_instead_of_per_candidate_checks(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        helper = body[
            body.index("function ensureFreshDelegatedActiveRuns"):
            body.index("function positiveInt")
        ]
        normalized = " ".join(helper.split())
        self.assertIn(
            "[activeRunsScript, 'snapshot', '--output', freshActiveRunsFile]",
            normalized,
        )
        self.assertIn("'task-active'", helper)
        self.assertNotIn("'check'", helper)
        self.assertIn("freshActiveRunsReady", helper)
        self.assertIn("freshActiveRunsUnavailable", helper)
        self.assertEqual(
            helper.count("execFileSync("),
            2,
            "one snapshot and one local lookup call, never per-candidate discovery",
        )


if __name__ == "__main__":
    unittest.main()
