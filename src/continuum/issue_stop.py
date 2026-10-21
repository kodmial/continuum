"""Durable operator STOP, cancellation targeting, and fence observation.

``/oc-cancel`` is no longer only a launch guard: an authenticated command is
an operator STOP for one ``repository + issue + generation``. STOP has to
revoke ``automation:ready`` first, persist a durable intent, cancel only the
runs that belong to that exact issue/generation, and then observe the real
terminal outcome. A successful cancellation POST is not proof that the
process stopped.

This module is pure (no I/O). Workflows fetch comments/runs, call the
functions here, and perform the returned side effects. It deliberately
reuses the existing terminal generation-stop/recover grammar from
:mod:`continuum.task_snapshot` (``reason=operator``) so scheduler, OpenCode,
watchdog and delegated admission all share one stop identity.

State machine
-------------
``STOP_REQUESTED`` -> ``CANCEL_PENDING`` -> ``STOPPED``
                                      \\-> ``PARTIAL`` / ``FAILED``

``STOPPED`` requires every targeted run to be terminal with a cancellation
(or already-terminal) outcome. A queued/in-progress run keeps the state at
``CANCEL_PENDING``; an unavailable reference or a run that ends non-terminal
is ``PARTIAL``; an API/authorization failure is ``FAILED``. Repeated STOP is
idempotent because the earliest intent/fence wins and later duplicates do
not create a second fence.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from .task_snapshot import (
    GENERATION_RECOVER_MARKER,
    GENERATION_STOP_MARKER,
    is_trusted_snapshot_comment,
    parse_generation_recover,
    parse_generation_stop,
)

#: Operator STOP reason in the shared terminal generation-stop grammar.
OPERATOR_STOP_REASON = "operator"

#: Marker carrying the monotonic intent/fence identity for an operator STOP.
STOP_INTENT_MARKER = "continuum-operator-stop-intent"

#: Stop lifecycle states.
STOP_REQUESTED = "STOP_REQUESTED"
CANCEL_PENDING = "CANCEL_PENDING"
STOPPED = "STOPPED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"

#: Native run statuses that still consume/execute a coder slot.
ACTIVE_RUN_STATUSES = frozenset(
    {
        "queued",
        "requested",
        "waiting",
        "pending",
        "in_progress",
        "in-progress",
    }
)

_TERMINAL_RUN_STATUSES = frozenset({"completed"})

_STOP_INTENT_RE = re.compile(
    r"<!--\s*continuum-operator-stop-intent\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s+"
    r"fence=([A-Za-z0-9_.:\-]+)\s*-->"
)


@dataclass(frozen=True)
class OperatorStopIntent:
    """Durable operator STOP identity for one generation."""

    repo: str
    issue: int
    generation: int
    fence: str
    created_at: str = ""
    comment_id: int = 0


@dataclass(frozen=True)
class RunTarget:
    """One Actions run selected as belonging to the stopped generation."""

    run_id: int
    path: str
    status: str
    conclusion: str
    title: str
    provenance: str


@dataclass(frozen=True)
class RunSelection:
    """Result of selecting cancellation targets from a run inventory."""

    targets: tuple
    skipped: tuple

    @property
    def target_ids(self) -> list:
        return [run.run_id for run in self.targets]


@dataclass(frozen=True)
class StopObservation:
    """Observed cancellation outcome for one STOP intent."""

    state: str
    target_ids: tuple
    pending_ids: tuple
    stopped_ids: tuple
    failure: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in (STOPPED, PARTIAL, FAILED)


def make_fence(comment_id: Any, created_at: Any = "") -> str:
    """Build a stable, monotonic-enough fence identity for one command.

    The GitHub comment id is monotonic within a repository; the timestamp is
    included so a replayed comment body cannot forge a new fence by editing
    text. Empty input still yields a deterministic digest rather than failing
    open.
    """

    payload = "{}:{}".format(comment_id or 0, str(created_at or "").strip())
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "{}:{}".format(int(comment_id or 0), digest)


def render_stop_intent(
    repo: Any,
    issue: Any,
    *,
    generation: int = 1,
    fence: str,
) -> str:
    """Render the durable intent marker comment body."""

    repository = str(repo or "").strip().lower()
    number = int(issue)
    generation_number = int(generation)
    token = str(fence or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:\-]+", token or ""):
        raise ValueError("fence must be a stable identifier")
    marker = (
        "<!-- {} repo={} issue={} generation={} fence={} -->".format(
            STOP_INTENT_MARKER, repository, number, generation_number, token
        )
    )
    return "\n".join(
        [
            marker,
            "Continuum operator STOP (machine-owned, do not edit or delete). "
            "This generation is fenced; do not publish code or merge it until "
            "an explicit owner recovery re-arms the generation.",
        ]
    )


def parse_stop_intent(body: Any) -> Optional[OperatorStopIntent]:
    """Parse one comment body as an operator STOP intent marker."""

    match = _STOP_INTENT_RE.search(str(body or ""))
    if not match:
        return None
    repo, issue, generation, fence = match.groups()
    return OperatorStopIntent(
        repo=repo.lower(),
        issue=int(issue),
        generation=int(generation),
        fence=fence,
    )


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _order_key(comment: Mapping[str, Any]):
    created = _parse_time(comment.get("created_at"))
    try:
        numeric_id = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        numeric_id = 0
    return (created is None, created or datetime.min.replace(tzinfo=timezone.utc), numeric_id)


def select_stop_intent(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: int = 1,
    owner_login: str = "",
) -> Optional[OperatorStopIntent]:
    """Earliest trusted STOP intent for the exact generation, if any."""

    repository = str(repo or "").strip().lower()
    number = int(issue)
    generation_number = int(generation)
    candidates = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        parsed = parse_stop_intent(comment.get("body", ""))
        if parsed is None:
            continue
        if (
            parsed.repo != repository
            or parsed.issue != number
            or parsed.generation != generation_number
        ):
            continue
        candidates.append(
            (
                _order_key(comment),
                OperatorStopIntent(
                    repo=parsed.repo,
                    issue=parsed.issue,
                    generation=parsed.generation,
                    fence=parsed.fence,
                    created_at=str(comment.get("created_at") or ""),
                    comment_id=int(comment.get("id") or 0),
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def stop_is_active(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: int = 1,
    owner_login: str = "",
) -> bool:
    """Whether an operator STOP is still in force.

    A recovery marker strictly after the latest matching stop re-arms the
    generation. Restoring ``automation:ready`` never does.
    """

    repository = str(repo or "").strip().lower()
    number = int(issue)
    generation_number = int(generation)
    intent = select_stop_intent(
        comments,
        repo=repository,
        issue=number,
        generation=generation_number,
        owner_login=owner_login,
    )
    stops = []
    recovers = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        stop = parse_generation_stop(comment.get("body", ""))
        if (
            stop is not None
            and stop.repo == repository
            and stop.issue == number
            and stop.generation == generation_number
        ):
            stops.append((_order_key(comment), comment))
            continue
        recover = parse_generation_recover(comment.get("body", ""))
        if (
            recover is not None
            and recover.repo == repository
            and recover.issue == number
            and recover.generation == generation_number
        ):
            recovers.append((_order_key(comment), comment))
    if intent is None and not stops:
        return False
    if not stops:
        # Intent persisted but the stop record has not been written yet:
        # fenced state is already in force.
        return True
    if not recovers:
        return True
    return max(item[0] for item in recovers) <= max(item[0] for item in stops)


def _run_issue_match(text: str, issue: int) -> bool:
    if not text:
        return False
    pattern = r"(^|[^0-9])#%d([^0-9]|$)" % int(issue)
    return re.search(pattern, str(text)) is not None


def _run_generation_match(text: str, generation: int) -> Optional[bool]:
    """True/False when the run text names a generation, None when silent."""

    text = str(text or "")
    if not text:
        return None
    matches = {
        int(value)
        for value in re.findall(
            r"(?:generation|gen)[^0-9]{0,4}(\d+)", text, flags=re.IGNORECASE
        )
    }
    if not matches:
        return None
    return generation in matches


def select_run_targets(
    runs: Iterable[Mapping[str, Any]],
    *,
    issue: int,
    generation: int = 1,
    workflow_paths: Iterable[str] = (),
) -> RunSelection:
    """Select only queued/in-progress runs for this issue and generation.

    Selection requires the exact workflow path (provenance), the issue number
    in the run's name/title (never the title alone), and -- when the run names
    a generation -- the same generation. Runs for unrelated generations,
    unrelated issues, or unrelated workflows are reported as skipped.
    """

    paths = {str(path or "").strip() for path in workflow_paths if str(path or "").strip()}
    targets = []
    skipped = []
    for run in runs or []:
        raw_id = run.get("id") or run.get("run_id")
        try:
            run_id = int(raw_id)
        except (TypeError, ValueError):
            skipped.append((raw_id, "missing run id"))
            continue
        path = str(run.get("path") or "").strip()
        status = str(run.get("status") or "").strip().lower()
        conclusion = str(run.get("conclusion") or "").strip().lower()
        title = str(
            run.get("display_title")
            or run.get("name")
            or run.get("run_name")
            or ""
        )
        branch = str(run.get("head_branch") or "")
        provenance_text = "{} {} {}".format(title, branch, path)

        if paths and path not in paths:
            skipped.append((run_id, "unrelated workflow path"))
            continue
        if not _run_issue_match(title, issue) and not _run_issue_match(branch, issue):
            skipped.append((run_id, "no issue provenance"))
            continue
        generation_match = _run_generation_match(provenance_text, generation)
        if generation_match is False:
            skipped.append((run_id, "different generation"))
            continue
        if status not in ACTIVE_RUN_STATUSES:
            if status in _TERMINAL_RUN_STATUSES:
                skipped.append((run_id, "already terminal"))
            else:
                skipped.append((run_id, "not cancellable"))
            continue
        targets.append(
            RunTarget(
                run_id=run_id,
                path=path,
                status=status,
                conclusion=conclusion,
                title=title,
                provenance=provenance_text.strip(),
            )
        )
    targets.sort(key=lambda run: run.run_id)
    return RunSelection(tuple(targets), tuple(skipped))


def observe_cancellation(
    *,
    intent: OperatorStopIntent,
    target_ids: Sequence[int],
    runs: Sequence[Mapping[str, Any]],
    cancel_failure: str = "",
) -> StopObservation:
    """Classify the real terminal outcome of a cancellation attempt.

    A queued/in-progress run keeps ``CANCEL_PENDING``. All targets terminal
    is ``STOPPED``. A missing target, a terminal non-cancel outcome, or a
    cancellation API failure is ``PARTIAL``/``FAILED`` -- never a false
    ``STOPPED``.
    """

    ids = tuple(int(run_id) for run_id in target_ids)
    if cancel_failure:
        return StopObservation(
            state=FAILED,
            target_ids=ids,
            pending_ids=(),
            stopped_ids=(),
            failure=cancel_failure,
        )
    if not ids:
        return StopObservation(
            state=STOPPED,
            target_ids=(),
            pending_ids=(),
            stopped_ids=(),
        )
    by_id = {}
    for run in runs or []:
        try:
            by_id[int(run.get("id") or run.get("run_id"))] = run
        except (TypeError, ValueError):
            continue
    pending = []
    stopped = []
    missing = []
    unexpected = []
    for run_id in ids:
        run = by_id.get(run_id)
        if run is None:
            missing.append(run_id)
            continue
        status = str(run.get("status") or "").strip().lower()
        conclusion = str(run.get("conclusion") or "").strip().lower()
        if status in ACTIVE_RUN_STATUSES:
            pending.append(run_id)
            continue
        if status in _TERMINAL_RUN_STATUSES and conclusion in (
            "cancelled",
            "canceled",
            "success",
            "failure",
            "timed_out",
            "skipped",
            "neutral",
            "stale",
            "action_required",
        ):
            stopped.append(run_id)
            continue
        unexpected.append(run_id)
    if pending:
        return StopObservation(
            state=CANCEL_PENDING,
            target_ids=ids,
            pending_ids=tuple(pending),
            stopped_ids=tuple(stopped),
        )
    if missing or unexpected:
        return StopObservation(
            state=PARTIAL,
            target_ids=ids,
            pending_ids=(),
            stopped_ids=tuple(stopped),
            failure="missing={} unexpected={}".format(
                ",".join(map(str, missing)) or "none",
                ",".join(map(str, unexpected)) or "none",
            ),
        )
    return StopObservation(
        state=STOPPED,
        target_ids=ids,
        pending_ids=(),
        stopped_ids=tuple(stopped),
    )


def stop_blocks_publication(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: int = 1,
    owner_login: str = "",
) -> bool:
    """Re-check immediately before push/PR/merge of the canceled generation."""

    return stop_is_active(
        comments,
        repo=repo,
        issue=issue,
        generation=generation,
        owner_login=owner_login,
    )
