"""Durable 429 recovery across the recent-Actions window (kodmial/continuum#418).

The watchdog tests execute the checked-in scheduled github-script body with
hundreds of newer unrelated runs, so the historical run is outside the newest
200/300 listing. The scheduler tests execute the checked-in
``evaluateScheduler429Recovery`` function to prove that WIP ownership follows
the exact run and metadata-verified checkpoint instead of a fixed 48h horizon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"

OWNER = "kodmial"
REPO = "continuum"
OPERATION = "opencode-429-issue-issue382"
CHECKPOINT_REF = "opencode/429-checkpoint-opencode-429-issue-issue382-gen-1"
OLD_SHA = "866301983c084f9d56eb3155066408af8006b361"
NEW_SHA = "17f88cfcb02c122defc4840b73e316d96a426e03"
OLD_RUN_ID = "39885792009"
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def extract_function(body: str, name: str) -> str:
    needle = "function " + name + "("
    start = body.index(needle)
    if body[max(0, start - len("async ")):start] == "async ":
        start -= len("async ")
    signature_end = body.index(")", body.index("(", start))
    brace = body.index("{", signature_end)
    depth = 0
    for position in range(brace, len(body)):
        if body[position] == "{":
            depth += 1
        elif body[position] == "}":
            depth -= 1
            if depth == 0:
                return body[start:position + 1]
    raise AssertionError("could not extract JS function: " + name)


def recovery_marker(state="observed", sha=OLD_SHA, run_id=OLD_RUN_ID, **kwargs):
    return (
        "<!-- continuum-429-recovery op={op} gen=1 state={state} "
        "attempt={attempt} next_due={next_due} checkpoint={ref} "
        "sha={sha} run={run_id} detail=test -->"
    ).format(
        op=OPERATION,
        state=state,
        attempt=kwargs.get("attempt", 1),
        next_due=kwargs.get("next_due", 0),
        ref=CHECKPOINT_REF,
        sha=sha,
        run_id=run_id,
    )


def metadata(sha=NEW_SHA):
    return {
        "schema_version": 1,
        "operation_id": OPERATION,
        "failure_kind": "quota_429",
        "generation": 1,
        "stage": "tests-passed",
        "mode": "issue",
        "issue_number": "382",
        "pr_number": "",
        "head_ref": "opencode/issue382-39933043642",
        "base_sha": BASE_SHA,
        "head_sha": sha,
        "branch": "opencode/issue382-39933043642",
        "checkpoint_ref": CHECKPOINT_REF,
    }


NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const source = fs.readFileSync(process.argv[3], 'utf8');
const resultPath = process.argv[4];

function apiError(status, message) {
  const error = new Error(message || ('HTTP ' + status));
  error.status = status;
  return error;
}

const runLookups = [];
const readGithub = {
  rest: {
    actions: {
      getWorkflowRun: async ({ run_id }) => {
        const key = String(run_id);
        runLookups.push(key);
        if (scenario.runErrors && scenario.runErrors[key]) {
          throw apiError(scenario.runErrors[key], 'run lookup failed');
        }
        const run = (scenario.exactRuns || []).find(
          item => String(item.id) === key
        );
        if (!run) throw apiError(404, 'run not found');
        return { data: { ...run } };
      },
    },
    git: {
      getRef: async () => {
        if (scenario.refError) throw apiError(scenario.refError, 'ref failed');
        if (scenario.missingRef) throw apiError(404, 'ref not found');
        return { data: { object: { sha: scenario.refSha || '' } } };
      },
    },
    repos: {
      getContent: async () => {
        if (scenario.metadataError) {
          throw apiError(scenario.metadataError, 'metadata failed');
        }
        if (scenario.missingMetadata) {
          throw apiError(404, 'metadata not found');
        }
        const value = scenario.metadata;
        const text = typeof value === 'string' ? value : JSON.stringify(value);
        return {
          data: {
            type: 'file',
            content: Buffer.from(text, 'utf8').toString('base64'),
          },
        };
      },
    },
  },
};

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const factory = new AsyncFunction(
  'owner', 'repo', 'workflowRuns', 'readGithub',
  source + '\nreturn evaluateScheduler429Recovery;'
);
(async () => {
  const evaluate = await factory(
    scenario.owner || 'kodmial',
    scenario.repo || 'continuum',
    scenario.workflowRuns || [],
    readGithub
  );
  const evaluation = await evaluate({
    recovery: scenario.recovery,
    issueNumber: scenario.issueNumber || 382,
  });
  fs.writeFileSync(
    resultPath,
    JSON.stringify({ evaluation, runLookups }, null, 2),
    'utf8'
  );
})().catch(error => {
  fs.writeFileSync(
    resultPath,
    JSON.stringify({ harnessError: String(error && error.stack || error) }),
    'utf8'
  );
  process.exitCode = 1;
});
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class SchedulerDurableRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        body = SCHEDULER.read_text(encoding="utf-8")
        cls.source = "\n\n".join(
            extract_function(body, name)
            for name in (
                "parseSchedulerCheckpointIdentity",
                "fetchSchedulerRecoveryRun",
                "readSchedulerCheckpointEvidence",
                "evaluateScheduler429Recovery",
            )
        )

    def run_scenario(self, scenario):
        workdir = Path(tempfile.mkdtemp(prefix="scheduler-429-", dir=str(TMP_PARENT)))
        try:
            source_path = workdir / "evaluate.js"
            scenario_path = workdir / "scenario.json"
            result_path = workdir / "result.json"
            harness_path = workdir / "harness.js"
            source_path.write_text(self.source, encoding="utf-8")
            scenario_path.write_text(
                json.dumps(scenario, indent=2), encoding="utf-8"
            )
            harness_path.write_text(NODE_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                ["node", str(harness_path), str(scenario_path),
                 str(source_path), str(result_path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=120,
            )
            self.assertEqual(
                completed.returncode, 0,
                (completed.stdout or "") + (completed.stderr or ""),
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertNotIn("harnessError", result, result.get("harnessError"))
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def base_scenario(self):
        return {
            "recovery": {
                "operation": OPERATION,
                "generation": 1,
                "state": "observed",
                "attempt": 1,
                "nextDue": 0,
                "checkpointRef": CHECKPOINT_REF,
                "checkpointSha": OLD_SHA,
                "runId": OLD_RUN_ID,
            },
            "issueNumber": 382,
            # The historical run is deliberately absent from the recent list.
            "workflowRuns": [
                {
                    "id": 700000 + index,
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/other.yml",
                }
                for index in range(320)
            ],
            "exactRuns": [
                {
                    "id": int(OLD_RUN_ID),
                    "status": "completed",
                    "conclusion": "failure",
                    "path": ".github/workflows/opencode.yml",
                }
            ],
            "refSha": NEW_SHA,
            "metadata": metadata(NEW_SHA),
        }

    def test_old_observed_run_with_newer_checkpoint_releases_coding_wip(self):
        result = self.run_scenario(self.base_scenario())
        evaluation = result["evaluation"]
        self.assertTrue(evaluation["keepReservation"])
        self.assertTrue(evaluation["recoveryPending"])
        self.assertFalse(evaluation["countWip"])
        self.assertEqual(evaluation["reason"], "checkpoint-awaiting-recovery")
        self.assertEqual(evaluation["checkpointSha"], NEW_SHA)
        self.assertEqual(result["runLookups"], [OLD_RUN_ID])

    def test_exact_active_run_is_counted_as_wip(self):
        scenario = self.base_scenario()
        scenario["exactRuns"][0]["status"] = "in_progress"
        scenario["exactRuns"][0]["conclusion"] = None
        evaluation = self.run_scenario(scenario)["evaluation"]
        self.assertTrue(evaluation["keepReservation"])
        self.assertTrue(evaluation["countWip"])
        self.assertFalse(evaluation["recoveryPending"])
        self.assertEqual(evaluation["reason"], "run-active")

    def test_recent_active_run_does_not_need_exact_lookup(self):
        scenario = self.base_scenario()
        scenario["workflowRuns"].insert(
            0,
            {
                "id": int(OLD_RUN_ID),
                "status": "queued",
                "conclusion": None,
                "path": ".github/workflows/opencode.yml",
            },
        )
        result = self.run_scenario(scenario)
        self.assertTrue(result["evaluation"]["countWip"])
        self.assertEqual(result["runLookups"], [])

    def test_run_api_unavailable_fails_closed_without_wip(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["runErrors"] = {OLD_RUN_ID: status}
                evaluation = self.run_scenario(scenario)["evaluation"]
                self.assertTrue(evaluation["keepReservation"])
                self.assertTrue(evaluation["recoveryPending"])
                self.assertFalse(evaluation["countWip"])
                self.assertEqual(evaluation["reason"], "run-unavailable")

    def test_checkpoint_api_unavailable_fails_closed(self):
        scenario = self.base_scenario()
        scenario["refError"] = 403
        evaluation = self.run_scenario(scenario)["evaluation"]
        self.assertTrue(evaluation["keepReservation"])
        self.assertTrue(evaluation["recoveryPending"])
        self.assertEqual(evaluation["reason"], "checkpoint-unavailable")

    def test_same_checkpoint_is_not_treated_as_pending_recovery(self):
        scenario = self.base_scenario()
        scenario["recovery"]["checkpointSha"] = NEW_SHA
        evaluation = self.run_scenario(scenario)["evaluation"]
        self.assertFalse(evaluation["keepReservation"])
        self.assertFalse(evaluation["recoveryPending"])
        self.assertEqual(evaluation["reason"], "checkpoint-unchanged")

    def test_invalid_marker_sha_fails_closed(self):
        for value in ("", "not-a-sha", "-"):
            with self.subTest(value=value):
                scenario = self.base_scenario()
                scenario["recovery"]["checkpointSha"] = value
                evaluation = self.run_scenario(scenario)["evaluation"]
                self.assertTrue(evaluation["keepReservation"])
                self.assertTrue(evaluation["recoveryPending"])
                self.assertFalse(evaluation["countWip"])
                self.assertEqual(evaluation["reason"], "checkpoint-sha-invalid")

    def test_missing_or_invalid_metadata_releases_the_reservation(self):
        for name, update in (
            ("missing", {"missingMetadata": True}),
            ("bad-json", {"metadata": "{not-json"}),
            ("wrong-issue", {"metadata": {**metadata(), "issue_number": "373"}}),
            ("bad-sha", {"metadata": {**metadata(), "head_sha": "nope"}}),
        ):
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario.update(update)
                evaluation = self.run_scenario(scenario)["evaluation"]
                self.assertFalse(evaluation["keepReservation"])
                self.assertIn("checkpoint-invalid", evaluation["reason"])

    def test_completed_success_is_not_live_recovery_ownership(self):
        scenario = self.base_scenario()
        scenario["exactRuns"][0]["conclusion"] = "success"
        evaluation = self.run_scenario(scenario)["evaluation"]
        self.assertFalse(evaluation["keepReservation"])
        self.assertEqual(evaluation["reason"], "run-completed-success")

    def test_watchdog_lease_is_bounded_and_does_not_poll_runs(self):
        scenario = self.base_scenario()
        scenario["recovery"]["state"] = "claimed"
        scenario["recovery"]["nextDue"] = 4102444800
        result = self.run_scenario(scenario)
        self.assertTrue(result["evaluation"]["keepReservation"])
        self.assertTrue(result["evaluation"]["recoveryPending"])
        self.assertEqual(result["evaluation"]["reason"], "watchdog-lease")
        self.assertEqual(result["runLookups"], [])

    def test_metadata_failed_lease_expires_without_dropping_checkpoint(self):
        scenario = self.base_scenario()
        scenario["recovery"]["state"] = "metadata-failed"
        scenario["recovery"]["nextDue"] = 4102444800
        self.assertTrue(
            self.run_scenario(scenario)["evaluation"]["keepReservation"]
        )
        expired = self.base_scenario()
        expired["recovery"]["state"] = "metadata-failed"
        expired["recovery"]["nextDue"] = 1
        evaluation = self.run_scenario(expired)["evaluation"]
        self.assertFalse(evaluation["keepReservation"])
        self.assertEqual(evaluation["reason"], "metadata-failed-expired")

    def test_malformed_or_foreign_marker_fails_closed(self):
        for name, update in (
            ("foreign", {"operation": "opencode-429-issue-issue373"}),
            ("generation", {"generation": 2}),
            ("checkpoint", {"checkpointRef": CHECKPOINT_REF + "-other"}),
        ):
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario["recovery"].update(update)
                evaluation = self.run_scenario(scenario)["evaluation"]
                self.assertTrue(evaluation["keepReservation"])
                self.assertTrue(evaluation["recoveryPending"])
                self.assertEqual(evaluation["reason"], "malformed-marker")

    def test_48_hour_old_marker_has_no_cliff(self):
        # The evaluator never uses marker age: the exact run and checkpoint
        # decide. The same fixture is exercised at both boundaries.
        for days in (1, 2, 3):
            with self.subTest(days=days):
                scenario = self.base_scenario()
                scenario["markerAgeDays"] = days
                evaluation = self.run_scenario(scenario)["evaluation"]
                self.assertEqual(
                    evaluation["reason"], "checkpoint-awaiting-recovery"
                )
                self.assertFalse(evaluation["countWip"])

    def test_no_dispatch_record_still_retains_verified_recovery(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        block = body.split("if (!latestDispatch) {", 1)[1].split(
            "const ageMs", 1
        )[0]
        self.assertIn("if (hasFresh429Recovery) {", block)
        self.assertLess(
            block.index("if (hasFresh429Recovery) {"),
            block.index("removeLabel(issueNumber, inProgressLabel)"),
            "a verified recovery marker must survive a missing dispatch record",
        )

    def test_scheduler_source_wires_recovery_to_wip_lifecycle(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        self.assertIn("evaluateScheduler429Recovery", body)
        self.assertIn("getWorkflowRun", body)
        self.assertIn("recoveryPendingIssueNumbers", body)
        self.assertIn("recoveryEvaluation.countWip", body)
        self.assertIn("verified 429 checkpoint is awaiting watchdog recovery", body)
        self.assertIn("retaining the reservation for the watchdog without", body)
        self.assertNotIn("48 * 60 * 60 * 1000", body)


if __name__ == "__main__":
    unittest.main()
