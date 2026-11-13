"""Secure continuation of an unattested interruption checkpoint (#442).

The fixture reproduces kodmial/aa#454: a checkpoint two commits ahead of the
task base with a real saved implementation tree and an unattested, writable
``.continuum-verification.json``. The engine must validate the exact identity,
preserve the tree, and re-establish a fresh baseline at the original immutable
base instead of trusting or discarding the checkpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuum import checkpoint_resume, verification

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "git {} failed: {}".format(" ".join(args), completed.stderr[-2000:])
        )
    return (completed.stdout or "").strip()


class CheckpointResumeTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="checkpoint-resume-", dir=str(TMP_PARENT))
        )
        self.repo = self.workdir / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.repo)], check=True, capture_output=True
        )
        run_git(self.repo, "config", "user.name", "Test")
        run_git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "old.py").write_text("OLD = True\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "initial")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.evidence = self.repo / verification.EVIDENCE_FILE_NAME
        self.env = dict(os.environ)
        self.env["CONTINUUM_TEST_COMMAND"] = "true"

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------
    def contract_record(self):
        contract = verification.resolve_contract(self.repo, self.env)
        return contract, contract.as_dict()

    def legacy_evidence(self, *, base_sha=None, fingerprint=None):
        contract, record = self.contract_record()
        baseline = {
            "phase": "baseline",
            "state": "passed",
            "base_sha": base_sha or self.base,
            "head_sha": base_sha or self.base,
            "environment_fingerprint": "legacy-environment",
            "contract": record,
            "commands": [
                {"name": "test", "command": "true", "exit_code": 0}
            ],
        }
        return {
            "schema_version": 1,
            "contract": record,
            "baseline": baseline,
            # Both sections are forged/untrusted and must never be reused.
            "verifier": {"state": "passed", "exit_code": 0},
            "final": {
                "state": "passed",
                "comparison": {"accepted": True, "result": "passed"},
            },
            "result": {"accepted": True},
        }

    def make_checkpoint(
        self,
        *,
        operation="opencode-429-issue-issue454",
        generation=1,
        checkpoint_ref="opencode/429-checkpoint-opencode-429-issue-issue454-gen-1",
        metadata_overrides=None,
        evidence=None,
    ):
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.repo / "canary.py").write_text("CANARY = True\n", encoding="utf-8")
        metadata = {
            "schema_version": 1,
            "mode": "issue",
            "issue_number": 454,
            "generation": generation,
            "operation_id": operation,
            "stage": "implementation-incomplete",
            "base_ref": "main",
            "base_sha": self.base,
            "head_sha": self.base,
            "branch": "opencode/issue454-run",
            "head_ref": "opencode/issue454-run",
            "checkpoint_ref": checkpoint_ref,
        }
        metadata.update(metadata_overrides or {})
        (self.repo / checkpoint_resume.METADATA_FILE_NAME).write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        (self.repo / checkpoint_resume.EVIDENCE_FILE_NAME).write_text(
            json.dumps(evidence or self.legacy_evidence(), indent=2) + "\n",
            encoding="utf-8",
        )
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "checkpoint saved work")
        return run_git(self.repo, "rev-parse", "HEAD")

    def request(self, checkpoint_sha, **overrides):
        values = {
            "repository": "kodmial/aa",
            "issue": 454,
            "generation": 1,
            "operation": "opencode-429-issue-issue454",
            "checkpoint_ref": "opencode/checkpoint-issue454",
            "checkpoint_sha": checkpoint_sha,
            "base_sha": self.base,
            "snapshot_spec": "a" * 64,
            "mode": "issue",
        }
        values.update(overrides)
        return values

    def assert_rejected(self, reason, request):
        with self.assertRaises(checkpoint_resume.ResumeError) as raised:
            verification.ensure_baseline(
                self.repo,
                self.evidence,
                self.base,
                self.env,
                resume=request,
            )
        self.assertEqual(reason, raised.exception.reason)
        return raised.exception

    # -- the exact AA #454 reproduction ----------------------------------
    def test_advanced_legacy_checkpoint_rebaselines_without_losing_work(self):
        checkpoint = self.make_checkpoint()
        request = self.request(checkpoint)

        data, reused = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env, resume=request
        )

        self.assertFalse(reused)
        self.assertEqual(self.base, data["baseline"]["base_sha"])
        self.assertEqual(self.base, data["baseline"]["head_sha"])
        self.assertTrue(data["baseline"]["environment_rebaselined"])
        self.assertTrue(data["resume_provenance"]["legacy_evidence_untrusted"])
        self.assertEqual(
            checkpoint, data["resume_provenance"]["checkpoint_sha"]
        )
        # The old forged verifier/final records are gone, never trusted.
        self.assertNotIn("verifier", data)
        self.assertNotIn("final", data)
        self.assertNotIn("result", data)
        on_disk = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertNotIn("verifier", on_disk)
        self.assertNotIn("final", on_disk)
        # Saved code and the exact checkpoint commit are preserved.
        self.assertEqual(checkpoint, run_git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(
            "VALUE = 2\n", (self.repo / "app.py").read_text(encoding="utf-8")
        )
        self.assertTrue((self.repo / "canary.py").exists())

    def test_fresh_checkpoint_429_ref_is_resumed(self):
        checkpoint = self.make_checkpoint()
        request = self.request(
            checkpoint,
            checkpoint_ref=(
                "opencode/429-checkpoint-opencode-429-issue-issue454-gen-1"
            ),
        )
        data, reused = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env, resume=request
        )
        self.assertFalse(reused)
        self.assertEqual(self.base, data["baseline"]["base_sha"])
        self.assertEqual(
            checkpoint, data["resume_provenance"]["checkpoint_sha"]
        )

    def test_checkpoint_at_the_expected_base_still_discards_old_evidence(self):
        # An interrupted run can save a checkpoint whose tip is already the
        # expected base (no implementation change yet). It must still be
        # re-baselined from its original base; a stale attested final section
        # may never certify this run.
        checkpoint = self.make_checkpoint()
        data, reused = verification.ensure_baseline(
            self.repo,
            self.evidence,
            checkpoint,
            self.env,
            resume=self.request(checkpoint, base_sha=checkpoint),
        )
        self.assertFalse(reused)
        self.assertEqual(self.base, data["baseline"]["base_sha"])
        self.assertNotIn("verifier", data)
        self.assertNotIn("final", data)
        self.assertNotIn("result", data)

    # -- deterministic negative coverage --------------------------------
    def test_wrong_base_is_rejected(self):
        checkpoint = self.make_checkpoint()
        unrelated = run_git(
            self.repo,
            "commit-tree",
            run_git(self.repo, "rev-parse", self.base + "^{tree}"),
            "-m",
            "unrelated root",
        )
        self.assert_rejected(
            "wrong-base", self.request(checkpoint, base_sha=unrelated)
        )

    def test_wrong_issue_is_rejected(self):
        checkpoint = self.make_checkpoint()
        self.assert_rejected("wrong-issue", self.request(checkpoint, issue=999))

    def test_origin_repository_binding_requires_the_exact_slug(self):
        run_git(
            self.repo,
            "remote",
            "add",
            "origin",
            "https://github.com/kodmial/aa.git",
        )
        checkpoint = self.make_checkpoint()
        data, reused = verification.ensure_baseline(
            self.repo,
            self.evidence,
            self.base,
            self.env,
            resume=self.request(checkpoint),
        )
        self.assertFalse(reused)
        self.assertEqual(self.base, data["baseline"]["base_sha"])

        run_git(
            self.repo,
            "remote",
            "set-url",
            "origin",
            "https://github.com/evil/kodmial/aa-extra.git",
        )
        self.assert_rejected(
            "wrong-repository", self.request(checkpoint)
        )

    def test_wrong_generation_is_rejected(self):
        checkpoint = self.make_checkpoint()
        self.assert_rejected(
            "wrong-generation", self.request(checkpoint, generation=2)
        )

    def test_wrong_ref_is_rejected(self):
        checkpoint = self.make_checkpoint()
        self.assert_rejected(
            "wrong-ref",
            self.request(checkpoint, checkpoint_ref="opencode/checkpoint-issue999"),
        )

    def test_wrong_sha_is_rejected(self):
        self.make_checkpoint()
        self.assert_rejected("wrong-sha", self.request("c" * 40))

    def test_stale_workflow_context_is_rejected(self):
        self.make_checkpoint()
        self.assert_rejected(
            "checkpoint-mismatch", self.request(self.base)
        )

    def test_contract_drift_is_rejected(self):
        evidence = self.legacy_evidence()
        evidence["contract"]["fingerprint"] = "d" * 64
        evidence["baseline"]["contract"]["fingerprint"] = "d" * 64
        checkpoint = self.make_checkpoint(evidence=evidence)
        self.assert_rejected("contract-drift", self.request(checkpoint))

    def test_tampered_evidence_is_rejected(self):
        evidence = self.legacy_evidence(base_sha="e" * 40)
        checkpoint = self.make_checkpoint(evidence=evidence)
        self.assert_rejected(
            "evidence-base-mismatch", self.request(checkpoint)
        )

    def test_missing_baseline_ability_is_rejected(self):
        checkpoint = self.make_checkpoint()
        self.env.pop("CONTINUUM_TEST_COMMAND", None)
        self.assert_rejected("contract-unavailable", self.request(checkpoint))

    def test_dirty_checkpoint_worktree_is_rejected(self):
        checkpoint = self.make_checkpoint()
        (self.repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assert_rejected("dirty-worktree", self.request(checkpoint))

    def test_workflow_owned_dirt_does_not_block_resume(self):
        checkpoint = self.make_checkpoint()
        (self.repo / ".continuum-429-stage").write_text(
            "implementation-incomplete\n", encoding="utf-8"
        )
        data, _ = verification.ensure_baseline(
            self.repo,
            self.evidence,
            self.base,
            self.env,
            resume=self.request(checkpoint),
        )
        self.assertEqual(self.base, data["baseline"]["base_sha"])

    def test_transient_git_failure_is_not_a_durable_hold(self):
        self.assertFalse(
            checkpoint_resume.should_hold(
                checkpoint_resume.ResumeError("git-unavailable", "temporary")
            )
        )
        self.assertFalse(
            checkpoint_resume.should_hold(
                checkpoint_resume.ResumeError("worktree-unavailable", "temporary")
            )
        )
        self.assertTrue(
            checkpoint_resume.should_hold(
                checkpoint_resume.ResumeError("contract-unavailable", "missing")
            )
        )

    def test_marker_record_round_trips_and_releases_on_premise_change(self):
        checkpoint = self.make_checkpoint()
        error = self.assert_rejected(
            "wrong-issue", self.request(checkpoint, issue=999)
        )
        record = checkpoint_resume.failure_record(
            checkpoint_resume.ResumeRequest.from_mapping(
                self.request(checkpoint)
            ),
            error,
        )
        marker = record["marker"]
        self.assertTrue(marker.startswith(checkpoint_resume.FAILURE_MARKER_PREFIX))
        parsed = checkpoint_resume.parse_failure_markers(marker)
        self.assertEqual(1, len(parsed))
        self.assertEqual(checkpoint, parsed[0]["checkpoint_sha"])
        self.assertEqual("wrong-issue", parsed[0]["reason"])
        self.assertEqual("", parsed[0]["episode"])
        self.assertFalse(
            checkpoint_resume.premise_changed(
                parsed[0],
                snapshot_spec="a" * 64,
                checkpoint_sha=checkpoint,
                base_sha=self.base,
            )
        )
        self.assertTrue(
            checkpoint_resume.premise_changed(
                parsed[0],
                snapshot_spec="b" * 64,
                checkpoint_sha=checkpoint,
                base_sha=self.base,
            )
        )
        self.assertTrue(
            checkpoint_resume.premise_changed(
                parsed[0],
                snapshot_spec="a" * 64,
                checkpoint_sha="f" * 40,
                base_sha=self.base,
            )
        )

    def test_repeated_identity_uses_a_distinct_failure_episode(self):
        checkpoint = self.make_checkpoint()
        error = self.assert_rejected(
            "wrong-issue", self.request(checkpoint, issue=999)
        )
        request = checkpoint_resume.ResumeRequest.from_mapping(
            self.request(checkpoint)
        )
        first = checkpoint_resume.failure_record(
            request, error, episode="12345-1"
        )
        second = checkpoint_resume.failure_record(
            request, error, episode="12345-2"
        )
        self.assertNotEqual(first["marker_line"], second["marker_line"])
        self.assertIn("episode=12345-1", first["marker_line"])
        self.assertIn("episode=12345-2", second["marker_line"])
        self.assertEqual(
            "12345-1",
            checkpoint_resume.parse_failure_markers(
                first["marker"]
            )[0]["episode"],
        )
        self.assertEqual(
            "12345-2",
            checkpoint_resume.parse_failure_markers(
                second["marker"]
            )[0]["episode"],
        )

    def test_failure_marker_publication_is_retried_and_confirmed(self):
        workflow = (
            ROOT / ".github" / "workflows" / "continuum-opencode.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("resume_hold_marker_published() {", workflow)
        self.assertIn("for resume_publish_attempt in 1 2 3; do", workflow)
        self.assertIn("RESUME_MARKER_CONFIRMED=true", workflow)
        self.assertIn("! resume_hold_marker_published; then", workflow)
        self.assertIn(
            "refusing to continue without a durable rejection record for "
            "this checkpoint.",
            workflow,
        )
        self.assertNotIn(
            '--body "$RESUME_MARKER" >/dev/null 2>&1 || true', workflow
        )


if __name__ == "__main__":
    unittest.main()
