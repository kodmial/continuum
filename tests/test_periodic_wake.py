"""External periodic wake-up and cross-source dedupe (kodmial/continuum#375).

The native GitHub schedule stays authoritative. An optional cron-job.org POST
uses the installed Issue scheduler caller with ``periodic_wake=true``. Both
signals must serialize onto the same native 10-minute window: a successful
pass from either source makes the second periodic delivery a cheap NOOP,
while failed/cancelled/pending attempts, manual dispatches and ordinary events
keep their existing behavior.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

import yaml

from continuum.periodic_wake import (
    ISSUE_SCHEDULER_INTERVAL_SECONDS,
    ISSUE_SCHEDULER_OFFSET_SECONDS,
    PERIODIC_RUN_MARKER,
    is_periodic_evidence,
    is_periodic_signal,
    parse_periodic_wake,
    periodic_dedupe_decision,
    scheduling_window_start,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
STUB = ROOT / ".github" / "caller-stubs" / "continuum-issue-scheduler.yml"


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def run(
    run_id: int,
    *,
    event: str = "schedule",
    status: str = "completed",
    conclusion: str = "success",
    created_at: str = "",
    title: str = "Issue scheduler schedule",
    branch: str = "main",
) -> dict:
    return {
        "id": run_id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "display_title": title,
        "head_branch": branch,
    }


class WindowTests(unittest.TestCase):
    def test_native_window_is_anchored_at_minute_seven(self):
        # 2026-10-23T10:59:00Z belongs to the 10:57 native window.
        now = 1792753140
        start = scheduling_window_start(now)
        self.assertEqual(1792753020, start)
        self.assertEqual(7 * 60, start % ISSUE_SCHEDULER_INTERVAL_SECONDS)
        self.assertTrue(0 <= now - start < ISSUE_SCHEDULER_INTERVAL_SECONDS)

    def test_delayed_native_delivery_stays_in_its_window(self):
        # 10:59 and 11:06 are both in the 10:57 window; 11:08 opens 11:07.
        first = scheduling_window_start(1792753140)
        delayed = scheduling_window_start(1792753560)
        next_window = scheduling_window_start(1792753680)
        self.assertEqual(first, delayed)
        self.assertNotEqual(delayed, next_window)
        self.assertEqual(next_window - delayed, ISSUE_SCHEDULER_INTERVAL_SECONDS)

    def test_signal_detection(self):
        self.assertTrue(is_periodic_signal("schedule", "false"))
        self.assertTrue(is_periodic_signal("workflow_dispatch", "true"))
        self.assertTrue(is_periodic_signal("workflow_dispatch", True))
        self.assertFalse(is_periodic_signal("workflow_dispatch", "false"))
        self.assertFalse(is_periodic_signal("issues", ""))
        self.assertTrue(parse_periodic_wake(" TRUE "))
        self.assertTrue(parse_periodic_wake(True))
        self.assertFalse(parse_periodic_wake(False))
        self.assertFalse(parse_periodic_wake(None))
        for legacy_truthy in ("1", "yes", "on"):
            with self.subTest(value=legacy_truthy):
                self.assertFalse(parse_periodic_wake(legacy_truthy))
        self.assertFalse(is_periodic_signal("workflow_dispatch", "yes"))


class DedupeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1792753140  # 2026-10-23T10:59:00Z
        self.start = scheduling_window_start(self.now)

    def decide(self, runs, **kwargs):
        kwargs.setdefault("caller_event_name", "schedule")
        kwargs.setdefault("periodic_wake", "false")
        return periodic_dedupe_decision(
            now_epoch=self.now,
            runs=runs,
            current_run_id=999,
            **kwargs,
        )

    def test_native_success_dedupes_external_signal(self):
        evidence = run(1, created_at=iso(self.start + 10))
        decision = self.decide(
            [evidence],
            caller_event_name="workflow_dispatch",
            periodic_wake="true",
        )
        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "duplicate-window")
        self.assertEqual(decision.evidence_run_id, 1)

    def test_external_success_dedupes_native_signal(self):
        evidence = run(
            2,
            event="workflow_dispatch",
            title=f"Issue scheduler {PERIODIC_RUN_MARKER}",
            created_at=iso(self.start),
        )
        decision = self.decide([evidence])
        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "duplicate-window")

    def test_first_due_external_signal_runs_when_native_was_skipped(self):
        decision = self.decide(
            [],
            caller_event_name="workflow_dispatch",
            periodic_wake="true",
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "window-due")
        self.assertEqual(decision.window_start, self.start)

    def test_previous_window_evidence_does_not_block_current_window(self):
        previous = run(3, created_at=iso(self.start - 60))
        decision = self.decide([previous])
        self.assertTrue(decision.run)

    def test_delayed_native_evidence_dedupes_later_external_signal(self):
        later = self.start + 9 * 60
        self.assertEqual(self.start, scheduling_window_start(later))
        delayed = run(4, created_at=iso(later))
        decision = periodic_dedupe_decision(
            now_epoch=later,
            runs=[delayed],
            current_run_id=999,
            caller_event_name="workflow_dispatch",
            periodic_wake="true",
        )
        self.assertFalse(decision.run)
        self.assertEqual(decision.window_start, self.start)

    def test_pending_queued_and_replaced_runs_are_not_evidence(self):
        for status in ("queued", "in_progress", "waiting", "pending"):
            with self.subTest(status=status):
                pending = run(5, status=status, conclusion=None, created_at=iso(self.start))
                decision = self.decide([pending])
                self.assertTrue(decision.run, "a pending run has not processed the window")

    def test_replaced_pending_run_does_not_poison_the_next_attempt(self):
        replaced = run(
            50,
            status="completed",
            conclusion="cancelled",
            created_at=iso(self.start),
        )
        replacement = run(51, status="queued", conclusion=None, created_at=iso(self.start))
        decision = self.decide([replaced, replacement])
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "window-due")

    def test_failed_cancelled_and_crashed_runs_are_not_evidence(self):
        for conclusion in ("failure", "cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                failed = run(6, conclusion=conclusion, created_at=iso(self.start))
                decision = self.decide([failed])
                self.assertTrue(decision.run, "a failed attempt must not poison retries")

    def test_current_run_is_never_its_own_evidence(self):
        current = run(777, created_at=iso(self.start))
        decision = periodic_dedupe_decision(
            now_epoch=self.now,
            runs=[current],
            current_run_id=777,
            caller_event_name="schedule",
            periodic_wake="false",
        )
        self.assertTrue(decision.run)

    def test_manual_dispatch_is_never_gated_and_never_counts_as_evidence(self):
        manual = run(8, event="workflow_dispatch", created_at=iso(self.start))
        manual_decision = self.decide(
            [manual],
            caller_event_name="workflow_dispatch",
            periodic_wake="false",
        )
        self.assertTrue(manual_decision.run)
        self.assertEqual(manual_decision.reason, "non-periodic")

        scheduled = self.decide([manual])
        self.assertTrue(scheduled.run, "a manual pass must not suppress the native pass")

    def test_issue_and_pr_events_are_never_gated(self):
        evidence = run(9, created_at=iso(self.start))
        for event in ("issues", "issue_comment", "pull_request_target", "workflow_run", "push"):
            with self.subTest(event=event):
                decision = self.decide([evidence], caller_event_name=event)
                self.assertTrue(decision.run)
                self.assertEqual(decision.reason, "non-periodic")

    def test_other_branch_evidence_does_not_block_default_branch(self):
        other = run(10, created_at=iso(self.start), branch="feature")
        decision = self.decide([other], default_branch="main")
        self.assertTrue(decision.run)

    def test_transient_lookup_failure_fails_open(self):
        decision = self.decide([], lookup_failed=True)
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "lookup-failed")

    def test_marker_filter_is_explicit(self):
        evidence = run(11, event="workflow_dispatch", title="ordinary manual", created_at=iso(self.start))
        self.assertFalse(
            is_periodic_evidence(
                evidence,
                current_run_id=999,
                window_start_epoch=self.start,
            )
        )


class WorkflowGateTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
        self.schedule = self.workflow["jobs"]["schedule"]
        self.gate = self.schedule["steps"][0]

    def test_gate_is_the_first_step_and_every_domain_step_is_gated(self):
        self.assertEqual(
            "Admit periodic wake for this scheduling window", self.gate["name"]
        )
        self.assertEqual("periodic_gate", self.gate["id"])
        for step in self.schedule["steps"][1:]:
            with self.subTest(step=step["name"]):
                self.assertIn(
                    "steps.periodic_gate.outputs.run == 'true'",
                    str(step.get("if", "")),
                )

    def test_gate_uses_the_native_window_and_periodic_marker(self):
        script = self.gate["run"]
        for sentinel in (
            "7 * 60",
            "10 * 60",
            "actions/runs/$GITHUB_RUN_ID",
            "actions/workflows/$workflow_id/runs",
            PERIODIC_RUN_MARKER,
            "status == \"completed\" and .conclusion == \"success\"",
            "event == \"schedule\"",
        ):
            self.assertIn(sentinel, script)

    def test_native_cron_expression_is_unchanged(self):
        rules = self.workflow.get("on") or self.workflow.get(True)
        stub = yaml.safe_load(STUB.read_text(encoding="utf-8"))
        stub_rules = stub.get("on") or stub.get(True)
        self.assertEqual(
            ["7,17,27,37,47,57 * * * *"],
            [entry["cron"] for entry in stub_rules["schedule"]],
        )
        self.assertIn("workflow_dispatch", stub_rules)
        self.assertEqual(["workflow_call"], list(rules.keys()))

    def test_periodic_input_is_additive_and_forwarded(self):
        rules = self.workflow.get("on") or self.workflow.get(True)
        callee = rules["workflow_call"]["inputs"]["periodic_wake"]
        self.assertEqual("false", callee["default"])
        self.assertEqual("string", callee["type"])
        self.assertFalse(callee["required"])

        stub = yaml.safe_load(STUB.read_text(encoding="utf-8"))
        stub_rules = stub.get("on") or stub.get(True)
        dispatch = stub_rules["workflow_dispatch"]["inputs"]["periodic_wake"]
        self.assertEqual("false", dispatch["default"])
        self.assertEqual("string", dispatch["type"])
        call_with = stub["jobs"]["call"]["with"]
        self.assertEqual("${{ inputs.periodic_wake }}", call_with["periodic_wake"])
        self.assertIn("[continuum-periodic]", stub["run-name"])
        self.assertIn("github.event.inputs.periodic_wake == 'true'", stub["run-name"])

    def test_child_runner_guard_unchanged(self):
        self.assertEqual("vars.CONTINUUM_ROLE != 'child'", self.schedule["if"])
        stub = yaml.safe_load(STUB.read_text(encoding="utf-8"))
        self.assertIn("vars.CONTINUUM_ROLE != 'child'", stub["jobs"]["call"]["if"])

    def test_docs_are_present_and_have_no_provisioning_api(self):
        doc = (ROOT / "docs" / "cron-job-org.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("cron-job-org.md", readme)
        self.assertIn("periodic_wake", doc)
        self.assertIn("continuum-issue-scheduler.yml", doc)
        self.assertIn("/dispatches", doc)
        self.assertIn("fine-grained", doc.lower())
        self.assertIn("Actions: write", doc)
        self.assertNotIn("CRON_JOB_ORG_API_KEY", doc)
        self.assertNotIn("api.cron-job.org", doc)


class ExtractedGateScriptTests(unittest.TestCase):
    """Execute the real workflow step with a fake gh, not a copy."""

    def _extract(self) -> str:
        workflow = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
        return workflow["jobs"]["schedule"]["steps"][0]["run"]

    def _run(self, *, caller_event, periodic, evidence="0", mode="normal", branch="main"):
        script = self._extract()
        fixtures = ROOT / ".opencode-tmp"
        fixtures.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=fixtures) as temp:
            temp_path = pathlib.Path(temp)
            bindir = temp_path / "bin"
            bindir.mkdir()
            gh = bindir / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "set -e\n"
                'printf \'%s\\n\' "$*" >> "$FAKE_GH_LOG"\n'
                'case "${FAKE_GH_MODE:-normal}" in\n'
                '  fail-403) echo "HTTP 403: Resource not accessible" >&2; exit 1 ;;\n'
                '  fail-429) echo "HTTP 429: rate limit exceeded" >&2; exit 1 ;;\n'
                '  fail-500) echo "HTTP 500: transient server error" >&2; exit 1 ;;\n'
                'esac\n'
                'if [[ "$*" == *"/actions/workflows/"* ]]; then\n'
                '  [[ "${FAKE_GH_MODE:-normal}" == "fail-evidence" ]] && exit 1\n'
                '  echo "${FAKE_EVIDENCE:-0}"\n'
                '  exit 0\n'
                "fi\n"
                'if [[ "$*" == *"/actions/runs/"* ]]; then\n'
                '  [[ "${FAKE_GH_MODE:-normal}" == "fail-workflow" ]] && exit 1\n'
                "  echo 42\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            log = temp_path / "gh.log"
            out = temp_path / "out"
            out.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env.update(
                PATH=f"{bindir}:{env['PATH']}",
                GITHUB_REPOSITORY="owner/repo",
                GITHUB_RUN_ID="123",
                GITHUB_OUTPUT=str(out),
                CALLER_EVENT_NAME=caller_event,
                PERIODIC_WAKE=periodic,
                DEFAULT_BRANCH=branch,
                FAKE_EVIDENCE=evidence,
                FAKE_GH_MODE=mode,
                FAKE_GH_LOG=str(log),
            )
            result = subprocess.run(
                ["bash", "-c", script],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            outputs = dict(
                line.split("=", 1)
                for line in out.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            return result, outputs, calls

    def test_non_periodic_and_manual_paths_skip_the_guard_lookup(self):
        for caller, periodic in (
            ("push", "false"),
            ("issues", "false"),
            ("workflow_dispatch", "false"),
        ):
            with self.subTest(caller=caller):
                result, outputs, calls = self._run(
                    caller_event=caller, periodic=periodic
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("true", outputs["run"])
                self.assertEqual("non-periodic", outputs["reason"])
                self.assertEqual("", calls)

    def test_duplicate_periodic_window_is_a_cheap_noop(self):
        result, outputs, calls = self._run(
            caller_event="workflow_dispatch", periodic="true", evidence="1"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("false", outputs["run"])
        self.assertEqual("duplicate-window", outputs["reason"])
        self.assertIn("actions/workflows/42/runs", calls)
        self.assertIn("contains(\"[continuum-periodic]\")", calls)

    def test_due_native_and_external_windows_run(self):
        for caller, periodic in (("schedule", "false"), ("workflow_dispatch", "true")):
            with self.subTest(caller=caller):
                result, outputs, _ = self._run(
                    caller_event=caller, periodic=periodic, evidence="0"
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("true", outputs["run"])
                self.assertEqual("window-due", outputs["reason"])

    def test_api_failures_fail_open_without_poisoning_the_window(self):
        for mode in (
            "fail-workflow",
            "fail-evidence",
            "fail-403",
            "fail-429",
            "fail-500",
        ):
            with self.subTest(mode=mode):
                result, outputs, _ = self._run(
                    caller_event="schedule",
                    periodic="false",
                    evidence="1",
                    mode=mode,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("true", outputs["run"])
                self.assertEqual("lookup-unavailable", outputs["reason"])


if __name__ == "__main__":
    unittest.main()
