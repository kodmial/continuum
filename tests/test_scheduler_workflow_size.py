"""Regression tests for the Continuum scheduler's extracted github-script payload.

The inline body previously exceeded Linux MAX_ARG_STRLEN and prevented Node
from starting. Keep the Actions loader small and syntax-check the checked-in
module that now contains the unchanged scheduler implementation.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
SCHEDULER = ROOT / ".github" / "scripts" / "issue_scheduler.js"
AUTOMATION = ROOT / ".github" / "workflows" / "automation.yml"
SCHEDULER_STUB = ROOT / ".github" / "caller-stubs" / "continuum-issue-scheduler.yml"
CHILD_WORKER = ROOT / ".github" / "workflows" / "continuum-consumer-child-worker.yml"
CHILD_REVIEW = ROOT / ".github" / "workflows" / "continuum-consumer-child-review.yml"
CHILD_PR_REVIEW = ROOT / ".github" / "workflows" / "continuum-consumer-child-pr-review.yml"
LOADER_LIMIT_BYTES = 4 * 1024


def reconcile_loader() -> str:
    body = WORKFLOW.read_text(encoding="utf-8")
    step_marker = "      - name: Reconcile state and dispatch ready issues\n"
    step_start = body.index(step_marker)
    next_step = body.find("\n      - name: ", step_start + len(step_marker))
    step = body[step_start:next_step if next_step >= 0 else len(body)]
    script_marker = "          script: |\n"
    script_start = step.index(script_marker) + len(script_marker)

    loader_lines: list[str] = []
    for line in step[script_start:].splitlines():
        if line.startswith("            "):
            loader_lines.append(line[12:])
        elif not line.strip():
            loader_lines.append("")
        else:
            break
    return "\n".join(loader_lines).strip()


class SchedulerWorkflowPayloadTests(unittest.TestCase):
    def test_reconcile_github_script_is_a_small_file_loader(self):
        loader = reconcile_loader()
        self.assertLess(len(loader.encode("utf-8")), LOADER_LIMIT_BYTES)
        self.assertIn("issue_scheduler.js", loader)
        self.assertIn("await runScheduler({ github, context, core });", loader)
        self.assertTrue(SCHEDULER.is_file())

    def test_owner_command_prefilters_accept_tab_and_crlf_delimiters(self):
        for path in (AUTOMATION, SCHEDULER_STUB):
            body = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for token in ("oc", "opencode"):
                    self.assertIn(f'fromJSON(\'"/{token}\\t"\')', body)
                    self.assertIn(f'fromJSON(\'"/{token}\\r\\n"\')', body)

    def test_child_worker_and_review_concurrency_groups_are_disjoint(self):
        worker = CHILD_WORKER.read_text(encoding="utf-8")
        review = CHILD_REVIEW.read_text(encoding="utf-8")
        pr_review = CHILD_PR_REVIEW.read_text(encoding="utf-8")
        self.assertIn("group: continuum-child-worker-", worker)
        self.assertIn("group: continuum-subtask-review-", review)
        self.assertIn("group: continuum-subtask-pr-review-", pr_review)

    def test_extracted_scheduler_is_valid_node_source(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable in this test environment")
        subprocess.run(
            [node, "--check", str(SCHEDULER)],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
