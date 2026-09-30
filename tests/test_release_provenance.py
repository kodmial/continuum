"""Provenance: a statement about who built a byte, and whether it checks out.

The tests here are the ones a consumer would run, because that is the only test
that matters for evidence: take the statement, take the bytes, and ask whether
the claim holds. So each property is exercised from the outside — build a
statement, tamper with one part of it, and assert that the check fails for that
reason and not for another.

The properties:

* a statement is bound to bytes by digest, so a renamed or rebuilt artifact does
  not inherit another build's statement;
* a statement is bound to a workflow at a ref and a commit, so it can be told
  apart from one produced by an unreviewed workflow;
* the reference a manifest records is the digest of a statement that is in the
  published bundle — not a statement this module rebuilds differently later;
* a declared artifact with no statement is refused rather than published;
* evidence does not attest itself;
* a partial build identity produces no attestor rather than a weak statement.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import tempfile
import unittest

from continuum.release.contract import (
    PROVENANCE_ATTESTED,
    PROVENANCE_DECLARED,
    ArtifactManifest,
    ManifestBuilder,
    SIGNING_NOT_APPLICABLE,
    VERIFICATION_VERIFIED,
    conform,
)
from continuum.release.provenance import (
    BUNDLE_SUFFIX,
    SLSA_PREDICATE,
    STATEMENT_TYPE,
    BuildIdentity,
    ProvenanceAttestor,
    ProvenanceError,
    assert_manifest_provenance,
    attestable_artifacts,
    attestor_from_environment,
    read_bundle,
    statement,
    statement_digest,
    subject_of,
    verify_statement,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "continuum/continuum"
WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/main"
VERSION = "1.0.0"
TAG = f"v{VERSION}"


def identity(**overrides) -> BuildIdentity:
    fields = {
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "workflow_ref": "refs/heads/main",
        "source_sha": SHA,
        "run_id": "12345",
        "run_attempt": "1",
        "event": "workflow_call",
    }
    fields.update(overrides)
    return BuildIdentity(**fields)


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.build = identity()
        self.document = statement(
            identity=self.build,
            name="app-1.0.0.tar.gz",
            digest_algorithm="sha256",
            digest="c" * 64,
        )

    def test_produces_an_in_toto_statement_bound_to_the_bytes(self):
        self.assertEqual(self.document["_type"], STATEMENT_TYPE)
        self.assertEqual(self.document["predicateType"], SLSA_PREDICATE)
        self.assertEqual(
            self.document["subject"],
            [{"name": "app-1.0.0.tar.gz", "digest": {"sha256": "c" * 64}}],
        )

    def test_names_the_workflow_that_ran_and_the_commit_it_ran_at(self):
        parameters = self.document["predicate"]["buildDefinition"]["externalParameters"]
        self.assertEqual(parameters["repository"], REPOSITORY)
        self.assertEqual(parameters["workflow"]["id"], WORKFLOW)
        self.assertEqual(parameters["workflow"]["ref"], "refs/heads/main")
        self.assertEqual(parameters["source"]["digest"]["gitCommit"], SHA)
        run = self.document["predicate"]["runDetails"]
        self.assertEqual(run["builder"]["id"], WORKFLOW)
        names = {item["name"]: item["value"] for item in run["metadata"]["byproducts"]}
        self.assertEqual(names["github.run_id"], "12345")
        self.assertEqual(names["github.event_name"], "workflow_call")

    def test_reports_a_digest_algorithm_other_than_sha256(self):
        other = statement(
            identity=self.build,
            name="app-1.0.0.tar.gz",
            digest_algorithm="sha512",
            digest="d" * 128,
        )
        self.assertEqual(subject_of(other)[1], "sha512")

    def test_refuses_a_subject_with_nothing_to_identify(self):
        with self.assertRaises(ProvenanceError):
            statement(
                identity=self.build, name="", digest_algorithm="sha256", digest="c" * 64
            )

    # -- what tampering must be caught
    def test_refuses_a_statement_moved_to_another_artifact(self):
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=self.build,
                name="other-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("cannot be moved", str(caught.exception))

    def test_refuses_a_statement_about_different_bytes(self):
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=self.build,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="e" * 64,
            )
        self.assertIn("different bytes", str(caught.exception))

    def test_refuses_a_statement_from_another_workflow(self):
        other = identity(
            workflow=f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/main"
        )
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=other,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("cannot tell a reviewed build", str(caught.exception))

    def test_refuses_a_statement_that_names_another_ref(self):
        # The workflow id is left alone, so the ref is the only thing that does
        # not line up: this is the check a tag-forced rebuild fails.
        parameters = self.document["predicate"]["buildDefinition"]["externalParameters"]
        parameters["workflow"]["ref"] = "refs/heads/attacker"
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=self.build,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("which version of the workflow", str(caught.exception))

    def test_refuses_a_workflow_ran_at_another_ref(self):
        other = identity(
            workflow=f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/attacker",
            workflow_ref="refs/heads/attacker",
        )
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=other,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("cannot tell a reviewed build", str(caught.exception))

    def test_refuses_a_statement_from_another_commit(self):
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                self.document,
                identity=identity(source_sha=OTHER_SHA),
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("claims commit", str(caught.exception))

    def test_refuses_a_predicate_a_consumer_cannot_interpret(self):
        tampered = dict(self.document, predicateType="https://example.test/made-up")
        with self.assertRaises(ProvenanceError) as caught:
            verify_statement(
                tampered,
                identity=self.build,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest="c" * 64,
            )
        self.assertIn("cannot check", str(caught.exception))

    def test_returns_the_digest_of_a_statement_that_checks_out(self):
        reference = verify_statement(
            self.document,
            identity=self.build,
            name="app-1.0.0.tar.gz",
            digest_algorithm="sha256",
            digest="c" * 64,
        )
        self.assertEqual(reference, statement_digest(self.document))

    def test_digest_is_the_same_for_the_same_claim_in_any_key_order(self):
        reordered = json.loads(json.dumps(self.document))
        reordered = {key: reordered[key] for key in sorted(reordered, reverse=True)}
        self.assertEqual(statement_digest(reordered), statement_digest(self.document))

    def test_refuses_an_envelope_it_does_not_recognise(self):
        with self.assertRaises(ProvenanceError):
            subject_of({"_type": "https://example.test/other"})

    def test_refuses_a_statement_with_two_subjects(self):
        two = dict(self.document)
        two["subject"] = list(self.document["subject"]) * 2
        with self.assertRaises(ProvenanceError) as caught:
            subject_of(two)
        self.assertIn("exactly one subject", str(caught.exception))

    def test_refuses_a_subject_with_two_digest_algorithms(self):
        two = dict(self.document)
        two["subject"] = [
            {"name": "app-1.0.0.tar.gz", "digest": {"sha256": "c" * 64, "sha512": "d" * 128}}
        ]
        with self.assertRaises(ProvenanceError) as caught:
            subject_of(two)
        self.assertIn("exactly one digest", str(caught.exception))


class BuildIdentityTests(unittest.TestCase):
    def test_lowercases_the_commit(self):
        self.assertEqual(identity(source_sha="A" * 40).source_sha, SHA)

    def test_refuses_a_repository_that_is_not_a_pair(self):
        with self.assertRaises(ProvenanceError) as caught:
            identity(repository="continuum")
        self.assertIn("owner/name", str(caught.exception))

    def test_refuses_a_workflow_that_is_not_a_reusable_identity(self):
        with self.assertRaises(ProvenanceError) as caught:
            identity(workflow="release.yml")
        self.assertIn("reusable workflow identity", str(caught.exception))

    def test_refuses_a_workflow_named_at_a_different_ref(self):
        with self.assertRaises(ProvenanceError) as caught:
            identity(workflow_ref="refs/tags/v1.0.0")
        self.assertIn("disagree", str(caught.exception))

    def test_refuses_an_abbreviated_commit(self):
        with self.assertRaises(ProvenanceError) as caught:
            identity(source_sha="abc1234")
        self.assertIn("full commit SHA", str(caught.exception))

    def test_refuses_a_ref_that_is_neither_a_ref_nor_a_commit(self):
        with self.assertRaises(ProvenanceError):
            identity(workflow=f"{REPOSITORY}/.github/workflows/release.yml@main")

    def test_refuses_a_run_attempt_that_is_not_a_number(self):
        with self.assertRaises(ProvenanceError):
            identity(run_attempt="first")

    def test_describes_itself_without_inventing_absent_fields(self):
        described = identity(run_id="", event="").describe()
        self.assertNotIn("run_id", described)
        self.assertNotIn("event", described)
        self.assertEqual(described["source_sha"], SHA)

    def test_names_the_path_and_invocation_a_consumer_greps_for(self):
        self.assertEqual(
            identity().workflow_path, ".github/workflows/release.yml"
        )
        self.assertEqual(
            identity().invocation_id,
            f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
        )


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.attestor = ProvenanceAttestor(identity(), directory=self.workspace.name)
        self.digest = hashlib.sha256(b"release bytes").hexdigest()

    def attest(self, name="app-1.0.0.tar.gz", digest=None, tag=TAG, algorithm="sha256"):
        return self.attestor.attest(
            tag=tag,
            name=name,
            digest_algorithm=algorithm,
            digest=digest or self.digest,
            source_sha=SHA,
        )

    def test_conforms_to_the_provenance_port(self):
        self.assertEqual(conform(self.attestor, "provenance"), ())
        self.assertTrue(self.attestor.name)

    def test_returns_a_reference_to_a_statement_it_publishes(self):
        reference = self.attest()
        published = self.attestor.bundle(VERSION).attestations()
        self.assertEqual(published["app-1.0.0.tar.gz"], reference)
        self.assertTrue(reference.startswith("sha256:"))

    def test_publishes_the_statements_it_returned_references_for(self):
        reference = self.attest()
        bundle = self.attestor.bundle(VERSION)
        found = {
            f"sha256:{statement_digest(document)}"
            for document in bundle.statements
        }
        self.assertIn(reference, found)

    def test_keeps_the_digest_algorithm_the_caller_asked_for(self):
        self.attest(algorithm="sha512", digest="d" * 128)
        name, algorithm, digest = subject_of(self.attestor.bundle(VERSION).statements[0])
        self.assertEqual((name, algorithm, digest), ("app-1.0.0.tar.gz", "sha512", "d" * 128))

    def test_refuses_one_name_attested_at_two_digests(self):
        self.attest()
        self.attest(digest="f" * 64)
        with self.assertRaises(ProvenanceError) as caught:
            self.attestor.bundle(VERSION)
        self.assertIn("one statement per artifact", str(caught.exception))

    def test_writes_one_line_per_statement(self):
        self.attest()
        self.attest(name="other-1.0.0.tar.gz")
        written = self.attestor.write(VERSION)
        self.assertEqual(len(written), 1)
        with open(written[0].write(self.attestor.path_for(written[0].name))) as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)

    def test_writes_nothing_when_nothing_was_attested(self):
        self.assertEqual(self.attestor.write(VERSION), ())

    def test_names_a_bundle_after_the_commit_it_describes(self):
        self.assertEqual(self.attestor.bundle_name, f"{SHA[:12]}{BUNDLE_SUFFIX}")

    def test_records_the_bundle_as_verified_evidence(self):
        self.attest()
        bundle = self.attestor.bundle(VERSION)
        path = bundle.write(self.attestor.path_for(bundle.name))
        builder = ManifestBuilder(
            target="desktop", adapter="fake", source_sha=SHA, version=VERSION
        )
        artifact = bundle.record(builder, path)
        self.assertEqual(artifact.verification, VERIFICATION_VERIFIED)
        self.assertEqual(artifact.signing, SIGNING_NOT_APPLICABLE)
        self.assertEqual(artifact.verified_by, self.attestor.name)

    def test_refuses_a_bundle_that_changed_on_the_way_to_disk(self):
        self.attest()
        bundle = self.attestor.bundle(VERSION)
        path = bundle.write(self.attestor.path_for(bundle.name))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"_type": "extra"}\n')
        builder = ManifestBuilder(
            target="desktop", adapter="fake", source_sha=SHA, version=VERSION
        )
        with self.assertRaises(ProvenanceError) as caught:
            bundle.record(builder, path)
        self.assertIn("not evidence", str(caught.exception))

    def test_reads_a_bundle_back_and_checks_every_statement_in_it(self):
        self.attest()
        bundle = self.attestor.bundle(VERSION)
        path = bundle.write(self.attestor.path_for(bundle.name))
        read = read_bundle(path)
        self.assertEqual(read.name, bundle.name)
        self.assertEqual(read.digest(), bundle.digest())
        self.assertEqual(read.attestations(), bundle.attestations())

    def test_refuses_to_read_a_bundle_holding_something_that_is_not_a_statement(self):
        path = os.path.join(self.workspace.name, "broken.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"hello": "world"}\n')
        with self.assertRaises(ProvenanceError):
            read_bundle(path)

    def test_refuses_statements_from_two_different_versions_of_one_release(self):
        self.attest(tag=TAG)
        self.attest(name="other-1.0.0.tar.gz", tag="v2.0.0")
        with self.assertRaises(ProvenanceError) as caught:
            self.attestor.bundle(VERSION)
        self.assertIn("One release holds one version", str(caught.exception))

    def test_accepts_a_prerelease_tag_of_the_same_version(self):
        self.attest(tag=f"v{VERSION}-rc.1")
        self.assertEqual(len(self.attestor.bundle(VERSION).statements), 1)

    def test_submits_each_statement_to_the_log_when_one_is_configured(self):
        submitted = []
        attestor = ProvenanceAttestor(
            identity(), directory=self.workspace.name, submitter=lambda **kw: submitted.append(kw)
        )
        attestor.attest(
            tag=TAG,
            name="app-1.0.0.tar.gz",
            digest_algorithm="sha256",
            digest=self.digest,
            source_sha=SHA,
        )
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["digest"], self.digest)
        self.assertEqual(submitted[0]["tag"], TAG)

    def test_refuses_a_submitter_that_cannot_be_called(self):
        with self.assertRaises(ProvenanceError):
            ProvenanceAttestor(identity(), directory=self.workspace.name, submitter="log")

    def test_refuses_to_bind_another_commits_bytes(self):
        with self.assertRaises(ProvenanceError) as caught:
            self.attestor.attest(
                tag=TAG,
                name="app-1.0.0.tar.gz",
                digest_algorithm="sha256",
                digest=self.digest,
                source_sha=OTHER_SHA,
            )
        self.assertIn("one build's identity", str(caught.exception))

    def test_refuses_an_attestor_with_nowhere_to_write(self):
        with self.assertRaises(ProvenanceError):
            ProvenanceAttestor(identity(), directory="")

    def test_says_what_it_would_do(self):
        self.assertIn("in-toto statement", self.attestor.intent())


class SbomTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.sbom = os.path.join(self.workspace.name, "bom.json")
        with open(self.sbom, "w", encoding="utf-8") as handle:
            handle.write('{"spdxVersion": "SPDX-2.3"}')
        self.attestor = ProvenanceAttestor(
            identity(), directory=os.path.join(self.workspace.name, "out"), sbom_path=self.sbom
        )

    def test_refuses_an_sbom_that_was_never_generated(self):
        with self.assertRaises(ProvenanceError) as caught:
            ProvenanceAttestor(
                identity(),
                directory=self.workspace.name,
                sbom_path=os.path.join(self.workspace.name, "absent.json"),
            )
        self.assertIn("never generated", str(caught.exception))

    def test_refuses_an_sbom_kind_it_cannot_name(self):
        with self.assertRaises(ProvenanceError) as caught:
            ProvenanceAttestor(
                identity(), directory=self.workspace.name, sbom_path=self.sbom, sbom_kind="guess"
            )
        self.assertIn("unknown SBOM kind", str(caught.exception))

    def test_attests_the_sbom_under_its_own_predicate(self):
        bundle = self.attestor.sbom_bundle(VERSION)
        document = bundle.statements[0]
        self.assertEqual(document["predicateType"], "https://spdx.dev/Document")
        self.assertEqual(
            subject_of(document)[0], f"{SHA[:12]}-{VERSION}-sbom.json"
        )

    def test_keeps_the_runners_paths_out_of_the_published_document(self):
        document = self.attestor.sbom_bundle(VERSION).statements[0]
        rendered = json.dumps(document)
        self.assertNotIn(self.workspace.name, rendered)

    def test_records_the_sbom_as_an_attested_evidence_asset(self):
        manifest = self.attestor.attach_to(build_manifest(), VERSION)
        sbom = [item for item in manifest.artifacts if item.name.endswith("sbom.json")][0]
        self.assertEqual(sbom.provenance, PROVENANCE_ATTESTED)
        self.assertEqual(sbom.verification, VERIFICATION_VERIFIED)
        self.assertEqual(sbom.media_type, "application/spdx+json")
        reference = self.attestor.sbom_bundle(VERSION).attestations()[sbom.name]
        self.assertEqual(sbom.attestation, reference)

    def test_names_a_cyclonedx_sbom_as_one(self):
        attestor = ProvenanceAttestor(
            identity(),
            directory=os.path.join(self.workspace.name, "cdx"),
            sbom_path=self.sbom,
            sbom_kind="cyclonedx",
        )
        document = attestor.sbom_bundle(VERSION).statements[0]
        self.assertEqual(document["predicateType"], "https://cyclonedx.org/bom")

    def test_says_the_sbom_is_attested_in_its_intent(self):
        self.assertIn("software bill of materials", self.attestor.intent())


#: Scratch space for the helper below, removed when the module goes.
_SCRATCH = tempfile.TemporaryDirectory()
atexit.register(_SCRATCH.cleanup)


def build_manifest(*, declared=False) -> ArtifactManifest:
    """One artifact's manifest, optionally asking for provenance.

    A build manifest rather than an attached one, so a test can decide what
    evidence exists before it asks for it to be attached.
    """

    path = os.path.join(_SCRATCH.name, "app-1.0.0.tar.gz")
    with open(path, "wb") as handle:
        handle.write(b"release bytes")
    builder = ManifestBuilder(
        target="desktop", adapter="fake", source_sha=SHA, version=VERSION
    )
    builder.record(
        "app-1.0.0.tar.gz",
        path,
        "archive",
        verification=VERIFICATION_VERIFIED,
        verified_by="test",
        provenance=PROVENANCE_DECLARED if declared else "absent",
    )
    return builder.build()


def attest_one(attestor: ProvenanceAttestor, name="app-1.0.0.tar.gz", tag=TAG) -> str:
    return attestor.attest(
        tag=tag,
        name=name,
        digest_algorithm="sha256",
        digest=hashlib.sha256(b"release bytes").hexdigest(),
        source_sha=SHA,
    )


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.attestor = ProvenanceAttestor(identity(), directory=self.workspace.name)
        self.digest = hashlib.sha256(b"release bytes").hexdigest()

    def test_marks_a_declared_artifact_attested_and_points_at_its_statement(self):
        reference = attest_one(self.attestor)
        attached = self.attestor.attach_to(build_manifest(declared=True), VERSION)
        self.assertEqual(attached.artifacts[0].provenance, PROVENANCE_ATTESTED)
        self.assertEqual(attached.artifacts[0].attestation, reference)
        self.assertEqual(
            attached.artifacts[0].attestation,
            self.attestor.bundle(VERSION).attestations()["app-1.0.0.tar.gz"],
        )

    def test_refuses_a_declared_artifact_with_no_statement(self):
        with self.assertRaises(ProvenanceError) as caught:
            self.attestor.attach_to(build_manifest(declared=True), VERSION)
        self.assertIn("claim nothing backs", str(caught.exception))

    def test_leaves_an_artifact_that_asked_for_nothing_alone(self):
        manifest = self.attestor.attach_to(build_manifest(), VERSION)
        self.assertEqual(manifest.artifacts[0].provenance, "absent")
        self.assertEqual(manifest.artifacts[0].attestation, "")

    def test_does_not_rebuild_the_manifest_it_was_given(self):
        attest_one(self.attestor)
        original = build_manifest(declared=True)
        attached = self.attestor.attach_to(original, VERSION)
        self.assertEqual(len(original.artifacts), 1)
        self.assertEqual(original.artifacts[0].provenance, PROVENANCE_DECLARED)
        self.assertEqual(len(attached.artifacts), 2)

    def test_adds_the_bundle_to_the_manifest_it_returns(self):
        attest_one(self.attestor)
        attached = self.attestor.attach_to(build_manifest(), VERSION)
        names = [item.name for item in attached.artifacts]
        self.assertIn(self.attestor.bundle_name, names)

    def test_attaching_the_same_evidence_twice_changes_nothing(self):
        attest_one(self.attestor)
        once = self.attestor.attach_to(build_manifest(), VERSION)
        twice = self.attestor.attach_to(once, VERSION)
        self.assertEqual([item.name for item in twice.artifacts], [item.name for item in once.artifacts])
        self.assertEqual(twice.digest(), once.digest())

    def test_refuses_evidence_that_disagrees_with_a_bundle_already_present(self):
        attest_one(self.attestor)
        once = self.attestor.attach_to(build_manifest(), VERSION)
        other = ProvenanceAttestor(identity(), directory=self.workspace.name)
        attest_one(other, name="extra-1.0.0.tar.gz")
        with self.assertRaises(ProvenanceError) as caught:
            other.attach_to(once, VERSION)
        self.assertIn("only one of them describes this build", str(caught.exception))

    def test_everything_it_publishes_is_verified(self):
        attest_one(self.attestor)
        self.assertTrue(self.attestor.attach_to(build_manifest(), VERSION).all_verified)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.attestor = ProvenanceAttestor(identity(), directory=self.workspace.name)
        attest_one(self.attestor)
        attest_one(self.attestor)
        self.attested = self.attestor.attach_to(build_manifest(declared=True), VERSION)

    def test_accepts_a_release_whose_installable_artifacts_are_all_attested(self):
        assert_manifest_provenance((self.attested,), require_attested=True)

    def test_refuses_a_release_whose_artifact_attests_nothing(self):
        with self.assertRaises(ProvenanceError) as caught:
            assert_manifest_provenance((build_manifest(),), require_attested=True)
        self.assertEqual(caught.exception.code, "provenance-required")

    def test_says_nothing_when_the_release_does_not_ask_for_statements(self):
        assert_manifest_provenance((build_manifest(),), require_attested=False)

    def test_attests_installable_artifacts_only(self):
        names = [item.name for item in attestable_artifacts((self.attested,))]
        self.assertEqual(names, ["app-1.0.0.tar.gz"])

    def test_has_nothing_to_attest_in_a_manifest_with_nothing_in_it(self):
        self.assertEqual(attestable_artifacts(()), ())


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def environment(self, **overrides):
        base = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_WORKFLOW_REF": WORKFLOW,
            "CONTINUUM_RELEASE_SOURCE_SHA": SHA,
            "GITHUB_RUN_ID": "999",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_EVENT_NAME": "workflow_call",
        }
        base.update(overrides)
        return base

    def test_builds_an_attestor_from_the_run_it_is_running_in(self):
        attestor = attestor_from_environment(self.environment(), directory=self.workspace.name)
        self.assertIsNotNone(attestor)
        self.assertEqual(attestor.identity.source_sha, SHA)
        self.assertEqual(attestor.identity.run_attempt, "2")
        self.assertEqual(attestor.identity.workflow_ref, "refs/heads/main")

    def test_produces_nothing_rather_than_a_guessed_statement(self):
        for missing in ("GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF", "CONTINUUM_RELEASE_SOURCE_SHA"):
            environment = self.environment()
            environment.pop(missing)
            self.assertIsNone(
                attestor_from_environment(environment, directory=self.workspace.name),
                msg=missing,
            )

    def test_produces_nothing_from_an_environment_it_cannot_read(self):
        self.assertIsNone(
            attestor_from_environment({"PATH": "/usr/bin"}, directory=self.workspace.name)
        )

    def test_produces_nothing_from_an_abbreviated_commit(self):
        self.assertIsNone(
            attestor_from_environment(
                self.environment(CONTINUUM_RELEASE_SOURCE_SHA="abc1234"),
                directory=self.workspace.name,
            )
        )

    def test_accepts_a_run_dispatched_at_a_commit(self):
        environment = self.environment(
            GITHUB_WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/release.yml@{SHA}",
            CONTINUUM_RELEASE_SOURCE_SHA=SHA,
        )
        attestor = attestor_from_environment(environment, directory=self.workspace.name)
        self.assertIsNotNone(attestor)
        self.assertEqual(attestor.identity.workflow_ref, SHA)


if __name__ == "__main__":
    unittest.main()
