"""CodeRabbit review adapter.

A sibling provider behind the same normalized review-gate contract. CodeRabbit
publishes a commit status, a review verdict, inline threads, and a summary
comment; this adapter translates all of it into a `ProviderSnapshot` and the
generic gate decides the verdict.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .findings import collect_summary_comments, parse_time, sanitize_title
from .snapshot import ProviderSnapshot
from .tracker import BOT_PREFIX, TRACKER_MARKER, VERDICT_MARKER

PROVIDER_NAME = "coderabbit"

DEFAULT_BOT_LOGIN = "coderabbitai[bot]"
DEFAULT_STATUS_CONTEXT = "CodeRabbit"
COMPLETED_RE = re.compile(r"review completed", re.I)

OUTPUT_MARKERS = ("coderabbit", "walkthrough", "review details", "nitpick")

# CodeRabbit's shared included-review quota is a repository-wide resource, so
# the queue must serialize against it. A rate-limit notice is the provider
# telling us when the next slot exists; the countdown is parsed here, once.
RATE_LIMIT_RE = re.compile(r"review rate limited|review limit reached", re.I)
FULL_REVIEW_COMMAND = "@coderabbitai full review"
_RETRY_WINDOW_RES = (
    re.compile(
        r"next\s+(?:included\s+)?review\b[^\n<.]{0,120}?\bavailable\b[^\n<.]{0,40}?"
        r"\b(?:in|after)\s+([^\n<.]+)",
        re.I,
    ),
    re.compile(r"(?:try|retry)\s+again\b[^\n<.]{0,40}?\b(?:in|after)\s+([^\n<.]+)", re.I),
)
_DURATION_RE = re.compile(r"(\d+)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)", re.I)


def parse_duration_ms(text: str) -> Optional[int]:
    """Milliseconds named by a provider countdown, or `None`."""

    if re.search(r"less than\s+(?:a|one)\s+minute", text or "", re.I):
        return 60_000
    total = 0
    found = False
    for match in _DURATION_RE.finditer(text or ""):
        found = True
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("hour") or unit.startswith("hr"):
            total += value * 3_600_000
        elif unit.startswith("minute") or unit.startswith("min"):
            total += value * 60_000
        else:
            total += value * 1_000
    return total if found else None


def parse_retry_delay_ms(body: str) -> Optional[int]:
    """Countdown published in a rate-limit notice, or `None` when absent."""

    for pattern in _RETRY_WINDOW_RES:
        match = pattern.search(body or "")
        if match:
            parsed = parse_duration_ms(match.group(1))
            if parsed is not None:
                return parsed
    return None


def is_full_review(review: Dict[str, Any], bot_login: str = DEFAULT_BOT_LOGIN) -> bool:
    """True when a CodeRabbit review consumed a full included review.

    An empty COMMENTED review is a thread confirmation, not quota use.
    """

    if not _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login):
        return False
    if review.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
        return True
    return review.get("state") == "COMMENTED" and bool((review.get("body") or "").strip())


def rate_limit_cooldown(
    client: Any,
    pr_number: int,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
) -> Tuple[int, str]:
    """Newest published retry window for this pull request, as `(until_ms, reason)`."""

    comments = client.list_issue_comments(int(pr_number))
    windows: List[Tuple[int, int]] = []
    for comment in comments or []:
        if not _matches_bot(((comment.get("user") or {}).get("login") or ""), bot_login):
            continue
        if not RATE_LIMIT_RE.search(comment.get("body") or ""):
            continue
        delay = parse_retry_delay_ms(comment.get("body") or "")
        published = parse_time(comment.get("updated_at") or comment.get("created_at"))
        if delay is None or published is None:
            continue
        at = int(published.timestamp() * 1000)
        windows.append((at + delay, at))
    if not windows:
        return 0, ""
    until, _at = max(windows)
    return until, "provider rate limit"


def queue_request(
    settings: Any,
    *,
    pr_number: int,
    head_sha: str = "",
    kind: str = "initial",
) -> Any:
    """CodeRabbit is asked for a full review with a fixed bot mention."""

    from .providers import REQUEST_COMMENT, QueueRequest

    del settings, pr_number, head_sha
    return QueueRequest(kind=REQUEST_COMMENT, provider=PROVIDER_NAME, body=FULL_REVIEW_COMMAND)

# Sections whose bullets can be actionable. The walkthrough and the file table
# describe the diff; they never produce findings on their own.
FINDING_SECTION_HEADINGS = (
    "review details",
    "nitpick",
    "issue",
    "concern",
    "bug",
    "vulnerability",
    "additional comments",
)
BOILERPLATE_TITLE_PHRASES = (
    "looks good",
    "no actionable",
    "no major issues",
    "lgtm",
    "preliminary",
    "walkthrough",
)

_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.*)\s*$")
_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*+])\s+(.{10,300})", re.M)


def is_boilerplate_title(title: str) -> bool:
    lowered = (title or "").lower()
    return any(phrase in lowered for phrase in BOILERPLATE_TITLE_PHRASES)


def select_finding_text(body: str) -> str:
    matches = list(_HEADING_RE.finditer(body or ""))
    if not matches:
        return body or ""
    kept: List[str] = []
    for index, match in enumerate(matches):
        heading = (match.group(1) or "").lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if any(phrase in heading for phrase in FINDING_SECTION_HEADINGS):
            kept.append(body[match.start() : end])
    return "".join(kept)


def parse_summary(body: str) -> List[str]:
    text = select_finding_text(body or "")
    if not text.strip():
        return []
    titles: List[str] = []
    seen = set()
    for match in _BULLET_RE.finditer(text):
        title = sanitize_title(match.group(1))
        if len(title) < 10 or is_boilerplate_title(title):
            continue
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles


def _matches_bot(login: str, bot_login: str) -> bool:
    left = (login or "").lower()
    right = (bot_login or "").lower()
    if not left:
        return False
    return left == right or left.startswith(right.split("[", 1)[0])


def latest_status(statuses: Sequence[Dict[str, Any]], context: str) -> Optional[Dict[str, Any]]:
    matching = [
        status
        for status in statuses or []
        if (status.get("context") or "").lower() == (context or "").lower()
    ]
    if not matching:
        return None
    return sorted(
        matching,
        key=lambda status: str(status.get("updated_at") or status.get("created_at") or ""),
        reverse=True,
    )[0]


def latest_review(
    reviews: Sequence[Dict[str, Any]], bot_login: str, head_sha: str
) -> Optional[Dict[str, Any]]:
    """Newest review by the provider that is bound to the given head."""

    candidates = [
        review
        for review in reviews or []
        if _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login)
        and (review.get("commit_id") or "").lower() == (head_sha or "").lower()
        and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda review: int(review.get("id") or 0))[-1]


def collect(
    client: Any,
    pr_number: int,
    head_sha: str,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    status_context: str = DEFAULT_STATUS_CONTEXT,
) -> ProviderSnapshot:
    """Read CodeRabbit's status, verdict, threads, and summary for this PR."""

    issue_comments = client.list_issue_comments(int(pr_number))
    review_comments = client.list_review_comments(int(pr_number))
    summary_comments = collect_summary_comments(
        issue_comments,
        bot_logins=(bot_login,),
        markers=OUTPUT_MARKERS,
        exclude_markers=(TRACKER_MARKER, VERDICT_MARKER),
        exclude_prefix=BOT_PREFIX,
    )
    provider_review_comments = [
        comment
        for comment in review_comments
        if _matches_bot(((comment.get("user") or {}).get("login") or ""), bot_login)
    ]

    extra_reason: Optional[str] = None
    try:
        status_payload = client.combined_status_for_ref(head_sha)
        status = latest_status(status_payload.get("statuses") or [], status_context)
    except Exception:  # noqa: BLE001 - unknown status must fail closed
        status = None
    if status is None:
        extra_reason = (
            f"the review provider reported no {status_context!r} status for this HEAD, "
            "so the diff is unverified"
        )
    elif status.get("state") != "success" or not COMPLETED_RE.search(
        str(status.get("description") or "")
    ):
        extra_reason = (
            f"the {status_context!r} status for this HEAD is "
            f"{status.get('state') or 'unknown'}: {str(status.get('description') or '')[:200]}"
        )

    reviews = client.list_reviews(int(pr_number))
    decision = latest_review(reviews, bot_login, head_sha)
    decision_state = (decision or {}).get("state")
    forced_titles: List[str] = []
    if decision_state == "CHANGES_REQUESTED":
        # A blocking verdict is evidence of at least one actionable finding even
        # when the report only states it in prose.
        forced_titles = parse_summary((decision or {}).get("body") or "")
        if not forced_titles:
            forced_titles = ["CodeRabbit requested changes on this HEAD without an inline finding."]

    return ProviderSnapshot(
        provider=PROVIDER_NAME,
        bot_logins=(bot_login,),
        review_bots=(bot_login,),
        output_markers=OUTPUT_MARKERS,
        summary_comments=summary_comments,
        review_comments=provider_review_comments,
        unresolved_ids=client.unresolved_thread_comment_ids(int(pr_number)),
        summary_parser=parse_summary,
        is_boilerplate=is_boilerplate_title,
        extra_reason=extra_reason,
        forced_open_titles=forced_titles,
        provider_decision=decision_state,
        metadata={"status_context": status_context, "review_id": (decision or {}).get("id")},
    )


def profile_for(bot_login: str, status_context: str) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    return (bot_login,), (bot_login,), OUTPUT_MARKERS
