"""Deterministic contract tests for the executable PR-Agent canary path.

Issue #174 audit correction: the offline harness alone is insufficient.
There must be a durable, opt-in executable canary workflow/caller that
drives the real GitHub-facing enabled path. These tests pin that path
without network access: disabled by default, trusted caller-approved route only, real
/review + /verify command path, fail-closed gates, reviewer-only,
CodeRabbit untouched, and guaranteed cleanup.

Standard library only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-pr-agent-canary.yml"
STUB = ROOT / ".github" / "caller-stubs" / "continuum-pr-agent-canary.yml"

CANARY_INPUTS = (
    "canary_enabled",
    "base_ref",
    "opencode_model",
    "model",
    "max_tokens",
    "api_base",
    "bridge_port",
    "server_port",
    "pr_agent_version",
)


def name_of(path: Path) -> str:
    match = re.search(r"^name:\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)
    assert match, f"{path.name}: no name:"
    return match.group(1).strip()


class ExecutableCanaryContractTests(unittest.TestCase):
    def test_workflow_and_stub_exist_with_matching_name(self):
        self.assertTrue(WORKFLOW.is_file(), "reusable canary workflow is missing")
        self.assertTrue(STUB.is_file(), "canary caller stub is missing")
        self.assertEqual(name_of(WORKFLOW), name_of(STUB))
        self.assertIn("canary", name_of(WORKFLOW).lower())

    def test_caller_forwards_every_dispatch_input(self):
        stub = STUB.read_text(encoding="utf-8")
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for key in CANARY_INPUTS:
            with self.subTest(input=key):
                self.assertIn(f"{key}:", workflow, f"callee input {key} missing")
                self.assertIn(f"{key}:", stub, f"caller input {key} missing")
                self.assertIn(
                    f'{key}: "${{{{ inputs.{key} }}}}"', stub,
                    f"caller must forward {key}",
                )
        self.assertIn(
            "uses: kodmial/continuum/.github/workflows/continuum-pr-agent-canary.yml@main",
            stub,
        )
        self.assertIn("continuum_ref: main", stub)
        self.assertIn("secrets: inherit", stub)

    def test_disabled_by_default(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("canary_enabled:", workflow)
        self.assertIn("CONTINUUM_PR_AGENT_CANARY_ENABLED", workflow)
        self.assertIn("then to 'false'", workflow)

    def test_free_default_route_and_no_paid_secret(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('vars.OPENCODE_MODELS', body)
        self.assertIn('uses: kodmial/continuum/.github/actions/model-route@main', body)
        self.assertNotIn('muse-spark-1.3-contributor-free', body)
        self.assertIn("No paid-provider", body)
        self.assertNotIn("secrets.OPENCODE_API_KEY", body)
        self.assertNotIn("secrets.ANTHROPIC_API_KEY", body)
        self.assertNotIn("secrets.GROQ_API_KEY", body)
        self.assertNotIn("secrets.RENDER_API_KEY", body)

    def test_real_command_path(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("/review", body)
        self.assertIn("/verify", body)
        self.assertIn("finding=", body)

    def test_fail_closed_gates(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("concluded", body)
        self.assertIn("UNRESOLVED", body)
        self.assertIn("APPROVED", body)
        self.assertIn("CHANGES_REQUESTED", body)
        self.assertIn("timed out waiting", body)

    def test_reviewer_only_and_coderabbit_untouched(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("coderabbit", body.lower())
        self.assertIn("correct disposable", body)

    def test_review_run_lookup_uses_exact_dispatch_title_and_pre_command_baseline(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        lookup_start = body.index("const findDispatchedRun = async")
        lookup = body[lookup_start:body.index("const reviewComment =", lookup_start)]
        self.assertIn("event: 'workflow_dispatch'", lookup)
        self.assertIn("candidate.display_title", lookup)
        self.assertIn("runTitlePrefix", lookup)
        self.assertIn("runTitleSuffix", lookup)
        self.assertIn("github.rest.actions.getWorkflowRun", lookup)
        self.assertIn("targetRunId = run.id", lookup)
        self.assertIn("created: `>=${dispatchCreatedAfter}`", lookup)
        self.assertIn("page <= 5", lookup)
        self.assertIn("per_page: 100", lookup)
        self.assertNotIn("branch,", lookup)
        before = body.index("const before = new Set(baseline.data.workflow_runs.map((run) => run.id));")
        comment = body.index("const reviewComment = await github.rest.issues.createComment")
        self.assertLess(body.index("prNumber = pr.data.number;"), lookup_start)
        self.assertLess(body.index("head = pr.data.head.sha;"), lookup_start)
        self.assertLess(before, comment)
        self.assertIn("workflowRunIds.push(String(reviewedRun.id));", body)
        self.assertIn(
            "const prAgentRuns = async (pullNumber, headSha)", body,
            "the final evidence pass must use a defined bounded lookup",
        )
        self.assertIn("await prAgentRuns(prNumber, cleanHead)", body)

    def test_run_lookup_accepts_the_router_and_legacy_workflow_names(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        router_name = name_of(ROOT / ".github" / "workflows" / "pr-agent.yml")
        self.assertEqual("PR Agent (OpenCode-backed)", router_name)
        self.assertIn(f"'{router_name}'", body)
        self.assertIn("'PR Agent (OpenCode backend)'", body)
        self.assertIn("const prAgentRunNames = new Set([", body)
        self.assertIn("prAgentRunNames.has(run.name)", body)
        self.assertIn("prAgentRunNames.has(candidate.name)", body)
        self.assertNotIn(
            "run.name === 'PR Agent (OpenCode backend)'", body,
            "a single legacy name misses the router-installed workflow",
        )

    def test_evidence_pass_runs_after_clean_head_approval(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        approval = body.index("what: 'the clean-HEAD approval'")
        evidence = body.index("for (const run of (await prAgentRuns(prNumber, cleanHead))")
        record = body.index("const evidence = {")
        self.assertLess(
            approval, evidence,
            "the evidence pass must observe the run that produced the approval",
        )
        self.assertLess(
            evidence, record,
            "the evidence bundle must be recorded after the run-ID pass",
        )

    def test_cleanup_and_evidence(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("state: 'closed'", body)
        self.assertIn("--delete", body)
        self.assertIn("pr-agent-canary-evidence.json", body)
        self.assertIn("upload-artifact", body)

    def test_caller_permissions_cover_callee(self):
        stub = STUB.read_text(encoding="utf-8")
        for perm in ("contents: write", "issues: write", "pull-requests: write", "actions: read"):
            self.assertIn(perm, stub, f"caller must grant {perm}")


if __name__ == "__main__":
    unittest.main()
