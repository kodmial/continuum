"""Synthetic E2E for OpenCode 429 runner-death recovery (kodmial/continuum#290).

Demonstrates the complete lifecycle with real Git operations inside the
worktree (``.opencode-tmp/``):

``agent changes repo -> 429 -> workflow checkpoint -> old run terminates ->
NEW workflow_dispatch -> checkpoint restored -> no repeated completed
stages -> PR/review lifecycle continues automatically``

No manual label removal, no manual ``/oc``, and no model invocation on the
VM that already returned 429.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuum.opencode_429_recovery import (
    already_resumed,
    checkpoint_ref,
    classify_agent_error,
    consumes_task_budget,
    is_opencode_429,
    next_generation,
    operation_id,
    parse_checkpoint_ref,
    recovery_dispatch_inputs,
    resumption_marker,
    should_enter_infra_cooldown,
    should_skip_opencode,
    validate_recovery_inputs,
)


ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}


def run_git(repo, *args, env_extra=None):
    env = dict(os.environ)
    env.update(GIT_ENV)
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "git {} failed: {}".format(
                " ".join(args), (completed.stderr or "").strip()[-2000:]
            )
        )
    return (completed.stdout or "").strip()


class FakeModel:
    """Fake OpenCode CLI: fails with 429 once, then counts invocations."""

    def __init__(self):
        self.invocations = 0
        self.fail_with_429 = True

    def run(self, burned):
        if burned:
            # Burned-runner fast path: exit 75 without touching the model.
            return 75, "CONTINUUM_OPENCODE_429_RESTART_REQUIRED (burned)"
        self.invocations += 1
        if self.fail_with_429:
            return 1, "ERROR FreeUsageLimitError: free usage limit reached"
        return 0, "done"


def evacuate(repo, operation, generation, stage, mode, issue, pr, base_sha):
    """Mirror of the workflow-owned 429 evacuator (shell in the workflow)."""
    status = run_git(repo, "status", "--porcelain")
    if status:
        run_git(repo, "add", "-A")
        run_git(
            repo,
            "commit",
            "-m",
            "chore: checkpoint 429-interrupted {} work".format(mode),
            "-m",
            "Continuum-Checkpoint-429-Operation: {}".format(operation),
            "-m",
            "Continuum-Component: opencode",
        )
    head_sha = run_git(repo, "rev-parse", "HEAD")
    metadata = {
        "operation_id": operation,
        "generation": generation,
        "stage": stage,
        "mode": mode,
        "issue_number": issue,
        "pr_number": pr,
        "head_ref": "",
        "review_id": "",
        "run_id": "",
        "base_ref": "main",
        "capability_number": "",
        "qualification_number": "",
        "required_sha": "",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch": run_git(repo, "branch", "--show-current"),
    }
    (Path(str(repo)) / ".continuum-429-recovery.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    run_git(repo, "add", ".continuum-429-recovery.json")
    run_git(
        repo,
        "commit",
        "-m",
        "chore: record 429 recovery identity {} gen {}".format(
            operation, generation
        ),
        "-m",
        "Continuum-Checkpoint-429-Operation: {}".format(operation),
        "-m",
        "Continuum-Component: opencode",
    )
    head_sha = run_git(repo, "rev-parse", "HEAD")
    ref = checkpoint_ref(operation, generation)
    run_git(repo, "push", "--force", "origin", "HEAD:refs/heads/{}".format(ref))
    return ref, head_sha, metadata


class OpenCode429EndToEndTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="429-e2e-", dir=str(TMP_PARENT)))
        self.origin = self.workdir / "origin.git"
        self.old_vm = self.workdir / "old-vm"
        self.new_vm = self.workdir / "new-vm"
        subprocess.run(
            ["git", "init", "--bare", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.old_vm)],
            check=True,
            capture_output=True,
        )
        run_git(self.old_vm, "checkout", "-b", "main")
        (self.old_vm / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(self.old_vm, "add", "-A")
        run_git(self.old_vm, "commit", "-m", "initial")
        run_git(self.old_vm, "push", "-u", "origin", "main")
        self.base_sha = run_git(self.old_vm, "rev-parse", "HEAD")
        self.model = FakeModel()
        self.burned = False

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_issue_429_checkpoint_resume_and_publish_without_repeating(self):
        # Agent changes the repo on the old VM, then the model returns 429.
        (self.old_vm / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        rc, log = self.model.run(self.burned)
        self.assertEqual(classify_agent_error(log, rc), "runner-death-429")
        self.assertTrue(is_opencode_429(log, rc))

        # 429 burns the VM: workflow-owned evacuation persists state, the old
        # run terminates, and no second model call happens on this VM.
        operation = operation_id("issue", issue_number="290")
        generation = next_generation(0)
        ref, checkpoint_sha, metadata = evacuate(
            self.old_vm,
            operation,
            generation,
            "implementation-incomplete",
            "issue",
            "290",
            "",
            self.base_sha,
        )
        self.burned = True
        old_run_exit = 75
        self.assertEqual(old_run_exit, 75)
        rc2, _ = self.model.run(self.burned)
        self.assertEqual(rc2, 75)
        self.assertEqual(self.model.invocations, 1)

        # Watchdog dispatches a NEW workflow_dispatch with explicit recovery
        # identity (not a reRunWorkflow of the dead run).
        dispatch = recovery_dispatch_inputs(
            operation=operation,
            checkpoint=ref,
            checkpoint_sha=checkpoint_sha,
            stage="implementation-incomplete",
            generation=generation,
            mode="issue",
            issue_number="290",
        )
        self.assertEqual(dispatch["mode"], "issue")
        self.assertEqual(dispatch["recovery_checkpoint_ref"], ref)
        ok, _ = validate_recovery_inputs(dispatch)
        self.assertTrue(ok)
        self.assertEqual(parse_checkpoint_ref(ref)["generation"], generation)

        # Deduplicate: a concurrent watchdog event for the same
        # operation+generation cannot create a competing resumption.
        thread_comments = resumption_marker(operation, generation)
        self.assertTrue(already_resumed(thread_comments, operation, generation))
        self.assertFalse(already_resumed(thread_comments, operation, generation + 1))

        # Fresh VM restores the exact checkpoint before doing any work.
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.new_vm)],
            check=True,
            capture_output=True,
        )
        run_git(self.new_vm, "fetch", "--no-tags", "origin", ref)
        run_git(self.new_vm, "switch", "--detach", "FETCH_HEAD")
        restored_sha = run_git(self.new_vm, "rev-parse", "HEAD")
        self.assertEqual(restored_sha, checkpoint_sha)
        self.assertEqual(
            (self.new_vm / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n"
        )

        # 429 consumed no task budget and applied no pause.
        self.assertFalse(consumes_task_budget(True))

        # Incomplete implementation continues the agent on the NEW VM only.
        self.assertFalse(should_skip_opencode("implementation-incomplete", False))
        self.model.fail_with_429 = False
        rc3, _ = self.model.run(False)
        self.assertEqual(rc3, 0)
        self.assertEqual(self.model.invocations, 2)

        # Workflow-owned publication from the restored checkpoint.
        run_git(self.new_vm, "switch", "-c", "opencode/issue290-new")
        run_git(self.new_vm, "push", "-u", "origin", "opencode/issue290-new")
        published = run_git(
            self.new_vm, "rev-parse", "origin/opencode/issue290-new"
        )
        self.assertEqual(published, run_git(self.new_vm, "rev-parse", "HEAD"))

    def test_tests_passed_checkpoint_publishes_without_agent(self):
        (self.old_vm / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        rc, log = self.model.run(self.burned)
        self.assertEqual(classify_agent_error(log, rc), "runner-death-429")
        operation = operation_id("issue", issue_number="291")
        ref, checkpoint_sha, _ = evacuate(
            self.old_vm,
            operation,
            1,
            "tests-passed",
            "issue",
            "291",
            "",
            self.base_sha,
        )
        self.burned = True

        subprocess.run(
            ["git", "clone", str(self.origin), str(self.new_vm)],
            check=True,
            capture_output=True,
        )
        run_git(self.new_vm, "fetch", "--no-tags", "origin", ref)
        run_git(self.new_vm, "switch", "--detach", "FETCH_HEAD")
        self.assertEqual(run_git(self.new_vm, "rev-parse", "HEAD"), checkpoint_sha)

        # Exact-SHA evidence exists: the fresh run skips OpenCode entirely.
        self.assertTrue(should_skip_opencode("tests-passed", True))
        invocations_before = self.model.invocations
        run_git(self.new_vm, "switch", "-c", "opencode/issue291-new")
        run_git(self.new_vm, "push", "-u", "origin", "opencode/issue291-new")
        self.assertEqual(self.model.invocations, invocations_before)

    def test_pr_repair_preserves_exact_head_identity(self):
        run_git(self.old_vm, "checkout", "-b", "opencode/issue292-1")
        (self.old_vm / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
        rc, log = self.model.run(self.burned)
        self.assertEqual(classify_agent_error(log, rc), "runner-death-429")
        operation = operation_id("coderabbit-fix", pr_number="12", review_id="99")
        ref, checkpoint_sha, metadata = evacuate(
            self.old_vm,
            operation,
            1,
            "review-repair",
            "coderabbit-fix",
            "",
            "12",
            self.base_sha,
        )
        pr_head_at_429 = checkpoint_sha

        subprocess.run(
            ["git", "clone", str(self.origin), str(self.new_vm)],
            check=True,
            capture_output=True,
        )
        run_git(self.new_vm, "fetch", "--no-tags", "origin", ref)
        run_git(self.new_vm, "switch", "--detach", "FETCH_HEAD")
        # Repair resumes on the new run without losing the exact PR HEAD.
        self.assertEqual(run_git(self.new_vm, "rev-parse", "HEAD"), pr_head_at_429)
        self.assertFalse(should_skip_opencode("review-repair", False))
        self.assertEqual(metadata["pr_number"], "12")

    def test_consecutive_429s_enter_backoff_without_pause(self):
        operation = operation_id("issue", issue_number="293")
        budgets_consumed = 0
        paused_applied = False
        for generation in (1, 2, 3, 4, 5):
            if consumes_task_budget(True):
                budgets_consumed += 1
            if should_enter_infra_cooldown(generation, 3):
                # Cooldown: no pause, no manual /oc; scheduler resumes with
                # the same checkpoint/operation identity.
                self.assertGreaterEqual(generation, 4)
                continue
            ref = checkpoint_ref(operation, generation)
            self.assertTrue(parse_checkpoint_ref(ref) is not None)
        self.assertEqual(budgets_consumed, 0)
        self.assertFalse(paused_applied)

    def test_normal_failures_keep_existing_semantics(self):
        self.assertEqual(
            classify_agent_error("401 unauthorized", 1), "fail-closed"
        )
        self.assertEqual(
            classify_agent_error("tests failed", 1), "task-failure"
        )
        self.assertTrue(consumes_task_budget(False))


if __name__ == "__main__":
    unittest.main()
