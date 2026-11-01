"""Immutable issue task-specification snapshots (kodmial/continuum#295).

Mechanical fix for contract drift: the issue title and body are immutable
for a single implementation generation once admission starts. OpenCode
issue mode resolves ``issue.title``/``issue.body`` live, so without a
pinned contract a later edit silently redefines an in-flight agent's
accepted specification.

Contract summary
----------------
* The canonical task contract is a GitHub-native durable, versioned
  snapshot: a bot-owned (or owner-owned) hidden-marker issue comment
  carrying the exact title/body plus SHA-256 digests, keyed by
  ``repository + issue number + implementation generation``.
* Canonical encoding is UTF-8 over NFC-normalized text with ``\\r\\n`` /
  ``\\r`` normalized to ``\\n``. The digest algorithm is SHA-256.
* The snapshot is created/pinned **before any agent may implement** and
  the exact snapshot (never a later live body) is passed into OpenCode.
* The pinned digest is verified against live issue content at execution
  entry, before PR publication, before any repair/review-as-task, and
  right before any merge decision. Machine-readable PR provenance carries
  the snapshot identity forward.
* On mismatch the generation **stops** with an actionable diagnostic and
  an issue/PR link: no silent rebaseline, no spurious "fixed" verdict,
  no permanent blind retry loop. The automation never rewrites or reverts
  the user's issue body; post-admission changes belong in a new
  follow-up issue.
* Admission is idempotent with stable generation identity: the earliest
  trusted snapshot for ``(repo, issue, generation)`` wins and is never
  overwritten. Simultaneous wakeups converge on the earliest record;
  partial writes are atomic at the GitHub comment level (create either
  succeeds or fails closed).
* Issue labels/comments/status may change freely: only the title/body
  digests are compared, so label churn never reads as drift.
* Tasks that started before this guard (no snapshot, no PR provenance
  marker) are explicitly classified ``legacy_unpinned``: they are never
  falsely declared protected, and in-flight legacy PRs may finish their
  current generation while every fresh admission pins first.

This module performs no I/O: workflows read live GitHub state, feed plain
data here, and execute the returned decision. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

#: Schema version of the snapshot record. Bump only with a migration note.
SNAPSHOT_SCHEMA_VERSION = 1

#: Hidden marker identifying a snapshot comment (issue) or snapshot
#: reference (PR body). The issue comment carries the full record in a
#: fenced JSON block after this line; the PR body carries only this
#: reference line (digests, no full text) next to the existing
#: ``continuum-task-context`` marker.
SNAPSHOT_MARKER = "continuum-task-snapshot"
SNAPSHOT_REF_MARKER = "continuum-task-snapshot-ref"

#: Default implementation generation. Generation 1 is the first (and
#: usually only) implementation attempt for an issue; retries, no-diff
#: retries, PR repair, review, conflict recovery and delegation reuse the
#: same generation. A new generation is only ever started explicitly.
DEFAULT_GENERATION = 1

#: Comment authorships trusted to publish snapshot state. Mirrors the
#: conflict-repair trust model: repository-side authority
#: (OWNER/MEMBER/COLLABORATOR) plus the workflow-owned bot login, which
#: commonly carries NONE association. Arbitrary external commenters can
#: never forge snapshot state.
TRUSTED_COMMENT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TRUSTED_AUTOMATION_LOGINS = frozenset({"github-actions[bot]"})

_HEX64_RE = re.compile(r"[0-9a-f]{64}")

_SNAPSHOT_MARKER_RE = re.compile(
    r"<!--\s*continuum-task-snapshot\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s+"
    r"title-sha256=([0-9a-fA-F]{64})\s+"
    r"body-sha256=([0-9a-fA-F]{64})\s+"
    r"spec-sha256=([0-9a-fA-F]{64})\s*-->"
)

_SNAPSHOT_REF_RE = re.compile(
    r"<!--\s*continuum-task-snapshot-ref\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s+"
    r"title-sha256=([0-9a-fA-F]{64})\s+"
    r"body-sha256=([0-9a-fA-F]{64})\s+"
    r"spec-sha256=([0-9a-fA-F]{64})\s*-->"
)

_JSON_BLOCK_RE = re.compile(r"```json\s+(.*?)\s+```", re.DOTALL)


class TaskSnapshotError(ValueError):
    """Raised when snapshot inputs cannot be interpreted safely."""


# ---------------------------------------------------------------------------
# Canonical encoding and digests.
# ---------------------------------------------------------------------------

def normalize_spec_text(value: Any) -> str:
    """Return the canonical form of an issue title or body.

    ``None`` becomes ``""``. Line endings normalize to ``\\n`` and text
    normalizes to Unicode NFC. No stripping is performed: trailing
    whitespace is part of the specification.
    """

    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def sha256_hex(text: str) -> str:
    """SHA-256 hex digest of UTF-8 bytes (canonical encoding)."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def title_digest(title: Any) -> str:
    """Digest of the canonical issue title."""

    return sha256_hex(normalize_spec_text(title))


def body_digest(body: Any) -> str:
    """Digest of the canonical issue body."""

    return sha256_hex(normalize_spec_text(body))


def spec_digest(title: Any, body: Any) -> str:
    """Combined specification digest binding title and body together.

    Computed over the two fixed-length hex digests (not over raw text)
    so no separator ambiguity can merge distinct pairs.
    """

    payload = "{}:{}".format(title_digest(title), body_digest(body))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def digests_match(left: str, right: str) -> bool:
    """Case-insensitive hex digest comparison (both must be 64-hex)."""

    try:
        left_norm = str(left or "").strip().lower()
        right_norm = str(right or "").strip().lower()
    except Exception:
        return False
    if not _HEX64_RE.fullmatch(left_norm) or not _HEX64_RE.fullmatch(right_norm):
        return False
    return left_norm == right_norm


# ---------------------------------------------------------------------------
# Repository / generation identity.
# ---------------------------------------------------------------------------

def normalize_repo(value: Any) -> str:
    """Canonical ``owner/name`` key (lowercased; GitHub names are case-insensitive)."""

    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.\-]+/[a-z0-9_.\-]+", text):
        raise TaskSnapshotError("repo must look like owner/name, got {!r}".format(value))
    return text


def normalize_generation(value: Any) -> int:
    """Validate an implementation generation (positive integer)."""

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TaskSnapshotError("generation must be a positive integer") from exc
    if number < 1:
        raise TaskSnapshotError("generation must be a positive integer")
    return number


def normalize_issue_number(value: Any) -> int:
    """Validate an issue/PR number (positive integer)."""

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TaskSnapshotError("issue number must be a positive integer") from exc
    if number < 1:
        raise TaskSnapshotError("issue number must be a positive integer")
    return number


# ---------------------------------------------------------------------------
# Snapshot record construction, rendering, and parsing.
# ---------------------------------------------------------------------------

def build_snapshot(
    repo: Any,
    issue: Any,
    title: Any,
    body: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
    created_by: str = "",
    created_at: str = "",
) -> dict:
    """Build the canonical snapshot record for exact title/body content."""

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    canonical_title = normalize_spec_text(title)
    canonical_body = normalize_spec_text(body)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "repo": repository,
        "issue": number,
        "generation": generation_number,
        "title": canonical_title,
        "body": canonical_body,
        "title_sha256": title_digest(canonical_title),
        "body_sha256": body_digest(canonical_body),
        "spec_sha256": spec_digest(canonical_title, canonical_body),
        "created_by": str(created_by or ""),
        "created_at": str(created_at or ""),
    }


def snapshot_marker_line(snapshot: Mapping[str, Any]) -> str:
    """Return the machine-readable marker line for a snapshot record."""

    return (
        "<!-- {} repo={} issue={} generation={} "
        "title-sha256={} body-sha256={} spec-sha256={} -->".format(
            SNAPSHOT_MARKER,
            snapshot["repo"],
            snapshot["issue"],
            snapshot["generation"],
            snapshot["title_sha256"],
            snapshot["body_sha256"],
            snapshot["spec_sha256"],
        )
    )


def render_snapshot_comment(snapshot: Mapping[str, Any]) -> str:
    """Render the GitHub-native durable snapshot comment body.

    The marker line is machine-readable without JSON parsing; the fenced
    JSON block carries the recoverable exact title/body. Human prose is
    informational only and is never parsed as state.
    """

    record = dict(snapshot)
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return "\n".join(
        [
            snapshot_marker_line(record),
            "Continuum task snapshot (machine-owned, do not edit or delete).",
            "This comment pins the exact issue title/body accepted for this "
            "implementation generation. Later issue edits never change it; "
            "they belong in a new follow-up issue.",
            "",
            "```json",
            payload,
            "```",
        ]
    )


def render_pr_snapshot_ref(snapshot: Mapping[str, Any]) -> str:
    """Render the short machine-readable PR provenance reference line."""

    return (
        "<!-- {} repo={} issue={} generation={} "
        "title-sha256={} body-sha256={} spec-sha256={} -->".format(
            SNAPSHOT_REF_MARKER,
            snapshot["repo"],
            snapshot["issue"],
            snapshot["generation"],
            snapshot["title_sha256"],
            snapshot["body_sha256"],
            snapshot["spec_sha256"],
        )
    )


@dataclass(frozen=True)
class ParsedSnapshot:
    """A parsed snapshot comment with integrity status."""

    record: Optional[dict]
    repo: str
    issue: int
    generation: int
    title_sha256: str
    body_sha256: str
    spec_sha256: str
    tampered: bool
    tamper_reason: str = ""


def parse_snapshot_comment(body: Any) -> Optional[ParsedSnapshot]:
    """Parse one comment body as a snapshot record, or return None.

    Returns ``None`` when the body carries no snapshot marker at all
    (ordinary human prose). When a marker is present but the embedded
    record is missing, unparseable, or digest-inconsistent, the result is
    marked ``tampered`` so callers fail closed instead of silently
    falling back to live mutable text.
    """

    text = str(body or "")
    match = _SNAPSHOT_MARKER_RE.search(text)
    if not match:
        return None
    repo_text, issue_text, generation_text, title_hex, body_hex, spec_hex = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation_number = normalize_generation(generation_text)
    except TaskSnapshotError as exc:
        return ParsedSnapshot(
            record=None,
            repo=str(repo_text or ""),
            issue=0,
            generation=0,
            title_sha256=title_hex.lower(),
            body_sha256=body_hex.lower(),
            spec_sha256=spec_hex.lower(),
            tampered=True,
            tamper_reason="malformed snapshot identity: {}".format(exc),
        )
    block = _JSON_BLOCK_RE.search(text)
    if not block:
        return ParsedSnapshot(
            record=None,
            repo=repository,
            issue=number,
            generation=generation_number,
            title_sha256=title_hex.lower(),
            body_sha256=body_hex.lower(),
            spec_sha256=spec_hex.lower(),
            tampered=True,
            tamper_reason="snapshot marker without recoverable JSON content",
        )
    try:
        record = json.loads(block.group(1))
    except (json.JSONDecodeError, ValueError) as exc:
        return ParsedSnapshot(
            record=None,
            repo=repository,
            issue=number,
            generation=generation_number,
            title_sha256=title_hex.lower(),
            body_sha256=body_hex.lower(),
            spec_sha256=spec_hex.lower(),
            tampered=True,
            tamper_reason="snapshot JSON is unparseable: {}".format(exc),
        )
    if not isinstance(record, dict):
        return ParsedSnapshot(
            record=None,
            repo=repository,
            issue=number,
            generation=generation_number,
            title_sha256=title_hex.lower(),
            body_sha256=body_hex.lower(),
            spec_sha256=spec_hex.lower(),
            tampered=True,
            tamper_reason="snapshot JSON is not an object",
        )
    embedded_title = normalize_spec_text(record.get("title", ""))
    embedded_body = normalize_spec_text(record.get("body", ""))
    expected_title = title_digest(embedded_title)
    expected_body = body_digest(embedded_body)
    expected_spec = spec_digest(embedded_title, embedded_body)
    problems = []
    if expected_title != title_hex.lower():
        problems.append("title digest mismatch")
    if expected_body != body_hex.lower():
        problems.append("body digest mismatch")
    if expected_spec != spec_hex.lower():
        problems.append("spec digest mismatch")
    if str(record.get("title_sha256", "")).lower() != title_hex.lower():
        problems.append("embedded title digest disagrees with marker")
    if str(record.get("body_sha256", "")).lower() != body_hex.lower():
        problems.append("embedded body digest disagrees with marker")
    if str(record.get("spec_sha256", "")).lower() != spec_hex.lower():
        problems.append("embedded spec digest disagrees with marker")
    try:
        record_repo = normalize_repo(record.get("repo", ""))
        record_issue = normalize_issue_number(record.get("issue", 0))
        record_generation = normalize_generation(record.get("generation", 0))
    except TaskSnapshotError as exc:
        problems.append("embedded identity invalid: {}".format(exc))
        record_repo, record_issue, record_generation = repository, number, generation_number
    else:
        if record_repo != repository or record_issue != number or record_generation != generation_number:
            problems.append("embedded identity disagrees with marker")
    if problems:
        return ParsedSnapshot(
            record=record,
            repo=repository,
            issue=number,
            generation=generation_number,
            title_sha256=title_hex.lower(),
            body_sha256=body_hex.lower(),
            spec_sha256=spec_hex.lower(),
            tampered=True,
            tamper_reason="; ".join(problems),
        )
    return ParsedSnapshot(
        record=record,
        repo=repository,
        issue=number,
        generation=generation_number,
        title_sha256=title_hex.lower(),
        body_sha256=body_hex.lower(),
        spec_sha256=spec_hex.lower(),
        tampered=False,
    )


@dataclass(frozen=True)
class ParsedSnapshotRef:
    """A parsed PR-body snapshot reference."""

    repo: str
    issue: int
    generation: int
    title_sha256: str
    body_sha256: str
    spec_sha256: str


def parse_pr_snapshot_ref(pr_body: Any) -> Optional[ParsedSnapshotRef]:
    """Parse the machine-readable snapshot reference from a PR body."""

    match = _SNAPSHOT_REF_RE.search(str(pr_body or ""))
    if not match:
        return None
    repo_text, issue_text, generation_text, title_hex, body_hex, spec_hex = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation_number = normalize_generation(generation_text)
    except TaskSnapshotError:
        return None
    return ParsedSnapshotRef(
        repo=repository,
        issue=number,
        generation=generation_number,
        title_sha256=title_hex.lower(),
        body_sha256=body_hex.lower(),
        spec_sha256=spec_hex.lower(),
    )


# ---------------------------------------------------------------------------
# Trust, selection, and verification over plain comment data.
# ---------------------------------------------------------------------------

def _comment_login(comment: Mapping[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, Mapping):
        login = str(user.get("login") or "").strip()
        if login:
            return login.lower()
    for key in ("author_login", "author", "login"):
        login = str(comment.get(key) or "").strip()
        if login:
            return login.lower()
    return ""


def is_trusted_snapshot_comment(
    comment: Mapping[str, Any],
    *,
    owner_login: str = "",
) -> bool:
    """Whether a comment is authorized to publish snapshot state."""

    association = str(comment.get("author_association") or "").upper()
    if association in TRUSTED_COMMENT_ASSOCIATIONS:
        return True
    login = _comment_login(comment)
    if login in {item.lower() for item in TRUSTED_AUTOMATION_LOGINS}:
        return True
    owner = str(owner_login or "").strip().lower()
    return bool(owner) and login == owner


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SnapshotSelection:
    """Outcome of selecting the authoritative snapshot for a generation."""

    status: str  # found | absent | tampered
    snapshot: Optional[ParsedSnapshot]
    tamper_reason: str = ""


def select_snapshot(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> SnapshotSelection:
    """Select the authoritative snapshot for one implementation generation.

    The earliest trusted snapshot wins; later duplicates are ignored and
    never overwrite it. Untrusted marker-lookalikes are ignored. A
    present-but-tampered earliest record fails closed as ``tampered``
    rather than falling back to live text or to a later duplicate.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    candidates = []
    tampered: Optional[ParsedSnapshot] = None
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        parsed = parse_snapshot_comment(comment.get("body", ""))
        if parsed is None:
            continue
        if parsed.repo != repository or parsed.issue != number:
            continue
        if parsed.generation != generation_number:
            continue
        created = _parse_time(comment.get("created_at"))
        candidates.append((created, comment.get("id"), parsed))
        if parsed.tampered and tampered is None:
            tampered = parsed
    if not candidates:
        if tampered is not None:
            return SnapshotSelection(
                status="tampered",
                snapshot=tampered,
                tamper_reason=tampered.tamper_reason,
            )
        # A tampered record from an untrusted author is not authoritative
        # state at all: report absent so a trusted pin can still be created.
        # Check for any marker-shaped body to distinguish tamper evidence.
        # Only trusted authors can produce authoritative tamper evidence:
        # untrusted marker lookalikes are ignored as absent so a fork user
        # can never block a trusted pin (denial-of-service).
        for comment in comments or []:
            if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
                continue
            parsed = parse_snapshot_comment(comment.get("body", ""))
            if (
                parsed is not None
                and parsed.repo == repository
                and parsed.issue == number
                and parsed.generation == generation_number
                and parsed.tampered
            ):
                return SnapshotSelection(
                    status="tampered",
                    snapshot=parsed,
                    tamper_reason=parsed.tamper_reason,
                )
        return SnapshotSelection(status="absent", snapshot=None)
    # Earliest wins: None timestamps sort last so undated records never
    # shadow a dated earlier pin; id tie-break keeps the order stable.
    def _sort_key(item: Any) -> Any:
        created, comment_id, _parsed = item
        try:
            numeric_id = int(comment_id) if comment_id is not None else 0
        except (TypeError, ValueError):
            numeric_id = 0
        return (
            created is None,
            created or datetime.min.replace(tzinfo=timezone.utc),
            numeric_id,
        )

    candidates.sort(key=_sort_key)
    earliest = candidates[0][2]
    if earliest.tampered:
        return SnapshotSelection(
            status="tampered", snapshot=earliest, tamper_reason=earliest.tamper_reason
        )
    return SnapshotSelection(status="found", snapshot=earliest)


@dataclass(frozen=True)
class DriftVerdict:
    """Result of comparing live issue content against a pinned snapshot."""

    matches: bool
    title_matches: bool
    body_matches: bool
    live_title_sha256: str
    live_body_sha256: str
    live_spec_sha256: str


def verify_live_against_snapshot(
    live_title: Any,
    live_body: Any,
    snapshot: ParsedSnapshot,
) -> DriftVerdict:
    """Compare live issue title/body to the pinned snapshot digests.

    Labels, comments and open/closed status are not inputs here by
    design: they may change independently without modifying the
    specification.
    """

    live_title_hex = title_digest(live_title)
    live_body_hex = body_digest(live_body)
    live_spec_hex = spec_digest(live_title, live_body)
    title_ok = digests_match(live_title_hex, snapshot.title_sha256)
    body_ok = digests_match(live_body_hex, snapshot.body_sha256)
    return DriftVerdict(
        matches=bool(title_ok and body_ok),
        title_matches=bool(title_ok),
        body_matches=bool(body_ok),
        live_title_sha256=live_title_hex,
        live_body_sha256=live_body_hex,
        live_spec_sha256=live_spec_hex,
    )


@dataclass(frozen=True)
class GateDecision:
    """One deterministic admission/lifecycle gate decision."""

    action: str
    reason: str
    diagnostic: str = ""


def issue_link(repo: str, issue: int) -> str:
    """Human-actionable link fragment for diagnostics."""

    return "https://github.com/{}/issues/{}".format(repo, issue)


def pr_link(repo: str, pr_number: int) -> str:
    """Human-actionable link fragment for diagnostics."""

    return "https://github.com/{}/pull/{}".format(repo, pr_number)


def decide_admission(
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    live_title: Any = "",
    live_body: Any = "",
    comments: Sequence[Mapping[str, Any]] = (),
    owner_login: str = "",
) -> GateDecision:
    """Decide admission for one implementation generation.

    * No trusted snapshot yet -> ``pin``: the caller must persist a
      snapshot of the live content first and then verify; the agent must
      not implement before that.
    * Snapshot present and live matches -> ``proceed``: the exact
      snapshot content (not the live body) is the task contract.
    * Snapshot present but live differs -> ``drift_blocked``: stop with
      an actionable diagnostic; never silently rebaseline.
    * Snapshot present but tampered -> ``tampered_fail_closed``.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    selection = select_snapshot(
        comments, repo=repository, issue=number,
        generation=generation_number, owner_login=owner_login,
    )
    link = issue_link(repository, number)
    if selection.status == "absent" or selection.snapshot is None:
        return GateDecision(
            action="pin",
            reason="no snapshot pinned for generation",
            diagnostic=(
                "No task snapshot is pinned for {}#{} generation {}. "
                "Pin the exact issue title/body before any agent work; "
                "the agent must implement only the pinned snapshot. "
                "See {}".format(repository, number, generation_number, link)
            ),
        )
    if selection.status == "tampered" or selection.snapshot.tampered:
        return GateDecision(
            action="tampered_fail_closed",
            reason="snapshot integrity failure",
            diagnostic=(
                "Task snapshot for {}#{} generation {} failed integrity "
                "validation ({}). Stop: never fall back to live mutable "
                "text. An owner must inspect the snapshot comment history. "
                "See {}".format(
                    repository, number, generation_number,
                    selection.tamper_reason or "digest mismatch", link)
            ),
        )
    verdict = verify_live_against_snapshot(live_title, live_body, selection.snapshot)
    if verdict.matches:
        return GateDecision(
            action="proceed",
            reason="live content matches pinned snapshot",
            diagnostic="",
        )
    parts = []
    if not verdict.title_matches:
        parts.append("title")
    if not verdict.body_matches:
        parts.append("body")
    return GateDecision(
        action="drift_blocked",
        reason="live {} differs from pinned snapshot".format(" and ".join(parts)),
        diagnostic=(
            "Task contract drift detected for {}#{} generation {}: live "
            "{} no longer matches the pinned snapshot "
            "(spec {}). Stop dispatch/implementation for this generation; "
            "do not rebaseline silently. Open a new follow-up issue with "
            "an explicit dependency instead of editing the working issue. "
            "Pinned spec={} live spec={}. See {}".format(
                repository, number, generation_number, " and ".join(parts),
                selection.snapshot.spec_sha256,
                selection.snapshot.spec_sha256, verdict.live_spec_sha256, link)
        ),
    )


def decide_execution_gate(
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    live_title: Any = "",
    live_body: Any = "",
    comments: Sequence[Mapping[str, Any]] = (),
    owner_login: str = "",
    stage: str = "execution-entry",
    pr_number: int = 0,
) -> GateDecision:
    """Decide a lifecycle gate (entry, pre-PR, repair, review, pre-merge).

    Same semantics as :func:`decide_admission` with stage-aware
    diagnostics. ``stage`` is one of ``execution-entry``, ``pre-pr``,
    ``repair``, ``review`` or ``pre-merge``; ``pr_number`` is included in
    the diagnostic when nonzero.
    """

    allowed = {"execution-entry", "pre-pr", "repair", "review", "pre-merge"}
    if stage not in allowed:
        raise TaskSnapshotError(
            "stage must be one of: {}".format(", ".join(sorted(allowed)))
        )
    decision = decide_admission(
        repo=repo, issue=issue, generation=generation,
        live_title=live_title, live_body=live_body,
        comments=comments, owner_login=owner_login,
    )
    if decision.action == "proceed":
        return decision
    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    context = " (PR #{})".format(int(pr_number)) if pr_number else ""
    link = pr_link(repository, int(pr_number)) if pr_number else issue_link(repository, number)
    stage_text = {
        "execution-entry": "at execution entry",
        "pre-pr": "before publishing the implementation PR",
        "repair": "before repair",
        "review": "before review-as-task",
        "pre-merge": "before merge",
    }[stage]
    return GateDecision(
        action=decision.action,
        reason=decision.reason,
        diagnostic="{} {}{}. {}".format(
            decision.diagnostic.rstrip(), stage_text, context, "See {}".format(link)
        ).strip(),
    )


def verify_pr_provenance_against_live(
    *,
    pr_body: Any,
    live_title: Any,
    live_body: Any,
    comments: Sequence[Mapping[str, Any]] = (),
    owner_login: str = "",
    expected_repo: Any = "",
    expected_issue: Any = 0,
    expected_generation: Any = DEFAULT_GENERATION,
) -> GateDecision:
    """Verify a PR's embedded snapshot reference against live issue content.

    * No reference marker -> ``legacy_unpinned``: a PR created before the
      guard; never falsely declared protected, allowed to finish its
      current generation while fresh admissions pin first.
    * Reference present but live differs -> ``drift_blocked``.
    * Reference present and live matches -> ``proceed``.

    When issue ``comments`` are supplied, the PR reference is additionally
    bound to the authoritative issue snapshot selected via
    :func:`select_snapshot` before any live comparison: a coordinated edit
    of the live issue plus the mutable PR-body reference cannot bypass
    drift detection, since the durable snapshot comment is the pinned
    contract. Absent/tampered snapshots or digest disagreement fail closed
    as ``tampered_fail_closed``.
    """

    ref = parse_pr_snapshot_ref(pr_body)
    if ref is None:
        return GateDecision(
            action="legacy_unpinned",
            reason="PR carries no task snapshot reference",
            diagnostic=(
                "This PR predates immutable task snapshots: it carries no "
                "continuum-task-snapshot-ref marker, so it is NOT drift "
                "protected. It may finish its current generation, but every "
                "fresh issue admission must pin a snapshot first."
            ),
        )
    if expected_repo:
        try:
            if normalize_repo(expected_repo) != ref.repo:
                return GateDecision(
                    action="drift_blocked",
                    reason="PR snapshot reference points at another repository",
                    diagnostic=(
                        "PR snapshot reference {}#{} does not match expected "
                        "{}. Stop; never repair across repositories silently.".format(
                            ref.repo, ref.issue, normalize_repo(expected_repo))
                    ),
                )
        except TaskSnapshotError as exc:
            return GateDecision(
                action="tampered_fail_closed",
                reason="invalid expected repository",
                diagnostic=str(exc),
            )
    if expected_issue:
        try:
            if normalize_issue_number(expected_issue) != ref.issue:
                return GateDecision(
                    action="drift_blocked",
                    reason="PR snapshot reference points at another issue",
                    diagnostic=(
                        "PR snapshot reference #{} does not match expected "
                        "#{} on {}. Stop.".format(
                            ref.issue, normalize_issue_number(expected_issue), ref.repo)
                    ),
                )
        except TaskSnapshotError as exc:
            return GateDecision(
                action="tampered_fail_closed",
                reason="invalid expected issue",
                diagnostic=str(exc),
            )
    try:
        expected_gen = normalize_generation(expected_generation)
    except TaskSnapshotError as exc:
        return GateDecision(
            action="tampered_fail_closed", reason="invalid generation",
            diagnostic=str(exc),
        )
    if expected_gen != ref.generation:
        return GateDecision(
            action="stale_generation",
            reason="PR snapshot reference is for another generation",
            diagnostic=(
                "PR snapshot reference generation {} does not match expected "
                "generation {} for {}#{}; the retry targets a stale contract. "
                "Stop.".format(ref.generation, expected_gen, ref.repo, ref.issue)
            ),
        )
    bound_spec_hex = sha256_hex(
        "{}:{}".format(ref.title_sha256.lower(), ref.body_sha256.lower())
    )
    if not digests_match(bound_spec_hex, ref.spec_sha256):
        return GateDecision(
            action="tampered_fail_closed",
            reason="PR snapshot reference fails provenance binding",
            diagnostic=(
                "PR snapshot reference for {}#{} generation {} carries "
                "title/body digests that do not bind to its spec digest "
                "(spec {}); the reference is inconsistent or tampered. "
                "Stop; never carry an unverifiable provenance identifier "
                "forward. See {}".format(
                    ref.repo, ref.issue, ref.generation,
                    ref.spec_sha256, issue_link(ref.repo, ref.issue))
            ),
        )
    if comments:
        try:
            selection = select_snapshot(
                comments, repo=ref.repo, issue=ref.issue,
                generation=ref.generation, owner_login=owner_login,
            )
        except TaskSnapshotError as exc:
            return GateDecision(
                action="tampered_fail_closed",
                reason="invalid snapshot identity",
                diagnostic=str(exc),
            )
        if selection.status == "tampered":
            return GateDecision(
                action="tampered_fail_closed",
                reason="snapshot integrity failure",
                diagnostic=selection.tamper_reason,
            )
        if selection.status != "found" or selection.snapshot is None:
            return GateDecision(
                action="tampered_fail_closed",
                reason="no authoritative issue snapshot to bind PR ref against",
                diagnostic=(
                    "PR carries a task snapshot reference for {}#{} "
                    "generation {} but no authoritative issue snapshot pins "
                    "that contract; coordinated issue plus PR-reference edits "
                    "cannot be ruled out. Stop; never trust the mutable "
                    "PR-body reference alone. See {}".format(
                        ref.repo, ref.issue, ref.generation,
                        issue_link(ref.repo, ref.issue))
                ),
            )
        snapshot = selection.snapshot
        if not (
            digests_match(snapshot.title_sha256, ref.title_sha256)
            and digests_match(snapshot.body_sha256, ref.body_sha256)
            and digests_match(snapshot.spec_sha256, ref.spec_sha256)
        ):
            return GateDecision(
                action="tampered_fail_closed",
                reason="PR provenance disagrees with authoritative snapshot",
                diagnostic=(
                    "PR snapshot reference for {}#{} generation {} does not "
                    "match the authoritative issue snapshot; the provenance "
                    "identifier cannot be carried forward. See {}".format(
                        ref.repo, ref.issue, ref.generation,
                        issue_link(ref.repo, ref.issue))
                ),
            )
    live_title_hex = title_digest(live_title)
    live_body_hex = body_digest(live_body)
    if digests_match(live_title_hex, ref.title_sha256) and digests_match(
        live_body_hex, ref.body_sha256
    ):
        return GateDecision(action="proceed", reason="PR provenance matches live issue")
    parts = []
    if not digests_match(live_title_hex, ref.title_sha256):
        parts.append("title")
    if not digests_match(live_body_hex, ref.body_sha256):
        parts.append("body")
    link = issue_link(ref.repo, ref.issue)
    return GateDecision(
        action="drift_blocked",
        reason="live {} differs from PR snapshot provenance".format(" and ".join(parts)),
        diagnostic=(
            "Task contract drift detected for {}#{} generation {}: live {} "
            "no longer matches the PR's pinned snapshot reference "
            "(spec {}). Stop repair/merge for this generation; open a new "
            "follow-up issue instead of editing the working issue. See {}".format(
                ref.repo, ref.issue, ref.generation, " and ".join(parts),
                ref.spec_sha256, link)
        ),
    )


def classify_pr(
    pr_body: Any,
    *,
    comments: Sequence[Mapping[str, Any]] = (),
    repo: Any = "",
    issue: Any = 0,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> GateDecision:
    """Migration classifier for already-running PRs without a snapshot.

    Returns ``legacy_unpinned`` when neither a PR provenance marker nor an
    issue snapshot exists (explicit safe treatment, never a false claim of
    immutability), otherwise defers to the snapshot admission decision.
    """

    ref = parse_pr_snapshot_ref(pr_body)
    if ref is None and repo and issue:
        try:
            selection = select_snapshot(
                comments, repo=repo, issue=issue,
                generation=generation, owner_login=owner_login,
            )
        except TaskSnapshotError as exc:
            return GateDecision(
                action="tampered_fail_closed", reason="invalid identity",
                diagnostic=str(exc),
            )
        if selection.status == "tampered":
            return GateDecision(
                action="tampered_fail_closed",
                reason="snapshot integrity failure",
                diagnostic=selection.tamper_reason,
            )
        if selection.status == "found":
            return GateDecision(
                action="protected",
                reason="issue snapshot protects PR without ref",
            )
        if selection.status == "absent":
            return GateDecision(
                action="legacy_unpinned",
                reason="no snapshot and no PR provenance marker",
                diagnostic=(
                    "Legacy in-flight work: no task snapshot is pinned and "
                    "the PR carries no snapshot reference, so drift "
                    "protection does not apply retrospectively. Finish or "
                    "close this generation explicitly; fresh admissions pin "
                    "first."
                ),
            )
    if ref is None:
        return GateDecision(
            action="legacy_unpinned",
            reason="PR carries no task snapshot reference",
            diagnostic=(
                "Legacy in-flight work without snapshot provenance; not "
                "drift protected."
            ),
        )
    bound_spec_hex = sha256_hex(
        "{}:{}".format(ref.title_sha256.lower(), ref.body_sha256.lower())
    )
    if not digests_match(bound_spec_hex, ref.spec_sha256):
        return GateDecision(
            action="tampered_fail_closed",
            reason="PR snapshot reference fails provenance binding",
            diagnostic=(
                "PR snapshot reference for {}#{} generation {} carries "
                "title/body digests that do not bind to its spec digest "
                "(spec {}); the reference is inconsistent or tampered. "
                "Stop. See {}".format(
                    ref.repo, ref.issue, ref.generation,
                    ref.spec_sha256, issue_link(ref.repo, ref.issue))
            ),
        )
    if repo and issue:
        try:
            selection = select_snapshot(
                comments, repo=repo, issue=issue,
                generation=generation, owner_login=owner_login,
            )
        except TaskSnapshotError as exc:
            return GateDecision(
                action="tampered_fail_closed", reason="invalid identity",
                diagnostic=str(exc),
            )
        if selection.status == "tampered":
            return GateDecision(
                action="tampered_fail_closed",
                reason="snapshot integrity failure",
                diagnostic=selection.tamper_reason,
            )
        if selection.status == "found":
            snapshot = selection.snapshot
            if (
                snapshot is not None
                and not snapshot.tampered
                and digests_match(snapshot.title_sha256, ref.title_sha256)
                and digests_match(snapshot.body_sha256, ref.body_sha256)
                and digests_match(snapshot.spec_sha256, ref.spec_sha256)
            ):
                return GateDecision(
                    action="protected", reason="PR carries snapshot provenance"
                )
            return GateDecision(
                action="tampered_fail_closed",
                reason="PR provenance disagrees with authoritative snapshot",
                diagnostic=(
                    "PR snapshot reference for {}#{} generation {} does not "
                    "match the authoritative issue snapshot; the provenance "
                    "identifier cannot be carried forward. See {}".format(
                        ref.repo, ref.issue, ref.generation,
                        issue_link(ref.repo, ref.issue))
                ),
            )
    return GateDecision(action="protected", reason="PR carries snapshot provenance")


# ---------------------------------------------------------------------------
# Terminal generation-stop state (kodmial/continuum#309).
#
# A truly drifted/tampered generation must stop exactly once and must
# never be redispatched by scheduler cron, event wakeups, lease reclaim,
# retries, or manual ``/oc``. The stop is a deterministic typed issue
# comment keyed by (repo, issue, generation, pinned/live hashes, reason)
# carrying digests only -- never prompt or task text -- so repeated
# ``/oc`` + drift pairs, repeated API calls, and ghost WIP leases are
# impossible. A genuine successor issue, or an explicit new generation
# number, remains schedulable without rewriting the earlier pinned
# snapshot. An explicit owner recovery marker posted *after* the stop
# re-arms exactly that generation; nothing else does.
#
# Fresh-read rule (same issue): callers must pass live title/body taken
# from a fresh authoritative GitHub issue API response fetched inside
# the exact verification step. Title/body text carried through prior
# GitHub Actions step outputs or environment variables is NOT an
# authoritative live source: long bodies (~55k characters) can be
# altered/truncated/interpolated in transit and read back as false
# drift. All workflow admission paths re-fetch before hashing.
# ---------------------------------------------------------------------------

#: Typed terminal stop marker for one stopped implementation generation.
GENERATION_STOP_MARKER = "continuum-task-generation-stop"

#: Explicit owner recovery marker re-arming one stopped generation.
GENERATION_RECOVER_MARKER = "continuum-task-generation-recover"

#: Allowed stop reasons (privacy-safe, no free-form text).
GENERATION_STOP_REASONS = frozenset({"drift", "tampered"})

_GENERATION_STOP_RE = re.compile(
    r"<!--\s*continuum-task-generation-stop\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s+"
    r"pinned-spec=([0-9a-fA-F]{64})\s+"
    r"live-spec=([0-9a-fA-F]{64})\s+"
    r"reason=(drift|tampered)\s*-->"
)

_GENERATION_RECOVER_RE = re.compile(
    r"<!--\s*continuum-task-generation-recover\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s*-->"
)


@dataclass(frozen=True)
class ParsedGenerationStop:
    """A parsed terminal generation-stop record (digests only)."""

    repo: str
    issue: int
    generation: int
    pinned_spec: str
    live_spec: str
    reason: str


@dataclass(frozen=True)
class ParsedGenerationRecover:
    """A parsed explicit owner recovery record for one generation."""

    repo: str
    issue: int
    generation: int


def parse_generation_stop(body: Any) -> Optional[ParsedGenerationStop]:
    """Parse one comment body as a terminal generation-stop, or None."""

    match = _GENERATION_STOP_RE.search(str(body or ""))
    if not match:
        return None
    repo_text, issue_text, generation_text, pinned_hex, live_hex, reason = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation_number = normalize_generation(generation_text)
    except TaskSnapshotError:
        return None
    if reason not in GENERATION_STOP_REASONS:
        return None
    return ParsedGenerationStop(
        repo=repository,
        issue=number,
        generation=generation_number,
        pinned_spec=pinned_hex.lower(),
        live_spec=live_hex.lower(),
        reason=reason,
    )


def parse_generation_recover(body: Any) -> Optional[ParsedGenerationRecover]:
    """Parse one comment body as an explicit generation recovery, or None."""

    match = _GENERATION_RECOVER_RE.search(str(body or ""))
    if not match:
        return None
    repo_text, issue_text, generation_text = match.groups()
    try:
        repository = normalize_repo(repo_text)
        number = normalize_issue_number(issue_text)
        generation_number = normalize_generation(generation_text)
    except TaskSnapshotError:
        return None
    return ParsedGenerationRecover(
        repo=repository, issue=number, generation=generation_number
    )


def _stop_sort_key(item: Any) -> Any:
    created, comment_id, _parsed = item
    try:
        numeric_id = int(comment_id) if comment_id is not None else 0
    except (TypeError, ValueError):
        numeric_id = 0
    return (
        created is None,
        created or datetime.min.replace(tzinfo=timezone.utc),
        numeric_id,
    )


def select_generation_stop(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> Optional[ParsedGenerationStop]:
    """Return the earliest trusted terminal stop for a generation, if any.

    Later duplicate stops never overwrite the first: selection is
    first-writer-wins, mirroring snapshot selection. Untrusted
    marker-lookalikes are ignored.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    candidates = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        parsed = parse_generation_stop(comment.get("body", ""))
        if parsed is None:
            continue
        if (
            parsed.repo != repository
            or parsed.issue != number
            or parsed.generation != generation_number
        ):
            continue
        candidates.append((_parse_time(comment.get("created_at")), comment.get("id"), parsed))
    if not candidates:
        return None
    candidates.sort(key=_stop_sort_key)
    return candidates[0][2]


def is_generation_terminally_stopped(
    comments: Sequence[Mapping[str, Any]],
    *,
    repo: Any,
    issue: Any,
    generation: Any = DEFAULT_GENERATION,
    owner_login: str = "",
) -> bool:
    """Whether a generation is terminally stopped without later recovery.

    A generation is stopped when a trusted stop marker exists for its
    exact identity and no trusted recovery marker for the same identity
    was created strictly after the latest stop. Recovery is the only
    in-place re-arm; a successor issue or a new generation number needs
    no recovery marker because neither rewrites the stopped record.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    stops = []
    recovers = []
    for comment in comments or []:
        if not is_trusted_snapshot_comment(comment, owner_login=owner_login):
            continue
        stop = parse_generation_stop(comment.get("body", ""))
        if (
            stop is not None
            and stop.repo == repository
            and stop.issue == number
            and stop.generation == generation_number
        ):
            stops.append((_parse_time(comment.get("created_at")), comment.get("id"), stop))
            continue
        recover = parse_generation_recover(comment.get("body", ""))
        if (
            recover is not None
            and recover.repo == repository
            and recover.issue == number
            and recover.generation == generation_number
        ):
            recovers.append((_parse_time(comment.get("created_at")), comment.get("id"), recover))
    if not stops:
        return False
    stops.sort(key=_stop_sort_key)
    recovers.sort(key=_stop_sort_key)
    if not recovers:
        return True
    # A recovery re-arms only when it is strictly later than the
    # latest stop (timestamp, then comment id). Equal/earlier
    # recovery markers never cancel an existing stop.
    return _stop_sort_key(recovers[-1]) <= _stop_sort_key(stops[-1])


def render_generation_stop(
    repo: Any,
    issue: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
    pinned_spec: str = "",
    live_spec: str = "",
    reason: str = "drift",
) -> str:
    """Render the deterministic terminal stop comment body.

    The body carries digests only -- never title/body text -- so the
    stop state cannot leak prompt or user content. Raises
    :class:`TaskSnapshotError` on malformed identity, digests, or
    reason so callers fail closed instead of publishing unparseable
    state.
    """

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    pinned = str(pinned_spec or "").strip().lower()
    live = str(live_spec or "").strip().lower()
    why = str(reason or "").strip().lower()
    if not _HEX64_RE.fullmatch(pinned):
        raise TaskSnapshotError("pinned-spec must be a 64-hex digest")
    if not _HEX64_RE.fullmatch(live):
        raise TaskSnapshotError("live-spec must be a 64-hex digest")
    if why not in GENERATION_STOP_REASONS:
        raise TaskSnapshotError("reason must be one of: drift, tampered")
    marker = (
        "<!-- {} repo={} issue={} generation={} "
        "pinned-spec={} live-spec={} reason={} -->".format(
            GENERATION_STOP_MARKER, repository, number, generation_number,
            pinned, live, why,
        )
    )
    return "\n".join(
        [
            marker,
            "Continuum terminal generation stop (machine-owned, do not edit or delete).",
            "This generation is stopped: open a successor issue with an explicit "
            "dependency, start an explicit new generation, or post an explicit "
            "owner recovery marker. Scheduler, retry, lease-reclaim, and manual "
            "/oc paths must not redispatch this generation.",
        ]
    )


def render_generation_recover(
    repo: Any,
    issue: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
) -> str:
    """Render the explicit owner recovery comment body for one generation."""

    repository = normalize_repo(repo)
    number = normalize_issue_number(issue)
    generation_number = normalize_generation(generation)
    marker = "<!-- {} repo={} issue={} generation={} -->".format(
        GENERATION_RECOVER_MARKER, repository, number, generation_number
    )
    return "\n".join(
        [
            marker,
            "Continuum explicit owner recovery for one stopped generation.",
        ]
    )


def generation_stop_skip_reason(
    repo: Any,
    issue: Any,
    *,
    generation: Any = DEFAULT_GENERATION,
    pinned_spec: str = "",
    live_spec: str = "",
    reason: str = "drift",
) -> str:
    """Auditable stable scheduler skip reason for a stopped generation."""

    try:
        repository = normalize_repo(repo)
        number = normalize_issue_number(issue)
        generation_number = normalize_generation(generation)
    except TaskSnapshotError:
        return "terminal generation stop: invalid generation identity"
    pinned = str(pinned_spec or "").strip().lower()[:12] or "unknown"
    live = str(live_spec or "").strip().lower()[:12] or "unknown"
    why = str(reason or "drift").strip().lower()
    return (
        "terminal generation stop: {}#{} generation {} is stopped "
        "({} pinned {} live {}); open a successor issue with an explicit "
        "dependency or start an explicit new generation instead of "
        "redispatching".format(repository, number, generation_number, why, pinned, live)
    )


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SNAPSHOT_MARKER",
    "SNAPSHOT_REF_MARKER",
    "GENERATION_STOP_MARKER",
    "GENERATION_RECOVER_MARKER",
    "GENERATION_STOP_REASONS",
    "DEFAULT_GENERATION",
    "TRUSTED_COMMENT_ASSOCIATIONS",
    "TRUSTED_AUTOMATION_LOGINS",
    "TaskSnapshotError",
    "ParsedSnapshot",
    "ParsedSnapshotRef",
    "ParsedGenerationStop",
    "ParsedGenerationRecover",
    "SnapshotSelection",
    "DriftVerdict",
    "GateDecision",
    "normalize_spec_text",
    "sha256_hex",
    "title_digest",
    "body_digest",
    "spec_digest",
    "digests_match",
    "normalize_repo",
    "normalize_generation",
    "normalize_issue_number",
    "build_snapshot",
    "snapshot_marker_line",
    "render_snapshot_comment",
    "render_pr_snapshot_ref",
    "parse_snapshot_comment",
    "parse_pr_snapshot_ref",
    "is_trusted_snapshot_comment",
    "select_snapshot",
    "verify_live_against_snapshot",
    "decide_admission",
    "decide_execution_gate",
    "verify_pr_provenance_against_live",
    "classify_pr",
    "parse_generation_stop",
    "parse_generation_recover",
    "select_generation_stop",
    "is_generation_terminally_stopped",
    "render_generation_stop",
    "render_generation_recover",
    "generation_stop_skip_reason",
    "issue_link",
    "pr_link",
]
