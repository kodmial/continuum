"""Provenance: a statement about who built a byte, and a way to check it.

A release that publishes an executable has published a claim that somebody
trusted ran a build. Without evidence that claim is the repository's word, and
the repository's word is exactly what a compromised workflow, a stolen token, or
a substituted dependency produces. Provenance is the artifact that turns the
claim into something a consumer can check offline, and this module exists to
make that artifact rather than to describe it.

Four properties are load-bearing, and each one is a way a provenance system has
been reduced to a rubber stamp.

**A statement is bound to bytes, not to a filename.** The subject is the
artifact's name *and its digest*, so a statement cannot be moved to a different
build of the same filename — the substitution every filename-only attestation
allows. `verify_statement()` re-derives the subject and refuses a statement
whose digest is not the digest of the artifact in hand.

**A statement is bound to a build, and the build is named.** The predicate
records the repository, the workflow file, the ref that workflow was read at,
the workflow's reusable identity, the commit, the run, and the event. A
statement that cannot say which workflow produced it is indistinguishable from
one anybody could have written, which is the property a consumer is actually
relying on when they read it.

**"Attested" means a statement exists.** A build whose provenance could not be
established reports `declared`, which the contract already refuses to treat as
evidence. A provenance port that cannot attest says so; it never invents an
identity to make a release look attested.

**Evidence does not attest itself.** A checksum file does not list itself, and a
statement bundle is not a subject of the statements it holds. Anything else makes
the digest of the evidence depend on the digest of the evidence, and no file
could ever satisfy it.

The shape is in-toto v1 with a SLSA v1 provenance predicate, which is what
GitHub's artifact attestations, `cosign attest`, and `slsa-verifier` all read.
This module depends on none of them: a statement is a value here, so it can be
built, checked, and diffed with nothing installed, and a consumer that does have
Sigstore tooling consumes the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import (
    PROVENANCE_ATTESTED,
    PROVENANCE_DECLARED,
    SIGNING_NOT_APPLICABLE,
    TYPE_PROVENANCE,
    VERIFICATION_VERIFIED,
    Artifact,
    ArtifactManifest,
    ContractError,
    ManifestBuilder,
    digest_file,
)

#: in-toto statement envelope. One of these is what a consumer verifies; the
#: predicate inside it is what the producer meant.
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"

#: The SLSA build provenance predicate. Chosen because it is the one GitHub's
#: attestation tooling, `slsa-verifier`, and most policy engines already read, so
#: a statement produced here is consumable rather than merely well-formed.
SLSA_PREDICATE = "https://slsa.dev/provenance/v1"

#: The predicate for an SBOM attachment. Deliberately distinct from the build
#: provenance: "this was built by this workflow" and "this contains these
#: components" are different claims about the same bytes, and merging them means a
#: consumer cannot check one without trusting the other.
SBOM_PREDICATE = "https://spdx.dev/Document"

#: The predicate types an SBOM may be attested under, keyed by the ecosystem that
#: produced it, so a consumer knows which tool to reach for.
SBOM_PREDICATE_BY_KIND: Dict[str, str] = {
    "spdx": SBOM_PREDICATE,
    "cyclonedx": "https://cyclonedx.org/bom",
    "syft": SBOM_PREDICATE,
}

#: The artifact type a statement bundle is recorded as, so a release that
#: carries one is still an evidence-only asset rather than something installable.
BUNDLE_TYPE = TYPE_PROVENANCE
BUNDLE_MEDIA_TYPE = "application/vnd.in-toto+jsonl"

#: The suffix of a statement bundle. A consumer looking for provenance on a
#: release should not have to be told what the file is called.
BUNDLE_SUFFIX = ".intoto.jsonl"

ATTESTOR_NAME = "continuum-provenance"

#: A `owner/name` repository. A statement names where the build happened, and
#: "somewhere" is not somewhere.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

#: A workflow identity as GitHub spells it for a reusable workflow:
#: `owner/name/.github/workflows/file.yml@refs/heads/main`.
_WORKFLOW_IDENTITY_RE = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml@.+$"
)

#: A refs path, or a raw commit for a run dispatched at a SHA.
_REF_RE = re.compile(r"^refs/[A-Za-z0-9._/-]+$|^[0-9a-f]{40}$|^[0-9a-f]{64}$")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ProvenanceError(ContractError):
    """Raised when provenance cannot be produced, or does not check out.

    A `ContractError` so the core classifies it as the stage's own failure with
    its own code, rather than as an unhandled crash in a component.
    """


def _require_text(value: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ProvenanceError(
            f"{what} is required: a statement that does not say {what} cannot be "
            "checked against anything"
        )
    return text


@dataclass(frozen=True)
class BuildIdentity:
    """Which build produced the bytes, in the terms a consumer can check.

    Every field is required. That is the argument of this module: an attestation
    with a hole in it is not a weaker attestation, it is a claim that cannot be
    checked at all, and a consumer who cannot check it is being asked to trust
    it.
    """

    repository: str
    workflow: str
    workflow_ref: str
    source_sha: str
    run_id: str = ""
    run_attempt: str = ""
    event: str = ""

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.match(self.repository or ""):
            raise ProvenanceError(
                f"build identity repository {self.repository!r} must be an owner/name pair; "
                "a statement has to name the repository whose workflow built the artifact"
            )
        if not _WORKFLOW_IDENTITY_RE.match(self.workflow or ""):
            raise ProvenanceError(
                f"build identity workflow {self.workflow!r} must be a reusable workflow "
                "identity such as owner/name/.github/workflows/release.yml@refs/heads/main. "
                "A statement that cannot name the workflow that ran is not evidence that "
                "the workflow ran"
            )
        if not _REF_RE.match(self.workflow_ref or ""):
            raise ProvenanceError(
                f"build identity workflow_ref {self.workflow_ref!r} must be a git ref "
                "(refs/heads/...) or a full commit SHA"
            )
        sha = (self.source_sha or "").strip().lower()
        if not re.match(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", sha):
            raise ProvenanceError(
                f"build identity source_sha {self.source_sha!r} must be a full commit SHA; "
                "an abbreviated one cannot be compared against the commit that was built"
            )
        object.__setattr__(self, "source_sha", sha)
        if self.run_id and not _RUN_ID_RE.match(self.run_id):
            raise ProvenanceError(
                f"build identity run_id {self.run_id!r} must be a run identifier or left empty"
            )
        if self.run_attempt and not str(self.run_attempt).isdigit():
            raise ProvenanceError(
                f"build identity run_attempt {self.run_attempt!r} must be a number or empty"
            )
        if self.workflow.split("@", 1)[-1] != self.workflow_ref:
            raise ProvenanceError(
                f"build identity workflow {self.workflow!r} and workflow_ref "
                f"{self.workflow_ref!r} disagree. A statement that names a workflow at a "
                "ref other than the one it was read at is claiming a build that did not "
                "happen here"
            )

    @property
    def builder_id(self) -> str:
        return self.workflow

    @property
    def workflow_path(self) -> str:
        return self.workflow.split("/", 2)[-1].split("@", 1)[0]

    @property
    def invocation_id(self) -> str:
        return f"{self.repository}/{self.workflow_path}@{self.workflow_ref}"

    def describe(self) -> Dict[str, str]:
        payload = {
            "repository": self.repository,
            "workflow": self.workflow,
            "workflow_ref": self.workflow_ref,
            "source_sha": self.source_sha,
        }
        for key, value in (
            ("run_id", self.run_id),
            ("run_attempt", self.run_attempt),
            ("event", self.event),
        ):
            if value:
                payload[key] = value
        return payload


def statement(
    *,
    identity: BuildIdentity,
    name: str,
    digest_algorithm: str,
    digest: str,
    version: str = "",
    target: str = "",
    predicate_type: str = SLSA_PREDICATE,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One in-toto statement about one artifact.

    Built by hand rather than through a builder, because the envelope is a fixed
    shape a verifier on the other side has to find fields in. `extra` is merged
    into the build's *external parameters* rather than replacing the predicate,
    so a caller can add detail (a version, a target, an SBOM's format) without
    being able to remove the binding.
    """

    subject_name = _require_text(name, "subject name")
    subject_digest = _require_text(digest, "subject digest")
    algorithm = _require_text(digest_algorithm, "subject digest algorithm")
    predicate_type = _require_text(predicate_type, "predicate type")
    if target and not _TARGET_RE.match(target):
        raise ProvenanceError(
            f"statement target {target!r} must be a lowercase slug; it is recorded in a "
            "document a consumer reads and compared with the manifest"
        )
    parameters: Dict[str, Any] = {
        "repository": identity.repository,
        "source": {
            "uri": identity.repository,
            "digest": {"gitCommit": identity.source_sha},
        },
        "workflow": {
            "id": identity.workflow,
            "ref": identity.workflow_ref,
            "path": identity.workflow_path,
            "repository": identity.repository,
        },
    }
    if version:
        parameters["version"] = version
    if target:
        parameters["target"] = target
    if extra:
        parameters.update({str(key): value for key, value in extra.items()})

    metadata: Dict[str, Any] = {"invocationId": identity.invocation_id}
    byproducts = [
        {"name": "github.repository", "value": identity.repository},
        {"name": "github.workflow_ref", "value": identity.workflow_ref},
    ]
    if identity.run_id:
        byproducts.append({"name": "github.run_id", "value": identity.run_id})
    if identity.run_attempt:
        byproducts.append({"name": "github.run_attempt", "value": str(identity.run_attempt)})
    if identity.event:
        byproducts.append({"name": "github.event_name", "value": identity.event})
    metadata["byproducts"] = byproducts

    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": subject_name, "digest": {algorithm: subject_digest}}],
        "predicateType": predicate_type,
        "predicate": {
            "buildDefinition": {
                "buildType": predicate_type,
                "externalParameters": parameters,
            },
            "runDetails": {"builder": {"id": identity.builder_id}, "metadata": metadata},
        },
    }


def statement_digest(document: Mapping[str, Any]) -> str:
    """The digest of a statement, over a canonical serialization.

    Canonical because the digest is the statement's identity: two runs that make
    the same claim in the same key order produce the same digest, and a statement
    re-serialized differently is not a different statement.
    """

    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def subject_of(document: Mapping[str, Any]) -> Tuple[str, str, str]:
    """The one subject of a statement, as ``(name, algorithm, digest)``.

    Exactly one, and anything else refused, because a verifier that has to
    search a list for the subject that matters is a verifier that will eventually
    accept the wrong one.
    """

    if document.get("_type") != STATEMENT_TYPE:
        raise ProvenanceError(
            f"statement _type {document.get('_type')!r} is not {STATEMENT_TYPE}; a verifier "
            "has to know the envelope shape before it can check anything"
        )
    subjects = document.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise ProvenanceError(
            "a statement must carry exactly one subject; one statement per artifact is "
            "what makes 'check this file' a mechanical operation"
        )
    subject = subjects[0]
    if not isinstance(subject, dict):
        raise ProvenanceError("a statement subject must be an object")
    digests = subject.get("digest")
    if not isinstance(digests, dict) or len(digests) != 1:
        raise ProvenanceError(
            f"statement subject {subject.get('name')!r} must carry exactly one digest "
            "algorithm; a subject with two is a subject nobody can check unambiguously"
        )
    algorithm, digest = next(iter(digests.items()))
    name = str(subject.get("name") or "")
    if not name or not digest:
        raise ProvenanceError(
            "a statement subject must carry a name and a digest; a subject missing "
            "either cannot identify bytes"
        )
    return name, str(algorithm), str(digest)


def verify_statement(
    document: Mapping[str, Any],
    *,
    identity: BuildIdentity,
    name: str,
    digest_algorithm: str,
    digest: str,
    predicate_type: str = SLSA_PREDICATE,
) -> str:
    """Check one statement against the bytes and the build it claims.

    Returns the statement's own digest, which is the reference a manifest
    records. Raises `ProvenanceError` naming the first thing that does not line
    up: a verifier that reports one generic failure gives a consumer no way to
    tell a substitution from a different builder's artifact.
    """

    subject_name, subject_algorithm, subject_digest = subject_of(document)
    if subject_name != name:
        raise ProvenanceError(
            f"statement is about {subject_name!r}, not {name!r}; a statement cannot be "
            "moved to another artifact, because its subject is the artifact"
        )
    if subject_algorithm != digest_algorithm or subject_digest != digest:
        raise ProvenanceError(
            f"statement for {name!r} covers {subject_algorithm}:{subject_digest}, not "
            f"{digest_algorithm}:{digest}. Those are different bytes; the statement does "
            "not describe what is in hand"
        )
    if document.get("predicateType") != predicate_type:
        raise ProvenanceError(
            f"statement for {name!r} has predicate {document.get('predicateType')!r}, not "
            f"{predicate_type!r}; a predicate a consumer cannot interpret is a claim it "
            "cannot check"
        )
    body = document.get("predicate")
    if not isinstance(body, Mapping):
        raise ProvenanceError(f"statement for {name!r} has no predicate")
    definition = body.get("buildDefinition")
    run = body.get("runDetails")
    if not isinstance(definition, Mapping) or not isinstance(run, Mapping):
        raise ProvenanceError(f"statement for {name!r} has no build definition or run details")
    parameters = definition.get("externalParameters", {})
    if not isinstance(parameters, Mapping):
        raise ProvenanceError(f"statement for {name!r} has no external parameters")
    if parameters.get("repository") != identity.repository:
        raise ProvenanceError(
            f"statement for {name!r} claims repository {parameters.get('repository')!r}, "
            f"not {identity.repository!r}"
        )
    workflow = parameters.get("workflow")
    if not isinstance(workflow, Mapping) or workflow.get("id") != identity.workflow:
        raise ProvenanceError(
            f"statement for {name!r} claims workflow {(workflow or {}).get('id')!r}, not "
            f"{identity.workflow!r}. Provenance that does not bind the workflow cannot "
            "tell a reviewed build from an unreviewed one"
        )
    if workflow.get("ref") != identity.workflow_ref:
        raise ProvenanceError(
            f"statement for {name!r} claims workflow ref {workflow.get('ref')!r}, not "
            f"{identity.workflow_ref!r}; the ref is what says which version of the "
            "workflow produced it"
        )
    commit = parameters.get("source", {}).get("digest", {}).get("gitCommit")
    if commit != identity.source_sha:
        raise ProvenanceError(
            f"statement for {name!r} claims commit {commit!r}, not {identity.source_sha!r}"
        )
    builder = run.get("builder", {}).get("id")
    if builder != identity.builder_id:
        raise ProvenanceError(
            f"statement for {name!r} names builder {builder!r}, not {identity.builder_id!r}"
        )
    return statement_digest(document)


@dataclass(frozen=True)
class StatementBundle:
    """A set of statements, and the reference each one is recorded under.

    Written once and uploaded once, so a release holds a single evidence asset
    whose contents cannot change without changing the digest a consumer verifies.
    """

    name: str
    statements: Tuple[Dict[str, Any], ...] = ()
    media_type: str = BUNDLE_MEDIA_TYPE

    @property
    def references(self) -> Tuple[Tuple[str, str], ...]:
        """The reference each statement is recorded under, by subject name.

        `sha256:` over the statement's own digest, which is what `attest()`
        returns, so a manifest and the bundle it points into cannot disagree
        about which statement backs an artifact.
        """

        pairs: List[Tuple[str, str]] = []
        for document in self.statements:
            name, _algorithm, _digest = subject_of(document)
            pairs.append((name, f"sha256:{statement_digest(document)}"))
        return tuple(pairs)

    def attestations(self) -> Dict[str, str]:
        return dict(self.references)

    def document(self) -> str:
        return "".join(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
            for document in self.statements
        )

    def digest(self) -> str:
        return hashlib.sha256(self.document().encode("utf-8")).hexdigest()

    def write(self, directory: str) -> str:
        parent = os.path.dirname(directory)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(directory, "w", encoding="utf-8") as handle:
            handle.write(self.document())
        return directory

    def record(self, builder: ManifestBuilder, path: str) -> Artifact:
        """Record the bundle as a verified evidence artifact.

        Verified by re-deriving the digest from the bytes just written, so a
        bundle that reached disk truncated or edited cannot be published as
        evidence.
        """

        _size, digest = digest_file(path)
        if digest != self.digest():
            raise ProvenanceError(
                f"the statement bundle at {path!r} does not match the statements it was "
                "built from; evidence that changed on the way to disk is not evidence"
            )
        return builder.record(
            self.name,
            path,
            BUNDLE_TYPE,
            media_type=self.media_type,
            signing=SIGNING_NOT_APPLICABLE,
            verification=VERIFICATION_VERIFIED,
            verified_by=ATTESTOR_NAME,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "statements": len(self.statements),
            "digest": f"sha256:{self.digest()}",
            "media_type": self.media_type,
            "attestations": {name: reference for name, reference in self.references},
        }


def read_bundle(path: str) -> StatementBundle:
    """Read a bundle back, checking every statement as it is read.

    A bundle is evidence, and evidence that is only checked when a consumer
    decides to check it is evidence that will not be. So the read path validates
    each statement's envelope and re-derives its digest.
    """

    statements: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            document = json.loads(line)
            subject_of(document)
            statements.append(document)
    return StatementBundle(name=os.path.basename(path), statements=tuple(statements))


def _attested(artifact: Artifact, attestation: str) -> Artifact:
    """The same artifact, with provenance recorded as attested."""

    return Artifact(
        target=artifact.target,
        source_sha=artifact.source_sha,
        version=artifact.version,
        name=artifact.name,
        path=artifact.path,
        type=artifact.type,
        size=artifact.size,
        digest=artifact.digest,
        digest_algorithm=artifact.digest_algorithm,
        platform=artifact.platform,
        arch=artifact.arch,
        classifier=artifact.classifier,
        media_type=artifact.media_type,
        signing=artifact.signing,
        signing_identity=artifact.signing_identity,
        verification=artifact.verification,
        verified_by=artifact.verified_by,
        provenance=PROVENANCE_ATTESTED,
        attestation=attestation,
        declared=artifact.declared,
    )


@dataclass(frozen=True)
class _Attested:
    """One statement, kept exactly as it was made and verified.

    Private because it is bookkeeping rather than evidence: a consumer reads the
    statement, not this.
    """

    name: str
    digest_algorithm: str
    digest: str
    document: Dict[str, Any]


class ProvenanceAttestor:
    """Attests artifacts by writing statements a consumer can check.

    The port is narrow on purpose: the destination asks for an attestation for a
    name and a digest, and the attestor returns a reference it can record.
    Recording a statement in a platform's transparency log is an *additional*
    step with its own failure semantics, never a condition of producing the
    statement — a release whose provenance cannot be registered must be able to
    say so rather than be blocked by a service outage.
    """

    SUPPORTS_DRY_RUN = True

    def __init__(
        self,
        identity: BuildIdentity,
        *,
        directory: str,
        name: str = ATTESTOR_NAME,
        submitter: Optional[Any] = None,
        sbom_path: str = "",
        sbom_kind: str = "spdx",
        sbom_name: str = "",
    ) -> None:
        self.identity = identity
        self.name = name
        self._directory = directory
        self._submitter = submitter
        self._sbom_path = sbom_path
        self._sbom_kind = (sbom_kind or "spdx").strip().lower()
        self._sbom_name = sbom_name
        self.attested: List[Tuple[str, str, str]] = []
        self._statements: List[_Attested] = []
        if submitter is not None and not callable(submitter):
            raise ProvenanceError("a provenance submitter must be callable")
        if not directory:
            raise ProvenanceError("a provenance attestor needs a directory to write its bundle into")
        if sbom_path and self._sbom_kind not in SBOM_PREDICATE_BY_KIND:
            raise ProvenanceError(
                f"unknown SBOM kind {sbom_kind!r}; supported: "
                + ", ".join(sorted(SBOM_PREDICATE_BY_KIND))
            )
        if sbom_path and not os.path.isfile(sbom_path):
            raise ProvenanceError(
                f"the SBOM at {sbom_path!r} does not exist. Attaching a software bill of "
                "materials that was never generated would publish a claim about "
                "dependencies nobody can check"
            )

    def intent(self) -> str:
        parts = [
            "write an in-toto statement for every published artifact, bound to its digest, "
            "this workflow identity, and this commit"
        ]
        if self._sbom_path:
            parts.append(f"attest the {self._sbom_kind} software bill of materials")
        if self._submitter is not None:
            parts.append("register each statement in the platform's transparency log")
        return ", and ".join(parts)

    # -- the provenance port ------------------------------------------------
    def attest(
        self,
        *,
        tag: str,
        name: str,
        digest_algorithm: str,
        digest: str,
        source_sha: str,
    ) -> str:
        """Attest one artifact and return the reference to record for it.

        `source_sha` is checked against the build identity rather than trusted: a
        destination handing over a digest for an artifact from another commit is
        exactly the case a statement exists to catch, and the cheapest place to
        catch it is here.

        The statement that is verified here is the one that is kept, and the
        reference returned is the digest of that document. A reference to a
        statement this attestor does not publish is worse than no reference: a
        consumer who looks it up in the bundle finds nothing and has to decide
        whether to trust the reference or the bundle.
        """

        if (source_sha or "").strip().lower() != self.identity.source_sha:
            raise ProvenanceError(
                f"{name!r} was built from {source_sha} but this build's identity is "
                f"{self.identity.source_sha}. Refusing to bind one build's identity to "
                "another build's bytes"
            )
        subject_name = _require_text(name, "attested artifact name")
        subject_digest = _require_text(digest, "attested artifact digest")
        algorithm = _require_text(digest_algorithm, "attested digest algorithm")
        document = statement(
            identity=self.identity,
            name=subject_name,
            digest_algorithm=algorithm,
            digest=subject_digest,
            extra={"release_tag": _require_text(tag, "release tag")},
        )
        # Verified before it is returned, not after: a statement this module
        # cannot check against its own inputs is not evidence.
        reference = verify_statement(
            document,
            identity=self.identity,
            name=subject_name,
            digest_algorithm=algorithm,
            digest=subject_digest,
        )
        self.attested.append((tag, subject_name, subject_digest))
        self._statements.append(
            _Attested(
                name=subject_name,
                digest_algorithm=algorithm,
                digest=subject_digest,
                document=document,
            )
        )
        if self._submitter is not None:
            self._submitter(
                tag=tag,
                name=subject_name,
                digest_algorithm=algorithm,
                digest=subject_digest,
                document=document,
            )
        return f"sha256:{reference}"

    # -- the bundle ---------------------------------------------------------
    @property
    def bundle_name(self) -> str:
        return f"{self.identity.source_sha[:12]}{BUNDLE_SUFFIX}"

    def sbom_asset_name(self, version: str) -> str:
        return self._sbom_name or f"{self.identity.source_sha[:12]}-{version}-sbom.json"

    def bundle(self, version: str) -> StatementBundle:
        """Every statement written for one release, as one uploadable asset.

        The statements that were verified when they were made, byte for byte.
        Rebuilding them here — with a version added, or with an algorithm
        defaulted — would give every reference already recorded in a manifest an
        entry it cannot be found under.

        A name may be attested once per release at one digest. Two statements for
        one name is a question a consumer has to answer, and a release must not
        manufacture a question.
        """

        by_name: Dict[str, _Attested] = {}
        for attested in self._statements:
            previous = by_name.get(attested.name)
            if previous is not None and previous.digest != attested.digest:
                raise ProvenanceError(
                    f"{attested.name!r} was attested at {previous.digest} and at "
                    f"{attested.digest} in one release. A release holds one statement per "
                    "artifact; two means one of them is about bytes that are not published"
                )
            by_name.setdefault(attested.name, attested)
        if not _version_tag_agrees(version, self.attested):
            raise ProvenanceError(
                f"statements were written for tags that do not all carry version "
                f"{version!r}. One release holds one version, and a bundle mixing two is a "
                "bundle whose contents cannot be checked against the release it is attached to"
            )
        documents: List[Dict[str, Any]] = []
        for name in sorted(by_name):
            attested = by_name[name]
            verify_statement(
                attested.document,
                identity=self.identity,
                name=name,
                digest_algorithm=attested.digest_algorithm,
                digest=attested.digest,
            )
            documents.append(attested.document)
        return StatementBundle(name=self.bundle_name, statements=tuple(documents))

    def sbom_bundle(self, version: str) -> Optional[StatementBundle]:
        """The SBOM's own statement, or None when no SBOM was configured."""

        if not self._sbom_path:
            return None
        _size, digest = digest_file(self._sbom_path)
        name = self.sbom_asset_name(version)
        return StatementBundle(
            name=f"{os.path.splitext(name)[0]}{BUNDLE_SUFFIX}",
            statements=(
                statement(
                    identity=self.identity,
                    name=name,
                    digest_algorithm="sha256",
                    digest=digest,
                    extra={
                        "version": version,
                        # The name and the kind, not the path: this document is
                        # published, and a runner's filesystem layout is
                        # evidence about nothing.
                        "sbom": {"kind": self._sbom_kind, "name": name},
                    },
                    predicate_type=SBOM_PREDICATE_BY_KIND[self._sbom_kind],
                ),
            ),
        )

    def write(self, version: str) -> Tuple[StatementBundle, ...]:
        """Write the statement bundle, and the SBOM's bundle when configured.

        A bundle with no statements is not written: an empty evidence asset is
        published weight that a consumer has to download and interpret, and it
        attests to nothing.
        """

        os.makedirs(self._directory, exist_ok=True)
        written: List[StatementBundle] = []
        for bundle in (self.bundle(version), self.sbom_bundle(version)):
            if bundle is None or not bundle.statements:
                continue
            bundle.write(self.path_for(bundle.name))
            written.append(bundle)
        return tuple(written)

    def path_for(self, name: str) -> str:
        """Where one of this attestor's bundles lives, from its name alone.

        One place, so a name recorded in a manifest always points at the file
        the release will upload rather than at a path assembled twice and hoped
        to match.
        """

        return os.path.join(self._directory, name)

    def attach_to(self, manifest: ArtifactManifest, version: str) -> ArtifactManifest:
        """Return ``manifest`` with this release's evidence attached.

        A copy rather than an edit of the build's own manifest, because the
        statement bundle describes the published asset set — including the
        checksums that describe the other assets — and rewriting the record of
        what was built would make it disagree with what was shipped.
        """

        bundles = self.write(version)
        attestations = self.bundle(version).attestations()
        sbom = self.sbom_bundle(version)
        builder = ManifestBuilder(
            target=manifest.target,
            adapter=manifest.adapter,
            source_sha=manifest.source_sha,
            version=manifest.version,
        )
        for artifact in manifest.artifacts:
            if artifact.provenance == PROVENANCE_DECLARED:
                reference = attestations.get(artifact.name)
                if reference is None:
                    raise ProvenanceError(
                        f"{artifact.name!r} declared that it wanted provenance and no "
                        "statement was written for it. Publishing it would attach a claim "
                        "nothing backs"
                    )
                builder.replace(_attested(artifact, reference))
                continue
            builder.replace(artifact)
        for bundle in bundles:
            path = self.path_for(bundle.name)
            recorded = {item.name: item for item in manifest.artifacts}
            existing = recorded.get(bundle.name)
            if existing is not None and existing.digest == _file_digest(path):
                # A resumed run attaches the same evidence again. Recording it
                # twice would publish two assets under one name, which the
                # destination cannot accept; the bytes are already described.
                builder.replace(existing)
                continue
            if existing is not None:
                raise ProvenanceError(
                    f"{bundle.name} is already in this manifest with digest "
                    f"{existing.digest}, and the evidence written for this release has "
                    f"{_file_digest(path)}. Two bundles under one name cannot both be "
                    "uploaded, and only one of them describes this build"
                )
            bundle.record(builder, path)
        if self._sbom_path:
            # The SBOM is attested by the same rules as any other artifact: the
            # reference recorded here is the reference in the bundle that is
            # about to be uploaded beside it.
            name = self.sbom_asset_name(version)
            reference = (sbom.attestations() if sbom is not None else {}).get(name)
            if reference is None:
                raise ProvenanceError(
                    f"the software bill of materials {name!r} has no statement to record"
                )
            builder.record(
                name,
                self._sbom_path,
                BUNDLE_TYPE,
                media_type=_sbom_media_type(self._sbom_kind),
                signing=SIGNING_NOT_APPLICABLE,
                verification=VERIFICATION_VERIFIED,
                verified_by=ATTESTOR_NAME,
                provenance=PROVENANCE_ATTESTED,
                attestation=reference,
            )
        return builder.build()


def _version_tag_agrees(version: str, attested: Sequence[Tuple[str, str, str]]) -> bool:
    """Whether every tag attested in this run names the same version.

    Checked rather than assumed because the attestor outlives a single call: a
    resumed run re-attests what the interrupted one did, and a bundle that mixed
    two versions' statements would look like one release's evidence while
    describing another.
    """

    if not version:
        return True
    seen = {tag for tag, _name, _digest in attested}
    for tag in seen:
        stripped = tag[1:] if tag.startswith("v") else tag
        if stripped != version and not stripped.startswith(f"{version}-"):
            return False
    return True


def _file_digest(path: str) -> str:
    return digest_file(path)[1]


def _sbom_media_type(kind: str) -> str:
    if kind == "cyclonedx":
        return "application/vnd.cyclonedx+json"
    if kind == "spdx":
        return "application/spdx+json"
    return "application/json"


def attestable_artifacts(manifests: Sequence[ArtifactManifest]) -> Tuple[Artifact, ...]:
    """The artifacts a public release attests.

    Installable artifacts only. Evidence is excluded for two reasons: a checksum
    file and a statement bundle describe other artifacts, and attesting a
    description adds something to check without adding a claim worth making. A
    declared artifact is excluded because there are no bytes to attest, which is
    exactly what `declared` means.
    """

    return tuple(
        artifact
        for manifest in manifests
        for artifact in manifest.installable
        if not artifact.declared
    )


def assert_manifest_provenance(
    manifests: Sequence[ArtifactManifest], *, require_attested: bool
) -> None:
    """Refuse a published asset set whose provenance is weaker than policy.

    `require_attested` is a policy input rather than a default, because the
    honest answer differs by release: a public executable wants statements, and a
    repository publishing a checksum for its own use may not have a build
    identity at all. What is never allowed is an artifact claiming `attested`
    without naming a statement — a combination the contract already refuses at
    construction.
    """

    if not require_attested:
        return
    unattested = sorted(
        artifact.name
        for manifest in manifests
        for artifact in manifest.installable
        if artifact.provenance != PROVENANCE_ATTESTED
    )
    if unattested:
        raise ProvenanceError(
            "this release is configured to publish attested artifacts, and "
            + ", ".join(unattested)
            + " are not attested. Publishing them would ship executables whose "
            "provenance nobody can check",
            code="provenance-required",
        )


def attestor_from_environment(
    environment: Mapping[str, str], *, directory: str
) -> Optional[ProvenanceAttestor]:
    """Build an attestor from the runner environment, or None if unconfigured.

    Read from the environment rather than from configuration because every field
    is a fact about *this run* — which workflow file ran, at which ref, on which
    event — and configuration cannot know any of them. A partial identity is a
    refusal rather than a weaker statement: a statement with a guessed commit is
    worse than no statement.
    """

    repository = (environment.get("GITHUB_REPOSITORY") or "").strip()
    workflow = (environment.get("GITHUB_WORKFLOW_REF") or "").strip()
    source_sha = (environment.get("CONTINUUM_RELEASE_SOURCE_SHA") or "").strip()
    if not (repository and workflow and source_sha):
        return None
    try:
        identity = BuildIdentity(
            repository=repository,
            workflow=workflow,
            workflow_ref=workflow.split("@", 1)[-1],
            source_sha=source_sha,
            run_id=(environment.get("GITHUB_RUN_ID") or "").strip(),
            run_attempt=(environment.get("GITHUB_RUN_ATTEMPT") or "").strip(),
            event=(environment.get("GITHUB_EVENT_NAME") or "").strip(),
        )
    except ProvenanceError:
        return None
    return ProvenanceAttestor(identity, directory=directory)


__all__ = [
    "ATTESTOR_NAME",
    "BUNDLE_MEDIA_TYPE",
    "BUNDLE_SUFFIX",
    "BUNDLE_TYPE",
    "BuildIdentity",
    "SBOM_PREDICATE",
    "SBOM_PREDICATE_BY_KIND",
    "SLSA_PREDICATE",
    "STATEMENT_TYPE",
    "ProvenanceAttestor",
    "ProvenanceError",
    "StatementBundle",
    "assert_manifest_provenance",
    "attestable_artifacts",
    "attestor_from_environment",
    "read_bundle",
    "statement",
    "statement_digest",
    "subject_of",
    "verify_statement",
]
