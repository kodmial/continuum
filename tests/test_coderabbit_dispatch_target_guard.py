"""Executable tests for the CodeRabbit dispatch-target guards.

`continuum-auto-merge.yml` and `continuum-add-review-label.yml` resolve each
sibling dispatch target through an embedded `resolveDispatchWorkflow()`. The
guard is the only thing that stops a configured value from being spliced into
a dispatch path, so exercise the production function rather than a copy.
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

WORKFLOWS = {
    "continuum-auto-merge.yml": ROOT / ".github/workflows/continuum-auto-merge.yml",
    "continuum-add-review-label.yml":
        ROOT / ".github/workflows/continuum-add-review-label.yml",
}

FUNCTION = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)function resolveDispatchWorkflow"
    r"\(configuredValue, fallback, label\) \{\n.*?^(?P=indent)\}\n"
)

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);
const factory = new Function(source + '\nreturn resolveDispatchWorkflow;');
const resolve = factory();
const out = {};
for (const item of payload.cases) {
  try {
    out[item.name] = {
      ok: true,
      value: resolve(item.configured, item.fallback, item.label),
    };
  } catch (err) {
    out[item.name] = { ok: false, error: String(err.message || err) };
  }
}
process.stdout.write(JSON.stringify(out));
"""


def extract_resolver(workflow):
    text = workflow.read_text(encoding="utf-8")
    matches = FUNCTION.findall(text)
    assert len(matches) == 1, f"{workflow.name}: expected one resolveDispatchWorkflow"
    return FUNCTION.search(text).group(0)


class DispatchTargetGuardTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="dispatch-guard-", dir=str(TMP_PARENT))
        )

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def resolve(self, workflow, cases):
        source = self.workdir / "resolver.js"
        source.write_text(extract_resolver(workflow), encoding="utf-8")
        harness = self.workdir / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        completed = subprocess.run(
            [
                "node",
                str(harness),
                str(source),
                json.dumps({"cases": cases}),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def assert_rejected(self, result, name, label):
        self.assertFalse(result[name]["ok"], f"{name}: guard accepted the value")
        self.assertIn(f"Invalid {label} workflow", result[name]["error"])

    def test_rejects_paths_and_non_workflow_names(self):
        cases = [
            {
                "name": "slash",
                "configured": "nested/coderabbit-retry.yml",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
            {
                "name": "backslash",
                "configured": "nested\\coderabbit-retry.yml",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
            {
                "name": "parent-traversal",
                "configured": "coderabbit..retry.yml",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
            {
                "name": "non-yml",
                "configured": "coderabbit-retry.yaml",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
        ]
        result = self.resolve(
            WORKFLOWS["continuum-auto-merge.yml"], cases
        )
        for case in cases:
            self.assert_rejected(result, case["name"], case["label"])

    def test_add_review_label_rejects_the_same_values(self):
        # Both dispatch targets in this engine share one guard; assert through
        # the auto-merge label so a per-call change is caught too.
        cases = [
            {
                "name": "slash",
                "configured": "nested/auto-merge.yml",
                "fallback": "continuum-auto-merge.yml",
                "label": "Auto-merge",
            },
            {
                "name": "backslash",
                "configured": "nested\\auto-merge.yml",
                "fallback": "continuum-auto-merge.yml",
                "label": "Auto-merge",
            },
            {
                "name": "parent-traversal",
                "configured": "auto..merge.yml",
                "fallback": "continuum-auto-merge.yml",
                "label": "Auto-merge",
            },
            {
                "name": "non-yml",
                "configured": "auto-merge.txt",
                "fallback": "continuum-auto-merge.yml",
                "label": "Auto-merge",
            },
            {
                "name": "retry-non-yml",
                "configured": "coderabbit-retry.yml.bak",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
        ]
        result = self.resolve(
            WORKFLOWS["continuum-add-review-label.yml"], cases
        )
        for case in cases:
            self.assert_rejected(result, case["name"], case["label"])

    def test_empty_configuration_keeps_the_legacy_default(self):
        cases = [
            {
                "name": "retry",
                "configured": "",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
            {
                "name": "unresolved",
                "configured": "",
                "fallback": "continuum-coderabbit-unresolved.yml",
                "label": "CodeRabbit unresolved",
            },
        ]
        result = self.resolve(WORKFLOWS["continuum-auto-merge.yml"], cases)
        self.assertTrue(result["retry"]["ok"], result["retry"])
        self.assertEqual(
            "continuum-coderabbit-retry.yml", result["retry"]["value"]
        )
        self.assertTrue(result["unresolved"]["ok"], result["unresolved"])
        self.assertEqual(
            "continuum-coderabbit-unresolved.yml", result["unresolved"]["value"]
        )

    def test_add_review_label_empty_configuration_keeps_the_legacy_default(self):
        cases = [
            {
                "name": "retry",
                "configured": "",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            },
            {
                "name": "auto-merge",
                "configured": "",
                "fallback": "continuum-auto-merge.yml",
                "label": "Auto-merge",
            },
        ]
        result = self.resolve(
            WORKFLOWS["continuum-add-review-label.yml"], cases
        )
        self.assertTrue(result["retry"]["ok"], result["retry"])
        self.assertEqual(
            "continuum-coderabbit-retry.yml", result["retry"]["value"]
        )
        self.assertTrue(result["auto-merge"]["ok"], result["auto-merge"])
        self.assertEqual(
            "continuum-auto-merge.yml", result["auto-merge"]["value"]
        )

    def test_a_valid_configured_name_is_used(self):
        cases = [
            {
                "name": "retry",
                "configured": "opencode.yml",
                "fallback": "continuum-coderabbit-retry.yml",
                "label": "CodeRabbit retry",
            }
        ]
        result = self.resolve(WORKFLOWS["continuum-auto-merge.yml"], cases)
        self.assertTrue(result["retry"]["ok"], result["retry"])
        self.assertEqual("opencode.yml", result["retry"]["value"])


if __name__ == "__main__":
    unittest.main()
