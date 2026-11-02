"""Observation tests for the contract qualification probes (#286)."""

import unittest
from datetime import datetime, timedelta, timezone

from continuum import contract_observation as observation
from continuum import contract_qualification as contract

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def iso(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def issue(number=7, state="open", labels=("automation:in-progress",), created=60, updated=30):
    return {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "created_at": iso(created),
        "updated_at": iso(updated),
        "closed_at": iso(updated) if state == "closed" else None,
    }


class ReservationObservationTest(unittest.TestCase):
    def test_live_run_is_attributable_progress(self):
        reservation = observation.observe_reservation(
            issue(),
            runs=[{
                "databaseId": 11,
                "status": "in_progress",
                "created_at": iso(120),
                "head_branch": "opencode/issue7-fix",
            }],
            now=NOW,
        )
        self.assertEqual(reservation.live_run_id, 11)
        self.assertEqual(reservation.worker_state, "running")
        self.assertEqual(
            contract.evaluate_reservation_liveness(reservation).outcome,
            "progress",
        )

    def test_label_without_live_work_is_stranded(self):
        reservation = observation.observe_reservation(issue(), now=NOW)
        self.assertTrue(reservation.has_label)
        self.assertEqual(
            contract.evaluate_reservation_liveness(reservation).outcome,
            contract.ERROR,
        )

    def test_stale_running_lease_routes_to_recovery(self):
        reservation = observation.observe_reservation(
            issue(),
            runs=[{
                "databaseId": 12,
                "status": "in_progress",
                "created_at": iso(4 * 60 * 60),
                "head_branch": "opencode/issue7-fix",
            }],
            now=NOW,
            lease_seconds=45 * 60,
        )
        self.assertTrue(reservation.lease_stale)
        self.assertEqual(
            contract.evaluate_reservation_liveness(reservation).outcome,
            "recover",
        )

    def test_terminal_worker_never_strands_the_issue(self):
        reservation = observation.observe_reservation(
            issue(),
            runs=[{
                "databaseId": 13,
                "status": "completed",
                "conclusion": "failure",
                "created_at": iso(120),
                "head_branch": "opencode/issue7-fix",
            }],
            now=NOW,
        )
        self.assertEqual(reservation.worker_state, "failed")
        self.assertEqual(
            contract.evaluate_reservation_liveness(reservation).outcome,
            "recover",
        )

    def test_open_pr_is_attributable_live_work(self):
        reservation = observation.observe_reservation(
            issue(),
            pull_requests=[{
                "number": 21,
                "state": "open",
                "body": "Closes #7",
                "headRefName": "opencode/issue7-fix",
            }],
            now=NOW,
        )
        self.assertEqual(reservation.live_pr_number, 21)
        self.assertEqual(
            contract.evaluate_reservation_liveness(reservation).outcome,
            "progress",
        )

    def test_select_observations_keeps_only_reserved_issues(self):
        selected = observation.select_observations(
            [issue(), issue(number=8, labels=())]
        )
        self.assertEqual([item["number"] for item in selected], [7])


class CanaryObservationTest(unittest.TestCase):
    def test_admission_is_measured_from_the_issue(self):
        observed = observation.observe_canary(issue(created=60), now=NOW)
        self.assertFalse(observed.completed)
        self.assertEqual(observed.state.stage, "admission")
        self.assertGreaterEqual(observed.state.stage_elapsed_seconds, 60)

    def test_worker_stage_uses_the_active_run(self):
        observed = observation.observe_canary(
            issue(),
            runs=[{
                "databaseId": 31,
                "status": "in_progress",
                "name": "OpenCode agent",
                "created_at": iso(90),
                "head_branch": "opencode/issue7-fix",
            }],
            now=NOW,
        )
        self.assertEqual(observed.state.stage, "worker")
        self.assertEqual(observed.state.run_id, 31)
        self.assertEqual(
            contract.evaluate_canary(observed.state).outcome,
            contract.CANARY_PROGRESS,
        )

    def test_non_worker_active_run_is_ci(self):
        observed = observation.observe_canary(
            issue(),
            runs=[{
                "databaseId": 32,
                "status": "in_progress",
                "name": "CI",
                "created_at": iso(90),
                "head_branch": "opencode/issue7-fix",
            }],
            now=NOW,
        )
        self.assertEqual(observed.state.stage, "ci")

    def test_approved_open_pr_is_the_merge_stage(self):
        observed = observation.observe_canary(
            issue(),
            pull_requests=[{
                "number": 41,
                "state": "open",
                "body": "Closes #7",
                "headRefName": "opencode/issue7-fix",
                "reviewDecision": "APPROVED",
                "updated_at": iso(30),
            }],
            now=NOW,
        )
        self.assertEqual(observed.state.stage, "merge")
        self.assertEqual(observed.state.pr_number, 41)

    def test_closed_issue_with_merged_pr_completes(self):
        observed = observation.observe_canary(
            issue(state="closed"),
            pull_requests=[{
                "number": 41,
                "state": "closed",
                "body": "Closes #7",
                "mergedAt": iso(20),
            }],
            now=NOW,
        )
        self.assertTrue(observed.completed)
        self.assertIsNone(observed.state)

    def test_closed_issue_without_merged_pr_fails(self):
        observed = observation.observe_canary(issue(state="closed"), now=NOW)
        self.assertFalse(observed.completed)
        self.assertIn("without a merged implementation PR", observed.failure)

    def test_recovery_attempts_are_counted_from_markers(self):
        observed = observation.observe_canary(
            issue(),
            comments=[
                {"body": "<!-- continuum-canary-recovery issue=7 attempt=1 -->"},
                {"body": "<!-- continuum-canary-recovery issue=7 attempt=2 -->"},
                {"body": "ordinary comment"},
            ],
            now=NOW,
        )
        self.assertEqual(observed.state.recovery_attempts, 2)


if __name__ == "__main__":
    unittest.main()
