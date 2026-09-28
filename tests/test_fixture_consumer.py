"""MVP boundary checks for post-MVP PR-Agent material."""

from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVE = ROOT / ".github" / "workflows"
REFERENCE = ROOT / "reference" / "post-mvp-pr-agent-workflows"


class MvpBoundaryTests(unittest.TestCase):
    def test_pr_agent_workflows_are_not_active_in_mvp(self):
        self.assertFalse((ACTIVE / "pr-agent.yml").exists())
        self.assertFalse((ACTIVE / "pr-agent-comment.yml").exists())

    def test_post_mvp_pr_agent_reference_is_preserved(self):
        self.assertTrue((REFERENCE / "pr-agent.yml").is_file())
        self.assertTrue((REFERENCE / "pr-agent-comment.yml").is_file())

    def test_reference_is_non_executable_by_github_actions(self):
        for path in REFERENCE.glob("*.yml"):
            self.assertNotIn(".github/workflows", str(path))


if __name__ == "__main__":
    unittest.main()
