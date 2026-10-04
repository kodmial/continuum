"""Deterministic tests for the generic lifecycle self-healing contract (#224)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from continuum import lifecycle_recovery as lifecycle  # noqa: E402
from continuum import pr_agent_recovery as legacy  # noqa: E402


HEAD = "a" * 40
OLD_HEAD = "b" * 40
REPO = "kodmial/continuum"


def comment(body: str, *, association: str = "OWNER", age_seconds: int = 0) -> dict:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "body": body,
        "author_association": association,
        "created_at": when.isoformat(),
    }


class IdentityTests(unittest.TestCase):
    def test_identity_is_repo_pr_head_and_kind(self):
        key = lifecycle.operation_key(REPO, 224, HEAD.upper(), "automerge")
        self.assertEqual(key, f"{REPO}#224:{HEAD}:automerge")
        self.assertNotEqual(
            lifecycle.operation_key(REPO, 224, HEAD, "review"),
            lifecycle.operation_key(REPO, 224, HEAD, "repair"),
        )
        self.assertNotEqual(
            lifecycle.operation_key(REPO, 224, HEAD, "review"),
            lifecycle.operation_key(REPO, 224, OLD_HEAD, "review"),
        )
        self.assertNotEqual(
            lifecycle.operation_key(REPO, 224, HEAD, "review"),
            lifecycle.operation_key(REPO, 225, HEAD, "review"),
        )
        self.assertNotEqual(
            lifecycle.operation_key(REPO, 224, HEAD, "review"),
            lifecycle.operation_key("other/repo", 224, HEAD, "review"),
        )

    def test_invalid_identity_fails_closed(self):
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.operation_key("not-a-repo", 1, HEAD, "review")
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.operation_key(REPO, 0, HEAD, "review")
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.operation_key(REPO, 1, "not-a-sha", "review")
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.operation_key(REPO, 1, HEAD, "merge-everything")

    def test_concurrency_key_is_per_pr_head(self):
        self.assertEqual(
            lifecycle.concurrency_key(REPO, 1, HEAD),
            lifecycle.concurrency_key(REPO.upper(), 1, HEAD.upper()),
        )
        self.assertNotEqual(
            lifecycle.concurrency_key(REPO, 1, HEAD),
            lifecycle.concurrency_key(REPO, 2, HEAD),
        )
        self.assertNotEqual(
            lifecycle.concurrency_key(REPO, 1, HEAD),
            lifecycle.concurrency_key(REPO, 1, OLD_HEAD),
        )


class TransientClassificationTests(unittest.TestCase):
    def test_rate_limit_403_with_empty_budget_is_transient(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1780000000"},
            error="API rate limit exceeded",
        )
        self.assertTrue(classified.transient)
        self.assertEqual(classified.ratelimit_reset_epoch, 1780000000)

    def test_plain_403_is_not_transient(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=403, headers={}, error="Resource not accessible by integration"
        )
        self.assertFalse(classified.transient)

    def test_429_honors_retry_after(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=429, headers={"Retry-After": "120"}, error="too many requests"
        )
        self.assertTrue(classified.transient)
        self.assertEqual(classified.retry_after_seconds, 120)

    def test_5xx_network_and_cancelled_are_transient(self):
        for kwargs in (
            {"status": 503, "error": "service unavailable"},
            {"status": 502, "error": "bad gateway"},
            {"error": "read timeout after 30s"},
            {"error": "connection reset by peer"},
            {"run_conclusion": "cancelled"},
            {"run_conclusion": "timed_out"},
            {"run_conclusion": "startup_failure"},
            {"error": "runner evicted by the provider"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertTrue(
                    lifecycle.classify_infrastructure_failure(**kwargs).transient
                )

    def test_deterministic_outcomes_never_consume_budget(self):
        for kwargs in (
            {"ci_failed": True},
            {"has_unresolved_findings": True},
            {"has_merge_conflict": True},
            {"malformed_state": True},
            {"head_moved": True},
            {"description": "3 current key issue(s) remain"},
            {"description": "current-head CI test failure"},
        ):
            with self.subTest(kwargs=kwargs):
                classified = lifecycle.classify_operation_failure(**kwargs)
                self.assertFalse(classified.transient)


class ResetAwareBackoffTests(unittest.TestCase):
    def test_three_attempt_budget_with_exponential_backoff(self):
        self.assertEqual(lifecycle.MAX_TRANSIENT_ATTEMPTS, 3)
        self.assertEqual(lifecycle.exponential_backoff_seconds(0), 0)
        self.assertEqual(lifecycle.exponential_backoff_seconds(1), 15)
        self.assertEqual(lifecycle.exponential_backoff_seconds(2), 30)

    def test_retry_after_and_reset_are_minimum_times(self):
        now = 1000
        base = lifecycle.next_retry_delay_seconds(attempt=1, now_epoch=now)
        delayed = lifecycle.next_retry_delay_seconds(
            attempt=1, retry_after_seconds=300, now_epoch=now
        )
        self.assertGreaterEqual(delayed, 300)
        self.assertGreaterEqual(delayed, base)
        reset_delay = lifecycle.next_retry_delay_seconds(
            attempt=0, ratelimit_reset_epoch=now + 600, now_epoch=now
        )
        self.assertGreaterEqual(reset_delay, 600)

    def test_long_waits_defer_instead_of_sleeping(self):
        self.assertFalse(lifecycle.should_defer_dispatch(0))
        self.assertFalse(lifecycle.should_defer_dispatch(60))
        self.assertTrue(lifecycle.should_defer_dispatch(61))
        self.assertTrue(lifecycle.should_defer_dispatch(3600))

    def test_jitter_is_bounded_and_deterministic(self):
        first = lifecycle.jitter_seconds("k", 1)
        self.assertEqual(first, lifecycle.jitter_seconds("k", 1))
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, lifecycle.JITTER_CAP_SECONDS)


class LatestStateReconciliationTests(unittest.TestCase):
    def test_rate_limit_failure_resumes_exact_head(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=403,
            headers={"X-RateLimit-Remaining": "0"},
            error="quota exhaustion",
        )
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="auto-merge failed: transient",
            failure_transient=classified.transient,
            retry_after_seconds=classified.retry_after_seconds,
            ratelimit_reset_epoch=classified.ratelimit_reset_epoch,
        )
        self.assertEqual(decision.action, "dispatch")

    def test_429_defers_dispatch_without_sleeping_runner(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=429, headers={"Retry-After": "3600"}, error="slow down"
        )
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=classified.transient,
            retry_after_seconds=classified.retry_after_seconds,
            now_epoch=1000,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertTrue(decision.defer_dispatch)
        self.assertIsNotNone(decision.not_before_epoch)

    def test_cancelled_run_gets_bounded_retry(self):
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion="cancelled",
            failure_transient=True,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertLess(decision.attempt, lifecycle.MAX_TRANSIENT_ATTEMPTS)

    def test_deterministic_ci_failure_does_not_burn_budget(self):
        classified = lifecycle.classify_operation_failure(ci_failed=True)
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="CI test failure",
            failure_transient=classified.transient,
            evidence=lifecycle.RetryEvidence(),
        )
        self.assertEqual(decision.action, "hold")
        self.assertIsNone(decision.attempt)

    def test_lost_wakeup_resumes_via_watchdog(self):
        decision = lifecycle.decide_recovery(ci_green=True, operation_state=None)
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 0)

    def test_duplicate_wakeups_coalesce_to_one_operation(self):
        key = lifecycle.concurrency_key(REPO, 224, HEAD)
        self.assertTrue(lifecycle.should_coalesce([key], key))
        self.assertFalse(lifecycle.should_coalesce([], key))
        self.assertEqual(lifecycle.dedupe_wakeups([key, key, key]), [key])
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            active_exact_lease=True,
        )
        self.assertEqual(decision.action, "wait")

    def test_old_head_recovery_cannot_mutate_new_head(self):
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            head_moved=True,
        )
        self.assertEqual(decision.action, "hold")

    def test_success_clears_recovery_state(self):
        self.assertTrue(lifecycle.forward_progress_clears("success"))
        self.assertTrue(lifecycle.forward_progress_clears("SUCCESS"))
        self.assertFalse(lifecycle.forward_progress_clears("failure"))
        self.assertFalse(lifecycle.forward_progress_clears(None))
        decision = lifecycle.decide_recovery(
            ci_green=True, operation_state="success"
        )
        self.assertEqual(decision.action, "settled")

    def test_attempt_exhaustion_holds_observably(self):
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2),
        )
        self.assertEqual(decision.action, "exhaust")
        held = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2, exhausted=True),
        )
        self.assertEqual(held.action, "hold")

    def test_two_prs_recover_without_global_starvation(self):
        first = lifecycle.concurrency_key(REPO, 1, HEAD)
        second = lifecycle.concurrency_key(REPO, 2, HEAD)
        self.assertFalse(lifecycle.should_coalesce([first], second))
        first_decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            active_exact_lease=False,
        )
        second_decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            active_exact_lease=False,
        )
        self.assertEqual(first_decision.action, "dispatch")
        self.assertEqual(second_decision.action, "dispatch")


class DurableEvidenceTests(unittest.TestCase):
    def test_changed_head_invalidates_stale_retry_state(self):
        comments = [comment(lifecycle.retry_marker(OLD_HEAD, "review", 2))]
        evidence = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertIsNone(evidence.latest_attempt)
        self.assertFalse(evidence.exhausted)

    def test_legacy_pr_agent_markers_still_count(self):
        comments = [
            comment(f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->")
        ]
        evidence = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertEqual(evidence.latest_attempt, 1)
        # Legacy markers never leak across kinds.
        repair = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="repair")
        self.assertIsNone(repair.latest_attempt)

    def test_external_comment_cannot_forge_budget(self):
        comments = [
            comment(
                lifecycle.retry_marker(HEAD, "review", 99), association="CONTRIBUTOR"
            )
        ]
        evidence = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertIsNone(evidence.latest_attempt)

    def test_not_before_is_durable_across_runs(self):
        marker = lifecycle.retry_marker(HEAD, "review", 1, not_before_epoch=2000)
        evidence = lifecycle.retry_evidence(
            [comment(marker)], head_sha=HEAD, kind="review"
        )
        self.assertEqual(evidence.not_before_epoch, 2000)
        early = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=evidence,
            now_epoch=1000,
        )
        self.assertEqual(early.action, "wait")
        late = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=evidence,
            now_epoch=3000,
        )
        self.assertEqual(late.action, "dispatch")


class TokenPolicyTests(unittest.TestCase):
    def test_reads_use_github_token_and_mutations_require_pat(self):
        for action in ("read", "list", "get", "discover", "scan", "status"):
            with self.subTest(action=action):
                self.assertFalse(lifecycle.requires_pat(action))
        for action in ("merge", "dispatch", "comment", "label", "push", "update_branch"):
            with self.subTest(action=action):
                self.assertTrue(lifecycle.requires_pat(action))

    def test_recovery_scans_do_not_use_pat(self):
        recovery = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("READ_GITHUB_TOKEN", recovery)
        self.assertIn("withReadFallback", recovery)
        automerge = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", automerge)
        self.assertIn("new github.constructor({ auth: readToken, baseUrl: readBaseUrl })", automerge)
        self.assertIn("async function withReadFallback(fn)", automerge)
        for read_call in (
            "client.rest.pulls.get",
            "client.rest.pulls.list",
            "client.rest.actions.listWorkflowRunsForRepo",
            "client.rest.repos.getCombinedStatusForRef",
            "client.rest.issues.listComments",
            "client.rest.pulls.listReviews",
            "client.rest.repos.getCommit",
            "client.rest.repos.getBranch",
        ):
            with self.subTest(read_call=read_call):
                self.assertIn(read_call, automerge)
        # Mutations and dispatches stay on the PAT-authenticated client.
        for mutation in (
            "github.rest.pulls.merge",
            "github.rest.issues.createComment",
            "github.rest.issues.addLabels",
            "github.rest.actions.createWorkflowDispatch",
        ):
            with self.subTest(mutation=mutation):
                self.assertIn(mutation, automerge)

    def read(self, path: str) -> str:
        with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
            return handle.read()


class CrossStackContractTests(unittest.TestCase):
    def read(self, path: str) -> str:
        with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_one_contract_covers_every_lifecycle_kind(self):
        for kind in ("review", "repair", "review-ready", "automerge", "main-sync", "merge", "ci-repair"):
            with self.subTest(kind=kind):
                key = lifecycle.operation_key(REPO, 224, HEAD, kind)
                self.assertIn(kind, key)

    def test_provider_policy_semantics_are_unchanged(self):
        # The generic contract reuses the legacy PR-Agent budget and trust
        # roots; provider gating itself is untouched.
        self.assertEqual(
            lifecycle.MAX_TRANSIENT_ATTEMPTS, legacy.MAX_EXECUTIONS
        )
        self.assertEqual(
            lifecycle.TRUSTED_ASSOCIATIONS, legacy.TRUSTED_ASSOCIATIONS
        )
        self.assertEqual(
            lifecycle.RETRYABLE_RUN_CONCLUSIONS, legacy.RETRYABLE_RUN_CONCLUSIONS
        )

    def test_event_driven_wakeups_remain_primary_with_schedule_safety_net(self):
        for stub in (
            ".github/caller-stubs/continuum-pr-agent-recovery.yml",
            ".github/caller-stubs/continuum-auto-merge.yml",
        ):
            with self.subTest(stub=stub):
                body = self.read(stub)
                self.assertIn("cron:", body)
        recovery_stub = self.read(
            ".github/caller-stubs/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("workflow_run:", recovery_stub)
        self.assertIn("pull_request_target:", recovery_stub)

    def test_recovery_is_exact_head_and_fail_closed(self):
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.normalize_head("not-a-sha")
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.normalize_kind("merge-everything")
        unknown = lifecycle.decide_recovery(
            ci_green=True, operation_state="weird-state"
        )
        self.assertEqual(unknown.action, "hold")

    def test_workflows_share_one_recovery_contract(self):
        recovery = self.read(
            ".github/workflows/continuum-pr-agent-recovery.yml"
        )
        automerge = self.read(".github/workflows/continuum-auto-merge.yml")
        # Generic transient classification lives in both reconcilers.
        for needle in ("isTransientApiError", "x-ratelimit-remaining"):
            with self.subTest(needle=needle):
                self.assertIn(needle, recovery)
                self.assertIn(needle, automerge)
        # Reset-aware scheduling never sleeps a runner through a long wait.
        self.assertIn("MAX_INLINE_WAIT_SECONDS", recovery)
        self.assertIn("resetAwareDelaySeconds", recovery)
        self.assertIn("not-before=", recovery)
        self.assertIn("scheduled safety net will redispatch", recovery)
        # Per-PR/HEAD leases coalesce duplicate wakeups in both reconcilers.
        for needle in ("tryAcquireLease", "leaseKey", "ownedLeases"):
            with self.subTest(needle=needle):
                self.assertIn(needle, recovery)
                self.assertIn(needle, automerge)
        # No repository-global cancellation starves unrelated PRs: the
        # auto-merge reconciler queues instead of cancelling, and the
        # per-PR/HEAD lease inside is authoritative.
        self.assertIn("cancel-in-progress: false", automerge)


if __name__ == "__main__":
    unittest.main()
