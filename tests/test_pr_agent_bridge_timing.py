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
            with patch.dict(bridge.os.environ, {"OPENCODE_SERVER_URL": "http://127.0.0.1:4096"}):
                with contextlib.redirect_stderr(stderr):
                    status, _ = bridge.opencode_call("POST", "/session", {"text": "private-data"}, 170)
                    status2, _ = bridge.opencode_call(
                        "POST", "/session/session-secret-should-not-be-logged/message",
                        {"text": "private-data"}, 170,
                    )
        self.assertEqual((status, status2), (200, 200))
        log = stderr.getvalue()
        self.assertRegex(log, r"bridge-phase stage=create-session elapsed_ms=\d+ outcome=http_200")
        self.assertRegex(log, r"bridge-phase stage=send-message elapsed_ms=\d+ outcome=http_200")
        self.assertNotIn("private-data", log)
        self.assertNotIn("session-secret", log)

    def test_network_timeout_at_open_code_is_logged_as_distinct_error(self):
        stderr = io.StringIO()
        with patch.object(bridge.urllib.request, "urlopen", side_effect=TimeoutError("private-data")):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(bridge.BridgeUpstreamError):
                    bridge.opencode_call("POST", "/session", None, 170)
        self.assertRegex(
            stderr.getvalue(),
            r"bridge-phase stage=create-session elapsed_ms=\d+ outcome=TimeoutError",
        )
        self.assertNotIn("private-data", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
