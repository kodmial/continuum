"""Deterministic tests for the review queue reconciliation contract.

The reference incident: the pull request at the head of the review queue was
closed on purpose, no review-related event fired, and the next eligible pull
request waited for an unrelated event. These tests pin the generic contract
that makes that class of stall impossible.
"""

from __future__ import annotations

import json
import unittest

from continuum import config as config_module
from continuum.review import providers as providers_module
from continuum.review import queue
from continuum.review import queue_controller as controller
from continuum.review.github import GitHubError
from tests import support

NOW = 1_789_000_000_000  # 2026-09-10T00:00:00Z-ish, fixed for determinism


def iso(ms: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def candidate(number: int, **overrides) -> queue.Candidate:
    fields = {
        "pr_number": number,
        "head_sha": f"{number:040d}",
        "state": queue.STATE_OPEN,
        "labels": ("review-ready",),
        "priority": "priority:p0",
        "ci": queue.CI_SUCCESS,
        "review": queue.REVIEW_UNREVIEWED,
    }
    fields.update(overrides)
    return queue.Candidate(**fields)


def policy(**overrides) -> queue.QueuePolicy:
    fields = {
        "provider": config_module.PROVIDER_CODERABBIT,
        "cooldown_ms": 0,
        "safety_margin_ms": 0,
        "in_flight_timeout_ms": 30 * 60_000,
    }
    fields.update(overrides)
    return queue.QueuePolicy(**fields)


def state(*candidates, wake=None, cooldown=None, now=NOW) -> queue.QueueState:
    return queue.QueueState(
        wake=wake or queue.WakeUp(event="pull_request", action="closed"),
        candidates=tuple(candidates),
        cooldown=cooldown or queue.Cooldown(),
        now_ms=now,
    )


def logs(plan: queue.Plan) -> str:
    return "\n".join(plan.logs)


class WakeUpCoverageTests(unittest.TestCase):
    def test_every_pull_request_action_that_can_change_the_queue_wakes_the_controller(self):
        for action in queue.PULL_REQUEST_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(queue.is_queue_wake_up("pull_request", action))

    def test_the_closure_transitions_are_covered(self):
        # These are the two actions whose absence stalls a queue.
        self.assertIn("closed", queue.PULL_REQUEST_ACTIONS)
        self.assertIn("converted_to_draft", queue.PULL_REQUEST_ACTIONS)
        self.assertIn("ready_for_review", queue.PULL_REQUEST_ACTIONS)
        self.assertIn("synchronize", queue.PULL_REQUEST_ACTIONS)
        self.assertIn("labeled", queue.PULL_REQUEST_ACTIONS)
        self.assertIn("unlabeled", queue.PULL_REQUEST_ACTIONS)

    def test_provider_and_ci_signals_wake_the_controller(self):
        for event in (
            queue.EVENT_PULL_REQUEST_REVIEW,
            queue.EVENT_ISSUE_COMMENT,
            queue.EVENT_STATUS,
            queue.EVENT_CHECK_RUN,
            queue.EVENT_WORKFLOW_RUN,
            queue.EVENT_ISSUES,
            queue.EVENT_SCHEDULE,
            queue.EVENT_WORKFLOW_DISPATCH,
        ):
            with self.subTest(event=event):
                self.assertTrue(queue.is_queue_wake_up(event))

    def test_the_trusted_context_trigger_is_the_pull_request_event(self):
        # The workflow subscribes to `pull_request_target` so no candidate code
        # is ever checked out. If that name were not a pull request wake-up, the
        # `closed` and `converted_to_draft` transitions would be dropped as
        # irrelevant -- exactly the stall this queue exists to remove.
        for action in queue.PULL_REQUEST_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(queue.is_queue_wake_up("pull_request_target", action))
        self.assertEqual(
            queue.normalise_event("pull_request_target"), queue.EVENT_PULL_REQUEST
        )
        self.assertEqual(
            queue.WakeUp(event="pull_request_target", action="closed").describe(),
            "pull_request closed",
        )

    def test_unrelated_events_are_not_wake_ups(self):
        for event in queue.NON_QUEUE_EVENTS:
            with self.subTest(event=event):
                self.assertFalse(queue.is_queue_wake_up(event))


class QueueLivenessTests(unittest.TestCase):
    """Scenarios 1-3 and 5: a candidate that leaves must not stall the queue."""

    def test_first_candidate_closes_so_second_is_selected(self):
        plan = queue.reconcile(
            state(
                candidate(58, state=queue.STATE_CLOSED),
                candidate(57, source_issue=57),
                wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: state=closed.", logs(plan))
        self.assertIn("Next eligible candidate PR #57 is ready.", logs(plan))
        self.assertIn("Dispatching one review request.", logs(plan))

    def test_first_candidate_merges_so_second_is_selected(self):
        plan = queue.reconcile(
            state(
                candidate(58, state=queue.STATE_MERGED),
                candidate(57),
                wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: state=merged.", logs(plan))

    def test_first_candidate_becomes_draft_so_second_is_selected(self):
        plan = queue.reconcile(
            state(
                candidate(58, draft=True),
                candidate(57),
                wake=queue.WakeUp(
                    event="pull_request", action="converted_to_draft", pr_number=58
                ),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: state=draft.", logs(plan))

    def test_pause_label_removes_a_candidate(self):
        plan = queue.reconcile(
            state(
                candidate(58, labels=("review-ready", "review-paused")),
                candidate(57),
                wake=queue.WakeUp(event="pull_request", action="labeled", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: blocked:review-paused.", logs(plan))

    def test_block_label_takes_a_candidate_out(self):
        plan = queue.reconcile(
            state(
                candidate(58, labels=("review-ready", "review-blocked")),
                candidate(57),
                wake=queue.WakeUp(event="pull_request", action="labeled", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.selected.pr_number, 57)

    def test_ineligible_untouched_candidates_are_not_reported_as_transitions(self):
        plan = queue.reconcile(
            state(candidate(12, draft=True), candidate(57)),
            policy(),
        )
        self.assertNotIn("PR #12", logs(plan))


class RankingTests(unittest.TestCase):
    def test_priority_label_change_reorders_the_queue(self):
        ranked = [candidate(57, priority="priority:p1"), candidate(59, priority="priority:p0")]
        first = queue.reconcile(state(*ranked), policy())
        self.assertEqual(first.selected.pr_number, 59)

        ranked[0] = candidate(57, priority="priority:p0")
        second = queue.reconcile(state(*ranked), policy())
        self.assertEqual(second.selected.pr_number, 57)

    def test_priority_beats_issue_number_and_pull_request_number(self):
        plan = queue.reconcile(
            state(
                candidate(57, priority="priority:p2", source_issue=1),
                candidate(59, priority="priority:p0", source_issue=99),
            ),
            policy(),
        )
        self.assertEqual(plan.selected.pr_number, 59)

    def test_unprioritized_is_last(self):
        plan = queue.reconcile(
            state(
                candidate(57, priority=queue.UNPRIORITIZED, source_issue=1),
                candidate(59, priority="priority:p2", source_issue=2),
            ),
            policy(),
        )
        self.assertEqual(plan.selected.pr_number, 59)

    def test_tie_breakers_are_configurable_and_deterministic(self):
        items = [candidate(59, priority="priority:p1"), candidate(57, priority="priority:p1")]
        by_number = queue.reconcile(state(*items), policy(tie_breakers=("pr_number",)))
        by_issue = queue.reconcile(
            state(
                candidate(59, priority="priority:p1", source_issue=2),
                candidate(57, priority="priority:p1", source_issue=1),
            ),
            policy(tie_breakers=("source_issue",)),
        )
        self.assertEqual(by_number.selected.pr_number, 57)
        self.assertEqual(by_issue.selected.pr_number, 57)
        # Reordering the input must not change the outcome.
        reversed_first = queue.reconcile(state(*reversed(items)), policy())
        self.assertEqual(reversed_first.selected, by_number.selected)

    def test_queue_order_is_stable_across_runs(self):
        items = [candidate(59), candidate(57), candidate(46)]
        first = queue.reconcile(state(*items), policy())
        second = queue.reconcile(state(*items), policy())
        self.assertEqual(
            [item.pr_number for item in first.queue],
            [item.pr_number for item in second.queue],
        )


class LockTests(unittest.TestCase):
    """Scenarios 6 and 7: in-flight ownership is validated, not trusted."""

    def test_stale_lock_on_a_closed_pull_request_is_released_immediately(self):
        lock = queue.ReviewRequest(
            pr_number=58, head_sha="a" * 40, requested_at_ms=NOW - 60_000
        )
        plan = queue.reconcile(
            state(
                candidate(58, state=queue.STATE_CLOSED, request=lock),
                candidate(57),
                wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertEqual(plan.released_lock, lock)
        self.assertIn("Released stale review slot for PR #58 (state=closed).", logs(plan))
        # Released long before the 30-minute in-flight timeout.
        self.assertLess(lock.age_ms(NOW), 30 * 60_000)

    def test_stale_lock_on_a_draft_pull_request_is_released(self):
        lock = queue.ReviewRequest(
            pr_number=58, head_sha="a" * 40, requested_at_ms=NOW - 60_000
        )
        plan = queue.reconcile(
            state(candidate(58, draft=True, request=lock), candidate(57)), policy()
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("Released stale review slot for PR #58 (state=draft).", logs(plan))

    def test_live_lock_holds_the_slot_without_a_second_request(self):
        lock = queue.ReviewRequest(
            pr_number=57, head_sha="a" * 40, requested_at_ms=NOW - 60_000
        )
        plan = queue.reconcile(
            state(candidate(57, head_sha="a" * 40, request=lock), candidate(59)), policy()
        )
        self.assertEqual(plan.action, queue.ACTION_WAIT)
        self.assertFalse(plan.dispatched)
        self.assertIsNone(plan.selected)
        self.assertIn("Review slot is in flight for PR #57#aaaaaaa", logs(plan))

    def test_lock_is_released_when_the_candidate_moved_to_a_new_head(self):
        lock = queue.ReviewRequest(
            pr_number=57, head_sha="a" * 40, requested_at_ms=NOW - 60_000
        )
        plan = queue.reconcile(
            state(candidate(57, head_sha="b" * 40, request=lock), candidate(59)), policy()
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        # The new HEAD still needs a review and outranks the rest of the queue.
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("Released stale review slot for PR #57 (head moved to bbbbbbb).", logs(plan))

    def test_expired_lock_on_a_live_candidate_is_released_after_the_timeout(self):
        lock = queue.ReviewRequest(
            pr_number=57, head_sha="a" * 40, requested_at_ms=NOW - 31 * 60_000
        )
        plan = queue.reconcile(
            state(candidate(57, head_sha="a" * 40, request=lock), candidate(59)),
            policy(in_flight_timeout_ms=30 * 60_000),
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertIn("no provider response for 31m", logs(plan))

    def test_settled_request_does_not_hold_the_slot(self):
        request = queue.ReviewRequest(
            pr_number=57,
            head_sha="a" * 40,
            requested_at_ms=NOW - 60_000,
            settled=True,
            settled_reason="review APPROVED",
        )
        plan = queue.reconcile(state(candidate(57, request=request)), policy())
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)


class CooldownTests(unittest.TestCase):
    def test_cooldown_is_respected_after_a_candidate_is_replaced(self):
        cooldown = queue.Cooldown(
            until_ms=NOW + 18 * 60_000 + 42_000, reason="provider rate limit"
        )
        plan = queue.reconcile(
            state(
                candidate(58, state=queue.STATE_CLOSED),
                candidate(57),
                cooldown=cooldown,
                wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_WAIT)
        self.assertFalse(plan.dispatched)
        self.assertIn("PR #58 left queue: state=closed.", logs(plan))
        self.assertIn("Next eligible review candidate: PR #57 (priority:p0).", logs(plan))
        self.assertIn("Review provider cooldown still active for 18m 42s.", logs(plan))

    def test_cooldown_does_not_reorder_the_queue(self):
        cooldown = queue.Cooldown(until_ms=NOW + 60_000, reason="shared quota")
        plan = queue.reconcile(
            state(
                candidate(57, priority="priority:p2", due_at_ms=NOW + 120_000),
                candidate(59, priority="priority:p0"),
                cooldown=cooldown,
            ),
            policy(),
        )
        self.assertEqual(plan.queue[0].pr_number, 59)
        self.assertEqual(plan.action, queue.ACTION_WAIT)

    def test_candidate_own_retry_due_time_is_reported_separately(self):
        plan = queue.reconcile(
            state(
                candidate(57, review=queue.REVIEW_RETRY, due_at_ms=NOW + 5 * 60_000),
                candidate(59),
            ),
            policy(),
        )
        self.assertEqual(plan.action, queue.ACTION_WAIT)
        self.assertIn("PR #57 is not due for 5m (retry review).", logs(plan))

    def test_duration_formatting(self):
        self.assertEqual(queue.format_duration(18 * 60_000 + 42_000), "18m 42s")
        self.assertEqual(queue.format_duration(42_000), "42s")
        self.assertEqual(queue.format_duration(65 * 60_000), "1h 5m")
        self.assertEqual(queue.format_duration(0), "0s")


class EligibilityTests(unittest.TestCase):
    def test_missing_ready_label_is_not_a_candidate(self):
        reason = queue.ineligibility_reason(
            candidate(57, labels=("bug",)), policy()
        )
        self.assertEqual(reason, "missing:review-ready")

    def test_ready_label_can_be_disabled(self):
        reason = queue.ineligibility_reason(
            candidate(57, labels=("bug",)), policy(ready_label=None)
        )
        self.assertIsNone(reason)

    def test_failing_ci_is_not_a_candidate(self):
        self.assertEqual(
            queue.ineligibility_reason(candidate(57, ci=queue.CI_FAILURE), policy()),
            "ci=failure",
        )
        self.assertEqual(
            queue.ineligibility_reason(candidate(57, ci=queue.CI_CANCELLED), policy()),
            "ci=cancelled",
        )
        self.assertEqual(
            queue.ineligibility_reason(candidate(57, ci=queue.CI_PENDING), policy()),
            "ci=pending",
        )

    def test_already_reviewed_head_is_not_a_candidate(self):
        self.assertEqual(
            queue.ineligibility_reason(candidate(57, review=queue.REVIEW_CURRENT), policy()),
            "review=current",
        )

    def test_closed_draft_reports_the_lifecycle_reason(self):
        self.assertEqual(
            queue.ineligibility_reason(
                candidate(57, state=queue.STATE_CLOSED, draft=True), policy()
            ),
            "state=closed",
        )

    def test_disabled_provider_dispatches_nothing(self):
        plan = queue.reconcile(
            state(candidate(57), candidate(59)), policy(provider="none")
        )
        self.assertEqual(plan.action, queue.ACTION_DISABLED)
        self.assertFalse(plan.dispatched)
        self.assertIn("review.provider is none", logs(plan))

    def test_empty_queue_is_idle(self):
        plan = queue.reconcile(state(), policy())
        self.assertEqual(plan.action, queue.ACTION_IDLE)
        self.assertIn("No eligible review candidate in the queue.", logs(plan))


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_wake_ups_produce_the_same_plan(self):
        items = [candidate(57), candidate(59)]
        plans = [
            queue.reconcile(
                state(*items, wake=wake),
                policy(),
            )
            for wake in (
                queue.WakeUp(event="pull_request", action="closed", pr_number=58),
                queue.WakeUp(event="pull_request_review", action="submitted"),
                queue.WakeUp(event="status"),
                queue.WakeUp(event="schedule"),
            )
        ]
        self.assertEqual({plan.action for plan in plans}, {queue.ACTION_DISPATCH})
        self.assertEqual({plan.selected for plan in plans}, {plans[0].selected})
        self.assertEqual(
            {json.dumps(plan.describe()["selected"], sort_keys=True) for plan in plans},
            {json.dumps(plans[0].describe()["selected"], sort_keys=True)},
        )

    def test_plan_document_is_json_serializable_and_stable(self):
        plan = queue.reconcile(state(candidate(57), candidate(58)), policy())
        first = plan.describe()
        second = queue.reconcile(state(candidate(57), candidate(58)), policy()).describe()
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "continuum.review-queue/v1")


class ControllerTests(unittest.TestCase):
    """The same contract, driven through the GitHub-facing controller."""

    def _repository(self, **kwargs) -> support.FakeQueueGitHub:
        return support.FakeQueueGitHub(**kwargs)

    def _ready_pull(self, number: int, priority: str = "priority:p1", **overrides):
        """An eligible pull request whose priority is explicit, never implicit."""

        return support.pull(
            number,
            labels=("review-ready", priority),
            source_issue=number,
            **overrides,
        )

    def _green(self, client: support.FakeQueueGitHub, *numbers: int) -> None:
        for number in numbers:
            client.check_runs[f"{number:040d}"] = [support.check_run()]

    def test_closing_the_queue_head_dispatches_the_next_candidate(self):
        client = self._repository(
            pulls=[
                self._ready_pull(58),
                self._ready_pull(57),
            ]
        )
        self._green(client, 57, 58)
        client.close_pull(58)

        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: state=closed.", logs(plan))
        self.assertEqual(
            client.provider_commands(), [providers_module.coderabbit.FULL_REVIEW_COMMAND]
        )
        self.assertEqual(client.lock_records()[0]["pr"], 57)

    def test_duplicate_wake_ups_send_exactly_one_provider_command(self):
        client = self._repository(pulls=[self._ready_pull(57), self._ready_pull(59)])
        self._green(client, 57, 59)

        wake_ups = [
            queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            queue.WakeUp(event="pull_request_review", action="submitted"),
            queue.WakeUp(event="status"),
            queue.WakeUp(event="schedule"),
        ]
        plans = [
            controller.reconcile_once(
                client, support.queue_config(), wake=wake, now=NOW
            )
            for wake in wake_ups
        ]
        self.assertEqual(sum(1 for plan in plans if plan.dispatched), 1)
        self.assertEqual(len(client.provider_commands()), 1)
        self.assertEqual(len(client.lock_records()), 1)
        # Every later wake-up is a no-op that names the held slot.
        self.assertEqual(plans[-1].action, queue.ACTION_WAIT)
        self.assertIn("no second review request will be sent", logs(plans[-1]))

    def test_a_restarted_controller_recovers_the_same_candidate_from_state(self):
        client = self._repository(pulls=[self._ready_pull(57), self._ready_pull(59)])
        self._green(client, 57, 59)

        first = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(first.selected.pr_number, 57)

        # A brand new controller with no memory of the first run.
        second = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(second.action, queue.ACTION_WAIT)
        self.assertEqual(second.held_lock.pr_number, 57)
        self.assertEqual(len(client.provider_commands()), 1)

    def test_no_apply_decides_without_writing_anything(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)
        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="schedule"),
            now=NOW,
            apply=False,
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(client.provider_commands(), [])
        self.assertEqual(client.lock_records(), [])

    def test_a_pull_request_that_closes_between_read_and_write_gets_no_command(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)

        original_get_pull = client.get_pull

        def closing_get_pull(number: int):
            if int(number) == 57 and not client.calls.count(("get_pull", 57)):
                client.close_pull(57)
            return original_get_pull(number)

        client.get_pull = closing_get_pull  # type: ignore[method-assign]
        plan = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertFalse(plan.dispatched)
        self.assertEqual(client.provider_commands(), [])
        self.assertIn("left queue: state=closed", logs(plan))

    def test_a_failed_provider_command_releases_the_reserved_slot(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)

        def failing_comment(issue_number: int, body: str):
            from continuum.review.github import GitHubError

            if controller.REQUEST_MARKER not in body:
                raise GitHubError("comment rejected", status=403)
            return support.FakeQueueGitHub.create_issue_comment(client, issue_number, body)

        client.create_issue_comment = failing_comment  # type: ignore[method-assign]
        with self.assertRaises(GitHubError):
            controller.reconcile_once(
                client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
            )
        self.assertEqual(len(client.lock_records()), 0)
        self.assertTrue(client.deleted_comments)

    def test_provider_none_never_contacts_the_provider(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)
        plan = controller.reconcile_once(
            client,
            support.queue_config(provider=config_module.PROVIDER_NONE),
            wake=queue.WakeUp(event="schedule"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_DISABLED)
        self.assertEqual(client.provider_commands(), [])
        self.assertEqual([name for name, *_ in client.calls], [])

    def test_pr_agent_provider_dispatches_the_gate_workflow_instead_of_a_comment(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)
        plan = controller.reconcile_once(
            client,
            support.queue_config(provider=config_module.PROVIDER_PR_AGENT),
            wake=queue.WakeUp(event="schedule"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(client.provider_commands(), [])
        self.assertEqual(len(client.workflow_dispatches), 1)
        self.assertEqual(client.workflow_dispatches[0]["inputs"]["pr_number"], "57")
        self.assertEqual(client.workflow_dispatches[0]["inputs"]["head_sha"], f"{57:040d}")

    def test_fork_pull_requests_never_occupy_the_shared_slot(self):
        client = self._repository(
            pulls=[self._ready_pull(57, fork=True), self._ready_pull(59)]
        )
        self._green(client, 59)
        plan = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(plan.selected.pr_number, 59)

    def test_a_scheduled_run_recovers_a_missed_event(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        # CI finished and the label appeared, but no event ever reached the
        # controller: only the low-frequency schedule notices.
        client.check_runs[f"{57:040d}"] = []
        client.set_labels(57, ("review-ready",))  # CI has not reported yet

        idle = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(idle.action, queue.ACTION_IDLE)
        self.assertIn("ci=unknown", logs(idle))

        self._green(client, 57)
        recovered = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(recovered.action, queue.ACTION_DISPATCH)
        self.assertEqual(recovered.selected.pr_number, 57)

    def test_the_provider_cooldown_survives_a_candidate_replacement(self):
        client = self._repository(
            pulls=[self._ready_pull(57), self._ready_pull(59)],
            comments={
                57: [
                    support.issue_comment(
                        "Review rate limited. Your next included review will be available "
                        "in 18 minutes.",
                        login="coderabbitai[bot]",
                        created_at=iso(NOW - 42_000),
                        updated_at=iso(NOW - 42_000),
                    )
                ]
            },
        )
        self._green(client, 57, 59)

        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="issue_comment", action="created"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_WAIT)
        self.assertIn("Review provider cooldown still active for 17m 18s.", logs(plan))
        self.assertEqual(client.provider_commands(), [])

    def test_a_rate_limited_candidate_is_retried_after_the_published_window(self):
        client = self._repository(
            pulls=[self._ready_pull(57)],
            comments={
                57: [
                    support.issue_comment(
                        "Review rate limited. Try again in less than a minute.",
                        login="coderabbitai[bot]",
                        created_at=iso(NOW - 30_000),
                        updated_at=iso(NOW - 30_000),
                    )
                ]
            },
        )
        self._green(client, 57)
        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="issue_comment", action="created"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_WAIT)
        self.assertIn("Review provider cooldown still active for 30s.", logs(plan))

        later = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="schedule"),
            now=NOW + 90_000,
        )
        self.assertEqual(later.action, queue.ACTION_DISPATCH)
        self.assertEqual(later.selected.pr_number, 57)

    def test_an_unanswered_request_holds_the_slot_until_it_expires(self):
        client = self._repository(pulls=[self._ready_pull(57), self._ready_pull(59)])
        self._green(client, 57, 59)

        controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(len(client.provider_commands()), 1)

        held = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(held.action, queue.ACTION_WAIT)

        expired = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="schedule"),
            now=NOW + 31 * 60_000,
        )
        self.assertEqual(expired.action, queue.ACTION_DISPATCH)
        # The unanswered candidate still needs a review, and it outranks the rest.
        self.assertEqual(expired.selected.pr_number, 57)
        self.assertIn("Released stale review slot for PR #57", logs(expired))

    def test_a_provider_review_settles_the_slot_and_starts_the_cooldown(self):
        client = self._repository(pulls=[self._ready_pull(57), self._ready_pull(59)])
        self._green(client, 57, 59)

        controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        client.reviews[57] = [
            {
                "id": 1,
                "state": "CHANGES_REQUESTED",
                "body": "Please fix this.",
                "user": {"login": "coderabbitai[bot]"},
                "commit_id": f"{57:040d}",
                "submitted_at": iso(NOW + 60_000),
            }
        ]

        after = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="pull_request_review", action="submitted"),
            now=NOW + 60_000,
        )
        self.assertEqual(after.action, queue.ACTION_WAIT)
        self.assertIn("Review provider cooldown still active for 1h 0m 30s.", logs(after))
        self.assertEqual(len(client.provider_commands()), 1)

    def _cleared_head(self, client, number: int) -> None:
        """Give a pull request a durable exact-head approval plus a later skip."""

        client.reviews[number] = [
            {
                "id": 1,
                "state": "APPROVED",
                "body": "",
                "user": {"login": "coderabbitai[bot]"},
                "commit_id": f"{number:040d}",
                "submitted_at": iso(NOW - 120_000),
            }
        ]
        client.statuses[f"{number:040d}"] = [
            {
                "id": 3,
                "context": "CodeRabbit",
                "state": "success",
                "description": "Review skipped",
                "sha": f"{number:040d}",
                "created_at": iso(NOW - 60_000),
                "updated_at": iso(NOW - 60_000),
            }
        ]

    def test_a_cleared_head_under_a_skipped_status_is_never_re_requested(self):
        # CodeRabbit answers a second request about an already-cleared head with
        # `Review skipped` instead of reviewing it again. Treating that as
        # "still owed a full review" burned the shared included-review quota on
        # every wake-up of a repaired pull request.
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)
        self._cleared_head(client, 57)

        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="status"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_IDLE)
        self.assertEqual(client.provider_commands(), [])
        self.assertEqual(client.lock_records(), [])

    def test_a_cleared_head_does_not_consume_the_slot_for_an_untouched_head(self):
        client = self._repository(pulls=[self._ready_pull(57), self._ready_pull(59)])
        self._green(client, 57, 59)
        self._cleared_head(client, 57)

        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="status"),
            now=NOW,
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 59)
        self.assertEqual([record["pr"] for record in client.lock_records()], [59])
        self.assertEqual(client.provider_commands(), ["@coderabbitai full review"])

    def test_a_human_review_does_not_settle_the_shared_slot(self):
        client = self._repository(pulls=[self._ready_pull(57)])
        self._green(client, 57)
        client.reviews[57] = [
            {
                "id": 1,
                "state": "APPROVED",
                "body": "LGTM",
                "user": {"login": "some-human"},
                "commit_id": f"{57:040d}",
                "submitted_at": iso(NOW - 60_000),
            }
        ]

        plan = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertEqual(client.provider_commands(), ["@coderabbitai full review"])

    def test_an_ineligible_in_flight_owner_is_released_and_the_next_candidate_runs(self):
        client = self._repository(
            pulls=[self._ready_pull(58, "priority:p0"), self._ready_pull(57)]
        )
        self._green(client, 57, 58)

        controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(client.lock_records()[0]["pr"], 58)
        self.assertEqual(len(client.provider_commands()), 1)

        # The queue head is closed on purpose; nothing else happens.
        client.close_pull(58)
        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="pull_request", action="closed", pr_number=58),
            now=NOW + 60_000,
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertEqual(plan.selected.pr_number, 57)
        self.assertIn("PR #58 left queue: state=closed.", logs(plan))
        self.assertIn("Released stale review slot for PR #58 (state=closed).", logs(plan))
        self.assertEqual(len(client.provider_commands()), 2)

    def test_waiting_reconciles_again_once_the_cooldown_expires(self):
        client = self._repository(
            pulls=[self._ready_pull(57)],
            comments={
                57: [
                    support.issue_comment(
                        "Review rate limited. Your next included review will be available "
                        "in 5 minutes.",
                        login="coderabbitai[bot]",
                        created_at=iso(NOW),
                        updated_at=iso(NOW),
                    )
                ]
            },
        )
        self._green(client, 57)

        clock_value = {"now": NOW}
        slept: list = []
        plan = controller.reconcile(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="schedule"),
            now=NOW,
            wait_ms=10 * 60_000,
            sleep=lambda seconds: (
                slept.append(seconds),
                clock_value.update(now=NOW + int(seconds * 1000)),
            )
            and None,
            clock=lambda: clock_value["now"],
        )
        self.assertEqual(plan.action, queue.ACTION_DISPATCH)
        self.assertTrue(slept)
        self.assertEqual(len(client.provider_commands()), 1)

    def test_a_source_issue_priority_label_orders_the_queue(self):
        client = self._repository(
            pulls=[self._ready_pull(57, ""), self._ready_pull(59, "")]
        )
        self._green(client, 57, 59)
        client.issues[57] = {"number": 57, "labels": [{"name": "priority:p2"}]}
        client.issues[59] = {"number": 59, "labels": [{"name": "priority:p0"}]}

        plan = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="schedule"),
            now=NOW,
            apply=False,
        )
        self.assertEqual(plan.selected.pr_number, 59)
        self.assertEqual(plan.selected.priority, "priority:p0")

        # The issue label changes: the queue must reorder on the next wake-up.
        client.issues[59] = {"number": 59, "labels": [{"name": "priority:p2"}]}
        client.issues[57] = {"number": 57, "labels": [{"name": "priority:p0"}]}
        reordered = controller.reconcile_once(
            client,
            support.queue_config(),
            wake=queue.WakeUp(event="issues", action="labeled"),
            now=NOW,
            apply=False,
        )
        self.assertEqual(reordered.selected.pr_number, 57)
        self.assertEqual(reordered.selected.priority, "priority:p0")

    def test_an_unreadable_check_run_never_looks_green(self):
        class BrokenChecks(support.FakeQueueGitHub):
            def list_check_runs(self, ref):
                from continuum.review.github import GitHubError

                raise GitHubError("check runs unavailable", status=503)

        client = BrokenChecks(pulls=[self._ready_pull(57)])
        plan = controller.reconcile_once(
            client, support.queue_config(), wake=queue.WakeUp(event="schedule"), now=NOW
        )
        self.assertEqual(plan.action, queue.ACTION_IDLE)
        self.assertIn("ci=unknown", logs(plan))


class RequestRecordTests(unittest.TestCase):
    def test_record_round_trip(self):
        body = controller.render_request_record(
            pr_number=57, head_sha="a" * 40, provider="coderabbit", kind="initial", requested_at_ms=NOW
        )
        record = controller.parse_request_record(body)
        self.assertEqual(
            record,
            {
                "pr": 57,
                "head": "a" * 40,
                "provider": "coderabbit",
                "kind": "initial",
                "requested_at_ms": NOW,
            },
        )

    def test_a_foreign_comment_is_not_a_record(self):
        self.assertIsNone(controller.parse_request_record("hello"))
        self.assertIsNone(controller.parse_request_record(controller.REQUEST_MARKER))
        self.assertIsNone(
            controller.parse_request_record(
                controller.REQUEST_MARKER + "\n```json\n{\"schema\": \"other/v1\"}\n```"
            )
        )

    def test_ci_aggregation(self):
        self.assertEqual(controller.aggregate_ci([]), queue.CI_UNKNOWN)
        self.assertEqual(
            controller.aggregate_ci([support.check_run()]), queue.CI_SUCCESS
        )
        self.assertEqual(
            controller.aggregate_ci([support.check_run(conclusion="failure")]),
            queue.CI_FAILURE,
        )
        self.assertEqual(
            controller.aggregate_ci([support.check_run(status="in_progress", conclusion=None)]),
            queue.CI_PENDING,
        )
        self.assertEqual(
            controller.aggregate_ci(
                [support.check_run("CI"), support.check_run("bootstrap-validation")],
                ("CI",),
            ),
            queue.CI_SUCCESS,
        )
        self.assertEqual(controller.aggregate_ci([support.check_run()], ("Release",)), queue.CI_PENDING)


if __name__ == "__main__":
    unittest.main()
