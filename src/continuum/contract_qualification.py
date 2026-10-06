"""Continuum contract qualification (kodmial/continuum#286).

One merge-blocking qualification contract with three layers:

- Layer 1 (regression gate): the full Continuum CI/contract suite runs on
  the exact PR HEAD. Evidence belongs to the exact current HEAD; absent,
  stale, or red evidence blocks every merge path (generic auto-merge and
  PR-Agent merge alike). Fail closed.
- Layer 2 (lifecycle integration): the real state-machine path
  ``issue -> scheduler/admission -> worker -> implementation PR -> CI ->
  review -> repair/re-review -> merge -> source issue reconciliation/close``
  is exercised end to end, proving liveness invariants (attributable
  ``automation:in-progress``, stale-lease recovery, no stranding on
  completed/failed/cancelled workers, duplicate coalescing,
  WAIT-vs-ERROR distinction, watchdog forward progress, exact-HEAD
  evidence, identical contract on both merge paths).
- Layer 3 (live canary): a disposable canary task exercises the same
  lifecycle against current ``main`` within bounded deadlines. Lack of
  forward progress records exact issue/PR/run IDs plus the stalled state,
  triggers bounded autonomous recovery where safe, and otherwise fails
  loudly.

Main-branch safety (server-side branch protection/ruleset requiring PRs,
required checks including this contract, no direct pushes/bypass) is
documented in ``docs/contract-qualification.md`` and enforced in code by
the exact-HEAD gates both merge reconcilers consult before any
``pulls.merge`` call.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Workflow name surfaced by the reusable contract-qualification workflow.
#: Both merge reconcilers require a successful run of exactly this workflow
#: for the exact PR HEAD. The name is part of the merge contract.
CONTRACT_WORKFLOW_NAME = "Contract qualification"

#: Workflow name of the primary regression suite (project-owned CI entry).
REGRESSION_WORKFLOW_NAME = "CI"

#: Lifecycle label holding an implementation reservation.
IN_PROGRESS_LABEL = "automation:in-progress"

#: Full commit identity required for every exact-HEAD decision.
_FULL_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}([0-9a-fA-F]{24})?\Z")

#: Lifecycle outcomes of :func:`evaluate_regression_gate`.
GATE_PASS = "pass"
GATE_BLOCK = "block"

#: WAIT (backpressure, retryable) vs ERROR (deterministic failure).
WAIT = "wait"
ERROR = "error"

#: Canary outcomes of :func:`evaluate_canary`.
CANARY_PROGRESS = "progress"
CANARY_STALLED_RECOVERABLE = "stalled-recoverable"
CANARY_STALLED_FAILED = "stalled-failed"

#: Bounded lifecycle deadlines (seconds) per stage for the canary. A stage
#: that exceeds its deadline without a forward-progress event is stalled.
CANARY_STAGE_DEADLINES: Dict[str, int] = {
    "admission": 15 * 60,
    "worker": 60 * 60,
    "ci": 30 * 60,
    "review": 60 * 60,
    "repair": 60 * 60,
    "merge": 30 * 60,
    "reconciliation": 15 * 60,
}

#: Ordered lifecycle stages for forward-progress accounting.
LIFECYCLE_STAGES: Tuple[str, ...] = (
    "admission",
    "worker",
    "ci",
    "review",
    "repair",
    "merge",
    "reconciliation",
)

#: Maximum bounded autonomous recovery attempts for a stalled canary or a
#: stranded reservation before failing loudly.
MAX_CANARY_RECOVERY_ATTEMPTS = 3


class ContractQualificationError(ValueError):
    """Raised when contract inputs cannot be interpreted safely."""


def normalize_sha(value: object) -> str:
    """Return the lowercased full commit id, or raise fail-closed."""
    text = str(value or "").strip().lower()
    if not _FULL_SHA_RE.match(text):
        raise ContractQualificationError(
            "head_sha must be a full hexadecimal commit id"
        )
    return text


def is_full_sha(value: object) -> bool:
    """Whether a value is usable as exact-HEAD identity (no raise)."""
    return bool(_FULL_SHA_RE.match(str(value or "").strip().lower()))


@dataclass(frozen=True)
class GateEvidence:
    """One workflow-run evidence record for a PR HEAD."""

    workflow_name: str
    head_sha: str
    status: str  # completed | in_progress | queued | waiting | ...
    conclusion: Optional[str] = None  # success | failure | skipped | ...


@dataclass(frozen=True)
class RegressionVerdict:
    """Deterministic Layer-1 verdict."""

    outcome: str  # pass | block
    reason: str


def _evidence_for_head(
    evidence: Sequence[GateEvidence], workflow_name: str, head_sha: str
) -> Optional[GateEvidence]:
    target = head_sha.strip().lower()
    candidates = [
        item
        for item in evidence
        if item.workflow_name == workflow_name
        and str(item.head_sha or "").strip().lower() == target
    ]
    # Newest-first is the caller's order; take the first exact-HEAD entry.
    # Stale evidence for any other SHA never satisfies the gate.
    return candidates[0] if candidates else None


def _has_any_evidence(
    evidence: Sequence[GateEvidence], workflow_name: str
) -> bool:
    return any(item.workflow_name == workflow_name for item in evidence)


def evaluate_regression_gate(
    pr_head_sha: object,
    evidence: Sequence[GateEvidence],
    contract_workflow_name: str = CONTRACT_WORKFLOW_NAME,
    regression_workflow_name: str = REGRESSION_WORKFLOW_NAME,
) -> RegressionVerdict:
    """Layer-1 verdict: exact-HEAD regression + contract evidence.

    ``pass`` requires, for the exact current ``pr_head_sha``, both a
    completed ``success`` regression (CI) run and a completed ``success``
    contract-qualification run. Anything else — missing run, stale SHA,
    non-completed status, or non-success conclusion — returns ``block``
    with a fail-closed reason naming the deficient leg. No merge path may
    bypass this gate.
    """
    try:
        head = normalize_sha(pr_head_sha)
    except ContractQualificationError:
        return RegressionVerdict(GATE_BLOCK, "invalid-pr-head")
    regression = _evidence_for_head(
        evidence, str(regression_workflow_name), head
    )
    if regression is None:
        if _has_any_evidence(evidence, str(regression_workflow_name)):
            return RegressionVerdict(GATE_BLOCK, "regression-evidence-stale")
        return RegressionVerdict(GATE_BLOCK, "regression-evidence-absent")
    if str(regression.head_sha or "").strip().lower() != head:
        return RegressionVerdict(GATE_BLOCK, "regression-evidence-stale")
    if regression.status != "completed" or regression.conclusion != "success":
        return RegressionVerdict(
            GATE_BLOCK,
            "regression-evidence-{}-{}".format(
                regression.status, regression.conclusion or "pending"
            ),
        )
    contract = _evidence_for_head(
        evidence, str(contract_workflow_name), head
    )
    if contract is None:
        if _has_any_evidence(evidence, str(contract_workflow_name)):
            return RegressionVerdict(GATE_BLOCK, "contract-evidence-stale")
        return RegressionVerdict(GATE_BLOCK, "contract-evidence-absent")
    if str(contract.head_sha or "").strip().lower() != head:
        return RegressionVerdict(GATE_BLOCK, "contract-evidence-stale")
    if contract.status != "completed" or contract.conclusion != "success":
        return RegressionVerdict(
            GATE_BLOCK,
            "contract-evidence-{}-{}".format(
                contract.status, contract.conclusion or "pending"
            ),
        )
    return RegressionVerdict(GATE_PASS, "exact-head-evidence-green")


def merge_path_allowed(
    pr_head_sha: object,
    evidence: Sequence[GateEvidence],
    review_provider: object = "none",
) -> Tuple[bool, str]:
    """Whether any supported merge path may merge this HEAD.

    Both the generic auto-merge path and the PR-Agent merge path obey the
    same qualification contract: the Layer-1 verdict must be ``pass``.
    ``review_provider`` is accepted for diagnostics only and never relaxes
    the gate — a ``pr-agent`` value still requires exact-HEAD evidence.
    """
    verdict = evaluate_regression_gate(pr_head_sha, evidence)
    if verdict.outcome != GATE_PASS:
        return False, verdict.reason
    provider = str(review_provider or "none").strip().lower()
    if provider not in ("none", "coderabbit", "pr-agent"):
        return False, "invalid-review-provider"
    return True, "contract-satisfied-for-{}".format(provider)


@dataclass(frozen=True)
class Reservation:
    """One ``automation:in-progress`` reservation on an issue."""

    issue_number: int
    has_label: bool
    live_run_id: Optional[int] = None
    live_pr_number: Optional[int] = None
    live_lease: bool = False
    lease_stale: bool = False
    worker_state: Optional[str] = None  # None | completed | failed | cancelled | running


@dataclass(frozen=True)
class LivenessVerdict:
    """Deterministic Layer-2 liveness verdict for one reservation."""

    outcome: str  # progress | wait | recover | error
    reason: str


def classify_wait_vs_error(
    worker_state: Optional[str], lease_stale: bool = False
) -> str:
    """WAIT (backpressure, retryable) vs ERROR (deterministic failure).

    Only an explicit running-ish worker state (``running``, ``queued``,
    ``in_progress``, ``waiting``, ``pending``) with a fresh lease is
    backpressure (WAIT). A stale lease, a terminal worker state, an
    absent worker state with no other live work, and any unknown state
    are ERROR signals that must release/recover the reservation instead
    of waiting silently.
    """
    state = str(worker_state or "").strip().lower() if worker_state else ""
    if state in ("running", "queued", "in_progress", "waiting", "pending"):
        return WAIT if not lease_stale else ERROR
    return ERROR


def evaluate_reservation_liveness(reservation: Reservation) -> LivenessVerdict:
    """Layer-2 liveness for one ``automation:in-progress`` reservation.

    - No label: ``progress`` (nothing to prove; normal scheduling owns it).
    - Label with an attributable live run/PR/lease: ``progress``.
    - Label with a stale lease: ``recover`` (release/recover without human
      intervention).
    - Label whose worker completed/failed/cancelled: ``recover`` — a
      terminal worker must never strand the issue.
    - Label with no attributable live work at all: ``error`` — a dead
      label that qualification must detect and fail loudly on.
    - Duplicate wakeups are coalesced by the caller onto one reservation;
      evaluating the single reservation twice yields the same verdict
      (idempotent), never duplicate work.
    """
    if not reservation.has_label:
        return LivenessVerdict("progress", "no-reservation")
    attributable = bool(
        reservation.live_run_id is not None
        or reservation.live_pr_number is not None
        or reservation.live_lease
    )
    worker = str(reservation.worker_state or "").strip().lower() if reservation.worker_state else ""
    terminal = worker in ("completed", "failed", "cancelled", "timed_out")
    if terminal:
        return LivenessVerdict(
            "recover", "terminal-worker-cannot-strand-issue:{}".format(worker or "unknown")
        )
    if reservation.lease_stale:
        return LivenessVerdict("recover", "stale-lease-released")
    if attributable:
        return LivenessVerdict("progress", "attributable-live-work")
    # No attributable live run/PR/lease. An explicit running-ish worker
    # state is backpressure (WAIT, still moving); anything else is a dead
    # label with no live work (ERROR, fail loudly).
    kind = classify_wait_vs_error(reservation.worker_state, reservation.lease_stale)
    if kind == WAIT:
        return LivenessVerdict("wait", "backpressure-distinct-from-error")
    return LivenessVerdict("error", "dead-label-without-live-work")


def coalesce_wakeups(wakeup_keys: Iterable[object]) -> List[str]:
    """Coalesce duplicate wakeups onto one key per distinct identity.

    Order-preserving deduplication: the first occurrence wins and later
    duplicates are dropped, so a burst of identical wakeups produces one
    unit of work instead of duplicating it.
    """
    seen: Dict[str, None] = {}
    for key in wakeups_keys_normalized(wakeup_keys):
        if key not in seen:
            seen[key] = None
    return list(seen.keys())


def wakeups_keys_normalized(keys: Iterable[object]) -> List[str]:
    """Normalize wakeup identities for :func:`coalesce_wakeups`."""
    normalized: List[str] = []
    for key in keys or []:
        text = str(key or "").strip().lower()
        if text:
            normalized.append(text)
    return normalized


@dataclass(frozen=True)
class WatchdogDecision:
    """Bounded autonomous watchdog/recovery decision."""

    action: str  # advance | retry | exhaust
    attempt: Optional[int]
    reason: str


def watchdog_advance(
    stalled: bool,
    attempt: int,
    max_attempts: int = MAX_CANARY_RECOVERY_ATTEMPTS,
) -> WatchdogDecision:
    """Watchdog moves stalled work forward without human intervention.

    Non-stalled work advances immediately. Stalled work retries with a
    bounded attempt budget; an exhausted budget fails loudly (``exhaust``)
    instead of silently stranding the issue.
    """
    if not stalled:
        return WatchdogDecision("advance", None, "work-is-moving")
    try:
        index = int(attempt)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        index = 0
    if index >= max_attempts:
        return WatchdogDecision("exhaust", int(max_attempts), "recovery-budget-exhausted-fail-loudly")
    return WatchdogDecision("retry", int(index + 1), "bounded-autonomous-recovery")


@dataclass(frozen=True)
class CanaryState:
    """One live-canary lifecycle observation."""

    canary_id: str
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    run_id: Optional[int] = None
    stage: str = "admission"
    stage_elapsed_seconds: int = 0
    recovery_attempts: int = 0


@dataclass(frozen=True)
class CanaryVerdict:
    """Deterministic Layer-3 canary verdict."""

    outcome: str  # progress | stalled-recoverable | stalled-failed
    reason: str
    detail: str


def canary_stage_deadline(stage: object) -> Optional[int]:
    """Bounded deadline (seconds) for one lifecycle stage, if known."""
    return CANARY_STAGE_DEADLINES.get(str(stage or "").strip().lower())


def evaluate_canary(state: CanaryState) -> CanaryVerdict:
    """Layer-3 verdict: bounded forward progress on current ``main``.

    - Within deadline: ``progress``.
    - Past deadline with remaining recovery budget: ``stalled-recoverable``
      (trigger bounded autonomous recovery).
    - Past deadline with exhausted budget: ``stalled-failed`` — fail
      loudly, recording exact canary/issue/PR/run IDs and the stalled
      stage so a human can reconstruct the stall.
    - Unknown stages fail closed to ``stalled-failed``.
    """
    stage = str(state.stage or "").strip().lower()
    deadline = canary_stage_deadline(stage)
    identity = "canary={} issue={} pr={} run={} stage={}".format(
        state.canary_id or "<unknown>",
        state.issue_number if state.issue_number is not None else "-",
        state.pr_number if state.pr_number is not None else "-",
        state.run_id if state.run_id is not None else "-",
        stage or "<unknown>",
    )
    if deadline is None:
        return CanaryVerdict(
            CANARY_STALLED_FAILED,
            "unknown-stage-fails-closed",
            identity,
        )
    try:
        elapsed = int(state.stage_elapsed_seconds)
    except (TypeError, ValueError):
        elapsed = deadline + 1
    if elapsed < 0:
        elapsed = deadline + 1
    if elapsed <= deadline:
        return CanaryVerdict(CANARY_PROGRESS, "forward-progress-within-deadline", identity)
    if int(state.recovery_attempts or 0) < MAX_CANARY_RECOVERY_ATTEMPTS:
        return CanaryVerdict(
            CANARY_STALLED_RECOVERABLE,
            "no-progress-past-deadline-bounded-recovery",
            identity,
        )
    return CanaryVerdict(
        CANARY_STALLED_FAILED,
        "no-progress-past-deadline-budget-exhausted",
        identity,
    )


def lifecycle_path_stages() -> Tuple[str, ...]:
    """The authoritative end-to-end lifecycle stage order."""
    return LIFECYCLE_STAGES


def progress_events_cover_lifecycle(events: Mapping[str, object]) -> Tuple[bool, str]:
    """Whether observed progress events cover the full lifecycle path."""
    missing = [stage for stage in LIFECYCLE_STAGES if not events.get(stage)]
    if missing:
        return False, "missing-stages:{}".format(",".join(missing))
    return True, "full-lifecycle-covered"
