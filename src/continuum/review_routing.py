"""Generic review-provider routing and review-repair contract.

This module is the provider-neutral layer required by Continuum #182. It
sits between provider adapters (CodeRabbit, PR-Agent) and the generic
OpenCode repair controller:

* provider adapters parse their own native review output;
* adapters emit a normalized actionable finding set through this module;
* the generic repair controller dispatches OpenCode once per normalized
  review batch;
* after repair, the selected provider adapter performs its native
  verification/re-review.

The canonical selector is ``review.provider`` (``none|coderabbit|pr-agent``)
from ``.continuum.yml``. Runtime workflows receive it as the single
normalized ``CONTINUUM_REVIEW_PROVIDER`` value. The two legacy booleans
(``CONTINUUM_REQUIRE_CODERABBIT`` and ``CONTINUUM_PR_AGENT_ENABLED``) are
kept only as deterministic migration compatibility: they resolve into
exactly one provider and any contradiction fails closed.

Standard library only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import (
    PROVIDER_CODERABBIT,
    PROVIDER_NONE,
    PROVIDER_PR_AGENT,
    SUPPORTED_PROVIDERS,
)

# Generic repair mode replacing the CodeRabbit-specific ``coderabbit-fix``.
# ``coderabbit-fix`` remains as a deprecated alias for safe migration.
REVIEW_FIX_MODE = "review-fix"
LEGACY_CODERABBIT_FIX_MODE = "coderabbit-fix"

# Normalized transport name. ``.continuum.yml`` remains authoritative;
# this environment value is generated transport only.
REVIEW_PROVIDER_ENV_VAR = "CONTINUUM_REVIEW_PROVIDER"
LEGACY_REQUIRE_CODERABBIT_VAR = "CONTINUUM_REQUIRE_CODERABBIT"
LEGACY_PR_AGENT_ENABLED_VAR = "CONTINUUM_PR_AGENT_ENABLED"

MAX_REPAIR_ATTEMPTS = 3


class ReviewRoutingError(ValueError):
    """Raised when provider selection or a repair contract is invalid."""


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes")


def normalize_provider(value: object) -> str:
    """Normalize one provider name, failing closed on unknown values."""

    text = str(value or "").strip().lower()
    if text not in SUPPORTED_PROVIDERS:
        raise ReviewRoutingError(
            "review provider must be one of "
            + ", ".join(SUPPORTED_PROVIDERS)
            + f"; got {value!r}"
        )
    return text


def resolve_review_provider(
    canonical: object = "",
    require_coderabbit: object = "",
    pr_agent_enabled: object = "",
) -> str:
    """Resolve the single active review provider.

    ``canonical`` is the ``review.provider`` value (or the normalized
    ``CONTINUUM_REVIEW_PROVIDER`` transport). The two legacy flags resolve
    deterministically into exactly one provider when the canonical value is
    absent. Any contradiction -- both legacy flags on, or a legacy flag that
    disagrees with an explicit canonical value -- fails closed.
    """

    canonical_text = str(canonical or "").strip().lower()
    has_canonical = bool(canonical_text)
    if has_canonical:
        canonical_text = normalize_provider(canonical_text)

    legacy_coderabbit = _is_truthy(require_coderabbit)
    legacy_pr_agent = _is_truthy(pr_agent_enabled)
    if legacy_coderabbit and legacy_pr_agent:
        raise ReviewRoutingError(
            "contradictory review configuration: "
            f"{LEGACY_REQUIRE_CODERABBIT_VAR} and "
            f"{LEGACY_PR_AGENT_ENABLED_VAR} are both enabled; "
            "select exactly one review provider"
        )
    legacy_provider: Optional[str] = None
    if legacy_coderabbit:
        legacy_provider = PROVIDER_CODERABBIT
    elif legacy_pr_agent:
        legacy_provider = PROVIDER_PR_AGENT

    if has_canonical:
        if legacy_provider is not None and legacy_provider != canonical_text:
            raise ReviewRoutingError(
                f"contradictory review configuration: canonical provider "
                f"{canonical_text!r} disagrees with legacy selection "
                f"{legacy_provider!r}; set {REVIEW_PROVIDER_ENV_VAR} only"
            )
        return canonical_text
    if legacy_provider is not None:
        return legacy_provider
    return PROVIDER_NONE


def resolve_review_provider_from_env(env: Mapping[str, object]) -> str:
    """Resolve the provider from a workflow environment mapping."""

    return resolve_review_provider(
        canonical=env.get(REVIEW_PROVIDER_ENV_VAR, ""),
        require_coderabbit=env.get(LEGACY_REQUIRE_CODERABBIT_VAR, ""),
        pr_agent_enabled=env.get(LEGACY_PR_AGENT_ENABLED_VAR, ""),
    )


def active_provider(provider: str) -> Optional[str]:
    """The required provider, or None when no external review is required."""

    normalized = normalize_provider(provider)
    if normalized == PROVIDER_NONE:
        return None
    return normalized


def require_single_provider(provider: str) -> str:
    """Assert exactly one provider is active (none counts as a choice)."""

    return normalize_provider(provider)


# -- Normalized finding / repair payload ------------------------------------


@dataclass(frozen=True)
class NormalizedFinding:
    """One provider-neutral actionable finding."""

    id: str
    path: str
    line: int
    severity: str
    summary: str
    body: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "summary": self.summary,
            "body": self.body,
        }


@dataclass(frozen=True)
class NormalizedRepairPayload:
    """Provider-neutral repair invocation contract.

    * ``provider``: ``coderabbit`` or ``pr-agent``;
    * ``pr_number``: pull request number;
    * ``review_id``: source review id / normalized review instance id;
    * ``reviewed_sha``: HEAD the provider evaluated;
    * ``head_sha``: exact current HEAD at dispatch time;
    * ``findings``: normalized actionable finding set with stable ids;
    * ``verification``: provider-owned verification strategy name.
    """

    provider: str
    pr_number: int
    review_id: str
    reviewed_sha: str
    head_sha: str
    findings: Tuple[NormalizedFinding, ...] = ()
    verification: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "pr_number": self.pr_number,
            "review_id": self.review_id,
            "reviewed_sha": self.reviewed_sha,
            "head_sha": self.head_sha,
            "findings": [finding.describe() for finding in self.findings],
            "verification": self.verification,
        }

    def finding_ids(self) -> Tuple[str, ...]:
        return tuple(finding.id for finding in self.findings)


def is_current_head(reviewed_sha: object, head_sha: object) -> bool:
    if not isinstance(reviewed_sha, str) or not isinstance(head_sha, str):
        return False
    if not reviewed_sha.strip() or not head_sha.strip():
        return False
    return reviewed_sha.strip().lower() == head_sha.strip().lower()


def stable_finding_id(provider: str, path: str, line: int, rule: str) -> str:
    """Stable finding identity shared by both adapters."""

    normalized = normalize_provider(provider) if provider else PROVIDER_NONE
    if normalized not in (PROVIDER_CODERABBIT, PROVIDER_PR_AGENT):
        raise ReviewRoutingError(f"cannot derive a finding id for provider {provider!r}")
    digest = hashlib.sha256(f"{path}:{line}:{rule}".encode()).hexdigest()[:12]
    prefix = "cr" if normalized == PROVIDER_CODERABBIT else "pra"
    return f"{prefix}-{digest}"


def coderabbit_finding_id(comment_id: int) -> str:
    """Stable id for one CodeRabbit inline review comment (its database id)."""

    if not isinstance(comment_id, int) or comment_id <= 0:
        raise ReviewRoutingError(f"invalid CodeRabbit finding id: {comment_id!r}")
    return f"cr-{comment_id}"


# -- Adapter: CodeRabbit native review -> normalized payload ------------------


@dataclass
class CodeRabbitClassification:
    dispatch: bool
    reason: str
    no_progress_marker: Optional[str] = None


def classify_coderabbit_review(
    review_state: str,
    review_body: str,
    review_id: int,
    reviewed_sha: str,
    head_sha: str,
    inline_findings: Sequence[Mapping[str, Any]],
) -> CodeRabbitClassification:
    """Classify one CodeRabbit review exactly like the current lifecycle.

    Mirrors ``continuum-opencode.yml`` ``dispatch-coderabbit-fix``: stale
    heads never dispatch, approvals record no-progress supersession, advisory
    states without nitpicks are ignored, and CHANGES_REQUESTED without an
    inline finding is a terminal non-code/policy blocker.
    """

    if not is_current_head(reviewed_sha, head_sha):
        return CodeRabbitClassification(
            dispatch=False, reason=f"stale review {review_id}: not current head"
        )
    state = (review_state or "").strip().lower()
    body = review_body or ""
    if state == "approved":
        return CodeRabbitClassification(
            dispatch=False,
            reason=f"approved exact HEAD {head_sha}; no-progress superseded",
        )
    actionable = state == "changes_requested" or (
        state == "commented" and bool(re.search(r"Nitpick comments", body, re.IGNORECASE))
    )
    if not actionable:
        return CodeRabbitClassification(
            dispatch=False, reason=f"advisory state={state}; no repair needed"
        )
    if state == "changes_requested" and len(list(inline_findings)) == 0:
        marker = (
            f"<!-- continuum-coderabbit-no-progress head={head_sha} "
            f"review={review_id} kind=non-code -->"
        )
        return CodeRabbitClassification(
            dispatch=False,
            reason="non-code/policy blocker without inline findings",
            no_progress_marker=marker,
        )
    return CodeRabbitClassification(dispatch=True, reason="actionable findings")


def coderabbit_review_to_payload(
    pr_number: int,
    review_id: int,
    review_state: str,
    review_body: str,
    reviewed_sha: str,
    head_sha: str,
    inline_comments: Sequence[Mapping[str, Any]],
) -> Optional[NormalizedRepairPayload]:
    """Convert a CodeRabbit review into the normalized repair contract."""

    findings: List[NormalizedFinding] = []
    for comment in inline_comments or []:
        if comment.get("pull_request_review_id") != review_id:
            continue
        if comment.get("in_reply_to_id"):
            continue
        comment_id = comment.get("id")
        if not isinstance(comment_id, int):
            continue
        findings.append(
            NormalizedFinding(
                id=coderabbit_finding_id(comment_id),
                path=str(comment.get("path", "") or ""),
                line=int(comment.get("line") or comment.get("original_line") or 0),
                severity=str(comment.get("severity", "") or "high"),
                summary=str(comment.get("body", "") or "")[:500],
                body=str(comment.get("body", "") or ""),
            )
        )
    classification = classify_coderabbit_review(
        review_state, review_body, review_id, reviewed_sha, head_sha, findings
    )
    if not classification.dispatch:
        return None
    findings_sorted = tuple(sorted(findings, key=lambda item: item.id))
    return NormalizedRepairPayload(
        provider=PROVIDER_CODERABBIT,
        pr_number=int(pr_number),
        review_id=str(review_id),
        reviewed_sha=reviewed_sha,
        head_sha=head_sha,
        findings=findings_sorted,
        verification="coderabbit-thread-verify",
    )


# -- Adapter: PR-Agent native review -> normalized payload --------------------


@dataclass
class PrAgentClassification:
    dispatch: bool
    reason: str


def pr_agent_review_to_payload(
    pr_number: int,
    review_instance_id: str,
    reviewed_sha: str,
    head_sha: str,
    findings: Sequence[NormalizedFinding],
    coverage_complete: bool,
) -> Optional[NormalizedRepairPayload]:
    """Convert a PR-Agent review into the same normalized repair contract."""

    if not is_current_head(reviewed_sha, head_sha):
        return None
    if not coverage_complete:
        raise ReviewRoutingError(
            "PR-Agent coverage is incomplete or unknown: failing closed"
        )
    actionable = [finding for finding in findings if finding.severity]
    if not actionable:
        return None
    ordered = tuple(sorted(actionable, key=lambda item: item.id))
    return NormalizedRepairPayload(
        provider=PROVIDER_PR_AGENT,
        pr_number=int(pr_number),
        review_id=str(review_instance_id),
        reviewed_sha=reviewed_sha,
        head_sha=head_sha,
        findings=ordered,
        verification="pr-agent-verify",
    )


def pr_agent_findings_from_text(
    text: str,
    path_fallback: str = "",
) -> List[NormalizedFinding]:
    """Parse ``path:line [severity] rule -- summary`` lines (test helper)."""

    from .pr_agent import parse_findings as _parse

    return [
        NormalizedFinding(
            id=finding.id,
            path=finding.path or path_fallback,
            line=finding.line,
            severity=finding.severity,
            summary=finding.summary,
            body=finding.summary,
        )
        for finding in _parse(text or "")
    ]


# -- Generic repair controller -------------------------------------------------


def dispatch_key(payload: NormalizedRepairPayload) -> str:
    """Deduplication key: provider/review/head/finding-set."""

    return "|".join(
        [
            payload.provider,
            payload.review_id,
            payload.head_sha.lower(),
            ",".join(sorted(payload.finding_ids())),
        ]
    )


def should_dispatch_repair(
    payload: Optional[NormalizedRepairPayload],
    current_head_sha: str,
    seen_keys: Sequence[str] = (),
) -> bool:
    """Whether one normalized review gets exactly one OpenCode dispatch."""

    if payload is None:
        return False
    if not payload.findings:
        return False
    if not is_current_head(payload.reviewed_sha, current_head_sha):
        return False
    if not is_current_head(payload.head_sha, current_head_sha):
        return False
    if dispatch_key(payload) in set(seen_keys):
        return False
    return True


def same_head_no_progress(
    previous_head: str,
    current_head: str,
    changed: bool,
    already_attempted: bool,
) -> bool:
    """Same-head no-progress protection against infinite review/fix loops."""

    if not previous_head or not current_head:
        return False
    if previous_head.strip().lower() != current_head.strip().lower():
        return False
    return already_attempted and not changed


def bounded_retry(attempt: int, max_attempts: int = MAX_REPAIR_ATTEMPTS) -> bool:
    """Whether an unresolved finding may re-enter the bounded repair loop."""

    if max_attempts <= 0:
        raise ReviewRoutingError("max repair attempts must be positive")
    return 0 <= attempt < max_attempts


def classify_retry(error: str) -> str:
    """Distinguish infrastructure retries from semantic repair retries."""

    text = (error or "").lower()
    infra_markers = ("timeout", "rate limit", "rate-limited", "502", "503", "504", "network", "econnreset")
    if any(marker in text for marker in infra_markers):
        return "infrastructure"
    return "semantic"


UNRESOLVED_RE = re.compile(r"\bUNRESOLVED\b", re.IGNORECASE)
RESOLVED_RE = re.compile(r"\*\*RESOLVED(?:\.|\*\*)", re.IGNORECASE)


def verification_verdict(text: str) -> str:
    """Machine-readable resolved/unresolved verdict (UNRESOLVED wins)."""

    body = text or ""
    if UNRESOLVED_RE.search(body):
        return "UNRESOLVED"
    if RESOLVED_RE.search(body):
        return "RESOLVED"
    return "UNRESOLVED"


# -- Normalized merge gate -----------------------------------------------------


@dataclass
class MergeGate:
    green: bool
    reason: str


def normalized_merge_gate(
    provider: str,
    reviewed_sha: str,
    head_sha: str,
    unresolved_count: int,
    coverage_complete: bool,
    ci_green: bool,
) -> MergeGate:
    """Normalized selected-provider review gate consumed by auto-merge."""

    normalized = normalize_provider(provider)
    if normalized == PROVIDER_NONE:
        if not ci_green:
            return MergeGate(green=False, reason="CI is not green")
        return MergeGate(green=True, reason="no external review provider required")
    if not is_current_head(reviewed_sha, head_sha):
        return MergeGate(green=False, reason="stale or missing current-head review")
    if not coverage_complete:
        return MergeGate(green=False, reason="review coverage is incomplete or unknown")
    if unresolved_count > 0:
        return MergeGate(
            green=False,
            reason=f"{unresolved_count} unresolved required finding(s)",
        )
    if not ci_green:
        return MergeGate(green=False, reason="CI is not green")
    return MergeGate(
        green=True, reason=f"{normalized} review is green for exact HEAD {head_sha}"
    )


def review_fix_prompt(payload: NormalizedRepairPayload) -> str:
    """Provider-neutral OpenCode repair prompt (no provider-specific logic)."""

    lines = [
        f"Fix every review finding on pull request #{payload.pr_number}.",
        "",
        "Context:",
        f"- The selected review provider is {payload.provider}.",
        f"- The normalized review instance is {payload.review_id}.",
        f"- The reviewed commit is {payload.reviewed_sha}.",
        f"- The exact current HEAD is {payload.head_sha}.",
        f"- Verification strategy: {payload.verification}.",
        "- Work only in the current checked-out PR branch.",
        "",
        "Findings:",
    ]
    for finding in payload.findings:
        lines.append(
            f"- {finding.id} {finding.path}:{finding.line} "
            f"[{finding.severity}] {finding.summary}"
        )
    lines += [
        "",
        "Requirements:",
        "- Address every finding in this batch in one repair pass.",
        "- Keep fixes scoped to the listed feedback.",
        "- Run the relevant tests, linters, or validation.",
        "- Commit and push fixes to the current PR branch.",
        "- If a finding is incorrect, explain why in the PR conversation.",
        "- Do not resolve or dismiss review threads yourself.",
        "- Keep all comments and commit messages in English.",
    ]
    return "\n".join(lines)


def describe_provider_transport(provider: str) -> Dict[str, str]:
    """The single normalized workflow transport value for a provider."""

    return {REVIEW_PROVIDER_ENV_VAR: normalize_provider(provider)}


__all__ = [
    "LEGACY_CODERABBIT_FIX_MODE",
    "LEGACY_PR_AGENT_ENABLED_VAR",
    "LEGACY_REQUIRE_CODERABBIT_VAR",
    "MAX_REPAIR_ATTEMPTS",
    "REVIEW_FIX_MODE",
    "REVIEW_PROVIDER_ENV_VAR",
    "MergeGate",
    "NormalizedFinding",
    "NormalizedRepairPayload",
    "PrAgentClassification",
    "CodeRabbitClassification",
    "ReviewRoutingError",
    "active_provider",
    "bounded_retry",
    "classify_coderabbit_review",
    "classify_retry",
    "coderabbit_finding_id",
    "coderabbit_review_to_payload",
    "describe_provider_transport",
    "dispatch_key",
    "is_current_head",
    "normalized_merge_gate",
    "normalize_provider",
    "pr_agent_findings_from_text",
    "pr_agent_review_to_payload",
    "require_single_provider",
    "resolve_review_provider",
    "resolve_review_provider_from_env",
    "review_fix_prompt",
    "same_head_no_progress",
    "should_dispatch_repair",
    "stable_finding_id",
    "verification_verdict",
]
