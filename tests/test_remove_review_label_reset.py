"""The per-head review reset must clear every CI repair lock.

``continuum-remove-review-label.yml`` is the only per-head reset for task-marker
pull requests that do not use an ``opencode/*`` head. The repair engine resolves
its lock label from ``inputs.ci_repair_label``, ``vars.CONTINUUM_CI_REPAIR_LABEL``,
or the literal default, so the reset must remove the configured label as well as
the literal default; otherwise a stale lock from the previous head suppresses
the next repair attempt.

A label chosen only through the repair caller's ``ci_repair_label`` dispatch
input is not visible to this synchronize event. The engine therefore stamps a
fixed description on every repair lock it creates, and the reset discovers and
removes those labels by that marker. Label discovery is retried; if it still
fails, the job fails after removing the known labels so the incomplete reset is
visible for a rerun.

The executable tests run the checked-in github-script body under Node with a
fake ``github`` client and assert the exact label removals and retry behavior.
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
REPAIR_WORKFLOW = (
    ROOT / ".github" / "workflows" / "continuum-opencode-repair.yml"
)
SCRIPT_MARKER = "          script: |\n"
DEFAULT_LABEL = "opencode-ci-repair"
LOCK_DESCRIPTION = (
    "One automatic OpenCode CI repair attempt per failing PR head."
)
BASE_LABELS = ["review-ready", "coderabbit-review-requested", DEFAULT_LABEL]

HARNESS = """\
const calls = [];
const listCalls = [];
const present = new Set();
const attached = JSON.parse(process.env.TEST_ATTACHED_LABELS || '[]');
const listFailures = Number(process.env.TEST_LIST_LABEL_FAILURES || '0');
global.context = { issue: { owner: 'owner', repo: 'repo', number: 7 } };
global.github = {
  paginate: async (method, params) => method(params),
  rest: {
    issues: {
      listLabelsOnIssue: async () => {
        listCalls.push(listCalls.length + 1);
        if (listCalls.length <= listFailures) {
          const error = new Error('label API unavailable');
          error.status = 502;
          throw error;
        }
        return attached;
      },
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
process.env.CI_REPAIR_LOCK_DESCRIPTION =
  process.env.TEST_REPAIR_LOCK_DESCRIPTION || '';
// Keep the workflow's progress logs off the JSON channel.
console.log = (...args) => process.stderr.write(args.join(' ') + '\\n');
(async () => {
%s
  process.stdout.write(JSON.stringify(calls));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.stdout.write(
    JSON.stringify({
      calls,
      listCalls,
      error: String((error && error.message) || error),
    })
  );
  process.exitCode = 1;
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
        self.assertIn(f"CI_REPAIR_LOCK_DESCRIPTION: '{LOCK_DESCRIPTION}'", body)
        self.assertIn("new Set([", body)

    def test_repair_engine_stamps_the_same_lock_description(self):
        body = REPAIR_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(f'--description "{LOCK_DESCRIPTION}"', body)
        self.assertIn(f'lock_description="{LOCK_DESCRIPTION}"', body)

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
                self.assertEqual(
                    expected,
                    self.run_reset(node, program, configured=configured),
                )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_dispatch_only_override_is_found_by_its_repair_lock_description(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        attached = [
            {"name": "dispatch-only-lock", "description": LOCK_DESCRIPTION},
            {"name": "unrelated-label", "description": "Not a repair lock."},
            {"name": "unmarked-lock", "description": ""},
        ]
        self.assertEqual(
            BASE_LABELS + ["dispatch-only-lock"],
            self.run_reset(
                node, program, configured=None, attached=attached
            ),
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_transient_label_discovery_failure_is_retried(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        attached = [
            {"name": "dispatch-only-lock", "description": LOCK_DESCRIPTION},
        ]
        self.assertEqual(
            BASE_LABELS + ["dispatch-only-lock"],
            self.run_reset(
                node,
                program,
                configured=None,
                attached=attached,
                list_failures=2,
            ),
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_exhausted_label_discovery_failure_is_visible_for_rerun(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        completed = self.run_reset_process(
            node, program, configured=None, list_failures=3
        )
        self.assertNotEqual(
            0, completed.returncode, "an incomplete reset must fail the job"
        )
        outcome = json.loads(completed.stdout)
        self.assertEqual(3, len(outcome["listCalls"]))
        self.assertEqual(sorted(BASE_LABELS), sorted(outcome["calls"]))
        self.assertIn("Rerun", outcome["error"])

    def run_reset(
        self,
        node,
        program,
        configured,
        attached=(),
        list_failures=0,
    ):
        completed = self.run_reset_process(
            node,
            program,
            configured=configured,
            attached=attached,
            list_failures=list_failures,
        )
        completed.check_returncode()
        return json.loads(completed.stdout)

    def run_reset_process(
        self,
        node,
        program,
        configured,
        attached=(),
        list_failures=0,
    ):
        environment = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TEST_CI_REPAIR_LABEL": configured or "",
            "TEST_REPAIR_LOCK_DESCRIPTION": LOCK_DESCRIPTION,
            "TEST_ATTACHED_LABELS": json.dumps(list(attached)),
            "TEST_LIST_LABEL_FAILURES": str(list_failures),
        }
        return subprocess.run(
            [node, "-e", program],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()
