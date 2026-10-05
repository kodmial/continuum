from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"


class OpenCode429RunnerRecoveryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = WORKFLOW.read_text(encoding="utf-8")

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

    def test_429_marks_runner_for_restart(self):
        for marker in (
            "FreeUsageLimitError",
            "HTTP[[:space:]]*429",
            "statusCode[^0-9]*429",
            "continuum-opencode-429",
            "exit 75",
        ):
            self.assertIn(marker, self.body)

    def test_runner_recovery_is_bounded_and_preserves_task_ownership(self):
        self.assertIn("recover-opencode-429:", self.body)
        self.assertIn("needs.opencode.outputs.restart_required == 'true'", self.body)
        self.assertIn("github.run_attempt < 4", self.body)
        self.assertIn("github.rest.actions.reRunWorkflow", self.body)
        self.assertIn("needs.opencode.outputs.restart_required != 'true'", self.body)


if __name__ == "__main__":
    unittest.main()
