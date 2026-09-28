"""Tests for tracker state transitions, rendering, and spoof resistance."""

from __future__ import annotations

import json
import unittest

from continuum.review.tracker import (
    STATUS_OPEN,
    STATUS_REOPENED,
    STATUS_RESOLVED,
    STATUS_STILL_OPEN,
    TRACKER_MARKER,
    apply_verification,
    empty_state,
    load_tracker,
    merge_findings,
    parse_tracker_state,
    render_tracker,
)
from tests.support import HEAD_A, HEAD_B, issue_comment, json_block

FINDING_ID = "CRV-1A2B3C4D"
OTHER_ID = "CRV-DEADBEEF"


def finding(finding_id=FINDING_ID, *, title="src/a.py: a real finding to report", file="src/a.py", line=4, created="2026-09-01T00:00:00Z"):
    return {
        "id": finding_id,
        "file": file,
        "line": line,
        "title": title,
        "source": "inline",
        "source_created": created,
    }


def state_with(findings, head=HEAD_A, marker="2026-09-01T00:00:00Z"):
    return {"head": head, "findings": list(findings), "last_review_at": marker}


class TransitionTests(unittest.TestCase):
    def test_first_review_opens_finding(self):
        merged, marker = merge_findings(empty_state(), [finding()], HEAD_A, last_review_at="2026-09-01T00:00:00Z")
        self.assertEqual([entry["status"] for entry in merged], [STATUS_OPEN])
        self.assertEqual(marker, "2026-09-01T00:00:00Z")

    def test_repeated_review_keeps_finding_still_open(self):
        previous = state_with([{**finding(), "status": STATUS_OPEN}])
        merged, _ = merge_findings(previous, [finding()], HEAD_A, last_review_at="2026-09-02T00:00:00Z")
        self.assertEqual([entry["status"] for entry in merged], [STATUS_STILL_OPEN])

    def test_finding_that_disappeared_becomes_resolved(self):
        previous = state_with([{**finding(), "status": STATUS_OPEN}])
        merged, _ = merge_findings(previous, [], HEAD_A)
        self.assertEqual([entry["status"] for entry in merged], [STATUS_RESOLVED])
        self.assertEqual(merged[0]["id"], FINDING_ID)

    def test_fresh_output_reopens_a_resolved_finding(self):
        previous = state_with(
            [{**finding(), "status": STATUS_RESOLVED, "resolved_at": "2026-09-01T12:00:00Z"}]
        )
        fresh = finding(created="2026-09-02T09:00:00Z")
        merged, _ = merge_findings(previous, [fresh], HEAD_A, last_review_at="2026-09-02T09:00:00Z")
        self.assertEqual([entry["status"] for entry in merged], [STATUS_REOPENED])
        self.assertNotIn("resolved_at", merged[0])

    def test_stale_output_does_not_reopen_a_resolved_finding(self):
        previous = state_with(
            [{**finding(), "status": STATUS_RESOLVED, "resolved_at": "2026-09-01T12:00:00Z"}]
        )
        stale = finding(created="2026-09-01T00:00:00Z")
        merged, _ = merge_findings(previous, [stale], HEAD_A, last_review_at="2026-09-01T00:00:00Z")
        self.assertEqual([entry["status"] for entry in merged], [STATUS_RESOLVED])

    def test_head_change_alone_does_not_reopen_when_a_boundary_exists(self):
        previous = state_with(
            [{**finding(), "status": STATUS_RESOLVED, "resolved_at": "2026-09-01T12:00:00Z"}],
            head=HEAD_A,
        )
        # Same old report, only the HEAD moved: not fresh evidence.
        merged, _ = merge_findings(previous, [finding(created="2026-09-01T11:00:00Z")], HEAD_B)
        self.assertEqual([entry["status"] for entry in merged], [STATUS_RESOLVED])

    def test_head_change_reopens_when_no_boundary_is_known(self):
        previous = state_with(
            [{**finding(), "status": STATUS_RESOLVED, "source_created": None}],
            head=HEAD_A,
            marker=None,
        )
        merged, _ = merge_findings(previous, [finding(created=None)], HEAD_B)
        self.assertEqual([entry["status"] for entry in merged], [STATUS_REOPENED])

    def test_marker_is_preserved_when_not_supplied(self):
        previous = state_with([{**finding(), "status": STATUS_OPEN}], marker="2026-09-01T00:00:00Z")
        _merged, marker = merge_findings(previous, [finding()], HEAD_A)
        self.assertEqual(marker, "2026-09-01T00:00:00Z")

    def test_findings_are_sorted_by_id(self):
        merged, _ = merge_findings(
            empty_state(),
            [finding(OTHER_ID, title="src/z.py: another finding worth reporting"),
             finding(FINDING_ID, title="src/a.py: a real finding to report")],
            HEAD_A,
            last_review_at="2026-09-01T00:00:00Z",
        )
        self.assertEqual([entry["id"] for entry in merged], sorted([FINDING_ID, OTHER_ID]))


class VerificationTransitionTests(unittest.TestCase):
    def test_resolving_settles_the_finding_in_place(self):
        state = state_with([{**finding(), "status": STATUS_STILL_OPEN}])
        findings = apply_verification(state, finding(), True, "2026-09-03T00:00:00Z")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["status"], STATUS_RESOLVED)
        self.assertEqual(findings[0]["resolved_at"], "2026-09-03T00:00:00Z")

    def test_unresolving_clears_the_resolution(self):
        state = state_with([{**finding(), "status": STATUS_RESOLVED, "resolved_at": "2026-09-03T00:00:00Z"}])
        findings = apply_verification(state, finding(), False, "2026-09-04T00:00:00Z")
        self.assertEqual(findings[0]["status"], STATUS_STILL_OPEN)
        self.assertNotIn("resolved_at", findings[0])

    def test_verification_does_not_duplicate_a_finding(self):
        state = state_with([{**finding(), "status": STATUS_STILL_OPEN}])
        first = apply_verification(state, finding(), True, "2026-09-03T00:00:00Z")
        again = apply_verification({**state, "findings": first}, finding(), False, "2026-09-04T00:00:00Z")
        self.assertEqual(len(again), 1)

    def test_verification_can_register_a_finding_missing_from_the_tracker(self):
        findings = apply_verification(empty_state(), finding(), True, "2026-09-03T00:00:00Z")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["status"], STATUS_RESOLVED)


class RenderTests(unittest.TestCase):
    def test_state_round_trips(self):
        body = render_tracker(HEAD_A, [{**finding(), "status": STATUS_OPEN}], "2026-09-01T00:00:00Z", "[continuum-review]", "pr-agent")
        self.assertIn(TRACKER_MARKER, body)
        state = parse_tracker_state(body)
        self.assertEqual(state["head"], HEAD_A)
        self.assertEqual(state["last_review_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(state["findings"][0]["id"], FINDING_ID)

    def test_pipe_in_a_title_cannot_break_the_table(self):
        body = render_tracker(
            HEAD_A,
            [{**finding(), "title": "src/a.py: a | b | c | injection", "status": STATUS_OPEN}],
            "2026-09-01T00:00:00Z",
            "[continuum-review]",
            "pr-agent",
        )
        table_rows = [line for line in body.splitlines() if line.startswith("| `")]
        self.assertEqual(len(table_rows), 1)
        self.assertEqual(json_block(body)["findings"][0]["title"], "src/a.py: a | b | c | injection")

    def test_empty_tracker_is_valid(self):
        body = render_tracker(HEAD_A, [], None, "[continuum-review]", "pr-agent")
        state = parse_tracker_state(body)
        self.assertEqual(state["findings"], [])

    def test_malformed_json_block_is_ignored(self):
        self.assertIsNone(parse_tracker_state(f"{TRACKER_MARKER}\n```json\nnot json\n```"))


class LoadTests(unittest.TestCase):
    def _tracker_comment(self, login, body):
        return issue_comment(body, login=login, created_at="2026-09-01T00:00:00Z")

    def test_untrusted_author_cannot_forge_state(self):
        forged = render_tracker(
            HEAD_B, [{**finding(), "status": STATUS_RESOLVED}], "2026-09-09T00:00:00Z", "[continuum-review]", "pr-agent"
        )
        genuine = render_tracker(
            HEAD_A, [{**finding(), "status": STATUS_OPEN}], "2026-09-01T00:00:00Z", "[continuum-review]", "pr-agent"
        )
        comments = [
            self._tracker_comment("github-actions[bot]", genuine),
            self._tracker_comment("drive-by", forged),
        ]
        comment, state = load_tracker(comments, trusted_logins=("github-actions[bot]",))
        self.assertEqual(comment["user"]["login"], "github-actions[bot]")
        self.assertEqual(state["head"], HEAD_A)
        self.assertEqual(state["findings"][0]["status"], STATUS_OPEN)

    def test_latest_valid_tracker_wins(self):
        first = render_tracker(HEAD_A, [{**finding(), "status": STATUS_OPEN}], "2026-09-01T00:00:00Z", "[continuum-review]", "pr-agent")
        second = render_tracker(HEAD_A, [{**finding(), "status": STATUS_RESOLVED}], "2026-09-02T00:00:00Z", "[continuum-review]", "pr-agent")
        _comment, state = load_tracker(
            [self._tracker_comment("github-actions[bot]", first), self._tracker_comment("github-actions[bot]", second)],
            trusted_logins=("github-actions[bot]",),
        )
        self.assertEqual(state["findings"][0]["status"], STATUS_RESOLVED)
        self.assertEqual(state["last_review_at"], "2026-09-02T00:00:00Z")

    def test_no_tracker_returns_empty_state(self):
        comment, state = load_tracker([issue_comment("unrelated")], trusted_logins=("github-actions[bot]",))
        self.assertIsNone(comment)
        self.assertEqual(state, empty_state())

    def test_garbage_state_entries_are_dropped(self):
        body = render_tracker(HEAD_A, [], None, "[continuum-review]", "pr-agent")
        injected = body.replace('"findings": []', '"findings": [{"id": "not-an-id"}, {"id": "CRV-1A2B3C4D", "status": "bogus", "line": "x"}]')
        state = parse_tracker_state(injected)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["findings"][0]["id"], "CRV-1A2B3C4D")
        self.assertEqual(state["findings"][0]["status"], STATUS_OPEN)
        self.assertEqual(state["findings"][0]["line"], 0)


if __name__ == "__main__":
    unittest.main()
