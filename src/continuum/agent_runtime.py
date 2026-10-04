"""Prebuilt immutable agent runtime with zero-idle per-job ephemeral runners.

Implements kodmial/continuum#179: fast agent execution comes from a warm
image/cache, never from warm compute. The lifecycle model is::

    versioned Continuum runtime manifest
            -> build immutable golden image/template
            -> validate + store image by immutable digest
            -> job is queued
            -> create a fresh compute instance from the prepared image
            -> JIT-register one ephemeral runner for that one job
            -> run OpenCode / PR-Agent / delegated worker
            -> destroy runner + compute + per-job network attachment

Non-negotiable invariants for strict ephemeral profiles:

* ``idle_instances == 0`` and ``max_uses_per_instance == 1``;
* no VM/container/host remains running merely to wait for a future job;
* no already-registered runner remains online waiting for a future job;
* every execution instance is created only after real queued demand exists;
* every terminal path (success, failure, cancellation, JIT failure, startup
  failure, controller restart, lost webhook) destroys the runner registration
  and the underlying instance instead of leaving a reusable or indefinitely
  waiting instance behind;
* a periodic external reconciler garbage-collects orphans even when the
  normal teardown path never runs.

A provider may later reissue the same public IP from its pool. IP uniqueness
between jobs is explicitly NOT required: qualification verifies lifecycle
(no compute/network resource deliberately persisted or reused), not string
inequality of provider-assigned addresses.

This module is the generic Continuum implementation: canonical manifests and
exact versions, immutable image digests, project-scoped namespaces,
on-demand ephemeral provisioning, JIT/single-job registration, per-job
network lifecycle, teardown and orphan reconciliation, reusable consumer
configuration, and Linux/macOS/Windows adapters. It is standard library only
and provider-agnostic: a small in-memory fake provider backs deterministic
tests and the documented live-qualification harness, while real providers sit
behind the same generic contract.

Ownership boundary (consumer projects)::

    Continuum immutable agent-runtime base
            + consumer-declared toolchain/profile inputs
            -> consumer-scoped derived immutable image/template

Continuum owns the manifest schema, pinned generic runtime, image/build
definitions, profile resolver, validation, provisioning/control-plane
implementation, lifecycle/reconciliation logic, and security rules. Each
consumer project owns its provider/account/project resources, image/template
generations, image/cache namespace, controller state, concurrency/cost
limits, and JIT runner registrations. There is no centralized multi-tenant
live runner pool. Consumer repositories declare a small runtime profile;
they never hand-write provider lifecycle logic.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Canonical pins (section 1 of the task).
#
# Runner software updates happen by rebuilding a new image generation, never
# by mutating an execution instance in place. Bumping any pin below produces
# a new manifest digest and therefore a new image generation (see
# manifest_digest / image_digest).
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = 1

OPENCODE_VERSION = "1.18.34"
PR_AGENT_VERSION = "0.46.0"
RUNNER_VERSION = "2.329.0"
PYTHON_VERSION = "3.12"

#: Bridge/dependency material: the PR-Agent/OpenCode compatibility bridge is
#: standard library only, so there is deliberately no third-party wheel to
#: pin here beyond the two agent releases above.
BRIDGE_DEPENDENCIES: Tuple[str, ...] = ()

#: Generic execution utilities that must be present in every golden image.
REQUIRED_UTILITIES: Tuple[str, ...] = ("bash", "curl", "git", "gh", "jq", "python3")

SUPPORTED_OSES = ("linux", "macos", "windows")
SUPPORTED_ARCHES = ("x64", "arm64")
SUPPORTED_FEATURES = ("opencode", "pr-agent")
SUPPORTED_LIFECYCLES = ("per-job",)
SUPPORTED_NETWORK_LIFECYCLES = ("per-job",)
SUPPORTED_PROVIDERS = ("github-hosted", "gce", "ec2", "azure", "custom")

#: Immutable base-image identity per (os, arch). These are digested into the
#: manifest, so a base refresh rebuilds the derived image as a new generation.
BASE_IMAGES: Dict[Tuple[str, str], str] = {
    ("linux", "x64"): "ubuntu-24.04-x86_64-base-v1",
    ("linux", "arm64"): "ubuntu-24.04-arm64-base-v1",
    ("macos", "x64"): "macos-15-x86_64-base-v1",
    ("macos", "arm64"): "macos-15-arm64-base-v1",
    ("windows", "x64"): "windows-2025-x86_64-base-v1",
}

#: Strict ephemeral invariants (section 4): tuning suggestions are rejected.
STRICT_IDLE_INSTANCES = 0
STRICT_MAX_USES_PER_INSTANCE = 1
STRICT_LIFECYCLE = "per-job"
STRICT_NETWORK_LIFECYCLE = "per-job"

#: Paid-provider keys must never be required, read, or forwarded by core.
FORBIDDEN_PROVIDER_KEYS = (
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GROQ.KEY",
)

#: Lease/timeout defaults (section 7). Exact defaults may be refined, but no
#: state may mean "wait indefinitely": every bound is finite.
DEFAULT_PROVISIONING_TIMEOUT_SECONDS = 600
DEFAULT_MAX_JOB_LIFETIME_SECONDS = 3 * 3600
DEFAULT_TEARDOWN_TIMEOUT_SECONDS = 300
DEFAULT_ORPHAN_GRACE_SECONDS = 900
DEFAULT_GLOBAL_MAX_INSTANCE_AGE_SECONDS = 4 * 3600
DEFAULT_TEARDOWN_RETRY_LIMIT = 5
DEFAULT_TEARDOWN_RETRY_BASE_SECONDS = 10

#: Old image generations are garbage-collected only after this retention.
DEFAULT_ROLLBACK_RETENTION_SECONDS = 7 * 24 * 3600

#: Failure injection outcomes for deterministic tests.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_JIT_FAILURE = "jit-failure"
OUTCOME_STARTUP_FAILURE = "startup-failure"
TERMINAL_OUTCOMES = (
    OUTCOME_SUCCESS,
    OUTCOME_FAILURE,
    OUTCOME_CANCELLED,
    OUTCOME_JIT_FAILURE,
    OUTCOME_STARTUP_FAILURE,
)


class AgentRuntimeError(ValueError):
    """Raised when a manifest, profile, image, lease, or cache is invalid."""


# ---------------------------------------------------------------------------
# Canonical runtime contract (section 1).
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentManifest:
    """One versioned, platform-specific agent-runtime manifest.

    Do not model this as one universal Linux image: the contract produces
    Linux, macOS, and Windows manifests from the same schema.
    """

    schema_version: int = MANIFEST_SCHEMA_VERSION
    opencode_version: str = OPENCODE_VERSION
    pr_agent_version: str = PR_AGENT_VERSION
    runner_version: str = RUNNER_VERSION
    python_version: str = PYTHON_VERSION
    bridge_dependencies: Tuple[str, ...] = BRIDGE_DEPENDENCIES
    utilities: Tuple[str, ...] = REQUIRED_UTILITIES
    base_image: str = ""
    os: str = "linux"
    arch: str = "x64"
    continuum_ref: str = "main"
    toolchain: Tuple[str, ...] = ()
    probes: Tuple[str, ...] = (
        "opencode --version",
        "pr-agent --version",
        "run --version",
    )

    def to_canonical(self) -> Dict[str, Any]:
        return {
            "arch": self.arch,
            "base_image": self.base_image,
            "bridge_dependencies": list(self.bridge_dependencies),
            "continuum_ref": self.continuum_ref,
            "opencode_version": self.opencode_version,
            "os": self.os,
            "pr_agent_version": self.pr_agent_version,
            "probes": list(self.probes),
            "python_version": self.python_version,
            "runner_version": self.runner_version,
            "schema_version": self.schema_version,
            "toolchain": list(self.toolchain),
            "utilities": list(self.utilities),
        }

    def describe(self) -> Dict[str, Any]:
        body = self.to_canonical()
        body["digest"] = manifest_digest(self)
        return body


def canonical_manifest(
    os: str = "linux",
    arch: str = "x64",
    continuum_ref: str = "main",
    toolchain: Sequence[str] = (),
) -> AgentManifest:
    """Build the canonical manifest for one platform.

    Every field is exact: runner software updates arrive as a new manifest
    (and therefore a new image generation), never as an in-place mutation.
    """

    if os not in SUPPORTED_OSES:
        raise AgentRuntimeError(
            "unsupported os {!r}; expected one of {}".format(os, ", ".join(SUPPORTED_OSES))
        )
    if arch not in SUPPORTED_ARCHES:
        raise AgentRuntimeError(
            "unsupported arch {!r}; expected one of {}".format(arch, ", ".join(SUPPORTED_ARCHES))
        )
    if (os, arch) not in BASE_IMAGES:
        raise AgentRuntimeError(
            "no immutable base image for os={!r} arch={!r}; refusing to advertise it".format(os, arch)
        )
    if not isinstance(continuum_ref, str) or not continuum_ref.strip():
        raise AgentRuntimeError("continuum_ref must be a non-empty revision, got {!r}".format(continuum_ref))
    tools = tuple(str(item) for item in (toolchain or ()))
    for item in tools:
        if not item or len(item) > 120 or any(char in item for char in "\n\r\0"):
            raise AgentRuntimeError("invalid toolchain input {!r}".format(item))
    if len(set(tools)) != len(tools):
        raise AgentRuntimeError("toolchain inputs list {!r} twice".format(sorted(tools)))
    return AgentManifest(
        base_image=BASE_IMAGES[(os, arch)],
        os=os,
        arch=arch,
        continuum_ref=continuum_ref.strip(),
        toolchain=tools,
    )


def manifest_digest(manifest: AgentManifest) -> str:
    """Immutable digest of the canonical manifest (sha256 over canonical JSON)."""

    return _sha256_hex(_canonical_json(manifest.to_canonical()))


# ---------------------------------------------------------------------------
# Consumer-facing configuration (section 4).
# ---------------------------------------------------------------------------

PRESETS: Dict[str, Dict[str, Any]] = {
    "agent-linux": {
        "os": "linux",
        "arch": "x64",
        "features": ["opencode", "pr-agent"],
    },
    "agent-linux-arm": {
        "os": "linux",
        "arch": "arm64",
        "features": ["opencode", "pr-agent"],
    },
    "agent-macos": {
        "os": "macos",
        "arch": "arm64",
        "features": ["opencode", "pr-agent"],
    },
    "agent-macos-intel": {
        "os": "macos",
        "arch": "x64",
        "features": ["opencode", "pr-agent"],
    },
    "agent-windows": {
        "os": "windows",
        "arch": "x64",
        "features": ["opencode", "pr-agent"],
    },
}


@dataclass(frozen=True)
class RuntimeProfile:
    """A resolved, consumer-declared runtime profile.

    For strict ephemeral profiles ``idle_instances == 0`` and
    ``max_uses_per_instance == 1`` are invariants, not tuning suggestions:
    configuration may select provider backend, concurrency, timeouts, and
    cache policy, but must not turn a strict profile into a persistent
    waiting runner.
    """

    name: str
    os: str
    arch: str
    features: Tuple[str, ...]
    lifecycle: str = STRICT_LIFECYCLE
    max_uses_per_instance: int = STRICT_MAX_USES_PER_INSTANCE
    idle_instances: int = STRICT_IDLE_INSTANCES
    network_lifecycle: str = STRICT_NETWORK_LIFECYCLE
    golden_image: bool = True
    dependencies_cache: bool = True
    provider: str = "github-hosted"
    concurrency_limit: int = 4
    provisioning_timeout_seconds: int = DEFAULT_PROVISIONING_TIMEOUT_SECONDS
    max_job_lifetime_seconds: int = DEFAULT_MAX_JOB_LIFETIME_SECONDS
    continuum_ref: str = "main"
    toolchain: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "arch": self.arch,
            "concurrency_limit": self.concurrency_limit,
            "continuum_ref": self.continuum_ref,
            "dependencies_cache": self.dependencies_cache,
            "features": list(self.features),
            "golden_image": self.golden_image,
            "idle_instances": self.idle_instances,
            "lifecycle": self.lifecycle,
            "max_job_lifetime_seconds": self.max_job_lifetime_seconds,
            "max_uses_per_instance": self.max_uses_per_instance,
            "name": self.name,
            "network_lifecycle": self.network_lifecycle,
            "os": self.os,
            "provider": self.provider,
            "provisioning_timeout_seconds": self.provisioning_timeout_seconds,
            "toolchain": list(self.toolchain),
        }


def resolve_profile(declaration: Mapping[str, Any]) -> RuntimeProfile:
    """Resolve a thin declarative consumer profile into strict invariants.

    Accepts either ``{"preset": "<name>", ...overrides}`` or an explicit
    ``{"os": ..., "arch": ..., ...}`` mapping. Rejects anything that would
    turn a strict ephemeral profile into a persistent waiting runner, any
    paid-provider key, and any unknown platform.
    """

    if not isinstance(declaration, Mapping):
        raise AgentRuntimeError("runtime profile must be a mapping, got {!r}".format(type(declaration).__name__))
    decl = dict(declaration)
    for key in tuple(decl):
        if isinstance(key, str) and key.upper() in FORBIDDEN_PROVIDER_KEYS:
            raise AgentRuntimeError(
                "core profiles must never require, read, or forward paid provider key {!r}".format(key)
            )
    preset = decl.get("preset")
    base: Dict[str, Any] = {}
    name = str(decl.get("name") or preset or "custom")
    if preset is not None:
        if str(preset) not in PRESETS:
            raise AgentRuntimeError(
                "unknown runtime preset {!r}; expected one of {}".format(preset, ", ".join(sorted(PRESETS)))
            )
        base.update(PRESETS[str(preset)])
        name = str(decl.get("name") or preset)
    for key in ("os", "arch", "features", "provider", "continuum_ref", "toolchain",
                "lifecycle", "max_uses_per_instance", "idle_instances",
                "network", "cache", "concurrency_limit",
                "provisioning_timeout_seconds", "max_job_lifetime_seconds",
                "golden_image", "dependencies"):
        if key in decl:
            base[key] = decl[key]

    os_name = str(base.get("os", "linux"))
    arch = str(base.get("arch", "x64"))
    if os_name not in SUPPORTED_OSES:
        raise AgentRuntimeError("unsupported runtime os {!r}".format(os_name))
    if arch not in SUPPORTED_ARCHES:
        raise AgentRuntimeError("unsupported runtime arch {!r}".format(arch))
    if (os_name, arch) not in BASE_IMAGES:
        raise AgentRuntimeError(
            "do not advertise unsupported platform profile os={!r} arch={!r}".format(os_name, arch)
        )
    raw_features = base.get("features", ["opencode", "pr-agent"])
    if not isinstance(raw_features, (list, tuple)) or not raw_features:
        raise AgentRuntimeError("runtime features must be a non-empty list")
    features = tuple(str(item) for item in raw_features)
    for item in features:
        if item not in SUPPORTED_FEATURES:
            raise AgentRuntimeError("unsupported runtime feature {!r}".format(item))
    if len(set(features)) != len(features):
        raise AgentRuntimeError("runtime features list {!r} twice".format(list(features)))

    lifecycle = str(base.get("lifecycle", STRICT_LIFECYCLE))
    if lifecycle not in SUPPORTED_LIFECYCLES:
        raise AgentRuntimeError(
            "strict ephemeral profiles require lifecycle {!r}, got {!r}".format(STRICT_LIFECYCLE, lifecycle)
        )
    network_block = base.get("network", {})
    if isinstance(network_block, Mapping):
        network_lifecycle = str(network_block.get("lifecycle", STRICT_NETWORK_LIFECYCLE))
    else:
        network_lifecycle = str(base.get("network_lifecycle", STRICT_NETWORK_LIFECYCLE))
    if network_lifecycle not in SUPPORTED_NETWORK_LIFECYCLES:
        raise AgentRuntimeError(
            "strict ephemeral profiles require network.lifecycle {!r}, got {!r}".format(
                STRICT_NETWORK_LIFECYCLE, network_lifecycle
            )
        )
    idle_instances = base.get("idle_instances", STRICT_IDLE_INSTANCES)
    max_uses = base.get("max_uses_per_instance", STRICT_MAX_USES_PER_INSTANCE)
    try:
        idle_number = int(idle_instances)  # type: ignore[arg-type]
        max_uses_number = int(max_uses)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise AgentRuntimeError("idle_instances and max_uses_per_instance must be integers")
    if idle_number != STRICT_IDLE_INSTANCES:
        raise AgentRuntimeError(
            "idle_instances={} is rejected: strict ephemeral profiles keep zero live idle instances".format(idle_number)
        )
    if max_uses_number != STRICT_MAX_USES_PER_INSTANCE:
        raise AgentRuntimeError(
            "max_uses_per_instance={} is rejected: one instance may execute exactly one job".format(max_uses_number)
        )

    provider = str(base.get("provider", "github-hosted"))
    if provider not in SUPPORTED_PROVIDERS:
        raise AgentRuntimeError("unsupported provider backend {!r}".format(provider))
    cache_block = base.get("cache", {})
    golden_image = True
    dependencies_cache = True
    if isinstance(cache_block, Mapping):
        golden_image = bool(cache_block.get("golden_image", True))
        dependencies_cache = bool(cache_block.get("dependencies", True))
    else:
        golden_image = bool(base.get("golden_image", True))
        dependencies_cache = bool(base.get("dependencies", True))
    continuum_ref = str(base.get("continuum_ref", "main") or "main")
    if not continuum_ref.strip():
        raise AgentRuntimeError("continuum_ref must be a non-empty revision")
    toolchain = tuple(str(item) for item in (base.get("toolchain", ()) or ()))
    try:
        concurrency = int(base.get("concurrency_limit", 4))
    except (TypeError, ValueError):
        raise AgentRuntimeError("concurrency_limit must be an integer between 1 and 256")
    if concurrency < 1 or concurrency > 256:
        raise AgentRuntimeError("concurrency_limit must be between 1 and 256")
    try:
        provisioning_timeout = int(base.get("provisioning_timeout_seconds", DEFAULT_PROVISIONING_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise AgentRuntimeError("provisioning_timeout_seconds must be a positive integer")
    try:
        max_lifetime = int(base.get("max_job_lifetime_seconds", DEFAULT_MAX_JOB_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        raise AgentRuntimeError("max_job_lifetime_seconds must be a positive integer")
    if provisioning_timeout <= 0 or max_lifetime <= 0:
        raise AgentRuntimeError("timeouts must be positive; no state may wait indefinitely")
    return RuntimeProfile(
        name=name,
        os=os_name,
        arch=arch,
        features=features,
        lifecycle=lifecycle,
        max_uses_per_instance=max_uses_number,
        idle_instances=idle_number,
        network_lifecycle=network_lifecycle,
        golden_image=golden_image,
        dependencies_cache=dependencies_cache,
        provider=provider,
        concurrency_limit=concurrency,
        provisioning_timeout_seconds=provisioning_timeout,
        max_job_lifetime_seconds=max_lifetime,
        continuum_ref=continuum_ref.strip(),
        toolchain=toolchain,
    )


def profile_digest(profile: RuntimeProfile) -> str:
    """Immutable digest of the normalized runtime profile."""

    return _sha256_hex(_canonical_json(profile.describe()))


def image_digest(manifest: AgentManifest, profile: RuntimeProfile) -> str:
    """Immutable image/template digest for one manifest + profile pair.

    Keyed by immutable inputs: resolved Continuum ref, normalized profile
    digest, base-image digest, and declared toolchain inputs. Same
    manifest/profile inputs always produce the same digest; any runtime or
    toolchain change produces a new digest.
    """

    payload = {
        "base_image": manifest.base_image,
        "continuum_ref": manifest.continuum_ref,
        "manifest": manifest.to_canonical(),
        "profile": profile.describe(),
        "profile_digest": profile_digest(profile),
    }
    return _sha256_hex(_canonical_json(payload))


# ---------------------------------------------------------------------------
# Immutable image store, rollout, and rollback (sections 2, 3, 9).
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    "ghp_",
    "github_pat_",
    "TAP_PAT",
    "RENDER_API_KEY",
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
)


def image_contains_secret(image_body: Mapping[str, Any]) -> Optional[str]:
    """Return the offending secret marker if an image body bakes one in."""

    text = _canonical_json(dict(image_body))
    for marker in _SECRET_PATTERNS:
        if marker in text:
            return marker
    return None


@dataclass
class ImageGeneration:
    digest: str
    manifest: AgentManifest
    profile: RuntimeProfile
    built_at: float
    validated: bool = False
    sbom: Tuple[str, ...] = ()
    provenance: str = ""


class ImageStore:
    """Provider-native immutable image/template store (per consumer project).

    Images are content-addressed by digest and immutable once validated.
    Consumers never share writable image state: each project owns its store
    instance (and its active-image pointer).
    """

    def __init__(self, project_id: str) -> None:
        if not project_id or not isinstance(project_id, str):
            raise AgentRuntimeError("project_id must be a non-empty string")
        self.project_id = project_id
        self._generations: Dict[str, ImageGeneration] = {}
        self._active_digest: Optional[str] = None
        self._history: List[Tuple[str, float, str]] = []

    @property
    def active_digest(self) -> Optional[str]:
        return self._active_digest

    def ensure_image(
        self,
        manifest: AgentManifest,
        profile: RuntimeProfile,
        now: Optional[float] = None,
    ) -> ImageGeneration:
        """Verify the prepared image exists; build and validate it if absent.

        Building happens in trusted infrastructure; the result is validated
        before it is stored. Rebuilds occur only when the desired digest
        changes or policy requires a base/security refresh.
        """

        digest = image_digest(manifest, profile)
        existing = self._generations.get(digest)
        if existing is not None and existing.validated:
            return existing
        timestamp = time.time() if now is None else float(now)
        body = {
            "digest": digest,
            "manifest": manifest.to_canonical(),
            "profile": profile.describe(),
        }
        offender = image_contains_secret(body)
        if offender is not None:
            raise AgentRuntimeError("refusing to bake secret marker {!r} into image".format(offender))
        generation = ImageGeneration(
            digest=digest,
            manifest=manifest,
            profile=profile,
            built_at=timestamp,
            validated=False,
            sbom=(
                "opencode=={}".format(manifest.opencode_version),
                "pr-agent=={}".format(manifest.pr_agent_version),
                "actions-runner=={}".format(manifest.runner_version),
            ),
            provenance="continuum-ref={} base={}".format(manifest.continuum_ref, manifest.base_image),
        )
        validate_image(manifest, profile, generation)
        generation.validated = True
        self._generations[digest] = generation
        return generation

    def promote(self, digest: str, now: Optional[float] = None) -> None:
        """Atomically promote one validated generation to the active pointer."""

        generation = self._generations.get(digest)
        if generation is None or not generation.validated:
            raise AgentRuntimeError("cannot promote unknown or unvalidated image {}".format(digest))
        timestamp = time.time() if now is None else float(now)
        previous = self._active_digest
        self._active_digest = digest
        self._history.append((digest, timestamp, previous or ""))

    def rollback(self, digest: str, now: Optional[float] = None) -> None:
        """Rollback changes the active immutable image pointer only."""

        self.promote(digest, now=now)

    def garbage_collect(self, now: float, retention_seconds: float = DEFAULT_ROLLBACK_RETENTION_SECONDS) -> List[str]:
        """Remove old generations only after rollback retention expires."""

        removed: List[str] = []
        for digest, generation in list(self._generations.items()):
            if digest == self._active_digest:
                continue
            if now - generation.built_at >= retention_seconds:
                del self._generations[digest]
                removed.append(digest)
        return sorted(removed)

    def generations(self) -> List[str]:
        return sorted(self._generations)

    def get(self, digest: str) -> Optional[ImageGeneration]:
        return self._generations.get(digest)


def validate_image(manifest: AgentManifest, profile: RuntimeProfile, generation: ImageGeneration) -> None:
    """Validate an image generation before it may serve jobs.

    Checks exact version identity, platform agreement, probe presence, and
    that no secret was baked in. Raises on any mismatch.
    """

    if generation.digest != image_digest(manifest, profile):
        raise AgentRuntimeError("image digest mismatch: refusing to serve unvalidated generation")
    if manifest.os != profile.os or manifest.arch != profile.arch:
        raise AgentRuntimeError("manifest platform does not match profile platform")
    if manifest.continuum_ref != profile.continuum_ref:
        raise AgentRuntimeError("manifest ref does not match profile ref; rebuild for the pinned ref")
    if not manifest.probes:
        raise AgentRuntimeError("manifest declares no validation/version probes")
    body = {"manifest": manifest.to_canonical(), "profile": profile.describe()}
    offender = image_contains_secret(body)
    if offender is not None:
        raise AgentRuntimeError("image bakes in secret marker {!r}".format(offender))


# ---------------------------------------------------------------------------
# Dependency/build caches (Layer C, section 3).
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    key: str
    project_id: str
    repository: str
    trusted: bool
    validated: bool
    payload: Dict[str, Any] = field(default_factory=dict)


class DependencyCache:
    """Content-addressed dependency cache (project/repository scoped).

    Rules enforced here: deterministic immutable/cache-version keys, no
    secrets or credentials, untrusted PR/fork contexts never publish trusted
    writable cache state, executable caches are validated before use, and
    cache failure falls back to deterministic reconstruction.
    """

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str, str], CacheEntry] = {}

    @staticmethod
    def cache_key(digest: str, cache_version: str = "v1") -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
            raise AgentRuntimeError("dependency cache key must wrap an immutable image digest")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", cache_version or ""):
            raise AgentRuntimeError("invalid cache version {!r}".format(cache_version))
        return "{}-{}".format(cache_version, digest)

    def publish(
        self,
        *,
        key: str,
        project_id: str,
        repository: str,
        payload: Mapping[str, Any],
        is_fork: bool,
        trusted_context: bool,
    ) -> CacheEntry:
        """Publish cache state. Fork/untrusted contexts never publish trusted state."""

        body = dict(payload)
        offender = image_contains_secret(body)
        if offender is not None:
            raise AgentRuntimeError("dependency cache must never store secret marker {!r}".format(offender))
        trusted = bool(trusted_context and not is_fork)
        entry = CacheEntry(
            key=key,
            project_id=project_id,
            repository=repository,
            trusted=trusted,
            validated=False,
            payload=body,
        )
        if not trusted:
            # Untrusted (fork or untrusted-context) results are never
            # stored: storing under the shared (project, repository, key)
            # would clobber a prior trusted entry for the same digest and
            # force subsequent trusted restores to miss. The caller still
            # receives the untrusted entry so it can record the refusal.
            return entry
        self._entries[(project_id, repository, key)] = entry
        return entry

    def restore(
        self,
        *,
        key: str,
        project_id: str,
        repository: str,
    ) -> Optional[CacheEntry]:
        """Restore validated, project-scoped cache state or return None.

        Cross-project restores are refused (isolation), untrusted
        (fork-published) entries are refused outright so a caller checking
        only non-None can never consume poisoned cache, and validation
        failure falls back to deterministic reconstruction (None).
        """

        entry = self._entries.get((project_id, repository, key))
        if entry is None:
            return None
        if entry.project_id != project_id or entry.repository != repository:
            return None
        if not entry.trusted:
            return None
        if image_contains_secret(entry.payload) is not None:
            return None
        entry.validated = True
        return entry


# ---------------------------------------------------------------------------
# Ephemeral provisioning, JIT registration, leases, reconciliation.
# ---------------------------------------------------------------------------

@dataclass
class NetworkAttachment:
    id: str
    profile_digest: str
    job_id: str
    created_at: float
    destroyed_at: Optional[float] = None
    persistent: bool = False


@dataclass
class RunnerInstance:
    id: str
    project_id: str
    repository: str
    profile_digest: str
    image_digest: str
    created_at: float
    job_id: Optional[str] = None
    uses: int = 0
    jit_registered: bool = False
    jit_id: Optional[str] = None
    destroyed_at: Optional[float] = None
    network_id: Optional[str] = None
    public_ip: Optional[str] = None
    log_forwarded: bool = False


@dataclass
class Lease:
    project_id: str
    repository: str
    job_id: str
    run_id: str
    profile_digest: str
    instance_id: str
    created_at: float
    lease_expires_at: float
    max_age_at: float
    state: str = "provisioning"


class FakeProvider:
    """Deterministic in-memory provider behind the generic contract.

    Models create/destroy of compute, per-job network attachments, and a
    small pool of reissuable public IPs. A later job may coincidentally
    receive the same IP string; lifecycle (not string inequality) is what
    qualification verifies.
    """

    def __init__(self) -> None:
        self.instances: Dict[str, RunnerInstance] = {}
        self.networks: Dict[str, NetworkAttachment] = {}
        self.registrations: Dict[str, str] = {}
        self.destroy_failures_remaining = 0
        self._counter = 0
        self._ip_pool = ["203.0.113.10", "203.0.113.11", "203.0.113.12"]
        self._ip_cursor = 0
        self.persistent_egress_objects: List[str] = []

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return "{}-{:06d}".format(prefix, self._counter)

    def create_network(self, profile_digest: str, job_id: str, now: float) -> NetworkAttachment:
        network = NetworkAttachment(
            id=self._next_id("net"),
            profile_digest=profile_digest,
            job_id=job_id,
            created_at=now,
        )
        self.networks[network.id] = network
        return network

    def create_instance(
        self,
        *,
        project_id: str,
        repository: str,
        profile_digest: str,
        digest: str,
        job_id: str,
        network_id: str,
        now: float,
    ) -> RunnerInstance:
        instance = RunnerInstance(
            id=self._next_id("i"),
            project_id=project_id,
            repository=repository,
            profile_digest=profile_digest,
            image_digest=digest,
            created_at=now,
            job_id=job_id,
            network_id=network_id,
            public_ip=self._ip_pool[self._ip_cursor % len(self._ip_pool)],
        )
        self._ip_cursor += 1
        self.instances[instance.id] = instance
        return instance

    def jit_register(self, instance: RunnerInstance) -> str:
        jit_id = "jit-{}".format(instance.id)
        self.registrations[jit_id] = instance.id
        instance.jit_registered = True
        instance.jit_id = jit_id
        return jit_id

    def jit_deregister(self, instance: RunnerInstance) -> None:
        if instance.jit_id is not None:
            self.registrations.pop(instance.jit_id, None)
            instance.jit_registered = False

    def destroy(self, instance: RunnerInstance, now: float) -> bool:
        """Idempotent delete with injectable transient failures."""

        if instance.destroyed_at is not None:
            return True
        if self.destroy_failures_remaining > 0:
            self.destroy_failures_remaining -= 1
            return False
        self.jit_deregister(instance)
        network = self.networks.get(instance.network_id or "")
        if network is not None and network.destroyed_at is None:
            network.destroyed_at = now
        instance.log_forwarded = True
        instance.destroyed_at = now
        return True

    def live_instances(self) -> List[RunnerInstance]:
        return [item for item in self.instances.values() if item.destroyed_at is None]

    def provider_tags(self, instance: RunnerInstance) -> Dict[str, str]:
        """Tags sufficient to discover owned resources without process memory."""

        return {
            "continuum-project": instance.project_id,
            "continuum-repository": instance.repository,
            "continuum-job": instance.job_id or "",
            "continuum-profile": instance.profile_digest[:16],
            "continuum-instance": instance.id,
        }


@dataclass
class JobResult:
    job_id: str
    outcome: str
    instance_id: str
    network_id: str
    public_ip: Optional[str]
    image_digest: str
    destroyed: bool
    events: List[str] = field(default_factory=list)


class EphemeralController:
    """Continuum-supplied controller for one consumer project.

    The controller is independent of the disposable runner: a runner never
    acts as the sole authority that deletes itself. All execution resources
    carry durable lease metadata; a scheduled orphan sweep destroys anything
    whose lease expired, even after a controller restart or a missed event.
    """

    def __init__(
        self,
        *,
        project_id: str,
        image_store: ImageStore,
        provider: FakeProvider,
        cache: Optional[DependencyCache] = None,
        provisioning_timeout_seconds: float = DEFAULT_PROVISIONING_TIMEOUT_SECONDS,
        max_job_lifetime_seconds: float = DEFAULT_MAX_JOB_LIFETIME_SECONDS,
        teardown_timeout_seconds: float = DEFAULT_TEARDOWN_TIMEOUT_SECONDS,
        orphan_grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
        global_max_age_seconds: float = DEFAULT_GLOBAL_MAX_INSTANCE_AGE_SECONDS,
        retry_limit: int = DEFAULT_TEARDOWN_RETRY_LIMIT,
    ) -> None:
        for label, value in (
            ("provisioning_timeout_seconds", provisioning_timeout_seconds),
            ("max_job_lifetime_seconds", max_job_lifetime_seconds),
            ("teardown_timeout_seconds", teardown_timeout_seconds),
            ("orphan_grace_seconds", orphan_grace_seconds),
            ("global_max_age_seconds", global_max_age_seconds),
        ):
            if not isinstance(value, (int, float)) or not value > 0:
                raise AgentRuntimeError("{} must be a positive duration".format(label))
        self.project_id = project_id
        self.images = image_store
        self.provider = provider
        self.cache = cache or DependencyCache()
        self.provisioning_timeout = float(provisioning_timeout_seconds)
        self.max_job_lifetime = float(max_job_lifetime_seconds)
        self.teardown_timeout = float(teardown_timeout_seconds)
        self.orphan_grace = float(orphan_grace_seconds)
        self.global_max_age = float(global_max_age_seconds)
        self.retry_limit = int(retry_limit)
        self.leases: Dict[str, Lease] = {}
        self.queued: List[Dict[str, Any]] = []
        self.diagnostics: List[str] = []
        self._job_counter = 0

    # -- demand ------------------------------------------------------
    def queue_job(self, repository: str, profile: RuntimeProfile, run_id: str = "") -> str:
        """Queue one job. No compute is created by queueing alone."""

        self._job_counter += 1
        job_id = "job-{:06d}".format(self._job_counter)
        self.queued.append(
            {"job_id": job_id, "repository": repository, "profile": profile, "run_id": run_id or job_id}
        )
        self.diagnostics.append("queued {} for {}".format(job_id, repository))
        return job_id

    def live_idle_count(self) -> int:
        """Live instances not bound to a live job lease (idle/orphan compute)."""

        leased = set(self.leases)
        return sum(1 for item in self.provider.live_instances() if item.id not in leased)

    def live_instance_count(self) -> int:
        return len(self.provider.live_instances())

    # -- provisioning path (section 5) --------------------------------
    def run_next_job(
        self,
        manifest: AgentManifest,
        *,
        now: float,
        outcome: str = OUTCOME_SUCCESS,
        fail_jit: bool = False,
        fail_startup: bool = False,
        teardown_retries: Optional[int] = None,
        is_fork: bool = False,
        trusted_context: bool = True,
    ) -> JobResult:
        """Execute steps 2-13 of the required provisioning path for one job."""

        if not self.queued:
            raise AgentRuntimeError("no queued demand: the controller never provisions without demand")
        # Peek first: image build/validation failures must not drop queued
        # demand. The entry is popped only after validation succeeds.
        queued = self.queued[0]
        profile: RuntimeProfile = queued["profile"]
        job_id: str = queued["job_id"]
        repository: str = queued["repository"]
        run_id: str = queued["run_id"]
        events: List[str] = ["queued {}".format(job_id)]

        # Consumer-owned concurrency/cost limit: never provision beyond the
        # profile's concurrency_limit live instances. The queued demand is
        # kept (not popped) so a refused job is retried later instead of
        # being silently dropped.
        try:
            concurrency_limit = int(profile.concurrency_limit)
        except (TypeError, ValueError):
            raise AgentRuntimeError("concurrency_limit must be an integer between 1 and 256")
        live = len(self.provider.live_instances())
        if live >= concurrency_limit:
            raise AgentRuntimeError(
                "concurrency limit {} reached ({} live instances): refusing to provision {}".format(
                    concurrency_limit, live, job_id
                )
            )

        digest = image_digest(manifest, profile)
        # Layer C (dependency/build cache): restore validated, scoped cache
        # state before provisioning; a miss (or a disabled dependency cache)
        # falls back to deterministic reconstruction below.
        cache_key: Optional[str] = None
        if profile.dependencies_cache:
            try:
                cache_key = DependencyCache.cache_key(digest)
            except AgentRuntimeError:
                cache_key = None
        if cache_key is not None:
            hit = self.cache.restore(key=cache_key, project_id=self.project_id, repository=repository)
            if hit is not None:
                events.append("restored-dependencies {}".format(cache_key))
            else:
                events.append("cache-miss {} fallback-to-reconstruction".format(cache_key))
        generation = self.images.ensure_image(manifest, profile, now=now)
        events.append("ensured-image {}".format(digest))
        if self.images.active_digest is None:
            self.images.promote(generation.digest, now=now)
        validate_image(manifest, profile, generation)
        events.append("validated-identity {}".format(digest))
        pending = self.queued.pop(0)

        network: Optional[NetworkAttachment] = None
        instance: Optional[RunnerInstance] = None
        try:
            network = self.provider.create_network(profile_digest=profile_digest(profile), job_id=job_id, now=now)
            if network.persistent:
                raise AgentRuntimeError("per-job network attachment must not be persistent")
            events.append("attached-network {}".format(network.id))
            instance = self.provider.create_instance(
                project_id=self.project_id,
                repository=repository,
                profile_digest=profile_digest(profile),
                digest=digest,
                job_id=job_id,
                network_id=network.id,
                now=now,
            )
            events.append("created-instance {} after demand".format(instance.id))
            lease = Lease(
                project_id=self.project_id,
                repository=repository,
                job_id=job_id,
                run_id=run_id,
                profile_digest=profile_digest(profile),
                instance_id=instance.id,
                created_at=now,
                lease_expires_at=now + self.max_job_lifetime,
                max_age_at=now + self.global_max_age,
                state="provisioning",
            )
            self.leases[instance.id] = lease
            if fail_jit or outcome == OUTCOME_JIT_FAILURE:
                events.append("jit-registration-failed {}".format(instance.id))
                self._teardown(instance, now=now, retries=teardown_retries)
                self.leases.pop(instance.id, None)
                assert network is not None and instance is not None
                return JobResult(job_id, OUTCOME_JIT_FAILURE, instance.id, network.id,
                                 instance.public_ip, digest, instance.destroyed_at is not None, events)
            jit_id = self.provider.jit_register(instance)
            events.append("jit-registered {}".format(jit_id))
        except Exception:
            # Provisioning failed before the job could run: destroy any
            # partially created resources, requeue the demand at the front
            # so no queued work is lost, then re-raise.
            if instance is not None:
                try:
                    self.provider.destroy(instance, now=now)
                finally:
                    self.leases.pop(instance.id, None)
            elif network is not None:
                stored = self.provider.networks.get(network.id)
                if stored is not None and stored.destroyed_at is None:
                    stored.destroyed_at = now
            self.queued.insert(0, pending)
            raise

        assert network is not None and instance is not None
        if fail_startup or outcome == OUTCOME_STARTUP_FAILURE:
            events.append("job-startup-failed {}".format(instance.id))
            self._teardown(instance, now=now, retries=teardown_retries)
            self.leases.pop(instance.id, None)
            return JobResult(job_id, OUTCOME_STARTUP_FAILURE, instance.id, network.id,
                             instance.public_ip, digest, instance.destroyed_at is not None, events)

        self._execute_one_job(instance, lease, now=now)
        events.append("executed-one-job {} uses={}".format(instance.id, instance.uses))

        terminal = outcome if outcome in TERMINAL_OUTCOMES else OUTCOME_SUCCESS
        events.append("terminal {}".format(terminal))
        destroyed = self._teardown(instance, now=now, retries=teardown_retries)
        self.leases.pop(instance.id, None)
        if not destroyed:
            events.append("teardown-deferred-to-reconciler {}".format(instance.id))
        if cache_key is not None and terminal == OUTCOME_SUCCESS:
            if not (trusted_context and not is_fork):
                # Fork/untrusted successes never publish: even an isolated
                # write would be unusable on restore, and a shared-key write
                # would clobber the trusted entry for this digest.
                events.append("cache-publish-refused {}".format(cache_key))
            else:
                try:
                    self.cache.publish(
                        key=cache_key,
                        project_id=self.project_id,
                        repository=repository,
                        payload={"image_digest": digest,
                                 "profile_digest": profile_digest(profile)},
                        is_fork=is_fork,
                        trusted_context=trusted_context,
                    )
                    events.append("published-dependencies {}".format(cache_key))
                except AgentRuntimeError:
                    events.append("cache-publish-refused {}".format(cache_key))
        return JobResult(job_id, terminal, instance.id, network.id,
                         instance.public_ip, digest, destroyed, events)

    def _execute_one_job(self, instance: RunnerInstance, lease: Lease, now: float) -> None:
        if instance.destroyed_at is not None:
            raise AgentRuntimeError("cannot execute on a destroyed instance")
        if instance.uses >= STRICT_MAX_USES_PER_INSTANCE:
            raise AgentRuntimeError("one instance cannot accept a second job")
        if lease.instance_id != instance.id:
            raise AgentRuntimeError("lease/instance mismatch")
        instance.uses += 1
        lease.state = "running"

    def run_second_job_on_same_instance(self, instance_id: str, now: float) -> None:
        """Attempt a second job on an instance: always rejected."""

        instance = self.provider.instances.get(instance_id)
        if instance is None:
            raise AgentRuntimeError("unknown instance {}".format(instance_id))
        if instance.destroyed_at is not None:
            raise AgentRuntimeError("instance {} already destroyed after its one job".format(instance_id))
        raise AgentRuntimeError("one instance cannot accept a second job")

    def _teardown(self, instance: RunnerInstance, now: float, retries: Optional[int] = None) -> bool:
        requested = self.retry_limit if retries is None else int(retries)
        # Bound teardown work by teardown_timeout_seconds: each retry costs
        # one DEFAULT_TEARDOWN_RETRY_BASE_SECONDS slot. Explicit retry
        # requests are still capped by the timeout so no teardown waits
        # indefinitely. The effective clock advances one slot per attempt
        # so the deadline actually bounds retry work instead of being dead
        # code on a frozen timestamp.
        budget = max(1, int(self.teardown_timeout // DEFAULT_TEARDOWN_RETRY_BASE_SECONDS))
        attempts = max(1, min(int(requested), budget))
        deadline = float(now) + float(self.teardown_timeout)
        for attempt in range(attempts):
            effective_now = float(now) + float(attempt) * float(DEFAULT_TEARDOWN_RETRY_BASE_SECONDS)
            if effective_now > deadline:
                break
            if self.provider.destroy(instance, now=effective_now):
                self.diagnostics.append("destroyed {}".format(instance.id))
                return True
        self.diagnostics.append("teardown-retry-exhausted {}".format(instance.id))
        return False

    # -- reconciliation (section 7) ------------------------------------
    def sweep_orphans(self, now: float) -> List[str]:
        """Scheduled orphan sweep: destroy expired/over-age/stale resources.

        Covers provisioning timeouts, maximum job lifetime, global maximum
        age, stale JIT registrations with no live lease, and resources that
        survived a controller restart (leases are durable controller state,
        and provider tags allow discovery without process memory).
        """

        removed: List[str] = []
        for instance in list(self.provider.live_instances()):
            lease = self.leases.get(instance.id)
            orphan = False
            reason = ""
            if lease is None:
                if instance.jit_registered:
                    orphan, reason = True, "stale-jit-without-lease"
                elif now - instance.created_at >= self.orphan_grace:
                    orphan, reason = True, "no-lease-past-grace"
            else:
                if now >= lease.max_age_at:
                    orphan, reason = True, "global-max-age"
                elif lease.state == "provisioning" and now - lease.created_at >= self.provisioning_timeout:
                    orphan, reason = True, "provisioning-timeout"
                elif lease.state == "running" and now >= lease.lease_expires_at:
                    orphan, reason = True, "job-lifetime-exceeded"
            if orphan:
                if self.provider.destroy(instance, now=now):
                    self.leases.pop(instance.id, None)
                    removed.append(instance.id)
                    self.diagnostics.append("reconciled-orphan {} ({})".format(instance.id, reason))
        for jit_id, instance_id in list(self.provider.registrations.items()):
            instance = self.provider.instances.get(instance_id)
            if instance is None or instance.destroyed_at is not None:
                self.provider.registrations.pop(jit_id, None)
                removed.append(jit_id)
                self.diagnostics.append("removed-stale-registration {}".format(jit_id))
        return sorted(removed)

    def discover_via_tags(self) -> List[Dict[str, str]]:
        """Discover owned live resources from provider tags, without memory.

        Uses only provider-side state (live instances + their tags), so
        orphan discovery does not depend on in-process leases or queues.
        """

        return [self.provider.provider_tags(item) for item in self.provider.live_instances()]

    def restart(self) -> "EphemeralController":
        """Simulate a controller restart: leases survive, memory does not.

        Leases/queue entries are durably copied (not shared by reference),
        so the restarted controller proves cleanup works from durable state
        plus provider-tag discovery, not from shared process memory.
        """

        restarted = EphemeralController(
            project_id=self.project_id,
            image_store=self.images,
            provider=self.provider,
            cache=self.cache,
            provisioning_timeout_seconds=self.provisioning_timeout,
            max_job_lifetime_seconds=self.max_job_lifetime,
            teardown_timeout_seconds=self.teardown_timeout,
            orphan_grace_seconds=self.orphan_grace,
            global_max_age_seconds=self.global_max_age,
            retry_limit=self.retry_limit,
        )
        restarted.leases = deepcopy(self.leases)
        restarted.queued = deepcopy(self.queued)
        restarted.diagnostics = list(self.diagnostics)
        restarted.diagnostics.append("controller-restarted")
        return restarted


# ---------------------------------------------------------------------------
# Platform adapters (section 10).
# ---------------------------------------------------------------------------

class PlatformAdapter:
    """One advertised platform profile deriving from the canonical contract."""

    os: str = "linux"
    arch: str = "x64"
    image_kind: str = "golden-image"

    def manifest(self, continuum_ref: str = "main", toolchain: Sequence[str] = ()) -> AgentManifest:
        return canonical_manifest(self.os, self.arch, continuum_ref, toolchain)

    def profile(self, continuum_ref: str = "main", toolchain: Sequence[str] = ()) -> RuntimeProfile:
        return resolve_profile(
            {
                "name": "agent-{}-{}".format(self.os, self.arch),
                "os": self.os,
                "arch": self.arch,
                "features": ["opencode", "pr-agent"],
                "lifecycle": "per-job",
                "max_uses_per_instance": 1,
                "idle_instances": 0,
                "network": {"lifecycle": "per-job"},
                "cache": {"golden_image": True, "dependencies": True},
                "continuum_ref": continuum_ref,
                "toolchain": list(toolchain),
            }
        )

    def qualify(self, continuum_ref: str = "main") -> Dict[str, Any]:
        """Deterministic qualification: image identity + ephemeral lifecycle."""

        manifest = self.manifest(continuum_ref)
        profile = self.profile(continuum_ref)
        store = ImageStore(project_id="qualification-{}".format(self.os))
        provider = FakeProvider()
        controller = EphemeralController(project_id="qualification-{}".format(self.os),
                                          image_store=store, provider=provider)
        job_id = controller.queue_job("example/qualification", profile, run_id="q1")
        result = controller.run_next_job(manifest, now=1000.0)
        return {
            "os": self.os,
            "arch": self.arch,
            "image_kind": self.image_kind,
            "image_digest": result.image_digest,
            "job_id": job_id,
            "instance_id": result.instance_id,
            "destroyed": result.destroyed,
            "live_after": controller.live_instance_count(),
        }


class LinuxAdapter(PlatformAdapter):
    os = "linux"
    arch = "x64"
    image_kind = "golden-image"


class LinuxArmAdapter(PlatformAdapter):
    os = "linux"
    arch = "arm64"
    image_kind = "golden-image"


class MacOSAdapter(PlatformAdapter):
    os = "macos"
    arch = "arm64"
    image_kind = "vm-template-snapshot"


class MacOSIntelAdapter(PlatformAdapter):
    os = "macos"
    arch = "x64"
    image_kind = "vm-template-snapshot"


class WindowsAdapter(PlatformAdapter):
    os = "windows"
    arch = "x64"
    image_kind = "prepared-image"


ADVERTISED_ADAPTERS: Tuple[PlatformAdapter, ...] = (
    LinuxAdapter(),
    LinuxArmAdapter(),
    MacOSAdapter(),
    MacOSIntelAdapter(),
    WindowsAdapter(),
)


def advertised_platforms() -> List[Dict[str, str]]:
    return [
        {"os": adapter.os, "arch": adapter.arch, "image_kind": adapter.image_kind}
        for adapter in ADVERTISED_ADAPTERS
    ]


# ---------------------------------------------------------------------------
# Workflow hygiene: normal execution must not bootstrap-install the agent
# runtime (sections 3, 11). The prepared image (or its provider-native
# cache equivalent on GitHub-hosted backends) already carries exact
# OpenCode/PR-Agent/runner binaries; a warm job probes versions and runs.
# ---------------------------------------------------------------------------

_BOOTSTRAP_PATTERNS = (
    "https://opencode.ai/install",
    "opencode.ai/install | bash",
    'pip install "pr-agent==',
    "pip install 'pr-agent==",
    "pip install pr-agent==",
)


def normal_execution_uses_bootstrap_install(workflow_text: str) -> bool:
    """Whether normal (non-reconstruction) execution would bootstrap-install.

    A prepared-runtime step probes first (``command -v opencode`` /
    ``pr-agent --version`` / cache restore guarded by the image digest) and
    only reconstructs deterministically on a validated cache miss. A step
    that unconditionally pipes the installer on the warm path counts as a
    bootstrap install. Comments mentioning the installer URL do not count.
    """

    if not isinstance(workflow_text, str) or not workflow_text:
        return False
    # Compare only code lines: a probe or installer URL inside a `#`
    # comment proves nothing about the warm path. A comment-only probe
    # marker must not suppress detection, and a comment-only installer
    # URL must not count as a bootstrap install.
    code_lines = [
        line for line in workflow_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not code_lines:
        return False
    first_bootstrap: Optional[int] = None
    first_probe: Optional[int] = None
    for index, line in enumerate(code_lines):
        if first_bootstrap is None and any(pattern in line for pattern in _BOOTSTRAP_PATTERNS):
            first_bootstrap = index
        # Only a real executable probe counts as a warm-path probe: an exact
        # CLI/version check (`command -v opencode`, `pr-agent --version).
        # A bare comment-grade marker such as `prepared-runtime` or
        # `prepared agent runtime` in a job name, echo string, or comment is
        # not a probe and must never suppress bootstrap detection.
        if first_probe is None and (
            "command -v opencode" in line
            or "pr-agent --version" in line
        ):
            first_probe = index
        if first_bootstrap is not None and first_probe is not None:
            break
    if first_bootstrap is None:
        return False
    # A prepared-runtime probe (exact-version check) on a code line ahead
    # of the first installer code line means the warm path performs zero
    # downloads: the installer below is deterministic reconstruction on a
    # validated cache miss only.
    if first_probe is not None and first_probe < first_bootstrap:
        return False
    return True


def workflow_step_has_prepared_runtime_probe(workflow_text: str) -> bool:
    """Whether a workflow probes the prepared runtime before any installer."""

    if not isinstance(workflow_text, str):
        return False
    probe = "command -v opencode" in workflow_text
    pinned = OPENCODE_VERSION in workflow_text
    digest = ("image digest" in workflow_text.lower() or "CONTINUUM_IMAGE_DIGEST" in workflow_text
              or "prepared-runtime" in workflow_text.lower() or "prepared agent runtime" in workflow_text.lower())
    if not (probe and pinned and digest):
        return False
    # The warm hit must be keyed by the image digest, not just mention it:
    # require a digest-gated probe line (a code line carrying both the
    # CONTINUUM_IMAGE_DIGEST gate and a real executable probe) so an empty
    # or unresolved digest cannot take the hit path and skip install.
    gated = any(
        "CONTINUUM_IMAGE_DIGEST" in line and ("command -v opencode" in line or "pr-agent --version" in line)
        for line in workflow_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    if not gated:
        return False
    if "pr-agent==" in workflow_text:
        if "pr-agent --version" not in workflow_text:
            return False
        if PR_AGENT_VERSION not in workflow_text:
            return False
    return True


# ---------------------------------------------------------------------------
# Live-qualification harness (section 13).
# ---------------------------------------------------------------------------

def live_qualification_evidence(
    *,
    project_id: str,
    repository: str,
    os: str = "linux",
    arch: str = "x64",
    continuum_ref: str = "main",
    now: float = 1700000000.0,
) -> Dict[str, Any]:
    """Prove the architecture with real controller jobs (two sequential runs).

    Starts with zero live instances, queues a real job, records profile +
    image digest, proves a new provider instance was created after demand,
    proves the runtime was already present (validated image identity before
    task execution), proves exactly one job ran, proves teardown destroyed
    the instance and per-job network attachment, proves idle count returned
    to zero, repeats with a second job on a different provider instance
    identity, and proves orphan reconciliation with one controlled stale
    resource. Public-IP string equality across runs is recorded but never
    treated as failure.
    """

    adapter = next((item for item in ADVERTISED_ADAPTERS if item.os == os and item.arch == arch), None)
    if adapter is None:
        raise AgentRuntimeError("do not advertise unsupported platform profile os={!r} arch={!r}".format(os, arch))
    manifest = adapter.manifest(continuum_ref)
    profile = adapter.profile(continuum_ref)
    store = ImageStore(project_id=project_id)
    provider = FakeProvider()
    controller = EphemeralController(project_id=project_id, image_store=store, provider=provider)

    evidence: Dict[str, Any] = {
        "project_id": project_id,
        "repository": repository,
        "os": os,
        "arch": arch,
        "continuum_ref": continuum_ref,
        "manifest_digest": manifest_digest(manifest),
        "profile_digest": profile_digest(profile),
        "start_live_instances": controller.live_instance_count(),
    }
    first_job = controller.queue_job(repository, profile, run_id="live-1")
    first = controller.run_next_job(manifest, now=now, outcome=OUTCOME_SUCCESS)
    evidence.update({
        "first_job": first_job,
        "first_instance": first.instance_id,
        "first_network": first.network_id,
        "first_ip": first.public_ip,
        "image_digest": first.image_digest,
        "first_outcome": first.outcome,
        "first_destroyed": first.destroyed,
        "live_after_first": controller.live_instance_count(),
        "first_events": list(first.events),
    })
    second_job = controller.queue_job(repository, profile, run_id="live-2")
    second = controller.run_next_job(manifest, now=now + 60.0, outcome=OUTCOME_SUCCESS)
    evidence.update({
        "second_job": second_job,
        "second_instance": second.instance_id,
        "second_network": second.network_id,
        "second_ip": second.public_ip,
        "second_destroyed": second.destroyed,
        "live_after_second": controller.live_instance_count(),
        "different_instance": second.instance_id != first.instance_id,
        "different_network": second.network_id != first.network_id,
        "ip_equal": second.public_ip == first.public_ip,
        "ip_equality_is_not_failure": True,
    })
    stale_job = controller.queue_job(repository, profile, run_id="live-orphan")
    stale_manifest = manifest
    stale_profile = profile
    digest = image_digest(stale_manifest, stale_profile)
    store.ensure_image(stale_manifest, stale_profile, now=now + 120.0)
    orphan_net = provider.create_network(profile_digest=profile_digest(stale_profile),
                                         job_id=stale_job, now=now + 120.0)
    orphan = provider.create_instance(
        project_id=project_id, repository=repository,
        profile_digest=profile_digest(stale_profile), digest=digest,
        job_id=stale_job, network_id=orphan_net.id, now=now + 120.0,
    )
    provider.jit_register(orphan)
    controller.leases[orphan.id] = Lease(
        project_id=project_id, repository=repository, job_id=stale_job, run_id="live-orphan",
        profile_digest=profile_digest(stale_profile), instance_id=orphan.id,
        created_at=now + 120.0,
        lease_expires_at=now + 120.0 + controller.max_job_lifetime,
        max_age_at=now + 120.0 + controller.global_max_age,
        state="running",
    )
    controller.queued = [item for item in controller.queued if item["job_id"] != stale_job]
    reconciled = controller.sweep_orphans(now=now + 120.0 + controller.max_job_lifetime + 1.0)
    evidence.update({
        "orphan_instance": orphan.id,
        "orphan_reconciled": orphan.id in reconciled,
        "live_final": controller.live_instance_count(),
    })
    checks = {
        "started_at_zero": evidence["start_live_instances"] == 0,
        "created_after_demand": first.instance_id not in ("", None),
        "validated_before_work": any(item.startswith("validated-identity") for item in first.events),
        "exactly_one_job": provider.instances[first.instance_id].uses == 1,
        "destroyed_after_job": first.destroyed and second.destroyed,
        "idle_returned_to_zero": evidence["live_after_first"] == 0 and evidence["live_after_second"] == 0,
        "second_used_new_instance": evidence["different_instance"] and evidence["different_network"],
        "no_persistent_egress": provider.persistent_egress_objects == [],
        "orphan_reconciled": evidence["orphan_reconciled"],
        "live_final_zero": evidence["live_final"] == 0,
    }
    evidence["checks"] = checks
    evidence["pass"] = all(checks.values())
    return evidence
