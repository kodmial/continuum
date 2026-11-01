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
