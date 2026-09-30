"""Moving a repository's release onto Continuum, and proving nothing was lost.

A consumer that has its own release writers is not wrong; it has just decided,
once, to keep a copy of the parts of a release that are the same everywhere. The
cutover is the moment that copy is deleted, and deletion is irreversible in the
only way that matters: after it, there is no second implementation left to fall
back to. So this module exists to make the cutover a decision with evidence
behind it rather than a hopeful edit.

Four questions have to be answerable, and each one is a separate mechanism here
because they fail in different ways.

**What does the consumer lose?** :data:`PRESERVED` is the ledger, and it is data
rather than prose because prose cannot fail a build. Every entry names the
Continuum surface that now supplies the behaviour, and :func:`ledger` resolves
each of those names against the release it is running in. An entry whose owner
has been renamed or deleted is a behaviour that silently stopped being supplied,
and it shows up here as an unresolved entry instead of as a support ticket.

**Would the new release publish the same thing?** :class:`ReleaseFacts` is the
answerable form of "a release", holding the resolved version, the candidate file
names, the digests recorded against them, and the files each publisher would
write. :func:`compare` puts two of them side by side. It distinguishes a
divergence from a silence, because a dry run has no bytes and therefore cannot
report a digest: an axis that either side says nothing about is *unevaluated*,
never *agreed*. A cutover certified on axes nobody measured is the failure this
avoids.

**Can this be driven twice without publishing twice?** :func:`canary` reads a
release outcome and reports whether anything was published. The dedupe is not
implemented here — it is the journal and the release key, which is where it
already lives, and which a replay of the same event already exercises.

**What is the way back?** :class:`Rollback` pins the last commit the consumer's
own writers shipped, and keeps that pin until a Continuum-managed production
release has actually been recorded in a journal. The check is deliberately
fail-closed: a claimed production release with no journal entry behind it is not
evidence, so the pin stays.

Nothing here runs a release, writes a file, or talks to GitHub. Every value here
is something a caller already had.
"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .contract import ContractError, require_source_sha
from .core import ReleaseOutcome
from .state import EMPTY_JOURNAL, FAILED, Journal

ADOPTION_SCHEMA = "continuum.release-adoption/v1"

#: An entry whose behaviour is supplied by this release of Continuum.
OWNED = "owned"

#: An entry waiting on work that has not landed. Named rather than omitted,
#: because a capability that is deliberately out of scope and a capability
#: somebody forgot are the same thing to a reader of a release script, and only
#: one of them is safe.
DEFERRED = "deferred"


class AdoptionError(ValueError):
    """Raised when a cutover would give something up without evidence for it."""


@dataclass(frozen=True)
class Preserved:
    """One behaviour the consumer's release used to supply, and who supplies it now.

    `owner` is a ``module:symbol`` name rather than prose so that the claim can
    be checked. A ledger entry that says "handled by signing" cannot fail; an
    entry that says ``continuum.release.apple:bind_material`` fails the moment
    that name stops resolving, which is the moment the behaviour actually
    stopped being supplied.
    """

    key: str
    summary: str
    owner: str = ""
    deferred_to: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise AdoptionError("a preserved behaviour needs a key")
        if not self.summary:
            raise AdoptionError(f"preserved behaviour {self.key!r} needs a summary")
        if bool(self.owner) == bool(self.deferred_to):
            missing = (
                "no owner and nothing it waits for"
                if not self.owner
                else "both an owner and a wait"
            )
            raise AdoptionError(
                f"preserved behaviour {self.key!r} names {missing}; it needs exactly one"
            )

    @property
    def state(self) -> str:
        return OWNED if self.owner else DEFERRED

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"key": self.key, "summary": self.summary, "state": self.state}
        if self.owner:
            payload["owner"] = self.owner
        else:
            payload["deferred_to"] = self.deferred_to
        return payload


#: The behaviours a consumer hands over when it stops running its own release
#: writers, and the Continuum surface that supplies each one after the cutover.
#:
#: Two entries are worth reading twice. `cli-and-app-bundle` and
#: `hardened-runtime` share an owner because they are assembled by the same
#: plan, not because one was left off; and `rust-static-library` is deferred
#: because the work that produces it has not landed in this release, which is a
#: different statement from it being handled.
PRESERVED: Tuple[Preserved, ...] = (
    Preserved(
        key="release-pr",
        summary="the release-please pull request and its version",
        owner="continuum.release.version:ReleasePullRequestVersion",
    ),
    Preserved(
        key="version-consistency",
        summary="Version.swift, the manifest version, and the tag agreeing",
        owner="continuum.release.version:require_agreement",
    ),
    Preserved(
        key="cli-and-app-bundle",
        summary="the CLI archive and the signed app bundle",
        owner="continuum.release.apple:plan_for",
    ),
    Preserved(
        key="rust-static-library",
        summary="the Rust static library and its linking",
        deferred_to="#67",
    ),
    Preserved(
        key="stable-signing-identity",
        summary="the stable self-signed P12 identity the previous release carried",
        owner="continuum.release.apple:bind_material",
    ),
    Preserved(
        key="hardened-runtime",
        summary="hardened runtime and the app's entitlements",
        owner="continuum.release.apple:plan_for",
    ),
    Preserved(
        key="candidate-bytes",
        summary="the exact candidate bytes Packaging already proved",
        owner="continuum.release.contract:ArtifactManifest",
    ),
    Preserved(
        key="checksums-and-release",
        summary="the checksum listing and the GitHub Release that carries it",
        owner="continuum.release:GitHubReleasePublisher",
    ),
    Preserved(
        key="homebrew",
        summary="the Homebrew formula and cask",
        owner="continuum.release.publishers.homebrew:plan",
    ),
    Preserved(
        key="macports",
        summary="the MacPorts Portfile and the port tree it lives in",
        owner="continuum.release.publishers.macports:plan",
    ),
    Preserved(
        key="installer-pin",
        summary="the installer line that pins the port tree revision",
        owner="continuum.release.publishers.macports:SURFACE_INSTALLER",
    ),
    Preserved(
        key="wakeups-and-loop-prevention",
        summary="scheduled wakeups arriving into one walk of one release",
        owner="continuum.release:ReleaseCore",
    ),
    Preserved(
        key="idempotent-retries",
        summary="a re-driven or interrupted release resuming instead of repeating",
        owner="continuum.release.state:Journal",
    ),
)


def _resolve(owner: str) -> Optional[Any]:
    """Import the module an owner names and hand back the symbol, if it is there."""

    module_name, _, symbol = owner.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    if not symbol:
        return module
    return getattr(module, symbol, None)


@dataclass(frozen=True)
class Ledger:
    """The preserved behaviours, and the ones this release does not actually supply.

    Built by :func:`ledger` rather than by hand: the two fields have no defaults
    because a ledger with no unresolved entries is a result, and a value that
    defaults to "everything is covered" is the one shape this type must not have.
    """

    entries: Tuple[Preserved, ...]
    unresolved: Tuple[Preserved, ...]

    @property
    def ok(self) -> bool:
        return not self.unresolved

    @property
    def owned(self) -> Tuple[Preserved, ...]:
        return tuple(entry for entry in self.entries if entry.state == OWNED)

    @property
    def deferred(self) -> Tuple[Preserved, ...]:
        return tuple(entry for entry in self.entries if entry.state == DEFERRED)

    def keys(self) -> Tuple[str, ...]:
        return tuple(entry.key for entry in self.entries)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": ADOPTION_SCHEMA,
            "ok": self.ok,
            "entries": [entry.describe() for entry in self.entries],
            "unresolved": [entry.key for entry in self.unresolved],
            "deferred": [entry.key for entry in self.deferred],
        }


def ledger(entries: Sequence[Preserved] = PRESERVED) -> Ledger:
    """Resolve every owner in ``entries`` against this release of Continuum.

    A deferred entry is not unresolved. It is waiting on something named, and the
    waiting is the record; what must not happen is an entry that claims an owner
    which no longer exists, because that reads as covered and behaves as lost.
    """

    listed = tuple(entries)
    seen: Dict[str, str] = {}
    for entry in listed:
        if entry.key in seen:
            raise AdoptionError(
                f"preserved behaviour {entry.key!r} is listed twice; two entries with "
                "one key cannot both be true"
            )
        seen[entry.key] = entry.state
    unresolved = tuple(
        entry for entry in listed if entry.state == OWNED and _resolve(entry.owner) is None
    )
    return Ledger(entries=listed, unresolved=unresolved)


AXIS_VERSION = "version"
AXIS_CANDIDATES = "candidates"
AXIS_DIGESTS = "digests"
AXIS_TEMPLATES = "templates"
AXIS_PUBLISHERS = "publishers"

#: The axes a cutover is judged on, in the order a reader asks about them: what
#: is this release, what does it carry, what are those bytes, and what would be
#: written downstream of it.
AXES: Tuple[str, ...] = (
    AXIS_VERSION,
    AXIS_CANDIDATES,
    AXIS_DIGESTS,
    AXIS_TEMPLATES,
    AXIS_PUBLISHERS,
)

AGREED = "agreed"
DIVERGED = "diverged"
UNEVALUATED = "unevaluated"


@dataclass(frozen=True)
class ReleaseFacts:
    """One release, reduced to the facts two implementations have to agree on.

    The pairs are tuples rather than mappings so that the whole value can be
    compared, hashed, and printed, and so that order cannot smuggle a difference
    past a comparison that meant to be order-independent. Every collection is
    stored sorted.
    """

    version: str
    candidates: Tuple[str, ...] = ()
    digests: Tuple[Tuple[str, str], ...] = ()
    templates: Tuple[Tuple[str, str], ...] = ()
    publishers: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.version:
            raise AdoptionError("release facts need the version they describe")

    @property
    def digest_of(self) -> Dict[str, str]:
        return dict(self.digests)

    def axis(self, name: str) -> Tuple[Any, ...]:
        """The comparable content of one axis, as one value.

        Anything not named is returned empty rather than raising, because the
        axes are the module's list and a caller asking about a new one should get
        "nothing to say" instead of an exception from a comparison.
        """

        if name == AXIS_VERSION:
            return (self.version,)
        if name == AXIS_CANDIDATES:
            return tuple(self.candidates)
        if name == AXIS_DIGESTS:
            return tuple(self.digests)
        if name == AXIS_TEMPLATES:
            return tuple(self.templates)
        if name == AXIS_PUBLISHERS:
            return tuple(self.publishers)
        raise AdoptionError(
            f"unknown release axis {name!r}; known axes: " + ", ".join(AXES)
        )

    @classmethod
    def from_outcome(
        cls,
        outcome: ReleaseOutcome,
        *,
        generated: Iterable[Any] = (),
    ) -> "ReleaseFacts":
        """Read the facts out of a walk the core actually performed.

        `generated` is what the publisher plans returned: the files they would
        write, with the content they would write.

        A declared artifact still contributes its name, because a plan genuinely
        knows the shape it will produce. It does not contribute its digest. A
        declared artifact carries the digest of no bytes, and comparing that
        placeholder against a published release would report agreement on the one
        axis where the plan said nothing — so the digest axis stays empty and
        reads as unevaluated instead.
        """

        candidates = sorted(
            {name for manifest in outcome.manifests for name in manifest.names}
        )
        digests = sorted(
            {
                (artifact.name, artifact.digest)
                for manifest in outcome.manifests
                for artifact in manifest.artifacts
                if not artifact.declared
            }
        )
        templates = sorted(
            (item.path, hashlib.sha256(item.content.encode("utf-8")).hexdigest())
            for item in generated
        )
        publishers = sorted((item.surface, item.path) for item in generated)
        return cls(
            version=outcome.version,
            candidates=tuple(candidates),
            digests=tuple(digests),
            templates=tuple(templates),
            publishers=tuple(publishers),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "candidates": list(self.candidates),
            "digests": [{"name": name, "digest": digest} for name, digest in self.digests],
            "templates": [{"path": path, "digest": digest} for path, digest in self.templates],
            "publishers": [
                {"surface": surface, "path": path} for surface, path in self.publishers
            ],
        }


@dataclass(frozen=True)
class Divergence:
    """One axis where two implementations disagree, with both sides kept."""

    axis: str
    summary: str
    expected: Tuple[Any, ...] = ()
    actual: Tuple[Any, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "summary": self.summary,
            "expected": [str(item) for item in self.expected],
            "actual": [str(item) for item in self.actual],
        }


@dataclass(frozen=True)
class Comparison:
    """Two releases side by side, and what a cutover would be gambling on."""

    expected: ReleaseFacts
    actual: ReleaseFacts
    verdicts: Tuple[Tuple[str, str], ...]
    divergences: Tuple[Divergence, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether nothing measured disagrees.

        Not sufficient on its own: an axis nobody measured does not make this
        false, which is what :attr:`complete` is for.
        """

        return not self.divergences

    @property
    def complete(self) -> bool:
        """Whether every axis was actually measured and every one agreed.

        This is the property a cutover needs. ``ok`` alone would be satisfied by
        comparing two empty values, which is the shape a comparison takes when
        the dry run produced no candidates — the exact case where a cutover must
        not be certified.
        """

        return self.ok and all(verdict == AGREED for _, verdict in self.verdicts)

    @property
    def unevaluated(self) -> Tuple[str, ...]:
        return tuple(name for name, verdict in self.verdicts if verdict == UNEVALUATED)

    def verdict(self, axis: str) -> str:
        for name, verdict in self.verdicts:
            if name == axis:
                return verdict
        raise AdoptionError(f"unknown release axis {axis!r}; known axes: " + ", ".join(AXES))

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": ADOPTION_SCHEMA,
            "ok": self.ok,
            "complete": self.complete,
            "verdicts": [{"axis": name, "verdict": verdict} for name, verdict in self.verdicts],
            "divergences": [item.describe() for item in self.divergences],
            "expected": self.expected.describe(),
            "actual": self.actual.describe(),
        }


def compare(expected: ReleaseFacts, actual: ReleaseFacts) -> Comparison:
    """Compare two releases on every axis, and say plainly what was not measured.

    An axis where either side has nothing to say is `unevaluated` rather than
    `agreed`. The alternative — treating silence as agreement — is how a cutover
    gets certified by a dry run that named no candidate files at all.
    """

    verdicts: list = []
    divergences: list = []
    for axis in AXES:
        left = expected.axis(axis)
        right = actual.axis(axis)
        if not left or not right:
            verdicts.append((axis, UNEVALUATED))
            continue
        if left == right:
            verdicts.append((axis, AGREED))
            continue
        verdicts.append((axis, DIVERGED))
        only_expected = [item for item in left if item not in right]
        only_actual = [item for item in right if item not in left]
        divergences.append(
            Divergence(
                axis=axis,
                summary=_describe_axis(axis, only_expected, only_actual),
                expected=tuple(left),
                actual=tuple(right),
            )
        )
    return Comparison(
        expected=expected,
        actual=actual,
        verdicts=tuple(verdicts),
        divergences=tuple(divergences),
    )


def _describe_axis(
    axis: str, only_expected: Sequence[Any], only_actual: Sequence[Any]
) -> str:
    """Name the difference in the terms that axis is about."""

    if axis == AXIS_VERSION:
        return (
            f"the pre-cutover release published "
            f"{only_expected[0]!r} and this one would publish {only_actual[0]!r}"
        )
    label = {
        AXIS_CANDIDATES: "candidate file",
        AXIS_DIGESTS: "digest",
        AXIS_TEMPLATES: "generated file",
        AXIS_PUBLISHERS: "publisher surface",
    }[axis]
    parts = []
    if only_expected:
        names = ", ".join(str(item) for item in only_expected)
        parts.append(f"only the pre-cutover release names {names}")
    if only_actual:
        names = ", ".join(str(item) for item in only_actual)
        parts.append(f"only this release names {names}")
    return f"the two releases disagree on a {label}: " + "; ".join(parts)


@dataclass(frozen=True)
class Canary:
    """What a repeat of an already-performed release did.

    The question a canary answers is narrow and worth stating exactly: did this
    run publish anything? Everything else about it — the stages it walked, the
    reasons it skipped — is in the outcome it was made from. A canary that
    reports "no publication" against a release that already exists is the proof
    that a re-drive, a retry, or a cutover on top of a live release is safe.
    """

    release: str
    version: str
    status: str
    published: bool
    summary: str

    @property
    def ok(self) -> bool:
        """Whether this run may have published nothing and still be healthy.

        A no-op is the pass. A failure is not, whatever it published.
        """

        return not self.published and not self.failed

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": ADOPTION_SCHEMA,
            "release": self.release,
            "version": self.version,
            "status": self.status,
            "published": self.published,
            "ok": self.ok,
            "summary": self.summary,
        }


def canary(outcome: ReleaseOutcome) -> Canary:
    """Read a release outcome as a canary verdict."""

    if outcome.published:
        summary = "this run published, so it is not a canary"
    elif outcome.no_op:
        summary = outcome.no_op_reason or "this release was already performed"
    elif outcome.failed:
        summary = outcome.failure.summary if outcome.failure else "the release failed"
    else:
        summary = (
            f"{outcome.status} with no destination holding a new publication"
        )
    return Canary(
        release=outcome.release,
        version=outcome.version,
        status=outcome.status,
        published=outcome.published,
        summary=summary,
    )


@dataclass(frozen=True)
class Rollback:
    """The commit a consumer's own release writers last shipped from.

    Kept because it is the only way back to a working release once the writers
    are gone. It stops being kept at exactly one moment — the first
    Continuum-managed production release — because after that the release
    history is Continuum's to resume from, and a stale pin is a line somebody
    has to remember to delete.
    """

    repository: str
    sha: str
    superseded_by: str = ""

    def __post_init__(self) -> None:
        if not self.repository:
            raise AdoptionError("a rollback pin needs the repository it belongs to")
        try:
            require_source_sha(self.sha, f"rollback pin for {self.repository}")
        except ContractError as caught:
            raise AdoptionError(str(caught)) from caught

    @property
    def retained(self) -> bool:
        return not self.superseded_by

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": ADOPTION_SCHEMA,
            "repository": self.repository,
            "sha": self.sha,
            "retained": self.retained,
        }
        if self.superseded_by:
            payload["superseded_by"] = self.superseded_by
        return payload


def retention(repository: str, sha: str, *, produced: Sequence[str] = ()) -> Rollback:
    """Pin ``sha`` for ``repository``, released by the first production version named.

    A value, not a decision: whether ``produced`` is telling the truth is
    settled by :func:`require_retirable` against a journal, so a caller that
    claims a release that never happened cannot make the pin disappear.
    """

    first = ""
    for version in produced:
        if not version:
            raise AdoptionError("an empty version cannot release a rollback pin")
        if not first:
            first = version
    return Rollback(repository=repository, sha=sha, superseded_by=first)


def require_retirable(rollback: Rollback, *, journal: Journal = EMPTY_JOURNAL) -> None:
    """Fail unless the rollback pin may be dropped.

    Three conditions, all of them necessary. Something has to have superseded
    the pin, the superseding release has to have run — a real, non-plan run that
    bound that version to a commit — and the caller has to be asking. A pin that
    has not been superseded is the answer, and a superseding release with no
    journal entry behind it is a claim rather than a fact.
    """

    if rollback.retained:
        raise AdoptionError(
            f"{rollback.repository} still keeps the pre-cutover release pin at "
            f"{rollback.sha}: no Continuum-managed production release has replaced it"
        )
    if journal.bound_source(rollback.superseded_by) is None:
        raise AdoptionError(
            f"{rollback.superseded_by} is named as the first Continuum-managed "
            f"production release for {rollback.repository}, but no real run recorded "
            "it; the pre-cutover pin is kept"
        )


def require_retained(rollback: Rollback) -> None:
    """Fail if the rollback pin has already been given up."""

    if not rollback.retained:
        raise AdoptionError(
            f"the pre-cutover pin for {rollback.repository} was released by "
            f"{rollback.superseded_by} and cannot be restored"
        )


__all__ = [
    "ADOPTION_SCHEMA",
    "AGREED",
    "AXES",
    "AXIS_CANDIDATES",
    "AXIS_DIGESTS",
    "AXIS_PUBLISHERS",
    "AXIS_TEMPLATES",
    "AXIS_VERSION",
    "AdoptionError",
    "Canary",
    "Comparison",
    "DEFERRED",
    "DIVERGED",
    "Divergence",
    "Journal",
    "Ledger",
    "OWNED",
    "PRESERVED",
    "Preserved",
    "ReleaseFacts",
    "Rollback",
    "UNEVALUATED",
    "canary",
    "compare",
    "ledger",
    "require_retained",
    "require_retirable",
    "retention",
]