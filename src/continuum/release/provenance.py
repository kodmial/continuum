"""Evidence that the bytes about to be published are the bytes a gate validated.

A release publishes bytes. Something has to have exercised *those bytes* before
they are made public, or the gate ran about a build that was then replaced. Three
failures are possible, and they are distinct because they are fixed in different
places:

* **A gate that never ran for this head.** The candidate exists and is green for
  some other commit. A head is an immutable identifier, so a green run for any
  other head says nothing about this one.
* **A gate that ran for this head but is not green.** The validation happened and
  failed, or was cancelled. A cancelled run is not a passing run: it says the gate
  did not finish, which is different from saying the artifact is good.
* **A gate that was green for this head, on different bytes.** This is the one
  nothing downstream can catch. The artifact names a digest, the run names a
  digest, and they differ, so the tested artifact and the published artifact are
  two files that happen to share a name. Downloading "the artifact the gate
  produced" is the only thing that makes them the same file, and that is why the
  provenance record carries a *source* for the bytes rather than only a digest.

So a :class:`TestedCandidate` is a claim with three parts -- the immutable source
head, the gate that ran, and the digest of each artifact the gate exercised -- and
:func:`verify_candidate` either proves the claim against the artifacts on disk or
refuses it. Every failure is a refusal with a named reason rather than a warning,
because a release is the one operation where "probably the same bytes" is worth
nothing.

Nothing here names a product, a language, a package manager, or a CI vendor. The
gate is identified by an opaque string, and a consumer with no such gate simply
does not pass one -- which is the difference between "no evidence" and "evidence of
nothing".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROVENANCE_SCHEMA = "continuum.release-provenance/v1"

#: Terminal states of a gate run. Deliberately four, not two: a run that was
#: cancelled or skipped did not decide, and collapsing that into "not green" would
#: lose the one fact that tells a reader whether to re-run the gate or fix the
#: artifact.
GATE_SUCCESS = "success"
GATE_FAILURE = "failure"
GATE_CANCELLED = "cancelled"
GATE_MISSING = "missing"

#: States that mean the artifact was exercised. Only one of them does.
PASSING_GATE_STATES: Tuple[str, ...] = (GATE_SUCCESS,)

_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
#: A commit is named by an object ID. Both widths are accepted because the width is
#: a property of the repository's hash function, not of the claim -- refusing a
#: 64-character ID would not be caution, it would be a bug in a repository that
#: happens to use a different hash.
_OBJECT_ID_RE = re.compile(r"\b(?:[0-9a-f]{40}|[0-9a-f]{64})\b")
_DEFAULT_CHUNK = 1 << 20


class ProvenanceError(ValueError):
    """The claim cannot be evaluated from what it was given."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def file_digest(path: str, algorithm: str = "sha256") -> str:
    """The digest of a file's bytes, read in chunks.

    Read from disk rather than from a size the caller remembered: the whole claim
    is about the bytes, and a digest computed over anything other than the file
    being published would be a claim about a different file.
    """

    try:
        digest = hashlib.new(algorithm)
    except ValueError:
        raise ProvenanceError("unknown_algorithm", f"unsupported digest {algorithm!r}") from None
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_DEFAULT_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except FileNotFoundError:
        raise ProvenanceError("artifact-missing", f"{path} is not a readable file") from None
    except OSError as error:
        raise ProvenanceError("artifact-unreadable", f"{path} could not be read: {error}") from None
    return digest.hexdigest()


def canonical_digest(value: str) -> str:
    """Normalise a digest for comparison, without inventing one.

    A record that names no digest is an absence, not a digest of the empty string,
    so an absent value stays absent and fails the comparison it would otherwise
    satisfy. The check is a full match rather than a prefix match: a 64-character
    run inside a longer string is not a digest, and accepting one would compare
    two strings and call them equal when they are not.
    """

    text = (value or "").strip().lower()
    if not text:
        return ""
    if not _SHA256_RE.fullmatch(text):
        raise ProvenanceError(
            "malformed_digest",
            f"{value!r} is not a 64-character hex digest; comparing it would be "
            "comparing two strings",
        )
    return text


def canonical_source_sha(value: str) -> str:
    """Normalise a commit object ID, or refuse it.

    A branch name is deliberately not accepted. `main` is a moving identifier, so a
    record naming it could be replayed against a tree its gate never ran on, which
    is the exact failure this module exists to make impossible.
    """

    text = (value or "").strip().lower()
    if not _OBJECT_ID_RE.fullmatch(text):
        raise ProvenanceError(
            "malformed_source_sha",
            f"{value!r} is not a commit object ID (40 or 64 hexadecimal characters). "
            "A branch name is not one: it moves, so a record naming it could be "
            "replayed against a tree the gate never ran on",
        )
    return text


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TestedCandidate:
    """What a validation gate exercised, for one immutable source head."""

    #: The commit the gate ran against. Required, and never a branch name: a
    #: branch moves, and the claim is about one tree.
    source_sha: str
    #: An opaque identifier for the gate -- the workflow file it ran in, the job
    #: that produced the artifacts. Not a product name.
    gate: str = ""
    gate_run_id: str = ""
    gate_conclusion: str = GATE_SUCCESS
    #: `artifact name -> digest`, for every artifact the gate exercised.
    artifacts: Mapping[str, str] = field(default_factory=dict)
    #: Where the validated bytes can be obtained. A path, or any reference a
    #: caller knows how to resolve; the point is that it is a *source*, so the
    #: published bytes are the tested bytes rather than a re-build of them.
    artifact_source: str = ""
    completed_at: str = ""

    def __post_init__(self) -> None:
        if not (self.source_sha or "").strip():
            raise ProvenanceError(
                "source_sha_missing",
                "a tested candidate must name the immutable source head its gate ran "
                "against; a branch name or nothing would make the claim repeatable "
                "against a tree it was never run on",
            )
        canonical_source_sha(self.source_sha)
        for name, digest in (self.artifacts or {}).items():
            if not str(name).strip():
                raise ProvenanceError("unnamed_artifact", "an artifact with no name cannot be reused")
            canonical_digest(str(digest))

    @property
    def conclusion(self) -> str:
        return str(self.gate_conclusion or "").strip().lower()

    @property
    def passed(self) -> bool:
        return self.conclusion in PASSING_GATE_STATES

    def digests(self) -> Dict[str, str]:
        return {
            str(name): canonical_digest(str(digest))
            for name, digest in (self.artifacts or {}).items()
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "source_sha": self.source_sha,
            "gate": self.gate,
            "gate_run_id": self.gate_run_id,
            "gate_conclusion": self.conclusion,
            "artifact_source": self.artifact_source,
            "completed_at": self.completed_at,
            "artifacts": self.digests(),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"

    @property
    def digest(self) -> str:
        """A digest over the claim, so a decision can be bound to it."""

        payload = {
            "source_sha": self.source_sha,
            "gate": self.gate,
            "gate_run_id": self.gate_run_id,
            "gate_conclusion": self.conclusion,
            "artifacts": sorted(self.digests().items()),
        }
        return "pc-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()[:16]


def read_candidate(document: Mapping[str, Any]) -> TestedCandidate:
    """Read a record written by another process, refusing what it cannot vouch for."""

    if not isinstance(document, Mapping):
        raise ProvenanceError("candidate_not_a_mapping", "a tested candidate must be an object")
    schema = document.get("schema")
    if schema not in (PROVENANCE_SCHEMA, None):
        raise ProvenanceError(
            "unknown_provenance_schema",
            f"expected {PROVENANCE_SCHEMA!r}, found {schema!r}",
        )
    return TestedCandidate(
        source_sha=str(document.get("source_sha", "")),
        gate=str(document.get("gate", "")),
        gate_run_id=str(document.get("gate_run_id", "")),
        gate_conclusion=str(document.get("gate_conclusion", GATE_SUCCESS)),
        artifacts={
            str(name): str(value)
            for name, value in (document.get("artifacts", {}) or {}).items()
        },
        artifact_source=str(document.get("artifact_source", "")),
        completed_at=str(document.get("completed_at", "")),
    )


def load_candidate(path: str) -> TestedCandidate:
    with open(path, "r", encoding="utf-8") as handle:
        return read_candidate(json.load(handle))


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verification:
    """Whether the bytes about to be published are the bytes that were tested."""

    ok: bool
    #: A refusal code, or "" when the claim holds. Named rather than free text so a
    #: workflow can branch on it.
    code: str = ""
    message: str = ""
    source_sha: str = ""
    gate: str = ""
    gate_run_id: str = ""
    candidate_digest: str = ""
    verified: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    mismatched: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "kind": "verification",
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "source_sha": self.source_sha,
            "gate": self.gate,
            "gate_run_id": self.gate_run_id,
            "candidate_digest": self.candidate_digest,
            "verified": list(self.verified),
            "missing": list(self.missing),
            "mismatched": list(self.mismatched),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.describe(), sort_keys=True, indent=indent) + "\n"


#: Refusal codes, so a caller can distinguish a gate that never ran from a gate
#: that failed from bytes that are simply not the ones that were tested.
NO_CANDIDATE = "no-tested-candidate"
GATE_NOT_GREEN = "gate-not-green"
PUBLISH_HEAD_UNKNOWN = "publish-head-unknown"
HEAD_MISMATCH = "source-head-mismatch"
NO_ARTIFACT_SOURCE = "no-artifact-source"
ARTIFACT_MISSING = "tested-artifact-missing"
ARTIFACT_MISMATCH = "artifact-digest-mismatch"


def verify_candidate(
    candidate: Optional[TestedCandidate],
    *,
    source_sha: str = "",
    artifacts: Optional[Mapping[str, str]] = None,
    require_source: bool = True,
) -> Verification:
    """Prove that the artifacts on disk are the ones the gate exercised.

    ``artifacts`` maps each publishable artifact's name to the path this run will
    upload from. The order of the checks is the order of what can be wrong: a gate
    that did not run, a gate that ran and did not pass, a publish that does not say
    which tree it is for, a gate that ran for a different tree, and only then the
    bytes themselves. Each of the first four makes the fifth meaningless, so
    reporting them in the other order would bury the actual cause behind a list of
    digests.

    ``require_source=False`` is for the caller that publishes from something with no
    commit at all -- a vendored tree, a generated archive. It drops both the head
    comparison and the artifact-source requirement together, because a consumer with
    no immutable head has neither to check.
    """

    if candidate is None:
        return Verification(
            ok=False,
            code=NO_CANDIDATE,
            message=(
                "no tested candidate was supplied, so nothing has established that "
                "the bytes about to be published are the bytes a validation gate "
                "exercised"
            ),
        )

    common = {
        "source_sha": candidate.source_sha,
        "gate": candidate.gate,
        "gate_run_id": candidate.gate_run_id,
        "candidate_digest": candidate.digest,
    }

    if not candidate.passed:
        return Verification(
            ok=False,
            code=GATE_NOT_GREEN,
            message=(
                f"the gate {candidate.gate or 'run'} concluded "
                f"{candidate.conclusion or 'unknown'!r} for {candidate.source_sha}; a "
                "cancelled or failed run is not a passing run, so nothing was "
                "exercised by it"
            ),
            **common,
        )

    if require_source and not source_sha.strip():
        # Skipping the comparison because the publish did not say what it is for is
        # the same as not doing it: the candidate is green for *a* head, and every
        # caller that reaches this point wants to know whether it is green for *this*
        # one. Without the answer there is nothing to compare, and an absent
        # comparison must not read as a matching one.
        return Verification(
            ok=False,
            code=PUBLISH_HEAD_UNKNOWN,
            message=(
                "this publish does not name the source head it is for, so there is "
                "nothing to compare the tested candidate against. Pass the commit "
                "being published, or pass require_source=False for a publish that has "
                "no commit at all."
            ),
            **common,
        )

    if require_source and canonical_source_sha(source_sha) != canonical_source_sha(
        candidate.source_sha
    ):
        return Verification(
            ok=False,
            code=HEAD_MISMATCH,
            message=(
                f"the gate exercised {candidate.source_sha} but this publish is for "
                f"{source_sha.strip().lower()}; a green run for any other commit says "
                "nothing about this tree"
            ),
            **common,
        )

    if require_source and not candidate.artifact_source.strip():
        return Verification(
            ok=False,
            code=NO_ARTIFACT_SOURCE,
            message=(
                "the candidate names no source for the validated bytes, so the "
                "artifacts on disk are a re-build whose equality with the tested "
                "ones is assumed rather than shown"
            ),
            **common,
        )

    expected = candidate.digests()
    if not artifacts:
        # No artifacts to check is not a pass. It means the caller asked a question
        # with no subject, and reporting `ok` would let a publish proceed on a claim
        # nothing was compared against.
        return Verification(
            ok=False,
            code=NO_CANDIDATE,
            message="no artifacts were supplied to compare against the tested candidate",
            **common,
        )

    verified: List[str] = []
    missing: List[str] = []
    mismatched: List[Tuple[str, str]] = []
    for name in sorted(artifacts):
        path = str(artifacts[name])
        claim = expected.get(name)
        if claim is None:
            # An artifact the gate never saw. Publishing it would put an
            # unexercised file into a release that is described as validated.
            missing.append(name)
            continue
        if not os.path.isfile(path):
            missing.append(name)
            continue
        observed = canonical_digest(file_digest(path))
        if observed == claim:
            verified.append(name)
        else:
            mismatched.append((name, path))

    if missing:
        return Verification(
            ok=False,
            code=ARTIFACT_MISSING,
            message=(
                "{} artifact(s) the gate did not exercise, or did not leave on disk: "
                "{}. Publishing them would put bytes into a release described as "
                "validated that no gate ever validated.".format(
                    len(missing), ", ".join(missing)
                )
            ),
            verified=tuple(verified),
            missing=tuple(missing),
            **common,
        )
    if mismatched:
        return Verification(
            ok=False,
            code=ARTIFACT_MISMATCH,
            message=(
                "{} artifact(s) do not match the bytes the gate exercised ({}). The "
                "tested artifact and the published artifact are two files that share "
                "a name; download the tested bytes rather than re-building them.".format(
                    len(mismatched), ", ".join(name for name, _ in mismatched)
                )
            ),
            verified=tuple(verified),
            mismatched=tuple(name for name, _ in mismatched),
            **common,
        )

    return Verification(
        ok=True,
        message=(
            "publishing the exact {} artifact(s) the gate exercised for {} ({}): {}".format(
                len(verified),
                candidate.source_sha[:12],
                candidate.gate or "a named gate",
                ", ".join(verified),
            )
        ),
        verified=tuple(verified),
        **common,
    )


def summarize(verification: Verification) -> str:
    if verification.ok:
        return f"provenance: {verification.message}"
    return f"provenance: refused ({verification.code}) -- {verification.message}"


def build_candidate(
    *,
    source_sha: str,
    artifacts: Mapping[str, str],
    gate: str = "",
    gate_run_id: str = "",
    gate_conclusion: str = GATE_SUCCESS,
    artifact_source: str = "",
    completed_at: str = "",
) -> TestedCandidate:
    """Assemble a candidate from digests the caller computed.

    A convenience for the workflow that has just run its gate: it knows the head and
    the files, and this names the record without the caller having to shape the
    digests by hand. The digests are taken with the same function verification uses,
    so a record cannot disagree with its own bytes.
    """

    if not artifacts:
        raise ProvenanceError(
            "no_tested_artifacts",
            "a candidate with no artifacts exercises nothing, so recording one would "
            "describe an empty run as a validated release",
        )
    digests = {str(name): file_digest(str(path)) for name, path in artifacts.items()}
    return TestedCandidate(
        source_sha=source_sha,
        gate=gate,
        gate_run_id=gate_run_id,
        gate_conclusion=gate_conclusion,
        artifacts=digests,
        # The run's own working tree, when the caller does not name something
        # narrower. Written out rather than left blank because a blank reads as an
        # absence and this is a recorded fact about how the bytes were obtained.
        artifact_source=artifact_source or ".",
        completed_at=completed_at,
    )


__all__ = [
    "ARTIFACT_MISMATCH",
    "ARTIFACT_MISSING",
    "GATE_CANCELLED",
    "GATE_FAILURE",
    "GATE_MISSING",
    "GATE_NOT_GREEN",
    "GATE_SUCCESS",
    "HEAD_MISMATCH",
    "NO_ARTIFACT_SOURCE",
    "NO_CANDIDATE",
    "PASSING_GATE_STATES",
    "PROVENANCE_SCHEMA",
    "PUBLISH_HEAD_UNKNOWN",
    "ProvenanceError",
    "TestedCandidate",
    "Verification",
    "build_candidate",
    "canonical_digest",
    "canonical_source_sha",
    "file_digest",
    "load_candidate",
    "read_candidate",
    "summarize",
    "verify_candidate",
]
