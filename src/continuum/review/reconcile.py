"""The review controller: the closed loop from review to merge.

`gate.run_gate` answers one question — is this pull request's current HEAD
mergeable? This module answers the next one — what should happen now that the
answer is known — and performs it.

The loop it closes is the one a human would otherwise have to drive:

    provider finding -> normalized findings on an exact HEAD
                      -> bounded agent repair on the existing branch
                      -> push -> CI
                      -> provider re-check of the original findings
                      -> merge on a green current-HEAD gate

Everything provider-shaped arrives through the adapter's `RepairPort`, and the
merge controller still sees nothing but the normalized status. Two properties
are load-bearing:

* **`review: false` is silent.** With review disabled the controller returns
  before reading or writing anything on the pull request, so a deliberate
  opt-out generates no provider traffic, no agent run, and no review status.
* **The budget is durable.** Every spend is recorded in a marker comment, so a
  re-dispatched run, a resumed job, or a second controller cannot reset the
  count and restart a loop the bound exists to stop.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import ContinuumConfig
from . import gate as gate_module
from . import providers as providers_module
from . import repair as repair_module
from .tracker import BOT_PREFIX

PLAN_SCHEMA = "continuum.review-reconcile/v1"

#: The dispatch mode a review repair runs under. Generic on purpose: the mode
#: reaches the trust policy allowlist and the agent workflow, and neither may
#: branch on which provider produced the findings.
DISPATCH_MODE = "review-fix"

#: Used when neither the caller nor the release configuration names a branch.
DEFAULT_AGENT_REF = "main"


class ReconcileError(RuntimeError):
    """Raised when the controller cannot reach a trustworthy decision."""


def disabled_plan(
    config: ContinuumConfig,
    pr_number: int,
    head: str = "",
) -> Dict[str, Any]:
    """The `review: false` plan: a free pass that touches nothing."""

    result = gate_module.disabled_result(config, int(pr_number), head)
    repair_plan = repair_module.plan(
        result,
        None,
        None,
        enabled=False,
    )
    return {
        "schema": PLAN_SCHEMA,
        "enabled": False,
        "pr": int(pr_number),
        "head": head,
        "gate": result,
        "repair": repair_plan.describe(),
        "dispatched": False,
        "requests_sent": 0,
        "reason": repair_plan.reason,
    }


def _plan_requests(
    client: Any,
    pr_number: int,
    requests: Sequence[repair_module.RepairRequest],
    *,
    apply: bool,
) -> List[Dict[str, Any]]:
    """Describe this pass's requests, sending them only when applying.

    The plan is always returned, so a dry run reports exactly what a live run
    would have asked the provider without asking it.
    """

    entries: List[Dict[str, Any]] = []
    for request in requests:
        entry = request.describe()
        entry["sent"] = False
        if apply:
            client.create_issue_comment(int(pr_number), request.body)
            entry["sent"] = True
        entries.append(entry)
    return entries


def _ledger_changed(previous: Any, updated: repair_module.RepairLedger) -> bool:
    """Whether the spend moved, ignoring the bookkeeping timestamp."""

    before = (previous or repair_module.RepairLedger()).describe()
    after = updated.describe()
    before.pop("updated_at", None)
    after.pop("updated_at", None)
    before.pop("last_action", None)
    after.pop("last_action", None)
    return before != after


def reconcile(
    client: Any,
    config: ContinuumConfig,
    pr_number: int,
    *,
    head_sha: Optional[str] = None,
    apply: bool = True,
    policy: Optional[repair_module.RepairPolicy] = None,
    dispatch: Optional[Callable[..., Any]] = None,
    dispatch_workflow: str = "",
    dispatch_ref: str = "",
) -> Dict[str, Any]:
    """Evaluate the gate for one pull request and perform its next repair action.

    With `apply=False` nothing is written to GitHub, which is what the
    deterministic tests use to read the decision without side effects.

    ``dispatch`` is the caller's privileged workflow-dispatch entry point. It is
    invoked only when the plan calls for an agent repair, and it receives the ref
    the controller itself read from the live pull request, so no caller-supplied
    value can choose what gets checked out. The mode is the generic
    ``review-fix``: which provider produced the findings never reaches the
    dispatch, the trust policy, or the agent workflow.
    """

    if not config.review.enabled:
        # Returned before the client is touched: `review: false` must produce no
        # review traffic of any kind, and a status written "for free" would be
        # traffic the merge controller could come to depend on.
        return disabled_plan(config, int(pr_number), head_sha or "")

    provider = config.review.provider
    port = providers_module.repair_port(provider)
    limits = policy or repair_module.RepairPolicy()
    trusted = gate_module.trusted_logins(config)

    pull = client.get_pull(int(pr_number))
    head = (head_sha or ((pull.get("head") or {}).get("sha")) or "").strip()
    if not head:
        raise ReconcileError(f"Could not determine the current HEAD for pull request #{pr_number}")

    result = gate_module.run_gate(
        client, config, int(pr_number), head_sha=head, apply=apply
    )
    head = str(result.get("head") or head)
    head_ref = str((pull.get("head") or {}).get("ref") or "")

    comments = client.list_issue_comments(int(pr_number))
    ledger = repair_module.load_ledger(comments, trusted_logins=trusted)
    seen = repair_module.RequestedMarkers.from_comments(
        comments, port, trusted_logins=trusted
    )

    repair_plan = repair_module.plan(
        result,
        ledger,
        port,
        policy=limits,
        enabled=True,
        seen=seen,
    )

    performed = _plan_requests(
        client, int(pr_number), repair_plan.requests, apply=apply
    )

    updated = repair_module.record(ledger, repair_plan, at=repair_module.now_iso())
    if apply and _ledger_changed(ledger, updated):
        # One marker comment per pull request carries the ledger, so a resumed or
        # re-dispatched run reads the same budget the previous run spent.
        client.upsert_marker_comment(
            int(pr_number),
            repair_module.REPAIR_MARKER,
            repair_module.render_ledger(updated, BOT_PREFIX),
            trusted,
        )

    dispatched = False
    prompt = ""
    if repair_plan.needs_agent and apply:
        prompt = repair_module.render_prompt(
            repair_plan,
            pr_number=int(pr_number),
            head_ref=head_ref,
            collected_at=str(result.get("generated_at") or ""),
        )
        # The instruction is written to the pull request before the dispatch, so
        # the repair agent runs exactly what was decided here rather than
        # re-deriving findings from a second read of mutable state. The reader
        # refuses an instruction written for a different commit.
        client.upsert_marker_comment(
            int(pr_number),
            repair_module.PROMPT_MARKER,
            repair_module.render_agent_prompt(prompt, head),
            trusted,
        )
        if dispatch is not None and dispatch_workflow:
            dispatch(
                dispatch_workflow,
                dispatch_ref or DEFAULT_AGENT_REF,
                {
                    "mode": DISPATCH_MODE,
                    "pr_number": str(int(pr_number)),
                    "head_ref": head_ref,
                },
            )
            dispatched = True

    payload = {
        "schema": PLAN_SCHEMA,
        "enabled": True,
        "pr": int(pr_number),
        "head": head,
        "provider": provider,
        "gate_passed": bool(result.get("gate_passed")),
        "verdict": str(result.get("verdict") or ""),
        "open_findings": int(result.get("open_findings") or 0),
        "gate": result,
        "repair": repair_plan.describe(),
        "policy": limits.describe(),
        "ledger": updated.describe(),
        "already_requested": seen.describe(),
        "requests": performed,
        "requests_sent": sum(1 for entry in performed if entry.get("sent")),
        "needs_agent": repair_plan.needs_agent,
        "dispatched": dispatched,
        "agent_prompt": prompt,
        "agent_timeout_minutes": limits.agent_timeout_minutes if repair_plan.needs_agent else 0,
        "head_ref": head_ref,
        "reason": repair_plan.reason,
    }
    return payload


def agent_prompt(
    client: Any,
    pr_number: int,
    head_sha: str,
) -> str:
    """The stored repair instruction for one exact HEAD.

    Read by the privileged agent step after the trust policy has authorized it.
    Fails closed rather than inventing one: an instruction assembled against a
    different commit, or one no trusted identity wrote, is not a repair request.
    """

    trusted = gate_module.automation_logins()
    head = str(head_sha or "").strip()
    if not head:
        raise ReconcileError("No HEAD was supplied for the repair instruction.")

    allowed = {name.lower() for name in trusted}
    for comment in reversed(list(client.list_issue_comments(int(pr_number)))):
        body = comment.get("body") or ""
        if repair_module.PROMPT_MARKER not in body:
            continue
        login = ((comment.get("user") or {}).get("login") or "").lower()
        # Fail closed on an absent author as well as an unrecognised one. A
        # comment with no attributed identity is not evidence that the engine
        # wrote it, and this body is about to become a privileged instruction.
        if not login or login not in allowed:
            continue
        prompt = repair_module.parse_agent_prompt(body, head)
        if prompt:
            return prompt
        raise ReconcileError(
            f"Pull request #{pr_number} carries a review repair instruction for a "
            f"different commit than {head[:12]}; refusing to run it."
        )

    raise ReconcileError(
        f"Pull request #{pr_number} has no review repair instruction for {head[:12]}."
    )
