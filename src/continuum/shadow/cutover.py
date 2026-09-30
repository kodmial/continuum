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

* **The gate cannot approve itself.** It reports ``ready``; approval needs a
  separate ``Approval`` that names the person, the canary, the rollback path, and
  the digest of the evidence it was granted against. An approval whose digest no
  longer matches is refused, so a decision cannot be carried over to a window
  that has moved on since it was made.

* **A window says nothing about the repository today.** Every argument to
  :func:`decide` is evidence recorded during the window. None of it names the
  consumer's current default-branch HEAD, its active workflow blobs, or the open
  pull requests that could reintroduce an orchestration writer the cutover
  removes. That is the reading :mod:`continuum.shadow.baseline` takes, and the
  gate requires one: an audit of the live head is the only thing that can tell a
  cutover that the world has not moved under it. The baseline's own evidence
  digest is folded into the approval digest, so a drift found after the approval
  was granted invalidates that approval rather than sitting beside it.

* **Fresh evidence means after the audit, not merely bound to it.** A clean
  reading says the repository still matches the ledger. It does not say the
  window observed that state: a ledger re-pinned today makes an old window
  *agree* with the present without ever having seen it. So the window must start
  on or after the ledger's audit date, and an unreadable date on either side
  blocks rather than defaulting to acceptable.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import baseline as baseline_plane
from . import liveness, parity

_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

CUTOVER_SCHEMA = "continuum.shadow-cutover/v1"

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
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "generated_from": self.generated_from,
            "required": len([entry for entry in self.scenarios if entry.required]),
            "covered": len([entry for entry in self.scenarios if entry.required and entry.live_covered]),
            "missing": list(self.missing),
            "scenarios": [entry.describe() for entry in self.scenarios],
        }


def _iso_date(value: str) -> Optional[str]:
    """The ``YYYY-MM-DD`` prefix of an ISO timestamp, or None if there is not one.

    Deliberately a prefix match rather than a full parse. These are human-supplied
    records whose exact format is not this gate's to decide, and the question being
    asked -- did the window start before the ledger was audited -- is a question
    about days. Anything with no leading date is returned as unreadable, so the
    caller blocks on it instead of guessing an order it cannot justify.
    """

    text = str(value or "").strip()
    match = _DATE_PREFIX.match(text)
    if match is None:
        return None
    try:
        datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return text[:10]


def _freshness_blockers(
    report: baseline_plane.BaselineReport, window_started_at: str
) -> List[Blocker]:
    """Require the window to postdate the ledger the evidence is judged against.

    The approval digest already ties an approval to one ledger. What it cannot do
    is notice that the window was collected *before* that ledger existed, which is
    exactly what happens when a difference is re-audited and the old window is
    re-signed: the reading and the approval then agree perfectly about a repository
    state no run in the window ever observed. Fresh evidence has to be evidence
    collected after the audit, so the order is checked rather than assumed.
    """

    audited = _iso_date(report.audited_at)
    if audited is None:
        return [
            Blocker(
                code="ledger_audit_date_unreadable",
                message=(
                    "the parity ledger records no usable audit date (found {!r}); the "
                    "window cannot be shown to postdate the audit, so the evidence is "
                    "not shown to be fresh".format(report.audited_at or "nothing")
                ),
            )
        ]

    started = _iso_date(window_started_at)
    if started is None:
        return [
            Blocker(
                code="window_start_unreadable",
                message=(
                    "the window's start ({!r}) is not an ISO timestamp, so it cannot be "
                    "compared with the ledger's audit date {}".format(
                        window_started_at or "nothing", audited
                    )
                ),
            )
        ]

    if started < audited:
        return [
            Blocker(
                code="window_predates_ledger",
                message=(
                    "the window began {} but the parity ledger was audited {}; evidence "
                    "collected before the audit cannot show what the audited state "
                    "does".format(started, audited)
                ),
            )
        ]
    return []


def coverage(
    results: Sequence[parity.ParityResult],
    *,
    origins: Optional[Mapping[str, str]] = None,
    window_started_at: str = "",
    window_ended_at: str = "",
    generated_from: str = "",
) -> Coverage:
    """Count what was observed, per required scenario class.

    ``origins`` maps a correlation id to where the run came from (``bridge`` or
    ``replay``). It is passed in rather than read from the results because a
    replay and a live run produce the same verdict, and the gate's whole point
    is to be able to tell them apart.

    A run with no recorded origin is counted as ``unknown-origin`` rather than as
    live: guessing would let a harness that lost its provenance file certify a
    cutover.
    """

    if results is None or isinstance(results, (str, bytes)):
        raise CutoverError("results_not_a_list", "Parity results must be a list.")

    required = {name: reason for name, reason in REQUIRED_SCENARIOS}
    optional = {name: "" for name in OPTIONAL_SCENARIOS}
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
    blockers: Tuple[Blocker, ...] = ()
    coverage: Optional[Coverage] = None
    evidence_digest: str = ""
    approval: Optional[Approval] = None
    window_started_at: str = ""
    window_ended_at: str = ""
    generated_from: str = ""
    unresolved: Tuple[Mapping[str, Any], ...] = ()
    canary: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    rollback: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: The live-head reading the window was judged against, and every difference
    #: it found. Kept on the decision rather than only in the gate's blockers so
    #: a reader of the artifact can see *what was compared*, not just that the
    #: comparison failed.
    baseline: Optional[baseline_plane.BaselineReport] = None

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(blocker.code for blocker in self.blockers)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": CUTOVER_SCHEMA,
            "kind": "decision",
            "ready": self.ready,
            "approved": self.approved,
            "evidence_digest": self.evidence_digest,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "generated_from": self.generated_from,
            "blockers": [blocker.describe() for blocker in self.blockers],
            "unresolved": [dict(entry) for entry in self.unresolved],
            "coverage": self.coverage.describe() if self.coverage is not None else None,
            "canary": dict(self.canary),
            "rollback": dict(self.rollback),
            "baseline": self.baseline.describe() if self.baseline is not None else None,
            "approval": self.approval.describe() if self.approval is not None else None,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"

    def summary_line(self) -> str:
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
    origins: Optional[Mapping[str, str]] = None,
    resolutions: Sequence[Resolution] = (),
    approval: Optional[Approval] = None,
    canary: Optional[Mapping[str, Any]] = None,
    rollback: Optional[Mapping[str, Any]] = None,
    baseline_report: Optional[baseline_plane.BaselineReport] = None,
    window_started_at: str = "",
    window_ended_at: str = "",
    generated_from: str = "",
) -> CutoverDecision:
    """Evaluate the window and return the decision, with every blocker named.

    Blockers are returned rather than raised: a reader of a cutover artifact
    needs the whole list, and a gate that stopped at the first problem would make
    fixing them a serial exercise.
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

    digest = evidence_digest(
        report,
        results,
        liveness_report,
        resolutions,
        window_started_at,
        window_ended_at,
        baseline_report,
    )

    # 4. The live head, read now rather than remembered from the window. Placed
    # after coverage and liveness and before the approval so that a drift and an
    # uncovered scenario are reported together: fixing one and re-running would
    # otherwise look like progress when the other was the thing that mattered.
    if baseline_report is None:
        blockers.append(
            Blocker(
                code="no_live_baseline",
                message=(
                    "no live-head reading was supplied. Every other input to this "
                    "decision was recorded during the window and says nothing about "
                    "the consumer repository as it is now: a historical reference "
                    "snapshot can never authorize a cutover"
                ),
            )
        )
    else:
        # The baseline's blockers are forwarded rather than summarised, so the
        # cutover artifact names the exact file or pull request that moved and the
        # issue that has to resolve it. Collapsing them into one code would make
        # a reader fetch a second document to learn what to do next.
        for blocker in baseline_report.blockers:
            blockers.append(
                Blocker(
                    code=blocker.code,
                    message="live head: {}".format(blocker.message),
                    scenario=blocker.subject,
                    correlation_ids=(blocker.subject,) if blocker.subject else (),
                )
            )
        blockers.extend(_freshness_blockers(baseline_report, window_started_at))

    # 5. The canary and the rollback path, then the human decision.
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
        elif not canary:
            blockers.append(
                Blocker(code="no_canary", message="no canary evidence was recorded")
            )
        elif not rollback:
            blockers.append(
                Blocker(
                    code="no_rollback",
                    message="no rollback path was recorded; NanoDictate must remain able to "
                    "resume ownership",
                )
            )
        else:
            # Everything the approval could be granted against has to still hold
            # when the approval is read. A complete, digest-matching approval over
            # a window with an uncovered scenario or an unresolved divergence is
            # not an approval, and returning one would let a stale signature turn
            # a failing window into an approved cutover.
            if blockers:
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

    return CutoverDecision(
        # ``ready`` means nothing stands in the way, approval gaps included: a
        # window with a canary still owed is not one anybody should act on.
        ready=not blockers,
        approved=approved,
        blockers=tuple(blockers),
        coverage=report,
        evidence_digest=digest,
        approval=approval,
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
        unresolved=tuple(unresolved),
        canary=dict(canary or {}),
        rollback=dict(rollback or {}),
        baseline=baseline_report,
    )


def evidence_digest(
    report: Coverage,
    results: Sequence[parity.ParityResult],
    liveness_report: Optional[liveness.LivenessReport],
    resolutions: Sequence[Resolution],
    window_started_at: str,
    window_ended_at: str,
    baseline_report: Optional["baseline_plane.BaselineReport"] = None,
) -> str:
    """A digest of the evidence an approval is granted against.

    Covers the verdicts, the coverage, the liveness verdicts, the resolutions,
    the window's dates, and the live-head reading -- so an approval cannot be
    carried over to a window that has gained an event, lost a resolution, or
    moved its dates, and cannot survive a workflow that changed after it was
    granted. Ordering is canonicalised first, so two runs that saw the same
    events in a different delivery order produce the same digest.
    """

    payload = {
        "window": [window_started_at, window_ended_at],
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
        # The live reading is part of the evidence, not a footnote to it. Reading
        # it through its own digest is enough and is what makes the omission
        # loud: a cutover judged against a drifted head carries a digest that no
        # existing approval can match, so the approval has to be granted again
        # against the head that exists when it is granted.
        "baseline": baseline_report.evidence_digest if baseline_report is not None else None,
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
    origins: Optional[Mapping[str, str]] = None,
    resolution_documents: Iterable[Mapping[str, Any]] = (),
    approval_document: Optional[Mapping[str, Any]] = None,
    canary: Optional[Mapping[str, Any]] = None,
    rollback: Optional[Mapping[str, Any]] = None,
    baseline_document: Optional[Mapping[str, Any]] = None,
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
    baseline_report = None
    if baseline_document is not None:
        # Parsed through the baseline's own reader rather than indexed here, so a
        # report the gate cannot vouch for -- an incoherent one, or one that lost
        # its evidence digest in transit -- fails here instead of reading as a
        # clean audit.
        baseline_report = baseline_plane.read_report(baseline_document)
    return decide(
        results,
        report,
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
        canary=canary,
        rollback=rollback,
        baseline_report=baseline_report,
        window_started_at=window_started_at or str((liveness_document or {}).get("window_started_at", "")),
        window_ended_at=window_ended_at or str((liveness_document or {}).get("window_ended_at", "")),
        generated_from=generated_from,
    )


def _result_from_document(document: Mapping[str, Any]) -> parity.ParityResult:
    from .parity import read_result

    if isinstance(document, parity.ParityResult):
        return document
    return read_result(document)


def summarize(decision: CutoverDecision) -> str:
    """One line for a job summary."""

    return decision.summary_line()
