"""The generic review-repair agent contract.

The gate decides *whether* a pull request is mergeable. This module decides
*what happens next* when it is not, and it does so without knowing which
provider produced the findings.

A normalized review result is a list of findings bound to one exact HEAD. The
repair contract turns that list into exactly one next action:

    done         the gate is open; nothing to do
    repair       an agent attempt has not been made on this HEAD yet
    verify       the provider must re-check the original findings on this HEAD
    full-review  the HEAD did not move, so ask the provider to look again
    wait         the gate is closed for a reason no agent can fix (coverage)
    pause        the repair budget is exhausted; fail closed with a reason
    disabled     review is off; no provider traffic at all

Everything provider-shaped — the wording of a re-check request, which surfaces
carry a verdict, what a full review is called, how the provider is mentioned —
is supplied by an adapter through the `RepairPort` below. This module only ever
reads the normalized result document and the ids Continuum owns.

Three properties are structural rather than incidental:

* **No full source-issue re-execution.** A repair prompt carries findings and a
  HEAD. It never carries the issue that produced the pull request, so a review
  round cannot re-derive the original task and undo unrelated work.
* **No thread-state loop.** The open findings this module reads have already
  been reconciled by the adapter: a provider that answered RESOLVED in prose has
  already had GitHub's thread state normalized, so a settled finding is simply
  absent here and nothing re-requests it.
* **Bounded, and fails closed.** Every attempt is counted per HEAD and per
  finding. When a bound is reached the answer is `pause` with a compact reason,
  never an unbounded retry and never a green gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .findings import escape_inline_code, now_iso, sanitize_title
from .result import OPEN_STATUSES, VERDICT_APPROVE, VERDICT_REQUEST_CHANGES

REPAIR_MARKER = "<!-- continuum-review-repair -->"
REPAIR_SCHEMA = "continuum.review-repair/v1"

#: One comment per pull request carries the instruction the repair agent runs.
#: A fixed marker keeps it to exactly one comment however many HEADs the pull
#: request goes through; the HEAD the instruction was written for is inside the
#: body and is checked before the instruction is used.
PROMPT_MARKER = "<!-- continuum-review-agent-prompt -->"

ACTION_DISABLED = "disabled"
ACTION_DONE = "done"
ACTION_REPAIR = "repair"
ACTION_VERIFY = "verify"
ACTION_FULL_REVIEW = "full-review"
ACTION_WAIT = "wait"
ACTION_PAUSE = "pause"

REQUEST_VERIFY = "verify"
REQUEST_FULL_REVIEW = "full-review"

# Defaults are deliberately small. A review round that has not converged in
# three attempts on one HEAD, or on one finding across HEADs, is not converging;
# continuing to spend agent time and provider quota on it is the failure mode
# this bound exists to stop.
DEFAULT_MAX_ATTEMPTS_PER_HEAD = 3
DEFAULT_MAX_ATTEMPTS_PER_FINDING = 3
DEFAULT_MAX_REREVIEWS_PER_HEAD = 1
DEFAULT_MAX_BATCH_SIZE = 20
DEFAULT_REPAIR_AGENT_TIMEOUT_MINUTES = 25

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


@dataclass(frozen=True)
class RepairPolicy:
    """Bounds on how much repair work one pull request may consume."""

    max_attempts_per_head: int = DEFAULT_MAX_ATTEMPTS_PER_HEAD
    max_attempts_per_finding: int = DEFAULT_MAX_ATTEMPTS_PER_FINDING
    max_rereviews_per_head: int = DEFAULT_MAX_REREVIEWS_PER_HEAD
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE
    agent_timeout_minutes: int = DEFAULT_REPAIR_AGENT_TIMEOUT_MINUTES

    def describe(self) -> Dict[str, Any]:
        return {
            "max_attempts_per_head": self.max_attempts_per_head,
            "max_attempts_per_finding": self.max_attempts_per_finding,
            "max_rereviews_per_head": self.max_rereviews_per_head,
            "max_batch_size": self.max_batch_size,
            "agent_timeout_minutes": self.agent_timeout_minutes,
        }


@dataclass(frozen=True)
class RepairPort:
    """The provider-shaped half of the repair contract.

    An adapter supplies these; nothing here knows how a particular reviewer is
    addressed or what it considers a re-check.
    """

    provider: str
    #: Build the request that asks the provider to re-check one finding on one
    #: exact HEAD. Returns the request body, or "" when unsupported.
    verification_body: Optional[Callable[[str, str], str]] = None
    #: Build the request for one full re-review of one exact HEAD.
    full_review_body: Optional[Callable[[str], str]] = None
    #: Markers a previous generation of Continuum wrote for the same intent.
    #: They are recognized so a migrated pull request does not start a second
    #: loop over work that was already requested once.
    historical_verification_markers: Tuple[str, ...] = ()
    historical_rereview_markers: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "verification": callable(self.verification_body),
            "full_review": callable(self.full_review_body),
            "historical_verification_markers": len(self.historical_verification_markers),
            "historical_rereview_markers": len(self.historical_rereview_markers),
        }


@dataclass(frozen=True)
class RepairRequest:
    """One outbound provider request produced by the repair contract."""

    kind: str
    provider: str
    head: str
    body: str
    marker: str
    finding: str = ""
    spent_head: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "head": self.head,
            "finding": self.finding,
            "marker": self.marker,
            "body_length": len(self.body),
        }


@dataclass(frozen=True)
class RepairPlan:
    """The single next action for one pull request at one exact HEAD."""

    action: str
    head: str
    provider: str = ""
    reason: str = ""
    findings: Tuple[Dict[str, Any], ...] = ()
    requests: Tuple[RepairRequest, ...] = ()
    attempts: int = 0
    attempts_per_head: Dict[str, int] = field(default_factory=dict)
    exhausted: Tuple[str, ...] = ()

    @property
    def pauses(self) -> bool:
        return self.action == ACTION_PAUSE

    @property
    def needs_agent(self) -> bool:
        return self.action == ACTION_REPAIR

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": REPAIR_SCHEMA,
            "action": self.action,
            "provider": self.provider,
            "head": self.head,
            "reason": self.reason,
            "attempts": self.attempts,
            "attempts_per_head": dict(self.attempts_per_head),
            "findings": [finding.get("id", "") for finding in self.findings],
            "requests": [request.describe() for request in self.requests],
            "exhausted": list(self.exhausted),
            "paused": self.pauses,
            "needs_agent": self.needs_agent,
        }


@dataclass
class RepairLedger:
    """Durable record of what has already been spent on one pull request.

    Persisted as a single marker comment so a re-dispatched or resumed run picks
    the same budget up where the previous one stopped instead of restarting it.
    """

    head_attempts: Dict[str, int] = field(default_factory=dict)
    finding_attempts: Dict[str, int] = field(default_factory=dict)
    rereview_heads: List[str] = field(default_factory=list)
    verified_heads: List[str] = field(default_factory=list)
    paused_reason: str = ""
    last_action: str = ""
    updated_at: str = ""

    def attempts_for_head(self, head: str) -> int:
        return int(self.head_attempts.get(_key(head), 0))

    def attempts_for_finding(self, finding_id: str) -> int:
        return int(self.finding_attempts.get(_key(finding_id), 0))

    def verified_head(self, head: str) -> bool:
        """Whether verification was already requested for this exact HEAD."""

        return _key(head) in set(self.verified_heads)

    def rereviewed_head(self, head: str) -> bool:
        """Whether a full re-review was already requested for this exact HEAD."""

        return _key(head) in set(self.rereview_heads)

    def copy(self) -> "RepairLedger":
        return RepairLedger(
            head_attempts=dict(self.head_attempts),
            finding_attempts=dict(self.finding_attempts),
            rereview_heads=list(self.rereview_heads),
            verified_heads=list(self.verified_heads),
            paused_reason=self.paused_reason,
            last_action=self.last_action,
            updated_at=self.updated_at,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": REPAIR_SCHEMA,
            "head_attempts": dict(self.head_attempts),
            "finding_attempts": dict(self.finding_attempts),
            "rereview_heads": list(self.rereview_heads),
            "verified_heads": list(self.verified_heads),
            "paused_reason": self.paused_reason,
            "last_action": self.last_action,
            "updated_at": self.updated_at,
        }


def _key(value: Any) -> str:
    return str(value or "").strip().lower()


def record(
    ledger: Optional[RepairLedger],
    repair_plan: RepairPlan,
    *,
    at: str = "",
) -> RepairLedger:
    """Fold one executed plan into the durable ledger.

    Only the action that actually ran is charged: an `ACTION_REPAIR` plan costs
    one attempt for the HEAD and one for each finding it handed the agent, and
    `ACTION_VERIFY` / `ACTION_FULL_REVIEW` only mark that the bounded request for
    this HEAD has been made. `ACTION_PAUSE` stores the compact reason so the next
    run reports the same thing instead of silently retrying.
    """

    state = (ledger.copy() if ledger is not None else RepairLedger())
    action = repair_plan.action
    head_key = _key(repair_plan.head)
    finding_ids = [str(finding.get("id") or "") for finding in repair_plan.findings]

    if action == ACTION_REPAIR and head_key:
        state.head_attempts[head_key] = state.attempts_for_head(repair_plan.head) + 1
        for finding_id in finding_ids:
            if finding_id:
                state.finding_attempts[finding_id] = state.attempts_for_finding(finding_id) + 1
    elif action == ACTION_VERIFY and head_key and head_key not in state.verified_heads:
        state.verified_heads.append(head_key)
    elif action == ACTION_FULL_REVIEW and head_key and head_key not in state.rereview_heads:
        state.rereview_heads.append(head_key)

    state.last_action = action
    state.updated_at = at or now_iso()
    if action == ACTION_PAUSE:
        state.paused_reason = sanitize_title(repair_plan.reason, limit=400)
    elif action in (ACTION_REPAIR, ACTION_VERIFY, ACTION_FULL_REVIEW, ACTION_DONE):
        # Real progress clears an earlier exhaustion, so the pause is not sticky
        # across a round that has since been fixed.
        state.paused_reason = ""
    return state


# -- ledger persistence ------------------------------------------------------


def empty_ledger() -> RepairLedger:
    return RepairLedger()


def _coerce_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item or "").strip()]


def _coerce_counts(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: Dict[str, int] = {}
    for key, count in value.items():
        name = str(key or "").strip().lower()
        if not name:
            continue
        try:
            counts[name] = max(0, int(count))
        except (TypeError, ValueError):
            continue
    return counts


def parse_ledger(body: str) -> Optional[RepairLedger]:
    """Read a repair ledger out of a marker comment body."""

    if REPAIR_MARKER not in (body or ""):
        return None
    match = _JSON_BLOCK_RE.search(body)
    if not match:
        return None
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return RepairLedger(
        head_attempts=_coerce_counts(raw.get("head_attempts")),
        finding_attempts=_coerce_counts(raw.get("finding_attempts")),
        rereview_heads=_coerce_str_list(raw.get("rereview_heads")),
        verified_heads=_coerce_str_list(raw.get("verified_heads")),
        paused_reason=sanitize_title(str(raw.get("paused_reason") or ""), limit=400),
        last_action=str(raw.get("last_action") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def load_ledger(
    comments: Sequence[Dict[str, Any]],
    *,
    trusted_logins: Sequence[str] = (),
) -> Optional[RepairLedger]:
    """Newest ledger authored by a trusted identity.

    A comment that merely copies the marker must not be able to reset the
    budget: an untrusted author would otherwise clear every attempt count and
    restart the loop the bound exists to stop.
    """

    trusted = {login.lower() for login in trusted_logins}
    for comment in reversed(list(comments or [])):
        ledger = parse_ledger(comment.get("body") or "")
        if ledger is None:
            continue
        login = ((comment.get("user") or {}).get("login") or "").lower()
        if trusted and login and login not in trusted:
            continue
        return ledger
    return None


def render_ledger(ledger: RepairLedger, bot_prefix: str) -> str:
    """Render the ledger comment. Every value is sanitized or machine-read."""

    lines = [
        f"{bot_prefix} Continuum review repair ledger",
        "",
        REPAIR_MARKER,
        "",
        f"Last action: `{escape_inline_code(ledger.last_action or 'none')}`",
        "",
        "| Scope | Attempts |",
        "| --- | --- |",
    ]
    for head, count in sorted(ledger.head_attempts.items()):
        lines.append(f"| `HEAD {escape_inline_code(head[:12])}` | {count} |")
    for finding_id, count in sorted(ledger.finding_attempts.items()):
        lines.append(f"| `{escape_inline_code(finding_id)}` | {count} |")
    if not ledger.head_attempts and not ledger.finding_attempts:
        lines.append("| — | — |")
    if ledger.paused_reason:
        lines += ["", f"Paused: {ledger.paused_reason}"]
    lines += [
        "",
        "<details><summary>Machine-readable state</summary>",
        "",
        "```json",
        json.dumps(ledger.describe(), indent=2, sort_keys=True),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(lines)


# -- markers -----------------------------------------------------------------

# Marker Continuum writes when it asks the provider to re-check one original
# finding on one exact HEAD. The ids are Continuum's own, so the marker means the
# same thing for every provider.
VERIFY_MARKER = "<!-- continuum-review-verify finding={finding} head={head} -->"
# Marker for the bounded, unchanged-HEAD full re-review.
REREVIEW_MARKER = "<!-- continuum-review-full-review head={head} -->"


def verification_marker(finding_id: str, head: str) -> str:
    return VERIFY_MARKER.format(finding=escape_inline_code(finding_id), head=escape_inline_code(head))


def rereview_marker(head: str) -> str:
    return REREVIEW_MARKER.format(head=escape_inline_code(head))


def marker_is_historical(marker: str, port: Optional[RepairPort]) -> bool:
    """Whether `marker` names an intent a previous generation already recorded."""

    if port is None:
        return False
    return any(
        marker in body
        for body in (*port.historical_verification_markers, *port.historical_rereview_markers)
    )


_FINDING_IN_MARKER_RE = re.compile(r"finding=([A-Za-z0-9_-]+)")
_HEAD_IN_MARKER_RE = re.compile(r"head=([0-9a-fA-F]{7,64})")

_VERIFY_PREFIX = "<!-- continuum-review-verify finding="
_REREVIEW_PREFIX = "<!-- continuum-review-full-review head="


@dataclass(frozen=True)
class RequestedMarkers:
    """The provider requests already visible on a pull request.

    A value rather than a live lookup so the planning decision stays a pure
    function of data, and so a test can state exactly which requests already
    happened without arranging a comment list.
    """

    verified: Tuple[Tuple[str, str], ...] = ()
    rereviewed: Tuple[str, ...] = ()

    @classmethod
    def from_comments(
        cls,
        comments: Sequence[Dict[str, Any]],
        port: Optional["RepairPort"] = None,
        *,
        trusted_logins: Sequence[str] = (),
    ) -> "RequestedMarkers":
        verified, rereviewed = requested_markers(
            comments, port, trusted_logins=trusted_logins
        )
        return cls(
            verified=tuple(sorted(verified)),
            rereviewed=tuple(sorted(rereviewed)),
        )

    def verification_pending(self, request: RepairRequest) -> bool:
        if request.kind != REQUEST_VERIFY:
            return True
        return (request.finding.upper(), _key(request.head)) not in set(self.verified)

    def rereview_pending(self, request: RepairRequest) -> bool:
        if request.kind != REQUEST_FULL_REVIEW:
            return True
        return _key(request.head) not in set(self.rereviewed)

    def describe(self) -> Dict[str, Any]:
        return {
            "verified": [list(pair) for pair in self.verified],
            "rereviewed": list(self.rereviewed),
        }


def _pending(
    requests: Sequence[RepairRequest],
    seen: RequestedMarkers,
) -> Tuple[RepairRequest, ...]:
    """Drop requests the pull request has already been given.

    This is the deduplication that stops a retry from re-asking the provider the
    same question: the marker is bound to the finding *and* the HEAD, so a new
    HEAD re-opens the question while a re-run on the same HEAD does not.
    """

    return tuple(
        request
        for request in requests
        if seen.verification_pending(request) and seen.rereview_pending(request)
    )


def requested_markers(
    comments: Sequence[Dict[str, Any]],
    port: Optional[RepairPort] = None,
    *,
    trusted_logins: Sequence[str] = (),
) -> Tuple[Set[Tuple[str, str]], Set[str]]:
    """Verification and re-review requests already visible on this pull request.

    Returns `(verified, rereviewed)` where `verified` holds the `(finding id,
    HEAD)` pairs a provider was already asked about and `rereviewed` holds the
    HEADs a full re-review was already requested for.

    Both the current marker family and the historical ones the port declares are
    read, so a pull request that was mid-repair when Continuum took over is
    recognized instead of being asked the same question a second time.

    Only comments from trusted identities count. Otherwise any account could post
    a marker-shaped comment and suppress every verification request on the pull
    request, which would freeze the gate on a lie.
    """

    historical_verify = tuple(port.historical_verification_markers) if port else ()
    historical_rereview = tuple(port.historical_rereview_markers) if port else ()
    trusted = {login.lower() for login in trusted_logins}

    verified: Set[Tuple[str, str]] = set()
    rereviewed: Set[str] = set()
    for comment in comments or []:
        body = comment.get("body") or ""
        if not body.strip():
            continue
        if trusted:
            login = ((comment.get("user") or {}).get("login") or "").lower()
            if login and login not in trusted:
                continue
        head_match = _HEAD_IN_MARKER_RE.search(body)
        if head_match is None:
            continue
        head = _key(head_match.group(1))
        if _VERIFY_PREFIX in body or any(marker in body for marker in historical_verify):
            finding_match = _FINDING_IN_MARKER_RE.search(body)
            if finding_match is not None:
                verified.add((finding_match.group(1).upper(), head))
            continue
        if _REREVIEW_PREFIX in body or any(marker in body for marker in historical_rereview):
            rereviewed.add(head)
    return verified, rereviewed


# -- planning ----------------------------------------------------------------


def open_findings(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Findings the normalized gate still reports as open on this HEAD."""

    return [
        finding
        for finding in (result.get("findings") or [])
        if finding.get("status") in OPEN_STATUSES
    ]


def _dedupe(findings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per finding id.

    The same finding is routinely reported by a summary bullet and by the inline
    thread it belongs to. Repairing it twice in one batch costs an agent run and
    teaches nothing, so the batch is keyed by the identifier Continuum owns.
    """

    unique: Dict[str, Dict[str, Any]] = {}
    for finding in findings:
        finding_id = str(finding.get("id") or "").strip().upper()
        if not finding_id:
            continue
        unique.setdefault(finding_id, dict(finding))
    return [unique[key] for key in sorted(unique)]


def _exhausted(
    head: str,
    findings: Sequence[Dict[str, Any]],
    ledger: RepairLedger,
    policy: RepairPolicy,
) -> Tuple[str, ...]:
    reasons: List[str] = []
    if ledger.attempts_for_head(head) >= policy.max_attempts_per_head:
        reasons.append(
            f"repair budget for HEAD {head[:12]} is exhausted after "
            f"{ledger.attempts_for_head(head)} attempt(s)"
        )
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        if ledger.attempts_for_finding(finding_id) >= policy.max_attempts_per_finding:
            reasons.append(
                f"repair budget for finding {finding_id} is exhausted after "
                f"{ledger.attempts_for_finding(finding_id)} attempt(s)"
            )
    return tuple(reasons)


def _build_requests(
    port: RepairPort,
    head: str,
    findings: Sequence[Dict[str, Any]],
    kind: str,
) -> Tuple[RepairRequest, ...]:
    if kind == REQUEST_FULL_REVIEW:
        builder = port.full_review_body
        if not callable(builder):
            return ()
        body = str(builder(head) or "")
        if not body.strip():
            return ()
        return (
            RepairRequest(
                kind=REQUEST_FULL_REVIEW,
                provider=port.provider,
                head=head,
                body=body,
                marker=rereview_marker(head),
                spent_head=head,
            ),
        )

    builder = port.verification_body
    if not callable(builder):
        return ()
    requests: List[RepairRequest] = []
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        body = str(builder(finding_id, head) or "")
        if not body.strip():
            continue
        requests.append(
            RepairRequest(
                kind=REQUEST_VERIFY,
                provider=port.provider,
                head=head,
                body=body,
                marker=verification_marker(finding_id, head),
                finding=finding_id,
                spent_head=head,
            )
        )
    return tuple(requests)


def plan(
    result: Dict[str, Any],
    ledger: Optional[RepairLedger],
    port: Optional[RepairPort] = None,
    *,
    policy: Optional[RepairPolicy] = None,
    enabled: bool = True,
    seen: Optional["RequestedMarkers"] = None,
) -> RepairPlan:
    """Decide the one next action for a normalized review result.

    A pure function: it reads the result document, the durable ledger, the
    requests already visible on the pull request, and the provider port, and
    writes nothing. The caller performs the requests and records the spend, which
    is what makes every branch here testable without a network.
    """

    limits = policy or RepairPolicy()
    state = ledger or RepairLedger()
    marker_state = seen or RequestedMarkers()
    head = str(result.get("head") or "")
    provider = str(result.get("provider") or (port.provider if port else ""))

    if not enabled:
        # No provider traffic and no agent work. This is the review:false path.
        return RepairPlan(
            action=ACTION_DISABLED,
            head=head,
            provider=provider,
            reason="Review is disabled by configuration; no provider or agent work is performed.",
        )

    if not head:
        return RepairPlan(
            action=ACTION_WAIT,
            head=head,
            provider=provider,
            reason="The pull request has no determinable HEAD; nothing can be bound to a review.",
        )

    if result.get("gate_passed") or result.get("verdict") == VERDICT_APPROVE:
        return RepairPlan(
            action=ACTION_DONE,
            head=head,
            provider=provider,
            attempts=state.attempts_for_head(head),
            attempts_per_head=dict(state.head_attempts),
        )

    findings = _dedupe(open_findings(result))
    verdict = str(result.get("verdict") or "")
    exhausted = _exhausted(head, findings, state, limits)

    # A coverage-only block names no finding, so there is nothing to hand an
    # agent: only a provider review of the current HEAD can close it. Spinning a
    # repair agent here would burn its budget on a gap it cannot fix.
    if not findings or verdict != VERDICT_REQUEST_CHANGES:
        return RepairPlan(
            action=ACTION_WAIT,
            head=head,
            provider=provider,
            reason=str(result.get("reason") or "The review gate is closed."),
            findings=tuple(findings),
            attempts=state.attempts_for_head(head),
            attempts_per_head=dict(state.head_attempts),
        )

    attempts = state.attempts_for_head(head)

    if attempts == 0:
        if exhausted:
            return RepairPlan(
                action=ACTION_PAUSE,
                head=head,
                provider=provider,
                reason="; ".join(exhausted),
                findings=tuple(findings),
                attempts=attempts,
                attempts_per_head=dict(state.head_attempts),
                exhausted=exhausted,
            )
        batch = findings[: limits.max_batch_size]
        return RepairPlan(
            action=ACTION_REPAIR,
            head=head,
            provider=provider,
            reason=(
                f"{len(findings)} finding(s) are open on this HEAD and no repair attempt "
                "has been made for it yet."
            ),
            findings=tuple(batch),
            attempts=attempts,
            attempts_per_head=dict(state.head_attempts),
        )

    if exhausted:
        return RepairPlan(
            action=ACTION_PAUSE,
            head=head,
            provider=provider,
            reason="; ".join(exhausted),
            findings=tuple(findings),
            attempts=attempts,
            attempts_per_head=dict(state.head_attempts),
            exhausted=exhausted,
        )

    # The agent has had its attempt on this HEAD. The provider is the only party
    # that can say whether the attempt worked, so ask it about the original
    # findings on this exact HEAD rather than trusting the agent's own report.
    if port is not None and not state.verified_head(head):
        requests = _pending(
            _build_requests(port, head, findings, REQUEST_VERIFY),
            marker_state,
        )
        if requests:
            return RepairPlan(
                action=ACTION_VERIFY,
                head=head,
                provider=provider,
                reason=(
                    f"{attempts} repair attempt(s) were made on this HEAD; asking the "
                    f"provider to re-check {len(requests)} original finding(s) against it."
                ),
                findings=tuple(findings),
                requests=requests,
                attempts=attempts,
                attempts_per_head=dict(state.head_attempts),
            )

    # The HEAD never moved, so no synchronize event will bring the provider back
    # to it. One bounded full re-review is what gets the diff looked at again;
    # without it a body-only nitpick would block forever on a HEAD nothing is
    # changing.
    if port is not None and not state.rereviewed_head(head):
        requests = _pending(
            _build_requests(port, head, findings, REQUEST_FULL_REVIEW),
            marker_state,
        )
        if requests:
            return RepairPlan(
                action=ACTION_FULL_REVIEW,
                head=head,
                provider=provider,
                reason=(
                    "The findings are still open on a HEAD that has not changed since the "
                    "repair attempt; requesting one full re-review of this HEAD."
                ),
                findings=tuple(findings),
                requests=requests,
                attempts=attempts,
                attempts_per_head=dict(state.head_attempts),
            )

    # Every bounded request for this HEAD has already been made and the gate is
    # still closed. Waiting is the honest answer: the next CI run, the next
    # provider answer, or the next HEAD move is what changes the outcome.
    return RepairPlan(
        action=ACTION_WAIT,
        head=head,
        provider=provider,
        reason=(
            f"Findings are still open on this HEAD after {attempts} repair attempt(s); "
            "verification and re-review have already been requested for it."
        ),
        findings=tuple(findings),
        attempts=attempts,
        attempts_per_head=dict(state.head_attempts),
    )


# -- agent prompt ------------------------------------------------------------


def render_prompt(
    repair_plan: RepairPlan,
    *,
    pr_number: int,
    head_ref: str = "",
    collected_at: str = "",
) -> str:
    """The instruction handed to the repair agent.

    Scoped to the findings and the HEAD on purpose. The agent repairs what the
    review found on the branch that is already open; it does not receive the
    source issue, so a review round cannot re-execute the original task.
    """

    if not repair_plan.needs_agent:
        raise ValueError(
            f"Action {repair_plan.action!r} does not dispatch a repair agent."
        )
    lines = [
        f"Repair the automated code review findings on pull request #{pr_number}.",
        "",
        f"HEAD under review: {repair_plan.head}",
        f"Head ref: {head_ref or '(the pull request head branch)'}",
        f"Findings to repair: {len(repair_plan.findings)}",
        "",
        "Fix every finding listed below. This is a review repair, not a new task:",
        "",
        "- The branch is already open and its pull request already exists. Commit and",
        "  push to that branch; do not create a branch, a commit on the base, or a new",
        "  pull request.",
        "- Repair only what the findings describe. Do not re-derive the original task,",
        "  and do not revert or rewrite unrelated work that is already on the branch.",
        "- The current code is authoritative. If a listed finding is already fixed by",
        "  the current code, do not reintroduce a fix for it.",
        "- Do not resolve or dismiss any review thread yourself. The provider verifies",
        "  every original finding independently after this run, on the exact HEAD you",
        "  push.",
        "- Do not weaken a check, delete a test, or add an ignore to make a finding go",
        "  away. If a finding is wrong, say so in your final message and leave the code",
        "  correct.",
        "",
        "## Findings",
        "",
    ]
    for finding in repair_plan.findings:
        finding_id = str(finding.get("id") or "")
        title = sanitize_title(str(finding.get("title") or ""), limit=400)
        path = str(finding.get("file") or "")
        line = finding.get("line") or 0
        location = f"{path}:{line}" if path else "general"
        lines.append(f"- `{finding_id}` ({location}): {title}")
    if collected_at:
        lines += ["", f"Findings collected at {collected_at}."]
    lines += [
        "",
        "Before finishing, report exactly which finding ids you changed and, for each,",
        "the one-line reason. Do not push if you changed nothing: an unchanged HEAD is",
        "a real answer and Continuum handles it explicitly.",
    ]
    return "\n".join(lines)


def render_agent_prompt(prompt: str, head: str) -> str:
    """Wrap the repair instruction in the marker the agent step looks for.

    The HEAD travels inside the body rather than in the marker so the marker
    stays a fixed key: one comment per pull request, overwritten per dispatch,
    with a reader that refuses an instruction written for a different commit.
    """

    return "\n".join(
        [
            "<!-- bot --> Continuum review repair",
            "",
            PROMPT_MARKER,
            "",
            f"Written for HEAD `{escape_inline_code(str(head or '')[:40])}`.",
            "",
            prompt,
        ]
    )


def parse_agent_prompt(body: str, head: str) -> Optional[str]:
    """The stored instruction, when it was written for exactly this HEAD.

    None for anything else, including a marker a trusted identity did not write
    for this commit. A repair agent must never run an instruction that was
    assembled against a different diff.
    """

    if PROMPT_MARKER not in (body or ""):
        return None
    lines = (body or "").splitlines()
    try:
        start = lines.index(PROMPT_MARKER) + 1
    except ValueError:
        return None
    rest = lines[start:]
    # Blank lines first, then the provenance line, then blanks again: the block
    # is rendered as marker, blank, provenance, blank, body, and the provenance
    # line must not survive into the text a model is handed.
    while rest and not rest[0].strip():
        rest = rest[1:]
    if rest and rest[0].startswith("Written for HEAD"):
        rest = rest[1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    if not rest:
        return None
    match = re.search(r"Written for HEAD `([0-9a-fA-F]*)`", body)
    if match is not None:
        stored = match.group(1).lower()
        # The first occurrence is the engine's own line: everything after it is
        # model- and provider-authored text that may itself contain the phrase.
        # An empty capture is not a wildcard. It is the one shape that would
        # match every commit, so it is refused rather than read as a match.
        if not stored or stored != str(head or "").strip().lower()[: len(stored)]:
            return None
    return "\n".join(rest).strip()


__all__ = [
    "ACTION_DISABLED",
    "ACTION_DONE",
    "ACTION_FULL_REVIEW",
    "ACTION_PAUSE",
    "ACTION_REPAIR",
    "ACTION_VERIFY",
    "ACTION_WAIT",
    "DEFAULT_MAX_ATTEMPTS_PER_FINDING",
    "DEFAULT_MAX_ATTEMPTS_PER_HEAD",
    "DEFAULT_MAX_BATCH_SIZE",
    "DEFAULT_MAX_REREVIEWS_PER_HEAD",
    "REPAIR_MARKER",
    "REPAIR_SCHEMA",
    "REREVIEW_MARKER",
    "REQUEST_FULL_REVIEW",
    "PROMPT_MARKER",
    "REQUEST_VERIFY",
    "VERIFY_MARKER",
    "RepairLedger",
    "RepairPlan",
    "RepairPolicy",
    "RepairPort",
    "RepairRequest",
    "RequestedMarkers",
    "empty_ledger",
    "load_ledger",
    "marker_is_historical",
    "open_findings",
    "parse_agent_prompt",
    "parse_ledger",
    "plan",
    "record",
    "requested_markers",
    "render_agent_prompt",
    "render_ledger",
    "render_prompt",
    "rereview_marker",
    "verification_marker",
]
