"""MVP boundary checks for post-MVP PR-Agent material."""

from __future__ import annotations

import pathlib
import unittest

from continuum.config import load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIVE = ROOT / ".github" / "workflows"
REFERENCE = ROOT / "reference" / "post-mvp-pr-agent-workflows"
CONSUMER = ROOT / "fixtures" / "consumer-repo"


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


class ConsumerConfigTests(unittest.TestCase):
    """The fixture is documentation that executes, so it must stay valid."""

    def setUp(self):
        self.config = load_config(str(CONSUMER / ".continuum.yml"))

    def test_the_fixture_selects_a_provider_and_a_queue(self):
        self.assertEqual(self.config.review.provider, "pr-agent")
        queue = self.config.review.queue
        self.assertEqual(queue.ready_label, "review-ready")
        self.assertIn("review-paused", queue.block_labels)
        self.assertEqual(queue.priority_labels[0], "priority:p0")
        self.assertEqual(queue.cooldown_minutes, 60)

    def test_the_fixture_still_resolves_provider_credentials_by_name(self):
        self.assertEqual(
            self.config.review.pr_agent.api_key_secret, "PR_AGENT_API_KEY"
        )


if __name__ == "__main__":
    unittest.main()
