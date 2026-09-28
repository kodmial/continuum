"""Deterministic reference-snapshot checks.

The NanoDictate PR-Agent implementation is preserved verbatim under
`reference/nanodictate-workflows/pr-agent/` and must never become an active
workflow. These tests keep the snapshot immutable, credential-free, and unwired
while the Continuum adapter stays the only executable review surface.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from continuum.review import pr_agent

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "reference" / "nanodictate-workflows" / "pr-agent"
WORKFLOWS = ROOT / ".github" / "workflows"

EXPECTED_FILES = {"pr-agent.yml", ".pr_agent.toml", "pr_agent_review_gate.py"}

SECRET_LIKE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"password\s*[=:]\s*\S+|(?i:api[_-]?key)\s*[=:]\s*['\"][^'\"]{8,}|"
    r"(?i:username|login)\s*[=:]\s*['\"][^'\"]{6,}@)"
)


class ReferenceImmutabilityTests(unittest.TestCase):
    def test_snapshot_layout_is_exact(self):
        present = {path.name for path in REFERENCE.iterdir() if path.is_file()}
        self.assertEqual(present, EXPECTED_FILES)

    def test_snapshot_contains_no_credentials(self):
        for path in REFERENCE.iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                SECRET_LIKE.search(text), f"credential-like value found in {path.name}"
            )

    def test_snapshot_never_inherits_secrets(self):
        for path in REFERENCE.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secrets: inherit", text)

    def test_reference_workflow_references_names_only(self):
        text = (REFERENCE / "pr-agent.yml").read_text(encoding="utf-8")
        self.assertIn("PR_AGENT_API_KEY", text)
        self.assertIn("PR_AGENT_API_BASE", text)
        self.assertHasNoInline("OPENAI.API_BASE: https://", text)

    def assertHasNoInline(self, needle: str, text: str) -> None:  # noqa: N802 - unittest name
        self.assertNotIn(needle, text)


class ReferenceNotWiredTests(unittest.TestCase):
    def test_reference_path_is_not_an_active_workflow(self):
        for workflow in WORKFLOWS.glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"uses:\s*[^\n]*nanodictate", text))
            for line in text.splitlines():
                if "nanodictate" not in line:
                    continue
                # Only the CI preservation guard may name the snapshot, and only
                # as a static `test -f reference/...` check that never executes it.
                self.assertIn("reference/nanodictate-workflows", line)
                self.assertIn("test -f", line)

    def test_active_pr_agent_workflow_is_a_rewrite_not_a_copy(self):
        reference = (REFERENCE / "pr-agent.yml").read_bytes()
        active = (WORKFLOWS / "pr-agent.yml").read_bytes()
        self.assertNotEqual(active, reference)
        text = (WORKFLOWS / "pr-agent.yml").read_text(encoding="utf-8")
        self.assertNotIn(".github/scripts/pr_agent_review_gate.py", text)
        self.assertNotIn("PRA-", text)


class AdapterParityTests(unittest.TestCase):
    def test_provider_name_matches_the_reference_surface(self):
        self.assertEqual(pr_agent.PROVIDER_NAME, "pr-agent")

    def test_parity_surface_is_exported(self):
        for name in ("parse_summary", "collect", "profile_for", "is_boilerplate_title"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(pr_agent, name)))

    def test_gate_script_survives_as_reference_only(self):
        self.assertTrue((REFERENCE / "pr_agent_review_gate.py").is_file())
        source = (REFERENCE / "pr_agent_review_gate.py").read_text(encoding="utf-8")
        self.assertNotIn("continuum", source)


if __name__ == "__main__":
    unittest.main()