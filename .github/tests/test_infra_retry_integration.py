#!/usr/bin/env python3
import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import failure_retry
import conflict_repair

HEAD = "a" * 40


class InfrastructureRetryStateTests(unittest.TestCase):
    def test_retry_marker_blocks_until_due(self):
        text = "<!-- continuum-infra-retry: attempt=3 next_retry_at=2026-09-30T12:04:00Z code=http_503 run=123 -->"
        before = failure_retry.retry_state(
            text, dt.datetime(2026, 9, 30, 12, 3, tzinfo=dt.timezone.utc)
        )
        after = failure_retry.retry_state(
            text, dt.datetime(2026, 9, 30, 12, 5, tzinfo=dt.timezone.utc)
        )
        self.assertEqual(before["infra_retry_attempts"], 3)
        self.assertFalse(before["retry_due"])
        self.assertTrue(after["retry_due"])

    def test_latest_marker_controls_due_time(self):
        text = "\n".join(
            [
                "<!-- continuum-infra-retry: attempt=1 next_retry_at=2026-09-30T12:01:00Z code=http_503 run=1 -->",
                "<!-- continuum-infra-retry: attempt=2 next_retry_at=2026-09-30T12:10:00Z code=http_503 run=2 -->",
            ]
        )
        state = failure_retry.retry_state(
            text, dt.datetime(2026, 9, 30, 12, 5, tzinfo=dt.timezone.utc)
        )
        self.assertEqual(state["infra_retry_attempts"], 2)
        self.assertEqual(state["next_retry_at"], "2026-09-30T12:10:00Z")
        self.assertFalse(state["retry_due"])

    def test_new_head_reset_starts_a_fresh_infrastructure_budget(self):
        text = "\n".join(
            [
                "<!-- continuum-infra-retry: attempt=8 next_retry_at=2026-09-30T11:00:00Z code=http_503 run=8 -->",
                "<!-- continuum-infra-retry-reset -->",
            ]
        )
        state = failure_retry.retry_state(
            text, dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)
        )
        self.assertEqual(state["infra_retry_attempts"], 0)
        self.assertEqual(state["next_retry_at"], "")
        self.assertTrue(state["retry_due"])
        self.assertFalse(state["infra_retry_exhausted"])


class ConflictBudgetSeparationTests(unittest.TestCase):
    def marker(self, attempt, state):
        return conflict_repair.render_episode_marker(
            attempt, 71, HEAD, attempt, state
        )

    def test_infrastructure_only_attempt_does_not_consume_semantic_budget(self):
        history = [
            self.marker(1, "open"),
            self.marker(1, "infra_failed"),
        ]
        self.assertEqual(conflict_repair.episode_attempts(history, 71), 0)

    def test_later_semantic_attempt_counts_normally(self):
        history = [
            self.marker(1, "open"),
            self.marker(1, "infra_failed"),
            self.marker(1, "open"),
            self.marker(1, "failed"),
        ]
        self.assertEqual(conflict_repair.episode_attempts(history, 71), 1)

    def test_three_semantic_attempts_still_exhaust_the_budget(self):
        history = []
        for attempt in range(1, 4):
            history.extend(
                [self.marker(attempt, "open"), self.marker(attempt, "failed")]
            )
        attempts = conflict_repair.episode_attempts(history, 71)
        self.assertEqual(attempts, 3)
        decision = conflict_repair.evaluate_recovery(
            behind=1,
            ruled_out=("update-branch",),
            attempts=attempts,
            max_attempts=3,
            seconds_since_attempt=10**6,
        )
        self.assertEqual(decision.action, conflict_repair.FAILED_ACTION)


class WorkflowWiringTests(unittest.TestCase):
    def test_schedulers_poll_without_runner_sleep(self):
        scheduler = (ROOT / ".github/workflows/issue-scheduler.yml").read_text()
        repair = (ROOT / ".github/workflows/opencode-repair.yml").read_text()
        self.assertIn('cron: "*/5 * * * *"', scheduler)
        self.assertIn('cron: "*/5 * * * *"', repair)
        self.assertIn("continuum-infra-retry:", scheduler)
        self.assertIn("failure_retry.py state", repair)

    def test_repair_uses_trusted_base_helpers(self):
        workflow = (ROOT / ".github/workflows/opencode.yml").read_text()
        self.assertIn("Materialize trusted control-plane helpers", workflow)
        self.assertIn("CONTINUUM_OPENCODE_RUNTIME_HELPER", workflow)
        self.assertIn("CONTINUUM_FAILURE_RETRY_HELPER", workflow)


if __name__ == "__main__":
    unittest.main()
