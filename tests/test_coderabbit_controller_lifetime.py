"""Regression guards for CodeRabbit global-controller lease lifetime."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-coderabbit-retry.yml"


class CodeRabbitControllerLifetimeContract(unittest.TestCase):
    def test_waiter_releases_slot_before_job_timeout(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("const controllerStartedAt = Date.now();", body)
        self.assertIn("const MAX_CONTROLLER_LIFETIME_MS = 80 * 60_000;", body)
        self.assertIn("timeout-minutes: 90", body)
        self.assertIn("controllerStartedAt + MAX_CONTROLLER_LIFETIME_MS - now", body)
        self.assertIn("waitMs + 5_000 >= remainingLifetimeMs", body)
        self.assertIn("return false;", body.split("remainingLifetimeMs", 1)[1])
        self.assertIn("Date.now() - controllerStartedAt >= MAX_CONTROLLER_LIFETIME_MS", body)
        self.assertIn("releasing global lease", body.split("Date.now() - controllerStartedAt", 1)[1])

    def test_hourly_quota_wait_is_within_one_controller_lifetime(self):
        """Due reviews must resume without relying on delayed GitHub cron."""
        body = WORKFLOW.read_text(encoding="utf-8")

        def minutes(pattern):
            match = re.search(pattern, body)
            self.assertIsNotNone(match, pattern)
            return int(match.group(1))

        quota = minutes(r"const REVIEW_COOLDOWN_MS = (\d+) \* 60_000;")
        defer_cap = minutes(r"const MAX_PUBLIC_DEFER_WAIT_MS = (\d+) \* 60_000;")
        lifetime = minutes(r"const MAX_CONTROLLER_LIFETIME_MS = (\d+) \* 60_000;")
        job_timeout = minutes(r"timeout-minutes: (\d+)")

        self.assertGreater(lifetime, quota + 1)
        self.assertGreater(lifetime, defer_cap + 5)
        self.assertGreater(job_timeout, lifetime + 5)

    def test_serialized_review_queue_unchanged(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: coderabbit-rate-limit-controller-", body)
        self.assertIn("cancel-in-progress: false", body)
        self.assertIn("const REVIEW_COOLDOWN_MS = 60 * 60_000;", body)
        self.assertIn("const IN_FLIGHT_TIMEOUT_MS = 30 * 60_000;", body)
        self.assertIn("core.notice('No CodeRabbit review is currently queued.')", body)


if __name__ == "__main__":
    unittest.main()
