"""The provenance ledger: what Continuum audited upstream, and what it concluded.

``docs/parity-ledger.md`` is the human account of the cross-repository parity
sweep. This module is the same account in a form a program can check, because
the failure this ledger exists to prevent is a *silent* divergence: production
automation in another repository fixes something, Continuum never hears about
it, and the two drift apart over months with nothing red anywhere. A document
only a person reads cannot stop that, and a document only a machine reads
cannot be reviewed. So there are two, and `tests/test_parity_ledger.py` asserts
they agree on every classification.

Three things are recorded per tracked source:

* **Where it was audited.** ``audit.baseline_sha`` and ``audit.current_sha``
  are the pair the last audit covered. Each new audit appends a pair, so the
  advancement of a source is a visible history rather than a single number that
  was quietly overwritten.
* **What was decided.** Every incident carries one of five dispositions, the
  Continuum files that answer it, and the tests that hold the answer in place.
  A disposition with no implementation reference is a claim, not a port.
* **What to watch.** ``paths`` says which paths in the source are
  automation-relevant and what an advance in each one means. This is the only
  part of the ledger a *machine* branches on, and it is why an advance confined
  to a consumer-local path is a green no-op instead of a question.

The loader is strict about the fields the gate branches on and permissive about
the rest, matching the convention in ``shadow/parity.py``: an unrecognised
disposition is refused rather than treated as benign, because "unclassified" and
"classified as harmless" must never collapse into each other.

Two properties of the file are load-bearing and are asserted in CI rather than
documented:

* **It names no private repository.** Continuum is public, so the committed
  ledger can only contain public sources. A private source is supplied as an
  uncommitted overlay (`load_ledger(..., overlay=...)`) and reaches a public log
  only under the alias its operator chose.
* **A tracked path is never an escape.** A pattern that is absolute, traverses,
  or is empty would classify paths outside the repository, so it is refused.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LEDGER_SCHEMA = "continuum.parity-ledger/v1"

# --------------------------------------------------------------------------- #
# The five dispositions
# --------------------------------------------------------------------------- #

ABSORBED = "absorbed"
MUST_PORT = "must-port"
CONSUMER_LOCAL = "consumer-local"
SUPERSEDED = "superseded"
NOT_APPLICABLE = "not-applicable"

DISPOSITIONS: Tuple[str, ...] = (
    ABSORBED,
    MUST_PORT,
    CONSUMER_LOCAL,
    SUPERSEDED,
    NOT_APPLICABLE,
)

#: The dispositions that answer "if this path advances, does a human have to
#: look?".
#:
#: Only two, and the choice is structural rather than a matter of taste. Both
#: are statements about where a fix *belongs*: ``consumer-local`` puts it in one
#: consumer's configuration and ``not-applicable`` says the case cannot arise in
#: Continuum's design. A path with either classification will keep advancing for
#: as long as the repository exists, and triaging each of those advances would
#: train the checker to be ignored.
#:
#: ``absorbed``, ``must-port`` and ``superseded`` are *not* here, and the
#: omission is deliberate. Each of them describes a state that was true of one
#: commit range. An advance past that range is new evidence that no earlier
#: classification covers, so it is triaged. Only a claim about the path itself
#: survives the next commit.
NO_TRIAGE_DISPOSITIONS: Tuple[str, ...] = (CONSUMER_LOCAL, NOT_APPLICABLE)

#: The dispositions that are claims about this repository, and therefore have to
#: name what in this repository answers them. The other two are claims that
#: nothing here answers them, which is a different kind of complete.
REFERENCED_DISPOSITIONS: Tuple[str, ...] = (ABSORBED, MUST_PORT, SUPERSEDED)

PUBLIC = "public"
PRIVATE = "private"
VISIBILITIES: Tuple[str, ...] = (PUBLIC, PRIVATE)

#: The access modes a source read may use. ``public`` is a normal read of a
#: repository anybody can see; ``credential`` is a least-privilege secret
#: supplied by configuration; ``none`` means no read was attempted because none
#: was available.
ACCESS_PUBLIC = "public"
ACCESS_CREDENTIAL = "credential"
ACCESS_NONE = "none"
ACCESS_MODES: Tuple[str, ...] = (ACCESS_PUBLIC, ACCESS_CREDENTIAL, ACCESS_NONE)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Incident ids are looser than source ids on purpose. An incident is named after
#: where it was reported -- ``owner/repo#23``, matching ``docs/parity-ledger.md``
#: so the two accounts cite the same thing -- while a source id has to survive
#: being written into an HTML comment marker and read back out of an issue body,
#: which is why it stays a plain identifier.
_INCIDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/#-]{0,63}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class LedgerError(ValueError):
    """A ledger document that cannot be trusted.

    Carries a machine-readable ``code`` so a workflow can tell a malformed file
    apart from a missing one, and so the error a maintainer sees names the field
    rather than the parser that tripped over it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.detail = message


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def normalize_path(value: str) -> str:
    """Normalise a repository-relative path, refusing anything that is not one.

    A leading ``./`` is dropped and backslashes are folded, because the same
    path arrives spelled differently from a commit message and from a compare
    response. Everything else is refused rather than repaired: an absolute path
    or a ``..`` segment is not a path in the repository, and quietly rewriting it
    into one would classify a file nobody audited.
    """

    candidate = str(value or "").strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate:
        raise LedgerError("empty_path", "a tracked path may not be empty")
    if candidate.startswith("/") or candidate.startswith("~"):
        raise LedgerError(
            "absolute_path", "{!r} is absolute; tracked paths are repository-relative".format(value)
        )
    if ".." in candidate.split("/"):
        raise LedgerError(
            "traversing_path", "{!r} traverses out of the repository".format(value)
        )
    if "//" in candidate:
        raise LedgerError("malformed_path", "{!r} contains an empty segment".format(value))
    return candidate


def _expand_braces(pattern: str) -> List[str]:
    """Expand ``{a,b}`` alternation, which :mod:`fnmatch` does not support.

    One level of nesting, applied repeatedly until the pattern stops changing so
    ``{a,{b,c}}`` also works. Nesting is bounded by the loop condition rather
    than by a depth counter: a pattern either stops changing or grows, and a
    pattern that grows without bound is rejected by the length check below.
    """

    expanded = [pattern]
    for _ in range(8):
        nxt: List[str] = []
        changed = False
        for candidate in expanded:
            start = candidate.find("{")
            if start < 0:
                nxt.append(candidate)
                continue
            depth = 0
            end = -1
            for index in range(start, len(candidate)):
                if candidate[index] == "{":
                    depth += 1
                elif candidate[index] == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
            if end < 0:
                raise LedgerError("unbalanced_brace", "{!r} has an unbalanced brace".format(pattern))
            alternatives = _split_alternatives(candidate[start + 1 : end])
            changed = True
            for alternative in alternatives:
                nxt.append(candidate[:start] + alternative + candidate[end + 1 :])
        expanded = nxt
        if not changed:
            break
    if any(len(candidate) > 512 for candidate in expanded):
        raise LedgerError("path_pattern_too_long", "{!r} expands past 512 characters".format(pattern))
    return expanded


def _split_alternatives(body: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part for part in parts if part != ""] or [""]


def path_matches(pattern: str, path: str) -> bool:
    """Whether a tracked pattern covers a repository-relative path.

    Three shapes, in this order:

    * a trailing ``/`` is a directory prefix, so ``.github/workflows/`` covers
      everything under it without depending on how deep the tree goes;
    * a pattern with no wildcard is an exact path, matched exactly rather than
      as a prefix, because ``opencode.yml`` must not quietly cover
      ``consumer-opencode.yml``;
    * anything else is a glob. ``fnmatch``'s ``*`` also crosses ``/``, which
      *widens* the set of paths a pattern claims. That is the safe direction for
      this file: a path that matches a rule it was not meant to match is
      triaged, not ignored, so the only cost of an over-broad glob is a
      question.
    """

    candidate = normalize_path(path)
    for expanded in _expand_braces(pattern):
        if expanded.endswith("/"):
            if candidate.startswith(expanded):
                return True
            continue
        if not any(char in expanded for char in "*?["):
            if candidate == expanded:
                return True
            continue
        if fnmatch.fnmatchcase(candidate, expanded):
            return True
    return False


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


def _require_str(document: Mapping[str, Any], key: str, where: str, *, code: str) -> str:
    if key not in document:
        raise LedgerError(code, "{} is missing {!r}".format(where, key))
    value = document[key]
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(code, "{} has a non-string {!r}".format(where, key))
    return value.strip()


def _optional_str(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any, where: str, key: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise LedgerError(
            "malformed_list", "{} {!r} must be a list of strings".format(where, key)
        )
    items: List[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise LedgerError(
                "malformed_list", "{} {!r} contains a non-string entry".format(where, key)
            )
        items.append(entry.strip())
    return tuple(items)


@dataclasses.dataclass(frozen=True)
class PathRule:
    """One tracked path in a source, and what an advance in it means.

    ``disposition`` is the classification of the *path*, not of one commit. It
    is the only field the drift checker branches on, which is why an unclassified
    path is treated as automation-relevant rather than as unknown-and-therefore-
    fine.
    """

    pattern: str
    disposition: str
    note: str = ""

    @property
    def needs_triage(self) -> bool:
        return self.disposition not in NO_TRIAGE_DISPOSITIONS

    @classmethod
    def from_document(cls, document: Any, where: str) -> "PathRule":
        if not isinstance(document, Mapping):
            raise LedgerError("malformed_path_rule", "{} is not an object".format(where))
        pattern = _require_str(document, "pattern", where, code="malformed_path_rule")
        if not any(char in pattern for char in "*?[{") and not pattern.endswith("/"):
            # An exact pattern is a file; make that explicit so a rule that was
            # meant to cover a directory cannot be written as one and silently
            # match nothing.
            normalize_path(pattern)
        else:
            for expanded in _expand_braces(pattern):
                normalize_path(expanded)
        disposition = _require_str(
            document, "disposition", where, code="unknown_disposition"
        )
        if disposition not in DISPOSITIONS:
            raise LedgerError(
                "unknown_disposition",
                "{}: {!r} is not one of {}".format(where, disposition, list(DISPOSITIONS)),
            )
        return cls(
            pattern=pattern,
            disposition=disposition,
            note=_optional_str(document, "note"),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern,
            "disposition": self.disposition,
            "note": self.note,
        }


@dataclasses.dataclass(frozen=True)
class Incident:
    """One upstream report, its classification, and where Continuum answers it."""

    id: str
    disposition: str
    paths: Tuple[str, ...] = ()
    implementation: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def from_document(cls, document: Any, where: str) -> "Incident":
        if not isinstance(document, Mapping):
            raise LedgerError("malformed_incident", "{} is not an object".format(where))
        identifier = _require_str(document, "id", where, code="malformed_incident")
        if not _INCIDENT_ID_RE.match(identifier):
            raise LedgerError(
                "malformed_incident",
                "{}: incident id {!r} must be owner/repo#number or repo#number".format(
                    where, identifier
                ),
            )
        disposition = _require_str(
            document, "disposition", where, code="unknown_disposition"
        )
        if disposition not in DISPOSITIONS:
            raise LedgerError(
                "unknown_disposition",
                "{}: incident {} has disposition {!r}, which is not one of {}".format(
                    where, identifier, disposition, list(DISPOSITIONS)
                ),
            )
        paths = tuple(normalize_path(item) for item in _string_list(document.get("paths"), where, "paths"))
        implementation = _string_list(document.get("implementation"), where, "implementation")
        tests = _string_list(document.get("tests"), where, "tests")
        for label, values in (("implementation", implementation), ("tests", tests)):
            for value in values:
                if value.startswith("/") or ".." in value.split("/"):
                    raise LedgerError(
                        "absolute_reference",
                        "{}: incident {} {} reference {!r} is not repository-relative".format(
                            where, identifier, label, value
                        ),
                    )
        return cls(
            id=identifier,
            disposition=disposition,
            paths=paths,
            implementation=implementation,
            tests=tests,
            note=_optional_str(document, "note"),
        )

    @property
    def answered(self) -> bool:
        """Whether the classification points at something in this repository.

        Whether a reference is required at all depends on the disposition, which
        is the whole point of having five rather than one. ``absorbed`` and
        ``superseded`` are claims about a property Continuum holds, and a claim
        with no pointer cannot be checked by anybody; ``must-port`` is a claim
        that a gap was closed somewhere. All three need an implementation or test
        reference, and a ``must-port`` without one is a gap nobody has closed.

        ``consumer-local`` and ``not-applicable`` are answered by construction:
        their whole content is that nothing belongs in this repository, so
        demanding a reference would be demanding a file whose only content would be
        that there is nothing to write.
        """

        if self.disposition not in REFERENCED_DISPOSITIONS:
            return True
        return bool(self.implementation) or bool(self.tests)

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "disposition": self.disposition,
            "paths": list(self.paths),
            "implementation": list(self.implementation),
            "tests": list(self.tests),
            "note": self.note,
        }


@dataclasses.dataclass(frozen=True)
class AuditRecord:
    """The commit range one audit covered, and when and by what."""

    baseline_sha: str
    current_sha: str
    audited_at: str
    audited_by: str = ""
    ledger_version: int = 0

    @classmethod
    def from_document(cls, document: Any, where: str) -> "AuditRecord":
        if not isinstance(document, Mapping):
            raise LedgerError("malformed_audit", "{} is not an object".format(where))
        baseline = _require_str(document, "baseline_sha", where, code="malformed_audit")
        current = _require_str(document, "current_sha", where, code="malformed_audit")
        for label, value in (("baseline_sha", baseline), ("current_sha", current)):
            if not _SHA_RE.match(value):
                raise LedgerError(
                    "malformed_audit",
                    "{}: {} {!r} is not a commit sha".format(where, label, value),
                )
        audited_at = _require_str(document, "audited_at", where, code="malformed_audit")
        if not _TIMESTAMP_RE.match(audited_at):
            raise LedgerError(
                "malformed_audit",
                "{}: audited_at {!r} is not an ISO 8601 UTC timestamp".format(where, audited_at),
            )
        try:
            version = int(document.get("ledger_version", 0) or 0)
        except (TypeError, ValueError):
            raise LedgerError(
                "malformed_audit", "{}: ledger_version must be an integer".format(where)
            ) from None
        if version < 0:
            raise LedgerError("malformed_audit", "{}: ledger_version is negative".format(where))
        return cls(
            baseline_sha=baseline,
            current_sha=current,
            audited_at=audited_at,
            audited_by=_optional_str(document, "audited_by"),
            ledger_version=version,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "baseline_sha": self.baseline_sha,
            "current_sha": self.current_sha,
            "audited_at": self.audited_at,
            "audited_by": self.audited_by,
            "ledger_version": self.ledger_version,
        }


@dataclasses.dataclass(frozen=True)
class Source:
    """One tracked upstream repository."""

    id: str
    repository: str
    visibility: str
    audit: AuditRecord
    paths: Tuple[PathRule, ...] = ()
    incidents: Tuple[Incident, ...] = ()
    note: str = ""

    @property
    def private(self) -> bool:
        return self.visibility == PRIVATE

    def classify(self, path: str) -> Optional[PathRule]:
        """The first rule that covers ``path``, or ``None``.

        First match wins and the rules are ordered by the maintainer, so a
        specific rule can be placed above a broad one. A path that matches no
        rule has no classification at all, which the drift plane treats as
        automation-relevant: an unclassified path is a question, never a pass.
        """

        candidate = normalize_path(path)
        for rule in self.paths:
            if path_matches(rule.pattern, candidate):
                return rule
        return None

    def advance_recorded(
        self, current_sha: str, *, baseline_sha: str = "", audited_at: str = "", audited_by: str = ""
    ) -> "Source":
        """A copy whose audit range ends at ``current_sha``.

        Used to close out a source that advanced only in paths nobody has to
        triage, so a green no-op still leaves the advancement on the record
        instead of re-detecting it on every scan.
        """

        return dataclasses.replace(
            self,
            audit=AuditRecord(
                baseline_sha=baseline_sha or self.audit.current_sha,
                current_sha=current_sha,
                audited_at=audited_at or self.audit.audited_at,
                audited_by=audited_by or self.audit.audited_by,
                ledger_version=self.audit.ledger_version,
            ),
        )

    @classmethod
    def from_document(cls, document: Any, where: str) -> "Source":
        if not isinstance(document, Mapping):
            raise LedgerError("malformed_source", "{} is not an object".format(where))
        identifier = _require_str(document, "id", where, code="malformed_source")
        if not _ID_RE.match(identifier):
            raise LedgerError(
                "malformed_source", "{}: source id {!r} is not a safe identifier".format(where, identifier)
            )
        repository = _require_str(document, "repository", where, code="malformed_repository")
        if not _REPOSITORY_RE.match(repository):
            raise LedgerError(
                "malformed_repository",
                "{}: repository {!r} is not owner/name".format(where, repository),
            )
        visibility = _require_str(document, "visibility", where, code="malformed_visibility")
        if visibility not in VISIBILITIES:
            raise LedgerError(
                "malformed_visibility",
                "{}: visibility {!r} is not one of {}".format(where, visibility, list(VISIBILITIES)),
            )
        paths = tuple(
            PathRule.from_document(entry, "{} source {} path {}".format(where, identifier, index))
            for index, entry in enumerate(document.get("paths") or ())
        )
        incidents = tuple(
            Incident.from_document(entry, "{} source {} incident {}".format(where, identifier, index))
            for index, entry in enumerate(document.get("incidents") or ())
        )
        seen: Dict[str, int] = {}
        for incident in incidents:
            if incident.id in seen:
                raise LedgerError(
                    "duplicate_incident",
                    "{}: source {} records incident {} twice".format(where, identifier, incident.id),
                )
            seen[incident.id] = 1
        return cls(
            id=identifier,
            repository=repository,
            visibility=visibility,
            audit=AuditRecord.from_document(
                document.get("audit") or {}, "{} source {}".format(where, identifier)
            ),
            paths=paths,
            incidents=incidents,
            note=_optional_str(document, "note"),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "visibility": self.visibility,
            "audit": self.audit.describe(),
            "paths": [rule.describe() for rule in self.paths],
            "incidents": [incident.describe() for incident in self.incidents],
            "note": self.note,
        }


@dataclasses.dataclass(frozen=True)
class Problem:
    """A finding about a ledger that parsed but cannot be fully trusted."""

    severity: str
    code: str
    message: str


#: The ledger is not an audit record: something in it cannot be checked at all.
ERROR = "error"
#: The ledger is an audit record with a gap a reviewer should see.
WARNING = "warning"


@dataclasses.dataclass(frozen=True)
class Ledger:
    """Every tracked source, at the version of the ledger that read them."""

    version: int
    generated_at: str
    sources: Tuple[Source, ...] = ()
    #: Where the document came from, and the schema it declared. A report states
    #: these so "the ledger said so" is a checkable claim.
    source_path: str = ""
    schema: str = LEDGER_SCHEMA
    #: Sources contributed by an uncommitted overlay, by id. Recorded separately
    #: so a public artifact can say which sources were never in the committed
    #: file.
    overlay_ids: Tuple[str, ...] = ()

    def get(self, identifier: str) -> Optional[Source]:
        for source in self.sources:
            if source.id == identifier:
                return source
        return None

    def public_sources(self) -> Tuple[Source, ...]:
        return tuple(source for source in self.sources if not source.private)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "generated_at": self.generated_at,
            "source_path": self.source_path,
            "overlay_ids": list(self.overlay_ids),
            "sources": [source.describe() for source in self.sources],
        }

    def problems(self) -> Tuple[Problem, ...]:
        """Findings about a ledger that parsed but cannot be fully trusted.

        Distinct from :func:`load`, which refuses a document it cannot parse at
        all. These are judgements, and they carry a severity because the two
        kinds are not the same kind of thing: an ``error`` means the file is not
        the audit record it claims to be -- a private repository named in a
        committed ledger, or a source that tracks no path at all -- and the
        check that reads this exits non-zero on it. A ``warning`` is a real gap
        with a legitimate reason to exist: a ``must-port`` classification whose
        port is still in flight. Reporting that as an error would mean refusing
        to record a classification the checker is about to rely on, which is how
        a ledger stops being updated.
        """

        found: List[Problem] = []
        for source in self.sources:
            if source.private and source.id not in self.overlay_ids:
                found.append(
                    Problem(
                        severity=ERROR,
                        code="private-source-in-committed-ledger",
                        message=(
                            "source {} is {} and is not supplied by an overlay: a "
                            "committed ledger in a public repository must not name "
                            "it".format(source.id, source.visibility)
                        ),
                    )
                )
            if not source.paths:
                found.append(
                    Problem(
                        severity=ERROR,
                        code="source-tracks-no-path",
                        message=(
                            "source {} tracks no path, so every advance in it is "
                            "unclassified".format(source.id)
                        ),
                    )
                )
            for incident in source.incidents:
                if not incident.answered:
                    found.append(
                        Problem(
                            severity=WARNING,
                            code="unanswered-incident",
                            message=(
                                "source {} incident {} is classified {} with no "
                                "Continuum implementation or test reference".format(
                                    source.id, incident.id, incident.disposition
                                )
                            ),
                        )
                    )
        return tuple(found)

    def errors(self) -> Tuple[Problem, ...]:
        return tuple(item for item in self.problems() if item.severity == ERROR)

    def warnings(self) -> Tuple[Problem, ...]:
        return tuple(item for item in self.problems() if item.severity == WARNING)

    def validate(self) -> List[str]:
        """Every finding as one line, oldest interface kept for a job summary."""

        return ["{}: {}".format(item.code, item.message) for item in self.problems()]


def ledger_from_document(
    document: Any, *, source_path: str = "", overlay_ids: Sequence[str] = ()
) -> Ledger:
    """Read a ledger document.

    Permissive about fields it does not know, strict about the ones the gate
    branches on, for the reason given in the module docstring: a typo in a
    disposition must be a failed load, not a source that quietly stops being
    triaged.
    """

    if not isinstance(document, Mapping):
        raise LedgerError("not_a_mapping", "a parity ledger must be an object")
    schema = document.get("schema")
    if schema != LEDGER_SCHEMA:
        raise LedgerError(
            "unknown_schema", "expected {!r}, found {!r}".format(LEDGER_SCHEMA, schema)
        )
    try:
        version = int(document.get("version", 0) or 0)
    except (TypeError, ValueError):
        raise LedgerError("malformed_version", "version must be an integer") from None
    if version < 1:
        raise LedgerError("malformed_version", "version must be at least 1")
    generated_at = _require_str(document, "generated_at", "ledger", code="malformed_timestamp")
    if not _TIMESTAMP_RE.match(generated_at):
        raise LedgerError(
            "malformed_timestamp",
            "generated_at {!r} is not an ISO 8601 UTC timestamp".format(generated_at),
        )
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, (list, tuple)):
        raise LedgerError("malformed_sources", "sources must be a list")
    sources: List[Source] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(raw_sources):
        source = Source.from_document(entry, "ledger source {}".format(index))
        if source.id in seen:
            raise LedgerError("duplicate_source", "source id {!r} appears twice".format(source.id))
        seen[source.id] = 1
        sources.append(source)
    return Ledger(
        version=version,
        generated_at=generated_at,
        sources=tuple(sources),
        source_path=source_path,
        overlay_ids=tuple(overlay_ids),
    )


def load(path: str, *, overlay: Optional[str] = None) -> Ledger:
    """Read the committed ledger, optionally merging an uncommitted overlay.

    The overlay exists for private sources. Continuum is public, so a private
    repository cannot be named in a committed file, but it still has to be
    audited; the overlay is the file that names it, it is read from wherever the
    operator keeps it, and the sources it contributes are marked as
    overlay-sourced so every artifact can say so.
    """

    base = _read(path, "committed")
    if not overlay:
        return base
    extra = _read(overlay, "overlay")
    merged: Dict[str, Source] = {source.id: source for source in base.sources}
    for source in extra.sources:
        merged[source.id] = source
    return dataclasses.replace(
        base,
        sources=tuple(merged[key] for key in sorted(merged)),
        overlay_ids=tuple(sorted({item.id for item in extra.sources})),
    )


def _read(path: str, label: str) -> Ledger:
    candidate = Path(path)
    if not candidate.is_file():
        raise LedgerError(
            "ledger_not_found", "{} ledger not found: {}".format(label, candidate)
        )
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LedgerError("malformed_ledger", "{} ledger is not valid JSON: {}".format(label, error)) from None
    return ledger_from_document(document, source_path=str(candidate))


def dump(ledger: Ledger) -> str:
    """The ledger as a document, for a change that updates it."""

    return json.dumps(ledger.describe(), indent=2, sort_keys=True) + "\n"


def disposition_counts(ledger: Ledger) -> Dict[str, int]:
    """How many incidents carry each disposition, across every source.

    The five dispositions are the ledger's whole vocabulary, so a count that
    does not add up to the number of incidents means one was recorded outside
    the vocabulary -- which the loader already refuses, and which this makes
    visible in a summary.
    """

    counts = {name: 0 for name in DISPOSITIONS}
    for source in ledger.sources:
        for incident in source.incidents:
            counts[incident.disposition] = counts.get(incident.disposition, 0) + 1
    return counts


def unclassified_paths(source: Source) -> Iterable[str]:
    """Paths in a source's tracked set that no rule would claim.

    Not reachable through :meth:`Source.classify` by construction, so this walks
    the incident references instead: an incident that points at a Continuum file
    nobody tracks on the source side is a path classification that is missing.
    """

    tracked = {normalize_path(path) for incident in source.incidents for path in incident.paths}
    for path in sorted(tracked):
        if source.classify(path) is None:
            yield path


__all__ = [
    "ABSORBED",
    "ACCESS_CREDENTIAL",
    "ACCESS_MODES",
    "ACCESS_NONE",
    "ACCESS_PUBLIC",
    "AuditRecord",
    "CONSUMER_LOCAL",
    "DISPOSITIONS",
    "ERROR",
    "Incident",
    "LEDGER_SCHEMA",
    "Ledger",
    "LedgerError",
    "MUST_PORT",
    "NO_TRIAGE_DISPOSITIONS",
    "NOT_APPLICABLE",
    "PRIVATE",
    "PUBLIC",
    "PathRule",
    "Problem",
    "REFERENCED_DISPOSITIONS",
    "SUPERSEDED",
    "Source",
    "VISIBILITIES",
    "WARNING",
    "disposition_counts",
    "dump",
    "ledger_from_document",
    "load",
    "normalize_path",
    "path_matches",
    "unclassified_paths",
]
