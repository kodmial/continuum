"""Deterministic fixture checks: the consumer repo wires the reusable workflow."""

from __future__ import annotations

import pathlib
import unittest

from continuum import config as config_module

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "consumer-repo"


class FixtureConfigTests(unittest.TestCase):
    def test_consumer_config_is_valid(self):
        config = config_module.load_config(str(FIXTURE / ".continuum.yml"))
        self.assertTrue(config.review.enabled)
        self.assertEqual(config.review.provider, config_module.PROVIDER_PR_AGENT)
        self.assertTrue(config.review.block_merge)
        self.assertEqual(config.review.status_context, config_module.DEFAULT_STATUS_CONTEXT)

    def test_consumer_config_references_names_not_values(self):
        config = config_module.load_config(str(FIXTURE / ".continuum.yml"))
        variables = config_module.required_variable_names(config)
        secrets = config_module.required_secret_names(config)
        self.assertIn("PR_AGENT_API_BASE", variables)
        self.assertIn("PR_AGENT_MODEL", variables)
        self.assertIn("PR_AGENT_API_KEY", secrets)
        settings = config.review.provider_settings()
        self.assertEqual(settings.api_base_var, "PR_AGENT_API_BASE")
        self.assertEqual(settings.api_key_secret, "PR_AGENT_API_KEY")

    def test_consumer_config_contains_no_secret_values(self):
        text = (FIXTURE / ".continuum.yml").read_text(encoding="utf-8")
        self.assertNotIn("https://", text)
        self.assertNotIn("sk-", text)
        self.assertNotIn("gpt-", text)


class FixtureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (FIXTURE / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")

    def test_calls_the_reusable_workflow_by_ref(self):
        self.assertIn("uses: kodmial/continuum/.github/workflows/pr-agent.yml@main", self.workflow)

    def test_never_inherits_secrets(self):
        self.assertNotIn("secrets: inherit", self.workflow)

    def test_wires_only_explicit_secrets(self):
        self.assertIn("continuum_token: ${{ secrets.GITHUB_TOKEN }}", self.workflow)
        self.assertIn("pr_agent_api_key: ${{ secrets.PR_AGENT_API_KEY }}", self.workflow)

    def test_passes_required_inputs(self):
        for needle in (
            "pr_number: ${{ github.event.pull_request.number }}",
            "head_sha: ${{ github.event.pull_request.head.sha }}",
            "api_base: ${{ vars.PR_AGENT_API_BASE }}",
            "model: ${{ vars.PR_AGENT_MODEL }}",
            "max_tokens: ${{ vars.PR_AGENT_MAX_TOKENS }}",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.workflow)

    def test_callee_workflow_is_present_and_pure_workflow_call(self):
        reusable = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-agent.yml"
        text = reusable.read_text(encoding="utf-8")
        self.assertIn("on:", text)
        self.assertIn("workflow_call:", text)
        self.assertNotIn("secrets: inherit", text)


if __name__ == "__main__":
    unittest.main()