"""The migration controller: what it reads, what it writes, and what it refuses.

Organised by the rules rather than by module, because the rules are what a change
to this package could break. Four groups:

*Reading* -- the inventory has to tell a repository with no cutover record from a
repository whose record it failed to see, or it will overwrite the only local copy
of where a cutover can be undone to.

*Planning* -- the atomic change, the phase boundary, and the single-writer rule.
Most of these tests are the negative ones: a plan that is one file too permissive
is the failure mode this whole cutover exists to prevent, and a test suite that
only checks the happy path would not notice it.

*Applying* -- the reconciliation. Exact heads, one commit, one pull request, and
a re-run that converges rather than starting again.

*Rolling back* -- restoring the recorded revision in one change, refusing to do it
half-way, and being a no-op the second time.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from continuum.migrate import apply as apply_module
from continuum.migrate import cli as cli_module
from continuum.migrate import client as client_module
from continuum.review import github as github_module
from continuum.migrate import inventory as inventory_module
from continuum.migrate import plan as plan_module
from continuum.migrate import preflight as preflight_module
from continuum.migrate import rollback as rollback_module
from continuum.migrate.client import MERGE_METHOD
from continuum.shadow import baseline
from continuum.shadow import cutover as cutover_module
from continuum.shadow.cutover import PHASE_A, PHASE_B
from tests import migrate_support as fixtures

CANDIDATE = "a" * 40
HEAD = "b" * 40


def _profile(**overrides):
    options = {
        "required_ci": ("CI",),
        "additional_gates": ("Packaging smoke",),
        "release_workflow": ".github/workflows/release.yml",
    }
    options.update(overrides)
    return plan_module.ConsumerProfile(**options)


def _reading(client, ledger=None):
    return inventory_module.capture_inventory(
        client, repository=client.repository, ledger=ledger
    )


def _plan(reading, **overrides):
    options = {"phase": PHASE_A, "candidate": CANDIDATE}
    options.update(overrides)
    return plan_module.build_plan(reading, _profile(), **options)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


class InventoryReadsTest(unittest.TestCase):
    def test_classifies_every_workflow_from_the_reviewed_ledger(self):
        client = fixtures.fake_for_nanodictate()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        self.assertTrue(reading.complete, reading.limits)
        self.assertEqual(reading.head_sha, HEAD)
        by_path = reading.workflow_map
        self.assertEqual(
            by_path[".github/workflows/issue-scheduler.yml"].kind,
            inventory_module.KIND_LEGACY_WRITER,
        )
        self.assertEqual(
            by_path[".github/workflows/ci.yml"].kind, inventory_module.KIND_PRODUCT
        )
        self.assertEqual(by_path[".github/workflows/ci.yml"].display_name, "CI")

    def test_records_secret_names_and_never_values(self):
        client = fixtures.fake_for_nanodictate()
        reading = _reading(client)

        self.assertIn("TAP_PAT", reading.secrets)
        self.assertTrue(all(value is True for value in reading.secrets.values()))
        self.assertEqual(
            reading.variables["OPENCODE_MODEL"],
            "opencode/muse-spark-1.3-contributor-free",
        )
        # The whole document, serialised, must not contain anything that looks
        # like a credential. A value that reached here would be a new leak.
        serialised = json.dumps(reading.describe())
        self.assertNotIn("BEGIN CERTIFICATE", serialised)
        self.assertNotIn("PRIVATE KEY", serialised)

    def test_a_failed_read_is_a_limit_and_not_an_absence(self):
        """The distinction ``read_file_at_ref`` exists to preserve.

        A 500 on the cutover record must not read as "this repository has never
        been cut over" -- the plan would then overwrite the rollback target with
        a fresh one and the recorded revision would be gone for good.
        """

        client = fixtures.fake_for_nanodictate(read_failures=["continuum-cutover"])
        reading = _reading(client)

        self.assertFalse(reading.complete)
        self.assertEqual(reading.record_blob_sha, "")
        self.assertTrue(
            any("continuum-cutover.json" in limit for limit in reading.limits), reading.limits
        )

    def test_an_absent_record_is_not_a_limit(self):
        client = fixtures.fake_for_nanodictate()
        reading = _reading(client)

        self.assertTrue(reading.complete, reading.limits)
        self.assertEqual(reading.record_blob_sha, "")

    def test_a_read_method_outside_the_allowlist_is_refused(self):
        """The value of a read allowlist is that a *write* is not on it.

        Asked for directly rather than through ``capture_inventory``, because the
        inventory never asks for it -- and that is the point being tested. The
        guarantee is that adding a write method to the client cannot make the
        inventory write, which has to be checkable without a scenario that
        happens to call it.
        """

        client = fixtures.fake_for_nanodictate()
        with self.assertRaises(inventory_module.MigrationError) as caught:
            inventory_module._read(client, "create_commit", "message", "tree", [])

        self.assertEqual(caught.exception.code, "write_capable_read")
        self.assertEqual(client.calls, [])

    def test_floating_pin_survives_the_reading_so_a_preflight_can_refuse_it(self):
        client = fixtures.fake_for_nanodictate()
        _rewrite(
            client,
            ".github/workflows/continuum-scheduler.yml",
            "name: Continuum scheduler\non: {schedule: [{cron: '*/15 * * * *'}]}\n"
            "jobs:\n  s:\n    uses: kodmial/continuum/.github/workflows/"
            "consumer-scheduler.yml@main\n",
        )
        reading = _reading(client)

        pinned = reading.workflow_map[".github/workflows/continuum-scheduler.yml"]
        self.assertEqual(pinned.kind, inventory_module.KIND_CONTINUUM_CALLER)
        self.assertFalse(pinned.pins[0].immutable)
        self.assertEqual(len(reading.floating_pins), 1)

    def test_the_digest_moves_when_the_head_moves(self):
        client = fixtures.fake_for_nanodictate()
        first = _reading(client)
        client.head_sha = "c" * 40
        second = _reading(client)

        self.assertNotEqual(first.digest, second.digest)

    def test_a_round_trip_through_the_document_preserves_the_reading(self):
        reading = _reading(
            fixtures.fake_for_nanodictate(),
            fixtures.baseline.read_ledger(fixtures.ledger_document()),
        )
        restored = inventory_module.read_inventory(json.loads(json.dumps(reading.describe())))

        self.assertEqual(restored.digest, reading.digest)
        self.assertEqual(restored.head_sha, reading.head_sha)
        self.assertEqual(
            {entry.path for entry in restored.workflows},
            {entry.path for entry in restored.workflows},
        )


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


class PreflightTest(unittest.TestCase):
    def _verdict(self, client, ledger=None, **kwargs):
        options = {"candidate": CANDIDATE, "ledger": ledger}
        options.update(kwargs)
        return preflight_module.evaluate(_reading(client, ledger), **options)

    def test_ready_when_the_candidate_is_immutable_and_the_secrets_are_present(self):
        verdict = self._verdict(
            fixtures.fake_for_nanodictate(),
            fixtures.baseline.read_ledger(fixtures.ledger_document()),
        )

        self.assertTrue(verdict.ready, preflight_module.summarize(verdict))
        self.assertEqual(verdict.verdict, preflight_module.READY)
        self.assertEqual(verdict.reasons, ())

    def test_a_branch_is_not_a_revision(self):
        verdict = self._verdict(fixtures.fake_for_nanodictate(), candidate="main")

        self.assertFalse(verdict.ready)
        self.assertEqual(verdict.verdict, preflight_module.NOT_READY)
        self.assertEqual(verdict.reasons[0].code, "candidate_not_immutable")

    def test_a_missing_secret_blocks_the_phase_that_needs_it(self):
        # ``review: true`` is what makes TAP_PAT required, and the fixture's
        # config says so. Removing the secret is the whole failure.
        client = fixtures.fake_for_nanodictate(secret_names=[])
        verdict = self._verdict(
            client, fixtures.baseline.read_ledger(fixtures.ledger_document())
        )

        self.assertFalse(verdict.ready)
        codes = [reason.code for reason in verdict.reasons]
        self.assertIn("missing_secret", codes)

    def test_release_secrets_do_not_block_phase_a(self):
        """Phase A does not replace the release writers, so it must not demand
        their credentials. A preflight that blocked here would make the phases
        sequential in a way #28 did not ask for."""

        client = fixtures.fake_for_nanodictate(
            secret_names=["TAP_PAT", "NANODICTATE_SIGNING_P12", "NANODICTATE_SIGNING_PASSWORD"]
        )
        without_release = fixtures.fake_for_nanodictate(secret_names=["TAP_PAT"])

        self.assertTrue(
            self._verdict(
                client, fixtures.baseline.read_ledger(fixtures.ledger_document())
            ).ready
        )
        self.assertTrue(
            self._verdict(
                without_release, fixtures.baseline.read_ledger(fixtures.ledger_document())
            ).ready
        )

    def test_an_incomplete_inventory_never_reports_ready(self):
        client = fixtures.fake_for_nanodictate(read_failures=["continuum.yml"])
        verdict = self._verdict(
            client, fixtures.baseline.read_ledger(fixtures.ledger_document())
        )

        self.assertFalse(verdict.ready)
        self.assertIn("inventory_incomplete", [reason.code for reason in verdict.reasons])

    def test_a_ledger_for_another_repository_is_refused(self):
        other = fixtures.ledger_document(repository="someone/else")
        verdict = self._verdict(
            fixtures.fake_for_nanodictate(), fixtures.baseline.read_ledger(other)
        )

        self.assertFalse(verdict.ready)
        self.assertIn("ledger_repository_mismatch", [r.code for r in verdict.reasons])

    def test_a_workflow_that_moved_since_the_audit_blocks(self):
        client = fixtures.fake_for_nanodictate()
        _rewrite(
            client,
            ".github/workflows/issue-scheduler.yml",
            "name: Issue scheduler\non:\n  schedule:\n    - cron: '*/30 * * * *'\n",
        )
        verdict = self._verdict(
            client, fixtures.baseline.read_ledger(fixtures.ledger_document())
        )

        self.assertFalse(verdict.ready)
        self.assertIn("audited_blob_moved", [reason.code for reason in verdict.reasons])


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


class PlanShapeTest(unittest.TestCase):
    def setUp(self):
        self.client = fixtures.fake_for_nanodictate()
        self.ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self.reading = _reading(self.client, self.ledger)
        self.plan = _plan(self.reading)

    def test_installs_and_retires_in_one_change(self):
        added = {edit.path: edit for edit in self.plan.edits if edit.action == "add"}
        removed = {edit.path for edit in self.plan.edits if edit.action == "remove"}

        for role in ("scheduler", "opencode", "repair", "review", "merge"):
            self.assertIn(
                plan_module._CALLER_PATHS[role], added, "{} was never installed".format(role)
            )
        self.assertIn(".github/workflows/issue-scheduler.yml", removed)
        self.assertIn(".github/workflows/auto-merge.yml", removed)
        self.assertEqual(self.plan.duplicated_roles, {})

    def test_every_generated_caller_pins_a_full_sha_and_calls_a_reusable(self):
        for edit in self.plan.edits:
            if not plan_module.is_generated(edit.path):
                continue
            self.assertIn(
                "kodmial/continuum/.github/workflows/consumer-", edit.content, edit.path
            )
            self.assertIn("@{}".format(CANDIDATE), edit.content, edit.path)
            self.assertNotIn("@main", edit.content, edit.path)
            self.assertNotIn("@v", edit.content, edit.path)

    def test_a_generated_caller_carries_no_logic(self):
        """A caller that grew a step or a condition would be a second
        implementation of the thing it exists to call."""

        for edit in self.plan.edits:
            if not plan_module.is_generated(edit.path):
                continue
            for line in edit.content.splitlines():
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith("run:") or stripped.startswith("uses: ./.github"),
                    "{} runs something: {}".format(edit.path, stripped),
                )

    def test_phase_a_does_not_touch_the_release_writers(self):
        paths = {edit.path for edit in self.plan.edits}

        self.assertNotIn(".github/workflows/release.yml", paths)
        self.assertIn((".github/workflows/release.yml", "release"), self.plan.retained)

    def test_phase_b_is_refused_rather_than_planned_without_a_release_caller(self):
        """The release phase retires the release writer, and this controller has
        no generated release caller to put in its place -- by design. A plan that
        did it anyway would satisfy every assertion in this module and leave the
        repository with no release at all."""

        with self.assertRaises(plan_module.PlanError) as caught:
            _plan(self.reading, phase=PHASE_B)

        self.assertEqual(caught.exception.code, "phase_not_implemented")
        self.assertIn("#21", caught.exception.message)
        self.assertIn("#22", caught.exception.message)

    def test_the_candidate_must_be_a_full_sha(self):
        for bad in ("main", "v1.2.3", "a" * 39, "A" * 40, ""):
            with self.assertRaises(plan_module.PlanError) as caught:
                _plan(self.reading, candidate=bad)
            self.assertEqual(caught.exception.code, "candidate_not_immutable", bad)

    def test_the_record_names_the_pre_cutover_revision_and_what_was_retired(self):
        record = json.loads(
            [edit for edit in self.plan.edits if edit.path == plan_module.ROLLBACK_RECORD_PATH][0].content
        )

        self.assertEqual(record["rollback_revision"], HEAD)
        self.assertEqual(record["revision"], CANDIDATE)
        self.assertIn(
            ".github/workflows/issue-scheduler.yml", record["retired"]
        )
        self.assertIn(
            plan_module._CALLER_PATHS["scheduler"], record["installed"]
        )
        self.assertTrue(record["actionable"])

    def test_the_overlay_classifies_every_path_the_change_adds(self):
        """The gate refuses a change to a path the ledger does not classify, so a
        cutover that installs a caller without proposing its audit entry would be
        refused by the very gate it is trying to satisfy."""

        overlay = self.plan.overlay_document
        proposed = {entry["path"] for entry in overlay["entries"]}

        for edit in self.plan.edits:
            if edit.action != "remove":
                self.assertIn(edit.path, proposed, edit.path)
            else:
                self.assertNotIn(edit.path, proposed, edit.path)

    def test_the_change_set_is_bound_to_the_head_the_change_is_built_on(self):
        """The gate judges a proposed change, so it needs the head it is on.

        Not a commit that does not exist yet: before the change is published the
        only commit it can honestly be bound to is the one it will be layered
        on top of, and that is the head `apply` has to find unchanged before it
        will build anything.
        """

        document = self.plan.change_set_document

        self.assertEqual(document["cutover_head"], self.plan.rollback_revision)
        self.assertEqual(
            {(change["path"], change["action"]) for change in document["changes"]},
            {(edit.path, edit.action) for edit in self.plan.edits},
        )

    def test_the_change_set_is_refused_when_the_plan_names_no_head(self):
        unbound = dataclasses.replace(self.plan, cutover_head="")

        with self.assertRaises(plan_module.PlanError) as caught:
            unbound.change_set_document

        self.assertEqual(caught.exception.code, "change_set_not_bound")


class PlanSingleWriterTest(unittest.TestCase):
    """The invariant the whole cutover turns on.

    Not "one file per role": NanoDictate's review lifecycle is several files and
    Continuum's is too. "No independently maintained implementation survives
    beside a generated one."
    """

    def test_a_legacy_writer_this_phase_replaces_is_retired_not_retained(self):
        client = fixtures.fake_for_nanodictate()
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        for path, writer in plan.retained:
            self.assertNotIn(writer, ("scheduler", "opencode", "repair", "review", "merge"))

    def test_a_hand_written_caller_elsewhere_is_removed_not_left_beside_the_generated_one(self):
        client = fixtures.fake_for_nanodictate()
        _rewrite(
            client,
            ".github/workflows/legacy-continuum-scheduler.yml",
            "name: Continuum scheduler\non: {schedule: [{cron: '*/15 * * * *'}]}\n"
            "jobs:\n  s:\n    uses: kodmial/continuum/.github/workflows/"
            "consumer-scheduler.yml@" + CANDIDATE + "\n",
        )
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        removed = {edit.path for edit in self.plan_removals(plan)}
        self.assertIn(".github/workflows/legacy-continuum-scheduler.yml", removed)
        self.assertEqual(plan.duplicated_roles, {})

    @staticmethod
    def plan_removals(plan):
        return [edit for edit in plan.edits if edit.action == "remove"]

    def test_a_writer_the_ledger_calls_release_survives_phase_a(self):
        client = fixtures.fake_for_nanodictate()
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        self.assertIn((".github/workflows/release.yml", "release"), plan.retained)

    def test_the_shadow_bridge_survives_every_phase(self):
        client = fixtures.fake_for_nanodictate()
        _rewrite(
            client,
            ".github/workflows/continuum-shadow-bridge.yml",
            "name: Continuum shadow bridge\non: [issues]\n",
        )
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        self.assertIn(
            (".github/workflows/continuum-shadow-bridge.yml", "other"),
            plan.retained,
        )


class PlanIdempotenceTest(unittest.TestCase):
    def test_a_second_plan_over_an_unchanged_repository_is_the_same_change(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        first = _plan(_reading(client, ledger))
        applied = _apply_first_change(client, first)

        second = _plan(_reading(client, ledger))

        changed = {edit.path for edit in first.edits if edit.action != "remove"}
        now_unchanged = {
            edit.path for edit in second.edits if edit.action != "remove"
        }
        self.assertEqual(now_unchanged & changed - {plan_module.ROLLBACK_RECORD_PATH}, set())
        self.assertNotIn(
            plan_module._CALLER_PATHS["scheduler"], now_unchanged
        )
        del applied

    def test_an_installed_caller_at_a_different_pin_is_replaced(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        _apply_first_change(client, _plan(_reading(client, ledger)))

        # Somebody changed the pin by hand. The reconciliation must put it back
        # rather than treat it as correct, and must not treat it as a conflict.
        path = plan_module._CALLER_PATHS["scheduler"]
        _rewrite(client, path, client.files[path].replace(CANDIDATE, "c" * 40))
        plan = _plan(_reading(client, ledger))

        actions = {edit.path: edit.action for edit in plan.edits}
        self.assertEqual(actions.get(path), "modify")


def _no_waiting():
    """Gate options that do not sleep.

    Zero deadline and a clock that never advances, so a test that is about a
    *conclusion* rather than about waiting costs no wall-clock time and cannot
    become flaky when CI is slow.
    """

    return {"timeout_s": 0.0, "poll_s": 0.0, "clock": _Clock(), "sleep": lambda _: None}


class _Clock:
    """A monotonic clock a test drives by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.now += float(seconds)


def _rewrite(client, path, body):
    """Replace a file's content *and* its recorded blob.

    Both, because the fake keeps them separately: the blob is what the drift
    checks compare, so a test that changes only the content would be testing
    nothing.
    """

    client.files[path] = body
    if path.startswith(".github/workflows/"):
        client.workflow_blobs[path] = inventory_module.git_blob_sha(body)


def _apply_first_change(client, plan) -> None:
    """Write a plan into the fake as though it had been merged."""

    for edit in plan.edits:
        if edit.action == "remove":
            client.files.pop(edit.path, None)
            client.workflow_blobs.pop(edit.path, None)
        else:
            client.files[edit.path] = edit.content
            if edit.path.startswith(".github/workflows/"):
                client.workflow_blobs[edit.path] = inventory_module.git_blob_sha(edit.content)


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #


class ApplyTest(unittest.TestCase):
    def _client(self, **kwargs):
        options = {"check_runs": {"*": [fixtures.run(name="CI")]}}
        options.update(kwargs)
        return fixtures.fake_for_nanodictate(**options)

    def _controller(self, client, plan):
        return apply_module.MigrationController(client, plan, repository=client.repository)

    def _state(self, reading):
        return apply_module.MigrationState(
            repository=reading.repository,
            phase=PHASE_A,
            base_sha=reading.head_sha,
            rollback_revision=reading.head_sha,
        )

    def _reconcile(self, client, plan, reading, **kwargs):
        options = {"required": ("CI",), "additional": ("Packaging smoke",)}
        options.update(kwargs)
        return apply_module.reconcile(
            self._controller(client, plan), self._state(reading), **options
        )

    def test_a_green_run_merges_with_the_validated_head(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        plan = _plan(reading)

        state = self._reconcile(client, plan, reading)

        self.assertTrue(state.merged, [s.describe() for s in state.stages])
        self.assertEqual(len(client.merge_calls), 1)
        number, head = client.merge_calls[0]
        self.assertEqual(head, state.validated_head)
        self.assertEqual(client.merge_calls[0][1], state.commit)

    def test_the_change_is_one_commit_on_one_branch(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        plan = _plan(reading)

        state = self._reconcile(client, plan, reading)

        self.assertEqual(len(client.commits), 1)
        self.assertEqual(client.trees[0][0], HEAD)
        self.assertEqual(client.refs["refs/heads/continuum/cutover"], state.commit)
        self.assertEqual(len(client.created_pulls), 1)

    def test_retirements_are_deletes_in_the_same_tree_not_a_second_change(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        plan = _plan(reading)

        self._reconcile(client, plan, reading)

        entries = {entry["path"]: entry for entry in client.trees[0][1]}
        self.assertIsNone(entries[".github/workflows/issue-scheduler.yml"]["sha"])
        self.assertTrue(entries[plan_module._CALLER_PATHS["scheduler"]]["sha"])
        self.assertEqual(client.count("create_tree"), 1)
        self.assertEqual(client.count("create_commit"), 1)

    def test_it_refuses_to_merge_when_a_blocking_gate_did_not_run(self):
        client = self._client(check_runs={"*": []})
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        plan = _plan(reading)

        state = self._reconcile(client, plan, reading, wait=_no_waiting())

        self.assertFalse(state.merged)
        self.assertEqual(client.merge_calls, [])
        stage = [item for item in state.stages if item.name == "head-gates"][0]
        self.assertFalse(stage.ok)
        self.assertIn("has not reported for this head", stage.detail)

    def test_it_refuses_to_merge_when_a_blocking_gate_failed(self):
        client = self._client(
            check_runs={"*": [fixtures.run(name="CI", conclusion="failure")]}
        )
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading, wait=_no_waiting())

        self.assertFalse(state.merged)
        self.assertEqual(client.merge_calls, [])

    def test_a_gate_that_has_not_run_yet_is_retryable_and_a_failure_is_not(self):
        """The distinction #84 requires: infrastructure does not consume the
        migration's semantic budget, and a red CI is an answer rather than an
        outage."""

        pending = self._controller(
            self._client(check_runs={"*": []}),
            _plan(_reading(self._client(), fixtures.baseline.read_ledger(fixtures.ledger_document()))),
        ).wait_for_head_gates("c" * 40, ("CI",), (), **_no_waiting())
        failed = self._controller(
            self._client(check_runs={"*": [fixtures.run(name="CI", conclusion="failure")]}),
            _plan(_reading(self._client(), fixtures.baseline.read_ledger(fixtures.ledger_document()))),
        ).wait_for_head_gates("c" * 40, ("CI",), (), **_no_waiting())

        self.assertFalse(pending["ok"])
        self.assertTrue(pending["retryable"])
        self.assertFalse(failed["ok"])
        self.assertFalse(failed["retryable"])

    def test_it_waits_for_the_gates_of_the_head_it_just_published(self):
        """Publishing a pull request and asking GitHub about its check runs
        immediately would find nothing, because the runs have not started."""

        client = self._client(check_runs={"*": []})
        clock = _Clock()
        client.check_runs["*"] = []

        original = client.list_check_runs
        seen = {"calls": 0}

        def list_check_runs(ref):
            seen["calls"] += 1
            if seen["calls"] > 2:
                client.check_runs["*"] = [fixtures.run(name="CI")]
            return original(ref)

        client.list_check_runs = list_check_runs
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(
            client,
            _plan(reading),
            reading,
            wait={"timeout_s": 60.0, "poll_s": 5.0, "clock": clock, "sleep": clock.sleep},
        )

        self.assertTrue(state.merged, [s.describe() for s in state.stages])
        self.assertGreater(seen["calls"], 2)
        self.assertEqual(clock.slept, [5.0, 5.0])

    def test_an_additional_gate_that_did_not_run_is_not_a_failure(self):
        """Path-filtered gates do not run for every head. The production merge
        controller treats an absent additional gate as inapplicable and a *run*
        additional gate as blocking, and the cutover has to agree or it would
        refuse to merge a change the repository's own merge rules allow."""

        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading)

        self.assertTrue(state.merged)

    def test_an_additional_gate_that_ran_and_failed_blocks(self):
        client = self._client(
            check_runs={
                "*": [
                    fixtures.run(name="CI"),
                    fixtures.run(name="Packaging smoke", conclusion="failure"),
                ]
            }
        )
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading, wait=_no_waiting())

        self.assertFalse(state.merged)

    def test_it_refuses_when_the_pull_request_does_not_contain_the_reviewed_change(self):
        """Read back from the pull request, not trusted from the plan.

        A hand-edit, a base that moved, or a merge resolved differently from what
        the plan expected would all show up here and nowhere earlier -- which is
        what makes this the check the cutover gate is entitled to insist on.
        """

        client = self._client()
        original = client.list_pull_files

        def list_pull_files(number):
            found = original(number)
            if client.created_pulls:
                found = found + [{"filename": ".github/workflows/surprise.yml"}]
            return found

        client.list_pull_files = list_pull_files
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading)

        self.assertFalse(state.merged)
        self.assertEqual(client.merge_calls, [])
        stage = [item for item in state.stages if item.name == "change-set"][0]
        self.assertFalse(stage.ok)
        self.assertIn("surprise.yml", stage.detail)

    def test_it_refuses_when_the_cutover_gate_says_no(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(
            client,
            _plan(reading),
            reading,
            gate={"ok": False, "detail": "the canary window has no release evidence"},
        )

        self.assertFalse(state.merged)
        self.assertEqual(client.merge_calls, [])

    def test_it_records_merging_without_a_gate_rather_than_hiding_it(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading)

        stage = [item for item in state.stages if item.name == "cutover-gate"][0]
        self.assertTrue(stage.ok)
        self.assertIn("without a cutover authorization", stage.detail)
        self.assertIn("merged without a cutover gate verdict", state.notes)

    def test_a_second_run_converges_and_does_not_open_a_second_pull_request(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        plan = _plan(reading)
        first = self._reconcile(client, plan, reading)

        # The pull request is now merged, which is what a re-run finds.
        for pull in client.pull_requests:
            pull["merged"] = True
            pull["state"] = "closed"
        second = apply_module.reconcile(
            self._controller(client, plan), first, required=("CI",), additional=()
        )

        self.assertEqual(len(client.created_pulls), 1)
        self.assertTrue(second.merged)
        self.assertEqual(client.count("create_commit"), 1)

    def test_it_reuses_the_pull_request_it_opened_last_time(self):
        client = self._client(
            pull_requests=[
                {
                    "number": 77,
                    "state": "open",
                    "merged": False,
                    "head": {"ref": "continuum/cutover", "sha": "x" * 40},
                }
            ],
            pull_files={77: []},
        )
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))

        state = self._reconcile(client, _plan(reading), reading)

        self.assertEqual(client.created_pulls, [])
        self.assertEqual(state.pull_request, 77)

    def test_it_refuses_to_move_a_branch_somebody_else_moved(self):
        client = self._client(
            refs={"refs/heads/continuum/cutover": "f" * 40},
        )
        controller = self._controller(
            client,
            _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))),
        )

        with self.assertRaises(apply_module.ApplyError) as caught:
            controller.publish(HEAD)

        self.assertEqual(caught.exception.code, "migration_branch_owned_elsewhere")
        self.assertNotIn("update_ref", client.method_names())

    def test_it_will_move_a_branch_holding_its_own_earlier_commit(self):
        """The other half of the same rule: re-planning against a newer base has
        to be able to supersede its own previous commit, or the controller could
        never converge."""

        client = self._client(refs={"refs/heads/continuum/cutover": "c" * 40})
        controller = self._controller(
            client,
            _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))),
        )

        controller.publish(HEAD, expected_tip="c" * 40)

        self.assertIn("update_ref", client.method_names())

    def test_a_method_outside_the_write_allowlist_is_refused(self):
        client = self._client()
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))
        controller = apply_module.MigrationController(client, plan)

        with self.assertRaises(apply_module.ApplyError) as caught:
            controller._write("dispatch_workflow", "ci.yml", "main", {})

        self.assertEqual(caught.exception.code, "unlisted_write")

    def test_labels_somebody_else_chose_are_left_alone(self):
        client = self._client(labels=["review-ready"])
        controller = self._controller(
            client,
            _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))),
        )

        outcome = controller.prepare()

        self.assertIn("review-ready", outcome["kept"])
        self.assertNotIn("review-ready", [name for name, _, _ in client.created_labels])

    def test_the_commit_message_names_the_pin(self):
        client = self._client()
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        self._reconcile(client, plan, _reading(client))

        self.assertIn(CANDIDATE, client.commits[0][0])

    def test_the_pull_request_body_names_the_retirements_and_the_way_back(self):
        reading = _reading(
            self._client(),
            fixtures.baseline.read_ledger(fixtures.ledger_document()),
        )
        body = apply_module.render_body(_plan(reading))

        self.assertIn("issue-scheduler.yml", body)
        self.assertIn("Retired in the same change", body)
        self.assertIn("continuum migrate rollback", body)
        self.assertIn(HEAD, body)

    def test_the_merge_method_is_the_one_that_keeps_one_commit(self):
        self.assertEqual(MERGE_METHOD, "squash")

    def test_the_state_document_round_trips(self):
        client = self._client()
        reading = _reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document()))
        state = self._reconcile(client, _plan(reading), reading)

        restored = apply_module.read_state(json.loads(json.dumps(state.describe())))

        self.assertEqual(restored.describe(), state.describe())
        self.assertTrue(restored.ready)


# --------------------------------------------------------------------------- #
# Rolling back
# --------------------------------------------------------------------------- #


class RollbackTest(unittest.TestCase):
    def _record(self, client, **overrides):
        options = {
            "revision": CANDIDATE,
            "rollback_revision": HEAD,
            "installed": sorted(plan_module._CALLER_PATHS.values()),
            "retired": [
                ".github/workflows/issue-scheduler.yml",
                ".github/workflows/auto-merge.yml",
            ],
        }
        options.update(overrides)
        return rollback_module.render_record(
            client.repository, PHASE_A, **options
        )

    def _cut_over(self, client, ledger):
        """Move the fake from pre-cutover to post-cutover.

        The pre-cutover file table is kept as history at ``HEAD``, because that is
        where the cutover record says a rollback reads from. Without it the fake
        would report the retired workflows as never having existed, and the
        rollback would look like it was restoring a revision that had nothing in
        it -- which is one of the cases the rollback is supposed to *refuse*,
        just for the wrong reason.
        """

        client.historical_files[HEAD] = dict(client.files)
        plan = _plan(_reading(client, ledger))
        _apply_first_change(client, plan)
        return plan

    def _reading_after(self, client, ledger):
        return _reading(client, ledger)

    def test_the_record_is_written_by_the_plan_and_names_the_pre_cutover_head(self):
        client = fixtures.fake_for_nanodictate()
        plan = _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

        record = rollback_module.read_record(
            json.loads(
                [
                    edit
                    for edit in plan.edits
                    if edit.path == rollback_module.RECORD_PATH
                ][0].content
            )
        )

        self.assertEqual(record.rollback_revision, HEAD)
        self.assertTrue(record.actionable)
        self.assertIn(".github/workflows/issue-scheduler.yml", record.retired)

    def test_a_record_with_no_revision_is_reported_as_unactionable_not_restored(self):
        client = fixtures.fake_for_nanodictate()
        record = rollback_module.read_record(
            json.loads(self._record(client, rollback_revision="", retired=[], installed=[]))
        )

        self.assertFalse(record.actionable)
        with self.assertRaises(rollback_module.RollbackError) as caught:
            rollback_module.compute_restore(record, _reading(client))
        self.assertEqual(caught.exception.code, "record_not_actionable")

    def test_it_restores_and_removes_in_one_change(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self._cut_over(client, ledger)
        record = rollback_module.read_record(json.loads(self._record(client)))

        reading = self._reading_after(client, ledger)
        outcome = rollback_module.reconcile(
            client, record, reading, target_writers={
                ".github/workflows/issue-scheduler.yml": baseline.WRITER_SCHEDULER,
                ".github/workflows/auto-merge.yml": baseline.WRITER_MERGE,
            },
        )

        self.assertFalse(outcome["noop"])
        self.assertEqual(len(client.commits), 1)
        self.assertEqual(len(client.created_pulls), 1)
        entries = {entry["path"]: entry for entry in client.trees[-1][1]}
        self.assertIn(".github/workflows/issue-scheduler.yml", entries)
        self.assertIn(
            plan_module._CALLER_PATHS["scheduler"], entries
        )
        self.assertIsNone(entries[plan_module._CALLER_PATHS["scheduler"]]["sha"])
        # The record goes with it, or the next reader would think a cutover is
        # still in force.
        self.assertIsNone(entries[rollback_module.RECORD_PATH]["sha"])

    def test_it_refuses_when_the_recorded_revision_does_not_have_the_file(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self._cut_over(client, ledger)
        record = rollback_module.read_record(json.loads(self._record(client)))
        # The fake's file table is the post-cutover state; the recorded revision's
        # copy is not there to restore from.
        client.read_file_at_ref = _missing_files(client)

        with self.assertRaises(rollback_module.RollbackError) as caught:
            rollback_module.reconcile(
                client,
                record,
                self._reading_after(client, ledger),
                target_writers={
                    ".github/workflows/issue-scheduler.yml": baseline.WRITER_SCHEDULER,
                    ".github/workflows/auto-merge.yml": baseline.WRITER_MERGE,
                },
            )

        self.assertEqual(caught.exception.code, "rollback_target_missing_file")

    def test_it_refuses_rather_than_stranding_a_role_with_two_implementations(self):
        """The dual-writer window is the failure this whole design avoids. A
        rollback that produced one would turn a recoverable incident into an
        unrecoverable one, so it refuses and names both paths."""

        client = fixtures.fake_for_nanodictate()
        self._cut_over(
            client,
            fixtures.baseline.read_ledger(fixtures.ledger_document()),
        )
        # Somebody took the scheduler job over while the cutover was in flight,
        # and the audit has since been extended to cover it -- which is what
        # makes this a decision the controller can reason about rather than a file
        # it has never heard of.
        _rewrite(
            client,
            ".github/workflows/replacement-scheduler.yml",
            "name: Replacement scheduler\non:\n  schedule:\n    - cron: '*/15 * * * *'\n",
        )
        entries = list(fixtures.LEDGER_ENTRIES) + [
            (
                ".github/workflows/replacement-scheduler.yml",
                baseline.WRITER_SCHEDULER,
                baseline.CLASSIFICATION_ABSORBED,
            )
        ]
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document(entries=entries))
        record = rollback_module.read_record(json.loads(self._record(client)))

        with self.assertRaises(rollback_module.RollbackError) as caught:
            rollback_module.reconcile(
                client,
                record,
                self._reading_after(client, ledger),
                target_writers={
                    ".github/workflows/issue-scheduler.yml": baseline.WRITER_SCHEDULER,
                    ".github/workflows/auto-merge.yml": baseline.WRITER_MERGE,
                },
            )

        self.assertEqual(caught.exception.code, "stranded_generated_caller")
        self.assertIn("replacement-scheduler.yml", caught.exception.message)

    def test_a_second_rollback_is_a_no_op_that_reports_success(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self._cut_over(client, ledger)
        record = rollback_module.read_record(json.loads(self._record(client)))
        writers = {
            ".github/workflows/issue-scheduler.yml": baseline.WRITER_SCHEDULER,
            ".github/workflows/auto-merge.yml": baseline.WRITER_MERGE,
        }
        rollback_module.reconcile(client, record, self._reading_after(client, ledger), target_writers=writers)
        _apply_first_change(client, _plan(_reading(client, ledger)))

        # The default branch now holds the restored legacy writers and no
        # generated callers, so there is nothing left to undo.
        client.files.pop(rollback_module.RECORD_PATH, None)
        for path in plan_module._CALLER_PATHS.values():
            client.files.pop(path, None)
            client.workflow_blobs.pop(path, None)
        for path in record.retired:
            client.files[path] = fixtures.nanodictate_files()[path]
            client.workflow_blobs[path] = inventory_module.git_blob_sha(client.files[path])
        reading = self._reading_after(client, ledger)

        again = rollback_module.reconcile(client, record, reading, target_writers=writers)

        self.assertTrue(again["ok"])
        self.assertTrue(again["noop"])

    def test_a_file_that_changed_after_the_cutover_is_left_alone_and_reported(self):
        client = fixtures.fake_for_nanodictate()
        ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self._cut_over(client, ledger)
        # The legacy scheduler was reinstated by hand, at a different content.
        client.files[".github/workflows/issue-scheduler.yml"] = (
            "name: Issue scheduler\non:\n  schedule:\n    - cron: '*/30 * * * *'\n"
        )
        client.workflow_blobs[".github/workflows/issue-scheduler.yml"] = (
            inventory_module.git_blob_sha(client.files[".github/workflows/issue-scheduler.yml"])
        )
        record = rollback_module.read_record(json.loads(self._record(client)))

        outcome = rollback_module.reconcile(
            client,
            record,
            self._reading_after(client, ledger),
            target_blobs={path: "0" * 40 for path in record.retired},
            target_writers={
                ".github/workflows/issue-scheduler.yml": baseline.WRITER_SCHEDULER,
                ".github/workflows/auto-merge.yml": baseline.WRITER_MERGE,
            },
        )

        self.assertIn(".github/workflows/issue-scheduler.yml", outcome["divergent"])
        self.assertTrue(
            any("left alone" in note for note in outcome["notes"]), outcome["notes"]
        )


def _missing_files(client):
    """A ``read_file_at_ref`` that reports every file absent.

    Used to simulate a recorded revision that does not contain what the record
    says it removed. The fake raises rather than returning ``None`` for a method
    it was not configured for, and returning ``None`` for *everything* is exactly
    the state this test needs, so it wraps rather than misuses.
    """

    def read(path, ref):
        client.calls.append(("read_file_at_ref", (path, ref), {}))
        return None

    return read


class MigrationClientTransportTest(unittest.TestCase):
    """The URLs and the reads the cutover depends on, checked against GitHub.

    The controller is exercised end to end against a fake, which is the right way
    to test a *reconciliation* and the wrong way to test a *transport*. A fake
    accepts any path, so a ref update aimed at ``/git/refs/refs/heads/x`` or a
    recursive tree read through a list paginator passes every behavioural test
    and then 404s in production. These are the shapes that can only be wrong
    against the real API.
    """

    def setUp(self):
        self.calls = []
        client = client_module.MigrationClient("t0ken", "kodmial/nanodictate")
        client._opener = self._opener
        self.client = client

    def _opener(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.data))
        answer = self.answers.pop(0) if self.answers else ({}, None)
        return answer

    def _respond(self, *documents):
        self.answers = list(documents)

    @property
    def paths(self):
        return [urllib.parse.urlparse(call[1]).path for call in self.calls]

    def test_a_branch_ref_is_read_from_the_documented_endpoint(self):
        self._respond({"object": {"sha": "a" * 40}})

        self.assertEqual(self.client.get_ref("refs/heads/continuum/cutover"), "a" * 40)

        # `git/ref/refs/heads/...` is a 404, and it is the shape a caller has
        # naturally got: the ref it holds locally is spelled with the prefix.
        self.assertEqual(
            self.paths, ["/repos/kodmial/nanodictate/git/ref/heads/continuum/cutover"]
        )

    def test_a_branch_name_without_the_prefix_names_the_same_endpoint(self):
        self._respond({"object": {"sha": "a" * 40}}, {"object": {"sha": "a" * 40}})

        self.client.get_ref("refs/heads/continuum/cutover")
        self.client.get_ref("continuum/cutover")

        self.assertEqual(self.paths[0], self.paths[1])

    def test_a_ref_that_does_not_exist_reads_as_absent(self):
        def refuse(request, timeout):
            self.calls.append((request.get_method(), request.full_url, request.data))
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")
            )

        self.client._opener = refuse

        self.assertEqual(self.client.get_ref("refs/heads/continuum/cutover"), "")

    def test_a_ref_that_cannot_be_read_is_not_absent(self):
        """A 500 must not look like an absent branch.

        Treating an unreachable API as "the branch is not there" would have the
        controller claim a branch somebody else owns.
        """

        def broken(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", {}, io.BytesIO(b"{}")
            )

        self.client._opener = broken

        # Not an empty string: an empty string is what "this branch does not
        # exist" looks like, and reading a 500 as an absent branch would have the
        # controller create a branch somebody else already owns.
        with self.assertRaises(github_module.GitHubError):
            self.client.get_ref("refs/heads/continuum/cutover")

    def test_a_ref_is_moved_at_the_update_endpoint_with_the_branch_name_only(self):
        self._respond({"object": {"sha": "a" * 40}}, {})

        self.client.update_ref("refs/heads/continuum/cutover", "b" * 40, expected="a" * 40)

        self.assertEqual(
            self.paths,
            [
                "/repos/kodmial/nanodictate/git/ref/heads/continuum/cutover",
                "/repos/kodmial/nanodictate/git/refs/heads/continuum/cutover",
            ],
        )
        method, _, body = self.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(json.loads(body), {"sha": "b" * 40, "force": True})
        self.assertNotIn("refs/refs", self.calls[-1][1])

    def test_a_ref_that_moved_underneath_us_is_refused(self):
        self._respond({"object": {"sha": "c" * 40}})

        with self.assertRaises(client_module.MigrationError) as caught:
            self.client.update_ref("refs/heads/continuum/cutover", "b" * 40, expected="a" * 40)

        self.assertEqual(caught.exception.code, "ref_moved_externally")
        # And it did not move it anyway.
        self.assertEqual([call[0] for call in self.calls], ["GET"])

    def test_a_recursive_tree_is_read_as_an_object_not_as_a_list_of_pages(self):
        """The tree endpoint answers with an object, not an array.

        A paginator accumulates lists and ignores everything else, so a tree read
        through one returns `{}` for every repository -- indistinguishable from a
        commit containing no files, which is what a rollback would then believe.
        """

        self._respond(
            {
                "sha": "t" * 40,
                "truncated": False,
                "tree": [
                    {"path": ".github/workflows/a.yml", "type": "blob", "sha": "1" * 40},
                    {"path": ".github", "type": "tree", "sha": "2" * 40},
                    {"path": ".github/workflows/b.yml", "type": "blob", "sha": "3" * 40},
                ],
            }
        )

        blobs = self.client.tree_blobs("d" * 40)

        self.assertEqual(
            blobs,
            {
                ".github/workflows/a.yml": "1" * 40,
                ".github/workflows/b.yml": "3" * 40,
            },
        )
        self.assertEqual(len(self.calls), 1)

    def test_a_truncated_tree_is_refused_rather_than_halved(self):
        self._respond({"sha": "t" * 40, "truncated": True, "tree": []})

        with self.assertRaises(client_module.MigrationError) as caught:
            self.client.tree_blobs("d" * 40)

        self.assertEqual(caught.exception.code, "tree_truncated")

    def test_a_file_that_is_absent_reads_as_absent_and_nothing_else(self):
        def refuse(request, timeout):
            self.calls.append((request.get_method(), request.full_url, request.data))
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")
            )

        self.client._opener = refuse
        self.assertIsNone(self.client.read_file_at_ref(".github/continuum.yml", "main"))

    def test_a_file_that_cannot_be_read_is_an_error_and_not_an_absence(self):
        """A rate limit that reads as "this file does not exist" is how a
        cutover quietly plans on top of a repository it never saw."""

        def refuse(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}")
            )

        self.client._opener = refuse

        # A rate limit is not an absence. ``read_file_at_ref`` is the only read
        # that may answer "this file is not there", and only for a 404.
        with self.assertRaises(github_module.GitHubError):
            self.client.read_file_at_ref(".github/continuum.yml", "main")

    def test_a_commit_read_is_a_read(self):
        self._respond({"sha": "e" * 40, "tree": {"sha": "f" * 40}})

        self.assertEqual(self.client.get_commit("e" * 40)["tree"]["sha"], "f" * 40)
        self.assertEqual(self.paths, ["/repos/kodmial/nanodictate/git/commits/" + "e" * 40])

    def test_a_secret_value_is_never_requested(self):
        source = inspect.getsource(client_module)

        self.assertIn("list_repo_secret_names", source)
        for needle in ("secret_value", "/secrets/{", "actions/secrets/"):
            self.assertNotIn(needle, source)


class RepositoryNameTest(unittest.TestCase):
    """The repository name is interpolated into every URL, so it is checked once.

    The base client accepts anything containing a slash and splits on the first
    one, which is enough to build a URL and not enough to build the right one.
    These are the shapes that reach a different API path rather than failing.
    """

    def test_an_ordinary_name_is_split(self):
        self.assertEqual(
            client_module.repository_owner_and_name("kodmial/nanodictate"),
            ("kodmial", "nanodictate"),
        )

    def test_a_second_slash_is_refused_rather_than_split(self):
        """``owner/name/contents`` is a path, not a repository.

        Split on the first slash it becomes an owner and a name of
        ``name/contents``, and every endpoint here is a string format, so the
        read or the write lands somewhere the operator never named.
        """

        for repository in ("kodmial/nanodictate/contents", "a/b/c", "/repos/a/b"):
            with self.subTest(repository=repository):
                with self.assertRaises(client_module.MigrationError) as caught:
                    client_module.repository_owner_and_name(repository)
                self.assertEqual(caught.exception.code, "malformed_repository")

    def test_an_empty_half_is_refused(self):
        for repository in ("/nanodictate", "kodmial/", "/", "", "   "):
            with self.subTest(repository=repository):
                with self.assertRaises(client_module.MigrationError) as caught:
                    client_module.repository_owner_and_name(repository)
                self.assertEqual(caught.exception.code, "malformed_repository")

    def test_characters_that_could_aim_a_url_elsewhere_are_refused(self):
        """A name is a name: no query, no fragment, no percent-encoding, no space.

        Each of these would either decode into a different path or produce a URL
        the caller did not write.
        """

        for repository in (
            "kodmial/nanodictate?per_page=100",
            "kodmial/nanodictate#x",
            "kodmial/nan%2Fnanodictate",
            "kodmial/nano dictate",
            "kodmial/nanodictate/../..",
            "../../kodmial/nanodictate",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(client_module.MigrationError) as caught:
                    client_module.repository_owner_and_name(repository)
                self.assertEqual(caught.exception.code, "malformed_repository")

    def test_the_client_refuses_before_it_can_build_a_url(self):
        """The check is in the constructor, not in a caller that might forget it."""

        with self.assertRaises(client_module.MigrationError) as caught:
            client_module.MigrationClient("t0ken", "kodmial/nanodictate/contents")
        self.assertEqual(caught.exception.code, "malformed_repository")

    def test_the_check_holds_for_the_keyword_constructor_too(self):
        """Validating only the positional form would leave a hole for the other one."""

        with self.assertRaises(client_module.MigrationError) as caught:
            client_module.MigrationClient(
                "t0ken", repository="kodmial/nanodictate/contents"
            )
        self.assertEqual(caught.exception.code, "malformed_repository")

    def test_the_workflow_and_the_engine_agree_on_what_a_target_is(self):
        """Two layers checking different shapes would let each one pass the other's.

        The workflow guards its `target_repository` input with a shell pattern; if
        the engine accepted something narrower, the check would only hold for
        people who came through the workflow.
        """

        workflow = (
            pathlib.Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "continuum-migrate.yml"
        ).read_text(encoding="utf-8")
        # The shape check is the line right after the target is resolved.
        following = re.search(
            r'target="\$\{TARGET_INPUT[^"]*"\n(.*?)\n', workflow, re.DOTALL
        ).group(1)
        shape = re.search(r"grep -Eq '([^']+)'", following).group(1)
        # The shell pattern is unanchored, so anchor it the way the engine does
        # before comparing the two character classes.
        anchored = shape.replace("^", r"\A").replace("$", r"\Z")

        self.assertEqual(client_module.REPOSITORY_SHAPE.pattern, anchored)

        # And the engine's own rule, exercised both ways, agrees with the workflow.
        self.assertTrue(client_module.REPOSITORY_SHAPE.match("kodmial/nanodictate"))
        self.assertIsNone(client_module.REPOSITORY_SHAPE.match("nanodictate"))


class CliTest(unittest.TestCase):
    """The command line, which is the only surface a workflow actually calls."""

    def _arguments(self, *argv):
        return client_module and cli_module._parser().parse_args(list(argv))

    def test_a_branch_name_is_refused_where_a_sha_belongs(self):
        for candidate in ("main", "v1.2.3", "abc", "A" * 40, "g" * 40):
            with self.subTest(candidate=candidate):
                with self.assertRaises(plan_module.PlanError) as caught:
                    cli_module._plan(
                        self._arguments(
                            "plan",
                            "--repo",
                            "kodmial/nanodictate",
                            "--candidate",
                            candidate,
                        ),
                        _reading(fixtures.fake_for_nanodictate()),
                    )
                self.assertEqual(caught.exception.code, "candidate_not_immutable")

    def test_apply_refuses_to_run_without_a_gate(self):
        """`--gate` is required at the parser, so there is no way to reach the
        reconciler's merge stage without one."""

        parser = cli_module._parser()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "apply",
                    "--repo",
                    "kodmial/nanodictate",
                    "--candidate",
                    "a" * 40,
                ]
            )
        self.assertIn("--gate", stderr.getvalue())

    def test_every_subcommand_except_inventory_requires_a_ledger(self):
        parser = cli_module._parser()
        extra = {
            "rollback": ["--record", "r.json"],
            "apply": ["--gate", "g.json"],
        }
        for command in ("preflight", "plan", "apply", "rollback"):
            with self.subTest(command=command):
                arguments = parser.parse_args(
                    [command, "--repo", "kodmial/nanodictate"]
                    + ([] if command == "rollback" else ["--candidate", "a" * 40])
                    + extra.get(command, [])
                )
                with self.assertRaises(baseline.BaselineError):
                    cli_module._require_ledger(arguments)

    def test_an_inventory_may_be_read_without_a_ledger(self):
        """Reading is not deciding, and a reading with no ledger is a document
        that says so rather than an error."""

        arguments = self._arguments("inventory", "--repo", "kodmial/nanodictate")

        self.assertIsNone(cli_module._ledger(arguments))


class GateBindingTest(unittest.TestCase):
    """The cutover gate's verdict is an authorization for one change, at one head.

    Without this the gate is a checkbox: a `ready` recorded against an earlier
    plan would authorize whatever the controller happens to build next, which is
    the failure mode a gate exists to prevent.
    """

    def setUp(self):
        self.client = fixtures.fake_for_nanodictate()
        self.ledger = fixtures.baseline.read_ledger(fixtures.ledger_document())
        self.reading = _reading(self.client, self.ledger)
        self.plan = _plan(self.reading)
        self.document = self.plan.change_set_document

    def _gate(self, document):
        path = self._write_json(document)
        arguments = self._arguments()
        arguments.gate = path
        arguments.phase = self.plan.phase
        return cli_module._gate(arguments, self.plan, head=self.plan.cutover_head)

    def _write_json(self, document):
        # Inside the worktree, never a system temporary directory: an agent run
        # leaves no files anywhere it could not clean up.
        directory = pathlib.Path(__file__).resolve().parent
        handle, path = tempfile.mkstemp(suffix=".json", dir=str(directory))
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def _arguments(self):
        arguments = cli_module._parser().parse_args(
            ["apply", "--repo", "kodmial/nanodictate", "--candidate", "a" * 40, "--gate", "g.json"]
        )
        return arguments

    def _decision(self, **overrides):
        document = {
            "schema": cutover_module.CUTOVER_SCHEMA,
            "kind": "decision",
            "ready": True,
            "approved": False,
            "authorized": True,
            "phase": self.plan.phase,
            "change_set": self.document,
        }
        document.update(overrides)
        return document

    def test_a_verdict_for_this_change_at_this_head_is_accepted(self):
        verdict = self._gate(self._decision())

        self.assertTrue(verdict["ok"])
        self.assertIn("authorized this head", verdict["detail"])

    def test_a_human_approval_counts_as_well_as_an_authorization(self):
        verdict = self._gate(self._decision(authorized=False, approved=True))

        self.assertTrue(verdict["ok"])

    def test_a_gate_that_is_not_ready_is_not_an_authorization(self):
        verdict = self._gate(
            self._decision(authorized=False, approved=False, blockers=[{"code": "x", "message": "no canary"}])
        )

        self.assertFalse(verdict["ok"])
        self.assertIn("no canary", verdict["detail"])

    def test_a_verdict_for_a_different_head_is_refused(self):
        other = dict(self.document, cutover_head="c" * 40)

        with self.assertRaises(plan_module.PlanError) as caught:
            self._gate(self._decision(change_set=other))

        self.assertEqual(caught.exception.code, "gate_head_mismatch")

    def test_a_verdict_for_a_different_phase_is_refused(self):
        with self.assertRaises(plan_module.PlanError) as caught:
            self._gate(self._decision(change_set=dict(self.document, phase=PHASE_B)))

        self.assertEqual(caught.exception.code, "gate_phase_mismatch")

    def test_a_verdict_naming_no_change_set_is_refused(self):
        with self.assertRaises(plan_module.PlanError) as caught:
            self._gate(self._decision(change_set=None))

        self.assertEqual(caught.exception.code, "gate_change_set_missing")

    def test_a_verdict_for_a_change_set_with_one_more_file_is_refused(self):
        """The case that matters: the plan grew a path the gate never saw."""

        changes = list(self.document["changes"])
        changes.append({"path": ".github/workflows/extra.yml", "action": "add", "writer": "merge"})

        with self.assertRaises(plan_module.PlanError) as caught:
            self._gate(self._decision(change_set=dict(self.document, changes=changes)))

        self.assertEqual(caught.exception.code, "gate_change_set_mismatch")
        self.assertIn("extra.yml", caught.exception.message)

    def test_a_verdict_for_a_change_set_with_one_file_missing_is_refused(self):
        changes = [change for change in self.document["changes"] if change["action"] != "remove"]

        with self.assertRaises(plan_module.PlanError) as caught:
            self._gate(self._decision(change_set=dict(self.document, changes=changes)))

        self.assertEqual(caught.exception.code, "gate_change_set_mismatch")

    def test_a_document_from_a_different_producer_is_refused(self):
        with self.assertRaises(Exception) as caught:
            self._gate({"ok": True, "authorized": True})

        self.assertIn("cutover", str(caught.exception).lower())


class ResumeWithoutStateTest(unittest.TestCase):
    """A cancelled run must not strand the branch it was in the middle of pushing.

    The branch is owned by *this controller* and the commit it holds is one it
    built, but neither fact is recorded anywhere durable: the state document lives
    in a workflow artifact, and the run that would have written it is the run that
    got cancelled. A controller that could only resume from its own bookkeeping
    would strand a branch nobody else will ever claim, and a cutover that has to
    be finished by hand is not zero-touch.
    """

    def _controller(self, client, plan):
        return apply_module.MigrationController(
            client, plan, repository="kodmial/nanodictate"
        )

    def _plan_for(self, client):
        return _plan(_reading(client, fixtures.baseline.read_ledger(fixtures.ledger_document())))

    def test_a_branch_holding_this_plans_tree_is_adopted(self):
        client = fixtures.fake_for_nanodictate()
        plan = self._plan_for(client)
        first = self._controller(client, plan).publish(plan.rollback_revision)

        # The next run has no state at all, which is what a cancelled run leaves
        # behind. It rebuilds the same change -- same tree, new commit SHA, exactly
        # as git behaves -- and finds its own earlier commit waiting.
        second = self._controller(client, self._plan_for(client)).publish(plan.rollback_revision)

        self.assertEqual(first["tree"], second["tree"])
        self.assertNotEqual(first["commit"], second["commit"])
        self.assertEqual(
            client.refs["refs/heads/{}".format(apply_module.BRANCH)], second["commit"]
        )

    def test_a_branch_holding_somebody_elses_tree_is_still_refused(self):
        client = fixtures.fake_for_nanodictate()
        plan = self._plan_for(client)
        published = self._controller(client, plan).publish(plan.rollback_revision)
        # A commit on the migration branch that is not this controller's: a human
        # fix, or another tool entirely.
        client.commit_trees[published["commit"]] = "a" * 40

        with self.assertRaises(apply_module.ApplyError) as caught:
            self._controller(client, self._plan_for(client)).publish(plan.rollback_revision)

        self.assertEqual(caught.exception.code, "migration_branch_owned_elsewhere")
        self.assertEqual(
            client.refs["refs/heads/{}".format(apply_module.BRANCH)], published["commit"]
        )

    def test_an_unreadable_commit_is_not_adopted(self):
        """Losing the ability to prove ownership has to fall on the side of
        refusing, or "cannot check" quietly becomes "assume it is mine"."""

        client = fixtures.fake_for_nanodictate()
        plan = self._plan_for(client)
        self._controller(client, plan).publish(plan.rollback_revision)
        client.commit_trees.clear()

        with self.assertRaises(apply_module.ApplyError) as caught:
            self._controller(client, self._plan_for(client)).publish(plan.rollback_revision)

        self.assertEqual(caught.exception.code, "migration_branch_owned_elsewhere")


if __name__ == "__main__":
    unittest.main()
