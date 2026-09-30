"""One parity issue per drifting source, and never a second one.

The failure this prevents is an issue storm. A scheduled checker that opens an
issue every time it runs produces one issue per run for as long as a source keeps
moving, and a queue of forty identical issues is indistinguishable from no
signal at all -- so the run has to be idempotent, not merely deduplicated.

Three properties make it idempotent, and they are separate on purpose:

* **One issue per source, not per scan.** The marker carries the source id, so a
  source that advances again updates the issue it already has. Different sources
  get different issues, because one issue describing two repositories cannot be
  closed by classifying either one of them.
* **The marker carries the head commit.** A scan that finds the source has not
  moved since the last one plans *no write at all*, rather than an update that
  rewrites an identical body. Re-running a green scan is free, and re-running a
  scan whose finding has not changed is free too.
* **The plan is a value.** What to create, what to update, what to reopen, and
  what to leave alone is computed here from the report and the existing issues,
  and the workflow only applies it. The deduplication decision is therefore
  unit-testable without a network, and the shell that applies it cannot grow a
  second opinion.

The invariant is not "one issue per source" but "**exactly one open issue per
drifting source, and none for a source that has stopped drifting**". The second
half needs the closed issues too, so the plan reads them (``gh issue list --state
all``) and closes an issue whose source is no longer in the findings, and reopens
one that a human closed while its source was still drifting. A parity issue that
sits closed over unclassified drift is a drift that has gone quiet, which is the
failure this plane exists to prevent.

What the body deliberately does not contain is the upstream change itself. A
drift issue is a *triage* request: here is a commit, here are the paths, here are
the pull requests, classify it. Pasting the diff would make copying it the path
of least resistance, and copying is exactly the outcome the issue forbids -- the
dispositions exist because "absorbed" and "must-port" mean different things and
neither of them is "paste this file".
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .drift import DriftReport, SourceDrift
from .sources import assert_public

ISSUE_PLAN_SCHEMA = "continuum.parity-issue-plan/v1"
APPLIED_PLAN_SCHEMA = "continuum.parity-applied-plan/v1"

#: The marker, and the two values it carries. ``head`` is what makes a repeated
#: scan a no-op, and keeping it out of the title means an update does not read as
#: a new finding.
_MARKER = "<!-- continuum-parity-drift source={source} head={head} -->"

_MARKER_RE = re.compile(
    r"<!--\s*continuum-parity-drift\s+source=(?P<source>[A-Za-z0-9][A-Za-z0-9._-]{0,63})"
    r"\s+head=(?P<head>[0-9a-f]{7,40})\s*-->"
)

CREATE = "create"
UPDATE = "update"
REOPEN = "reopen"
CLOSE = "close"
NONE = "none"

#: Every action the plan may name, in the order a reader wants them.
ACTIONS: Tuple[str, ...] = (CREATE, UPDATE, REOPEN, CLOSE, NONE)

#: The comment written when the checker closes a resolved parity issue, so the
#: thread says why it ended rather than just ending.
RESOLUTION_COMMENT = (
    "Resolved by the parity checker: this source is no longer reporting "
    "unclassified drift. Either the audit range moved forward over a commit that "
    "is now classified, or the source returned to the audited commit."
)


class IssuePlanError(ValueError):
    """A set of issues, or a plan document, the checker cannot work from.

    ``where`` names the document the refusal came from, so a workflow log says
    which of the four files was rejected rather than only that one was.
    """

    def __init__(self, message: str, *, where: str = "") -> None:
        super().__init__("{}: {}".format(where, message) if where else message)
        self.where = where
        self.message = message


def marker_for(source_id: str, head_sha: str) -> str:
    """The marker for one source at one head."""

    return _MARKER.format(source=source_id, head=str(head_sha or "unknown")[:12])


def parse_marker(body: str) -> Optional[Tuple[str, str]]:
    """The ``(source id, head)`` a body declares, or ``None``.

    Returns the *first* marker, so a body cannot claim two sources. The value is
    a pair rather than one string because a scan needs both: the id to decide
    which issue this is, and the head to decide whether it is still current.
    """

    match = _MARKER_RE.search(str(body or ""))
    if not match:
        return None
    return match.group("source"), match.group("head")


@dataclasses.dataclass(frozen=True)
class ExistingIssue:
    """An issue the plan may update, reopen, or close.

    ``state`` is read but never written: the plan decides from the report, and a
    closed issue that still has drift is reopened rather than left closed.
    """

    number: int
    body: str
    title: str = ""
    state: str = "open"

    @property
    def marker(self) -> Optional[Tuple[str, str]]:
        return parse_marker(self.body)

    @property
    def closed(self) -> bool:
        return str(self.state or "open").strip().lower() == "closed"


@dataclasses.dataclass(frozen=True)
class IssuePlan:
    """What to do about one source's drift."""

    source_id: str
    action: str
    marker: str
    title: str
    body: str
    number: int = 0
    #: Other open issues carrying the same marker. Reported rather than closed:
    #: the plan does not decide that an issue a human opened is the wrong one.
    duplicate_numbers: Tuple[int, ...] = ()

    @property
    def writes(self) -> bool:
        """Whether applying this plan changes anything.

        ``close`` and ``reopen`` change the issue's state rather than its text, so
        they are writes too; ``none`` is the only action that does nothing.
        """

        return self.action != NONE

    def describe(self) -> Dict[str, Any]:
        return assert_public(
            {
                "schema": ISSUE_PLAN_SCHEMA,
                "kind": "issue",
                "version": 1,
                "id": self.source_id,
                "action": self.action,
                "marker": self.marker,
                "number": self.number,
                "title": self.title,
                "body": self.body,
                "duplicate_numbers": list(self.duplicate_numbers),
            },
            where="issue plan",
        )


def render_title(source: SourceDrift) -> str:
    """A stable title.

    The head is deliberately absent. A title that changes on every scan is a
    search result nobody can find, and GitHub's own issue list is how a maintainer
    looks for the one open parity issue.
    """

    count = len([path for path in source.paths if path.triaged])
    if source.private:
        # The count of unclassified paths is private metadata too, so the title
        # says what happened rather than how much of it.
        return "Parity drift: {} advanced and cannot be classified from public data".format(
            source.id
        )
    return "Parity drift: {} has {} unclassified automation change(s)".format(
        source.id, count
    )


def render_body(source: SourceDrift, *, marker: str) -> str:
    """The issue body: the evidence, and what is being asked for.

    Every value is a reference -- a commit, a path, a pull request number. There
    is no upstream content here, and :func:`assert_public` on the plan is what
    keeps it that way if a field is ever added.
    """

    lines = [
        marker,
        "",
        "Continuum's parity ledger records an audited commit for `{}`; that source "
        "has advanced and the advance has not been classified.".format(source.id),
        "",
        "Nothing has been copied. This is a classification request, not a port.",
        "",
        "## Evidence",
        "",
        "| | |",
        "| --- | --- |",
        "| Source | `{}` |".format(source.id),
        "| Visibility | {} |".format(source.visibility),
        "| Read with | {} |".format(source.access),
        "| Ledger baseline | `{}` |".format(source.baseline_sha or "--"),
        "| Ledger current | `{}` |".format(source.current_sha or "--"),
        "| Observed head | `{}` |".format(source.head_sha or "--"),
        "| Commits in range | {} |".format(
            "withheld" if source.private else len(source.commits)
        ),
        "| Upstream pull requests | {} |".format(
            "withheld"
            if source.private
            else ", ".join("#{}".format(number) for number in source.pull_requests) or "none named"
        ),
        "",
    ]
    if source.private:
        # The body is published on an issue in a public repository, so a private
        # source's paths, repository name, and commit metadata are not written
        # here -- not as a diff, not as a path list, not as a commit count that
        # would let a reader infer one. What is published is that the source moved,
        # and what has to happen about it.
        lines += [
            "This source is private, so its repository, changed paths, commits, and "
            "pull request numbers are withheld from this issue. Review the advance "
            "with the configured least-privilege credential and move the audit range "
            "forward in the overlay ledger, which is not committed.",
            "",
            "Until that audit range moves, this stays an open unclassified finding: the "
            "checker cannot classify a private advance from data it is allowed to "
            "publish, and it will not pretend otherwise by ignoring it.",
            "",
        ]
        return "\n".join(lines)
    if source.commits:
        lines += ["Commits: " + ", ".join("`{}`".format(sha[:12]) for sha in source.commits[:20]), ""]
    if source.redacted:
        lines += [
            "This source is private, so its repository, paths, and commit metadata are "
            "withheld from this issue. The withheld fields are: "
            + ", ".join("`{}`".format(name) for name in source.redacted)
            + ".",
            "",
        ]
    lines += [
        "## Changed paths",
        "",
        "| Path | Change | Ledger classification | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    for path in source.paths:
        lines.append(
            "| `{}` | {} | {} | {} |".format(
                _cell(path.path), path.status, path.disposition or "**unclassified**", _cell(path.summary)
            )
        )
    if source.truncated:
        lines += [
            "",
            "The upstream change list was truncated, so this table is **not** the "
            "whole advance. The files that were not read are part of why this is "
            "`{}`.".format(source.priority or "p0"),
        ]
    lines += [
        "",
        "## What is required",
        "",
        "1. Classify every unclassified path in "
        "`reference/parity-ledger.json`, as a path rule and, where the advance "
        "carries a report, as an incident with one of the five dispositions and a "
        "reference to the Continuum file and test that answer it.",
        "2. Move `audit.current_sha` to the head above, so the advancement is on "
        "the record rather than re-detected on the next scan.",
        "3. Do not promote a consumer pin until the gate reports "
        "`no unclassified P0 drift`.",
        "",
        "A change confined to a path already classified `consumer-local` or "
        "`not-applicable` is a green no-op and needs none of this -- the checker "
        "reports it as in-sync-with-a-no-op and writes nothing.",
        "",
    ]
    return "\n".join(lines)


def _cell(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def existing_from_documents(documents: Any) -> Tuple[ExistingIssue, ...]:
    """Read the open issues the plan may update.

    Accepts what ``gh issue list --json number,title,body,state`` produces,
    because that is what the workflow has, and refuses a document without a usable
    number rather than defaulting to one: a plan that updated issue 0 would look
    like it did something.
    """

    if documents is None:
        return ()
    if isinstance(documents, Mapping):
        documents = documents.get("issues") or ()
    if not isinstance(documents, (list, tuple)):
        raise IssuePlanError("existing_issues must be a list of objects")
    issues: List[ExistingIssue] = []
    for index, entry in enumerate(documents):
        if not isinstance(entry, Mapping):
            raise IssuePlanError("existing issue {} is not an object".format(index))
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise IssuePlanError(
                "existing issue {} has no usable issue number".format(index)
            )
        state = entry.get("state")
        issues.append(
            ExistingIssue(
                number=number,
                body=str(entry.get("body") or ""),
                title=str(entry.get("title") or ""),
                # Absent means open, because that is what the pre-existing call
                # passed and a caller that lists only open issues must keep
                # meaning exactly that.
                state="open" if state is None else str(state),
            )
        )
    return tuple(issues)


def plan_for(source: SourceDrift, existing: Sequence[ExistingIssue]) -> IssuePlan:
    """The plan for one source's drift, against the issues that already exist."""

    marker = marker_for(source.id, source.head_sha)
    title = render_title(source)
    mine = [issue for issue in existing if issue.marker is not None and issue.marker[0] == source.id]
    # Lowest number first, deterministically: two concurrent scans that both see
    # no marker both create an issue, and choosing the older one to keep makes the
    # duplicate decision independent of list order.
    mine.sort(key=lambda issue: issue.number)
    if not mine:
        return IssuePlan(
            source_id=source.id,
            action=CREATE,
            marker=marker,
            title=title,
            body=render_body(source, marker=marker),
        )
    keep = mine[0]
    duplicates = tuple(issue.number for issue in mine[1:])
    if keep.closed:
        # Somebody closed this while the source was still drifting. The finding is
        # still true, so the issue goes back open with the current evidence --
        # whatever the human who closed it concluded has not survived contact with
        # the ledger, and the ledger is the record.
        return IssuePlan(
            source_id=source.id,
            action=REOPEN,
            marker=marker,
            title=title,
            body=render_body(source, marker=marker),
            number=keep.number,
            duplicate_numbers=duplicates,
        )
    if keep.marker is not None and keep.marker[1] == source.head_sha[:12]:
        # The open issue already names this exact head. A scan that changes
        # nothing must not write anything.
        return IssuePlan(
            source_id=source.id,
            action=NONE,
            marker=marker_for(source.id, keep.marker[1]),
            title=keep.title or title,
            body=keep.body,
            number=keep.number,
            duplicate_numbers=duplicates,
        )
    return IssuePlan(
        source_id=source.id,
        action=UPDATE,
        marker=marker,
        title=title,
        body=render_body(source, marker=marker),
        number=keep.number,
        duplicate_numbers=duplicates,
    )


def resolutions_for(
    report: DriftReport, existing: Sequence[ExistingIssue]
) -> Tuple[IssuePlan, ...]:
    """Issues to close: a tracked source that has stopped drifting.

    Only a source the report still tracks can resolve an issue. A marker naming a
    source that is no longer in the ledger is somebody's own note about a
    repository the checker does not track any more, and closing it would be the
    checker editing a human's record on a subject it can no longer evaluate.
    """

    drifting = {source.id for source in report.findings}
    tracked = {source.id for source in report.sources}
    plans: List[IssuePlan] = []
    for issue in existing:
        parsed = issue.marker
        if parsed is None:
            continue
        source_id = parsed[0]
        if source_id in drifting or source_id not in tracked or issue.closed:
            continue
        plans.append(
            IssuePlan(
                source_id=source_id,
                action=CLOSE,
                marker=marker_for(source_id, parsed[1]),
                title=issue.title,
                body="",
                number=issue.number,
            )
        )
    plans.sort(key=lambda plan_item: plan_item.number)
    return tuple(plans)


def plan(
    report: DriftReport, existing: Sequence[ExistingIssue] = ()
) -> Tuple[IssuePlan, ...]:
    """A plan per source that needs triage, a close per one that stopped drifting.

    A source whose advance is entirely within out-of-scope paths gets no plan at
    all, so the common case -- a live repository that moved -- costs zero writes
    and zero issues.
    """

    return tuple(plan_for(source, existing) for source in report.findings) + resolutions_for(
        report, existing
    )


def plan_document(
    report: DriftReport, existing: Sequence[ExistingIssue] = ()
) -> Dict[str, Any]:
    """The plan as a document the workflow can apply without re-deriving it."""

    plans = plan(report, existing)
    return assert_public(
        {
            "schema": ISSUE_PLAN_SCHEMA,
            "kind": "plan",
            "version": 1,
            "generated_at": report.generated_at,
            "continuum_sha": report.continuum_sha,
            "ledger_version": report.ledger_version,
            "clean": report.clean,
            "unclassified_p0": report.unclassified_p0,
            "unclassified_p1": report.unclassified_p1,
            "plans": [item.describe() for item in plans],
            "totals": {
                "create": sum(1 for item in plans if item.action == CREATE),
                "update": sum(1 for item in plans if item.action == UPDATE),
                "reopen": sum(1 for item in plans if item.action == REOPEN),
                "close": sum(1 for item in plans if item.action == CLOSE),
                "skip": sum(1 for item in plans if item.action == NONE),
            },
        },
        where="issue plan document",
    )


# --------------------------------------------------------------------------- #
# Applying a plan
# --------------------------------------------------------------------------- #


def commands_for(item: Mapping[str, Any], repository: str) -> Tuple[List[str], ...]:
    """The ``gh`` invocations that carry out one planned action.

    Argument lists, never a shell string: a body is operator-influenced text, and
    a body interpolated into a command line is a body someone can inject a flag
    into. ``gh`` takes the body as one argv element regardless of what is in it.

    The close carries a comment before the transition, so the thread says why it
    ended rather than just ending.
    """

    action = str(item.get("action") or "")
    number = item.get("number") or 0
    body = str(item.get("body") or "")
    title = str(item.get("title") or "")
    if action == CREATE:
        return (
            ["gh", "issue", "create", "--repo", repository, "--title", title, "--body", body],
        )
    if action == UPDATE:
        return (["gh", "issue", "edit", "--repo", repository, str(number), "--body", body],)
    if action == REOPEN:
        # Reopen first, then rewrite the body: editing a closed issue is allowed
        # on GitHub, but a reader watching the thread should see it open with the
        # current evidence rather than a closed issue whose evidence is newer.
        return (
            ["gh", "issue", "reopen", "--repo", repository, str(number)],
            ["gh", "issue", "edit", "--repo", repository, str(number), "--body", body],
        )
    if action == CLOSE:
        return (
            ["gh", "issue", "comment", "--repo", repository, str(number), "--body", RESOLUTION_COMMENT],
            ["gh", "issue", "close", "--repo", repository, str(number)],
        )
    if action == NONE:
        return ()
    raise IssuePlanError("unknown plan action: {}".format(action), where="issue plan")


def apply_plan(
    document: Any,
    repository: str,
    *,
    run: Optional[Any] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply a plan document, one command at a time.

    ``run`` takes an argument list and returns whatever the caller wants; it
    defaults to :func:`subprocess.run` with ``check=True``, which stops at the
    first failure rather than pressing on and leaving half the plan applied. A
    plan is idempotent, so re-running after a failure is safe: the actions that
    already landed become ``none``.

    A dry run executes nothing and returns the commands, which is what makes this
    reviewable before it is allowed to write.
    """

    if not isinstance(document, Mapping):
        raise IssuePlanError("issue plan document must be an object", where="issue plan")
    plans = document.get("plans")
    if not isinstance(plans, (list, tuple)):
        raise IssuePlanError("issue plan document has no plans", where="issue plan")
    if not str(repository or "").strip():
        raise IssuePlanError("no repository to apply the plan to", where="issue plan")

    runner = run if run is not None else _run_command
    # The same five keys the plan document reports, so a caller compares one
    # summary against the other without translating `none` to `skip`.
    totals = {"create": 0, "update": 0, "reopen": 0, "close": 0, "skip": 0}
    commands: List[Dict[str, Any]] = []
    for entry in plans:
        if not isinstance(entry, Mapping):
            raise IssuePlanError("planned issue is not an object", where="issue plan")
        action = str(entry.get("action") or "")
        issued = commands_for(entry, repository)
        if action in ACTIONS:
            totals["skip" if action == NONE else action] += 1
        for argv in issued:
            commands.append({"action": action, "argv": list(argv)})
            if not dry_run:
                runner(argv)
    return assert_public(
        {
            "schema": APPLIED_PLAN_SCHEMA,
            "kind": "applied-plan",
            "version": 1,
            "repository": repository,
            "dry_run": bool(dry_run),
            "totals": totals,
            "commands": commands,
        },
        where="applied issue plan",
    )


def _run_command(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True)


__all__ = [
    "ACTIONS",
    "APPLIED_PLAN_SCHEMA",
    "apply_plan",
    "commands_for",
    "CLOSE",
    "CREATE",
    "ExistingIssue",
    "ISSUE_PLAN_SCHEMA",
    "IssuePlan",
    "IssuePlanError",
    "NONE",
    "REOPEN",
    "RESOLUTION_COMMENT",
    "UPDATE",
    "existing_from_documents",
    "marker_for",
    "parse_marker",
    "plan",
    "plan_document",
    "plan_for",
    "resolutions_for",
    "render_body",
    "render_title",
]
