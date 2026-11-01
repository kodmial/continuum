"""Resilient delegated PR-Agent fan-out (kodmial/continuum#289).

One transient ``workflow_dispatch`` failure must not zero the scheduler
pass: each per-child recovery dispatch retries transient GitHub failures
with bounded backoff, deterministic failures fail closed without retry,
and the fan-out continues over unrelated children with one aggregate
diagnostic at the end.
"""

import unittest
from pathlib import Path

from continuum.delegated_dispatch import (
    MIN_RETRY_DELAY_SECONDS,
    MAX_DISPATCH_ATTEMPTS,
    classify_dispatch_failure,
    is_transient_dispatch_failure,
    retry_delays,
    run_with_retry,
)

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class ClassifierTests(unittest.TestCase):
    def test_http_500_is_transient(self):
        self.assertEqual(
            classify_dispatch_failure("gh: HTTP 500: Internal Server Error"),
            "transient",
        )
        self.assertTrue(
            is_transient_dispatch_failure("Failed with HTTP 500 from the API")
        )

    def test_http_5xx_family_is_transient(self):
        for status in ("502", "503", "504"):
            with self.subTest(status=status):
                self.assertEqual(
                    classify_dispatch_failure(f"gh: HTTP {status}: Bad Gateway"),
                    "transient",
                )

    def test_http_429_is_transient(self):
        self.assertEqual(
            classify_dispatch_failure("gh: HTTP 429: rate limited"),
            "transient",
        )

    def test_rate_limit_403_is_transient(self):
        self.assertEqual(
            classify_dispatch_failure(
                "gh: HTTP 403: API rate limit exceeded, try again later"
            ),
            "transient",
        )
        self.assertEqual(
            classify_dispatch_failure(
                "HTTP 403 secondary rate limit, please retry"
            ),
            "transient",
        )

    def test_plain_403_is_deterministic(self):
        self.assertEqual(
            classify_dispatch_failure("gh: HTTP 403: Forbidden"),
            "deterministic",
        )
        self.assertFalse(
            is_transient_dispatch_failure("HTTP 403 Resource not accessible")
        )

    def test_deterministic_failures_fail_closed(self):
        for output in (
            "gh: HTTP 401: Bad credentials",
            "gh: HTTP 404: workflow not found",
            "gh: HTTP 422: validation failed, target_child_id is unknown",
            "",
            "gh: HTTP 400: Bad request",
        ):
            with self.subTest(output=output):
                self.assertEqual(
                    classify_dispatch_failure(output), "deterministic"
                )


class RetryScheduleTests(unittest.TestCase):
    def test_delays_are_bounded_with_minimum_5s(self):
        delays = retry_delays()
        self.assertEqual(len(delays), MAX_DISPATCH_ATTEMPTS - 1)
        for delay in delays:
            self.assertGreaterEqual(delay, MIN_RETRY_DELAY_SECONDS)
        self.assertGreaterEqual(MIN_RETRY_DELAY_SECONDS, 5.0)

    def test_no_tight_loop_without_sleep(self):
        sleeps: list[float] = []

        def always_transient():
            return (False, "HTTP 500: Internal Server Error")

        outcome = run_with_retry(
            always_transient, sleep=sleeps.append
        )
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.attempts, MAX_DISPATCH_ATTEMPTS)
        self.assertEqual(len(sleeps), MAX_DISPATCH_ATTEMPTS - 1)
        for delay in sleeps:
            self.assertGreaterEqual(delay, 5.0)


class RetryBehaviorTests(unittest.TestCase):
    def test_transient_success_after_retry(self):
        calls = {"n": 0}
        sleeps: list[float] = []

        def dispatch():
            calls["n"] += 1
            if calls["n"] == 1:
                return (False, "HTTP 500: Internal Server Error")
            return (True, "")

        outcome = run_with_retry(dispatch, sleep=sleeps.append)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 5.0)

    def test_deterministic_failure_does_not_retry(self):
        calls = {"n": 0}
        sleeps: list[float] = []

        def dispatch():
            calls["n"] += 1
            return (False, "HTTP 404: workflow not found")

        outcome = run_with_retry(dispatch, sleep=sleeps.append)
        self.assertFalse(outcome.succeeded)
        self.assertTrue(outcome.deterministic)
        self.assertEqual(outcome.attempts, 1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleeps, [])

    def test_one_transient_child_does_not_stop_later_children(self):
        # One child persistently returns 500 while later children dispatch
        # cleanly: the fan-out must still reach every later child and report
        # one aggregate failure at the end.
        dispatched: list[str] = []
        failures = 0

        behaviors = {
            "child-a": ["HTTP 500: Internal Server Error"] * 10,
            "child-b": ["ok"],
            "child-c": ["ok"],
        }

        for child_id in ("child-a", "child-b", "child-c"):
            queue = list(behaviors[child_id])

            def dispatch(queue=queue):
                output = queue.pop(0) if queue else "ok"
                return (output == "ok", "" if output == "ok" else output)

            outcome = run_with_retry(dispatch, sleep=lambda _: None)
            if outcome.succeeded:
                dispatched.append(child_id)
            else:
                failures += 1

        self.assertEqual(dispatched, ["child-b", "child-c"])
        self.assertEqual(failures, 1)


class SchedulerFanoutContractTests(unittest.TestCase):
    def test_scheduler_retries_transient_dispatch_with_backoff(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn("dispatch_pr_agent_recovery_with_retry", body)
        self.assertIn("pr_agent_dispatch_is_transient", body)
        self.assertIn("max_attempts=3", body)
        self.assertIn('sleep "$delay"', body)
        # Minimum 5s retry delay, never a tight loop.
        self.assertIn("local attempt=1 max_attempts=3 delay=5", body)

    def test_scheduler_continues_fanout_with_aggregate_diagnostic(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn("pr_agent_dispatch_failures", body)
        self.assertIn("fan-out continues", body)
        self.assertIn(
            "remaining children were still processed", body
        )
        # The per-child dispatch no longer aborts under set -e: it is
        # guarded by an if-condition and the step keeps its outputs.
        self.assertIn(
            'if dispatch_pr_agent_recovery_with_retry "$child_id"; then',
            body,
        )

    def test_scheduler_counts_only_successful_dispatches(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        start = body.index("Parent-side PR-Agent fan-out")
        window = body[start : start + 4000]
        self.assertIn("review_dispatches=$((review_dispatches + 1))", window)
        # Success counting lives on the success branch only.
        success_branch = window.split(
            'if dispatch_pr_agent_recovery_with_retry "$child_id"; then'
        )[1]
        self.assertIn(
            "review_dispatches=$((review_dispatches + 1))",
            success_branch.split("else")[0],
        )

    def test_scheduler_fanout_preserves_opaque_id_and_privacy(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn('-f target_child_id="$child_id"', body)
        start = body.index("Parent-side PR-Agent fan-out")
        window = body[start : start + 4000]
        # Diagnostics carry counts only, never resolved private names/URLs.
        self.assertNotIn("child_repo", window.replace("$child_repo", ""))
        self.assertNotIn("http", window.lower().replace("http 500", ""))

    def test_deterministic_failures_are_not_retried_as_transient(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn("return 2", body)
        self.assertIn("pr_agent_deterministic_failures", body)
        self.assertIn(
            "failed deterministically for one verified child", body
        )


if __name__ == "__main__":
    unittest.main()
