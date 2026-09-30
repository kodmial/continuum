"""The rolling parity baseline: what the consumer repository looks like *now*.

The gate in :mod:`continuum.shadow.cutover` decides whether a window of recorded
events is good enough to hand a decision over. On its own that answer goes stale
quietly. A window recorded last week says the shadow engine agreed with production
on the paths it saw; it says nothing about the two repositories since. So a
baseline is not a snapshot taken once and kept in ``reference/``, it is a reading
taken immediately before the cutover decision, of three things:

* the consumer's **current default-branch HEAD**;
* **every active ``.github/workflows/*`` file and its blob SHA**;
* **every open pull request that touches ``.github/**``**.

Those are compared against an approved ledger (:data:`LEDGER_SCHEMA`), and the
differences are routed to the issues that own them. The rules that make the answer
worth reading:

* **A historical snapshot can never authorize cutover.** The ledger records what
  was audited; this module records what is true now. If the two differ, the audit
  is out of date and that is a blocker, not a footnote.

* **Absence is failure.** If the head, the workflow inventory, or the open pull
  request list could not be read in full, the capture says so and the verdict is
  ``incomplete``. A truncated inventory compares equal to a short one and would
  report a clean drift for files nobody looked at.

* **An unknown file is a finding, not a pass.** A workflow nobody classified
  cannot be compared against anything, so it is routed to the audit issue and
  blocks. The alternative -- ignoring files with no ledger entry -- would make the
  ledger's completeness the thing being measured.

* **Routing is mechanical.** Every difference names the issue that owns it, drawn
  from the ledger and, for something discovered rather than audited, from the
  ledger's declared discovery issue. The gate never decides that a difference does
  not matter; that is the ledger's claim and the ledger is reviewed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LEDGER_SCHEMA = "continuum.parity-ledger/v1"
BASELINE_SCHEMA = "continuum.shadow-baseline/v1"

#: The five classifications the ledger may use, and the only ones. A document
#: naming anything else is refused: an unrecognised classification and "absorbed"
#: must never collapse into each other.
CLASSIFICATIONS: Tuple[str, ...] = (
    "absorbed",
    "must-port",
    "consumer-local",
    "superseded",
    "not-applicable",
)

#: The issues a generic difference may be routed to. The ledger's declared owners
#: are checked against this, so a ledger cannot invent a destination that nobody
#: will ever read.
ROUTABLE_ISSUES: Tuple[str, ...] = ("#60", "#11", "#27", "#21", "#22")

#: The prefix that makes a changed path part of this gate rather than of ordinary
#: product work. A pull request that touches it can reintroduce or remove an
#: orchestration writer, which is what cutover has to reason about.
WORKFLOW_PREFIX = ".github/workflows/"

#: Any path under this prefix is in scope for the open-pull-request half of the
#: reading, not just the workflows directory: `.github/actions/**` and composite
#: action definitions can carry the same writers.
GITHUB_PREFIX = ".github/"

_WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: The single blocker an incomplete reading may carry. Named so the coherence
#: check can tell "we could not read everything" from "we read everything and it
#: does not match": the first is a gap in the instrument, the second is a finding
#: about the repository, and conflating them would let a drifted repository be
#: filed as an infrastructure problem and routed away from the issues that own it.
INCOMPLETE_BLOCKER = "live_head_incomplete"


class BaselineError(ValueError):
    """The baseline cannot be evaluated from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# The live reading
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class WorkflowFile:
    """One active workflow, by the only identifier that says what it contains."""

    path: str
    blob_sha: str


@dataclasses.dataclass(frozen=True)
class OpenPullRequest:
    """One open pull request, and the `.github/**` paths it touches."""

    number: int
    head_sha: str
    paths: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "head_sha": self.head_sha,
            "paths": list(self.paths),
        }


@dataclasses.dataclass(frozen=True)
class LiveHead:
    """What the consumer repository looks like at this instant."""

    repository: str
    default_branch: str = ""
    head_sha: str = ""
    workflows: Tuple[WorkflowFile, ...] = ()
    open_pull_requests: Tuple[OpenPullRequest, ...] = ()
    #: False when a read did not complete. The verdict is then ``incomplete``
    #: whatever the differences say.
    complete: bool = True
    limits: Tuple[str, ...] = ()

    @property
    def workflow_map(self) -> Dict[str, str]:
        return {entry.path: entry.blob_sha for entry in self.workflows}

    @property
    def pull_request_map(self) -> Dict[int, Tuple[str, ...]]:
        return {entry.number: entry.paths for entry in self.open_pull_requests}

    @property
    def pull_request_heads(self) -> Dict[int, str]:
        return {entry.number: entry.head_sha for entry in self.open_pull_requests}

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": BASELINE_SCHEMA,
            "kind": "live-head",
            "repository": self.repository,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "complete": self.complete,
            "limits": list(self.limits),
            "workflows": [
                {"path": entry.path, "blob_sha": entry.blob_sha}
                for entry in sorted(self.workflows, key=lambda item: item.path)
            ],
            "open_pull_requests": [
                entry.describe() for entry in sorted(self.open_pull_requests, key=lambda item: item.number)
            ],
        }

    @property
    def digest(self) -> str:
        """A digest of the reading, so two readings can be told apart."""

        payload = {
            "repository": self.repository,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "workflows": sorted(self.workflow_map.items()),
            "open_pull_requests": sorted(
                (entry.number, entry.head_sha, sorted(entry.paths))
                for entry in self.open_pull_requests
            ),
        }
        return "lh-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]


def read_live_head(document: Mapping[str, Any]) -> LiveHead:
    """Read a recorded live reading back from an artifact."""

    if not isinstance(document, Mapping):
        raise BaselineError("live_head_not_a_mapping", "a live-head reading must be an object")
    schema = document.get("schema")
    if schema not in (BASELINE_SCHEMA, None):
        raise BaselineError(
            "unknown_live_head_schema",
            "expected {!r}, found {!r}".format(BASELINE_SCHEMA, schema),
        )
    return LiveHead(
        repository=str(document.get("repository", "")),
        default_branch=str(document.get("default_branch", "")),
        head_sha=str(document.get("head_sha", "")),
        workflows=tuple(
            WorkflowFile(
                path=str(entry.get("path", "")), blob_sha=str(entry.get("blob_sha", ""))
            )
            for entry in document.get("workflows", []) or []
            if isinstance(entry, Mapping)
        ),
        open_pull_requests=tuple(
            OpenPullRequest(
                number=int(entry.get("number", 0) or 0),
                head_sha=str(entry.get("head_sha", "")),
                paths=tuple(str(path) for path in entry.get("paths", []) or []),
            )
            for entry in document.get("open_pull_requests", []) or []
            if isinstance(entry, Mapping)
        ),
        complete=bool(document.get("complete", True)),
        limits=tuple(str(item) for item in document.get("limits", []) or []),
    )


def capture_live_head(
    client: Any,
    *,
    repository: str = "",
    max_pull_requests: int = 100,
) -> LiveHead:
    """Take the reading, through the same read-only client the shadow plane uses.

    Every read is attempted even after one fails, because the useful report is the
    whole picture with the gaps named, not the first error. What it will not do is
    guess: a read that did not complete marks the capture incomplete, and an
    incomplete capture never authorizes anything.
    """

    repository = repository or getattr(client, "repository", "") or ""
    limits: List[str] = []
    default_branch = ""
    head_sha = ""
    workflows: List[WorkflowFile] = []
    pulls: List[OpenPullRequest] = []

    try:
        default_branch = str(client.default_branch())
    except Exception as error:  # noqa: BLE001 - an unread branch is a limit, not a crash
        limits.append("default branch could not be read: {}".format(error))
    if default_branch:
        try:
            head_sha = str(client.ref_sha(default_branch))
        except Exception as error:  # noqa: BLE001
            limits.append("default-branch HEAD could not be read: {}".format(error))
    else:
        limits.append("default-branch HEAD was not read")

    try:
        workflows = [
            WorkflowFile(path=path, blob_sha=sha)
            for path, sha in sorted(client.workflow_inventory(default_branch).items())
            if path
        ]
    except Exception as error:  # noqa: BLE001
        limits.append("workflow inventory could not be read: {}".format(error))
        workflows = []

    # An empty inventory is a legitimate reading -- a repository may have no
    # workflows -- so it is not treated as a limit. What is not legitimate is a
    # read that failed, and that has already been recorded above.
    for entry in _open_pull_requests(client, limit=limits):
        if len(pulls) >= max(0, max_pull_requests):
            # Silently truncating would report a repository with no unclassified pull
            # request as clean, which is the one thing the cutover gate must never
            # claim. A capture that did not read everything says so.
            limits.append(
                "more than {} open .github/** pull requests; the rest were not read".format(
                    max_pull_requests
                )
            )
            break
        pulls.append(entry)

    return LiveHead(
        repository=repository,
        default_branch=default_branch,
        head_sha=head_sha,
        workflows=tuple(workflows),
        open_pull_requests=tuple(sorted(pulls, key=lambda item: item.number)),
        complete=not limits,
        limits=tuple(limits),
    )


def _open_pull_requests(client: Any, *, limit: List[str]) -> List[OpenPullRequest]:
    """Every open pull request whose changed paths include ``.github/**``.

    The file list is a separate read per pull request, so a failure names the pull
    request it belongs to. A pull request whose paths could not be read is dropped
    and recorded: reporting it with no paths would claim it touches nothing.
    """

    found: List[OpenPullRequest] = []
    try:
        listed = list(client.list_pulls(state="open"))
    except Exception as error:  # noqa: BLE001
        limit.append("open pull requests could not be listed: {}".format(error))
        return found
    for pull in listed:
        number = int(pull.get("number", 0) or 0)
        head_sha = str((pull.get("head") or {}).get("sha", "") or "")
        try:
            files = client.list_pull_files(number)
        except Exception as error:  # noqa: BLE001
            limit.append("pull request #{} files could not be read: {}".format(number, error))
            continue
        paths = tuple(
            sorted(
                str(entry.get("filename", ""))
                for entry in files or []
                if str(entry.get("filename", "")).startswith(GITHUB_PREFIX)
            )
        )
        if paths:
            found.append(OpenPullRequest(number=number, head_sha=head_sha, paths=paths))
    return found


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class LedgerEntry:
    """What was audited for one workflow, and who owns it."""

    path: str
    blob_sha: str
    classification: str
    owner: str = ""
    rationale: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "blob_sha": self.blob_sha,
            "classification": self.classification,
            "owner": self.owner,
            "rationale": self.rationale,
        }


@dataclasses.dataclass(frozen=True)
class LedgerPullRequest:
    """What was audited for one open pull request that touches `.github/**`.

    ``head_sha`` is the commit the classification was granted against, and it is
    required rather than optional. Paths alone cannot tell a reader that the
    classification still applies: a pull request can keep its exact path set and
    rewrite every one of those files -- adding the orchestration writer this gate
    exists to keep out is a content change, not a path change. Without the head
    commit, "classified" would silently mean "classified once, at some point".
    """

    number: int
    head_sha: str
    paths: Tuple[str, ...]
    classification: str
    owner: str = ""
    rationale: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "head_sha": self.head_sha,
            "paths": list(self.paths),
            "classification": self.classification,
            "owner": self.owner,
            "rationale": self.rationale,
        }


@dataclasses.dataclass(frozen=True)
class ParityLedger:
    """The approved audit: which workflow blobs, at which classifications."""

    repository: str
    audited_head: str = ""
    audited_at: str = ""
    #: Where a difference nobody had classified is routed. Defaults to this issue.
    discovery_issue: str = "#60"
    workflows: Tuple[LedgerEntry, ...] = ()
    open_pull_requests: Tuple[LedgerPullRequest, ...] = ()

    @property
    def workflow_map(self) -> Dict[str, LedgerEntry]:
        return {entry.path: entry for entry in self.workflows}

    @property
    def pull_request_map(self) -> Dict[int, LedgerPullRequest]:
        return {entry.number: entry for entry in self.open_pull_requests}

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "repository": self.repository,
            "audited_head": self.audited_head,
            "audited_at": self.audited_at,
            "discovery_issue": self.discovery_issue,
            "workflows": [entry.describe() for entry in self.workflows],
            "open_pull_requests": [entry.describe() for entry in self.open_pull_requests],
        }

    @property
    def digest(self) -> str:
        payload = {
            "repository": self.repository,
            "discovery_issue": self.discovery_issue,
            "workflows": sorted(
                (entry.path, entry.blob_sha, entry.classification, entry.owner)
                for entry in self.workflows
            ),
            "open_pull_requests": sorted(
                (entry.number, entry.head_sha, entry.classification, entry.owner)
                for entry in self.open_pull_requests
            ),
        }
        return "pl-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]

    def at(
        self,
        head: LiveHead,
        *,
        audited_at: str = "",
        accept_removed: Sequence[str] = (),
        accept_closed: Sequence[int] = (),
    ) -> "ParityLedger":
        """Re-pin this ledger to a live reading that has been re-audited.

        Moving the recorded SHAs forward is a human act, so this only ever moves
        bytes a person has already classified. It is deliberately unable to grow
        the audited set: a file or pull request that was not in the ledger has not
        been classified, and inventing a classification for it here would be
        exactly the silent absorption the gate exists to prevent.

        Removals need saying out loud. A workflow that left the repository and a
        pull request that closed are both ordinary, and neither can reintroduce
        an orchestration writer, so re-pinning past them is legitimate -- but only
        when the caller names them. Without that, the ledger and the repository
        would stay permanently out of step with a difference nobody ever looked
        at, and the one tool meant to resolve the blocker would refuse to run.
        """

        live_workflows = head.workflow_map
        live_pulls = head.pull_request_map
        live_heads = head.pull_request_heads
        here_workflows = self.workflow_map
        here_pulls = self.pull_request_map

        added = sorted(set(live_workflows) - set(here_workflows))
        if added:
            raise BaselineError(
                "ledger_set_changed",
                "the live repository has {} workflow(s) the ledger does not audit: {}. "
                "Classify each one before re-pinning -- a file nobody read cannot "
                "become classified by being copied into the ledger.".format(
                    len(added), ", ".join(added)
                ),
            )
        added_pulls = sorted(set(live_pulls) - set(here_pulls))
        if added_pulls:
            raise BaselineError(
                "ledger_pull_requests_changed",
                "{} open pull request(s) touching .github/** are not classified: {}. "
                "Classify them before re-pinning -- an unclassified pull request is "
                "one the cutover cannot rule out.".format(
                    len(added_pulls),
                    ", ".join("#{}".format(number) for number in added_pulls),
                ),
            )

        withdrawn = sorted(set(here_workflows) - set(live_workflows))
        unacknowledged = [path for path in withdrawn if path not in set(accept_removed)]
        if unacknowledged:
            raise BaselineError(
                "withdrawals_not_acknowledged",
                "{} audited workflow(s) are no longer active and were not named in "
                "accept_removed: {}. Pass them to accept_removed once you have "
                "established where their behaviour went.".format(
                    len(unacknowledged), ", ".join(unacknowledged)
                ),
            )
        gone = sorted(number for number in set(here_pulls) - set(live_pulls))
        unacknowledged_pulls = [
            number for number in gone if number not in set(accept_closed)
        ]
        if unacknowledged_pulls:
            raise BaselineError(
                "closings_not_acknowledged",
                "{} classified pull request(s) are no longer open and were not named "
                "in accept_closed: {}. Pass them to accept_closed once you have "
                "checked whether their .github/** changes were merged.".format(
                    len(unacknowledged_pulls),
                    ", ".join("#{}".format(number) for number in unacknowledged_pulls),
                ),
            )

        return dataclasses.replace(
            self,
            audited_head=head.head_sha or self.audited_head,
            audited_at=audited_at or self.audited_at,
            workflows=tuple(
                dataclasses.replace(entry, blob_sha=live_workflows[entry.path])
                for entry in sorted(self.workflows, key=lambda item: item.path)
                if entry.path in live_workflows
            ),
            open_pull_requests=tuple(
                dataclasses.replace(
                    entry,
                    head_sha=live_heads.get(entry.number, entry.head_sha),
                    paths=tuple(sorted(live_pulls[entry.number])),
                )
                for entry in sorted(self.open_pull_requests, key=lambda item: item.number)
                if entry.number in live_pulls
            ),
        )


def dump_ledger(ledger: ParityLedger, *, indent: int = 2) -> str:
    """Serialise a ledger in the form :func:`read_ledger` accepts.

    Round-tripping through this is checked in the test suite: a ledger that only
    reads back under hand-tweaking is a ledger whose next refresh is a guess.
    """

    return json.dumps(ledger.describe(), indent=indent, sort_keys=False) + "\n"


def read_ledger(document: Mapping[str, Any]) -> ParityLedger:
    """Read an approved ledger, refusing anything it cannot vouch for.

    The strictness is the point of the type. A ledger that names an unknown
    classification, a workflow with no blob, or an owner outside the routable set
    is not a ledger this gate can compare anything against, and accepting it would
    turn a typo into a clean drift report.
    """

    if not isinstance(document, Mapping):
        raise BaselineError("ledger_not_a_mapping", "a parity ledger must be an object")
    schema = document.get("schema")
    if schema != LEDGER_SCHEMA:
        raise BaselineError(
            "unknown_ledger_schema",
            "expected {!r}, found {!r}".format(LEDGER_SCHEMA, schema),
        )
    repository = str(document.get("repository", ""))
    if not repository.strip():
        raise BaselineError("ledger_repository_missing", "a ledger must name its repository")

    discovery_issue = str(document.get("discovery_issue", "") or "#60")
    if discovery_issue not in ROUTABLE_ISSUES:
        raise BaselineError(
            "unroutable_discovery_issue",
            "{} is not an issue this gate can route to; expected one of {}".format(
                discovery_issue, ", ".join(ROUTABLE_ISSUES)
            ),
        )

    workflows: List[LedgerEntry] = []
    for entry in document.get("workflows", []) or []:
        if not isinstance(entry, Mapping):
            raise BaselineError("ledger_workflow_not_a_mapping", "each workflow must be an object")
        path = str(entry.get("path", ""))
        blob_sha = str(entry.get("blob_sha", ""))
        classification = str(entry.get("classification", ""))
        owner = str(entry.get("owner", ""))
        if not path or not blob_sha:
            raise BaselineError(
                "ledger_workflow_incomplete",
                "{} must name both a path and a blob SHA; a file with no blob cannot "
                "be compared against anything".format(path or "<unnamed>"),
            )
        if classification not in CLASSIFICATIONS:
            raise BaselineError(
                "unknown_classification",
                "{!r} is not one of {}".format(classification, ", ".join(CLASSIFICATIONS)),
            )
        if owner and owner not in ROUTABLE_ISSUES:
            raise BaselineError(
                "unroutable_owner",
                "{} routes {} to {}, which is not one of {}".format(
                    repository, path, owner, ", ".join(ROUTABLE_ISSUES)
                ),
            )
        workflows.append(
            LedgerEntry(
                path=path,
                blob_sha=blob_sha,
                classification=classification,
                owner=owner,
                rationale=str(entry.get("rationale", "")),
            )
        )

    pulls: List[LedgerPullRequest] = []
    for entry in document.get("open_pull_requests", []) or []:
        if not isinstance(entry, Mapping):
            raise BaselineError(
                "ledger_pull_request_not_a_mapping", "each pull request must be an object"
            )
        classification = str(entry.get("classification", ""))
        owner = str(entry.get("owner", ""))
        if classification not in CLASSIFICATIONS:
            raise BaselineError(
                "unknown_classification",
                "{!r} is not one of {}".format(classification, ", ".join(CLASSIFICATIONS)),
            )
        if owner and owner not in ROUTABLE_ISSUES:
            raise BaselineError(
                "unroutable_owner",
                "{} routes pull request #{} to {}, which is not one of {}".format(
                    repository, entry.get("number"), owner, ", ".join(ROUTABLE_ISSUES)
                ),
            )
        head_sha = str(entry.get("head_sha", ""))
        if not head_sha:
            raise BaselineError(
                "ledger_pull_request_head_sha_missing",
                "pull request #{} is classified but records no head commit. The "
                "classification was granted against specific bytes; without them a "
                "later rewrite of the same paths would still read as classified. "
                "Re-audit the pull request and record its current head_sha.".format(
                    entry.get("number")
                ),
            )
        pulls.append(
            LedgerPullRequest(
                number=int(entry.get("number", 0) or 0),
                head_sha=head_sha,
                paths=tuple(str(path) for path in entry.get("paths", []) or []),
                classification=classification,
                owner=owner,
                rationale=str(entry.get("rationale", "")),
            )
        )

    return ParityLedger(
        repository=repository,
        audited_head=str(document.get("audited_head", "")),
        audited_at=str(document.get("audited_at", "")),
        discovery_issue=discovery_issue,
        workflows=tuple(sorted(workflows, key=lambda item: item.path)),
        open_pull_requests=tuple(sorted(pulls, key=lambda item: item.number)),
    )


def load_ledger(path: str) -> ParityLedger:
    """Read a ledger from a file."""

    with open(path, "r", encoding="utf-8") as handle:
        return read_ledger(json.load(handle))


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #

UNCHANGED = "unchanged"
CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"
INCOMPLETE = "incomplete"
CLASSIFIED = "classified"
UNCLASSIFIED = "unclassified"


@dataclasses.dataclass(frozen=True)
class Blocker:
    """One reason the reading cannot support a cutover."""

    code: str
    message: str
    subject: str = ""
    owner: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "owner": self.owner,
        }


@dataclasses.dataclass(frozen=True)
class Difference:
    """One audited thing that no longer matches the ledger."""

    kind: str
    subject: str
    detail: str
    expected_blob: str = ""
    observed_blob: str = ""
    classification: str = ""
    owner: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "expected_blob": self.expected_blob,
            "observed_blob": self.observed_blob,
            "classification": self.classification,
            "owner": self.owner,
        }


@dataclasses.dataclass(frozen=True)
class BaselineReport:
    """The reading, the ledger, the differences, and whether the gate is satisfied."""

    #: The reading covers every path the ledger claims to audit and matches it.
    ready: bool
    repository: str = ""
    ledger_digest: str = ""
    live_digest: str = ""
    #: One digest over the ledger *and* the reading, so an approval cannot be
    #: carried to a repository that has moved since it was granted.
    evidence_digest: str = ""
    audited_head: str = ""
    #: When the ledger was last audited, as an ISO date. Carried here rather than
    #: left in the ledger because the cutover compares it against the window: an
    #: approval over a window collected before the ledger was re-audited is a
    #: signature over evidence that never saw the state now being blessed.
    audited_at: str = ""
    live_head: str = ""
    complete: bool = True
    #: One verdict: `clean`, `drifted`, or `incomplete`.
    verdict: str = "clean"
    differences: Tuple[Difference, ...] = ()
    blockers: Tuple[Blocker, ...] = ()
    routing: Mapping[str, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    limits: Tuple[str, ...] = ()
    generated_from: str = ""

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(blocker.code for blocker in self.blockers)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": BASELINE_SCHEMA,
            "kind": "report",
            "ready": self.ready,
            "verdict": self.verdict,
            "complete": self.complete,
            "repository": self.repository,
            "audited_head": self.audited_head,
            "audited_at": self.audited_at,
            "live_head": self.live_head,
            "ledger_digest": self.ledger_digest,
            "live_digest": self.live_digest,
            "evidence_digest": self.evidence_digest,
            "generated_from": self.generated_from,
            "differences": [item.describe() for item in self.differences],
            "blockers": [blocker.describe() for blocker in self.blockers],
            "routing": {key: list(self.routing[key]) for key in sorted(self.routing)},
            "limits": list(self.limits),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"


DRIFTED = "drifted"
CLEAN = "clean"


def _digest(ledger: ParityLedger, live: LiveHead) -> str:
    payload = {"ledger": ledger.digest, "live": live.digest}
    return "bl-" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]


def _route(routing: Dict[str, List[str]], owner: str, subject: str) -> None:
    routing.setdefault(owner, []).append(subject)


def audit(
    ledger: ParityLedger,
    live: LiveHead,
    *,
    generated_from: str = "",
) -> BaselineReport:
    """Compare the reading with the approved audit, and route what differs.

    Three cases, in the order of trust:

    * the reading is **incomplete**, so nothing can be concluded from it;
    * the reading is **drifted**, so the audit no longer describes the repository;
    * the reading is **clean**, and the ledger is what was audited.

    A difference is never dropped for being inconvenient. A workflow the ledger
    does not mention, a pull request nobody classified, and a blob that moved all
    block, each naming the issue that has to resolve it.
    """

    routing: Dict[str, List[str]] = {}
    differences: List[Difference] = []
    blockers: List[Blocker] = []

    if ledger.repository != live.repository:
        blockers.append(
            Blocker(
                code="repository_mismatch",
                message=(
                    "the ledger audits {} but the reading was taken from {}".format(
                        ledger.repository or "<none>", live.repository or "<none>"
                    )
                ),
                subject=ledger.repository or live.repository,
                owner=ledger.discovery_issue,
            )
        )
        _route(routing, ledger.discovery_issue, ledger.repository or live.repository)

    if not live.complete:
        # The comparison below would compare a partial reading with a complete
        # ledger and name every file it did not happen to see as removed, which is
        # a claim about the gap rather than about the repository. So the difference
        # walk is skipped: the only honest finding is that the reading is partial,
        # and that finding is what blocks.
        blockers.append(
            Blocker(
                code=INCOMPLETE_BLOCKER,
                message=(
                    "the live reading could not be completed ({}), so a clean comparison "
                    "would be a claim about files nobody read".format("; ".join(live.limits) or "no detail")
                ),
                subject=live.repository,
                owner=ledger.discovery_issue,
            )
        )
        _route(routing, ledger.discovery_issue, "live reading: {}".format(live.repository))

    audited = ledger.workflow_map
    observed = live.workflow_map

    for path in sorted(set(audited) | set(observed)) if live.complete else ():
        entry = audited.get(path)
        seen = observed.get(path)
        if entry is not None and seen is not None and entry.blob_sha == seen:
            continue
        if entry is None:
            owner = ledger.discovery_issue
            differences.append(
                Difference(
                    kind=ADDED,
                    subject=path,
                    detail="an active workflow the ledger does not classify",
                    observed_blob=seen or "",
                    classification="",
                    owner=owner,
                )
            )
            blockers.append(
                Blocker(
                    code="unclassified_workflow",
                    message="{} is active and unclassified, so nothing about it was audited".format(path),
                    subject=path,
                    owner=owner,
                )
            )
            _route(routing, owner, path)
            continue
        if seen is None:
            owner = entry.owner or ledger.discovery_issue
            differences.append(
                Difference(
                    kind=REMOVED,
                    subject=path,
                    detail="the ledger audits a workflow that is no longer active",
                    expected_blob=entry.blob_sha,
                    classification=entry.classification,
                    owner=owner,
                )
            )
            blockers.append(
                Blocker(
                    code="workflow_removed",
                    message="{} was audited at {} but is no longer active".format(path, entry.blob_sha),
                    subject=path,
                    owner=owner,
                )
            )
            _route(routing, owner, path)
            continue
        owner = entry.owner or ledger.discovery_issue
        differences.append(
            Difference(
                kind=CHANGED,
                subject=path,
                detail="the blob changed since it was audited",
                expected_blob=entry.blob_sha,
                observed_blob=seen,
                classification=entry.classification,
                owner=owner,
            )
        )
        blockers.append(
            Blocker(
                code="workflow_blob_changed",
                message=(
                    "{} moved from {} to {}; the parity evidence granted against the "
                    "old blob is no longer evidence about this file".format(
                        path, entry.blob_sha, seen
                    )
                ),
                subject=path,
                owner=owner,
            )
        )
        _route(routing, owner, path)

    audited_pulls = ledger.pull_request_map
    observed_pulls = live.pull_request_map
    observed_heads = live.pull_request_heads
    for number in sorted(set(audited_pulls) | set(observed_pulls)) if live.complete else ():
        entry = audited_pulls.get(number)
        seen = observed_pulls.get(number)
        if entry is None:
            owner = ledger.discovery_issue
            differences.append(
                Difference(
                    kind=UNCLASSIFIED,
                    subject="#{}".format(number),
                    detail="an open pull request touching .github/** the ledger does not classify",
                    classification="",
                    owner=owner,
                )
            )
            blockers.append(
                Blocker(
                    code="unclassified_open_pull_request",
                    message=(
                        "#{} touches {} and is unclassified; it could restore an orchestration "
                        "writer the cutover removes".format(number, ", ".join(seen or ()))
                    ),
                    subject="#{}".format(number),
                    owner=owner,
                )
            )
            _route(routing, owner, "#{}".format(number))
            continue

        # Two separate questions, because they have different remedies. A changed
        # path set means the pull request now reaches a surface it did not before,
        # so its classification was granted against the wrong file list. A moved
        # head with an identical path set means the same files were rewritten, which
        # is the case paths alone cannot see: a rewrite can add the very writer
        # this gate removes. Neither is evidence the classification still holds.
        paths_moved = tuple(entry.paths) != tuple(seen or ())
        head_moved = entry.head_sha != observed_heads.get(number, "")
        if not paths_moved and not head_moved:
            continue

        owner = entry.owner or ledger.discovery_issue
        if paths_moved:
            code = "open_pull_request_drift"
            detail = "the .github/** paths changed since they were classified"
            message = "#{} now touches {}; it was classified as {!r}".format(
                number, ", ".join(seen or entry.paths), entry.classification
            )
        else:
            code = "open_pull_request_head_moved"
            detail = (
                "the same .github/** paths were rewritten at a new head commit"
            )
            message = (
                "#{} still touches {}, but its head moved from {} to {}; the classification "
                "was granted against the old commit and says nothing about these bytes".format(
                    number,
                    ", ".join(entry.paths),
                    entry.head_sha or "<none>",
                    observed_heads.get(number, "<none>"),
                )
            )
        differences.append(
            Difference(
                kind=CHANGED,
                subject="#{}".format(number),
                detail=detail,
                classification=entry.classification,
                owner=owner,
            )
        )
        blockers.append(
            Blocker(
                code=code,
                message=message,
                subject="#{}".format(number),
                owner=owner,
            )
        )
        _route(routing, owner, "#{}".format(number))

    verdict = INCOMPLETE if not live.complete else (CLEAN if not blockers else DRIFTED)
    return BaselineReport(
        ready=not blockers,
        repository=live.repository or ledger.repository,
        ledger_digest=ledger.digest,
        live_digest=live.digest,
        evidence_digest=_digest(ledger, live),
        audited_head=ledger.audited_head,
        audited_at=ledger.audited_at,
        live_head=live.head_sha,
        complete=live.complete,
        verdict=verdict,
        differences=tuple(differences),
        blockers=tuple(blockers),
        routing={key: tuple(sorted(set(value))) for key, value in routing.items()},
        limits=live.limits,
        generated_from=generated_from,
    )


def read_report(document: Mapping[str, Any]) -> BaselineReport:
    """Read a baseline report back, for the cutover gate to trust or refuse."""

    if not isinstance(document, Mapping):
        raise BaselineError("report_not_a_mapping", "a baseline report must be an object")
    schema = document.get("schema")
    if schema != BASELINE_SCHEMA:
        raise BaselineError(
            "unknown_report_schema",
            "expected {!r}, found {!r}".format(BASELINE_SCHEMA, schema),
        )
    verdict = str(document.get("verdict", ""))
    if verdict not in (CLEAN, DRIFTED, INCOMPLETE):
        raise BaselineError(
            "unknown_verdict", "{!r} is not one of {}, {}".format(verdict, CLEAN, DRIFTED, INCOMPLETE)
        )
    report = BaselineReport(
        ready=bool(document.get("ready", False)),
        repository=str(document.get("repository", "")),
        ledger_digest=str(document.get("ledger_digest", "")),
        live_digest=str(document.get("live_digest", "")),
        evidence_digest=str(document.get("evidence_digest", "")),
        audited_head=str(document.get("audited_head", "")),
        audited_at=str(document.get("audited_at", "")),
        live_head=str(document.get("live_head", "")),
        complete=bool(document.get("complete", True)),
        verdict=verdict,
        differences=tuple(
            Difference(
                kind=str(entry.get("kind", "")),
                subject=str(entry.get("subject", "")),
                detail=str(entry.get("detail", "")),
                expected_blob=str(entry.get("expected_blob", "")),
                observed_blob=str(entry.get("observed_blob", "")),
                classification=str(entry.get("classification", "")),
                owner=str(entry.get("owner", "")),
            )
            for entry in document.get("differences", []) or []
            if isinstance(entry, Mapping)
        ),
        blockers=tuple(
            Blocker(
                code=str(entry.get("code", "")),
                message=str(entry.get("message", "")),
                subject=str(entry.get("subject", "")),
                owner=str(entry.get("owner", "")),
            )
            for entry in document.get("blockers", []) or []
            if isinstance(entry, Mapping)
        ),
        routing={
            str(key): tuple(str(item) for item in value)
            for key, value in (document.get("routing", {}) or {}).items()
            if isinstance(value, (list, tuple))
        },
        limits=tuple(str(item) for item in document.get("limits", []) or []),
        generated_from=str(document.get("generated_from", "")),
    )
    _check_report_is_coherent(report)
    return report


def _check_report_is_coherent(report: BaselineReport) -> None:
    """Refuse a report that disagrees with itself.

    The cutover gate forwards a report's blockers and folds its evidence digest into
    the approval digest, so every field a reader acts on is a field somebody can edit.
    ``ready`` and ``verdict`` are the two the gate would trust, and they are derivable
    from the rest: ``ready`` is ``not blockers``, and the verdict follows from the
    blockers and the completeness. A report where they disagree is not a report with a
    bad field, it is a report whose remaining fields cannot be assumed to mean what they
    say either -- a truncated write leaves exactly this shape. So it is refused rather
    than reconciled, and the reader gets the inconsistency instead of a verdict.
    """

    blockers = report.blockers
    if report.ready and blockers:
        raise BaselineError(
            "inconsistent_report",
            "the report is marked ready but names {} blocker(s); its own verdict is "
            "not evidence".format(len(blockers)),
        )
    if not report.ready and not blockers:
        raise BaselineError(
            "inconsistent_report",
            "the report is not ready but names no blocker, so it cannot be resolved",
        )
    if report.verdict == CLEAN and blockers:
        raise BaselineError(
            "inconsistent_report",
            "the report is clean but names {} blocker(s)".format(len(blockers)),
        )
    if report.verdict == DRIFTED and not blockers:
        raise BaselineError(
            "inconsistent_report", "the report is drifted but names no blocker"
        )
    if report.verdict == INCOMPLETE and any(
        blocker.code != INCOMPLETE_BLOCKER for blocker in blockers
    ):
        raise BaselineError(
            "inconsistent_report",
            "the report is incomplete and also reports findings ({}); a partial reading "
            "has no comparison to disagree with, so one of the two is wrong".format(
                ", ".join(sorted({blocker.code for blocker in blockers}))
            ),
        )
    if report.verdict != INCOMPLETE and any(
        blocker.code == INCOMPLETE_BLOCKER for blocker in blockers
    ):
        raise BaselineError(
            "inconsistent_report",
            "the reading was incomplete but the verdict is {!r}".format(report.verdict),
        )
    if report.limits and report.complete:
        raise BaselineError(
            "inconsistent_report",
            "the report is marked complete but names {} unread read(s)".format(len(report.limits)),
        )
    if not report.complete and report.verdict != INCOMPLETE:
        raise BaselineError(
            "inconsistent_report",
            "the report is not complete but its verdict is {!r}".format(report.verdict),
        )
    if not report.evidence_digest:
        # The approval is granted over this digest. An empty one is not a weaker
        # match, it is no match at all, so it must never reach the gate as evidence.
        raise BaselineError(
            "inconsistent_report",
            "the report carries no evidence digest, so no approval can be bound to it",
        )


def summarize(report: BaselineReport) -> str:
    """One line, for a job summary."""

    if report.verdict == INCOMPLETE:
        return "baseline: incomplete -- {}".format("; ".join(report.limits) or "no detail")
    if report.ready:
        return "baseline: clean at {} (ledger {})".format(
            report.live_head or "an unnamed head", report.ledger_digest
        )
    return "baseline: not clean -- {}".format(
        "; ".join(blocker.message for blocker in report.blockers) or "unknown"
    )


def render_markdown(ledger: ParityLedger) -> str:
    """The ledger as a reviewer reads it: one row per audited file.

    Generated from the same object the gate reads, so the document and the
    comparison cannot disagree about what was audited.

    The repository name comes from the ledger rather than being written here.
    This module is generic core: a name typed into it would be one more consumer
    baked into the code the contract says has to resolve every consumer.
    """

    lines = [
        # Names the generator so a hand edit shows up in the diff as a conflict with
        # this line. The path is not named because the ledger can live anywhere;
        # `--write-ledger` writes the table beside whatever JSON it was given.
        "<!-- Generated by continuum.shadow.baseline.render_markdown. Do not edit;"
        " refresh with `continuum.shadow.cli baseline --write-ledger`. -->",
        "",
        "## `{}` workflow ledger".format(ledger.repository or "consumer"),
        "",
        "Audited head: `{}`  ".format(ledger.audited_head or "unknown"),
        "Audited at: `{}`  ".format(ledger.audited_at or "unknown"),
        "Discovery issue for unclassified differences: `{}`".format(ledger.discovery_issue),
        "",
        "| Workflow | Blob | Classification | Owner | Rationale |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in ledger.workflows:
        lines.append(
            "| `{}` | `{}` | `{}` | {} | {} |".format(
                entry.path,
                entry.blob_sha,
                entry.classification,
                entry.owner or "-",
                entry.rationale.replace("|", "\\|") if entry.rationale else "-",
            )
        )
    lines.extend(["", "### Open pull requests touching `.github/**`", ""])
    if ledger.open_pull_requests:
        lines.extend(
            [
                "| Pull request | Head commit | Paths | Classification | Owner | Rationale |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in ledger.open_pull_requests:
            lines.append(
                "| `#{}` | `{}` | {} | `{}` | {} | {} |".format(
                    entry.number,
                    entry.head_sha or "-",
                    ", ".join("`{}`".format(path) for path in entry.paths) or "-",
                    entry.classification,
                    entry.owner or "-",
                    entry.rationale.replace("|", "\\|") if entry.rationale else "-",
                )
            )
    else:
        lines.append("None open.")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ADDED",
    "BASELINE_SCHEMA",
    "BaselineError",
    "BaselineReport",
    "Blocker",
    "CHANGED",
    "CLASSIFICATIONS",
    "Difference",
    "INCOMPLETE",
    "LEDGER_SCHEMA",
    "LedgerEntry",
    "LedgerPullRequest",
    "LiveHead",
    "OpenPullRequest",
    "ParityLedger",
    "REMOVED",
    "ROUTABLE_ISSUES",
    "audit",
    "capture_live_head",
    "load_ledger",
    "read_ledger",
    "read_live_head",
    "read_report",
    "render_markdown",
    "summarize",
]