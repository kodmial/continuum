"""Regression contract for nested reviewer timeouts and approved route ownership."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReviewerRetryPolicy(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
