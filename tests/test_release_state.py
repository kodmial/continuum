"""The chain, its keys, and its record of itself.

The state machine is the part of a release that decides whether something
happens twice. Its tests are therefore mostly about *not* happening twice: a
duplicate event, a re-dispatched delivery, a re-run of a failed job, and a plan
that must not count as a run. The one place it is allowed to be permissive is
a stage that changed nothing, which is asked again and costs nothing.
"""

from __future__ import annotations

import unittest

from continuum.release import state
from continuum.release.state import (
    BLOCKED,
    COMPLETED,
    EMPTY_JOURNAL,
    FAILED,
    IllegalTransition,
    Journal,
    NOOP,
    ReleaseStateError,
    SKIPPED,
    STAGES,
    StageOutcome,
    UnknownStage,
    assert_transition,
    channel_key,
    event_key,
    next_stage,
    owed_reconciliation,
    release_key,
    scopes_are_isolated,
    stage,
    stage_key,
    stage_names,
    unit_key,
    wake_key,
)

from . import release_core_support as support

RELEASE = release_key(support.REPOSITORY, support.VERSION)


class ChainTests(unittest.TestCase):
    def test_the_chain_is_the_documented_one(self):
        self.assertEqual(
            stage_names(),
            (
                "eligibility",
                "version",
                "release-pr",
                "validation",
                "source",
                "build",
                "sign",
                "verify",
                "draft",
                "publish",
                "sync",
            ),
        )

    def test_every_legal_move_is_enumerable(self):
        self.assertEqual(len(state.TRANSITIONS), len(STAGES) - 1)
        for source, destination in state.TRANSITIONS:
            assert_transition(source, destination)

    def test_a_stage_cannot_be_skipped(self):
        with self.assertRaises(IllegalTransition) as caught:
            assert_transition("build", "publish")
        self.assertIn("source", str(caught.exception))

    def test_a_move_cannot_run_backwards(self):
        with self.assertRaises(IllegalTransition):
            assert_transition("publish", "draft")

    def test_an_unknown_stage_is_refused_by_name(self):
        with self.assertRaises(UnknownStage):
            stage("sign-and-upload")

    def test_the_next_stage_is_the_next_stage(self):
        self.assertEqual(next_stage("build"), "sign")
        self.assertIsNone(next_stage("sync"))

    def test_signing_and_verifying_are_separate_stages(self):
        # One requirement, two claims: a signature that verifies is not the same
        # as a signature from the identity a release pins, and a release has to be
        # able to fail between them.
        self.assertIn(("build", "sign"), state.TRANSITIONS)
        self.assertIn(("sign", "verify"), state.TRANSITIONS)

    def test_only_the_release_pull_request_and_the_sync_may_be_empty(self):
        optional = {item.name for item in STAGES if item.optional}
        self.assertEqual(optional, {"release-pr", "sync"})

    def test_a_no_op_at_these_stages_ends_the_release(self):
        # `terminal` is not `optional` written differently: the release pull
        # request is optional and *passed through* when it does nothing, while
        # eligibility and the destination stages are where "nothing to do" is a
        # statement about the whole release rather than about one step of it.
        terminal = {item.name for item in STAGES if item.terminal}
        self.assertEqual(terminal, {"eligibility", "draft", "publish", "sync"})
        self.assertNotIn("release-pr", terminal)

    def test_the_stages_that_write_say_so(self):
        for name in ("release-pr", "build", "sign", "draft", "publish", "sync"):
            self.assertTrue(stage(name).side_effects, name)
        for name in ("eligibility", "version", "validation", "source", "verify"):
            self.assertFalse(stage(name).side_effects, name)


class ScopeTests(unittest.TestCase):
    def test_release_permissions_cannot_reach_a_pull_request(self):
        self.assertTrue(scopes_are_isolated())
        self.assertEqual(set(state.RELEASE_SCOPES) & set(state.PR_AUTOMATION_SCOPES), set())

    def test_a_release_may_publish_and_attest(self):
        for scope in ("contents: write", "packages: write", "attestations: write"):
            self.assertIn(scope, state.RELEASE_SCOPES)

    def test_the_stages_that_publish_ask_for_the_scope_that_publishes(self):
        self.assertEqual(stage("draft").scope, "contents: write")
        self.assertEqual(stage("publish").scope, "contents: write")
        self.assertEqual(stage("sync").scope, "packages: write")


class KeyTests(unittest.TestCase):
    def test_a_delivery_does_not_change_the_event_key(self):
        self.assertEqual(
            event_key(support.REPOSITORY, "push", support.SHA),
            event_key(support.REPOSITORY, "push", support.SHA),
        )

    def test_a_different_commit_is_a_different_event(self):
        self.assertNotEqual(
            event_key(support.REPOSITORY, "push", support.SHA),
            event_key(support.REPOSITORY, "push", support.OTHER_SHA),
        )

    def test_the_parts_of_a_key_cannot_be_shifted_between_them(self):
        # ("1.2", "3") and ("1", "2.3") joined with a space would collide.
        self.assertNotEqual(
            state.digest_key("1.2", "3"), state.digest_key("1", "2.3")
        )

    def test_a_release_key_is_one_version_of_one_repository(self):
        self.assertEqual(
            release_key(support.REPOSITORY, support.VERSION),
            f"continuum-release/{support.REPOSITORY}/{support.VERSION}",
        )

    def test_a_version_that_is_not_a_slug_cannot_be_a_key(self):
        with self.assertRaises(ReleaseStateError):
            release_key(support.REPOSITORY, "1.4.0/../etc")

    def test_a_concurrency_group_serialises_a_repository_and_does_not_cancel(self):
        self.assertEqual(
            channel_key(support.REPOSITORY, "default"),
            f"continuum-release/{support.REPOSITORY}/default",
        )

    def test_a_stage_key_is_scoped_to_the_release(self):
        self.assertNotEqual(
            stage_key(RELEASE, "publish"),
            stage_key(release_key(support.REPOSITORY, "9.9.9"), "publish"),
        )

    def test_a_unit_key_is_scoped_to_the_stage_and_the_unit(self):
        self.assertNotEqual(
            unit_key(RELEASE, "build", "fixture"),
            unit_key(RELEASE, "sign", "fixture"),
        )
        self.assertNotEqual(
            unit_key(RELEASE, "build", "fixture"),
            unit_key(RELEASE, "build", "other"),
        )

    def test_a_unit_key_must_name_what_it_acts_on(self):
        with self.assertRaises(ReleaseStateError):
            unit_key(RELEASE, "build", "")

    def test_an_unknown_stage_has_no_key(self):
        with self.assertRaises(UnknownStage):
            stage_key(RELEASE, "teleport")


class StageOutcomeTests(unittest.TestCase):
    def test_an_outcome_must_say_what_it_did(self):
        with self.assertRaises(ReleaseStateError):
            StageOutcome(stage="build", key="k", outcome=COMPLETED, summary="")

    def test_an_outcome_must_be_on_the_chain(self):
        with self.assertRaises(ReleaseStateError):
            StageOutcome(stage="build", key="k", outcome="maybe", summary="x")

    def test_a_failure_must_carry_a_code(self):
        with self.assertRaises(ReleaseStateError) as caught:
            StageOutcome(stage="publish", key="k", outcome=FAILED, summary="it broke")
        self.assertIn("code", str(caught.exception))

    def test_a_completed_stage_has_nothing_to_retry(self):
        with self.assertRaises(ReleaseStateError):
            StageOutcome(
                stage="build",
                key="k",
                outcome=COMPLETED,
                summary="built",
                retryable=True,
            )

    def test_a_code_is_a_field_not_a_detail(self):
        outcome = StageOutcome.noop("eligibility", "k", "off", code="release-disabled")
        self.assertEqual(outcome.code, "release-disabled")
        self.assertEqual(outcome.detail("code"), "")

    def test_a_duplicate_says_so(self):
        outcome = StageOutcome.skipped("publish", "k", "already", duplicate=True)
        self.assertTrue(outcome.duplicate)
        self.assertTrue(outcome.ok)
        self.assertFalse(StageOutcome.noop("sync", "k", "nothing to do").duplicate)

    def test_a_blocked_outcome_is_not_green(self):
        self.assertFalse(StageOutcome.blocked("eligibility", "k", "no", code="x").ok)

    def test_details_survive_a_rewrite(self):
        outcome = StageOutcome.completed("build", "k", "built", target="fixture")
        self.assertEqual(outcome.detail("target"), "fixture")
        self.assertEqual(outcome.with_detail(target="other").detail("target"), "other")
        self.assertEqual(outcome.detail("target"), "fixture")


class JournalTests(unittest.TestCase):
    def _key(self, name: str) -> str:
        return unit_key(RELEASE, "build", name)

    def test_an_empty_journal_has_nothing_recorded(self):
        self.assertIsNone(EMPTY_JOURNAL.entry("anything"))
        self.assertEqual(EMPTY_JOURNAL.resume_point(), "eligibility")

    def test_a_recorded_stage_is_recognised(self):
        journal = Journal().record(StageOutcome.completed("build", self._key("a"), "built"))
        self.assertTrue(journal.is_complete(self._key("a")))
        self.assertFalse(journal.is_complete(self._key("b")))

    def test_the_latest_entry_for_a_key_is_the_one_that_counts(self):
        journal = (
            Journal()
            .record(StageOutcome.completed("build", self._key("a"), "built"))
            .record(
                StageOutcome.failed("build", self._key("a"), "broke", code="boom", retryable=True)
            )
        )
        self.assertFalse(journal.is_complete(self._key("a")))
        self.assertEqual(journal.resume_point(), "build")

    def test_a_plan_does_not_count_as_having_done_anything(self):
        plan = StageOutcome.completed("build", self._key("a"), "would build", **{"dry-run": "true"})
        journal = Journal().record(plan)
        self.assertEqual(journal.completed_keys, ())
        self.assertTrue(journal.was_just_a_plan(plan))
        self.assertEqual(journal.resume_point(), "eligibility")

    def test_is_complete_ignores_a_plan(self):
        plan = StageOutcome.completed(
            "build", self._key("a"), "would build", **{"dry-run": "true"}
        )
        self.assertFalse(Journal().record(plan).is_complete(self._key("a")))

    def test_a_plan_underneath_a_real_record_does_not_mask_it(self):
        # The order that matters: a real run published this, and then a plan was
        # taken of the same event. Reading only the *last* entry would see the
        # plan and decide nothing had been done, which is how a dry run ends up
        # causing the duplicate release it exists to prevent.
        real = StageOutcome.completed("build", self._key("a"), "built")
        plan = StageOutcome.completed(
            "build", self._key("a"), "would build", **{"dry-run": "true"}
        )
        journal = Journal().record(real).record(plan)
        self.assertTrue(journal.is_complete(self._key("a")))
        self.assertEqual(journal.completed_keys, (self._key("a"),))
        self.assertIs(journal.trusted[self._key("a")], real)

    def test_a_real_record_underneath_a_plan_is_still_the_one_trusted(self):
        real = StageOutcome.failed(
            "build", self._key("a"), "broke", code="boom", retryable=True
        )
        plan = StageOutcome.completed(
            "build", self._key("a"), "would build", **{"dry-run": "true"}
        )
        journal = Journal().record(plan).record(real).record(plan)
        self.assertFalse(journal.is_complete(self._key("a")))
        self.assertEqual(journal.trusted[self._key("a")].outcome, FAILED)
        self.assertEqual(journal.resume_point(), "build")

    def test_a_key_completed_then_retried_and_failed_is_not_done(self):
        journal = (
            Journal()
            .record(StageOutcome.completed("build", self._key("a"), "built"))
            .record(
                StageOutcome.failed("build", self._key("a"), "broke", code="boom", retryable=True)
            )
        )
        self.assertEqual(journal.completed_keys, ())
        self.assertFalse(journal.is_complete(self._key("a")))

    def test_a_plan_does_not_bind_a_version_to_a_commit(self):
        journal = Journal().record(
            StageOutcome.completed(
                "source",
                "k",
                "pinned",
                version=support.VERSION,
                source_sha=support.SHA,
                **{"dry-run": "true"},
            )
        )
        self.assertIsNone(journal.bound_source(support.VERSION))

    def test_a_run_binds_a_version_to_the_commit_it_pinned(self):
        journal = Journal().record(
            StageOutcome.completed(
                "source",
                "k",
                "pinned",
                version=support.VERSION,
                source_sha=support.SHA,
            )
        )
        self.assertEqual(journal.bound_source(support.VERSION), support.SHA)
        self.assertIsNone(journal.bound_source("9.9.9"))

    def test_a_retryable_failure_is_resumed_at_the_stage_that_failed(self):
        journal = (
            Journal()
            .record(StageOutcome.completed("build", "k1", "built"))
            .record(StageOutcome.failed("publish", "k2", "timed out", code="x", retryable=True))
        )
        self.assertEqual(journal.resume_point(), "publish")

    def test_a_fatal_failure_is_not_resumed_at_all(self):
        journal = Journal().record(
            StageOutcome.failed("verify", "k", "wrong identity", code="bad-signing")
        )
        self.assertIsNone(journal.resume_point())

    def test_a_blocked_stage_is_not_resumed(self):
        journal = Journal().record(StageOutcome.blocked("eligibility", "k", "no", code="x"))
        self.assertIsNone(journal.resume_point())

    def test_a_finished_journal_has_nothing_to_resume(self):
        journal = Journal().extend(
            [StageOutcome.completed(name, f"k-{name}", "done") for name in stage_names()]
        )
        self.assertIsNone(journal.resume_point())

    def test_the_journal_describes_itself_for_a_job_log(self):
        described = Journal().record(
            StageOutcome.failed("publish", "k", "timed out", code="upload-failed", retryable=True)
        ).describe()
        self.assertEqual(described["resume_point"], "publish")
        self.assertEqual(described["entries"][0]["code"], "upload-failed")
        self.assertTrue(described["entries"][0]["retryable"])

    def test_a_summary_is_one_line_per_transition(self):
        journal = Journal().record(StageOutcome.noop("sync", "k", "no sync configured"))
        self.assertEqual(journal.summaries(), ["sync: noop — no sync configured"])


class ReconciliationWakeTests(unittest.TestCase):
    """Deferred work becomes owed when the release it was waiting on completes.

    The failure is a stranded release-PR update, and it strands silently. A code
    change merges while a release is still publishing, the release-pr reconciliation
    correctly defers, the release then completes green -- and nothing ever looks at
    the deferral again, so the code waits indefinitely for a reconciliation that
    already happened once and was told to wait.

    So the deferral has to survive into the next run as something *owed*.
    """

    def _deferred_then_published(self):
        return Journal().record(
            StageOutcome.blocked("release-pr", "k-pr", "deferred: a release is publishing")
        ).record(StageOutcome.completed("publish", "k-publish", "published 1.4.0"))

    def test_a_completed_release_owes_the_stage_it_deferred(self):
        self.assertEqual(owed_reconciliation(self._deferred_then_published()), "release-pr")

    def test_a_deferral_is_not_owed_while_the_release_is_still_running(self):
        # Otherwise the wake fires immediately, before the thing it is waiting for
        # has happened, and re-defers -- a wake loop driven by its own retry.
        journal = Journal().record(
            StageOutcome.blocked("release-pr", "k", "deferred: a release is publishing")
        )
        self.assertIsNone(owed_reconciliation(journal))

    def test_a_non_terminal_completion_does_not_owe_anything(self):
        # `source` completing says the release is pinned, not finished. Waking on it
        # would re-run reconciliation against a release that has not published.
        journal = Journal().record(
            StageOutcome.blocked("release-pr", "k", "deferred")
        ).record(StageOutcome.completed("source", "k2", "bound 9e6dcc2"))
        self.assertIsNone(owed_reconciliation(journal))

    def test_a_fatal_failure_is_not_reopened_by_an_unrelated_completion(self):
        # The thing that has to change is the release, not the failure. Re-opening it
        # on somebody else's success is how a fatal failure becomes a loop.
        journal = Journal().record(
            StageOutcome.failed("release-pr", "k", "branch is protected", code="protected")
        ).record(StageOutcome.completed("publish", "k2", "published"))
        self.assertIsNone(owed_reconciliation(journal))

    def test_a_dry_run_owes_nothing(self):
        # A plan's blocked entry describes what *would* wait, so treating it as a
        # real deferral would make planning a release schedule a wake-up.
        journal = Journal().record(
            StageOutcome.blocked(
                "release-pr", "k", "deferred", **{"dry-run": "true"}
            )
        ).record(StageOutcome.completed("publish", "k2", "published"))
        self.assertIsNone(owed_reconciliation(journal))

    def test_the_earliest_deferred_stage_is_the_one_owed(self):
        journal = Journal().record(
            StageOutcome.blocked("sync", "k-sync", "deferred")
        ).record(StageOutcome.blocked("release-pr", "k-pr", "deferred")).record(
            StageOutcome.completed("publish", "k-publish", "published")
        )
        self.assertEqual(owed_reconciliation(journal), "release-pr")

    def test_a_nothing_deferred_release_owes_nothing(self):
        journal = Journal().record(StageOutcome.completed("publish", "k", "published"))
        self.assertIsNone(owed_reconciliation(journal))

    def test_the_terminal_stages_are_read_off_the_chain(self):
        # A stage added to the chain has to be terminal by being declared terminal,
        # not by being added to a second list here.
        self.assertEqual(
            tuple(item.name for item in STAGES if item.terminal), state.TERMINAL_STAGES
        )
        self.assertIn("publish", state.TERMINAL_STAGES)

    def test_a_wake_is_idempotent_for_one_source_head(self):
        # Three deliveries of one completed release -- a re-dispatch, a re-run, and
        # a second workflow watching the same branch -- are one wake. Without this a
        # busy repository reconciles its release PR once per delivery.
        head = "d9e6dcc2e0c9c43bc87cd1963b75c097dec7ea1c"
        first = wake_key("acme/widget", head)
        self.assertEqual(wake_key("acme/widget", head), first)
        self.assertEqual(wake_key("acme/widget", head.upper()), first)
        self.assertNotEqual(wake_key("acme/widget", "9" * 40), first)
        self.assertNotEqual(wake_key("acme/other", head), first)

    def test_a_wake_must_name_the_head_that_completed(self):
        # Keyed on nothing, every completion in the repository shares one key and
        # only the first of them is ever reconciled.
        for value in ("", "   "):
            with self.assertRaises(ReleaseStateError) as caught:
                wake_key("acme/widget", value)
            self.assertEqual(caught.exception.code, "missing-source-sha")


if __name__ == "__main__":
    unittest.main()
