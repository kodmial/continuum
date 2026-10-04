"""Regression tests for kodmial/continuum#254 (GitHub API/PAT minimization).

Proves the Definition of Done items with deterministic fixtures:

1. no newly-audited list operation grows unbounded with 10k+ records;
2. duplicate same-resource reads within one pass are coalesced;
3. local Git is used for repository-state questions where equivalent;
4. event payload is used when sufficient;
5. same-repo read-only calls avoid the shared PAT where allowed;
6. shared-PAT mutations/cross-repo semantics remain correct;
7. rate-limit/transient failures in non-critical reads never retry-storm;
8. core scheduler/review/repair safety gates remain unchanged;
9. request counts are materially lower on active-consumer fixtures.
"""

import pathlib
import unittest

from continuum.api_budget import (
    DEFAULT_MAX_PAGES,
    FILES_MAX_PAGES,
    GITHUB_ONLY_QUESTIONS,
    GIT_EQUIVALENT_QUESTIONS,
    PAT_CREDENTIAL,
    QUEUE_MAX_PAGES,
    READ_CREDENTIAL,
    RECENT_MAX_PAGES,
    ApiBudget,
    ConsumerFixture,
    MemoCache,
    RetryPolicy,
    credential_for,
    endpoint_family,
    estimate_new_pass,
    estimate_old_pass,
    read_with_budget,
    simulate_scan,
    substitution_for,
    worst_case_requests,
)

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def workflow(name):
    return (WORKFLOWS / name).read_text()


class BoundedScanTest(unittest.TestCase):
    """DoD 1: every newly-audited list operation is bounded under 10k+ history."""

    def test_huge_history_never_exceeds_page_cap(self):
        for total in (10_000, 100_000, 1_000_000):
            self.assertLessEqual(worst_case_requests(total), DEFAULT_MAX_PAGES)
            self.assertGreater(worst_case_requests(total), 0)

    def test_simulated_scan_truncates_instead_of_growing(self):
        result = simulate_scan(12_000)
        self.assertEqual(result["requests"], DEFAULT_MAX_PAGES)
        self.assertEqual(result["observed"], DEFAULT_MAX_PAGES * 100)
        self.assertTrue(result["truncated"])

    def test_recent_gate_scans_use_tighter_cap(self):
        self.assertLessEqual(worst_case_requests(12_000, 100, RECENT_MAX_PAGES), 2)

    def test_queue_and_file_caps_are_history_independent(self):
        self.assertEqual(worst_case_requests(50_000, 100, QUEUE_MAX_PAGES), QUEUE_MAX_PAGES)
        self.assertEqual(worst_case_requests(50_000, 100, FILES_MAX_PAGES), FILES_MAX_PAGES)

    def test_empty_history_costs_nothing(self):
        self.assertEqual(worst_case_requests(0), 0)


class MemoCacheTest(unittest.TestCase):
    """DoD 2: duplicate same-resource reads within one pass are eliminated."""

    def test_repeated_read_fetches_once(self):
        cache = MemoCache()
        calls = []

        def fetch():
            calls.append(1)
            return {"sha": "abc"}

        first = cache.get_or_fetch("pulls.get:1", fetch)
        second = cache.get_or_fetch("pulls.get:1", fetch)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.request_count, 1)
        self.assertEqual(cache.hits, 1)

    def test_distinct_resources_fetch_independently(self):
        cache = MemoCache()
        cache.get_or_fetch("issues.get:1", lambda: 1)
        cache.get_or_fetch("issues.get:2", lambda: 2)
        self.assertEqual(cache.request_count, 2)

    def test_gate_results_share_one_branch_listing(self):
        # Three gate checks (CI, packaging, required workflow) on one branch
        # share a single branch-filtered run listing within a pass.
        cache = MemoCache()
        fetches = []
        for workflow in ("CI", "Packaging smoke", "Required"):
            cache.get_or_fetch("runs:main:pull_request",
                               lambda: fetches.append(1) or ["run"])
        self.assertEqual(len(fetches), 1)


class SubstitutionTest(unittest.TestCase):
    """DoD 3+4: local Git and event payload replace equivalent API reads."""

    def test_repo_state_prefers_event_then_git(self):
        for question in ("head_sha", "merge_base", "changed_files",
                         "behind_by", "commit_history", "file_content_at_ref"):
            self.assertIn(question, GIT_EQUIVALENT_QUESTIONS)
            self.assertEqual(substitution_for(question, True, True), "event")
            self.assertEqual(substitution_for(question, True, False), "git")

    def test_no_checkout_falls_back_to_api(self):
        self.assertEqual(substitution_for("head_sha", False, False), "api")

    def test_github_only_state_never_uses_git(self):
        self.assertTrue(len(GITHUB_ONLY_QUESTIONS) > 0)
        for question in sorted(GITHUB_ONLY_QUESTIONS):
            self.assertEqual(substitution_for(question, True, True), "api",
                             f"{question} must stay API-backed")


class CredentialPolicyTest(unittest.TestCase):
    """DoD 5+6: same-repo reads avoid PAT; mutations/cross-repo keep it."""

    def test_same_repo_read_uses_repository_token(self):
        self.assertEqual(credential_for(True, True), READ_CREDENTIAL)

    def test_mutation_keeps_pat(self):
        self.assertEqual(credential_for(True, False), PAT_CREDENTIAL)

    def test_cross_repo_read_keeps_pat(self):
        self.assertEqual(credential_for(False, True), PAT_CREDENTIAL)

    def test_insufficient_token_permissions_keep_pat(self):
        self.assertEqual(
            credential_for(True, True, token_permissions_sufficient=False),
            PAT_CREDENTIAL)

    def test_endpoint_families_cover_controllers(self):
        self.assertEqual(endpoint_family("pulls.list"), "pulls")
        self.assertEqual(endpoint_family("issues.listComments"), "issues")
        self.assertEqual(endpoint_family("actions.listWorkflowRunsForRepo"), "actions")
        self.assertEqual(endpoint_family("repos.getCombinedStatusForRef"), "repos")
        self.assertEqual(endpoint_family("graphql:reviewThreads"), "graphql:reviewThreads")


class RetryBudgetTest(unittest.TestCase):
    """DoD 7: transient/rate-limit failures never create retry storms."""

    def test_success_costs_one_attempt(self):
        self.assertEqual(read_with_budget(lambda: "ok"), "ok")

    def test_exhausted_budget_returns_fallback(self):
        attempts = []

        def failing():
            attempts.append(1)
            raise RuntimeError("rate limited")

        self.assertIsNone(read_with_budget(failing, RetryPolicy(max_attempts=3), None))
        self.assertEqual(len(attempts), 3)

    def test_non_transient_failure_does_not_retry(self):
        attempts = []

        def failing():
            attempts.append(1)
            raise ValueError("bad input")

        result = read_with_budget(failing, RetryPolicy(max_attempts=5), "fallback",
                                  is_rate_limited=lambda e: False)
        self.assertEqual(result, "fallback")
        self.assertEqual(len(attempts), 1)

    def test_delays_are_bounded_and_jittered(self):
        policy = RetryPolicy(max_attempts=4, base_delay_seconds=2.0,
                             max_delay_seconds=30.0)
        delays = policy.delays(seed=7)
        self.assertEqual(len(delays), 3)
        for delay, cap in zip(delays, (2.0, 4.0, 8.0)):
            self.assertGreaterEqual(delay, 0)
            self.assertLessEqual(delay, cap)

    def test_single_attempt_policy_never_sleeps(self):
        self.assertEqual(RetryPolicy(max_attempts=1).delays(), [])


class SafetyGateTest(unittest.TestCase):
    """DoD 8: core scheduler/review/repair safety gates remain unchanged."""

    def test_coderabbit_queue_gates_intact(self):
        body = workflow("continuum-coderabbit-retry.yml")
        for sentinel in (
            "await latestWorkflowForHead(pr, 'Packaging smoke')",
            "await unresolvedCodeRabbitThreads(pr)",
            "currentDecision?.state === 'APPROVED'",
            "continuum-coderabbit-no-progress head=",
            "could not inspect pre-review workflow gates; skipping this PR for this pass",
        ):
            self.assertIn(sentinel, body)

    def test_pr_agent_safety_gates_intact(self):
        body = workflow("continuum-pr-agent.yml")
        for sentinel in (
            "expected_head_sha",
            "exact HEAD",
        ):
            self.assertIn(sentinel, body)

    def test_auto_merge_housekeeping_scope_untouched(self):
        # #253 owns cancelObsoleteRuns; #254 must not re-solve it.
        body = workflow("continuum-auto-merge.yml")
        self.assertIn("cancelObsoleteRuns", body)

    def test_opencode_repair_never_checks_out_pr_code(self):
        body = workflow("continuum-opencode-repair.yml")
        self.assertIn("never checks out or executes PR code", body)


class RequestReductionTest(unittest.TestCase):
    """DoD 9: request counts are materially lower on active-consumer fixtures."""

    def test_bounded_scan_beats_unbounded_on_history(self):
        fixture = ConsumerFixture()
        old = worst_case_requests(fixture.historical_runs, 100, 10 ** 9)
        new = worst_case_requests(fixture.historical_runs, 100, RECENT_MAX_PAGES)
        self.assertGreater(old, 100)
        self.assertLessEqual(new, RECENT_MAX_PAGES)

    def test_full_pass_is_materially_cheaper(self):
        fixture = ConsumerFixture(open_prs=25, historical_runs=12_000)
        old = estimate_old_pass(fixture)
        new = estimate_new_pass(fixture)
        # Fewer total calls (no second full pass) and far fewer pages
        # (bounded scans instead of history-sized scans).
        self.assertLess(new.calls, old.calls)
        self.assertLess(new.pages, old.pages // 2)
        self.assertLess(new.pat_calls(), old.pat_calls())

    def test_noop_pass_costs_a_bounded_minimum(self):
        fixture = ConsumerFixture(open_prs=0, historical_runs=12_000)
        new = estimate_new_pass(fixture)
        # Queue scan (1 page) + targeted revalidation reads + 1 write.
        self.assertLessEqual(new.calls, 10)

    def test_budget_reports_by_credential_family_and_pages(self):
        budget = ApiBudget()
        budget.record("pulls.list", READ_CREDENTIAL, pages=2)
        budget.record("issues.createComment", PAT_CREDENTIAL)
        budget.record_substitution("git")
        budget.record_substitution("event")
        lines = budget.summary_lines()
        self.assertTrue(any("total_calls=2" in line for line in lines))
        self.assertTrue(any("TAP_PAT=1" in line for line in lines))
        self.assertTrue(any("pulls=1" in line for line in lines))
        self.assertTrue(any("git=1" in line and "event=1" in line for line in lines))
        self.assertEqual(budget.pat_calls(), 1)


if __name__ == "__main__":
    unittest.main()
