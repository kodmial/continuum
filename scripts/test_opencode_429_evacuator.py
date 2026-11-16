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


def extracted_resume_generation_carry() -> str:
    """The exact same-step assignment that carries a resumed generation."""

    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["opencode"]["steps"]:
        if step.get("name") != "Implement issue":
            continue
        script = step["run"]
        marker = "# kodmial/continuum#460: a resumed checkpoint carries"
        start = script.index(marker)
        end = script.index("\nfi\n", start) + len("\nfi\n")
        return textwrap.dedent(script[start:end])
    raise AssertionError("Implement issue step not found")


def extracted_latest_durable_generation() -> str:
    """The exact durable-generation reader used before the baseline."""

    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["opencode"]["steps"]:
        if step.get("name") != "Implement issue":
            continue
        script = step["run"]
        marker = "continuum_latest_durable_generation() {"
        start = script.index(marker)
        end = script.index("\n}\n", start) + len("\n}\n")
        return textwrap.dedent(script[start:end])
    raise AssertionError("Implement issue step not found")


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
               issue="350", pr="", prefix="", generation="0",
               resume_active="false", resume_generation="1"):
        call = self.workdir / "run-evacuator.sh"
        call.write_text(
            prefix
            + self.script.read_text(encoding="utf-8")
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
                "RECOVERY_GENERATION_IN": generation,
                "RESUME_ACTIVE": resume_active,
                "RESUME_GENERATION": resume_generation,
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
            '"repository": "owner/repo"',
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

    def test_resumed_generation_evacuates_next_generation_without_overwriting(self):
        # A generation-2 legacy resume on an ordinary issue dispatch starts
        # with an empty RECOVERY_GENERATION_IN. The workflow must carry the
        # validated resume generation into the same-step evacuator so the next
        # 429 writes generation 3 and leaves every earlier checkpoint ref,
        # including generation 1 that the pre-fix code force-pushed over,
        # untouched.
        gen1_ref = "opencode/429-checkpoint-opencode-429-issue-issue350-gen-1"
        gen2_ref = "opencode/429-checkpoint-opencode-429-issue-issue350-gen-2"
        gen3_ref = "opencode/429-checkpoint-opencode-429-issue-issue350-gen-3"
        gen1_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(
            self.repo,
            "push",
            "origin",
            "{}:refs/heads/{}".format(gen1_sha, gen1_ref),
        )
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        run_git(self.repo, "commit", "-am", "generation 2 checkpoint")
        gen2_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(
            self.repo,
            "push",
            "origin",
            "{}:refs/heads/{}".format(gen2_sha, gen2_ref),
        )
        (self.repo / "app.py").write_text("VALUE = 4\n", encoding="utf-8")

        completed = self.invoke(
            prefix=extracted_resume_generation_carry() + "\n",
            generation="",
            resume_active="true",
            resume_generation="2",
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 75, output)
        self.assertIn("Evacuated 429 checkpoint", output)
        env = self.github_env.read_text(encoding="utf-8")
        self.assertIn("CONTINUUM_429_CHECKPOINT_REF={}".format(gen3_ref), env)

        refs = {}
        for line in run_git(self.repo, "ls-remote", "origin").splitlines():
            sha, _, name = line.partition("\t")
            refs[name] = sha
        self.assertEqual(gen1_sha, refs["refs/heads/" + gen1_ref])
        self.assertEqual(gen2_sha, refs["refs/heads/" + gen2_ref])
        self.assertIn("refs/heads/" + gen3_ref, refs)
        self.assertNotEqual(gen1_sha, refs["refs/heads/" + gen3_ref])
        self.assertNotEqual(gen2_sha, refs["refs/heads/" + gen3_ref])

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

    def invoke(self, checkpoint_sha=None, output_name="github-output"):
        output = self.workdir / output_name
        env = dict(os.environ)
        env.update(GIT_ENV)
        env.update(
            {
                "RECOVERY_OPERATION_ID": "opencode-429-issue-issue350",
                "RECOVERY_CHECKPOINT_REF": self.ref,
                "RECOVERY_CHECKPOINT_SHA": (
                    self.checkpoint_sha if checkpoint_sha is None else checkpoint_sha
                ),
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

    def test_omitted_checkpoint_sha_records_the_tip_for_recovery_admission(self):
        """An omitted dispatch SHA must still admit the restored checkpoint.

        ``recovery_checkpoint_sha`` is optional: when the dispatch leaves it
        empty the restore step verifies the ref tip and records that exact
        commit as its ``checkpoint_sha`` output. The duplicate guard forwards
        that output, so admission sees a complete identity instead of failing
        closed on the empty dispatch input.
        """
        completed = self.invoke(checkpoint_sha="", output_name="omitted-output")
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        data = (self.workdir / "omitted-output").read_text(encoding="utf-8")
        self.assertIn("resumed=true", data)
        recorded = ""
        for line in data.splitlines():
            if line.startswith("checkpoint_sha="):
                recorded = line.split("=", 1)[1]
        self.assertEqual(recorded, self.checkpoint_sha)

        from continuum import opencode_admission as admission

        previous = {
            "id": 20,
            "display_title": "OpenCode issue #350",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-01-01T10:00:00Z",
            "updated_at": "2026-01-01T10:05:00Z",
            "name": "OpenCode agent",
            "path": ".github/workflows/continuum-opencode.yml",
        }
        current = {
            "id": 21,
            "display_title": "OpenCode issue #350",
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2026-01-01T10:02:00Z",
            "updated_at": "2026-01-01T10:06:00Z",
            "name": "OpenCode agent",
            "path": ".github/workflows/continuum-opencode.yml",
        }
        identity = {
            "operation_id": "opencode-429-issue-issue350",
            "checkpoint_ref": self.ref,
            "checkpoint_sha": recorded,
            "generation": 1,
        }
        admitted = admission.duplicate_run_decision(
            current=current,
            runs=[previous, current],
            issue_number=350,
            workflow_name="OpenCode agent",
            recovery_identity=identity,
            latest_recovery_generation=1,
        )
        self.assertFalse(admitted["skip"], admitted["reason"])
        self.assertIn("explicit 429 recovery", admitted["reason"])

        omitted = admission.duplicate_run_decision(
            current=current,
            runs=[previous, current],
            issue_number=350,
            workflow_name="OpenCode agent",
            recovery_identity=dict(identity, checkpoint_sha=""),
            latest_recovery_generation=1,
        )
        self.assertTrue(omitted["skip"])
        self.assertIn("malformed", omitted["reason"])

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


class LatestDurableGenerationTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="generation-", dir=str(TMP_PARENT)))
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"${FAKE_GH_FAIL:-}\" == \"true\" ]]; then exit 1; fi\n"
            "case \"$*\" in\n"
            "  *infra-checkpoint*) printf '%s\\n' \"${FAKE_INFRA_REFS:-}\" ;;\n"
            "  *) printf '%s\\n' \"${FAKE_429_REFS:-}\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.script = self.workdir / "latest-generation.sh"
        self.script.write_text(
            extracted_latest_durable_generation()
            + '\ncontinuum_latest_durable_generation "opencode-429-issue-issue350"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def invoke(self, **environment):
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "GITHUB_REPOSITORY": "owner/repo",
            }
        )
        env.update(environment)
        return subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_reads_the_highest_durable_generation(self):
        completed = self.invoke(
            FAKE_429_REFS=(
                "refs/heads/opencode/429-checkpoint-"
                "opencode-429-issue-issue350-gen-1\n"
                "refs/heads/opencode/429-checkpoint-"
                "opencode-429-issue-issue350-gen-3"
            ),
            FAKE_INFRA_REFS=(
                "refs/heads/opencode/infra-checkpoint-"
                "opencode-429-issue-issue350-gen-2"
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual("3", completed.stdout.strip())

    def test_unreadable_durable_state_fails_closed(self):
        completed = self.invoke(FAKE_GH_FAIL="true")
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
