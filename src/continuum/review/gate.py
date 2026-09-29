"""The normalized review gate.

One implementation, any provider. The gate reads a `ProviderSnapshot`, derives
findings and coverage, folds them into tracker state, decides one normalized
verdict, and publishes it as a commit status plus a real GitHub review. It
never inspects provider-specific text outside the adapter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..config import PROVIDER_NONE, ContinuumConfig, PrAgentSettings
from . import coverage as coverage_module
from . import providers as registry
from . import result as result_module
from .findings import (
    extract_findings,
    now_iso,
    parse_time,
    sanitize_title,
    stable_id,
)
from .findings import current_review_marker, select_current_summary_comments
from .github import GitHubError
from .pr_agent import linked_issue_numbers
from .snapshot import ProviderSnapshot
from .tracker import (
    BOT_PREFIX,
    TRACKER_MARKER,
    VERDICT_MARKER,
    load_tracker,
    merge_findings,
    render_tracker,
)

# GitHub review events. The API accepts REQUEST_CHANGES (the resulting review
# state is CHANGES_REQUESTED), so the mapping is explicit.
EVENT_FOR_VERDICT = {
    result_module.VERDICT_APPROVE: "APPROVE",
    result_module.VERDICT_REQUEST_CHANGES: "REQUEST_CHANGES",
    result_module.VERDICT_COMMENT: "COMMENT",
    result_module.VERDICT_NONE: None,
}


class GateError(RuntimeError):
    """Raised when the gate cannot reach a trustworthy conclusion."""


def disabled_result(
    config: ContinuumConfig,
    pr_number: int,
    head: str = "",
    repository: str = "",
) -> Dict[str, Any]:
    """Normalized no-op result for `review.provider: none`.

    No provider is contacted and nothing is written to GitHub: a deliberate
    opt-out must produce zero external review traffic.
    """

    decision = result_module.decide(True, 0, True)
    return result_module.build_result(
        provider=PROVIDER_NONE,
        repository=repository,
        pr_number=pr_number,
        head=head,
        decision=decision,
        generated_at=now_iso(),
    )


def _forced_findings(snapshot: ProviderSnapshot) -> List[Dict[str, Any]]:
    """Findings implied by a provider verdict that reports no inline finding."""

    findings: List[Dict[str, Any]] = []
    for raw_title in snapshot.forced_open_titles or []:
        title = sanitize_title(raw_title)
        if len(title) < 10:
            continue
        findings.append(
            {
                "id": stable_id("PR", 0, title),
                "file": "",
                "line": 0,
                "title": title,
                "source": "verdict",
                "source_created": None,
            }
        )
    return findings


def _merge_current(current: List[Dict[str, Any]], forced: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    known = {finding["id"] for finding in current}
    merged = list(current) + [finding for finding in forced if finding["id"] not in known]
    merged.sort(key=lambda finding: finding["id"])
    return merged


def _evaluate(
    *,
    provider: str,
    snapshot: ProviderSnapshot,
    pr_number: int,
    head: str,
    files: Sequence[Dict[str, Any]],
    pr_body: str,
    state: Optional[Dict[str, Any]],
    marker: Optional[str],
) -> Dict[str, Any]:
    """Derive findings, coverage, and one normalized verdict. No writes."""

    tracker_state = state or {}
    since: Optional[datetime] = (
        parse_time(tracker_state["last_review_at"]) if tracker_state.get("last_review_at") else None
    )
    selected = select_current_summary_comments(snapshot.summary_comments, head, since)

    current = _merge_current(
        extract_findings(
            issue_comments=selected,
            review_comments=snapshot.review_comments,
            summary_comments=selected,
            summary_parser=snapshot.summary_parser,
            current_head=head,
            since=since,
            unresolved_ids=snapshot.unresolved_ids,
            review_bots=snapshot.review_bots,
            is_boilerplate=snapshot.is_boilerplate,
        ),
        _forced_findings(snapshot),
    )

    coverage_reason = snapshot.extra_reason or coverage_module.evaluate_coverage(
        [comment.get("body") or "" for comment in selected],
        files,
        current_head=head,
        provider_output=bool(selected),
    )

    merged, new_marker = merge_findings(tracker_state, current, head, last_review_at=marker)
    open_findings = [
        finding for finding in merged if finding.get("status") in result_module.OPEN_STATUSES
    ]
    decision = result_module.decide(
        False, len(open_findings), coverage_reason is None, coverage_reason
    )

    return result_module.build_result(
        provider=provider,
        repository="",
        pr_number=pr_number,
        head=head,
        decision=decision,
        findings=merged,
        coverage_reason=coverage_reason,
        linked_issues=linked_issue_numbers(pr_body),
        generated_at=now_iso(),
        metadata={
            "thread_state_known": snapshot.unresolved_ids is not None,
            "last_review_at": new_marker,
            "selected_output": len(selected),
            **snapshot.describe(),
        },
    )


def evaluate(
    *,
    config: ContinuumConfig,
    snapshot: ProviderSnapshot,
    pr_number: int,
    head: str,
    files: Sequence[Dict[str, Any]],
    pr_body: str = "",
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate a snapshot without contacting GitHub. Used by the tests."""

    if not config.review.enabled:
        return disabled_result(config, pr_number, head)
    state = previous_state or {}
    return _evaluate(
        provider=config.review.provider,
        snapshot=snapshot,
        pr_number=pr_number,
        head=head,
        files=files,
        pr_body=pr_body,
        state=state,
        marker=None,
    )


def run_gate(
    client: Any,
    config: ContinuumConfig,
    pr_number: int,
    *,
    head_sha: Optional[str] = None,
    apply: bool = True,
) -> Dict[str, Any]:
    """Evaluate the gate for a pull request and publish the normalized result.

    With `apply=False` nothing is written to GitHub, which is what the
    deterministic tests use.
    """

    if not config.review.enabled:
        return disabled_result(config, pr_number, head_sha or "", repository=client.repository)

    provider_name = config.review.provider
    settings = config.review.provider_settings()
    if settings is None:
        raise GateError(f"Review provider {provider_name!r} has no configuration")

    pull = client.get_pull(int(pr_number))
    head = (head_sha or ((pull.get("head") or {}).get("sha")) or "").strip()
    if not head:
        raise GateError(f"Could not determine the current HEAD for pull request #{pr_number}")

    files = client.list_pull_files(int(pr_number))
    issue_comments = client.list_issue_comments(int(pr_number))
    _existing, state = load_tracker(issue_comments, trusted_logins=_trusted_logins(config))

    snapshot = registry.collect_snapshot(
        provider_name, client, pr_number, head, settings, apply=apply
    )

    since = parse_time(state["last_review_at"]) if state.get("last_review_at") else None
    marker, _selected = current_review_marker(
        snapshot.summary_comments, head, since, state.get("last_review_at")
    )

    result = _evaluate(
        provider=provider_name,
        snapshot=snapshot,
        pr_number=pr_number,
        head=head,
        files=files,
        pr_body=pull.get("body") or "",
        state=state,
        marker=marker,
    )
    result["repository"] = getattr(client, "repository", "")
    result["metadata"]["last_review_at"] = marker

    if apply:
        publish(client, config, pr_number, result, state=state, marker=marker)
    return result


def _trusted_logins(config: ContinuumConfig) -> Sequence[str]:
    """Identities allowed to author a tracker comment."""

    settings = config.review.provider_settings()
    logins = ["github-actions[bot]", "github-actions"]
    if isinstance(settings, PrAgentSettings):
        logins.append(settings.bot_login)
    return tuple(dict.fromkeys(logins))


def publish(
    client: Any,
    config: ContinuumConfig,
    pr_number: int,
    result: Dict[str, Any],
    *,
    state: Optional[Dict[str, Any]] = None,
    marker: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the tracker, the GitHub review verdict, and the commit status."""

    provider = str(result.get("provider") or "none")
    settings = config.review.provider_settings()
    bot_login = getattr(settings, "bot_login", "github-actions[bot]")
    pr_number = int(result.get("pr", pr_number))
    head = str(result.get("head") or "")

    if state is not None and marker is not None:
        body = render_tracker(head, result.get("findings", []), marker, BOT_PREFIX, provider)
        client.upsert_marker_comment(pr_number, TRACKER_MARKER, body, (bot_login,))

    event = EVENT_FOR_VERDICT.get(str(result.get("verdict")))
    submitted: Optional[str] = None
    if event is not None:
        review_body = f"{VERDICT_MARKER}\n{result_module.render_summary(result, BOT_PREFIX)}"
        try:
            client.create_review(pr_number, head, review_body, event)
            submitted = event
        except GitHubError as exc:
            # Approval is not always available to the token in use. Record the
            # same verdict as a review comment and keep the gate closed: a
            # rejected APPROVE must never turn into a mergeable state.
            if event == "APPROVE" and exc.status == 422:
                client.create_review(pr_number, head, review_body, "COMMENT")
                submitted = "COMMENT"
                result["verdict"] = result_module.VERDICT_COMMENT
                result["state"] = result_module.STATE_BLOCKED
                result["gate_passed"] = False
                result["blocking"] = True
                result["reason"] = (
                    "Approval is not available to the review token, so the review gate "
                    "stays closed for this HEAD."
                )
            else:
                raise

    status_state = "success" if result.get("gate_passed") else "failure"
    description = result_module.status_description(result)
    client.create_status(head, status_state, description, config.review.status_context)

    result["metadata"] = {
        **(result.get("metadata") or {}),
        "submitted_event": submitted,
        "status_state": status_state,
        "status_description": description,
    }
    return result
