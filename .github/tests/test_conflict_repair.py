#!/usr/bin/env python3
"""Regression suite for the runtime-lab #66 conflict-repair failure.

On 2026-09-28 a *finished* pull request (runtime-lab #66, from issue #56) was
frozen as ``no-auto-merge`` + ``opencode-conflict-repair`` after ``main``
advanced. Conflict recovery then dispatched the agent in **issue mode**, which
re-ran the whole source task instead of repairing the change that already
existed. The re-run burned ~35 minutes, hit the workflow timeout, published
nothing, and left the original pull request frozen with no watchdog to release
it. The task was unrecoverable even though the implementation had been
completed the day before.

These tests encode that incident as invariants, so the failure mode cannot come
back through a refactor:

* a conflict never selects "re-execute the source task" (RecoveryLadderTests),
* a repair's runtime is a constant, not a function of the source task
  (RepairBudgetTests),
* a mechanical replay preserves the existing commits (ReplayTests, and the
  end-to-end git test in :class:`MechanicalReplayGitTests`),
* a failed or timed-out repair leaves the pull request reconcilable
  (FailureRecoveryTests, and the shell-level test in
  :class:`RepairRunShellTests`),
* publication is transactional (PublicationTests),
* something always notices a stale state (WatchdogTests),
* the workflows are actually wired to the policy (WorkflowWiringTests).

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import json
import os
import re
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / ".github" / "scripts"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

sys.path.insert(0, str(SCRIPT_DIR))

import conflict_repair  # noqa: E402
import trust_policy  # noqa: E402

POLICY_SCRIPT = SCRIPT_DIR / "conflict_repair.py"
REPAIR_WORKFLOW = "opencode-repair.yml"
AGENT_WORKFLOW = "opencode.yml"
CONSUMER_AGENT_WORKFLOW = "consumer-opencode.yml"
CONSUMER_REPAIR_WORKFLOW = "consumer-repair.yml"

#: A real-looking commit id. Every API surface in the policy refuses anything
#: that is not a full sha, so tests must use one.
#: The `gh` calls the outcome step makes, in the form the fake CLI records.
LOCK_RELEASE = "DELETE | api repos/kodmial/continuum/issues/66/labels/opencode-conflict-repair"
OUTCOME_COMMENT = "pr comment 66"
OPT_OUT_LABEL = "labels[]=no-auto-merge"
REPAIR_FAILED_LABEL = "labels[]=opencode-repair-failed"

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def run_policy(*args, stdin=None, env=None):
    """Invoke the policy CLI the way a workflow step does."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SCRIPT_DIR)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(POLICY_SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def policy_outputs(*args, stdin=None, env=None):
    """Run the policy CLI and return the ``GITHUB_OUTPUT`` mapping it wrote."""
    with tempfile.TemporaryDirectory() as directory:
        destination = os.path.join(directory, "out.txt")
        result = run_policy(
            *args, stdin=stdin, env={"GITHUB_OUTPUT": destination, **(env or {})}
        )
        if result.returncode != 0:
            raise AssertionError(
                "conflict_repair.py {} failed: {}{}".format(
                    " ".join(args), result.stdout, result.stderr
                )
            )
        mapping = {}
        with open(destination, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.rstrip("\n").partition("=")
                mapping[key] = value
        return mapping


def marker(episode=1, pr=66, head=HEAD_A, attempt=1, state="open"):
    return conflict_repair.render_episode_marker(episode, pr, head, attempt, state)


def workflow_text(name):
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The core invariant: a conflict repairs a change, it does not redo a task
# --------------------------------------------------------------------------- #


class RecoveryLadderTests(unittest.TestCase):
    """The ladder is a closed, ordered, mechanical-first constant."""

    def test_mechanical_rungs_come_before_the_model_rung(self):
        self.assertEqual(
            conflict_repair.RECOVERY_RUNGS,
            ("update-branch", "replay-commits", "resolve-hunks"),
        )
        self.assertEqual(
            list(conflict_repair.MECHANICAL_RUNGS) + list(conflict_repair.AGENT_RUNGS),
            list(conflict_repair.RECOVERY_RUNGS),
        )

    def test_the_ladder_cannot_name_a_source_task_rerun(self):
        # This is the incident. A merge conflict is a request to repair an
        # existing change. There is no rung that re-executes the source issue,
        # so no input -- and no future edit that only adds an input -- can
        # select one.
        for rung in conflict_repair.RECOVERY_RUNGS:
            for word in ("issue", "task", "implement", "rerun", "re-run", "restart"):
                self.assertNotIn(
                    word, rung, "{} must not describe re-executing the source task".format(rung)
                )

    def test_a_fresh_conflict_starts_with_the_free_mechanical_rung(self):
        decision = conflict_repair.evaluate_recovery(behind=12, attempts=0)
        self.assertEqual(decision.action, "update-branch")
        self.assertTrue(decision.hold_lock)

    def test_the_ladder_advances_strictly_in_order(self):
        actions = []
        ruled_out = []
        for _ in conflict_repair.RECOVERY_RUNGS:
            decision = conflict_repair.evaluate_recovery(behind=12, ruled_out=ruled_out)
            actions.append(decision.action)
            ruled_out.append(decision.action)
        self.assertEqual(actions, list(conflict_repair.RECOVERY_RUNGS))
        self.assertLess(
            actions.index("replay-commits"),
            actions.index("resolve-hunks"),
            "mechanical replay must be tried before any model is called",
        )

    def test_the_ladder_never_re_enters_a_rung_it_already_ruled_out(self):
        # A retry must not redo the free mechanical work that already failed,
        # and must never re-run a rung on top of a half-applied attempt.
        decision = conflict_repair.evaluate_recovery(
            behind=12, ruled_out=("update-branch", "replay-commits"), attempts=1
        )
        self.assertEqual(decision.action, "resolve-hunks")

    def test_exhausting_the_ladder_is_an_explicit_failure_not_a_rerun(self):
        decision = conflict_repair.evaluate_recovery(
            behind=12, ruled_out=conflict_repair.RECOVERY_RUNGS, attempts=1
        )
        self.assertEqual(decision.action, conflict_repair.FAILED_ACTION)
        self.assertTrue(decision.record_failure, "a dead repair must be visible")
        self.assertFalse(
            decision.freeze_original, "a dead repair must not strand the pull request"
        )
        self.assertFalse(decision.retryable)

    def test_a_branch_that_already_contains_main_needs_no_repair(self):
        decision = conflict_repair.evaluate_recovery(behind=0, attempts=2)
        self.assertEqual(decision.action, "none")
        self.assertTrue(
            decision.release_lock,
            "a lock on a healthy pull request is what hid the incident",
        )

    def test_unknown_rung_names_are_ignored_rather_than_trusted(self):
        decision = conflict_repair.evaluate_recovery(
            behind=4, ruled_out=("re-run-the-issue", "update-branch"), attempts=0
        )
        self.assertEqual(decision.action, "replay-commits")

    def test_negative_and_malformed_bounds_fail_closed_to_the_default_budget(self):
        for bad in (-1, "abc", None, True, float("nan")):
            with self.subTest(bad=bad):
                decision = conflict_repair.evaluate_recovery(
                    behind=5, max_attempts=bad, attempts=0
                )
                self.assertIn(decision.action, conflict_repair.RECOVERY_RUNGS)


# --------------------------------------------------------------------------- #
# The budget: a long task must not set its own repair deadline
# --------------------------------------------------------------------------- #


class RepairBudgetTests(unittest.TestCase):
    def test_repair_modes_get_a_small_constant_budget(self):
        for mode in conflict_repair.REPAIR_MODES:
            self.assertEqual(
                conflict_repair.repair_budget_minutes(mode),
                conflict_repair.REPAIR_BUDGET_MINUTES,
            )

    def test_repair_budget_ignores_any_caller_supplied_total(self):
        # runtime-lab #56 ran for ~35 minutes before the source run. If a repair
        # could inherit that, the incident is back.
        for supplied in (1, 60, 180, 360, 100000, "not-a-number", None):
            with self.subTest(supplied=supplied):
                self.assertEqual(
                    conflict_repair.repair_budget_minutes("resolve-conflict", supplied),
                    conflict_repair.REPAIR_BUDGET_MINUTES,
                )

    def test_task_execution_keeps_the_generous_window(self):
        self.assertGreaterEqual(
            conflict_repair.repair_budget_minutes("issue"),
            conflict_repair.TASK_BUDGET_MINUTES,
        )

    def test_the_repair_budget_is_far_smaller_than_the_task_budget(self):
        # A conflict repair is minutes of git. Only if both rungs of git fail
        # does a model get involved at all, and even then not for three hours.
        self.assertLess(
            conflict_repair.REPAIR_BUDGET_MINUTES,
            conflict_repair.TASK_BUDGET_MINUTES,
        )

    def test_the_model_call_is_capped_below_the_job_budget(self):
        # Otherwise a slow model call consumes the whole job and nobody is left
        # to push what it produced or record an outcome.
        seconds = conflict_repair.repair_agent_timeout_seconds()
        self.assertLess(seconds, conflict_repair.REPAIR_BUDGET_MINUTES * 60)
        self.assertGreaterEqual(seconds, 120)

    def test_a_plan_for_a_repair_never_reports_the_task_budget(self):
        plan = conflict_repair.evaluate_plan(mode="resolve-conflict", behind=3)
        self.assertEqual(plan.task_timeout_minutes, conflict_repair.REPAIR_BUDGET_MINUTES)
        self.assertGreater(plan.agent_timeout_seconds, 0)

    def test_a_plan_for_a_task_reports_no_repair_rung(self):
        plan = conflict_repair.evaluate_plan(mode="issue")
        self.assertEqual(plan.action, "")
        self.assertEqual(plan.agent_timeout_seconds, 0)
        self.assertEqual(plan.task_timeout_minutes, conflict_repair.TASK_BUDGET_MINUTES)

    def test_budget_cli_agrees_with_the_policy(self):
        repair = policy_outputs("plan", "--mode", "resolve-conflict", "--behind", "4")
        task = policy_outputs("plan", "--mode", "issue")
        self.assertEqual(
            int(repair["task_timeout_minutes"]), conflict_repair.REPAIR_BUDGET_MINUTES
        )
        self.assertGreaterEqual(
            int(task["task_timeout_minutes"]), conflict_repair.TASK_BUDGET_MINUTES
        )
        self.assertEqual(task["repair_action"], "")


# --------------------------------------------------------------------------- #
# Lock lifecycle: bounded retry, never a permanent verdict
# --------------------------------------------------------------------------- #


class LockLifecycleTests(unittest.TestCase):
    def test_a_concluded_attempt_always_releases_the_lock(self):
        for attempts in range(0, 6):
            with self.subTest(attempts=attempts):
                decision = conflict_repair.evaluate_lock(
                    lock_present=True, attempts=attempts, max_attempts=3
                )
                self.assertTrue(
                    decision.release_lock,
                    "attempt {} left the lock in place".format(attempts),
                )

    def test_only_a_live_run_may_hold_the_lock(self):
        decision = conflict_repair.evaluate_lock(
            lock_present=True, attempts=1, repair_run_in_flight=True
        )
        self.assertTrue(decision.hold_lock)
        self.assertFalse(decision.release_lock)

    def test_an_exhausted_budget_releases_the_lock_and_records_the_failure(self):
        decision = conflict_repair.evaluate_lock(
            lock_present=True, attempts=3, max_attempts=3
        )
        self.assertTrue(decision.release_lock)
        self.assertTrue(decision.record_failure)
        self.assertFalse(decision.freeze_original)

    def test_the_label_alone_can_never_justify_holding_the_lock(self):
        # This is the runtime-lab #66 bug in one line: the old controller
        # treated "the label is present" as a verdict, so a lock that survived a
        # dead run suppressed every later repair forever.
        for in_flight in (False,):
            decision = conflict_repair.evaluate_lock(
                lock_present=True, attempts=0, repair_run_in_flight=in_flight
            )
            self.assertFalse(decision.hold_lock)
            self.assertTrue(decision.release_lock)

    def test_backoff_grows_and_is_capped(self):
        delays = [
            conflict_repair.backoff_delay_seconds(attempt, base=600, cap=7200)
            for attempt in range(1, 8)
        ]
        self.assertEqual(delays, [600, 1200, 2400, 4800, 7200, 7200, 7200])
        self.assertEqual(conflict_repair.backoff_delay_seconds(0), 0)

    def test_a_retry_before_its_backoff_expires_waits_instead_of_dispatching(self):
        decision = conflict_repair.evaluate_recovery(
            behind=3,
            ruled_out=("update-branch",),
            attempts=1,
            seconds_since_attempt=10,
        )
        self.assertEqual(decision.action, "wait")
        self.assertGreater(decision.retry_after_seconds, 0)

    def test_a_retry_after_its_backoff_expires_proceeds(self):
        decision = conflict_repair.evaluate_recovery(
            behind=3,
            ruled_out=("update-branch",),
            attempts=1,
            seconds_since_attempt=100000,
        )
        self.assertEqual(decision.action, "replay-commits")

    def test_recovery_and_lock_release_agree_on_the_exhausted_case(self):
        recovery = conflict_repair.evaluate_recovery(behind=2, attempts=3, max_attempts=3)
        lock = conflict_repair.evaluate_lock(lock_present=True, attempts=3, max_attempts=3)
        self.assertEqual(recovery.action, conflict_repair.FAILED_ACTION)
        self.assertTrue(lock.release_lock)
        self.assertTrue(recovery.record_failure)
        self.assertTrue(lock.record_failure)


# --------------------------------------------------------------------------- #
# Publication: freeze only after a replacement provably exists
# --------------------------------------------------------------------------- #


class PublicationTests(unittest.TestCase):
    def test_a_failed_repair_never_freezes_the_original(self):
        decision = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A, repair_succeeded=False, attempts=1
        )
        self.assertFalse(decision.freeze_original)
        self.assertEqual(decision.code, "no_replacement_exists")

    def test_a_timeout_leaves_the_pull_request_reconcilable(self):
        # A replacement head is exactly what a timeout does not produce.
        decision = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A, replacement_head_sha="", repair_succeeded=False
        )
        self.assertFalse(decision.freeze_original)
        self.assertFalse(decision.record_failure is False)

    def test_a_repair_in_place_does_not_supersede_itself(self):
        decision = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A,
            replacement_head_sha=HEAD_B,
            repair_succeeded=True,
            repaired_in_place=True,
        )
        self.assertEqual(decision.code, "repaired_in_place")
        self.assertFalse(
            decision.freeze_original,
            "a repair that moved the head of its own branch is not a replacement",
        )

    def test_freezing_requires_a_verified_distinct_replacement_head(self):
        decision = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A,
            replacement_head_sha=HEAD_B,
            repair_succeeded=True,
            repaired_in_place=False,
        )
        self.assertEqual(decision.code, "replacement_published")
        self.assertTrue(decision.freeze_original)

    def test_an_unverifiable_replacement_never_freezes_anything(self):
        for candidate in ("", "not-a-sha", "abc123", HEAD_B[:39], "HEAD_B"):
            with self.subTest(candidate=candidate):
                decision = conflict_repair.evaluate_publication(
                    original_head_sha=HEAD_A,
                    replacement_head_sha=candidate,
                    repair_succeeded=True,
                    repaired_in_place=False,
                )
                self.assertFalse(decision.freeze_original, candidate)
                self.assertEqual(decision.code, "unverifiable_replacement")

    def test_freezing_is_transactional_in_both_directions(self):
        # Nothing frozen before a replacement exists; once frozen, the decision
        # is stable and does not flap back to live.
        before = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A, repair_succeeded=False
        )
        self.assertFalse(before.freeze_original)
        after = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A,
            replacement_head_sha=HEAD_B,
            repair_succeeded=True,
            original_frozen=True,
        )
        self.assertEqual(after.code, "already_superseded")
        self.assertTrue(after.freeze_original)


# --------------------------------------------------------------------------- #
# Episode records: the durable evidence the retry budget and watchdog need
# --------------------------------------------------------------------------- #


class EpisodeRecordTests(unittest.TestCase):
    def test_a_marker_round_trips(self):
        text = "some prose\n" + marker(episode=2, pr=66, head=HEAD_B, attempt=3, state="failed")
        fields = conflict_repair.parse_episode_marker(text)
        self.assertEqual(fields["episode"], "2")
        self.assertEqual(fields["pr"], "66")
        self.assertEqual(fields["head"], HEAD_B)
        self.assertEqual(fields["attempt"], "3")
        self.assertEqual(fields["state"], "failed")

    def test_a_marker_refuses_to_render_from_unvalidated_fields(self):
        self.assertEqual(marker(pr=0), "")
        self.assertEqual(marker(attempt=0), "")
        self.assertEqual(marker(head="not-a-sha"), "")
        self.assertEqual(marker(state="maybe"), "")

    def test_attempts_are_counted_per_pull_request(self):
        texts = [
            marker(pr=66, attempt=1),
            marker(pr=66, attempt=2),
            marker(pr=99, attempt=3),
        ]
        self.assertEqual(conflict_repair.episode_attempts(texts, 66), 2)
        self.assertEqual(conflict_repair.episode_attempts(texts, 99), 3)

    def test_a_hostile_comment_cannot_inflate_the_attempt_budget(self):
        # Comments are attacker-influenceable on a public repository, so an
        # attempt counter that trusted them would let a stranger exhaust a pull
        # request's repair budget with one comment -- a denial of repair that
        # needs no write access at all.
        forged = [
            # An attempt count nothing could reach.
            "<!-- continuum-conflict-repair: episode=1 pr=66 attempt=9999 state=open -->",
            # A negative number must not be sanitised into a positive one.
            "<!-- continuum-conflict-repair: episode=1 pr=66 attempt=-5 state=open -->",
            # Digits with anything appended are not a number.
            "<!-- continuum-conflict-repair: episode=1 pr=66 attempt=3x state=open -->",
            # No attempt at all.
            "<!-- continuum-conflict-repair: pr=66 attempt=500 state=open -->",
            # A non-numeric episode number.
            "<!-- continuum-conflict-repair: episode=x pr=66 attempt=3 state=open -->",
            # A state that does not exist.
            "<!-- continuum-conflict-repair: episode=1 pr=66 attempt=3 state=bogus -->",
            # A head that is not a commit id.
            "<!-- continuum-conflict-repair: episode=1 pr=66 head=deadbeef attempt=3 state=open -->",
        ]
        for comment in forged:
            with self.subTest(comment=comment[-60:]):
                self.assertEqual(conflict_repair.episode_attempts([comment], 66), 0)

    def test_a_comment_cannot_claim_attempts_for_another_pull_request(self):
        self.assertEqual(
            conflict_repair.episode_attempts([marker(pr=1, attempt=3)], 66), 0
        )

    def test_the_latest_episode_wins(self):
        texts = [marker(episode=1, attempt=1), marker(episode=2, attempt=1, head=HEAD_C)]
        self.assertEqual(conflict_repair.latest_episode(texts)["head"], HEAD_C)

    def test_text_without_a_marker_yields_no_episode(self):
        self.assertEqual(conflict_repair.parse_episode_marker("just a comment"), {})
        self.assertEqual(conflict_repair.latest_episode(["nothing here"]), {})

    def test_the_episode_cli_reads_a_comment_stream(self):
        stream = "\n".join(
            [
                "first comment",
                marker(pr=66, attempt=1),
                marker(pr=66, attempt=2, state="failed"),
            ]
        )
        outputs = policy_outputs("episode", "--pr-number", "66", stdin=stream + "\n")
        self.assertEqual(outputs["episode_attempts"], "2")
        self.assertEqual(outputs["episode_state"], "failed")
        self.assertEqual(outputs["episode_pr"], "66")

    def test_the_render_marker_cli_emits_a_parseable_marker(self):
        result = run_policy(
            "render-marker",
            "--episode", "1",
            "--pr-number", "66",
            "--head-sha", HEAD_A,
            "--attempt", "1",
            "--state", "resolved",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            conflict_repair.parse_episode_marker(result.stdout)["state"], "resolved"
        )


# --------------------------------------------------------------------------- #
# Watchdog: something must always notice a stale state
# --------------------------------------------------------------------------- #


class WatchdogTests(unittest.TestCase):
    def test_a_live_repair_is_never_declared_stale(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.CONFLICT_LOCK_LABEL],
            attempts=1,
            repair_run_in_flight=True,
            seconds_since_lock=10**7,
        )
        self.assertEqual(decision.action, "none")

    def test_a_fresh_lock_within_its_window_is_left_alone(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.CONFLICT_LOCK_LABEL],
            head_sha=HEAD_A,
            locked_head_sha=HEAD_A,
            attempts=1,
            seconds_since_lock=60,
        )
        self.assertEqual(decision.code, "lock_is_current")

    def test_a_lock_whose_head_moved_is_stale(self):
        # The episode is over by definition: the thing the lock protected has
        # been replaced. This is how a cancelled run's lock gets cleaned up
        # without waiting for the stale window to elapse.
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.CONFLICT_LOCK_LABEL],
            head_sha=HEAD_B,
            locked_head_sha=HEAD_A,
            attempts=1,
        )
        self.assertEqual(decision.action, "release-stale-lock")
        self.assertTrue(decision.release_lock)

    def test_a_lock_nobody_refreshed_past_the_window_is_stale(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.CONFLICT_LOCK_LABEL],
            head_sha=HEAD_A,
            locked_head_sha=HEAD_A,
            attempts=1,
            stale_after_minutes=90,
            seconds_since_lock=91 * 60,
        )
        self.assertEqual(decision.action, "release-stale-lock")

    def test_an_exhausted_budget_is_reported_once(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[],
            attempts=3,
            max_attempts=3,
        )
        self.assertEqual(decision.action, "record-failure")
        self.assertFalse(decision.freeze_original)

        already = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.REPAIR_FAILED_LABEL],
            attempts=3,
            max_attempts=3,
        )
        self.assertNotEqual(already.action, "record-failure")

    def test_a_controller_set_opt_out_is_released_once_the_conflict_is_gone(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.AUTO_MERGE_OPT_OUT_LABEL],
            mergeable_state="clean",
            controller_marked_opt_out=True,
        )
        self.assertEqual(decision.action, "release-stale-opt-out")

    def test_a_humans_opt_out_is_never_touched(self):
        # A human who set `no-auto-merge` has no conflict-repair episode marker,
        # so the watchdog must leave their decision alone.
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.AUTO_MERGE_OPT_OUT_LABEL],
            mergeable_state="clean",
            controller_marked_opt_out=False,
        )
        self.assertEqual(decision.action, "none")

    def test_a_still_dirty_pull_request_keeps_its_opt_out(self):
        decision = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.AUTO_MERGE_OPT_OUT_LABEL],
            mergeable_state="dirty",
            controller_marked_opt_out=True,
        )
        self.assertEqual(decision.action, "none")

    def test_a_clean_pull_request_with_no_repair_state_is_untouched(self):
        decision = conflict_repair.evaluate_watchdog(labels=[], attempts=0)
        self.assertEqual(decision.action, "none")

    def test_the_watchdog_cli_agrees_with_the_policy(self):
        outputs = policy_outputs(
            "watchdog",
            "--labels", "opencode-conflict-repair",
            "--head-sha", HEAD_B,
            "--locked-head-sha", HEAD_A,
            "--attempts", "1",
        )
        self.assertEqual(outputs["action"], "release-stale-lock")
        self.assertEqual(outputs["release_lock"], "true")


# --------------------------------------------------------------------------- #
# Git behaviour: a mechanical replay really does preserve the commits
# --------------------------------------------------------------------------- #


class MechanicalReplayGitTests(unittest.TestCase):
    """The replay rung, exercised against real git.

    A pure function can say "this preserves the commits" all it likes; what
    matters is that the command the workflow runs does. This drives the exact
    sequence from the `Replay the pull request commits onto the base` step.
    """

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _replay(self, workdir, head_ref="opencode/issue56-research"):
        """The rung-2 command sequence, verbatim from the workflow."""
        original = self._git("rev-parse", "HEAD", cwd=workdir)
        self._git("fetch", "origin", "main", "--quiet", cwd=workdir)
        preserved = self._git(
            "rev-list", "--count", "origin/main..{}".format(original), cwd=workdir
        )
        try:
            self._git("rebase", "--rebase-merges", "origin/main", cwd=workdir)
        except subprocess.CalledProcessError:
            self._git("rebase", "--abort", cwd=workdir)
            return {"replayed": False, "original": original, "preserved": preserved}
        self._git(
            "push",
            "--force-with-lease=refs/heads/{}:{}".format(head_ref, original),
            "origin",
            "HEAD:{}".format(head_ref),
            cwd=workdir,
        )
        return {"replayed": True, "original": original, "preserved": preserved}

    def _repository(self, root):
        """A bare origin plus a clone, with a finished change on a branch."""
        origin = root / "origin.git"
        clone = root / "clone"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "clone", str(origin), str(clone)], capture_output=True, check=True
        )
        for key, value in (
            ("user.name", "Continuum Test"),
            ("user.email", "test@example.invalid"),
            ("user.name", "Continuum Test"),
        ):
            self._git("config", key, value, cwd=clone)

        (clone / "research.md").write_text("base result\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-m", "base", cwd=clone)
        self._git("push", "-u", "origin", "main", cwd=clone)

        self._git("checkout", "-b", "opencode/issue56-research", cwd=clone)
        (clone / "report.md").write_text("finding one\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-m", "add finding one", cwd=clone)
        (clone / "report.md").write_text("finding one\nfinding two\n", encoding="utf-8")
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-m", "add finding two", cwd=clone)
        self._git("push", "-u", "origin", "opencode/issue56-research", cwd=clone)
        return origin, clone

    def _advance_main(self, origin, extra_clone=None):
        """Push an unrelated change to main, as another merged pull request would."""
        target = extra_clone or origin.parent / "main-writer"
        if not target.exists():
            subprocess.run(
                ["git", "clone", str(origin), str(target)], capture_output=True, check=True
            )
            self._git("config", "user.name", "Main Writer", cwd=target)
            self._git("config", "user.email", "main@example.invalid", cwd=target)
        (target / "unrelated.md").write_text("shipped elsewhere\n", encoding="utf-8")
        self._git("add", "-A", cwd=target)
        self._git("commit", "-m", "unrelated work", cwd=target)
        self._git("push", "origin", "main", cwd=target)
        return target

    def test_a_replayable_change_is_repaired_with_no_model_and_no_lost_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            self._advance_main(origin)

            before = self._git("log", "--format=%s", "origin/main..HEAD", cwd=clone)
            result = self._replay(clone)

            self.assertTrue(result["replayed"], "an unrelated main advance must replay cleanly")
            self.assertEqual(result["preserved"], "2")

            # The finished change is intact: same subjects, same content.
            self.assertEqual(self._git("log", "--format=%s", "origin/main..HEAD", cwd=clone), before)
            self.assertEqual(
                (clone / "report.md").read_text(encoding="utf-8"),
                "finding one\nfinding two\n",
            )
            # And it now sits on the new base.
            self.assertEqual(self._git("merge-base", "--is-ancestor", "origin/main", "HEAD", cwd=clone), "")
            self.assertIn("unrelated.md", os.listdir(str(clone)))

            # The pull request's branch moved, and the old head is reachable as
            # the lease, so the push was a compare-and-swap rather than a
            # blind force push.
            remote = self._git("rev-parse", "opencode/issue56-research", cwd=origin)
            self.assertNotEqual(remote, result["original"])
            self.assertEqual(remote, self._git("rev-parse", "HEAD", cwd=clone))

    def test_a_genuinely_conflicting_change_survives_the_failed_replay_intact(self):
        # When the replay cannot finish, the finished change must still be in
        # the worktree so the hunk rung has something to work with. A rung that
        # half-applied and then reset would throw away the completed work.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            main_writer = self._advance_main(origin)

            # main rewrites the same line the change added.
            (main_writer / "report.md").write_text("finding one\nmain rewrote this\n", encoding="utf-8")
            self._git("add", "-A", cwd=main_writer)
            self._git("commit", "-m", "main touches the same line", cwd=main_writer)
            self._git("push", "origin", "main", cwd=main_writer)

            self._git("fetch", "origin", "--quiet", cwd=clone)
            result = self._replay(clone)

            self.assertFalse(result["replayed"], "a real conflict must stop the replay")
            self.assertEqual(result["preserved"], "2")
            # The abort restored the finished change, not an empty tree.
            self.assertEqual(
                self._git("log", "--format=%s", "origin/main..HEAD", cwd=clone),
                "add finding two\nadd finding one",
            )
            self.assertEqual(
                (clone / "report.md").read_text(encoding="utf-8"),
                "finding one\nfinding two\n",
            )
            # And the remote branch was never touched by the failed attempt.
            self.assertEqual(
                self._git("rev-parse", "opencode/issue56-research", cwd=origin),
                result["original"],
            )

    def test_a_stale_lease_refuses_the_push_instead_of_overwriting(self):
        # `--force-with-lease` against a head that is no longer current must
        # fail. Without this, a repair that took a few minutes could silently
        # discard a commit somebody pushed while it was working.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            origin, clone = self._repository(root)
            self._advance_main(origin)

            stale_original = self._git("rev-parse", "HEAD", cwd=clone)
            # Somebody else pushes to the same branch first.
            other = root / "other"
            subprocess.run(
                ["git", "clone", str(origin), str(other)], capture_output=True, check=True
            )
            for key, value in (
                ("user.name", "Other"),
                ("user.email", "other@example.invalid"),
            ):
                self._git("config", key, value, cwd=other)
            self._git("fetch", "origin", "--quiet", cwd=other)
            self._git("checkout", "opencode/issue56-research", cwd=other)
            (other / "extra.md").write_text("concurrent\n", encoding="utf-8")
            self._git("add", "-A", cwd=other)
            self._git("commit", "-m", "concurrent commit", cwd=other)
            self._git("push", "origin", "opencode/issue56-research", cwd=other)

            self._git("fetch", "origin", "main", "--quiet", cwd=clone)
            self._git("rebase", "--rebase-merges", "origin/main", cwd=clone)
            with self.assertRaises(subprocess.CalledProcessError):
                self._git(
                    "push",
                    "--force-with-lease=refs/heads/opencode/issue56-research:{}".format(
                        stale_original
                    ),
                    "origin",
                    "HEAD:opencode/issue56-research",
                    cwd=clone,
                )
            # The concurrent commit survived: the repair's push was refused, and
            # the ref on the remote is still the other person's work.
            remote_log = self._git(
                "log", "--format=%s", "-n", "5", "opencode/issue56-research", cwd=origin
            )
            self.assertIn("concurrent commit", remote_log)


# --------------------------------------------------------------------------- #
# Workflow wiring: the policy has to actually be called
# --------------------------------------------------------------------------- #


class WorkflowWiringTests(unittest.TestCase):
    """The rung order is only real if the workflows follow it."""

    def test_the_agent_workflow_replays_before_it_asks_a_model(self):
        text = workflow_text(AGENT_WORKFLOW)
        replay = text.index("Replay the pull request commits onto the base")
        hunks = text.index("Resolve the conflicting hunks")
        publish = text.index("Publish the conflict-repair outcome")
        self.assertLess(replay, hunks, "mechanical replay must precede the model rung")
        self.assertLess(hunks, publish)

    def test_the_replay_rung_uses_a_rebase_with_a_lease_protected_push(self):
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn("git rebase --rebase-merges", text)
        self.assertIn("--force-with-lease=", text)
        self.assertIn("git rebase --abort", text)

    def test_the_model_rung_is_capped_and_merges_rather_than_restarts(self):
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn("REPAIR_AGENT_TIMEOUT_SECONDS", text)
        self.assertIn("timeout --signal=TERM", text)
        self.assertIn(
            "git merge --no-edit",
            text,
            "a merge git can finish must not call a model",
        )
        # The prompt has to tell the model this is a repair, or the most likely
        # outcome of a confusing conflict is a redesign.
        self.assertIn("already finished, not a new task", text)
        self.assertIn("do not start over from the base branch", text.lower())

    def test_the_agent_workflow_never_dispatches_itself_in_issue_mode(self):
        # The original incident was a conflict triggering the issue lifecycle.
        # A repair mode must be structurally unable to reach the issue path.
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn("github.event_name != 'workflow_dispatch'", text)
        for step_gated in ("Run OpenCode",):
            self.assertIn(step_gated, text)
        # The issue-mode agent step is not reachable from a dispatch, and the
        # repair steps are not reachable from a comment.
        self.assertIn("needs.authorize.outputs.repair_action == 'replay-commits'", text)
        self.assertIn("needs.authorize.outputs.repair_action == 'resolve-hunks'", text)

    def test_the_agent_job_timeout_is_a_policy_output_not_a_literal(self):
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn(
            "timeout-minutes: ${{ needs.authorize.outputs.task_timeout_minutes }}", text
        )
        self.assertNotIn("timeout-minutes: 180", text)

    def test_the_controller_uses_the_ladder_instead_of_the_label_as_a_verdict(self):
        text = workflow_text(REPAIR_WORKFLOW)
        # The conflict path is the `sync-current-pr` job. `ci-repair` is a
        # different episode type with different, legitimately label-gated
        # semantics, so this assertion is scoped rather than repository-wide.
        conflict_path = text[text.index("  sync-current-pr:") : text.index("  ci-repair:")]
        self.assertIn("conflict_repair.py recovery-plan", conflict_path)
        self.assertIn("--ruled-out update-branch", conflict_path)
        self.assertIn("conflict_repair.py episode", conflict_path)
        self.assertIn("conflict_repair.py render-marker", conflict_path)
        self.assertNotIn(
            "reason=repair_already_attempted",
            conflict_path,
            "a present label must no longer be treated as a finished repair",
        )
        self.assertNotIn(
            "conflict repair is already reserved",
            conflict_path,
            "a stuck lock must not silently decline the pull request forever",
        )
        # The same rule applies to the sweep that runs after main advances.
        sweep = text[text.index("  sync-stale-prs:") : text.index("  sync-current-pr:")]
        self.assertIn("conflict_repair.py recovery-plan", sweep)
        self.assertIn("conflict_repair.py episode", sweep)
        self.assertIn("inputs[rungs_ruled_out]=update-branch", sweep)
        self.assertNotIn("already reserved", sweep)

    def test_no_decision_path_writes_action_twice(self):
        # `GITHUB_OUTPUT` keeps the *last* value for a repeated key, so a step
        # that writes `action` twice on one path silently discards the first.
        # `reset-head-locks` was exactly that bug: it was written first and then
        # overwritten by the ladder's own decision, so the per-head lock reset
        # never ran. Several mutually exclusive writes are fine; two reachable
        # without an `exit` between them are not.
        text = workflow_text(REPAIR_WORKFLOW)
        checked = 0
        for job in ("sync-current-pr", "sync-stale-prs", "ci-repair"):
            start = text.index("  {}:".format(job))
            end = text.find("\n  [a-z]", start + 4)
            body = text[start : end if end != -1 else len(text)]
            for step_name, step_body in re.findall(
                r"- name: ([^\n]+)\n(.*?)(?=\n      - name:|\n  [a-z]|\Z)",
                body,
                re.S,
            ):
                for segment in re.split(r"\bexit\b", step_body):
                    writes = re.findall(r'echo "action=([a-z-]+)"', segment)
                    if not writes:
                        continue
                    checked += 1
                    self.assertEqual(
                        len(writes),
                        1,
                        "{} / {} writes {} actions on one path: {}".format(
                            job, step_name, len(writes), writes
                        ),
                    )
        self.assertGreater(checked, 5, "the scan found no decision paths at all")

    def test_the_dispatch_declares_the_ladder_position_within_the_allowlist(self):
        text = workflow_text(REPAIR_WORKFLOW)
        self.assertIn("inputs[rungs_ruled_out]=update-branch", text)
        self.assertIn("--rungs-ruled-out update-branch", text)

    def test_the_outcome_step_runs_even_when_the_repair_fails(self):
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn(
            "if: always() && github.event_name == 'workflow_dispatch' "
            "&& contains(fromJSON('[\"replay-commits\",\"resolve-hunks\"]'), "
            "needs.authorize.outputs.repair_action)",
            text,
        )

    def test_the_outcome_step_releases_the_lock_and_gates_the_opt_out(self):
        text = workflow_text(AGENT_WORKFLOW)
        self.assertIn("conflict_repair.py publication-gate", text)
        self.assertIn("conflict_repair.py lock", text)
        self.assertIn("labels/opencode-conflict-repair", text)
        # `no-auto-merge` may only be added behind the publication gate, and
        # only after the gate has seen a replacement head.
        publish = text[text.index("Publish the conflict-repair outcome") :]
        self.assertLess(
            publish.index("freeze_original=true"),
            publish.index("labels[]=no-auto-merge"),
        )
        # The gate itself refuses to freeze without one.
        for decision in (
            conflict_repair.evaluate_publication(
                original_head_sha=HEAD_A, repair_succeeded=False
            ),
            conflict_repair.evaluate_publication(
                original_head_sha=HEAD_A, replacement_head_sha="", repair_succeeded=True
            ),
        ):
            self.assertFalse(decision.freeze_original)

    def test_the_watchdog_is_scheduled_and_can_only_release_state(self):
        text = workflow_text(REPAIR_WORKFLOW)
        self.assertIn("schedule:", text)
        self.assertIn("repair-watchdog:", text)
        self.assertIn("conflict_repair.py watchdog", text)
        self.assertIn("continuum-conflict-repair", text)
        # A watchdog that could dispatch would be a new privileged surface.
        watchdog = text[text.index("repair-watchdog:") :]
        self.assertNotIn("/dispatches", watchdog)
        self.assertNotIn("git push", watchdog)
        self.assertNotIn("pulls/merge", watchdog)
        self.assertNotIn("pr create", watchdog)

    def test_the_watchdog_only_releases_an_opt_out_it_owns(self):
        text = workflow_text(REPAIR_WORKFLOW)
        self.assertIn("--controller-marked-opt-out", text)
        self.assertIn("grep -q 'continuum-conflict-repair'", text)

    def test_the_consumer_plane_gets_the_same_treatment(self):
        agent = workflow_text(CONSUMER_AGENT_WORKFLOW)
        self.assertLess(
            agent.index("Replay the pull request commits onto main"),
            agent.index("Resolve the conflicting hunks"),
        )
        self.assertIn("Record the conflict-repair outcome", agent)
        self.assertIn("inputs[repair_timeout_minutes]", workflow_text(CONSUMER_REPAIR_WORKFLOW))
        self.assertIn("inputs[rungs_ruled_out]=update-branch", workflow_text(CONSUMER_REPAIR_WORKFLOW))

    def test_the_ruled_out_rungs_are_a_closed_allowlist(self):
        # A payload must be able to skip the free mechanical rungs and nothing
        # else. If this list ever grows a rung that re-runs the task, the
        # invariant that makes a conflict a repair is gone.
        self.assertEqual(
            trust_policy.RULABLE_OUT_RUNGS, ("update-branch", "replay-commits")
        )
        for rung in trust_policy.RULABLE_OUT_RUNGS:
            self.assertIn(rung, conflict_repair.RECOVERY_RUNGS)

    def test_the_dispatch_shape_rejects_any_other_rung(self):
        base = {
            "mode": "resolve-conflict",
            "pr_number": "66",
            "head_ref": "opencode/issue56-research",
        }
        for value in (
            "issue",
            "re-run-the-issue",
            "update-branch,issue",
            "UPDATE-BRANCH",
            "update-branch,",
            ",update-branch",
            ["update-branch"],
            "update-branch replay-commits",
        ):
            with self.subTest(value=value):
                decision = trust_policy.validate_dispatch_shape(
                    dict(base, rungs_ruled_out=value), repository="kodmial/continuum"
                )
                self.assertFalse(decision.allowed, value)
                self.assertEqual(decision.code, "untrusted_ruled_out_rungs")

    def test_the_dispatch_shape_accepts_the_mechanical_rungs(self):
        base = {
            "mode": "resolve-conflict",
            "pr_number": "66",
            "head_ref": "opencode/issue56-research",
        }
        for value in (None, "", "update-branch", "replay-commits", "replay-commits,update-branch"):
            with self.subTest(value=value):
                decision = trust_policy.validate_dispatch_shape(
                    dict(base, rungs_ruled_out=value), repository="kodmial/continuum"
                )
                self.assertTrue(decision.allowed, value)

    def test_the_ruled_out_rungs_survive_into_the_verified_dispatch(self):
        decision = trust_policy.validate_dispatch_shape(
            {
                "mode": "resolve-conflict",
                "pr_number": "66",
                "head_ref": "opencode/issue56-research",
                "rungs_ruled_out": "update-branch,replay-commits",
            },
            repository="kodmial/continuum",
        )
        self.assertEqual(
            decision.ruled_out_rungs, ("update-branch", "replay-commits")
        )
        self.assertIn(
            "update-branch,replay-commits", decision.as_outputs()["trust_ruled_out_rungs"]
        )


# --------------------------------------------------------------------------- #
# The publication step itself, executed as a shell script
# --------------------------------------------------------------------------- #


def _step_run_block(workflow, step_name):
    """The literal `run:` body of a named step, as the runner would see it."""
    doc = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == step_name:
                return step["run"], step
    raise AssertionError("no step named {!r} in {}".format(step_name, workflow))


class _FakeGh:
    """A `gh` stand-in that records the calls a step makes.

    The publication step's whole job is to make writes *conditional*, so a test
    that only inspects the shell source is testing the wrong thing. Running it
    and looking at which calls happened is the only way to know that a failed
    repair does not opt the pull request out of merging.
    """

    def __init__(self, directory):
        self.log = pathlib.Path(directory) / "gh-calls.log"
        self.log.write_text("", encoding="utf-8")
        script = pathlib.Path(directory) / "gh"
        script.write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                # Record the shape of the call, then succeed. Flags that take a
                # value are recorded too, so an assertion can look for the
                # label an API call carried rather than only its path.
                method="GET"; args=()
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --method) method="$2"; shift 2 ;;
                    -*) args+=("$1"); shift ;;
                    *) args+=("$1"); shift ;;
                  esac
                done
                printf '%s | %s\\n' "$method" "${args[*]}" >> "$GH_CALL_LOG"
                # Keep any piped body: the outcome comment is where the pull
                # request is told what happened, and a test that cannot read it
                # cannot check that it says the right thing.
                cat >> "$GH_CALL_LOG.stdin" 2>/dev/null || true
                exit 0
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)

    def calls(self):
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def bodies(self):
        path = pathlib.Path(str(self.log) + ".stdin")
        return path.read_text(encoding="utf-8") if path.exists() else ""


class RepairRunShellTests(unittest.TestCase):
    """Run the real `Publish the conflict-repair outcome` step as a shell script.

    Every other test in this file checks a decision function. This one checks
    that the workflow actually acts on the decision, because on runtime-lab #66
    the policy was fine and the outcome step did not exist: nothing released
    the lock, nothing recorded the episode, and the pull request stayed frozen.
    """

    def _run_publish(self, outcome, head, original_head=HEAD_A, attempt=2):
        run, step = _step_run_block(AGENT_WORKFLOW, "Publish the conflict-repair outcome")
        self.assertIn("conflict_repair.py publication-gate", run)
        self.assertIn("conflict_repair.py lock", run)

        with tempfile.TemporaryDirectory() as directory:
            gh = _FakeGh(directory)
            environment = dict(os.environ)
            environment.update(
                {
                    "PATH": directory + os.pathsep + os.environ["PATH"],
                    "GH_CALL_LOG": str(gh.log),
                    "GITHUB_REPOSITORY": "kodmial/continuum",
                    "GH_TOKEN": "not-a-real-token",
                    "RUNNER_TEMP": directory,
                    "PR_NUMBER": "66",
                    "HEAD_REF": "opencode/issue56-research",
                    "REPAIR_EPISODE": str(attempt),
                    "REPAIR_ATTEMPT": str(attempt),
                    "MAX_ATTEMPTS": "3",
                    "REPAIR_ACTION": "resolve-hunks",
                    "REPAIR_OUTCOME": outcome,
                    "REPAIR_HEAD": head,
                    "CONFLICT_ORIGINAL_HEAD": original_head,
                    "CONFLICT_PRESERVED_COMMITS": "2",
                }
            )
            for name, value in (step.get("env") or {}).items():
                # The step's own env block is an expression template; the
                # subprocess environment above is the resolved form.
                environment.setdefault(name, "")
            result = subprocess.run(
                ["bash", "-c", run],
                cwd=str(REPO_ROOT),
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode, 0, "publish step failed:\n{}\n{}".format(result.stdout, result.stderr)
            )
            return gh.calls(), result.stdout, gh.bodies()

    def test_a_failed_repair_releases_the_lock_and_never_opts_the_pr_out(self):
        calls, _output, comment_body = self._run_publish(
            outcome="hunks_delegated", head="", attempt=2
        )
        joined = "\n".join(calls)
        # The lock goes, unconditionally.
        self.assertIn(LOCK_RELEASE, joined)
        # The opt-out does not: there is no replacement head to justify it.
        self.assertNotIn("no-auto-merge", joined)
        # The failure is recorded, and the episode is written for the watchdog.
        self.assertIn(REPAIR_FAILED_LABEL, joined)
        self.assertIn(OUTCOME_COMMENT, "\n".join(calls))
        self.assertIn("source issue was not re-executed", comment_body)

    def test_a_timed_out_repair_leaves_the_pull_request_fully_reconcilable(self):
        # The incident's step 6: the repair hit the workflow timeout. Nothing
        # after it ran, so the step never started either. A step that never
        # started must still be harmless.
        calls, _, _ = self._run_publish(outcome="not_started", head="", attempt=1)
        joined = "\n".join(calls)
        self.assertNotIn("no-auto-merge", joined)
        self.assertIn(LOCK_RELEASE, joined)

    def test_a_repair_that_landed_in_place_does_not_supersede_itself(self):
        calls, _, _ = self._run_publish(
            outcome="merged_automatically", head=HEAD_B, original_head=HEAD_A
        )
        joined = "\n".join(calls)
        self.assertNotIn("no-auto-merge", joined)
        self.assertIn(LOCK_RELEASE, joined)

    def test_the_outcome_step_never_opts_the_pull_request_out(self):
        # This design repairs the change on its own branch, so the pull request
        # *is* the repaired change and is never superseded. The step used to add
        # `no-auto-merge` whenever the head had moved -- that is every
        # successful repair -- which froze the pull request the repair had just
        # fixed and left the watchdog to undo it.
        for outcome, head in (
            ("replayed", HEAD_B),
            ("merged_automatically", HEAD_B),
            ("hunks_resolved", HEAD_C),
        ):
            with self.subTest(outcome=outcome):
                calls, _, _ = self._run_publish(outcome=outcome, head=head)
                self.assertNotIn(
                    OPT_OUT_LABEL,
                    "\n".join(calls),
                    "{} opted the repaired pull request out of merging".format(outcome),
                )

    def test_a_genuine_replacement_would_still_freeze(self):
        # The policy keeps the ability to supersede, for a path that really
        # does publish a different pull request.
        decision = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A,
            replacement_head_sha=HEAD_B,
            repair_succeeded=True,
            repaired_in_place=False,
        )
        self.assertTrue(decision.freeze_original)

    def test_a_lock_is_never_left_behind_whatever_the_outcome(self):
        for outcome, head in (
            ("replayed", HEAD_B),
            ("merged_automatically", HEAD_B),
            ("hunks_resolved", HEAD_C),
            ("hunks_delegated", ""),
            ("replay_conflicted", ""),
            ("not_started", ""),
            ("no_commits_to_replay", ""),
        ):
            with self.subTest(outcome=outcome):
                calls, _, _ = self._run_publish(outcome=outcome, head=head)
                self.assertIn(
                    LOCK_RELEASE,
                    "\n".join(calls),
                    outcome,
                )


# --------------------------------------------------------------------------- #
# End-to-end: the exact incident, start to finish
# --------------------------------------------------------------------------- #


class RuntimeLab66RegressionTests(unittest.TestCase):
    """Replay the incident as a sequence of controller and agent decisions.

    The unit tests above pin each property. This one walks the whole episode
    the way the repository did on 2026-09-28 and asserts the outcome is the
    opposite of what happened: the finished change is repaired, and the pull
    request is never stranded.
    """

    def _controller_plan(self, behind, attempts, rungs=("update-branch",), **kwargs):
        return conflict_repair.evaluate_recovery(
            behind=behind, ruled_out=rungs, attempts=attempts, **kwargs
        )

    def test_a_completed_pull_request_is_repaired_without_rerunning_the_issue(self):
        # 1. main advanced and PR #66 became non-mergeable.
        plan = self._controller_plan(behind=12, attempts=0)
        self.assertEqual(plan.action, "replay-commits")

        # 2. The agent replays mechanically: no model, existing commits kept.
        # 3. Replay is impossible here, so the conflicting hunks are delegated.
        #    Either way the source issue is never re-executed.
        for ruled_out in (("update-branch",), ("update-branch", "replay-commits")):
            with self.subTest(ruled_out=ruled_out):
                rung = self._controller_plan(behind=12, attempts=0, rungs=ruled_out)
                self.assertIn(rung.action, ("replay-commits", "resolve-hunks"))
                self.assertNotIn("issue", rung.action)

        # 4. The repair's own budget, not the source task's 35 minutes.
        budget = conflict_repair.evaluate_plan(
            mode="resolve-conflict", behind=12, ruled_out=("update-branch", "replay-commits")
        )
        self.assertEqual(budget.task_timeout_minutes, conflict_repair.REPAIR_BUDGET_MINUTES)

    def test_a_timed_out_repair_does_not_strand_the_pull_request(self):
        # Step 6 of the incident: the replacement run hit the workflow timeout.
        # What must be true afterwards is the opposite of steps 7 and 8.
        timeout_outcome = "hunks_started"  # the agent was killed mid-flight

        lock = conflict_repair.evaluate_lock(lock_present=True, attempts=1, max_attempts=3)
        self.assertTrue(lock.release_lock, "the lock outlived its attempt")

        publication = conflict_repair.evaluate_publication(
            original_head_sha=HEAD_A, repair_succeeded=timeout_outcome.endswith("resolved")
        )
        self.assertFalse(publication.freeze_original, "no-auto-merge must not be published")

        # A further reconciliation must still see the pull request as
        # repairable, because nothing opted it out.
        next_plan = self._controller_plan(behind=12, attempts=1, seconds_since_attempt=10**6)
        self.assertIn(next_plan.action, conflict_repair.RECOVERY_RUNGS)
        self.assertTrue(next_plan.retryable)

    def test_the_retry_budget_eventually_reports_instead_of_looping_forever(self):
        # Each dispatch rules out the rung the previous one proved it could not
        # finish, so the episode advances through the ladder and then stops.
        seen = []
        ruled_out = ["update-branch"]
        for attempt in range(1, 5):
            plan = self._controller_plan(
                behind=12,
                attempts=attempt,
                rungs=tuple(ruled_out),
                seconds_since_attempt=10**6,
            )
            seen.append((attempt, plan.action))
            if plan.action in conflict_repair.RECOVERY_RUNGS:
                ruled_out.append(plan.action)
        self.assertEqual(
            seen,
            [
                (1, "replay-commits"),
                (2, "resolve-hunks"),
                (3, conflict_repair.FAILED_ACTION),
                (4, conflict_repair.FAILED_ACTION),
            ],
        )

    def test_the_watchdog_recovers_a_pull_request_the_controller_abandoned(self):
        # Step 8 of the incident: the pull request stayed frozen and every
        # later reconciliation skipped it. The watchdog is what ends that.
        decision = conflict_repair.evaluate_watchdog(
            labels=[
                conflict_repair.CONFLICT_LOCK_LABEL,
                conflict_repair.AUTO_MERGE_OPT_OUT_LABEL,
            ],
            head_sha=HEAD_A,
            locked_head_sha=HEAD_A,
            attempts=1,
            stale_after_minutes=90,
            seconds_since_lock=91 * 60,
            mergeable_state="clean",
            controller_marked_opt_out=True,
        )
        self.assertEqual(decision.action, "release-stale-lock")
        self.assertTrue(decision.release_lock)

        # Once the lock is gone, the opt-out becomes releasable too, because the
        # conflict is no longer there. Both are required for the merge
        # controller to consider the pull request again.
        after_lock = conflict_repair.evaluate_watchdog(
            labels=[conflict_repair.AUTO_MERGE_OPT_OUT_LABEL],
            mergeable_state="clean",
            controller_marked_opt_out=True,
        )
        self.assertEqual(after_lock.action, "release-stale-opt-out")

    def test_the_whole_episode_converges_to_a_mergeable_pull_request(self):
        # No label, no lock, and no un-reconciled state is left behind by the
        # time every rung has been tried.
        for action, labels in (
            ("replayed", [conflict_repair.CONFLICT_LOCK_LABEL]),
            (conflict_repair.FAILED_ACTION, [conflict_repair.CONFLICT_LOCK_LABEL]),
        ):
            with self.subTest(action=action):
                lock = conflict_repair.evaluate_lock(lock_present=True, attempts=3, max_attempts=3)
                self.assertTrue(lock.release_lock)
                state = conflict_repair.evaluate_watchdog(
                    labels=[name for name in labels if name != lock_label_of(lock)],
                    attempts=3,
                    max_attempts=3,
                )
                self.assertIn(state.action, ("none", "record-failure"))
                # Never an opt-out: the merge controller keeps seeing the PR.
                publication = conflict_repair.evaluate_publication(
                    original_head_sha=HEAD_A, repair_succeeded=action == "replayed"
                )
                if action == "replayed":
                    self.assertFalse(publication.freeze_original)
                else:
                    self.assertFalse(publication.freeze_original)


def lock_label_of(lock_decision):
    """The label a lock decision would leave behind (always none: it releases)."""
    return "__none__"


if __name__ == "__main__":
    unittest.main()
