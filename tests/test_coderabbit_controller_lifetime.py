"""Regression guards for CodeRabbit global-controller lease lifetime."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-coderabbit-retry.yml"


class CodeRabbitControllerLifetimeContract(unittest.TestCase):
    def test_waiter_releases_slot_before_job_timeout(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("const controllerStartedAt = Date.now();", body)
        self.assertIn("const MAX_CONTROLLER_LIFETIME_MS = 55 * 60_000;", body)
        self.assertIn("timeout-minutes: 85", body)
        self.assertIn("controllerStartedAt + MAX_CONTROLLER_LIFETIME_MS - now", body)
        self.assertIn("waitMs + 5_000 >= remainingLifetimeMs", body)
        self.assertIn("return false;", body.split("remainingLifetimeMs", 1)[1])
        self.assertIn("Date.now() - controllerStartedAt >= MAX_CONTROLLER_LIFETIME_MS", body)
        self.assertIn("release", body.split("Date.now() - controllerStartedAt", 1)[1])

    def test_serialized_review_queue_unchanged(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: coderabbit-rate-limit-controller-", body)
        self.assertIn("cancel-in-progress: false", body)
        self.assertIn("const REVIEW_COOLDOWN_MS = 60 * 60_000;", body)
        self.assertIn("const IN_FLIGHT_TIMEOUT_MS = 30 * 60_000;", body)
        self.assertIn("core.notice('No CodeRabbit review is currently queued.')", body)


if __name__ == "__main__":
    unittest.main()
