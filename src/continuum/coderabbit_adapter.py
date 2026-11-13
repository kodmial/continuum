"""CodeRabbit provider adapter (kodmial/continuum#304).

This module owns genuinely provider-specific CodeRabbit semantics and
nothing else:

- approval / review basis;
- unresolved / nitpick state;
- quota / rate-limit semantics, including the distinction between the
  shared full-review quota and the inline chat-message/hour quota;
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

#: The only login allowed to speak for CodeRabbit. A copied reply from any
#: other author is never a quota signal or a finding verdict.
CODERABBIT_BOT_LOGIN = "coderabbitai[bot]"

#: Inline chat-message quota fallback. CodeRabbit rate-limits ``chat``
#: messages per hour, so an unparseable countdown fails closed for one full
#: hour plus a small margin instead of guessing a short wait.
CHAT_QUOTA_FALLBACK_MS = 65 * 60_000

#: Safety padding added to the bot reply timestamp + chat countdown.
CHAT_RETRY_SAFETY_PADDING_MS = 30_000

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

# Inline chat-message quota lives in a PR review-comment thread. Its heading
# and prose are matched only on unquoted Markdown lines, so a human comment
# that pastes the provider text can never be mistaken for a quota reply.
_CHAT_RATE_LIMIT_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]*Rate[ \t]+Limit[ \t]+Exceeded[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
_CHAT_RATE_LIMIT_PROSE_RE = re.compile(
    r"^[ \t]*(?!>)[^\n]*\b(?:exceeded|reached)\b[^\n]*\blimit\b"
    r"[^\n]*\bchat messages? per hour\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CHAT_RATE_LIMIT_NEGATED_RE = re.compile(
    r"\b(?:not|never|no)\b[^\n]{0,80}\b(?:exceeded|reached)\b",
    re.IGNORECASE,
)
_CHAT_COUNTDOWN_RE = re.compile(
    r"please\s+wait\s+\*{0,2}\s*"
    r"((?:less\s+than\s+(?:a|one)\s+minute)|[0-9][^\n*]*?)"
    r"\s*\*{0,2}\s+before\s+sending\s+another\s+message",
    re.IGNORECASE,
)

_CHAT_RETRY_MARKER_TEMPLATE = (
    "<!-- auto-merge-coderabbit-chat-quota-retry finding={finding} "
    "head={head} limited={limited} -->"
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


def is_trusted_coderabbit_author(login: Any) -> bool:
    """Whether ``login`` is the real CodeRabbit bot account."""

    return str(login or "").strip().lower() == CODERABBIT_BOT_LOGIN


def parse_chat_retry_delay_ms(body: Any) -> Optional[int]:
    """Parse an inline CodeRabbit chat-message quota reply.

    The countdown is anchored to the reply text
    ``Please wait **14 minutes and 56 seconds** before sending another
    message.`` A malformed countdown still carries the chat-quota prose, so
    it fails closed with :data:`CHAT_QUOTA_FALLBACK_MS`. A body without a
    chat-quota signal at all returns ``None``.
    """

    text = str(body or "")
    if not _CHAT_RATE_LIMIT_HEADING_RE.search(text):
        if not _CHAT_RATE_LIMIT_PROSE_RE.search(text):
            return None
        # A finding may discuss a quota that the user did NOT reach.
        if _CHAT_RATE_LIMIT_NEGATED_RE.search(text):
            return None
    match = _CHAT_COUNTDOWN_RE.search(text)
    if match is None:
        return CHAT_QUOTA_FALLBACK_MS
    parsed = parse_duration_ms(match.group(1))
    return CHAT_QUOTA_FALLBACK_MS if parsed is None else parsed


def chat_retry_not_before_ms(created_at: Any, delay_ms: int) -> Optional[int]:
    """Compute the chat-quota not-before from the bot reply timestamp.

    ``created_at`` is the immutable review-comment timestamp. A missing or
    malformed timestamp returns ``None`` so the caller fails closed instead
    of sending a verification it cannot time.
    """

    from datetime import datetime, timezone

    raw = str(created_at or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        epoch_ms = int(parsed.timestamp() * 1000)
    except (OverflowError, OSError):
        return None
    return epoch_ms + int(delay_ms) + CHAT_RETRY_SAFETY_PADDING_MS


def classify_coderabbit_reply(
    body: Any,
    *,
    author_login: Any = None,
) -> Dict[str, Any]:
    """Classify one provider reply without trusting quoted text.

    ``kind`` is ``untrusted`` for any author other than the real bot,
    ``chat_rate_limit`` for the inline chat-message quota,
    ``review_rate_limit`` for the full-review quota, and ``verdict`` for a
    substantive finding answer. The two quota forms are deliberately
    distinct: a chat rejection never consumes or resets a full review slot.
    """

    if author_login is not None and not is_trusted_coderabbit_author(
        author_login
    ):
        return {"kind": "untrusted", "delay_ms": None}
    chat_delay = parse_chat_retry_delay_ms(body)
    if chat_delay is not None:
        return {"kind": "chat_rate_limit", "delay_ms": chat_delay}
    review_delay = parse_retry_delay_ms(body)
    if review_delay is not None:
        return {"kind": "review_rate_limit", "delay_ms": review_delay}
    return {"kind": "verdict", "delay_ms": None}


def is_provider_quota_reply(
    body: Any, *, author_login: Any = None
) -> bool:
    """Whether a reply is either CodeRabbit quota form, never a verdict."""

    return classify_coderabbit_reply(
        body, author_login=author_login
    )["kind"] in ("chat_rate_limit", "review_rate_limit")


def latest_chat_rate_limit(
    comments: Sequence[Mapping[str, Any]],
    *,
    root_comment_id: Any = None,
    after_comment_id: Any = None,
) -> Optional[Dict[str, Any]]:
    """Select the newest trusted chat-quota reply for one review thread."""

    candidates = []
    for comment in comments or []:
        if not isinstance(comment, Mapping):
            continue
        if root_comment_id is not None:
            if comment.get("in_reply_to_id") != root_comment_id:
                continue
        if after_comment_id is not None:
            try:
                if int(comment.get("id") or 0) <= int(after_comment_id):
                    continue
            except (TypeError, ValueError):
                continue
        user = comment.get("user")
        login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
        if not is_trusted_coderabbit_author(login):
            continue
        delay = parse_chat_retry_delay_ms(comment.get("body"))
        if delay is None:
            continue
        candidates.append({"comment": comment, "delay_ms": delay})
    if not candidates:
        return None
    candidates.sort(
        key=lambda entry: int(entry["comment"].get("id") or 0), reverse=True
    )
    return candidates[0]


def chat_retry_marker(
    finding_id: Any, head_sha: Any, limited_comment_id: Any
) -> str:
    """Durable exact-finding/HEAD/quota-reply retry acknowledgement marker."""

    return _CHAT_RETRY_MARKER_TEMPLATE.format(
        finding=int(finding_id),
        head=str(head_sha or "").strip().lower(),
        limited=int(limited_comment_id),
    )


def chat_retry_acknowledged(
    comments: Sequence[Mapping[str, Any]],
    *,
    finding_id: Any,
    head_sha: Any,
    limited_comment_id: Any,
    root_comment_id: Any = None,
    trusted_author_logins: Sequence[Any] = (),
) -> bool:
    """Whether this exact chat-quota retry was already sent.

    The marker is stored in a thread reply, so it survives workflow
    cancellation, controller restarts, and concurrent wakeups. Only the
    accounts that post auto-merge verification replies may acknowledge the
    retry; a copied marker from any other commenter never suppresses it.
    """

    allowed = {
        str(login or "").strip().lower()
        for login in trusted_author_logins or ()
        if str(login or "").strip()
    }
    if not allowed:
        return False
    marker = chat_retry_marker(finding_id, head_sha, limited_comment_id)
    for comment in comments or []:
        if not isinstance(comment, Mapping):
            continue
        if root_comment_id is not None and comment.get("in_reply_to_id") != root_comment_id:
            continue
        user = comment.get("user")
        login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
        if login.strip().lower() not in allowed:
            continue
        if marker in str(comment.get("body") or ""):
            return True
    return False


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
    "CHAT_QUOTA_FALLBACK_MS",
    "CHAT_RETRY_SAFETY_PADDING_MS",
    "CODERABBIT_BOT_LOGIN",
    "QUOTA_FALLBACK_MS",
    "RETRY_SAFETY_PADDING_MS",
    "THREAD_VERIFICATION_TIMEOUT_MS",
    "approval_basis",
    "carried_approval_valid",
    "chat_retry_acknowledged",
    "chat_retry_marker",
    "chat_retry_not_before_ms",
    "classify_coderabbit_reply",
    "is_provider_quota_reply",
    "is_rate_limited_status",
    "is_trusted_coderabbit_author",
    "latest_chat_rate_limit",
    "latest_rate_limit",
    "parse_chat_retry_delay_ms",
    "parse_duration_ms",
    "parse_retry_delay_ms",
    "retry_due_at_ms",
    "retry_window_text",
    "review_command_posted_after",
    "unresolved_blocking",
]
