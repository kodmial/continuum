"""Continuum OpenCode routing: one trusted configuration/qualification/state contract.

A route is frozen before an agent begins side effects. A failed task MUST NOT
automatically execute again with another route; its recovery owner owns the
checkpoint and idempotency decision.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
STATE_REF = "continuum-model-state"
STATE_VERSION = 1
# Only bound a real-completion receipt to its original Actions run attempt.
# Long-running consumers may finish after the semantic probe TTL expires.
MAX_EXECUTION_SECONDS = 6 * 60 * 60
MAX_PENDING_EXECUTIONS = 128


class RouteError(RuntimeError):
    pass


class BurnedRunner(RouteError):
    pass


class Conflict(RouteError):
    pass


def bounded(name: str, value: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise RouteError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return int(value)


@dataclass(frozen=True)
class Configuration:
    models: tuple[str, ...]
    pinned: bool
    ttl: int = 3600
    cooldown: int = 900
    timeout: int = 90
    task_class: str = "coder"

    @classmethod
    def parse(cls, pool: str, legacy: str = "", explicit: str = "",
              *, ttl: str = "", cooldown: str = "", timeout: str = "",
              task_class: str = "coder", strategy: str = "sticky-failover"):
        if strategy != "sticky-failover":
            raise RouteError("Unsupported OPENCODE_MODEL_STRATEGY")
        if task_class not in ("coder", "review", "repair"):
            raise RouteError("Unknown model execution class")
        if len(pool) > 2048 or len(legacy) > 160 or len(explicit) > 160:
            raise RouteError("Model configuration too large")
        if pool:
            choices = [s.strip() for s in pool.split(",")]
            if not 1 <= len(choices) <= 8 or any(not s for s in choices):
                raise RouteError("OPENCODE_MODELS contains empty or excessive routes")
            if len(set(choices)) != len(choices):
                raise RouteError("Duplicate OPENCODE_MODELS routes")
            chosen = explicit.strip()
            if chosen and chosen not in choices:
                raise RouteError("Explicit run pin not in trusted OPENCODE_MODELS")
            models = (chosen,) if chosen else tuple(choices)
            pin = bool(chosen)
        else:
            chosen = explicit.strip() or legacy.strip()
            if not chosen:
                raise RouteError("Set trusted OPENCODE_MODELS or legacy OPENCODE_MODEL")
            models = (chosen,)
            pin = True
        if any(not MODEL_ID.fullmatch(model) or any(c in model for c in ("..", "//"))
               for model in models):
            raise RouteError("Malformed provider/model route")
        return cls(models, pin, bounded("probe TTL", ttl, 3600, 60, 86400),
                   bounded("cooldown", cooldown, 900, 60, 86400),
                   bounded("probe timeout", timeout, 90, 15, 240), task_class)

    def fingerprint(self, binary_sha: str, scope: str) -> str:
        data = [list(self.models), self.pinned, self.task_class, binary_sha, scope]
        return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def route_order(config: Configuration, state: dict, now: int) -> tuple[str, ...]:
    cooldowns = state.get("cooldowns") or {}
    allowed = [m for m in config.models if now >= int(cooldowns.get(m, 0))]
    if config.pinned:
        # Pin disables failover, not the circuit breaker.
        return tuple(allowed)
    sticky = state.get("last_successful_real_execution") or {}
    preferred = sticky.get("model")
    if preferred in allowed:
        # An expired probe requires fresh qualification of the *same*
        # last-known-good model, never a silent return to first-list order.
        allowed.remove(preferred)
        allowed.insert(0, preferred)
    return tuple(allowed)


def classify(output: str, rc: int) -> str:
    text = output.lower()
    if "freeusagelimiterror" in text or (rc != 0 and re.search(r"\b429\b|rate.limit|quota", text)):
        return "quota_429"
    if re.search(r"\b401\b|\b403\b|unauthorized|invalid.api.key|authentication", text):
        return "authentication"
    if re.search(r"deprecated|retired|model.not.found|model.unavailable", text):
        return "unavailable"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if re.search(r"\b50[0-9]\b|upstream.request.failed|bad.gateway", text):
        return "upstream"
    return "invalid_response" if rc == 0 else "execution_error"


def extract_response(log: str, expected: str) -> str:
    output = []
    observations = []
    for line in log.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        part = payload.get("part") or {}
        info = payload.get("info") or {}
        if not isinstance(part, dict):
            part = {}
        if not isinstance(info, dict):
            info = {}
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            output.append(part["text"])
        if payload.get("type") == "text" and isinstance(payload.get("text"), str):
            output.append(payload["text"])
        for entry in (part, info, payload):
            provider = entry.get("providerID")
            model = entry.get("modelID")
            if provider and model:
                observations.append(f"{provider}/{model}")
    if observations and any(observed != expected for observed in observations):
        raise RouteError("OpenCode returned a different observed model")
    answer = "\n".join(output).strip()
    if not answer:
        raise RouteError("Official OpenCode returned no assistant text events")
    return answer


def _probe_environment(home: str) -> dict:
    # Do not inherit GH_TOKEN, GITHUB_TOKEN, PATs or provider secrets.
    # Anonymous OpenCode probes use a disposable HOME, no repository checkout.
    keys = ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env.update(HOME=home, XDG_CONFIG_HOME=home + "/config",
               XDG_DATA_HOME=home + "/data", XDG_CACHE_HOME=home + "/cache",
               CI="true")
    return env


def semantic_probe(model: str, kind: str, timeout: int, *, runner=subprocess.run) -> None:
    with tempfile.TemporaryDirectory(prefix="continuum-route-probe-") as scratch:
        if kind == "review":
            prompt = ("Read only: this function must accept age 18: "
                      "def eligible(age): return age > 18. "
                      "State the exact comparison fix; do not modify files.")
        else:
            prompt = ("Use your file editing tool to create a file named "
                      "route-proof.txt in the working directory with exactly "
                      "the contents ROUTE_PROOF_OK. Report when complete.")
        command = ["opencode", "run", "--auto", "--format", "json", "--model", model, prompt]
        try:
            finished = runner(command, cwd=scratch, env=_probe_environment(scratch),
                              capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise RouteError("timeout") from None
        except OSError as error:
            raise RouteError("local_runtime: " + type(error).__name__) from None
        raw = (finished.stdout or "") + "\n" + (finished.stderr or "")
        # Refuse any second model request on a runner that received a 429.
        if classify(raw, finished.returncode) == "quota_429":
            marker = Path(os.environ.get("RUNNER_TEMP", scratch)) / "continuum-opencode-429"
            marker.touch()
            raise BurnedRunner("quota_429")
        if finished.returncode != 0:
            raise RouteError(classify(raw, finished.returncode))
        answer = extract_response(finished.stdout or "", model)
        if kind == "review":
            if ">=" not in answer and "greater than or equal" not in answer.lower():
                raise RouteError("review_capability")
        else:
            proof = Path(scratch) / "route-proof.txt"
            if not proof.is_file() or proof.read_text().strip() != "ROUTE_PROOF_OK":
                raise RouteError("coding_tool_capability")


class GitRefStore:
    """Small, orphan branch-backed state store with non-force fast-forward CAS.

    State belongs to the repository receiving the model request, not to a
    public controller. No private repository name or credential goes in a blob.
    """

    def __init__(self, repository: str, token: str):
        if not REPOSITORY.fullmatch(repository):
            raise RouteError("Invalid state repository")
        if not token:
            raise RouteError("GitHub contents-write token needed for model state")
        self.repo = repository
        self.token = token

    def api(self, method: str, path: str, value: dict | None = None, *, missing=False) -> dict | None:
        url = "https://api.github.com/repos/" + self.repo + "/" + path
        data = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as reply:
                return json.load(reply)
        except urllib.error.HTTPError as err:
            if missing and err.code == 404:
                return None
            if method in ("PATCH", "POST") and err.code in (409, 422):
                raise Conflict("Git ref changed concurrently") from None
            raise RouteError(f"GitHub model state {method} failed ({err.code})") from None
        except (OSError, ValueError):
            raise RouteError("GitHub model state backend unavailable") from None

    def read(self, key: str) -> tuple[dict, str | None, str | None]:
        ref = self.api("GET", "git/ref/heads/" + STATE_REF, missing=True)
        if ref is None:
            return {}, None, None
        head = ref["object"]["sha"]
        commit = self.api("GET", "git/commits/" + head)
        tree = commit["tree"]["sha"]
        listing = self.api("GET", "git/trees/" + tree + "?recursive=1")
        entry = next((entry for entry in listing.get("tree", [])
                      if entry["path"] == "state/" + key + ".json"), None)
        if entry is None:
            return {}, head, tree
        blob = self.api("GET", "git/blobs/" + entry["sha"])
        value = json.loads(base64.b64decode(blob["content"]).decode())
        if not isinstance(value, dict) or value.get("schema") != STATE_VERSION:
            raise RouteError("Unsupported/corrupt durable state schema")
        return value, head, tree

    def update(self, key: str, mutator) -> dict:
        for attempt in range(4):
            current, head, base_tree = self.read(key)
            updated = mutator(dict(current))
            updated["schema"] = STATE_VERSION
            entry = {"path": "state/" + key + ".json", "mode": "100644",
                     "type": "blob", "content": json.dumps(updated, sort_keys=True)}
            tree_body = {"tree": [entry]}
            if base_tree:
                tree_body["base_tree"] = base_tree
            tree = self.api("POST", "git/trees", tree_body)
            commit = self.api("POST", "git/commits",
                              {"message": "chore: route state [skip ci]",
                               "tree": tree["sha"], "parents": [head] if head else []})
            try:
                if head:
                    self.api("PATCH", "git/refs/heads/" + STATE_REF,
                             {"sha": commit["sha"], "force": False})
                else:
                    self.api("POST", "git/refs",
                             {"ref": "refs/heads/" + STATE_REF, "sha": commit["sha"]})
                return updated
            except Conflict:
                if attempt == 3:
                    raise
        raise RouteError("Model state CAS budget exhausted")


def execution_ticket(environ) -> str:
    """Privacy-safe, per-run-attempt identifier; no cross-run attestations."""
    run = environ.get("GITHUB_RUN_ID", "")
    attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    if not (re.fullmatch(r"[0-9]{1,20}", run) and re.fullmatch(r"[0-9]{1,8}", attempt)):
        raise RouteError("Model route requires a trusted Actions run identity")
    return hashlib.sha256((run + "/" + attempt).encode()).hexdigest()


def reserve_completion(state: dict, ticket: str, model: str, now: int,
                       probe_at: int) -> dict:
    """Record qualified-at-selection proof without extending semantic TTL."""
    if not (0 <= now - probe_at <= 86400):
        raise RouteError("Invalid qualification timestamp")
    pending = state.get("qualified_selections") or {}
    if not isinstance(pending, dict):
        raise RouteError("Corrupt model selection state")
    active = {
        key: value for key, value in pending.items()
        if isinstance(value, dict) and isinstance(value.get("at"), int)
        and 0 <= now - value["at"] <= MAX_EXECUTION_SECONDS
    }
    active[ticket] = {"model": model, "at": now, "probe_at": probe_at}
    if len(active) > MAX_PENDING_EXECUTIONS:
        active = dict(sorted(active.items(), key=lambda kv: kv[1]["at"])
                      [-MAX_PENDING_EXECUTIONS:])
    state["qualified_selections"] = active
    return state


def attest_completion(current: dict, ticket: str, model: str, now: int,
                      fingerprint: str, ttl: int, execution_id: str,
                      head: str = "") -> dict:
    """A live success may outlast probe TTL, but never its run-scoped receipt."""
    if current.get("fingerprint") != fingerprint:
        raise RouteError("Stale model state fingerprint")
    pending = current.get("qualified_selections") or {}
    if not isinstance(pending, dict):
        raise RouteError("Corrupt model selection state")
    selected = pending.get(ticket) or {}
    if selected.get("model") != model:
        raise RouteError("No qualified selection for this run and model")
    selected_at = selected.get("at")
    probe_at = selected.get("probe_at")
    if not (isinstance(selected_at, int) and isinstance(probe_at, int)
            and 0 <= selected_at - probe_at <= ttl
            and 0 <= now - selected_at <= MAX_EXECUTION_SECONDS):
        raise RouteError("Model selection proof expired or was not qualified")
    # Never promote the model of a different run, even if it became the
    # most recent successful probe after this operation was selected.
    remaining = dict(pending)
    del remaining[ticket]
    current["qualified_selections"] = remaining
    current["last_successful_real_execution"] = {
        "model": model, "at": now, "run": execution_id, "head": head,
    }
    return current


def route_key(repo: str, profile: str, credentials: str, config: Configuration,
              binary_sha: str) -> str:
    if not profile or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", profile):
        raise RouteError("Invalid trusted route profile")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", credentials):
        raise RouteError("Invalid trusted credential scope")
    identity = [repo.lower(), profile, credentials,
                config.fingerprint(binary_sha, credentials)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def binary_provenance() -> tuple[str, str]:
    executable = shutil.which("opencode")
    if not executable:
        raise RouteError("Official OpenCode binary missing")
    binary = Path(executable).resolve()
    version = subprocess.run([str(binary), "--version"], capture_output=True,
                             text=True, timeout=15, check=True).stdout.strip()
    expected = os.environ.get("CONTINUUM_EXPECTED_OPENCODE_VERSION", "")
    if not SEMVER.fullmatch(version) or (expected and version != expected):
        raise RouteError("OpenCode version differs from verified manifest/override")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return version, digest


def run() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("operation", choices=("select", "record"))
    args = cli.parse_args()
    env = os.environ
    config = Configuration.parse(
        env.get("OPENCODE_MODELS", ""), env.get("OPENCODE_MODEL", ""),
        env.get("CONTINUUM_EXPLICIT_MODEL", ""),
        ttl=env.get("OPENCODE_PROBE_TTL", ""),
        cooldown=env.get("OPENCODE_MODEL_COOLDOWN", ""),
        timeout=env.get("OPENCODE_PROBE_TIMEOUT", ""),
        task_class=env.get("CONTINUUM_MODEL_TASK_CLASS", "coder"),
        strategy=env.get("OPENCODE_MODEL_STRATEGY", "sticky-failover"))
    if args.operation == "select" and (Path(env.get("RUNNER_TEMP", tempfile.gettempdir())) / "continuum-opencode-429").exists():
        raise BurnedRunner("runner already burned")
    version, digest = binary_provenance()
    owner = env.get("CONTINUUM_MODEL_STATE_REPOSITORY") or env.get("GITHUB_REPOSITORY", "")
    credential_scope = env.get("CONTINUUM_MODEL_CREDENTIAL_SCOPE", "anonymous")
    key = route_key(owner, env.get("CONTINUUM_MODEL_PROFILE", "default"),
                    credential_scope, config, digest)
    store = GitRefStore(owner, env.get("CONTINUUM_MODEL_STATE_TOKEN", ""))
    now = int(time.time())
    ticket = execution_ticket(env)
    if args.operation == "record":
        selected = env.get("CONTINUUM_SELECTED_MODEL", "")
        execution_id = env.get("GITHUB_RUN_ID", "") + "/" + env.get("GITHUB_RUN_ATTEMPT", "")
        if selected not in config.models or not execution_id.strip("/"):
            raise RouteError("Cannot attest unspecified execution")
        def record(current):
            return attest_completion(
                current, ticket, selected, now,
                config.fingerprint(digest, credential_scope), config.ttl,
                execution_id, env.get("CONTINUUM_TASK_HEAD", ""))
        store.update(key, record)
        return 0

    state, _, _ = store.read(key)
    fingerprint = config.fingerprint(digest, credential_scope)
    if state.get("fingerprint") != fingerprint:
        state = {}
    order = route_order(config, state, now)
    if not order:
        raise RouteError("All trusted routes are cooling down")
    errors = {}
    for model in order:
        last_probe = state.get("last_successful_probe") or {}
        qualified = (last_probe.get("model") == model
                     and now - int(last_probe.get("at", 0)) <= config.ttl)
        try:
            if not qualified:
                semantic_probe(model, "review" if config.task_class == "review" else "coder",
                               config.timeout)
            def save(current):
                if current.get("fingerprint") != fingerprint:
                    current = {"fingerprint": fingerprint, "cooldowns": {}}
                current.update(schema=STATE_VERSION, fingerprint=fingerprint,
                               runtime={"version": version, "sha256": digest},
                               task_class=config.task_class)
                if not qualified:
                    current["last_successful_probe"] = {"model": model, "at": now}
                # The proof's TTL is evaluated at selection time. The
                # trusted run identity, not probe age at completion, is
                # authoritative for a successful task receipt.
                chosen_probe = current.get("last_successful_probe") or {}
                if chosen_probe.get("model") != model:
                    raise RouteError("Selected route has no qualification")
                at = int(chosen_probe.get("at", 0))
                if not 0 <= now - at <= config.ttl:
                    raise RouteError("Selected probe expired before task start")
                return reserve_completion(current, ticket, model, now, at)
            store.update(key, save)
            if env.get("GITHUB_OUTPUT"):
                with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write("model=" + model + "\n")
            print(model)
            return 0
        except BurnedRunner:
            def pause(current):
                if current.get("fingerprint") != fingerprint:
                    current = {"fingerprint": fingerprint, "cooldowns": {}}
                current.setdefault("cooldowns", {})[model] = now + config.cooldown
                return current
            store.update(key, pause)
            raise
        except RouteError as error:
            errors[model] = str(error)
            def pause(current):
                if current.get("fingerprint") != fingerprint:
                    current = {"fingerprint": fingerprint, "cooldowns": {}}
                current.setdefault("cooldowns", {})[model] = now + config.cooldown
                return current
            store.update(key, pause)
            if config.pinned:
                break
    raise RouteError("No route qualified: " + "; ".join(errors.values()))


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except BurnedRunner:
        print("::error::OpenCode 429: retire this runner without another LLM request", file=sys.stderr)
        raise SystemExit(75)
    except RouteError as error:
        print("::error::Continuum model routing: " + str(error), file=sys.stderr)
        raise SystemExit(2)
