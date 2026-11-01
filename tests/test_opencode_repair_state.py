"""Shared latest-state classification for the OpenCode repair controller.

The consumer-reported CI dispatch and the ``workflow_run`` completion must
agree on when a head is repairable, in flight, green, or untrusted; only
the resulting lock/repair action differs by admission path.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from continuum.opencode_repair_state import classify_latest_state

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".github" / "scripts" / "opencode_repair_state.py"


def run(run_id, name, status="completed", conclusion="success"):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def listing(*runs, total=None):
    payload = {"workflow_runs": list(runs)}
    payload["total_count"] = len(runs) if total is None else total
    return payload


def classify(payload, **kwargs):
    kwargs.setdefault("trusted", True)
    kwargs.setdefault("allowed_workflows", "CI, Windows host")
    return classify_latest_state(json.dumps(payload), **kwargs)


class LatestStateTests(unittest.TestCase):
    def test_green_requires_a_trusted_complete_listing(self):
        payload = listing(
            run(1, "CI", conclusion="success"),
            run(2, "Windows host", conclusion="success"),
        )
        self.assertEqual(("green", "", ""), classify(payload))
        self.assertEqual(
            ("empty", "", ""), classify(payload, trusted=False)
        )
        self.assertEqual(
            ("empty", "", ""),
            classify(listing(run(1, "CI"), total=101)),
        )
        self.assertEqual(
            ("unknown", "", ""),
            classify(
                listing(run(1, "CI"), total=101),
                own_name="CI",
                own_id="1",
                own_conclusion="success",
            ),
        )

    def test_a_failing_approved_workflow_beats_green_and_inflight(self):
        payload = listing(
            run(1, "CI", conclusion="success"),
            run(2, "Windows host", conclusion="failure"),
            run(3, "Docs", status="in_progress", conclusion=None),
        )
        self.assertEqual(("failure", "2", "Windows host"), classify(payload))

    def test_failure_uses_the_lowest_run_id_for_stable_reporting(self):
        payload = listing(
            run(9, "Windows host", conclusion="failure"),
            run(4, "CI", conclusion="timed_out"),
        )
        self.assertEqual(("failure", "4", "CI"), classify(payload))

    def test_inflight_holds_when_no_approved_workflow_failed(self):
        payload = listing(
            run(1, "CI", conclusion="success"),
            run(2, "Windows host", status="in_progress", conclusion=None),
        )
        self.assertEqual(("inflight", "", "Windows host"), classify(payload))

    def test_unapproved_workflows_are_ignored_and_cannot_force_green(self):
        payload = listing(
            run(1, "CI", conclusion="success"),
            run(2, "Release", conclusion="failure"),
        )
        self.assertEqual(("green", "", ""), classify(payload))

    def test_latest_run_per_workflow_wins(self):
        payload = listing(
            run(1, "CI", conclusion="failure"),
            run(3, "CI", conclusion="success"),
        )
        self.assertEqual(("green", "", ""), classify(payload))

    def test_own_result_is_merged_with_or_without_force(self):
        payload = listing(run(1, "CI", conclusion="success"))
        self.assertEqual(
            ("failure", "9", "Windows host"),
            classify(
                payload,
                own_name="Windows host",
                own_id="9",
                own_conclusion="failure",
            ),
        )
        self.assertEqual(
            ("green", "", ""),
            classify(
                payload,
                allowed_workflows="CI",
                own_name="Windows host",
                own_id="9",
                own_conclusion="failure",
            ),
        )
        self.assertEqual(
            ("failure", "9", "Windows host"),
            classify(
                payload,
                allowed_workflows="CI",
                own_name="Windows host",
                own_id="9",
                own_conclusion="failure",
                own_force=True,
            ),
        )

    def test_empty_and_malformed_listings_are_not_green(self):
        self.assertEqual(("empty", "", ""), classify(listing()))
        self.assertEqual(("empty", "", ""), classify({}, trusted=False))
        self.assertEqual(("empty", "", ""), classify({}, trusted=True))
        self.assertEqual(
            ("empty", "", ""),
            classify_latest_state("not json", trusted=True, allowed_workflows="CI"),
        )

    def test_cli_prints_the_three_line_contract(self):
        payload = listing(
            run(1, "CI", conclusion="success"),
            run(2, "Windows host", conclusion="failure"),
        )
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--runs-trusted",
                "1",
                "--allowed-workflows",
                "CI,Windows host",
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("failure\n2\nWindows host\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
