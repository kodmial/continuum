"""MVP boundary checks for post-MVP PR-Agent material."""

from __future__ import annotations

import pathlib
import re
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


class ActiveWorkflowAllowlistTests(unittest.TestCase):
    """A workflow this repository did not intend to run must not be runnable.

    The boundary is enforced in `ci.yml` by matching every file in
    `.github/workflows/` against a name allowlist. That check only works while
    the allowlist and the directory agree, and a workflow added without an
    allowlist entry fails CI in a way that reads like a mistake somewhere else.
    So the agreement is asserted here too, where the failure can say which of
    the two drifted.
    """

    ALLOWLIST_LINE = re.compile(r"allowed='\^?\((?P<names>[^)]*)\)\\\.ya\?ml\$'")

    def setUp(self):
        text = (ACTIVE / "ci.yml").read_text(encoding="utf-8")
        match = self.ALLOWLIST_LINE.search(text)
        self.assertIsNotNone(match, "ci.yml no longer declares the workflow allowlist")
        self.allowed = {
            name for name in match.group("names").split("|") if name
        }

    def test_every_active_workflow_is_allowlisted(self):
        present = {path.stem for path in ACTIVE.glob("*.yml")}
        self.assertEqual(
            present - self.allowed,
            set(),
            "these workflows would be rejected by ci.yml's allowlist",
        )

    def test_the_allowlist_names_no_workflow_that_does_not_exist(self):
        present = {path.stem for path in ACTIVE.glob("*.yml")}
        self.assertEqual(
            self.allowed - present,
            set(),
            "the allowlist names workflows that are not here",
        )

    def test_the_release_verification_gate_is_reachable_only_by_a_call(self):
        # The gate decides whether a publication may proceed, and it reads the
        # verification run of the commit its own run is on. If it also reacted to
        # repository events it could be retriggered by the publication it
        # cleared -- so it declares exactly one trigger, and no run of it ever
        # appears in the `workflow_run` stream for anything to subscribe to.
        text = (ACTIVE / "release-verify.yml").read_text(encoding="utf-8")
        triggers = re.search(r"^on:\s*$(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(triggers, "release-verify.yml declares no trigger block")
        top = [
            line.strip().split(":", 1)[0]
            for line in triggers.group(1).splitlines()
            if line.startswith("  ")
            and not line.startswith("   ")
            and line.strip()
            and not line.strip().startswith("#")
        ]
        self.assertEqual(top, ["workflow_call"])
        self.assertNotIn("workflow_run", text)

    def test_the_release_verification_gate_never_checks_out_a_pull_request(self):
        # It runs on `workflow_call` from a trusted caller and reads the API. A
        # checkout of a candidate's head here would execute attacker code in a
        # job that decides whether a release happens, and the persisted token
        # would outlive the run in `.git/config`.
        text = (ACTIVE / "release-verify.yml").read_text(encoding="utf-8")
        self.assertNotIn("pull_request_target", text)
        self.assertEqual(text.count("uses: actions/checkout@"), 1)
        checkout = text.split("uses: actions/checkout@", 1)[1]
        self.assertIn("persist-credentials: false", checkout.split("steps:", 1)[0])


if __name__ == "__main__":
    unittest.main()
