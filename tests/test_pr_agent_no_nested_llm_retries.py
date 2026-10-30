"""Regression contract for nested reviewer timeouts and approved route ownership."""
from pathlib import Path
import re
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

    def test_native_improve_retirement_and_review_receipt_are_mutually_exclusive(self):
        workflow = (ROOT / ".github/workflows/continuum-pr-agent.yml").read_text()
        improve = workflow.split(
            "      - name: Run upstream improve on the exact HEAD when repair value remains", 1
        )[1].split("      - name: Record skipped automatic improve for the clean HEAD", 1)[0]
        for expected in (
            'improve_log="$RUNNER_TEMP/pr-agent-improve-execution.log"',
            "provider_failure=timeout",
            "provider_failure=quota_429",
            "provider_failure=upstream",
            'echo "provider_failure=timeout" >> "$GITHUB_OUTPUT"',
            "if [[ \"$improve_ok\" != true ]]; then",
        ):
            self.assertIn(expected, improve)
        cooldown = workflow.split(
            "      - name: Cool down failed real provider route before the bounded retry", 1
        )[1].split("      - name: Attest successful independent OpenCode review", 1)[0]
        self.assertIn("steps.improve.outputs.provider_failure != ''", cooldown)
        self.assertIn(
            "steps.pragent.outputs.provider_failure || steps.improve.outputs.provider_failure",
            cooldown)
        receipt = workflow.split(
            "      - name: Attest successful independent OpenCode review", 1
        )[1].split("      - name: Publish private exact-head repair handoff", 1)[0]
        self.assertIn("steps.improve.outcome != 'failure'", receipt)


    def test_malformed_live_yaml_fails_over_without_bypassing_review(self):
        workflow = (ROOT / ".github/workflows/continuum-pr-agent.yml").read_text()
        full_review = workflow.split(
            "      - name: Run upstream full review on the exact HEAD", 1
        )[1].split(
            "      - name: Attest successful independent OpenCode review", 1
        )[0]
        # A parser-level failure after successful OpenCode HTTP calls is a
        # model response-format error, not a code-quality finding, transport
        # timeout, or an approval. It MUST retire just the selected route.
        self.assertIn('provider_failure=invalid_review_format', full_review)
        # Exercise the EXACT production grep expression with sanitized
        # excerpts of the AA #410 review failure, not a hand-written regex.
        matcher_line = next(
            line for line in full_review.splitlines()
            if "elif grep -Eiq" in line
            and "Failed to parse AI prediction after fallbacks" in line
        )
        expression = matcher_line.split("'", 2)[1]
        for log_line in (
            "pr_agent.algo.utils:load_yaml:886 - Failed to parse AI prediction after fallbacks",
            "ValueError: review chunk 2 did not contain a non-empty review mapping",
        ):
            with self.subTest(log_line=log_line):
                self.assertRegex(log_line, re.compile(expression, re.I))
        for non_model_error in (
            "CI failed: invalid YAML in application workflow",
            "Review failed because network permissions are missing",
            "PR diff mentions: review chunk 2 did not contain a non-empty review mapping",
            "PR-Agent returned clean review after a recoverable YAML warning",
        ):
            with self.subTest(non_model_error=non_model_error):
                self.assertIsNone(re.search(expression, non_model_error, re.I))

        self.assertIn(
            'pr_agent[.]algo[.]utils.*Failed to parse AI prediction after fallbacks',
            full_review)
        self.assertIn(
            'ValueError.*review chunk [0-9]+ did not contain a non-empty review mapping',
            full_review)
        self.assertIn("operation: fail", full_review)
        self.assertIn("steps.pragent.outputs.provider_failure", full_review)
        self.assertIn(
            "steps.pragent.outcome == 'success'", full_review)
        self.assertIn(
            "Return strictly valid YAML in the requested review schema",
            full_review)
        # The upstream reviewer remains independent and must still succeed:
        # format failure alone NEVER authorizes auto-merge.
        self.assertIn("CONFIG__FALLBACK_MODELS: '[]'", full_review)
        self.assertIn("CONFIG__NUM_RETRIES: '0'", full_review)

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
        self.assertEqual(workflow.count("CONFIG__AI_TIMEOUT: '1260'"), 2)
        self.assertIn("PR_AGENT_TIMEOUT_SECONDS: '1200'", workflow)
        self.assertNotIn("CONFIG__AI_TIMEOUT: '660'", workflow)
        self.assertNotIn("PR_AGENT_TIMEOUT_SECONDS: '600'", workflow)
        self.assertIn("Report safe OpenCode bridge phase timings", workflow)


if __name__ == "__main__":
    unittest.main()
