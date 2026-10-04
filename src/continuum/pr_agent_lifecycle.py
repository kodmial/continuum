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

SUGGESTIONS_SCORE_THRESHOLD = 7

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
    # Merge split envelopes fail-closed so outer and nested findings are
    # both preserved (see _unwrap_review).
    review = _unwrap_review(payload)
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


# Explicit security-concern fields that block the automatic-improve skip.
# The upstream merge recommendation already aggregates security posture, but
# an explicit non-empty concern list must never be skipped over: a clean
# skip requires both `safe_to_merge` and no listed security concern.
BLOCKING_SECURITY_SIGNAL_KEYS = (
    "security_concerns",
    "security_issues",
    "security_vulnerabilities",
    "critical_security_issues",
)

# Upstream clean reviews commonly report security fields as negation prose
# ("No", "None", "N/A", "No security concerns found"). Such prose must not
# block the clean-PR fast path; anything else fails closed and blocks.
_CLEAN_SECURITY_TEXTS = frozenset(
    {
        "no",
        "none",
        "n/a",
        "na",
        "nil",
        "null",
        "nope",
        "0",
        "false",
        "ok",
        "clear",
        "clean",
        "pass",
        "passed",
        "not applicable",
        "none found",
        "no findings",
        "no finding",
        "no issues",
        "no issue",
        "no errors",
        "no error",
        "no tool errors",
        "no tool error",
        "no concerns",
        "no concern",
        "no risks",
        "no risk",
        "no vulnerabilities",
        "no vulnerability",
        "no problems",
        "no problem",
        "no threats",
        "no threat",
        "no security concerns",
        "no security issues",
        "no security vulnerabilities",
        "no critical issues",
        "no critical security issues",
    }
)

_CLEAN_SECURITY_SUFFIXES = (" found", " detected", " identified", " observed")


def _is_clean_security_text(value: str) -> bool:
    """Whether a security-field string is negation/empty prose, not a concern."""

    norm = re.sub(r"[^a-z0-9/ ]+", " ", value.strip().lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return True
    if norm in _CLEAN_SECURITY_TEXTS:
        return True
    for suffix in _CLEAN_SECURITY_SUFFIXES:
        if norm.endswith(suffix) and norm[: -len(suffix)].strip() in _CLEAN_SECURITY_TEXTS:
            return True
    return False


def _security_value_is_blocking(value: object) -> bool:
    """Whether a security-field value carries a real concern (fail closed)."""

    if value is None:
        return False
    if isinstance(value, str):
        return not _is_clean_security_text(value)
    if isinstance(value, Mapping):
        if len(value) == 0:
            return False
        return any(_security_value_is_blocking(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return False
        return any(_security_value_is_blocking(item) for item in value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


# Review-payload keys that carry an explicit tool-error signal when
# `config.propagate_tool_errors` surfaces a failed tool. Any non-clean
# entry fails closed; absent keys mean no signal.
_TOOL_ERROR_SIGNAL_KEYS = (
    "tool_errors",
    "tool_error",
    "tool_failures",
    "failed_tools",
    "errors",
    "error",
)

# Review-payload keys that carry an explicit coverage signal. Absent keys
# mean no signal (coverage is assumed complete here; the findings-cap
# truncation check still applies separately below).
_COVERAGE_FLAG_KEYS = (
    "review_coverage_complete",
    "coverage_complete",
    "coverage_completed",
    "is_complete",
    "is_completed",
    "complete",
    "truncated",
    "partial",
    "incomplete",
)

_COVERAGE_OBJECT_KEYS = (
    "coverage",
    "review_coverage",
    "chunk_coverage",
    "review_coverage_footer",
    "coverage_footer",
)


def has_tool_error_signal(review: Mapping[str, Any]) -> bool:
    """Whether the review payload itself reports a tool error (fail closed)."""

    inner = _unwrap_review(review)
    for key in _TOOL_ERROR_SIGNAL_KEYS:
        if key not in inner:
            continue
        if _security_value_is_blocking(inner.get(key)):
            return True
    return False


def _coverage_flag_value_is_incomplete(key: str, value: object) -> bool:
    """Whether one coverage flag entry reports incomplete coverage (fail closed)."""

    if key in ("truncated", "partial", "incomplete"):
        if value is True:
            return True
        if isinstance(value, bool):
            return False
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes"):
            return True
        if isinstance(value, (int, float)) and value != 0:
            return True
        return False
    if value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no"):
        return True
    if isinstance(value, (int, float)):
        if value == 0:
            return True
        if isinstance(value, float) and value != value:  # NaN: fail closed
            return True
    return False


def has_incomplete_coverage_signal(review: Mapping[str, Any]) -> bool:
    """Whether the review payload itself reports incomplete coverage."""

    inner = _unwrap_review(review)
    for key in _COVERAGE_FLAG_KEYS:
        if key not in inner:
            continue
        value = inner.get(key)
        if _coverage_flag_value_is_incomplete(key, value):
            return True
    for key in _COVERAGE_OBJECT_KEYS:
        if key not in inner:
            continue
        value = inner.get(key)
        if isinstance(value, Mapping):
            has_reviewed_key = "reviewed" in value or "reviewed_chunks" in value
            has_total_key = "total" in value or "total_chunks" in value
            reviewed = value.get("reviewed", value.get("reviewed_chunks"))
            total = value.get("total", value.get("total_chunks"))
            try:
                reviewed_num: float | None = (
                    float(reviewed) if reviewed is not None else None
                )
            except (TypeError, ValueError):
                reviewed_num = None
            try:
                total_num: float | None = float(total) if total is not None else None
            except (TypeError, ValueError):
                total_num = None
            has_count_signal = has_reviewed_key or has_total_key
            has_flag_signal = any(flag_key in value for flag_key in _COVERAGE_FLAG_KEYS)
            if has_count_signal:
                # Fail closed on unparseable counts: a present-but-unrecognized
                # count never reads as complete coverage.
                if reviewed_num is None or total_num is None:
                    return True
                if (
                    reviewed_num != reviewed_num
                    or total_num != total_num
                    or reviewed_num in (float("inf"), float("-inf"))
                    or total_num in (float("inf"), float("-inf"))
                ):
                    return True
                if total_num <= 0 or reviewed_num < total_num:
                    return True
            # Flags are independent of counts: a full count never masks an
            # explicit incomplete flag (fail closed).
            for flag_key in _COVERAGE_FLAG_KEYS:
                if flag_key in value and _coverage_flag_value_is_incomplete(
                    flag_key, value.get(flag_key)
                ):
                    return True
            # A coverage object with no recognized counts or flags (e.g. {}
            # or only unknown fields) is not evidence of complete coverage:
            # fail closed so unknown coverage never permits an improve skip.
            if not has_count_signal and not has_flag_signal:
                return True
            continue
        if isinstance(value, str):
            lowered = value.strip().lower()
            if not lowered:
                continue
            if any(
                word in lowered for word in ("partial", "incomplete", "truncated")
            ) and "complete" not in lowered.replace("incomplete", ""):
                return True
    return False


def _coverage_single_value_is_incomplete(key: str, value: object) -> bool:
    """Whether one coverage key/value pair alone reports incomplete coverage."""

    if key in _COVERAGE_FLAG_KEYS:
        return _coverage_flag_value_is_incomplete(key, value)
    if key in _COVERAGE_OBJECT_KEYS:
        if isinstance(value, Mapping):
            has_reviewed_key = "reviewed" in value or "reviewed_chunks" in value
            has_total_key = "total" in value or "total_chunks" in value
            reviewed = value.get("reviewed", value.get("reviewed_chunks"))
            total = value.get("total", value.get("total_chunks"))
            try:
                reviewed_num = float(reviewed) if reviewed is not None else None
            except (TypeError, ValueError):
                reviewed_num = None
            try:
                total_num = float(total) if total is not None else None
            except (TypeError, ValueError):
                total_num = None
            has_count_signal = has_reviewed_key or has_total_key
            has_flag_signal = any(flag_key in value for flag_key in _COVERAGE_FLAG_KEYS)
            if has_count_signal:
                if reviewed_num is None or total_num is None:
                    return True
                if (
                    reviewed_num != reviewed_num
                    or total_num != total_num
                    or reviewed_num in (float("inf"), float("-inf"))
                    or total_num in (float("inf"), float("-inf"))
                ):
                    return True
                if total_num <= 0 or reviewed_num < total_num:
                    return True
            for flag_key in _COVERAGE_FLAG_KEYS:
                if flag_key in value and _coverage_flag_value_is_incomplete(
                    flag_key, value.get(flag_key)
                ):
                    return True
            if not has_count_signal and not has_flag_signal:
                return True
            return False
        if isinstance(value, str):
            lowered = value.strip().lower()
            if not lowered:
                return False
            return any(
                word in lowered for word in ("partial", "incomplete", "truncated")
            ) and "complete" not in lowered.replace("incomplete", "")
        return False
    return False


def _unwrap_review(review: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept the canonical review payload in either envelope shape.

    The GitHub Action output may be the flat PRReview object or
    `{"review": <PRReview>}`; both are canonical machine sources.
    Split envelopes carry signals on both sides, so both sides are
    merged fail-closed: returning only the outer object would discard
    nested findings (breaking the clean fast path) or miss nested
    blocking security (failing open).
    """

    if not isinstance(review, Mapping):
        raise LifecycleError("PR-Agent review JSON must be an object")
    inner = review.get("review", None)
    if not isinstance(inner, dict):
        return review
    outer_has_signal = (
        "key_issues_to_review" in review
        or "merge_recommendation" in review
        or any(key in review for key in BLOCKING_SECURITY_SIGNAL_KEYS)
        or any(key in review for key in _TOOL_ERROR_SIGNAL_KEYS)
        or any(key in review for key in _COVERAGE_FLAG_KEYS)
        or any(key in review for key in _COVERAGE_OBJECT_KEYS)
    )
    if not outer_has_signal:
        return inner
    merged: Dict[str, Any] = {
        key: value for key, value in inner.items() if key != "review"
    }
    for key, value in review.items():
        if key == "review":
            continue
        if key not in merged:
            merged[key] = value
            continue
        current = merged[key]
        if key == "key_issues_to_review":
            current_is_list = isinstance(current, list)
            outer_is_list = isinstance(value, list)
            if current_is_list and outer_is_list:
                merged[key] = [*value, *current]
            elif not current_is_list:
                # Keep the non-list so callers fail closed on invalid
                # shape instead of silently reading the clean side.
                pass
            else:
                # Outer is non-list while nested is a list: surface the
                # invalid shape so validation throws (fail closed).
                merged[key] = value
            continue
        if key == "merge_recommendation":
            current_text = str(current or "").strip()
            outer_text = str(value or "").strip()
            if not current_text:
                merged[key] = value
            elif not outer_text:
                pass
            elif current_text != outer_text:
                # Most restrictive wins: any non-safe blocks.
                if current_text == REVIEW_MERGE_SAFE and outer_text != REVIEW_MERGE_SAFE:
                    merged[key] = value
            continue
        if key in BLOCKING_SECURITY_SIGNAL_KEYS or key in _TOOL_ERROR_SIGNAL_KEYS:
            # Either side blocking must block the merged view.
            if not _security_value_is_blocking(current) and _security_value_is_blocking(value):
                merged[key] = value
            continue
        if key in _COVERAGE_FLAG_KEYS or key in _COVERAGE_OBJECT_KEYS:
            # Either side incomplete must read as incomplete.
            current_incomplete = _coverage_single_value_is_incomplete(key, current)
            outer_incomplete = _coverage_single_value_is_incomplete(key, value)
            if outer_incomplete and not current_incomplete:
                merged[key] = value
            elif not current_incomplete and not outer_incomplete:
                if isinstance(current, Mapping) and isinstance(value, Mapping):
                    combined = {**current, **value}
                    merged[key] = combined
                else:
                    merged[key] = value
            # Else keep the incomplete current value (fail closed).
            continue
        merged[key] = value
    return merged


def has_blocking_security_signal(review: Mapping[str, Any]) -> bool:
    """Whether the review lists an explicit blocking security concern.

    Negation/empty prose ("No", "None", "N/A",
    "No security concerns found") is normalized to clean and does not
    block; any other non-empty signal fails closed and blocks.
    """

    inner = _unwrap_review(review)
    for key in BLOCKING_SECURITY_SIGNAL_KEYS:
        value = inner.get(key, None)
        if value is None:
            continue
        if _security_value_is_blocking(value):
            return True
    return False


def should_skip_improve(
    review: Mapping[str, Any],
    persistent_state: object,
    *,
    head_matches: bool,
    tool_error: bool = False,
    review_coverage_complete: bool = True,
    reviewed_head_sha: object = None,
) -> Dict[str, Any]:
    """Decide whether the automatic `improve` pass can be skipped.

    `review` remains the authoritative merge gate. A skip is allowed only
    when the review is provably clean for the exact HEAD: no tool error
    (caller flag or an explicit tool-error signal in the review payload),
    complete review coverage (caller flag and no incomplete-coverage
    signal in the review payload), matching HEAD, `safe_to_merge`, zero
    current key issues (a batch at the findings cap is never clean), no
    explicit blocking security signal, and native persistent state that
    is a complete full review for the exact reviewed HEAD with no ACTIVE
    finding.

    `reviewed_head_sha` is mandatory for any `skip=True`: it is compared
    against `persistent_state.last_run.head_sha` and a missing value raises
    `LifecycleError` (fail closed) so a stale persistent state from a prior
    HEAD can never authorize a skip. Callers must pass the exact reviewed
    HEAD; a missing `last_run.head_sha` always raises `LifecycleError`.

    Anything else returns `skip=False` so `improve` may still run to
    generate additional repair suggestions. Invalid review payloads and a
    missing reviewed HEAD raise `LifecycleError` and fail closed instead
    of skipping; a benign persistent format variation (non-list findings,
    missing `last_run`, non-full run) with a known HEAD returns
    `skip=False` so `improve` still runs.
    """

    inner = _unwrap_review(review)
    if tool_error or has_tool_error_signal(inner):
        return {"skip": False, "reason": "tool error: failing closed"}
    if (not review_coverage_complete) or has_incomplete_coverage_signal(inner):
        return {"skip": False, "reason": "incomplete review coverage: failing closed"}
    if not head_matches:
        return {"skip": False, "reason": "stale head: result is not for the current HEAD"}
    if "key_issues_to_review" not in inner:
        raise LifecycleError("PR-Agent review JSON has no key_issues_to_review")
    try:
        recommendation = merge_recommendation(inner)
    except LifecycleError as exc:
        raise LifecycleError(f"cannot decide improve skip: {exc}") from None
    if recommendation != REVIEW_MERGE_SAFE:
        return {"skip": False, "reason": f"merge recommendation blocks: {recommendation}"}
    issues = current_key_issues(inner)
    if issues:
        if is_potentially_truncated(inner):
            return {
                "skip": False,
                "reason": "review batch reached the findings cap: potentially truncated",
            }
        return {"skip": False, "reason": f"{len(issues)} current key issue(s) remain"}
    if has_blocking_security_signal(inner):
        return {"skip": False, "reason": "blocking security signal remains"}
    if not isinstance(persistent_state, dict):
        raise LifecycleError("upstream finding state must be the v0.46.0 state object")
    # The exact reviewed HEAD is mandatory for any skip decision: validate
    # it before interpreting persistent format variations so a missing HEAD
    # still fails closed by exception while a benign upstream format
    # variation with a known HEAD safely runs improve (skip=False).
    expected_head = str(reviewed_head_sha or "").strip()
    if not expected_head:
        raise LifecycleError(
            "cannot decide improve skip without the exact reviewed HEAD."
        )
    raw_findings = persistent_state.get("findings")
    if not isinstance(raw_findings, list):
        # A benign upstream format variation must still run improve for
        # repair value instead of crashing the orchestrator: fail closed
        # to skip=False, never to an exception.
        return {"skip": False, "reason": "persistent state findings is not a list: failing closed"}
    last_run = persistent_state.get("last_run")
    if not isinstance(last_run, dict):
        return {"skip": False, "reason": "persistent state has no last_run: failing closed"}
    if last_run.get("complete") is not True or str(last_run.get("kind") or "") != "full":
        return {"skip": False, "reason": "persistent state is not from a complete full review: failing closed"}
    state_head = str(last_run.get("head_sha") or "").strip()
    if not state_head:
        raise LifecycleError("upstream PR-Agent persistent state has no last_run.head_sha.")
    if not is_same_head(state_head, expected_head):
        return {"skip": False, "reason": "stale persistent state: not for the reviewed HEAD"}
    try:
        has_active = upstream_state_has_active(persistent_state)
    except Exception:
        return {"skip": False, "reason": "persistent state has an unrecognized finding state: failing closed"}
    if has_active:
        return {"skip": False, "reason": "native persistent state has an ACTIVE finding"}
    return {
        "skip": True,
        "reason": (
            "clean exact HEAD: safe_to_merge with zero findings and complete "
            "state; automatic improve skipped"
        ),
    }


def is_improve_skipped_clean(improve_skipped_success: object) -> bool:
    """Whether the automatic-improve skip counts as clean coverage.

    Repair/merge gating must derive ``GateInputs.improve_skipped_clean``
    through this helper from the ``improve_skipped`` step outcome instead
    of relying on the ``False`` default: a skipped-clean HEAD carries an
    empty improve payload, so ``improve_coverage_complete`` stays
    ``False`` and only this flag lets :func:`evaluate_gate` green it.
    Accepts the workflow step outcome in either boolean or
    ``'true'``/``'false'`` string form.
    """

    if isinstance(improve_skipped_success, bool):
        return improve_skipped_success
    return str(improve_skipped_success or "").strip().lower() == "true"


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
    """High-signal native improve suggestions eligible for autonomous repair.

    Work Lock #37 raises the autonomous threshold to 7. Missing or malformed
    scores remain visible presentation data but do not trigger automatic code
    changes or block the merge gate.
    """

    out: List[Dict[str, Any]] = []
    for entry in suggestions:
        score = entry.get("score", None)
        if score is None:
            continue
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            continue
        if numeric >= threshold:
            out.append(dict(entry))
    return out


_PATH_KEYS = ("relevant_file", "path", "file", "filename")
_START_KEYS = ("relevant_lines_start", "line_start", "start_line", "line")
_END_KEYS = ("relevant_lines_end", "line_end", "end_line", "line")
_REVIEW_TEXT_KEYS = ("issue_header", "issue_content", "title", "body", "description")
_IMPROVE_TEXT_KEYS = (
    "one_sentence_summary",
    "suggestion_content",
    "title",
    "body",
    "description",
    "label",
)


def _first_string(entry: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_number(entry: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _normalize_path(entry: Mapping[str, Any]) -> str:
    return (
        _first_string(entry, _PATH_KEYS)
        .removeprefix("./")
        .strip("'\"` ")
        .lower()
    )


def _line_range(entry: Mapping[str, Any]) -> tuple[int | None, int | None]:
    start = _first_number(entry, _START_KEYS)
    end = _first_number(entry, _END_KEYS)
    raw = entry.get("relevant_lines")
    if (start is None or end is None) and isinstance(raw, str):
        pair = re.search(r"(\d+)\D+(\d+)", raw)
        if pair:
            start = start if start is not None else int(pair.group(1))
            end = end if end is not None else int(pair.group(2))
        else:
            one = re.search(r"\d+", raw)
            if one:
                start = start if start is not None else int(one.group(0))
                end = end if end is not None else int(one.group(0))
    if start is not None and end is None:
        end = start
    if end is not None and start is None:
        start = end
    if start is not None and end is not None and end < start:
        start, end = end, start
    return start, end


def _normalize_problem(entry: Mapping[str, Any], source: str) -> str:
    keys = _REVIEW_TEXT_KEYS if source == "review" else _IMPROVE_TEXT_KEYS
    text = " ".join(
        str(entry.get(key) or "") for key in keys if isinstance(entry.get(key), str)
    )
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\`*_>#()\[\]{}]", " ", text)
    text = re.sub(r"[^\w./-]+", " ", text.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _equivalent_problem(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 32 and shorter in longer:
        return True
    left_tokens = {token for token in left.split() if len(token) >= 3}
    right_tokens = {token for token in right.split() if len(token) >= 3}
    if not left_tokens or not right_tokens:
        return False
    common = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = common / union if union else 0.0
    containment = common / min(len(left_tokens), len(right_tokens))
    return common >= 5 and (jaccard >= 0.75 or containment >= 0.85)


def _same_logical_defect(
    review_finding: Mapping[str, Any],
    improve_suggestion: Mapping[str, Any],
) -> bool:
    if _normalize_path(review_finding) != _normalize_path(improve_suggestion):
        return False
    if not _normalize_path(review_finding):
        return False
    r_start, r_end = _line_range(review_finding)
    i_start, i_end = _line_range(improve_suggestion)
    if None in (r_start, r_end, i_start, i_end):
        return False
    if not (r_start <= i_end and i_start <= r_end):
        return False
    return _equivalent_problem(
        _normalize_problem(review_finding, "review"),
        _normalize_problem(improve_suggestion, "improve"),
    )


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
    """Batch current review findings plus unique high-signal suggestions.

    Native review findings are authoritative when /review and /improve emit
    the same logical defect at an overlapping location. Distinct findings are
    retained even when they touch the same file.
    """

    if not head_sha or not head_sha.strip():
        raise LifecycleError("repair batch requires the exact reviewed HEAD")
    review_findings = current_key_issues(review)
    items: List[Dict[str, Any]] = [
        {"source": "review", "index": index, "finding": dict(finding)}
        for index, finding in enumerate(review_findings)
    ]
    for index, suggestion in enumerate(qualifying_suggestions(suggestions)):
        if any(_same_logical_defect(finding, suggestion) for finding in review_findings):
            continue
        items.append(
            {"source": "improve", "index": index, "suggestion": dict(suggestion)}
        )
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
    improve_skipped_clean: bool = False


def evaluate_gate(decision: GateInputs) -> Dict[str, Any]:
    """Exact-head, fail-closed PR-Agent merge gate.

    Green requires every native signal at once: no tool error, complete
    review and improve coverage, green CI on the exact HEAD, matching
    HEAD, safe_to_merge, empty current key issues, no qualifying improve
    suggestions, no ACTIVE finding in native persistent state, and no
    potentially truncated batch. Anything else blocks with a reason.

    A clean HEAD whose automatic improve was skipped carries an empty
    improve payload with ``improve_coverage_complete=False``: callers must
    set ``improve_skipped_clean`` via :func:`is_improve_skipped_clean`
    from the ``improve_skipped`` step outcome, otherwise the gate fails
    closed on incomplete improve coverage and negates the skip's latency
    win. A skipped HEAD with remaining qualifying suggestions never
    greens.
    """

    if decision.tool_error:
        return {"green": False, "reason": "tool error: failing closed"}
    if not decision.review_coverage_complete:
        return {"green": False, "reason": "incomplete review coverage: failing closed"}
    if not decision.improve_coverage_complete and not decision.improve_skipped_clean:
        return {"green": False, "reason": "incomplete improve coverage: failing closed"}
    if decision.improve_skipped_clean and decision.qualifying_improve:
        return {
            "green": False,
            "reason": "improve was skipped but qualifying suggestions remain",
        }
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
    attempt: object,
    base: int = RETRY_BASE_DELAY_SECONDS,
    limit: int = RETRY_MAX_ATTEMPTS,
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
    try:
        limit_number = int(str(limit).strip())
    except (TypeError, ValueError):
        raise LifecycleError("retry limit must be a non-negative integer")
    if limit_number < 0:
        raise LifecycleError("retry limit must be a non-negative integer")
    if attempt_number >= limit_number:
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


def caller_review_event_is_actionable(
    event_name: object,
    *,
    is_pull_request_comment: bool = False,
    actor_is_owner: bool = False,
    comment_body: object = "",
) -> bool:
    """Whether a caller event may invoke the reusable PR-Agent operation layer.

    Mirrors the thin-router gate in `continuum-pr-agent-router.yml` (heavy
    callers are dispatch-only): workflow_dispatch (bounded retries/recovery)
    is always actionable, while issue_comment is actionable only for an
    owner `/review` comment on a pull request. Every other event is a no-op
    that must never create a heavy run or hold a per-PR lock.
    """

    name = str(event_name or "").strip()
    if name == "workflow_dispatch":
        return True
    if name != "issue_comment":
        return False
    if not is_pull_request_comment or not actor_is_owner:
        return False
    return "/review" in str(comment_body or "")


def stale_review_may_be_cancelled(running_head_sha: object, current_head_sha: object) -> bool:
    """Whether a running/queued review may be superseded for a newer HEAD.

    A review is stale exactly when the PR HEAD moved: the newer HEAD needs
    fresh CI plus a complete review, so the older run may be superseded
    without losing useful work. Same-HEAD duplicates must coalesce instead
    of cancelling/restarting because exact-HEAD work is idempotent.
    Missing SHAs fail closed to False: never discard work blindly.

    The reusable workflow implements this predicate with exact-HEAD
    admission, before/after review revalidation, and retry-backoff HEAD
    rechecks. Native `cancel-in-progress` is never used for preemption
    because it cannot compare HEADs: an out-of-order old-HEAD event
    starting later must not cancel newer exact-HEAD work.
    """

    running = str(running_head_sha or "").strip().lower()
    current = str(current_head_sha or "").strip().lower()
    if not running or not current:
        return False
    return running != current


def repair_is_protected_from_review_preemption(
    *,
    repair_active: bool = False,
    repair_head_sha: object = "",
    review_head_sha: object = "",
) -> bool:
    """Whether an active repair publication blocks review preemption.

    Repair mutates and publishes the PR branch under exact-HEAD
    revalidation plus force-with-lease. While such a publication is
    active for the HEAD under review, a newer review event must wait
    rather than interrupt it: reviews are serializable, repairs are not
    interruptible. With no active repair there is nothing to protect.
    Unknown HEADs fail closed to protected so a blind preemption can
    never interrupt a publish it cannot identify; a repair for a
    different HEAD does not block the review because that stale repair
    fails closed on its own exact-HEAD check.
    """

    if not repair_active:
        return False
    repair = str(repair_head_sha or "").strip().lower()
    review = str(review_head_sha or "").strip().lower()
    if not repair or not review:
        return True
    return repair == review


def review_supersession_decision(
    running_head_sha: object,
    current_head_sha: object,
    *,
    repair_active: bool = False,
    repair_head_sha: object = "",
    review_head_sha: object = "",
    event_name: object = "workflow_dispatch",
    is_pull_request_comment: bool = False,
    actor_is_owner: bool = False,
    comment_body: object = "",
) -> Dict[str, Any]:
    """Single HEAD-guarded scheduling decision for PR-Agent review work.

    Composition root wiring the three scheduling predicates so none is
    dead code: non-actionable caller events are ignored before any HEAD
    comparison (caller_review_event_is_actionable), an active repair for
    the HEAD under review blocks preemption
    (repair_is_protected_from_review_preemption), and only a moved HEAD
    supersedes older review work (stale_review_may_be_cancelled).
    Same-HEAD duplicates coalesce; a current HEAD with no running review
    proceeds; missing SHAs fail closed to wait rather than discard
    blindly. The reusable workflow implements this decision with
    workflow-level serialization (no native preemption) plus exact-HEAD
    admission, before/after revalidation, and retry-backoff HEAD rechecks:
    `proceed` enters the admission path, `supersede` means the stale
    run's results are discarded by those exact-HEAD checks and the newer
    HEAD is reviewed fresh once the lock frees (a stale running review is
    never interrupted mid-flight), `coalesce` skips duplicate same-HEAD
    work, and `wait` holds while a same-HEAD repair publishes or a HEAD
    is still unknown.
    """

    if not caller_review_event_is_actionable(
        event_name,
        is_pull_request_comment=is_pull_request_comment,
        actor_is_owner=actor_is_owner,
        comment_body=comment_body,
    ):
        return {"action": "ignore", "reason": "non-actionable caller event"}
    review_head = str(review_head_sha or current_head_sha or "").strip().lower()
    if repair_is_protected_from_review_preemption(
        repair_active=repair_active,
        repair_head_sha=repair_head_sha,
        review_head_sha=review_head,
    ):
        return {"action": "wait", "reason": "repair publication in flight"}
    if stale_review_may_be_cancelled(running_head_sha, current_head_sha):
        return {"action": "supersede", "reason": "HEAD moved: fresh review required"}
    running = str(running_head_sha or "").strip().lower()
    current = str(current_head_sha or "").strip().lower()
    if running and current and running == current:
        return {"action": "coalesce", "reason": "same HEAD: exact-HEAD work is idempotent"}
    if current and not running:
        return {"action": "proceed", "reason": "no active review: admit fresh HEAD"}
    return {"action": "wait", "reason": "missing HEAD: fail closed"}


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
    "BLOCKING_SECURITY_SIGNAL_KEYS",
    "has_blocking_security_signal",
    "has_tool_error_signal",
    "has_incomplete_coverage_signal",
    "should_skip_improve",
    "is_improve_skipped_clean",
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
    "caller_review_event_is_actionable",
    "stale_review_may_be_cancelled",
    "repair_is_protected_from_review_preemption",
    "review_supersession_decision",
    "required_toml",
]
