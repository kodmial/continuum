"""A release with no platform in it.

Everything in this module exists to be obviously not a platform: the adapter
writes a text file and measures it, the destination is a dict, the notes are a
fixed string. That is the point. The generic contract is supposed to be provable
without a Mac, an Android SDK, or a JVM, and a fixture that needed one of those
would be testing the platform rather than the contract.

The counters are the other point. An idempotent release that is only idempotent
because nothing happened is not idempotent; so every side effect the contract can
cause is counted here, and the tests assert on the counts. A second run over the
same event must leave every one of them unchanged.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from continuum.release import contract
from continuum.release.contract import (
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    Eligibility,
    ManifestBuilder,
    NotesRequest,
    PublishRequest,
    PublisherResult,
    ReleaseEvent,
    ReleaseNotes,
    ReleasePrRequest,
    ReleasePrResult,
    TargetSpec,
    VerificationReport,
    digest_file,
)
from continuum.release.github import (
    DESTINATION as GITHUB_DESTINATION,
    DRY_RUN_CODE,
    GitHubReleasePublisher,
    PortFailure,
    ReleaseAsset,
    ReleaseRecord,
)
from continuum.release.state import Journal
from continuum.release.version import ExplicitVersion, ProjectFileVersion, VersionPolicy

REPOSITORY = "example/widgets"
SHA = "a" * 40
OTHER_SHA = "b" * 40
VERSION = "1.4.0"
IDENTITY = "fixture signing identity"
VERIFIER = "fixture verifier"
CLASSIFIER = "linux-x86_64"
CHECKSUMS = "checksums"
FIXTURE = "binary"
ADAPTER = "fixture"


def event(
    *,
    repository: str = REPOSITORY,
    name: str = "tag-push",
    sha: str = SHA,
    ref: str = "refs/tags/v1.4.0",
    tag: str = "v1.4.0",
    delivery: str = "fixture-delivery-1",
    **attributes: str,
) -> ReleaseEvent:
    return ReleaseEvent(
        repository=repository,
        name=name,
        sha=sha,
        ref=ref,
        tag=tag,
        default_branch="main",
        delivery=delivery,
        attributes=tuple(sorted(attributes.items())),
    )


def targets(*ids: str) -> Tuple[TargetSpec, ...]:
    return tuple(TargetSpec(id=item, adapter=ADAPTER) for item in ids or (ADAPTER,))


def _digest_of(path: str) -> str:
    return digest_file(path)[1]


def project_file(directory: str, version: str = VERSION) -> str:
    """Write a project file that the project-file strategy can be asked to read."""

    path = os.path.join(directory, "VERSION")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(version + "\n")
    return path


class FixtureEligibility:
    """Says what it was configured to say about an event."""

    SUPPORTS_DRY_RUN = True
    name = "fixture-eligibility"

    def __init__(self, *, eligible: bool = True, blocking: bool = False) -> None:
        self.eligible = eligible
        self.blocking = blocking
        self.seen: List[ReleaseEvent] = []

    def intent(self) -> str:
        return "say whether this event may become a release"

    def evaluate(self, event: ReleaseEvent) -> Eligibility:
        self.seen.append(event)
        return Eligibility(
            eligible=self.eligible,
            blocking=self.blocking,
            reason="" if self.eligible else "the fixture says so",
            code="fixture-verdict",
        )


class FixtureNotes:
    SUPPORTS_DRY_RUN = True
    name = "fixture-notes"

    def __init__(self) -> None:
        self.seen: List[NotesRequest] = []

    def intent(self) -> str:
        return "write the release notes for a version"

    def build(self, request: NotesRequest) -> ReleaseNotes:
        self.seen.append(request)
        return ReleaseNotes(body=f"{request.tag} was released.")


class FixtureReleasePr:
    """A release pull request driver that reports what it was configured with."""

    SUPPORTS_DRY_RUN = True
    name = "fixture-release-pr"

    def __init__(self, *, state: str = "merged", version: str = VERSION) -> None:
        self.state = state
        self.version = version
        self.seen: List[ReleasePrRequest] = []

    def intent(self) -> str:
        return "make sure a release pull request exists and carries the version"

    def ensure(self, request: ReleasePrRequest) -> ReleasePrResult:
        self.seen.append(request)
        return ReleasePrResult(
            state=self.state,
            number=17,
            url="https://example.invalid/pull/17",
            version=self.version,
            reason=f"the fixture reports a pull request carrying {self.version}",
        )


@dataclass
class FixtureAdapter:
    """Builds a text artifact, signs it by stamping it, and checks the stamp.

    The signing is a prefix written into the file and the verification checks
    that the prefix is there and the digest still matches, which is enough to
    make the core's invariants observable: an artifact signed by the wrong
    identity must not verify, and a manifest built from the wrong commit must not
    publish. A real adapter has the same shape with a real signature.
    """

    SUPPORTS_DRY_RUN = True
    name = ADAPTER

    identity: str = IDENTITY
    verifier: str = VERIFIER
    is_available: bool = True
    workdir: str = ""
    #: The artifact name each target produces, so a test can make two targets
    #: collide on purpose.
    artifact_name: str = ""
    built: List[BuildRequest] = field(default_factory=list)
    signed: List[BuildRequest] = field(default_factory=list)
    verified: List[BuildRequest] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    fail_build: bool = False
    fail_verify: bool = False
    report_unverified: bool = False
    wrong_source: bool = False
    drop_signature: bool = False
    #: Suffix for the produced artifact name, so a test can ask for a file whose
    #: extension selects a comment convention (`.rb`, `.plist`) instead of the
    #: plain text fixture.
    artifact_suffix: str = ".txt"
    #: Appended to the artifact body verbatim. Used to leave a template token in a
    #: generated file, which is the one defect the build stage must refuse.
    leftover_token: str = ""
    #: When set, written verbatim as the artifact body instead of the usual text,
    #: so a test can ask for a file the placeholder reader cannot decode.
    body_bytes: bytes = b""

    def intent(self) -> str:
        return "build, sign, and verify one text artifact per target"

    def available(self) -> bool:
        return self.is_available

    def _directory(self, request: BuildRequest) -> str:
        root = self.workdir or request.workdir or tempfile.gettempdir()
        path = os.path.join(root, "continuum-fixture", request.version, request.target.id)
        os.makedirs(path, exist_ok=True)
        return path

    def _name(self, request: BuildRequest) -> str:
        if self.artifact_name:
            return self.artifact_name
        return f"{request.target.id}-{request.version}-{CLASSIFIER}{self.artifact_suffix}"

    def _checksums_name(self, request: BuildRequest) -> str:
        return f"{request.target.id}-{request.version}-SHA256SUMS.txt"

    def _signature(self, request: BuildRequest) -> str:
        return f"signed-by:{self.identity}:{request.source_sha}\n".encode("utf-8")

    def _body(self, request: BuildRequest, source_sha: str) -> bytes:
        if self.body_bytes:
            return self.body_bytes
        return (
            f"target={request.target.id}\nversion={request.version}\n"
            f"source={source_sha}\nclassifier={CLASSIFIER}\n"
            f"{self.leftover_token}"
        ).encode("utf-8")

    def _record_checksums(
        self, builder: ManifestBuilder, directory: str, name: str
    ) -> None:
        """Write and record the checksum listing for what has been recorded.

        Named after the target because a release's assets share one flat
        namespace: two targets both shipping `SHA256SUMS.txt` would collide, and
        the contract refuses a collision rather than choosing a winner.
        """

        path = builder.build().write_checksums(directory, name)
        builder.record(
            name,
            path,
            CHECKSUMS,
            classifier=CHECKSUMS,
            media_type="text/plain",
            verification=contract.VERIFICATION_VERIFIED,
            verified_by=self.verifier,
        )

    def build(self, request: BuildRequest) -> ArtifactManifest:
        self.built.append(request)
        if self.fail_build:
            raise ContractError("the fixture toolchain is broken")
        directory = self._directory(request)
        source_sha = OTHER_SHA if self.wrong_source else request.source_sha
        name = self._name(request)
        path = os.path.join(directory, name)
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=ADAPTER,
            source_sha=source_sha,
            version=request.version,
        )
        if request.dry_run:
            builder.declare(name, path, FIXTURE, classifier=CLASSIFIER)
            return builder.build()
        with open(path, "wb") as handle:
            handle.write(self._body(request, source_sha))
        self.writes.append(path)
        builder.record(
            name,
            path,
            FIXTURE,
            classifier=CLASSIFIER,
            # Declared, not attested: the fixture cannot sign anything, so it
            # says "this artifact wants provenance" and lets the destination
            # attach it, which is the arrangement a real adapter with a real
            # keyless signing identity is in.
            provenance=contract.PROVENANCE_DECLARED,
        )
        self._record_checksums(builder, directory, self._checksums_name(request))
        return builder.build()

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        self.signed.append(request)
        directory = self._directory(request)
        builder = ManifestBuilder(
            target=manifest.target,
            adapter=ADAPTER,
            source_sha=manifest.source_sha,
            version=manifest.version,
        )
        for artifact in manifest.artifacts:
            if artifact.type == CHECKSUMS:
                # The listing describes the artifacts, and signing changes their
                # bytes, so it is rewritten below rather than carried over.
                continue
            if artifact.declared:
                builder.declare(
                    artifact.name, artifact.path, artifact.type, classifier=artifact.classifier
                )
                continue
            with open(artifact.path, "rb") as handle:
                body = handle.read()
            prefix = b"" if self.drop_signature else self._signature(request)
            if not body.startswith(prefix):
                with open(artifact.path, "wb") as handle:
                    handle.write(prefix + body)
            builder.record(
                artifact.name,
                artifact.path,
                artifact.type,
                classifier=artifact.classifier,
                signing=contract.SIGNING_SIGNED,
                signing_identity=self.identity,
                # Signing the bytes does not change what was built, so what was
                # declared about them is carried across rather than dropped.
                provenance=artifact.provenance,
            )
        if not request.dry_run:
            self._record_checksums(builder, directory, self._checksums_name(request))
        return builder.build()

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        self.verified.append(request)
        if request.dry_run:
            # A plan has no bytes to check, so it reports the shape it would
            # check rather than claiming a verification it cannot have made.
            return VerificationReport(
                verified=True,
                code="fixture-planned",
                verified_by=self.verifier,
                detail=", ".join(artifact.name for artifact in manifest.artifacts),
            )
        if self.fail_verify:
            return VerificationReport(
                verified=False,
                code="fixture-verification-failed",
                detail="the fixture was told to fail",
            )
        checked: List[str] = []
        for artifact in manifest.artifacts:
            if artifact.type == CHECKSUMS:
                if manifest.checksums_digest() != _digest_of(artifact.path):
                    return VerificationReport(
                        verified=False,
                        code="checksums-mismatch",
                        detail=(
                            f"{artifact.name} does not describe the artifacts it was "
                            "recorded beside"
                        ),
                    )
                continue
            if artifact.declared:
                return VerificationReport(
                    verified=False,
                    code="artifact-declared",
                    detail=(
                        f"{artifact.name} is declared, not built; there is nothing to "
                        "verify yet"
                    ),
                )
            with open(artifact.path, "rb") as handle:
                body = handle.read()
            if not body.startswith(self._signature(request)):
                return VerificationReport(
                    verified=False,
                    code="signature-mismatch",
                    detail=f"{artifact.name} is not signed by {self.identity}",
                )
            _, digest = digest_file(artifact.path)
            if digest != artifact.digest:
                return VerificationReport(
                    verified=False,
                    code="digest-mismatch",
                    detail=f"{artifact.name} does not match the digest it was recorded with",
                )
            checked.append(artifact.name)
        if self.report_unverified:
            return VerificationReport(
                verified=False,
                code="fixture-unverified",
                detail="the fixture was told to report nothing as verified",
            )
        return VerificationReport(
            verified=True,
            code="fixture-verified",
            verified_by=self.verifier,
            detail=", ".join(checked),
        )


class MemoryReleaseRepository:
    """A release destination that is a dict with the same shape as the API's."""

    def __init__(self) -> None:
        self.releases: Dict[str, ReleaseRecord] = {}
        self.held: Dict[str, Dict[str, ReleaseAsset]] = {}
        self.calls: List[str] = []
        self.fail_upload: Dict[str, Exception] = {}
        self.fail_publish: Optional[Exception] = None
        self.fail_find: Optional[Exception] = None

    def find_by_tag(self, tag: str) -> Optional[ReleaseRecord]:
        self.calls.append(f"find:{tag}")
        if self.fail_find is not None:
            raise self.fail_find
        return self.releases.get(tag)

    def create_draft(
        self,
        *,
        tag: str,
        name: str,
        target_sha: str,
        notes: Optional[ReleaseNotes] = None,
    ) -> Optional[ReleaseRecord]:
        self.calls.append(f"draft:{tag}")
        record = ReleaseRecord(
            id=f"release-{len(self.releases) + 1}",
            tag=tag,
            target_sha=target_sha,
            draft=True,
            name=name,
        )
        self.releases[tag] = record
        self.held[tag] = {}
        return record

    def assets(self, record: ReleaseRecord) -> Tuple[ReleaseAsset, ...]:
        held = self.held.get(record.tag, {})
        return tuple(held[name] for name in sorted(held))

    def upload(self, record: ReleaseRecord, expected: ReleaseAsset, path: str) -> None:
        self.calls.append(f"upload:{record.tag}:{expected.name}")
        if expected.name in self.fail_upload:
            raise self.fail_upload[expected.name]
        held = self.held.setdefault(record.tag, {})
        if expected.name in held:
            # A destination will not take a second asset under a name it already
            # holds, which is what makes "an attached asset is never replaced" a
            # property of the destination and not only a promise.
            raise RuntimeError(f"{expected.name} already exists on {record.tag}")
        with open(path, "rb") as handle:
            body = handle.read()
        size, digest = digest_file(path)
        if len(body) != size:
            raise RuntimeError("the file changed while it was being read")
        held[expected.name] = ReleaseAsset(
            name=expected.name,
            size=expected.size,
            digest=expected.digest,
            digest_algorithm=expected.digest_algorithm,
        )

    def publish(self, record: ReleaseRecord) -> ReleaseRecord:
        self.calls.append(f"publish:{record.tag}")
        if self.fail_publish is not None:
            raise self.fail_publish
        promoted = ReleaseRecord(
            id=record.id,
            tag=record.tag,
            target_sha=record.target_sha,
            draft=False,
            name=record.name,
            url=f"https://example.invalid/releases/{record.tag}",
            immutable=True,
        )
        self.releases[record.tag] = promoted
        return promoted

    def attached(self, tag: str) -> Tuple[ReleaseAsset, ...]:
        return self.assets(ReleaseRecord(id="probe", tag=tag, target_sha=SHA, draft=True))


class MemoryProvenance:
    """Attestation that is a list, so a test can see whether it was asked."""

    SUPPORTS_DRY_RUN = True
    name = "memory-provenance"

    def __init__(self) -> None:
        self.attested: List[Tuple[str, str]] = []

    def intent(self) -> str:
        return "attest an artifact so a consumer can check who built it"

    def attest(
        self,
        *,
        tag: str,
        name: str,
        digest_algorithm: str,
        digest: str,
        source_sha: str,
    ) -> str:
        self.attested.append((name, tag))
        return f"attestation:{name}:{digest_algorithm}:{digest[:12]}"


class MemorySync:
    """A downstream record of what was published."""

    SUPPORTS_DRY_RUN = True
    name = "memory-sync"

    def __init__(self) -> None:
        self.published: List[PublishRequest] = []
        self.planned: List[PublishRequest] = []

    def intent(self) -> str:
        return "record the published release downstream"

    def publish(self, request: PublishRequest) -> PublisherResult:
        # A plan is asked what it would do, and what it would do is nothing: a
        # downstream record that a plan had written would be the plan's dry run
        # lying about having synced a release.
        if request.dry_run:
            self.planned.append(request)
            return contract.skipped(
                self.name,
                "a plan writes nothing downstream",
                code=DRY_RUN_CODE,
                identity=f"downstream:{request.tag}",
            )
        self.published.append(request)
        return contract.published(
            self.name,
            f"downstream:{request.tag}",
            external_id=f"downstream-{len(self.published)}",
            external_version=request.version,
        )


def github_publisher(
    repository: MemoryReleaseRepository,
    provenance: Optional[MemoryProvenance] = None,
) -> GitHubReleasePublisher:
    return GitHubReleasePublisher(
        repository=repository,
        provenance=provenance,
        name="github",
        destination=GITHUB_DESTINATION,
        notes=FixtureNotes(),
    )


def components(
    *,
    adapter: Optional[FixtureAdapter] = None,
    eligibility: Optional[FixtureEligibility] = None,
    version: Optional[Any] = None,
    notes: Optional[FixtureNotes] = None,
    release_pr: Optional[FixtureReleasePr] = None,
    publishers: Sequence[Any] = (),
    syncs: Sequence[Any] = (),
    adapters: Optional[Dict[str, Any]] = None,
) -> Any:
    from continuum.release.core import ReleaseComponents

    if adapters is None:
        the_adapter = adapter if adapter is not None else FixtureAdapter()
        adapters = {ADAPTER: the_adapter}
    return ReleaseComponents(
        eligibility=eligibility or FixtureEligibility(),
        version=version or ExplicitVersion(),
        notes=notes or FixtureNotes(),
        adapters=adapters,
        release_pr=release_pr,
        publishers=tuple(publishers),
        syncs=tuple(syncs),
    )


__all__ = [
    "ADAPTER",
    "Artifact",
    "ArtifactManifest",
    "CLASSIFIER",
    "ContractError",
    "DRY_RUN_CODE",
    "ExplicitVersion",
    "FixtureAdapter",
    "FixtureEligibility",
    "FixtureNotes",
    "FIXTURE",
    "FixtureReleasePr",
    "GITHUB_DESTINATION",
    "GitHubReleasePublisher",
    "IDENTITY",
    "Journal",
    "MemoryProvenance",
    "MemoryReleaseRepository",
    "MemorySync",
    "OTHER_SHA",
    "PortFailure",
    "ProjectFileVersion",
    "PublisherResult",
    "REPOSITORY",
    "SHA",
    "VERSION",
    "VERIFIER",
    "VersionPolicy",
    "components",
    "event",
    "github_publisher",
    "project_file",
    "targets",
]
