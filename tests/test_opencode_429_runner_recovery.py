from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"


class OpenCode429RunnerRecoveryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = WORKFLOW.read_text(encoding="utf-8")
        cls.watchdog = WATCHDOG.read_text(encoding="utf-8")

    def test_all_agent_invocations_use_rate_limit_wrapper(self):
        self.assertIn("Install Continuum OpenCode rate-limit wrapper", self.body)
        self.assertIn("continuum-opencode github run", self.body)
        self.assertGreaterEqual(
            self.body.count('continuum-opencode run --auto --model "$OPENCODE_MODEL" "$PROMPT"'),
            5,
        )
        active_lines = [
            line.strip()
            for line in self.body.splitlines()
            if line.strip().startswith("opencode run ")
            or line.strip() == "run: opencode github run"
        ]
        self.assertEqual(active_lines, [])

    def test_429_marks_current_runner_for_retirement(self):
        for marker in (
            "FreeUsageLimitError",
            "HTTP[^0-9]*429",
            "statusCode[^0-9]*429",
            "CONTINUUM_OPENCODE_429_RESTART_REQUIRED",
            "continuum-opencode-429",
            "exit 75",
        ):
            self.assertIn(marker, self.body)
        self.assertIn("restart_required=true", self.body)
        self.assertIn("Publish OpenCode 429 recovery artifact", self.body)

    def test_completed_run_watchdog_restarts_same_workflow_on_fresh_runner(self):
        for marker in (
            "max_429_runner_restarts",
            "AUTOMATION_WATCHDOG_MAX_429_RUNNER_RESTARTS",
            "CONTINUUM_OPENCODE_429_RESTART_REQUIRED",
            "listJobsForWorkflowRun",
            "reRunWorkflow",
            "run.run_attempt",
            "fresh runner(s)",
        ):
            self.assertIn(marker, self.watchdog)

    def test_429_recovery_is_bounded_and_does_not_release_task_locks(self):
        self.assertIn(
            "const max429Restarts = Number.parseInt(",
            self.watchdog,
        )
        self.assertIn("if (attempt <= max429Restarts)", self.watchdog)
        self.assertIn("needs.opencode.outputs.restart_required != 'true'", self.body)
        self.assertIn(
            "OpenCode 429 fresh-runner recovery exhausted",
            self.watchdog,
        )


if __name__ == "__main__":
    unittest.main()
