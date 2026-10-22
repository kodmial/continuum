"""Legacy (pre-metadata) 429 checkpoint recovery (kodmial/continuum#373).

Covers the Python validation helpers plus the *actual* inline JavaScript
decisions extracted from the scheduler and watchdog workflows. A legacy
`opencode/checkpoint-issue<N>` tip is only replayable through standard issue
mode when it is bound to a same-tip 429/infra generation, carries no recovery
metadata, and the issue is still owner-admitted. The recorded stage is never
an authorization to skip the agent or tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from continuum import opencode_429_recovery as recovery

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
TMP_PARENT = ROOT / ".opencode-tmp"

ISSUE = "357"
SHA = "a" * 40
OPERATION = "opencode-429-issue-issue357"
CHECKPOINT = "opencode/429-checkpoint-" + OPERATION + "-gen-2"
LEGACY = "opencode/checkpoint-issue357"

HARNESS = r"""
const scenario = JSON.parse(process.env.SCENARIO_JSON || '{}');
const out = [];
for (const check of scenario.checks || []) {
  const args = check.args || {};
  let value;
  try {
    if (check.fn === 'bind') value = bindLegacy429Checkpoint(args);
    else if (check.fn === 'decide') value = decideExpiredLeaseOutcome(args);
    else if (check.fn === 'marker') value = legacyLeaseMarker(args);
    else if (check.fn === 'parseLease') {
      value = parseLegacyLeaseState(
        args.body, args.operation, args.generation
      );
    } else if (check.fn === 'dispatchInputs') {
      value = legacyRecoveryDispatchInputs(args.issueNumber);
    } else if (check.fn === 'admission') {
      value = decideLegacyRecoveryAdmission(args);
    } else if (check.fn === 'legacyRef') {
      value = legacyIssueCheckpointRef(args.issueNumber);
    } else if (check.fn === 'parseRef') {
      value = parseCheckpointRef(args.ref);
    } else {
      value = { unknown: check.fn };
    }
    out.push({ fn: check.fn, value });
  } catch (error) {
    out.push({
      fn: check.fn,
      error: String((error && error.message) || error),
    });
  }
}
process.stdout.write(JSON.stringify(out));
"""


def extract_between(path: Path, start: str, end: str) -> str:
    body = path.read_text(encoding="utf-8")
    begin = body.index(start)
    finish = body.index(end, begin)
    return textwrap.dedent(body[begin:finish])


def scheduler_helpers() -> str:
    return extract_between(
        SCHEDULER,
        "const LEGACY_ISSUE_CHECKPOINT_PREFIX =",
        "const newestRecovery = recoveryComments",
    )


def watchdog_helpers() -> str:
    return extract_between(
        WATCHDOG,
        "const LEGACY_ISSUE_CHECKPOINT_PREFIX =",
        "async function observeDispatchedRun(",
    )


class ProductionJavaScriptTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="legacy-429-", dir=str(TMP_PARENT))
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_helpers(self, source: str, checks: list[dict]) -> list[dict]:
        script = self.workdir / "helpers.js"
        script.write_text(
            source
            + "\nconst available = {};\n"
            + "for (const name of [\n"
            + "  'bindLegacy429Checkpoint', 'decideExpiredLeaseOutcome',\n"
            + "  'legacyLeaseMarker', 'parseLegacyLeaseState',\n"
            + "  'legacyRecoveryDispatchInputs', 'decideLegacyRecoveryAdmission',\n"
            + "  'legacyIssueCheckpointRef', 'parseCheckpointRef',\n"
            + "]) {\n"
            + "  try {\n"
            + "    const fn = eval(name);\n"
            + "    if (typeof fn === 'function') available[name] = fn;\n"
            + "  } catch (error) {}\n"
            + "}\n"
            + "module.exports = available;\n",
            encoding="utf-8",
        )
        harness = self.workdir / "harness.js"
        harness.write_text(
            "const helpers = require(" + json.dumps(str(script)) + ");\n"
            + "Object.assign(globalThis, helpers);\n"
            + HARNESS,
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["SCENARIO_JSON"] = json.dumps({"checks": checks})
        completed = subprocess.run(
            ["node", str(harness)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + completed.stderr
        )
        return json.loads(completed.stdout)

    def test_scheduler_binds_only_exact_issue_checkpoint(self):
        results = self.run_helpers(
            scheduler_helpers(),
            [
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [
                            {"ref": CHECKPOINT, "sha": SHA},
                            {"ref": "opencode/429-checkpoint-" + OPERATION + "-gen-1", "sha": "b" * 40},
                        ],
                        "stageFileContent": "publish-pr\n",
                        "metadataPresent": False,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": "opencode/checkpoint-issue358",
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "implementation-incomplete\n",
                        "metadataPresent": False,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "publish-pr\n",
                        "metadataPresent": True,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": "b" * 40}],
                        "stageFileContent": "tests-passed\n",
                        "metadataPresent": False,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "",
                        "metadataPresent": False,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "not-a-stage\n",
                        "metadataPresent": False,
                    },
                },
            ],
        )
        bound = results[0]["value"]
        self.assertTrue(bound["ok"], results)
        self.assertEqual(bound["generation"], 2)
        self.assertEqual(bound["checkpointRef"], LEGACY)
        self.assertEqual(bound["checkpointSha"], SHA)
        self.assertEqual(bound["stage"], "publish-pr")
        self.assertFalse(bound["skipAgent"])
        self.assertFalse(results[1]["value"]["ok"])
        self.assertFalse(results[2]["value"]["ok"])
        self.assertFalse(results[3]["value"]["ok"])
        self.assertTrue(results[4]["value"]["ok"], results[4])
        self.assertEqual(
            results[4]["value"]["stage"], "implementation-incomplete"
        )
        self.assertFalse(results[4]["value"]["skipAgent"])
        self.assertFalse(results[5]["value"]["ok"])

    def test_scheduler_lease_decision_holds_then_bounds(self):
        results = self.run_helpers(
            scheduler_helpers(),
            [
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": True,
                        "legacyCheckpoint": None,
                        "legacyLeaseFresh": False,
                        "legacyLeaseAttempt": 0,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": True,
                    },
                },
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": False,
                        "legacyCheckpoint": {"ok": True},
                        "legacyLeaseFresh": True,
                        "legacyLeaseAttempt": 1,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": True,
                    },
                },
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": False,
                        "legacyCheckpoint": {"ok": True},
                        "legacyLeaseFresh": False,
                        "legacyLeaseAttempt": 1,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": True,
                    },
                },
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": False,
                        "legacyCheckpoint": {"ok": True},
                        "legacyLeaseFresh": False,
                        "legacyLeaseAttempt": 4,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": True,
                    },
                },
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": False,
                        "legacyCheckpoint": None,
                        "legacyLeaseFresh": False,
                        "legacyLeaseAttempt": 0,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": True,
                    },
                },
                {
                    "fn": "decide",
                    "args": {
                        "hasFresh429Recovery": False,
                        "legacyCheckpoint": None,
                        "legacyLeaseFresh": False,
                        "legacyLeaseAttempt": 0,
                        "maxLegacyLeases": 4,
                        "schedulerDispatchCount": 9,
                        "maxDispatchAttempts": 2,
                        "pauseOnFailure": False,
                    },
                },
            ],
        )
        values = [item["value"] for item in results]
        self.assertEqual(
            values,
            [
                "hold-429", "hold-legacy", "hold-legacy",
                "retry-legacy", "pause", "retry",
            ],
        )

    def test_scheduler_lease_marker_round_trips(self):
        results = self.run_helpers(
            scheduler_helpers(),
            [
                {
                    "fn": "marker",
                    "args": {
                        "operation": OPERATION,
                        "generation": 2,
                        "checkpointRef": LEGACY,
                        "checkpointSha": SHA,
                        "attempt": 2,
                        "nextDue": 123456,
                    },
                },
            ],
        )
        marker = results[0]["value"]
        self.assertIn("continuum-429-legacy-lease", marker)
        parsed = self.run_helpers(
            scheduler_helpers(),
            [
                {
                    "fn": "parseLease",
                    "args": {
                        "body": "before\n" + marker + "\nafter",
                        "operation": OPERATION,
                        "generation": 2,
                    },
                },
                {
                    "fn": "parseLease",
                    "args": {
                        "body": marker,
                        "operation": OPERATION,
                        "generation": 3,
                    },
                },
            ],
        )
        self.assertEqual(parsed[0]["value"]["attempt"], 2)
        self.assertEqual(parsed[0]["value"]["nextDue"], 123456)
        self.assertEqual(parsed[0]["value"]["checkpointRef"], LEGACY)
        self.assertEqual(parsed[0]["value"]["checkpointSha"], SHA)
        self.assertIsNone(parsed[1]["value"])

    def test_watchdog_replay_uses_plain_issue_mode(self):
        results = self.run_helpers(
            watchdog_helpers(),
            [
                {"fn": "dispatchInputs", "args": {"issueNumber": ISSUE}},
                {"fn": "legacyRef", "args": {"issueNumber": ISSUE}},
                {"fn": "legacyRef", "args": {"issueNumber": "abc"}},
                {"fn": "parseRef", "args": {"ref": CHECKPOINT}},
                {"fn": "parseRef", "args": {"ref": "opencode/checkpoint-issue357"}},
            ],
        )
        self.assertEqual(
            results[0]["value"], {"mode": "issue", "issue_number": ISSUE}
        )
        self.assertEqual(results[1]["value"], LEGACY)
        self.assertEqual(results[2]["value"], "")
        self.assertEqual(
            results[3]["value"],
            {"operationId": OPERATION, "generation": 2},
        )
        self.assertIsNone(results[4]["value"])

    def test_watchdog_admission_never_grants_authorization(self):
        base = {
            "issueState": "open",
            "labels": ["automation:ready"],
            "openIssuePullRequests": False,
            "activeRunId": 0,
            "openBlockers": [],
            "readyLabel": "automation:ready",
            "pausedLabel": "automation:paused",
            "blockedLabel": "automation:blocked",
            "qualifyingLabel": "automation:qualifying",
        }
        cases = [
            (base, True),
            ({**base, "issueState": "closed"}, False),
            ({**base, "labels": ["automation:ready", "automation:paused"]}, False),
            ({**base, "labels": ["automation:ready", "automation:blocked"]}, False),
            ({**base, "labels": ["automation:ready", "automation:qualifying"]}, False),
            ({**base, "labels": []}, False),
            ({**base, "labels": [], "readyLabel": ""}, True),
            ({**base, "openIssuePullRequests": True}, False),
            ({**base, "activeRunId": 123}, False),
            ({**base, "openBlockers": [1]}, False),
        ]
        results = self.run_helpers(
            watchdog_helpers(),
            [{"fn": "admission", "args": args} for args, _ in cases],
        )
        for (_, expected), result in zip(cases, results):
            self.assertEqual(result["value"]["ok"], expected, result)

    def test_watchdog_binder_matches_python_contract(self):
        results = self.run_helpers(
            watchdog_helpers(),
            [
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "publish-pr\n",
                        "metadataPresent": False,
                    },
                },
                {
                    "fn": "bind",
                    "args": {
                        "issueNumber": ISSUE,
                        "legacyRef": LEGACY,
                        "legacySha": SHA,
                        "checkpointRefs": [{"ref": CHECKPOINT, "sha": SHA}],
                        "stageFileContent": "publish-pr\n",
                        "metadataPresent": True,
                    },
                },
            ],
        )
        self.assertTrue(results[0]["value"]["ok"])
        self.assertFalse(results[0]["value"]["skipAgent"])
        self.assertFalse(results[1]["value"]["ok"])


class PythonLegacyContractTest(unittest.TestCase):
    def refs(self):
        return [
            {"ref": CHECKPOINT, "sha": SHA},
            {"ref": "opencode/infra-checkpoint-" + OPERATION + "-gen-3", "sha": SHA},
            {"ref": "opencode/429-checkpoint-" + OPERATION + "-gen-9", "sha": "b" * 40},
        ]

    def test_ref_round_trip(self):
        self.assertEqual(recovery.legacy_issue_checkpoint_ref(ISSUE), LEGACY)
        self.assertEqual(
            recovery.parse_legacy_issue_checkpoint_ref(LEGACY), 357
        )
        self.assertEqual(recovery.legacy_issue_checkpoint_ref("x"), "")
        self.assertEqual(recovery.legacy_issue_checkpoint_ref(""), "")
        self.assertIsNone(
            recovery.parse_legacy_issue_checkpoint_ref("opencode/issue357")
        )
        self.assertIsNone(
            recovery.parse_legacy_issue_checkpoint_ref("opencode/checkpoint-issuex")
        )

    def test_validate_requires_exact_tip_and_no_metadata(self):
        ok, reason = recovery.validate_legacy_checkpoint(
            issue_number=ISSUE,
            legacy_ref=LEGACY,
            legacy_sha=SHA.upper(),
            checkpoint_refs=self.refs(),
            stage_file_content="publish-pr\n",
            metadata_present=False,
        )
        self.assertTrue(ok, reason)
        self.assertEqual(
            recovery.legacy_checkpoint_generation(
                issue_number=ISSUE, legacy_sha=SHA, checkpoint_refs=self.refs()
            ),
            3,
        )
        self.assertEqual(
            recovery.legacy_recovery_stage("publish-pr\n"),
            "implementation-incomplete",
        )
        ok, reason = recovery.validate_legacy_checkpoint(
            issue_number=ISSUE,
            legacy_ref=LEGACY,
            legacy_sha=SHA,
            checkpoint_refs=self.refs(),
            stage_file_content="",
            metadata_present=False,
        )
        self.assertTrue(ok, reason)
        self.assertFalse(
            recovery.validate_legacy_checkpoint(
                issue_number=ISSUE,
                legacy_ref=LEGACY,
                legacy_sha=SHA,
                checkpoint_refs=self.refs(),
                stage_file_content="not-a-stage\n",
                metadata_present=False,
            )[0]
        )

    def test_validate_rejects_untrusted_shapes(self):
        cases = [
            dict(issue_number="", legacy_ref=LEGACY, legacy_sha=SHA,
                 checkpoint_refs=self.refs(), stage_file_content="publish-pr",
                 metadata_present=False),
            dict(issue_number=ISSUE, legacy_ref="opencode/checkpoint-issue358",
                 legacy_sha=SHA, checkpoint_refs=self.refs(),
                 stage_file_content="publish-pr", metadata_present=False),
            dict(issue_number=ISSUE, legacy_ref=LEGACY, legacy_sha="not-a-sha",
                 checkpoint_refs=self.refs(), stage_file_content="publish-pr",
                 metadata_present=False),
            dict(issue_number=ISSUE, legacy_ref=LEGACY, legacy_sha=SHA,
                 checkpoint_refs=self.refs(), stage_file_content="unknown",
                 metadata_present=False),
            dict(issue_number=ISSUE, legacy_ref=LEGACY, legacy_sha=SHA,
                 checkpoint_refs=self.refs(), stage_file_content="publish-pr",
                 metadata_present=True),
            dict(issue_number=ISSUE, legacy_ref=LEGACY, legacy_sha=SHA,
                 checkpoint_refs=[{"ref": CHECKPOINT, "sha": "c" * 40}],
                 stage_file_content="publish-pr", metadata_present=False),
            dict(issue_number=ISSUE, legacy_ref=LEGACY, legacy_sha=SHA,
                 checkpoint_refs="malformed", stage_file_content="publish-pr",
                 metadata_present=False),
        ]
        for case in cases:
            ok, _ = recovery.validate_legacy_checkpoint(**case)
            self.assertFalse(ok, case)


if __name__ == "__main__":
    unittest.main()
