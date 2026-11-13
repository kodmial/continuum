#!/usr/bin/env python3
"""Minimal OpenAI-compatible bridge from PR-Agent to `opencode serve`.

Protocol reality: `opencode serve` is NOT OpenAI Chat Completions
compatible. It speaks an asynchronous session/message API (`POST /session`,
then select a read-only agent and model through `POST /api/session/:id/agent`
and `POST /api/session/:id/model`, enqueue `POST /api/session/:id/prompt`,
wait on `POST /api/experimental/session/:id/wait`, read a completed assistant
record from `GET /api/session/:id/message`, and delete the isolated session.
The v2 server requires loopback Basic auth even for this private workflow.
PR-Agent reaches its model through LiteLLM, which
routes any `openai/<name>` model to an OpenAI-compatible
`POST /v1/chat/completions` endpoint. This bridge exposes exactly that
surface and translates each completion into one isolated OpenCode
session: a fresh session per request, deleted afterwards, so requests
never contaminate each other.

Inference hardening:
- every request runs as the read-only `plan` agent with a reviewer-only
  system prompt; the bridge never sends a per-request `tools` map and the
  server runs with its default tool surface (both overrides are rejected
  by the free tier). The bridge creates each session with an explicit
  session-level `Permission.Ruleset` that denies the v2 `shell` and
  `subagent` tool actions, and verifies the server reported those rules
  before submitting the prompt;
- the server and the bridge both bind 127.0.0.1 only. A Docker action
  cannot reach runner loopback, so the workflow runs the pinned PR-Agent
  package directly on the runner instead of in a container;
- the full OpenCode control API is never exposed: only the chat
  completions (plus models/health) routes exist here.

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.pr_agent import (  # noqa: E402
    BridgeUpstreamError,
    DEFAULT_MAX_TOKENS,
    PrAgentError,
    build_opencode_request,
    extract_assistant_text,
    map_opencode_error,
    openai_error,
    opencode_response_to_openai,
    require_inference_hardening,
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def server_url() -> str:
    return _env("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")


def inference_model(request_model: str) -> str:
    """The configured OpenCode provider/model wins; the request names the route."""

    configured = _env("OPENCODE_MODEL", "")
    if configured:
        return configured
    raise BridgeUpstreamError("Frozen OpenCode route was not configured", retryable=False)


def split_model(model: str) -> Tuple[str, str]:
    provider, _, rest = model.partition("/")
    return (provider.strip() or "opencode", (rest.strip() or model.strip()))


#: Session-level permission rules (the v2 `Permission.Ruleset` shape) applied
#: to every reviewer session. OpenCode evaluates session rules after agent
#: rules with last-match-wins, so these denies override the `plan` agent's
#: wildcard allows: the reviewer cannot execute shell commands or delegate to
#: a subagent that would. The actions are the pinned v2 tool ids that assert
#: them (`shell` and `subagent`), not the legacy v1 names (`bash`/`task`).
#: Per-request `tools` overrides and server config permissions stay unused
#: because the free tier rejects both.
REVIEWER_SESSION_PERMISSIONS: Tuple[Dict[str, str], ...] = (
    {"action": "shell", "resource": "*", "effect": "deny"},
    {"action": "subagent", "resource": "*", "effect": "deny"},
)

#: Distinct `--preflight` exit status for a retryable infrastructure failure
#: (HTTP 429/5xx). The workflow retries only this status so a protocol
#: mismatch (plain 1) can never consume the reviewer retry budget.
PREFLIGHT_RETRYABLE_EXIT = 3

# The v2 assistant finish reasons (`FinishReason` in the pinned schema).
_FINAL_ASSISTANT_FINISH = "stop"
_KNOWN_ASSISTANT_FINISHES = (
    "stop", "length", "tool-calls", "content-filter", "error", "unknown",
)


def _bridge_timing(stage: str, started: float, outcome: str) -> None:
    """Structured, content-free evidence; never print prompts, paths or IDs."""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(
        f"bridge-phase stage={stage} elapsed_ms={elapsed_ms} outcome={outcome}",
        file=sys.stderr,
        flush=True,
    )


def opencode_call(method: str, path: str, payload: Dict[str, Any] | None, timeout: int) -> Tuple[int, str]:
    import urllib.error

    # Fixed labels avoid logging OpenCode session ids or request bodies.
    if method == "POST" and path == "/api/session":
        stage = "create-session"
    elif method == "POST" and path.endswith("/prompt"):
        stage = "submit-async"
    elif method == "POST" and path.endswith("/wait"):
        stage = "wait-for-completion"
    elif method == "POST" and path.endswith("/agent"):
        stage = "select-agent"
    elif method == "POST" and path.endswith("/model"):
        stage = "select-model"
    elif method == "GET" and path.endswith("/message"):
        stage = "read-messages"
    elif method == "GET" and path.startswith("/api/session/"):
        stage = "read-session"
    elif method == "POST" and path.endswith("/message"):
        stage = "send-message"
    elif method == "DELETE" and path.startswith("/api/session/"):
        stage = "delete-session"
    else:
        stage = "other"
    # Every request uses the same version-pinned v2 server credential. Never
    # silently fall back to an unauthenticated legacy endpoint.
    password = _env("OPENCODE_PASSWORD")
    if not password:
        raise BridgeUpstreamError("OpenCode v2 bridge password not configured", retryable=False)
    token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
    started = time.monotonic()
    _bridge_timing(stage, started, "started")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        server_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Basic " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode()
            _bridge_timing(stage, started, f"http_{response.status}")
            return response.status, content
    except urllib.error.HTTPError as exc:
        # Upstream error bodies may contain prompts or secrets: retain only
        # the authenticated HTTP status for classification and diagnostics.
        exc.close()
        _bridge_timing(stage, started, f"http_{exc.code}")
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _bridge_timing(stage, started, type(exc).__name__)
        raise BridgeUpstreamError(f"cannot reach opencode server: {exc}", retryable=True) from None


def _require_opencode_status(status: int, accepted: tuple[int, ...], stage: str) -> None:
    """Fail closed with status-only diagnostics, never upstream response text."""
    if status not in accepted:
        raise BridgeUpstreamError(
            f"OpenCode v2 {stage} rejected (status={status})",
            retryable=status == 429 or status >= 500,
        )


def _session_info(session_id: str, stage: str = "read session") -> Dict[str, Any]:
    """Fetch one v2 session record through the authenticated control API."""
    quoted = urllib.parse.quote(session_id, safe="")
    status, raw = opencode_call("GET", f"/api/session/{quoted}", None, 15)
    _require_opencode_status(status, (200,), stage)
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        raise BridgeUpstreamError(
            "malformed OpenCode v2 session envelope", retryable=False
        ) from None
    info = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(info, dict):
        raise BridgeUpstreamError(
            "invalid OpenCode v2 session envelope", retryable=False
        )
    return info


def _require_reviewer_session_rules(info: Dict[str, Any]) -> None:
    """Fail closed unless the server reports the reviewer deny rules.

    The session is only safe when OpenCode itself returns the ruleset we
    requested: a server that ignored `permissions` must never run the
    reviewer prompt with the agent's wildcard defaults still in force.
    """
    permissions = info.get("permissions")
    if not isinstance(permissions, list):
        raise BridgeUpstreamError(
            "OpenCode v2 session did not report reviewer permissions",
            retryable=False,
        )
    for rule in REVIEWER_SESSION_PERMISSIONS:
        if not any(
            isinstance(candidate, dict)
            and candidate.get("action") == rule["action"]
            and candidate.get("resource") == rule["resource"]
            and candidate.get("effect") == rule["effect"]
            for candidate in permissions
        ):
            raise BridgeUpstreamError(
                "OpenCode v2 session did not enforce reviewer permissions",
                retryable=False,
            )


def _await_async_assistant(session_id: str, timeout: int) -> Dict[str, Any]:
    """A v2 enqueue receipt is NOT a finished review.

    The v2 idle barrier bounds processing, but neither the barrier nor an
    HTTP 200 admission proves execution succeeded. The session's recorded
    terminal outcome must be `succeeded`, and the newest assistant message
    must be a completed, non-error, final answer (`finish == "stop"`). An
    intermediate step (for example `finish == "tool-calls"`) or an older
    text record is never returned as the review.
    """
    if timeout <= 0:
        raise BridgeUpstreamError("invalid OpenCode completion watchdog", retryable=False)
    quoted = urllib.parse.quote(session_id, safe="")
    status, _ = opencode_call(
        "POST", f"/api/experimental/session/{quoted}/wait", None, timeout
    )
    _require_opencode_status(status, (204,), "wait")
    outcome = _session_info(session_id, "read session outcome").get("outcome")
    if outcome != "succeeded":
        label = outcome if outcome in ("failed", "interrupted") else "unknown"
        raise BridgeUpstreamError(
            f"OpenCode v2 session did not succeed (outcome={label})",
            retryable=True,
        )
    status, raw = opencode_call(
        "GET", f"/api/session/{quoted}/message?order=desc&type=assistant&limit=20", None, 12
    )
    _require_opencode_status(status, (200,), "message list")
    try:
        response = json.loads(raw)
    except (TypeError, ValueError):
        raise BridgeUpstreamError("malformed OpenCode v2 message page", retryable=False) from None
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise BridgeUpstreamError("invalid OpenCode v2 message envelope", retryable=False)
    latest = next(
        (
            entry for entry in response["data"]
            if isinstance(entry, dict) and entry.get("type") == "assistant"
        ),
        None,
    )
    if latest is None:
        raise BridgeUpstreamError(
            "OpenCode v2 returned no assistant response", retryable=True
        )
    time_info = latest.get("time")
    if not isinstance(time_info, dict) or time_info.get("completed") is None:
        raise BridgeUpstreamError(
            "OpenCode v2 assistant response is not completed", retryable=True
        )
    if latest.get("error"):
        raise BridgeUpstreamError(
            "OpenCode v2 assistant finished with error", retryable=True
        )
    finish = latest.get("finish")
    if finish != _FINAL_ASSISTANT_FINISH:
        label = finish if finish in _KNOWN_ASSISTANT_FINISHES else "missing"
        raise BridgeUpstreamError(
            f"OpenCode v2 assistant response is not final (finish={label})",
            retryable=True,
        )
    content = latest.get("content")
    if not isinstance(content, list):
        raise BridgeUpstreamError(
            "OpenCode v2 assistant response has no content", retryable=True
        )
    text = "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ).strip()
    if not text:
        raise BridgeUpstreamError(
            "OpenCode v2 assistant response has no text", retryable=True
        )
    # Compatibility shape for PR-Agent's existing strict text extractor.
    # This is backed exclusively by a real, completed, final v2 assistant.
    return {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": text}]}


def _delete_review_session(session_id: str) -> None:
    """Best-effort deletion; review evidence is never inferred from cleanup."""
    quoted = urllib.parse.quote(session_id, safe="")
    try:
        opencode_call("DELETE", f"/api/session/{quoted}", None, 15)
    except BridgeUpstreamError:
        pass


def _create_review_session(model: Dict[str, str]) -> str:
    """Native authenticated v2 control path shared by review and preflight."""
    status, raw = opencode_call(
        "POST",
        "/api/session",
        {
            "title": "pr-agent-bridge",
            # Explicit reviewer-only rules, evaluated after agent rules.
            "permissions": [dict(rule) for rule in REVIEWER_SESSION_PERMISSIONS],
        },
        15,
    )
    _require_opencode_status(status, (200,), "create session")
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError):
        raise BridgeUpstreamError(
            "malformed OpenCode v2 session envelope", retryable=False
        ) from None
    session = envelope.get("data") if isinstance(envelope, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise BridgeUpstreamError("OpenCode v2 returned no session id", retryable=False)
    quoted = urllib.parse.quote(session_id, safe="")
    try:
        # The server, not the prompt, owns this restriction: verify it took.
        _require_reviewer_session_rules(
            _session_info(session_id, "verify session permissions")
        )
        status, _ = opencode_call(
            "POST", f"/api/session/{quoted}/agent", {"agent": "plan"}, 15
        )
        _require_opencode_status(status, (204,), "select read-only agent")
        status, _ = opencode_call(
            "POST", f"/api/session/{quoted}/model", {"model": model}, 15
        )
        _require_opencode_status(status, (204,), "select pinned model")
    except Exception:
        _delete_review_session(session_id)
        raise
    return session_id


def preflight_review_session() -> None:
    """No-LLM ABI check on the actual pinned OpenCode v2 server.

    Uses exactly the same authenticated session/agent/model control path as
    real review, and never spends reviewer tokens or modifies the worktree.
    """
    provider, model_id = split_model(inference_model(""))
    session_id = _create_review_session({"providerID": provider, "id": model_id})
    _delete_review_session(session_id)


def run_completion(body: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    messages = body.get("messages", [])
    request_model = str(body.get("model", "") or "")
    max_tokens = body.get("max_tokens", None)
    try:
        limit = int(max_tokens) if max_tokens is not None else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_TOKENS
    opencode_body = build_opencode_request(messages, inference_model(request_model), limit)
    require_inference_hardening(opencode_body)

    session_id = _create_review_session({
        "providerID": opencode_body["model"]["providerID"],
        "id": opencode_body["model"]["modelID"],
    })
    quoted = urllib.parse.quote(session_id, safe="")
    try:
        # Preserve the immutable reviewer-only system policy in the prompt
        # text because v2's admission body no longer has a system field.
        # The server-side 'plan' agent remains read-only independently.
        text = opencode_body["system"] + "\n\nReview request:\n" + (
            opencode_body["parts"][0]["text"]
        )
        status, raw = opencode_call(
            "POST", f"/api/session/{quoted}/prompt", {"text": text}, 15
        )
        _require_opencode_status(status, (200,), "enqueue prompt")
        try:
            admission = json.loads(raw)
        except (ValueError, TypeError):
            raise BridgeUpstreamError("malformed OpenCode v2 prompt receipt", retryable=False) from None
        if not isinstance(admission, dict) or not isinstance(admission.get("data"), dict):
            raise BridgeUpstreamError("invalid OpenCode v2 prompt admission", retryable=False)
        completed = _await_async_assistant(session_id, timeout)
        text = extract_assistant_text(completed)
        return opencode_response_to_openai(
            request_model or inference_model(request_model), text
        )
    finally:
        _delete_review_session(session_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "ContinuumPrAgentBridge/1"

    def log_message(self, *args: object) -> None:  # quieter runner logs
        sys.stderr.write("bridge: %s\n" % (args[0] % tuple(args[1:])))

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = _env("BRIDGE_API_KEY", "")
        if not expected:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/healthz", "/v1/healthz"):
            self._send(200, {"ok": True})
            return
        if path in ("/models", "/v1/models"):
            model = inference_model("")
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "continuum",
                        }
                    ],
                },
            )
            return
        self._send(404, openai_error("not_found", f"unknown path: {self.path}"))

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/chat/completions", "/v1/chat/completions"):
            self._send(404, openai_error("not_found", f"unknown path: {self.path}"))
            return
        if not self._authorized():
            self._send(401, openai_error("unauthorized", "invalid bridge bearer token"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send(400, openai_error("invalid_request", f"invalid JSON: {exc}"))
            return
        try:
            timeout = int(_env("PR_AGENT_TIMEOUT_SECONDS", "600") or "600")
        except ValueError:
            print(
                "bridge: PR_AGENT_TIMEOUT_SECONDS is not an integer; "
                "falling back to 600 seconds",
                file=sys.stderr,
                flush=True,
            )
            timeout = 600
        if not isinstance(body, dict):
            self._send(
                400,
                openai_error("invalid_request", "request body must be a JSON object"),
            )
            return
        messages = body.get("messages", [])
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            # The caller's request shape is validated here so a malformed
            # `messages` value is a 400, never an upstream "malformed
            # opencode response" 502.
            self._send(
                400,
                openai_error(
                    "invalid_request",
                    "messages must be a list of JSON objects",
                ),
            )
            return
        started = time.monotonic()
        try:
            self._send(200, run_completion(body, timeout))
            outcome = "http_200"
        except PrAgentError as exc:
            self._send(400, openai_error("invalid_request", str(exc)))
            outcome = "http_400"
        except BridgeUpstreamError as exc:
            # Python socket read timeout says "timed out", not "timeout".
            # Expose a 504 to reviewer routing, not a generic 502.
            message = str(exc).lower()
            timeout_detected = any(
                token in message for token in ("timeout", "timed out")
            ) or "session exceeded its configured completion watchdog" in message
            status = 504 if timeout_detected else 502
            self._send(status, openai_error("upstream_error", str(exc)))
            outcome = f"http_{status}"
        except (ValueError, AttributeError, TypeError) as exc:
            # A malformed OpenCode response (non-JSON or non-object session
            # payload) must still reach the reviewer as a classified status,
            # never as a dropped connection.
            self._send(
                502,
                openai_error(
                    "upstream_error",
                    "malformed opencode response: {}".format(type(exc).__name__),
                ),
            )
            outcome = "http_502"
        except (BrokenPipeError, ConnectionResetError):
            # The reviewer has already ended the HTTP request. Record the
            # disconnection rather than misattribute it to OpenCode.
            outcome = "client_disconnected"
        finally:
            _bridge_timing("client-request", started, outcome if "outcome" in locals() else "exception")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PR_AGENT_BRIDGE_PORT", "18000")))
    parser.add_argument("--hostname", default="127.0.0.1")
    parser.add_argument("--preflight", action="store_true",
                        help="Verify v2 session ABI on the pinned server without LLM usage")
    args = parser.parse_args()
    if args.preflight:
        try:
            preflight_review_session()
        except BridgeUpstreamError as exc:
            # Status-only diagnostics: never echo an upstream prompt body.
            print(f"PR-Agent/OpenCode v2 preflight failed: {exc}", file=sys.stderr)
            # The workflow retries only the distinct retryable status, so a
            # protocol mismatch (plain 1) never consumes reviewer budget.
            return PREFLIGHT_RETRYABLE_EXIT if exc.retryable else 1
        print("PR-Agent/OpenCode v2 native session API qualified.", flush=True)
        return 0
    if args.hostname not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind beyond loopback", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.hostname, args.port), Handler)
    print(
        f"pr-agent bridge on http://{args.hostname}:{args.port}/v1 "
        f"-> {server_url()} (model={inference_model('')})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
