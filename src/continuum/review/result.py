"""Normalized review-gate result.

Every review provider produces exactly this document. Merge logic consumes
`gate_passed` and the documented status description and never parses a
provider-specific comment, review body, or thread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RESULT_SCHEMA = "continuum.review-gate/v1"

VERDICT_NONE = "NONE"
VERDICT_APPROVE = "APPROVE"
VERDICT_REQUEST_CHANGES = "REQUEST_CHANGES"
VERDICT_COMMENT = "COMMENT"

STATE_SKIPPED = "skipped"
STATE_APPROVED = "approved"
STATE_CHANGES_REQUESTED = "changes_requested"
STATE_BLOCKED = "blocked"

OPEN_STATUSES = ("open", "still-open", "reopened")

# Status context/description are the machine contract between the provider
# adapter and the merge controller. Only these keys are read back.
_STATUS_DESCRIPTION_RE = re.compile(
    r"verdict=(?P<verdict>[A-Z_]+) state=(?P<state>[a-z_]+) "
    r"provider=(?P<provider>[a-z0-9-]+) head=(?P<head>[0-9a-fA-F]+)"
)

_STATUS_DESCRIPTION_TEMPLATE = "verdict={verdict} state={state} provider={provider} head={head}"


@dataclass(frozen=True)
class Decision:
    """Verdict for one gate evaluation."""

    verdict: str
    state: str
    gate_passed: bool
    blocking: bool
    reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "state": self.state,
            "gate_passed": self.gate_passed,
            "blocking": self.blocking,
            "reason": self.reason,
        }


def decide(provider_disabled: bool, open_findings: int, coverage_complete: bool, coverage_reason: Optional[str] = None) -> Decision:
    """Map findings and coverage onto one normalized verdict.

    Fails closed: zero findings only produce a clean gate when full coverage is
    proven. Anything unknown is blocking.
    """

    if provider_disabled:
        return Decision(
            verdict=VERDICT_NONE,
            state=STATE_SKIPPED,
            gate_passed=True,
            blocking=False,
            reason="Review provider is disabled (review.provider: none).",
        )
    if open_findings > 0:
        return Decision(
            verdict=VERDICT_REQUEST_CHANGES,
            state=STATE_CHANGES_REQUESTED,
            gate_passed=False,
            blocking=True,
            reason=f"{open_findings} actionable finding(s) remain open on the current HEAD.",
        )
    if not coverage_complete:
        return Decision(
            verdict=VERDICT_COMMENT,
            state=STATE_BLOCKED,
            gate_passed=False,
            blocking=True,
            reason=(
                "Coverage incomplete, so zero findings cannot be treated as a clean "
                f"full-PR review: {coverage_reason or 'unknown coverage gap'}"
            ),
        )
    return Decision(
        verdict=VERDICT_APPROVE,
        state=STATE_APPROVED,
        gate_passed=True,
        blocking=False,
        reason=None,
    )


def status_description(result: Dict[str, Any]) -> str:
    """Render the normalized, provider-agnostic commit-status description."""

    head = str(result.get("head") or "")
    return _STATUS_DESCRIPTION_TEMPLATE.format(
        verdict=result.get("verdict", VERDICT_NONE),
        state=result.get("state", STATE_BLOCKED),
        provider=result.get("provider", "none"),
        head=head[:40] if head else "0" * 40,
    )


def parse_status_description(description: Optional[str]) -> Dict[str, str]:
    """Parse a status description produced by `status_description`."""

    match = _STATUS_DESCRIPTION_RE.search(description or "")
    if not match:
        return {}
    return match.groupdict()


def build_result(
    *,
    provider: str,
    repository: str,
    pr_number: int,
    head: str,
    decision: Decision,
    findings: Optional[List[Dict[str, Any]]] = None,
    coverage_reason: Optional[str] = None,
    linked_issues: Optional[List[int]] = None,
    summary: Optional[str] = None,
    generated_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the normalized review-gate document."""

    normalized_findings = [dict(finding) for finding in (findings or [])]
    normalized_findings.sort(key=lambda finding: str(finding.get("id", "")))
    open_count = sum(
        1 for finding in normalized_findings if finding.get("status") in OPEN_STATUSES
    )
    result: Dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "provider": provider,
        "repository": repository,
        "pr": int(pr_number),
        "head": head or "",
        "state": decision.state,
        "verdict": decision.verdict,
        "gate_passed": bool(decision.gate_passed),
        "blocking": bool(decision.blocking),
        "reason": decision.reason,
        "open_findings": open_count,
        "findings": normalized_findings,
        "coverage": {
            "complete": decision.verdict != VERDICT_COMMENT,
            "reason": coverage_reason,
        },
        "linked_issues": sorted({int(number) for number in (linked_issues or [])}),
        "summary": summary or "",
        "generated_at": generated_at or "",
    }
    if metadata:
        result["metadata"] = metadata
    return result


def open_findings(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [finding for finding in result.get("findings", []) if finding.get("status") in OPEN_STATUSES]


def render_summary(result: Dict[str, Any], bot_prefix: str) -> str:
    """Human-readable review body. Untrusted text is escaped by the caller."""

    lines = [
        f"{bot_prefix} Review gate for HEAD `{result.get('head', '')}`",
        "",
        f"Verdict: `{result.get('verdict', VERDICT_NONE)}` (state: {result.get('state', '')}).",
        "",
    ]
    reason = result.get("reason")
    if reason:
        lines += [str(reason), ""]
    findings = result.get("findings", [])
    if findings:
        lines += ["| Finding | Location | Status | Title |", "| --- | --- | --- | --- |"]
        for finding in findings:
            lines.append(
                "| `{id}` | {loc} | {status} | {title} |".format(
                    id=finding.get("id", ""),
                    loc=finding.get("location", "general"),
                    status=finding.get("status", ""),
                    title=str(finding.get("title", ""))[:120],
                )
            )
        lines.append("")
    else:
        lines += ["No tracked findings.", ""]
    lines += [
        "Coverage: " + str((result.get("coverage") or {}).get("reason") or "complete"),
        "",
        "This review is produced by the Continuum review provider adapter and is the "
        "only review signal consumed by the Continuum merge controller.",
    ]
    return "\n".join(lines)
