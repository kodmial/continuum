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

A CI report can acquire a repair lock for the new head before this reset job
runs. The reset uses the issue timeline to distinguish a lock attached after
the current head's commit from a previous-head lock, so it keeps current-head
state and cannot permit a duplicate repair dispatch for the same head. If that
head comparison cannot be made, repair locks are kept and the job fails
visibly instead of risking the duplicate.

Label removal is not atomic with that snapshot. The issue timeline is the
durable, head-specific record of every acquisition, and each removal this job
performs adds its own ``unlabeled`` event, so the reset reads the timeline
again after the removals. A lock for the current head that was acquired inside
the race window is re-attached, and a read that does not reflect this job's own
removals is treated as stale: the removed locks are restored and the job fails
visibly for a rerun.

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
const attached = JSON.parse(process.env.TEST_ATTACHED_LABELS || '[]');
const present = new Set(attached.map((label) => label.name));
const events = JSON.parse(process.env.TEST_LABEL_EVENTS || '[]');
const eventsAfterRemoval = JSON.parse(
  process.env.TEST_LABEL_EVENTS_AFTER_REMOVAL || 'null'
);
const listFailures = Number(process.env.TEST_LIST_LABEL_FAILURES || '0');
const timelineFailures = Number(process.env.TEST_TIMELINE_FAILURES || '0');
const verifyFailures = Number(process.env.TEST_TIMELINE_VERIFY_FAILURES || '0');
let timelineCalls = 0;
global.context = {
  issue: { owner: 'owner', repo: 'repo', number: 7 },
  payload: JSON.parse(process.env.TEST_CONTEXT_PAYLOAD || '{}'),
};
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
      listEventsForTimeline: async () => {
        timelineCalls += 1;
        if (timelineCalls <= timelineFailures ||
            (verifyFailures > 0 && timelineCalls > 1 &&
             timelineCalls <= 1 + verifyFailures)) {
          const error = new Error('timeline unavailable');
          error.status = 502;
          throw error;
        }
        if (timelineCalls === 1 || eventsAfterRemoval === null) {
          return events;
        }
        return eventsAfterRemoval;
      },
      removeLabel: async ({ name }) => {
        calls.push(name);
        if (!present.has(name)) {
          const error = new Error('not found');
          error.status = 404;
          throw error;
        }
        present.delete(name);
      },
      addLabels: async ({ labels }) => {
        for (const name of labels) {
          calls.push(`restored:${name}`);
          present.add(name);
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

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_current_head_lock_acquired_before_reset_is_preserved(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "a" * 40
        attached = [
            {"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION},
            {"name": "dispatch-only-lock", "description": LOCK_DESCRIPTION},
        ]
        events = [
            {"event": "committed", "sha": head_sha},
            {
                "event": "labeled",
                "label": {"name": DEFAULT_LABEL},
                "created_at": "2026-11-11T17:05:00Z",
            },
            {
                "event": "labeled",
                "label": {"name": "dispatch-only-lock"},
                "created_at": "2026-11-11T17:06:00Z",
            },
        ]
        self.assertEqual(
            ["review-ready", "coderabbit-review-requested"],
            self.run_reset(
                node,
                program,
                configured=None,
                attached=attached,
                payload={"pull_request": {"head": {"sha": head_sha}}},
                events=events,
            ),
            "a repair lock acquired for the current head must not be reset",
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_previous_head_lock_is_still_reset(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "b" * 40
        attached = [
            {"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION},
            {"name": "dispatch-only-lock", "description": LOCK_DESCRIPTION},
        ]
        events = [
            {
                "event": "labeled",
                "label": {"name": DEFAULT_LABEL},
                "created_at": "2026-11-11T16:00:00Z",
            },
            {
                "event": "labeled",
                "label": {"name": "dispatch-only-lock"},
                "created_at": "2026-11-11T16:01:00Z",
            },
            {"event": "committed", "sha": head_sha},
        ]
        events_after_removal = events + [
            {"event": "unlabeled", "label": {"name": DEFAULT_LABEL}},
            {"event": "unlabeled", "label": {"name": "dispatch-only-lock"}},
        ]
        self.assertEqual(
            sorted(BASE_LABELS + ["dispatch-only-lock"]),
            sorted(
                self.run_reset(
                    node,
                    program,
                    configured=None,
                    attached=attached,
                    payload={"pull_request": {"head": {"sha": head_sha}}},
                    events=events,
                    events_after_removal=events_after_removal,
                )
            ),
            "a previous-head lock must be removed and must not be restored",
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_lock_acquired_between_snapshot_and_removal_is_restored(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "d" * 40
        attached = [{"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION}]
        # The snapshot sees only the commit: the CI report has not attached
        # the lock yet. The post-removal read shows the acquisition that
        # landed inside the race window together with the reset's own removal
        # event, so the durable record proves the lock belongs to this head.
        events_after_removal = [
            {"event": "committed", "sha": head_sha},
            {"event": "labeled", "label": {"name": DEFAULT_LABEL}},
            {"event": "unlabeled", "label": {"name": DEFAULT_LABEL}},
        ]
        self.assertEqual(
            sorted(
                [
                    "review-ready",
                    "coderabbit-review-requested",
                    DEFAULT_LABEL,
                    f"restored:{DEFAULT_LABEL}",
                ]
            ),
            sorted(
                self.run_reset(
                    node,
                    program,
                    configured=None,
                    attached=attached,
                    payload={"pull_request": {"head": {"sha": head_sha}}},
                    events=[{"event": "committed", "sha": head_sha}],
                    events_after_removal=events_after_removal,
                )
            ),
            "a lock acquired after the snapshot but before the removal must be re-attached",
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_stale_post_removal_timeline_restores_locks_and_fails(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "e" * 40
        attached = [{"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION}]
        # A read that does not reflect this job's own removal cannot prove
        # that no concurrent acquisition preceded it. Fail closed: every
        # removed lock is restored and the job fails visibly.
        completed = self.run_reset_process(
            node,
            program,
            configured=None,
            attached=attached,
            payload={"pull_request": {"head": {"sha": head_sha}}},
            events=[{"event": "committed", "sha": head_sha}],
            events_after_removal=[{"event": "committed", "sha": head_sha}],
        )
        self.assertNotEqual(
            0, completed.returncode, "an unverifiable reset must fail the job"
        )
        outcome = json.loads(completed.stdout)
        self.assertEqual(
            sorted(
                [
                    "review-ready",
                    "coderabbit-review-requested",
                    DEFAULT_LABEL,
                    f"restored:{DEFAULT_LABEL}",
                ]
            ),
            sorted(outcome["calls"]),
        )
        self.assertIn("Rerun", outcome["error"])

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_unreadable_post_removal_timeline_restores_locks_and_fails(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "f" * 40
        attached = [{"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION}]
        completed = self.run_reset_process(
            node,
            program,
            configured=None,
            attached=attached,
            payload={"pull_request": {"head": {"sha": head_sha}}},
            events=[{"event": "committed", "sha": head_sha}],
            verify_failures=1,
        )
        self.assertNotEqual(
            0, completed.returncode, "an unreadable verification must fail the job"
        )
        outcome = json.loads(completed.stdout)
        self.assertEqual(
            sorted(
                [
                    "review-ready",
                    "coderabbit-review-requested",
                    DEFAULT_LABEL,
                    f"restored:{DEFAULT_LABEL}",
                ]
            ),
            sorted(outcome["calls"]),
        )
        self.assertIn("Rerun", outcome["error"])

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_unreadable_timeline_keeps_repair_locks_and_fails_for_rerun(self):
        node = shutil.which("node")
        program = HARNESS % script_body()
        head_sha = "c" * 40
        attached = [
            {"name": DEFAULT_LABEL, "description": LOCK_DESCRIPTION},
            {"name": "dispatch-only-lock", "description": LOCK_DESCRIPTION},
        ]
        completed = self.run_reset_process(
            node,
            program,
            configured=None,
            attached=attached,
            payload={"pull_request": {"head": {"sha": head_sha}}},
            timeline_failures=1,
        )
        self.assertNotEqual(
            0, completed.returncode,
            "an unreadable timeline must fail visibly instead of risking a duplicate",
        )
        outcome = json.loads(completed.stdout)
        self.assertEqual(
            sorted(["review-ready", "coderabbit-review-requested"]),
            sorted(outcome["calls"]),
            "repair locks must be kept when current-head state cannot be identified",
        )
        self.assertIn("Rerun", outcome["error"])

    def run_reset(
        self,
        node,
        program,
        configured,
        attached=(),
        list_failures=0,
        payload=None,
        events=(),
        events_after_removal=None,
        timeline_failures=0,
        verify_failures=0,
    ):
        completed = self.run_reset_process(
            node,
            program,
            configured=configured,
            attached=attached,
            list_failures=list_failures,
            payload=payload,
            events=events,
            events_after_removal=events_after_removal,
            timeline_failures=timeline_failures,
            verify_failures=verify_failures,
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
        payload=None,
        events=(),
        events_after_removal=None,
        timeline_failures=0,
        verify_failures=0,
    ):
        environment = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TEST_CI_REPAIR_LABEL": configured or "",
            "TEST_REPAIR_LOCK_DESCRIPTION": LOCK_DESCRIPTION,
            "TEST_ATTACHED_LABELS": json.dumps(list(attached)),
            "TEST_LIST_LABEL_FAILURES": str(list_failures),
            "TEST_TIMELINE_FAILURES": str(timeline_failures),
            "TEST_TIMELINE_VERIFY_FAILURES": str(verify_failures),
            "TEST_CONTEXT_PAYLOAD": json.dumps(payload or {}),
            "TEST_LABEL_EVENTS": json.dumps(list(events)),
            "TEST_LABEL_EVENTS_AFTER_REMOVAL": json.dumps(events_after_removal),
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
