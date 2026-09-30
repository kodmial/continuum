"""Replay: re-run a real NanoDictate case against a different Continuum.

A captured case is the only way to answer two questions that live observation
cannot: *would a newer Continuum have decided differently about a case that
actually happened*, and *can this case still be reproduced at all*. The first is
how a regression is found before it reaches production; the second is how a
decision from six weeks ago is explained with today's code.

Two properties make a case worth keeping:

* **It is a real event.** The case is built from a run that happened, not from a
  fixture someone invented. ``build_case`` takes the journal of a live shadow run
  and the run's own URLs, so a reader can go and check it. A synthetic case is
  labelled ``origin: fixture`` and a comparison of two of them proves only that
  the planner is deterministic.

* **It cannot write.** A replay builds its own record-only client from the
  captured state, so a replay of a merge case re-evaluates the merge and records
  it without a merge adapter in the process. ``replay`` refuses any client that
  is not record-only rather than trusting the caller.

What a replay deliberately does *not* do is call the network. A replay that
fetched live state would stop being a replay of that case and start being a
second live run, which is how a historical case quietly becomes unreproducible.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import config as config_module
from . import parity
from .config import ShadowConfig
from .effects import RecordingEffects, sequence_signatures
from .event import EventError, ShadowEvent, from_payload as event_from_payload
from .journal import STATUS_FAILED, ShadowJournal, journal_from_payload
from .observation import ObservedOutcome, outcome_from_payload
from .state import ObservedState, ShadowGitHubClient, state_from_payload

CASE_SCHEMA = "continuum.shadow-replay-case/v1"
REPLAY_SCHEMA = "continuum.shadow-replay/v1"

#: Where the case came from, because a fixture-driven replay and a production
#: replay are different kinds of evidence and must not be counted as the same.
ORIGIN_LIVE = "bridge"
ORIGIN_FIXTURE = "fixture"
ORIGIN_REPLAY = "replay"

#: Replay verdicts.
REPRODUCED = "reproduced"        #: Same decision, same effects. The engine is unchanged here.
CHANGED = "changed"              #: A different decision or a different effect set.
CRASHED = "crashed"              #: The newer engine failed on a case the old one handled.
UNVERIFIABLE = "unverifiable"    #: The case was recorded by an engine too old to compare.

#: Playback of a recorded journal is an ``exact`` observation when the capture
#: was complete, which is what the case format stores: the full capture, not the
#: summary. Anything less would compare a replay against a partial record and
#: call the difference missing actions.
REPLAY_FIDELITY = "exact"


class ReplayError(ValueError):
    """The case cannot be replayed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class Case:
    """One real case, small enough to keep and complete enough to re-run.

    ``event`` and ``observed`` are the inputs. ``recorded`` is the journal the
    live run produced, kept as a *document* rather than a journal object: the
    point of a case is that the recorded run can be a different schema version
    from the engine reading it, and rebuilding it would reject exactly the
    historical cases replay exists for.
    """

    correlation_id: str
    event: Mapping[str, Any]
    observed: Mapping[str, Any]
    recorded: Mapping[str, Any]
    scenario: str = ""
    repository: str = ""
    origin: str = ORIGIN_LIVE
    captured_at: str = ""
    source: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: What the case was captured for, in free text. A reader deciding whether a
    #: case is worth re-running needs the reason it was kept.
    subject: str = ""

    @property
    def is_live(self) -> bool:
        return self.origin == ORIGIN_LIVE

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": CASE_SCHEMA,
            "kind": "replay-case",
            "correlation_id": self.correlation_id,
            "scenario": self.scenario,
            "repository": self.repository,
            "origin": self.origin,
            "captured_at": self.captured_at,
            "subject": self.subject,
            "source": dict(self.source),
            "event": dict(self.event),
            "observed": dict(self.observed),
            "recorded": dict(self.recorded),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"

    def to_compact(self) -> str:
        return json.dumps(self.describe(), sort_keys=True, separators=(",", ":"))


@dataclasses.dataclass(frozen=True)
class Replay:
    """The result of re-running one case on this engine."""

    correlation_id: str
    verdict: str
    engine_sha: str
    journal: ShadowJournal
    #: The parity comparison of the new run against the recorded one. Its
    #: classification is the detail behind ``verdict``.
    comparison: Optional[parity.ParityResult] = None
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.verdict in (CHANGED, CRASHED)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": REPLAY_SCHEMA,
            "kind": "replay",
            "correlation_id": self.correlation_id,
            "verdict": self.verdict,
            "reason": self.reason,
            "engine": {
                "engine_sha": self.engine_sha,
                "engine_version": str(self.journal.engine.get("engine_version", "")),
                "engine_ref": str(self.journal.engine.get("engine_ref", "")),
                "config_version": str(self.journal.config.get("version", "")),
            },
            "status": self.journal.status,
            "decision": self.journal.decision,
            "actions": [effect.describe() for effect in self.journal.actions],
            "suppressed": [effect.describe() for effect in self.journal.suppressed],
            "comparison": self.comparison.describe() if self.comparison is not None else None,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"

    def summary_line(self) -> str:
        return "replay {}: {} on {}".format(
            self.correlation_id, self.verdict, self.engine_sha or "this engine"
        )


# --------------------------------------------------------------------------- #
# Capturing
# --------------------------------------------------------------------------- #


def build_case(
    journal: ShadowJournal,
    *,
    run_url: str = "",
    delivery_id: str = "",
    event_id: str = "",
    captured_at: str = "",
    subject: str = "",
    origin: Optional[str] = None,
) -> Case:
    """Turn a live shadow run into a replayable case.

    The journal's ``observed`` is already the full capture, so the case carries
    the same state the decision was made on rather than a summary of it. A case
    built from a summary would replay a decision the engine cannot re-derive,
    which is the quietest possible way for a replay to lie.
    """

    if journal.observed is None:
        raise ReplayError(
            "no_observed_state",
            "A case must carry the state the run saw; this journal has none.",
        )
    document = journal.describe()
    if origin is None:
        # A journal that was itself produced by a replay is one generation
        # removed, and saying so is what stops two hops of replay from being
        # counted as a second independent observation.
        origin = ORIGIN_REPLAY if journal.origin == ORIGIN_REPLAY else ORIGIN_LIVE

    source = {
        "run_url": run_url,
        "delivery_id": delivery_id,
        "event_id": event_id,
        "engine_sha": str(journal.engine.get("engine_sha", "")),
        "engine_ref": str(journal.engine.get("engine_ref", "")),
        "config_version": str(journal.config.get("version", "")),
        "replay_command": journal.replay_command,
        "workflow": journal.workflow_reference,
    }
    return Case(
        correlation_id=journal.event.correlation_id,
        event=dict(document["event"]),
        observed=dict(document["observed"]),
        recorded=document,
        scenario=journal.event.scenario,
        repository=journal.event.repository,
        origin=origin,
        captured_at=captured_at or journal.event.observed_at,
        source=source,
        subject=subject or journal.reason or journal.decision,
    )


def write_case(case: Case, directory: Path) -> Path:
    """Write a case into an artifact directory, named for its correlation id.

    One file per case rather than a bundle, because a case is what a person
    downloads to investigate one event, and because a bundle would need a
    container format the replay command would then have to parse.
    """

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "{}.case.json".format(_safe_name(case.correlation_id))
    path.write_text(case.to_json(), encoding="utf-8")
    return path


def read_case(document: Mapping[str, Any]) -> Case:
    """Validate and rebuild a case read back from an artifact."""

    if not isinstance(document, Mapping):
        raise ReplayError("case_not_a_mapping", "A replay case must be an object.")
    schema = document.get("schema")
    if schema != CASE_SCHEMA:
        raise ReplayError(
            "unknown_case_schema",
            "expected {!r}, found {!r}".format(CASE_SCHEMA, schema),
        )
    correlation = str(document.get("correlation_id") or "").strip()
    if not correlation:
        raise ReplayError("missing_correlation_id", "A case must name its event.")
    for field in ("event", "observed", "recorded"):
        if not isinstance(document.get(field), Mapping):
            raise ReplayError("missing_{}".format(field), "A case must carry {!r}.".format(field))
    recorded = document["recorded"]
    recorded_correlation = str(recorded.get("event", {}).get("correlation_id", ""))
    if recorded_correlation and recorded_correlation != correlation:
        raise ReplayError(
            "case_correlation_mismatch",
            "The case names {!r} but the recorded journal names {!r}.".format(
                correlation, recorded_correlation
            ),
        )
    return Case(
        correlation_id=correlation,
        event=dict(document["event"]),
        observed=dict(document["observed"]),
        recorded=dict(recorded),
        scenario=str(document.get("scenario", "")),
        repository=str(document.get("repository", "")),
        origin=str(document.get("origin", ORIGIN_LIVE)),
        captured_at=str(document.get("captured_at", "")),
        source=dict(document.get("source", {}) or {}),
        subject=str(document.get("subject", "")),
    )


def read_case_file(path: Path) -> Case:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReplayError("unreadable_case", "{} is not valid JSON: {}".format(path, error))
    return read_case(document)


def describe(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Re-render a replay document, keys normalised."""

    return json.loads(json.dumps(document, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# Replaying
# --------------------------------------------------------------------------- #


def replay(
    case: Case,
    config: Optional[ShadowConfig] = None,
    *,
    now_ms: Optional[int] = None,
    subject: str = "",
) -> Replay:
    """Re-run one case on this engine and say whether the answer changed.

    The recorded journal becomes the observed outcome, so the comparison runs
    through the same parity code the live plane uses. Reusing it is the point: a
    replay that compared differently from production would be evidence about the
    replay, not about the engine.
    """

    from . import planner

    state = state_from_payload(case.observed)
    try:
        event = event_from_payload(case.event)
    except EventError as error:
        # A case this engine can no longer read is a finding, not a harness
        # error: an engine that dropped an event type has to hear about the
        # cases that used it, and a replay that raised would take the whole
        # replay batch with it.
        return Replay(
            correlation_id=case.correlation_id,
            verdict=CRASHED,
            engine_sha=str(engine_sha_of(state)),
            journal=ShadowJournal(
                event=ShadowEvent(
                    correlation_id=case.correlation_id,
                    repository=case.repository,
                    event_type=str(case.event.get("event_type", "")),
                ),
                status=STATUS_FAILED,
                decision="unreadable_case",
                decision_source="continuum.shadow.replay",
                reason="this engine cannot read the case: {}".format(error),
            ),
            reason="this engine cannot read the case: {}".format(error),
        )
    if case.correlation_id and event.correlation_id != case.correlation_id:
        raise ReplayError(
            "case_correlation_mismatch",
            "The case's event is {!r}, not {!r}.".format(
                event.correlation_id, case.correlation_id
            ),
        )

    effects = RecordingEffects()
    client = ShadowGitHubClient(state, effects, scenario=event.scenario)
    if not _is_record_only(client):
        raise ReplayError(
            "write_capable_client",
            "A replay must run against a record-only client; refusing to continue.",
        )

    journal = planner.plan(
        event,
        state,
        config if config is not None else config_module.load(),
        origin=ORIGIN_REPLAY,
        now_ms=now_ms,
    )
    engine_sha = str(journal.engine.get("engine_sha", ""))

    if journal.status == STATUS_FAILED:
        return Replay(
            correlation_id=case.correlation_id,
            verdict=CRASHED,
            engine_sha=engine_sha,
            journal=journal,
            reason=journal.reason or "the engine failed on this case",
        )

    comparison = parity.compare(journal, _recorded_outcome(case))
    recorded_status = str(case.recorded.get("status", ""))
    recorded_decision = str(case.recorded.get("decision", ""))
    differences: List[str] = []
    if journal.status != recorded_status:
        differences.append(
            "status {!r} instead of {!r}".format(journal.status, recorded_status)
        )
    if journal.decision != recorded_decision:
        differences.append(
            "decision {!r} instead of {!r}".format(journal.decision, recorded_decision)
        )
    if comparison.divergent:
        differences.append(comparison.summary)
    reproduced = not differences
    return Replay(
        correlation_id=case.correlation_id,
        verdict=REPRODUCED if reproduced else CHANGED,
        engine_sha=engine_sha,
        journal=journal,
        comparison=comparison,
        reason=(
            "the newer engine reached the same decision and the same effects"
            if reproduced
            else "this engine reached " + "; ".join(differences)
        ),
    )


def replay_document(
    case: Case,
    config: Optional[ShadowConfig] = None,
    *,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """The replay's artifact, including the case it came from.

    Both in one file: a replay result without the case cannot be re-read, and a
    case without its result says nothing about the engine that read it.
    """

    result = replay(case, config, now_ms=now_ms)
    document = result.describe()
    document["case"] = case.describe()
    return document


def write_replay(document: Mapping[str, Any], directory: Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    correlation = str(document.get("correlation_id", "unknown"))
    engine = str(document.get("engine", {}).get("engine_sha", "") or "local")[:12]
    path = target / "{}.{}.replay.json".format(_safe_name(correlation), _safe_name(engine))
    path.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# Two engines
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ReplayComparison:
    """Two engines, one real case."""

    correlation_id: str
    #: ``reproduced`` when the two engines agree about the decision.
    classification: str
    summary: str
    left: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    right: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    differences: Tuple[Mapping[str, Any], ...] = ()

    @property
    def agrees(self) -> bool:
        return self.classification == REPRODUCED

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": REPLAY_SCHEMA,
            "kind": "comparison",
            "correlation_id": self.correlation_id,
            "classification": self.classification,
            "summary": self.summary,
            "left": dict(self.left),
            "right": dict(self.right),
            "differences": [dict(entry) for entry in self.differences],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"


def compare_replays(left: Mapping[str, Any], right: Mapping[str, Any]) -> ReplayComparison:
    """Compare two replay documents of the same case, from two engines.

    The engines' own SHAs are carried into the result rather than left to the
    caller: a comparison that does not say which two SHAs disagreed is a
    comparison nobody can act on.
    """

    correlation = str(left.get("correlation_id", "")) or str(right.get("correlation_id", ""))
    left_sha = str(left.get("engine", {}).get("engine_sha", ""))
    right_sha = str(right.get("engine", {}).get("engine_sha", ""))
    if correlation and str(right.get("correlation_id", "")) not in ("", correlation):
        raise ReplayError(
            "comparison_mismatch",
            "The two replays are of different cases: {!r} and {!r}.".format(
                correlation, right.get("correlation_id")
            ),
        )

    if left_sha and right_sha and left_sha == right_sha:
        raise ReplayError(
            "same_engine",
            "Both replays ran on {}. Comparing an engine with itself proves nothing; "
            "point the second replay at a different Continuum ref.".format(left_sha),
        )

    left_status = str(left.get("status", ""))
    right_status = str(right.get("status", ""))
    left_decision = str(left.get("decision", ""))
    right_decision = str(right.get("decision", ""))

    differences: List[Mapping[str, Any]] = []
    if left_status != right_status:
        differences.append({"field": "status", "left": left_status, "right": right_status})
    if left_decision != right_decision:
        differences.append(
            {"field": "decision", "left": left_decision, "right": right_decision}
        )

    left_actions = [dict(action) for action in left.get("actions", []) or []]
    right_actions = [dict(action) for action in right.get("actions", []) or []]
    if sequence_signatures([_effect(action) for action in left_actions]) != (
        sequence_signatures([_effect(action) for action in right_actions])
    ):
        differences.append(
            {
                "field": "actions",
                "left": [action.get("kind") for action in left_actions],
                "right": [action.get("kind") for action in right_actions],
            }
        )

    crashed = left_status == STATUS_FAILED or right_status == STATUS_FAILED
    if crashed:
        return ReplayComparison(
            correlation_id=correlation,
            classification=CRASHED,
            summary="one of the engines failed on this case",
            left={"engine_sha": left_sha, "status": left_status, "decision": left_decision},
            right={"engine_sha": right_sha, "status": right_status, "decision": right_decision},
            differences=tuple(differences),
        )
    if differences:
        return ReplayComparison(
            correlation_id=correlation,
            classification=CHANGED,
            summary="{} and {} decided differently: {}".format(
                left_sha or "the recorded run", right_sha or "this engine",
                ", ".join(str(entry["field"]) for entry in differences),
            ),
            left={"engine_sha": left_sha, "status": left_status, "decision": left_decision},
            right={"engine_sha": right_sha, "status": right_status, "decision": right_decision},
            differences=tuple(differences),
        )
    return ReplayComparison(
        correlation_id=correlation,
        classification=REPRODUCED,
        summary="{} and {} reached the same decision".format(
            left_sha or "the recorded run", right_sha or "this engine"
        ),
        left={"engine_sha": left_sha, "status": left_status, "decision": left_decision},
        right={"engine_sha": right_sha, "status": right_status, "decision": right_decision},
    )


def summarize(comparison: ReplayComparison) -> str:
    """One line for a job summary."""

    return "replay {}: {} ({})".format(
        comparison.correlation_id or "case", comparison.classification, comparison.summary
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _recorded_outcome(case: Case) -> ObservedOutcome:
    """The recorded run, as an observed outcome to compare the replay against.

    Reads the recorded journal's own action list, so the comparison is over what
    the engine actually did rather than over what the case's inputs suggest it
    should have done.
    """

    recorded = case.recorded
    status = str(recorded.get("status", ""))
    actions = [dict(action) for action in recorded.get("actions", []) or []]
    return outcome_from_payload(
        {
            "correlation_id": case.correlation_id,
            "fidelity": REPLAY_FIDELITY,
            "terminal_state": status,
            "decision": str(recorded.get("decision", "")),
            "actions": actions,
        }
    )


def _effect(document: Mapping[str, Any]):
    from .effects import Effect

    return Effect(
        kind=str(document.get("kind", "")),
        target=str(document.get("target", "")),
        detail=dict(document.get("detail", {}) or {}),
    )


def engine_sha_of(state: Any = None) -> str:
    """The engine that is about to answer, without needing a run to ask it.

    Used on the failure path, where there is no journal to read the identity
    from: a replay that crashed still has to name which Continuum crashed.
    """

    from . import engine as engine_module

    return str(engine_module.describe().get("engine_sha", ""))


def _is_record_only(client: Any) -> bool:
    """Whether ``client`` is the record-only shadow client.

    Checked structurally -- the type, not a flag the caller sets -- because a
    caller that could set the flag could also set it on a real client.
    """

    from .state import ShadowGitHubClient as RecordOnlyClient

    return isinstance(client, RecordOnlyClient)


def _safe_name(value: str) -> str:
    """A filename that is safe and still recognisable."""

    cleaned = "".join(
        char if (char.isalnum() or char in "-_.") else "-" for char in value
    ).strip("-.")
    return cleaned or "case"
