"""CodeRabbit review adapter.

A sibling provider behind the same normalized review-gate contract. CodeRabbit
publishes a commit status, a review verdict, inline threads, and a summary
comment; this adapter translates all of it into a `ProviderSnapshot` and the
generic gate decides the verdict.

Three surfaces CodeRabbit owns disagree with each other often enough to
deadlock a naive gate, so this adapter is where they are reconciled:

* the commit status is *operational* state (reviewing, rate limited, skipped),
  never merge authorization. A later `Review skipped` success must not erase an
  approval GitHub already stores as a durable review;
* review records are bound to the immutable head SHA they were submitted
  against, and a later decisive verdict supersedes earlier nitpick-only
  COMMENTED reviews on that same head;
* a settled finding is stated in prose inside CodeRabbit's own thread, while
  GitHub keeps `isResolved: false`. The prose is normalized into GitHub's own
  state, and anything ambiguous stays blocking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .findings import collect_summary_comments, parse_time, sanitize_title
from .snapshot import ProviderSnapshot
from .tracker import BOT_PREFIX, TRACKER_MARKER, VERDICT_MARKER

PROVIDER_NAME = "coderabbit"

DEFAULT_BOT_LOGIN = "coderabbitai[bot]"
DEFAULT_STATUS_CONTEXT = "CodeRabbit"

# CodeRabbit publishes advisory nitpicks as a COMMENTED review whose body counts
# the outstanding comments ("... reviewed 2 extra nitpick comments"). That count
# is what distinguishes a nitpick from the empty review shells CodeRabbit leaves
# when it confirms a single thread.
NITPICK_RE = re.compile(r"comments?\s*\(\s*[1-9]\d*\s*\)", re.I)

# A re-check answer. The machine token and the plain sentence
# ("Review thread resolved.") are the same statement, so one word-boundary
# pattern covers both. `\bresolved\b` never matches inside `unresolved`, and an
# explicit refusal anywhere in the body wins over any positive wording.
RESOLVED_REPLY_RE = re.compile(r"\bresolved\b", re.I)
UNRESOLVED_REPLY_RE = re.compile(r"\bunresolved\b|\bnot\s+resolved\b", re.I)
REPLY_RESOLVED = "RESOLVED"
REPLY_UNRESOLVED = "UNRESOLVED"

OUTPUT_MARKERS = ("coderabbit", "walkthrough", "review details", "nitpick")

# CodeRabbit's shared included-review quota is a repository-wide resource, so
# the queue must serialize against it. A rate-limit notice is the provider
# telling us when the next slot exists; the countdown is parsed here, once.
RATE_LIMIT_RE = re.compile(r"review rate limited|review limit reached", re.I)
FULL_REVIEW_COMMAND = "@coderabbitai full review"
_RETRY_WINDOW_RES = (
    re.compile(
        r"next\s+(?:included\s+)?review\b[^\n<.]{0,120}?\bavailable\b[^\n<.]{0,40}?"
        r"\b(?:in|after)\s+([^\n<.]+)",
        re.I,
    ),
    re.compile(r"(?:try|retry)\s+again\b[^\n<.]{0,40}?\b(?:in|after)\s+([^\n<.]+)", re.I),
)
_DURATION_RE = re.compile(r"(\d+)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)", re.I)


def parse_duration_ms(text: str) -> Optional[int]:
    """Milliseconds named by a provider countdown, or `None`."""

    if re.search(r"less than\s+(?:a|one)\s+minute", text or "", re.I):
        return 60_000
    total = 0
    found = False
    for match in _DURATION_RE.finditer(text or ""):
        found = True
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("hour") or unit.startswith("hr"):
            total += value * 3_600_000
        elif unit.startswith("minute") or unit.startswith("min"):
            total += value * 60_000
        else:
            total += value * 1_000
    return total if found else None


def parse_retry_delay_ms(body: str) -> Optional[int]:
    """Countdown published in a rate-limit notice, or `None` when absent."""

    for pattern in _RETRY_WINDOW_RES:
        match = pattern.search(body or "")
        if match:
            parsed = parse_duration_ms(match.group(1))
            if parsed is not None:
                return parsed
    return None


def is_full_review(review: Dict[str, Any], bot_login: str = DEFAULT_BOT_LOGIN) -> bool:
    """True when a CodeRabbit review consumed a full included review.

    An empty COMMENTED review is a thread confirmation, not quota use.
    """

    if not _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login):
        return False
    if review.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
        return True
    return review.get("state") == "COMMENTED" and bool((review.get("body") or "").strip())


def rate_limit_cooldown(
    client: Any,
    pr_number: int,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
) -> Tuple[int, str]:
    """Newest published retry window for this pull request, as `(until_ms, reason)`."""

    comments = client.list_issue_comments(int(pr_number))
    windows: List[Tuple[int, int]] = []
    for comment in comments or []:
        if not _matches_bot(((comment.get("user") or {}).get("login") or ""), bot_login):
            continue
        if not RATE_LIMIT_RE.search(comment.get("body") or ""):
            continue
        delay = parse_retry_delay_ms(comment.get("body") or "")
        published = parse_time(comment.get("updated_at") or comment.get("created_at"))
        if delay is None or published is None:
            continue
        at = int(published.timestamp() * 1000)
        windows.append((at + delay, at))
    if not windows:
        return 0, ""
    until, _at = max(windows)
    return until, "provider rate limit"


def queue_request(
    settings: Any,
    *,
    pr_number: int,
    head_sha: str = "",
    kind: str = "initial",
) -> Any:
    """CodeRabbit is asked for a full review with a fixed bot mention."""

    from .providers import REQUEST_COMMENT, QueueRequest

    del settings, pr_number, head_sha
    return QueueRequest(kind=REQUEST_COMMENT, provider=PROVIDER_NAME, body=FULL_REVIEW_COMMAND)

# Sections whose bullets can be actionable. The walkthrough and the file table
# describe the diff; they never produce findings on their own.
FINDING_SECTION_HEADINGS = (
    "review details",
    "nitpick",
    "issue",
    "concern",
    "bug",
    "vulnerability",
    "additional comments",
)
BOILERPLATE_TITLE_PHRASES = (
    "looks good",
    "no actionable",
    "no major issues",
    "lgtm",
    "preliminary",
    "walkthrough",
)

_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.*)\s*$")
_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*+])\s+(.{10,300})", re.M)


def is_boilerplate_title(title: str) -> bool:
    lowered = (title or "").lower()
    return any(phrase in lowered for phrase in BOILERPLATE_TITLE_PHRASES)


def select_finding_text(body: str) -> str:
    matches = list(_HEADING_RE.finditer(body or ""))
    if not matches:
        return body or ""
    kept: List[str] = []
    for index, match in enumerate(matches):
        heading = (match.group(1) or "").lower()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if any(phrase in heading for phrase in FINDING_SECTION_HEADINGS):
            kept.append(body[match.start() : end])
    return "".join(kept)


def parse_summary(body: str) -> List[str]:
    text = select_finding_text(body or "")
    if not text.strip():
        return []
    titles: List[str] = []
    seen = set()
    for match in _BULLET_RE.finditer(text):
        title = sanitize_title(match.group(1))
        if len(title) < 10 or is_boilerplate_title(title):
            continue
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles


def _matches_bot(login: str, bot_login: str) -> bool:
    left = (login or "").lower()
    right = (bot_login or "").lower()
    if not left:
        return False
    return left == right or left.startswith(right.split("[", 1)[0])


def latest_status(statuses: Sequence[Dict[str, Any]], context: str) -> Optional[Dict[str, Any]]:
    matching = [
        status
        for status in statuses or []
        if (status.get("context") or "").lower() == (context or "").lower()
    ]
    if not matching:
        return None
    return sorted(
        matching,
        key=lambda status: str(status.get("updated_at") or status.get("created_at") or ""),
        reverse=True,
    )[0]


def review_order(review: Dict[str, Any]) -> Tuple[int, int]:
    """Total order over reviews of one pull request: submission time, then id."""

    submitted = parse_time(review.get("submitted_at") or review.get("created_at"))
    return (
        int(submitted.timestamp() * 1_000_000) if submitted is not None else 0,
        int(review.get("id") or 0),
    )


@dataclass(frozen=True)
class HeadVerdict:
    """CodeRabbit's durable opinion about one exact PR head."""

    decision: Optional[Dict[str, Any]] = None
    nitpicks: Tuple[Dict[str, Any], ...] = ()
    blocking_nitpicks: Tuple[Dict[str, Any], ...] = ()

    @property
    def state(self) -> str:
        return str((self.decision or {}).get("state") or "")

    @property
    def approved(self) -> bool:
        return self.state == "APPROVED"


def head_verdict(
    reviews: Sequence[Dict[str, Any]],
    bot_login: str = DEFAULT_BOT_LOGIN,
    head_sha: str = "",
) -> HeadVerdict:
    """Decisive verdict plus advisory nitpicks bound to one exact head SHA.

    GitHub keeps every review record forever, so a verdict only counts for the
    immutable head SHA it was submitted against: an approval left behind by an
    earlier commit must never authorize a newer one, and no CI/review timestamp
    ordering is needed because both signals already name the same SHA.

    Nitpicks are advisory. A decisive review newer than them supersedes them —
    that is the bot acknowledging the same head once the nitpicks were dealt
    with — while a nitpick published after the decisive review is new
    information that still blocks.
    """

    candidates = [
        review
        for review in reviews or []
        if _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login)
        and (review.get("commit_id") or "").lower() == (head_sha or "").lower()
        and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
    ]
    decisive = sorted(
        (
            review
            for review in candidates
            if review.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        ),
        key=review_order,
    )
    decision = decisive[-1] if decisive else None
    nitpicks = sorted(
        (
            review
            for review in candidates
            if review.get("state") == "COMMENTED"
            and NITPICK_RE.search(str(review.get("body") or ""))
        ),
        key=review_order,
    )
    # With no decisive review every nitpick is the newest word on this head.
    blocking = (
        tuple(nitpicks)
        if decision is None
        else tuple(nitpick for nitpick in nitpicks if review_order(nitpick) > review_order(decision))
    )
    return HeadVerdict(
        decision=decision,
        nitpicks=tuple(nitpicks),
        blocking_nitpicks=blocking,
    )


def status_reason(status: Optional[Dict[str, Any]], context: str) -> Optional[str]:
    """Why the commit status says this head is not reviewed yet, else `None`.

    The commit status is operational state, not authorization. `success` is
    therefore never a reason on its own — it says the provider finished doing
    something (a review, or nothing at all), never that the diff is clean — and
    an operational state can never revoke a durable exact-head approval.
    """

    if status is None:
        return (
            f"the review provider reported no {context!r} status for this HEAD, "
            "so the diff is unverified"
        )
    state = str(status.get("state") or "")
    description = str(status.get("description") or "")
    if RATE_LIMIT_RE.search(description):
        return (
            f"the {context!r} status for this HEAD reports a review rate limit: "
            f"{description[:200]}"
        )
    if state == "success":
        return None
    if state == "pending":
        return f"the {context!r} status for this HEAD is still pending: {description[:200]}"
    return f"the {context!r} status for this HEAD is {state or 'unknown'}: {description[:200]}"


#: A run of fenced-code markers, as in ```` ```python ````. Matched on its own so
#: an ordinary backtick in prose cannot open or close a fence.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_DETAILS_OPEN_RE = re.compile(r"^\s*<details\b", re.I)
_DETAILS_CLOSE_RE = re.compile(r"^\s*</details\s*>", re.I)
#: An inline code span, which is code even in the middle of a sentence.
_INLINE_CODE_RE = re.compile(r"`+[^`]*`+")


def _verdict_text(body: str) -> str:
    """The part of a reply that states a verdict, and nothing else.

    A provider reply is Markdown, and Markdown has places where a reader expects
    a *quotation* rather than a statement: fenced code, indented code, inline
    code, block quotes, and collapsible ``<details>`` blocks. A finding body
    quoted inside any of them is evidence about the finding, not about whether it
    is fixed. CodeRabbit re-quotes the original comment when it re-checks one, so
    the same word can appear in the same reply in both roles.

    Searching the whole body therefore makes the parser read a quotation as a
    verdict, and it fails in whichever direction the quote happens to point: an
    old `unresolved` quoted above a real resolution reads as blocking, and a
    `resolved` inside a fenced diff reads as a fix. Neither is a decision the
    provider made, and one of them can drop a live finding.
    """

    text = body or ""
    kept: List[str] = []
    in_fence = False
    in_details = False
    for raw in text.splitlines():
        line = raw.strip()
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _DETAILS_OPEN_RE.match(line):
            in_details = True
            continue
        if in_details:
            # The closing tag is the only part of the block worth acting on.
            if _DETAILS_CLOSE_RE.match(line):
                in_details = False
            continue
        # A block quote and an indented block are quotations in their entirety, so
        # the line is dropped rather than unmarked: stripping the `>` would leave
        # the quoted verdict looking like the reply's own statement, which is the
        # mistake this function exists to prevent.
        quoted = raw.lstrip()
        if quoted.startswith(">") or quoted.startswith("    "):
            continue
        # Inline code anywhere in the line is code, not a verdict, and removing
        # only a surrounding pair would leave `resolved` mid-sentence reading as
        # a statement.
        stripped = _INLINE_CODE_RE.sub(" ", raw).strip()
        if stripped:
            kept.append(stripped)
    return "\n".join(kept)


def classify_reply(body: str) -> str:
    """One verdict token for a provider reply, or `""` when it states none.

    Three rules, and each is about refusing to over-read:

    * Only the reply's own prose is searched. See :func:`_verdict_text` for why a
      quoted, fenced or inline `unresolved` must not read as a verdict.

    * A reply that only *asks* whether something is resolved states no verdict, so
      the lines that carry a question are dropped before the positive form is
      accepted. Guessing in that direction would let a fixed-looking finding be
      dropped, which is the one failure mode the gate cannot recover from.

    * The *last* verdict in the prose wins. A re-check quotes the earlier verdict
      and then states the current one, so reading first-match would let the
      quotation decide instead of the reply. Where a single statement carries both
      forms ("fixed, but unresolved for now") the negative wins, because a reply
      that contradicts itself has not settled it and an over-reported finding is
      recoverable while a dropped one is not.
    """

    verdicts: List[str] = []
    for line in _verdict_text(body).splitlines():
        if "?" in line:
            continue
        if UNRESOLVED_REPLY_RE.search(line):
            verdicts.append(REPLY_UNRESOLVED)
        elif RESOLVED_REPLY_RE.search(line):
            verdicts.append(REPLY_RESOLVED)
    if not verdicts:
        return ""
    if verdicts[-1] == REPLY_UNRESOLVED:
        # A self-contradicting final line stays blocking. Checked against the
        # whole line rather than the last match, so "fixed but unresolved" is not
        # read as a fix on the strength of the first word.
        return REPLY_UNRESOLVED
    return REPLY_RESOLVED


def thread_reply_verdict(
    thread: Dict[str, Any], bot_login: str = DEFAULT_BOT_LOGIN
) -> str:
    """Verdict of the newest provider reply in a provider-authored thread.

    Only a thread the provider opened is read: a human thread that happens to
    contain the word "resolved" is not provider output.
    """

    comments = list(thread.get("comments") or [])
    if not comments:
        return ""
    if not _matches_bot(str(comments[0].get("author") or ""), bot_login):
        return ""
    verdict = ""
    for comment in comments[1:]:
        if not _matches_bot(str(comment.get("author") or ""), bot_login):
            continue
        verdict = classify_reply(str(comment.get("body") or ""))
    return verdict


@dataclass(frozen=True)
class ThreadNormalization:
    """What thread normalization decided, and what it could not decide."""

    known: bool = True
    resolved: Tuple[str, ...] = ()
    blocking: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "known": self.known,
            "semantically_resolved": len(self.resolved),
            "blocking": len(self.blocking),
            "normalization_failed": len(self.failed),
        }


def normalize_threads(
    client: Any,
    pr_number: int,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    apply: bool = False,
) -> Tuple[Optional[Set[int]], ThreadNormalization]:
    """Unresolved comment ids, with provider-answered threads settled.

    CodeRabbit re-checks a repaired finding by replying inside its own thread
    and saying so in prose; GitHub keeps `isResolved: false` until somebody
    flips the flag. The gate used to trust only the flag, so an already-fixed
    finding was re-requested forever and the merge gate deadlocked. When the
    newest provider reply explicitly says RESOLVED, GitHub's own state is
    normalized with the workflow token so both surfaces agree.

    An explicit UNRESOLVED always stays blocking, and a normalization that
    fails also stays blocking: an unverifiable thread may over-report a
    finding, but it may never silently drop one. `apply=False` predicts the
    outcome without writing, which is what the deterministic tests read.
    """

    threads = client.review_threads(int(pr_number))
    if threads is None:
        return None, ThreadNormalization(known=False)

    unresolved: Set[int] = set()
    resolved: List[str] = []
    blocking: List[str] = []
    failed: List[str] = []
    for thread in threads:
        if thread.get("is_resolved") or thread.get("is_outdated"):
            continue
        database_ids = {
            int(comment["database_id"])
            for comment in (thread.get("comments") or [])
            if comment.get("database_id") is not None
        }
        thread_id = str(thread.get("id") or "")
        if thread_reply_verdict(thread, bot_login) == REPLY_RESOLVED and thread_id:
            if apply:
                try:
                    client.resolve_review_thread(thread_id)
                except Exception:  # noqa: BLE001 - an unknown outcome fails closed
                    failed.append(thread_id)
                    unresolved.update(database_ids)
                    continue
            resolved.append(thread_id)
            continue
        blocking.append(thread_id or str(thread.get("path") or "thread"))
        unresolved.update(database_ids)
    return unresolved, ThreadNormalization(
        known=True,
        resolved=tuple(resolved),
        blocking=tuple(blocking),
        failed=tuple(failed),
    )


def collect(
    client: Any,
    pr_number: int,
    head_sha: str,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    status_context: str = DEFAULT_STATUS_CONTEXT,
    apply: bool = False,
) -> ProviderSnapshot:
    """Read CodeRabbit's status, verdict, threads, and summary for this PR."""

    issue_comments = client.list_issue_comments(int(pr_number))
    review_comments = client.list_review_comments(int(pr_number))
    summary_comments = collect_summary_comments(
        issue_comments,
        bot_logins=(bot_login,),
        markers=OUTPUT_MARKERS,
        exclude_markers=(TRACKER_MARKER, VERDICT_MARKER),
        exclude_prefix=BOT_PREFIX,
    )
    provider_review_comments = [
        comment
        for comment in review_comments
        if _matches_bot(((comment.get("user") or {}).get("login") or ""), bot_login)
    ]

    try:
        status_payload = client.combined_status_for_ref(head_sha)
        status = latest_status(status_payload.get("statuses") or [], status_context)
    except Exception:  # noqa: BLE001 - unknown status must fail closed
        status = None
    operational_reason = status_reason(status, status_context)

    reviews = client.list_reviews(int(pr_number))
    verdict = head_verdict(reviews, bot_login, head_sha)
    unresolved_ids, normalization = normalize_threads(
        client, pr_number, bot_login=bot_login, apply=apply
    )

    reasons: List[str] = []
    forced_titles: List[str] = []
    if verdict.state == "CHANGES_REQUESTED":
        reasons.append(
            "the review provider requested changes on this HEAD "
            f"(review {verdict.decision.get('id')})"
        )
        # A blocking verdict is evidence of at least one actionable finding even
        # when the report only states it in prose.
        forced_titles = parse_summary(str(verdict.decision.get("body") or ""))
        if not forced_titles:
            forced_titles = ["CodeRabbit requested changes on this HEAD without an inline finding."]
    elif not verdict.approved:
        reasons.append(
            "the review provider published no APPROVED review bound to this HEAD, "
            "so the diff is unverified"
        )
    if verdict.blocking_nitpicks:
        reasons.append(
            f"{len(verdict.blocking_nitpicks)} provider nitpick review(s) were published "
            "after the latest decisive review on this HEAD and are still unaddressed"
        )
    # The commit status reports operational state only. It can block a head that
    # was never approved, but it must never revoke an approval GitHub already
    # stores as a durable review for this exact head.
    if operational_reason and not verdict.approved:
        reasons.append(operational_reason)

    return ProviderSnapshot(
        provider=PROVIDER_NAME,
        bot_logins=(bot_login,),
        review_bots=(bot_login,),
        output_markers=OUTPUT_MARKERS,
        summary_comments=summary_comments,
        review_comments=provider_review_comments,
        unresolved_ids=unresolved_ids,
        summary_parser=parse_summary,
        is_boilerplate=is_boilerplate_title,
        extra_reason="; ".join(reasons) or None,
        forced_open_titles=forced_titles,
        provider_decision=verdict.state or None,
        metadata={
            "status_context": status_context,
            "status_state": (status or {}).get("state"),
            "status_description": str((status or {}).get("description") or "")[:200],
            "review_id": (verdict.decision or {}).get("id"),
            "review_state": verdict.state or None,
            "nitpicks": len(verdict.nitpicks),
            "blocking_nitpicks": len(verdict.blocking_nitpicks),
            "threads": normalization.describe(),
        },
    )


def covers_head(
    reviews: Sequence[Dict[str, Any]],
    statuses: Sequence[Dict[str, Any]],
    head: str,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
    status_context: str = DEFAULT_STATUS_CONTEXT,
) -> bool:
    """Whether CodeRabbit has already reported on this exact HEAD.

    The shared included-review slot is only owed to a head the provider has
    never looked at. A durable exact-head APPROVED review answers that on its
    own, so a later `Review skipped` status cannot make the queue spend another
    full review on a head it already cleared. Reviews by other identities do not
    count: a human approving a pull request says nothing about CodeRabbit's
    quota.
    """

    if not head:
        return False
    for review in reviews or []:
        if not _matches_bot(((review.get("user") or {}).get("login") or ""), bot_login):
            continue
        if (review.get("commit_id") or "").lower() == head.lower():
            return True
    status = latest_status(statuses or [], status_context)
    if status is None:
        return False
    return (status.get("sha") or "").lower() == head.lower()


def profile_for(bot_login: str, status_context: str) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    return (bot_login,), (bot_login,), OUTPUT_MARKERS
