"""Tests for exact single-finding re-verification against the current HEAD."""

from __future__ import annotations

import unittest

from continuum.review.findings import sanitize_title, stable_id
from continuum.review.verify import (
    RESOLVED,
    UNRESOLVED,
    VerifyError,
    build_context,
    build_prompt,
    classify,
    find_finding,
    verify_finding,
)
from tests.support import HEAD_A, FakeGitHub, issue_comment, clean_pr_agent_report, review_comment, snapshot

FINDING_TITLE = "Unhandled exception in the worker loop"
FINDING_PATH = "src/worker.py"
FINDING_LINE = 42
FINDING_ID = stable_id(FINDING_PATH, FINDING_LINE, sanitize_title(FINDING_TITLE))


def finding(**overrides):
    base = {
        "id": FINDING_ID,
        "file": FINDING_PATH,
        "line": FINDING_LINE,
        "title": FINDING_TITLE,
        "status": "open",
    }
    base.update(overrides)
    return base


def inline_snapshot():
    return snapshot(
        summary_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
        review_comments=[
            review_comment(FINDING_TITLE, path=FINDING_PATH, line=FINDING_LINE, comment_id=1)
        ],
        unresolved_ids={1},
    )


def worker_file(lines=100):
    return "\n".join(f"line {index}" for index in range(lines))


class DummyLLM:
    def __init__(self, answer: str):
        self.answer = answer
        self.prompt = None
        self.calls = 0

    def chat(self, prompt: str) -> str:
        self.calls += 1
        self.prompt = prompt
        return self.answer


class PromptTests(unittest.TestCase):
    def test_prompt_is_instruction_shaped(self):
        prompt = build_prompt(finding(), HEAD_A, "context body", partial=False)
        self.assertIn(FINDING_ID, prompt)
        self.assertIn(HEAD_A, prompt)
        self.assertIn("RESOLVED", prompt)
        self.assertIn("UNRESOLVED", prompt)

    def test_partial_diff_requests_unresolved_when_code_is_missing(self):
        prompt = build_prompt(finding(), HEAD_A, "truncated", partial=True)
        self.assertIn("If the code relevant to this finding is not present above, reply UNRESOLVED", prompt)

    def test_hostile_title_is_sanitized(self):
        prompt = build_prompt(
            finding(title="<script>alert(1)</script> \\u0060pipe|trick\\u0060"), HEAD_A, "ctx", False
        )
        self.assertNotIn("<script>", prompt)
        self.assertNotIn("`", prompt)

    def test_context_is_budget_bounded(self):
        prompt = build_prompt(finding(), HEAD_A, "x" * 40000, False)
        self.assertLessEqual(len(prompt), 9000)


class ClassifyTests(unittest.TestCase):
    def test_resolved_first_line(self):
        verdict, text = classify("RESOLVED because the guard was added")
        self.assertEqual(verdict, RESOLVED)
        self.assertTrue(text.startswith("RESOLVED"))

    def test_unresolved_first_line(self):
        verdict, _ = classify("UNRESOLVED the branch is still missing")
        self.assertEqual(verdict, UNRESOLVED)

    def test_case_insensitive_tokens(self):
        self.assertEqual(classify("  resolved now\n")[0], RESOLVED)
        self.assertEqual(classify("\nunresolved\n")[0], UNRESOLVED)

    def test_non_conforming_answer_fails_closed(self):
        verdict, text = classify("I think it is probably fine now")
        self.assertEqual(verdict, UNRESOLVED)
        self.assertIn("non-conforming", text)

    def test_empty_answer_fails_closed(self):
        self.assertEqual(classify("")[0], UNRESOLVED)
        self.assertEqual(classify("   \n")[0], UNRESOLVED)


class ContextTests(unittest.TestCase):
    def test_file_context_is_windowed_around_the_line(self):
        client = FakeGitHub(file_contents={FINDING_PATH: worker_file()})
        context, partial = build_context(client, 7, HEAD_A, finding(line=50))
        self.assertFalse(partial)
        self.assertNotIn("line 0", context)
        self.assertIn("21:line 20", context)
        self.assertIn("80:line 79", context)

    def test_missing_file_yields_empty_context(self):
        client = FakeGitHub()
        context, partial = build_context(client, 7, HEAD_A, finding())
        self.assertEqual(context, "")
        self.assertFalse(partial)

    def test_summary_finding_uses_the_diff(self):
        client = FakeGitHub(
            files=[
                {"filename": FINDING_PATH, "additions": 3, "deletions": 0, "patch": "@@ -10,3 +10,6 @@\n-old\n+new"},
                {"filename": "src/unrelated.py", "additions": 1, "deletions": 0, "patch": "@@ -1 +1 @@ changed"},
            ]
        )
        context, partial = build_context(client, 7, HEAD_A, finding(file="", line=0))
        self.assertIn("src/worker.py", context)
        self.assertFalse(partial)

    def test_diff_projection_is_budget_bounded(self):
        huge = [{"filename": f"src/f{index}.py", "patch": "x" * 2000} for index in range(20)]
        client = FakeGitHub(files=huge)
        context, partial = build_context(client, 7, HEAD_A, finding(file="", line=0))
        self.assertLessEqual(len(context), 6000)
        self.assertTrue(partial)


class FindFindingTests(unittest.TestCase):
    def test_found_in_tracker_state(self):
        state = {"findings": [finding()]}
        self.assertEqual(find_finding(state, [], FINDING_ID)["id"], FINDING_ID)

    def test_found_in_derived_findings_with_open_status(self):
        derived = [finding(status="open")]
        self.assertEqual(find_finding({}, derived, FINDING_ID)["status"], "open")

    def test_case_insensitive_match(self):
        self.assertEqual(find_finding({"findings": [finding()]}, [], FINDING_ID.lower())["id"], FINDING_ID)

    def test_unknown_finding(self):
        self.assertIsNone(find_finding({}, [], "CRV-00000000"))


class VerifyFindingTests(unittest.TestCase):
    def _client(self, **overrides):
        defaults = {"issue_comments": [issue_comment(clean_pr_agent_report(HEAD_A))]}
        defaults.update(overrides)
        return FakeGitHub(**defaults)

    def test_resolved_settles_the_finding(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        llm = DummyLLM("RESOLVED guard clause now present\nthanks")
        result = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=llm,
            apply=True,
        )
        self.assertEqual(result["verdict"], RESOLVED)
        self.assertTrue(result["resolved"])
        self.assertEqual(llm.calls, 1)
        marker_comment = next(c for c in client.comments_created if "verify CRV-" in c["body"])
        self.assertIn("RESOLVED", marker_comment["body"])
        self.assertEqual(client.tracker_state()["findings"][0]["status"], "resolved")

    def test_unresolved_keeps_the_finding_open(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        llm = DummyLLM("UNRESOLVED the retry branch is still missing")
        result = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=llm,
            apply=True,
        )
        self.assertEqual(result["verdict"], UNRESOLVED)
        tracker = client.tracker_state()["findings"][0]
        self.assertEqual(tracker["status"], "still-open")

    def test_non_conforming_answer_stays_unresolved(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        llm = DummyLLM("probably looks fixed but not sure")
        result = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=llm,
            apply=False,
        )
        self.assertEqual(result["verdict"], UNRESOLVED)
        self.assertFalse(result["resolved"])
        self.assertIn("non-conforming", result["answer"])

    def test_missing_code_context_fails_closed_without_a_model_call(self):
        client = self._client(file_contents={"src/other.py": "irrelevant"})
        result = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            apply=False,
        )
        self.assertEqual(result["verdict"], UNRESOLVED)
        self.assertIn("no current-HEAD code context", result["answer"])

    def test_context_requires_a_client_otherwise(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        with self.assertRaises(VerifyError):
            verify_finding(client, inline_snapshot(), pr_number=7, head=HEAD_A, finding_id=FINDING_ID, apply=False)

    def test_unknown_finding_is_rejected(self):
        client = self._client()
        with self.assertRaises(VerifyError):
            verify_finding(client, inline_snapshot(), pr_number=7, head=HEAD_A, finding_id="CRV-00000000", apply=False)

    def test_apply_false_writes_nothing(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        result = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=DummyLLM("RESOLVED fixed"),
            apply=False,
        )
        self.assertEqual(client.comments_created, [])
        self.assertEqual(client.tracker_comments(), [])
        self.assertEqual(result["verdict"], RESOLVED)

    def test_no_duplicate_verdict_findings_across_runs(self):
        client = self._client(file_contents={FINDING_PATH: worker_file()})
        first = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=DummyLLM("RESOLVED fixed"),
            apply=True,
        )
        self.assertEqual(first["verdict"], RESOLVED)
        second = verify_finding(
            client,
            inline_snapshot(),
            pr_number=7,
            head=HEAD_A,
            finding_id=FINDING_ID,
            llm=DummyLLM("UNRESOLVED still happening"),
            apply=True,
        )
        self.assertEqual(second["verdict"], UNRESOLVED)
        tracker = client.tracker_state()["findings"][0]
        self.assertEqual(tracker["status"], "still-open")


if __name__ == "__main__":
    unittest.main()