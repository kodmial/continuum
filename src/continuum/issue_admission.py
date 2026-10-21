"""Canonical admission policy for new issue-coder attempts.

``automation:ready`` is the single mandatory permission for a *new*
implementation attempt. The label is fixed in core: it is deliberately not a
``workflow_call`` input and cannot be overridden by a repository variable.
Every path that could start a coder -- the scheduled reconciler, a manual
owner ``/oc`` command, a direct ``issue`` workflow dispatch, watchdog/429
fresh-run recovery, delegated child workers, and delayed events -- re-reads
the actual target issue and applies :func:`decide_issue_admission`.

Admission is deliberately separate from authorization/provenance: a command
may be authentic and still not admitted, and it may be eligible while the
caller is not authorized. Both checks are required for every launch.

Legacy ``automation:paused`` / ``pause_on_failure`` / configurable
``ready_label`` values are inert here. They are accepted as compatibility
inputs by old ``workflow_call`` callers, but they never widen admission.
Errors are recovered by bounded retry/backoff and explicit generation STOP,
never by a permanent human pause.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

#: The one mandatory permission for a new issue-coder attempt.
READY_LABEL = "automation:ready"

#: Qualification lifecycle labels. These still gate generic implementation.
QUALIFYING_LABEL = "automation:qualifying"
BLOCKED_LABEL = "automation:blocked"

#: Legacy terminal-pause label. It is never written or read as admission
#: state any more; a stale label is inert so migration cannot strand work.
LEGACY_PAUSE_LABEL = "automation:paused"

#: Decision codes. Stable strings so workflows and tests can assert them.
ADMITTED = "admitted"
ISSUE_CLOSED = "issue_closed"
MISSING_READY = "missing_ready"
GENERATION_STOPPED = "generation_stopped"
QUALIFICATION_LIFECYCLE = "qualification_lifecycle"
BLOCKED = "blocked"
TARGET_IS_PULL_REQUEST = "target_is_pull_request"

#: Compatibility outcome for callers that configured a custom ready label.
LEGACY_READY_LABEL_IGNORED = "legacy_ready_label_ignored"


@dataclass(frozen=True)
class AdmissionDecision:
    """One deterministic admission decision."""

    admitted: bool
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "code": self.code,
            "reason": self.reason,
        }


def _label_set(labels: Iterable[str]) -> set:
    return {str(label) for label in (labels or []) if str(label)}


def decide_issue_admission(
    *,
    labels: Iterable[str],
    is_open: bool = True,
    is_pull_request: bool = False,
    generation_stopped: bool = False,
    blocked: bool = False,
    qualifying: bool = False,
    code: str = "",
) -> AdmissionDecision:
    """Decide whether a new coder attempt may start for the target issue.

    Ordering is fail-closed and deterministic. ``automation:paused`` is not
    consulted: removing ``automation:ready`` is the only way to stop new
    starts, and an explicit STOP is the only way to cancel running work.
    """

    if is_pull_request:
        return AdmissionDecision(
            False,
            TARGET_IS_PULL_REQUEST,
            "admission applies to issues only; PR review/repair has its own gate",
        )
    if not is_open:
        return AdmissionDecision(False, ISSUE_CLOSED, "target issue is closed")
    if generation_stopped:
        return AdmissionDecision(
            False,
            GENERATION_STOPPED,
            "implementation generation is terminally stopped",
        )
    if blocked:
        return AdmissionDecision(
            False,
            BLOCKED,
            "capability is blocked in the qualification lifecycle",
        )
    if qualifying:
        return AdmissionDecision(
            False,
            QUALIFICATION_LIFECYCLE,
            "capability is in the qualification lifecycle",
        )
    if READY_LABEL not in _label_set(labels):
        return AdmissionDecision(
            False,
            MISSING_READY,
            "missing required admission label `%s`" % READY_LABEL,
        )
    return AdmissionDecision(True, ADMITTED, "admitted")


def admission_skip_reason(decision: AdmissionDecision) -> str:
    """Workflow/test-facing skip text for a denied decision."""

    if decision.admitted:
        return ""
    return decision.reason


def legacy_ready_label_is_ignored(
    configured_value: Optional[str],
) -> bool:
    """Whether a caller-provided legacy ready label is being ignored.

    Empty configuration is not a deprecation signal. Any non-empty value,
    including the canonical label, is accepted for backward compatibility but
    cannot change core admission.
    """

    return bool(str(configured_value or "").strip())


def authorization_and_admission(
    *, authorized: bool, admission: AdmissionDecision
) -> bool:
    """Require both authentic command provenance and readiness.

    A valid owner command is not an either/or alternative to readiness.
    """

    return bool(authorized) and admission.admitted
