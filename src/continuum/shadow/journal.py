"""The expected-action journal: what Continuum would have done, in a form two
implementations can be compared on.

This is the artifact the whole plane produces. Everything else -- the bridge, the
barrier, the parity comparison -- exists to fill it in or check it. So the
constraints on it are the constraints on a diff format:

* **Deterministic.** The same event, state, and Continuum produce a
  byte-identical document. Nothing is stamped at write time that is not either
  an input or explicitly declared as an input. A journal whose ordering depends
  on dict iteration cannot be compared at all, because a difference in ordering
  is indistinguishable from a difference in decision.

* **Complete about suppression.** Every effect the decision engine wanted is
  listed twice: once as a planned action and once under ``suppressed``, with the
  reason. A reader must be able to answer "did Continuum intend to merge this?"
  from the journal alone, without inferring it from the absence of a merge.

* **Honest about failure.** A shadow run that crashed, hung, or hit the write
  barrier has a terminal status saying so. The default is not "success": the
  terminal status starts as ``pending`` and only becomes ``ok`` when the planner
  reached a decision. A run that dies half way therefore cannot be mistaken for a
  run that decided nothing needed doing -- which is the one failure that would
  silently manufacture parity.

* **Self-describing enough to diagnose.** The engine root, the module files, the
  event source, and the replay command are all in the document, because the
  question "which Continuum produced this" has to be answerable from the
  artifact rather than from the run that happened to be looking at it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .effects import Effect
from .event import ShadowEvent
from .state import ObservedState

JOURNAL_SCHEMA = "continuum.shadow-journal/v1"

#: Terminal statuses. The set is closed because the parity comparison branches
#: on it, and an unrecognised status would have to be treated as a failure --
#: which is the right default, but only if the set is closed enough that
#: "unrecognised" cannot happen by accident.
STATUS_OK = "ok"
STATUS_NO_ACTION = "no-action"
STATUS_DENIED = "denied"
STATUS_BLOCKED_WRITE = "blocked-write"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_REJECTED = "rejected"

TERMINAL_STATUSES: Tuple[str, ...] = (
    STATUS_OK,
    STATUS_NO_ACTION,
    STATUS_DENIED,
    STATUS_BLOCKED_WRITE,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_REJECTED,
)

#: Statuses that are a *failure of the shadow run itself* rather than a report
#: about the event. Parity is not evaluable against these, and reporting them as
#: a difference would let a broken shadow run read as a divergence in Continuum.
EXECUTION_FAILURES: Tuple[str, ...] = (STATUS_BLOCKED_WRITE, STATUS_FAILED, STATUS_TIMEOUT)


@dataclasses.dataclass(frozen=True)
class Guard:
    """One condition the decision path evaluated, and what it concluded.

    Guards are recorded rather than summarised because a parity report that says
    "these two disagree" is not actionable, and "Continuum refused the merge
    because ``ci_not_green`` while NanoDictate merged anyway" is.
    """

    name: str
    outcome: str
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: Which engine function answered. A guard without an owner cannot be
    #: reproduced, and an unowned guard is usually an invented one.
    source: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "source": self.source,
            "detail": {key: _scalar(self.detail[key]) for key in sorted(self.detail, key=str)},
        }


@dataclasses.dataclass(frozen=True)
class ShadowJournal:
    """One shadow run's expected actions."""

    event: ShadowEvent
    status: str = "pending"
    #: The decision the engine reached, in its own vocabulary: a policy code, a
    #: queue action, a recovery rung.
    decision: str = ""
    decision_source: str = ""
    reason: str = ""
    guards: Tuple[Guard, ...] = ()
    actions: Tuple[Effect, ...] = ()
    suppressed: Tuple[Effect, ...] = ()
    observed: Optional[ObservedState] = None
    engine: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    config: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    errors: Tuple[Mapping[str, Any], ...] = ()
    retries: int = 0
    #: Milliseconds from the event to the terminal decision. ``None`` means the
    #: run never reached a terminal decision, which liveness reports as a stall
    #: rather than as a very fast one.
    duration_ms: Optional[int] = None
    #: The barrier's own report, so a blocked write is in the artifact even when
    #: it also stopped the run.
    barrier: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: Where this event came from: the bridge, a replay, or a fixture sweep.
    origin: str = "bridge"
    #: Free-form, schema-versioned additions. Kept last so the required fields
    #: stay readable.
    notes: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    # -- construction ------------------------------------------------------

    def with_status(
        self,
        status: str,
        *,
        decision: str = "",
        decision_source: str = "",
        reason: str = "",
    ) -> "ShadowJournal":
        return dataclasses.replace(
            self,
            status=status,
            decision=decision or self.decision,
            decision_source=decision_source or self.decision_source,
            reason=reason or self.reason,
        )

    def with_guards(self, guards: Sequence[Guard]) -> "ShadowJournal":
        return dataclasses.replace(self, guards=tuple(guards))

    def with_actions(self, actions: Sequence[Effect], suppressed: Sequence[Effect]) -> "ShadowJournal":
        return dataclasses.replace(
            self, actions=tuple(actions), suppressed=tuple(suppressed)
        )

    def with_error(self, code: str, message: str, **detail: Any) -> "ShadowJournal":
        entry = {"code": code, "message": message}
        entry.update({key: _scalar(value) for key, value in detail.items()})
        return dataclasses.replace(self, errors=self.errors + (entry,))

    def with_observed(self, observed: ObservedState) -> "ShadowJournal":
        return dataclasses.replace(self, observed=observed)

    def with_engine(self, engine: Mapping[str, Any]) -> "ShadowJournal":
        return dataclasses.replace(self, engine=dict(engine))

    def with_config(self, config: Mapping[str, Any]) -> "ShadowJournal":
        return dataclasses.replace(self, config=dict(config))

    def with_barrier(self, barrier: Mapping[str, Any]) -> "ShadowJournal":
        return dataclasses.replace(self, barrier=dict(barrier))

    def with_note(self, key: str, value: Any) -> "ShadowJournal":
        notes = dict(self.notes)
        notes[key] = value
        return dataclasses.replace(self, notes=notes)

    def with_duration(self, duration_ms: int) -> "ShadowJournal":
        return dataclasses.replace(self, duration_ms=int(duration_ms))

    # -- properties --------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_execution_failure(self) -> bool:
        return self.status in EXECUTION_FAILURES

    @property
    def replay_command(self) -> str:
        """How to run this exact case again against another Continuum.

        Recorded in the artifact rather than reconstructed by a reader, because
        a replay that has to be rebuilt by hand is a replay nobody runs.
        """

        return (
            "PYTHONPATH=src python3 -m continuum.shadow.cli replay "
            "--case <case.json> --engine-ref <continuum-sha> --out shadow-out"
        )

    @property
    def workflow_reference(self) -> str:
        return ".github/workflows/continuum-shadow.yml (workflow_dispatch)"

    def guard(self, name: str) -> Optional[Guard]:
        for item in self.guards:
            if item.name == name:
                return item
        return None

    def describe(self) -> Dict[str, Any]:
        """The journal document.

        Every mapping is emitted with sorted keys and every list is emitted in
        decision order, so ``json.dumps(..., sort_keys=True)`` of this document
        is the canonical form and two journals differ only where they differ in
        substance.
        """

        return {
            "schema": JOURNAL_SCHEMA,
            "event": self.event.describe(),
            "scenario": self.event.scenario,
            "status": self.status,
            "decision": self.decision,
            "decision_source": self.decision_source,
            "reason": self.reason,
            "guards": [guard.describe() for guard in self.guards],
            "actions": [effect.describe() for effect in self.actions],
            "suppressed": [effect.describe() for effect in self.suppressed],
            "observed": self.observed.as_capture() if self.observed is not None else None,
            "engine": {key: _scalar(self.engine[key]) for key in sorted(self.engine, key=str)},
            "config": {key: _scalar(self.config[key]) for key in sorted(self.config, key=str)},
            "errors": [
                {key: _scalar(entry[key]) for key in sorted(entry, key=str)}
                for entry in self.errors
            ],
            "retries": self.retries,
            "duration_ms": self.duration_ms,
            "barrier": {key: _scalar(self.barrier[key]) for key in sorted(self.barrier, key=str)},
            "origin": self.origin,
            "notes": {key: _scalar(self.notes[key]) for key in sorted(self.notes, key=str)},
            "diagnostics": {
                "correlation_id": self.event.correlation_id,
                "continuum_sha": self.engine.get("engine_sha", ""),
                "continuum_version": self.engine.get("continuum_version", ""),
                "config_version": self.config.get("version", ""),
                "replay_command": self.replay_command,
                "workflow": self.workflow_reference,
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        """The canonical serialization.

        ``ensure_ascii`` is off and the separator is fixed so a diff of two
        journals shows a decision difference rather than an encoding one.
        """

        return json.dumps(
            self.describe(), sort_keys=True, indent=indent, ensure_ascii=False
        ) + "\n"

    def to_compact(self) -> str:
        """One-line form, for ``GITHUB_OUTPUT`` and for grep in a job log."""

        return json.dumps(
            self.describe(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def journal_from_document(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a journal read back from disk.

    A journal is only comparable if it is one of ours and its schema is one we
    understand, so an unknown schema is an error rather than a best-effort
    parse. Returning the document (not a ``ShadowJournal``) is deliberate: the
    comparison works on documents, and rebuilding the dataclass would throw away
    fields a newer Continuum added.
    """

    if not isinstance(document, Mapping):
        raise ValueError("journal_not_a_mapping: journal must be an object")
    schema = document.get("schema")
    if schema != JOURNAL_SCHEMA:
        raise ValueError(
            "unknown_journal_schema: expected {!r}, found {!r}".format(
                JOURNAL_SCHEMA, schema
            )
        )
    status = document.get("status")
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            "unrecognised_status: {!r} is not a terminal journal status".format(status)
        )
    return dict(document)


def journal_from_payload(payload: Mapping[str, Any]) -> ShadowJournal:
    """Rebuild a journal object from its own document.

    ``journal_from_document`` is the comparison plane's reader and deliberately
    stays a document. This is the *execution* plane's reader, for the reports that
    have to make a decision about a run they did not perform -- liveness has to
    read a crashed run's status and duration, and replay has to re-run a case. Both
    need the event back, not just its serialisation, so the rebuild happens once
    here and both reuse it.

    The action lists are rebuilt in full, because the comparison plane reads them
    from artifacts: a re-read journal that arrived with no actions would report
    every decision as a missing action.
    """

    from .effects import effects_from_payload
    from .event import from_payload as event_from_payload
    from .state import state_from_payload

    document = journal_from_document(payload)
    observed = document.get("observed")
    if not isinstance(observed, Mapping):
        raise ValueError("missing_observed_state: a journal must carry the state it saw")

    return ShadowJournal(
        event=event_from_payload(_scalar(document.get("event", {}))),
        status=str(document.get("status", "")),
        decision=str(document.get("decision", "")),
        decision_source=str(document.get("decision_source", "")),
        reason=str(document.get("reason", "")),
        guards=tuple(
            Guard(
                name=str(entry.get("name", "")),
                outcome=str(entry.get("outcome", "")),
                source=str(entry.get("source", "")),
                detail=_scalar(entry.get("detail", {})),
            )
            for entry in document.get("guards", []) or []
            if isinstance(entry, Mapping)
        ),
        actions=effects_from_payload(document.get("actions", [])),
        suppressed=effects_from_payload(document.get("suppressed", [])),
        observed=state_from_payload(_scalar(observed)),
        engine=_scalar(document.get("engine", {})),
        config=_scalar(document.get("config", {})),
        errors=tuple(
            {key: _scalar(value) for key, value in entry.items()}
            for entry in document.get("errors", []) or []
            if isinstance(entry, Mapping)
        ),
        retries=int(document.get("retries", 0) or 0),
        duration_ms=document.get("duration_ms"),
        barrier=_scalar(document.get("barrier", {})),
        origin=str(document.get("origin", "bridge")),
        notes=_scalar(document.get("notes", {})),
    )


def _scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _scalar(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in value]
    return str(value)
