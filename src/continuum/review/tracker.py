"""Finding tracker state: persistence, rendering, and status transitions.

The tracker is one issue comment per pull request. It carries a human-readable
table plus a machine-readable JSON block, so repeated reviews can track findings
as open / still-open / resolved / reopened and `/verify` can settle a single
finding without duplicating it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .findings import (
    FINDING_ID_PREFIX,
    escape_inline_code,
    format_time,
    parse_time,
    sanitize_table_cell,
)

TRACKER_MARKER = "<!-- continuum-review-tracker -->"
TRACKER_SCHEMA = "continuum.review-tracker/v1"

# Prefix on every comment Continuum's gate writes. Used to exclude Continuum's
# own output when looking for provider output.
BOT_PREFIX = "[continuum-review]"

# Marker on the comment the gate posts with its GitHub review verdict, so the
# verdict is never re-read as provider output.
VERDICT_MARKER = "<!-- continuum-review-verdict -->"

STATUS_OPEN = "open"
STATUS_STILL_OPEN = "still-open"
STATUS_RESOLVED = "resolved"
STATUS_REOPENED = "reopened"

_ACTIVE_STATUSES = (STATUS_OPEN, STATUS_STILL_OPEN, STATUS_REOPENED)
_SETTLED_STATUSES = (STATUS_RESOLVED, "fixed")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def empty_state() -> Dict[str, Any]:
    return {"schema": TRACKER_SCHEMA, "head": "", "findings": [], "last_review_at": None}


def _coerce_findings(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    findings: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("id") or "").upper()
        if not re.match(r"^(CRV|PRA)-[0-9A-F]{8}$", finding_id):
            continue
        try:
            line = max(0, int(item.get("line") or 0))
        except (TypeError, ValueError):
            line = 0
        status = str(item.get("status") or STATUS_OPEN)
        if status not in _ACTIVE_STATUSES + _SETTLED_STATUSES:
            status = STATUS_OPEN
        entry = {
            "id": finding_id,
            "file": str(item.get("file") or ""),
            "line": line,
            "title": str(item.get("title") or ""),
            "source": str(item.get("source") or "summary"),
            "source_created": item.get("source_created"),
            "status": status,
        }
        if item.get("resolved_at"):
            entry["resolved_at"] = item["resolved_at"]
        findings.append(entry)
    findings.sort(key=lambda finding: finding["id"])
    return findings


def parse_tracker_state(body: str) -> Optional[Dict[str, Any]]:
    """Extract the machine-readable state from a tracker comment body."""

    if TRACKER_MARKER not in (body or ""):
        return None
    match = _JSON_BLOCK_RE.search(body)
    if not match:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    state = empty_state()
    state["head"] = str(raw.get("head") or "")
    state["last_review_at"] = raw.get("last_review_at")
    state["findings"] = _coerce_findings(raw.get("findings"))
    return state


def load_tracker(
    comments: Sequence[Dict[str, Any]],
    *,
    trusted_logins: Sequence[str] = (),
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Newest tracker state from a comment list.

    Only comments authored by a trusted identity are considered. A comment that
    merely copies the tracker marker must never be able to rewrite finding state.
    """

    trusted = {login.lower() for login in trusted_logins}
    for comment in reversed(list(comments or [])):
        body = comment.get("body") or ""
        state = parse_tracker_state(body)
        if state is None:
            continue
        login = ((comment.get("user") or {}).get("login") or "").lower()
        if trusted and login and login not in trusted:
            continue
        return comment, state
    return None, empty_state()


def render_tracker(
    head_sha: str,
    findings: Sequence[Dict[str, Any]],
    last_review_at: Optional[str],
    bot_prefix: str,
    provider: str,
) -> str:
    """Render the tracker comment. Every provider string is sanitized."""

    lines = [
        f"{bot_prefix} Continuum review tracker ({provider}) for HEAD `{escape_inline_code(head_sha)}`",
        "",
        TRACKER_MARKER,
        "",
        "| Finding | Location | Status | Title |",
        "| --- | --- | --- | --- |",
    ]
    if findings:
        for finding in findings:
            location = (
                f"{sanitize_table_cell(str(finding.get('file') or ''), 120)}:{finding.get('line') or 0}"
                if finding.get("file")
                else "general"
            )
            lines.append(
                "| `{id}` | {loc} | {status} | {title} |".format(
                    id=escape_inline_code(str(finding.get("id") or "")),
                    loc=location,
                    status=escape_inline_code(str(finding.get("status") or "")),
                    title=sanitize_table_cell(str(finding.get("title") or "")),
                )
            )
    else:
        lines.append("| — | — | — | No tracked findings |")

    state = {
        "schema": TRACKER_SCHEMA,
        "head": head_sha or "",
        "last_review_at": last_review_at,
        "findings": [dict(finding) for finding in findings],
    }
    lines += [
        "",
        "<details><summary>Machine-readable state</summary>",
        "",
        "```json",
        json.dumps(state, indent=2, sort_keys=True),
        "```",
        "",
        "</details>",
        "",
        f"Re-check one finding against the current HEAD with `/verify <finding-id>` "
        f"(for example `/verify {FINDING_ID_PREFIX}-1A2B3C4D`).",
    ]
    return "\n".join(lines)


def merge_findings(
    previous_state: Optional[Dict[str, Any]],
    current: Sequence[Dict[str, Any]],
    head_sha: str,
    last_review_at: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fold this run's findings into the persisted state.

    Reopening a resolved finding requires fresh provider output: the source
    comment must be strictly newer than both the persisted review marker and the
    finding's own `/verify` resolution time. A HEAD change alone is never
    sufficient, so re-running the gate cannot flip resolved back to reopened.
    """

    state = previous_state or empty_state()
    previous = {finding["id"]: finding for finding in state.get("findings", [])}
    previous_head = str(state.get("head") or "")
    previous_marker = state.get("last_review_at")
    since_dt = parse_time(previous_marker) if previous_marker else None

    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for finding in current:
        finding_id = finding["id"]
        seen.add(finding_id)
        old = previous.get(finding_id)
        if old is None:
            merged.append({**finding, "status": STATUS_OPEN})
            continue
        if old.get("status") in _SETTLED_STATUSES:
            source_dt = parse_time(finding.get("source_created"))
            resolved_dt = parse_time(old.get("resolved_at"))
            bounds = [value for value in (since_dt, resolved_dt) if value is not None]
            if bounds and source_dt is not None and source_dt > max(bounds):
                merged.append({**finding, "status": STATUS_REOPENED})
            elif not bounds and previous_head and head_sha != previous_head:
                merged.append({**finding, "status": STATUS_REOPENED})
            else:
                settled = {**finding, "status": STATUS_RESOLVED}
                if old.get("resolved_at"):
                    settled["resolved_at"] = old["resolved_at"]
                merged.append(settled)
            continue
        merged.append({**finding, "status": STATUS_STILL_OPEN})

    for finding_id, old in sorted(previous.items()):
        if finding_id in seen:
            continue
        entry = dict(old)
        if entry.get("status") in _ACTIVE_STATUSES:
            entry["status"] = STATUS_RESOLVED
            entry.pop("resolved_at", None)
        merged.append(entry)

    merged.sort(key=lambda finding: finding["id"])
    marker = last_review_at if last_review_at is not None else previous_marker
    return merged, marker


def apply_verification(
    state: Optional[Dict[str, Any]],
    finding: Dict[str, Any],
    resolved: bool,
    verified_at: str,
) -> List[Dict[str, Any]]:
    """Settle exactly one finding in place, without duplicating it."""

    tracker = state or empty_state()
    findings = _coerce_findings(tracker.get("findings"))
    finding_id = str(finding["id"]).upper()
    matched = False
    for entry in findings:
        if entry["id"] == finding_id:
            entry["status"] = STATUS_RESOLVED if resolved else STATUS_STILL_OPEN
            if resolved:
                entry["resolved_at"] = verified_at
            else:
                entry.pop("resolved_at", None)
            matched = True
            break
    if not matched:
        entry = {
            "id": finding_id,
            "file": str(finding.get("file") or ""),
            "line": int(finding.get("line") or 0),
            "title": str(finding.get("title") or ""),
            "source": str(finding.get("source") or "summary"),
            "source_created": finding.get("source_created"),
            "status": STATUS_RESOLVED if resolved else STATUS_STILL_OPEN,
        }
        if resolved:
            entry["resolved_at"] = verified_at
        findings.append(entry)
    findings.sort(key=lambda item: item["id"])
    return findings


def marker_time(state: Optional[Dict[str, Any]]) -> Optional[str]:
    if not state:
        return None
    value = state.get("last_review_at")
    return format_time(parse_time(value)) if value else None
