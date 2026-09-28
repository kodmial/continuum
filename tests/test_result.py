"""Tests for coverage accounting and the normalized verdict mapping."""

from __future__ import annotations

import unittest

from continuum.review import result as result_module
from continuum.review.coverage import evaluate_coverage
from tests.support import HEAD_A

ONE_SMALL_FILE = [{"filename": "a.py", "additions": 1, "deletions": 0}]
MANY_FILES = [
    {"filename": f"src/module_{index}.py", "additions": 5, "deletions": 1} for index in range(7)
]
BIG_FILE = [{"filename": "generated/schema.py", "additions": 9000, "deletions": 2000}]


class CoverageTests(unittest.TestCase):
    def test_trivial_diff_with_output_is_complete(self):
        self.assertIsNone(evaluate_coverage(["## Review completed"], ONE_SMALL_FILE, current_head=HEAD_A))

    def test_missing_current_head_output_is_a_gap(self):
        reason = evaluate_coverage([], ONE_SMALL_FILE, current_head=HEAD_A, provider_output=False)
        self.assertIsNotNone(reason)
        self.assertIn("no review output", reason)

    def test_historical_output_alone_is_not_coverage(self):
        reason = evaluate_coverage(
            ["review completed, all files reviewed"], ONE_SMALL_FILE, current_head=HEAD_A, provider_output=False
        )
        self.assertIsNotNone(reason)

    def test_explicit_gap_phrases_are_detected(self):
        for phrase in ("Some remaining files were not reviewed", "output was truncated", "chunk limit reached"):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(evaluate_coverage([phrase], ONE_SMALL_FILE, current_head=HEAD_A))

    def test_generic_completion_phrase_is_not_coverage_evidence(self):
        files = [{"filename": "src/a.py", "additions": 4, "deletions": 1}, {"filename": "src/b.py", "additions": 2, "deletions": 0}]
        reason = evaluate_coverage(["I reviewed every single file, looks good"], files, current_head=HEAD_A)
        self.assertIsNotNone(reason)
        self.assertIn("cannot prove complete coverage", reason)

    def test_large_file_count_is_a_gap(self):
        self.assertIsNotNone(evaluate_coverage(["ok"], MANY_FILES, current_head=HEAD_A))

    def test_oversized_patch_is_a_gap(self):
        self.assertIsNotNone(evaluate_coverage(["ok"], BIG_FILE, current_head=HEAD_A))

    def test_medium_diff_is_a_gap_even_without_gap_phrases(self):
        files = [
            {"filename": "src/app.py", "additions": 30, "deletions": 5},
            {"filename": "src/worker.py", "additions": 12, "deletions": 0},
        ]
        self.assertIsNotNone(evaluate_coverage(["looks fine"], files, current_head=HEAD_A))

    def test_trivial_single_line_diff_needs_no_extra_evidence(self):
        self.assertIsNone(evaluate_coverage(["no notes"], ONE_SMALL_FILE, current_head=HEAD_A))

    def test_no_files_means_no_coverage_claim(self):
        self.assertIsNone(evaluate_coverage(["ok"], [], current_head=HEAD_A))

    def test_phrase_match_is_case_insensitive(self):
        self.assertIsNotNone(evaluate_coverage(["UNREVIEWED by the packer"], ONE_SMALL_FILE, current_head=HEAD_A))


class DecisionTests(unittest.TestCase):
    def test_open_findings_request_changes(self):
        decision = result_module.decide(False, 2, True)
        self.assertEqual(decision.verdict, result_module.VERDICT_REQUEST_CHANGES)
        self.assertEqual(decision.state, result_module.STATE_CHANGES_REQUESTED)
        self.assertFalse(decision.gate_passed)
        self.assertTrue(decision.blocking)

    def test_clean_and_covered_approves(self):
        decision = result_module.decide(False, 0, True)
        self.assertEqual(decision.verdict, result_module.VERDICT_APPROVE)
        self.assertEqual(decision.state, result_module.STATE_APPROVED)
        self.assertTrue(decision.gate_passed)
        self.assertFalse(decision.blocking)

    def test_clean_but_uncovered_is_blocked(self):
        decision = result_module.decide(False, 0, False, "chunk budget exhausted")
        self.assertEqual(decision.verdict, result_module.VERDICT_COMMENT)
        self.assertEqual(decision.state, result_module.STATE_BLOCKED)
        self.assertFalse(decision.gate_passed)
        self.assertTrue(decision.blocking)
        self.assertIn("chunk budget exhausted", decision.reason)

    def test_disabled_provider_is_a_deliberate_no_op(self):
        decision = result_module.decide(True, 0, True)
        self.assertEqual(decision.verdict, result_module.VERDICT_NONE)
        self.assertEqual(decision.state, result_module.STATE_SKIPPED)
        self.assertTrue(decision.gate_passed)
        self.assertFalse(decision.blocking)


class EventMappingTests(unittest.TestCase):
    def test_verdicts_map_to_github_review_events(self):
        from continuum.review.gate import EVENT_FOR_VERDICT

        self.assertEqual(EVENT_FOR_VERDICT["APPROVE"], "APPROVE")
        self.assertEqual(EVENT_FOR_VERDICT["REQUEST_CHANGES"], "REQUEST_CHANGES")
        self.assertEqual(EVENT_FOR_VERDICT["COMMENT"], "COMMENT")
        self.assertIsNone(EVENT_FOR_VERDICT["NONE"])


class StatusContractTests(unittest.TestCase):
    def _result(self, **overrides):
        base = {
            "schema": result_module.RESULT_SCHEMA,
            "provider": "pr-agent",
            "repository": "o/r",
            "pr": 1,
            "head": HEAD_A,
            "state": result_module.STATE_APPROVED,
            "verdict": result_module.VERDICT_APPROVE,
            "gate_passed": True,
            "blocking": False,
            "reason": None,
            "open_findings": 0,
            "findings": [],
            "coverage": {"complete": True, "reason": None},
            "linked_issues": [],
            "summary": "",
            "generated_at": "2026-09-01T00:00:00Z",
        }
        base.update(overrides)
        return base

    def test_status_description_round_trips(self):
        description = result_module.status_description(self._result())
        parsed = result_module.parse_status_description(description)
        self.assertEqual(parsed["verdict"], "APPROVE")
        self.assertEqual(parsed["state"], "approved")
        self.assertEqual(parsed["provider"], "pr-agent")
        self.assertEqual(parsed["head"], HEAD_A)

    def test_blocked_status_round_trips(self):
        result = self._result(verdict="REQUEST_CHANGES", state="changes_requested", gate_passed=False, blocking=True)
        parsed = result_module.parse_status_description(result_module.status_description(result))
        self.assertEqual(parsed["verdict"], "REQUEST_CHANGES")
        self.assertEqual(parsed["state"], "changes_requested")

    def test_unrelated_status_description_is_not_parsed(self):
        self.assertEqual(result_module.parse_status_description("CodeRabbit: review completed"), {})

    def test_description_never_carries_finding_text(self):
        result = self._result(
            verdict="REQUEST_CHANGES",
            findings=[
                {
                    "id": "CRV-1A2B3C4D",
                    "file": "src/a.py",
                    "line": 3,
                    "title": "$(rm -rf /) secret finding detail",
                    "status": "open",
                }
            ],
        )
        description = result_module.status_description(result)
        self.assertNotIn("rm -rf", description)
        self.assertNotIn("secret", description)

    def test_missing_head_does_not_produce_a_broken_description(self):
        description = result_module.status_description(self._result(head=""))
        self.assertIn("head=0000", description)


if __name__ == "__main__":
    unittest.main()
