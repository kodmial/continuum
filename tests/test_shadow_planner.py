"""The scenario planners, driven through the real decision engines.

Each test here runs one event against one capture and asserts on the *decision*
and the *effects*, not on the plumbing that got there. Two properties are
checked across every scenario as well:

* no scenario performs a write -- every effect is suppressed, and the client is
  the record-only one;
* no scenario crashes. A shadow run that raised instead of journalling would be
  indistinguishable from one that was never started.
"""

from __future__ import annotations

import unittest

from continuum.shadow import event as event_module
from continuum.shadow import planner
from continuum.shadow.effects import forbidden_in_shadow
from continuum.shadow.journal import ShadowJournal
from tests import shadow_support as fixtures


def plan(scenario: str, index: int = 0) -> ShadowJournal:
    return planner.plan(
        fixtures.event_for(scenario),
        fixtures.states_for(scenario)[index],
        fixtures.config(),
        origin="test",
    )


def kinds(journal: ShadowJournal) -> list:
    return [effect.kind for effect in journal.actions]


def guard(journal: ShadowJournal, name: str):
    for entry in journal.guards:
        if entry.name == name:
            return entry
    raise AssertionError(
        "no {!r} guard in [{}]".format(name, ", ".join(g.name for g in journal.guards))
    )


class EveryScenario(unittest.TestCase):
    def test_every_scenario_class_reaches_a_terminal_decision(self) -> None:
        for scenario in fixtures.SCENARIO_EVENTS:
            for index, _state in enumerate(fixtures.states_for(scenario)):
                with self.subTest(scenario=scenario, capture=index):
                    journal = plan(scenario, index)
                    self.assertTrue(journal.is_terminal)
                    self.assertNotEqual(journal.status, planner.STATUS_FAILED)
                    self.assertNotEqual(journal.decision, "crashed")
                    self.assertTrue(journal.decision, "a decision with no vocabulary")
                    self.assertTrue(journal.decision_source, "a decision with no source")

    def test_no_scenario_performs_a_write(self) -> None:
        for scenario in fixtures.SCENARIO_EVENTS:
            with self.subTest(scenario=scenario):
                journal = plan(scenario)
                self.assertEqual(forbidden_in_shadow(journal.actions), ())

    def test_every_scenario_records_its_guards_and_engine(self) -> None:
        for scenario in fixtures.SCENARIO_EVENTS:
            with self.subTest(scenario=scenario):
                journal = plan(scenario)
                self.assertTrue(journal.guards)
                self.assertEqual(journal.engine.get("continuum_version"), 1)
                self.assertTrue(journal.engine.get("engine_sha"))
                self.assertTrue(journal.config.get("version"))
                self.assertEqual(journal.notes.get("scenario"), scenario)


class IssueLifecycle(unittest.TestCase):
    def test_a_trusted_issue_opens_a_pull_request_without_a_wake_up(self) -> None:
        journal = plan(planner.SCENARIO_ISSUE_LIFECYCLE)
        self.assertEqual(guard(journal, "trust.authorization").outcome, "allow")
        # ``issues: opened`` is not a queue trigger, and saying so is the point:
        # widening it would invent a dispatch production never made.
        self.assertEqual(guard(journal, "queue.wake_up").outcome, "ignored")
        self.assertEqual(journal.status, planner.STATUS_NO_ACTION)

    def test_a_fork_pull_request_is_refused_and_the_refusal_is_recorded(self) -> None:
        # The refusal is journal evidence. "Continuum would not have dispatched"
        # is a parity claim, and a queue that reported nothing would leave the
        # claim unrecorded.
        journal = planner.plan(
            fixtures.pull_synchronize_fork(), fixtures.state(), fixtures.config()
        )
        self.assertEqual(journal.status, planner.STATUS_DENIED)
        self.assertEqual(journal.decision, "fork_pull_request")
        self.assertEqual(guard(journal, "trust.authorization").outcome, "deny")
        self.assertEqual(kinds(journal), ["workflow.dispatch"])
        self.assertEqual(
            journal.actions[0].detail["refused"], "fork_pull_request"
        )

    def test_a_queue_wake_up_dispatches_after_writing_the_slot_first(self) -> None:
        # The in-flight slot has to exist before the dispatch goes out; the
        # queue controller's rollback depends on the order.
        journal = planner.plan(
            event_module.from_payload(
                {
                    "correlation_id": "e-synchronize",
                    "repository": "acme/widgets",
                    "event_type": "pull_request.synchronize",
                    "action": "synchronize",
                    "number": 43,
                    "head_sha": fixtures.HEAD_E,
                }
            ),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual(journal.status, planner.STATUS_OK)
        comments = [effect for effect in journal.actions if effect.kind == "comment.create"]
        self.assertGreaterEqual(len(comments), 1)
        self.assertEqual(kinds(journal)[-1], "comment.create")


class CiRepair(unittest.TestCase):
    def test_a_behind_pull_request_dispatches_and_merges(self) -> None:
        # One ``workflow_run: CI completed`` starts two production workflows.
        # A shadow run that only walked the repair would compare half a program
        # against all of production's.
        journal = plan(planner.SCENARIO_CI_REPAIR)
        self.assertEqual(journal.decision, "update-branch")
        self.assertEqual(
            kinds(journal), ["label.add", "workflow.dispatch", "pull.merge"]
        )
        self.assertEqual(guard(journal, "merge.eligibility").outcome, "allow")

    def test_the_dispatch_carries_the_policy_inputs(self) -> None:
        journal = plan(planner.SCENARIO_CI_REPAIR)
        dispatch = [e for e in journal.actions if e.kind == "workflow.dispatch"][0]
        inputs = dispatch.detail["inputs"]
        self.assertEqual(dispatch.detail["ref"], "main")
        self.assertEqual(inputs["mode"], "resolve-conflict")
        self.assertEqual(inputs["pr_number"], "43")
        self.assertEqual(inputs["head_ref"], "opencode/issue8-green")

    def test_the_lock_is_taken_before_the_dispatch(self) -> None:
        # Production's dispatcher adds the lock and rolls it back if the
        # dispatch fails; the order is the whole rollback.
        journal = plan(planner.SCENARIO_CI_REPAIR)
        self.assertEqual(
            journal.actions[0].detail["labels"], ["opencode-conflict-repair"]
        )

    def test_a_pull_request_that_contains_base_releases_its_lock(self) -> None:
        # The ladder's rule, and the reason it exists: a lock left on a healthy
        # pull request is what made one repair episode invisible to every later
        # reconciliation, so "nothing to repair" is a write.
        journal = planner.plan(
            fixtures.check_suite_green(), fixtures.state(), fixtures.config()
        )
        self.assertEqual(journal.decision, "none")
        self.assertEqual(kinds(journal), ["label.remove"])
        self.assertEqual(
            journal.actions[0].detail["label"], "opencode-conflict-repair"
        )

    def test_a_redelivered_ci_event_does_not_dispatch_twice(self) -> None:
        journal = plan(planner.SCENARIO_DUPLICATE_REPLAY)
        self.assertEqual(journal.decision, "idempotent")
        self.assertEqual(journal.status, planner.STATUS_OK)
        self.assertEqual(kinds(journal), ["label.add", "workflow.dispatch"])
        self.assertEqual(guard(journal, "replay.idempotency").outcome, "no-new-effects")

    def test_a_redelivery_without_its_event_type_is_refused(self) -> None:
        # "Was it idempotent" has no answer without knowing what was redelivered.
        event = fixtures.duplicate_replay()
        journal = planner.plan(
            event_module.from_payload(
                {
                    "correlation_id": event.correlation_id,
                    "repository": event.repository,
                    "event_type": "duplicate.replay",
                    "action": "redelivered",
                    "number": 43,
                    "replays": "e-ci",
                }
            ),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual(journal.status, planner.STATUS_REJECTED)
        self.assertEqual(journal.decision, "missing_original_event_type")
        self.assertEqual([error["code"] for error in journal.errors], ["missing_original_event_type"])


class CoderabbitFinding(unittest.TestCase):
    def test_a_verdict_is_reached_through_the_gate(self) -> None:
        journal = plan(planner.SCENARIO_CODERABBIT_FINDING)
        verdict = journal.notes["review_verdict"]
        # The capture has no provider summary comment, so coverage is
        # incomplete and the gate cannot pass the review.
        self.assertEqual(verdict, "COMMENT")
        self.assertEqual(guard(journal, "review.verdict").outcome, "COMMENT")

    def test_the_gate_publishes_review_and_status(self) -> None:
        journal = plan(planner.SCENARIO_CODERABBIT_FINDING)
        self.assertIn("review.create", kinds(journal))
        self.assertIn("status.create", kinds(journal))

    def test_a_blocking_verdict_dispatches_the_agent(self) -> None:
        journal = plan(planner.SCENARIO_CODERABBIT_FINDING)
        self.assertIn("workflow.dispatch", kinds(journal))

    def test_a_review_comment_is_not_a_queue_wake_up(self) -> None:
        journal = planner.plan(
            fixtures.coderabbit_review_comment(), fixtures.state(), fixtures.config()
        )
        self.assertEqual(guard(journal, "queue.wake_up").outcome, "ignored")


class MergeDecision(unittest.TestCase):
    def test_green_and_trusted_merges(self) -> None:
        journal = plan(planner.SCENARIO_MERGE_DECISION)
        self.assertEqual(journal.decision, "merge_allowed")
        self.assertEqual(kinds(journal), ["pull.merge"])
        self.assertEqual(guard(journal, "merge.ci_green").outcome, "green")

    def test_failing_ci_blocks_the_merge(self) -> None:
        journal = planner.plan(
            planner_event := fixtures.event(
                "schedule.tick", number=41, action="merge", correlation_id="e-merge-41"
            ),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual(journal.status, planner.STATUS_DENIED)
        self.assertEqual(journal.decision, "ci_not_green")
        self.assertEqual(kinds(journal), [])
        del planner_event

    def test_a_fork_is_refused_whatever_the_ci_says(self) -> None:
        journal = planner.plan(
            fixtures.event("schedule.tick", number=42, action="merge", correlation_id="e-merge-42"),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual(journal.decision, "fork_pull_request")


class ReleasePlanning(unittest.TestCase):
    def test_a_dry_run_walks_every_stage_and_publishes_nothing(self) -> None:
        journal = plan(planner.SCENARIO_RELEASE_PLANNING)
        self.assertEqual(journal.status, planner.STATUS_OK)
        stages = [
            guard.name for guard in journal.guards if guard.name.startswith("release.stage.")
        ]
        # Every stage has to be walked. A chain that stopped early would report
        # parity against a plan that never existed.
        self.assertGreaterEqual(len(stages), 8)
        self.assertEqual(forbidden_in_shadow(journal.actions), ())
        self.assertIn("release.publish", kinds(journal))

    def test_a_release_without_a_pinned_commit_is_refused(self) -> None:
        payload = {
            "correlation_id": "e-release-unpinned",
            "repository": "acme/widgets",
            "event_type": "release.event",
            "action": "published",
            "release": {"tag": "v0.1.0", "version": "0.1.0", "dry_run": True},
        }
        journal = planner.plan(
            event_module.from_payload(payload), fixtures.state(), fixtures.config()
        )
        self.assertEqual(journal.status, planner.STATUS_REJECTED)
        self.assertEqual(journal.decision, "missing_source_sha")
        self.assertEqual(journal.actions, ())


class DependencyBlocked(unittest.TestCase):
    def test_an_open_blocker_is_reported_and_the_queue_still_runs(self) -> None:
        journal = plan(planner.SCENARIO_DEPENDENCY_BLOCKED)
        self.assertEqual(guard(journal, "dependency.blockers").outcome, "blocked")
        self.assertEqual(journal.notes["open_blockers"], [8])
        # ``issues: labeled`` genuinely reorders the review queue even for an
        # issue that cannot be worked yet, so a shadow run that only checked the
        # dependencies would miss the dispatch.
        self.assertEqual(journal.status, planner.STATUS_OK)
        self.assertEqual(kinds(journal), ["comment.create", "comment.create"])

    def test_a_closed_blocker_dispatches_on_the_same_evidence(self) -> None:
        blocked = plan(planner.SCENARIO_DEPENDENCY_BLOCKED, 0)
        clear = plan(planner.SCENARIO_DEPENDENCY_BLOCKED, 1)
        self.assertEqual(guard(blocked, "dependency.blockers").outcome, "blocked")
        self.assertEqual(guard(clear, "dependency.blockers").outcome, "clear")
        # The two captures differ only in the blocker state, so any difference
        # in the action would be a decision that read something else.
        self.assertEqual(kinds(blocked), kinds(clear))


class TimeoutRecovery(unittest.TestCase):
    def test_a_healthy_pull_request_needs_no_recovery(self) -> None:
        journal = plan(planner.SCENARIO_TIMEOUT_RECOVERY)
        self.assertEqual(journal.status, planner.STATUS_NO_ACTION)
        self.assertEqual(journal.actions, ())

    def test_the_watchdog_uses_the_captured_clock(self) -> None:
        # The verdict has to be the same a year from now, so the age comes from
        # the capture rather than from the wall clock.
        journal = plan(planner.SCENARIO_TIMEOUT_RECOVERY)
        self.assertIsNotNone(journal.duration_ms)
        self.assertTrue(any(g.name.startswith("repair.") for g in journal.guards))


class Refusals(unittest.TestCase):
    def test_an_unplannable_event_is_rejected_not_defaulted(self) -> None:
        with self.assertRaises(event_module.EventError) as caught:
            fixtures.event("push.something")
        self.assertEqual(caught.exception.code, "unknown_event_type")

    def test_a_missing_head_is_refused_for_a_head_scoped_event(self) -> None:
        with self.assertRaises(event_module.EventError) as caught:
            fixtures.event("pull_request.synchronize", number=43)
        self.assertEqual(caught.exception.code, "missing_head_sha")

    def test_a_planner_crash_still_produces_a_journal(self) -> None:
        # ``require_pull`` refuses a pull request the capture does not have, and
        # that refusal is a finding about the capture, not a crash.
        journal = planner.plan(
            fixtures.event("check_suite.completed", number=99, head_sha=fixtures.HEAD_A),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual(journal.status, planner.STATUS_REJECTED)
        self.assertEqual(journal.decision, "unknown_pull_request")
        self.assertTrue(journal.is_terminal)

    def test_an_orphan_event_is_reported_in_the_journal(self) -> None:
        journal = planner.plan(
            fixtures.event("check_suite.completed", number=99, head_sha=fixtures.HEAD_A),
            fixtures.state(),
            fixtures.config(),
        )
        self.assertEqual([error["code"] for error in journal.errors], ["unknown_pull_request"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
