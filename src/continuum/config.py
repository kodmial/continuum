"""Continuum repository configuration (`.continuum.yml`).

The file is repository-controlled and validated before anything else happens.
It never carries an endpoint, a model, or a credential: those are referenced by
*name* so the value stays in repository variables/secrets. That keeps secrets
out of the repository and makes the provider switch a settings change.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import yamlmini

SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = ".continuum.yml"
DEFAULT_STATUS_CONTEXT = "continuum/review"

PROVIDER_NONE = "none"
PROVIDER_PR_AGENT = "pr-agent"
PROVIDER_CODERABBIT = "coderabbit"
SUPPORTED_PROVIDERS = (PROVIDER_NONE, PROVIDER_PR_AGENT, PROVIDER_CODERABBIT)

# The single review implementation Continuum v0.1 ships. `review: true` resolves
# to it, so the MVP boolean carries no provider selector: choosing a reviewer is
# an adapter change inside the engine, never a user-facing configuration key.
MVP_REVIEW_PROVIDER = PROVIDER_CODERABBIT

DEFAULT_PR_AGENT_BOT_LOGIN = "github-actions[bot]"
DEFAULT_CODERABBIT_BOT_LOGIN = "coderabbitai[bot]"
DEFAULT_CODERABBIT_STATUS_CONTEXT = "CodeRabbit"

DEFAULT_QUEUE_READY_LABEL = "review-ready"
DEFAULT_QUEUE_BLOCK_LABELS = ("review-paused", "review-blocked", "no-review")
DEFAULT_QUEUE_PRIORITY_LABELS = ("priority:p0", "priority:p1", "priority:p2")
DEFAULT_QUEUE_TIE_BREAKERS = ("source_issue", "pr_number")
DEFAULT_QUEUE_REQUIRED_CHECKS: tuple = ()
SUPPORTED_QUEUE_TIE_BREAKERS = ("source_issue", "pr_number", "created_at", "head_sha")
DEFAULT_QUEUE_COOLDOWN_MINUTES = 60
DEFAULT_QUEUE_IN_FLIGHT_TIMEOUT_MINUTES = 30
DEFAULT_QUEUE_SAFETY_MARGIN_SECONDS = 30
DEFAULT_QUEUE_DISPATCH_WORKFLOW = "pr-agent.yml"
MAX_QUEUE_CANDIDATES = 200

# -- release targets --------------------------------------------------------
#
# A release target is a *typed contract*: what is built, how it is signed, and
# which downstream publishers consume it. These names are platform vocabulary
# only. The code that acts on them lives in `continuum.release`, so the release
# core never learns what `codesign` or a keychain is.
ADAPTER_APPLE = "apple"
SUPPORTED_RELEASE_ADAPTERS = (ADAPTER_APPLE,)

PLATFORM_MACOS = "macos"
PLATFORM_IOS = "ios"
SUPPORTED_PLATFORMS = (PLATFORM_MACOS, PLATFORM_IOS)

BUILD_STRATEGY_SWIFTPM = "swiftpm"
BUILD_STRATEGY_XCODE_ARCHIVE = "xcode-archive"
SUPPORTED_BUILD_STRATEGIES = (BUILD_STRATEGY_SWIFTPM, BUILD_STRATEGY_XCODE_ARCHIVE)

DISTRIBUTION_DIRECT = "direct"
DISTRIBUTION_APP_STORE = "app-store"
SUPPORTED_DISTRIBUTIONS = (DISTRIBUTION_DIRECT, DISTRIBUTION_APP_STORE)

ARCH_X86_64 = "x86_64"
ARCH_ARM64 = "arm64"
SUPPORTED_ARCHITECTURES = (ARCH_X86_64, ARCH_ARM64)

ARTIFACT_TAR_GZ = "tar.gz"
ARTIFACT_ZIP = "zip"
ARTIFACT_PKG = "pkg"
ARTIFACT_DMG = "dmg"
SUPPORTED_ARTIFACT_FORMATS = (ARTIFACT_TAR_GZ, ARTIFACT_ZIP, ARTIFACT_PKG, ARTIFACT_DMG)

# Downstream package publishers are *declared* here so a repository states who
# consumes its artifacts, but publishing them is a separate concern (#23): the
# Apple adapter signs and hands off, it never uploads to a tap or a port tree.
PUBLISHER_HOMEBREW = "homebrew"
PUBLISHER_MACPORTS = "macports"
SUPPORTED_PUBLISHERS = (PUBLISHER_HOMEBREW, PUBLISHER_MACPORTS)

SIGNING_SELF_SIGNED_STABLE = "self-signed-stable"
SIGNING_ADHOC = "adhoc"
SIGNING_DEVELOPER_ID = "developer-id"
SUPPORTED_SIGNING_MODES = (SIGNING_SELF_SIGNED_STABLE, SIGNING_ADHOC, SIGNING_DEVELOPER_ID)

# The one (platform, build strategy) pair that is executable end to end today.
# The other combinations are schema support: a repository can declare intent
# without Continuum pretending it already builds that variant.
MVP_PLATFORM = PLATFORM_MACOS
MVP_BUILD_STRATEGY = BUILD_STRATEGY_SWIFTPM

# Repository variable / secret names are always upper-case identifiers. Anything
# else (a URL, a model name, a token) is a configuration mistake that must fail
# closed instead of silently inlining a value into a workflow or a prompt.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# A target id is a bare slug: it names a target in a command line and in a plan
# document, so it must not carry whitespace, separators, or shell punctuation.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# A bundle identifier must be reverse-DNS with at least two components, which is
# also the invariant `codesign --identifier` and `tccd` both read. Anything else
# silently produces a different TCC identity.
_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


class ConfigError(ValueError):
    """Raised when `.continuum.yml` is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class PrAgentSettings:
    api_base_var: str = "PR_AGENT_API_BASE"
    api_key_secret: str = "PR_AGENT_API_KEY"
    model_var: str = "PR_AGENT_MODEL"
    max_tokens_var: str = "PR_AGENT_MAX_TOKENS"
    bot_login: str = DEFAULT_PR_AGENT_BOT_LOGIN

    def describe(self) -> Dict[str, str]:
        return {
            "api_base_var": self.api_base_var,
            "api_key_secret": self.api_key_secret,
            "model_var": self.model_var,
            "max_tokens_var": self.max_tokens_var,
            "bot_login": self.bot_login,
        }


@dataclass(frozen=True)
class CodeRabbitSettings:
    bot_login: str = DEFAULT_CODERABBIT_BOT_LOGIN
    status_context: str = DEFAULT_CODERABBIT_STATUS_CONTEXT

    def describe(self) -> Dict[str, str]:
        return {"bot_login": self.bot_login, "status_context": self.status_context}


@dataclass(frozen=True)
class QueueSettings:
    """How the review queue is reconciled.

    These are queue *semantics*, not provider credentials: which label marks a
    candidate as ready, which labels take it out of the queue, how candidates are
    ordered, and how long a shared provider slot stays reserved. A repository
    with a different priority scheme configures it here instead of forking the
    controller.
    """

    ready_label: Optional[str] = DEFAULT_QUEUE_READY_LABEL
    block_labels: tuple = DEFAULT_QUEUE_BLOCK_LABELS
    priority_labels: tuple = DEFAULT_QUEUE_PRIORITY_LABELS
    tie_breakers: tuple = DEFAULT_QUEUE_TIE_BREAKERS
    required_checks: tuple = DEFAULT_QUEUE_REQUIRED_CHECKS
    require_green_ci: bool = True
    cooldown_minutes: int = DEFAULT_QUEUE_COOLDOWN_MINUTES
    in_flight_timeout_minutes: int = DEFAULT_QUEUE_IN_FLIGHT_TIMEOUT_MINUTES
    safety_margin_seconds: int = DEFAULT_QUEUE_SAFETY_MARGIN_SECONDS
    dispatch_workflow: str = DEFAULT_QUEUE_DISPATCH_WORKFLOW
    max_candidates: int = MAX_QUEUE_CANDIDATES

    def describe(self) -> Dict[str, Any]:
        return {
            "ready_label": self.ready_label,
            "block_labels": list(self.block_labels),
            "priority_labels": list(self.priority_labels),
            "tie_breakers": list(self.tie_breakers),
            "required_checks": list(self.required_checks),
            "require_green_ci": self.require_green_ci,
            "cooldown_minutes": self.cooldown_minutes,
            "in_flight_timeout_minutes": self.in_flight_timeout_minutes,
            "safety_margin_seconds": self.safety_margin_seconds,
            "dispatch_workflow": self.dispatch_workflow,
            "max_candidates": self.max_candidates,
        }


@dataclass(frozen=True)
class ReviewSettings:
    provider: str = PROVIDER_NONE
    block_merge: bool = True
    status_context: str = DEFAULT_STATUS_CONTEXT
    pr_agent: PrAgentSettings = field(default_factory=PrAgentSettings)
    coderabbit: CodeRabbitSettings = field(default_factory=CodeRabbitSettings)
    queue: QueueSettings = field(default_factory=QueueSettings)

    @property
    def enabled(self) -> bool:
        return self.provider != PROVIDER_NONE

    def provider_settings(self) -> Optional[Any]:
        if self.provider == PROVIDER_PR_AGENT:
            return self.pr_agent
        if self.provider == PROVIDER_CODERABBIT:
            return self.coderabbit
        return None


@dataclass(frozen=True)
class ReleaseSigningSettings:
    """How a release target's artifacts are signed.

    `self-signed-stable` is the first-class MVP profile: a purpose-made,
    long-lived self-signed code-signing identity whose P12 lives in repository
    secrets. Because the identity is stable, the designated requirement and the
    TCC rows a user granted stay stable across releases. `adhoc` is an explicit,
    visibly degraded fallback for builds that cannot reach the signing secrets;
    it never carries the stable profile's guarantees.
    """

    mode: str = SIGNING_SELF_SIGNED_STABLE
    identity: str = ""
    p12_secret: str = ""
    password_secret: str = ""
    timestamp: bool = False
    allow_adhoc_fallback: bool = False
    team_id_var: str = ""

    @property
    def is_stable(self) -> bool:
        return self.mode == SIGNING_SELF_SIGNED_STABLE

    def secret_names(self) -> tuple:
        return tuple(name for name in (self.p12_secret, self.password_secret) if name)

    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "identity": self.identity,
            "p12_secret": self.p12_secret,
            "password_secret": self.password_secret,
            "timestamp": self.timestamp,
            "allow_adhoc_fallback": self.allow_adhoc_fallback,
            "team_id_var": self.team_id_var,
        }


@dataclass(frozen=True)
class ReleaseBinary:
    """One signed executable inside a release artifact.

    `identifier` is the `codesign --identifier` and the `CFBundleIdentifier` a
    user agent sees. Both must agree, and both must stay the same across
    releases, or the OS treats the next release as a different application and
    discards the permissions the user granted to the previous one.
    """

    name: str
    identifier: str
    entitlements: str = ""
    link_flags: tuple = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "identifier": self.identifier,
            "entitlements": self.entitlements,
            "link_flags": list(self.link_flags),
        }


@dataclass(frozen=True)
class ReleaseAppBundle:
    """The `.app` assembled from already-signed binaries and sealed as a unit.

    The bundle reuses the binaries rather than re-building them: it is signed
    with the *same* identity, identifier, and entitlements so the permissions
    granted to one product do not fork into two.
    """

    name: str
    identifier: str
    info_plist: str = ""
    entitlements: str = ""
    resources: tuple = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "identifier": self.identifier,
            "info_plist": self.info_plist,
            "entitlements": self.entitlements,
            "resources": list(self.resources),
        }


@dataclass(frozen=True)
class ReleaseTarget:
    """One buildable, signable, publishable thing."""

    id: str
    adapter: str = ADAPTER_APPLE
    platform: str = PLATFORM_MACOS
    build_strategy: str = BUILD_STRATEGY_SWIFTPM
    distribution: str = DISTRIBUTION_DIRECT
    architectures: tuple = (ARCH_X86_64, ARCH_ARM64)
    universal: bool = False
    artifacts: tuple = (ARTIFACT_TAR_GZ, ARTIFACT_ZIP)
    hardened_runtime: bool = True
    binaries: tuple = ()
    app_bundle: Optional[ReleaseAppBundle] = None
    signing: ReleaseSigningSettings = field(default_factory=ReleaseSigningSettings)
    publishers: tuple = ()

    @property
    def is_mvp_executable(self) -> bool:
        """Whether this combination is buildable today, or only declared."""

        return self.platform == MVP_PLATFORM and self.build_strategy == MVP_BUILD_STRATEGY

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "platform": self.platform,
            "build_strategy": self.build_strategy,
            "distribution": self.distribution,
            "architectures": list(self.architectures),
            "universal": self.universal,
            "artifacts": list(self.artifacts),
            "hardened_runtime": self.hardened_runtime,
            "binaries": [item.describe() for item in self.binaries],
            "app_bundle": self.app_bundle.describe() if self.app_bundle else None,
            "signing": self.signing.describe(),
            "publishers": list(self.publishers),
        }


@dataclass(frozen=True)
class ReleaseSettings:
    targets: tuple = ()

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    def target(self, target_id: str) -> Optional[ReleaseTarget]:
        for target in self.targets:
            if target.id == target_id:
                return target
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "targets": [target.describe() for target in self.targets],
            "required_secrets": sorted(
                {
                    name
                    for target in self.targets
                    for name in target.signing.secret_names()
                }
            ),
        }


DELEGATION_NONE = "none"
DELEGATION_PARENT = "parent"
DELEGATION_CHILD = "child"
SUPPORTED_DELEGATION_ROLES = (DELEGATION_NONE, DELEGATION_PARENT, DELEGATION_CHILD)

_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)


@dataclass(frozen=True)
class DelegationSettings:
    """Cross-repository execution relationship.

    A parent lists opaque child ids only. The id -> owner/repository binding is
    runtime secret data, so a public donor repository does not disclose the
    names of private child repositories. A child records its own id and parent;
    execution is allowed only when both sides agree.
    """

    role: str = DELEGATION_NONE
    children: tuple = ()
    id: str = ""
    parent: str = ""
    validation_script: str = ""

    @property
    def enabled(self) -> bool:
        return self.role != DELEGATION_NONE

    def describe(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "children": list(self.children),
            "id": self.id or None,
            "parent": self.parent or None,
            "validation_script": self.validation_script or None,
        }


@dataclass(frozen=True)
class ContinuumConfig:
    version: int = SCHEMA_VERSION
    review: ReviewSettings = field(default_factory=ReviewSettings)
    release: ReleaseSettings = field(default_factory=ReleaseSettings)
    delegation: DelegationSettings = field(default_factory=DelegationSettings)
    source: str = "<defaults>"

    def describe(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "review": {
                "provider": self.review.provider,
                "block_merge": self.review.block_merge,
                "status_context": self.review.status_context,
                "queue": self.review.queue.describe(),
                "provider_settings": (
                    self.review.provider_settings().describe()
                    if self.review.provider_settings() is not None
                    else None
                ),
            },
            "release": self.release.describe(),
            "delegation": self.delegation.describe(),
        }


_ALLOWED_TOP_LEVEL = ("version", "review", "release", "delegation")
_ALLOWED_DELEGATION_KEYS = ("role", "children", "id", "parent", "validation_script")
_ALLOWED_REVIEW_KEYS = (
    "provider",
    "block_merge",
    "status_context",
    "pr_agent",
    "coderabbit",
    "queue",
)
_ALLOWED_PR_AGENT_KEYS = (
    "api_base_var",
    "api_key_secret",
    "model_var",
    "max_tokens_var",
    "bot_login",
)
_ALLOWED_CODERABBIT_KEYS = ("bot_login", "status_context")
_ALLOWED_RELEASE_KEYS = ("targets",)
_ALLOWED_TARGET_KEYS = (
    "id",
    "adapter",
    "platform",
    "build_strategy",
    "distribution",
    "architectures",
    "universal",
    "artifacts",
    "hardened_runtime",
    "binaries",
    "app_bundle",
    "signing",
    "publishers",
)
_ALLOWED_BINARY_KEYS = ("name", "identifier", "entitlements", "link_flags")
_ALLOWED_APP_BUNDLE_KEYS = ("name", "identifier", "info_plist", "entitlements", "resources")
_ALLOWED_SIGNING_KEYS = (
    "mode",
    "identity",
    "p12_secret",
    "password_secret",
    "timestamp",
    "allow_adhoc_fallback",
    "team_id_var",
)
_ALLOWED_QUEUE_KEYS = (
    "ready_label",
    "block_labels",
    "priority_labels",
    "tie_breakers",
    "required_checks",
    "require_green_ci",
    "cooldown_minutes",
    "in_flight_timeout_minutes",
    "safety_margin_seconds",
    "dispatch_workflow",
    "max_candidates",
)


def _require_mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


_TOP_LEVEL_KEY_RE = "^{key}\\s*:(?P<value>.*)$"
_INLINE_COMMENT_RE = re.compile(r"\s+#")


def _top_level_value(text: str, key: str) -> str:
    """The literal value written for one top-level key, comments excluded.

    Read from the source rather than from the parsed document because the point
    is to see what was *written*: `yes`, `on`, and `"true"` all parse to a
    boolean, and the contract accepts exactly two spellings.
    """

    pattern = re.compile(_TOP_LEVEL_KEY_RE.format(key=re.escape(key)))
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line[:1].isspace():
            continue
        match = pattern.match(line.strip())
        if match is None:
            continue
        return _INLINE_COMMENT_RE.split(match.group("value"), maxsplit=1)[0].strip()
    return ""


def _carries_nothing(value: Any) -> bool:
    """Whether an optional value is really the same as leaving it out.

    Every optional field in this schema defaults to `""` or `()`, and
    `describe()` writes those defaults rather than dropping the keys. So an
    empty value has to mean what an absent key means, or a target cannot make
    the trip a release hands it: the core passes each target to its adapter as
    the mapping `describe()` produced, and the adapter parses those options back
    with this module. Reading "nothing configured" as a mistake would fail every
    target that legitimately configures nothing for an optional field.

    A non-empty value of the wrong type is *not* absent: `0` and `[]`-shaped
    mistakes still reach the validators below and are still rejected.
    """

    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _without_empty(value: Any) -> Any:
    """``value``, or ``None`` when it carries nothing."""

    return None if _carries_nothing(value) else value


def _reject_unknown(mapping: Dict[str, Any], allowed: Any, where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError(
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        )


def _require_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ConfigError(
            f"{where} must be an upper-case repository variable or secret name "
            f"(letters, digits, underscore); got {value!r}"
        )
    return value


def _require_login(value: Any, where: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ConfigError(f"{where} must be a non-empty bot login of at most 64 characters")
    if any(char in value for char in "\n\r"):
        raise ConfigError(f"{where} must not contain newlines")
    return value.strip()


def _require_status_context(value: Any, where: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ConfigError(f"{where} must be a non-empty status context of at most 100 characters")
    if any(char in value for char in "\n\r"):
        raise ConfigError(f"{where} must not contain newlines")
    return value.strip()


def _require_choice(value: Any, where: str, choices: tuple, default: Any) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(
            f"{where} must be one of {', '.join(choices)}; got {value!r}"
        )
    return value


def _require_bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false, got {value!r}")
    return value


def _require_slug(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise ConfigError(
            f"{where} must be a lower-case slug of at most 64 characters "
            f"(letters, digits, '.', '_', '-'); got {value!r}"
        )
    return value


def _require_identifier(value: Any, where: str) -> str:
    """A reverse-DNS bundle identifier.

    This is the string the signing tool pins and the OS reads back to decide
    whether a new build is the same application as the last one. It therefore
    has to look like a bundle identifier, not like a display name.
    """

    if not isinstance(value, str) or len(value) > 255 or not _IDENTIFIER_RE.match(value):
        raise ConfigError(
            f"{where} must be a reverse-DNS bundle identifier with at least two "
            f"components (letters, digits, '-', '.'); got {value!r}"
        )
    return value


def _require_relative_path(value: Any, where: str) -> str:
    """A repository-relative path that cannot escape the checkout."""

    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise ConfigError(f"{where} must be a repository-relative path")
    path = value.strip()
    if any(char in path for char in "\n\r\0") or path.startswith("/") or "\\" in path:
        raise ConfigError(
            f"{where} must be a repository-relative path without a leading '/' or "
            f"a backslash; got {value!r}"
        )
    if ".." in path.split("/"):
        raise ConfigError(f"{where} must not contain a '..' segment; got {value!r}")
    return path


def _require_file_name(value: Any, where: str) -> str:
    """A single path segment: a product or artifact file name."""

    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ConfigError(f"{where} must be a file name of at most 120 characters")
    name = value.strip()
    if any(char in name for char in "\n\r\0") or "/" in name or name in (".", ".."):
        raise ConfigError(f"{where} must be a single file name; got {value!r}")
    return name


def _require_choice_list(
    value: Any, where: str, choices: tuple, default: Any
) -> tuple:
    if _carries_nothing(value):
        return tuple(default)
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list of {', '.join(choices)}")
    seen: List[str] = []
    for index, item in enumerate(value):
        if item is None:
            raise ConfigError(f"{where}[{index}] must be one of {', '.join(choices)}; got null")
        choice = _require_choice(item, f"{where}[{index}]", choices, None)
        if choice in seen:
            raise ConfigError(f"{where} lists {choice!r} twice")
        seen.append(choice)
    return tuple(seen)


def _require_freeform_list(value: Any, where: str, default: Any) -> tuple:
    if _carries_nothing(value):
        return tuple(default)
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list")
    items: List[str] = []
    for index, item in enumerate(value):
        items.append(_require_relative_path(item, f"{where}[{index}]"))
    return tuple(items)


def _require_link_flags(value: Any, where: str) -> tuple:
    """Linker flags, kept as an opaque list of tokens.

    A flag is a token, never a shell fragment: the plan executes an argument
    vector, so a value carrying shell syntax here is inert rather than
    dangerous.
    """

    if _carries_nothing(value):
        return ()
    flags: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or len(item) > 120:
            raise ConfigError(
                f"{where}[{index}] must be a non-empty string of at most 120 characters"
            )
        if any(char in item for char in "\n\r\0"):
            raise ConfigError(f"{where}[{index}] must not contain newlines")
        flags.append(item)
    return tuple(flags)



def _optional_label(value: Any, where: str) -> Optional[str]:
    """A label name, or `null` to disable the check entirely."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ConfigError(f"{where} must be a label name of at most 100 characters, or null")
    if any(char in value for char in "\n\r"):
        raise ConfigError(f"{where} must not contain newlines")
    return value.strip().lower()


def _require_label(value: Any, where: str) -> str:
    label = _optional_label(value, where)
    if label is None:
        raise ConfigError(f"{where} must be a label name")
    return label


def _require_label_list(
    value: Any, where: str, default: Any, *, allow_empty: bool = False
) -> tuple:
    if value is None:
        return tuple(default)
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = " (use [] to disable the check)" if allow_empty else ""
        raise ConfigError(f"{where} must be a list of label names{suffix}")
    labels: List[str] = []
    for index, item in enumerate(value):
        label = _require_label(item, f"{where}[{index}]")
        if label in labels:
            raise ConfigError(f"{where} lists {label!r} twice")
        labels.append(label)
    return tuple(labels)


def _require_bounded_int(value: Any, where: str, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where} must be between {minimum} and {maximum}, got {value}")
    return value


def _require_tie_breakers(value: Any) -> tuple:
    if value is None:
        return DEFAULT_QUEUE_TIE_BREAKERS
    if not isinstance(value, list) or not value:
        raise ConfigError(
            "review.queue.tie_breakers must be a non-empty list drawn from "
            + ", ".join(SUPPORTED_QUEUE_TIE_BREAKERS)
        )
    seen: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item not in SUPPORTED_QUEUE_TIE_BREAKERS:
            raise ConfigError(
                f"review.queue.tie_breakers[{index}] must be one of "
                + ", ".join(SUPPORTED_QUEUE_TIE_BREAKERS)
            )
        if item in seen:
            raise ConfigError(f"review.queue.tie_breakers lists {item!r} twice")
        seen.append(item)
    return tuple(seen)


def _require_workflow(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ConfigError("review.queue.dispatch_workflow must be a workflow file name")
    if not value.strip().endswith((".yml", ".yaml")):
        raise ConfigError("review.queue.dispatch_workflow must end with .yml or .yaml")
    if "/" in value or "\\" in value or value.strip().startswith("."):
        raise ConfigError("review.queue.dispatch_workflow must be a bare workflow file name")
    return value.strip()


def _parse_queue(value: Any) -> QueueSettings:
    mapping = _require_mapping(value, "review.queue")
    _reject_unknown(mapping, _ALLOWED_QUEUE_KEYS, "review.queue")
    defaults = QueueSettings()
    ready_label = mapping.get("ready_label", defaults.ready_label)
    if ready_label is not None and not isinstance(ready_label, str):
        raise ConfigError("review.queue.ready_label must be a label name or null")
    require_green_ci = mapping.get("require_green_ci", defaults.require_green_ci)
    if not isinstance(require_green_ci, bool):
        raise ConfigError(
            f"review.queue.require_green_ci must be a boolean, got {require_green_ci!r}"
        )
    return QueueSettings(
        ready_label=_optional_label(ready_label, "review.queue.ready_label"),
        block_labels=_require_label_list(
            mapping.get("block_labels"), "review.queue.block_labels", defaults.block_labels
        ),
        priority_labels=_require_label_list(
            mapping.get("priority_labels"), "review.queue.priority_labels", defaults.priority_labels
        ),
        tie_breakers=_require_tie_breakers(mapping.get("tie_breakers")),
        required_checks=_require_label_list(
            mapping.get("required_checks"),
            "review.queue.required_checks",
            defaults.required_checks,
            allow_empty=True,
        ),
        require_green_ci=require_green_ci,
        cooldown_minutes=_require_bounded_int(
            mapping.get("cooldown_minutes"),
            "review.queue.cooldown_minutes",
            defaults.cooldown_minutes,
            minimum=0,
            maximum=24 * 60,
        ),
        in_flight_timeout_minutes=_require_bounded_int(
            mapping.get("in_flight_timeout_minutes"),
            "review.queue.in_flight_timeout_minutes",
            defaults.in_flight_timeout_minutes,
            minimum=1,
            maximum=24 * 60,
        ),
        safety_margin_seconds=_require_bounded_int(
            mapping.get("safety_margin_seconds"),
            "review.queue.safety_margin_seconds",
            defaults.safety_margin_seconds,
            minimum=0,
            maximum=3600,
        ),
        dispatch_workflow=_require_workflow(
            mapping.get("dispatch_workflow", defaults.dispatch_workflow)
        ),
        max_candidates=_require_bounded_int(
            mapping.get("max_candidates"),
            "review.queue.max_candidates",
            defaults.max_candidates,
            minimum=1,
            maximum=MAX_QUEUE_CANDIDATES,
        ),
    )


def _parse_pr_agent(value: Any) -> PrAgentSettings:
    mapping = _require_mapping(value, "review.pr_agent")
    _reject_unknown(mapping, _ALLOWED_PR_AGENT_KEYS, "review.pr_agent")
    defaults = PrAgentSettings()
    return PrAgentSettings(
        api_base_var=_require_name(
            mapping.get("api_base_var", defaults.api_base_var), "review.pr_agent.api_base_var"
        ),
        api_key_secret=_require_name(
            mapping.get("api_key_secret", defaults.api_key_secret),
            "review.pr_agent.api_key_secret",
        ),
        model_var=_require_name(
            mapping.get("model_var", defaults.model_var), "review.pr_agent.model_var"
        ),
        max_tokens_var=_require_name(
            mapping.get("max_tokens_var", defaults.max_tokens_var),
            "review.pr_agent.max_tokens_var",
        ),
        bot_login=_require_login(
            mapping.get("bot_login"), "review.pr_agent.bot_login", defaults.bot_login
        ),
    )


def _parse_coderabbit(value: Any) -> CodeRabbitSettings:
    mapping = _require_mapping(value, "review.coderabbit")
    _reject_unknown(mapping, _ALLOWED_CODERABBIT_KEYS, "review.coderabbit")
    defaults = CodeRabbitSettings()
    return CodeRabbitSettings(
        bot_login=_require_login(
            mapping.get("bot_login"), "review.coderabbit.bot_login", defaults.bot_login
        ),
        status_context=_require_status_context(
            mapping.get("status_context"),
            "review.coderabbit.status_context",
            defaults.status_context,
        ),
    )


def _parse_signing(value: Any, where: str) -> ReleaseSigningSettings:
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_SIGNING_KEYS, where)
    defaults = ReleaseSigningSettings()
    mode = _require_choice(mapping.get("mode"), f"{where}.mode", SUPPORTED_SIGNING_MODES, defaults.mode)
    timestamp = _require_bool(mapping.get("timestamp"), f"{where}.timestamp", defaults.timestamp)
    allow_fallback = _require_bool(
        mapping.get("allow_adhoc_fallback"), f"{where}.allow_adhoc_fallback", defaults.allow_adhoc_fallback
    )

    identity = _without_empty(mapping.get("identity"))
    if identity is None:
        identity = ""
    elif not isinstance(identity, str) or not identity.strip() or len(identity) > 200:
        raise ConfigError(
            f"{where}.identity must be a signing identity of at most 200 characters"
        )
    elif any(char in identity for char in "\n\r"):
        raise ConfigError(f"{where}.identity must not contain newlines")
    else:
        identity = identity.strip()

    team_id_var = _without_empty(mapping.get("team_id_var"))
    if team_id_var is not None:
        team_id_var = _require_name(team_id_var, f"{where}.team_id_var")

    p12_secret = _without_empty(mapping.get("p12_secret"))
    password_secret = _without_empty(mapping.get("password_secret"))
    if p12_secret is not None:
        p12_secret = _require_name(p12_secret, f"{where}.p12_secret")
    if password_secret is not None:
        password_secret = _require_name(password_secret, f"{where}.password_secret")

    if mode == SIGNING_ADHOC:
        # An ad-hoc signature has no identity and no key material. Accepting
        # them here would imply a guarantee the mode cannot keep, so the
        # configuration must say out loud that it is degraded instead.
        for stale in ("identity", "p12_secret", "password_secret", "team_id_var"):
            if not _carries_nothing(mapping.get(stale)):
                raise ConfigError(
                    f"{where}.{stale} is configured but {where}.mode is "
                    f"'{SIGNING_ADHOC}', which signs without an identity; remove the "
                    "unused key or select the self-signed-stable mode"
                )
        if allow_fallback:
            raise ConfigError(
                f"{where}.allow_adhoc_fallback has no effect when {where}.mode is "
                f"'{SIGNING_ADHOC}': the fallback applies to the self-signed-stable mode"
            )
    else:
        if not identity:
            raise ConfigError(
                f"{where}.identity is required for {where}.mode '{mode}'; an Apple "
                "signing identity must be named so the plan can pin what it produced"
            )
        for key, value in (("p12_secret", p12_secret), ("password_secret", password_secret)):
            if not value:
                raise ConfigError(
                    f"{where}.{key} is required for {where}.mode '{mode}'; the "
                    "certificate material is referenced by repository secret name"
                )
        if mode == SIGNING_DEVELOPER_ID and not team_id_var:
            raise ConfigError(
                f"{where}.team_id_var is required for {where}.mode "
                f"'{SIGNING_DEVELOPER_ID}'"
            )
        if mode == SIGNING_SELF_SIGNED_STABLE and team_id_var:
            raise ConfigError(
                f"{where}.team_id_var applies to '{SIGNING_DEVELOPER_ID}', not to the "
                f"self-signed CI identity used by '{SIGNING_SELF_SIGNED_STABLE}'"
            )

    return ReleaseSigningSettings(
        mode=mode,
        identity=identity,
        p12_secret=p12_secret or "",
        password_secret=password_secret or "",
        timestamp=timestamp,
        allow_adhoc_fallback=allow_fallback,
        team_id_var=team_id_var or "",
    )


def _parse_binary(value: Any, where: str) -> ReleaseBinary:
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_BINARY_KEYS, where)
    entitlements = _without_empty(mapping.get("entitlements"))
    if entitlements is not None:
        entitlements = _require_relative_path(entitlements, f"{where}.entitlements")
    return ReleaseBinary(
        name=_require_file_name(mapping.get("name"), f"{where}.name"),
        identifier=_require_identifier(mapping.get("identifier"), f"{where}.identifier"),
        entitlements=entitlements or "",
        link_flags=_require_link_flags(mapping.get("link_flags"), f"{where}.link_flags"),
    )


def _parse_app_bundle(value: Any, where: str) -> Optional[ReleaseAppBundle]:
    if value is None:
        return None
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_APP_BUNDLE_KEYS, where)
    name = _require_file_name(mapping.get("name"), f"{where}.name")
    if not name.endswith(".app"):
        raise ConfigError(f"{where}.name must end with '.app'; got {name!r}")
    for key in ("info_plist", "entitlements"):
        if not _carries_nothing(mapping.get(key)):
            mapping[key] = _require_relative_path(mapping[key], f"{where}.{key}")
    return ReleaseAppBundle(
        name=name,
        identifier=_require_identifier(mapping.get("identifier"), f"{where}.identifier"),
        info_plist=mapping.get("info_plist") or "",
        entitlements=mapping.get("entitlements") or "",
        resources=_require_freeform_list(mapping.get("resources"), f"{where}.resources", ()),
    )


def _parse_target(value: Any, where: str) -> ReleaseTarget:
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_TARGET_KEYS, where)
    defaults = ReleaseTarget(id="placeholder")

    binaries_value = mapping.get("binaries")
    if binaries_value is None:
        binaries: Tuple[ReleaseBinary, ...] = ()
    elif not isinstance(binaries_value, list):
        raise ConfigError(f"{where}.binaries must be a list of products")
    else:
        binaries = tuple(
            _parse_binary(item, f"{where}.binaries[{index}]")
            for index, item in enumerate(binaries_value)
        )
    identifiers = [item.identifier for item in binaries]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise ConfigError(
            f"{where}.binaries reuses the identifier(s) {', '.join(duplicates)}; each "
            "signed product needs its own, or they collapse into one identity"
        )

    app_bundle = _parse_app_bundle(mapping.get("app_bundle"), f"{where}.app_bundle")
    # A target must name at least one thing to sign, but not necessarily a
    # binary. An app-store build is a single sealed bundle with no command line
    # product beside it, and forcing such a target to invent a binary — or to
    # declare `binaries: []`, which the schema below cannot express — would make
    # the iOS contract unrepresentable before it is implementable.
    if not binaries and app_bundle is None:
        raise ConfigError(
            f"{where} names no products: a release target must have at least one "
            "binary or an app_bundle to sign"
        )
    signing = _parse_signing(mapping.get("signing"), f"{where}.signing")

    return ReleaseTarget(
        id=_require_slug(mapping.get("id"), f"{where}.id"),
        adapter=_require_choice(
            mapping.get("adapter"), f"{where}.adapter", SUPPORTED_RELEASE_ADAPTERS, defaults.adapter
        ),
        platform=_require_choice(
            mapping.get("platform"), f"{where}.platform", SUPPORTED_PLATFORMS, defaults.platform
        ),
        build_strategy=_require_choice(
            mapping.get("build_strategy"),
            f"{where}.build_strategy",
            SUPPORTED_BUILD_STRATEGIES,
            defaults.build_strategy,
        ),
        distribution=_require_choice(
            mapping.get("distribution"),
            f"{where}.distribution",
            SUPPORTED_DISTRIBUTIONS,
            defaults.distribution,
        ),
        architectures=_require_choice_list(
            mapping.get("architectures"),
            f"{where}.architectures",
            SUPPORTED_ARCHITECTURES,
            defaults.architectures,
        ),
        universal=_require_bool(mapping.get("universal"), f"{where}.universal", defaults.universal),
        artifacts=_require_choice_list(
            mapping.get("artifacts"), f"{where}.artifacts", SUPPORTED_ARTIFACT_FORMATS, defaults.artifacts
        ),
        hardened_runtime=_require_bool(
            mapping.get("hardened_runtime"), f"{where}.hardened_runtime", defaults.hardened_runtime
        ),
        binaries=binaries,
        app_bundle=app_bundle,
        signing=signing,
        publishers=_require_choice_list(
            mapping.get("publishers"), f"{where}.publishers", SUPPORTED_PUBLISHERS, ()
        ),
    )


def _parse_release(value: Any, source: str = "release") -> ReleaseSettings:
    if value is None:
        return ReleaseSettings()
    if not isinstance(value, dict):
        # Same single-boolean surface as `review`, for the same reason: the
        # MVP contract is two toggles and no selectors, so both are read the
        # way they were written rather than as whatever YAML made of them.
        token = _top_level_value(source, "release")
        if token in ("true", "false"):
            # Neither spelling declares a target, and a declared target is what
            # release orchestration consumes. `true` therefore means "the
            # contract is adopted, no targets are declared", which is exactly
            # the disabled posture.
            return ReleaseSettings()
        raise ConfigError(
            f"release must be the bare boolean true or false (unquoted), or a "
            f"mapping of release options; got {token or value!r}"
        )
    mapping = value
    _reject_unknown(mapping, _ALLOWED_RELEASE_KEYS, "release")
    targets_value = mapping.get("targets")
    if targets_value is None:
        return ReleaseSettings()
    if not isinstance(targets_value, list) or not targets_value:
        raise ConfigError("release.targets must be a non-empty list, or omitted entirely")
    targets = tuple(
        _parse_target(item, f"release.targets[{index}]")
        for index, item in enumerate(targets_value)
    )
    seen: List[str] = []
    for index, target in enumerate(targets):
        if target.id in seen:
            raise ConfigError(f"release.targets[{index}].id duplicates {target.id!r}")
        seen.append(target.id)
    return ReleaseSettings(targets=targets)


def parse_release_target(value: Any, where: str = "release target") -> ReleaseTarget:
    """Parse one release target from a mapping.

    Public because a target's configuration is no longer confined to the file it
    was parsed from: the release core hands a target to its adapter as
    `TargetSpec.options`, and the adapter needs the same validation applied to
    those options that this module applied to the file. Re-parsing rather than
    trusting the round trip means a hand-assembled target is held to the schema
    instead of being privileged by the route it arrived on.
    """

    return _parse_target(value, where)



def _require_repository(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.match(value.strip()):
        raise ConfigError(
            f"{where} must be an owner/repository name; got {value!r}"
        )
    return value.strip()


def _parse_delegation(value: Any) -> DelegationSettings:
    mapping = _require_mapping(value, "delegation")
    _reject_unknown(mapping, _ALLOWED_DELEGATION_KEYS, "delegation")
    role = _require_choice(
        mapping.get("role"),
        "delegation.role",
        SUPPORTED_DELEGATION_ROLES,
        DELEGATION_NONE,
    )

    children_value = mapping.get("children")
    child_id_value = mapping.get("id")
    parent_value = mapping.get("parent")
    validation_script_value = mapping.get("validation_script")

    if role == DELEGATION_NONE:
        stale = [
            key for key in ("children", "id", "parent", "validation_script")
            if mapping.get(key) is not None
        ]
        if stale:
            raise ConfigError(
                "delegation role 'none' cannot configure " + ", ".join(stale)
            )
        return DelegationSettings()

    if role == DELEGATION_PARENT:
        if child_id_value is not None or parent_value is not None or validation_script_value is not None:
            raise ConfigError("delegation role 'parent' accepts children only")
        if not isinstance(children_value, list) or not children_value:
            raise ConfigError(
                "delegation.children must be a non-empty list for role 'parent'"
            )
        children: List[str] = []
        for index, item in enumerate(children_value):
            child_id = _require_slug(item, f"delegation.children[{index}]")
            if child_id in children:
                raise ConfigError(
                    f"delegation.children lists {child_id!r} twice"
                )
            children.append(child_id)
        return DelegationSettings(role=role, children=tuple(children))

    if children_value is not None:
        raise ConfigError("delegation role 'child' does not accept children")
    child_id = _require_slug(child_id_value, "delegation.id")
    parent = _require_repository(parent_value, "delegation.parent")
    validation_script = ""
    if validation_script_value is not None:
        validation_script = _require_relative_path(
            validation_script_value,
            "delegation.validation_script",
        )
        if not validation_script.endswith(".sh"):
            raise ConfigError(
                "delegation.validation_script must name a .sh file executed by bash"
            )
    return DelegationSettings(
        role=role,
        id=child_id,
        parent=parent,
        validation_script=validation_script,
    )


def parse_config(text: str, source: str = "<string>") -> ContinuumConfig:
    """Validate configuration text and return a `ContinuumConfig`."""

    try:
        document = yamlmini.loads(text)
    except yamlmini.YamlSubsetError as exc:
        raise ConfigError(f"{source}: {exc}") from None
    if document is None:
        document = {}
    mapping = _require_mapping(document, source)
    _reject_unknown(mapping, _ALLOWED_TOP_LEVEL, source)

    version = mapping.get("version", SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise ConfigError(f"{source}: version must be the integer {SCHEMA_VERSION}, got {version!r}")

    raw_review = mapping.get("review")
    if raw_review is None:
        review: Dict[str, Any] = {}
    elif isinstance(raw_review, dict):
        review = raw_review
    else:
        # The MVP contract's surface: one boolean, no provider selector. Only the
        # bare tokens are accepted, so a YAML 1.1 truthy spelling cannot turn a
        # gate on by accident.
        token = _top_level_value(text, "review")
        if token == "true":
            review = {"provider": MVP_REVIEW_PROVIDER}
        elif token == "false":
            review = {}
        else:
            raise ConfigError(
                f"{source}: review must be the bare boolean true or false (unquoted), "
                f"or a mapping of review options; got {token or raw_review!r}"
            )
    _reject_unknown(review, _ALLOWED_REVIEW_KEYS, "review")

    provider = review.get("provider", PROVIDER_NONE)
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            "review.provider must be one of "
            + ", ".join(SUPPORTED_PROVIDERS)
            + f"; got {provider!r}"
        )

    block_merge = review.get("block_merge", True)
    if not isinstance(block_merge, bool):
        raise ConfigError(f"review.block_merge must be a boolean, got {block_merge!r}")

    if provider == PROVIDER_NONE:
        for stale in ("pr_agent", "coderabbit"):
            if review.get(stale) is not None:
                raise ConfigError(
                    f"review.{stale} is configured but review.provider is '{PROVIDER_NONE}'; "
                    "remove the unused provider block or select the provider"
                )

    status_context = _require_status_context(
        review.get("status_context"), "review.status_context", DEFAULT_STATUS_CONTEXT
    )
    if provider == PROVIDER_PR_AGENT and status_context == DEFAULT_CODERABBIT_STATUS_CONTEXT:
        raise ConfigError(
            "review.status_context must not reuse a sibling provider's status context"
        )

    return ContinuumConfig(
        version=version,
        review=ReviewSettings(
            provider=provider,
            block_merge=block_merge,
            status_context=status_context,
            pr_agent=_parse_pr_agent(review.get("pr_agent")),
            coderabbit=_parse_coderabbit(review.get("coderabbit")),
            queue=_parse_queue(review.get("queue")),
        ),
        release=_parse_release(mapping.get("release"), text),
        delegation=_parse_delegation(mapping.get("delegation")),
        source=source,
    )


def load_config(path: str = DEFAULT_CONFIG_PATH, text: Optional[str] = None) -> ContinuumConfig:
    """Load configuration from ``text`` or from ``path`` on disk."""

    if text is None:
        if not os.path.isfile(path):
            raise ConfigError(
                f"Missing Continuum configuration file {path!r}. Add a validated "
                f"{DEFAULT_CONFIG_PATH} with at least 'version: {SCHEMA_VERSION}'."
            )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ConfigError(f"Cannot read {path!r}: {exc}") from None
    return parse_config(text, source=path)


def load_optional_config(
    path: str = DEFAULT_CONFIG_PATH, text: Optional[str] = None
) -> ContinuumConfig:
    """Load configuration, falling back to `review.provider: none` when absent."""

    if text is None and not os.path.isfile(path):
        return ContinuumConfig()
    return load_config(path, text)


def required_variable_names(config: ContinuumConfig) -> List[str]:
    """Repository variable names the selected provider needs (sorted, stable)."""

    settings = config.review.provider_settings()
    if isinstance(settings, PrAgentSettings):
        return sorted({settings.api_base_var, settings.model_var, settings.max_tokens_var})
    return []


def required_secret_names(config: ContinuumConfig) -> List[str]:
    """Repository secret names the selected provider needs (sorted, stable)."""

    settings = config.review.provider_settings()
    if isinstance(settings, PrAgentSettings):
        return sorted({settings.api_key_secret})
    return []


def required_release_secret_names(config: ContinuumConfig) -> List[str]:
    """Repository secret names the configured release targets need.

    Names only, never values: the caller turns each name into a request for
    that secret. A target that falls back to an ad-hoc signature still reports
    the names it would have needed, so a missing secret is visible in the job
    summary rather than silently absent.
    """

    return sorted({name for target in config.release.targets for name in target.signing.secret_names()})
