"""Resilient delegated PR-Agent fan-out (kodmial/continuum#289).

A single transient ``workflow_dispatch`` failure (for example GitHub HTTP 500
from ``gh workflow run continuum-pr-agent-recovery.yml``) must not abort the
whole scheduler pass.  This module owns the failure classifier and the
bounded retry schedule so the workflow shell and the regression tests share
one authoritative policy:

- transient: HTTP 5xx, HTTP 429 / rate-limit signals, and 403 only when it
  carries a rate-limit/transient marker.  These are worth a bounded retry
  with a minimum 5s backoff between attempts (never a tight loop).
- deterministic: anything else (400/401/403 without a transient marker,
  404 unknown workflow, 422 bad input, auth failures).  These fail closed on
  the first attempt and are never retried as transient.

Retries never duplicate logical review work: a failed ``workflow_dispatch``
creates no run, so a retry only re-issues a dispatch that never landed.  A
recovery run that does land dedups downstream through the stable PR-Agent
operation identity across scheduler/event/recovery wakeups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

#: Total dispatch attempts per child (1 initial + bounded retries).
MAX_DISPATCH_ATTEMPTS = 3

#: Minimum delay between dispatch attempts.  Never retry in a tight loop.
MIN_RETRY_DELAY_SECONDS = 5.0

#: Bounded inter-attempt backoff.  Length is MAX_DISPATCH_ATTEMPTS - 1 and
#: every entry is >= MIN_RETRY_DELAY_SECONDS.
RETRY_DELAYS: Sequence[float] = (5.0, 10.0)

_STATUS_RE = re.compile(r"(^|[^0-9])([0-9]{3})([^0-9]|$)")

_TRANSIENT_403_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "secondary rate",
    "abuse",
    "try again",
    "please retry",
    "temporarily",
    "transient",
    "timeout",
    "timed out",
    "service unavailable",
    "retry",
)

_GENERIC_TRANSIENT_MARKERS = (
    "try again",
    "temporarily",
    "service unavailable",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "broken pipe",
    "socket",
    "eof",
)

_DETERMINISTIC_STATUSES = frozenset({400, 401, 403, 404, 422})


def _statuses(text: str) -> set[int]:
    found: set[int] = set()
    for match in _STATUS_RE.finditer(text or ""):
        try:
            found.add(int(match.group(2)))
        except ValueError:
            continue
    return found


def classify_dispatch_failure(output: str) -> str:
    """Classify one failed dispatch output.

    Returns ``"transient"`` when the failure is worth a bounded retry and
    ``"deterministic"`` otherwise.  Unknown/empty output fails closed as
    deterministic so bad workflow/input/auth problems are never retried
    as transient forever.
    """
    text = output or ""
    lower = text.lower()
    statuses = _statuses(text)

    if 429 in statuses:
        return "transient"
    if any(500 <= status <= 599 for status in statuses):
        return "transient"
    if (
        "rate limit" in lower
        or "rate-limit" in lower
        or "ratelimit" in lower
        or "secondary rate" in lower
        or "abuse" in lower
    ):
        return "transient"
    if 403 in statuses:
        if any(marker in lower for marker in _TRANSIENT_403_MARKERS):
            return "transient"
        return "deterministic"
    if any(status in statuses for status in _DETERMINISTIC_STATUSES):
        return "deterministic"
    if any(marker in lower for marker in _GENERIC_TRANSIENT_MARKERS):
        return "transient"
    return "deterministic"


def is_transient_dispatch_failure(output: str) -> bool:
    """Return True only for retryable transient dispatch failures."""
    return classify_dispatch_failure(output) == "transient"


def retry_delays(
    max_attempts: int = MAX_DISPATCH_ATTEMPTS,
) -> Sequence[float]:
    """Return the bounded inter-attempt delays for ``max_attempts``.

    Every delay is >= MIN_RETRY_DELAY_SECONDS so retries never spin in a
    tight loop, and the schedule is bounded (never unbounded).
    """
    attempts = max(1, int(max_attempts))
    delays = list(RETRY_DELAYS[: max(0, attempts - 1)])
    while len(delays) < max(0, attempts - 1):
        last = delays[-1] if delays else MIN_RETRY_DELAY_SECONDS
        delays.append(min(last * 2, 60.0))
    return tuple(delays)


@dataclass
class DispatchOutcome:
    """Result of one bounded dispatch sequence."""

    succeeded: bool
    attempts: int
    transient: bool = False
    deterministic: bool = False


def run_with_retry(
    dispatch: Callable[[], tuple[bool, str]],
    max_attempts: int = MAX_DISPATCH_ATTEMPTS,
    delays: Optional[Sequence[float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> DispatchOutcome:
    """Run ``dispatch`` under the bounded transient retry budget.

    ``dispatch`` returns ``(ok, output)`` per attempt.  Transient failures
    sleep through the bounded schedule and retry; deterministic failures
    return immediately without further attempts.  The caller decides how to
    aggregate per-child outcomes so one exhausted target never aborts the
    fan-out over unrelated children.
    """
    attempts = max(1, int(max_attempts))
    schedule = list(delays) if delays is not None else list(retry_delays(attempts))
    waiter = sleep if sleep is not None else (lambda _: None)
    last_output = ""
    for attempt in range(1, attempts + 1):
        ok, output = dispatch()
        last_output = output or ""
        if ok:
            return DispatchOutcome(succeeded=True, attempts=attempt)
        if classify_dispatch_failure(last_output) != "transient":
            return DispatchOutcome(
                succeeded=False, attempts=attempt, deterministic=True
            )
        if attempt < attempts:
            index = attempt - 1
            delay = schedule[index] if index < len(schedule) else MIN_RETRY_DELAY_SECONDS
            waiter(max(float(delay), MIN_RETRY_DELAY_SECONDS))
    return DispatchOutcome(
        succeeded=False, attempts=attempts, transient=True
    )
