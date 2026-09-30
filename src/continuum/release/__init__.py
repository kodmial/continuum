"""Release adapters and the generic release core.

Two things live here, and they are deliberately separate.

**The lifecycle core.** `core.ReleaseCore` walks one event through one fixed
chain of stages and records every transition in a `Journal`. It never learns
what a keychain, a bundle identifier, or a release asset is: everything
platform-specific arrives as a component that satisfies a port in
`contract.py`, so a release can be planned and run for a platform that has not
been written yet.

**The plan adapters.** The original surface — `plan.py` and `run.py` — turns a
validated target from `.continuum.yml` into a `ReleasePlan`: an ordered,
inspectable list of steps plus the teardown that has to happen whatever the
outcome. Adapters own the platform; the plan owns nothing but the plan's shape
and the rules for running it.

The split is the point. `plan.py` and `run.py` never move named steps around
and enforce the invariants that hold everywhere (teardown always runs, a value
that is required to be pinned was actually pinned, a degraded plan says so out
loud). Everything platform-specific lives in an adapter module and is expressed
as argument vectors, so nothing an adapter emits is ever handed to a shell.

The lifecycle core is the newer of the two and the one new platforms should be
written against. The plan adapters are kept working for the platforms that
already have one.
"""

from __future__ import annotations

from . import archive
from .android import AndroidAdapter, AndroidError
from .apple import AppleAdapter, AppleError
from .contract import (
    PORT_ATTRIBUTES,
    PORT_SURFACES,
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    Eligibility,
    ManifestBuilder,
    PublishRequest,
    PublisherResult,
    ReleaseEvent,
    ReleaseNotes,
    ReleasePrRequest,
    ReleasePrResult,
    TargetSpec,
    VerificationReport,
    conform,
    digest_file,
)
from .core import (
    DISABLED_CODE,
    DUPLICATE_EVENT_CODE,
    NO_OP,
    PLANNED,
    RELEASED,
    SOURCE_CONFLICT_CODE,
    UNRESUMABLE_CODE,
    ReleaseComponents,
    ReleaseCore,
    ReleaseOutcome,
    ReleaseRequest,
)
from .github import GitHubReleasePublisher
from .jvm import (
    BUILD_AUTO,
    BUILD_GRADLE,
    BUILD_MAVEN,
    JvmAdapter,
    JvmError,
    JvmSettings,
    MavenCoordinates,
    PomMetadata,
    maven_version_for,
    parse_settings,
    read_pom,
)
from .maven_publish import (
    CENTRAL_PUBLISHER,
    GITHUB_PACKAGES_PUBLISHER,
    CentralSettings,
    GitHubPackagesPublisher,
    GitHubPackagesSettings,
    MavenApiError,
    MavenCentralPublisher,
    MavenError,
    MavenLibrary,
    read_library,
    write_bundle,
)
from .plan import (
    PlanStep,
    ReleaseError,
    ReleasePlan,
    SigningUnavailable,
    TargetNotExecutable,
)
from .play import GooglePlayPublisher, PlayError, PlaySettings
from .state import (
    BLOCKED,
    COMPLETED,
    EMPTY_JOURNAL,
    FAILED,
    NOOP,
    SKIPPED,
    Journal,
    StageOutcome,
    stage,
    stage_names,
    unit_key,
)
from .version import (
    ExplicitVersion,
    ProjectFileVersion,
    ReleasePullRequestVersion,
    TagVersion,
    VersionAgreement,
    VersionError,
    VersionPolicy,
    agree,
    register_strategy,
    require_agreement,
    strategies,
)

__all__ = [
    "AndroidAdapter",
    "AndroidError",
    "AppleAdapter",
    "AppleError",
    "Artifact",
    "archive",
    "ArtifactManifest",
    "BLOCKED",
    "BUILD_AUTO",
    "BUILD_GRADLE",
    "BUILD_MAVEN",
    "BuildRequest",
    "CENTRAL_PUBLISHER",
    "COMPLETED",
    "CentralSettings",
    "ContractError",
    "DISABLED_CODE",
    "DUPLICATE_EVENT_CODE",
    "EMPTY_JOURNAL",
    "Eligibility",
    "ExplicitVersion",
    "FAILED",
    "GITHUB_PACKAGES_PUBLISHER",
    "GitHubPackagesPublisher",
    "GitHubPackagesSettings",
    "GitHubReleasePublisher",
    "GooglePlayPublisher",
    "Journal",
    "JvmAdapter",
    "JvmError",
    "JvmSettings",
    "ManifestBuilder",
    "MavenApiError",
    "MavenCentralPublisher",
    "MavenCoordinates",
    "MavenError",
    "MavenLibrary",
    "NOOP",
    "NO_OP",
    "PLANNED",
    "PORT_ATTRIBUTES",
    "PORT_SURFACES",
    "PlanStep",
    "PlayError",
    "PlaySettings",
    "PomMetadata",
    "ProjectFileVersion",
    "PublishRequest",
    "PublisherResult",
    "RELEASED",
    "ReleaseComponents",
    "ReleaseCore",
    "ReleaseError",
    "ReleaseEvent",
    "ReleaseNotes",
    "ReleaseOutcome",
    "ReleasePlan",
    "ReleasePrRequest",
    "ReleasePrResult",
    "ReleasePullRequestVersion",
    "ReleaseRequest",
    "SKIPPED",
    "SOURCE_CONFLICT_CODE",
    "SigningUnavailable",
    "StageOutcome",
    "TagVersion",
    "TargetNotExecutable",
    "TargetSpec",
    "UNRESUMABLE_CODE",
    "VerificationReport",
    "VersionAgreement",
    "VersionError",
    "VersionPolicy",
    "agree",
    "conform",
    "digest_file",
    "maven_version_for",
    "parse_settings",
    "read_library",
    "read_pom",
    "register_strategy",
    "require_agreement",
    "stage",
    "stage_names",
    "strategies",
    "unit_key",
    "write_bundle",
]
