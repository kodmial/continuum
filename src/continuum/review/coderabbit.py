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

# The commands and marker families the preserved NanoDictate snapshot used for
# the same three intents. They are read, never written: a pull request that was
# already being repaired before the migration must not be asked to verify or to
# re-review the same finding and HEAD a second time.
HISTORICAL_VERIFICATION_MARKERS = (
    "opencode-coderabbit-verification",
    "auto-merge-coderabbit-verification",
)
HISTORICAL_REREVIEW_MARKERS = (
    "opencode-coderabbit-full-rereview",
)

# The mention CodeRabbit answers to when asked to look at a specific finding
# again. It re-checks one original thread against the current HEAD rather than
# spending a full included review.
VERIFICATION_COMMAND = "@coderabbitai review"

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


def verification_body(finding_id: str, head: str) -> str:
    """Ask CodeRabbit to re-check one original finding on one exact HEAD.

    The marker is Continuum's, so the same body means the same thing whatever the
    provider is called. What the wording is, how the bot is addressed, and which
    surface answers stay here: the generic repair contract only asks for a body.
    """

    from .repair import verification_marker

    marker = verification_marker(finding_id, head)
    return "\n".join(
        [
            BOT_PREFIX,
            VERIFICATION_COMMAND,
            "",
            f"Re-check finding {finding_id} against the current pull request HEAD "
            f"({head[:12]}).",
            "",
            "Compare the original finding in its own thread with the current code "
            "itself, not with the latest incremental diff.",
            "",
            f"Answer with exactly one of **RESOLVED** or **UNRESOLVED** and one "
            f"sentence of justification. Finding id: {finding_id}.",
            "",
            marker,
        ]
    )


def full_review_body(head: str) -> str:
    """One full re-review request for a HEAD that a repair attempt did not move."""

    from .repair import rereview_marker

    return "\n".join(
        [
            BOT_PREFIX,
            FULL_REVIEW_COMMAND,
            "",
            f"The pull request HEAD is still {head[:12]} and review findings on it are "
            "still open.",
            "",
            rereview_marker(head),
        ]
    )

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


#: Fenced code blocks, collapsed on one line or spanning several. A bot quoting its
#: own earlier verdict inside a fence is quoting history, not stating a new one.
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")
#: A collapsed `<details>` block, which is where a provider puts the prior thread
#: body and its own "Review details" transcript.
_DETAILS_RE = re.compile(r"<details\b[\s\S]*?</details\s*>", re.I)
#: The same block with the closing tag missing, which happens when a provider
#: truncates its own reply. Left in place the block would swallow the visible
#: verdict, so an unterminated block runs to the end of the body.
_DETAILS_UNTERMINATED_RE = re.compile(r"<details\b[\s\S]*\Z", re.I)
#: A block-quoted line. The whole line goes, not just the marker: stripping only
#: the ``>`` would promote somebody else's statement to the provider's own, which is
#: the exact substitution the quote exists to prevent.
_QUOTE_LINE_RE = re.compile(r"(?m)^[ \t]*>+[^\n]*(?:\n|\Z)")


def _visible_conclusion(body: str) -> str:
    """The part of a reply the provider meant as its conclusion.

    Three removals, in this order, because each one hides history the next would
    otherwise read as a statement:

    * collapsed ``<details>`` blocks and fenced code, which carry the provider's
      own earlier verdict and the diff it was reasoning about;
    * block-quoted lines, which are somebody else's statement repeated back;
    * lines that only *ask* whether something is resolved.

    What remains is the conclusion. This is the same reduction the production
    adapter applies, and it is the difference between "the newest reply says the
    finding is still unresolved" and "the newest reply quotes a reply that once
    said resolved".
    """

    text = body or ""
    # Terminated blocks first: the unterminated pattern would otherwise swallow
    # everything after the *first* opening tag, including a conclusion the provider
    # wrote after closing it.
    text = _DETAILS_RE.sub("", text)
    text = _DETAILS_UNTERMINATED_RE.sub("", text)
    text = _CODE_FENCE_RE.sub("", text)
    text = _QUOTE_LINE_RE.sub("", text)
    return text


def classify_reply(body: str) -> str:
    """One verdict token for a provider reply, or `""` when it states none.

    Only the visible conclusion is read. A reply that quotes an older verdict, or
    reproduces it inside a collapsed block or a code fence, states no *new*
    verdict, and treating the quote as the verdict would let a resolved-then-
    reopened thread read as resolved -- the one failure mode the gate cannot
    recover from.

    The ordering of the two checks is deliberate and is not symmetric. An explicit
    refusal wins wherever it appears in the conclusion, because a provider that
    says UNRESOLVED and also says "this was resolved earlier" is describing the
    present and the past. Only then is the positive form accepted.
    """

    text = _visible_conclusion(body)
    if UNRESOLVED_REPLY_RE.search(text):
        return REPLY_UNRESOLVED
    statements = "\n".join(line for line in text.splitlines() if "?" not in line)
    if RESOLVED_REPLY_RE.search(statements):
        return REPLY_RESOLVED
    return ""

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
