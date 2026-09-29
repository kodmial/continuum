"""The values every release is described in.

These are the assertions that would be most expensive to get wrong later,
because everything above them assumes them: a manifest is one target from one
commit of one version, a digest is a fact rather than a claim, and a publisher
result is one of three words. A release is only as trustworthy as the types it
is assembled from, so those types are tested on their own.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from continuum.release import contract
from continuum.release.contract import (
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    PublishRequest,
    ReleaseEvent,
    TargetSpec,
    VerificationReport,
    digest_file,
    failed,
    published,
    skipped,
)

from . import release_core_support as support

SHA = support.SHA
VERSION = support.VERSION


def artifact(**overrides) -> Artifact:
    values = {
        "target": "fixture",
        "source_sha": SHA,
        "version": VERSION,
        "name": "app.tar.gz",
        "path": "/tmp/app.tar.gz",
        "type": "archive",
        "size": 3,
        "digest": hashlib.sha256(b"abc").hexdigest(),
    }
    values.update(overrides)
    return Artifact(**values)


def manifest(*artifacts: Artifact, **overrides) -> ArtifactManifest:
    values = {
        "target": "fixture",
        "source_sha": SHA,
        "version": VERSION,
        "adapter": "fixture",
        "artifacts": artifacts if artifacts else (artifact(),),
    }
    values.update(overrides)
    return ArtifactManifest(**values)


def other_manifest(name: str = "other.tar.gz") -> ArtifactManifest:
    """A second target's manifest, for the multi-target cases."""

    return ArtifactManifest(
        target="other",
        source_sha=SHA,
        version=VERSION,
        adapter="other",
        artifacts=(
            Artifact(
                target="other",
                source_sha=SHA,
                version=VERSION,
                name=name,
                path=f"/tmp/{name}",
                type="archive",
                size=4,
                digest=hashlib.sha256(b"other").hexdigest(),
            ),
        ),
    ).mark_verified("ci")


class EventTests(unittest.TestCase):
    def test_a_repository_is_an_owner_and_a_name(self):
        with self.assertRaises(ContractError):
            ReleaseEvent(repository="widgets", name="push", sha=SHA)

    def test_a_tag_push_carries_its_tag(self):
        with self.assertRaises(ContractError) as caught:
            ReleaseEvent(repository="owner/widgets", name="tag-push", sha=SHA)
        self.assertIn("tag", str(caught.exception))

    def test_the_source_must_be_a_commit(self):
        with self.assertRaises(ContractError):
            ReleaseEvent(repository="owner/widgets", name="push", sha="not-a-sha")

    def test_a_delivery_does_not_change_the_event(self):
        one = support.event(delivery="first")
        two = support.event(delivery="second")
        self.assertNotEqual(one.delivery, two.delivery)
        self.assertEqual(
            (one.repository, one.name, one.sha), (two.repository, two.name, two.sha)
        )


class ArtifactTests(unittest.TestCase):
    def test_a_digest_is_checked_for_shape_as_well_as_length(self):
        with self.assertRaises(ContractError):
            artifact(digest="z" * 64)
        with self.assertRaises(ContractError):
            artifact(digest="ab")

    def test_an_artifact_cannot_claim_a_signature_it_does_not_have(self):
        with self.assertRaises(ContractError):
            artifact(signing=contract.SIGNING_SIGNED, signing_identity="")

    def test_a_declared_artifact_cannot_claim_to_be_signed_or_verified(self):
        with self.assertRaises(ContractError):
            artifact(declared=True, signing=contract.SIGNING_SIGNED, signing_identity="ci")
        with self.assertRaises(ContractError):
            artifact(
                declared=True,
                verification=contract.VERIFICATION_VERIFIED,
                verified_by="ci",
            )

    def test_an_artifact_must_belong_to_the_release_that_carries_it(self):
        with self.assertRaises(ContractError) as caught:
            artifact().assert_release_source("b" * 40)
        self.assertIn(SHA, str(caught.exception))

    def test_identity_names_the_bytes_not_the_file(self):
        self.assertEqual(artifact().identity, f"app.tar.gz@sha256:{artifact().digest}")


class ManifestTests(unittest.TestCase):
    def test_one_manifest_is_one_target(self):
        with self.assertRaises(ContractError):
            manifest(artifact(target="other"))

    def test_a_name_appears_once(self):
        with self.assertRaises(ContractError):
            manifest(artifact(), artifact(digest=hashlib.sha256(b"xyz").hexdigest()))

    def test_a_manifest_with_nothing_in_it_is_refused(self):
        with self.assertRaises(ContractError) as caught:
            ArtifactManifest(
                target="fixture", source_sha=SHA, version=VERSION, adapter="fixture", artifacts=()
            )
        self.assertIn("built nothing", str(caught.exception))

    def test_publishing_refuses_anything_not_from_the_approved_commit(self):
        with self.assertRaises(ContractError):
            manifest().assert_publishable("b" * 40, VERSION)
        with self.assertRaises(ContractError):
            manifest().assert_publishable(SHA, "9.9.9")

    def test_publishing_refuses_an_unverified_artifact(self):
        unsigned = manifest()
        with self.assertRaises(ContractError) as caught:
            unsigned.assert_publishable(SHA, VERSION)
        self.assertIn("app.tar.gz", str(caught.exception))

    def test_publishing_accepts_a_verified_manifest_from_the_right_commit(self):
        verified = manifest().mark_verified("ci")
        verified.assert_publishable(SHA, VERSION)
        self.assertTrue(verified.by_name("app.tar.gz").verified)
        self.assertEqual(verified.by_name("app.tar.gz").verified_by, "ci")

    def test_a_manifest_digest_does_not_depend_on_the_order_artifacts_were_recorded(self):
        one = artifact(name="a", digest=hashlib.sha256(b"a").hexdigest())
        two = artifact(name="b", digest=hashlib.sha256(b"b").hexdigest())
        self.assertEqual(
            manifest(one, two).digest(), manifest(two, one).digest()
        )

    def test_a_checksum_listing_describes_everything_but_itself(self):
        listing = manifest(artifact()).checksums_listing()
        self.assertIn("app.tar.gz", listing)
        self.assertIn(artifact().digest, listing)

        with tempfile.TemporaryDirectory() as directory:
            path = manifest(artifact()).write_checksums(directory)
            size, digest = digest_file(path)
        self.assertEqual(digest, manifest(artifact()).checksums_digest())
        self.assertEqual(size, len(listing.encode("utf-8")))


class ManifestBuilderTests(unittest.TestCase):
    def test_an_adapter_never_types_a_digest_by_hand(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "app.bin")
            with open(path, "wb") as handle:
                handle.write(b"hello")
            builder = ManifestBuilder("fixture", "fixture", SHA, VERSION)
            built = builder.record("app.bin", path, "binary")
            self.assertEqual(built.size, 5)
            self.assertEqual(built.digest, hashlib.sha256(b"hello").hexdigest())

    def test_recording_a_file_that_is_not_there_fails_where_it_is_missing(self):
        builder = ManifestBuilder("fixture", "fixture", SHA, VERSION)
        with self.assertRaises(ContractError) as caught:
            builder.record("app.bin", "/nonexistent/app.bin", "binary")
        self.assertIn("does not exist", str(caught.exception))

    def test_a_declared_artifact_has_the_shape_and_none_of_the_facts(self):
        builder = ManifestBuilder("fixture", "fixture", SHA, VERSION)
        declared = builder.declare("app.bin", "/tmp/app.bin", "binary")
        self.assertTrue(declared.declared)
        self.assertEqual(declared.size, 0)
        self.assertEqual(declared.digest, contract.EMPTY_DIGEST)
        self.assertFalse(declared.signed)

    def test_replacing_a_row_keeps_one_name_to_one_artifact(self):
        builder = ManifestBuilder("fixture", "fixture", SHA, VERSION)
        first = builder.record("app.bin", __file__, "binary")
        second = builder.replace(artifact(name="app.bin", path=__file__, size=1))
        built = builder.build()
        self.assertEqual(built.names, ("app.bin",))
        self.assertEqual(built.by_name("app.bin").size, 1)
        self.assertNotEqual(first.digest, second.digest)


class PublishRequestTests(unittest.TestCase):
    def _request(self, *manifests: ArtifactManifest, **overrides) -> PublishRequest:
        values = {
            "destination": "github release",
            "tag": "v1.4.0",
            "version": VERSION,
            "source_sha": SHA,
            "manifests": manifests or (manifest().mark_verified("ci"),),
            "key": "k",
        }
        values.update(overrides)
        return PublishRequest(**values)

    def test_a_release_with_no_manifest_holds_nothing(self):
        with self.assertRaises(ContractError):
            self._request(manifests=())

    def test_a_manifest_from_another_commit_is_refused_before_a_destination_sees_it(self):
        wrong = ArtifactManifest(
            target="fixture",
            source_sha="b" * 40,
            version=VERSION,
            adapter="fixture",
            artifacts=(
                artifact(source_sha="b" * 40),
            ),
        ).mark_verified("ci")
        with self.assertRaises(ContractError):
            self._request(wrong)

    def test_every_target_is_addressed(self):
        request = self._request(manifest(artifact()).mark_verified("ci"), other_manifest())
        self.assertEqual(request.targets, ("fixture", "other"))
        self.assertEqual(len(request.artifacts), 2)

    def test_two_targets_claiming_one_asset_name_are_refused(self):
        with self.assertRaises(ContractError) as caught:
            self._request(
                manifest(artifact()).mark_verified("ci"), other_manifest(name="app.tar.gz")
            )
        self.assertIn("app.tar.gz", str(caught.exception))

    def test_the_same_manifest_twice_is_not_a_collision(self):
        verified = manifest().mark_verified("ci")
        self.assertEqual(self._request(verified, verified).targets, ("fixture", "fixture"))


class PublisherResultTests(unittest.TestCase):
    def test_there_are_three_outcomes_and_no_others(self):
        self.assertEqual(
            contract.SUPPORTED_PUBLISH_OUTCOMES,
            ("published", "skipped", "failed"),
        )

    def test_a_skipped_publication_is_a_success_that_explained_itself(self):
        result = skipped("github release", "already published", identity="v1.4.0@abc")
        self.assertTrue(result.skipped)
        self.assertTrue(result.ok)
        self.assertFalse(result.published)

    def test_a_failure_says_whether_another_run_could_help(self):
        retryable = failed("github release", "upload timed out", code="upload-failed", retryable=True)
        fatal = failed("github release", "digest mismatch", code="asset-digest-mismatch")
        self.assertTrue(retryable.retryable)
        self.assertFalse(fatal.retryable)
        self.assertFalse(retryable.ok)

    def test_a_result_records_where_and_under_which_identity(self):
        result = published(
            "github release",
            "v1.4.0@" + SHA,
            external_id="R_1",
            external_version="v1.4.0",
            code="published",
        )
        described = result.describe()
        self.assertEqual(described["destination"], "github release")
        self.assertEqual(described["identity"], f"v1.4.0@{SHA}")
        self.assertEqual(described["external_id"], "R_1")


class VerificationReportTests(unittest.TestCase):
    def test_a_report_cannot_claim_success_and_list_failures(self):
        with self.assertRaises(ContractError):
            VerificationReport(verified=True, failures=("digest",))

    def test_a_failure_must_say_what_failed(self):
        with self.assertRaises(ContractError):
            VerificationReport(verified=False)

    def test_success_must_name_who_verified(self):
        with self.assertRaises(ContractError) as caught:
            VerificationReport(verified=True, detail="all good")
        self.assertIn("who verified it", str(caught.exception))


class PortConformanceTests(unittest.TestCase):
    def test_every_role_has_a_surface(self):
        for role in (
            "eligibility",
            "version",
            "notes",
            "target-adapter",
            "publisher",
            "release-pr",
            "downstream-sync",
            "release-repository",
            "provenance",
        ):
            self.assertIn(role, contract.PORT_SURFACES)

    def test_a_conforming_component_reports_nothing_missing(self):
        self.assertEqual(contract.conform(support.FixtureAdapter(), "target-adapter"), ())
        self.assertEqual(contract.conform(support.FixtureNotes(), "notes"), ())

    def test_a_component_that_cannot_be_planned_is_not_accepted(self):
        class Blind:
            name = "blind"

            def build(self, request):
                return None

            def sign(self, request, manifest):
                return manifest

            def verify(self, request, manifest):
                return None

            def intent(self):
                return "build things"

        self.assertIn("SUPPORTS_DRY_RUN", contract.conform(Blind(), "target-adapter"))

    def test_a_component_with_no_name_is_not_accepted(self):
        class Anonymous(support.FixtureAdapter):
            name = ""

        self.assertIn("name", contract.conform(Anonymous(), "target-adapter"))

    def test_an_unknown_role_is_refused_rather_than_assumed_empty(self):
        with self.assertRaises(ContractError):
            contract.conform(object(), "publisher-of-rumours")


class BuildRequestTests(unittest.TestCase):
    def test_a_build_request_names_the_target_and_the_pin(self):
        request = BuildRequest(
            target=TargetSpec(id="fixture", adapter="fixture"),
            version=VERSION,
            source_sha=SHA,
            key="k",
            stage="build",
        )
        self.assertEqual(request.stage, "build")
        self.assertEqual(request.target.id, "fixture")
        self.assertFalse(request.dry_run)


if __name__ == "__main__":
    unittest.main()
