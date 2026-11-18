"""Executed GitHub Actions errors, not echoed shell source, trigger publish-race recovery."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/continuum-opencode-watchdog.yml"
)
PUBLISH_RACE = "CONTINUUM_OPENCODE_PUBLISH_RACE_RETRY_REQUIRED"


def emitted_error(log):
    """Execute the actual watchdog predicate rather than a test reimplementation."""
    source = WORKFLOW.read_text(encoding="utf-8")
    begin = "            const hasEmittedWorkflowError = (logText, marker) =>"
    end = "            const metadataFailedMarker ="
    assert source.count(begin) == 1 and source.count(end) == 1
    predicate = source[source.index(begin):source.index(end, source.index(begin))]
    if not shutil.which("node"):
        raise unittest.SkipTest("Node.js is required to execute watchdog JavaScript")
    javascript = (
        predicate
        + "\nconsole.log(JSON.stringify(hasEmittedWorkflowError("
        + json.dumps(log)
        + ", "
        + json.dumps(PUBLISH_RACE)
        + ")));\n"
    )
    result = subprocess.run(
        ["node", "-"], input=javascript, text=True,
        capture_output=True, check=False, timeout=15,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


class WatchdogPublishRaceTests(unittest.TestCase):
    def test_echoed_shell_source_does_not_trigger_rerun(self):
        log = (
            '2026-11-17T20:43:32Z \\u001b[36;1m'
            '    echo "::error::' + PUBLISH_RACE + '"\\u001b[0m'
        )
        self.assertFalse(emitted_error(log))

    def test_executed_publish_race_error_triggers_recovery(self):
        self.assertTrue(emitted_error(
            "2026-11-17T20:43:32Z ##[error]" + PUBLISH_RACE
        ))

    def test_unrelated_executed_error_does_not_trigger_rerun(self):
        self.assertFalse(emitted_error(
            "2026-11-17T20:43:32Z ##[error]"
            "CONTINUUM_OPENCODE_429_RESTART_REQUIRED"
        ))

    def test_watchdog_dispatch_is_gated_by_emitted_error(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "if (hasEmittedWorkflowError(logText, publishRaceMarker))", source
        )
        self.assertNotIn("if (logText.includes(publishRaceMarker))", source)

    def test_github_rest_exhaustion_halts_both_log_scans(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("const isExhaustedGithubREST = (response)", source)
        self.assertEqual(source.count("if (isExhaustedGithubREST(response))"), 2)
        self.assertIn("githubRateResponse.json()).resources?.core", source)
        self.assertIn("return;\n                    }\n                    continue;", source)
        self.assertIn("GitHub REST 403 budget exhausted; stop this watchdog pass", source)


if __name__ == "__main__":
    unittest.main()
