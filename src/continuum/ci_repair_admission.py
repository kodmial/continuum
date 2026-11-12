"""Production admission helpers for exact-HEAD CI repair (kodmial/continuum#446).

Phase 1 (``ci_repair_receipt``) defined the durable receipt format. This module
turns that format into an admission plan for the one serialized CI-repair
owner. It remains dependency-free and side-effect free: it normalizes supplied
GitHub comment pages, classifies a workflow-dispatch response, and correlates
an attempt with an actual workflow run. The executable wrapper
``.github/scripts/ci_repair_admission.py`` performs the GitHub calls.

The correctness rule is deliberately narrow:

* GitHub issue comments are a durable *record*, not an atomic lock. Two
  concurrent controllers could both read the same empty history.
* Therefore the workflow concurrency group, not this module, is what makes the
  read -> claim -> dispatch sequence single-owner. A receipt alone never
  authorizes a dispatch outside that serialized owner.
* A claim is persisted before the dispatch request. If acceptance cannot be
  proved, the attempt becomes ``ambiguous`` and is never retried blindly; the
  controller reconciles the attempt correlation ID against actual workflow
  runs first.

The reader fails closed: incomplete pagination, duplicate comments, malformed
metadata, or an unreadable response never authorize a new claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .ci_repair_receipt import (
    ACTION_CLAIM,
    ACTION_HOLD,
    ACTION_WAIT,
    OUTCOME_COMPLETE,
    OUTCOME_INCOMPLETE,
    RECEIPT_MARKER,
    STATUS_AMBIGUOUS,
    STATUS_CLAIMED,
    STATUS_DISPATCHED,
    STATUS_REJECTED,
    STATUS_REQUESTING,
    AttemptSummary,
    ReceiptCollection,
    RepairReceipt,
    attempt_for_id,
    build_receipt,
    decide_new_attempt,
    is_trusted_receipt_comment,
    parse_receipts,
    read_receipts,
    transition_attempt,
    transition_receipt,
)

DISPATCH_ACCEPTED = "accepted"
DISPATCH_REJECTED = "rejected"
DISPATCH_AMBIGUOUS = "ambiguous"

#: Plan action for a bounded, evidence-backed crash recovery: the stale claim
#: is persisted as ``rejected`` and the same serialized pass may claim a fresh
#: attempt. It is deliberately distinct from ``claim`` so a caller cannot
#: mistake the required rejection write for an ordinary admission.
ACTION_REARM = "rearm"

# A 4xx in this set is an explicit refusal: GitHub did not create a run. 403
# and 429 are deliberately absent: they can be rate limiting or a transient
# authorization/budget failure whose acceptance status is not proved. They are
# correlated instead of retried.
DEFINITE_REJECTION_STATUSES = frozenset({400, 401, 404, 409, 410, 422})

#: The structured correlation marker appended to a ci-fix run name. It lets a
#: later reconciler prove which dispatch produced a run without trusting an
#: unordered "latest run" guess.
ATTEMPT_RUN_MARKER_TEMPLATE = "[ci-attempt:{attempt}]"

_ATTEMPT_RUN_MARKER_RE = re.compile(
    r"\[ci-attempt:(?P<attempt>[A-Za-z0-9][A-Za-z0-9._:\-]{7,127})\]"
)
_PULL_TITLE_RE = re.compile(r"^OpenCode repair PR #(?P<pr>[1-9][0-9]*)\b")
_UNMARKED_REPAIR_TITLE_RE = re.compile(r"^OpenCode repair PR #([1-9][0-9]*)$")
_ISO_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class AdmissionError(ValueError):
    """Raised when admission evidence cannot be interpreted safely."""


@dataclass(frozen=True)
class CommentHistory:
    """Normalized comment pages supplied to the receipt reader."""

    comments: Tuple[Mapping[str, Any], ...]
    complete: bool
    ordered: bool
    reason: str
    expected_count: Optional[int] = None


@dataclass(frozen=True)
class DispatchOutcome:
    """A classified workflow-dispatch response."""

    kind: str
    receipt: RepairReceipt
    reason: str
    accepted: bool = False
    ambiguous: bool = False
    rejected: bool = False


@dataclass(frozen=True)
class RunInventory:
    """Normalized workflow-run inventory used for admission correlation.

    ``complete`` is true only when pagination was exhausted and every page was
    well formed. Recovery decisions must never treat a partial inventory as
    proof that no run exists.
    """

    runs: Tuple[Mapping[str, Any], ...]
    complete: bool
    reason: str
    expected_count: Optional[int] = None


@dataclass(frozen=True)
class AdmissionPlan:
    """What the serialized owner should do with the supplied history."""

    action: str
    reason: str
    attempt_id: str = ""
    transition: Optional[RepairReceipt] = None
    run_id: Optional[int] = None
    run_url: str = ""


# ---------------------------------------------------------------------------
# Comment pagination and ordering.
# ---------------------------------------------------------------------------

def normalize_comment_pages(
    pages: Any,
    *,
    expected_count: Any = None,
) -> CommentHistory:
    """Flatten GitHub issue-comment pages into one ordered, complete history.

    ``pages`` is the JSON produced by ``gh api --paginate --slurp``: an array
    of page arrays. A single page array is also accepted for convenience. A
    non-iterable/malformed page, a non-object comment, a non-string body, a
    duplicate comment ID, or fewer comments than the independently reported
    issue-comment count yields an explicit incomplete result. Comments are
    sorted by creation time and then ID, so the receipt reader receives one
    deterministic ascending log even if a proxy returned pages out of order.
    """

    if pages is None:
        return CommentHistory((), False, False, "comment pages are unavailable")
    if isinstance(pages, Mapping):
        return CommentHistory(
            (), False, False, "comment pages must be an array, not an object"
        )
    try:
        materialized_pages = list(pages)
    except TypeError:
        return CommentHistory((), False, False, "comment pages are not iterable")

    # ``gh api --slurp`` yields an array of page arrays, but a single page is
    # also valid input. Distinguish the two by the entry type.
    if materialized_pages and all(
        isinstance(entry, Mapping) for entry in materialized_pages
    ):
        materialized_pages = [materialized_pages]
    elif not materialized_pages:
        materialized_pages = [[]]

    flattened = []
    seen_ids = set()
    for page in materialized_pages:
        if not isinstance(page, Sequence) or isinstance(
            page, (str, bytes, bytearray)
        ):
            return CommentHistory(
                (), False, False, "a comment page is not an array"
            )
        for comment in page:
            if not isinstance(comment, Mapping):
                return CommentHistory(
                    (), False, False, "a comment entry is not an object"
                )
            if not isinstance(comment.get("body"), str):
                return CommentHistory(
                    (),
                    False,
                    False,
                    "a comment has no string body; receipt state is unreadable",
                )
            created = comment.get("created_at")
            if not isinstance(created, str) or not _ISO_TIMESTAMP_RE.match(created):
                return CommentHistory(
                    (),
                    False,
                    False,
                    "a comment has no usable creation timestamp; comment order "
                    "and the attempt observation clock are unproven",
                )
            raw_id = comment.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                try:
                    raw_id = int(str(raw_id))
                except (TypeError, ValueError):
                    raw_id = 0
            if raw_id <= 0:
                return CommentHistory(
                    (), False, False, "a comment has no usable ID"
                )
            if raw_id in seen_ids:
                return CommentHistory(
                    (),
                    False,
                    False,
                    "comment {!r} appears on more than one page".format(raw_id),
                )
            seen_ids.add(raw_id)
            flattened.append((raw_id, comment))

    expected = None
    if expected_count is not None:
        if isinstance(expected_count, bool):
            return CommentHistory(
                (), False, False, "expected comment count is not an integer"
            )
        try:
            expected = int(str(expected_count).strip())
        except (TypeError, ValueError):
            return CommentHistory(
                (), False, False, "expected comment count is not an integer"
            )
        if expected < 0:
            return CommentHistory(
                (), False, False, "expected comment count is negative"
            )
        # The PR's comment count is fetched before the page scan. New comments
        # can legitimately appear in between, so only a short scan is
        # incomplete; pagination has already exhausted every page.
        if len(flattened) < expected:
            return CommentHistory(
                (),
                False,
                False,
                "comment pagination is incomplete: expected at least {} "
                "comments, received {}".format(expected, len(flattened)),
                expected,
            )

    def sort_key(item):
        comment_id, comment = item
        created = comment.get("created_at")
        if not isinstance(created, str) or not _ISO_TIMESTAMP_RE.match(created):
            created = ""
        return (created, comment_id)

    flattened.sort(key=sort_key)
    return CommentHistory(
        tuple(comment for _, comment in flattened),
        True,
        True,
        "comment pagination is complete and deterministically ordered",
        expected,
    )


def collection_from_pages(
    pages: Any,
    *,
    repository: Any,
    pull_number: Any,
    head_sha: Any,
    expected_count: Any = None,
    owner_login: str = "",
    trusted_associations: Sequence[str] = (),
    trusted_logins: Sequence[str] = (),
) -> Tuple[CommentHistory, ReceiptCollection]:
    """Normalize pages and read the exact-HEAD receipt history."""

    history = normalize_comment_pages(pages, expected_count=expected_count)
    if not history.complete:
        return history, ReceiptCollection(
            outcome=OUTCOME_INCOMPLETE,
            reason=history.reason,
        )
    kwargs = {}
    if trusted_associations:
        kwargs["trusted_associations"] = trusted_associations
    if trusted_logins:
        kwargs["trusted_logins"] = trusted_logins
    collection = read_receipts(
        history.comments,
        repository=repository,
        pull_number=pull_number,
        head_sha=head_sha,
        history_complete=True,
        history_ordered=True,
        expected_comment_count=(
            len(history.comments) if history.expected_count is not None else None
        ),
        owner_login=owner_login,
        **kwargs
    )
    return history, collection


def normalize_run_pages(
    pages: Any,
    *,
    expected_count: Any = None,
) -> RunInventory:
    """Flatten ``gh api --paginate --slurp`` run pages into one inventory.

    A single page object (``{"total_count": n, "workflow_runs": [...]}``) is
    also accepted. Malformed pages, duplicate run IDs, or fewer runs than the
    independently reported ``total_count`` are incomplete: they can never be
    used as proof that a dispatch produced no run.
    """

    if pages is None:
        return RunInventory((), False, "workflow run pages are unavailable")
    if isinstance(pages, Mapping):
        materialized_pages = [pages]
    elif isinstance(pages, Sequence) and not isinstance(
        pages, (str, bytes, bytearray)
    ):
        materialized_pages = list(pages)
    else:
        return RunInventory((), False, "workflow run pages are not an array")

    flattened = []
    seen_ids = set()
    reported_total = None
    for page in materialized_pages:
        if not isinstance(page, Mapping):
            return RunInventory((), False, "a workflow run page is not an object")
        runs = page.get("workflow_runs")
        if not isinstance(runs, Sequence) or isinstance(
            runs, (str, bytes, bytearray)
        ):
            return RunInventory(
                (), False, "a workflow run page has no run array"
            )
        raw_total = page.get("total_count")
        if raw_total is not None and not isinstance(raw_total, bool):
            try:
                parsed_total = int(str(raw_total).strip())
            except (TypeError, ValueError):
                parsed_total = None
            if parsed_total is not None and parsed_total >= 0:
                if reported_total is None or parsed_total > reported_total:
                    reported_total = parsed_total
        for run in runs:
            if not isinstance(run, Mapping):
                return RunInventory((), False, "a workflow run entry is not an object")
            raw_id = run.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                try:
                    raw_id = int(str(raw_id))
                except (TypeError, ValueError):
                    raw_id = 0
            if raw_id <= 0:
                return RunInventory((), False, "a workflow run has no usable ID")
            if raw_id in seen_ids:
                return RunInventory(
                    (), False, "workflow run {!r} appears on more than one page".format(raw_id)
                )
            title = run.get("display_title")
            if not isinstance(title, str) or not title.strip():
                # Attempt correlation reads the marker from the run title. A run
                # without one cannot be excluded as the accepted repair, so an
                # inventory containing it is not proof that no run exists.
                return RunInventory(
                    (), False, "a workflow run has no usable display title"
                )
            seen_ids.add(raw_id)
            flattened.append((raw_id, run))

    expected = None
    candidate = expected_count if expected_count is not None else reported_total
    if candidate is not None:
        if isinstance(candidate, bool):
            return RunInventory((), False, "expected run count is not an integer")
        try:
            expected = int(str(candidate).strip())
        except (TypeError, ValueError):
            return RunInventory((), False, "expected run count is not an integer")
        if expected < 0:
            return RunInventory((), False, "expected run count is negative")
        if len(flattened) < expected:
            return RunInventory(
                (),
                False,
                "workflow run pagination is incomplete: expected at least {} "
                "runs, received {}".format(expected, len(flattened)),
                expected,
            )

    def sort_key(item):
        run_id, run = item
        created = run.get("created_at")
        if not isinstance(created, str) or not _ISO_TIMESTAMP_RE.match(created):
            created = ""
        return (created, run_id)

    flattened.sort(key=sort_key)
    return RunInventory(
        tuple(run for _, run in flattened),
        True,
        "workflow run pagination is complete and deterministically ordered",
        expected,
    )


def has_unattributed_repair_run(
    inventory: RunInventory,
    *,
    pull_number: Any = None,
    since: Optional[str] = None,
    attempt_id: Any = None,
) -> bool:
    """Whether a repair run exists that the receipt cannot attribute.

    Older installed callers render ``OpenCode repair PR #<n>`` without the
    correlation marker. If such a run exists for this PR after the claim was
    observed, the evidence cannot distinguish our dispatch from a manual one,
    so recovery must hold rather than dispatch again.

    When ``attempt_id`` is supplied, a *marked* run for the same PR whose
    marker names another attempt is also unattributable: the receipt history
    cannot explain it, and re-arming the stale claim would risk a duplicate
    repair for the same HEAD. Without ``attempt_id`` the previous marker-blind
    behavior is retained for callers that only classify legacy runs.
    """

    wanted_pr = None
    if pull_number is not None:
        try:
            wanted_pr = int(str(pull_number).strip())
        except (TypeError, ValueError):
            raise AdmissionError("pull number is not an integer")
    wanted_attempt = str(attempt_id or "").strip()
    threshold = str(since or "").strip()
    if threshold and not _ISO_TIMESTAMP_RE.match(threshold):
        threshold = ""

    for run in inventory.runs:
        title = str(run.get("display_title") or "")
        marker = _ATTEMPT_RUN_MARKER_RE.search(title)
        if marker:
            pull = _PULL_TITLE_RE.match(title)
            if not pull:
                # A marker outside the canonical repair title is not a run this
                # controller can attribute at all; never guess by recency.
                continue
            if wanted_pr is not None and int(pull.group("pr")) != wanted_pr:
                continue
            if not wanted_attempt:
                # Preserve the marker-blind legacy classification for callers
                # that do not name the attempt under recovery.
                continue
            if marker.group("attempt") != wanted_attempt:
                created = str(run.get("created_at") or "")
                if threshold:
                    if not _ISO_TIMESTAMP_RE.match(created):
                        return True
                    if created < threshold:
                        continue
                return True
            continue
        match = _UNMARKED_REPAIR_TITLE_RE.match(title)
        if not match:
            continue
        if wanted_pr is not None and int(match.group(1)) != wanted_pr:
            continue
        created = str(run.get("created_at") or "")
        if threshold:
            if not _ISO_TIMESTAMP_RE.match(created):
                # A legacy-shaped repair run with an unknown creation time
                # cannot be proved older than the claim. Hold rather than
                # treating it as absent and risking a duplicate dispatch.
                return True
            if created < threshold:
                continue
        return True
    return False


def attempt_observed_at(
    comments: Iterable[Mapping[str, Any]],
    attempt_id: Any,
    *,
    repository: Any,
    pull_number: Any,
    head_sha: Any,
    owner_login: str = "",
    trusted_associations: Sequence[str] = (),
    trusted_logins: Sequence[str] = (),
) -> Optional[str]:
    """Return the earliest trusted comment timestamp for one attempt.

    The receipt itself is immutable and has no timestamp field; the comment
    creation time is the durable observation clock. This returns the *first*
    observation (the claim), because a run created by this attempt can never
    predate its claim. A later transition comment is written after the dispatch
    request, so using it as a ``created >=`` lower bound would hide a run that
    was created between the claim and that comment, permanently preventing
    correlation. Untrusted lookalikes are ignored exactly as in
    :func:`read_receipts`.
    """

    wanted = str(attempt_id or "").strip()
    if not wanted:
        return None
    earliest = None
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or RECEIPT_MARKER not in body:
            continue
        if not is_trusted_receipt_comment(
            comment,
            owner_login=owner_login,
            trusted_associations=(
                trusted_associations
                if trusted_associations
                else ("OWNER",)
            ),
            trusted_logins=(
                trusted_logins
                if trusted_logins
                else ("github-actions[bot]",)
            ),
        ):
            continue
        try:
            receipts = parse_receipts(body)
        except ValueError:
            continue
        if not any(receipt.attempt_id == wanted for receipt in receipts):
            continue
        created = comment.get("created_at")
        if isinstance(created, str) and _ISO_TIMESTAMP_RE.match(created):
            if earliest is None or created < earliest:
                earliest = created
    return earliest


def timestamp_is_stale(
    observed_at: Optional[str],
    *,
    now: Any = None,
    timeout_seconds: Any,
) -> bool:
    """Whether a claim observation is older than its bounded recovery window."""

    if not observed_at or not _ISO_TIMESTAMP_RE.match(str(observed_at)):
        return False
    try:
        timeout = int(str(timeout_seconds).strip())
    except (TypeError, ValueError):
        return False
    if timeout <= 0:
        return False
    observed = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        text = str(now).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError:
            return False
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    return (current - observed).total_seconds() >= timeout


# ---------------------------------------------------------------------------
# Dispatch response classification.
# ---------------------------------------------------------------------------

def _response_payload(body: Any) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        return body
    text = str(body or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _positive_run_id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _canonical_run_url(repository: str, run_id: int) -> str:
    return "https://github.com/{}/actions/runs/{}".format(repository, run_id)


def classify_dispatch_response(
    claimed: RepairReceipt,
    *,
    http_status: Any = None,
    response_body: Any = None,
    network_error: bool = False,
) -> DispatchOutcome:
    """Classify a workflow-dispatch attempt without ever inventing acceptance.

    ``200`` may carry ``workflow_run_id``/``workflow_run_url``; ``204`` is the
    legacy accepted response without run details. A definite 4xx rejection
    produces an immutable ``rejected`` receipt. Timeout/network/403/429/5xx
    produce ``ambiguous``: they must be correlated against actual runs before
    any retry.
    """

    if claimed.status not in (
        STATUS_CLAIMED,
        STATUS_REQUESTING,
        STATUS_AMBIGUOUS,
    ):
        raise AdmissionError(
            "dispatch outcomes may only follow a claimed, requesting, or "
            "ambiguous attempt"
        )

    status = None
    if http_status is not None and not isinstance(http_status, bool):
        try:
            status = int(str(http_status).strip())
        except (TypeError, ValueError):
            status = None

    if network_error or status is None:
        return DispatchOutcome(
            kind=DISPATCH_AMBIGUOUS,
            receipt=transition_receipt(claimed, status=STATUS_AMBIGUOUS),
            reason=(
                "workflow dispatch acceptance is unknown (network/interrupted "
                "response); reconcile the attempt correlation ID against live "
                "runs instead of retrying blindly"
            ),
            ambiguous=True,
        )

    payload = _response_payload(response_body)
    run_id = _positive_run_id(
        payload.get("workflow_run_id")
        if "workflow_run_id" in payload
        else payload.get("run_id")
    )

    if 200 <= status < 300:
        if run_id is not None:
            receipt = transition_receipt(
                claimed,
                status=STATUS_DISPATCHED,
                run_id=run_id,
                run_url=_canonical_run_url(claimed.repository, run_id),
            )
            return DispatchOutcome(
                kind=DISPATCH_ACCEPTED,
                receipt=receipt,
                reason="workflow dispatch accepted with run {}".format(run_id),
                accepted=True,
            )
        return DispatchOutcome(
            kind=DISPATCH_ACCEPTED,
            receipt=transition_receipt(claimed, status=STATUS_DISPATCHED),
            reason=(
                "workflow dispatch accepted without run details (legacy 204); "
                "the attempt is still spent and must never be dispatched again"
            ),
            accepted=True,
        )

    if status in DEFINITE_REJECTION_STATUSES:
        return DispatchOutcome(
            kind=DISPATCH_REJECTED,
            receipt=transition_receipt(claimed, status=STATUS_REJECTED),
            reason=(
                "workflow dispatch was definitively rejected with HTTP {}; "
                "only this immutable attempt is rejected, a later serialized "
                "owner may claim a fresh attempt ID".format(status)
            ),
            rejected=True,
        )

    return DispatchOutcome(
        kind=DISPATCH_AMBIGUOUS,
        receipt=transition_receipt(claimed, status=STATUS_AMBIGUOUS),
        reason=(
            "workflow dispatch outcome is ambiguous (HTTP {}); reconcile the "
            "attempt correlation ID and live run inventory before retrying"
        ).format(status),
        ambiguous=True,
    )


# ---------------------------------------------------------------------------
# Actual-run correlation.
# ---------------------------------------------------------------------------

def _runs_list(payload: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        runs = payload.get("workflow_runs")
    else:
        runs = payload
    if runs is None:
        return ()
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes, bytearray)):
        raise AdmissionError("workflow run inventory is not an array")
    return tuple(run for run in runs if isinstance(run, Mapping))


def select_correlated_run(
    payload: Any,
    *,
    attempt_id: Any,
    pull_number: Any = None,
) -> Optional[Mapping[str, Any]]:
    """Return the one actual run carrying this attempt's correlation marker.

    Multiple matching run IDs are corrupt/ambiguous evidence and raise instead
    of choosing one. A run with no marker is never correlated by recency: that
    would let an unrelated manual run authorize a retry.
    """

    wanted = str(attempt_id or "").strip()
    if not wanted:
        raise AdmissionError("attempt ID is required for run correlation")
    wanted_pr = None
    if pull_number is not None:
        try:
            wanted_pr = int(str(pull_number).strip())
        except (TypeError, ValueError):
            raise AdmissionError("pull number is not an integer")

    matches = {}
    for run in _runs_list(payload):
        run_id = _positive_run_id(run.get("id"))
        title = run.get("display_title")
        if run_id is None or not isinstance(title, str):
            continue
        marker = _ATTEMPT_RUN_MARKER_RE.search(title)
        if not marker or marker.group("attempt") != wanted:
            continue
        pull = _PULL_TITLE_RE.match(title)
        if wanted_pr is not None and pull and int(pull.group("pr")) != wanted_pr:
            continue
        matches[run_id] = {
            "id": run_id,
            "display_title": title,
            "status": str(run.get("status") or ""),
            "conclusion": str(run.get("conclusion") or ""),
            "html_url": str(run.get("html_url") or ""),
            "created_at": str(run.get("created_at") or ""),
            "head_sha": str(run.get("head_sha") or ""),
        }

    if len(matches) > 1:
        raise AdmissionError(
            "multiple actual runs claim attempt {!r}: {}".format(
                wanted, ", ".join(str(key) for key in sorted(matches))
            )
        )
    if not matches:
        return None
    return matches[next(iter(matches))]


def correlated_run_url(repository: Any, run: Mapping[str, Any]) -> str:
    """Return a canonical run URL for a selected actual run."""

    number = _positive_run_id(run.get("id"))
    if number is None:
        raise AdmissionError("correlated run has no positive ID")
    return _canonical_run_url(str(repository).strip().lower(), number)


def run_actor_logins(run: Any) -> frozenset:
    """Return the normalized logins GitHub reports as a run's actors."""

    if not isinstance(run, Mapping):
        return frozenset()
    logins = set()
    for key in ("actor", "triggering_actor"):
        actor = run.get(key)
        if not isinstance(actor, Mapping):
            continue
        login = actor.get("login")
        if isinstance(login, str) and login.strip():
            logins.add(login.strip().lower())
    return frozenset(logins)


def run_repairs_pull(run: Any, pull_number: Any) -> bool:
    """Whether a run title is the canonical CI-repair title for ``pull_number``.

    A marked title is a repair run for the PR named before its attempt marker.
    A legacy title has no marker but still names the PR. This is only a title
    check; callers must also require the run actor before treating it as proof.
    """

    if not isinstance(run, Mapping):
        return False
    try:
        wanted = int(str(pull_number).strip())
    except (TypeError, ValueError):
        raise AdmissionError("pull number is not an integer")
    title = run.get("display_title")
    if not isinstance(title, str):
        return False
    if _ATTEMPT_RUN_MARKER_RE.search(title):
        pull = _PULL_TITLE_RE.match(title)
        return bool(pull) and int(pull.group("pr")) == wanted
    legacy = _UNMARKED_REPAIR_TITLE_RE.match(title)
    return bool(legacy) and int(legacy.group(1)) == wanted


def run_proves_historical_receipt(
    run: Any,
    receipt: Any,
    *,
    author_login: Any = "",
    allow_dispatch_ref: bool = True,
) -> bool:
    """Whether GitHub's own run record proves an otherwise untrusted receipt.

    ``TAP_PAT`` can be rotated to another account. A receipt the previous
    account published must not become an ignored lookalike, or the exact HEAD
    it already spent looks unused and admission dispatches it a second time.
    A ``claimed``, ``requesting``, or ``ambiguous`` receipt can remain when
    outcome persistence fails after an accepted dispatch, so it is proved by
    the same run's attempt marker and actor rather than by a run ID that was
    never stored.

    A ``workflow_dispatch`` run records its dispatch ref (normally ``main``) as
    ``head_sha``, not the admitted PR HEAD. A run-name attempt marker, and an
    exact run ID when the receipt persisted one, bind that run to the receipt
    even when the SHA differs. A legacy unmarked title has no such binding, so
    it still has to name the same exact HEAD. The comment author alone is
    never sufficient, so an unrelated commenter cannot borrow a run to
    suppress or authorize a repair.
    """

    if not isinstance(run, Mapping) or not isinstance(receipt, RepairReceipt):
        return False
    if receipt.status not in (
        STATUS_CLAIMED,
        STATUS_REQUESTING,
        STATUS_AMBIGUOUS,
        STATUS_DISPATCHED,
    ):
        return False
    run_id = _positive_run_id(run.get("id"))
    if run_id is None:
        return False
    if receipt.run_id is not None:
        if receipt.status != STATUS_DISPATCHED or run_id != receipt.run_id:
            return False
    head_sha = str(run.get("head_sha") or "").strip().lower()
    if not allow_dispatch_ref and head_sha != receipt.head_sha:
        return False
    author = str(author_login or "").strip().lower()
    if not author or author not in run_actor_logins(run):
        return False
    title = run.get("display_title")
    if not isinstance(title, str):
        return False
    marker = _ATTEMPT_RUN_MARKER_RE.search(title)
    if marker:
        if not run_repairs_pull(run, receipt.pull_number):
            return False
        return marker.group("attempt") == receipt.attempt_id
    # Legacy callers predate the attempt marker. Their canonical repair title
    # is still bound to this action (same actor, same exact HEAD, same PR), so
    # it proves the historical writer even when a legacy 204 persisted no run
    # ID. A different PR title or another account never matches.
    legacy = _UNMARKED_REPAIR_TITLE_RE.match(title)
    return (
        bool(legacy)
        and int(legacy.group(1)) == receipt.pull_number
        and head_sha == receipt.head_sha
    )


def run_proves_dispatched_receipt(
    run: Any,
    receipt: Any,
    *,
    author_login: Any = "",
) -> bool:
    """Whether GitHub's run record proves a ``dispatched`` receipt's run.

    This retains the original strict contract: the receipt must already carry
    the proved run ID and the run must name the same exact HEAD. Use
    :func:`run_proves_historical_receipt` for claimed, ambiguous, or
    run-detail-less dispatched receipts.
    """

    if not isinstance(receipt, RepairReceipt):
        return False
    if receipt.status != STATUS_DISPATCHED or receipt.run_id is None:
        return False
    return run_proves_historical_receipt(
        run,
        receipt,
        author_login=author_login,
        allow_dispatch_ref=False,
    )


def run_marker(attempt_id: Any) -> str:
    """Render the run-name correlation marker for one attempt."""

    attempt = str(attempt_id or "").strip()
    marker = ATTEMPT_RUN_MARKER_TEMPLATE.format(attempt=attempt)
    if not _ATTEMPT_RUN_MARKER_RE.fullmatch(marker):
        raise AdmissionError("attempt ID cannot be used as a run marker")
    return marker


# ---------------------------------------------------------------------------
# Admission planning.
# ---------------------------------------------------------------------------

def plan_admission(
    collection: ReceiptCollection,
    *,
    runs: Optional[RunInventory] = None,
    observed_at: Optional[str] = None,
    now: Any = None,
    claim_timeout_seconds: Any = 0,
    pull_number: Any = None,
) -> AdmissionPlan:
    """Plan claim/wait/hold/rearm for one serialized exact-HEAD admission pass.

    A new claim is permitted only by the phase-1 fail-closed decision. An
    existing claimed/requesting/ambiguous attempt is first correlated with its
    actual run; a proved run advances it to ``dispatched``. A ``dispatched``
    attempt may be enriched with run details but is otherwise a wait.

    An attempt that outlives the bounded dispatch window is handled by
    evidence, never by a blind retry:

    * only a stale ``claimed`` attempt proves the dispatch request was never
      sent, because the owner persists ``requesting`` immediately before that
      request. A complete run inventory with no correlation marker and no
      legacy-shaped repair run then persists the dead claim as ``rejected`` so
      the same serialized pass may claim a fresh attempt (``rearm``);
    * a stale ``requesting`` attempt may already have reached GitHub even when
      its run is absent from the inventory (runs can be deleted), so it is
      persisted as a visible ``ambiguous`` hold instead of being re-armed;
    * an unreadable/partial inventory, or any repair run that cannot be
      attributed to this attempt, also marks the attempt a visible
      ``ambiguous`` hold.

    An ``ambiguous`` attempt is never re-armed automatically: it may only be
    reconciled to a proved run, or stay a monitored hold.
    """

    decision = decide_new_attempt(collection)
    if decision.allowed:
        return AdmissionPlan(
            action=ACTION_CLAIM,
            reason=decision.reason,
        )
    if decision.action not in (ACTION_WAIT, ACTION_HOLD):
        return AdmissionPlan(
            action=ACTION_HOLD,
            reason="unknown admission action {!r}: failing closed".format(
                decision.action
            ),
        )

    attempt = attempt_for_id(collection, decision.attempt_id)
    if attempt is None:
        return AdmissionPlan(
            action=ACTION_HOLD,
            reason="receipt decision named an attempt that is not in history",
            attempt_id=decision.attempt_id,
        )

    complete_inventory = runs is not None and runs.complete
    if attempt.status in (
        STATUS_CLAIMED,
        STATUS_REQUESTING,
        STATUS_AMBIGUOUS,
    ) and complete_inventory:
        try:
            run = select_correlated_run(
                runs.runs,
                attempt_id=attempt.attempt_id,
                pull_number=pull_number,
            )
        except AdmissionError as exc:
            return AdmissionPlan(
                action=ACTION_HOLD,
                reason=str(exc),
                attempt_id=attempt.attempt_id,
            )
        if run is not None:
            receipt = transition_attempt(
                attempt,
                status=STATUS_DISPATCHED,
                run_id=run["id"],
                run_url=correlated_run_url(
                    attempt.latest_receipt.repository, run
                ),
            )
            return AdmissionPlan(
                action=ACTION_WAIT,
                reason=(
                    "actual run {} proves attempt {!r} was accepted; recording "
                    "the dispatch instead of retrying".format(
                        run["id"], attempt.attempt_id
                    )
                ),
                attempt_id=attempt.attempt_id,
                transition=receipt,
                run_id=run["id"],
                run_url=receipt.run_url,
            )

    if (
        attempt.status == STATUS_DISPATCHED
        and attempt.run_id is None
        and complete_inventory
    ):
        try:
            run = select_correlated_run(
                runs.runs,
                attempt_id=attempt.attempt_id,
                pull_number=pull_number,
            )
        except AdmissionError as exc:
            return AdmissionPlan(
                action=ACTION_HOLD,
                reason=str(exc),
                attempt_id=attempt.attempt_id,
            )
        if run is not None:
            receipt = transition_attempt(
                attempt,
                status=STATUS_DISPATCHED,
                run_id=run["id"],
                run_url=correlated_run_url(attempt.latest_receipt.repository, run),
            )
            return AdmissionPlan(
                action=ACTION_WAIT,
                reason="enriched a legacy accepted dispatch with its actual run",
                attempt_id=attempt.attempt_id,
                transition=receipt,
                run_id=run["id"],
                run_url=receipt.run_url,
            )

    if attempt.status in (STATUS_CLAIMED, STATUS_REQUESTING) and timestamp_is_stale(
        observed_at,
        now=now,
        timeout_seconds=claim_timeout_seconds,
    ):
        if not complete_inventory:
            receipt = transition_attempt(attempt, status=STATUS_AMBIGUOUS)
            return AdmissionPlan(
                action=ACTION_HOLD,
                reason=(
                    "attempt {!r} exceeded the bounded dispatch window and the "
                    "workflow run inventory is unavailable/partial; it is now "
                    "a visible hold, not a retry".format(attempt.attempt_id)
                ),
                attempt_id=attempt.attempt_id,
                transition=receipt,
            )
        if has_unattributed_repair_run(
            runs,
            pull_number=pull_number,
            since=observed_at,
            attempt_id=attempt.attempt_id,
        ):
            receipt = transition_attempt(attempt, status=STATUS_AMBIGUOUS)
            return AdmissionPlan(
                action=ACTION_HOLD,
                reason=(
                    "attempt {!r} expired, but a repair run for this PR cannot "
                    "be attributed to it; holding instead of risking a "
                    "duplicate dispatch".format(attempt.attempt_id)
                ),
                attempt_id=attempt.attempt_id,
                transition=receipt,
            )
        if attempt.status == STATUS_REQUESTING:
            # ``requesting`` is persisted immediately before the dispatch
            # request. The request may have reached GitHub even though the run
            # has since been deleted, so absence of a run is not proof that no
            # repair started. Hold it visibly; only a proved run may advance it.
            receipt = transition_attempt(attempt, status=STATUS_AMBIGUOUS)
            return AdmissionPlan(
                action=ACTION_HOLD,
                reason=(
                    "attempt {!r} recorded a dispatch request and the complete "
                    "run inventory no longer proves whether it was accepted; "
                    "holding instead of re-arming a possible duplicate".format(
                        attempt.attempt_id
                    )
                ),
                attempt_id=attempt.attempt_id,
                transition=receipt,
            )
        # ``claimed`` is persisted before ``requesting`` and dispatch is only
        # attempted after ``requesting`` is durable, so this state proves the
        # request was never sent. The complete inventory must also show no run.
        receipt = transition_attempt(attempt, status=STATUS_REJECTED)
        return AdmissionPlan(
            action=ACTION_REARM,
            reason=(
                "claim {!r} exceeded the bounded dispatch window with no "
                "request marker and the complete run inventory proves no run "
                "was created; the dead claim is rejected and a fresh claim may "
                "be admitted".format(attempt.attempt_id)
            ),
            attempt_id=attempt.attempt_id,
            transition=receipt,
        )

    return AdmissionPlan(
        action=decision.action,
        reason=decision.reason,
        attempt_id=attempt.attempt_id,
    )


def is_definite_dispatch_rejection(
    http_status: Any,
    response_body: Any = None,
) -> bool:
    """Whether a response is a proven pre-acceptance rejection."""

    try:
        status = int(str(http_status).strip())
    except (TypeError, ValueError):
        return False
    return status in DEFINITE_REJECTION_STATUSES


def unexpected_dispatch_inputs(response_body: Any) -> bool:
    """Whether a 422 response specifically rejected undeclared dispatch inputs."""

    text = str(response_body or "").lower()
    return "unexpected input" in text or "unexpected inputs" in text


__all__ = [
    "DISPATCH_ACCEPTED",
    "DISPATCH_REJECTED",
    "DISPATCH_AMBIGUOUS",
    "ACTION_REARM",
    "DEFINITE_REJECTION_STATUSES",
    "ATTEMPT_RUN_MARKER_TEMPLATE",
    "AdmissionError",
    "CommentHistory",
    "RunInventory",
    "DispatchOutcome",
    "AdmissionPlan",
    "normalize_comment_pages",
    "normalize_run_pages",
    "collection_from_pages",
    "attempt_observed_at",
    "timestamp_is_stale",
    "classify_dispatch_response",
    "select_correlated_run",
    "correlated_run_url",
    "run_actor_logins",
    "run_repairs_pull",
    "run_proves_historical_receipt",
    "run_proves_dispatched_receipt",
    "has_unattributed_repair_run",
    "run_marker",
    "plan_admission",
    "is_definite_dispatch_rejection",
    "unexpected_dispatch_inputs",
]
