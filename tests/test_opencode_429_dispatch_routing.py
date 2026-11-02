"""Contract tests for the 429 dispatch-target resolver (#350).

A reusable `workflow_call` workflow has no `workflow_dispatch` trigger, so a
watchdog dispatch aimed at it fails (HTTP 422). These tests exercise the actual
routing module used by the watchdog rather than a duplicate implementation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / ".github" / "scripts" / "opencode_429_dispatch.js"
TMP_PARENT = ROOT / ".opencode-tmp"

HEADER = r"""
const helper = require(process.argv[2]);
const payload = JSON.parse(process.argv[3]);
const result = helper.resolveDispatchWorkflow({
  configured: payload.configured,
  watchedRunPath: payload.watchedPath,
  workflows: payload.files || {},
});
process.stdout.write(JSON.stringify(result));
"""

DISPATCHABLE = "name: OpenCode agent\non:\n  workflow_dispatch:\n  workflow_call:\n"
CALL_ONLY = "name: OpenCode agent\non:\n  workflow_call:\n"


class DispatchRoutingTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="dispatch-", dir=str(TMP_PARENT)))
        self.harness = self.workdir / "harness.js"
        self.harness.write_text(HEADER, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def resolve(self, configured="", watched_path="", files=None):
        completed = subprocess.run(
            [
                "node",
                str(self.harness),
                str(MODULE),
                json.dumps(
                    {
                        "configured": configured,
                        "watchedPath": watched_path,
                        "files": files or {},
                    }
                ),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_self_dogfood_caller_is_dispatchable(self):
        result = self.resolve(
            configured="opencode.yml",
            watched_path=".github/workflows/opencode.yml",
            files={
                ".github/workflows/opencode.yml": DISPATCHABLE,
                ".github/workflows/continuum-opencode.yml": CALL_ONLY,
            },
        )
        self.assertEqual(result["path"], ".github/workflows/opencode.yml")
        self.assertEqual(result["workflowId"], "opencode.yml")
        self.assertEqual(result["source"], "configured")
        self.assertFalse(result["watchedRunTrigger"])

    def test_workflow_call_engine_is_never_dispatched(self):
        result = self.resolve(
            configured="continuum-opencode.yml",
            watched_path=".github/workflows/continuum-opencode.yml",
            files={
                ".github/workflows/continuum-opencode.yml": CALL_ONLY,
                ".github/workflows/opencode.yml": DISPATCHABLE,
            },
        )
        self.assertEqual(result["path"], ".github/workflows/opencode.yml")

    def test_configured_dispatch_caller_wins_over_watched_path(self):
        result = self.resolve(
            configured=".github/workflows/opencode.yml",
            watched_path=".github/workflows/continuum-opencode.yml",
            files={
                ".github/workflows/opencode.yml": DISPATCHABLE,
                ".github/workflows/continuum-opencode.yml": CALL_ONLY,
            },
        )
        self.assertEqual(result["path"], ".github/workflows/opencode.yml")

    def test_reports_no_dispatchable_target_instead_of_guessing(self):
        result = self.resolve(
            configured="continuum-opencode.yml",
            watched_path=".github/workflows/continuum-opencode.yml",
            files={
                ".github/workflows/continuum-opencode.yml": CALL_ONLY,
                ".github/scripts/continuum-opencode.yml": DISPATCHABLE,
            },
        )
        self.assertIsNone(result)
        

if __name__ == "__main__":
    unittest.main()
