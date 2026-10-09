"""CodeRabbit provider adapter (kodmial/continuum#304).

This module owns genuinely provider-specific CodeRabbit semantics and
nothing else:

- approval / review basis;
- unresolved / nitpick state;
- quota / rate-limit semantics;
- CodeRabbit review commands;
- carried-approval policy.

It mirrors the provider-owned JavaScript embedded in
``continuum-auto-merge.yml`` (retry parser, rate-limit selection,
review-command detection), ``continuum-coderabbit-retry.yml``, and
``continuum-coderabbit-unresolved.yml``. It never implements shared
lifecycle behavior (HEAD reconciliation, CI/gate validation,
``no-auto-merge``, main-sync, conflict plumbing, packaging/required
gates, exact-HEAD merge, merge titles, post-merge wakeups,
idempotency): those live in :mod:`continuum.merge_lifecycle`.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Quota fallback when a rate-limit comment carries no parseable
#: countdown. Mirrors the workflow warning path byte-for-byte.
QUOTA_FALLBACK_MS = 65 * 60_000

#: Safety padding added to a parsed quota window before retry.
RETRY_SAFETY_PADDING_MS = 30_000

#: Thread-verification loss timeout: a lost CodeRabbit
#: thread-verification reply never strands a PR forever.
THREAD_VERIFICATION_TIMEOUT_MS = 30 * 60_000

_RETRY_WINDOW_PATTERNS = (
    re.compile(
        r"next\s+(?:included\s+)?review\b[^\n<.]{0,120}?\bavailable\b"
        r"[^\n<.]{0,40}?\b(?:in|after)\s+([^\n<.]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:try|retry)\s+again\b[^\n<.]{0,40}?\b(?:in|after)\s+([^\n<.]+)",
        re.IGNORECASE,
    ),
)

_DURATION_PART_RE = re.compile(
    r"(\d+)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)", re.IGNORECASE
)

_RATE_LIMIT_RE = re.compile(
    r"Review rate limited|Review limit reached", re.IGNORECASE
)

_REVIEW_COMMAND_RE = re.compile(
    r"^@coderabbitai\s+(?:full\s+)?review\s*$", re.IGNORECASE
)


def retry_window_text(body: Any) -> Optional[str]:
    """Extract the quota-countdown window text from a bot comment."""

    text = str(body or "")
    for pattern in _RETRY_WINDOW_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def parse_duration_ms(text: Any) -> Optional[int]:
    """Parse a human quota countdown into milliseconds."""

    value = str(text or "")
    if re.search(r"less than\s+(?:a|one)\s+minute", value, re.IGNORECASE):
        return 60_000
    total = 0
    found = False
    for amount, unit in _DURATION_PART_RE.findall(value):
        found = True
        number = int(amount)
        lowered = unit.lower()
        if lowered.startswith("hour") or lowered.startswith("hr"):
            total += number * 3_600_000
        elif lowered.startswith("minute") or lowered.startswith("min"):
            total += number * 60_000
        else:
            total += number * 1_000
    return total if found else None


def parse_retry_delay_ms(body: Any) -> Optional[int]:
    """Parse a CodeRabbit quota comment into a retry delay.

    Returns the parsed window, the 65-minute fallback for quota prose
    without a countdown, or ``None`` when the comment carries no quota
    signal at all.
    """

    text = str(body or "")
    window = retry_window_text(text)
    parsed = parse_duration_ms(window) if window is not None else None
    if parsed is not None:
        return parsed
    if _RATE_LIMIT_RE.search(text):
        return QUOTA_FALLBACK_MS
    return None


def is_rate_limited_status(description: Any) -> bool:
    """Whether a CodeRabbit status description reports quota exhaustion."""

    return _RATE_LIMIT_RE.search(str(description or "")) is not None


def latest_rate_limit(
    comments: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Select the newest CodeRabbit comment carrying a quota delay."""

    candidates = []
    for comment in comments or []:
        user = comment.get("user") if isinstance(comment, Mapping) else None
        login = ""
        if isinstance(user, Mapping):
            login = str(user.get("login") or "")
        if login != "coderabbitai[bot]":
            continue
        body = comment.get("body") if isinstance(comment, Mapping) else ""
        delay = parse_retry_delay_ms(body)
        if delay is None:
            continue
        candidates.append({"comment": comment, "delay_ms": delay})
    if not candidates:
        return None

    def _updated(entry: Dict[str, Any]) -> str:
        comment = entry["comment"]
        if isinstance(comment, Mapping):
            return str(comment.get("updated_at") or comment.get("created_at") or "")
        return ""

    candidates.sort(key=_updated, reverse=True)
    return candidates[0]


def retry_due_at_ms(limited_at_ms: int, delay_ms: int) -> int:
    """Compute the earliest retry timestamp for a quota response."""

    return int(limited_at_ms) + int(delay_ms) + RETRY_SAFETY_PADDING_MS


def review_command_posted_after(
    comments: Sequence[Mapping[str, Any]], timestamp_ms: int
) -> bool:
    """Whether a human posted ``@coderabbitai review`` after a timestamp."""

    for comment in comments or []:
        if not isinstance(comment, Mapping):
            continue
        user = comment.get("user")
        login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
        if login == "coderabbitai[bot]":
            continue
        body = str(comment.get("body") or "").strip()
        if not _REVIEW_COMMAND_RE.match(body):
            continue
        created = str(comment.get("created_at") or "")
        if not created:
            continue
        try:
            from datetime import datetime, timezone

            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            created_ms = int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError):
            continue
        if created_ms > int(timestamp_ms):
            return True
    return False


def unresolved_blocking(unresolved_count: Any) -> bool:
    """Whether CodeRabbit unresolved threads block the merge."""

    if unresolved_count is None or unresolved_count == "":
        return True
    try:
        return int(unresolved_count or 0) > 0
    except (TypeError, ValueError):
        return True


def approval_basis(decision: Any) -> Tuple[bool, str]:
    """Evaluate the CodeRabbit approval/review basis for one HEAD.

    ``decision`` is a mapping with ``state`` (``APPROVED`` /
    ``CHANGES_REQUESTED`` / other) and ``summary_only``. Only an
    ``APPROVED`` decision approves; summary-only ``CHANGES_REQUESTED``
    is explicitly not terminal (the serialized queue owns cooldown and
    re-review).
    """

    if not isinstance(decision, Mapping):
        return False, "no CodeRabbit decision"
    state = str(decision.get("state") or "").strip().upper()
    if state == "APPROVED":
        return True, "CodeRabbit approved"
    if state == "CHANGES_REQUESTED" and bool(decision.get("summary_only")):
        return False, "summary-only CHANGES_REQUESTED is not terminal"
    return False, "CodeRabbit has not approved: {}".format(state or "missing")


def carried_approval_valid(approved_head: Any, live_head: Any) -> bool:
    """Whether a prior CodeRabbit approval carries to the live HEAD.

    Approvals never survive a HEAD move or a main-sync: only the exact
    approved HEAD may merge.
    """

    expected = str(approved_head or "").strip().lower()
    actual = str(live_head or "").strip().lower()
    if not expected or not actual:
        return False
    return expected == actual


__all__ = [
    "QUOTA_FALLBACK_MS",
    "RETRY_SAFETY_PADDING_MS",
    "THREAD_VERIFICATION_TIMEOUT_MS",
    "approval_basis",
    "carried_approval_valid",
    "is_rate_limited_status",
    "latest_rate_limit",
    "parse_duration_ms",
    "parse_retry_delay_ms",
    "retry_due_at_ms",
    "retry_window_text",
    "review_command_posted_after",
    "unresolved_blocking",
]
