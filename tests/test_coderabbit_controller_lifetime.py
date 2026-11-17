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

    def test_caller_rejects_unrelated_comments_before_queue(self):
        """The caller must reject telemetry before reusable-workflow concurrency."""
        same_as_engine = [
            "github.event.issue.pull_request != null",
            "github.event.comment.user.login == 'coderabbitai[bot]'",
            "contains(github.event.comment.body, 'Review rate limited')",
            "contains(github.event.comment.body, 'Review limit reached')",
        ]
        engine = WORKFLOW.read_text(encoding="utf-8")
        for entry in (
            ROOT / ".github/workflows/coderabbit-retry.yml",
            ROOT / ".github/caller-stubs/continuum-coderabbit-retry.yml",
        ):
            with self.subTest(entry=entry.name):
                text = entry.read_text(encoding="utf-8")
                # Not a post-admission filter in reusable jobs.controller.
                caller_if = text.split("jobs:\n  call:\n", 1)[1].split(
                    "    uses:", 1
                )[0]
                self.assertIn("github.event_name != 'issue_comment'", caller_if)
                for predicate in same_as_engine:
                    self.assertIn(predicate, caller_if)
                    self.assertIn(predicate, engine)
                self.assertIn("github.event_name == 'schedule'", engine)
                self.assertIn("github.event_name == 'workflow_dispatch'", engine)
                # The ordinary manual and scheduled events remain enabled by
                # the short-circuit, and the shared quota serializer is intact.
                self.assertNotIn("cancel-in-progress: true", text)


    def test_real_coderabbit_review_events_only_enter_shared_queue(self):
        """Both callers must reject non-provider reviews before queue admission."""
        for path in (
            ROOT / ".github/workflows/coderabbit-retry.yml",
            ROOT / ".github/caller-stubs/continuum-coderabbit-retry.yml",
        ):
            with self.subTest(path=path.name):
                body = path.read_text(encoding="utf-8")
                guard = body.split("jobs:\\n  call:\\n", 1)[1].split("    uses:", 1)[0]
                for fragment in (
                    "github.event_name != 'pull_request_review'",
                    "github.event.review.user.login == 'coderabbitai[bot]'",
                    "github.event_name != 'pull_request_review_comment'",
                    "startsWith(github.event.comment.user.login, 'coderabbitai')",
                    "github.event.pull_request.state == 'open'",
                ):
                    self.assertIn(fragment, guard)

    def test_auto_merge_code_rabbit_wake_is_single_owner(self):
        body = (ROOT / ".github/workflows/continuum-auto-merge.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            body.count(
                "owner, repo, workflow_id: codeRabbitRetryWorkflow, ref: 'main'"
            ), 1,
        )
        self.assertIn("async function wakeCodeRabbitRetryIfIdle(pr, reason)", body)
        self.assertIn("client.rest.actions.listWorkflowRuns({", body)
        self.assertIn("status, per_page: 1,", body)
        self.assertIn("queue state unreadable; deferring", body)
        self.assertEqual(body.count("await wakeCodeRabbitRetryIfIdle(pr,"), 2)

    def test_serialized_review_queue_unchanged(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: coderabbit-rate-limit-controller-", body)
        self.assertIn("cancel-in-progress: false", body)
        self.assertIn("const REVIEW_COOLDOWN_MS = 60 * 60_000;", body)
        self.assertIn("const IN_FLIGHT_TIMEOUT_MS = 30 * 60_000;", body)
        self.assertIn("core.notice('No CodeRabbit review is currently queued.')", body)


if __name__ == "__main__":
    unittest.main()
