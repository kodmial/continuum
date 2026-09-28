"""PR-Agent review adapter.

Owns everything specific to how PR-Agent renders a report. The generic gate
only sees the titles this adapter returns, so no provider knowledge leaks into
merge logic.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from .findings import collect_summary_comments, sanitize_title
from .snapshot import ProviderSnapshot
from .tracker import BOT_PREFIX, TRACKER_MARKER, VERDICT_MARKER

PROVIDER_NAME = "pr-agent"

# Trusted output identities. A provider report is only accepted from one of
# these; a human or a fork posting similar text is untrusted input, not output.
DEFAULT_BOT_LOGINS = ("github-actions[bot]", "pr-agent[bot]")
DEFAULT_REVIEW_BOT_LOGINS = ("github-actions[bot]", "pr-agent[bot]")
OUTPUT_MARKERS = (
    "pr reviewer guide",
    "pr-agent",
    "reviewer guide",
    "key issues",
    "inline issues",
)

# Headings whose whole markdown section must never produce findings: they list
# relevant files and review effort, which describes the diff instead of
# reporting an actionable problem.
BOILERPLATE_SECTION_HEADINGS = (
    "reviewer guide",
    "relevant file",
    "relevant files",
    "general comment",
    "general comments",
    "estimated effort",
    "effort to review",
)

# Phrases inside a single bullet title that mark guide text, a file listing, or
# a no-op summary rather than a finding.
BOILERPLATE_TITLE_PHRASES = (
    "pr reviewer guide",
    "reviewer guide",
    "relevant file",
    "relevant files",
    "no actionable",
    "no major issues",
    "looks good",
    "lgtm",
    "general comment",
    "general comments",
    "estimated effort",
    "effort to review",
    "pay attention",
    "focus on",
    "to review this",
)

# Headings whose section may hold actionable findings. Summary bullets are only
# extracted from these so guide text and file listings never become findings.
FINDING_SECTION_HEADINGS = (
    "key issue",
    "key finding",
    "finding",
    "actionable",
    "inline issue",
    "inline finding",
    "issue to",
    "issues to",
    "problem",
    "concern",
    "bug",
    "vulnerability",
    "weakness",
    "risk",
)

_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.*)\s*$")
_FOCUS_AREAS_RE = re.compile(r"recommended\s+focus\s+areas\s+for\s+review", re.I)
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.I | re.S)
_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+(.{10,300})", re.M)
_NUMBERED_TITLE_RE = re.compile(r"(?:^|[;:\n])\s*\d+[.)]\s+([A-Z].{10,200})")


def is_boilerplate_title(title: str) -> bool:
    lowered = (title or "").lower()
    return any(phrase in lowered for phrase in BOILERPLATE_TITLE_PHRASES)


def _clean_strong_title(raw: str) -> str:
    return sanitize_title(raw, limit=160).strip(":").strip()[:160]


def extract_focus_area_titles(body: str) -> List[str]:
    """Actionable titles from a PR-Agent ``Recommended focus areas`` block.

    With ``inline_key_issues=false`` the key issues live only in that block, so
    it is extracted before the whole reviewer-guide section is dropped.
    """

    if not body:
        return []
    match = _FOCUS_AREAS_RE.search(body)
    if not match:
        return []
    rest = body[match.end() :]
    next_heading = _HEADING_RE.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest
    titles: List[str] = []
    for strong in _STRONG_RE.finditer(section):
        title = _clean_strong_title(strong.group(1))
        if len(title) < 10 or is_boilerplate_title(title):
            continue
        titles.append(title)
    return titles


def drop_boilerplate_sections(body: str) -> str:
    matches = list(_HEADING_RE.finditer(body or ""))
    if not matches:
        return body or ""
    kept = [body[: matches[0].start()]]
    for index, match in enumerate(matches):
        heading = (match.group(1) or "").lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if any(phrase in heading for phrase in BOILERPLATE_SECTION_HEADINGS):
            continue
        kept.append(body[match.start() : end])
    return "".join(kept)


def select_finding_text(body: str) -> str:
    """Keep only markdown that can hold actionable findings."""

    focus_titles = extract_focus_area_titles(body or "")
    text = drop_boilerplate_sections(body or "")
    matches = list(_HEADING_RE.finditer(text))
    if matches:
        kept: List[str] = []
        for index, match in enumerate(matches):
            heading = (match.group(1) or "").lower()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if any(phrase in heading for phrase in FINDING_SECTION_HEADINGS):
                kept.append(text[match.start() : end])
        base = "".join(kept)
    else:
        # Heading-free reports still carry findings; do not drop everything.
        base = text
    if focus_titles:
        bullets = "\n".join(f"- {title}" for title in focus_titles)
        if base and not base.endswith("\n"):
            base += "\n"
        base += bullets + "\n"
    return base


def parse_summary(body: str) -> List[str]:
    """Titles of the actionable findings in one provider report."""

    text = select_finding_text(body or "")
    if not text.strip():
        return []
    titles: List[str] = []
    seen = set()
    for pattern in (_BULLET_RE, _NUMBERED_TITLE_RE):
        for match in pattern.finditer(text):
            title = sanitize_title(match.group(1))
            if len(title) < 10 or is_boilerplate_title(title):
                continue
            if title in seen:
                continue
            seen.add(title)
            titles.append(title)
    return titles


def linked_issue_numbers(pr_body: Optional[str]) -> List[int]:
    """Issue numbers the PR body claims to close."""

    return sorted(
        {
            int(number)
            for number in re.findall(
                r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", pr_body or "", re.I
            )
        }
    )


def profile_for(bot_login: str) -> Tuple[Sequence[str], Sequence[str], Sequence[str]]:
    """(bot_logins, review_bots, output_markers) for this adapter."""

    bot_logins = tuple(dict.fromkeys([bot_login, *DEFAULT_BOT_LOGINS]))
    return bot_logins, DEFAULT_REVIEW_BOT_LOGINS, OUTPUT_MARKERS


def queue_request(
    settings: Any,
    *,
    pr_number: int,
    head_sha: str = "",
    kind: str = "initial",
    dispatch_workflow: str = "pr-agent.yml",
) -> Any:
    """PR-Agent is driven by a gate run, not by a bot mention.

    A workflow dispatch keeps the request in Actions, where the gate already
    normalizes the result; no provider command is posted to the PR.
    """

    from .providers import REQUEST_WORKFLOW_DISPATCH, QueueRequest

    return QueueRequest(
        kind=REQUEST_WORKFLOW_DISPATCH,
        provider=PROVIDER_NAME,
        workflow=dispatch_workflow,
        inputs={
            "pr_number": str(int(pr_number)),
            "head_sha": str(head_sha or ""),
            "mode": str(kind),
        },
    )


def collect(
    client: Any,
    pr_number: int,
    *,
    bot_login: str = DEFAULT_BOT_LOGINS[0],
    trusted_logins: Sequence[str] = (),
) -> ProviderSnapshot:
    """Read everything PR-Agent has published for this pull request."""

    bot_logins, review_bots, markers = profile_for(bot_login)
    issue_comments = client.list_issue_comments(int(pr_number))
    review_comments = client.list_review_comments(int(pr_number))
    summary_comments = collect_summary_comments(
        issue_comments,
        bot_logins=bot_logins,
        markers=markers,
        exclude_markers=(TRACKER_MARKER, VERDICT_MARKER),
        exclude_prefix=BOT_PREFIX,
    )
    return ProviderSnapshot(
        provider=PROVIDER_NAME,
        bot_logins=tuple(bot_logins),
        review_bots=tuple(review_bots),
        output_markers=tuple(markers),
        summary_comments=summary_comments,
        review_comments=list(review_comments),
        unresolved_ids=client.unresolved_thread_comment_ids(int(pr_number)),
        summary_parser=parse_summary,
        is_boilerplate=is_boilerplate_title,
        metadata={"linked_issues": []},
    )
