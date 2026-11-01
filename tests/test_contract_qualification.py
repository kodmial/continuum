"""Contract qualification tests (kodmial/continuum#286).

Proves the three-layer merge-blocking contract deterministically:

- Layer 1: a deliberately broken lifecycle change (red/stale/absent
  exact-HEAD evidence) is rejected before merge on every merge path.
- Layer 2: a deliberately stranded automation:in-progress reservation is
  detected and recovered (or fails loudly); duplicates coalesce; WAIT is
  distinct from ERROR; the watchdog advances stalled work; exact-HEAD
  evidence is required; both merge paths obey the same contract.
- Layer 3: the live canary detects absent forward progress within bounded
  deadlines, records exact IDs plus the stalled state, recovers within a
  bounded budget, and otherwise fails loudly.
"""

import unittest

from continuum import contract_qualification as contract

SHA_A = "a" * 40
SHA_B = "b" * 40


def gate_evidence(workflow, sha, status="completed", conclusion="success"):
    return contract.GateEvidence(
        workflow_name=workflow, head_sha=sha, status=status, conclusion=conclusion
    )


class RegressionGateTest(unittest.TestCase):
    def test_green_exact_head_passes(self):
        verdict = contract.evaluate_regression_gate(
            SHA_A,
            [gate_evidence("CI", SHA_A), gate_evidence("Contract qualification", SHA_A)],
        )
        self.assertEqual(verdict.outcome, contract.GATE_PASS)

    def test_broken_change_is_rejected(self):
        # A deliberately broken lifecycle change: red CI on the exact HEAD.
        verdict = contract.evaluate_regression_gate(
            SHA_A,
            [
                gate_evidence("CI", SHA_A, conclusion="failure"),
                gate_evidence("Contract qualification", SHA_A),
            ],
        )
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)

    def test_absent_contract_evidence_blocks(self):
        verdict = contract.evaluate_regression_gate(
            SHA_A, [gate_evidence("CI", SHA_A)]
        )
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)
        self.assertIn("contract-evidence-absent", verdict.reason)

    def test_stale_evidence_blocks(self):
        verdict = contract.evaluate_regression_gate(
            SHA_B,
            [gate_evidence("CI", SHA_A), gate_evidence("Contract qualification", SHA_A)],
        )
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)
        self.assertIn("stale", verdict.reason)

    def test_red_contract_evidence_blocks(self):
        verdict = contract.evaluate_regression_gate(
            SHA_A,
            [
                gate_evidence("CI", SHA_A),
                gate_evidence("Contract qualification", SHA_A, conclusion="failure"),
            ],
        )
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)

    def test_incomplete_status_blocks(self):
        verdict = contract.evaluate_regression_gate(
            SHA_A,
            [
                gate_evidence("CI", SHA_A, status="in_progress", conclusion=None),
                gate_evidence("Contract qualification", SHA_A),
            ],
        )
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)

    def test_invalid_head_blocks(self):
        verdict = contract.evaluate_regression_gate("short", [])
        self.assertEqual(verdict.outcome, contract.GATE_BLOCK)


class MergePathParityTest(unittest.TestCase):
    def green(self, sha=SHA_A):
        return [
            gate_evidence("CI", sha),
            gate_evidence("Contract qualification", sha),
        ]

    def test_both_merge_paths_share_one_contract(self):
        for provider in ("none", "coderabbit", "pr-agent"):
            allowed, _ = contract.merge_path_allowed(SHA_A, self.green(), provider)
            self.assertTrue(allowed, provider)

    def test_every_merge_path_blocked_without_evidence(self):
        for provider in ("none", "coderabbit", "pr-agent"):
            allowed, reason = contract.merge_path_allowed(SHA_A, [], provider)
            self.assertFalse(allowed, provider)
            self.assertIn("evidence", reason)

    def test_every_merge_path_blocked_on_stale_head(self):
        evidence = self.green(SHA_A)
        for provider in ("none", "coderabbit", "pr-agent"):
            allowed, _ = contract.merge_path_allowed(SHA_B, evidence, provider)
            self.assertFalse(allowed, provider)

    def test_every_merge_path_blocked_on_red(self):
        evidence = [
            gate_evidence("CI", SHA_A, conclusion="failure"),
            gate_evidence("Contract qualification", SHA_A),
        ]
        for provider in ("none", "coderabbit", "pr-agent"):
            allowed, _ = contract.merge_path_allowed(SHA_A, evidence, provider)
            self.assertFalse(allowed, provider)


class LifecycleLivenessTest(unittest.TestCase):
    def test_live_reservation_is_progress(self):
        verdict = contract.evaluate_reservation_liveness(
            contract.Reservation(issue_number=7, has_label=True, live_run_id=101)
        )
        self.assertEqual(verdict.outcome, "progress")

    def test_live_pr_and_lease_are_progress(self):
        for reservation in (
            contract.Reservation(issue_number=7, has_label=True, live_pr_number=12),
            contract.Reservation(issue_number=7, has_label=True, live_lease=True),
        ):
            verdict = contract.evaluate_reservation_liveness(reservation)
            self.assertEqual(verdict.outcome, "progress")

    def test_stranded_label_fails_loudly(self):
        # Deliberately stranded automation:in-progress: no live run/PR/lease.
        verdict = contract.evaluate_reservation_liveness(
            contract.Reservation(issue_number=7, has_label=True)
        )
        self.assertEqual(verdict.outcome, "error")
        self.assertIn("dead-label", verdict.reason)

    def test_stale_lease_is_recovered(self):
        verdict = contract.evaluate_reservation_liveness(
            contract.Reservation(
                issue_number=7, has_label=True, live_lease=True, lease_stale=True
            )
        )
        self.assertEqual(verdict.outcome, "recover")

    def test_terminal_workers_cannot_strand_issue(self):
        for state in ("completed", "failed", "cancelled", "timed_out"):
            verdict = contract.evaluate_reservation_liveness(
                contract.Reservation(
                    issue_number=7,
                    has_label=True,
                    live_run_id=101,
                    worker_state=state,
                )
            )
            self.assertEqual(verdict.outcome, "recover", state)

    def test_duplicate_wakeups_coalesce(self):
        merged = contract.coalesce_wakeups(["a", "A ", "b", "a", "b", "c"])
        self.assertEqual(merged, ["a", "b", "c"])

    def test_wait_is_distinguishable_from_error(self):
        self.assertEqual(contract.classify_wait_vs_error("running"), contract.WAIT)
        self.assertEqual(contract.classify_wait_vs_error("queued"), contract.WAIT)
        self.assertEqual(contract.classify_wait_vs_error(None), contract.ERROR)
        self.assertEqual(contract.classify_wait_vs_error("failed"), contract.ERROR)
        self.assertEqual(contract.classify_wait_vs_error("completed"), contract.ERROR)
        self.assertEqual(
            contract.classify_wait_vs_error("running", lease_stale=True), contract.ERROR
        )

    def test_watchdog_moves_stalled_work_forward(self):
        decision = contract.watchdog_advance(stalled=True, attempt=0)
        self.assertEqual(decision.action, "retry")
        moving = contract.watchdog_advance(stalled=False, attempt=0)
        self.assertEqual(moving.action, "advance")

    def test_watchdog_exhaustion_fails_loudly(self):
        decision = contract.watchdog_advance(
            stalled=True, attempt=contract.MAX_CANARY_RECOVERY_ATTEMPTS
        )
        self.assertEqual(decision.action, "exhaust")

    def test_lifecycle_path_is_end_to_end(self):
        stages = contract.lifecycle_path_stages()
        self.assertEqual(
            list(stages),
            ["admission", "worker", "ci", "review", "repair", "merge", "reconciliation"],
        )
        ok, _ = contract.progress_events_cover_lifecycle({stage: True for stage in stages})
        self.assertTrue(ok)
        ok, reason = contract.progress_events_cover_lifecycle({"worker": True})
        self.assertFalse(ok)
        self.assertIn("missing-stages", reason)


class LiveCanaryTest(unittest.TestCase):
    def test_canary_proves_forward_progress(self):
        verdict = contract.evaluate_canary(
            contract.CanaryState(
                canary_id="canary-1", issue_number=1, stage="worker",
                stage_elapsed_seconds=60,
            )
        )
        self.assertEqual(verdict.outcome, contract.CANARY_PROGRESS)

    def test_canary_detects_stall_and_recovers_within_budget(self):
        verdict = contract.evaluate_canary(
            contract.CanaryState(
                canary_id="canary-1", issue_number=1, pr_number=2, run_id=3,
                stage="worker",
                stage_elapsed_seconds=10 * 3600,
                recovery_attempts=0,
            )
        )
        self.assertEqual(verdict.outcome, contract.CANARY_STALLED_RECOVERABLE)

    def test_canary_failure_records_exact_ids_and_stalled_state(self):
        verdict = contract.evaluate_canary(
            contract.CanaryState(
                canary_id="canary-9", issue_number=11, pr_number=22, run_id=33,
                stage="review",
                stage_elapsed_seconds=10 * 3600,
                recovery_attempts=contract.MAX_CANARY_RECOVERY_ATTEMPTS,
            )
        )
        self.assertEqual(verdict.outcome, contract.CANARY_STALLED_FAILED)
        for token in ("canary-9", "11", "22", "33", "review"):
            self.assertIn(token, verdict.detail)

    def test_unknown_stage_fails_closed(self):
        verdict = contract.evaluate_canary(
            contract.CanaryState(canary_id="canary-1", stage="nope", stage_elapsed_seconds=0)
        )
        self.assertEqual(verdict.outcome, contract.CANARY_STALLED_FAILED)

    def test_every_stage_has_a_bounded_deadline(self):
        for stage in contract.lifecycle_path_stages():
            deadline = contract.canary_stage_deadline(stage)
            self.assertIsNotNone(deadline, stage)
            self.assertGreater(deadline, 0)


if __name__ == "__main__":
    unittest.main()
