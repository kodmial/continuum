"""Executable tests for the reconciliation-dispatch caller contract.

`continuum-auto-merge.yml` may dispatch a sibling caller only when the exact
dispatch ref declares `workflow_dispatch` and every input the dispatch sends.
The embedded parser is the single source of that decision, so exercise the
production function rather than a copy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
WORKFLOW = ROOT / ".github/workflows/continuum-auto-merge.yml"

FUNCTION = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)function parseWorkflowDispatchContract"
    r"\(text, label, requiredInputs = \[\]\) \{\n.*?^(?P=indent)\}\n"
)

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);
const parse = new Function(source + '\nreturn parseWorkflowDispatchContract;')();
const out = {};
for (const item of payload.cases) {
  try {
    out[item.name] = parse(
      item.text,
      item.label || 'caller.yml@main',
      item.requiredInputs || []
    );
  } catch (err) {
    out[item.name] = { ok: false, error: String(err.message || err) };
  }
}
process.stdout.write(JSON.stringify(out));
"""


class WorkflowDispatchContractTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="dispatch-contract-", dir=str(TMP_PARENT))
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def parse(self, cases):
        text = WORKFLOW.read_text(encoding="utf-8")
        matches = FUNCTION.findall(text)
        self.assertEqual(
            len(matches), 1, "expected one parseWorkflowDispatchContract"
        )
        source = self.workdir / "parser.js"
        source.write_text(FUNCTION.search(text).group(0), encoding="utf-8")
        harness = self.workdir / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        completed = subprocess.run(
            ["node", str(harness), str(source), json.dumps({"cases": cases})],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_accepts_a_caller_that_declares_the_required_inputs(self):
        text = """on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "Pull request that woke the controller"
        required: false
        default: ''
        type: string
permissions:
  contents: read
"""
        result = self.parse(
            [
                {
                    "name": "pr",
                    "text": text,
                    "requiredInputs": ["pr_number"],
                }
            ]
        )
        self.assertTrue(result["pr"]["ok"], result["pr"])
        self.assertEqual(["pr_number"], result["pr"]["inputs"])

    def test_bare_dispatch_has_no_inputs(self):
        text = """on:
  workflow_dispatch:
permissions:
  contents: read
"""
        result = self.parse(
            [
                {"name": "empty", "text": text, "requiredInputs": []},
                {
                    "name": "missing",
                    "text": text,
                    "requiredInputs": ["pr_number"],
                },
            ]
        )
        self.assertTrue(result["empty"]["ok"], result["empty"])
        self.assertFalse(result["missing"]["ok"])
        self.assertIn("required input(s): pr_number", result["missing"]["reason"])

    def test_rejects_a_caller_without_workflow_dispatch(self):
        text = """on:
  pull_request:
    types: [opened]
permissions: {}
"""
        result = self.parse([{"name": "absent", "text": text}])
        self.assertFalse(result["absent"]["ok"])
        self.assertIn("does not declare workflow_dispatch", result["absent"]["reason"])

    def test_does_not_read_inputs_from_another_trigger(self):
        text = """on:
  workflow_dispatch:
    inputs:
      mode:
        description: "Mode"
        required: true
        type: string
  pull_request:
    inputs:
      pr_number:
        type: string
"""
        result = self.parse(
            [
                {
                    "name": "cross-trigger",
                    "text": text,
                    "requiredInputs": ["mode", "pr_number"],
                }
            ]
        )
        self.assertFalse(result["cross-trigger"]["ok"])
        self.assertEqual(["mode"], result["cross-trigger"]["inputs"])
        self.assertIn("required input(s): pr_number", result["cross-trigger"]["reason"])

    def test_ignores_comments_and_accepts_a_trailing_comment(self):
        text = """on:
  workflow_dispatch: # manual reconciliation
    inputs:
      # the PR identity
      pr_number:
        description: "PR"
        required: false
        type: string
permissions: {}
"""
        result = self.parse(
            [
                {
                    "name": "commented",
                    "text": text,
                    "requiredInputs": ["pr_number"],
                }
            ]
        )
        self.assertTrue(result["commented"]["ok"], result["commented"])

    def test_normalizes_crlf_line_endings(self):
        text = (
            "on:\r\n"
            "  workflow_dispatch:\r\n"
            "    inputs:\r\n"
            "      pr_number:\r\n"
            "        type: string\r\n"
            "permissions: {}\r\n"
        )
        result = self.parse(
            [{"name": "crlf", "text": text, "requiredInputs": ["pr_number"]}]
        )
        self.assertTrue(result["crlf"]["ok"], result["crlf"])

    def test_all_reconciliation_dispatches_use_one_gate(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("function parseWorkflowDispatchContract("), 1)
        self.assertEqual(text.count("async function reconciliationDispatchSupport("), 1)
        self.assertEqual(text.count("await reconciliationDispatchSupport("), 4)
        self.assertIn("return parseWorkflowDispatchContract(", text)


if __name__ == "__main__":
    unittest.main()
