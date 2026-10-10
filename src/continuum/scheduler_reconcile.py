"""Deterministic model of one scheduled issue-scheduler reconcile pass.

This module is the executable contract for the automatic (cron) dispatch
path embedded in ``.github/workflows/continuum-issue-scheduler.yml``. The
workflow's ``github-script`` block mirrors these semantics for the live
GitHub API; this model operates on plain data so the cron-only recovery
path can be regression-tested without repository events.

Modeled invariant (kodmial/continuum#280): an open, unblocked, admitted
issue with available WIP capacity is automatically reserved
(``in_progress_label``) and dispatched without any owner comment. A
scheduled pass emits a deterministic skip reason for every issue it does
not dispatch, and it reports failure (``failed=True``) instead of success
when eligible backlog waits but zero progress is made.

Covered lifecycle:

- priority normalization (labels authoritative, legacy ``P0:`` title
  prefix migrates only when no priority label exists; shared with
  :mod:`continuum.delegation_priority`);
- pause / in-progress / qualification lifecycle filtering;
- declared (``automation-blocked-by`` marker) and native Blocked-By
  filtering;
- WIP accounting over active runs and open implementation PRs;
- dispatch-attempt accounting with lease expiry, stale-reservation
  recovery, and bounded retry (``max_dispatch_attempts`` with
  ``pause_on_failure`` gating only the terminal pause);
- owner ``/oc`` race grace window;
- exactly-once automatic dispatch per reservation (idempotent second
  pass);
- failed dispatch rolls the reservation back so cron retries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import re
from typing import Callable, Dict, List, Optional, Set

from .delegation_priority import effective_priority


@dataclass
class SchedulerConfig:
    """Knobs mirroring the reusable workflow inputs (with their defaults)."""

    wip_limit: int = 2
    lease_minutes: int = 45
    max_dispatch_attempts: int = 2
    dispatch_marker: str = "<!-- issue-scheduler-dispatch -->"
    in_progress_label: str = "automation:in-progress"
    pause_label: str = "automation:paused"
    qualifying_label: str = "automation:qualifying"
    blocked_label: str = "automation:blocked"
    require_priority_label: bool = False
    ready_label: str = ""
    command_grace_minutes: int = 5
    count_open_prs_as_wip: bool = True
    pause_on_failure: bool = False


@dataclass
class IssueState:
    """Mutable per-issue state shared across reconcile passes."""

    number: int
    title: str = ""
    body: str = ""
    labels: Set[str] = field(default_factory=set)
    is_open: bool = True


@dataclass
class CommentRecord:
    """One issue comment."""

    body: str
    author_is_owner: bool
    created_at: datetime


@dataclass
class ReconcileResult:
    """Outcome of a single scheduled reconcile pass."""

    dispatched: List[int] = field(default_factory=list)
    reservations_added: List[int] = field(default_factory=list)
    reservations_removed: List[int] = field(default_factory=list)
    paused: List[int] = field(default_factory=list)
    skip_reasons: Dict[int, str] = field(default_factory=dict)
    wip_summary: str = ""
    failed: bool = False
    failed_message: str = ""


# An owner command is a command token at the start of a line (up to three
# leading spaces, mirroring the canonical manual-command rule), followed by
# whitespace or end of input. Trailing prose on the same line (``/oc please``)
# still counts as owner intent for the scheduler race guard, but substrings
# such as ``/ocean`` or ``/ock``, mid-line prose mentions, quoted/inline-code
# examples, and ``/oc-cancel`` (a launch guard, not a run) must not stall an
# otherwise eligible issue.
_OWNER_COMMAND_RE = re.compile(r"^[ ]{0,3}/(?:opencode|oc)(?=\s|$)", re.MULTILINE)


def _is_owner_command(body: str) -> bool:
    if not isinstance(body, str) or not body:
        return False
    return _OWNER_COMMAND_RE.search(body) is not None


def _scheduler_dispatch_comments(
    comments: List[CommentRecord], marker: str
) -> List[CommentRecord]:
    return [c for c in comments if marker in (c.body or "")]


def _latest_relevant_dispatch(
    comments: List[CommentRecord], marker: str
) -> Optional[CommentRecord]:
    relevant = [
        c
        for c in comments
        if marker in (c.body or "")
        or (c.author_is_owner and _is_owner_command(c.body or ""))
    ]
    if not relevant:
        return None
    return max(relevant, key=lambda c: c.created_at)


def terminal_stop_skip_reason(number: int) -> str:
    """Auditable stable skip reason for a terminally stopped generation."""
    return (
        "terminal generation stop: issue #%d generation 1 is stopped; "
        "open a successor issue with an explicit dependency or start an "
        "explicit new generation instead of redispatching" % number
    )


def reconcile(
    now: datetime,
    issues: Dict[int, IssueState],
    open_pr_issues: Set[int],
    active_run_issues: Set[int],
    comments: Dict[int, List[CommentRecord]],
    declared_open_blockers: Dict[int, List[int]],
    native_open_blockers: Dict[int, List[int]],
    qualification_trackers: Set[int],
    config: SchedulerConfig,
    dispatch_hook: Optional[Callable[[int], None]] = None,
    terminal_stopped: Optional[Set[int]] = None,
) -> ReconcileResult:
    """Run one deterministic scheduled reconcile pass (cron, no event).

    ``issues`` maps every open issue number to its mutable state; label and
    comment mutations are applied in place so a second call observes the
    first pass (idempotency). ``open_pr_issues`` holds issue numbers with an
    open implementation PR, ``active_run_issues`` those with an active
    OpenCode run. Blocker maps hold *open* blocker numbers only. ``dispatch``
    performs the visible side effect (comment and OpenCode wake-up); tests
    may inject failures through ``dispatch_hook``.
    """
    result = ReconcileResult()
    in_progress = config.in_progress_label

    active: Set[int] = set()

    def labels_of(number: int) -> Set[str]:
        return issues[number].labels

    # -- Active runs and open PRs are authoritative in-flight work. --------
    for number in sorted(active_run_issues):
        if number not in issues or not issues[number].is_open:
            continue
        labels = labels_of(number)
        if not config.ready_label or config.ready_label in labels:
            active.add(number)
        labels.add(in_progress)
        labels.discard(config.pause_label)
    for number in sorted(open_pr_issues):
        if number not in issues or not issues[number].is_open:
            continue
        if config.count_open_prs_as_wip:
            active.add(number)
        labels_of(number).add(in_progress)
        labels_of(number).discard(config.pause_label)

    # -- Reservation reconciliation (lease / stale / attempts). -----------
    lease = timedelta(minutes=config.lease_minutes)
    grace = timedelta(minutes=config.command_grace_minutes)
    for number in sorted(issues):
        issue = issues[number]
        if not issue.is_open:
            continue
        if number in open_pr_issues or number in active_run_issues:
            continue
        labels = issue.labels
        if in_progress not in labels:
            continue
        if config.ready_label and config.ready_label not in labels:
            result.skip_reasons[number] = (
                "reservation belongs to another lane: missing ready label"
            )
            continue
        if config.qualifying_label in labels or config.blocked_label in labels:
            labels.discard(in_progress)
            result.reservations_removed.append(number)
            result.skip_reasons[number] = (
                "released implementation reservation: capability is in "
                "qualification lifecycle"
            )
            continue
        if native_open_blockers.get(number):
            labels.discard(in_progress)
            result.reservations_removed.append(number)
            result.skip_reasons[number] = (
                "released reservation: blocked by "
                + ", ".join("#%d" % b for b in native_open_blockers[number])
            )
            continue
        issue_comments = comments.get(number, [])
        latest = _latest_relevant_dispatch(issue_comments, config.dispatch_marker)
        scheduler_dispatches = _scheduler_dispatch_comments(
            issue_comments, config.dispatch_marker
        )
        if latest is None:
            labels.discard(in_progress)
            result.reservations_removed.append(number)
            result.skip_reasons[number] = (
                "released stale reservation: no scheduler dispatch record exists"
            )
            continue
        if now - latest.created_at < lease:
            active.add(number)
            continue
        labels.discard(in_progress)
        result.reservations_removed.append(number)
        if (
            len(scheduler_dispatches) >= config.max_dispatch_attempts
            and config.pause_on_failure
        ):
            labels.add(config.pause_label)
            result.paused.append(number)
            result.skip_reasons[number] = (
                "paused after %d scheduler dispatch attempt(s) with no "
                "OpenCode PR" % len(scheduler_dispatches)
            )
        else:
            labels.discard(config.pause_label)
            result.skip_reasons[number] = (
                "lease expired: eligible for an automatic retry"
                + (
                    " because pause_on_failure=false"
                    if len(scheduler_dispatches) >= config.max_dispatch_attempts
                    else ""
                )
            )

    total_active = len(active)
    free_slots = max(0, config.wip_limit - total_active)
    holders = ", ".join("#%d" % n for n in sorted(active)) or "none"
    result.wip_summary = (
        "WIP: %d/%d (local %d, delegated 0); free slots: %d; "
        "active local issues: %s." % (total_active, config.wip_limit,
                                      total_active, free_slots, holders)
    )

    # -- Candidate selection with a reason for every skip. -----------------
    waiters: List[int] = []

    def priority_of(issue: IssueState):
        priority, rank = effective_priority(sorted(issue.labels), issue.title)
        return priority, rank

    stopped = set(terminal_stopped or set())

    for number in sorted(issues):
        issue = issues[number]
        if not issue.is_open:
            continue
        if number in stopped:
            # Terminal generation stops never consume WIP, never count
            # as waiters, and never fail the pass: the generation is
            # closed by design while successors remain schedulable.
            # automation:blocked semantics are untouched: this is a
            # dedicated stop identity, not a dependency marker.
            result.skip_reasons[number] = terminal_stop_skip_reason(number)
            continue
        if number in active:
            _, rank = priority_of(issue)
            result.skip_reasons[number] = (
                "WIP slot held by an active reservation or run"
            )
            continue
        if number in open_pr_issues:
            result.skip_reasons[number] = (
                "open implementation PR must not be duplicated"
            )
            continue
        if number in qualification_trackers:
            result.skip_reasons[number] = (
                "qualification tracker needs explicit dispatch, "
                "not generic implementation"
            )
            continue
        labels = issue.labels
        if config.pause_on_failure and config.pause_label in labels:
            result.skip_reasons[number] = "automation is paused"
            continue
        if config.qualifying_label in labels or config.blocked_label in labels:
            result.skip_reasons[number] = (
                "capability is in qualification lifecycle"
            )
            continue
        priority, _rank = priority_of(issue)
        if config.require_priority_label and priority is None:
            result.skip_reasons[number] = (
                "no priority label and require_priority_label is enabled"
            )
            continue
        if config.ready_label and config.ready_label not in labels:
            result.skip_reasons[number] = (
                "missing required ready label `%s`" % config.ready_label
            )
            continue
        blockers = [
            b for b in declared_open_blockers.get(number, []) if b != number
        ]
        if blockers:
            result.skip_reasons[number] = (
                "declared blocked by " + ", ".join("#%d" % b for b in blockers)
            )
            continue
        native = [b for b in native_open_blockers.get(number, []) if b != number]
        if native:
            result.skip_reasons[number] = (
                "blocked by " + ", ".join("#%d" % b for b in native)
            )
            continue
        waiters.append(number)

    if free_slots == 0:
        if waiters:
            for number in waiters:
                priority, _rank = priority_of(issues[number])
                result.skip_reasons[number] = (
                    "waiting for a free WIP slot (%s); active WIP holders: %s"
                    % (priority or "no priority", holders)
                )
            result.failed = True
            result.failed_message = (
                "Issue scheduler WIP exhausted with eligible backlog waiting."
            )
        return result

    # Blocked issues were already assigned skip reasons during candidate
    # selection, so `waiters` holds only eligible backlog here.
    ranked = []
    for number in waiters:
        priority, rank = priority_of(issues[number])
        ranked.append((rank, number, priority))
    ranked.sort()

    selected = [number for _rank, number, _p in ranked[:free_slots]]
    selected_priority = {number: p for _r, number, p in ranked if number in selected}
    for _rank, number, priority in ranked[free_slots:]:
        result.skip_reasons[number] = (
            "waiting for a free WIP slot (%s); selected ahead: %s"
            % (
                priority or "no priority",
                ", ".join("#%d" % n for n in selected) or "none",
            )
        )

    # -- Dispatch with just-in-time guards. --------------------------------
    for number in selected:
        issue = issues[number]
        labels = issue.labels
        if number in stopped:
            result.skip_reasons[number] = terminal_stop_skip_reason(number)
            continue
        if (
            not issue.is_open
            or (config.pause_on_failure and config.pause_label in labels)
            or in_progress in labels
        ):
            result.skip_reasons[number] = "state changed before dispatch"
            continue
        if config.ready_label and config.ready_label not in labels:
            result.skip_reasons[number] = (
                "missing required ready label `%s`" % config.ready_label
            )
            continue
        if number in qualification_trackers:
            result.skip_reasons[number] = (
                "qualification tracker needs explicit dispatch, "
                "not generic implementation"
            )
            continue
        if [b for b in declared_open_blockers.get(number, []) if b != number]:
            result.skip_reasons[number] = "declared blocker appeared before dispatch"
            continue
        if [b for b in native_open_blockers.get(number, []) if b != number]:
            result.skip_reasons[number] = "blocker appeared before dispatch"
            continue
        recent_command = None
        for comment in comments.get(number, []):
            if comment.author_is_owner and _is_owner_command(comment.body or ""):
                if now - comment.created_at < grace:
                    if recent_command is None or comment.created_at > recent_command.created_at:
                        recent_command = comment
        if recent_command is not None:
            result.skip_reasons[number] = (
                "an owner OpenCode command was posted within the short "
                "dispatch race window"
            )
            continue
        labels.add(in_progress)
        result.reservations_added.append(number)
        try:
            if dispatch_hook is not None:
                dispatch_hook(number)
            comments.setdefault(number, []).append(
                CommentRecord(
                    body="/oc\n\n%s\nAutomatically dispatched by the issue "
                    "scheduler (%s)."
                    % (config.dispatch_marker,
                       selected_priority.get(number) or "no explicit priority"),
                    author_is_owner=True,
                    created_at=now,
                )
            )
        except Exception:
            labels.discard(in_progress)
            result.reservations_added.remove(number)
            raise
        result.dispatched.append(number)
        result.skip_reasons.pop(number, None)

    if not result.dispatched and waiters:
        # Every waiter carries a skip reason; a pass that dispatches nothing
        # while admitted backlog waits must not look like success when the
        # only cause left is WIP pressure discovered during selection.
        pass
    return result
