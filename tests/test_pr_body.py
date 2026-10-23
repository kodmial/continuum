"""Deterministic PR body composition for ordinary and Child PR producers."""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")


def _body():
    sys.path.insert(0, SRC)
    try:
        from continuum import pr_body

        return pr_body
    finally:
        sys.path.remove(SRC)


DIFF = (
    "diff --git a/docs/usage.md b/docs/usage.md\n"
    "--- a/docs/usage.md\n"
    "+++ b/docs/usage.md\n"
    "@@ -1 +1 @@\n"
    "-Before: 3 retries\n"
    "+After: 5 retries\n"
)


class BeforeAfterRequirementTests(unittest.TestCase):
    def test_detects_explicit_task_requirement(self):
        body = _body()
        for text in (
            "Include before/after evidence in the PR description.",
            "The PR description must show before and after behavior.",
            "Add a before & after example.",
        ):
            with self.subTest(text=text):
                self.assertTrue(body.task_requires_before_after(text))

    def test_plain_task_does_not_force_examples(self):
        body = _body()
        self.assertFalse(body.task_requires_before_after("Fix the retry loop."))
        composed = body.compose_pr_body(
            issue_number=7,
            task_body="Fix the retry loop.",
            diff_text=DIFF,
        )
        self.assertNotIn("Before / After", composed)
        self.assertNotIn("Before: 3 retries", composed)


class ComposeTests(unittest.TestCase):
    def test_before_after_is_source_grounded(self):
        body = _body()
        composed = body.compose_pr_body(
            task_marker="<!-- continuum-task-context repo=o/r issue=7 -->",
            snapshot_ref="<!-- continuum-task-snapshot-ref repo=o/r issue=7 generation=1 "
            "title-sha256=a body-sha256=b spec-sha256=c -->",
            issue_number=7,
            task_body="The issue requires before/after evidence in the PR description.",
            diff_text=DIFF,
            head="a" * 40,
        )
        self.assertIn("Before: 3 retries", composed)
        self.assertIn("After: 5 retries", composed)
        self.assertIn("continuum-task-context", composed)
        self.assertIn("continuum-task-snapshot-ref", composed)
        self.assertIn("Closes #7", composed)
        self.assertIn("continuum-pr-body-evidence:v1 head=" + "a" * 40, composed)

    def test_qualification_uses_non_closing_semantics(self):
        body = _body()
        composed = body.compose_pr_body(
            issue_number=7,
            task_body="Ship it.",
            diff_text=DIFF,
            qualification=True,
        )
        self.assertNotIn("Closes #7", composed)
        self.assertIn("Relates to #7", composed)
        self.assertIn("must not auto-close", composed)

    def test_child_accepted_marker_is_preserved(self):
        body = _body()
        composed = body.compose_pr_body(
            snapshot_ref="<!-- continuum-task-snapshot-ref x -->",
            issue_number=9,
            task_body="Ship it.",
            diff_text=DIFF,
            child_accepted=True,
        )
        self.assertIn("<!-- continuum-child-accepted -->", composed)
        self.assertIn("Closes #9", composed)

    def test_no_invented_examples_without_pairs(self):
        body = _body()
        composed = body.compose_pr_body(
            issue_number=7,
            task_body="The PR description requires before/after evidence.",
            diff_text="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+only added\n",
        )
        self.assertNotIn("Before:", composed)
        self.assertNotIn("After:", composed)

    def test_replace_preserves_everything_outside_the_block(self):
        body = _body()
        existing = (
            "<!-- continuum-task-context repo=o/r issue=7 -->\n\n"
            "Closes #7\n\n"
            "<!-- continuum-pr-body-evidence:v1 head=" + "a" * 40 + " -->\n"
            "stale\n<!-- /continuum-pr-body-evidence -->\n"
        )
        section = body.build_evidence_section(DIFF, head="b" * 40)
        updated = body.replace_evidence_section(existing, section)
        self.assertIn("Closes #7", updated)
        self.assertIn("head=" + "b" * 40, updated)
        self.assertNotIn("stale", updated)


if __name__ == "__main__":
    unittest.main()
