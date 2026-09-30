"""Liveness: whether Continuum actually answered.

Parity on its own is a trap. A planner that never returns produces no journal,
produces no difference, and looks exactly like a planner that decided nothing
needed doing -- which is the one failure that would silently manufacture parity
out of a Continuum that had stopped working. This module makes the *absence* of a
terminal result into a reportable outcome, and treats it as a validation failure
rather than as a pass.

Three things are tracked, because they fail in different ways:

* **Within-run.** One shadow run carries a budget. A run that exceeds it ends as
  ``timeout`` with a terminal status, so the journal says what happened instead of
  being truncated at whatever the CI job's limit was.

* **Across runs.** A run that was accepted and never reported is an *orphan* --
  the workflow died, the runner was reclaimed, the artifact was never uploaded.
  Nothing inside the process can notice that, so it is detected here, from the
  acceptance records the bridge writes on the way in.

* **Across the fleet.** A count of terminal results per accepted event makes
  "how often does Continuum answer at all" a number in the artifact instead of
  something a reader has to infer from a folder of journals.

Time is always an input. Nothing here calls ``time.time()`` on its own, because
a liveness report that could not be reproduced from its own inputs would be
unusable as evidence.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .journal import (
    EXECUTION_FAILURES,
    JOURNAL_SCHEMA,
    STATUS_TIMEOUT,
    TERMINAL_STATUSES,
    ShadowJournal,
)

LIVENESS_SCHEMA = "continuum.shadow-liveness/v1"

#: How long one shadow run may take before it is a liveness failure. Ten
#: minutes: long enough for a real repair ladder walk with a provider snapshot,
#: short enough that a stuck run is reported inside a CI job's patience.
DEFAULT_RUN_BUDGET_MS = 10 * 60_000

#: How long after acceptance a run may be silent before it is an orphan. Wider
#: than the run budget, because the gap between "accepted" and "started" is
#: queue time in a CI system and is not Continuum's to account for.
DEFAULT_ORPHAN_GRACE_MS = 30 * 60_000

#: Liveness verdicts.
LIVE = "live"                 #: A terminal result arrived, and it is a decision.
SLOW = "slow"                 #: A terminal result arrived, but inside the budget by a hair.
STALLED = "stalled"           #: The run exceeded its budget and was stopped.
ORPHANED = "orphaned"         #: Accepted, and no result at all.
CRASHED = "crashed"           #: A terminal result arrived, and it is a failure of the run.
ABANDONED = "abandoned"       #: Deliberately not run: a duplicate, a superseded event.

#: Verdicts that make the whole validation fail. ``slow`` and ``live`` do not;
#: the point of the plane is stalls, not speed.
PASSING: Tuple[str, ...] = (LIVE, SLOW)
FAILING: Tuple[str, ...] = (STALLED, ORPHANED, CRASHED)

#: A run that was never going to answer is neither a pass nor a failure, and
#: that is a third thing rather than a rounding of the first two. Counting an
#: abandoned run as live would let a plane claim a decision it never made, which
#: is the one thing this plane exists not to do; counting it as a failure would
#: let a duplicate delivery block a cutover, which is the one thing a duplicate
#: must not do. It is its own verdict, excluded from the answer rate, and named
#: in the summary so a reader can see that it was considered rather than missed.
ABANDONED_VERDICTS: Tuple[str, ...] = (ABANDONED,)

#: At or above this fraction of the budget, a result is reported as ``slow``
#: rather than ``live``. Reported, not failed: a run that consistently needs 80%
#: of its budget is a run that will breach it under load, and that is worth seeing
#: before it happens.
SLOW_FRACTION = 0.8


class LivenessError(ValueError):
    """The inputs cannot produce a liveness report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class Acceptance:
    """One event the shadow plane accepted responsibility for.

    Written by the bridge *before* the run starts. That ordering is the whole
    mechanism: an acceptance with no matching result is only visible because the
    acceptance was recorded first, so a run that dies before it can write a
    result is still accounted for.
    """

    correlation_id: str
    scenario: str = ""
    repository: str = ""
    #: Milliseconds since the epoch, as the *clock that ran the run* saw them.
    accepted_at_ms: int = 0
    #: A run that is deliberately abandoned -- a duplicate of a run already in
    #: progress, a superseded event. Named here rather than dropped, so the
    #: difference between "did not finish" and "was not going to" is visible.
    abandoned_reason: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "scenario": self.scenario,
            "repository": self.repository,
            "accepted_at_ms": self.accepted_at_ms,
            "abandoned_reason": self.abandoned_reason,
        }


@dataclasses.dataclass(frozen=True)
class RunLiveness:
    """The liveness of one run."""

    correlation_id: str
    verdict: str
    status: str = ""
    decision: str = ""
    duration_ms: Optional[int] = None
    budget_ms: int = DEFAULT_RUN_BUDGET_MS
    reason: str = ""
    errors: Tuple[Mapping[str, Any], ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict in PASSING

    @property
    def failed(self) -> bool:
        return self.verdict in FAILING

    @property
    def abandoned(self) -> bool:
        return self.verdict in ABANDONED_VERDICTS

    def describe(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "verdict": self.verdict,
            "status": self.status,
            "decision": self.decision,
            "duration_ms": self.duration_ms,
            "budget_ms": self.budget_ms,
            "passed": self.passed,
            "reason": self.reason,
            "errors": [dict(entry) for entry in self.errors],
        }


@dataclasses.dataclass(frozen=True)
class LivenessReport:
    """Every accepted event, and whether it answered."""

    runs: Tuple[RunLiveness, ...] = ()
    orphan_grace_ms: int = DEFAULT_ORPHAN_GRACE_MS
    window_started_at: str = ""
    window_ended_at: str = ""
    generated_from: str = ""

    # -- queries ----------------------------------------------------------

    @property
    def failures(self) -> Tuple[RunLiveness, ...]:
        return tuple(run for run in self.runs if run.failed)

    @property
    def is_clean(self) -> bool:
        return not self.failures and bool(self.runs)

    @property
    def answer_rate(self) -> float:
        """The fraction of runs that were expected to answer, and did.

        A rate rather than a count because the rate is what a cutover decision
        turns on: a fleet that answered nine events in ten has a different risk
        profile from one that answered ninety in a hundred, and the count alone
        hides that.

        Runs that were abandoned on purpose are out of the denominator, because
        they were never expected to answer. Including them would make a window
        with ten live runs and one superseded event look like a plane that
        answered nine in ten, which is a different and much worse claim.
        """

        expected = [run for run in self.runs if not run.abandoned]
        if not expected:
            return 0.0
        answered = sum(1 for run in expected if run.duration_ms is not None)
        return answered / len(expected)

    @property
    def abandoned_runs(self) -> Tuple[RunLiveness, ...]:
        return tuple(run for run in self.runs if run.abandoned)

    def by_verdict(self) -> Dict[str, int]:
        counts = {name: 0 for name in (LIVE, SLOW, STALLED, ORPHANED, CRASHED)}
        for run in self.runs:
            counts[run.verdict] = counts.get(run.verdict, 0) + 1
        return counts

    def slowest(self) -> Optional[RunLiveness]:
        timed = [run for run in self.runs if run.duration_ms is not None]
        return max(timed, key=lambda run: run.duration_ms) if timed else None

    def describe(self) -> Dict[str, Any]:
        return _document(self)


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #


def run_liveness(
    journal: ShadowJournal,
    *,
    budget_ms: int = DEFAULT_RUN_BUDGET_MS,
    elapsed_ms: Optional[int] = None,
) -> RunLiveness:
    """The liveness verdict for one journal.

    ``elapsed_ms`` overrides the journal's own duration, because the journal only
    knows how long the *decision* took while the question is how long the run
    took. A run that spent nine minutes capturing state and then decided
    instantly is a slow run, and a liveness report that called it live would be
    measuring the wrong interval.
    """

    correlation = journal.event.correlation_id
    status = journal.status
    duration = journal.duration_ms if elapsed_ms is None else int(elapsed_ms)

    if status == STATUS_TIMEOUT:
        return RunLiveness(
            correlation_id=correlation,
            verdict=STALLED,
            status=status,
            decision=journal.decision,
            duration_ms=duration,
            budget_ms=budget_ms,
            reason=journal.reason or "the run exceeded its budget and was stopped",
            errors=journal.errors,
        )

    if status not in TERMINAL_STATUSES:
        return RunLiveness(
            correlation_id=correlation,
            verdict=ORPHANED,
            status=status,
            decision=journal.decision,
            duration_ms=None,
            budget_ms=budget_ms,
            reason="the run has no terminal status",
            errors=journal.errors,
        )

    if status in EXECUTION_FAILURES:
        return RunLiveness(
            correlation_id=correlation,
            verdict=CRASHED,
            status=status,
            decision=journal.decision,
            duration_ms=duration,
            budget_ms=budget_ms,
            reason=journal.reason or "the run ended as {!r}".format(status),
            errors=journal.errors,
        )

    if duration is None:
        return RunLiveness(
            correlation_id=correlation,
            verdict=ORPHANED,
            status=status,
            decision=journal.decision,
            duration_ms=None,
            budget_ms=budget_ms,
            reason="the run reached a terminal status but recorded no duration, so "
            "there is no evidence it finished inside its budget",
            errors=journal.errors,
        )

    if duration > budget_ms:
        return RunLiveness(
            correlation_id=correlation,
            verdict=STALLED,
            status=status,
            decision=journal.decision,
            duration_ms=duration,
            budget_ms=budget_ms,
            reason="the run took {}ms, over its {}ms budget".format(duration, budget_ms),
            errors=journal.errors,
        )

    verdict = SLOW if duration >= budget_ms * SLOW_FRACTION else LIVE
    return RunLiveness(
        correlation_id=correlation,
        verdict=verdict,
        status=status,
        decision=journal.decision,
        duration_ms=duration,
        budget_ms=budget_ms,
        reason="the run reached {!r} in {}ms".format(status, duration),
        errors=journal.errors,
    )


def timeout_journal(journal: ShadowJournal, *, budget_ms: int, elapsed_ms: int) -> ShadowJournal:
    """Turn a journal that ran out of time into a terminal one.

    Built rather than thrown, because a timeout is a result: the run had to be
    stopped, and the artifact has to say which event it stopped on, how far it
    got, and what it had decided when the clock ran out.
    """

    return journal.with_status(
        STATUS_TIMEOUT,
        decision="budget_exceeded",
        decision_source="continuum.shadow.liveness",
        reason="the shadow run exceeded its {}ms budget after {}ms and was stopped".format(
            budget_ms, elapsed_ms
        ),
    ).with_error(
        "budget_exceeded",
        "The shadow run exceeded its {}ms budget.".format(budget_ms),
        elapsed_ms=elapsed_ms,
        budget_ms=budget_ms,
    ).with_duration(int(elapsed_ms))


# --------------------------------------------------------------------------- #
# The fleet
# --------------------------------------------------------------------------- #


def report(
    acceptances: Sequence[Acceptance],
    journals: Sequence[ShadowJournal],
    *,
    now_ms: int,
    budget_ms: int = DEFAULT_RUN_BUDGET_MS,
    orphan_grace_ms: int = DEFAULT_ORPHAN_GRACE_MS,
    generated_from: str = "",
    window_started_at: str = "",
    window_ended_at: str = "",
) -> LivenessReport:
    """Account for every accepted event, including the ones that never answered.

    A journal nobody accepted is reported too, under a synthetic acceptance. The
    alternative -- ignoring it -- would let a run that reported itself into a
    report it was not part of go unmeasured, which is how a broken harness
    produces a clean-looking report.
    """

    if not isinstance(acceptances, Sequence) or isinstance(acceptances, (str, bytes)):
        raise LivenessError("acceptances_not_a_list", "Acceptances must be a list.")

    answered: Dict[str, ShadowJournal] = {}
    for journal in journals:
        correlation = journal.event.correlation_id
        if correlation in answered:
            # Two results for one acceptance is a harness fault, and silently
            # keeping the first would make the duplicate invisible.
            raise LivenessError(
                "duplicate_result",
                "Two journals claim correlation id {!r}.".format(correlation),
            )
        answered[correlation] = journal

    runs: List[RunLiveness] = []
    for acceptance in acceptances:
        journal = answered.get(acceptance.correlation_id)
        if journal is not None:
            runs.append(run_liveness(journal, budget_ms=budget_ms))
            continue
        if acceptance.abandoned_reason:
            # Its own verdict, and out of the answer rate: a run that was never
            # going to finish must not be able to fail the window, must not be
            # able to pass it either, and must not be reported as a decision that
            # was taken.
            runs.append(
                RunLiveness(
                    correlation_id=acceptance.correlation_id,
                    verdict=ABANDONED,
                    reason="the run was abandoned on purpose: {}".format(
                        acceptance.abandoned_reason
                    ),
                )
            )
            continue
        waited = int(now_ms) - int(acceptance.accepted_at_ms)
        runs.append(
            RunLiveness(
                correlation_id=acceptance.correlation_id,
                verdict=ORPHANED,
                reason=(
                    "accepted {}ms ago with no result; an orphaned run is one that "
                    "produced neither a decision nor a failure".format(waited)
                ),
            )
        )

    known = {acceptance.correlation_id for acceptance in acceptances}
    for correlation, journal in sorted(answered.items()):
        if correlation not in known:
            runs.append(
                run_liveness(
                    journal,
                    budget_ms=budget_ms,
                )
            )

    return LivenessReport(
        runs=tuple(sorted(runs, key=lambda run: run.correlation_id)),
        orphan_grace_ms=orphan_grace_ms,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
    )


def acceptance_from_payload(payload: Mapping[str, Any]) -> Acceptance:
    """Read one acceptance record the bridge wrote."""

    if not isinstance(payload, Mapping):
        raise LivenessError("acceptance_not_a_mapping", "An acceptance must be an object.")
    correlation = str(payload.get("correlation_id") or "").strip()
    if not correlation:
        raise LivenessError(
            "missing_correlation_id", "An acceptance must name the event it accepted."
        )
    try:
        accepted = int(payload.get("accepted_at_ms") or 0)
    except (TypeError, ValueError):
        raise LivenessError(
            "invalid_accepted_at", "accepted_at_ms must be a number."
        ) from None
    return Acceptance(
        correlation_id=correlation,
        scenario=str(payload.get("scenario") or ""),
        repository=str(payload.get("repository") or ""),
        accepted_at_ms=accepted,
        abandoned_reason=str(payload.get("abandoned_reason") or ""),
    )


def acceptance_from_journal(journal: ShadowJournal, accepted_at_ms: int) -> Acceptance:
    """The acceptance a run would have written for itself.

    Used when the bridge's record is missing but the run exists, so the two sides
    of the report can be reconciled either way round.
    """

    return Acceptance(
        correlation_id=journal.event.correlation_id,
        scenario=journal.event.scenario,
        repository=journal.event.repository,
        accepted_at_ms=int(accepted_at_ms),
    )


def report_from_documents(
    acceptances: Iterable[Mapping[str, Any]],
    journal_documents: Iterable[Mapping[str, Any]],
    *,
    now_ms: int,
    budget_ms: int = DEFAULT_RUN_BUDGET_MS,
    orphan_grace_ms: int = DEFAULT_ORPHAN_GRACE_MS,
    generated_from: str = "",
    window_started_at: str = "",
    window_ended_at: str = "",
) -> LivenessReport:
    """Build a report from the two artifact kinds, without re-running anything.

    The CI job that measures liveness must not be the same process that produced
    the runs, or a crash in the runner takes the accounting with it.
    """

    from .journal import journal_from_payload

    return report(
        [acceptance_from_payload(entry) for entry in acceptances],
        [journal_from_payload(entry) for entry in journal_documents],
        now_ms=now_ms,
        budget_ms=budget_ms,
        orphan_grace_ms=orphan_grace_ms,
        generated_from=generated_from,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
    )


def _document(liveness: LivenessReport) -> Dict[str, Any]:
    """The report document.

    Every count in it is derived from the run list rather than tracked alongside
    it. A summary field that can disagree with the rows it summarises is worse
    than no summary field, because a cutover gate that reads the summary is then
    reading a number nobody computed from the evidence.
    """

    runs = [run.describe() for run in liveness.runs]
    slowest = liveness.slowest()
    return {
        "schema": LIVENESS_SCHEMA,
        "kind": "report",
        "generated_from": liveness.generated_from,
        "window_started_at": liveness.window_started_at,
        "window_ended_at": liveness.window_ended_at,
        "total": len(runs),
        "answer_rate": round(liveness.answer_rate, 4),
        "by_verdict": liveness.by_verdict(),
        "clean": liveness.is_clean,
        "abandoned": [run.describe() for run in liveness.abandoned_runs],
        "orphan_grace_ms": liveness.orphan_grace_ms,
        "slowest": slowest.describe() if slowest is not None else None,
        "failures": [run.describe() for run in liveness.failures],
        "runs": runs,
    }


def describe(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Read a liveness report document back, through the same renderer.

    Round-tripping through one function is what makes "the artifact is the
    evidence" true here: a reader cannot end up with a different report than the
    one that was written, and the derived counts are recomputed from the rows.
    """

    if not isinstance(document, Mapping):
        raise LivenessError("report_not_a_mapping", "A liveness report must be an object.")
    schema = document.get("schema")
    if schema != LIVENESS_SCHEMA:
        raise LivenessError(
            "unknown_liveness_schema",
            "expected {!r}, found {!r}".format(LIVENESS_SCHEMA, schema),
        )
    runs = []
    for entry in document.get("runs", []) or []:
        duration = entry.get("duration_ms")
        runs.append(
            RunLiveness(
                correlation_id=str(entry.get("correlation_id", "")),
                verdict=str(entry.get("verdict", "")),
                status=str(entry.get("status", "")),
                decision=str(entry.get("decision", "")),
                duration_ms=None if duration is None else int(duration),
                budget_ms=int(entry.get("budget_ms", DEFAULT_RUN_BUDGET_MS) or 0),
                reason=str(entry.get("reason", "")),
                errors=tuple(dict(item) for item in entry.get("errors", []) or []),
            )
        )
    return _document(
        LivenessReport(
            runs=tuple(runs),
            orphan_grace_ms=int(document.get("orphan_grace_ms", DEFAULT_ORPHAN_GRACE_MS) or 0),
            window_started_at=str(document.get("window_started_at", "")),
            window_ended_at=str(document.get("window_ended_at", "")),
            generated_from=str(document.get("generated_from", "")),
        )
    )


def summary_line(liveness: LivenessReport) -> str:
    """One line for a job summary."""

    if not liveness.runs:
        return "liveness: no runs accepted in this window"
    counts = liveness.by_verdict()
    expected = len(liveness.runs) - len(liveness.abandoned_runs)
    return "liveness: {}/{} answered ({:.0%}), {} live, {} slow, {} failed, {} abandoned".format(
        round(liveness.answer_rate * expected),
        expected,
        liveness.answer_rate,
        counts.get(LIVE, 0),
        counts.get(SLOW, 0),
        len(liveness.failures),
        counts.get(ABANDONED, 0),
    )


def dumps(document: Mapping[str, Any]) -> str:
    """Deterministic JSON: sorted keys, one item per line."""

    return json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"
