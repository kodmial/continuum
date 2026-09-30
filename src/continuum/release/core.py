"""The release core: the chain, walked, with nothing platform-specific in it.

Every release Continuum performs is this loop — eleven stages, in a fixed order,
each one recording what it did under a key that says what it acted on. The loop
lives here; the platform does not. There is no keychain in this module, no
bundle identifier, no build system, no registry client, and no branch anywhere
on an adapter's name: a target is an id and an adapter, the adapter is asked for
a manifest, and whatever it produced is checked, published, and recorded the
same way whether it is an Apple bundle, an Android package, a JVM jar, or a
fixture written to prove the contract works.

Three properties are enforced here rather than trusted to a component:

**A release is pinned to one commit.** The `source` stage binds the version to
the event's SHA and records the binding. If a later run proposes the same
version from a different commit, the run stops. The build and the publication
re-check it: `assert_publishable` on every manifest, again at the destination.

**A transition that already happened does not happen again.** Each stage's key
is derived from the release, so a duplicate event, a re-run of a failed job, and
a second workflow watching the same tag all resolve to keys that are already
recorded. The stage is skipped and says it was a duplicate. This is checked
before the stage runs, not after it has built something, and a dry run never
counts as having done it — otherwise planning a release would prevent performing
it.

**A failure is either resumable or fatal, and never silently ignored.** A
publisher that fails says whether re-running could help; the core stops on the
first failure, skips the stages after it, and records the point a resumed run
would start from. A run that would have to publish artifacts it cannot see
refuses rather than assuming.

And the one that is a matter of configuration rather than logic: a release with
no targets configured is not a release that failed to find its targets, it is a
release that is switched off. It is a green no-op that touches no component, so
that a repository which has not opted in cannot be made to publish by an event
that happens to arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import contract
from .contract import (
    ArtifactManifest,
    BuildRequest,
    ContractError,
    Eligibility,
    PublishRequest,
    PublisherResult,
    ReleaseEvent,
    ReleaseNotes,
    ReleasePrRequest,
    ReleasePrResult,
    TargetSpec,
    VerificationReport,
    require_conform,
)
from .state import (
    BLOCKED,
    COMPLETED,
    EMPTY_JOURNAL,
    FAILED,
    Journal,
    NOOP,
    SKIPPED,
    ReleaseStateError,
    StageOutcome,
    assert_transition,
    channel_key,
    event_key,
    release_key,
    scopes_are_isolated,
    stage,
    stage_key,
    stage_names,
    unit_key,
)
from .version import (
    VersionAgreement,
    VersionContext,
    VersionPolicy,
    VersionProposal,
    VersionStrategy,
    agree,
    require_agreement,
    tag_for,
)

CORE_NAME = "release-core"

# Outcome statuses. `no-op` is green and is the release's way of saying there was
# nothing to do and that was correct — a disabled release, an ineligible event, a
# duplicate. It is deliberately not `failed`, and not `released`.
PLANNED = "planned"
RELEASED = "released"
NO_OP = "no-op"
GREEN_STATUSES: Tuple[str, ...] = (PLANNED, RELEASED, NO_OP)

#: The stages that write somewhere outside the job. A release that reaches them
#: and writes to none of them has not published, and has to say why.
DESTINATION_STAGES: Tuple[str, ...] = ("draft", "publish", "sync")

DISABLED_CODE = "release-disabled"
DUPLICATE_EVENT_CODE = "duplicate-event"
SOURCE_CONFLICT_CODE = "source-conflict"
UNRESUMABLE_CODE = "unresumable-manifest"


def destination_of(component: Any) -> str:
    """The name a destination is recorded under in a publication result.

    A publisher that knows the name its destination answers to says so; one that
    does not is recorded under its component name. Both are stable, which is what
    a journal needs — a destination whose recorded name changes between runs
    cannot be recognised as one that has already run.
    """

    value = getattr(component, "destination", None)
    return value if isinstance(value, str) and value else component.name


def _destination_outcome(
    stage_name: str,
    key: str,
    destination: str,
    result: PublisherResult,
    run: _Run,
) -> StageOutcome:
    """One destination's result, in the vocabulary the rest of the chain speaks.

    A publisher says `skipped` for two quite different things — "this was a
    duplicate" and "a plan writes nothing" — and a chain that recorded both as
    one outcome would claim a plan had published. So a skipped publication is a
    no-op: nothing was written, and that was correct.
    """

    if result.failed:
        return StageOutcome.failed(
            stage_name,
            key,
            result.reason,
            code=result.code or "publish-failed",
            retryable=result.retryable,
            destination=destination,
            **run.note(),
        )
    if result.skipped:
        return StageOutcome.noop(
            stage_name,
            key,
            result.reason,
            code=result.code or "nothing-to-do",
            destination=destination,
            **run.note(),
        )
    return StageOutcome.completed(
        stage_name,
        key,
        result.reason or f"{destination} published it",
        code=result.code,
        destination=destination,
        **run.note(),
    )


def _require_name(component: Any, description: str) -> None:
    """Refuse a component that cannot say which one it is.

    A manifest records the adapter that produced an artifact, a journal entry
    records the destination that was written to, and an idempotent run matches
    on those names. A component that does not name itself cannot appear in any
    of those, and a release that cannot say what published it cannot tell a
    duplicate from a new release.
    """

    if not str(getattr(component, "name", "")):
        raise ContractError(
            f"{description} does not name itself. Every component has to name itself, "
            "because a manifest, a journal entry, and a publication result all have to "
            "say which one produced them."
        )


@dataclass(frozen=True)
class ReleaseComponents:
    """Everything the core is allowed to know, injected.

    The core holds no adapter, opens no client, and reads no file other than
    through a component. That is what makes a dry-run plan possible for a release
    nobody has run, and what makes a new platform an addition here rather than a
    fork of the loop.
    """

    eligibility: Any
    version: VersionStrategy
    notes: Any
    adapters: Mapping[str, Any] = field(default_factory=dict)
    release_pr: Optional[Any] = None
    publishers: Tuple[Any, ...] = ()
    syncs: Tuple[Any, ...] = ()

    def adapter_for(self, name: str) -> Any:
        found = self.adapters.get(name)
        if found is None:
            available = ", ".join(sorted(self.adapters)) or "none registered"
            raise ContractError(
                f"no release adapter is registered as {name!r}; registered adapters: "
                f"{available}. A target naming an adapter that does not exist is a "
                "configuration error, not a reason to skip the target: a release that "
                "silently builds fewer targets than it declares is a release that "
                "publishes a partial set and calls it a release."
            )
        return found

    def targets(self) -> Tuple[str, ...]:
        return tuple(sorted(self.adapters))

    def publisher_names(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self.publishers)

    def sync_names(self) -> Tuple[str, ...]:
        return tuple(item.name for item in self.syncs)

    def conform(self) -> "ReleaseComponents":
        """Refuse a component set that cannot honour the contract, up front.

        Checked at construction rather than at each stage, so a mis-wired release
        fails before the first side effect instead of halfway through one.
        """

        require_conform(self.eligibility, "eligibility")
        require_conform(self.version, "version")
        require_conform(self.notes, "notes")
        if self.release_pr is not None:
            require_conform(self.release_pr, "release-pr")
        for name, adapter in sorted(self.adapters.items()):
            require_conform(adapter, "target-adapter")
            _require_name(adapter, f"the release adapter registered as {name!r}")
        for publisher in self.publishers:
            require_conform(publisher, "publisher")
            _require_name(publisher, f"the publisher {destination_of(publisher)!r}")
        for sync in self.syncs:
            require_conform(sync, "downstream-sync")
            _require_name(sync, f"the downstream sync {destination_of(sync)!r}")
        if not scopes_are_isolated():
            raise ContractError(
                "release permissions and pull-request automation permissions overlap; a "
                "release job that can approve or merge a pull request can publish "
                "something a human never reviewed"
            )
        return self

    def describe(self) -> Dict[str, Any]:
        return {
            "eligibility": getattr(self.eligibility, "name", ""),
            "version": self.version.name,
            "notes": getattr(self.notes, "name", ""),
            "adapters": {
                name: getattr(adapter, "name", name)
                for name, adapter in sorted(self.adapters.items())
            },
            "release_pr": getattr(self.release_pr, "name", "") if self.release_pr else "",
            "publishers": [getattr(item, "name", "") for item in self.publishers],
            "syncs": [getattr(item, "name", "") for item in self.syncs],
        }


@dataclass(frozen=True)
class ReleaseRequest:
    """One attempt at one release.

    `version_checks` are the independent sources a proposal is cross-checked
    against. A repository that takes its version from a project file and can also
    be dispatched by hand passes both strategies here, and a manual version that
    disagrees with the file stops the release instead of overriding it.
    """

    event: ReleaseEvent
    targets: Tuple[TargetSpec, ...] = ()
    enabled: bool = True
    version_policy: VersionPolicy = field(default_factory=VersionPolicy)
    version_checks: Tuple[VersionStrategy, ...] = ()
    requested_version: str = ""
    release_pr_number: int = 0
    workdir: str = ""
    dry_run: bool = False
    channel: str = "default"

    def __post_init__(self) -> None:
        if not self.targets and self.enabled:
            raise ContractError(
                "a release with no targets is not a release; a repository that has not "
                "configured any target has release switched off, which is "
                "ReleaseRequest(enabled=False)"
            )
        seen: List[str] = []
        for target in self.targets:
            if target.id in seen:
                raise ContractError(
                    f"target {target.id!r} is configured twice; two targets with one id "
                    "would produce two artifacts with one name"
                )
            seen.append(target.id)

    def as_dry_run(self) -> "ReleaseRequest":
        if self.dry_run:
            return self
        return ReleaseRequest(
            event=self.event,
            targets=self.targets,
            enabled=self.enabled,
            version_policy=self.version_policy,
            version_checks=self.version_checks,
            requested_version=self.requested_version,
            release_pr_number=self.release_pr_number,
            workdir=self.workdir,
            dry_run=True,
            channel=self.channel,
        )

    @classmethod
    def from_config(
        cls,
        config: Any,
        event: ReleaseEvent,
        *,
        version_policy: Optional[VersionPolicy] = None,
        **options: Any,
    ) -> "ReleaseRequest":
        """Build a request from a validated `.continuum.yml`.

        `release` with no targets is a repository that has not opted in, so
        `enabled` comes straight from the configuration's own answer. That is the
        only place the "release can be completely disabled" property is decided;
        the core above it never has to be told to do nothing.
        """

        targets = tuple(
            TargetSpec(id=target.id, adapter=target.adapter)
            for target in config.release.targets
        )
        return cls(
            event=event,
            targets=targets,
            enabled=config.release.enabled,
            version_policy=version_policy or VersionPolicy(),
            **options,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "event": self.event.describe(),
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "channel": self.channel,
            "version_policy": self.version_policy.describe(),
            "targets": [target.describe() for target in self.targets],
        }


@dataclass(frozen=True)
class ReleaseOutcome:
    """What a release attempt did, in a form a job summary can print.

    `resumable` is the answer to "may this be run again": true when a stage
    failed in a way re-running can fix, false when it failed in a way only a
    human can. A release that cannot be resumed still has to be *reported*, so
    `ok` and `resumable` are separate questions.
    """

    status: str
    event: ReleaseEvent
    outcomes: Tuple[StageOutcome, ...] = ()
    journal: Journal = EMPTY_JOURNAL
    manifests: Tuple[ArtifactManifest, ...] = ()
    publications: Tuple[PublisherResult, ...] = ()
    version: str = ""
    tag: str = ""
    source_sha: str = ""
    release: str = ""
    channel: str = ""
    no_op_reason: str = ""
    failure: Optional[StageOutcome] = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status in GREEN_STATUSES

    @property
    def released(self) -> bool:
        return self.status == RELEASED

    @property
    def planned(self) -> bool:
        return self.status == PLANNED

    @property
    def no_op(self) -> bool:
        return self.status == NO_OP

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    @property
    def published(self) -> bool:
        """Whether a destination is holding this release right now.

        Separate from `ok`, because "the job is green" and "the release exists"
        are different claims: a plan is green, a release with no destination
        configured is green, and neither has published anything.
        """

        return any(item.outcome == contract.PUBLISHED for item in self.publications)

    @property
    def resumable(self) -> bool:
        return self.failure is not None and self.failure.retryable

    @property
    def stage(self) -> str:
        return self.outcomes[-1].stage if self.outcomes else ""

    def outcome_for(self, stage_name: str) -> Optional[StageOutcome]:
        for outcome in self.outcomes:
            if outcome.stage == stage_name:
                return outcome
        return None

    def scopes(self) -> Tuple[str, ...]:
        return tuple(sorted({stage(entry.stage).scope for entry in self.outcomes}))

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema": OUTCOME_SCHEMA,
            "status": self.status,
            "ok": self.ok,
            "resumable": self.resumable,
            "dry_run": self.dry_run,
            "event": self.event.describe(),
            "release": self.release,
            "channel": self.channel,
            "stages": [
                outcome.describe(sequence=index)
                for index, outcome in enumerate(self.outcomes, start=1)
            ],
            "manifests": [manifest.describe() for manifest in self.manifests],
            "publications": [item.describe() for item in self.publications],
            "permissions": {
                "scopes": [scope for scope in self.scopes()],
                "isolated_from_pull_requests": scopes_are_isolated(),
            },
        }
        for key, value in (
            ("version", self.version),
            ("tag", self.tag),
            ("source_sha", self.source_sha),
            ("no_op_reason", self.no_op_reason),
        ):
            if value:
                payload[key] = value
        if self.failure is not None:
            payload["failure"] = self.failure.describe()
        return payload


OUTCOME_SCHEMA = "continuum.release-outcome/v1"


class _Run:
    """The state one pass of the chain carries between stages.

    Mutable on purpose and private on purpose: this is the loop's working memory,
    and nothing outside this module may read it. Every field that matters to a
    caller ends up in a `StageOutcome` or a `ReleaseOutcome`, which are values.
    """

    def __init__(
        self,
        request: ReleaseRequest,
        components: ReleaseComponents,
        journal: Journal,
        manifests: Tuple[ArtifactManifest, ...] = (),
    ) -> None:
        self.request = request
        self.components = components
        self.incoming = journal
        self.outcomes: List[StageOutcome] = []
        self.units: List[StageOutcome] = []
        self.publications: List[PublisherResult] = []
        self.notes: Optional[ReleaseNotes] = None
        self.release_pr: Optional[ReleasePrResult] = None
        self.proposal: Optional[VersionProposal] = None
        self.agreement: Optional[VersionAgreement] = None
        self.release = ""
        self.channel = channel_key(request.event.repository, request.channel)
        self.event_key = event_key(
            request.event.repository, request.event.name, request.event.sha
        )
        self.version = ""
        self.tag = ""
        self.source_sha = ""
        self.manifests: List[ArtifactManifest] = []
        for manifest in manifests:
            self.adopt(manifest)

    def adopt(self, manifest: ArtifactManifest) -> None:
        """Take a manifest the caller already holds, for a run being resumed.

        A journal records that a target was built, not what it built. Resuming
        without the artifacts in hand would mean publishing from a description of
        a release, so a resumed run is given its manifests back explicitly and
        refuses if they do not cover every target.
        """

        for existing in self.manifests:
            if existing.target == manifest.target:
                if existing != manifest:
                    raise ContractError(
                        f"two different manifests were given for target "
                        f"{manifest.target!r}: {existing.digest()} and {manifest.digest()}"
                    )
                return
        self.manifests.append(manifest)

    def record(self, outcome: StageOutcome) -> StageOutcome:
        self.outcomes.append(outcome)
        return outcome

    def unit(self, outcome: StageOutcome) -> StageOutcome:
        """Record the work one target or one destination did.

        Kept apart from the stage outcomes because the two answer different
        questions: a stage outcome is the transition the chain made, and a unit
        outcome is the work under it. Both go into the journal, because the keys
        a later run recognises are the unit keys — "this destination already has
        this release" is a fact about one destination, and recording only the
        stage would make it all-or-nothing.
        """

        self.units.append(outcome)
        return outcome

    @property
    def dry_run(self) -> bool:
        return self.request.dry_run

    def key_for(self, stage_name: str) -> str:
        return stage_key(self.release, stage_name) if self.release else self.event_key

    def already_done(self, stage_name: str) -> Optional[StageOutcome]:
        """The recorded outcome of a stage this release has already run.

        A stage whose key is already recorded complete is not run again. That is
        the whole of idempotency: the check is against the release's own record
        rather than against a destination, so it happens before anything is
        built, uploaded, or published.

        A stage that was a no-op or was blocked is not "already done" — nothing
        was done — so it is asked again, and asked again for free, because it
        changed nothing the first time.
        """

        recorded = self.recorded_for(self.key_for(stage_name))
        if recorded is None:
            return None
        if recorded.outcome in (NOOP, BLOCKED):
            return None
        return recorded

    def recorded_for(self, key: str) -> Optional[StageOutcome]:
        """The recorded outcome for an exact key, or None if there is nothing to
        trust.

        Read through the journal's trusted view, so a plan under this key is
        ignored and a plan *underneath* a real record does not mask it.
        """

        return self.incoming.trusted.get(key)

    def note(self, **details: str) -> Dict[str, str]:
        if not self.dry_run:
            return details
        merged = dict(details)
        merged["dry-run"] = "true"
        return merged

    def bind_version(self, version: str) -> None:
        self.version = version
        self.tag = tag_for(version, self.request.version_policy.tag_prefix)
        self.release = release_key(self.request.event.repository, version)


class ReleaseCore:
    """The chain, walked in order, one stage at a time.

    `plan` and `execute` are the same walk; the difference is that a plan passes
    `dry_run` down to every component and records nothing it can later rely on.
    That is deliberate — a plan and a run are the same code, so a plan cannot
    describe a release the run would not perform.
    """

    SUPPORTS_DRY_RUN = True
    name = CORE_NAME

    def __init__(self, components: ReleaseComponents) -> None:
        self._components = components.conform()

    @property
    def components(self) -> ReleaseComponents:
        return self._components

    def intent(self) -> str:
        names = ", ".join(self._components.publisher_names())
        return (
            "walk the release chain once per event, pinning the release to one commit "
            "and refusing any transition that has already been recorded"
            + (f", publishing to {names}" if names else "")
        )

    def plan(self, request: ReleaseRequest) -> ReleaseOutcome:
        return self.execute(request.as_dry_run())

    def execute(
        self,
        request: ReleaseRequest,
        *,
        journal: Journal = EMPTY_JOURNAL,
        manifests: Tuple[ArtifactManifest, ...] = (),
    ) -> ReleaseOutcome:
        """Walk the chain once.

        `journal` is what previous runs recorded; `manifests` are the artifacts a
        previous run built, needed when a run resumes past a stage it may not
        repeat. A run that needs a manifest it was not given fails rather than
        publishing from a description.
        """

        run = _Run(request, self._components, journal, manifests)
        handlers: Dict[str, Callable[[_Run], StageOutcome]] = {
            "eligibility": self._eligibility,
            "version": self._version,
            "release-pr": self._release_pr,
            "validation": self._validation,
            "source": self._source,
            "build": self._build,
            "sign": self._sign,
            "verify": self._verify,
            "draft": self._draft,
            "publish": self._publish,
            "sync": self._sync,
        }
        previous: Optional[str] = None
        for name in stage_names():
            if previous is not None:
                assert_transition(previous, name)
            outcome = run.record(self._stage(name, run, handlers[name]))
            if outcome.outcome in (FAILED, BLOCKED):
                return self._finish(run, status=FAILED, failure=outcome)
            if outcome.outcome == NOOP and stage(name).terminal:
                if any(item.outcome == contract.PUBLISHED for item in run.publications):
                    # Something already holds this release, so the walk stops
                    # here and the honest answer is "released", not "nothing
                    # happened". Reporting NO_OP for a release whose assets are
                    # public is the one summary a caller cannot act on: it says
                    # the same thing about a successful release and about a
                    # duplicate one, and a workflow branching on it either
                    # re-publishes or reports a failure for a release that is
                    # fine. This is reachable whenever a terminal optional
                    # stage — the downstream sync — has nothing configured to
                    # do, which is the common case for a single-destination
                    # release.
                    return self._finish(run, status=RELEASED)
                return self._finish(run, status=NO_OP, no_op_reason=outcome.summary)
            previous = name
        if run.dry_run:
            return self._finish(run, status=PLANNED)
        if any(item.outcome == contract.PUBLISHED for item in run.publications):
            return self._finish(run, status=RELEASED)
        destinations = [
            item
            for item in run.outcomes
            if item.stage in DESTINATION_STAGES and item.outcome == SKIPPED
        ]
        return self._finish(
            run,
            status=NO_OP,
            no_op_reason="every destination already held this release",
        )

    def _stage(
        self, name: str, run: "_Run", handler: Callable[["_Run"], StageOutcome]
    ) -> StageOutcome:
        """Ask one stage to run, and make its failure a fact rather than a crash.

        Every component in this chain is code somebody else wrote, and the
        ordinary reasons a release fails are all things a component says rather
        than things it can be expected to have already handled: a toolchain is
        not installed, a version policy refuses the number, a destination is
        unreachable, a manifest names two different sources. Letting any of those
        escape would lose the two things that make a failed run usable — the
        classification, which is what tells a retry from a human, and the
        journal of everything that had already been done.

        The `code` and `retryable` a component carried are kept, so "the
        destination is down, and that is worth retrying" survives the trip. A
        component that failed in a way that says neither is recorded as fatal:
        the absence of a claim that something is retryable is not a claim that it
        is.
        """

        try:
            return handler(run)
        except Exception as caught:  # noqa: BLE001 - any component, any error
            return self._failed(name, run.key_for(name), caught)

    @staticmethod
    def _failed(name: str, key: str, caught: Exception, **details: str) -> StageOutcome:
        """Record a component's exception as the outcome it should have been.

        Shared by the stage guard and by the per-target and per-destination loops,
        so a component that fails is classified the same way whether it was one
        target among three or the only thing running. The `code` and `retryable`
        it carried are kept, so "the destination is down, and that is worth
        retrying" survives the trip; a component that said neither is recorded as
        fatal, because the absence of a claim that something is retryable is not
        a claim that it is.
        """

        return StageOutcome.failed(
            name,
            key,
            f"{name} could not run. {type(caught).__name__}: {caught}",
            code=getattr(caught, "code", "") or f"{name}-failed",
            retryable=bool(getattr(caught, "retryable", False)),
            error=type(caught).__name__,
            **details,
        )

    def _finish(
        self,
        run: _Run,
        *,
        status: str,
        failure: Optional[StageOutcome] = None,
        no_op_reason: str = "",
    ) -> ReleaseOutcome:
        recorded = run.incoming.extend(tuple(run.outcomes) + tuple(run.units))
        return ReleaseOutcome(
            status=status,
            event=run.request.event,
            outcomes=tuple(run.outcomes),
            journal=recorded,
            manifests=tuple(run.manifests),
            publications=tuple(run.publications),
            version=run.version,
            tag=run.tag,
            source_sha=run.source_sha,
            release=run.release,
            channel=run.channel,
            no_op_reason=no_op_reason,
            failure=failure,
            dry_run=run.dry_run,
        )

    # -- stages -------------------------------------------------------------

    def _eligibility(self, run: _Run) -> StageOutcome:
        key = run.event_key
        if not run.request.enabled:
            return StageOutcome.noop(
                "eligibility",
                key,
                "release is switched off for this repository; no component was asked "
                "to do anything",
                code=DISABLED_CODE,
                **run.note(),
            )
        recorded = run.recorded_for(key)
        verdict: Eligibility = self._components.eligibility.evaluate(run.request.event)
        if verdict.eligible:
            return StageOutcome.completed(
                "eligibility",
                key,
                f"this event may become a release ({verdict.reason})"
                + ("; this event was already assessed" if recorded else ""),
                code=verdict.code or "eligible",
                **run.note(),
            )
        if verdict.blocking:
            return StageOutcome.blocked(
                "eligibility",
                key,
                verdict.reason,
                code=verdict.code or "event-not-releasable",
                **run.note(),
            )
        return StageOutcome.noop(
            "eligibility",
            key,
            verdict.reason,
            code=verdict.code or "event-not-releasable",
            **run.note(),
        )

    def _version(self, run: _Run) -> StageOutcome:
        key = run.key_for("version")
        recorded = run.already_done("version")
        if recorded is not None and recorded.detail("version"):
            # One event, one version: if this event has already been given a
            # version, a later run reads it back rather than proposing another,
            # even if a project file has moved on since.
            run.bind_version(recorded.detail("version"))
            return StageOutcome.skipped(
                "version",
                key,
                f"this event is already version {run.version}",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                version=run.version,
                tag=run.tag,
                release=run.release,
                **run.note(),
            )
        strategy = self._components.version
        text, source_path = strategy.collect(run.request.event, run.request.workdir)
        context = VersionContext(
            event=run.request.event,
            text=text,
            source_path=source_path,
            requested=run.request.requested_version,
            release_pr=run.request.release_pr_number,
        )
        proposals: List[VersionProposal] = [strategy.propose(context, run.request.version_policy)]
        for check in run.request.version_checks:
            check_text, check_path = check.collect(run.request.event, run.request.workdir)
            proposals.append(
                check.propose(
                    VersionContext(
                        event=run.request.event,
                        text=check_text,
                        source_path=check_path,
                        requested=run.request.requested_version,
                        release_pr=run.request.release_pr_number,
                    ),
                    run.request.version_policy,
                )
            )
        agreement = agree(*proposals)
        run.agreement = agreement
        version = require_agreement(agreement)
        run.bind_version(version)
        return StageOutcome.completed(
            "version",
            key,
            f"{version} agreed by {', '.join(agreement.sources)}",
            version=version,
            tag=run.tag,
            release=run.release,
            **run.note(),
        )

    def _release_pr(self, run: _Run) -> StageOutcome:
        key = run.key_for("release-pr")
        recorded = run.already_done("release-pr")
        if recorded is not None:
            return StageOutcome.skipped(
                "release-pr",
                key,
                f"already recorded: {recorded.summary}",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                **run.note(),
            )
        if self._components.release_pr is None:
            return StageOutcome.noop(
                "release-pr",
                key,
                "no release pull request driver is configured; this release takes its "
                f"version from {self._components.version.name}",
                code="no-release-pr-driver",
                **run.note(),
            )
        request = ReleasePrRequest(
            event=run.request.event,
            version=run.version,
            tag=run.tag,
            source_sha=run.request.event.sha,
            key=key,
            dry_run=run.dry_run,
        )
        result: ReleasePrResult = self._components.release_pr.ensure(request)
        run.release_pr = result
        if result.exists and result.version and result.version != run.version:
            return StageOutcome.failed(
                "release-pr",
                key,
                f"the release pull request proposes {result.version} but this release "
                f"is {run.version}",
                code="release-pr-version-conflict",
                **run.note(),
            )
        return StageOutcome.completed(
            "release-pr",
            key,
            f"release pull request is {result.state}"
            + (f" (#{result.number})" if result.number else ""),
            state=result.state,
            **run.note(),
        )

    def _validation(self, run: _Run) -> StageOutcome:
        key = run.key_for("validation")
        recorded = run.already_done("validation")
        if recorded is not None:
            return StageOutcome.skipped(
                "validation",
                key,
                f"already validated: {recorded.summary}",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                **run.note(),
            )
        unavailable: List[str] = []
        for target in run.request.targets:
            try:
                adapter = self._components.adapter_for(target.adapter)
            except ContractError as exc:
                return StageOutcome.failed(
                    "validation",
                    key,
                    str(exc),
                    code="adapter-unavailable",
                    target=target.id,
                    **run.note(),
                )
            probe = getattr(adapter, "available", None)
            if callable(probe) and not probe():
                unavailable.append(target.id)
        if unavailable:
            return StageOutcome.failed(
                "validation",
                key,
                f"target(s) {', '.join(unavailable)} have no toolchain on this runner. "
                "Building anyway would produce an artifact from whatever this machine "
                "happens to have.",
                code="toolchain-unavailable",
                retryable=True,
                **run.note(),
            )
        return StageOutcome.completed(
            "validation",
            key,
            f"{len(run.request.targets)} target(s) have a registered adapter: "
            + ", ".join(target.id for target in run.request.targets),
            **run.note(),
        )

    def _source(self, run: _Run) -> StageOutcome:
        key = run.key_for("source")
        recorded = run.already_done("source")
        source_sha = run.request.event.sha
        # The contradiction is checked before the duplicate, and it has to be:
        # a stage that is already recorded is otherwise a reason to do nothing,
        # and "do nothing" would be the answer to an event that names the same
        # version from a different commit. A recorded binding resumes a run; it
        # never excuses a contradiction.
        bound = run.incoming.bound_source(run.version)
        if bound is not None and bound != source_sha:
            return StageOutcome.failed(
                "source",
                key,
                f"{run.version} is already bound to {bound}, but this event is "
                f"{source_sha}. A release is one version of one commit; publishing from "
                "both would make the tag a lie.",
                code=SOURCE_CONFLICT_CODE,
                version=run.version,
                source_sha=source_sha,
                **run.note(),
            )
        if recorded is not None and recorded.detail("source_sha"):
            # The binding stands: re-running a release resumes the commit it was
            # pinned to rather than re-reading a tag that has since moved.
            run.source_sha = recorded.detail("source_sha")
            return StageOutcome.skipped(
                "source",
                key,
                f"already pinned to {run.source_sha}",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                version=run.version,
                source_sha=run.source_sha,
                **run.note(),
            )
        run.source_sha = source_sha
        return StageOutcome.completed(
            "source",
            key,
            f"{run.version} is pinned to {source_sha}",
            version=run.version,
            source_sha=source_sha,
            **run.note(),
        )

    def _build(self, run: _Run) -> StageOutcome:
        outcome = self._per_target(run, "build", self._build_target)
        if outcome.outcome != COMPLETED:
            return outcome
        collision = self._collision(run)
        if collision is not None:
            return StageOutcome.failed(
                "build",
                run.key_for("build"),
                collision,
                code="asset-name-collision",
                **run.note(),
            )
        return outcome

    def _collision(self, run: _Run) -> Optional[str]:
        """Why this release's assets cannot be told apart, or None if they can.

        Asked of the whole release rather than per destination, because the
        answer does not depend on who is publishing: a release whose two targets
        both produced `app.tar.gz` is ambiguous before any upload is attempted,
        and finding out during an upload would mean the ambiguity had already
        cost a draft and some bandwidth.
        """

        try:
            contract.assert_distinct_artifact_names(
                artifact for manifest in run.manifests for artifact in manifest.artifacts
            )
        except contract.ContractError as exc:
            return str(exc)
        return None

    def _build_target(self, run: _Run, target: TargetSpec) -> StageOutcome:
        key = unit_key(run.release, "build", target.id)
        request = BuildRequest(
            target=target,
            version=run.version,
            source_sha=run.source_sha,
            key=key,
            workdir=run.request.workdir,
            dry_run=run.dry_run,
            stage="build",
        )
        manifest = self._components.adapter_for(target.adapter).build(request)
        if not isinstance(manifest, ArtifactManifest):
            return StageOutcome.failed(
                "build",
                key,
                f"target {target.id!r} returned {type(manifest).__name__} rather than an "
                "artifact manifest",
                code="manifest-not-returned",
                target=target.id,
                **run.note(),
            )
        if manifest.target != target.id:
            return StageOutcome.failed(
                "build",
                key,
                f"target {target.id!r} returned a manifest for {manifest.target!r}",
                code="manifest-target-mismatch",
                target=target.id,
                **run.note(),
            )
        if manifest.source_sha != run.source_sha:
            return StageOutcome.failed(
                "build",
                key,
                f"target {target.id!r} built from {manifest.source_sha}, but this "
                f"release is pinned to {run.source_sha}",
                code=SOURCE_CONFLICT_CODE,
                target=target.id,
                **run.note(),
            )
        self._remember(run, manifest)
        return StageOutcome.completed(
            "build",
            key,
            f"target {target.id!r} built {len(manifest.artifacts)} artifact(s)",
            target=target.id,
            manifest=manifest.digest(),
            **run.note(),
        )

    def _sign(self, run: _Run) -> StageOutcome:
        return self._per_target(run, "sign", self._sign_target)

    def _sign_target(self, run: _Run, target: TargetSpec) -> StageOutcome:
        key = unit_key(run.release, "sign", target.id)
        manifest = self._require_manifest(run, target, "sign")
        signed = self._components.adapter_for(target.adapter).sign(
            BuildRequest(
                target=target,
                version=run.version,
                source_sha=run.source_sha,
                key=key,
                workdir=run.request.workdir,
                dry_run=run.dry_run,
                stage="sign",
            ),
            manifest,
        )
        if not isinstance(signed, ArtifactManifest):
            return StageOutcome.failed(
                "sign",
                key,
                f"target {target.id!r} returned {type(signed).__name__} rather than a "
                "signed artifact manifest",
                code="manifest-not-returned",
                target=target.id,
                **run.note(),
            )
        self._remember(run, signed)
        count = sum(1 for item in signed.artifacts if item.signed)
        return StageOutcome.completed(
            "sign",
            key,
            f"target {target.id!r} signed {count} of {len(signed.artifacts)} artifact(s)",
            target=target.id,
            manifest=signed.digest(),
            **run.note(),
        )

    def _verify(self, run: _Run) -> StageOutcome:
        return self._per_target(run, "verify", self._verify_target)

    def _verify_target(self, run: _Run, target: TargetSpec) -> StageOutcome:
        key = unit_key(run.release, "verify", target.id)
        manifest = self._require_manifest(run, target, "verify")
        report: VerificationReport = self._components.adapter_for(target.adapter).verify(
            BuildRequest(
                target=target,
                version=run.version,
                source_sha=run.source_sha,
                key=key,
                workdir=run.request.workdir,
                dry_run=run.dry_run,
                stage="verify",
            ),
            manifest,
        )
        if not isinstance(report, VerificationReport) or not report.verified:
            detail = report.describe() if isinstance(report, VerificationReport) else str(report)
            return StageOutcome.failed(
                "verify",
                key,
                f"target {target.id!r} did not verify: {detail}",
                code="verification-failed",
                target=target.id,
                **run.note(),
            )
        # The answer is recorded on the artifacts themselves, so the upload, the
        # checksum listing, and the attestation all read it from one place
        # instead of the publication path having to remember the report. A plan
        # does not record it: its artifacts are declared, and a declared artifact
        # claiming a verification is the one claim the contract refuses to hold.
        if not run.dry_run:
            manifest = manifest.mark_verified(report.verified_by)
        try:
            manifest.assert_publishable(run.source_sha, run.version)
        except ContractError as exc:
            # A plan's artifacts are declared rather than built, so the
            # invariants a real run insists on are the ones it is reporting that
            # are not yet established. Only a real run can fail here.
            if not run.dry_run:
                return StageOutcome.failed(
                    "verify",
                    key,
                    str(exc),
                    code="manifest-not-publishable",
                    target=target.id,
                    **run.note(),
                )
        self._remember(run, manifest)
        return StageOutcome.completed(
            "verify",
            key,
            f"target {target.id!r} verified {len(manifest.artifacts)} artifact(s)",
            target=target.id,
            manifest=manifest.digest(),
            **run.note(),
        )

    def _draft(self, run: _Run) -> StageOutcome:
        return self._per_destination(run, "draft", "draft")

    def _publish(self, run: _Run) -> StageOutcome:
        return self._per_destination(run, "publish", "publish")

    def _sync(self, run: _Run) -> StageOutcome:
        return self._per_destination(run, "sync", "publish", syncs=True)

    # -- stage helpers ------------------------------------------------------

    def _remember(self, run: _Run, manifest: ArtifactManifest) -> None:
        for index, existing in enumerate(run.manifests):
            if existing.target == manifest.target:
                run.manifests[index] = manifest
                return
        run.manifests.append(manifest)

    def _require_manifest(
        self, run: _Run, target: TargetSpec, stage_name: str
    ) -> ArtifactManifest:
        for manifest in run.manifests:
            if manifest.target == target.id:
                return manifest
        raise ReleaseStateError(
            f"no manifest for target {target.id!r} at the {stage_name} stage, and this "
            "run may not build it again because the journal already records it as "
            "built. A resumed run is given its manifests back with "
            "ReleaseCore.execute(manifests=...); without them there is nothing to sign, "
            "verify, or publish, and guessing is not an option",
            code=UNRESUMABLE_CODE,
        )

    def _per_target(
        self,
        run: _Run,
        name: str,
        handler: Callable[[_Run, TargetSpec], StageOutcome],
    ) -> StageOutcome:
        """Run one stage over every target, and record each target's own key.

        The stage's outcome is the aggregate; the journal gets one entry per
        target as well, because a stage that fails on the third of three targets
        has to be resumable on the third without redoing the first two.
        """

        key = run.key_for(name)
        before = len(run.units)
        for target in run.request.targets:
            target_key = unit_key(run.release, name, target.id)
            recorded = run.recorded_for(target_key)
            if recorded is not None and recorded.outcome in (COMPLETED, SKIPPED):
                run.unit(
                    StageOutcome.skipped(
                        name,
                        target_key,
                        f"target {target.id!r} already has {name} recorded: "
                        f"{recorded.summary}",
                        code=DUPLICATE_EVENT_CODE,
                        duplicate=True,
                        target=target.id,
                        **run.note(),
                    )
                )
                continue
            try:
                run.unit(handler(run, target))
            except Exception as caught:  # noqa: BLE001 - one target, one failure
                # Classified per target rather than per stage: a release with
                # three targets where one cannot build has to say which one, and
                # has to keep the other two targets' work rather than discard
                # everything behind the first failure.
                if isinstance(caught, ReleaseStateError):
                    run.unit(
                        StageOutcome.failed(
                            name,
                            target_key,
                            str(caught),
                            code=caught.code or "stage-not-resumable",
                            target=target.id,
                            **run.note(),
                        )
                    )
                else:
                    run.unit(
                        self._failed(name, target_key, caught, target=target.id, **run.note())
                    )
        outcomes = run.units[before:]
        failed = next((item for item in outcomes if item.outcome == FAILED), None)
        if failed is not None:
            return failed
        if outcomes and all(item.outcome == SKIPPED for item in outcomes):
            return StageOutcome.skipped(
                name,
                key,
                f"every target already had {name} recorded",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                **run.note(),
            )
        return StageOutcome.completed(
            name,
            key,
            "; ".join(
                f"{item.detail('target')}: {item.summary}" for item in outcomes
            ),
            **run.note(),
        )

    def _per_destination(
        self,
        run: _Run,
        name: str,
        method: str,
        *,
        syncs: bool = False,
    ) -> StageOutcome:
        """Run one stage over every destination, and record each one's own key.

        A stage that is skipped wholesale because it is already recorded is a
        coarse answer: a release with two publishers, one of which had already
        been written to, has to resume on the other. So the per-destination keys
        are recorded individually, and each destination's result becomes a unit
        outcome in the same vocabulary the rest of the chain uses.
        """

        key = run.key_for(name)
        destinations = self._components.syncs if syncs else self._components.publishers
        if not destinations:
            return StageOutcome.noop(
                name,
                key,
                f"no {'downstream sync' if syncs else 'publisher'} is configured, so this "
                "stage has nothing to do",
                code="no-destination",
                **run.note(),
            )
        try:
            manifests = tuple(run.manifests)
            for target in run.request.targets:
                self._require_manifest(run, target, name)
        except ReleaseStateError as exc:
            return StageOutcome.failed(
                name,
                key,
                str(exc),
                code=exc.code or UNRESUMABLE_CODE,
                **run.note(),
            )
        results: List[PublisherResult] = []
        before = len(run.units)
        for publisher in destinations:
            destination_key = unit_key(run.release, name, publisher.name)
            destination = destination_of(publisher)
            done = run.recorded_for(destination_key)
            if done is not None and done.outcome in (COMPLETED, SKIPPED):
                duplicate = contract.skipped(
                    destination,
                    "this destination already has this release recorded",
                    code=DUPLICATE_EVENT_CODE,
                )
                run.publications.append(duplicate)
                run.unit(
                    StageOutcome.skipped(
                        name,
                        destination_key,
                        f"{destination}: already recorded ({done.summary})",
                        code=DUPLICATE_EVENT_CODE,
                        duplicate=True,
                        destination=destination,
                        **run.note(),
                    )
                )
                continue
            try:
                result = getattr(publisher, method)(
                    PublishRequest(
                        destination=destination,
                        tag=run.tag,
                        version=run.version,
                        source_sha=run.source_sha,
                        manifests=manifests,
                        key=destination_key,
                        dry_run=run.dry_run,
                    )
                )
            except Exception as caught:  # noqa: BLE001 - one destination, one failure
                run.unit(
                    self._failed(
                        name, destination_key, caught, destination=destination, **run.note()
                    )
                )
                continue
            results.append(result)
            run.publications.append(result)
            run.unit(_destination_outcome(name, destination_key, destination, result, run))
        written = run.units[before:]
        failed = next((item for item in written if item.outcome == FAILED), None)
        if failed is not None:
            return failed
        if written and all(item.duplicate for item in written):
            return StageOutcome.skipped(
                name,
                key,
                f"every destination already had {name} recorded",
                code=DUPLICATE_EVENT_CODE,
                duplicate=True,
                **run.note(),
            )
        return StageOutcome.completed(
            name,
            key,
            "; ".join(f"{item.detail('destination')}: {item.summary}" for item in written),
            **run.note(),
        )


__all__ = [
    "CORE_NAME",
    "DESTINATION_STAGES",
    "DISABLED_CODE",
    "DUPLICATE_EVENT_CODE",
    "GREEN_STATUSES",
    "NO_OP",
    "OUTCOME_SCHEMA",
    "PLANNED",
    "RELEASED",
    "ReleaseComponents",
    "ReleaseCore",
    "ReleaseOutcome",
    "ReleaseRequest",
    "SOURCE_CONFLICT_CODE",
    "UNRESUMABLE_CODE",
    "destination_of",
]
