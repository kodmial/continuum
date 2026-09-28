"""The review queue reconciliation contract.

Events are wake-ups, not state. Every event that can change *who the next
eligible review candidate is* must reach the controller; the controller then
recomputes the whole queue from current GitHub state and schedules at most one
action. That is what makes the queue self-healing: a candidate that is closed,
merged, withdrawn, converted to draft, or paused simply stops being eligible, and
the next candidate becomes actionable in the same reconciliation cycle.

    event -> cheap wake-up -> recompute candidates -> rank -> validate lock
          -> schedule exactly one action

This module is pure. It performs no I/O, holds no process state, and takes the
current time as an argument, so a duplicate or reordered wake-up over unchanged
state always produces the identical plan. GitHub is the only source of truth.

Nothing here is provider specific. A CodeRabbit comment, a PR-Agent gate run, and
`review.provider: none` all pass through the same eligibility, ranking, lock, and
cooldown rules; the provider only decides what a dispatch means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

PLAN_SCHEMA = "continuum.review-queue/v1"

# -- event vocabulary --------------------------------------------------------

EVENT_PULL_REQUEST = "pull_request"
EVENT_PULL_REQUEST_REVIEW = "pull_request_review"
EVENT_ISSUE_COMMENT = "issue_comment"
EVENT_ISSUES = "issues"
EVENT_STATUS = "status"
EVENT_CHECK_RUN = "check_run"
EVENT_CHECK_SUITE = "check_suite"
EVENT_WORKFLOW_RUN = "workflow_run"
EVENT_PUSH = "push"
EVENT_SCHEDULE = "schedule"
EVENT_WORKFLOW_DISPATCH = "workflow_dispatch"

# Every action that can change the identity of the next eligible candidate.
# `closed` and `converted_to_draft` are the two that a controller without them
# stalls on: the head of the queue leaves the set and nothing else fires.
PULL_REQUEST_ACTIONS: Tuple[str, ...] = (
    "opened",
    "reopened",
    "synchronize",
    "ready_for_review",
    "converted_to_draft",
    "closed",
    "labeled",
    "unlabeled",
    "edited",
    "auto_merge_enabled",
    "auto_merge_disabled",
)

# A provider comment (rate-limit notice, review summary) and a submitted review
# both change provider cooldown and in-flight settlement state.
ISSUE_COMMENT_ACTIONS: Tuple[str, ...] = ("created", "edited")
PULL_REQUEST_REVIEW_ACTIONS: Tuple[str, ...] = ("submitted", "dismissed")

# An empty tuple means "every action of this event is a wake-up".
WAKE_UP_ACTIONS: Dict[str, Tuple[str, ...]] = {
    EVENT_PULL_REQUEST: PULL_REQUEST_ACTIONS,
    EVENT_PULL_REQUEST_REVIEW: PULL_REQUEST_REVIEW_ACTIONS,
    EVENT_ISSUE_COMMENT: ISSUE_COMMENT_ACTIONS,
    # A priority label on the source issue reorders the queue.
    EVENT_ISSUES: ("labeled", "unlabeled", "reopened", "closed", "edited"),
    EVENT_STATUS: (),
    EVENT_CHECK_RUN: ("completed", "rerequested", "created"),
    EVENT_CHECK_SUITE: ("completed", "rerequested"),
    EVENT_WORKFLOW_RUN: ("completed", "requested"),
    EVENT_PUSH: (),
    EVENT_SCHEDULE: (),
    EVENT_WORKFLOW_DISPATCH: (),
}

# Continuous-delivery events that cannot change review eligibility. They are
# accepted so a wake-up is never lost, but they are reported as such.
NON_QUEUE_EVENTS: Tuple[str, ...] = ("deployment", "deployment_status", "release", "page")

# GitHub's trusted-context trigger for the event vocabulary above.
EVENT_ALIASES: Dict[str, str] = {"pull_request_target": EVENT_PULL_REQUEST}


@dataclass(frozen=True)
class WakeUp:
    """One cheap signal that the queue may have changed.

    The payload is advisory. `reconcile` never trusts it: it recomputes from
    state. That is what makes duplicate and out-of-order wake-ups harmless.
    """

    event: str
    action: str = ""
    pr_number: Optional[int] = None
    reason: str = ""

    @property
    def is_queue_relevant(self) -> bool:
        return is_queue_wake_up(self.event, self.action)

    def describe(self) -> str:
        action = f" {self.action}" if self.action else ""
        pr = f" PR #{self.pr_number}" if self.pr_number else ""
        return f"{normalise_event(self.event)}{action}{pr}".strip()


def normalise_event(event: str) -> str:
    """Map a platform event name onto the queue's vocabulary.

    The queue is triggered by `pull_request_target` (trusted metadata, no pull
    request code is checked out), but it reasons about a pull request event.
    Without this alias the wake-up that matters most -- the one that carries
    `closed` and `converted_to_draft` -- would be discarded as irrelevant.
    """

    name = (event or "").strip()
    return EVENT_ALIASES.get(name, name)


def is_queue_wake_up(event: str, action: str = "") -> bool:
    """True when `event`/`action` can change the next eligible candidate."""

    actions = WAKE_UP_ACTIONS.get(normalise_event(event))
    if actions is None:
        return False
    if not actions or not action:
        return True
    return action in actions


# -- candidate state ---------------------------------------------------------

STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_MERGED = "merged"

CI_SUCCESS = "success"
CI_PENDING = "pending"
CI_FAILURE = "failure"
CI_CANCELLED = "cancelled"
CI_SKIPPED = "skipped"
CI_UNKNOWN = "unknown"

# Normalized per-candidate review need. The provider adapter decides which one
# applies; the queue only reasons about the difference.
REVIEW_UNREVIEWED = "unreviewed"
REVIEW_RETRY = "retry"
REVIEW_CURRENT = "current"

UNPRIORITIZED = "unprioritized"

SUPPORTED_TIE_BREAKERS: Tuple[str, ...] = (
    "source_issue",
    "pr_number",
    "created_at",
    "head_sha",
)

# Reasons a candidate is not actionable right now. They are stable strings
# because they are written verbatim into the reconciliation log.
REASON_DRAFT = "state=draft"
REASON_UNREVIEWED_NONE = "review=current"


def format_duration(milliseconds: int) -> str:
    """Render a duration the way the reconciliation log does (`18m 42s`)."""

    total = max(0, int(milliseconds)) // 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        parts = [f"{hours}h", f"{minutes}m"]
        if seconds:
            parts.append(f"{seconds}s")
        return " ".join(parts)
    if minutes:
        parts = [f"{minutes}m"]
        if seconds:
            parts.append(f"{seconds}s")
        return " ".join(parts)
    return f"{seconds}s"


@dataclass(frozen=True)
class ReviewRequest:
    """A recorded provider review request: the in-flight slot identity.

    Keyed to candidate identity (`pr_number` + `head_sha`), not to a timer, so a
    lock can be validated against live PR state instead of being trusted until
    it expires.
    """

    pr_number: int
    head_sha: str
    requested_at_ms: int
    provider: str = ""
    kind: str = REVIEW_UNREVIEWED
    settled: bool = False
    settled_reason: str = ""
    settled_at_ms: int = 0

    def age_ms(self, now_ms: int) -> int:
        return max(0, int(now_ms) - int(self.requested_at_ms))

    def identity(self) -> str:
        return f"PR #{self.pr_number}#{self.head_sha[:7]}" if self.head_sha else f"PR #{self.pr_number}"


@dataclass(frozen=True)
class Cooldown:
    """The provider's global, repository-wide quiet period.

    Shared quota belongs to the provider, not to a pull request, so it is
    applied after ranking: a higher-priority candidate waits behind a cooldown
    rather than bypassing it.
    """

    until_ms: int = 0
    reason: str = ""

    def remaining_ms(self, now_ms: int) -> int:
        return max(0, int(self.until_ms) - int(now_ms))

    def active(self, now_ms: int) -> bool:
        return self.remaining_ms(now_ms) > 0

    def describe(self, now_ms: int) -> str:
        if not self.active(now_ms):
            return ""
        return f"Review provider cooldown still active for {format_duration(self.remaining_ms(now_ms))}."


@dataclass(frozen=True)
class Candidate:
    """Everything the queue needs to know about one pull request, right now."""

    pr_number: int
    head_sha: str = ""
    state: str = STATE_OPEN
    draft: bool = False
    labels: Tuple[str, ...] = ()
    priority: str = UNPRIORITIZED
    source_issue: Optional[int] = None
    ci: str = CI_SUCCESS
    review: str = REVIEW_UNREVIEWED
    due_at_ms: int = 0
    created_at_ms: int = 0
    request: Optional[ReviewRequest] = None

    def has_label(self, name: str) -> bool:
        return name.lower() in self.labels

    def describe(self) -> Dict[str, Any]:
        return {
            "pr": self.pr_number,
            "head": self.head_sha,
            "priority": self.priority,
            "source_issue": self.source_issue,
            "ci": self.ci,
            "review": self.review,
            "draft": self.draft,
            "state": self.state,
        }


@dataclass(frozen=True)
class QueuePolicy:
    """Configuration-derived queue rules. Provider independent by construction."""

    provider: str = "none"
    ready_label: Optional[str] = "review-ready"
    block_labels: Tuple[str, ...] = ("review-paused", "review-blocked", "no-review")
    priority_labels: Tuple[str, ...] = ("priority:p0", "priority:p1", "priority:p2")
    tie_breakers: Tuple[str, ...] = ("source_issue", "pr_number")
    required_checks: Tuple[str, ...] = ("CI",)
    require_green_ci: bool = True
    cooldown_ms: int = 0
    safety_margin_ms: int = 30_000
    in_flight_timeout_ms: int = 30 * 60_000
    dispatch_workflow: str = "pr-agent.yml"
    max_candidates: int = 200

    def priority_rank(self, priority: str) -> int:
        lowered = (priority or "").lower()
        for index, name in enumerate(self.priority_labels):
            if name.lower() == lowered:
                return index
        return len(self.priority_labels)

    def cooldown_window_ms(self) -> int:
        return int(self.cooldown_ms) + int(self.safety_margin_ms)


def policy_from_config(config: Any) -> QueuePolicy:
    """Build the queue policy from validated repository configuration.

    The provider only supplies the cooldown window and the dispatch target; the
    eligibility, ranking, and lock rules come from the queue settings, so
    swapping the provider does not change queue semantics.
    """

    settings = config.review.queue
    return QueuePolicy(
        provider=config.review.provider,
        ready_label=settings.ready_label,
        block_labels=tuple(settings.block_labels),
        priority_labels=tuple(settings.priority_labels),
        tie_breakers=tuple(settings.tie_breakers),
        required_checks=tuple(settings.required_checks),
        require_green_ci=settings.require_green_ci,
        cooldown_ms=int(settings.cooldown_minutes) * 60_000,
        safety_margin_ms=int(settings.safety_margin_seconds) * 1000,
        in_flight_timeout_ms=int(settings.in_flight_timeout_minutes) * 60_000,
        dispatch_workflow=settings.dispatch_workflow,
        max_candidates=int(settings.max_candidates),
    )


@dataclass(frozen=True)
class QueueState:
    """The authoritative view a reconciliation runs against."""

    wake: WakeUp
    candidates: Tuple[Candidate, ...] = ()
    cooldown: Cooldown = Cooldown()
    now_ms: int = 0

    def by_number(self, number: int) -> Optional[Candidate]:
        for candidate in self.candidates:
            if candidate.pr_number == number:
                return candidate
        return None


# -- eligibility -------------------------------------------------------------


def ineligibility_reason(candidate: Candidate, policy: QueuePolicy) -> Optional[str]:
    """Why this candidate cannot be reviewed right now, or `None` if it can.

    Order matters: lifecycle beats labels beats CI, so a closed draft reports
    `state=closed` rather than `state=draft`.
    """

    if candidate.state == STATE_MERGED:
        return "state=merged"
    if candidate.state == STATE_CLOSED:
        return "state=closed"
    if candidate.state != STATE_OPEN:
        return f"state={candidate.state}"
    if candidate.draft:
        return REASON_DRAFT
    for label in policy.block_labels:
        if candidate.has_label(label):
            return f"blocked:{label}"
    if policy.ready_label and not candidate.has_label(policy.ready_label):
        return f"missing:{policy.ready_label}"
    if policy.require_green_ci and candidate.ci != CI_SUCCESS:
        return f"ci={candidate.ci}"
    if candidate.review == REVIEW_CURRENT:
        return REASON_UNREVIEWED_NONE
    return None


def rank_key(candidate: Candidate, policy: QueuePolicy) -> Tuple:
    """Deterministic ordering: priority, then configured tie-breakers.

    `priority:p0` beats `priority:p1` beats `priority:p2` beats unprioritized;
    every tie is broken by the configured tie-breakers and finally by the PR
    number, so two runs over the same state always agree.
    """

    key: List[Any] = [policy.priority_rank(candidate.priority)]
    for tie_breaker in policy.tie_breakers:
        if tie_breaker == "source_issue":
            key.append(candidate.source_issue if candidate.source_issue is not None else 1 << 30)
        elif tie_breaker == "pr_number":
            key.append(candidate.pr_number)
        elif tie_breaker == "created_at":
            key.append(candidate.created_at_ms)
        elif tie_breaker == "head_sha":
            key.append(candidate.head_sha)
    key.append(candidate.pr_number)
    return tuple(key)


# -- plan --------------------------------------------------------------------

ACTION_DISPATCH = "dispatch"
ACTION_WAIT = "wait"
ACTION_IDLE = "idle"
ACTION_DISABLED = "disabled"


@dataclass(frozen=True)
class Plan:
    """The result of one reconciliation. At most one action is scheduled."""

    action: str
    wake: WakeUp
    now_ms: int = 0
    selected: Optional[Candidate] = None
    queue: Tuple[Candidate, ...] = ()
    excluded: Tuple[Tuple[int, str], ...] = ()
    held_lock: Optional[ReviewRequest] = None
    released_lock: Optional[ReviewRequest] = None
    release_reason: str = ""
    cooldown: Cooldown = Cooldown()
    logs: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def dispatched(self) -> bool:
        return self.action == ACTION_DISPATCH and self.selected is not None

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "wake": self.wake.describe(),
            "action": self.action,
            "dispatched": self.dispatched,
            "selected": self.selected.describe() if self.selected else None,
            "queue": [candidate.describe() for candidate in self.queue],
            "excluded": [{"pr": number, "reason": reason} for number, reason in self.excluded],
            "held_lock": self.held_lock.identity() if self.held_lock else None,
            "released_lock": (
                {"identity": self.released_lock.identity(), "reason": self.release_reason}
                if self.released_lock
                else None
            ),
            "cooldown": {
                "until_ms": self.cooldown.until_ms,
                "reason": self.cooldown.reason,
                "remaining_ms": self.cooldown.remaining_ms(self.now_ms),
            },
            "logs": list(self.logs),
        }


def _open_lock(state: QueueState) -> Optional[ReviewRequest]:
    """Newest unsettled review request across the whole repository."""

    requests = [
        candidate.request
        for candidate in state.candidates
        if candidate.request is not None and not candidate.request.settled
    ]
    if not requests:
        return None
    return sorted(requests, key=lambda item: (item.requested_at_ms, item.pr_number))[-1]


def _transition_logs(state: QueueState, excluded: Sequence[Tuple[Candidate, str]]) -> List[str]:
    """Explain every candidate that left the queue, in deterministic order.

    A PR is reported as having left the queue when Continuum had actually queued
    it: it holds a review request record, or this wake-up describes a change to
    an existing pull request. A pull request that was created ineligible was
    never in the queue, so it is not announced as leaving it.
    """

    logs: List[str] = []
    for candidate, reason in excluded:
        changed = (
            state.wake.pr_number == candidate.pr_number
            and state.wake.action != "opened"
        )
        if not (changed or candidate.request is not None):
            continue
        logs.append(f"PR #{candidate.pr_number} left queue: {reason}.")
    return logs


def _summary_reasons(excluded: Sequence[Tuple[Candidate, str]]) -> str:
    """Compact reason summary for an empty queue, so a stall is explainable."""

    counts: Dict[str, int] = {}
    for _candidate, reason in excluded:
        counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return ""
    parts = [
        f"{reason} x{count}" if count > 1 else reason
        for reason, count in sorted(counts.items())
    ]
    return f"{len(excluded)} outside the eligible set: {', '.join(parts)}"


def reconcile(state: QueueState, policy: QueuePolicy) -> Plan:
    """Recompute the queue from state and schedule at most one action.

    Pure and idempotent: the same state always yields the same plan, so a
    duplicate, reordered, or replayed wake-up cannot produce a second provider
    request.
    """

    now_ms = int(state.now_ms)
    logs: List[str] = []

    if not policy.provider or policy.provider == "none":
        logs.append(
            "review.provider is none; the review queue is disabled and no provider is contacted."
        )
        return Plan(action=ACTION_DISABLED, wake=state.wake, now_ms=now_ms, logs=tuple(logs))

    eligible: List[Candidate] = []
    excluded: List[Tuple[Candidate, str]] = []
    for candidate in state.candidates:
        reason = ineligibility_reason(candidate, policy)
        if reason is None:
            eligible.append(candidate)
        else:
            excluded.append((candidate, reason))

    excluded.sort(key=lambda item: rank_key(item[0], policy))
    logs.extend(_transition_logs(state, excluded))

    # In-flight ownership: a slot whose candidate is no longer eligible is
    # released immediately. The queue must never wait out a timeout for a PR
    # that is closed, merged, draft, or paused.
    lock = _open_lock(state)
    released_lock: Optional[ReviewRequest] = None
    release_reason = ""
    if lock is not None:
        owner = state.by_number(lock.pr_number)
        if owner is None:
            release_reason = "the pull request is no longer visible"
        else:
            owner_reason = ineligibility_reason(owner, policy)
            if owner_reason is not None:
                release_reason = owner_reason
            elif owner.head_sha != lock.head_sha:
                release_reason = f"head moved to {owner.head_sha[:7] or 'unknown'}"
            elif lock.age_ms(now_ms) > int(policy.in_flight_timeout_ms):
                release_reason = (
                    f"no provider response for {format_duration(lock.age_ms(now_ms))}"
                )
        if release_reason:
            logs.append(
                f"Released stale review slot for PR #{lock.pr_number} ({release_reason})."
            )
            released_lock = lock
            lock = None

    ranked = sorted(eligible, key=lambda item: rank_key(item, policy))
    # The locked candidate is already being reviewed; it must not be selected
    # again, and it must not starve the rest of the queue by ranking first.
    if lock is not None:
        ranked = [item for item in ranked if item.pr_number != lock.pr_number]

    if ranked:
        head = ranked[0]
        logs.append(
            f"Next eligible review candidate: PR #{head.pr_number} ({head.priority})."
        )
    else:
        summary = _summary_reasons(excluded)
        logs.append(
            "No eligible review candidate in the queue."
            + (f" ({summary})." if summary else "")
        )

    cooldown = state.cooldown
    if lock is not None:
        logs.append(
            f"Review slot is in flight for {lock.identity()}; "
            "no second review request will be sent."
        )
        return Plan(
            action=ACTION_WAIT,
            wake=state.wake,
            now_ms=now_ms,
            selected=None,
            queue=tuple(ranked),
            excluded=tuple((item.pr_number, reason) for item, reason in excluded),
            held_lock=lock,
            released_lock=released_lock,
            release_reason=release_reason,
            cooldown=cooldown,
            logs=tuple(logs),
        )

    if not ranked:
        return Plan(
            action=ACTION_IDLE,
            wake=state.wake,
            now_ms=now_ms,
            selected=None,
            queue=(),
            excluded=tuple((item.pr_number, reason) for item, reason in excluded),
            released_lock=released_lock,
            release_reason=release_reason,
            cooldown=cooldown,
            logs=tuple(logs),
        )

    selected = ranked[0]
    # Shared provider quota is global: it delays the top candidate, it never
    # reorders the queue and it never authorizes a second concurrent request.
    not_before = max(int(selected.due_at_ms), int(cooldown.until_ms))
    if not_before > now_ms:
        if cooldown.active(now_ms):
            logs.append(cooldown.describe(now_ms))
        else:
            logs.append(
                f"Next eligible candidate PR #{selected.pr_number} is not due for "
                f"{format_duration(not_before - now_ms)} ({selected.review} review)."
            )
        return Plan(
            action=ACTION_WAIT,
            wake=state.wake,
            now_ms=now_ms,
            selected=None,
            queue=tuple(ranked),
            excluded=tuple((item.pr_number, reason) for item, reason in excluded),
            released_lock=released_lock,
            release_reason=release_reason,
            cooldown=cooldown,
            logs=tuple(logs),
        )

    logs.append(f"Next eligible candidate PR #{selected.pr_number} is ready.")
    logs.append("Dispatching one review request.")
    return Plan(
        action=ACTION_DISPATCH,
        wake=state.wake,
        now_ms=now_ms,
        selected=selected,
        queue=tuple(ranked),
        excluded=tuple((item.pr_number, reason) for item, reason in excluded),
        released_lock=released_lock,
        release_reason=release_reason,
        cooldown=cooldown,
        logs=tuple(logs),
    )
