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
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
WORKFLOW = ROOT / ".github/workflows/continuum-auto-merge.yml"
PR_AGENT_WORKFLOW = (
    ROOT / ".github/workflows/continuum-pr-agent-auto-merge.yml"
)
OPENCODE_STUB = ROOT / ".github/caller-stubs/continuum-opencode.yml"
PR_AGENT_REPAIR_STUB = (
    ROOT / ".github/caller-stubs/continuum-pr-agent-repair.yml"
)

FUNCTION = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)function parseWorkflowDispatchContract"
    r"\(text, label, requiredInputs = \[\]\) \{\n.*?^(?P=indent)\}\n"
)

SUPPORT = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)async function reconciliationDispatchSupport"
    r"\(.*?^(?P=indent)\}\n"
)

SUPPORT_HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);
const github = {
  rest: {
    repos: {
      getContent: async () => {
        if (payload.error) {
          const err = new Error(payload.error.message);
          err.status = payload.error.status;
          throw err;
        }
        return { data: payload.data };
      },
    },
  },
};
const support = new Function(
  'github',
  source + '\nreturn reconciliationDispatchSupport;'
)(github);
support('owner', 'repo', payload.workflow, 'main', payload.required)
  .then(value => {
    process.stdout.write(JSON.stringify({ ok: true, value }));
  })
  .catch(err => {
    process.stdout.write(
      JSON.stringify({ ok: false, error: String(err.message || err) })
    );
  });
"""

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

    def test_accepts_four_space_indentation(self):
        text = """on:
    workflow_dispatch:
        inputs:
            mode:
                description: "Mode"
                required: true
                default: ci-fix
                type: string
            pr_number:
                required: false
                type: string
permissions: {}
"""
        result = self.parse(
            [
                {
                    "name": "four",
                    "text": text,
                    "requiredInputs": ["mode", "pr_number"],
                }
            ]
        )
        self.assertTrue(result["four"]["ok"], result["four"])
        self.assertEqual(["mode", "pr_number"], result["four"]["inputs"])
        self.assertEqual(["mode"], result["four"]["required"])

    def test_accepts_flow_style_inputs_and_nested_keys(self):
        text = (
            "on: {workflow_dispatch: {inputs: {mode: {required: true, "
            "default: 'resolve-conflict'}, pr_number: {description: "
            "'Pull request', required: false, type: string}}}}\n"
            "permissions: {}\n"
        )
        result = self.parse(
            [
                {
                    "name": "flow",
                    "text": text,
                    "requiredInputs": ["mode", "pr_number"],
                }
            ]
        )
        self.assertTrue(result["flow"]["ok"], result["flow"])
        self.assertEqual(["mode", "pr_number"], result["flow"]["inputs"])

    def test_rejects_tab_indentation(self):
        text = "on:\n\tworkflow_dispatch:\n"
        result = self.parse([{"name": "tabs", "text": text}])
        self.assertFalse(result["tabs"]["ok"])
        self.assertIn("tab indentation is not YAML", result["tabs"]["reason"])

    def test_required_input_without_default_must_be_supplied(self):
        text = """on:
  workflow_dispatch:
    inputs:
      mode:
        description: "Mode"
        required: true
        type: string
      pr_number:
        required: true
        default: ''
        type: string
"""
        result = self.parse(
            [
                {"name": "missing", "text": text, "requiredInputs": ["pr_number"]},
                {
                    "name": "supplied",
                    "text": text,
                    "requiredInputs": ["mode", "pr_number"],
                },
            ]
        )
        self.assertTrue(result["supplied"]["ok"], result["supplied"])
        self.assertFalse(result["missing"]["ok"])
        self.assertIn(
            "requires input(s) without a default", result["missing"]["reason"]
        )
        self.assertIn("mode", result["missing"]["reason"])

    def test_optional_input_without_default_is_accepted(self):
        text = """on:
  workflow_dispatch:
    inputs:
      pr_number:
        required: false
        type: string
"""
        result = self.parse(
            [{"name": "optional", "text": text, "requiredInputs": []}]
        )
        self.assertTrue(result["optional"]["ok"], result["optional"])

    def test_rejects_a_non_boolean_required_marker(self):
        text = """on:
  workflow_dispatch:
    inputs:
      pr_number:
        required: sometimes
        type: string
"""
        result = self.parse(
            [{"name": "bad", "text": text, "requiredInputs": ["pr_number"]}]
        )
        self.assertFalse(result["bad"]["ok"])
        self.assertIn("non-boolean required value", result["bad"]["reason"])

    def test_embedded_parsers_are_generated_from_one_source(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/embed_workflow_dispatch_contract.py"),
                "--check",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_real_callers_parse_with_their_actual_dispatch_inputs(self):
        cases = []
        for name, path, inputs in (
            (
                "opencode",
                OPENCODE_STUB,
                ["mode", "pr_number", "head_ref", "conflict_strategy"],
            ),
            (
                "pr-agent-repair",
                PR_AGENT_REPAIR_STUB,
                ["pr_number", "mode", "head_sha", "target_child_id"],
            ),
            (
                "coderabbit-retry",
                ROOT / ".github/caller-stubs/continuum-coderabbit-retry.yml",
                ["pr_number"],
            ),
            (
                "coderabbit-unresolved",
                ROOT / ".github/caller-stubs/continuum-coderabbit-unresolved.yml",
                ["pr_number"],
            ),
        ):
            cases.append(
                {
                    "name": name,
                    "text": path.read_text(encoding="utf-8"),
                    "requiredInputs": inputs,
                }
            )
        result = self.parse(cases)
        for name in (case["name"] for case in cases):
            self.assertTrue(result[name]["ok"], result[name])

    def test_conflict_repair_uses_configured_callers_without_undeclared_inputs(self):
        workflow = PR_AGENT_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "OPENCODE_WORKFLOW: ${{ inputs.opencode_workflow || "
            "vars.CONTINUUM_OPENCODE_WORKFLOW || 'continuum-opencode.yml' }}",
            workflow,
        )
        self.assertIn(
            "PR_AGENT_REPAIR_WORKFLOW: ${{ inputs.repair_workflow || "
            "vars.CONTINUUM_PR_AGENT_REPAIR_WORKFLOW || "
            "'continuum-pr-agent-repair.yml' }}",
            workflow,
        )
        self.assertIn("resolveDispatchWorkflow(", workflow)
        self.assertEqual(2, workflow.count("workflow_id: repairWorkflow"))
        self.assertIn("if (repairWorkflow === 'opencode.yml')", workflow)
        self.assertIn(
            "? ['mode', 'pr_number', 'head_ref', 'conflict_strategy', "
            "'timeout_minutes']",
            workflow,
            'the dogfood timeout input must be part of the dispatch contract',
        )
        self.assertIn("dispatchInputs.timeout_minutes = '60'", workflow)
        self.assertIn("inputs: dispatchInputs", workflow)
        dispatch_payload = workflow.split("const dispatchInputs", 1)[1].split(
            "inputs: dispatchInputs", 1
        )[0]
        self.assertNotIn("timeout_minutes:", dispatch_payload.split(
            "if (repairWorkflow === 'opencode.yml')", 1
        )[0])

        for stub_path, expected_inputs in (
            (OPENCODE_STUB, 24),
            (PR_AGENT_REPAIR_STUB, None),
        ):
            stub = stub_path.read_text(encoding="utf-8")
            dispatch = stub.split("workflow_dispatch:", 1)[1].split(
                "\npermissions:", 1
            )[0]
            inputs = re.findall(r"^      ([A-Za-z0-9_]+):\s*$", dispatch, re.M)
            if expected_inputs is not None:
                self.assertEqual(expected_inputs, len(inputs))
            self.assertNotIn("timeout_minutes", inputs)

    def test_all_reconciliation_dispatches_use_one_gate(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("function parseWorkflowDispatchContract("), 1)
        self.assertEqual(text.count("async function reconciliationDispatchSupport("), 1)
        self.assertEqual(text.count("await reconciliationDispatchSupport("), 4)
        self.assertIn("return parseWorkflowDispatchContract(", text)

        pr_agent = PR_AGENT_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            pr_agent.count("function parseWorkflowDispatchContract("), 1
        )
        self.assertEqual(
            pr_agent.count("async function reconciliationDispatchSupport("), 1
        )
        self.assertEqual(
            pr_agent.count("await reconciliationDispatchSupport("), 1
        )
        self.assertIn("return parseWorkflowDispatchContract(", pr_agent)

    def support(self, workflow, payload):
        text = workflow.read_text(encoding="utf-8")
        parser = FUNCTION.findall(text)
        support = SUPPORT.findall(text)
        self.assertEqual(len(parser), 1, "expected one parse function")
        self.assertEqual(len(support), 1, "expected one support function")
        source = self.workdir / "support.js"
        source.write_text(
            FUNCTION.search(text).group(0) + "\n" + SUPPORT.search(text).group(0),
            encoding="utf-8",
        )
        harness = self.workdir / "support-harness.js"
        harness.write_text(SUPPORT_HARNESS, encoding="utf-8")
        completed = subprocess.run(
            ["node", str(harness), str(source), json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_dispatch_support_skips_a_missing_caller(self):
        result = self.support(
            PR_AGENT_WORKFLOW,
            {
                "workflow": "continuum-opencode.yml",
                "required": ["mode"],
                "error": {"status": 404, "message": "Not Found"},
            },
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["value"]["ok"])
        self.assertIn("is not present on the dispatch ref", result["value"]["reason"])

    def test_dispatch_support_skips_an_unreadable_entry(self):
        result = self.support(
            PR_AGENT_WORKFLOW,
            {
                "workflow": "continuum-opencode.yml",
                "required": ["mode"],
                "data": {"type": "dir", "content": ""},
            },
        )
        self.assertFalse(result["value"]["ok"])
        self.assertIn("is not a readable workflow file", result["value"]["reason"])

    def test_dispatch_support_rethrows_non_404_read_errors(self):
        result = self.support(
            PR_AGENT_WORKFLOW,
            {
                "workflow": "continuum-opencode.yml",
                "required": ["mode"],
                "error": {"status": 500, "message": "server error"},
            },
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("server error", result["error"])

    def test_dispatch_support_requires_declared_inputs(self):
        import base64

        body = (
            "on:\n  workflow_dispatch:\n    inputs:\n"
            "      mode:\n        type: string\npermissions: {}\n"
        )
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        missing = self.support(
            PR_AGENT_WORKFLOW,
            {
                "workflow": "continuum-opencode.yml",
                "required": ["mode", "pr_number"],
                "data": {"type": "file", "content": encoded},
            },
        )
        self.assertFalse(missing["value"]["ok"])
        self.assertIn("pr_number", missing["value"]["reason"])
        present = self.support(
            PR_AGENT_WORKFLOW,
            {
                "workflow": "continuum-opencode.yml",
                "required": ["mode"],
                "data": {"type": "file", "content": encoded},
            },
        )
        self.assertTrue(present["value"]["ok"], present)


if __name__ == "__main__":
    unittest.main()
