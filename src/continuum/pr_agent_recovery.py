"""Deterministic PR-Agent recovery decisions for Work Lock #38.

This module models only provider-specific recovery/reconciliation.  It does not
review code, repair code, or implement cross-HEAD finding convergence (#39).

Durable retry evidence is scoped by PR + exact HEAD + operation kind
("review" or "repair").  GitHub issue comments are accepted as retry evidence
only when their author association is trusted by the repository; arbitrary
external comments must never consume or exhaust the automatic retry budget.

The budget, schedule, and trust roots are the single lifecycle contract in
:mod:`continuum.lifecycle_recovery` (#224): 10 total transient executions by
default (configurable through a safe bounded Continuum variable), the
~10s/30s/60s/3m/5m/10m/20m/40m/60m retry schedule with bounded jitter,
reset-aware minimums, and durable not-before redispatch.  A success or a new
HEAD resets the episode; deterministic failures never consume the budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from continuum.lifecycle_recovery import (
    DISPATCH_GRACE_SECONDS as _LIFECYCLE_DISPATCH_GRACE,
)
from continuum.lifecycle_recovery import (
    MAX_TRANSIENT_EXECUTIONS as _LIFECYCLE_MAX_EXECUTIONS,
)
from continuum.lifecycle_recovery import (
    RETRYABLE_RUN_CONCLUSIONS as _LIFECYCLE_RETRYABLE,
)
from continuum.lifecycle_recovery import STALE_AFTER_SECONDS as _LIFECYCLE_STALE
from continuum.lifecycle_recovery import (
    TRUSTED_ASSOCIATIONS as _LIFECYCLE_TRUSTED,
)
from continuum.lifecycle_recovery import exponential_backoff_seconds as _lifecycle_backoff
from continuum.lifecycle_recovery import resolve_max_executions as _resolve_budget


MAX_EXECUTIONS = _LIFECYCLE_MAX_EXECUTIONS
MIN_EXECUTIONS = 1
RETRY_DELAY_SCHEDULE = (0, 10, 30, 60, 180, 300, 600, 1200, 2400, 3600)
STALE_AFTER_SECONDS = _LIFECYCLE_STALE
DISPATCH_GRACE_SECONDS = _LIFECYCLE_DISPATCH_GRACE
TRUSTED_ASSOCIATIONS = _LIFECYCLE_TRUSTED
RETRYABLE_RUN_CONCLUSIONS = _LIFECYCLE_RETRYABLE
KINDS = frozenset({"review", "repair"})

_RETRY_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempt=(\d+)\s*-->"
)
# Canonical lifecycle markers (#224) carry the same review/repair budget with
# an optional durable ``not-before=<epoch>`` inside the marker. They are read
# here so the two helpers share one compatible recovery contract; new writes
# use the lifecycle prefix.
_LIFECYCLE_RETRY_RE = re.compile(
    r"<!--\s*continuum-lifecycle-retry\s+"
    r"head=([0-9a-fA-F]{40,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempt=(\d+)"
    r"(?:\s+not-before=(\d+))?"
    r"[^>]*-->"
)
_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempts=(\d+)\s*-->"
)
_LIFECYCLE_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-lifecycle-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{40,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempts=(\d+)\s*-->"
)


class RecoveryError(ValueError):
    """Raised when recovery inputs cannot be interpreted safely."""


@dataclass(frozen=True)
class RetryEvidence:
    """Authoritative retry evidence for one PR/HEAD/operation."""

    latest_attempt: Optional[int] = None
    latest_marker_at: Optional[datetime] = None
    exhausted: bool = False
    not_before_epoch: Optional[int] = None


@dataclass(frozen=True)
class RecoveryDecision:
    """One deterministic reconciler decision."""

    action: str
    attempt: Optional[int]
    reason: str


def operation_key(pr_number: object, head_sha: object, kind: object) -> str:
    """Return the durable PR + exact HEAD + operation identity."""

    try:
        number = int(str(pr_number).strip())
    except (TypeError, ValueError) as exc:
        raise RecoveryError("pr_number must be a positive integer") from exc
    head = str(head_sha or "").strip().lower()
    normalized_kind = str(kind or "").strip().lower()
    if number <= 0:
        raise RecoveryError("pr_number must be a positive integer")
    # Exact-HEAD identity requires a full commit id; short prefixes would
    # split the retry budget and weaken the old-HEAD-cannot-mutate guarantee.
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise RecoveryError("head_sha must be a full hexadecimal commit id")
    if normalized_kind not in KINDS:
        raise RecoveryError("kind must be review or repair")
    return f"{number}:{head}:{normalized_kind}"


def _same_head(marker_head: str, head: str) -> bool:
    """Whether a durable marker refers to the same logical HEAD.

    New writes always carry a full commit id, but pre-existing Work Lock #38
    markers may carry a short prefix. A short marker that is a prefix of the
    full HEAD (or vice versa, minimum 7 hex chars) is the same logical commit
    and must still count toward the budget; otherwise tightening the pattern
    would reset prior attempts/exhaustion and re-retry an exhausted operation.
    """

    marker = str(marker_head or "").strip().lower()
    current = str(head or "").strip().lower()
    if marker == current:
        return True
    if len(marker) >= 7 and len(current) >= 7:
        if current.startswith(marker) or marker.startswith(current):
            return True
    return False


def _parse_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def retry_evidence(
    comments: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    kind: str,
    max_executions: int = MAX_EXECUTIONS,
) -> RetryEvidence:
    """Read trusted durable retry markers for one exact operation.

    Association rather than login is used so a PAT owned by an organization
    member/collaborator remains usable, while arbitrary external commenters
    cannot forge retry/exhaustion state.
    """

    head = str(head_sha or "").strip().lower()
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in KINDS:
        raise RecoveryError("kind must be review or repair")

    budget = _resolve_budget(max_executions)
    latest_attempt: Optional[int] = None
    latest_marker_at: Optional[datetime] = None
    not_before: Optional[int] = None
    exhausted = False

    def consider(
        marker_head: str,
        marker_kind: str,
        attempt_text: str,
        marker_not_before: Optional[int],
        created_at: Optional[datetime],
    ) -> None:
        nonlocal latest_attempt, latest_marker_at, not_before
        if marker_kind != normalized_kind or not _same_head(marker_head, head):
            return
        attempt = int(attempt_text)
        if latest_attempt is None or attempt > latest_attempt:
            latest_attempt = attempt
            latest_marker_at = created_at
            not_before = marker_not_before
        elif attempt == latest_attempt and created_at is not None:
            if latest_marker_at is None or created_at > latest_marker_at:
                latest_marker_at = created_at
                if marker_not_before is not None:
                    not_before = marker_not_before

    for comment in comments:
        association = str(comment.get("author_association") or "").upper()
        if association not in TRUSTED_ASSOCIATIONS:
            continue
        body = str(comment.get("body") or "")
        created_at = _parse_time(comment.get("updated_at") or comment.get("created_at"))

        for match in _LIFECYCLE_RETRY_RE.finditer(body):
            marker_head, marker_kind, attempt_text, not_before_text = match.groups()
            marker_not_before = int(not_before_text) if not_before_text else None
            consider(marker_head, marker_kind, attempt_text, marker_not_before, created_at)
        for match in _RETRY_RE.finditer(body):
            marker_head, marker_kind, attempt_text = match.groups()
            # Legacy markers predate durable not-before and carry none; a
            # stray ``not-before=`` outside any canonical marker is ignored.
            consider(marker_head, marker_kind, attempt_text, None, created_at)

        for match in _LIFECYCLE_EXHAUSTED_RE.finditer(body):
            marker_head, marker_kind, attempts_text = match.groups()
            if marker_kind != normalized_kind or not _same_head(marker_head, head):
                continue
            if int(attempts_text) >= budget:
                exhausted = True
        for match in _EXHAUSTED_RE.finditer(body):
            marker_head, marker_kind, attempts_text = match.groups()
            if marker_kind != normalized_kind or not _same_head(marker_head, head):
                continue
            if int(attempts_text) >= budget:
                exhausted = True

    return RetryEvidence(
        latest_attempt=latest_attempt,
        latest_marker_at=latest_marker_at,
        exhausted=exhausted,
        not_before_epoch=not_before,
    )


def resolve_max_executions(raw: object = None, default: int = MAX_EXECUTIONS) -> int:
    """Resolve the bounded transient-execution budget (1..10, default 10)."""

    return _resolve_budget(raw, default)


def retry_delay_schedule() -> tuple[int, ...]:
    """Return the canonical per-execution delay schedule (index 0..9)."""

    return RETRY_DELAY_SCHEDULE


def backoff_seconds(attempt: int) -> int:
    """Delay for execution index 0..9 from the single lifecycle schedule.

    Index 0 runs immediately; retries 1..9 wait ~10s, 30s, 60s, 3m, 5m,
    10m, 20m, 40m, 60m (bounded jitter is added by the caller).
    """

    if attempt < 0:
        raise RecoveryError("attempt must be non-negative")
    try:
        return _lifecycle_backoff(attempt)
    except ValueError as exc:
        raise RecoveryError(str(exc)) from exc


def _next_attempt(
    *,
    operation_seen: bool,
    evidence: RetryEvidence,
    marker_newer_than_status: bool,
) -> int:
    """Return the next execution index, always advancing.

    The dispatch-grace wait handles an unobserved dispatch; replaying the
    same index when the marker is newer would keep ``attempt >= budget``
    from ever firing and bypass the bounded budget.
    """

    if evidence.latest_attempt is None:
        return 1 if operation_seen else 0
    return evidence.latest_attempt + 1


def decide_recovery(
    *,
    ci_green: bool,
    operation_state: Optional[str],
    operation_description: str = "",
    active_exact_run: bool = False,
    run_conclusion: Optional[str] = None,
    status_age_seconds: Optional[int] = None,
    evidence: RetryEvidence = RetryEvidence(),
    marker_newer_than_status: bool = False,
    marker_age_seconds: Optional[int] = None,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
    dispatch_grace_seconds: int = DISPATCH_GRACE_SECONDS,
    max_executions: int = MAX_EXECUTIONS,
    now_epoch: Optional[int] = None,
) -> RecoveryDecision:
    """Reconcile one review/repair operation from latest authoritative state.

    The budget defaults to the single lifecycle contract (10 total transient
    executions, safe-bounded).  Deterministic failures never consume it; a
    success or a new HEAD resets the episode.  A durable reset-aware
    not-before (canonical marker, redispatch via the scheduled safety net)
    waits instead of dispatching early.
    """

    if not ci_green:
        return RecoveryDecision("wait", None, "exact HEAD CI is not green")
    if evidence.exhausted:
        return RecoveryDecision("hold", None, "retry budget already exhausted")
    if active_exact_run:
        return RecoveryDecision("wait", None, "exact operation already active")
    if (
        evidence.not_before_epoch is not None
        and now_epoch is not None
        and int(now_epoch) < int(evidence.not_before_epoch)
    ):
        return RecoveryDecision(
            "wait", None, "durable reset-aware not-before time has not arrived"
        )

    state = str(operation_state or "").strip().lower() or None
    conclusion = str(run_conclusion or "").strip().lower() or None
    description = str(operation_description or "")

    if (
        marker_newer_than_status
        and evidence.latest_attempt is not None
        and marker_age_seconds is not None
        and marker_age_seconds < dispatch_grace_seconds
    ):
        return RecoveryDecision(
            "wait", None, "newer retry dispatch marker is still inside dispatch grace"
        )

    recoverable = False
    operation_seen = state is not None

    if state is None:
        recoverable = True
    elif state == "success":
        return RecoveryDecision("settled", None, "operation already settled")
    elif state == "failure":
        if "recovery eligible" in description.lower() or "transient" in description.lower():
            recoverable = True
        else:
            return RecoveryDecision(
                "hold", None, "deterministic failure is not automatically retried"
            )
    elif state == "pending":
        if conclusion in RETRYABLE_RUN_CONCLUSIONS:
            recoverable = True
        elif (
            status_age_seconds is not None
            and status_age_seconds >= stale_after_seconds
            and conclusion not in {"queued", "in_progress", "waiting", "requested"}
        ):
            recoverable = True
        else:
            return RecoveryDecision("wait", None, "pending operation is not stale")
    else:
        return RecoveryDecision("hold", None, f"unknown operation state: {state}")

    if not recoverable:
        return RecoveryDecision("hold", None, "operation is not recoverable")

    budget = _resolve_budget(max_executions)
    attempt = _next_attempt(
        operation_seen=operation_seen,
        evidence=evidence,
        marker_newer_than_status=marker_newer_than_status,
    )
    if attempt >= budget:
        return RecoveryDecision("exhaust", None, "automatic execution budget exhausted")
    return RecoveryDecision(
        "dispatch",
        attempt,
        "lost/missing operation" if state is None else "transient/stale operation",
    )


__all__ = [
    "MAX_EXECUTIONS",
    "MIN_EXECUTIONS",
    "RETRY_DELAY_SCHEDULE",
    "STALE_AFTER_SECONDS",
    "DISPATCH_GRACE_SECONDS",
    "TRUSTED_ASSOCIATIONS",
    "RETRYABLE_RUN_CONCLUSIONS",
    "KINDS",
    "RecoveryError",
    "RetryEvidence",
    "RecoveryDecision",
    "operation_key",
    "retry_evidence",
    "resolve_max_executions",
    "retry_delay_schedule",
    "backoff_seconds",
    "decide_recovery",
]
