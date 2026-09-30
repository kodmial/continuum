"""Reading a tracked source, and refusing to read one that is not there.

This is the only part of the parity plane that touches the network, and it is
built around one asymmetry: **a source that cannot be read is a result, not an
error.** Continuum is public, its ambient token reads public repositories and
nothing else, and the source repositories it audits are not all public. So a
private source with no configured credential produces an ``unavailable``
observation, the scan carries on with the sources it *can* read, and nothing
anywhere claims the unread one was checked.

That asymmetry only stays safe if two other things hold, and both are enforced
here rather than documented:

* **No source content is ever carried.** A compare response carries a patch per
  changed file. This module parses filenames and commit metadata and drops
  everything else at the parse boundary, so a patch cannot reach a report, a log
  line, or an issue body by being remembered one layer further out.
* **Private metadata is not published by default.** A private repository's name
  and path list are themselves private. ``public_view`` replaces them with
  redaction markers, leaving the alias, the verdict, and the commit range --
  which is what a reader needs to know that *something* moved.

The credential story is deliberately narrow. There is no token argument that
takes whatever is in the environment, and no fallback to a broader credential
than the one a source declares it needs: a public source is read with the
ambient read-only token, a private source is read only with the
least-privilege credential the caller supplies for it, and the report records
which of the two was used so a reviewer can tell "nothing moved" from "we were
not allowed to look".
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import ledger as ledger_module
from .ledger import Source

DEFAULT_API_BASE = "https://api.github.com"
USER_AGENT = "continuum-parity-drift"

OBSERVATIONS_SCHEMA = "continuum.parity-observations/v1"

#: The observation reached the source and knows its current commit.
READABLE = "readable"
#: The observation could not be made. The reason is recorded, never guessed at,
#: and a source in this state contributes no verdict about drift.
UNAVAILABLE = "unavailable"

#: Every key a parity artifact may carry.
#:
#: The list is the control. A parity document is published in a public
#: repository's job summary, artifact, and issue body, so "what may appear in
#: it" has to be a closed set rather than a convention. :func:`assert_public`
#: walks a finished document against it and refuses anything else, which means a
#: field added later is a test failure rather than a disclosure.
PUBLIC_FIELD_ALLOWLIST: frozenset = frozenset(
    {
        # document envelope
        "schema",
        "kind",
        "version",
        "generated_at",
        "continuum_sha",
        "ledger_source",
        "ledger_version",
        "ledger_generated_at",
        "overlay_ids",
        "redacted",
        # sources
        "id",
        "repository",
        "visibility",
        "access",
        "state",
        "code",
        "reason",
        "sha",
        "previous_sha",
        "advanced",
        "head_sha",
        "baseline_sha",
        "current_sha",
        "audited_at",
        "audited_by",
        "ledger_version_of_source",
        "paths",
        "path",
        "changes",
        "commits",
        "pull_requests",
        "count",
        "needs_triage",
        "disposition",
        "note",
        "pattern",
        "changed",
        "unchanged",
        "removed",
        "truncated",
        "priority",
        # drift
        "sources",
        "findings",
        "verdict",
        "summary",
        "clean",
        "can_claim_clean",
        "unclassified_p0",
        "unclassified_p1",
        "unavailable_sources",
        "in_sync_sources",
        "classified_sources",
        "detail",
        "evidence",
        "url",
        "sha_short",
        "message",
        "capability",
        "prior_sha",
        "observed_sha",
        "classification",
        "p0",
        "approved",
        "claim",
        "consumer",
        "from_pin",
        "to_pin",
        "blockers",
        "advisories",
        "drift_generated_at",
        "issue",
        "number",
        "title",
        "body",
        "action",
        "marker",
        "status",
        "create",
        "update",
        "reopen",
        "close",
        "none",
        "skip",
        "duplicate_numbers",
        # applying a plan
        "applied-plan",
        "commands",
        "argv",
        "dry_run",
        "repository",
        "by_disposition",
        "by_source",
        "totals",
        "existing_issues",
        "plans",
        "unanswered_incidents",
    }
)

#: Keys whose *values* are data rather than structure.
#:
#: A count keyed by disposition, or a verdict keyed by source id, has the source
#: names as its keys -- names the ledger chose and the allowlist cannot know. So
#: for these two keys the nested keys are not checked against the allowlist; the
#: values still are, and the credential and hunk scans still reach them. Every
#: other mapping in a parity artifact is closed, which is the property that
#: matters: an artifact's *shape* is reviewable, and only its contents are data.
KEY_VALUES_ARE_DATA: frozenset = frozenset({"by_disposition", "by_source"})

#: Keys that must never appear, whatever the allowlist says. Listed separately
#: so the reason they are refused is visible next to the reason the allowlist
#: exists: these are the shapes a credential or a patch arrives in. ``body`` is
#: *not* here -- a parity issue body is Continuum's own text and is published on
#: purpose -- but the credential and hunk scans below read it anyway, which is
#: what keeps a body rendered from upstream data from carrying either.
FORBIDDEN_FIELDS: frozenset = frozenset(
    {
        "patch",
        "diff",
        "raw",
        "content",
        "contents",
        "token",
        "authorization",
        "password",
        "secret",
        "credential",
        "email",
        "actor",
    }
)

#: A value that looks like a credential, checked over the whole serialised
#: document rather than over known fields, because a token that leaked through an
#: unexpected key would not be in any list of fields.
_SECRET_LIKE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|"
    r"(?i:authorization)\s*[:=]|"
    r"(?i:bearer)\s+[A-Za-z0-9._-]{12,})"
)

#: A diff hunk. Reaching one of these in an artifact means a patch survived
#: parsing, which is the specific accident the parse boundary exists to prevent.
_HUNK = re.compile(r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", re.M)

#: GitHub writes the pull request it merged into the first line of a squash
#: merge's subject, as `(#123)`. It is the only place a commit record names a
#: pull request, so it is where the issue body gets its PR evidence -- and it is
#: evidence, not proof: a commit that was not squash-merged names no pull
#: request and the finding simply carries fewer references.
_PR_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")


class SourceAccessError(RuntimeError):
    """A source could not be read. Never escapes :meth:`GitHubSourceReader.read`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.detail = message


class RedactionError(RuntimeError):
    """A document carried something the public allowlist does not permit."""


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Commit:
    """One upstream commit, as a reference. Not as a change."""

    sha: str
    url: str = ""
    message: str = ""
    pull_requests: Tuple[int, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "url": self.url,
            "message": self.message,
            "pull_requests": list(self.pull_requests),
        }


@dataclasses.dataclass(frozen=True)
class Change:
    """One changed path, and what happened to it.

    ``status`` is the compare API's own vocabulary -- ``added``, ``removed``,
    ``modified``, ``renamed`` -- kept because "renamed out of the tracked set"
    and "deleted from it" are different facts about whether a path is still
    audited.
    """

    path: str
    status: str = "modified"

    def describe(self) -> Dict[str, Any]:
        return {"path": self.path, "status": self.status}


@dataclasses.dataclass(frozen=True)
class SourceObservation:
    """What one read attempt learned about one tracked source."""

    id: str
    repository: str
    visibility: str
    state: str
    access: str
    head_sha: str = ""
    previous_sha: str = ""
    changes: Tuple[Change, ...] = ()
    commits: Tuple[Commit, ...] = ()
    code: str = ""
    reason: str = ""
    #: True when the compare response listed more files than this observation
    #: kept, so "no drift here" is never concluded from a truncated list.
    truncated: bool = False

    @property
    def available(self) -> bool:
        return self.state == READABLE

    @property
    def advanced(self) -> bool:
        return self.available and bool(self.previous_sha) and self.head_sha != self.previous_sha

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @property
    def pull_requests(self) -> Tuple[int, ...]:
        numbers: List[int] = []
        for commit in self.commits:
            for number in commit.pull_requests:
                if number not in numbers:
                    numbers.append(number)
        return tuple(sorted(numbers))

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "visibility": self.visibility,
            "state": self.state,
            "access": self.access,
            "head_sha": self.head_sha,
            "previous_sha": self.previous_sha,
            "advanced": self.advanced,
            "changes": [change.describe() for change in self.changes],
            "commits": [commit.describe() for commit in self.commits],
            "pull_requests": list(self.pull_requests),
            "code": self.code,
            "reason": self.reason,
            "truncated": self.truncated,
        }


def observations_document(
    observations: Sequence[SourceObservation],
    *,
    generated_at: str = "",
    continuum_sha: str = "",
    ledger: Optional[ledger_module.Ledger] = None,
) -> Dict[str, Any]:
    """The read phase's artifact: every attempt, including the ones that failed.

    Rendered through :func:`public_view`, so an artifact uploaded from a public
    repository's run never names a private source even when the run held a
    credential that could read it. The cost of that is stated rather than hidden:
    a redacted private source carries no path list, so a scan replayed from a
    published artifact cannot classify its advance and reports it as unclassified
    instead. Failing towards "a human has to look" is the direction to be wrong
    in.
    """

    return assert_public(
        {
            "schema": OBSERVATIONS_SCHEMA,
            "kind": "observations",
            "version": 1,
            "generated_at": generated_at,
            "continuum_sha": continuum_sha,
            "ledger_version": ledger.version if ledger is not None else 0,
            "ledger_generated_at": ledger.generated_at if ledger is not None else "",
            "ledger_source": ledger.source_path if ledger is not None else "",
            "overlay_ids": list(ledger.overlay_ids) if ledger is not None else [],
            "sources": [public_view(observation) for observation in observations],
        }
    )


def observations_from_document(document: Any) -> Tuple[SourceObservation, ...]:
    """Read observations back, for a scan that runs without network access.

    Strict about the schema and about the state vocabulary, permissive about
    everything else: a recorded observation is evidence, and evidence that is
    rejected for carrying an unexpected field is evidence nobody can replay.
    """

    if not isinstance(document, Mapping):
        raise ledger_module.LedgerError(
            "not_a_mapping", "an observations document must be an object"
        )
    if document.get("schema") != OBSERVATIONS_SCHEMA:
        raise ledger_module.LedgerError(
            "unknown_schema",
            "expected {!r}, found {!r}".format(OBSERVATIONS_SCHEMA, document.get("schema")),
        )
    raw = document.get("sources")
    if not isinstance(raw, (list, tuple)):
        raise ledger_module.LedgerError("malformed_observations", "sources must be a list")
    result: List[SourceObservation] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ledger_module.LedgerError(
                "malformed_observations", "observation {} is not an object".format(index)
            )
        state = str(entry.get("state", ""))
        if state not in (READABLE, UNAVAILABLE):
            raise ledger_module.LedgerError(
                "unknown_observation_state",
                "observation {} has state {!r}".format(index, state),
            )
        result.append(
            SourceObservation(
                id=str(entry.get("id", "")),
                repository=str(entry.get("repository", "")),
                visibility=str(entry.get("visibility", ledger_module.PUBLIC)),
                state=state,
                access=str(entry.get("access", ledger_module.ACCESS_NONE)),
                head_sha=str(entry.get("head_sha", "")),
                previous_sha=str(entry.get("previous_sha", "")),
                changes=tuple(
                    Change(
                        path=ledger_module.normalize_path(str(change.get("path", ""))),
                        status=str(change.get("status", "modified")),
                    )
                    for change in entry.get("changes") or ()
                    if isinstance(change, Mapping)
                ),
                commits=tuple(
                    Commit(
                        sha=str(commit.get("sha", "")),
                        url=str(commit.get("url", "")),
                        message=str(commit.get("message", "")),
                        pull_requests=tuple(
                            int(number) for number in commit.get("pull_requests") or ()
                        ),
                    )
                    for commit in entry.get("commits") or ()
                    if isinstance(commit, Mapping)
                ),
                code=str(entry.get("code", "")),
                reason=str(entry.get("reason", "")),
                truncated=bool(entry.get("truncated", False)),
            )
        )
    return tuple(result)


# --------------------------------------------------------------------------- #
# The public allowlist
# --------------------------------------------------------------------------- #


def assert_public(document: Any, *, where: str = "document") -> Any:
    """Refuse a document that carries anything the public allowlist excludes.

    Called on the way *out* of every artifact builder rather than on the way in,
    because the leak that matters is the one in the published copy. Three checks,
    in increasing order of what they catch:

    1. no key outside :data:`PUBLIC_FIELD_ALLOWLIST`, so a field cannot appear
       by being added to a record rather than by being thought about;
    2. no key in :data:`FORBIDDEN_FIELDS` at any depth, which is redundant with
       the first and kept because it survives an allowlist edit;
    3. no value anywhere that looks like a credential or a diff hunk, which is
       the check that catches a leak through a *permitted* key -- a commit
       message that quotes a token, for instance.
    """

    def walk(node: Any, path: str, closed: bool = True) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                if closed:
                    if name in FORBIDDEN_FIELDS:
                        raise RedactionError(
                            "{}: field {!r} at {} may never appear in a parity artifact".format(
                                where, name, path
                            )
                        )
                    if name not in PUBLIC_FIELD_ALLOWLIST:
                        raise RedactionError(
                            "{}: field {!r} at {} is outside the public allowlist".format(
                                where, name, path
                            )
                        )
                walk(
                    value,
                    "{}.{}".format(path, name),
                    closed and name not in KEY_VALUES_ARE_DATA,
                )
            return
        if isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, "{}[{}]".format(path, index), closed=True)
            return
        if isinstance(node, str):
            if _SECRET_LIKE.search(node):
                raise RedactionError(
                    "{}: a value at {} looks like a credential".format(where, path)
                )
            if _HUNK.search(node):
                raise RedactionError(
                    "{}: a value at {} contains a diff hunk".format(where, path)
                )

    walk(document, where)
    return document


def public_view(observation: SourceObservation) -> Dict[str, Any]:
    """An observation as it may be published.

    A private source keeps its alias, its state, its access mode, and its commit
    range, and loses its repository name, its paths, and its commit metadata --
    those are the private facts. ``redacted`` names what was withheld, so the
    artifact says the source was checked and partially reported rather than
    looking like a source with nothing to say.
    """

    described = observation.describe()
    if observation.visibility != ledger_module.PRIVATE:
        return assert_public(described, where="observation {}".format(observation.id))
    withheld = ["repository", "changes", "commits", "pull_requests"]
    redacted = dict(described)
    redacted["repository"] = "<redacted:private>"
    redacted["changes"] = []
    redacted["commits"] = []
    redacted["pull_requests"] = []
    redacted["reason"] = _redact_text(observation.reason or observation.code)
    redacted["code"] = observation.code
    redacted["redacted"] = withheld
    redacted["count"] = len(observation.changes)
    return assert_public(redacted, where="observation {}".format(observation.id))


def _redact_text(value: str) -> str:
    """Keep a reason, drop anything in it that names a private thing.

    A reason is a short, code-generated sentence, so this is a backstop for the
    case where a message is built from a repository name rather than from a
    code: the code survives, the prose does not.
    """

    if not value:
        return ""
    return "private source was not read in full; see the ledger for the alias"


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #


def pull_requests_in(subject: str) -> Tuple[int, ...]:
    """Pull request numbers a squash-merge subject names."""

    match = _PR_SUBJECT.search(str(subject or "").strip())
    return (int(match.group(1)),) if match else ()


def _first_line(message: str) -> str:
    return str(message or "").strip().splitlines()[0].strip() if str(message or "").strip() else ""


def _head_entry(payload: Any) -> Optional[Mapping[str, Any]]:
    """The newest commit record out of whatever shape the endpoint used.

    ``GET /repos/{owner}/{repo}/commits`` answers with a JSON array; an
    object-shaped payload is accepted as well so an opener that returns a single
    record, or a search-shaped object carrying ``items``, resolves to the same
    commit. Returns ``None`` rather than guessing, which the caller reports as an
    unreadable source -- not as a source whose head happens to be the audited one.
    """

    if isinstance(payload, Mapping):
        items = payload.get("items")
        if isinstance(items, (list, tuple)):
            payload = items
        else:
            payload = [payload]
    if isinstance(payload, (list, tuple)):
        for entry in payload:
            if isinstance(entry, Mapping) and entry.get("sha"):
                return entry
    return None


class GitHubSourceReader:
    """Read the current state of a tracked source.

    One instance serves every source, because the credential differs per source
    and the client does not: ``read`` is handed the token to use, so a private
    source can never be read with the public one by accident, and a public source
    is never read with a private credential by accident.
    """

    def __init__(
        self,
        *,
        api_base: str = DEFAULT_API_BASE,
        public_token: str = "",
        private_token: str = "",
        opener: Optional[Callable[[urllib.request.Request, int], Any]] = None,
        timeout: int = 60,
        max_files: int = 300,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.public_token = public_token or ""
        self.private_token = private_token or ""
        self.timeout = timeout
        self.max_files = max_files
        self._opener = opener or _default_opener

    # -- transport ---------------------------------------------------------
    def _get(self, path: str, token: str) -> Any:
        """One GET, as parsed JSON, or a refusal with a reason.

        Accepts an object *or* a list at the top level, because two of the three
        endpoints this module uses return one and one returns the other: the
        commit list is a JSON array and the compare is an object. Refusing the
        array shape here would make every live read report ``malformed_response``
        for a reason that has nothing to do with the repository.
        """
        if not token:
            raise SourceAccessError(
                ledger_module.ACCESS_NONE, "no credential is configured for this read"
            )
        request = urllib.request.Request(
            self.api_base + path,
            headers={
                "Authorization": "token {}".format(token),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            result = self._opener(request, self.timeout)
        except urllib.error.HTTPError as error:
            code = {404: "not-found", 401: "unauthorized", 403: "forbidden"}.get(
                error.code, "http-{}".format(error.code)
            )
            # The response body is not read. GitHub error bodies quote the URL
            # that was requested, and for a private source that URL is the secret.
            raise SourceAccessError(code, "the API refused the read (status {})".format(error.code)) from None
        except urllib.error.URLError:
            raise SourceAccessError("unreachable", "the API is unreachable") from None
        if isinstance(result, tuple):
            result = result[0]
        if isinstance(result, (bytes, bytearray)):
            result = result.decode("utf-8", "replace")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                raise SourceAccessError("malformed_response", "the API returned non-JSON") from None
        if not isinstance(result, (Mapping, list, tuple)):
            raise SourceAccessError("malformed_response", "the API returned an unexpected shape")
        return result

    # -- the read ----------------------------------------------------------
    def read(self, source: Source) -> SourceObservation:
        """Read one source, or explain why it was not read.

        The order is the order of the security requirement. A private source is
        asked whether a credential exists *before* any request is built, so the
        absence of a private credential produces no network traffic at all rather
        than a 404 that has to be interpreted.
        """

        private = source.private
        token = self.private_token if private else self.public_token
        if private and not token:
            return _unavailable(
                source,
                ledger_module.ACCESS_NONE,
                "private-credential-absent",
                "no least-privilege credential is configured for this private source",
            )
        if not private and not token:
            return _unavailable(
                source,
                ledger_module.ACCESS_NONE,
                "read-credential-absent",
                "no read-only credential is configured for public sources",
            )
        access = ledger_module.ACCESS_CREDENTIAL if private else ledger_module.ACCESS_PUBLIC

        owner, name = source.repository.split("/", 1)
        quoted = "/repos/{}/{}".format(
            urllib.parse.quote(owner), urllib.parse.quote(name)
        )
        try:
            latest = _head_entry(self._get(quoted + "/commits?per_page=1", token))
            if not isinstance(latest, Mapping) or not latest.get("sha"):
                raise SourceAccessError("malformed_response", "the head commit is missing")
            head_sha = str(latest["sha"])

            if head_sha == source.audit.current_sha:
                return SourceObservation(
                    id=source.id,
                    repository=source.repository,
                    visibility=source.visibility,
                    state=READABLE,
                    access=access,
                    head_sha=head_sha,
                    previous_sha=source.audit.current_sha,
                )

            comparison = self._get(
                "{}/compare/{}...{}".format(
                    quoted,
                    urllib.parse.quote(source.audit.current_sha),
                    urllib.parse.quote(head_sha),
                ),
                token,
            )
        except SourceAccessError as error:
            return _unavailable(source, access, error.code, error.detail)

        listed = comparison.get("files") if isinstance(comparison, Mapping) else None
        return SourceObservation(
            id=source.id,
            repository=source.repository,
            visibility=source.visibility,
            state=READABLE,
            access=access,
            head_sha=head_sha,
            previous_sha=source.audit.current_sha,
            changes=_changes(comparison, self.max_files),
            commits=_commits(comparison),
            truncated=isinstance(listed, (list, tuple)) and len(listed) > self.max_files,
        )

    def read_all(self, ledger: ledger_module.Ledger) -> Tuple[SourceObservation, ...]:
        """Read every source, in ledger order, without stopping on a failure.

        Sequential and never short-circuiting, because the whole point of the
        unavailable state is that one unreadable source does not cost the audit
        of the others.
        """

        return tuple(self.read(source) for source in ledger.sources)


def _unavailable(source: Source, access: str, code: str, reason: str) -> SourceObservation:
    return SourceObservation(
        id=source.id,
        repository=source.repository,
        visibility=source.visibility,
        state=UNAVAILABLE,
        access=access,
        code=code,
        reason=reason,
    )


def _changes(comparison: Mapping[str, Any], max_files: int) -> Tuple[Change, ...]:
    """The changed paths, and nothing else about them.

    ``files`` entries carry a ``patch``. It is read here only to be discarded:
    selecting the two fields this module needs, rather than copying the entry and
    deleting the patch afterwards, means a patch cannot survive a future edit that
    forgets the deletion.
    """

    changes: List[Change] = []
    for entry in list(comparison.get("files") or ())[:max_files]:
        if not isinstance(entry, Mapping):
            continue
        raw = str(entry.get("filename", ""))
        if not raw:
            continue
        try:
            path = ledger_module.normalize_path(raw)
        except ledger_module.LedgerError:
            # A path the ledger cannot even name is a path the ledger cannot
            # classify, so it is reported under a stable placeholder rather than
            # dropped: dropping it would make an unreadable path look unchanged.
            path = raw.replace("\\", "/")
        changes.append(Change(path=path, status=str(entry.get("status", "modified"))))
    return tuple(changes)


def _commits(comparison: Mapping[str, Any]) -> Tuple[Commit, ...]:
    commits: List[Commit] = []
    for entry in comparison.get("commits") or ():
        if not isinstance(entry, Mapping):
            continue
        sha = str(entry.get("sha", ""))
        if not sha:
            continue
        message = _first_line(str((entry.get("commit") or {}).get("message", "")))
        commits.append(
            Commit(
                sha=sha,
                url=str(entry.get("html_url", "")),
                message=message,
                pull_requests=pull_requests_in(message),
            )
        )
    return tuple(commits)


def _default_opener(request: urllib.request.Request, timeout: int) -> Any:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


__all__ = [
    "Change",
    "Commit",
    "DEFAULT_API_BASE",
    "FORBIDDEN_FIELDS",
    "GitHubSourceReader",
    "KEY_VALUES_ARE_DATA",
    "OBSERVATIONS_SCHEMA",
    "PUBLIC_FIELD_ALLOWLIST",
    "READABLE",
    "RedactionError",
    "SourceAccessError",
    "SourceObservation",
    "UNAVAILABLE",
    "assert_public",
    "observations_document",
    "observations_from_document",
    "public_view",
    "pull_requests_in",
]
