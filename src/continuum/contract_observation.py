"""Observe real lifecycle state for contract qualification (kodmial/continuum#286).

The qualification workflow must never publish success from constructed
identifiers alone. This module turns GitHub API observations (issues, pull
requests, workflow runs and comments) into the pure inputs consumed by
:mod:`continuum.contract_qualification`, so Layer 2 can inspect reservations
that actually exist and Layer 3 can read the live canary task's real stage.

The helpers are deliberately free of network access: the workflow fetches
JSON with ``gh`` and passes it here, which keeps every rule deterministic and
unit-testable.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .contract_qualification import (
    IN_PROGRESS_LABEL,
    CanaryState,
    ContractQualificationError,
    Reservation,
)

#: Workflow-run states that still hold an attributable reservation.
ACTIVE_RUN_STATES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)

#: Workflow-run states that are terminal.
TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "skipped"}
)

#: Default reservation lease (seconds) before a running worker is stale.
DEFAULT_LEASE_SECONDS = 45 * 60

#: Comment marker the canary workflow writes when it requests recovery.
CANARY_RECOVERY_MARKER = "<!-- continuum-canary-recovery"

_ISSUE_REF_RE = re.compile(r"(?<![0-9A-Za-z_])#(\d+)(?![0-9])")
_BRANCH_ISSUE_RE = re.compile(r"(?:^|/)issue(\d+)(?:-|$)")


@dataclass(frozen=True)
class CanaryObservation:
    """Observed canary state plus whether the task already completed."""

    state: Optional[CanaryState]
    completed: bool
    failure: str


def _timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)


def _age_seconds(value: object, now: Optional[datetime]) -> Optional[int]:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    return max(0, int((_now(now) - parsed).total_seconds()))


def _labels(issue: Mapping[str, Any]) -> set:
    labels = issue.get("labels") or []
    names = set()
    for label in labels:
        if isinstance(label, Mapping):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            names.add(name)
    return names


def _issue_number(issue: Mapping[str, Any]) -> int:
    try:
        number = int(issue.get("number"))
    except (TypeError, ValueError):
        raise ContractQualificationError("observed issue has no numeric number")
    if number < 1:
        raise ContractQualificationError("observed issue number must be positive")
    return number


def _run_id(run: Mapping[str, Any]) -> Optional[int]:
    for key in ("databaseId", "id"):
        try:
            value = int(run.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _run_started_at(run: Mapping[str, Any]) -> object:
    return (
        run.get("run_started_at")
        or run.get("runStartedAt")
        or run.get("created_at")
        or run.get("createdAt")
    )


def _references_issue(value: object, issue_number: int) -> bool:
    text = str(value or "")
    if not text:
        return False
    if any(int(match) == issue_number for match in _ISSUE_REF_RE.findall(text)):
        return True
    return any(
        int(match) == issue_number for match in _BRANCH_ISSUE_RE.findall(text)
    )


def _run_matches_issue(run: Mapping[str, Any], issue_number: int) -> bool:
    for key in ("head_branch", "headBranch", "display_title", "displayTitle", "name"):
        if _references_issue(run.get(key), issue_number):
            return True
    return False


def _pr_matches_issue(pr: Mapping[str, Any], issue_number: int) -> bool:
    for key in ("body", "title", "headRefName", "head_ref_name"):
        if _references_issue(pr.get(key), issue_number):
            return True
    return False


def _pr_number(pr: Mapping[str, Any]) -> Optional[int]:
    try:
        number = int(pr.get("number"))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _active_runs(runs: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    active = []
    for run in runs or []:
        state = str(run.get("status") or "").strip().lower()
        if state in ACTIVE_RUN_STATES:
            active.append(run)
    return active


def _is_worker_run(run: Mapping[str, Any]) -> bool:
    name = str(
        run.get("name") or run.get("workflowName") or run.get("display_title") or ""
    ).strip().lower()
    return "opencode" in name or "worker" in name


def _terminal_worker_state(
    runs: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    if not runs:
        return None
    latest = runs[0]
    state = str(latest.get("status") or "").strip().lower()
    if state in ACTIVE_RUN_STATES:
        return "running"
    conclusion = str(
        latest.get("conclusion") or latest.get("state") or ""
    ).strip().lower()
    if conclusion in ("success", "completed"):
        return "completed"
    if conclusion in ("failure", "failed", "timed_out"):
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    return conclusion or None


def observe_reservation(
    issue: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]] = (),
    pull_requests: Sequence[Mapping[str, Any]] = (),
    now: Optional[datetime] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Reservation:
    """Build a :class:`Reservation` from real GitHub state.

    ``runs`` are the workflow runs that reference this issue (newest first)
    and ``pull_requests`` are the open or recently merged PRs that reference
    it. A label is only evidence of live work when a run, PR or fresh lease
    is attributable; otherwise the liveness evaluator reports the stranded
    reservation as an error.
    """
    number = _issue_number(issue)
    has_label = IN_PROGRESS_LABEL in _labels(issue)
    matching_runs = [run for run in runs or [] if _run_matches_issue(run, number)]
    active = _active_runs(matching_runs)
    live_run_id = _run_id(active[0]) if active else None
    live_pr_number = None
    for pr in pull_requests or []:
        if str(pr.get("state") or "").strip().lower() == "closed":
            continue
        if _pr_matches_issue(pr, number):
            live_pr_number = _pr_number(pr)
            if live_pr_number is not None:
                break
    worker_state = _terminal_worker_state(matching_runs)
    lease_stale = False
    if active:
        age = _age_seconds(_run_started_at(active[0]), now)
        lease_stale = bool(
            age is not None and lease_seconds > 0 and age > lease_seconds
        )
    return Reservation(
        issue_number=number,
        has_label=has_label,
        live_run_id=live_run_id,
        live_pr_number=live_pr_number,
        live_lease=bool(active),
        lease_stale=lease_stale,
        worker_state=worker_state,
    )


def _latest_pr(
    issue_number: int, pull_requests: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    matches = [
        pr for pr in pull_requests or [] if _pr_matches_issue(pr, issue_number)
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda pr: str(
            pr.get("updated_at") or pr.get("updatedAt") or pr.get("created_at") or ""
        ),
        reverse=True,
    )
    return matches[0]


def _pr_merged(pr: Optional[Mapping[str, Any]]) -> bool:
    if not pr:
        return False
    return bool(pr.get("merged_at") or pr.get("mergedAt"))


def _pr_state(pr: Mapping[str, Any]) -> str:
    return str(pr.get("state") or "").strip().lower()


def _review_stage(pr: Mapping[str, Any]) -> str:
    decision = str(pr.get("reviewDecision") or pr.get("review_decision") or "").strip().lower()
    if decision == "approved":
        return "merge"
    if decision == "changes_requested":
        return "repair"
    return "review"


def _stage_elapsed(
    stage: str,
    issue: Mapping[str, Any],
    pr: Optional[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    now: Optional[datetime],
) -> int:
    candidates = []
    if stage == "admission":
        # Admission measures how long the task has waited for a worker, so
        # the issue creation time is the authoritative start.
        candidates.append(issue.get("created_at") or issue.get("createdAt"))
    if stage == "worker" and runs:
        candidates.append(_run_started_at(runs[0]))
    if pr is not None:
        candidates.append(pr.get("updated_at") or pr.get("updatedAt"))
        if stage == "merge" and _pr_merged(pr):
            candidates.append(pr.get("merged_at") or pr.get("mergedAt"))
    candidates.append(issue.get("updated_at") or issue.get("updatedAt"))
    candidates.append(issue.get("created_at") or issue.get("createdAt"))
    for value in candidates:
        age = _age_seconds(value, now)
        if age is not None:
            return age
    raise ContractQualificationError(
        "observed canary issue has no usable timestamp for stage " + stage
    )


def observe_canary(
    issue: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]] = (),
    pull_requests: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
    canary_id: str = "canary-main",
    now: Optional[datetime] = None,
) -> CanaryObservation:
    """Read a live canary task's current stage from real GitHub state.

    The stage is derived from evidence, never supplied by the caller:

    - closed issue with a merged implementation PR: the canary completed;
    - closed issue without a merged PR: the canary failed loudly;
    - active worker run: ``worker``;
    - open implementation PR: ``ci``, ``review``, ``repair`` or ``merge``
      according to its run and review state;
    - reserved but no worker/PR yet: ``admission``.
    """
    number = _issue_number(issue)
    matching_runs = [run for run in runs or [] if _run_matches_issue(run, number)]
    matching_runs.sort(
        key=lambda run: str(_run_started_at(run) or ""), reverse=True
    )
    pr = _latest_pr(number, pull_requests)
    active = _active_runs(matching_runs)
    recovery_attempts = sum(
        1
        for comment in comments or []
        if CANARY_RECOVERY_MARKER in str(comment.get("body") or "")
    )
    state_text = str(issue.get("state") or "").strip().lower()
    if state_text == "closed":
        if _pr_merged(pr):
            return CanaryObservation(
                state=None,
                completed=True,
                failure="",
            )
        return CanaryObservation(
            state=None,
            completed=False,
            failure="canary issue closed without a merged implementation PR",
        )

    if active:
        # A worker run is the lifecycle worker; any other active run for the
        # canary branch is the implementation CI stage.
        stage = "worker" if _is_worker_run(active[0]) else "ci"
    elif pr is not None and _pr_state(pr) == "open":
        run_state = _terminal_worker_state(matching_runs)
        if run_state in ("failed", "cancelled", "timed_out"):
            stage = "repair"
        else:
            stage = _review_stage(pr)
    else:
        stage = "admission"

    latest_run = active[0] if active else (matching_runs[0] if matching_runs else None)
    state = CanaryState(
        canary_id=str(canary_id or "canary-main"),
        issue_number=number,
        pr_number=_pr_number(pr) if pr is not None else None,
        run_id=_run_id(latest_run) if latest_run is not None else None,
        stage=stage,
        stage_elapsed_seconds=_stage_elapsed(stage, issue, pr, matching_runs, now),
        recovery_attempts=recovery_attempts,
    )
    return CanaryObservation(state=state, completed=False, failure="")


def select_observations(
    issues: Iterable[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Return only issues carrying the reservation label."""
    return [
        issue for issue in issues or [] if IN_PROGRESS_LABEL in _labels(issue)
    ]
