"""The provenance ledger: which sources Continuum claims parity with, and at which
audited commit.

A parity claim has a shelf life. ``docs/parity-ledger.json`` records which of one
repository's workflows were audited and at which blob; this ledger records the
sources themselves -- the repository, the commit the claim was made against, what
was observed there, and where in Continuum the property it established lives. It is
versioned (``continuum.provenance-ledger/v1``) because a document whose shape can
change under a reader is not a document anybody can gate on.

Two classifications, and the difference between them is the whole mechanism:

``tracked_prefixes``
    *Is this path part of parity at all?* A change outside every tracked prefix is
    not drift, and is not reported as one. This is scope, not judgement: it says
    what was audited, so the checker never has to decide on its own that a change
    is uninteresting.

``path_dispositions``
    *What was this path's change taken to mean?* A disposition from
    :data:`PERSISTENT_DISPOSITIONS` is a claim about the *kind* of the path, so it
    survives the path changing: a consumer-local workflow that moved is still
    consumer-local, and the move is a recorded no-op. Every other disposition
    (``absorbed``, ``must-port``, ``superseded``) is a claim about the content that
    was read, so it expires the moment that content moves, and the advance becomes
    unclassified drift until somebody classifies the new content.

That asymmetry is deliberate. Treating a content-scoped classification as durable
would make "we absorbed this" a permanent claim nobody ever has to revisit, which is
the drift this ledger exists to catch. Treating a scope claim as expiring would make
every product-side commit a parity event, which is noise, and noise is how a real
drift goes unnoticed.

Every source also names where in Continuum the property it established lives -- the
implementation and the tests that would fail without it -- because a classification
nobody can point at is a claim with nothing behind it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..shadow.baseline import CLASSIFICATIONS

PROVENANCE_SCHEMA = "continuum.provenance-ledger/v1"

#: The document's own version, separate from the schema string. A reader that
#: understands v1 can refuse v2 instead of guessing at the fields it does not know.
LEDGER_VERSION = 1

#: One vocabulary with the cutover ledger's, imported rather than restated: two
#: lists of dispositions that are allowed to drift apart is one too many.
DISPOSITIONS: Tuple[str, ...] = CLASSIFICATIONS

#: The dispositions that survive the file changing. See the module docstring.
PERSISTENT_DISPOSITIONS: Tuple[str, ...] = ("consumer-local", "not-applicable")

#: How much an unclassified advance in this source matters. `p0` is a source whose
#: automation is the trust boundary -- the orchestration Continuum replaced -- so
#: "no unclassified p0 drift" is the claim a consumer promotion has to be able to
#: make, and the report counts it separately rather than leaving a reader to work it
#: out from a list.
SEVERITIES: Tuple[str, ...] = ("p0", "p1", "p2")
DEFAULT_SEVERITY = "p1"

#: Whether the source is readable with the ambient read credential. A private
#: source is never read with it.
VISIBILITIES: Tuple[str, ...] = ("public", "private")

#: The least-privilege credential a private source is read with when it does not
#: name its own. It is a *default*, not a fallback: the value still has to be
#: present in the process, and its absence is reported rather than worked around.
DEFAULT_CREDENTIAL_ENV = "PRIVATE_SOURCE_READ_TOKEN"

_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ProvenanceError(ValueError):
    """The ledger cannot be read as the record it claims to be."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ContinuumReference:
    """Where in Continuum a source's property lives, and what proves it."""

    implementation: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "implementation": list(self.implementation),
            "tests": list(self.tests),
        }


@dataclasses.dataclass(frozen=True)
class TrackedSource:
    """One repository Continuum claims parity with."""

    id: str
    repository: str
    visibility: str
    #: The commit the claim was made against. Required, because "the source moved"
    #: is a comparison and a source with nothing to compare from cannot drift.
    baseline_sha: str
    #: The last head the checker observed. Recorded so the ledger itself is the
    #: audit trail of SHA advancement rather than a snapshot of one moment.
    current_sha: str = ""
    #: The source-level disposition: what this source is to Continuum.
    disposition: str = ""
    severity: str = DEFAULT_SEVERITY
    #: The instants the claim was last reviewed by a person.
    audited_at: str = ""
    audit_version: str = "1"
    #: Paths in scope for parity auditing. Empty means the source is tracked by
    #: incident only, which the reader refuses unless it is recorded as such.
    tracked_prefixes: Tuple[str, ...] = ()
    #: ``path or prefix -> disposition``. Longest match wins.
    path_dispositions: Tuple[Tuple[str, str], ...] = ()
    #: Incident identifiers this source exists for, when the claim came from a
    #: report rather than from a path.
    incidents: Tuple[str, ...] = ()
    continuum: ContinuumReference = dataclasses.field(default_factory=ContinuumReference)
    #: The variable a private source's least-privilege credential is read from.
    #: Never set for a public source: reaching for a broader credential than a
    #: public read needs is the failure this ledger is not allowed to make quietly.
    credential_env: str = ""
    #: The reviewed record this source's per-workflow classifications live in.
    workflow_ledger: str = ""
    rationale: str = ""

    @property
    def is_private(self) -> bool:
        return self.visibility == "private"

    def tracks(self, path: str) -> bool:
        """Whether a path is in scope for this source's parity audit."""

        return any(path.startswith(prefix) for prefix in self.tracked_prefixes)

    def disposition_for(self, path: str) -> Optional[str]:
        """The disposition recorded for a path, or ``None`` if it has none.

        Longest match wins, so an exact path overrides the prefix that contains it.
        Two rules of the same length cannot both match: the reader refuses that,
        because a tie would make the classification depend on dictionary order.
        """

        best: Optional[str] = None
        best_length = -1
        for pattern, disposition in self.path_dispositions:
            if not (path == pattern or path.startswith(pattern)):
                continue
            if len(pattern) > best_length:
                best, best_length = disposition, len(pattern)
        return best

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "visibility": self.visibility,
            "baseline_sha": self.baseline_sha,
            "current_sha": self.current_sha,
            "disposition": self.disposition,
            "severity": self.severity,
            "audited_at": self.audited_at,
            "audit_version": self.audit_version,
            "tracked_prefixes": list(self.tracked_prefixes),
            "path_dispositions": [
                {"pattern": pattern, "disposition": disposition}
                for pattern, disposition in self.path_dispositions
            ],
            "incidents": list(self.incidents),
            "continuum": self.continuum.describe(),
            "credential_env": self.credential_env,
            "workflow_ledger": self.workflow_ledger,
            "rationale": self.rationale,
        }

    @property
    def digest(self) -> str:
        """A digest of what this source *claims*, not of when it was reviewed.

        `audited_at` and `audit_version` are deliberately absent. They are the
        record of a review having happened, not part of the claim it reviewed, and a
        digest that moved every time somebody re-recorded a date would invalidate
        every captured reading for a change to nothing -- which is how a staleness
        check becomes noise nobody reads.
        """

        payload = {
            "id": self.id,
            "repository": self.repository,
            "visibility": self.visibility,
            "baseline_sha": self.baseline_sha,
            "disposition": self.disposition,
            "severity": self.severity,
            "tracked_prefixes": list(self.tracked_prefixes),
            "path_dispositions": [list(item) for item in self.path_dispositions],
            "incidents": list(self.incidents),
            "continuum": self.continuum.describe(),
            "workflow_ledger": self.workflow_ledger,
        }
        return "ts-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        ).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class ProvenanceLedger:
    """Every source Continuum claims parity with, and the audit behind each claim."""

    sources: Tuple[TrackedSource, ...] = ()
    version: int = LEDGER_VERSION
    generated_from: str = ""

    @property
    def source_map(self) -> Dict[str, TrackedSource]:
        return {source.id: source for source in self.sources}

    def source(self, identifier: str) -> Optional[TrackedSource]:
        return self.source_map.get(identifier)

    @property
    def public_sources(self) -> Tuple[TrackedSource, ...]:
        return tuple(source for source in self.sources if not source.is_private)

    @property
    def private_sources(self) -> Tuple[TrackedSource, ...]:
        return tuple(source for source in self.sources if source.is_private)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "version": self.version,
            "generated_from": self.generated_from,
            "sources": [source.describe() for source in self.sources],
        }

    @property
    def digest(self) -> str:
        payload = {
            "version": self.version,
            "sources": sorted(source.digest for source in self.sources),
        }
        return "pv-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        ).hexdigest()[:16]


def _require_sha(value: str, *, field: str, owner: str, allow_empty: bool = False) -> str:
    text = str(value or "")
    if not text and allow_empty:
        return ""
    if not _SHA.match(text):
        raise ProvenanceError(
            "invalid_sha",
            "{} names {} as {!r}, which is not a full commit SHA; drift is a "
            "comparison and needs both ends of it".format(owner, field, text),
        )
    return text


def _string_list(value: Any, *, field: str, owner: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ProvenanceError(
            "invalid_list",
            "{} {} must be a list, not {}".format(owner, field, type(value).__name__),
        )
    items = tuple(str(item) for item in value)
    for item in items:
        if not item.strip():
            raise ProvenanceError(
                "invalid_list", "{} {} contains an empty entry".format(owner, field)
            )
    return items


def _reference(value: Any, *, owner: str) -> ContinuumReference:
    """Read the Continuum-side reference, refusing one that points at nothing.

    Both halves are required. A disposition with no implementation is a claim
    about code that does not exist, and one with no test is a claim nothing would
    notice losing -- which is the failure this ledger is read to catch, applied to
    the ledger itself.
    """

    if value is None:
        raise ProvenanceError(
            "missing_continuum_reference",
            "{} names no Continuum implementation or tests; a classification nobody "
            "can point at is a claim with nothing behind it".format(owner),
        )
    if not isinstance(value, Mapping):
        raise ProvenanceError(
            "invalid_continuum_reference",
            "{} continuum must be an object with implementation and tests".format(owner),
        )
    implementation = _string_list(value.get("implementation"), field="continuum.implementation", owner=owner)
    tests = _string_list(value.get("tests"), field="continuum.tests", owner=owner)
    if not implementation:
        raise ProvenanceError(
            "missing_continuum_reference",
            "{} names no Continuum implementation".format(owner),
        )
    if not tests:
        raise ProvenanceError(
            "missing_continuum_reference",
            "{} names no Continuum tests; nothing would fail if the property it "
            "claims were lost".format(owner),
        )
    return ContinuumReference(implementation=implementation, tests=tests)


def _dispositions(value: Any, *, owner: str) -> Tuple[Tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ProvenanceError(
            "invalid_dispositions",
            "{} path_dispositions must be an object of path -> disposition".format(owner),
        )
    pairs: List[Tuple[str, str]] = []
    for pattern, disposition in value.items():
        pattern_text = str(pattern or "")
        if not pattern_text.strip():
            raise ProvenanceError(
                "invalid_dispositions", "{} declares a disposition with no path".format(owner)
            )
        text = str(disposition or "")
        if text not in DISPOSITIONS:
            raise ProvenanceError(
                "unknown_disposition",
                "{} gives {} the disposition {!r}, which is not one of {}".format(
                    owner, pattern_text, text, ", ".join(DISPOSITIONS)
                ),
            )
        pairs.append((pattern_text, text))
    _refuse_ties(pairs, owner=owner)
    return tuple(sorted(pairs, key=lambda item: (-len(item[0]), item[0])))


def _refuse_ties(pairs: Sequence[Tuple[str, str]], *, owner: str) -> None:
    """Refuse two rules that claim the same path.

    A tie between two *distinct* patterns is impossible rather than merely
    unlikely: two prefixes of one path that are the same length are the same
    string. So the duplicate is the only ambiguity worth refusing, and it is
    refused at the reader because the consequence -- one classification silently
    replacing another -- is exactly the kind of quiet edit this ledger is for.
    """

    seen: Dict[str, str] = {}
    for pattern, disposition in pairs:
        if pattern in seen and seen[pattern] != disposition:
            raise ProvenanceError(
                "ambiguous_disposition",
                "{} declares two dispositions for {}".format(owner, pattern),
            )
        seen[pattern] = disposition


def _read_source(document: Mapping[str, Any]) -> TrackedSource:
    if not isinstance(document, Mapping):
        raise ProvenanceError("source_not_a_mapping", "each source must be an object")

    identifier = str(document.get("id", "") or "")
    owner = identifier or "<unnamed source>"
    if not _ID.match(identifier):
        raise ProvenanceError(
            "invalid_source_id",
            "{!r} is not a usable source id; the checker names sources by it in "
            "reports, issue bodies and the promotion gate".format(identifier),
        )

    repository = str(document.get("repository", "") or "")
    if not _REPOSITORY.match(repository):
        raise ProvenanceError(
            "invalid_repository",
            "{} names the repository {!r}; an owner/name pair is required, because "
            "the drift reading is taken from that repository's API".format(
                owner, repository
            ),
        )

    visibility = str(document.get("visibility", "") or "")
    if visibility not in VISIBILITIES:
        raise ProvenanceError(
            "unknown_visibility",
            "{} declares the visibility {!r}, which is not one of {}".format(
                owner, visibility, ", ".join(VISIBILITIES)
            ),
        )

    disposition = str(document.get("disposition", "") or "")
    if disposition not in DISPOSITIONS:
        raise ProvenanceError(
            "unknown_disposition",
            "{} has the disposition {!r}, which is not one of {}".format(
                owner, disposition, ", ".join(DISPOSITIONS)
            ),
        )

    severity = str(document.get("severity", "") or DEFAULT_SEVERITY)
    if severity not in SEVERITIES:
        raise ProvenanceError(
            "unknown_severity",
            "{} declares the severity {!r}, which is not one of {}".format(
                owner, severity, ", ".join(SEVERITIES)
            ),
        )

    baseline_sha = _require_sha(document.get("baseline_sha", ""), field="baseline_sha", owner=owner)
    current_sha = _require_sha(
        document.get("current_sha", ""), field="current_sha", owner=owner, allow_empty=True
    )

    audited_at = str(document.get("audited_at", "") or "")
    if not audited_at.strip():
        raise ProvenanceError(
            "missing_audit_timestamp",
            "{} records no audited_at; an audit with no instant cannot be shown to "
            "have happened before or after anything".format(owner),
        )

    tracked_prefixes = _string_list(
        document.get("tracked_prefixes"), field="tracked_prefixes", owner=owner
    )
    incidents = _string_list(document.get("incidents"), field="incidents", owner=owner)
    if not tracked_prefixes and not incidents:
        raise ProvenanceError(
            "untracked_source",
            "{} names neither a tracked path nor an incident; nothing about it would "
            "ever be read, so it would sit in the ledger looking audited".format(owner),
        )

    credential_env = str(document.get("credential_env", "") or "")
    if visibility == "public" and credential_env:
        raise ProvenanceError(
            "public_source_with_credential",
            "{} is public but names the credential {}; a public source is read with "
            "normal read access, and reaching for a broader credential than a public "
            "read needs is not something this ledger may request quietly".format(
                owner, credential_env
            ),
        )
    if credential_env and not re.match(r"^[A-Z][A-Z0-9_]*$", credential_env):
        raise ProvenanceError(
            "invalid_credential_env",
            "{} names the credential {!r}, which is not an environment variable "
            "name".format(owner, credential_env),
        )

    return TrackedSource(
        id=identifier,
        repository=repository,
        visibility=visibility,
        baseline_sha=baseline_sha,
        current_sha=current_sha,
        disposition=disposition,
        severity=severity,
        audited_at=audited_at,
        audit_version=str(document.get("audit_version", "") or "1"),
        tracked_prefixes=tracked_prefixes,
        path_dispositions=_dispositions(document.get("path_dispositions"), owner=owner),
        incidents=incidents,
        continuum=_reference(document.get("continuum"), owner=owner),
        credential_env=credential_env or (DEFAULT_CREDENTIAL_ENV if visibility == "private" else ""),
        workflow_ledger=str(document.get("workflow_ledger", "") or ""),
        rationale=str(document.get("rationale", "") or ""),
    )


def read_provenance(document: Mapping[str, Any]) -> ProvenanceLedger:
    """Read a provenance ledger, refusing anything it cannot vouch for.

    The strictness is the point of the type. A ledger with an unknown disposition,
    no baseline commit, no tracked path, or a source that names no Continuum code is
    not a record this checker can compare anything against, and accepting it would
    turn a typo into a clean audit.
    """

    if not isinstance(document, Mapping):
        raise ProvenanceError("ledger_not_a_mapping", "a provenance ledger must be an object")
    schema = document.get("schema")
    if schema != PROVENANCE_SCHEMA:
        raise ProvenanceError(
            "unknown_ledger_schema",
            "expected {!r}, found {!r}".format(PROVENANCE_SCHEMA, schema),
        )
    version = document.get("version", LEDGER_VERSION)
    try:
        version_number = int(version)
    except (TypeError, ValueError):
        raise ProvenanceError(
            "unknown_ledger_version",
            "ledger version {!r} is not a number".format(version),
        ) from None
    if version_number != LEDGER_VERSION:
        raise ProvenanceError(
            "unknown_ledger_version",
            "this checker reads version {} ledgers, not {}".format(LEDGER_VERSION, version_number),
        )

    raw_sources = document.get("sources")
    if raw_sources is None:
        raise ProvenanceError("no_sources", "a provenance ledger must list its sources")
    if not isinstance(raw_sources, (list, tuple)) or not raw_sources:
        raise ProvenanceError(
            "no_sources",
            "a provenance ledger with no sources audits nothing and would report a "
            "clean drift for every repository Continuum tracks",
        )

    sources: List[TrackedSource] = []
    seen: Dict[str, str] = {}
    for entry in raw_sources:
        source = _read_source(entry)
        if source.id in seen:
            raise ProvenanceError(
                "duplicate_source_id",
                "{} is declared twice; a checker that reported on one of them would "
                "leave the other unaudited without saying so".format(source.id),
            )
        seen[source.id] = source.repository
        sources.append(source)

    return ProvenanceLedger(
        sources=tuple(sorted(sources, key=lambda item: item.id)),
        version=version_number,
        generated_from=str(document.get("generated_from", "") or ""),
    )


def load_provenance(path: str) -> ProvenanceLedger:
    """Read a provenance ledger from a file."""

    with open(path, "r", encoding="utf-8") as handle:
        return read_provenance(json.load(handle))


def missing_references(ledger: ProvenanceLedger, root: str = ".") -> Tuple[str, ...]:
    """Continuum paths a source names that are not in this repository.

    The failure this catches is the ledger's own: a source records where the
    property it claims lives, and after a rename or a deletion that pointer names
    nothing. Nothing else in the plane would notice -- the drift checker reads the
    *source* repository and never looks here -- so a classification whose
    implementation is gone would keep being reported as satisfied by a test that
    also no longer exists.

    Checked against the working tree, so it is a statement about the commit the
    checker runs in rather than about a remembered layout.
    """

    import os

    missing: List[str] = []
    for source in ledger.sources:
        references = list(source.continuum.implementation) + list(source.continuum.tests)
        if source.workflow_ledger:
            references.append(source.workflow_ledger)
        for reference in references:
            if not os.path.exists(os.path.join(root, reference)):
                missing.append("{}: {}".format(source.id, reference))
    return tuple(missing)


def render_markdown(ledger: ProvenanceLedger) -> str:
    """The ledger as a reviewer reads it, generated from the object the gate reads.

    Rendered from the same parsed ledger rather than from the file, so the document
    and the comparison cannot disagree about what is tracked or what it was
    classified as.
    """

    lines = [
        "<!-- Generated by continuum.provenance.ledger.render_markdown from "
        "docs/provenance-ledger.json. -->",
        "",
        "Ledger version: `{}`  ".format(ledger.version),
        "",
        "| Source | Repository | Visibility | Severity | Disposition | Baseline | Last observed | Audited at |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for source in ledger.sources:
        lines.append(
            "| `{}` | {} | {} | {} | `{}` | `{}` | `{}` | {} |".format(
                source.id,
                # A private repository's name is not public metadata, and this is a
                # public document. The row names the source; the repository is in the
                # JSON, for the process that has the credential to read it.
                "`{}`".format(source.repository) if not source.is_private else "_private_",
                source.visibility,
                source.severity,
                source.disposition,
                (source.baseline_sha or "")[:12] or "unknown",
                (source.current_sha or "unobserved")[:12],
                source.audited_at,
            )
        )

    for source in ledger.sources:
        lines.extend(["", "### {}".format(source.id), ""])
        if source.rationale:
            lines.extend([source.rationale, ""])
        lines.extend(
            [
                "* Tracked paths: {}".format(
                    ", ".join("`{}`".format(prefix) for prefix in source.tracked_prefixes)
                    or "none -- tracked by incident"
                ),
                "* Dispositions: {}".format(
                    "; ".join(
                        "`{}` -> `{}`".format(pattern, disposition)
                        for pattern, disposition in source.path_dispositions
                    )
                    or "none recorded, so any advance on a tracked path is unclassified"
                ),
                "* Incidents: {}".format(
                    ", ".join("`{}`".format(item) for item in source.incidents) or "none"
                ),
                "* Continuum implementation: {}".format(
                    ", ".join("`{}`".format(item) for item in source.continuum.implementation)
                ),
                "* Continuum tests: {}".format(
                    ", ".join("`{}`".format(item) for item in source.continuum.tests)
                ),
            ]
        )
        if source.workflow_ledger:
            lines.append("* Per-path classifications: `{}`".format(source.workflow_ledger))
        if source.is_private:
            lines.append(
                "* Credential: the least-privilege read token named by `{}`; without "
                "it the source is reported `unavailable`, never read with the ambient "
                "token.".format(source.credential_env)
            )

    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ContinuumReference",
    "DEFAULT_CREDENTIAL_ENV",
    "DEFAULT_SEVERITY",
    "DISPOSITIONS",
    "LEDGER_VERSION",
    "PERSISTENT_DISPOSITIONS",
    "PROVENANCE_SCHEMA",
    "ProvenanceError",
    "ProvenanceLedger",
    "SEVERITIES",
    "VISIBILITIES",
    "TrackedSource",
    "load_provenance",
    "missing_references",
    "read_provenance",
    "render_markdown",
]