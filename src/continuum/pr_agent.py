"""Reusable PR-Agent review semantics for Continuum.

This module is provider-neutral: it never names a paid provider, a vendor
model, or a credential. It resolves generic configuration (backend base,
model, token limit, enable/disable), converts between the OpenAI-compatible
surface PR-Agent needs and the OpenCode server session/message API the
bridge serves, and implements the review-state contract (stable finding
ids, approve vs request-changes, verify semantics, current-head tracking,
large-PR chunking with fail-closed coverage).

The dependency-free rule matters: this module is imported by workflow
scripts on a bare runner and by unit tests, so it uses the standard
library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# A resolved configuration must never smuggle a paid-provider reference
# into the generic path. The bridge and the workflow both route through
# `assert_provider_neutral` so a regression fails closed instead of
# silently pinning a vendor.
FORBIDDEN_PROVIDER_SUBSTRINGS = ("groq", "anthropic", "openai_api_key")

DEFAULT_MODEL = "openai/continuum-review"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_BRIDGE_PATH = "/v1"

# The OpenCode agent every inference request runs as. `plan` natively
# denies file edits, which keeps PR-Agent inference reviewer-only without
# any per-request tool override (a per-request `tools` map is rejected by
# the free tier, so the bridge must never send one).
INFERENCE_AGENT = "plan"

REVIEWER_SYSTEM_PROMPT = (
    "You are a read-only code reviewer. Review the provided pull request "
    "diff for correctness, regressions, reliability, races, security, "
    "performance, maintainability, and test gaps. "
    "Never modify, create, or delete files. Never run shell commands that "
    "change state. Report findings as structured text only."
)

BLOCKING_SEVERITIES = ("blocker", "critical", "high")

VERIFY_RESOLVED = "RESOLVED"
VERIFY_UNRESOLVED = "UNRESOLVED"


class PrAgentError(ValueError):
    """Raised when PR-Agent configuration or bridge conversion fails."""


@dataclass(frozen=True)
class ResolvedRuntime:
    """Generic, provider-neutral runtime PR-Agent executes against."""

    enabled: bool
    api_base: str
    model: str
    max_tokens: int
    timeout_seconds: int
    api_key_present: bool

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "api_base": self.api_base,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "api_key_present": self.api_key_present,
        }


def assert_provider_neutral(*values: object) -> None:
    """Fail closed when a generic-path value names a paid provider."""

    for value in values:
        if not isinstance(value, str) or not value:
            continue
        lowered = value.lower()
        for needle in FORBIDDEN_PROVIDER_SUBSTRINGS:
            if needle in lowered:
                raise PrAgentError(
                    f"provider-neutral PR-Agent path must not reference {needle!r}: {value!r}"
                )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _as_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return parsed if parsed > 0 else default


def resolve_runtime(env: Mapping[str, str]) -> ResolvedRuntime:
    """Resolve the generic PR-Agent runtime from environment configuration.

    Only generic knobs are read: enable/disable, backend base, model,
    token limit, timeout, and whether an (optional) key is present. No
    paid-provider variable is ever consulted here.
    """

    enabled = _as_bool(env.get("CONTINUUM_PR_AGENT_ENABLED", ""))
    api_base = (env.get("PR_AGENT_API_BASE") or "").strip()
    if not api_base:
        port = (env.get("PR_AGENT_BRIDGE_PORT") or "18000").strip() or "18000"
        api_base = f"http://127.0.0.1:{port}{DEFAULT_BRIDGE_PATH}"
    model = (env.get("PR_AGENT_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    max_tokens = _as_int(env.get("PR_AGENT_MAX_TOKENS"), DEFAULT_MAX_TOKENS)
    timeout_seconds = _as_int(
        env.get("PR_AGENT_TIMEOUT_SECONDS"), DEFAULT_TIMEOUT_SECONDS
    )
    api_key_present = bool((env.get("PR_AGENT_API_KEY") or "").strip())
    assert_provider_neutral(api_base, model)
    return ResolvedRuntime(
        enabled=enabled,
        api_base=api_base,
        model=model,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        api_key_present=api_key_present,
    )


# -- OpenAI-compatible <-> OpenCode conversion -------------------------------


def openai_messages_to_prompt(messages: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
    """Split chat messages into (system, user_prompt) for the session API.

    `system` messages become the session `system` prompt; every other role
    is concatenated (in order) into one user text part. An empty prompt
    fails closed: LiteLLM always sends at least one user message.
    """

    if not messages:
        raise PrAgentError("chat completions request has no messages")
    system_parts: List[str] = []
    user_parts: List[str] = []
    for message in messages:
        role = str(message.get("role", "")).strip().lower()
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(content or "")
        if role == "system":
            if text:
                system_parts.append(text)
        elif role in ("user", "assistant", "developer"):
            user_parts.append(f"[{role}] {text}" if role != "user" else text)
        else:
            raise PrAgentError(f"unsupported chat message role: {role!r}")
    prompt = "\n\n".join(part for part in user_parts if part).strip()
    if not prompt:
        raise PrAgentError("chat completions request has no user content")
    system = "\n\n".join(system_parts).strip() or REVIEWER_SYSTEM_PROMPT
    return system, prompt


def build_opencode_request(
    messages: Sequence[Mapping[str, Any]],
    model: str,
    max_tokens: int,
) -> Dict[str, Any]:
    """Build the OpenCode `POST /session/:id/message` body for one request.

    Inference hardening is structural: the request pins the read-only
    `plan` agent, carries a reviewer-only system prompt, and never carries
    a per-request `tools` map (the free tier rejects such requests, and
    the plan agent already denies edits). Each bridge request uses a fresh
    session that is deleted afterwards, so there is no cross-request
    contamination.
    """

    assert_provider_neutral(model)
    system, prompt = openai_messages_to_prompt(messages)
    provider_id, _, model_id = model.partition("/")
    provider_id = provider_id.strip() or "opencode"
    model_id = model_id.strip() or model.strip()
    body: Dict[str, Any] = {
        "agent": INFERENCE_AGENT,
        "system": system,
        "parts": [{"type": "text", "text": prompt}],
    }
    if max_tokens > 0:
        body["metadata"] = {"max_tokens": max_tokens}
    void = {"providerID": provider_id, "modelID": model_id}
    body["model"] = void
    return body


def require_inference_hardening(body: Mapping[str, Any]) -> None:
    """Assert a bridge request body cannot mutate the repository."""

    if body.get("agent") != INFERENCE_AGENT:
        raise PrAgentError(
            f"inference must run as the {INFERENCE_AGENT!r} agent, got {body.get('agent')!r}"
        )
    if "tools" in body:
        raise PrAgentError("inference must not carry a per-request tools override")
    system = str(body.get("system", ""))
    if "read-only" not in system.lower() and "never modify" not in system.lower():
        raise PrAgentError("inference must carry the reviewer-only system prompt")


def extract_assistant_text(result: Mapping[str, Any]) -> str:
    """Extract concatenated assistant text from a session message result."""

    parts = result.get("parts", [])
    texts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ]
    text = "".join(texts).strip()
    if not text:
        info = result.get("info", {})
        error = info.get("error") if isinstance(info, dict) else None
        raise PrAgentError(f"OpenCode session returned no assistant text (error={error!r})")
    return text


def opencode_response_to_openai(
    model: str,
    content: str,
    request_id: str = "",
    created: Optional[int] = None,
) -> Dict[str, Any]:
    """Wrap assistant text in the OpenAI chat-completions shape LiteLLM parses."""

    return {
        "id": request_id or f"chatcmpl-{hashlib.sha256(content.encode()).hexdigest()[:12]}",
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def openai_error(status: str, message: str) -> Dict[str, Any]:
    return {"error": {"type": status, "message": message}}


class BridgeUpstreamError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def map_opencode_error(status_code: int, body: str) -> BridgeUpstreamError:
    """Map an OpenCode server failure onto a retryable bridge error."""

    retryable = status_code in (408, 429) or 500 <= status_code <= 599
    kind = "timeout" if status_code in (408, 504) else "upstream_error"
    return BridgeUpstreamError(
        f"opencode server {kind} (status={status_code}): {body[:300]}",
        retryable=retryable,
    )


def post_json(url: str, payload: Mapping[str, Any], timeout: int) -> Tuple[int, str]:
    data = json.dumps(dict(payload)).encode()
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeUpstreamError(f"cannot reach {url}: {exc}", retryable=True) from None


# -- Review-state contract ----------------------------------------------------


@dataclass
class Finding:
    id: str
    path: str
    line: int
    severity: str
    summary: str
    status: str = "open"

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "summary": self.summary,
            "status": self.status,
        }


def finding_id(path: str, line: int, rule: str) -> str:
    """Stable finding identity across repeated reviews of nearby heads."""

    digest = hashlib.sha256(f"{path}:{line}:{rule}".encode()).hexdigest()[:12]
    return f"pra-{digest}"


def parse_findings(text: str) -> List[Finding]:
    """Parse `path:line [severity] rule -- summary` lines into findings."""

    pattern = re.compile(
        r"^(?P<path>\S+):(?P<line>\d+)\s+\[(?P<severity>[a-z]+)\]\s+"
        r"(?P<rule>[a-z0-9_-]+)\s+--\s+(?P<summary>.+)$",
        re.IGNORECASE | re.MULTILINE,
    )
    findings: List[Finding] = []
    for match in pattern.finditer(text or ""):
        severity = match.group("severity").lower()
        findings.append(
            Finding(
                id=finding_id(match.group("path"), int(match.group("line")), match.group("rule").lower()),
                path=match.group("path"),
                line=int(match.group("line")),
                severity=severity,
                summary=match.group("summary").strip(),
            )
        )
    return findings


def track_findings(previous: Sequence[Finding], current: Sequence[Finding]) -> Dict[str, List[Finding]]:
    """Track finding state across repeated reviews instead of duplicating."""

    before = {finding.id: finding for finding in previous}
    now = {finding.id: finding for finding in current}
    persisted = [now[key] for key in now if key in before]
    new = [now[key] for key in now if key not in before]
    fixed = [before[key] for key in before if key not in now]
    for finding in fixed:
        finding.status = "fixed"
    return {"persisted": persisted, "new": new, "fixed": fixed}


def verify_finding(
    finding_id_value: str, previous: Sequence[Finding], current: Sequence[Finding]
) -> str:
    """Machine-detectable `/verify <finding-id>` verdict for one finding."""

    before = {finding.id: finding for finding in previous}
    if finding_id_value not in before:
        raise PrAgentError(f"unknown finding id: {finding_id_value}")
    now = {finding.id for finding in current}
    return VERIFY_RESOLVED if finding_id_value not in now else VERIFY_UNRESOLVED


def decide_review(findings: Sequence[Finding]) -> str:
    """Real review outcome: changes requested iff a blocking finding exists."""

    for finding in findings:
        if finding.severity.lower() in BLOCKING_SEVERITIES and finding.status == "open":
            return "CHANGES_REQUESTED"
    return "APPROVED"


def is_current_head(reviewed_sha: str, head_sha: str) -> bool:
    """A review is valid only for the exact HEAD it evaluated."""

    if not reviewed_sha or not head_sha:
        return False
    return reviewed_sha.strip().lower() == head_sha.strip().lower()


@dataclass
class Chunk:
    index: int
    total: int
    content: str


def chunk_diff(diff: str, max_chars: int = 60000) -> List[Chunk]:
    """Split a large PR diff into bounded chunks for large-PR review."""

    if max_chars <= 0:
        raise PrAgentError("chunk size must be positive")
    text = diff or ""
    if not text:
        return []
    pieces = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    return [Chunk(index=index, total=len(pieces), content=piece) for index, piece in enumerate(pieces)]


@dataclass
class Coverage:
    reviewed: int = 0
    total: int = 0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.reviewed >= self.total


def check_coverage(coverage: Coverage) -> None:
    """Fail closed when required review coverage cannot be established."""

    if coverage.total <= 0:
        raise PrAgentError("review coverage is unknown: refusing to approve an unreviewed diff")
    if coverage.reviewed < coverage.total:
        raise PrAgentError(
            f"review covered {coverage.reviewed}/{coverage.total} chunks: failing closed"
        )


def review_report(
    findings: Sequence[Finding],
    reviewed_sha: str,
    head_sha: str,
    coverage: Coverage,
    issue_refs: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build the review outcome, enforcing current-head and coverage gates."""

    if not is_current_head(reviewed_sha, head_sha):
        return {
            "outcome": "STALE_HEAD",
            "reviewed_sha": reviewed_sha,
            "head_sha": head_sha,
            "findings": [finding.describe() for finding in findings],
            "coverage": {"reviewed": coverage.reviewed, "total": coverage.total},
            "issues": list(issue_refs),
        }
    check_coverage(coverage)
    outcome = decide_review(findings)
    return {
        "outcome": outcome,
        "reviewed_sha": reviewed_sha,
        "head_sha": head_sha,
        "findings": [finding.describe() for finding in findings],
        "coverage": {"reviewed": coverage.reviewed, "total": coverage.total},
        "issues": list(issue_refs),
    }


def disabled_report(reason: str = "PR-Agent is not enabled") -> Dict[str, Any]:
    """Explicit disabled-path outcome: never a silent green no-op."""

    return {"outcome": "DISABLED", "reason": reason, "findings": []}


VERIFY_COMMAND_RE = re.compile(r"(?m)^\s*/verify\s+(?P<id>\S+)\s*$")


def parse_verify_command(body: str) -> Optional[str]:
    """Extract the finding id from a `/verify <finding-id>` comment."""

    match = VERIFY_COMMAND_RE.search(body or "")
    return match.group("id") if match else None


def inline_comment_finding_id(path: str, line: int, body: str) -> str:
    """Stable id for one upstream inline review comment.

    Repeated reviews of the same lines re-derive the same id, so the
    workflow can track finding state instead of creating duplicates.
    """

    digest = hashlib.sha256(f"{path}:{line}:{body}".encode()).hexdigest()[:12]
    return f"pra-{digest}"


def verdict_from_text(text: str) -> str:
    """Machine-detectable verdict from a verification reply.

    `UNRESOLVED` always wins: an ambiguous reply must never be read as a
    resolution. Only an explicit `**RESOLVED**` marker without any
    `UNRESOLVED` mention counts as resolved.
    """

    body = text or ""
    if re.search(r"\bUNRESOLVED\b", body, re.IGNORECASE):
        return VERIFY_UNRESOLVED
    if re.search(r"\*\*RESOLVED(?:\.|\*\*)", body, re.IGNORECASE):
        return VERIFY_RESOLVED
    return VERIFY_UNRESOLVED


@dataclass
class ReviewThread:
    """One review round tracked across repeated reviews of the same PR."""

    head_sha: str = ""
    findings: List[Finding] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)

    def record(self, head_sha: str, findings: Sequence[Finding], coverage: Coverage) -> Dict[str, List[Finding]]:
        transition = track_findings(self.findings, list(findings))
        self.head_sha = head_sha
        self.findings = list(findings)
        self.coverage = coverage
        return transition
