"""Executable test for the production 429 checkpoint evacuator (#350).

The red test for the live failure extracts the *actual* Bash here-doc from
`.github/workflows/continuum-opencode.yml`, runs it against a disposable Git
repository, and inspects the pushed ref. It does not use a test-only mirror of
the helper as the oracle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
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
        self.github_env = self.workdir / "github-env"
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
        run_git(self.repo, "checkout", "-b", "opencode/issue350-test")
        gh = self.bin / "gh"
        gh.write_text("#!/usr/bin/env bash\necho main\n", encoding="utf-8")
        gh.chmod(0o755)
        self.script = self.workdir / "evacuate.sh"
        self.script.write_text(extracted_evacuator(), encoding="utf-8")
        self.base_sha = run_git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def invoke(self, operation="opencode-429-issue-issue350", mode="issue",
               issue="350", pr=""):
        call = self.workdir / "run-evacuator.sh"
        call.write_text(
            self.script.read_text(encoding="utf-8")
            + "\ncontinuum_infra_evacuate \"{}\" \"implementation-incomplete\" "
            "\"{}\" \"{}\" \"{}\" \"{}\" \"quota_429\"\n".format(
                operation, mode, issue, pr, self.base_sha
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
                "HEAD_REF": "opencode/issue350-test",
                "RECOVERY_GENERATION_IN": "0",
            }
        )
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
