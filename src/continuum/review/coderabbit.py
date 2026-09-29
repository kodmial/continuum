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

# The verdict states that decide a pull request. COMMENTED is advisory: a
# nitpick-only review is not a verdict and must never outlive a later decisive
# review on the same head.
DECISIVE_STATES = ("APPROVED", "CHANGES_REQUESTED")

# CodeRabbit re-checks a fixed finding and answers in prose (`RESOLVED`,
# "Review thread resolved") while the GitHub review thread keeps reporting
# `isResolved: false`. Trusting only the flag is deadlock #37 root cause 1. An
# explicit answer in either direction is authoritative; silence is not.
RESOLVED_RE = re.compile(r"(?:^|\W)(?:review\s+thread\s+)?resolved\b", re.I)
UNRESOLVED_RE = re.compile(r"(?:^|\W)unresolved\b", re.I)

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


def _comment_login(comment: Dict[str, Any]) -> str:
    """The comment author, across the REST and GraphQL field names."""

    for key in ("user", "author"):
        login = (comment.get(key) or {}).get("login")
        if login:
            return str(login)
    return str(comment.get("login") or comment.get("user") or "")


def _comment_ms(comment: Dict[str, Any]) -> int:
    """Publication time of a comment, in epoch ms; 0 when unparseable.

    Zero sorts first, which keeps an unparseable timestamp in the older bucket.
    Failing that way is deliberate: output the adapter cannot place in time is
    treated as predating the approval rather than silently escaping it.
    """

    published = parse_time(
        comment.get("created_at")
        or comment.get("createdAt")
        or comment.get("updated_at")
        or comment.get("updatedAt")
    )
    return int(published.timestamp() * 1000) if published else 0


def _review_threads(client: Any, pr_number: int) -> Optional[List[Dict[str, Any]]]:
    """Full thread state, or None when the client cannot read it."""

    reader = getattr(client, "review_threads", None)
    if reader is None:
        return None
    try:
        return reader(pr_number)
    except Exception:  # noqa: BLE001 - unknown thread state must fail closed
        return None


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


def latest_decisive_review(
    reviews: Sequence[Dict[str, Any]], bot_login: str, head_sha: str
) -> Optional[Dict[str, Any]]:
    """Newest APPROVED/CHANGES_REQUESTED review bound to the exact head.

    A `Review skipped` status can overwrite the status surface for a head that
    already carries a durable GitHub review, so the review record, not the
    status, is the acceptance basis. That only works if the review is bound to
    the same immutable SHA CI is bound to.
    """

    candidates = [
        review
        for review in reviews or []
        if _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login)
        and (review.get("commit_id") or "").lower() == (head_sha or "").lower()
        and review.get("state") in DECISIVE_STATES
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda review: int(review.get("id") or 0))[-1]


def approved_at_ms(
    reviews: Sequence[Dict[str, Any]], bot_login: str, head_sha: str
) -> int:
    """Publication time of the newest exact-head APPROVED review, in ms.

    Provider output published strictly before this instant on the same head was
    advisory and has been adjudicated: a later APPROVED supersedes it. Output
    published after it has not, and still blocks.
    """

    approved = latest_decisive_review(reviews, bot_login, head_sha)
    if approved is None or approved.get("state") != "APPROVED":
        return 0
    published = parse_time(approved.get("submitted_at"))
    return int(published.timestamp() * 1000) if published else 0


def thread_resolution(comments: Sequence[Dict[str, Any]], bot_login: str) -> str:
    """What the provider's newest reply in one thread says about it.

    `"resolved"`, `"unresolved"`, or `""` when the provider said nothing. Silence
    is not a resolution: an unanswered thread keeps whatever GitHub's flag says.
    """

    # GraphQL review threads name the author `author` and the time `createdAt`;
    # REST review comments name them `user` and `created_at`. Both reach here.
    replies = [
        comment
        for comment in comments or []
        if _matches_bot(_comment_login(comment), bot_login)
        and str(comment.get("body") or "").strip()
    ]
    if not replies:
        return ""
    newest = max(
        replies,
        key=lambda comment: (
            str(comment.get("created_at") or comment.get("createdAt") or ""),
            int(comment.get("databaseId") or comment.get("id") or 0),
        ),
    )
    body = str(newest.get("body") or "")
    # Unresolved is checked first: "not resolved" and "unresolved" both contain
    # "resolved", so a naive order would read an explicit refusal as agreement.
    if UNRESOLVED_RE.search(body):
        return "unresolved"
    if RESOLVED_RE.search(body):
        return "resolved"
    return ""


def thread_resolution_by_comment(
    threads: Optional[Sequence[Dict[str, Any]]], bot_login: str
) -> Dict[int, str]:
    """Explicit provider resolution per comment id, keyed by comment id.

    Empty when thread state is unknown, so a caller can keep every thread.
    """

    if threads is None:
        return {}
    verdicts: Dict[int, str] = {}
    for thread in threads:
        if thread.get("is_outdated"):
            continue
        verdict = thread_resolution(thread.get("comments") or [], bot_login)
        if not verdict:
            continue
        for comment in thread.get("comments") or []:
            database_id = comment.get("databaseId")
            if database_id is not None:
                verdicts[int(database_id)] = verdict
    return verdicts


def pending_thread_normalizations(
    threads: Optional[Sequence[Dict[str, Any]]], bot_login: str
) -> List[Dict[str, Any]]:
    """Threads GitHub still calls unresolved that the provider declared resolved.

    These are the #37 deadlock: a fixed finding whose textual resolution never
    reached GitHub's own state. Normalizing them is what stops the gate from
    re-requesting verification of a finding that is already fixed.
    """

    if threads is None:
        return []
    pending: List[Dict[str, Any]] = []
    for thread in threads:
        if thread.get("is_resolved") or thread.get("is_outdated"):
            continue
        if thread_resolution(thread.get("comments") or [], bot_login) != "resolved":
            continue
        pending.append({"id": str(thread.get("id") or ""), "comments": thread.get("comments") or []})
    return pending


def collect(
    client: Any,
    pr_number: int,
    head_sha: str,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    status_context: str = DEFAULT_STATUS_CONTEXT,
) -> ProviderSnapshot:
    """Read CodeRabbit's status, verdict, threads, and summary for this PR.

    Both deadlock surfaces are resolved here rather than in the generic gate,
    because both are facts about how this provider publishes: only this adapter
    knows which of its own records are decisive and which of its own replies
    claim a thread is resolved.
    """

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
        if _matches_bot(_comment_login(comment), bot_login)
    ]

    reviews = client.list_reviews(int(pr_number))
    approval_ms = approved_at_ms(reviews, bot_login, head_sha)
    # A provider can overwrite the status surface for a head it already reviewed
    # (`Review skipped`), so a durable exact-head approval outranks the status:
    # the review is the acceptance basis, and the status is kept for pending and
    # rate-limit state only. Without an approval the status is all there is, and
    # an unusable one fails closed.
    status = None
    try:
        status_payload = client.combined_status_for_ref(head_sha)
        status = latest_status(status_payload.get("statuses") or [], status_context)
    except Exception:  # noqa: BLE001 - unknown status must fail closed
        status = None
    status_problem = ""
    if status is None:
        status_problem = (
            f"the review provider reported no {status_context!r} status for this HEAD, "
            "so the diff is unverified"
        )
    elif status.get("state") != "success" or not COMPLETED_RE.search(
        str(status.get("description") or "")
    ):
        status_problem = (
            f"the {status_context!r} status for this HEAD is "
            f"{status.get('state') or 'unknown'}: {str(status.get('description') or '')[:200]}"
        )
    extra_reason = status_problem if not approval_ms else None

    # Output the provider published before its own approval was advisory and has
    # been adjudicated. Anything newer has not been reviewed yet and still blocks.
    if approval_ms:
        provider_review_comments = [
            comment
            for comment in provider_review_comments
            if _comment_ms(comment) >= approval_ms
        ]
        summary_comments = [
            comment for comment in summary_comments if _comment_ms(comment) >= approval_ms
        ]

    decision = latest_review(reviews, bot_login, head_sha)
    decision_state = (decision or {}).get("state")
    forced_titles: List[str] = []
    if decision_state == "CHANGES_REQUESTED":
        # A blocking verdict is evidence of at least one actionable finding even
        # when the report only states it in prose.
        forced_titles = parse_summary((decision or {}).get("body") or "")
        if not forced_titles:
            forced_titles = ["CodeRabbit requested changes on this HEAD without an inline finding."]

    threads = _review_threads(client, int(pr_number))
    verdicts = thread_resolution_by_comment(threads, bot_login)
    superseded = {
        comment_id for comment_id, verdict in verdicts.items() if verdict == "resolved"
    }
    # A thread the provider called unresolved blocks even when GitHub's own flag
    # says it is closed, so those ids are added back to the unresolved set. Only an
    # unknown thread state is left alone: a null answer is not permission to guess.
    unresolved = client.unresolved_thread_comment_ids(int(pr_number))
    if unresolved is not None:
        unresolved = set(unresolved) | {
            comment_id
            for comment_id, verdict in verdicts.items()
            if verdict == "unresolved"
        }

    return ProviderSnapshot(
        provider=PROVIDER_NAME,
        bot_logins=(bot_login,),
        review_bots=(bot_login,),
        output_markers=OUTPUT_MARKERS,
        summary_comments=summary_comments,
        review_comments=provider_review_comments,
        unresolved_ids=unresolved,
        summary_parser=parse_summary,
        is_boilerplate=is_boilerplate_title,
        extra_reason=extra_reason,
        forced_open_titles=forced_titles,
        provider_decision=decision_state,
        superseded_thread_ids=superseded,
        approved_at_ms=approval_ms,
        metadata={
            "status_context": status_context,
            "review_id": (decision or {}).get("id"),
            "status_only": bool(approval_ms),
            "thread_state_known": threads is not None,
            "threads_to_normalize": len(pending_thread_normalizations(threads, bot_login)),
        },
    )


def profile_for(bot_login: str, status_context: str) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    return (bot_login,), (bot_login,), OUTPUT_MARKERS
