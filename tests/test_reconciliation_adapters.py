"""Deterministic tests for state-derived reconciliation adapters (#343)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from continuum import reconciliation_adapters as adapters
from continuum.lifecycle_recovery import RetryEvidence, retry_marker
from continuum.scheduler_reconcile import SchedulerConfig


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def issue(number=1, *, title="P1: task", labels=("P1",), state="open", comments=None):
    return {
        "number": number,
        "title": title,
        "labels": list(labels),
        "state": state,
        "body": "",
        "comments": list(comments or []),
    }


def pr(number=7, *, state="open", draft=False, head=HEAD, labels=(), mergeable=True):
    return {
        "number": number,
        "title": "fix: change",
        "state": state,
        "draft": draft,
        "labels": list(labels),
        "head_sha": head,
        "head_repo": "owner/repo",
        "base_repo": "owner/repo",
        "mergeable": mergeable,
        "mergeable_state": "clean" if mergeable else "dirty",
    }


class IssueAdmissionTests(unittest.TestCase):
    def test_runnable_issue_is_action_without_any_event_context(self):
        decisions = adapters.plan_issue_admissions([issue(2)], now=NOW)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].kind, adapters.ACTION)
        self.assertEqual(decisions[0].action, "dispatch_issue")
        self.assertEqual(decisions[0].subject, "issue:2")

    def test_unrelated_active_context_does_not_block_runnable_issue(self):
        decisions = adapters.plan_issue_admissions(
            [issue(1, labels=("P1", "automation:in-progress")), issue(2)],
            now=NOW,
            open_pr_issue_numbers=[1],
        )
        by_subject = {decision.subject: decision for decision in decisions}
        self.assertEqual(by_subject["issue:1"].kind, adapters.WAIT)
        self.assertEqual(by_subject["issue:2"].kind, adapters.ACTION)

    def test_owner_command_race_window_waits(self):
        comments = {
            2: [
                {
                    "body": "/oc please implement",
                    "author_association": "OWNER",
                    "created_at": NOW - timedelta(minutes=1),
                }
            ]
        }
        decision = adapters.decide_issue_admission(issue(2), now=NOW, comments=comments)
        self.assertEqual(decision.kind, adapters.WAIT)
        self.assertIn("race window", decision.reason)

    def test_declared_blocker_waits(self):
        decision = adapters.decide_issue_admission(
            issue(3),
            now=NOW,
            declared_open_blockers={3: [9]},
        )
        self.assertEqual(decision.kind, adapters.WAIT)
        self.assertIn("#9", decision.reason)

    def test_wip_exhaustion_is_no_progress_wait(self):
        decision = adapters.decide_issue_admission(
            issue(4),
            now=NOW,
            config=SchedulerConfig(wip_limit=0),
        )
        self.assertEqual(decision.kind, adapters.WAIT)
        self.assertTrue(decision.details.get("no_progress"))
        self.assertIn("WIP", decision.reason)

    def test_terminal_generation_stop_is_terminal_not_retry(self):
        decision = adapters.decide_issue_admission(
            issue(5),
            now=NOW,
            terminal_stopped={(5, 1)},
            generations={5: 1},
        )
        self.assertEqual(decision.kind, adapters.TERMINAL)
        self.assertIn("terminal generation stop", decision.reason)

    def test_closed_issue_is_terminal(self):
        decision = adapters.decide_issue_admission(issue(6, state="closed"), now=NOW)
        self.assertEqual(decision.kind, adapters.TERMINAL)
        self.assertIn("closed", decision.reason)

    def test_input_labels_and_comments_are_not_mutated(self):
        labels = ["P1"]
        comments = {
            7: [
                {
                    "body": "/oc",
                    "author_association": "OWNER",
                    "created_at": NOW - timedelta(minutes=30),
                }
            ]
        }
        record = issue(7, labels=labels)
        original_comments = [dict(item) for item in comments[7]]
        adapters.plan_issue_admissions([record], now=NOW, comments=comments)
        self.assertEqual(labels, ["P1"])
        self.assertEqual(comments[7], original_comments)
        self.assertEqual(record["labels"], ["P1"])


class ReviewAndRepairTests(unittest.TestCase):
    def test_review_ready_uses_state_not_event(self):
        decision = adapters.decide_review_ready(
            pr(),
            ci_run={"status": "completed", "conclusion": "success"},
            provider="none",
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        self.assertEqual(decision.action, "wake_auto_merge")

    def test_draft_pr_waits(self):
        decision = adapters.decide_review_ready(
            pr(draft=True),
            ci_run={"status": "completed", "conclusion": "success"},
        )
        self.assertEqual(decision.kind, adapters.WAIT)

    def test_ci_repair_is_action_for_current_head_failure(self):
        decision = adapters.decide_ci_repair(
            pr(),
            ci_run={"status": "completed", "conclusion": "failure"},
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        self.assertEqual(decision.action, "repair_exact_head_ci")

    def test_ci_repair_budget_exhaustion_is_terminal(self):
        decision = adapters.decide_ci_repair(
            pr(),
            ci_run={"status": "completed", "conclusion": "failure"},
            retry_evidence=RetryEvidence(latest_attempt=9),
        )
        self.assertEqual(decision.kind, adapters.TERMINAL)

    def test_conflict_repair_uses_canonical_conflict_policy(self):
        bad = pr(mergeable=False)
        decision = adapters.decide_ci_repair(
            bad,
            ci_run={"status": "completed", "conclusion": "success"},
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        self.assertEqual(decision.action, "repair_conflict")

    def test_coderabbit_queue_retries_during_cooldown(self):
        limited_at = NOW - timedelta(minutes=5)
        decision = adapters.decide_coderabbit_queue(
            pr(),
            comments=[
                {
                    "body": "Review rate limited. Next review available in 10 minutes.",
                    "user": {"login": "coderabbitai[bot]"},
                    "created_at": limited_at,
                    "updated_at": limited_at,
                }
            ],
            now_ms=int(NOW.timestamp() * 1000),
        )
        self.assertEqual(decision.kind, adapters.RETRY)
        self.assertGreater(decision.retry_after_seconds or 0, 0)

    def test_coderabbit_unresolved_requires_current_head_verification(self):
        comment = {
            "body": "**UNRESOLVED** finding",
            "user": {"login": "coderabbitai[bot]"},
            "in_reply_to_id": 99,
        }
        stale = adapters.decide_coderabbit_unresolved(
            pr(),
            review_comment=comment,
            verified_head=OTHER_HEAD,
        )
        self.assertEqual(stale.kind, adapters.WAIT)
        current = adapters.decide_coderabbit_unresolved(
            pr(),
            review_comment=comment,
            verified_head=HEAD,
        )
        self.assertEqual(current.kind, adapters.ACTION)

    def test_pr_agent_review_is_action_only_for_exact_open_head(self):
        decision = adapters.decide_pr_agent_review(
            pr(),
            ci_green_on_exact_head=True,
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        reviewed = adapters.decide_pr_agent_review(
            pr(),
            ci_green_on_exact_head=True,
            existing_review_head=HEAD,
        )
        self.assertEqual(reviewed.kind, adapters.TERMINAL)

    def test_pr_agent_recovery_maps_lifecycle_dispatch(self):
        decision = adapters.decide_pr_agent_recovery(
            pr(),
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            now_epoch=1000,
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        self.assertEqual(decision.action, "recover_operation")

    def test_pr_agent_recovery_deterministic_failure_is_terminal(self):
        decision = adapters.decide_pr_agent_recovery(
            pr(),
            ci_green=True,
            operation_state="failure",
            failure_transient=False,
        )
        self.assertEqual(decision.kind, adapters.TERMINAL)

    def test_exact_head_merge_gate_rejects_moved_head(self):
        moved = adapters.decide_pr_agent_merge(
            pr(head=OTHER_HEAD),
            ci_run={"status": "completed", "conclusion": "success"},
            reviewed_head=HEAD,
        )
        self.assertEqual(moved.kind, adapters.WAIT)
        exact = adapters.decide_pr_agent_merge(
            pr(head=HEAD),
            ci_run={"status": "completed", "conclusion": "success"},
            reviewed_head=HEAD,
        )
        self.assertEqual(exact.kind, adapters.ACTION)
        self.assertEqual(exact.details["sha"], HEAD)

    def test_no_auto_merge_is_terminal(self):
        decision = adapters.decide_pr_agent_merge(
            pr(labels=("no-auto-merge",)),
            ci_run={"status": "completed", "conclusion": "success"},
        )
        self.assertEqual(decision.kind, adapters.TERMINAL)


class QualificationDelegationAndWatchdogTests(unittest.TestCase):
    def test_qualification_dispatch_and_retry(self):
        dispatch = adapters.decide_qualification(
            subject="qualification:8",
            qualification_open=True,
            qualification_paused=False,
            capability_state="qualifying",
            already_dispatched_for_sha=False,
            required_sha=HEAD,
        )
        self.assertEqual(dispatch.kind, adapters.ACTION)
        retry = adapters.decide_qualification(
            subject="qualification:8",
            qualification_open=True,
            qualification_paused=True,
            capability_state="blocked",
            already_dispatched_for_sha=False,
            required_sha=HEAD,
            evidence_state="fail",
        )
        self.assertEqual(retry.kind, adapters.ACTION)
        self.assertEqual(retry.reason, "unpause-and-dispatch")

    def test_opaque_child_id_is_preserved_but_not_leaked_into_reason(self):
        child_id = "child_opaque_7f83a1"
        decision = adapters.decide_delegation(
            subject="child-task:42",
            delegation_state="ok",
            children=[{"id": child_id, "repository": "private/child"}],
            child_id=child_id,
        )
        self.assertEqual(decision.kind, adapters.ACTION)
        self.assertEqual(decision.details["child_id"], child_id)
        self.assertNotIn(child_id, decision.reason)

    def test_degraded_child_discovery_is_retry_not_silent_success(self):
        decision = adapters.decide_delegation(
            subject="child-task:42",
            delegation_state="degraded",
            children=[],
        )
        self.assertEqual(decision.kind, adapters.RETRY)

    def test_global_watchdog_respects_rate_limit(self):
        decision = adapters.decide_global_watchdog(
            subject="consumer:owner/repo",
            consumer_verified=True,
            open_pull_requests=1,
            rate_limit_remaining=0,
            rate_limit_reset_epoch=2000,
            now_epoch=1000,
        )
        self.assertEqual(decision.kind, adapters.RETRY)
        self.assertEqual(decision.retry_after_seconds, 1000)


if __name__ == "__main__":
    unittest.main()
