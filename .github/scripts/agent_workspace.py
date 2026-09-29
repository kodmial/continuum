#!/usr/bin/env python3
"""Agent workspace materialization: the workflow publishes, the model edits.

Incidents this module exists for
--------------------------------
Two production incidents are the same incident, seen from two repositories.

**kodmai #35** -- *"Recovered automatically after the OpenCode agent changed
branches during issue #23 execution."* That sentence is the recovery step's own
pull request body, written by the step that fired after the agent moved. The
step worked, and it worked by accident: it read ``git branch --show-current``,
saw an ``opencode/*`` branch, committed, pushed, and opened a pull request.

The recovery step had three ways to lose the finished work, and each one was
silent:

1. If the agent left a branch that was not ``opencode/*`` -- or detached HEAD,
   where ``git branch --show-current`` prints nothing -- the step printed
   "nothing to recover" and exited ``0``. The runner then discarded a working
   tree that held a completed implementation. The comment in that step claimed
   it preserved edits the agent left behind; the code did the opposite.
2. It pushed to whatever branch the agent happened to be standing on. So the
   workflow published to a ref it did not create, which is a ref the trust
   policy does not recognise as an agent branch and no later reconciliation
   will consider.
3. It asked ``gh pr list --state all``, so a closed or already-merged pull
   request for the branch counted as "already published" and suppressed
   recovery permanently.

**runtime-lab #23** -- *materialize temporary runner results into branch/PR.*
Same shape from the other side: the agent's output lives only in a runner
workspace that is destroyed when the job ends. If publication is not a
transaction that the workflow owns, a crash between "the model finished" and
"the branch was pushed" destroys the work with no trace.

The fix is a decision, not a retry
----------------------------------
:func:`evaluate_workspace` takes what the workspace looks like *after* the agent
returned and returns one of four actions. The important property is that three
of them publish or report, and none of them discards:

``publish``            The agent stayed on the workflow's branch. Commit what is
                       there and push. Commits are preserved verbatim.
``materialize``        The agent drifted -- a different branch, or detached HEAD.
                       Rebuild the *tree* it produced as one commit on the
                       workflow's own branch, then push. This cannot conflict,
                       because it does not merge: it takes the finished tree
                       verbatim, which is what the work actually is.
``already-published``  An open pull request already owns the branch. Do nothing.
``refuse``             The branch identity is unusable, so there is no ref this
                       workflow is willing to push to. Report loudly; do not
                       guess.

Why a tree and not a merge
--------------------------
Bringing drifted work back with ``git merge`` re-introduces the one thing
recovery must not have: a conflict, at the end of a long run, with no agent left
to resolve it. Materializing the tree cannot conflict by construction, and it
cannot drop bytes either -- the commit it writes has the salvaged tree
verbatim, and the salvaged commit is reported so the original history stays
reachable. The cost is that the recovery commit is one squashed commit rather
than the agent's original series. That is the right trade: a reviewable commit
whose content is exact beats an unmergeable history whose content is exact.

Why the branch is an input, not a lookup
----------------------------------------
The workflow creates the branch. That makes the branch an *identity* the
workflow already knows, and identity is what a recovery must publish to.
``owned_branch`` is therefore a required input. :func:`select_owned_branch` is
the one place that infers it, and it refuses to guess when the inference is
ambiguous.

Everything here is pure: no network, no filesystem, no clock. The CLI is the
only place that touches the environment, and the workflow consumes its output
rather than re-deriving the decision in shell.

CLI
---
    workspace-plan   Print the materialization decision for a workspace.
    select-branch    Print the workflow-owned branch for an issue.

Environment:
    GITHUB_OUTPUT, CONTINUUM_BASE_BRANCH.

Standard library only, so it runs on stock ``ubuntu-latest`` runners without
dependency installation.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Policy constants
# --------------------------------------------------------------------------- #

DEFAULT_BASE_BRANCH = "main"

#: The closed set of things the workflow may do with an agent workspace. There
#: is deliberately no "discard" and no "skip": a workspace that holds finished
#: work is either published or reported, and the only way to publish nothing is
#: to prove there is nothing to publish.
WORKSPACE_ACTIONS = ("publish", "materialize", "already-published", "refuse")

#: Prefix for the quarantine branch a ``refuse`` pushes to. It is inside the
#: agent branch shape on purpose: a preserved workspace the trust policy will
#: not recognize is a workspace no controller will ever reconcile.
SALVAGE_BRANCH_RE = re.compile(r"^opencode/issue(?P<number>[1-9][0-9]{0,9})-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

#: A full commit id, or nothing.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- #
# Decision type
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class WorkspaceDecision:
    """What the workflow must do with the workspace the agent left behind.

    ``action`` is the whole decision. The remaining fields are the values the
    caller needs to *carry out* that action, so the shell never has to re-derive
    a judgement the policy already made.

    ``salvaged_head`` is recorded even on success: the commit the agent left
    behind stays reachable through the pull request body, so a squashed
    recovery is auditable rather than silent.
    """

    action: str
    code: str
    reason: str
    #: The ref the workflow will publish to. Always the branch the workflow
    #: owns, never a branch the agent happened to leave behind.
    publish_branch: str = ""
    base_branch: str = ""
    #: The commit whose tree is being materialized, when ``action`` is
    #: ``materialize``. Empty otherwise.
    salvaged_head: str = ""
    #: Whether the caller must record a visible failure on the issue.
    report_failure: bool = False
    #: Whether the run is a success. ``refuse`` is the only false.
    recoverable: bool = True
    #: The ref whose tree a salvage must preserve. Empty only when there is no
    #: work anywhere to preserve.
    salvage_source: str = ""

    def as_outputs(self) -> dict:
        return {
            "workspace_action": self.action,
            "workspace_code": self.code,
            "workspace_reason": " ".join((self.reason or "").split()),
            "workspace_publish_branch": self.publish_branch,
            "workspace_base_branch": self.base_branch,
            "workspace_salvaged_head": self.salvaged_head,
            "workspace_salvage_source": self.salvage_source,
            "workspace_report_failure": "true" if self.report_failure else "false",
            "workspace_recoverable": "true" if self.recoverable else "false",
        }


# --------------------------------------------------------------------------- #
# Branch identity
# --------------------------------------------------------------------------- #


def is_agent_branch(ref) -> bool:
    """Whether ``ref`` is the only branch shape the agent plane recognizes."""
    if not isinstance(ref, str):
        return False
    ref = ref.strip()
    if not ref or len(ref) > 120 or ".." in ref or "//" in ref:
        return False
    return bool(SALVAGE_BRANCH_RE.match(ref))


def owned_branch_name(issue_number) -> str:
    """The branch the workflow owns for ``issue_number``.

    One branch per issue, not per run: a re-dispatch of the same issue has to
    find the previous run's pull request and leave it alone, which it cannot do
    if each run invents a fresh ref. The branch is created by the workflow
    before the model runs, so the ref is never chosen by the model.
    """
    number = str(issue_number or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", number):
        return ""
    return "opencode/issue{}-continuum".format(number)


def salvage_branch_name(issue_number, run_id) -> str:
    """The quarantine ref a ``refuse`` pushes preserved work to.

    It is a normal agent branch: same trust rules, same merge gates, same
    reconciliation. Work that Continuum refuses to publish must still be work
    something can find, or refusing has silently become discarding.
    """
    number = str(issue_number or "").strip()
    run = str(run_id or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", number):
        return ""
    if not re.fullmatch(r"[0-9]{1,20}", run):
        return ""
    return "opencode/issue{}-salvage-{}".format(number, run)


def select_owned_branch(candidates, current_branch: str = "") -> str:
    """Choose the workflow-owned branch from the branches an agent run created.

    The self-plane agent action creates its own branch name, so the workflow
    has to learn it after the fact. That inference is the one place ambiguity
    could publish to the wrong ref, so it is a closed rule rather than a shell
    ``head -1``:

    * nothing in the agent shape -> ``""`` (refuse; never a non-agent ref);
    * exactly one candidate -> that one;
    * more than one -> ``""``.

    More than one is a refusal even when one of them is the current branch.
    The workspace being on a branch is a fact about where the *model* walked,
    and a model that can create a second ``opencode/*`` branch would otherwise
    choose the ref this workflow publishes its finished work to -- which is the
    original kodmai #35 bug wearing a different hat. The cost of refusing is
    that no pull request is opened; the run still quarantines the finished tree
    on a salvage branch, so a refusal costs a review, not the work.

    ``current_branch`` is accepted and used only to keep the caller honest about
    what it is passing, and to keep this signature stable for the CLI.
    """
    shaped = sorted({c.strip() for c in candidates if is_agent_branch(c)})
    if len(shaped) == 1:
        return shaped[0]
    return ""


# --------------------------------------------------------------------------- #
# Workspace evaluation
# --------------------------------------------------------------------------- #


def evaluate_workspace(
    *,
    owned_branch: str = "",
    current_branch: str = "",
    base_branch: str = DEFAULT_BASE_BRANCH,
    dirty: bool = False,
    commits_ahead: int = 0,
    open_pull_request=0,
    head_sha: str = "",
    work_branches=(),
) -> WorkspaceDecision:
    """Decide how the workflow publishes the work an agent produced.

    Every input is a fact the caller observed, not a judgement: the branch the
    workspace is on, whether the tree is dirty, how far the head is ahead of the
    base, whether an open pull request already exists for the branch, which
    commit currently holds the work, and which agent branches carry commits the
    base does not have.

    The decision order is what makes it lossless. Identity is checked first,
    because a decision about the wrong ref is worse than no decision; publication
    is checked second, because publishing twice is how one task becomes two;
    only then is the state of the work considered.

    ``head_sha`` is reported, not interpreted. It is what makes a squashed
    recovery auditable: the caller can state the commit the work came from, so
    "we rebuilt your tree" is a claim a reader can check.
    """
    base = base_branch.strip() if isinstance(base_branch, str) else ""
    base = base or DEFAULT_BASE_BRANCH
    salvaged = head_sha.strip().lower() if isinstance(head_sha, str) else ""
    if not SHA_RE.match(salvaged):
        salvaged = ""

    # The work the agent produced may not be on the branch the workspace is
    # standing on: it can commit to a branch, walk away, and return. Those
    # branches are ordered by the caller, most work first, and are recorded
    # even when the current tree is clean -- "clean" is only good news if it is
    # clean *and* there is nothing elsewhere.
    stranded = [b.strip() for b in work_branches or [] if is_agent_branch(b)]
    here_has_work = bool(dirty or commits_ahead > 0)

    if not is_agent_branch(owned_branch):
        return WorkspaceDecision(
            action="refuse",
            code="no_owned_branch",
            reason=(
                "The workflow does not know which branch it owns for this run "
                "({!r} is not an agent branch), so there is no ref it is "
                "willing to publish to. Preserved work is pushed to a salvage "
                "branch and the run is reported rather than published to a "
                "guessed ref."
            ).format(owned_branch),
            base_branch=base,
            report_failure=True,
            recoverable=False,
            salvage_source=(current_branch if here_has_work else (stranded[0] if stranded else "")),
        )

    if open_pull_request:
        return WorkspaceDecision(
            action="already-published",
            code="open_pull_request_exists",
            reason=(
                "Open pull request #{} already owns {}. Publication is "
                "idempotent: a second run, or a retry after a failed wrapper "
                "step, must not turn one task into two pull requests."
            ).format(open_pull_request, owned_branch),
            publish_branch=owned_branch,
            base_branch=base,
        )

    on_owned_branch = isinstance(current_branch, str) and current_branch.strip() == owned_branch

    if not here_has_work and stranded:
        # The workspace looks empty and is not. A model that commits to a branch
        # and then switches back leaves commits that the current tree, the
        # current head and a `status --porcelain` all agree do not exist. Every
        # one of those is exactly what the original step consulted, which is how
        # a finished implementation reached "nothing to recover" and was
        # discarded with the runner.
        #
        # This is a refusal rather than a publication: more than one branch is
        # in play and the workflow does not own any of them, so the work is
        # preserved under its own ref and reported.
        return WorkspaceDecision(
            action="refuse",
            code="work_stranded_on_another_branch",
            reason=(
                "The workspace is clean, but {} still {} commits that {} does "
                "not: {}. The agent committed to a branch and moved away, so the "
                "finished work is on {}. Continuum does not know which branch it "
                "owns, so that work is preserved on a salvage branch and the run "
                "is reported instead of being published to a branch the model "
                "chose."
            ).format(
                ", ".join(stranded),
                "holds" if len(stranded) == 1 else "hold",
                base,
                owned_branch,
                stranded[0],
            ),
            base_branch=base,
            report_failure=True,
            recoverable=False,
            salvage_source=stranded[0],
        )

    if on_owned_branch:
        if not here_has_work:
            return WorkspaceDecision(
                action="publish",
                code="no_work_to_publish",
                reason=(
                    "{} is clean and has no commits ahead of {}. The agent "
                    "produced no change, which is an outcome and not a failure."
                ).format(owned_branch, base),
                publish_branch=owned_branch,
                base_branch=base,
            )
        return WorkspaceDecision(
            action="publish",
            code="agent_stayed_on_branch",
            reason=(
                "The agent left the work on {}. Committing and pushing it "
                "preserves the agent's own commits verbatim."
            ).format(owned_branch),
            publish_branch=owned_branch,
            base_branch=base,
        )

    if not here_has_work:
        # Drift with no work on the drifted branch is not a recovery: there is
        # nothing to salvage, so the run reports that rather than pushing an
        # empty commit.
        return WorkspaceDecision(
            action="publish",
            code="drift_without_work",
            reason=(
                "The agent left {} for {!r} but left no commits and no "
                "uncommitted changes behind, so there is no work to "
                "materialize. The agent produced no change, which is an outcome "
                "and not a failure."
            ).format(owned_branch, current_branch),
            publish_branch=owned_branch,
            base_branch=base,
        )

    return WorkspaceDecision(
        action="materialize",
        code="branch_drift",
        reason=(
            "The agent left the work on {!r} instead of {}. The finished tree "
            "is rebuilt as one commit on {} and pushed there, so the workflow "
            "publishes to a ref it owns rather than to a branch the model "
            "chose."
        ).format(current_branch, owned_branch, owned_branch),
        publish_branch=owned_branch,
        base_branch=base,
        salvaged_head=salvaged,
        salvage_source=current_branch,
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def one_line(text, limit: int = 480) -> str:
    """Collapse text to a single log-safe line.

    Issue and pull-request text is attacker-influenceable and ends up in
    workflow logs, so control characters and workflow commands are removed
    before the text is ever printed.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    cleaned = re.sub(r"(?m)^\s*::", r"\\:", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _output_line(key: str, value) -> str:
    """Render one ``GITHUB_OUTPUT`` entry, heredoc-quoting multi-line values.

    ``GITHUB_OUTPUT`` has no escaping for a raw newline, so a value containing
    one has to use the delimiter form or the file is corrupted for every later
    step in the job.
    """
    text = "" if value is None else str(value)
    if "\n" not in text:
        return "{}={}".format(key, text)
    index = 0
    while True:
        delimiter = "continuum_workspace_{}".format(index)
        if delimiter not in text:
            return "{}<<{}\n{}\n{}".format(key, delimiter, text, delimiter)
        index += 1


def write_outputs(outputs: dict) -> None:
    """Emit ``key=value`` pairs to stdout and ``GITHUB_OUTPUT``.

    Both sinks, deliberately. Steps consume this policy by reading
    ``$GITHUB_OUTPUT`` after the step and by parsing stdout with ``sed``; a
    value written to only one of them is invisible to half the callers, and a
    caller reading an empty action cannot tell "nothing to do" from "this is
    broken".
    """
    for key, value in outputs.items():
        print("{}={}".format(key, value))
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write("{}\n".format(_output_line(key, value)))


def report(decision: WorkspaceDecision, stream=None) -> None:
    """Log the decision for humans.

    Stderr, so stdout stays a clean ``key=value`` stream that the workflow
    steps parse with ``sed`` and ``grep``.
    """
    out = stream or sys.stderr
    print(
        "agent-workspace [{}] {} -> {}".format(
            decision.code, one_line(decision.reason), decision.action
        ),
        file=out,
    )


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "on", "1"}


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def cmd_workspace_plan(args) -> int:
    decision = evaluate_workspace(
        owned_branch=args.owned_branch,
        current_branch=args.current_branch,
        base_branch=args.base_branch or os.environ.get("CONTINUUM_BASE_BRANCH", DEFAULT_BASE_BRANCH),
        dirty=_as_bool(args.dirty),
        commits_ahead=_as_int(args.commits_ahead),
        open_pull_request=_as_int(args.open_pull_request),
        head_sha=args.head_sha,
        work_branches=[b for b in (args.work_branches or "").split(",") if b],
    )
    write_outputs(decision.as_outputs())
    report(decision)
    # `refuse` is a decision, not a crash: the caller has to preserve the work
    # and report it, which it can only do if this command still exits cleanly.
    return 0


def cmd_select_branch(args) -> int:
    branch = select_owned_branch(
        [c for c in (args.candidates or "").split(",") if c],
        current_branch=args.current_branch,
    )
    write_outputs({"owned_branch": branch})
    if branch:
        print("agent-workspace selected {}".format(branch), file=sys.stderr)
    else:
        print(
            "agent-workspace could not select a single owned branch from {!r}".format(
                args.candidates
            ),
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuum agent workspace policy")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("workspace-plan", help="Decide how to publish an agent workspace")
    plan.add_argument("--owned-branch", dest="owned_branch", default="")
    plan.add_argument("--current-branch", dest="current_branch", default="")
    plan.add_argument("--base-branch", dest="base_branch", default=DEFAULT_BASE_BRANCH)
    plan.add_argument("--dirty", default="false")
    plan.add_argument("--commits-ahead", dest="commits_ahead", default="0")
    plan.add_argument("--open-pull-request", dest="open_pull_request", default="0")
    plan.add_argument("--head-sha", dest="head_sha", default="")
    plan.add_argument(
        "--work-branches",
        dest="work_branches",
        default="",
        help="Comma-separated agent branches holding commits ahead of the base, "
        "most work first. Order is significant: the first one is the ref a "
        "salvage preserves.",
    )
    plan.set_defaults(func=cmd_workspace_plan)

    select = sub.add_parser("select-branch", help="Select the workflow-owned branch")
    select.add_argument("--candidates", default="")
    select.add_argument("--current-branch", dest="current_branch", default="")
    select.set_defaults(func=cmd_select_branch)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
