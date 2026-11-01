"""Read-only repository-wide reconciliation plan and dry-run API (#343).

This is phase A: the planner enumerates actionable issues and pull requests
from trusted state, derives typed decisions with
:mod:`continuum.reconciliation_adapters`, and never dispatches OpenCode,
PR-Agent, a review, a merge, or any other mutation.  It owns no scheduler,
reviewer, or merger of its own.

Pagination is bounded per pass and resumable through an explicit
:class:`Continuation` value that callers persist between passes.  A page source
is read-only by contract: it exposes only ``list_issues`` and
``list_pull_requests``.  There is deliberately no mutation method to call, so
dry-run mode cannot dispatch even when every decision is ``ACTION``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from . import reconciliation_adapters as adapters
from .lifecycle_recovery import RetryEvidence
from .reconciliation_adapters import ReconciliationDecision
from .scheduler_reconcile import SchedulerConfig


DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES_PER_PASS = 10


class ReconciliationPlanError(ValueError):
    """Raised when a plan window cannot be enumerated safely."""


@dataclass(frozen=True)
class StatePage:
    """One bounded page of read-only state."""

    items: Tuple[Mapping[str, Any], ...] = ()
    next_cursor: Optional[str] = None


class ReadOnlyStateSource(Protocol):
    """Minimal read-only state source accepted by the dry-run planner.

    Implementations must not perform writes or workflow dispatches.  The
    protocol intentionally has no mutation methods.
    """

    def list_issues(self, cursor: Optional[str], limit: int) -> StatePage:
        ...

    def list_pull_requests(self, cursor: Optional[str], limit: int) -> StatePage:
        ...


class SnapshotStateSource:
    """Deterministic in-memory source used by fixture traces and tests."""

    def __init__(
        self,
        issues: Sequence[Mapping[str, Any]] = (),
        pull_requests: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._issues = tuple(issues)
        self._pull_requests = tuple(pull_requests)
        self.read_calls: List[Tuple[str, Optional[str], int]] = []

    @staticmethod
    def _page(items: Sequence[Mapping[str, Any]], cursor: Optional[str], limit: int) -> StatePage:
        try:
            offset = 0 if cursor in (None, "") else int(cursor)
        except (TypeError, ValueError) as exc:
            raise ReconciliationPlanError("cursor must be an integer offset") from exc
        if offset < 0:
            raise ReconciliationPlanError("cursor must be non-negative")
        end = min(len(items), offset + int(limit))
        next_cursor = str(end) if end < len(items) else None
        return StatePage(tuple(items[offset:end]), next_cursor)

    def list_issues(self, cursor: Optional[str], limit: int) -> StatePage:
        self.read_calls.append(("issues", cursor, int(limit)))
        return self._page(self._issues, cursor, limit)

    def list_pull_requests(self, cursor: Optional[str], limit: int) -> StatePage:
        self.read_calls.append(("pull_requests", cursor, int(limit)))
        return self._page(self._pull_requests, cursor, limit)


@dataclass(frozen=True)
class Continuation:
    """Persisted pagination state for a bounded repository-wide pass."""

    issue_cursor: Optional[str] = None
    pull_cursor: Optional[str] = None
    issues_exhausted: bool = False
    pull_requests_exhausted: bool = False
    completed_passes: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_cursor": self.issue_cursor,
            "pull_cursor": self.pull_cursor,
            "issues_exhausted": self.issues_exhausted,
            "pull_requests_exhausted": self.pull_requests_exhausted,
            "completed_passes": self.completed_passes,
        }

    @classmethod
    def from_dict(cls, payload: Optional[Mapping[str, Any]]) -> "Continuation":
        if not payload:
            return cls()
        return cls(
            issue_cursor=payload.get("issue_cursor"),
            pull_cursor=payload.get("pull_cursor"),
            issues_exhausted=bool(payload.get("issues_exhausted", False)),
            pull_requests_exhausted=bool(payload.get("pull_requests_exhausted", False)),
            completed_passes=int(payload.get("completed_passes", 0) or 0),
        )

    def restart(self) -> "Continuation":
        """Start a new full enumeration cycle from the beginning."""

        return Continuation(completed_passes=self.completed_passes)


@dataclass(frozen=True)
class PlanContext:
    """Trusted state context for one repository-wide dry-run pass."""

    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    review_provider: str = "none"
    repository: str = ""
    comments_by_issue: Mapping[Any, Sequence[Any]] = field(default_factory=dict)
    comments_by_pull_request: Mapping[Any, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    open_pr_issue_numbers: Sequence[int] = ()
    active_run_issue_numbers: Sequence[int] = ()
    declared_open_blockers: Mapping[int, Sequence[int]] = field(default_factory=dict)
    native_open_blockers: Mapping[int, Sequence[int]] = field(default_factory=dict)
    qualification_trackers: Sequence[int] = ()
    terminal_stopped: Sequence[Any] = ()
    generations: Mapping[int, int] = field(default_factory=dict)
    ci_runs: Mapping[Any, Any] = field(default_factory=dict)
    pull_request_state: Mapping[Any, Mapping[str, Any]] = field(default_factory=dict)
    qualifications: Sequence[Mapping[str, Any]] = ()
    delegations: Sequence[Mapping[str, Any]] = ()
    consumers: Sequence[Mapping[str, Any]] = ()
    event: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class ReconciliationPlan:
    """Result of a read-only plan/dry-run pass."""

    decisions: Tuple[ReconciliationDecision, ...]
    continuation: Continuation
    issues_scanned: int = 0
    pull_requests_scanned: int = 0
    pages_read: int = 0
    dry_run: bool = True

    def by_kind(self, kind: str) -> Tuple[ReconciliationDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.kind == kind)

    def by_subject(self, subject: str) -> Tuple[ReconciliationDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.subject == subject)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "continuation": self.continuation.to_dict(),
            "issues_scanned": self.issues_scanned,
            "pull_requests_scanned": self.pull_requests_scanned,
            "pages_read": self.pages_read,
        }


def _coerce_page(value: Any) -> StatePage:
    if isinstance(value, StatePage):
        return value
    if isinstance(value, Mapping):
        return StatePage(
            items=tuple(value.get("items") or ()),
            next_cursor=value.get("next_cursor"),
        )
    if isinstance(value, tuple) and len(value) == 2:
        return StatePage(items=tuple(value[0]), next_cursor=value[1])
    raise ReconciliationPlanError("page source must return StatePage or an equivalent mapping")


def _read_window(
    read: Any,
    start_cursor: Optional[str],
    *,
    exhausted: bool,
    page_size: int,
    max_pages: int,
) -> Tuple[List[Mapping[str, Any]], Optional[str], bool, int]:
    if exhausted:
        return [], start_cursor, True, 0
    items: List[Mapping[str, Any]] = []
    cursor = start_cursor
    seen: set = set()
    pages = 0
    while pages < max_pages:
        if cursor is not None:
            if cursor in seen:
                raise ReconciliationPlanError("pagination cursor did not advance")
            seen.add(cursor)
        page = _coerce_page(read(cursor, page_size))
        pages += 1
        for item in page.items:
            if not isinstance(item, Mapping):
                raise ReconciliationPlanError("state page items must be mappings")
            items.append(item)
        next_cursor = page.next_cursor
        if next_cursor in (None, ""):
            return items, None, True, pages
        cursor = str(next_cursor)
    return items, cursor, False, pages


def plan_snapshot(
    issues: Sequence[Mapping[str, Any]],
    pull_requests: Sequence[Mapping[str, Any]],
    *,
    context: Optional[PlanContext] = None,
) -> ReconciliationPlan:
    """Plan decisions for one already-materialized trusted snapshot."""

    state = context or PlanContext()
    decisions: List[ReconciliationDecision] = []

    decisions.extend(
        adapters.plan_issue_admissions(
            issues,
            now=state.now,
            config=state.scheduler,
            comments=state.comments_by_issue,
            open_pr_issue_numbers=state.open_pr_issue_numbers,
            active_run_issue_numbers=state.active_run_issue_numbers,
            declared_open_blockers=state.declared_open_blockers,
            native_open_blockers=state.native_open_blockers,
            qualification_trackers=state.qualification_trackers,
            terminal_stopped=state.terminal_stopped,
            generations=state.generations,
            repository_owner=(
                state.repository.split("/", 1)[0] if state.repository else ""
            ),
        )
    )

    for pr in pull_requests:
        if not isinstance(pr, Mapping):
            raise ReconciliationPlanError("pull request state must be a mapping")
        number = int(pr.get("number") or pr.get("pull_number") or 0)
        per_pr = state.pull_request_state.get(number) or state.pull_request_state.get(str(number)) or {}
        ci_run = state.ci_runs.get(number) or state.ci_runs.get(str(number))
        comments = state.comments_by_pull_request.get(number) or state.comments_by_pull_request.get(str(number)) or ()
        provider = str(per_pr.get("provider") or state.review_provider or "none")

        decisions.append(
            adapters.decide_review_ready(
                pr,
                provider=provider,
                ci_run=ci_run,
                contract_gate_run=per_pr.get("contract_gate_run"),
                contract_qualification_run=per_pr.get("contract_qualification_run"),
                packaging_run=per_pr.get("packaging_run"),
                required_gate_label=str(per_pr.get("required_gate_label") or ""),
                required_gate_name=str(per_pr.get("required_gate_name") or ""),
                required_gate_run=per_pr.get("required_gate_run"),
                repository=state.repository,
                delegated_skipped_ci=bool(per_pr.get("delegated_skipped_ci", False)),
            )
        )
        decisions.append(
            adapters.decide_ci_repair(
                pr,
                ci_run=ci_run,
                active_repair_run=bool(per_pr.get("active_repair_run", False)),
                conflict_lock=bool(per_pr.get("conflict_lock", False)),
                dispatches_for_head=int(per_pr.get("dispatches_for_head", 0) or 0),
                conflict_marker_age_seconds=per_pr.get("conflict_marker_age_seconds"),
                retry_evidence=per_pr.get("retry_evidence") or RetryEvidence(),
                max_executions=int(per_pr.get("max_executions", 10) or 10),
            )
        )
        decisions.append(
            adapters.decide_pr_agent_merge(
                pr,
                ci_run=ci_run,
                contract_gate_run=per_pr.get("contract_gate_run"),
                contract_qualification_run=per_pr.get("contract_qualification_run"),
                packaging_run=per_pr.get("packaging_run"),
                required_gate_label=str(per_pr.get("required_gate_label") or ""),
                required_gate_name=str(per_pr.get("required_gate_name") or ""),
                required_gate_run=per_pr.get("required_gate_run"),
                repository=state.repository,
                delegated_skipped_ci=bool(per_pr.get("delegated_skipped_ci", False)),
                reviewed_head=str(per_pr.get("reviewed_head") or ""),
                merge_title=str(per_pr.get("merge_title") or ""),
            )
        )

        if provider == "coderabbit":
            decisions.append(
                adapters.decide_coderabbit_queue(
                    pr,
                    comments=comments,
                    now_ms=int(per_pr.get("now_ms") or int(state.now.timestamp() * 1000)),
                    provider=provider,
                    review_requested=bool(per_pr.get("review_requested", False)),
                    default_limited_at_ms=per_pr.get("default_limited_at_ms"),
                )
            )
            decisions.append(
                adapters.decide_coderabbit_unresolved(
                    pr,
                    review_comment=per_pr.get("unresolved_comment"),
                    verified_head=str(per_pr.get("verified_head") or ""),
                    provider=provider,
                    active_repair=bool(per_pr.get("active_unresolved_repair", False)),
                )
            )
        elif provider == "pr-agent":
            status, conclusion = (("missing", "missing") if not isinstance(ci_run, Mapping) else (str(ci_run.get("status") or "missing"), str(ci_run.get("conclusion") or "pending")))
            ci_green = status == "completed" and conclusion == "success"
            decisions.append(
                adapters.decide_pr_agent_review(
                    pr,
                    ci_green_on_exact_head=bool(per_pr.get("ci_green_on_exact_head", ci_green)),
                    existing_review_head=str(per_pr.get("existing_review_head") or ""),
                    active_review=bool(per_pr.get("active_review", False)),
                    provider=provider,
                )
            )
            decisions.append(
                adapters.decide_pr_agent_recovery(
                    pr,
                    kind=str(per_pr.get("operation_kind") or "review"),
                    ci_green=bool(per_pr.get("ci_green", ci_green)),
                    operation_state=per_pr.get("operation_state"),
                    operation_description=str(per_pr.get("operation_description") or ""),
                    operation_context=per_pr.get("operation_context"),
                    failure_transient=per_pr.get("failure_transient"),
                    head_moved=bool(per_pr.get("head_moved", False)),
                    malformed_state=bool(per_pr.get("malformed_state", False)),
                    merge_blocked_deterministic=bool(per_pr.get("merge_blocked_deterministic", False)),
                    active_exact_lease=bool(per_pr.get("active_exact_lease", False)),
                    active_operation=bool(per_pr.get("active_operation", False)),
                    run_conclusion=per_pr.get("run_conclusion"),
                    status_age_seconds=per_pr.get("status_age_seconds"),
                    main_sync_required=bool(per_pr.get("main_sync_required", False)),
                    evidence=per_pr.get("retry_evidence") or RetryEvidence(),
                    marker_newer_than_status=bool(per_pr.get("marker_newer_than_status", False)),
                    marker_age_seconds=per_pr.get("marker_age_seconds"),
                    not_before_epoch=per_pr.get("not_before_epoch"),
                    now_epoch=per_pr.get("now_epoch"),
                    retry_after_seconds=per_pr.get("retry_after_seconds"),
                    ratelimit_reset_epoch=per_pr.get("ratelimit_reset_epoch"),
                    provider_reset_epoch=per_pr.get("provider_reset_epoch"),
                    max_executions=int(per_pr.get("max_executions", 10) or 10),
                )
            )

    for item in state.qualifications:
        decisions.append(adapters.decide_qualification(**dict(item)))
    for item in state.delegations:
        decisions.append(adapters.decide_delegation(**dict(item)))
    for item in state.consumers:
        decisions.append(adapters.decide_global_watchdog(**dict(item)))

    return ReconciliationPlan(
        decisions=tuple(decisions),
        continuation=Continuation(),
        issues_scanned=len(issues),
        pull_requests_scanned=len(pull_requests),
        pages_read=0,
        dry_run=True,
    )


def plan_repository(
    source: ReadOnlyStateSource,
    *,
    context: Optional[PlanContext] = None,
    continuation: Optional[Continuation] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages_per_pass: int = DEFAULT_MAX_PAGES_PER_PASS,
) -> ReconciliationPlan:
    """Enumerate a bounded, resumable repository window and plan read-only.

    The source is queried only through its read methods.  Even when the plan
    contains ``ACTION`` decisions, this function performs no mutation and the
    source exposes no dispatch/merge/write method.
    """

    if int(page_size) <= 0:
        raise ReconciliationPlanError("page_size must be positive")
    if int(max_pages_per_pass) <= 0:
        raise ReconciliationPlanError("max_pages_per_pass must be positive")
    state = context or PlanContext()
    previous = continuation or Continuation()

    issues, issue_cursor, issues_done, issue_pages = _read_window(
        source.list_issues,
        previous.issue_cursor,
        exhausted=previous.issues_exhausted,
        page_size=int(page_size),
        max_pages=int(max_pages_per_pass),
    )
    pull_requests, pull_cursor, pulls_done, pull_pages = _read_window(
        source.list_pull_requests,
        previous.pull_cursor,
        exhausted=previous.pull_requests_exhausted,
        page_size=int(page_size),
        max_pages=int(max_pages_per_pass),
    )

    plan = plan_snapshot(issues, pull_requests, context=state)
    next_continuation = Continuation(
        issue_cursor=issue_cursor,
        pull_cursor=pull_cursor,
        issues_exhausted=issues_done,
        pull_requests_exhausted=pulls_done,
        completed_passes=previous.completed_passes + 1,
    )
    return ReconciliationPlan(
        decisions=plan.decisions,
        continuation=next_continuation,
        issues_scanned=len(issues),
        pull_requests_scanned=len(pull_requests),
        pages_read=issue_pages + pull_pages,
        dry_run=True,
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_MAX_PAGES_PER_PASS",
    "ReconciliationPlanError",
    "StatePage",
    "ReadOnlyStateSource",
    "SnapshotStateSource",
    "Continuation",
    "PlanContext",
    "ReconciliationPlan",
    "plan_snapshot",
    "plan_repository",
]
