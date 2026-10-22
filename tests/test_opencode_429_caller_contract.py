"""End-to-end contract for the 429 fresh-VM dispatch surface (#350).

The watchdog must dispatch the repository-local *caller* that actually
declares ``workflow_dispatch``. Continuum self-dogfood names it
``opencode.yml``; an installed consumer names it ``continuum-opencode.yml``.
The reusable engine (``continuum-opencode.yml`` in Continuum's own checkout)
is ``workflow_call``-only and must never be targeted.

These tests use the real checked-in files and the real routing helper, not a
mirror implementation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
HELPER = ROOT / ".github" / "scripts" / "opencode_429_dispatch.js"
SELF_CALLER = ROOT / ".github" / "workflows" / "opencode.yml"
ENGINE = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
CONSUMER_CALLER = ROOT / ".github" / "caller-stubs" / "continuum-opencode.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
WATCHDOG_STUB = (
    ROOT / ".github" / "caller-stubs" / "continuum-opencode-watchdog.yml"
)
AUTOMATION = ROOT / ".github" / "workflows" / "automation.yml"

RECOVERY_FIELDS = (
    "recovery_operation_id",
    "recovery_checkpoint_ref",
    "recovery_checkpoint_sha",
    "recovery_stage",
    "recovery_generation",
)

NODE_HARNESS = r"""
const helper = require(process.argv[2]);
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(helper.resolveDispatchWorkflow(payload)));
"""


def workflow_events(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("on") or data.get(True) or {}


def installed_workflow_path(path: Path) -> str:
    """Map a checked-in file to the path it occupies after installation."""
    relative = Path(path).resolve().relative_to(ROOT)
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == ".github" and parts[1] == "caller-stubs":
        return (Path(".github") / "workflows" / parts[-1]).as_posix()
    return relative.as_posix()


class CallerDeclarationTest(unittest.TestCase):
    def test_self_dogfood_caller_accepts_and_forwards_recovery_identity(self):
        events = workflow_events(SELF_CALLER)
        dispatch = events.get("workflow_dispatch") or {}
        inputs = dispatch.get("inputs") or {}
        self.assertIn("recovery_identity", inputs)
        self.assertFalse(inputs["recovery_identity"].get("required"))

        body = SELF_CALLER.read_text(encoding="utf-8")
        for field in RECOVERY_FIELDS:
            self.assertIn(field, body)
            self.assertIn("fromJSON(inputs.recovery_identity).{}".format(field), body)

    def test_consumer_stub_accepts_and_forwards_identical_identity(self):
        events = workflow_events(CONSUMER_CALLER)
        dispatch = events.get("workflow_dispatch") or {}
        inputs = dispatch.get("inputs") or {}
        self.assertIn("recovery_identity", inputs)
        self.assertFalse(inputs["recovery_identity"].get("required"))

        body = CONSUMER_CALLER.read_text(encoding="utf-8")
        for field in RECOVERY_FIELDS:
            self.assertIn(field, body)
            self.assertIn("fromJSON(inputs.recovery_identity).{}".format(field), body)

    def test_dispatch_inputs_stay_within_github_limit(self):
        for path in (SELF_CALLER, CONSUMER_CALLER):
            events = workflow_events(path)
            inputs = (events.get("workflow_dispatch") or {}).get("inputs") or {}
            self.assertLessEqual(
                len(inputs), 25,
                "{} declares {} dispatch inputs (GitHub limit is 25)".format(
                    path, len(inputs)
                ),
            )

    def test_reusable_engine_is_call_only(self):
        events = workflow_events(ENGINE)
        self.assertIn("workflow_call", events)
        self.assertNotIn("workflow_dispatch", events)
        self.assertNotIn("schedule", events)

    def test_both_callers_expose_workflow_dispatch(self):
        for path in (SELF_CALLER, CONSUMER_CALLER):
            self.assertIn("workflow_dispatch", workflow_events(path), path)


class DispatchResolutionTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="caller-", dir=str(TMP_PARENT)))
        self.harness = self.workdir / "harness.js"
        self.harness.write_text(NODE_HARNESS, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def resolve(self, configured, watched_path, paths):
        files = {
            installed_workflow_path(path): Path(path).read_text(encoding="utf-8")
            for path in paths
        }
        completed = subprocess.run(
            ["node", str(self.harness), str(HELPER)],
            input=json.dumps(
                {
                    "configured": configured,
                    "watchedRunPath": watched_path,
                    "workflows": files,
                }
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_self_dogfood_resolves_the_dispatchable_caller(self):
        result = self.resolve(
            configured="opencode.yml",
            watched_path=".github/workflows/opencode.yml",
            paths=[SELF_CALLER, ENGINE],
        )
        self.assertEqual(result["path"], ".github/workflows/opencode.yml")
        self.assertEqual(result["workflowId"], "opencode.yml")

    def test_consumer_resolves_the_installed_stub(self):
        result = self.resolve(
            configured="",
            watched_path=".github/workflows/continuum-opencode.yml",
            paths=[CONSUMER_CALLER],
        )
        self.assertEqual(
            result["path"], ".github/workflows/continuum-opencode.yml"
        )
        self.assertEqual(result["workflowId"], "continuum-opencode.yml")

    def test_reusable_engine_is_never_selected(self):
        result = self.resolve(
            configured="continuum-opencode.yml",
            watched_path=".github/workflows/continuum-opencode.yml",
            paths=[ENGINE, SELF_CALLER],
        )
        self.assertEqual(result["path"], ".github/workflows/opencode.yml")

    def test_wrong_or_unknown_paths_fail_closed(self):
        result = self.resolve(
            configured="../escape.yml",
            watched_path=".github/scripts/opencode.yml",
            paths=[ENGINE],
        )
        self.assertIsNone(result)


class WatchdogContractTest(unittest.TestCase):
    def setUp(self):
        self.body = WATCHDOG.read_text(encoding="utf-8")

    def test_dispatch_uses_resolved_target_not_engine_literal(self):
        self.assertIn("workflow_id: target.workflowId", self.body)
        recovery = self.body.split("async function recoverFrom429OnFreshVm")[-1]
        self.assertNotIn("workflow_id: 'continuum-opencode.yml'", recovery)
        self.assertNotIn("workflow_id: \"continuum-opencode.yml\"", recovery)

    def test_catch_up_timer_reuses_recovery_owner(self):
        events = workflow_events(WATCHDOG_STUB)
        self.assertEqual(
            [entry["cron"] for entry in events.get("schedule", [])],
            ["*/15 * * * *"],
        )
        automation = AUTOMATION.read_text(encoding="utf-8")
        self.assertIn("github.event_name == 'schedule'", automation)
        self.assertIn("opencode_workflow: 'opencode.yml'", automation)
        self.assertIn("listWorkflowRunsForRepo", self.body)
        self.assertIn("recoverFrom429OnFreshVm({", self.body)

    def test_recovery_states_cover_every_required_outcome(self):
        for state in (
            "metadata-pending",
            "metadata-failed",
            "dispatch-failed",
            "accepted",
            "observed",
            "backoff",
        ):
            self.assertIn("'" + state + "'", self.body, state)
        self.assertIn("next_due=", self.body)
        self.assertIn("stalePointer", self.body)
        self.assertIn("kind: 'missing'", self.body)
        self.assertIn("'auth'", self.body)
        self.assertIn("'transient'", self.body)

    def test_engine_never_dispatches_a_workflow_call_only_target(self):
        engine = ENGINE.read_text(encoding="utf-8")
        self.assertNotIn("workflow_id: 'continuum-opencode.yml'", engine)
        self.assertNotIn('workflow_id: "continuum-opencode.yml"', engine)
        self.assertIn("resolveDispatchCaller", engine)
        self.assertIn("workflow_dispatch", engine)
        self.assertIn("OPENCODE_WORKFLOW", engine)
        self.assertIn("github.workflow_ref", engine)
        self.assertNotIn(".github/workflows/opencode.yml", engine)


if __name__ == "__main__":
    unittest.main()
