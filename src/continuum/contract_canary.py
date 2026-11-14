"""Bootstrap and verify the live contract-qualification canary (kodmial/continuum#458).

Layer 3 used to require an operator to configure ``CONTINUUM_CANARY_ISSUE``.
This module owns the deterministic half of the replacement: discover exactly
one repository-owned canary issue, fail closed on ambiguous or unreadable
metadata, and only create a fresh issue after the previous one is conclusively
complete and the documented seven-day window has elapsed.

The module is deliberately free of network access. The workflow fetches GitHub
state with ``gh`` and passes the JSON here, which keeps every decision
deterministic and unit-testable. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contract_observation import _latest_pr, observe_canary
from .contract_qualification import (
    GATE_PASS,
    ContractQualificationError,
    GateEvidence,
    evaluate_regression_gate,
    is_full_sha,
)

#: Only Continuum's own dogfood repository may bootstrap a canary issue.
CANARY_OWNER_REPO = "kodmial/continuum"

#: Stable canary identity. It is part of the issue marker and never changes.
CANARY_TASK_ID = "canary-main"

#: The scheduler's explicit admission label required on a live canary issue.
CANARY_READY_LABEL = "automation:ready"

#: At most one fresh canary issue per this documented window.
CANARY_WINDOW_SECONDS = 7 * 24 * 60 * 60

#: The only repository path a canary task may touch. It is documentation-only
#: and outside every product, workflow, consumer, and release surface.
CANARY_FIXTURE_PATH = "docs/contract-canary-fixture.txt"

#: Stable title prefix for the owned canary issue.
CANARY_TITLE = "[continuum-canary] live contract qualification (automated, do not edit)"

#: Exact marker prefix. A body containing this prefix is treated as canary
#: metadata and must therefore be valid, owned, and single.
CANARY_MARKER_PREFIX = "<!-- continuum-contract-canary"

_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_TRUSTED_LOGINS = frozenset({"github-actions[bot]"})
_MARKER_RE = re.compile(
    r"\A<!-- continuum-contract-canary "
    r"repo=(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"task=(?P<task>[A-Za-z0-9_.-]+) -->\Z"
)
_SAFE_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._-]{1,120}\Z")
#: GitHub auto-close keywords pointing at an issue in this repository. The
#: lifecycle writes ``Closes #<issue>`` in its implementation PR, so this is the
#: only body shape that may attribute a merged PR to the canary issue. A bare
#: number (``Closes 100``) and a reference to another repository are not
#: closing references to this repository's issue.
_CLOSING_REFERENCE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b"
    r"\s*:?\s*"
    r"(?:"
    r"#(?P<issue>\d+)"
    r"|(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<cross_issue>\d+)"
    r"|https?://github\.com/(?P<url_repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"/issues/(?P<url_issue>\d+)"
    r")"
    r"(?![\w/-])",
    re.IGNORECASE,
)
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
_INDENTED_CODE_RE = re.compile(r"^(?: {4}| {0,3}\t)")
_ATX_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]|$)")
_THEMATIC_BREAK_RE = re.compile(
    r"^[ \t]{0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})[ \t]*$"
)


def _blank_markdown(text: str) -> str:
    """Replace an inert Markdown span while preserving its line breaks."""
    return "".join("\n" if char == "\n" else "\x00" for char in text)


def _visible_markdown(body: object) -> str:
    """Return the part of a PR body where GitHub acts on closing keywords.

    Closing phrases inside HTML comments, fenced code blocks, indented code
    blocks, and inline code spans are examples, not operative references.
    Blanking those spans with a non-whitespace sentinel keeps them from
    matching and prevents adjacent visible text from joining into a new
    closing reference.

    An indented line opens a code block only at a block boundary, because GFM
    renders a four-space or tab-indented line that directly follows a paragraph
    line as a lazy paragraph continuation. A heading or thematic break ends its
    block too, so an indented line directly after one is already code. Once
    opened, the block continues across blank lines and still-indented lines,
    just as GFM renders it.
    """
    text = str(body or "")
    visible = []
    index = 0
    line_start = True
    block_boundary = True
    line_blank = True
    line_ends_block = False
    while index < len(text):
        if line_start and text[index] != "\n":
            line_end = text.find("\n", index)
            if line_end == -1:
                line_end = len(text)
            line_ends_block = (
                _ATX_HEADING_RE.match(text[index:line_end]) is not None
                or _THEMATIC_BREAK_RE.match(text[index:line_end]) is not None
            )
            opener = _FENCE_OPEN_RE.match(text[index:line_end])
            if opener is not None:
                fence = opener.group("fence")
                cursor = line_end
                end = len(text)
                while cursor < len(text):
                    next_start = cursor + 1
                    next_end = text.find("\n", next_start)
                    if next_end == -1:
                        next_end = len(text)
                    closing = _FENCE_CLOSE_RE.match(text[next_start:next_end])
                    if (
                        closing is not None
                        and closing.group("fence")[0] == fence[0]
                        and len(closing.group("fence")) >= len(fence)
                    ):
                        end = next_end
                        break
                    cursor = next_end
                visible.append(_blank_markdown(text[index:end]))
                index = end
                line_start = False
                block_boundary = True
                line_blank = True
                continue
            if block_boundary and _INDENTED_CODE_RE.match(text[index:line_end]):
                end = line_end
                cursor = line_end + 1 if line_end < len(text) else len(text)
                while cursor < len(text):
                    next_end = text.find("\n", cursor)
                    if next_end == -1:
                        next_end = len(text)
                    candidate = text[cursor:next_end]
                    if _INDENTED_CODE_RE.match(candidate) or not candidate.strip():
                        end = next_end
                        cursor = next_end + 1 if next_end < len(text) else len(text)
                        continue
                    break
                visible.append(_blank_markdown(text[index:end]))
                index = end
                line_start = False
                block_boundary = True
                line_blank = True
                continue
        if text.startswith("<!--", index):
            close = text.find("-->", index + 4)
            end = len(text) if close == -1 else close + 3
            visible.append(_blank_markdown(text[index:end]))
            line_begin = text.rfind("\n", 0, index) + 1
            rest_end = text.find("\n", end)
            if rest_end == -1:
                rest_end = len(text)
            line_blank = (
                not text[line_begin:index].strip()
                and not text[end:rest_end].strip()
            )
            index = end
            line_start = index > 0 and text[index - 1] == "\n"
            continue
        if text[index] == "`":
            run_end = index
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            run = text[index:run_end]
            search = run_end
            close = -1
            while True:
                candidate = text.find(run, search)
                if candidate == -1:
                    break
                candidate_end = candidate + len(run)
                if (
                    (candidate == 0 or text[candidate - 1] != "`")
                    and (candidate_end == len(text) or text[candidate_end] != "`")
                ):
                    close = candidate
                    break
                search = candidate_end
                while search < len(text) and text[search] == "`":
                    search += 1
            if close != -1:
                end = close + len(run)
                visible.append(_blank_markdown(text[index:end]))
                index = end
                line_start = index > 0 and text[index - 1] == "\n"
                line_blank = False
                continue
            visible.append(run)
            index = run_end
            line_start = False
            line_blank = False
            continue
        char = text[index]
        visible.append(char)
        index += 1
        if char == "\n":
            block_boundary = line_blank or line_ends_block
            line_blank = True
            line_ends_block = False
            line_start = True
        else:
            if not char.isspace():
                line_blank = False
            line_start = False
    return "".join(visible)


class CanaryBootstrapError(ValueError):
    """Raised when live-canary ownership cannot be proven safely."""


@dataclass(frozen=True)
class CanaryIssue:
    """A validated repository-owned canary issue."""

    number: int
    state: str
    labels: Tuple[str, ...]
    created_at: str
    closed_at: str
    author: str
    author_association: str
    body: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class CanaryDecision:
    """Deterministic outcome of live-canary discovery."""

    action: str  # observe | create
    issue_number: Optional[int]
    reason: str
    detail: str
    created_at: str = ""
    age_seconds: Optional[int] = None
    completed: bool = False
    previous_issue_number: Optional[int] = None


def _parse_time(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)


def _age_seconds(value: object, now: Optional[datetime]) -> Optional[int]:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0, int((_now(now) - parsed).total_seconds()))


def _labels(issue: Mapping[str, Any]) -> Tuple[str, ...]:
    names = []
    for label in issue.get("labels") or []:
        if isinstance(label, Mapping):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            names.append(name)
    return tuple(names)


def _marker(body: object, expected_repo: str) -> Optional[Tuple[str, str]]:
    """Return ``(repo, task)`` for exactly one valid marker line, or ``None``.

    A body with no marker at all is not canary metadata. Once any line starts
    with the canary marker prefix, however, the body must carry exactly one
    well-formed marker for this repository and task. Anything else is forged,
    stale, or ambiguous metadata and fails closed.
    """
    lines = [
        line.strip()
        for line in str(body or "").splitlines()
        if line.strip().startswith(CANARY_MARKER_PREFIX)
    ]
    if not lines:
        return None
    if len(lines) != 1:
        raise CanaryBootstrapError(
            "canary marker is duplicated; refusing to guess issue ownership"
        )
    match = _MARKER_RE.match(lines[0])
    if match is None:
        raise CanaryBootstrapError(
            "canary marker is malformed; refusing to use forged metadata"
        )
    repo = match.group("repo")
    task = match.group("task")
    if repo.lower() != str(expected_repo or "").strip().lower():
        raise CanaryBootstrapError(
            "canary marker names another repository; refusing to touch it"
        )
    if task != CANARY_TASK_ID:
        raise CanaryBootstrapError(
            "canary marker names another task; refusing to guess ownership"
        )
    return repo, task


def _trusted_author(issue: Mapping[str, Any], expected_repo: str) -> Tuple[str, str]:
    user = issue.get("user") or {}
    login = str(user.get("login") or "").strip()
    association = str(issue.get("author_association") or "").strip().upper()
    owner = str(expected_repo or "").split("/", 1)[0].lower()
    trusted = (
        association in _TRUSTED_ASSOCIATIONS
        or login.lower() in _TRUSTED_LOGINS
        or (owner and login.lower() == owner)
    )
    if not trusted:
        raise CanaryBootstrapError(
            "canary marker is authored by an untrusted actor; refusing forged metadata"
        )
    return login, association


def _owned_issue(issue: Mapping[str, Any], expected_repo: str) -> CanaryIssue:
    try:
        number = int(issue.get("number"))
    except (TypeError, ValueError):
        raise CanaryBootstrapError("canary issue has no numeric number")
    if number < 1:
        raise CanaryBootstrapError("canary issue number must be positive")
    state = str(issue.get("state") or "").strip().lower()
    if state not in ("open", "closed"):
        raise CanaryBootstrapError(
            "canary issue #{} has an unknown state {!r}".format(number, state)
        )
    labels = _labels(issue)
    if CANARY_READY_LABEL not in labels:
        raise CanaryBootstrapError(
            "canary issue #{} is missing the required {} label".format(
                number, CANARY_READY_LABEL
            )
        )
    created_at = str(issue.get("created_at") or issue.get("createdAt") or "").strip()
    if _parse_time(created_at) is None:
        raise CanaryBootstrapError(
            "canary issue #{} has no usable creation timestamp".format(number)
        )
    author, association = _trusted_author(issue, expected_repo)
    return CanaryIssue(
        number=number,
        state=state,
        labels=labels,
        created_at=created_at,
        closed_at=str(issue.get("closed_at") or issue.get("closedAt") or "").strip(),
        author=author,
        author_association=association,
        body=str(issue.get("body") or ""),
        raw=issue,
    )


def select_canary_issue(
    issues: Sequence[Mapping[str, Any]],
    expected_repo: str = CANARY_OWNER_REPO,
) -> Optional[CanaryIssue]:
    """Return the current owned canary issue, or ``None`` when none exists.

    Pull requests are ignored. Any issue carrying the canary marker must be a
    fully valid, labelled, trusted canary. Historical *closed* canaries are
    expected after a replacement, so only multiple *open* canaries are an
    ownership conflict. When every owned canary is closed, the newest one is
    the current canary and older ones remain immutable history.
    """
    found = []
    for raw in issues or []:
        if not isinstance(raw, Mapping):
            raise CanaryBootstrapError("canary inventory contains a non-object entry")
        if raw.get("pull_request"):
            continue
        marker = _marker(raw.get("body"), expected_repo)
        if marker is None:
            continue
        found.append(_owned_issue(raw, expected_repo))
    active = [item for item in found if item.state == "open"]
    if len(active) > 1:
        raise CanaryBootstrapError(
            "multiple open owned canary issues exist ({}) ; refusing to duplicate canary work".format(
                ", ".join("#{}".format(item.number) for item in sorted(active, key=lambda item: item.number))
            )
        )
    if active:
        return active[0]
    if not found:
        return None
    return max(found, key=lambda item: (_parse_time(item.created_at), item.number))


def _configured_number(value: object) -> int:
    text = str(value if value is not None else "").strip()
    if re.fullmatch(r"[1-9][0-9]*", text) is None:
        raise CanaryBootstrapError(
            "CONTINUUM_CANARY_ISSUE must be a positive integer when provided"
        )
    return int(text)


def _pr_attributes_issue(pr: Mapping[str, Any], issue_number: int) -> bool:
    """Whether the lifecycle itself attributed ``pr`` to ``issue_number``.

    Completion is stricter than ordinary liveness: the implementation PR must
    use the lifecycle's canonical ``opencode/issue<N>-`` branch or carry an
    operative GitHub auto-close keyword for the issue. A PR that merely mentions
    the issue number, or only shows the keyword in a Markdown example, is not
    completion evidence.
    """
    branch = str(
        pr.get("headRefName") or pr.get("head_ref_name") or ""
    ).strip()
    if branch.startswith("opencode/issue{}-".format(issue_number)):
        return True
    body = _visible_markdown(pr.get("body"))
    expected_repo = CANARY_OWNER_REPO.lower()
    for match in _CLOSING_REFERENCE_RE.finditer(body):
        number = (
            match.group("issue")
            or match.group("cross_issue")
            or match.group("url_issue")
        )
        if int(number) != issue_number:
            continue
        repo = match.group("repo") or match.group("url_repo")
        if repo is None or repo.lower() == expected_repo:
            return True
    return False


def select_implementation_pr(
    issue_number: int,
    pull_requests: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Return the newest lifecycle-attributed implementation PR for the issue."""
    attributed = [
        pr
        for pr in pull_requests or []
        if isinstance(pr, Mapping) and _pr_attributes_issue(pr, issue_number)
    ]
    return _latest_pr(issue_number, attributed)


def merge_exact_head_runs(
    runs: Sequence[Mapping[str, Any]],
    head_sha: object,
    exact_head_runs: Sequence[Mapping[str, Any]],
) -> list:
    """Replace snapshot run records for one head with an exact-HEAD fetch.

    ``evaluate_regression_gate`` consumes the newest record for the target
    head, so a stale snapshot success must never shadow a fresher exact-HEAD
    failure. Records for other heads are preserved.
    """
    target = str(head_sha or "").strip().lower()
    snapshot = list(runs or [])
    if target:
        snapshot = [
            run
            for run in snapshot
            if not (
                isinstance(run, Mapping)
                and str(run.get("headSha") or run.get("head_sha") or "")
                .strip()
                .lower()
                == target
            )
        ]
    return snapshot + list(exact_head_runs or [])


def validate_completed_canary(
    pull_request: Optional[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    issue_number: int,
) -> Optional[str]:
    """Return a failure reason unless a completed canary PR has exact-HEAD evidence.

    A closed canary issue only proves a live E2E run when its implementation PR
    is attributed to that issue by the lifecycle, is merged, carries a full
    head and merge commit, and has completed successful ``CI`` and ``Contract
    qualification`` runs for that exact head. Evidence for any other SHA is
    stale and never satisfies the check.
    """
    if not pull_request:
        return "no merged implementation PR references the canary issue"
    if not _pr_attributes_issue(pull_request, issue_number):
        return (
            "implementation PR is not attributed to canary issue #{}"
            .format(issue_number)
        )
    merged_at = pull_request.get("mergedAt") or pull_request.get("merged_at")
    if not merged_at:
        return "implementation PR is not merged"
    head = str(
        pull_request.get("headRefOid") or pull_request.get("head_ref_oid") or ""
    ).strip().lower()
    if not is_full_sha(head):
        return "merged implementation PR has no full headRefOid"
    merge_commit = pull_request.get("mergeCommit") or pull_request.get("merge_commit") or {}
    merge_oid = (
        str(merge_commit.get("oid") or "").strip().lower()
        if isinstance(merge_commit, Mapping)
        else ""
    )
    if not is_full_sha(merge_oid):
        return "merged implementation PR has no full merge commit"
    evidence = []
    for run in runs or []:
        if not isinstance(run, Mapping):
            continue
        name = str(run.get("name") or run.get("workflowName") or "").strip()
        sha = str(run.get("headSha") or run.get("head_sha") or "").strip().lower()
        if not name or not sha:
            continue
        conclusion = run.get("conclusion")
        evidence.append(
            GateEvidence(
                name,
                sha,
                str(run.get("status") or "").strip().lower(),
                str(conclusion).strip().lower() if conclusion is not None else None,
            )
        )
    verdict = evaluate_regression_gate(head, evidence)
    if verdict.outcome != GATE_PASS:
        return (
            "merged implementation PR lacks exact-HEAD CI/contract evidence: "
            + verdict.reason
        )
    return None


def resolve_canary(
    issues: Sequence[Mapping[str, Any]],
    pull_requests: Sequence[Mapping[str, Any]] = (),
    runs: Sequence[Mapping[str, Any]] = (),
    configured_issue: object = None,
    expected_repo: str = CANARY_OWNER_REPO,
    now: Optional[datetime] = None,
    read_error: object = None,
    pagination_complete: bool = True,
) -> CanaryDecision:
    """Decide whether to observe the existing canary or create a fresh one.

    ``read_error`` and ``pagination_complete`` model an unreadable inventory:
    every read failure (403/429/422/5xx, timeout, truncated pagination) fails
    closed and never creates a duplicate issue.
    """
    if read_error not in (None, ""):
        raise CanaryBootstrapError(
            "canary inventory read failed ({}); refusing to create a duplicate "
            "issue from incomplete metadata".format(read_error)
        )
    if not pagination_complete:
        raise CanaryBootstrapError(
            "canary inventory pagination was incomplete; refusing to create a "
            "duplicate issue from partial metadata"
        )
    if configured_issue not in (None, ""):
        number = _configured_number(configured_issue)
        return CanaryDecision(
            action="observe",
            issue_number=number,
            reason="configured-override",
            detail="CONTINUUM_CANARY_ISSUE selects the live canary issue",
        )
    if str(expected_repo or "").strip().lower() != CANARY_OWNER_REPO:
        raise CanaryBootstrapError(
            "live-canary bootstrap is only allowed in {}; refusing to touch {}"
            .format(CANARY_OWNER_REPO, expected_repo or "<unknown>")
        )
    issue = select_canary_issue(issues, expected_repo)
    if issue is None:
        return CanaryDecision(
            action="create",
            issue_number=None,
            reason="initial-bootstrap",
            detail="no repository-owned canary issue exists",
        )
    age = _age_seconds(issue.created_at, now)
    if age is None:
        raise CanaryBootstrapError(
            "canary issue #{} has no usable creation timestamp".format(issue.number)
        )
    if issue.state == "open":
        return CanaryDecision(
            action="observe",
            issue_number=issue.number,
            reason="active-canary",
            detail="the owned canary issue is open",
            created_at=issue.created_at,
            age_seconds=age,
            completed=False,
        )
    try:
        observed = observe_canary(
            issue.raw, runs=runs, pull_requests=pull_requests, now=now
        )
    except ContractQualificationError as exc:
        raise CanaryBootstrapError(
            "previous canary #{} could not be evaluated: {}".format(issue.number, exc)
        )
    if observed.failure:
        raise CanaryBootstrapError(
            "previous canary #{} is not conclusively complete: {}".format(
                issue.number, observed.failure
            )
        )
    if not observed.completed:
        raise CanaryBootstrapError(
            "previous canary #{} is closed without a merged implementation PR".format(
                issue.number
            )
        )
    problem = validate_completed_canary(
        select_implementation_pr(issue.number, pull_requests), runs, issue.number
    )
    if problem:
        raise CanaryBootstrapError(
            "previous canary #{} completion is not trustworthy: {}".format(
                issue.number, problem
            )
        )
    if age >= CANARY_WINDOW_SECONDS:
        return CanaryDecision(
            action="create",
            issue_number=None,
            reason="window-elapsed",
            detail=(
                "previous canary #{} completed and the {}-day window elapsed"
                .format(issue.number, CANARY_WINDOW_SECONDS // (24 * 60 * 60))
            ),
            created_at=issue.created_at,
            age_seconds=age,
            completed=True,
            previous_issue_number=issue.number,
        )
    return CanaryDecision(
        action="observe",
        issue_number=issue.number,
        reason="completed-canary",
        detail="the owned canary completed inside the freshness window",
        created_at=issue.created_at,
        age_seconds=age,
        completed=True,
    )


def _safe_token(value: object, name: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if _SAFE_TOKEN_RE.fullmatch(text) is None:
        raise CanaryBootstrapError(
            "{} must match [A-Za-z0-9._-] and be at most 120 characters".format(name)
        )
    return text


def build_canary_issue(
    *,
    nonce: object,
    run_id: object = "",
    run_attempt: object = "",
    repo: str = CANARY_OWNER_REPO,
) -> Tuple[str, str]:
    """Return the stable title and bounded body for a fresh canary issue."""
    nonce_text = _safe_token(nonce, "canary nonce")
    run_id_text = _safe_token(run_id, "run_id", allow_empty=True)
    attempt_text = _safe_token(run_attempt, "run_attempt", allow_empty=True)
    expected = str(repo or "").strip()
    if expected.lower() != CANARY_OWNER_REPO:
        raise CanaryBootstrapError(
            "canary issues may only be created in {}".format(CANARY_OWNER_REPO)
        )
    marker = "<!-- continuum-contract-canary repo={} task={} -->".format(
        CANARY_OWNER_REPO, CANARY_TASK_ID
    )
    body = "\n".join(
        [
            marker,
            "⚡ **Continuum · contract-canary**",
            "<!-- continuum-origin role=continuum component=contract-canary -->",
            "",
            "# Automated live canary",
            "",
            "This issue is repository-owned and is generated by the scheduled Contract",
            "qualification canary. It is intentionally harmless. The normal issue",
            "scheduler owns the implementation; do not ask for human input.",
            "",
            "## Task",
            "",
            "Create or replace only `{}`. Its entire content must be exactly:".format(
                CANARY_FIXTURE_PATH
            ),
            "",
            "```",
            "continuum-canary nonce={}".format(nonce_text),
            "```",
            "",
            "Do not modify any other file, workflow, configuration, or consumer repository.",
            "",
            "## Definition of Done",
            "",
            "- [ ] `{}` contains exactly `continuum-canary nonce={}` and no other content."
            .format(CANARY_FIXTURE_PATH, nonce_text),
            "- [ ] The implementation PR closes this issue.",
            "- [ ] The ordinary CI and Contract qualification checks pass on the exact PR HEAD.",
            "- [ ] The guarded merge path merges the PR and source-issue reconciliation closes this issue.",
            "",
            "Canary run: `{}` attempt `{}` (automation metadata; not a secret).".format(
                run_id_text or "unknown", attempt_text or "unknown"
            ),
            "",
        ]
    )
    return CANARY_TITLE, body
