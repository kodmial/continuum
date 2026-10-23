"""Publish/checkpoint isolation for workflow-owned OpenCode files (#413).

The regression executes the checked-in production staging library against
real temporary Git repositories. It reproduces the PR #409 failure (an
embedded cancellation-engine checkout staged as an unconfigured Gitlink and
recovery metadata staged next to it), proves the publish tree is clean, and
verifies that legitimate declared submodules survive the boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
STAGING_LIBRARY = ROOT / ".github" / "scripts" / "continuum_pr_staging.sh"
OPENCODE_WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
PR_AGENT_REPAIR = ROOT / ".github" / "workflows" / "continuum-pr-agent-repair.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "github-actions[bot]",
    "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}


def run_git(repo: Path, *args: str, check: bool = True) -> str:
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
            "git {} failed: {}".format(" ".join(args), completed.stderr[-2000:])
        )
    return (completed.stdout or "").strip()


def init_repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test")


class PublishIsolationTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="publish-", dir=str(TMP_PARENT)))
        self.origin = self.workdir / "origin.git"
        self.repo = self.workdir / "work"
        self.nested = self.workdir / "nested"
        subprocess.run(
            ["git", "init", "--bare", "-q", str(self.origin)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.repo)],
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

        init_repository(self.nested)
        (self.nested / "helper.py").write_text("HELPER = True\n", encoding="utf-8")
        run_git(self.nested, "add", "-A")
        run_git(self.nested, "commit", "-m", "nested helper")
        self.nested_sha = run_git(self.nested, "rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def embed_cancel_engine(self) -> None:
        shutil.copytree(self.nested, self.repo / ".continuum-cancel-engine")

    def stage(self, function: str) -> subprocess.CompletedProcess:
        script = (
            'source "{}"; {}'.format(STAGING_LIBRARY, function)
        )
        env = dict(os.environ)
        env.update(GIT_ENV)
        return subprocess.run(
            ["bash", "-c", script],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def publish_and_checkout(self, branch: str) -> Path:
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "publish")
        run_git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/{}".format(branch))
        checkout = self.workdir / "checkout-{}".format(branch)
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(checkout)],
            check=True,
            capture_output=True,
        )
        run_git(checkout, "checkout", "-q", branch)
        return checkout

    def assert_no_workflow_artifacts(self, repo: Path) -> None:
        for name in (
            ".continuum-cancel-engine",
            ".continuum-429-recovery.json",
            ".continuum-429-stage",
        ):
            self.assertFalse((repo / name).exists(), "{} leaked".format(name))
        staged = run_git(repo, "ls-files", "--stage")
        self.assertNotIn(".continuum-cancel-engine", staged)
        self.assertNotIn(".continuum-429-recovery.json", staged)
        self.assertNotIn(".continuum-429-stage", staged)

    def test_missing_ignore_untracked_gitlink_is_excluded_from_real_checkout(self):
        self.assertFalse((self.repo / ".gitignore").exists())
        self.embed_cancel_engine()
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.repo / ".continuum-429-stage").write_text(
            "tests-passed\n", encoding="utf-8"
        )
        (self.repo / ".continuum-429-recovery.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )

        completed = self.stage("continuum_stage_publishable")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_no_workflow_artifacts(self.repo)

        checkout = self.publish_and_checkout("pr-missing-ignore")
        self.assert_no_workflow_artifacts(checkout)
        self.assertEqual(
            (checkout / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n"
        )

    def test_previously_tracked_gitlink_is_removed_even_when_ignored(self):
        (self.repo / ".gitignore").write_text(
            ".continuum-cancel-engine/\n", encoding="utf-8"
        )
        run_git(self.repo, "add", ".gitignore")
        run_git(self.repo, "commit", "-m", "ignore helper checkout")
        self.embed_cancel_engine()
        run_git(self.repo, "add", "-f", ".continuum-cancel-engine")
        run_git(self.repo, "commit", "-m", "legacy tracked gitlink")
        self.assertIn(
            "160000", run_git(self.repo, "ls-files", "--stage", ".continuum-cancel-engine")
        )

        (self.repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        (self.repo / ".continuum-429-stage").write_text(
            "publish-pr\n", encoding="utf-8"
        )
        completed = self.stage("continuum_stage_publishable")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_no_workflow_artifacts(self.repo)

        checkout = self.publish_and_checkout("pr-tracked-gitlink")
        self.assert_no_workflow_artifacts(checkout)
        self.assertEqual(
            (checkout / "app.py").read_text(encoding="utf-8"), "VALUE = 3\n"
        )

    def test_checkpoint_staging_keeps_recovery_metadata(self):
        self.embed_cancel_engine()
        (self.repo / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
        (self.repo / ".continuum-429-stage").write_text(
            "tests-passed\n", encoding="utf-8"
        )
        (self.repo / ".continuum-429-recovery.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )

        completed = self.stage("continuum_stage_checkpoint")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.repo / ".continuum-429-stage").is_file())
        self.assertTrue((self.repo / ".continuum-429-recovery.json").is_file())
        self.assertFalse((self.repo / ".continuum-cancel-engine").exists())
        staged = run_git(self.repo, "ls-files", "--stage")
        self.assertIn(".continuum-429-stage", staged)
        self.assertIn(".continuum-429-recovery.json", staged)
        self.assertNotIn(".continuum-cancel-engine", staged)

    def test_interrupted_429_checkpoint_resumes_to_a_clean_pull_request(self):
        self.embed_cancel_engine()
        (self.repo / "app.py").write_text("VALUE = 7\n", encoding="utf-8")
        (self.repo / ".continuum-429-stage").write_text(
            "tests-passed\n", encoding="utf-8"
        )
        (self.repo / ".continuum-429-recovery.json").write_text(
            '{"schema_version": 1, "stage": "tests-passed"}\n', encoding="utf-8"
        )

        checkpoint = self.stage("continuum_stage_checkpoint")
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)
        run_git(self.repo, "commit", "-m", "429 checkpoint")
        checkpoint_sha = run_git(self.repo, "rev-parse", "HEAD")
        run_git(
            self.repo,
            "push",
            "-q",
            "origin",
            "HEAD:refs/heads/opencode/429-checkpoint-test-gen-1",
        )

        # Fresh-VM restore: check out the exact checkpoint and drop the stale
        # progress file, exactly as the production recovery_restore step does.
        restored = self.workdir / "restored"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(restored)],
            check=True,
            capture_output=True,
        )
        run_git(
            restored,
            "fetch",
            "-q",
            "--no-tags",
            "origin",
            "opencode/429-checkpoint-test-gen-1",
        )
        self.assertEqual(run_git(restored, "rev-parse", "FETCH_HEAD"), checkpoint_sha)
        run_git(restored, "switch", "-q", "--detach", "FETCH_HEAD")
        run_git(restored, "config", "user.email", "test@example.com")
        run_git(restored, "config", "user.name", "Test")
        (restored / ".continuum-429-stage").unlink(missing_ok=True)
        self.assertTrue((restored / ".continuum-429-recovery.json").is_file())

        script = 'source "{}"; continuum_stage_publishable'.format(STAGING_LIBRARY)
        env = dict(os.environ)
        env.update(GIT_ENV)
        publish = subprocess.run(
            ["bash", "-c", script],
            cwd=str(restored),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(publish.returncode, 0, publish.stderr)
        run_git(restored, "commit", "-m", "publish resumed issue")
        run_git(
            restored,
            "push",
            "-q",
            "origin",
            "HEAD:refs/heads/opencode/issue-413-resumed",
        )

        checkout = self.workdir / "checkout-resumed"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(checkout)],
            check=True,
            capture_output=True,
        )
        run_git(checkout, "checkout", "-q", "opencode/issue-413-resumed")
        self.assert_no_workflow_artifacts(checkout)
        self.assertEqual(
            (checkout / "app.py").read_text(encoding="utf-8"), "VALUE = 7\n"
        )

    def test_declared_submodule_with_matching_gitmodules_is_preserved(self):
        (self.repo / ".gitmodules").write_text(
            '[submodule "vendor/lib"]\n'
            "\tpath = vendor/lib\n"
            "\turl = https://example.invalid/vendor/lib.git\n",
            encoding="utf-8",
        )
        (self.repo / "vendor").mkdir()
        shutil.copytree(self.nested, self.repo / "vendor" / "lib")
        subprocess.run(
            ["git", "-C", str(self.repo), "update-index", "--add", "--cacheinfo",
             "160000,{},vendor/lib".format(self.nested_sha)],
            check=True,
            capture_output=True,
        )
        (self.repo / "app.py").write_text("VALUE = 5\n", encoding="utf-8")

        completed = self.stage("continuum_stage_publishable")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        staged = run_git(self.repo, "ls-files", "--stage")
        self.assertIn(
            "160000 {} 0\tvendor/lib".format(self.nested_sha), staged
        )
        self.assertIn(".gitmodules", staged)

        checkout = self.publish_and_checkout("pr-valid-submodule")
        self.assertIn("vendor/lib", run_git(checkout, "ls-files", "--stage"))

    def test_unconfigured_gitlink_fails_closed_without_deleting_it(self):
        shutil.copytree(self.nested, self.repo / "mystery")
        run_git(self.repo, "add", "mystery")
        (self.repo / "app.py").write_text("VALUE = 6\n", encoding="utf-8")

        completed = self.stage("continuum_stage_publishable")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unexpected unconfigured Gitlink 'mystery'", completed.stderr)
        self.assertIn(
            "160000", run_git(self.repo, "ls-files", "--stage", "mystery")
        )
        self.assertTrue((self.repo / "mystery").exists())

    def test_removal_failure_refuses_to_leave_workflow_paths_staged(self):
        self.embed_cancel_engine()
        script = (
            'source "{}"; git add -A; : > .git/index.lock; '
            'continuum_remove_workflow_path .continuum-cancel-engine'
        ).format(STAGING_LIBRARY)
        env = dict(os.environ)
        env.update(GIT_ENV)
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("failed to remove workflow-owned path", completed.stderr)
        self.assertIn(
            ".continuum-cancel-engine",
            run_git(self.repo, "ls-files", "--stage"),
        )

    def test_concurrent_publish_staging_keeps_both_trees_clean(self):
        work_a = self.workdir / "work-a"
        work_b = self.workdir / "work-b"
        for index, work in enumerate((work_a, work_b), start=1):
            subprocess.run(
                ["git", "clone", "-q", str(self.origin), str(work)],
                check=True,
                capture_output=True,
            )
            run_git(work, "config", "user.email", "test@example.com")
            run_git(work, "config", "user.name", "Test")
            run_git(work, "checkout", "-q", "-b", "concurrent-{}".format(index))
            shutil.copytree(self.nested, work / ".continuum-cancel-engine")
            (work / "app.py").write_text(
                "VALUE = {}\n".format(10 + index), encoding="utf-8"
            )

        script = 'source "{}"; continuum_stage_publishable'.format(STAGING_LIBRARY)
        env = dict(os.environ)
        env.update(GIT_ENV)
        processes = [
            subprocess.Popen(
                ["bash", "-c", script],
                cwd=str(work),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for work in (work_a, work_b)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, stderr)

        for index, work in enumerate((work_a, work_b), start=1):
            run_git(work, "add", "-A")
            run_git(work, "commit", "-m", "concurrent publish")
            run_git(
                work,
                "push",
                "-q",
                "origin",
                "HEAD:refs/heads/concurrent-{}".format(index),
            )
            checkout = self.workdir / "checkout-concurrent-{}".format(index)
            subprocess.run(
                ["git", "clone", "-q", str(self.origin), str(checkout)],
                check=True,
                capture_output=True,
            )
            run_git(checkout, "checkout", "-q", "concurrent-{}".format(index))
            self.assert_no_workflow_artifacts(checkout)
            self.assertEqual(
                (checkout / "app.py").read_text(encoding="utf-8"),
                "VALUE = {}\n".format(10 + index),
            )


class WorkflowStagingContractTest(unittest.TestCase):
    def test_every_publication_path_uses_the_staging_boundary(self):
        workflow = OPENCODE_WORKFLOW.read_text(encoding="utf-8")
        for commit_message in (
            "chore: recover OpenCode issue changes",
            "implement issue #${ISSUE_NUMBER}",
            "fix: address CodeRabbit review findings",
            "fix: resolve merge conflict with main",
            "fix: repair blocking workflow",
        ):
            position = workflow.index(commit_message)
            preceding = workflow[:position]
            self.assertIn(
                "continuum_stage_publishable",
                preceding,
                "{} must be preceded by the publish boundary".format(commit_message),
            )
        self.assertIn("continuum_stage_checkpoint", workflow)
        self.assertNotIn("git add -A", workflow)
        self.assertIn(
            'cp "$engine/.github/scripts/continuum_pr_staging.sh"',
            workflow,
        )

        repair = PR_AGENT_REPAIR.read_text(encoding="utf-8")
        self.assertIn("continuum_stage_publishable", repair)
        self.assertNotIn("git add -A", repair)
        self.assertIn(
            'cp "$engine/.github/scripts/continuum_pr_staging.sh"',
            repair,
        )

    def test_snapshot_owner_stop_and_branch_safety_remain(self):
        workflow = OPENCODE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("continuum_assert_no_stop", workflow)
        self.assertIn("verify_snapshot_live", workflow)
        self.assertIn("continuum_assert_safe_push", workflow)
        self.assertLess(
            workflow.index("continuum_stage_publishable"),
            workflow.index("git push --force-with-lease"),
        )
        # The publish boundary removes the in-worktree cancellation checkout,
        # so the post-staging Owner STOP checks must use the trusted copy the
        # install step places outside the task worktree.
        self.assertIn(
            'echo "CONTINUUM_CANCEL_HELPER=$install_root/.github/scripts/opencode_cancel.py"',
            workflow,
        )
        self.assertIn(
            '${CONTINUUM_CANCEL_HELPER:-$GITHUB_WORKSPACE/.continuum-cancel-engine',
            workflow,
        )
        self.assertGreater(
            workflow.count('${CONTINUUM_CANCEL_HELPER:-'), 1
        )


if __name__ == "__main__":
    unittest.main()
