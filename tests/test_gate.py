"""End-to-end gate behavior: verdicts, publish paths, and fail-closed cases."""

from __future__ import annotations

import unittest

from continuum import config as config_module
from continuum.review import result as result_module
from continuum.review import tracker as tracker_module
from continuum.review.gate import EVENT_FOR_VERDICT, evaluate, publish, run_gate
from continuum.review.providers import supported_providers
from tests.support import (
    HEAD_A,
    HEAD_B,
    FakeGitHub,
    clean_pr_agent_report,
    issue_comment,
    pr_agent_report,
    review_comment,
    review_config,
    snapshot,
)

SMALL_FILES = [{"filename": "app.py", "additions": 1, "deletions": 0}]
MEDIUM_FILES = [
    {"filename": "src/a.py", "additions": 4, "deletions": 1},
    {"filename": "src/b.py", "additions": 2, "deletions": 0},
]


class DecisionTests(unittest.TestCase):
    def test_findings_block_the_gate(self):
        client = FakeGitHub(
            issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            review_comments=[
                review_comment("Memory leak when the queue is emptied", path="src/queue.py", line=3)
            ],
            unresolved_ids={1},
        )
        result = run_gate(
            client,
            review_config(),
            7,
            head_sha=HEAD_A,
            apply=False,
        )
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        self.assertEqual(result["state"], result_module.STATE_CHANGES_REQUESTED)
        self.assertFalse(result["gate_passed"])
        self.assertTrue(result["blocking"])
        self.assertGreater(result["open_findings"], 0)

    def test_clean_and_covered_approves(self):
        client = FakeGitHub(issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))])
        result = run_gate(client, review_config(), 7, head_sha=HEAD_A, apply=False)
        self.assertEqual(result["verdict"], result_module.VERDICT_APPROVE)
        self.assertTrue(result["gate_passed"])
        self.assertFalse(result["blocking"])
        self.assertEqual(result["open_findings"], 0)

    def test_clean_but_coverage_gap_is_blocked(self):
        client = FakeGitHub(
            issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            files=MEDIUM_FILES,
        )
        result = run_gate(client, review_config(), 7, head_sha=HEAD_A, apply=False)
        self.assertEqual(result["verdict"], result_module.VERDICT_COMMENT)
        self.assertEqual(result["state"], result_module.STATE_BLOCKED)
        self.assertFalse(result["gate_passed"])
        self.assertTrue(result["blocking"])

    def test_unknown_thread_state_keeps_every_inline_finding(self):
        client = FakeGitHub(
            issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            review_comments=[
                review_comment("Race on the shared counter variable", path="src/counter.py", line=9)
            ],
            thread_state="graphql-error",
        )
        result = run_gate(client, review_config(), 7, head_sha=HEAD_A, apply=False)
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        self.assertGreater(result["open_findings"], 0)
        self.assertEqual(result["metadata"]["thread_state_known"], False)

    def test_historical_summary_cannot_stand_in_for_current_head(self):
        stale = issue_comment(clean_pr_agent_report(HEAD_B), created_at="2026-09-01T00:00:00Z")
        result = evaluate(
            config=review_config(),
            snapshot=snapshot(summary_comments=[stale]),
            pr_number=7,
            head=HEAD_A,
            files=SMALL_FILES,
            previous_state={"last_review_at": "2026-09-05T00:00:00Z"},
        )
        self.assertEqual(result["verdict"], result_module.VERDICT_COMMENT)
        self.assertEqual(result["state"], result_module.STATE_BLOCKED)
        self.assertFalse(result["gate_passed"])

    def test_previous_open_finding_stays_still_open(self):
        base = FakeGitHub(
            issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            review_comments=[
                review_comment(
                    "Retry loop swallows the permanent error",
                    path="src/worker.py",
                    line=10,
                    created_at="2026-09-01T00:00:00Z",
                )
            ],
            unresolved_ids={1},
        )
        first = run_gate(base, review_config(), 7, head_sha=HEAD_A, apply=False)
        finding_id = first["findings"][0]["id"]

        previous = {
            "head": HEAD_A,
            "last_review_at": "2026-09-01T12:00:00Z",
            "findings": [{**first["findings"][0], "status": "open"}],
        }
        rerun = evaluate(
            config=review_config(),
            snapshot=snapshot(
                summary_comments=[
                    issue_comment(clean_pr_agent_report(HEAD_A), created_at="2026-09-01T13:00:00Z")
                ],
                review_comments=[
                    review_comment(
                        "Retry loop swallows the permanent error",
                        path="src/worker.py",
                        line=10,
                        created_at="2026-09-01T00:00:00Z",
                    )
                ],
                unresolved_ids={1},
            ),
            pr_number=7,
            head=HEAD_A,
            files=SMALL_FILES,
            previous_state=previous,
        )
        entry = next(f for f in rerun["findings"] if f["id"] == finding_id)
        self.assertEqual(entry["status"], "still-open")
        self.assertEqual(rerun["verdict"], result_module.VERDICT_REQUEST_CHANGES)


class CoverageAndAdaptersTests(unittest.TestCase):
    def test_summary_findings_are_derived(self):
        client = FakeGitHub(
            issue_comments=[
                issue_comment(
                    pr_agent_report(HEAD_A, ["Unhandled exception in the worker loop"])
                )
            ],
        )
        result = run_gate(
            client,
            review_config(),
            7,
            head_sha=HEAD_A,
            apply=False,
        )
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        titles = [finding["title"] for finding in result["findings"]]
        self.assertTrue(any("Unhandled exception" in title for title in titles))
        source_set = {finding["source"] for finding in result["findings"]}
        self.assertEqual(source_set, {"summary"})

    def test_supported_providers_are_listed(self):
        self.assertEqual(set(supported_providers()), {"pr-agent", "coderabbit"})

    def test_forced_open_titles_block_from_a_verdict_without_inline_findings(self):
        client = FakeGitHub(
            issue_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            reviews=[
                {
                    "id": 1,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-09-01T00:00:00Z",
                    "commit_id": HEAD_A,
                    "user": {"login": "coderabbitai[bot]"},
                    "body": "A dependency regression needs a fix.",
                }
            ],
        )
        result = run_gate(
            client,
            review_config(provider=config_module.PROVIDER_CODERABBIT),
            7,
            head_sha=HEAD_A,
            apply=False,
        )
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        self.assertGreater(result["open_findings"], 0)


class DisabledProviderTests(unittest.TestCase):
    def test_disabled_provider_produces_zero_traffic(self):
        client = FakeGitHub()
        result = run_gate(client, review_config(provider=config_module.PROVIDER_NONE), 7, apply=True)
        self.assertEqual(result["verdict"], result_module.VERDICT_NONE)
        self.assertEqual(result["state"], result_module.STATE_SKIPPED)
        self.assertTrue(result["gate_passed"])
        self.assertEqual(client.call_names(), [])

    def test_disabled_provider_evaluate_result(self):
        result = evaluate(
            config=review_config(provider=config_module.PROVIDER_NONE),
            snapshot=snapshot(),
            pr_number=7,
            head=HEAD_A,
            files=SMALL_FILES,
        )
        self.assertEqual(result["provider"], config_module.PROVIDER_NONE)
        self.assertEqual(result["open_findings"], 0)
        self.assertTrue(result["gate_passed"])


class PublishTests(unittest.TestCase):
    def _approved_result(self, client):
        return evaluate(
            config=review_config(),
            snapshot=snapshot(
                summary_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
            ),
            pr_number=7,
            head=HEAD_A,
            files=SMALL_FILES,
        )

    def test_publish_writes_tracker_review_and_status(self):
        client = FakeGitHub()
        result = self._approved_result(client)
        publish(client, review_config(), 7, result, state={}, marker="2026-09-01T00:00:00Z")
        self.assertEqual(client.reviews_created[0]["event"], "APPROVE")
        self.assertEqual(client.statuses_created[0]["state"], "success")
        self.assertEqual(len(client.tracker_comments()), 1)
        state = client.tracker_state()
        self.assertEqual(state["head"], HEAD_A)
        self.assertEqual(state["findings"], [])

    def test_status_description_implements_the_normalized_contract(self):
        client = FakeGitHub()
        result = self._approved_result(client)
        publish(client, review_config(), 7, result, state={}, marker="2026-09-01T00:00:00Z")
        description = client.statuses_created[0]["description"]
        parsed = result_module.parse_status_description(description)
        self.assertEqual(parsed["verdict"], "APPROVE")
        self.assertEqual(parsed["state"], "approved")
        self.assertEqual(parsed["provider"], "pr-agent")
        self.assertEqual(parsed["head"], HEAD_A)

    def test_approve_missing_review_authority_falls_back_to_comment_and_stays_closed(self):
        client = FakeGitHub()
        client.fail_approve_with_422 = True
        result = self._approved_result(client)
        published = publish(client, review_config(), 7, result, state={}, marker="2026-09-01T00:00:00Z")
        self.assertEqual(published["verdict"], result_module.VERDICT_COMMENT)
        self.assertEqual(published["state"], result_module.STATE_BLOCKED)
        self.assertFalse(published["gate_passed"])
        self.assertEqual(client.reviews_created[0]["event"], "COMMENT")
        self.assertEqual(client.statuses_created[0]["state"], "failure")

    def test_findings_review_uses_request_changes(self):
        client = FakeGitHub()
        result = evaluate(
            config=review_config(),
            snapshot=snapshot(
                summary_comments=[issue_comment(clean_pr_agent_report(HEAD_A))],
                review_comments=[
                    review_comment("Unhandled error remains in the retry loop", path="src/retry.py", line=5)
                ],
                unresolved_ids={1},
            ),
            pr_number=7,
            head=HEAD_A,
            files=SMALL_FILES,
        )
        publish(client, review_config(), 7, result, state={}, marker="2026-09-01T00:00:00Z")
        self.assertEqual(client.reviews_created[0]["event"], "REQUEST_CHANGES")
        self.assertEqual(client.statuses_created[0]["state"], "failure")

    def test_event_mapping_is_explicit(self):
        self.assertEqual(
            EVENT_FOR_VERDICT,
            {"APPROVE": "APPROVE", "REQUEST_CHANGES": "REQUEST_CHANGES", "COMMENT": "COMMENT", "NONE": None},
        )


if __name__ == "__main__":
    unittest.main()