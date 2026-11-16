"""Executable test for the production 429 checkpoint evacuator (#350).

The red test for the live failure extracts the *actual* Bash here-doc from
`.github/workflows/continuum-opencode.yml`, runs it against a disposable Git
repository, and inspects the pushed ref. It does not use a test-only mirror of
the helper as the oracle.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
STAGING_LIBRARY = ROOT / ".github" / "scripts" / "continuum_pr_staging.sh"
TMP_PARENT = ROOT / ".opencode-tmp"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}


def extracted_evacuator() -> str:
    body = WORKFLOW.read_text(encoding="utf-8")
    marker = "cat > \"$helper\" <<'SH'"
    start = body.index(marker) + len(marker)
    end = body.index("\n          SH\n", start)
    return textwrap.dedent(body[start:end])


def run_git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "git {} failed: {}".format(" ".join(args), completed.stderr[-2000:])
        )
    return (completed.stdout or "").strip()


class ProductionEvacuatorTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="evac-", dir=str(TMP_PARENT)))
        self.origin = self.workdir / "origin.git"
        self.repo = self.workdir / "work"
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        self.runner = self.workdir / "runner"
        self.runner.mkdir()
        shutil.copy(STAGING_LIBRARY, self.runner / "continuum-pr-staging.sh")
        self.github_env = self.workdir / "github-env"
        self.gh_log = self.workdir / "gh.log"
        subprocess.run(
            ["git", "init", "--bare", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.repo)],
            check=True,
            capture_output=True,
        )
        run_git(self.repo, "config", "user.email", "test@example.com")
        run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "initial")
        run_git(self.repo, "branch", "-M", "main")
        run_git(self.repo, "push", "-u", "origin", "main")
        self.main_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "checkout", "-b", "opencode/issue350-test")
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "$*" >> "${CONTINUUM_TEST_GH_LOG:-/dev/null}"\n'
            "echo main\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.script = self.workdir / "evacuate.sh"
        self.script.write_text(extracted_evacuator(), encoding="utf-8")
        self.base_sha = run_git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def remote_ref_tips(self):
        tips = {}
        for line in run_git(self.repo, "ls-remote", "origin").splitlines():
            sha, _, ref = line.partition("\t")
            if ref:
                tips[ref] = sha
        return tips

    def invoke(self, operation="opencode-429-issue-issue350", mode="issue",
               issue="350", pr="", stage="implementation-incomplete",
               legacy_ref=None, generation="0", failure_kind="quota_429",
               head_ref="opencode/issue350-test"):
        call = self.workdir / "run-evacuator.sh"
        call.write_text(
            "set -u\n"
            + self.script.read_text(encoding="utf-8")
            + "\ncontinuum_infra_evacuate \"{}\" \"{}\" "
            "\"{}\" \"{}\" \"{}\" \"{}\" \"{}\"\n".format(
                operation, stage, mode, issue, pr, self.base_sha, failure_kind
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(GIT_ENV)
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_ENV": str(self.github_env),
                "RUNNER_TEMP": str(self.runner),
                "HEAD_REF": head_ref,
                "RECOVERY_GENERATION_IN": generation,
                "CONTINUUM_TEST_GH_LOG": str(self.gh_log),
            }
        )
        if legacy_ref is not None:
            env["CONTINUUM_429_LEGACY_REF"] = legacy_ref
        return subprocess.run(
            ["bash", str(call)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_production_helper_writes_and_pushes_valid_metadata(self):
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        completed = self.invoke()
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertIn("Evacuated 429 checkpoint", output)
        self.assertNotIn("not enough values to unpack", output)
        self.assertNotIn("RECOVERY_METADATA_FAILED", output)
        env = self.github_env.read_text(encoding="utf-8")
        self.assertIn(
            "CONTINUUM_429_CHECKPOINT_REF=opencode/429-checkpoint-"
            "opencode-429-issue-issue350-gen-1",
            env,
        )
        ref = "opencode/429-checkpoint-opencode-429-issue-issue350-gen-1"
        checkout = self.workdir / "verify"
        subprocess.run(
            ["git", "clone", str(self.origin), str(checkout)],
            check=True,
            capture_output=True,
        )
        run_git(checkout, "fetch", "--no-tags", "origin", ref)
        run_git(checkout, "switch", "--detach", "FETCH_HEAD")
        metadata = checkout / ".continuum-429-recovery.json"
        self.assertTrue(metadata.is_file(), "checkpoint lacks recovery metadata")
        text = metadata.read_text(encoding="utf-8")
        for field in (
            '"schema_version": 1',
            '"operation_id": "opencode-429-issue-issue350"',
            '"generation": 1',
            '"stage": "implementation-incomplete"',
            '"mode": "issue"',
            '"issue_number": "350"',
            '"head_ref": "opencode/issue350-test"',
            '"branch": "opencode/issue350-test"',
        ):
            self.assertIn(field, text)
        self.assertRegex(text, r'"head_sha": "[0-9a-f]{40}"')
        self.assertEqual(
            (checkout / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n"
        )

    def test_metadata_failure_preserves_raw_work_without_false_pointer(self):
        (self.repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        # Force metadata generation to fail without masking it.
        (self.repo / ".continuum-429-recovery.json").mkdir()
        completed = self.invoke()
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertIn("CONTINUUM_OPENCODE_429_RECOVERY_METADATA_FAILED", output)
        self.assertNotIn("Evacuated 429 checkpoint", output)
        env = self.github_env.read_text(encoding="utf-8")
        self.assertIn("CONTINUUM_429_CHECKPOINT_REF=\n", env + "\n")
        self.assertIn("CONTINUUM_429_SALVAGE_REF=opencode/429-salvage-", env)
        refs = run_git(self.repo, "ls-remote", "origin").splitlines()
        self.assertTrue(
            any("refs/heads/opencode/429-salvage-" in line for line in refs),
            "raw work was not preserved on a salvage ref",
        )
        self.assertFalse(
            any("refs/heads/opencode/429-checkpoint-" in line for line in refs),
            "a false recoverable checkpoint pointer was published",
        )

    def test_set_u_unset_legacy_ref_keeps_published_checkpoint(self):
        # Exact live reproducer: the CodeRabbit repair path invokes the
        # evacuator without CONTINUUM_429_LEGACY_REF while the shell runs
        # under `set -u`. The omitted optional mirror must never turn a
        # successfully published primary checkpoint into a fatal shell error.
        run_git(self.repo, "checkout", "-b", "opencode/pr459-review-repair")
        (self.repo / "app.py").write_text("VALUE = 459\n", encoding="utf-8")
        operation = "opencode-429-coderabbit-fix-pr459-review5909047979"
        completed = self.invoke(
            operation=operation,
            mode="coderabbit-fix",
            issue="",
            pr="459",
            stage="review-repair",
            head_ref="opencode/pr459-review-repair",
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertNotIn("unbound variable", output)
        self.assertNotIn("refusing legacy checkpoint mirror", output)
        self.assertIn("Evacuated 429 checkpoint", output)
        ref = "opencode/429-checkpoint-{}-gen-1".format(operation)
        env = self.github_env.read_text(encoding="utf-8")
        self.assertIn("CONTINUUM_429_CHECKPOINT_REF={}".format(ref), env)
        tips = self.remote_ref_tips()
        self.assertIn("refs/heads/{}".format(ref), tips)
        checkpoints = [
            name for name in tips
            if name.startswith("refs/heads/opencode/429-checkpoint-")
        ]
        self.assertEqual(checkpoints, ["refs/heads/{}".format(ref)])

    def test_valid_same_repo_legacy_ref_is_mirrored(self):
        run_git(self.repo, "checkout", "-b", "opencode/pr459-legacy")
        (self.repo / "app.py").write_text("VALUE = 460\n", encoding="utf-8")
        operation = "opencode-429-coderabbit-fix-pr459-review5909047980"
        legacy = "opencode/issue459-123456789"
        completed = self.invoke(
            operation=operation,
            mode="coderabbit-fix",
            issue="",
            pr="459",
            stage="review-repair",
            legacy_ref=legacy,
            head_ref="opencode/pr459-legacy",
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertNotIn("unbound variable", output)
        self.assertIn("Evacuated 429 checkpoint", output)
        tips = self.remote_ref_tips()
        checkpoint = "refs/heads/opencode/429-checkpoint-{}-gen-1".format(
            operation
        )
        self.assertIn(checkpoint, tips)
        self.assertIn("refs/heads/{}".format(legacy), tips)
        self.assertEqual(tips["refs/heads/{}".format(legacy)], tips[checkpoint])

    def test_default_branch_and_malicious_legacy_refs_are_rejected(self):
        for index, legacy in enumerate(
            ("main", "refs/heads/main", "HEAD", "../escape", "a..b")
        ):
            with self.subTest(legacy=legacy):
                operation = "opencode-429-issue-issue46{}".format(index)
                completed = self.invoke(
                    operation=operation,
                    issue="46{}".format(index),
                    legacy_ref=legacy,
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode, 75, output)
                self.assertNotIn("unbound variable", output)
                self.assertIn("Evacuated 429 checkpoint", output)
                self.assertIn(
                    "refusing legacy checkpoint mirror", output
                )
                tips = self.remote_ref_tips()
                self.assertIn(
                    "refs/heads/opencode/429-checkpoint-{}-gen-1".format(
                        operation
                    ),
                    tips,
                )
                # The default branch never advances to a checkpoint commit.
                self.assertEqual(tips["refs/heads/main"], self.main_sha)
                if legacy not in ("main", "refs/heads/main"):
                    self.assertNotIn(
                        "refs/heads/{}".format(legacy), tips
                    )
                self.assertNotIn(
                    "refs/heads/opencode/429-checkpoint-{}-gen-2".format(
                        operation
                    ),
                    tips,
                )

    def test_provider_429_fresh_runner_handoff_preserves_provenance(self):
        from continuum.opencode_429_recovery import is_opencode_429

        provider_429 = json.dumps(
            {
                "type": "error",
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": "free usage limit reached",
                        "statusCode": 429,
                    },
                },
            }
        )
        self.assertTrue(is_opencode_429(provider_429, 1))
        run_git(self.repo, "checkout", "-b", "opencode/pr459-provenance")
        (self.repo / "app.py").write_text("VALUE = 461\n", encoding="utf-8")
        operation = "opencode-429-coderabbit-fix-pr459-review5909047981"
        completed = self.invoke(
            operation=operation,
            mode="coderabbit-fix",
            issue="",
            pr="459",
            stage="review-repair",
            head_ref="opencode/pr459-provenance",
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertNotIn("unbound variable", output)
        self.assertIn("Evacuated 429 checkpoint", output)
        ref = "opencode/429-checkpoint-{}-gen-1".format(operation)
        checkpoint_sha = self.remote_ref_tips()["refs/heads/{}".format(ref)]

        # A real fresh runner clones the same repository and runs the exact
        # production restore step with the evacuation provenance.
        fresh = self.workdir / "fresh-runner"
        subprocess.run(
            ["git", "clone", str(self.origin), str(fresh)],
            check=True,
            capture_output=True,
        )
        restore = self.workdir / "restore.sh"
        restore.write_text(extracted_restore_step(), encoding="utf-8")
        restore_output = self.workdir / "restore-output"
        restore_env = dict(os.environ)
        restore_env.update(
            {
                "RECOVERY_OPERATION_ID": operation,
                "RECOVERY_CHECKPOINT_REF": ref,
                "RECOVERY_CHECKPOINT_SHA": checkpoint_sha,
                "RECOVERY_STAGE": "review-repair",
                "RECOVERY_GENERATION": "1",
                "RECOVERY_MODE": "coderabbit-fix",
                "GITHUB_OUTPUT": str(restore_output),
                "RUNNER_TEMP": str(self.runner),
            }
        )
        restored = subprocess.run(
            ["bash", str(restore)],
            cwd=str(fresh),
            capture_output=True,
            text=True,
            env=restore_env,
            timeout=120,
        )
        restore_log = restored.stdout + restored.stderr
        self.assertEqual(restored.returncode, 0, restore_log)
        self.assertIn(
            "Validated 429 recovery metadata for {} generation 1 stage "
            "review-repair".format(operation),
            restore_log,
        )
        self.assertIn("Restored 429 checkpoint {}".format(ref), restore_log)
        outputs = restore_output.read_text(encoding="utf-8")
        for expected in (
            "resumed=true",
            "checkpoint_sha={}".format(checkpoint_sha),
            "checkpoint_ref={}".format(ref),
            "operation_id={}".format(operation),
            "generation=1",
            "stage=review-repair",
            "skip_opencode=false",
        ):
            self.assertIn(expected, outputs)
        self.assertEqual(
            run_git(fresh, "rev-parse", "HEAD"), checkpoint_sha
        )
        metadata = json.loads(
            (fresh / ".continuum-429-recovery.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(metadata["operation_id"], operation)
        self.assertEqual(metadata["failure_kind"], "quota_429")
        self.assertEqual(metadata["generation"], 1)
        self.assertEqual(metadata["checkpoint_ref"], ref)
        self.assertEqual(metadata["stage"], "review-repair")
        self.assertEqual(metadata["mode"], "coderabbit-fix")
        self.assertEqual(str(metadata["issue_number"]), "")
        self.assertEqual(str(metadata["pr_number"]), "459")
        self.assertEqual(metadata["head_ref"], "opencode/pr459-provenance")
        self.assertEqual(metadata["base_sha"], self.base_sha)
        self.assertEqual(metadata["branch"], "opencode/pr459-provenance")
        self.assertRegex(metadata["head_sha"], r"^[0-9a-f]{40}$")

        # Evacuation only queried the repository default branch; it never
        # created a stray PR or duplicate issue as a recovery side effect.
        gh_calls = self.gh_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(gh_calls)
        self.assertEqual(
            set(gh_calls),
            {"api repos/owner/repo --jq .default_branch"},
        )


if __name__ == "__main__":
    unittest.main()


def extracted_restore_step() -> str:
    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["opencode"]["steps"]:
        if step.get("id") == "recovery_restore":
            return step["run"]
    raise AssertionError("recovery_restore step not found")


class ProductionRestoreTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="restore-", dir=str(TMP_PARENT)))
        self.origin = self.workdir / "origin.git"
        self.repo = self.workdir / "work"
        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.repo)], check=True, capture_output=True)
        run_git(self.repo, "config", "user.email", "test@example.com")
        run_git(self.repo, "config", "user.name", "Test")
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "code head")
        run_git(self.repo, "branch", "-M", "main")
        run_git(self.repo, "push", "-u", "origin", "main")
        run_git(self.repo, "checkout", "-b", "opencode/issue350-test")
        self.code_sha = run_git(self.repo, "rev-parse", "HEAD")
        self.ref = "opencode/429-checkpoint-opencode-429-issue-issue350-gen-1"
        self.metadata = {
            "schema_version": 1,
            "operation_id": "opencode-429-issue-issue350",
            "failure_kind": "quota_429",
            "generation": 1,
            "checkpoint_ref": self.ref,
            "stage": "implementation-incomplete",
            "mode": "issue",
            "issue_number": "350",
            "pr_number": "",
            "head_ref": "opencode/issue350-test",
            "base_sha": "b" * 40,
            "head_sha": self.code_sha,
            "branch": "opencode/issue350-test",
        }
        self.write_metadata(self.metadata)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "metadata")
        self.checkpoint_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "push", "origin", f"HEAD:refs/heads/{self.ref}")
        run_git(self.repo, "checkout", "opencode/issue350-test")
        self.script = self.workdir / "restore.sh"
        self.script.write_text(extracted_restore_step(), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def write_metadata(self, data):
        import json

        (self.repo / ".continuum-429-recovery.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def invoke(self):
        output = self.workdir / "github-output"
        env = dict(os.environ)
        env.update(GIT_ENV)
        env.update(
            {
                "RECOVERY_OPERATION_ID": "opencode-429-issue-issue350",
                "RECOVERY_CHECKPOINT_REF": self.ref,
                "RECOVERY_CHECKPOINT_SHA": self.checkpoint_sha,
                "RECOVERY_STAGE": "implementation-incomplete",
                "RECOVERY_GENERATION": "1",
                "RECOVERY_MODE": "issue",
                "GITHUB_OUTPUT": str(output),
            }
        )
        return subprocess.run(
            ["bash", str(self.script)],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_production_restore_validates_metadata_and_resumes(self):
        completed = self.invoke()
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("Validated 429 recovery metadata", output)
        self.assertIn("Restored 429 checkpoint", output)
        data = (self.workdir / "github-output").read_text(encoding="utf-8")
        self.assertIn("resumed=true", data)
        self.assertIn(f"checkpoint_sha={self.checkpoint_sha}", data)

    def test_production_restore_rejects_tampered_metadata(self):
        run_git(self.repo, "checkout", self.code_sha)
        tampered = dict(self.metadata)
        tampered["head_sha"] = "c" * 40
        self.write_metadata(tampered)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "tampered metadata")
        bad_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "push", "--force", "origin", f"{bad_sha}:refs/heads/{self.ref}")
        run_git(self.repo, "checkout", "opencode/issue350-test")
        run_git(self.repo, "fetch", "--no-tags", "origin", self.ref)
        self.assertEqual(run_git(self.repo, "rev-parse", "FETCH_HEAD"), bad_sha)
        blob = run_git(self.repo, "show", "FETCH_HEAD:.continuum-429-recovery.json")
        self.assertIn('"head_sha": "cccccccccccccccccccccccccccccccccccccccc"', blob)
        env_sha = self.checkpoint_sha
        self.checkpoint_sha = bad_sha
        completed = self.invoke()
        self.checkpoint_sha = env_sha
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("head SHA is outside the checkpoint history", output)
        self.assertNotIn("Restored 429 checkpoint", output)
