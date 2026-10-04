"""Generic Continuum PR-lifecycle self-healing contract (issue #224).

This module is the one recovery contract for every Continuum-managed PR
transition: CodeRabbit/common auto-merge, PR-Agent review/repair, CI repair,
review-ready transitions and main-sync/merge reconciliation.

It generalizes the PR-Agent-specific Work Lock #38 mechanism
(:mod:`continuum.pr_agent_recovery`) and reuses its durable-identity and
token-policy semantics without changing any provider-specific review policy:

- recovery identity is ``repo + PR + exact HEAD + operation kind``;
- latest repository state is authoritative; a stale failed run is only a
  wakeup, never state;
- only generic infrastructure failures consume the bounded transient budget
  (10 total executions per identity by default, configurable through a safe
  bounded Continuum variable); deterministic policy/code failures hold and
  a success or a new HEAD resets the episode;
- backoff follows the configured schedule (~10s, 30s, 60s, 3m, 5m, 10m,
  20m, 40m, 60m for retries 1..9) with bounded jitter, honors
  ``Retry-After`` and GitHub ``X-RateLimit-Reset`` (or any provider reset)
  as a minimum next-attempt time, and never asks a runner to sleep through
  a long wait (the caller persists not-before/next-attempt and exits; the
  scheduled watchdog redispatches when the window expires);
- duplicate wakeups coalesce onto one per-PR/HEAD lease;
- same-repository read-only discovery uses the run-scoped ``GITHUB_TOKEN``;
  PAT/TAP_PAT is reserved for mutations/dispatches.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


MAX_TRANSIENT_ATTEMPTS = 10
# Alias kept so callers can name the budget either way; both denote the same
# 10-execution contract.
MAX_TRANSIENT_EXECUTIONS = 10
MIN_TRANSIENT_EXECUTIONS = 1
# Canonical retry schedule: execution index 0 runs immediately; retries 1..9
# wait approximately 10s, 30s, 60s, 3m, 5m, 10m, 20m, 40m, 60m (plus bounded
# jitter, and never earlier than Retry-After / rate-limit / provider reset).
RETRY_DELAY_SCHEDULE = (0, 10, 30, 60, 180, 300, 600, 1200, 2400, 3600)
BASE_BACKOFF_SECONDS = 10
JITTER_CAP_SECONDS = 5
STALE_AFTER_SECONDS = 70 * 60
DISPATCH_GRACE_SECONDS = 90
# A runner must never sleep through a long rate-limit/quota wait.  Delays at
# or below this threshold may be awaited inline; anything longer must be
# deferred to a future scheduled redispatch so the runner exits.
MAX_INLINE_WAIT_SECONDS = 60

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
RETRYABLE_RUN_CONCLUSIONS = frozenset(
    {"cancelled", "timed_out", "stale", "startup_failure"}
)
# Operation kinds covered by the single lifecycle contract.  ``review`` and
# ``repair`` keep the exact Work Lock #38 identities; the remaining kinds
# extend the same durable semantics to the generic lifecycle without changing
# provider review policy.
KINDS = frozenset(
    {
        "review",
        "repair",
        "review-ready",
        "automerge",
        "main-sync",
        "merge",
        "ci-repair",
    }
)
PR_AGENT_KINDS = frozenset({"review", "repair"})

_LIFECYCLE_RETRY_RE = re.compile(
    r"<!--\s*continuum-lifecycle-retry\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=([A-Za-z][A-Za-z0-9-]*)\s+"
    r"attempt=(\d+)"
    r"(?:\s+not-before=(\d+))?"
    r"[^>]*-->"
)
_LIFECYCLE_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-lifecycle-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=([A-Za-z][A-Za-z0-9-]*)\s+"
    r"attempts=(\d+)\s*-->"
)
# Work Lock #38 markers remain readable so PR-Agent durable state survives
# the generalization; new markers use the lifecycle prefix.
_LEGACY_RETRY_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempt=(\d+)\s*-->"
)
_LEGACY_EXHAUSTED_RE = re.compile(
    r"<!--\s*continuum-pr-agent-retry-exhausted\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"kind=(review|repair)\s+"
    r"attempts=(\d+)\s*-->"
)

_TRANSIENT_ERROR_PATTERNS = (
    "timeout",
    "timed out",
    "econnreset",
    "econnrefused",
    "eai_again",
    "epipe",
    "socket hang up",
    "network error",
    "network failure",
    "network timeout",
    "network unreachable",
    "dns error",
    "dns failure",
    "dns resolution",
    "tls handshake",
    "tls error",
    "ssl error",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "bootstrap failed",
    "bootstrap error",
    "runner evicted",
    "runner cancelled",
    "runner timeout",
    "runner lost",
    "run evicted",
    "run cancelled",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "quota exhaustion",
    "secondary rate limit",
    "retry after",
)
_DETERMINISTIC_HINTS = (
    "test failure",
    "tests failed",
    "unresolved",
    "merge conflict",
    "conflict",
    "malformed",
    "stale head",
    "moved head",
    "invalid state",
)


class LifecycleRecoveryError(ValueError):
    """Raised when lifecycle recovery inputs cannot be interpreted safely."""


@dataclass(frozen=True)
class FailureClassification:
    """Generic infrastructure-vs-deterministic classification."""

    transient: bool
    reason: str
    retry_after_seconds: Optional[int] = None
    ratelimit_reset_epoch: Optional[int] = None


@dataclass(frozen=True)
class RetryEvidence:
    """Authoritative durable retry evidence for one repo/PR/HEAD/operation."""

    latest_attempt: Optional[int] = None
    latest_marker_at: Optional[datetime] = None
    exhausted: bool = False
    not_before_epoch: Optional[int] = None


@dataclass(frozen=True)
class RecoveryDecision:
    """One deterministic latest-state reconciler decision."""

    action: str
    attempt: Optional[int]
    reason: str
    not_before_epoch: Optional[int] = None
    defer_dispatch: bool = False


def normalize_head(head_sha: object) -> str:
    head = str(head_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise LifecycleRecoveryError("head_sha must be a hexadecimal commit id")
    return head


def normalize_kind(kind: object) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in KINDS:
        raise LifecycleRecoveryError(
            "kind must be one of: " + ", ".join(sorted(KINDS))
        )
    return normalized


def operation_key(repo: object, pr_number: object, head_sha: object, kind: object) -> str:
    """Return the durable repo + PR + exact HEAD + operation identity."""

    repository = str(repo or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise LifecycleRecoveryError("repo must look like owner/name")
    try:
        number = int(str(pr_number).strip())
    except (TypeError, ValueError) as exc:
        raise LifecycleRecoveryError("pr_number must be a positive integer") from exc
    if number <= 0:
        raise LifecycleRecoveryError("pr_number must be a positive integer")
    return f"{repository}#{number}:{normalize_head(head_sha)}:{normalize_kind(kind)}"


def concurrency_key(repo: object, pr_number: object, head_sha: object) -> str:
    """Per-PR/HEAD lease key: at most one authoritative reconciliation per key."""

    repository = str(repo or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise LifecycleRecoveryError("repo must look like owner/name")
    try:
        number = int(str(pr_number).strip())
    except (TypeError, ValueError) as exc:
        raise LifecycleRecoveryError("pr_number must be a positive integer") from exc
    if number <= 0:
        raise LifecycleRecoveryError("pr_number must be a positive integer")
    return f"{repository}#{number}:{normalize_head(head_sha)}"


def _parse_int(value: object) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


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


def classify_infrastructure_failure(
    *,
    status: object = None,
    headers: Optional[Mapping[str, object]] = None,
    error: object = "",
    run_conclusion: object = None,
) -> FailureClassification:
    """Classify a failure as transient infrastructure or deterministic.

    Transient: GitHub 403 rate limit with X-RateLimit-Remaining=0, 429 with
    Retry-After, any 5xx, network timeout/reset, runner cancellation/eviction,
    temporary provider/bootstrap outage.  Everything else fails closed to
    deterministic so it never consumes the transient budget.
    """

    norm_headers: dict[str, str] = {}
    for key, value in (headers or {}).items():
        norm_headers[str(key).strip().lower()] = str(value).strip()
    message = str(error or "").strip().lower()
    conclusion = str(run_conclusion or "").strip().lower() or None
    code = _parse_int(status)

    def reset_epoch() -> Optional[int]:
        for key in ("x-ratelimit-reset", "ratelimit-reset"):
            epoch = _parse_int(norm_headers.get(key, ""))
            if epoch is not None and epoch > 0:
                return epoch
        return None

    def retry_after() -> Optional[int]:
        for key in ("retry-after", "retry_after"):
            seconds = _parse_int(norm_headers.get(key, ""))
            if seconds is not None and seconds >= 0:
                return seconds
        return None

    if conclusion in RETRYABLE_RUN_CONCLUSIONS:
        return FailureClassification(
            True, f"retryable run conclusion: {conclusion}",
            retry_after_seconds=retry_after(),
            ratelimit_reset_epoch=reset_epoch(),
        )
    if code == 429:
        return FailureClassification(
            True, "http 429 too many requests",
            retry_after_seconds=retry_after(),
            ratelimit_reset_epoch=reset_epoch(),
        )
    if code == 403:
        remaining = _parse_int(norm_headers.get("x-ratelimit-remaining", ""))
        rate_limited = remaining == 0 or any(
            token in message
            for token in ("rate limit", "rate-limit", "ratelimit", "quota exhaustion")
        )
        if rate_limited:
            return FailureClassification(
                True, "github 403 rate limit exhausted",
                retry_after_seconds=retry_after(),
                ratelimit_reset_epoch=reset_epoch(),
            )
        return FailureClassification(False, "github 403 without rate-limit evidence")
    if code is not None and 500 <= code <= 599:
        return FailureClassification(
            True, f"http {code} server error",
            retry_after_seconds=retry_after(),
            ratelimit_reset_epoch=reset_epoch(),
        )
    if message and any(token in message for token in _TRANSIENT_ERROR_PATTERNS):
        return FailureClassification(
            True, "transient network/runner/provider error signature",
            retry_after_seconds=retry_after(),
            ratelimit_reset_epoch=reset_epoch(),
        )
    if conclusion in {"failure", "success"} and message:
        return FailureClassification(False, "deterministic run conclusion with message")
    return FailureClassification(False, "no transient infrastructure evidence")


def classify_operation_failure(
    *,
    description: object = "",
    ci_failed: bool = False,
    has_unresolved_findings: bool = False,
    has_merge_conflict: bool = False,
    malformed_state: bool = False,
    head_moved: bool = False,
) -> FailureClassification:
    """Classify policy/code outcomes that must never burn transient budget."""

    text = str(description or "").strip().lower()
    if head_moved:
        return FailureClassification(False, "moved/stale HEAD is deterministic")
    if malformed_state or "malformed" in text or "invalid state" in text:
        return FailureClassification(False, "malformed lifecycle state is deterministic")
    if ci_failed or "test failure" in text or "tests failed" in text:
        return FailureClassification(False, "current-head CI test failure is deterministic")
    if has_unresolved_findings or "unresolved" in text:
        return FailureClassification(False, "unresolved review finding is deterministic")
    if has_merge_conflict or "merge conflict" in text:
        return FailureClassification(False, "merge conflict uses the repair path")
    if text and any(token in text for token in _DETERMINISTIC_HINTS):
        return FailureClassification(False, "deterministic policy failure")
    return FailureClassification(False, "no transient infrastructure evidence")


def resolve_max_executions(raw: object = None, default: int = MAX_TRANSIENT_ATTEMPTS) -> int:
    """Resolve the bounded transient-execution budget from a Continuum variable.

    The budget is always clamped to [MIN_TRANSIENT_EXECUTIONS,
    MAX_TRANSIENT_EXECUTIONS] so a misconfigured variable can neither disable
    recovery silently nor grant an unbounded budget.  Unparseable values fall
    back to ``default`` (itself clamped).
    """

    try:
        fallback = int(str(default).strip())
    except (TypeError, ValueError):
        fallback = MAX_TRANSIENT_ATTEMPTS
    fallback = max(MIN_TRANSIENT_EXECUTIONS, min(MAX_TRANSIENT_EXECUTIONS, fallback))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return max(MIN_TRANSIENT_EXECUTIONS, min(MAX_TRANSIENT_EXECUTIONS, value))


def jitter_seconds(key: str, attempt: int, cap: int = JITTER_CAP_SECONDS) -> int:
    """Deterministic 0..cap jitter so tests and controllers agree."""

    if attempt < 0:
        raise LifecycleRecoveryError("attempt must be non-negative")
    if cap < 0:
        raise LifecycleRecoveryError("jitter cap must be non-negative")
    digest = hashlib.sha256(f"{key}:{attempt}".encode("utf-8")).digest()
    return int(digest[0] % (cap + 1)) if cap else 0


def exponential_backoff_seconds(attempt: int, base: int = BASE_BACKOFF_SECONDS) -> int:
    """Canonical schedule delay for execution index 0..9.

    Index 0 runs immediately; retries 1..9 wait ~10s, 30s, 60s, 3m, 5m,
    10m, 20m, 40m, 60m.  ``base`` is retained for signature compatibility
    and is ignored: the schedule is the contract.
    """

    if attempt < 0:
        raise LifecycleRecoveryError("attempt must be non-negative")
    if attempt < len(RETRY_DELAY_SCHEDULE):
        return RETRY_DELAY_SCHEDULE[attempt]
    return RETRY_DELAY_SCHEDULE[-1]


def retry_delay_schedule() -> tuple[int, ...]:
    """Return the canonical per-execution delay schedule (index 0..9)."""

    return RETRY_DELAY_SCHEDULE


def next_retry_delay_seconds(
    *,
    attempt: int,
    operation_key_value: str = "",
    retry_after_seconds: Optional[int] = None,
    ratelimit_reset_epoch: Optional[int] = None,
    provider_reset_epoch: Optional[int] = None,
    now_epoch: Optional[int] = None,
) -> int:
    """Reset-aware delay honoring Retry-After / reset epochs as minimum.

    The canonical schedule plus bounded jitter is the floor; any
    ``Retry-After``, ``X-RateLimit-Reset``, or provider reset wait that is
    longer becomes the delay.  ``provider_reset_epoch`` is an explicit alias
    for a non-GitHub provider reset so callers can name it without routing
    through the GitHub header name.
    """

    if attempt < 0:
        raise LifecycleRecoveryError("attempt must be non-negative")
    delay = exponential_backoff_seconds(attempt) + jitter_seconds(
        operation_key_value or "lifecycle", attempt
    )
    candidates = [delay]
    if retry_after_seconds is not None and retry_after_seconds >= 0:
        candidates.append(int(retry_after_seconds))
    for reset_epoch in (ratelimit_reset_epoch, provider_reset_epoch):
        if reset_epoch is not None and now_epoch is not None:
            try:
                wait = int(reset_epoch) - int(now_epoch)
            except (TypeError, ValueError) as exc:
                raise LifecycleRecoveryError("reset/now epochs must be integers") from exc
            candidates.append(max(0, wait))
    return max(candidates)


def should_defer_dispatch(delay_seconds: int) -> bool:
    """Whether the runner must exit and let the schedule redispatch later."""

    try:
        delay = int(delay_seconds)
    except (TypeError, ValueError) as exc:
        raise LifecycleRecoveryError("delay must be an integer") from exc
    return delay > MAX_INLINE_WAIT_SECONDS


def retry_marker(head_sha: str, kind: str, attempt: int, *, not_before_epoch: Optional[int] = None) -> str:
    head = normalize_head(head_sha)
    normalized_kind = normalize_kind(kind)
    if attempt < 0:
        raise LifecycleRecoveryError("attempt must be non-negative")
    suffix = f" not-before={int(not_before_epoch)}" if not_before_epoch else ""
    return (
        f"<!-- continuum-lifecycle-retry head={head} "
        f"kind={normalized_kind} attempt={int(attempt)}{suffix} -->"
    )


def exhausted_marker(head_sha: str, kind: str, attempts: int = MAX_TRANSIENT_ATTEMPTS) -> str:
    head = normalize_head(head_sha)
    normalized_kind = normalize_kind(kind)
    return (
        f"<!-- continuum-lifecycle-retry-exhausted head={head} "
        f"kind={normalized_kind} attempts={int(attempts)} -->"
    )


def retry_evidence(
    comments: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    kind: str,
    max_executions: int = MAX_TRANSIENT_ATTEMPTS,
) -> RetryEvidence:
    """Read trusted durable retry markers for one exact operation.

    Both the canonical ``continuum-lifecycle-retry`` markers and the legacy
    Work Lock #38 ``continuum-pr-agent-retry`` markers count for
    review/repair kinds so existing durable state survives generalization.
    ``not-before`` is read from the single canonical marker itself (the
    ``not-before=<epoch>`` field inside the trailing ``-->``); a stray
    ``not-before=`` outside any marker is ignored so producers cannot diverge.
    """

    head = normalize_head(head_sha)
    normalized_kind = normalize_kind(kind)
    budget = resolve_max_executions(max_executions)

    latest_attempt: Optional[int] = None
    latest_marker_at: Optional[datetime] = None
    not_before: Optional[int] = None
    exhausted = False

    def consider(
        match_head: str,
        match_kind: str,
        attempt_text: str,
        marker_not_before: Optional[int],
        created_at: Optional[datetime],
    ) -> None:
        nonlocal latest_attempt, latest_marker_at, not_before
        if match_head.lower() != head or match_kind.lower() != normalized_kind:
            return
        attempt = int(attempt_text)
        if latest_attempt is None or attempt > latest_attempt:
            latest_attempt = attempt
            latest_marker_at = created_at
            not_before = marker_not_before
        elif attempt == latest_attempt and created_at is not None:
            if latest_marker_at is None or created_at > latest_marker_at:
                latest_marker_at = created_at
                not_before = marker_not_before if marker_not_before is not None else not_before

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
        if normalized_kind in PR_AGENT_KINDS:
            for match in _LEGACY_RETRY_RE.finditer(body):
                marker_head, marker_kind, attempt_text = match.groups()
                # Legacy markers predate durable not-before and carry none:
                # a stray ``not-before=`` outside any canonical marker is
                # ignored so producers cannot diverge.
                consider(marker_head, marker_kind, attempt_text, None, created_at)
        for match in _LIFECYCLE_EXHAUSTED_RE.finditer(body):
            marker_head, marker_kind, attempts_text = match.groups()
            if marker_head.lower() == head and marker_kind.lower() == normalized_kind:
                if int(attempts_text) >= budget:
                    exhausted = True
        if normalized_kind in PR_AGENT_KINDS:
            for match in _LEGACY_EXHAUSTED_RE.finditer(body):
                marker_head, marker_kind, attempts_text = match.groups()
                if marker_head.lower() == head and marker_kind.lower() == normalized_kind:
                    if int(attempts_text) >= budget:
                        exhausted = True

    return RetryEvidence(
        latest_attempt=latest_attempt,
        latest_marker_at=latest_marker_at,
        exhausted=exhausted,
        not_before_epoch=not_before,
    )


def _next_attempt(
    *,
    operation_seen: bool,
    evidence: RetryEvidence,
    marker_newer_than_status: bool,
) -> int:
    if evidence.latest_attempt is None:
        return 1 if operation_seen else 0
    if marker_newer_than_status:
        return evidence.latest_attempt
    return evidence.latest_attempt + 1


def decide_recovery(
    *,
    ci_green: bool,
    operation_state: Optional[str] = None,
    operation_description: str = "",
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
    stale_after_seconds: int = STALE_AFTER_SECONDS,
    dispatch_grace_seconds: int = DISPATCH_GRACE_SECONDS,
    max_executions: int = MAX_TRANSIENT_ATTEMPTS,
    operation_key_value: str = "lifecycle",
) -> RecoveryDecision:
    """Reconcile one lifecycle operation from latest authoritative state.

    Fail-closed: anything that is not a proven transient infrastructure gap
    waits or holds without consuming the transient budget.  An old HEAD can
    never authorize a mutation for a new HEAD (``head_moved`` holds); a
    success or a new HEAD starts a new episode and resets the budget.
    Exhaustion after the bounded budget is fail-closed and observable
    (``exhaust`` once, then ``hold`` while the durable marker exists).
    """

    budget = resolve_max_executions(max_executions)
    if head_moved:
        return RecoveryDecision("hold", None, "HEAD moved: old recovery cannot mutate new HEAD")
    if malformed_state:
        return RecoveryDecision("hold", None, "malformed lifecycle state is deterministic")
    if not ci_green:
        return RecoveryDecision("wait", None, "exact HEAD CI is not green")
    if evidence.exhausted:
        return RecoveryDecision("hold", None, "retry budget already exhausted")
    if active_exact_lease or active_operation:
        return RecoveryDecision("wait", None, "exact PR/HEAD operation already owns the lease")

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
    if not_before_epoch is not None and now_epoch is not None:
        try:
            if int(now_epoch) < int(not_before_epoch):
                return RecoveryDecision(
                    "wait", None, "reset-aware not-before time has not arrived",
                    not_before_epoch=int(not_before_epoch),
                )
        except (TypeError, ValueError) as exc:
            raise LifecycleRecoveryError("not-before/now epochs must be integers") from exc
    if evidence.not_before_epoch is not None and now_epoch is not None:
        if int(now_epoch) < int(evidence.not_before_epoch):
            return RecoveryDecision(
                "wait", None, "durable not-before time has not arrived",
                not_before_epoch=int(evidence.not_before_epoch),
            )

    recoverable = False
    transient_failure = False
    operation_seen = state is not None

    if state is None:
        # Lost wakeup: no operation state for a healthy open PR HEAD.
        recoverable = True
        transient_failure = True
    elif state == "success":
        return RecoveryDecision("settled", None, "operation already settled")
    elif state == "failure":
        if failure_transient is True:
            recoverable = True
            transient_failure = True
        elif failure_transient is False:
            return RecoveryDecision("hold", None, "deterministic failure is not automatically retried")
        elif "recovery eligible" in description.lower() or "transient" in description.lower():
            recoverable = True
            transient_failure = True
        else:
            if merge_blocked_deterministic:
                return RecoveryDecision("hold", None, "deterministic merge block is not automatically retried")
            return RecoveryDecision("hold", None, "deterministic failure is not automatically retried")
    elif state == "pending":
        if conclusion in RETRYABLE_RUN_CONCLUSIONS:
            recoverable = True
            transient_failure = True
        elif (
            status_age_seconds is not None
            and status_age_seconds >= stale_after_seconds
            and conclusion not in {"queued", "in_progress", "waiting", "requested"}
        ):
            recoverable = True
            transient_failure = True
        else:
            return RecoveryDecision("wait", None, "pending operation is not stale")
    else:
        return RecoveryDecision("hold", None, f"unknown operation state: {state}")

    if not recoverable:
        return RecoveryDecision("hold", None, "operation is not recoverable")
    if not transient_failure:
        return RecoveryDecision("hold", None, "only transient failures consume the retry budget")

    attempt = _next_attempt(
        operation_seen=operation_seen,
        evidence=evidence,
        marker_newer_than_status=marker_newer_than_status,
    )
    if attempt >= budget:
        return RecoveryDecision("exhaust", None, "automatic transient budget exhausted")

    delay = next_retry_delay_seconds(
        attempt=attempt,
        operation_key_value=operation_key_value,
        retry_after_seconds=retry_after_seconds,
        ratelimit_reset_epoch=ratelimit_reset_epoch,
        provider_reset_epoch=provider_reset_epoch,
        now_epoch=now_epoch,
    )
    computed_not_before: Optional[int] = None
    if now_epoch is not None and (
        retry_after_seconds is not None
        or ratelimit_reset_epoch is not None
        or provider_reset_epoch is not None
    ):
        computed_not_before = int(now_epoch) + delay
    return RecoveryDecision(
        "dispatch",
        attempt,
        "lost/missing operation" if state is None else "transient/stale operation",
        not_before_epoch=computed_not_before,
        defer_dispatch=should_defer_dispatch(delay),
    )


def forward_progress_clears(operation_state: Optional[str]) -> bool:
    """Successful forward progress clears obsolete durable retry state."""

    return str(operation_state or "").strip().lower() == "success"


def dedupe_wakeups(keys: Sequence[str]) -> list[str]:
    """Coalesce multiple wakeups for the same PR/HEAD into one operation."""

    seen: dict[str, None] = {}
    for key in keys:
        normalized = str(key or "").strip()
        if normalized and normalized not in seen:
            seen[normalized] = None
    return list(seen.keys())


def should_coalesce(active_leases: Sequence[str], key: str) -> bool:
    """Whether a wakeup must stand down because its lease is already owned."""

    normalized = str(key or "").strip()
    return normalized in {str(item or "").strip() for item in active_leases}


def requires_pat(action: object) -> bool:
    """Token policy: reads use GITHUB_TOKEN; only mutations require PAT."""

    name = str(action or "").strip().lower().replace("-", "_")
    pat_actions = {
        "merge", "dispatch", "comment", "label", "review_submit",
        "update_branch", "push", "release", "mutate", "write",
    }
    read_actions = {
        "read", "list", "get", "discover", "scan", "reconcile_read",
        "status", "check", "poll",
    }
    if name in pat_actions:
        return True
    if name in read_actions:
        return False
    # Fail closed: unknown actions keep PAT semantics rather than silently
    # widening GITHUB_TOKEN use.
    return True


__all__ = [
    "MAX_TRANSIENT_ATTEMPTS",
    "MAX_TRANSIENT_EXECUTIONS",
    "MIN_TRANSIENT_EXECUTIONS",
    "RETRY_DELAY_SCHEDULE",
    "BASE_BACKOFF_SECONDS",
    "JITTER_CAP_SECONDS",
    "STALE_AFTER_SECONDS",
    "DISPATCH_GRACE_SECONDS",
    "MAX_INLINE_WAIT_SECONDS",
    "TRUSTED_ASSOCIATIONS",
    "RETRYABLE_RUN_CONCLUSIONS",
    "KINDS",
    "PR_AGENT_KINDS",
    "LifecycleRecoveryError",
    "FailureClassification",
    "RetryEvidence",
    "RecoveryDecision",
    "normalize_head",
    "normalize_kind",
    "operation_key",
    "concurrency_key",
    "classify_infrastructure_failure",
    "classify_operation_failure",
    "resolve_max_executions",
    "retry_delay_schedule",
    "jitter_seconds",
    "exponential_backoff_seconds",
    "next_retry_delay_seconds",
    "should_defer_dispatch",
    "retry_marker",
    "exhausted_marker",
    "retry_evidence",
    "decide_recovery",
    "forward_progress_clears",
    "dedupe_wakeups",
    "should_coalesce",
    "requires_pat",
]
