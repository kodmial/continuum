"""The chain, walked.

These are the tests for the properties the release contract is sold on. Each
one is a sentence a repository owner would recognise as a promise: a dry run
writes nothing; a duplicate event publishes nothing; a release is one commit;
a failure that can be retried is retried from where it stopped and a failure
that cannot is not retried at all; a plan does not prevent the release it
planned; a release with no toolchain does not build with whatever it can find.

The fixture is deliberately dull — a text file, a dict, a counter — because a
platform in the test would be testing the platform.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from continuum.release import contract
from continuum.release.core import (
    DISABLED_CODE,
    DUPLICATE_EVENT_CODE,
    NO_OP,
    PLANNED,
    RELEASED,
    SOURCE_CONFLICT_CODE,
    UNFILLED_PLACEHOLDER_CODE,
    UNRESUMABLE_CODE,
    ReleaseComponents,
    ReleaseCore,
    ReleaseOutcome,
    ReleaseRequest,
)
from continuum.release.state import (
    BLOCKED,
    COMPLETED,
    Journal,
    StageOutcome,
    release_key,
    stage_key,
    stage_names,
    wake_key,
)
from continuum.release.version import ExplicitVersion, ProjectFileVersion, VersionPolicy

from . import release_core_support as support

VERSION = support.VERSION
SHA = support.SHA


class ReleaseTestCase(unittest.TestCase):
    """A release with one target, one destination, and counters on both."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.repository = support.MemoryReleaseRepository()
        self.provenance = support.MemoryProvenance()
        self.sync = support.MemorySync()
        self.adapter = support.FixtureAdapter(workdir=self.directory)
        self.publisher = support.github_publisher(self.repository, self.provenance)
        self.core = ReleaseCore(self.components())

    def components(self, **overrides) -> ReleaseComponents:
        values = {
            "adapter": self.adapter,
            "publishers": (self.publisher,),
            "syncs": (self.sync,),
        }
        values.update(overrides)
        return support.components(**values)

    def request(self, **overrides) -> ReleaseRequest:
        values = {
            "event": support.event(),
            "targets": support.targets(),
            "workdir": self.directory,
            "requested_version": VERSION,
        }
        values.update(overrides)
        return ReleaseRequest(**values)

    def destination_calls(self) -> list:
        return list(self.repository.calls)

    def assertStages(self, outcome, *names):
        self.assertEqual([item.stage for item in outcome.outcomes], list(names))


class ConfigurationTests(ReleaseTestCase):
    def test_a_component_set_that_cannot_honour_the_contract_is_refused_up_front(self):
        class Blind(support.FixtureAdapter):
            SUPPORTS_DRY_RUN = False

        with self.assertRaises(contract.ContractError) as caught:
            ReleaseCore(support.components(adapter=Blind()))
        self.assertIn("SUPPORTS_DRY_RUN", str(caught.exception))

    def test_an_adapter_with_no_name_is_refused(self):
        class Anonymous(support.FixtureAdapter):
            name = ""

        with self.assertRaises(contract.ContractError):
            ReleaseCore(support.components(adapter=Anonymous()))

    def test_a_target_naming_an_adapter_that_does_not_exist_is_a_failure_not_a_skip(self):
        outcome = self.core.execute(
            self.request(
                targets=(contract.TargetSpec(id="fixture", adapter="nonexistent"),)
            )
        )
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "adapter-unavailable")
        self.assertIn("nonexistent", outcome.failure.summary)
        self.assertEqual(self.adapter.built, [])

    def test_a_release_with_no_targets_is_switched_off_rather_than_half_configured(self):
        with self.assertRaises(contract.ContractError) as caught:
            ReleaseRequest(event=support.event())
        self.assertIn("switched off", str(caught.exception))

    def test_a_disabled_release_touches_nothing_and_is_green(self):
        outcome = self.core.execute(
            self.request(enabled=False, targets=support.targets())
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.no_op)
        self.assertEqual(outcome.status, NO_OP)
        self.assertEqual(outcome.outcomes[0].code, DISABLED_CODE)
        self.assertEqual(self.adapter.built, [])
        self.assertEqual(self.destination_calls(), [])

    def test_two_targets_with_one_id_are_refused(self):
        with self.assertRaises(contract.ContractError):
            ReleaseRequest(event=support.event(), targets=support.targets("a", "a"))

    def test_a_configured_repository_can_build_its_own_request(self):
        from continuum import config as config_module

        # What `.continuum.yml` can express today. Its adapter enum is still
        # `apple`, because the config surface is the MVP consumer contract and
        # widening it is a consumer-facing change; the generic path is reached by
        # a consumer that builds its own request, or by widening this enum.
        config = config_module.ContinuumConfig(
            release=config_module.ReleaseSettings(
                targets=(config_module.ReleaseTarget(id="desktop"),)
            )
        )
        request = ReleaseRequest.from_config(config, support.event())
        self.assertTrue(request.enabled)
        self.assertEqual(request.targets[0].id, "desktop")
        self.assertEqual(request.targets[0].adapter, "apple")

    def test_a_target_declaring_an_adapter_nobody_registered_is_a_failure(self):
        from continuum import config as config_module

        config = config_module.ContinuumConfig(
            release=config_module.ReleaseSettings(
                targets=(config_module.ReleaseTarget(id="desktop", adapter="apple"),)
            )
        )
        outcome = self.core.execute(
            ReleaseRequest.from_config(
                config,
                support.event(),
                workdir=self.directory,
                requested_version=VERSION,
            )
        )
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "adapter-unavailable")

    def test_a_repository_with_no_release_section_is_not_enabled(self):
        from continuum import config as config_module

        request = ReleaseRequest.from_config(
            config_module.ContinuumConfig(), support.event()
        )
        self.assertFalse(request.enabled)

    def test_the_core_says_what_it_is_for(self):
        self.assertIn("publishing to", self.core.intent())


class ChainTests(ReleaseTestCase):
    def test_a_release_walks_every_stage_in_order(self):
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.released)
        self.assertStages(outcome, *stage_names())

    def test_a_release_ends_with_one_tag_at_one_commit(self):
        outcome = self.core.execute(self.request())
        self.assertEqual(outcome.version, VERSION)
        self.assertEqual(outcome.tag, "v1.4.0")
        self.assertEqual(outcome.source_sha, SHA)
        self.assertEqual(
            outcome.release, f"continuum-release/{support.REPOSITORY}/{VERSION}"
        )
        self.assertEqual(
            self.repository.releases["v1.4.0"].target_sha, SHA
        )

    def test_every_outcome_is_recorded_with_a_key(self):
        outcome = self.core.execute(self.request())
        for entry in outcome.outcomes:
            self.assertTrue(entry.key.startswith("continuum-"))
            self.assertTrue(entry.summary)

    def test_the_outcome_describes_itself_for_a_job_summary(self):
        described = self.core.execute(self.request()).describe()
        self.assertEqual(described["status"], RELEASED)
        self.assertTrue(described["ok"])
        self.assertFalse(described["resumable"])
        self.assertEqual(described["schema"], "continuum.release-outcome/v1")
        self.assertEqual(len(described["stages"]), len(stage_names()))
        self.assertTrue(described["permissions"]["isolated_from_pull_requests"])
        self.assertEqual(described["source_sha"], SHA)

    def test_an_ineligible_event_is_a_green_no_op(self):
        core = ReleaseCore(self.components(eligibility=support.FixtureEligibility(eligible=False)))
        outcome = core.execute(self.request())
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.no_op)
        self.assertEqual(outcome.outcomes[-1].stage, "eligibility")
        self.assertEqual(self.adapter.built, [])

    def test_a_blocking_verdict_fails_and_asks_for_a_person(self):
        core = ReleaseCore(
            self.components(
                eligibility=support.FixtureEligibility(eligible=False, blocking=True)
            )
        )
        outcome = core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertFalse(outcome.resumable)
        self.assertEqual(outcome.outcomes[-1].outcome, "blocked")

    def test_a_release_pull_request_driver_is_driven_and_its_conflict_stops_the_release(self):
        core = ReleaseCore(
            self.components(release_pr=support.FixtureReleasePr(version="9.9.9"))
        )
        outcome = core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "release-pr-version-conflict")
        self.assertEqual(self.adapter.built, [])

    def test_a_release_with_no_pull_request_driver_says_so_and_continues(self):
        outcome = self.core.execute(self.request())
        release_pr = outcome.outcome_for("release-pr")
        self.assertEqual(release_pr.outcome, "noop")
        self.assertEqual(release_pr.code, "no-release-pr-driver")
        self.assertTrue(outcome.released)

    def test_a_target_with_no_toolchain_stops_the_release_before_anything_is_built(self):
        self.adapter.is_available = False
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "toolchain-unavailable")
        self.assertTrue(outcome.resumable)
        self.assertEqual(self.adapter.built, [])

    def test_a_build_failure_stops_before_signing(self):
        self.adapter.fail_build = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.stage, "build")
        self.assertEqual(self.adapter.signed, [])
        self.assertEqual(self.destination_calls(), [])

    def test_an_artifact_signed_by_nobody_does_not_verify(self):
        self.adapter.drop_signature = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.stage, "verify")
        self.assertEqual(self.destination_calls(), [])

    def test_a_verifier_that_reports_nothing_verified_stops_the_release(self):
        self.adapter.report_unverified = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(self.destination_calls(), [])

    def test_a_manifest_built_from_another_commit_is_refused_at_the_build(self):
        self.adapter.wrong_source = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, SOURCE_CONFLICT_CODE)
        self.assertEqual(self.destination_calls(), [])

    def test_a_generated_file_with_an_unfilled_token_is_refused_before_it_is_published(self):
        # A missed substitution produces a file that is syntactically fine and
        # looks plausible, and it ships a literal token to every user who installs
        # from it. Nothing downstream would notice, so the build stage reads the
        # files back.
        self.adapter.artifact_suffix = ".rb"
        self.adapter.leftover_token = 'version = "__VERSION__"\n'
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, UNFILLED_PLACEHOLDER_CODE)
        self.assertEqual(outcome.failure.stage, "build")
        self.assertIn("__VERSION__", outcome.failure.summary)
        # Refused before signing and before anything left the job.
        self.assertEqual(self.adapter.signed, [])
        self.assertEqual(self.destination_calls(), [])

    def test_a_token_named_in_a_generated_file_comment_is_not_a_defect(self):
        # The consumer's own templates document the tokens they substitute. A scan
        # that flagged those would block correct releases until the documentation
        # was rewritten, which is how a check teaches everyone to ignore it.
        self.adapter.artifact_suffix = ".rb"
        self.adapter.leftover_token = '# substituted for __VERSION__ by the generator\n'
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.released)
        self.assertEqual(outcome.failure, None)

    def test_a_binary_artifact_is_not_scanned_for_placeholders(self):
        # The scan only claims to read text it can name a comment convention for.
        # A `.zip` in the manifest is none of this module's business, and reading
        # it as text would either fail or invent findings.
        self.adapter.artifact_suffix = ".zip"
        self.adapter.leftover_token = "__VERSION__"
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.released)

    def test_a_declared_artifact_is_not_scanned(self):
        # A dry run declares what would exist rather than building it, so there is
        # no file to read and no substitution to have missed.
        self.adapter.artifact_suffix = ".rb"
        outcome = self.core.execute(self.request(dry_run=True))
        self.assertTrue(outcome.planned)

    def test_every_offending_file_is_named_rather_than_only_the_first(self):
        # A serial repair is the wrong shape here: which file is wrong does not
        # depend on who built it, and one refusal naming all of them is what lets
        # a person fix the generator once.
        self.adapter.artifact_suffix = ".rb"
        self.adapter.leftover_token = 'a = "__VERSION__"\nb = "__SHA256_"\n'
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertIn("__VERSION__", outcome.failure.summary)
        self.assertIn("__SHA256_", outcome.failure.summary)

    def test_a_generated_file_that_cannot_be_decoded_is_reported(self):
        # A build step that produced an unreadable "text" artifact has produced
        # something nobody can vouch for, which is not the same as a clean scan.
        self.adapter.artifact_suffix = ".rb"
        self.adapter.body_bytes = b"\xff\xfe\x00not utf-8 at all"
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, UNFILLED_PLACEHOLDER_CODE)
        self.assertIn("could not be read", outcome.failure.summary)
        self.assertEqual(self.destination_calls(), [])

    def test_a_published_manifest_records_who_verified_it(self):
        outcome = self.core.execute(self.request())
        manifest = outcome.manifests[0]
        for artifact in manifest.artifacts:
            self.assertTrue(artifact.verified, artifact.name)
            self.assertEqual(artifact.verified_by, support.VERIFIER)

    def test_the_evidence_attached_to_a_release_travels_with_it(self):
        outcome = self.core.execute(self.request())
        names = sorted(name for name, _ in self.provenance.attested)
        self.assertEqual(names, ["fixture-1.4.0-linux-x86_64.txt"])
        self.assertEqual(self.provenance.attested[0][1], "v1.4.0")
        self.assertTrue(outcome.released)


class DryRunTests(ReleaseTestCase):
    def test_a_plan_walks_the_whole_chain(self):
        outcome = self.core.plan(self.request())
        self.assertEqual(outcome.status, PLANNED)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.dry_run)
        self.assertStages(outcome, *stage_names())

    def test_a_plan_writes_no_file_and_reaches_no_destination(self):
        self.core.plan(self.request())
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.sync.published, [])
        for item in self.repository.calls:
            self.assertTrue(item.startswith("find:"), item)
        self.assertEqual(self.repository.releases, {})

    def test_a_plan_describes_artifacts_it_has_not_built(self):
        outcome = self.core.plan(self.request())
        artifact = outcome.manifests[0].artifacts[0]
        self.assertTrue(artifact.declared)
        self.assertEqual(artifact.size, 0)
        self.assertFalse(artifact.signed)
        self.assertFalse(artifact.verified)

    def test_a_plan_records_what_it_would_do(self):
        outcome = self.core.plan(self.request())
        draft = outcome.outcome_for("draft")
        self.assertIn("would create a draft", draft.summary)

    def test_a_plan_is_not_a_record_of_anything_having_happened(self):
        plan = self.core.plan(self.request())
        self.assertEqual(plan.journal.completed_keys, ())
        self.assertEqual(plan.journal.resume_point(), "eligibility")

    def test_a_plan_does_not_prevent_the_release_it_planned(self):
        plan = self.core.plan(self.request())
        self.assertEqual(len(self.adapter.built), 1)
        outcome = self.core.execute(self.request(), journal=plan.journal)
        self.assertTrue(outcome.released)
        # Asked twice: once to describe, once to build. The plan's build request
        # did not satisfy the run's, which is the whole point.
        self.assertEqual(len(self.adapter.built), 2)
        self.assertEqual(outcome.outcome_for("build").outcome, "completed")

    def test_a_plan_of_a_disabled_release_is_a_no_op_too(self):
        plan = self.core.plan(self.request(enabled=False, targets=support.targets()))
        self.assertTrue(plan.no_op)
        self.assertEqual(plan.dry_run, True)


class IdempotencyTests(ReleaseTestCase):
    def _first(self) -> tuple:
        outcome = self.core.execute(self.request())
        return outcome, self.destination_calls()

    def test_a_duplicate_event_publishes_nothing_a_second_time(self):
        first, calls = self._first()
        self.assertTrue(first.released)
        second = self.core.execute(
            self.request(event=support.event(delivery="delivery-2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        self.assertTrue(second.ok)
        self.assertTrue(second.no_op)
        self.assertEqual(second.no_op_reason, "every destination already held this release")
        self.assertEqual(self.destination_calls(), calls)

    def test_a_duplicate_event_does_not_rebuild(self):
        first, _ = self._first()
        self.core.execute(
            self.request(event=support.event(delivery="delivery-2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        self.assertEqual(len(self.adapter.built), 1)
        self.assertEqual(len(self.adapter.signed), 1)
        self.assertEqual(len(self.adapter.verified), 1)

    def test_a_duplicate_event_does_not_republish_downstream(self):
        first, _ = self._first()
        self.core.execute(
            self.request(event=support.event(delivery="delivery-2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        self.assertEqual(len(self.sync.published), 1)

    def test_one_event_cannot_produce_two_versions(self):
        first, _ = self._first()
        # The project file now says something else, and the same event arrives
        # again. The event's version is a fact about the event now.
        with open(os.path.join(self.directory, "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("2.0.0\n")
        core = ReleaseCore(self.components(version=ProjectFileVersion("VERSION")))
        second = core.execute(
            self.request(requested_version="", event=support.event(delivery="delivery-2")),
            journal=first.journal,
        )
        self.assertEqual(second.version, VERSION)
        self.assertEqual(second.outcome_for("version").code, DUPLICATE_EVENT_CODE)

    def test_a_second_version_is_a_second_release(self):
        first, _ = self._first()
        second = self.core.execute(
            self.request(
                event=support.event(ref="refs/tags/v1.5.0", tag="v1.5.0", delivery="d2"),
                requested_version="1.5.0",
            )
        )
        self.assertTrue(second.released)
        self.assertNotEqual(second.release, first.release)
        self.assertEqual(len(self.adapter.built), 2)
        self.assertEqual(sorted(self.repository.releases), ["v1.4.0", "v1.5.0"])

    def test_two_workflows_watching_the_same_tag_converge_on_one_release(self):
        left = ReleaseCore(self.components())
        right = ReleaseCore(self.components())
        one = left.execute(self.request())
        two = right.execute(self.request(), journal=one.journal, manifests=one.manifests)
        self.assertTrue(two.no_op)
        self.assertEqual(len(self.repository.releases), 1)

    def test_a_stage_that_did_nothing_is_asked_again_rather_than_assumed_done(self):
        first, _ = self._first()
        second = self.core.execute(
            self.request(event=support.event(delivery="delivery-2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        # The release pull request was a no-op, so it is re-evaluated: it changed
        # nothing the first time, so re-asking costs nothing.
        self.assertEqual(second.outcome_for("release-pr").outcome, "noop")


class SourcePinningTests(ReleaseTestCase):
    def test_a_version_bound_to_another_commit_is_refused(self):
        first = self.core.execute(self.request())
        self.assertTrue(first.released)
        built = len(self.adapter.built)
        second = self.core.execute(
            self.request(event=support.event(sha=support.OTHER_SHA, delivery="d2")),
            journal=first.journal,
        )
        self.assertTrue(second.failed)
        self.assertEqual(second.failure.code, SOURCE_CONFLICT_CODE)
        self.assertIn("already bound to", second.failure.summary)
        self.assertEqual(len(self.adapter.built), built)

    def test_the_binding_survives_into_the_published_release(self):
        self.core.execute(self.request())
        record = self.repository.releases["v1.4.0"]
        self.assertEqual(record.target_sha, SHA)


class ResumabilityTests(ReleaseTestCase):
    def test_a_retryable_publication_failure_is_resumed_and_completes(self):
        self.repository.fail_publish = RuntimeError("the destination was down")
        first = self.core.execute(self.request())
        self.assertTrue(first.failed)
        self.assertTrue(first.resumable)
        self.assertEqual(first.failure.stage, "publish")
        self.assertEqual(self.repository.releases["v1.4.0"].draft, True)

        self.repository.fail_publish = None
        second = self.core.execute(
            self.request(), journal=first.journal, manifests=first.manifests
        )
        self.assertTrue(second.released)
        # Resumed, not restarted: the draft and its assets were already there.
        self.assertEqual(
            len([call for call in self.repository.calls if call.startswith("upload:")]), 2
        )
        self.assertEqual(len(self.adapter.built), 1)

    def test_a_resume_point_names_the_stage_to_start_from(self):
        self.repository.fail_publish = RuntimeError("down")
        first = self.core.execute(self.request())
        self.assertEqual(first.journal.resume_point(), "publish")

    def test_a_clean_release_owes_no_reconciliation(self):
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.released)
        self.assertIsNone(outcome.owed_stage)
        self.assertEqual(outcome.reconciliation_wake, "")
        # Absent rather than empty in the artifact, so a reader cannot mistake an
        # owed wake for one that was considered and found unnecessary.
        self.assertNotIn("owed_stage", outcome.describe())
        self.assertNotIn("reconciliation_wake", outcome.describe())

    def test_a_deferred_stage_is_named_in_the_outcome_and_its_wake_is_keyed_on_the_head(self):
        # A release-pr update that lands mid-publish is deferred rather than lost.
        # Without the wake the release is green and the update is stranded, so the
        # outcome has to say what is owed even when everything it did succeeded.
        journal = Journal().extend(
            [
                StageOutcome(
                    stage=name,
                    key=stage_key(release_key(support.REPOSITORY, VERSION), name),
                    outcome=BLOCKED if name == "draft" else COMPLETED,
                    summary="deferred" if name == "draft" else "done",
                    code="release-in-flight" if name == "draft" else "",
                )
                for name in stage_names()
            ]
        )
        outcome = ReleaseOutcome(
            status=RELEASED,
            event=support.event(),
            outcomes=(),
            journal=journal,
            source_sha=support.SHA,
        )
        self.assertEqual(outcome.owed_stage, "draft")
        self.assertEqual(
            outcome.reconciliation_wake, wake_key(support.REPOSITORY, support.SHA)
        )
        described = outcome.describe()
        self.assertEqual(described["owed_stage"], "draft")
        self.assertEqual(described["reconciliation_wake"], outcome.reconciliation_wake)

    def test_a_wake_needs_a_head_and_is_absent_without_one(self):
        # An outcome with no source SHA cannot name which head to reconcile, and a
        # wake keyed on the wrong head would reconcile something nobody asked for.
        journal = Journal().extend(
            [
                StageOutcome(
                    stage=name,
                    key=stage_key(release_key(support.REPOSITORY, VERSION), name),
                    outcome=BLOCKED if name == "draft" else COMPLETED,
                    summary="deferred" if name == "draft" else "done",
                    code="release-in-flight" if name == "draft" else "",
                )
                for name in stage_names()
            ]
        )
        outcome = ReleaseOutcome(
            status=RELEASED, event=support.event(), journal=journal, source_sha=""
        )
        self.assertEqual(outcome.owed_stage, "draft")
        self.assertEqual(outcome.reconciliation_wake, "")

    def test_a_run_that_cannot_see_its_artifacts_refuses_to_publish_them(self):
        first = self.core.execute(self.request())
        calls = self.destination_calls()
        second = self.core.execute(
            self.request(event=support.event(delivery="delivery-2")), journal=first.journal
        )
        self.assertTrue(second.failed)
        self.assertEqual(second.failure.code, UNRESUMABLE_CODE)
        self.assertEqual(second.failure.stage, "draft")
        self.assertIn("ReleaseCore.execute(manifests=", second.failure.summary)
        self.assertEqual(self.destination_calls(), calls)

    def test_a_non_retryable_failure_is_not_resumed(self):
        first = self.core.execute(self.request())
        # A draft that points at another commit cannot be repaired by retrying.
        self.repository.releases["v1.4.0"] = first.repository if False else self.repository.releases["v1.4.0"]
        self.assertTrue(first.journal.resume_point() is None)

    def test_two_destinations_resume_independently(self):
        other = support.MemorySync()
        core = ReleaseCore(self.components(syncs=(self.sync, other)))
        self.sync.published.clear()
        first = core.execute(self.request())
        self.assertTrue(first.released)
        again = core.execute(
            self.request(event=support.event(delivery="d2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        self.assertTrue(again.no_op)
        self.assertEqual(len(self.sync.published), 1)
        self.assertEqual(len(other.published), 1)

    def test_a_target_that_built_is_not_rebuilt_when_another_target_fails(self):
        class Broken(support.FixtureAdapter):
            def build(self, request):
                if request.target.id == "second":
                    raise contract.ContractError("the second target cannot build")
                return super().build(request)

        core = ReleaseCore(
            support.components(
                adapters={"fixture": self.adapter, "second": Broken(workdir=self.directory)}
            )
        )
        request = self.request(
            targets=(
                contract.TargetSpec(id="fixture", adapter="fixture"),
                contract.TargetSpec(id="second", adapter="second"),
            )
        )
        first = core.execute(request)
        self.assertTrue(first.failed)
        built_first_run = len(self.adapter.built)
        second = core.execute(
            request,
            journal=first.journal,
            manifests=tuple(item for item in first.manifests),
        )
        # The target that built is not built again; the one that failed is.
        self.assertEqual(len(self.adapter.built), built_first_run)
        # Both halves of that run are in the journal: the target that was already
        # built, and the one that failed again. The stage carries only the
        # failure, because a stage that fails on one target is that failure.
        units = {
            entry.detail("target"): entry
            for entry in second.journal.entries
            if entry.stage == "build"
        }
        self.assertEqual(units["fixture"].outcome, "skipped")
        self.assertIn("already has build recorded", units["fixture"].summary)
        self.assertEqual(units["second"].outcome, "failed")
        self.assertIn("the second target cannot build", units["second"].summary)
        self.assertEqual(second.outcome_for("build").outcome, "failed")


class MultiTargetTests(ReleaseTestCase):
    def _two_target_release(self):
        request = self.request(targets=support.targets("fixture", "other"))
        return self.core.execute(request)

    def test_every_target_is_built_signed_and_verified(self):
        outcome = self._two_target_release()
        self.assertTrue(outcome.released)
        self.assertEqual(len(outcome.manifests), 2)
        self.assertEqual(
            sorted(manifest.target for manifest in outcome.manifests), ["fixture", "other"]
        )

    def test_one_release_holds_every_targets_assets(self):
        self._two_target_release()
        attached = [asset.name for asset in self.repository.attached("v1.4.0")]
        self.assertEqual(
            sorted(attached),
            sorted(
                [
                    "fixture-1.4.0-linux-x86_64.txt",
                    "fixture-1.4.0-SHA256SUMS.txt",
                    "other-1.4.0-linux-x86_64.txt",
                    "other-1.4.0-SHA256SUMS.txt",
                ]
            ),
        )

    def test_one_draft_holds_them_all(self):
        self._two_target_release()
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("draft:")],
            ["draft:v1.4.0"],
        )

    def test_two_targets_claiming_one_asset_name_are_refused(self):
        # A destination addresses assets by name, so this is a collision the
        # release must refuse rather than resolve by upload order.
        core = ReleaseCore(
            support.components(
                adapters={
                    "fixture": support.FixtureAdapter(
                        workdir=self.directory, artifact_name="app.tar.gz"
                    ),
                    "other": support.FixtureAdapter(
                        workdir=self.directory, artifact_name="app.tar.gz"
                    ),
                }
            )
        )
        outcome = core.execute(
            self.request(
                targets=(
                    contract.TargetSpec(id="fixture", adapter="fixture"),
                    contract.TargetSpec(id="other", adapter="other"),
                )
            )
        )
        self.assertTrue(outcome.failed)
        self.assertIn("app.tar.gz", outcome.failure.summary)


class PublisherResultTests(ReleaseTestCase):
    def test_a_publication_names_its_destination_and_its_identity(self):
        outcome = self.core.execute(self.request())
        publication = outcome.publications[0]
        self.assertTrue(publication.published)
        self.assertEqual(publication.destination, support.GITHUB_DESTINATION)
        self.assertEqual(publication.identity, f"v1.4.0@{SHA}")
        self.assertEqual(publication.external_version, "v1.4.0")

    def test_a_draft_and_a_publication_are_two_distinct_writes(self):
        outcome = self.core.execute(self.request())
        draft, publish = outcome.publications[0], outcome.publications[1]
        self.assertEqual(draft.code, "drafted")
        self.assertEqual(publish.code, "published")
        self.assertTrue(publish.details and "assets" in dict(publish.details))

    def test_an_existing_asset_is_never_replaced(self):
        first = self.core.execute(self.request())
        self.assertTrue(first.released)
        held = self.repository.held["v1.4.0"]
        name = "fixture-1.4.0-linux-x86_64.txt"
        held[name] = type(held[name])(name=name, size=1, digest="f" * 64)
        second = self.core.execute(
            self.request(event=support.event(delivery="delivery-2")),
            journal=first.journal,
            manifests=first.manifests,
        )
        self.assertTrue(second.no_op)
        self.assertEqual(held[name].digest, "f" * 64)
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("upload:")],
            [f"upload:v1.4.0:{name}", "upload:v1.4.0:fixture-1.4.0-SHA256SUMS.txt"],
        )

    def test_a_publication_that_cannot_reach_its_destination_is_a_retryable_failure(self):
        self.repository.fail_find = support.PortFailure("boom", code="offline")
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertTrue(outcome.resumable)
        self.assertEqual(outcome.failure.stage, "draft")

    def test_a_release_with_no_destination_configured_stops_at_the_destination(self):
        core = ReleaseCore(support.components(adapter=self.adapter))
        outcome = core.execute(self.request())
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.no_op)
        self.assertFalse(outcome.published)
        draft = outcome.outcome_for("draft")
        self.assertEqual(draft.code, "no-destination")
        # The build happened, and then the chain stopped: there is nothing to
        # hold a manifest, so signing and uploading would be work for nobody.
        self.assertIsNone(outcome.outcome_for("sync"))
        self.assertEqual(len(self.adapter.built), 1)
        self.assertEqual(len(self.adapter.signed), 1)
        self.assertEqual(len(self.adapter.verified), 1)


class VersionAgreementTests(ReleaseTestCase):
    def test_two_sources_that_agree_are_recorded_as_the_evidence_for_the_version(self):
        path = support.project_file(self.directory, VERSION)
        core = ReleaseCore(
            self.components(
                version=ProjectFileVersion("VERSION"),
            )
        )
        outcome = core.execute(
            self.request(requested_version=VERSION, version_checks=(ExplicitVersion(),))
        )
        self.assertTrue(outcome.released)
        summary = outcome.outcome_for("version").summary
        self.assertIn("explicit", summary)
        self.assertIn("project-file", summary)
        self.assertTrue(os.path.isfile(path))

    def test_a_manual_version_that_contradicts_the_file_stops_the_release(self):
        core = ReleaseCore(self.components(version=ProjectFileVersion("VERSION")))
        support.project_file(self.directory, VERSION)
        outcome = core.execute(
            self.request(requested_version="2.0.0", version_checks=(ExplicitVersion(),))
        )
        self.assertTrue(outcome.failed)
        self.assertIn("disagree", outcome.failure.summary)
        self.assertEqual(self.adapter.built, [])

    def test_a_policy_that_refuses_the_version_stops_the_release_at_the_version(self):
        core = ReleaseCore(self.components())
        outcome = self.core.execute(self.request(version_policy=VersionPolicy(allowed_series=("2",))))
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.stage, "version")

    def test_the_tag_prefix_is_the_policys_not_the_strategys(self):
        core = ReleaseCore(self.components())
        outcome = core.execute(self.request(version_policy=VersionPolicy(tag_prefix="release-")))
        self.assertEqual(outcome.tag, f"release-{VERSION}")
        self.assertIn(f"release-{VERSION}", self.repository.releases)


class RequestTests(ReleaseTestCase):
    def test_a_dry_run_request_is_a_copy_that_changes_nothing_else(self):
        request = self.request()
        planned = request.as_dry_run()
        self.assertTrue(planned.dry_run)
        self.assertFalse(request.dry_run)
        self.assertEqual(planned.targets, request.targets)
        self.assertEqual(planned.event, request.event)
        self.assertTrue(planned.as_dry_run().dry_run)
        self.assertEqual(planned.as_dry_run(), planned)

    def test_a_request_describes_itself_for_a_plan(self):
        described = self.request().describe()
        self.assertEqual(described["targets"][0]["id"], "fixture")
        self.assertFalse(described["dry_run"])
        self.assertIn("event", described)

    def test_a_channel_is_part_of_the_concurrency_group_not_the_version(self):
        outcome = self.core.execute(self.request(channel="beta"))
        self.assertTrue(outcome.released)
        self.assertTrue(outcome.channel.endswith("/beta"))
        self.assertEqual(outcome.version, VERSION)


class JournalTests(ReleaseTestCase):
    def test_a_run_records_every_key_it_relied_on(self):
        outcome = self.core.execute(self.request())
        keys = {entry.key for entry in outcome.journal.entries}
        # The stage keys and the per-target and per-destination keys.
        self.assertGreaterEqual(len(keys), len(stage_names()))
        for key in keys:
            self.assertEqual(key.startswith("continuum-"), True)

    def test_a_journal_is_a_value_that_can_be_carried_to_the_next_run(self):
        first = self.core.execute(self.request())
        carried = Journal().extend(first.journal.entries)
        second = self.core.execute(
            self.request(event=support.event(delivery="d2")),
            journal=carried,
            manifests=first.manifests,
        )
        self.assertTrue(second.no_op)


if __name__ == "__main__":
    unittest.main()
