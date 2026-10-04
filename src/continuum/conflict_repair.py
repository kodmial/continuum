"""Deterministic dirty-PR conflict-repair decisions (issue #246).

This module is the testable contract behind the auto-merge reconciler's
self-healing conflict recovery. It performs no I/O: the workflow reads live
GitHub state, feeds it here, and executes the returned decision.

Lifecycle model:

- Every reconciliation pass scans every open PR, including PRs that turned
  dirty before the current wake (no PR event or manual kick required).
- A dirty PR dispatches the *configured* consumer OpenCode caller
  (Continuum dogfood: ``opencode.yml``). The reusable engine file is never
  dispatched directly; it has no ``workflow_dispatch`` trigger.
- Dispatch is idempotent per PR + exact HEAD: repeated wakeups while a repair
  is in flight (active run, or a fresh dispatch marker inside the grace
  window) coalesce to ``wait`` instead of dispatching duplicates.
- A stale ``opencode-conflict-repair`` lock with no active run and no fresh
  marker is reconciled (cleared) and the repair is redispatched safely,
  bounded per HEAD.
- Retry is autonomous and bounded: after ``MAX_DISPATCHES_PER_HEAD``
  dispatches for one unchanged HEAD the decision is ``hold``. The hold never
  applies ``automation:paused`` and never requires manual label removal; a new
  HEAD starts a fresh budget.
- A PR that is no longer dirty resumes the normal lifecycle: the lock is
  reconciled (``resume`` clear signal), CI runs on the new HEAD, and the
  review/merge gates revalidate that exact HEAD. Review/CI gates themselves
  are never weakened here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

CONFLICT_LOCK_LABEL = "opencode-conflict-repair"
PAUSE_LABEL = "automation:paused"

DEFAULT_CALLER = "continuum-opencode.yml"
MAX_DISPATCHES_PER_HEAD = 3
DISPATCH_GRACE_SECONDS = 600

_REPAIR_MARKER_RE = re.compile(
    r"<!--\s*continuum-conflict-repair\s+"
    r"head=([0-9a-fA-F]{7,64})\s+"
    r"attempt=(\d+)\s*-->"
)

_ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)


class ConflictRepairError(ValueError):
    """Raised when conflict-repair inputs cannot be interpreted safely."""


@dataclass(frozen=True)
class ConflictDecision:
    """One deterministic reconciler decision for a single PR."""

    action: str  # dispatch | wait | hold | resume | none
    attempt: Optional[int]
    reason: str
    reconcile_lock: bool = False


def is_conflicted(mergeable: object, mergeable_state: object) -> Optional[bool]:
    """Return True/False for known mergeability, None while GitHub computes it.

    ``mergeable`` may be a REST boolean (or None) or a GraphQL-style string
    such as ``CONFLICTING``. ``mergeable_state`` is the REST ``dirty``/``clean``
    style string.
    """

    state = str(mergeable_state or "").strip().lower()
    if state == "dirty":
        return True
    if isinstance(mergeable, bool):
        return not mergeable
    if mergeable is None:
        return None if state in ("", "unknown") else False
    text = str(mergeable).strip().lower()
    if text in ("false", "conflicting", "conflicted", "dirty"):
        return True
    if text in ("true", "mergeable", "clean", "stable", "blocked", "behind"):
        return False
    if text in ("", "null", "unknown"):
        return None
    return None


def normalize_caller(configured: object, default: str = DEFAULT_CALLER) -> str:
    """Resolve the consumer OpenCode caller workflow file to dispatch.

    Never returns an empty value and never synthesizes a direct reusable-engine
    dispatch: the caller file name is always an explicit ``*.yml`` workflow
    that owns a ``workflow_dispatch`` trigger.
    """

    candidate = str(configured or "").strip() or str(default or "").strip()
    if not candidate:
        raise ConflictRepairError("conflict-repair caller workflow must not be empty")
    if not candidate.endswith(".yml"):
        raise ConflictRepairError(
            f"conflict-repair caller must be a workflow file, got {candidate!r}"
        )
    if "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ConflictRepairError(
            f"conflict-repair caller must be a bare workflow file name, got {candidate!r}"
        )
    return candidate


def _parse_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def repair_markers_for_head(
    comments: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    trusted_associations: Sequence[str] = ("OWNER", "MEMBER", "COLLABORATOR"),
) -> list:
    """Return trusted conflict-repair markers for one exact HEAD, oldest first."""

    head = str(head_sha or "").strip().lower()
    trusted = {str(item).upper() for item in trusted_associations}
    found: list = []
    for comment in comments:
        association = str(comment.get("author_association") or "").upper()
        if association not in trusted:
            continue
        body = str(comment.get("body") or "")
        created_at = _parse_time(
            comment.get("updated_at") or comment.get("created_at")
        )
        for match in _REPAIR_MARKER_RE.finditer(body):
            marker_head, attempt_text = match.groups()
            if marker_head.lower() != head:
                continue
            found.append(
                {
                    "attempt": int(attempt_text),
                    "created_at": created_at,
                    "comment_id": comment.get("id"),
                }
            )
    found.sort(
        key=lambda item: (
            item["attempt"],
            item["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    return found


def repair_marker(
    head_sha: str,
    attempt: int,
    *,
    short: bool = False,
) -> str:
    """Return the durable dispatch marker written on every conflict dispatch."""

    head = str(head_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head):
        raise ConflictRepairError("head_sha must be a hexadecimal commit id")
    if int(attempt) < 0:
        raise ConflictRepairError("attempt must be non-negative")
    if short:
        return f"<!-- continuum-conflict-repair head={head} attempt={int(attempt)} -->"
    return (
        f"<!-- continuum-conflict-repair head={head} attempt={int(attempt)} -->\n"
        f"Continuum dispatched automatic OpenCode conflict repair for this exact HEAD."
    )


def decide_conflict_repair(
    *,
    mergeable: object = None,
    mergeable_state: object = None,
    has_lock: bool = False,
    active_repair_run: bool = False,
    dispatches_for_head: int = 0,
    marker_age_seconds: Optional[int] = None,
    max_dispatches_per_head: int = MAX_DISPATCHES_PER_HEAD,
    dispatch_grace_seconds: int = DISPATCH_GRACE_SECONDS,
    head_changed_since_dispatch: bool = False,
) -> ConflictDecision:
    """Decide one PR's conflict-repair step from latest authoritative state.

    ``dispatches_for_head`` counts prior ``resolve-conflict`` dispatches for
    the *current* exact HEAD (durable markers). ``marker_age_seconds`` is the
    age of the newest such marker, if any. ``head_changed_since_dispatch``
    signals the repaired HEAD path: the PR left the conflicted HEAD behind.
    """

    if int(max_dispatches_per_head) <= 0:
        raise ConflictRepairError("max_dispatches_per_head must be positive")
    if int(dispatches_for_head) < 0:
        raise ConflictRepairError("dispatches_for_head must be non-negative")

    conflicted = is_conflicted(mergeable, mergeable_state)

    if head_changed_since_dispatch and conflicted is False:
        # The repair (or any head update) moved the PR off the conflicted
        # HEAD: reconcile the lock so CI runs on the new HEAD and the normal
        # PR-Agent/review/merge lifecycle resumes from exact-HEAD gates.
        if has_lock:
            return ConflictDecision(
                "resume",
                None,
                "repaired HEAD is no longer dirty; reconciling conflict lock",
                reconcile_lock=True,
            )
        return ConflictDecision(
            "resume", None, "repaired HEAD is no longer dirty; resuming lifecycle"
        )

    if conflicted is None:
        return ConflictDecision(
            "wait", None, "GitHub mergeability is still being computed"
        )
    if conflicted is False:
        if has_lock:
            return ConflictDecision(
                "resume",
                None,
                "PR is clean; reconciling leftover conflict lock",
                reconcile_lock=True,
            )
        return ConflictDecision("none", None, "PR is not conflicted")

    # Dirty from here on.
    if active_repair_run:
        return ConflictDecision(
            "wait", None, "a matching conflict-repair run is already active"
        )
    if (
        marker_age_seconds is not None
        and int(marker_age_seconds) < int(dispatch_grace_seconds)
        and int(dispatches_for_head) > 0
    ):
        return ConflictDecision(
            "wait", None, "a conflict-repair dispatch is inside its grace window"
        )
    if int(dispatches_for_head) >= int(max_dispatches_per_head):
        # Bounded autonomy: stop redispatching this unchanged HEAD. No pause
        # label is applied and no manual label removal is required; a new HEAD
        # starts a fresh budget.
        return ConflictDecision(
            "hold", None, "bounded conflict-repair budget exhausted for this HEAD"
        )
    if has_lock:
        return ConflictDecision(
            "dispatch",
            int(dispatches_for_head),
            "stale conflict lock with no active run; reconciling and redispatching",
            reconcile_lock=True,
        )
    return ConflictDecision(
        "dispatch", int(dispatches_for_head), "dirty PR needs conflict repair"
    )


def is_active_run_status(status: object) -> bool:
    """Return True when a workflow-run status counts as still in flight."""

    return str(status or "").strip().lower() in _ACTIVE_RUN_STATUSES


__all__ = [
    "CONFLICT_LOCK_LABEL",
    "PAUSE_LABEL",
    "DEFAULT_CALLER",
    "MAX_DISPATCHES_PER_HEAD",
    "DISPATCH_GRACE_SECONDS",
    "ConflictRepairError",
    "ConflictDecision",
    "is_conflicted",
    "normalize_caller",
    "repair_markers_for_head",
    "repair_marker",
    "decide_conflict_repair",
    "is_active_run_status",
]
