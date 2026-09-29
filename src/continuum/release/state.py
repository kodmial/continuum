"""The release state machine: its stages, its keys, and its record of itself.

The chain is fixed, and the order is the argument:

    eligibility -> version -> release-pr -> validation -> source
                -> build -> sign -> verify -> draft -> publish -> sync

Everything a release can be asked to do is one of those stages, in that order.
A new destination is a new publisher inside an existing stage, not a new stage,
because a stage that is not on the chain has no place to be enforced from — and
the enforcement is the whole point of having a chain rather than a script.

**Two stages, one requirement.** The issue this implements asks for a
`sign/verify` step; the contract asks for signing status and verification status
to be reported separately, because they are different claims and a signature
that verifies is not the same as a signature from the identity a release pins.
So they are two stages driven by one adapter. Splitting them costs one line in
the chain and buys the ability to fail between them, which is the moment where a
wrong signing identity is still a failed job rather than a published release.

**Idempotency is a key, not a flag.** Every transition computes a key from what
it is acting on, and a key that is already recorded as complete is *not run
again*. That is the whole mechanism behind "a duplicate event cannot publish a
duplicate version", and it is deliberately not a check for "is this release
already published" at the last moment: by then the build has run, the assets
have been uploaded, and the damage is done. The key is computed at the start.

The key is scoped to the release, not to the run. A re-dispatch, a re-run of a
failed job, and a second workflow watching the same tag are three runs and one
release, and they must be unable to become three releases. Which is why the key
is derived from the repository and the *version* — never from a delivery id, a
run id, or a timestamp.

**Fail closed.** A stage that fails with a non-retryable result stops the
release. Retryable means "running this again could succeed and cannot double
anything", and it is the publisher's call, not the core's: an upload that failed
half way can be resumed, and a published tag pointing at the wrong commit cannot.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# The namespace every Continuum key carries. A key is written to a workflow's
# concurrency group and to a journal, so it has to be recognisable as ours and
# impossible to confuse with a consumer's own.
KEY_NAMESPACE = "continuum"

#: The permission scopes a release may hold. `contents: write` publishes a
#: release; `packages: write` publishes to a registry; `id-token: write` and
#: `attestations: write` are what signing provenance requires. Nothing here can
#: approve, request, or evaluate a pull request.
RELEASE_SCOPES: Tuple[str, ...] = (
    "contents: read",
    "contents: write",
    "packages: write",
    "id-token: write",
    "attestations: write",
)

#: The scopes that belong to pull-request automation. They are listed here, and
#: asserted disjoint from `RELEASE_SCOPES`, because the failure this prevents is
#: a release job quietly holding the token that can label, approve, and merge
#: pull requests: a release that can merge is a release that can publish
#: something a human never reviewed.
PR_AUTOMATION_SCOPES: Tuple[str, ...] = (
    "pull-requests: write",
    "issues: write",
    "checks: write",
    "actions: write",
    "deployments: write",
)

# Transition outcomes. `no-op` is a green outcome that means "there was nothing
# to do and that is correct" — a push that changed no version, a repository
# whose release is switched off. It is separate from `skipped` because it is a
# decision the policy made rather than a duplicate the core recognised.
COMPLETED = "completed"
SKIPPED = "skipped"
NOOP = "noop"
FAILED = "failed"
BLOCKED = "blocked"
SUPPORTED_OUTCOMES: Tuple[str, ...] = (COMPLETED, SKIPPED, NOOP, FAILED, BLOCKED)

GREEN_OUTCOMES: Tuple[str, ...] = (COMPLETED, SKIPPED, NOOP)


class ReleaseStateError(ValueError):
    """Raised when the state machine is asked to do something it cannot."""

    def __init__(self, message: str, *, code: str = "release-state") -> None:
        super().__init__(message)
        self.code = code


class UnknownStage(ReleaseStateError):
    """Raised for a stage name that is not on the chain."""


class IllegalTransition(ReleaseStateError):
    """Raised when a transition is not on the chain."""


@dataclass(frozen=True)
class Stage:
    """One step of the chain, and what it is accountable for.

    `optional` marks a stage a release may legitimately have nothing to do in,
    and `terminal` says what a no-op there means, because the two are not the
    same thing everywhere.

    A no-op at an *optional, non-terminal* stage is passed through: the release
    pull request, because a repository that cuts releases from a tag has no
    release pull request and requiring one would mean that repository could never
    release. Nothing downstream depends on it having done work, so nothing
    downstream has to change.

    A no-op at a *terminal* stage ends the release, and that is the honest
    reading rather than the convenient one. Eligibility is terminal because an
    event that is not releasable is not a release that goes on to be released —
    continuing would build artifacts for a version nobody is publishing. The
    destination stages are terminal because a release that reached no
    destination, or that every destination already holds, has nothing left to
    publish: continuing would sign and upload what no one asked for.

    `side_effects` marks a stage that changes something outside the job. It is
    the reason a duplicate is refused rather than tolerated: a stage that only
    reads can be repeated freely, and a stage that writes must be proven not to
    have run before it runs again.
    """

    name: str
    purpose: str
    optional: bool = False
    terminal: bool = False
    side_effects: bool = False
    scope: str = "contents: read"

    def describe(self) -> Dict[str, Any]:
        return {
            "stage": self.name,
            "purpose": self.purpose,
            "optional": self.optional,
            "terminal": self.terminal,
            "side_effects": self.side_effects,
            "scope": self.scope,
        }


STAGES: Tuple[Stage, ...] = (
    Stage(
        "eligibility",
        "decide whether this event is allowed to become a release at all",
        terminal=True,
    ),
    Stage("version", "agree on the one version this release cuts"),
    Stage(
        "release-pr",
        "make sure a release pull request exists and carries that version",
        optional=True,
        side_effects=True,
    ),
    Stage(
        "validation",
        "check the inputs this release depends on before anything is built",
    ),
    Stage(
        "source",
        "pin the release to the exact commit that is being released",
    ),
    Stage(
        "build",
        "build every configured target from that commit",
        side_effects=True,
        scope="contents: read",
    ),
    Stage(
        "sign",
        "sign what the targets produced, and record who signed it",
        side_effects=True,
    ),
    Stage(
        "verify",
        "check the artifacts against their digests and their pins",
    ),
    Stage(
        "draft",
        "create the release that will hold the assets, unpublished",
        terminal=True,
        side_effects=True,
        scope="contents: write",
    ),
    Stage(
        "publish",
        "publish the release, but only once the whole asset set is verified",
        terminal=True,
        side_effects=True,
        scope="contents: write",
    ),
    Stage(
        "sync",
        "bring the downstream records up to date with what was published",
        terminal=True,
        optional=True,
        side_effects=True,
        scope="packages: write",
    ),
)

#: The chain itself, as a table rather than as an index. A state machine whose
#: legal moves are implied by a list index has a legal move you cannot enumerate,
#: and the enumeration is what the tests assert against.
TRANSITIONS: Tuple[Tuple[str, str], ...] = tuple(
    (STAGES[index].name, STAGES[index + 1].name) for index in range(len(STAGES) - 1)
)

FIRST_STAGE = STAGES[0].name
LAST_STAGE = STAGES[-1].name

_STAGES_BY_NAME: Dict[str, Stage] = {stage.name: stage for stage in STAGES}


def stage(name: str) -> Stage:
    found = _STAGES_BY_NAME.get(name)
    if found is None:
        raise UnknownStage(
            f"{name!r} is not a release stage; the chain is: "
            + " -> ".join(_STAGES_BY_NAME),
            code="unknown-stage",
        )
    return found


def stage_names() -> Tuple[str, ...]:
    return tuple(_STAGES_BY_NAME)


def next_stage(name: str) -> Optional[str]:
    """The stage after ``name``, or None at the end of the chain."""

    current = stage(name)
    index = stage_names().index(current.name)
    remaining = STAGES[index + 1 :]
    return remaining[0].name if remaining else None


def assert_transition(source: str, destination: str) -> None:
    """Refuse a move that is not on the chain."""

    stage(source)
    stage(destination)
    if (source, destination) not in TRANSITIONS:
        legal = [f"{left}->{right}" for left, right in TRANSITIONS if left == source]
        raise IllegalTransition(
            f"{source!r} -> {destination!r} is not a release transition"
            + (f"; from {source!r} the only legal move is {legal[0]}" if legal else "")
            + ". The chain is enforced so that no caller can skip the stage that pins "
            "the source or the stage that verifies the artifacts.",
            code="illegal-transition",
        )


def scopes_are_isolated() -> bool:
    """Whether release permissions and pull-request permissions are disjoint.

    Exposed as a question rather than only as a test, because it is a property a
    caller can assert before dispatching a job rather than a property only CI
    notices afterwards.
    """

    return not set(RELEASE_SCOPES) & set(PR_AUTOMATION_SCOPES)


# -- keys --------------------------------------------------------------------


def digest_key(*parts: Any) -> str:
    """A short, stable key from the parts that identify a unit of work.

    The unit separator matters: joining with a space would let
    `("1.2", "3")` and `("1", "2.3")` produce the same key, and two releases would
    share an idempotency key and one of them would be silently skipped.
    """

    joined = "\x1f".join(str(part) for part in parts)
    return f"{KEY_NAMESPACE}-ck-{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:32]}"


def event_key(event_repository: str, event_name: str, source_sha: str) -> str:
    """The key that identifies the *event*, independent of who dispatched it.

    A delivery id is deliberately not part of this. GitHub re-dispatches, a
    failed job is re-run with a new id, and a repository can have two workflows
    watching the same branch — three deliveries, one release.
    """

    return digest_key("event", event_repository, event_name, source_sha)


def channel_key(repository: str, channel: str = "default") -> str:
    """The key that serialises a repository's releases.

    A concurrency group a workflow can use verbatim. It is release-scoped and not
    cancel-in-progress: a second release for the same repository must wait for
    the first to finish, because the first is holding the credential and the
    version namespace the second needs.
    """

    return f"{KEY_NAMESPACE}-release/{repository}/{channel}"


def release_key(repository: str, version: str) -> str:
    """The key that identifies one version of one repository.

    This is the release's identity, and every publication under it is deduped on
    it. Two events that agree on the version and disagree on nothing else are the
    same release, whichever job they arrived in.
    """

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$", version or ""):
        raise ReleaseStateError(
            f"{version!r} cannot be part of a release key: a key is used as a "
            "concurrency group name and in a journal, so it must be a slug",
            code="invalid-release-key",
        )
    return f"{KEY_NAMESPACE}-release/{repository}/{version}"


def stage_key(release: str, stage_name: str) -> str:
    """The key of one transition within one release."""

    stage(stage_name)
    return digest_key("stage", release, stage_name)


def unit_key(release: str, stage_name: str, unit: str) -> str:
    """The key of one stage's work on one unit of that stage.

    A stage that fans out — a build over three targets, a publish to two
    destinations — has to be able to say which of them has already been done, or
    "already done" is all-or-nothing and one finished target is either re-done or
    lost. The unit is the thing being acted on: a target id, a destination name.
    It is part of the key rather than part of the summary so that a later run
    recognises the same work.
    """

    stage(stage_name)
    if not unit or not unit.strip():
        raise ReleaseStateError(
            f"a {stage_name!r} key must name what it is acting on",
            code="missing-unit",
        )
    return digest_key("stage-unit", release, stage_name, unit)


# -- outcomes and the journal ------------------------------------------------


@dataclass(frozen=True)
class StageOutcome:
    """What one transition did, recorded in a form a job log can print.

    The fields exist to make three questions answerable without re-running
    anything: did this transition complete (`outcome`), was it already done
    (`duplicate`), and if it failed, may it be run again (`retryable`).
    """

    stage: str
    key: str
    outcome: str
    summary: str
    retryable: bool = False
    code: str = ""
    details: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        stage(self.stage)
        if self.outcome not in SUPPORTED_OUTCOMES:
            raise ReleaseStateError(
                f"unknown transition outcome {self.outcome!r}; supported: "
                + ", ".join(SUPPORTED_OUTCOMES),
                code="unknown-outcome",
            )
        if not self.key:
            raise ReleaseStateError(
                f"transition {self.stage!r} recorded no key, so it could never be "
                "recognised as already done",
                code="missing-key",
            )
        if not self.summary:
            raise ReleaseStateError(
                f"transition {self.stage!r} recorded no summary; a transition nobody "
                "can describe is a transition nobody will notice breaking",
                code="missing-summary",
            )
        if self.outcome == FAILED and not self.code:
            raise ReleaseStateError(
                f"failed transition {self.stage!r} carries no code, so a caller cannot "
                "tell a retryable failure from a fatal one",
                code="missing-code",
            )
        if self.outcome == COMPLETED and self.retryable:
            raise ReleaseStateError(
                f"transition {self.stage!r} completed, so there is nothing to retry"
            )

    @property
    def ok(self) -> bool:
        return self.outcome in GREEN_OUTCOMES

    @property
    def duplicate(self) -> bool:
        return self.outcome == SKIPPED and self.detail("duplicate") == "true"

    def detail(self, key: str, default: str = "") -> str:
        for name, value in self.details:
            if name == key:
                return value
        return default

    def with_detail(self, **values: str) -> "StageOutcome":
        merged = list(self.details)
        for key, value in values.items():
            merged = [(name, old) for name, old in merged if name != key]
            merged.append((key, str(value)))
        return StageOutcome(
            stage=self.stage,
            key=self.key,
            outcome=self.outcome,
            summary=self.summary,
            retryable=self.retryable,
            code=self.code,
            details=tuple(merged),
        )

    def describe(self, sequence: int = 0) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stage": self.stage,
            "key": self.key,
            "outcome": self.outcome,
            "summary": self.summary,
        }
        if sequence:
            payload["sequence"] = sequence
        if self.retryable:
            payload["retryable"] = True
        if self.code:
            payload["code"] = self.code
        if self.details:
            payload["details"] = {name: value for name, value in self.details}
        return payload

    @classmethod
    def completed(
        cls,
        stage_name: str,
        key: str,
        summary: str,
        *,
        code: str = "",
        **details: str,
    ) -> "StageOutcome":
        return cls(
            stage=stage_name,
            key=key,
            outcome=COMPLETED,
            summary=summary,
            code=code,
            details=_details(details),
        )

    @classmethod
    def skipped(
        cls,
        stage_name: str,
        key: str,
        summary: str,
        *,
        duplicate: bool = False,
        code: str = "",
        **details: str,
    ) -> "StageOutcome":
        merged = dict(details)
        if duplicate:
            merged["duplicate"] = "true"
        return cls(
            stage=stage_name,
            key=key,
            outcome=SKIPPED,
            summary=summary,
            code=code,
            details=_details(merged),
        )

    @classmethod
    def noop(
        cls,
        stage_name: str,
        key: str,
        summary: str,
        *,
        code: str = "",
        **details: str,
    ) -> "StageOutcome":
        return cls(
            stage=stage_name,
            key=key,
            outcome=NOOP,
            summary=summary,
            code=code,
            details=_details(details),
        )

    @classmethod
    def failed(
        cls,
        stage_name: str,
        key: str,
        summary: str,
        *,
        code: str,
        retryable: bool = False,
        **details: str,
    ) -> "StageOutcome":
        return cls(
            stage=stage_name,
            key=key,
            outcome=FAILED,
            summary=summary,
            retryable=retryable,
            code=code,
            details=_details(details),
        )

    @classmethod
    def blocked(
        cls,
        stage_name: str,
        key: str,
        summary: str,
        *,
        code: str = "",
        **details: str,
    ) -> "StageOutcome":
        return cls(
            stage=stage_name,
            key=key,
            outcome=BLOCKED,
            summary=summary,
            code=code,
            details=_details(details),
        )


def _details(values: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    return tuple((str(key), str(value)) for key, value in values.items())


@dataclass(frozen=True)
class Journal:
    """The record of what a release has already done.

    The journal is a value and it is passed in, not read from a store, because
    the interesting property is that a caller can hand a journal from a previous
    run to the next one and the release resumes. What a workflow persists
    between jobs is its own business; the core needs a record it can be given.
    """

    entries: Tuple[StageOutcome, ...] = ()

    def record(self, outcome: StageOutcome) -> "Journal":
        return Journal(entries=self.entries + (outcome,))

    def extend(self, outcomes: Sequence[StageOutcome]) -> "Journal":
        return Journal(entries=self.entries + tuple(outcomes))

    @property
    def completed_keys(self) -> Tuple[str, ...]:
        """The keys this journal says are done.

        Excludes a plan's entries, because "done" is the one thing a dry run must
        not claim: a key listed here is one a later run will refuse to repeat.
        And it is the *last trusted* entry per key, not every completion ever
        written, so a key that was later retried and failed is not listed as
        done — that is the one a run has to be allowed to try again.
        """

        return tuple(
            key
            for key, entry in sorted(self.trusted.items())
            if entry.outcome == COMPLETED
        )

    @property
    def reached(self) -> Tuple[str, ...]:
        return tuple(entry.stage for entry in self.entries)

    def entry(self, key: str) -> Optional[StageOutcome]:
        """The most recent entry recorded under ``key``, plan or not.

        Last, not first: a retried stage records a second entry under the same
        key, and the one that describes what was last done is the one the last
        run wrote.

        This is the reporting view. Deciding whether work is already done wants
        `trusted` instead, because the last thing written to a key may be a
        plan, and a plan that counted would mean planning a release prevents
        performing it.
        """

        found = None
        for entry in self.entries:
            if entry.key == key:
                found = entry
        return found

    def last_for(self, stage_name: str) -> Optional[StageOutcome]:
        found = None
        for entry in self.entries:
            if entry.stage == stage_name:
                found = entry
        return found

    def is_complete(self, key: str) -> bool:
        entry = self.trusted.get(key)
        return entry is not None and entry.outcome == COMPLETED

    @property
    def trusted(self) -> Dict[str, StageOutcome]:
        """The last recorded outcome per key, ignoring a plan's.

        A plan writes entries so a job can report what it would do, and those
        entries carry the same keys a run would use. So two rules follow, and
        both are needed: a plan's entry never counts, and a plan's entry never
        *erases* what a real run recorded under that key. Dropping the last
        entry instead of the last non-plan one would mean a plan of a release
        that had already been published would persuade the next run to publish
        it again — the exact harm the dry-run rule exists to prevent.
        """

        trusted: Dict[str, StageOutcome] = {}
        for entry in self.entries:
            if not Journal.was_just_a_plan(entry):
                trusted[entry.key] = entry
        return trusted

    @staticmethod
    def was_just_a_plan(entry: StageOutcome) -> bool:
        """Whether an entry is a dry run's record rather than a run's.

        A plan writes to the journal like any other pass, because the journal is
        how a job reports what it did. What a plan must not do is *count*: if a
        plan's entry made `is_complete` true, planning a release would prevent
        performing it, and the first plan anybody ran would silently disable
        releases for that version. So a plan's entry is kept for the report and
        ignored for idempotency.
        """

        return entry.detail("dry-run") == "true"

    def bound_source(self, version: str) -> Optional[str]:
        """The commit a previous run bound to ``version``, if it bound one.

        Two events that propose the same version from different commits are not
        two releases; they are one release with a contradiction in it. Refusing
        is the only safe reading, because a tag cannot point at both.
        """

        for entry in self.entries:
            if entry.stage != "source" or entry.outcome not in (COMPLETED, NOOP):
                continue
            if self.was_just_a_plan(entry):
                continue
            if entry.detail("version") != version:
                continue
            return entry.detail("source_sha") or None
        return None

    def resume_point(self) -> Optional[str]:
        """The stage a resumed run should start from, or None if it is finished.

        A stage that failed retryably is resumed at that stage. A stage that
        failed fatally, or was blocked, is not: the run stops, because the thing
        that has to change is not the release and re-running it would fail the
        same way.
        """

        entries = [entry for entry in self.entries if not self.was_just_a_plan(entry)]
        for entry in entries:
            if entry.outcome == FAILED and entry.retryable:
                return entry.stage
            if entry.outcome in (FAILED, BLOCKED):
                return None
        for name in stage_names():
            if not any(entry.stage == name for entry in entries):
                return name
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "entries": [
                entry.describe(sequence=index)
                for index, entry in enumerate(self.entries, start=1)
            ],
            "resume_point": self.resume_point(),
        }

    def summaries(self) -> List[str]:
        return [f"{entry.stage}: {entry.outcome} — {entry.summary}" for entry in self.entries]


EMPTY_JOURNAL = Journal()


__all__ = [
    "BLOCKED",
    "COMPLETED",
    "EMPTY_JOURNAL",
    "FAILED",
    "FIRST_STAGE",
    "GREEN_OUTCOMES",
    "IllegalTransition",
    "Journal",
    "KEY_NAMESPACE",
    "LAST_STAGE",
    "NOOP",
    "PR_AUTOMATION_SCOPES",
    "RELEASE_SCOPES",
    "ReleaseStateError",
    "SKIPPED",
    "STAGES",
    "SUPPORTED_OUTCOMES",
    "TRANSITIONS",
    "Stage",
    "StageOutcome",
    "UnknownStage",
    "assert_transition",
    "channel_key",
    "digest_key",
    "event_key",
    "next_stage",
    "release_key",
    "scopes_are_isolated",
    "stage",
    "stage_key",
    "stage_names",
    "unit_key",
]
