"""Parity: what Continuum would have done, against what NanoDictate did, and
whether the difference matters.

The classification is the product of this plane, so it is worth being precise
about what each verdict claims, because the wrong one is worse than no report.

``exact``
    The same effects, in the same order, with the same targets. Nothing to
    explain.

``explainable``
    The same effects, with differences confined to things that are not decisions:
    a differently-worded comment, a different marker revision, a different
    timestamp, or a reordering of effects that do not depend on each other. The
    effects are the same set, so both implementations chose the same things to
    do; only the presentation differs.

``missing-action``
    Continuum would have done something NanoDictate did not. This is a
    divergence: something the production system is not doing that Continuum
    believes should happen.

``extra-action``
    NanoDictate did something Continuum would not. Also a divergence, and
    usually the more interesting direction, because it means the production
    system is acting on a rule Continuum does not model.

``ordering-guard``
    The same effects, but the order differs in a way that is not independent --
    a lock taken after the dispatch it was meant to reserve, a status written
    after the review that justifies it. Independence is the test, and it is the
    reason this is a separate verdict from ``explainable``.

``terminal-state``
    The two disagree about how the event ended. This outranks the action
    verdicts: a run that merged and a run that recorded a failure have not
    diverged in detail, they have diverged in outcome.

``shadow-failure``
    The shadow run itself did not reach a decision, exceeded its budget, or
    tripped the write barrier. Parity is not evaluable, and reporting it as a
    difference would blame Continuum for a fault in the observer.

Two rules keep the report honest. A run whose outcome capture is not usable
produces ``unevaluable`` rather than a difference, because missing evidence is
not evidence. And every verdict carries the identifiers needed to go and look:
the correlation id, the Continuum SHA, and whatever links the capture recorded.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .effects import Effect
from .journal import ShadowJournal
from .observation import (
    OBSERVED_UNKNOWN,
    ObservedOutcome,
    journal_effects,
    reconcile_targets,
)

PARITY_SCHEMA = "continuum.shadow-parity/v1"

EXACT = "exact"
EXPLAINABLE = "explainable"
MISSING_ACTION = "missing-action"
EXTRA_ACTION = "extra-action"
ORDERING_GUARD = "ordering-guard"
TERMINAL_STATE = "terminal-state"
SHADOW_FAILURE = "shadow-failure"
UNEVALUABLE = "unevaluable"

#: Every verdict, in the order a reader cares: agreement first, then the
#: observer's own failure, then the divergences from most to least structural.
CLASSIFICATIONS: Tuple[str, ...] = (
    EXACT,
    EXPLAINABLE,
    UNEVALUABLE,
    SHADOW_FAILURE,
    TERMINAL_STATE,
    MISSING_ACTION,
    EXTRA_ACTION,
    ORDERING_GUARD,
)

#: Verdicts that mean "the two implementations agree about what matters".
AGREEMENT: Tuple[str, ...] = (EXACT, EXPLAINABLE)

#: Verdicts that mean "do not cut over on the strength of this event".
DIVERGENT: Tuple[str, ...] = (
    TERMINAL_STATE,
    MISSING_ACTION,
    EXTRA_ACTION,
    ORDERING_GUARD,
    SHADOW_FAILURE,
    UNEVALUABLE,
)

#: Terminal statuses on the Continuum side that have no production counterpart
#: to compare against, because they describe the shadow run rather than the
#: decision.
_NO_PRODUCTION_COUNTERPART = ("denied",)

#: Scenarios whose planned effects are a *plan*, not a prediction.
#:
#: The release dry run walks the whole publication chain -- build, sign, verify,
#: draft, publish, open the release pull request -- through record-only ports, and
#: production is not expected to perform any of it: a dry run's entire purpose is
#: that nothing is written. Comparing those effects against a read-only release
#: record would report six missing actions against a system that behaved
#: correctly by publishing nothing, and a gate that is always blocked by a
#: scenario that cannot ever agree is a gate nobody trusts.
#:
#: The check is still made, and it is the check that matters: did production
#: publish anything it should not have? A release observed where a dry run
#: planned one is ``extra-action``. Only the *absence* of the planned effects is
#: expected.
#:
#: Named by scenario string rather than imported from :mod:`continuum.shadow.
#: planner`, which would make the comparison plane depend on the decision plane.
#: ``tests/test_shadow_observer.py`` asserts this matches the planner's constant.
PLAN_ONLY_SCENARIOS: Tuple[str, ...] = ("release-planning",)


@dataclasses.dataclass(frozen=True)
class Difference:
    """One classified difference, with everything needed to diagnose it."""

    kind: str
    summary: str
    #: The expected effect, for a missing action or an ordering difference.
    expected: Optional[Effect] = None
    #: The observed effect, for an extra action or an ordering difference.
    observed: Optional[Effect] = None
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "expected": _describe(self.expected),
            "observed": _describe(self.observed),
            "detail": {key: self.detail[key] for key in sorted(self.detail, key=str)},
        }


def _describe(effect: Optional[Effect]) -> Optional[Dict[str, Any]]:
    return effect.describe() if effect is not None else None


@dataclasses.dataclass(frozen=True)
class ParityResult:
    """The verdict for one event."""

    classification: str
    correlation_id: str
    scenario: str
    #: The single-sentence answer for a summary table.
    summary: str
    differences: Tuple[Difference, ...] = ()
    expected_status: str = ""
    observed_status: str = ""
    expected_count: int = 0
    observed_count: int = 0
    matched_count: int = 0
    continuum_sha: str = ""
    config_version: str = ""
    #: Correlation id, repository, and the capture's own links. A verdict with
    #: no way back to the original run is not actionable, so these are always
    #: populated.
    links: Mapping[str, str] = dataclasses.field(default_factory=dict)
    limits: Tuple[str, ...] = ()

    @property
    def agrees(self) -> bool:
        return self.classification in AGREEMENT

    @property
    def divergent(self) -> bool:
        return self.classification in DIVERGENT

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PARITY_SCHEMA,
            "classification": self.classification,
            "correlation_id": self.correlation_id,
            "scenario": self.scenario,
            "summary": self.summary,
            "differences": [item.describe() for item in self.differences],
            "expected_status": self.expected_status,
            "observed_status": self.observed_status,
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "matched_count": self.matched_count,
            "continuum_sha": self.continuum_sha,
            "config_version": self.config_version,
            "links": {key: self.links[key] for key in sorted(self.links, key=str)},
            "limits": list(self.limits),
        }


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


def compare(
    journal: ShadowJournal,
    outcome: Optional[ObservedOutcome],
) -> ParityResult:
    """Classify one event.

    The order of the checks is the order of trust in the evidence. A shadow run
    that did not finish cannot be compared, whatever it recorded on the way. A
    run that finished and reached a terminal decision is compared on that
    decision before it is compared on its actions, because the terminal state is
    what the two systems are actually for.
    """

    correlation = journal.event.correlation_id
    scenario = journal.event.scenario
    expected = journal_effects(journal)
    links = dict(outcome.links) if outcome is not None else {}
    links.setdefault("correlation_id", correlation)
    links.setdefault("continuum_sha", str(journal.engine.get("engine_sha", "")))
    limits = tuple(outcome.limits) if outcome is not None else ()

    def result(
        classification: str,
        summary: str,
        differences: Sequence[Difference] = (),
        **detail: Any,
    ) -> ParityResult:
        return ParityResult(
            classification=classification,
            correlation_id=correlation,
            scenario=scenario,
            summary=summary,
            differences=tuple(differences),
            expected_status=journal.status,
            observed_status=outcome.terminal_status if outcome is not None else "",
            expected_count=len(expected),
            observed_count=len(outcome.effects) if outcome is not None else 0,
            matched_count=int(detail.pop("matched", 0)),
            continuum_sha=str(journal.engine.get("engine_sha", "")),
            config_version=str(journal.config.get("version", "")),
            links=links,
            limits=limits,
        )

    # 1. Did the shadow run itself reach a decision?
    if journal.is_execution_failure:
        return result(
            SHADOW_FAILURE,
            "the shadow run ended as {!r}: {}".format(journal.status, journal.reason),
            [
                Difference(
                    kind=SHADOW_FAILURE,
                    summary=journal.reason or journal.status,
                    detail={
                        "status": journal.status,
                        "errors": [dict(entry) for entry in journal.errors],
                    },
                )
            ],
        )

    if outcome is None:
        return result(
            UNEVALUABLE,
            "no production outcome was captured for this event, so parity cannot "
            "be evaluated",
            [
                Difference(
                    kind=UNEVALUABLE,
                    summary="no captured outcome",
                    detail={"reason": "the capture carries no outcome for this correlation id"},
                )
            ],
        )

    # 2. Is the capture usable at all?
    if not outcome.is_usable or outcome.fidelity == OBSERVED_UNKNOWN:
        return result(
            UNEVALUABLE,
            "the captured outcome is {!r} fidelity, so a difference would be a "
            "claim about the capture rather than about Continuum".format(outcome.fidelity),
            [
                Difference(
                    kind=UNEVALUABLE,
                    summary="capture fidelity is {!r}".format(outcome.fidelity),
                    detail={"limits": list(outcome.limits)},
                )
            ],
        )

    if outcome.correlation_id and outcome.correlation_id != correlation:
        return result(
            UNEVALUABLE,
            "the captured outcome belongs to {} but the journal is for {}".format(
                outcome.correlation_id, correlation
            ),
            [
                Difference(
                    kind=UNEVALUABLE,
                    summary="correlation id mismatch",
                    detail={
                        "outcome": outcome.correlation_id,
                        "journal": correlation,
                    },
                )
            ],
        )

    observed = outcome.effects
    opaque = outcome.opaque_keys if outcome is not None else frozenset()
    left_matched, right_matched = reconcile_targets(expected, observed, opaque=opaque)
    matched = len(left_matched)

    missing = [
        expected[index]
        for index in range(len(expected))
        if index not in set(left_matched)
    ]
    extra = [
        observed[index]
        for index in range(len(observed))
        if index not in set(right_matched)
    ]

    # 3. A scenario whose effects are a plan rather than a prediction.
    #
    #    Checked before the terminal state, because for these scenarios the
    #    journal's status describes the *plan* -- "the release was planned and
    #    suppressed" -- while the outcome describes production's ending. Comparing
    #    them would report "Continuum ended 'ok', production ended 'no-action'" for
    #    every dry run whose tag was never published, which is the single most
    #    common and least interesting thing that can happen to a dry run.
    if scenario in PLAN_ONLY_SCENARIOS:
        return _plan_only_result(
            result, missing, extra, matched, len(expected), len(observed), journal, outcome
        )

    # 4. Terminal state next: a run that merged and a run that recorded a
    #    failure have not diverged in detail.
    terminal = _terminal_difference(journal, outcome)
    if terminal is not None:
        return result(
            TERMINAL_STATE,
            terminal.summary,
            [terminal] + _action_differences(missing, extra, ordering=None),
            matched=matched,
        )

    # 5. The action set, as a *multiset*. Compared this way rather than by the
    #    alignment above, because a pure reordering leaves the alignment with
    #    unmatched entries on both sides: reporting those as a missing action and
    #    an extra one would be wrong, and it would make ``ordering-guard``
    #    unreachable in the only case it exists for.
    same_members = _multiset(expected, opaque) == _multiset(observed, opaque)

    # 6. Same members. Did the order matter?
    if same_members:
        ordering = _ordering_difference(expected, observed)
        if ordering is not None:
            return result(
                ordering.kind,
                ordering.summary,
                [ordering],
                matched=matched,
            )

    if not same_members or missing or extra:
        differences = _action_differences(missing, extra, ordering=None)
        missing_effects = [item for item in differences if item.kind == MISSING_ACTION]
        extra_effects = [item for item in differences if item.kind == EXTRA_ACTION]
        if missing_effects and extra_effects:
            summary = "Continuum would have taken {} action(s) production did not, and production took {} Continuum would not".format(
                len(missing_effects), len(extra_effects)
            )
        elif missing_effects:
            summary = "Continuum would have taken {} action(s) production did not".format(
                len(missing_effects)
            )
        else:
            summary = "production took {} action(s) Continuum would not".format(
                len(extra_effects)
            )
        return result(
            MISSING_ACTION if missing_effects else EXTRA_ACTION,
            summary,
            differences,
            matched=matched,
        )

    # 6. Same members, same order. Anything left is presentation.
    undecidable, note = _presentation_difference(expected, observed, opaque)
    if undecidable:
        return result(
            EXPLAINABLE,
            "the same {} effect(s) in the same order; {}".format(
                len(expected), note
            ),
            [
                Difference(
                    kind=EXPLAINABLE,
                    summary=note,
                    expected=None,
                    observed=None,
                    detail={"fields": "detail fields outside the presentation vocabulary"},
                )
            ],
            matched=matched,
        )

    return result(
        EXACT,
        "Continuum planned {} effect(s) and production took the same {} in the "
        "same order".format(len(expected), len(observed)),
        matched=matched,
    )


def _multiset(
    effects: Sequence[Effect],
    opaque: "frozenset[str] | set[str] | tuple[str, ...]" = (),
) -> Tuple[Tuple[str, str, Tuple[Tuple[str, str], ...]], ...]:
    """Signatures in sorted order, so order is not part of the comparison.

    A multiset rather than a set, because two identical effects are two effects:
    collapsing them would make a capture that wrote the label twice look
    identical to one that wrote it once.
    """

    return tuple(sorted(effect.signature_excluding(opaque) for effect in effects))


def _plan_only_result(
    result: Any,
    missing: Sequence[Effect],
    extra: Sequence[Effect],
    matched: int,
    expected_count: int,
    observed_count: int,
    journal: ShadowJournal,
    outcome: ObservedOutcome,
) -> ParityResult:
    """The verdict for a scenario whose effects are a plan, not a prediction.

    Production was never asked to perform them, so their absence is the
    agreement. Production performing one of them *is* a divergence -- something
    happened on the release surface that a dry run says should not have -- and
    that is checked here rather than assumed away, because "the dry run planned a
    release" and "a release was published" being the same event is exactly the
    bug this plane exists to catch.

    Returned through the caller's ``result`` factory so the correlation id, the
    config version, and the links come from the same place as every other verdict.
    """

    if extra:
        return result(
            EXTRA_ACTION,
            "production performed {} effect(s) on a surface the dry run only planned".format(
                len(extra)
            ),
            _action_differences(missing, extra, ordering=None),
            matched=matched,
        )
    return result(
        EXACT,
        "Continuum planned {} suppressed publication(s) and production published "
        "nothing, which is what a dry run is for".format(expected_count),
        matched=matched,
    )


def _terminal_difference(
    journal: ShadowJournal, outcome: ObservedOutcome
) -> Optional[Difference]:
    """Whether the two disagree about how the event ended.

    A Continuum refusal is not compared against a production terminal state: the
    production system was never asked to do anything in that case, so there is
    nothing to disagree with. Reporting it would turn "Continuum would have
    refused" into "production failed to act", which is a false accusation
    against a system that was behaving correctly by not being invoked.
    """

    if journal.status in _NO_PRODUCTION_COUNTERPART:
        return None
    if not outcome.terminal_status:
        return None
    if outcome.terminal_status == journal.status:
        return None
    return Difference(
        kind=TERMINAL_STATE,
        summary="Continuum ended {!r}; production ended {!r}".format(
            journal.status, outcome.terminal_status
        ),
        detail={
            "expected": journal.status,
            "observed": outcome.terminal_status,
            "observed_detail": outcome.terminal_detail,
        },
    )


def _action_differences(
    missing: Sequence[Effect],
    extra: Sequence[Effect],
    ordering: Optional[Difference],
) -> List[Difference]:
    differences: List[Difference] = []
    for effect in missing:
        differences.append(
            Difference(
                kind=MISSING_ACTION,
                summary="Continuum would have performed {} on {}, and production did not".format(
                    effect.kind, effect.target or "the repository"
                ),
                expected=effect,
            )
        )
    for effect in extra:
        differences.append(
            Difference(
                kind=EXTRA_ACTION,
                summary="production performed {} on {}, and Continuum would not".format(
                    effect.kind, effect.target or "the repository"
                ),
                observed=effect,
            )
        )
    if ordering is not None:
        differences.append(ordering)
    return differences


#: Effects that must happen in this order relative to each other, because one
#: makes the other meaningless without it. The keys are effect kinds; the value
#: lists the kinds that must come first.
ORDERING_CONSTRAINTS: Dict[str, Tuple[str, ...]] = {
    # A provider request that is not recorded cannot be rolled back, so the
    # in-flight slot has to exist before the dispatch goes out.
    "workflow.dispatch": ("comment.create",),
    # A status that claims a verdict nobody has published is a lie.
    "status.create": ("review.create",),
    # Removing a lock before taking it in the same episode is not a repair.
    "label.remove": (),
}


def _ordering_difference(
    expected: Sequence[Effect], observed: Sequence[Effect]
) -> Optional[Difference]:
    """Whether the two orderings differ in a way that matters.

    Independence is the test, and it is what separates this from
    ``explainable``. Two comments on the same pull request can arrive in either
    order. A dispatch whose rollback comment was deleted afterwards cannot.
    """

    kinds = [effect.kind for effect in expected]
    observed_kinds = [effect.kind for effect in observed]
    if kinds == observed_kinds:
        return None

    inversions: List[Dict[str, Any]] = []
    for kind, must_precede in sorted(ORDERING_CONSTRAINTS.items()):
        if kind not in kinds or kind not in observed_kinds:
            continue
        expected_index = kinds.index(kind)
        observed_index = observed_kinds.index(kind)
        for other in must_precede:
            if other not in kinds or other not in observed_kinds:
                continue
            # The *pair's* relative order, not whether each kind kept its absolute
            # position. A third effect moving in between can invert a dependency
            # while both kinds keep their index, and an absolute-index check would
            # miss exactly that case.
            expected_pair = (kinds.index(other), expected_index)
            observed_pair = (observed_kinds.index(other), observed_index)
            if (expected_pair[0] < expected_pair[1]) != (observed_pair[0] < observed_pair[1]):
                inversions.append(
                    {
                        "first": other,
                        "second": kind,
                        "expected": [index + 1 for index in expected_pair],
                        "observed": [index + 1 for index in observed_pair],
                    }
                )
    if not inversions:
        return Difference(
            kind=EXPLAINABLE,
            summary="the effects are independent, so the order carries no decision",
            detail={
                "expected_order": kinds,
                "observed_order": observed_kinds,
            },
        )
    return Difference(
        kind=ORDERING_GUARD,
        summary="the same effects in a different order, and the order matters here",
        detail={
            "inversions": inversions,
            "expected_order": kinds,
            "observed_order": observed_kinds,
        },
    )


#: Detail fields that carry prose or timing. A difference confined to these is
#: a difference in wording, not in decision.
_PRESENTATION_KEYS = frozenset(
    {"text", "body", "message", "title", "reason", "marker", "inputs", "started_at", "ended_at"}
)


def _presentation_difference(
    expected: Sequence[Effect],
    observed: Sequence[Effect],
    opaque: "frozenset[str] | set[str] | tuple[str, ...]" = (),
) -> Tuple[bool, str]:
    """Whether anything outside presentation differs, and a note if so.

    Two return values rather than one, because "the details differ only in
    prose" and "the details are identical" are the difference between
    ``explainable`` and ``exact``. Collapsing them would make ``exact``
    unreachable -- and a report where the best possible verdict is never reached
    is a report nobody can trust to say "these are the same".

    A *matched* effect is a pair, so a genuinely undecidable difference -- one
    that is neither a presentation key nor identical -- is reported as
    explainable with the fields named. It is never silently absorbed.

    ``opaque`` keys are skipped rather than reported: they are already absent from
    both signatures, so counting them again here would make every dispatch land on
    ``explainable`` for a field the capture was never able to read, and the best
    possible verdict would be unreachable for every scenario that dispatches.
    """

    notes: List[str] = []
    decisional: List[str] = []
    for left, right in zip(expected, observed):
        for key in sorted((set(left.detail) | set(right.detail)) - set(opaque)):
            if left.detail.get(key) == right.detail.get(key):
                continue
            note = "{} on {}: expected {!r}, observed {!r}".format(
                left.kind,
                left.target or "the repository",
                left.detail.get(key),
                right.detail.get(key),
            )
            if key in _PRESENTATION_KEYS:
                notes.append(note)
            else:
                decisional.append(note)
    if decisional:
        return True, "; ".join((decisional + notes)[:3])
    if notes:
        return True, "the details differ only in prose and timing ({})".format(notes[0])
    return False, "the details are identical"


# --------------------------------------------------------------------------- #
# The aggregate report
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ParityReport:
    """Every event in a run, and the counts a cutover decision needs."""

    results: Tuple[ParityResult, ...] = ()
    #: The window the evidence covers, so a report is not read as more than it
    #: is.
    window_started_at: str = ""
    window_ended_at: str = ""
    generated_from: str = ""

    def by_classification(self) -> Dict[str, int]:
        counts = {name: 0 for name in CLASSIFICATIONS}
        for result in self.results:
            counts[result.classification] = counts.get(result.classification, 0) + 1
        return counts

    def by_scenario(self) -> Dict[str, Dict[str, int]]:
        counts: Dict[str, Dict[str, int]] = {}
        for result in self.results:
            bucket = counts.setdefault(result.scenario, {})
            bucket[result.classification] = bucket.get(result.classification, 0) + 1
        return counts

    @property
    def divergences(self) -> Tuple[ParityResult, ...]:
        return tuple(result for result in self.results if result.divergent)

    @property
    def is_clean(self) -> bool:
        return not self.divergences and bool(self.results)

    def scenario_classes_covered(self) -> Tuple[str, ...]:
        return tuple(sorted({result.scenario for result in self.results}))

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PARITY_SCHEMA,
            "kind": "report",
            "generated_from": self.generated_from,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "total": len(self.results),
            "by_classification": self.by_classification(),
            "by_scenario": self.by_scenario(),
            "clean": self.is_clean,
            "results": [result.describe() for result in self.results],
        }


def report(
    results: Sequence[ParityResult],
    *,
    generated_from: str = "",
    window_started_at: str = "",
    window_ended_at: str = "",
) -> ParityReport:
    return ParityReport(
        results=tuple(results),
        window_started_at=window_started_at,
        window_ended_at=window_ended_at,
        generated_from=generated_from,
    )


def read_result(document: Mapping[str, Any]) -> ParityResult:
    """Read one verdict back from an artifact.

    The cutover plane reads verdicts a different process wrote, possibly by a
    different Continuum, so the parse is deliberately permissive about *extra*
    fields and strict about the ones the gate branches on: an unknown
    classification is refused rather than treated as benign, because
    "unrecognised" and "agrees" must never collapse into each other.
    """

    if not isinstance(document, Mapping):
        raise ValueError("result_not_a_mapping: a parity result must be an object")
    schema = document.get("schema")
    if schema != PARITY_SCHEMA:
        raise ValueError(
            "unknown_parity_schema: expected {!r}, found {!r}".format(PARITY_SCHEMA, schema)
        )
    classification = str(document.get("classification", ""))
    if classification not in CLASSIFICATIONS:
        raise ValueError(
            "unknown_classification: {!r} is not a parity classification".format(classification)
        )
    return ParityResult(
        classification=classification,
        correlation_id=str(document.get("correlation_id", "")),
        scenario=str(document.get("scenario", "")),
        summary=str(document.get("summary", "")),
        differences=tuple(
            Difference(
                kind=str(entry.get("kind", "")),
                summary=str(entry.get("summary", "")),
                detail={str(key): value for key, value in (entry.get("detail", {}) or {}).items()},
            )
            for entry in document.get("differences", []) or []
            if isinstance(entry, Mapping)
        ),
        expected_status=str(document.get("expected_status", "")),
        observed_status=str(document.get("observed_status", "")),
        expected_count=int(document.get("expected_count", 0) or 0),
        observed_count=int(document.get("observed_count", 0) or 0),
        matched_count=int(document.get("matched_count", 0) or 0),
        continuum_sha=str(document.get("continuum_sha", "")),
        config_version=str(document.get("config_version", "")),
        links={str(key): str(value) for key, value in (document.get("links", {}) or {}).items()},
        limits=tuple(str(item) for item in document.get("limits", []) or []),
    )


def summarize(result: ParityResult) -> str:
    """One line, for a job summary or a table."""

    return "{} {} [{}] {}".format(
        "OK " if result.agrees else "!! ",
        result.classification,
        result.correlation_id,
        result.summary,
    ).rstrip()
