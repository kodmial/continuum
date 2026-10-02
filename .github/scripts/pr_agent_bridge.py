#!/usr/bin/env python3
"""Minimal OpenAI-compatible bridge from PR-Agent to `opencode serve`.

Protocol reality: `opencode serve` is NOT OpenAI Chat Completions
compatible. It speaks a session/message API (`POST /session`, then
`POST /session/:id/message` with `{model, agent, system, parts}`, then
`DELETE /session/:id`). PR-Agent reaches its model through LiteLLM, which
routes any `openai/<name>` model to an OpenAI-compatible
`POST /v1/chat/completions` endpoint. This bridge exposes exactly that
surface and translates each completion into one isolated OpenCode
session: a fresh session per request, deleted afterwards, so requests
never contaminate each other.

Inference hardening (all verified free-tier compatible):
- every request runs as the read-only `plan` agent with a reviewer-only
  system prompt; the bridge never sends a per-request `tools` map and the
  server runs with its default tool surface (both overrides are rejected
  by the free tier, so hardening lives in the agent selection plus the
  workflow's post-run git-clean guard, not in request/server overrides);
- the server and the bridge both bind 127.0.0.1 only. A Docker action
  cannot reach runner loopback, so the workflow runs the pinned PR-Agent
  package directly on the runner instead of in a container;
- the full OpenCode control API is never exposed: only the chat
  completions (plus models/health) routes exist here.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
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
    return (request_model or "").strip() or "opencode/continuum-review"


def split_model(model: str) -> Tuple[str, str]:
    provider, _, rest = model.partition("/")
    return (provider.strip() or "opencode", (rest.strip() or model.strip()))


def opencode_call(method: str, path: str, payload: Dict[str, Any] | None, timeout: int) -> Tuple[int, str]:
    import urllib.error

    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        server_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeUpstreamError(f"cannot reach opencode server: {exc}", retryable=True) from None


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

    status, raw = opencode_call("POST", "/session", {"title": "pr-agent-bridge"}, timeout)
    if status != 200:
        raise map_opencode_error(status, raw)
    session_id = json.loads(raw).get("id", "")
    if not session_id:
        raise BridgeUpstreamError("opencode server returned no session id", retryable=True)
    try:
        status, raw = opencode_call(
            "POST", f"/session/{session_id}/message", opencode_body, timeout
        )
        if status != 200:
            raise map_opencode_error(status, raw)
        result = json.loads(raw)
        text = extract_assistant_text(result)
        return opencode_response_to_openai(request_model or inference_model(request_model), text)
    finally:
        try:
            opencode_call("DELETE", f"/session/{session_id}", None, 15)
        except BridgeUpstreamError:
            pass


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
        timeout = int(_env("PR_AGENT_TIMEOUT_SECONDS", "180") or "180")
        try:
            self._send(200, run_completion(body, timeout))
        except PrAgentError as exc:
            self._send(400, openai_error("invalid_request", str(exc)))
        except BridgeUpstreamError as exc:
            status = 504 if "timeout" in str(exc).lower() else 502
            self._send(status, openai_error("upstream_error", str(exc)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PR_AGENT_BRIDGE_PORT", "18000")))
    parser.add_argument("--hostname", default="127.0.0.1")
    args = parser.parse_args()
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
