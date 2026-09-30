"""Wiring a repository's own configuration into the release core.

`ReleaseCore` takes its components by injection and knows nothing about any
repository. This module is the seam where a `.continuum.yml` becomes a set of
components, and it is deliberately thin: every decision about *what* to release
is made by the configuration, and every decision about *how* is made by the
adapter or the strategy that was registered.

The components this module can build on its own are the ones with no external
side effects and no policy of their own to get wrong: an **eligibility policy**
and a **notes builder**, both of which are needed for the chain to be walkable at
all. The version strategy is required from the caller, because the right one
depends on the event being released rather than on the repository's
configuration.

The ports that talk to something outside the checkout — publishers, the release
repository, downstream syncs, the release pull request — are required arguments.
A release that silently ran with no publisher would report success having
published nothing, and one that silently ran with a guessed eligibility policy
would make up a policy a human never wrote. So this module refuses to assemble a
release plane that is missing one.

There is no production `release-repository` client here. That is not an omission
to paper over with a stub: `GitHubReleasePublisher` takes its repository by
injection, so a caller that has one hands it in, and a caller that does not gets
an error naming what is missing rather than a release that appears to succeed.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .. import config as config_module
from .contract import ContractError, ReleaseEvent
from .core import ReleaseComponents, ReleaseRequest
from .version import VersionPolicy


class TagEligibility:
    """The eligibility policy a tag-driven release implies.

    A tag push is a human saying "this is the release". This policy therefore
    *agrees* with it rather than re-deciding it, and records the agreement as a
    code rather than as prose: the contract does not let an eligible event carry
    a reason, and rightly so — there is no refusal to explain. What the journal
    keeps is the code, which is enough to tell "it was tagged" apart from "a
    policy said so".

    Anything that is not a tag push is not eligible, and not merely because it
    lacks a tag: a release pinned to a tag is what makes the version in the
    manifest and the version in the repository agree, and a branch push has
    neither.
    """

    SUPPORTS_DRY_RUN = True
    name = "tag-push"

    def intent(self) -> str:
        return "accept a tag push as the decision to release, and refuse anything else"

    def evaluate(self, event: ReleaseEvent) -> Any:
        from .contract import ELIGIBLE, Eligibility

        if event.tag:
            return ELIGIBLE
        return Eligibility(
            eligible=False,
            reason=(
                f"event {event.name!r} on {event.ref!r} is not a tag push. A release is "
                "pinned to one tag, so a branch event has no version to release."
            ),
            code="not-a-tag",
            blocking=True,
        )


class ChangelogNotes:
    """Release notes assembled from the commits between two tags.

    Written against an injected `git log`, because reading git is a side effect
    and this module's whole job is to be the place where side effects are
    declared. With no reader configured it produces a body that says exactly
    that, rather than an empty string that would publish as a release with
    nothing in its notes.
    """

    SUPPORTS_DRY_RUN = True
    name = "commits"

    def __init__(self, *, log: Optional["LogReader"] = None, limit: int = 50) -> None:
        self.log = log
        self.limit = limit

    def intent(self) -> str:
        return "describe a release as the commits between the previous tag and this one"

    def build(self, request: Any) -> Any:
        from .contract import ReleaseNotes

        if self.log is None:
            return ReleaseNotes(
                body=f"{request.tag} was released.",
                notes=("no commit reader is configured, so the notes name the release "
                       "and nothing else"),
            )
        entries = self.log(request.previous_tag, request.tag, self.limit)
        if not entries:
            return ReleaseNotes(
                body=f"{request.tag} was released.",
                notes=("no commits were found between the previous tag and this one"),
            )
        body = "\n".join(f"- {entry}" for entry in entries)
        return ReleaseNotes(body=f"## {request.tag}\n\n{body}")


#: `git log --format=%s <from>..<to>`, or an equivalent. Injected rather than
#: shelled out here so the shape stays a data question, not a process boundary.
LogReader = Any


def components(
    config: Any,
    *,
    version: Any,
    adapters: Mapping[str, Any],
    publishers: Sequence[Any] = (),
    release_pr: Optional[Any] = None,
    syncs: Sequence[Any] = (),
    eligibility: Optional[Any] = None,
    notes: Optional[Any] = None,
) -> ReleaseComponents:
    """Assemble the release plane a validated configuration describes.

    `adapters` is a mapping of adapter name to component rather than a list,
    because a target names its adapter as a string and the core resolves it by
    that string.

    `version` is required rather than derived. A tag push and a release pull
    request are cut by different strategies, and which one is right depends on
    the event being released — not on anything `.continuum.yml` can say. Picking
    one here would make a repository's release take its version from a guess.
    Callers get it from `version.get_strategy(name)`, which is the same registry
    the strategies themselves register into.

    A repository that configures a target naming an adapter nobody registered
    fails at the first stage that needs it, which is after eligibility and
    version have run; `conformance_errors` below answers the same question
    before anything runs.
    """

    plane = ReleaseComponents(
        eligibility=eligibility or TagEligibility(),
        version=version,
        notes=notes or ChangelogNotes(),
        adapters=dict(adapters),
        release_pr=release_pr,
        publishers=tuple(publishers),
        syncs=tuple(syncs),
    )
    return plane.conform()


def conformance_errors(
    config: Any,
    adapters: Mapping[str, Any],
) -> Tuple[str, ...]:
    """Which configured targets could not be built, by name.

    Asked before a run rather than discovered during one: a target naming an
    adapter that is not registered is a configuration error, and finding it in the
    journal after the release has already built three other targets means the
    release published a partial set.
    """

    errors: list = []
    registered = set(adapters)
    for target in config.release.targets:
        if target.adapter not in registered:
            errors.append(
                f"target {target.id!r} names adapter {target.adapter!r}, which is not "
                f"registered. Registered: {', '.join(sorted(registered)) or 'none'}."
            )
    return tuple(errors)


def request_from_config(
    config: Any,
    event: ReleaseEvent,
    *,
    workdir: str = "",
    dry_run: bool = False,
    version_policy: Optional[VersionPolicy] = None,
    **options: Any,
) -> ReleaseRequest:
    """A `ReleaseRequest` for one event, carrying every target's own options.

    `ReleaseRequest.from_config` is what turns a declared target into something
    the core can hand to an adapter; this wrapper only supplies the parts a
    repository owns — where its checkout is, whether this is a plan, and what
    version a manual dispatch asked for. Anything else the core accepts is passed
    through rather than re-listed, so this cannot fall behind `from_config`.
    """

    return ReleaseRequest.from_config(
        config,
        event,
        version_policy=version_policy,
        workdir=workdir,
        dry_run=dry_run,
        **options,
    )


__all__: Tuple[str, ...] = (
    "ChangelogNotes",
    "TagEligibility",
    "components",
    "conformance_errors",
    "request_from_config",
)