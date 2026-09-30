"""The event that arrives from NanoDictate, normalized into something a decision
can be reproduced from.

The bridge in ``reference/nanodictate-shadow-bridge/`` is deliberately thin: it
forwards an event and the state that was true when the event fired, and nothing
else. It does not decide anything, because a bridge that decided anything would
make the shadow comparison a comparison of two implementations of the *bridge*
rather than of Continuum's decision path.

So everything a decision can depend on has to be in the payload, and everything
in the payload has to be enough to reproduce the decision months later against a
different Continuum. That is the whole design constraint here, and it is why
this module is strict:

* an event with no correlation id, no repository, or no event type is refused,
  not defaulted. A journal keyed on a guess cannot be compared to anything.
* the observed state is a *snapshot*, not a pointer. Nothing in a journal may
  require re-reading GitHub to be understood, because GitHub will have moved.
* the correlation id is derived deterministically from the identifying fields
  when the bridge does not supply one, so a replayed event and its original
  land on the same key. A duplicated webhook and a genuine re-decision then
  become distinguishable, which is the only way the idempotency scenario in the
  issue can be tested for real.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

EVENT_SCHEMA = "continuum.shadow-event/v1"

#: The lifecycle events Continuum knows how to shadow. A closed list, for the
#: same reason the effect vocabulary is closed: an event nobody reasoned about
#: must fail closed rather than be shadowed by a default path.
TYPE_ISSUE_OPENED = "issue.opened"
TYPE_ISSUE_LABELLED = "issue.labelled"
TYPE_ISSUE_CLOSED = "issue.closed"
TYPE_PR_OPENED = "pull_request.opened"
TYPE_PR_SYNCHRONIZE = "pull_request.synchronize"
TYPE_PR_REVIEW = "pull_request.review"
TYPE_PR_REVIEW_COMMENT = "pull_request.review_comment"
TYPE_CI_COMPLETED = "check_suite.completed"
TYPE_WORKFLOW_RUN_COMPLETED = "workflow_run.completed"
TYPE_RELEASE_EVENT = "release.event"
TYPE_DUPLICATE = "duplicate.replay"
TYPE_TIMEOUT = "timeout.detected"
TYPE_SCHEDULE = "schedule.tick"

EVENT_TYPES: Tuple[str, ...] = (
    TYPE_ISSUE_OPENED,
    TYPE_ISSUE_LABELLED,
    TYPE_ISSUE_CLOSED,
    TYPE_PR_OPENED,
    TYPE_PR_SYNCHRONIZE,
    TYPE_PR_REVIEW,
    TYPE_PR_REVIEW_COMMENT,
    TYPE_CI_COMPLETED,
    TYPE_WORKFLOW_RUN_COMPLETED,
    TYPE_RELEASE_EVENT,
    TYPE_DUPLICATE,
    TYPE_TIMEOUT,
    TYPE_SCHEDULE,
)

#: Events that are not about one pull request head.
#:
#: A head sha is required for everything else, because the trust policy, the
#: review gate, the repair ladder, and the merge controller all decide against a
#: specific head and a journal keyed on a headless event could not be replayed
#: against it. These are repository- or issue-scoped -- a release request, a
#: merge sweep, a redelivery, a watchdog timeout, an ordinary issue -- and
#: demanding a sha for them would mean a bridge that had none could not report
#: the event at all, which is worse than reporting it with the head it does not
#: have.
#:
#: An ``issue`` webhook is in this set even though a pull request is an issue,
#: because ``issues`` is GitHub's own name for the event: it fires for issues
#: with no pull request behind them, and the bridge cannot invent a head for
#: those. ``issues`` on a pull request resolves to its head from the capture.
REPOSITORY_SCOPED_EVENTS: Tuple[str, ...] = (
    TYPE_RELEASE_EVENT,
    TYPE_DUPLICATE,
    TYPE_TIMEOUT,
    TYPE_SCHEDULE,
    TYPE_ISSUE_OPENED,
    TYPE_ISSUE_CLOSED,
    TYPE_ISSUE_LABELLED,
)

#: Which scenario class each event type belongs to. The mapping is the reason
#: the type list is closed: coverage is reported per scenario class, and a new
#: event type with no class would be a hole in that report.
SCENARIO_BY_EVENT: Dict[str, str] = {
    TYPE_ISSUE_OPENED: "issue-lifecycle",
    TYPE_ISSUE_LABELLED: "dependency-blocked",
    TYPE_ISSUE_CLOSED: "issue-lifecycle",
    TYPE_PR_OPENED: "issue-lifecycle",
    TYPE_PR_SYNCHRONIZE: "issue-lifecycle",
    TYPE_PR_REVIEW: "coderabbit-finding",
    TYPE_PR_REVIEW_COMMENT: "coderabbit-finding",
    TYPE_CI_COMPLETED: "ci-repair",
    TYPE_WORKFLOW_RUN_COMPLETED: "ci-repair",
    TYPE_RELEASE_EVENT: "release-planning",
    TYPE_DUPLICATE: "duplicate-replay",
    TYPE_TIMEOUT: "timeout-recovery",
    TYPE_SCHEDULE: "merge-decision",
}

#: A full commit id, or nothing. An abbreviated sha cannot identify a state, and
#: a journal keyed on one cannot be compared to a real outcome.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: ``owner/name`` with nothing surprising in either half.
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]{1,39}/[A-Za-z0-9._-]{1,100}$")

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")


class EventError(ValueError):
    """The payload is not an event a shadow run can act on.

    Carries a stable ``code`` so a bridge can report why an event was dropped
    without a journal, and so the drop is visible in a parity report instead of
    being a silently missing row.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ShadowEvent:
    """One real NanoDictate lifecycle event, normalized and immutable.

    Every field here is something a decision can depend on, and nothing here is
    something only the bridge understood. The journal writes all of it, so a
    reader can reconstruct the decision inputs without the bridge.
    """

    #: Stable key for this event. Identical events share one, which is what
    #: makes a duplicate observable.
    correlation_id: str
    #: The event that just arrived, if this is a re-delivery. Empty otherwise.
    replays: str = ""
    repository: str = ""
    event_type: str = ""
    action: str = ""
    number: Optional[int] = None
    head_sha: str = ""
    base_sha: str = ""
    base_ref: str = "main"
    #: Labels on the issue or pull request at event time, sorted.
    labels: Tuple[str, ...] = ()
    #: Source issue, when the pull request names one.
    source_issue: Optional[int] = None
    #: Normalized check/review state, as a mapping of context to conclusion.
    checks: Mapping[str, str] = dataclasses.field(default_factory=dict)
    reviews: Mapping[str, str] = dataclasses.field(default_factory=dict)
    #: Release context, when the event is a release event.
    release: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    #: Who or what produced the event. Recorded, never trusted.
    actor: str = ""
    #: ISO-8601, as reported by the bridge. Never generated locally: a shadow
    #: run that invented its own clock would make a replay disagree with the
    #: original for a reason that has nothing to do with the decision.
    observed_at: str = ""
    #: Milliseconds since the epoch, for liveness. Zero when unknown, which
    #: liveness treats as unmeasurable rather than instant.
    observed_at_ms: int = 0
    #: Free-form sanitized payload the planner may need. Kept last so the
    #: common fields stay readable in a journal.
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def scenario(self) -> str:
        return SCENARIO_BY_EVENT.get(self.event_type, "unclassified")

    @property
    def is_replay(self) -> bool:
        return bool(self.replays)

    def identity(self) -> Tuple[str, str, str, str]:
        """The fields that make two deliveries of the same event the same event."""

        return (
            self.repository,
            self.event_type,
            self.action,
            self.head_sha,
        )

    def with_release_source(self, commit: str) -> "ShadowEvent":
        """The same event, pinned to the commit its release tag names.

        A release webhook names a tag, and a release contract needs a commit, so
        the capture resolves one into the other. The event stays immutable: this
        returns a copy, and only the release mapping changes. An event that is
        not a release event, or that already names a commit, is returned as it
        was -- a pin the planner does not need is a field that could disagree
        with the one it does.
        """

        if self.event_type != TYPE_RELEASE_EVENT or not commit:
            return self
        if str(self.release.get("source_sha") or ""):
            return self
        release = dict(self.release)
        release["source_sha"] = str(commit).lower()
        return dataclasses.replace(self, release=release)

    def target(self) -> str:
        """The effect target this event acts on, in the shared vocabulary."""

        if self.number is None:
            return "repository:{}".format(self.repository)
        if self.event_type.startswith("issue."):
            return "issue:{}".format(self.number)
        if self.event_type.startswith("pull_request.") or self.event_type.startswith(
            "check_suite."
        ):
            return "pr:{}".format(self.number)
        if self.event_type == "workflow_run.completed":
            return "pr:{}".format(self.number) if self.number else "repository:{}".format(
                self.repository
            )
        return "repository:{}".format(self.repository)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "correlation_id": self.correlation_id,
            "replays": self.replays,
            "repository": self.repository,
            "event_type": self.event_type,
            "action": self.action,
            "number": self.number,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "base_ref": self.base_ref,
            "labels": list(self.labels),
            "source_issue": self.source_issue,
            "checks": {key: self.checks[key] for key in sorted(self.checks)},
            "reviews": {key: self.reviews[key] for key in sorted(self.reviews)},
            "release": {key: _scalar(self.release[key]) for key in sorted(self.release)},
            "actor": self.actor,
            "observed_at": self.observed_at,
            "observed_at_ms": self.observed_at_ms,
            "scenario": self.scenario,
            "extra": {key: _scalar(self.extra[key]) for key in sorted(self.extra)},
        }


def _scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _scalar(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in value]
    return str(value)


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


def derive_correlation_id(
    repository: str,
    event_type: str,
    action: str,
    number: Optional[int],
    head_sha: str,
    nonce: str = "",
) -> str:
    """A deterministic key for an event.

    The nonce is what makes this usable. Without it, every ``synchronize`` on
    the same head is one key -- good for spotting a duplicate delivery, bad for
    distinguishing a later decision about the same head. The bridge therefore
    supplies the run attempt or delivery id when it has one, and only falls back
    to the bare identity when it does not.
    """

    material = "|".join(
        (
            repository,
            event_type,
            action,
            "" if number is None else str(number),
            head_sha,
            nonce,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "{}-{}".format(_short_kind(event_type), digest[:16])


def _short_kind(event_type: str) -> str:
    return event_type.replace(".", "-").replace("_", "-")


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def from_payload(payload: Mapping[str, Any]) -> ShadowEvent:
    """Normalize a bridge payload into a :class:`ShadowEvent`.

    Fail-closed on everything the decision path would otherwise have to guess
    about. A payload missing the head sha is not a payload with an empty head
    sha: a decision that depends on the head cannot be reproduced without it, and
    a journal that pretended otherwise would be a journal of a different event.
    """

    if not isinstance(payload, Mapping):
        raise EventError("payload_not_a_mapping", "Event payload must be an object.")

    repository = _text(payload.get("repository"))
    if not REPOSITORY_RE.match(repository):
        raise EventError(
            "invalid_repository",
            "repository {!r} is not an owner/name pair.".format(repository),
        )

    event_type = _text(payload.get("event_type"))
    if event_type not in EVENT_TYPES:
        raise EventError(
            "unknown_event_type",
            "event_type {!r} is not one of the shadowable lifecycle "
            "events.".format(event_type),
        )

    number = _optional_number(payload.get("number"))
    head_sha = _sha(
        payload.get("head_sha"),
        required=event_type not in REPOSITORY_SCOPED_EVENTS,
        field="head_sha",
    )
    base_sha = _sha(payload.get("base_sha"), required=False, field="base_sha")

    base_ref = _text(payload.get("base_ref")) or "main"
    if not base_ref or ".." in base_ref or base_ref.startswith("-"):
        raise EventError("invalid_base_ref", "base_ref {!r} is not a ref.".format(base_ref))

    labels = tuple(sorted({_text(label) for label in _sequence(payload.get("labels")) if _text(label)}))
    checks = _string_map(payload.get("checks"))
    reviews = _string_map(payload.get("reviews"))
    release = _mapping(payload.get("release"))
    extra = _mapping(payload.get("extra"))

    source_issue = _optional_number(payload.get("source_issue"))
    if source_issue is None and number is not None and event_type.startswith("pull_request."):
        source_issue = _issue_from_branch(_text(payload.get("head_ref")), _text(payload.get("body")))

    replays = _text(payload.get("replays"))
    if replays and not _CORRELATION_RE.match(replays):
        raise EventError("invalid_replays", "replays {!r} is not an id.".format(replays))

    correlation = _text(payload.get("correlation_id"))
    if not correlation:
        correlation = derive_correlation_id(
            repository,
            event_type,
            _text(payload.get("action")),
            number,
            head_sha,
            _text(payload.get("nonce")),
        )
    elif not _CORRELATION_RE.match(correlation):
        raise EventError(
            "invalid_correlation_id",
            "correlation_id {!r} is not a usable id.".format(correlation),
        )

    return ShadowEvent(
        correlation_id=correlation,
        replays=replays,
        repository=repository,
        event_type=event_type,
        action=_text(payload.get("action")),
        number=number,
        head_sha=head_sha,
        base_sha=base_sha,
        base_ref=base_ref,
        labels=labels,
        source_issue=source_issue,
        checks=checks,
        reviews=reviews,
        release=release,
        actor=_text(payload.get("actor")),
        observed_at=_text(payload.get("observed_at")),
        observed_at_ms=_number(payload.get("observed_at_ms"), 0),
        extra=extra,
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _text(item) for key, item in value.items()}


def _number(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,18}", value.strip()):
        return int(value.strip())
    return default


def _optional_number(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    number = _number(value, 0)
    return number if number > 0 else None


def _sha(value: Any, *, required: bool, field: str) -> str:
    text = _text(value).lower()
    if not text:
        if required:
            raise EventError("missing_{}".format(field), "{} is required.".format(field))
        return ""
    if not SHA_RE.match(text):
        raise EventError(
            "invalid_{}".format(field),
            "{!r} is not a full 40-character commit id.".format(text),
        )
    return text


def _issue_from_branch(head_ref: str, body: str) -> Optional[int]:
    """Recover the source issue the way the production controllers do."""

    match = re.match(r"^opencode/issue([0-9]+)-", head_ref or "")
    if match:
        number = int(match.group(1))
        return number if number > 0 else None
    closing = re.search(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([0-9]+)", body or "", re.I)
    if closing:
        return int(closing.group(1))
    return None


def from_github_event(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    repository: str = "",
    base_ref: str = "main",
    observed_at: str = "",
    observed_at_ms: int = 0,
    nonce: str = "",
) -> ShadowEvent:
    """Build a shadow event from a raw GitHub webhook payload.

    This is the whole of the NanoDictate-side bridge's understanding of the
    event, and it is intentionally a *classification* -- "this webhook is a check
    suite completing" -- plus a copy of the identifying fields. It contains no
    orchestration: which of these events matters, and what should happen, is not
    decided here.
    """

    if not isinstance(payload, Mapping):
        raise EventError("payload_not_a_mapping", "Event payload must be an object.")

    repository = repository or _text(payload.get("repository"))
    action = _text(payload.get("action"))
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), Mapping) else {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), Mapping) else {}
    check = payload.get("check_run") if isinstance(payload.get("check_run"), Mapping) else {}
    review = payload.get("review") if isinstance(payload.get("review"), Mapping) else {}
    comment = payload.get("comment") if isinstance(payload.get("comment"), Mapping) else {}
    workflow_run = (
        payload.get("workflow_run") if isinstance(payload.get("workflow_run"), Mapping) else {}
    )
    release = payload.get("release") if isinstance(payload.get("release"), Mapping) else {}

    head = pull.get("head") if isinstance(pull.get("head"), Mapping) else {}
    head_sha = _text(head.get("sha")) or _text(check.get("head_sha")) or _text(
        workflow_run.get("head_sha")
    )
    base = pull.get("base") if isinstance(pull.get("base"), Mapping) else {}

    number = pull.get("number") or issue.get("number")
    labels: list[str] = []
    for source in (pull.get("labels"), issue.get("labels")):
        for item in _sequence(source):
            if isinstance(item, Mapping):
                name = _text(item.get("name"))
            else:
                name = _text(item)
            if name:
                labels.append(name)

    event_type, reason = _classify(
        event_name,
        action,
        pull,
        issue,
        check,
        review,
        comment,
        workflow_run,
        release,
    )
    if event_type is None:
        raise EventError("unclassified_event", reason or "Event is not shadowable.")

    checks = {context: conclusion for context, conclusion in _check_state(payload).items()}
    reviews = {context: conclusion for context, conclusion in _review_state(payload).items()}

    body = _text(issue.get("body")) or _text(pull.get("body"))

    return from_payload(
        {
            "repository": repository,
            "event_type": event_type,
            "action": action,
            "number": number,
            "head_sha": head_sha,
            "base_sha": _text(base.get("sha")),
            "base_ref": base_ref,
            "head_ref": _text(head.get("ref")),
            "body": body,
            "labels": labels,
            "source_issue": _issue_from_branch(_text(head.get("ref")), body),
            "checks": checks,
            "reviews": reviews,
            "release": _release_context(release) if event_type == TYPE_RELEASE_EVENT else {},
            "actor": _actor(payload),
            "observed_at": observed_at,
            "observed_at_ms": observed_at_ms,
            "nonce": nonce or _text(workflow_run.get("id")) or _text(check.get("id")),
        }
    )


def _classify(
    event_name: str,
    action: str,
    pull: Mapping[str, Any],
    issue: Mapping[str, Any],
    check: Mapping[str, Any],
    review: Mapping[str, Any],
    comment: Mapping[str, Any],
    workflow_run: Mapping[str, Any],
    release: Mapping[str, Any],
) -> Tuple[Optional[str], str]:
    """Map a webhook onto a shadowable event type.

    Returns ``(None, reason)`` for anything the bridge cannot vouch for. An
    unrecognised webhook is not forwarded: forwarding it would produce a journal
    with no decision in it, and a journal with no decision in it is evidence
    that looks like coverage.
    """

    name = (event_name or "").strip()

    if name == "check_run" and action == "completed":
        return TYPE_CI_COMPLETED, ""
    if name == "check_suite" and action == "completed":
        return TYPE_CI_COMPLETED, ""
    if name == "workflow_run" and action == "completed":
        return TYPE_WORKFLOW_RUN_COMPLETED, ""
    if name in ("release", "release_event"):
        return TYPE_RELEASE_EVENT, ""
    if name == "pull_request_review_comment" and action == "created":
        return TYPE_PR_REVIEW_COMMENT, ""
    if name == "pull_request_review" and action in ("submitted", "dismissed"):
        return TYPE_PR_REVIEW, ""
    if name in ("pull_request", "pull_request_target"):
        if pull.get("number") is None:
            return None, "pull request payload has no number"
        if action == "opened":
            return TYPE_PR_OPENED, ""
        if action == "synchronize":
            return TYPE_PR_SYNCHRONIZE, ""
        return None, "pull_request action {!r} is not shadowable".format(action)
    if name in ("issues", "issue_comment"):
        if issue.get("pull_request"):
            return None, "issue payload is a pull request"
        if issue.get("number") is None:
            return None, "issue payload has no number"
        if name == "issue_comment":
            return TYPE_ISSUE_OPENED, ""
        if action == "labeled":
            return TYPE_ISSUE_LABELLED, ""
        if action == "closed":
            return TYPE_ISSUE_CLOSED, ""
        return None, "issues action {!r} is not shadowable".format(action)
    if name == "schedule":
        return TYPE_SCHEDULE, ""
    return None, "event {!r} is not shadowable".format(name)


def _check_state(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Conclusion per check context, from whichever field the event carried."""

    states: Dict[str, str] = {}
    for container, context_key, conclusion_key in (
        (payload.get("check_run"), "name", "conclusion"),
        (payload.get("check_suite"), "", "conclusion"),
        (payload.get("workflow_run"), "name", "conclusion"),
    ):
        if not isinstance(container, Mapping):
            continue
        context = _text(container.get(context_key)) or _text(container.get("name")) or "ci"
        conclusion = _text(container.get(conclusion_key)) or _text(container.get("status"))
        if conclusion:
            states[context] = conclusion
    return states


def _review_state(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Review verdicts by reviewer, from the event payload.

    The production review gate keys on the *bot* that submitted a review, so
    the login is what has to survive normalization; a verdict with no author
    cannot be matched against the gate's expectations.
    """

    states: Dict[str, str] = {}
    review = payload.get("review")
    if isinstance(review, Mapping):
        user = review.get("user") if isinstance(review.get("user"), Mapping) else {}
        login = _text(user.get("login")) or "unknown"
        state = _text(review.get("state"))
        if state:
            states[login] = state
    states.update(_string_map(payload.get("reviews")))
    return states


def _release_context(release: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "tag": _text(release.get("tag_name")),
        "name": _text(release.get("name")),
        "draft": bool(release.get("draft")),
        "prerelease": bool(release.get("prerelease")),
        "action": _text(release.get("action")),
    }


def _actor(payload: Mapping[str, Any]) -> str:
    for key in ("sender", "comment", "review", "workflow_run", "check_run", "release"):
        container = payload.get(key)
        if isinstance(container, Mapping):
            user = container.get("user") or container.get("actor") or container.get("sender")
            if isinstance(user, Mapping):
                login = _text(user.get("login"))
                if login:
                    return login
            if key in ("workflow_run", "check_run", "release"):
                login = _text(container.get("actor", {}).get("login")) if isinstance(
                    container.get("actor"), Mapping
                ) else ""
                if login:
                    return login
    return ""
