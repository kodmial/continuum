"""Isolated upstream PR-Agent lifecycle for Continuum (issue #196).

This stack is a second, isolated review stack beside CodeRabbit. It provides
behavioral parity only where upstream PR-Agent v0.46.0 documents a primitive.
It never emulates missing CodeRabbit features and never modifies CodeRabbit.

Pinned upstream: PR-Agent v0.46.0.

Authoritative v0.46.0 sources followed here (not assumptions):
- usage-guide/automations_and_usage.md (automation, GitHub Action outputs,
  push-trigger defaults, check-run publishing)
- tools/review.md (review tool)
- tools/improve.md (improve tool, code suggestions)
- usage-guide/configuration_reference.md (native push outputs)
- pr_reviewer_prompts.toml (PRReview schema: key_issues_to_review,
  merge_recommendation, ticket compliance, security/risk fields)
- configuration.toml (persistent_finding_state, persistent_inline_comments,
  large-PR chunking, propagate_tool_errors)
- installation/github.md (GitHub Action integration)

Native capabilities used: review, structured review output,
persistent_finding_state, inline_key_issues, merge recommendation,
tests/security/risk/ticket-compliance fields, large-PR chunking, improve,
native push outputs, fail-on-tool-errors, persistent inline-comment
de-duplication. Out of scope: describe, ask, add_docs, changelog generation.

Transport only: this module never talks to an inference endpoint directly.
The OpenCode/LiteLLM-compatible bridge (existing transport layer) owns the
wire translation. This module owns deterministic lifecycle decisions over
native upstream outputs only. It implements no review engine, no severity
inference, no finding identity scheme, no resolution protocol, no chunking,
and no native review-state emulation.

Finding state: per-finding ACTIVE/RESOLVED history is owned by upstream
PR-Agent v0.46.0 persistent_finding_state through its persistent review
comment. This module reads only the upstream-structured state object handed
to it (a list/mapping with a status field per finding). Hidden marker
parsing lives in the pinned upstream implementation;
this file contains no marker format knowledge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence


PR_AGENT_VERSION = "0.46.0"

UPSTREAM_FINDING_STATE_VERSION = "0.46.0"

UPSTREAM_ACTION_REF = "docker://pragent/pr-agent:0.46.0-github_action"

REVIEW_OUTPUT_REF = "steps.pragent.outputs.review"

PUSH_OUTPUTS_FILE_PATH = "pr-agent-outputs/continuum.jsonl"

NUM_MAX_FINDINGS = 6

SUGGESTIONS_SCORE_THRESHOLD = 1

REVIEW_MERGE_SAFE = "safe_to_merge"
REVIEW_MERGE_CAUTION = "merge_with_caution"
REVIEW_MERGE_CHANGES = "changes_required"

STATE_ACTIVE = "ACTIVE"
STATE_RESOLVED = "RESOLVED"

CONFLICT_LOCK_LABEL = "opencode-conflict-repair"

CONFLICT_REPAIR_TIMEOUT_MINUTES = 60

CONFLICT_REPAIR_ATTEMPTS_PER_HEAD = 1

RETRY_MAX_ATTEMPTS = 3

RETRY_BASE_DELAY_SECONDS = 15

PR_AGENT_WORKFLOWS = (
    "continuum-pr-agent.yml",
    "continuum-pr-agent-repair.yml",
    "continuum-pr-agent-auto-merge.yml",
)

CODERABBIT_WORKFLOWS = (
    "continuum-coderabbit-retry.yml",
    "continuum-coderabbit-unresolved.yml",
)

GENERAL_MERGE_WORKFLOWS = (
    "continuum-add-review-label.yml",
    "continuum-auto-merge.yml",
)


class LifecycleError(ValueError):
    """Raised when the PR-Agent lifecycle gate refuses to proceed."""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes")


def resolve_consumer_mode(env: Mapping[str, object]) -> Dict[str, Any]:
    """Resolve the single authoritative review provider.

    CONTINUUM_REVIEW_PROVIDER accepts exactly:
      none | coderabbit | pr-agent

    Missing/empty configuration defaults to none.
    """

    stack = str(env.get("CONTINUUM_REVIEW_PROVIDER", "") or "none").strip().lower()
    if not stack:
        stack = "none"
    if stack not in ("none", "coderabbit", "pr-agent"):
        raise LifecycleError(
            "invalid CONTINUUM_REVIEW_PROVIDER: expected none, coderabbit, or pr-agent"
        )
    return {
        "review_provider": stack,
        "pr_agent_enabled": stack == "pr-agent",
        "require_coderabbit": stack == "coderabbit",
        "stack": stack,
    }

def workflows_for_stack(stack: str) -> List[str]:
    """Workflows invoked for one complete stack (no mixing of gates)."""

    if stack == "pr-agent":
        return list(PR_AGENT_WORKFLOWS)
    if stack == "coderabbit":
        return list(CODERABBIT_WORKFLOWS)
    if stack == "none":
        return []
    raise LifecycleError(f"unknown review stack: {stack!r}")


def is_same_head(expected_sha: str, actual_sha: str) -> bool:
    """A result is valid only for the exact HEAD SHA it evaluated."""

    if not expected_sha or not actual_sha:
        return False
    return expected_sha.strip().lower() == actual_sha.strip().lower()


def admission_allowed(
    *,
    pr_state: str,
    is_draft: bool,
    head_repo: str,
    base_repo: str,
    ci_green_on_exact_head: bool,
) -> Dict[str, Any]:
    """Admission for an upstream review: open, non-draft, same-repo, CI green."""

    if (pr_state or "").strip().lower() != "open":
        return {"allowed": False, "reason": "pull request is not open"}
    if is_draft:
        return {"allowed": False, "reason": "draft pull requests are not admitted"}
    if not head_repo or not base_repo or head_repo != base_repo:
        return {"allowed": False, "reason": "only same-repository pull requests are admitted"}
    if not ci_green_on_exact_head:
        return {"allowed": False, "reason": "exact current HEAD has no green CI"}
    return {"allowed": True, "reason": "admitted"}


def parse_review_json(payload: object) -> Dict[str, Any]:
    """Parse the canonical machine source for repair/gating.

    Canonical source is the GitHub Action output JSON
    (steps.<id>.outputs.review) matching the upstream PRReview schema.
    Prose comments are never the primary protocol. Fails closed on
    invalid or missing JSON.
    """

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise LifecycleError("PR-Agent review JSON is missing")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LifecycleError(f"PR-Agent review JSON is invalid: {exc}") from None
    if not isinstance(payload, dict):
        raise LifecycleError("PR-Agent review JSON must be an object")
    review = payload.get("review", payload)
    if not isinstance(review, dict):
        raise LifecycleError("PR-Agent review JSON has no review object")
    if "key_issues_to_review" not in review:
        raise LifecycleError("PR-Agent review JSON has no key_issues_to_review")
    key_issues = review.get("key_issues_to_review")
    if key_issues is None:
        raise LifecycleError("PR-Agent review JSON key_issues_to_review is null")
    if not isinstance(key_issues, list):
        raise LifecycleError("PR-Agent review JSON key_issues_to_review must be a list")
    return dict(review)


def current_key_issues(review: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Canonical current review-finding payload for one bounded repair pass."""

    items = review.get("key_issues_to_review", [])
    if not isinstance(items, list):
        raise LifecycleError("key_issues_to_review must be a list")
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise LifecycleError("each key issue must be an object")
        out.append(dict(item))
    return out


def merge_recommendation(review: Mapping[str, Any]) -> str:
    """Upstream opt-in merge recommendation for the reviewed HEAD."""

    value = review.get("merge_recommendation", "")
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError("PR-Agent review has no merge_recommendation")
    return value.strip()


def is_potentially_truncated(review: Mapping[str, Any], cap: int = NUM_MAX_FINDINGS) -> bool:
    """A batch cap is not a cleanliness signal.

    When a review returns exactly the configured cap, treat the result
    conservatively as potentially truncated: repair the returned batch,
    re-run a complete review on the new HEAD, and never declare clean.
    """

    return len(current_key_issues(review)) == cap


def parse_improve_push_outputs(text: object) -> List[Dict[str, Any]]:
    """Capture native improve suggestions via the runner-local file channel.

    Reads only the documented push_outputs file channel
    (pr-agent-outputs/continuum.jsonl) payload.code_suggestions.
    Comment scraping is never used. Fails closed on unreadable JSON.
    """

    if text is None:
        raise LifecycleError("improve push_outputs file payload is missing")
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if not isinstance(text, str):
        raise LifecycleError("improve push_outputs file payload must be text")
    stripped = text.strip()
    if not stripped:
        return []
    suggestions: List[Dict[str, Any]] = []
    for line_number, line in enumerate(stripped.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LifecycleError(
                f"improve push_outputs line {line_number} is invalid: {exc}"
            ) from None
        if not isinstance(record, dict):
            raise LifecycleError(
                f"improve push_outputs line {line_number} must be an object"
            )
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            raise LifecycleError(
                f"improve push_outputs line {line_number} has no payload object"
            )
        batch = payload.get("code_suggestions", [])
        if batch is None:
            continue
        if not isinstance(batch, list):
            raise LifecycleError(
                f"improve push_outputs line {line_number} code_suggestions must be a list"
            )
        for entry in batch:
            if not isinstance(entry, dict):
                raise LifecycleError(
                    f"improve push_outputs line {line_number} suggestion must be an object"
                )
            suggestions.append(dict(entry))
    return suggestions


def qualifying_suggestions(
    suggestions: Sequence[Mapping[str, Any]],
    threshold: int = SUGGESTIONS_SCORE_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Qualifying native improve suggestions for the same repair batch.

    The threshold of 1 is intentional: actionable native suggestions must
    not be silently discarded by an arbitrary Continuum severity filter.
    A missing score is treated conservatively as qualifying.
    """

    out: List[Dict[str, Any]] = []
    for entry in suggestions:
        score = entry.get("score", None)
        if score is None:
            out.append(dict(entry))
            continue
        try:
            numeric = int(score)
        except (TypeError, ValueError):
            try:
                numeric = int(float(str(score)))
            except (TypeError, ValueError):
                out.append(dict(entry))
                continue
        if numeric >= threshold:
            out.append(dict(entry))
    return out


@dataclass
class RepairBatch:
    """One bounded OpenCode repair pass containing every current item."""

    head_sha: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    bounded: bool = True

    def describe(self) -> Dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            "bounded": self.bounded,
            "items": [dict(item) for item in self.items],
        }


def build_repair_batch(
    review: Mapping[str, Any],
    suggestions: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
) -> RepairBatch:
    """Batch every current review finding plus every qualifying suggestion.

    Each item preserves its native source (review or improve). The batch
    processes every item together; it never stops after the first finding
    and never dispatches one agent per finding. The OpenCode pass itself
    is bounded; the item list is complete, never truncated.
    """

    if not head_sha or not head_sha.strip():
        raise LifecycleError("repair batch requires the exact reviewed HEAD")
    items: List[Dict[str, Any]] = []
    for index, finding in enumerate(current_key_issues(review)):
        items.append({"source": "review", "index": index, "finding": dict(finding)})
    for index, suggestion in enumerate(qualifying_suggestions(suggestions)):
        items.append({"source": "improve", "index": index, "suggestion": dict(suggestion)})
    return RepairBatch(head_sha=head_sha.strip(), items=items, bounded=True)


def upstream_state_has_active(state: object) -> bool:
    """Whether native persistent state still contains an ACTIVE finding.

    Reads the upstream v0.46.0 state object: top-level `findings` entries
    use `state=ACTIVE|RESOLVED`. Marker parsing stays in pinned upstream
    PR-Agent and is not reproduced here.
    """

    if not isinstance(state, dict):
        raise LifecycleError("upstream finding state must be the v0.46.0 state object")
    raw = state.get("findings")
    if not isinstance(raw, list):
        raise LifecycleError("upstream finding state findings must be a list")
    findings: Sequence[Any] = raw
    for entry in findings:
        if not isinstance(entry, dict):
            raise LifecycleError("upstream finding state contains a non-object finding")
        finding_state = str(entry.get("state", "") or "").strip().upper()
        if finding_state not in (STATE_ACTIVE, STATE_RESOLVED):
            raise LifecycleError("upstream finding state contains an unknown state")
        if finding_state == STATE_ACTIVE:
            return True
    return False


def review_has_ticket_compliance(review: Mapping[str, Any]) -> bool:
    """Whether the review carries native ticket-compliance evidence."""

    for key in ("ticket_compliance_check", "ticket_compliance", "ticket_analysis"):
        value = review.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def ticket_compliance_ok(pr_body: str, review: Mapping[str, Any]) -> bool:
    """Native ticket review must evaluate the PR against its source issue.

    OpenCode-created PRs reference their source issue, so a PR body that
    links an issue requires native ticket-compliance evidence in the
    review. A PR without an issue link is vacuously compliant here.
    """

    import re

    body = pr_body or ""
    linked = bool(re.search(r"#\d+", body) or re.search(r"/issues/\d+", body))
    if not linked:
        return True
    return review_has_ticket_compliance(review)


@dataclass
class GateInputs:
    review: Dict[str, Any]
    qualifying_improve: List[Dict[str, Any]]
    persistent_state: object
    ci_green_on_exact_head: bool
    head_matches: bool
    review_coverage_complete: bool
    improve_coverage_complete: bool
    tool_error: bool = False


def evaluate_gate(decision: GateInputs) -> Dict[str, Any]:
    """Exact-head, fail-closed PR-Agent merge gate.

    Green requires every native signal at once: no tool error, complete
    review and improve coverage, green CI on the exact HEAD, matching
    HEAD, safe_to_merge, empty current key issues, no qualifying improve
    suggestions, no ACTIVE finding in native persistent state, and no
    potentially truncated batch. Anything else blocks with a reason.
    """

    if decision.tool_error:
        return {"green": False, "reason": "tool error: failing closed"}
    if not decision.review_coverage_complete:
        return {"green": False, "reason": "incomplete review coverage: failing closed"}
    if not decision.improve_coverage_complete:
        return {"green": False, "reason": "incomplete improve coverage: failing closed"}
    if not decision.ci_green_on_exact_head:
        return {"green": False, "reason": "current-head CI is not green"}
    if not decision.head_matches:
        return {"green": False, "reason": "stale head: result is not for the current HEAD"}
    try:
        recommendation = merge_recommendation(decision.review)
    except LifecycleError as exc:
        return {"green": False, "reason": f"invalid review JSON: {exc}"}
    if recommendation != REVIEW_MERGE_SAFE:
        return {"green": False, "reason": f"merge recommendation blocks: {recommendation}"}
    issues = current_key_issues(decision.review)
    if issues:
        if is_potentially_truncated(decision.review):
            return {
                "green": False,
                "reason": "review batch reached the findings cap: potentially truncated",
            }
        return {"green": False, "reason": f"{len(issues)} current key issue(s) remain"}
    if decision.qualifying_improve:
        return {
            "green": False,
            "reason": f"{len(decision.qualifying_improve)} qualifying suggestion(s) remain",
        }
    if upstream_state_has_active(decision.persistent_state):
        return {"green": False, "reason": "native persistent state has an ACTIVE finding"}
    return {"green": True, "reason": "clean exact HEAD"}


def should_dispatch_repair(head_sha: str, already_dispatched: Sequence[str]) -> bool:
    """At most one OpenCode repair per review result; no same-head storm."""

    if not head_sha or not head_sha.strip():
        raise LifecycleError("repair dispatch requires a HEAD SHA")
    normalized = head_sha.strip().lower()
    seen = {str(item or "").strip().lower() for item in already_dispatched if str(item or "").strip()}
    return normalized not in seen


def is_conflict_lock_stranded(
    *,
    label_present: bool,
    active_repair_runs: int,
) -> bool:
    """Whether the conflict-repair lock is stranded.

    The label is evidence of an active repair only while a repair run is
    actually still queued or in progress. A label with no active run behind
    it is left over from a terminal run (timeout, cancellation, or a success
    path that never released it). Treating that bare label as "already
    active" waits forever and permanently excludes the PR from automatic
    merge, so stranded must release instead of wait.
    """

    if active_repair_runs < 0:
        raise LifecycleError("active repair run count cannot be negative")
    return bool(label_present) and active_repair_runs == 0


def conflict_repair_action(
    *,
    label_present: bool,
    active_repair_runs: int,
    attempts_for_head: int,
) -> Dict[str, Any]:
    """Bounded conflict-repair decision for one reconciliation pass.

    Exactly one automatic attempt runs per PR HEAD. A repeat conflict on a
    HEAD that already consumed its attempt is held (never re-dispatched for
    the same HEAD); a stranded label with no live run is released so the
    single bounded attempt for the current HEAD can proceed. A changed HEAD
    starts a new episode.
    """

    if attempts_for_head < 0:
        raise LifecycleError("conflict-repair attempt count cannot be negative")
    if active_repair_runs < 0:
        raise LifecycleError("active repair run count cannot be negative")
    if active_repair_runs > 0:
        return {
            "action": "wait",
            "release_lock": False,
            "reason": "conflict repair already active",
        }
    if attempts_for_head >= CONFLICT_REPAIR_ATTEMPTS_PER_HEAD:
        return {
            "action": "hold",
            "release_lock": True,
            "reason": "a conflict-repair attempt already ran for this HEAD",
        }
    if label_present:
        return {
            "action": "release-and-dispatch",
            "release_lock": True,
            "reason": "stranded conflict-repair lock released",
        }
    return {
        "action": "dispatch",
        "release_lock": False,
        "reason": "no active repair and no prior attempt for this HEAD",
    }


def retry_allowed(attempt: object, limit: int = RETRY_MAX_ATTEMPTS) -> bool:
    """Whether another bounded retry attempt may run."""

    try:
        attempt_number = int(str(attempt).strip())
    except (TypeError, ValueError):
        raise LifecycleError("retry attempt must be a non-negative integer")
    if attempt_number < 0:
        raise LifecycleError("retry attempt must be a non-negative integer")
    try:
        limit_number = int(str(limit).strip())
    except (TypeError, ValueError):
        raise LifecycleError("retry limit must be a non-negative integer")
    if limit_number < 0:
        raise LifecycleError("retry limit must be a non-negative integer")
    # attempt_number is the zero-based index of the current execution.
    # With limit=3, only executions 0 and 1 may schedule a successor; 2 is
    # the third and final execution.
    return (attempt_number + 1) < limit_number


def retry_backoff_seconds(
    attempt: object, base: int = RETRY_BASE_DELAY_SECONDS
) -> int:
    """Exponential backoff before a bounded retry (15s, 30s, 60s)."""

    try:
        attempt_number = int(str(attempt).strip())
    except (TypeError, ValueError):
        raise LifecycleError("retry attempt must be a non-negative integer")
    if attempt_number < 0:
        raise LifecycleError("retry attempt must be a non-negative integer")
    try:
        base_number = int(str(base).strip())
    except (TypeError, ValueError):
        raise LifecycleError("retry base must be a non-negative integer")
    if base_number < 0:
        raise LifecycleError("retry base must be a non-negative integer")
    if attempt_number > 10:
        raise LifecycleError("retry attempt out of bounded retry range")
    return base_number * (1 << attempt_number)


def resolve_dispatch_ref(default_branch: object, fallback: object = "main") -> str:
    """Dispatch ref for retries: the repository default branch, never hardcoded.

    An empty default still falls back to the given fallback (then main), so a
    retry never targets a ref that does not exist on the repository.
    """

    name = str(default_branch or "").strip()
    if not name:
        name = str(fallback or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name or ""):
        return "main"
    if ".." in name or "//" in name or "@{" in name:
        return "main"
    if name.endswith(("/", ".", ".lock")):
        return "main"
    if name in ("HEAD", "@"):
        return "main"
    if name.startswith(("refs/", "-", ".")):
        return "main"
    if any(part.startswith(".") or part.endswith(".lock") for part in name.split("/")):
        return "main"
    return name


def needs_fresh_review(old_head_sha: str, new_head_sha: str) -> bool:
    """Any HEAD change requires fresh CI plus a complete review pass.

    Main synchronization included: no prior result is carried forward as
    approval for a new HEAD.
    """

    if not old_head_sha or not new_head_sha:
        return True
    return old_head_sha.strip().lower() != new_head_sha.strip().lower()


def required_toml() -> Dict[str, Any]:
    """Minimum upstream configuration this stack requires."""

    return {
        "pr_reviewer": {
            "persistent_comment": True,
            "persistent_finding_state": True,
            "inline_key_issues": True,
            "enable_large_pr_chunking": True,
            "max_number_of_calls": 3,
            "require_tests_review": True,
            "require_security_review": True,
            "require_risk_assessment": True,
            "require_merge_recommendation": True,
            "require_ticket_analysis_review": True,
            "enable_review_coverage_footer": True,
            "num_max_findings": NUM_MAX_FINDINGS,
        },
        "config": {
            "persistent_inline_comments": True,
            "propagate_tool_errors": True,
        },
        "pr_code_suggestions": {
            "focus_only_on_problems": True,
            "persistent_comment": True,
            "publish_output_no_suggestions": True,
            "suggestions_score_threshold": SUGGESTIONS_SCORE_THRESHOLD,
            "max_suggestions_per_file": 0,
            "max_number_of_calls": 3,
            "enable_suggestions_coverage_footer": True,
        },
        "push_outputs": {
            "enable": True,
            "channels": ["file"],
            "file_path": PUSH_OUTPUTS_FILE_PATH,
        },
        "github": {
            "publish_as_check_run": False,
        },
        "github_action_config": {
            "enable_output": True,
            "fail_on_tool_errors": True,
        },
    }


__all__ = [
    "PR_AGENT_VERSION",
    "UPSTREAM_FINDING_STATE_VERSION",
    "UPSTREAM_ACTION_REF",
    "REVIEW_OUTPUT_REF",
    "PUSH_OUTPUTS_FILE_PATH",
    "NUM_MAX_FINDINGS",
    "SUGGESTIONS_SCORE_THRESHOLD",
    "REVIEW_MERGE_SAFE",
    "REVIEW_MERGE_CAUTION",
    "REVIEW_MERGE_CHANGES",
    "STATE_ACTIVE",
    "STATE_RESOLVED",
    "CONFLICT_LOCK_LABEL",
    "CONFLICT_REPAIR_TIMEOUT_MINUTES",
    "CONFLICT_REPAIR_ATTEMPTS_PER_HEAD",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_BASE_DELAY_SECONDS",
    "PR_AGENT_WORKFLOWS",
    "CODERABBIT_WORKFLOWS",
    "GENERAL_MERGE_WORKFLOWS",
    "LifecycleError",
    "GateInputs",
    "RepairBatch",
    "resolve_consumer_mode",
    "workflows_for_stack",
    "is_same_head",
    "admission_allowed",
    "parse_review_json",
    "current_key_issues",
    "merge_recommendation",
    "is_potentially_truncated",
    "parse_improve_push_outputs",
    "qualifying_suggestions",
    "build_repair_batch",
    "upstream_state_has_active",
    "review_has_ticket_compliance",
    "ticket_compliance_ok",
    "evaluate_gate",
    "should_dispatch_repair",
    "is_conflict_lock_stranded",
    "conflict_repair_action",
    "retry_allowed",
    "retry_backoff_seconds",
    "resolve_dispatch_ref",
    "needs_fresh_review",
    "required_toml",
]
