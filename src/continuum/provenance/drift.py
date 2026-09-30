"""Drift between what a source repository did and what Continuum classified.

The check answers one question per source: has the repository moved since the
commit its parity claims were audited against, and if so, does every path it
touched already have a classification whose scope still covers that path?

What comes back is not a boolean. It is a set of readings with an explicit status
each, because the three ways of not being clean are three different problems with
three different responses:

``unchanged``
    The head is the baseline. No finding, and nothing to publish.
``classified-advance``
    The head moved and every tracked path it touched is covered by a persistent
    disposition. This is a *finding* -- somebody upstream made a change, and the
    ledger should say so -- but it is not drift, and it is the thing that keeps a
    busy upstream repository from producing an issue on every run.
``unclassified-drift``
    A tracked path moved with a disposition that does not survive the move, or with
    none at all. Someone has to look at it, which is the entire point.
``unavailable`` / ``unreadable``
    The reading did not complete. Reported as its own status so it can never be
    mistaken for a clean source, and so the audit that reports it stays loud about
    the fact that it did not check what it was going to check.

Nothing in this module reads file contents or pull-request bodies. The evidence it
carries is SHAs, paths, blob-relative change statuses and PR numbers: what moved
and how far, never what it said. A private source is read through its own
least-privilege credential and is never read at all when that credential is not
configured, which is reported as ``unavailable`` rather than being worked around
with a broader token.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .ledger import (
    PERSISTENT_DISPOSITIONS,
    ProvenanceLedger,
    TrackedSource,
    load_provenance,
)

# Per-source statuses.
UNCHANGED = "unchanged"
CLASSIFIED_ADVANCE = "classified-advance"
UNCLASSIFIED_DRIFT = "unclassified-drift"
UNAVAILABLE = "unavailable"
UNREADABLE = "unreadable"

#: Statuses that mean the audit did not establish anything about the source. They
#: are never counted as clean, and a report that contains one is ``incomplete``.
UNRESOLVED_STATUSES = (UNAVAILABLE, UNREADABLE)

#: Report-level verdicts. ``incomplete`` is not a softer ``clean``: it means the
#: audit could not see part of what it was asked to audit.
CLEAN = "clean"
DRIFTED = "drifted"
INCOMPLETE = "incomplete"

#: Why an individual path is unclassified. Separated from the status so a reader can
#: tell "we never classified this path" from "the classification we had has expired",
#: which are different pieces of work.
UNCLASSIFIED_PATH = "unclassified-path"
EXPIRED_CLASSIFICATION = "expired-classification"
REMOVED_PATH = "removed-path"

#: The read limit applied before pull-request evidence is collected. Evidence is
#: evidence, not the classification: the change set decides, and a truncated PR list
#: is recorded as truncated rather than presented as the whole set.
DEFAULT_PR_LIMIT = 20

_HEAD = re.compile(r"^[0-9a-f]{40}$")


@dataclasses.dataclass(frozen=True)
class Finding:
    """One path that needs a human, and why."""

    path: str
    reason: str
    change: str = ""
    previous_disposition: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "change": self.change,
            "previous_disposition": self.previous_disposition,
        }


@dataclasses.dataclass(frozen=True)
class SourceReading:
    """What one source looked like at audit time."""

    id: str
    repository: str
    visibility: str
    severity: str
    status: str
    baseline_sha: str
    head_sha: str = ""
    #: ``(path, disposition)`` for every tracked path the source advanced that keeps
    #: its classification. Recorded so an audit trail can show what was dismissed and
    #: why, rather than only what was escalated.
    classified: Tuple[Tuple[str, str], ...] = ()
    findings: Tuple[Finding, ...] = ()
    pull_requests: Tuple[int, ...] = ()
    #: Anything that makes the reading partial. Non-empty means the reading cannot
    #: support a clean claim, whatever the status says.
    limits: Tuple[str, ...] = ()
    #: How the source was read. Recorded in the report so a reader can see that a
    #: private source was not read with the public token. The credential itself is
    #: never here.
    credential: str = ""
    ledger_digest: str = ""
    audited_at: str = ""

    @property
    def unresolved(self) -> bool:
        return self.status in UNRESOLVED_STATUSES or bool(self.limits)

    @property
    def drifted(self) -> bool:
        return self.status == UNCLASSIFIED_DRIFT

    @property
    def needs_human(self) -> bool:
        return self.drifted or self.status == UNAVAILABLE or self.status == UNREADABLE

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "visibility": self.visibility,
            "severity": self.severity,
            "status": self.status,
            "baseline_sha": self.baseline_sha,
            "head_sha": self.head_sha,
            "classified": [
                {"path": path, "disposition": disposition}
                for path, disposition in self.classified
            ],
            "findings": [finding.describe() for finding in self.findings],
            "pull_requests": list(self.pull_requests),
            "limits": list(self.limits),
            "credential": self.credential,
            "ledger_digest": self.ledger_digest,
            "audited_at": self.audited_at,
        }

    @property
    def digest(self) -> str:
        """A digest of the observation, not of when it was made.

        `audited_at` is excluded for the same reason it is excluded from the ledger's
        digest: this reading is folded into a promotion's evidence, and an approval
        that expired because the weekly audit ran would be worse than no audit at
        all. Re-running the checker against unchanged repositories has to produce
        an unchanged digest.
        """

        payload = json.dumps(
            {key: value for key, value in self.describe().items() if key != "audited_at"},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sr-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class DriftReport:
    """Every source's reading, plus the one verdict they add up to."""

    ledger_digest: str
    readings: Tuple[SourceReading, ...] = ()
    generated_at: str = ""

    def reading(self, identifier: str) -> Optional[SourceReading]:
        for reading in self.readings:
            if reading.id == identifier:
                return reading
        return None

    @property
    def drifted(self) -> Tuple[SourceReading, ...]:
        return tuple(reading for reading in self.readings if reading.drifted)

    @property
    def unresolved(self) -> Tuple[SourceReading, ...]:
        """Readings that did not establish something a reader can rely on.

        A reading with limits counts even when its status reads clean, because that
        is the whole point of a limit: the status is what was found, and the limit is
        what could not be. Reading `incomplete` and this list off the same predicate
        is deliberate -- if they could disagree, one of them would report a partial
        audit as complete.
        """

        return tuple(reading for reading in self.readings if reading.unresolved)

    @property
    def incomplete(self) -> bool:
        """Whether any reading failed to establish what it was meant to establish."""

        return any(reading.unresolved for reading in self.readings)

    @property
    def advanced(self) -> Tuple[SourceReading, ...]:
        """Readings where the source moved, classified or not."""

        return tuple(
            reading
            for reading in self.readings
            if reading.status in (CLASSIFIED_ADVANCE, UNCLASSIFIED_DRIFT)
        )

    @property
    def unclassified_p0(self) -> Tuple[SourceReading, ...]:
        """Drifted sources whose severity is ``p0``.

        Counted separately because this is the number a consumer promotion is
        refused on: it is the answer to "did the source of the automation we
        replaced move in a way nobody has looked at".
        """

        return tuple(
            reading for reading in self.readings if reading.drifted and reading.severity == "p0"
        )

    @property
    def verdict(self) -> str:
        if self.drifted:
            return DRIFTED
        if self.incomplete:
            return INCOMPLETE
        return CLEAN

    @property
    def no_unclassified_source_drift(self) -> bool:
        """The claim a consumer pin promotion has to be able to make."""

        return not self.drifted

    @property
    def fingerprint(self) -> str:
        """The identity of a drift finding set.

        Stable across runs that observe the same thing, and different the moment the
        observed thing changes. Both ends matter: a fingerprint that changed on every
        run would open a new issue per week, and one that did not change when the
        source moved would leave the first issue describing a state that no longer
        exists.

        The digest of the source's own reading is *not* included, because it covers
        the audit timestamp, and a timestamp changes every run. The evidence a reader
        would compare is here instead: baseline, head, the paths, and the PRs.
        """

        payload = [
            {
                "id": reading.id,
                "baseline": reading.baseline_sha,
                "head": reading.head_sha,
                "status": reading.status,
                "paths": sorted(
                    [finding.path for finding in reading.findings]
                    + [path for path, _ in reading.classified]
                ),
                "pull_requests": sorted(reading.pull_requests),
            }
            for reading in self.readings
            if reading.status != UNCHANGED
        ]
        return "pd-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]

    @property
    def digest(self) -> str:
        """What the promotion gate folds into its evidence digest.

        Every reading's own evidence is bound here, not just the identifiers of the
        ones that failed. A digest that named only the drifted sources would leave
        two different clean reports indistinguishable, so a promotion approved
        against the first would still be approved after the second was observed --
        with no record anywhere that the thing that was approved had changed.
        """

        payload = {
            "ledger_digest": self.ledger_digest,
            "verdict": self.verdict,
            "drifted": [reading.id for reading in self.drifted],
            "unresolved": [reading.id for reading in self.unresolved],
            "readings": [reading.digest for reading in self.readings],
        }
        return "pdr-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": "continuum.provenance-drift/v1",
            "ledger_digest": self.ledger_digest,
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "audit_complete": not self.incomplete,
            "no_unclassified_source_drift": self.no_unclassified_source_drift,
            "drifted": [reading.id for reading in self.drifted],
            "unresolved": [reading.id for reading in self.unresolved],
            "unclassified_p0": [reading.id for reading in self.unclassified_p0],
            "advanced": [reading.id for reading in self.advanced],
            "fingerprint": self.fingerprint,
            "digest": self.digest,
            "readings": [reading.describe() for reading in self.readings],
        }


def read_report(document: Mapping[str, Any]) -> DriftReport:
    """Read a drift report, refusing one whose summary contradicts its readings.

    The report is written by one process and read by another -- by the promotion
    gate, which cannot re-run the audit. So the counts, the verdict and the digest
    are recomputed here rather than trusted: a report claiming `clean` with a drifted
    reading inside it is refused, because the gate's whole job is to not believe a
    summary that disagrees with its evidence.
    """

    from .ledger import ProvenanceError

    if not isinstance(document, Mapping):
        raise ProvenanceError("report_not_a_mapping", "a drift report must be an object")
    if document.get("schema") != "continuum.provenance-drift/v1":
        raise ProvenanceError(
            "unknown_report_schema",
            "expected 'continuum.provenance-drift/v1', found {!r}".format(document.get("schema")),
        )
    raw = document.get("readings")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ProvenanceError(
            "no_readings",
            "a drift report with no readings claims nothing was checked, which is "
            "not the same as claiming nothing moved",
        )

    readings: List[SourceReading] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ProvenanceError("reading_not_a_mapping", "each reading must be an object")
        status = str(entry.get("status") or "")
        known = (
            UNCHANGED,
            CLASSIFIED_ADVANCE,
            UNCLASSIFIED_DRIFT,
            UNAVAILABLE,
            UNREADABLE,
        )
        if status not in known:
            raise ProvenanceError(
                "unknown_reading_status",
                "{!r} is not a status this checker produces".format(status),
            )
        findings = tuple(
            Finding(
                path=str(item.get("path") or ""),
                reason=str(item.get("reason") or ""),
                change=str(item.get("change") or ""),
                previous_disposition=str(item.get("previous_disposition") or ""),
            )
            for item in entry.get("findings") or []
            if isinstance(item, Mapping)
        )
        classified = tuple(
            (str(item.get("path") or ""), str(item.get("disposition") or ""))
            for item in entry.get("classified") or []
            if isinstance(item, Mapping)
        )
        readings.append(
            SourceReading(
                id=str(entry.get("id") or ""),
                repository=str(entry.get("repository") or ""),
                visibility=str(entry.get("visibility") or "public"),
                severity=str(entry.get("severity") or "p1"),
                status=status,
                baseline_sha=str(entry.get("baseline_sha") or ""),
                head_sha=str(entry.get("head_sha") or ""),
                classified=classified,
                findings=findings,
                pull_requests=tuple(int(number) for number in entry.get("pull_requests") or []),
                limits=tuple(str(limit) for limit in entry.get("limits") or []),
                credential=str(entry.get("credential") or ""),
                ledger_digest=str(entry.get("ledger_digest") or ""),
                audited_at=str(entry.get("audited_at") or ""),
            )
        )

    report = DriftReport(
        ledger_digest=str(document.get("ledger_digest") or ""),
        readings=tuple(readings),
        generated_at=str(document.get("generated_at") or ""),
    )

    stated = str(document.get("verdict") or "")
    if stated and stated != report.verdict:
        raise ProvenanceError(
            "report_verdict_mismatch",
            "the report says {!r} but its readings add up to {!r}".format(
                stated, report.verdict
            ),
        )
    stated_digest = str(document.get("digest") or "")
    if stated_digest and stated_digest != report.digest:
        raise ProvenanceError(
            "report_digest_mismatch",
            "the report carries the digest {!r}, which is not the digest its own "
            "readings produce".format(stated_digest),
        )
    return report


def load_report(path: str) -> DriftReport:
    with open(path, "r", encoding="utf-8") as handle:
        return read_report(json.load(handle))


def _tracked_findings(source: TrackedSource, changes: Sequence[Mapping[str, str]]) -> List[Finding]:
    """Classify the tracked paths a change set touched."""

    findings: List[Finding] = []
    for change in changes:
        path = str(change.get("path") or "")
        status = str(change.get("status") or "")
        if not path or not source.tracks(path):
            # Outside the audited scope. Not drift, and not reported as drift: the
            # ledger said what was in scope, so the checker does not get to widen or
            # narrow that judgement by itself.
            continue
        if status in ("removed", "renamed"):
            # A path that was audited and is gone is not a no-op even when its
            # disposition is permanent: the audit has to point at the successor.
            findings.append(
                Finding(
                    path=path,
                    reason=REMOVED_PATH,
                    change=status,
                    previous_disposition=source.disposition_for(path) or "",
                )
            )
            continue
        disposition = source.disposition_for(path)
        if disposition is None:
            findings.append(
                Finding(path=path, reason=UNCLASSIFIED_PATH, change=status)
            )
        elif disposition not in PERSISTENT_DISPOSITIONS:
            # The classification was about this content. The content moved, so the
            # classification no longer says anything about what replaced it.
            findings.append(
                Finding(
                    path=path,
                    reason=EXPIRED_CLASSIFICATION,
                    change=status,
                    previous_disposition=disposition,
                )
            )
    return findings


def classify(
    source: TrackedSource,
    *,
    head_sha: str,
    changes: Optional[Sequence[Mapping[str, str]]],
    audited_at: str = "",
    limits: Sequence[str] = (),
    pull_requests: Sequence[int] = (),
    credential: str = "",
) -> SourceReading:
    """Turn one source's head and change set into a reading.

    Split out from the reading so it can be exercised on real change sets without a
    client: the classification is the part that decides whether a repository's
    movement matters, and it is the part worth testing exhaustively.

    `changes` is optional on purpose. An empty list means "the comparison ran and
    the source moved nothing", which is the reading that reports `unchanged`; `None`
    means "no comparison came back at all", which is a failed audit and is reported
    as `unreadable`. Collapsing the two would let a missing reading be published as a
    clean source -- the exact shape of a false all-clear.
    """

    recorded = tuple(sorted({item for item in limits}))
    if not head_sha or not _HEAD.match(head_sha) or changes is None:
        # No usable comparison. Treating that as an unchanged source would report a
        # green audit on top of a missing reading.
        if changes is None and not recorded:
            recorded = ("no-change-set",)
        return SourceReading(
            id=source.id,
            repository=source.repository,
            visibility=source.visibility,
            severity=source.severity,
            status=UNREADABLE,
            baseline_sha=source.baseline_sha,
            limits=recorded,
            credential=credential,
            ledger_digest=source.digest,
            audited_at=audited_at,
        )

    findings = _tracked_findings(source, changes)
    tracked_touched = sorted(
        {
            str(change.get("path"))
            for change in changes
            if str(change.get("path")) and source.tracks(str(change.get("path")))
        }
    )
    if findings:
        status = UNCLASSIFIED_DRIFT
    elif tracked_touched:
        # The source moved, on tracked paths, and every one of them kept its
        # classification. A finding for the record, not a problem.
        status = CLASSIFIED_ADVANCE
    else:
        status = UNCHANGED

    classified = tuple(
        (path, source.disposition_for(path))
        for path in tracked_touched
        if not any(finding.path == path for finding in findings)
    )

    return SourceReading(
        id=source.id,
        repository=source.repository,
        visibility=source.visibility,
        severity=source.severity,
        status=status,
        baseline_sha=source.baseline_sha,
        head_sha=head_sha,
        # Only recorded when something moved. An `unchanged` source carries no
        # classified list, so the fingerprint cannot be read two ways -- "nothing
        # moved" and "nothing was looked at" would otherwise produce the same empty
        # list, and only the status separates them.
        classified=classified if status != UNCHANGED else (),
        findings=tuple(findings),
        pull_requests=tuple(sorted({int(number) for number in pull_requests})),
        limits=recorded,
        credential=credential,
        ledger_digest=source.digest,
        audited_at=audited_at,
    )


def read_source(
    client: Any,
    source: TrackedSource,
    *,
    private_token: str = "",
    audited_at: str = "",
    pull_request_limit: int = DEFAULT_PR_LIMIT,
) -> SourceReading:
    """Take one source's reading from a repository's API.

    `client` is the bound :class:`~continuum.review.github.GitHubClient` for
    `source.repository`, already authenticated with whatever credential the caller
    chose. For a private source that credential has to be the source's own, and
    `private_token` is how the caller says so.

    The check is here rather than only at the caller because "the caller was careful"
    is not a control. A private source handed a client with no private credential
    resolves to `unavailable` here, without a request, rather than to whatever the
    ambient token happened to be able to see.
    """

    from ..review.github import GitHubError

    unreadable = SourceReading(
        id=source.id,
        repository=source.repository,
        visibility=source.visibility,
        severity=source.severity,
        status=UNREADABLE,
        baseline_sha=source.baseline_sha,
        credential="private-read-token" if source.is_private else "public-read",
        ledger_digest=source.digest,
        audited_at=audited_at,
    )

    if source.is_private and not private_token:
        return dataclasses.replace(
            unreadable,
            status=UNAVAILABLE,
            credential="none",
            limits=("no-credential-configured-for-private-source",),
        )

    try:
        head_sha = client.ref_sha(client.default_branch())
        if head_sha == source.baseline_sha:
            # The source has not moved, so the comparison cannot say anything. Skipping
            # it is not an optimisation of convenience: it is the difference between a
            # weekly audit that costs one request per source and one that costs a full
            # change set for every source, every week, forever.
            comparison: Optional[Dict[str, Any]] = None
            changes: Optional[List[Mapping[str, str]]] = []
        else:
            comparison = client.compare_commits(source.baseline_sha, head_sha)
            changes = comparison.get("files") or []
    except GitHubError as error:
        # The status code, never the message: GitHubError carries the API's response
        # body, and for a private source that body is metadata this plane is not
        # allowed to put in a report, an issue, or a log line.
        return dataclasses.replace(
            unreadable, limits=("read-failed-status-{}".format(error.status or "unknown"),)
        )

    limits: List[str] = []
    if comparison:
        if comparison.get("files_truncated"):
            limits.append("change-set-truncated")
        if comparison.get("behind_by") or comparison.get("status") == "diverged":
            # The baseline is no longer an ancestor. Every classification in the
            # ledger was made against content that is no longer in this history, so
            # the comparison cannot be trusted to describe what changed.
            limits.append("baseline-is-not-an-ancestor")

    pull_requests: List[int] = []
    if pull_request_limit > 0 and comparison is not None:
        # PR numbers are evidence attached to a drift finding, so they are only
        # fetched for a source that moved -- which is also what keeps a busy
        # repository from costing a paginated read on every weekly run. An advance
        # that touched no file still gets them: the head moved, and a reader asking
        # "which PR did that" deserves an answer.
        try:
            for pull in client.list_pulls(state="open", limit=pull_request_limit):
                number = int(pull.get("number") or 0)
                if not number:
                    continue
                pull_requests.append(number)
        except GitHubError as error:
            # PR numbers are corroborating evidence for a human. Failing to collect
            # them is recorded, not fatal: the change set still decides the finding,
            # and a missing PR number must not become a missing drift report.
            limits.append("pull-request-evidence-unavailable-{}".format(error.status or "unknown"))

    return classify(
        source,
        head_sha=head_sha,
        changes=changes,
        audited_at=audited_at,
        limits=limits,
        pull_requests=pull_requests,
        credential="private-read-token" if source.is_private else "public-read",
    )


def check(
    ledger: ProvenanceLedger,
    *,
    readings: Sequence[SourceReading] = (),
    generated_at: str = "",
) -> DriftReport:
    """The report every consumer of this plane reads.

    A source in the ledger with no reading is not silently dropped. It becomes an
    `unreadable` reading, so a checker that failed to look at a repository cannot
    produce a clean report by omitting it.

    Two readings for one source are refused rather than merged. The second would
    otherwise displace the first, and whichever lost would take its findings with
    it -- a way for drift to disappear from a report without anything recording
    that it was ever there.
    """

    from .ledger import ProvenanceError

    by_id: Dict[str, SourceReading] = {}
    for reading in readings:
        if reading.id in by_id:
            raise ProvenanceError(
                "duplicate_reading",
                "two readings were collected for the source {}; this report will not "
                "choose between them".format(reading.id or "<unnamed>"),
            )
        by_id[reading.id] = reading

    resolved: List[SourceReading] = []
    for source in ledger.sources:
        reading = by_id.get(source.id)
        if reading is None:
            reading = SourceReading(
                id=source.id,
                repository=source.repository,
                visibility=source.visibility,
                severity=source.severity,
                status=UNREADABLE,
                baseline_sha=source.baseline_sha,
                limits=("no-reading-collected",),
                ledger_digest=source.digest,
                audited_at=generated_at,
            )
        elif reading.ledger_digest and reading.ledger_digest != source.digest:
            # The reading was taken against a different version of this source's
            # record, so its classification was decided by rules the ledger in hand
            # no longer states. The status is *not* overwritten: a reviewer looking
            # at this report needs the finding that was collected more than they need
            # the report to be tidy. The limit is what makes the report incomplete,
            # and the finding survives next to it.
            reading = dataclasses.replace(
                reading,
                limits=tuple(sorted(set(reading.limits) | {"ledger-changed-since-reading"})),
            )
        resolved.append(reading)
    return DriftReport(
        ledger_digest=ledger.digest,
        readings=tuple(resolved),
        generated_at=generated_at,
    )


def observe(client: Any, *, branch: str = "") -> Dict[str, Any]:
    """The reading an operator needs before registering a source: head and inventory.

    Deliberately raw. Registering a source means deciding what to audit and at which
    commit, and those are human decisions; this only supplies the facts for them.
    """

    from ..review.github import GitHubError

    try:
        ref = branch or client.default_branch()
        head_sha = client.ref_sha(ref)
        inventory = client.workflow_inventory(head_sha)
    except GitHubError as error:
        return {
            "branch": branch,
            "status": UNREADABLE,
            "limits": ["read-failed-status-{}".format(error.status or "unknown")],
            "head_sha": "",
            "workflows": {},
        }
    return {
        "branch": ref,
        "status": UNCHANGED if not inventory else CLASSIFIED_ADVANCE,
        "limits": [],
        "head_sha": head_sha,
        # Blob SHAs, not contents: the question is "which commit do we audit", and
        # an inventory of file bodies would put the source's code into this
        # repository's report artifact for no benefit.
        "workflows": dict(sorted(inventory.items())),
    }


# -- the parity issue -------------------------------------------------------

ISSUE_LABEL = "parity-drift"
ISSUE_MARKER = "<!-- continuum:parity-drift -->"

_CREATE = "create"
_COMMENT = "comment"
_NONE = "none"
_NONE = "none"


def render_issue(report: DriftReport) -> Dict[str, Any]:
    """The parity issue for a report: what moved, and what has to be classified.

    Contains SHAs, paths, change statuses and PR numbers. No file contents, no diffs,
    no API responses, and for a private source no repository name.
    """

    drifted = report.drifted
    unresolved = report.unresolved
    title_source = drifted[0] if drifted else (unresolved[0] if unresolved else report.readings[0])
    scope = "{} source".format(len(drifted or unresolved or report.readings))
    if title_source.severity == "p0":
        scope = "p0 " + scope
    title = "[parity] {} unclassified advance: {}".format(
        scope, title_source.repository if title_source.visibility == "public" else title_source.id
    )

    lines = [
        ISSUE_MARKER,
        "",
        "Provenance drift: {} of {} tracked sources advanced without a surviving "
        "classification.".format(len(drifted), len(report.readings)),
        "",
        "This is a report, not an import. Nothing has been copied from any source, "
        "and nothing will be until a reviewer classifies the paths below and records "
        "the outcome in the provenance ledger.",
        "",
        "| Source | Severity | Baseline | Head | Unclassified paths | Open PRs |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for reading in report.readings:
        if reading.status == UNCHANGED:
            continue
        name = reading.repository if reading.visibility == "public" else "`{}` (private)".format(reading.id)
        lines.append(
            "| {} | {} | `{}` | `{}` | {} | {} |".format(
                name,
                reading.severity,
                (reading.baseline_sha or "?")[:12],
                (reading.head_sha or "?")[:12],
                len(reading.findings),
                ", ".join("#{}".format(number) for number in reading.pull_requests) or "none",
            )
        )

    for reading in report.readings:
        if reading.status in UNCHANGED:
            continue
        if reading.status == CLASSIFIED_ADVANCE:
            lines.extend(
                [
                    "",
                    "### `{}` advanced, classified".format(reading.id),
                    "",
                    "Baseline `{}` -> head `{}`. Every tracked path it touched keeps its "
                    "classification, so no work is required:".format(
                        (reading.baseline_sha or "?")[:12], (reading.head_sha or "?")[:12]
                    ),
                    "",
                ]
            )
            for path, disposition in reading.classified:
                lines.append("* `{}` -- `{}`".format(path, disposition))
            continue
        if reading.status in UNRESOLVED_STATUSES:
            lines.extend(
                [
                    "",
                    "### `{}` could not be read".format(reading.id),
                    "",
                    "This source is **not** confirmed unchanged. Until it is read, no "
                    "claim of parity for it can be made or promoted:",
                    "",
                ]
            )
            for limit in reading.limits or ("unknown",):
                lines.append("* {}".format(limit))
            continue
        lines.extend(
            [
                "",
                "### `{}` -- {} unclassified path{}".format(
                    reading.id, len(reading.findings), "" if len(reading.findings) == 1 else "s"
                ),
                "",
                "Baseline `{}` -> head `{}`. Each path below needs a recorded "
                "classification of `{}`, `{}` or `{}` -- or, if the source no longer "
                "bears on Continuum at all, an explicit `{}`. A classification that "
                "describes the *previous* content does not carry over to content that "
                "replaced it:".format(
                    (reading.baseline_sha or "?")[:12],
                    (reading.head_sha or "?")[:12],
                    "absorbed",
                    "must-port",
                    "superseded",
                    "not-applicable",
                ),
                "",
                "| Path | Change | Why it needs a classification | Previous disposition |",
                "| --- | --- | --- | --- |",
            ]
        )
        for finding in reading.findings:
            lines.append(
                "| `{}` | {} | {} | {} |".format(
                    finding.path,
                    finding.change or "-",
                    {
                        UNCLASSIFIED_PATH: "no disposition recorded for this path",
                        EXPIRED_CLASSIFICATION: "the recorded disposition described the previous content",
                        REMOVED_PATH: "an audited path is gone; point the ledger at what replaced it",
                    }.get(finding.reason, finding.reason),
                    "`{}`".format(finding.previous_disposition) if finding.previous_disposition else "-",
                )
            )

    lines.extend(
        [
            "",
            "---",
            "",
            "Fingerprint: `{}`".format(report.fingerprint),
            "",
            "To resolve: classify each path in `docs/provenance-ledger.json`, then run the "
            "checker again. The fingerprint changes when the observed evidence changes, "
            "so a re-scan of the same state will not open a new issue.",
        ]
    )
    return {
        "title": title,
        "body": "\n".join(lines),
        "fingerprint": report.fingerprint,
        "label": ISSUE_LABEL,
        "marker": ISSUE_MARKER,
    }


def fingerprint_of(body: str) -> str:
    """The fingerprint an existing parity issue carries, or an empty string."""

    for line in str(body or "").splitlines():
        text = line.strip()
        if text.startswith("Fingerprint: `") and text.endswith("`"):
            return text[len("Fingerprint: `") : -1]
    return ""


def issue_plan(
    report: DriftReport,
    *,
    existing_number: int = 0,
    existing_body: str = "",
) -> Dict[str, Any]:
    """Whether to open a parity issue, comment on the open one, or do nothing.

    The three rules, in order:

    1. Nothing to say -- no drift, nothing unresolved -- does nothing at all.
    2. A fingerprint that matches the open issue's does nothing. A weekly schedule
       re-reading the same unchanged evidence must not produce a comment a week for
       as long as nobody has classified it.
    3. Any other evidence against an open issue gets a comment, because the open
       issue's description is a snapshot of a state that no longer exists and is now
       describing drift nobody is looking at.
    """

    issue = render_issue(report)
    if not report.drifted and not report.incomplete:
        return {"action": _NONE, "reason": "no drift and every source was read", **issue}
    existing_fingerprint = fingerprint_of(existing_body)
    if existing_number and existing_fingerprint and existing_fingerprint == report.fingerprint:
        return {
            "action": _NONE,
            "reason": "issue #{} already describes this evidence".format(existing_number),
            **issue,
        }
    if existing_number:
        return {
            "action": _COMMENT,
            "number": existing_number,
            "reason": "the evidence moved since issue #{} was opened".format(existing_number),
            **issue,
        }
    return {"action": _CREATE, "reason": "no open parity issue describes this drift", **issue}


def load_ledger(path: str) -> ProvenanceLedger:
    return load_provenance(path)


__all__ = [
    "CLASSIFIED_ADVANCE",
    "CLEAN",
    "DEFAULT_PR_LIMIT",
    "DRIFTED",
    "EXPIRED_CLASSIFICATION",
    "Finding",
    "INCOMPLETE",
    "ISSUE_LABEL",
    "ISSUE_MARKER",
    "REMOVED_PATH",
    "SourceReading",
    "DriftReport",
    "UNAVAILABLE",
    "UNCLASSIFIED_DRIFT",
    "UNCLASSIFIED_PATH",
    "UNCHANGED",
    "UNREADABLE",
    "UNRESOLVED_STATUSES",
    "check",
    "classify",
    "fingerprint_of",
    "issue_plan",
    "load_report",
    "observe",
    "read_report",
    "read_source",
    "render_issue",
]