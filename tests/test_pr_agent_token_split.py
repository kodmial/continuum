"""Regression tests for Work-Lock #58 item 4 (issue #232).

Same-repository, read-only PR-Agent admission and revalidation calls in
`.github/workflows/continuum-pr-agent.yml` must run on the
repository-scoped token instead of the shared TAP_PAT budget, while tool
execution credentials and every write/dispatch path keep PAT identity.
Exact-HEAD admission and CI-gating semantics must be unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "continuum-pr-agent.yml")
STUB = os.path.join(ROOT, ".github", "caller-stubs", "continuum-pr-agent.yml")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def step_body(body: str, step_name: str) -> str:
    """Raw YAML of one named step, up to the next step or job boundary."""
    lines = body.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines)
         if line.startswith(f"      - name: {step_name}")),
        None,
    )
    assert start is not None, f"step is missing: {step_name}"
    rest = lines[start + 1:]
    stop = next(
        (index for index, line in enumerate(rest)
         if (line.startswith("      - name: ") or (len(line) - len(line.lstrip()) <= 6 and line.strip()))
         and not line.strip().startswith("#")),
        None,
    )
    return "".join(lines[start:start + 1 + stop]) if stop is not None else "".join(lines[start:])


class PrAgentTokenSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = read(WORKFLOW)
        with open(WORKFLOW, "r", encoding="utf-8") as handle:
            cls.workflow = yaml.safe_load(handle)
        with open(STUB, "r", encoding="utf-8") as handle:
            cls.stub = yaml.safe_load(handle)

    def test_repository_token_has_the_migrated_read_scopes(self):
        self.assertEqual(self.workflow["permissions"]["actions"], "read")
        job_permissions = self.workflow["jobs"]["pr_agent"]["permissions"]
        self.assertEqual(job_permissions["actions"], "read")
        self.assertEqual(job_permissions["contents"], "read")
        self.assertEqual(job_permissions["pull-requests"], "write")
        # issues:write covers the comment reads the persistent-state
        # export performs; no separate issues:read scope is required.
        self.assertEqual(job_permissions["issues"], "write")
        self.assertEqual(self.stub["permissions"]["actions"], "read")

    def test_admission_reads_use_the_repository_token_client(self):
        admit = step_body(self.body, "Admit only a review-ready PR with green CI on the exact HEAD")
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", admit)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", admit)
        self.assertIn("new github.constructor({ auth: readToken, baseUrl: readBaseUrl })", admit)
        self.assertIn("github.request.endpoint.DEFAULTS.baseUrl", admit)
        self.assertIn("process.env.GITHUB_API_URL", admit)
        self.assertIn("async function withReadFallback(fn)", admit)
        self.assertIn("client.rest.pulls.get", admit)
        self.assertIn("client.rest.actions.listWorkflowRunsForRepo", admit)
        self.assertIn("client.paginate(", admit)
        self.assertNotIn("require('@actions/github')", admit)
        self.assertNotIn('require("@actions/github")', admit)
        self.assertNotIn("await github.rest.pulls.get({", admit)
        self.assertNotIn("await github.paginate(", admit)

    def test_admission_keeps_exact_head_and_ci_gating_semantics(self):
        admit = step_body(self.body, "Admit only a review-ready PR with green CI on the exact HEAD")
        for needle in (
            "expected !== headSha.toLowerCase()",
            "run.name === ciWorkflowName && run.head_sha === headSha",
            "latestCi.status !== 'completed'",
            "latestCi.conclusion !== 'success'",
            "Stale admission ignored:",
            "is not same-repo; skipping PR-Agent review.",
            "has no successful current-head ",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, admit)

    def test_post_review_revalidation_reads_use_the_repository_token_client(self):
        result = step_body(self.body, "Revalidate the PR head and native review output after review")
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", result)
        self.assertIn("new github.constructor({ auth: readToken, baseUrl: readBaseUrl })", result)
        self.assertIn("async function withReadFallback(fn)", result)
        self.assertIn("client.rest.pulls.get", result)
        self.assertIn("PR head moved during review", result)
        self.assertNotIn("await github.rest.pulls.get({", result)
        self.assertNotIn("require('@actions/github')", result)

    def test_shell_revalidation_reads_use_the_repository_token(self):
        before = step_body(self.body, "Revalidate the admitted exact HEAD immediately before review")
        self.assertIn("GH_TOKEN: ${{ github.token }}", before)
        self.assertIn("actions/runs?event=pull_request&head_sha=$ADMITTED_SHA", before)
        self.assertNotIn("secrets.TAP_PAT", before)

        persistent = step_body(self.body, "Export native persistent finding state for the reviewed HEAD")
        self.assertIn("GH_TOKEN: ${{ github.token }}", persistent)
        self.assertNotIn("secrets.TAP_PAT", persistent)

        fail_closed = step_body(self.body, "Fail closed on a moved head")
        self.assertIn("GH_TOKEN: ${{ github.token }}", fail_closed)
        self.assertNotIn("secrets.TAP_PAT", fail_closed)

    def test_retry_reads_use_the_repository_token_but_dispatch_keeps_pat(self):
        retry = step_body(self.body, "Schedule bounded retry for retryable PR-Agent review failure")
        self.assertIn("GH_TOKEN: ${{ github.token }}", retry)
        self.assertIn("RETRY_DISPATCH_TOKEN: ${{ secrets.TAP_PAT }}", retry)
        self.assertIn('GH_TOKEN="$RETRY_DISPATCH_TOKEN" gh workflow run', retry)
        # The same-repository revalidation reads stay on the step token.
        self.assertIn('gh pr view "$PR_NUMBER"', retry)
        self.assertIn("actions/runs?event=pull_request&head_sha=$HEAD_SHA", retry)
        self.assertIn('gh repo view "$GITHUB_REPOSITORY"', retry)
        # Exact-HEAD retirement semantics are unchanged.
        self.assertIn("PR state changed during retry backoff; stale retry cancelled.", retry)
        self.assertIn("Exact HEAD no longer has green CI; retry cancelled until a fresh CI event.", retry)

    def test_tool_execution_and_writes_keep_pat_identity(self):
        tool = step_body(self.body, "Run upstream full review and full improve on the exact HEAD")
        self.assertIn("GITHUB__USER_TOKEN: ${{ secrets.TAP_PAT }}", tool)
        self.assertIn("GH_TOKEN: ${{ secrets.TAP_PAT }}", tool)

        for step_name in (
            "Mark PR-Agent review in flight",
            "Normalize persistent improve presentation",
            "Clear settled PR-Agent controller state",
            "Publish durable PR-Agent review state",
            "Publish failed PR-Agent review state",
            "Update persistent PR-Agent retry controller state",
        ):
            with self.subTest(step=step_name):
                self.assertIn("github-token: ${{ secrets.TAP_PAT }}", step_body(self.body, step_name))

        self.assertIn("await github.rest.repos.createCommitStatus({", self.body)
        self.assertIn("await github.rest.issues.deleteComment({", self.body)
        # The cross-repository runtime bundle fetch is not a
        # same-repository read, so it stays PAT-backed.
        runtime = step_body(self.body, "Resolve the Continuum-owned PR-Agent runtime bundle")
        self.assertIn("GH_TOKEN: ${{ secrets.TAP_PAT }}", runtime)

    def test_read_client_constructs_in_github_script_sandbox(self):
        """Runtime-shaped guard mirroring the recovery sandbox check.

        The github-script sandbox does not resolve
        `require('@actions/github')`. The admission and post-review blocks
        must therefore build their repository-token read clients from the
        supplied `github.constructor`, preserving the enterprise base URL.
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for the github-script sandbox check")

        harness = r"""
const assert = require('assert');
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

        # The executed semantics must match the shipped workflow text.
        for needle in (
            "const readToken = String(process.env.READ_GITHUB_TOKEN",
            "github.request.endpoint.DEFAULTS.baseUrl",
            "process.env.GITHUB_API_URL",
            "new github.constructor({ auth: readToken, baseUrl: readBaseUrl })",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.body)


if __name__ == "__main__":
    unittest.main()
