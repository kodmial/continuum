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
import os
import re
import shutil
import subprocess
import threading
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


def _declares_forbidden_provider_key(value: Any) -> Optional[str]:
    """Return the offending paid-provider key found anywhere in a declaration.

    Scans mapping keys recursively (so ``{"env": {"OPENCODE_API_KEY": ...}}``
    and nested cache/toolchain blocks cannot smuggle a paid key past the
    top level) as well as bare string entries in sequences. Key comparison
    is case-insensitive via ``upper()`` to match the resolver contract.
    """

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.upper() in FORBIDDEN_PROVIDER_KEYS:
                return key
            found = _declares_forbidden_provider_key(item)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str) and item.upper() in FORBIDDEN_PROVIDER_KEYS:
                return item
            found = _declares_forbidden_provider_key(item)
            if found is not None:
                return found
        return None
    if isinstance(value, str) and value.upper() in FORBIDDEN_PROVIDER_KEYS:
        return value
    return None

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
        "runner --version",
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
    if isinstance(toolchain, str) or not isinstance(toolchain, (list, tuple)):
        raise AgentRuntimeError(
            "toolchain must be a list of inputs, got {!r}".format(type(toolchain).__name__)
        )
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

#: Repository variables consumed from the environment by the provisioning
#: path (see docs/consumer-variables.md). They select the strict ephemeral
#: preset and provider backend; strict invariants (zero idle, one job per
#: instance, per-job network) are still enforced by ``resolve_profile`` so
#: the variables cannot retune a strict profile into a persistent runner.
CONTINUUM_RUNTIME_PRESET_VAR = "CONTINUUM_RUNTIME_PRESET"
CONTINUUM_RUNTIME_PROVIDER_VAR = "CONTINUUM_RUNTIME_PROVIDER"
DEFAULT_RUNTIME_PRESET = "agent-linux"
DEFAULT_RUNTIME_PROVIDER = "github-hosted"


def resolve_profile_from_env(
    env: Optional[Mapping[str, Any]] = None,
    **overrides: Any,
) -> RuntimeProfile:
    """Resolve the consumer runtime profile from repository-variable config.

    Reads ``CONTINUUM_RUNTIME_PRESET`` (default ``agent-linux``) and
    ``CONTINUUM_RUNTIME_PROVIDER`` (default ``github-hosted``) from ``env``
    (default ``os.environ``). Explicit keyword overrides win over the
    environment. Enforcement stays in ``resolve_profile``: unknown presets,
    unsupported providers, persistent lifecycles, non-zero idle instances,
    non-single max uses, and paid-provider keys all raise.
    """

    source: Any = os.environ if env is None else env

    def _read(name: str, default: str) -> str:
        try:
            raw = source.get(name)
        except AttributeError:
            raw = None
        text = str(raw).strip() if raw is not None else ""
        return text or default

    declaration: Dict[str, Any] = {
        "preset": _read(CONTINUUM_RUNTIME_PRESET_VAR, DEFAULT_RUNTIME_PRESET),
        "provider": _read(CONTINUUM_RUNTIME_PROVIDER_VAR, DEFAULT_RUNTIME_PROVIDER),
    }
    declaration.update(overrides)
    return resolve_profile(declaration)


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
    offending = _declares_forbidden_provider_key(decl)
    if offending is not None:
        raise AgentRuntimeError(
            "core profiles must never require, read, or forward paid provider key {!r}".format(offending)
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
                "network", "network_lifecycle", "cache", "concurrency_limit",
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
        if "lifecycle" in network_block:
            network_lifecycle = str(network_block["lifecycle"])
        else:
            # A top-level {"network_lifecycle": ...} declaration must not be
            # masked by the default empty network block: fall through to it
            # so persistent intent is validated (and rejected) instead of
            # silently defaulting to per-job.
            network_lifecycle = str(base.get("network_lifecycle", STRICT_NETWORK_LIFECYCLE))
    elif isinstance(network_block, str):
        # A scalar network declaration is a lifecycle intent (e.g.
        # {"network": "persistent"}): validate it as the lifecycle instead
        # of falling through to the per-job default and accepting it.
        network_lifecycle = str(network_block)
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
    if (
        isinstance(idle_instances, bool)
        or not isinstance(idle_instances, int)
        or isinstance(max_uses, bool)
        or not isinstance(max_uses, int)
    ):
        raise AgentRuntimeError("idle_instances and max_uses_per_instance must be integers")
    idle_number = idle_instances
    max_uses_number = max_uses
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
    if cache_block is None:
        cache_block = {}
    golden_image = True
    dependencies_cache = True
    if isinstance(cache_block, Mapping):
        golden_image = bool(cache_block.get("golden_image", base.get("golden_image", True)))
        dependencies_cache = bool(cache_block.get("dependencies", base.get("dependencies", True)))
    elif isinstance(cache_block, bool):
        # A scalar cache declaration is an enable/disable intent for both
        # caches (e.g. {"cache": False} disables the golden image and the
        # dependency cache). An explicit top-level golden_image/dependencies
        # key still overrides its own cache so mixed declarations stay
        # expressible; without one the scalar governs both.
        golden_image = bool(base.get("golden_image", cache_block))
        dependencies_cache = bool(base.get("dependencies", cache_block))
    else:
        raise AgentRuntimeError(
            "cache must be a mapping or boolean, got {!r}".format(type(cache_block).__name__)
        )
    continuum_ref = str(base.get("continuum_ref", "main") or "main")
    if not continuum_ref.strip():
        raise AgentRuntimeError("continuum_ref must be a non-empty revision")
    raw_toolchain = base.get("toolchain", ())
    if raw_toolchain is None:
        raw_toolchain = ()
    if isinstance(raw_toolchain, str) or not isinstance(raw_toolchain, (list, tuple)):
        raise AgentRuntimeError(
            "toolchain must be a list of inputs, got {!r}".format(type(raw_toolchain).__name__)
        )
    toolchain = tuple(str(item) for item in raw_toolchain)
    for item in toolchain:
        if not item or len(item) > 120 or any(char in item for char in "\n\r\0"):
            raise AgentRuntimeError("invalid toolchain input {!r}".format(item))
    if len(set(toolchain)) != len(toolchain):
        raise AgentRuntimeError("toolchain inputs list {!r} twice".format(sorted(toolchain)))
    raw_concurrency = base.get("concurrency_limit", 4)
    if isinstance(raw_concurrency, bool) or not isinstance(raw_concurrency, int):
        raise AgentRuntimeError("concurrency_limit must be an integer between 1 and 256")
    concurrency = raw_concurrency
    if concurrency < 1 or concurrency > 256:
        raise AgentRuntimeError("concurrency_limit must be between 1 and 256")
    raw_provisioning_timeout = base.get(
        "provisioning_timeout_seconds", DEFAULT_PROVISIONING_TIMEOUT_SECONDS
    )
    if isinstance(raw_provisioning_timeout, bool) or not isinstance(
        raw_provisioning_timeout, int
    ):
        raise AgentRuntimeError("provisioning_timeout_seconds must be a positive integer")
    provisioning_timeout = raw_provisioning_timeout
    raw_max_lifetime = base.get(
        "max_job_lifetime_seconds", DEFAULT_MAX_JOB_LIFETIME_SECONDS
    )
    if isinstance(raw_max_lifetime, bool) or not isinstance(raw_max_lifetime, int):
        raise AgentRuntimeError("max_job_lifetime_seconds must be a positive integer")
    max_lifetime = raw_max_lifetime
    if provisioning_timeout <= 0 or max_lifetime <= 0:
        raise AgentRuntimeError("timeouts must be positive; no state may wait indefinitely")
    if max_lifetime > DEFAULT_GLOBAL_MAX_INSTANCE_AGE_SECONDS:
        raise AgentRuntimeError(
            "max_job_lifetime_seconds={} exceeds the global maximum instance age ceiling "
            "of {} seconds: a profile must never extend global-max-age reclamation".format(
                max_lifetime, DEFAULT_GLOBAL_MAX_INSTANCE_AGE_SECONDS
            )
        )
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


#: How long trusted build infrastructure waits for one manifest version
#: probe before the image fails validation.
MANIFEST_PROBE_TIMEOUT_SECONDS = 60

#: Binaries a manifest probe may attest, mapped to the manifest attribute
#: carrying their pinned version. A probe naming none of these proves
#: nothing about the agent runtime and is rejected.
_PROBE_BINARY_VERSION_ATTRS = (
    ("opencode", "opencode_version"),
    ("pr-agent", "pr_agent_version"),
    ("runner", "runner_version"),
)


def _probe_expected_versions(manifest: AgentManifest, probe_text: str) -> List[str]:
    """Return the pinned versions the probe text claims to attest."""

    lowered = str(probe_text).lower()
    return [str(getattr(manifest, attr)) for binary, attr in _PROBE_BINARY_VERSION_ATTRS
            if binary in lowered]


#: Exact allowlist for manifest probes. Only a bare `<binary> --version`
#: or `command -v <binary>` attestation (extra whitespace tolerated) may
#: ever execute: anything with shell metacharacters, flags, redirections,
#: or chained commands (`;`, `|`, `&`, `$`, backticks, parens, `<`, `>`)
#: is rejected before execution.
_PROBE_ALLOWLIST_RE = re.compile(
    r"(?:opencode|pr-agent|runner)\s+--version|command\s+-v\s+(?:opencode|pr-agent|runner)"
)


def _validated_probe_argv(probe_text: str) -> List[str]:
    """Validate a probe against the allowlist and return its argv (no shell)."""

    text = str(probe_text)
    stripped = text.strip()
    if not stripped or not _PROBE_ALLOWLIST_RE.fullmatch(stripped):
        raise AgentRuntimeError(
            "manifest probe {!r} is not an executable version check: "
            "refusing to record probe execution".format(text)
        )
    parts = stripped.split()
    if parts[0] == "command":
        binary = parts[-1]
        return ["command", "-v", binary]
    return [parts[0], "--version"]


def _run_probe_command(probe: str, timeout: float = MANIFEST_PROBE_TIMEOUT_SECONDS) -> str:
    """Run one manifest probe command; return its combined output.

    Probes run without a shell: the allowlisted argv executes directly
    (``command -v`` resolves via ``PATH`` lookup), so injected shell
    syntax can never execute. A non-zero exit means the binary is absent
    or broken, so the probe produces no execution evidence.
    """

    argv = _validated_probe_argv(probe)
    if argv[0] == "command":
        resolved = shutil.which(argv[-1])
        if not resolved:
            raise AgentRuntimeError(
                "manifest probe {!r} failed with exit 1: "
                "refusing to record probe execution".format(probe)
            )
        return resolved + "\n"
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentRuntimeError(
            "manifest probe {!r} could not be executed: {}".format(probe, exc)
        )
    output = "{}{}".format(completed.stdout or "", completed.stderr or "")
    if completed.returncode != 0:
        raise AgentRuntimeError(
            "manifest probe {!r} failed with exit {}: "
            "refusing to record probe execution".format(probe, completed.returncode)
        )
    return output


def execute_manifest_probes(
    manifest: AgentManifest,
    executor: Any = None,
) -> Tuple[str, ...]:
    """Execute each declared manifest probe in trusted build infrastructure.

    This is the single point where version probes are run at build time.
    Each probe is actually executed here (as an allowlisted argv with no
    shell in the build environment, or via ``executor`` when one is
    injected): a zero exit proves the binary is present, and a
    ``--version`` probe must report the manifest's pinned version or the
    binary is not the pinned one. The returned tuple is output-bound
    evidence of the form ``"<probe> => <output>"`` and is stored on the
    generation as ``executed_probes``. It is distinct from the SBOM /
    provenance version strings: echoing versions into metadata without
    running the probes yields no execution record and fails validation,
    and copying bare probe strings into ``executed_probes`` without
    output evidence fails validation too. A base image missing binaries
    can never produce this record.
    """

    if not manifest.probes:
        raise AgentRuntimeError("manifest declares no validation/version probes")
    run = executor if executor is not None else _run_probe_command
    executed: List[str] = []
    for probe in manifest.probes:
        text = str(probe)
        # Allowlist first: anything outside `<binary> --version` /
        # `command -v <binary>` (including `;`, `|`, `$()`, backticks, or
        # redirections smuggling a payload after a valid prefix) is
        # rejected before anything executes.
        _validated_probe_argv(text)
        lowered = text.lower()
        expected = _probe_expected_versions(manifest, text)
        if not expected:
            raise AgentRuntimeError(
                "manifest probe {!r} names no pinned runtime binary "
                "(opencode/pr-agent/runner): refusing to record probe execution".format(text)
            )
        try:
            output = run(text)
        except AgentRuntimeError:
            raise
        except Exception as exc:
            raise AgentRuntimeError(
                "manifest probe {!r} could not be executed: {}".format(text, exc)
            )
        output = str(output or "")
        # A `command -v` probe proves presence by exiting zero; its output
        # is a filesystem path, so no version binding is possible. A
        # `--version` probe must report every pinned version it names.
        if "--version" in lowered:
            missing = [version for version in expected if version not in output]
            if missing:
                raise AgentRuntimeError(
                    "manifest probe {!r} output does not report pinned version(s) {}: "
                    "refusing to record probe execution".format(text, ", ".join(missing))
                )
        one_line = " ".join(output.split())[:500]
        executed.append("{} => {}".format(text, one_line))
    return tuple(executed)


@dataclass
class ImageGeneration:
    digest: str
    manifest: AgentManifest
    profile: RuntimeProfile
    built_at: float
    validated: bool = False
    sbom: Tuple[str, ...] = ()
    provenance: str = ""
    executed_probes: Tuple[str, ...] = ()


class ImageStore:
    """Provider-native immutable image/template store (per consumer project).

    Images are content-addressed by digest and immutable once validated.
    Consumers never share writable image state: each project owns its store
    instance. The active-image pointer is per runtime profile (keyed by
    profile digest): multi-platform consumers promoting e.g. linux-x64 and
    macos-arm64 keep one active digest per profile instead of one global
    pointer where the second profile's jobs would be rejected as "not the
    active image". ``active_digest`` is derived from those per-profile
    pointers and is only defined when they agree on a single digest: with
    divergent per-profile promotions there is no global digest, so it
    returns ``None`` instead of the most recent promotion misleading a
    single-profile caller into serving another platform's image.
    """

    def __init__(self, project_id: str, probe_executor: Any = None) -> None:
        if not project_id or not isinstance(project_id, str):
            raise AgentRuntimeError("project_id must be a non-empty string")
        self.project_id = project_id
        self._generations: Dict[str, ImageGeneration] = {}
        self._active_by_profile: Dict[str, str] = {}
        self._history: List[Tuple[str, float, str]] = []
        # Injected probe runner for trusted build infrastructure. When set,
        # image builds execute manifest probes through it instead of the
        # real subprocess executor, so hosts without the runtime binaries
        # on PATH (or deterministic tests) can supply build-infra evidence
        # without failing provisioning before any compute is created.
        self._probe_executor = probe_executor

    @property
    def active_digest(self) -> Optional[str]:
        distinct = set(self._active_by_profile.values())
        if len(distinct) == 1:
            return next(iter(distinct))
        return None

    def active_digest_for(self, profile: Any) -> Optional[str]:
        """Return the active digest serving one runtime profile.

        Accepts a ``RuntimeProfile`` or an already-computed profile-digest
        string. Returns ``None`` when no generation has been promoted for
        that profile yet.
        """

        if isinstance(profile, RuntimeProfile):
            key = profile_digest(profile)
        else:
            key = str(profile)
        return self._active_by_profile.get(key)

    def ensure_image(
        self,
        manifest: AgentManifest,
        profile: RuntimeProfile,
        now: Optional[float] = None,
        executor: Any = None,
    ) -> ImageGeneration:
        """Verify the prepared image exists; build and validate it if absent.

        Building happens in trusted infrastructure; the result is validated
        before it is stored. Rebuilds occur only when the desired digest
        changes or policy requires a base/security refresh. ``executor``
        overrides the probe runner for this build (falling back to the
        store-level ``probe_executor`` and finally the real subprocess
        executor), so controllers on hosts without the runtime binaries on
        PATH can inject build-infra evidence instead of failing
        provisioning before any compute is created.
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
        # Trusted build infrastructure runs the declared version probes now:
        # the output-bound evidence below is what validation requires, not
        # the version strings alone. A base image missing binaries cannot
        # produce this record (the helper executes each probe and rejects
        # non-executable probes, failed runs, and output that does not
        # report the pinned versions). The executor override (per-call,
        # then store-level, then the real subprocess runner) is the
        # injection path for hosts without the binaries on PATH.
        probe_runner = executor if executor is not None else self._probe_executor
        executed_probes = execute_manifest_probes(manifest, executor=probe_runner)
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
            provenance="continuum-ref={} base={} probes=opencode=={},pr-agent=={},actions-runner=={}".format(
                manifest.continuum_ref,
                manifest.base_image,
                manifest.opencode_version,
                manifest.pr_agent_version,
                manifest.runner_version,
            ),
            executed_probes=executed_probes,
        )
        validate_image(manifest, profile, generation)
        generation.validated = True
        self._generations[digest] = generation
        return generation

    def promote(self, digest: str, now: Optional[float] = None, profile: Any = None) -> None:
        """Atomically promote one validated generation to the active pointer.

        The pointer is per runtime profile: the profile key is the explicit
        ``profile`` argument when given (a ``RuntimeProfile`` or a
        profile-digest string), otherwise the promoted generation's own
        profile. Each profile keeps its own active digest so queuing a
        second platform never invalidates the first.
        """

        generation = self._generations.get(digest)
        if generation is None or not generation.validated:
            raise AgentRuntimeError("cannot promote unknown or unvalidated image {}".format(digest))
        timestamp = time.time() if now is None else float(now)
        if profile is None:
            profile_key = profile_digest(generation.profile)
        elif isinstance(profile, RuntimeProfile):
            profile_key = profile_digest(profile)
        else:
            profile_key = str(profile)
        previous = self._active_by_profile.get(profile_key)
        self._active_by_profile[profile_key] = digest
        self._history.append((digest, timestamp, previous or ""))

    def rollback(self, digest: str, now: Optional[float] = None, profile: Any = None) -> None:
        """Rollback changes the active immutable image pointer only."""

        self.promote(digest, now=now, profile=profile)

    def active_digests(self) -> Dict[str, str]:
        """Return a copy of the per-profile active-image pointers."""

        return dict(self._active_by_profile)

    def garbage_collect(self, now: float, retention_seconds: float = DEFAULT_ROLLBACK_RETENTION_SECONDS) -> List[str]:
        """Remove old generations only after rollback retention expires."""

        removed: List[str] = []
        protected = set(self._active_by_profile.values())
        for digest, generation in list(self._generations.items()):
            if digest in protected:
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

    Checks exact version identity, platform agreement, probe presence and
    execution evidence, and that no secret was baked in. Raises on any
    mismatch.
    """

    if generation.digest != image_digest(manifest, profile):
        raise AgentRuntimeError("image digest mismatch: refusing to serve unvalidated generation")
    if manifest.os != profile.os or manifest.arch != profile.arch:
        raise AgentRuntimeError("manifest platform does not match profile platform")
    if manifest.continuum_ref != profile.continuum_ref:
        raise AgentRuntimeError("manifest ref does not match profile ref; rebuild for the pinned ref")
    if not manifest.probes:
        raise AgentRuntimeError("manifest declares no validation/version probes")
    # Probes must be executable version checks, not bare name mentions: a
    # substring test (`"runner" in probe`) accepts `echo runner --version`
    # as a runner probe even though it could never pass the build-time
    # execution path. Gate on the strict execution allowlist
    # (`_validated_probe_argv`) so declared probes must be exactly
    # `<binary> --version` / `command -v <binary>` (extra whitespace
    # tolerated) naming the runner binary.
    def _has_executable_probe(binary: str) -> bool:
        for probe in manifest.probes:
            try:
                _validated_probe_argv(str(probe))
            except AgentRuntimeError:
                continue
            if binary in str(probe).lower():
                return True
        return False
    if not _has_executable_probe("opencode"):
        raise AgentRuntimeError("manifest declares no opencode version probe")
    if not _has_executable_probe("pr-agent"):
        raise AgentRuntimeError("manifest declares no pr-agent version probe")
    if not _has_executable_probe("runner"):
        raise AgentRuntimeError("manifest declares no runner version probe")
    # The declared probes must actually have been executed at build time:
    # SBOM and provenance version strings are synthesized from manifest
    # versions by the store, so they prove nothing on their own. A base
    # image carrying correct metadata but lacking the binaries would
    # otherwise validate on metadata alone and serve jobs that cannot run.
    # The same executed evidence must therefore also be recorded as
    # output-bound probe-execution evidence (``executed_probes`` entries
    # of the form ``"<probe> => <output>"``, written only by trusted
    # build infrastructure via ``execute_manifest_probes`` after running
    # the probes and checking the output reports the pinned versions), so
    # a generation with echoed SBOM/provenance but no probe execution
    # record still fails -- as does one whose ``executed_probes`` merely
    # repeats the bare probe strings without output evidence.
    executed = tuple(generation.executed_probes or ())
    if not executed:
        raise AgentRuntimeError(
            "image carries no executed probe record: refusing to serve unvalidated generation"
        )
    for probe in manifest.probes:
        prefix = "{} => ".format(probe)
        matches = [entry for entry in executed if entry.startswith(prefix)]
        if not matches:
            raise AgentRuntimeError(
                "image probe {!r} was never executed: refusing to serve unvalidated generation".format(probe)
            )
        if "--version" in str(probe).lower():
            for expected in _probe_expected_versions(manifest, str(probe)):
                if not any(expected in entry.split(" => ", 1)[1] for entry in matches):
                    raise AgentRuntimeError(
                        "image probe {!r} output does not report pinned version {!r}: "
                        "refusing to serve unvalidated generation".format(probe, expected)
                    )
    expected_probe_evidence = (
        "opencode=={}".format(manifest.opencode_version),
        "pr-agent=={}".format(manifest.pr_agent_version),
        "actions-runner=={}".format(manifest.runner_version),
    )
    sbom = tuple(generation.sbom or ())
    for expected in expected_probe_evidence:
        if expected not in sbom:
            raise AgentRuntimeError(
                "image probe evidence missing {!r}: refusing to serve unvalidated generation".format(expected)
            )
    provenance_text = str(generation.provenance or "")
    if not provenance_text.strip():
        raise AgentRuntimeError("image carries no build provenance for its version probes")
    for expected in expected_probe_evidence:
        if expected not in provenance_text:
            raise AgentRuntimeError(
                "image provenance missing executed probe evidence {!r}: "
                "refusing to serve unvalidated generation".format(expected)
            )
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


#: Shell-executable syntax that cached dependency content must never carry:
#: command substitution, backticks, pipes, chaining, or newlines would
#: execute if a consumer sourced or evaluated the payload.
_CACHE_EXECUTABLE_RE = re.compile(r"(\$|`|&|[;|<>]|\n|\r|\0)")


def _validate_cache_payload(payload: Any, _depth: int = 0) -> bool:
    """Validate executable cache content before use (shape + no shell code)."""

    if _depth > 8:
        return False
    if isinstance(payload, dict):
        for cache_key, value in payload.items():
            if not isinstance(cache_key, str) or not cache_key or len(cache_key) > 256:
                return False
            if not _validate_cache_payload(value, _depth + 1):
                return False
        bound_digest = payload.get("image_digest")
        if bound_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(bound_digest or "")):
            return False
        bound_profile = payload.get("profile_digest")
        if bound_profile is not None and not re.fullmatch(r"[0-9a-f]{64}", str(bound_profile or "")):
            return False
        return True
    if isinstance(payload, (list, tuple)):
        if len(payload) > 1024:
            return False
        return all(_validate_cache_payload(item, _depth + 1) for item in payload)
    if payload is None or isinstance(payload, (bool, int, float)):
        return True
    if isinstance(payload, str):
        if len(payload) > 4096:
            return False
        return _CACHE_EXECUTABLE_RE.search(payload) is None
    return False


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
        only non-None can never consume poisoned cache, the stored entry
        must bind to the requested key/digest (a mismatched, corrupt, or
        transplanted entry falls back to deterministic reconstruction),
        executable cache content is validated before use, and any
        validation failure falls back to deterministic reconstruction
        (None).
        """

        entry = self._entries.get((project_id, repository, key))
        if entry is None:
            return None
        if entry.project_id != project_id or entry.repository != repository:
            return None
        if not entry.trusted:
            return None
        # The payload must bind to the requested key: the stored key must
        # equal the requested key, the requested key must wrap a valid
        # immutable digest, and a payload carrying its own digest binding
        # must agree with it. Anything else is a mismatched/corrupt entry
        # and reconstructs deterministically instead.
        if entry.key != key:
            return None
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]{0,31})-([0-9a-f]{64})", str(key or "")
        )
        if match is None:
            return None
        digest = match.group(2)
        payload = entry.payload
        if not isinstance(payload, dict):
            return None
        bound = payload.get("image_digest")
        if bound is not None and str(bound) != digest:
            return None
        if image_contains_secret(payload) is not None:
            return None
        if not _validate_cache_payload(payload):
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
    jit_config: Optional[str] = None
    destroyed_at: Optional[float] = None
    network_id: Optional[str] = None
    public_ip: Optional[str] = None
    log_forwarded: bool = False
    # Durable cloud identity (e.g. EC2 InstanceId or GCE/Azure resource
    # name). Stored on the instance so teardown survives a process restart
    # that clears the in-memory local-id -> provider-id map; provider_tags
    # also carries it for tag-based discovery without process memory.
    provider_resource_id: Optional[str] = None


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
    provisioning_timeout_seconds: float = DEFAULT_PROVISIONING_TIMEOUT_SECONDS


def _lease_provisioning_timeout(lease: Lease, default: float) -> float:
    """Provisioning timeout governing one lease.

    Leases created by ``run_next_job`` carry the queued profile's
    ``provisioning_timeout_seconds``; leases built by hand (or before the
    profile-carried timeout existed) fall back to the controller default so
    expiry is still evaluated instead of waiting indefinitely.
    """

    try:
        timeout = float(getattr(lease, "provisioning_timeout_seconds", default))
    except (TypeError, ValueError):
        return float(default)
    if not timeout > 0:
        return float(default)
    return timeout


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

    def create_network(
        self,
        profile_digest: str,
        job_id: str,
        now: float,
        persistent: bool = False,
    ) -> NetworkAttachment:
        network = NetworkAttachment(
            id=self._next_id("net"),
            profile_digest=profile_digest,
            job_id=job_id,
            created_at=now,
            persistent=bool(persistent),
        )
        self.networks[network.id] = network
        if network.persistent:
            # A deliberately persisted attachment is recorded as persistent
            # egress so lifecycle qualification can fail on it: the
            # no-persistent-egress check below is only meaningful because
            # this path can populate the list.
            self.persistent_egress_objects.append(network.id)
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
        self.prune_destroyed_resources()
        return True

    def delete_network(self, network: NetworkAttachment, now: float) -> bool:
        """Idempotent per-job network teardown (local bookkeeping).

        Cloud-backed subclasses override this to delete the remote
        firewall/security-group/NSG first, then delegate here for the
        local mark. Keeping the local mark in one place lets the
        provisioning failure path and the orphan sweep reclaim remote
        attachments through the same entry point instead of setting
        ``destroyed_at`` directly and leaking the remote rule.
        """

        stored = self.networks.get(network.id)
        target = stored if stored is not None else network
        if target.destroyed_at is not None:
            return True
        target.destroyed_at = now
        if stored is not None and stored is not network:
            try:
                network.destroyed_at = now
            except Exception:
                pass
        return True

    def prune_destroyed_resources(self, max_retained_destroyed: int = 256) -> int:
        """Prune oldest destroyed instances/networks beyond a bounded history.

        Destroyed resources are retained for post-job assertions, but a
        long-lived controller must not grow linearly in memory. Only the
        most recent ``max_retained_destroyed`` destroyed entries per
        collection are kept; live resources are never pruned.
        """

        pruned = 0
        for collection in (self.instances, self.networks):
            destroyed = sorted(
                (key for key, item in collection.items() if item.destroyed_at is not None),
                key=lambda key: collection[key].destroyed_at or 0.0,
            )
            excess = len(destroyed) - int(max_retained_destroyed)
            for key in destroyed[: max(0, excess)]:
                del collection[key]
                pruned += 1
        return pruned

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


#: Generic compute-provider contract behind ``EphemeralController``.
#:
#: Any object implementing ``create_network`` / ``create_instance`` /
#: ``jit_register`` / ``jit_deregister`` / ``destroy`` / ``live_instances``
#: / ``provider_tags`` plus the ``instances`` / ``networks`` /
#: ``persistent_egress_objects`` collections can back a controller.
#: ``FakeProvider`` is the deterministic in-memory member; the concrete
#: providers below implement the same contract against real infrastructure
#: using only the standard library (urllib/subprocess) so consumer projects
#: have a path to real compute, GitHub JIT registration, and per-job
#: networks without extra dependencies.
RunnerProvider = Any


def _provider_github_token(explicit: Optional[str] = None) -> Optional[str]:
    """Return the GitHub credential for real JIT calls (never a paid key).

    Reads only the ``TAP_PAT`` repository secret (falling back to the
    ephemeral ``GITHUB_TOKEN``). Paid provider keys are never consulted:
    see ``FORBIDDEN_PROVIDER_KEYS``.
    """

    if explicit:
        return explicit
    for name in ("TAP_PAT", "GITHUB_TOKEN"):
        try:
            value = os.environ.get(name, "")
        except Exception:
            value = ""
        if value and str(value).strip():
            return str(value).strip()
    return None


def _github_api_post_json(url: str, token: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """POST a JSON payload to the GitHub REST API (stdlib only)."""

    import urllib.request
    import urllib.error

    body = json.dumps(dict(payload)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer {}".format(token),
            "Content-Type": "application/json",
            "User-Agent": "continuum-ephemeral-controller",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise AgentRuntimeError(
            "GitHub JIT registration failed with HTTP {}: {}".format(exc.code, detail)
        )
    except Exception as exc:
        raise AgentRuntimeError("GitHub JIT registration could not be reached: {}".format(exc))
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        raise AgentRuntimeError("GitHub JIT registration returned non-JSON output")
    if not isinstance(parsed, dict):
        raise AgentRuntimeError("GitHub JIT registration returned an unexpected payload")
    return parsed


def _github_api_delete(url: str, token: str) -> None:
    """DELETE a GitHub REST resource (stdlib only); missing token fails closed."""

    import urllib.request
    import urllib.error

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer {}".format(token),
            "User-Agent": "continuum-ephemeral-controller",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise AgentRuntimeError("GitHub runner deregistration failed with HTTP {}".format(exc.code))
    except Exception as exc:
        raise AgentRuntimeError("GitHub runner deregistration could not be reached: {}".format(exc))


class GitHubHostedProvider(FakeProvider):
    """Real GitHub-backed provider: JIT-register ephemeral runners via GitHub.

    Compute itself is provisioned by GitHub Actions on demand; this provider
    supplies the real JIT-registration path (``generate-jitconfig``) against
    the consumer repository using ``TAP_PAT``/``GITHUB_TOKEN``, tracks the
    per-job network attachment as a non-persistent logical attachment (there
    is no consumer VPC to create on GitHub-hosted), and deregisters the
    runner on teardown. Missing credentials fail explicitly instead of
    passing as a silent no-op.
    """

    def __init__(self, *, api_base: str = "https://api.github.com", token: Optional[str] = None) -> None:
        super().__init__()
        self.api_base = str(api_base or "https://api.github.com").rstrip("/")
        self._explicit_token = token

    @property
    def backend(self) -> str:
        return "github-hosted"

    def jit_register(self, instance: RunnerInstance) -> str:
        token = _provider_github_token(self._explicit_token)
        if not token:
            raise AgentRuntimeError(
                "github-hosted JIT registration requires TAP_PAT (or GITHUB_TOKEN): "
                "refusing to record a local-only registration as real"
            )
        repository = instance.repository
        if not repository or "/" not in repository:
            raise AgentRuntimeError(
                "github-hosted JIT registration requires repository 'owner/repo', got {!r}".format(repository)
            )
        url = "{}/repos/{}/actions/runners/generate-jitconfig".format(self.api_base, repository)
        payload = {
            "name": instance.id,
            "runner_group_id": 1,
            "labels": ["continuum-ephemeral", "continuum-{}".format(instance.job_id or "job")],
            "work_folder": "_work",
        }
        try:
            response = _github_api_post_json(url, token, payload)
        except AgentRuntimeError:
            raise
        runner = response.get("runner") if isinstance(response.get("runner"), dict) else {}
        runner_id = runner.get("id") if isinstance(runner, dict) else None
        jit_id = "jit-{}-{}".format(instance.id, runner_id) if runner_id else "jit-{}".format(instance.id)
        encoded = response.get("encoded_jit_config")
        if not isinstance(encoded, str) or not encoded:
            raise AgentRuntimeError("github-hosted JIT registration returned no encoded_jit_config")
        self.registrations[jit_id] = instance.id
        instance.jit_registered = True
        instance.jit_id = jit_id
        instance.jit_config = encoded
        return jit_id

    def jit_deregister(self, instance: RunnerInstance) -> None:
        jit_id = instance.jit_id
        if jit_id is None:
            return
        # Derive the remote runner id only from our own registration id:
        # jit_register stores "jit-<instance.id>-<runner_id>" when GitHub
        # returns a runner id and "jit-<instance.id>" otherwise. Parsing
        # int(parts[-1]) unconditionally mistakes the instance counter
        # suffix (e.g. "jit-i-000001" -> 1) for a runner id and DELETEs an
        # unrelated low-numbered runner. Only a suffix after the exact
        # "jit-<instance.id>-" prefix is a runner id; anything else means
        # no remote id was recorded, so skip the remote DELETE and clear
        # only local state.
        runner_id: Optional[int] = None
        prefix = "jit-{}-".format(instance.id)
        if jit_id.startswith(prefix):
            suffix = jit_id[len(prefix):]
            if suffix.isdigit():
                try:
                    runner_id = int(suffix)
                except (TypeError, ValueError):
                    runner_id = None
        if runner_id is None:
            self.registrations.pop(jit_id, None)
            instance.jit_registered = False
            return
        token = _provider_github_token(self._explicit_token)
        if not (token and instance.repository and "/" in instance.repository):
            raise AgentRuntimeError(
                "github-hosted deregistration for runner {} needs TAP_PAT/GITHUB_TOKEN "
                "and repository 'owner/repo': refusing to drop local registration "
                "while remote teardown is unattempted".format(runner_id))
        url = "{}/repos/{}/actions/runners/{}".format(self.api_base, instance.repository, runner_id)
        # Attempt the remote DELETE before clearing local state, and let
        # failures propagate: swallowing them while popping the
        # registration marks teardown destroyed locally while the remote
        # ephemeral runner stays registered with no record and no retry.
        # _github_api_delete already tolerates 404; any remaining
        # AgentRuntimeError is a real outage and must surface so
        # FakeProvider.destroy never marks the instance destroyed and the
        # controller retries via _teardown/sweep_orphans.
        _github_api_delete(url, token)
        self.registrations.pop(jit_id, None)
        instance.jit_registered = False


class CustomProvider(FakeProvider):
    """Bring-your-own-infrastructure provider behind the generic contract.

    Optional callables receive the same arguments as the corresponding
    ``FakeProvider`` method and may perform real cloud/VM/container work
    (create compute, attach a per-job network, JIT-register, destroy).
    When a hook is omitted the in-memory bookkeeping applies, so consumers
    can adopt one real operation at a time. Hooks must return the same
    shapes (a ``NetworkAttachment`` / ``RunnerInstance`` / JIT id / bool);
    exceptions propagate and the controller requeues demand.
    """

    def __init__(
        self,
        *,
        create_network_fn: Any = None,
        create_instance_fn: Any = None,
        jit_register_fn: Any = None,
        jit_deregister_fn: Any = None,
        destroy_fn: Any = None,
    ) -> None:
        super().__init__()
        self._create_network_fn = create_network_fn
        self._create_instance_fn = create_instance_fn
        self._jit_register_fn = jit_register_fn
        self._jit_deregister_fn = jit_deregister_fn
        self._destroy_fn = destroy_fn

    @property
    def backend(self) -> str:
        return "custom"

    def create_network(
        self,
        profile_digest: str,
        job_id: str,
        now: float,
        persistent: bool = False,
    ) -> NetworkAttachment:
        if self._create_network_fn is None:
            return super().create_network(
                profile_digest=profile_digest, job_id=job_id, now=now, persistent=persistent
            )
        network = self._create_network_fn(
            profile_digest=profile_digest, job_id=job_id, now=now, persistent=persistent
        )
        if not isinstance(network, NetworkAttachment):
            raise AgentRuntimeError("custom create_network hook must return a NetworkAttachment")
        if network.persistent:
            raise AgentRuntimeError("per-job network attachment must not be persistent")
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
        if self._create_instance_fn is None:
            return super().create_instance(
                project_id=project_id, repository=repository, profile_digest=profile_digest,
                digest=digest, job_id=job_id, network_id=network_id, now=now,
            )
        instance = self._create_instance_fn(
            project_id=project_id, repository=repository, profile_digest=profile_digest,
            digest=digest, job_id=job_id, network_id=network_id, now=now,
        )
        if not isinstance(instance, RunnerInstance):
            raise AgentRuntimeError("custom create_instance hook must return a RunnerInstance")
        self.instances[instance.id] = instance
        return instance

    def jit_register(self, instance: RunnerInstance) -> str:
        if self._jit_register_fn is None:
            return super().jit_register(instance)
        jit_id = self._jit_register_fn(instance)
        jit_id = str(jit_id)
        self.registrations[jit_id] = instance.id
        instance.jit_registered = True
        instance.jit_id = jit_id
        return jit_id

    def jit_deregister(self, instance: RunnerInstance) -> None:
        if self._jit_deregister_fn is None:
            return super().jit_deregister(instance)
        self._jit_deregister_fn(instance)
        if instance.jit_id is not None:
            self.registrations.pop(instance.jit_id, None)
            instance.jit_registered = False

    def destroy(self, instance: RunnerInstance, now: float) -> bool:
        if self._destroy_fn is None:
            return super().destroy(instance, now)
        destroyed = bool(self._destroy_fn(instance, now))
        if not destroyed:
            return False
        if instance.destroyed_at is not None:
            return True
        self.jit_deregister(instance)
        network = self.networks.get(instance.network_id or "")
        if network is not None and network.destroyed_at is None:
            network.destroyed_at = now
        instance.log_forwarded = True
        instance.destroyed_at = now
        self.prune_destroyed_resources()
        return True


def _sanitize_cloud_resource_token(value: str, fallback: str = "job") -> str:
    """Normalize one free-form token into a cloud-safe name fragment."""

    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9-]", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return fallback
    return text


def _cloud_resource_name(job_id: str, profile_digest: str) -> str:
    """Build a CLI-safe per-job resource name (GCE/EC2/Azure compatible).

    Raw job ids may carry slashes, spaces, uppercase, or excessive length;
    embedding them verbatim fails CLI validation or collides. Normalize to
    lowercase alphanumeric plus hyphens, bound the total to the 63-char GCE
    limit, and keep the profile slice so distinct profiles do not collide.
    """

    job = _sanitize_cloud_resource_token(job_id)
    digest = _sanitize_cloud_resource_token(str(profile_digest or "")[:8], fallback="profile")
    max_job = 63 - len("continuum--") - 8
    if len(job) > max_job:
        job = job[:max_job].rstrip("-") or "job"
    name = "continuum-{}-{}".format(job, digest[:8])
    if not name[0].isalpha():
        name = "c" + name[1:]
    return name[:63].strip("-") or "continuum-job-profile"


class CloudCliProvider(FakeProvider):
    """Real VM-backed provider that shells out to the cloud CLI (no SDK).

    ``backend`` is one of ``gce`` / ``ec2`` / ``azure``. Compute creation,
    per-job firewall/network rules, and teardown invoke ``gcloud`` /
    ``aws`` / ``az`` with no shell, tagged per job for orphan discovery via
    ``provider_tags``. A missing CLI or missing cloud configuration fails
    explicitly; it never records a local-only instance as real compute.
    """

    _CLI_BY_BACKEND = {"gce": "gcloud", "ec2": "aws", "azure": "az"}

    def __init__(
        self,
        backend: str,
        *,
        project: str = "",
        region: str = "",
        extra_args: Optional[Sequence[str]] = None,
    ) -> None:
        if backend not in ("gce", "ec2", "azure"):
            raise AgentRuntimeError("unsupported cloud backend {!r}".format(backend))
        super().__init__()
        self._backend = backend
        self.project = str(project or "")
        self.region = str(region or "")
        self.extra_args: Tuple[str, ...] = tuple(extra_args or ())
        # Local instance id -> real provider identity (EC2 InstanceId or
        # GCE/Azure resource name). Teardown must address real compute, never
        # the local "i-000001" key.
        self._provider_instance_ids: Dict[str, str] = {}

    @property
    def backend(self) -> str:
        return self._backend

    def _require_cli(self) -> str:
        binary = self._CLI_BY_BACKEND[self._backend]
        resolved = shutil.which(binary)
        if not resolved:
            raise AgentRuntimeError(
                "{} provider requires the {!r} CLI on PATH: refusing to fake real compute".format(
                    self._backend, binary
                )
            )
        return resolved

    def _run_cli(self, argv: List[str]) -> str:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, shell=False)
        output = "{}{}".format(completed.stdout or "", completed.stderr or "")
        if completed.returncode != 0:
            raise AgentRuntimeError(
                "{} provider CLI failed (exit {}): {}".format(self._backend, completed.returncode, output[:500])
            )
        return output

    def create_network(
        self,
        profile_digest: str,
        job_id: str,
        now: float,
        persistent: bool = False,
    ) -> NetworkAttachment:
        if persistent:
            raise AgentRuntimeError("per-job network attachment must not be persistent")
        cli = self._require_cli()
        name = _cloud_resource_name(job_id, profile_digest)
        tags = "continuum-job={}".format(name)
        if self._backend == "gce":
            self._run_cli([cli, "compute", "firewall-rules", "create",
                            name,
                            "--allow=tcp:22", "--description={}".format(tags)] + list(self.extra_args))
        elif self._backend == "ec2":
            self._run_cli([cli, "ec2", "create-security-group",
                            "--group-name", name,
                            "--description", tags] + list(self.extra_args))
        else:
            self._run_cli([cli, "network", "nsg", "create",
                            "--name", name,
                            "--tags", tags] + list(self.extra_args))
        return super().create_network(
            profile_digest=profile_digest, job_id=job_id, now=now, persistent=False
        )

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
        cli = self._require_cli()
        if not self.project:
            raise AgentRuntimeError(
                "{} provider requires an explicit project/account: refusing to fake real compute".format(self._backend)
            )
        name = _cloud_resource_name(job_id, profile_digest)
        real_id = ""
        if self._backend == "gce":
            self._run_cli([cli, "compute", "instances", "create", name,
                            "--project", self.project] + list(self.extra_args))
            real_id = name
        elif self._backend == "ec2":
            output = self._run_cli([cli, "ec2", "run-instances", "--tag-specifications",
                            "ResourceType=instance,Tags=[{Key=continuum-job,Value=" + name + "}]"]
                           + list(self.extra_args))
            try:
                parsed = json.loads(output or "{}")
                instances = parsed.get("Instances") if isinstance(parsed, dict) else None
                first = instances[0] if isinstance(instances, list) and instances else {}
                if isinstance(first, dict):
                    real_id = str(first.get("InstanceId") or "")
            except Exception:
                real_id = ""
            if not real_id:
                match = re.search(r"\bi-[0-9a-fA-F]{8,17}\b", output or "")
                real_id = match.group(0) if match else ""
            if not real_id:
                raise AgentRuntimeError("ec2 provider returned no InstanceId: refusing to fake real compute")
        else:
            self._run_cli([cli, "vm", "create", "--name", name,
                            "--resource-group", self.project] + list(self.extra_args))
            real_id = name
        instance = super().create_instance(
            project_id=project_id, repository=repository, profile_digest=profile_digest,
            digest=digest, job_id=job_id, network_id=network_id, now=now,
        )
        self._provider_instance_ids[instance.id] = real_id
        # Persist the real cloud identity on the durable instance record
        # (and therefore in provider_tags) so teardown after a process
        # restart with a fresh in-memory map can still address real
        # compute instead of raising with no InstanceId.
        try:
            instance.provider_resource_id = real_id
        except Exception:
            pass
        return instance

    def provider_tags(self, instance: RunnerInstance) -> Dict[str, str]:
        tags = super().provider_tags(instance)
        durable = getattr(instance, "provider_resource_id", None) or self._provider_instance_ids.get(instance.id, "")
        if durable:
            tags["continuum-provider-id"] = str(durable)
        return tags

    def _resolve_ec2_instance_id(self, cli: str, name: str, instance: RunnerInstance) -> str:
        """Resolve the real EC2 InstanceId from durable state, then the cloud.

        Preference order: in-memory map (fast path), durable instance
        record (survives restarts), live cloud discovery by the
        ``continuum-job`` tag (reclaims instances created before the
        durable record existed). An empty string means the cloud reports
        no such instance, which the caller treats as already gone.
        """

        real_id = self._provider_instance_ids.get(instance.id, "")
        if real_id:
            return real_id
        durable = str(getattr(instance, "provider_resource_id", "") or "")
        if durable:
            self._provider_instance_ids[instance.id] = durable
            return durable
        try:
            output = self._run_cli([cli, "ec2", "describe-instances", "--filters",
                                    "Name=tag:continuum-job,Values={}".format(name)])
        except AgentRuntimeError as exc:
            text = str(exc).lower()
            if "not found" in text or "does not exist" in text or "invalidinstanceid" in text:
                return ""
            raise
        try:
            parsed = json.loads(output or "{}")
        except Exception:
            parsed = None
        found = ""
        if isinstance(parsed, dict):
            for reservation in parsed.get("Reservations") or []:
                if not isinstance(reservation, dict):
                    continue
                for entry in reservation.get("Instances") or []:
                    if isinstance(entry, dict) and entry.get("InstanceId"):
                        found = str(entry.get("InstanceId"))
                        break
                if found:
                    break
        if not found:
            match = re.search(r"\bi-[0-9a-fA-F]{8,17}\b", output or "")
            found = match.group(0) if match else ""
        if found:
            self._provider_instance_ids[instance.id] = found
            try:
                instance.provider_resource_id = found
            except Exception:
                pass
        return found

    def _delete_remote_network_rule(self, cli: str, name: str) -> None:
        if self._backend == "gce":
            self._run_cli([cli, "compute", "firewall-rules", "delete", name, "--quiet"])
        elif self._backend == "ec2":
            self._run_cli([cli, "ec2", "delete-security-group", "--group-name", name])
        else:
            self._run_cli([cli, "network", "nsg", "delete", "--name", name,
                            "--resource-group", self.project, "--yes"])

    def delete_network(self, network: NetworkAttachment, now: float) -> bool:
        """Delete the remote per-job firewall/SG/NSG, then mark it locally."""

        stored = self.networks.get(network.id)
        target = stored if stored is not None else network
        if target.destroyed_at is not None:
            return True
        cli = self._require_cli()
        name = _cloud_resource_name(target.job_id or "", target.profile_digest or "")
        try:
            self._delete_remote_network_rule(cli, name)
        except AgentRuntimeError as exc:
            text = str(exc).lower()
            if "not found" not in text and "does not exist" not in text:
                raise
        return FakeProvider.delete_network(self, target, now)

    def destroy(self, instance: RunnerInstance, now: float) -> bool:
        if instance.destroyed_at is not None:
            return True
        cli = self._require_cli()
        name = _cloud_resource_name(instance.job_id or instance.id, instance.profile_digest)
        try:
            if self._backend == "gce":
                self._run_cli([cli, "compute", "instances", "delete", name, "--quiet"])
            elif self._backend == "ec2":
                real_id = self._resolve_ec2_instance_id(cli, name, instance)
                if real_id:
                    self._run_cli([cli, "ec2", "terminate-instances", "--instance-ids", real_id])
            else:
                self._run_cli([cli, "vm", "delete", "--name", name, "--yes"])
        except AgentRuntimeError as exc:
            text = str(exc).lower()
            if "not found" not in text and "does not exist" not in text and "invalidinstanceid" not in text:
                raise
        try:
            self._delete_remote_network_rule(cli, name)
        except AgentRuntimeError as exc:
            text = str(exc).lower()
            if "not found" not in text and "does not exist" not in text:
                raise
        destroyed = super().destroy(instance, now)
        if destroyed:
            self._provider_instance_ids.pop(instance.id, None)
        return destroyed


def create_provider(backend: str, **kwargs: Any) -> FakeProvider:
    """Create the real provider for a declared ``SUPPORTED_PROVIDERS`` backend.

    ``github-hosted`` returns a GitHub JIT provider, ``custom`` returns a
    hook-backed provider, and ``gce`` / ``ec2`` / ``azure`` return CLI-backed
    providers that create real compute and per-job networks. Unknown
    backends raise; cloud backends with a missing CLI or missing project
    fail explicitly at use time, never as a silent green no-op. No paid
    provider key is read or required.
    """

    if backend not in SUPPORTED_PROVIDERS:
        raise AgentRuntimeError("unsupported provider backend {!r}".format(backend))
    if backend == "github-hosted":
        return GitHubHostedProvider(**kwargs)
    if backend == "custom":
        return CustomProvider(**kwargs)
    return CloudCliProvider(backend, **kwargs)


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

    ``provider`` is any object behind the generic compute contract
    (``FakeProvider`` for deterministic tests, or ``GitHubHostedProvider`` /
    ``CloudCliProvider`` / ``CustomProvider`` via ``create_provider`` for
    real compute, GitHub JIT registration, and per-job networks).
    """

    def __init__(
        self,
        *,
        project_id: str,
        image_store: ImageStore,
        provider: Any,
        cache: Optional[DependencyCache] = None,
        provisioning_timeout_seconds: float = DEFAULT_PROVISIONING_TIMEOUT_SECONDS,
        max_job_lifetime_seconds: float = DEFAULT_MAX_JOB_LIFETIME_SECONDS,
        teardown_timeout_seconds: float = DEFAULT_TEARDOWN_TIMEOUT_SECONDS,
        orphan_grace_seconds: float = DEFAULT_ORPHAN_GRACE_SECONDS,
        global_max_age_seconds: float = DEFAULT_GLOBAL_MAX_INSTANCE_AGE_SECONDS,
        retry_limit: int = DEFAULT_TEARDOWN_RETRY_LIMIT,
        probe_executor: Any = None,
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
        # Probe runner override for image builds on hosts without the
        # runtime binaries on PATH. Per-call executors win over this
        # controller-level default, which wins over the store-level
        # default, which wins over the real subprocess executor.
        self.probe_executor = probe_executor
        self.leases: Dict[str, Lease] = {}
        self.queued: List[Dict[str, Any]] = []
        self.diagnostics: List[str] = []
        self._job_counter = 0
        # Serializes queue/lease bookkeeping so concurrent callers cannot
        # both claim the same demand or both pass the concurrency-limit
        # check and over-provision beyond the consumer cost cap. Only the
        # short claim/bookkeeping sections hold this lock: image
        # ensure/validate, cloud CLI calls, and JIT registration run
        # outside it, with an in-flight reservation covering the gap so
        # queue operations never block for a full provisioning duration.
        self._provisioning_lock = threading.Lock()
        self._provisioning_inflight = 0

    # -- demand ------------------------------------------------------
    def queue_job(self, repository: str, profile: RuntimeProfile, run_id: str = "") -> str:
        """Queue one job. No compute is created by queueing alone."""

        with self._provisioning_lock:
            self._job_counter += 1
            job_id = "job-{:06d}".format(self._job_counter)
            self.queued.append(
                {"job_id": job_id, "repository": repository, "profile": profile, "run_id": run_id or job_id}
            )
            self.diagnostics.append("queued {} for {}".format(job_id, repository))
            return job_id

    def live_idle_count(self, now: Optional[float] = None) -> int:
        """Live instances not bound to a live job lease (idle/orphan compute).

        A lease past ``lease_expires_at``/``max_age_at`` (or past the
        provisioning timeout while still provisioning) is awaiting the
        orphan sweep, not actively serving a job: its compute is idle
        leaked compute and must count as idle so the metric cannot report
        zero idle while live compute leaks until reconciliation runs. When
        ``now`` is omitted the current clock applies, so expiry is still
        evaluated and callers cannot get a vacuous zero-idle pass from an
        unswept expired lease.
        """

        clock = time.time() if now is None else float(now)
        live_leased = set()
        for instance_id, lease in self.leases.items():
            expired = False
            if clock >= lease.max_age_at or clock >= lease.lease_expires_at:
                expired = True
            elif lease.state == "provisioning" and clock - lease.created_at >= _lease_provisioning_timeout(lease, self.provisioning_timeout):
                expired = True
            if not expired:
                live_leased.add(instance_id)
        return sum(1 for item in self.provider.live_instances() if item.id not in live_leased)

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
        probe_executor: Any = None,
    ) -> JobResult:
        """Execute steps 2-13 of the required provisioning path for one job.

        Only short queue/limit-claim and bookkeeping sections hold the
        provisioning lock: image ensure/validate, cloud CLI calls, and JIT
        registration run outside it. An in-flight reservation covers the
        gap so concurrent callers cannot both pass the concurrency-limit
        check and over-provision, without serializing slow provisioning
        across workers. ``probe_executor`` overrides the image-probe
        runner for this job (falling back to the controller-level and
        store-level defaults).
        """

        with self._provisioning_lock:
            if not self.queued:
                raise AgentRuntimeError("no queued demand: the controller never provisions without demand")
            if outcome not in TERMINAL_OUTCOMES:
                raise AgentRuntimeError("unknown job outcome {!r}: refusing to record success".format(outcome))
            peeked = self.queued[0]
            peek_profile: RuntimeProfile = peeked["profile"]
            try:
                concurrency_limit = int(peek_profile.concurrency_limit)
            except (TypeError, ValueError):
                raise AgentRuntimeError("concurrency_limit must be an integer between 1 and 256")
            live = len(self.provider.live_instances())
            if live + self._provisioning_inflight >= concurrency_limit:
                raise AgentRuntimeError(
                    "concurrency limit {} reached ({} live instances): refusing to provision {}".format(
                        concurrency_limit, live, peeked.get("job_id", "?")
                    )
                )
            pending = self.queued.pop(0)
            self._provisioning_inflight += 1
        try:
            return self._run_claimed_job(
                pending,
                manifest,
                now=now,
                outcome=outcome,
                fail_jit=fail_jit,
                fail_startup=fail_startup,
                teardown_retries=teardown_retries,
                is_fork=is_fork,
                trusted_context=trusted_context,
                probe_executor=probe_executor,
            )
        finally:
            with self._provisioning_lock:
                self._provisioning_inflight = max(0, self._provisioning_inflight - 1)

    def _run_next_job_impl(
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
        probe_executor: Any = None,
    ) -> JobResult:
        """Execute steps 2-13 of the required provisioning path for one job."""

        with self._provisioning_lock:
            if not self.queued:
                raise AgentRuntimeError("no queued demand: the controller never provisions without demand")
            pending_claim = self.queued.pop(0)
            self._provisioning_inflight += 1
        try:
            return self._run_claimed_job(
                pending_claim,
                manifest,
                now=now,
                outcome=outcome,
                fail_jit=fail_jit,
                fail_startup=fail_startup,
                teardown_retries=teardown_retries,
                is_fork=is_fork,
                trusted_context=trusted_context,
                probe_executor=probe_executor,
            )
        finally:
            with self._provisioning_lock:
                self._provisioning_inflight = max(0, self._provisioning_inflight - 1)

    def _run_claimed_job(
        self,
        pending: Dict[str, Any],
        manifest: AgentManifest,
        *,
        now: float,
        outcome: str = OUTCOME_SUCCESS,
        fail_jit: bool = False,
        fail_startup: bool = False,
        teardown_retries: Optional[int] = None,
        is_fork: bool = False,
        trusted_context: bool = True,
        probe_executor: Any = None,
    ) -> JobResult:
        """Run one already-claimed queued entry (lock held only for bookkeeping)."""

        def _requeue_claim() -> None:
            with self._provisioning_lock:
                if pending not in self.queued:
                    self.queued.insert(0, pending)

        if outcome not in TERMINAL_OUTCOMES:
            _requeue_claim()
            raise AgentRuntimeError("unknown job outcome {!r}: refusing to record success".format(outcome))
        profile: RuntimeProfile = pending["profile"]
        job_id: str = pending["job_id"]
        repository: str = pending["repository"]
        run_id: str = pending["run_id"]
        events: List[str] = ["queued {}".format(job_id)]

        # Consumer-owned concurrency/cost limit: re-checked here from the
        # claimed entry so a direct _run_claimed_job/_run_next_job_impl
        # caller (and the pre-claim check in run_next_job) never
        # provisions beyond the profile's concurrency_limit live
        # instances. Refused demand is requeued at the front so it is
        # retried later instead of being silently dropped.
        try:
            concurrency_limit = int(profile.concurrency_limit)
        except (TypeError, ValueError):
            _requeue_claim()
            raise AgentRuntimeError("concurrency_limit must be an integer between 1 and 256")
        live = len(self.provider.live_instances())
        if live >= concurrency_limit:
            _requeue_claim()
            raise AgentRuntimeError(
                "concurrency limit {} reached ({} live instances): refusing to provision {}".format(
                    concurrency_limit, live, job_id
                )
            )

        digest = image_digest(manifest, profile)
        # Promotion/rollback gate execution: each runtime profile keeps its
        # own active digest. A queued job requesting any other digest for
        # its profile must wait for an explicit promotion instead of
        # building/validating and running unpromoted. The claimed demand
        # is requeued at the front so the refused job is retried after
        # promotion. Profiles are independent: promoting linux-x64 never
        # blocks macos-arm64.
        active = self.images.active_digest_for(profile)
        if active is not None and digest != active:
            _requeue_claim()
            raise AgentRuntimeError(
                "image digest {} is not the active image {} for profile {!r}: "
                "promote it before serving jobs".format(
                    digest, active, profile_digest(profile)
                )
            )
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
        # Probe runner precedence for this job: per-call override, then
        # the controller default, then the store default, then the real
        # subprocess executor. Without an injection path a host missing
        # the binaries on PATH fails here before any compute is created;
        # the claimed demand is requeued for retry. This slow
        # ensure/validate path runs outside the provisioning lock.
        effective_probe_executor = (
            probe_executor if probe_executor is not None else self.probe_executor
        )
        try:
            generation = self.images.ensure_image(
                manifest, profile, now=now, executor=effective_probe_executor
            )
        except Exception:
            _requeue_claim()
            raise
        events.append("ensured-image {}".format(digest))
        if self.images.active_digest_for(profile) is None:
            self.images.promote(generation.digest, now=now, profile=profile)
        try:
            validate_image(manifest, profile, generation)
        except Exception:
            _requeue_claim()
            raise
        events.append("validated-identity {}".format(digest))

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
            # Consumer timeout tuning lives on the profile (concurrency is
            # already enforced from it above): the lease must expire from the
            # profile's timeouts, not from the controller-level defaults, or
            # profile timeout tuning has no effect. The global maximum age
            # stays a hard instance-age ceiling: it is never extended by the
            # profile's job lifetime (profiles are capped at the global
            # ceiling in ``resolve_profile``), so a hung or orphaned
            # instance is always reclaimed by the global-max-age sweep.
            try:
                profile_provisioning_timeout = float(profile.provisioning_timeout_seconds)
            except (TypeError, ValueError):
                profile_provisioning_timeout = self.provisioning_timeout
            try:
                profile_max_lifetime = float(profile.max_job_lifetime_seconds)
            except (TypeError, ValueError):
                profile_max_lifetime = self.max_job_lifetime
            if not profile_provisioning_timeout > 0:
                profile_provisioning_timeout = self.provisioning_timeout
            if not profile_max_lifetime > 0:
                profile_max_lifetime = self.max_job_lifetime
            lease = Lease(
                project_id=self.project_id,
                repository=repository,
                job_id=job_id,
                run_id=run_id,
                profile_digest=profile_digest(profile),
                instance_id=instance.id,
                created_at=now,
                lease_expires_at=now + profile_max_lifetime,
                max_age_at=now + self.global_max_age,
                state="provisioning",
                provisioning_timeout_seconds=profile_provisioning_timeout,
            )
            with self._provisioning_lock:
                self.leases[instance.id] = lease
            if fail_jit or outcome == OUTCOME_JIT_FAILURE:
                events.append("jit-registration-failed {}".format(instance.id))
                destroyed = self._teardown(instance, now=now, retries=teardown_retries)
                if destroyed:
                    with self._provisioning_lock:
                        self.leases.pop(instance.id, None)
                else:
                    events.append("teardown-deferred-to-reconciler {}".format(instance.id))
                assert network is not None and instance is not None
                return JobResult(job_id, OUTCOME_JIT_FAILURE, instance.id, network.id,
                                 instance.public_ip, digest, destroyed, events)
            jit_id = self.provider.jit_register(instance)
            events.append("jit-registered {}".format(jit_id))
        except Exception:
            # Provisioning failed before the job could run: destroy any
            # partially created resources with the same bounded-retry
            # teardown used on every other path (a single destroy attempt
            # leaves a live instance behind on a transient failure),
            # requeue the demand at the front so no queued work is lost,
            # then re-raise.
            if instance is not None:
                try:
                    destroyed = self._teardown(instance, now=now, retries=teardown_retries)
                    if not destroyed:
                        events.append("teardown-deferred-to-reconciler {}".format(instance.id))
                        with self._provisioning_lock:
                            self.diagnostics.append(
                                "teardown-deferred-to-reconciler {}".format(instance.id)
                            )
                finally:
                    with self._provisioning_lock:
                        self.leases.pop(instance.id, None)
            elif network is not None:
                # Network-only failure (create_instance raised after
                # create_network succeeded): tear down the remote
                # firewall/SG/NSG through the provider instead of only
                # marking local state, or the real per-job network leaks.
                deleter = getattr(self.provider, "delete_network", None)
                if callable(deleter):
                    try:
                        deleter(network, now)
                    except Exception as cleanup_exc:
                        with self._provisioning_lock:
                            self.diagnostics.append(
                                "network-cleanup-failed {}: {}".format(network.id, cleanup_exc)
                            )
                else:
                    try:
                        networks = getattr(self.provider, "networks", {})
                        stored = networks.get(network.id)
                    except Exception:
                        stored = None
                    if stored is not None and stored.destroyed_at is None:
                        stored.destroyed_at = now
            _requeue_claim()
            raise

        assert network is not None and instance is not None
        if fail_startup or outcome == OUTCOME_STARTUP_FAILURE:
            events.append("job-startup-failed {}".format(instance.id))
            destroyed = self._teardown(instance, now=now, retries=teardown_retries)
            with self._provisioning_lock:
                self.leases.pop(instance.id, None)
            if not destroyed:
                events.append("teardown-deferred-to-reconciler {}".format(instance.id))
            return JobResult(job_id, OUTCOME_STARTUP_FAILURE, instance.id, network.id,
                             instance.public_ip, digest, destroyed, events)

        self._execute_one_job(instance, lease, now=now)
        events.append("executed-one-job {} uses={}".format(instance.id, instance.uses))

        # Unknown outcomes must never coerce to success: a typo (`succes`)
        # or a new caller outcome (`timeout`) recorded as success would
        # publish dependency cache and mask the failure. Fail closed.
        if outcome not in TERMINAL_OUTCOMES:
            raise AgentRuntimeError("unknown job outcome {!r}: refusing to record success".format(outcome))
        terminal = outcome
        events.append("terminal {}".format(terminal))
        destroyed = self._teardown(instance, now=now, retries=teardown_retries)
        with self._provisioning_lock:
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
        # code on a frozen timestamp. Runs outside the provisioning lock
        # so slow cloud CLI teardown never blocks queue operations.
        budget = max(1, int(self.teardown_timeout // DEFAULT_TEARDOWN_RETRY_BASE_SECONDS))
        attempts = max(1, min(int(requested), budget))
        deadline = float(now) + float(self.teardown_timeout)
        for attempt in range(attempts):
            effective_now = float(now) + float(attempt) * float(DEFAULT_TEARDOWN_RETRY_BASE_SECONDS)
            if effective_now > deadline:
                break
            try:
                destroyed = bool(self.provider.destroy(instance, now=effective_now))
            except Exception:
                destroyed = False
            if destroyed:
                with self._provisioning_lock:
                    self.diagnostics.append("destroyed {}".format(instance.id))
                return True
        with self._provisioning_lock:
            self.diagnostics.append("teardown-retry-exhausted {}".format(instance.id))
        return False

    # -- reconciliation (section 7) ------------------------------------
    def sweep_orphans(self, now: float) -> List[str]:
        """Scheduled orphan sweep without holding the lock across teardown.

        Candidate selection and bookkeeping hold the provisioning lock
        only briefly; provider destroy/delete calls (cloud CLI, JIT
        deregistration) run outside it so queue operations never block
        for a full sweep duration.
        """

        with self._provisioning_lock:
            try:
                live_snapshot = list(self.provider.live_instances())
            except Exception:
                live_snapshot = []
            try:
                networks_snapshot = list(self.provider.networks.items())
            except Exception:
                networks_snapshot = []
            leases_snapshot = dict(self.leases)
        live_network_ids = {item.network_id for item in live_snapshot if getattr(item, "network_id", None)}
        orphan_networks = [
            (network_id, network)
            for network_id, network in networks_snapshot
            if getattr(network, "destroyed_at", None) is None
            and network_id not in live_network_ids
            and now - getattr(network, "created_at", now) >= self.orphan_grace
        ]
        orphan_instances: List[Tuple[Any, str]] = []
        for instance in live_snapshot:
            lease = leases_snapshot.get(instance.id)
            orphan = False
            reason = ""
            if lease is None:
                if getattr(instance, "jit_registered", False):
                    orphan, reason = True, "stale-jit-without-lease"
                elif now - getattr(instance, "created_at", now) >= self.orphan_grace:
                    orphan, reason = True, "no-lease-past-grace"
            else:
                if now >= lease.max_age_at:
                    orphan, reason = True, "global-max-age"
                elif lease.state == "provisioning" and now - lease.created_at >= _lease_provisioning_timeout(lease, self.provisioning_timeout):
                    orphan, reason = True, "provisioning-timeout"
                elif lease.state == "running" and now >= lease.lease_expires_at:
                    orphan, reason = True, "job-lifetime-exceeded"
            if orphan:
                orphan_instances.append((instance, reason))
        removed: List[str] = []
        # Network-only sweep first: a per-job attachment created before a
        # crash between `create_network` and `create_instance` has no
        # owning instance, so the live-instance loop below would never
        # reclaim it. Route through provider network teardown so real
        # cloud firewall/SG/NSG rules are deleted, not just local state.
        for network_id, network in orphan_networks:
            deleter = getattr(self.provider, "delete_network", None)
            try:
                if callable(deleter):
                    deleter(network, now)
                else:
                    if getattr(network, "destroyed_at", None) is None:
                        network.destroyed_at = now
            except Exception as exc:
                with self._provisioning_lock:
                    self.diagnostics.append(
                        "reconciled-orphan-network-destroy-failed {}: {}".format(network_id, exc))
                continue
            removed.append(network_id)
            with self._provisioning_lock:
                self.diagnostics.append("reconciled-orphan-network {} ({})".format(network_id, network.job_id))
        for instance, reason in orphan_instances:
            try:
                destroyed = bool(self.provider.destroy(instance, now=now))
            except Exception as exc:
                with self._provisioning_lock:
                    self.diagnostics.append(
                        "reconciled-orphan-destroy-failed {} ({}): {}".format(instance.id, reason, exc))
                continue
            if destroyed:
                with self._provisioning_lock:
                    self.leases.pop(instance.id, None)
                removed.append(instance.id)
                with self._provisioning_lock:
                    self.diagnostics.append("reconciled-orphan {} ({})".format(instance.id, reason))
        with self._provisioning_lock:
            try:
                registrations_snapshot = list(self.provider.registrations.items())
            except Exception:
                registrations_snapshot = []
        for jit_id, instance_id in registrations_snapshot:
            with self._provisioning_lock:
                try:
                    instance = self.provider.instances.get(instance_id)
                except Exception:
                    instance = None
                if instance is None or getattr(instance, "destroyed_at", None) is not None:
                    try:
                        self.provider.registrations.pop(jit_id, None)
                    except Exception:
                        pass
                    removed.append(jit_id)
                    self.diagnostics.append("removed-stale-registration {}".format(jit_id))
        return sorted(removed)

    def _sweep_orphans_impl(self, now: float) -> List[str]:
        """Scheduled orphan sweep: destroy expired/over-age/stale resources.

        Covers provisioning timeouts, maximum job lifetime, global maximum
        age, stale JIT registrations with no live lease, and resources that
        survived a controller restart (leases are durable controller state,
        and provider tags allow discovery without process memory).
        """

        return self.sweep_orphans(now)

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
            probe_executor=self.probe_executor,
        )
        restarted.leases = deepcopy(self.leases)
        restarted.queued = deepcopy(self.queued)
        restarted.diagnostics = list(self.diagnostics)
        restarted._job_counter = self._job_counter
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

    def qualify(
        self,
        continuum_ref: str = "main",
        probe_executor: Any = None,
        provider: Any = None,
    ) -> Dict[str, Any]:
        """Deterministic qualification: image identity + ephemeral lifecycle.

        ``probe_executor`` injects build-infra probe evidence so
        qualification can run on hosts without the runtime binaries on
        PATH; ``provider`` injects the compute backend (default
        in-memory) so consumers can qualify against real infrastructure.
        """

        manifest = self.manifest(continuum_ref)
        profile = self.profile(continuum_ref)
        store = ImageStore(
            project_id="qualification-{}".format(self.os),
            probe_executor=probe_executor,
        )
        active_provider = provider if provider is not None else FakeProvider()
        controller = EphemeralController(project_id="qualification-{}".format(self.os),
                                          image_store=store, provider=active_provider,
                                          probe_executor=probe_executor)
        job_id = controller.queue_job("example/qualification", profile, run_id="q1")
        result = controller.run_next_job(manifest, now=1000.0, probe_executor=probe_executor)
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
    "pip install opencode",
    "pipx install pr-agent",
    "pipx install opencode",
    "uv tool install pr-agent",
    "uv tool install opencode",
    "uv pip install pr-agent",
    "uv pip install opencode",
    "cargo install pr-agent",
    "cargo install opencode",
    "npm install opencode",
    "npm i opencode",
    "brew install opencode",
)

#: Executable warm-path probes: a real CLI/version check that proves the
#: prepared runtime is already present. `opencode --version` (exact-version
#: grep) and `command -v pr-agent` are equivalent probes to the two
#: canonical markers, so they also suppress bootstrap detection.
_PROBE_PATTERNS = (
    "command -v opencode",
    "pr-agent --version",
    "opencode --version",
    "command -v pr-agent",
)

#: Signals that make an `if`/`elif`/`case` line a validated cache-miss
#: guard for the installer (the digest gate, the cache-hit flag, or the
#: stamp binding). A conditional on any other predicate -- including an
#: unrelated `command -v foo` / `--version` check -- guards nothing about
#: the prepared runtime and never suppresses bootstrap detection.
_GUARD_MARKERS = (
    "CONTINUUM_IMAGE_DIGEST",
    "CACHE_HIT",
    "CACHE_MISS",
    "STAMP_FILE",
    "image-digest",
)


def _is_bootstrap_line(line: str) -> bool:
    """Whether one code line downloads/installs the agent runtime.

    A bare `releases/download` is not an installer signal on its own: a
    legitimate runner-binary asset download carries no opencode/pr-agent
    reference. It counts only when the same line names the agent runtime
    being probed.
    """

    if any(pattern in line for pattern in _BOOTSTRAP_PATTERNS):
        return True
    return "releases/download" in line and (
        "opencode" in line or "pr-agent" in line
    )


def _code_without_comment(line: str) -> str:
    """Return the code portion of a line with `#` comments stripped.

    Only a `#` outside single/double quotes starts a comment, so a quoted
    `#` (e.g. inside an echo string) is preserved while a trailing comment
    such as ``run: echo ok # see https://opencode.ai/install`` no longer
    counts as an installer reference (and a trailing-comment probe no longer
    counts as a warm-path probe).
    """

    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


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
    # URL must not count as a bootstrap install. Trailing comments are
    # stripped as well: `run: echo ok # see https://opencode.ai/install`
    # is not a bootstrap install, and an installer line carrying only a
    # trailing-comment probe is not a warm path.
    code_lines = [
        _code_without_comment(line)
        for line in workflow_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code_lines = [line for line in code_lines if line.strip()]
    if not code_lines:
        return False
    first_bootstrap: Optional[int] = None
    first_probe: Optional[int] = None
    for index, line in enumerate(code_lines):
        if first_bootstrap is None and _is_bootstrap_line(line):
            first_bootstrap = index
        # Only a real executable probe counts as a warm-path probe: an exact
        # CLI/version check (`command -v opencode`, `pr-agent --version`,
        # `opencode --version`, `command -v pr-agent`). A bare
        # comment-grade marker such as `prepared-runtime` or
        # `prepared agent runtime` in a job name, echo string, or comment is
        # not a probe and must never suppress bootstrap detection.
        if first_probe is None and any(pattern in line for pattern in _PROBE_PATTERNS):
            first_probe = index
        if first_bootstrap is not None and first_probe is not None:
            break
    if first_bootstrap is None:
        return False
    # A prepared-runtime probe (exact-version check) on a code line ahead
    # of the first installer code line is not enough on its own: the
    # installer must be conditionally guarded by a validated cache miss
    # (the warm hit short-circuits with `exit 0`, or the installer sits in
    # the `else` reconstruction branch). A bare probe followed by an
    # unconditional installer reinstalls on every run and still counts as a
    # bootstrap install.
    if first_probe is not None and first_probe < first_bootstrap:
        guarded = any(
            stripped == "exit 0"
            or stripped.startswith("exit 0 ")
            or stripped.startswith("exit 0;")
            or stripped == "else"
            or stripped.startswith("else ")
            or stripped.startswith("else;")
            for stripped in (
                code_lines[index].strip() for index in range(first_probe + 1, first_bootstrap)
            )
        )
        if guarded:
            return False
        # A conditionally guarded reconstruction is also a warm path, not a
        # bootstrap install: e.g. `if [ "$CACHE_HIT" != "true" ]; then
        # curl https://opencode.ai/install | bash; fi` after a probe only
        # reinstalls on a validated cache miss. Only a conditional that
        # references the validated miss signal guards the download: the
        # digest/cache-miss/stamp gate. A bare `if`/`elif`/`case` on an
        # unrelated predicate (e.g. `if [ -f foo ]; then echo hi; fi`, or
        # `if command -v foo; then curl ... | bash; fi` whose guard is
        # unrelated to the image digest/cache-miss) followed by an
        # unconditional installer still reinstalls on every run and counts
        # as a bootstrap install.
        conditional = any(
            (
                stripped.startswith("if ")
                or stripped.startswith("if\t")
                or stripped.startswith("elif ")
                or stripped.startswith("case ")
                or stripped == "case"
            )
            and any(marker in stripped for marker in _GUARD_MARKERS)
            for stripped in (
                code_lines[index].strip() for index in range(first_probe + 1, first_bootstrap + 1)
            )
        )
        if conditional:
            return False
    return True


def workflow_step_has_prepared_runtime_probe(workflow_text: str) -> bool:
    """Whether a workflow probes the prepared runtime before any installer.

    Either executable opencode probe counts: ``command -v opencode`` and
    ``opencode --version`` are equivalent warm-path probes (see
    ``_PROBE_PATTERNS`` and the canonical manifest, whose first probe is
    ``opencode --version``). Requiring only the literal ``command -v``
    form would misclassify a workflow probing via ``opencode --version``
    plus pinned version and digest as unprobed.
    """

    if not isinstance(workflow_text, str):
        return False
    # Compare only code lines (mirrors
    # normal_execution_uses_bootstrap_install): a probe marker inside a
    # `#` comment proves nothing about the warm path, so a comment-only
    # `command -v opencode` or `opencode --version` alongside pinned
    # version/digest strings must not count as a prepared-runtime probe.
    code_lines = [
        _code_without_comment(line)
        for line in workflow_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    code_lines = [code for code in code_lines if code.strip()]
    code_text = "\n".join(code_lines)
    probe = ("command -v opencode" in code_text or "opencode --version" in code_text)
    pinned = OPENCODE_VERSION in workflow_text
    digest = ("image digest" in workflow_text.lower() or "CONTINUUM_IMAGE_DIGEST" in workflow_text
              or "prepared-runtime" in workflow_text.lower() or "prepared agent runtime" in workflow_text.lower())
    if not (probe and pinned and digest):
        return False
    # The warm hit must be keyed by the image digest, not just mention it:
    # require a digest-gated probe (a code line carrying both the
    # CONTINUUM_IMAGE_DIGEST gate and a real executable probe, or a
    # multi-line gate where a digest conditional within a short window of
    # the probe guards the hit path) so an empty or unresolved digest
    # cannot take the hit path and skip install.
    # Comment portions are already stripped above so a trailing-comment
    # probe cannot fake a digest-gated hit.
    gated = any(
        "CONTINUUM_IMAGE_DIGEST" in code and any(pattern in code for pattern in _PROBE_PATTERNS)
        for code in code_lines
    )
    if not gated:
        # Multi-line gate: `if [ -n "$CONTINUUM_IMAGE_DIGEST" ]; then` on
        # one line and `command -v opencode` on a nearby line is the same
        # digest keying as the single-line `&&` form. The gate must precede
        # the probe in the same workflow step within a tight window so an
        # unrelated digest `if` and a distant `command -v opencode` cannot
        # pair up across steps (or in either order) and fake a digest-keyed
        # hit without actually guarding the warm path. A bare digest
        # assignment without a conditional gate never counts.
        _gate_markers = ("=~", "-n", "-z", "==", "!=", "&&", "||", "case ")
        digest_gates = [
            index for index, code in enumerate(code_lines)
            if "CONTINUUM_IMAGE_DIGEST" in code
            and any(marker in code for marker in _gate_markers)
        ]
        probe_lines = [
            index for index, code in enumerate(code_lines)
            if any(pattern in code for pattern in _PROBE_PATTERNS)
        ]
        step_breaks = [
            index for index, code in enumerate(code_lines)
            if code.strip().startswith("- name") or code.strip().startswith("-name")
        ]

        def _same_step(first: int, second: int) -> bool:
            low, high = (first, second) if first <= second else (second, first)
            return not any(low < boundary <= high for boundary in step_breaks)

        gated = any(
            digest_index <= probe_index
            and (probe_index - digest_index) <= 10
            and _same_step(digest_index, probe_index)
            for digest_index in digest_gates
            for probe_index in probe_lines
        )
    if not gated:
        return False
    if "pr-agent==" in code_text:
        if "pr-agent --version" not in code_text:
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
    probe_executor: Any = None,
    provider: Any = None,
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
    treated as failure. ``probe_executor`` injects build-infra probe
    evidence for hosts without the runtime binaries on PATH; ``provider``
    injects the compute backend (default in-memory).
    """

    adapter = next((item for item in ADVERTISED_ADAPTERS if item.os == os and item.arch == arch), None)
    if adapter is None:
        raise AgentRuntimeError("do not advertise unsupported platform profile os={!r} arch={!r}".format(os, arch))
    manifest = adapter.manifest(continuum_ref)
    profile = adapter.profile(continuum_ref)
    store = ImageStore(project_id=project_id, probe_executor=probe_executor)
    active_provider = provider if provider is not None else FakeProvider()
    controller = EphemeralController(
        project_id=project_id, image_store=store, provider=active_provider,
        probe_executor=probe_executor,
    )

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
    first = controller.run_next_job(
        manifest, now=now, outcome=OUTCOME_SUCCESS, probe_executor=probe_executor
    )
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
    second = controller.run_next_job(
        manifest, now=now + 60.0, outcome=OUTCOME_SUCCESS, probe_executor=probe_executor
    )
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
    store.ensure_image(
        stale_manifest, stale_profile, now=now + 120.0, executor=probe_executor
    )
    orphan_net = active_provider.create_network(profile_digest=profile_digest(stale_profile),
                                         job_id=stale_job, now=now + 120.0)
    orphan = active_provider.create_instance(
        project_id=project_id, repository=repository,
        profile_digest=profile_digest(stale_profile), digest=digest,
        job_id=stale_job, network_id=orphan_net.id, now=now + 120.0,
    )
    active_provider.jit_register(orphan)
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
        "networks_created": len(active_provider.networks),
        "live_networks_final": sum(1 for item in active_provider.networks.values() if item.destroyed_at is None),
    })
    networks = list(active_provider.networks.values())
    first_created_event = any(
        "created-instance" in item and "after demand" in item for item in first.events
    )
    first_instance = active_provider.instances.get(first.instance_id)
    first_created_at_ok = (
        first_instance is not None and first_instance.created_at >= now
    )
    # Negative control: the no-persistent-egress check is only meaningful
    # because a deliberately persistent attachment populates
    # persistent_egress_objects (see FakeProvider.create_network and
    # test_persistent_attachment_is_recorded_as_persistent_egress). Prove
    # the detector can fail on a scratch provider so an empty list on the
    # real provider reflects real per-job lifecycle work, not a detector
    # that can never trigger.
    _control_provider = FakeProvider()
    _control_net = _control_provider.create_network(
        profile_digest=profile_digest(profile), job_id="negative-control",
        now=now, persistent=True,
    )
    persistent_detector_works = (
        _control_net.persistent
        and _control_provider.persistent_egress_objects == [_control_net.id]
    )
    checks = {
        "started_at_zero": evidence["start_live_instances"] == 0,
        # Non-vacuous creation-after-demand: a non-empty id alone is always
        # true once created, so also require the created-after-demand event,
        # a provider timestamp at/after demand, zero live instances at start
        # (no pre-created pool), and a distinct second instance below (no
        # pool reuse).
        "created_after_demand": bool(first.instance_id)
        and first_created_event
        and first_created_at_ok
        and evidence["start_live_instances"] == 0,
        "validated_before_work": any(item.startswith("validated-identity") for item in first.events),
        "exactly_one_job": active_provider.instances[first.instance_id].uses == 1,
        "destroyed_after_job": first.destroyed and second.destroyed,
        "idle_returned_to_zero": evidence["live_after_first"] == 0 and evidence["live_after_second"] == 0,
        "second_used_new_instance": evidence["different_instance"] and evidence["different_network"],
        # Non-vacuous per-job egress lifecycle: the provider records
        # deliberately persistent attachments in persistent_egress_objects
        # (see FakeProvider.create_network), every attachment created by
        # this harness is non-persistent and destroyed, and the harness
        # exercises at least the two job networks plus the orphan network,
        # so an empty list alone cannot pass without real lifecycle work.
        # The scratch-provider negative control above proves the detector
        # itself can fail.
        "no_persistent_egress": (
            active_provider.persistent_egress_objects == []
            and all(not item.persistent for item in networks)
            and persistent_detector_works
        ),
        "all_networks_destroyed": all(item.destroyed_at is not None for item in networks),
        "egress_lifecycle_exercised": len(networks) >= 3,
        "orphan_reconciled": evidence["orphan_reconciled"],
        "live_final_zero": evidence["live_final"] == 0 and evidence["live_networks_final"] == 0,
    }
    evidence["checks"] = checks
    evidence["pass"] = all(checks.values())
    return evidence
