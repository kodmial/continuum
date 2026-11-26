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


def extracted_issue_resume_supersession() -> str:
    """The exact fail-closed generation gate used by the issue step."""

    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["opencode"]["steps"]:
        if step.get("name") != "Implement issue":
            continue
        script = step["run"]
        start_marker = (
            "# kodmial/continuum#460: a resumed checkpoint is only current"
        )
        end_marker = "# kodmial/continuum#460: a resumed checkpoint carries"
        start = script.index(start_marker)
        end = script.index(end_marker, start)
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
               issue="350", pr="", prefix="", stage="implementation-incomplete",
               legacy_ref=None, generation="0", failure_kind="quota_429",
               head_ref="opencode/issue350-test", resume_active="false",
               resume_generation="1", model=""):
        call = self.workdir / "run-evacuator.sh"
        call.write_text(
            "set -u\n"
            + prefix
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
                "RESUME_ACTIVE": resume_active,
                "RESUME_GENERATION": resume_generation,
                "CONTINUUM_TEST_GH_LOG": str(self.gh_log),
                "OPENCODE_MODEL": model,
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

    def test_pinned_route_quota_preflight_preserves_checkpoint_and_model(self):
        import yaml
        model = "opencode/example-free"
        completed = self.invoke(model=model)
        self.assertEqual(completed.returncode, 75, completed.stdout + completed.stderr)
        operation = "opencode-429-issue-issue350"
        first_ref = "opencode/429-checkpoint-" + operation + "-gen-1"
        old_sha = self.remote_ref_tips()["refs/heads/" + first_ref]
        fresh = self.workdir / "pinned-preflight"
        subprocess.run(["git", "clone", str(self.origin), str(fresh)], check=True, capture_output=True)
        run_git(fresh, "fetch", "origin", first_ref)
        run_git(fresh, "switch", "--detach", "FETCH_HEAD")
        # This is the actual workflow-owned marker from a confirmed failed
        # pinned route probe; no primary implementation or verifier ran.
        (self.runner / "continuum-opencode-429").touch()
        shutil.copy(self.script, self.runner / "continuum-429-evacuate.sh")
        steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["opencode"]["steps"]
        step = next(s for s in steps if s.get("name") == "Preserve restored issue checkpoint after pinned route quota 429")
        self.assertIn("steps.model_route.outcome == 'failure'", step["if"])
        self.assertIn(
            "(inputs.mode == 'issue' || needs.admit.outputs.route == 'issue')",
            step["if"],
        )
        self.assertNotIn("steps.recovery_restore.outputs.resumed == 'true'", step["if"])
        # The guard reads with the scoped token; code pushes retain the
        # already-configured checkout credential, without new PAT bindings.
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertNotIn("GITHUB_TOKEN", step["env"])
        checkout = next(s for s in steps if s.get("name") == "Checkout repository")
        self.assertTrue(checkout["with"]["persist-credentials"])
        self.assertEqual(checkout["with"]["token"], "${{ secrets.TAP_PAT }}")
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   RUNNER_TEMP=str(self.runner), GITHUB_ENV=str(self.github_env),
                   GITHUB_REPOSITORY="owner/repo", GITHUB_RUN_ID="400",
                   ISSUE_NUMBER="350", RECOVERY_RESUMED="true",
                   RECOVERY_OPERATION_ID=operation,
                   RECOVERY_GENERATION_IN="1", RECOVERY_STAGE_IN="implementation-incomplete",
                   CHECKPOINT_SHA=old_sha, CHECKPOINT_REF=first_ref,
                   OPENCODE_MODEL=model)
        env.update(GIT_ENV)
        result = subprocess.run(["bash", "-c", step["run"]], cwd=fresh,
                                env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        new_ref = "opencode/429-checkpoint-" + operation + "-gen-2"
        tips = self.remote_ref_tips()
        self.assertEqual(tips["refs/heads/" + first_ref], old_sha)
        self.assertIn("refs/heads/" + new_ref, tips)
        new_metadata = json.loads(run_git(fresh, "show", "HEAD:.continuum-429-recovery.json"))
        self.assertEqual(new_metadata["model"], model)
        self.assertEqual(new_metadata["generation"], 2)
        self.assertEqual(new_metadata["stage"], "implementation-incomplete")

    def route_preservation_step(self):
        import yaml
        steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["opencode"]["steps"]
        return next(s for s in steps if s.get("name") == "Preserve restored issue checkpoint after pinned route quota 429")

    def route_preservation_env(self, **overrides):
        env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            RUNNER_TEMP=str(self.runner),
            GITHUB_ENV=str(self.github_env),
            GITHUB_REPOSITORY="owner/repo",
            GITHUB_RUN_ID="400",
            ISSUE_NUMBER="350",
        )
        env.update(overrides)
        env.update(GIT_ENV)
        return env

    def prepare_route_probe_checkpoint(self, model=""):
        completed = self.invoke(model=model)
        self.assertEqual(completed.returncode, 75, completed.stdout + completed.stderr)
        operation = "opencode-429-issue-issue350"
        first_ref = "opencode/429-checkpoint-" + operation + "-gen-1"
        old_sha = self.remote_ref_tips()["refs/heads/" + first_ref]
        (self.runner / "continuum-opencode-429").touch()
        shutil.copy(self.script, self.runner / "continuum-429-evacuate.sh")
        return operation, first_ref, old_sha

    def test_legacy_restored_checkpoint_without_model_still_evacuates(self):
        operation, first_ref, old_sha = self.prepare_route_probe_checkpoint()
        fresh = self.workdir / "legacy-restored"
        subprocess.run(
            ["git", "clone", str(self.origin), str(fresh)],
            check=True, capture_output=True,
        )
        run_git(fresh, "fetch", "origin", first_ref)
        run_git(fresh, "switch", "--detach", "FETCH_HEAD")
        step = self.route_preservation_step()
        env = self.route_preservation_env(
            GITHUB_RUN_ID="401",
            RECOVERY_RESUMED="true",
            RECOVERY_OPERATION_ID=operation,
            RECOVERY_GENERATION_IN="1",
            RECOVERY_STAGE_IN="implementation-incomplete",
            CHECKPOINT_SHA=old_sha,
            CHECKPOINT_REF=first_ref,
            OPENCODE_MODEL="",
        )
        result = subprocess.run(
            ["bash", "-c", step["run"]], cwd=fresh, env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        new_ref = "opencode/429-checkpoint-" + operation + "-gen-2"
        tips = self.remote_ref_tips()
        self.assertEqual(tips["refs/heads/" + first_ref], old_sha)
        self.assertIn("refs/heads/" + new_ref, tips)
        new_metadata = json.loads(
            run_git(fresh, "show", "HEAD:.continuum-429-recovery.json")
        )
        self.assertEqual(new_metadata["model"], "")
        self.assertEqual(new_metadata["generation"], 2)
        self.assertEqual(new_metadata["stage"], "implementation-incomplete")

    def test_manual_issue_checkpoint_discovery_evacuates_without_resume(self):
        model = "opencode/example-free"
        operation, first_ref, old_sha = self.prepare_route_probe_checkpoint(
            model=model
        )
        run_git(
            self.repo, "push", "origin",
            "{}:refs/heads/opencode/checkpoint-issue350".format(old_sha),
        )
        fresh = self.workdir / "manual-discovery"
        subprocess.run(
            ["git", "clone", str(self.origin), str(fresh)],
            check=True, capture_output=True,
        )
        step = self.route_preservation_step()
        env = self.route_preservation_env(
            GITHUB_RUN_ID="402",
            RECOVERY_RESUMED="",
            RECOVERY_OPERATION_ID="",
            RECOVERY_GENERATION_IN="",
            RECOVERY_STAGE_IN="",
            CHECKPOINT_SHA="",
            CHECKPOINT_REF="",
            OPENCODE_MODEL=model,
        )
        result = subprocess.run(
            ["bash", "-c", step["run"]], cwd=fresh, env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        new_ref = "opencode/429-checkpoint-" + operation + "-gen-2"
        tips = self.remote_ref_tips()
        self.assertEqual(tips["refs/heads/" + first_ref], old_sha)
        self.assertIn("refs/heads/" + new_ref, tips)
        new_metadata = json.loads(
            run_git(fresh, "show", "HEAD:.continuum-429-recovery.json")
        )
        self.assertEqual(new_metadata["model"], model)
        self.assertEqual(new_metadata["generation"], 2)
        self.assertEqual(new_metadata["stage"], "implementation-incomplete")
        self.assertEqual(new_metadata["checkpoint_ref"], new_ref)

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
            model="opencode/example-free",
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
            "model=opencode/example-free",
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
        self.assertEqual(metadata["model"], "opencode/example-free")
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
                "RUNNER_TEMP": str(self.workdir),
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

    def model_pin(self, *, model="", manual=False, pool="opencode/example-free,opencode/other-free", legacy=""):
        import yaml
        steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["opencode"]["steps"]
        step = next(s for s in steps if s.get("id") == "recovery_model")
        route = next(s for s in steps if s.get("id") == "model_route")
        self.assertLess(steps.index(step), steps.index(route))
        self.assertEqual(route["with"]["explicit-pin"], "${{ steps.recovery_model.outputs.model }}")
        script = self.workdir / "model-pin.sh"
        script.write_text(step["run"], encoding="utf-8")
        output = self.workdir / "model-output"
        output.unlink(missing_ok=True)
        env = dict(os.environ, RECOVERY_MODEL=model, RECOVERY_RESUMED="false" if manual else "true",
                   ISSUE_NUMBER="350", DISPATCH_MODE="issue", ADMITTED_ROUTE="issue",
                   TRUSTED_MODEL_POOL=pool, TRUSTED_LEGACY_MODEL=legacy,
                   GITHUB_REPOSITORY="owner/repo", GITHUB_OUTPUT=str(output))
        result = subprocess.run(["bash", str(script)], cwd=self.repo, env=env,
                                capture_output=True, text=True, timeout=120)
        return result, output.read_text() if output.exists() else ""

    def test_bound_checkpoint_model_is_pinned_or_rejected_before_routing(self):
        result, output = self.model_pin(model="opencode/example-free")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "model=opencode/example-free\n")
        result, output = self.model_pin(model="opencode/not-approved-free")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        result, output = self.model_pin(model="opencode/not-approved-free",
                                        pool="", legacy="opencode/example-free")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        result, output = self.model_pin()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "model=\n")
        self.assertIn("Legacy checkpoint", result.stdout)

    def test_manual_issue_mode_resolves_model_from_exact_durable_mirror(self):
        self.metadata.update(repository="owner/repo", model="opencode/example-free")
        self.write_metadata(self.metadata)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "bound checkpoint")
        run_git(self.repo, "push", "origin", f"HEAD:refs/heads/{self.ref}",
                "HEAD:refs/heads/opencode/checkpoint-issue350")
        result, output = self.model_pin(manual=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, "model=opencode/example-free\n")
        # A mirror at a different commit cannot authorize a model pin.
        run_git(self.repo, "commit", "--allow-empty", "-m", "changed mirror")
        run_git(self.repo, "push", "origin", "HEAD:refs/heads/opencode/checkpoint-issue350")
        result, output = self.model_pin(manual=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")

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


class IssueResumeSupersessionTest(unittest.TestCase):
    """Run the workflow's ordinary-issue supersession gate."""

    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="supersession-", dir=str(TMP_PARENT))
        )
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
        self.script = self.workdir / "resume-supersession.sh"
        self.script.write_text(
            extracted_latest_durable_generation()
            + "\n"
            + extracted_issue_resume_supersession(),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def invoke(self, generation, **environment):
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "GITHUB_REPOSITORY": "owner/repo",
                "RESUME_ACTIVE": "true",
                "RESUME_OPERATION": "opencode-429-issue-issue350",
                "RESUME_GENERATION": str(generation),
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

    def test_stale_mirror_generation_fails_before_resume(self):
        completed = self.invoke(
            2,
            FAKE_429_REFS=(
                "refs/heads/opencode/429-checkpoint-"
                "opencode-429-issue-issue350-gen-3"
            ),
        )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("superseded by durable generation 3", output)

    def test_current_generation_resumes(self):
        completed = self.invoke(
            3,
            FAKE_429_REFS=(
                "refs/heads/opencode/429-checkpoint-"
                "opencode-429-issue-issue350-gen-3"
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unreadable_durable_state_fails_closed(self):
        completed = self.invoke(2, FAKE_GH_FAIL="true")
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "Could not read durable recovery generation state",
            output,
        )


if __name__ == "__main__":
    unittest.main()
