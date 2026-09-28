"""Generic OpenAI-compatible chat client.

The provider is fully repository-controlled: base URL, API key, model, and an
optional output limit. Nothing about a specific vendor is hard-coded, and the
credentials are never logged, echoed into a prompt, or written to an artifact.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_SYSTEM_PROMPT = "You are a precise code-review verifier. Answer with the requested token first."

# LiteLLM-style routing prefixes the review job may pass through. The provider's
# chat-completions endpoint expects the bare model name.
MODEL_ROUTING_PREFIXES = (
    "text-completion-openai/",
    "chat-completion-openai/",
    "openai/",
    "litellm/",
    "openrouter/",
)
PR_AGENT_ROUTING_PREFIX = "openai/"

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


class ProviderConfigError(ValueError):
    """Raised when provider configuration is missing or unsafe."""


class ProviderRequestError(RuntimeError):
    """Raised when the provider rejects or garbles a request."""


def strip_model_prefix(model: str) -> str:
    """Remove routing prefixes from a model identifier."""

    name = (model or "").strip()
    for prefix in MODEL_ROUTING_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def route_model(model: str) -> str:
    """Return the identifier in LiteLLM routing form used by the review job."""

    name = (model or "").strip()
    if not name:
        raise ProviderConfigError("Provider model identifier is empty")
    if any(name.startswith(prefix) for prefix in MODEL_ROUTING_PREFIXES):
        return name
    return PR_AGENT_ROUTING_PREFIX + name


def validate_api_base(value: str) -> str:
    """Validate an OpenAI-compatible base URL.

    Requires https, except for loopback hosts used by local development. User
    info in the URL is rejected so a credential can never be smuggled through
    the endpoint configuration.
    """

    raw = (value or "").strip()
    if not raw:
        raise ProviderConfigError("Provider API base URL is empty")
    if any(char.isspace() for char in raw):
        raise ProviderConfigError("Provider API base URL must not contain whitespace")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("https", "http"):
        raise ProviderConfigError(
            f"Provider API base URL must use https (got scheme {parsed.scheme or 'none'!r})"
        )
    if not parsed.hostname:
        raise ProviderConfigError("Provider API base URL has no host")
    if parsed.username or parsed.password:
        raise ProviderConfigError("Provider API base URL must not embed credentials")
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise ProviderConfigError(
            "Provider API base URL must use https unless the host is loopback"
        )
    return raw.rstrip("/")


def parse_max_tokens(value: Optional[str]) -> Optional[int]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        raise ProviderConfigError("Provider max output tokens must be an integer") from None
    if parsed <= 0:
        raise ProviderConfigError("Provider max output tokens must be positive")
    return parsed


def redact(text: str, secret: Optional[str]) -> str:
    """Remove the API key from anything about to be printed or persisted."""

    value = text or ""
    if secret:
        value = value.replace(secret, "***redacted***")
    return value[:2000]


class OpenAICompatibleClient:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        *,
        max_tokens: Optional[int] = None,
        timeout: int = 120,
        opener: Optional[Any] = None,
    ) -> None:
        self.api_base = validate_api_base(api_base)
        if not (api_key or "").strip():
            raise ProviderConfigError("Provider API key is empty")
        self.api_key = api_key
        self.model = strip_model_prefix(model)
        if not self.model:
            raise ProviderConfigError("Provider model identifier is empty")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._opener = opener or _default_opener

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.api_base + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "continuum-review-gate",
            },
            method="POST",
        )
        try:
            response = self._opener(request, self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                detail = ""
            raise ProviderRequestError(
                redact(f"provider request failed with status {exc.code}: {detail}", self.api_key)
            ) from None
        except urllib.error.URLError as exc:
            raise ProviderRequestError(
                redact(f"provider endpoint unreachable: {exc.reason}", self.api_key)
            ) from None
        if not isinstance(response, dict):
            raise ProviderRequestError("provider response was not an object")
        return response

    def chat(self, prompt: str, system: str = DEFAULT_SYSTEM_PROMPT) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        data = self._request(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderRequestError(
                redact(f"unexpected provider response: {json.dumps(data)[:2000]}", self.api_key)
            ) from None
        return content if isinstance(content, str) else json.dumps(content)


def _default_opener(request: urllib.request.Request, timeout: int) -> Any:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", "replace")
        return json.loads(payload) if payload else {}


def client_from_environment(env: Optional[Dict[str, str]] = None) -> OpenAICompatibleClient:
    """Build a client from CONTINUUM_REVIEW_* environment variables."""

    source = env if env is not None else dict(os.environ)
    return OpenAICompatibleClient(
        source.get("CONTINUUM_REVIEW_API_BASE", ""),
        source.get("CONTINUUM_REVIEW_API_KEY", ""),
        source.get("CONTINUUM_REVIEW_MODEL", ""),
        max_tokens=parse_max_tokens(source.get("CONTINUUM_REVIEW_MAX_TOKENS", "")),
    )


def describe_provider(model: str, routing: bool = False) -> Dict[str, str]:
    """Non-sensitive provider description for workflow outputs and logs."""

    return {
        "model": (route_model(model) if routing else strip_model_prefix(model)),
    }


def missing_provider_settings(
    api_base: Optional[str], model: Optional[str], api_key: Optional[str]
) -> List[str]:
    """Names of the missing settings, for an actionable configuration error."""

    missing: List[str] = []
    if not (api_base or "").strip():
        missing.append("api_base")
    if not (model or "").strip():
        missing.append("model")
    if not (api_key or "").strip():
        missing.append("api_key")
    return missing
