"""Maven publication: a repository GitHub owns, and the Maven Central Portal.

This is the module that knows about registries, deployment bundles, publishing
policies, and GPG signatures as a repository sees them. The JVM adapter in
`jvm.py` knows nothing about any of it: a project that ships an application
attaches a jar to a release and never loads this module, and a project that
ships a library configures one of these publishers with a credential it holds
itself.

Three properties of a Maven repository shape the whole design, and each of them
is a way a JVM release has gone wrong before:

**A version is immutable.** Neither GitHub Packages nor Central will let a second
set of bytes occupy a coordinate a version already holds, and neither offers a
delete-then-replace for it. So the publishers *look before they write*: a
version already present with the file set this release would upload is a green
skip, because that is what a duplicate event is, and a version holding *some* of
the files is a refusal that names what is there. That second case is the one that
looks like a retry and is not: re-uploading the missing half of a version whose
other half came from an earlier, unreproducible run produces a coordinate whose
jar and sources were built from different trees, and no registry can tell.

**GitHub Packages is a repository, not a store.** A publish to it is a plain
authenticated `PUT` to a path under the caller's own repository, authorized by
the workflow's `GITHUB_TOKEN` with nothing but `packages: write`. There is no
Central credential in the picture, and a release that has one configured is a
release holding a credential it did not need.

**Central is a deployment, not an upload.** A deployment bundle — a zip laid out
in Maven repository paths, carrying the jar, the POM, the sources jar, the
Javadoc jar, and a detached signature for each — is uploaded to the Publisher API
and then *validated* by Sonatype before anything is public. The deployment has an
id and a state, and the state is the answer to "what happened to this release":
`PENDING`, `VALIDATING`, `VALIDATED`, `PUBLISHING`, `PUBLISHED`, or `FAILED`
with the validation errors attached. The retired OSSRH staging model — an
OSSHR-issued id, a staging repository, a release call — is not implemented, and
an endpoint that looks like one is refused rather than tolerated, because a
release that half-works against a retired protocol is a release that reports
success for nothing.

Two policies are first class because Central offers both and they mean different
things to the person who has to be in the loop: `AUTOMATIC` publishes as soon as
validation passes, and `USER_MANAGED` stops at `VALIDATED` for a human to
publish. Continuum honours the second by default, and converting a user-managed
namespace into an automated one is an explicit setting rather than a side effect
of a job that wants a green build.

Every destination is injected as a transport, so both publishers — every retry,
every skip, every refusal, every deployment state — are exercised in this
repository's tests against an in-memory registry and an in-memory portal with no
network, no JDK, and no credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contract import (
    TYPE_PACKAGE,
    TYPE_SIGNATURE,
    Artifact,
    ContractError,
    PublishRequest,
    PublisherResult,
    failed,
    published,
    skipped,
)
from .jvm import (
    CLASSIFIER_JAVADOC,
    CLASSIFIER_MAIN,
    CLASSIFIER_POM,
    CLASSIFIER_SOURCES,
    MavenCoordinates,
    PomMetadata,
    read_pom,
)

#: The two publishers' names and the destination strings their results carry.
#: A component name rather than a URL, so the recorded identity of a destination
#: is the same whichever repository the same publisher is pointed at.
GITHUB_PACKAGES_PUBLISHER = "github-packages"
GITHUB_PACKAGES_DESTINATION = "github packages"
CENTRAL_PUBLISHER = "maven-central"
CENTRAL_DESTINATION = "maven central"

DRY_RUN_CODE = "dry-run"

#: GitHub Packages is a Maven repository hosted on the repository's own path, and
#: the token that may write to it is the job's `GITHUB_TOKEN` with nothing but
#: `packages: write`. A release that reached for a Central token here would be
#: holding a far larger credential than the destination needs.
GITHUB_TOKEN = "GITHUB_TOKEN"
GITHUB_MAVEN_ROOT = "https://maven.pkg.github.com"

#: The current Central Portal. The `central.sonatype.com` host replaced
#: `oss.sonatype.org` entirely, and an endpoint under the old one is a retired
#: protocol rather than an alternative spelling.
CENTRAL_ROOT = "https://central.sonatype.com"
CENTRAL_API_ROOT = "/api/v1/publisher"

#: The two publishing policies the Portal offers.
PUBLISHING_AUTOMATIC = "AUTOMATIC"
PUBLISHING_USER_MANAGED = "USER_MANAGED"
SUPPORTED_PUBLISHING_TYPES: Tuple[str, ...] = (
    PUBLISHING_AUTOMATIC,
    PUBLISHING_USER_MANAGED,
)

#: The deployment states the Portal reports. `VALIDATED` is the terminal state of
#: a user-managed deployment, and `PUBLISHED` is the only state in which anything
#: is on Maven Central.
STATE_PENDING = "PENDING"
STATE_VALIDATING = "VALIDATING"
STATE_VALIDATED = "VALIDATED"
STATE_PUBLISHING = "PUBLISHING"
STATE_PUBLISHED = "PUBLISHED"
STATE_FAILED = "FAILED"
SUPPORTED_DEPLOYMENT_STATES: Tuple[str, ...] = (
    STATE_PENDING,
    STATE_VALIDATING,
    STATE_VALIDATED,
    STATE_PUBLISHING,
    STATE_PUBLISHED,
    STATE_FAILED,
)

#: The states in which a deployment is still being processed. Polling stops when
#: a state is not one of these, and a deployment stuck in one of them forever is
#: a timeout rather than a success.
IN_PROGRESS_STATES: Tuple[str, ...] = (STATE_PENDING, STATE_VALIDATING, STATE_PUBLISHING)

DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 120
DEFAULT_POLL_SECONDS = 5.0
MAX_POLL_SECONDS = 60.0

#: How many times a deployment is asked about before it is called unsettled. Its
#: own budget, deliberately not the request retry budget: `attempts` bounds how
#: many times a *call* is repeated when the Portal says try again, which is a
#: question about a 5xx. This bounds how long a *deployment* is watched, which is
#: a question about how long validation takes, and the two are minutes apart. A
#: Portal that validates a normal release in thirty seconds and a budget of three
#: five-second polls reports every ordinary release as never settled.
DEFAULT_POLL_ATTEMPTS = 60

#: Backoff between retries: the first retry waits this long and each one after it
#: doubles, up to the cap. Long enough to be a request rather than a retry storm,
#: short enough that a release is not held up for minutes.
RETRY_BACKOFF_BASE = 1.0
RETRY_BACKOFF_CAP = 30.0

#: The only file names a Maven repository is addressed by. A release publishes
#: these and nothing else: a distribution zip is a GitHub Release asset, and
#: uploading one to a repository would put a file in the namespace that no
#: dependency can resolve and that Central's validation would reject.
CENTRAL_PUBLISHED_CLASSIFIERS: Tuple[str, ...] = (
    CLASSIFIER_MAIN,
    CLASSIFIER_POM,
    CLASSIFIER_SOURCES,
    CLASSIFIER_JAVADOC,
)

#: A Maven version, in the character set Central accepts. Checked here rather
#: than left to the registry so a snapshot release is refused before an upload
#: rather than after one.
_SNAPSHOT_SUFFIX = "-SNAPSHOT"
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_RETRY_AFTER_RE = re.compile(r"^\d+$")
#: A file name a bundle can be written under on any runner. A colon is the one
#: character a GAV always contains and a file name never may, so it is the one
#: that has to be replaced rather than escaped.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class MavenError(RuntimeError):
    """The publisher cannot honestly report a result.

    Carries a stable `code`, a message safe to print, whether a second attempt
    could plausibly succeed, and optional details — the same four things every
    other release component reports, and the reason a caller can branch on the
    kind of failure instead of on prose.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        remediation: str = "",
        details: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.remediation = remediation
        self.details: Dict[str, str] = dict(details or {})

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class MavenApiError(MavenError):
    """A call to a registry failed, classified by what the response said.

    The distinction that matters most is `version-exists`: an immutable
    coordinate that is already occupied is not a failed request, it is a
    destination state, and the correct response is to report it rather than to
    try again with the same bytes.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        retryable: bool = False,
        version_exists: bool = False,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.status = status
        self.version_exists = version_exists
        #: Seconds the destination asked to wait, from `Retry-After`. Zero means
        #: it said nothing, so the caller backs off on its own schedule.
        self.retry_after = retry_after


# -- configuration -----------------------------------------------------------


def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MavenError(
            "configuration-invalid", f"{where} must be a mapping, got {type(value).__name__}"
        )
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise MavenError(
            "configuration-invalid",
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed)}",
        )


def _text(value: Any, where: str, default: str = "", *, required: bool = False) -> str:
    if value is None:
        if required:
            raise MavenError("configuration-invalid", f"{where} is required")
        return default
    if not isinstance(value, str) or not value.strip():
        raise MavenError("configuration-invalid", f"{where} must be a non-empty string")
    return value.strip()


def _choice(value: Any, where: str, choices: Sequence[str], default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise MavenError(
            "configuration-invalid",
            f"{where} must be one of {', '.join(choices)}; got {value!r}",
        )
    return value


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise MavenError("configuration-invalid", f"{where} must be true or false")
    return value


def _int(value: Any, where: str, default: int, *, low: int, high: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise MavenError("configuration-invalid", f"{where} must be an integer")
    if not low <= value <= high:
        raise MavenError(
            "configuration-invalid",
            f"{where} must be between {low} and {high}, got {value!r}",
        )
    return value


def _number(value: Any, where: str, default: float, *, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MavenError("configuration-invalid", f"{where} must be a number")
    if not low <= float(value) <= high:
        raise MavenError(
            "configuration-invalid",
            f"{where} must be between {low} and {high}, got {value!r}",
        )
    return round(float(value), 2)


def _secret_name(value: Any, where: str, *, required: bool = True) -> str:
    """A repository secret *name*, never a value.

    A Central credential is a user token, and a user token in a configuration
    file is a credential in a git history. The name is the only thing
    configuration may hold.
    """

    text = _text(value, where, required=required)
    if not text:
        return ""
    if not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", text):
        raise MavenError(
            "configuration-invalid",
            f"{where} must be an upper-case repository secret name; a registry "
            f"credential is referenced by name, never inlined; got {text!r}",
        )
    return text


def _identifier(value: Any, where: str) -> str:
    text = _text(value, where, required=True)
    if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", text):
        raise MavenError(
            "configuration-invalid",
            f"{where} must be a Maven group or artifact identifier; got {text!r}",
        )
    return text


# -- the library a release publishes -----------------------------------------


@dataclass(frozen=True)
class LibraryFile:
    """One file of a release's publication, and the signature that covers it.

    Kept as a pair rather than as two lists because the pairing is the invariant:
    Central requires a detached signature for every file it is given, and a
    release that uploads a jar without its signature has published something no
    consumer can verify. Building the list in pairs is what makes "every file is
    signed" a property of the type rather than a check somebody remembered.
    """

    artifact: Artifact
    signature: Optional[Artifact] = None

    @property
    def name(self) -> str:
        return self.artifact.name

    @property
    def signature_name(self) -> str:
        return self.signature.name if self.signature is not None else ""

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"file": self.artifact.name}
        if self.signature is not None:
            payload["signature"] = self.signature.name
        return payload


@dataclass(frozen=True)
class MavenLibrary:
    """A release's publishable library, resolved from its manifests.

    `metadata` is the POM's own answer to the questions a registry asks, read
    from the POM rather than from the build script that produced it — because a
    registry judges the POM, and a release that has not read it has not checked
    what will be judged.
    """

    coordinates: MavenCoordinates
    files: Tuple[LibraryFile, ...] = ()
    metadata: Optional[PomMetadata] = None

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self.files)

    def artifact(self, name: str) -> Artifact:
        for item in self.files:
            if item.artifact.name == name:
                return item.artifact
        raise MavenError("library-incomplete", f"no artifact named {name!r} in this library")

    def find(self, classifier: str) -> Optional[LibraryFile]:
        for item in self.files:
            if item.artifact.classifier == classifier:
                return item
        return None

    def paths(self) -> Tuple[Tuple[Artifact, str], ...]:
        """Every file and signature, in the order a repository expects them."""

        pairs: List[Tuple[Artifact, str]] = []
        for item in self.files:
            pairs.append((item.artifact, self.coordinates.path(item.artifact.name)))
            if item.signature is not None:
                pairs.append((item.signature, self.coordinates.path(item.signature.name)))
        return tuple(pairs)


def library_files(request: PublishRequest) -> Tuple[Artifact, ...]:
    """The artifacts in a release that a Maven repository can be addressed by.

    Filtered to the four coordinate-bearing files and nothing else: a release may
    also hold a distribution zip, an SBOM, and a checksum file, and a registry is
    not the place for any of them. Selecting by classifier rather than by "every
    package" is what keeps a GitHub Release asset out of a repository namespace
    even when a target produces both.
    """

    wanted = set(CENTRAL_PUBLISHED_CLASSIFIERS)
    return tuple(
        artifact
        for artifact in request.artifacts
        if artifact.type == TYPE_PACKAGE and artifact.classifier in wanted
    )


def read_library(
    request: PublishRequest,
    coordinates: MavenCoordinates,
    *,
    require_sources: bool = True,
    require_javadoc: bool = True,
    require_signatures: bool = True,
    require_metadata: bool = True,
) -> Tuple[MavenLibrary, Tuple[str, ...]]:
    """Resolve a release's library, and everything wrong with it.

    Returns the library *and* its problems rather than raising on the first one,
    because a release that has to be fixed should be told all of it in one pass.
    Every problem is a named, stable string a caller can branch on, and every one
    of them is a property of the release rather than of any destination — which
    is why the two publishers in this module share the function instead of each
    re-deriving the rules.
    """

    problems: List[str] = []
    candidates = library_files(request)
    signatures = {
        artifact.name: artifact
        for artifact in request.artifacts
        if artifact.type == TYPE_SIGNATURE
    }

    by_classifier: Dict[str, List[Artifact]] = {}
    for artifact in candidates:
        by_classifier.setdefault(artifact.classifier, []).append(artifact)

    main = by_classifier.get(CLASSIFIER_MAIN, [])
    if not main:
        problems.append(
            "no library jar: a release publishes a coordinate, and the jar is the "
            "artifact that coordinate resolves to"
        )
    elif len(main) > 1:
        problems.append(
            "this release holds "
            + ", ".join(item.name for item in main)
            + ". A coordinate resolves to exactly one main artifact, so a release with "
            "two cannot be published without choosing between them"
        )

    required: List[Tuple[str, bool]] = [
        (CLASSIFIER_POM, True),
        (CLASSIFIER_SOURCES, require_sources),
        (CLASSIFIER_JAVADOC, require_javadoc),
    ]
    for classifier, needed in required:
        if not needed:
            continue
        found = by_classifier.get(classifier, [])
        if not found:
            problems.append(
                f"no {classifier} artifact for {coordinates.gav}"
                + (
                    ". Central rejects a deployment without it"
                    if classifier == CLASSIFIER_POM
                    else ""
                )
            )
        elif len(found) > 1:
            problems.append(
                f"this release holds {len(found)} {classifier} artifacts "
                + ", ".join(item.name for item in found)
            )

    for classifier in (CLASSIFIER_MAIN, CLASSIFIER_POM):
        found = by_classifier.get(classifier, [])
        if not found:
            continue
        artifact = found[0]
        if artifact.name != coordinates.file(extension="pom" if classifier == CLASSIFIER_POM else "jar"):
            problems.append(
                f"{artifact.name} does not match the coordinate {coordinates.gav}. A "
                "registry addresses files by coordinate, so publishing a file whose "
                "name says one version under coordinates that say another makes the "
                "artifact unresolvable"
            )
        if not os.path.isfile(artifact.path):
            problems.append(
                f"{artifact.name} is in the manifest at {artifact.path!r}, which is not "
                "a readable file on this runner"
            )

    files: List[LibraryFile] = []
    for classifier in (CLASSIFIER_MAIN, CLASSIFIER_POM, CLASSIFIER_SOURCES, CLASSIFIER_JAVADOC):
        found = by_classifier.get(classifier, [])
        if not found:
            continue
        artifact = found[0]
        signature = signatures.get(os.path.basename(artifact.path) + ".asc")
        if require_signatures and signature is None:
            problems.append(
                f"{artifact.name} has no detached signature. Central rejects a "
                "deployment whose artifacts are unsigned, and a consumer has no way to "
                "check where the bytes came from"
            )
        if signature is not None and not os.path.isfile(signature.path):
            problems.append(
                f"the signature for {artifact.name} is recorded at {signature.path!r}, "
                "which is not a readable file on this runner"
            )
        files.append(LibraryFile(artifact=artifact, signature=signature))

    metadata: Optional[PomMetadata] = None
    pom = by_classifier.get(CLASSIFIER_POM, [])
    if pom and os.path.isfile(pom[0].path):
        try:
            metadata = read_pom(pom[0].path)
        except Exception as exc:  # noqa: BLE001 - reported as a problem, not raised
            problems.append(f"the POM could not be read: {exc}")
        else:
            if require_metadata:
                missing = metadata.missing_central_requirements()
                if missing:
                    problems.append(
                        "the POM is missing the metadata Central's validation requires: "
                        + ", ".join(missing)
                        + ". A namespace that has never published has no other way to "
                        "answer 'who wrote this, under what licence, and from where'"
                    )
            if metadata.group_id and metadata.group_id != coordinates.group_id:
                problems.append(
                    f"the POM declares groupId {metadata.group_id!r} but this release is "
                    f"published as {coordinates.group_id!r}. Continuum does not rewrite "
                    "coordinates, so a mismatch is a configuration error"
                )
            if metadata.artifact_id and metadata.artifact_id != coordinates.artifact_id:
                problems.append(
                    f"the POM declares artifactId {metadata.artifact_id!r} but this "
                    f"release is published as {coordinates.artifact_id!r}"
                )

    return (
        MavenLibrary(coordinates=coordinates, files=tuple(files), metadata=metadata),
        tuple(problems),
    )


def refuse_snapshot(version: str) -> None:
    """Refuse a snapshot version where snapshots are not published.

    Central has no snapshot repository: a version ending `-SNAPSHOT` is
    permanently mutable, and Central's whole value to a consumer is that a
    coordinate resolves to the same bytes forever. The check lives here so a
    release is told why before an upload rather than after a validation failure.
    """

    if version.endswith(_SNAPSHOT_SUFFIX):
        raise MavenError(
            "snapshot-refused",
            f"{version} is a snapshot version. Maven Central publishes immutable "
            "releases only, so a coordinate ending -SNAPSHOT can never be published "
            "there; cut a release version instead",
            remediation="release with a version that has no -SNAPSHOT suffix",
        )


# -- GitHub Packages ---------------------------------------------------------


@dataclass(frozen=True)
class GitHubPackagesSettings:
    """One GitHub Packages destination.

    `group_id` and `artifact_id` are carried through exactly as the build
    declared them, and the token is named rather than supplied: a repository that
    already has a `GITHUB_TOKEN` in the job needs no configuration at all to
    publish here, and the only permission that job needs is `packages: write`.
    """

    group_id: str
    artifact_id: str
    owner: str = ""
    repository: str = ""
    token_secret: str = GITHUB_TOKEN
    registry: str = GITHUB_MAVEN_ROOT
    require_signatures: bool = False
    attempts: int = DEFAULT_ATTEMPTS
    timeout: int = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not _identifier(self.group_id, "group_id"):
            raise MavenError("configuration-invalid", "group_id is required")
        if not _identifier(self.artifact_id, "artifact_id"):
            raise MavenError("configuration-invalid", "artifact_id is required")
        for name, value in (("owner", self.owner), ("repository", self.repository)):
            if value and not _REPOSITORY_RE.match(value):
                raise MavenError(
                    "configuration-invalid",
                    f"{name} {value!r} is not a GitHub owner or repository name",
                )
        if self.owner and not self.repository:
            raise MavenError(
                "configuration-invalid",
                "GitHub Packages serves a path under one repository, so owner and "
                "repository are both required or neither",
            )
        if not 1 <= self.attempts <= 5:
            raise MavenError(
                "configuration-invalid", f"attempts must be between 1 and 5; got {self.attempts}"
            )
        if not 1 <= self.timeout <= 900:
            raise MavenError(
                "configuration-invalid",
                f"timeout must be between 1 and 900 seconds; got {self.timeout}",
            )

    def coordinates(self, version: str) -> MavenCoordinates:
        return MavenCoordinates(
            group_id=self.group_id, artifact_id=self.artifact_id, version=version
        )

    def base_url(self) -> str:
        if not self.owner:
            return self.registry.rstrip("/")
        return f"{self.registry.rstrip('/')}/{self.owner}/{self.repository}"

    def secret_names(self) -> Tuple[str, ...]:
        return (self.token_secret,) if self.token_secret else ()

    def describe(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "owner": self.owner,
            "repository": self.repository,
            "token_secret": self.token_secret,
            "require_signatures": self.require_signatures,
            "attempts": self.attempts,
        }


_GITHUB_PACKAGES_KEYS = (
    "group_id",
    "artifact_id",
    "owner",
    "repository",
    "token_secret",
    "registry",
    "require_signatures",
    "attempts",
    "timeout",
)


def parse_github_packages_settings(value: Any, where: str = "github packages") -> GitHubPackagesSettings:
    """Validate one GitHub Packages destination's options."""

    mapping = _mapping(value, where)
    _reject_unknown(mapping, _GITHUB_PACKAGES_KEYS, where)
    return GitHubPackagesSettings(
        group_id=_identifier(mapping.get("group_id"), f"{where}.group_id"),
        artifact_id=_identifier(mapping.get("artifact_id"), f"{where}.artifact_id"),
        owner=_text(mapping.get("owner"), f"{where}.owner", ""),
        repository=_text(mapping.get("repository"), f"{where}.repository", ""),
        token_secret=_secret_name(
            mapping.get("token_secret"), f"{where}.token_secret", required=False
        )
        or GITHUB_TOKEN,
        registry=_text(mapping.get("registry"), f"{where}.registry", GITHUB_MAVEN_ROOT),
        require_signatures=_bool(
            mapping.get("require_signatures"), f"{where}.require_signatures", False
        ),
        attempts=_int(mapping.get("attempts"), f"{where}.attempts", DEFAULT_ATTEMPTS, low=1, high=5),
        timeout=_int(mapping.get("timeout"), f"{where}.timeout", DEFAULT_TIMEOUT, low=1, high=900),
    )


@dataclass(frozen=True)
class UploadReceipt:
    """What a registry says about the bytes it received.

    A registry that echoes nothing back is not evidence of anything, so a receipt
    that reports a size or a digest is compared with the manifest and one that
    reports neither is simply not evidence — the comparison is skipped rather
    than failed, because a registry is not obliged to compute a digest.
    """

    size: int = 0
    sha256: str = ""
    created: bool = True


class MavenRegistryTransport:
    """The calls the GitHub Packages publisher makes against a repository.

    Declared as an explicit attribute list rather than a `Protocol` so that a
    mis-wired destination is refused at construction, by the same `conform()`
    discipline every other port in the release contract follows.
    """

    SUPPORTS_DRY_RUN = True
    name = "maven-registry-transport"

    def file_exists(self, url: str, name: str) -> bool:
        raise NotImplementedError

    def upload(self, url: str, name: str, path: str) -> UploadReceipt:
        raise NotImplementedError


REGISTRY_SURFACE: Tuple[str, ...] = ("file_exists", "upload")


def require_registry_transport(transport: Any) -> Any:
    """Refuse a registry that cannot answer every call this publisher makes.

    A transport that cannot say whether a file is already there cannot support
    the one thing a repository publisher exists to get right: not uploading a
    version that is already published.
    """

    missing = [
        member for member in REGISTRY_SURFACE if not callable(getattr(transport, member, None))
    ]
    if missing:
        raise MavenError(
            "transport-incomplete",
            f"the Maven registry transport is missing {', '.join(missing)}. Every call "
            "this publisher makes has to be answered, including the one that asks "
            "whether a version is already published",
        )
    return transport


class InMemoryMavenRegistry(MavenRegistryTransport):
    """A repository that keeps its files in a dict, and its promises too.

    It enforces what the real destination enforces, because a fake that enforced
    none of it would prove nothing: a version is immutable once a file of it is
    present, an upload of a file that is already there is refused rather than
    overwritten, and a digest is echoed back from the bytes that arrived.
    """

    name = "maven-in-memory"

    def __init__(self, *, files: Optional[Mapping[str, bytes]] = None) -> None:
        self.files: Dict[str, bytes] = dict(files or {})
        self.uploads: List[Dict[str, Any]] = []
        self.lookups: List[str] = []
        self.failures: List[MavenApiError] = []
        self.echo_digest: bool = True

    def fail_next(self, *errors: MavenApiError) -> None:
        """Queue failures to be returned by the next calls, in order."""

        self.failures.extend(errors)

    def _next_failure(self) -> Optional[MavenApiError]:
        return self.failures.pop(0) if self.failures else None

    def present(self, base: str, name: str) -> List[str]:
        """The names under one coordinate that this registry already holds."""

        prefix = f"{base.rstrip('/')}/"
        return sorted(
            key[len(prefix):]
            for key, value in self.files.items()
            if key.startswith(prefix) and value is not None
        )

    def file_exists(self, url: str, name: str) -> bool:
        self.lookups.append(url)
        failure = self._next_failure()
        if failure is not None:
            raise failure
        return f"{url}/{name}" in self.files

    def upload(self, url: str, name: str, path: str) -> UploadReceipt:
        failure = self._next_failure()
        if failure is not None:
            raise failure
        if not os.path.isfile(path):
            raise MavenError(
                "library-artifact-absent",
                f"the file at {path!r} is not a readable file; uploading it would place a "
                "coordinate no consumer could verify",
            )
        with open(path, "rb") as handle:
            data = handle.read()
        key = f"{url}/{name}"
        if key in self.files:
            raise MavenApiError(
                "version-exists",
                f"{url} already holds {name}. A published version is immutable, so a "
                "second set of bytes cannot take its place",
                status=409,
                version_exists=True,
            )
        self.files[key] = data
        self.uploads.append({"url": url, "name": name, "size": len(data)})
        return UploadReceipt(
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest() if self.echo_digest else "",
            created=True,
        )


class HttpGitHubPackagesTransport(MavenRegistryTransport):
    """The real GitHub Packages Maven registry, over the standard library.

    The token is read from a repository secret *by name* and held only in the
    request header, so a credential never reaches a manifest, a journal, or a
    log. Every call is a `HEAD` or a `PUT` against a path under the caller's own
    repository: there is no upload session, no bundle, and no second protocol.
    """

    name = "github-packages-http"

    def __init__(
        self,
        environment: Optional[Mapping[str, str]] = None,
        *,
        token_secret: str = GITHUB_TOKEN,
        attempts: int = DEFAULT_ATTEMPTS,
        timeout: int = DEFAULT_TIMEOUT,
        sleeper: Any = time.sleep,
    ) -> None:
        self.environment: Dict[str, str] = dict(environment or {})
        self.token_secret = token_secret
        self.attempts = attempts
        self.timeout = timeout
        #: Injected so a test can assert the backoff without waiting for it.
        self.sleeper = sleeper

    def _token(self) -> str:
        token = (self.environment.get(self.token_secret) or "").strip()
        if not token:
            raise MavenError(
                "credential-missing",
                f"the registry token is not available in this job. The repository "
                f"secret {self.token_secret!r} is empty or unset; a fork receives it with "
                "nothing in it, so a check for the name alone would pass and publish as "
                "nobody",
                remediation=(
                    "give the release job `packages: write`, which is the only permission "
                    "GitHub Packages needs and is the token the job already holds"
                ),
            )
        return token

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "User-Agent": "continuum-release",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, url: str, *, body: Optional[bytes] = None) -> Tuple[int, bytes]:
        request = urllib.request.Request(
            url, data=body, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b""
            error = _classify_registry(
                exc.code, payload.decode("utf-8", "replace"), _retry_after(exc.headers)
            )
            raise error from None
        except (urllib.error.URLError, OSError) as exc:
            raise MavenApiError(
                "registry-unavailable",
                f"the registry could not be reached: {exc}",
                retryable=True,
            ) from None

    def file_exists(self, url: str, name: str) -> bool:
        """Whether this coordinate already holds this file.

        A `404` is the answer, not a failure: "no such file" is the normal state
        of a version that has not been published, which is every version on the
        first release and the state a publisher exists to detect. Only a
        refusal or a transport failure is an error, and both are retried.
        """

        return self._retry(lambda: self._file_exists_once(url, name))

    def _file_exists_once(self, url: str, name: str) -> bool:
        try:
            self._request("HEAD", f"{url}/{name}")
        except MavenApiError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def upload(self, url: str, name: str, path: str) -> UploadReceipt:
        if not os.path.isfile(path):
            raise MavenError(
                "library-artifact-absent",
                f"the file at {path!r} is not a readable file on this runner",
            )
        with open(path, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        return self._retry(lambda: self._upload_once(url, name, data, digest))

    def _upload_once(
        self, url: str, name: str, data: bytes, digest: str
    ) -> UploadReceipt:
        status, _ = self._request("PUT", f"{url}/{name}", body=data)
        return UploadReceipt(size=len(data), sha256=digest, created=status == 201)

    def _retry(self, call: Callable[[], Any]) -> Any:
        """Repeat a call while the registry says the answer was temporary.

        Retried on the destination's own words — a 429 or a 5xx, or a connection
        that could not be made — and on nothing else. A 403 is a permission the
        job does not have and a 409 is a version that is already there, and
        repeating either would report a slower version of the same answer.
        """

        last: Optional[MavenApiError] = None
        for attempt in range(1, self.attempts + 1):
            try:
                return call()
            except MavenApiError as exc:
                last = exc
                if not exc.retryable or attempt >= self.attempts:
                    raise
                if exc.retry_after:
                    self.sleeper(min(MAX_POLL_SECONDS, exc.retry_after))
                else:
                    self.sleeper(
                        min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** (attempt - 1)))
                    )
        raise last or MavenError("registry-call-failed", "the registry call never ran")


def _retry_after(headers: Any) -> float:
    """Seconds the destination asked to wait, if it said.

    Read rather than assumed: a rate limit that says how long to wait is
    answered by waiting that long, and a destination that says nothing is backed
    off on this module's own schedule.
    """

    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        return 0.0
    if not raw or not _RETRY_AFTER_RE.match(str(raw).strip()):
        return 0.0
    return float(str(raw).strip())


def _classify_registry(
    status: int, body: str, retry_after: float = 0.0
) -> MavenApiError:
    """Turn a registry response into a classified failure.

    Classified by the fact being reported rather than by the status alone,
    because two 4xx answers mean opposite things to a release: "your token was
    refused" is a permissions or credential problem, and "that version is already
    published" is a destination state that a re-run should recognise and report
    rather than a request worth repeating.
    """

    lowered = (body or "").lower()
    if status in (401, 403):
        return MavenApiError(
            "credential-rejected",
            f"the registry refused the token (HTTP {status}): {body[:200]}",
            status=status,
        )
    if status == 409 or "cannot be overwritten" in lowered or "already exists" in lowered:
        return MavenApiError(
            "version-exists",
            f"the registry refused an immutable version (HTTP {status}): {body[:200]}",
            status=status,
            version_exists=True,
        )
    if status == 404:
        return MavenApiError(
            "coordinate-not-found",
            f"the registry has no such path (HTTP {status}); check owner, repository, "
            "group_id and artifact_id",
            status=status,
        )
    if status == 400:
        return MavenApiError(
            "request-rejected", f"the registry rejected the request (HTTP 400): {body[:200]}", status=400
        )
    if status in (429, 500, 502, 503, 504):
        return MavenApiError(
            "registry-unavailable",
            f"the registry is temporarily unavailable (HTTP {status})",
            status=status,
            retryable=True,
            retry_after=retry_after,
        )
    return MavenApiError(
        "registry-call-failed", f"the registry returned HTTP {status}: {body[:200]}", status=status
    )


# -- Maven Central -----------------------------------------------------------


@dataclass(frozen=True)
class CentralSettings:
    """One Central Portal destination.

    The namespace is the Portal's name for the group the deployment publishes
    into, and the policy says what the Portal does once validation passes.
    `promote_validated` is the one setting that is a *decision* rather than a
    fact: it turns a user-managed namespace into an automated one at the last
    step, and it defaults to False because a namespace somebody chose to manage
    by hand is not one a job should take over without being told to.
    """

    namespace: str
    group_id: str
    artifact_id: str
    publishing_type: str = PUBLISHING_USER_MANAGED
    username_secret: str = ""
    password_secret: str = ""
    token_secret: str = ""
    base_url: str = CENTRAL_ROOT
    deployment_name: str = ""
    promote_validated: bool = False
    require_sources: bool = True
    require_javadoc: bool = True
    attempts: int = DEFAULT_ATTEMPTS
    timeout: int = DEFAULT_TIMEOUT
    poll_seconds: float = DEFAULT_POLL_SECONDS
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS

    def __post_init__(self) -> None:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$", self.namespace or ""):
            raise MavenError(
                "configuration-invalid",
                f"namespace {self.namespace!r} is not a Central namespace name",
            )
        if not _identifier(self.group_id, "group_id"):
            raise MavenError("configuration-invalid", "group_id is required")
        if not _identifier(self.artifact_id, "artifact_id"):
            raise MavenError("configuration-invalid", "artifact_id is required")
        if self.publishing_type not in SUPPORTED_PUBLISHING_TYPES:
            raise MavenError(
                "configuration-invalid",
                f"publishing_type {self.publishing_type!r} is not one of "
                f"{', '.join(SUPPORTED_PUBLISHING_TYPES)}",
            )
        if not 1 <= self.attempts <= 5:
            raise MavenError(
                "configuration-invalid", f"attempts must be between 1 and 5; got {self.attempts}"
            )
        if not 1 <= self.timeout <= 900:
            raise MavenError(
                "configuration-invalid",
                f"timeout must be between 1 and 900 seconds; got {self.timeout}",
            )
        if not 0.1 <= self.poll_seconds <= MAX_POLL_SECONDS:
            raise MavenError(
                "configuration-invalid",
                f"poll_seconds must be between 0.1 and {MAX_POLL_SECONDS}; got "
                f"{self.poll_seconds}",
            )
        if not 1 <= self.poll_attempts <= 1000:
            raise MavenError(
                "configuration-invalid",
                f"poll_attempts must be between 1 and 1000; got {self.poll_attempts}",
            )
        base = self.base_url.rstrip("/")
        if "oss.sonatype.org" in base or "s01.oss" in base:
            # Refused rather than tolerated: the OSSRH staging protocol was
            # retired, and a job pointed at it would be attempting a release that
            # can never succeed while reporting a specific and reassuring error.
            raise MavenError(
                "legacy-ossrh-refused",
                f"{self.base_url!r} is an OSSRH staging endpoint. Staging repositories "
                "were retired in favour of the Central Portal: a namespace, a "
                "deployment bundle, and the Publisher API under "
                f"{CENTRAL_ROOT}{CENTRAL_API_ROOT} are what a release publishes through "
                "now",
                remediation=(
                    f"set base_url to {CENTRAL_ROOT} and supply a Portal user token by "
                    "secret name"
                ),
            )

    def coordinates(self, version: str) -> MavenCoordinates:
        return MavenCoordinates(
            group_id=self.group_id, artifact_id=self.artifact_id, version=version
        )

    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}{CENTRAL_API_ROOT}"

    def secret_names(self) -> Tuple[str, ...]:
        return tuple(
            name
            for name in (self.username_secret, self.password_secret, self.token_secret)
            if name
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "publishing_type": self.publishing_type,
            "username_secret": self.username_secret,
            "password_secret": self.password_secret,
            "promote_validated": self.promote_validated,
            "require_sources": self.require_sources,
            "require_javadoc": self.require_javadoc,
            "attempts": self.attempts,
        }


_CENTRAL_KEYS = (
    "namespace",
    "group_id",
    "artifact_id",
    "publishing_type",
    "username_secret",
    "password_secret",
    "token_secret",
    "base_url",
    "deployment_name",
    "promote_validated",
    "require_sources",
    "require_javadoc",
    "attempts",
    "timeout",
    "poll_seconds",
)


def parse_central_settings(value: Any, where: str = "maven central") -> CentralSettings:
    """Validate one Central destination's options."""

    mapping = _mapping(value, where)
    _reject_unknown(mapping, _CENTRAL_KEYS, where)
    return CentralSettings(
        namespace=_text(mapping.get("namespace"), f"{where}.namespace", required=True),
        group_id=_identifier(mapping.get("group_id"), f"{where}.group_id"),
        artifact_id=_identifier(mapping.get("artifact_id"), f"{where}.artifact_id"),
        publishing_type=_choice(
            mapping.get("publishing_type"),
            f"{where}.publishing_type",
            SUPPORTED_PUBLISHING_TYPES,
            PUBLISHING_USER_MANAGED,
        ),
        username_secret=_secret_name(
            mapping.get("username_secret"), f"{where}.username_secret", required=False
        ),
        password_secret=_secret_name(
            mapping.get("password_secret"), f"{where}.password_secret", required=False
        ),
        token_secret=_secret_name(
            mapping.get("token_secret"), f"{where}.token_secret", required=False
        ),
        base_url=_text(mapping.get("base_url"), f"{where}.base_url", CENTRAL_ROOT),
        deployment_name=_text(mapping.get("deployment_name"), f"{where}.deployment_name", ""),
        promote_validated=_bool(
            mapping.get("promote_validated"), f"{where}.promote_validated", False
        ),
        require_sources=_bool(mapping.get("require_sources"), f"{where}.require_sources", True),
        require_javadoc=_bool(mapping.get("require_javadoc"), f"{where}.require_javadoc", True),
        attempts=_int(mapping.get("attempts"), f"{where}.attempts", DEFAULT_ATTEMPTS, low=1, high=5),
        timeout=_int(mapping.get("timeout"), f"{where}.timeout", DEFAULT_TIMEOUT, low=1, high=900),
        poll_seconds=_number(
            mapping.get("poll_seconds"), f"{where}.poll_seconds", DEFAULT_POLL_SECONDS, low=0.1, high=MAX_POLL_SECONDS
        ),
        poll_attempts=_int(
            mapping.get("poll_attempts"), f"{where}.poll_attempts", DEFAULT_POLL_ATTEMPTS, low=1, high=1000
        ),
    )


@dataclass(frozen=True)
class DeploymentState:
    """What the Portal currently says about one deployment.

    `errors` is the part that matters: a `FAILED` deployment without them is a
    red job nobody can act on, and Central puts the reason there — a missing
    signature, an absent source jar, a POM field its validation rejected.
    """

    deployment_id: str
    state: str = STATE_PENDING
    name: str = ""
    errors: Tuple[str, ...] = ()
    purls: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.deployment_id:
            raise MavenError(
                "configuration-invalid", "a deployment state must carry its deployment id"
            )
        if self.state not in SUPPORTED_DEPLOYMENT_STATES:
            raise MavenError(
                "configuration-invalid",
                f"unknown deployment state {self.state!r}; supported: "
                f"{', '.join(SUPPORTED_DEPLOYMENT_STATES)}",
            )

    @property
    def settled(self) -> bool:
        """Whether the deployment has stopped moving on its own."""

        return self.state not in IN_PROGRESS_STATES

    @property
    def validated(self) -> bool:
        return self.state in (STATE_VALIDATED, STATE_PUBLISHING, STATE_PUBLISHED)

    @property
    def published(self) -> bool:
        return self.state == STATE_PUBLISHED

    @property
    def failed(self) -> bool:
        return self.state == STATE_FAILED

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "deployment_id": self.deployment_id,
            "state": self.state,
        }
        if self.name:
            payload["name"] = self.name
        if self.errors:
            payload["errors"] = list(self.errors)
        if self.purls:
            payload["purls"] = list(self.purls)
        return payload


class CentralTransport:
    """The calls this publisher makes against the Central Portal.

    The Portal's API is the deployment's whole lifecycle: upload a bundle, ask
    for its state, and — only when the namespace is automated — promote it. There
    is no staging repository to remember and no OSSRH id to correlate, which is
    why this surface is four calls and not a session.
    """

    SUPPORTS_DRY_RUN = True
    name = "central-transport"

    def upload_bundle(
        self, namespace: str, publishing_type: str, name: str, path: str
    ) -> str:
        raise NotImplementedError

    def status(self, deployment_id: str) -> DeploymentState:
        raise NotImplementedError

    def publish(self, deployment_id: str) -> None:
        raise NotImplementedError

    def drop(self, deployment_id: str) -> None:
        raise NotImplementedError


CENTRAL_SURFACE: Tuple[str, ...] = ("upload_bundle", "status", "publish", "drop")


def require_central_transport(transport: Any) -> Any:
    """Refuse a Portal that cannot answer every call this publisher makes.

    A transport that cannot ask for a deployment's state cannot tell a validation
    failure from a deployment still being processed, and a release that cannot
    tell those apart either reports every slow deployment as a failure or waits
    forever.
    """

    missing = [
        member for member in CENTRAL_SURFACE if not callable(getattr(transport, member, None))
    ]
    if missing:
        raise MavenError(
            "transport-incomplete",
            f"the Central transport is missing {', '.join(missing)}. Every call this "
            "publisher makes has to be answered, including the one that asks whether "
            "validation has finished",
        )
    return transport


class InMemoryCentral(CentralTransport):
    """A Portal that keeps its deployments in a dict, and its states in order.

    It enforces what the real one enforces, because a fake that enforced none of
    it would prove nothing: a deployment is created `PENDING` and settles only
    after the polls a test asks for, validation failure carries the errors Central
    would have reported, one deployment may not carry a version a previous
    deployment already published, and a promotion is refused for a deployment that
    is not `VALIDATED`.
    """

    name = "central-in-memory"

    def __init__(
        self,
        *,
        validation_errors: Sequence[str] = (),
        polls_before_settled: int = 1,
        published: bool = True,
    ) -> None:
        self.deployments: Dict[str, DeploymentState] = {}
        self.bundles: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.promoted: List[str] = []
        self.dropped: List[str] = []
        self.failures: List[MavenApiError] = []
        self.validation_errors: Tuple[str, ...] = tuple(validation_errors)
        self._polls_left: Dict[str, int] = {}
        self._polls_before_settled = polls_before_settled
        self._published = published
        self._counter = 0
        #: Which namespace each deployment belongs to, and which version it
        #: carries, so a promotion records the coordinate the version belongs to.
        self.namespaces: Dict[str, str] = {}
        self.versions: Dict[str, str] = {}
        #: The coordinates this portal has already published, as `namespace:version`
        #: keys. Versioned rather than version-only because two namespaces may
        #: legitimately hold the same version of different artifacts, and because
        #: the immutability Central enforces is per coordinate, not per string.
        self.published_versions: Set[str] = set()

    def fail_next(self, *errors: MavenApiError) -> None:
        self.failures.extend(errors)

    def seed(
        self,
        deployment_id: str,
        state: str = STATE_PENDING,
        *,
        namespace: str = "",
        version: str = "",
        polls: int = 0,
    ) -> None:
        """Place a deployment in a state a test needs to find it in.

        A resumed run is handed the id a previous run recorded, so the only way
        to test one is to have a deployment already there. Seeding it here rather
        than by writing into the dicts means a test cannot build a state this
        Portal would never have produced.
        """

        self.deployments[deployment_id] = DeploymentState(
            deployment_id=deployment_id, state=state, name=deployment_id
        )
        self.namespaces[deployment_id] = namespace
        self.versions[deployment_id] = version or deployment_id
        self._polls_left[deployment_id] = polls

    def publish_version(self, version: str, namespace: str = "") -> None:
        """Seed a version as already on Maven Central."""

        self.published_versions.add(f"{namespace}:{version}" if namespace else version)

    def _next_failure(self) -> Optional[MavenApiError]:
        return self.failures.pop(0) if self.failures else None

    def _version_of(self, path: str, namespace: str = "") -> str:
        """The version a bundle is carrying, read off the file names inside it.

        A deployment name is a free string, so the version is taken from the
        artifacts themselves — which is also the only place it can be read
        honestly, since the Portal derives the version the same way. A bundle
        that cannot be read yields the namespace rather than a guess, so the
        immutability check fails loudly instead of comparing the wrong string.
        """

        with zipfile.ZipFile(path) as archive:
            entries = archive.namelist()
        for entry in entries:
            base = entry.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            parts = stem.split("-")
            # The longest suffix that is a legal, non-snapshot version wins: an
            # artifactId may itself contain hyphens, so `my-lib-1.4.0` has to
            # yield `1.4.0` rather than `lib-1.4.0`.
            for index in range(1, len(parts)):
                candidate = "-".join(parts[index:])
                if _VERSION_RE.match(candidate) and not candidate.endswith(_SNAPSHOT_SUFFIX):
                    return candidate
            if parts and _VERSION_RE.match(parts[0]):
                return parts[0]
        return namespace

    def upload_bundle(
        self, namespace: str, publishing_type: str, name: str, path: str
    ) -> str:
        self.calls.append("upload_bundle")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        if not os.path.isfile(path):
            raise MavenError(
                "bundle-absent",
                f"the deployment bundle at {path!r} is not a readable file; uploading "
                "it would record a deployment with nothing in it",
            )
        with open(path, "rb") as handle:
            data = handle.read()
        version = self._version_of(path, namespace)
        key = f"{namespace}:{version}"
        if key in self.published_versions:
            raise MavenApiError(
                "version-exists",
                f"{namespace} has already published {version}. A released version is "
                "immutable and cannot be replaced",
                status=409,
                version_exists=True,
            )
        self.bundles.append(
            {
                "namespace": namespace,
                "publishing_type": publishing_type,
                "name": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        self._counter += 1
        deployment_id = f"deployment-{self._counter:03d}"
        self.deployments[deployment_id] = DeploymentState(
            deployment_id=deployment_id, state=STATE_PENDING, name=name
        )
        self.namespaces[deployment_id] = namespace
        self.versions[deployment_id] = version
        self._polls_left[deployment_id] = self._polls_before_settled
        return deployment_id

    def status(self, deployment_id: str) -> DeploymentState:
        self.calls.append("status")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        current = self.deployments.get(deployment_id)
        if current is None:
            raise MavenApiError(
                "deployment-not-found",
                f"the Portal has no deployment {deployment_id!r}",
                status=404,
            )
        if current.settled:
            return current
        # Per deployment, not per portal: one deployment reaching VALIDATED must
        # not advance another deployment's clock, or a test with two of them
        # reports states the Portal would never have produced.
        left = self._polls_left.get(deployment_id, 0)
        if left > 0:
            self._polls_left[deployment_id] = left - 1
            return current
        if current.state == STATE_PUBLISHING:
            # A promoted deployment is published by the Portal, whatever
            # publishing_type says: promotion is the one action that moves it.
            settled = STATE_PUBLISHED
        elif self.validation_errors:
            settled = STATE_FAILED
        elif self._published:
            # An automated namespace publishes as soon as validation passes; a
            # user-managed one stops here for a human.
            settled = STATE_PUBLISHED
        else:
            settled = STATE_VALIDATED
        self.deployments[deployment_id] = DeploymentState(
            deployment_id=deployment_id,
            state=settled,
            name=current.name,
            errors=self.validation_errors if settled == STATE_FAILED else (),
        )
        if settled == STATE_PUBLISHED:
            # A version that reached Maven Central is immutable, so a second
            # deployment of it is refused the way the real Portal refuses one.
            namespace = self.namespaces.get(deployment_id, "")
            version = self.versions.get(deployment_id, "")
            self.published_versions.add(
                f"{namespace}:{version}" if namespace else (version or current.name)
            )
        return self.deployments[deployment_id]

    def publish(self, deployment_id: str) -> None:
        self.calls.append("publish")
        failure = self._next_failure()
        if failure is not None:
            raise failure
        current = self.deployments.get(deployment_id)
        if current is None:
            raise MavenApiError(
                "deployment-not-found",
                f"the Portal has no deployment {deployment_id!r}",
                status=404,
            )
        if current.state != STATE_VALIDATED:
            raise MavenApiError(
                "deployment-not-validated",
                f"deployment {deployment_id} is {current.state}; the Portal publishes a "
                "validated deployment, and promoting one that has not passed "
                "validation is refused",
                status=409,
            )
        self.promoted.append(deployment_id)
        namespace = self.namespaces.get(deployment_id, "")
        version = self.versions.get(deployment_id, "")
        self.published_versions.add(
            f"{namespace}:{version}" if namespace else (version or current.name)
        )
        self.deployments[deployment_id] = DeploymentState(
            deployment_id=deployment_id, state=STATE_PUBLISHING, name=current.name
        )

    def drop(self, deployment_id: str) -> None:
        self.calls.append("drop")
        self.dropped.append(deployment_id)
        self.deployments.pop(deployment_id, None)
        self.namespaces.pop(deployment_id, None)
        self.versions.pop(deployment_id, None)
        self._polls_left.pop(deployment_id, None)


def _multipart_body(
    field: str, file_name: str, path: str, boundary: str
) -> bytes:
    """A `multipart/form-data` body, built by hand.

    The Portal takes the bundle as one part named `bundle` with a filename, and
    the standard library has no multipart encoder, so it is written here rather
    than by adding a dependency to a release tool.
    """

    with open(path, "rb") as handle:
        payload = handle.read()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{file_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    return b"".join([head, payload, f"\r\n--{boundary}--\r\n".encode("ascii")])


def _classify_central(status: int, body: str) -> MavenApiError:
    """Turn a Portal response into a classified failure."""

    lowered = (body or "").lower()
    if status in (401, 403):
        return MavenApiError(
            "credential-rejected",
            f"the Portal refused the user token (HTTP {status}): {body[:200]}. A Portal "
            "token is a *user* token, not an OSSHR one",
            status=status,
        )
    if status == 409 or "already published" in lowered or "cannot be overwritten" in lowered:
        return MavenApiError(
            "version-exists",
            f"the Portal refused an immutable version (HTTP {status}): {body[:200]}",
            status=status,
            version_exists=True,
        )
    if status == 404:
        return MavenApiError(
            "deployment-not-found",
            f"the Portal has no such deployment (HTTP 404): {body[:200]}",
            status=status,
        )
    if status == 400:
        return MavenApiError(
            "request-rejected", f"the Portal rejected the request (HTTP 400): {body[:200]}", status=400
        )
    if status in (429, 500, 502, 503, 504):
        return MavenApiError(
            "portal-unavailable",
            f"the Portal is temporarily unavailable (HTTP {status})",
            status=status,
            retryable=True,
        )
    return MavenApiError(
        "portal-call-failed", f"the Portal returned HTTP {status}: {body[:200]}", status=status
    )


def _portal_errors(raw: Any) -> Tuple[str, ...]:
    """The validation errors the Portal attached, in a shape that can be read.

    The Portal reports them keyed by the file that failed — `"<path>": "<why>"` —
    so each is prefixed with the file it names: a validation failure that says
    *which* artifact was rejected is actionable, and one that says only "no
    signature found" is not. A list of objects and a bare list of strings are
    both accepted so a Portal that changes shape between responses cannot turn a
    rejected deployment into an unreadable one.
    """

    errors: List[str] = []
    if isinstance(raw, Mapping):
        for location, message in sorted(raw.items()):
            text = str(message).strip()
            errors.append(f"{location}: {text}" if text else str(location))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for entry in raw:
            if isinstance(entry, Mapping):
                message = str(entry.get("message") or entry.get("key") or "").strip()
                location = str(entry.get("locator") or entry.get("field") or "").strip()
                errors.append(f"{location}: {message}" if location else message)
            else:
                errors.append(str(entry))
    return tuple(error for error in errors if error)


class HttpCentralTransport(CentralTransport):
    """The real Central Portal Publisher API, over the standard library.

    The token is assembled from a username and password secret as
    base64(`user:password`) and held only in the request header, so a Portal
    credential never reaches a manifest, a journal, or a log.
    """

    name = "central-http"

    def __init__(
        self,
        environment: Optional[Mapping[str, str]] = None,
        *,
        settings: CentralSettings,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.environment: Dict[str, str] = dict(environment or {})
        self.settings = settings
        self.timeout = timeout
        self.api_root = settings.api_root()

    def _token(self) -> str:
        username = (self.environment.get(self.settings.username_secret) or "").strip() if self.settings.username_secret else ""
        password = (self.environment.get(self.settings.password_secret) or "").strip() if self.settings.password_secret else ""
        token = (self.environment.get(self.settings.token_secret) or "").strip() if self.settings.token_secret else ""
        if token:
            # A job that already holds a Portal user token uses it directly rather
            # than reassembling one from a password it may not have.
            return token
        if not username or not password:
            names = ", ".join(self.settings.secret_names()) or "no secret named"
            raise MavenError(
                "credential-missing",
                f"no Portal credential is available in this job ({names} are empty or "
                "unset). A Central user token is a repository secret referenced by name; "
                "a fork receives it with nothing in it, so a check for the name alone "
                "would pass and authenticate as nobody",
                remediation=(
                    "expose a Portal user token as a repository secret and name it in "
                    "username_secret and password_secret (or token_secret)"
                ),
            )
        return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "User-Agent": "continuum-release",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return (
                    response.status,
                    response.read(),
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b""
            raise _classify_central(exc.code, payload.decode("utf-8", "replace")) from None
        except (urllib.error.URLError, OSError) as exc:
            raise MavenApiError(
                "portal-unavailable",
                f"the Portal could not be reached: {exc}",
                retryable=True,
            ) from None

    def upload_bundle(
        self, namespace: str, publishing_type: str, name: str, path: str
    ) -> str:
        boundary = "continuum-release-boundary"
        body = _multipart_body("bundle", name, path, boundary)
        query = urllib.parse.urlencode({"name": name, "publishingType": publishing_type})
        status, data, _ = self._request(
            "POST",
            f"{self.api_root}/upload?{query}",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        # The Portal answers with the bare deployment id, but a version behind an
        # edge can answer with the JSON envelope; both are accepted rather than
        # guessing which one this is.
        text = data.decode("utf-8", "replace").strip()
        if text.startswith("{"):
            payload = json.loads(text)
            deployment_id = str(payload.get("deploymentId") or "")
        else:
            deployment_id = text
        if not deployment_id:
            raise MavenError(
                "portal-call-failed",
                f"the Portal accepted the bundle (HTTP {status}) but returned no "
                "deployment id, so there is nothing to ask about or to promote",
            )
        return deployment_id

    def status(self, deployment_id: str) -> DeploymentState:
        """Ask the Portal what state one deployment is in.

        A `POST` with the id as a query parameter, which is what the Publisher
        API specifies — the same verb as the upload, with the id as the subject
        rather than as a path. Sending a `GET` here would be a guess about a
        documented protocol, and a Portal that answers neither is a release that
        reports a state nobody asked about.
        """

        query = urllib.parse.urlencode({"id": deployment_id})
        _, data, _ = self._request("POST", f"{self.api_root}/status?{query}")
        payload = json.loads(data.decode("utf-8") or "{}")
        errors = _portal_errors(payload.get("errors"))
        return DeploymentState(
            deployment_id=str(payload.get("deploymentId") or deployment_id),
            state=str(payload.get("deploymentState") or STATE_PENDING),
            name=str(payload.get("deploymentName") or ""),
            errors=errors,
            purls=tuple(str(item) for item in (payload.get("purls") or [])),
        )

    def publish(self, deployment_id: str) -> None:
        self._request("POST", f"{self.api_root}/deployment/{deployment_id}")

    def drop(self, deployment_id: str) -> None:
        try:
            self._request("DELETE", f"{self.api_root}/deployment/{deployment_id}")
        except MavenApiError:
            # Dropping a deployment that is already gone is the outcome we wanted
            # anyway, and a failure here must not mask the failure that got us here.
            return


# -- the deployment bundle ---------------------------------------------------


@dataclass(frozen=True)
class BundleInfo:
    """The bundle that was written, and what is in it.

    The digest is recorded in the publication result so a deployment can be
    correlated with the exact bytes that produced it: two runs of the same release
    produce the same bundle, and a bundle that differs is a build that differs.
    """

    path: str
    size: int
    sha256: str
    entries: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "entries": len(self.entries),
            "size": self.size,
            "sha256": self.sha256,
        }


#: A fixed timestamp for every entry, so the same release produces the same
#: bundle twice. A zip that embeds the wall clock is not reproducible, and a
#: deployment whose digest changes between two runs of one release cannot be
#: correlated with anything.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def write_bundle(path: str, library: MavenLibrary) -> BundleInfo:
    """Write the deployment bundle for a library.

    Laid out as the repository paths it will be published at, which is what the
    Portal expects: `com/example/widgets/1.4.0/widgets-1.4.0.jar`, and beside it
    the `.asc` that covers the jar. Assembled here rather than by a build plugin
    so the bundle is a pure function of the manifest — a build tool that can sign
    and cannot be run in a test is a bundle nothing can be checked against, and a
    plugin that lays the bundle out slightly differently from the publisher that
    validates it is a release that fails validation for a reason no report names.
    """

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    entries: List[str] = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for artifact, entry in library.paths():
            info = zipfile.ZipInfo(entry, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(artifact.path, "rb") as handle:
                archive.writestr(info, handle.read())
            entries.append(entry)
    with open(path, "rb") as handle:
        data = handle.read()
    return BundleInfo(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        entries=tuple(entries),
    )


# -- the publishers ----------------------------------------------------------


def _refuse_unpublishable(request: PublishRequest) -> Optional[PublisherResult]:
    """The last gate before anything is uploaded to a repository.

    Every manifest is re-checked against the approved source and version here as
    well as in the state machine, because this is the last code between a build
    and the outside world and a check that exists only further up is a check a
    future caller can reach past. A plan is exempt: its artifacts are declared
    rather than built, so the invariants a real run insists on are exactly the
    ones it is reporting are not yet established.
    """

    if request.dry_run:
        return None
    for manifest in request.manifests:
        try:
            manifest.assert_publishable(request.source_sha, request.version)
        except ContractError as exc:
            return failed(request.destination, str(exc), code="manifest-not-publishable")
    return None


def _confirm(
    destination: str, artifact: Artifact, receipt: UploadReceipt
) -> Optional[PublisherResult]:
    """Check that the bytes a registry received are the bytes that were built.

    A registry that echoes a size or a digest has told us something checkable, and
    checking it turns "the upload returned 200" into "the upload returned 200 with
    these bytes". A registry that reports neither is not evidence either way, so
    the comparison is skipped rather than failed.
    """

    if receipt.size and receipt.size != artifact.size:
        return failed(
            destination,
            f"the registry reports {receipt.size} bytes for {artifact.name}, but the "
            f"manifest holds {artifact.size}. The upload was truncated or the file "
            "changed after the manifest was written",
            code="upload-size-mismatch",
            retryable=True,
            details={"artifact": artifact.name, "received": str(receipt.size)},
        )
    if receipt.sha256 and receipt.sha256 != artifact.digest:
        return failed(
            destination,
            f"the registry hashed {artifact.name} to {receipt.sha256}, but the manifest "
            f"holds {artifact.digest}. The bytes that reached the registry are not the "
            "bytes this release was approved for",
            code="upload-digest-mismatch",
            details={"artifact": artifact.name, "received": receipt.sha256},
        )
    return None


def _refusal(
    destination: str,
    reason: str,
    *,
    code: str,
    version: str = "",
    remediation: str = "",
    retryable: bool = False,
    identity: str = "",
    external_id: str = "",
    details: Optional[Mapping[str, str]] = None,
) -> PublisherResult:
    """A failure, with the version and the next step recorded alongside it.

    The contract records a version for a *published* or *skipped* destination
    but not for a failed one, so both facts are folded into what it does carry:
    the version as a detail, and the remediation as the last sentence of the
    reason. A caller reading nothing but the message still learns what to do
    next, which is the whole point of a refusal that will not clear itself.
    """

    merged: Dict[str, str] = dict(details or {})
    if version:
        merged.setdefault("version", version)
    return failed(
        destination,
        f"{reason} To proceed: {remediation}" if remediation else reason,
        code=code,
        retryable=retryable,
        identity=identity,
        external_id=external_id,
        details=merged or None,
    )


def _rejected(
    destination: str, problems: Sequence[str], *, code: str, extra: str = ""
) -> PublisherResult:
    return failed(
        destination,
        "; ".join(problems) + (f" {extra}" if extra else ""),
        code=code,
        details={"problems": str(len(problems))},
    )


class GitHubPackagesPublisher:
    """Publishes a library's files to the repository's own Maven registry.

    The transport is injected, so the whole publisher — every skip, every refusal
    to overwrite an immutable version, every retry — runs against an in-memory
    registry with no network and no credential.
    """

    SUPPORTS_DRY_RUN = True
    name = GITHUB_PACKAGES_PUBLISHER

    def __init__(
        self,
        settings: GitHubPackagesSettings,
        transport: Any,
        *,
        name: str = GITHUB_PACKAGES_PUBLISHER,
        destination: str = GITHUB_PACKAGES_DESTINATION,
        sleeper: Any = time.sleep,
    ) -> None:
        if not isinstance(settings, GitHubPackagesSettings):
            raise MavenError(
                "configuration-invalid",
                f"settings must be GitHubPackagesSettings, got {type(settings).__name__}",
            )
        if not destination:
            raise MavenError(
                "configuration-invalid",
                "a publisher must name its destination; a result that does not say "
                "where it went cannot be compared with anything",
            )
        self.settings = settings
        self.transport = require_registry_transport(transport)
        self.name = name
        self._destination = destination
        #: Injected so a test can assert the backoff without waiting for it.
        self._sleeper = sleeper
        #: The files this run has uploaded, reported as a detail so a release
        #: that uploaded something can say what. Nothing consults it to decide
        #: what the registry holds: the confirm stage is a read, and a read that
        #: answered from memory would report a coordinate complete after
        #: something outside this process had removed a file from it.
        self._uploaded: set = set()

    @property
    def destination(self) -> str:
        return self._destination

    def intent(self) -> str:
        return (
            f"upload the jar, POM, sources jar, Javadoc jar and their signatures for "
            f"{self.settings.group_id}:{self.settings.artifact_id} to this repository's "
            f"Maven registry at {self.settings.base_url()}, using the job's "
            f"{self.settings.token_secret} with no other permission"
        )

    def _identity(self, coordinates: MavenCoordinates) -> str:
        return coordinates.gav

    def _coordinate_url(self, coordinates: MavenCoordinates) -> str:
        return f"{self.settings.base_url()}/{coordinates.group_path}/{coordinates.artifact_id}/{coordinates.version}"

    # -- inputs -----------------------------------------------------------
    def _library(
        self, request: PublishRequest
    ) -> Tuple[Optional[MavenLibrary], Optional[PublisherResult]]:
        coordinates = self.settings.coordinates(request.version)
        library, problems = read_library(
            request,
            coordinates,
            # GitHub Packages takes any file set, so the sources and Javadoc jars
            # are only required when the target asks for them. Signatures are the
            # same: the registry does not check them, so a library that signs
            # publishes its signatures and one that does not is not blocked here.
            require_sources=CLASSIFIER_SOURCES in self._wanted(request),
            require_javadoc=CLASSIFIER_JAVADOC in self._wanted(request),
            require_signatures=self.settings.require_signatures,
        )
        if problems:
            return None, _rejected(
                self._destination, problems, code="library-incomplete"
            )
        return library, None

    @staticmethod
    def _wanted(request: PublishRequest) -> Tuple[str, ...]:
        return tuple(
            sorted(
                {
                    artifact.classifier
                    for artifact in library_files(request)
                }
            )
        )

    def _survey(
        self, library: MavenLibrary, url: str
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...], Optional[PublisherResult]]:
        """What this coordinate already holds, and what to do about it.

        The three answers are the three things a repository can say, and they are
        different outcomes rather than one "it failed":

        * every file present — the version is already published, so there is
          nothing to upload and a re-run is green;
        * nothing present — the write is safe to attempt;
        * some present — a *partial* version, which is refused. The registry will
          not let the missing files be replaced with different bytes, and
          completing it from a second build would publish a coordinate whose jar
          and sources came from different trees. The only safe answer is a new
          version, and this says so.
        """

        present: List[str] = []
        absent: List[str] = []
        for item in library.files:
            for artifact, name in ((item.artifact, item.artifact.name),) + (
                ((item.signature, item.signature.name),) if item.signature is not None else ()
            ):
                if self.transport.file_exists(url, name):
                    present.append(artifact.name)
                else:
                    absent.append(artifact.name)
        if present and absent:
            return (
                tuple(present),
                tuple(absent),
                _refusal(
                    self._destination,
                    f"{library.coordinates.gav} is already half published: the registry "
                    f"holds {', '.join(sorted(present))} and not {', '.join(sorted(absent))}. "
                    "A published file cannot be replaced, so completing this version from "
                    "a second build would publish a coordinate whose files came from "
                    "different trees",
                    code="version-partial",
                    identity=self._identity(library.coordinates),
                    version=library.coordinates.version,
                    details={
                        "present": ",".join(sorted(present)),
                        "absent": ",".join(sorted(absent)),
                    },
                    remediation="cut a new version; a partial coordinate cannot be repaired",
                ),
            )
        return tuple(present), tuple(absent), None

    def _planned(self, request: PublishRequest, library: MavenLibrary) -> PublisherResult:
        names = ", ".join(name for name in library.names) or "nothing"
        return skipped(
            self._destination,
            f"would upload {names} for {library.coordinates.gav} to "
            f"{self._coordinate_url(library.coordinates)}"
            + (", and the signature of each" if self.settings.require_signatures else ""),
            identity=self._identity(library.coordinates),
            external_version=request.version,
            code=DRY_RUN_CODE,
            details={
                "files": ",".join(library.names),
                "signatures": str(sum(1 for item in library.files if item.signature)),
            },
        )

    # -- draft ------------------------------------------------------------
    def draft(self, request: PublishRequest) -> PublisherResult:
        """Put the whole file set of a coordinate in the registry.

        There is no unpublished state in a repository — a file is resolvable the
        moment it is there — so this stage is the one that must be complete or
        nothing. Everything that has to be true before a file is written is
        checked first: the coordinate is the one configured, the file set is
        complete and signed if the destination demands signatures, every file is
        present on this runner, and the version is not already occupied.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            library, problem = self._library(request)
        except MavenError as exc:
            return failed(self._destination, exc.message, code=exc.code, retryable=exc.retryable)
        if problem is not None:
            return problem
        assert library is not None
        if request.dry_run:
            return self._planned(request, library)
        return self._with_error_mapping(
            lambda: self._upload_missing(request, library)
        )

    def _upload_missing(
        self, request: PublishRequest, library: MavenLibrary
    ) -> PublisherResult:
        url = self._coordinate_url(library.coordinates)
        present, absent, partial = self._survey(library, url)
        if partial is not None:
            return partial
        if not absent:
            return skipped(
                self._destination,
                f"{library.coordinates.gav} is already published to this registry with "
                f"{len(present)} file(s); a published version is immutable and there is "
                "nothing to upload",
                identity=self._identity(library.coordinates),
                external_version=request.version,
                code="already-published",
                details={"files": ",".join(sorted(present))},
            )
        uploaded: List[str] = []
        for artifact, entry in library.paths():
            if artifact.name in present:
                continue
            if not os.path.isfile(artifact.path):
                return failed(
                    self._destination,
                    f"{artifact.name} is in the manifest at {artifact.path!r}, which is "
                    "not a readable file on this runner",
                    code="library-artifact-absent",
                    details={"artifact": artifact.name},
                )
            receipt = self.transport.upload(url, entry.rsplit("/", 1)[-1], artifact.path)
            mismatch = _confirm(self._destination, artifact, receipt)
            if mismatch is not None:
                # Something of this version is now in the registry, so a re-run
                # would meet a partial coordinate. Reported as it happened rather
                # than as safely retryable, because it is not.
                return _refusal(
                    self._destination,
                    mismatch.reason
                    + ". The registry now holds part of this version, so re-running would "
                    "find a half-published coordinate rather than finish this one",
                    code=mismatch.code,
                    identity=self._identity(library.coordinates),
                    version=library.coordinates.version,
                    details=dict(mismatch.details),
                    remediation="cut a new version; this coordinate is now partial",
                )
            self._uploaded.add(artifact.name)
            uploaded.append(artifact.name)
        return published(
            self._destination,
            self._identity(library.coordinates),
            external_version=request.version,
            code="uploaded",
            details={
                "uploaded": ",".join(uploaded),
                "files": str(len(library.files)),
                "url": url,
            },
        )

    # -- publish ----------------------------------------------------------
    def publish(self, request: PublishRequest) -> PublisherResult:
        """Confirm the coordinate is complete in the registry.

        Nothing is uploaded that the draft stage did not already put there: this
        is a read. That is what makes it safe to run twice, safe to resume, and
        safe to report as a duplicate — a registry either holds a complete
        coordinate or it does not hold the release at all.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            library, problem = self._library(request)
        except MavenError as exc:
            return failed(self._destination, exc.message, code=exc.code, retryable=exc.retryable)
        if problem is not None:
            return problem
        assert library is not None
        if request.dry_run:
            return skipped(
                self._destination,
                f"would confirm that {library.coordinates.gav} is complete in this "
                "repository's Maven registry, with a detached signature for every file",
                identity=self._identity(library.coordinates),
                external_version=request.version,
                code=DRY_RUN_CODE,
            )
        return self._with_error_mapping(lambda: self._confirm_complete(request, library))

    def _confirm_complete(
        self, request: PublishRequest, library: MavenLibrary
    ) -> PublisherResult:
        url = self._coordinate_url(library.coordinates)
        _, absent, partial = self._survey(library, url)
        if partial is not None:
            return partial
        if absent:
            return _refusal(
                self._destination,
                f"{library.coordinates.gav} is not in this registry: "
                + ", ".join(sorted(absent))
                + " are missing. Nothing has been released, and the draft stage has to run "
                "first; this is safe to retry",
                code="coordinate-absent",
                retryable=True,
                identity=self._identity(library.coordinates),
                version=request.version,
                details={"absent": ",".join(sorted(absent))},
            )
        return published(
            self._destination,
            self._identity(library.coordinates),
            external_version=request.version,
            code="published",
            details={
                "files": str(len(library.files)),
                "url": url,
                "signatures": str(sum(1 for item in library.files if item.signature)),
            },
        )

    # -- error mapping ----------------------------------------------------
    def _with_error_mapping(self, call: Any) -> PublisherResult:
        """Turn a classified registry failure into a publication result.

        A version that is already published is reported as the green duplicate it
        is: the release is out there, it is immutable, and re-uploading it would
        be refused forever.
        """

        try:
            result = call()
        except MavenApiError as exc:
            if exc.version_exists:
                return skipped(
                    self._destination,
                    f"the registry refused an upload because this version is already "
                    f"published and immutable: {exc.message}",
                    code="already-published",
                )
            return failed(
                self._destination,
                exc.message,
                code=exc.code,
                retryable=exc.retryable,
                details=exc.details or None,
            )
        except MavenError as exc:
            return failed(
                self._destination,
                exc.message,
                code=exc.code,
                retryable=exc.retryable,
                details=exc.details or None,
            )
        if not isinstance(result, PublisherResult):
            return failed(
                self._destination,
                f"the publisher produced {type(result).__name__} rather than a "
                "publication result",
                code="result-not-returned",
            )
        return result


class MavenCentralPublisher:
    """Publishes a library to Maven Central through the current Portal.

    The transport is injected, so every deployment state, every validation
    failure, and every retry is exercised against an in-memory Portal with no
    network and no credential.
    """

    SUPPORTS_DRY_RUN = True
    name = CENTRAL_PUBLISHER

    def __init__(
        self,
        settings: CentralSettings,
        transport: Any,
        *,
        name: str = CENTRAL_PUBLISHER,
        destination: str = CENTRAL_DESTINATION,
        deployment_id: str = "",
        scratch_dir: str = "",
        sleeper: Any = time.sleep,
    ) -> None:
        if not isinstance(settings, CentralSettings):
            raise MavenError(
                "configuration-invalid",
                f"settings must be CentralSettings, got {type(settings).__name__}",
            )
        if not destination:
            raise MavenError(
                "configuration-invalid",
                "a publisher must name its destination; a result that does not say "
                "where it went cannot be compared with anything",
            )
        self.settings = settings
        self.transport = require_central_transport(transport)
        self.name = name
        self._destination = destination
        self._sleeper = sleeper
        self.scratch_dir = scratch_dir
        #: The deployment this publisher is working on. Learned from the upload in
        #: this run, or supplied by a caller resuming one — the Portal has no
        #: endpoint that looks a deployment up by coordinate, so a resumed run has
        #: to be told which deployment it is resuming. The value is what the draft
        #: stage recorded as `external_id`, and a publisher without it reports
        #: that rather than uploading a second deployment of one version.
        self._deployment_id = deployment_id
        self._state: Optional[DeploymentState] = None

    @property
    def destination(self) -> str:
        return self._destination

    @property
    def deployment_id(self) -> str:
        return self._deployment_id

    def intent(self) -> str:
        policy = (
            "publish it as soon as validation passes"
            if self.settings.publishing_type == PUBLISHING_AUTOMATIC
            else (
                "validate it and leave it for a human to publish in the Portal"
                if not self.settings.promote_validated
                else "validate it and promote the validated deployment"
            )
        )
        return (
            f"bundle the jar, POM, sources jar, Javadoc jar and their signatures for "
            f"{self.settings.group_id}:{self.settings.artifact_id} as a Central Portal "
            f"deployment bundle, upload it to namespace {self.settings.namespace}, and "
            f"{policy}"
        )

    # -- inputs -----------------------------------------------------------
    def _library(
        self, request: PublishRequest
    ) -> Tuple[Optional[MavenLibrary], Optional[PublisherResult]]:
        coordinates = self.settings.coordinates(request.version)
        try:
            refuse_snapshot(coordinates.version)
        except MavenError as exc:
            return None, failed(self._destination, exc.message, code=exc.code)
        library, problems = read_library(
            request,
            coordinates,
            require_sources=self.settings.require_sources,
            require_javadoc=self.settings.require_javadoc,
            # Central requires a signature for every file it is given. This is
            # not a setting because it is not a choice: a deployment whose
            # artifacts are unsigned is rejected by validation, and finding that
            # out from the Portal costs an upload and a wait.
            require_signatures=True,
            require_metadata=True,
        )
        if problems:
            return None, _rejected(
                self._destination,
                problems,
                code="central-metadata-incomplete",
                extra=(
                    "A Central deployment is validated before it is published, so a "
                    "release that is missing any of this is refused here rather than "
                    "uploaded and rejected minutes later."
                ),
            )
        return library, None

    def _bundle_name(self, request: PublishRequest, library: MavenLibrary) -> str:
        """The deployment's name, which is also the file it is uploaded as.

        Derived from the coordinate with the colons replaced, because a colon is
        not a legal file name on every runner a release might be built on and a
        name that cannot be written is a release that cannot be uploaded. A
        configured `deployment_name` is used verbatim, because a name somebody
        chose is the name they will look for in the Portal.
        """

        configured = self.settings.deployment_name
        if configured:
            return configured
        coordinate = library.coordinates.gav.replace(":", "-")
        return f"{_SAFE_NAME_RE.sub('_', coordinate)}-bundle.zip"

    def _bundle_path(self, library: MavenLibrary, name: str) -> str:
        directory = self.scratch_dir or os.path.dirname(library.files[0].artifact.path)
        return os.path.join(directory, name)

    def _planned(self, request: PublishRequest, library: MavenLibrary) -> PublisherResult:
        return skipped(
            self._destination,
            f"would assemble a deployment bundle of "
            f"{len(library.files)} artifact(s) with "
            f"{sum(1 for item in library.files if item.signature)} signature(s) for "
            f"{library.coordinates.gav}, upload it to namespace "
            f"{self.settings.namespace} as {self.settings.publishing_type}, and wait for "
            "validation to settle",
            identity=self._identity(library.coordinates),
            external_version=request.version,
            code=DRY_RUN_CODE,
            details={
                "namespace": self.settings.namespace,
                "publishing_type": self.settings.publishing_type,
                "files": ",".join(library.names),
            },
        )

    def _identity(self, coordinates: MavenCoordinates) -> str:
        return f"{self.settings.namespace}:{coordinates.gav}"

    # -- draft ------------------------------------------------------------
    def draft(self, request: PublishRequest) -> PublisherResult:
        """Upload the deployment bundle and wait for validation to settle.

        This is the stage that spends the coordinate, so everything that has to
        be true before it is spent is checked here: the version is one Central
        publishes, the POM carries the metadata its validation requires, every
        file is signed, and every file is on this runner. The stage ends at the
        first settled state — `VALIDATED`, `PUBLISHED`, or `FAILED` — because
        that is where the answer is, and a failure here has the validation errors
        attached rather than a bare red job.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            library, problem = self._library(request)
        except MavenError as exc:
            return failed(self._destination, exc.message, code=exc.code, retryable=exc.retryable)
        if problem is not None:
            return problem
        assert library is not None
        if request.dry_run:
            return self._planned(request, library)
        return self._with_error_mapping(lambda: self._deploy(request, library))

    def _deploy(self, request: PublishRequest, library: MavenLibrary) -> PublisherResult:
        if self._deployment_id:
            # A resumed run: ask about the deployment it already has rather than
            # uploading a second bundle of a version that may already be released.
            state = self._await(self._deployment_id)
            return self._settled(request, library, state)
        name = self._bundle_name(request, library)
        bundle = write_bundle(self._bundle_path(library, name), library)
        deployment_id = self.transport.upload_bundle(
            self.settings.namespace, self.settings.publishing_type, name, bundle.path
        )
        self._deployment_id = deployment_id
        state = self._await(deployment_id)
        if self.settings.publishing_type == PUBLISHING_AUTOMATIC and state.validated and not state.published:
            # An automated deployment goes to Maven Central on its own once it is
            # validated; watching it to the end here means the release reports
            # what actually happened rather than what was expected to.
            state = self._await(deployment_id)
        return self._settled(request, library, state, bundle=bundle)

    # -- publish ----------------------------------------------------------
    def publish(self, request: PublishRequest) -> PublisherResult:
        """Report what the Portal holds for this deployment.

        An automated deployment was already published by the time the draft stage
        returned, so this is a read that confirms it. A user-managed deployment is
        either promoted — when the destination is configured to do that — or left
        waiting, and the result says which, because "validated, waiting for
        somebody" and "published" are different facts about a release.
        """

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        try:
            library, problem = self._library(request)
        except MavenError as exc:
            return failed(self._destination, exc.message, code=exc.code, retryable=exc.retryable)
        if problem is not None:
            return problem
        assert library is not None
        if request.dry_run:
            return skipped(
                self._destination,
                f"would report the deployment state of {library.coordinates.gav} in "
                f"namespace {self.settings.namespace}"
                + (
                    " and promote it, because the destination is configured to publish "
                    "validated deployments"
                    if self.settings.promote_validated
                    else " and leave it for a human to publish, because the namespace is "
                    "user-managed"
                ),
                identity=self._identity(library.coordinates),
                external_version=request.version,
                code=DRY_RUN_CODE,
            )
        if not self._deployment_id:
            return _refusal(
                self._destination,
                "no deployment id is known for this release, so there is nothing to "
                "report or promote. The Portal has no endpoint that looks a deployment "
                "up by coordinate, so a resumed run has to be given the id the draft "
                "stage recorded as its publication's external_id",
                code="deployment-absent",
                retryable=True,
                identity=self._identity(library.coordinates),
                version=request.version,
                remediation="pass deployment_id= to the publisher, or let the draft stage run",
            )
        return self._with_error_mapping(lambda: self._advance(request, library))

    def _advance(
        self, request: PublishRequest, library: MavenLibrary
    ) -> PublisherResult:
        state = self._await(self._deployment_id)
        if state.published:
            return skipped(
                self._destination,
                f"deployment {self._deployment_id} of {library.coordinates.gav} is "
                f"{state.state}; Maven Central is serving this release",
                identity=self._identity(library.coordinates),
                external_id=self._deployment_id,
                external_version=request.version,
                code="already-published",
                details={"state": state.state},
            )
        if state.failed:
            return self._validation_failure(library, state)
        # What is left is VALIDATED: `_await` returns a settled state or raises,
        # and PUBLISHED and FAILED are both handled above, so a deployment that
        # is neither validated nor published cannot reach this point.
        assert state.state == STATE_VALIDATED, state.state
        if not self.settings.promote_validated:
            return skipped(
                self._destination,
                f"deployment {self._deployment_id} of {library.coordinates.gav} passed "
                f"validation and is waiting to be published by hand. Namespace "
                f"{self.settings.namespace} is user-managed, so Continuum does not "
                "promote it; set promote_validated to change that",
                identity=self._identity(library.coordinates),
                external_id=self._deployment_id,
                external_version=request.version,
                code="awaiting-manual-publish",
                details={"state": state.state, "awaiting": "manual-publish"},
            )
        self.transport.publish(self._deployment_id)
        settled = self._await(self._deployment_id)
        if settled.failed:
            return self._validation_failure(library, settled)
        if not settled.published:
            return _refusal(
                self._destination,
                f"deployment {self._deployment_id} was promoted but is {settled.state}, "
                "not PUBLISHED. The Portal is still uploading; this is safe to retry",
                code="deployment-unsettled",
                retryable=True,
                identity=self._identity(library.coordinates),
                external_id=self._deployment_id,
                version=request.version,
                details={"state": settled.state},
            )
        return published(
            self._destination,
            self._identity(library.coordinates),
            external_id=self._deployment_id,
            external_version=request.version,
            code="published",
            details={
                "state": settled.state,
                "namespace": self.settings.namespace,
                "publishing_type": self.settings.publishing_type,
            },
        )

    # -- polling ----------------------------------------------------------
    def _await(self, deployment_id: str) -> DeploymentState:
        """Poll until the deployment stops moving, bounded by the poll budget.

        The bound is what keeps this honest: a deployment the Portal never
        finishes is a timeout, reported as retryable, rather than a wait that
        hangs the job. Each poll waits a little longer than the last, capped, so
        a deployment that takes a minute to validate is not polled sixty times at
        the same interval.

        Two budgets, because they answer two questions. `poll_attempts` bounds
        how long the *deployment* is watched, and `attempts` bounds how many
        times one poll is *repeated* when the Portal answers with a 5xx -- a
        repeated poll is the same question asked again, so it does not spend the
        deployment's patience.
        """

        state = DeploymentState(deployment_id=deployment_id, state=STATE_PENDING)
        poll = 0
        while poll < self.settings.poll_attempts:
            poll += 1
            for retry in range(1, self.settings.attempts + 1):
                try:
                    state = self.transport.status(deployment_id)
                    break
                except MavenApiError as exc:
                    if not exc.retryable or retry >= self.settings.attempts:
                        raise
                    self._sleeper(
                        min(
                            MAX_POLL_SECONDS,
                            RETRY_BACKOFF_BASE * (2 ** (retry - 1)),
                        )
                    )
            if state.settled:
                return state
            self._sleeper(min(MAX_POLL_SECONDS, self.settings.poll_seconds * poll))
        raise MavenError(
            "deployment-unsettled",
            f"deployment {deployment_id} was still {state.state} after {poll} checks. "
            "Nothing has been released and the deployment can still settle, so this "
            "is safe to retry",
            retryable=True,
            details={"state": state.state, "checks": str(poll)},
        )

    def _settled(
        self,
        request: PublishRequest,
        library: MavenLibrary,
        state: DeploymentState,
        *,
        bundle: Optional[BundleInfo] = None,
    ) -> PublisherResult:
        details: Dict[str, str] = {
            "state": state.state,
            "namespace": self.settings.namespace,
            "publishing_type": self.settings.publishing_type,
        }
        if bundle is not None:
            details["bundle_sha256"] = bundle.sha256
            details["bundle_files"] = str(len(bundle.entries))
        if state.published:
            return published(
                self._destination,
                self._identity(library.coordinates),
                external_id=state.deployment_id,
                external_version=request.version,
                code="published",
                details=details,
            )
        if state.validated:
            # Validation is not publication. A deployment that is only validated
            # has not reached Maven Central, and reporting it as published would
            # tell a reader the coordinate resolves when it does not. It is a
            # green result either way -- the upload succeeded -- but it says what
            # it actually is.
            return skipped(
                self._destination,
                f"deployment {state.deployment_id} of {library.coordinates.gav} passed "
                f"validation and is {state.state}, not PUBLISHED. "
                + (
                    "An automated deployment is published by the Portal after "
                    "validation, so this is the point the release stops waiting"
                    if self.settings.publishing_type == PUBLISHING_AUTOMATIC
                    else f"Namespace {self.settings.namespace} is user-managed, so this "
                    "waits for a human to publish it"
                ),
                identity=self._identity(library.coordinates),
                external_id=state.deployment_id,
                external_version=request.version,
                code="validated",
                details=details,
            )
        return self._validation_failure(library, state, details)

    def _validation_failure(
        self,
        library: MavenLibrary,
        state: DeploymentState,
        details: Optional[Mapping[str, str]] = None,
    ) -> PublisherResult:
        """Report a rejected deployment with the Portal's own reasons.

        Not retryable, and not because it is discouraged: the bytes are fixed and
        the validation rules are fixed, so running the identical deployment again
        produces the identical rejection. The next release has to change something
        — the missing signature, the POM field, the source jar.
        """

        merged = dict(details or {})
        merged["state"] = state.state
        reasons = "; ".join(state.errors) if state.errors else "the Portal reported no reason"
        return _refusal(
            self._destination,
            f"Central rejected deployment {state.deployment_id} of "
            f"{library.coordinates.gav}: {reasons}. Nothing was published, and re-uploading "
            "the same bytes would be rejected the same way",
            code="validation-failed",
            identity=self._identity(library.coordinates),
            external_id=state.deployment_id,
            version=library.coordinates.version,
            details=merged,
            remediation="fix what validation named and cut a new version; this one is spent",
        )

    # -- error mapping ----------------------------------------------------
    def _with_error_mapping(self, call: Any) -> PublisherResult:
        try:
            result = call()
        except MavenApiError as exc:
            if exc.version_exists:
                return skipped(
                    self._destination,
                    f"the Portal refused the deployment because this version is already "
                    f"published and immutable: {exc.message}",
                    code="already-published",
                )
            return failed(
                self._destination,
                exc.message,
                code=exc.code,
                retryable=exc.retryable,
                details=exc.details or None,
            )
        except MavenError as exc:
            return failed(
                self._destination,
                exc.message,
                code=exc.code,
                retryable=exc.retryable,
                details=exc.details or None,
            )
        if not isinstance(result, PublisherResult):
            return failed(
                self._destination,
                f"the publisher produced {type(result).__name__} rather than a "
                "publication result",
                code="result-not-returned",
            )
        return result


__all__ = [
    "CENTRAL_API_ROOT",
    "CENTRAL_DESTINATION",
    "CENTRAL_PUBLISHER",
    "CENTRAL_ROOT",
    "CENTRAL_SURFACE",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_POLL_ATTEMPTS",
    "DEFAULT_POLL_SECONDS",
    "GITHUB_MAVEN_ROOT",
    "GITHUB_PACKAGES_DESTINATION",
    "GITHUB_PACKAGES_PUBLISHER",
    "GITHUB_TOKEN",
    "IN_PROGRESS_STATES",
    "PUBLISHING_AUTOMATIC",
    "PUBLISHING_USER_MANAGED",
    "REGISTRY_SURFACE",
    "RETRY_BACKOFF_BASE",
    "RETRY_BACKOFF_CAP",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_PUBLISHING",
    "STATE_PUBLISHED",
    "STATE_VALIDATED",
    "STATE_VALIDATING",
    "SUPPORTED_DEPLOYMENT_STATES",
    "SUPPORTED_PUBLISHING_TYPES",
    "BundleInfo",
    "CentralSettings",
    "CentralTransport",
    "DeploymentState",
    "GitHubPackagesPublisher",
    "GitHubPackagesSettings",
    "HttpCentralTransport",
    "HttpGitHubPackagesTransport",
    "InMemoryCentral",
    "InMemoryMavenRegistry",
    "LibraryFile",
    "MavenApiError",
    "MavenError",
    "MavenLibrary",
    "MavenCentralPublisher",
    "MavenRegistryTransport",
    "UploadReceipt",
    "library_files",
    "parse_central_settings",
    "parse_github_packages_settings",
    "read_library",
    "refuse_snapshot",
    "require_central_transport",
    "require_registry_transport",
    "write_bundle",
]
