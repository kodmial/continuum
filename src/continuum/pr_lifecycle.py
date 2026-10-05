"""Authoritative PR lifecycle state record + deterministic reducer (issue #282).

Collapse the distributed PR/issue lifecycle (labels, human-visible comments,
status contexts, provider reviews, workflow-run history, HEAD SHAs, cron
wakeups) into one versioned, exact-HEAD authoritative state record and one
deterministic reducer.

Architecture (incremental migration, steps 1-2 active):

- :class:`LifecycleState` is the single machine-owned record keyed by
  ``repo + PR + exact HEAD SHA + generation``. It serializes to exactly one
  hidden marker line (``continuum-pr-lifecycle-state``) suitable for a single
  upserted state comment. The marker carries machine state only.
- :func:`reduce` is the single reducer. Every event path (CI completion,
  review, review comment, status, push, workflow_run, cron, manual command)
  calls it with latest facts + prior authoritative state. It performs no I/O,
  so API reads stay bounded by construction.
- Facts (:class:`LifecycleFacts`, :class:`ReviewFact`) are external inputs.
  Historical comments/labels/statuses are migration-only evidence and are
  never independent locks; see :func:`coerce_legacy_evidence`.
- WAIT is separated from ERROR: :class:`Decision` carries
  ``workflow_failure`` which is True only for deterministic invariant
  violation, broken configuration, permission/authentication failure,
  malformed state, or exhausted recovery. Quota/backpressure/WIP/computing/
  in-flight waits are ``workflow_failure=False``.
- Idempotency keys are ``<repo>:<pr>:<head>:<phase>:<generation>``;
  duplicate wakeups coalesce via ``last_event`` + the emitted-key set without
  scanning unbounded history.
- Provider adapters (:func:`normalize_coderabbit_review`,
  :func:`normalize_pragent_review`) produce the same :class:`ReviewFact`.
- Credential choice is orthogonal to lifecycle state:
  :func:`requires_pat`, :func:`validate_caller_permissions`.

Human-visible text/branding is never parsed as machine state. Command
recognition (:func:`is_coderabbit_command_in_flight`) ignores attribution
headers, emoji and prose and matches only the machine command line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

STATE_SCHEMA_VERSION = 1

STATE_MARKER_NAME = "continuum-pr-lifecycle-state"

MAX_STATE_COMMENTS_SCANNED = 20

# Phases carried by the authoritative record.
WAITING_CI = "waiting_ci"
WAITING_REVIEW = "waiting_review"
REVIEW_IN_FLIGHT = "review_in_flight"
RATE_LIMITED = "rate_limited"
REPAIR_IN_FLIGHT = "repair_in_flight"
WAITING_RECHECK = "waiting_recheck"
MERGE_READY = "merge_ready"
CONFLICT_REPAIR = "conflict_repair"
MERGED = "merged"
TERMINAL_BLOCK = "terminal_block"

PHASES = (
    WAITING_CI,
    WAITING_REVIEW,
    REVIEW_IN_FLIGHT,
    RATE_LIMITED,
    REPAIR_IN_FLIGHT,
    WAITING_RECHECK,
    MERGE_READY,
    CONFLICT_REPAIR,
    MERGED,
    TERMINAL_BLOCK,
)

PROVIDERS = ("coderabbit", "pr-agent", "none")

EVENTS = (
    "ci_completion",
    "review",
    "review_comment",
    "status",
    "push",
    "workflow_run",
    "cron",
    "manual_command",
)

# Explicit transition graph within one generation. Any same-generation phase
# change not listed here is rejected (fail-closed no-op hold). HEAD changes
# always start a new generation at waiting_ci and are the only implicit
# rollback path.
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    WAITING_CI: frozenset({WAITING_CI, WAITING_REVIEW, REVIEW_IN_FLIGHT,
                           RATE_LIMITED, CONFLICT_REPAIR, TERMINAL_BLOCK,
                           WAITING_RECHECK, MERGE_READY}),
    WAITING_REVIEW: frozenset({WAITING_REVIEW, REVIEW_IN_FLIGHT, RATE_LIMITED,
                               REPAIR_IN_FLIGHT, WAITING_RECHECK, MERGE_READY,
                               CONFLICT_REPAIR, TERMINAL_BLOCK, WAITING_CI}),
    REVIEW_IN_FLIGHT: frozenset({REVIEW_IN_FLIGHT, RATE_LIMITED,
                                 REPAIR_IN_FLIGHT, WAITING_RECHECK,
                                 WAITING_REVIEW, MERGE_READY, TERMINAL_BLOCK}),
    RATE_LIMITED: frozenset({RATE_LIMITED, WAITING_REVIEW, REVIEW_IN_FLIGHT,
                             TERMINAL_BLOCK}),
    REPAIR_IN_FLIGHT: frozenset({REPAIR_IN_FLIGHT, WAITING_RECHECK,
                                 WAITING_REVIEW, TERMINAL_BLOCK}),
    WAITING_RECHECK: frozenset({WAITING_RECHECK, WAITING_REVIEW,
                                REVIEW_IN_FLIGHT, REPAIR_IN_FLIGHT,
                                MERGE_READY, CONFLICT_REPAIR, TERMINAL_BLOCK,
                                WAITING_CI}),
    MERGE_READY: frozenset({MERGE_READY, MERGED, WAITING_RECHECK,
                            WAITING_CI, TERMINAL_BLOCK}),
    CONFLICT_REPAIR: frozenset({CONFLICT_REPAIR, WAITING_CI, WAITING_RECHECK,
                                TERMINAL_BLOCK}),
    MERGED: frozenset({MERGED}),
    TERMINAL_BLOCK: frozenset({TERMINAL_BLOCK, WAITING_CI}),
}

MAX_REVIEW_ATTEMPTS_PER_HEAD = 3
MAX_REPAIR_ATTEMPTS_PER_HEAD = 1
MAX_CONFLICT_DISPATCHES_PER_HEAD = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_STATE_RE = re.compile(
    r"<!--\s*continuum-pr-lifecycle-state\s+"
    r"v=(\d+)\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"pr=(\d+)\s+"
    r"head=([0-9a-fA-F]{40})\s+"
    r"generation=(\d+)\s+"
    r"phase=([a-z_]+)\s+"
    r"provider=([a-z\-]+)\s+"
    r"review_attempt=(\d+)\s+"
    r"repair_attempt=(\d+)\s+"
    r"conflict_generation=(\d+)\s+"
    r"not_before=(\d+)\s+"
    r"finding=([0-9a-f]{64}|-)\s+"
    r"last_event=([A-Za-z0-9_:\-./+]{1,128})"
    r"\s*-->"
)

# Attribution/emoji/prose lines that must never influence machine parsing.
_ATTRIBUTION_PREFIXES = (
    "\u26a1",  # leading emoji of the continuum header
    "\U0001f9be",  # agent-coder header
    "\U0001f6f0",  # project header
    "**continuum",
    "**agent coder",
    "**project",
    "continuum",
)
_ORIGIN_MARKER_RE = re.compile(r"<!--\s*continuum-origin\b[^>]*-->")
_HIDDEN_LINE_RE = re.compile(r"^\s*<!--.*?-->\s*$")

_FINDING_RE = re.compile(r"^[0-9a-f]{64}$")
_LAST_EVENT_RE = re.compile(r"^[A-Za-z0-9_:\-./+]{1,128}$")


def _sanitize_last_event(value: object, max_len: int = 128) -> str:
    """Return a marker-safe event identity (parseable by ``parse_state_marker``).

    ``render_state_marker`` interpolates ``last_event`` verbatim while
    ``_STATE_RE`` only accepts ``[A-Za-z0-9_:./+-]{1,128}``. Any space,
    ``-->`` or overlong ``event_id`` would render an unparsable marker and
    lose the authoritative record on next load, so sanitize and bound here
    and when assigning ``event_id`` in :func:`reduce`.
    """
    bound = max(1, min(int(max_len or 128), 128))
    safe = re.sub(r"[^A-Za-z0-9_:\-./+]", "_", str(value or "init"))[:bound]
    return safe or "init"


class LifecycleError(ValueError):
    """Raised for malformed lifecycle inputs (fail-closed ERROR path)."""


@dataclass(frozen=True)
class LifecycleState:
    """One versioned machine-owned record for a PR at an exact HEAD."""

    repo: str
    pr: int
    head: str
    generation: int
    phase: str = WAITING_CI
    provider: str = "coderabbit"
    review_attempt: int = 0
    repair_attempt: int = 0
    conflict_generation: int = 0
    not_before: int = 0
    finding: str = "-"
    last_event: str = "init"
    schema_version: int = STATE_SCHEMA_VERSION

    def key(self) -> str:
        return f"{self.repo}:{self.pr}:{self.head}:{self.phase}:{self.generation}"

    def idempotency_key(self) -> str:
        return self.key()


@dataclass(frozen=True)
class ReviewFact:
    """Normalized provider review fact for the exact current HEAD."""

    provider: str
    decision: str  # approved | changes_requested | comment | quota_wait | disabled | unknown
    head: str
    actionable: Tuple[str, ...] = ()
    quota_not_before: int = 0


@dataclass(frozen=True)
class LifecycleFacts:
    """Latest external facts supplied to the reducer (no history scans)."""

    event: str
    head: str
    event_id: str = ""
    ci: str = "unknown"  # pass | fail | pending | unknown
    mergeable: str = "unknown"  # clean | dirty | computing | unknown
    review: Optional[ReviewFact] = None
    wip_full: bool = False
    caller_permissions: Mapping[str, str] = field(default_factory=dict)
    config_valid: bool = True
    authenticated: bool = True


@dataclass(frozen=True)
class Decision:
    """One reducer outcome. WAIT never sets workflow_failure."""

    action: str  # noop | dispatch_review | dispatch_repair | dispatch_conflict_repair | retry_after | wait | merge | hold | error
    workflow_failure: bool
    reason: str
    emit_side_effect: bool
    idempotency_key: str
    not_before: int = 0


@dataclass(frozen=True)
class Transition:
    state: LifecycleState
    decision: Decision


def _require_repo(repo: object) -> str:
    text = str(repo or "").strip()
    if not _REPO_RE.match(text):
        raise LifecycleError(f"invalid repo identity: {repo!r}")
    return text


def _require_head(head: object) -> str:
    text = str(head or "").strip().lower()
    if not _SHA_RE.match(text):
        raise LifecycleError(f"invalid exact HEAD SHA: {head!r}")
    return text


def _require_phase(phase: object) -> str:
    text = str(phase or "").strip()
    if text not in PHASES:
        raise LifecycleError(f"unknown lifecycle phase: {phase!r}")
    return text


def initial_state(repo: str, pr: int, head: str,
                  provider: str = "coderabbit") -> LifecycleState:
    if provider not in PROVIDERS:
        raise LifecycleError(f"unknown provider: {provider!r}")
    return LifecycleState(
        repo=_require_repo(repo),
        pr=int(pr),
        head=_require_head(head),
        generation=1,
        phase=WAITING_CI,
        provider=provider,
    )


def render_state_marker(state: LifecycleState) -> str:
    _require_repo(state.repo)
    _require_head(state.head)
    _require_phase(state.phase)
    if int(state.generation) < 1:
        raise LifecycleError("generation must be >= 1")
    safe_event = _sanitize_last_event(state.last_event, 128)
    return (
        f"<!-- {STATE_MARKER_NAME} "
        f"v={STATE_SCHEMA_VERSION} "
        f"repo={state.repo} pr={int(state.pr)} head={state.head} "
        f"generation={int(state.generation)} phase={state.phase} "
        f"provider={state.provider} review_attempt={int(state.review_attempt)} "
        f"repair_attempt={int(state.repair_attempt)} "
        f"conflict_generation={int(state.conflict_generation)} "
        f"not_before={int(state.not_before)} finding={state.finding} "
        f"last_event={safe_event} -->"
    )


def parse_state_marker(body: object) -> Optional[LifecycleState]:
    """Parse the single authoritative marker in a body, or None.

    Only the hidden machine marker is read; visible branding/emoji/prose is
    never consulted. Malformed markers return None (caller treats absence
    as no authoritative state; strict validation lives in reduce()).
    """
    if not isinstance(body, str):
        return None
    match = _STATE_RE.search(body)
    if not match:
        return None
    try:
        (version, repo, pr, head, generation, phase, provider,
         review_attempt, repair_attempt, conflict_generation,
         not_before, finding, last_event) = match.groups()
        if int(version) != STATE_SCHEMA_VERSION:
            return None
        if phase not in PHASES or provider not in PROVIDERS:
            return None
        return LifecycleState(
            repo=repo,
            pr=int(pr),
            head=head.lower(),
            generation=int(generation),
            phase=phase,
            provider=provider,
            review_attempt=int(review_attempt),
            repair_attempt=int(repair_attempt),
            conflict_generation=int(conflict_generation),
            not_before=int(not_before),
            finding=finding,
            last_event=last_event,
        )
    except (ValueError, LifecycleError):
        return None


def load_authoritative_state(
    comment_bodies: Sequence[object],
    *,
    repo: str = "",
    pr: int = 0,
    max_comments: int = MAX_STATE_COMMENTS_SCANNED,
) -> Optional[LifecycleState]:
    """Return the newest authoritative state from a bounded comment window.

    Only the newest ``max_comments`` bodies are inspected and only hidden
    markers are parsed: no full-history scan is ever needed for idempotency.
    When ``repo``/``pr`` are given, markers for another PR are ignored so an
    old record from elsewhere can never authorize this PR.
    """
    window = list(comment_bodies)[-max_comments:] if comment_bodies else []
    # Newest wins: scan from the tail.
    for body in reversed(window):
        state = parse_state_marker(body)
        if state is None:
            continue
        if repo and state.repo != repo:
            continue
        if pr and int(state.pr) != int(pr):
            continue
        return state
    return None


def _is_attribution_or_hidden(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _HIDDEN_LINE_RE.match(stripped):
        return True
    if _ORIGIN_MARKER_RE.search(stripped):
        return True
    for prefix in _ATTRIBUTION_PREFIXES:
        if stripped.startswith(prefix):
            return True
    # Markdown bold headers emitted by with_attribution.
    if stripped.startswith("**") or stripped.startswith("\u26a1 **"):
        return True
    return False


_CODERABBIT_COMMAND_RE = re.compile(
    r"^@coderabbitai\b.*\bfull\s+review\b", re.IGNORECASE)


def is_coderabbit_command_in_flight(body: object) -> bool:
    """Whether a comment carries an in-flight ``@coderabbitai full review``.

    Branding/emoji/attribution/prose never affect the verdict: attribution
    headers, hidden markers and blank lines are skipped and only a command
    line of the form ``@coderabbitai ... full review`` counts. An exact
    single-line body is NOT required (that was the regression in #282).
    """
    if not isinstance(body, str):
        return False
    for raw in body.replace("\r\n", "\n").split("\n"):
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if _is_attribution_or_hidden(line):
            continue
        # Quoted/code mentions are not commands.
        if line.startswith(">") or line.startswith("`"):
            continue
        if _CODERABBIT_COMMAND_RE.match(line):
            return True
    return False


def coderabbit_command_marker(head: str, attempt: int = 0) -> str:
    """Hidden idempotency marker for one exact-HEAD CodeRabbit command."""
    return (f"<!-- continuum-coderabbit-command head={_require_head(head)} "
            f"attempt={int(attempt)} -->")


def has_exact_head_command(comment_bodies: Sequence[object],
                           head: str) -> bool:
    """Whether any bounded comment window shows an exact-HEAD command marker.

    Only hidden markers with the exact HEAD count; branded prose and old-HEAD
    markers never authorize a new HEAD.
    """
    want = _require_head(head)
    pattern = re.compile(
        r"<!--\s*continuum-coderabbit-command\s+head=([0-9a-fA-F]{40})\b[^>]*-->")
    for body in list(comment_bodies)[-MAX_STATE_COMMENTS_SCANNED:]:
        if not isinstance(body, str):
            continue
        for match in pattern.finditer(body):
            if match.group(1).lower() == want:
                return True
    return False


# -- Provider adapters -----------------------------------------------------

def normalize_coderabbit_review(raw: Mapping[str, object],
                                head: str) -> ReviewFact:
    """Normalize a CodeRabbit review payload into a shared ReviewFact."""
    data = dict(raw or {})
    head = _require_head(head)
    decision = str(data.get("decision") or data.get("state") or "unknown")
    decision = decision.strip().lower()
    if decision in ("approved", "approve"):
        norm = "approved"
    elif decision in ("changes_requested", "changes-required",
                      "request_changes", "actionable"):
        norm = "changes_requested"
    elif decision in ("quota_exceeded", "rate_limited", "quota_wait",
                      "quota", "rate-limit"):
        norm = "quota_wait"
    elif decision in ("disabled", "auto_review_disabled"):
        norm = "disabled"
    elif decision in ("commented", "comment"):
        norm = "comment"
    else:
        norm = "unknown"
    fps = tuple(str(x) for x in (data.get("actionable") or []) if str(x))
    try:
        not_before = int(data.get("quota_not_before") or data.get("not_before") or 0)
    except (TypeError, ValueError):
        not_before = 0
    return ReviewFact(provider="coderabbit", decision=norm, head=head,
                      actionable=fps, quota_not_before=not_before)


def normalize_pragent_review(raw: Mapping[str, object],
                             head: str) -> ReviewFact:
    """Normalize a PR-Agent review payload into the same ReviewFact shape."""
    data = dict(raw or {})
    head = _require_head(head)
    rec = str(data.get("merge_recommendation")
              or data.get("decision") or "unknown").strip().lower()
    if rec in ("safe_to_merge", "approved"):
        norm = "approved"
    elif rec in ("changes_required", "merge_with_caution") and data.get(
            "actionable", None) is not None:
        norm = "changes_requested" if data.get("actionable") else "comment"
    elif rec in ("changes_required",):
        norm = "changes_requested"
    elif rec in ("quota_wait", "rate_limited", "quota_exceeded"):
        norm = "quota_wait"
    elif rec in ("disabled",):
        norm = "disabled"
    elif rec in ("comment", "commented"):
        norm = "comment"
    else:
        raw_issues = data.get("key_issues_to_review") or data.get("issues") or []
        norm = "changes_requested" if raw_issues else "unknown"
    fps = tuple(str(x) for x in (data.get("actionable") or []) if str(x))
    try:
        not_before = int(data.get("quota_not_before") or data.get("not_before") or 0)
    except (TypeError, ValueError):
        not_before = 0
    return ReviewFact(provider="pr-agent", decision=norm, head=head,
                      actionable=fps, quota_not_before=not_before)


# -- Credential boundary (orthogonal to lifecycle state) --------------------

READ_ACTIONS = frozenset({
    "discover_pr", "read_comments", "read_reviews", "read_status",
    "read_runs", "read_mergeability",
})
WRITE_ACTIONS = frozenset({
    "comment", "label", "status_write", "dispatch", "merge", "push",
    "resolve_thread", "upsert_state",
})

REQUIRED_CALLER_PERMISSIONS: Dict[str, Dict[str, str]] = {
    "comment": {"issues": "write"},
    "label": {"issues": "write"},
    "status_write": {"statuses": "write"},
    "dispatch": {"actions": "write"},
    "merge": {"contents": "write", "pull-requests": "write"},
    "push": {"contents": "write"},
    "upsert_state": {"issues": "write"},
    "discover_pr": {"pull-requests": "read"},
}

_RANK = {"none": 0, "read": 1, "write": 2}


def requires_pat(action: str) -> bool:
    """Whether an action requires PAT/App (True) or may use GITHUB_TOKEN.

    Same-repo reads use GITHUB_TOKEN; every mutation/dispatch/merge/push
    requires PAT/App because token-suppressed events would break fan-out.
    Unknown actions fail closed to PAT.
    """
    if action in READ_ACTIONS:
        return False
    if action in WRITE_ACTIONS:
        return True
    return True


def validate_caller_permissions(caller_permissions: Mapping[str, object],
                                action: str) -> Tuple[bool, Tuple[str, ...]]:
    """Check consumer caller permissions cover one lifecycle action.

    Returns ``(ok, missing)``; missing is non-empty when the caller must
    grant more permissions. Unknown actions fail closed (not ok).
    """
    required = REQUIRED_CALLER_PERMISSIONS.get(action)
    if required is None:
        return False, (f"unknown-action:{action}",)
    missing = []
    granted = {str(k): str(v or "none").strip().lower()
               for k, v in dict(caller_permissions or {}).items()}
    for key, level in required.items():
        have = _RANK.get(granted.get(key, "none"), 0)
        need = _RANK.get(level, 2)
        if have < need:
            missing.append(f"{key}:{level}")
    return (not missing), tuple(missing)


# -- Parent/child privacy ---------------------------------------------------

PARENT_SAFE_FIELDS = frozenset({
    "schema_version", "pr", "head", "generation", "phase", "provider",
    "review_attempt", "repair_attempt", "conflict_generation", "not_before",
    "finding", "last_event",
})

_CHILD_IDENTITY_KEYS = frozenset({
    "child_repo", "child_repository", "child_owner", "child",
    "repository", "repo", "owner",
})


def sanitize_parent_state(state: Mapping[str, object]) -> Dict[str, object]:
    """Return a parent-visible projection with no child repository identity."""
    data = dict(state or {})
    for key in list(data.keys()):
        lowered = str(key).lower()
        if lowered in _CHILD_IDENTITY_KEYS and lowered != "repo":
            # "repo" on a parent record names the parent itself and is safe;
            # every child-* key is stripped.
            if lowered != "repo":
                data.pop(key, None)
        if lowered in ("child_repo", "child_repository", "child_id",
                       "child-owner", "child_owner"):
            data.pop(key, None)
    # Belt and braces: drop any owner/name shaped value under a child key.
    for key in ("child", "child_id", "child_repo", "child_repository"):
        data.pop(key, None)
    projected = {k: v for k, v in data.items() if k in PARENT_SAFE_FIELDS
                 or k in ("repo", "pr")}
    # Scrub values: key allowlisting alone still leaks when child identity is
    # embedded in an allowed value such as last_event, finding, or repo.
    for key in list(projected.keys()):
        value = projected[key]
        if key == "repo":
            # Any repo value is repository identity. The parent knows its own
            # repo from its own context; a projected (child) repo must never
            # be parent-visible.
            projected[key] = "redacted"
        elif key == "finding":
            text = str(value or "-")
            if text != "-" and ("/" in text or not _FINDING_RE.match(text)):
                projected[key] = "-"
        elif key == "last_event":
            text = str(value or "redacted")
            if not _LAST_EVENT_RE.match(text):
                projected[key] = "redacted"
        elif isinstance(value, str) and "/" in value:
            projected[key] = "redacted"
    return projected


# -- Legacy compatibility (migration only) ----------------------------------

def label_allows_admission(labels: Iterable[object]) -> bool:
    """Labels are admission/UI hints only, never the transactional lock."""
    names = {str(x) for x in (labels or [])}
    if "automation:paused" in names:
        return False
    return True


def coerce_legacy_evidence(legacy: Mapping[str, object],
                           head: str) -> Optional[ReviewFact]:
    """Coerce a legacy marker into a ReviewFact only for the exact HEAD.

    Returns None for any other HEAD so stale history can neither block nor
    authorize a new HEAD. New state never depends on human-visible prose:
    only hidden legacy markers with an exact HEAD are honored, and only
    until the authoritative record exists (dual-write/shadow phase).
    """
    data = dict(legacy or {})
    marker_head = str(data.get("head") or "").strip().lower()
    if not marker_head or marker_head != _require_head(head):
        return None
    decision = str(data.get("decision") or "unknown").strip().lower()
    if decision not in ("approved", "changes_requested", "comment",
                        "quota_wait", "disabled", "unknown"):
        decision = "unknown"
    fps = tuple(str(x) for x in (data.get("actionable") or []) if str(x))
    return ReviewFact(provider=str(data.get("provider") or "unknown"),
                      decision=decision, head=_require_head(head),
                      actionable=fps)


def shadow_compare(old_action: str, decision: Decision) -> Dict[str, object]:
    """Compare a legacy projection decision vs the reducer (shadow mode)."""
    match = str(old_action or "") == decision.action
    return {"match": match, "legacy": str(old_action or ""),
            "reducer": decision.action,
            "idempotency_key": decision.idempotency_key}


# -- The reducer -------------------------------------------------------------

def _error(repo: str, pr: int, head: str, generation: int, reason: str,
           last_event: str = "error") -> Transition:
    state = LifecycleState(repo=repo, pr=pr, head=head, generation=generation,
                           phase=TERMINAL_BLOCK,
                           last_event=_sanitize_last_event(last_event, 64))
    return Transition(
        state=state,
        decision=Decision(action="error", workflow_failure=True, reason=reason,
                          emit_side_effect=False,
                          idempotency_key=state.idempotency_key()),
    )


def _wait(state: LifecycleState, reason: str,
          not_before: int = 0) -> Transition:
    return Transition(
        state=state,
        decision=Decision(action="wait", workflow_failure=False, reason=reason,
                          emit_side_effect=False,
                          idempotency_key=state.idempotency_key(),
                          not_before=not_before),
    )


def _checked_transition(prior: LifecycleState,
                        phase: str) -> bool:
    allowed = ALLOWED_TRANSITIONS.get(prior.phase, frozenset())
    return phase in allowed


def _facts_changed_since(prior: LifecycleState, facts: LifecycleFacts,
                         review: Optional[ReviewFact]) -> bool:
    """Whether material lifecycle facts changed since ``prior``.

    The default ``event_id`` (``f"{facts.event}:{head[:12]}"``) repeats for
    every cron wakeup without an explicit id, so coalescing on
    ``event_id == prior.last_event`` alone would treat a CI flip from pending
    to pass as a duplicate noop and stick the lifecycle. Only coalesce when
    the material facts are also identical.
    """
    if not facts.config_valid or not facts.authenticated:
        return prior.phase != TERMINAL_BLOCK
    if facts.wip_full:
        return True
    if facts.mergeable == "dirty":
        return prior.phase != CONFLICT_REPAIR
    if facts.ci == "fail":
        return prior.phase != WAITING_RECHECK
    if facts.ci == "pass":
        if prior.phase in (WAITING_CI, WAITING_REVIEW, WAITING_RECHECK):
            return True
    if review is not None:
        if review.decision == "quota_wait":
            if prior.phase != RATE_LIMITED:
                return True
            try:
                quota = int(review.quota_not_before or 0)
            except (TypeError, ValueError):
                quota = 0
            return quota != int(prior.not_before or 0)
        if review.decision == "disabled":
            return prior.phase != REVIEW_IN_FLIGHT
        try:
            fingerprint = _finding_fp(review.actionable)
        except Exception:
            fingerprint = "-"
        if fingerprint != (prior.finding or "-"):
            return True
        if review.decision == "approved" and not review.actionable:
            if facts.mergeable == "clean":
                return prior.phase not in (MERGE_READY, MERGED)
            return prior.phase in (WAITING_CI, WAITING_REVIEW,
                                   WAITING_RECHECK, REVIEW_IN_FLIGHT,
                                   REPAIR_IN_FLIGHT)
        if review.decision == "changes_requested" or review.actionable:
            return prior.phase != REPAIR_IN_FLIGHT
        if review.decision in ("comment", "unknown"):
            return prior.phase != WAITING_RECHECK
        return prior.phase in (WAITING_CI, WAITING_REVIEW, WAITING_RECHECK)
    return False


def reduce(prior: Optional[LifecycleState], facts: LifecycleFacts,
           now: int = 0) -> Transition:
    """Own one lifecycle transition from latest facts + prior state.

    Exact-HEAD safe, idempotent, monotonic within one generation (unless the
    transition graph explicitly allows it), fail-closed on ambiguous
    evidence, and explicit about WAIT vs ERROR. Performs no I/O: callers
    supply bounded latest facts, so API reads stay bounded by construction.
    """
    try:
        head = _require_head(facts.head)
    except LifecycleError as exc:
        # No valid HEAD identity at all: fail closed without a state record.
        dummy = LifecycleState(repo="unknown/unknown", pr=0,
                               head="0" * 40, generation=1,
                               phase=TERMINAL_BLOCK, last_event="malformed")
        return Transition(
            state=dummy,
            decision=Decision(action="error", workflow_failure=True,
                              reason=str(exc), emit_side_effect=False,
                              idempotency_key=dummy.idempotency_key()),
        )
    if facts.event not in EVENTS:
        if prior is None:
            return _error("unknown/unknown", 0, head, 1,
                          f"unknown event: {facts.event!r}")
        return _error(prior.repo, prior.pr, prior.head, prior.generation,
                      f"unknown event: {facts.event!r}",
                      last_event=facts.event_id or prior.last_event)

    event_id = _sanitize_last_event(
        str(facts.event_id or "").strip() or f"{facts.event}:{head[:12]}", 64)

    if prior is None:
        repo_guess = "unknown/unknown"
        state = LifecycleState(repo=repo_guess, pr=0, head=head, generation=1,
                               phase=WAITING_CI, last_event=event_id[:64])
        return _wait(state, "initialized authoritative record; awaiting CI")

    # Exact-HEAD gate: a new HEAD always starts a new generation. Stale
    # commands/labels/statuses from the old HEAD are dropped here, so they
    # can neither block nor authorize the new HEAD.
    if head != prior.head:
        if prior.phase == MERGED:
            # A merged PR that moves is a new lifecycle; still explicit.
            pass
        nxt = LifecycleState(
            repo=prior.repo, pr=prior.pr, head=head,
            generation=int(prior.generation) + 1, phase=WAITING_CI,
            provider=prior.provider, review_attempt=0, repair_attempt=0,
            conflict_generation=0, not_before=0, finding="-",
            last_event=event_id[:64],
        )
        return _wait(nxt, "new HEAD started a new generation; awaiting CI")

    review = facts.review
    if review is not None and review.head != head:
        # Stale review from another HEAD is ignored (current exact-HEAD
        # review supersedes historical findings/statuses).
        review = None

    # Duplicate wakeup: same event identity and no material change coalesces.
    if event_id == prior.last_event and not _facts_changed_since(
            prior, facts, review):
        return Transition(
            state=prior,
            decision=Decision(action="noop", workflow_failure=False,
                              reason="duplicate wakeup coalesced",
                              emit_side_effect=False,
                              idempotency_key=prior.idempotency_key()),
        )

    if not facts.config_valid:
        nxt = replace(prior, last_event=event_id[:64])
        if _checked_transition(prior, TERMINAL_BLOCK):
            nxt = replace(nxt, phase=TERMINAL_BLOCK)
        return Transition(
            state=nxt,
            decision=Decision(action="error", workflow_failure=True,
                              reason="broken lifecycle configuration",
                              emit_side_effect=False,
                              idempotency_key=nxt.idempotency_key()),
        )
    if not facts.authenticated:
        nxt = replace(prior, last_event=event_id[:64])
        if _checked_transition(prior, TERMINAL_BLOCK):
            nxt = replace(nxt, phase=TERMINAL_BLOCK)
        return Transition(
            state=nxt,
            decision=Decision(action="error", workflow_failure=True,
                              reason="permission/authentication failure",
                              emit_side_effect=False,
                              idempotency_key=nxt.idempotency_key()),
        )

    def move(phase: str, reason: str, action: str, emit: bool,
             failure: bool = False, **updates: object) -> Transition:
        if not _checked_transition(prior, phase):
            return _wait(replace(prior, last_event=event_id[:64]),
                         f"non-monotonic transition refused: "
                         f"{prior.phase}->{phase}")
        nxt = replace(prior, phase=phase, last_event=event_id[:64],
                      **updates)  # type: ignore[arg-type]
        return Transition(
            state=nxt,
            decision=Decision(action=action, workflow_failure=failure,
                              reason=reason, emit_side_effect=emit,
                              idempotency_key=nxt.idempotency_key(),
                              not_before=int(getattr(nxt, "not_before", 0))),
        )

    # Conflict path owns dirty PRs: one dispatch per generation.
    if facts.mergeable == "dirty":
        if prior.phase == CONFLICT_REPAIR and prior.conflict_generation >= \
                MAX_CONFLICT_DISPATCHES_PER_HEAD:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "conflict-repair budget exhausted for this HEAD")
        if prior.phase == CONFLICT_REPAIR and prior.conflict_generation >= 1:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "conflict repair already in flight for this HEAD")
        return move(CONFLICT_REPAIR, "dirty PR needs conflict repair",
                    "dispatch_conflict_repair", True,
                    conflict_generation=int(prior.conflict_generation) + 1)

    if facts.mergeable == "computing":
        return _wait(replace(prior, last_event=event_id[:64]),
                     "mergeability still computing")

    if facts.wip_full:
        # WIP-full backlog waiting is healthy queue visibility, never repair.
        return _wait(replace(prior, last_event=event_id[:64]),
                     "WIP full: eligible backlog waiting")

    # Quota/backpressure WAIT never becomes deterministic failure.
    if review is not None and review.decision == "quota_wait":
        not_before = int(review.quota_not_before or facts_not_before(facts)
                          or now or 0)
        nxt_phase = RATE_LIMITED
        return move(nxt_phase, "provider quota/backpressure wait",
                    "retry_after", False, not_before=not_before)

    if isinstance(review, ReviewFact) and review.decision == "disabled":
        # Auto-review disabled: the controller emits one explicit review.
        if prior.phase == REVIEW_IN_FLIGHT:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "explicit review already in flight")
        if int(prior.review_attempt) >= MAX_REVIEW_ATTEMPTS_PER_HEAD:
            nxt = replace(prior, last_event=event_id[:64])
            if _checked_transition(prior, TERMINAL_BLOCK):
                nxt = replace(nxt, phase=TERMINAL_BLOCK)
            return Transition(
                state=nxt,
                decision=Decision(action="error", workflow_failure=True,
                                  reason="exhausted review recovery",
                                  emit_side_effect=False,
                                  idempotency_key=nxt.idempotency_key()))
        return move(REVIEW_IN_FLIGHT, "explicit controller review dispatched",
                    "dispatch_review", True,
                    review_attempt=int(prior.review_attempt) + 1)

    if facts.ci in ("pending", "unknown") and review is None \
            and facts.mergeable in ("unknown", "clean"):
        # Ambiguous evidence fails closed to WAIT (recheck), never ERROR.
        if prior.phase in (REVIEW_IN_FLIGHT, REPAIR_IN_FLIGHT,
                           CONFLICT_REPAIR, RATE_LIMITED):
            return _wait(replace(prior, last_event=event_id[:64]),
                         "operation already in flight; awaiting outcome")
        return _wait(replace(prior, last_event=event_id[:64]),
                     "ambiguous evidence: awaiting CI/review recheck")

    if facts.ci in ("pending", "unknown"):
        if prior.phase == WAITING_CI:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "CI still running")
        return move(WAITING_CI, "CI still running", "wait", False)

    if facts.ci == "fail":
        return move(WAITING_RECHECK, "CI failed; awaiting recheck",
                    "wait", False)

    # CI is green from here on.
    if review is None:
        if prior.phase == REVIEW_IN_FLIGHT:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "review already in flight for this HEAD")
        if int(prior.review_attempt) >= MAX_REVIEW_ATTEMPTS_PER_HEAD:
            nxt = replace(prior, last_event=event_id[:64])
            if _checked_transition(prior, TERMINAL_BLOCK):
                nxt = replace(nxt, phase=TERMINAL_BLOCK)
            return Transition(
                state=nxt,
                decision=Decision(action="error", workflow_failure=True,
                                  reason="exhausted review recovery",
                                  emit_side_effect=False,
                                  idempotency_key=nxt.idempotency_key()))
        return move(REVIEW_IN_FLIGHT, "review dispatched for exact HEAD",
                    "dispatch_review", True,
                    review_attempt=int(prior.review_attempt) + 1)

    if review.decision == "approved" and not review.actionable \
            and facts.mergeable == "clean":
        return move(MERGE_READY, "exact-HEAD review approved; ready to merge",
                    "merge", True)

    if review.decision in ("changes_requested",) or review.actionable:
        fp = ",".join(sorted(set(review.actionable)))[:128] or "-"
        finding = _finding_fp(review.actionable)
        if prior.phase == REPAIR_IN_FLIGHT:
            return _wait(replace(prior, last_event=event_id[:64]),
                         "repair already in flight for this HEAD")
        if int(prior.repair_attempt) >= MAX_REPAIR_ATTEMPTS_PER_HEAD:
            nxt = replace(prior, last_event=event_id[:64])
            if _checked_transition(prior, TERMINAL_BLOCK):
                nxt = replace(nxt, phase=TERMINAL_BLOCK)
            return Transition(
                state=nxt,
                decision=Decision(action="hold", workflow_failure=False,
                                  reason="repair budget exhausted; "
                                  "awaiting new HEAD",
                                  emit_side_effect=False,
                                  idempotency_key=nxt.idempotency_key()))
        _ = fp
        return move(REPAIR_IN_FLIGHT,
                    "actionable finding needs one repair", "dispatch_repair",
                    True, repair_attempt=int(prior.repair_attempt) + 1,
                    finding=finding)

    if review.decision in ("comment", "unknown"):
        return move(WAITING_RECHECK,
                    "review needs recheck on exact HEAD", "wait", False)

    return _wait(replace(prior, last_event=event_id[:64]),
                 "no lifecycle advance on this wakeup")


def facts_not_before(facts: LifecycleFacts) -> int:  # pragma: no cover
    return 0


def _finding_fp(actionable: Sequence[str]) -> str:
    import hashlib

    payload = ",".join(sorted({str(x) for x in actionable}))
    if not payload:
        return "-"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "STATE_SCHEMA_VERSION",
    "STATE_MARKER_NAME",
    "MAX_STATE_COMMENTS_SCANNED",
    "WAITING_CI",
    "WAITING_REVIEW",
    "REVIEW_IN_FLIGHT",
    "RATE_LIMITED",
    "REPAIR_IN_FLIGHT",
    "WAITING_RECHECK",
    "MERGE_READY",
    "CONFLICT_REPAIR",
    "MERGED",
    "TERMINAL_BLOCK",
    "PHASES",
    "PROVIDERS",
    "EVENTS",
    "ALLOWED_TRANSITIONS",
    "LifecycleError",
    "LifecycleState",
    "ReviewFact",
    "LifecycleFacts",
    "Decision",
    "Transition",
    "initial_state",
    "render_state_marker",
    "parse_state_marker",
    "load_authoritative_state",
    "is_coderabbit_command_in_flight",
    "coderabbit_command_marker",
    "has_exact_head_command",
    "normalize_coderabbit_review",
    "normalize_pragent_review",
    "requires_pat",
    "validate_caller_permissions",
    "sanitize_parent_state",
    "label_allows_admission",
    "coerce_legacy_evidence",
    "shadow_compare",
    "reduce",
]
