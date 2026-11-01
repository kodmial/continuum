"""Executable tests for the global cross-repository PR watchdog helper."""

from __future__ import annotations

import json
import os
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MODULE = os.path.join(ROOT, ".github", "scripts", "global_pr_watchdog.js")

NODE_PROBE = r"""
const watchdog = require(process.env.WATCHDOG_MODULE);
const status = Number(process.env.PROBE_STATUS);
const headers = JSON.parse(process.env.PROBE_HEADERS || '{}');
const consumer = {
  id: 11,
  owner: { login: 'owner' },
  name: 'consumer',
  default_branch: 'main',
  permissions: { push: true },
};
const client = {
  async getRateLimit() {
    return { limit: 5000, remaining: 4999, reset: 1 };
  },
  async listRepositories(page) {
    return page === 1 ? [consumer] : [];
  },
  async getConsumerMarker() {
    throw new watchdog.HttpError(status, 'consumer_marker', headers);
  },
  async probeOpenPulls() {
    return true;
  },
  async dispatchWorkflow() {},
};
watchdog
  .runScan(client, { continuumRepo: 'owner/continuum', continuumRepoId: 99 })
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((error) => {
    process.stderr.write(String(error && error.message));
    process.exitCode = 1;
  });
"""


def run_probe(status, headers=None):
    env = dict(os.environ)
    env.update(
        {
            "WATCHDOG_MODULE": MODULE,
            "PROBE_STATUS": str(status),
            "PROBE_HEADERS": json.dumps(headers or {}),
        }
    )
    return subprocess.run(
        ["node", "-e", NODE_PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


BUDGET_PROBE = r"""
const watchdog = require(process.env.WATCHDOG_MODULE);
const mode = process.env.PROBE_MODE;
const state = { listCalls: [], markerCalls: 0, rateCalls: 0 };
function repo(id) {
  return {
    id,
    owner: { login: 'owner' },
    name: `consumer-${id}`,
    default_branch: 'main',
    permissions: { push: true },
  };
}
const pageOne = mode.startsWith('recheck')
  ? Array.from({ length: watchdog.REPOS_PER_PAGE }, (_, index) => repo(1000 + index))
  : [repo(11)];
const client = {
  async getRateLimit() {
    state.rateCalls += 1;
    if (mode === 'start-missing') return { limit: 5000, reset: 1 };
    if (mode === 'start-nonnumeric') return { limit: 5000, remaining: 'n/a', reset: 1 };
    if (mode === 'start-valid-low') return { limit: 5000, remaining: 400, reset: 1 };
    if (mode === 'start-valid-high') return { limit: 5000, remaining: 4000, reset: 1 };
    if (mode === 'recheck-missing') {
      return state.rateCalls === 1
        ? { limit: 5000, remaining: 4000, reset: 1 }
        : { limit: 5000, reset: 1 };
    }
    if (mode === 'recheck-nonnumeric') {
      return state.rateCalls === 1
        ? { limit: 5000, remaining: 4000, reset: 1 }
        : { limit: 5000, remaining: 'unknown', reset: 1 };
    }
    return { limit: 5000, remaining: 4000, reset: 1 };
  },
  async listRepositories(page) {
    state.listCalls.push(page);
    return page === 1 ? pageOne : [];
  },
  async getConsumerMarker() {
    state.markerCalls += 1;
    return 'not a continuum caller';
  },
  async probeOpenPulls() {
    return true;
  },
  async dispatchWorkflow() {},
};
watchdog
  .runScan(client, { continuumRepo: 'owner/continuum', continuumRepoId: 99 })
  .then((payload) => {
    process.stdout.write(JSON.stringify({ result: payload, state }));
  })
  .catch((error) => {
    process.stderr.write(String(error && error.message));
    process.exitCode = 1;
  });
"""


def run_budget_probe(mode):
    env = dict(os.environ)
    env.update({"WATCHDOG_MODULE": MODULE, "PROBE_MODE": mode})
    return subprocess.run(
        ["node", "-e", BUDGET_PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class LowBudgetGuardTests(unittest.TestCase):
    def assert_budget_stopped_before_discovery(self, mode):
        result = run_budget_probe(mode)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["result"]["lowBudget"])
        self.assertEqual(payload["result"]["counters"]["skipped_low_rate_budget"], 1)
        self.assertEqual(payload["state"]["listCalls"], [])
        self.assertEqual(payload["state"]["markerCalls"], 0)

    def test_missing_remaining_at_discovery_start_is_low_budget(self):
        self.assert_budget_stopped_before_discovery("start-missing")

    def test_non_numeric_remaining_at_discovery_start_is_low_budget(self):
        self.assert_budget_stopped_before_discovery("start-nonnumeric")

    def test_valid_remaining_at_or_below_the_floor_still_stops(self):
        self.assert_budget_stopped_before_discovery("start-valid-low")

    def test_valid_remaining_above_the_floor_starts_discovery(self):
        result = run_budget_probe("start-valid-high")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["result"]["lowBudget"])
        self.assertEqual(payload["result"]["counters"]["skipped_low_rate_budget"], 0)
        self.assertEqual(payload["state"]["listCalls"], [1])
        self.assertEqual(payload["state"]["markerCalls"], 1)

    def assert_recheck_stopped(self, mode):
        result = run_budget_probe(mode)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["result"]["lowBudget"])
        self.assertEqual(payload["result"]["counters"]["skipped_low_rate_budget"], 1)
        # Discovery stopped immediately on the recheck; no further markers
        # were inspected and no second repository page was requested.
        self.assertEqual(payload["state"]["markerCalls"], 25)
        self.assertEqual(payload["state"]["listCalls"], [1])

    def test_missing_remaining_on_recheck_stops_discovery(self):
        self.assert_recheck_stopped("recheck-missing")

    def test_non_numeric_remaining_on_recheck_stops_discovery(self):
        self.assert_recheck_stopped("recheck-nonnumeric")


class RateLimitStopTests(unittest.TestCase):
    def test_secondary_403_with_retry_after_stops_the_pass(self):
        result = run_probe(403, {"retry-after": "30", "x-ratelimit-remaining": "4999"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["stoppedRateLimited"])

    def test_per_repository_403_is_counted_and_the_pass_continues(self):
        result = run_probe(403, {"x-ratelimit-remaining": "4999"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["stoppedRateLimited"])
        self.assertEqual(payload["counters"]["request_failures_by_status"].get("403"), 1)

    def test_invalid_credential_remains_fatal(self):
        result = run_probe(401, {})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("central credential is invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
