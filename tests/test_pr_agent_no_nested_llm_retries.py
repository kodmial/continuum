"""Regression contract for nested reviewer timeouts and approved route ownership."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReviewerRetryPolicy(unittest.TestCase):
    def test_repair_provider_outage_retires_route_before_retry(self):
        workflow = (ROOT / ".github/workflows/continuum-pr-agent-repair.yml").read_text()
        for expected in (
            "provider_failure=unavailable",
            "provider_failure=timeout",
            "provider_failure=upstream",
            "Retire verified failed OpenCode repair route",
            "failure-reason: ${{ steps.repair_pass.outputs.provider_failure }}",
            "selected-model: ${{ steps.model_route.outputs.model }}",
            "steps.model_route.outcome == 'failure'",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, workflow)
        self.assertIn(
            "steps.repair_pass.outputs.provider_failure != ''", workflow
        )

    def test_both_native_review_and_improve_disable_hidden_llm_retries(self):
        workflow = (ROOT / ".github/workflows/continuum-pr-agent.yml").read_text()
        for setting in (
            "CONFIG__RETRY_SAME_MODEL_ON_TIMEOUT: 'false'",
            "CONFIG__NUM_RETRIES: '0'",
            "CONFIG__FALLBACK_MODELS: '[]'",
        ):
            with self.subTest(setting=setting):
                self.assertEqual(workflow.count(setting), 2)
        self.assertIn("steps.pragent.outputs.provider_failure", workflow)
        self.assertIn("operation: fail", workflow)
        self.assertIn("steps.model_route.outputs.model", workflow)
        self.assertEqual(workflow.count("CONFIG__AI_TIMEOUT: '200'"), 2)
        self.assertIn("PR_AGENT_TIMEOUT_SECONDS: '170'", workflow)
        self.assertIn("Report safe OpenCode bridge phase timings", workflow)


if __name__ == "__main__":
    unittest.main()
