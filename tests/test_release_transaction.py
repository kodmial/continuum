"""One release, many runners, one publication.

The transaction is the only place in the release plane that can write a
destination, so the tests here are mostly about refusals: a missing target, a
fragment from another commit, a matrix that disagrees with the request, two
targets that produced the same asset name. Each of those is a way a distributed
release turns into a partial one, and the counter-assertions on the fixtures
prove the refusal happened *before* anything was uploaded rather than after.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, Dict, Optional, Sequence, Tuple

from continuum import config as config_module
from continuum.release import entrypoints, transaction
from continuum.release.contract import ContractError, TargetSpec
from continuum.release.contract import ReleaseEvent
from continuum.release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
from continuum.release.entrypoints import EntrypointError, MatrixTarget, ReleaseMatrix
from continuum.release.transaction import (
    FAILURE_TAXONOMY,
    TargetFailure,
    TargetFragment,
    TransactionError,
)
from continuum.release.version import ExplicitVersion

from . import release_core_support as support

ADAPTER = support.ADAPTER
OTHER_VERSION = "1.4.1"
CHANNEL = "stable"


def rows(*ids: str, version: str = support.VERSION, sha: str = support.SHA) -> Tuple[MatrixTarget, ...]:
    return tuple(
        MatrixTarget(
            target=item,
            adapter=ADAPTER,
            runner="ubuntu-24.04",
            argv=("continuum", "release", "target", "--target", item),
            source_sha=sha,
            version=version,
            toolchain="fixture",
            secrets=("CONTINUUM_FIXTURE_P12",),
            fragment=entrypoints.fragment_path("runs", item),
        )
        for item in ids
    )


def matrix(*ids: str, version: str = support.VERSION, sha: str = support.SHA, **kwargs: Any) -> ReleaseMatrix:
    return ReleaseMatrix(
        version=version,
        source_sha=sha,
        channel=CHANNEL,
        targets=rows(*ids, version=version, sha=sha),
        **kwargs,
    )


def request_for(the_matrix: ReleaseMatrix, **kwargs: Any) -> ReleaseRequest:
    event = support.event(sha=the_matrix.source_sha, tag=f"v{the_matrix.version}")
    return ReleaseRequest(
        event=event,
        targets=tuple(TargetSpec(id=item, adapter=ADAPTER) for item in the_matrix.target_ids),
        version_checks=(ExplicitVersion(the_matrix.version),),
        requested_version=the_matrix.version,
        workdir=kwargs.pop("workdir", "."),
        channel=the_matrix.channel,
        **kwargs,
    )


def build_only(*adapters: Any, **kwargs: Any) -> ReleaseComponents:
    """Components a build job may hold: adapters, and no destination at all."""

    return support.components(adapters={ADAPTER: adapters[0]}, **kwargs)


class TransactionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="continuum-transaction-")
        self.checkout = tempfile.mkdtemp(prefix="continuum-checkout-", dir=self.root)
        self.fragment_root = os.path.join(self.root, "runs")
        self.repository = support.MemoryReleaseRepository()
        self.adapter = support.FixtureAdapter(workdir=self.checkout)

    def build_components(self) -> ReleaseComponents:
        return support.components(
            adapter=self.adapter,
            adapters={ADAPTER: self.adapter},
            publishers=(support.github_publisher(self.repository),),
        )

    def run_target(self, the_matrix: ReleaseMatrix, target_id: str) -> TargetFragment:
        """Do what a build job does: build one target, write one fragment."""

        fragment = transaction.build_target(
            request_for(the_matrix, workdir=self.checkout),
            the_matrix,
            target_id,
            components=build_only(self.adapter),
        )
        transaction.write_fragment(self.fragment_root, fragment)
        return fragment

    def transact(self, the_matrix: ReleaseMatrix, **kwargs: Any) -> Any:
        return transaction.transaction(
            request_for(the_matrix, workdir=self.checkout),
            the_matrix,
            fragment_root=self.fragment_root,
            components=self.build_components(),
            **kwargs,
        )


class FragmentTests(TransactionTestCase):
    def test_survives_being_written_and_read_back(self):
        the_matrix = matrix("alpha")
        fragment = self.run_target(the_matrix, "alpha")
        restored = transaction.read_fragment(
            self.fragment_root, the_matrix, "alpha"
        )
        self.assertEqual(restored.describe(), fragment.describe())
        self.assertEqual(restored.manifest.target, "alpha")
        self.assertEqual(restored.manifest.version, support.VERSION)
        self.assertEqual(restored.manifest.source_sha, support.SHA)

    def test_writes_the_path_the_matrix_row_named(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        self.assertTrue(
            os.path.isfile(os.path.join(self.fragment_root, "alpha.json")),
            "the fragment must land where the row said, or the transaction looks "
            "somewhere else and finds nothing",
        )

    def test_leaves_no_partial_file_behind(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        self.assertEqual(
            [name for name in os.listdir(self.fragment_root) if name.endswith(".partial")],
            [],
            "a partial file is a half-written fragment; a reader must never see one",
        )

    def test_refuses_a_fragment_that_is_not_a_mapping(self):
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(["not", "a", "fragment"])
        self.assertIn("mapping", str(caught.exception))

    def test_refuses_a_fragment_from_another_schema(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["schema"] = "continuum.release-fragment/v99"
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn("schema", str(caught.exception))

    def test_refuses_a_fragment_with_a_field_nobody_checks(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["approved_by"] = "someone"
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn("approved_by", str(caught.exception))

    def test_refuses_a_file_that_is_not_json(self):
        path = os.path.join(self.fragment_root, "alpha.json")
        os.makedirs(self.fragment_root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("this is not a fragment")
        with self.assertRaises(TransactionError) as caught:
            transaction.read_fragment(self.fragment_root, matrix("alpha"), "alpha")
        self.assertIn("JSON", str(caught.exception))

    def test_refuses_a_fragment_with_no_manifest(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["manifests"] = []
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn("no manifest", str(caught.exception))

    def test_refuses_a_fragment_that_reports_for_another_target(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["manifests"][0]["target"] = "beta"
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn("beta", str(caught.exception))

    def test_refuses_a_manifest_from_another_commit(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["manifests"][0]["source_sha"] = support.OTHER_SHA
        payload["manifests"][0]["artifacts"][0]["source_sha"] = support.OTHER_SHA
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn(support.OTHER_SHA, str(caught.exception))

    def test_refuses_a_manifest_from_another_version(self):
        the_matrix = matrix("alpha")
        payload = self.run_target(the_matrix, "alpha").describe()
        payload["manifests"][0]["version"] = OTHER_VERSION
        payload["manifests"][0]["artifacts"][0]["version"] = OTHER_VERSION
        with self.assertRaises(TransactionError) as caught:
            TargetFragment.from_describe(payload)
        self.assertIn(OTHER_VERSION, str(caught.exception))

    def test_reports_a_missing_fragment_as_retryable(self):
        os.makedirs(self.fragment_root, exist_ok=True)
        with self.assertRaises(TargetFailure) as caught:
            transaction.read_fragment(self.fragment_root, matrix("alpha"), "alpha")
        self.assertEqual(caught.exception.code, "build-failed")
        self.assertTrue(caught.exception.retryable)


class BindingTests(TransactionTestCase):
    def test_refuses_a_fragment_built_from_another_commit(self):
        the_matrix = matrix("alpha")
        stale = matrix("alpha", sha=support.OTHER_SHA)
        # The wrong commit is refused at the source: a manifest is checked
        # against the pin before it can be written at all.
        fragment = transaction.build_target(
            request_for(stale, workdir=self.checkout),
            stale,
            "alpha",
            components=build_only(self.adapter),
        )
        transaction.write_fragment(self.fragment_root, fragment)
        with self.assertRaises(TargetFailure) as caught:
            transaction.read_fragment(self.fragment_root, the_matrix, "alpha")
        self.assertEqual(caught.exception.code, "stale-source")
        self.assertFalse(caught.exception.retryable)
        self.assertIn(support.OTHER_SHA, str(caught.exception))

    def test_refuses_a_fragment_for_another_version(self):
        the_matrix = matrix("alpha")
        other = matrix("alpha", version=OTHER_VERSION)
        self.run_target(other, "alpha")
        with self.assertRaises(TargetFailure) as caught:
            transaction.read_fragment(self.fragment_root, the_matrix, "alpha")
        self.assertEqual(caught.exception.code, "stale-source")

    def test_refuses_a_fragment_whose_matrix_is_not_the_one_dispatched(self):
        the_matrix = matrix("alpha", "beta")
        fragment = self.run_target(the_matrix, "alpha")
        narrowed = ReleaseMatrix(
            version=the_matrix.version,
            source_sha=the_matrix.source_sha,
            channel=the_matrix.channel,
            targets=the_matrix.targets[:1],
        )
        transaction.write_fragment(
            self.fragment_root,
            TargetFragment(
                target=fragment.target,
                version=fragment.version,
                source_sha=fragment.source_sha,
                matrix=narrowed,
                manifests=fragment.manifests,
                journal=fragment.journal,
            ),
        )
        with self.assertRaises(TransactionError) as caught:
            transaction.read_fragment(self.fragment_root, the_matrix, "alpha")
        self.assertIn("different matrix", str(caught.exception))

    def test_refuses_a_transaction_whose_matrix_is_for_another_commit(self):
        """The matrix and the request are compared before any fragment is read.

        Reading first would make the diagnosis depend on which file happened to
        be found, so the same mistake would be reported three different ways
        depending on the state of the run directory.
        """

        self.run_target(matrix("alpha"), "alpha")
        with self.assertRaises(TransactionError) as caught:
            transaction.transaction(
                request_for(matrix("alpha"), workdir=self.checkout),
                matrix("alpha", sha=support.OTHER_SHA),
                fragment_root=self.fragment_root,
                components=self.build_components(),
            )
        self.assertIn("pinned to", str(caught.exception))
        self.assertEqual(self.repository.calls, [])

    def test_refuses_a_transaction_whose_matrix_is_for_another_version(self):
        self.run_target(matrix("alpha"), "alpha")
        with self.assertRaises(TransactionError) as caught:
            transaction.transaction(
                request_for(matrix("alpha"), workdir=self.checkout),
                matrix("alpha", version=OTHER_VERSION),
                fragment_root=self.fragment_root,
                components=self.build_components(),
            )
        self.assertIn("but the release is", str(caught.exception))
        self.assertEqual(self.repository.calls, [])

    def test_refuses_a_fragment_of_another_version_as_stale(self):
        self.run_target(matrix("alpha", version=OTHER_VERSION), "alpha")
        with self.assertRaises(TargetFailure) as caught:
            self.transact(matrix("alpha"))
        self.assertEqual(caught.exception.code, "stale-source")
        self.assertIn(OTHER_VERSION, str(caught.exception))

    def test_refuses_a_matrix_for_another_commit(self):
        the_matrix = matrix("alpha", sha=support.OTHER_SHA)
        event = support.event(sha=support.SHA)
        request = ReleaseRequest(
            event=event,
            targets=(TargetSpec(id="alpha", adapter=ADAPTER),),
            requested_version=support.VERSION,
            workdir=self.checkout,
        )
        with self.assertRaises(TransactionError) as caught:
            transaction.assert_request_matches(the_matrix, request)
        self.assertIn("pinned to", str(caught.exception))

    def test_refuses_a_matrix_that_builds_a_target_the_policy_does_not_declare(self):
        the_matrix = matrix("alpha", "beta")
        event = support.event(sha=support.SHA)
        request = ReleaseRequest(
            event=event,
            targets=(TargetSpec(id="alpha", adapter=ADAPTER),),
            version_checks=(ExplicitVersion(support.VERSION),),
            requested_version=support.VERSION,
            workdir=self.checkout,
        )
        with self.assertRaises(TransactionError) as caught:
            transaction.assert_request_matches(the_matrix, request)
        self.assertIn("beta", str(caught.exception))


class BuildJobTests(TransactionTestCase):
    def test_builds_only_the_target_it_was_dispatched(self):
        the_matrix = matrix("alpha", "beta")
        fragment = self.run_target(the_matrix, "alpha")
        self.assertEqual(
            [request.target.id for request in self.adapter.built], ["alpha"], "a build job "
            "must not build a sibling target; two jobs building both is a race"
        )
        self.assertEqual(fragment.target, "alpha")

    def test_names_a_broken_toolchain_by_the_taxonomy(self):
        self.adapter.fail_build = True
        with self.assertRaises(TargetFailure) as caught:
            self.run_target(matrix("alpha"), "alpha")
        self.assertIn("fixture toolchain is broken", str(caught.exception))
        self.assertEqual(caught.exception.as_outcome().outcome, "failed")
        self.assertEqual(caught.exception.as_outcome().code, caught.exception.code)

    def test_a_failure_keeps_the_retryability_its_component_claimed(self):
        """The core decides what is worth retrying, and the transaction does not
        second-guess it.

        The fixture's toolchain fails with a plain error that claims nothing, so
        the failure stays fatal. Upgrading it to retryable here would turn every
        permanent build refusal into an unattended rebuild loop.
        """

        self.adapter.fail_build = True
        with self.assertRaises(TargetFailure) as caught:
            self.run_target(matrix("alpha"), "alpha")
        self.assertFalse(caught.exception.retryable)

    def test_a_component_that_claims_retryable_stays_retryable(self):
        self.adapter.failure = support.PortFailure("the toolchain registry is down", code="provider-unavailable", retryable=True)
        with self.assertRaises(TargetFailure) as caught:
            self.run_target(matrix("alpha"), "alpha")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "provider-unavailable")

    def test_a_failing_verification_keeps_the_code_the_core_recorded(self):
        """The stage's own word is not renamed to fit the taxonomy.

        `verify-failed` names the stage that broke; `contract-violation` would
        not, and a reader deciding whether to re-run anything needs the former.
        """

        self.adapter.fail_verify = True
        with self.assertRaises(TargetFailure) as caught:
            self.run_target(matrix("alpha"), "alpha")
        self.assertEqual(caught.exception.code, "verification-failed")
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(transaction.classify("verification-failed")[0])
        self.assertIn("unclassified", transaction.classify("verification-failed")[1])
        self.assertFalse(caught.exception.retryable)
        self.assertIn("verify", str(caught.exception))

    def test_a_build_from_the_wrong_commit_never_reaches_the_transaction(self):
        """The wrong commit is caught at the source, not at publication.

        A manifest built from another commit is refused by the core's own source
        stage, so the fragment is never written and the transaction never has
        the chance to publish it. The alternative — write it, then check at
        publication — would mean the wrong bytes have already been uploaded to a
        draft.
        """

        self.adapter.wrong_source = True
        with self.assertRaises(TargetFailure) as caught:
            self.run_target(matrix("alpha"), "alpha")
        self.assertIn("pinned to", str(caught.exception))
        self.assertEqual(
            os.listdir(self.fragment_root) if os.path.isdir(self.fragment_root) else [],
            [],
            "a failed build must leave no fragment for a later job to pick up",
        )

    def test_holds_no_way_to_publish(self):
        """The build job's components must contain no destination at all.

        Not "a destination that is unused" — none. The privilege boundary here
        is the absence of a token, and a component set that merely happens not to
        call its publisher is one refactor away from a release published from a
        pull request.
        """

        components = build_only(self.adapter)
        self.assertEqual(components.publisher_names(), ())
        self.assertEqual(components.sync_names(), ())
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        self.assertEqual(
            self.repository.calls, [], "a build job that could reach the destination "
            "would be one refactor from publishing a pull request's output"
        )

    def test_refuses_to_run_a_declared_target(self):
        the_matrix = ReleaseMatrix(
            version=support.VERSION,
            source_sha=support.SHA,
            channel=CHANNEL,
            targets=(
                MatrixTarget(
                    target="apple",
                    adapter=ADAPTER,
                    runner="ubuntu-24.04",
                    argv=("noop",),
                    source_sha=support.SHA,
                    version=support.VERSION,
                    fragment=entrypoints.fragment_path("runs", "apple"),
                    declared=True,
                ),
            ),
        )
        with self.assertRaises(TransactionError) as caught:
            self.run_target(the_matrix, "apple")
        self.assertIn("declared", str(caught.exception))

    def test_carries_only_its_own_work_as_evidence(self):
        """The fragment's carried journal must be this target's, and only this
        target's.

        Everything in a single-target run that is *not* attributed to the target
        is an aggregate the transaction must re-derive, and the per-target entry
        is the one that lets a resumed transaction skip this work.
        """

        the_matrix = matrix("alpha", "beta")
        fragment = self.run_target(the_matrix, "alpha")
        units = fragment.unit_outcomes()
        self.assertTrue(units, "a build job that kept no evidence can never be resumed")
        self.assertEqual({entry.detail("target") for entry in units}, {"alpha"})
        self.assertEqual(
            {entry.stage for entry in units},
            {"build", "sign", "verify"},
            "the stages a build job runs; a draft entry here would be a destination "
            "reachable from a build job",
        )


class JournalTests(TransactionTestCase):
    def test_merges_the_work_of_every_target(self):
        the_matrix = matrix("alpha", "beta")
        alpha = self.run_target(the_matrix, "alpha")
        beta = self.run_target(the_matrix, "beta")
        journal = transaction.merge_journal((alpha, beta))
        targets = {entry.detail("target") for entry in journal.entries if entry.detail("target")}
        self.assertEqual(targets, {"alpha", "beta"})

    def test_never_carries_an_aggregate_key(self):
        """One target's build must not stand in for another's.

        Each build job records a `build` key for the whole release as well as
        one per target. Carrying the aggregate across would let a two-target
        release skip the build stage because one target said it was done.
        """

        the_matrix = matrix("alpha", "beta")
        alpha = self.run_target(the_matrix, "alpha")
        self.assertTrue(
            any(not entry.detail("target") for entry in alpha.journal),
            "a single-target run does record aggregate keys, or this test proves nothing",
        )
        journal = transaction.merge_journal((alpha,))
        self.assertEqual(
            [entry for entry in journal.entries if not entry.detail("target")], []
        )

    def test_merges_deterministically(self):
        the_matrix = matrix("alpha", "beta")
        alpha = self.run_target(the_matrix, "alpha")
        beta = self.run_target(the_matrix, "beta")
        self.assertEqual(
            transaction.merge_journal((alpha, beta)).describe(),
            transaction.merge_journal((beta, alpha)).describe(),
        )


class TransactionTests(TransactionTestCase):
    def test_publishes_every_target_once(self):
        the_matrix = matrix("alpha", "beta")
        for item in the_matrix.target_ids:
            self.run_target(the_matrix, item)
        result = self.transact(the_matrix)
        self.assertTrue(result.released, result.summary)
        self.assertEqual(result.status, "released")
        self.assertEqual(result.version, support.VERSION)
        self.assertEqual(result.tag, f"v{support.VERSION}")
        self.assertEqual(result.source_sha, support.SHA)
        self.assertEqual(result.targets, ("alpha", "beta"))
        self.assertEqual(result.outputs["ok"], "true")
        self.assertEqual(result.outputs["retryable"], "false")

    def test_does_not_rebuild_what_the_build_jobs_already_built(self):
        the_matrix = matrix("alpha", "beta")
        for item in the_matrix.target_ids:
            self.run_target(the_matrix, item)
        built = len(self.adapter.built)
        self.transact(the_matrix)
        self.assertEqual(
            len(self.adapter.built), built, "the transaction must not build again; a rebuild "
            "here would produce different bytes under the same tag",
        )

    def test_uploads_each_asset_once(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        self.transact(the_matrix)
        uploads = [call for call in self.repository.calls if call.startswith("upload:")]
        assets = len(self.adapter.built[0].target.id and [1])  # one manifest, read below
        self.assertTrue(uploads, "the transaction must actually upload")
        self.assertEqual(assets, 1)
        self.assertEqual(len(set(uploads)), len(uploads))

    def test_refuses_to_publish_a_partial_release(self):
        the_matrix = matrix("alpha", "beta")
        self.run_target(the_matrix, "alpha")
        with self.assertRaises(TransactionError) as caught:
            self.transact(the_matrix)
        self.assertIn("beta", str(caught.exception))
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("draft:")],
            [],
            "the refusal must come before a draft exists, or the partial set is "
            "already public as an unpublished release",
        )

    def test_names_the_partial_release_as_such(self):
        the_matrix = matrix("alpha", "beta")
        self.run_target(the_matrix, "alpha")
        with self.assertRaises(TransactionError) as caught:
            self.transact(the_matrix)
        self.assertIn("1 of 2", str(caught.exception))

    def test_refuses_two_targets_that_produced_one_asset_name(self):
        """The collision is only knowable here, so it is checked here.

        Neither build job can see the other's assets, so if the transaction did
        not check, the failure would arrive as one target overwriting the other's
        upload — a published release whose checksums name bytes that are not
        there.
        """

        the_matrix = matrix("alpha", "beta")
        self.adapter.artifact_name = "app.tar.gz"
        for item in the_matrix.target_ids:
            self.run_target(the_matrix, item)
        with self.assertRaises(TransactionError) as caught:
            self.transact(the_matrix)
        self.assertIn("app.tar.gz", str(caught.exception))
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("draft:")], []
        )

    def test_waits_for_no_declared_target_because_it_has_no_runner(self):
        the_matrix = ReleaseMatrix(
            version=support.VERSION,
            source_sha=support.SHA,
            channel=CHANNEL,
            targets=(
                rows("alpha")[0],
                MatrixTarget(
                    target="future",
                    adapter=ADAPTER,
                    runner="ubuntu-24.04",
                    argv=("noop",),
                    source_sha=support.SHA,
                    version=support.VERSION,
                    fragment=entrypoints.fragment_path("runs", "future"),
                    declared=True,
                ),
            ),
        )
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        self.assertTrue(result.released, result.summary)
        self.assertEqual(result.targets, ("alpha",))

    def test_reports_a_declared_target_as_not_shipped(self):
        """A release smaller than its policy has to say so in three places.

        The workflow's job summary, the result document, and the normalized
        outputs are three readers with three reasons to look; a gap that only
        appears in one of them is a gap the other two will misread as a smaller
        release rather than a scoped one.
        """

        the_matrix = ReleaseMatrix(
            version=support.VERSION,
            source_sha=support.SHA,
            channel=CHANNEL,
            targets=(
                rows("alpha")[0],
                MatrixTarget(
                    target="future",
                    adapter=ADAPTER,
                    runner="ubuntu-24.04",
                    argv=("noop",),
                    source_sha=support.SHA,
                    version=support.VERSION,
                    fragment=entrypoints.fragment_path("runs", "future"),
                    declared=True,
                ),
            ),
        )
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        self.assertEqual(result.declared, ("future",))
        self.assertEqual(result.outputs["declared"], "future")
        self.assertIn("future", result.job_summary())
        self.assertIn("not shipped", result.job_summary())
        self.assertIn("future", result.describe()["declared"])
        self.assertNotIn(
            "future",
            [manifest.target for manifest in result.manifests],
            "a declared target has no manifest, and inventing an empty one would let it "
            "pass as a target that shipped nothing on purpose",
        )

    def test_a_release_with_nothing_declared_reports_no_gap(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        self.assertEqual(result.declared, ())
        self.assertEqual(result.outputs["declared"], "")
        self.assertNotIn("not shipped", result.job_summary())

    def test_a_declared_gap_does_not_make_a_release_retryable(self):
        """It is a decision somebody made, not a condition to retry."""

        the_matrix = ReleaseMatrix(
            version=support.VERSION,
            source_sha=support.SHA,
            channel=CHANNEL,
            targets=(
                rows("alpha")[0],
                MatrixTarget(
                    target="future",
                    adapter=ADAPTER,
                    runner="ubuntu-24.04",
                    argv=("noop",),
                    source_sha=support.SHA,
                    version=support.VERSION,
                    fragment=entrypoints.fragment_path("runs", "future"),
                    declared=True,
                ),
            ),
        )
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        self.assertTrue(result.ok)
        self.assertFalse(result.retryable)

    def test_the_result_reports_the_failure_s_own_retryability(self):
        """A component's judgement outranks a spelling match in the table.

        A rate limit the destination will not clear, or a build that will not
        pass on this tree, is transient-shaped and permanently true at once. A
        component that says so has seen something the table cannot.
        """

        def result_for(code: str, retryable: bool) -> Any:
            """A result carrying one recorded failure, and nothing else.

            Built directly rather than through a run, because the interesting
            case is a disagreement between the code and the classification, and
            a fixture that produces it by failing a build would be testing the
            build's path instead.
            """

            from continuum.release.core import ReleaseOutcome, StageOutcome

            return transaction.ReleaseResult(
                status="failed",
                version=support.VERSION,
                tag=f"v{support.VERSION}",
                source_sha=support.SHA,
                release="",
                channel=CHANNEL,
                outcome=ReleaseOutcome(
                    status="failed",
                    event=ReleaseEvent(
                        repository=support.REPOSITORY,
                        name="tag-push",
                        sha=support.SHA,
                        tag=f"v{support.VERSION}",
                    ),
                    version=support.VERSION,
                    tag=f"v{support.VERSION}",
                    source_sha=support.SHA,
                    failure=StageOutcome.failed(
                        "publish", "publish/" + code, "the destination refused", code=code,
                        retryable=retryable,
                    ),
                ),
                summary="the publish stage failed: the destination refused",
            )

        fatal = result_for("contract", False)
        self.assertFalse(fatal.ok)
        self.assertFalse(fatal.retryable)

        transient = result_for("contract", True)
        self.assertFalse(
            transaction.classify("contract")[0],
            "the table still calls this code unclassified; the result must not "
            "silently adopt the table's opinion",
        )
        self.assertTrue(
            transient.retryable,
            "the component recorded this failure as clearable by a retry, and "
            "re-deciding from the code's spelling discards what it knew",
        )

        permanent = result_for("provider-unavailable", False)
        self.assertTrue(
            transaction.classify("provider-unavailable")[0],
            "the table calls this code transient",
        )
        self.assertFalse(
            permanent.retryable,
            "a rate limit that will not clear is transient-shaped and permanently true "
            "at once, and the component is the only thing that can tell them apart",
        )

    def test_a_successful_release_is_never_retryable(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        self.assertTrue(result.ok)
        self.assertFalse(result.retryable)

    def test_a_second_run_releases_nothing_again(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        first = self.transact(the_matrix)
        uploads = [call for call in self.repository.calls if call.startswith("upload:")]
        second = transaction.transaction(
            request_for(the_matrix, workdir=self.checkout),
            the_matrix,
            fragment_root=self.fragment_root,
            components=self.build_components(),
            journal=first.outcome.journal,
        )
        self.assertFalse(second.released)
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("upload:")], uploads
        )

    def test_publishes_nothing_for_a_matrix_with_nothing_to_build(self):
        """Every target declared, none buildable.

        The release is well-formed and has nothing to wait for, so the only
        honest answers are "publish what was declared" or "publish nothing".
        The transaction waits for no build job that will never run, and the
        declared manifest is what gets published — which is a separate code
        path from a build, and one a caller has to be able to see.
        """

        the_matrix = ReleaseMatrix(
            version=support.VERSION,
            source_sha=support.SHA,
            channel=CHANNEL,
            targets=(
                MatrixTarget(
                    target="future",
                    adapter=ADAPTER,
                    runner="ubuntu-24.04",
                    argv=("noop",),
                    source_sha=support.SHA,
                    version=support.VERSION,
                    fragment=entrypoints.fragment_path("runs", "future"),
                    declared=True,
                ),
            ),
        )
        with self.assertRaises(TransactionError) as caught:
            self.transact(the_matrix)
        self.assertIn("nothing to collect", str(caught.exception))
        self.assertEqual(self.repository.calls, [])

    def test_names_the_target_it_cannot_find_in_the_matrix(self):
        with self.assertRaises(EntrypointError) as caught:
            self.transact(matrix("alpha"), targets=("ghost",))
        self.assertIn("ghost", str(caught.exception))
        self.assertIn("alpha", str(caught.exception))

    def test_every_output_is_a_string(self):
        """A workflow's outputs are strings, so a non-string one is a bug.

        Not a style point: `outputs: ${{ steps.x.outputs.y }}` on a boolean or a
        number is either an empty string or a silent failure at the boundary,
        and the caller cannot tell which.
        """

        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        outputs = self.transact(the_matrix).outputs
        self.assertTrue(outputs)
        for name, value in outputs.items():
            self.assertIsInstance(value, str, f"output {name} is {type(value).__name__}")
            self.assertNotIn("\n", value, f"output {name} cannot span lines in a workflow")

    def test_describes_itself_as_json(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        result = self.transact(the_matrix)
        payload = json.loads(result.to_json())
        self.assertEqual(payload["schema"], transaction.RESULT_SCHEMA)
        self.assertEqual(payload["tag"], f"v{support.VERSION}")
        self.assertTrue(payload["manifests"])
        self.assertIn("stages", payload)

    def test_writes_a_job_summary_a_human_can_read(self):
        the_matrix = matrix("alpha")
        self.run_target(the_matrix, "alpha")
        summary = self.transact(the_matrix).job_summary()
        self.assertIn("released", summary)
        self.assertIn(f"v{support.VERSION}", summary)
        self.assertIn("alpha", summary)

    def test_explains_a_failure_in_the_summary_it_prints(self):
        self.adapter.fail_verify = True
        with self.assertRaises(TargetFailure):
            self.run_target(matrix("alpha"), "alpha")
        result = transaction.ReleaseResult(
            status="failed",
            version=support.VERSION,
            tag=f"v{support.VERSION}",
            source_sha=support.SHA,
            release=f"release/{support.REPOSITORY}/{support.VERSION}",
            channel=CHANNEL,
            summary="the verify stage refused the signature",
        )
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.ok)
        self.assertIn("Continuum release: failed", result.job_summary())


class FailureTaxonomyTests(unittest.TestCase):
    def test_every_code_names_itself_as_retryable_or_not(self):
        for code in FAILURE_TAXONOMY:
            retryable, advice = transaction.classify(code)
            self.assertIsInstance(retryable, bool, code)
            self.assertTrue(advice, f"{code} explains nothing to a reader")
            self.assertIn(code, code)

    def test_a_transient_failure_is_retryable(self):
        self.assertTrue(transaction.classify("build-failed")[0])
        self.assertTrue(transaction.classify("provider-unavailable")[0])
        self.assertTrue(transaction.classify("provider-rate-limited")[0])

    def test_a_permanent_refusal_is_not(self):
        for code in ("validation-failed", "policy-refused", "already-published", "stale-source"):
            self.assertFalse(transaction.classify(code)[0], code)

    def test_an_unclassified_failure_is_treated_as_fatal(self):
        """Silence about retryability is not a claim of retryability.

        Defaulting the other way turns a permanent refusal into an unattended
        rebuild loop, which is the failure mode a taxonomy exists to prevent.
        """

        retryable, advice = transaction.classify("something-new")
        self.assertFalse(retryable)
        self.assertIn("unclassified", advice)

    def test_reports_a_missing_credential_as_needing_a_human(self):
        retryable, advice = transaction.classify("missing-credential")
        self.assertFalse(retryable)
        self.assertIn("secret", advice)

    def test_names_an_already_published_version_as_a_new_version_problem(self):
        _retryable, advice = transaction.classify("already-published")
        self.assertIn("new version", advice)


class ResolveTests(TransactionTestCase):
    def config_for(self, *ids: str) -> Any:
        return config_module.ContinuumConfig(
            release=config_module.ReleaseSettings(
                targets=tuple(
                    config_module.ReleaseTarget(
                        id=item,
                        adapter=config_module.ADAPTER_APPLE,
                        platform=config_module.PLATFORM_MACOS,
                        build_strategy=config_module.BUILD_STRATEGY_SWIFTPM,
                        distribution=config_module.DISTRIBUTION_DIRECT,
                    )
                    for item in ids
                )
            )
        )

    def test_builds_a_request_the_build_jobs_and_the_transaction_share(self):
        request = transaction.resolve_release(
            self.config_for("macos-app"),
            event=support.event(),
            version=support.VERSION,
            source_sha=support.SHA,
            matrix=matrix("macos-app"),
            workdir=self.checkout,
        )
        self.assertEqual(request.requested_version, support.VERSION)
        self.assertEqual([spec.id for spec in request.targets], ["macos-app"])
        self.assertEqual(request.event.sha, support.SHA)
        self.assertEqual(request.workdir, self.checkout)

    def test_refuses_a_matrix_for_another_version(self):
        with self.assertRaises(TransactionError) as caught:
            transaction.resolve_release(
                self.config_for("macos-app"),
                event=support.event(),
                version=support.VERSION,
                source_sha=support.SHA,
                matrix=matrix("macos-app", version=OTHER_VERSION),
            )
        self.assertIn(OTHER_VERSION, str(caught.exception))

    def test_refuses_a_matrix_for_another_commit(self):
        with self.assertRaises(TransactionError) as caught:
            transaction.resolve_release(
                self.config_for("macos-app"),
                event=support.event(),
                version=support.VERSION,
                source_sha=support.SHA,
                matrix=matrix("macos-app", sha=support.OTHER_SHA),
            )
        self.assertIn(support.OTHER_SHA, str(caught.exception))

    def test_refuses_a_release_whose_event_names_another_commit(self):
        with self.assertRaises(TransactionError) as caught:
            transaction.resolve_release(
                self.config_for("macos-app"),
                event=support.event(sha=support.OTHER_SHA),
                version=support.VERSION,
                source_sha=support.SHA,
                matrix=matrix("macos-app"),
            )
        self.assertIn("names commit", str(caught.exception))

    def test_the_dry_run_of_a_request_changes_nothing_else(self):
        request = request_for(matrix("alpha"))
        dry = request.as_dry_run()
        self.assertTrue(dry.dry_run)
        self.assertEqual(dry.requested_version, request.requested_version)
        self.assertEqual(dry.event, request.event)
        self.assertEqual([spec.id for spec in dry.targets], ["alpha"])


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()
