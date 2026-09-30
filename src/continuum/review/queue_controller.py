"""The review queue controller: the GitHub side of the reconciliation contract.

The controller does four things, in this order, and nothing else:

1. read current state (open pull requests, their labels, CI, and the provider
   responses Continuum recorded);
2. hand that state to the pure contract in `queue.reconcile`;
3. revalidate the selected candidate against live PR state, so a command is
   never sent to a pull request that closed, merged, turned to draft, or was
   paused between the read and the write;
4. send exactly one provider request, recording the in-flight slot first so a
   duplicate or replayed wake-up cannot spend the same shared slot twice.

There is no remembered queue here. Every run recomputes the whole queue, which
is what makes the controller self-healing: a candidate that disappears simply
stops being eligible, and the next candidate becomes actionable in the same
cycle.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import ContinuumConfig
from . import providers as registry
from . import queue
from .findings import parse_time
from .github import GitHubError

# The in-flight slot is a comment, not controller memory: a comment is visible
# to every consumer, survives a controller restart, and is keyed to the exact
# candidate identity (PR plus HEAD) the provider was asked about.
REQUEST_MARKER = "<!-- continuum-review-request -->"
REQUEST_SCHEMA = "continuum.review-request/v1"

_SOURCE_BRANCH_RE = re.compile(r"^opencode/issue(\d+)-")
_SOURCE_BODY_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)

_CI_FAILURE = ("failure", "timed_out", "action_required", "startup_failure")
_CI_PENDING = ("queued", "in_progress", "waiting", "requested", "pending")


class QueueError(RuntimeError):
    """Raised when the queue cannot be reconciled safely."""


def now_ms() -> int:
    return int(time.time() * 1000)


def _epoch_ms(value: Any) -> int:
    parsed = parse_time(value)
    return int(parsed.timestamp() * 1000) if parsed is not None else 0


def _labels_of(payload: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(label.get("name") if isinstance(label, dict) else label).strip().lower()
                for label in (payload.get("labels") or [])
            }
        )
    )


def source_issue_number(pull: Dict[str, Any]) -> Optional[int]:
    """The issue a pull request implements, from its branch or its body."""

    match = _SOURCE_BRANCH_RE.match(str(((pull.get("head") or {}).get("ref")) or ""))
    if match:
        return int(match.group(1))
    body_match = _SOURCE_BODY_RE.search(str(pull.get("body") or ""))
    return int(body_match.group(1)) if body_match else None


def render_request_record(
    *,
    pr_number: int,
    head_sha: str,
    provider: str,
    kind: str,
    requested_at_ms: int,
) -> str:
    """The comment that reserves the shared provider slot for one candidate."""

    payload = {
        "schema": REQUEST_SCHEMA,
        "pr": int(pr_number),
        "head": str(head_sha),
        "provider": str(provider),
        "kind": str(kind),
        "requested_at_ms": int(requested_at_ms),
    }
    return "\n".join(
        [
            REQUEST_MARKER,
            f"Continuum reserved the shared {provider} review slot for PR #{pr_number} "
            f"at `{head_sha}`.",
            "",
            "```json",
            json.dumps(payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def parse_request_record(body: str) -> Optional[Dict[str, Any]]:
    """The request record carried by a comment, or `None`."""

    if REQUEST_MARKER not in (body or ""):
        return None
    start = (body or "").find("```json")
    if start < 0:
        return None
    end = (body or "").find("```", start + 7)
    if end < 0:
        return None
    try:
        payload = json.loads(body[start + 7 : end])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != REQUEST_SCHEMA:
        return None
    try:
        return {
            "pr": int(payload.get("pr")),
            "head": str(payload.get("head") or ""),
            "provider": str(payload.get("provider") or ""),
            "kind": str(payload.get("kind") or queue.REVIEW_UNREVIEWED),
            "requested_at_ms": int(payload.get("requested_at_ms") or 0),
        }
    except (TypeError, ValueError):
        return None


def _latest_request_record(
    comments: Sequence[Dict[str, Any]], pr_number: int
) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for comment in comments or []:
        record = parse_request_record(comment.get("body") or "")
        if record is None or record["pr"] != int(pr_number):
            continue
        if best is None or record["requested_at_ms"] >= best["requested_at_ms"]:
            best = record
    return best


def _is_provider_identity(login: Any, logins: Sequence[str]) -> bool:
    text = str(login or "").lower()
    if not text:
        return False
    for known in logins:
        candidate = str(known).lower()
        if text == candidate or text.startswith(candidate.split("[", 1)[0]):
            return True
    return False


def provider_responded_after(
    *,
    comments: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
    statuses: Sequence[Dict[str, Any]],
    since_ms: int,
    logins: Sequence[str],
    status_context: str,
) -> Optional[Tuple[str, int]]:
    """The provider's answer to a request as `(reason, at_ms)`, else `None`.

    Any provider-authored output newer than the request settles the slot: a
    rate-limit notice, a submitted review, or the provider's own status. The
    controller never assumes silence means failure, and never waits for a
    timeout before a *live* candidate's slot is reused.
    """

    for review in reviews or []:
        submitted = _epoch_ms(review.get("submitted_at"))
        if submitted <= since_ms:
            continue
        if _is_provider_identity((review.get("user") or {}).get("login"), logins):
            return f"review {review.get('state') or 'submitted'}", submitted
    for status in statuses or []:
        updated = _epoch_ms(status.get("updated_at") or status.get("created_at"))
        if updated <= since_ms:
            continue
        if str(status.get("context") or "").lower() == str(status_context).lower():
            return f"status {status.get('state') or 'updated'}", updated
    for comment in comments or []:
        if REQUEST_MARKER in (comment.get("body") or ""):
            continue
        updated = _epoch_ms(comment.get("updated_at") or comment.get("created_at"))
        if updated <= since_ms:
            continue
        if _is_provider_identity((comment.get("user") or {}).get("login"), logins):
            return "provider comment", updated
    return None


def aggregate_ci(
    check_runs: Sequence[Dict[str, Any]], required: Sequence[str] = ()
) -> str:
    """One state for the HEAD's CI: the worst of the checks that matter."""

    runs = list(check_runs or [])
    if required:
        wanted = {str(name).lower() for name in required}
        runs = [run for run in runs if str(run.get("name") or "").lower() in wanted]
        if not runs:
            return queue.CI_PENDING
    if not runs:
        return queue.CI_UNKNOWN

    latest: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name") or "")
        started = _epoch_ms(run.get("started_at") or run.get("completed_at"))
        current = latest.get(name)
        if current is None or started >= _epoch_ms(
            current.get("started_at") or current.get("completed_at")
        ):
            latest[name] = run

    states = []
    for run in latest.values():
        if run.get("status") != "completed":
            states.append(queue.CI_PENDING)
        elif run.get("conclusion") in _CI_FAILURE:
            states.append(queue.CI_FAILURE)
        elif run.get("conclusion") == "cancelled":
            states.append(queue.CI_CANCELLED)
        else:
            # `success` and `skipped` (a path-filtered job) both leave the diff
            # verified as far as this check is concerned.
            states.append(queue.CI_SUCCESS)

    for state in (queue.CI_FAILURE, queue.CI_CANCELLED, queue.CI_PENDING):
        if state in states:
            return state
    if states and all(state == queue.CI_SUCCESS for state in states):
        return queue.CI_SUCCESS
    return queue.CI_UNKNOWN


def _priority_of(
    client: Any,
    pr_labels: Sequence[str],
    source_issue: Optional[int],
    policy: queue.QueuePolicy,
) -> str:
    """Priority label on the pull request, or on the issue it implements."""

    for label in pr_labels:
        if label.lower() in {name.lower() for name in policy.priority_labels}:
            return label.lower()
    if source_issue is None:
        return queue.UNPRIORITIZED
    try:
        issue = client.get_issue(int(source_issue))
    except GitHubError:
        return queue.UNPRIORITIZED
    for label in _labels_of(issue):
        if label in {name.lower() for name in policy.priority_labels}:
            return label
    return queue.UNPRIORITIZED


def _observe(
    client: Any,
    config: ContinuumConfig,
    policy: queue.QueuePolicy,
    pull: Dict[str, Any],
    *,
    now_ms: int,
    logins: Sequence[str],
    status_context: str,
    settings: Any,
    statuses: Optional[Sequence[Dict[str, Any]]] = None,
) -> queue.Candidate:
    """Read one pull request into the contract's candidate shape."""

    number = int(pull.get("number") or 0)
    head = str(((pull.get("head") or {}).get("sha")) or "")
    labels = _labels_of(pull)
    source_issue = source_issue_number(pull)
    merged = bool(pull.get("merged_at")) or bool(pull.get("merged"))
    if merged:
        state = queue.STATE_MERGED
    else:
        state = str(pull.get("state") or queue.STATE_OPEN)

    comments = client.list_issue_comments(number)
    reviews = client.list_reviews(number)
    if statuses is None:
        statuses = _statuses_for(client, head)

    try:
        check_runs = client.list_check_runs(head) if head else []
    except GitHubError:
        # Unknown CI state is never green: an unreadable CI result must not make
        # a candidate actionable.
        check_runs = []
    ci = aggregate_ci(check_runs, policy.required_checks)

    record = _latest_request_record(comments, number)
    request: Optional[queue.ReviewRequest] = None
    settled_reason = ""
    settled_at_ms = 0
    if record is not None:
        if record["head"] != head:
            settled_reason = "the candidate moved to a new HEAD"
            settled_at_ms = record["requested_at_ms"]
        else:
            response = provider_responded_after(
                comments=comments,
                reviews=reviews,
                statuses=statuses,
                since_ms=record["requested_at_ms"],
                logins=logins,
                status_context=status_context,
            )
            if response is not None:
                settled_reason, settled_at_ms = response
        request = queue.ReviewRequest(
            pr_number=number,
            head_sha=record["head"],
            requested_at_ms=record["requested_at_ms"],
            provider=record["provider"],
            kind=record["kind"],
            settled=bool(settled_reason),
            settled_reason=settled_reason,
            settled_at_ms=settled_at_ms,
        )

    rate_limit_until, _reason = registry.rate_limit_cooldown(
        config.review.provider, client, settings, pr_number=number
    )

    review_need = queue.REVIEW_UNREVIEWED
    due_at_ms = 0
    if request is not None and not request.settled:
        # The slot is occupied; `reconcile` decides what that means.
        review_need = request.kind or queue.REVIEW_UNREVIEWED
    elif rate_limit_until > now_ms:
        review_need = queue.REVIEW_RETRY
        due_at_ms = rate_limit_until
    elif registry.provider_covers_head(
        config.review.provider,
        settings,
        reviews=reviews,
        statuses=statuses,
        head=head,
    ):
        review_need = queue.REVIEW_CURRENT

    return queue.Candidate(
        pr_number=number,
        head_sha=head,
        state=state,
        draft=bool(pull.get("draft")),
        labels=labels,
        priority=_priority_of(client, labels, source_issue, policy),
        source_issue=source_issue,
        ci=ci,
        review=review_need,
        due_at_ms=due_at_ms,
        created_at_ms=_epoch_ms(pull.get("created_at")),
        request=request,
    )


def _statuses_for(client: Any, head: str) -> List[Dict[str, Any]]:
    if not head:
        return []
    try:
        payload = client.combined_status_for_ref(head)
    except GitHubError:
        return []
    return list(payload.get("statuses") or [])


def _cooldown_from_responses(
    state_candidates: Sequence[queue.Candidate], *, window_ms: int
) -> queue.Cooldown:
    """The shared slot is quiet for `window_ms` after the provider consumes one.

    The window is global: a completed review anywhere in the repository moves
    the shared cooldown for every other candidate, which is what keeps a
    high-priority PR from spending quota that was already consumed.
    """

    latest = 0
    for candidate in state_candidates:
        if candidate.request is not None and candidate.request.settled:
            latest = max(latest, candidate.request.settled_at_ms)
    if not latest:
        return queue.Cooldown()
    return queue.Cooldown(
        until_ms=latest + window_ms,
        reason="configured provider cooldown after the last review",
    )


def _is_internal(client: Any, pull: Dict[str, Any]) -> bool:
    """A fork's HEAD is not reviewable by a repository-scoped controller."""

    head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
    if not head_repo:
        return True
    return str(head_repo) == str(getattr(client, "repository", "") or "")


def collect_state(
    client: Any,
    config: ContinuumConfig,
    *,
    wake: queue.WakeUp,
    now_ms_value: Optional[int] = None,
) -> queue.QueueState:
    """Recompute the whole queue from current GitHub state."""

    now = int(now_ms_value if now_ms_value is not None else now_ms())
    policy = queue.policy_from_config(config)
    if not config.review.enabled:
        return queue.QueueState(wake=wake, now_ms=now)

    settings = config.review.provider_settings()
    logins = registry.provider_logins(config)
    status_context = registry.provider_status_context(config)

    observed: List[queue.Candidate] = []
    open_numbers = set()
    try:
        pulls = client.list_pulls(state="open")
    except GitHubError as exc:
        raise QueueError(f"Could not list open pull requests: {exc}") from None

    for pull in sorted(pulls, key=lambda item: int(item.get("number") or 0))[
        : policy.max_candidates
    ]:
        if not _is_internal(client, pull):
            # A fork's HEAD must never occupy the shared provider slot.
            continue
        number = int(pull.get("number") or 0)
        open_numbers.add(number)
        observed.append(
            _observe(
                client,
                config,
                policy,
                pull,
                now_ms=now,
                logins=logins,
                status_context=status_context,
                settings=settings,
            )
        )

    # The pull request a wake-up names is read even when it is no longer open,
    # so "PR #58 left queue: state=closed" is reported from state rather than
    # from the event payload.
    if wake.pr_number and wake.pr_number not in open_numbers:
        try:
            closed = client.get_pull(int(wake.pr_number))
        except GitHubError:
            closed = None
        if isinstance(closed, dict) and closed:
            observed.append(
                _observe(
                    client,
                    config,
                    policy,
                    closed,
                    now_ms=now,
                    logins=logins,
                    status_context=status_context,
                    settings=settings,
                )
            )

    cooldown = _cooldown_from_responses(observed, window_ms=policy.cooldown_window_ms())
    # A provider-declared retry window is authoritative for its own candidate
    # and for the shared slot it belongs to.
    until = max([cooldown.until_ms, *(candidate.due_at_ms for candidate in observed)])
    if until > cooldown.until_ms:
        cooldown = queue.Cooldown(until_ms=until, reason="provider rate limit")

    return queue.QueueState(
        wake=wake, candidates=tuple(observed), cooldown=cooldown, now_ms=now
    )


def _dispatch(
    client: Any,
    config: ContinuumConfig,
    policy: queue.QueuePolicy,
    plan: queue.Plan,
    *,
    now: int,
) -> queue.Plan:
    """Send exactly one provider request for the selected candidate."""

    selected = plan.selected
    if selected is None:  # pragma: no cover - Plan.dispatched guarantees this
        raise QueueError("Dispatch was requested without a selected candidate")
    logs = list(plan.logs)

    # Revalidate live state: the queue is recomputed from a read that can be a
    # few seconds old, and a closed, merged, draft, or paused pull request must
    # never receive a provider command.
    try:
        live = client.get_pull(selected.pr_number)
    except GitHubError as exc:
        raise QueueError(f"Could not revalidate pull request #{selected.pr_number}: {exc}") from None

    fresh = _observe(
        client,
        config,
        policy,
        live,
        now_ms=now,
        logins=registry.provider_logins(config),
        status_context=registry.provider_status_context(config),
        settings=config.review.provider_settings(),
    )
    reason = queue.ineligibility_reason(fresh, policy)
    if reason is not None or fresh.head_sha != selected.head_sha:
        detail = reason or f"head moved to {fresh.head_sha[:7] or 'unknown'}"
        logs.append(
            f"PR #{selected.pr_number} left queue: {detail}."
        )
        logs.append("No provider request was sent; the next candidate takes the slot.")
        return queue.Plan(
            action=queue.ACTION_IDLE,
            wake=plan.wake,
            now_ms=plan.now_ms,
            selected=None,
            queue=plan.queue,
            excluded=plan.excluded + ((selected.pr_number, detail),),
            released_lock=plan.released_lock,
            release_reason=plan.release_reason,
            cooldown=plan.cooldown,
            logs=tuple(logs),
        )

    request = registry.queue_request(
        config.review.provider,
        config.review.provider_settings(),
        pr_number=selected.pr_number,
        head_sha=selected.head_sha,
        kind=selected.review,
        dispatch_workflow=policy.dispatch_workflow,
    )

    # The slot is recorded before the command exists, so a crash or a
    # concurrent wake-up can never spend the same shared slot twice.
    record = client.create_issue_comment(
        selected.pr_number,
        render_request_record(
            pr_number=selected.pr_number,
            head_sha=selected.head_sha,
            provider=config.review.provider,
            kind=selected.review,
            requested_at_ms=now,
        ),
    )
    try:
        if request.kind == registry.REQUEST_COMMENT:
            client.create_issue_comment(selected.pr_number, request.body)
        elif request.kind == registry.REQUEST_WORKFLOW_DISPATCH:
            client.dispatch_workflow(request.workflow, "main", request.inputs)
        else:  # pragma: no cover - defensive: providers only return the kinds above
            raise QueueError(
                f"Provider {config.review.provider!r} produced an unsupported request "
                f"kind {request.kind!r}"
            )
    except Exception:
        # Release the reservation so the queue is not blocked by a command that
        # was never delivered.
        try:
            client.delete_issue_comment(int((record or {}).get("id") or 0))
        except Exception:  # noqa: BLE001 - diagnostics must not mask the failure
            pass
        raise

    logs.append(
        f"Dispatched one {config.review.provider} review request for PR "
        f"#{selected.pr_number} at head {selected.head_sha[:7]} ({selected.priority})."
    )
    return queue.Plan(
        action=queue.ACTION_DISPATCH,
        wake=plan.wake,
        now_ms=plan.now_ms,
        selected=selected,
        queue=plan.queue,
        excluded=plan.excluded,
        held_lock=None,
        released_lock=plan.released_lock,
        release_reason=plan.release_reason,
        cooldown=plan.cooldown,
        logs=tuple(logs),
    )


def reconcile_once(
    client: Any,
    config: ContinuumConfig,
    *,
    wake: queue.WakeUp,
    now: Optional[int] = None,
    apply: bool = True,
) -> queue.Plan:
    """One full pass: collect, decide, and at most one provider request."""

    moment = int(now if now is not None else now_ms())
    policy = queue.policy_from_config(config)
    state = collect_state(client, config, wake=wake, now_ms_value=moment)
    plan = queue.reconcile(state, policy)
    if not plan.dispatched or not apply:
        return plan
    return _dispatch(client, config, policy, plan, now=moment)


def reconcile(
    client: Any,
    config: ContinuumConfig,
    *,
    wake: queue.WakeUp,
    now: Optional[int] = None,
    apply: bool = True,
    wait_ms: int = 0,
    max_passes: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], int] = now_ms,
) -> queue.Plan:
    """Reconcile, and optionally keep waiting for a cooldown inside one run.

    Waiting is a convenience for a queue that is only held back by a provider
    cooldown. It never substitutes for event coverage: every state change that
    can change the next candidate already wakes the controller on its own, and
    each wake-up reconciles from live state when its run starts.

    A sleeping run is deliberately *not* cancelled by a later wake-up -- see the
    `concurrency` block in `.github/workflows/review-queue.yml` for why, since
    cancellation here turns a cooldown wait into a livelock.
    """

    plan = reconcile_once(client, config, wake=wake, now=now, apply=apply)
    budget = int(wait_ms)
    for _pass in range(max(1, int(max_passes)) - 1):
        if plan.action != queue.ACTION_WAIT or budget <= 0 or not apply:
            break
        remaining = plan.cooldown.remaining_ms(plan.now_ms)
        if remaining <= 0:
            break
        pause = min(remaining, budget)
        sleep(pause / 1000)
        budget -= pause
        plan = reconcile_once(client, config, wake=wake, now=clock(), apply=apply)
    return plan
