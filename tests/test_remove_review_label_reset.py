"""The per-head review reset must clear the configured CI repair lock.

``continuum-remove-review-label.yml`` is the only per-head reset for task-marker
pull requests that do not use an ``opencode/*`` head. The repair engine resolves
its lock label from ``vars.CONTINUUM_CI_REPAIR_LABEL``, so the reset must remove
that label as well as the literal default; otherwise a stale lock from the
previous head suppresses the next repair attempt.

The executable test runs the checked-in github-script body under Node with a
fake ``github`` client and asserts the exact label removals.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-remove-review-label.yml"
SCRIPT_MARKER = "          script: |\n"
DEFAULT_LABEL = "opencode-ci-repair"
BASE_LABELS = ["review-ready", "coderabbit-review-requested", DEFAULT_LABEL]

HARNESS = """\
const calls = [];
const present = new Set();
global.context = { issue: { owner: 'owner', repo: 'repo', number: 7 } };
global.github = {
  rest: {
    issues: {
      removeLabel: async ({ name }) => {
        calls.push(name);
        if (!present.has(name)) {
          const error = new Error('not found');
          error.status = 404;
          throw error;
        }
      },
    },
  },
};
if (process.env.TEST_CI_REPAIR_LABEL) {
  process.env.CI_REPAIR_LABEL = process.env.TEST_CI_REPAIR_LABEL;
} else {
  delete process.env.CI_REPAIR_LABEL;
}
// Keep the workflow's progress logs off the JSON channel.
console.log = (...args) => process.stderr.write(args.join(' ') + '\\n');
(async () => {
%s
  process.stdout.write(JSON.stringify(calls));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""


def script_body() -> str:
    body = WORKFLOW.read_text(encoding="utf-8")
    start = body.index(SCRIPT_MARKER) + len(SCRIPT_MARKER)
    lines: list[str] = []
    for line in body[start:].splitlines():
        if line.strip() and not line.startswith("            "):
            break
        lines.append(line[12:] if line.strip() else "")
    return textwrap.dedent("\n".join(lines))


class RemoveReviewLabelResetTests(unittest.TestCase):
    def test_reset_reads_the_configured_repair_label(self):
        body = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "CI_REPAIR_LABEL: ${{ vars.CONTINUUM_CI_REPAIR_LABEL || "
            f"'{DEFAULT_LABEL}' }}",
            body,
        )
        self.assertIn("new Set([", body)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_default_and_configured_labels_are_removed_without_duplicates(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        cases = (
            (None, BASE_LABELS),
            ("custom-repair-lock", BASE_LABELS + ["custom-repair-lock"]),
            ("review-ready", BASE_LABELS),
        )
        for configured, expected in cases:
            with self.subTest(configured=configured):
                environment = {
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                    "TEST_CI_REPAIR_LABEL": configured or "",
                }
                completed = subprocess.run(
                    [node, "-e", program],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(expected, json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
