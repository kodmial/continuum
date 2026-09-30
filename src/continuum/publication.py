"""Cutting an immutable SemVer release of Continuum itself.

:mod:`continuum.pin` owns the consumer half of ADR-0002: the one literal
reference a repository holds, and the refusal to let anything else move it. This
module owns the producer half -- what a Continuum release *is*, whether it may be
published at all, and what the published release says about itself.

A Continuum release is not an archive of this module. It is a commit. The
release tag identifies one commit, and that commit supplies every workflow,
action, and script a consumer executes, because
``.github/workflows/consumer.yml`` reaches the rest of the control plane with
same-repository relative references. Publishing therefore carries three
obligations, and each of them is an offline decision a test can drive:

* **The identity is exact.** ``v0.1.0`` -- not ``v0.1``, not ``main``, not a
  prerelease, not a shortened SHA. A moving reference is not a release identity,
  so it is refused here rather than discovered later by a consumer whose
  behaviour changed without anybody editing their repository.
  :func:`require_release_tag`

* **The tag cannot move afterwards.** ADR-0002 makes GitHub immutable releases a
  precondition for treating a SemVer tag as a production dependency identity, so
  a repository that has not enabled them cannot publish one, and a tag that
  already exists is either the same commit or a refusal -- never a new target.
  :func:`require_immutable_releases`, :func:`require_untouched_tag`

* **The release is complete and frozen.** The whole graph reachable from the
  release entrypoint is resolved from that one commit and recorded as a manifest
  with a digest, so "the same complete workflow graph" is a checkable claim
  rather than a promise in prose. :func:`release_graph`, :func:`build_candidate`

Two things are deliberately absent. Nothing here discovers a version, and
nothing here contacts a consumer: a release is cut by naming one, and the notes
say how to adopt it without performing the adoption. Publishing ``v0.1.1`` is
not a statement about ``v0.1.0``, and the module has no operation that could
make it one.

The notes are derived, not written. The configuration-schema and compatibility
sections are computed from what actually changed between the two commits, so a
release cannot be published with a stale or invented compatibility claim, and
the required sections are asserted before the notes are returned rather than
hoped for. :func:`render_notes`
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import pin

#: The one file a consumer names, and therefore the root of a release graph.
RELEASE_ENTRYPOINT = pin.RELEASE_ENTRYPOINT

#: The repository a consumer's ingress names. The engine checkout below is the
#: only place this value is allowed to appear as a revision source.
REPOSITORY = pin.CONTINUUM_REPOSITORY

#: Where the nested control plane lives, and how a release reaches it: a
#: same-repository relative reference, which GitHub resolves from the caller's
#: own commit.
WORKFLOW_PREFIX = "./.github/workflows/"

#: The engine is checked out into the consumer's workspace and used as a local
#: action. The prefix identifies "this reference is the engine that came with
#: this release" as opposed to "this reference names some other file".
ENGINE_PREFIX = "./.continuum-engine/"

#: The only revision expressions a workflow in the release graph may use to
#: resolve Continuum code. Both describe the commit of the workflow file GitHub
#: loaded, which is the commit the consumer's single reference already selected.
RELEASE_SOURCES = ("${{ job.workflow_sha }}", "${{ github.workflow_sha }}")

#: Paths whose change is a change to the configuration schema a consumer writes.
#: The notes name them, because "the config file still loads" is not the same
#: statement as "the file means what it meant".
CONFIG_SURFACE = (
    ".continuum.yml",
    ".github/continuum.yml",
    ".github/fixtures/continuum-config",
    ".github/scripts/continuum_config.py",
    ".github/scripts/test_continuum_config.py",
    "docs/continuum-mvp-contract.md",
    "fixtures/consumer-repo/.continuum.yml",
    "fixtures/consumer-repo/.github/continuum.yml",
    "src/continuum/config.py",
    "src/continuum/yamlmini.py",
)

#: Section headings every published release notes document carries. The question
#: a release exists to answer -- "can I adopt this, and what will it change about
#: my configuration?" -- is only answerable if the answers are there, so they are
#: required rather than recommended.
NOTES_SECTIONS = (
    "## Select this release",
    "## Release contents",
    "## Configuration schema changes",
    "## Compatibility",
    "## Upgrade",
)

#: `uses:` with an optional trailing comment, read as text. The property under
#: test is the graph a reviewer reads in the file, and the same reasoning as
#: :mod:`continuum.pin` applies: a folded scalar or an assembled reference is not
#: something a reviewer's eye treats as a version selection.
_USES = re.compile(r"^\s*uses:\s*(?P<value>[^\s#]+)")

#: A third-party action pinned to a full commit. Continuum's own references are
#: relative by construction, so the only remote references a release graph may
#: contain are other people's code, and they have to be pinned to stay put.
_PINNED_ACTION = re.compile(r"^[^./@][^@]*@[0-9a-f]{40}$")

#: A `ref:` line, for pairing with the `repository:` above it.
_REF_LINE = re.compile(r"^\s*ref:[ \t]*(?P<value>\S+)[ \t]*(?:#.*)?$")

#: A `repository:` line naming Continuum.
_REPOSITORY_LINE = re.compile(r"^\s*repository:\s*(?P<value>\S+)\s*(?:#.*)?$")

#: How far after a Continuum `repository:` its `ref:` may appear. The two keys
#: are adjacent in every workflow that has them, and a bound is what keeps an
#: unbounded scan from pairing one checkout with a later, unrelated `ref:`.
_REF_WINDOW = 6

#: Ways to fetch Continuum source without a `uses:` line. A `uses:` audit is
#: blind to all of them, and every one of them would put a revision outside the
#: release in front of a consumer who pinned `v0.1.0`.
_NETWORK_FETCH = (
    "raw.githubusercontent.com/{0}".format(REPOSITORY),
    "codeload.github.com/{0}".format(REPOSITORY),
    "{0}/archive".format(REPOSITORY),
)

#: A `git clone`, with continuations, so a clone split over several lines is still
#: read as one command.
_CLONE = re.compile(r"git\s+clone[^\n]*(?:\n[ \t]*[^\n]*){0,3}")

#: The consumer configuration document's schema version, read as a literal
#: because only this one key decides whether a consumer's configuration is
#: written against a different schema than the last release.
_CONFIG_VERSION = re.compile(r"^version:[ \t]*(?P<value>\S+)", re.M)

#: A full, unabbreviated commit. An abbreviated one names every commit that
#: shares its prefix for as long as the repository lives.
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: The manifest schema, so an audit of an old release can tell what it was
#: reading without also reading that release's Continuum.
MANIFEST_SCHEMA = "continuum.release-manifest/v1"

#: The manifest filename published with the release.
MANIFEST_NAME = "release-manifest.json"

#: The notes filename published as the release body.
NOTES_NAME = "release-notes.md"

#: How the compatibility level is described, weakest first. A reader who only
#: reads the level still learns whether an existing ingress can be left alone.
COMPATIBILITY_LEVELS = ("compatible", "review", "additive", "breaking")


class PublicationRefusal(RuntimeError):
    """This release may not be published, and the run says which rule stopped it.

    A refusal carries a machine-readable ``reason`` and a human sentence, because
    the operator who dispatched a release and the automation that reports the
    failure are the same audience: the operator needs to know which obligation
    failed and what to do about it.
    """

    def __init__(self, reason: str, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.remediation = remediation


class ReleaseGraph(NamedTuple):
    """The complete set of files one consumer reference selects, and their digests."""

    #: The file every consumer names.
    entrypoint: str
    #: The nested control plane, reachable only by relative reference.
    workflows: Tuple[str, ...]
    #: Engine references, as they appear after the checkout prefix.
    engine_actions: Tuple[str, ...]
    #: Third-party actions, pinned to full commits.
    third_party_actions: Tuple[str, ...]
    #: Every file above, as ``(path, sha256)``, sorted by path.
    files: Tuple[Tuple[str, str], ...]
    #: One digest over the files above.
    digest: str

    def file_digest(self, relative: str) -> str:
        """The digest of one file, or the empty string when it is not in the graph."""
        return dict(self.files).get(relative, "")


class Compatibility(NamedTuple):
    """What adopting this release would change for a consumer that has not moved.

    ``level`` is the single word a skimming reader needs:

    * ``compatible`` -- an existing ingress can stay exactly as it is.
    * ``review`` -- the release entrypoint changed, so the ingress contract has to
      be re-read before upgrading. Not automatically breaking: the entrypoint is
      Continuum's own file, and whether an ingress must change is a property of
      the diff, not of the fact that one exists.
    * ``additive`` -- new surface appeared in the graph; an existing ingress does
      not have to name it.
    * ``breaking`` -- a controller a consumer could reach is no longer reachable
      from the release entrypoint.
    """

    level: str
    #: The sentences that go into the notes, in order.
    findings: Tuple[str, ...]


class ReleaseCandidate(NamedTuple):
    """A validated, publishable-in-principle release, and nothing about the world.

    Building a candidate reads only the trees it is given. Whether it may be
    published is a separate decision -- :func:`decide` -- because that question
    needs facts this repository cannot see for itself: whether immutability is
    enabled, and what the tag already names.
    """

    tag: str
    commit: str
    graph: ReleaseGraph
    #: The git tree of the release commit, when the caller knows it.
    tree: str
    #: The release this one is compared against, empty for a first release.
    previous: str
    #: Configuration-schema files that changed since ``previous``.
    config_changes: Tuple[str, ...]
    compatibility: Compatibility
    notes: str
    manifest: Dict[str, Any]

    @property
    def digest(self) -> str:
        """The manifest digest: what validation produced and publication reuses."""
        return str(self.manifest["digest"])


class ReleaseState(NamedTuple):
    """What the publishing repository looks like from outside."""

    #: GitHub's immutable-releases setting, as ``GET
    #: /repos/{owner}/{repo}/immutable-releases`` reports it. ``None`` is the 404
    #: that endpoint returns when immutability is not enabled.
    immutable_releases: Optional[Mapping[str, Any]]
    #: Published releases, as ``{"tag": ..., "commit": ...}`` records.
    published: Tuple[Mapping[str, Any], ...]
    #: Every existing tag, used to refuse publishing behind an existing release.
    tags: Tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReleaseState":
        """Read the JSON document the release workflow observed."""
        return cls(
            immutable_releases=payload.get("immutable_releases"),
            published=tuple(payload.get("published_releases") or []),
            tags=tuple(payload.get("tags") or []),
        )

    def published_at(self, tag: str) -> Optional[str]:
        """The commit a published release names, or ``None`` if it does not exist."""
        for record in self.published:
            if record.get("tag") == tag:
                return record.get("commit") or ""
        return None


def require_release_tag(tag: str) -> str:
    """Return ``tag`` if it is an exact SemVer release identity.

    The classification is the consumer's own, reused rather than restated: a tag
    Continuum would refuse in ``consumer.yml`` cannot be the right identity to
    publish under either, and two definitions would eventually disagree about
    where the line is.
    """
    if not isinstance(tag, str) or not tag:
        raise PublicationRefusal(
            "version-missing",
            "no release version was given; name the exact SemVer tag to publish, "
            "for example v0.1.0.",
        )
    if tag != tag.strip() or any(character.isspace() for character in tag):
        raise PublicationRefusal(
            "version-malformed",
            "{0!r} is not a release tag: a tag carries no surrounding or "
            "embedded whitespace.".format(tag),
        )
    kind = pin.classify(tag)
    if kind != "release":
        raise PublicationRefusal(
            "version-not-exact",
            "refusing to publish Continuum {0!r}: {1}. ADR-0002 makes an exact "
            "SemVer release tag the production dependency identity, and every "
            "other reference names more than one commit.".format(tag, kind),
            "Publish an exact release such as v0.1.0, or v0.1.1.",
        )
    return tag


def require_release_commit(commit: str) -> str:
    """Return ``commit`` if it is a full commit SHA.

    The manifest names the commit the release is, and an abbreviated SHA names
    every commit sharing its prefix for as long as the repository lives.
    """
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        raise PublicationRefusal(
            "commit-not-exact",
            "the release commit must be a full 40-character commit SHA, not "
            "{0!r}.".format(commit),
        )
    return commit


def _semver_key(value: str) -> Tuple[int, int, int]:
    core = value[1:] if value.startswith("v") else value
    major, _, rest = core.partition(".")
    minor, _, patch = rest.partition(".")
    return int(major), int(minor), int(patch)


def exact_release_tags(tags: Iterable[str]) -> List[str]:
    """The tags that are exact SemVer releases, newest first.

    Continuum publishes no prereleases as a production dependency contract, so
    a tag such as ``v0.2.0-rc1`` is not a release and is not a release
    predecessor. Selecting a predecessor has to apply the same rule everywhere it
    is done, which is why the selection lives here rather than in a shell
    pipeline that a sort flag decides.
    """
    return sorted(
        (tag for tag in tags if pin.is_exact_release(tag)), key=_semver_key, reverse=True
    )


def require_previous(previous_tag: str, tags: Sequence[str], tag: str) -> str:
    """Validate the release this one is compared against, and return it.

    The comparison target is chosen by the caller, which is a shell pipeline
    rather than this module, so it is cross-checked here: a comparison against
    anything other than the immediate predecessor would make the compatibility
    section describe a version that is not the one being replaced. Two readers
    that disagree must fail the release rather than publish notes derived from
    the wrong pair of commits.
    """
    published = [
        value
        for value in exact_release_tags(tags)
        if value != tag and _semver_key(value) < _semver_key(tag)
    ]
    if not published:
        if previous_tag:
            if not tags:
                raise PublicationRefusal(
                    "previous-unverified",
                    "{0!r} was offered as the release {1} replaces, but no tags "
                    "were supplied to check it against.".format(previous_tag, tag),
                    "Pass --tags-file with the repository's tags, so the "
                    "comparison cannot be made against the wrong release.",
                )
            raise PublicationRefusal(
                "previous-not-a-release",
                "{0} has no earlier release to be compared against, so {1!r} "
                "cannot be its predecessor.".format(tag, previous_tag),
                "Compare against the previous release tag, or pass none for the "
                "first release.",
            )
        return ""
    newest = published[0]
    if not previous_tag:
        raise PublicationRefusal(
            "previous-missing",
            "{0} replaces {1}, but no previous release was named, so the "
            "compatibility section would describe nothing.".format(tag, newest),
            "Compare against {0}.".format(newest),
        )
    if not pin.is_exact_release(previous_tag):
        raise PublicationRefusal(
            "previous-not-exact",
            "the previous release must be an exact SemVer tag, not {0!r}.".format(previous_tag),
            "Compare against the release this one replaces, for example v0.1.0.",
        )
    if previous_tag != newest:
        raise PublicationRefusal(
            "previous-mismatch",
            "{0} is not the release {1} replaces: {2} is.".format(
                previous_tag, tag, newest
            ),
            "Compare against {0}.".format(newest),
        )
    return previous_tag


def require_forward(tags: Iterable[str], tag: str) -> None:
    """Refuse to publish ``tag`` behind a release that already exists.

    A dependency version that goes backwards is a dependency identity two
    consumers can disagree about, so it is refused at publication time rather
    than discovered by whichever consumer adopts it last.
    """
    published = [value for value in tags if pin.is_exact_release(value)]
    if not published:
        return
    newest = max(published, key=_semver_key)
    if _semver_key(tag) < _semver_key(newest):
        raise PublicationRefusal(
            "release-goes-backwards",
            "refusing to publish {0}: {1} is already published and is newer. "
            "Continuum does not publish a release older than one it has "
            "published.".format(tag, newest),
            "Publish a version greater than {0}.".format(newest),
        )


def require_immutable_releases(state: ReleaseState) -> None:
    """Refuse unless GitHub immutable releases are enabled for this repository.

    ADR-0002 makes immutability a precondition, not an improvement: without it
    the tag is a pointer a maintainer can move, and a consumer holding
    ``@v0.1.0`` would then execute a commit nobody selected. Publishing without
    the setting would be a release that has already broken the contract it was
    published under, so this refuses rather than warns.
    """
    setting = state.immutable_releases
    if setting and setting.get("enabled"):
        return
    owner, _, name = REPOSITORY.partition("/")
    raise PublicationRefusal(
        "immutable-releases-disabled",
        "immutable releases are not enabled for {0}, so a release tag published "
        "now could be moved afterwards, and a consumer pinned to it would "
        "execute a commit nobody selected. ADR-0002 makes them a precondition "
        "for treating a SemVer tag as a production dependency identity.".format(
            REPOSITORY
        ),
        (
            "Enable release immutability for the repository -- Settings -> "
            "Releases -> Enable release immutability, or "
            "PUT /repos/{0}/{1}/immutable-releases with an admin token -- then "
            "dispatch this release again.".format(owner, name)
        ),
    )


def require_untouched_tag(state: ReleaseState, tag: str, commit: str) -> str:
    """Return ``publish`` or ``already-published``; never move an existing tag.

    A re-dispatch of the same release is a no-op rather than a new release: the
    tag and its commit already agree, and re-running a release must not be able
    to change what a consumer executes. A tag that names a *different* commit is
    a refusal, because the only way to satisfy it would be to move the tag.
    """
    existing = state.published_at(tag)
    if existing is not None:
        if not existing:
            raise PublicationRefusal(
                "tag-target-unknown",
                "{0} already has a published release, but the commit its tag "
                "names could not be read. Agreement cannot be assumed for a tag "
                "nobody could resolve.".format(tag),
                "Resolve the tag's commit -- fetch the tags -- and dispatch the "
                "release again.",
            )
        if existing != commit:
            raise PublicationRefusal(
                "tag-already-published",
                "{0} is already published at {1}. A published release tag is "
                "immutable: it cannot be moved, and moving it would change what "
                "every consumer pinned to {0} executes.".format(tag, existing),
                "Publish a new release tag for commit {0}.".format(commit),
            )
        return "already-published"
    if tag in state.tags:
        raise PublicationRefusal(
            "tag-already-exists",
            "the tag {0} already exists and has no published release. Moving it "
            "to {1} would silently redefine a version a consumer may already "
            "have selected.".format(tag, commit),
            "Delete the unused tag, or publish a new release tag for {0}.".format(commit),
        )
    return "publish"


#: Files a checkout can contain that no release contains. A git checkout has
#: none of them, and a manifest is a statement about the release commit, so a
#: locally generated bytecode cache must not appear in one.
_NOT_RELEASED = ("__pycache__",)
_NOT_RELEASED_SUFFIXES = (".pyc", ".pyo")


def _is_released(relative: str) -> bool:
    """Whether a repository-relative path is part of what a release ships."""
    if relative.endswith(_NOT_RELEASED_SUFFIXES):
        return False
    return not any(part in _NOT_RELEASED for part in relative.split("/"))


def _digest_of(pairs: Sequence[Tuple[str, str]]) -> str:
    """One digest over ``(name, hex)`` pairs, order-independent by construction."""
    accumulator = hashlib.sha256()
    for name, value in sorted(pairs):
        accumulator.update(name.encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(value.encode("utf-8"))
        accumulator.update(b"\n")
    return accumulator.hexdigest()


def _sha256(path: pathlib.Path) -> str:
    accumulator = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            accumulator.update(block)
    return accumulator.hexdigest()


def _code_lines(text: str) -> List[str]:
    """The file's lines with whole-line comments removed.

    The release entrypoint documents the consumer's one reference in a comment,
    and that documentation is not a reference. Reading comments as code would
    make the file's own explanation look like a second version selection.
    """
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _references(text: str) -> List[str]:
    """Every ``uses:`` value in ``text``, in order."""
    found: List[str] = []
    for line in _code_lines(text):
        match = _USES.match(line)
        if match is not None:
            found.append(match.group("value"))
    return found


def _fetch_attempts(text: str) -> List[str]:
    """Ways this file could pull Continuum source from outside the release."""
    body = "\n".join(_code_lines(text))
    attempts = [needle for needle in _NETWORK_FETCH if needle in body]
    for match in _CLONE.finditer(body):
        if REPOSITORY in match.group(0):
            attempts.append(match.group(0).splitlines()[0].strip())
    return attempts


def _continuum_checkout_refs(text: str) -> List[str]:
    """The ``ref:`` values paired with a checkout of Continuum in ``text``."""
    lines = _code_lines(text)
    found: List[str] = []
    for index, line in enumerate(lines):
        repository = _REPOSITORY_LINE.match(line)
        if repository is None or repository.group("value") != REPOSITORY:
            continue
        for follower in lines[index + 1 : index + 1 + _REF_WINDOW]:
            reference = _REF_LINE.match(follower)
            if reference is not None:
                found.append(reference.group("value"))
                break
    return found


def release_graph(root: "os.PathLike[str] | str") -> ReleaseGraph:
    """Resolve the whole graph one consumer reference selects, from ``root``.

    The closure starts at the release entrypoint and follows relative references
    only, because that is the only reference kind GitHub resolves from the
    caller's commit. Anything the graph reaches that is *not* in the release tree
    is a refusal: the graph is either closed, or it is not a release.
    """
    base = pathlib.Path(root)
    entry = base / RELEASE_ENTRYPOINT
    if not entry.is_file():
        raise PublicationRefusal(
            "entrypoint-missing",
            "{0} does not contain the release entrypoint {1}, so there is no "
            "release to publish.".format(base, RELEASE_ENTRYPOINT),
            "A release is the tree that contains {0}; publish from a commit that "
            "has it.".format(RELEASE_ENTRYPOINT),
        )

    workflows: List[str] = []
    pending = [RELEASE_ENTRYPOINT]
    seen = set()
    engine_actions: List[str] = []
    third_party: List[str] = []
    files: Dict[str, str] = {}
    bad_checkouts: Dict[str, List[str]] = {}
    fetch_attempts: Dict[str, List[str]] = {}

    while pending:
        relative = pending.pop(0)
        if relative in seen:
            continue
        seen.add(relative)
        workflows.append(relative)
        path = base / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublicationRefusal(
                "graph-incomplete",
                "the release graph names {0}, which cannot be read: {1}".format(relative, exc),
                "A release ships every workflow it reaches; restore the file in "
                "the release commit.",
            )
        files[relative] = _sha256(path)

        for value in _references(text):
            if value.startswith(WORKFLOW_PREFIX):
                target = value[2:]
                if "@" in target:
                    raise PublicationRefusal(
                        "graph-not-closed",
                        "{0} reaches {1}, which appends a revision to a "
                        "relative reference. A relative reference is already the "
                        "commit the consumer's single reference selected, so "
                        "naming another one opens a second selection.".format(
                            relative, value
                        ),
                        "Call {0} with no revision.".format(target),
                    )
                if not (base / target).is_file():
                    raise PublicationRefusal(
                        "graph-incomplete",
                        "{0} calls {1}, which is not a file in this release.".format(relative, value),
                        "A release is the whole reachable graph; include the file "
                        "in the release commit.",
                    )
                pending.append(target)
            elif value.startswith(ENGINE_PREFIX):
                engine_actions.append(value[len(ENGINE_PREFIX) :])
            elif _PINNED_ACTION.match(value):
                third_party.append(value)
            else:
                raise PublicationRefusal(
                    "graph-not-closed",
                    "{0} reaches {1}, which names a revision of its own. Only "
                    "relative references and full-commit-pinned third-party "
                    "actions are allowed in a release graph.".format(relative, value),
                    "Call another controller by relative reference, or pin a "
                    "third-party action to a full commit.",
                )

        for ref in _continuum_checkout_refs(text):
            if ref not in RELEASE_SOURCES:
                bad_checkouts.setdefault(relative, []).append(ref)
        for attempt in _fetch_attempts(text):
            fetch_attempts.setdefault(relative, []).append(attempt)

    if bad_checkouts:
        detail = "; ".join(
            "{0} -> {1}".format(name, ", ".join(refs)) for name, refs in sorted(bad_checkouts.items())
        )
        raise PublicationRefusal(
            "engine-pinned-independently",
            "a workflow in the release graph checks Continuum out at a revision of "
            "its own ({0}). The engine must come from the commit the consumer's "
            "single reference selected.".format(detail),
            "Use ${{{{ job.workflow_sha }}}} for the engine checkout ref.",
        )
    if fetch_attempts:
        detail = "; ".join(
            "{0} -> {1}".format(name, ", ".join(attempts))
            for name, attempts in sorted(fetch_attempts.items())
        )
        raise PublicationRefusal(
            "graph-escapes-release",
            "the release graph fetches Continuum from outside the release ({0}). A "
            "consumer pinned to this release would execute a commit nobody "
            "selected.".format(detail),
            "Use the engine checkout the workflow already makes, at "
            "${{{{ job.workflow_sha }}}}.",
        )

    # The engine is used as a local action out of the checkout the graph just
    # verified, so the action's own bytes are part of the release. GitHub
    # resolves `./.continuum-engine/.github/actions/engine-path` to the
    # `action.yml` inside it, so the reference names a directory and every file
    # under it ships with the release.
    for action in sorted(set(engine_actions)):
        action_root = base / action
        if not action_root.exists():
            raise PublicationRefusal(
                "graph-incomplete",
                "the release graph uses the engine action {0}, which is not in "
                "this release.".format(action),
                "The engine ships with the release; include the action in the "
                "release commit.",
            )
        members = [action_root] if action_root.is_file() else sorted(action_root.rglob("*"))
        for member in members:
            if not member.is_file():
                continue
            relative = member.relative_to(base).as_posix()
            if not _is_released(relative):
                continue
            files[relative] = _sha256(member)

    pairs = tuple(sorted(files.items()))
    return ReleaseGraph(
        entrypoint=RELEASE_ENTRYPOINT,
        workflows=tuple(sorted(workflows)),
        engine_actions=tuple(sorted(set(engine_actions))),
        third_party_actions=tuple(sorted(set(third_party))),
        files=pairs,
        digest=_digest_of(pairs),
    )


def config_schema_changes(changed: Iterable[str]) -> Tuple[str, ...]:
    """The configuration-schema files ``changed`` touches.

    A surface is matched as a directory prefix, because the schema is spread over
    a document, a loader, and the fixtures that prove the loader accepts it. Any
    of the three moving is a change a consumer's configuration may care about.
    """
    touches = set()
    for path in changed:
        normalized = str(path).replace(os.sep, "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        for surface in CONFIG_SURFACE:
            if normalized == surface or normalized.startswith(surface.rstrip("/") + "/"):
                touches.add(normalized)
    return tuple(sorted(touches))


def config_version(root: "os.PathLike[str] | str") -> str:
    """The ``version:`` a repository configuration declares, or the empty string."""
    document = pathlib.Path(root) / ".continuum.yml"
    if not document.is_file():
        return ""
    match = _CONFIG_VERSION.search(document.read_text(encoding="utf-8"))
    return match.group("value") if match else ""


def _strongest(levels: Iterable[str]) -> str:
    ranked = [level for level in levels if level in COMPATIBILITY_LEVELS]
    return max(ranked, key=COMPATIBILITY_LEVELS.index) if ranked else "compatible"


def compare_compatibility(
    *,
    tag: str,
    graph: ReleaseGraph,
    previous: Optional[ReleaseGraph],
    previous_tag: str = "",
) -> Compatibility:
    """Classify what this release would change for a consumer that has not moved.

    The classification is a file-level statement, and deliberately so: the graph
    is hashed rather than parsed, so the claim is one about bytes. A
    byte-identical release entrypoint means a byte-identical set of declared
    inputs, and the declared inputs are the whole of what a consumer's generated
    ingress has to satisfy.
    """
    if previous is None:
        return Compatibility(
            level="compatible",
            findings=(
                "First Continuum release: there is no earlier release to compare "
                "against, so no consumer compatibility claim can be made.",
                "A consumer adopts this release by changing one reference in its "
                "generated ingress. Nothing else in its repository moves.",
            ),
        )

    findings: List[str] = []
    levels: List[str] = []
    compare = "https://github.com/{0}/compare/{1}...{2}".format(
        REPOSITORY, previous_tag or "HEAD", tag
    )

    if graph.file_digest(graph.entrypoint) == previous.file_digest(graph.entrypoint):
        levels.append("compatible")
        findings.append(
            "The release entrypoint is byte-identical to {0}, so every existing "
            "consumer ingress remains valid without an edit.".format(
                previous_tag or "the previous release"
            )
        )
    else:
        levels.append("review")
        findings.append(
            "The release entrypoint changed since {0} ({1}). A consumer's ingress "
            "declares the events it wakes on and the one release reference, so a "
            "new required input, a renamed input, or a changed default is "
            "consumer-visible: review {2} before upgrading.".format(
                previous_tag or "the previous release", compare, compare
            )
        )

    added = sorted(set(graph.workflows) - set(previous.workflows))
    removed = sorted(set(previous.workflows) - set(graph.workflows))
    for name in removed:
        levels.append("breaking")
        findings.append(
            "{0} is no longer reachable from the release entrypoint. A consumer "
            "pinned to this release loses that controller, and its ingress still "
            "dispatches into it.".format(name)
        )
    for name in added:
        levels.append("additive")
        findings.append(
            "{0} is new in the release graph. An existing ingress does not have "
            "to name it, and the entrypoint routes to it without a consumer "
            "edit.".format(name)
        )
    if not added and not removed:
        findings.append(
            "The control plane a consumer can reach is unchanged: {0} workflows, "
            "all resolved from this commit.".format(len(graph.workflows))
        )
    if not findings:
        findings.append("No compatibility change was found against {0}.".format(previous_tag))

    return Compatibility(level=_strongest(levels), findings=tuple(findings))


def build_candidate(
    *,
    tag: str,
    commit: str,
    root: "os.PathLike[str] | str",
    previous_root: "os.PathLike[str] | str | None" = None,
    previous_tag: str = "",
    tags: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    tree: str = "",
) -> ReleaseCandidate:
    """Validate the release and derive everything the publication will state.

    Reads two trees at most and touches no network. The refusals are the ones
    that would publish a release no consumer could trust: a version that is not
    an exact SemVer tag, a commit that is not a full SHA, a comparison against
    something other than the release being replaced, and a graph that reaches
    outside the release.
    """
    release_tag = require_release_tag(tag)
    release_commit = require_release_commit(commit)
    previous_tag = require_previous(previous_tag, tags, release_tag)
    graph = release_graph(root)
    previous = release_graph(previous_root) if previous_root else None

    config_changes = config_schema_changes(changed_paths)
    before = config_version(previous_root) if previous_root else ""
    after = config_version(root)
    if before and after and before != after:
        config_changes = tuple(sorted(set(config_changes) | {".continuum.yml"}))

    compatibility = compare_compatibility(
        tag=release_tag, graph=graph, previous=previous, previous_tag=previous_tag
    )
    return ReleaseCandidate(
        tag=release_tag,
        commit=release_commit,
        graph=graph,
        tree=tree,
        previous=previous_tag,
        config_changes=config_changes,
        compatibility=compatibility,
        notes=render_notes(
            tag=release_tag,
            commit=release_commit,
            graph=graph,
            tree=tree,
            previous=previous_tag,
            config_changes=config_changes,
            compatibility=compatibility,
            config_versions=(before, after),
        ),
        manifest=render_manifest(
            tag=release_tag,
            commit=release_commit,
            graph=graph,
            tree=tree,
            previous=previous_tag,
        ),
    )


def render_manifest(
    *, tag: str, commit: str, graph: ReleaseGraph, tree: str = "", previous: str = ""
) -> Dict[str, Any]:
    """The machine-readable statement of what this release contains."""
    return {
        "schema": MANIFEST_SCHEMA,
        "repository": REPOSITORY,
        "release": tag,
        "commit": commit,
        "tree": tree,
        "previous": previous,
        "entrypoint": graph.entrypoint,
        "workflows": list(graph.workflows),
        "engine_actions": list(graph.engine_actions),
        "third_party_actions": list(graph.third_party_actions),
        "files": {name: value for name, value in graph.files},
        "digest": graph.digest,
    }


def render_notes(
    *,
    tag: str,
    commit: str,
    graph: ReleaseGraph,
    tree: str = "",
    previous: str = "",
    config_changes: Sequence[str] = (),
    compatibility: Compatibility,
    config_versions: Tuple[str, str] = ("", ""),
) -> str:
    """Render the notes a published release carries.

    Every section in :data:`NOTES_SECTIONS` is always present, including when the
    answer is "nothing changed". A section that appears only when there is
    something to say is a section a reader cannot rely on finding.
    """
    before, after = config_versions
    lines: List[str] = [
        "# Continuum {0}".format(tag),
        "",
        "One consumer reference selects this whole release:",
        "",
        "```yaml",
        "jobs:",
        "  continuum:",
        "    uses: {0}/{1}@{2}".format(REPOSITORY, RELEASE_ENTRYPOINT, tag),
        "    secrets: inherit",
        "```",
        "",
        "`{0}` is the release entrypoint. Every controller it reaches -- "
        "scheduler, agent, repair, review, merge, delegated child work -- is called "
        "with a same-repository relative reference, so GitHub resolves all of them "
        "from the commit this tag names. The engine the agent runs is checked out "
        "from that same commit and verified against it. Nothing inside this release "
        "resolves a revision of its own, which is what makes this release the same "
        "complete workflow graph after `main` advances and after the next release is "
        "published.".format(RELEASE_ENTRYPOINT),
        "",
        "## Select this release",
        "",
        "- tag: `{0}`".format(tag),
        "- commit: `{0}`".format(commit),
        "- entrypoint: `{0}`".format(graph.entrypoint),
        "- graph digest: `{0}`".format(graph.digest),
        "- previous release: {0}".format(
            "`{0}`".format(previous) if previous else "none (first release)"
        ),
        "",
        "## Release contents",
        "",
        "These {0} workflows are the entire control plane, and each is resolved from "
        "the commit above:".format(len(graph.workflows)),
        "",
    ]
    lines.extend("- `{0}`".format(name) for name in graph.workflows)
    lines.append("")
    if graph.engine_actions:
        lines.append("Engine action, used out of the checkout verified against that commit:")
        lines.append("")
        lines.extend("- `{0}`".format(name) for name in graph.engine_actions)
        lines.append("")
    if graph.third_party_actions:
        lines.append("Third-party actions, each pinned to a full commit:")
        lines.append("")
        lines.extend("- `{0}`".format(name) for name in graph.third_party_actions)
        lines.append("")
    if tree:
        lines.append("Git tree of the release commit: `{0}`.".format(tree))
        lines.append("")
    lines.append("`{0}` carries the same list with a SHA-256 per file.".format(MANIFEST_NAME))
    lines.append("")
    lines.append("## Configuration schema changes")
    lines.append("")
    if before and after and before != after:
        lines.append(
            "**The repository configuration schema changed: `version:` moved from "
            "`{0}` to `{1}`.**".format(before, after)
        )
        lines.append("")
    if config_changes:
        lines.append("These configuration files changed since the previous release:")
        lines.append("")
        lines.extend("- `{0}`".format(name) for name in config_changes)
        lines.append("")
        lines.append(
            "Read the contract before upgrading. A consumer's own configuration is "
            "not rewritten by this release, and a configuration that no longer "
            "matches the schema is refused by the release's own validation rather "
            "than silently reinterpreted."
        )
    else:
        lines.append(
            "None. The configuration schema is unchanged, so a consumer's "
            "`.github/continuum.yml` and `.continuum.yml` mean what they meant."
        )
    lines.append("")
    lines.append("## Compatibility")
    lines.append("")
    lines.append("Level: **{0}**".format(compatibility.level))
    lines.append("")
    for finding in compatibility.findings:
        lines.append("- {0}".format(finding))
    lines.append("")
    lines.append(
        "Publishing this release wrote nothing in any consumer repository. Adoption "
        "is an explicit change to one reference in a consumer's own ingress."
    )
    lines.append("")
    lines.append("## Upgrade")
    lines.append("")
    lines.append(
        "A consumer moves to this release by changing the one reference in its "
        "generated ingress, then verifying it:"
    )
    lines.append("")
    lines.append("```sh")
    lines.append("continuum release pin verify --ingress .github/workflows/continuum.yml")
    lines.append("```")
    lines.append("")
    lines.append(
        "Rollback is the same operation in reverse: restore the previous exact "
        "release tag. Neither direction runs automatically, and publishing this "
        "release did not modify any consumer."
    )
    lines.append("")

    notes = "\n".join(lines).rstrip("\n") + "\n"
    missing = [section for section in NOTES_SECTIONS if section not in notes]
    if missing:
        # Unreachable by construction, and asserted anyway: these sections are the
        # release's answer to "can I adopt this?", so a rendering change that drops
        # one has to fail here rather than in a published release.
        raise PublicationRefusal(
            "notes-incomplete",
            "the rendered notes are missing {0}.".format(", ".join(missing)),
        )
    return notes


def decide(candidate: ReleaseCandidate, state: ReleaseState) -> str:
    """Return the publication action, or refuse with the rule that stopped it."""
    require_immutable_releases(state)
    require_forward(state.tags, candidate.tag)
    return require_untouched_tag(state, candidate.tag, candidate.commit)


def manifest_document(candidate: ReleaseCandidate, decision: str) -> str:
    """The manifest as published, including the decision it was made under."""
    document = dict(candidate.manifest)
    document["decision"] = decision
    document["compatibility"] = candidate.compatibility.level
    document["config_schema_changes"] = list(candidate.config_changes)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
