"""Review provider registry.

Adding a provider means adding a module that returns a `ProviderSnapshot`. The
gate, the normalized result, the merge controller, and the review queue are
unaware of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from .. import config as config_module
from . import coderabbit, pr_agent
from . import repair
from .snapshot import ProviderSnapshot

PR_AGENT = config_module.PROVIDER_PR_AGENT
CODERABBIT = config_module.PROVIDER_CODERABBIT
NONE = config_module.PROVIDER_NONE

# How a queued candidate is asked for a review. The queue schedules exactly one
# of these; the kind is provider specific, the decision to schedule it is not.
REQUEST_NONE = "none"
REQUEST_COMMENT = "comment"
REQUEST_WORKFLOW_DISPATCH = "workflow_dispatch"


class UnsupportedProvider(ValueError):
    """Raised when configuration names a provider Continuum cannot run."""


@dataclass(frozen=True)
class QueueRequest:
    """The external command that asks one provider to review one PR."""

    kind: str
    provider: str
    body: str = ""
    workflow: str = ""
    inputs: Dict[str, str] = field(default_factory=dict)
    lock_marker: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "body": self.body,
            "workflow": self.workflow,
            "inputs": dict(self.inputs),
        }


def supported_providers() -> tuple:
    return (PR_AGENT, CODERABBIT)


def get_adapter(name: str) -> Any:
    if name == PR_AGENT:
        return pr_agent
    if name == CODERABBIT:
        return coderabbit
    raise UnsupportedProvider(
        f"Unsupported review provider {name!r}; supported: {', '.join(supported_providers())}"
    )


def queue_request(
    name: str,
    settings: Any,
    *,
    pr_number: int,
    head_sha: str = "",
    kind: str = "initial",
    dispatch_workflow: str = config_module.DEFAULT_QUEUE_DISPATCH_WORKFLOW,
) -> QueueRequest:
    """Build the provider command for one already-selected candidate.

    A pull request that is closed, draft, or paused never reaches this function:
    selection happens first, and the controller revalidates live state again
    before the command is sent.
    """

    if name == CODERABBIT:
        return coderabbit.queue_request(
            settings,
            pr_number=pr_number,
            head_sha=head_sha,
            kind=kind,
        )
    if name == PR_AGENT:
        return pr_agent.queue_request(
            settings,
            pr_number=pr_number,
            head_sha=head_sha,
            kind=kind,
            dispatch_workflow=dispatch_workflow,
        )
    raise UnsupportedProvider(
        f"Unsupported review provider {name!r}; supported: {', '.join(supported_providers())}"
    )


def rate_limit_cooldown(
    name: str,
    client: Any,
    settings: Any,
    *,
    pr_number: int,
) -> Tuple[int, str]:
    """Provider-declared quiet period as an absolute `(until_ms, reason)`.

    Only a provider can extend the global cooldown beyond the configured window,
    by publishing a retry window. `(0, "")` means the provider declared nothing
    and the configured window alone applies.
    """

    if name == CODERABBIT:
        return coderabbit.rate_limit_cooldown(client, pr_number, bot_login=settings.bot_login)
    return 0, ""


def provider_status_context(config: Any) -> str:
    """The commit status context that reports provider output for a HEAD."""

    settings = config.review.provider_settings()
    context = getattr(settings, "status_context", None)
    return str(context or config.review.status_context)


def provider_logins(config: Any) -> Tuple[str, ...]:
    """Logins whose comments and reviews count as provider output."""

    settings = config.review.provider_settings()
    bot_login = getattr(settings, "bot_login", None) or "github-actions[bot]"
    return tuple(
        dict.fromkeys(
            (str(bot_login), coderabbit.DEFAULT_BOT_LOGIN, *pr_agent.DEFAULT_BOT_LOGINS)
        )
    )


def collect_snapshot(
    name: str,
    client: Any,
    pr_number: int,
    head_sha: str,
    settings: Any,
    *,
    apply: bool = False,
) -> ProviderSnapshot:
    """Read the provider's current output for a pull request.

    `apply=True` lets an adapter reconcile provider state it owns before the
    snapshot is taken. The decision stays provider-independent: an adapter only
    writes back what it already read, and anything it could not confirm stays
    blocking.
    """

    if name == PR_AGENT:
        return pr_agent.collect(client, pr_number, bot_login=settings.bot_login)
    if name == CODERABBIT:
        return coderabbit.collect(
            client,
            pr_number,
            head_sha,
            bot_login=settings.bot_login,
            status_context=settings.status_context,
            apply=apply,
        )
    raise UnsupportedProvider(
        f"Unsupported review provider {name!r}; supported: {', '.join(supported_providers())}"
    )


def provider_covers_head(
    name: str,
    settings: Any,
    *,
    reviews: Sequence[Dict[str, Any]],
    statuses: Sequence[Dict[str, Any]],
    head: str,
) -> bool:
    """Whether the provider already reported on this exact HEAD.

    The queue uses this to decide whether a candidate still owes the shared
    provider slot a full review. The answer is the adapter's, because only the
    adapter knows which of the provider's surfaces carries that meaning.
    """

    if name == CODERABBIT:
        return coderabbit.covers_head(
            reviews,
            statuses,
            head,
            bot_login=settings.bot_login,
            status_context=settings.status_context,
        )
    return any(
        (review.get("commit_id") or "").lower() == (head or "").lower()
        for review in reviews or []
    )


def repair_port(name: str) -> repair.RepairPort:
    """The provider-shaped half of the generic review-repair contract.

    Every adapter returns a port; an adapter with nothing to build returns a port
    with empty builders, which makes the contract answer "wait" instead of
    pretending it asked the provider for something. That is the honest degradation
    for a provider whose repair surface has not been ported yet.
    """

    if name == CODERABBIT:
        return repair.RepairPort(
            provider=CODERABBIT,
            verification_body=coderabbit.verification_body,
            full_review_body=coderabbit.full_review_body,
            historical_verification_markers=coderabbit.HISTORICAL_VERIFICATION_MARKERS,
            historical_rereview_markers=coderabbit.HISTORICAL_REREVIEW_MARKERS,
        )
    if name == PR_AGENT:
        return repair.RepairPort(provider=PR_AGENT)
    raise UnsupportedProvider(
        f"Unsupported review provider {name!r}; supported: {', '.join(supported_providers())}"
    )
