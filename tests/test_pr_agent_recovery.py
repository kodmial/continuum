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

        # One helper definition plus twelve guarded read sites. Controller-state
        # upsert also reads comments through the repository-scoped token, and
        # both post-dispatch prune paths re-read fresh (grace-coalesce and
        # coalesce-failure) so an interleaved controller write is never pruned
        # from a stale pre-dispatch snapshot.
        self.assertEqual(body.count("withReadFallback("), 13)

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

    def test_recovery_controller_touch_changes_comment_body_per_dispatch_run(self):
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("Controller dispatch run:", body)
        self.assertIn("String(context.runId)", body)
        self.assertLess(
            body.index("Controller dispatch run:"),
            body.index("github.rest.actions.createWorkflowDispatch"),
        )
        # The touch is presentation-only: durable retry identity is the
        # stateMarker line alone, and the touch prefix cannot match the
        # retry/exhausted marker regexes.
        self.assertIn("durable retry identity", body)
        self.assertIn("is the stateMarker line alone", body)
        self.assertIn("continuum-pr-agent-retry markers", body)

    def test_controller_touch_body_differs_per_run_but_keeps_retry_identity(self):
        """Exercise the real shipped JS, not a Python reimplementation.

        The static pins fail if the workflow touch is reverted; the Node
        harness below extracts the real `controllerStateBody` plus the real
        touch expression from the workflow text and executes them, so this
        test cannot stay green while the shipped code regresses.
        """
        import json
        import re
        import shutil
        import subprocess
        import tempfile

        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        # Static pins against revert: the unsafe bare concatenation must be
        # gone and the null-safe coercion must be present.
        self.assertNotIn("summary + '\\n\\nController dispatch run:'", body)
        self.assertIn(
            "String(summary || '').trim() + '\\n\\nController dispatch run: '"
            " + String(context.runId)",
            body,
        )

        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to execute the shipped JS; failing closed instead of silently skipping",
        )

        marker_match = re.search(
            r"const CONTROLLER_STATE_MARKER = '([^']*)';", body
        )
        self.assertIsNotNone(marker_match, "controller state marker const is missing")
        marker_const = marker_match.group(1)

        fn_start = body.index("function controllerStateBody(stateMarker, summary)")
        brace = body.index("{", fn_start)
        depth = 0
        fn_end = None
        for pos in range(brace, len(body)):
            if body[pos] == "{":
                depth += 1
            elif body[pos] == "}":
                depth -= 1
                if depth == 0:
                    fn_end = pos + 1
                    break
        self.assertIsNotNone(fn_end, "could not extract controllerStateBody")
        fn_source = body[fn_start:fn_end]

        touch_match = re.search(
            r"const body = controllerStateBody\(\s*stateMarker,\s*([\s\S]*?)\n\s*\);",
            body,
        )
        self.assertIsNotNone(touch_match, "could not extract the touch expression")
        touch_expr = touch_match.group(1).strip()
        self.assertIn("Controller dispatch run:", touch_expr)

        harness = (
            "const assert = require('assert');\n"
            f"const CONTROLLER_STATE_MARKER = {json.dumps(marker_const)};\n"
            + fn_source
            + "\n"
            "function touchedSummary(summary, runId) {\n"
            "  const context = { runId };\n"
            f"  return ({touch_expr});\n"
            "}\n"
            f"const marker = {json.dumps(f'<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->')};\n"
            "const base = 'Automatic PR-Agent review recovery for exact HEAD.';\n"
            "const a = controllerStateBody(marker, touchedSummary(base, 111));\n"
            "const b = controllerStateBody(marker, touchedSummary(base, 222));\n"
            "assert.notStrictEqual(a, b, 'different runs must produce different bodies');\n"
            "assert.ok(a.includes('Controller dispatch run: 111'));\n"
            "assert.ok(b.includes('Controller dispatch run: 222'));\n"
            "assert.strictEqual(a.split('\\n')[1], marker);\n"
            "assert.strictEqual(b.split('\\n')[1], marker);\n"
            "for (const summary of [undefined, null, 0, 12345, '']) {\n"
            "  const touched = controllerStateBody(marker, touchedSummary(summary, 999));\n"
            "  assert.ok(!/\\bundefined\\b/.test(touched), 'summary must never render undefined: ' + touched);\n"
            "  assert.ok(!/\\bnull\\b/.test(touched), 'summary must never render null: ' + touched);\n"
            "  assert.ok(touched.includes(marker), 'retry identity must survive nullish summary');\n"
            "  assert.ok(touched.includes('Controller dispatch run: 999'));\n"
            "}\n"
            "console.log('TOUCH_OK');\n"
        )
        tmpdir = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, dir=tmpdir
        ) as handle:
            handle.write(harness)
            script = handle.name
        try:
            completed = subprocess.run(
                [node, script],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            os.unlink(script)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("TOUCH_OK", completed.stdout)

    def test_failed_dispatch_leaves_controller_state_untouched(self):
        """A dispatch that throws must not advance existing comment timestamps.

        Dispatch records a pre-dispatch claim via a fresh createComment
        before createWorkflowDispatch (so a crash after a successful
        dispatch can never leave the next wakeup without marker evidence
        inside grace, which would duplicate the same attempt), never via
        updateComment (which would bump updated_at even on failure), and
        deletes that claim when the dispatch throws, so a retry that never
        dispatched is never coalesced as fresh. The success path then
        coalesces the claim via upsertControllerState.
        """
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        window = body[
            body.index("async function dispatch("): body.index("async function exhaust(")
        ]
        dispatch_at = window.index("github.rest.actions.createWorkflowDispatch")
        self.assertLess(
            window.index("github.rest.issues.createComment"),
            dispatch_at,
            "pre-dispatch claim must be recorded before dispatch",
        )
        self.assertNotIn(
            "github.rest.issues.updateComment",
            window[:dispatch_at],
            "pre-dispatch claim must be a fresh create, never an update that bumps updated_at on failure",
        )
        self.assertIn(
            "rollbackControllerState",
            window,
            "a failed dispatch must delete the pre-dispatch claim",
        )
        self.assertLess(
            dispatch_at,
            window.index("await upsertControllerState("),
            "success path must coalesce the claim into one controller comment",
        )

    def test_controller_touch_line_never_consumes_retry_budget(self):
        marker = f"<!-- continuum-pr-agent-retry head={HEAD} kind=review attempt=1 -->"
        touched = (
            "<!-- continuum-pr-agent-controller-state:v1 -->\n"
            f"{marker}\n"
            "<details>\n"
            "<summary>Continuum PR-Agent controller state</summary>\n"
            "\n"
            "Automatic recovery.\n"
            "\n"
            "Controller dispatch run: 999\n"
            "\n"
            "</details>"
        )
        plain = (
            "<!-- continuum-pr-agent-controller-state:v1 -->\n"
            f"{marker}\n"
            "<details>\n"
            "<summary>Continuum PR-Agent controller state</summary>\n"
            "\n"
            "Automatic recovery.\n"
            "\n"
            "</details>"
        )
        touched_evidence = recovery.retry_evidence(
            [comment(touched)], head_sha=HEAD, kind="review"
        )
        plain_evidence = recovery.retry_evidence(
            [comment(plain)], head_sha=HEAD, kind="review"
        )
        self.assertEqual(touched_evidence.latest_attempt, 1)
        self.assertEqual(
            touched_evidence.latest_attempt, plain_evidence.latest_attempt
        )
        self.assertFalse(touched_evidence.exhausted)
        # The touch prefix itself carries no retry marker.
        self.assertNotRegex(
            "Controller dispatch run: 999",
            r"continuum-pr-agent-retry",
        )

    def test_controller_state_stays_in_one_comment_with_rollback(self):
        body = self.read(".github/workflows/continuum-pr-agent-recovery.yml")
        # Single-comment coalescing: update the latest controller comment,
        # delete every older one, and roll back on dispatch failure.
        self.assertIn("github.rest.issues.updateComment", body)
        self.assertIn("github.rest.issues.createComment", body)
        self.assertIn("controller.slice(0, -1)", body)
        self.assertIn("rollbackControllerState", body)
        self.assertIn("previousBody", body)
        # Grace/coalescing reads the bumped timestamp.
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


class CodeRabbitDeadlockWiringTests(unittest.TestCase):
    """Lock the issue-37 P0 merge-gate deadlock semantics in place.

    The controller-touch follow-up above must never regress or silently
    substitute for these CodeRabbit adapter guarantees: exact-head
    APPROVED identity, Review-skipped tolerance, nitpick supersession,
    RESOLVED/UNRESOLVED normalization with fail-closed behavior, and
    quota serialization.
    """

    def read(self, path: str) -> str:
        with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_exact_head_approved_identity(self):
        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("review.commit_id === headSha", gate)
        self.assertIn("decision.state !== 'APPROVED'", gate)
        self.assertIn("CHANGES_REQUESTED", gate)
        self.assertIn("relative timestamps do not add safety", gate)
        self.assertIn("sha: pr.head.sha", gate)

    def test_review_skipped_does_not_erase_durable_approval(self):
        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("Review skipped", gate)
        self.assertIn("Review completed", gate)
        # The Review-completed wait must stay conditional on a missing
        # review basis. An unconditional `description contains Review
        # completed` merge-authorization check would erase a durable
        # exact-head APPROVED basis whenever an auto-review-disabled event
        # overwrote the status with "Review skipped".
        self.assertRegex(gate, r"!reviewBasis[\s\S]{0,400}Review completed")
        # Exactly two mentions exist: the status-independence comment and
        # the single guarded wait. A reintroduced hard Review-completed
        # authorization check adds a third and fails here.
        self.assertEqual(gate.count("Review completed"), 2)
        basis_src = self._extract_js_function(gate, "directCodeRabbitReviewBasis")
        # The basis is review+CI+nitpick only: it must never read a commit
        # status description. (Its explanatory comment mentions the status
        # names, so pin on status plumbing instead of bare substrings.)
        self.assertNotIn(
            "rabbitStatus",
            basis_src,
            "review basis must stay status-independent; a skipped status never erases it",
        )
        self.assertNotIn(
            "latestCodeRabbitStatus",
            basis_src,
            "review basis must stay status-independent; completed status is not merge evidence",
        )
        self.assertNotIn(
            ".description",
            basis_src,
            "review basis must not authorize on a status description",
        )

    def test_nitpick_supersession(self):
        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("supersedes earlier advisory", gate)
        self.assertIn("codeRabbitNitpickReviews", gate)
        self.assertIn("> decisionAt", gate)

    def test_thread_normalization_is_fail_closed(self):
        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("resolveReviewThread", gate)
        self.assertIn("UNRESOLVED always wins", gate)
        self.assertIn("unresolved.push", gate)
        self.assertIn("unresolvedCodeRabbitThreads", gate)

    def test_verification_requests_are_serialized_and_deduplicated(self):
        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        self.assertIn("auto-merge-coderabbit-verification", gate)
        self.assertIn("if (duplicate) continue", gate)

    @staticmethod
    def _extract_js_function(source: str, name: str) -> str:
        """Extract one top-level JS function, skipping braces in strings.

        A naive brace counter miscounts the GraphQL template literals in the
        auto-merge gate, so this scanner tracks line/block comments,
        single/double-quoted strings, and template literals with ${}
        interpolation.
        """
        import re

        match = re.search(
            r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", source
        )
        assert match is not None, f"JS function {name} not found"
        brace = source.index("{", match.end() - 1)
        depth = 1
        mode = ["code"]
        i = brace + 1
        total = len(source)
        while i < total:
            char = source[i]
            nxt = source[i + 1] if i + 1 < total else ""
            top = mode[-1]
            if top == "line":
                if char == "\n":
                    mode.pop()
            elif top == "block":
                if char == "*" and nxt == "/":
                    mode.pop()
                    i += 1
            elif top == "str1":
                if char == "\\":
                    i += 1
                elif char == "'":
                    mode.pop()
            elif top == "str2":
                if char == "\\":
                    i += 1
                elif char == '"':
                    mode.pop()
            elif top == "tpl":
                if char == "\\":
                    i += 1
                elif char == "`":
                    mode.pop()
                elif char == "$" and nxt == "{":
                    mode.append("{tpl}")
                    depth += 1
                    i += 1
            else:  # code or {tpl}
                if char == "/" and nxt == "/" and top == "code":
                    mode.append("line")
                    i += 1
                elif char == "/" and nxt == "*" and top == "code":
                    mode.append("block")
                    i += 1
                elif char == "'" and top == "code":
                    mode.append("str1")
                elif char == '"' and top == "code":
                    mode.append("str2")
                elif char == "`" and top == "code":
                    mode.append("tpl")
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if top == "{tpl}":
                        mode.pop()
                    if depth == 0:
                        return source[match.start() : i + 1]
            i += 1
        raise AssertionError(f"unterminated JS function {name}")

    def test_deadlock_ticket_cases_distinguish_green_from_blocked(self):
        """Execute the real gate logic against fixtures for the deadlock cases.

        Substring presence cannot tell a green gate from a blocked one, so
        this test extracts the real decision functions from
        continuum-auto-merge.yml and runs green-vs-blocked fixtures under
        Node: stale-head approval, CHANGES_REQUESTED over a skipped status,
        skipped status preserving a durable approval, newer vs superseded
        nitpicks, RESOLVED normalization, UNRESOLVED blocking, and fail-closed
        normalization failure.
        """
        import json
        import shutil
        import subprocess
        import tempfile

        gate = self.read(".github/workflows/continuum-auto-merge.yml")
        needed = (
            "latestWorkflowForHead",
            "latestCiForHead",
            "latestCodeRabbitDecision",
            "codeRabbitNitpickReviews",
            "directCodeRabbitReviewBasis",
            "waitingForCompletedCodeRabbitReview",
            "codeRabbitExplicitlyResolved",
            "unresolvedCodeRabbitThreads",
        )
        extracted = [self._extract_js_function(gate, name) for name in needed]
        self.assertIn("Review completed", extracted[5])
        self.assertIn("isResolved", extracted[-1])

        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to execute the shipped JS; failing closed instead of silently skipping",
        )

        head = "a" * 40
        old_head = "b" * 40
        driver = """
const assert = require('assert');
const owner = 'acme';
const repo = 'demo';
let REVIEWS = [];
let RUNS = [];
let THREAD_NODES = [];
let MUTATION_SHOULD_FAIL = false;
let MUTATIONS = 0;
const LIST_REVIEWS = { kind: 'listReviews' };
const LIST_RUNS = { kind: 'listRuns' };
const github = {
  rest: {
    pulls: { listReviews: LIST_REVIEWS },
    actions: { listWorkflowRunsForRepo: LIST_RUNS },
  },
  paginate: async (endpoint, params) => {
    if (endpoint === LIST_REVIEWS) return REVIEWS;
    if (endpoint === LIST_RUNS) return RUNS;
    throw new Error('unexpected paginate endpoint');
  },
  graphql: async (query, vars) => {
    if (query.includes('mutation')) {
      MUTATIONS += 1;
      if (MUTATION_SHOULD_FAIL) throw new Error('resolution failed');
      return { resolveReviewThread: { thread: { id: vars.threadId, isResolved: true } } };
    }
    return {
      repository: {
        pullRequest: {
          reviewThreads: { nodes: THREAD_NODES, pageInfo: { hasNextPage: false, endCursor: null } },
        },
      },
    };
  },
};
const core = { info() {}, notice() {}, warning() {} };
""" + "\n".join(extracted) + """
async function main() {
""" + f"""
const HEAD = {json.dumps(head)};
const OLD_HEAD = {json.dumps(old_head)};
const review = (over = {{}}) => Object.assign(
  {{
    user: {{ login: 'coderabbitai[bot]' }},
    commit_id: HEAD,
    state: 'APPROVED',
    submitted_at: '2026-01-01T00:00:00Z',
    id: 1,
    body: '',
  }},
  over,
);
const pr = {{ number: 7, head: {{ sha: HEAD, ref: 'feature' }} }};
const ciRun = () => [{{
  name: 'CI', head_sha: HEAD, status: 'completed',
  conclusion: 'success', id: 10, created_at: '2026-01-01T00:00:00Z',
}}];
function threadWith(id, replyBodies) {{
  const nodes = [{{ databaseId: 1, body: 'finding', author: {{ login: 'coderabbitai[bot]' }} }}];
  replyBodies.forEach((reply, index) => {{
    nodes.push({{ databaseId: 10 + index, body: reply, author: {{ login: 'coderabbitai[bot]' }} }});
  }});
  return [{{
    id, isResolved: false, isOutdated: false, path: 'a.ts',
    comments: {{ nodes }},
  }}];
}}
const results = {{}};
// 1. Exact-head approval with green CI and no findings merges.
REVIEWS = [review()];
RUNS = ciRun();
results.green_direct = !!(await directCodeRabbitReviewBasis(pr, HEAD));
// 2. Approval for a stale head must not merge the new head.
REVIEWS = [review({{ commit_id: OLD_HEAD }})];
RUNS = ciRun();
results.stale_head_blocked = (await directCodeRabbitReviewBasis(pr, HEAD)) === null;
// 3. A newer CHANGES_REQUESTED verdict blocks even when the status was
// overwritten with "Review skipped" by an auto-review-disabled event.
REVIEWS = [
  review({{ state: 'APPROVED', submitted_at: '2026-01-01T00:00:00Z', id: 1 }}),
  review({{ state: 'CHANGES_REQUESTED', submitted_at: '2026-01-02T00:00:00Z', id: 2 }}),
];
RUNS = ciRun();
const skippedStatus = {{ context: 'CodeRabbit', state: 'success', description: 'Review skipped' }};
const completedStatus = {{ context: 'CodeRabbit', state: 'success', description: 'Review completed' }};
const changesBasis = await directCodeRabbitReviewBasis(pr, HEAD);
results.changes_requested_blocked =
  changesBasis === null &&
  waitingForCompletedCodeRabbitReview(changesBasis, skippedStatus);
// 4. "Review skipped" must not erase a durable exact-head approval.
REVIEWS = [review({{ state: 'APPROVED' }})];
RUNS = ciRun();
const keptBasis = await directCodeRabbitReviewBasis(pr, HEAD);
results.skipped_keeps_approval =
  keptBasis !== null &&
  !waitingForCompletedCodeRabbitReview(keptBasis, skippedStatus) &&
  !waitingForCompletedCodeRabbitReview(keptBasis, completedStatus);
// 5. A nitpick newer than the decision blocks the merge.
REVIEWS = [
  review({{ state: 'APPROVED', submitted_at: '2026-01-01T00:00:00Z', id: 1 }}),
  review({{
    state: 'COMMENTED', submitted_at: '2026-01-02T00:00:00Z', id: 2,
    body: 'Nitpick comments (2): check bounds',
  }}),
];
RUNS = ciRun();
results.newer_nitpick_blocks =
  (await directCodeRabbitReviewBasis(pr, HEAD)) === null &&
  (await codeRabbitNitpickReviews(pr, HEAD)).length === 1;
// 6. A nitpick older than the latest decision is superseded and merges.
REVIEWS = [
  review({{
    state: 'COMMENTED', submitted_at: '2026-01-01T00:00:00Z', id: 1,
    body: 'Nitpick comments (3): stale advice',
  }}),
  review({{ state: 'APPROVED', submitted_at: '2026-01-02T00:00:00Z', id: 2 }}),
];
RUNS = ciRun();
results.superseded_nitpick_green =
  !!(await directCodeRabbitReviewBasis(pr, HEAD)) &&
  (await codeRabbitNitpickReviews(pr, HEAD)).length === 0;
// 7. A RESOLVED bot reply normalizes the thread and merges.
THREAD_NODES = threadWith('T1', ['**RESOLVED** fixed in latest push']);
MUTATIONS = 0;
MUTATION_SHOULD_FAIL = false;
results.resolved_normalized =
  (await unresolvedCodeRabbitThreads(pr)).length === 0 && MUTATIONS === 1;
// 8. UNRESOLVED always wins and blocks without a resolution attempt.
THREAD_NODES = threadWith('T2', ['UNRESOLVED: still broken on current HEAD']);
MUTATIONS = 0;
const stillOpen = await unresolvedCodeRabbitThreads(pr);
results.unresolved_blocks = stillOpen.length === 1 && MUTATIONS === 0;
// 9. A failed GitHub thread resolution stays fail-closed and blocks.
THREAD_NODES = threadWith('T3', ['✅ Review thread resolved.']);
MUTATIONS = 0;
MUTATION_SHOULD_FAIL = true;
results.normalization_failure_blocks =
  (await unresolvedCodeRabbitThreads(pr)).length === 1 && MUTATIONS === 1;
console.log('CASES:' + JSON.stringify(results));
}}
""" + """main().then(
  () => {},
  (err) => {
    console.error((err && err.stack) || err);
    process.exit(1);
  },
);
"""
        tmpdir = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, dir=tmpdir
        ) as handle:
            handle.write(driver)
            script = handle.name
        try:
            completed = subprocess.run(
                [node, script],
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            os.unlink(script)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        line = next(
            entry for entry in completed.stdout.splitlines() if entry.startswith("CASES:")
        )
        results = json.loads(line[len("CASES:"):])
        for case, green in results.items():
            with self.subTest(case=case):
                self.assertTrue(green, f"deadlock fixture {case} regressed")


if __name__ == "__main__":
    unittest.main()
