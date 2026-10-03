"""Deterministic regression tests for PR-Agent recovery (Work Lock #38)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from continuum import pr_agent_recovery as recovery  # noqa: E402


HEAD = "a" * 40
OLD_HEAD = "b" * 40


def comment(body: str, *, association: str = "OWNER", age_seconds: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "body": body,
        "author_association": association,
        "created_at": when.isoformat(),
    }


class IdentityTests(unittest.TestCase):
    def test_operation_identity_is_pr_head_and_kind(self):
        self.assertEqual(recovery.operation_key(38, HEAD.upper(), "review"), f"38:{HEAD}:review")
        self.assertNotEqual(
            recovery.operation_key(38, HEAD, "review"),
            recovery.operation_key(38, HEAD, "repair"),
        )
        self.assertNotEqual(
            recovery.operation_key(38, HEAD, "review"),
            recovery.operation_key(38, OLD_HEAD, "review"),
        )

    def test_invalid_identity_fails_closed(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.operation_key(0, HEAD, "review")
        with self.assertRaises(recovery.RecoveryError):
            recovery.operation_key(1, "not-a-sha", "review")
        with self.assertRaises(recovery.RecoveryError):
            recovery.operation_key(1, HEAD, "merge")


class DurableEvidenceTests(unittest.TestCase):
    def test_retry_state_survives_independent_wakeups(self):
        comments = [
            comment(f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->"),
            comment(f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=2 -->"),
        ]
        evidence = recovery.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertEqual(evidence.latest_attempt, 2)
        self.assertFalse(evidence.exhausted)

    def test_external_comment_cannot_forge_retry_budget(self):
        comments = [
            comment(
                f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=99 -->",
                association="CONTRIBUTOR",
            ),
            comment(
                f"<!-- continuum-pr-agent-retry-exhausted head={HEAD} kind=review attempts=99 -->",
                association="NONE",
            ),
        ]
        evidence = recovery.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertIsNone(evidence.latest_attempt)
        self.assertFalse(evidence.exhausted)

    def test_changed_head_invalidates_old_retry_state(self):
        comments = [
            comment(f"<!-- continuum-pr-agent-retry head={OLD_HEAD} kind=review attempt=2 -->"),
            comment(
                f"<!-- continuum-pr-agent-retry-exhausted head={OLD_HEAD} kind=review attempts=3 -->"
            ),
        ]
        evidence = recovery.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertIsNone(evidence.latest_attempt)
        self.assertFalse(evidence.exhausted)

    def test_review_and_repair_budgets_are_independent(self):
        comments = [
            comment(f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=2 -->"),
            comment(f"<!-- continuum-pr-agent-retry head={HEAD} kind=repair attempt=1 -->"),
        ]
        review = recovery.retry_evidence(comments, head_sha=HEAD, kind="review")
        repair = recovery.retry_evidence(comments, head_sha=HEAD, kind="repair")
        self.assertEqual(review.latest_attempt, 2)
        self.assertEqual(repair.latest_attempt, 1)


class RecoveryDecisionTests(unittest.TestCase):
    def test_lost_ci_wakeup_dispatches_initial_exact_head_review(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state=None,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 0)

    def test_cancelled_review_recovers_with_next_bounded_attempt(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion="cancelled",
            evidence=recovery.RetryEvidence(),
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 1)

    def test_timed_out_repair_recovers(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion="timed_out",
            evidence=recovery.RetryEvidence(latest_attempt=1),
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 2)

    def test_stale_pending_operation_recovers_without_a_run(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion=None,
            status_age_seconds=recovery.STALE_AFTER_SECONDS + 1,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 1)

    def test_active_exact_run_coalesces_duplicate_wakeup(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="transient; recovery eligible",
            active_exact_run=True,
        )
        self.assertEqual(decision.action, "wait")
        self.assertIsNone(decision.attempt)

    def test_newer_dispatch_marker_coalesces_independent_wakeup(self):
        evidence = recovery.RetryEvidence(latest_attempt=1)
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="transient; recovery eligible",
            evidence=evidence,
            marker_newer_than_status=True,
            marker_age_seconds=10,
        )
        self.assertEqual(decision.action, "wait")

    def test_lost_dispatch_replays_same_attempt_instead_of_burning_slot(self):
        evidence = recovery.RetryEvidence(latest_attempt=1)
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="transient; recovery eligible",
            evidence=evidence,
            marker_newer_than_status=True,
            marker_age_seconds=recovery.DISPATCH_GRACE_SECONDS + 1,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 1)

    def test_transient_failure_retries(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent review failed: transient; recovery eligible",
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 1)

    def test_deterministic_failure_is_not_retried(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent review failed: deterministic; recovery held",
        )
        self.assertEqual(decision.action, "hold")
        self.assertIsNone(decision.attempt)

    def test_non_green_ci_never_dispatches(self):
        decision = recovery.decide_recovery(
            ci_green=False,
            operation_state=None,
        )
        self.assertEqual(decision.action, "wait")

    def test_attempt_exhaustion_holds(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="transient; recovery eligible",
            evidence=recovery.RetryEvidence(latest_attempt=2),
        )
        self.assertEqual(decision.action, "exhaust")

    def test_durable_exhausted_marker_holds_future_wakeups(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="transient; recovery eligible",
            evidence=recovery.RetryEvidence(latest_attempt=2, exhausted=True),
        )
        self.assertEqual(decision.action, "hold")

    def test_successful_operation_is_settled(self):
        decision = recovery.decide_recovery(
            ci_green=True,
            operation_state="success",
        )
        self.assertEqual(decision.action, "settled")

    def test_backoff_matches_three_execution_budget(self):
        self.assertEqual(recovery.MAX_EXECUTIONS, 3)
        self.assertEqual(recovery.backoff_seconds(0), 0)
        self.assertEqual(recovery.backoff_seconds(1), 15)
        self.assertEqual(recovery.backoff_seconds(2), 30)


if __name__ == "__main__":
    unittest.main()
