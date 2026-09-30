"""The captured state a shadow decision runs against, and the read-only client
that serves it.

The record-only client is what makes "the same decision engine" literally true
rather than aspirational. ``queue_controller.reconcile`` is not reimplemented,
not stubbed, and not bypassed: it is called, and it calls
``client.create_issue_comment`` and ``client.dispatch_workflow`` exactly as it
would in production. What changes is the object on the other end of those two
calls. The steps *after* an effect -- record the in-flight slot, roll the
dispatch back when the slot could not be recorded, refuse to publish a plan that
is incomplete -- are where a surprising amount of orchestration lives, and a
shadow run that skipped them would be validating a different program.

Reads are answered from the captured snapshot, which has three consequences worth
stating:

* no network, so the barrier can refuse ``socket.connect`` outright;
* determinism, so the same case replays to a byte-identical plan against a
  newer Continuum;
* the decision is reproducible, because the state it read is in the journal.

The last one is the reason ``ShadowGitHubClient`` refuses unknown attributes
instead of returning ``None``. A read method it does not implement would
otherwise look like an empty result -- an empty check-run list reads as "no CI",
which is a *decision*, and it would be a decision nobody made.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .effects import Effect, RecordingEffects, WriteBarrierViolation, assert_registry_covers

SNAPSHOT_SCHEMA = "continuum.shadow-state/v1"


class StateError(ValueError):
    """The captured state is not usable for a decision.

    Same reasoning as :class:`~continuum.shadow.event.EventError`: a snapshot
    that is missing the pull request the event is about cannot be padded with
    defaults, because the defaults are themselves a decision.
    """

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
class PullView:
    """One pull request, exactly as the event saw it."""

    number: int
    state: str = "open"
    draft: bool = False
    head_ref: str = ""
    head_sha: str = ""
    base_ref: str = "main"
    base_sha: str = ""
    body: str = ""
    #: ``full_name`` of the head repository. Empty means the pull request is from
    #: a fork, which is exactly the case the trust policy has to refuse.
    head_repository: str = ""
    #: ``full_name`` of the repository the pull request targets. Recorded
    #: separately from the head because for a fork they differ, and the trust
    #: policy's "does this target us?" check reads the base.
    base_repository: str = ""
    labels: Tuple[str, ...] = ()
    mergeable: str = ""
    mergeable_state: str = ""
    review_decision: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_api(self) -> Dict[str, Any]:
        """The shape ``trust_policy.is_trusted_pull_request`` expects.

        The ``base.repo.full_name`` is not optional decoration: the trust policy
        decides whether a pull request targets *this* repository by reading it,
        and a capture that omitted it would be read as "targets some other
        repository", which turns every pull request into a fork.
        """

        return {
            "number": self.number,
            "state": self.state,
            "draft": self.draft,
            "merged": self.state == "closed" and self.mergeable_state == "merged",
            "head": {
                "ref": self.head_ref,
                "sha": self.head_sha,
                "repo": {"full_name": self.head_repository} if self.head_repository else None,
            },
            "base": {
                "ref": self.base_ref,
                "sha": self.base_sha,
                "repo": {"full_name": self.base_repository or self.head_repository},
            },
            "body": self.body,
            "labels": [{"name": name} for name in self.labels],
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def as_capture(self) -> Dict[str, Any]:
        """The full-fidelity form :func:`state_from_payload` reads.

        Distinct from :meth:`describe` because the two answer different
        questions. ``describe`` is what goes in a journal artifact, where a
        reviewer needs to know which pull request and which head were decided
        about -- not the body text. ``as_capture`` is what makes an artifact
        *replayable*: a replay run has to read the same state this run read, so
        anything it would decide differently is a hole in the evidence rather
        than a detail.
        """

        return {
            "number": self.number,
            "state": self.state,
            "draft": self.draft,
            # Nested, and named the way the parser reads it: a capture the
            # parser cannot read back is not a capture.
            "head": {
                "ref": self.head_ref,
                "sha": self.head_sha,
                "repository": self.head_repository,
            },
            "base": {
                "ref": self.base_ref,
                "sha": self.base_sha,
                "repository": self.base_repository,
            },
            "body": self.body,
            "labels": list(self.labels),
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "review_decision": self.review_decision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "draft": self.draft,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "labels": list(self.labels),
            "head_repository": self.head_repository,
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
        }


@dataclasses.dataclass(frozen=True)
class IssueView:
    number: int
    state: str = "open"
    author: str = ""
    author_association: str = ""
    title: str = ""
    body: str = ""
    labels: Tuple[str, ...] = ()
    updated_at: str = ""

    def as_api(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "title": self.title,
            "body": self.body,
            "labels": [{"name": name} for name in self.labels],
            "user": {
                "login": self.author,
                "type": "Bot" if self.author.endswith("[bot]") else "User",
            },
            "author_association": self.author_association,
            "updated_at": self.updated_at,
        }

    def as_capture(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "author": self.author,
            "author_association": self.author_association,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "updated_at": self.updated_at,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "author": self.author,
            "labels": list(self.labels),
        }


@dataclasses.dataclass(frozen=True)
class ObservedState:
    """The immutable state a shadow decision reads.

    Everything the decision path can consult is here. There is no fallback to
    "ask GitHub", because a shadow run has no business asking GitHub and a
    decision that silently consulted live state would not be the decision the
    journal describes.
    """

    repository: str
    base_ref: str = "main"
    repository_owner: str = ""
    #: Trusted actors, as the ``AUTOMATION_TRUSTED_ACTORS`` variable holds them.
    trusted_actors: str = ""
    pulls: Tuple[PullView, ...] = ()
    issues: Tuple[IssueView, ...] = ()
    #: ``pr number -> comment bodies``, in the order GitHub returned them.
    issue_comments: Mapping[int, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    #: ``pr number -> reviews`` as ``(login, state, body)``.
    reviews: Mapping[int, Tuple[Tuple[str, str, str], ...]] = dataclasses.field(default_factory=dict)
    #: ``pr number -> review comments`` as ``(login, body, in_reply_to)``.
    review_comments: Mapping[int, Tuple[Tuple[str, str, int], ...]] = dataclasses.field(
        default_factory=dict
    )
    #: ``ref -> (state, conclusion)`` pairs, newest first as GitHub reports them.
    check_runs: Mapping[str, Tuple[Tuple[str, str], ...]] = dataclasses.field(default_factory=dict)
    #: ``ref -> combined status`` as GitHub's combined endpoint reports it.
    combined_status: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: ``pr number -> filenames``.
    changed_files: Mapping[int, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    #: ``pr number -> review threads`` as ``(thread id, is_resolved, path, line)``.
    review_threads: Mapping[int, Tuple[Tuple[str, bool, str, int], ...]] = dataclasses.field(
        default_factory=dict
    )
    #: ``pr number -> whether a repair run is currently in flight``.
    repair_in_flight: Mapping[int, bool] = dataclasses.field(default_factory=dict)
    #: ``issue number -> open blockers`` as ``(blocker number, blocker state)``,
    #: from GitHub's issue-dependency endpoint. Captured rather than fetched,
    #: because the production scheduler reads it before every dispatch and a
    #: shadow run that guessed at it would be guessing at the thing the scenario
    #: exists to exercise.
    blocked_by: Mapping[int, Tuple[Tuple[int, str], ...]] = dataclasses.field(default_factory=dict)
    #: The commit a release tag resolves to, when the event was a release event.
    #:
    #: A ``release`` webhook names a tag and a branch, never a commit, and the
    #: release contract refuses to plan an unpinned release. So the capture
    #: resolves the tag here, once, from the same read surface as everything
    #: else -- and if it cannot, it says so rather than leaving the release
    #: looking plannable.
    release_commit: str = ""
    #: Milliseconds since the epoch for the run that captured this state.
    captured_at_ms: int = 0
    #: ``field -> why it could not be read``, from a live capture that could not
    #: reach part of the state.
    #:
    #: Recorded rather than defaulted, because the difference between "there are
    #: no unresolved review threads" and "the capture could not see the threads"
    #: is the difference between a repair ladder and a merge nobody reviewed. A
    #: guard that reads an unreadable field refuses, and the journal names this
    #: mapping as the reason.
    unreadable: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def owner(self) -> str:
        return self.repository_owner or self.repository.split("/", 1)[0]

    def pull(self, number: int) -> Optional[PullView]:
        for candidate in self.pulls:
            if candidate.number == number:
                return candidate
        return None

    def require_pull(self, number: Optional[int]) -> PullView:
        if number is None:
            raise StateError("missing_number", "This event has no number to look up.")
        found = self.pull(number)
        if found is None:
            raise StateError(
                "unknown_pull_request",
                "The captured state has no pull request #{}.".format(number),
            )
        return found

    def issue(self, number: int) -> Optional[IssueView]:
        for candidate in self.issues:
            if candidate.number == number:
                return candidate
        return None

    def require_issue(self, number: Optional[int]) -> IssueView:
        if number is None:
            raise StateError("missing_number", "This event has no number to look up.")
        found = self.issue(number)
        if found is None:
            raise StateError(
                "unknown_issue",
                "The captured state has no issue #{}.".format(number),
            )
        return found

    def open_blockers(self, number: int) -> Tuple[Tuple[int, str], ...]:
        """The captured blockers that are still open."""

        return tuple(
            (blocker, state)
            for blocker, state in self.blocked_by.get(number, ())
            if state == "open"
        )

    def comments(self, number: int) -> Tuple[str, ...]:
        return tuple(self.issue_comments.get(number, ()))

    def as_capture(self) -> Dict[str, Any]:
        """A payload :func:`state_from_payload` turns back into this state.

        This is what the replay plane feeds a second run. Round-tripping has to
        be exact: a replay that read a slightly different capture would compare
        two different decisions and report the difference as drift, and a replay
        that could not read its own artifact at all could not run. The pair is
        asserted by ``tests/test_shadow_state.py``.
        """

        return {
            "schema": SNAPSHOT_SCHEMA,
            "repository": self.repository,
            "repository_owner": self.repository_owner,
            "base_ref": self.base_ref,
            "trusted_actors": self.trusted_actors,
            "pulls": [pull.as_capture() for pull in self.pulls],
            "issues": [issue.as_capture() for issue in self.issues],
            "issue_comments": {
                str(number): list(bodies)
                for number, bodies in sorted(self.issue_comments.items())
            },
            "reviews": {
                str(number): [list(entry) for entry in entries]
                for number, entries in sorted(self.reviews.items())
            },
            "review_comments": {
                str(number): [list(entry) for entry in entries]
                for number, entries in sorted(self.review_comments.items())
            },
            "check_runs": {
                ref: [list(pair) for pair in runs] for ref, runs in sorted(self.check_runs.items())
            },
            "combined_status": {ref: value for ref, value in sorted(self.combined_status.items())},
            "changed_files": {
                str(number): list(files) for number, files in sorted(self.changed_files.items())
            },
            "review_threads": {
                str(number): [list(item) for item in threads]
                for number, threads in sorted(self.review_threads.items())
            },
            "repair_in_flight": {
                str(number): bool(value)
                for number, value in sorted(self.repair_in_flight.items())
            },
            "blocked_by": {
                str(number): [list(item) for item in blockers]
                for number, blockers in sorted(self.blocked_by.items())
            },
            "release_commit": self.release_commit,
            "captured_at_ms": self.captured_at_ms,
            "unreadable": {key: self.unreadable[key] for key in sorted(self.unreadable)},
        }

    def is_complete(self) -> bool:
        """Whether the capture saw everything it tried to.

        A guard consults this rather than assuming: an incomplete capture is
        still a usable capture, but the decision made on it has to be able to say
        that it was made on one.
        """

        return not self.unreadable

    def describe(self) -> Dict[str, Any]:
        """The summary that goes into a journal artifact.

        Counts, not bodies: a reviewer needs to know that three comments were
        captured for a pull request and that two review threads were, so they
        can judge whether the run had the evidence it needed. The bodies
        themselves are what :meth:`as_capture` keeps, and the two are never
        interchanged -- a summary that claimed to be a capture would replay as
        an empty repository.
        """

        return {
            "schema": SNAPSHOT_SCHEMA,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "trusted_actors": self.trusted_actors,
            "pulls": [pull.describe() for pull in self.pulls],
            "issues": [issue.describe() for issue in self.issues],
            "issue_comments": {
                str(number): len(bodies)
                for number, bodies in sorted(self.issue_comments.items())
            },
            "reviews": {
                str(number): len(entries) for number, entries in sorted(self.reviews.items())
            },
            "review_comments": {
                str(number): len(entries)
                for number, entries in sorted(self.review_comments.items())
            },
            "check_runs": {
                ref: [list(pair) for pair in runs] for ref, runs in sorted(self.check_runs.items())
            },
            "combined_status": {ref: state for ref, state in sorted(self.combined_status.items())},
            "changed_files": {
                str(number): list(files) for number, files in sorted(self.changed_files.items())
            },
            "review_threads": {
                str(number): [list(item) for item in threads]
                for number, threads in sorted(self.review_threads.items())
            },
            "repair_in_flight": {
                str(number): bool(value)
                for number, value in sorted(self.repair_in_flight.items())
            },
            "blocked_by": {
                str(number): [list(item) for item in blockers]
                for number, blockers in sorted(self.blocked_by.items())
            },
            "captured_at_ms": self.captured_at_ms,
        }


# --------------------------------------------------------------------------- #
# Parsing a captured snapshot
# --------------------------------------------------------------------------- #


def state_from_payload(payload: Mapping[str, Any]) -> ObservedState:
    """Build an :class:`ObservedState` from a captured snapshot document.

    Sanitization happens before this: the snapshot a bridge uploads has already
    had bodies, titles, and log text reduced to what the decision needs. This
    function does not sanitize, because silently truncating a captured body would
    make a replay of the sanitized case disagree with the original run in a way
    that looks like a decision difference.
    """

    if not isinstance(payload, Mapping):
        raise StateError("state_not_a_mapping", "Captured state must be an object.")

    repository = _text(payload.get("repository"))
    if "/" not in repository:
        raise StateError(
            "invalid_repository",
            "repository {!r} is not an owner/name pair.".format(repository),
        )

    base_ref = _text(payload.get("base_ref")) or "main"

    pulls: List[PullView] = []
    for entry in _sequence(payload.get("pulls")):
        number = _number(_get(entry, "number"))
        if number <= 0:
            raise StateError("invalid_pull_request", "A captured pull request has no number.")
        head = _get(entry, "head")
        base = _get(entry, "base")
        pulls.append(
            PullView(
                number=number,
                state=_text(_get(entry, "state")) or "open",
                draft=bool(_get(entry, "draft")),
                head_ref=_text(_get(head, "ref")),
                head_sha=_text(_get(head, "sha")).lower(),
                base_ref=_text(_get(base, "ref")) or base_ref,
                base_sha=_text(_get(base, "sha")).lower(),
                body=_text(_get(entry, "body")),
                head_repository=_text(_get(head, "repository")),
                base_repository=_text(_get(base, "repository")) or repository,
                labels=_labels(_get(entry, "labels")),
                mergeable=_text(_get(entry, "mergeable")),
                mergeable_state=_text(_get(entry, "mergeable_state")),
                review_decision=_text(_get(entry, "review_decision")),
                created_at=_text(_get(entry, "created_at")),
                updated_at=_text(_get(entry, "updated_at")),
            )
        )

    issues: List[IssueView] = []
    for entry in _sequence(payload.get("issues")):
        number = _number(_get(entry, "number"))
        if number <= 0:
            raise StateError("invalid_issue", "A captured issue has no number.")
        issues.append(
            IssueView(
                number=number,
                state=_text(_get(entry, "state")) or "open",
                author=_text(_get(entry, "author")),
                author_association=_text(_get(entry, "author_association")),
                title=_text(_get(entry, "title")),
                body=_text(_get(entry, "body")),
                labels=_labels(_get(entry, "labels")),
                updated_at=_text(_get(entry, "updated_at")),
            )
        )

    return ObservedState(
        repository=repository,
        base_ref=base_ref,
        repository_owner=_text(payload.get("repository_owner")),
        trusted_actors=_text(payload.get("trusted_actors")),
        pulls=tuple(sorted(pulls, key=lambda item: item.number)),
        issues=tuple(sorted(issues, key=lambda item: item.number)),
        issue_comments=_int_keyed_bodies(payload.get("issue_comments")),
        reviews=_int_keyed_triples(payload.get("reviews")),
        review_comments=_int_keyed_review_comments(payload.get("review_comments")),
        check_runs=_ref_keyed_runs(payload.get("check_runs")),
        combined_status=_str_map(payload.get("combined_status")),
        changed_files=_int_keyed_lists(payload.get("changed_files")),
        review_threads=_int_keyed_threads(payload.get("review_threads")),
        repair_in_flight=_int_keyed_bools(payload.get("repair_in_flight")),
        blocked_by=_int_keyed_blockers(payload.get("blocked_by")),
        release_commit=_text(payload.get("release_commit")),
        captured_at_ms=_number(payload.get("captured_at_ms")),
        unreadable=_str_map(payload.get("unreadable")),
    )


def _get(container: Any, key: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(key)
    return None


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _labels(value: Any) -> Tuple[str, ...]:
    names = set()
    for item in _sequence(value):
        name = _text(_get(item, "name")) or _text(item)
        if name:
            names.add(name)
    return tuple(sorted(names))


def _int_keyed_bodies(value: Any) -> Dict[int, Tuple[str, ...]]:
    result: Dict[int, Tuple[str, ...]] = {}
    if isinstance(value, Mapping):
        for key, bodies in value.items():
            number = _number(key)
            if number > 0:
                result[number] = tuple(_text(body) for body in _sequence(bodies))
    return result


def _int_keyed_triples(value: Any) -> Dict[int, Tuple[Tuple[str, str, str], ...]]:
    result: Dict[int, Tuple[Tuple[str, str, str], ...]] = {}
    if isinstance(value, Mapping):
        for key, entries in value.items():
            number = _number(key)
            if number <= 0:
                continue
            rows: List[Tuple[str, str, str]] = []
            for entry in _sequence(entries):
                if isinstance(entry, Mapping):
                    rows.append(
                        (
                            _text(entry.get("login")),
                            _text(entry.get("state")),
                            _text(entry.get("body")),
                        )
                    )
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    rows.append(
                        (
                            _text(entry[0]),
                            _text(entry[1]),
                            _text(entry[2]) if len(entry) > 2 else "",
                        )
                    )
            result[number] = tuple(rows)
    return result


def _int_keyed_review_comments(value: Any) -> Dict[int, Tuple[Tuple[str, str, int], ...]]:
    result: Dict[int, Tuple[Tuple[str, str, int], ...]] = {}
    if isinstance(value, Mapping):
        for key, entries in value.items():
            number = _number(key)
            if number <= 0:
                continue
            rows: List[Tuple[str, str, int]] = []
            for entry in _sequence(entries):
                if isinstance(entry, Mapping):
                    rows.append(
                        (
                            _text(entry.get("login")),
                            _text(entry.get("body")),
                            _number(entry.get("in_reply_to")),
                        )
                    )
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    rows.append(
                        (
                            _text(entry[0]),
                            _text(entry[1]),
                            _number(entry[2]) if len(entry) > 2 else 0,
                        )
                    )
            result[number] = tuple(rows)
    return result


def _ref_keyed_runs(value: Any) -> Dict[str, Tuple[Tuple[str, str], ...]]:
    result: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    if isinstance(value, Mapping):
        for ref, runs in value.items():
            pairs: List[Tuple[str, str]] = []
            for run in _sequence(runs):
                if isinstance(run, Mapping):
                    pairs.append(
                        (_text(run.get("context")) or "ci", _text(run.get("conclusion")))
                    )
                elif isinstance(run, (list, tuple)) and len(run) >= 2:
                    pairs.append((_text(run[0]), _text(run[1])))
            result[_text(ref)] = tuple(pairs)
    return result


def _int_keyed_lists(value: Any) -> Dict[int, Tuple[str, ...]]:
    result: Dict[int, Tuple[str, ...]] = {}
    if isinstance(value, Mapping):
        for key, items in value.items():
            number = _number(key)
            if number > 0:
                result[number] = tuple(_text(item) for item in _sequence(items))
    return result


def _int_keyed_blockers(value: Any) -> Dict[int, Tuple[Tuple[int, str], ...]]:
    result: Dict[int, Tuple[Tuple[int, str], ...]] = {}
    if isinstance(value, Mapping):
        for key, blockers in value.items():
            number = _number(key)
            if number <= 0:
                continue
            rows: List[Tuple[int, str]] = []
            for blocker in _sequence(blockers):
                if isinstance(blocker, Mapping):
                    rows.append((_number(blocker.get("number")), _text(blocker.get("state")) or "open"))
                elif isinstance(blocker, (list, tuple)) and blocker:
                    rows.append(
                        (
                            _number(blocker[0]),
                            _text(blocker[1]) if len(blocker) > 1 and _text(blocker[1]) else "open",
                        )
                    )
            result[number] = tuple(rows)
    return result


def _int_keyed_threads(value: Any) -> Dict[int, Tuple[Tuple[str, bool, str, int], ...]]:
    result: Dict[int, Tuple[Tuple[str, bool, str, int], ...]] = {}
    if isinstance(value, Mapping):
        for key, threads in value.items():
            number = _number(key)
            if number <= 0:
                continue
            rows: List[Tuple[str, bool, str, int]] = []
            for thread in _sequence(threads):
                if isinstance(thread, Mapping):
                    rows.append(
                        (
                            _text(thread.get("id")),
                            bool(thread.get("resolved")),
                            _text(thread.get("path")),
                            _number(thread.get("line")),
                        )
                    )
                elif isinstance(thread, (list, tuple)) and len(thread) >= 4:
                    rows.append(
                        (
                            _text(thread[0]),
                            bool(thread[1]),
                            _text(thread[2]),
                            _number(thread[3]),
                        )
                    )
            result[number] = tuple(rows)
    return result


def _int_keyed_bools(value: Any) -> Dict[int, bool]:
    result: Dict[int, bool] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            number = _number(key)
            if number > 0:
                result[number] = bool(item)
    return result


def _str_map(value: Any) -> Dict[str, str]:
    if isinstance(value, Mapping):
        return {_text(key): _text(item) for key, item in value.items()}
    return {}


# --------------------------------------------------------------------------- #
# The record-only client
# --------------------------------------------------------------------------- #


class ShadowGitHubClient:
    """Answers reads from a captured snapshot and records writes.

    It presents the same read surface as ``continuum.review.github.GitHubClient``
    and the same write surface, because the production controller does not
    branch on which object it holds. Every mutating method is answered here and
    *only* here; an attribute the shadow client does not implement raises rather
    than returning a plausible empty value.
    """

    def __init__(
        self,
        state: ObservedState,
        effects: RecordingEffects,
        *,
        scenario: str = "",
    ) -> None:
        # Fail before the first decision if a mutating method on the real
        # client has no effect kind. This is the check that makes "shadow mode
        # cannot invoke an unknown mutating adapter" a startup property.
        from continuum.review.github import GitHubClient

        assert_registry_covers(GitHubClient)
        self._state = state
        self._effects = effects
        self.scenario = scenario
        #: What this run has written so far, as reads should see it.
        #:
        #: Production's write is visible to the next read immediately, and the
        #: controller depends on it: it writes the in-flight slot, and if a later
        #: step cannot record it, it rolls the dispatch back. A recorder that
        #: recorded the write and then answered reads from the pre-write snapshot
        #: would break exactly that path, and the rollback it exercises is one of
        #: the things a shadow run exists to validate. So the overlay is not
        #: bookkeeping -- it is the only way the read-after-write behaviour of
        #: the real client is reproduced.
        self._comments: Dict[int, List[str]] = {
            number: list(bodies) for number, bodies in state.issue_comments.items()
        }
        self._labels: Dict[int, set] = {
            pull.number: set(pull.labels) for pull in state.pulls
        }
        self._labels.update({issue.number: set(issue.labels) for issue in state.issues})
        self._check_runs: Dict[str, List[Tuple[str, str]]] = {
            ref: list(runs) for ref, runs in state.check_runs.items()
        }
        self._resolved_threads: Dict[str, str] = {}
        self._comment_ids: Dict[Any, int] = {}
        #: Repair episodes this run has started. A dispatch is not controller
        #: memory in production -- GitHub holds the run -- but in a shadow run
        #: there is no GitHub, so the client has to hold what a second delivery
        #: would observe: the repair slot is in flight.
        self._repair_in_flight: Dict[int, bool] = dict(state.repair_in_flight)
        #: What a recorded comment claims about its author, so a read-back can
        #: present it the way the real client would.
        self._writer = "github-actions[bot]"
        self._created_at = ""

        # The data attributes the real client carries. They are part of its
        # public surface, and the engine reads them: ``gate.py`` publishes the
        # repository into its result with ``getattr(client, "repository", "")``.
        # Answering them from the capture is not a convenience, it is what makes
        # the gate's published result match production's.
        self.repository = state.repository
        self.owner, self.name = state.repository.split("/", 1)
        self.api_base = "shadow://record-only"
        self.timeout = 0

    # -- reads ------------------------------------------------------------

    def get_pull(self, number: int) -> Dict[str, Any]:
        pull = self._state.pull(int(number))
        if pull is None:
            raise _unknown("pull request #{}".format(number))
        return self._with_labels(pull.as_api(), pull.number)

    def list_pulls(self, *, state: str = "open", base: str = "") -> List[Dict[str, Any]]:
        wanted = (state or "open").lower()
        target_base = base or self._state.base_ref
        return [
            pull.as_api()
            for pull in self._state.pulls
            if pull.state == wanted and (not base or pull.base_ref == target_base)
        ]

    def _with_labels(self, api: Dict[str, Any], number: int) -> Dict[str, Any]:
        """The captured view plus whatever labels this run has written."""

        labels = self._labels.get(int(number))
        if labels is None:
            return api
        return dict(api, labels=[{"name": name} for name in sorted(labels)])

    def get_issue(self, number: int) -> Dict[str, Any]:
        issue = self._state.issue(int(number))
        if issue is not None:
            return self._with_labels(issue.as_api(), issue.number)
        pull = self._state.pull(int(number))
        if pull is not None:
            # GitHub serves pull requests from the issues endpoint too, and the
            # repair controller reads labels that way.
            return self._with_labels(
                {
                    "number": pull.number,
                    "state": pull.state,
                    "title": "",
                    "body": pull.body,
                    "user": {"login": "", "type": "User"},
                    "author_association": "",
                },
                pull.number,
            )
        raise _unknown("issue #{}".format(number))

    def list_check_runs(self, ref: str) -> List[Dict[str, Any]]:
        runs = self._runs_for(ref)
        return [
            {"name": context, "status": "completed", "conclusion": conclusion or ""}
            for context, conclusion in runs
        ]

    def _runs_for(self, ref: str) -> Tuple[Tuple[str, str], ...]:
        runs = self._check_runs.get(_text(ref).lower())
        if runs is None:
            raise _unknown("check runs for ref {!r}".format(_text(ref)))
        return tuple(runs)

    def list_pull_files(self, number: int) -> List[Dict[str, Any]]:
        files = self._state.changed_files.get(int(number), ())
        return [{"filename": name} for name in files]

    def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
        bodies = self._comments.get(int(number))
        if bodies is None:
            # Not captured, and not written: the distinction matters, because
            # "this pull request has no comments" is a decision input.
            raise _unknown("comments for issue #{}".format(int(number)))
        return [
            {
                "id": self._comment_id(body),
                "body": body,
                "user": {"login": self._writer},
                "created_at": self._created_at,
            }
            for body in bodies
        ]

    def _comment_id(self, body: str) -> int:
        """A stable local id for a comment body.

        Positional, because a captured comment has no id of its own and the
        number only has to be consistent within a run: a comment written during
        the run gets the next identifier, and a captured one gets its position.
        What matters is that the same body always reads as the same comment, so
        a marker lookup finds the same row it found last time.
        """

        key = (id(self), body)
        existing = self._comment_ids.get(key)
        if existing is None:
            self._comment_ids[key] = self._effects.next_identifier("comment")
            existing = self._comment_ids[key]
        return existing

    def labels_of(self, number: int) -> Tuple[str, ...]:
        """The labels a read would see, including this run's own writes."""

        return tuple(sorted(self._labels.get(int(number), set())))

    def list_review_comments(self, number: int) -> List[Dict[str, Any]]:
        return [
            {"id": self._effects.next_identifier("review-comment"), "user": {"login": login}, "body": body, "in_reply_to_id": reply}
            for login, body, reply in self._state.review_comments.get(int(number), ())
        ]

    def list_reviews(self, number: int) -> List[Dict[str, Any]]:
        return [
            {"id": self._effects.next_identifier("review"), "user": {"login": login}, "state": state, "body": body}
            for login, state, body in self._state.reviews.get(int(number), ())
        ]

    def combined_status_for_ref(self, ref: str) -> Dict[str, Any]:
        key = _text(ref).lower()
        if key not in self._state.combined_status and key not in self._check_runs:
            raise _unknown("combined status for ref {!r}".format(key))
        statuses = [
            {
                "context": context,
                "state": conclusion,
                "status": conclusion,
            }
            for context, conclusion in self._runs_for(key)
        ]
        # The combined state is derived from the statuses the run can see, with
        # the captured value as the starting point. GitHub derives it the same
        # way, and a status written during this run has to be able to change it or
        # the merge controller would read a stale "failure".
        return {
            "state": _combine_status(statuses, self._state.combined_status.get(key, "")),
            "statuses": statuses,
            "total_count": len(statuses),
        }

    def file_at_ref(self, path: str, ref: str) -> Optional[str]:
        # The captured snapshot holds the decision-relevant content only. A
        # shadow run must not read the working tree: that would make the result
        # depend on which Continuum checkout performed it.
        return None

    def review_threads(self, pr_number: int) -> Optional[List[Dict[str, Any]]]:
        threads = self._state.review_threads.get(int(pr_number))
        if threads is None:
            return None
        return [
            {
                "id": identifier,
                "isResolved": self._resolved_threads.get(identifier, ""),
                "path": path,
                "line": line,
            }
            for identifier, resolved, path, line in threads
        ]

    def unresolved_thread_comment_ids(self, pr_number: int) -> Optional[Set[int]]:
        threads = self._state.review_threads.get(int(pr_number))
        if threads is None:
            return None
        return {
            index + 1
            for index, (identifier, resolved, _, _) in enumerate(threads)
            if not resolved and identifier not in self._resolved_threads
        }

    def request(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise _not_implemented("request")

    def paginate(self, *args: Any, **kwargs: Any) -> List[Any]:
        raise _not_implemented("paginate")

    def graphql(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise _not_implemented("graphql")

    # -- writes: recorded, never performed --------------------------------

    def create_issue_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(kind="comment.create", target="issue:{}".format(int(issue_number)), detail={"text": body})
        )
        identifier = self._effects.next_identifier("comment")
        self._comments.setdefault(int(issue_number), []).append(body)
        self._comment_ids[(id(self), body)] = identifier
        return {"id": identifier, "body": body}

    def update_issue_comment(self, comment_id: int, body: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(kind="comment.update", target="comment:{}".format(int(comment_id)), detail={"text": body})
        )
        # The captured snapshot has no comment ids, so an update replaces the
        # body of the comment whose positional id matches. Getting this wrong
        # would make an upsert look like a create, which is precisely the
        # difference the marker-comment path exists to avoid.
        for bodies, _number in self._comment_locations():
            for index, existing in enumerate(list(bodies)):
                if self._comment_id(existing) == int(comment_id):
                    bodies[index] = body
                    return {"id": int(comment_id), "body": body}
        return {"id": int(comment_id), "body": body}

    def _comment_locations(self) -> List[Tuple[List[str], int]]:
        return [(bodies, number) for number, bodies in sorted(self._comments.items())]

    def delete_issue_comment(self, comment_id: int) -> Dict[str, Any]:
        self._effects.record(
            Effect(kind="comment.delete", target="comment:{}".format(int(comment_id)))
        )
        for bodies, _ in self._comment_locations():
            for existing in list(bodies):
                if self._comment_id(existing) == int(comment_id):
                    bodies.remove(existing)
                    break
        return {"id": int(comment_id)}

    def add_labels(self, issue_number: int, labels: Sequence[str]) -> Dict[str, Any]:
        names = sorted(str(label) for label in labels)
        self._effects.record(
            Effect(
                kind="label.add",
                target="issue:{}".format(int(issue_number)),
                detail={"labels": names},
            )
        )
        self._labels.setdefault(int(issue_number), set()).update(names)
        return {"number": int(issue_number)}

    def remove_label(self, issue_number: int, name: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(
                kind="label.remove",
                target="issue:{}".format(int(issue_number)),
                detail={"label": str(name)},
            )
        )
        self._labels.setdefault(int(issue_number), set()).discard(str(name))
        return {"number": int(issue_number)}

    def create_review(self, pr_number: int, head_sha: str, body: str, event: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(
                kind="review.create",
                target="pr:{}".format(int(pr_number)),
                detail={"head": _text(head_sha), "event": _text(event), "text": body},
            )
        )
        return {"id": self._effects.next_identifier("review")}

    def create_status(self, head_sha: str, state: str, description: str, context: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(
                kind="status.create",
                target="ref:{}".format(_text(head_sha)),
                detail={"state": _text(state), "context": _text(context), "text": description},
            )
        )
        # A status written during the run is visible to the next read. The review
        # gate writes ``CodeRabbit`` and then the merge controller reads it, and
        # a recorder that could not answer that read would make the coverage
        # check look like a permanent gap.
        key = _text(head_sha).lower()
        runs = self._check_runs.setdefault(key, [])
        for index, (existing, _conclusion) in enumerate(runs):
            if existing == _text(context):
                runs[index] = (existing, _text(state))
                break
        else:
            runs.insert(0, (_text(context), _text(state)))
        return {"id": self._effects.next_identifier("status")}

    def resolve_review_thread(self, thread_id: str) -> Dict[str, Any]:
        self._effects.record(
            Effect(kind="thread.resolve", target="thread:{}".format(_text(thread_id)))
        )
        self._resolved_threads[_text(thread_id)] = "resolved"
        return {"id": _text(thread_id), "isResolved": True}

    def dispatch_workflow(self, workflow: str, ref: str, inputs: Mapping[str, str]) -> Dict[str, Any]:
        self._effects.record(
            Effect(
                kind="workflow.dispatch",
                target="workflow:{}".format(_text(workflow)),
                detail={
                    "ref": _text(ref),
                    "inputs": {str(key): str(value) for key, value in sorted(inputs.items())},
                },
            )
        )
        # A dispatch is a write the shadow has to observe: GitHub would now have
        # a workflow run in flight, and a re-delivered event would see it. The
        # pr_number travels in the inputs, which is how the reconcile path names
        # the slot in production.
        number = _text(inputs.get("pr_number", "")).strip()
        if number.isdigit():
            self._repair_in_flight[int(number)] = True
        return {"status": "recorded"}

    def repair_in_flight(self, number: int) -> bool:
        """Whether the repair slot for a pull request is in flight, as a read
        would see it -- including a dispatch this run recorded."""

        return bool(self._repair_in_flight.get(int(number), False))

    def mark_repair_dispatched(self, number: int) -> None:
        """Record that this run sent a repair dispatch for a pull request.

        The planner records the ``workflow.dispatch`` effect itself -- the
        effect is journal evidence -- but the slot a second delivery would
        observe is this client's, and only a dispatch the planner actually sent
        may fill it.
        """

        self._repair_in_flight[int(number)] = True

    def upsert_marker_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        state: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        rendered = body if not state else "{}\n\n```json\n{}\n```".format(body, _json_state(state))
        bodies = self._comments.get(int(issue_number), [])
        existing = _marker_comment_id(bodies, marker)
        existing_id = self._comment_id(bodies[existing - 1]) if existing else None
        if existing is None:
            return self.create_issue_comment(int(issue_number), rendered)
        self._effects.record(
            Effect(
                kind="comment.update",
                target="comment:{}".format(existing_id),
                detail={"marker": marker, "text": rendered},
            )
        )
        bodies[existing - 1] = rendered
        return {"id": existing_id, "body": rendered}

    # -- fail closed -------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        raise _not_implemented(name)


def _marker_comment_id(bodies: Sequence[str], marker: str) -> Optional[int]:
    """Find the comment carrying ``marker``.

    The captured snapshot stores comment bodies without ids, so a marker
    comment is identified positionally. A shadow run that created a *new*
    marker comment where production would have updated one would record a
    different action, so the lookup is on the marker text and the identifier is
    the position -- stable for a given capture.
    """

    for index, body in enumerate(bodies):
        if marker and marker in body:
            return index + 1
    return None


def _json_state(state: Mapping[str, Any]) -> str:
    import json

    return json.dumps(state, sort_keys=True, separators=(",", ":"))


class _Unknown(StateError):
    """The captured state has no answer, and guessing would be a decision.

    A :class:`StateError` rather than a bare exception so the planner reports it
    as an unusable *capture* rather than as a planner crash. Those are different
    findings with different owners: a crash points at the shadow code, an
    incomplete capture points at the bridge that produced it.
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            "capture_incomplete",
            "the captured state has no {}; a shadow run never guesses".format(what),
        )
        self.what = what


def _unknown(what: str) -> _Unknown:
    return _Unknown(what)


def _not_implemented(name: str) -> WriteBarrierViolation:
    return WriteBarrierViolation(
        "the record-only client has no {!r}; a shadow run must not reach "
        "past its read surface".format(name)
    )


def _combine_status(statuses: Sequence[Mapping[str, Any]], captured: str) -> str:
    """GitHub's combined status, derived the way GitHub derives it.

    Re-implemented rather than hard-coded because the merge controller reads
    this, and a hard-coded captured value would go stale the moment this run
    wrote a status of its own -- which is exactly what the review gate does
    before the merge controller runs.
    """

    conclusions = [_text(item.get("state") or item.get("conclusion")) for item in statuses]
    if not conclusions:
        return captured
    if any(value in ("failure", "error", "timed_out", "action_required") for value in conclusions):
        return "failure"
    if any(value == "cancelled" for value in conclusions):
        return "failure"
    if all(value in ("success", "neutral", "skipped") for value in conclusions):
        return "success"
    return "pending"
