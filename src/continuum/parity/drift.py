"""Turning "the source moved" into "does anyone have to do anything about it".

The distinction this module exists to keep is between *movement* and *drift*. A
tracked source advancing is the normal state of a live repository; what matters
is whether the advance touched anything Continuum has a claim on. So an advance
is not a finding. An advance is a finding when it lands on a path nobody has
classified, and it is a recorded, non-finding no-op when every path it touched
was classified out of scope in advance.

That is the whole rule, and the priority a finding carries follows from the same
table rather than from a separate judgement:

===============================  ========  ==============================
path rule disposition            priority  meaning
===============================  ========  ==============================
``absorbed`` / ``must-port``    ``p0``    Continuum holds a claim here.
                                           An unclassified advance may undo
                                           an absorbed property or add a
                                           gap, and neither is knowable
                                           without a human.
``superseded``                  ``p1``    The earlier classification no
                                           longer describes this path.
                                           Worth recording, not blocking.
``consumer-local`` /            --        A green no-op. The fix belongs
``not-applicable``                          in one consumer's configuration,
                                           or the case cannot arise. These
                                           will advance forever.
no rule at all                  ``p0``    Unclassified, so treated as if it
                                           were ``p0``. A path the ledger
                                           does not mention is a question,
                                           never a pass.
===============================  ========  ==============================

Three properties of the report are load-bearing, and each one is a decision
rather than an omission:

* **Absence is stated, never implied.** An unreadable source is ``unavailable``
  with a reason. It is not counted as clean, and it is not counted as drift
  either, because neither claim is supported.
* **Truncation is not evidence of absence.** A compare response that listed more
  files than were kept makes the source ``p0`` regardless of what the kept files
  were: the unkept ones were never classified.
* **A removed tracked path is drift.** Deleting a file the ledger tracks
  invalidates the classification written for it, and the checker's job is to
  notice that, not to notice that a path no longer appears.

The report is also the input to the promotion gate, and it carries the two
notions those need kept apart: :attr:`DriftReport.clean` says there is no
unclassified drift, while :attr:`DriftReport.can_claim_clean` additionally says
everything was actually looked at. A scan may honestly be drift-clean and still
be unable to make the stronger claim.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import ledger as ledger_module
from .ledger import Ledger, PathRule, Source
from .sources import READABLE, SourceObservation, assert_public

DRIFT_SCHEMA = "continuum.parity-drift/v1"

#: The source was read and its head is the commit the ledger last audited.
IN_SYNC = "in-sync"
#: The source was read and its head is a different commit.
ADVANCED = "advanced"
#: The source was not read. Carries a reason and contributes no verdict.
UNAVAILABLE = "unavailable"

PRIORITY_P0 = "p0"
PRIORITY_P1 = "p1"
PRIORITIES: Tuple[str, ...] = (PRIORITY_P0, PRIORITY_P1)

#: Dispositions that mean Continuum holds a claim on the path, so an unclassified
#: advance there blocks a consumer pin.
BLOCKING_DISPOSITIONS: Tuple[str, ...] = (ledger_module.ABSORBED, ledger_module.MUST_PORT)

#: Path-level outcomes.
CHANGED = "changed"
REMOVED = "removed"
UNCHANGED = "unchanged"


def priority_for(rule: Optional[PathRule]) -> str:
    """The priority an unclassified advance in a path with this rule carries."""

    if rule is None:
        return PRIORITY_P0
    if rule.disposition in ledger_module.NO_TRIAGE_DISPOSITIONS:
        return ""
    if rule.disposition in BLOCKING_DISPOSITIONS:
        return PRIORITY_P0
    return PRIORITY_P1


@dataclasses.dataclass(frozen=True)
class PathVerdict:
    """What happened to one tracked path, and what it means."""

    path: str
    status: str
    disposition: str = ""
    priority: str = ""
    summary: str = ""

    @property
    def triaged(self) -> bool:
        return bool(self.priority)

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "disposition": self.disposition,
            "priority": self.priority,
            "summary": self.summary,
        }


@dataclasses.dataclass(frozen=True)
class SourceDrift:
    """One tracked source's verdict, with the evidence behind it."""

    id: str
    repository: str
    visibility: str
    state: str
    access: str
    verdict: str
    summary: str = ""
    priority: str = ""
    baseline_sha: str = ""
    current_sha: str = ""
    head_sha: str = ""
    paths: Tuple[PathVerdict, ...] = ()
    commits: Tuple[str, ...] = ()
    pull_requests: Tuple[int, ...] = ()
    code: str = ""
    reason: str = ""
    truncated: bool = False
    redacted: Tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.priority == PRIORITY_P0

    @property
    def needs_triage(self) -> bool:
        return bool(self.priority)

    @property
    def private(self) -> bool:
        """Whether this source's metadata may not be published.

        Checked here as well as in :mod:`.sources`: the redaction in the read
        phase protects the observations document, and this protects the report, the
        issue body and the gate verdict. A private source's path list is itself
        private, so it is withheld at every boundary rather than at one.
        """

        return self.visibility == ledger_module.PRIVATE

    def describe(self) -> Dict[str, Any]:
        withheld: List[str] = []
        repository = self.repository
        paths = [item.describe() for item in self.paths]
        commits = list(self.commits)
        pull_requests = list(self.pull_requests)
        if self.private:
            repository = "<redacted:private>"
            paths = []
            commits = []
            pull_requests = []
            withheld = ["repository", "paths", "commits", "pull_requests"]
        return {
            "id": self.id,
            "repository": repository,
            "visibility": self.visibility,
            "state": self.state,
            "access": self.access,
            "verdict": self.verdict,
            "summary": self.summary,
            "priority": self.priority,
            "baseline_sha": self.baseline_sha,
            "current_sha": self.current_sha,
            "head_sha": self.head_sha,
            "paths": paths,
            "commits": commits,
            "pull_requests": pull_requests,
            "code": self.code,
            "reason": "" if self.private else self.reason,
            "truncated": self.truncated,
            "redacted": withheld,
        }


@dataclasses.dataclass(frozen=True)
class DriftReport:
    """Every source's verdict, and the one claim a promotion may make."""

    sources: Tuple[SourceDrift, ...] = ()
    generated_at: str = ""
    continuum_sha: str = ""
    ledger_version: int = 0
    ledger_source: str = ""
    ledger_generated_at: str = ""
    overlay_ids: Tuple[str, ...] = ()

    # -- aggregates --------------------------------------------------------
    @property
    def findings(self) -> Tuple[SourceDrift, ...]:
        return tuple(source for source in self.sources if source.needs_triage)

    @property
    def blocking(self) -> Tuple[SourceDrift, ...]:
        return tuple(source for source in self.sources if source.blocking)

    @property
    def unavailable(self) -> Tuple[SourceDrift, ...]:
        return tuple(source for source in self.sources if source.state == UNAVAILABLE)

    @property
    def unclassified_p0(self) -> int:
        return sum(1 for source in self.sources if source.priority == PRIORITY_P0)

    @property
    def unclassified_p1(self) -> int:
        return sum(1 for source in self.sources if source.priority == PRIORITY_P1)

    @property
    def in_sync(self) -> int:
        return sum(1 for source in self.sources if source.verdict == IN_SYNC)

    @property
    def advanced(self) -> int:
        return sum(1 for source in self.sources if source.verdict == ADVANCED)

    @property
    def clean(self) -> bool:
        """No unclassified drift, and nothing claimed about what was not read.

        A scan over a ledger that has no sources is not clean: an empty audit is
        not an audit that found nothing, and reporting it as one would let a
        truncated ledger read as a passing one.
        """

        return bool(self.sources) and self.unclassified_p0 == 0

    @property
    def can_claim_clean(self) -> bool:
        """``clean``, and every source was actually read.

        The stronger claim, and the one a consumer pin promotion needs: "no
        unclassified P0 drift" is a statement about the whole tracked set, and a
        source that could not be read is a hole in that statement rather than a
        pass through it.

        Unclassified ``p1`` is deliberately not part of this. The claim is about
        P0, a P1 finding is recorded and reported and does not hold a promotion
        hostage, and folding P1 in here would make the gate assert something
        stricter than the sentence it publishes -- which is how a gate stops
        meaning what it says.
        """

        return self.clean and not self.unavailable

    def by_source(self) -> Dict[str, str]:
        return {source.id: source.verdict for source in self.sources}

    def by_disposition(self) -> Dict[str, int]:
        counts: Dict[str, int] = {name: 0 for name in ledger_module.DISPOSITIONS}
        for source in self.sources:
            for path in source.paths:
                if path.disposition in counts:
                    counts[path.disposition] += 1
        return counts

    def describe(self) -> Dict[str, Any]:
        return assert_public(
            {
                "schema": DRIFT_SCHEMA,
                "kind": "report",
                "version": 1,
                "generated_at": self.generated_at,
                "continuum_sha": self.continuum_sha,
                "ledger_version": self.ledger_version,
                "ledger_source": self.ledger_source,
                "ledger_generated_at": self.ledger_generated_at,
                "overlay_ids": list(self.overlay_ids),
                "totals": {
                    "sources": len(self.sources),
                    "in_sync_sources": self.in_sync,
                    "advanced": self.advanced,
                    "unavailable_sources": len(self.unavailable),
                },
                "unclassified_p0": self.unclassified_p0,
                "unclassified_p1": self.unclassified_p1,
                "unavailable_sources": len(self.unavailable),
                "in_sync_sources": self.in_sync,
                "clean": self.clean,
                "can_claim_clean": self.can_claim_clean,
                "by_source": self.by_source(),
                "by_disposition": self.by_disposition(),
                "sources": [source.describe() for source in self.sources],
            },
            where="drift report",
        )


# --------------------------------------------------------------------------- #
# The computation
# --------------------------------------------------------------------------- #


def evaluate(source: Source, observation: SourceObservation) -> SourceDrift:
    """One source's verdict, from one read.

    The unavailable branch comes first: a source nobody could look at has no
    path verdicts at all, because a path verdict would be a statement about a
    comparison that never happened.

    The private branch comes second, and it is the reason this function is where
    the privacy rule lives rather than only in :mod:`.sources`. A private
    source's advance cannot be classified from data that may be published: its
    paths are private, so no rule can be applied to them without disclosing them,
    and the artifacts that follow -- the report, the issue body, the gate verdict --
    are all published. So an advance in a private source is reported as
    unclassified ``p0`` with its content withheld, and the only way to clear it is
    for the operator who holds the credential to review it and advance the audit
    range in their overlay. Failing towards "a human has to look" is the right
    direction to be wrong in, and it is also the only one available.
    """

    base = dict(
        id=source.id,
        repository=source.repository,
        visibility=source.visibility,
        state=observation.state,
        access=observation.access,
        baseline_sha=source.audit.baseline_sha,
        current_sha=source.audit.current_sha,
        head_sha=observation.head_sha,
        code=observation.code,
        reason=observation.reason,
        truncated=observation.truncated,
    )

    if observation.state != READABLE:
        return SourceDrift(
            verdict=UNAVAILABLE,
            summary="not read ({}); this source is unaccounted for, not clean".format(
                observation.code or "no reason recorded"
            ),
            **base,
        )

    if not observation.advanced:
        return SourceDrift(
            verdict=IN_SYNC,
            summary="head is the audited commit {}".format(observation.head_sha[:12]),
            **base,
        )

    if source.private:
        return SourceDrift(
            verdict=ADVANCED,
            priority=PRIORITY_P0,
            summary=(
                "{} advanced to {}; this source is private, so the advance cannot be "
                "classified from publishable data. Review it with the configured "
                "credential and advance the audit range in the overlay".format(
                    source.id, observation.head_sha[:12]
                )
            ),
            redacted=("repository", "paths", "commits", "pull_requests"),
            **base,
        )

    verdicts, no_triage, blocking, advisory = _classify(source, observation)
    commits = tuple(commit.sha for commit in observation.commits)
    pull_requests = observation.pull_requests

    if blocking:
        return SourceDrift(
            verdict=ADVANCED,
            priority=PRIORITY_P0,
            summary=(
                "{} advanced to {} with {} unclassified change(s) in paths Continuum "
                "has a claim on".format(
                    source.id, observation.head_sha[:12], blocking
                )
            ),
            paths=verdicts,
            commits=commits,
            pull_requests=pull_requests,
            **base,
        )
    if observation.truncated:
        # Checked before the advisory branch, and that ordering is the point: a
        # truncated compare means files were listed and not kept, so a source with
        # visible P1 changes still has unseen ones. Reporting P1 there would let
        # the cap decide the severity of a source nobody read in full.
        return SourceDrift(
            verdict=ADVANCED,
            priority=PRIORITY_P0,
            summary=(
                "{} advanced to {} and the change list was truncated; the changes "
                "that were not read cannot be classified".format(
                    source.id, observation.head_sha[:12]
                )
            ),
            paths=verdicts,
            commits=commits,
            pull_requests=pull_requests,
            **base,
        )
    if advisory:
        return SourceDrift(
            verdict=ADVANCED,
            priority=PRIORITY_P1,
            summary=(
                "{} advanced to {} with {} change(s) in a path whose earlier "
                "classification is superseded".format(
                    source.id, observation.head_sha[:12], advisory
                )
            ),
            paths=verdicts,
            commits=commits,
            pull_requests=pull_requests,
            **base,
        )
    return SourceDrift(
        verdict=ADVANCED,
        summary=(
            "{} advanced to {} in {} path(s) classified out of scope; no action".format(
                source.id, observation.head_sha[:12], no_triage
            )
        ),
        paths=verdicts,
        commits=commits,
        pull_requests=pull_requests,
        **base,
    )


def _classify(
    source: Source, observation: SourceObservation
) -> Tuple[Tuple[PathVerdict, ...], int, int, int]:
    """Classify every changed path, and count the outcomes."""

    verdicts: List[PathVerdict] = []
    no_triage = 0
    blocking = 0
    advisory = 0
    for change in observation.changes:
        rule = source.classify(change.path)
        if rule is None:
            verdicts.append(
                PathVerdict(
                    path=change.path,
                    status=REMOVED if change.status == "removed" else CHANGED,
                    disposition="",
                    priority=PRIORITY_P0,
                    summary=(
                        "no tracked path rule covers {!r}; an unclassified path is "
                        "treated as automation-relevant".format(change.path)
                    ),
                )
            )
            blocking += 1
            continue
        priority = priority_for(rule)
        if not priority:
            verdicts.append(
                PathVerdict(
                    path=change.path,
                    status=REMOVED if change.status == "removed" else CHANGED,
                    disposition=rule.disposition,
                    summary="classified {}: an advance here is a no-op".format(
                        rule.disposition
                    ),
                )
            )
            no_triage += 1
            continue
        if change.status == "removed":
            # The classification was written for a file that no longer exists.
            # Keeping the same priority rather than downgrading it is the point:
            # the rule may have been the only thing keeping that path audited.
            verdicts.append(
                PathVerdict(
                    path=change.path,
                    status=REMOVED,
                    disposition=rule.disposition,
                    priority=PRIORITY_P0,
                    summary=(
                        "the tracked path was removed upstream, so its {} "
                        "classification no longer describes anything".format(
                            rule.disposition
                        )
                    ),
                )
            )
            blocking += 1
            continue
        verdicts.append(
            PathVerdict(
                path=change.path,
                status=CHANGED,
                disposition=rule.disposition,
                priority=priority,
                summary=(
                    "classified {}; the classification was made at {}, and an "
                    "advance past that commit is not covered by it".format(
                        rule.disposition, source.audit.current_sha[:12]
                    )
                ),
            )
        )
        if priority == PRIORITY_P0:
            blocking += 1
        else:
            advisory += 1
    return tuple(verdicts), no_triage, blocking, advisory


def compute(
    ledger: Ledger,
    observations: Sequence[SourceObservation],
    *,
    generated_at: str = "",
    continuum_sha: str = "",
) -> DriftReport:
    """Every tracked source's verdict.

    A source in the ledger with no observation is ``unavailable`` rather than
    absent, because a scan that silently skipped it would report a smaller
    tracked set than the ledger declares -- and a smaller tracked set is exactly
    how a source stops being audited without anyone deciding that it should.
    """

    by_id = {observation.id: observation for observation in observations}
    verdicts: List[SourceDrift] = []
    for source in ledger.sources:
        observation = by_id.get(source.id)
        if observation is None:
            verdicts.append(
                SourceDrift(
                    id=source.id,
                    repository=source.repository,
                    visibility=source.visibility,
                    state=UNAVAILABLE,
                    access=ledger_module.ACCESS_NONE,
                    verdict=UNAVAILABLE,
                    code="no-observation",
                    summary="no observation was recorded for this source",
                    reason="the read phase did not report on it",
                    baseline_sha=source.audit.baseline_sha,
                    current_sha=source.audit.current_sha,
                )
            )
            continue
        verdicts.append(evaluate(source, observation))
    return DriftReport(
        sources=tuple(verdicts),
        generated_at=generated_at,
        continuum_sha=continuum_sha,
        ledger_version=ledger.version,
        ledger_source=ledger.source_path,
        ledger_generated_at=ledger.generated_at,
        overlay_ids=ledger.overlay_ids,
    )


def unanswered_incidents(ledger: Ledger) -> Tuple[Tuple[str, str], ...]:
    """``(source id, incident id)`` for classifications with no answer here.

    Not a drift signal: a ``must-port`` incident with no implementation reference
    is a legitimate intermediate state while the work is in flight. It is
    reported so a reviewer sees it, and so the promotion gate can name it as
    something to look at rather than leaving it implicit in a count.
    """

    return tuple(
        (source.id, incident.id)
        for source in ledger.sources
        for incident in source.incidents
        if not incident.answered
    )


def summary_line(report: DriftReport) -> str:
    """One line for a job summary.

    The last clause is the one that matters, and it says which claim holds. "No
    P0" and "clean over the tracked set" are different statements, and a summary
    that printed only the first would read as a pass for a run that read nothing.
    """

    if report.can_claim_clean:
        verdict = "clean over the tracked set"
    elif report.clean:
        verdict = (
            "no unclassified P0, but {} source(s) unreadable -- unaccounted for, "
            "not clean".format(len(report.unavailable))
        )
    else:
        verdict = "not clean"
    return (
        "parity drift: {} source(s) tracked, {} in sync, {} advanced, {} unavailable; "
        "{} unclassified P0, {} unclassified P1; {}".format(
            len(report.sources),
            report.in_sync,
            report.advanced,
            len(report.unavailable),
            report.unclassified_p0,
            report.unclassified_p1,
            verdict,
        )
    )


def render_markdown(report: DriftReport) -> str:
    """The report as a reviewer reads it, in the same order the gate reads it.

    Rendered from the report rather than re-derived, so the summary and the gate
    cannot disagree about what was found.
    """

    lines = [
        "## Cross-repository parity drift",
        "",
        summary_line(report),
        "",
    ]
    if not report.sources:
        lines += ["No source is tracked, so nothing was audited.", ""]
        return "\n".join(lines)

    lines += [
        "| Source | Verdict | Priority | Head | Baseline | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for source in report.sources:
        lines.append(
            "| `{}` | {} | {} | `{}` | `{}` | {} |".format(
                source.id,
                source.verdict,
                source.priority or "--",
                source.head_sha[:12] or "--",
                source.current_sha[:12] or "--",
                _cell(source.summary),
            )
        )
    lines.append("")

    for source in report.findings:
        lines += [
            "### `{}` ({})".format(source.id, source.priority),
            "",
            source.summary,
            "",
        ]
        if source.pull_requests:
            lines.append(
                "Upstream pull requests: "
                + ", ".join("#{}".format(number) for number in source.pull_requests)
            )
            lines.append("")
        if source.commits:
            lines.append(
                "Commits ({}): {}".format(
                    len(source.commits),
                    ", ".join("`{}`".format(sha[:12]) for sha in source.commits[:10]),
                )
            )
            lines.append("")
        if source.paths:
            lines += [
                "| Path | Change | Classification | Meaning |",
                "| --- | --- | --- | --- |",
            ]
            for path in source.paths:
                lines.append(
                    "| `{}` | {} | {} | {} |".format(
                        path.path,
                        path.status,
                        path.disposition or "unclassified",
                        _cell(path.summary),
                    )
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


__all__ = [
    "ADVANCED",
    "BLOCKING_DISPOSITIONS",
    "CHANGED",
    "DriftReport",
    "DRIFT_SCHEMA",
    "IN_SYNC",
    "PathVerdict",
    "PRIORITY_P0",
    "PRIORITY_P1",
    "PRIORITIES",
    "REMOVED",
    "SourceDrift",
    "UNAVAILABLE",
    "compute",
    "evaluate",
    "priority_for",
    "render_markdown",
    "summary_line",
    "unanswered_incidents",
]
