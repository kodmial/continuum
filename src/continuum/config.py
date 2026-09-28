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
class ReviewSettings:
    provider: str = PROVIDER_NONE
    block_merge: bool = True
    status_context: str = DEFAULT_STATUS_CONTEXT
    pr_agent: PrAgentSettings = field(default_factory=PrAgentSettings)
    coderabbit: CodeRabbitSettings = field(default_factory=CodeRabbitSettings)

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
                "provider_settings": (
                    self.review.provider_settings().describe()
                    if self.review.provider_settings() is not None
                    else None
                ),
            },
        }


_ALLOWED_TOP_LEVEL = ("version", "review")
_ALLOWED_REVIEW_KEYS = ("provider", "block_merge", "status_context", "pr_agent", "coderabbit")
_ALLOWED_PR_AGENT_KEYS = (
    "api_base_var",
    "api_key_secret",
    "model_var",
    "max_tokens_var",
    "bot_login",
)
_ALLOWED_CODERABBIT_KEYS = ("bot_login", "status_context")


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
