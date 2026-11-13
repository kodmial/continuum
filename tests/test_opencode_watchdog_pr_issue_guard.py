"""Regression: OpenCode issue watchdog must not retry PR conversation events."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / ".github/workflows/continuum-opencode-watchdog.yml"


class WatchdogPrIssueGuardTest(unittest.TestCase):
    def test_pr_guard_stands_down_before_retry_and_pause_paths(self) -> None:
        source = WATCHDOG.read_text(encoding="utf-8")
        anchor = "const issue = issueResponse.data;"
        self.assertEqual(source.count(anchor), 1)
        after_issue = source.split(anchor, 1)[1]
        before_operator_stop = after_issue.split("const operatorStopRe", 1)[0]
        self.assertIn("if (issue.pull_request)", before_operator_stop)
        self.assertIn("core.notice(", before_operator_stop)
        self.assertIn("return;", before_operator_stop)
        self.assertLess(
            before_operator_stop.index("if (issue.pull_request)"),
            before_operator_stop.index("if (issue.state !== 'open')"),
        )
        self.assertNotIn("retryMarker", before_operator_stop)


if __name__ == "__main__":
    unittest.main()
