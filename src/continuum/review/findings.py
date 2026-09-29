"""Provider-agnostic finding extraction.

The engine owns stable identifiers, sanitization of untrusted text, current-HEAD
selection, and inline-thread state. A provider adapter supplies only the
provider-specific summary parser, so nothing in this module knows how a
particular reviewer formats its report.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Continuum owns the identifier space so several providers can feed the same
# normalized contract. The retired NanoDictate prefix stays accepted for
# migration of already-published trackers.
FINDING_ID_PREFIX = "CRV"
LEGACY_FINDING_ID_PREFIXES = ("PRA",)
FINDING_ID_RE = re.compile(r"^(?i:crv|pra)-[0-9A-Fa-f]{8}$")

MAX_TITLE_LENGTH = 160
MIN_TITLE_LENGTH = 10
INLINE_TITLE_LIMIT = 200

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")

# Authored inline findings are only trusted from these identities. Anything else
# is a third party commenting on the PR, which is untrusted data, not a finding.
DEFAULT_REVIEW_BOT_LOGINS = (
    "github-actions[bot]",
    "github-actions",
    "pr-agent[bot]",
)

SummaryParser = Callable[[str], Sequence[str]]


class FindingsError(RuntimeError):
    """Raised when findings cannot be derived safely."""


def stable_id(file_path: str, line: int, title: str) -> str:
    """Deterministic finding id derived only from normalized input."""

    digest = hashlib.sha256(
        f"{file_path or 'PR'}|{int(line or 0)}|{title}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{FINDING_ID_PREFIX}-{digest.upper()}"


def is_valid_finding_id(value: str) -> bool:
    return bool(FINDING_ID_RE.match((value or "").strip()))


def normalize_finding_id(value: str) -> Optional[str]:
    """Return the canonical id, or None when the token is not a finding id.

    Legacy NanoDictate ids are rewritten into the Continuum id space so a
    tracker written by the reference implementation stays verifiable.
    """

    token = (value or "").strip().strip("`").strip()
    if not is_valid_finding_id(token):
        return None
    upper = token.upper()
    if upper.startswith(FINDING_ID_PREFIX + "-"):
        return upper
    return None


def sanitize_title(raw: str, limit: int = MAX_TITLE_LENGTH) -> str:
    """Reduce untrusted model/comment text to one safe single-line string."""

    text = _CONTROL_RE.sub(" ", raw or "")
    text = _TAG_RE.sub(" ", text)
    # Backticks are removed outright: a finding title must never be able to
    # form a code span, a fenced block, or a table cell of its own.
    text = text.replace("`", "'")
    text = text.replace("\\", "\\\\")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def sanitize_table_cell(raw: str, limit: int = 120) -> str:
    """One-line, delimiter-safe cell for the rendered tracker table."""

    text = sanitize_title(raw, limit=limit)
    return text.replace("|", "\\|")


def escape_inline_code(raw: str) -> str:
    """Make untrusted text safe to inline in a backtick span."""

    return sanitize_title(raw).replace("`", "'")


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return format_time(datetime.now(timezone.utc)) or ""


def comment_time(comment: Dict[str, Any]) -> Optional[datetime]:
    """Freshness of a comment; `updated_at` wins so in-place edits count.

    Persistent provider comments are edited rather than re-posted, so only
    `created_at` would hide fresh output.
    """

    if comment is None:
        return None
    return parse_time(comment.get("updated_at")) or parse_time(comment.get("created_at"))


def is_provider_comment(
    comment: Dict[str, Any], bot_logins: Sequence[str], markers: Sequence[str] = ()
) -> bool:
    """Decide whether a comment is provider output rather than human input.

    A trusted author is required *and* the body must match one of the provider's
    output markers. Matching text alone is never enough: any user can post a
    comment that looks like a reviewer report, and treating that as provider
    output would let an untrusted comment open or close findings.
    """

    body = comment.get("body") or ""
    if not body.strip():
        return False
    user = ((comment.get("user") or {}).get("login") or "").strip()
    if not user or user not in set(bot_logins):
        return False
    if not markers:
        return True
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in markers)


def collect_summary_comments(
    issue_comments: Iterable[Dict[str, Any]],
    *,
    bot_logins: Sequence[str],
    markers: Sequence[str] = (),
    exclude_markers: Sequence[str] = (),
    exclude_prefix: str = "",
) -> List[Dict[str, Any]]:
    """Provider summary comments, excluding Continuum's own tracker/verdicts."""

    collected: List[Dict[str, Any]] = []
    for comment in issue_comments or []:
        body = comment.get("body") or ""
        if not body.strip():
            continue
        if exclude_prefix and body.startswith(exclude_prefix):
            continue
        if any(marker in body for marker in exclude_markers):
            continue
        if not is_provider_comment(comment, bot_logins, markers):
            continue
        collected.append(comment)
    return collected


def select_current_summary_comments(
    comments: Sequence[Dict[str, Any]],
    current_head: Optional[str],
    since: Optional[datetime],
) -> List[Dict[str, Any]]:
    """Only summaries that can describe the current HEAD.

    Preference: summaries naming the current HEAD, then summaries not older than
    the persisted review marker, else the newest batch. When nothing matches, an
    empty list is returned so historical output can never stand in for a review
    of the current HEAD.
    """

    if not comments:
        return []
    if current_head:
        head = current_head.lower()
        short = head[:7]
        headed = [
            comment
            for comment in comments
            if head in (comment.get("body") or "").lower()
            or (len(short) == 7 and short in (comment.get("body") or "").lower())
        ]
        if headed:
            return headed
    if since is not None:
        fresh = [comment for comment in comments if (comment_time(comment) or since) >= since]
        return fresh
    times = [comment_time(comment) for comment in comments]
    if not any(time is not None for time in times):
        return list(comments)
    latest = max(time for time in times if time is not None)
    return [comment for comment in comments if comment_time(comment) == latest]


def current_review_marker(
    comments: Sequence[Dict[str, Any]],
    current_head: Optional[str],
    since: Optional[datetime],
    previous_marker: Optional[str],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Persisted `last review` marker: newest selected summary creation time."""

    selected = select_current_summary_comments(comments, current_head, since)
    times = [comment_time(comment) for comment in selected]
    times = [time for time in times if time is not None]
    if times:
        return format_time(max(times)), selected
    if previous_marker:
        return previous_marker, selected
    all_times = [comment_time(comment) for comment in comments]
    all_times = [time for time in all_times if time is not None]
    if all_times:
        return format_time(max(all_times)), selected
    return None, selected


def _normalized_line(comment: Dict[str, Any]) -> int:
    value = comment.get("line")
    if value is None:
        value = comment.get("original_line")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def extract_findings(
    *,
    issue_comments: Sequence[Dict[str, Any]],
    review_comments: Sequence[Dict[str, Any]],
    summary_comments: Optional[Sequence[Dict[str, Any]]] = None,
    summary_parser: Optional[SummaryParser] = None,
    current_head: Optional[str] = None,
    since: Optional[datetime] = None,
    unresolved_ids: Optional[Set[int]] = None,
    superseded_ids: Optional[Set[int]] = None,
    review_bots: Sequence[str] = DEFAULT_REVIEW_BOT_LOGINS,
    is_boilerplate: Optional[Callable[[str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Derive findings from provider summaries plus unresolved inline threads.

    `unresolved_ids` is the set of comment ids GitHub still reports as
    unresolved and non-outdated. `None` means the state could not be
    determined; every inline thread is then kept, because failing closed may
    over-report a finding but must never silently drop one.

    `superseded_ids` names threads the provider has explicitly declared
    resolved. GitHub's own flag lags behind such an answer, and treating it as
    authoritative is what turns a fixed finding into a permanent blocker
    (continuum#37). Only an explicit resolution may appear here; an explicit
    `unresolved` never does.
    """

    findings: Dict[str, Dict[str, Any]] = {}
    superseded = set(superseded_ids or ())

    def add(finding: Dict[str, Any]) -> None:
        findings.setdefault(finding["id"], finding)

    # `summary_comments` (or `issue_comments` when no explicit selection is
    # given) is the provider's already author-filtered output. It is narrowed to
    # the current HEAD here so no caller can accidentally widen the scope.
    candidates = list(summary_comments) if summary_comments is not None else list(issue_comments)
    selected = (
        select_current_summary_comments(candidates, current_head, since)
        if current_head or since is not None
        else candidates
    )

    for comment in selected:
        body = comment.get("body") or ""
        created = format_time(comment_time(comment))
        titles: List[str] = []
        if summary_parser is not None and body.strip():
            try:
                titles = list(summary_parser(body) or [])
            except Exception as exc:  # noqa: BLE001 - untrusted input must not crash the gate
                raise FindingsError(f"summary parser failed: {exc}") from None
        for raw_title in titles:
            title = sanitize_title(raw_title)
            if len(title) < MIN_TITLE_LENGTH:
                continue
            if is_boilerplate and is_boilerplate(title):
                continue
            add(
                {
                    "id": stable_id("PR", 0, title),
                    "file": "",
                    "line": 0,
                    "title": title,
                    "source": "summary",
                    "source_created": created,
                }
            )

    for comment in review_comments or []:
        body = comment.get("body") or ""
        if not body.strip():
            continue
        user = ((comment.get("user") or {}).get("login") or "").strip()
        if user not in tuple(review_bots):
            continue
        if unresolved_ids is not None and comment.get("id") is not None:
            if comment["id"] not in unresolved_ids:
                continue
            if comment["id"] in superseded:
                continue
        if current_head:
            head = current_head.lower()
            commit_id = (comment.get("commit_id") or "").lower()
            original_commit_id = (comment.get("original_commit_id") or "").lower()
            anchored = head in (commit_id, original_commit_id)
            if not anchored:
                if since is None:
                    continue
                created = comment_time(comment)
                if created is None or created <= since:
                    continue
        title = sanitize_title(body, limit=INLINE_TITLE_LIMIT)
        if len(title) < MIN_TITLE_LENGTH:
            continue
        if is_boilerplate and is_boilerplate(title):
            continue
        path = (comment.get("path") or "").strip()
        line = _normalized_line(comment)
        add(
            {
                "id": stable_id(path or "PR", line, title),
                "file": path,
                "line": line,
                "title": title,
                "source": "inline",
                "source_created": format_time(comment_time(comment)),
            }
        )

    return sorted(findings.values(), key=lambda finding: finding["id"])
