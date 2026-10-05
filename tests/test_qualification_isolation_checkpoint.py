"""Regression contracts for lossless issue execution and qualification isolation."""

from __future__ import annotations

import unittest
from pathlib import Path

from continuum import qualification as gate

ROOT = Path(__file__).resolve().parents[1]
OPENCODE = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"


def step_block(body: str, name: str) -> str:
    start = body.index(f"- name: {name}")
    next_step = body.find("\n      - name:", start + 1)
    return body[start:] if next_step < 0 else body[start:next_step]


class QualificationIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.opencode = OPENCODE.read_text(encoding="utf-8")
        cls.scheduler = SCHEDULER.read_text(encoding="utf-8")

    def test_qualification_dispatch_comment_cannot_enter_issue_implementation(self) -> None:
        for name in (
            "Resolve authoritative task context",
            "Check issue Definition of Ready",
            "Recover agent-managed issue branch",
            "Implement issue",
        ):
            with self.subTest(step=name):
                block = step_block(self.opencode, name)
                self.assertIn("continuum-qualification-dispatch", block)
                self.assertIn("!contains(github.event.comment.body", block)

    def test_validation_only_marker_blocks_generic_manual_implementation(self) -> None:
        readiness = step_block(self.opencode, "Check issue Definition of Ready")
        self.assertIn("automation-validation-only", readiness)
        self.assertIn("generic implementation is disabled", readiness)

    def test_qualification_tracker_own_blockers_are_honored_before_unpause(self) -> None:
        marker = "const declaredQualificationBlockers = await openDeclaredBlockers(qual);"
        start = self.scheduler.index(marker)
        block = self.scheduler[start : start + 6500]
        self.assertIn("declaredQualificationGate.effective.length > 0", block)
        self.assertIn("nativeQualificationGate.effective.length > 0", block)
        self.assertIn("return 'qualification-blocked'", block)
        self.assertLess(
            block.index("return 'qualification-blocked'"),
            block.index("await removeLabel(qualificationNumber, pausedLabel)"),
        )

    def test_only_capability_back_edge_is_bypassed(self) -> None:
        effective, bypassed, reason = gate.qualification_dispatch_bypass(
            7, 6, [6, 118]
        )
        self.assertEqual(effective, [118])
        self.assertEqual(bypassed, (6,))
        self.assertEqual(reason, "blocked")


class InterruptedIssueCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.body = OPENCODE.read_text(encoding="utf-8")

    def test_interrupted_agent_work_is_checkpointed_and_resumed(self) -> None:
        implement = step_block(self.body, "Implement issue")
        for marker in (
            'CHECKPOINT_BRANCH="opencode/checkpoint-issue${ISSUE_NUMBER}"',
            'git ls-remote --exit-code --heads origin "refs/heads/$CHECKPOINT_BRANCH"',
            "checkpoint_issue_progress()",
            "trap 'checkpoint_issue_progress 143' TERM",
            "checkpoint_issue_progress 75",
            'git push --force origin "HEAD:refs/heads/$CHECKPOINT_BRANCH"',
            'COMMITS_FROM_START="$(git rev-list --count "$BASE_START_SHA..HEAD"',
        ):
            self.assertIn(marker, implement)

    def test_checkpoint_is_deleted_after_pr_publication(self) -> None:
        implement = step_block(self.body, "Implement issue")
        self.assertIn(
            '"repos/$GITHUB_REPOSITORY/git/refs/heads/$CHECKPOINT_BRANCH"',
            implement,
        )
        self.assertIn("Created/updated PR #$PR_NUMBER", implement)


if __name__ == "__main__":
    unittest.main()
