"""Deterministic reference-snapshot checks.

The NanoDictate PR-Agent implementation is preserved verbatim under
`reference/nanodictate-workflows/pr-agent/`. The capability it provided is now
Continuum's, so the provider surface is active under `.github/workflows` and the
snapshot keeps only its provenance. These tests keep the snapshot immutable,
credential-free, and unwired, and keep the promoted surface the single
implementation rather than one of several.
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

    def test_the_snapshot_is_never_executed_by_an_active_workflow(self):
        # Absorbing the capability means the implementation lives in
        # `.github/workflows` now. The snapshot stays for provenance only, so
        # nothing under `reference/` may be a checkout, a dispatch, or a run.
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "reference/" not in line:
                    continue
                self.assertRegex(
                    line.strip(),
                    r"^test -f reference/",
                    "{} reaches into the reference tree: {}".format(
                        workflow.name, line.strip()
                    ),
                )


class ProviderSurfaceTests(unittest.TestCase):
    """Exactly one implementation of the provider, and it is the active one."""

    ACTIVE_PROVIDER = ("pr-agent.yml", "pr-agent-comment.yml")

    def test_the_provider_surface_is_active(self):
        for name in self.ACTIVE_PROVIDER:
            with self.subTest(workflow=name):
                self.assertTrue((WORKFLOWS / name).is_file(), name)

    def test_no_second_pr_agent_implementation_is_kept(self):
        # A promoted draft left beside the promoted file is two places to
        # update and a second thing for a reader to mistake for the
        # implementation, so the archive goes when the surface goes live. The
        # provider fixture is exempt because it is wiring and is asserted to
        # contain no implementation of its own.
        self.assertFalse((ROOT / "reference" / "post-mvp-pr-agent-workflows").exists())
        stray = sorted(
            path
            for path in ROOT.rglob("pr-agent*.yml")
            if ".git" not in path.parts
            and "nanodictate-workflows" not in path.parts
            and "fixtures" not in path.parts
            and path.parent != WORKFLOWS
        )
        self.assertEqual(stray, [], stray)

    def test_the_nano_snapshot_is_not_the_active_surface(self):
        # The absorbed file and the live one are different files: the active
        # surface is Continuum's own, so nothing here was ever wired as-is.
        snapshot = (REFERENCE / "pr-agent.yml").read_text(encoding="utf-8")
        active = (WORKFLOWS / "pr-agent.yml").read_text(encoding="utf-8")
        self.assertNotEqual(snapshot, active)


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