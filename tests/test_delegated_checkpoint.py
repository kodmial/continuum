"""Regression proof: delegated work must survive an unsuccessful runner cycle.

Uses local bare Git remotes only. No GitHub, private provider, tokens or model
calls are required. These tests cover one frozen generation across fresh clones.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / ".github/scripts/delegated_checkpoint.sh"
WORKER = ROOT / ".github/workflows/continuum-consumer-child-worker.yml"


class CheckpointIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin.git"
        self.spec = "a" * 64
        self.env = {**os.environ, "CHECKPOINT_HELPER": str(HELPER), "SPEC": self.spec}
        self.run_cmd(self.root, "git", "init", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        self.run_cmd(self.root, "git", "init", "-b", "main", str(seed))
        self.run_cmd(seed, "git", "config", "user.name", "continuum-test")
        self.run_cmd(seed, "git", "config", "user.email", "test@example.invalid")
        (seed / "README.md").write_text("baseline\n", encoding="utf-8")
        self.run_cmd(seed, "git", "add", ".")
        self.run_cmd(seed, "git", "commit", "-m", "baseline")
        self.run_cmd(seed, "git", "remote", "add", "origin", str(self.origin))
        self.run_cmd(seed, "git", "push", "-u", "origin", "main")

    def run_cmd(self, cwd, *args, check=True):
        return subprocess.run(
            args, cwd=cwd, env=self.env, capture_output=True, text=True,
            check=check, timeout=20,
        )

    def clone(self, name):
        work = self.root / name
        self.run_cmd(self.root, "git", "clone", str(self.origin), str(work))
        self.run_cmd(work, "git", "config", "user.name", "continuum-test")
        self.run_cmd(work, "git", "config", "user.email", "test@example.invalid")
        return work

    def shell(self, cwd, body, check=True):
        script = (
            'source "$CHECKPOINT_HELPER"\n'
            'continuum_assert_safe_push() { [[ "$1" == continuum-child/* && "$1" != main ]]; }\n'
            + body
        )
        return self.run_cmd(cwd, "bash", "-euo", "pipefail", "-c", script, check=check)

    def ref(self):
        return f"continuum-child/checkpoint-task-42-g1-{self.spec}"

    def test_restore_partial_work_on_second_runner_and_publish_final_branch(self):
        first = self.clone("worker1")
        self.shell(first, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-run1 "$SPEC"
            [[ "$CONTINUUM_CHECKPOINT_RESTORED" == false ]]
            printf 'unfinished work\\n' > progress.txt
            continuum_checkpoint_save "$ref" "$SPEC" child/example 42
        """)
        self.assertIn(
            self.ref(), self.run_cmd(first, "git", "ls-remote", "--heads", "origin").stdout
        )
        second = self.clone("worker2")
        self.shell(second, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-run2 "$SPEC"
            [[ "$CONTINUUM_CHECKPOINT_RESTORED" == true ]]
            [[ "$(cat progress.txt)" == "unfinished work" ]]
            printf 'more work\\n' >> progress.txt
            continuum_checkpoint_save "$ref" "$SPEC" child/example 42
            git push origin HEAD:refs/heads/continuum-child/task-42-run2
            continuum_checkpoint_cleanup "$ref" child/example
        """)
        self.assertNotIn(
            self.ref(), self.run_cmd(second, "git", "ls-remote", "--heads", "origin").stdout
        )
        check = self.clone("accepted")
        self.run_cmd(check, "git", "switch", "-c", "review", "origin/continuum-child/task-42-run2")
        self.assertEqual(
            (check / "progress.txt").read_text(encoding="utf-8"),
            "unfinished work\nmore work\n",
        )

    def test_two_runners_preserve_both_nonconflicting_deltas(self):
        # Both workers start before either has published a checkpoint.
        # The later push must fetch/rebase and preserve both contributions.
        first = self.clone("race-first")
        second = self.clone("race-second")
        self.shell(first, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-race1 "$SPEC"
            printf 'first\n' > first.txt
        """)
        self.shell(second, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-race2 "$SPEC"
            printf 'second\n' > second.txt
        """)
        for work in (first, second):
            self.shell(work, """
                ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
                continuum_checkpoint_save "$ref" "$SPEC" child/example 42
            """)
        check = self.clone("race-check")
        self.run_cmd(check, "git", "switch", "-c", "resume", f"origin/{self.ref()}")
        self.assertEqual((check / "first.txt").read_text(encoding="utf-8"), "first\n")
        self.assertEqual((check / "second.txt").read_text(encoding="utf-8"), "second\n")

    def test_invalid_snapshot_identity_never_produces_ref(self):
        for task, generation, spec in [(0, 1, self.spec), (42, 0, self.spec),
                                       (42, 1, "bad"), ("42/foo", 1, self.spec)]:
            with self.subTest(task=task, generation=generation, spec=spec[:5]):
                p = self.shell(self.clone(f"invalid-{task}-{generation}".replace("/", "_")),
                               f'continuum_checkpoint_ref "{task}" "{generation}" "{spec}"',
                               check=False)
                self.assertNotEqual(p.returncode, 0)

    def test_remote_lookup_outage_fails_closed_not_clean_restart(self):
        work = self.clone("outage")
        self.run_cmd(work, "git", "remote", "set-url", "origin", str(self.root / "absent.git"))
        result = self.shell(work, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-run3 "$SPEC"
        """, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(
            "continuum-child/task-42-run3", self.run_cmd(work, "git", "branch").stdout
        )

    def test_untrusted_checkpoint_without_matching_spec_rejected(self):
        work = self.clone("wrong")
        self.shell(work, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            git switch -c staged origin/main
            printf 'some data\\n' > progress.txt
            git add progress.txt
            git commit -m "not a Continuum checkpoint"
            git push origin "HEAD:refs/heads/$ref"
        """)
        second = self.clone("wrong-restore")
        p = self.shell(second, """
            ref="$(continuum_checkpoint_ref 42 1 "$SPEC")"
            continuum_checkpoint_restore "$ref" continuum-child/task-42-run4 "$SPEC"
        """, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn(
            "continuum-child/task-42-run4", self.run_cmd(second, "git", "branch").stdout
        )


class WorkflowCheckpointContractTest(unittest.TestCase):
    def test_worker_never_reports_partial_checkpoint_as_acceptance(self):
        body = WORKER.read_text(encoding="utf-8")
        self.assertIn("continuum_checkpoint_restore", body)
        self.assertIn("continuum_checkpoint_save", body)
        self.assertIn("continuum_checkpoint_cleanup", body)
        self.assertLess(body.index("continuum_checkpoint_restore"), body.index("opencode run --auto"))
        self.assertLess(body.index("continuum_checkpoint_save"), body.index("gh pr create"))
        self.assertLess(body.index("gh pr create"), body.index("continuum_checkpoint_cleanup"))
        self.assertIn("exit 30", body)
        self.assertIn("result=$result_state; checkpoint=$checkpoint_state", body)
        self.assertIn("CONTINUUM_CHECKPOINT_RESTORED", body)
        self.assertIn("continuum_assert_safe_push", HELPER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
