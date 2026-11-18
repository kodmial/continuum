"""Regression harness for kodmial/continuum#485.

The Layer 2 qualification step reads its issue inventory from
``gh issue list --json`` and used to hand any valid JSON straight to
``select_observations``. An unexpected shape could therefore be silently
treated as zero reservations. These tests extract the real, unmodified step
from the workflow and run it under an isolated fake ``gh``: malformed
inventories must fail closed, while an empty inventory and a genuinely
label-filtered inventory keep their existing green outcomes.
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
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-contract-qualification.yml"
TMP_PARENT = ROOT / ".opencode-tmp"
STEP_NAME = "Run Layer 2 lifecycle integration probe"
FULL_SHA = "a" * 40


def extracted_layer2_step() -> str:
    """The production run script of the Layer 2 lifecycle probe, unmodified."""

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in data["jobs"]["qualify"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"{STEP_NAME} step not found")


class Layer2IssueInventoryGuardTest(unittest.TestCase):
    """Run the production step with fake GitHub CLI inventory output."""

    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="layer2-inventory-", dir=str(TMP_PARENT))
        )
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        self.fixtures = self.workdir / "fixtures"
        self.fixtures.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  issue)\n"
            "    [[ \"${2:-}\" == \"list\" ]] || { echo \"unexpected gh: $*\" >&2; exit 1; }\n"
            "    cat \"$FAKE_GH_DIR/issues.json\"\n"
            "    ;;\n"
            "  run)\n"
            "    [[ \"${2:-}\" == \"list\" ]] || { echo \"unexpected gh: $*\" >&2; exit 1; }\n"
            "    cat \"$FAKE_GH_DIR/runs.json\"\n"
            "    ;;\n"
            "  pr)\n"
            "    [[ \"${2:-}\" == \"list\" ]] || { echo \"unexpected gh: $*\" >&2; exit 1; }\n"
            "    cat \"$FAKE_GH_DIR/pulls.json\"\n"
            "    ;;\n"
            "  *)\n"
            "    echo \"unexpected gh invocation: $*\" >&2\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.script = self.workdir / "layer2-step.sh"
        self.script.write_text(extracted_layer2_step(), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def write_inventory(self, issues, runs=None, pulls=None):
        (self.fixtures / "issues.json").write_text(
            json.dumps(issues), encoding="utf-8"
        )
        (self.fixtures / "runs.json").write_text(
            json.dumps(runs or []), encoding="utf-8"
        )
        (self.fixtures / "pulls.json").write_text(
            json.dumps(pulls or []), encoding="utf-8"
        )

    def invoke(self):
        env = dict(os.environ)
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "GITHUB_REPOSITORY": "kodmial/continuum",
                "TARGET_SHA": FULL_SHA,
                "RUNNER_TEMP": str(self.workdir / "runner-temp"),
                "GH_TOKEN": "test-token",
                "FAKE_GH_DIR": str(self.fixtures),
            }
        )
        return subprocess.run(
            ["bash", str(self.script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def assert_failed_closed(self, completed, expected):
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn(expected, output)
        self.assertNotIn("real reservation(s); none stranded", output)

    def test_empty_inventory_remains_valid_zero_reservations(self):
        self.write_inventory([])
        completed = self.invoke()
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("Observed 0 real reservation(s); none stranded.", output)

    def test_label_filtered_issue_remains_observable(self):
        self.write_inventory(
            [
                {
                    "number": 42,
                    "state": "OPEN",
                    "labels": [{"name": "automation:in-progress"}],
                }
            ],
            runs=[
                {
                    "databaseId": 900,
                    "status": "in_progress",
                    "created_at": "2029-12-31T23:59:00Z",
                    "headBranch": "opencode/issue42-test",
                }
            ],
        )
        completed = self.invoke()
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("Observed 1 real reservation(s); none stranded.", output)
        self.assertIn("observed reservation issue=42", output)

    def test_unexpected_object_fails_closed(self):
        self.write_inventory({})
        completed = self.invoke()
        self.assert_failed_closed(
            completed, "issue inventory must be a JSON list, got dict"
        )

    def test_empty_labels_fail_closed(self):
        self.write_inventory(
            [{"number": 42, "state": "OPEN", "labels": []}]
        )
        completed = self.invoke()
        self.assert_failed_closed(
            completed,
            "issue #42 is missing the automation:in-progress label",
        )

    def test_missing_labels_fail_closed(self):
        self.write_inventory([{"number": 42, "state": "OPEN"}])
        completed = self.invoke()
        self.assert_failed_closed(
            completed, "issue #42 has no labels list"
        )

    def test_non_object_entry_fails_closed(self):
        self.write_inventory([42])
        completed = self.invoke()
        self.assert_failed_closed(
            completed,
            "issue inventory entry 0 is not a JSON object (int)",
        )

    def test_missing_number_is_reported_as_placeholder(self):
        self.write_inventory([{"state": "OPEN", "labels": []}])
        completed = self.invoke()
        self.assert_failed_closed(
            completed, "issue #? is missing the automation:in-progress label"
        )


if __name__ == "__main__":
    unittest.main()
