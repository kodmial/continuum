#!/usr/bin/env python3
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import failure_retry


class FailureClassificationTests(unittest.TestCase):
    def test_network_failures_are_transient_infrastructure(self):
        samples = [
            "curl: (6) Could not resolve host: opencode.ai",
            "curl: (28) Connection timed out after 10000 milliseconds",
            "HTTP 429 Too Many Requests",
            "HTTP/1.1 503 Service Unavailable",
            "read ECONNRESET",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                result = failure_retry.classify(sample, failed_step="Run OpenCode")
                self.assertEqual(result.failure_class, "transient_infra")
                self.assertTrue(result.retryable)

    def test_agent_failure_is_semantic_only_when_the_agent_step_ran(self):
        result = failure_retry.classify("model returned an unrecoverable task error", failed_step="Run OpenCode")
        self.assertEqual(result.failure_class, "agent_execution")
        self.assertFalse(result.retryable)

    def test_pre_agent_failure_is_control_plane_not_semantic(self):
        result = failure_retry.classify(
            "python3: can't open file '.github/scripts/opencode_runtime.py': [Errno 2] No such file",
            failed_step="Install OpenCode CLI for automated repair",
        )
        self.assertEqual(result.failure_class, "control_plane")
        self.assertFalse(result.retryable)

    def test_auth_failure_is_not_retried_as_infrastructure(self):
        result = failure_retry.classify("403 Forbidden: authentication failed", failed_step="Run OpenCode")
        self.assertEqual(result.failure_class, "auth_policy")
        self.assertFalse(result.retryable)


class RetryScheduleTests(unittest.TestCase):
    def test_policy_windows_are_the_agreed_sequence(self):
        self.assertEqual(
            failure_retry.INFRA_RETRY_WINDOWS_MINUTES,
            (1, 2, 4, 8, 16, 32, 60, 60),
        )

    def test_jitter_stays_within_twenty_percent(self):
        for attempt, minutes in enumerate(failure_retry.INFRA_RETRY_WINDOWS_MINUTES, start=1):
            delay = failure_retry.retry_delay_seconds(attempt, "issue-42-run-99")
            base = minutes * 60
            self.assertGreaterEqual(delay, round(base * 0.8) - 1)
            self.assertLessEqual(delay, round(base * 1.2) + 1)

    def test_jitter_is_stable_for_the_same_retry(self):
        first = failure_retry.retry_delay_seconds(4, "same-fingerprint")
        second = failure_retry.retry_delay_seconds(4, "same-fingerprint")
        self.assertEqual(first, second)

    def test_attempt_nine_is_outside_the_infrastructure_budget(self):
        with self.assertRaises(ValueError):
            failure_retry.retry_delay_seconds(9, "x")


if __name__ == "__main__":
    unittest.main()
