"""The workflow owns the branch lifecycle; a task must not.

A task agent is told not to create or switch branches, and a prompt is not a
control. When the agent does switch branches anyway, the workflow's subsequent
`git push` and `gh pr create --head` operate on the branch the agent chose: the
change lands somewhere the workflow did not name, or the pull request is opened
against a branch the merge controller does not recognize as an agent branch.
Either way the workflow silently stops owning its own output.

This module is the enforcement. It is a pure value function so the assertion is
a decision that can be tested without a git repository, and a shell-runnable
command so a workflow can enforce it at the only points where a push or a pull
request is about to happen.

The invariant is deliberately narrow: the checked-out branch must be the one the
workflow created for this run, and nothing else. It does not try to repair a
switched branch, because pushing the change somewhere unexpected is worse than
failing: a visible failure is recoverable and a misplaced commit is not.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Dict, List, Optional

# The agent branch convention. The merge controller's trust policy accepts only
# this shape, so a branch outside it can never merge: producing one is a silent
# dead end, and detecting it here is the point.
BRANCH_PREFIX = "opencode/issue"
BRANCH_RE = re.compile(r"^opencode/issue(?P<issue>[0-9]+)-(?P<run>[0-9]+)$")

FALLBACK_BRANCH = "main"


class BranchOwnershipError(RuntimeError):
    """Raised when the checked-out branch is not the workflow-owned branch."""


def issue_branch(issue_number: int, run_id: int) -> str:
    """The single branch name this workflow is allowed to own for a run."""

    return f"{BRANCH_PREFIX}{int(issue_number)}-{int(run_id)}"


def is_workflow_branch(ref: str) -> bool:
    """True when `ref` is a branch the workflow may own."""

    return bool(BRANCH_RE.match((ref or "").strip()))


def current_branch(runner: Optional[object] = None) -> str:
    """The checked-out branch name, or "" when it cannot be read."""

    call = runner or subprocess.run
    try:
        completed = call(  # noqa: S603 - fixed argv, no shell
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if getattr(completed, "returncode", 1) != 0:
        return ""
    return (getattr(completed, "stdout", "") or "").strip()


def assert_owned_branch(
    *,
    expected: str,
    actual: str,
    issue_number: Optional[int] = None,
    run_id: Optional[int] = None,
) -> str:
    """Return `expected` when HEAD is the workflow-owned branch, else raise.

    Both halves of the check matter. `actual` must equal `expected`, and
    `expected` must itself be a branch shape the merge controller will accept,
    so a misconfigured workflow fails here instead of producing an unmergeable
    pull request.
    """

    if not is_workflow_branch(expected):
        raise BranchOwnershipError(
            f"Refusing to proceed: {expected!r} is not a workflow-owned branch "
            f"(expected {BRANCH_PREFIX}<issue>-<run>)."
        )
    if actual != expected:
        raise BranchOwnershipError(
            f"HEAD moved to {actual or 'a detached HEAD'!r}, but this workflow owns "
            f"{expected!r}. The change was not pushed and no pull request was created."
        )
    context = []
    if issue_number is not None:
        context.append(f"issue #{int(issue_number)}")
    if run_id is not None:
        context.append(f"run {int(run_id)}")
    suffix = f" ({', '.join(context)})" if context else ""
    return f"Branch ownership verified: HEAD is {expected}{suffix}."


def describe_issue_branch(ref: str) -> Dict[str, object]:
    """Structured description of a branch, for logs and machine assertions."""

    match = BRANCH_RE.match((ref or "").strip())
    if not match:
        return {"branch": ref or "", "owned": False, "issue": None, "run": None}
    return {
        "branch": match.group(0),
        "owned": True,
        "issue": int(match.group("issue")),
        "run": int(match.group("run")),
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Assert that HEAD is the branch this run owns.

    Exit codes are the contract a workflow depends on: 0 when ownership holds,
    1 when it does not, with the reason on stderr as a GitHub annotation.
    """

    parser = argparse.ArgumentParser(
        prog="continuum-git-lifecycle",
        description=(
            "Fail closed unless the checked-out branch is the workflow-owned branch "
            "for this run."
        ),
    )
    parser.add_argument("--expected", required=True, help="The branch this run created.")
    parser.add_argument("--issue", default="", help="Issue number, for the log line only.")
    parser.add_argument("--run-id", default="", help="Run id, for the log line only.")
    args = parser.parse_args(argv)

    issue = int(args.issue) if str(args.issue).strip().isdigit() else None
    run_id = int(args.run_id) if str(args.run_id).strip().isdigit() else None
    try:
        message = assert_owned_branch(
            expected=str(args.expected),
            actual=current_branch(),
            issue_number=issue,
            run_id=run_id,
        )
    except BranchOwnershipError as exc:
        print(f"::error::{exc}")
        print(json.dumps({"owned": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(f"::notice::{message}")
    print(json.dumps({"owned": True, **describe_issue_branch(str(args.expected))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
