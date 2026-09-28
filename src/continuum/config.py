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

# Repository variable / secret names are always upper-case identifiers. Anything
# else (a URL, a model name, a token) is a configuration mistake that must fail
# closed instead of silently inlining a value into a workflow or a prompt.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


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
class ContinuumConfig:
    version: int = SCHEMA_VERSION
    review: ReviewSettings = field(default_factory=ReviewSettings)
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
        }


_ALLOWED_TOP_LEVEL = ("version", "review")
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

    review = _require_mapping(mapping.get("review"), "review")
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
