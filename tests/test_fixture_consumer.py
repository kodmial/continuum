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


class ConsumerToggleTests(unittest.TestCase):
    """The two-boolean contract, in both postures, from real fixture files."""

    CONTRACT = CONSUMER / ".github" / "continuum.yml"
    REPO_CONTRACT = ROOT / ".github" / "continuum.yml"

    def test_the_enabled_fixture_activates_the_single_review_implementation(self):
        enabled = load_config(str(self.CONTRACT))
        # The consumer does not choose; it only turns the loop on. The provider
        # is what `true` resolves to inside the engine, and no configuration
        # line in the consumer's own file names or selects one. Comments are
        # excluded deliberately: prose may explain the choice, but it may not
        # make one.
        self.assertEqual(enabled.review.provider, "coderabbit")
        declared = [
            line
            for line in self.CONTRACT.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            sorted(declared), ["release: false", "review: true"], declared
        )

    def test_the_disabled_fixture_resolves_to_no_traffic_at_all(self):
        # Continuum's own contract is the disabled posture: it is not a consumer
        # product, so adopting the contract declares nothing.
        disabled = load_config(str(self.REPO_CONTRACT))
        self.assertEqual(disabled.review.provider, "none")
        self.assertEqual(disabled.release.targets, ())

    def test_both_fixtures_declare_release_without_declaring_a_target(self):
        for path in (self.CONTRACT, self.REPO_CONTRACT):
            with self.subTest(path=path.name):
                config = load_config(str(path))
                self.assertEqual(config.release.targets, ())


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
