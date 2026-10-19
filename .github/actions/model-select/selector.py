"""Bounded, fail-closed OpenCode model selection and durable anonymous route state.

Work Lock #66. Repository-specific model choices live in configuration, not
the selector source. Never replay an already-started coding/review operation.
This module only qualifies and pins a route *before* side effects begin.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
from typing import Callable

IDENTITY = re.compile(r"^[a-z][a-z0-9_-]*/[a-zA-Z0-9][a-zA-Z0-9._-]{1,120}$")
FAILURE = re.compile(
    r"(authentication required|invalid api key|FreeUsageLimitError|FreeTierError|"
    r"model unavailable|model not found|has been deprecated|upstream request failed|"
    r"HTTP[^0-9]*(?:401|403|429|500|502|503))", re.I)
PROMPT = ("There are 3 boxes with 4 pencils in each box. How many pencils "
          "are there in total? Explain in one sentence.")
DEFAULT_TTL = 3600
DEFAULT_COOLDOWN = 900
STATE_REF = "refs/heads/continuum-model-state"
STATE_SCHEMA = 1


class ConfigError(ValueError):
    """A setting cannot be safely interpreted."""


class NoRoute(RuntimeError):
    """All explicitly approved models failed qualification."""
    def __init__(self, message: str, *, state: dict | None = None,
                 attempts: list[dict] | None = None):
        super().__init__(message)
        self.state = state
        self.attempts = attempts or []


@dataclasses.dataclass(frozen=True)
class Config:
    models: tuple[str, ...]
    explicit_pin: str | None = None
    ttl_seconds: int = DEFAULT_TTL
    cooldown_seconds: int = DEFAULT_COOLDOWN
    probe_timeout: int = 90

    @classmethod
    def from_values(cls, pool: str, explicit: str = "", *, ttl: str = "",
                    cooldown: str = "", timeout: str = "") -> "Config":
        models = [s.strip() for s in pool.split(",")]
        if not pool.strip() or any(not m for m in models):
            raise ConfigError("OPENCODE_MODELS must contain an explicit nonempty ordered list")
        if len(models) > 12 or len(set(models)) != len(models):
            raise ConfigError("Duplicate or excessive OPENCODE_MODELS entries")
        if any(not IDENTITY.fullmatch(m) or not m.endswith("-free") for m in models):
            raise ConfigError("Unrecognized or non-free OpenCode model in allowlist")
        if any(not m.startswith("opencode/") for m in models):
            raise ConfigError("Production pool must contain official opencode/*-free IDs only")
        pin = explicit.strip() or None
        if pin and (not IDENTITY.fullmatch(pin) or pin not in models):
            raise ConfigError("Explicit OPENCODE_MODEL pin must be on the approved allowlist")
        def bounded(s: str, default: int, low: int, high: int) -> int:
            if not s:
                return default
            if not re.fullmatch(r"[0-9]+", s):
                raise ConfigError("Timeout, TTL and cooldown must be integers")
            value = int(s)
            if not low <= value <= high:
                raise ConfigError(f"Configuration value outside {low}..{high}")
            return value
        return cls(tuple(models), pin,
                   bounded(ttl, DEFAULT_TTL, 30, 86400),
                   bounded(cooldown, DEFAULT_COOLDOWN, 60, 86400),
                   bounded(timeout, 90, 20, 180))

    @property
    def fingerprint(self) -> str:
        # Keep generation keyed by ordered allowlist, no credentials in state.
        return hashlib.sha256(json.dumps(self.models).encode()).hexdigest()

    def order(self, state: dict, now: float) -> list[str]:
        if self.explicit_pin:
            return [self.explicit_pin]
        blocked = {
            m for m, value in state.get("cooldowns", {}).items()
            if m in self.models and isinstance(value, (float, int)) and value > now
        }
        eligible = [m for m in self.models if m not in blocked]
        sticky = state.get("last_good")
        if sticky in eligible and state.get("fingerprint") == self.fingerprint:
            try:
                stamp = dt.datetime.fromisoformat(state.get("last_execution", ""))
                age = now - stamp.timestamp()
                if 0 <= age <= self.ttl_seconds:
                    return [sticky] + [m for m in eligible if m != sticky]
            except (TypeError, ValueError):
                pass
        return eligible


def extract_response(stdout: str, *, expected: str = "") -> tuple[str, bool]:
    """Read official OpenCode JSONL text and reject observed wrong-model routing."""
    snippets: list[str] = []
    identity_seen = False
    for row in stdout.splitlines():
        try:
            event = json.loads(row)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") or {}
        if not isinstance(part, dict):
            part = {}
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            snippets.append(part["text"])
        elif event.get("type") == "text" and isinstance(event.get("text"), str):
            snippets.append(event["text"])
        # OpenCode releases sometimes attach model identity to message info or
        # step metadata. If present, check it; absence must be reported, not faked.
        info = event.get("info") or part.get("metadata") or {}
        if isinstance(info, dict):
            observed_model = info.get("modelID") or info.get("modelId")
            observed_provider = info.get("providerID") or info.get("providerId")
            if observed_model and observed_provider:
                identity_seen = True
                if expected and f"{observed_provider}/{observed_model}" != expected:
                    raise NoRoute("Official-client event reports mismatched served model")
    return " ".join(snippets).strip(), identity_seen


def real_probe(model: str, timeout: int, *, runner: Callable = subprocess.run) -> dict:
    """Probe official CLI before task mutation; never use a catalog-only hit."""
    started = time.monotonic()
    try:
        # Never forward GitHub state credentials, provider keys, runner-global
        # sessions or private Child metadata to a model subprocess.
        with tempfile.TemporaryDirectory(prefix="continuum-model-probe-") as scratch:
            safe = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": scratch,
                    "XDG_CONFIG_HOME": scratch + "/config",
                    "XDG_DATA_HOME": scratch + "/data",
                    "XDG_STATE_HOME": scratch + "/state",
                    "XDG_CACHE_HOME": scratch + "/cache",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1",
                    "LANG": "C.UTF-8"}
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
                if key in os.environ:
                    safe[key] = os.environ[key]
            finished = runner(
                ["opencode", "run", "--model", model, "--format", "json", PROMPT],
                capture_output=True, text=True, timeout=timeout, check=False,
                cwd=scratch, env=safe,
            )
        output = finished.stdout or ""
        error = finished.stderr or ""
        detail = output + "\\n" + error
        if FAILURE.search(detail):
            if re.search(r"(429|FreeUsageLimitError|FreeTierError)", detail, re.I):
                reason = "quota_or_429"
            elif re.search(r"(401|403|authentication required|invalid api key)", detail, re.I):
                reason = "authorization"
            elif re.search(r"(deprecated|model unavailable|model not found)", detail, re.I):
                reason = "model_retired"
            elif re.search(r"(500|502|503|upstream request failed)", detail, re.I):
                reason = "upstream_5xx"
            else:
                reason = "provider_error"
            return {"ok": False, "reason": reason,
                    "seconds": round(time.monotonic()-started, 2)}
        if finished.returncode != 0:
            return {"ok": False, "reason": "client_or_provider_exit", "seconds": round(time.monotonic()-started, 2)}
        try:
            answer, observed = extract_response(output, expected=model)
        except NoRoute:
            return {"ok": False, "reason": "served_model_mismatch", "seconds": round(time.monotonic()-started, 2)}
        correct = bool(re.search(r"\b12\b", answer) and re.search("pencils?", answer, re.I))
        return {"ok": correct, "reason": "semantic_pass" if correct else "invalid_answer",
                "seconds": round(time.monotonic()-started, 2),
                "identity_observed": observed}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "seconds": round(time.monotonic()-started, 2)}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def blank_state(config: Config) -> dict:
    return {"schema": STATE_SCHEMA, "fingerprint": config.fingerprint, "last_good": "",
            "last_probe": "", "last_execution": "", "cooldowns": {}}


def state_key(*, repo: str, profile: str, identity_scope: str, config: Config) -> str:
    """Names are hashed; never expose private Child IDs in public state paths."""
    if identity_scope != "anonymous":
        # Independent credential scopes need an explicit, separately approved
        # private state backend; never borrow anonymous results.
        raise ConfigError("Credentialed model state requires an isolated scope")
    if not repo or not profile or len(profile) > 128:
        raise ConfigError("Missing bounded execution scope")
    fields = [repo, profile, identity_scope, config.fingerprint]
    return hashlib.sha256("\0".join(fields).encode()).hexdigest()


def state_path(key: str) -> str:
    return f"scopes/{key}.json"


class GitHubState:
    """Optimistic branch update: GitHub rejects non-fast-forward competing commits.

    Uses only existing GH_TOKEN permissions. The service state lives on a
    separate Git ref and never writes default/main; all data are public-safe.
    """

    def __init__(self, repository: str, token: str, *, runner: Callable = subprocess.run):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ConfigError("Invalid GitHub repository")
        if not token:
            raise ConfigError("GitHub state writes require an existing authorized token")
        self.repo, self.token, self.runner = repository, token, runner

    def api(self, method: str, endpoint: str, data: dict | None = None, *,
            optional: bool = False) -> dict | None:
        import urllib.error
        import urllib.request
        uri = "https://api.github.com/repos/" + self.repo + "/" + endpoint
        body = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        req = urllib.request.Request(
            uri, data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if optional and exc.code == 404:
                return None
            if exc.code in (409, 422):
                raise ConcurrentWrite("GitHub state reference changed or request conflicted") from None
            raise RuntimeError(f"GitHub state API {method} {endpoint.split('?')[0]} HTTP {exc.code}") from None

    def read(self, path: str) -> tuple[dict | None, str | None, str | None]:
        reference = self.api("GET", "git/ref/heads/continuum-model-state", optional=True)
        if reference is None:
            return None, None, None
        head = reference["object"]["sha"]
        commit = self.api("GET", "git/commits/" + head)
        tree_sha = commit["tree"]["sha"]
        import base64
        content = self.api("GET", "contents/" + path + "?ref=continuum-model-state", optional=True)
        if content is None:
            return None, head, tree_sha
        state = json.loads(base64.b64decode(content["content"]))
        if not isinstance(state, dict):
            raise ConfigError("Invalid durable model state")
        return state, head, tree_sha

    def write(self, path: str, state: dict, *, expected_head: str | None = None, retries: int = 4) -> None:
        for _ in range(retries):
            current, head, tree_sha = self.read(path)
            # Optimistic head check: never overwrite an intervening successful
            # execution's sticky state with a stale probe. Caller must reread.
            if head != expected_head:
                raise ConcurrentWrite("State advanced concurrently; re-resolve model safely")
            payload = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
            blob = self.api("POST", "git/blobs", {"content": payload, "encoding": "utf-8"})["sha"]
            tree = self.api("POST", "git/trees", {
                **({"base_tree": tree_sha} if tree_sha else {}),
                "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob}],
            })["sha"]
            if head is None:
                # The independent state ref is born from the current default
                # commit, but only the *state ref* is modified.
                base = self.api("GET", "git/ref/heads/main")["object"]["sha"]
                commit = self.api("POST", "git/commits",
                                  {"message": "continuum: initialize model state",
                                   "tree": tree, "parents": [base]})["sha"]
                try:
                    self.api("POST", "git/refs",
                             {"ref": STATE_REF, "sha": commit})
                    return
                except ConcurrentWrite:
                    raise
            commit = self.api("POST", "git/commits", {
                "message": "continuum: update model qualification state",
                "tree": tree, "parents": [head],
            })["sha"]
            try:
                self.api("PATCH", "git/refs/heads/continuum-model-state",
                         {"sha": commit, "force": False})
                return
            except ConcurrentWrite:
                raise
        raise ConcurrentWrite("Concurrent state writes exceeded bounded retries")


class ConcurrentWrite(RuntimeError):
    pass


def select(config: Config, state: dict, *, probe: Callable = real_probe,
           now: float | None = None) -> tuple[str, dict, list[dict]]:
    """Try allowed candidates in order; probes cannot mutate project files."""
    now = time.time() if now is None else now
    data = dict(blank_state(config))
    if state.get("schema") == STATE_SCHEMA and state.get("fingerprint") == config.fingerprint:
        data.update(state)
    data["cooldowns"] = dict(data.get("cooldowns") or {})
    attempts: list[dict] = []
    for candidate in config.order(data, now):
        result = probe(candidate, config.probe_timeout)
        attempts.append({"model": candidate, **result})
        if result["ok"]:
            data.update(last_probe_model=candidate, last_probe=utc_now())
            data["cooldowns"].pop(candidate, None)
            return candidate, data, attempts
        # A missing/retired/429 route is cooled down only until later attempts.
        if not config.explicit_pin:
            data["cooldowns"][candidate] = now + config.cooldown_seconds
    raise NoRoute("No approved model passed real anonymous OpenCode inference",
                  state=data, attempts=attempts)
