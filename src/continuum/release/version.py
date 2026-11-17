"""Where a release version comes from, and whether it is allowed.

Versioning is the part of a release that most often disagrees with itself. The
same repository will have a version in a project file, a version in the merged
release pull request, a version on the tag that was pushed, and possibly a
version somebody typed into a manual dispatch. Four sources, one number, and a
release that ships the wrong one is a release nobody can upgrade to.

So the number and the question "where did it come from" are separate. A
*strategy* knows one place to read a version from; a *policy* decides which
versions a repository may publish. They never touch: a repository that wants
`0.1.x` only says so in its policy, and the strategy that reads the version out
of its project file does not learn that. That separation is also why the
publishing side of a release can be written once — it is handed a version that
has already been agreed on, and it does not care which of the four sources
produced it.

The four strategies here are the ones the reference implementation used between
them, kept as separate names rather than one function with a switch:

`explicit`
    A human typed it. Valid, and the most likely place for a typo, which is why
    it is the strategy most often paired with a second source.

`tag`
    A tag was pushed. The tag is the release.

`project-file`
    A file in the repository carries the version, so the version is part of the
    commit being released rather than something a tool decides afterwards.

`release-pr`
    A release pull request proposed it. The strategy reads the version the
    proposal carries; it does not compute one, and it does not open the pull
    request — that is a separate port, because proposing a version and opening
    the pull request that proposes it are different jobs with different
    permissions.

Strategies are *pure* about the number. A strategy gathers its own raw input
through `collect` and then proposes from it, so a proposal can be asserted in a
test with no repository, no network, and no clock.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from .contract import ContractError, ReleaseEvent

# Semantic versioning, strictly. The strictness is the point: `01.2.3` and
# `1.02.3` are two spellings of nothing, and a release that sorts them
# differently from the package registry it publishes to is an upgrade that does
# not upgrade.
#
# Build metadata is accepted and ignored. It is not part of a version's identity
# for ordering purposes, and a repository that stamps a build id there has not
# asked for anything this core has to enforce.
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

# The prefix a tag carries. `v1.2.3` is overwhelmingly the convention, and the
# core needs to know the mapping because the tag is the release's immutable
# identity: a tag is what a consumer pins, and `1.2.3` and `v1.2.3` are two
# different releases to every git tool.
DEFAULT_TAG_PREFIX = "v"

# A series is a leading `major` or `major.minor`. Allowing a series is how a
# repository that is still finding its versioning says "0.0.x and 0.1.x are the
# versions I publish" without a policy that has to be rewritten at 1.0.
_SERIES_RE = re.compile(r"^(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$")

_VERSION = "version"
STRATEGY_EXPLICIT = "explicit"
STRATEGY_TAG = "tag"
STRATEGY_PROJECT_FILE = "project-file"
STRATEGY_RELEASE_PR = "release-pr"


class VersionError(ValueError):
    """Raised when a version cannot be agreed, or is not one that may be cut."""

    def __init__(self, message: str, *, code: str = "version-invalid") -> None:
        super().__init__(message)
        self.code = code


def is_version(value: str) -> bool:
    return bool(_SEMVER_RE.match(value or ""))


def is_prerelease(value: str) -> bool:
    match = _SEMVER_RE.match(value or "")
    return bool(match and match.group("pre"))


def normalize(value: str) -> str:
    """Strip decoration and return the bare semantic version.

    A leading `v` and surrounding whitespace are decoration: `v1.2.3` and
    `1.2.3` name one version, and a strategy that compared them as strings would
    treat a tag and a project file as disagreeing when they agree.
    """

    text = (value or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not _SEMVER_RE.match(text):
        raise VersionError(
            f"{value!r} is not a semantic version. A release version is three "
            "dot-separated numbers with an optional pre-release suffix; nothing else "
            "sorts, and a version that does not sort cannot be upgraded to.",
            code="version-malformed",
        )
    return text


def tag_for(version: str, prefix: str = DEFAULT_TAG_PREFIX) -> str:
    return f"{prefix}{normalize(version)}"


def version_from_tag(tag: str, prefix: str = DEFAULT_TAG_PREFIX) -> str:
    text = (tag or "").strip()
    if prefix and text.startswith(prefix):
        text = text[len(prefix) :]
    elif text[:1] in ("v", "V"):
        text = text[1:]
    return normalize(text)


@dataclass(frozen=True)
class VersionPolicy:
    """Which versions this repository is allowed to publish.

    A policy is a filter, not a proposer. It never looks at a repository or an
    event, so the same policy object governs a manual dispatch and a release
    pull request and cannot be right for one and wrong for the other.
    """

    allowed_series: Tuple[str, ...] = ()
    allow_prerelease: bool = False
    tag_prefix: str = DEFAULT_TAG_PREFIX

    def __post_init__(self) -> None:
        for series in self.allowed_series:
            if not _SERIES_RE.match(series or ""):
                raise VersionError(
                    f"{series!r} is not a version series. A series is a major, or a "
                    "major and a minor, as in '0' or '0.1'."
                )
        if self.tag_prefix and not re.match(r"^[A-Za-z._-]{0,16}$", self.tag_prefix):
            raise VersionError(
                f"tag prefix {self.tag_prefix!r} must be letters, '.', '_' or '-'; it "
                "becomes part of every tag and of a shell-visible artifact name"
            )

    @property
    def series(self) -> Tuple[str, ...]:
        return tuple(sorted(self.allowed_series))

    def admit(self, version: str) -> bool:
        """Whether ``version`` may be published under this policy.

        A series of one part (`0`) admits every minor of that major. A series of
        two parts (`0.1`) admits only that major.minor, which is how a
        repository still finding its versioning pins a narrower set.
        """

        parsed = _SEMVER_RE.match(version or "")
        if parsed is None:
            return False
        if parsed.group("pre") and not self.allow_prerelease:
            return False
        if not self.allowed_series:
            return True
        major = parsed.group("major")
        minor = parsed.group("minor")
        return any(
            series == (f"{major}.{minor}" if "." in series else major)
            for series in self.allowed_series
        )

    def require(self, version: str) -> str:
        """Return ``version`` if the policy admits it, and fail closed if not."""

        if not self.admit(version):
            allowed = ", ".join(self.series) if self.allowed_series else "any stable version"
            if not self.allow_prerelease and is_prerelease(version):
                raise VersionError(
                    f"{version} is a pre-release and this repository's policy publishes "
                    "stable versions only. Opt in with allow_prerelease rather than "
                    "naming a stable-looking tag for a pre-release.",
                    code="version-prerelease-not-allowed",
                )
            raise VersionError(
                f"{version} is not a version this repository publishes. Allowed "
                f"series: {allowed}.",
                code="version-series-not-allowed",
            )
        return version

    def describe(self) -> Dict[str, Any]:
        return {
            "allowed_series": list(self.allowed_series),
            "allow_prerelease": self.allow_prerelease,
            "tag_prefix": self.tag_prefix,
        }


@dataclass(frozen=True)
class VersionProposal:
    """A version, and the claim about where it came from that produced it.

    `origin` is not decoration. When two events disagree about a version, the
    thing that settles it is knowing which source each one was reading, and a
    proposal that only carried the number would make the disagreement
    unresolvable after the fact.
    """

    version: str
    source: str
    origin: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not _SEMVER_RE.match(self.version or ""):
            raise VersionError(
                f"a proposal carries a bare semantic version, not {self.version!r}",
                code="version-malformed",
            )
        if not self.source:
            raise VersionError("a proposal must name the strategy that produced it")

    @property
    def tag(self) -> str:
        return tag_for(self.version)

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "version": self.version,
            "source": self.source,
            "tag": self.tag,
        }
        if self.origin:
            payload["origin"] = self.origin
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class VersionAgreement:
    """The result of comparing independent sources about one version.

    Sources that agree are evidence. Sources that disagree are a *failure*, not
    a majority vote: the reference implementation refused a manual dispatch whose
    typed version did not match the one in the project file, and it was right —
    a disagreement means one of the two is stale, and publishing either one ships
    something the other record will not accept.
    """

    version: str
    sources: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts

    @property
    def agreed(self) -> bool:
        return bool(self.sources) and self.ok

    def describe(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "sources": list(self.sources),
            "conflicts": list(self.conflicts),
            "agreed": self.agreed,
        }


def agree(*proposals: VersionProposal) -> VersionAgreement:
    """Compare proposals from independent sources and report the disagreement."""

    if not proposals:
        raise VersionError(
            "a version has to come from somewhere; no source proposed one",
            code="version-absent",
        )
    distinct = sorted({proposal.version for proposal in proposals})
    sources = tuple(sorted(proposal.source for proposal in proposals))
    origins = [
        f"{proposal.source} says {proposal.version}"
        + (f" ({proposal.origin})" if proposal.origin else "")
        for proposal in proposals
    ]
    if len(distinct) > 1:
        # No version, not the lowest one. A disagreement has no answer, and an
        # agreement object that carried one would let a caller that checks
        # nothing but `.version` publish whichever number happened to sort first.
        return VersionAgreement(version="", sources=sources, conflicts=tuple(origins))
    return VersionAgreement(version=distinct[0], sources=sources)


def require_agreement(agreement: VersionAgreement) -> str:
    if agreement.ok:
        return agreement.version
    raise VersionError(
        "the sources of this release's version disagree: "
        + "; ".join(agreement.conflicts)
        + ". Refusing to cut a release, because whichever number is published, one of "
        "those records will be wrong and the upgrade will not match it.",
        code="version-sources-disagree",
    )


# -- the context a strategy proposes from -------------------------------------


@dataclass(frozen=True)
class VersionContext:
    """Everything a strategy may read, gathered before it is asked.

    `text` is the strategy's own raw input, which the strategy collected through
    `collect`. Keeping collection separate from proposal is what lets a proposal
    be tested against a string rather than against a repository.
    """

    event: ReleaseEvent
    text: str = ""
    source_path: str = ""
    requested: str = ""
    release_pr: int = 0

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"event": self.event.describe()}
        if self.source_path:
            payload["source_path"] = self.source_path
        if self.requested:
            payload["requested"] = self.requested
        if self.release_pr:
            payload["release_pr"] = self.release_pr
        return payload


class VersionStrategy:
    """A place to read a version from.

    Subclasses set `name`, gather their input, and propose. Proposing a version
    reads a file or a pull request but changes nothing, so a strategy runs
    unchanged in a dry run: the plan of a release names the version it would cut.
    """

    SUPPORTS_DRY_RUN = True
    name = ""

    def collect(self, event: ReleaseEvent, workdir: str = "") -> Tuple[str, str]:
        """Gather the raw text this strategy reads a version from."""

        return "", ""

    def propose(self, context: VersionContext, policy: VersionPolicy) -> VersionProposal:
        raise NotImplementedError

    def intent(self) -> str:
        raise NotImplementedError

    def _admit(self, version: str, context: VersionContext, policy: VersionPolicy) -> str:
        return policy.require(normalize(version))


@dataclass(frozen=True)
class ExplicitVersion(VersionStrategy):
    """A version a human named — a manual dispatch, or an argument."""

    SUPPORTS_DRY_RUN = True
    name = STRATEGY_EXPLICIT
    requested: str = ""

    def intent(self) -> str:
        return "use the version this run was asked for, if one was given"

    def propose(self, context: VersionContext, policy: VersionPolicy) -> VersionProposal:
        requested = (self.requested or context.requested or "").strip()
        if not requested:
            raise VersionError(
                "this release was dispatched without a version. An explicit release "
                "needs one; use the tag or project-file strategy for a release that "
                "takes its version from the repository.",
                code="version-not-specified",
            )
        version = self._admit(requested, context, policy)
        return VersionProposal(
            version=version,
            source=self.name,
            origin=f"requested {requested}",
            detail="named by the dispatch that started this release",
        )


@dataclass(frozen=True)
class TagVersion(VersionStrategy):
    """A version carried by the tag that was pushed."""

    SUPPORTS_DRY_RUN = True
    name = STRATEGY_TAG
    tag: str = ""

    def intent(self) -> str:
        return "cut the version named by the pushed tag"

    def collect(self, event: ReleaseEvent, workdir: str = "") -> Tuple[str, str]:
        return self.tag or event.tag, "tag"

    def propose(self, context: VersionContext, policy: VersionPolicy) -> VersionProposal:
        tag = (context.text or "").strip()
        if not tag:
            raise VersionError(
                "this release was triggered by a tag push but no tag was named, so "
                "there is no version to cut. The tag is the release; without it the "
                "only honest outcome is to do nothing.",
                code="version-tag-absent",
            )
        version = self._admit(version_from_tag(tag, policy.tag_prefix), context, policy)
        return VersionProposal(
            version=version,
            source=self.name,
            origin=f"tag {tag}",
            detail="the tag is this release's immutable identity",
        )


@dataclass(frozen=True)
class ProjectFileVersion(VersionStrategy):
    """A version carried by a file in the repository being released.

    The pattern is required, and it is the repository's, because "the first
    semantic version in this file" is only correct for a file that has one. Two
    *different* versions matching is a failure rather than a first-match guess:
    a file that says two things about its version is a file whose version nobody
    knows, and publishing either one is a coin toss that decides what every
    consumer installs.
    """

    SUPPORTS_DRY_RUN = True
    name = STRATEGY_PROJECT_FILE
    path: str = ""
    pattern: str = _SEMVER_RE.pattern

    def __post_init__(self) -> None:
        if not self.path:
            raise VersionError(
                "the project-file strategy needs the file it reads; a version derived "
                "from nowhere is a guess"
            )
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise VersionError(
                f"the project-file pattern is not a regular expression: {exc}",
                code="version-pattern-invalid",
            ) from None

    def intent(self) -> str:
        return f"read the release version from {self.path}"

    def collect(self, event: ReleaseEvent, workdir: str = "") -> Tuple[str, str]:
        candidates = [self.path]
        if workdir:
            candidates.insert(0, os.path.join(workdir, self.path))
        for candidate in candidates:
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as handle:
                    return handle.read(), candidate
        raise VersionError(
            f"{self.path} is not a readable file in this release, so the version it "
            "carries cannot be read. A version strategy that cannot read its source "
            "must fail here rather than fall back to another one: silently using a "
            "different source is how a release gets a version nobody approved.",
            code="version-source-missing",
        )

    def propose(self, context: VersionContext, policy: VersionPolicy) -> VersionProposal:
        matches = {
            normalize(match.group(0)) for match in re.finditer(self.pattern, context.text or "")
        }
        if not matches:
            raise VersionError(
                f"{context.source_path or self.path} carries no version matching this "
                "repository's pattern, so there is nothing to release.",
                code="version-not-found",
            )
        if len(matches) > 1:
            raise VersionError(
                f"{context.source_path or self.path} carries more than one version "
                f"({', '.join(sorted(matches))}). A file that disagrees with itself "
                "about its version does not have one; fix the file rather than let the "
                "release choose.",
                code="version-ambiguous",
            )
        version = self._admit(matches.pop(), context, policy)
        return VersionProposal(
            version=version,
            source=self.name,
            origin=context.source_path or self.path,
            detail="the version is part of the commit being released",
        )


@dataclass(frozen=True)
class ReleasePullRequestVersion(VersionStrategy):
    """A version proposed by the release pull request that was just merged.

    The strategy does not compute a version and does not open a pull request. It
    reads the version the proposal carries and agrees with it, because the version
    bump *is* the change being released: taking a different number from a
    different file would release a version whose commit is not the one that
    declares it.
    """

    SUPPORTS_DRY_RUN = True
    name = STRATEGY_RELEASE_PR
    pr_number: int = 0
    manifest_path: str = ""

    def intent(self) -> str:
        return "cut the version the merged release pull request proposed"

    def collect(self, event: ReleaseEvent, workdir: str = "") -> Tuple[str, str]:
        proposed = event.attribute("release-pr-version")
        if proposed:
            return proposed, "release pull request"
        if not self.manifest_path:
            return "", ""

        candidate = self.manifest_path
        if workdir and not os.path.isabs(candidate):
            joined = os.path.join(workdir, candidate)
            if os.path.isfile(joined):
                candidate = joined
        if not os.path.isfile(candidate):
            raise VersionError(
                f"{self.manifest_path} is not a readable file, so the version the "
                "release pull request proposed cannot be read.",
                code="version-source-missing",
            )
        with open(candidate, encoding="utf-8") as handle:
            return handle.read(), candidate

    def propose(self, context: VersionContext, policy: VersionPolicy) -> VersionProposal:
        text = (context.text or "").strip()
        if not text:
            raise VersionError(
                "this release was triggered by a merged release pull request, but the "
                "event carries no proposed version. A release that takes its version "
                "from a proposal it cannot read would take it from somewhere else "
                "instead.",
                code="version-not-proposed",
            )
        try:
            version = self._admit(text, context, policy)
        except VersionError as exc:
            if exc.code == "version-malformed":
                raise VersionError(
                    f"the merged release pull request proposed {text!r}, which is not a "
                    "version. Refusing to cut a release whose own proposal is not a "
                    "version.",
                    code="version-not-proposed",
                ) from None
            raise
        number = self.pr_number or context.release_pr
        origin = f"release pull request #{number}" if number else "release pull request"
        return VersionProposal(
            version=version,
            source=self.name,
            origin=origin,
            detail="the version bump is the change this release ships",
        )


# -- registry ----------------------------------------------------------------
#
# The same shape as the review provider registry and the release adapter
# registry: naming a strategy selects an implementation, and nothing above this
# file switches on which one it is. A repository that cuts releases from a
# private manifest registers its own strategy and joins here.

StrategyFactory = Callable[..., VersionStrategy]

_STRATEGIES: Dict[str, StrategyFactory] = {
    STRATEGY_EXPLICIT: ExplicitVersion,
    STRATEGY_TAG: TagVersion,
    STRATEGY_PROJECT_FILE: ProjectFileVersion,
    STRATEGY_RELEASE_PR: ReleasePullRequestVersion,
}


class UnknownStrategy(ContractError):
    """Raised when configuration names a version strategy that does not exist."""


def strategies() -> Tuple[str, ...]:
    return tuple(sorted(_STRATEGIES))


def get_strategy(name: str, **options: Any) -> VersionStrategy:
    factory = _STRATEGIES.get(name)
    if factory is None:
        raise UnknownStrategy(
            f"unknown version strategy {name!r}; available: " + ", ".join(strategies())
        )
    try:
        strategy = factory(**options)
    except TypeError as exc:
        raise UnknownStrategy(
            f"version strategy {name!r} was configured with options it does not take: {exc}"
        ) from None
    if not strategy.name:
        raise UnknownStrategy(
            f"version strategy {name!r} produced an instance with no name; every "
            "strategy has to name itself, because the proposal records which one ran"
        )
    return strategy


def register_strategy(name: str, factory: StrategyFactory) -> None:
    if not name:
        raise UnknownStrategy("a version strategy must be registered under a name")
    if name in _STRATEGIES:
        raise UnknownStrategy(
            f"version strategy {name!r} is already registered; a duplicate registration "
            "is a copy-paste, and the copy would never be selected"
        )
    _STRATEGIES[name] = factory


__all__ = [
    "DEFAULT_TAG_PREFIX",
    "STRATEGY_EXPLICIT",
    "STRATEGY_PROJECT_FILE",
    "STRATEGY_RELEASE_PR",
    "STRATEGY_TAG",
    "ExplicitVersion",
    "ProjectFileVersion",
    "ReleasePullRequestVersion",
    "TagVersion",
    "UnknownStrategy",
    "VersionAgreement",
    "VersionContext",
    "VersionError",
    "VersionPolicy",
    "VersionProposal",
    "VersionStrategy",
    "agree",
    "get_strategy",
    "is_prerelease",
    "is_version",
    "normalize",
    "register_strategy",
    "require_agreement",
    "strategies",
    "tag_for",
    "version_from_tag",
]
