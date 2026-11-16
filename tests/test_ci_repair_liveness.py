"""Regression tests for fail-closed GitHub Actions jobless-run recovery."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / ".github/scripts/ci_repair_liveness.py"
spec = importlib.util.spec_from_file_location("ci_repair_liveness", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

NOW = datetime(2026, 11, 16, 19, 30, tzinfo=timezone.utc)
RUN_ID = 43929743624
TITLE = "OpenCode repair PR #459 [ci-attempt:ci-8703f93f8ae241529da0f645763786ed]"

def run(**overrides):
    obj = {
        "id": RUN_ID, "event": "workflow_dispatch", "display_title": TITLE,
        "run_attempt": 1, "status": "in_progress", "conclusion": None,
        "created_at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    obj.update(overrides)
    return obj

class TestJoblessLiveness(unittest.TestCase):
    def classify(self, value, jobs=None):
        return mod.run_decision(value, jobs if jobs is not None else {
            "total_count": 0, "jobs": [],
        }, NOW, RUN_ID, TITLE)[0]

    def test_real_stuck_case_requests_cancel_not_duplicate_dispatch(self):
        self.assertEqual(self.classify(run()), "cancel")

    def test_queued_young_and_old_runs_preserve_scheduler_position(self):
        self.assertEqual(self.classify(run(status="queued", created_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))), "wait")
        # A queued/pending job may legitimately be delayed by GitHub Actions.
        # Elapsed time alone must never cancel it or lose its queue position.
        self.assertEqual(self.classify(run(status="queued")), "wait")
        self.assertEqual(self.classify(run(status="pending")), "wait")
        self.assertEqual(self.classify(run(status="waiting")), "wait")
        self.assertEqual(self.classify(run(status="requested")), "wait")

    def test_running_with_a_job_cannot_be_cancelled(self):
        self.assertEqual(self.classify(run(), {"total_count": 1, "jobs": [{"id": 23}]}), "work")

    def test_cancelled_zero_job_run_can_rerun_same_id_once(self):
        self.assertEqual(self.classify(run(status="completed", conclusion="cancelled")), "rerun")
        self.assertEqual(self.classify(run(status="completed", conclusion="cancelled", run_attempt=2)), "hold")

    def test_failed_validation_does_not_rerun(self):
        self.assertEqual(self.classify(run(status="completed", conclusion="failure")), "hold")

    def test_unknown_or_partial_evidence_fails_closed(self):
        self.assertEqual(self.classify(run(), {"jobs": []}), "hold")
        self.assertEqual(self.classify(run(display_title="wrong")), "hold")
        self.assertEqual(self.classify(run(event="push")), "hold")
        self.assertEqual(self.classify(run(created_at="invalid")), "hold")
        self.assertEqual(self.classify(run(status="invalid")), "hold")

    def test_untrusted_marker_does_not_claim_mutation(self):
        marker = "<!-- continuum-ci-repair-liveness repo=r pr=459 -->"
        self.assertFalse(mod.trusted_marker([{"body": marker, "user": {"login": "attacker"}}], marker, "kodmial"))
        self.assertTrue(mod.trusted_marker([{"body": marker, "user": {"login": "kodmial"}}], marker, "kodmial"))

if __name__ == "__main__":
    unittest.main()
