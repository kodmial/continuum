"""The GitHub Release baseline: draft, upload, attest, verify, then publish.

This is the shape the current GitHub supply-chain model expects, and the order
is the requirement rather than an implementation detail:

1. **A draft release is created first**, pointing at the exact commit. Not a
   published release with assets added to it, because a published release is a
   claim that the release is complete, and a claim that is briefly false is a
   claim somebody's installer reads.
2. **Every asset is uploaded against the draft**, including the checksum file
   and any provenance statement. An asset that is missing from a release is an
   artifact a consumer will look for and not find.
3. **The complete asset set is verified against the manifest**, read back from
   the destination rather than from our own bookkeeping. The claim being checked
   is "the release holds everything this release produced", and only the
   destination can answer it.
4. **Only then is the release published.** This is why the draft stage and the
   publish stage are separate stages in the state machine: the check is between
   them, and a state machine that has no place to put a check has no way to
   require one.

Two properties are enforced here that are easy to get wrong and expensive to
discover later:

**Assets are never replaced.** If the destination already holds an asset of the
same name with a different digest, this is a failure, not an upload. The
digests of a published release get recorded in downstream manifests — a formula,
a lockfile, an installer script — and replacing the bytes invalidates every
record that already points at them, silently. The reference implementation this
generalizes refused `--clobber` for exactly this reason.

**A tag is bound to a commit.** A draft or published release whose commit is not
the one this release is approved for is a conflict that cannot be resolved by
retrying. A tag names one commit; if the tag already points somewhere else, the
only correct outcome is a failure and a human.

Repositories with immutable releases enabled are supported without a separate
code path, because the properties above are already the ones immutable releases
enforce. The flag is recorded in the result so an operator can see that the
release is one of those, and so a caller can refuse to attempt any repair on a
published release.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .contract import (
    PROVENANCE_ATTESTED,
    PROVENANCE_DECLARED,
    TYPE_CHECKSUMS,
    ArtifactManifest,
    ContractError,
    PublishRequest,
    PublisherResult,
    failed,
    published,
    require_conform,
    skipped,
)

PUBLISHER_NAME = "github-release"

# The destination string that appears in a publication result. It is a component
# name, not a URL: a result that carried a URL would be a result that varies by
# host, and the identity is what dedupes on it.
DESTINATION = "github release"

DRY_RUN_CODE = "dry-run"


class ReleaseError(RuntimeError):
    """Raised when the destination cannot be used as a release repository.

    Distinct from a publication *failure*: this is "this object is not a release
    repository at all", which a caller fixes by wiring the right thing rather
    than by retrying.
    """


class PortFailure(RuntimeError):
    """A call into the destination failed in a way this class cannot classify."""

    def __init__(self, message: str, *, code: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _call_port(code: str, call: Any, *args: Any, **kwargs: Any) -> Any:
    """Call into the destination, turning any failure into a classified one.

    A destination is somebody else's object: it can raise an HTTP error, a
    `KeyError`, or its own domain error, and this class cannot enumerate what any
    of them will be. What it can guarantee is that none of them escapes as an
    unhandled traceback — a stack trace in a job log is not a publication
    result, and it would be read as a fault in Continuum rather than as a
    failure at a destination, which is the difference between re-running a job
    and opening an issue.
    """

    try:
        return call(*args, **kwargs)
    except PortFailure:
        # It already classified itself, and it knows more than a generic label
        # does: a port that says "the signing service is down" is telling the
        # caller something a blanket "attestation-failed" would throw away.
        raise
    except Exception as exc:
        raise PortFailure(f"{exc}", code=code) from exc


@dataclass(frozen=True)
class ReleaseRecord:
    """A release as the destination holds it."""

    id: str
    tag: str
    target_sha: str
    draft: bool
    name: str = ""
    url: str = ""
    immutable: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ReleaseError("a release record needs an id; it is the identity")
        if not self.tag:
            raise ReleaseError(f"release {self.id} has no tag")
        if not self.target_sha:
            raise ReleaseError(
                f"release {self.id} ({self.tag}) does not say which commit it points "
                "at. A release that does not name its commit cannot be checked "
                "against the one this run is approved for."
            )
        if not self.draft and not self.url:
            # A published release with no locator cannot be consumed, and a
            # missing locator is a destination problem worth failing on rather
            # than a value to default.
            raise ReleaseError(
                f"published release {self.id} ({self.tag}) has no URL; a consumer "
                "cannot be told where to download it"
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tag": self.tag,
            "target_sha": self.target_sha,
            "draft": self.draft,
            "name": self.name,
            "url": self.url,
            "immutable": self.immutable,
        }


@dataclass(frozen=True)
class ReleaseAsset:
    """An attached asset, identified the way the manifest identifies one."""

    name: str
    size: int
    digest: str
    digest_algorithm: str = "sha256"

    def matches(self, other: "ReleaseAsset") -> bool:
        return (
            self.name == other.name
            and self.size == other.size
            and self.digest == other.digest
            and self.digest_algorithm == other.digest_algorithm
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "digest": self.digest,
            "digest_algorithm": self.digest_algorithm,
        }


def expected_assets(manifests: Sequence[ArtifactManifest]) -> Tuple[ReleaseAsset, ...]:
    """What a release of these manifests must hold.

    Every artifact, evidence included, from every target. A checksum file that
    is not uploaded is an artifact set a consumer cannot verify, which is the
    same as an artifact set nobody can trust.
    """

    return tuple(
        ReleaseAsset(
            name=item.name,
            size=item.size,
            digest=item.digest,
            digest_algorithm=item.digest_algorithm,
        )
        for manifest in manifests
        for item in manifest.artifacts
    )


@dataclass(frozen=True)
class AssetSetReport:
    """The difference between what was built and what the release holds."""

    expected: Tuple[ReleaseAsset, ...]
    missing: Tuple[str, ...] = ()
    mismatched: Tuple[str, ...] = ()
    unexpected: Tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing and not self.mismatched

    def describe(self) -> Dict[str, Any]:
        return {
            "expected": [asset.describe() for asset in self.expected],
            "missing": list(self.missing),
            "mismatched": list(self.mismatched),
            "unexpected": list(self.unexpected),
            "complete": self.complete,
        }


def compare_asset_set(
    manifests: Sequence[ArtifactManifest], attached: Tuple[ReleaseAsset, ...]
) -> AssetSetReport:
    """Compare a release's attached assets against the manifest it must match.

    An unexpected asset is reported and not treated as a failure: a release may
    legitimately carry something this manifest does not describe — a signature
    added by a signing service, a build log attached by a human. Refusing to
    publish because a release holds *more* than expected would make a repair
    impossible, and the failure this guards against is a missing or wrong asset,
    not an extra one.
    """

    expected = {asset.name: asset for asset in expected_assets(manifests)}
    present = {asset.name: asset for asset in attached}
    missing = tuple(sorted(name for name in expected if name not in present))
    mismatched = tuple(
        sorted(
            name
            for name, asset in expected.items()
            if name in present and not present[name].matches(asset)
        )
    )
    unexpected = tuple(sorted(name for name in present if name not in expected))
    return AssetSetReport(
        expected=tuple(expected[name] for name in sorted(expected)),
        missing=missing,
        mismatched=mismatched,
        unexpected=unexpected,
    )


def _would_do(request: PublishRequest, record: Optional[ReleaseRecord]) -> str:
    """One sentence describing what a real run of this stage would do."""

    assets = expected_assets(request.manifests)
    names = ", ".join(asset.name for asset in assets) or "no assets"
    if request.dry_run:
        verb = "would"
    else:
        verb = "will"
    where = (
        f"resume the draft for {request.tag}"
        if record is not None
        else f"create a draft {request.tag} at {request.source_sha[:7]}"
    )
    return f"{verb} {where}, {verb} upload {len(assets)} asset(s) [{names}], and {verb} verify the set before publishing"


class GitHubReleasePublisher:
    """Publishes a release to a GitHub Releases-shaped destination.

    The destination is injected as a port, not opened here. That is what lets
    this class be tested against an in-memory repository with no network, and
    what lets a consumer's own release host be used without this class learning
    anything about it.
    """

    SUPPORTS_DRY_RUN = True

    def __init__(
        self,
        repository: Any,
        *,
        provenance: Optional[Any] = None,
        name: str = PUBLISHER_NAME,
        destination: str = DESTINATION,
        notes: str = "",
    ) -> None:
        require_conform(repository, "release-repository")
        if provenance is not None:
            require_conform(provenance, "provenance")
            if not str(getattr(provenance, "name", "")):
                raise ContractError(
                    "the provenance port does not name itself. An attestation is the "
                    "evidence a consumer uses to decide who built an artifact, and a "
                    "statement from a port that cannot name itself is not evidence of "
                    "anything in particular."
                )
        if not destination:
            raise ReleaseError(
                "a publisher must name its destination; a result that does not say "
                "where it went cannot be compared with anything"
            )
        self._repository = repository
        self._provenance = provenance
        self.name = name
        self._destination = destination
        self._notes = notes

    # -- intent -------------------------------------------------------------
    def intent(self) -> str:
        attested = " and attest them" if self._provenance is not None else ""
        return (
            f"create or resume the draft release, upload every asset{attested}, verify "
            "the complete asset set against the manifest, and publish only if the set "
            "is complete"
        )

    @property
    def destination(self) -> str:
        return self._destination

    # -- identity -----------------------------------------------------------
    def _identity(self, record: ReleaseRecord) -> str:
        """The immutable identity of a published release.

        The tag alone is not enough: a tag is a name, and what makes it a release
        is that it points at one commit. Recording both means a later run that
        proposes the same version from a different commit is recognisable as a
        contradiction rather than as a duplicate.
        """

        return f"{record.tag}@{record.target_sha}"

    # -- draft --------------------------------------------------------------
    def draft(self, request: PublishRequest) -> PublisherResult:
        """Create or complete the unpublished release that will hold the assets."""

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal

        record = self._repository.find_by_tag(request.tag)
        if request.dry_run:
            return skipped(
                self._destination,
                _would_do(request, record),
                identity=(
                    self._identity(record) if record is not None else f"{request.tag}@{request.source_sha}"
                ),
                code=DRY_RUN_CODE,
                details={"assets": str(len(expected_assets(request.manifests)))},
            )
        if record is not None and not record.draft:
            return skipped(
                self._destination,
                f"{request.tag} is already published; there is no draft to complete",
                identity=self._identity(record),
                external_id=record.id,
                external_version=record.tag,
                code="already-published",
            )
        if record is not None and record.target_sha != request.source_sha:
            return failed(
                self._destination,
                f"the draft for {request.tag} points at {record.target_sha} but this "
                f"release is approved for {request.source_sha}. A tag names one commit; "
                "re-uploading assets cannot change which one it names.",
                code="draft-source-conflict",
                retryable=False,
                identity=self._identity(record),
                external_id=record.id,
            )

        if record is None:
            record = self._repository.create_draft(
                tag=request.tag,
                name=request.tag,
                target_sha=request.source_sha,
                notes=self._notes,
            )
        if record is None:
            return failed(
                self._destination,
                f"the draft release for {request.tag} could not be created",
                code="draft-not-created",
                retryable=True,
            )

        return self._complete_draft(request, record)

    def _complete_draft(
        self, request: PublishRequest, record: ReleaseRecord
    ) -> PublisherResult:
        attached = {asset.name: asset for asset in self._repository.assets(record)}
        uploaded: List[str] = []
        reused: List[str] = []
        for artifact in request.artifacts:
            expected = ReleaseAsset(
                name=artifact.name,
                size=artifact.size,
                digest=artifact.digest,
                digest_algorithm=artifact.digest_algorithm,
            )
            present = attached.get(artifact.name)
            if present is not None:
                if present.matches(expected):
                    reused.append(artifact.name)
                    continue
                return failed(
                    self._destination,
                    f"{artifact.name} is already attached to {request.tag} with "
                    f"{present.digest_algorithm}:{present.digest}, and this build "
                    f"produced {expected.digest_algorithm}:{expected.digest}. Replacing "
                    "it would invalidate every manifest that already records the "
                    "published digest, so this is refused rather than overwritten.",
                    code="asset-conflict",
                    retryable=False,
                    identity=self._identity(record),
                    external_id=record.id,
                    details={"asset": artifact.name},
                )
            if not os.path.isfile(artifact.path):
                return failed(
                    self._destination,
                    f"{artifact.name} is in the manifest at {artifact.path!r}, which is "
                    "not a readable file on this runner. Uploading it would attach "
                    "nothing while the release claims to hold it.",
                    code="artifact-missing",
                    retryable=True,
                    identity=self._identity(record),
                    external_id=record.id,
                    details={"asset": artifact.name},
                )
            try:
                _call_port("asset-upload-failed", self._repository.upload, record, expected, artifact.path)
            except PortFailure as exc:
                return failed(
                    self._destination,
                    f"{artifact.name} could not be uploaded to {request.tag}: {exc}",
                    code=exc.code,
                    retryable=exc.retryable,
                    identity=self._identity(record),
                    external_id=record.id,
                    details={"asset": artifact.name},
                )
            uploaded.append(artifact.name)

        attested = self._attest(request, record)
        if isinstance(attested, PublisherResult):
            return attested

        return published(
            self._destination,
            self._identity(record),
            external_id=record.id,
            external_version=record.tag,
            code="drafted",
            details={
                "uploaded": ",".join(uploaded) or "-",
                "reused": ",".join(reused) or "-",
                "attestations": ",".join(attested) or "-",
                "immutable": "true" if record.immutable else "false",
            },
        )

    def _attest(
        self, request: PublishRequest, record: ReleaseRecord
    ) -> Union[List[str], PublisherResult]:
        """Attach provenance statements for the artifacts that declare one.

        An artifact the adapter already attested is left alone: re-attesting
        would produce a second statement for the same digest, and two statements
        for one artifact is a question a consumer has to answer rather than a
        property they can rely on.
        """

        if self._provenance is None:
            return []
        attached: List[str] = []
        for artifact in request.artifacts:
            if artifact.provenance == PROVENANCE_ATTESTED:
                attached.append(f"{artifact.name}={artifact.attestation}")
                continue
            if artifact.provenance != PROVENANCE_DECLARED:
                continue
            try:
                statement = _call_port(
                    "attestation-failed",
                    self._provenance.attest,
                    tag=record.tag,
                    name=artifact.name,
                    digest_algorithm=artifact.digest_algorithm,
                    digest=artifact.digest,
                    source_sha=artifact.source_sha,
                )
            except PortFailure as exc:
                return failed(
                    self._destination,
                    f"{artifact.name} could not be attested for {record.tag}: {exc}. The "
                    "release is left as a draft; nothing is published with a declared "
                    "artifact and no evidence.",
                    code=exc.code,
                    retryable=exc.retryable,
                    identity=self._identity(record),
                    external_id=record.id,
                    details={"asset": artifact.name},
                )
            attached.append(f"{artifact.name}={statement}")
        return attached

    # -- publish ------------------------------------------------------------
    def publish(self, request: PublishRequest) -> PublisherResult:
        """Verify the release holds everything it must, then publish it."""

        refusal = _refuse_unpublishable(request)
        if refusal is not None:
            return refusal
        if request.dry_run:
            # Before anything is looked up: a plan describes the work, and "there
            # is no release for this tag yet" is what a plan should say, not a
            # failure of the plan.
            return skipped(
                self._destination,
                f"would verify the complete asset set of {request.tag} against "
                f"{len(expected_assets(request.manifests))} manifest artifact(s) and "
                "publish it",
                identity=f"{request.tag}@{request.source_sha}",
                code=DRY_RUN_CODE,
            )

        record = self._repository.find_by_tag(request.tag)
        identity = (
            self._identity(record)
            if record is not None
            else f"{request.tag}@{request.source_sha}"
        )
        if record is None:
            return failed(
                self._destination,
                f"there is no release for {request.tag}, so there is nothing to "
                "publish. The draft stage has to run first; this is safe to retry.",
                code="release-absent",
                retryable=True,
            )
        if record.target_sha != request.source_sha:
            return failed(
                self._destination,
                f"the release for {request.tag} points at {record.target_sha} but this "
                f"release is approved for {request.source_sha}. Publishing it would "
                "publish bytes from a commit that was never approved for this version.",
                code="release-source-conflict",
                retryable=False,
                identity=identity,
                external_id=record.id,
            )
        if not record.draft:
            return skipped(
                self._destination,
                f"{request.tag} is already published",
                identity=identity,
                external_id=record.id,
                external_version=record.tag,
                code="already-published",
            )
        if request.dry_run:
            return skipped(
                self._destination,
                f"would verify the complete asset set of {request.tag} and publish it",
                identity=identity,
                external_id=record.id,
                external_version=record.tag,
                code=DRY_RUN_CODE,
            )

        attached = tuple(self._repository.assets(record))
        report = compare_asset_set(request.manifests, attached)
        if report.missing:
            return failed(
                self._destination,
                f"{request.tag} is missing {', '.join(report.missing)}; publishing it "
                "would announce a release that does not hold everything it was built "
                "from. The draft stage can be run again to attach what is missing.",
                code="asset-set-incomplete",
                retryable=True,
                identity=identity,
                external_id=record.id,
                details={"missing": ",".join(report.missing)},
            )
        if report.mismatched:
            return failed(
                self._destination,
                f"{request.tag} holds asset(s) whose digest does not match this build: "
                f"{', '.join(report.mismatched)}. Refusing to publish, because the "
                "attached bytes are not the bytes this release was approved for.",
                code="asset-digest-mismatch",
                retryable=False,
                identity=identity,
                external_id=record.id,
                details={"mismatched": ",".join(report.mismatched)},
            )
        bad_checksums = _checksum_mismatches(request.manifests, attached)
        if bad_checksums:
            return failed(
                self._destination,
                f"the checksum file attached to {request.tag} does not match the "
                f"manifest ({', '.join(bad_checksums)}). A checksum file that disagrees "
                "with the artifacts it describes is worse than no checksum file.",
                code="checksums-mismatch",
                retryable=False,
                identity=identity,
                external_id=record.id,
                details={"mismatched": ",".join(bad_checksums)},
            )

        try:
            promoted = _call_port("promote-failed", self._repository.publish, record)
        except PortFailure as exc:
            return failed(
                self._destination,
                f"{request.tag} could not be published: {exc}. The draft and its assets "
                "are intact, so this is safe to retry.",
                code=exc.code,
                retryable=exc.retryable,
                identity=identity,
                external_id=record.id,
            )
        return published(
            self._destination,
            self._identity(promoted),
            external_id=promoted.id,
            external_version=promoted.tag,
            code="published",
            details={
                "assets": str(len(report.expected)),
                "url": promoted.url,
                "immutable": "true" if promoted.immutable else "false",
            },
        )


def _refuse_unpublishable(request: PublishRequest) -> Optional[PublisherResult]:
    """The last gate before anything is written to a destination.

    Every manifest is checked against the approved source and version here as
    well as in the state machine, because this is the last code between a build
    and the outside world and a check that exists only further up is a check a
    future caller can reach past.

    A plan is exempt. Its artifacts are declared rather than built, so the
    invariants a real run insists on are exactly the ones a plan is reporting
    are not yet established; failing a plan on them would report a broken
    release rather than an unbuilt one.
    """

    if request.dry_run:
        return None
    for manifest in request.manifests:
        try:
            manifest.assert_publishable(request.source_sha, request.version)
        except ContractError as exc:
            return failed(
                request.destination,
                str(exc),
                code="manifest-not-publishable",
                retryable=False,
            )
    return None


def _checksum_mismatches(
    manifests: Sequence[ArtifactManifest], attached: Tuple[ReleaseAsset, ...]
) -> Tuple[str, ...]:
    """Checksum files whose attached bytes disagree with the manifest.

    Only worth checking when a checksum file is present, and worth checking
    because a checksum file is the one asset a consumer trusts without reading
    the manifest: if it is wrong, every verification a consumer performs with it
    fails, or — worse — passes against the wrong file.
    """

    attached_by_name = {asset.name: asset for asset in attached}
    bad: List[str] = []
    for manifest in manifests:
        expected = manifest.checksums_digest()
        for artifact in manifest.of_type(TYPE_CHECKSUMS):
            present = attached_by_name.get(artifact.name)
            if present is None or present.digest == expected:
                continue
            bad.append(artifact.name)
    return tuple(bad)


__all__ = [
    "AssetSetReport",
    "DESTINATION",
    "DRY_RUN_CODE",
    "GitHubReleasePublisher",
    "PUBLISHER_NAME",
    "PortFailure",
    "ReleaseAsset",
    "ReleaseError",
    "ReleaseRecord",
    "compare_asset_set",
    "expected_assets",
]
