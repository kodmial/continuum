"""Deterministic tests for the reusable PR-Agent + OpenCode backend."""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest

from continuum import pr_agent
from continuum.pr_agent import (
    BLOCKING_SEVERITIES,
    Coverage,
    PrAgentError,
    ReviewThread,
)


def runtime_env(**overrides: str) -> dict:
    env = {
        "CONTINUUM_PR_AGENT_ENABLED": "",
        "PR_AGENT_API_BASE": "",
        "PR_AGENT_MODEL": "",
        "PR_AGENT_MAX_TOKENS": "",
        "PR_AGENT_TIMEOUT_SECONDS": "",
        "PR_AGENT_API_KEY": "",
        "PR_AGENT_BRIDGE_PORT": "",
    }
    env.update(overrides)
    return env


class ProviderConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        runtime = pr_agent.resolve_runtime(runtime_env())
        self.assertFalse(runtime.enabled)
        self.assertTrue(runtime.api_base.startswith("http://127.0.0.1:"))
        self.assertEqual(runtime.model, pr_agent.DEFAULT_MODEL)
        self.assertEqual(runtime.max_tokens, pr_agent.DEFAULT_MAX_TOKENS)

    def test_generic_contract_is_configuration(self):
        runtime = pr_agent.resolve_runtime(
            runtime_env(
                CONTINUUM_PR_AGENT_ENABLED="true",
                PR_AGENT_API_BASE="http://127.0.0.1:18001/v1",
                PR_AGENT_MODEL="openai/continuum-review",
                PR_AGENT_MAX_TOKENS="2048",
                PR_AGENT_API_KEY="optional-key",
            )
        )
        self.assertTrue(runtime.enabled)
        self.assertEqual(runtime.api_base, "http://127.0.0.1:18001/v1")
        self.assertEqual(runtime.model, "openai/continuum-review")
        self.assertEqual(runtime.max_tokens, 2048)
        self.assertTrue(runtime.api_key_present)

    def test_no_groq_hard_coding_in_defaults_or_resolution(self):
        runtime = pr_agent.resolve_runtime(runtime_env())
        for value in (runtime.api_base, runtime.model):
            self.assertNotIn("groq", value.lower())
        self.assertIn("groq", pr_agent.FORBIDDEN_PROVIDER_SUBSTRINGS)

    def test_groq_reference_fails_closed(self):
        with self.assertRaises(PrAgentError):
            pr_agent.resolve_runtime(runtime_env(PR_AGENT_MODEL="groq/llama-3"))
        with self.assertRaises(PrAgentError):
            pr_agent.resolve_runtime(
                runtime_env(PR_AGENT_API_BASE="https://api.groq.com/openai/v1")
            )

    def test_no_paid_provider_key_is_required(self):
        runtime = pr_agent.resolve_runtime(
            runtime_env(CONTINUUM_PR_AGENT_ENABLED="1", PR_AGENT_MODEL="openai/continuum-review")
        )
        self.assertTrue(runtime.enabled)
        self.assertFalse(runtime.api_key_present)


class BridgeConversionTests(unittest.TestCase):
    def test_system_and_user_messages_split(self):
        system, prompt = pr_agent.openai_messages_to_prompt(
            [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Review this diff."},
            ]
        )
        self.assertEqual(system, "Be terse.")
        self.assertEqual(prompt, "Review this diff.")

    def test_missing_system_falls_back_to_reviewer_prompt(self):
        system, prompt = pr_agent.openai_messages_to_prompt(
            [{"role": "user", "content": "Review this diff."}]
        )
        self.assertIn("read-only", system.lower())
        self.assertEqual(prompt, "Review this diff.")

    def test_external_system_prompt_is_wrapped_by_reviewer_policy(self):
        body = pr_agent.build_opencode_request(
            [
                {"role": "system", "content": "You are PR-Reviewer. Focus on correctness."},
                {"role": "user", "content": "Review this diff."},
            ],
            "openai/continuum-review",
            4096,
        )
        self.assertIn(pr_agent.REVIEWER_SYSTEM_PROMPT, body["system"])
        self.assertIn("You are PR-Reviewer", body["system"])
        pr_agent.require_inference_hardening(body)

    def test_empty_messages_fail_closed(self):
        with self.assertRaises(PrAgentError):
            pr_agent.openai_messages_to_prompt([])
        with self.assertRaises(PrAgentError):
            pr_agent.openai_messages_to_prompt([{"role": "system", "content": "x"}])

    def test_build_request_pins_plan_agent_without_tools(self):
        body = pr_agent.build_opencode_request(
            [{"role": "user", "content": "Review."}], "openai/continuum-review", 1024
        )
        self.assertEqual(body["agent"], pr_agent.INFERENCE_AGENT)
        self.assertNotIn("tools", body)
        self.assertEqual(body["parts"], [{"type": "text", "text": "Review."}])
        self.assertEqual(
            body["model"], {"providerID": "openai", "modelID": "continuum-review"}
        )
        pr_agent.require_inference_hardening(body)

    def test_hardening_rejects_tools_override_and_foreign_agent(self):
        good = pr_agent.build_opencode_request(
            [{"role": "user", "content": "Review."}], "openai/continuum-review", 1024
        )
        mutated = dict(good)
        mutated["tools"] = {}
        with self.assertRaises(PrAgentError):
            pr_agent.require_inference_hardening(mutated)
        mutated = dict(good)
        mutated["agent"] = "build"
        with self.assertRaises(PrAgentError):
            pr_agent.require_inference_hardening(mutated)

    def test_extract_and_wrap_roundtrip(self):
        result = {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "text", "text": "Found one issue. "},
                {"type": "text", "text": "See details."},
                {"type": "step-start"},
            ],
        }
        text = pr_agent.extract_assistant_text(result)
        self.assertEqual(text, "Found one issue. See details.")
        wrapped = pr_agent.opencode_response_to_openai("openai/continuum-review", text)
        self.assertEqual(wrapped["object"], "chat.completion")
        self.assertEqual(wrapped["model"], "openai/continuum-review")
        self.assertEqual(
            wrapped["choices"][0]["message"], {"role": "assistant", "content": text}
        )
        self.assertEqual(wrapped["choices"][0]["finish_reason"], "stop")

    def test_empty_assistant_text_fails_closed(self):
        with self.assertRaises(PrAgentError):
            pr_agent.extract_assistant_text({"info": {}, "parts": []})


class UpstreamErrorTests(unittest.TestCase):
    def test_retryable_server_errors(self):
        for status in (408, 429, 500, 502, 504):
            with self.subTest(status=status):
                err = pr_agent.map_opencode_error(status, "boom")
                self.assertTrue(err.retryable, status)

    def test_client_errors_are_not_retryable(self):
        for status in (400, 403, 404):
            with self.subTest(status=status):
                err = pr_agent.map_opencode_error(status, "nope")
                self.assertFalse(err.retryable, status)

    def test_unreachable_server_is_retryable(self):
        with self.assertRaises(pr_agent.BridgeUpstreamError) as ctx:
            pr_agent.post_json("http://127.0.0.1:1/session", {}, timeout=1)
        self.assertTrue(ctx.exception.retryable)


class FakeOpenCodeHandler(BaseHTTPRequestHandler):
    sessions: dict = {}
    deleted: list = []
    bodies: list = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode() if length else "{}"
        if self.path == "/session":
            sid = f"ses-{len(type(self).sessions) + 1}"
            type(self).sessions[sid] = True
            self._send(200, {"id": sid})
            return
        if self.path.endswith("/message"):
            type(self).bodies.append(json.loads(raw))
            self._send(
                200,
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "bridge-probe-ok"}],
                },
            )
            return
        self._send(404, {"error": "nope"})

    def do_DELETE(self) -> None:  # noqa: N802
        sid = self.path.rsplit("/", 1)[-1]
        type(self).deleted.append(sid)
        type(self).sessions.pop(sid, None)
        self._send(200, True)


class BridgeIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        FakeOpenCodeHandler.sessions = {}
        FakeOpenCodeHandler.deleted = []
        FakeOpenCodeHandler.bodies = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCodeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=10)
        cls.server.server_close()

    def test_completion_uses_fresh_session_and_deletes_it(self):
        import sys

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script_dir = os.path.join(root, ".github", "scripts")
        sys.path.insert(0, script_dir)
        try:
            import pr_agent_bridge as bridge
        finally:
            sys.path.remove(script_dir)
        old = os.environ.get("OPENCODE_SERVER_URL", "")
        os.environ["OPENCODE_SERVER_URL"] = f"http://127.0.0.1:{self.port}"
        bodies_before = len(FakeOpenCodeHandler.bodies)
        deleted_before = len(FakeOpenCodeHandler.deleted)
        try:
            first = bridge.run_completion(
                {"model": "openai/continuum-review", "messages": [{"role": "user", "content": "hi"}]},
                timeout=15,
            )
            second = bridge.run_completion(
                {"model": "openai/continuum-review", "messages": [{"role": "user", "content": "hi"}]},
                timeout=15,
            )
        finally:
            if old:
                os.environ["OPENCODE_SERVER_URL"] = old
            else:
                os.environ.pop("OPENCODE_SERVER_URL", None)
        self.assertEqual(
            first["choices"][0]["message"]["content"], "bridge-probe-ok"
        )
        # Two requests used two distinct sessions, and both were deleted:
        # no cross-request session contamination is possible.
        self.assertEqual(len(FakeOpenCodeHandler.bodies) - bodies_before, 2)
        self.assertEqual(len(FakeOpenCodeHandler.deleted) - deleted_before, 2)
        self.assertEqual(FakeOpenCodeHandler.sessions, {})
        for body in FakeOpenCodeHandler.bodies[bodies_before:]:
            self.assertEqual(body.get("agent"), pr_agent.INFERENCE_AGENT)
            self.assertNotIn("tools", body)

    def test_bridge_handler_chat_completions_end_to_end(self):
        import sys

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script_dir = os.path.join(root, ".github", "scripts")
        sys.path.insert(0, script_dir)
        try:
            import pr_agent_bridge as bridge
        finally:
            sys.path.remove(script_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            old = os.environ.get("OPENCODE_SERVER_URL", "")
            os.environ["OPENCODE_SERVER_URL"] = f"http://127.0.0.1:{self.port}"
            try:
                payload = json.dumps(
                    {
                        "model": "openai/continuum-review",
                        "messages": [{"role": "user", "content": "summarize"}],
                    }
                ).encode()
                req = urlrequest.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urlrequest.urlopen(req, timeout=30) as response:
                    self.assertEqual(response.status, 200)
                    parsed = json.loads(response.read().decode())
                self.assertEqual(parsed["object"], "chat.completion")
                with urlrequest.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=30
                ) as response:
                    models = json.loads(response.read().decode())
                self.assertEqual(models["object"], "list")
                self.assertTrue(models["data"])
            finally:
                if old:
                    os.environ["OPENCODE_SERVER_URL"] = old
                else:
                    os.environ.pop("OPENCODE_SERVER_URL", None)
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()


class FindingStateTests(unittest.TestCase):
    REVIEW = (
        "src/app.py:10 [high] missing-null-check -- "
        "dereferences None when the config is absent\n"
        "src/app.py:44 [low] typo -- "
        "comment misspells receive\n"
    )

    def test_stable_finding_ids(self):
        first = pr_agent.parse_findings(self.REVIEW)
        second = pr_agent.parse_findings(self.REVIEW)
        self.assertEqual([f.id for f in first], [f.id for f in second])
        self.assertTrue(all(f.id.startswith("pra-") for f in first))

    def test_repeated_review_tracks_state_without_duplicates(self):
        previous = pr_agent.parse_findings(self.REVIEW)
        current = pr_agent.parse_findings(
            "src/app.py:10 [high] missing-null-check -- still dereferences None\n"
            "src/other.py:3 [medium] unchecked-error -- ignores return code\n"
        )
        transition = pr_agent.track_findings(previous, current)
        self.assertEqual(len(transition["persisted"]), 1)
        self.assertEqual(len(transition["new"]), 1)
        self.assertEqual(len(transition["fixed"]), 1)
        self.assertEqual(transition["fixed"][0].status, "fixed")

    def test_verify_resolved_after_correction(self):
        previous = pr_agent.parse_findings(self.REVIEW)
        corrected = pr_agent.parse_findings(
            "src/app.py:44 [low] typo -- comment misspells receive\n"
        )
        target = previous[0].id
        self.assertEqual(
            pr_agent.verify_finding(target, previous, corrected), "RESOLVED"
        )
        self.assertEqual(
            pr_agent.verify_finding(previous[1].id, previous, corrected), "UNRESOLVED"
        )

    def test_verify_unknown_id_fails_closed(self):
        previous = pr_agent.parse_findings(self.REVIEW)
        with self.assertRaises(PrAgentError):
            pr_agent.verify_finding("pra-unknown", previous, previous)

    def test_blocking_finding_requests_changes(self):
        blocking = pr_agent.parse_findings(
            "src/app.py:10 [high] missing-null-check -- dereferences None\n"
        )
        self.assertEqual(pr_agent.decide_review(blocking), "CHANGES_REQUESTED")
        clean_notes = pr_agent.parse_findings(
            "src/app.py:44 [low] typo -- comment misspells receive\n"
        )
        self.assertEqual(pr_agent.decide_review(clean_notes), "APPROVED")
        self.assertEqual(pr_agent.decide_review([]), "APPROVED")
        for severity in BLOCKING_SEVERITIES:
            finding = pr_agent.Finding(
                id="pra-x", path="f", line=1, severity=severity, summary="s"
            )
            self.assertEqual(pr_agent.decide_review([finding]), "CHANGES_REQUESTED")

    def test_current_head_gate(self):
        self.assertTrue(pr_agent.is_current_head("abc123", "abc123"))
        self.assertTrue(pr_agent.is_current_head("ABC123", "abc123"))
        self.assertFalse(pr_agent.is_current_head("abc123", "def456"))
        self.assertFalse(pr_agent.is_current_head("", "abc123"))

    def test_stale_head_never_approves(self):
        coverage = Coverage(reviewed=1, total=1)
        report = pr_agent.review_report([], "old-sha", "new-sha", coverage)
        self.assertEqual(report["outcome"], "STALE_HEAD")


class ChunkingCoverageTests(unittest.TestCase):
    def test_small_diff_is_one_chunk(self):
        chunks = pr_agent.chunk_diff("diff --git small", max_chars=60000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].total, 1)

    def test_large_pr_is_chunked(self):
        chunks = pr_agent.chunk_diff("x" * 130000, max_chars=60000)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(c.total == 3 for c in chunks))

    def test_partial_coverage_fails_closed(self):
        with self.assertRaises(PrAgentError):
            pr_agent.check_coverage(Coverage(reviewed=1, total=3))
        with self.assertRaises(PrAgentError):
            pr_agent.check_coverage(Coverage(reviewed=0, total=0))
        pr_agent.check_coverage(Coverage(reviewed=3, total=3))

    def test_clean_current_head_reaches_approval(self):
        report = pr_agent.review_report([], "sha", "sha", Coverage(1, 1))
        self.assertEqual(report["outcome"], "APPROVED")

    def test_thread_record_tracks_repeated_reviews(self):
        thread = ReviewThread()
        first = pr_agent.parse_findings(self.REVIEW_FIXTURE)
        transition = thread.record("sha-1", first, Coverage(1, 1))
        self.assertEqual(len(transition["new"]), 1)
        second = pr_agent.parse_findings(self.REVIEW_FIXTURE)
        transition = thread.record("sha-2", second, Coverage(1, 1))
        self.assertEqual(len(transition["persisted"]), 1)
        self.assertEqual(len(transition["new"]), 0)

    REVIEW_FIXTURE = (
        "src/app.py:10 [high] missing-null-check -- dereferences None\n"
    )


class DisabledPathTests(unittest.TestCase):
    def test_disabled_report_is_explicit(self):
        report = pr_agent.disabled_report()
        self.assertEqual(report["outcome"], "DISABLED")
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["reason"])


class VerifyCommandTests(unittest.TestCase):
    def test_parse_verify_command(self):
        self.assertEqual(pr_agent.parse_verify_command("/verify pra-abc123"), "pra-abc123")
        self.assertEqual(
            pr_agent.parse_verify_command("please\n/verify pra-abc123\nthanks"), "pra-abc123"
        )
        self.assertIsNone(pr_agent.parse_verify_command("/review"))
        self.assertIsNone(pr_agent.parse_verify_command("/verify"))
        self.assertIsNone(pr_agent.parse_verify_command("no command here"))

    def test_inline_comment_ids_are_stable(self):
        first = pr_agent.inline_comment_finding_id("src/a.py", 10, "nil check")
        second = pr_agent.inline_comment_finding_id("src/a.py", 10, "nil check")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("pra-"))
        self.assertNotEqual(first, pr_agent.inline_comment_finding_id("src/a.py", 11, "nil check"))

    def test_verdict_requires_explicit_resolved(self):
        self.assertEqual(pr_agent.verdict_from_text("**RESOLVED** fixed"), "RESOLVED")
        self.assertEqual(pr_agent.verdict_from_text("looks fine"), "UNRESOLVED")
        self.assertEqual(pr_agent.verdict_from_text(""), "UNRESOLVED")
        # UNRESOLVED always wins over a concurrent RESOLVED mention.
        self.assertEqual(
            pr_agent.verdict_from_text("**RESOLVED**? No: UNRESOLVED, still broken"),
            "UNRESOLVED",
        )


if __name__ == "__main__":
    unittest.main()
