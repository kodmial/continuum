"""Exact re-check of one finding against the current HEAD.

`/verify <finding-id>` answers RESOLVED or UNRESOLVED for a single existing
finding. It never creates a new finding, never duplicates the thread, and
treats an unparseable answer as UNRESOLVED so the answer is always
machine-detectable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .findings import (
    DEFAULT_REVIEW_BOT_LOGINS,
    extract_findings,
    now_iso,
    parse_time,
    sanitize_title,
    select_current_summary_comments,
)
from .llm import OpenAICompatibleClient
from .snapshot import ProviderSnapshot
from .tracker import (
    BOT_PREFIX,
    TRACKER_MARKER,
    VERDICT_MARKER,
    apply_verification,
    load_tracker,
    render_tracker,
)

RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"

DIFF_BUDGET_CHARS = 6000


class VerifyError(RuntimeError):
    """Raised when a finding cannot be re-checked."""


def build_prompt(finding: Dict[str, Any], head: str, context: str, partial: bool) -> str:
    """Verification prompt. Untrusted titles are delimited, never interpolated."""

    title = sanitize_title(str(finding.get("title") or ""), limit=200)
    location = sanitize_title(str(finding.get("file") or "general"), limit=200)
    try:
        line = int(finding.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    label = (
        "Partial PR diff (may be truncated and may not contain the relevant code):"
        if partial
        else "Current code around the finding:"
    )
    extra = (
        "If the code relevant to this finding is not present above, reply UNRESOLVED.\n"
        if partial
        else ""
    )
    return (
        f"Re-check finding {finding.get('id')} against the current pull request HEAD.\n"
        f"Finding title: {title}\n"
        f"Location: {location}:{line}\n"
        f"Current HEAD: {head}\n\n"
        f"{label}\n{context[:DIFF_BUDGET_CHARS]}\n\n"
        f"{extra}"
        "Reply with exactly one machine-detectable first line: RESOLVED when the "
        "underlying problem is fully fixed, or UNRESOLVED when it still exists. "
        "After that line, explain the precise remaining problem or why it is fixed. "
        "Do not report a new finding id."
    )


def classify(answer: str) -> Tuple[str, str]:
    """Map a model answer onto exactly one verdict token."""

    text = (answer or "").strip()
    first = text.splitlines()[0].strip().upper() if text else ""
    if first.startswith(RESOLVED):
        return RESOLVED, text
    if first.startswith(UNRESOLVED):
        return UNRESOLVED, text
    return (
        UNRESOLVED,
        f"{UNRESOLVED} — verifier returned a non-conforming first line.\n\n{text}".strip(),
    )


def _mentions(title: str, filename: str) -> bool:
    name = (filename or "").lower()
    if not name or not title:
        return False
    if name in title:
        return True
    base = name.rsplit("/", 1)[-1]
    return bool(base) and base in title


def build_context(
    client: Any,
    pr_number: int,
    head: str,
    finding: Dict[str, Any],
) -> Tuple[str, bool]:
    """Best-effort current-HEAD code context plus a truncation flag."""

    path = str(finding.get("file") or "")
    if path:
        content = client.file_at_ref(path, head)
        if content is not None:
            lines = content.splitlines()
            try:
                line_no = int(finding.get("line") or 0)
            except (TypeError, ValueError):
                line_no = 0
            if line_no > 0:
                low = max(0, line_no - 30)
                high = min(len(lines), line_no + 30)
                return "\n".join(f"{index + 1}:{lines[index]}" for index in range(low, high)), False
            return "\n".join(
                f"{index + 1}:{text}" for index, text in enumerate(lines[:120])
            ), False
        return "", False

    # A summary finding carries no file/line: verify it against the diff.
    title = (str(finding.get("title") or "")).lower()
    try:
        files = client.list_pull_files(int(pr_number)) or []
    except Exception:  # noqa: BLE001 - unreadable diff is not a verification result
        return "", False
    ordered = sorted(files, key=lambda entry: 0 if _mentions(title, entry.get("filename")) else 1)
    parts: List[str] = []
    used = 0
    truncated = False
    for entry in ordered:
        patch = entry.get("patch") or ""
        if not patch:
            continue
        chunk = f"--- {entry.get('filename', '?')}\n{patch}"
        if used + len(chunk) > DIFF_BUDGET_CHARS:
            remaining = DIFF_BUDGET_CHARS - used
            if remaining > 0:
                parts.append(chunk[:remaining])
            truncated = True
            break
        parts.append(chunk)
        used += len(chunk) + 1
    return "\n\n".join(parts)[:DIFF_BUDGET_CHARS], truncated


def find_finding(
    state: Dict[str, Any],
    findings: List[Dict[str, Any]],
    finding_id: str,
) -> Optional[Dict[str, Any]]:
    for finding in state.get("findings", []):
        if str(finding.get("id", "")).upper() == finding_id.upper():
            return finding
    for finding in findings:
        if str(finding.get("id", "")).upper() == finding_id.upper():
            return {**finding, "status": "open"}
    return None


def verify_finding(
    client: Any,
    snapshot: ProviderSnapshot,
    *,
    pr_number: int,
    head: str,
    finding_id: str,
    llm: Optional[OpenAICompatibleClient] = None,
    apply: bool = True,
    issue_comments: Optional[List[Dict[str, Any]]] = None,
    review_comments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Re-check one finding and settle it in place."""

    comments = issue_comments if issue_comments is not None else client.list_issue_comments(int(pr_number))
    inline = review_comments if review_comments is not None else client.list_review_comments(int(pr_number))
    _existing, state = load_tracker(comments, trusted_logins=("github-actions[bot]", "github-actions"))

    since = parse_time(state["last_review_at"]) if state.get("last_review_at") else None
    selected = select_current_summary_comments(snapshot.summary_comments, head, since)
    derived = extract_findings(
        issue_comments=selected,
        review_comments=snapshot.review_comments or inline,
        summary_comments=selected,
        summary_parser=snapshot.summary_parser,
        current_head=head,
        since=since,
        unresolved_ids=snapshot.unresolved_ids,
        review_bots=snapshot.review_bots or DEFAULT_REVIEW_BOT_LOGINS,
        is_boilerplate=snapshot.is_boilerplate,
    )

    target = find_finding(state, derived, finding_id)
    if target is None:
        raise VerifyError(
            f"Unknown finding {finding_id!r}; check the review tracker comment for valid ids"
        )

    context, partial = build_context(client, pr_number, head, target)
    if not context:
        verdict = UNRESOLVED
        answer = (
            f"{UNRESOLVED} — no current-HEAD code context was available to verify this finding."
        )
    else:
        if llm is None:
            raise VerifyError("A provider client is required to re-check a finding")
        answer = llm.chat(build_prompt(target, head, context, partial))
        verdict, answer = classify(answer)

    findings = apply_verification(state, target, verdict == RESOLVED, now_iso())
    body = "\n".join(
        [
            f"{BOT_PREFIX} `/verify {target['id']}` against HEAD `{head}`",
            "",
            verdict,
            "",
            answer,
            "",
            f"Finding: `{target['id']}` ({target.get('file') or 'general'}:{target.get('line') or 0}) — {sanitize_title(str(target.get('title') or ''), limit=200)}",
        ]
    )

    if apply:
        client.create_issue_comment(int(pr_number), f"{VERDICT_MARKER}\n{body}")
        tracker_body = render_tracker(
            head, findings, state.get("last_review_at"), BOT_PREFIX, snapshot.provider
        )
        client.upsert_marker_comment(
            int(pr_number),
            TRACKER_MARKER,
            tracker_body,
            ("github-actions[bot]", "github-actions"),
        )

    return {
        "schema": "continuum.review-verify/v1",
        "provider": snapshot.provider,
        "pr": int(pr_number),
        "head": head,
        "finding": target["id"],
        "verdict": verdict,
        "resolved": verdict == RESOLVED,
        "context_truncated": partial,
        "findings": findings,
        "last_review_at": state.get("last_review_at"),
        "answer": answer,
    }
