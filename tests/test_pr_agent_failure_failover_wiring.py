"""Contract test: a failed real PR-Agent run changes the *next* model route."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FailureFailoverWiring(unittest.TestCase):
    def test_live_reviewer_failure_reports_the_frozen_model_before_retry(self):
        workflow = (ROOT / ".github/workflows/continuum-pr-agent.yml").read_text()
        select = workflow.index("id: model_route")
        review = workflow.index("id: pragent")
        failed = workflow.index("name: Record failed real PR-Agent execution for route failover")
        retry = workflow.index("name: Schedule bounded retry for retryable PR-Agent review failure")
        self.assertLess(select, review)
        self.assertLess(review, failed)
        self.assertLess(failed, retry)
        snippet = workflow[failed:workflow.index("      - name:", failed + 15)]
        for term in (
            "always()",
            "steps.pragent.outcome == 'failure'",
            "steps.model_route.outcome == 'success'",
            "operation: fail",
            "selected-model: ${{ steps.model_route.outputs.model }}",
            "profile: pr-review",
            "task-class: review",
            "state-repository: ${{ env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY }}",
            "state-token: ${{ env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED",
        ):
            with self.subTest(term=term):
                self.assertIn(term, snippet)
        self.assertNotIn("continue-on-error: true", snippet)

    def test_shared_action_exposes_review_failure_without_new_model_defaults(self):
        action = (ROOT / ".github/actions/model-route/action.yml").read_text()
        self.assertIn("select|record|fail)", action)
        self.assertIn('python3 "$GITHUB_ACTION_PATH/../../../src/continuum/model_routing.py" "$OPERATION"', action)
        self.assertNotIn("deepseek-v4", action)
        self.assertNotIn("glm-", action)
        self.assertNotIn("nemotron-", action)


if __name__ == "__main__":
    unittest.main()
