"""Deterministic contract tests for the executable PR-Agent canary path.

Issue #174 audit correction: the offline harness alone is insufficient.
There must be a durable, opt-in executable canary workflow/caller that
drives the real GitHub-facing enabled path. These tests pin that path
without network access: disabled by default, free route only, real
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

FREE_MODEL = "opencode/muse-spark-1.3-contributor-free"
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
        self.assertIn(FREE_MODEL, body)
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
