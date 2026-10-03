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


# -- Adapter: PR-Agent persistent state block -> normalized payload -------------
#
# Live defect oracle (#186): the OpenCode-backed PR-Agent backend does NOT
# emit its findings as GitHub inline review comments. It emits them in the
# persistent ``PR Reviewer Guide`` issue comment inside a machine-readable
# block::
#
#     <!-- pr-agent-review-state:v1
#     {...}
#     -->
#
# PR #185 had zero review submissions and zero inline findings while the
# state block carried six ACTIVE findings, so an adapter that counts inline
# comments false-approves a review with real findings. The functions below
# are the deterministic oracle the workflow adapters mirror: parse only
# provider-owned state for the exact reviewed HEAD, fail closed on
# incomplete/partial coverage, ignore arbitrary human/other-bot comments,
# batch completely without silent loss, and validate privileged triggers.

PR_AGENT_STATE_MARKER = "pr-agent-review-state:v1"
PR_AGENT_STATE_RE = re.compile(
    r"<!--\s*pr-agent-review-state:v1\s*\n(?P<json>.*?)-->",
    re.DOTALL,
)
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
ACTIVE_FINDING_STATUSES = ("ACTIVE", "OPEN", "UNRESOLVED")
RESOLVED_FINDING_STATUSES = ("RESOLVED", "FIXED", "DISMISSED", "DONE")
PR_AGENT_BLOCKING_SEVERITIES = ("blocker", "critical", "high")
MAX_FINDINGS_PER_DISPATCH = 50
MAX_DISPATCH_BATCHES = 10


def extract_pr_agent_state_blocks(body: str) -> List[str]:
    """Return every raw ``pr-agent-review-state:v1`` JSON payload in a body."""

    if not body or PR_AGENT_STATE_MARKER not in body:
        return []
    return [
        match.group("json").strip()
        for match in PR_AGENT_STATE_RE.finditer(body)
    ]


def parse_pr_agent_state_block(raw: str) -> Dict[str, Any]:
    """Parse one raw state payload, failing closed on malformed JSON."""

    try:
        parsed = __import__("json").loads(raw)
    except Exception as exc:
        raise ReviewRoutingError(
            f"PR-Agent review state is not valid JSON: {exc}"
        ) from None
    if not isinstance(parsed, dict):
        raise ReviewRoutingError("PR-Agent review state must be a JSON object")
    return parsed


def _state_head(state: Mapping[str, Any]) -> str:
    for key in ("reviewed_sha", "head_sha", "head", "sha", "commit"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    last_run = state.get("last_run")
    if isinstance(last_run, dict):
        for key in ("reviewed_sha", "head_sha", "head", "sha"):
            value = last_run.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _state_review_id(state: Mapping[str, Any]) -> str:
    for key in ("review_id", "review_instance_id", "instance_id", "id"):
        value = state.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return _state_head(state)


def _state_provider(state: Mapping[str, Any]) -> str:
    provider = state.get("provider", "pr-agent")
    return str(provider or "pr-agent").strip().lower()


def is_pr_agent_state_complete(state: Mapping[str, Any]) -> bool:
    """Whether a state block represents a complete review (fail-closed gate)."""

    if not isinstance(state, dict):
        return False
    last_run = state.get("last_run")
    if isinstance(last_run, dict) and "complete" in last_run:
        if not last_run.get("complete") is True:
            return False
    elif "complete" in state:
        if not state.get("complete") is True:
            return False
    coverage = state.get("coverage")
    if isinstance(coverage, dict) and ("reviewed" in coverage or "total" in coverage):
        try:
            reviewed = int(coverage.get("reviewed", 0))
            total = int(coverage.get("total", 0))
        except (TypeError, ValueError):
            return False
        if total <= 0 or reviewed < total:
            return False
    return True


def pr_agent_state_active_findings(
    state: Mapping[str, Any],
) -> List[NormalizedFinding]:
    """ACTIVE findings from provider-owned state (resolved ones excluded)."""

    raw_findings = state.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ReviewRoutingError("PR-Agent review state findings must be a list")
    active: List[NormalizedFinding] = []
    for entry in raw_findings:
        if not isinstance(entry, dict):
            raise ReviewRoutingError("PR-Agent review state finding must be an object")
        status = str(entry.get("status", "ACTIVE") or "ACTIVE").strip().upper()
        if status in RESOLVED_FINDING_STATUSES:
            continue
        if status not in ACTIVE_FINDING_STATUSES:
            raise ReviewRoutingError(
                f"PR-Agent review state has unknown finding status {status!r}: failing closed"
            )
        path = str(entry.get("path", "") or "")
        try:
            line = int(entry.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        severity = str(entry.get("severity", "") or "high").strip().lower()
        rule = str(entry.get("rule", "") or entry.get("id", "") or summary_fallback(entry))
        finding_id = str(entry.get("id", "") or "").strip()
        if not finding_id:
            finding_id = stable_finding_id(PROVIDER_PR_AGENT, path, line, rule or path)
        summary = str(entry.get("summary", "") or entry.get("title", "") or rule)
        active.append(
            NormalizedFinding(
                id=finding_id,
                path=path,
                line=line,
                severity=severity,
                summary=summary[:500],
                body=summary,
            )
        )
    return active


def summary_fallback(entry: Mapping[str, Any]) -> str:
    return str(entry.get("summary", "") or entry.get("title", "") or entry.get("rule", "") or "")


def select_pr_agent_state_for_head(
    bodies: Sequence[str],
    head_sha: str,
) -> Optional[Dict[str, Any]]:
    """Select the latest provider-owned state block for the exact HEAD.

    ``bodies`` are comment bodies in chronological order (oldest first);
    the last exact-head state wins. Blocks from other heads are stale and
    never selected, so an old-head ACTIVE set cannot block a newly clean
    current HEAD.
    """

    selected: Optional[Dict[str, Any]] = None
    for body in bodies or []:
        for raw in extract_pr_agent_state_blocks(body or ""):
            try:
                state = parse_pr_agent_state_block(raw)
            except ReviewRoutingError:
                raise
            if _state_provider(state) != PROVIDER_PR_AGENT:
                continue
            if not is_current_head(_state_head(state), head_sha):
                continue
            selected = state
    return selected


def is_valid_head_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX40_RE.match(value.strip()))


def pr_agent_state_to_payload(
    pr_number: int,
    state: Mapping[str, Any],
    current_head_sha: str,
) -> Optional[NormalizedRepairPayload]:
    """Normalize one provider-owned state block (fail-closed on partial).

    Returns ``None`` for a complete clean exact-head review (APPROVED, no
    dispatch) or a stale head (rejected, never dispatched). Raises
    :class:`ReviewRoutingError` when coverage is incomplete/partial/unknown
    so the caller can fail closed instead of approving.
    """

    if _state_provider(state) != PROVIDER_PR_AGENT:
        raise ReviewRoutingError("PR-Agent state provider must be pr-agent")
    reviewed_sha = _state_head(state)
    if not reviewed_sha or not is_current_head(reviewed_sha, current_head_sha):
        return None
    if not is_pr_agent_state_complete(state):
        raise ReviewRoutingError(
            "PR-Agent review state is incomplete or partial: failing closed"
        )
    active = pr_agent_state_active_findings(state)
    if not active:
        return None
    ordered = tuple(sorted(active, key=lambda item: item.id))
    return NormalizedRepairPayload(
        provider=PROVIDER_PR_AGENT,
        pr_number=int(pr_number),
        review_id=_state_review_id(state) or reviewed_sha,
        reviewed_sha=reviewed_sha,
        head_sha=current_head_sha,
        findings=ordered,
        verification="pr-agent-verify",
    )


def pr_agent_state_verdict(
    state: Optional[Mapping[str, Any]],
    current_head_sha: str,
) -> str:
    """Normalized review verdict from provider-owned state.

    ``APPROVED`` only for a complete clean exact-head review; every other
    shape (missing, stale, incomplete, ACTIVE findings) is
    ``CHANGES_REQUESTED`` or ``STALE``/fail-closed and must never read green.
    """

    if state is None:
        return "STALE"
    reviewed_sha = _state_head(state)
    if not is_current_head(reviewed_sha, current_head_sha):
        return "STALE"
    if not is_pr_agent_state_complete(state):
        return "CHANGES_REQUESTED"
    active = pr_agent_state_active_findings(state)
    blocking = [
        finding
        for finding in active
        if finding.severity.lower() in PR_AGENT_BLOCKING_SEVERITIES
        or not finding.severity
    ]
    # Any ACTIVE finding blocks: unknown/empty severity fails closed too.
    actionable = blocking if blocking else active
    return "CHANGES_REQUESTED" if actionable else "APPROVED"


def batch_normalized_findings(
    findings: Sequence[NormalizedFinding],
    per_batch: int = MAX_FINDINGS_PER_DISPATCH,
) -> List[Tuple[NormalizedFinding, ...]]:
    """Split findings into bounded complete batches (no silent loss)."""

    items = list(findings or [])
    if not items:
        raise ReviewRoutingError("cannot batch an empty finding set")
    if per_batch <= 0:
        raise ReviewRoutingError("batch size must be positive")
    batches = [
        tuple(items[index : index + per_batch])
        for index in range(0, len(items), per_batch)
    ]
    if len(batches) > MAX_DISPATCH_BATCHES:
        raise ReviewRoutingError(
            f"PR-Agent finding set needs {len(batches)} batches: failing closed"
        )
    # Completeness proof: every finding appears exactly once.
    flattened = [finding.id for batch in batches for finding in batch]
    if sorted(flattened) != sorted(finding.id for finding in items):
        raise ReviewRoutingError("finding batch coverage is incomplete: failing closed")
    return batches


def is_provider_owned_login(login: object, repo_owner: str) -> bool:
    """Whether a comment author is provider-owned (never an arbitrary user)."""

    if not isinstance(login, str) or not login.strip():
        return False
    text = login.strip()
    return text == repo_owner or text in (
        "github-actions[bot]",
        "github-actions",
    )


def validate_pr_agent_trigger(
    unresolved_author: str,
    verification_author: str,
    repo_owner: str,
    finding_id: str,
    state: Optional[Mapping[str, Any]],
    current_head_sha: str,
) -> bool:
    """Deterministic privileged-trigger gate for UNRESOLVED re-entry.

    All of these must hold before a repair dispatch: the verification
    request is provider-owned, the UNRESOLVED reply is provider-owned (an
    arbitrary human reply can never trigger privileged repair), the
    finding identity exists in the provider-owned ACTIVE set for the
    exact current HEAD, and the state head correlates.
    """

    if not is_provider_owned_login(verification_author, repo_owner):
        return False
    if not is_provider_owned_login(unresolved_author, repo_owner):
        return False
    if state is None:
        return False
    if not is_current_head(_state_head(state), current_head_sha):
        return False
    if not is_pr_agent_state_complete(state):
        return False
    try:
        active_ids = {finding.id for finding in pr_agent_state_active_findings(state)}
    except ReviewRoutingError:
        return False
    return str(finding_id or "").strip() in active_ids


def select_verification_findings(
    state: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]],
    head_sha: str,
    repo_owner: str,
) -> List[Mapping[str, Any]]:
    """Select exact normalized finding threads for verification.

    Only provider-owned top-level comments whose normalized finding id is
    in the provider-owned ACTIVE set for the exact HEAD are returned.
    Replies and unrelated comments are always excluded.
    """

    try:
        active_ids = {finding.id for finding in pr_agent_state_active_findings(state)}
    except ReviewRoutingError:
        return []
    if not is_current_head(_state_head(state), head_sha):
        return []
    selected: List[Mapping[str, Any]] = []
    for comment in comments or []:
        if comment.get("in_reply_to_id"):
            continue
        if not is_provider_owned_login(comment.get("user", {}).get("login")
                                        if isinstance(comment.get("user"), dict)
                                        else comment.get("user_login", ""), repo_owner):
            # Fall back to explicit login field used by tests.
            login = ""
            user = comment.get("user")
            if isinstance(user, dict):
                login = str(user.get("login", "") or "")
            elif isinstance(comment.get("user_login"), str):
                login = str(comment.get("user_login") or "")
            else:
                login = str(comment.get("author", "") or "")
            if not is_provider_owned_login(login, repo_owner):
                continue
        candidate = str(comment.get("finding_id", "") or comment.get("id", "") or "")
        # Tests and live threads identify findings by stable id; accept the
        # explicit normalized id or a body-embedded `finding=<id>` token.
        body_ids = re.findall(r"finding=([A-Za-z0-9_-]+)", str(comment.get("body", "") or ""))
        if candidate in active_ids or any(token in active_ids for token in body_ids):
            selected.append(comment)
    return selected


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
    "ACTIVE_FINDING_STATUSES",
    "LEGACY_CODERABBIT_FIX_MODE",
    "LEGACY_PR_AGENT_ENABLED_VAR",
    "LEGACY_REQUIRE_CODERABBIT_VAR",
    "MAX_DISPATCH_BATCHES",
    "MAX_FINDINGS_PER_DISPATCH",
    "MAX_REPAIR_ATTEMPTS",
    "PR_AGENT_BLOCKING_SEVERITIES",
    "PR_AGENT_STATE_MARKER",
    "PR_AGENT_STATE_RE",
    "RESOLVED_FINDING_STATUSES",
    "REVIEW_FIX_MODE",
    "REVIEW_PROVIDER_ENV_VAR",
    "MergeGate",
    "NormalizedFinding",
    "NormalizedRepairPayload",
    "PrAgentClassification",
    "CodeRabbitClassification",
    "ReviewRoutingError",
    "active_provider",
    "batch_normalized_findings",
    "bounded_retry",
    "classify_coderabbit_review",
    "classify_retry",
    "coderabbit_finding_id",
    "coderabbit_review_to_payload",
    "describe_provider_transport",
    "dispatch_key",
    "extract_pr_agent_state_blocks",
    "is_current_head",
    "is_pr_agent_state_complete",
    "is_provider_owned_login",
    "is_valid_head_sha",
    "normalized_merge_gate",
    "normalize_provider",
    "parse_pr_agent_state_block",
    "pr_agent_findings_from_text",
    "pr_agent_review_to_payload",
    "pr_agent_state_active_findings",
    "pr_agent_state_to_payload",
    "pr_agent_state_verdict",
    "require_single_provider",
    "resolve_review_provider",
    "resolve_review_provider_from_env",
    "review_fix_prompt",
    "same_head_no_progress",
    "select_pr_agent_state_for_head",
    "select_verification_findings",
    "should_dispatch_repair",
    "stable_finding_id",
    "validate_pr_agent_trigger",
    "verification_verdict",
]
