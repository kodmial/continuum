"""The production-outcome observer: what NanoDictate actually did, reconstructed
from read-only evidence.

This plane exists because the rest of the shadow plane could only answer "what
would Continuum have done" and "did the run stay live". Neither of those is a
comparison. A cutover decision needs a third answer -- "did NanoDictate do the
same thing" -- and until now the only way to get one was for somebody to write
down what production did, which is a witness, not evidence.

The rule this module is built around:

* **NanoDictate is the only writer.** Every read is a ``GET`` on a named method
  listed in :data:`OBSERVER_READ_METHODS`. There is no code path here that can
  comment, label, dispatch, merge, or publish, and the generic transports
  (``request``, ``paginate``, ``graphql``) are *excluded* from that allowlist
  even though the wider client treats them as reads -- a method that takes a
  verb as an argument is not something to hand to the one component whose whole
  claim is that it cannot write.
* **Absence is evidenced, never assumed.** Every read is bounded to the window
  and checked for truncation. "No dispatch happened" is a supported answer only
  when the read that would have found one was complete; if it was truncated, or
  failed, the window stays open and no outcome is emitted at all.
* **Partial is a real answer.** Some of what a journal plans cannot be read back
  through a read-only API. The observer says which fields those are
  (:data:`UNOBSERVABLE_DETAIL_KEYS`) and the comparison drops them from *both*
  sides, rather than reporting every dispatch as a difference.
* **The artifact is independent of the journal.** The observer writes a ledger of
  open correlation windows and its own ``continuum.shadow-outcome/v1``
  documents. It never reads a journal and never needs the shadow run to have
  happened. Pairing is a separate step (:func:`reconcile`), which is what lets a
  window survive across workflow runs that the journal's run never saw.

Windows are the unit. A production outcome is not an instant: an issue is
dispatched, reviewed, labelled, merged, and released across hours and several
independent runs. So a window opens on the event that made Continuum decide
something and closes only when there is positive evidence of how it ended -- or
when a stated horizon has passed with complete reads. Until then the window
emits nothing, because an outcome built from half a sequence is exactly the
false accusation this plane must not make.
"""

from __future__ import annotations

import dataclasses
import datetime as _datetime
import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .effects import READ_ONLY_METHODS
from .event import ShadowEvent
from .observation import (
    OBSERVED_EXACT,
    OBSERVED_PARTIAL,
    OBSERVED_UNKNOWN,
    ObservedOutcome,
    observed_from_effects,
)

LEDGER_SCHEMA = "continuum.shadow-observer-ledger/v1"

#: The only methods this module may call. A subset of the client's read surface
#: with the three generic transports removed, because they take a verb or a
#: query as an argument and therefore cannot be shown to be read-only at the call
#: site. ``tests/test_shadow_observer.py`` asserts both properties against the
#: real ``GitHubClient``.
OBSERVER_READ_METHODS = frozenset(
    {
        "get_issue",
        "get_pull",
        "list_issue_events",
        "list_issue_comments",
        "list_workflow_runs",
        "list_reviews",
        "list_check_runs",
        "combined_status_for_ref",
        "list_releases",
    }
)

#: Detail keys a journal records that no read-only API returns.
#:
#: ``inputs`` is the payload a ``workflow_dispatch`` was given. GitHub's REST
#: read APIs return the run, not the inputs it was dispatched with, so a
#: dispatch is identifiable (the run exists, it names the workflow, it says which
#: event triggered it) without being reproducible (which mode, which pull
#: request, which rungs had been ruled out).
#:
#: ``method`` is how a pull request was merged -- squash, merge, rebase. A merged
#: pull request records the resulting commit, not the button somebody pressed.
#:
#: Both are dropped from both sides of the comparison rather than reconstructed as
#: guesses. A guessed input is worse than a missing one: it would match by luck.
UNOBSERVABLE_DETAIL_KEYS: Tuple[str, ...] = ("inputs", "method")

#: Effect kinds the read surface cannot enumerate at all. Named so a window that
#: covers a scenario which could produce one is honest about it; a scenario with
#: none of these can reach ``exact``.
UNRECONSTRUCTIBLE_KINDS: Tuple[str, ...] = ("thread.resolve",)

#: How long a correlation window stays open before its reads are considered the
#: whole story.
#:
#: Not a timeout in the liveness sense. The queue's own in-flight timeout is 30
#: minutes and its cooldown zero, but those bound *retry* behaviour; the sequence
#: an issue actually goes through -- dispatch, CodeRabbit review, ready label,
#: pull request, CI, merge -- routinely takes longer than that, and a window that
#: closed when the retry budget expired would report every one of those steps as
#: a missing action. Six hours is the point past which a queue window is not
#: "still in progress" but "the automation did not continue", and it is a stated
#: input rather than a constant the reader has to find: ``--horizon-ms``.
DEFAULT_HORIZON_MS = 6 * 60 * 60 * 1000

#: A read that returns this many records may have been cut short. GitHub's
#: pagination caps at ten pages, so a caller that gets a full thousand cannot
#: tell a complete answer from a truncated one, and a truncated read cannot
#: support a claim that something did not happen.
TRUNCATION_FLOOR = 1000

#: Terminal states a window can close on. Named so a report says "the pull request
#: merged" rather than "the capture ended".
TERMINAL_MERGED = "merged"
TERMINAL_CLOSED = "closed"
TERMINAL_PUBLISHED = "published"
TERMINAL_EXPIRED = "expired"

WINDOW_OPEN = "open"
WINDOW_CLOSED = "closed"

#: Issue-event types that are evidence about a *decision* rather than a state
#: transition. A lock is evidence that the repair lock was taken; it is not an
#: effect kind, because Continuum models the lock as the label that carries it.
_LOCK_EVENTS = frozenset({"locked", "unlocked"})

#: GitHub review states, and the values Continuum's review gate submits. Two
#: vocabularies for one thing, so the mapping is explicit and tested rather than
#: left to a reader.
REVIEW_EVENTS: Mapping[str, str] = {
    "APPROVED": "APPROVE",
    "CHANGES_REQUESTED": "REQUEST_CHANGES",
    "COMMENTED": "COMMENT",
    "DISMISSED": "COMMENT",
}

#: Evidence categories that must be present before a window's absence of them is
#: a finding rather than a gap. The comment is the load-bearing entry: it is what
#: makes "the dispatch read was complete and found nothing" checkable, because a
#: window that never even saw its own subject's timeline has not established
#: anything.
#: What each scenario is *about*, and so what a window must be able to see for its
#: result to be about that scenario at all.
#:
#: One category per scenario, on purpose. A list of several would make the absence
#: rule stricter with every entry, and the result would be a plane that reports
#: "the window closed but I never saw the thing I came for" on paths that worked
#: exactly as intended -- a repair that finished before the window opened, a queue
#: that was declined because the issue was blocked. The category is the subject of
#: the scenario; whether its absence is a gap or the finding is decided by whether
#: the window closed on a terminal state or on the horizon.
_REQUIRED_CATEGORIES: Mapping[str, Tuple[str, ...]] = {
    "issue-lifecycle": ("dispatch",),
    "ci-repair": ("repair",),
    "coderabbit-finding": ("review",),
    "merge-decision": ("merge",),
    "dependency-blocked": ("dispatch",),
    "release-planning": ("release",),
    "duplicate-replay": ("dispatch",),
    "timeout-recovery": ("repair",),
}

#: Categories a window closes on, per scenario. Anything not listed closes only on
#: a real terminal state or on the horizon.
_CLOSE_ON: Mapping[str, Tuple[str, ...]] = {
    "release-planning": (TERMINAL_PUBLISHED,),
    "merge-decision": (TERMINAL_MERGED, TERMINAL_CLOSED),
}


class ObserverError(ValueError):
    """The observer could not be run, or its ledger could not be read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read(client: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call one read, and refuse anything that is not on the allowlist.

    The same shape as :func:`continuum.shadow.capture._read`, and for the same
    reason: an allowlist checked where the call is issued means a new call site
    naming a mutator fails instead of quietly writing. Two separate codes so the
    artifact distinguishes "this observer asked for something write-capable" from
    "this observer asked for a read the client does not have".
    """

    if name not in OBSERVER_READ_METHODS:
        raise ObserverError(
            "write_capable_read",
            "{!r} is not on the observer's read allowlist, so the observer "
            "refuses to call it.".format(name),
        )
    method = getattr(client, name, None)
    if method is None:
        raise ObserverError(
            "missing_read_method",
            "the client has no {!r} read for the observer to call.".format(name),
        )
    return method(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One immutable fact read out of production, with the identifiers that make
    it re-findable.

    ``source_id`` is the id GitHub gave the record -- an issue-event id, a run id,
    a review id -- and it is what makes replaying the same window produce the same
    evidence rather than a second copy of it. ``evidence_id`` is a digest over
    those identifiers, so duplicate deliveries and repeated reads collapse.

    ``correlated`` is the honest answer to "is this record about *this* subject?".
    Most reads name their subject, so it is always true. Repository-wide surfaces
    do not: a ``workflow_dispatch`` run carries the ref it ran on and nothing that
    names the issue it was dispatched for, so on an issue window it is false.
    Such a record is kept -- it happened, and dropping it would lose the fact that
    production dispatched something -- but it is not turned into an effect,
    because claiming it as an action on this subject is the correlation the API
    does not support.
    """

    evidence_id: str
    category: str
    kind: str
    target: str
    subject: str
    head_sha: str
    source: str
    source_id: str
    observed_at: str
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    correlated: bool = True

    def describe(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "category": self.category,
            "kind": self.kind,
            "target": self.target,
            "subject": self.subject,
            "head_sha": self.head_sha,
            "source": self.source,
            "source_id": self.source_id,
            "observed_at": self.observed_at,
            "correlated": self.correlated,
            "detail": {key: self.detail[key] for key in sorted(self.detail, key=str)},
        }


def evidence_id(
    repository: str, subject: str, category: str, target: str, source: str, source_id: str
) -> str:
    """A stable id for one piece of evidence.

    Derived only from identifiers GitHub itself assigned. Deliberately not
    including the timestamp or the observed payload: re-reading the same issue
    event tomorrow must produce the same id, or every observer run would append a
    fresh copy of the window's whole history to the ledger.
    """

    digest = hashlib.sha256(
        "|".join(
            [repository, subject, category, target, source, source_id or "?"]
        ).encode("utf-8")
    )
    return "ev-{}".format(digest.hexdigest()[:20])


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Window:
    """One open or closed correlation window."""

    window_id: str
    repository: str
    subject: str
    scenario: str
    head_sha: str
    correlation_ids: Tuple[str, ...]
    opened_at: str
    updated_at: str
    closed_at: str = ""
    terminal: str = ""
    #: Categories still expected, in the order they were required. An open
    #: window's ``missing`` is the reason it is not evaluable yet.
    missing: Tuple[str, ...] = ()
    limits: Tuple[str, ...] = ()
    evidence_ids: Tuple[str, ...] = ()
    #: Subject facts carried in from the opening event, so later passes can still
    #: correlate against them. Kept in the ledger because the opening event is not
    #: re-read.
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return not self.closed_at

    def with_evidence(self, ids: Sequence[str], seen: Sequence[str]) -> "Window":
        merged = list(self.evidence_ids)
        for item in ids:
            if item not in merged:
                merged.append(item)
        return dataclasses.replace(
            self,
            evidence_ids=tuple(merged),
            updated_at=seen or self.updated_at,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "window_id": self.window_id,
            "repository": self.repository,
            "subject": self.subject,
            "scenario": self.scenario,
            "head_sha": self.head_sha,
            "correlation_ids": list(self.correlation_ids),
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "terminal": self.terminal,
            "missing": list(self.missing),
            "limits": list(self.limits),
            "evidence_ids": list(self.evidence_ids),
            "extra": {key: self.extra[key] for key in sorted(self.extra, key=str)},
        }


@dataclasses.dataclass(frozen=True)
class Ledger:
    """The observer's own state: every window it knows about.

    Persisted independently of any shadow run. That independence is the point:
    an outcome has to be writable by a workflow run that the journal's run never
    saw, and a window has to survive the run that opened it.
    """

    repository: str = ""
    windows: Tuple[Window, ...] = ()
    #: Window ids whose outcome document has already been written. Carrying this
    #: forward is what makes repeated runs idempotent: an outcome is emitted once
    #: per window, no matter how many times the window is read.
    emitted: Tuple[str, ...] = ()
    #: ISO-8601 of the last run, so a ledger says how fresh it is.
    observed_at: str = ""

    def for_id(self, window_id: str) -> Optional[Window]:
        for window in self.windows:
            if window.window_id == window_id:
                return window
        return None

    def for_correlation(self, correlation_id: str) -> Tuple[Window, ...]:
        return tuple(
            window for window in self.windows if correlation_id in window.correlation_ids
        )

    def with_window(self, window: Window) -> "Ledger":
        windows = [
            window if item.window_id == window.window_id else item for item in self.windows
        ]
        if window.window_id not in {item.window_id for item in self.windows}:
            windows.append(window)
        return dataclasses.replace(
            self, repository=window.repository or self.repository, windows=tuple(windows)
        )

    def with_emitted(self, window_id: str) -> "Ledger":
        if window_id in self.emitted:
            return self
        return dataclasses.replace(self, emitted=self.emitted + (window_id,))

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "repository": self.repository,
            "observed_at": self.observed_at,
            "emitted": list(self.emitted),
            "windows": [window.describe() for window in self.windows],
        }

    @property
    def open_windows(self) -> Tuple[Window, ...]:
        return tuple(window for window in self.windows if window.is_open)


def ledger_from_payload(payload: Mapping[str, Any]) -> Ledger:
    """Read a ledger written by a previous run.

    An unknown schema is an error rather than an empty ledger, because the
    alternative -- starting from nothing -- silently discards every open window
    and turns a long-running correlation into a fresh start that reports its own
    history as absent.
    """

    if not isinstance(payload, Mapping):
        raise ObserverError("ledger_not_a_mapping", "a ledger must be an object")
    schema = _text(payload.get("schema"))
    if schema != LEDGER_SCHEMA:
        raise ObserverError(
            "unknown_ledger_schema",
            "expected {!r}, found {!r}".format(LEDGER_SCHEMA, schema or "none"),
        )
    windows: List[Window] = []
    for entry in payload.get("windows") or ():
        if not isinstance(entry, Mapping):
            continue
        window_id = _text(entry.get("window_id"))
        if not window_id:
            continue
        windows.append(
            Window(
                window_id=window_id,
                repository=_text(entry.get("repository")),
                subject=_text(entry.get("subject")),
                scenario=_text(entry.get("scenario")),
                head_sha=_text(entry.get("head_sha")),
                correlation_ids=tuple(
                    _text(item) for item in entry.get("correlation_ids") or () if _text(item)
                ),
                opened_at=_text(entry.get("opened_at")),
                updated_at=_text(entry.get("updated_at")),
                closed_at=_text(entry.get("closed_at")),
                terminal=_text(entry.get("terminal")),
                missing=tuple(_text(item) for item in entry.get("missing") or () if _text(item)),
                limits=tuple(_text(item) for item in entry.get("limits") or () if _text(item)),
                evidence_ids=tuple(
                    _text(item) for item in entry.get("evidence_ids") or () if _text(item)
                ),
                extra=dict(entry.get("extra") or {}),
            )
        )
    return Ledger(
        repository=_text(payload.get("repository")),
        windows=tuple(windows),
        emitted=tuple(_text(item) for item in payload.get("emitted") or () if _text(item)),
        observed_at=_text(payload.get("observed_at")),
    )


# --------------------------------------------------------------------------- #
# Reading production
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Reading:
    """Everything one observer pass read, and what it could not read."""

    evidence: Tuple[Evidence, ...] = ()
    failures: Tuple[Tuple[str, str], ...] = ()
    truncated: Tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.failures and not self.truncated

    @property
    def limits(self) -> Tuple[str, ...]:
        return tuple(
            ["could not read {}: {}".format(name, reason) for name, reason in self.failures]
            + ["the {} read returned at least {} records and may be cut short".format(name, TRUNCATION_FLOOR) for name in self.truncated]
        )


def _attempt(failures: List[Tuple[str, str]], name: str, call: Any) -> Any:
    """One read, recorded as a failure rather than raised.

    A read that fails does not make the observation wrong; it makes it
    unevaluable. Collapsing the two would either abort a pass that could still
    have recorded half its evidence as open, or -- worse, on the other side of
    that choice -- proceed as though the failed read had returned nothing, which
    is the exact inference this module exists to refuse.

    One :class:`ObserverError` is *not* absorbed. ``write_capable_read`` means the
    observer asked for something off its own allowlist, which is a bug in this
    module and not a fact about GitHub; recording it as a failed read would let a
    write-capable call slip past unnoticed behind an "unevaluable" label. A client
    that simply lacks a method is a fact about GitHub, and is recorded.
    """

    try:
        return call()
    except ObserverError as error:
        if error.code != "missing_read_method":
            raise
        failures.append((name, str(error)))
        return None
    except Exception as error:  # noqa: BLE001 - any read failure is a limit, not a crash
        failures.append((name, "{}: {}".format(type(error).__name__, error)))
        return None


def _records(value: Any) -> List[Mapping[str, Any]]:
    return [item for item in value or () if isinstance(item, Mapping)]


def read_production(
    client: Any,
    *,
    repository: str,
    event: ShadowEvent,
    window: Window,
) -> Reading:
    """Read every surface a window's outcome could be reconstructed from.

    The read set is fixed by the scenario, not chosen by what would be convenient:
    ``comment`` is read for every window because without the subject's own
    timeline there is no evidence that the repository was even being watched, and
    ``dispatch`` because it is the effect the queue exists to make.
    """

    failures: List[Tuple[str, str]] = []
    truncated: List[str] = []
    evidence: List[Evidence] = []
    is_release = window.scenario == "release-planning"

    def note_truncated(name: str, value: Any) -> Any:
        records = _records(value)
        if len(records) >= TRUNCATION_FLOOR:
            truncated.append(name)
        return records

    number = event.number or 0
    subject = window.subject
    categories = _REQUIRED_CATEGORIES.get(window.scenario, ("comment",))

    # -- the subject's own timeline ---------------------------------------
    # Read for every scenario except a release, because without the subject's own
    # timeline there is no evidence that anything at all happened to it, and a
    # window with no timeline is a window that established nothing.
    events: List[Mapping[str, Any]] = []
    if not is_release:
        events = _records(
            _attempt(
                failures,
                "issue-events",
                lambda: note_truncated(
                    "issue-events",
                    _read(client, "list_issue_events", number, since=window.opened_at),
                ),
            )
        )
        evidence.extend(_evidence_from_issue_events(repository, subject, window, events))

    # -- dispatches, the effect the queue exists to make -------------------
    if not is_release:
        runs = _records(
            _attempt(
                failures,
                "workflow-runs",
                lambda: note_truncated(
                    "workflow-runs",
                    _read(
                        client,
                        "list_workflow_runs",
                        created=">={}".format(window.opened_at),
                    ),
                ),
            )
        )
        evidence.extend(_evidence_from_workflow_runs(repository, subject, window, runs))

    # -- the subject itself: merged, or closed without merging -------------
    if number and not is_release:
        if _is_pull(window):
            pull = _attempt(failures, "pull", lambda: _read(client, "get_pull", number))
            evidence.extend(_evidence_from_pull(repository, subject, window, pull))
        else:
            _attempt(failures, "issue", lambda: _read(client, "get_issue", number))

    # -- reviews, for the CodeRabbit path ----------------------------------
    if "review" in categories and number:
        reviews = _records(
            _attempt(
                failures,
                "reviews",
                lambda: note_truncated(
                    "reviews", _read(client, "list_reviews", number)
                ),
            )
        )
        evidence.extend(_evidence_from_reviews(repository, subject, window, reviews))

    # -- checks and statuses, for the merge decision -----------------------
    if window.head_sha:
        checks = _records(
            _attempt(
                failures,
                "check-runs",
                lambda: note_truncated(
                    "check-runs", _read(client, "list_check_runs", window.head_sha)
                ),
            )
        )
        evidence.extend(_evidence_from_check_runs(repository, subject, window, checks))
        statuses = _attempt(
            failures,
            "statuses",
            lambda: _read(client, "combined_status_for_ref", window.head_sha),
        )
        evidence.extend(
            _evidence_from_statuses(repository, subject, window, statuses, window.head_sha)
        )

    # -- comments ----------------------------------------------------------
    if number and not is_release:
        comments = _records(
            _attempt(
                failures,
                "issue-comments",
                lambda: note_truncated(
                    "issue-comments", _read(client, "list_issue_comments", number)
                ),
            )
        )
        evidence.extend(_evidence_from_comments(repository, subject, window, comments))

    # -- the releases surface ----------------------------------------------
    if "release" in categories:
        releases = _records(
            _attempt(
                failures,
                "releases",
                lambda: note_truncated("releases", _read(client, "list_releases")),
            )
        )
        evidence.extend(_evidence_from_releases(repository, subject, window, releases, event))

    return Reading(
        evidence=tuple(item for item in evidence if _within_window(window, item.observed_at)),
        failures=tuple(failures),
        truncated=tuple(truncated),
    )


def _is_pull(window: Window) -> bool:
    return window.subject.startswith("pr:")


def _evidence(
    repository: str,
    subject: str,
    window: Window,
    *,
    category: str,
    kind: str,
    target: str,
    source: str,
    source_id: str,
    observed_at: str,
    detail: Optional[Mapping[str, Any]] = None,
    correlated: bool = True,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id(repository, subject, category, target, source, source_id),
        category=category,
        kind=kind,
        target=target,
        subject=subject,
        head_sha=window.head_sha,
        source=source,
        source_id=source_id,
        observed_at=observed_at or window.opened_at,
        detail=dict(detail or {}),
        correlated=correlated,
    )


def _within_window(window: Window, observed_at: str) -> bool:
    """Whether a record falls inside the window's own span of time.

    Applied once, centrally, to everything the pass collected, because the reads
    are not uniformly scoped: ``list_issue_events`` takes a ``since``, while
    comments, reviews and commit statuses have no time filter at all and return the
    subject's entire history. Attributing last week's approving review to a window
    that opened today would turn unrelated history into a claimed action, and would
    do it silently, which is the specific failure this whole component exists to
    prevent.

    An unparseable or absent timestamp is kept. The read that returned it was
    already scoped to the subject, and dropping the record would lose a fact on the
    strength of a formatting quirk; the artifact carries the timestamp either way,
    so a reader can see the window it was attributed to. That is a deliberate
    asymmetry with the rest of this module, which fails closed: here the record is
    the only copy of the fact, so it is kept and attributed, and there the filter is
    the only thing standing between an unrelated year of history and a claimed
    action.
    """

    if not observed_at or not window.opened_at:
        return True
    opened = _millis(window.opened_at)
    observed = _millis(observed_at)
    if observed == 0 or opened == 0:
        # A timestamp this module could not read is not a timestamp before the
        # window; treating it as one would quietly drop the record, which is the
        # opposite of what the comparison is for.
        return True
    return observed >= opened


def _number_from_subject(subject: str) -> str:
    """The issue or pull request number a subject names, if it names one.

    Matched on the prefix rather than split on the colon, because a repository
    subject is ``repository:acme/widgets`` -- which has a colon and a colon-separated
    tail, and neither half of which is a number. Treating that as number 7 would
    have the observer reading pull request seven while reporting on the repository.
    """

    prefix, _, tail = subject.partition(":")
    if prefix in ("issue", "pr") and tail.isdigit():
        return tail
    return ""


def _evidence_from_issue_events(
    repository: str, subject: str, window: Window, events: Any
) -> List[Evidence]:
    """Label, lock and close transitions, from the subject's issue timeline."""

    found: List[Evidence] = []
    for entry in _records(events):
        kind = _text(entry.get("event"))
        if kind not in ("labeled", "unlabeled", "closed", "reopened") and kind not in _LOCK_EVENTS:
            continue
        label = _text((entry.get("label") or {}).get("name"))
        target = "issue:{}".format(_number_from_subject(subject))
        if kind == "labeled":
            category, effect = "label", "label.add"
        elif kind == "unlabeled":
            category, effect = "label", "label.remove"
        elif kind in _LOCK_EVENTS:
            category, effect = "lock", kind
        else:
            category, effect = "close", "issue.{}".format(kind)
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category=category,
                kind=effect,
                target=target,
                source="issues.events",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("created_at")),
                detail={
                    "label": label,
                    "actor": _text((entry.get("actor") or {}).get("login")),
                    "event": kind,
                },
            )
        )
    return found


def _evidence_from_workflow_runs(
    repository: str, subject: str, window: Window, runs: Any
) -> List[Evidence]:
    """A run that exists is a dispatch somebody made.

    Classified rather than filtered on the workflow's name, because the set of
    workflows is the thing being validated and hard-coding it would make the
    observer agree with production by construction. A run created by
    ``workflow_dispatch`` is a dispatch; which rung sent it is evidence.

    The correlation is the hard part. ``list_workflow_runs`` is a repository-wide
    read: the only thing tying a dispatch run to the subject it was dispatched
    *for* is its ``inputs``, and no read-only endpoint returns them. What is left
    is the commit or branch the run checked out, which is enough to correlate a
    pull-request window and useless for an issue window. So the record is always
    kept, and marked uncorrelated when it cannot be tied to this subject, rather
    than attributed to whichever window happens to be reading.
    """

    found: List[Evidence] = []
    for entry in _records(runs):
        if _text(entry.get("event")) != "workflow_dispatch":
            continue
        name = _text(entry.get("path")).rsplit("/", 1)[-1]
        if not name:
            name = _text(entry.get("name"))
        category = "repair" if name in _REPAIR_WORKFLOWS else "dispatch"
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category=category,
                kind="workflow.dispatch",
                target="workflow:{}".format(name),
                source="actions.workflow_runs",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("created_at")),
                detail={
                    "ref": _text(entry.get("head_branch")),
                    "run_id": entry.get("id"),
                    "status": _text(entry.get("status")),
                    "conclusion": _text(entry.get("conclusion")),
                    "workflow": name,
                },
                correlated=_correlates(window, entry),
            )
        )
    return found


def _correlates(window: Window, run: Mapping[str, Any]) -> bool:
    """Whether a dispatch run can be tied to this window's subject.

    True when the run checked out this window's commit, or when the window names
    a pull request and the run ran on that pull request's head branch. False
    otherwise -- including for every issue window, where there is no commit to
    match, and the honest answer is that the correlation is not available.
    """

    if window.head_sha:
        if _text(run.get("head_sha")) == window.head_sha:
            return True
    branch = _text(run.get("head_branch"))
    if branch and _text(window.extra.get("head_ref")) == branch:
        return True
    return False


#: The workflows whose runs are evidence that a repair episode ran, as opposed to
#: evidence that a dispatch was made. Named because "the repair controller acted"
#: and "something was dispatched" are different findings, and the ci-repair
#: scenario is about the first.
_REPAIR_WORKFLOWS = frozenset({"opencode.yml"})


def _evidence_from_pull(
    repository: str, subject: str, window: Window, pull: Any
) -> List[Evidence]:
    found: List[Evidence] = []
    if not isinstance(pull, Mapping):
        return found
    target = "pr:{}".format(_number_from_subject(subject))
    head = _text((pull.get("head") or {}).get("sha")) or window.head_sha
    if pull.get("merged") or _text(pull.get("merged_at")):
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="merge",
                kind="pull.merge",
                target=target,
                source="pulls",
                source_id=_text(pull.get("id")),
                observed_at=_text(pull.get("merged_at")),
                detail={"head": head},
            )
        )
        return found
    if _text(pull.get("state")) == "closed":
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="close",
                kind="issue.close",
                target="issue:{}".format(_number_from_subject(subject)),
                source="pulls",
                source_id=_text(pull.get("id")),
                observed_at=_text(pull.get("closed_at")),
                detail={"head": head},
            )
        )
    return found


def _evidence_from_reviews(
    repository: str, subject: str, window: Window, reviews: Any
) -> List[Evidence]:
    found: List[Evidence] = []
    for entry in _records(reviews):
        state = _text(entry.get("state")).upper()
        if not state or state == "PENDING":
            continue
        mapped = REVIEW_EVENTS.get(state)
        if mapped is None:
            # An unrecognised review state is a vocabulary gap, not a verdict.
            # Dropping it would make the observed side look like it saw no
            # review, which is the "absence read as nothing happened" mistake.
            continue
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="review",
                kind="review.create",
                target="pr:{}".format(_number_from_subject(subject)),
                source="pulls.reviews",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("submitted_at")),
                detail={
                    "head": _text(entry.get("commit_id")),
                    "event": mapped,
                    "state": state,
                    "author": _text((entry.get("user") or {}).get("login")),
                },
            )
        )
    return found


def _evidence_from_check_runs(
    repository: str, subject: str, window: Window, checks: Any
) -> List[Evidence]:
    found: List[Evidence] = []
    for entry in _records(checks):
        if not _text(entry.get("conclusion")):
            continue
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="check",
                kind="check.run",
                target="ref:{}".format(window.head_sha),
                source="commits.check_runs",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("completed_at")),
                detail={
                    "name": _text(entry.get("name")),
                    "conclusion": _text(entry.get("conclusion")),
                },
            )
        )
    return found


def _evidence_from_statuses(
    repository: str, subject: str, window: Window, statuses: Any, head_sha: str
) -> List[Evidence]:
    """Commit statuses, which is where ``status.create`` is readable.

    Separate from check runs because they are different GitHub surfaces and
    Continuum's review gate writes a *status*; a merge-decision comparison that
    read only check runs would report the gate's own status as a missing action.
    """

    found: List[Evidence] = []
    if not isinstance(statuses, Mapping):
        return found
    for entry in _records(statuses.get("statuses")):
        context = _text(entry.get("context"))
        state = _text(entry.get("state"))
        if not context or not state:
            continue
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="check",
                kind="status.create",
                target="ref:{}".format(head_sha),
                source="commits.status",
                source_id="{}/{}".format(context, _text(entry.get("id"))),
                observed_at=_text(entry.get("updated_at")) or _text(entry.get("created_at")),
                detail={"state": state, "context": context},
            )
        )
    return found


def _evidence_from_comments(
    repository: str, subject: str, window: Window, comments: Any
) -> List[Evidence]:
    found: List[Evidence] = []
    for entry in _records(comments):
        target = "issue:{}".format(_number_from_subject(subject))
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="comment",
                kind="comment.create",
                target=target,
                source="issues.comments",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("created_at")),
                detail={
                    "text": _text(entry.get("body")),
                    "author": _text((entry.get("user") or {}).get("login")),
                },
            )
        )
    return found


def _evidence_from_releases(
    repository: str, subject: str, window: Window, releases: Any, event: ShadowEvent
) -> List[Evidence]:
    """A release for this tag, if one exists.

    Read whole rather than filtered: the releases surface is small, and reading
    all of it is what makes "no release was published" a supported answer rather
    than a gap.
    """

    tag = _text(event.release.get("tag"))
    found: List[Evidence] = []
    for entry in _records(releases):
        name = _text(entry.get("tag_name")) or _text(entry.get("name"))
        if tag and name != tag:
            continue
        found.append(
            _evidence(
                repository,
                subject,
                window,
                category="release",
                kind="release.publish",
                target="release:{}".format(name),
                source="releases",
                source_id=_text(entry.get("id")),
                observed_at=_text(entry.get("published_at"))
                or _text(entry.get("created_at")),
                detail={
                    "tag": name,
                    "draft": bool(entry.get("draft")),
                    "prerelease": bool(entry.get("prerelease")),
                },
            )
        )
    return found


# --------------------------------------------------------------------------- #
# Effects, reconstructed
# --------------------------------------------------------------------------- #


def effects_from_evidence(evidence: Sequence[Evidence]) -> Tuple[Any, ...]:
    """The production effects the evidence supports, in observed order.

    Only the effect kinds a read-only surface can actually establish. A check run
    is kept as evidence and produces no effect, because ``check.run`` is not a kind
    Continuum plans; a lock transition is kept and produces no effect either,
    because Continuum models the repair lock as the label that carries it and
    reconstructing it as an effect of its own would double-count.

    An uncorrelated record produces no effect either, and that is the whole
    reason ``correlated`` exists: production dispatched *something* during the
    window, and attributing it to this subject would be a claim the API cannot
    support.

    One record is deliberately dropped: a merged pull request also appears as a
    ``closed`` issue event, because a pull request *is* an issue. Emitting both
    would report the same single production action as two, and the second would
    look like an action Continuum missed.
    """

    from .effects import Effect

    merged = any(item.kind == "pull.merge" for item in evidence)
    built: List[Any] = []
    for item in sorted(evidence, key=lambda record: (record.observed_at, record.evidence_id)):
        if not item.correlated:
            continue
        if item.kind == "issue.close" and merged:
            continue
        detail = dict(item.detail)
        if item.kind == "workflow.dispatch":
            detail = {"ref": detail.get("ref", "")}
        elif item.kind == "label.add":
            detail = {"labels": [detail["label"]] if detail.get("label") else []}
        elif item.kind == "label.remove":
            detail = {"label": detail.get("label", "")}
        elif item.kind in ("pull.merge", "issue.close"):
            detail = {"head": detail.get("head", "")}
        elif item.kind == "review.create":
            detail = {"head": detail.get("head", ""), "event": detail.get("event", "")}
        elif item.kind == "status.create":
            detail = {"state": detail.get("state", ""), "context": detail.get("context", "")}
        elif item.kind == "comment.create":
            detail = {"text": detail.get("text", "")}
        elif item.kind == "release.publish":
            detail = {"tag": detail.get("tag", ""), "draft": detail.get("draft", False)}
        elif item.kind in ("check.run", "locked", "unlocked"):
            # Evidence, not an effect Continuum's journal would have planned.
            continue
        else:
            continue
        if not _matches_vocabulary(item):
            continue
        if not _matches_vocabulary(item):
            continue
        built.append(
            Effect(
                kind=item.kind,
                target=item.target,
                detail=detail,
                suppressed=False,
                reason="reconstructed from read-only evidence",
            )
        )
    return tuple(built)


def _matches_vocabulary(item: Evidence) -> bool:
    """Whether a reconstructed detail uses the planner's vocabulary for the kind.

    The journal's ``label.add`` names ``labels`` (a list, because one call can
    add several) and its ``label.remove`` names ``label`` (a string, because the
    adapter takes one). A reconstruction that used the same key for both would
    compare unequal for a reason that is about vocabulary rather than about a
    decision, and the vocabulary is exactly what this projection fixes.
    """

    if item.kind == "label.add":
        return bool(item.detail.get("label"))
    if item.kind == "label.remove":
        return bool(item.detail.get("label"))
    if item.kind == "workflow.dispatch":
        return bool(item.detail.get("ref"))
    return True


# --------------------------------------------------------------------------- #
# Closing a window
# --------------------------------------------------------------------------- #


def _terminal_state(evidence: Sequence[Evidence]) -> str:
    for item in evidence:
        if item.category == "merge":
            return TERMINAL_MERGED
        if item.kind == "issue.close":
            return TERMINAL_CLOSED
        if item.category == "release":
            return TERMINAL_PUBLISHED
    return ""


def _observed_status(terminal: str) -> str:
    """Production's end, in the *journal's* vocabulary.

    ``ObservedOutcome.terminal_status`` is compared against ``journal.status``,
    which is one of the planner's own statuses. Writing "merged" there would make
    every merge look like a terminal-state divergence, so the observer maps its
    terminal states onto the vocabulary the other side speaks and keeps the
    specifics in ``terminal_detail``.
    """

    return "ok" if terminal and terminal != TERMINAL_EXPIRED else "no-action"


def _expired(window: Window, now_ms: int, horizon_ms: int) -> bool:
    opened = _millis(window.opened_at)
    if opened <= 0:
        return True
    return now_ms - opened >= horizon_ms


def _millis(value: str) -> int:
    text = _text(value)
    if not text:
        return 0
    try:
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_at(ms: int) -> str:
    """UTC ISO-8601, the form every timestamp in the plane uses."""

    if ms <= 0:
        return ""
    moment = _datetime.datetime.fromtimestamp(ms / 1000, tz=_datetime.timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def window_id(repository: str, subject: str, scenario: str, correlation_id: str) -> str:
    """A window's identity.

    Keyed on the correlation id rather than on the clock, because a duplicate
    delivery of the same webhook is the same event and must land in the same
    window. Keyed on the time instead, the second delivery would open a second
    window over the same production timeline and both would report the same
    dispatch as an extra action.
    """

    digest = hashlib.sha256(
        "|".join([repository, subject, scenario, correlation_id]).encode("utf-8")
    )
    return "win-{}".format(digest.hexdigest()[:16])


def _subject_for(event: ShadowEvent) -> str:
    """The subject a window covers.

    Not :meth:`ShadowEvent.target`: that names the *effect target*, which for a
    schedule tick is the repository, and it is right, because the tick's decision
    acts on the repository rather than on a pull request. The observer needs
    something narrower, because it has to know which pull request to read. A tick
    that names a number named one, so the window covers that pull request -- and
    the repository-wide reads stay repository-wide, which is why a window is never
    attributed an action it could not tie to a subject.
    """

    if not event.number:
        return "repository:{}".format(event.repository)
    if event.event_type.startswith("issue."):
        return "issue:{}".format(event.number)
    return "pr:{}".format(event.number)


def open_window(
    repository: str,
    event: ShadowEvent,
    *,
    window_id_override: str = "",
) -> Window:
    """The window this event opens, or the one already on record for it."""

    subject = _subject_for(event)
    opened = _text(event.observed_at) or event.extra.get("__opened_at", "")
    if not opened and event.observed_at_ms:
        opened = iso_at(event.observed_at_ms)
    return Window(
        window_id=window_id_override or window_id(repository, subject, event.scenario, event.correlation_id),
        repository=repository,
        subject=subject,
        scenario=event.scenario,
        # A release event carries the commit it was cut from rather than a
        # ``head_sha``: the release *is* the decision, so the commit is in the
        # release object. Reading it here anchors the window to a commit, which is
        # what lets it read that commit's statuses and refuse to claim anything if
        # it has no anchor at all.
        head_sha=_text(event.head_sha) or _text(event.release.get("source_sha")),
        correlation_ids=(event.correlation_id,) if event.correlation_id else (),
        opened_at=opened,
        updated_at=opened,
        missing=_REQUIRED_CATEGORIES.get(event.scenario, ("comment",)),
        extra={
            key: value
            for key, value in event.extra.items()
            if key not in ("__opened_at",) and isinstance(value, (str, int, float, bool))
        },
    )


@dataclasses.dataclass(frozen=True)
class Observation:
    """One observer pass: the reading, the window it advanced, and any outcome.

    ``outcome`` is ``None`` while the window is open or while the reads were not
    good enough to support a claim. That is the ordinary case, not a failure --
    most passes do not close a window, and the ledger is the artifact that shows
    progress.
    """

    ledger: Ledger
    window: Window
    reading: Reading
    evidence: Tuple[Evidence, ...] = ()
    outcome: Optional[ObservedOutcome] = None
    emitted: bool = False

    @property
    def new_evidence(self) -> Tuple[Evidence, ...]:
        return self.evidence


def observe(
    client: Any,
    *,
    repository: str,
    event: ShadowEvent,
    ledger: Optional[Ledger] = None,
    now_ms: Optional[int] = None,
    horizon_ms: int = DEFAULT_HORIZON_MS,
) -> Observation:
    """Read production for one window, advance it, and emit an outcome if it closed.

    Idempotent by construction: evidence is keyed on GitHub's own identifiers and
    merged into the ledger, and a window whose outcome has already been emitted
    does not emit it again. Running the same event twice against the same
    production state produces the same ledger.
    """

    ledger = ledger or Ledger()
    moment = int(now_ms if now_ms is not None else 0)
    subject = _subject_for(event)
    # A window is keyed on the correlation id, because that is what distinguishes
    # two decisions about the same subject from one decision. A duplicate delivery
    # carries its *own* correlation id and names the original in ``replays``, so it
    # has to be looked up by that: opening a second window over the same production
    # timeline would report the same dispatch as an extra action a second time.
    identities = tuple(
        identity for identity in (event.correlation_id, _text(event.replays)) if identity
    )
    existing = next(
        (
            window
            for window in ledger.windows
            if any(identity in window.correlation_ids for identity in identities)
            or window.window_id
            == window_id(repository, subject, event.scenario, event.correlation_id)
        ),
        None,
    )
    window = existing or open_window(repository, event)
    known_identities = window.correlation_ids
    if window.is_open and identities:
        window = dataclasses.replace(
            window,
            correlation_ids=known_identities
            + tuple(identity for identity in identities if identity not in known_identities),
        )
    if window.is_open and moment:
        window = dataclasses.replace(window, updated_at=iso_at(moment))

    reading = read_production(client, repository=repository, event=event, window=window)
    known = set(window.evidence_ids)
    fresh = tuple(item for item in reading.evidence if item.evidence_id not in known)
    window = window.with_evidence([item.evidence_id for item in fresh], iso_at(moment))
    window = dataclasses.replace(
        window,
        limits=tuple(sorted(set(window.limits) | set(reading.limits))),
    )

    next_ledger = ledger.with_window(window)
    next_ledger = dataclasses.replace(next_ledger, observed_at=iso_at(moment))

    if window.is_open:
        terminal = _terminal_state(reading.evidence)
        window = dataclasses.replace(
            window, missing=_missing_categories(window, reading.evidence)
        )
        # A window closes on evidence of how it ended, or on the horizon -- never
        # on the event that opened it. Scenarios differ in what counts as the
        # end: a merge decision *is* the merge, so that closes it, while the queue
        # scenarios carry on after the pull request is merged (the review label is
        # removed on close) and closing there would truncate the very effects
        # being compared.
        if terminal in _CLOSE_ON.get(window.scenario, ()):
            window = _close(window, terminal, iso_at(moment))
        elif _expired(window, moment, horizon_ms):
            window = _close(window, TERMINAL_EXPIRED, iso_at(moment))
        else:
            next_ledger = next_ledger.with_window(window)
            return Observation(
                ledger=next_ledger, window=window, reading=reading, evidence=fresh
            )

    next_ledger = next_ledger.with_window(window)
    if window.window_id in next_ledger.emitted:
        return Observation(
            ledger=next_ledger, window=window, reading=reading, evidence=fresh, emitted=False
        )

    outcome = _outcome_for(window, reading, fresh)
    if outcome is None:
        return Observation(ledger=next_ledger, window=window, reading=reading, evidence=fresh)
    next_ledger = next_ledger.with_emitted(window.window_id)
    return Observation(
        ledger=next_ledger,
        window=window,
        reading=reading,
        evidence=fresh,
        outcome=outcome,
        emitted=True,
    )


def _close(window: Window, terminal: str, at: str) -> Window:
    return dataclasses.replace(
        window, closed_at=at or window.updated_at, terminal=terminal, missing=()
    )


def _missing_categories(window: Window, evidence: Sequence[Evidence]) -> Tuple[str, ...]:
    seen = {item.category for item in evidence}
    return tuple(
        category for category in _REQUIRED_CATEGORIES.get(window.scenario, ()) if category not in seen
    )


def _outcome_for(
    window: Window, reading: Reading, fresh: Sequence[Evidence]
) -> Optional[ObservedOutcome]:
    """The outcome for a closed window, or ``None`` when the reads cannot support one.

    Three ways this refuses, and the choice between them is the whole design:

    * **A required read failed.** Nothing was established about the subject, so
      there is no claim to make. This is the ordinary case while GitHub is having a
      bad afternoon, and it is the case most likely to be got wrong in the other
      direction -- reporting "production did nothing" when in fact the observer
      could not look is how a plane like this destroys the trust it was built to
      earn.

    * **The reads may have been truncated and the window closed on absence.**
      "No dispatch happened" needs a complete read to be a finding; a truncated one
      cannot tell a quiet episode from a busy one whose first thousand records are
      the only ones returned. Truncation alongside a real terminal state is
      different -- the merge or the release was read from its own surface -- so it
      lowers fidelity instead of blocking.

    * **Nothing at all was seen of the subject.** A numbered subject has a timeline,
      a pull request, comments and statuses; a pass that read all of them and got
      nothing back is a real finding, but a pass that got nothing back because the
      endpoint answered 404 with an empty object is not, and the two are
      indistinguishable from here. Refusing to answer is the honest response to that
      ambiguity.

    What is deliberately *not* a reason to refuse: the scenario's required category
    being absent. Those categories describe what the window is watching for, and
    their absence is frequently the finding -- a blocked issue is supposed to have no
    dispatch. Blocking on them would turn every correct refusal into an unevaluable
    window, and the plane would spend its life reporting that production behaved.
    """

    if reading.failures:
        return None
    on_absence = window.terminal == TERMINAL_EXPIRED
    if on_absence and reading.truncated:
        return None
    if not _is_anchored(window):
        # Every read this window could make was repository-wide, so an empty
        # effect list would be indistinguishable from "the repository is quiet" --
        # which is not a statement about any decision. Refusing is the honest
        # answer, and it is narrow: a numbered subject or a commit is an anchor.
        return None

    evidence = reading.evidence
    effects = effects_from_evidence(evidence)
    # Fidelity is about the *reads*, and unobservable detail keys are about the
    # surfaces. A key no endpoint returns is a permanent property of that surface,
    # not a gap in this pass, so it lowers the comparison's resolution (the
    # ``unobservable`` list) without lowering the reads' fidelity.
    fidelity = OBSERVED_PARTIAL if reading.truncated else OBSERVED_EXACT
    limits: List[str] = list(reading.limits)
    unobservable: List[str] = []
    stranded = _missing_categories(window, evidence)
    if stranded:
        limits.append(
            "the window was watching for {} and saw none of it; that absence is "
            "the finding rather than a gap".format(", ".join(stranded))
        )

    def drop(key: str, why: str) -> None:
        if key not in unobservable:
            unobservable.append(key)
        if why not in limits:
            limits.append(why)

    if any(item.kind == "workflow.dispatch" for item in evidence):
        drop(
            "inputs",
            "workflow dispatch inputs are not returned by any read-only API, so "
            "they are excluded from the comparison on both sides",
        )
    if any(item.kind == "pull.merge" for item in evidence):
        drop(
            "method",
            "a merged pull request does not record which merge method was used, "
            "so the method is excluded from the comparison on both sides",
        )
    if any(item.kind == "release.publish" for item in evidence):
        drop(
            "destination",
            "a GitHub release names no publication destination, so the "
            "destination Continuum planned for is excluded from the comparison "
            "on both sides",
        )
    stray = [item for item in evidence if not item.correlated]
    if stray:
        limits.append(
            "{} dispatch run(s) during this window could not be tied to this "
            "subject, because a dispatch's inputs are not readable. They are "
            "recorded as evidence and are not counted as effects on this "
            "subject".format(len(stray))
        )
    for item in UNRECONSTRUCTIBLE_KINDS:
        if any(record.kind == item for record in evidence):
            limits.append(
                "{} was seen but cannot be reconstructed into an effect from "
                "read-only evidence".format(item)
            )
    if on_absence:
        limits.append(
            "the window reached its horizon with complete reads, so the empty "
            "effect list is the finding rather than a gap"
        )

    outcome = observed_from_effects(
        window.correlation_ids[0] if window.correlation_ids else "",
        effects,
        terminal_status=_observed_status(window.terminal),
        terminal_detail=_terminal_detail(window),
        fidelity=fidelity,
        limits=limits,
        unobservable=unobservable,
        links={
            "repository": window.repository,
            "window_id": window.window_id,
            "subject": window.subject,
            "scenario": window.scenario,
            "opened_at": window.opened_at,
            "closed_at": window.closed_at,
        },
    )
    # The span is a top-level field rather than only a link, because the two
    # answer different questions: the links say which window this belongs to, and
    # these say when the thing being described actually happened. A reader
    # filtering outcomes by time cannot follow a link, and an outcome whose own
    # timestamps are empty invites exactly that misreading.
    return dataclasses.replace(
        outcome,
        window_started_at=window.opened_at,
        window_ended_at=window.closed_at,
    )


def _is_anchored(window: Window) -> bool:
    """Whether this window is about something specific enough to have an outcome.

    A numbered subject (an issue, a pull request) or a commit. A window anchored on
    neither can only read the repository at large, so nothing it finds can be
    attributed to a decision -- and nothing it fails to find is a statement about
    one either.
    """

    return bool(_number_from_subject(window.subject) or window.head_sha)


def _terminal_detail(window: Window) -> str:
    if window.terminal == TERMINAL_EXPIRED:
        return "no terminal activity on {} before the window's horizon".format(window.subject)
    return "{}: {}".format(window.subject, window.terminal)


# --------------------------------------------------------------------------- #
# Pairing with the journal
# --------------------------------------------------------------------------- #


#: Why a journal could or could not be paired with an outcome. A code rather than
#: a sentence, so a report can count them; the sentence is for a human.
PAIRED = "paired"
NO_WINDOW = "no-window"
WINDOW_OPEN = "window-open"
CLOSED_UNEVALUABLE = "closed-unevaluable"
UNKNOWN_FIDELITY = "unknown-fidelity"

PAIRING_REASONS: Mapping[str, str] = {
    PAIRED: "the observer produced an outcome for this event",
    NO_WINDOW: "the observer has no window for this event",
    WINDOW_OPEN: "production has not finished with the subject yet, so there is no "
    "outcome to compare",
    CLOSED_UNEVALUABLE: "the window closed but the reads could not support a claim, "
    "so parity is not evaluable",
    UNKNOWN_FIDELITY: "the outcome established nothing, so parity is not evaluable",
}


@dataclasses.dataclass(frozen=True)
class Reconciliation:
    """The pairing of one journal with whatever the observer knows about it."""

    correlation_id: str
    window_id: str
    outcome: Optional[ObservedOutcome]
    status: str

    @property
    def verdict(self) -> str:
        return PAIRING_REASONS.get(self.status, self.status)

    def describe(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "window_id": self.window_id,
            "status": self.status,
            "verdict": self.verdict,
            "window_known": self.window_id != "",
            "outcome_emitted": self.outcome is not None,
            "fidelity": self.outcome.fidelity if self.outcome else "",
        }


def reconcile(
    ledger: Ledger,
    correlation_id: str,
    outcome: Optional[ObservedOutcome] = None,
) -> Reconciliation:
    """Pair one shadow run with the observer's independent record of it.

    The pairing happens here and nowhere else. The observer never reads a journal
    and the journal never reads the ledger, so either side can be produced, and
    re-produced, without the other: an outcome that arrived before the run it
    belongs to, or a run whose window has not closed yet, are both ordinary states
    rather than a deadlock.
    """

    windows = ledger.for_correlation(correlation_id)
    window = windows[-1] if windows else None
    if outcome is None:
        if window is None:
            return Reconciliation(correlation_id, "", None, NO_WINDOW)
        if window.is_open:
            return Reconciliation(correlation_id, window.window_id, None, WINDOW_OPEN)
        return Reconciliation(correlation_id, window.window_id, None, CLOSED_UNEVALUABLE)
    if outcome.fidelity == OBSERVED_UNKNOWN:
        return Reconciliation(
            correlation_id, window.window_id if window else "", outcome, UNKNOWN_FIDELITY
        )
    return Reconciliation(correlation_id, window.window_id if window else "", outcome, PAIRED)


def summarize(observation: Observation) -> str:
    """One line, for a job log."""

    window = observation.window
    if observation.outcome is not None:
        return "observer {} [{}]: {} outcome ({}) from {} evidence record(s)".format(
            window.window_id,
            window.scenario,
            observation.outcome.fidelity,
            window.terminal or "open",
            len(observation.evidence),
        )
    if window.is_open:
        return "observer {} [{}]: open, waiting on {}".format(
            window.window_id, window.scenario, ", ".join(window.missing) or "terminal evidence"
        )
    return "observer {} [{}]: closed as {!r} but not evaluable ({})".format(
        window.window_id,
        window.scenario,
        window.terminal,
        "; ".join(window.limits) or "no stated limit",
    )


__all__ = [
    "DEFAULT_HORIZON_MS",
    "LEDGER_SCHEMA",
    "OBSERVER_READ_METHODS",
    "REVIEW_EVENTS",
    "TRUNCATION_FLOOR",
    "UNOBSERVABLE_DETAIL_KEYS",
    "UNRECONSTRUCTIBLE_KINDS",
    "Evidence",
    "Ledger",
    "ObserverError",
    "Observation",
    "Reading",
    "Reconciliation",
    "Window",
    "effects_from_evidence",
    "evidence_id",
    "iso_at",
    "ledger_from_payload",
    "observe",
    "open_window",
    "reconcile",
    "read_production",
    "summarize",
    "window_id",
]
