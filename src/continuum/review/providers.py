"""Review provider registry.

Adding a provider means adding a module that returns a `ProviderSnapshot`. The
gate, the normalized result, and the merge controller are unaware of it.
"""

from __future__ import annotations

from typing import Any

from .. import config as config_module
from . import coderabbit, pr_agent
from .snapshot import ProviderSnapshot

PR_AGENT = config_module.PROVIDER_PR_AGENT
CODERABBIT = config_module.PROVIDER_CODERABBIT
NONE = config_module.PROVIDER_NONE


class UnsupportedProvider(ValueError):
    """Raised when configuration names a provider Continuum cannot run."""


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


def collect_snapshot(
    name: str,
    client: Any,
    pr_number: int,
    head_sha: str,
    settings: Any,
) -> ProviderSnapshot:
    """Read the provider's current output for a pull request."""

    if name == PR_AGENT:
        return pr_agent.collect(client, pr_number, bot_login=settings.bot_login)
    if name == CODERABBIT:
        return coderabbit.collect(
            client,
            pr_number,
            head_sha,
            bot_login=settings.bot_login,
            status_context=settings.status_context,
        )
    raise UnsupportedProvider(
        f"Unsupported review provider {name!r}; supported: {', '.join(supported_providers())}"
    )
