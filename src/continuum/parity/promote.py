"""The consumer pin promotion gate.

A consumer pins an immutable Continuum commit (contract §9.1), and promoting
that pin is the moment a parity gap stops being an internal note and starts being
somebody's runtime. So the promotion is the one place where "we classified
everything we looked at" has to be a checked statement rather than an assumption,
and this module is the statement.

The claim is deliberately narrow and deliberately quotable:

    no unclassified P0 drift

It is emitted only when it is true of the *whole* tracked set, which means both
halves of the report have to hold: no unclassified ``p0`` finding, and no source
that could not be read. A scan with one unreadable private source cannot make the
claim, and that is the intended asymmetry -- the checker keeps auditing what it
can, the gate refuses to speak for what it could not see, and neither behaviour
blocks the other.

The verdict is a value. It names the consumer, the pin being moved from and to,
and every blocker individually, so a refusal says what would have to change
rather than only that something did.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Mapping, Tuple

from .drift import PRIORITY_P0, PRIORITY_P1, UNAVAILABLE, DriftReport
from .sources import assert_public

PROMOTION_SCHEMA = "continuum.parity-promotion/v1"

#: The exact sentence a passing verdict asserts. Named once, so the gate, the
#: document, and the test that reads it cannot drift into three spellings.
NO_UNCLASSIFIED_P0_CLAIM = "no unclassified P0 drift"

_PIN = re.compile(r"^[0-9a-f]{40}$")


class PromotionError(ValueError):
    """A promotion request that cannot be evaluated at all.

    Distinct from a verdict that evaluates to "not approved": a request naming a
    pin that is not an immutable commit is malformed, and reporting that as drift
    would blame a tracked source for a typo in the caller's own input.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.detail = message


@dataclasses.dataclass(frozen=True)
class PromotionVerdict:
    """Whether a consumer pin may move, and on what basis."""

    consumer: str
    approved: bool
    claim: str
    from_pin: str = ""
    to_pin: str = ""
    blockers: Tuple[str, ...] = ()
    advisories: Tuple[str, ...] = ()
    drift_generated_at: str = ""
    ledger_version: int = 0
    continuum_sha: str = ""
    unclassified_p0: int = 0
    unclassified_p1: int = 0
    unavailable_sources: int = 0

    @property
    def asserts_claim(self) -> bool:
        """Whether this document makes the claim rather than merely testing it."""

        return self.approved and self.claim == NO_UNCLASSIFIED_P0_CLAIM

    def describe(self) -> Dict[str, Any]:
        return assert_public(
            {
                "schema": PROMOTION_SCHEMA,
                "kind": "promotion",
                "version": 1,
                "consumer": self.consumer,
                "approved": self.approved,
                "claim": self.claim,
                "from_pin": self.from_pin,
                "to_pin": self.to_pin,
                "blockers": list(self.blockers),
                "advisories": list(self.advisories),
                "drift_generated_at": self.drift_generated_at,
                "ledger_version": self.ledger_version,
                "continuum_sha": self.continuum_sha,
                "unclassified_p0": self.unclassified_p0,
                "unclassified_p1": self.unclassified_p1,
                "unavailable_sources": self.unavailable_sources,
            },
            where="promotion verdict",
        )


def promote(
    report: DriftReport,
    *,
    consumer: str,
    from_pin: str = "",
    to_pin: str = "",
) -> PromotionVerdict:
    """Judge a consumer pin promotion against a drift report."""

    name = str(consumer or "").strip()
    if not name:
        raise PromotionError("missing_consumer", "name the consumer whose pin is moving")
    if not report.sources:
        raise PromotionError(
            "empty_ledger",
            "the drift report covers no tracked source, so it cannot support a "
            "claim about the tracked set",
        )
    origin = str(from_pin or "").strip()
    target = str(to_pin or "").strip()
    for label, value in (("from_pin", origin), ("to_pin", target)):
        if value and not _PIN.match(value):
            raise PromotionError(
                "malformed_pin",
                "{} must be a full commit sha; a branch ref would make the "
                "promotion depend on whatever the ref holds later".format(label),
            )

    blockers: List[str] = []
    advisories: List[str] = []
    for source in report.sources:
        if source.priority == PRIORITY_P0:
            blockers.append(
                "unclassified P0 drift in {}: {}".format(source.id, source.summary)
            )
        elif source.priority == PRIORITY_P1:
            advisories.append(
                "unclassified P1 drift in {}: {}".format(source.id, source.summary)
            )
        if source.state == UNAVAILABLE:
            blockers.append(
                "{} was not read ({}); the tracked set is not fully audited, so no "
                "claim can be made for it".format(source.id, source.code or "no reason recorded")
            )

    approved = not blockers
    return PromotionVerdict(
        consumer=name,
        approved=approved,
        claim=NO_UNCLASSIFIED_P0_CLAIM if approved else "",
        from_pin=origin,
        to_pin=target,
        blockers=tuple(blockers),
        advisories=tuple(advisories),
        drift_generated_at=report.generated_at,
        ledger_version=report.ledger_version,
        continuum_sha=report.continuum_sha,
        unclassified_p0=report.unclassified_p0,
        unclassified_p1=report.unclassified_p1,
        unavailable_sources=len(report.unavailable),
    )


def summary_line(verdict: PromotionVerdict) -> str:
    """One line for a job summary."""

    if verdict.approved:
        return "promotion of {} to {}: approved -- {}".format(
            verdict.consumer, verdict.to_pin or "the current pin", verdict.claim
        )
    return "promotion of {} to {}: refused -- {} unclassified P0, {} unreadable".format(
        verdict.consumer, verdict.to_pin or "the current pin", verdict.unclassified_p0, verdict.unavailable_sources
    )


def report_from_document(document: Any) -> DriftReport:
    """Rebuild a drift report from its own artifact, for a gate run on a file.

    Permissive about the fields it does not use and strict about the aggregate
    counts, because the counts are what the verdict branches on: a document whose
    recorded ``unclassified_p0`` disagrees with its own findings must not be
    silently believed, or a stale artifact would authorise a promotion.
    """

    from .drift import SourceDrift, PathVerdict

    if not isinstance(document, Mapping):
        raise PromotionError("not_a_mapping", "a drift report must be an object")
    from .drift import DRIFT_SCHEMA

    if document.get("schema") != DRIFT_SCHEMA:
        raise PromotionError(
            "unknown_schema",
            "expected {!r}, found {!r}".format(DRIFT_SCHEMA, document.get("schema")),
        )
    sources: List[SourceDrift] = []
    for index, entry in enumerate(document.get("sources") or ()):
        if not isinstance(entry, Mapping):
            raise PromotionError("malformed_report", "source {} is not an object".format(index))
        sources.append(
            SourceDrift(
                id=str(entry.get("id", "")),
                repository=str(entry.get("repository", "")),
                visibility=str(entry.get("visibility", "public")),
                state=str(entry.get("state", "")),
                access=str(entry.get("access", "")),
                verdict=str(entry.get("verdict", "")),
                summary=str(entry.get("summary", "")),
                priority=str(entry.get("priority", "")),
                baseline_sha=str(entry.get("baseline_sha", "")),
                current_sha=str(entry.get("current_sha", "")),
                head_sha=str(entry.get("head_sha", "")),
                paths=tuple(
                    PathVerdict(
                        path=str(path.get("path", "")),
                        status=str(path.get("status", "")),
                        disposition=str(path.get("disposition", "")),
                        priority=str(path.get("priority", "")),
                        summary=str(path.get("summary", "")),
                    )
                    for path in entry.get("paths") or ()
                    if isinstance(path, Mapping)
                ),
                commits=tuple(str(commit) for commit in entry.get("commits") or ()),
                pull_requests=tuple(
                    int(number) for number in entry.get("pull_requests") or ()
                ),
                code=str(entry.get("code", "")),
                reason=str(entry.get("reason", "")),
                truncated=bool(entry.get("truncated", False)),
                redacted=tuple(str(item) for item in entry.get("redacted") or ()),
            )
        )
    rebuilt = DriftReport(
        sources=tuple(sources),
        generated_at=str(document.get("generated_at", "")),
        continuum_sha=str(document.get("continuum_sha", "")),
        ledger_version=int(document.get("ledger_version", 0) or 0),
        ledger_source=str(document.get("ledger_source", "")),
        ledger_generated_at=str(document.get("ledger_generated_at", "")),
        overlay_ids=tuple(str(item) for item in document.get("overlay_ids") or ()),
    )
    declared = document.get("unclassified_p0")
    if declared is not None and int(declared) != rebuilt.unclassified_p0:
        raise PromotionError(
            "report_self_inconsistent",
            "the report declares {} unclassified P0 but its own findings count {}".format(
                int(declared), rebuilt.unclassified_p0
            ),
        )
    return rebuilt


__all__ = [
    "NO_UNCLASSIFIED_P0_CLAIM",
    "PROMOTION_SCHEMA",
    "PromotionError",
    "PromotionVerdict",
    "promote",
    "report_from_document",
    "summary_line",
]
