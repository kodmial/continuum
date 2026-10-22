"""Deterministic admission model for an optional external periodic wake-up.

Continuum's native GitHub ``schedule`` remains the authoritative periodic
signal. An owner may additionally configure an external cron service to POST
an authenticated ``workflow_dispatch`` at the same cadence. Both signals must
map onto the *same* scheduling window so the second delivery is a cheap NOOP
instead of a second reconciliation pass.

The issue scheduler's native cadence is ``7,17,27,37,47,57 * * * *``: a
10-minute window anchored at minute 7. The external workflow-dispatch run is
marked in its ``run-name`` with :data:`PERIODIC_RUN_MARKER`; native runs are
identified by ``event == "schedule"``. A completed successful run from either
source inside the current window is processing evidence, and a later periodic
signal in that window does not run the domain work.

Only successful completed runs count. A failed, cancelled, timed-out or
in-progress run leaves the window open so the next tick retries. A lookup
failure fails open (runs the pass) because a transient API error must not
strand due work; existing state-based idempotency still protects the domain
side effects. Ordinary manual dispatches and event-driven runs are never
gated by this periodic-only guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

# The caller stub stamps this token into its run-name when
# ``periodic_wake`` is the string ``true``. Native schedule runs need no
# marker because their event name is already unambiguous.
PERIODIC_RUN_MARKER = "[continuum-periodic]"

# Native Issue scheduler cron: 7,17,27,37,47,57 * * * *.
ISSUE_SCHEDULER_INTERVAL_SECONDS = 10 * 60
ISSUE_SCHEDULER_OFFSET_SECONDS = 7 * 60

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})


def parse_periodic_wake(value: Any) -> bool:
    """Return whether the mode selector asks for external periodic behavior."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def is_periodic_signal(
    caller_event_name: Optional[str], periodic_wake: Any
) -> bool:
    """Native schedule is always periodic; a dispatch opts in explicitly."""
    return (
        str(caller_event_name or "").strip().lower() == "schedule"
        or parse_periodic_wake(periodic_wake)
    )


def scheduling_window_start(
    now_epoch: int,
    *,
    interval_seconds: int = ISSUE_SCHEDULER_INTERVAL_SECONDS,
    offset_seconds: int = ISSUE_SCHEDULER_OFFSET_SECONDS,
) -> int:
    """Start of the native scheduling window that contains ``now_epoch``.

    The offset aligns the window with the configured cron minute rather than
    the receipt time of an HTTP request. A native run delayed by a few
    minutes therefore still lands in its intended window instead of opening a
    new one.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    offset = offset_seconds % interval_seconds
    return int(now_epoch) - ((int(now_epoch) - offset) % interval_seconds)


def _created_epoch(run: Mapping[str, Any]) -> Optional[int]:
    value = run.get("created_at")
    if value is None:
        value = run.get("createdAt")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def is_periodic_evidence(
    run: Mapping[str, Any],
    *,
    current_run_id: Any,
    window_start_epoch: int,
    now_epoch: Optional[int] = None,
    default_branch: Optional[str] = None,
) -> bool:
    """Return whether ``run`` proves a successful periodic pass in the window."""
    run_id = run.get("id")
    if run_id is None or str(run_id) == str(current_run_id):
        return False
    if str(run.get("status") or "") != "completed":
        return False
    if str(run.get("conclusion") or "") != "success":
        return False

    event = str(run.get("event") or "")
    title = str(run.get("display_title") or run.get("displayTitle") or "")
    periodic_dispatch = event == "workflow_dispatch" and PERIODIC_RUN_MARKER in title
    if event != "schedule" and not periodic_dispatch:
        return False

    if default_branch:
        branch = run.get("head_branch")
        if branch is not None and str(branch) != default_branch:
            return False

    created = _created_epoch(run)
    if created is None or created < window_start_epoch:
        return False
    if now_epoch is not None and created > int(now_epoch):
        return False
    return True


@dataclass
class PeriodicDecision:
    """Outcome of the cheap pre-domain admission check."""

    run: bool
    reason: str
    window_start: Optional[int] = None
    evidence_run_id: Optional[Any] = None


def periodic_dedupe_decision(
    *,
    now_epoch: int,
    runs: Iterable[Mapping[str, Any]],
    current_run_id: Any,
    caller_event_name: Optional[str],
    periodic_wake: Any,
    lookup_failed: bool = False,
    default_branch: Optional[str] = None,
    interval_seconds: int = ISSUE_SCHEDULER_INTERVAL_SECONDS,
    offset_seconds: int = ISSUE_SCHEDULER_OFFSET_SECONDS,
) -> PeriodicDecision:
    """Decide whether a periodic signal should execute the domain pass.

    ``runs`` is the recent workflow-run listing. Callers may pass an empty
    listing when there is no evidence, and ``lookup_failed=True`` to model a
    403/429/transient REST failure. Both cases run the pass: a missed wake is
    worse than a redundant cheap pass, and the domain remains idempotent.
    """
    if not is_periodic_signal(caller_event_name, periodic_wake):
        return PeriodicDecision(True, "non-periodic")
    if lookup_failed:
        return PeriodicDecision(True, "lookup-failed")

    window_start = scheduling_window_start(
        now_epoch,
        interval_seconds=interval_seconds,
        offset_seconds=offset_seconds,
    )
    for run in runs:
        if is_periodic_evidence(
            run,
            current_run_id=current_run_id,
            window_start_epoch=window_start,
            now_epoch=now_epoch,
            default_branch=default_branch,
        ):
            return PeriodicDecision(
                False,
                "duplicate-window",
                window_start=window_start,
                evidence_run_id=run.get("id"),
            )
    return PeriodicDecision(True, "window-due", window_start=window_start)

