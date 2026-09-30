"""The shadow planner: run the real decision path, record what it decided.

This module is where the issue's "same decision engine" requirement is met, and
the interesting part is what that does *not* mean.

It does not mean reimplementing the decisions in a shadow-friendly shape. It
does not mean calling the decision functions with ``apply=False`` and reporting
the result, because ``apply=False`` stops the controller immediately *after* the
decision and therefore never exercises what happens around a write -- recording
the in-flight slot, rolling a dispatch back when the slot could not be recorded,
turning a lock into a hold. Those are decisions too, and they are where the two
implementations are most likely to differ.

It means the decision functions are called the way production calls them, with
the only substitution being the object that receives the effects. So:

* ``queue_controller.reconcile`` runs with ``apply=True`` against
  :class:`~continuum.shadow.state.ShadowGitHubClient`;
* ``trust_policy.evaluate_event``, ``evaluate_merge`` are called with the
  captured state;
* ``conflict_repair.evaluate_plan``, ``evaluate_lock``, ``evaluate_watchdog``
  are called with the observed repair state;
* ``gate.run_gate`` is called with ``apply=True``, so the tracker comment, the
  review, and the status all land on the recorder;
* ``ReleaseCore.plan`` is called with record-only ports.

Every one of those returns a decision the journal records verbatim -- the policy
code, the queue action, the recovery rung, the release stage outcome. A parity
report comparing Continuum to NanoDictate is therefore comparing two real
decisions, and a divergence points at a specific function rather than at "the
shadow".
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..config import ContinuumConfig
from . import engine, release_ports
from .config import ShadowConfig
from .effects import Effect, RecordingEffects, WriteBarrierViolation
from .event import (
    TYPE_CI_COMPLETED,
    TYPE_DUPLICATE,
    TYPE_ISSUE_CLOSED,
    TYPE_ISSUE_LABELLED,
    TYPE_ISSUE_OPENED,
    TYPE_PR_OPENED,
    TYPE_PR_REVIEW,
    TYPE_PR_REVIEW_COMMENT,
    TYPE_PR_SYNCHRONIZE,
    TYPE_RELEASE_EVENT,
    TYPE_SCHEDULE,
    TYPE_TIMEOUT,
    TYPE_WORKFLOW_RUN_COMPLETED,
    ShadowEvent,
)
from .journal import (
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_NO_ACTION,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_TIMEOUT,
    Guard,
    ShadowJournal,
)
from .state import ObservedState, ShadowGitHubClient, StateError

#: Scenario classes the issue requires evidence for. The planner dispatches on
#: these rather than on event types directly, so a new event type has to be
#: assigned a class before coverage can be reported.
SCENARIO_ISSUE_LIFECYCLE = "issue-lifecycle"
SCENARIO_CI_REPAIR = "ci-repair"
SCENARIO_CODERABBIT_FINDING = "coderabbit-finding"
SCENARIO_MERGE_DECISION = "merge-decision"
SCENARIO_DEPENDENCY_BLOCKED = "dependency-blocked"
SCENARIO_RELEASE_PLANNING = "release-planning"
SCENARIO_DUPLICATE_REPLAY = "duplicate-replay"
SCENARIO_TIMEOUT_RECOVERY = "timeout-recovery"

SCENARIO_CLASSES: Tuple[str, ...] = (
    SCENARIO_ISSUE_LIFECYCLE,
    SCENARIO_CI_REPAIR,
    SCENARIO_CODERABBIT_FINDING,
    SCENARIO_MERGE_DECISION,
    SCENARIO_DEPENDENCY_BLOCKED,
    SCENARIO_RELEASE_PLANNING,
    SCENARIO_DUPLICATE_REPLAY,
    SCENARIO_TIMEOUT_RECOVERY,
)

#: Which planner handles which event type. An event type that is not here cannot
#: be planned, and :func:`plan` refuses it rather than falling through to a
#: default that would report a decision nobody made.
PLANNERS: Dict[str, str] = {
    TYPE_ISSUE_OPENED: SCENARIO_ISSUE_LIFECYCLE,
    TYPE_ISSUE_CLOSED: SCENARIO_ISSUE_LIFECYCLE,
    TYPE_PR_OPENED: SCENARIO_ISSUE_LIFECYCLE,
    TYPE_PR_SYNCHRONIZE: SCENARIO_ISSUE_LIFECYCLE,
    TYPE_CI_COMPLETED: SCENARIO_CI_REPAIR,
    TYPE_WORKFLOW_RUN_COMPLETED: SCENARIO_CI_REPAIR,
    TYPE_PR_REVIEW: SCENARIO_CODERABBIT_FINDING,
    TYPE_PR_REVIEW_COMMENT: SCENARIO_CODERABBIT_FINDING,
    TYPE_SCHEDULE: SCENARIO_MERGE_DECISION,
    TYPE_RELEASE_EVENT: SCENARIO_RELEASE_PLANNING,
    TYPE_DUPLICATE: SCENARIO_DUPLICATE_REPLAY,
    TYPE_TIMEOUT: SCENARIO_TIMEOUT_RECOVERY,
    TYPE_ISSUE_LABELLED: SCENARIO_DEPENDENCY_BLOCKED,
}

#: A re-delivery can only carry a real webhook event type back. A shadow
#: internal event (a replay of a replay, a watchdog tick) cannot be what GitHub
#: redelivered, and letting it through would recurse.
_REPLAYABLE_EVENT_TYPES = frozenset(PLANNERS) - {
    TYPE_DUPLICATE,
    TYPE_TIMEOUT,
    TYPE_SCHEDULE,
}


class PlanError(RuntimeError):
    """The event cannot be planned.

    Carries a code so the journal can record a rejection rather than a crash:
    an event the planner refuses is a finding about the bridge, and reporting it
    as a failed shadow run would blame Continuum for it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


#: Capture fields a decision cannot be made safely without.
#:
#: An allowlist, because the alternative -- refusing on anything unreadable --
#: would refuse every run in a repository whose review threads the client cannot
#: enumerate, which would make the plane report nothing rather than report
#: honestly. These three are the ones where a wrong default is a merge, a
#: publish, or a repair that should not have run.
_CAPTURE_REQUIRED_FOR_EFFECTS: Tuple[str, ...] = (
    "check_runs",
    "combined_status",
    "review_threads",
)


@dataclasses.dataclass
class _Run:
    """Accumulator for one shadow decision."""

    event: ShadowEvent
    state: ObservedState
    config: ShadowConfig
    effects: RecordingEffects
    client: ShadowGitHubClient
    guards: List[Guard] = dataclasses.field(default_factory=list)
    notes: Dict[str, Any] = dataclasses.field(default_factory=dict)
    now_ms: int = 0

    def guard(self, name: str, outcome: str, source: str, **detail: Any) -> None:
        self.guards.append(
            Guard(name=name, outcome=outcome, source=source, detail=detail)
        )

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = value


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def plan(
    event: ShadowEvent,
    state: ObservedState,
    config: ShadowConfig,
    *,
    origin: str = "bridge",
    now_ms: Optional[int] = None,
    barrier: Optional[Mapping[str, Any]] = None,
) -> ShadowJournal:
    """Decide what Continuum would have done about ``event``.

    Never raises for a decision failure. Every failure mode ends as a terminal
    journal status with the reason attached, because a shadow run that crashed
    without a journal is indistinguishable from a shadow run that was never
    started -- and the issue requires the difference to be reportable.
    """

    moment = int(now_ms if now_ms is not None else _now_ms())
    effects = RecordingEffects()
    journal = ShadowJournal(event=event, origin=origin).with_engine(
        engine.describe()
    ).with_config(config.describe()).with_observed(state)
    if barrier is not None:
        journal = journal.with_barrier(barrier)

    scenario = PLANNERS.get(event.event_type)
    if scenario is None:
        journal = journal.with_status(
            STATUS_REJECTED,
            decision="unplannable",
            decision_source="continuum.shadow.planner.PLANNERS",
            reason="no planner is registered for {!r}".format(event.event_type),
        ).with_error(
            "unplannable_event",
            "No shadow planner is registered for event type {!r}.".format(event.event_type),
        )
        return journal

    run = _Run(
        event=event,
        state=state,
        config=config,
        effects=effects,
        client=ShadowGitHubClient(state, effects, scenario=scenario),
        now_ms=moment,
    )
    run.note("scenario", scenario)
    run.note("capture_complete", state.is_complete())

    # A capture that could not read what a decision needs is refused before any
    # effect is planned. The reason to refuse rather than to proceed with a
    # default is asymmetric: a missing check run read as green, or missing
    # threads read as resolved, produces a merge that nobody reviewed and a
    # journal that looks identical to a clean one.
    unreadable = dict(getattr(state, "unreadable", {}) or {})
    run.guard(
        "capture",
        "complete" if not unreadable else "incomplete",
        "continuum.shadow.capture",
        fields=sorted(unreadable),
    )
    blocking = sorted(
        field
        for field in unreadable
        if any(
            field == prefix or field.startswith(prefix + ":")
            for prefix in _CAPTURE_REQUIRED_FOR_EFFECTS
        )
    )
    if blocking:
        run.guard(
            "capture_sufficient",
            "no",
            "continuum.shadow.capture",
            missing=blocking,
        )
        return (
            journal.with_status(
                "denied",
                decision="incomplete_capture",
                decision_source="continuum.shadow.capture",
                reason="the capture could not read {}: {}".format(
                    ", ".join(blocking),
                    "; ".join(unreadable[field] for field in blocking),
                ),
            )
            .with_error(
                "incomplete_capture",
                "The captured state is missing {}.".format(", ".join(blocking)),
                fields=blocking,
            )
            .with_guards(run.guards)
            .with_actions(run.effects.recorded(), run.effects.recorded())
            .with_duration(0)
            .with_note("capture_complete", False)
        )
    if unreadable:
        run.guard(
            "capture_sufficient",
            "yes",
            "continuum.shadow.capture",
            ignored=sorted(unreadable),
        )

    handler = _HANDLERS[scenario]
    started = time.monotonic()
    try:
        journal = handler(run, journal)
    except WriteBarrierViolation as violation:
        journal = journal.with_status(
            "blocked-write",
            decision="write-barrier",
            decision_source="continuum.shadow.barrier",
            reason=str(violation),
        ).with_error("write_barrier", str(violation))
    except (StateError, PlanError) as refused:
        journal = journal.with_status(
            STATUS_REJECTED,
            decision=getattr(refused, "code", "refused"),
            decision_source="continuum.shadow.planner",
            reason=getattr(refused, "message", str(refused)),
        ).with_error(
            getattr(refused, "code", "refused"),
            getattr(refused, "message", str(refused)),
        )
    except Exception as crash:  # noqa: BLE001 - a shadow run must always journal
        journal = journal.with_status(
            STATUS_FAILED,
            decision="crashed",
            decision_source="continuum.shadow.planner",
            reason="{}: {}".format(type(crash).__name__, crash),
        ).with_error(
            "planner_crashed",
            "{}: {}".format(type(crash).__name__, crash),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    if not journal.is_terminal:
        journal = journal.with_status(
            STATUS_NO_ACTION,
            decision="no-decision",
            decision_source="continuum.shadow.planner",
            reason="the planner returned without reaching a terminal decision",
        )
    journal = (
        journal.with_guards(run.guards)
        .with_actions(run.effects.recorded(), run.effects.recorded())
        .with_duration(duration_ms)
    )
    for key, value in run.notes.items():
        journal = journal.with_note(key, value)
    return journal


_HANDLERS: Dict[str, Callable[[_Run, ShadowJournal], ShadowJournal]] = {}


def _handler(scenario: str) -> Callable[[Callable[[_Run, ShadowJournal], ShadowJournal]], Callable[[_Run, ShadowJournal], ShadowJournal]]:
    def register(function: Callable[[_Run, ShadowJournal], ShadowJournal]) -> Callable[[_Run, ShadowJournal], ShadowJournal]:
        _HANDLERS[scenario] = function
        return function

    return register


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# issue -> worker / pull request lifecycle
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_ISSUE_LIFECYCLE)
def _plan_issue_lifecycle(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """A normal issue or pull request event, through the real queue controller.

    The trust policy runs first because that is the order production runs in:
    the scheduler refuses to spend a write credential on an event nobody
    authorised, and the queue only ever sees events that passed. Recording the
    refusal matters -- "Continuum would not have dispatched" is a parity claim.
    """

    from continuum.review import queue as queue_module

    event = run.event

    # -- guard: may privileged automation run for this event at all? -------
    source, code, reason = _authorization(run, event)
    allowed = not code
    run.guard(
        "trust.authorization",
        "allow" if allowed else "deny",
        source,
        code=code,
        reason=reason,
    )
    run.note("trust_code", code)
    run.note("trust_reason", reason)

    # -- guard: is this event a queue wake-up at all? ----------------------
    wake = queue_module.WakeUp(
        event=queue_module.normalise_event(_gh_event_name(event)),
        action=_gh_action(event),
        pr_number=event.number,
        reason="shadow of {} {}".format(event.event_type, event.action),
    )
    run.guard(
        "queue.wake_up",
        "relevant" if wake.is_queue_relevant else "ignored",
        "continuum.review.queue.is_queue_wake_up",
        event=wake.event,
        action=wake.action,
    )

    if not allowed:
        journal = journal.with_status(
            STATUS_DENIED,
            decision=code,
            decision_source=source,
            reason=reason,
        )
        # The refusal itself is an action the journal records, because "nothing
        # happened" and "the controller refused and said why" are different
        # reports of the same run.
        run.effects.record(
            Effect(
                kind="workflow.dispatch",
                target="workflow:{}".format(
                    run.config.config.review.queue.dispatch_workflow
                ),
                detail={"ref": run.state.base_ref, "inputs": {}, "refused": code},
            )
        )
        return journal

    if not wake.is_queue_relevant:
        return _not_a_wake_up(run, journal, wake)

    return _reconcile_queue(run, journal, wake)


def _not_a_wake_up(run: _Run, journal: ShadowJournal, wake: Any) -> ShadowJournal:
    """An event the queue is not triggered by.

    This is a real production outcome -- the workflow runs, the trigger list
    does not match, the job exits -- and it is *not* the same as the queue having
    run and found nothing. Both end as ``no-action``; the decision names which
    one it was, so a parity report can tell "Continuum would have stayed quiet
    for the same reason" from "Continuum would have woken and done nothing".
    """

    return journal.with_status(
        STATUS_NO_ACTION,
        decision="wake_up_not_relevant",
        decision_source="continuum.review.queue.is_queue_wake_up",
        reason="{} cannot change the next eligible candidate".format(wake.describe()),
    )


def _reconcile_queue(run: _Run, journal: ShadowJournal, wake: Any) -> ShadowJournal:
    """The real queue controller, with the real apply path.

    ``apply=True`` on purpose. The interesting behaviour of this controller is
    after the effect -- holding the provider slot, rolling the dispatch back when
    the slot could not be recorded -- and a dry run skips exactly that.
    """

    from continuum.review import queue_controller

    plan_result = queue_controller.reconcile(
        run.client,
        run.config.config,
        wake=wake,
        now=run.now_ms,
        apply=True,
        wait_ms=0,
    )

    run.guard(
        "queue.plan",
        plan_result.action,
        "continuum.review.queue.reconcile",
        dispatched=plan_result.dispatched,
        selected=plan_result.selected.pr_number if plan_result.selected else 0,
        queue=[candidate.pr_number for candidate in plan_result.queue],
        excluded=[
            {"pr": number, "reason": reason} for number, reason in plan_result.excluded
        ],
        held_lock=plan_result.held_lock.identity() if plan_result.held_lock else "",
        released_lock=(
            plan_result.released_lock.identity() if plan_result.released_lock else ""
        ),
        cooldown_ms=plan_result.cooldown.remaining_ms(run.now_ms),
    )
    run.note("queue_action", plan_result.action)
    run.note("queue_dispatched", plan_result.dispatched)
    run.note("queue_logs", list(plan_result.logs))

    for candidate in plan_result.queue:
        run.guard(
            "queue.eligibility.pr{}".format(candidate.pr_number),
            "eligible",
            "continuum.review.queue.ineligibility_reason",
            priority=candidate.priority,
            ci=candidate.ci,
            review=candidate.review,
            source_issue=candidate.source_issue or 0,
        )
    for number, reason in plan_result.excluded:
        run.guard(
            "queue.eligibility.pr{}".format(number),
            "ineligible",
            "continuum.review.queue.ineligibility_reason",
            reason=reason,
        )

    if plan_result.dispatched:
        selected = plan_result.selected
        run.guard(
            "queue.revalidation.pr{}".format(selected.pr_number),
            "still_eligible",
            "continuum.review.queue_controller.reconcile",
            head_sha=selected.head_sha,
        )
        return journal.with_status(
            STATUS_OK,
            decision=plan_result.action,
            decision_source="continuum.review.queue.reconcile",
            reason="the controller selected PR #{} and scheduled one provider "
            "request".format(selected.pr_number),
        )

    return journal.with_status(
        STATUS_NO_ACTION,
        decision=plan_result.action,
        decision_source="continuum.review.queue.reconcile",
        reason=_queue_reason(plan_result),
    )


def _queue_reason(plan_result: Any) -> str:
    if plan_result.held_lock is not None:
        return "PR #{} already holds the provider slot".format(plan_result.held_lock.pr_number)
    if plan_result.released_lock is not None:
        return "released the stale provider slot for PR #{}".format(
            plan_result.released_lock.pr_number
        )
    if plan_result.action == "wait":
        return "waiting out the provider cooldown"
    if plan_result.action == "disabled":
        return "review.provider is none, so the queue is disabled"
    return "no candidate is eligible"


# --------------------------------------------------------------------------- #
# CI failure -> repair
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_CI_REPAIR)
def _plan_ci_repair(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """A failing check, through the real conflict-repair ladder.

    ``evaluate_plan`` is the function the agent job's ``authorize`` step calls
    to decide whether it may start and with what budget, so calling it is
    calling the guard that gates the real repair.
    """

    repair = engine.conflict_repair()
    event = run.event
    pull = run.state.require_pull(event.number)

    behind = _behind(run, pull)
    attempts = _attempts(run, pull)
    in_flight = run.client.repair_in_flight(pull.number)
    ruled_out = _ruled_out(run, pull)

    run.guard(
        "repair.behind",
        "behind" if behind > 0 else "current",
        "continuum shadow: captured compare state",
        behind=behind,
    )

    ladder = repair.evaluate_plan(
        mode="ci-fix",
        behind=behind,
        ruled_out=ruled_out,
        attempts=attempts,
        max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
        seconds_since_attempt=None,
    )
    run.guard(
        "repair.plan",
        ladder.action or "task_execution",
        "conflict_repair.evaluate_plan",
        code=ladder.code,
        reason=ladder.reason,
        hold_lock=ladder.hold_lock,
        release_lock=ladder.release_lock,
        record_failure=ladder.record_failure,
        retryable=ladder.retryable,
        retry_after_seconds=ladder.retry_after_seconds,
        task_timeout_minutes=ladder.task_timeout_minutes,
        agent_timeout_seconds=ladder.agent_timeout_seconds,
        episode_attempts=ladder.episode_attempts,
    )
    run.note("repair_rung", ladder.action)
    run.note("repair_code", ladder.code)

    lock = repair.evaluate_lock(
        lock_present=repair.CONFLICT_LOCK_LABEL in pull.labels,
        attempts=attempts,
        max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
        repair_run_in_flight=in_flight,
    )
    run.guard(
        "repair.lock",
        lock.action,
        "conflict_repair.evaluate_lock",
        code=lock.code,
        reason=lock.reason,
        hold_lock=lock.hold_lock,
        release_lock=lock.release_lock,
        record_failure=lock.record_failure,
    )

    if lock.release_lock:
        run.effects.record(
            Effect(
                kind="label.remove",
                target="issue:{}".format(pull.number),
                detail={"label": repair.CONFLICT_LOCK_LABEL},
            )
        )
    elif lock.hold_lock:
        run.effects.record(
            Effect(
                kind="label.add",
                target="issue:{}".format(pull.number),
                detail={"labels": [repair.CONFLICT_LOCK_LABEL]},
            )
        )

    if ladder.action and ladder.action in repair.RECOVERY_RUNGS:
        # The dispatch the controller would send. Its inputs are the policy's,
        # not the planner's: mode comes from the ladder's rung, the head from
        # the captured pull request, and the ruled-out set from the episode.
        #
        # Production's dispatcher (``dispatchConflictRepair``) declines while a
        # repair is already active -- the lock label is present -- and adds the
        # lock before dispatching. That is what makes a re-delivered webhook a
        # no-op: the second delivery sees the first one's slot. A shadow run
        # that dispatched twice over the same record would be describing a
        # redelivery as two repairs, which is exactly the drift this field
        # exists to rule out.
        if in_flight or repair.CONFLICT_LOCK_LABEL in pull.labels:
            run.guard(
                "repair.dispatch",
                "declined",
                "continuum shadow: a repair is already active",
                in_flight=in_flight,
                lock_present=repair.CONFLICT_LOCK_LABEL in pull.labels,
            )
            run.note("dispatch", "declined")
        else:
            run.effects.record(
                Effect(
                    kind="label.add",
                    target="issue:{}".format(pull.number),
                    detail={"labels": [repair.CONFLICT_LOCK_LABEL]},
                )
            )
            run.effects.record(
                Effect(
                    kind="workflow.dispatch",
                    target="workflow:opencode.yml",
                    detail={
                        "ref": run.state.base_ref,
                        "inputs": {
                            "mode": "resolve-conflict",
                            "pr_number": str(pull.number),
                            "head_ref": pull.head_ref,
                            "rungs_ruled_out": ",".join(ruled_out) or ladder.action,
                        },
                    },
                )
            )
            run.client.mark_repair_dispatched(pull.number)
    elif ladder.action == repair.FAILED_ACTION:
        run.effects.record(
            Effect(
                kind="label.add",
                target="issue:{}".format(pull.number),
                detail={"labels": [repair.REPAIR_FAILED_LABEL]},
            )
        )
    elif ladder.action == "none":
        run.effects.record(
            Effect(
                kind="label.remove",
                target="issue:{}".format(pull.number),
                detail={"label": repair.CONFLICT_LOCK_LABEL},
            )
        )

    # -- the second consumer of this event --------------------------------
    #
    # ``workflow_run: CI completed`` starts two production workflows: the repair
    # watchdog and the merge sweep. A shadow run that only walked one of them
    # would validate half the program and then compare the half against
    # production's two halves -- which reads as a missing action every time the
    # merge controller did something. So the merge decision runs here too, on the
    # same captured evidence, and its effects land in the same journal.
    if event.event_type == TYPE_WORKFLOW_RUN_COMPLETED:
        _evaluate_merge_effects(run, pull)

    status = STATUS_OK if ladder.action not in ("", repair.FAILED_ACTION) else (
        STATUS_NO_ACTION if ladder.action in ("none", "wait") else STATUS_OK
    )
    return journal.with_status(
        status,
        decision=ladder.action or ladder.code,
        decision_source="conflict_repair.evaluate_plan",
        reason=ladder.reason,
    )


def _evaluate_merge_effects(run: _Run, pull: Any) -> None:
    """Run the real merge decision and record what it would do.

    The merge policy is the only thing that may claim ``pull.merge``, so it is
    asked directly rather than inferred. A denial records no effect: the refusal
    *is* the decision, and a recorded "did not merge" that parity then compared
    against production's actions would turn a correct refusal into a difference.
    """

    policy = engine.trust_policy()
    ci_green = _ci_is_green(run, pull)
    decision = policy.evaluate_merge(
        repository=run.state.repository,
        pull_request=pull.as_api(),
        changed_files=list(run.state.changed_files.get(pull.number, ())),
        ci_green=ci_green,
        base_ref=run.state.base_ref,
    )
    run.guard(
        "merge.ci_green",
        "green" if ci_green else "not-green",
        "continuum.shadow.planner._ci_is_green",
        head_sha=pull.head_sha,
    )
    run.guard(
        "merge.eligibility",
        "allow" if decision.allowed else "deny",
        "trust_policy.evaluate_merge",
        code=decision.code,
        reason=decision.reason,
        sensitive_paths=list(policy.trust_sensitive_changes(
            run.state.changed_files.get(pull.number, ())
        )),
    )
    run.note("merge_code", decision.code)
    if decision.allowed:
        run.effects.record(
            Effect(
                kind="pull.merge",
                target="pr:{}".format(pull.number),
                detail={"head": pull.head_sha, "method": "squash"},
            )
        )


# --------------------------------------------------------------------------- #
# CodeRabbit blocking finding -> repair -> re-review
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_CODERABBIT_FINDING)
def _plan_coderabbit_finding(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """A review verdict, through the real review gate.

    ``run_gate`` is called with ``apply=True`` and the record-only client, so
    the tracker comment, the review, and the commit status all land on the
    recorder. That is the whole path: collect the provider snapshot, derive the
    findings, decide the verdict, publish it. A gate that returned a verdict
    without publishing would not be the gate.
    """

    from continuum.review import gate as gate_module
    from continuum.review import queue as queue_module

    event = run.event
    pull = run.state.require_pull(event.number)
    settings = run.config.config.review

    run.guard(
        "review.provider",
        settings.provider,
        "continuum.config.ReviewSettings.enabled",
        bot_login=settings.coderabbit.bot_login,
        status_context=settings.coderabbit.status_context,
    )

    result = gate_module.run_gate(
        run.client,
        run.config.config,
        pull.number,
        head_sha=pull.head_sha,
        apply=True,
    )

    verdict = str(result.get("verdict") or "")
    run.guard(
        "review.verdict",
        verdict or "unknown",
        "continuum.review.gate.run_gate",
        findings=len(result.get("findings", ()) or ()),
        open_findings=result.get("open_findings", 0),
        head=result.get("head", ""),
        state=result.get("state", ""),
        coverage_complete=bool((result.get("coverage") or {}).get("complete", False)),
    )
    run.note("review_verdict", verdict)
    run.note("review_state", result.get("state", ""))
    run.note("review_open_findings", result.get("open_findings", 0))

    # A blocking verdict is the point where the production controllers hand the
    # work back to the agent. Whether Continuum would do the same is a second
    # decision, and it is taken with the repair ladder, not invented here.
    repair = engine.conflict_repair()
    blocking = verdict not in ("", "NONE", "APPROVED", "PASS")
    if blocking:
        ladder = repair.evaluate_plan(
            mode="resolve-conflict",
            behind=1,
            ruled_out=(),
            attempts=_attempts(run, pull),
            max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
            seconds_since_attempt=None,
        )
        run.guard(
            "review.repair_dispatch",
            ladder.action,
            "conflict_repair.evaluate_plan",
            code=ladder.code,
            reason=ladder.reason,
            episode_attempts=ladder.episode_attempts,
        )
        run.effects.record(
            Effect(
                kind="workflow.dispatch",
                target="workflow:opencode.yml",
                detail={
                    "ref": run.state.base_ref,
                    "inputs": {
                        "mode": "resolve-conflict",
                        "pr_number": str(pull.number),
                        "head_ref": pull.head_ref,
                    },
                },
            )
        )
    else:
        run.guard(
            "review.repair_dispatch",
            "none",
            "continuum.shadow.planner",
            reason="the verdict is not blocking, so no repair is dispatched",
        )

    wake = queue_module.WakeUp(
        event=queue_module.EVENT_PULL_REQUEST_REVIEW
        if event.event_type == TYPE_PR_REVIEW
        # A review *comment* is not a queue trigger. Production wakes on
        # `pull_request_review` (submitted, dismissed) and on `issue_comment`;
        # `pull_request_review_comment` is absent from ``WAKE_UP_ACTIONS``, so
        # widening it here would invent a wake-up the production workflow does
        # not have.
        else "pull_request_review_comment",
        action=event.action or "submitted",
        pr_number=pull.number,
        reason="shadow of {}".format(event.event_type),
    )
    run.guard(
        "queue.wake_up",
        "relevant" if wake.is_queue_relevant else "ignored",
        "continuum.review.queue.is_queue_wake_up",
        event=wake.event,
        action=wake.action,
    )

    return journal.with_status(
        STATUS_OK,
        decision=verdict or "unknown",
        decision_source="continuum.review.gate.run_gate",
        reason="the review gate decided {!r} for PR #{} at {}".format(
            verdict, pull.number, pull.head_sha[:12]
        ),
    )


# --------------------------------------------------------------------------- #
# merge eligibility
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_MERGE_DECISION)
def _plan_merge_decision(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """Merge eligibility, through ``trust_policy.evaluate_merge``.

    ``ci_green`` is computed by the planner from the captured check state, by
    the same rule the production controller uses: a *completed, same-repository,
    pull-request-evented* run for the exact head. The planner cannot be lenient
    about it, because ``evaluate_merge`` treats anything but ``True`` as not
    green, and a planner that passed a guess would make Continuum look stricter
    than it is.
    """

    policy = engine.trust_policy()
    event = run.event
    pull = run.state.require_pull(event.number)

    ci_green = _ci_is_green(run, pull)
    changed = list(run.state.changed_files.get(pull.number, ()))
    decision = policy.evaluate_merge(
        repository=run.state.repository,
        pull_request=pull.as_api(),
        changed_files=changed,
        ci_green=ci_green,
        base_ref=run.state.base_ref,
    )

    run.guard(
        "merge.ci_green",
        "green" if ci_green else "not-green",
        "continuum.shadow.planner._ci_is_green",
        head_sha=pull.head_sha,
        contexts={
            context: conclusion
            for context, conclusion in run.state.check_runs.get(pull.head_sha, ())
        },
        combined=run.state.combined_status.get(pull.head_sha, ""),
    )
    run.guard(
        "merge.eligibility",
        "allow" if decision.allowed else "deny",
        "trust_policy.evaluate_merge",
        code=decision.code,
        reason=decision.reason,
        pr_number=decision.pr_number,
        head_ref=decision.head_ref,
        sensitive_paths=list(policy.trust_sensitive_changes(changed)),
    )
    run.note("merge_code", decision.code)

    if decision.allowed:
        run.effects.record(
            Effect(
                kind="pull.merge",
                target="pr:{}".format(pull.number),
                detail={"head": pull.head_sha, "method": "squash"},
            )
        )
        return journal.with_status(
            STATUS_OK,
            decision=decision.code,
            decision_source="trust_policy.evaluate_merge",
            reason=decision.reason,
        )

    return journal.with_status(
        STATUS_DENIED,
        decision=decision.code,
        decision_source="trust_policy.evaluate_merge",
        reason=decision.reason,
    )


# --------------------------------------------------------------------------- #
# dependency / blocked task
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_DEPENDENCY_BLOCKED)
def _plan_dependency_blocked(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """A task held by a dependency, and the state that can outlive its cause.

    This is the scenario where a *correct* decision is "do nothing", so the
    interesting evidence is the reasoning rather than the effects. Three real
    decisions, in the order production makes them:

    1. the trust policy's issue gate -- the issue scheduler intersects the
       policy allowlist with its own filter and resolves disagreement in favour
       of the policy;
    2. the dependency check -- the scheduler skips an issue with any open
       blocker, read from GitHub's issue-dependency endpoint;
    3. the queue controller, because ``issues: labeled`` genuinely reorders the
       review queue even for an issue that cannot be worked yet.

    A blocked issue that is also a queue wake-up is the case where a shadow run
    that only checked one of them would get the wrong answer, which is why all
    three run.
    """

    from continuum.review import queue as queue_module

    event = run.event
    issue = run.state.require_issue(event.number)

    # -- guard 1: the issue gate ------------------------------------------
    source, code, reason = _authorization(run, event)
    allowed = not code
    run.guard(
        "trust.issue",
        "allow" if allowed else "deny",
        source,
        code=code,
        reason=reason,
        issue=issue.number,
        author=issue.author,
    )
    run.note("trust_code", code)

    # -- guard 2: dependencies --------------------------------------------
    blockers = run.state.open_blockers(issue.number)
    run.guard(
        "dependency.blockers",
        "blocked" if blockers else "clear",
        "issue-scheduler.yml: issues/dependencies/blocked_by",
        issue=issue.number,
        open_blockers=[blocker for blocker, _ in blockers],
        all_blockers=[list(item) for item in run.state.blocked_by.get(issue.number, ())],
    )
    run.note("open_blockers", [blocker for blocker, _ in blockers])

    if not allowed:
        return journal.with_status(
            STATUS_DENIED,
            decision=code,
            decision_source=source,
            reason=reason,
        )

    # -- guard 3: the queue, which this event genuinely reorders -----------
    wake = queue_module.WakeUp(
        event=queue_module.normalise_event(_gh_event_name(event)),
        action=_gh_action(event),
        pr_number=None,
        reason="shadow of {} {}".format(event.event_type, event.action),
    )
    run.guard(
        "queue.wake_up",
        "relevant" if wake.is_queue_relevant else "ignored",
        "continuum.review.queue.is_queue_wake_up",
        event=wake.event,
        action=wake.action,
    )
    if not wake.is_queue_relevant:
        return journal.with_status(
            STATUS_NO_ACTION,
            decision="wake_up_not_relevant",
            decision_source="continuum.review.queue.is_queue_wake_up",
            reason="{} cannot change the next eligible candidate".format(wake.describe()),
        )

    journal = _reconcile_queue(run, journal, wake)
    if blockers and journal.status == STATUS_OK:
        # The queue moved, but production would not have dispatched the issue
        # itself. Recording that keeps the two decisions separable in the
        # report instead of one hiding the other.
        journal = journal.with_note("dispatch_suppressed_by_dependency", True)
    return journal


# --------------------------------------------------------------------------- #
# release planning, dry run
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_RELEASE_PLANNING)
def _plan_release_planning(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """The release chain, walked for real in dry-run mode.

    ``ReleaseCore.plan`` is the production entry point for "what would a release
    do", and it is the whole eleven-stage chain rather than the stages the
    planner feels like including. The ports are record-only, so the chain
    completes and every publication is recorded as suppressed.
    """

    from continuum.release import core as release_core
    from continuum.release import state as release_state

    event = run.event
    release = event.release
    tag = str(release.get("tag", "") or "")
    source_sha = str(release.get("source_sha", "") or event.head_sha or "").lower()
    if not source_sha:
        # Fail closed with a stated reason rather than letting the release
        # contract raise. A contract error here is a real production
        # possibility, but a shadow run that reports it as "the planner crashed"
        # has lost the only thing worth knowing: the capture could not pin the
        # release to a commit, so no release decision is evaluable.
        return journal.with_status(
            STATUS_REJECTED,
            decision="missing_source_sha",
            decision_source="continuum.release.contract",
            reason="the captured release event names no source commit, and a "
            "release cannot be planned against an unpinned commit",
        ).with_error(
            "missing_source_sha",
            "A release event must carry release.source_sha when it has no head.",
        )

    target_ids = [str(name) for name in release.get("targets", ())] or ["shadow"]
    publishers = [str(name) for name in release.get("publishers", ())] or ["github-release"]

    component_set = release_ports.components(
        run.effects, target_ids=target_ids, publishers=publishers
    )
    request = release_ports.request(
        repository=event.repository,
        sha=source_sha,
        tag=tag,
        delivery=event.correlation_id,
        target_ids=target_ids,
    )

    outcome = release_core.ReleaseCore(component_set).plan(request)

    for stage_outcome in outcome.outcomes:
        run.guard(
            "release.stage.{}".format(stage_outcome.stage),
            stage_outcome.outcome,
            "continuum.release.core.ReleaseCore.plan",
            code=stage_outcome.code,
            summary=stage_outcome.summary,
            duplicate=stage_outcome.duplicate,
            target=stage_outcome.detail("target"),
            destination=stage_outcome.detail("destination"),
        )
    run.guard(
        "release.status",
        outcome.status,
        "continuum.release.core.ReleaseCore.plan",
        version=outcome.version,
        tag=outcome.tag,
        source_sha=outcome.source_sha,
        dry_run=outcome.dry_run,
        publications=release_ports.recorded_publications(outcome),
        no_op_reason=outcome.no_op_reason,
    )
    run.note("release_status", outcome.status)
    run.note("release_version", outcome.version)
    run.note("release_tag", outcome.tag)
    run.note("release_stages", [item.stage for item in outcome.outcomes])
    run.note("release_chain", list(release_ports.chain()))

    failed = next(
        (
            item
            for item in outcome.outcomes
            if item.outcome not in release_state.GREEN_OUTCOMES
        ),
        None,
    )
    if failed is not None:
        return journal.with_status(
            STATUS_FAILED,
            decision=failed.outcome,
            decision_source="continuum.release.core.ReleaseCore.plan",
            reason="{}: {}".format(failed.code, failed.summary),
        )
    if not outcome.ok:
        return journal.with_status(
            STATUS_NO_ACTION,
            decision=outcome.status,
            decision_source="continuum.release.core.ReleaseCore.plan",
            reason=outcome.no_op_reason or "the release chain reached {}".format(outcome.status),
        )
    return journal.with_status(
        STATUS_OK,
        decision=outcome.status,
        decision_source="continuum.release.core.ReleaseCore.plan",
        reason="the release chain planned {} for {}".format(outcome.version or "no version", tag or "no tag"),
    )


# --------------------------------------------------------------------------- #
# duplicate / replayed event
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_DUPLICATE_REPLAY)
def _plan_duplicate_replay(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """The same event delivered twice, and the decision the second time.

    Idempotency is not a property this handler can assert about itself. It is a
    property of the controller *plus* the state it left behind, so the test has
    to be a real first delivery followed by a real second one against the state
    the first left. Both run through the scenario the capture says was
    redelivered, on the same client, so the second delivery sees the first
    delivery's writes -- which is the whole mechanism idempotency rests on.

    The original event type comes from the capture, and its absence is a refusal
    rather than a guess: "was the redelivery idempotent" has no answer without
    knowing what was redelivered, and defaulting it to the queue path would
    manufacture an answer about an event nobody captured.
    """

    original = str(run.event.extra.get("original_event_type") or "").strip()
    if not original:
        return journal.with_status(
            STATUS_REJECTED,
            decision="missing_original_event_type",
            decision_source="continuum.shadow.planner",
            reason="a re-delivery cannot be tested for idempotency without the "
            "event type that was re-delivered",
        ).with_error(
            "missing_original_event_type",
            "A duplicate.replay event must carry extra.original_event_type.",
        )

    if original not in _REPLAYABLE_EVENT_TYPES:
        return journal.with_status(
            STATUS_REJECTED,
            decision="unknown_original_event_type",
            decision_source="continuum.shadow.planner",
            reason="{!r} is not an event type a webhook re-delivery can carry".format(original),
        )
    run.note("original_event_type", original)
    scenario = PLANNERS[original]
    run.note("original_scenario", scenario)

    handler = _HANDLERS[scenario]
    first = handler(run, journal)
    first_effects = tuple(effect.signature for effect in run.effects.recorded())
    run.note("first_status", first.status)

    # The second delivery, on the same client, so the first one's writes are
    # part of what it reads.
    second = handler(run, first)
    all_effects = tuple(effect.signature for effect in run.effects.recorded())
    new_effects = all_effects[len(first_effects):]

    run.guard(
        "replay.idempotency",
        "no-new-effects" if not new_effects else "new-effects",
        "continuum.shadow.planner: two deliveries of {}".format(original),
        original_event_type=original,
        first_status=first.status,
        second_status=second.status,
        first_effects=len(first_effects),
        new_effects=len(new_effects),
    )
    run.note("replays", run.event.replays)
    run.note("first_effect_count", len(first_effects))
    run.note("second_status", second.status)
    run.note("replay_is_idempotent", not new_effects)

    if new_effects:
        return journal.with_status(
            STATUS_FAILED,
            decision="not-idempotent",
            decision_source="continuum.shadow.planner",
            reason="a re-delivered {} produced {} further effect(s); the second "
            "delivery must be a no-op".format(original, len(new_effects)),
        )
    if second.status == first.status:
        return journal.with_status(
            first.status,
            decision="idempotent",
            decision_source="continuum.shadow.planner",
            reason="both deliveries reached {!r} and the second recorded no "
            "further effects".format(first.status),
        )
    return journal.with_status(
        STATUS_FAILED,
        decision="not-idempotent",
        decision_source="continuum.shadow.planner",
        reason="the second delivery changed the decision from {!r} to {!r} even "
        "though it recorded no further effects".format(first.status, second.status),
    )


# --------------------------------------------------------------------------- #
# timeout / stalled recovery
# --------------------------------------------------------------------------- #


@_handler(SCENARIO_TIMEOUT_RECOVERY)
def _plan_timeout_recovery(run: _Run, journal: ShadowJournal) -> ShadowJournal:
    """A repair that stopped, and what the watchdog does about it.

    The two decisions that matter for a stalled run are the watchdog's -- is the
    lock still current, has the failure been recorded -- and the ladder's, which
    decides whether another attempt is allowed. Both are called with the observed
    age so a captured case reproduces the same verdict months later.
    """

    repair = engine.conflict_repair()
    event = run.event
    pull = run.state.require_pull(event.number)
    attempts = _attempts(run, pull)
    age_seconds = _seconds_since_lock(run)

    watchdog = repair.evaluate_watchdog(
        labels=pull.labels,
        head_sha=pull.head_sha,
        locked_head_sha=_locked_head(run, pull),
        attempts=attempts,
        max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
        repair_run_in_flight=run.client.repair_in_flight(pull.number),
        seconds_since_lock=age_seconds,
        mergeable_state=pull.mergeable_state,
        stale_after_minutes=repair.STALE_EPISODE_MINUTES,
        controller_marked_opt_out=_controller_marked_opt_out(run, pull),
    )
    run.guard(
        "repair.watchdog",
        watchdog.action,
        "conflict_repair.evaluate_watchdog",
        code=watchdog.code,
        reason=watchdog.reason,
        release_lock=watchdog.release_lock,
        record_failure=watchdog.record_failure,
        seconds_since_lock=age_seconds,
        stale_after_minutes=repair.STALE_EPISODE_MINUTES,
    )

    lock = repair.evaluate_lock(
        lock_present=repair.CONFLICT_LOCK_LABEL in pull.labels,
        attempts=attempts,
        max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
        repair_run_in_flight=run.client.repair_in_flight(pull.number),
    )
    run.guard(
        "repair.lock",
        lock.action,
        "conflict_repair.evaluate_lock",
        code=lock.code,
        reason=lock.reason,
        release_lock=lock.release_lock,
    )

    ladder = repair.evaluate_plan(
        mode="resolve-conflict",
        behind=_behind(run, pull),
        ruled_out=_ruled_out(run, pull),
        attempts=attempts,
        max_attempts=repair.DEFAULT_MAX_ATTEMPTS,
        seconds_since_attempt=age_seconds or None,
    )
    run.guard(
        "repair.plan",
        ladder.action or "task_execution",
        "conflict_repair.evaluate_plan",
        code=ladder.code,
        reason=ladder.reason,
        retry_after_seconds=ladder.retry_after_seconds,
        episode_attempts=ladder.episode_attempts,
    )

    if watchdog.action == "release-stale-lock" or lock.release_lock:
        run.effects.record(
            Effect(
                kind="label.remove",
                target="issue:{}".format(pull.number),
                detail={"label": repair.CONFLICT_LOCK_LABEL},
            )
        )
    if watchdog.record_failure or ladder.record_failure:
        run.effects.record(
            Effect(
                kind="label.add",
                target="issue:{}".format(pull.number),
                detail={"labels": [repair.REPAIR_FAILED_LABEL]},
            )
        )
    if ladder.action in repair.RECOVERY_RUNGS:
        run.effects.record(
            Effect(
                kind="workflow.dispatch",
                target="workflow:opencode.yml",
                detail={
                    "ref": run.state.base_ref,
                    "inputs": {
                        "mode": "resolve-conflict",
                        "pr_number": str(pull.number),
                        "head_ref": pull.head_ref,
                    },
                },
            )
        )

    terminal = STATUS_OK if run.effects.kinds() else STATUS_NO_ACTION
    return journal.with_status(
        terminal,
        decision=watchdog.action,
        decision_source="conflict_repair.evaluate_watchdog",
        reason=watchdog.reason,
    )


# --------------------------------------------------------------------------- #
# Captured-state derivations
#
# These are the only places the planner reads the capture rather than calling
# the engine, and each one is a fact NanoDictate's own controllers also compute
# from the same fields. Each is recorded as a guard so a parity report can show
# which captured fact produced which decision.
# --------------------------------------------------------------------------- #


def _behind(run: _Run, pull: Any) -> int:
    """Commits the pull request head is behind its base.

    From the captured comparison, exactly as ``opencode-repair.yml`` reads it
    from ``gh api repos/.../compare/main...<head> --jq .behind_by``. A capture
    with no comparison is a capture that cannot answer this, and the planner
    says zero only when the pull request is open on the base ref.
    """

    recorded = run.event.extra.get("behind_by")
    if isinstance(recorded, int):
        return max(0, recorded)
    if isinstance(recorded, str) and recorded.isdigit():
        return int(recorded)
    return 0


def _attempts(run: _Run, pull: Any) -> int:
    """Repair attempts already spent, as ``conflict_repair.episode_attempts`` reads them."""

    repair = engine.conflict_repair()
    return repair.episode_attempts(run.state.comments(pull.number), pr_number=pull.number)


def _ruled_out(run: _Run, pull: Any) -> Tuple[str, ...]:
    """Recovery rungs the captured episode already tried."""

    repair = engine.conflict_repair()
    episode = repair.latest_episode(run.state.comments(pull.number))
    recorded = episode.get("rungs_ruled_out") or ()
    if isinstance(recorded, str):
        recorded = [item for item in recorded.split(",") if item]
    return tuple(item for item in recorded if item in repair.RECOVERY_RUNGS)


def _locked_head(run: _Run, pull: Any) -> str:
    """The head the repair lock was taken against."""

    recorded = run.event.extra.get("locked_head_sha")
    return str(recorded or "").strip().lower()


def _seconds_since_lock(run: _Run) -> int:
    """How long the lock has been held, from the capture's own timestamps."""

    recorded = run.event.extra.get("seconds_since_lock")
    if isinstance(recorded, int) and recorded >= 0:
        return recorded
    if isinstance(recorded, str) and recorded.isdigit():
        return int(recorded)
    captured = run.state.captured_at_ms
    started = run.event.observed_at_ms
    if captured and started and captured >= started:
        return int((captured - started) / 1000)
    return 0


def _controller_marked_opt_out(run: _Run, pull: Any) -> bool:
    """Whether this controller's own marker accounts for the opt-out.

    ``conflict_repair`` only ever removes a ``no-auto-merge`` its own episode
    marker claims. A human's opt-out is never touched, so the planner has to
    establish the same provenance or it would record a label removal the
    production controller would refuse to make.
    """

    recorded = run.event.extra.get("controller_marked_opt_out")
    if isinstance(recorded, bool):
        return recorded
    return False


def _ci_is_green(run: _Run, pull: Any) -> bool:
    """Whether the exact head has a completed, successful CI run.

    The production rule, from ``trust_policy.list_ci_is_green``: the newest
    *completed* run for the exact head, in this repository, from a
    ``pull_request`` event. A planner that answered "true" because one run
    somewhere was green would make every merge comparison wrong in the same
    direction, which is the worst kind of wrong.
    """

    runs = run.state.check_runs.get(pull.head_sha, ())
    if not runs:
        return False
    contexts = set(run.config.config.review.queue.required_checks)
    outcomes: Dict[str, str] = {}
    for context, conclusion in runs:
        outcomes[context] = conclusion
    for context in contexts:
        if outcomes.get(context) != "success":
            return False
    return True


def _gh_event_name(event: ShadowEvent) -> str:
    """The GitHub event name production would have seen.

    Shadow event types are finer-grained than GitHub's -- there is one for
    ``pull_request.synchronize`` and one for the merge sweep -- so this is a
    widening into GitHub's own names, never a narrowing. The controller wakes
    are looked up in :data:`continuum.review.queue.WAKE_UP_ACTIONS`, which is
    keyed on GitHub's spelling, so a wrong name here does not produce a wrong
    decision: it produces "this event cannot change the queue", which is a
    decision nobody made.
    """

    name, _, _ = event.event_type.partition(".")
    return {
        "pull_request": "pull_request",
        "issue": "issues",
        "check_run": "check_run",
        "check_suite": "check_suite",
        "workflow_run": "workflow_run",
        "schedule": "schedule",
        "release": "release",
    }.get(name, name)


#: Actions GitHub spells with one ``l``. The shadow vocabulary uses the
#: repository's own spelling, and the queue is keyed on GitHub's, so the
#: translation happens here at the boundary rather than by rewriting the
#: captured event -- the journal keeps the action the bridge observed.
_GH_ACTIONS = {"labelled": "labeled", "unlabelled": "unlabeled"}


def _gh_action(event: ShadowEvent) -> str:
    return _GH_ACTIONS.get(event.action, event.action)


#: Event types whose *content* is a third party. In production this text
#: reaches Continuum through the ``issue_comment`` control plane, which is the
#: only place a payload is authorized against a login, so these are the events
#: for which ``trust_policy.evaluate_event`` is the real gate. Everything else
#: arrives as controller metadata, and the authorization question for it is
#: ``is_trusted_pull_request``.
_CONTROL_PLANE_EVENTS = frozenset(
    {TYPE_PR_REVIEW, TYPE_PR_REVIEW_COMMENT}
)


def _authorization(run: _Run, event: ShadowEvent) -> Tuple[str, str, str]:
    """Ask the real trust policy whether this event may drive automation.

    Returns ``(source, code, reason)`` with an empty ``code`` meaning allowed.
    The three gates are kept apart because a parity report has to attribute a
    refusal correctly: refusing an untrusted *login* is a security decision both
    systems must share, refusing a *fork pull request* is a decision the
    controller makes internally, and reporting either as the other would
    misattribute it.
    """

    policy = engine.trust_policy()
    owner = run.state.owner()
    pull = run.state.pull(event.number) if event.number is not None else None
    api_pr = pull.as_api() if pull is not None else None

    if event.event_type in _CONTROL_PLANE_EVENTS:
        payload = _gh_payload(event, run.state)
        decision = policy.evaluate_event(
            "issue_comment",
            payload,
            repository=run.state.repository,
            configured_actors=run.state.trusted_actors,
            base_ref=run.state.base_ref,
            pull_request=api_pr,
        )
        return "trust_policy.evaluate_event", decision.code, decision.reason

    if event.event_type.startswith("issue."):
        issue = run.state.issue(event.number) if event.number is not None else None
        if issue is None:
            return "trust_policy.is_trusted_issue", "", ""
        trusted, code, reason = policy.is_trusted_issue(
            issue.as_api(), owner, run.state.trusted_actors
        )
        return "trust_policy.is_trusted_issue", "" if trusted else code, reason

    if api_pr is None:
        # No pull request to distrust: the controller's own eligibility checks
        # are the whole gate for a repository-scoped event.
        return "trust_policy.is_trusted_pull_request", "", ""

    trusted, code, reason = policy.is_trusted_pull_request(
        api_pr, run.state.repository, run.state.base_ref
    )
    return "trust_policy.is_trusted_pull_request", "" if trusted else code, reason


def _gh_payload(event: ShadowEvent, state: ObservedState) -> Dict[str, Any]:
    """A payload shaped like the webhook the trust policy expects.

    Rebuilt from the capture rather than kept, so a replay cannot smuggle a
    field the capture does not contain.
    """

    issue = state.issue(event.number) if event.number is not None else None
    payload: Dict[str, Any] = {
        "action": event.action,
        "repository": {"full_name": event.repository},
        "sender": {"login": event.actor},
    }
    if event.event_type.startswith("issue."):
        payload["issue"] = issue.as_api() if issue is not None else {
            "number": event.number,
            "user": {"login": event.actor},
            "labels": [{"name": name} for name in event.labels],
            "pull_request": None,
        }
    elif event.number is not None:
        pull = state.pull(event.number)
        if pull is not None:
            payload["pull_request"] = pull.as_api()
    if event.labels:
        payload["labels"] = [{"name": name} for name in event.labels]
    return payload


def scenarios() -> Tuple[str, ...]:
    return SCENARIO_CLASSES
