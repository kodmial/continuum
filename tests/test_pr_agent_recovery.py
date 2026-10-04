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

    def test_edited_controller_marker_uses_updated_timestamp(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        fresh = datetime.now(timezone.utc)
        comments = [{
            "body": (
                "<!-- continuum-pr-agent-controller-state:v1 -->\n"
                f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->"
            ),
            "author_association": "OWNER",
            "created_at": old.isoformat(),
            "updated_at": fresh.isoformat(),
        }]
        evidence = recovery.retry_evidence(comments, head_sha=HEAD, kind="review")
        self.assertEqual(evidence.latest_attempt, 1)
        self.assertIsNotNone(evidence.latest_marker_at)
        self.assertLess(abs((evidence.latest_marker_at - fresh).total_seconds()), 1)

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


class RecoveryWiringTests(unittest.TestCase):
    def read(self, path: str) -> str:
        with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_recovery_has_event_driven_wakeups_and_schedule_safety_net(self):
        stub = self.read(".github/caller-stubs/continuum-pr-agent-recovery.yml")
        self.assertIn("workflow_run:", stub)
        self.assertIn("- CI", stub)
        self.assertIn("- PR Agent (OpenCode backend)", stub)
        self.assertIn("pull_request_target:", stub)
        self.assertIn('cron: "*/10 * * * *"', stub)

    def test_review_caller_no_longer_competes_for_ci_wakeup(self):
        for path in (
            ".github/workflows/pr-agent.yml",
            ".github/caller-stubs/continuum-pr-agent.yml",
        ):
            body = self.read(path)
            with self.subTest(path=path):
                self.assertNotIn("workflow_run:", body)
                self.assertNotIn("pull_request_target:", body)
                self.assertIn("recovery_kind:", body)
                self.assertIn("run-name: >-", body)
                self.assertIn("head=${{ inputs.expected_head_sha || 'event' }}", body)

    def test_recovery_engine_is_provider_isolated_and_exact_head_driven(self):
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        for needle in (
            "head_sha: head",
            "exactActiveRun",
            "author_association",
            "continuum/pr-agent-review",
            "continuum/pr-agent-repair",
            "retryableConclusions",
            "createWorkflowDispatch",
            "expected_head_sha: head",
            "recovery_kind: kind",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        self.assertNotIn("continuum-coderabbit-retry.yml", body)
        self.assertNotIn("continuum-coderabbit-unresolved.yml", body)

    def test_recovery_reads_use_repository_token_but_mutations_keep_pat(self):
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", body)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", body)
        self.assertNotIn("require('@actions/github')", body)
        self.assertNotIn("require(\"@actions/github\")", body)
        self.assertIn("new github.constructor({ auth: readToken, baseUrl: readBaseUrl })", body)
        self.assertIn("github.request.endpoint.DEFAULTS.baseUrl", body)
        self.assertIn("process.env.GITHUB_API_URL", body)
        self.assertIn("using PAT client for PR-Agent recovery reads", body)
        self.assertIn("let readTokenUnavailable = false", body)
        self.assertIn("async function withReadFallback(fn)", body)
        self.assertIn("err.status ?? err.response?.status", body)
        self.assertIn("status === 401 || status === 403 || status === 429", body)
        self.assertIn("if (readTokenUnavailable || readGithub === github)", body)

        for read_call in (
            "client.rest.actions.listWorkflowRuns",
            "client.rest.actions.getWorkflowRun",
            "client.rest.actions.listWorkflowRunsForRepo",
            "client.rest.pulls.list",
            "client.rest.pulls.get",
            "client.rest.repos.listCommitStatusesForRef",
            "client.rest.issues.listComments",
            "client.rest.repos.get({ owner, repo })",
        ):
            with self.subTest(read_call=read_call):
                self.assertIn(read_call, body)

        # One helper definition plus ten guarded read sites. Controller-state
        # upsert also reads comments through the repository-scoped token.
        self.assertEqual(body.count("withReadFallback("), 11)

        # Item 1 keeps all mutation/dispatch calls on the PAT-authenticated
        # action client, preserving actor and event fan-out semantics.
        for mutation in (
            "github.rest.issues.createComment",
            "github.rest.issues.updateComment",
            "github.rest.issues.deleteComment",
            "github.rest.actions.createWorkflowDispatch",
        ):
            with self.subTest(mutation=mutation):
                self.assertIn(mutation, body)

        self.assertNotIn("github.paginate(", body)
        self.assertNotRegex(
            body,
            r"github\.rest\.(?!issues\.createComment\b|issues\.updateComment\b|issues\.deleteComment\b|actions\.createWorkflowDispatch\b)",
            "PAT client must only be used for mutations/dispatch",
        )

    def test_recovery_read_client_constructs_in_github_script_sandbox(self):
        """Runtime-shaped guard for issue #217.

        The github-script sandbox does not resolve `require('@actions/github')`
        (the Node 24 regression). The workflow must therefore build its
        repository-token read client from the supplied `github.constructor`.
        This test executes the workflow's construction semantics under Node with
        a `require` that rejects `@actions/github`, so reintroducing the bare
        require fails here exactly as it fails live.
        """
        import re
        import shutil
        import subprocess
        import tempfile

        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertNotIn("require('@actions/github')", body)

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for the github-script sandbox check")

        harness = r"""
const assert = require('assert');
// github-script sandbox shape: '@actions/github' is not resolvable.
function sandboxRequire(name) {
  throw new Error("Cannot find module '" + name + "'");
}
let rejected = false;
try {
  sandboxRequire('@actions/github');
} catch (err) {
  rejected = /Cannot find module/.test(String(err && err.message));
}
assert.ok(rejected, 'sandbox must reject require(@actions/github)');

// Plugin-composed Octokit mock as supplied by actions/github-script v7.
class FakeOctokit {
  constructor(opts = {}) {
    this.opts = opts;
    this.request = { endpoint: { DEFAULTS: { baseUrl: 'https://ghe.example.com/api/v3' } } };
  }
}
const github = new FakeOctokit({ auth: 'PAT' });
github.constructor = FakeOctokit;
const core = { warning: () => {}, info: () => {}, notice: () => {}, setFailed: (m) => { throw new Error(m); } };

// --- workflow construction block under test (mirrors the yml) ---
const readToken = String(process.env.READ_GITHUB_TOKEN || '').trim();
const readBaseUrl = (
  (github && github.request && github.request.endpoint &&
    github.request.endpoint.DEFAULTS && github.request.endpoint.DEFAULTS.baseUrl) ||
  process.env.GITHUB_API_URL || 'https://api.github.com'
);
const readGithub = readToken
  ? new github.constructor({ auth: readToken, baseUrl: readBaseUrl })
  : github;
// --- end workflow block ---
assert.notStrictEqual(readGithub, github, 'repository token must yield an independent client');
assert.strictEqual(readGithub.opts.auth, 'ghs_repo_token');
assert.strictEqual(readGithub.opts.baseUrl, 'https://ghe.example.com/api/v3', 'GHE base URL must be preserved');
const emptyToken = '';
const fallback = emptyToken ? new github.constructor({ auth: emptyToken, baseUrl: readBaseUrl }) : github;
assert.strictEqual(fallback, github, 'empty token must fall back to the PAT client');
console.log('sandbox construction OK');
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(harness)
            script = handle.name
        try:
            env = dict(
                os.environ,
                READ_GITHUB_TOKEN="ghs_repo_token",
                GITHUB_API_URL="https://api.github.com",
            )
            completed = subprocess.run(
                [node, script],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        finally:
            os.unlink(script)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("sandbox construction OK", completed.stdout)

        # The executed semantics must match the shipped workflow text, so the
        # harness cannot drift from the yml silently.
        for needle in (
            "const readToken = String(process.env.READ_GITHUB_TOKEN",
            "github.request.endpoint.DEFAULTS.baseUrl",
            "process.env.GITHUB_API_URL",
            "new github.constructor({ auth: readToken, baseUrl: readBaseUrl })",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_recovery_coalesces_retry_state_into_one_controller_comment(self):
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("continuum-pr-agent-controller-state:v1", body)
        self.assertIn("async function upsertControllerState", body)
        self.assertIn("github.rest.issues.updateComment", body)
        self.assertIn("controller.slice(0, -1)", body)
        self.assertIn("rollbackControllerState", body)
        self.assertIn("comment.updated_at || comment.created_at", body)

    def test_recovered_review_uses_ci_workflow_not_combined_status(self):
        review = self.read(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("listWorkflowRunsForRepo", review)
        self.assertIn("CI_WORKFLOW_NAME", review)
        self.assertNotIn(
            "const { data: combined } = await github.rest.repos.getCombinedStatusForRef",
            review,
        )
        self.assertNotIn(
            'commits/$ADMITTED_SHA/status',
            review,
            "pre-review revalidation must not fall back to combined commit status",
        )
        self.assertIn(
            'actions/runs?event=pull_request&head_sha=$ADMITTED_SHA',
            review,
        )

    def test_failure_classification_is_explicit(self):
        review = self.read(".github/workflows/continuum-pr-agent.yml")
        repair = self.read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("PR-Agent review failed: transient; recovery eligible", review)
        self.assertIn("PR-Agent review failed: deterministic; recovery held", review)
        self.assertIn('echo "classification=transient"', repair)
        self.assertIn('echo "classification=deterministic"', repair)
        self.assertIn("PR-Agent repair failed: transient; recovery eligible", repair)
        self.assertIn("PR-Agent repair failed: deterministic; recovery held", repair)

    def test_inline_retries_preserve_operation_kind(self):
        review = self.read(".github/workflows/continuum-pr-agent.yml")
        repair = self.read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn('CURRENT_RECOVERY_KIND:', review)
        self.assertIn(
            '-f recovery_kind="${CURRENT_RECOVERY_KIND:-review}"',
            review,
        )
        self.assertNotIn(
            'if [[ "${CURRENT_RECOVERY_KIND:-review}" != "review" ]]; then',
            review,
        )
        self.assertIn('-f recovery_kind="repair"', repair)

    def test_cancelled_recovery_review_keeps_repair_identity(self):
        recovery_workflow = self.read(
            ".github/workflows/continuum-pr-agent-recovery.yml"
        )
        self.assertIn("statusRunMetadata", recovery_workflow)
        self.assertIn("reviewRun.recoveryKind === 'repair'", recovery_workflow)
        self.assertIn(
            "it still belongs to the durable repair",
            recovery_workflow,
        )


if __name__ == "__main__":
    unittest.main()
