"""Compatibility fallback for absent native PR-Agent finding state (issue #241).

When upstream PR-Agent v0.46.0 publishes no native persistent finding state
for a successfully validated structured full review, the review workflow
derives a schema-compatible fallback instead of blocking repair. Native
upstream state remains authoritative whenever present and valid; this
fallback runs only after exact-HEAD and PRReview-schema validation.

This module is a dependency-free equivalent of the upstream v0.46.0
finding-state contract (`pr_agent.algo.review_finding_state`,
STATE_SCHEMA_VERSION = 1): the same path/body normalization, the same
12-hex finding fingerprint, and the same first-seen reconciliation with no
previous state. Every current finding starts ACTIVE; nothing is ever marked
RESOLVED here because resolution requires a known previous HEAD, and there
is none. The workflow runtime calls the upstream implementation directly;
this module is the deterministic equivalent used by unit tests. It owns no
marker format knowledge: hidden marker parsing stays in pinned upstream
PR-Agent.

The dependency-free rule matters: this module is imported by unit tests on
a bare runner, so it uses the standard library only.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping


class FallbackStateError(ValueError):
    """Raised when no safe fallback finding state can be derived."""


STATE_ACTIVE = "ACTIVE"

FINDING_STATE_SCHEMA_VERSION = 1

_WHITESPACE_RE = re.compile(r"\s+")


def _state_line(value: object) -> int | None:
    """Positive integer lines only, mirroring upstream `_as_line`."""

    try:
        line = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return line if line > 0 else None


def finding_fingerprint(path: str, body: str) -> str:
    """Stable finding identity mirroring upstream `key_issue_fingerprint`.

    Whitespace-collapsed, lowercased body keyed by path, SHA-256 truncated
    to 12 hex characters, so repeated reviews of the same defect re-derive
    the same id instead of creating duplicates.
    """

    collapsed = _WHITESPACE_RE.sub(" ", body).lower()
    return hashlib.sha256(f"{path}|{collapsed}".encode("utf-8")).hexdigest()[:12]


def normalize_finding(finding: object) -> Dict[str, Any] | None:
    """Normalize one structured finding, mirroring upstream `normalize_finding`.

    Returns the stable display-oriented record (finding_id, ACTIVE state,
    body, path, optional line range) or None when the finding cannot be
    represented safely/losslessly (missing path or body). A None result
    must fail closed upstream: inventing data is never allowed.
    """

    if not isinstance(finding, Mapping):
        return None
    path = str(finding.get("path") or finding.get("relevant_file") or "").strip()
    path = path.strip().strip("`").lstrip("/")
    body = str(
        finding.get("body")
        or finding.get("issue_content")
        or finding.get("description")
        or ""
    ).strip()
    if not path or not body:
        return None
    finding_id = finding_fingerprint(path, body)
    start = _state_line(
        finding.get("line_start")
        or finding.get("relevant_lines_start")
        or finding.get("start_line")
    )
    end = _state_line(
        finding.get("line_end")
        or finding.get("relevant_lines_end")
        or finding.get("end_line")
    )
    if start is not None and end is None:
        end = start
    if start is not None and end is not None and end < start:
        end = start
    normalized: Dict[str, Any] = {
        "finding_id": finding_id,
        "state": STATE_ACTIVE,
        "body": body,
        "path": path,
    }
    if start is not None:
        normalized["line_start"] = start
    if end is not None:
        normalized["line_end"] = end
    return normalized


def _timestamp(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def derive_fallback_state(
    review: object,
    head_sha: str,
    run_id: str = "",
    timestamp: object = None,
) -> Dict[str, Any]:
    """Derive a schema-compatible ACTIVE finding state from a validated review.

    `review` is the parsed structured review (either the full PRReview
    payload with a `review` object or the inner review object itself) whose
    `key_issues_to_review` list already passed exact-HEAD and schema
    validation. `head_sha` is the exact reviewed HEAD; `run_id` identifies
    the producing run; `timestamp` pins first/last-seen times for
    deterministic tests.

    Every actionable finding is preserved as ACTIVE: findings are
    de-duplicated by stable fingerprint only, never dropped, resolved, or
    weakened. Any malformed or unrepresentable finding fails closed with
    FallbackStateError instead of inventing data.
    """

    head = str(head_sha or "").strip().lower()
    if not head:
        raise FallbackStateError(
            "fallback persistent state requires the exact reviewed HEAD"
        )
    if not isinstance(review, dict):
        raise FallbackStateError("fallback persistent state requires a review object")
    # Split envelopes carry findings on both sides: merge fail-closed (union)
    # so no actionable finding is dropped from the derived fallback state.
    outer_issues = review.get("key_issues_to_review")
    nested = review.get("review")
    nested_issues = nested.get("key_issues_to_review") if isinstance(nested, dict) else None
    if outer_issues is None:
        key_issues = nested_issues
    elif nested_issues is None:
        key_issues = outer_issues
    elif isinstance(outer_issues, list) and isinstance(nested_issues, list):
        key_issues = [*outer_issues, *nested_issues]
    elif isinstance(outer_issues, list):
        key_issues = outer_issues
    elif isinstance(nested_issues, list):
        key_issues = nested_issues
    else:
        key_issues = outer_issues
    if key_issues is None:
        raise FallbackStateError(
            "fallback persistent state requires key_issues_to_review"
        )
    if not isinstance(key_issues, list):
        raise FallbackStateError(
            "fallback persistent state key_issues_to_review must be a list"
        )
    for index, entry in enumerate(key_issues):
        if not isinstance(entry, dict):
            raise FallbackStateError(
                f"fallback persistent state cannot represent finding #{index}: "
                "each key issue must be an object"
            )
    normalized = [normalize_finding(entry) for entry in key_issues]
    unrepresentable = [
        index for index, record in enumerate(normalized) if record is None
    ]
    if unrepresentable:
        raise FallbackStateError(
            "fallback persistent state cannot represent finding(s) "
            f"{unrepresentable}: failing closed rather than inventing data"
        )
    by_id: Dict[str, Dict[str, Any]] = {}
    for record in normalized:
        assert record is not None
        by_id.setdefault(record["finding_id"], record)
    now = _timestamp(timestamp)
    findings: List[Dict[str, Any]] = []
    for finding_id in sorted(by_id):
        record = dict(by_id[finding_id])
        record.update(first_seen=now, last_seen=now, last_seen_head_sha=head)
        findings.append(record)
    return {
        "schema_version": FINDING_STATE_SCHEMA_VERSION,
        "findings": findings,
        "last_run": {
            "complete": True,
            "excluded_files": [],
            "head_sha": head,
            "kind": "full",
            "run_id": str(run_id or "").strip(),
        },
    }


__all__ = [
    "STATE_ACTIVE",
    "FINDING_STATE_SCHEMA_VERSION",
    "FallbackStateError",
    "finding_fingerprint",
    "normalize_finding",
    "derive_fallback_state",
]
