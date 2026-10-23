"""Owner-armed, predecessor-completion-gated issue resume (kodmial/continuum#382).

A paused issue is re-admitted only when the repository owner places exactly
one authorization marker line::

    <!-- continuum-resume-after:v1 predecessor=374 -->

The marker line must be the first line of an owner-authored API comment; the
owner may append an explanation after that line.  Inline prose or any other
marker-shaped comment is never authorization.

The marker is metadata, never an OpenCode command.  It authorizes one narrow
transition for its own target issue after the named predecessor is provably
complete.  The canonical scheduler/watchdog recovery paths remain the only
executors; this module is a pure decision model and performs no I/O.

Predecessor completion requires all of the following trusted evidence:

* the predecessor issue is *closed as completed*;
* its latest implementation PR is bound by the existing
  ``opencode/issue<number>-`` branch identity or, for an owner-authored
  normal fix branch, by the exact ``continuum-task-context`` plus
  ``continuum-task-snapshot-ref`` markers in the PR body verified against
  the durable task-snapshot comment on the predecessor issue (never by
  matching titles);
* that PR is merged into the expected default branch with a real merge
  commit and exact HEAD;
* the exact PR HEAD has completed/successful project CI and Contract Gate
  runs;
* the exact PR HEAD carries independent clean-review evidence (the durable
  PR-Agent clean status or an approving review from a different actor).

Anything missing, ambiguous, failed, canceled, or unreadable yields ``WAIT``
or ``NOOP``.  A marker can never revive a different paused issue, and a prior
post-marker dispatch attempt fences a failed recovery so repeated wakes do not
re-arm it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, Set, Tuple

from . import task_snapshot


WAIT = "WAIT"
RESUME = "RESUME"
NOOP = "NOOP"

RESUME_MARKER = "continuum-resume-after:v1"
RESUME_MARKER_RE = re.compile(
    r"\A[ \t]*<!--\s*continuum-resume-after:v1\s+predecessor=([1-9][0-9]*)\s*-->"
    r"[ \t]*(?:\r?\n|\Z)"
)
RESUME_MARKER_LINE_RE = re.compile(
    r"^[ \t]*<!--\s*continuum-resume-after:v1\s+predecessor=([1-9][0-9]*)\s*-->"
    r"[ \t]*$",
    re.MULTILINE,
)
MARKER_SHAPE_RE = re.compile(r"continuum-resume-after(?::v1)?\b")
DISPATCH_MARKER = "<!-- issue-scheduler-dispatch -->"
OWNER_COMMAND_RE = re.compile(
    r"^[ ]{0,3}/(?:opencode|oc)(?=\s|$)", re.MULTILINE
)
RECOVERY_MARKER_RE = re.compile(
    r"<!--\s*continuum-429-(?:recovery|resumption|backoff)\b"
)
GENERATION_STOP_RE = re.compile(
    r"<!--\s*continuum-task-generation-stop\s+"
    r"repo=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+"
    r"issue=(\d+)\s+generation=(\d+)\s+"
    r"pinned-spec=[0-9a-fA-F]{64}\s+live-spec=[0-9a-fA-F]{64}\s+"
    r"reason=(drift|tampered)\s*-->"
)
GENERATION_RECOVER_RE = re.compile(
    r"<!--\s*continuum-task-generation-recover\s+"
    r"repo=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+"
    r"issue=(\d+)\s+generation=(\d+)\s*-->"
)
FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
CLEAN_PR_AGENT_REVIEW = "PR-Agent review complete: clean"
TASK_CONTEXT_RE = re.compile(
    r"<!--\s*continuum-task-context\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+issue=(\d+)\s*-->"
)
PR_SNAPSHOT_REF_RE = re.compile(
    r"<!--\s*continuum-task-snapshot-ref\s+"
    r"repo=([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\s+"
    r"issue=(\d+)\s+generation=(\d+)\s+"
    r"title-sha256=([0-9a-fA-F]{64})\s+"
    r"body-sha256=([0-9a-fA-F]{64})\s+"
    r"spec-sha256=([0-9a-fA-F]{64})\s*-->"
)


@dataclass(frozen=True)
class ResumeInstruction:
    """One validated owner authorization marker."""

    target: int
    predecessor: int
    created_at: datetime
    comment_id: str = ""


@dataclass(frozen=True)
class MarkerScan:
    """Result of scanning one issue's comments for authorization markers."""

    instruction: Optional[ResumeInstruction]
    reason: str


@dataclass(frozen=True)
class ResumeDecision:
    """One typed, side-effect-free owner-resume decision."""

    state: str
    reason: str
    instruction: Optional[ResumeInstruction] = None
    predecessor_pr: Optional[int] = None
    predecessor_head: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in (WAIT, RESUME, NOOP):
            raise ValueError("unknown owner-resume state: {}".format(self.state))


def _body(comment: Any) -> str:
    if isinstance(comment, Mapping):
        return str(comment.get("body") or "")
    return str(getattr(comment, "body", "") or "")


def _login(comment: Any) -> str:
    if isinstance(comment, Mapping):
        user = comment.get("user")
        if isinstance(user, Mapping):
            return str(user.get("login") or "")
        return str(comment.get("author_login") or "")
    return str(getattr(comment, "author_login", "") or "")


def _comment_id(comment: Any) -> str:
    if isinstance(comment, Mapping):
        return str(comment.get("id") or "")
    return str(getattr(comment, "id", "") or "")


def _created_at(comment: Any) -> Optional[datetime]:
    value = None
    if isinstance(comment, Mapping):
        value = comment.get("created_at") or comment.get("updated_at")
    else:
        value = getattr(comment, "created_at", None)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_owner(comment: Any, repository_owner: str) -> bool:
    login = _login(comment)
    if login:
        return login.lower() == str(repository_owner or "").lower()
    if isinstance(comment, Mapping):
        return False
    # CommentRecord carries the already-validated API owner bit.
    return bool(getattr(comment, "author_is_owner", False))


def _is_trusted_stop(comment: Any, repository_owner: str) -> bool:
    if _is_owner(comment, repository_owner):
        return True
    login = _login(comment).lower()
    if login == "github-actions[bot]":
        return True
    if isinstance(comment, Mapping):
        association = str(comment.get("author_association") or "").upper()
        return association in ("OWNER", "MEMBER", "COLLABORATOR")
    # CommentRecord carries the adapter's already-validated trusted-actor bit.
    return bool(getattr(comment, "author_is_owner", False))


def _order_key(comment: Any) -> Tuple[str, str]:
    created = _created_at(comment)
    stamp = created.isoformat() if created else ""
    return (stamp, _comment_id(comment).zfill(12))


def _marker_shaped(comments: Iterable[Any]) -> list:
    return [
        comment
        for comment in comments or ()
        if MARKER_SHAPE_RE.search(_body(comment))
    ]


def scan_resume_marker(
    comments: Sequence[Any],
    *,
    target: int,
    repository_owner: str,
) -> MarkerScan:
    """Validate the target's authorization marker set.

    Exactly one marker-shaped comment is accepted.  It must be authored by
    the repository owner *as reported by the GitHub API*, use the exact
    supported syntax, and name a positive predecessor different from the
    target.  Duplicates and conflicts fail closed.
    """

    shaped = _marker_shaped(comments)
    if not shaped:
        return MarkerScan(None, "not owner-armed")
    if len(shaped) != 1:
        return MarkerScan(None, "duplicate or conflicting owner-resume markers")
    comment = shaped[0]
    if not _is_owner(comment, repository_owner):
        return MarkerScan(None, "owner-resume marker author is not the repository owner")
    body = _body(comment)
    if len(MARKER_SHAPE_RE.findall(body)) != 1:
        return MarkerScan(None, "duplicate or conflicting owner-resume markers")
    match = RESUME_MARKER_RE.match(body)
    if not match or len(RESUME_MARKER_LINE_RE.findall(body)) != 1:
        return MarkerScan(None, "owner-resume marker syntax is not supported")
    predecessor = int(match.group(1))
    if predecessor == int(target):
        return MarkerScan(None, "owner-resume marker cannot name its own target")
    created = _created_at(comment)
    if created is None:
        return MarkerScan(None, "owner-resume marker has no trusted timestamp")
    return MarkerScan(
        ResumeInstruction(
            target=int(target),
            predecessor=predecessor,
            created_at=created,
            comment_id=_comment_id(comment),
        ),
        "owner-authorized resume",
    )


def resume_baseline(
    comments: Sequence[Any],
    *,
    target: int,
    repository_owner: str,
) -> Optional[datetime]:
    """Return the valid marker timestamp that scopes a fresh retry budget.

    The scheduler counts only dispatch attempts after this timestamp, so
    historic 429/infrastructure attempts cannot immediately re-pause an
    owner-armed target.  No valid marker means no reset.
    """

    scan = scan_resume_marker(
        comments, target=target, repository_owner=repository_owner
    )
    return scan.instruction.created_at if scan.instruction else None


def _has_activity_after(
    comments: Sequence[Any],
    instruction: ResumeInstruction,
    repository_owner: str,
    dispatch_marker: str,
) -> bool:
    baseline = instruction.created_at
    for comment in comments or ():
        created = _created_at(comment)
        if created is None or created <= baseline:
            continue
        if instruction.comment_id and _comment_id(comment) == instruction.comment_id:
            continue
        body = _body(comment)
        if dispatch_marker and dispatch_marker in body:
            return True
        if RECOVERY_MARKER_RE.search(body):
            return True
        if _is_owner(comment, repository_owner) and OWNER_COMMAND_RE.search(body):
            return True
    return False


def generation_stopped(
    comments: Sequence[Any],
    *,
    target: int,
    repository: str,
    repository_owner: str,
    generation: int = 1,
) -> bool:
    """Whether a trusted terminal generation stop still holds."""

    repo_lower = str(repository or "").lower()
    stops = []
    recovers = []
    for comment in comments or ():
        if not _is_trusted_stop(comment, repository_owner):
            continue
        body = _body(comment)
        stop = GENERATION_STOP_RE.search(body)
        if (
            stop
            and stop.group(1).lower() == repo_lower
            and int(stop.group(2)) == int(target)
            and int(stop.group(3)) == int(generation)
        ):
            stops.append(_order_key(comment))
            continue
        recover = GENERATION_RECOVER_RE.search(body)
        if (
            recover
            and recover.group(1).lower() == repo_lower
            and int(recover.group(2)) == int(target)
            and int(recover.group(3)) == int(generation)
        ):
            recovers.append(_order_key(comment))
    if not stops:
        return False
    return not recovers or max(recovers) < max(stops)


def _labels(value: Any) -> Set[str]:
    names: Set[str] = set()
    for label in value or ():
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, Mapping) and isinstance(label.get("name"), str):
            names.add(label["name"])
    return names


def _number(value: Any) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _pr_head_ref(pr: Mapping[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, Mapping) and head.get("ref"):
        return str(head.get("ref"))
    return str(pr.get("head_ref") or pr.get("headRefName") or "")


def _pr_head_repo(pr: Mapping[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, Mapping):
        repository = head.get("repo")
        if isinstance(repository, Mapping) and repository.get("full_name"):
            return str(repository.get("full_name"))
    return str(pr.get("head_repo") or "")


def _pr_head_sha(pr: Mapping[str, Any]) -> str:
    head = pr.get("head")
    if isinstance(head, Mapping) and head.get("sha"):
        return str(head.get("sha")).strip().lower()
    return str(pr.get("head_sha") or "").strip().lower()


def _pr_base_ref(pr: Mapping[str, Any]) -> str:
    base = pr.get("base")
    if isinstance(base, Mapping) and base.get("ref"):
        return str(base.get("ref"))
    return str(pr.get("base_ref") or pr.get("baseRefName") or "")


def _pr_merged(pr: Mapping[str, Any]) -> bool:
    return bool(pr.get("merged") or pr.get("merged_at"))


def _pr_author(pr: Mapping[str, Any]) -> str:
    user = pr.get("user")
    if isinstance(user, Mapping):
        return str(user.get("login") or "").lower()
    return str(pr.get("author_login") or "").lower()


def _pr_body(pr: Mapping[str, Any]) -> str:
    return str(pr.get("body") or "")


def _pr_snapshot_binding(
    pr: Mapping[str, Any],
    *,
    predecessor: int,
    repository: str,
) -> Any:
    """Return the PR's task-snapshot digests, or None/False for no/conflict.

    ``None`` means the PR body carries no usable marker binding: either no
    task-context/snapshot-reference markers at all, or only a matching legacy
    task-context marker that canonical ``opencode/issue<N>-`` branch binding
    can trust. ``False`` means marker-shaped content is present but malformed,
    duplicated, or targets another issue. Otherwise the tuple is the exact
    ``(title, body, spec)`` digest binding from the PR body.
    """

    body = _pr_body(pr)
    contexts = TASK_CONTEXT_RE.findall(body)
    refs = PR_SNAPSHOT_REF_RE.findall(body)
    if not contexts and not refs:
        return None
    if len(contexts) > 1 or len(refs) > 1:
        return False
    if contexts:
        context_repo, context_issue = contexts[0]
        if str(context_repo).lower() != str(repository or "").lower():
            return False
        if _number(context_issue) != int(predecessor):
            return False
    if not refs:
        return None
    ref_repo, ref_issue, generation, title, body_digest, spec = refs[0]
    if str(ref_repo).lower() != str(repository or "").lower():
        return False
    if _number(ref_issue) != int(predecessor):
        return False
    if _number(generation) != 1:
        return False
    return (title.lower(), body_digest.lower(), spec.lower())


def _durable_snapshot_digests(
    comments: Sequence[Any],
    *,
    predecessor: int,
    repository: str,
    owner_login: str,
) -> Optional[Tuple[str, str, str]]:
    selection = task_snapshot.select_snapshot(
        [
            comment if isinstance(comment, Mapping) else _comment_mapping(comment)
            for comment in comments or ()
        ],
        repo=repository,
        issue=predecessor,
        generation=1,
        owner_login=owner_login,
    )
    if selection.status != "found" or selection.snapshot is None:
        return None
    pinned = selection.snapshot
    return (
        str(pinned.title_sha256).lower(),
        str(pinned.body_sha256).lower(),
        str(pinned.spec_sha256).lower(),
    )


def _comment_mapping(comment: Any) -> Mapping[str, Any]:
    return {
        "body": _body(comment),
        "user": {"login": _login(comment)},
        "author_association": str(
            getattr(comment, "author_association", "") or ""
        ),
        "created_at": _created_at(comment),
        "id": _comment_id(comment),
    }


def bound_implementation_prs(
    pull_requests: Sequence[Mapping[str, Any]],
    *,
    predecessor: int,
    repository: str,
    predecessor_comments: Optional[Sequence[Any]] = None,
    owner_login: str = "",
) -> list:
    """Return implementation PRs bound to the predecessor.

    A trusted binding is either the canonical ``opencode/issue<N>-`` branch
    identity or an exact task-context plus task-snapshot reference in the PR
    body. A canonical branch PR may also carry only a matching legacy
    task-context marker. Any explicit snapshot reference is accepted only when
    it matches the durable task-snapshot comment on the predecessor issue, so
    a mutable PR body alone can never retarget the authorization.
    """

    prefix = "opencode/issue{}-".format(int(predecessor))
    bound = []
    for pr in pull_requests or ():
        if not isinstance(pr, Mapping):
            continue
        if repository and _pr_head_repo(pr).lower() != str(repository).lower():
            continue
        branch_bound = _pr_head_ref(pr).startswith(prefix)
        snapshot_binding = _pr_snapshot_binding(
            pr, predecessor=predecessor, repository=repository
        )
        if snapshot_binding is False:
            continue
        snapshot_bound = snapshot_binding is not None
        if not branch_bound and not snapshot_bound:
            continue
        if snapshot_bound:
            durable = _durable_snapshot_digests(
                predecessor_comments or (),
                predecessor=predecessor,
                repository=repository,
                owner_login=owner_login,
            )
            if durable is None or durable != snapshot_binding:
                continue
        if _number(pr.get("number") or pr.get("pull_number")):
            bound.append(pr)
    bound.sort(key=lambda pr: _number(pr.get("number") or pr.get("pull_number")))
    return bound


def _workflow_run_name(run: Mapping[str, Any]) -> str:
    return str(run.get("name") or run.get("workflow_name") or "")


def _workflow_run_path(run: Mapping[str, Any]) -> str:
    return str(run.get("path") or "")


def newest_exact_head_run(
    runs: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    workflow_names: Sequence[str],
    workflow_paths: Sequence[str],
) -> Optional[Mapping[str, Any]]:
    """Newest run for one exact HEAD and one required workflow identity."""

    head = str(head_sha or "").lower()
    names = {str(name) for name in workflow_names if name}
    paths = {str(path) for path in workflow_paths if path}
    matching = []
    for run in runs or ():
        if not isinstance(run, Mapping):
            continue
        if str(run.get("head_sha") or "").lower() != head:
            continue
        if _workflow_run_name(run) not in names and _workflow_run_path(run) not in paths:
            continue
        matching.append(run)
    if not matching:
        return None
    matching.sort(
        key=lambda run: (
            str(run.get("run_started_at") or run.get("created_at") or ""),
            _number(run.get("id") or run.get("databaseId")),
        )
    )
    return matching[-1]


def independent_review_evidence(
    reviews: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    pr_author: str,
) -> bool:
    """Exact-HEAD independent review proof from trusted producer evidence."""

    head = str(head_sha or "").lower()
    for status in statuses or ():
        if not isinstance(status, Mapping):
            continue
        if (
            str(status.get("context") or "") == "continuum/pr-agent-review"
            and str(status.get("state") or "").lower() == "success"
            and str(status.get("description") or "").strip() == CLEAN_PR_AGENT_REVIEW
        ):
            return True
    author = str(pr_author or "").lower()
    for review in reviews or ():
        if not isinstance(review, Mapping):
            continue
        if str(review.get("state") or "").upper() != "APPROVED":
            continue
        if str(review.get("commit_id") or "").lower() != head:
            continue
        user = review.get("user")
        login = str(
            user.get("login") if isinstance(user, Mapping) else review.get("author_login")
        ).lower()
        if not login or login == author or login.endswith("[bot]"):
            continue
        if str(review.get("author_association") or "").upper() == "BOT":
            continue
        return True
    return False


def evaluate_predecessor_completion(
    *,
    predecessor_issue: Mapping[str, Any],
    pull_requests: Sequence[Mapping[str, Any]],
    exact_head_runs: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    repository: str,
    default_branch: str = "main",
    ci_workflow_names: Sequence[str] = ("CI",),
    ci_workflow_paths: Sequence[str] = (".github/workflows/ci.yml",),
    contract_gate_names: Sequence[str] = ("Continuum Contract Gate",),
    contract_gate_paths: Sequence[str] = (
        ".github/workflows/contract-gate.yml",
    ),
    predecessor_comments: Optional[Sequence[Any]] = None,
    owner_login: str = "",
) -> Tuple[bool, str, Optional[int], str]:
    """Evaluate the complete trusted completion chain for a predecessor."""

    if predecessor_issue.get("pull_request"):
        return False, "predecessor is a pull request, not an issue", None, ""
    number = _number(predecessor_issue.get("number"))
    if not number:
        return False, "predecessor issue identity is invalid", None, ""
    state = str(predecessor_issue.get("state") or "").lower()
    reason = str(
        predecessor_issue.get("state_reason")
        or predecessor_issue.get("stateReason")
        or ""
    ).lower()
    if state == "open":
        return False, "predecessor issue is still open", None, ""
    if state != "closed":
        return False, "predecessor issue is not closed", None, ""
    if reason != "completed":
        return False, "predecessor issue is not closed as completed", None, ""

    bound = bound_implementation_prs(
        pull_requests,
        predecessor=number,
        repository=repository,
        predecessor_comments=predecessor_comments,
        owner_login=owner_login,
    )
    if not bound:
        return False, "predecessor has no trusted implementation PR binding", None, ""
    latest = bound[-1]
    if not _pr_merged(latest) or str(latest.get("state") or "").lower() != "closed":
        return False, "latest predecessor implementation PR is not merged", None, ""
    if _pr_base_ref(latest) != str(default_branch or "main"):
        return False, "predecessor PR base is not the expected default branch", None, ""
    if _pr_head_repo(latest).lower() != str(repository or "").lower():
        return False, "predecessor PR head repository is not trusted", None, ""
    head_sha = _pr_head_sha(latest)
    if not FULL_SHA_RE.match(head_sha):
        return False, "predecessor PR HEAD is missing or not a full SHA", None, ""
    merge_sha = str(latest.get("merge_commit_sha") or "").strip().lower()
    if not FULL_SHA_RE.match(merge_sha):
        return False, "predecessor merge commit is not a full SHA", None, ""
    if not str(latest.get("merged_at") or "").strip():
        return False, "predecessor merge timestamp is missing", None, ""

    for names, paths, label in (
        (ci_workflow_names, ci_workflow_paths, "CI"),
        (contract_gate_names, contract_gate_paths, "Contract Gate"),
    ):
        run = newest_exact_head_run(
            exact_head_runs,
            head_sha=head_sha,
            workflow_names=names,
            workflow_paths=paths,
        )
        if run is None:
            return False, "{} evidence is missing on the PR HEAD".format(label), None, ""
        if (
            str(run.get("status") or "").lower() != "completed"
            or str(run.get("conclusion") or "").lower() != "success"
        ):
            return False, "{} is not successful on the PR HEAD".format(label), None, ""

    if not independent_review_evidence(
        reviews,
        statuses,
        head_sha=head_sha,
        pr_author=_pr_author(latest),
    ):
        return False, "independent clean review evidence is missing", None, ""

    return True, "predecessor completion is trusted", _number(latest.get("number")), head_sha


def decide_owner_resume(
    *,
    target_issue: Mapping[str, Any],
    repository_owner: str,
    repository: str,
    comments: Sequence[Any],
    labels: Any,
    open_pr_issue_numbers: Sequence[int] = (),
    active_run_issue_numbers: Sequence[int] = (),
    predecessor_issue: Optional[Mapping[str, Any]] = None,
    predecessor_comments: Optional[Sequence[Any]] = None,
    pull_requests: Sequence[Mapping[str, Any]] = (),
    exact_head_runs: Sequence[Mapping[str, Any]] = (),
    statuses: Sequence[Mapping[str, Any]] = (),
    reviews: Sequence[Mapping[str, Any]] = (),
    default_branch: str = "main",
    pause_label: str = "automation:paused",
    ready_label: str = "automation:ready",
    blocked_label: str = "automation:blocked",
    no_auto_merge_label: str = "no-auto-merge",
    dispatch_marker: str = DISPATCH_MARKER,
    api_error: Optional[str] = None,
) -> ResumeDecision:
    """Decide the narrow armed-target transition from trusted state."""

    if api_error:
        return ResumeDecision(NOOP, "GitHub API failure: failing closed")
    target = _number(target_issue.get("number"))
    if not target:
        return ResumeDecision(NOOP, "target issue identity is invalid")
    if target_issue.get("pull_request"):
        return ResumeDecision(NOOP, "target is a pull request")
    if str(target_issue.get("state") or "open").lower() != "open":
        return ResumeDecision(NOOP, "target issue is not open")

    label_set = _labels(labels)
    if not ready_label or ready_label not in label_set or pause_label not in label_set:
        return ResumeDecision(NOOP, "target is not paused and admitted")
    if no_auto_merge_label in label_set:
        return ResumeDecision(NOOP, "target carries no-auto-merge")
    if blocked_label in label_set:
        return ResumeDecision(NOOP, "target is qualification-blocked")

    scan = scan_resume_marker(
        comments, target=target, repository_owner=repository_owner
    )
    if scan.instruction is None:
        return ResumeDecision(NOOP, scan.reason)
    instruction = scan.instruction

    if target in {int(number) for number in open_pr_issue_numbers}:
        return ResumeDecision(NOOP, "target already has an open implementation PR")
    if target in {int(number) for number in active_run_issue_numbers}:
        return ResumeDecision(NOOP, "target already has an active implementation run")

    if generation_stopped(
        comments,
        target=target,
        repository=repository,
        repository_owner=repository_owner,
    ):
        return ResumeDecision(NOOP, "target generation has an owner STOP")

    if _has_activity_after(comments, instruction, repository_owner, dispatch_marker):
        return ResumeDecision(
            NOOP,
            "a post-marker run or recovery attempt already owns this target",
        )

    if predecessor_issue is None:
        return ResumeDecision(NOOP, "predecessor state is unavailable")
    if predecessor_issue.get("pull_request"):
        return ResumeDecision(NOOP, "predecessor is a pull request")
    if _number(predecessor_issue.get("number")) != instruction.predecessor:
        return ResumeDecision(
            NOOP, "predecessor identity does not match the owner marker"
        )
    if str(predecessor_issue.get("state") or "").lower() == "open":
        return ResumeDecision(
            WAIT,
            "predecessor issue #{} is still open".format(instruction.predecessor),
            instruction,
        )

    complete, reason, pr_number, head_sha = evaluate_predecessor_completion(
        predecessor_issue=predecessor_issue,
        pull_requests=pull_requests,
        exact_head_runs=exact_head_runs,
        statuses=statuses,
        reviews=reviews,
        repository=repository,
        default_branch=default_branch,
        predecessor_comments=predecessor_comments,
        owner_login=repository_owner,
    )
    if not complete:
        return ResumeDecision(NOOP, reason, instruction)

    return ResumeDecision(
        RESUME,
        "predecessor #{} is complete; resuming armed target".format(
            instruction.predecessor
        ),
        instruction,
        predecessor_pr=pr_number,
        predecessor_head=head_sha,
    )


__all__ = [
    "NOOP",
    "RESUME",
    "WAIT",
    "MarkerScan",
    "ResumeDecision",
    "ResumeInstruction",
    "bound_implementation_prs",
    "decide_owner_resume",
    "evaluate_predecessor_completion",
    "generation_stopped",
    "independent_review_evidence",
    "newest_exact_head_run",
    "resume_baseline",
    "scan_resume_marker",
]
