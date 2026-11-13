"""Regression tests: content-free per-stage OpenCode bridge timing."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github" / "scripts" / "pr_agent_bridge.py"
SPEC = importlib.util.spec_from_file_location("continuum_pr_agent_bridge_timing", PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeReply:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"id": "session-secret-should-not-be-logged"}'


class BridgeTimingTests(unittest.TestCase):
    def test_per_stage_timing_records_healthy_opencode_without_payloads(self):
        stderr = io.StringIO()
        with patch.object(bridge.urllib.request, "urlopen", return_value=FakeReply()):
            with patch.dict(bridge.os.environ, {"OPENCODE_SERVER_URL": "http://127.0.0.1:4096",
                                                     "OPENCODE_PASSWORD": "private-credential"}):
                with contextlib.redirect_stderr(stderr):
                    status, _ = bridge.opencode_call("POST", "/api/session", {"text": "private-data"}, 170)
                    status2, _ = bridge.opencode_call(
                        "POST", "/api/session/session-secret-should-not-be-logged/prompt",
                        {"text": "private-data"}, 170,
                    )
        self.assertEqual((status, status2), (200, 200))
        log = stderr.getvalue()
        self.assertRegex(log, r"bridge-phase stage=create-session elapsed_ms=\d+ outcome=http_200")
        self.assertRegex(log, r"bridge-phase stage=submit-async elapsed_ms=\d+ outcome=http_200")
        self.assertNotIn("private-data", log)
        self.assertNotIn("session-secret", log)
        self.assertNotIn("private-credential", log)

    def test_bridge_socket_read_timeout_is_http_504(self):
        class RecordingHandler(bridge.Handler):
            def _send(self, status, payload):
                self.observed_status = status
                self.observed_payload = payload

        handler = object.__new__(RecordingHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(handler, "_authorized", return_value=True):
            with patch.object(
                bridge,
                "run_completion",
                side_effect=bridge.BridgeUpstreamError(
                    "cannot reach opencode server: timed out", retryable=True
                ),
            ):
                handler.do_POST()
        self.assertEqual(handler.observed_status, 504)
        self.assertEqual(handler.observed_payload["error"]["type"], "upstream_error")

    def test_completion_watchdog_is_http_504_for_route_cooldown(self):
        class RecordingHandler(bridge.Handler):
            def _send(self, status, payload):
                self.observed_status = status
                self.observed_payload = payload

        handler = object.__new__(RecordingHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(handler, "_authorized", return_value=True):
            with patch.object(
                bridge,
                "run_completion",
                side_effect=bridge.BridgeUpstreamError(
                    "opencode session exceeded its configured completion watchdog",
                    retryable=True,
                ),
            ):
                handler.do_POST()
        self.assertEqual(handler.observed_status, 504)
        self.assertEqual(handler.observed_payload["error"]["type"], "upstream_error")

    def test_invalid_completion_watchdog_is_not_misclassified_as_timeout(self):
        class RecordingHandler(bridge.Handler):
            def _send(self, status, payload):
                self.observed_status = status
                self.observed_payload = payload

        handler = object.__new__(RecordingHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(handler, "_authorized", return_value=True):
            with patch.object(
                bridge,
                "run_completion",
                side_effect=bridge.BridgeUpstreamError(
                    "invalid OpenCode completion watchdog",
                    retryable=False,
                ),
            ):
                handler.do_POST()
        self.assertEqual(handler.observed_status, 502)

    def test_authenticated_requests_never_log_password_or_prompt(self):
        class AuthReply(FakeReply):
            status = 204

            def read(self):
                return b""

        with patch.dict(bridge.os.environ, {"OPENCODE_PASSWORD": "private-password"}):
            with patch.object(bridge.urllib.request, "urlopen", return_value=AuthReply()) as fetch:
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    status, _ = bridge.opencode_call(
                        "POST", "/api/session/ses-private/agent", {"agent": "plan"}, 15
                    )
        self.assertEqual(status, 204)
        import base64
        request = fetch.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"),
                         "Basic " + base64.b64encode(b"opencode:private-password").decode())
        self.assertNotIn("private-password", stream.getvalue())
        self.assertNotIn("ses-private", stream.getvalue())

    def test_missing_authentication_fails_before_any_network_request(self):
        with patch.dict(bridge.os.environ, {"OPENCODE_PASSWORD": ""}):
            with patch.object(bridge.urllib.request, "urlopen") as fetch:
                with self.assertRaises(bridge.BridgeUpstreamError) as ctx:
                    bridge.opencode_call("POST", "/api/session", {}, 15)
        self.assertFalse(ctx.exception.retryable)
        fetch.assert_not_called()

    def test_wait_barrier_requires_a_real_completed_v2_assistant(self):
        session = {
            "id": "secret-session",
            "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
            "outcome": "succeeded",
        }
        complete = {
            "data": [{
                "id": "msg-1", "type": "assistant",
                "time": {"created": 1, "completed": 2},
                "content": [{"type": "text", "text": "grounded-review"}],
                "finish": "stop",
            }],
            "cursor": {"next": None, "previous": None},
        }
        with patch.object(
            bridge, "opencode_call",
            side_effect=[
                (204, ""),
                (200, bridge.json.dumps({"data": session})),
                (200, bridge.json.dumps(complete)),
            ],
        ) as transport:
            result = bridge._await_async_assistant("secret-session", 600)
        self.assertEqual(bridge.extract_assistant_text(result), "grounded-review")
        self.assertEqual(transport.call_args_list[0].args[:2],
                         ("POST", "/api/experimental/session/secret-session/wait"))
        self.assertEqual(transport.call_args_list[1].args[:2],
                         ("GET", "/api/session/secret-session"))
        self.assertEqual(transport.call_args_list[2].args[:2],
                         ("GET", "/api/session/secret-session/message?order=desc&type=assistant&limit=20"))

    def test_async_enqueue_is_never_mistaken_for_completed_reply(self):
        session = {"data": {
            "id": "ses-1",
            "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
            "outcome": "succeeded",
        }}
        for item in (
            {"type": "assistant", "time": {"created": 1}, "content": [
                {"type": "text", "text": "partial"}]},
            {"type": "assistant", "time": {"created": 1, "completed": 2},
             "error": {"message": "failed"}, "content": [
                 {"type": "text", "text": "fake successful reply"}]},
            {"type": "user", "time": {"created": 1}, "text": "admission"},
        ):
            with self.subTest(item=item):
                response = {"data": [item], "cursor": {}}
                with patch.object(bridge, "opencode_call", side_effect=[
                    (204, ""),
                    (200, bridge.json.dumps(session)),
                    (200, bridge.json.dumps(response)),
                ]):
                    with self.assertRaises(bridge.BridgeUpstreamError):
                        bridge._await_async_assistant("ses-1", 60)

    def test_tool_call_step_is_never_returned_as_a_final_review(self):
        session = {"data": {
            "id": "ses-1",
            "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
            "outcome": "succeeded",
        }}
        intermediate = {
            "data": [
                {"type": "assistant", "time": {"created": 3, "completed": 4},
                 "content": [{"type": "text", "text": "intermediate step"}],
                 "finish": "tool-calls"},
                {"type": "assistant", "time": {"created": 1, "completed": 2},
                 "content": [{"type": "text", "text": "earlier final answer"}],
                 "finish": "stop"},
            ],
            "cursor": {},
        }
        with patch.object(bridge, "opencode_call", side_effect=[
            (204, ""), (200, bridge.json.dumps(session)),
            (200, bridge.json.dumps(intermediate)),
        ]):
            with self.assertRaises(bridge.BridgeUpstreamError) as ctx:
                bridge._await_async_assistant("ses-1", 60)
        self.assertIn("not final", str(ctx.exception))

    def test_terminal_session_outcome_must_have_succeeded(self):
        for outcome in ("failed", "interrupted", None):
            with self.subTest(outcome=outcome):
                session = {"data": {
                    "id": "ses-1",
                    "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
                    "outcome": outcome,
                }}
                with patch.object(bridge, "opencode_call", side_effect=[
                    (204, ""), (200, bridge.json.dumps(session)),
                ]) as transport:
                    with self.assertRaises(bridge.BridgeUpstreamError):
                        bridge._await_async_assistant("ses-1", 60)
                # The failed turn never reaches the message endpoint and
                # never falls back to an earlier reply.
                self.assertEqual(transport.call_count, 2)

    def test_wait_fail_closed_on_503_without_polling_legacy_routes(self):
        with patch.object(bridge, "opencode_call", return_value=(503, "")) as call:
            with self.assertRaises(bridge.BridgeUpstreamError) as ctx:
                bridge._await_async_assistant("ses-1", 600)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(call.call_count, 1)

    def test_prompt_admission_failure_still_deletes_v2_session(self):
        with patch.dict(bridge.os.environ, {"OPENCODE_MODEL": "opencode/test-route"}):
            with patch.object(bridge, "opencode_call", side_effect=[
                (200, '{"data":{"id":"private-session"}}'),
                (200, bridge.json.dumps({"data": {
                    "id": "private-session",
                    "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
                }})),
                (204, ""),
                (204, ""),
                (405, ""),
                (204, ""),
            ]) as transport:
                with self.assertRaises(bridge.BridgeUpstreamError):
                    bridge.run_completion({
                        "messages": [{"role": "user", "content": "review"}]
                    }, timeout=600)
        self.assertEqual(
            [call.args[1] for call in transport.call_args_list],
            ["/api/session", "/api/session/private-session",
             "/api/session/private-session/agent",
             "/api/session/private-session/model",
             "/api/session/private-session/prompt", "/api/session/private-session"],
        )

    def test_review_session_requests_and_verifies_reviewer_permissions(self):
        session = {"data": {
            "id": "private-session",
            "permissions": list(bridge.REVIEWER_SESSION_PERMISSIONS),
        }}
        with patch.object(bridge, "opencode_call", side_effect=[
            (200, '{"data":{"id":"private-session"}}'),
            (200, bridge.json.dumps(session)),
            (204, ""),
            (204, ""),
        ]) as transport:
            session_id = bridge._create_review_session(
                {"providerID": "opencode", "id": "test-route"}
            )
        self.assertEqual(session_id, "private-session")
        create_body = transport.call_args_list[0].args[2]
        self.assertIn(
            {"action": "bash", "resource": "*", "effect": "deny"},
            create_body["permissions"],
        )
        self.assertIn(
            {"action": "task", "resource": "*", "effect": "deny"},
            create_body["permissions"],
        )
        self.assertEqual(transport.call_args_list[1].args[1],
                         "/api/session/private-session")

    def test_ignored_reviewer_permissions_fail_closed_and_delete_session(self):
        for permissions in (None, [], [
            {"action": "bash", "resource": "*", "effect": "allow"},
        ]):
            with self.subTest(permissions=permissions):
                session = {"data": {"id": "private-session"}}
                if permissions is not None:
                    session["data"]["permissions"] = permissions
                with patch.object(bridge, "opencode_call", side_effect=[
                    (200, '{"data":{"id":"private-session"}}'),
                    (200, bridge.json.dumps(session)),
                    (204, ""),
                ]) as transport:
                    with self.assertRaises(bridge.BridgeUpstreamError) as ctx:
                        bridge._create_review_session(
                            {"providerID": "opencode", "id": "test-route"}
                        )
                self.assertFalse(ctx.exception.retryable)
                self.assertEqual(
                    [call.args[1] for call in transport.call_args_list][-1],
                    "/api/session/private-session",
                )
                self.assertEqual(transport.call_args_list[-1].args[0], "DELETE")

    def test_preflight_retryable_exit_status_is_distinct_from_protocol_failure(self):
        for retryable, expected in (
            (True, bridge.PREFLIGHT_RETRYABLE_EXIT),
            (False, 1),
        ):
            with self.subTest(retryable=retryable):
                failure = bridge.BridgeUpstreamError(
                    "OpenCode v2 create session rejected (status=503)",
                    retryable=retryable,
                )
                with patch.object(bridge, "preflight_review_session", side_effect=failure):
                    with patch.object(
                        bridge.sys, "argv", ["pr_agent_bridge.py", "--preflight"]
                    ):
                        with contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(bridge.main(), expected)

    def test_network_timeout_at_open_code_is_logged_as_distinct_error(self):
        stderr = io.StringIO()
        with patch.dict(bridge.os.environ, {"OPENCODE_PASSWORD": "private-password"}):
            with patch.object(bridge.urllib.request, "urlopen", side_effect=TimeoutError("private-data")):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(bridge.BridgeUpstreamError):
                        bridge.opencode_call("POST", "/api/session", None, 170)
        self.assertRegex(
            stderr.getvalue(),
            r"bridge-phase stage=create-session elapsed_ms=\d+ outcome=TimeoutError",
        )
        self.assertNotIn("private-data", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
