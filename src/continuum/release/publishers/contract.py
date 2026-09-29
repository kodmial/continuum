"""What a downstream package publisher is given, and what it has to say.

A publisher runs *after* a release exists. It never builds anything, never
re-signs anything, and never asks the release pipeline for a favour: it is
handed the identity of what was published, the immutable URLs it was published
at, and the digests those URLs are supposed to serve, and it turns those into a
package manager's own description of the release. Everything in this module is
that handover, and nothing in it belongs to any particular package manager.

Three properties are enforced here rather than in the publishers, because
getting them wrong is the same mistake whichever package manager is involved:

* **The release is identified by an immutable URL.** `ReleaseIdentity` refuses
  a download base that does not end in the tag it claims to describe. A URL
  that follows a moving ref produces a formula that resolves to a different
  artifact the moment anything is re-released, and the digest check that
  follows would then fail on a file that was never wrong.
* **A digest is a claim about bytes.** `PublishedArtifact` carries the digest
  the release pipeline recorded, and a publisher that cannot obtain a matching
  asset reports `digest-mismatch` rather than writing the digest it was given
  into a manifest. The interesting failure is not the wrong digest; it is a
  digest that was never checked.
* **"Skipped" and "failed" are different answers.** A publisher that was not
  asked to do anything, or that found its destination already correct, has
  succeeded. A publisher that could not prove what it was about to write has
  not, and says which of the two happened with a stable reason code.

The reason codes are part of the contract rather than of any one publisher,
because the caller that reacts to them — a workflow deciding whether to retry —
must not have to know which package manager produced one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from ..plan import ReleaseError

PUBLISHER_RESULT_SCHEMA = "continuum.publisher-result/v1"

STATUS_PUBLISHED = "published"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
SUPPORTED_STATUSES = (STATUS_PUBLISHED, STATUS_SKIPPED, STATUS_FAILED)

# A destination is written one of two ways. `trusted` commits straight to the
# destination's branch, which is only appropriate where the credential is the
# owner of that repository. `pull-request` lands the change on a feature branch
# and opens a pull request, so the destination's own review — and its own
# required checks — still run.
MODE_TRUSTED = "trusted"
MODE_PULL_REQUEST = "pull-request"
SUPPORTED_UPDATE_MODES = (MODE_TRUSTED, MODE_PULL_REQUEST)

# Artifact kinds. A package manager's "source archive" and its "application
# bundle" are different downloads with different consumers, and the distinction
# is what lets the Homebrew formula and the Homebrew cask be enabled
# independently: a release that ships no bundle has a formula and no cask.
KIND_ARCHIVE = "archive"
KIND_APP_BUNDLE = "app-bundle"
SUPPORTED_ARTIFACT_KINDS = (KIND_ARCHIVE, KIND_APP_BUNDLE)

# Architectures are package-manager-neutral vocabulary: a tap and a port tree
# both need to say which slice of a release a URL and a checksum belong to.
ARCH_ARM64 = "arm64"
ARCH_X86_64 = "x86_64"
SUPPORTED_ARCHITECTURES = (ARCH_ARM64, ARCH_X86_64)

# -- reason codes -----------------------------------------------------------

# The reason a publisher gave. A caller branches on these, never on prose, and
# a code that is not retryable means a retry will produce the same answer.
PUBLISHED = "published"
ALREADY_CURRENT = "already-current"
DISABLED = "disabled"
SURFACE_DISABLED = "surface-disabled"
DRY_RUN = "dry-run"
NO_ARTIFACT = "no-matching-artifact"
DIGEST_MISMATCH = "digest-mismatch"
ASSET_UNREACHABLE = "asset-unreachable"
STALE_DESTINATION = "stale-destination"
CREDENTIAL_MISSING = "credential-missing"
TEMPLATE_UNKNOWN_TOKEN = "template-token-unknown"
TEMPLATE_UNFILLED_TOKEN = "template-token-unfilled"
VALIDATION_FAILED = "validation-failed"
VALIDATION_UNAVAILABLE = "validation-unavailable"
QUARANTINE_POLICY = "quarantine-policy"
CONFIGURATION_INVALID = "configuration-invalid"
DESTINATION_UNREACHABLE = "destination-unreachable"

# Retrying is only honest for a failure that is about the world rather than
# about the request. A moved destination HEAD, an asset that did not download,
# and a destination that could not be reached are all worth a second attempt; a
# digest that does not match, a template with an unknown token, and a missing
# credential are not, and a workflow that retries those just burns time while
# the same wrong answer comes back.
RETRYABLE_REASONS = frozenset(
    {
        STALE_DESTINATION,
        ASSET_UNREACHABLE,
        DESTINATION_UNREACHABLE,
    }
)

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")


class PublisherError(ReleaseError):
    """A publisher cannot honestly report a result.

    Carries the same three things a step failure does — a stable `code`, a
    message safe to print, and something the reader can do about it — plus
    whether a second attempt could plausibly succeed. A publisher raises this
    for a condition it has itself classified, and the caller turns it into a
    failed result; anything a publisher cannot classify is a bug and keeps the
    plain exception.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
        remediation: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.retryable = RETRYABLE_REASONS.__contains__(code) if retryable is None else retryable

    def describe(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "retryable": self.retryable,
        }


def is_retryable(code: str) -> bool:
    return code in RETRYABLE_REASONS


def architecture_token(architecture: str) -> str:
    """The token suffix an architecture contributes to a template.

    `x86_64` and `x86-64` are the same slice of a release under two spellings, so
    the token is derived rather than spelled: a template that asked for
    `__SHA256_X86_64__` and a manifest that says `x86-64` are the same thing, and
    a publisher that treated them as different would report a missing artifact
    for an artifact it is holding.
    """

    return (architecture or "").upper().replace("-", "_").replace(".", "_")


@dataclass(frozen=True)
class ReleaseIdentity:
    """Which project, which release, and the one URL that will always serve it.

    `download_base` is the immutable prefix the release's assets hang off — a
    tag-pinned download location, never a `latest` alias. It is required to end
    in `tag`, because that is the property the whole downstream chain rests on:
    a formula whose URL is not pinned to a tag is a formula that will describe a
    different file after the next release, and its recorded checksum will be
    rejected by every user who installs after that.
    """

    repository: str
    version: str
    tag: str = ""
    download_base: str = ""

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.match(self.repository or ""):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"release repository must be 'owner/name'; got {self.repository!r}",
            )
        if not _VERSION_RE.match(self.version or ""):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"release version must be a bare version token; got {self.version!r}",
            )
        if not self.tag:
            object.__setattr__(self, "tag", f"v{self.version}")
        if any(char in self.tag for char in "\n\r/ "):
            raise PublisherError(
                CONFIGURATION_INVALID, f"release tag must be a single path segment; got {self.tag!r}"
            )
        if not self.download_base:
            raise PublisherError(
                CONFIGURATION_INVALID,
                "a release download base is required: a publisher writes absolute "
                "URLs into a manifest, and a manifest cannot be regenerated later "
                "from a moving reference",
            )
        if not self.download_base.startswith("https://"):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"release download base must be https; got {self.download_base!r}",
            )
        if not self.download_base.rstrip("/").endswith(self.tag):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"release download base must end in the tag {self.tag!r} so the URL a "
                f"manifest records stays the same after the next release; got "
                f"{self.download_base!r}",
            )

    def asset_url(self, name: str) -> str:
        if not name or any(char in name for char in "\n\r/ "):
            raise PublisherError(
                CONFIGURATION_INVALID, f"asset name must be a single file name; got {name!r}"
            )
        return f"{self.download_base.rstrip('/')}/{name}"

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repository.split("/", 1)[1]

    def describe(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "version": self.version,
            "tag": self.tag,
            "download_base": self.download_base,
        }


@dataclass(frozen=True)
class PublishedArtifact:
    """One asset of a published release: where it is, and what it must hash to.

    `architecture` is empty for an asset that is not per-architecture, which is
    how a single universal download is expressed without inventing an
    architecture name for it.
    """

    name: str
    url: str
    sha256: str
    kind: str = KIND_ARCHIVE
    architecture: str = ""
    size: int = 0

    def __post_init__(self) -> None:
        if not self.name or any(char in self.name for char in "\n\r/ "):
            raise PublisherError(
                CONFIGURATION_INVALID, f"artifact name must be a file name; got {self.name!r}"
            )
        if not self.url.startswith("https://"):
            raise PublisherError(
                CONFIGURATION_INVALID, f"artifact url must be https; got {self.url!r}"
            )
        if not _SHA256_RE.match(self.sha256 or ""):
            raise PublisherError(
                CONFIGURATION_INVALID,
                "artifact sha256 must be 64 lower-case hex characters; a manifest that "
                "cannot state a digest cannot be verified before it is published",
            )
        if self.kind not in SUPPORTED_ARTIFACT_KINDS:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"artifact kind must be one of {', '.join(SUPPORTED_ARTIFACT_KINDS)}; "
                f"got {self.kind!r}",
            )
        if self.architecture and self.architecture not in SUPPORTED_ARCHITECTURES:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"artifact architecture must be one of "
                f"{', '.join(SUPPORTED_ARCHITECTURES)} or empty; got {self.architecture!r}",
            )
        if self.size < 0:
            raise PublisherError(
                CONFIGURATION_INVALID, f"artifact size cannot be negative; got {self.size}"
            )

    @property
    def token_architecture(self) -> str:
        return architecture_token(self.architecture)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "sha256": self.sha256,
            "kind": self.kind,
            "architecture": self.architecture,
            "size": self.size,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    """Everything a release published, indexed the way a package manager asks.

    A manifest is the only source of digests a publisher is allowed to use. It
    is never computed from a rebuild, because a rebuild is a different file
    with a plausible name, and a manifest that pins a checksum nobody downloaded
    is a manifest that breaks on the user's machine instead of in CI.
    """

    release: ReleaseIdentity
    artifacts: Tuple[PublishedArtifact, ...] = ()

    def __post_init__(self) -> None:
        names = [artifact.name for artifact in self.artifacts]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"artifact manifest lists {', '.join(duplicates)} twice; a release that "
                "publishes the same name twice cannot be described by a package manager",
            )

    def select(self, kind: str) -> Tuple[PublishedArtifact, ...]:
        return tuple(artifact for artifact in self.artifacts if artifact.kind == kind)

    def architecture_names(self, kind: str) -> Tuple[str, ...]:
        """The architectures a kind was published for, in manifest order."""

        found: list = []
        for artifact in self.select(kind):
            if artifact.architecture and artifact.architecture not in found:
                found.append(artifact.architecture)
        return tuple(found)

    def for_architecture(self, kind: str, architecture: str) -> Optional[PublishedArtifact]:
        for artifact in self.select(kind):
            if artifact.architecture == architecture:
                return artifact
        return None

    def unversioned(self, kind: str) -> Optional[PublishedArtifact]:
        """The single asset of a kind that is not split by architecture."""

        candidates = [artifact for artifact in self.select(kind) if not artifact.architecture]
        return candidates[0] if len(candidates) == 1 else None

    def digests(self) -> Tuple[str, ...]:
        return tuple(artifact.sha256 for artifact in self.artifacts)

    def describe(self) -> Dict[str, Any]:
        return {
            "release": self.release.describe(),
            "artifacts": [artifact.describe() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class Destination:
    """Where a publisher writes, and under what authority.

    `credential_secret` is a repository secret *name*, never a token. A
    cross-repository write is a different capability from the one that published
    the release, so it is named separately and checked separately: a publisher
    that finds no credential reports `credential-missing` instead of quietly
    reusing whatever the job happened to be holding.
    """

    repository: str
    branch: str = "main"
    mode: str = MODE_TRUSTED
    credential_secret: str = ""
    attempts: int = 2

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.match(self.repository or ""):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"destination repository must be 'owner/name'; got {self.repository!r}",
            )
        if not self.branch or any(char in self.branch for char in "\n\r/ "):
            raise PublisherError(
                CONFIGURATION_INVALID, f"destination branch must be a branch name; got {self.branch!r}"
            )
        if self.mode not in SUPPORTED_UPDATE_MODES:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"destination mode must be one of {', '.join(SUPPORTED_UPDATE_MODES)}; "
                f"got {self.mode!r}",
            )
        if not self.credential_secret:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"destination {self.repository!r} must name the repository secret that "
                "holds its credential; a cross-repository write that borrows the "
                "release job's own token is not a separate capability, it is an "
                "accident waiting to happen",
            )
        if not 1 <= self.attempts <= 5:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"destination attempts must be between 1 and 5; got {self.attempts}",
            )

    def feature_branch(self, prefix: str, version: str) -> str:
        """The branch a pull-request update lands on.

        Keyed on the version so a re-run of the same release updates the branch
        it already has instead of opening a second pull request describing the
        same bytes.

        A branch name may contain `/` — `continuum/homebrew-v1.4.0` is an
        ordinary, namespaced branch — so the check is the set of forms git
        actually refuses, not "anything with a slash in it".
        """

        branch = f"{prefix}-v{version}"
        invalid = (
            any(char in branch for char in "\n\r\t ")
            or ".." in branch
            or "//" in branch
            or branch.startswith("/")
            or branch.endswith("/")
            or branch.startswith("-")
            or branch.endswith(".lock")
        )
        if invalid:
            raise PublisherError(
                CONFIGURATION_INVALID, f"feature branch must be a valid git branch name; got {branch!r}"
            )
        return branch

    def describe(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "branch": self.branch,
            "mode": self.mode,
            "credential_secret": self.credential_secret,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class PullRequest:
    """The pull request a destination ended up with, when it takes that shape."""

    number: int
    url: str
    head: str
    base: str

    def describe(self) -> Dict[str, Any]:
        return {"number": self.number, "url": self.url, "head": self.head, "base": self.base}


@dataclass(frozen=True)
class CommitResult:
    """What a write to a destination actually did.

    `changed` is False when the destination already held exactly these bytes.
    That is the whole of idempotency: re-running the same version finds the
    committed manifest identical, and a publisher that has nothing to say says
    so instead of opening an empty pull request.
    """

    revision: str
    branch: str
    changed: bool
    pull_request: Optional[PullRequest] = None

    def describe(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "branch": self.branch,
            "changed": self.changed,
            "pull_request": self.pull_request.describe() if self.pull_request else None,
        }


@dataclass(frozen=True)
class PublishRequest:
    """One release, one publisher's validated configuration, and nothing else.

    Deliberately not a release target. A publisher consumes a published release,
    and a project that has no signed macOS bundle at all — a static Linux
    binary, a Rust crate with a tap — describes exactly the same thing, so
    nothing here requires an Apple adapter to have run first.
    """

    manifest: ArtifactManifest
    settings: Any
    surfaces: Tuple[str, ...] = ()
    dry_run: bool = False
    environment: Mapping[str, str] = field(default_factory=dict)

    def surface_requested(self, name: str, enabled: bool) -> bool:
        """Whether one of a publisher's independently toggled surfaces runs.

        An explicit `surfaces` list narrows the run — that is how a caller asks
        for a plan of one surface — but it can never turn a disabled surface on,
        because "enabled" is a decision the configuration made and the reason
        code for not doing it has to be the one that says so.
        """

        if not enabled:
            return False
        if self.surfaces and name not in self.surfaces:
            return False
        return True

    def describe(self) -> Dict[str, Any]:
        return {
            "release": self.manifest.release.describe(),
            "surfaces": list(self.surfaces),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class PublishResult:
    """What a publisher did, in a form a workflow can branch on.

    `status` and `reason` are the two fields a caller acts on. Everything else
    — which repository, which branch, which revision, which pull request — is
    there so a human reading a job summary can tell which of several publishes
    went where without reading logs.
    """

    publisher: str
    status: str
    reason: str
    destination: str = ""
    branch: str = ""
    revision: str = ""
    pull_request: Optional[PullRequest] = None
    files: Tuple[str, ...] = ()
    surfaces: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    retryable: bool = False
    message: str = ""
    remediation: str = ""

    def __post_init__(self) -> None:
        if self.status not in SUPPORTED_STATUSES:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"publish status must be one of {', '.join(SUPPORTED_STATUSES)}; got {self.status!r}",
            )
        if not self.reason:
            raise PublisherError(
                CONFIGURATION_INVALID, "a publish result must carry a reason code"
            )

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PUBLISHED, STATUS_SKIPPED)

    @classmethod
    def failure(cls, publisher: str, error: PublisherError, **extra: Any) -> "PublishResult":
        return cls(
            publisher=publisher,
            status=STATUS_FAILED,
            reason=error.code,
            retryable=error.retryable,
            message=error.message,
            remediation=error.remediation,
            **extra,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PUBLISHER_RESULT_SCHEMA,
            "publisher": self.publisher,
            "status": self.status,
            "reason": self.reason,
            "destination": self.destination,
            "branch": self.branch,
            "revision": self.revision,
            "pull_request": self.pull_request.describe() if self.pull_request else None,
            "files": list(self.files),
            "surfaces": list(self.surfaces),
            "notes": list(self.notes),
            "retryable": self.retryable,
            "message": self.message,
            "remediation": self.remediation,
        }


__all__ = [
    "ALREADY_CURRENT",
    "ARCH_ARM64",
    "ARCH_X86_64",
    "ASSET_UNREACHABLE",
    "CONFIGURATION_INVALID",
    "CREDENTIAL_MISSING",
    "DIGEST_MISMATCH",
    "DISABLED",
    "DRY_RUN",
    "DESTINATION_UNREACHABLE",
    "KIND_APP_BUNDLE",
    "KIND_ARCHIVE",
    "MODE_PULL_REQUEST",
    "MODE_TRUSTED",
    "NO_ARTIFACT",
    "PUBLISHED",
    "PUBLISHER_RESULT_SCHEMA",
    "QUARANTINE_POLICY",
    "STALE_DESTINATION",
    "STATUS_FAILED",
    "STATUS_PUBLISHED",
    "STATUS_SKIPPED",
    "SURFACE_DISABLED",
    "SUPPORTED_ARCHITECTURES",
    "SUPPORTED_ARTIFACT_KINDS",
    "SUPPORTED_STATUSES",
    "SUPPORTED_UPDATE_MODES",
    "TEMPLATE_UNFILLED_TOKEN",
    "TEMPLATE_UNKNOWN_TOKEN",
    "VALIDATION_FAILED",
    "VALIDATION_UNAVAILABLE",
    "ArtifactManifest",
    "CommitResult",
    "Destination",
    "PublishRequest",
    "PublishResult",
    "PublishedArtifact",
    "PublisherError",
    "PullRequest",
    "RETRYABLE_REASONS",
    "ReleaseIdentity",
    "architecture_token",
    "is_retryable",
]
