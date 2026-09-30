"""Live capture: read the state a shadow decision needs, and nothing else.

The issue is strict about this half. Shadow mode must not be "a simplified
scheduler fed synthetic data", and it must not be a decision made against
whatever the API said five minutes later. So the capture is:

* **Real.** Every field comes from the same read surface the production controller
  uses, on the same pull requests, in the same run. A shadow decision is the
  production decision, computed from production's own view of the world.

* **Read-only by construction.** Only the methods in
  ``effects.READ_ONLY_METHODS`` are called, and the method name is checked
  against that allowlist at the call site rather than trusted. Reads are not
  writes, but a capture that could reach a mutating method would be a hole in the
  barrier, so the check is at the point of use.

* **Complete or visibly incomplete.** A field the capture could not read is
  recorded as unread rather than defaulted. A guard that consults an unread
  field fails closed and says so, because the alternative -- a default that
  happens to permit a merge -- is the failure this whole plane exists to prevent.

Nothing here writes to GitHub, and nothing here decides. It builds the
``ObservedState`` the planner already knows how to read, and it records which
fields were unreadable so the journal can say so.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .effects import READ_ONLY_METHODS
from .event import ShadowEvent
from .state import IssueView, ObservedState, PullView, StateError

#: The most pull requests a single capture will walk. A bound, not a
#: preference: a shadow run that tried to capture an entire repository's open
#: pull requests would spend its budget on state no decision in this event
#: consults, and would be reported as slow for it.
MAX_PULLS = 50

#: The same bound for the issue queue, for the same reason: the queue decides
#: among the open issues, so it reads them, but only as many as a decision could
#: reasonably consider before it is slow rather than live.
MAX_ISSUES = 50


class CaptureError(RuntimeError):
    """The state could not be captured."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass
class _Capture:
    """Mutable accumulator, because a capture is a series of reads."""

    repository: str
    base_ref: str
    owner: str
    captured_at_ms: int
    pulls: Dict[int, PullView] = dataclasses.field(default_factory=dict)
    issues: Dict[int, IssueView] = dataclasses.field(default_factory=dict)
    issue_comments: Dict[int, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    reviews: Dict[int, Tuple[Tuple[str, str, str], ...]] = dataclasses.field(default_factory=dict)
    review_comments: Dict[int, Tuple[Tuple[str, str, int], ...]] = dataclasses.field(default_factory=dict)
    check_runs: Dict[str, Tuple[Tuple[str, str], ...]] = dataclasses.field(default_factory=dict)
    combined_status: Dict[str, str] = dataclasses.field(default_factory=dict)
    changed_files: Dict[int, Tuple[str, ...]] = dataclasses.field(default_factory=dict)
    review_threads: Dict[int, Tuple[Tuple[str, bool, str, int], ...]] = dataclasses.field(default_factory=dict)
    repair_in_flight: Dict[int, bool] = dataclasses.field(default_factory=dict)
    blocked_by: Dict[int, Tuple[Tuple[int, str], ...]] = dataclasses.field(default_factory=dict)
    release_commit: str = ""
    #: Field name -> why it could not be read. Surfaced in the state, so a
    #: decision made on a partial capture is recognisable as one.
    unreadable: Dict[str, str] = dataclasses.field(default_factory=dict)
    trusted_actors: str = ""

    def note(self, field: str, reason: str) -> None:
        self.unreadable.setdefault(field, reason)

    def build(self) -> ObservedState:
        return ObservedState(
            repository=self.repository,
            base_ref=self.base_ref,
            repository_owner=self.owner,
            trusted_actors=self.trusted_actors,
            pulls=tuple(self.pulls[key] for key in sorted(self.pulls)),
            issues=tuple(self.issues[key] for key in sorted(self.issues)),
            issue_comments=dict(self.issue_comments),
            reviews=dict(self.reviews),
            review_comments=dict(self.review_comments),
            check_runs=dict(self.check_runs),
            combined_status=dict(self.combined_status),
            changed_files=dict(self.changed_files),
            review_threads=dict(self.review_threads),
            repair_in_flight=dict(self.repair_in_flight),
            blocked_by=dict(self.blocked_by),
            release_commit=self.release_commit,
            captured_at_ms=self.captured_at_ms,
            unreadable=dict(self.unreadable),
        )


def _read(client: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a read method, having checked that it is on the read allowlist.

    The check is here rather than at import time because the whole point of the
    allowlist is that a *new* method is mutating until proved otherwise, and a
    method is only new when something calls it.
    """

    if name not in READ_ONLY_METHODS:
        raise CaptureError(
            "write_capable_read",
            "{} is not on the read-only allowlist, so a shadow capture may not "
            "call it.".format(name),
        )
    method = getattr(client, name, None)
    if method is None:
        raise CaptureError("missing_read_method", "The client has no {}.".format(name))
    return method(*args, **kwargs)


def capture_state(
    client: Any,
    event: ShadowEvent,
    *,
    captured_at_ms: int = 0,
    trusted_actors: str = "",
    extra_pulls: Sequence[int] = (),
) -> ObservedState:
    """Capture the state ``event``'s decision will be made against.

    ``client`` is a ``continuum.review.github.GitHubClient`` holding a token with
    read-only permission. The shadow workflow must supply that token rather than
    a write-capable one, so the barrier is a second line of defence rather than
    the only one -- but the capture is written to be correct on its own, because a
    second line of defence that is also the first is not a defence.
    """

    if not event.repository:
        raise CaptureError("no_repository", "The event names no repository to capture.")
    owner, _, name = event.repository.partition("/")
    if not owner or not name:
        raise CaptureError(
            "malformed_repository",
            "{!r} is not an owner/name repository.".format(event.repository),
        )

    capture = _Capture(
        repository=event.repository,
        base_ref=event.base_ref or "main",
        owner=owner,
        captured_at_ms=int(captured_at_ms),
        trusted_actors=trusted_actors,
    )

    # Which pull requests the decision can look at: the event's own, plus the
    # open ones when the event names neither a number nor a head to find them by.
    numbers: List[int] = []
    for candidate in (event.number, *extra_pulls):
        if candidate and candidate not in numbers:
            numbers.append(int(candidate))
    if not numbers:
        numbers = _pull_numbers_for(client, capture, event)
    for number in numbers[:MAX_PULLS]:
        try:
            capture.pulls[number] = _pull_view(client, number, capture)
        except Exception as error:  # noqa: BLE001
            # An issue number that is not a pull request is not an unreadable
            # field: it is the answer, and the client says so with a 404 that
            # would otherwise end the whole capture. Only a failure that is *not*
            # "there is no pull request here" is recorded, because marking an
            # ordinary issue as an unreadable pull request would make every
            # issue-lifecycle capture look incomplete and every merge guard refuse
            # for the wrong reason.
            if not _is_absent(error):
                capture.note("pull:{}".format(number), str(error))
        if number not in capture.issues:
            issue = _issue_view(client, number, capture)
            if issue is not None:
                capture.issues[number] = issue

    # An issue-only event still needs the issue the queue acts on.
    if event.event_type.startswith("issue.") and event.number:
        issue = _issue_view(client, event.number, capture)
        if issue is not None:
            capture.issues[event.number] = issue
    if not capture.issues:
        capture.issues = _open_issues(client, capture)

    capture.release_commit = _release_commit(client, capture, event)

    return capture.build()


#: A tag is a name, and a release contract needs a commit. This is the smallest
#: query that turns one into the other, written for a *tag*: for an annotated
#: tag the ref points at the tag object, whose own ``target`` is the commit.
_RELEASE_TAG_QUERY = """
query ShadowTag($owner: String!, $name: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) {
      target {
        oid
        ... on Tag { target { oid } }
      }
    }
  }
}
"""


def _release_commit(client: Any, capture: _Capture, event: ShadowEvent) -> str:
    """Resolve a release tag to the commit it names, or record that it could not.

    A ``release`` webhook carries a tag and a branch, never a commit, and the
    release contract refuses to plan a release that is not pinned to one. So a
    shadow release event that reached the planner unresolved would report
    "unpinned" for every release in the window, which says nothing about
    Continuum and looks like a bug in the release path. The tag is resolved here
    instead, from the same read surface, and a tag that does not resolve is
    recorded as unreadable so the refusal names the real reason.
    """

    if event.event_type != "release.event":
        return ""
    release = event.release if isinstance(event.release, Mapping) else {}
    if str(release.get("source_sha") or "") or str(release.get("tag") or "") == "":
        return ""
    tag = str(release.get("tag") or "")
    if not tag.startswith(("refs/tags/", "v", "release/")):
        # A tag the contract could not name anyway; recorded rather than queried
        # so the window says the tag was unusable.
        capture.note("release_commit", "release tag {!r} is not a tag".format(tag))
        return ""
    try:
        payload = _read(
            client,
            "graphql",
            _RELEASE_TAG_QUERY,
            {
                "owner": capture.owner,
                "name": capture.repository.partition("/")[2],
                "ref": tag if tag.startswith("refs/tags/") else "refs/tags/" + tag,
            },
        )
    except Exception as error:  # noqa: BLE001
        capture.note("release_commit", "could not resolve {}: {}".format(tag, error))
        return ""
    target = ((payload.get("repository") or {}).get("ref") or {}).get("target")
    commit = ""
    if isinstance(target, Mapping):
        nested = target.get("target")
        commit = str(
            (nested.get("oid") if isinstance(nested, Mapping) else "")
            or target.get("oid")
            or ""
        )
    if not commit:
        capture.note("release_commit", "tag {} does not resolve to a commit".format(tag))
    return commit


def _pull_numbers_for(client: Any, capture: _Capture, event: ShadowEvent) -> List[int]:
    """Find the pull requests an event that names no number is about.

    Two cases, and the difference is what the event carries:

    * **A head.** A red CI run on a push names the commit, not a pull request.
      The pull request it is about is the open one whose head is that commit, so
      the listing is filtered to it. A push with no pull request is an ordinary
      thing that happens, and finding nothing is the answer rather than a gap in
      the capture.
    * **Neither.** A schedule tick is a merge decision over the whole open
      queue, so every open pull request is in scope, up to the bound.

    There is deliberately no third case keyed on the event type: an event that
    names a number already knows its pull request, and one that names a head
    finds it by that head, so a list of "these event types are about the queue"
    would be a claim the engine could silently stop honouring.
    """

    listing = _read(client, "list_pulls", state="open", base=capture.base_ref)
    numbers: List[int] = []
    for item in listing[:MAX_PULLS]:
        if not isinstance(item, Mapping) or not item.get("number"):
            continue
        if event.head_sha:
            head = item.get("head") or {}
            if not isinstance(head, Mapping) or str(head.get("sha") or "") != event.head_sha:
                continue
        numbers.append(int(item["number"]))
    return numbers


def _is_absent(error: BaseException) -> bool:
    """Whether a failed read means "there is nothing there" rather than "unknown".

    GitHub answers 404 for an issue number that is not a pull request, and a
    capture that recorded that as an unreadable field would be reporting a
    question with an answer. Anything else -- a 403, a 5xx, a timeout -- is a
    read that failed, and unknown is not the same as absent.
    """

    if getattr(error, "status", None) == 404:
        return True
    text = str(error).lower()
    return "404" in text or "not found" in text


def _pull_view(client: Any, number: int, capture: _Capture) -> PullView:
    payload = _read(client, "get_pull", number)
    if not isinstance(payload, Mapping):
        raise CaptureError("unexpected_pull_payload", "get_pull returned {!r}".format(type(payload)))

    head = payload.get("head") or {}
    base = payload.get("base") or {}
    capture.issue_comments[number] = tuple(
        str(item.get("body", ""))
        for item in _read(client, "list_issue_comments", number)
        if isinstance(item, Mapping)
    )
    capture.reviews[number] = tuple(
        (
            str((item.get("user") or {}).get("login", "")),
            str(item.get("state", "")).upper(),
            str(item.get("body") or ""),
        )
        for item in _read(client, "list_reviews", number)
        if isinstance(item, Mapping)
    )
    capture.review_comments[number] = tuple(
        (
            str((item.get("user") or {}).get("login", "")),
            str(item.get("body") or ""),
            int(item.get("in_reply_to_id") or 0),
        )
        for item in _read(client, "list_review_comments", number)
        if isinstance(item, Mapping)
    )
    capture.changed_files[number] = tuple(
        str(item.get("filename", ""))
        for item in _read(client, "list_pull_files", number)
        if isinstance(item, Mapping)
    )

    head_sha = str(head.get("sha") or "")
    if head_sha:
        try:
            capture.check_runs[head_sha] = tuple(
                (
                    str(item.get("name", "")),
                    _conclusion(item),
                )
                for item in _read(client, "list_check_runs", head_sha)
                if isinstance(item, Mapping)
            )
        except Exception as error:  # noqa: BLE001 - recorded, never guessed
            capture.note("check_runs:{}".format(head_sha[:12]), str(error))
        try:
            status = _read(client, "combined_status_for_ref", head_sha)
            capture.combined_status[head_sha] = str((status or {}).get("state", ""))
        except Exception as error:  # noqa: BLE001
            capture.note("combined_status:{}".format(head_sha[:12]), str(error))

    threads = _threads(client, number, capture)
    if threads is not None:
        capture.review_threads[number] = threads

    return PullView(
        number=number,
        state=str(payload.get("state", "")),
        draft=bool(payload.get("draft", False)),
        head_ref=str(head.get("ref") or ""),
        head_sha=head_sha,
        base_ref=str(base.get("ref") or capture.base_ref),
        base_sha=str(base.get("sha") or ""),
        body=str(payload.get("body") or ""),
        head_repository=str((head.get("repo") or {}).get("full_name") or ""),
        base_repository=str((base.get("repo") or {}).get("full_name") or ""),
        labels=_labels(payload.get("labels")),
        mergeable=str(payload.get("mergeable") or ""),
        mergeable_state=str(payload.get("mergeable_state") or ""),
        review_decision=str((payload.get("review_decision") or "")),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def _issue_view(client: Any, number: int, capture: _Capture) -> Optional[IssueView]:
    """An issue view from the same number.

    A pull request is an issue as far as the API is concerned, so the issue list
    gets every pull request too; the queue only reads the ones it can label.
    """

    try:
        payload = _read(client, "get_issue", number)
    except Exception as error:  # noqa: BLE001
        capture.note("issue:{}".format(number), str(error))
        return None
    if not isinstance(payload, Mapping):
        return None
    return IssueView(
        number=number,
        state=str(payload.get("state", "")),
        author=str((payload.get("user") or {}).get("login", "")),
        author_association=str(payload.get("author_association") or ""),
        title=str(payload.get("title", "")),
        body=str(payload.get("body") or ""),
        labels=_labels(payload.get("labels")),
        updated_at=str(payload.get("updated_at") or ""),
    )


def _open_issues(client: Any, capture: _Capture) -> Dict[int, IssueView]:
    """The open issues the queue could pick up.

    Read from the issues endpoint rather than from ``list_pulls``: the queue's
    work is ordinary issues, and a list of open pull requests is a different set
    of numbers entirely. The issues endpoint also returns pull requests, because
    in GitHub's data model a pull request *is* an issue, so entries carrying a
    ``pull_request`` key are dropped rather than counted as queue work.
    """

    issues: Dict[int, IssueView] = {}
    path = "/repos/{}/issues?state=open&per_page=100".format(
        capture.repository.replace("/", "%2F")
    )
    try:
        items = _read(client, "paginate", path)
    except Exception as error:  # noqa: BLE001
        capture.note("open_issues", str(error))
        return issues
    for item in items[:MAX_ISSUES]:
        if not isinstance(item, Mapping) or item.get("pull_request"):
            continue
        number = int(item.get("number") or 0)
        if not number:
            continue
        issue = _issue_view(client, number, capture)
        if issue is not None:
            issues[number] = issue
    return issues


def _threads(
    client: Any, number: int, capture: _Capture
) -> Optional[Tuple[Tuple[str, bool, str, int], ...]]:
    """Review threads, or ``None`` when the client could not tell us.

    ``None`` means *unknown*, and unknown is not the same as "no unresolved
    threads": the repair path refuses to act when it cannot see the threads, and
    that refusal is the point.
    """

    try:
        threads = _read(client, "review_threads", number)
    except Exception as error:  # noqa: BLE001
        capture.note("review_threads:{}".format(number), str(error))
        return None
    if threads is None:
        capture.note("review_threads:{}".format(number), "the client reported unknown threads")
        return None
    return tuple(
        (
            str(item.get("id", "")),
            bool(item.get("isResolved", item.get("is_resolved", False))),
            str(item.get("path", "")),
            int(item.get("line") or item.get("original_line") or 0),
        )
        for item in threads
        if isinstance(item, Mapping)
    )


def _labels(value: Any) -> Tuple[str, ...]:
    return tuple(
        sorted(
            str((item.get("name") if isinstance(item, Mapping) else item) or "")
            for item in value or []
        )
    )


def _conclusion(item: Mapping[str, Any]) -> str:
    """A check run's outcome, from whichever field this run populated.

    A check run that is still queued has no conclusion. Recording ``pending``
    rather than ``success`` is deliberate in the other direction: an unfinished
    check is not green, and a merge guard that treated it as green would merge
    unreviewed work.
    """

    status = str(item.get("status") or "")
    conclusion = str(item.get("conclusion") or "")
    if conclusion:
        return conclusion
    return {"queued": "pending", "in_progress": "pending", "waiting": "pending"}.get(
        status, status or "pending"
    )


def unreadable_fields(state: ObservedState) -> Dict[str, str]:
    """What the capture could not read, for the journal's diagnostics."""

    return dict(getattr(state, "unreadable", {}) or {})


def describe_capture(state: ObservedState) -> Dict[str, Any]:
    """A short record of what a capture covered, for the job summary."""

    return {
        "repository": state.repository,
        "base_ref": state.base_ref,
        "captured_at_ms": state.captured_at_ms,
        "pulls": len(state.pulls),
        "issues": len(state.issues),
        "review_threads": len(state.review_threads),
        "unreadable": unreadable_fields(state),
    }
