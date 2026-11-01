"""Behavioural tests for delegated repository and checkpoint shell helpers."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DELEGATION = os.path.join(ROOT, ".github", "scripts", "delegation_repository.sh")
CHECKPOINT = os.path.join(ROOT, ".github", "scripts", "delegated_checkpoint.sh")
SPEC = "a" * 64
BASH_MAJOR = int(
    subprocess.run(
        ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    or 0
)


def run_bash(script: str, env=None):
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=120,
    )


@unittest.skipUnless(BASH_MAJOR >= 4, "the delegation script requires bash 4+")
class ParentAllowlistTests(unittest.TestCase):
    """The parent lookup failure must never be reported as allow-by-default."""

    def _run(self, lookup_rc: int, child_ids: str):
        with open(DELEGATION, "r", encoding="utf-8") as handle:
            source = handle.read()
        # Everything before the CLI dispatch is definitions and defaults.
        definitions = source.split('\ncase "${1:-}" in', 1)[0]
        driver = definitions + """
parent_child_ids() {
  printf '%s\\n' "$CHILD_IDS"
  return "$LOOKUP_RC"
}
if assert_parent_allows_child "42"; then
  printf 'rc=0\\n'
else
  printf 'rc=%s\\n' "$?"
fi
"""
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(driver)
            path = handle.name
        try:
            return run_bash(
                'source "$DRIVER"',
                {
                    "DRIVER": path,
                    "CONTINUUM_ENGINE_ROOT": ROOT,
                    "GITHUB_REPOSITORY": "owner/parent",
                    "CHILD_IDS": child_ids,
                    "LOOKUP_RC": str(lookup_rc),
                },
            )
        finally:
            os.unlink(path)

    def test_unavailable_parent_lookup_propagates_exit_4(self):
        result = self._run(4, "")
        self.assertIn("rc=4", result.stdout)

    def test_invalid_parent_configuration_propagates_exit_2(self):
        result = self._run(2, "")
        self.assertIn("rc=2", result.stdout)

    def test_allowed_child_passes(self):
        result = self._run(0, "41\n42\n43")
        self.assertIn("rc=0", result.stdout)

    def test_child_outside_allowlist_fails_closed(self):
        result = self._run(0, "41\n43")
        self.assertIn("rc=2", result.stdout)
        self.assertIn("not allowed", result.stderr)


class CheckpointHistoryTests(unittest.TestCase):
    """A checkpoint must share history with the current default branch."""

    DEFAULT_BRANCH = "main"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.origin = os.path.join(base, "origin.git")
        self.seed = os.path.join(base, "seed")
        self.work = os.path.join(base, "work")
        self._git(base, "init", "--bare", "-q", "-b", self.DEFAULT_BRANCH, self.origin)
        self._git(base, "clone", "-q", self.origin, self.seed)
        self._config(self.seed)
        self._commit(self.seed, "M0")
        self._git(self.seed, "push", "-q", "origin", self.DEFAULT_BRANCH)

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, cwd, *args):
        subprocess.run(
            ["git", *args], cwd=cwd, check=True,
            capture_output=True, text=True, timeout=60,
        )

    def _config(self, cwd):
        self._git(cwd, "config", "user.email", "test@example.com")
        self._git(cwd, "config", "user.name", "Test")
        self._git(cwd, "config", "commit.gpgsign", "false")

    def _commit(self, cwd, subject, message=None):
        self._git(cwd, "commit", "-q", "--allow-empty", "-m", message or subject)

    def _checkpoint(self, ref, spec=SPEC, orphan=False):
        if orphan:
            self._git(self.seed, "checkout", "-q", "--orphan", ref)
        else:
            self._git(self.seed, "checkout", "-q", "-B", ref, self.DEFAULT_BRANCH)
        self._commit(self.seed, "checkpoint", "Continuum-Checkpoint: " + spec)
        self._git(self.seed, "push", "-q", "origin", ref)
        self._git(self.seed, "checkout", "-q", self.DEFAULT_BRANCH)

    def _advance_main(self):
        self._commit(self.seed, "M1")
        self._git(self.seed, "push", "-q", "origin", self.DEFAULT_BRANCH)

    def test_checkpoint_behind_advanced_default_branch_restores(self):
        ref = "continuum-child/checkpoint-task-1-g1-" + SPEC
        self._checkpoint(ref)
        self._advance_main()
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-1" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=0", result.stdout, result.stderr)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.work, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(branch, "continuum-child/task-1")

    def test_shallow_checkout_restores_the_pinned_checkpoint(self):
        # Deepening a shallow clone fetches again and overwrites FETCH_HEAD.
        # Restore must switch to the checkpoint SHA captured before that.
        ref = "continuum-child/checkpoint-task-3-g1-" + SPEC
        self._checkpoint(ref)
        checkpoint_sha = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=self.seed, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self._advance_main()
        self._git(
            self.tmp.name,
            "clone", "-q", "--depth=1", "file://" + self.origin, self.work,
        )
        self._config(self.work)
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-3" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=0", result.stdout, result.stderr)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.work, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(head, checkpoint_sha)

    def test_unrelated_checkpoint_history_fails_closed(self):
        ref = "continuum-child/checkpoint-task-2-g1-" + SPEC
        self._checkpoint(ref, orphan=True)
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-2" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=4", result.stdout, result.stderr)

    def test_fresh_start_uses_the_discovered_default_branch(self):
        # A child whose default branch is not main has no origin/main at all,
        # so a hardcoded ref would fail the fresh-start restore.
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        ref = "continuum-child/checkpoint-task-6-g1-" + SPEC
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-6" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=0", result.stdout, result.stderr)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.work, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        default_head = subprocess.run(
            ["git", "rev-parse", self.DEFAULT_BRANCH],
            cwd=self.seed, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(head, default_head)
        if self.DEFAULT_BRANCH != "main":
            main_ref = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 "refs/remotes/origin/main"],
                cwd=self.work, capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            self.assertEqual(main_ref, "")

    def test_stale_origin_head_with_missing_tracking_ref_is_refreshed(self):
        # A clone can keep a stale origin/HEAD and lose the resolved
        # remote-tracking ref. Resume must refresh origin/HEAD, then fetch
        # the resolved default branch before switching to it.
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        self._git(self.work, "branch", "stale")
        self._git(
            self.work, "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/stale",
        )
        self._git(
            self.work, "update-ref", "-d",
            "refs/remotes/origin/" + self.DEFAULT_BRANCH,
        )
        ref = "continuum-child/checkpoint-task-8-g1-" + SPEC
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-8" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=0", result.stdout, result.stderr)
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.work, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(branch, "continuum-child/task-8")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.work, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        default_head = subprocess.run(
            ["git", "rev-parse", self.DEFAULT_BRANCH],
            cwd=self.seed, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        self.assertEqual(head, default_head)

    def test_undiscoverable_default_branch_fails_closed(self):
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        self._git(self.work, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self._git(
            self.work,
            "remote", "set-url", "origin",
            os.path.join(self.tmp.name, "missing.git"),
        )
        ref = "continuum-child/checkpoint-task-7-g1-" + SPEC
        driver = (
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_restore "$REF" "continuum-child/task-7" "$SPEC"; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        result = subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": CHECKPOINT, "REF": ref, "SPEC": SPEC},
            timeout=120,
        )
        self.assertIn("rc=4", result.stdout, result.stderr)
        self.assertIn("default branch", result.stderr)

    def _dirty_clone(self):
        self._git(self.tmp.name, "clone", "-q", self.origin, self.work)
        self._config(self.work)
        with open(os.path.join(self.work, "delta.txt"), "w", encoding="utf-8") as handle:
            handle.write("delegated delta\n")

    def _save(self, driver, checkpoint=CHECKPOINT):
        return subprocess.run(
            ["bash", "-c", driver],
            cwd=self.work,
            capture_output=True,
            text=True,
            env={**os.environ, "CHECKPOINT": checkpoint, "REF": self.ref,
                 "SPEC": SPEC},
            timeout=120,
        )

    def test_save_without_the_staging_boundary_fails_closed(self):
        # A plain `git add -A` fallback could publish a workflow-owned engine
        # checkout, so the shared boundary is required rather than optional.
        self._dirty_clone()
        self.ref = "continuum-child/checkpoint-task-4-g1-" + SPEC
        isolated = os.path.join(self.tmp.name, "isolated-checkpoint.sh")
        with open(CHECKPOINT, "r", encoding="utf-8") as source:
            with open(isolated, "w", encoding="utf-8") as target:
                target.write(source.read())
        result = self._save(
            'source "$CHECKPOINT"\n'
            'if continuum_checkpoint_save "$REF" "$SPEC" kodmial/continuum 4; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n',
            checkpoint=isolated,
        )
        self.assertIn("rc=4", result.stdout, result.stderr)
        self.assertIn("staging boundary is unavailable", result.stderr)
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=self.work,
            capture_output=True, text=True, timeout=60,
        ).stdout
        self.assertEqual(len(log.strip().splitlines()), 1, log)

    def test_sourcing_by_bare_file_name_loads_the_staging_boundary(self):
        # `${BASH_SOURCE[0]%/*}` keeps the whole name when the script is
        # sourced without a directory, which used to look for
        # "delegated_checkpoint.sh/continuum_pr_staging.sh".
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source delegated_checkpoint.sh\n'
                'if declare -F continuum_stage_checkpoint >/dev/null; then\n'
                '  printf "loaded\\n"\n'
                'else\n'
                '  printf "missing\\n"\n'
                'fi\n',
            ],
            cwd=os.path.dirname(CHECKPOINT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn("loaded", result.stdout, result.stderr)

    def test_save_loads_and_uses_the_shared_staging_boundary(self):
        self._dirty_clone()
        engine_checkout = os.path.join(self.work, ".continuum-cancel-engine")
        os.makedirs(engine_checkout)
        with open(os.path.join(engine_checkout, "engine.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("workflow-owned checkout\n")
        self.ref = "continuum-child/checkpoint-task-5-g1-" + SPEC
        result = self._save(
            'source "$CHECKPOINT"\n'
            'continuum_assert_safe_push() { return 0; }\n'
            'if continuum_checkpoint_save "$REF" "$SPEC" kodmial/continuum 5; then\n'
            '  printf "rc=0\\n"\n'
            'else\n'
            '  printf "rc=%s\\n" "$?"\n'
            'fi\n'
        )
        self.assertIn("rc=0", result.stdout, result.stderr)
        tracked = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"], cwd=self.work,
            capture_output=True, text=True, timeout=60,
        ).stdout
        self.assertIn("delta.txt", tracked)
        self.assertNotIn(".continuum-cancel-engine", tracked)


class CheckpointNonMainDefaultBranchTests(CheckpointHistoryTests):
    """Every checkpoint flow against a child whose default branch is ``trunk``."""

    DEFAULT_BRANCH = "trunk"


if __name__ == "__main__":
    unittest.main()
