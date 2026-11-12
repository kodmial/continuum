"""Exact-HEAD CI-repair attempt receipts (kodmial/continuum#444).

This module is the durable evidence format for the single-owner CI-repair
controller described by kodmial/continuum#443 phase 1. It is deliberately
pure: it builds, renders, parses, validates and reconciles supplied comment
data only. It performs no GitHub API call, mutates no label/comment/workflow
state, and is not called in production yet.

A receipt is a hidden machine marker on an issue comment::

    <!-- continuum-ci-repair-receipt v=1 repo=owner/name pr=123
         head=<40-hex> attempt=<id> status=claimed -->

A receipt is keyed by the normalized ``(repository, pull number, exact
40-hex HEAD)`` triple plus its unique attempt/correlation ID. The optional
``run=<positive id>`` and ``url=https://github.com/<repo>/actions/runs/<id>``
fields are accepted only for a ``dispatched`` receipt and only when the URL
proves the same repository and run ID.

Status vocabulary and allowed transitions
-----------------------------------------
``claimed``
    The serialized owner has persisted the claim before asking GitHub to
    dispatch a repair. Run details are forbidden because no run is proved.
``dispatched``
    A dispatch was accepted, or an ambiguous attempt was reconciled to a
    proved actual run. Run details are optional (legacy dispatch responses
    may not return them) and may be enriched later without losing identity.
``ambiguous``
    The dispatch may have been accepted but no run is proved. This is a
    visible hold: it must never authorize a second dispatch by itself.
``rejected``
    The dispatch definitively failed before acceptance. A later claim may
    re-arm the same exact HEAD, but this attempt ID stays immutable.

The reader is fail closed. A complete, ordered comment history must be
explicitly attested by the caller. Missing coverage, pagination, length or
ordering yields the explicit ``incomplete`` outcome, never an empty
permissive result. Malformed/truncated markers, unsupported versions,
untrusted authors, mixed repository/PR records, conflicting duplicate
records and impossible transitions yield ``invalid`` or ``conflict``.
:func:`decide_new_attempt` returns ``allowed=True`` only for a complete and
valid history with no existing non-rejected attempt for the exact HEAD.

This is a record, not an atomic lock. Issue-comment creation has no GitHub
compare-and-swap guarantee. Phase 2 must still serialize the read -> claim ->
dispatch sequence per ``(repository, exact HEAD)`` and reconcile an
``ambiguous`` claim against the live run inventory before any retry.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple


#: Schema version of the receipt marker. A different version is unsupported
#: and fails closed instead of being interpreted best-effort.
RECEIPT_SCHEMA_VERSION = 1

#: Stable hidden marker token. It is the only ownership evidence parsed here;
#: mutable labels and presentation text are never authoritative.
RECEIPT_MARKER = "continuum-ci-repair-receipt"

STATUS_CLAIMED = "claimed"
STATUS_DISPATCHED = "dispatched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_REJECTED = "rejected"

#: Bounded status vocabulary. Unknown statuses are invalid.
RECEIPT_STATUSES = (
    STATUS_CLAIMED,
    STATUS_DISPATCHED,
    STATUS_AMBIGUOUS,
    STATUS_REJECTED,
)

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_INVALID = "invalid"
OUTCOME_CONFLICT = "conflict"

ACTION_CLAIM = "claim"
ACTION_WAIT = "wait"
ACTION_HOLD = "hold"

#: Comment authorships trusted to publish receipts. Only repository
#: ownership grants trust here: organization membership and outside
#: collaboration can be read-only, so they cannot prove write access.
#: Any other author needs the configured owner login or an explicit login
#: allowlist.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({"OWNER"})
TRUSTED_AUTOMATION_LOGINS = frozenset({"github-actions[bot]"})

_REPO_RE = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}")
_ATTEMPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{7,127}")
_PULL_NUMBER_RE = re.compile(r"[1-9][0-9]*")
_RUN_ID_RE = re.compile(r"[1-9][0-9]*")
_RECEIPT_TOKEN_RE = re.compile(re.escape(RECEIPT_MARKER))

# The field order is part of the durable format. Unknown/extra fields,
# missing fields, or a truncated closing delimiter never match and are
# rejected by :func:`parse_receipts` as malformed evidence.
_RECEIPT_MARKER_RE = re.compile(
    r"<!--\s*"
    + re.escape(RECEIPT_MARKER)
    + r"\s+v=([0-9]+)\s+"
    r"repo=([^\s>]+)\s+"
    r"pr=([0-9]+)\s+"
    r"head=([0-9a-fA-F]{40})\s+"
    r"attempt=([^\s>]+)\s+"
    r"status=([a-z]+)"
    r"(?:\s+run=([0-9]+))?"
    r"(?:\s+url=([^\s>]+))?"
    r"\s*-->"
)

# A status may only move forward. ``ambiguous`` may be reconciled to
# ``dispatched`` once a proved run is found; it never re-enters ``claimed``.
_TRANSITIONS = {
    STATUS_CLAIMED: frozenset(
        {STATUS_CLAIMED, STATUS_DISPATCHED, STATUS_AMBIGUOUS, STATUS_REJECTED}
    ),
    STATUS_DISPATCHED: frozenset({STATUS_DISPATCHED}),
    STATUS_AMBIGUOUS: frozenset({STATUS_AMBIGUOUS, STATUS_DISPATCHED}),
    STATUS_REJECTED: frozenset({STATUS_REJECTED}),
}


class ReceiptError(ValueError):
    """Raised when receipt data cannot be interpreted safely."""


# ---------------------------------------------------------------------------
# Normalization and validation.
# ---------------------------------------------------------------------------

def normalize_repository(value: Any) -> str:
    """Return the canonical lowercase ``owner/name`` repository key."""

    text = str(value or "").strip().lower()
    if not _REPO_RE.fullmatch(text):
        raise ReceiptError(
            "repository must look like owner/name, got {!r}".format(value)
        )
    return text


def normalize_pull_number(value: Any) -> int:
    """Validate a positive pull-request number."""

    if isinstance(value, bool):
        raise ReceiptError("pull number must be a positive integer")
    text = str(value if value is not None else "").strip()
    if not _PULL_NUMBER_RE.fullmatch(text):
        raise ReceiptError("pull number must be a positive integer")
    return int(text)


def normalize_head_sha(value: Any) -> str:
    """Return the canonical lowercase 40-hex exact HEAD SHA."""

    text = str(value or "").strip().lower()
    if not _HEAD_RE.fullmatch(text):
        raise ReceiptError(
            "head_sha must be exactly 40 hexadecimal characters, got {!r}".format(
                value
            )
        )
    return text


def normalize_attempt_id(value: Any) -> str:
    """Validate the unique immutable attempt/correlation ID."""

    text = str(value or "").strip()
    if not _ATTEMPT_ID_RE.fullmatch(text):
        raise ReceiptError(
            "attempt ID must be 8-128 characters from [A-Za-z0-9._:-] "
            "starting with an alphanumeric character, got {!r}".format(value)
        )
    return text


def _coerce_integral(value: Any) -> Optional[int]:
    """Return ``value`` as an integer only when it carries no fractional part.

    Floats and other numeric objects are rejected even when their value is
    whole, because their fractional part must never be silently truncated.
    Decimal strings remain accepted for marker text parsed from comments.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?[0-9]+", text):
            return int(text)
    return None


def new_attempt_id() -> str:
    """Return a fresh, nonempty attempt ID (UUID4; no network access)."""

    return "ci-" + uuid.uuid4().hex


def _normalize_run_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ReceiptError("run ID must be a positive integer")
    text = str(value).strip()
    if not text:
        return None
    if not _RUN_ID_RE.fullmatch(text):
        raise ReceiptError("run ID must be a positive integer")
    return int(text)


def _canonical_run_url(repository: str, run_id: int) -> str:
    return "https://github.com/{}/actions/runs/{}".format(repository, run_id)


# ---------------------------------------------------------------------------
# Canonical record and marker rendering.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepairReceipt:
    """One validated exact-HEAD CI-repair attempt receipt.

    ``repository``, ``head_sha`` and ``run_url`` are canonical lowercase
    values. ``run_id``/``run_url`` are populated only for ``dispatched``.
    """

    schema_version: int
    repository: str
    pull_number: int
    head_sha: str
    attempt_id: str
    status: str
    run_id: Optional[int] = None
    run_url: str = ""


def build_receipt(
    *,
    repository: Any,
    pull_number: Any,
    head_sha: Any,
    attempt_id: Any,
    status: Any,
    run_id: Any = None,
    run_url: Any = None,
    schema_version: Any = RECEIPT_SCHEMA_VERSION,
) -> RepairReceipt:
    """Build and validate one canonical receipt.

    Raises :class:`ReceiptError` for an unsupported version, malformed
    identity, unknown status, status/run-field inconsistency, or a run URL
    that does not prove the same repository and run ID. No I/O is performed.
    """

    version = _coerce_integral(schema_version)
    if version is None:
        raise ReceiptError("receipt schema version must be an integer")
    if version != RECEIPT_SCHEMA_VERSION:
        raise ReceiptError(
            "unsupported receipt schema version {} (expected {})".format(
                version, RECEIPT_SCHEMA_VERSION
            )
        )

    repository_key = normalize_repository(repository)
    number = normalize_pull_number(pull_number)
    head = normalize_head_sha(head_sha)
    attempt = normalize_attempt_id(attempt_id)
    status_text = str(status or "").strip()
    if status_text not in RECEIPT_STATUSES:
        raise ReceiptError(
            "unknown receipt status {!r} (expected one of {})".format(
                status, ", ".join(RECEIPT_STATUSES)
            )
        )

    number_run_id = _normalize_run_id(run_id)
    url_text = str(run_url or "").strip()
    if url_text:
        url_text = url_text.rstrip("/")
    if status_text != STATUS_DISPATCHED:
        if number_run_id is not None or url_text:
            raise ReceiptError(
                "run ID/URL may only prove a {!r} receipt".format(
                    STATUS_DISPATCHED
                )
            )
    else:
        if url_text and number_run_id is None:
            raise ReceiptError("a run URL requires its workflow run ID")
        if number_run_id is not None and url_text:
            expected = _canonical_run_url(repository_key, number_run_id)
            if url_text != expected:
                raise ReceiptError(
                    "run URL must prove the same repository and run ID "
                    "(expected {})".format(expected)
                )

    return RepairReceipt(
        schema_version=version,
        repository=repository_key,
        pull_number=number,
        head_sha=head,
        attempt_id=attempt,
        status=status_text,
        run_id=number_run_id,
        run_url=url_text,
    )


def receipt_marker_line(receipt: RepairReceipt) -> str:
    """Render the hidden machine marker for a validated receipt."""

    parts = [
        "<!--",
        RECEIPT_MARKER,
        "v={}".format(receipt.schema_version),
        "repo={}".format(receipt.repository),
        "pr={}".format(receipt.pull_number),
        "head={}".format(receipt.head_sha),
        "attempt={}".format(receipt.attempt_id),
        "status={}".format(receipt.status),
    ]
    if receipt.run_id is not None:
        parts.append("run={}".format(receipt.run_id))
    if receipt.run_url:
        parts.append("url={}".format(receipt.run_url))
    parts.append("-->")
    return " ".join(parts)


def render_receipt(receipt: RepairReceipt) -> str:
    """Render a durable issue-comment body containing one receipt marker."""

    return receipt_marker_line(receipt)


def receipt_key(receipt: RepairReceipt) -> Tuple[str, int, str]:
    """Return the normalized ``(repository, pull number, HEAD)`` key."""

    return (receipt.repository, receipt.pull_number, receipt.head_sha)


# ---------------------------------------------------------------------------
# Parsing one supplied comment body.
# ---------------------------------------------------------------------------

def parse_receipts(body: Any) -> Tuple[RepairReceipt, ...]:
    """Parse every strict receipt marker in one comment body.

    Returns an empty tuple when the body has no receipt token at all. A token
    that does not occur inside a complete strict marker is treated as
    malformed/truncated evidence and raises :class:`ReceiptError`; no partial
    markers are accepted.
    """

    text = str(body or "")
    matches = list(_RECEIPT_MARKER_RE.finditer(text))
    spans = [(match.start(), match.end()) for match in matches]
    # A token inside a complete marker belongs to that marker (the unique
    # attempt ID is allowed to contain the token text). Any token outside
    # every complete marker is malformed/truncated evidence.
    for token in _RECEIPT_TOKEN_RE.finditer(text):
        if not any(start <= token.start() < end for start, end in spans):
            raise ReceiptError(
                "malformed or truncated CI-repair receipt marker: "
                "every marker token must belong to one complete strict marker"
            )
    if not matches:
        return ()

    receipts = []
    for match in matches:
        (
            version_text,
            repository_text,
            pull_text,
            head_text,
            attempt_text,
            status_text,
            run_text,
            url_text,
        ) = match.groups()
        try:
            receipts.append(
                build_receipt(
                    repository=repository_text,
                    pull_number=pull_text,
                    head_sha=head_text,
                    attempt_id=attempt_text,
                    status=status_text,
                    run_id=run_text,
                    run_url=url_text,
                    schema_version=version_text,
                )
            )
        except ReceiptError as exc:
            raise ReceiptError(
                "invalid CI-repair receipt marker: {}".format(exc)
            ) from exc
    return tuple(receipts)


# ---------------------------------------------------------------------------
# Trust and status transitions.
# ---------------------------------------------------------------------------

def _comment_login(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, Mapping):
        login = str(user.get("login") or "").strip()
        if login:
            return login.lower()
    author = comment.get("author")
    if isinstance(author, Mapping):
        login = str(author.get("login") or "").strip()
        if login:
            return login.lower()
    for key in ("author_login", "login"):
        login = str(comment.get(key) or "").strip()
        if login:
            return login.lower()
    return ""


def _has_author_metadata(comment: Mapping[str, Any]) -> bool:
    """Whether enough author metadata exists to prove a marked comment untrusted.

    Both trust sources must be known before a failed trust check may ignore
    a marked comment. A missing login might identify a trusted workflow bot;
    a missing association might identify a repository OWNER.
    """

    association = str(comment.get("author_association") or "").strip()
    return bool(association) and bool(_comment_login(comment))


def is_trusted_receipt_comment(
    comment: Mapping[str, Any],
    *,
    owner_login: str = "",
    trusted_associations: Sequence[str] = tuple(TRUSTED_COMMENT_ASSOCIATIONS),
    trusted_logins: Sequence[str] = tuple(TRUSTED_AUTOMATION_LOGINS),
) -> bool:
    """Whether a comment is authorized to publish authoritative receipts.

    Repository ownership (OWNER), the workflow bot login, and the configured
    owner login are trusted. Membership and outside collaboration do not
    prove write access, so they are not trusted by default. Anything else is
    ignored and can never spoof or block receipt state.
    """

    if not isinstance(comment, Mapping):
        return False
    association = str(comment.get("author_association") or "").upper()
    associations = {str(item).upper() for item in trusted_associations}
    if association in associations:
        return True
    login = _comment_login(comment)
    logins = {str(item).lower() for item in trusted_logins}
    owner = str(owner_login or "").strip().lower()
    if owner:
        logins.add(owner)
    return bool(login) and login in logins


def _validate_advance(previous: RepairReceipt, following: RepairReceipt) -> None:
    if receipt_key(previous) != receipt_key(following):
        raise ReceiptError("receipt transition changes the exact-HEAD key")
    if previous.attempt_id != following.attempt_id:
        raise ReceiptError("receipt transition changes the attempt ID")
    if following.status not in _TRANSITIONS[previous.status]:
        raise ReceiptError(
            "impossible receipt transition {} -> {}".format(
                previous.status, following.status
            )
        )

    if (
        previous.status == STATUS_DISPATCHED
        and following.status == STATUS_DISPATCHED
    ):
        if previous.run_id is not None:
            if following.run_id is None:
                raise ReceiptError(
                    "dispatched receipt must not lose its proved run ID"
                )
            if following.run_id != previous.run_id:
                raise ReceiptError(
                    "conflicting run IDs for the same dispatched attempt"
                )
        if previous.run_url:
            if not following.run_url:
                raise ReceiptError(
                    "dispatched receipt must not lose its proved run URL"
                )
            if following.run_url != previous.run_url:
                raise ReceiptError(
                    "conflicting run URLs for the same dispatched attempt"
                )


def validate_receipt_transition(
    previous: RepairReceipt,
    following: RepairReceipt,
) -> None:
    """Raise :class:`ReceiptError` unless ``following`` is a legal successor.

    Duplicate identical records are legal. Run details may be added to a
    ``dispatched`` record, but an attempt may never regress or switch to a
    different run.
    """

    _validate_advance(previous, following)


def transition_receipt(
    previous: RepairReceipt,
    *,
    status: Any,
    run_id: Any = None,
    run_url: Any = None,
) -> RepairReceipt:
    """Build the next receipt for one attempt after validating its transition.

    This is the helper phase 2 uses to persist ``dispatched`` (accepted run),
    ``ambiguous`` (uncertain dispatch), ``rejected`` (definite failure), or to
    enrich a dispatched record with the correlated run details.
    """

    following = build_receipt(
        repository=previous.repository,
        pull_number=previous.pull_number,
        head_sha=previous.head_sha,
        attempt_id=previous.attempt_id,
        status=status,
        run_id=run_id,
        run_url=run_url,
    )
    _validate_advance(previous, following)
    return following


# ---------------------------------------------------------------------------
# Reading a supplied comment history.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttemptSummary:
    """Reconciled state of one attempt ID for the queried exact HEAD."""

    attempt_id: str
    status: str
    run_id: Optional[int]
    run_url: str
    receipt_count: int
    latest_receipt: RepairReceipt


@dataclass(frozen=True)
class ReceiptCollection:
    """Fail-closed result of reading one supplied PR comment history.

    ``receipts`` contains every valid observation for the queried HEAD, in
    supplied order. ``all_receipts`` additionally contains valid receipts for
    older/newer HEADs of the same repository/PR, so earlier keys remain
    visible and are never rewritten. ``attempts`` is the reconciled per-attempt
    state for the queried HEAD only.
    """

    outcome: str
    reason: str
    receipts: Tuple[RepairReceipt, ...] = ()
    all_receipts: Tuple[RepairReceipt, ...] = ()
    attempts: Tuple[AttemptSummary, ...] = ()
    ignored_untrusted: int = 0


@dataclass(frozen=True)
class AttemptDecision:
    """Whether the serialized owner may claim a new exact-HEAD attempt."""

    allowed: bool
    action: str
    reason: str
    attempt_id: str = ""


def _incomplete(reason: str, ignored_untrusted: int = 0) -> ReceiptCollection:
    return ReceiptCollection(
        outcome=OUTCOME_INCOMPLETE,
        reason=reason,
        ignored_untrusted=ignored_untrusted,
    )


def _invalid(reason: str, ignored_untrusted: int = 0) -> ReceiptCollection:
    return ReceiptCollection(
        outcome=OUTCOME_INVALID,
        reason=reason,
        ignored_untrusted=ignored_untrusted,
    )


def _conflict(
    reason: str,
    receipts: Tuple[RepairReceipt, ...],
    all_receipts: Tuple[RepairReceipt, ...],
    attempts: Tuple[AttemptSummary, ...],
    ignored_untrusted: int,
) -> ReceiptCollection:
    return ReceiptCollection(
        outcome=OUTCOME_CONFLICT,
        reason=reason,
        receipts=receipts,
        all_receipts=all_receipts,
        attempts=attempts,
        ignored_untrusted=ignored_untrusted,
    )


def read_receipts(
    comments: Sequence[Mapping[str, Any]],
    *,
    repository: Any,
    pull_number: Any,
    head_sha: Any,
    history_complete: bool = False,
    history_ordered: bool = False,
    expected_comment_count: Optional[int] = None,
    owner_login: str = "",
    trusted_associations: Sequence[str] = tuple(TRUSTED_COMMENT_ASSOCIATIONS),
    trusted_logins: Sequence[str] = tuple(TRUSTED_AUTOMATION_LOGINS),
) -> ReceiptCollection:
    """Read exact-HEAD receipts from a caller-supplied PR comment history.

    ``history_complete`` and ``history_ordered`` must be literal ``True``
    only after the caller has established that the supplied comments cover
    the whole history (pagination exhausted, no lost/truncated pages) and are
    in ascending creation order. Their default is deliberately fail closed:
    an un-attested history yields ``incomplete``, never an empty permissive
    result. If ``expected_comment_count`` is supplied it must equal the
    supplied length or the history is incomplete. A supplied comment without
    a string body also makes the history incomplete.

    Trusted markers for another repository or PR invalidate the history as
    mixed evidence. Trusted markers for another HEAD are retained in
    ``all_receipts`` as independent keys and their valid state does not block
    the queried HEAD. An impossible transition, reused attempt ID, or duplicate
    live attempt anywhere in the supplied log is corrupt evidence and fails
    closed instead of permitting a dispatch. Untrusted marker lookalikes are
    ignored without affecting authority. A marker whose author has neither an
    association nor a usable login is unknown, not untrusted, and makes the
    history incomplete.
    """

    repository_key = normalize_repository(repository)
    number = normalize_pull_number(pull_number)
    head = normalize_head_sha(head_sha)

    if comments is None:
        return _incomplete(
            "comment history is unavailable; authoritative state cannot be read"
        )
    try:
        materialized = list(comments)
    except TypeError:
        return _invalid("comment history is not an iterable sequence")

    if history_complete is not True:
        return _incomplete(
            "comment history completeness was not established (pagination/"
            "coverage unknown)"
        )
    if history_ordered is not True:
        return _incomplete(
            "comment history ordering was not established (oldest-first "
            "order unknown)"
        )
    if expected_comment_count is not None:
        expected = _coerce_integral(expected_comment_count)
        if expected is None:
            return _incomplete("expected comment count is not an integer")
        if expected < 0 or expected != len(materialized):
            return _incomplete(
                "comment history length does not prove coverage: expected "
                "{}, received {}".format(expected_comment_count, len(materialized))
            )

    target_receipts = []
    all_receipts = []
    ignored_untrusted = 0

    for comment in materialized:
        if not isinstance(comment, Mapping):
            return _invalid("comment history contains a non-object entry")
        body_value = comment.get("body")
        if not isinstance(body_value, str):
            return _incomplete(
                "comment history contains a comment without a string body; "
                "authoritative receipt state cannot be read"
            )
        body = body_value
        if not is_trusted_receipt_comment(
            comment,
            owner_login=owner_login,
            trusted_associations=trusted_associations,
            trusted_logins=trusted_logins,
        ):
            if RECEIPT_MARKER in body:
                if not _has_author_metadata(comment):
                    return _incomplete(
                        "marked receipt comment has unknown authorship; "
                        "authoritative author metadata is required",
                        ignored_untrusted,
                    )
                ignored_untrusted += 1
            continue
        if RECEIPT_MARKER not in body:
            continue
        try:
            parsed = parse_receipts(body)
        except ReceiptError as exc:
            return _invalid(str(exc), ignored_untrusted)
        for receipt in parsed:
            if (
                receipt.repository != repository_key
                or receipt.pull_number != number
            ):
                return _invalid(
                    "mixed repository/PR receipt {}#{} while reading {}#{}".format(
                        receipt.repository,
                        receipt.pull_number,
                        repository_key,
                        number,
                    ),
                    ignored_untrusted,
                )
            all_receipts.append(receipt)
            if receipt.head_sha == head:
                target_receipts.append(receipt)

    # An attempt/correlation ID is immutable and unique. Reusing it for a
    # different HEAD destroys correlation, so it is never accepted.
    attempt_heads = {}
    for receipt in all_receipts:
        previous_head = attempt_heads.get(receipt.attempt_id)
        if previous_head is not None and previous_head != receipt.head_sha:
            return _invalid(
                "attempt ID {!r} is reused across exact HEADs".format(
                    receipt.attempt_id
                ),
                ignored_untrusted,
            )
        attempt_heads[receipt.attempt_id] = receipt.head_sha

    # Reconcile every attempt in the supplied history, not only the queried
    # HEAD: an impossible transition anywhere means the append-only receipt
    # log is corrupt and cannot be trusted to authorize a fresh dispatch.
    current = {}
    counts = {}
    order = []
    for receipt in all_receipts:
        if receipt.attempt_id not in current:
            current[receipt.attempt_id] = receipt
            counts[receipt.attempt_id] = 1
            order.append(receipt.attempt_id)
            continue
        try:
            _validate_advance(current[receipt.attempt_id], receipt)
        except ReceiptError as exc:
            attempts = _summarize(order, current, counts, head)
            return _conflict(
                str(exc),
                tuple(target_receipts),
                tuple(all_receipts),
                attempts,
                ignored_untrusted,
            )
        current[receipt.attempt_id] = receipt
        counts[receipt.attempt_id] += 1

    attempts = _summarize(order, current, counts, head)

    # One exact HEAD may have at most one non-rejected attempt. Rejected
    # attempts are terminal and may be re-armed with a fresh attempt ID.
    live_by_head = {}
    for attempt_id in order:
        receipt = current[attempt_id]
        if receipt.status == STATUS_REJECTED:
            continue
        live_by_head.setdefault(receipt.head_sha, []).append(attempt_id)
    for live_head, live_ids in live_by_head.items():
        if len(live_ids) < 2:
            continue
        if live_head == head:
            reason = (
                "multiple non-rejected attempts for the same exact HEAD: {}"
            ).format(", ".join(live_ids))
        else:
            reason = (
                "multiple non-rejected attempts for exact HEAD {}: {}"
            ).format(live_head, ", ".join(live_ids))
        return _conflict(
            reason,
            tuple(target_receipts),
            tuple(all_receipts),
            attempts,
            ignored_untrusted,
        )

    return ReceiptCollection(
        outcome=OUTCOME_COMPLETE,
        reason="receipt history is complete, ordered and internally consistent",
        receipts=tuple(target_receipts),
        all_receipts=tuple(all_receipts),
        attempts=attempts,
        ignored_untrusted=ignored_untrusted,
    )


def _summarize(
    order: Sequence[str],
    current: Mapping[str, RepairReceipt],
    counts: Mapping[str, int],
    head_sha: str,
) -> Tuple[AttemptSummary, ...]:
    """Summarize reconciled attempts for one exact HEAD, in history order."""

    summaries = []
    for attempt_id in order:
        receipt = current[attempt_id]
        if receipt.head_sha != head_sha:
            continue
        summaries.append(
            AttemptSummary(
                attempt_id=attempt_id,
                status=receipt.status,
                run_id=receipt.run_id,
                run_url=receipt.run_url,
                receipt_count=counts[attempt_id],
                latest_receipt=receipt,
            )
        )
    return tuple(summaries)


def attempt_for_id(
    collection: ReceiptCollection,
    attempt_id: Any,
) -> Optional[AttemptSummary]:
    """Return the reconciled attempt with this ID, or None."""

    text = str(attempt_id or "").strip()
    for attempt in collection.attempts:
        if attempt.attempt_id == text:
            return attempt
    return None


def transition_attempt(
    attempt: AttemptSummary,
    *,
    status: Any,
    run_id: Any = None,
    run_url: Any = None,
) -> RepairReceipt:
    """Build a validated next receipt for an already-read attempt."""

    return transition_receipt(
        attempt.latest_receipt,
        status=status,
        run_id=run_id,
        run_url=run_url,
    )


def decide_new_attempt(collection: ReceiptCollection) -> AttemptDecision:
    """Decide whether a new exact-HEAD attempt may be claimed.

    The only permitting result is a complete/valid history with no
    non-rejected attempt for the queried HEAD. An incomplete, invalid or
    conflicting history holds. ``claimed`` waits for its owner; ``dispatched``
    already spent the attempt; ``ambiguous`` holds for correlation and can
    never authorize a second dispatch. A history containing only definitely
    ``rejected`` attempts may re-arm with a new attempt ID.
    """

    if collection.outcome == OUTCOME_INCOMPLETE:
        return AttemptDecision(
            allowed=False,
            action=ACTION_HOLD,
            reason="incomplete receipt history: {}".format(collection.reason),
        )
    if collection.outcome == OUTCOME_INVALID:
        return AttemptDecision(
            allowed=False,
            action=ACTION_HOLD,
            reason="invalid receipt history: {}".format(collection.reason),
        )
    if collection.outcome == OUTCOME_CONFLICT:
        return AttemptDecision(
            allowed=False,
            action=ACTION_HOLD,
            reason="conflicting receipt history: {}".format(collection.reason),
        )
    if collection.outcome != OUTCOME_COMPLETE:
        return AttemptDecision(
            allowed=False,
            action=ACTION_HOLD,
            reason="unknown receipt history outcome {!r}: failing closed".format(
                collection.outcome
            ),
        )

    for attempt in collection.attempts:
        if attempt.status == STATUS_AMBIGUOUS:
            return AttemptDecision(
                allowed=False,
                action=ACTION_HOLD,
                reason=(
                    "attempt {!r} has ambiguous dispatch acceptance; reconcile "
                    "the correlation ID and live run inventory first"
                ).format(attempt.attempt_id),
                attempt_id=attempt.attempt_id,
            )
        if attempt.status == STATUS_CLAIMED:
            return AttemptDecision(
                allowed=False,
                action=ACTION_WAIT,
                reason="attempt {!r} already holds the exact-HEAD claim".format(
                    attempt.attempt_id
                ),
                attempt_id=attempt.attempt_id,
            )
        if attempt.status == STATUS_DISPATCHED:
            return AttemptDecision(
                allowed=False,
                action=ACTION_WAIT,
                reason="attempt {!r} already dispatched this exact HEAD".format(
                    attempt.attempt_id
                ),
                attempt_id=attempt.attempt_id,
            )
        if attempt.status != STATUS_REJECTED:
            return AttemptDecision(
                allowed=False,
                action=ACTION_HOLD,
                reason="unknown attempt status {!r}: failing closed".format(
                    attempt.status
                ),
                attempt_id=attempt.attempt_id,
            )

    if collection.attempts:
        reason = (
            "every prior attempt for this exact HEAD was definitely rejected; "
            "a new attempt ID may be claimed"
        )
    else:
        reason = "no receipt exists for this exact HEAD"
    return AttemptDecision(
        allowed=True,
        action=ACTION_CLAIM,
        reason=reason,
    )


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_MARKER",
    "STATUS_CLAIMED",
    "STATUS_DISPATCHED",
    "STATUS_AMBIGUOUS",
    "STATUS_REJECTED",
    "RECEIPT_STATUSES",
    "OUTCOME_COMPLETE",
    "OUTCOME_INCOMPLETE",
    "OUTCOME_INVALID",
    "OUTCOME_CONFLICT",
    "ACTION_CLAIM",
    "ACTION_WAIT",
    "ACTION_HOLD",
    "TRUSTED_COMMENT_ASSOCIATIONS",
    "TRUSTED_AUTOMATION_LOGINS",
    "ReceiptError",
    "RepairReceipt",
    "AttemptSummary",
    "ReceiptCollection",
    "AttemptDecision",
    "normalize_repository",
    "normalize_pull_number",
    "normalize_head_sha",
    "normalize_attempt_id",
    "new_attempt_id",
    "build_receipt",
    "receipt_marker_line",
    "render_receipt",
    "receipt_key",
    "parse_receipts",
    "is_trusted_receipt_comment",
    "validate_receipt_transition",
    "transition_receipt",
    "read_receipts",
    "attempt_for_id",
    "transition_attempt",
    "decide_new_attempt",
]
