"""Pure state-derived admission adapters for repository-wide reconciliation.

Phase A (continuum#343) adds adapters, not controllers.  Every function here
reads a trusted *state* mapping and returns a typed decision.  None of them
accepts or fabricates ``github.event``: an event is only a wake-up hint, never
decision evidence.  The functions perform no I/O, dispatch nothing, write
nothing, and never mutate their inputs, so they can later be called by a
repository-wide dry-run without changing current wake-up ownership.

Canonical policy is not duplicated here:

- issue admission delegates to :mod:`continuum.scheduler_reconcile`;
- lifecycle retries delegate to :mod:`continuum.lifecycle_recovery`;
- merge gates delegate to :mod:`continuum.merge_lifecycle`;
- provider semantics delegate to :mod:`continuum.coderabbit_adapter` and
  :mod:`continuum.pr_agent_lifecycle`;
- conflict repair delegates to :mod:`continuum.conflict_repair`;
- qualification and delegation delegate to their canonical modules.

Decision kinds are exactly ``ACTION``, ``WAIT``, ``RETRY`` and ``TERMINAL``.
Only a future phase may execute an ``ACTION``; this phase's planner is
read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import coderabbit_adapter
from . import conflict_repair
from . import delegation_discovery
from . import lifecycle_recovery
from . import merge_lifecycle
from . import pr_agent_lifecycle
from . import qualification
from .delegation_priority import effective_priority
from .lifecycle_recovery import (
    RetryEvidence,
    decide_recovery,
    next_retry_delay_seconds,
    resolve_max_executions,
)
from .scheduler_reconcile import (
    CommentRecord,
    IssueState,
    SchedulerConfig,
    _is_owner_command,
    _normalize_terminal_stopped,
    reconcile,
    terminal_stop_skip_reason,
)


ACTION = "ACTION"
WAIT = "WAIT"
RETRY = "RETRY"
TERMINAL = "TERMINAL"
DECISION_KINDS = (ACTION, WAIT, RETRY, TERMINAL)


class AdapterError(ValueError):
    """Raised when trusted state cannot be interpreted safely."""


@dataclass(frozen=True)
class ReconciliationDecision:
    """One typed, side-effect-free decision for a single domain subject."""

    kind: str
    domain: str
    owner: str
    subject: str
    action: str
    reason: str
    retry_after_seconds: Optional[int] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise AdapterError("unknown decision kind: {}".format(self.kind))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "domain": self.domain,
            "owner": self.owner,
            "subject": self.subject,
            "action": self.action,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "details": dict(self.details),
        }


def _decision(
    kind: str,
    domain: str,
    owner: str,
    subject: str,
    action: str,
    reason: str,
    *,
    retry_after_seconds: Optional[int] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> ReconciliationDecision:
    return ReconciliationDecision(
        kind=kind,
        domain=domain,
        owner=owner,
        subject=subject,
        action=action,
        reason=reason,
        retry_after_seconds=retry_after_seconds,
        details=dict(details or {}),
    )


def _integer(value: Any, field_name: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise AdapterError("{} must be an integer".format(field_name)) from exc
    if number <= 0:
        raise AdapterError("{} must be positive".format(field_name))
    return number


def _labels(value: Any) -> Tuple[str, ...]:
    names: List[str] = []
    for label in value or ():
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, Mapping):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(sorted(set(names)))


def _state(value: Any) -> str:
    return str(value or "").strip().lower()


def _pr_number(pr: Mapping[str, Any]) -> int:
    return _integer(pr.get("number") or pr.get("pull_number"), "pull request number")


def _pr_subject(pr: Mapping[str, Any]) -> str:
    return "pull_request:{}".format(_pr_number(pr))


def _head_sha(pr: Mapping[str, Any]) -> str:
    return str(pr.get("head_sha") or pr.get("headRefOid") or "").strip().lower()


def _is_open(subject: Mapping[str, Any]) -> bool:
    state = _state(subject.get("state"))
    return state in ("open", "opened") or (not state and not subject.get("merged"))


def _is_draft(pr: Mapping[str, Any]) -> bool:
    if "draft" in pr:
        return bool(pr.get("draft"))
    return bool(pr.get("is_draft"))


def _run_state(run: Any) -> Tuple[str, str]:
    if not isinstance(run, Mapping):
        return ("missing", "missing")
    return (
        _state(run.get("status") or "missing"),
        _state(run.get("conclusion") or "pending"),
    )


def _comment_records(
    raw_comments: Sequence[Any], repository_owner: str = ""
) -> List[CommentRecord]:
    records: List[CommentRecord] = []
    normalized_owner = str(repository_owner or "").strip().casefold()
    owner_known = bool(normalized_owner)
    for raw in raw_comments or ():
        if isinstance(raw, CommentRecord):
            records.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        user = raw.get("user") if isinstance(raw.get("user"), Mapping) else {}
        author_login = str(user.get("login") or "")
        normalized_login = author_login.strip().casefold()
        association = str(raw.get("author_association") or "").strip().upper()
        is_scheduler_bot = normalized_login == "github-actions[bot]"
        if owner_known:
            login_is_owner = normalized_login == normalized_owner
        else:
            # The snapshot omitted its repository, so the owner login is
            # genuinely unknown.  Fall back to GitHub's own association bit
            # instead of silently dropping the owner ``/oc`` race guard:
            # only the exact ``OWNER`` association asserts repository
            # ownership, while MEMBER/COLLABORATOR remain non-owner actors.
            login_is_owner = association == "OWNER"
        author_is_owner = bool(
            raw.get("author_is_owner") or login_is_owner or is_scheduler_bot
        )
        author_is_trusted = bool(
            raw.get("author_is_trusted")
            or author_is_owner
            or association in ("OWNER", "MEMBER", "COLLABORATOR")
        )
        created = raw.get("created_at")
        if isinstance(created, datetime):
            when = created
        else:
            text = str(created or raw.get("updated_at") or "").replace("Z", "+00:00")
            try:
                when = datetime.fromisoformat(text)
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        records.append(
            CommentRecord(
                body=str(raw.get("body") or ""),
                author_is_owner=author_is_owner,
                created_at=when,
                author_login=author_login,
                author_is_trusted=author_is_trusted,
            )
        )
    return records


def _issue_records(issues: Sequence[Mapping[str, Any]]) -> Dict[int, IssueState]:
    records: Dict[int, IssueState] = {}
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise AdapterError("issue state must be a mapping")
        number = _integer(issue.get("number"), "issue number")
        records[number] = IssueState(
            number=number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or ""),
            labels=set(_labels(issue.get("labels"))),
            is_open=_state(issue.get("state") or "open") == "open",
        )
    return records


def _comments_by_number(
    issues: Sequence[Mapping[str, Any]],
    comments: Optional[Mapping[Any, Sequence[Any]]],
    repository_owner: str = "",
) -> Dict[int, List[CommentRecord]]:
    by_number: Dict[int, List[CommentRecord]] = {}
    if comments:
        for key, value in comments.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            by_number[number] = _comment_records(value, repository_owner)
    for issue in issues:
        embedded = issue.get("comments") if isinstance(issue, Mapping) else None
        if embedded and isinstance(issue.get("number"), (int, str)):
            number = int(issue["number"])
            by_number.setdefault(number, _comment_records(embedded, repository_owner))
    return by_number


def _issue_decision(
    issue: IssueState,
    number: int,
    result: Any,
    stopped: set,
    generation: int,
    *,
    now: datetime,
) -> ReconciliationDecision:
    subject = "issue:{}".format(number)
    domain = "issue-admission"
    owner = "issue-scheduler"
    in_progress = False
    for comment in getattr(result, "dispatched", ()) or ():
        if int(comment) == number:
            in_progress = True
    if in_progress:
        priority, _rank = effective_priority(sorted(issue.labels), issue.title)
        return _decision(
            ACTION,
            domain,
            owner,
            subject,
            "dispatch_issue",
            "eligible issue admitted within WIP capacity",
            details={"priority": priority, "generation": generation},
        )
    if not issue.is_open:
        return _decision(
            TERMINAL,
            domain,
            owner,
            subject,
            "none",
            "issue is closed",
        )
    if (number, generation) in stopped:
        return _decision(
            TERMINAL,
            domain,
            owner,
            subject,
            "none",
            terminal_stop_skip_reason(number, generation),
            details={"generation": generation},
        )
    reason = (getattr(result, "skip_reasons", {}) or {}).get(number)
    if not reason:
        reason = "no admission decision was produced"
    lowered = reason.lower()
    details: Dict[str, Any] = {"generation": generation}
    if getattr(result, "failed", False):
        details["no_progress"] = True
        details["pass_message"] = str(getattr(result, "failed_message", "") or "")
    if "automation is paused" in lowered:
        return _decision(TERMINAL, domain, owner, subject, "none", reason, details=details)
    if (
        "lease expired" in lowered
        or "stale reservation" in lowered
        or "automatic retry" in lowered
        or "eligible for a retry" in lowered
    ):
        return _decision(RETRY, domain, owner, subject, "retry_issue_admission", reason, details=details)
    return _decision(WAIT, domain, owner, subject, "none", reason, details=details)


def plan_issue_admissions(
    issues: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    config: Optional[SchedulerConfig] = None,
    comments: Optional[Mapping[Any, Sequence[Any]]] = None,
    open_pr_issue_numbers: Sequence[int] = (),
    active_run_issue_numbers: Sequence[int] = (),
    declared_open_blockers: Optional[Mapping[int, Sequence[int]]] = None,
    native_open_blockers: Optional[Mapping[int, Sequence[int]]] = None,
    qualification_trackers: Sequence[int] = (),
    terminal_stopped: Sequence[Any] = (),
    generations: Optional[Mapping[int, int]] = None,
    repository_owner: str = "",
) -> List[ReconciliationDecision]:
    """Derive all issue admission decisions from trusted repository state.

    The scheduler is invoked on *copies* so label/comment settlement never
    leaks into the caller's snapshot.  This is the event-independent form of
    the cron reconcile pass: it accepts no ``github.event`` payload and an
    unrelated wake-up cannot change the result.
    """

    records = _issue_records(issues)
    comments_by_number = _comments_by_number(issues, comments, repository_owner)
    stopped = _normalize_terminal_stopped(list(terminal_stopped or ()))
    generations = {int(key): int(value) for key, value in (generations or {}).items()}
    result = reconcile(
        now,
        records,
        {int(number) for number in open_pr_issue_numbers},
        {int(number) for number in active_run_issue_numbers},
        {number: list(values) for number, values in comments_by_number.items()},
        {
            int(number): [int(item) for item in values]
            for number, values in (declared_open_blockers or {}).items()
        },
        {
            int(number): [int(item) for item in values]
            for number, values in (native_open_blockers or {}).items()
        },
        {int(number) for number in qualification_trackers},
        config or SchedulerConfig(),
        dispatch_hook=None,
        terminal_stopped=set(stopped),
        generations=generations,
        repository_owner=repository_owner,
    )
    decisions: List[ReconciliationDecision] = []
    for number in sorted(records):
        decisions.append(
            _issue_decision(
                records[number],
                number,
                result,
                stopped,
                generations.get(number, 1),
                now=now,
            )
        )
    return decisions


def decide_issue_admission(
    issue: Mapping[str, Any],
    *,
    now: datetime,
    repository_issues: Optional[Sequence[Mapping[str, Any]]] = None,
    **kwargs: Any,
) -> ReconciliationDecision:
    """Return one issue's admission decision using the full WIP context."""

    number = _integer(issue.get("number"), "issue number")
    snapshot = list(repository_issues) if repository_issues is not None else [issue]
    if not any(int(item.get("number", 0)) == number for item in snapshot):
        snapshot.append(issue)
    decisions = plan_issue_admissions(snapshot, now=now, **kwargs)
    for decision in decisions:
        if decision.subject == "issue:{}".format(number):
            return decision
    raise AdapterError("issue decision was not produced")


def decide_review_ready(
    pr: Mapping[str, Any],
    *,
    provider: str = "none",
    ci_run: Any = None,
    contract_gate_run: Any = None,
    contract_qualification_run: Any = None,
    packaging_run: Any = None,
    required_gate_label: str = "",
    required_gate_name: str = "",
    required_gate_run: Any = None,
    repository: str = "",
    delegated_skipped_ci: bool = False,
) -> ReconciliationDecision:
    """Derive whether the exact-HEAD PR is ready to hand to review/merge."""

    subject = _pr_subject(pr)
    domain = "review-ready"
    owner = "review-ready"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    if _is_draft(pr):
        return _decision(WAIT, domain, owner, subject, "none", "draft pull request is not review-ready")
    if not _head_sha(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "PR HEAD is missing")
    labels = _labels(pr.get("labels"))
    if "review-ready" in labels or "coderabbit-review-requested" in labels:
        return _decision(WAIT, domain, owner, subject, "none", "review readiness is already recorded for this state")
    ok, reason = merge_lifecycle.evaluate_current_head_gates(
        ci_run=ci_run,
        contract_gate_run=contract_gate_run,
        contract_qualification_run=contract_qualification_run,
        packaging_run=packaging_run,
        labels=labels,
        required_gate_label=required_gate_label,
        required_gate_name=required_gate_name,
        required_gate_run=required_gate_run,
        repository=repository,
        delegated_skipped_ci=delegated_skipped_ci,
    )
    if not ok:
        return _decision(WAIT, domain, owner, subject, "none", reason)
    selected = merge_lifecycle.resolve_review_provider(provider)
    if selected == "none":
        return _decision(ACTION, domain, owner, subject, "wake_auto_merge", "exact HEAD is ready; no review provider selected")
    return _decision(
        ACTION,
        domain,
        owner,
        subject,
        "request_{}_review".format(selected.replace("-", "_")),
        "exact HEAD is ready for {}".format(selected),
        details={"provider": selected},
    )


def decide_ci_repair(
    pr: Mapping[str, Any],
    *,
    ci_run: Any = None,
    active_repair_run: bool = False,
    conflict_lock: bool = False,
    dispatches_for_head: int = 0,
    conflict_marker_age_seconds: Optional[int] = None,
    retry_evidence: RetryEvidence = RetryEvidence(),
    max_executions: int = lifecycle_recovery.MAX_TRANSIENT_ATTEMPTS,
) -> ReconciliationDecision:
    """Derive conflict/CI repair admission without an event payload."""

    subject = _pr_subject(pr)
    domain = "ci-repair"
    owner = "ci-repair"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    if not _head_sha(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "PR HEAD is missing")

    conflict = conflict_repair.decide_conflict_repair(
        mergeable=pr.get("mergeable"),
        mergeable_state=pr.get("mergeable_state"),
        has_lock=conflict_lock,
        active_repair_run=active_repair_run,
        dispatches_for_head=dispatches_for_head,
        marker_age_seconds=conflict_marker_age_seconds,
    )
    if conflict.action == "dispatch":
        return _decision(
            ACTION,
            domain,
            owner,
            subject,
            "repair_conflict",
            conflict.reason,
            details={"attempt": conflict.attempt},
        )
    if conflict.action in ("wait", "resume", "hold"):
        return _decision(WAIT, domain, owner, subject, "none", conflict.reason)

    status, conclusion = _run_state(ci_run)
    if status != "completed":
        return _decision(WAIT, domain, owner, subject, "none", "exact-HEAD CI is not complete")
    if conclusion in ("success", "skipped"):
        return _decision(TERMINAL, domain, owner, subject, "none", "exact-HEAD CI needs no repair")
    if active_repair_run:
        return _decision(WAIT, domain, owner, subject, "none", "a matching repair run is already active")
    budget = resolve_max_executions(max_executions)
    attempt = (
        0
        if retry_evidence.latest_attempt is None
        else int(retry_evidence.latest_attempt) + 1
    )
    if retry_evidence.exhausted or attempt >= budget:
        return _decision(TERMINAL, domain, owner, subject, "none", "bounded CI-repair budget is exhausted")
    return _decision(
        ACTION,
        domain,
        owner,
        subject,
        "repair_exact_head_ci",
        "exact-HEAD CI is {}/{}".format(status, conclusion),
        details={"attempt": attempt},
    )


def _timestamp_ms(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def decide_coderabbit_queue(
    pr: Mapping[str, Any],
    *,
    comments: Sequence[Mapping[str, Any]] = (),
    now_ms: int,
    provider: str = "coderabbit",
    review_requested: bool = False,
    default_limited_at_ms: Optional[int] = None,
) -> ReconciliationDecision:
    """Derive the CodeRabbit queue slot from trusted quota/PR state."""

    subject = _pr_subject(pr)
    domain = "coderabbit-queue"
    owner = "coderabbit-queue"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    if str(provider or "none").strip().lower() != "coderabbit":
        return _decision(TERMINAL, domain, owner, subject, "none", "CodeRabbit is not the selected review provider")
    if review_requested:
        return _decision(WAIT, domain, owner, subject, "none", "a review request already exists for this state")
    latest = coderabbit_adapter.latest_rate_limit(comments)
    if latest is not None:
        comment = latest.get("comment") or {}
        limited_at = _timestamp_ms(comment.get("updated_at") or comment.get("created_at"))
        if limited_at is None:
            limited_at = default_limited_at_ms
        if limited_at is None:
            return _decision(
                WAIT,
                domain,
                owner,
                subject,
                "none",
                "CodeRabbit quota is limited but its reset clock is unknown",
            )
        due = coderabbit_adapter.retry_due_at_ms(limited_at, int(latest["delay_ms"]))
        if int(now_ms) < due:
            return _decision(
                RETRY,
                domain,
                owner,
                subject,
                "request_coderabbit_review",
                "CodeRabbit quota cooldown has not elapsed",
                retry_after_seconds=max(0, int((due - int(now_ms) + 999) // 1000)),
                details={"due_at_ms": due},
            )
    return _decision(ACTION, domain, owner, subject, "request_coderabbit_review", "CodeRabbit queue slot is available")


def decide_coderabbit_unresolved(
    pr: Mapping[str, Any],
    *,
    review_comment: Optional[Mapping[str, Any]] = None,
    verified_head: str = "",
    provider: str = "coderabbit",
    active_repair: bool = False,
) -> ReconciliationDecision:
    """Derive unresolved-finding repair admission from PR/comment state."""

    subject = _pr_subject(pr)
    domain = "coderabbit-unresolved"
    owner = "coderabbit-unresolved"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    if str(provider or "none").strip().lower() != "coderabbit":
        return _decision(TERMINAL, domain, owner, subject, "none", "CodeRabbit is not the selected review provider")
    if not review_comment:
        return _decision(WAIT, domain, owner, subject, "none", "no unresolved CodeRabbit reply is recorded")
    user = review_comment.get("user") if isinstance(review_comment.get("user"), Mapping) else {}
    login = str(user.get("login") or review_comment.get("login") or "").lower()
    body = str(review_comment.get("body") or "")
    if login != "coderabbitai[bot]" or "**UNRESOLVED**" not in body.upper():
        return _decision(TERMINAL, domain, owner, subject, "none", "comment is not a CodeRabbit UNRESOLVED reply")
    if review_comment.get("in_reply_to_id") in (None, ""):
        return _decision(TERMINAL, domain, owner, subject, "none", "reply is not attached to a finding")
    head = _head_sha(pr)
    if not verified_head:
        return _decision(WAIT, domain, owner, subject, "none", "no verification request matches the finding")
    if verified_head.strip().lower() != head:
        return _decision(WAIT, domain, owner, subject, "none", "verification request belongs to another HEAD")
    if active_repair:
        return _decision(WAIT, domain, owner, subject, "none", "a matching repair is already active")
    return _decision(ACTION, domain, owner, subject, "dispatch_unresolved_repair", "current-HEAD UNRESOLVED finding needs one repair")


def decide_pr_agent_review(
    pr: Mapping[str, Any],
    *,
    ci_green_on_exact_head: bool,
    existing_review_head: str = "",
    active_review: bool = False,
    provider: str = "pr-agent",
) -> ReconciliationDecision:
    """Derive PR-Agent review admission from PR and CI state."""

    subject = _pr_subject(pr)
    domain = "pr-agent-review"
    owner = "pr-agent-review"
    if str(provider or "none").strip().lower() != "pr-agent":
        return _decision(TERMINAL, domain, owner, subject, "none", "PR-Agent is not the selected review provider")
    state = _state(pr.get("state") or "open")
    head = _head_sha(pr)
    if state != "open":
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    if not head:
        return _decision(TERMINAL, domain, owner, subject, "none", "PR HEAD is missing")
    if existing_review_head and existing_review_head.strip().lower() == head:
        return _decision(TERMINAL, domain, owner, subject, "none", "PR-Agent already reviewed this exact HEAD")
    if active_review:
        return _decision(WAIT, domain, owner, subject, "none", "a matching PR-Agent review is already active")
    admission = pr_agent_lifecycle.admission_allowed(
        pr_state=state,
        is_draft=_is_draft(pr),
        head_repo=str(pr.get("head_repo") or ""),
        base_repo=str(pr.get("base_repo") or ""),
        ci_green_on_exact_head=bool(ci_green_on_exact_head),
    )
    if not admission["allowed"]:
        return _decision(WAIT, domain, owner, subject, "none", admission["reason"])
    return _decision(ACTION, domain, owner, subject, "dispatch_pr_agent_review", admission["reason"])


def _recovery_decision(
    decision: Any,
    *,
    domain: str,
    owner: str,
    subject: str,
    now_epoch: Optional[int],
) -> ReconciliationDecision:
    action = str(getattr(decision, "action", "") or "")
    reason = str(getattr(decision, "reason", "") or "")
    not_before = getattr(decision, "not_before_epoch", None)
    if action == "dispatch":
        if getattr(decision, "defer_dispatch", False):
            delay = (
                max(0, int(not_before) - int(now_epoch))
                if not_before is not None and now_epoch is not None
                else None
            )
            return _decision(
                RETRY,
                domain,
                owner,
                subject,
                "redispatch_after_durable_wait",
                reason,
                retry_after_seconds=delay,
                details={"attempt": getattr(decision, "attempt", None), "not_before_epoch": not_before},
            )
        return _decision(
            ACTION,
            domain,
            owner,
            subject,
            "recover_operation",
            reason,
            details={"attempt": getattr(decision, "attempt", None), "not_before_epoch": not_before},
        )
    if action == "wait":
        lowered = reason.lower()
        if not_before is not None or any(
            token in lowered
            for token in (
                "not-before",
                "backoff",
                "reset-aware",
                "requires a clock",
                "dispatch grace",
            )
        ):
            delay = (
                max(0, int(not_before) - int(now_epoch))
                if not_before is not None and now_epoch is not None
                else None
            )
            return _decision(
                RETRY,
                domain,
                owner,
                subject,
                "redispatch_after_durable_wait",
                reason,
                retry_after_seconds=delay,
                details={"not_before_epoch": not_before},
            )
        return _decision(WAIT, domain, owner, subject, "none", reason)
    if action in ("hold", "exhaust", "settled"):
        return _decision(TERMINAL, domain, owner, subject, "none", reason)
    return _decision(WAIT, domain, owner, subject, "none", reason or "recovery produced no action")


def decide_pr_agent_recovery(
    pr: Mapping[str, Any],
    *,
    kind: str = "review",
    ci_green: bool = True,
    operation_state: Optional[str] = None,
    operation_description: str = "",
    operation_context: Optional[str] = None,
    failure_transient: Optional[bool] = None,
    head_moved: bool = False,
    malformed_state: bool = False,
    merge_blocked_deterministic: bool = False,
    active_exact_lease: bool = False,
    active_operation: bool = False,
    run_conclusion: Optional[str] = None,
    status_age_seconds: Optional[int] = None,
    main_sync_required: bool = False,
    evidence: RetryEvidence = RetryEvidence(),
    marker_newer_than_status: bool = False,
    marker_age_seconds: Optional[int] = None,
    not_before_epoch: Optional[int] = None,
    now_epoch: Optional[int] = None,
    retry_after_seconds: Optional[int] = None,
    ratelimit_reset_epoch: Optional[int] = None,
    provider_reset_epoch: Optional[int] = None,
    max_executions: int = lifecycle_recovery.MAX_TRANSIENT_ATTEMPTS,
) -> ReconciliationDecision:
    """Derive PR-Agent review/repair recovery from exact-HEAD state."""

    subject = _pr_subject(pr)
    domain = "pr-agent-recovery"
    owner = "pr-agent-recovery"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    head = _head_sha(pr)
    if not head:
        return _decision(TERMINAL, domain, owner, subject, "none", "PR HEAD is missing")
    decision = decide_recovery(
        ci_green=ci_green,
        operation_state=operation_state,
        operation_description=operation_description,
        operation_context=operation_context,
        failure_transient=failure_transient,
        head_moved=head_moved,
        malformed_state=malformed_state,
        merge_blocked_deterministic=merge_blocked_deterministic,
        active_exact_lease=active_exact_lease,
        active_operation=active_operation,
        run_conclusion=run_conclusion,
        status_age_seconds=status_age_seconds,
        main_sync_required=main_sync_required,
        evidence=evidence,
        marker_newer_than_status=marker_newer_than_status,
        marker_age_seconds=marker_age_seconds,
        not_before_epoch=not_before_epoch,
        now_epoch=now_epoch,
        retry_after_seconds=retry_after_seconds,
        ratelimit_reset_epoch=ratelimit_reset_epoch,
        provider_reset_epoch=provider_reset_epoch,
        max_executions=max_executions,
        operation_key_value="{}-{}".format(head, kind),
    )
    return _recovery_decision(
        decision,
        domain=domain,
        owner=owner,
        subject=subject,
        now_epoch=now_epoch,
    )


def decide_opencode_recovery(
    subject_state: Mapping[str, Any],
    *,
    active_run: bool = False,
    operation_state: Optional[str] = None,
    evidence: RetryEvidence = RetryEvidence(),
    now_epoch: Optional[int] = None,
    max_executions: int = lifecycle_recovery.MAX_TRANSIENT_ATTEMPTS,
) -> ReconciliationDecision:
    """Derive lost/failed OpenCode run recovery from durable state."""

    kind = str(subject_state.get("kind") or "issue").strip().lower()
    number = _integer(subject_state.get("number"), "subject number")
    subject = "{}:{}".format(kind, number)
    domain = "opencode-recovery"
    owner = "opencode-watchdog"
    if not _is_open(subject_state):
        return _decision(TERMINAL, domain, owner, subject, "none", "subject is not open")
    if active_run:
        return _decision(WAIT, domain, owner, subject, "none", "a matching OpenCode run is already active")
    state = _state(operation_state)
    if state == "success":
        return _decision(TERMINAL, domain, owner, subject, "none", "OpenCode run already succeeded")
    if evidence.exhausted:
        return _decision(TERMINAL, domain, owner, subject, "none", "OpenCode recovery budget is exhausted")
    attempt = 0 if evidence.latest_attempt is None else int(evidence.latest_attempt) + 1
    budget = resolve_max_executions(max_executions)
    if attempt >= budget:
        return _decision(TERMINAL, domain, owner, subject, "none", "OpenCode recovery budget is exhausted")
    if state in ("pending", "queued", "in_progress"):
        return _decision(WAIT, domain, owner, subject, "none", "OpenCode run is still in flight")
    delay = next_retry_delay_seconds(
        attempt=attempt,
        operation_key_value=subject,
        retry_after_seconds=None,
    )
    if now_epoch is None and attempt > 0:
        return _decision(
            RETRY,
            domain,
            owner,
            subject,
            "recover_opencode_run",
            "OpenCode recovery requires a clock for durable backoff",
            details={"attempt": attempt},
        )
    return _decision(
        ACTION if attempt == 0 else RETRY,
        domain,
        owner,
        subject,
        "recover_opencode_run",
        "lost or failed OpenCode run is recoverable" if state else "no successful OpenCode run is recorded",
        retry_after_seconds=None if attempt == 0 else delay,
        details={"attempt": attempt},
    )


def decide_pr_agent_merge(
    pr: Mapping[str, Any],
    *,
    ci_run: Any = None,
    contract_gate_run: Any = None,
    contract_qualification_run: Any = None,
    packaging_run: Any = None,
    required_gate_label: str = "",
    required_gate_name: str = "",
    required_gate_run: Any = None,
    repository: str = "",
    delegated_skipped_ci: bool = False,
    reviewed_head: str = "",
    merge_title: str = "",
) -> ReconciliationDecision:
    """Derive the provider-neutral exact-HEAD merge decision."""

    subject = _pr_subject(pr)
    domain = "pr-agent-merge"
    owner = "pr-agent-merge"
    if not _is_open(pr):
        return _decision(TERMINAL, domain, owner, subject, "none", "pull request is not open")
    labels = _labels(pr.get("labels"))
    if merge_lifecycle.is_merge_blocked(labels):
        return _decision(TERMINAL, domain, owner, subject, "none", "no-auto-merge is present")
    live_head = _head_sha(pr)
    expected = str(reviewed_head or "").strip().lower()
    if not live_head:
        return _decision(TERMINAL, domain, owner, subject, "none", "live HEAD is missing")
    if not expected:
        return _decision(
            WAIT,
            domain,
            owner,
            subject,
            "none",
            "no reviewed HEAD is attested for this PR",
        )
    ok, reason = merge_lifecycle.evaluate_current_head_gates(
        ci_run=ci_run,
        contract_gate_run=contract_gate_run,
        contract_qualification_run=contract_qualification_run,
        packaging_run=packaging_run,
        labels=labels,
        required_gate_label=required_gate_label,
        required_gate_name=required_gate_name,
        required_gate_run=required_gate_run,
        repository=repository,
        delegated_skipped_ci=delegated_skipped_ci,
    )
    if not ok:
        return _decision(WAIT, domain, owner, subject, "none", reason)
    try:
        params = merge_lifecycle.build_merge_params(
            reviewed_head=expected,
            live_head=live_head,
            title=merge_title or str(pr.get("title") or "Merge pull request"),
        )
    except merge_lifecycle.MergeLifecycleError as exc:
        return _decision(WAIT, domain, owner, subject, "none", str(exc))
    return _decision(
        ACTION,
        domain,
        owner,
        subject,
        "merge_exact_head",
        "all exact-HEAD gates are green",
        details={"sha": params["sha"]},
    )


def decide_qualification(
    *,
    subject: str,
    qualification_open: bool,
    qualification_paused: bool,
    capability_state: str,
    already_dispatched_for_sha: bool,
    required_sha: Any,
    evidence_state: str = "unknown",
    active_qualification: bool = False,
) -> ReconciliationDecision:
    """Derive mandatory qualification dispatch/retry from trusted state."""

    domain = "qualification"
    owner = "qualification"
    if active_qualification:
        return _decision(WAIT, domain, owner, subject, "none", "a qualifying run is already active")
    dispatch, reason = qualification.should_dispatch_qualification(
        qualification_open=qualification_open,
        qualification_paused=qualification_paused,
        capability_state=capability_state,
        already_dispatched_for_sha=already_dispatched_for_sha,
        required_sha=required_sha,
    )
    if dispatch:
        return _decision(
            ACTION,
            domain,
            owner,
            subject,
            "dispatch_qualification",
            reason,
            details={"required_sha": str(required_sha or "")},
        )
    retry, retry_reason = qualification.qualification_needs_retry(
        evidence_state=evidence_state,
        qualification_open=qualification_open,
        capability_state=capability_state,
    )
    if retry:
        return _decision(RETRY, domain, owner, subject, "retry_qualification", retry_reason)
    return _decision(TERMINAL, domain, owner, subject, "none", reason)


def decide_delegation(
    *,
    subject: str,
    delegation_state: str,
    children: Sequence[Mapping[str, Any]] = (),
    task_open: bool = True,
    task_blocked: bool = False,
    task_active: bool = False,
    child_verified: bool = True,
    child_id: str = "",
) -> ReconciliationDecision:
    """Derive one parent/child dispatch decision; child ids stay opaque."""

    domain = "delegation"
    owner = "delegation"
    inventory = delegation_discovery.resolve_child_inventory(delegation_state, children)
    if not inventory.enabled:
        return _decision(
            TERMINAL if not inventory.degraded else RETRY,
            domain,
            owner,
            subject,
            "none" if not inventory.degraded else "retry_child_discovery",
            inventory.reason,
            details={"child_id": str(child_id or "")},
        )
    if not task_open:
        return _decision(TERMINAL, domain, owner, subject, "none", "child task is not open", details={"child_id": str(child_id or "")})
    if not child_verified:
        return _decision(TERMINAL, domain, owner, subject, "none", "child relationship is not verified", details={"child_id": str(child_id or "")})
    if task_blocked:
        return _decision(WAIT, domain, owner, subject, "none", "child task has an open dependency", details={"child_id": str(child_id or "")})
    if task_active:
        return _decision(WAIT, domain, owner, subject, "none", "a matching child run is already active", details={"child_id": str(child_id or "")})
    return _decision(
        ACTION,
        domain,
        owner,
        subject,
        "dispatch_child_task",
        "child task is eligible for delegated execution",
        details={"child_id": str(child_id or "")},
    )


def decide_global_watchdog(
    *,
    subject: str,
    consumer_verified: bool,
    open_pull_requests: int,
    rate_limit_remaining: Optional[int] = None,
    rate_limit_reset_epoch: Optional[int] = None,
    now_epoch: Optional[int] = None,
) -> ReconciliationDecision:
    """Derive whether a verified consumer needs a liveness wake-up."""

    domain = "global-watchdog"
    owner = "global-watchdog"
    if not consumer_verified:
        return _decision(TERMINAL, domain, owner, subject, "none", "consumer provenance is not verified")
    if int(open_pull_requests) <= 0:
        return _decision(WAIT, domain, owner, subject, "none", "consumer has no open pull requests")
    if rate_limit_remaining is not None and int(rate_limit_remaining) <= 0:
        delay = None
        if rate_limit_reset_epoch is not None and now_epoch is not None:
            delay = max(0, int(rate_limit_reset_epoch) - int(now_epoch))
        return _decision(
            RETRY,
            domain,
            owner,
            subject,
            "wake_consumer",
            "GitHub rate limit is exhausted",
            retry_after_seconds=delay,
        )
    return _decision(ACTION, domain, owner, subject, "wake_consumer", "verified consumer has open pull requests")


ADAPTERS = {
    "issue-admission": plan_issue_admissions,
    "review-ready": decide_review_ready,
    "ci-repair": decide_ci_repair,
    "coderabbit-queue": decide_coderabbit_queue,
    "coderabbit-unresolved": decide_coderabbit_unresolved,
    "pr-agent-review": decide_pr_agent_review,
    "pr-agent-recovery": decide_pr_agent_recovery,
    "opencode-recovery": decide_opencode_recovery,
    "pr-agent-merge": decide_pr_agent_merge,
    "qualification": decide_qualification,
    "delegation": decide_delegation,
    "global-watchdog": decide_global_watchdog,
}


__all__ = [
    "ACTION",
    "WAIT",
    "RETRY",
    "TERMINAL",
    "DECISION_KINDS",
    "AdapterError",
    "ReconciliationDecision",
    "ADAPTERS",
    "plan_issue_admissions",
    "decide_issue_admission",
    "decide_review_ready",
    "decide_ci_repair",
    "decide_coderabbit_queue",
    "decide_coderabbit_unresolved",
    "decide_pr_agent_review",
    "decide_pr_agent_recovery",
    "decide_opencode_recovery",
    "decide_pr_agent_merge",
    "decide_qualification",
    "decide_delegation",
    "decide_global_watchdog",
]
