"""Executable tests for the bounded 429 dispatch observation helper.

These tests drive the real ``observeDispatchedRun`` helper under Node with a
mocked REST inventory. They pin two properties that a whole-second mock cannot
observe:

* bisecting a saturated window must not drop a run whose ``created_at`` has a
  fractional second between two split boundaries;
* retry sleeps draw from one shared wall-clock budget instead of restarting a
  ten-retry (about 45 second) allowance for every candidate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / ".github" / "scripts" / "opencode_429_dispatch.js"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
TMP_PARENT = ROOT / ".opencode-tmp"

FIXED_NOW = 1_700_000_000_000
NOT_BEFORE = FIXED_NOW - 60_000
# Derived exactly like the helper: floor((notBefore - 5000) / 1000) and
# ceil(now / 1000), split at the whole-second midpoint.
LOWER = (NOT_BEFORE - 5000) // 1000
UPPER = FIXED_NOW // 1000
MIDPOINT = (LOWER + UPPER) // 2

NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const helper = require(process.argv[2]);
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const resultPath = process.argv[4];
const scenario = payload.scenario || {};

const fixedNow = Number(payload.now);
const realNow = Date.now;
Date.now = () => fixedNow;

const calls = [];
const sleeps = [];
const runs = (scenario.runs || []).map(run => ({ ...run }));
const sharedBudget = payload.sleepBudget
  ? { remainingMs: Number(payload.sleepBudget.remainingMs) }
  : undefined;

async function listWorkflowRuns(params = {}) {
  calls.push({ ...params });
  if (scenario.unstable) {
    // Claim two runs but never return the second: an inconsistent page that
    // can only be retried, never completed.
    return { data: { total_count: 2, workflow_runs: [runs[0]] } };
  }
  const [start, end] = String(params.created || '').split('..');
  const lower = new Date(start).getTime();
  const upper = new Date(end).getTime();
  let matched = runs.filter(run => {
    // Deliberately full precision: GitHub may compare `created` against the
    // exact `created_at` instead of truncating it to whole seconds.
    const at = new Date(run.created_at).getTime();
    return at >= lower && at <= upper;
  });
  matched.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  const total = matched.length;
  if (total > 1000) matched = matched.slice(0, 1000);
  const perPage = Number(params.per_page || 30);
  const page = Number(params.page || 1);
  const offset = (page - 1) * perPage;
  return {
    data: {
      total_count: total,
      workflow_runs: matched.slice(offset, offset + perPage),
    },
  };
}

async function observe() {
  return helper.observeDispatchedRun({
    target: {
      path: '.github/workflows/opencode.yml',
      workflowId: 'opencode.yml',
    },
    notBefore: Number(payload.notBefore),
    displayTitle: payload.displayTitle || '',
    recoveryRunName: payload.recoveryRunName || '',
    oldRunId: 0,
    adopting: Boolean(payload.adopting),
    owner: 'kodmial',
    repo: 'continuum',
    listWorkflowRuns,
    warn: () => {},
    sleep: async ms => { sleeps.push(ms); },
    sleepBudget: sharedBudget,
  });
}

(async () => {
  const results = [];
  for (let index = 0; index < Number(payload.observations || 1); index += 1) {
    const before = sleeps.length;
    const result = await observe();
    results.push({ result, sleeps: sleeps.slice(before) });
  }
  fs.writeFileSync(resultPath, JSON.stringify({
    results,
    calls,
    budgetRemaining: sharedBudget ? sharedBudget.remainingMs : null,
  }), 'utf8');
})().catch(error => {
  fs.writeFileSync(resultPath, JSON.stringify({
    harnessError: String(error && error.stack || error),
  }), 'utf8');
  process.exitCode = 1;
});
"""


def iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(
        milliseconds / 1000, tz=timezone.utc
    ).isoformat().replace('+00:00', 'Z')


class ObservationTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="observe-", dir=str(TMP_PARENT)))
        self.harness = self.workdir / "harness.js"
        self.harness.write_text(NODE_HARNESS, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def observe(self, payload):
        payload = {"now": FIXED_NOW, "notBefore": NOT_BEFORE, **payload}
        payload_path = self.workdir / "payload.json"
        result_path = self.workdir / "result.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        completed = subprocess.run(
            ["node", str(self.harness), str(MODULE),
             str(payload_path), str(result_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertNotIn("harnessError", result, result.get("harnessError"))
        return result

    def test_saturated_split_does_not_drop_a_fractional_second_run(self):
        # A run created at `midpoint.500` is invisible to both whole-second
        # windows `[start, midpoint]` and `[midpoint + 1, end]`. A false
        # absence here dispatches a duplicate fresh-runner recovery.
        recovery = {
            "id": 424242,
            "event": "workflow_dispatch",
            "created_at": iso(MIDPOINT * 1000 + 500),
            "status": "in_progress",
            "display_title": "OpenCode 429 recovery op gen 1",
            "path": ".github/workflows/opencode.yml",
        }
        unrelated = [
            {
                "id": 90000 + index,
                "event": "workflow_dispatch",
                "created_at": iso((LOWER + index % (UPPER - LOWER)) * 1000),
                "status": "completed",
                "conclusion": "failure",
                "display_title": "Unrelated dispatch",
                "path": ".github/workflows/opencode.yml",
            }
            for index in range(1000)
        ]
        result = self.observe({
            "scenario": {"runs": unrelated + [recovery]},
            "adopting": True,
            "displayTitle": "OpenCode 429 recovery op gen 1",
            "recoveryRunName": "OpenCode 429 recovery op gen 1",
        })
        observed = result["results"][0]["result"]
        self.assertIsNotNone(observed, result["calls"])
        self.assertEqual(observed["id"], recovery["id"], result["calls"])
        self.assertGreater(len(result["calls"]), 1, "the window must have split")

    def test_retry_wall_clock_is_shared_across_observations(self):
        scenario = {
            "unstable": True,
            "runs": [{
                "id": 70001,
                "event": "workflow_dispatch",
                "created_at": iso(FIXED_NOW - 10_000),
                "status": "completed",
                "display_title": "Unrelated dispatch",
                "path": ".github/workflows/opencode.yml",
            }],
        }
        result = self.observe({
            "scenario": scenario,
            "observations": 2,
            "sleepBudget": {"remainingMs": 10_000},
        })
        first, second = result["results"]
        self.assertTrue(first["result"]["incomplete"], first)
        self.assertTrue(second["result"]["incomplete"], second)
        self.assertEqual(first["sleeps"], [5000, 5000], first["sleeps"])
        self.assertEqual(second["sleeps"], [], second["sleeps"])
        self.assertEqual(result["budgetRemaining"], 0)

    def test_single_observation_wall_clock_is_bounded(self):
        scenario = {
            "unstable": True,
            "runs": [{
                "id": 70002,
                "event": "workflow_dispatch",
                "created_at": iso(FIXED_NOW - 10_000),
                "status": "completed",
                "display_title": "Unrelated dispatch",
                "path": ".github/workflows/opencode.yml",
            }],
        }
        result = self.observe({"scenario": scenario})
        observed = result["results"][0]
        self.assertTrue(observed["result"]["incomplete"], observed)
        self.assertLessEqual(sum(observed["sleeps"]), 30_000, observed["sleeps"])

    def test_watchdog_shares_one_budget_with_the_helper(self):
        source = WATCHDOG.read_text(encoding="utf-8")
        self.assertIn("const dispatchObservationSleepBudget", source)
        self.assertIn("sleepBudget: dispatchObservationSleepBudget,", source)
        helper = MODULE.read_text(encoding="utf-8")
        self.assertIn("OBSERVATION_SLEEP_BUDGET_MS", helper)
        self.assertIn("sleepBudget", helper)


if __name__ == "__main__":
    unittest.main()
