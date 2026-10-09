"""Cron-only autonomous dispatch regression tests (kodmial/continuum#280).

NanoDictate issue #156 (open P0, unblocked, eligible since 2026-10-03)
received no automatic reservation or dispatch across many scheduled
reconciler passes and stayed idle until an owner manually posted ``/oc``
on 2026-10-05. The manual command immediately reserved the issue,
proving the consumer issue-comment path was alive while the automatic
reconcile/backstop made zero progress -- and every such pass still
reported success.

These tests pin the executable reconcile contract in
``src/continuum/scheduler_reconcile.py`` (which mirrors the
``continuum-issue-scheduler.yml`` github-script semantics):

- a lone eligible P0 is selected, reserved, and dispatched exactly once
  with no repository event after creation (cron-only recovery);
- a second reconcile is idempotent and does not duplicate dispatch;
- stale reservations expire and failed dispatches roll back and retry
  under the existing bounded/backoff policy;
- WIP starvation names every waiter and holder and fails instead of
  reporting success;
- priority ordering and Blocked-By semantics are preserved.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from continuum.scheduler_reconcile import (
    CommentRecord,
    IssueState,
    SchedulerConfig,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def p0_issue(number: int = 156) -> IssueState:
    return IssueState(
        number=number,
        title="Fix cancellation races and Homebrew quarantine regression",
        body="## Priority\n\nP0 repair task.",
        labels={"priority:p0"},
        is_open=True,
    )


class CronOnlyRecoveryTests(unittest.TestCase):
    """The #156 fixture: no event after creation, cron must recover alone."""

    def test_eligible_p0_selected_reserved_and_dispatched_once(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        first = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )

        self.assertEqual(first.dispatched, [156])
        self.assertEqual(first.reservations_added, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        self.assertFalse(first.failed)
        # Every non-dispatched issue would carry a reason; here nothing waits.
        self.assertEqual(first.skip_reasons, {})

    def test_active_run_suppresses_dispatch_across_repeated_reconciles(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        for offset in (0, 1):
            result = reconcile(
                now + timedelta(minutes=offset),
                issues,
                set(),
                {156},
                comments,
                {},
                {},
                set(),
                config,
            )
            self.assertEqual(result.dispatched, [])
            self.assertIn(config.in_progress_label, issues[156].labels)
            self.assertFalse(result.failed)

        self.assertEqual(comments.get(156, []), [])

    def test_open_pr_suppresses_dispatch_across_repeated_reconciles(self):
        config = SchedulerConfig(count_open_prs_as_wip=False)
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        for offset in (0, 1):
            result = reconcile(
                now + timedelta(minutes=offset),
                issues,
                {156},
                set(),
                comments,
                {},
                {},
                set(),
                config,
            )
            self.assertEqual(result.dispatched, [])
            self.assertIn(config.in_progress_label, issues[156].labels)
            self.assertIn("open implementation PR must not be duplicated", result.skip_reasons[156])
            self.assertFalse(result.failed)

        self.assertEqual(comments.get(156, []), [])

    def test_second_reconcile_is_idempotent(self):
        config = SchedulerConfig()
        issues = {156: p0_issue()}
        comments: dict = {}
        now = utcnow()

        reconcile(now, issues, set(), set(), comments, {}, {}, set(), config)
        second = reconcile(
            now + timedelta(minutes=1),
            issues, set(), set(), comments, {}, {}, set(), config
        )

        self.assertEqual(second.dispatched, [])
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        # The held reservation explains the skip deterministically.
        self.assertIn(156, second.skip_reasons)
        self.assertIn("WIP slot held", second.skip_reasons[156])
        self.assertFalse(second.failed)


class StaleReservationAndRetryTests(unittest.TestCase):
    def test_lease_expiry_releases_and_retries_automatically(self):
        config = SchedulerConfig(lease_minutes=45, max_dispatch_attempts=2)
        now = utcnow()
        issues = {156: p0_issue()}
        issues[156].labels.add(config.in_progress_label)
        comments = {
            156: [
                CommentRecord(
                    body=config.dispatch_marker + "\nAutomatic dispatch.",
                    author_is_owner=True,
                    created_at=now - timedelta(minutes=60),
                )
            ]
        }

        result = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )

        # Stale lease released, then the same pass reselects and redispatches.
        self.assertIn(156, result.reservations_removed)
        self.assertEqual(result.dispatched, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 2)
        self.assertNotIn(config.pause_label, issues[156].labels)

    def test_reservation_without_dispatch_record_is_released_immediately(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        # A manual/stale reservation with no dispatch evidence (e.g. the
        # comment post failed and the label leaked, or a human added it).
        issues[156].labels.add(config.in_progress_label)

        result = reconcile(
            now, issues, set(), set(), {}, {}, {}, set(), config
        )

        self.assertIn(156, result.reservations_removed)
        # Released stale state makes the issue dispatchable again: the same
        # pass releases the stale lease and redispatches (both are logged).
        self.assertEqual(result.dispatched, [156])

    def test_dead_reservation_recovers_then_replay_is_idempotent(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        issues[156].labels.add(config.in_progress_label)
        comments: dict = {}

        recovered = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )
        self.assertIn(156, recovered.reservations_removed)
        self.assertEqual(recovered.dispatched, [156])
        self.assertIn(config.in_progress_label, issues[156].labels)

        replay = reconcile(
            now + timedelta(minutes=1),
            issues,
            set(),
            set(),
            comments,
            {},
            {},
            set(),
            config,
        )
        self.assertEqual(replay.dispatched, [])
        markers = [
            c for c in comments[156] if config.dispatch_marker in c.body
        ]
        self.assertEqual(len(markers), 1)
        self.assertIn("WIP slot held", replay.skip_reasons[156])

    def test_exhausted_attempts_pause_only_when_configured(self):
        now = utcnow()
        old = now - timedelta(minutes=120)

        for pause_on_failure, expect_paused in ((True, True), (False, False)):
            with self.subTest(pause_on_failure=pause_on_failure):
                config = SchedulerConfig(
                    lease_minutes=45,
                    max_dispatch_attempts=2,
                    pause_on_failure=pause_on_failure,
                )
                issues = {156: p0_issue()}
                issues[156].labels.add(config.in_progress_label)
                comments = {
                    156: [
                        CommentRecord(
                            body=config.dispatch_marker, author_is_owner=True,
                            created_at=old,
                        ),
                        CommentRecord(
                            body=config.dispatch_marker, author_is_owner=True,
                            created_at=old + timedelta(minutes=50),
                        ),
                    ]
                }
                result = reconcile(
                    now, issues, set(), set(), comments, {}, {}, set(), config
                )
                if expect_paused:
                    self.assertIn(156, result.paused)
                    self.assertIn(config.pause_label, issues[156].labels)
                    self.assertEqual(result.dispatched, [])
                else:
                    # Autonomous consumers keep retrying without a terminal
                    # human-required pause path for recoverable failures.
                    self.assertNotIn(156, result.paused)
                    self.assertNotIn(config.pause_label, issues[156].labels)
                    self.assertEqual(result.dispatched, [156])

    def test_failed_dispatch_rolls_back_and_cron_retries(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue()}
        comments: dict = {}
        calls: list = []

        def flaky(number: int) -> None:
            calls.append(number)
            if len(calls) == 1:
                raise RuntimeError("comment post failed")

        with self.assertRaises(RuntimeError):
            reconcile(
                now, issues, set(), set(), comments, {}, {}, set(), config,
                dispatch_hook=flaky,
            )
        # The reservation is rolled back so no stale lease strands the issue.
        self.assertNotIn(config.in_progress_label, issues[156].labels)
        self.assertEqual(comments.get(156, []), [])

        # The next scheduled pass retries automatically and dispatches once.
        retry = reconcile(
            now + timedelta(minutes=10),
            issues, set(), set(), comments, {}, {}, set(), config,
            dispatch_hook=flaky,
        )
        self.assertEqual(retry.dispatched, [156])
        self.assertEqual(len(calls), 2)


class WipStarvationVisibilityTests(unittest.TestCase):
    """The #156 stall: 3 lower-priority PRs held 3/2 WIP slots for days."""

    def test_starved_p0_is_explained_and_fails_instead_of_success(self):
        config = SchedulerConfig(wip_limit=2)
        now = utcnow()
        issues = {
            25: IssueState(25, "P2 work one", "", {"priority:p2"}, True),
            35: IssueState(35, "P2 work two", "", {"priority:p2"}, True),
            77: IssueState(77, "P1 work three", "", {"priority:p1"}, True),
            156: p0_issue(),
        }
        open_prs = {25, 35, 77}

        result = reconcile(
            now, issues, open_prs, set(), {}, {}, {}, set(), config
        )

        self.assertEqual(result.dispatched, [])
        # The eligible P0 waiter is named with its priority and the holders.
        self.assertIn(156, result.skip_reasons)
        reason = result.skip_reasons[156]
        self.assertIn("waiting for a free WIP slot", reason)
        self.assertIn("priority:p0", reason)
        for holder in ("#25", "#35", "#77"):
            self.assertIn(holder, reason)
        # Zero progress for eligible backlog must not report success.
        self.assertTrue(result.failed)
        self.assertTrue(result.failed_message)

    def test_full_wip_without_waiters_stays_success(self):
        config = SchedulerConfig(wip_limit=2)
        now = utcnow()
        issues = {
            25: IssueState(25, "P2 work one", "", {"priority:p2"}, True),
            35: IssueState(35, "P2 work two", "", {"priority:p2"}, True),
        }
        result = reconcile(
            now, issues, {25, 35}, set(), {}, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertFalse(result.failed)


class OrderingAndBlockerTests(unittest.TestCase):
    def test_priority_ordering_preserved(self):
        config = SchedulerConfig(wip_limit=1)
        now = utcnow()
        issues = {
            10: IssueState(10, "P2 work", "", {"priority:p2"}, True),
            11: IssueState(11, "P0 work", "", {"priority:p0"}, True),
            12: IssueState(12, "P1 work", "", {"priority:p1"}, True),
        }
        result = reconcile(
            now, issues, set(), set(), {}, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [11])
        self.assertIn("waiting for a free WIP slot", result.skip_reasons[12])

    def test_declared_and_native_blockers_still_gate(self):
        config = SchedulerConfig()
        now = utcnow()
        issues = {156: p0_issue(), 99: p0_issue(99)}
        result = reconcile(
            now, issues, set(), set(), {}, {156: [99]}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [99])
        self.assertIn("declared blocked by #99", result.skip_reasons[156])

        issues = {156: p0_issue()}
        result = reconcile(
            now, issues, set(), set(), {}, {}, {156: [42]}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertIn("blocked by #42", result.skip_reasons[156])
        self.assertFalse(result.failed)

    def test_owner_command_grace_prevents_duplicate_dispatch(self):
        config = SchedulerConfig(command_grace_minutes=5)
        now = utcnow()
        issues = {156: p0_issue()}
        comments = {
            156: [
                CommentRecord(
                    body="/oc please", author_is_owner=True,
                    created_at=now - timedelta(minutes=1),
                )
            ]
        }
        result = reconcile(
            now, issues, set(), set(), comments, {}, {}, set(), config
        )
        self.assertEqual(result.dispatched, [])
        self.assertIn("race window", result.skip_reasons[156])


class WorkflowContractPinTests(unittest.TestCase):
    """The shipped workflow must mirror this contract byte-for-byte in
    semantics: deterministic skip reasons, visible WIP starvation, no
    PR-Agent introduction, no paid provider keys in the core path."""

    def test_scheduler_workflow_explains_every_skip(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        self.assertIn("WIP slot held", body)
        self.assertIn("open implementation PR must not be duplicated", body)
        self.assertIn("waiting for a free WIP slot", body)
        self.assertIn("active local issues", body)

    def test_wip_saturation_keeps_backlog_queued_without_failing(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        self.assertIn("waiting for a free WIP slot", body)
        self.assertIn("WIP is saturated;", body)
        self.assertIn("eligible issue(s) remain queued", body)
        self.assertNotIn(
            "Issue scheduler WIP exhausted with eligible backlog waiting.", body
        )

    def test_no_review_gate_or_credential_regression(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        # CodeRabbit stays the consumer review gate: the local automatic
        # implementation dispatch path must not route through PR-Agent.
        # (Delegated parent-side PR-Agent fan-out for child tasks predates
        # this change under issue #244 and is out of scope here.)
        local_dispatch = body[
            body.index("Re-check mutable state immediately before dispatch."):
            body.index("Wake the configured downstream dispatcher")
        ]
        self.assertNotIn("pr-agent", local_dispatch.lower())
        self.assertIn("opencodeDispatch", local_dispatch)
        for needle in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
            self.assertNotIn(needle, body)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", body)
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", body)


if __name__ == "__main__":
    unittest.main()
