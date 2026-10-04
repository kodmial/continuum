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
    r"head=([0-9a-fA-F]{40,64})\s+"
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
    r"head=([0-9a-fA-F]{40,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempts=(\d+)\s*-->"
)
# Pre-existing short-SHA exhausted markers (7-39 hex): never authorize a
# retry attempt, but a prefix-matching exhausted marker preserves exhaustion
# fail-closed so the budget never restarts.
_LEGACY_SHORT_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{7,39})\s+"
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
    """Return the durable PR + exact HEAD + operation identity.

    Batch callers must isolate per-PR failures (catch per PR and continue,
    or pre-check with :func:`is_full_head`) so one short/truncated SHA
    never aborts a repository-global open-PR scan."""

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
    # Migration note: short SHAs were rejected here (and by every durable
    # marker reader) since exact-HEAD safety was introduced, so producers
    # must pass the full commit id from the PR HEAD (``pr.head.sha``).
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise RecoveryError(
            "head_sha must be a full hexadecimal commit id "
            "(40-hex SHA-1 or 64-hex SHA-256); short prefixes are rejected"
        )
    if normalized_kind not in KINDS:
        raise RecoveryError("kind must be review or repair")
    return f"{number}:{head}:{normalized_kind}"


_RECOVERY_ELIGIBLE_RE = re.compile(r"\brecovery eligible\b")
_NEGATED_RECOVERY_ELIGIBLE_RE = re.compile(
    r"\b(?:not|no|non|never)[\s\-_]*recovery[\s\-_]+eligible\b"
)
# Deterministic policy hints dominate the recovery token: a description that
# carries both (e.g. "CI test failure; recovery eligible" synthesized from
# PR-visible output) must hold, never burn transient budget. Only an
# explicit failure_transient=True classifier verdict overrides this.
# ``unresolved`` alone is not a hint: transient infrastructure text such as
# ``unresolved host`` or ``unresolved DNS`` must retry through the bounded
# budget, so only an unresolved review finding (finding/review/thread/
# comment/conversation) holds. Bare ``conflict`` matches at any position
# (including position zero) except inside ``conflict-free``/``conflicting``,
# so incidental mentions dispatch while a real merge conflict still holds
# via ``merge conflict`` or the bare-conflict pattern.
_DETERMINISTIC_HINTS = (
    "test failure",
    "tests failed",
    "merge conflict",
    "malformed",
    "stale head",
    "moved head",
    "invalid state",
)
_UNRESOLVED_FINDING_RE = re.compile(
    r"unresolved\s+(findings?|reviews?|threads?|comments?|conversations?)"
)
_BARE_CONFLICT_RE = re.compile(r"(?:^|[^a-z])conflict(?!\s*-\s*free\b)(?!ing\b)")
TRUSTED_OPERATION_CONTEXTS = frozenset(
    {
        "continuum/pr-agent-review",
        "continuum/pr-agent-repair",
    }
)


def _is_explicit_recovery_eligible(description: str) -> bool:
    """Whether a status description carries the explicit recovery token.

    Mirrors the single lifecycle contract in
    :mod:`continuum.lifecycle_recovery`: only the exact ``recovery
    eligible`` token authorizes a retry, and negated forms (``not/no/non/
    never`` plus any space/hyphen/underscore separator, including the
    concatenated ``nonrecovery eligible``) hold.
    """

    text = str(description or "")
    if not _RECOVERY_ELIGIBLE_RE.search(text.lower()):
        return False
    if _NEGATED_RECOVERY_ELIGIBLE_RE.search(text.lower()):
        return False
    return True


def _same_head(marker_head: str, head: str) -> bool:
    """Whether a durable marker refers to the same logical HEAD.

    Identity is the exact full commit id (case-insensitive). Prefix
    matching is rejected: two distinct commits can share a 7-char prefix,
    so a prefix match would let old-HEAD budget/exhaustion authorize a new
    HEAD.
    """

    marker = str(marker_head or "").strip().lower()
    current = str(head or "").strip().lower()
    return bool(marker) and marker == current


def is_full_head(head_sha: object) -> bool:
    """Whether a value is a full commit id usable as recovery identity.

    Batch loops use this as a per-PR guard (`continue` on False) so one
    malformed HEAD skips loudly instead of raising out of the whole scan.
    """

    return bool(re.fullmatch(r"[0-9a-f]{40,64}", str(head_sha or "").strip().lower()))


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
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise RecoveryError(
            "head_sha must be a full hexadecimal commit id "
            "(40-hex SHA-1 or 64-hex SHA-256); short prefixes are rejected"
        )
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
                # Latest write wins, including clearing: a newer
                # same-attempt marker without not-before lifts the older
                # reset-aware wait instead of leaving a stale deferral
                # that waits past the point the latest write cleared it.
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
        # Legacy short-SHA markers are ignored: two distinct commits can
        # share a 7-char prefix, so prefix matching would let old-HEAD
        # evidence strand an unrelated healthy HEAD with no expiry path.
        # Exact-HEAD isolation requires full-commit identity only.

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
    operation_context: Optional[str] = None,
    failure_transient: Optional[bool] = None,
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

    ``failure_transient`` carries the caller's explicit classifier verdict:
    True dispatches even without the token (so classifier-proven transient
    infrastructure gaps never strand for lack of a token), False holds even
    with the token. When None, only the explicit reconciler-synthesized
    ``recovery eligible`` token from a trusted commit-status context
    authorizes a retry; a bare ``transient`` substring never suffices, and
    a deterministic policy hint alongside the token still holds.
    """

    state = str(operation_state or "").strip().lower() or None
    conclusion = str(run_conclusion or "").strip().lower() or None
    description = str(operation_description or "")

    # Settled dominates every wait/hold below so a success clears obsolete
    # durable state (exhaustion markers, stale leases, not-before waits)
    # via the success path instead of holding on stale evidence.
    if state == "success":
        return RecoveryDecision("settled", None, "operation already settled")
    if not ci_green:
        return RecoveryDecision("wait", None, "exact HEAD CI is not green")
    if evidence.exhausted:
        return RecoveryDecision("hold", None, "retry budget already exhausted")
    if active_exact_run:
        return RecoveryDecision("wait", None, "exact operation already active")
    if evidence.not_before_epoch is not None and now_epoch is None:
        # Fail closed without a clock: a durable reset-aware wait the marker
        # already committed must defer to the scheduled safety net, never
        # dispatch straight through it and burn transient budget on 403/429.
        return RecoveryDecision(
            "wait", None, "durable reset-aware not-before requires a clock; deferring"
        )
    if (
        evidence.not_before_epoch is not None
        and now_epoch is not None
    ):
        try:
            not_before = int(evidence.not_before_epoch)
            now = int(now_epoch)
        except (TypeError, ValueError) as exc:
            raise RecoveryError("not-before/now epochs must be integers") from exc
        if now < not_before:
            return RecoveryDecision(
                "wait", None, "durable reset-aware not-before time has not arrived"
            )

    if (
        marker_newer_than_status
        and evidence.latest_attempt is not None
        and (
            marker_age_seconds is None
            or marker_age_seconds < dispatch_grace_seconds
        )
    ):
        # Fail closed on unknown marker age, mirroring the reconciler: a
        # durable marker without a parseable timestamp cannot prove grace
        # expired, so it stays inside grace instead of dispatching a
        # likely duplicate. The scheduled safety net keeps waking (caller
        # stub cron), and newer status activity clears marker_newer, so
        # this wait always has an expiry path.
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
        if failure_transient is True:
            recoverable = True
        elif failure_transient is False:
            return RecoveryDecision(
                "hold", None, "deterministic failure is not automatically retried"
            )
        elif _is_explicit_recovery_eligible(description):
            # Trust gate: the token alone never authorizes a retry unless
            # it comes from a reconciler-synthesized status in a trusted
            # context. A missing context is untrusted: PR-visible text is
            # not a reconciler status, so omitting the context can never
            # bypass the TRUSTED_OPERATION_CONTEXTS check and burn
            # transient budget on the token alone.
            if str(operation_context or "").strip().lower() not in TRUSTED_OPERATION_CONTEXTS:
                return RecoveryDecision(
                    "hold", None, "recovery token from untrusted context is not automatically retried"
                )
            lowered = description.lower()
            if (
                any(hint in lowered for hint in _DETERMINISTIC_HINTS)
                or _UNRESOLVED_FINDING_RE.search(lowered)
                or _BARE_CONFLICT_RE.search(lowered)
            ):
                return RecoveryDecision(
                    "hold", None, "deterministic failure is not automatically retried"
                )
            recoverable = True
        else:
            return RecoveryDecision(
                "hold", None, "deterministic failure is not automatically retried"
            )
    elif state == "pending":
        # An explicit deterministic verdict dominates staleness, mirroring
        # the single lifecycle contract; an explicit transient verdict
        # dispatches immediately without waiting for staleness.
        if failure_transient is False:
            return RecoveryDecision(
                "hold", None, "deterministic failure is not automatically retried"
            )
        if failure_transient is True:
            recoverable = True
        elif conclusion in RETRYABLE_RUN_CONCLUSIONS:
            recoverable = True
        elif (
            status_age_seconds is not None
            and status_age_seconds >= stale_after_seconds
            and (
                conclusion is None
                or conclusion in RETRYABLE_RUN_CONCLUSIONS
            )
        ):
            # Stale alone never proves transient: a stale deterministic
            # failure/success conclusion waits instead of dispatching and
            # burning the transient budget.
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
    "TRUSTED_OPERATION_CONTEXTS",
    "RETRYABLE_RUN_CONCLUSIONS",
    "KINDS",
    "RecoveryError",
    "RetryEvidence",
    "RecoveryDecision",
    "operation_key",
    "is_full_head",
    "retry_evidence",
    "resolve_max_executions",
    "retry_delay_schedule",
    "backoff_seconds",
    "decide_recovery",
]
