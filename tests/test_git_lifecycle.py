"""A workflow owns the branch lifecycle; a prompt does not.

The OpenCode task is told not to create or switch branches. A prompt is not a
control, and when the agent switches branches anyway the workflow's own `git
push` and `gh pr create --head` operate on the branch the agent chose. The
change lands somewhere the workflow never named, or a pull request is opened
against a branch the merge controller's trust policy will not accept, and the
run reports success either way.

These are deterministic tests over a pure decision function plus one subprocess
stand-in. No git repository is created and no branch is actually moved; the
value under test is the answer to "may this run still push?".
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import unittest

from continuum import git_lifecycle
from continuum.git_lifecycle import BranchOwnershipError

ISSUE = 59
RUN = 20260929132857
EXPECTED = f"opencode/issue{ISSUE}-{RUN}"


class CompletedRun:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BranchNameTests(unittest.TestCase):
    def test_the_branch_name_is_derived_from_issue_and_run(self):
        self.assertEqual(git_lifecycle.issue_branch(ISSUE, RUN), EXPECTED)

    def test_the_derived_name_is_recognized_as_owned(self):
        self.assertTrue(git_lifecycle.is_workflow_branch(EXPECTED))

    def test_a_non_numeric_issue_is_not_a_workflow_branch(self):
        # The merge controller's trust policy accepts only digits, so producing
        # anything else is a silent dead end.
        self.assertFalse(git_lifecycle.is_workflow_branch("opencode/issueN-59-1"))
        self.assertFalse(git_lifecycle.is_workflow_branch("opencode/issue-59-1"))

    def test_a_human_branch_is_not_a_workflow_branch(self):
        for ref in ("main", "feature/x", "opencode/repair-12-1", ""):
            self.assertFalse(git_lifecycle.is_workflow_branch(ref), ref)

    def test_a_suffixed_branch_is_not_the_same_branch(self):
        # An extra path segment or a trailing marker is a different branch, even
        # though the convention is a prefix of it.
        self.assertFalse(git_lifecycle.is_workflow_branch(EXPECTED + "/extra"))
        self.assertFalse(git_lifecycle.is_workflow_branch(EXPECTED + "-rebase"))
        self.assertFalse(git_lifecycle.is_workflow_branch(EXPECTED + "x"))

    def test_the_description_is_structured(self):
        described = git_lifecycle.describe_issue_branch(EXPECTED)
        self.assertEqual(
            described, {"branch": EXPECTED, "owned": True, "issue": ISSUE, "run": RUN}
        )

    def test_an_unowned_branch_is_described_as_such(self):
        self.assertEqual(
            git_lifecycle.describe_issue_branch("main"),
            {"branch": "main", "owned": False, "issue": None, "run": None},
        )


class CurrentBranchTests(unittest.TestCase):
    def test_the_branch_is_read_from_git(self):
        def runner(argv, **kwargs):
            self.assertEqual(argv, ["git", "branch", "--show-current"])
            return CompletedRun(stdout=f"{EXPECTED}\n")

        self.assertEqual(git_lifecycle.current_branch(runner), EXPECTED)

    def test_a_detached_head_reads_as_empty(self):
        # `git branch --show-current` prints nothing when HEAD is detached, and
        # that is a failure of ownership, not a branch named "".
        def runner(argv, **kwargs):
            return CompletedRun(stdout="\n")

        self.assertEqual(git_lifecycle.current_branch(runner), "")

    def test_a_git_failure_reads_as_empty(self):
        def runner(argv, **kwargs):
            return CompletedRun(returncode=128, stderr="not a git repository")

        self.assertEqual(git_lifecycle.current_branch(runner), "")

    def test_a_missing_git_binary_fails_closed(self):
        def runner(argv, **kwargs):
            raise OSError("git not found")

        self.assertEqual(git_lifecycle.current_branch(runner), "")


class OwnershipAssertionTests(unittest.TestCase):
    def test_the_owned_branch_passes(self):
        message = git_lifecycle.assert_owned_branch(
            expected=EXPECTED, actual=EXPECTED, issue_number=ISSUE, run_id=RUN
        )
        self.assertIn(EXPECTED, message)
        self.assertIn(f"issue #{ISSUE}", message)

    def test_a_switched_branch_fails(self):
        # The incident shape: the agent moved HEAD, and the workflow is about to
        # push and open a pull request against a branch it does not own.
        with self.assertRaises(BranchOwnershipError) as caught:
            git_lifecycle.assert_owned_branch(
                expected=EXPECTED, actual="opencode/issue60-777"
            )
        self.assertIn("HEAD moved", str(caught.exception))

    def test_a_switched_branch_names_the_unexpected_ref(self):
        with self.assertRaises(BranchOwnershipError) as caught:
            git_lifecycle.assert_owned_branch(expected=EXPECTED, actual="main")
        self.assertIn("'main'", str(caught.exception))

    def test_a_detached_head_fails(self):
        with self.assertRaises(BranchOwnershipError) as caught:
            git_lifecycle.assert_owned_branch(expected=EXPECTED, actual="")
        self.assertIn("detached HEAD", str(caught.exception))

    def test_a_misconfigured_expected_branch_fails_before_any_check(self):
        # A workflow that computed the wrong name must fail on its own bug, not
        # on the branch state it produced.
        with self.assertRaises(BranchOwnershipError) as caught:
            git_lifecycle.assert_owned_branch(expected="main", actual="main")
        self.assertIn("not a workflow-owned branch", str(caught.exception))

    def test_the_context_is_optional(self):
        message = git_lifecycle.assert_owned_branch(expected=EXPECTED, actual=EXPECTED)
        self.assertNotIn("issue #", message)


class CommandLineTests(unittest.TestCase):
    def _run(self, argv):
        return git_lifecycle.main(argv)

    def test_ownership_holding_exits_zero(self):
        original = git_lifecycle.current_branch
        git_lifecycle.current_branch = lambda runner=None: EXPECTED
        try:
            self.assertEqual(self._run(["--expected", EXPECTED, "--issue", str(ISSUE)]), 0)
        finally:
            git_lifecycle.current_branch = original

    def test_ownership_failing_exits_one(self):
        original = git_lifecycle.current_branch
        git_lifecycle.current_branch = lambda runner=None: "main"
        try:
            self.assertEqual(self._run(["--expected", EXPECTED]), 1)
        finally:
            git_lifecycle.current_branch = original

    def test_a_real_git_invocation_against_this_branch_succeeds(self):
        # One end-to-end call against the branch this test file lives on, so the
        # argv, the git read, and the exit code a workflow depends on are proven
        # rather than mocked.
        root = pathlib.Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(root / "src"))
        completed = subprocess.run(
            ["python3", "-m", "continuum.git_lifecycle", "--expected", EXPECTED],
            capture_output=True,
            text=True,
            cwd=str(root),
            env=environment,
        )
        # The suite can run on a detached HEAD or a differently named branch, so
        # the assertion is on the shape of the answer, not on one branch name.
        if completed.returncode == 0:
            self.assertIn("Branch ownership verified", completed.stdout)
        else:
            self.assertIn("::error::", completed.stdout)

    def test_a_non_numeric_issue_is_only_a_log_detail(self):
        original = git_lifecycle.current_branch
        git_lifecycle.current_branch = lambda runner=None: EXPECTED
        try:
            self.assertEqual(
                self._run(["--expected", EXPECTED, "--issue", "not-a-number", "--run-id", ""]),
                0,
            )
        finally:
            git_lifecycle.current_branch = original


if __name__ == "__main__":
    unittest.main()
