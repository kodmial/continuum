"""Contract for safe in-flight OpenCode diagnostics."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/continuum/opencode_live_status.py"
spec = importlib.util.spec_from_file_location("opencode_live_status", SRC)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveStatusTests(unittest.TestCase):
    def test_tool_metadata_is_safe(self):
        event = {"type":"tool_use","part":{"tool":"bash","state":{
            "status":"running","title":"sk-VERY_SECRET","input":{"command":"private"}}}}
        label = live.event_metadata(json.dumps(event))
        self.assertEqual("tool: bash (running)", label)
        self.assertNotIn("SECRET", label)
        self.assertNotIn("private", label)
        self.assertEqual("tool: other (unknown)", live.event_metadata(json.dumps({"type": "tool_use", "part": {"tool": "ghp_SECRET", "state": {"status": "bad"}}})))

    def test_web_search_error_is_classified_without_echoing_secret(self):
        event = {"type":"error","error":{"message":
            "Cancelled a request for input this run cannot answer: Web Search token=private"}}
        self.assertEqual("error: web-search-input-unavailable", live.event_metadata(json.dumps(event)))

    def test_unknown_and_text_are_withheld(self):
        self.assertIsNone(live.event_metadata("private text"))
        self.assertIsNone(live.event_metadata('{"type":"other","content":"secret"}'))
        self.assertEqual("text (content withheld)", live.event_metadata(
            '{"type":"text","part":{"text":"password"}}'))

    def test_partial_event_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "events"
            line = '{"type":"tool_use","part":{"tool":"read","state":{"status":"completed"}}}'
            p.write_text(line[:10])
            s = live.Snapshot()
            self.assertEqual(0, live.consume(p, 0, s))
            with p.open("a") as stream:
                stream.write(line[10:] + "\n")
            self.assertEqual(p.stat().st_size, live.consume(p, 0, s))
            self.assertEqual(1, s.count)

    def test_age_visible_while_stalled(self):
        s = live.Snapshot()
        start = datetime(2026, 11, 12, tzinfo=timezone.utc)
        s.ingest('{"type":"step_start"}', observed=start)
        status = s.render("o/r", 4, 7, observed=start + timedelta(minutes=12))
        self.assertIn("720s since last event", status)
        self.assertIn("actions/runs/7", status)

    def test_credentials_are_required(self):
        with patch.dict("os.environ", {"GITHUB_REPOSITORY":"o/r",
             "CONTINUUM_LIVE_LOG_ISSUE":"4","GITHUB_RUN_ID":"7",
             "CONTINUUM_LIVE_LOG_TOKEN":"","RUNNER_TEMP":"/tmp"}, clear=True):
            self.assertIsNone(live.settings())

    def test_comment_is_updated_in_place(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = ("o/r", 42, 123, "hidden-token", "https://api.github.com",
                   Path(root) / "comment.id")
            history = []
            class Response(BytesIO):
                def __enter__(self): return self
                def __exit__(self, *args): self.close()
            def fake(req, timeout):
                history.append((req.get_method(), req.full_url, json.loads(req.data)["body"]))
                return Response(b'{"id":987}' if req.get_method() == "POST" else b"{}")
            with patch.object(live.request, "urlopen", side_effect=fake):
                live.publish(cfg, live.Snapshot(), "running")
                live.publish(cfg, live.Snapshot(), "finished")
            self.assertEqual(["POST", "PATCH"], [h[0] for h in history])
            self.assertIn("/issues/42/comments", history[0][1])
            self.assertIn("/issues/comments/987", history[1][1])
            self.assertNotIn("hidden-token", str(history))

    def test_workflow_and_wrapper_wiring(self):
        flow = (ROOT / ".github/workflows/continuum-opencode.yml").read_text()
        wrapper = (ROOT / ".github/scripts/opencode_429_wrapper.sh").read_text()
        self.assertIn('cp -R "$engine/src/continuum" "$install_root/src/"', flow)
        self.assertIn('live_script="$script_dir/src/continuum/opencode_live_status.py"', wrapper)
        self.assertIn('CONTINUUM_LIVE_LOG_TOKEN="$live_token"', wrapper)
        self.assertIn('python3 "$live_script" watch --log "$log"', wrapper)
        self.assertIn('python3 "$live_script" once --log "$log"', wrapper)
        self.assertIn("env -u CONTINUUM_LIVE_LOG_TOKEN", wrapper)


if __name__ == "__main__":
    unittest.main()
