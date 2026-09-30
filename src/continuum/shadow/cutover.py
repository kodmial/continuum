"""Scenario coverage and the cutover gate for #28.

The issue is explicit about the failure mode this plane exists to prevent:

    Do not hard-code an arbitrary number of successful runs as a substitute for
    scenario coverage; record the evidence used to approve cutover.

So a count is never the gate. The gate is: every required scenario class has been
observed, each on a *real* NanoDictate event, with no unresolved semantic
divergence and no liveness failure across the whole window. Ninety-nine green
merge decisions and zero releases is a window that says nothing about releases,
and a gate that only counted runs would read it as approval.

Three properties make the answer trustworthy rather than merely computed:

* **Live evidence is distinguished from replayed evidence.** A scenario covered
  only by replays of captured cases is reported as such, and does not satisfy the
  requirement. Replays are what prove a *new* engine still agrees; they are not
  evidence that the live path still works.

* **A divergence is unresolved until somebody says how.** ``Resolution`` is an
  explicit record naming who decided and why. Silently dropping a divergence from
  the input list would be indistinguishable from fixing it.

* **The gate cannot approve itself.** It reports ``ready``, and the decision to act
  on it comes from outside. For a consumer whose cutover is a human decision that
  record is an :class:`Approval`, naming the person, the canary, the rollback path
  and the digest of the evidence it was granted against. For a zero-touch consumer
  it is an :class:`Authorization`, a record the trusted controller issued, bound to
  every input that could have moved since: the evidence digest, the rolling
  baseline, the consumer HEAD, the controller's own SHA, the canary, the rollback
  target and the exact cutover pull request head. Either way a record whose bindings
  no longer match is refused, so a decision cannot be carried over to a window that
  has moved on since it was made.

**Coverage is phase-aware.** NanoDictate's first cutover replaces the control-plane
writers -- scheduler, OpenCode, repair, CodeRabbit review and merge -- and leaves
its release workflows as the sole release writer until #21/#22 replace those. So
:data:`REQUIRED_SCENARIOS` is the full list, :data:`PHASE_A` drops
``release-planning`` from what it demands, and the gate refuses a phase-A change
set that touches a release writer at all. The full list is the default, so a
caller that names no phase gets the stricter answer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import baseline, engine, liveness, parity

CUTOVER_SCHEMA = "continuum.shadow-cutover/v1"
AUTHORIZATION_SCHEMA = "continuum.shadow-authorization/v1"
CHANGE_SET_SCHEMA = "continuum.shadow-change-set/v1"

#: A full commit SHA, and nothing that merely resembles one. Every binding that
#: names code names it this way, so an authorization cannot be expressed against
#: "whatever HEAD is" or a branch.
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: The scenario classes the issue requires before #28 may cut over, and why each
#: is on the list. Carried with reasons because a coverage list without them
#: invites someone to drop the inconvenient one.
REQUIRED_SCENARIOS: Tuple[Tuple[str, str], ...] = (
    (
        "issue-lifecycle",
        "the ordinary issue -> worker -> pull request path, which is most production traffic",
    ),
    ("ci-repair", "CI failure -> repair/retry, the ladder and its attempt limits"),
    (
        "coderabbit-finding",
        "a blocking review finding -> repair -> re-review, the path with the most state",
    ),
    ("merge-decision", "merge eligibility and the merge itself"),
    (
        "dependency-blocked",
        "a task blocked on another, where refusing to act is the correct answer",
    ),
    (
        "release-planning",
        "release planning in dry-run mode, coordinated with #22",
    ),
    (
        "duplicate-replay",
        "a duplicate or replayed delivery, which must not act twice",
    ),
    (
        "timeout-recovery",
        "a timeout or failure, and the recovery rung it selects",
    ),
)

#: Where a run came from. A live run and a replay of a captured case produce
#: identical verdicts, so the origin has to be recorded separately for the gate to
#: be able to tell live coverage from replayed coverage.
ORIGIN_BRIDGE = "bridge"
ORIGIN_REPLAY = "replay"

#: The scenario class the release phase owns. It is the whole difference between
#: the two phases: the control-plane phase does not replace the release writer, so
#: it cannot ask for release evidence, and the release phase cannot cut over
#: without it.
RELEASE_SCENARIO = "release-planning"

#: Cutover phases, in the order they happen. A phase says which writers the cutover
#: replaces and therefore which evidence it has to be judged on, and it is spelled
#: out rather than inferred so that an authorization recorded for one phase cannot
#: be honoured by a gate judging another.
PHASE_A = "phase-a"
PHASE_B = "phase-b"
PHASES: Tuple[str, ...] = (PHASE_A, PHASE_B)

#: The phase a gate judges when the caller names none. The stricter one: a window
#: that has not shown release evidence is refused, which is the answer every
#: consumer but NanoDictate's first consumer wants.
DEFAULT_PHASE = PHASE_B

#: What a phase does to a path, as the change set records it. ``retain`` is not a
#: no-op for the gate -- it is how a cutover says it considered a path -- but it
#: changes nothing, so it is neither a retirement nor a replacement.
CHANGE_ACTIONS: Tuple[str, ...] = ("add", "modify", "remove", "disable", "retain")

#: The actions that take a writer away. Phase A may only take away a writer it
#: replaces; anything else it removes is a removal this phase has no mandate for.
RETIREMENT_ACTIONS: Tuple[str, ...] = ("remove", "disable")

#: Scenario classes that would satisfy the requirement but were not asked for.
#: Reported, never counted: extra classes are evidence, not a substitute.
OPTIONAL_SCENARIOS: Tuple[str, ...] = (
    "schedule-merge",
    "unplannable",
)


class CutoverError(ValueError):
    """The gate cannot be evaluated from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


def required_scenarios(phase: str = DEFAULT_PHASE) -> Tuple[Tuple[str, str], ...]:
    """The scenario classes ``phase`` must have observed, each with its reason.

    Phase A does not replace the release writer, so demanding release evidence from
    it would be demanding proof of a migration it is not performing -- and a gate
    that cannot be satisfied is a gate nobody trusts. Phase B keeps the full list,
    and it is the default for that reason: an unnamed phase gets the strict answer.
    """

    if phase not in PHASES:
        raise CutoverError(
            "unknown_cutover_phase",
            "{!r} is not a phase; expected one of {}".format(phase, ", ".join(PHASES)),
        )
    if phase == PHASE_A:
        return tuple(
            (name, reason)
            for name, reason in REQUIRED_SCENARIOS
            if name != RELEASE_SCENARIO
        )
    return REQUIRED_SCENARIOS


def phase_writers(phase: str = DEFAULT_PHASE) -> Tuple[str, ...]:
    """The writer roles ``phase`` replaces, from the ledger's own vocabulary.

    Neither phase lists ``other``: a workflow implementing no writer this gate knows
    about is never one a cutover retires, and listing it would make "Phase A may
    only touch what it replaces" true by including everything.
    """

    if phase not in PHASES:
        raise CutoverError(
            "unknown_cutover_phase",
            "{!r} is not a phase; expected one of {}".format(phase, ", ".join(PHASES)),
        )
    if phase == PHASE_A:
        return baseline.CONTROL_PLANE_WRITERS
    return baseline.CONTROL_PLANE_WRITERS + (baseline.RELEASE_WRITER,)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Resolution:
    """How a divergence was settled.

    A resolution is a claim, not a fix: the gate records that somebody decided
    the difference was acceptable and why, and the claim stays in the artifact
    for the person reading the cutover decision later.
    """

    correlation_id: str
    resolution: str
    resolved_by: str = ""
    resolved_at: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.resolution.strip()) and bool(self.resolved_by.strip())

    def describe(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "resolution": self.resolution,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
        }


@dataclasses.dataclass(frozen=True)
class Approval:
    """A human decision to cut over, bound to one window's evidence."""

    approved_by: str = ""
    approved_at: str = ""
    evidence_digest: str = ""
    canary_reference: str = ""
    rollback_reference: str = ""
    note: str = ""

    @property
    def complete(self) -> bool:
        return all(
            (
                self.approved_by.strip(),
                self.approved_at.strip(),
                self.evidence_digest.strip(),
                self.canary_reference.strip(),
                self.rollback_reference.strip(),
            )
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "evidence_digest": self.evidence_digest,
            "canary_reference": self.canary_reference,
            "rollback_reference": self.rollback_reference,
            "note": self.note,
        }


@dataclasses.dataclass(frozen=True)
class Authorization:
    """A trusted controller's authorization, bound to one exact reading.

    An :class:`Approval` says a person looked at this window. An authorization says
    the trusted controller did, and it is what lets a zero-touch cutover merge
    without anybody commenting on it. That only works if the record cannot be
    reused after the world moves, so it names every input the decision depended on
    and the gate re-derives all of them:

    * ``evidence_digest`` -- the window's verdicts, coverage, liveness, resolutions
      and baseline reading, exactly as :func:`evidence_digest` hashes them;
    * ``baseline_digest`` and ``consumer_head`` -- the rolling reading and the
      consumer commit it was taken from, so a moved workflow or a new commit expires
      the authorization rather than riding along under it;
    * ``controller_sha`` -- the Continuum commit that issued the authorization,
      compared against the engine this gate is running as, because an authorization
      for one engine is not a statement about another;
    * ``canary_reference`` and ``rollback_reference`` -- the canary that passed and
      the way back, matched against the evidence supplied with this run;
    * ``cutover_head`` -- the exact head of the atomic cutover pull request, matched
      against the change set being judged.

    The controller is trusted, not believed: everything here is a claim the gate
    checks, and a mismatch is a blocker rather than a warning.
    """

    phase: str = ""
    authorized_by: str = ""
    authorized_at: str = ""
    evidence_digest: str = ""
    baseline_digest: str = ""
    consumer_head: str = ""
    controller_sha: str = ""
    canary_reference: str = ""
    rollback_reference: str = ""
    cutover_head: str = ""
    note: str = ""

    #: Bindings that carry a reference rather than a commit, and so only have to be
    #: present and non-blank.
    TEXT_BINDINGS: Tuple[str, ...] = (
        "phase",
        "authorized_by",
        "authorized_at",
        "evidence_digest",
        "baseline_digest",
        "canary_reference",
        "rollback_reference",
    )
    #: Bindings that name code, and so have to be full commit SHAs. A branch name or
    #: a truncated SHA here would be a binding to nothing in particular.
    COMMIT_BINDINGS: Tuple[str, ...] = (
        "consumer_head",
        "controller_sha",
        "cutover_head",
    )

    @property
    def missing(self) -> Tuple[str, ...]:
        """The bindings this record does not carry, named as the gate sees them."""

        gaps = [
            name for name in self.TEXT_BINDINGS if not str(getattr(self, name)).strip()
        ]
        gaps.extend(
            name
            for name in self.COMMIT_BINDINGS
            if not _COMMIT.match(str(getattr(self, name)))
        )
        return tuple(gaps)

    @property
    def complete(self) -> bool:
        return not self.missing

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": AUTHORIZATION_SCHEMA,
            "kind": "authorization",
            "phase": self.phase,
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "evidence_digest": self.evidence_digest,
            "baseline_digest": self.baseline_digest,
            "consumer_head": self.consumer_head,
            "controller_sha": self.controller_sha,
            "canary_reference": self.canary_reference,
            "rollback_reference": self.rollback_reference,
            "cutover_head": self.cutover_head,
            "note": self.note,
        }


@dataclasses.dataclass(frozen=True)
class FileChange:
    """One path in the atomic cutover, and the writer it implements."""

    path: str
    #: One of :data:`CHANGE_ACTIONS`.
    action: str
    #: The writer role this path implements, from :data:`baseline.WRITER_ROLES`. The
    #: gate compares it with the role the ledger recorded for the same path, so a
    #: change set cannot reclassify a release workflow as a merge workflow to slip
    #: past a phase that does not replace releases.
    writer: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "writer": self.writer,
        }


@dataclasses.dataclass(frozen=True)
class ChangeSet:
    """What the atomic cutover pull request does, at one exact head.

    The authorization binds a pull request head, and this is the claim about what
    that head contains. Without it the gate would be authorizing a head nobody
    described, which is why a change set is required rather than optional: the
    authorization's ``cutover_head`` has to be checkable against something.
    """

    cutover_head: str = ""
    #: The phase this change set was written for. Empty means "the phase the gate is
    #: judging", which is also the phase that gets refused if the two disagree about
    #: anything else.
    phase: str = ""
    changes: Tuple[FileChange, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": CHANGE_SET_SCHEMA,
            "kind": "change-set",
            "cutover_head": self.cutover_head,
            "phase": self.phase,
            "changes": [change.describe() for change in self.changes],
        }

    @property
    def retired(self) -> Tuple[FileChange, ...]:
        return tuple(
            change for change in self.changes if change.action in RETIREMENT_ACTIONS
        )


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ScenarioCoverage:
    """What was observed for one scenario class."""

    scenario: str
    required: bool
    reason: str = ""
    events: int = 0
    #: Events that came from the live bridge, as opposed to a replay of a
    #: captured case.
    live_events: int = 0
    replayed_events: int = 0
    engines: Tuple[str, ...] = ()
    classifications: Mapping[str, int] = dataclasses.field(default_factory=dict)
    last_window_at: str = ""

    @property
    def covered(self) -> bool:
        return self.events > 0

    @property
    def live_covered(self) -> bool:
        return self.live_events > 0

    @property
    def state(self) -> str:
        """One word for the summary table.

        ``live`` only when a live event was observed. A scenario seen only
        through replays is ``replayed-only``: covered, but not by the live path.
        """

        if self.live_covered:
            return "live"
        if self.replayed_events:
            return "replayed-only"
        if self.covered:
            return "unknown-origin"
        return "missing"

    def describe(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "required": self.required,
            "reason": self.reason,
            "state": self.state,
            "events": self.events,
            "live_events": self.live_events,
            "replayed_events": self.replayed_events,
            "engines": list(self.engines),
            "classifications": {
                key: self.classifications[key] for key in sorted(self.classifications)
            },
            "last_window_at": self.last_window_at,
        }


@dataclasses.dataclass(frozen=True)
class Coverage:
    """Coverage of the required scenario classes over one window."""

    scenarios: Tuple[ScenarioCoverage, ...] = ()
    #: Which phase's requirement this is coverage for. Carried on the report rather
    #: than passed alongside it, because the digest hashes this object: an
    #: authorization for a control-plane window must not cover a release window.
    phase: str = DEFAULT_PHASE
    window_started_at: str = ""
    window_ended_at: str = ""
    generated_from: str = ""

    def for_scenario(self, scenario: str) -> Optional[ScenarioCoverage]:
        for entry in self.scenarios:
            if entry.scenario == scenario:
                return entry
        return None

    @property
    def missing(self) -> Tuple[str, ...]:
        return tuple(
            entry.scenario for entry in self.scenarios if entry.required and not entry.live_covered
        )

    @property
    def optional_seen(self) -> Tuple[str, ...]:
        return tuple(
            entry.scenario for entry in self.scenarios if not entry.required and entry.covered
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "generated_from": self.generated_from,
            "required": len([entry for entry in self.scenarios if entry.required]),
            "covered": len([entry for entry in self.scenarios if entry.required and entry.live_covered]),
            "missing": list(self.missing),
            "scenarios": [entry.describe() for entry in self.scenarios],
        }


def coverage(
    results: Sequence[parity.ParityResult],
    *,
    origins: Optional[Mapping[str, str]] = None,
    phase: str = DEFAULT_PHASE,
    window_started_at: str = "",
    window_ended_at: str = "",
    generated_from: str = "",
) -> Coverage:
    """Count what was observed, per scenario class the phase requires.

    ``origins`` maps a correlation id to where the run came from (``bridge`` or
    ``replay``). It is passed in rather than read from the results because a replay
    and a live run produce the same verdict, and the gate's whole point
    is to be able to tell them apart.

    A run with no recorded origin is counted as ``unknown-origin`` rather than as
    live: guessing would let a harness that lost its provenance file certify a
    cutover.

    ``phase`` decides which classes are required, and a class the phase does not
    require is still reported -- as evidence, not as a gap -- so a reader can see
    that release planning happened without the control-plane phase depending on it.
    """

    if results is None or isinstance(results, (str, bytes)):
        raise CutoverError("results_not_a_list", "Parity results must be a list.")

    required = {name: reason for name, reason in required_scenarios(phase)}
    optional = {name: "" for name in OPTIONAL_SCENARIOS}
    if phase == PHASE_A:
        # Observed, not demanded. Listed as optional so the summary keeps showing
        # it, and counted as covered when it happened.
        optional[RELEASE_SCENARIO] = (
            "release planning, which the control-plane phase observes but does not "
            "require, and which replaces the release writer in a later phase"
        )
    counts: Dict[str, Dict[str, Any]] = {}
    for name, reason in list(required.items()) + list(optional.items()):
        counts[name] = {
            "required": name in required,
            "reason": reason,
            "events": 0,
            "live": 0,
            "replayed": 0,
            "engines": set(),
            "classifications": {},
            "last": "",
        }

    unclassified: Dict[str, Dict[str, Any]] = {}

    for result in results:
        bucket = counts.get(result.scenario)
        if bucket is None:
            # A scenario nobody declared. Kept visible instead of dropped: an
            # undeclared scenario in a window usually means the classifier
            # changed and the coverage requirement did not.
            bucket = unclassified.setdefault(
                result.scenario or "unclassified",
                {
                    "required": False,
                    "reason": "observed but not a declared scenario class",
                    "events": 0,
                    "live": 0,
                    "replayed": 0,
                    "engines": set(),
                    "classifications": {},
                    "last": "",
                },
            )
        origin = str((origins or {}).get(result.correlation_id, ""))
        if origin == ORIGIN_REPLAY:
            bucket["replayed"] += 1
        elif origin == ORIGIN_BRIDGE:
            bucket["live"] += 1
        bucket["events"] += 1
        if result.continuum_sha:
            bucket["engines"].add(result.continuum_sha)
        key = result.classification
        bucket["classifications"][key] = bucket["classifications"].get(key, 0) + 1
        bucket["last"] = bucket["last"] or result.links.get("window_ended_at", "")

    rows: List[ScenarioCoverage] = []
    for name in list(required) + list(optional) + sorted(unclassified):
        bucket = counts.get(name) or unclassified[name]
        rows.append(
            ScenarioCoverage(
                scenario=name,
                required=bool(bucket["required"]),
                reason=str(bucket["reason"]),
                events=int(bucket["events"]),
                live_events=int(bucket["live"]),
                replayed_events=int(bucket["replayed"]),
                engines=tuple(sorted(bucket["engines"])),
                classifications=dict(bucket["classifications"]),
                last_window_at=str(bucket["last"]),
            )
        )

    return Coverage(
        scenarios=tuple(rows),
        phase=phase,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Blocker:
    """One reason the window cannot support a cutover."""

    code: str
    message: str
    scenario: str = ""
    correlation_ids: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "scenario": self.scenario,
            "correlation_ids": list(self.correlation_ids),
        }


@dataclasses.dataclass(frozen=True)
class CutoverDecision:
    """The gate's answer, and everything behind it."""

    #: Evidence is sufficient. Says nothing about whether anybody approved.
    ready: bool
    approved: bool
    #: The trusted controller authorized this exact window, evidence, consumer head,
    #: engine, canary, rollback target and cutover head. This is the zero-touch path:
    #: it needs no comment and no click between READY and the merge. Separate from
    #: ``approved`` because the two records are not the same claim and one does not
    #: stand in for the other.
    authorized: bool = False
    blockers: Tuple[Blocker, ...] = ()
    coverage: Optional[Coverage] = None
    evidence_digest: str = ""
    #: The rolling parity reading this decision was made against, when one was
    #: supplied. A window recorded while the consumer's workflows matched the
    #: ledger says nothing about the consumer's workflows now.
    baseline: Optional["baseline.BaselineReport"] = None
    #: The source-drift reading this decision was made against. The rolling baseline
    #: above asks whether the *consumer's* workflows moved; this asks whether the
    #: repositories Continuum's parity claims were taken from moved. Both can be
    #: clean at once while the claim has quietly expired.
    provenance: Optional[Any] = None
    approval: Optional[Approval] = None
    authorization: Optional[Authorization] = None
    change_set: Optional[ChangeSet] = None
    #: Which phase's requirement this decision answers, and which writers that phase
    #: replaces.
    phase: str = DEFAULT_PHASE
    window_started_at: str = ""
    window_ended_at: str = ""
    generated_from: str = ""
    unresolved: Tuple[Mapping[str, Any], ...] = ()
    canary: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    rollback: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(blocker.code for blocker in self.blockers)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": CUTOVER_SCHEMA,
            "kind": "decision",
            "ready": self.ready,
            "approved": self.approved,
            "authorized": self.authorized,
            "phase": self.phase,
            "phase_writers": list(phase_writers(self.phase)),
            "evidence_digest": self.evidence_digest,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "generated_from": self.generated_from,
            "blockers": [blocker.describe() for blocker in self.blockers],
            "unresolved": [dict(entry) for entry in self.unresolved],
            "coverage": self.coverage.describe() if self.coverage is not None else None,
            "baseline": self.baseline.describe() if self.baseline is not None else None,
            "provenance": (
                self.provenance.describe() if self.provenance is not None else None
            ),
            "canary": dict(self.canary),
            "rollback": dict(self.rollback),
            "approval": self.approval.describe() if self.approval is not None else None,
            "authorization": (
                self.authorization.describe() if self.authorization is not None else None
            ),
            "change_set": (
                self.change_set.describe() if self.change_set is not None else None
            ),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"

    def summary_line(self) -> str:
        if self.authorized:
            return "cutover: authorized for {} against evidence {}".format(
                self.phase, self.evidence_digest
            )
        if self.approved:
            return "cutover: approved against evidence {}".format(self.evidence_digest)
        if self.ready:
            return "cutover: evidence is sufficient, awaiting a recorded approval"
        return "cutover: not ready -- {}".format(
            "; ".join(blocker.message for blocker in self.blockers) or "unknown"
        )


def decide(
    results: Sequence[parity.ParityResult],
    liveness_report: Optional[liveness.LivenessReport] = None,
    *,
    baseline: Optional["baseline.BaselineReport"] = None,
    provenance: Optional[Any] = None,
    origins: Optional[Mapping[str, str]] = None,
    resolutions: Sequence[Resolution] = (),
    approval: Optional[Approval] = None,
    authorization: Optional[Authorization] = None,
    change_set: Optional[ChangeSet] = None,
    ledger: Optional["baseline.ParityLedger"] = None,
    canary: Optional[Mapping[str, Any]] = None,
    rollback: Optional[Mapping[str, Any]] = None,
    phase: str = DEFAULT_PHASE,
    controller_sha: str = "",
    window_started_at: str = "",
    window_ended_at: str = "",
    generated_from: str = "",
) -> CutoverDecision:
    """Evaluate the window and return the decision, with every blocker named.

    Blockers are returned rather than raised: a reader of a cutover artifact
    needs the whole list, and a gate that stopped at the first problem would make
    fixing them a serial exercise.

    ``phase`` selects the requirement (see :func:`required_scenarios`) and the
    writers the cutover may replace (see :func:`phase_writers`). ``ledger`` is the
    reviewed audit the change set's declared writer roles are checked against,
    ``baseline`` is the rolling reading of the consumer's own workflows, and
    ``provenance`` is the drift reading of the sources those parity claims came
    from. ``controller_sha`` is the Continuum commit the authorization is expected to
    name; empty means "the engine this gate is running as", which is the only value a
    caller should ever rely on.
    """

    if not window_started_at or not window_ended_at:
        raise CutoverError(
            "window_not_declared",
            "A cutover window must state when it started and ended; an undated "
            "window cannot be reviewed or reproduced.",
        )

    report = coverage(
        results,
        origins=origins,
        phase=phase,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
    )

    blockers: List[Blocker] = []
    resolved_ids = {
        record.correlation_id for record in resolutions if record.complete
    }

    # 1. Coverage, per class, from live events only.
    for entry in report.scenarios:
        if not entry.required:
            continue
        if entry.live_covered:
            continue
        if entry.replayed_events:
            blockers.append(
                Blocker(
                    code="replayed_only_coverage",
                    scenario=entry.scenario,
                    message=(
                        "{} has only been exercised through replays, so the live path "
                        "is unproven: {}".format(entry.scenario, entry.reason)
                    ),
                )
            )
            continue
        blockers.append(
            Blocker(
                code="uncovered_scenario",
                scenario=entry.scenario,
                message=(
                    "{} has not been observed on a live NanoDictate event, so the "
                    "window says nothing about it: {}".format(entry.scenario, entry.reason)
                ),
            )
        )

    # 2. Divergences, unless somebody recorded how each was settled.
    unresolved: List[Mapping[str, Any]] = []
    for result in results:
        if not result.divergent or result.correlation_id in resolved_ids:
            continue
        unresolved.append(
            {
                "correlation_id": result.correlation_id,
                "scenario": result.scenario,
                "classification": result.classification,
                "summary": result.summary,
                "links": dict(result.links),
            }
        )
    if unresolved:
        blockers.append(
            Blocker(
                code="unresolved_divergence",
                message="{} event(s) diverged with no recorded resolution".format(len(unresolved)),
                correlation_ids=tuple(str(entry["correlation_id"]) for entry in unresolved),
            )
        )

    # 3. Liveness, over the same window.
    if liveness_report is None:
        blockers.append(
            Blocker(
                code="no_liveness_evidence",
                message="no liveness report was supplied, so a stalled plane would read "
                "as a quiet one",
            )
        )
    else:
        for run in liveness_report.failures:
            blockers.append(
                Blocker(
                    code="liveness_{}".format(run.verdict),
                    message="{}: {}".format(run.correlation_id, run.reason),
                    correlation_ids=(run.correlation_id,),
                )
            )
        if not liveness_report.runs:
            blockers.append(
                Blocker(
                    code="no_liveness_evidence",
                    message="the liveness report contains no runs, which is not evidence "
                    "that any run answered",
                )
            )

    # 4. The rolling baseline: what the consumer's workflows look like now, against
    #    the ledger the parity claim was made from. Checked before the digest so a
    #    drifted consumer is refused on its own terms, and included in the digest so
    #    an approval cannot outlive the repository state it was granted against.
    if baseline is None:
        blockers.append(
            Blocker(
                code="no_baseline_evidence",
                message=(
                    "no rolling baseline reading was supplied; a window says nothing "
                    "about the consumer's current workflow blobs or open .github/** "
                    "pull requests, and a historical snapshot alone can never "
                    "authorize cutover"
                ),
            )
        )
    else:
        for blocker in baseline.blockers:
            blockers.append(
                Blocker(
                    code="baseline_{}".format(blocker.code),
                    message=blocker.message,
                    scenario=blocker.subject,
                )
            )

    # 5. Source drift: whether the repositories Continuum's parity claims were taken
    #    from have moved since the commit each claim was audited against. A clean
    #    rolling baseline above says the consumer's own workflows are unchanged; it
    #    says nothing about whether the source those claims came from moved on, and a
    #    claim about content that has since been replaced is not still a claim.
    blockers.extend(provenance_blockers(provenance))

    digest = evidence_digest(
        report,
        results,
        liveness_report,
        resolutions,
        window_started_at,
        window_ended_at,
        baseline_digest=(baseline.evidence_digest if baseline is not None else ""),
        provenance_digest=(provenance.digest if provenance is not None else ""),
    )

    # 6. The canary and the rollback path, then whichever decision record the window
    #    carries. Both are required before either kind of decision is honoured: a
    #    cutover with no recorded way back is refused however it was authorised.
    deciding = approval is not None or authorization is not None
    if deciding and not canary:
        blockers.append(
            Blocker(code="no_canary", message="no canary evidence was recorded")
        )
    if deciding and not rollback:
        blockers.append(
            Blocker(
                code="no_rollback",
                message="no rollback path was recorded; NanoDictate must remain able to "
                "resume ownership",
            )
        )

    approved = False
    if approval is not None:
        if not approval.complete:
            blockers.append(
                Blocker(
                    code="incomplete_approval",
                    message="the approval must name who approved, when, the canary, the "
                    "rollback path, and the evidence it was granted against",
                )
            )
        elif approval.evidence_digest != digest:
            blockers.append(
                Blocker(
                    code="stale_approval",
                    message="the approval was granted against evidence {}, but this "
                    "window's evidence is {}; the window has moved on".format(
                        approval.evidence_digest or "nothing", digest
                    ),
                )
            )
        elif blockers:
            # Everything the approval could be granted against has to still hold
            # when the approval is read. A complete, digest-matching approval over
            # a window with an uncovered scenario or an unresolved divergence is
            # not an approval, and returning one would let a stale signature turn
            # a failing window into an approved cutover.
            blockers.append(
                Blocker(
                    code="approval_over_blocked_window",
                    message=(
                        "the approval is complete and matches this evidence, but the "
                        "window still has {} blocker(s): {}".format(
                            len(blockers),
                            ", ".join(sorted({blocker.code for blocker in blockers})),
                        )
                    ),
                )
            )
        else:
            approved = True

    # 7. What the atomic cutover itself does. The phase says which writers it
    #    replaces, and the ledger says which writer each path implements, so a
    #    control-plane cutover cannot remove the consumer's release workflows even
    #    by declaring them to be something else.
    if change_set is not None:
        blockers.extend(phase_scope_blockers(phase, change_set, ledger))

    # 8. The controller's authorization, and the only path that merges with nobody
    #    watching. Checked against everything it names, and only over a window with
    #    no blockers left.
    authorized = False
    if authorization is not None:
        found, bound = check_authorization(
            authorization,
            phase=phase,
            evidence_digest=digest,
            baseline=baseline,
            change_set=change_set,
            canary=canary,
            rollback=rollback,
            controller_sha=controller_sha,
        )
        blockers.extend(found)
        if bound:
            if blockers:
                blockers.append(
                    Blocker(
                        code="authorization_over_blocked_window",
                        message=(
                            "the authorization is complete and every binding matches, "
                            "but the window still has {} blocker(s): {}".format(
                                len(blockers),
                                ", ".join(sorted({blocker.code for blocker in blockers})),
                            )
                        ),
                    )
                )
            else:
                authorized = True

    return CutoverDecision(
        # ``ready`` means nothing stands in the way, approval gaps included: a
        # window with a canary still owed is not one anybody should act on.
        ready=not blockers,
        approved=approved,
        authorized=authorized,
        blockers=tuple(blockers),
        coverage=report,
        evidence_digest=digest,
        # Carried on the decision itself, not only in the digest: an artifact that
        # records the digest a reading contributed but not the reading would leave a
        # reviewer unable to see what was checked, which is the whole reason the
        # evidence exists.
        baseline=baseline,
        provenance=provenance,
        approval=approval,
        authorization=authorization,
        change_set=change_set,
        phase=phase,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
        unresolved=tuple(unresolved),
        canary=dict(canary or {}),
        rollback=dict(rollback or {}),
    )


def provenance_blockers(provenance: Optional[Any]) -> List[Blocker]:
    """What the source-drift reading refuses about this window.

    Four blockers, in the order a reader needs them:

    ``no_provenance_evidence``
        No reading was supplied at all. Without it the gate cannot tell a window
        whose sources still bear on Continuum from one whose sources have moved on
        since the claim was made, and it cannot report that as ready.
    ``provenance_unclassified_drift``
        A tracked source advanced on paths that no surviving classification covers.
        This is the substantive one: the property Continuum claims parity with may
        have changed, and nothing has been recorded about what it changed into.
    ``provenance_source_unavailable`` / ``provenance_source_unreadable``
        A source could not be read -- a private source with no credential
        configured, or a failed API read. Never folded into "clean": a source that
        was not read has not been shown to be unchanged.

    One blocker per source rather than one summary, because the fix differs -- a
    credential to configure is not the same work as a classification to record.
    """

    if provenance is None:
        return [
            Blocker(
                code="no_provenance_evidence",
                message=(
                    "no source-drift reading was supplied; the repositories Continuum's "
                    "parity claims were audited against may have moved since that audit, "
                    "and a window recorded without it cannot say they have not"
                ),
            )
        ]

    blockers: List[Blocker] = []
    for reading in provenance.readings:
        if reading.drifted:
            paths = ", ".join(finding.path for finding in reading.findings) or "unknown paths"
            blockers.append(
                Blocker(
                    code="provenance_unclassified_drift",
                    scenario=reading.id,
                    message=(
                        "source {} advanced from {} to {} on {} path(s) with no surviving "
                        "classification: {}; classify them in the provenance ledger, or "
                        "this source cannot be used to support a cutover".format(
                            reading.id,
                            (reading.baseline_sha or "?")[:12],
                            (reading.head_sha or "?")[:12],
                            len(reading.findings),
                            paths,
                        )
                    ),
                )
            )
        elif reading.status == "unavailable":
            blockers.append(
                Blocker(
                    code="provenance_source_unavailable",
                    scenario=reading.id,
                    message=(
                        "source {} is private and its read credential is not configured, "
                        "so it was not read; its parity claims are unverified rather "
                        "than confirmed ({})".format(
                            reading.id,
                            ", ".join(reading.limits) or "no credential",
                        )
                    ),
                )
            )
        elif reading.status == "unreadable":
            blockers.append(
                Blocker(
                    code="provenance_source_unreadable",
                    scenario=reading.id,
                    message=(
                        "source {} could not be read ({}); an unread source is not an "
                        "unchanged one".format(
                            reading.id, ", ".join(reading.limits) or "unknown reason"
                        )
                    ),
                )
            )
        elif reading.limits:
            # A partial reading on a source with no drift: report it under the code
            # for what happened rather than quietly folding it into "read".
            blockers.append(
                Blocker(
                    code="provenance_source_unreadable",
                    scenario=reading.id,
                    message="source {} was read only partially: {}".format(
                        reading.id, ", ".join(reading.limits)
                    ),
                )
            )
    return blockers


def phase_scope_blockers(
    phase: str,
    change_set: Optional[ChangeSet],
    ledger: Optional["baseline.ParityLedger"] = None,
) -> List[Blocker]:
    """What the change set is not allowed to do in ``phase``.

    Fail-closed on the role: a path the ledger does not classify, a change that
    claims no writer, and a change that disagrees with the ledger are all refused
    rather than resolved in the cutover's favour. The point of the rule is that
    Phase A leaves NanoDictate's release workflows as the sole release writer, and
    that has to hold against a change set that mislabels them, not only against one
    that admits what it is.
    """

    if change_set is None:
        return []

    blockers: List[Blocker] = []
    writers = phase_writers(phase)

    if change_set.phase and change_set.phase != phase:
        blockers.append(
            Blocker(
                code="change_set_phase_mismatch",
                message="the change set is written for {}, but the gate is judging {}".format(
                    change_set.phase, phase
                ),
            )
        )

    if ledger is None:
        blockers.append(
            Blocker(
                code="no_ledger_evidence",
                message="no parity ledger was supplied, so no change in the cutover can "
                "be checked against the writer roles a reviewer approved",
            )
        )
        return blockers

    reviewed = ledger.workflow_map
    for change in change_set.changes:
        entry = reviewed.get(change.path)
        if entry is None or not entry.writer:
            blockers.append(
                Blocker(
                    code="unclassified_cutover_path",
                    scenario=change.path,
                    message="{} is changed by the cutover but the ledger classifies no "
                    "writer for it".format(change.path),
                )
            )
            continue
        if not change.writer:
            blockers.append(
                Blocker(
                    code="undeclared_writer_role",
                    scenario=change.path,
                    message="the cutover {} {} without naming the writer it implements".format(
                        change.action, change.path
                    ),
                )
            )
            continue
        if change.writer != entry.writer:
            blockers.append(
                Blocker(
                    code="writer_role_disagrees_with_ledger",
                    scenario=change.path,
                    message="the cutover calls {} a {} writer, but the ledger audited it "
                    "as {}".format(
                        change.path, change.writer, entry.writer
                    ),
                )
            )
            continue
        if phase == PHASE_A and entry.writer == baseline.RELEASE_WRITER:
            blockers.append(
                Blocker(
                    code="release_writer_in_phase_a",
                    scenario=change.path,
                    message="{} is {}'s release writer, which the control-plane phase "
                    "leaves in place; replacing it belongs to the release phase".format(
                        change.path, ledger.repository or "the consumer"
                    ),
                )
            )
            continue
        if entry.writer not in writers and change.action in RETIREMENT_ACTIONS:
            blockers.append(
                Blocker(
                    code="out_of_phase_removal",
                    scenario=change.path,
                    message="the cutover {} {}, which is a {} writer; {} replaces "
                    "{}".format(
                        change.action,
                        change.path,
                        entry.writer,
                        phase,
                        ", ".join(writers),
                    ),
                )
            )

    return blockers


def check_authorization(
    authorization: Authorization,
    *,
    phase: str = DEFAULT_PHASE,
    evidence_digest: str = "",
    baseline: Optional["baseline.BaselineReport"] = None,
    change_set: Optional[ChangeSet] = None,
    canary: Optional[Mapping[str, Any]] = None,
    rollback: Optional[Mapping[str, Any]] = None,
    controller_sha: str = "",
) -> Tuple[List[Blocker], bool]:
    """Every binding the authorization names, re-derived from this run.

    Returns the blockers found and whether the record still matches. Each binding
    gets its own code, because "the authorization is stale" is not something an
    operator can act on and "the consumer HEAD moved since it was issued" is.
    """

    found: List[Blocker] = []
    controller = controller_sha or engine.engine_sha()

    gaps = authorization.missing
    if gaps:
        found.append(
            Blocker(
                code="incomplete_authorization",
                message="the authorization is missing {}; it has to bind the phase, the "
                "controller, when it was issued, the evidence, the baseline, the "
                "consumer HEAD, the controller's own commit, the canary, the rollback "
                "target and the cutover pull request head".format(", ".join(gaps)),
            )
        )
        return found, False

    if authorization.phase not in PHASES:
        found.append(
            Blocker(
                code="authorization_phase_unknown",
                message="{!r} is not a phase; expected one of {}".format(
                    authorization.phase, ", ".join(PHASES)
                ),
            )
        )
        return found, False

    if authorization.phase != phase:
        found.append(
            Blocker(
                code="authorization_phase_mismatch",
                message="the authorization was issued for {}, but the gate is judging "
                "{}".format(authorization.phase, phase),
            )
        )
        return found, False

    if not _COMMIT.match(controller):
        found.append(
            Blocker(
                code="undeterminable_controller_sha",
                message="this gate cannot name the Continuum commit it is running as, so "
                "it cannot check the controller binding",
            )
        )
        return found, False

    if change_set is None:
        found.append(
            Blocker(
                code="no_cutover_change_set",
                message="the authorization names cutover head {}, but no change set "
                "describes that head, so there is nothing to check the cutover "
                "against".format(authorization.cutover_head),
            )
        )
        return found, False

    # Each binding, named the way an operator has to look it up.
    bindings: Tuple[Tuple[str, str, str, str], ...] = (
        (
            "stale_authorization",
            "evidence",
            authorization.evidence_digest,
            evidence_digest,
        ),
        (
            "stale_baseline_binding",
            "rolling-baseline digest",
            authorization.baseline_digest,
            baseline.evidence_digest if baseline is not None else "",
        ),
        (
            "stale_consumer_head",
            "consumer HEAD",
            authorization.consumer_head,
            baseline.live_head if baseline is not None else "",
        ),
        (
            "stale_controller",
            "controller commit",
            authorization.controller_sha,
            controller,
        ),
        (
            "stale_cutover_head",
            "cutover pull request head",
            authorization.cutover_head,
            change_set.cutover_head,
        ),
        (
            "stale_canary_binding",
            "canary evidence",
            authorization.canary_reference,
            str((canary or {}).get("reference", "")),
        ),
        (
            "stale_rollback_binding",
            "rollback target",
            authorization.rollback_reference,
            str((rollback or {}).get("reference", "")),
        ),
    )
    for code, name, claimed, observed in bindings:
        if claimed == observed:
            continue
        found.append(
            Blocker(
                code=code,
                message="the authorization names {} {}, but this run can see {}; the "
                "reading it was granted against has moved".format(
                    name, claimed or "nothing", observed or "nothing"
                ),
            )
        )

    return found, not found


def evidence_digest(
    report: Coverage,
    results: Sequence[parity.ParityResult],
    liveness_report: Optional[liveness.LivenessReport],
    resolutions: Sequence[Resolution],
    window_started_at: str,
    window_ended_at: str,
    baseline_digest: str = "",
    provenance_digest: str = "",
) -> str:
    """A digest of the evidence an approval is granted against.

    Covers the verdicts, the coverage, the liveness verdicts, the resolutions,
    the rolling baseline, the source-drift reading, and the window's dates -- so an
    approval cannot be carried over to a window that has gained an event, lost a
    resolution, moved its dates, been judged against a consumer whose workflows have
    since changed, or been judged against sources that have since moved. Ordering
    is canonicalised first, so two runs that saw the same events in a different
    delivery order produce the same digest.
    """

    payload = {
        "window": [window_started_at, window_ended_at],
        # The phase travels with the digest because the requirement does. The same
        # events are sufficient for a control-plane cutover and not for a release
        # one, so a digest that could not tell the two apart would let an
        # authorization issued for one be spent on the other.
        "phase": report.phase,
        # An empty baseline digest still travels. It is the difference between a
        # window that was refused for having no baseline and one that was judged
        # against a clean reading, and a digest that could not tell those apart
        # would let the second be carried forward under the first's approval.
        "baseline": baseline_digest,
        # And the same for source drift: an empty provenance digest has to be
        # distinguishable from a clean one, or a window judged against sources that
        # were read would be indistinguishable from one judged against none.
        "provenance": provenance_digest,
        "verdicts": sorted(
            (
                result.correlation_id,
                result.classification,
                # The summary and the action names travel with the verdict: a
                # window whose divergence now names a different action is a
                # different window, and an approval granted against the earlier
                # one must not carry over to it.
                result.summary,
                sorted(str(name) for name in result.links),
            )
            for result in results
        ),
        "coverage": sorted(
            (entry.scenario, entry.state, entry.events) for entry in report.scenarios
        ),
        "liveness": sorted(
            (run.correlation_id, run.verdict)
            for run in (liveness_report.runs if liveness_report is not None else ())
        ),
        "resolutions": sorted(
            (record.correlation_id, record.resolution) for record in resolutions
        ),
    }
    return "ev-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]


def describe(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Re-render a cutover decision, keys normalised."""

    if not isinstance(document, Mapping):
        raise CutoverError("decision_not_a_mapping", "A cutover decision must be an object.")
    schema = document.get("schema")
    if schema != CUTOVER_SCHEMA:
        raise CutoverError(
            "unknown_cutover_schema",
            "expected {!r}, found {!r}".format(CUTOVER_SCHEMA, schema),
        )
    return json.loads(json.dumps(document, sort_keys=True, default=str))


def from_documents(
    parity_documents: Iterable[Mapping[str, Any]],
    liveness_document: Optional[Mapping[str, Any]] = None,
    *,
    baseline_document: Optional[Mapping[str, Any]] = None,
    provenance_document: Optional[Mapping[str, Any]] = None,
    ledger_document: Optional[Mapping[str, Any]] = None,
    origins: Optional[Mapping[str, str]] = None,
    resolution_documents: Iterable[Mapping[str, Any]] = (),
    approval_document: Optional[Mapping[str, Any]] = None,
    authorization_document: Optional[Mapping[str, Any]] = None,
    change_set_document: Optional[Mapping[str, Any]] = None,
    canary: Optional[Mapping[str, Any]] = None,
    rollback: Optional[Mapping[str, Any]] = None,
    phase: str = DEFAULT_PHASE,
    controller_sha: str = "",
    window_started_at: str = "",
    window_ended_at: str = "",
    generated_from: str = "",
) -> CutoverDecision:
    """Evaluate a window from its artifacts, in a process that decided nothing.

    Same reasoning as the liveness report: the thing that judges a window must not
    be the thing that produced it, or a crash takes the judgement with it.
    """

    results = [_result_from_document(entry) for entry in parity_documents]
    report = None
    if liveness_document is not None:
        parsed = liveness.describe(liveness_document)
        report = liveness.LivenessReport(
            runs=tuple(
                liveness.RunLiveness(
                    correlation_id=str(entry.get("correlation_id", "")),
                    verdict=str(entry.get("verdict", "")),
                    status=str(entry.get("status", "")),
                    decision=str(entry.get("decision", "")),
                    duration_ms=entry.get("duration_ms"),
                    budget_ms=int(entry.get("budget_ms", 0) or 0),
                    reason=str(entry.get("reason", "")),
                    errors=tuple(dict(item) for item in entry.get("errors", []) or []),
                )
                for entry in parsed.get("runs", [])
            ),
            orphan_grace_ms=int(parsed.get("orphan_grace_ms", 0) or 0),
            window_started_at=str(parsed.get("window_started_at", "")),
            window_ended_at=str(parsed.get("window_ended_at", "")),
            generated_from=str(parsed.get("generated_from", "")),
        )
    approval = None
    if approval_document is not None:
        approval = Approval(
            approved_by=str(approval_document.get("approved_by", "")),
            approved_at=str(approval_document.get("approved_at", "")),
            evidence_digest=str(approval_document.get("evidence_digest", "")),
            canary_reference=str(approval_document.get("canary_reference", "")),
            rollback_reference=str(approval_document.get("rollback_reference", "")),
            note=str(approval_document.get("note", "")),
        )
    return decide(
        results,
        report,
        baseline=(
            baseline.read_report(baseline_document)
            if baseline_document is not None
            else None
        ),
        provenance=(
            _read_provenance_document(provenance_document)
            if provenance_document is not None
            else None
        ),
        ledger=(
            baseline.read_ledger(ledger_document)
            if ledger_document is not None
            else None
        ),
        origins=origins,
        resolutions=[
            Resolution(
                correlation_id=str(entry.get("correlation_id", "")),
                resolution=str(entry.get("resolution", "")),
                resolved_by=str(entry.get("resolved_by", "")),
                resolved_at=str(entry.get("resolved_at", "")),
            )
            for entry in resolution_documents
        ],
        approval=approval,
        authorization=(
            read_authorization(authorization_document)
            if authorization_document is not None
            else None
        ),
        change_set=(
            read_change_set(change_set_document)
            if change_set_document is not None
            else None
        ),
        canary=canary,
        rollback=rollback,
        phase=phase,
        controller_sha=controller_sha,
        window_started_at=window_started_at or str((liveness_document or {}).get("window_started_at", "")),
        window_ended_at=window_ended_at or str((liveness_document or {}).get("window_ended_at", "")),
        generated_from=generated_from,
    )


def _read_provenance_document(document: Mapping[str, Any]) -> Any:
    """Read a source-drift report, imported here so the cutover module's own
    imports stay on its own vocabulary.

    The gate refuses a report whose stated verdict disagrees with its readings, which
    is the one thing this boundary has to guarantee: a document cannot claim to be
    clean while carrying a drifted source inside it.
    """

    from ..provenance.drift import read_report

    return read_report(document)


def read_authorization(document: Mapping[str, Any]) -> Authorization:
    """Read a controller's authorization, refusing a document this gate cannot name.

    Strict about the schema and lenient about the fields: a binding that is missing
    is a blocker the gate reports by name, which is more use to whoever has to
    reissue the record than a parse failure here would be.
    """

    if isinstance(document, Authorization):
        return document
    if not isinstance(document, Mapping):
        raise CutoverError(
            "authorization_not_a_mapping", "an authorization must be an object."
        )
    schema = document.get("schema")
    if schema != AUTHORIZATION_SCHEMA:
        raise CutoverError(
            "unknown_authorization_schema",
            "expected {!r}, found {!r}".format(AUTHORIZATION_SCHEMA, schema),
        )
    return Authorization(
        phase=str(document.get("phase", "")),
        authorized_by=str(document.get("authorized_by", "")),
        authorized_at=str(document.get("authorized_at", "")),
        evidence_digest=str(document.get("evidence_digest", "")),
        baseline_digest=str(document.get("baseline_digest", "")),
        consumer_head=str(document.get("consumer_head", "")),
        controller_sha=str(document.get("controller_sha", "")),
        canary_reference=str(document.get("canary_reference", "")),
        rollback_reference=str(document.get("rollback_reference", "")),
        cutover_head=str(document.get("cutover_head", "")),
        note=str(document.get("note", "")),
    )


def read_change_set(document: Mapping[str, Any]) -> ChangeSet:
    """Read the atomic cutover's change set.

    Strict throughout: a head that is not a commit SHA, an action this gate does not
    know, a writer role outside the ledger's vocabulary, or the same path twice are
    all refused here rather than resolved later. Each of them would otherwise be a
    way to describe a cutover that changes more than the change set says.
    """

    if isinstance(document, ChangeSet):
        return document
    if not isinstance(document, Mapping):
        raise CutoverError(
            "change_set_not_a_mapping", "a change set must be an object."
        )
    schema = document.get("schema")
    if schema != CHANGE_SET_SCHEMA:
        raise CutoverError(
            "unknown_change_set_schema",
            "expected {!r}, found {!r}".format(CHANGE_SET_SCHEMA, schema),
        )
    cutover_head = str(document.get("cutover_head", ""))
    if not _COMMIT.match(cutover_head):
        raise CutoverError(
            "change_set_head_not_a_commit",
            "a change set must name the cutover pull request head as a full commit "
            "SHA, got {!r}".format(cutover_head),
        )
    phase = str(document.get("phase", ""))
    if phase and phase not in PHASES:
        raise CutoverError(
            "unknown_cutover_phase",
            "{!r} is not a phase; expected one of {}".format(phase, ", ".join(PHASES)),
        )
    changes: List[FileChange] = []
    seen: Set[str] = set()
    for entry in document.get("changes", []) or []:
        if not isinstance(entry, Mapping):
            raise CutoverError(
                "change_set_entry_not_a_mapping", "each change must be an object"
            )
        path = str(entry.get("path", ""))
        action = str(entry.get("action", ""))
        writer = str(entry.get("writer", ""))
        if not path:
            raise CutoverError(
                "change_set_path_missing", "every change must name the path it touches"
            )
        if action not in CHANGE_ACTIONS:
            raise CutoverError(
                "unknown_change_action",
                "{!r} is not one of {}".format(action, ", ".join(CHANGE_ACTIONS)),
            )
        if writer and writer not in baseline.WRITER_ROLES:
            raise CutoverError(
                "unknown_writer_role",
                "{!r} is not one of {}".format(writer, ", ".join(baseline.WRITER_ROLES)),
            )
        if path in seen:
            raise CutoverError(
                "change_set_change_repeated",
                "{} appears twice in the change set, so the cutover it describes is "
                "not the one that was reviewed".format(path),
            )
        seen.add(path)
        changes.append(FileChange(path=path, action=action, writer=writer))
    return ChangeSet(
        cutover_head=cutover_head, phase=phase, changes=tuple(changes)
    )


def _result_from_document(document: Mapping[str, Any]) -> parity.ParityResult:
    from .parity import read_result

    if isinstance(document, parity.ParityResult):
        return document
    return read_result(document)


def summarize(decision: CutoverDecision) -> str:
    """One line for a job summary."""

    return decision.summary_line()
