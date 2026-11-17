"""Regression: long-lived OpenCode work must survive the recent-Actions window.

Runs the exact checked-in JavaScript discovery block with a fake GitHub API.
No detached approximation of the scheduler decision is used.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "issue_scheduler.js"
BEGIN = "  const workflowRuns = [];"
END = "  for (const issueNumber of activeRunIssues) {"


def probe(scenario):
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index(BEGIN)
    stop = script.index(END, start)
    source = script[start:stop]
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is required")
    harness = r"""
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const fragment = JSON.parse(process.argv[1]);
const scenario = JSON.parse(process.argv[2]);
const calls = [];
const worker = (issue, status = 'in_progress', event = 'issue_comment') => ({
  id: 1000 + issue, path: '.github/workflows/opencode.yml',
  name: 'OpenCode agent', status, event,
  display_title: 'OpenCode issue #' + issue
});
const fake = {
  rest: { actions: {
    listWorkflowRunsForRepo: async ({status, per_page, page}) => {
      calls.push({status, page});
      if (scenario.error) throw new Error('HTTP 403');
      if (scenario.truncated && status === 'in_progress') {
        return {data: {workflow_runs: Array.from({length: 100}, (_, n) => worker(n + 1))}};
      }
      const runs = [];
      if (status === 'in_progress') {
        if (scenario.oldActive) {
          const active = worker(473);
          if (scenario.missingPath) delete active.path;
          runs.push(active);
        }
        if (scenario.recovery) {
          runs.push({...worker(472, 'in_progress', 'workflow_dispatch'),
            display_title: 'OpenCode 429 recovery opencode-429-issue-issue472 gen 3'});
        }
        if (scenario.wrongWorkflow) {
          runs.push({...worker(999), path: '.github/workflows/ci.yml'});
        }
      }
      return {data: {workflow_runs: runs.slice((page - 1) * per_page, page * per_page)}};
    }
  }}
};
(async () => {
  try {
    const f = new AsyncFunction(
      'readGithub','owner','repo','opencodeWorkflowPath',
      'opencodeWorkflowName','issueNumbersByTitle',
      fragment + '\nreturn Array.from(activeRunIssues).sort((a,b)=>a-b);'
    );
    const issues = await f(fake, 'kodmial', 'continuum',
      '.github/workflows/opencode.yml', 'OpenCode agent', new Map());
    console.log(JSON.stringify({issues, calls, failed: false}));
  } catch (e) {
    console.log(JSON.stringify({failed: true, error: String(e), calls}));
  }
})();
"""
    result = subprocess.run(
        [node, "-e", harness, json.dumps(source), json.dumps(scenario)],
        text=True, capture_output=True, check=True, timeout=15,
    )
    return json.loads(result.stdout)


class ActiveRunDiscoveryTest(unittest.TestCase):
    def test_old_worker_outside_recent_300_is_still_active(self):
        result = probe({"oldActive": True})
        self.assertFalse(result["failed"], result)
        self.assertEqual([473], result["issues"])
        self.assertEqual(
            {"queued", "in_progress", "waiting", "pending", "requested"},
            {call["status"] for call in result["calls"]},
        )
        self.assertTrue(all(call["status"] for call in result["calls"]))
        scheduler = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("activeRunIssues.has(issueNumber)", scheduler)
        self.assertIn("await addLabel(issueNumber, inProgressLabel);", scheduler)

    def test_old_429_recovery_worker_is_attributed_to_issue(self):
        result = probe({"recovery": True, "wrongWorkflow": True})
        self.assertFalse(result["failed"], result)
        self.assertEqual([472], result["issues"])

    def test_legacy_worker_missing_path_still_matches_by_configured_name(self):
        # Explicitly wrong paths cannot impersonate the configured workflow;
        # older Actions inventories without a path remain backwards compatible.
        result = probe({"oldActive": True, "missingPath": True})
        self.assertFalse(result["failed"], result)
        self.assertEqual([473], result["issues"])

    def test_unreadable_active_inventory_fails_closed(self):
        result = probe({"error": True})
        self.assertTrue(result["failed"], result)
        self.assertIn("403", result["error"])

    def test_overfull_active_inventory_fails_closed(self):
        result = probe({"truncated": True})
        self.assertTrue(result["failed"], result)
        self.assertIn("truncated", result["error"])


    def test_expired_429_backoff_keeps_a_verified_checkpoint_for_watchdog(self):
        # Run the real checked-in scheduler evaluator: due is a retry floor,
        # not permission to pause a still-authenticated checkpoint.
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("  async function evaluateScheduler429Recovery({")
        end = source.index("  // Owner-armed resume is evaluated", start)
        fragment = source[start:end]
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required")
        harness = r"""
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const code = JSON.parse(process.argv[1]);
const scenario = JSON.parse(process.argv[2]);
const now = Date.now();
const sha = 'a'.repeat(40);
const recovery = {
  operation: 'opencode-429-issue-issue472', generation: 4,
  checkpointRef: 'opencode/429-checkpoint-opencode-429-issue-issue472-gen-4',
  checkpointSha: sha, commentAt: now - 5000, runId: '',
  state: scenario.state || 'backoff',
  nextDue: Math.floor(now / 1000) - 180,
};
const parser = () => ({ operation: recovery.operation, generation: 4 });
const getRun = async () => ({kind: 'missing'});
const getCheckpoint = async () => scenario.invalid
  ? {kind: 'invalid'} : {kind: 'valid', refSha: sha};
(async () => {
  const fn = new AsyncFunction(
    'recovery','issueNumber','parseSchedulerCheckpointIdentity',
    'fetchSchedulerRecoveryRun','readSchedulerCheckpointEvidence',
    code + '\nreturn await evaluateScheduler429Recovery({recovery, issueNumber});'
  );
  const state = await fn(recovery, 472, parser, getRun, getCheckpoint);
  console.log(JSON.stringify({
    keep: state.keepReservation, pending: state.recoveryPending,
    countWip: state.countWip, reason: state.reason
  }));
})();
"""
        def run(scenario):
            completed = subprocess.run(
                [node, "-e", harness, json.dumps(fragment), json.dumps(scenario)],
                check=True, text=True, capture_output=True, timeout=15,
            )
            return json.loads(completed.stdout)
        live = run({})
        self.assertEqual(
            {"keep": True, "pending": True, "countWip": False,
             "reason": "backoff-awaiting-watchdog"}, live,
        )
        invalid = run({"invalid": True})
        self.assertFalse(invalid["keep"], invalid)
        self.assertEqual("checkpoint-invalid", invalid["reason"])
        completed = run({"state": "observed"})
        self.assertFalse(completed["keep"], completed)
        self.assertEqual("checkpoint-unchanged", completed["reason"])

    def test_empty_inventory_does_not_invent_worker(self):
        result = probe({})
        self.assertFalse(result["failed"], result)
        self.assertEqual([], result["issues"])


if __name__ == "__main__":
    unittest.main()
