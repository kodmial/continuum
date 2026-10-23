"""Physical cancellation of one OpenCode issue implementation generation.

``/oc-cancel`` used to be a launch guard only. This module owns the durable
operator stop intent, the strict GitHub Actions run provenance that may be
cancelled, and the deterministic classification of GitHub's cancellation
responses. It is deliberately separate from the drift/tamper
``continuum-task-generation-stop`` marker: an operator cancel is recorded as
``continuum-task-operator-stop`` and is re-armed only by the explicit
``continuum-task-operator-resume`` marker (or by a new generation number).
An ordinary scheduler dispatch, watchdog retry, label flip or 429 recovery
never re-arms it.

The command grammar itself stays in :mod:`continuum.opencode_commands`; this
module never parses free text as a command.

Run provenance rules
--------------------
Only active runs that all of the following identify may be cancelled:

* the run belongs to the exact caller workflow path or workflow name supplied
  by the trusted controller;
* the run event is ``issue_comment`` or ``workflow_dispatch``;
* the run's stable ``display_title`` binds it to the exact issue number (and,
  for delegated Parent workers, the exact Child id);
* a non-generation-1 run additionally requires a trusted dispatch-time run-id
  association, because the issue run title carries no generation number.

A legacy ``SubTask #<n>`` title has no Child identity and is never guessed at:
it is cancellable only through an explicit associated run id. Lines that only
mention a similar title, issue number or workflow name never match.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .task_snapshot import (
    DEFAULT_GENERATION,
    TaskSnapshotError,
    is_generation_terminally_stopped,
    is_trusted_snapshot_comment,
    normalize_generation,
    normalize_issue_number,
    normalize_repo,
)

#: Marker for a durable operator cancellation stop.
OPERATOR_STOP_MARKER = "continuum-task-operator-stop"

#: Marker for the explicit operator re-arm of one stopped generation.
OPERATOR_RESUME_MARKER = "continuum-task-operator-resume"

#: Closed reason vocabulary for the operator stop record.
OPERATOR_STOP_REASON = "operator"

#: GitHub Actions run statuses that still own work.
ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "pending", "waiting", "requested", "in_progress", "action_required"}
)

#: GitHub Actions run status that is terminal for cancellation purposes.
COMPLETED_RUN_STATUS = "completed"

#: Events that can represent issue implementation work.
CANCELLABLE_RUN_EVENTS = frozenset({"issue_comment", "workflow_dispatch"})

_OPERATOR_STOP_RE = re.compile(
    r"<!--\s*continuum-task-operator-stop\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s*-->"
)

_OPERATOR_RESUME_RE = re.compile(
    r"<!--\s*continuum-task-operator-resume\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s*-->"
)


@dataclass(frozen=True)
class ParsedOperatorStop:
    """A parsed durable operator stop (identity only, no task text)."""

    repo: str
    issue: int
    generation: int


@dataclass(frozen=True)
class ParsedOperatorResume:
    """A parsed explicit operator re-arm for one generation."""

    repo: str
    issue: int
    generation: int


@dataclass(frozen=True)
class RunBinding:
    """The issue identity embedded in one run's stable display title."""

    kind: str
    issue: int
    child_id: str = ""
    recovery_generation: Optional[int] = None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sort_key(item: Tuple[Optional[datetime], Any]) -> Tuple[bool, datetime, int]:
    created, comment_id = item
    try:
        numeric_id = int(comment_id) if comment_id is not None else 0
    except (TypeError, ValueError):
        numeric_id = 0
    return (
        created is None,
        created or datetime.min.replace(tzinfo=timezone.utc),
        numeric_id,
    )


def parse_operator_stop(body: Any) -> Optional[ParsedOperatorStop]:
    """Parse one comment body as an operator stop, or None."""

    match = _OPERATOR_STOP_RE.search(str(body or ""))
    if not match:
        return None
    repo_text, issue_text, generation_text = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation = normalize_generation(generation_text)
    except TaskSnapshotError:
        return None
    return ParsedOperatorStop(repo=repository, issue=number, generation=generation)


def parse_operator_resume(body: Any) -> Optional[ParsedOperatorResume]:
    """Parse one comment body as an explicit operator resume, or None."""

    match = _OPERATOR_RESUME_RE.search(str(body or ""))
    if not match:
        return None
    repo_text, issue_text, generation_text = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation = normalize_generation(generation_text)
    except TaskSnapshotError:
        return None
    return ParsedOperatorResume(repo=repository, issue=number, generation=generation)


def render_operator_stop(
    repo: Any,
    issue: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
) -> str:
    """Render the durable operator stop comment body."""

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    marker = "<!-- {} repo={} issue={} generation={} -->".format(
        OPERATOR_STOP_MARKER, repository, number, generation_number
    )
    return "\n".join(
        [
            marker,
            "Continuum operator cancellation stop (machine-owned, do not edit or delete).",
            "This implementation generation was cancelled by the repository owner. "
            "Scheduler, watchdog, 429 recovery, label changes, and automated "
            "retries must not redispatch it. Resume only with an explicit trusted "
            "operator resume marker or a new generation number.",
        ]
    )


def render_operator_resume(
    repo: Any,
    issue: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
) -> str:
    """Render the explicit operator resume comment body for one generation."""

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    marker = "<!-- {} repo={} issue={} generation={} -->".format(
        OPERATOR_RESUME_MARKER, repository, number, generation_number
    )
    return "\n".join(
        [
            marker,
            "Continuum explicit operator resume for one cancelled generation.",
        ]
    )


def select_operator_stop(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> Optional[ParsedOperatorStop]:
    """Return the earliest trusted operator stop for one generation."""

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    candidates = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        parsed = parse_operator_stop(comment.get("body", ""))
        if (
            parsed is None
            or parsed.repo != repository
            or parsed.issue != number
            or parsed.generation != generation_number
        ):
            continue
        candidates.append((_parse_timestamp(comment.get("created_at")), comment.get("id")))
    if not candidates:
        return None
    candidates.sort(key=_sort_key)
    return ParsedOperatorStop(
        repo=repository, issue=number, generation=generation_number
    )


def is_operator_stopped(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> bool:
    """Whether an operator stop is effective for this exact generation.

    An operator resume re-arms only when it is strictly later (timestamp, then
    comment id) than the latest operator stop. Drift/tamper recovery markers
    never re-arm an operator stop and operator resumes never re-arm a drift
    stop, because the two stop kinds stay independent.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    stops = []
    resumes = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        stop = parse_operator_stop(comment.get("body", ""))
        if (
            stop is not None
            and stop.repo == repository
            and stop.issue == number
            and stop.generation == generation_number
        ):
            stops.append((_parse_timestamp(comment.get("created_at")), comment.get("id")))
            continue
        resume = parse_operator_resume(comment.get("body", ""))
        if (
            resume is not None
            and resume.repo == repository
            and resume.issue == number
            and resume.generation == generation_number
        ):
            # A malformed resume timestamp must never re-arm a stop.
            created = _parse_timestamp(comment.get("created_at"))
            if created is not None:
                resumes.append((created, comment.get("id")))
    if not stops:
        return False
    stops.sort(key=_sort_key)
    resumes.sort(key=_sort_key)
    if not resumes:
        return True
    return _sort_key(resumes[-1]) <= _sort_key(stops[-1])


def is_execution_stopped(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> bool:
    """Whether either an operator stop or a drift/tamper stop holds.

    The two markers remain distinct; this helper only lets every existing
    admission gate ask one yes/no question before dispatch or publication.
    """

    if is_operator_stopped(
        comments,
        repo=repo,
        issue=issue,
        generation=generation,
        owner_login=owner_login,
    ):
        return True
    return is_generation_terminally_stopped(
        comments,
        repo=repo,
        issue=issue,
        generation=generation,
        owner_login=owner_login,
    )


def execution_stop_kind(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> str:
    """Return ``operator``, ``generation`` or ``active`` for this generation."""

    if is_operator_stopped(
        comments,
        repo=repo,
        issue=issue,
        generation=generation,
        owner_login=owner_login,
    ):
        return "operator"
    if is_generation_terminally_stopped(
        comments,
        repo=repo,
        issue=issue,
        generation=generation,
        owner_login=owner_login,
    ):
        return "generation"
    return "active"


_OPENCODE_ISSUE_RE = re.compile(r"^OpenCode issue #(\d+)$")
_OPENCODE_RECOVERY_RE = re.compile(
    r"^OpenCode 429 recovery (\S+) gen (\d+)$"
)
_DELEGATED_TASK_RE = re.compile(
    r"^(?:Child|Worker) task \[([^\]]+)\] #(\d+)$"
)
_LEGACY_SUBTASK_RE = re.compile(r"^SubTask #(\d+)$")
_RECOVERY_OPERATION_ISSUE_RE = re.compile(r"(?:^|-)issue(\d+)(?:-|$)")


def bind_run_to_issue(run: Mapping[str, Any]) -> Optional[RunBinding]:
    """Resolve the exact issue identity carried by one run display title."""

    title = str(run.get("display_title") or "").strip()
    match = _OPENCODE_ISSUE_RE.match(title)
    if match:
        return RunBinding(kind="issue", issue=int(match.group(1)))
    match = _OPENCODE_RECOVERY_RE.match(title)
    if match:
        operation, generation_text = match.groups()
        issue_match = _RECOVERY_OPERATION_ISSUE_RE.search(operation)
        if not issue_match:
            return None
        return RunBinding(
            kind="recovery",
            issue=int(issue_match.group(1)),
            recovery_generation=int(generation_text),
        )
    match = _DELEGATED_TASK_RE.match(title)
    if match:
        return RunBinding(kind="delegated", issue=int(match.group(2)), child_id=match.group(1))
    match = _LEGACY_SUBTASK_RE.match(title)
    if match:
        return RunBinding(kind="legacy-delegated", issue=int(match.group(1)))
    return None


def _normalize_workflow_path(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("./"):
        text = text[2:]
    return text


def _workflow_provenance_matches(
    run: Mapping[str, Any],
    workflow_paths: Iterable[str],
    workflow_names: Iterable[str],
) -> bool:
    paths = {_normalize_workflow_path(path) for path in workflow_paths if str(path or "").strip()}
    names = {str(name).strip() for name in workflow_names if str(name or "").strip()}
    if not paths and not names:
        return False
    run_path = _normalize_workflow_path(run.get("path"))
    if run_path:
        # A reported workflow path is the authoritative provenance. A matching
        # display name alone is never enough when the path says otherwise.
        return run_path in paths
    run_name = str(run.get("name") or "").strip()
    return bool(run_name) and run_name in names


def _run_id(run: Mapping[str, Any]) -> Optional[int]:
    try:
        value = int(run.get("id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def run_matches_cancellation(
    run: Mapping[str, Any],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    workflow_paths: Iterable[str] = (),
    workflow_names: Iterable[str] = (),
    child_id: str = "",
    associated_run_ids: Iterable[Any] = (),
    exclude_run_id: Any = None,
) -> Tuple[bool, str]:
    """Whether one run is an exact actionable target for this cancellation.

    Returns ``(True, reason)`` only when every provenance check passes. The
    reason for a negative result is safe to log (no repository names or task
    text). Unknown statuses and malformed identities fail closed.
    """

    normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)

    if not isinstance(run, dict):
        return False, "malformed run entry"
    run_id = _run_id(run)
    if run_id is None:
        return False, "run id is missing or malformed"

    try:
        excluded = int(exclude_run_id) if exclude_run_id not in (None, "") else None
    except (TypeError, ValueError):
        excluded = None
    if excluded is not None and run_id == excluded:
        return False, "control-plane run is never cancelled"

    status = str(run.get("status") or "").strip()
    if status == COMPLETED_RUN_STATUS:
        return False, "run is already completed"
    if status not in ACTIVE_RUN_STATUSES:
        return False, "run status is not a recognized active state"

    event = str(run.get("event") or "").strip()
    if event not in CANCELLABLE_RUN_EVENTS:
        return False, "run event is not issue implementation work"
    if not _workflow_provenance_matches(run, workflow_paths, workflow_names):
        return False, "run workflow provenance does not match the caller"

    binding = bind_run_to_issue(run)
    if binding is None:
        return False, "run title does not carry an exact issue identity"
    if binding.issue != number:
        return False, "run title binds another issue"

    if binding.kind == "recovery":
        # The recovery generation is an infrastructure counter, not the task
        # implementation generation. The operation identity already binds the
        # exact issue, so every active recovery of that issue is actionable.
        return True, "active 429 recovery for the exact issue"

    if generation_number != DEFAULT_GENERATION:
        try:
            associated = {int(value) for value in associated_run_ids}
        except (TypeError, ValueError):
            return False, "trusted run-id association is malformed"
        if run_id not in associated:
            return False, "non-default generation requires a trusted run-id association"

    if binding.kind == "issue":
        return True, "active issue implementation for the exact issue"

    if binding.kind == "delegated":
        wanted_child = str(child_id or "").strip()
        if not wanted_child:
            return False, "delegated run needs the exact Child identity"
        if binding.child_id != wanted_child:
            return False, "delegated run binds another Child"
        return True, "active delegated task for the exact issue and Child"

    if binding.kind == "legacy-delegated":
        try:
            associated = {int(value) for value in associated_run_ids}
        except (TypeError, ValueError):
            return False, "trusted run-id association is malformed"
        if run_id not in associated:
            return False, "legacy delegated title has no Child attribution"
        return True, "legacy delegated task bound by a trusted run-id association"

    return False, "unrecognized run identity"


def plan_cancellation(
    runs: Any,
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    workflow_paths: Iterable[str] = (),
    workflow_names: Iterable[str] = (),
    child_id: str = "",
    associated_run_ids: Iterable[Any] = (),
    exclude_run_id: Any = None,
) -> Dict[str, Any]:
    """Return a deterministic cancellation plan for an already-fetched inventory.

    ``inventory`` is ``ok`` only for a complete JSON array. A non-list or a
    malformed entry makes the whole inventory ``unavailable``: the caller must
    report that failure and cancel nothing rather than guess.
    """

    if not isinstance(runs, list):
        return {"inventory": "unavailable", "reason": "run inventory is not a JSON array", "targets": [], "ignored": []}
    for run in runs:
        if not isinstance(run, dict) or _run_id(run) is None:
            return {"inventory": "unavailable", "reason": "run inventory entry is malformed", "targets": [], "ignored": []}

    targets: List[Dict[str, Any]] = []
    ignored: List[Dict[str, Any]] = []
    for run in runs:
        matched, reason = run_matches_cancellation(
            run,
            repo=repo,
            issue=issue,
            generation=generation,
            workflow_paths=workflow_paths,
            workflow_names=workflow_names,
            child_id=child_id,
            associated_run_ids=associated_run_ids,
            exclude_run_id=exclude_run_id,
        )
        run_id = _run_id(run)
        if matched:
            targets.append(
                {
                    "run_id": run_id,
                    "status": run.get("status"),
                    "event": run.get("event"),
                    "display_title": run.get("display_title"),
                    "path": run.get("path"),
                    "reason": reason,
                }
            )
        else:
            ignored.append({"run_id": run_id, "reason": reason})

    targets.sort(key=lambda item: int(item["run_id"]))
    return {
        "inventory": "ok",
        "reason": "inventory evaluated",
        "targets": targets,
        "ignored": ignored,
    }


def classify_cancel_response(status: Any) -> str:
    """Classify a GitHub cancel/force-cancel HTTP status.

    ``accepted`` means GitHub accepted the request (including 202); it does
    *not* mean the job stopped. Only an observed terminal ``cancelled`` run
    proves the job stopped.
    """

    try:
        code = int(status)
    except (TypeError, ValueError):
        return "unknown"
    if 200 <= code < 300:
        return "accepted"
    if code == 409:
        return "conflict"
    if code == 403:
        return "forbidden"
    if code == 429:
        return "rate-limited"
    if 500 <= code < 600:
        return "server-error"
    if 400 <= code < 500:
        return "client-error"
    return "unknown"


def is_retryable_cancel_status(status: Any) -> bool:
    """Whether a failed cancellation request may be retried a bounded amount."""

    return classify_cancel_response(status) in {"rate-limited", "server-error"}


def observe_cancel_outcome(run: Mapping[str, Any]) -> str:
    """Describe an observed run state after a cancel request.

    Returns ``cancelled`` only for the real terminal cancelled conclusion;
    everything else is reported exactly as observed.
    """

    status = str(run.get("status") or "").strip()
    conclusion = str(run.get("conclusion") or "").strip().lower()
    if status == COMPLETED_RUN_STATUS:
        if conclusion == "cancelled":
            return "cancelled"
        return "completed:" + (conclusion or "unknown")
    if status in ACTIVE_RUN_STATUSES:
        return "active:" + status
    return "unknown:" + (status or "missing")


def needs_force_cancel(run: Mapping[str, Any]) -> bool:
    """Whether the same validated run is still non-terminal after observation."""

    return str(run.get("status") or "").strip() != COMPLETED_RUN_STATUS


__all__ = [
    "OPERATOR_STOP_MARKER",
    "OPERATOR_RESUME_MARKER",
    "OPERATOR_STOP_REASON",
    "ACTIVE_RUN_STATUSES",
    "COMPLETED_RUN_STATUS",
    "CANCELLABLE_RUN_EVENTS",
    "ParsedOperatorStop",
    "ParsedOperatorResume",
    "RunBinding",
    "parse_operator_stop",
    "parse_operator_resume",
    "render_operator_stop",
    "render_operator_resume",
    "select_operator_stop",
    "is_operator_stopped",
    "is_execution_stopped",
    "execution_stop_kind",
    "bind_run_to_issue",
    "run_matches_cancellation",
    "plan_cancellation",
    "classify_cancel_response",
    "is_retryable_cancel_status",
    "observe_cancel_outcome",
    "needs_force_cancel",
]
