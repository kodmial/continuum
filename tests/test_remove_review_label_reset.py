"""The synchronize reset is review/CodeRabbit housekeeping only (#446).

After kodmial/continuum#446, a fresh PR head no longer resets any CI-repair
admission state. CI repair is owned by a durable exact-HEAD receipt in the
serialized repair controller; a mutable label (or its description/timeline) is
presentation only. These tests execute the checked-in script body and assert
that it removes exactly the review-readiness labels and never reads or writes
CI-repair labels.
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
REVIEW_LABELS = ["review-ready", "coderabbit-review-requested"]

HARNESS = """\
const calls = [];
const present = new Set(JSON.parse(process.env.TEST_ATTACHED_LABELS || '[]'));
const failure = process.env.TEST_REMOVE_FAILURE || '';
global.context = {
  issue: { owner: 'owner', repo: 'repo', number: 7 },
  payload: {},
};
global.github = {
  paginate: async (method, params) => method(params),
  rest: {
    issues: {
      removeLabel: async ({ name }) => {
        calls.push(name);
        if (name === failure) {
          const error = new Error('label API unavailable');
          error.status = 500;
          throw error;
        }
        if (!present.has(name)) {
          const error = new Error('not found');
          error.status = 404;
          throw error;
        }
        present.delete(name);
      },
      listLabelsOnIssue: async () => {
        calls.push('LIST_LABELS');
        return [];
      },
      listEventsForTimeline: async () => {
        calls.push('LIST_TIMELINE');
        return [];
      },
      addLabels: async () => {
        calls.push('ADD_LABELS');
      },
    },
  },
};
console.log = (...args) => process.stderr.write(args.join(' ') + '\\n');
(async () => {
%s
  process.stdout.write(JSON.stringify(calls));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.stdout.write(
    JSON.stringify({ calls, error: String((error && error.message) || error) })
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
    @classmethod
    def setUpClass(cls):
        cls.body = WORKFLOW.read_text(encoding="utf-8")
        cls.script = script_body()

    def test_the_workflow_no_longer_touches_ci_repair_state(self):
        for forbidden in (
            "CI_REPAIR",
            "ci_repair",
            "opencode-ci-repair",
            "opencode-packaging-smoke-repair",
            "CI_REPAIR_LOCK_DESCRIPTION",
            "listLabelsOnIssue",
            "listEventsForTimeline",
            "addLabels",
            "diff --git",
            "timeline",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.body)

    def test_only_review_readiness_labels_are_removed(self):
        self.assertIn("'review-ready'", self.script)
        self.assertIn("'coderabbit-review-requested'", self.script)
        self.assertEqual(1, self.script.count("removeLabel"))

    def test_reset_does_not_need_the_repair_workflow_lock_description(self):
        repair_body = REPAIR_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn(
            "One automatic OpenCode CI repair attempt per failing PR head.",
            repair_body,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_repair_labels_are_never_removed_by_the_reset(self):
        result = self.run_reset(
            ["review-ready", "coderabbit-review-requested", "opencode-ci-repair"]
        )
        self.assertEqual(REVIEW_LABELS, result)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_absent_review_labels_are_tolerated(self):
        self.assertEqual(REVIEW_LABELS, self.run_reset([]))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_a_non_404_removal_failure_is_visible(self):
        completed = self.run_reset_process(
            [], failure="coderabbit-review-requested"
        )
        self.assertNotEqual(
            0, completed.returncode, "a real label API failure must fail the job"
        )
        outcome = json.loads(completed.stdout)
        self.assertEqual(
            ["review-ready", "coderabbit-review-requested"], outcome["calls"]
        )
        self.assertIn("label API unavailable", outcome["error"])

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_the_reset_never_calls_any_state_machine_api(self):
        calls = self.run_reset(["review-ready", "opencode-ci-repair"])
        self.assertNotIn("LIST_LABELS", calls)
        self.assertNotIn("LIST_TIMELINE", calls)
        self.assertNotIn("ADD_LABELS", calls)

    def run_reset(self, attached, *, failure=""):
        completed = self.run_reset_process(attached, failure=failure)
        completed.check_returncode()
        return json.loads(completed.stdout)

    def run_reset_process(self, attached, *, failure=""):
        node = shutil.which("node")
        environment = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TEST_ATTACHED_LABELS": json.dumps(list(attached)),
            "TEST_REMOVE_FAILURE": failure,
        }
        return subprocess.run(
            [node, "-e", HARNESS % self.script],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()
