"""Proof that the bytes being published are the bytes a gate exercised.

The failure these tests exist for is not "a bug in a manifest". It is a release
that is green, signed, checksummed and consistent with itself, about a file that no
validation ever touched -- because the build was re-run after the gate, or the
artifact came from somewhere else, or the green run was for an earlier commit.

So each test is one way that can happen, and the assertion is always a refusal with
a *named* reason. A warning would be worse than nothing here: the publish would go
out anyway, and the log would read as though something had been checked.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from continuum.release import provenance

REPO_ROOT = Path(__file__).resolve().parents[1]

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


class TheHappyPath(unittest.TestCase):
    """Everything agreeing, which is the only case allowed to publish."""

    def setUp(self) -> None:
        self.dir = _scratch()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.artifact = _write(Path(self.dir) / "dist.tar.gz", b"the tested bytes")
        self.artifacts = {"dist.tar.gz": str(self.artifact)}
        self.candidate = provenance.build_candidate(
            source_sha=HEAD,
            artifacts=self.artifacts,
            gate=".github/workflows/validate.yml",
            gate_run_id="runs/1",
            artifact_source="run artifacts",
        )

    def test_a_green_gate_on_this_head_over_these_bytes_verifies(self) -> None:
        verification = provenance.verify_candidate(
            self.candidate, source_sha=HEAD, artifacts=self.artifacts
        )
        self.assertTrue(verification.ok, verification.message)
        self.assertEqual(verification.code, "")
        self.assertEqual(verification.verified, ("dist.tar.gz",))
        self.assertEqual(verification.source_sha, HEAD)
        self.assertIn("dist.tar.gz", provenance.summarize(verification))

    def test_the_verification_round_trips_as_a_document(self) -> None:
        verification = provenance.verify_candidate(
            self.candidate, source_sha=HEAD, artifacts=self.artifacts
        )
        document = verification.describe()
        self.assertEqual(document["kind"], "verification")
        self.assertEqual(json.loads(verification.to_json()), document)

    def test_the_candidate_round_trips_through_a_file(self) -> None:
        path = Path(self.dir) / "candidate.json"
        path.write_text(self.candidate.to_json(), encoding="utf-8")
        loaded = provenance.load_candidate(str(path))
        self.assertEqual(loaded.describe(), self.candidate.describe())
        self.assertEqual(loaded.digest, self.candidate.digest)

    def test_the_digest_moves_when_the_claim_does(self) -> None:
        # A decision bound to the claim is only binding if the claim can be told
        # apart from another one.
        retitled = provenance.TestedCandidate(
            source_sha=self.candidate.source_sha,
            gate=self.candidate.gate,
            gate_run_id="runs/2",
            gate_conclusion=self.candidate.gate_conclusion,
            artifacts=dict(self.candidate.artifacts),
            artifact_source=self.candidate.artifact_source,
        )
        self.assertNotEqual(retitled.digest, self.candidate.digest)


class TheGateThatDidNotRun(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _scratch()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.artifact = _write(Path(self.dir) / "dist.tar.gz", b"the tested bytes")
        self.artifacts = {"dist.tar.gz": str(self.artifact)}

    def _candidate(self, **kwargs) -> provenance.TestedCandidate:
        defaults = {
            "source_sha": HEAD,
            "gate": ".github/workflows/validate.yml",
            "gate_run_id": "runs/1",
            "artifacts": {"dist.tar.gz": provenance.file_digest(str(self.artifact))},
            "artifact_source": "run artifacts",
        }
        defaults.update(kwargs)
        return provenance.TestedCandidate(**defaults)

    def test_no_candidate_is_refused(self) -> None:
        verification = provenance.verify_candidate(
            None, source_sha=HEAD, artifacts=self.artifacts
        )
        self.assertFalse(verification.ok)
        self.assertEqual(verification.code, provenance.NO_CANDIDATE)
        self.assertIn("nothing has established", verification.message)

    def test_a_failed_gate_is_refused(self) -> None:
        verification = provenance.verify_candidate(
            self._candidate(gate_conclusion=provenance.GATE_FAILURE),
            source_sha=HEAD,
            artifacts=self.artifacts,
        )
        self.assertEqual(verification.code, provenance.GATE_NOT_GREEN)

    def test_a_cancelled_gate_is_not_a_passing_gate(self) -> None:
        # Cancelled means the gate did not finish, which is a different fact from
        # "the artifact is bad" and a different repair. The code is the same because
        # the publish is refused for the same reason either way.
        verification = provenance.verify_candidate(
            self._candidate(gate_conclusion=provenance.GATE_CANCELLED),
            source_sha=HEAD,
            artifacts=self.artifacts,
        )
        self.assertEqual(verification.code, provenance.GATE_NOT_GREEN)
        self.assertIn("cancelled", verification.message)

    def test_a_missing_gate_conclusion_is_refused(self) -> None:
        verification = provenance.verify_candidate(
            self._candidate(gate_conclusion=""),
            source_sha=HEAD,
            artifacts=self.artifacts,
        )
        self.assertFalse(verification.ok)

    def test_an_empty_artifact_set_is_refused_rather_than_trivially_verified(self) -> None:
        # The dangerous version: nothing to compare against, everything compared, so
        # the check passes vacuously and the release publishes with no coverage.
        verification = provenance.verify_candidate(self._candidate(), source_sha=HEAD)
        self.assertFalse(verification.ok)
        self.assertEqual(verification.code, provenance.NO_CANDIDATE)
        verification = provenance.verify_candidate(
            self._candidate(), source_sha=HEAD, artifacts={}
        )
        self.assertFalse(verification.ok)

    def test_a_candidate_with_no_artifacts_cannot_be_recorded(self) -> None:
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.build_candidate(source_sha=HEAD, artifacts={})
        self.assertEqual(caught.exception.code, "no_tested_artifacts")


class TheWrongHead(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _scratch()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.artifact = _write(Path(self.dir) / "dist.tar.gz", b"the tested bytes")
        self.artifacts = {"dist.tar.gz": str(self.artifact)}
        self.candidate = provenance.build_candidate(
            source_sha=HEAD,
            artifacts=self.artifacts,
            gate=".github/workflows/validate.yml",
            artifact_source="run artifacts",
        )

    def test_a_green_run_for_another_commit_says_nothing(self) -> None:
        verification = provenance.verify_candidate(
            self.candidate, source_sha=OTHER_HEAD, artifacts=self.artifacts
        )
        self.assertEqual(verification.code, provenance.HEAD_MISMATCH)
        self.assertIn(OTHER_HEAD, verification.message)

    def test_a_publish_that_does_not_say_which_tree_it_is_for_is_refused(self) -> None:
        # The absence of a head to compare is not a matching head. Without this the
        # check is skipped silently, which is the same as passing it.
        verification = provenance.verify_candidate(
            self.candidate, source_sha="", artifacts=self.artifacts
        )
        self.assertEqual(verification.code, provenance.PUBLISH_HEAD_UNKNOWN)
        self.assertFalse(verification.ok)

    def test_a_branch_name_is_not_a_head(self) -> None:
        # `main` moves, so a record naming it could be replayed against a tree the
        # gate never ran on. It is refused at the point of recording, so no such
        # record can exist to be replayed.
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.TestedCandidate(source_sha="main")
        self.assertEqual(caught.exception.code, "malformed_source_sha")
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.build_candidate(
                source_sha="release/1.2", artifacts=self.artifacts
            )
        self.assertEqual(caught.exception.code, "malformed_source_sha")

    def test_a_commit_of_either_hash_width_is_accepted(self) -> None:
        # The width is a property of the repository's hash function, not of the
        # claim; refusing a 64-character object ID would be a bug, not caution.
        for width in (40, 64):
            candidate = provenance.TestedCandidate(source_sha="c" * width)
            self.assertEqual(len(candidate.source_sha), width)

    def test_a_candidate_with_no_source_at_all_is_refused_at_recording(self) -> None:
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.TestedCandidate(source_sha="  ")
        self.assertEqual(caught.exception.code, "source_sha_missing")


class TheWrongBytes(unittest.TestCase):
    """The one failure nothing downstream can catch."""

    def setUp(self) -> None:
        self.dir = _scratch()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tested = _write(Path(self.dir) / "run" / "dist.tar.gz", b"the tested bytes")
        self.candidate = provenance.build_candidate(
            source_sha=HEAD,
            artifacts={"dist.tar.gz": str(self.tested)},
            gate=".github/workflows/validate.yml",
            artifact_source="run artifacts",
        )

    def test_a_rebuilt_artifact_is_refused(self) -> None:
        # Byte-identical in name, different in content. The digest, the checksum and
        # the manifest would all agree with each other and none of them would
        # describe what the gate ran.
        rebuilt = _write(Path(self.dir) / "dist" / "dist.tar.gz", b"a rebuild, near enough")
        verification = provenance.verify_candidate(
            self.candidate,
            source_sha=HEAD,
            artifacts={"dist.tar.gz": str(rebuilt)},
        )
        self.assertEqual(verification.code, provenance.ARTIFACT_MISMATCH)
        self.assertEqual(verification.mismatched, ("dist.tar.gz",))
        self.assertIn("two files that share a name", verification.message)

    def test_an_artifact_the_gate_never_saw_is_refused(self) -> None:
        extra = _write(Path(self.dir) / "dist" / "checksums.txt", b"whatever\n")
        verification = provenance.verify_candidate(
            self.candidate,
            source_sha=HEAD,
            artifacts={
                "dist.tar.gz": str(self.tested),
                "checksums.txt": str(extra),
            },
        )
        self.assertEqual(verification.code, provenance.ARTIFACT_MISSING)
        self.assertEqual(verification.missing, ("checksums.txt",))
        self.assertEqual(verification.verified, ("dist.tar.gz",))

    def test_a_file_that_is_not_there_is_refused(self) -> None:
        verification = provenance.verify_candidate(
            self.candidate,
            source_sha=HEAD,
            artifacts={"dist.tar.gz": str(Path(self.dir) / "dist" / "dist.tar.gz")},
        )
        self.assertEqual(verification.code, provenance.ARTIFACT_MISSING)

    def test_one_good_artifact_never_covers_a_bad_one(self) -> None:
        rebuilt = _write(Path(self.dir) / "dist" / "dist.tar.gz", b"a rebuild")
        second = _write(Path(self.dir) / "run" / "notes.txt", b"notes")
        other = provenance.build_candidate(
            source_sha=HEAD,
            artifacts={
                "dist.tar.gz": str(self.tested),
                "notes.txt": str(second),
            },
            gate=".github/workflows/validate.yml",
            artifact_source="run artifacts",
        )
        verification = provenance.verify_candidate(
            other,
            source_sha=HEAD,
            artifacts={"dist.tar.gz": str(rebuilt), "notes.txt": str(second)},
        )
        self.assertFalse(verification.ok)
        self.assertEqual(verification.verified, ("notes.txt",))
        self.assertEqual(verification.mismatched, ("dist.tar.gz",))

    def test_a_candidate_with_no_recorded_source_is_refused(self) -> None:
        candidate = provenance.TestedCandidate(
            source_sha=HEAD,
            gate=".github/workflows/validate.yml",
            artifacts={"dist.tar.gz": provenance.file_digest(str(self.tested))},
            artifact_source="",
        )
        verification = provenance.verify_candidate(
            candidate, source_sha=HEAD, artifacts={"dist.tar.gz": str(self.tested)}
        )
        self.assertEqual(verification.code, provenance.NO_ARTIFACT_SOURCE)

    def test_a_publish_with_no_commit_may_opt_out_of_both_source_checks(self) -> None:
        # A vendored tree or a generated archive has no immutable head and no
        # artifact source to name, so both checks are refused together -- and the
        # byte comparison, which is the point, still runs.
        candidate = provenance.TestedCandidate(
            source_sha=HEAD,
            gate=".github/workflows/validate.yml",
            artifacts={"dist.tar.gz": provenance.file_digest(str(self.tested))},
            artifact_source="",
        )
        verification = provenance.verify_candidate(
            candidate,
            artifacts={"dist.tar.gz": str(self.tested)},
            require_source=False,
        )
        self.assertTrue(verification.ok, verification.message)


class Digests(unittest.TestCase):
    def test_a_digest_is_read_from_the_bytes(self) -> None:
        directory = _scratch()
        self.addCleanup(shutil.rmtree, directory, True)
        path = _write(Path(directory) / "f", b"abc")
        self.assertEqual(
            provenance.file_digest(str(path)), hashlib.sha256(b"abc").hexdigest()
        )

    def test_a_missing_file_is_named_rather_than_digested_as_empty(self) -> None:
        directory = _scratch()
        self.addCleanup(shutil.rmtree, directory, True)
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.file_digest(str(Path(directory) / "absent"))
        self.assertEqual(caught.exception.code, "artifact-missing")

    def test_an_absent_digest_stays_absent(self) -> None:
        # A blank is not the digest of the empty string; collapsing the two would
        # make a record that names no digest compare equal to anything.
        self.assertEqual(provenance.canonical_digest(""), "")
        self.assertEqual(provenance.canonical_digest("   "), "")

    def test_a_digest_is_normalised_rather_than_reinterpreted(self) -> None:
        self.assertEqual(provenance.canonical_digest("  " + "A" * 64 + " "), "a" * 64)

    def test_something_that_is_not_a_digest_is_refused(self) -> None:
        for value in ("deadbeef", "z" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(provenance.ProvenanceError) as caught:
                provenance.canonical_digest(value)
            self.assertEqual(caught.exception.code, "malformed_digest")

    def test_a_record_from_another_schema_is_refused(self) -> None:
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.read_candidate(
                {"schema": "something-else/v1", "source_sha": HEAD, "artifacts": {}}
            )
        self.assertEqual(caught.exception.code, "unknown_provenance_schema")

    def test_a_record_that_is_not_an_object_is_refused(self) -> None:
        with self.assertRaises(provenance.ProvenanceError) as caught:
            provenance.read_candidate(["source_sha", HEAD])
        self.assertEqual(caught.exception.code, "candidate_not_a_mapping")


def _scratch() -> str:
    """A directory inside the worktree, so a failed run leaves nothing behind it.

    Not the system temp directory: a test that fails in CI should leave its evidence
    where the CI configuration can collect it.
    """

    return tempfile.mkdtemp(prefix="continuum-provenance-test-", dir=str(REPO_ROOT))


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


if __name__ == "__main__":
    unittest.main()