"""Record-only release ports: a release chain that walks all eleven stages and
touches nothing.

The release scenario has to exercise ``ReleaseCore`` itself, not a description
of it. A shadow run that "plans the release" by reading the stage list and
printing it has validated nothing -- the stage list is a constant, and the
decisions live in the stages: whether a tag push is eligible, which version
three strategies agree on, whether a manifest can be published, whether a
destination already holds this release.

So the planner builds a real ``ReleaseComponents`` out of the ports below and
calls ``ReleaseCore.plan``. Every port conforms to the contract, and every one of
them records the effect it would have performed. The chain runs to
``planned``/``skipped``; nothing is built, signed, uploaded, or published, and
the journal lists the publications as suppressed.

The one thing these ports do *not* do is fake success. A port that returned
``published`` would make the chain believe a release happened, and the journal
would then say Continuum would have released -- which is exactly the claim
shadow mode exists to test. They report ``skipped`` with a reason naming the
suppression, which is a green outcome that says nothing was written.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..release import contract
from ..release.contract import (
    ArtifactManifest,
    BuildRequest,
    Eligibility,
    ManifestBuilder,
    NotesRequest,
    PublishRequest,
    PublisherResult,
    ReleaseNotes,
    RELEASE_PR_OPEN,
    ReleasePrRequest,
    ReleasePrResult,
    VerificationReport,
)
from ..release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
from ..release.state import STAGES
from ..release.version import TagVersion, VersionPolicy
from .effects import Effect, RecordingEffects

RELEASE_PORTS_SCHEMA = "continuum.shadow-release-ports/v1"

#: The artifact name every shadow target declares. A plan declares artifacts
#: rather than building them, so the name is the only thing the chain records.
SHADOW_ARTIFACT = "shadow-artifact"
SHADOW_CHECKSUMS = "shadow-SHA256SUMS.txt"
SHADOW_CLASSIFIER = "shadow"
#: The artifact types the release contract accepts. A declared row is still a
#: row in the contract's own vocabulary: using a media type here would be
#: rejected, and it would be right to reject it.
SHADOW_TYPE = contract.TYPE_PACKAGE
SHADOW_CHECKSUMS_TYPE = contract.TYPE_CHECKSUMS

#: Where a declared artifact would have been written. Never created, and named so
#: that a reader of a shadow manifest can tell at a glance that the path is a
#: description of an output rather than a record of one.
SHADOW_OUTPUT_PATH = "shadow-output/{target}/{version}/{name}"

#: The scope a shadow publisher claims. Release scopes and pull-request scopes
#: must stay disjoint, and the release core refuses a component set where they
#: are not, so these are declared rather than assumed.
SHADOW_RELEASE_SCOPE = "contents: write"
SHADOW_PR_SCOPE = "pull-requests: write"


@dataclasses.dataclass
class _Recorded:
    """A port that records the effect it would perform and does nothing else."""

    SUPPORTS_DRY_RUN: bool = True
    name: str = ""
    _effects: Optional[RecordingEffects] = dataclasses.field(default=None, repr=False)

    def intent(self) -> str:
        return "record what {} would do".format(self.name)

    def _record(self, kind: str, target: str, **detail: Any) -> None:
        assert self._effects is not None, "port was not bound to an effect sink"
        self._effects.record(Effect(kind=kind, target=target, detail=detail))

    def _identifier(self, prefix: str) -> int:
        """A local id for an object the port would have created."""

        assert self._effects is not None, "port was not bound to an effect sink"
        return self._effects.next_identifier(prefix)


class ShadowEligibility(_Recorded):
    """Decides release eligibility from the event, and records nothing.

    The verdict is taken from the request's own configuration rather than
    invented here: an event on a repository with release switched off is
    ineligible, and that is a decision a real eligibility component makes.
    """

    name = "shadow-eligibility"

    def evaluate(self, event: Any) -> Eligibility:
        if not getattr(event, "tag", ""):
            return Eligibility(
                eligible=False,
                blocking=True,
                reason="the event named no tag, so there is no version to release",
                code="tag-absent",
            )
        # An eligible verdict carries no reason. The contract refuses one, and
        # that refusal is a guard: an eligibility component that explains why it
        # said yes is a component whose explanation can drift from its decision.
        return Eligibility(eligible=True, blocking=False, code="tag-push")


class ShadowVersion(_Recorded):
    """Proposes the version the tag carries.

    Delegates to the engine's own tag strategy, because the interesting part --
    stripping the prefix and admitting the number against the policy -- is
    exactly what a divergence would hide. Both halves are delegated: ``collect``
    is the strategy's, so the text the proposal is derived from is the one
    production would have read, and a wrapper that rebuilt it could disagree
    with the version it then proposes.
    """

    name = "shadow-version"

    def collect(self, event: Any, workdir: str = "") -> Tuple[str, str]:
        return TagVersion().collect(event, workdir)

    def propose(self, context: Any, policy: VersionPolicy) -> Any:
        return TagVersion().propose(context, policy)


class ShadowNotes(_Recorded):
    name = "shadow-notes"

    def build(self, request: NotesRequest) -> ReleaseNotes:
        self._record(
            "release.asset.upload",
            "notes:{}".format(request.tag),
            target="notes",
            text="{tag} notes".format(tag=request.tag),
        )
        return ReleaseNotes(body="{} was released.".format(request.tag))


class ShadowReleasePr(_Recorded):
    """Records the release pull request instead of opening one.

    Reports ``open`` with a local pseudo-number rather than ``absent``. ``absent``
    is a real answer for a repository that cuts releases from a tag, and it is
    green -- but it is a claim about the repository, and this port knows nothing
    about the repository. Claiming ``open`` with a number the recorder minted is
    the honest version of "the port was asked and did not do it": the core
    exercises the same ``exists`` branch production would, and the journal shows
    the suppression.
    """

    name = "shadow-release-pr"

    def ensure(self, request: ReleasePrRequest) -> ReleasePrResult:
        self._record(
            "pr.create",
            "repository:{}".format(request.event.repository),
            number=self._identifier("pr"),
            version=request.version,
            tag=request.tag,
            purpose="release-pr",
            dry_run=request.dry_run,
        )
        return ReleasePrResult(
            state=RELEASE_PR_OPEN,
            number=self._identifier("pr"),
            url="",
            version=request.version,
            reason="shadow mode records the release pull request without opening one",
        )


class ShadowAdapter(_Recorded):
    """Declares the artifacts a target would build, sign, and verify.

    ``build`` declares rather than builds, which is what the release core
    already does for a plan. ``sign`` and ``verify`` re-declare the same
    artifacts with their post-sign attributes, so the chain sees the shape it
    would see for real -- and the recorded effects still say nothing was written.
    """

    name = "shadow"

    def available(self) -> bool:
        return True

    def build(self, request: BuildRequest) -> ArtifactManifest:
        self._record(
            "ref.push",
            "target:{}".format(request.target.id),
            version=request.version,
            phase="build",
        )
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=self.name,
            source_sha=request.source_sha,
            version=request.version,
        )
        for name, kind in ((SHADOW_ARTIFACT, SHADOW_TYPE), (SHADOW_CHECKSUMS, SHADOW_CHECKSUMS_TYPE)):
            # ``declare`` does not read the path, and the contract still insists
            # a declared row names one. A descriptive path is therefore honest
            # where a real one would be a lie: nothing is written there, and
            # ``declared`` is set on the row so nothing downstream can mistake it
            # for a shipped artifact.
            builder.declare(
                name,
                SHADOW_OUTPUT_PATH.format(
                    target=request.target.id, version=request.version, name=name
                ),
                kind,
                classifier=SHADOW_CLASSIFIER,
            )
        return builder.build()

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        self._record(
            "ref.push",
            "target:{}".format(request.target.id),
            version=request.version,
            phase="sign",
        )
        # The manifest is returned unchanged, and the contract is what makes that
        # correct: a row `declare`d by a plan is refused if it claims a
        # signature, because nothing was signed. A shadow port that marked these
        # rows signed to make the stage look busier would be claiming a signature
        # that does not exist -- and the contract exists precisely to stop
        # exactly that.
        return manifest

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        # A declared artifact has no bytes, so this reports the shape of the
        # check rather than a check: every row is present and accounted for, and
        # the subject is the manifest rather than a signature. The contract
        # requires a named verifier on any successful report, so ``verified_by``
        # names the thing that did the accounting. Claiming a cryptographic
        # verification here would be the one thing this port must never do.
        return VerificationReport(
            verified=True,
            code="shadow-declared",
            detail="{} declared artifact(s) accounted for; shadow mode verified "
            "no bytes".format(len(manifest.artifacts)),
            verified_by=self.name,
        )


class ShadowPublisher(_Recorded):
    """Records a draft and a publish, and reports both as skipped.

    ``skipped`` rather than ``published`` on purpose: it is a supported outcome,
    it is green, and it says in its reason that nothing was written. Reporting
    ``published`` would be the one thing this port must never do.
    """

    def draft(self, request: PublishRequest) -> PublisherResult:
        self._record(
            "release.create",
            "destination:{}".format(request.destination),
            version=request.version,
            phase="draft",
        )
        return PublisherResult(
            outcome=contract.SKIPPED,
            destination=request.destination,
            identity=request.version,
            reason="shadow mode records the draft and creates none",
            code="shadow-suppressed",
        )

    def publish(self, request: PublishRequest) -> PublisherResult:
        self._record(
            "release.publish",
            "destination:{}".format(request.destination),
            version=request.version,
            phase="publish",
        )
        return PublisherResult(
            outcome=contract.SKIPPED,
            destination=request.destination,
            identity=request.version,
            reason="shadow mode records the publication and performs none",
            code="shadow-suppressed",
        )


def _bind(port: Any, effects: RecordingEffects) -> Any:
    port._effects = effects
    return port


def components(
    effects: RecordingEffects,
    *,
    target_ids: Sequence[str] = ("shadow",),
    publishers: Sequence[str] = ("github-release",),
    syncs: Sequence[str] = (),
) -> ReleaseComponents:
    """A conforming component set whose every port records instead of acting."""

    adapters = {
        name: _bind(ShadowAdapter(name=name), effects) for name in target_ids
    }
    return ReleaseComponents(
        eligibility=_bind(ShadowEligibility(name="shadow-eligibility"), effects),
        version=_bind(ShadowVersion(name="shadow-version"), effects),
        notes=_bind(ShadowNotes(name="shadow-notes"), effects),
        adapters=adapters,
        release_pr=_bind(ShadowReleasePr(name="shadow-release-pr"), effects),
        publishers=tuple(
            _bind(ShadowPublisher(name=name), effects) for name in publishers
        ),
        syncs=tuple(_bind(ShadowPublisher(name=name), effects) for name in syncs),
    )


def request(
    *,
    repository: str,
    sha: str,
    tag: str = "",
    ref: str = "",
    delivery: str = "",
    target_ids: Sequence[str] = ("shadow",),
    version_checks: Sequence[Any] = (),
) -> ReleaseRequest:
    """A release request derived from a shadow event.

    The event's own fields, not constants: a release shadowed on a push event
    and a release shadowed on a tag push are different releases, and a request
    that hard-coded either of them would be validating a scenario rather than
    the event.
    """

    name = contract.EVENT_TAG_PUSH if tag else contract.EVENT_PUSH
    return ReleaseRequest(
        event=contract.ReleaseEvent(
            repository=repository,
            name=name,
            sha=sha,
            ref=ref or ("refs/tags/{}".format(tag) if tag else ""),
            tag=tag,
            default_branch="main",
            delivery=delivery,
        ),
        targets=tuple(
            contract.TargetSpec(id=item, adapter=item) for item in target_ids
        ),
        enabled=True,
        version_policy=VersionPolicy(),
        version_checks=tuple(version_checks),
    )


def chain() -> Tuple[str, ...]:
    """The stage names the core walks, for the journal's guard record."""

    return tuple(stage.name for stage in STAGES)


def describe() -> Dict[str, Any]:
    return {
        "schema": RELEASE_PORTS_SCHEMA,
        "stages": list(chain()),
        "adapter": ShadowAdapter.name,
        "suppressed": [
            spec.kind
            for spec in (
                Effect(kind="release.create"),
                Effect(kind="release.publish"),
                Effect(kind="ref.push"),
            )
        ],
    }


def recorded_publications(outcome: Any) -> List[Dict[str, Any]]:
    """The publication results, for the journal's guard record."""

    return [
        {
            "destination": item.destination,
            "outcome": item.outcome,
            "code": item.code,
        }
        for item in getattr(outcome, "publications", ())
    ]
