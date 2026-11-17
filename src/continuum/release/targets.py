"""Target adapters that bridge typed `.continuum.yml` targets into the core.

A repository can declare a release target in configuration without adopting a
generic hooks document. That declaration is enough to *plan* a release — the
version, the platforms, the artifact shapes are all there — but it is not enough
to build one, because the build and signing commands are the repository's own.

Rather than pretending otherwise, this module provides two small adapters:

* `ConfigTargetAdapter` declares the artifacts a configured target would
  produce, so a plan is a real answer, and refuses to build with a stable
  `hooks-required` code. It never invents a command.
* `RecordedAdapter` is the publish-half counterpart. A publish job has the
  receipt of a build that already ran; the core skips the recorded build, sign,
  and verify stages, and this adapter makes sure that a job which somehow arrives
  without that record fails closed instead of claiming a verification nobody
  performed.

Keeping both adapters here rather than in `config.py` preserves the boundary the
release core exists to hold: configuration is vocabulary, and only an adapter
knows how to act on it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .contract import (
    TYPE_ARCHIVE,
    TYPE_INSTALLER,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    VerificationReport,
)
from .. import config as config_module

CONFIG_TARGET_ADAPTER = "config-target"
RECORDED_ADAPTER = "recorded"
_SUFFIXES = {
    config_module.ARTIFACT_TAR_GZ: ("tar.gz", TYPE_ARCHIVE),
    config_module.ARTIFACT_ZIP: ("zip", TYPE_ARCHIVE),
    config_module.ARTIFACT_PKG: ("pkg", TYPE_INSTALLER),
    config_module.ARTIFACT_DMG: ("dmg", TYPE_INSTALLER),
}


class HooksRequired(ContractError):
    """A configured target cannot be built without a generic hooks document."""


def artifact_names(target: config_module.ReleaseTarget, version: str) -> tuple:
    """The names a configured target would publish, in declaration order.

    The name carries everything a consumer needs to tell two downloads apart:
    the target, the version, and either the architecture or `universal`. That is
    the same information the hooks document's template gives, so a plan reads
    the same whether a repository adopted hooks or not.
    """

    architectures = (
        ("universal",)
        if target.universal
        else tuple(target.architectures) or ("universal",)
    )
    names = []
    for fmt in target.artifacts:
        suffix, kind = _SUFFIXES[fmt]
        for arch in architectures:
            names.append((f"{target.id}-{version}-{arch}.{suffix}", kind))
    if not names:
        names.append((f"{target.id}-{version}.tar.gz", TYPE_ARCHIVE))
    return tuple(names)


class ConfigTargetAdapter:
    """Plan a configured target; refuse to build one.

    `available()` is true because a plan is a legitimate thing to run on any
    runner. That is not a claim that this runner can build the target: the build
    is refused explicitly, with the reason and the remedy.
    """

    SUPPORTS_DRY_RUN = True
    name = CONFIG_TARGET_ADAPTER

    def __init__(self, target: config_module.ReleaseTarget) -> None:
        self._target = target

    @property
    def target(self) -> config_module.ReleaseTarget:
        return self._target

    def intent(self) -> str:
        return (
            f"plan the artifacts {self._target.id!r} declares in .continuum.yml; a "
            "real build needs a release.hooks document"
        )

    def available(self) -> bool:
        return True

    def _builder(self, request: BuildRequest) -> ManifestBuilder:
        return ManifestBuilder(
            target=request.target.id,
            adapter=CONFIG_TARGET_ADAPTER,
            source_sha=request.source_sha,
            version=request.version,
        )

    def build(self, request: BuildRequest) -> ArtifactManifest:
        if not request.dry_run:
            raise HooksRequired(
                f"target {self._target.id!r} is declared in .continuum.yml, but no "
                "release.hooks document says how to build, sign, or verify it. Add "
                "`release.hooks: path/to/hooks.json` (see "
                "docs/release-consumer-guide.md) rather than letting Continuum guess "
                "at a build command.",
                code="hooks-required",
            )
        builder = self._builder(request)
        for name, kind in artifact_names(self._target, request.version):
            builder.declare(
                name,
                name,
                kind,
                platform=self._target.platform,
                classifier=(
                    "universal" if self._target.universal else self._target.id
                ),
            )
        return builder.build()

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        if request.dry_run:
            return manifest
        raise HooksRequired(
            f"target {self._target.id!r} has no signing hook; configuration names a "
            "signing mode but not the command that applies it",
            code="hooks-required",
        )

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="config-target-planned",
                detail="a plan verifies nothing; the build has not run",
                verified_by=f"{CONFIG_TARGET_ADAPTER}:{self._target.id}",
            )
        return VerificationReport(
            verified=False,
            code="hooks-required",
            detail=(
                f"target {self._target.id!r} has no verification hook, so nothing "
                "checked the bytes a release would publish"
            ),
        )


class RecordedAdapter:
    """The publish-half adapter: trust only what the receipt already recorded.

    A publish job carries no toolchain and must not build. The core skips a stage
    whose key the receipt records as complete, so this adapter is only reached
    when the receipt does *not* cover the target — which is exactly when
    publishing would mean shipping something nobody built.
    """

    SUPPORTS_DRY_RUN = True
    name = RECORDED_ADAPTER

    def __init__(self, target_id: str) -> None:
        self._target_id = target_id

    def intent(self) -> str:
        return (
            f"refuse to build or verify {self._target_id!r} during publication; the "
            "receipt of the build is the only evidence this job has"
        )

    def available(self) -> bool:
        return True

    def build(self, request: BuildRequest) -> ArtifactManifest:
        raise ContractError(
            f"target {request.target.id!r} has no recorded build, so this publish job "
            "has nothing to publish. A publish job must resume a build that already "
            "happened; it is not allowed to produce one.",
            code="build-not-recorded",
        )

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        if request.dry_run:
            return manifest
        raise ContractError(
            f"target {request.target.id!r} has no recorded signature",
            code="sign-not-recorded",
        )

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="recorded-planned",
                detail="a publish plan verifies nothing",
                verified_by=f"{RECORDED_ADAPTER}:{self._target_id}",
            )
        return VerificationReport(
            verified=False,
            code="verification-not-recorded",
            detail=(
                f"target {request.target.id!r} has no recorded verification; a "
                "publish job cannot verify bytes it never built"
            ),
        )


def describe(adapter: Any) -> Dict[str, Any]:
    return {
        "name": getattr(adapter, "name", type(adapter).__name__),
        "target": getattr(getattr(adapter, "target", None), "id", ""),
    }


def recorded_adapters(target_ids) -> Dict[str, Any]:
    """One `recorded` adapter per target id, for a publish-only run."""

    return {RECORDED_ADAPTER: _RecordedSet(target_ids)}


class _RecordedSet:
    """Dispatch `recorded` by target id, like `CommandAdapterSet` does."""

    SUPPORTS_DRY_RUN = True
    name = RECORDED_ADAPTER

    def __init__(self, target_ids) -> None:
        self._adapters = {
            str(target_id): RecordedAdapter(str(target_id)) for target_id in target_ids
        }
        if not self._adapters:
            raise ContractError(
                "a publish-only run needs at least one target to resume", code="no-targets"
            )

    def intent(self) -> str:
        return "resume the recorded build of every target and publish only that"

    def available(self) -> bool:
        return True

    def _for(self, request: BuildRequest) -> RecordedAdapter:
        adapter = self._adapters.get(request.target.id)
        if adapter is None:
            raise ContractError(
                f"target {request.target.id!r} is not part of this recorded release",
                code="target-not-recorded",
            )
        return adapter

    def build(self, request: BuildRequest) -> ArtifactManifest:
        return self._for(request).build(request)

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        return self._for(request).sign(request, manifest)

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        return self._for(request).verify(request, manifest)


def config_adapters(
    targets, *, publish_only: bool = False
) -> Dict[str, Any]:
    """Adapters for configured targets, keyed by the adapter name they declare."""

    adapters: Dict[str, Any] = {}
    if publish_only:
        return recorded_adapters(target.id for target in targets)
    for target in targets:
        if target.adapter not in adapters:
            adapters[target.adapter] = _ConfigSet()
        adapters[target.adapter].register(ConfigTargetAdapter(target))
    return adapters


class _ConfigSet:
    SUPPORTS_DRY_RUN = True
    name = CONFIG_TARGET_ADAPTER

    def __init__(self) -> None:
        self._adapters: Dict[str, ConfigTargetAdapter] = {}

    def register(self, adapter: ConfigTargetAdapter) -> None:
        self._adapters[adapter.target.id] = adapter

    def intent(self) -> str:
        return "plan every configured target from .continuum.yml"

    def available(self) -> bool:
        return True

    def _for(self, request: BuildRequest) -> ConfigTargetAdapter:
        adapter = self._adapters.get(request.target.id)
        if adapter is None:
            raise ContractError(
                f"target {request.target.id!r} is not declared in .continuum.yml",
                code="target-not-configured",
            )
        return adapter

    def build(self, request: BuildRequest) -> ArtifactManifest:
        return self._for(request).build(request)

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        return self._for(request).sign(request, manifest)

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        return self._for(request).verify(request, manifest)


__all__ = [
    "CONFIG_TARGET_ADAPTER",
    "ConfigTargetAdapter",
    "HooksRequired",
    "RECORDED_ADAPTER",
    "RecordedAdapter",
    "artifact_names",
    "config_adapters",
    "describe",
    "recorded_adapters",
]
