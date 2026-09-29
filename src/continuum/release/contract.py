"""The release target contract: what every adapter owes, and what it reports back.

A release is the only part of Continuum whose output other people install, so
the two things it produces — a set of artifacts and a set of publication
outcomes — are defined here as *values* rather than as whatever an adapter
happened to print. The vocabulary is deliberately dull: a manifest row carries
an id, a source SHA, a version, a name, a type, a size, a digest, and three
statuses. An Android adapter, a JVM adapter, and a reference fixture all fill in
the same fields, which is what makes "these bytes came from that commit"
checkable without knowing which toolchain made them.

Three rules carry the weight, and each one exists because of a way a release has
already gone wrong:

**A digest is identity, and a claim is not a fact.** An artifact that has not
been observed on disk is marked `declared`, and a declared artifact cannot
report itself as verified or signed. The failure this prevents is a dry run that
prints a manifest of files that were never built, which is indistinguishable
from a real manifest until someone installs the release.

**"We could not check this" is a different value from "we checked it".** The
statuses are three-valued, not boolean. `verified` additionally requires the
name of what verified it, because a self-consistent signature from the wrong
key passes every check a boolean can express. This is the same lesson the Apple
adapter encodes in `pinned_identity`: the ability to assert has to be recorded
separately from the assertion.

**One source SHA per manifest.** The core pins a release to an exact source SHA
and refuses to publish anything that was built from another one. The manifest
enforces it at construction, where a mistake is still correctable, and the core
re-checks it at publish time, where a mistake is no longer correctable.

The ports at the bottom are the seams. Eligibility, versioning, notes, build,
signing, verification, publication, and downstream sync are separate interfaces
with separate implementations, so the state machine in `core.py` never learns
what a keystore or a package index is. Adding a platform is a new module that
implements `TargetAdapter`; it is never an edit to the core.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MANIFEST_SCHEMA = "continuum.artifact-manifest/v1"
PUBLISHER_RESULT_SCHEMA = "continuum.publisher-result/v1"

# The contract version an adapter implements. It is reported in every manifest
# so a consumer reading a published artifact set can tell which rules produced
# it, and it is checked by `conform()` so a stale adapter is refused at
# registration rather than at publication.
CONTRACT_VERSION = 1

# -- artifact vocabulary -----------------------------------------------------
#
# A type is a *handling* claim, not a format: it says what the core is allowed
# to assume about the bytes (an `archive` is unpacked, a `checksums` file is the
# manifest of the others, a `provenance` file is evidence rather than something
# a user installs). The set is intentionally small. A vocabulary that grows per
# platform is a vocabulary nothing can rely on.
TYPE_BINARY = "binary"
TYPE_ARCHIVE = "archive"
TYPE_BUNDLE = "bundle"
TYPE_INSTALLER = "installer"
TYPE_PACKAGE = "package"
TYPE_CHECKSUMS = "checksums"
TYPE_PROVENANCE = "provenance"
TYPE_SIGNATURE = "signature"
SUPPORTED_ARTIFACT_TYPES: Tuple[str, ...] = (
    TYPE_BINARY,
    TYPE_ARCHIVE,
    TYPE_BUNDLE,
    TYPE_INSTALLER,
    TYPE_PACKAGE,
    TYPE_CHECKSUMS,
    TYPE_PROVENANCE,
    TYPE_SIGNATURE,
)

# The types that describe *other* artifacts. They are published and verified
# like anything else, but they are never installed, so a consumer that installs
# every published asset wants these excluded.
EVIDENCE_ARTIFACT_TYPES: Tuple[str, ...] = (TYPE_CHECKSUMS, TYPE_PROVENANCE, TYPE_SIGNATURE)

# -- signing vocabulary ------------------------------------------------------
#
# `degraded` is a first-class value rather than a note. A release signed ad-hoc
# is installable and must never be mistaken for one signed by a stable identity,
# so the manifest says so in the row itself, and the Apple adapter's whole
# degraded-plan discipline depends on there being a word for it.
SIGNING_SIGNED = "signed"
SIGNING_UNSIGNED = "unsigned"
SIGNING_DEGRADED = "degraded"
SIGNING_NOT_APPLICABLE = "not-applicable"
SUPPORTED_SIGNING_STATUSES: Tuple[str, ...] = (
    SIGNING_SIGNED,
    SIGNING_UNSIGNED,
    SIGNING_DEGRADED,
    SIGNING_NOT_APPLICABLE,
)

# -- verification vocabulary -------------------------------------------------
#
# `unverified` is the default and the normal state of an artifact that has just
# been built. A two-valued field would force a build to say either "verified"
# (a lie) or "not verified" (which reads as a failure), and both readings would
# then be acted on.
VERIFICATION_VERIFIED = "verified"
VERIFICATION_FAILED = "failed"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_NOT_APPLICABLE = "not-applicable"
SUPPORTED_VERIFICATION_STATUSES: Tuple[str, ...] = (
    VERIFICATION_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_NOT_APPLICABLE,
)

# -- provenance vocabulary ---------------------------------------------------
#
# `attested` requires an attestation identity: evidence that somebody vouched
# for the build is a reference to that somebody, not a boolean.
PROVENANCE_ATTESTED = "attested"
PROVENANCE_DECLARED = "declared"
PROVENANCE_ABSENT = "absent"
SUPPORTED_PROVENANCE_STATES: Tuple[str, ...] = (
    PROVENANCE_ATTESTED,
    PROVENANCE_DECLARED,
    PROVENANCE_ABSENT,
)

# -- publisher vocabulary ----------------------------------------------------
#
# Three outcomes, no fourth. `skipped` is a *green* outcome and says why, which
# is what makes "this repository already released this version" a normal,
# expected result of a duplicate event rather than a red job.
PUBLISHED = "published"
SKIPPED = "skipped"
FAILED = "failed"
SUPPORTED_PUBLISH_OUTCOMES: Tuple[str, ...] = (PUBLISHED, SKIPPED, FAILED)

# -- event vocabulary --------------------------------------------------------

EVENT_PUSH = "push"
EVENT_TAG_PUSH = "tag-push"
EVENT_DISPATCH = "workflow-dispatch"
EVENT_RELEASE_PR_MERGED = "release-pr-merged"
EVENT_SCHEDULED = "scheduled"
SUPPORTED_EVENT_NAMES: Tuple[str, ...] = (
    EVENT_PUSH,
    EVENT_TAG_PUSH,
    EVENT_DISPATCH,
    EVENT_RELEASE_PR_MERGED,
    EVENT_SCHEDULED,
)

# A git object name. GitHub is SHA-1 today and SHA-256 capable, so both widths
# are accepted; a value that is neither is a truncated or mis-transcribed SHA
# and must never reach a manifest.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

_DIGEST_LENGTHS: Dict[str, int] = {"sha256": 64, "sha512": 128}
SUPPORTED_DIGEST_ALGORITHMS: Tuple[str, ...] = tuple(sorted(_DIGEST_LENGTHS))

_HEX_RE = re.compile(r"^[0-9a-f]+$")

# An asset name is a flat name: it is what a download URL carries, what a
# checksum file lists, and what an attestation is bound to. A separator in it
# means two different things are being called one artifact.
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")

# A target id names a target in a command line and in every manifest row, so it
# carries the same shape a release target id already does.
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ContractError(ValueError):
    """Raised when a component does not honour the release target contract.

    Carries a `code` so the chain can record which half of the contract was
    broken, and a `retryable` flag for the same reason publishers carry one: an
    adapter that cannot find its toolchain will not find it on a retry, and one
    that lost a temporary directory might.
    """

    def __init__(
        self, message: str, *, code: str = "contract", retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def is_source_sha(value: str) -> bool:
    return bool(_SHA_RE.match(value or ""))


def require_source_sha(value: str, where: str) -> str:
    if not is_source_sha(value):
        raise ContractError(
            f"{where} needs a full source SHA to pin the release to; {value!r} is not "
            "one. An abbreviated or missing SHA cannot be compared against the commit "
            "that was built, which is the comparison the whole release rests on."
        )
    return value


# -- normalized values -------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """One artifact of one release: what it is, and what is known about it.

    Every field is either an observation or an explicit absence. `declared` is
    the flag that separates them: an artifact marked declared is what a plan
    says will exist, and it may not claim a signature or a verification it has
    not got.
    """

    target: str
    source_sha: str
    version: str
    name: str
    path: str
    type: str
    size: int
    digest: str
    digest_algorithm: str = "sha256"
    platform: str = ""
    arch: str = ""
    classifier: str = ""
    media_type: str = ""
    signing: str = SIGNING_UNSIGNED
    signing_identity: str = ""
    verification: str = VERIFICATION_UNVERIFIED
    verified_by: str = ""
    provenance: str = PROVENANCE_ABSENT
    attestation: str = ""
    declared: bool = False

    def __post_init__(self) -> None:
        if not _TARGET_ID_RE.match(self.target or ""):
            raise ContractError(
                f"artifact {self.name!r}: target id {self.target!r} must be a lowercase "
                "slug; it names a row in every manifest the release publishes"
            )
        require_source_sha(self.source_sha, f"artifact {self.name!r}")
        if not self.version:
            raise ContractError(
                f"artifact {self.name!r}: an artifact without a version cannot be "
                "matched to the release that shipped it"
            )
        if not _ARTIFACT_NAME_RE.match(self.name or ""):
            raise ContractError(
                f"artifact name {self.name!r} must be a flat name of letters, digits, "
                "'.', '_', '+' or '-': it is a download name, a checksum line, and an "
                "attestation subject"
            )
        if not self.path:
            raise ContractError(f"artifact {self.name!r}: an artifact needs a path")
        if self.type not in SUPPORTED_ARTIFACT_TYPES:
            raise ContractError(
                f"artifact {self.name!r}: unknown type {self.type!r}; supported: "
                + ", ".join(SUPPORTED_ARTIFACT_TYPES)
            )
        if self.size < 0:
            raise ContractError(f"artifact {self.name!r}: size cannot be negative")
        self._check_digest()
        self._check_signing()
        self._check_verification()
        self._check_provenance()

    def _check_digest(self) -> None:
        expected = _DIGEST_LENGTHS.get(self.digest_algorithm)
        if expected is None:
            raise ContractError(
                f"artifact {self.name!r}: unsupported digest algorithm "
                f"{self.digest_algorithm!r}; supported: "
                + ", ".join(SUPPORTED_DIGEST_ALGORITHMS)
            )
        if not _HEX_RE.match(self.digest or "") or len(self.digest) != expected:
            raise ContractError(
                f"artifact {self.name!r}: the {self.digest_algorithm} digest must be "
                f"{expected} lowercase hex characters; a short or malformed digest "
                "cannot identify the bytes it names"
            )

    def _check_signing(self) -> None:
        if self.signing not in SUPPORTED_SIGNING_STATUSES:
            raise ContractError(
                f"artifact {self.name!r}: unknown signing status {self.signing!r}; "
                "supported: " + ", ".join(SUPPORTED_SIGNING_STATUSES)
            )
        if self.signing == SIGNING_SIGNED and not self.signing_identity:
            raise ContractError(
                f"artifact {self.name!r} claims a signature but names no signing "
                "identity. Asserting that a signature is *the expected one* needs a "
                "name to compare against; without it, any signature at all passes."
            )
        if self.signing != SIGNING_SIGNED and self.signing_identity:
            raise ContractError(
                f"artifact {self.name!r} reports signing status {self.signing!r} and "
                f"also names a signing identity ({self.signing_identity!r}); the two "
                "claims cannot both be true"
            )

    def _check_verification(self) -> None:
        if self.verification not in SUPPORTED_VERIFICATION_STATUSES:
            raise ContractError(
                f"artifact {self.name!r}: unknown verification status "
                f"{self.verification!r}; supported: "
                + ", ".join(SUPPORTED_VERIFICATION_STATUSES)
            )
        if self.verification == VERIFICATION_VERIFIED and not self.verified_by:
            raise ContractError(
                f"artifact {self.name!r} claims verification but names no verifier. "
                "'Verified' with nothing to compare against is a claim, not a check."
            )
        if self.declared and self.verification not in (
            VERIFICATION_UNVERIFIED,
            VERIFICATION_NOT_APPLICABLE,
        ):
            raise ContractError(
                f"artifact {self.name!r} is declared by a plan and therefore does not "
                f"exist yet, so it cannot report verification status "
                f"{self.verification!r}"
            )
        if self.declared and self.signing == SIGNING_SIGNED:
            raise ContractError(
                f"artifact {self.name!r} is declared by a plan and therefore does not "
                "exist yet, so it cannot report a signature"
            )

    def _check_provenance(self) -> None:
        if self.provenance not in SUPPORTED_PROVENANCE_STATES:
            raise ContractError(
                f"artifact {self.name!r}: unknown provenance state {self.provenance!r}; "
                "supported: " + ", ".join(SUPPORTED_PROVENANCE_STATES)
            )
        if self.provenance == PROVENANCE_ATTESTED and not self.attestation:
            raise ContractError(
                f"artifact {self.name!r} claims an attestation without naming it. "
                "Attestation is a reference to a signed statement, not a status."
            )
        if self.provenance != PROVENANCE_ATTESTED and self.attestation:
            raise ContractError(
                f"artifact {self.name!r} reports provenance {self.provenance!r} and "
                f"also names an attestation ({self.attestation!r})"
            )

    @property
    def is_evidence(self) -> bool:
        return self.type in EVIDENCE_ARTIFACT_TYPES

    @property
    def signed(self) -> bool:
        return self.signing == SIGNING_SIGNED

    @property
    def verified(self) -> bool:
        return self.verification == VERIFICATION_VERIFIED

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.digest_algorithm}:{self.digest}"

    def assert_release_source(self, source_sha: str) -> None:
        """Refuse an artifact that was not built from the approved commit."""

        if self.source_sha == source_sha:
            return
        raise ContractError(
            f"artifact {self.name!r} of target {self.target!r} was built from "
            f"{self.source_sha} but this release is approved for {source_sha}. "
            "Publishing it would ship bytes that no reviewed commit produced."
        )

    def assert_release_version(self, version: str) -> None:
        if self.version == version:
            return
        raise ContractError(
            f"artifact {self.name!r} of target {self.target!r} carries version "
            f"{self.version!r} but this release is {version!r}"
        )

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "target": self.target,
            "source_sha": self.source_sha,
            "version": self.version,
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "size": self.size,
            "digest": self.digest,
            "digest_algorithm": self.digest_algorithm,
            "signing": self.signing,
            "verification": self.verification,
            "provenance": self.provenance,
            "declared": self.declared,
        }
        for key, value in (
            ("platform", self.platform),
            ("arch", self.arch),
            ("classifier", self.classifier),
            ("media_type", self.media_type),
            ("signing_identity", self.signing_identity),
            ("verified_by", self.verified_by),
            ("attestation", self.attestation),
        ):
            if value:
                payload[key] = value
        return payload


EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()

#: The conventional name of a checksum file. Not a rule — an adapter may call it
#: whatever it likes — but the name a consumer's documentation will mention, so
#: it is a constant rather than a string repeated in three places.
CHECKSUMS_FILE = "SHA256SUMS.txt"


def checksums_document(artifacts: "Sequence[Artifact]") -> str:
    """The canonical checksum listing for a set of artifacts.

    Sorted by name and in the two-column form every checksum tool reads, because
    the point of this file is that a consumer can run one command against it
    without knowing anything about the tool that produced it. The digest is of
    the same bytes a consumer will verify, which is what lets a publisher check
    an attached checksum file against a manifest instead of trusting it.
    """

    lines = [
        f"{item.digest}  {item.name}"
        for item in sorted(artifacts, key=lambda entry: entry.name)
    ]
    return "".join(f"{line}\n" for line in lines)



@dataclass(frozen=True)
class ArtifactManifest:
    """Everything one target produced for one release, in one vocabulary.

    The manifest is a value, so it can be printed, diffed between two runs, and
    asserted on without a build toolchain in sight. It carries no timestamp:
    "when" is the journal's business, and a timestamp would make two identical
    releases differ, which is exactly the comparison reproducibility is for.
    """

    target: str
    source_sha: str
    version: str
    adapter: str
    contract_version: int = CONTRACT_VERSION
    artifacts: Tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        if not _TARGET_ID_RE.match(self.target or ""):
            raise ContractError(
                f"manifest target id {self.target!r} must be a lowercase slug"
            )
        require_source_sha(self.source_sha, f"manifest for target {self.target!r}")
        if not self.version:
            raise ContractError(f"manifest for target {self.target!r} has no version")
        if not self.adapter:
            raise ContractError(
                f"manifest for target {self.target!r} does not say which adapter "
                "produced it, so a reader cannot tell which rules were applied"
            )
        if not self.artifacts:
            raise ContractError(
                f"manifest for target {self.target!r} has no artifacts; a target that "
                "built nothing must say so by not producing a manifest at all"
            )
        seen: Dict[str, str] = {}
        for artifact in self.artifacts:
            if artifact.name in seen:
                raise ContractError(
                    f"manifest for target {self.target!r} lists {artifact.name!r} twice; "
                    "two artifacts with one name cannot both be uploaded"
                )
            seen[artifact.name] = artifact.digest
            if artifact.target != self.target:
                raise ContractError(
                    f"manifest for target {self.target!r} contains an artifact "
                    f"labelled {artifact.target!r}"
                )
            artifact.assert_release_source(self.source_sha)
            artifact.assert_release_version(self.version)

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(artifact.name for artifact in self.artifacts)

    def by_name(self, name: str) -> Artifact:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        raise ContractError(
            f"manifest for target {self.target!r} has no artifact {name!r}; it has: "
            + ", ".join(self.names)
        )

    def of_type(self, *types: str) -> Tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.type in types)

    @property
    def installable(self) -> Tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if not item.is_evidence)

    @property
    def all_verified(self) -> bool:
        return all(
            item.verification == VERIFICATION_VERIFIED for item in self.artifacts
        )

    def unverified(self) -> Tuple[Artifact, ...]:
        return tuple(
            item
            for item in self.artifacts
            if item.verification != VERIFICATION_VERIFIED
        )

    @property
    def checksummed(self) -> Tuple[Artifact, ...]:
        """The artifacts a checksum file lists.

        A checksum file does not list itself. That is not a detail: a manifest
        whose checksums file listed its own digest would have a digest that
        depends on itself, and no file could satisfy it.
        """

        return tuple(item for item in self.artifacts if item.type != TYPE_CHECKSUMS)

    def checksums_listing(self) -> str:
        return checksums_document(self.checksummed)

    def checksums_digest(self) -> str:
        return hashlib.sha256(self.checksums_listing().encode("utf-8")).hexdigest()

    def write_checksums(self, directory: str, name: str = CHECKSUMS_FILE) -> str:
        """Write this manifest's checksum file, and return its path.

        Written from the manifest rather than from the directory, so the file
        describes what was built even when a build directory also holds
        intermediates. An adapter records the result with `record()` and the
        publisher re-derives the digest to check it.
        """

        path = os.path.join(directory, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.checksums_listing())
        return path

    def digest(self) -> str:
        """A stable digest of the whole manifest, used as an idempotency input.

        Sorted by name so the value does not depend on the order an adapter
        happened to record its artifacts in.
        """

        joined = "\n".join(
            f"{item.name} {item.digest_algorithm}:{item.digest} {item.size}"
            for item in sorted(self.artifacts, key=lambda entry: entry.name)
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def assert_release_source(self, source_sha: str) -> None:
        """Refuse a manifest built from any commit but the approved one."""

        require_source_sha(source_sha, "approved release source")
        if self.source_sha != source_sha:
            raise ContractError(
                f"manifest for target {self.target!r} was built from {self.source_sha}, "
                f"not from the approved {source_sha}"
            )

    def assert_release_version(self, version: str) -> None:
        if self.version != version:
            raise ContractError(
                f"manifest for target {self.target!r} carries version {self.version!r}, "
                f"not the release version {version!r}"
            )

    def mark_verified(self, verified_by: str) -> "ArtifactManifest":
        """This manifest, with every artifact marked verified by ``verified_by``.

        Verification is a claim about an artifact, and a claim belongs on the
        artifact's own row rather than in a report that the publication path has
        to remember to read. So the core asks the adapter whether the artifacts
        verify, and then records the answer where the upload, the checksum, and
        the attestation will all find it.
        """

        if not verified_by:
            raise ContractError(
                "a manifest cannot record a verification without saying who performed it"
            )
        return ArtifactManifest(
            target=self.target,
            source_sha=self.source_sha,
            version=self.version,
            adapter=self.adapter,
            artifacts=tuple(
                replace(item, verification=VERIFICATION_VERIFIED, verified_by=verified_by)
                for item in self.artifacts
            ),
        )

    def assert_publishable(self, source_sha: str, version: str) -> None:
        """Fail closed unless every artifact belongs to this exact release."""

        require_source_sha(source_sha, "approved release source")
        if self.source_sha != source_sha:
            raise ContractError(
                f"manifest for target {self.target!r} was built from {self.source_sha}, "
                f"not from the approved {source_sha}"
            )
        if self.version != version:
            raise ContractError(
                f"manifest for target {self.target!r} carries version {self.version!r}, "
                f"not the release version {version!r}"
            )
        unverified = self.unverified()
        if unverified:
            raise ContractError(
                f"manifest for target {self.target!r} has unverified artifact(s): "
                + ", ".join(item.name for item in unverified)
                + ". A release publishes what was checked, not what was hoped for."
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "contract_version": self.contract_version,
            "target": self.target,
            "adapter": self.adapter,
            "source_sha": self.source_sha,
            "version": self.version,
            "digest": self.digest(),
            "artifacts": [item.describe() for item in self.artifacts],
        }


def digest_file(path: str, algorithm: str = "sha256") -> Tuple[int, str]:
    """Size and digest of a file, read in bounded blocks.

    A release artifact can be larger than memory, so this never loads one. An
    empty digest is returned only for a file that could be opened and read to its
    end, which is what makes a digest a fact rather than an assumption.
    """

    if algorithm not in SUPPORTED_DIGEST_ALGORITHMS:
        raise ContractError(f"unsupported digest algorithm {algorithm!r}")
    digester = hashlib.new(algorithm)
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digester.update(block)
    return size, digester.hexdigest()


class ManifestBuilder:
    """How an adapter reports what it produced.

    An adapter never constructs an `Artifact` by hand. It records paths, and the
    builder measures them: a manifest row that says a file is 4 KiB when the
    file is 4 KiB is a claim nobody has to maintain by hand, and one that says
    so when the file is missing is a failure at the point where the file is
    missing, not at publication.
    """

    def __init__(self, target: str, adapter: str, source_sha: str, version: str) -> None:
        if not _TARGET_ID_RE.match(target or ""):
            raise ContractError(f"target id {target!r} must be a lowercase slug")
        require_source_sha(source_sha, f"build of target {target!r}")
        self._target = target
        self._adapter = adapter
        self._source_sha = source_sha
        self._version = version
        self._artifacts: List[Artifact] = []

    def record(
        self,
        name: str,
        path: str,
        type: str,
        *,
        platform: str = "",
        arch: str = "",
        classifier: str = "",
        media_type: str = "",
        signing: str = SIGNING_UNSIGNED,
        signing_identity: str = "",
        verification: str = VERIFICATION_UNVERIFIED,
        verified_by: str = "",
        provenance: str = PROVENANCE_ABSENT,
        attestation: str = "",
        digest_algorithm: str = "sha256",
    ) -> Artifact:
        if not os.path.isfile(path):
            raise ContractError(
                f"target {self._target!r} reported artifact {name!r} at {path!r}, which "
                "does not exist. A manifest describes files; one that describes a file "
                "that was never written is a description of nothing."
            )
        size, digest = digest_file(path, digest_algorithm)
        artifact = Artifact(
            target=self._target,
            source_sha=self._source_sha,
            version=self._version,
            name=name,
            path=path,
            type=type,
            size=size,
            digest=digest,
            digest_algorithm=digest_algorithm,
            platform=platform,
            arch=arch,
            classifier=classifier,
            media_type=media_type,
            signing=signing,
            signing_identity=signing_identity,
            verification=verification,
            verified_by=verified_by,
            provenance=provenance,
            attestation=attestation,
        )
        self._artifacts.append(artifact)
        return artifact

    def declare(
        self,
        name: str,
        path: str,
        type: str,
        *,
        platform: str = "",
        arch: str = "",
        classifier: str = "",
        media_type: str = "",
        signing: str = SIGNING_UNSIGNED,
        digest_algorithm: str = "sha256",
    ) -> Artifact:
        """Record an artifact a plan says it will produce.

        Only the shape is known, so only the shape is claimed: the digest is the
        digest of no bytes, the verification is `unverified`, and `declared` is
        set so nothing downstream can mistake this row for a shipped one.
        """

        artifact = Artifact(
            target=self._target,
            source_sha=self._source_sha,
            version=self._version,
            name=name,
            path=path,
            type=type,
            size=0,
            digest=EMPTY_DIGEST,
            digest_algorithm=digest_algorithm,
            platform=platform,
            arch=arch,
            classifier=classifier,
            media_type=media_type,
            signing=signing,
            declared=True,
        )
        self._artifacts.append(artifact)
        return artifact

    def replace(self, artifact: Artifact) -> Artifact:
        """Record a later state of an artifact, such as its signed form.

        Signing changes the bytes, so it changes the digest. Replacing the row
        rather than appending keeps one name to one artifact, which is the
        invariant the asset upload depends on.
        """

        for index, existing in enumerate(self._artifacts):
            if existing.name == artifact.name:
                self._artifacts[index] = artifact
                return artifact
        self._artifacts.append(artifact)
        return artifact

    @property
    def artifacts(self) -> Tuple[Artifact, ...]:
        return tuple(self._artifacts)

    def build(self) -> ArtifactManifest:
        return ArtifactManifest(
            target=self._target,
            source_sha=self._source_sha,
            version=self._version,
            adapter=self._adapter,
            artifacts=tuple(self._artifacts),
        )


# -- publisher results -------------------------------------------------------


@dataclass(frozen=True)
class PublisherResult:
    """What one destination did with one release.

    `skipped` is a first-class success. The commonest reason a publication
    happens is a duplicate event, and a duplicate event must produce a green
    result that says why — otherwise the only way to make a re-run quiet is to
    make it fail, and then nobody re-runs anything.

    `retryable` is the fail-closed switch. A failure that is retryable may be
    resumed; one that is not must stop the release, because resuming it would
    either duplicate an immutable identity or clobber bytes a consumer has
    already recorded the digest of.
    """

    outcome: str
    destination: str
    identity: str = ""
    external_id: str = ""
    external_version: str = ""
    retryable: bool = False
    reason: str = ""
    code: str = ""
    details: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in SUPPORTED_PUBLISH_OUTCOMES:
            raise ContractError(
                f"unknown publication outcome {self.outcome!r}; supported: "
                + ", ".join(SUPPORTED_PUBLISH_OUTCOMES)
            )
        if not self.destination:
            raise ContractError(
                "a publication result must name its destination; 'it went somewhere' "
                "is not a record"
            )
        if self.outcome == PUBLISHED and not self.identity:
            raise ContractError(
                f"publication to {self.destination!r} reported success without an "
                "immutable identity. Something that cannot be named cannot be "
                "checked for duplication, which is the property that makes a "
                "publication safe to retry."
            )
        if self.outcome in (SKIPPED, FAILED) and not self.reason:
            raise ContractError(
                f"a {self.outcome} result for {self.destination!r} must say why"
            )
        if self.outcome == FAILED and not self.code:
            raise ContractError(
                f"a failed result for {self.destination!r} must carry a stable code so "
                "a caller can branch on the kind of failure rather than on prose"
            )
        if self.outcome == PUBLISHED and self.retryable:
            raise ContractError(
                f"publication to {self.destination!r} succeeded, so there is nothing to "
                "retry"
            )

    @property
    def published(self) -> bool:
        return self.outcome == PUBLISHED

    @property
    def ok(self) -> bool:
        """Whether this destination left the release in an acceptable state.

        True for a publication and true for a skip, because "there was nothing
        to do and that was correct" is the outcome a duplicate event is supposed
        to have. Only a failure is not ok.
        """

        return self.outcome != FAILED

    @property
    def skipped(self) -> bool:
        return self.outcome == SKIPPED

    @property
    def failed(self) -> bool:
        return self.outcome == FAILED

    def detail(self, key: str, default: str = "") -> str:
        for name, value in self.details:
            if name == key:
                return value
        return default

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "outcome": self.outcome,
            "destination": self.destination,
            "retryable": self.retryable,
        }
        for key, value in (
            ("identity", self.identity),
            ("external_id", self.external_id),
            ("external_version", self.external_version),
            ("code", self.code),
            ("reason", self.reason),
        ):
            if value:
                payload[key] = value
        if self.details:
            payload["details"] = {name: value for name, value in self.details}
        return payload


def published(
    destination: str,
    identity: str,
    *,
    external_id: str = "",
    external_version: str = "",
    code: str = "",
    details: Optional[Mapping[str, str]] = None,
) -> PublisherResult:
    return PublisherResult(
        outcome=PUBLISHED,
        destination=destination,
        identity=identity,
        external_id=external_id,
        external_version=external_version,
        code=code,
        details=_detail_tuple(details),
    )


def skipped(
    destination: str,
    reason: str,
    *,
    identity: str = "",
    external_id: str = "",
    external_version: str = "",
    code: str = "",
    details: Optional[Mapping[str, str]] = None,
) -> PublisherResult:
    """A destination that did nothing, which is still an answer.

    `external_id` and `external_version` are accepted because a skip is often
    the *most* informative result there is: "v1.4.0 is already published" names
    the release that is already there, and a caller that has to go looking that
    up is a caller that will eventually guess.
    """

    return PublisherResult(
        outcome=SKIPPED,
        destination=destination,
        identity=identity,
        external_id=external_id,
        external_version=external_version,
        reason=reason,
        code=code,
        details=_detail_tuple(details),
    )


def failed(
    destination: str,
    reason: str,
    *,
    code: str,
    retryable: bool = False,
    identity: str = "",
    external_id: str = "",
    details: Optional[Mapping[str, str]] = None,
) -> PublisherResult:
    return PublisherResult(
        outcome=FAILED,
        destination=destination,
        identity=identity,
        external_id=external_id,
        retryable=retryable,
        reason=reason,
        code=code,
        details=_detail_tuple(details),
    )


def _detail_tuple(details: Optional[Mapping[str, str]]) -> Tuple[Tuple[str, str], ...]:
    if not details:
        return ()
    return tuple((str(key), str(value)) for key, value in sorted(details.items()))


# -- other normalized values -------------------------------------------------


@dataclass(frozen=True)
class ReleaseEvent:
    """The fact that started a release, and the only facts about it we trust.

    A delivery id is *not* an identity: GitHub re-dispatching, a re-run of a
    job, and a second workflow watching the same tag all produce events that are
    the same release. Deduplication is therefore keyed on what the event is
    *about* — the repository, the event kind, and the commit — not on who sent it.
    """

    repository: str
    name: str
    sha: str
    ref: str = ""
    delivery: str = ""
    default_branch: str = ""
    tag: str = ""
    attributes: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.repository or "/" not in self.repository:
            raise ContractError(
                f"event repository {self.repository!r} must be an owner/name pair"
            )
        if self.name not in SUPPORTED_EVENT_NAMES:
            raise ContractError(
                f"unsupported release event {self.name!r}; supported: "
                + ", ".join(SUPPORTED_EVENT_NAMES)
            )
        require_source_sha(self.sha, f"event {self.name!r}")
        if self.name == EVENT_TAG_PUSH and not self.tag:
            raise ContractError(
                "a tag-push event must carry the tag it pushed; a tag-driven release "
                "that does not know its tag has to invent one"
            )

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repository.split("/", 1)[1]

    def attribute(self, key: str, default: str = "") -> str:
        for name, value in self.attributes:
            if name == key:
                return value
        return default

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "repository": self.repository,
            "event": self.name,
            "sha": self.sha,
        }
        for key, value in (
            ("ref", self.ref),
            ("tag", self.tag),
            ("default_branch", self.default_branch),
            ("delivery", self.delivery),
        ):
            if value:
                payload[key] = value
        if self.attributes:
            payload["attributes"] = {name: value for name, value in self.attributes}
        return payload


@dataclass(frozen=True)
class Eligibility:
    """Whether this event is allowed to become a release at all.

    `blocking` separates "do not release, and that is fine" from "do not
    release, and someone has to look at it". A push that changed no version is
    the first kind and must stay green; a repository whose policy input cannot
    be read is the second and must fail.
    """

    eligible: bool
    reason: str = ""
    code: str = ""
    blocking: bool = False

    def __post_init__(self) -> None:
        if not self.eligible and not self.reason:
            raise ContractError("an ineligible event must say why")
        if not self.eligible and not self.code:
            raise ContractError(
                "an ineligible event must carry a stable code so a caller can branch "
                "on the kind of ineligibility rather than on prose"
            )
        if self.eligible and self.reason:
            raise ContractError(
                f"an eligible event cannot also carry a refusal reason ({self.reason!r})"
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "blocking": self.blocking,
            "code": self.code,
            "reason": self.reason,
        }


ELIGIBLE = Eligibility(eligible=True)


def ineligible(code: str, reason: str, *, blocking: bool = False) -> Eligibility:
    return Eligibility(eligible=False, reason=reason, code=code, blocking=blocking)


@dataclass(frozen=True)
class ReleaseNotes:
    """The body of a release, and where it came from.

    A non-empty body is required. The reference implementation this core
    generalizes once failed a release for cutting an empty changelog section,
    and that is the right failure: a published release with no notes is a support
    problem discovered by the person who needed the note.
    """

    body: str
    source: str = ""
    reference: str = ""

    def __post_init__(self) -> None:
        if not (self.body or "").strip():
            raise ContractError(
                "release notes must not be empty. A release with nothing to say is a "
                "release nobody can find later; if there is genuinely nothing, point "
                "at the changelog rather than publishing a blank body."
            )

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"body": self.body, "source": self.source}
        if self.reference:
            payload["reference"] = self.reference
        return payload


@dataclass(frozen=True)
class VerificationReport:
    """What checking an artifact came to, in the manifest's own vocabulary.

    A report that says `verified` while listing failures is a contract
    violation rather than a confusing value: it is the one combination that
    would let a release publish artifacts its own checker rejected.
    """

    verified: bool
    code: str = ""
    detail: str = ""
    failures: Tuple[str, ...] = ()
    verified_by: str = ""

    def __post_init__(self) -> None:
        if self.verified and self.failures:
            raise ContractError(
                f"verification report claims success and lists {len(self.failures)} "
                "failure(s): " + "; ".join(self.failures)
            )
        if not self.verified and not (self.code or self.failures):
            raise ContractError(
                "a failed verification must say what failed"
            )
        if self.verified and not self.verified_by:
            raise ContractError(
                "a verification report that claims success must say who verified it. "
                "'verified' with no subject is the one claim a consumer cannot check, "
                "and it is the one a release is built on."
            )

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "verified": self.verified,
            "code": self.code,
            "detail": self.detail,
            "failures": list(self.failures),
        }
        if self.verified_by:
            payload["verified_by"] = self.verified_by
        return payload


VERIFIED = VerificationReport(verified=True, code="verified", detail="verified", verified_by="continuum")


@dataclass(frozen=True)
class TargetSpec:
    """One release target as the core sees it: an id and an adapter.

    `options` is the adapter's configuration, carried through untouched. The
    core reads the id and the adapter name and nothing else, which is what keeps
    a keychain, a bundle identifier, or a Java module out of the state machine:
    there is no field here for the core to branch on, so there is no field for it
    to be tempted by.
    """

    id: str
    adapter: str
    options: Tuple[Tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not _TARGET_ID_RE.match(self.id or ""):
            raise ContractError(
                f"target id {self.id!r} must be a lowercase slug of letters, digits, "
                "'.', '_' or '-'"
            )
        if not self.adapter:
            raise ContractError(f"target {self.id!r} names no adapter")

    def option(self, name: str, default: Any = None) -> Any:
        for key, value in self.options:
            if key == name:
                return value
        return default

    def options_as_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in self.options}

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "adapter": self.adapter}


@dataclass(frozen=True)
class BuildRequest:
    """What a target adapter is asked to produce, and under which constraints.

    `key` is the transition's idempotency key. An adapter that has external
    effects of its own — a signing service, a build cache, a device farm —
    dedupes on it, exactly as the core dedupes on it.
    """

    target: TargetSpec
    version: str
    source_sha: str
    key: str
    workdir: str = ""
    dry_run: bool = False
    stage: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "target": self.target.id,
            "adapter": self.target.adapter,
            "version": self.version,
            "source_sha": self.source_sha,
            "key": self.key,
            "stage": self.stage,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class PublishRequest:
    """What a publisher is asked to do with a finished release.

    A release holds every target's artifacts, not one target's, so this carries
    all of the manifests. A publisher that publishes per target is a publisher
    that creates one release per target, and two releases for one version is the
    duplicate this whole module exists to prevent.
    """

    destination: str
    tag: str
    version: str
    source_sha: str
    manifests: Tuple[ArtifactManifest, ...]
    key: str
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not self.destination:
            raise ContractError("a publish request must name its destination")
        if not self.manifests:
            raise ContractError(
                f"publishing {self.tag} with no manifest would announce a release that "
                "holds nothing"
            )
        if not self.key:
            raise ContractError("a publish request must carry its idempotency key")
        for manifest in self.manifests:
            manifest.assert_release_source(self.source_sha)
            manifest.assert_release_version(self.version)
        self.assert_no_name_collisions()

    def assert_no_name_collisions(self) -> None:
        assert_distinct_artifact_names(self.artifacts)

    @property
    def targets(self) -> Tuple[str, ...]:
        return tuple(manifest.target for manifest in self.manifests)

    @property
    def artifacts(self) -> Tuple[Artifact, ...]:
        return tuple(artifact for manifest in self.manifests for artifact in manifest.artifacts)

    def describe(self) -> Dict[str, Any]:
        return {
            "destination": self.destination,
            "tag": self.tag,
            "version": self.version,
            "source_sha": self.source_sha,
            "targets": list(self.targets),
            "key": self.key,
            "dry_run": self.dry_run,
        }


def assert_distinct_artifact_names(artifacts: Sequence["Artifact"]) -> None:
    """Refuse two artifacts a release would have to name the same.

    A release's assets are addressed by name, not by target, so two targets that
    both produce `app.zip` are not two assets — they are one asset and a choice
    about which one the release holds. That choice is the adapter's to make by
    naming its artifacts distinctly, and it is refused here rather than resolved
    here, because resolving it by upload order would make the published release
    depend on the order the targets happened to be walked.

    A free function rather than a method on `PublishRequest` because the
    collision is a property of the release's asset set, not of any destination:
    a release with two colliding targets is ambiguous whether or not a
    destination is configured to receive it, and it should be refused at the
    build rather than at the first upload that happens to notice.
    """

    owners: Dict[str, str] = {}
    for artifact in artifacts:
        seen = owners.get(artifact.name)
        if seen is not None and seen != artifact.identity:
            raise ContractError(
                f"artifact {artifact.name!r} is claimed by {seen!r} and "
                f"{artifact.identity!r}. A destination addresses assets by name, "
                "so the two would collide; give each artifact a name that "
                "includes its target, platform, or classifier."
            )
        owners[artifact.name] = artifact.identity


@dataclass(frozen=True)
class NotesRequest:
    """What a notes builder is asked to describe."""

    event: ReleaseEvent
    version: str
    tag: str
    source_sha: str
    key: str
    release_pr: Optional["ReleasePrResult"] = None
    dry_run: bool = False

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "version": self.version,
            "tag": self.tag,
            "source_sha": self.source_sha,
            "key": self.key,
            "dry_run": self.dry_run,
        }
        if self.release_pr is not None:
            payload["release_pr"] = self.release_pr.describe()
        return payload


RELEASE_PR_ABSENT = "absent"
RELEASE_PR_OPEN = "open"
RELEASE_PR_MERGED = "merged"
SUPPORTED_RELEASE_PR_STATES: Tuple[str, ...] = (
    RELEASE_PR_ABSENT,
    RELEASE_PR_OPEN,
    RELEASE_PR_MERGED,
)


@dataclass(frozen=True)
class ReleasePrRequest:
    """What the release-pull-request port is asked to do.

    The port is separate from publication because it is a different job with a
    different credential: opening a pull request needs the pull-request
    automation's scope, and a release job that holds it can merge what it opens.
    """

    event: ReleaseEvent
    version: str
    tag: str
    source_sha: str
    key: str
    dry_run: bool = False

    def describe(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "tag": self.tag,
            "source_sha": self.source_sha,
            "key": self.key,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ReleasePrResult:
    """The state of the release pull request for one version.

    `absent` is a real answer and not a failure: a repository that cuts its
    releases from a tag has no release pull request, and a port that insisted on
    one would make that repository unable to release.
    """

    state: str
    number: int = 0
    version: str = ""
    url: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in SUPPORTED_RELEASE_PR_STATES:
            raise ContractError(
                f"unknown release pull request state {self.state!r}; supported: "
                + ", ".join(SUPPORTED_RELEASE_PR_STATES)
            )
        if self.state != RELEASE_PR_ABSENT and not self.number:
            raise ContractError(
                f"a release pull request that is {self.state} needs its number; "
                "'there is one, somewhere' is not a state"
            )
        if self.state == RELEASE_PR_ABSENT and not self.reason:
            raise ContractError(
                "a release pull request that does not exist must say why not, so a "
                "reader can tell a repository that does not use one from a driver "
                "that failed to find one"
            )

    @property
    def exists(self) -> bool:
        return self.state != RELEASE_PR_ABSENT

    @property
    def merged(self) -> bool:
        return self.state == RELEASE_PR_MERGED

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"state": self.state}
        for key, value in (
            ("number", self.number),
            ("version", self.version),
            ("url", self.url),
            ("reason", self.reason),
        ):
            if value:
                payload[key] = value
        return payload



# -- ports -------------------------------------------------------------------
#
# The core holds these interfaces and calls them; it never learns an
# implementation. Every port must be able to describe what it *would* do, which
# is what makes a dry-run plan possible for an adapter nobody has run yet. A
# port that cannot is not registered: a release whose dry run is a guess is not
# a plan.


class ReleasePort:
    """The marker every port declares, plus what the core needs from it.

    Written as an explicit attribute list rather than a `Protocol` so that
    `conform()` can check conformance at registration time on an arbitrary
    object, including one written against a different Continuum.
    """

    #: Every port can be asked what it would do without being run.
    SUPPORTS_DRY_RUN: bool = True

    #: The name this component is recorded under in a plan, a journal, or a
    #: publication result. It is a component name, not a platform.
    name: str = ""

    def intent(self) -> str:
        """One line describing what running this component would do."""

        raise NotImplementedError


PORT_SURFACES: Dict[str, Tuple[str, ...]] = {
    "eligibility": ("SUPPORTS_DRY_RUN", "name", "evaluate", "intent"),
    "version": ("SUPPORTS_DRY_RUN", "name", "propose", "intent"),
    "notes": ("SUPPORTS_DRY_RUN", "name", "build", "intent"),
    "target-adapter": (
        "SUPPORTS_DRY_RUN",
        "name",
        "build",
        "sign",
        "verify",
        "intent",
    ),
    "publisher": ("SUPPORTS_DRY_RUN", "name", "draft", "publish", "intent"),
    "release-pr": ("SUPPORTS_DRY_RUN", "name", "ensure", "intent"),
    "downstream-sync": ("SUPPORTS_DRY_RUN", "name", "publish", "intent"),
    "release-repository": ("find_by_tag", "create_draft", "assets", "upload", "publish"),
    "provenance": ("attest",),
}


#: The members of a port surface that are values rather than methods. A port has
#: to be identifiable — in a journal entry, a plan, a publication result, a
#: summary — so a missing or blank name disqualifies it exactly as a missing
#: method does, and is checked the same way rather than being assumed.
PORT_ATTRIBUTES: Tuple[str, ...] = ("name",)


def conform(component: Any, role: str) -> Tuple[str, ...]:
    """The members ``component`` is missing for ``role``, in declaration order.

    An empty tuple means conformance. This is the mechanical half of "a new
    platform adapter can be written against the contract": a new adapter is
    correct when the conformance test for its role passes, and the test data
    lives in one table rather than in prose that drifts.
    """

    surface = PORT_SURFACES.get(role)
    if surface is None:
        raise ContractError(
            f"unknown release port role {role!r}; known roles: "
            + ", ".join(sorted(PORT_SURFACES))
        )
    missing: List[str] = []
    for member in surface:
        if member == "SUPPORTS_DRY_RUN":
            if getattr(component, member, False) is not True:
                missing.append(member)
            continue
        if member in PORT_ATTRIBUTES:
            value = getattr(component, member, None)
            if not isinstance(value, str) or not value.strip():
                missing.append(member)
            continue
        if not callable(getattr(component, member, None)):
            missing.append(member)
    return tuple(missing)


def require_conform(component: Any, role: str) -> Any:
    missing = conform(component, role)
    if missing:
        raise ContractError(
            f"component {getattr(component, 'name', component)!r} cannot act as a "
            f"{role}: it is missing {', '.join(missing)}. Every release port must be "
            "able to describe what it would do, or a dry-run plan is a guess."
        )
    return component


def register(registry: Dict[str, Any], name: str, component: Any, role: str) -> Any:
    """Put a component in a registry, refusing one that breaks the contract."""

    if not name:
        raise ContractError(f"a {role} must be registered under a name")
    if name in registry:
        raise ContractError(
            f"{role} {name!r} is already registered; a duplicate registration is "
            "either a copy-paste or two components that would race for one name"
        )
    registry[name] = require_conform(component, role)
    return component


__all__ = [
    "CONTRACT_VERSION",
    "EVIDENCE_ARTIFACT_TYPES",
    "EVENT_DISPATCH",
    "EVENT_PUSH",
    "EVENT_RELEASE_PR_MERGED",
    "EVENT_SCHEDULED",
    "EVENT_TAG_PUSH",
    "FAILED",
    "MANIFEST_SCHEMA",
    "NotesRequest",
    "PORT_ATTRIBUTES",
    "PORT_SURFACES",
    "PUBLISHED",
    "PUBLISHER_RESULT_SCHEMA",
    "SKIPPED",
    "SUPPORTED_ARTIFACT_TYPES",
    "SUPPORTED_DIGEST_ALGORITHMS",
    "SUPPORTED_EVENT_NAMES",
    "SUPPORTED_PUBLISH_OUTCOMES",
    "SUPPORTED_PROVENANCE_STATES",
    "SUPPORTED_SIGNING_STATUSES",
    "SUPPORTED_VERIFICATION_STATUSES",
    "TYPE_ARCHIVE",
    "TYPE_BINARY",
    "TYPE_BUNDLE",
    "TYPE_CHECKSUMS",
    "TYPE_INSTALLER",
    "TYPE_PACKAGE",
    "TYPE_PROVENANCE",
    "TYPE_SIGNATURE",
    "PROVENANCE_ABSENT",
    "PROVENANCE_ATTESTED",
    "PROVENANCE_DECLARED",
    "SIGNING_DEGRADED",
    "SIGNING_NOT_APPLICABLE",
    "SIGNING_SIGNED",
    "SIGNING_UNSIGNED",
    "VERIFICATION_FAILED",
    "VERIFICATION_NOT_APPLICABLE",
    "VERIFICATION_UNVERIFIED",
    "VERIFICATION_VERIFIED",
    "Artifact",
    "ArtifactManifest",
    "BuildRequest",
    "ContractError",
    "Eligibility",
    "ManifestBuilder",
    "PublishRequest",
    "PublisherResult",
    "ReleaseEvent",
    "ReleaseNotes",
    "ReleasePrRequest",
    "ReleasePrResult",
    "ReleasePort",
    "TargetSpec",
    "VerificationReport",
    "CHECKSUMS_FILE",
    "conform",
    "checksums_document",
    "digest_file",
    "failed",
    "ineligible",
    "is_source_sha",
    "published",
    "register",
    "require_conform",
    "require_source_sha",
    "skipped",
]
