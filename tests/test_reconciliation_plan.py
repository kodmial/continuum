"""Read-only repository-wide planning, pagination, and dry-run tests (#343)."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from continuum.lifecycle_recovery import RetryEvidence
from continuum.reconciliation_adapters import ACTION, RETRY, TERMINAL, WAIT
from continuum.reconciliation_plan import (
    Continuation,
    PlanContext,
    ReconciliationPlanError,
    SnapshotStateSource,
    StatePage,
    plan_repository,
    plan_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "reconciliation"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def load_trace(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def context_for(trace, *, event=None):
    payload = dict(trace["context"])
    payload["now"] = NOW
    if event is not None:
        payload["event"] = event
    return PlanContext(**payload)


class FixtureTraceTests(unittest.TestCase):
    def assert_trace(self, trace):
        plan = plan_snapshot(
            trace["issues"],
            trace["pull_requests"],
            context=context_for(trace),
        )
        kinds = {decision.subject: decision.kind for decision in plan.decisions}
        for subject, expected_kind in trace["expect"].items():
            if subject.startswith("issue:"):
                self.assertEqual(kinds[subject], expected_kind, subject)
        pr_subject = next(subject for subject in kinds if subject.startswith("pull_request:"))
        domains = {
            decision.domain
            for decision in plan.decisions
            if decision.subject == pr_subject
        }
        for expected in trace["expect"]["pull_request:%s domains" % pr_subject.split(":")[1]]:
            self.assertIn(expected, domains)
        return plan

    def test_continuum_trace_plans_beyond_the_unrelated_wakeup(self):
        self.assert_trace(load_trace("continuum-trace.json"))

    def test_consumer_trace_plans_beyond_the_unrelated_wakeup(self):
        self.assert_trace(load_trace("consumer-trace.json"))

    def test_unrelated_event_does_not_change_any_decision(self):
        for name in ("continuum-trace.json", "consumer-trace.json"):
            trace = load_trace(name)
            without_event = plan_snapshot(
                trace["issues"],
                trace["pull_requests"],
                context=context_for(trace),
            )
            with_event = plan_snapshot(
                trace["issues"],
                trace["pull_requests"],
                context=context_for(trace, event=trace["event"]),
            )
            self.assertEqual(
                [decision.to_dict() for decision in without_event.decisions],
                [decision.to_dict() for decision in with_event.decisions],
            )

    def test_dry_run_never_mutates_the_source_snapshot(self):
        trace = load_trace("continuum-trace.json")
        issues = copy.deepcopy(trace["issues"])
        pulls = copy.deepcopy(trace["pull_requests"])
        source = SnapshotStateSource(issues, pulls)
        plan = plan_repository(source, context=context_for(trace))
        self.assertTrue(plan.dry_run)
        self.assertTrue(any(decision.kind == ACTION for decision in plan.decisions))
        self.assertEqual(issues, trace["issues"])
        self.assertEqual(pulls, trace["pull_requests"])
        for forbidden in ("dispatch", "merge", "write", "comment", "publish"):
            self.assertFalse(hasattr(source, forbidden), forbidden)


class PaginationTests(unittest.TestCase):
    def make_source(self, issue_count=250, pull_count=150):
        issues = [
            {"number": number, "title": "P1: issue", "labels": ["P1"], "state": "open"}
            for number in range(1, issue_count + 1)
        ]
        pulls = [
            {
                "number": 1000 + number,
                "title": "fix: pr",
                "state": "open",
                "head_sha": ("%040x" % number),
                "head_repo": "o/r",
                "base_repo": "o/r",
                "mergeable": True,
                "mergeable_state": "clean",
            }
            for number in range(1, pull_count + 1)
        ]
        return SnapshotStateSource(issues, pulls)

    def test_bounded_pages_resume_from_persisted_continuation(self):
        source = self.make_source()
        context = PlanContext(now=NOW)
        seen_issues = set()
        seen_pulls = set()
        continuation = None
        passes = 0
        while True:
            plan = plan_repository(
                source,
                context=context,
                continuation=continuation,
                page_size=100,
                max_pages_per_pass=1,
            )
            passes += 1
            seen_issues.update(
                decision.subject for decision in plan.decisions if decision.subject.startswith("issue:")
            )
            seen_pulls.update(
                decision.subject for decision in plan.decisions if decision.subject.startswith("pull_request:")
            )
            continuation = plan.continuation
            self.assertLessEqual(plan.pages_read, 2)
            if continuation.issues_exhausted and continuation.pull_requests_exhausted:
                break
            self.assertLess(passes, 10)
        self.assertEqual(len(seen_issues), 250)
        self.assertEqual(len(seen_pulls), 150)
        self.assertEqual(passes, 3)
        self.assertEqual(continuation.completed_passes, 3)

    def test_max_pages_bounds_one_pass_and_keeps_a_cursor(self):
        source = self.make_source()
        plan = plan_repository(
            source,
            context=PlanContext(now=NOW),
            page_size=100,
            max_pages_per_pass=2,
        )
        self.assertEqual(plan.issues_scanned, 200)
        self.assertEqual(plan.continuation.issue_cursor, "200")
        self.assertFalse(plan.continuation.issues_exhausted)
        self.assertEqual(plan.pages_read, 4)

    def test_continuation_round_trips_through_json_state(self):
        continuation = Continuation(
            issue_cursor="200",
            pull_cursor=None,
            issues_exhausted=False,
            pull_requests_exhausted=True,
            completed_passes=4,
        )
        self.assertEqual(
            Continuation.from_dict(continuation.to_dict()),
            continuation,
        )
        self.assertEqual(continuation.restart().issue_cursor, None)

    def test_non_advancing_cursor_fails_closed(self):
        class StuckSource:
            def list_issues(self, cursor, limit):
                return StatePage(({"number": 1, "state": "open"},), "same")

            def list_pull_requests(self, cursor, limit):
                return StatePage((), None)

        with self.assertRaises(ReconciliationPlanError):
            plan_repository(StuckSource(), context=PlanContext(now=NOW))

    def test_invalid_bounds_are_rejected(self):
        source = self.make_source(1, 1)
        with self.assertRaises(ReconciliationPlanError):
            plan_repository(source, context=PlanContext(now=NOW), page_size=0)
        with self.assertRaises(ReconciliationPlanError):
            plan_repository(source, context=PlanContext(now=NOW), max_pages_per_pass=0)


class OwnerRaceGuardContextTests(unittest.TestCase):
    """The dry-run plan must keep the owner ``/oc`` race guard."""

    def race_comment(self, login, association):
        return {
            "body": "/oc",
            "author_association": association,
            "user": {"login": login},
            "created_at": NOW - timedelta(minutes=1),
        }

    def test_snapshot_repository_arms_owner_race_guard(self):
        comments = {2: [self.race_comment("repo-owner", "NONE")]}
        plan = plan_snapshot(
            [{"number": 2, "title": "P1: issue", "labels": ["P1"], "state": "open"}],
            [],
            context=PlanContext(
                now=NOW,
                repository="repo-owner/repository",
                comments_by_issue=comments,
            ),
        )
        self.assertEqual(plan.by_subject("issue:2")[0].kind, WAIT)
        self.assertIn("race window", plan.by_subject("issue:2")[0].reason)

    def test_missing_snapshot_repository_keeps_association_race_guard(self):
        comments = {2: [self.race_comment("repo-owner", "OWNER")]}
        plan = plan_snapshot(
            [{"number": 2, "title": "P1: issue", "labels": ["P1"], "state": "open"}],
            [],
            context=PlanContext(now=NOW, comments_by_issue=comments),
        )
        self.assertEqual(plan.by_subject("issue:2")[0].kind, WAIT)
        self.assertIn("race window", plan.by_subject("issue:2")[0].reason)


def plan_one_pr(*, ci_run, per_pr):
    """Plan a single open PR with explicit CI and per-PR state."""

    pr = {
        "number": 201,
        "title": "fix: one",
        "state": "open",
        "head_sha": "1" * 40,
        "head_repo": "kodmial/continuum",
        "base_repo": "kodmial/continuum",
        "mergeable": True,
        "mergeable_state": "clean",
    }
    state = dict(per_pr)
    state.setdefault("provider", "pr-agent")
    return plan_snapshot(
        (),
        (pr,),
        context=PlanContext(
            now=NOW,
            review_provider="pr-agent",
            repository="kodmial/continuum",
            ci_runs={201: ci_run},
            pull_request_state={201: state},
        ),
    )


def decision_for(plan, domain):
    matches = [item for item in plan.decisions if item.domain == domain]
    if len(matches) != 1:
        raise AssertionError("expected exactly one {} decision, got {}".format(
            domain, [item.domain for item in plan.decisions]))
    return matches[0]


class RetryEvidenceIsolationTests(unittest.TestCase):
    """kodmial/continuum#432: CI-repair and PR-Agent evidence stay separate."""

    def test_pr_agent_exhaustion_does_not_block_ci_repair(self):
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "failure"},
            per_pr={
                "pr_agent_retry_evidence": RetryEvidence(exhausted=True),
                "operation_state": "failure",
                "failure_transient": True,
            },
        )
        self.assertEqual(decision_for(plan, "ci-repair").kind, ACTION)
        # PR-Agent still waits on the red exact HEAD rather than reading the
        # other operation's budget.
        self.assertEqual(decision_for(plan, "pr-agent-recovery").kind, WAIT)

    def test_ci_repair_exhaustion_does_not_block_pr_agent_recovery(self):
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "success"},
            per_pr={
                "ci_repair_retry_evidence": RetryEvidence(exhausted=True),
                "operation_state": "failure",
                "failure_transient": True,
            },
        )
        self.assertEqual(decision_for(plan, "pr-agent-recovery").kind, ACTION)

    def test_ci_repair_still_reads_its_own_budget(self):
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "failure"},
            per_pr={"ci_repair_retry_evidence": RetryEvidence(exhausted=True)},
        )
        self.assertEqual(decision_for(plan, "ci-repair").kind, TERMINAL)

    def test_pr_agent_recovery_still_reads_its_own_budget(self):
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "success"},
            per_pr={
                "pr_agent_retry_evidence": RetryEvidence(exhausted=True),
                "operation_state": "failure",
                "failure_transient": True,
            },
        )
        self.assertEqual(decision_for(plan, "pr-agent-recovery").kind, TERMINAL)

    def test_legacy_retry_evidence_key_still_feeds_ci_repair(self):
        # A caller that still writes the pre-split ``retry_evidence`` key must
        # keep its exhausted budget instead of silently starting over.
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "failure"},
            per_pr={"retry_evidence": RetryEvidence(exhausted=True)},
        )
        self.assertEqual(decision_for(plan, "ci-repair").kind, TERMINAL)

    def test_legacy_retry_evidence_key_still_feeds_pr_agent_recovery(self):
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "success"},
            per_pr={
                "retry_evidence": RetryEvidence(exhausted=True),
                "operation_state": "failure",
                "failure_transient": True,
            },
        )
        self.assertEqual(decision_for(plan, "pr-agent-recovery").kind, TERMINAL)


class PlanClockTests(unittest.TestCase):
    """kodmial/continuum#432: an unset now_epoch defaults to the plan clock."""

    def test_future_not_before_reports_a_retry_delay_from_the_plan_clock(self):
        not_before = int(NOW.timestamp()) + 300
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "success"},
            per_pr={
                "operation_state": "failure",
                "failure_transient": True,
                "not_before_epoch": not_before,
            },
        )
        decision = decision_for(plan, "pr-agent-recovery")
        self.assertEqual(decision.kind, RETRY)
        self.assertEqual(decision.retry_after_seconds, 300)

    def test_explicit_now_epoch_still_wins(self):
        not_before = int(NOW.timestamp()) + 300
        plan = plan_one_pr(
            ci_run={"status": "completed", "conclusion": "success"},
            per_pr={
                "operation_state": "failure",
                "failure_transient": True,
                "not_before_epoch": not_before,
                "now_epoch": not_before - 60,
            },
        )
        decision = decision_for(plan, "pr-agent-recovery")
        self.assertEqual(decision.kind, RETRY)
        self.assertEqual(decision.retry_after_seconds, 60)


if __name__ == "__main__":
    unittest.main()
