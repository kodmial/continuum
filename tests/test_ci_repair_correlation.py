"""Executable regression tests for the exact-HEAD CI-repair correlation step.

The step runs inside ``actions/github-script`` after the serialized admission
owner has already persisted a ``claimed`` receipt. These tests extract the
production script from ``continuum-opencode.yml`` and execute it under Node
with a fake Octokit, so the shipped behavior is observed rather than copied:

* a comment-pagination failure warns and leaves the admitted repair running;
* attempt selection requires the dispatched attempt identity, folds each
  attempt's latest reconciled state, and never falls back to the live HEAD;
* a requested attempt is correlated only when its latest state is active, and
  a queued run for an older HEAD cannot be attributed to a newer claim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-opencode.yml"
STEP_NAME = "Record exact-HEAD CI repair dispatch correlation"
REPO = "Kodmial/Continuum"
PR = 449
HEAD = "a" * 40

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('github', 'context', 'core', source);
const context = { repo: { owner: 'Kodmial', repo: 'Continuum' } };
const results = {};

(async () => {
  for (const item of payload.cases) {
    process.env.PR_NUMBER = String(payload.pullNumber);
    process.env.CI_ATTEMPT_ID = item.requestedAttempt === undefined ? '' : item.requestedAttempt;
    process.env.CI_HEAD_SHA = item.head === undefined ? payload.head : item.head;
    process.env.DISPATCH_RUN_ID = String(item.dispatchRunId || 9001);
    const calls = { warnings: [], notices: [], created: [] };
    const core = {
      warning: message => calls.warnings.push(String(message)),
      notice: message => calls.notices.push(String(message)),
      info: message => calls.notices.push(String(message)),
    };
    const github = {
      paginate: async () => {
        if (item.paginateError) throw new Error(item.paginateError);
        return item.comments || [];
      },
      rest: {
        issues: {
          listComments: async () => ({}),
          createComment: async args => {
            calls.created.push(args);
            return { data: { id: 1000 } };
          },
        },
        users: {
          getAuthenticated: async () => ({
            data: { login: item.tokenLogin || 'github-actions[bot]' },
          }),
        },
      },
    };
    try {
      await run(github, context, core);
      calls.ok = true;
    } catch (error) {
      calls.ok = false;
      calls.error = String((error && error.message) || error);
    }
    results[item.name] = calls;
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


def receipt(
    attempt: str,
    status: str,
    *,
    comment_id: int,
    head: str = HEAD,
    repository: str = REPO,
    pull_number: int = PR,
) -> dict:
    marker = (
        f"<!-- continuum-ci-repair-receipt v=1 repo={repository.lower()} "
        f"pr={pull_number} head={head} attempt={attempt} status={status} -->"
    )
    return {
        "id": comment_id,
        "body": marker,
        "author_association": "OWNER",
        "user": {"login": "kodmial"},
    }


class CiRepairCorrelationStepTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / ".opencode-tmp"
        root.mkdir(exist_ok=True)
        self.dir = Path(
            tempfile.mkdtemp(prefix="ci-repair-correlation-", dir=str(root))
        )
        self.addCleanup(shutil.rmtree, self.dir, True)
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = [
            step
            for job in workflow["jobs"].values()
            for step in (job.get("steps") or [])
        ]
        matches = [step for step in steps if step.get("name") == STEP_NAME]
        self.assertEqual(len(matches), 1, "correlation step must exist exactly once")
        self.script = matches[0]["with"]["script"]

    def run_cases(self, cases):
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required for the github-script sandbox check; "
            "failing closed instead of silently skipping",
        )
        source = self.dir / "correlation.js"
        source.write_text(self.script, encoding="utf-8")
        harness = self.dir / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        completed = subprocess.run(
            [
                node,
                str(harness),
                str(source),
                json.dumps(
                    {"pullNumber": PR, "head": HEAD, "cases": cases}
                ),
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_pagination_failure_warns_without_writing_or_failing(self):
        result = self.run_cases(
            [
                {
                    "name": "read-failure",
                    "requestedAttempt": "ci-attempt-0001",
                    "paginateError": "pagination unavailable",
                }
            ]
        )["read-failure"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "Could not read CI repair receipts" in warning
                for warning in result["warnings"]
            ),
            result["warnings"],
        )

    def test_head_only_identity_never_selects_an_active_claim(self):
        result = self.run_cases(
            [
                {
                    "name": "head-only",
                    "comments": [
                        receipt("ci-attempt-0001", "claimed", comment_id=1),
                    ],
                }
            ]
        )["head-only"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "no verified attempt identity" in notice
                for notice in result["notices"]
            ),
            result["notices"],
        )

    def test_an_older_head_run_is_never_correlated_to_a_newer_claim(self):
        other_head = "b" * 40
        result = self.run_cases(
            [
                {
                    "name": "advanced",
                    "requestedAttempt": "ci-attempt-0001",
                    "head": HEAD,
                    "comments": [
                        receipt(
                            "ci-attempt-0002",
                            "claimed",
                            comment_id=2,
                            head=other_head,
                        ),
                    ],
                }
            ]
        )["advanced"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "not an active claim (no receipt)" in notice
                for notice in result["notices"]
            ),
            result["notices"],
        )

    def test_a_legacy_run_without_head_identity_never_uses_the_live_head(self):
        other_head = "b" * 40
        result = self.run_cases(
            [
                {
                    "name": "legacy-no-head",
                    "head": "",
                    "requestedAttempt": "",
                    "comments": [
                        receipt(
                            "ci-attempt-0002",
                            "claimed",
                            comment_id=2,
                            head=other_head,
                        ),
                    ],
                }
            ]
        )["legacy-no-head"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "no verified exact-HEAD dispatch evidence" in notice
                for notice in result["notices"]
            ),
            result["notices"],
        )

    def test_requested_attempt_uses_latest_rejected_state(self):
        result = self.run_cases(
            [
                {
                    "name": "rejected-requested",
                    "requestedAttempt": "ci-attempt-0001",
                    "comments": [
                        receipt("ci-attempt-0001", "claimed", comment_id=1),
                        receipt("ci-attempt-0001", "rejected", comment_id=2),
                    ],
                }
            ]
        )["rejected-requested"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "not an active claim (rejected)" in notice
                for notice in result["notices"]
            ),
            result["notices"],
        )

    def test_requested_attempt_without_a_receipt_is_not_correlated(self):
        result = self.run_cases(
            [
                {
                    "name": "missing-requested",
                    "requestedAttempt": "ci-attempt-0001",
                    "comments": [],
                }
            ]
        )["missing-requested"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any("not an active claim (no receipt)" in notice for notice in result["notices"]),
            result["notices"],
        )

    def test_requested_attempt_ignores_a_different_active_attempt(self):
        result = self.run_cases(
            [
                {
                    "name": "different-active",
                    "requestedAttempt": "ci-attempt-0001",
                    "comments": [
                        receipt("ci-attempt-0002", "claimed", comment_id=2),
                    ],
                }
            ]
        )["different-active"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["created"], [])
        self.assertTrue(
            any(
                "not an active claim (no receipt)" in notice
                for notice in result["notices"]
            ),
            result["notices"],
        )

    def test_latest_claim_wins_after_an_earlier_attempt_is_rejected(self):
        result = self.run_cases(
            [
                {
                    "name": "latest-claim",
                    "requestedAttempt": "ci-attempt-0002",
                    "comments": [
                        receipt("ci-attempt-0001", "claimed", comment_id=1),
                        receipt("ci-attempt-0001", "rejected", comment_id=2),
                        receipt("ci-attempt-0002", "claimed", comment_id=3),
                    ],
                }
            ]
        )["latest-claim"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["created"]), 1, result["created"])
        body = result["created"][0]["body"]
        self.assertIn(f"head={HEAD}", body)
        self.assertIn("attempt=ci-attempt-0002", body)
        self.assertIn("status=dispatched", body)
        self.assertNotIn("attempt=ci-attempt-0001", body)


if __name__ == "__main__":
    unittest.main()
