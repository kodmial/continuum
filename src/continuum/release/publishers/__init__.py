"""Downstream package publishers, isolated from the release pipeline.

A publisher runs after a release exists. It is handed the release's identity,
the immutable URLs its assets were published at, and the digests those URLs must
serve, and it turns them into a package manager's own description of the
release. It never builds, re-signs, or uploads anything, and it never asks the
release pipeline for a favour.

The package is deliberately separate from `continuum.release.plan` and
`continuum.release.run`. Those two own the release state machine and must not
learn the name of a package manager; this package owns everything a package
manager needs and knows nothing about how the release was cut. The seam between
them is `contract.PublishRequest` and `contract.PublishResult`, both immutable
value types, plus a `PackageRepository` for the write and an `AssetFetcher` for
the verification. A caller assembles those and hands them to a publisher; the
publisher returns a result and nothing else happens outside it.

Each publisher is reached through this registry rather than imported by name, so
adding a package manager is adding a module and an entry here, not editing the
caller. A publisher is registered under the name its configuration section uses.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Dict, Tuple

from . import contract, homebrew, macports

# The publishers this package ships. A name is the configuration key a project
# writes its settings under, and it is stable: it appears in reviewed config.
PUBLISHERS: Dict[str, ModuleType] = {
    homebrew.PUBLISHER_NAME: homebrew,
    macports.PUBLISHER_NAME: macports,
}


def names() -> Tuple[str, ...]:
    """Every registered publisher name, in a stable order."""

    return tuple(sorted(PUBLISHERS))


def get(name: str) -> ModuleType:
    """The publisher registered under `name`.

    A name that is not registered is a configuration error rather than an
    import error, so a project that misspells a publisher learns which names
    exist instead of seeing a traceback from inside a module it never asked for.
    """

    try:
        return PUBLISHERS[name]
    except KeyError:
        raise contract.PublisherError(
            contract.CONFIGURATION_INVALID,
            f"{name!r} is not a registered package publisher; the registered names "
            f"are {', '.join(names())}",
        ) from None


def parse_settings(name: str, value: Any, where: str = "") -> Any:
    """Validate one publisher's configuration through its own parser."""

    publisher = get(name)
    parse = publisher.parse_settings
    return parse(value, where or name)


__all__ = ["PUBLISHERS", "contract", "get", "homebrew", "macports", "names", "parse_settings"]
