"""Regression tests for kodmial/continuum#374.

A fresh-VM 429 issue recovery must bootstrap the initial checkout from a
repository ref that exists. The burned runner's ``opencode/issueN-RUNID``
branch may never have reached origin (#357 run 39853197635 and #373 run
39866102190 both failed this way), which previously prevented the workflow
from reaching the checkpoint restore step.

These tests read the checked-in production workflow and execute its real
checkout expression and restore script against real Git repositories. They do
not mirror the decision in a separate helper.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
ENGINE = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
SELF_CALLER = ROOT / ".github" / "workflows" / "opencode.yml"
CONSUMER_CALLER = ROOT / ".github" / "caller-stubs" / "continuum-opencode.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}

NODE_EVALUATOR = r"""
const expression = process.argv[1];
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const fn = new Function('inputs', 'github', 'return (' + expression + ');');
process.stdout.write(String(fn(payload.inputs, payload.github)));
"""


def load_workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def job(workflow: dict, name: str) -> dict:
    return workflow["jobs"][name]


def step(workflow_job: dict, name: str) -> dict:
    for item in workflow_job["steps"]:
        if item.get("name") == name:
            return item
    raise AssertionError("missing step: " + name)


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(GIT_ENV)
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            "git {} failed: {}".format(
                " ".join(args), (completed.stderr or "").strip()[-2000:]
            )
        )
    return completed


class BootstrapCheckoutExpressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow(ENGINE)
        cls.opencode = job(cls.workflow, "opencode")
        cls.checkout = step(cls.opencode, "Checkout repository")
        cls.ref_expression = cls.checkout["with"]["ref"]
        match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", cls.ref_expression)
        if not match:
            raise AssertionError("checkout ref is not a single expression")
        cls.expression = match.group(1)

    def resolve(self, inputs: dict, github: dict) -> str:
        completed = subprocess.run(
            ["node", "-e", NODE_EVALUATOR, self.expression],
            input=json.dumps({"inputs": inputs, "github": github}),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def github(self):
        return {
            "ref": "refs/heads/main",
            "event": {"repository": {"default_branch": "main"}},
        }

    def test_357_and_373_ephemeral_branches_are_never_required(self):
        # Exact branch names from the failed recovery runs.
        for dead_ref in (
            "opencode/issue357-39852307581",
            "opencode/issue373-39863508457",
        ):
            for issue in ("357", "373"):
                resolved = self.resolve(
                    {
                        "mode": "issue",
                        "recovery_checkpoint_ref": (
                            "opencode/429-checkpoint-"
                            "opencode-429-issue-issue{}-gen-1".format(issue)
                        ),
                        "base_ref": "main",
                        "head_ref": dead_ref,
                    },
                    self.github(),
                )
                self.assertEqual(
                    resolved,
                    "main",
                    "issue recovery must bootstrap from the trusted base ref, "
                    "not the dead runner branch " + dead_ref,
                )

    def test_issue_recovery_without_explicit_base_uses_default_branch(self):
        resolved = self.resolve(
            {
                "mode": "issue",
                "recovery_checkpoint_ref": "opencode/429-checkpoint-op-gen-1",
                "base_ref": "",
                "head_ref": "opencode/issue373-39863508457",
            },
            self.github(),
        )
        self.assertEqual(resolved, "main")

    def test_ordinary_issue_and_other_modes_keep_existing_ref_semantics(self):
        cases = [
            # Ordinary issue dispatch with no checkpoint is unchanged.
            (
                {
                    "mode": "issue",
                    "recovery_checkpoint_ref": "",
                    "base_ref": "main",
                    "head_ref": "",
                },
                "refs/heads/main",
            ),
            (
                {
                    "mode": "issue",
                    "recovery_checkpoint_ref": "",
                    "base_ref": "main",
                    "head_ref": "feature/normal",
                },
                "feature/normal",
            ),
            # PR repair modes must still use their real pushed PR head ref.
            (
                {
                    "mode": "coderabbit-fix",
                    "recovery_checkpoint_ref": "opencode/429-checkpoint-op-gen-1",
                    "base_ref": "main",
                    "head_ref": "opencode/issue373-39863508457",
                },
                "opencode/issue373-39863508457",
            ),
            (
                {
                    "mode": "ci-fix",
                    "recovery_checkpoint_ref": "opencode/429-checkpoint-op-gen-1",
                    "base_ref": "main",
                    "head_ref": "pr-head",
                },
                "pr-head",
            ),
            (
                {
                    "mode": "resolve-conflict",
                    "recovery_checkpoint_ref": "opencode/429-checkpoint-op-gen-1",
                    "base_ref": "main",
                    "head_ref": "pr-head",
                },
                "pr-head",
            ),
            # Qualification has no head_ref and keeps using github.ref.
            (
                {
                    "mode": "qualification",
                    "recovery_checkpoint_ref": "opencode/429-checkpoint-op-gen-1",
                    "base_ref": "main",
                    "head_ref": "",
                },
                "refs/heads/main",
            ),
        ]
        for inputs, expected in cases:
            with self.subTest(mode=inputs["mode"], head_ref=inputs["head_ref"]):
                self.assertEqual(self.resolve(inputs, self.github()), expected)

    def test_restore_runs_after_bootstrap_and_before_any_model_call(self):
        names = [item.get("name", "") for item in self.opencode["steps"]]
        checkout_index = names.index("Checkout repository")
        restore_index = names.index("Restore 429 recovery checkpoint")
        self.assertLess(checkout_index, restore_index)

        restore = self.opencode["steps"][restore_index]
        self.assertEqual(
            restore.get("if"), "inputs.recovery_checkpoint_ref != ''"
        )
        script = restore["run"]
        for marker in (
            'git fetch --no-tags origin "$RECOVERY_CHECKPOINT_REF"',
            'git show "FETCH_HEAD:.continuum-429-recovery.json"',
            '"git", "merge-base", "--is-ancestor"',
            "git switch --detach FETCH_HEAD",
        ):
            self.assertIn(marker, script)

        model_indexes = [
            index
            for index, item in enumerate(self.opencode["steps"])
            if "continuum-opencode run --auto" in item.get("run", "")
        ]
        self.assertTrue(model_indexes)
        self.assertGreater(min(model_indexes), restore_index)

    def test_watchdog_and_callers_keep_the_recovery_identity_mapping(self):
        watchdog = WATCHDOG.read_text(encoding="utf-8")
        self.assertIn(
            "head_ref: String(identity.head_ref || identity.branch || '')",
            watchdog,
        )
        for marker in (
            "recovery_operation_id",
            "recovery_checkpoint_ref",
            "recovery_checkpoint_sha",
            "recovery_stage",
            "recovery_generation",
        ):
            self.assertIn(marker, watchdog)
        for caller in (SELF_CALLER, CONSUMER_CALLER):
            body = caller.read_text(encoding="utf-8")
            self.assertIn("inputs.recovery_identity", body)
            self.assertIn(
                "fromJSON(inputs.recovery_identity).recovery_checkpoint_ref",
                body,
            )


class RestoreAgainstRealGitTest(unittest.TestCase):
    """Runs the production restore script extracted from the workflow YAML."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow(ENGINE)
        cls.restore_script = step(
            job(cls.workflow, "opencode"), "Restore 429 recovery checkpoint"
        )["run"]

    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="429-bootstrap-", dir=str(TMP_PARENT))
        )
        self.origin = self.workdir / "origin.git"
        self.seed = self.workdir / "seed"
        subprocess.run(
            ["git", "init", "--bare", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.seed)],
            check=True,
            capture_output=True,
        )
        run_git(self.seed, "checkout", "-b", "main")
        (self.seed / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(self.seed, "add", "-A")
        run_git(self.seed, "commit", "-m", "initial")
        run_git(self.seed, "push", "-u", "origin", "main")
        self.base_sha = run_git(self.seed, "rev-parse", "HEAD").stdout.strip()
        self.checkpoint_count = 0

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def make_checkpoint(
        self,
        operation="opencode-429-issue-issue373",
        generation=1,
        stage="tests-passed",
        mode="issue",
        issue="373",
        metadata=True,
        checkpoint_ref=None,
    ):
        self.checkpoint_count += 1
        (self.seed / "app.py").write_text(
            "VALUE = {}\n".format(self.checkpoint_count + 1), encoding="utf-8"
        )
        run_git(self.seed, "add", "-A")
        run_git(self.seed, "commit", "-m", "agent work")
        head_sha = run_git(self.seed, "rev-parse", "HEAD").stdout.strip()
        operation = operation.lower()
        checkpoint_ref = checkpoint_ref or (
            "opencode/429-checkpoint-{}-gen-{}".format(operation, generation)
        )
        metadata_path = self.seed / ".continuum-429-recovery.json"
        if not metadata and metadata_path.exists():
            run_git(self.seed, "rm", ".continuum-429-recovery.json")
        if metadata:
            payload = {
                "schema_version": 1,
                "operation_id": operation,
                "failure_kind": "quota_429",
                "generation": generation,
                "checkpoint_ref": checkpoint_ref,
                "stage": stage,
                "mode": mode,
                "issue_number": issue,
                "pr_number": "",
                "head_ref": "opencode/issue{}-39863508457".format(issue),
                "base_ref": "main",
                "base_sha": self.base_sha,
                "head_sha": head_sha,
                "branch": "opencode/issue{}-{}".format(issue, 39863508457),
            }
            (self.seed / ".continuum-429-recovery.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_git(self.seed, "add", ".continuum-429-recovery.json")
        run_git(self.seed, "commit", "-m", "recovery metadata", check=False)
        checkpoint_sha = run_git(self.seed, "rev-parse", "HEAD").stdout.strip()
        run_git(
            self.seed,
            "push",
            "--force",
            "origin",
            "HEAD:refs/heads/" + checkpoint_ref,
        )
        return checkpoint_ref, checkpoint_sha

    def bootstrap_worktree_from_expression(self, dead_ref, base_ref="main"):
        """Resolve the production checkout expression and clone that ref."""
        expression = step(
            job(self.workflow, "opencode"), "Checkout repository"
        )["with"]["ref"]
        inner = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expression).group(1)
        completed = subprocess.run(
            ["node", "-e", NODE_EVALUATOR, inner],
            input=json.dumps(
                {
                    "inputs": {
                        "mode": "issue",
                        "recovery_checkpoint_ref": (
                            "opencode/429-checkpoint-opencode-429-"
                            "issue-issue373-gen-1"
                        ),
                        "base_ref": base_ref,
                        "head_ref": dead_ref,
                    },
                    "github": {
                        "ref": "refs/heads/main",
                        "event": {"repository": {"default_branch": "main"}},
                    },
                }
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        resolved = completed.stdout.strip()
        self.assertEqual(resolved, "main")

        # The dead branch is absent from origin, proving the bootstrap cannot
        # depend on it; the trusted ref and its commit exist.
        dead = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.origin), dead_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(dead.stdout.strip(), "")
        trusted = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", str(self.origin), resolved],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(trusted.returncode, 0, trusted.stderr)

        worktree = self.workdir / "fresh-vm"
        subprocess.run(
            ["git", "clone", "--branch", resolved, str(self.origin), str(worktree)],
            check=True,
            capture_output=True,
        )
        return worktree

    def run_restore(self, worktree, operation, ref, sha, stage, generation, mode):
        runner_temp = self.workdir / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        output_file = self.workdir / "github-output"
        env = dict(os.environ)
        env.update(GIT_ENV)
        env.update(
            {
                "RECOVERY_OPERATION_ID": operation,
                "RECOVERY_CHECKPOINT_REF": ref,
                "RECOVERY_CHECKPOINT_SHA": sha,
                "RECOVERY_STAGE": stage,
                "RECOVERY_GENERATION": str(generation),
                "RECOVERY_MODE": mode,
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(output_file),
            }
        )
        completed = subprocess.run(
            ["bash", "-c", self.restore_script],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return completed, output_file

    def test_missing_ephemeral_branch_reaches_checkpoint_verification(self):
        ref, checkpoint_sha = self.make_checkpoint()
        worktree = self.bootstrap_worktree_from_expression(
            "opencode/issue373-39863508457"
        )
        completed, output_file = self.run_restore(
            worktree,
            "opencode-429-issue-issue373",
            ref,
            checkpoint_sha,
            "tests-passed",
            1,
            "issue",
        )
        self.assertEqual(
            completed.returncode,
            0,
            (completed.stdout or "") + (completed.stderr or ""),
        )
        output = output_file.read_text(encoding="utf-8")
        self.assertIn("resumed=true", output)
        self.assertIn("checkpoint_sha=" + checkpoint_sha, output)
        self.assertIn("skip_opencode=true", output)
        self.assertEqual(
            run_git(worktree, "rev-parse", "HEAD").stdout.strip(),
            checkpoint_sha,
        )
        self.assertEqual(
            (worktree / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n"
        )

    def test_tampered_missing_or_mismatched_checkpoints_fail_closed(self):
        ref, checkpoint_sha = self.make_checkpoint()
        worktree = self.bootstrap_worktree_from_expression(
            "opencode/issue357-39852307581"
        )

        tampered, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue373",
            ref,
            "f" * 40,
            "tests-passed",
            1,
            "issue",
        )
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("does not match", tampered.stdout + tampered.stderr)

        wrong_operation, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue999",
            ref,
            checkpoint_sha,
            "tests-passed",
            1,
            "issue",
        )
        self.assertNotEqual(wrong_operation.returncode, 0)
        self.assertIn(
            "operation mismatch", wrong_operation.stdout + wrong_operation.stderr
        )

        wrong_generation, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue373",
            ref,
            checkpoint_sha,
            "tests-passed",
            2,
            "issue",
        )
        self.assertNotEqual(wrong_generation.returncode, 0)
        self.assertIn(
            "generation mismatch", wrong_generation.stdout + wrong_generation.stderr
        )

        wrong_stage, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue373",
            ref,
            checkpoint_sha,
            "publish-pr",
            1,
            "issue",
        )
        self.assertNotEqual(wrong_stage.returncode, 0)
        self.assertIn("stage mismatch", wrong_stage.stdout + wrong_stage.stderr)

        wrong_mode, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue373",
            ref,
            checkpoint_sha,
            "tests-passed",
            1,
            "ci-fix",
        )
        self.assertNotEqual(wrong_mode.returncode, 0)
        self.assertIn("mode mismatch", wrong_mode.stdout + wrong_mode.stderr)

        metadata_less_ref, metadata_less_sha = self.make_checkpoint(
            operation="opencode-429-issue-issue357",
            issue="357",
            metadata=False,
        )
        missing, _ = self.run_restore(
            worktree,
            "opencode-429-issue-issue357",
            metadata_less_ref,
            metadata_less_sha,
            "implementation-incomplete",
            1,
            "issue",
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(
            "no recovery metadata", missing.stdout + missing.stderr
        )


class PreRestoreFailureClassificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = load_workflow(ENGINE)
        cls.opencode = job(cls.workflow, "opencode")
        cls.body = ENGINE.read_text(encoding="utf-8")
        cls.watchdog = WATCHDOG.read_text(encoding="utf-8")

    def test_checkout_failure_is_marked_as_infrastructure(self):
        classifier = step(self.opencode, "Classify 429 recovery bootstrap failure")
        condition = classifier["if"]
        self.assertIn("inputs.recovery_checkpoint_ref != ''", condition)
        self.assertIn("steps.checkout.outcome != 'success'", condition)
        script = classifier["run"]
        self.assertIn("continuum-opencode-429-bootstrap", script)
        self.assertIn("CONTINUUM_OPENCODE_429_BOOTSTRAP_RETRY_REQUIRED", script)
        self.assertIn("no task attempt was consumed", script)
        # A retry may only be scheduled for a pinned SHA, never a mutable tip.
        self.assertIn("^[0-9a-fA-F]{40}$", script)

        recovery = step(
            self.opencode, "Detect OpenCode 429 runner lifecycle recovery"
        )["run"]
        self.assertIn("continuum-opencode-429-bootstrap", recovery)
        self.assertIn("restart_required=true", recovery)

        # restart_required=true keeps recover-scheduled-issue from ever
        # charging historical dispatch attempts for this infrastructure path.
        recover_job = job(self.workflow, "recover-scheduled-issue")
        self.assertIn(
            "needs.opencode.outputs.restart_required != 'true'",
            recover_job["if"],
        )

    def test_recover_cleanup_never_pauses_a_bootstrap_failure(self):
        recover_job = job(self.workflow, "recover-scheduled-issue")
        release = step(
            recover_job, "Release failed reservation or pause exhausted issue"
        )
        self.assertEqual(
            recover_job["env"]["RECOVERY_BOOTSTRAP_FAILED"],
            "${{ needs.opencode.outputs.recovery_bootstrap_failed }}",
        )
        script = release["with"]["script"]
        guard = script.split("RECOVERY_BOOTSTRAP_FAILED", 1)
        self.assertEqual(len(guard), 2)
        self.assertIn("no implementation attempt was consumed", guard[1])
        # The early return must happen before the historical-attempt pause.
        self.assertLess(
            script.index("RECOVERY_BOOTSTRAP_FAILED"),
            script.index("attempts >= maxAttempts && pauseOnFailure"),
        )
        self.assertLess(
            script.index("RECOVERY_BOOTSTRAP_FAILED"),
            script.index("issues.addLabels"),
        )

    def test_watchdog_retries_the_same_checkpoint_with_a_bounded_budget(self):
        for marker in (
            "CONTINUUM_OPENCODE_429_BOOTSTRAP_RETRY_REQUIRED",
            "parseBootstrapRetry",
            "bootstrap: true",
            "bootstrap-attempts-exhausted",
            "bounded pre-checkpoint retry",
        ):
            self.assertIn(marker, self.watchdog)
        recovery = self.watchdog.split(
            "async function recoverFrom429OnFreshVm"
        )[-1].split("if (publishRaceRestartRequired)")[0]
        self.assertIn("const bootstrapRetry", recovery)
        self.assertIn("sameFailedRun", recovery)
        self.assertIn("previous.detail === 'bootstrap-attempts-exhausted'", recovery)
        self.assertIn("(!backoffExpired || bootstrapRetry)", recovery)


if __name__ == "__main__":
    unittest.main()
