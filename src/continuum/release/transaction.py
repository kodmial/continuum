"""The release transaction: many build jobs, one publication.

A release runs on many runners at once, each of which can see one target and no
destination, and then converges in one privileged job that holds the token. The
split is the security property — no job that runs a build from a fork's pull
request is anywhere near the credential that can write a release — and it is
also why this module exists: something has to merge those partial results and
refuse a partial set.

The merge is the point at which a release becomes a release, so it is the point
at which the rules live. A fragment is trusted only if it is bound to the
version, the commit, and the target this transaction is running; a build that
wrote bytes for a different commit is not a failure to recover from, it is a
fragment to reject. Every declared target must be present before anything is
uploaded, and the aggregate asset-name collision check runs here rather than in
a build job, because "these two targets both produced `app.tar.gz`" is only
knowable once both have answered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import contract
from .contract import (
    Artifact,
    ArtifactManifest,
    ContractError,
    assert_distinct_artifact_names,
    require_source_sha,
)
from .core import (
    GREEN_STATUSES,
    RELEASED,
    ReleaseComponents,
    ReleaseCore,
    ReleaseOutcome,
    ReleaseRequest,
)
from .entrypoints import EntrypointError, MatrixTarget, ReleaseMatrix, fragment_path
from .state import Journal, StageOutcome

FRAGMENT_SCHEMA = "continuum.release-fragment/v1"
RESULT_SCHEMA = "continuum.release-result/v1"

#: A fragment is a build job's report about itself. Nothing else may be named
#: here: an unrecognised field is a field this transaction does not check, and
#: a field it does not check is a field somebody can use to talk it into
#: believing something untrue.
FRAGMENT_KEYS = ("schema", "target", "version", "source_sha", "matrix", "manifests", "journal")


class TransactionError(ContractError):
    """The transaction cannot proceed and no retry of it will change that.

    Distinct from a release that failed at a stage: a `TransactionError` means
    the inputs to the transaction disagree with each other, which is a decision
    somebody has to make, not a condition to retry.
    """


class TargetFailure(ContractError):
    """One build job failed, in a way the taxonomy names.

    `code` and `retryable` travel with the failure rather than being inferred
    from a log line, so the workflow's `if:` conditions and a human reading the
    summary are looking at the same classification the build job recorded.
    """

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_outcome(self) -> StageOutcome:
        return StageOutcome.failed(
            "build", f"build/{self.code}", str(self), code=self.code, retryable=self.retryable
        )


#: How a release can fail, and what a reader is supposed to do about it. The
#: names are the workflow's vocabulary: a classification nobody can branch on is
#: a classification that gets guessed at from a log.
FAILURE_TAXONOMY: Dict[str, Tuple[bool, str]] = {
    # Transient: the same input, attempted again, can succeed.
    "provider-unavailable": (True, "the destination or a registry did not answer; retry the same version"),
    "provider-rate-limited": (True, "GitHub rate limited the run; retry later without changing the version"),
    "build-failed": (True, "a toolchain, compiler, or test failed; fix the tree and retry"),
    "transient-attestation": (True, "attestation signing could not be completed; retry the same release"),
    # A human has to look at this one.
    "validation-failed": (False, "the release policy, version, or source pin refused the release"),
    "policy-refused": (False, "a target is not releasable as configured, and no retry will change that"),
    "partial-draft": (False, "a draft exists but the release is incomplete; inspect it before retrying"),
    "incomplete-set": (False, "a target has no fragment; re-run that target's build job, then retry the transaction"),
    "already-published": (False, "this version is already published immutably; release a new version"),
    "stale-source": (False, "a build or manifest names a different commit than this release"),
    "missing-credential": (False, "a required secret or capability was not provided to this job"),
    "contract-violation": (False, "a result violated the release contract and was refused"),
    # The commands' own refusals: the inputs the jobs were supposed to be given
    # are missing or disagree. None of these is worth retrying, because the
    # workflow that produced them will produce them again.
    "matrix-absent": (False, "the resolve job did not write a matrix; a target job without one has no release to build"),
    "target-absent": (False, "the policy does not declare the target this job was dispatched for"),
    "targets-absent": (False, "the policy declares none of the matrix's targets"),
    "adapter-not-walkable": (False, "the adapter has a plan but no release adapter, so it produces no manifest to merge"),
    "repository-absent": (False, "the transaction was not told which repository to publish to"),
    "journal-invalid": (False, "the journal handed to the transaction is not a list of recorded transitions"),
    "provenance-unavailable": (False, "this release was asked to attest and cannot name the build that produced it"),
    "signing-material-unexpected": (False, "the job's signing material contradicts what it was told to expect"),
    "event-unsupported": (False, "the event cannot start a release as described; fix the trigger or the inputs"),
    "release-command": (False, "a release command was asked to do something it cannot"),
}


def classify(code: str) -> Tuple[bool, str]:
    """Is this failure worth retrying, and what should a reader be told?

    An unrecognised code is fatal. Reading "nobody said it was retryable" as
    "retryable" is how a release loop turns a permanent refusal into an
    unattended rebuild every few minutes.
    """

    found = FAILURE_TAXONOMY.get(code)
    if found is None:
        return (False, f"unclassified failure {code!r}; treated as fatal")
    return found


@dataclass(frozen=True)
class TargetFragment:
    """One build job's result, as the transaction receives it.

    A fragment is bound to the release that asked for it. `version` and
    `source_sha` are the binding, and they are re-checked on read rather than
    trusted because the file crossed a job boundary: a build job that wrote
    bytes for the wrong commit would otherwise contribute a manifest that
    passes every structural check and ships unreviewed code.
    """

    target: str
    version: str
    source_sha: str
    matrix: ReleaseMatrix
    manifests: Tuple[ArtifactManifest, ...] = ()
    journal: Tuple[StageOutcome, ...] = ()

    def __post_init__(self) -> None:
        require_source_sha(self.source_sha, f"fragment for target {self.target!r}")
        if not self.version:
            raise TransactionError(
                f"fragment for target {self.target!r} names no version; an unbound result "
                "cannot be told apart from another release's"
            )
        if not self.manifests:
            raise TransactionError(
                f"fragment for target {self.target!r} carries no manifest. A build job that "
                "produced no manifest either produced nothing or lost what it produced, and "
                "the transaction publishes neither"
            )
        seen = [manifest.target for manifest in self.manifests]
        if set(seen) != {self.target}:
            raise TransactionError(
                f"fragment for target {self.target!r} carries manifests for "
                f"{', '.join(sorted(set(seen)))}. A build job builds the one target its row "
                "names; a fragment that reports for others is reporting for a release this "
                "transaction is not running"
            )
        for manifest in self.manifests:
            manifest.assert_release_source(self.source_sha)
            manifest.assert_release_version(self.version)

    @property
    def manifest(self) -> ArtifactManifest:
        return self.manifests[0]

    def unit_outcomes(self) -> Tuple[StageOutcome, ...]:
        """The per-target entries of this fragment's journal.

        A single-target run records an aggregate key per stage as well — one
        `build` entry for the whole release, not one for this target. Only the
        per-target entries are carried forward: the aggregate key means "the
        build stage of this release is done", and three fragments each claiming
        that would let one target's build stand in for the other two.
        """

        return tuple(entry for entry in self.journal if entry.detail("target"))

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": FRAGMENT_SCHEMA,
            "target": self.target,
            "version": self.version,
            "source_sha": self.source_sha,
            "matrix": self.matrix.describe(),
            "manifests": [manifest.describe() for manifest in self.manifests],
            "journal": [entry.describe() for entry in self.journal],
        }

    def to_json(self) -> str:
        return json.dumps(self.describe(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_describe(cls, payload: Mapping[str, Any]) -> "TargetFragment":
        if not isinstance(payload, Mapping):
            raise TransactionError(
                f"a target fragment is a mapping, not {type(payload).__name__}; a file that "
                "is not a fragment is not evidence of a build"
            )
        unknown = sorted(set(payload) - set(FRAGMENT_KEYS))
        if unknown:
            raise TransactionError(
                f"target fragment carries unknown field(s) {', '.join(unknown)}; a field this "
                "transaction does not check cannot be used to make it agree with something"
            )
        schema = payload.get("schema")
        if schema != FRAGMENT_SCHEMA:
            raise TransactionError(
                f"target fragment declares schema {schema!r}; this transaction reads "
                f"{FRAGMENT_SCHEMA!r} and will not guess at the rest"
            )
        return cls(
            target=payload.get("target") or "",
            version=payload.get("version") or "",
            source_sha=payload.get("source_sha") or "",
            matrix=_matrix_from_describe(payload.get("matrix")),
            manifests=tuple(
                manifest_from_describe(item) for item in _require_sequence(payload.get("manifests"), "manifests")
            ),
            journal=tuple(
                outcome_from_describe(item) for item in _require_sequence(payload.get("journal"), "journal")
            ),
        )

    @classmethod
    def from_json(cls, document: str) -> "TargetFragment":
        return cls.from_describe(_require_json(document, "target fragment"))


def write_fragment(root: str, fragment: TargetFragment) -> str:
    """Write a build job's result where the transaction will look for it.

    The path is the one the matrix row named, so a fragment cannot be written
    somewhere the transaction will not read, and so two targets cannot write to
    one file and clobber each other's manifests.
    """

    path = fragment_path(root, fragment.target)
    _write_json(path, fragment.describe())
    return path


def read_fragment(root: str, matrix: ReleaseMatrix, target_id: str) -> TargetFragment:
    """Read one build job's result and refuse it if it is not this release's."""

    target = matrix.target(target_id)
    path = fragment_path(root, target.target)
    if not Path(path).is_file():
        raise TargetFailure(
            f"target {target.target!r} left no result at {path}. The build job did not run, "
            f"was cancelled, or wrote somewhere else; nothing can be published for it",
            code="build-failed",
            retryable=True,
        )
    fragment = TargetFragment.from_json(_read(path))
    _bind(fragment, target, matrix)
    return fragment


def collect_fragments(root: str, matrix: ReleaseMatrix, *, targets: Sequence[str] = ()) -> Tuple[TargetFragment, ...]:
    """Gather every target's result, and refuse a partial set.

    The refuse is the whole reason the transaction exists. A release whose third
    target's runner died has not released anything; if the transaction published
    the two that finished, the published set would differ from the declared one
    and nothing downstream could tell that from a release that was always two
    targets.
    """

    wanted = tuple(targets) or matrix.buildable_ids
    if not wanted:
        raise TransactionError(
            "the matrix has no buildable target, so there is nothing to collect. A release "
            "with no build is a plan, not a transaction"
        )
    fragments = []
    missing: list[str] = []
    for target_id in wanted:
        try:
            fragments.append(read_fragment(root, matrix, target_id))
        except TargetFailure as exc:
            if not Path(fragment_path(root, target_id)).is_file():
                missing.append(target_id)
                continue
            raise
    if missing:
        raise TransactionError(
            f"target(s) {', '.join(sorted(missing))} produced no result, so publishing now "
            f"would release {len(wanted) - len(missing)} of {len(wanted)} buildable targets. A "
            "partial release is indistinguishable from a smaller one, which is why this is "
            "refused rather than summarised; re-run the build job for the missing target or "
            "release without it",
            code="incomplete-set",
        )
    bound = {fragment.target for fragment in fragments}
    declared = set(matrix.target_ids)
    missing = sorted(declared - bound - set(matrix.declared_ids))
    if missing:
        raise TransactionError(
            f"target(s) {', '.join(missing)} produced no result, so publishing now would "
            f"release {len(bound)} of {len(declared)} declared targets. A partial release is "
            "indistinguishable from a smaller one, which is why this is refused rather than "
            "summarised",
            code="incomplete-set",
        )
    return tuple(sorted(fragments, key=lambda fragment: fragment.target))


def merge_journal(fragments: Sequence[TargetFragment]) -> Journal:
    """One journal carrying every target's work, in a deterministic order.

    Sorted so that two runs over the same fragments produce the same journal,
    which is what makes a resumed transaction comparable with the one before it.
    """

    entries: list[StageOutcome] = []
    seen = set()
    for fragment in sorted(fragments, key=lambda item: item.target):
        for entry in fragment.unit_outcomes():
            if entry.key in seen:
                # Two fragments claiming one unit key is either a duplicated
                # job or a rewritten one. The record has to be a fact, so the
                # first trusted completion wins and a later claim is dropped
                # rather than merged into a key that means two things.
                continue
            seen.add(entry.key)
            entries.append(entry)
    return Journal(entries=tuple(entries))


def merge_manifests(fragments: Sequence[TargetFragment]) -> Tuple[ArtifactManifest, ...]:
    """Every target's manifest, checked for the collision the build jobs cannot see."""

    manifests = tuple(
        fragment.manifest for fragment in sorted(fragments, key=lambda item: item.target)
    )
    try:
        assert_distinct_artifact_names(
            artifact for manifest in manifests for artifact in manifest.artifacts
        )
    except ContractError as exc:
        raise TransactionError(
            f"{exc} The transaction is the first point at which all targets have answered, "
            "so this is checked here rather than in a build job that cannot see the others"
        ) from None
    return manifests


@dataclass(frozen=True)
class ReleaseResult:
    """What the workflow reports to its caller.

    The fields are the release's own facts rather than the core's, and they are
    the ones a job summary, a downstream repository, and a human all need to
    agree on. `outputs` is flattened on purpose: a workflow's outputs are strings,
    and a result that had to be re-shaped per consumer is a result whose consumers
    could disagree.
    """

    status: str
    version: str
    tag: str
    source_sha: str
    release: str
    channel: str
    targets: Tuple[str, ...] = ()
    declared: Tuple[str, ...] = ()
    manifests: Tuple[ArtifactManifest, ...] = ()
    outcome: Optional[ReleaseOutcome] = None
    fragment: Optional[TargetFragment] = None
    summary: str = ""

    @property
    def ok(self) -> bool:
        return self.status in GREEN_STATUSES

    @property
    def released(self) -> bool:
        return self.status == RELEASED

    @property
    def retryable(self) -> bool:
        """The failure's own classification, never re-derived from its name.

        A component that records `retryable=False` for a code this table would
        call transient knows something the table does not — a rate limit that
        will not clear, a build that will not pass on this tree. Re-deciding
        here would overwrite that judgement with a spelling match.
        """

        outcome = self.outcome
        if outcome is not None and outcome.failure is not None:
            return bool(outcome.failure.retryable)
        return False

    @property
    def code(self) -> str:
        return self.failure_code()

    def failure_code(self) -> str:
        outcome = self.outcome
        if outcome is not None and outcome.failure is not None:
            return outcome.failure.code
        return ""

    @property
    def exit_code(self) -> int:
        """What the job should exit with.

        Retryable failures and fatal ones both fail the job — a release that
        half-published is not a success — but they answer different questions to
        the caller, which is what `code` and `retryable` are for.
        """

        return 0 if self.ok else 1

    @property
    def outputs(self) -> Dict[str, str]:
        payload = {
            "status": self.status,
            "ok": "true" if self.ok else "false",
            "retryable": "true" if self.retryable else "false",
            "code": self.code,
            "version": self.version,
            "tag": self.tag,
            "source_sha": self.source_sha,
            "release": self.release,
            "channel": self.channel,
            "targets": ",".join(self.targets),
            "declared": ",".join(self.declared),
            "summary": self.summary,
        }
        return {name: str(value) for name, value in payload.items()}

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "status": self.status,
            "ok": self.ok,
            "version": self.version,
            "tag": self.tag,
            "source_sha": self.source_sha,
            "release": self.release,
            "channel": self.channel,
            "targets": list(self.targets),
            "declared": list(self.declared),
            "outputs": self.outputs,
            "summary": self.summary,
        }
        if self.manifests:
            payload["manifests"] = [manifest.describe() for manifest in self.manifests]
        if self.outcome is not None:
            payload["stages"] = self.outcome.describe()["stages"]
        return payload

    def to_json(self) -> str:
        return json.dumps(self.describe(), indent=2, sort_keys=True) + "\n"

    def job_summary(self) -> str:
        lines = [f"## Continuum release: {self.status}", "", self.summary or "no summary"]
        if self.tag:
            lines.append(f"- tag: `{self.tag}`")
        if self.source_sha:
            lines.append(f"- source: `{self.source_sha}`")
        if self.targets:
            lines.append(f"- targets: {', '.join(self.targets)}")
        if self.declared:
            lines.append(
                f"- **not shipped**: {', '.join(self.declared)} — declared in the policy, "
                "with no release adapter, so this release contains no artifact for it. A "
                "consumer expecting every declared target is looking at a release that is "
                "smaller than the policy"
            )
        if not self.ok and self.code:
            retryable, advice = classify(self.code)
            lines.append(f"- failure `{self.code}` ({'retryable' if retryable else 'needs a human'}): {advice}")
        return "\n".join(lines) + "\n"


def summarise(outcome: ReleaseOutcome) -> str:
    """One sentence a job summary and a caller can both read.

    Composed here rather than taken from the core because the core records
    outcomes per stage and a release has one status: the sentence that matters
    is the one that says whether the assets are public, and which tag holds
    them.
    """

    if outcome.failure is not None:
        return (
            f"the {outcome.failure.stage} stage failed: {outcome.failure.summary} "
            f"[{outcome.failure.code}]"
        )
    if outcome.published:
        destinations = sorted({item.destination for item in outcome.publications})
        return (
            f"{outcome.tag} is published to {', '.join(destinations)} from "
            f"{outcome.source_sha[:7]}"
        )
    if outcome.no_op_reason:
        return f"nothing to do: {outcome.no_op_reason}"
    return f"the release finished as {outcome.status}"


# -- the three jobs ----------------------------------------------------------


def resolve_release(
    config: Any,
    *,
    event: Any,
    version: str,
    source_sha: str,
    matrix: ReleaseMatrix,
    channel: str = "",
    release_pr_number: int = 0,
    workdir: str = "",
    version_checks: Sequence[Any] = (),
) -> ReleaseRequest:
    """Turn a validated policy and a pinned commit into one request.

    The request is what both the build jobs and the transaction execute, so
    building it here is what makes the two agree about the release. Nothing is
    derived from the runner: a request that depended on which machine it was
    built on would be a different release per runner.
    """

    request = ReleaseRequest.from_config(
        config,
        event,
        version_checks=tuple(version_checks),
        requested_version=version,
        release_pr_number=release_pr_number,
        workdir=workdir or os.environ.get("CONTINUUM_WORKDIR", "") or ".",
        channel=channel or "default",
    )
    if matrix.version != version:
        raise TransactionError(
            f"the matrix builds version {matrix.version!r} for a release pinned to "
            f"{version!r}. The two jobs that carry a release's identity were handed different "
            "ones, and neither can prove which is right"
        )
    if matrix.source_sha != source_sha:
        raise TransactionError(
            f"the matrix builds {matrix.source_sha} for a release pinned to {source_sha}"
        )
    if event.sha != source_sha:
        raise TransactionError(
            f"the event names commit {event.sha} but this release is pinned to {source_sha}. "
            "The event is the fact the release rests on; building anything else from it would "
            "publish bytes the reviewed commit did not produce"
        )
    return request


def build_target(
    request: ReleaseRequest,
    matrix: ReleaseMatrix,
    target_id: str,
    *,
    components: ReleaseComponents,
) -> TargetFragment:
    """Run one target's half of the release and report what it did.

    No publisher is reachable from here: the components handed to a build job
    carry adapters and nothing with a destination in it, so the strongest thing
    this function can do is fail. The fragment it writes is the transaction's
    evidence, and it carries the journal's per-target entries so a later run can
    skip the work without repeating it.
    """

    target = matrix.target(target_id)
    if target.declared:
        raise TransactionError(
            f"target {target_id!r} is declared rather than built: no adapter can produce it yet, "
            "so there is nothing for a build job to run. A declared target is published from "
            "its plan, and needs no runner"
        )
    if target.source_sha != request.event.sha:
        raise TransactionError(
            f"target {target_id!r} is bound to {target.source_sha} but the release is pinned to "
            f"{request.event.sha}. The matrix and the event disagree about the commit"
        )
    if target.version != request.requested_version:
        raise TransactionError(
            f"target {target_id!r} carries version {target.version!r} but the release is "
            f"{request.requested_version!r}"
        )
    spec = _spec_for(request, target_id)
    scoped = _request_for(request, spec)
    outcome = ReleaseCore(components).execute(scoped)
    if not outcome.ok:
        raise _target_failure(outcome, target_id)
    fragment = TargetFragment(
        target=target_id,
        version=target.version,
        source_sha=target.source_sha,
        matrix=matrix,
        manifests=tuple(manifest for manifest in outcome.manifests if manifest.target == target_id),
        journal=outcome.journal.entries,
    )
    return fragment


def publish_release(
    request: ReleaseRequest,
    matrix: ReleaseMatrix,
    fragments: Sequence[TargetFragment],
    *,
    components: ReleaseComponents,
    journal: Optional[Journal] = None,
) -> ReleaseResult:
    """Merge every target's work, publish it once, and report the facts.

    The core is executed a second time here, with the merged journal and
    manifests. That is the resumption mechanism, not a second release: the build
    stages find their own unit keys already recorded and skip, and the walk
    continues at draft. So the publish path is the same code whether the build
    ran in this job or in a hundred jobs an hour ago, and a release that is
    re-run after a provider outage does not rebuild what it already built.
    """

    merged_manifests = merge_manifests(fragments)
    merged_journal = journal if journal is not None else merge_journal(fragments)
    outcome = ReleaseCore(components).execute(
        request,
        journal=merged_journal,
        manifests=merged_manifests,
    )
    return ReleaseResult(
        status=outcome.status,
        version=outcome.version or request.requested_version,
        tag=outcome.tag,
        source_sha=outcome.source_sha or request.event.sha,
        release=outcome.release,
        channel=outcome.channel or request.channel,
        targets=tuple(fragment.target for fragment in fragments),
        declared=tuple(
            target for target in matrix.declared_ids if target not in {f.target for f in fragments}
        ),
        manifests=merged_manifests,
        outcome=outcome,
        summary=summarise(outcome),
    )


def transaction(
    request: ReleaseRequest,
    matrix: ReleaseMatrix,
    *,
    fragment_root: str,
    components: ReleaseComponents,
    journal: Optional[Journal] = None,
    targets: Sequence[str] = (),
) -> ReleaseResult:
    """The privileged job: collect, merge, publish, report.

    `fragment_root` is the one input that is not the release, and it is where
    the build jobs' evidence was collected. It is passed separately because the
    transaction must not infer it from a workflow's convenience: a transaction
    that read fragments from wherever the last job happened to write would
    publish whatever it found.
    """

    assert_request_matches(matrix, request)
    fragments = collect_fragments(fragment_root, matrix, targets=targets)
    return publish_release(request, matrix, fragments, components=components, journal=journal)


def assert_request_matches(matrix: ReleaseMatrix, request: ReleaseRequest) -> None:
    """Refuse a transaction whose matrix and request describe different releases.

    The matrix is what the build jobs were dispatched with and the request is
    what the core walks. They are built in different jobs from the same inputs
    and are handed to this one as separate files, so this is where the two are
    finally compared — and a release that merged a matrix from one version into
    a request for another would publish the wrong bytes with the right tag.
    """

    if matrix.version != request.requested_version:
        raise TransactionError(
            f"the matrix builds {matrix.version!r} but the release is "
            f"{request.requested_version!r}"
        )
    if matrix.source_sha != request.event.sha:
        raise TransactionError(
            f"the matrix builds {matrix.source_sha} but the release is pinned to "
            f"{request.event.sha}"
        )
    unknown = sorted(set(matrix.target_ids) - {spec.id for spec in request.targets})
    if unknown:
        raise TransactionError(
            f"the matrix builds target(s) {', '.join(unknown)} that the release does not "
            "declare. The policy decides what a release is; a matrix may narrow it to a subset, "
            "never widen it"
        )


# -- serialisation helpers ---------------------------------------------------


def manifest_from_describe(payload: Any) -> ArtifactManifest:
    if not isinstance(payload, Mapping):
        raise TransactionError(
            f"a manifest is a mapping, not {type(payload).__name__}; a build job that could not "
            "write one is a build job whose output cannot be checked"
        )
    try:
        artifacts = tuple(
            Artifact(
                target=item.get("target") or "",
                source_sha=item.get("source_sha") or "",
                version=item.get("version") or "",
                name=item.get("name") or "",
                path=item.get("path") or "",
                type=item.get("type") or "",
                size=int(item.get("size") or 0),
                digest=item.get("digest") or "",
                digest_algorithm=item.get("digest_algorithm") or "",
                platform=item.get("platform") or "",
                arch=item.get("arch") or "",
                classifier=item.get("classifier") or "",
                media_type=item.get("media_type") or "",
                signing=item.get("signing") or "",
                signing_identity=item.get("signing_identity") or "",
                verification=item.get("verification") or "",
                verified_by=item.get("verified_by") or "",
                provenance=item.get("provenance") or "",
                attestation=item.get("attestation") or "",
                declared=bool(item.get("declared")),
            )
            for item in _require_sequence(payload.get("artifacts"), "manifest.artifacts")
        )
        return ArtifactManifest(
            target=payload.get("target") or "",
            source_sha=payload.get("source_sha") or "",
            version=payload.get("version") or "",
            adapter=payload.get("adapter") or "",
            contract_version=int(payload.get("contract_version") or contract.CONTRACT_VERSION),
            artifacts=artifacts,
        )
    except ContractError as exc:
        raise TransactionError(
            f"a manifest in a target fragment is not a manifest: {exc}"
        ) from None


def outcome_from_describe(payload: Any) -> StageOutcome:
    if not isinstance(payload, Mapping):
        raise TransactionError(
            f"a journal entry is a mapping, not {type(payload).__name__}"
        )
    try:
        return StageOutcome(
            stage=payload.get("stage") or "",
            key=payload.get("key") or "",
            outcome=payload.get("outcome") or "",
            summary=payload.get("summary") or "",
            retryable=bool(payload.get("retryable")),
            code=payload.get("code") or "",
            details=tuple(
                (name, str(value)) for name, value in (payload.get("details") or {}).items()
            ),
        )
    except ContractError as exc:
        raise TransactionError(f"a journal entry in a target fragment is not one: {exc}") from None


def _target_failure(outcome: ReleaseOutcome, target_id: str) -> TargetFailure:
    """Turn a core failure into the taxonomy's name for it.

    A build that fails is the common case, and it is the one failure a workflow
    is most tempted to report as a string. Naming it means the transaction, the
    job summary, and any retry policy all quote the same code.
    """

    failure = outcome.failure
    if failure is None:
        return TargetFailure(
            f"target {target_id!r} did not complete: {outcome.status}",
            code="build-failed",
            retryable=True,
        )
    # The core's own code is passed through rather than rewritten, and
    # `retryable` comes from the recorded outcome rather than from the
    # taxonomy. The core already decided, and it decided that a component which
    # claimed nothing is fatal. Rewriting its word to fit the table would lose
    # the one detail that tells a reader which stage broke: `verify-failed` says
    # something `contract-violation` does not, and the table's job is to advise,
    # not to rename. An unlisted code simply gets the fail-closed advice, which
    # is the honest answer for a failure this code has not been taught about.
    code = failure.code or "contract-violation"
    return TargetFailure(
        f"target {target_id!r} failed at {failure.stage}: {failure.summary}",
        code=code,
        retryable=failure.retryable,
    )


def _spec_for(request: ReleaseRequest, target_id: str) -> contract.TargetSpec:
    for spec in request.targets:
        if spec.id == target_id:
            return spec
    raise TransactionError(
        f"target {target_id!r} is in the matrix but not in the release; the two disagree about "
        "what this release is, and neither can be trusted over the other"
    )


def _request_for(request: ReleaseRequest, spec: contract.TargetSpec) -> ReleaseRequest:
    """The same release, narrowed to one target.

    Every field is carried across unchanged apart from the target list, so the
    build job and the transaction are provably running the same release with
    the same policy and the same pin; a job that could quietly differ here is a
    job publishing bytes the transaction did not ask for.
    """

    return ReleaseRequest(
        event=request.event,
        targets=(spec,),
        enabled=request.enabled,
        version_policy=request.version_policy,
        version_checks=request.version_checks,
        requested_version=request.requested_version,
        release_pr_number=request.release_pr_number,
        workdir=request.workdir,
        dry_run=request.dry_run,
        channel=request.channel,
    )


def _bind(fragment: TargetFragment, target: MatrixTarget, matrix: ReleaseMatrix) -> None:
    if fragment.target != target.target:
        raise TransactionError(
            f"the result found for {target.target!r} reports for {fragment.target!r}. Two build "
            "jobs wrote over each other, or a fragment was copied; either way the manifests in "
            "it cannot be attributed to the target they claim"
        )
    if fragment.source_sha != target.source_sha:
        raise TargetFailure(
            f"target {target.target!r} reported a build of {fragment.source_sha} but this "
            f"release is pinned to {target.source_sha}. Nothing built from another commit can be "
            "published, whatever the manifest says about itself",
            code="stale-source",
        )
    if fragment.version != target.version:
        raise TargetFailure(
            f"target {target.target!r} reported version {fragment.version!r} but this release is "
            f"{target.version!r}; the tag and the assets would disagree",
            code="stale-source",
        )
    if fragment.matrix.describe() != matrix.describe():
        raise TransactionError(
            f"target {target.target!r} reported a result for a different matrix than the one it "
            "was dispatched with. A build job that ran a different job than the transaction "
            "expects produced something nobody has reviewed"
        )


def _matrix_from_describe(payload: Any) -> ReleaseMatrix:
    if not isinstance(payload, Mapping):
        raise TransactionError(
            f"a fragment's matrix is a mapping, not {type(payload).__name__}; without it the "
            "result cannot be bound to the job that produced it"
        )
    try:
        return ReleaseMatrix.from_describe(payload)
    except EntrypointError as exc:
        raise TransactionError(f"a fragment carries an unusable matrix: {exc}") from None


def _require_json(document: str, what: str) -> Any:
    try:
        return json.loads(document)
    except (TypeError, ValueError) as exc:
        raise TransactionError(
            f"the {what} is not JSON ({exc}). A file that is not a {what} is not evidence, and "
            "publishing on the strength of a file nobody can read would be a guess"
        ) from None


def _require_sequence(value: Any, where: str) -> Sequence[Any]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TransactionError(f"{where} must be a list, not {type(value).__name__}")
    return value


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TargetFailure(
            f"the result at {path} could not be read ({exc.strerror}); a build job's output that "
            "cannot be read cannot be published",
            code="build-failed",
            retryable=True,
        ) from None


def _write_json(path: str, payload: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = f"{path}.partial"
    Path(temporary).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def target_ids_from(matrix: ReleaseMatrix) -> Tuple[str, ...]:
    return matrix.buildable_ids


__all__ = [
    "FAILURE_TAXONOMY",
    "FRAGMENT_SCHEMA",
    "RESULT_SCHEMA",
    "ReleaseResult",
    "TargetFailure",
    "TargetFragment",
    "TransactionError",
    "build_target",
    "classify",
    "collect_fragments",
    "manifest_from_describe",
    "merge_journal",
    "merge_manifests",
    "outcome_from_describe",
    "publish_release",
    "read_fragment",
    "resolve_release",
    "target_ids_from",
    "transaction",
    "write_fragment",
]
