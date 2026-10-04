"""Deterministic PR-Agent recovery decisions for Work Lock #38.

This module models only provider-specific recovery/reconciliation.  It does not
review code, repair code, or implement cross-HEAD finding convergence (#39).

Durable retry evidence is scoped by PR + exact HEAD + operation kind
("review" or "repair").  GitHub issue comments are accepted as retry evidence
only when their author association is trusted by the repository; arbitrary
external comments must never consume or exhaust the automatic retry budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


MAX_EXECUTIONS = 3
STALE_AFTER_SECONDS = 70 * 60
DISPATCH_GRACE_SECONDS = 90
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
RETRYABLE_RUN_CONCLUSIONS = frozenset(
    {"cancelled", "timed_out", "stale", "startup_failure"}
)
KINDS = frozenset({"review", "repair"})

_RETRY_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempt=(\d+)\s*-->"
)
_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
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
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise RecoveryError("head_sha must be a hexadecimal commit id")
    if normalized_kind not in KINDS:
        raise RecoveryError("kind must be review or repair")
    return f"{number}:{head}:{normalized_kind}"


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

    latest_attempt: Optional[int] = None
    latest_marker_at: Optional[datetime] = None
    exhausted = False

    for comment in comments:
        association = str(comment.get("author_association") or "").upper()
        if association not in TRUSTED_ASSOCIATIONS:
            continue
        body = str(comment.get("body") or "")
        created_at = _parse_time(comment.get("updated_at") or comment.get("created_at"))

        for match in _RETRY_RE.finditer(body):
            marker_head, marker_kind, attempt_text = match.groups()
            if marker_head.lower() != head or marker_kind != normalized_kind:
                continue
            attempt = int(attempt_text)
            if latest_attempt is None or attempt > latest_attempt:
                latest_attempt = attempt
                latest_marker_at = created_at
            elif attempt == latest_attempt and created_at is not None:
                if latest_marker_at is None or created_at > latest_marker_at:
                    latest_marker_at = created_at

        for match in _EXHAUSTED_RE.finditer(body):
            marker_head, marker_kind, attempts_text = match.groups()
            if marker_head.lower() != head or marker_kind != normalized_kind:
                continue
            if int(attempts_text) >= MAX_EXECUTIONS:
                exhausted = True

    return RetryEvidence(
        latest_attempt=latest_attempt,
        latest_marker_at=latest_marker_at,
        exhausted=exhausted,
    )


def backoff_seconds(attempt: int) -> int:
    """Delay before execution index 1/2; initial execution index 0 is immediate."""

    if attempt < 0:
        raise RecoveryError("attempt must be non-negative")
    if attempt == 0:
        return 0
    return 15 * (1 << (attempt - 1))


def _next_attempt(
    *,
    operation_seen: bool,
    evidence: RetryEvidence,
    marker_newer_than_status: bool,
) -> int:
    """Return the next execution index without double-consuming a lost dispatch."""

    if evidence.latest_attempt is None:
        return 1 if operation_seen else 0
    if marker_newer_than_status:
        # The marker was written for a dispatch that has not produced any newer
        # operation state.  If its run vanished, replay that same bounded
        # attempt instead of burning the next slot.
        return evidence.latest_attempt
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
) -> RecoveryDecision:
    """Reconcile one review/repair operation from latest authoritative state."""

    if not ci_green:
        return RecoveryDecision("wait", None, "exact HEAD CI is not green")
    if evidence.exhausted:
        return RecoveryDecision("hold", None, "retry budget already exhausted")
    if active_exact_run:
        return RecoveryDecision("wait", None, "exact operation already active")

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

    attempt = _next_attempt(
        operation_seen=operation_seen,
        evidence=evidence,
        marker_newer_than_status=marker_newer_than_status,
    )
    if attempt >= MAX_EXECUTIONS:
        return RecoveryDecision("exhaust", None, "automatic execution budget exhausted")
    return RecoveryDecision(
        "dispatch",
        attempt,
        "lost/missing operation" if state is None else "transient/stale operation",
    )


__all__ = [
    "MAX_EXECUTIONS",
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
    "backoff_seconds",
    "decide_recovery",
]
