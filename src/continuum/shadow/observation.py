"""Normalizing what NanoDictate actually did, into the shadow journal's vocabulary.

The comparison in :mod:`continuum.shadow.parity` is only as good as the two
sides speaking the same language, and the two sides do not arrive in the same
shape. A journal says "record-only: this effect was suppressed". A production
outcome is a pile of API responses: a label that is present, a check run that
concluded, a workflow run that completed, a comment body that mentions a
dispatch. Somebody has to turn the second into the first, and the rule that
matters is what counts as *evidence* of an effect.

So an observed outcome is never inferred from a diff. It is reconstructed from
facts the capture carries about what happened, and it has to be reconstructible
months later by a reader. That rules out anything of the form "the label was not
there before and is there now" unless both observations are in the capture: a
diff is a claim about a moment nobody can go back to, and the whole point of
capturing state at event time was to avoid needing it.

The consequence worth stating: an observed outcome that cannot be reconstructed
is reported as *unknown*, not as *nothing happened*. "Unknown" and "no action"
are different findings and the comparison treats them differently, because a
capture that is missing evidence is a defect in the capture and not evidence
about Continuum.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .effects import Effect, effect_spec
from .event import ShadowEvent
from .journal import ShadowJournal

OUTCOME_SCHEMA = "continuum.shadow-outcome/v1"

#: How confident a capture is about what production did.
OBSERVED_EXACT = "exact"
OBSERVED_PARTIAL = "partial"
OBSERVED_UNKNOWN = "unknown"


class OutcomeError(ValueError):
    """The captured outcome is not usable for a comparison."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


@dataclasses.dataclass(frozen=True)
class ObservedOutcome:
    """What NanoDictate is recorded as having done about one event.

    ``fidelity`` is the important field. ``exact`` means the capture enumerates
    every effect; ``partial`` means it enumerates some and the rest are unknown;
    ``unknown`` means it enumerates none, in which case the comparison refuses
    to draw a conclusion rather than reporting a missing action that production
    may well have taken.
    """

    correlation_id: str
    #: The window the capture covers. A production outcome is a *window*, not an
    #: instant: the dispatch happened, then the label, then the review. Anything
    #: inside the window is attributed to this event.
    window_started_at: str = ""
    window_ended_at: str = ""
    effects: Tuple[Effect, ...] = ()
    terminal_status: str = ""
    terminal_detail: str = ""
    fidelity: str = OBSERVED_UNKNOWN
    #: What the capture could not see, stated rather than left blank.
    limits: Tuple[str, ...] = ()
    #: Detail keys this capture could not read from the observed side. The
    #: comparison drops them from *both* sides rather than reporting every
    #: effect that has one as a difference; see
    #: :meth:`continuum.shadow.effects.Effect.signature_excluding`.
    unobservable: Tuple[str, ...] = ()
    #: Links back to the real run, so a report can be acted on.
    links: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.fidelity in (OBSERVED_EXACT, OBSERVED_PARTIAL)

    @property
    def opaque_keys(self) -> frozenset:
        """The detail keys this capture could not see, as a set."""

        return frozenset(self.unobservable)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": OUTCOME_SCHEMA,
            "correlation_id": self.correlation_id,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            # ``actions``, not ``effects``: this is the wire format
            # :func:`outcome_from_payload` reads, and it is what ``capture`` writes
            # and ``replay`` compares. The two names drifted once, and the drift was
            # invisible -- a document written under one name and read under the
            # other produced an outcome with no actions and, worse, read back as
            # ``unknown`` fidelity because the reader infers fidelity from the
            # presence of the key. A capture of production's behaviour that silently
            # came back empty is the one failure this whole plane cannot have.
            "actions": [effect.describe() for effect in self.effects],
            "terminal_status": self.terminal_status,
            "terminal_detail": self.terminal_detail,
            "fidelity": self.fidelity,
            "limits": list(self.limits),
            "unobservable": list(self.unobservable),
            "links": {key: self.links[key] for key in sorted(self.links, key=str)},
        }


def outcome_from_payload(
    payload: Mapping[str, Any],
    event: Optional[ShadowEvent] = None,
) -> ObservedOutcome:
    """Normalize a captured production outcome.

    The payload's ``actions`` list is the capture's own claim about what
    production did, expressed in the shared effect vocabulary. Anything that is
    not in that vocabulary is rejected rather than passed through, because an
    unrecognised action cannot be compared and must not be silently dropped --
    dropping it would turn "production did something we do not model" into
    "production did nothing extra", which is the most dangerous possible
    misreading of a parity report.
    """

    if not isinstance(payload, Mapping):
        raise OutcomeError("outcome_not_a_mapping", "Captured outcome must be an object.")

    correlation = _text(payload.get("correlation_id")) or (event.correlation_id if event else "")
    if not correlation:
        raise OutcomeError(
            "missing_correlation_id",
            "A captured outcome must name the event it belongs to.",
        )

    effects: List[Effect] = []
    limits: List[str] = []
    for entry in _sequence(payload.get("actions")):
        if not isinstance(entry, Mapping):
            limits.append("an action that is not an object was dropped")
            continue
        kind = _text(entry.get("kind"))
        if kind not in _known_kinds():
            raise OutcomeError(
                "unknown_effect_kind",
                "The captured outcome names action {!r}, which is not a "
                "classified external mutation.".format(kind),
            )
        detail = entry.get("detail")
        effects.append(
            Effect(
                kind=kind,
                target=_text(entry.get("target")),
                detail=dict(detail) if isinstance(detail, Mapping) else {},
                suppressed=False,
                reason="captured from production",
            )
        )

    limits.extend(_text(item) for item in _sequence(payload.get("limits")) if _text(item))
    unobservable = tuple(
        sorted(
            _text(item)
            for item in _sequence(payload.get("unobservable"))
            if _text(item)
        )
    )
    fidelity = _text(payload.get("fidelity")) or (
        OBSERVED_EXACT if "actions" in payload else OBSERVED_UNKNOWN
    )
    if fidelity not in (OBSERVED_EXACT, OBSERVED_PARTIAL, OBSERVED_UNKNOWN):
        raise OutcomeError(
            "unknown_fidelity",
            "{!r} is not a capture fidelity.".format(fidelity),
        )

    links: Dict[str, str] = {}
    raw_links = payload.get("links")
    if isinstance(raw_links, Mapping):
        links = {str(key): _text(value) for key, value in raw_links.items() if _text(value)}

    return ObservedOutcome(
        correlation_id=correlation,
        window_started_at=_text(payload.get("window_started_at")),
        window_ended_at=_text(payload.get("window_ended_at")),
        effects=tuple(effects),
        terminal_status=_text(payload.get("terminal_status")),
        terminal_detail=_text(payload.get("terminal_detail")),
        fidelity=fidelity,
        limits=tuple(limits),
        unobservable=unobservable,
        links=links,
    )


def _known_kinds() -> frozenset:
    from .effects import EFFECT_REGISTRY

    return frozenset(EFFECT_REGISTRY)


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def observed_from_effects(
    correlation_id: str,
    effects: Sequence[Any],
    *,
    terminal_status: str = "",
    terminal_detail: str = "",
    fidelity: str = OBSERVED_EXACT,
    links: Optional[Mapping[str, str]] = None,
    limits: Sequence[str] = (),
    unobservable: Sequence[str] = (),
) -> ObservedOutcome:
    """Build an observed outcome from a list of effects or effect documents.

    Takes the effects themselves rather than their kinds, because an outcome
    compared by target and detail cannot be reconstructed from a list of kind
    names -- and the replay plane needs exactly the full form, to ask whether a
    second run over the same capture plans the same things. Effects and
    documents are both accepted so a caller can pass either a journal's recorded
    effects or the artifact it was read back from.
    """

    rebuilt: List[Effect] = []
    for entry in effects:
        if isinstance(entry, Effect):
            rebuilt.append(entry)
            continue
        if isinstance(entry, Mapping):
            document = dict(entry)
            rebuilt.append(
                Effect(
                    kind=_text(document.get("kind")),
                    target=_text(document.get("target")),
                    detail=dict(document.get("detail") or {}),
                    suppressed=False,
                    reason="captured from production",
                )
            )
            continue
        raise OutcomeError(
            "effect_not_an_effect",
            "{!r} is neither an Effect nor an effect document.".format(entry),
        )
    return ObservedOutcome(
        correlation_id=correlation_id,
        effects=tuple(rebuilt),
        terminal_status=terminal_status,
        terminal_detail=terminal_detail,
        fidelity=fidelity,
        limits=tuple(_text(item) for item in limits if _text(item)),
        unobservable=tuple(sorted(_text(item) for item in unobservable if _text(item))),
        links=dict(links or {}),
    )


def observed_from_kinds(
    correlation_id: str,
    kinds: Sequence[str],
    *,
    targets: Optional[Mapping[str, str]] = None,
    terminal_status: str = "",
    fidelity: str = OBSERVED_PARTIAL,
    links: Optional[Mapping[str, str]] = None,
) -> ObservedOutcome:
    """An outcome from a list of action *names* and nothing else.

    The fidelity is ``partial`` unless a caller asserts otherwise, because a
    capture that recorded which actions ran but not what they touched cannot
    support a claim about a missing target. A partial capture still compares --
    it just cannot claim a difference it did not observe.
    """

    return observed_from_effects(
        correlation_id,
        [
            {
                "kind": kind,
                "target": (targets or {}).get(kind, ""),
                "detail": {},
            }
            for kind in kinds
        ],
        terminal_status=terminal_status,
        fidelity=fidelity,
        links=links,
        limits=() if fidelity != OBSERVED_PARTIAL else ("the capture recorded action names only",),
    )


def assert_in_vocabulary(effect: Effect) -> Effect:
    """Re-check an effect against the registry.

    Called by the comparison on both sides, so a journal written by a newer
    Continuum that knows an effect this one does not is reported as a version
    difference rather than as a missing action.
    """

    effect_spec(effect.kind)
    return effect


def reconcile_targets(
    expected: Sequence[Effect],
    observed: Sequence[Effect],
    *,
    opaque: "frozenset[str] | set[str] | tuple[str, ...]" = (),
) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """A longest-common-subsequence alignment of two effect sequences.

    Alignment rather than set comparison, because the issue asks for ordering
    differences to be their own classification. Two sequences with the same
    members in a different order are not the same as two sequences with
    different members, and only an alignment tells those apart.

    ``opaque`` names detail keys the capture could not read; see
    :meth:`continuum.shadow.effects.Effect.signature_excluding` for why they are
    dropped from both sides rather than from the observed one.
    """

    left = [effect.signature_excluding(opaque) for effect in expected]
    right = [effect.signature_excluding(opaque) for effect in observed]
    rows, columns = len(left), len(right)
    table = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for column in range(columns - 1, -1, -1):
            if left[row] == right[column]:
                table[row][column] = table[row + 1][column + 1] + 1
            else:
                table[row][column] = max(table[row + 1][column], table[row][column + 1])

    matched: List[Tuple[int, int]] = []
    row = column = 0
    while row < rows and column < columns:
        if left[row] == right[column]:
            matched.append((row, column))
            row += 1
            column += 1
        elif table[row + 1][column] >= table[row][column + 1]:
            row += 1
        else:
            column += 1

    left_matched = tuple(item[0] for item in matched)
    right_matched = tuple(item[1] for item in matched)
    return left_matched, right_matched


def journal_effects(journal: ShadowJournal) -> Tuple[Effect, ...]:
    """The effects a journal planned, in decision order.

    ``actions`` rather than ``suppressed``: both lists are the same sequence, but
    ``actions`` is the one that states intent, and the comparison is about what
    Continuum would have done.
    """

    return tuple(journal.actions)


def describe_effect(effect: Effect) -> Dict[str, Any]:
    document = effect.describe()
    document["signature"] = [
        list(effect.signature[0:2]) + [list(pair) for pair in effect.signature[2]]
    ]
    return document
