"""The GitHub Release baseline, tested against a dict shaped like the API.

The destination is the part of a release that talks to something else, so the
properties asserted here are the ones a repository owner would be upset by if
they broke: a draft is never published with an asset missing from it, an
attached asset is never replaced, a release never points at a commit it was not
approved for, and a plan of the whole thing writes nothing at all.

The fixture repository raises on a second upload under a name it already holds,
so "an attached asset is never replaced" is enforced by the destination rather
than only promised by the publisher.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from continuum.release import contract
from continuum.release.contract import (
    Artifact,
    ArtifactManifest,
    ManifestBuilder,
    PublishRequest,
    ReleaseEvent,
    ReleaseNotes,
    VerificationReport,
)
from continuum.release.github import (
    DESTINATION,
    DRY_RUN_CODE,
    AssetSetReport,
    GitHubReleasePublisher,
    PortFailure,
    ReleaseAsset,
    ReleaseRecord,
    compare_asset_set,
)
from continuum.release.state import Journal

from . import release_core_support as support

SHA = support.SHA
OTHER_SHA = support.OTHER_SHA
VERSION = support.VERSION
CLASSIFIER = support.CLASSIFIER


class GitHubTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.repository = support.MemoryReleaseRepository()
        self.provenance = support.MemoryProvenance()
        self.publisher = support.github_publisher(self.repository, self.provenance)
        self.manifest = self.built_manifest()

    # -- fixtures -----------------------------------------------------------

    def build_one(self, *, name: str = "app-1.4.0-linux-x86_64.txt", body: str = "hello") -> str:
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def built_manifest(
        self,
        *,
        target: str = "fixture",
        name: str = "app-1.4.0-linux-x86_64.txt",
        source_sha: str = SHA,
        body: str = "hello",
        declared: bool = False,
        with_checksums: bool = True,
        provenance: str = contract.PROVENANCE_ABSENT,
    ) -> ArtifactManifest:
        builder = ManifestBuilder(
            target=target, adapter="fixture", source_sha=source_sha, version=VERSION
        )
        path = self.build_one(name=name, body=body)
        if declared:
            builder.declare(name, path, "binary", classifier=CLASSIFIER)
            return builder.build()
        builder.record(
            name,
            path,
            "binary",
            classifier=CLASSIFIER,
            signing=contract.SIGNING_SIGNED,
            signing_identity=support.IDENTITY,
            verification=contract.VERIFICATION_VERIFIED,
            verified_by=support.VERIFIER,
            provenance=provenance,
        )
        if with_checksums:
            listing = f"{target}-SHA256SUMS.txt"
            path = builder.build().write_checksums(self.directory, listing)
            builder.record(
                listing,
                path,
                contract.TYPE_CHECKSUMS,
                classifier=contract.TYPE_CHECKSUMS,
                media_type="text/plain",
                verification=contract.VERIFICATION_VERIFIED,
                verified_by=support.VERIFIER,
            )
        return builder.build()

    def request(self, *manifests: ArtifactManifest, **overrides) -> PublishRequest:
        values = {
            "destination": DESTINATION,
            "tag": f"v{VERSION}",
            "version": VERSION,
            "source_sha": SHA,
            "manifests": manifests or (self.manifest,),
            "key": "continuum-draft-key",
        }
        values.update(overrides)
        return PublishRequest(**values)

    def held(self, tag: str = f"v{VERSION}"):
        return sorted(asset.name for asset in self.repository.attached(tag))


class AssetSetTests(GitHubTestCase):
    def test_a_set_that_holds_everything_is_complete(self):
        attached = tuple(
            ReleaseAsset(
                name=artifact.name,
                size=artifact.size,
                digest=artifact.digest,
                digest_algorithm=artifact.digest_algorithm,
            )
            for artifact in self.manifest.artifacts
        )
        report = compare_asset_set((self.manifest,), attached)
        self.assertTrue(report.complete)
        self.assertEqual(report.missing, ())
        self.assertEqual(report.mismatched, ())
        self.assertEqual(report.expected, attached)

    def test_a_missing_asset_is_named_rather_than_counted(self):
        attached = (
            ReleaseAsset(
                name=self.manifest.artifacts[0].name,
                size=self.manifest.artifacts[0].size,
                digest=self.manifest.artifacts[0].digest,
                digest_algorithm="sha256",
            ),
        )
        report = compare_asset_set((self.manifest,), attached)
        self.assertFalse(report.complete)
        self.assertEqual(report.missing, (self.manifest.artifacts[1].name,))

    def test_an_asset_with_the_wrong_digest_is_a_mismatch_not_a_missing_one(self):
        wrong = ReleaseAsset(
            name=self.manifest.artifacts[0].name, size=1, digest="f" * 64
        )
        attached = (wrong,) + tuple(
            ReleaseAsset(
                name=artifact.name,
                size=artifact.size,
                digest=artifact.digest,
                digest_algorithm=artifact.digest_algorithm,
            )
            for artifact in self.manifest.artifacts[1:]
        )
        report = compare_asset_set((self.manifest,), attached)
        self.assertEqual(report.mismatched, (self.manifest.artifacts[0].name,))
        self.assertEqual(report.missing, ())

    def test_a_report_describes_itself_for_a_job_log(self):
        described = compare_asset_set((self.manifest,), ()).describe()
        self.assertEqual(
            described["missing"], [artifact.name for artifact in self.manifest.artifacts]
        )
        self.assertFalse(described["complete"])


class DraftTests(GitHubTestCase):
    def test_a_draft_is_created_holding_every_asset(self):
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, contract.PUBLISHED)
        self.assertEqual(result.code, "drafted")
        self.assertTrue(self.repository.releases["v1.4.0"].draft)
        self.assertEqual(
            self.held(), ["app-1.4.0-linux-x86_64.txt", "fixture-SHA256SUMS.txt"]
        )

    def test_drafting_twice_uploads_nothing_the_second_time(self):
        first = self.publisher.draft(self.request())
        second = self.publisher.draft(self.request())
        self.assertTrue(second.ok)
        self.assertEqual(second.detail("uploaded"), "-")
        self.assertEqual(
            second.detail("reused"),
            "app-1.4.0-linux-x86_64.txt,fixture-SHA256SUMS.txt",
        )
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("upload:")],
            [
                "upload:v1.4.0:app-1.4.0-linux-x86_64.txt",
                "upload:v1.4.0:fixture-SHA256SUMS.txt",
            ],
        )
        self.assertEqual(first.external_id, second.external_id)

    def test_a_resumed_draft_attaches_only_what_is_missing(self):
        self.publisher.draft(self.request())
        # The run died after the first upload. The resumed attempt finds one
        # asset already there, reuses it, and attaches the rest.
        self.repository.held["v1.4.0"].pop("app-1.4.0-linux-x86_64.txt")
        self.repository.calls.clear()
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("upload:")],
            ["upload:v1.4.0:app-1.4.0-linux-x86_64.txt"],
        )
        self.assertEqual(result.detail("uploaded"), "app-1.4.0-linux-x86_64.txt")
        self.assertEqual(result.detail("reused"), "fixture-SHA256SUMS.txt")

    def test_an_asset_that_is_already_attached_with_another_digest_is_refused(self):
        self.publisher.draft(self.request())
        again = self.built_manifest(body="a different build")
        result = self.publisher.draft(self.request(again))
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "asset-conflict")
        self.assertFalse(result.retryable)
        self.assertIn("app-1.4.0-linux-x86_64.txt", result.reason)

    def test_a_draft_that_points_at_another_commit_is_refused(self):
        self.publisher.draft(self.request())
        held = self.repository.releases["v1.4.0"]
        self.repository.releases["v1.4.0"] = ReleaseRecord(
            id=held.id,
            tag=held.tag,
            target_sha=OTHER_SHA,
            draft=True,
            name=held.name,
        )
        result = self.publisher.draft(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "draft-source-conflict")
        self.assertFalse(result.retryable)

    def test_an_artifact_that_is_not_on_this_runner_is_refused_rather_than_skipped(self):
        from dataclasses import replace

        manifest = self.built_manifest()
        gone = replace(
            manifest.artifacts[0], path=os.path.join(self.directory, "not-there.txt")
        )
        result = self.publisher.draft(
            self.request(replace(manifest, artifacts=(gone,) + manifest.artifacts[1:]))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "artifact-missing")
        self.assertTrue(result.retryable)

    def test_an_upload_that_fails_is_reported_as_retryable_and_names_the_asset(self):
        name = "app-1.4.0-linux-x86_64.txt"
        self.repository.fail_upload[name] = PortFailure("the connection dropped", code="upload-failed")
        result = self.publisher.draft(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "upload-failed")
        self.assertTrue(result.retryable)
        self.assertIn(name, result.reason)

    def test_two_targets_assets_land_in_one_draft(self):
        other = self.built_manifest(target="other", name="other-1.4.0-linux-x86_64.txt")
        result = self.publisher.draft(self.request(self.manifest, other))
        self.assertTrue(result.ok)
        self.assertEqual(
            self.held(),
            [
                "app-1.4.0-linux-x86_64.txt",
                "fixture-SHA256SUMS.txt",
                "other-1.4.0-linux-x86_64.txt",
                "other-SHA256SUMS.txt",
            ],
        )

    def test_two_targets_claiming_one_name_are_refused_before_anything_is_uploaded(self):
        other = self.built_manifest(target="other", name=self.manifest.artifacts[0].name, body="other")
        with self.assertRaises(contract.ContractError) as caught:
            self.publisher.draft(self.request(self.manifest, other))
        self.assertIn("claimed by", str(caught.exception))
        self.assertEqual([call for call in self.repository.calls if ":" in call and not call.startswith("find:")], [])


class ProvenanceTests(GitHubTestCase):
    def test_an_artifact_that_declares_provenance_is_attested(self):
        manifest = self.built_manifest(provenance=contract.PROVENANCE_DECLARED)
        result = self.publisher.draft(self.request(manifest))
        self.assertTrue(result.ok)
        self.assertEqual(len(self.provenance.attested), 1)
        self.assertEqual(self.provenance.attested[0][0], manifest.artifacts[0].name)
        self.assertIn(manifest.artifacts[0].name, result.detail("attestations"))

    def test_an_artifact_that_declares_nothing_is_not_attested(self):
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(self.provenance.attested, [])

    def test_an_attestation_that_fails_leaves_the_release_a_draft(self):
        class Broken:
            SUPPORTS_DRY_RUN = True
            name = "broken-provenance"

            def intent(self):
                return "attest an artifact, and fail"

            def attest(self, **kwargs):
                raise PortFailure("the signing service is down", code="attestation-offline")

        publisher = GitHubReleasePublisher(
            repository=self.repository,
            provenance=Broken(),
            name="github",
            destination=DESTINATION,
            notes=support.FixtureNotes(),
        )
        manifest = self.built_manifest(provenance=contract.PROVENANCE_DECLARED)
        result = publisher.draft(self.request(manifest))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "attestation-offline")
        self.assertTrue(self.repository.releases["v1.4.0"].draft)


class PublishTests(GitHubTestCase):
    def _drafted(self) -> PublishRequest:
        self.publisher.draft(self.request())
        return self.request()

    def test_a_complete_draft_is_published(self):
        result = self.publisher.publish(self._drafted())
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, contract.PUBLISHED)
        self.assertEqual(result.code, "published")
        self.assertEqual(result.external_version, "v1.4.0")
        self.assertFalse(self.repository.releases["v1.4.0"].draft)
        self.assertEqual(result.detail("url"), "https://example.invalid/releases/v1.4.0")
        self.assertEqual(result.detail("assets"), "2")

    def test_a_release_missing_an_asset_is_not_published(self):
        request = self._drafted()
        self.repository.held["v1.4.0"].pop("fixture-SHA256SUMS.txt")
        result = self.publisher.publish(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "asset-set-incomplete")
        self.assertTrue(result.retryable)
        self.assertTrue(self.repository.releases["v1.4.0"].draft)

    def test_a_release_holding_the_wrong_bytes_is_not_published(self):
        request = self._drafted()
        self.repository.held["v1.4.0"]["app-1.4.0-linux-x86_64.txt"] = ReleaseAsset(
            name="app-1.4.0-linux-x86_64.txt", size=5, digest="a" * 64
        )
        result = self.publisher.publish(request)
        self.assertFalse(result.ok)
        self.assertIn(
            result.code, ("asset-digest-mismatch", "asset-set-incomplete", "asset-conflict")
        )
        self.assertFalse(result.retryable)
        self.assertTrue(self.repository.releases["v1.4.0"].draft)

    def test_a_release_pointing_at_another_commit_is_not_published(self):
        request = self._drafted()
        self.repository.releases["v1.4.0"] = ReleaseRecord(
            id="release-1", tag="v1.4.0", target_sha=OTHER_SHA, draft=True, name="v1.4.0"
        )
        result = self.publisher.publish(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "release-source-conflict")
        self.assertFalse(result.retryable)

    def test_publishing_a_release_that_does_not_exist_is_retryable(self):
        result = self.publisher.publish(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "release-absent")
        self.assertTrue(result.retryable)

    def test_publishing_twice_is_a_no_op_not_a_second_publication(self):
        self.publisher.publish(self._drafted())
        again = self.publisher.publish(self.request())
        self.assertTrue(again.ok)
        self.assertEqual(again.outcome, contract.SKIPPED)
        self.assertEqual(again.code, "already-published")
        self.assertEqual(
            [call for call in self.repository.calls if call.startswith("publish:")],
            ["publish:v1.4.0"],
        )

    def test_a_promotion_that_fails_is_retryable_and_leaves_the_draft(self):
        request = self._drafted()
        self.repository.fail_publish = PortFailure("the API is down", code="api-down")
        failed = self.publisher.publish(request)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.code, "api-down")
        self.assertTrue(failed.retryable)
        self.assertTrue(self.repository.releases["v1.4.0"].draft)
        self.repository.fail_publish = None
        self.assertTrue(self.publisher.publish(request).ok)

    def test_a_checksum_file_that_describes_something_else_is_refused(self):
        from dataclasses import replace

        # The interesting case is the one the asset-set check cannot see: a
        # manifest whose recorded checksum row agrees with what is attached, and
        # whose checksum row disagrees with the artifacts it sits beside. A
        # checksum file that a consumer trusts is the one asset nobody reads the
        # manifest for, so it is checked against the manifest as well.
        drafted = self.built_manifest()
        self.publisher.draft(self.request(drafted))
        honest = self.built_manifest()
        stale = replace(
            honest,
            artifacts=tuple(
                replace(artifact, digest="b" * 64)
                if artifact.type == contract.TYPE_CHECKSUMS
                else artifact
                for artifact in honest.artifacts
            ),
        )
        stale_row = next(
            artifact
            for artifact in stale.artifacts
            if artifact.type == contract.TYPE_CHECKSUMS
        )
        self.repository.held["v1.4.0"]["fixture-SHA256SUMS.txt"] = ReleaseAsset(
            name=stale_row.name, size=stale_row.size, digest="b" * 64
        )
        result = self.publisher.publish(self.request(stale))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "checksums-mismatch")
        self.assertTrue(self.repository.releases["v1.4.0"].draft)

    def test_a_checksum_file_that_agrees_publishes(self):
        result = self.publisher.publish(self._drafted())
        self.assertTrue(result.ok)


class RefusalTests(GitHubTestCase):
    def test_a_manifest_that_is_not_publishable_is_refused_before_anything_is_written(self):
        # A manifest from another commit or another version cannot be put in a
        # request at all — `tests/test_release_contract.py` covers that at the
        # value level. What the publisher's own gate is for is a caller that
        # reaches past those constructors, and the case it is asked about most is
        # the one that is easy to reach by accident: artifacts nobody checked.
        result = self.publisher.draft(self.request(self.unverified_manifest()))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "manifest-not-publishable")
        self.assertFalse(result.retryable)
        self.assertEqual(self.repository.releases, {})
        self.assertIn("app.txt", result.reason)

    def unverified_manifest(self) -> ArtifactManifest:
        builder = ManifestBuilder(
            target="fixture", adapter="fixture", source_sha=SHA, version=VERSION
        )
        path = self.build_one(name="app.txt")
        builder.record(name="app.txt", path=path, type="binary")
        return builder.build()

    def test_an_artifact_that_was_never_verified_is_refused(self):
        builder = ManifestBuilder(
            target="fixture", adapter="fixture", source_sha=SHA, version=VERSION
        )
        path = self.build_one(name="app.txt")
        builder.record(name="app.txt", path=path, type="binary")
        manifest = builder.build()
        result = self.publisher.draft(self.request(manifest))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "manifest-not-publishable")


class DryRunTests(GitHubTestCase):
    def declared(self) -> ArtifactManifest:
        builder = ManifestBuilder(
            target="fixture", adapter="fixture", source_sha=SHA, version=VERSION
        )
        for artifact in self.manifest.artifacts:
            builder.declare(
                artifact.name, artifact.path, artifact.type, classifier=artifact.classifier
            )
        return builder.build()

    def request(self, *manifests, **overrides):
        values = {
            "destination": DESTINATION,
            "tag": f"v{VERSION}",
            "version": VERSION,
            "source_sha": SHA,
            "manifests": manifests or (self.declared(),),
            "key": "continuum-draft-key",
            "dry_run": True,
        }
        values.update(overrides)
        return PublishRequest(**values)

    def test_a_plan_of_a_draft_writes_nothing(self):
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.code, DRY_RUN_CODE)
        self.assertEqual(self.repository.releases, {})
        self.assertEqual([call for call in self.repository.calls if call.startswith("draft:")], [])

    def test_a_plan_of_a_draft_says_what_it_would_do(self):
        result = self.publisher.draft(self.request())
        self.assertIn("would", result.reason.lower())
        self.assertEqual(result.identity, f"v1.4.0@{SHA}")

    def test_a_plan_of_a_publication_does_not_look_anything_up(self):
        result = self.publisher.publish(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.code, DRY_RUN_CODE)
        self.assertEqual(self.repository.calls, [])

    def test_a_plan_is_exempt_from_the_publishability_gate(self):
        # A plan's artifacts are declared, so the invariants a real run insists
        # on are exactly what it is reporting are not yet true.
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertFalse(result.failed)

    def test_a_plan_of_a_draft_reports_what_a_draft_already_exists(self):
        self.publisher.draft(self.request(dry_run=False))
        self.repository.calls.clear()
        result = self.publisher.draft(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.code, DRY_RUN_CODE)
        self.assertEqual([call for call in self.repository.calls if not call.startswith("find:")], [])


class IdentityTests(GitHubTestCase):
    def test_the_destination_says_what_it_is_for(self):
        self.assertIn("draft", self.publisher.intent())
        self.assertIn("verify", self.publisher.intent())
        self.assertEqual(self.publisher.destination, DESTINATION)

    def test_a_publisher_with_no_provenance_says_so_in_its_intent(self):
        publisher = GitHubReleasePublisher(
            repository=self.repository,
            name="github",
            destination=DESTINATION,
            notes=support.FixtureNotes(),
        )
        self.assertNotIn("attest", publisher.intent())

    def test_a_publisher_with_an_unnamed_provenance_port_is_refused(self):  # noqa: D401
        class Anonymous:
            SUPPORTS_DRY_RUN = True
            name = ""

            def intent(self):
                return "attest"

            def attest(self, **kwargs):
                return ""

        with self.assertRaises(contract.ContractError):
            GitHubReleasePublisher(
                repository=self.repository,
                provenance=Anonymous(),
                name="github",
                destination=DESTINATION,
                notes=support.FixtureNotes(),
            )


if __name__ == "__main__":
    unittest.main()
