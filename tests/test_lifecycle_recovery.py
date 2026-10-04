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

    def test_bare_policy_words_are_not_transient(self):
        # Bare substrings such as "runner configuration unsupported" or
        # "network policy denied" are deterministic policy outcomes, not
        # infrastructure gaps, and must never consume the transient budget.
        for message in (
            "runner configuration unsupported",
            "network policy denied",
            "dns policy violation",
            "tls version not allowed",
            "bootstrap configuration invalid",
            "cancelled by user policy",
            "evicted by quota policy review",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    lifecycle.classify_infrastructure_failure(error=message).transient
                )
        # Qualified infrastructure signatures still retry.
        for message in (
            "runner evicted by the provider",
            "connection reset by peer",
            "tls handshake failed",
            "network unreachable",
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    lifecycle.classify_infrastructure_failure(error=message).transient
                )


class ResetAwareBackoffTests(unittest.TestCase):
    def test_ten_execution_budget_with_canonical_schedule(self):
        self.assertEqual(lifecycle.MAX_TRANSIENT_ATTEMPTS, 10)
        self.assertEqual(lifecycle.MAX_TRANSIENT_EXECUTIONS, 10)
        self.assertEqual(
            lifecycle.retry_delay_schedule(),
            (0, 10, 30, 60, 180, 300, 600, 1200, 2400, 3600),
        )
        self.assertEqual(lifecycle.exponential_backoff_seconds(0), 0)
        self.assertEqual(lifecycle.exponential_backoff_seconds(1), 10)
        self.assertEqual(lifecycle.exponential_backoff_seconds(2), 30)
        self.assertEqual(lifecycle.exponential_backoff_seconds(3), 60)
        self.assertEqual(lifecycle.exponential_backoff_seconds(4), 180)
        self.assertEqual(lifecycle.exponential_backoff_seconds(9), 3600)

    def test_budget_is_configurable_but_safe_bounded(self):
        self.assertEqual(lifecycle.resolve_max_executions(None), 10)
        self.assertEqual(lifecycle.resolve_max_executions(""), 10)
        self.assertEqual(lifecycle.resolve_max_executions("5"), 5)
        # A misconfigured variable can neither silence recovery nor grant an
        # unbounded budget.
        self.assertEqual(lifecycle.resolve_max_executions("0"), 1)
        self.assertEqual(lifecycle.resolve_max_executions("-3"), 1)
        self.assertEqual(lifecycle.resolve_max_executions("99"), 10)
        self.assertEqual(lifecycle.resolve_max_executions("not-a-number"), 10)

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
        provider_delay = lifecycle.next_retry_delay_seconds(
            attempt=1, provider_reset_epoch=now + 900, now_epoch=now
        )
        self.assertGreaterEqual(provider_delay, 900)

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

    def test_reset_epoch_without_clock_fails_closed(self):
        # A reset minimum without the live clock must never be silently
        # dropped (which would dispatch straight through a rate limit).
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.next_retry_delay_seconds(
                attempt=1, ratelimit_reset_epoch=2000
            )
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.next_retry_delay_seconds(
                attempt=1, provider_reset_epoch=2000
            )

    def test_short_sha_identity_is_rejected(self):
        # Exact-HEAD identity requires a full commit id: a 7-char prefix
        # must not create a split-budget identity for the same commit.
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.normalize_head("a" * 7)
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.operation_key(REPO, 1, "a" * 7, "review")
        # Full SHAs (40-hex SHA-1 and 64-hex SHA-256) are accepted.
        self.assertEqual(lifecycle.normalize_head("A" * 40), "a" * 40)
        self.assertEqual(lifecycle.normalize_head("B" * 64), "b" * 64)

    def test_schedule_only_long_wait_persists_durable_not_before(self):
        # A large schedule-only delay (no Retry-After/reset headers) must
        # still persist not-before so the watchdog honors backoff instead
        # of retrying immediately and burning the budget.
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=7),
            now_epoch=1000,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertTrue(decision.defer_dispatch)
        self.assertIsNotNone(decision.not_before_epoch)
        self.assertGreater(decision.not_before_epoch, 1000)

    def test_schedule_only_long_wait_without_clock_defers(self):
        # Fail closed without a clock: a schedule-only tail delay (10m+)
        # cannot persist a durable not-before, so it must wait instead of
        # dispatching with defer=true and no durable wait (which would let
        # the next watchdog retry immediately and burn the budget).
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=7),
            now_epoch=None,
        )
        self.assertEqual(decision.action, "wait")
        self.assertIsNone(decision.attempt)
        # A short wait without a clock still dispatches immediately: there
        # is nothing durable to lose when no deferral is required.
        immediate = lifecycle.decide_recovery(
            ci_green=True, operation_state=None, now_epoch=None
        )
        self.assertEqual(immediate.action, "dispatch")
        self.assertFalse(immediate.defer_dispatch)

    def test_unknown_marker_age_stays_inside_dispatch_grace(self):
        # Fail closed: a marker newer than status whose age is unknown
        # (missing/unparsable timestamp) cannot prove grace expired, so it
        # waits instead of dispatching a likely duplicate.
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2),
            marker_newer_than_status=True,
            marker_age_seconds=None,
        )
        self.assertEqual(decision.action, "wait")
        # A known age past grace still proceeds to dispatch.
        past_grace = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2),
            marker_newer_than_status=True,
            marker_age_seconds=lifecycle.DISPATCH_GRACE_SECONDS + 1,
            now_epoch=1000,
        )
        self.assertEqual(past_grace.action, "dispatch")

    def test_malformed_durable_not_before_fails_closed(self):
        # Both the live parameter and the durable evidence value must raise
        # the documented fail-closed error, never a raw ValueError.
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.decide_recovery(
                ci_green=True,
                operation_state="failure",
                failure_transient=True,
                not_before_epoch="not-an-epoch",
                now_epoch=1000,
            )
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.decide_recovery(
                ci_green=True,
                operation_state="failure",
                failure_transient=True,
                evidence=lifecycle.RetryEvidence(not_before_epoch="not-an-epoch"),
                now_epoch=1000,
            )


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

    def test_pending_explicit_transient_dispatches_without_staleness(self):
        # An explicit transient verdict in pending state dispatches on
        # reset-aware backoff immediately, mirroring the failure path,
        # instead of waiting for a retryable conclusion or staleness.
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion="failure",
            status_age_seconds=0,
            failure_transient=True,
        )
        self.assertEqual(decision.action, "dispatch")
        # An explicit deterministic verdict still dominates staleness.
        deterministic = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="pending",
            run_conclusion="cancelled",
            status_age_seconds=lifecycle.STALE_AFTER_SECONDS + 1,
            failure_transient=False,
        )
        self.assertEqual(deterministic.action, "hold")

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

    def test_success_settles_despite_exhaustion_and_stale_lease(self):
        # A prior exhausted marker or a stale lease must never block the
        # success path: obsolete durable state is cleared via settled.
        exhausted = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="success",
            evidence=lifecycle.RetryEvidence(latest_attempt=9, exhausted=True),
        )
        self.assertEqual(exhausted.action, "settled")
        leased = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="success",
            active_exact_lease=True,
            active_operation=True,
        )
        self.assertEqual(leased.action, "settled")

    def test_non_transient_substring_holds_without_burning_budget(self):
        # A deterministic message containing the "transient" substring
        # (e.g. "non-transient policy failure") must hold without an
        # explicit classifier input, never burning transient budget.
        for description in (
            "non-transient policy failure",
            "not transient: policy denied",
            "policy failure: transient handling disabled",
        ):
            with self.subTest(description=description):
                decision = lifecycle.decide_recovery(
                    ci_green=True,
                    operation_state="failure",
                    operation_description=description,
                    failure_transient=None,
                )
                self.assertEqual(decision.action, "hold")
                self.assertIsNone(decision.attempt)

    def test_negated_recovery_eligible_holds(self):
        for description in (
            "not recovery eligible",
            "no recovery eligible for this HEAD",
            "non-recovery eligible outcome",
        ):
            with self.subTest(description=description):
                decision = lifecycle.decide_recovery(
                    ci_green=True,
                    operation_state="failure",
                    operation_description=description,
                    failure_transient=None,
                )
                self.assertEqual(decision.action, "hold")

    def test_explicit_recovery_eligible_requires_trusted_context(self):
        # The token alone never authorizes a retry: a missing or untrusted
        # commit-status context holds, so PR-visible check output or review
        # text containing the token can never burn transient budget. Only a
        # trusted reconciler-synthesized context dispatches, and an explicit
        # transient classifier verdict overrides the gate.
        missing = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            failure_transient=None,
        )
        self.assertEqual(missing.action, "hold")
        self.assertIsNone(missing.attempt)
        trusted = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            operation_context="continuum/pr-agent-review",
            failure_transient=None,
        )
        self.assertEqual(trusted.action, "dispatch")
        override = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            failure_transient=True,
        )
        self.assertEqual(override.action, "dispatch")

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
        # Nine retries consumed (execution index 9 seen): the next execution
        # would be index 10, outside the 10-execution budget.
        decision = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=9),
        )
        self.assertEqual(decision.action, "exhaust")
        held = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=9, exhausted=True),
        )
        self.assertEqual(held.action, "hold")
        # Mid-budget work still dispatches.
        mid = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2),
        )
        self.assertEqual(mid.action, "dispatch")
        self.assertEqual(mid.attempt, 3)

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
    def test_prefix_collision_does_not_share_budget(self):
        # Two distinct full SHAs sharing a 7-char prefix are different
        # HEADs: old-HEAD evidence must never authorize a new HEAD.
        old_head = "abc1234" + "0" * 33
        new_head = "abc1234" + "1" * 33
        self.assertNotEqual(old_head, new_head)
        comments = [comment(lifecycle.retry_marker(old_head, "review", 5))]
        evidence = lifecycle.retry_evidence(comments, head_sha=new_head, kind="review")
        self.assertIsNone(evidence.latest_attempt)
        self.assertFalse(evidence.exhausted)
        exhausted_comments = [
            comment(lifecycle.exhausted_marker(old_head, "review", attempts=10))
        ]
        exhausted = lifecycle.retry_evidence(
            exhausted_comments, head_sha=new_head, kind="review"
        )
        self.assertFalse(exhausted.exhausted)

    def test_short_prefix_marker_never_matches_full_head(self):
        short = HEAD[:7]
        comments = [
            comment(
                f"<!-- continuum-pr-agent-retry head={short} kind=review attempt=3 -->"
            )
        ]
        evidence = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertIsNone(evidence.latest_attempt)
        self.assertFalse(evidence.exhausted)

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

    def test_not_before_lives_inside_the_canonical_marker(self):
        marker = lifecycle.retry_marker(HEAD, "review", 1, not_before_epoch=2000)
        self.assertIn("not-before=2000 -->", marker)
        self.assertNotIn("--> not-before=", marker)
        evidence = lifecycle.retry_evidence(
            [comment(marker)], head_sha=HEAD, kind="review"
        )
        self.assertEqual(evidence.not_before_epoch, 2000)

    def test_retry_marker_preserves_epoch_zero_not_before(self):
        # Epoch zero is a real scheduling commitment, not a missing value:
        # the marker must carry it and the reader must return it instead
        # of treating it as no wait.
        marker = lifecycle.retry_marker(HEAD, "review", 1, not_before_epoch=0)
        self.assertIn("not-before=0 -->", marker)
        evidence = lifecycle.retry_evidence(
            [comment(marker)], head_sha=HEAD, kind="review"
        )
        self.assertEqual(evidence.not_before_epoch, 0)

    def test_stray_not_before_outside_any_marker_is_ignored(self):
        evidence = lifecycle.retry_evidence(
            [comment("hello not-before=2000")], head_sha=HEAD, kind="review"
        )
        self.assertIsNone(evidence.not_before_epoch)
        self.assertIsNone(evidence.latest_attempt)

    def test_legacy_marker_with_stray_not_before_carries_no_wait(self):
        # Legacy Work Lock #38 markers predate durable not-before: a stray
        # ``not-before=`` next to one must not become a durable wait.
        comments = [
            comment(
                f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->"
                " not-before=2000"
            )
        ]
        evidence = lifecycle.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertEqual(evidence.latest_attempt, 1)
        self.assertIsNone(evidence.not_before_epoch)

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

    def test_token_policy_covers_every_reconciler_operation(self):
        # Engine-level token policy (workflow wiring is asserted once in
        # scripts/test-continuum.rb so unit tests never couple to JS
        # needles): every same-repository discovery read used by either
        # reconciler stays off PAT; every mutation/dispatch stays PAT-backed;
        # unknown actions fail closed to PAT.
        for read_call in (
            "client.rest.pulls.get",
            "client.rest.pulls.list",
            "client.rest.repos.getCombinedStatusForRef",
            "client.rest.issues.listComments",
            "client.rest.repos.getCommit",
            "client.rest.repos.getBranch",
            "client.rest.pulls.listReviews",
            "client.rest.pulls.listReviewComments",
            "client.rest.actions.listWorkflowRunsForRepo",
            "client.rest.actions.listWorkflowRuns",
            "client.rest.actions.getWorkflowRun",
            "client.rest.repos.listCommitStatusesForRef",
            "client.graphql",
            "client.paginate",
            "client.request",
        ):
            with self.subTest(read_call=read_call):
                self.assertFalse(lifecycle.requires_pat(read_call))
        for mutation in (
            "merge",
            "dispatch",
            "github.rest.pulls.merge",
            "github.rest.issues.createComment",
            "github.rest.issues.updateComment",
            "github.rest.issues.deleteComment",
            "github.rest.issues.addLabels",
            "github.rest.actions.createWorkflowDispatch",
            "github.rest.actions.cancelWorkflowRun",
            "resolveReviewThread",
        ):
            with self.subTest(mutation=mutation):
                self.assertTrue(lifecycle.requires_pat(mutation))
        self.assertTrue(lifecycle.requires_pat("some_future_unknown_action"))
        self.assertTrue(lifecycle.requires_pat(""))


class CrossStackContractTests(unittest.TestCase):
    def test_one_contract_covers_every_lifecycle_kind(self):
        for kind in ("review", "repair", "review-ready", "automerge", "main-sync", "merge", "ci-repair"):
            with self.subTest(kind=kind):
                key = lifecycle.operation_key(REPO, 224, HEAD, kind)
                self.assertIn(kind, key)

    def test_provider_policy_semantics_are_unchanged(self):
        # One compatible recovery contract: the generic lifecycle budget and
        # the legacy PR-Agent helper agree on 10 executions and the same
        # schedule; provider gating itself is untouched.
        self.assertEqual(lifecycle.MAX_TRANSIENT_ATTEMPTS, 10)
        self.assertEqual(
            lifecycle.MAX_TRANSIENT_ATTEMPTS, legacy.MAX_EXECUTIONS
        )
        self.assertEqual(
            tuple(lifecycle.retry_delay_schedule()),
            tuple(legacy.retry_delay_schedule()),
        )
        self.assertEqual(
            lifecycle.TRUSTED_ASSOCIATIONS, legacy.TRUSTED_ASSOCIATIONS
        )
        self.assertEqual(
            lifecycle.RETRYABLE_RUN_CONCLUSIONS, legacy.RETRYABLE_RUN_CONCLUSIONS
        )

    def test_recovery_is_exact_head_and_fail_closed(self):
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.normalize_head("not-a-sha")
        with self.assertRaises(lifecycle.LifecycleRecoveryError):
            lifecycle.normalize_kind("merge-everything")
        unknown = lifecycle.decide_recovery(
            ci_green=True, operation_state="weird-state"
        )
        self.assertEqual(unknown.action, "hold")

    def test_recovery_token_requires_trusted_context(self):
        # PR-visible output containing the token must never burn budget:
        # an untrusted commit-status context holds even with the token.
        untrusted = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            operation_context="ci/some-external-check",
            failure_transient=None,
        )
        self.assertEqual(untrusted.action, "hold")
        trusted = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            operation_context="continuum/pr-agent-review",
            failure_transient=None,
        )
        self.assertEqual(trusted.action, "dispatch")
        # Without a context the token holds: PR-visible text is not a
        # reconciler-synthesized status, so it can never burn transient
        # budget on its own. Only an explicit transient classifier
        # verdict overrides the gate.
        legacy = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            failure_transient=None,
        )
        self.assertEqual(legacy.action, "hold")
        override = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="PR-Agent blocking review: recovery eligible",
            failure_transient=True,
        )
        self.assertEqual(override.action, "dispatch")

    def test_deterministic_hint_dominates_recovery_token(self):
        # A deterministic policy hint alongside the token still holds; only
        # an explicit transient classifier verdict overrides it.
        for description in (
            "CI test failure; recovery eligible",
            "unresolved findings; recovery eligible",
            "merge conflict; recovery eligible",
            "malformed state; recovery eligible",
        ):
            with self.subTest(description=description):
                decision = lifecycle.decide_recovery(
                    ci_green=True,
                    operation_state="failure",
                    operation_description=description,
                    failure_transient=None,
                )
                self.assertEqual(decision.action, "hold")
        override = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            operation_description="CI test failure; recovery eligible",
            failure_transient=True,
        )
        self.assertEqual(override.action, "dispatch")

    def test_legacy_short_exhausted_marker_preserves_exhaustion(self):
        # A pre-existing short-SHA exhausted marker that prefixes the
        # current HEAD preserves exhaustion fail-closed instead of
        # restarting the budget; short retry markers never authorize work.
        short = HEAD[:7]
        exhausted_comments = [
            comment(
                f"<!-- continuum-pr-agent-retry-exhausted head={short} kind=review attempts=10 -->"
            )
        ]
        exhausted = lifecycle.retry_evidence(
            exhausted_comments, head_sha=HEAD, kind="review"
        )
        self.assertTrue(exhausted.exhausted)
        held = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=exhausted,
        )
        self.assertEqual(held.action, "hold")
        retry_comments = [
            comment(
                f"<!-- continuum-pr-agent-retry head={short} kind=review attempt=3 -->"
            )
        ]
        retry = lifecycle.retry_evidence(
            retry_comments, head_sha=HEAD, kind="review"
        )
        self.assertIsNone(retry.latest_attempt)
        self.assertFalse(retry.exhausted)

    def test_cross_run_safety_comes_from_durable_guards(self):
        # In-memory dedupe/should_coalesce are intra-run only; overlapping
        # runs serialize per PR/HEAD through decide_recovery's durable guards
        # (per-PR/HEAD lease via concurrency_key, dispatch grace, durable
        # not-before) so unrelated PRs never queue behind or starve each
        # other. A second run that sees the first run's fresh marker inside
        # grace waits instead of computing a duplicate dispatch.
        key = lifecycle.concurrency_key(REPO, 224, HEAD)
        self.assertEqual(lifecycle.dedupe_wakeups([key, key]), [key])
        self.assertTrue(lifecycle.should_coalesce([key], key))
        waiting = lifecycle.decide_recovery(
            ci_green=True,
            operation_state="failure",
            failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=2),
            marker_newer_than_status=True,
            marker_age_seconds=10,
        )
        self.assertEqual(waiting.action, "wait")


if __name__ == "__main__":
    unittest.main()
