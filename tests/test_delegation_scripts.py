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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.origin = os.path.join(base, "origin.git")
        self.seed = os.path.join(base, "seed")
        self.work = os.path.join(base, "work")
        self._git(base, "init", "--bare", "-q", "-b", "main", self.origin)
        self._git(base, "clone", "-q", self.origin, self.seed)
        self._config(self.seed)
        self._commit(self.seed, "M0")
        self._git(self.seed, "push", "-q", "origin", "main")

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
            self._git(self.seed, "checkout", "-q", "-B", ref, "main")
        self._commit(self.seed, "checkpoint", "Continuum-Checkpoint: " + spec)
        self._git(self.seed, "push", "-q", "origin", ref)
        self._git(self.seed, "checkout", "-q", "main")

    def _advance_main(self):
        self._commit(self.seed, "M1")
        self._git(self.seed, "push", "-q", "origin", "main")

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


if __name__ == "__main__":
    unittest.main()
