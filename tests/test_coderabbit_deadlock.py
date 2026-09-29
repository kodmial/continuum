"""CodeRabbit deadlock regressions (continuum#37).

Three production deadlocks, one normalized gate. Each fixture below is a shape
that actually happened: a green repaired pull request that stopped merging for
a reason no log named.

* A `Review skipped` success status overwrote the status surface of a head that
  already carried a durable APPROVED review, so a correct gate looked closed.
* A historical nitpick COMMENTED review outlived a later decisive APPROVED on
  the same head and blocked forever.
* CodeRabbit answered `RESOLVED` in a thread while GitHub's own
  `isResolved` stayed false, so a fixed finding was re-requested indefinitely.

No network call and no provider is contacted: every fixture is a frozen record
of what the provider published.
"""

from __future__ import annotations

import unittest

from continuum import config as config_module
from continuum.review import coderabbit, gate
from tests.support import (
    HEAD_A,
    HEAD_B,
    FakeGitHub,
    issue_comment,
    review_comment,
)

BOT = coderabbit.DEFAULT_BOT_LOGIN
CONTEXT = coderabbit.DEFAULT_STATUS_CONTEXT

APPROVED_AT = "2026-09-28T10:00:00Z"
NITPICK_AT = "2026-09-28T09:00:00Z"
LATE_NITPICK_AT = "2026-09-28T11:00:00Z"


def review(state: str, *, review_id: int, head: str = HEAD_A, at: str, body: str = ""):
    return {
        "id": review_id,
        "state": state,
        "commit_id": head,
        "submitted_at": at,
        "body": body,
        "user": {"login": BOT},
    }


def status(state: str, description: str, *, head: str = HEAD_A, at: str = "2026-09-28T10:05:00Z"):
    return {
        "context": CONTEXT,
        "state": state,
        "description": description,
        "sha": head,
        "updated_at": at,
        "created_at": at,
    }


def thread(*, thread_id: str, resolved: bool, comments, outdated: bool = False):
    return {
        "id": thread_id,
        "is_resolved": resolved,
        "is_outdated": outdated,
        "comments": list(comments),
    }


def thread_comment(
    database_id: int,
    body: str,
    *,
    login: str = BOT,
    created_at: str = "2026-09-28T10:00:00Z",
):
    return {
        "databaseId": database_id,
        "body": body,
        "createdAt": created_at,
        "user": {"login": login},
    }


def clean_summary(at: str):
    """A post-approval provider summary that names the current HEAD.

    Coverage is only complete when provider output that describes this exact HEAD
    exists, so every green fixture carries one.
    """

    body = f"## Walkthrough\n\nReviewed `{HEAD_A}`.\n\nNo actionable comments.\n\n<!-- CodeRabbit -->\n"
    return issue_comment(body, login=BOT, created_at=at, updated_at=at)


def config_for_review():
    return config_module.ContinuumConfig(
        version=1,
        review=config_module.ReviewSettings(
            provider=config_module.PROVIDER_CODERABBIT, block_merge=True
        ),
    )


def run(client: FakeGitHub, **kwargs):
    return gate.run_gate(client, config_for_review(), 7, **kwargs)


class StatusSurfaceTests(unittest.TestCase):
    """The status is operational state, not merge authorization."""

    def test_an_approval_survives_a_later_review_skipped_status(self):
        client = FakeGitHub(
            issue_comments=[clean_summary(APPROVED_AT)],
            reviews=[review("APPROVED", review_id=1, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
        )
        result = run(client, apply=False)
        self.assertTrue(result["gate_passed"], result["reason"])
        self.assertEqual(result["verdict"], "APPROVE")

    def test_a_request_for_changes_still_blocks_on_the_same_status(self):
        client = FakeGitHub(
            reviews=[review("CHANGES_REQUESTED", review_id=1, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
        )
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "REQUEST_CHANGES")

    def test_a_missing_status_still_blocks_without_an_approval(self):
        client = FakeGitHub(reviews=[], statuses=[])
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])
        self.assertIn("no", result["coverage"]["reason"])

    def test_a_failing_status_still_blocks_without_an_approval(self):
        client = FakeGitHub(reviews=[], statuses=[status("pending", "Review in progress")])
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])

    def test_an_approval_for_another_head_never_authorizes_this_one(self):
        client = FakeGitHub(
            reviews=[review("APPROVED", review_id=1, head=HEAD_B, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
        )
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])


class NitpickSupersessionTests(unittest.TestCase):
    """Advisory output cannot outlive a later decisive verdict on the same head."""

    def _client(self, reviews, inline, *, unresolved=None):
        return FakeGitHub(
            issue_comments=[clean_summary("2026-09-28T12:00:00Z")],
            reviews=reviews,
            review_comments=inline,
            unresolved_ids=unresolved or set(),
            statuses=[status("success", "Review completed")],
        )

    def test_a_nitpick_before_an_approval_no_longer_blocks(self):
        finding = review_comment(
            "Rename this helper for clarity",
            path="app.py",
            line=3,
            login=BOT,
            created_at=NITPICK_AT,
        )
        client = self._client(
            [
                review("COMMENTED", review_id=1, at=NITPICK_AT, body="Nitpick"),
                review("APPROVED", review_id=2, at=APPROVED_AT),
            ],
            [finding],
            unresolved={finding["id"]},
        )
        result = run(client, apply=False)
        self.assertTrue(result["gate_passed"], result["reason"])
        self.assertEqual(result["open_findings"], 0)

    def test_a_nitpick_after_an_approval_still_blocks(self):
        finding = review_comment(
            "Rename this helper for clarity",
            path="app.py",
            line=3,
            login=BOT,
            created_at=LATE_NITPICK_AT,
        )
        client = self._client(
            [review("APPROVED", review_id=1, at=APPROVED_AT)],
            [finding],
            unresolved={finding["id"]},
        )
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "REQUEST_CHANGES")

    def test_a_stale_nitpick_from_another_head_is_not_carried(self):
        finding = review_comment(
            "Rename this helper for clarity",
            path="app.py",
            line=3,
            login=BOT,
            commit_id=HEAD_B,
            original_commit_id=HEAD_B,
        )
        client = self._client(
            [review("APPROVED", review_id=1, at=APPROVED_AT)],
            [finding],
            unresolved={finding["id"]},
        )
        result = run(client, apply=False)
        self.assertTrue(result["gate_passed"], result["reason"])


class ThreadResolutionTests(unittest.TestCase):
    """A provider's explicit answer outranks GitHub's lagging flag."""

    def _client(self, *, threads, unresolved, resolve_fails=()):
        client = FakeGitHub(
            reviews=[review("APPROVED", review_id=1, at="2026-09-28T12:00:00Z")],
            review_comments=[],
            issue_comments=[clean_summary("2026-09-28T12:05:00Z")],
            statuses=[status("success", "Review skipped")],
            unresolved_ids=unresolved,
            threads=threads,
        )
        client.resolve_failures = set(resolve_fails)
        return client

    def test_an_explicit_resolved_reply_closes_a_thread_github_kept_open(self):
        threads = [
            thread(
                thread_id="T1",
                resolved=False,
                comments=[
                    thread_comment(1, "This can leak a handle", created_at=NITPICK_AT),
                    thread_comment(2, "✅ Review thread resolved.", created_at=APPROVED_AT),
                ],
            )
        ]
        client = self._client(threads=threads, unresolved={1, 2})
        result = run(client, apply=True)
        self.assertEqual(client.threads_resolved, ["T1"])
        self.assertTrue(result["gate_passed"], result["reason"])

    def test_an_explicit_unresolved_reply_blocks_even_when_github_says_resolved(self):
        threads = [
            thread(
                thread_id="T1",
                resolved=True,
                comments=[
                    thread_comment(1, "Still reproducible", created_at=NITPICK_AT),
                    thread_comment(2, "UNRESOLVED", created_at=APPROVED_AT),
                ],
            )
        ]
        client = self._client(threads=threads, unresolved=set())
        snapshot = coderabbit.collect(client, 7, HEAD_A, bot_login=BOT, status_context=CONTEXT)
        self.assertIn(1, snapshot.unresolved_ids)
        self.assertIn(2, snapshot.unresolved_ids)
        self.assertEqual(snapshot.superseded_thread_ids, set())

    def test_a_failed_normalization_fails_closed(self):
        threads = [
            thread(
                thread_id="T1",
                resolved=False,
                comments=[
                    thread_comment(1, "This can leak a handle", created_at=NITPICK_AT),
                    thread_comment(2, "RESOLVED", created_at=APPROVED_AT),
                ],
            )
        ]
        client = self._client(threads=threads, unresolved={1, 2}, resolve_fails=["T1"])
        result = run(client, apply=True)
        self.assertEqual(client.threads_resolved, [])
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["state"], "blocked")
        self.assertIn("normalization", result["reason"])

    def test_an_unanswered_thread_keeps_githubs_own_answer(self):
        threads = [
            thread(
                thread_id="T1",
                resolved=False,
                comments=[thread_comment(1, "This can leak a handle", created_at=NITPICK_AT)],
            )
        ]
        client = self._client(threads=threads, unresolved={1})
        snapshot = coderabbit.collect(client, 7, HEAD_A, bot_login=BOT, status_context=CONTEXT)
        self.assertEqual(snapshot.unresolved_ids, {1})
        self.assertEqual(snapshot.superseded_thread_ids, set())

    def test_an_outdated_thread_is_never_normalized(self):
        threads = [
            thread(
                thread_id="T1",
                resolved=False,
                outdated=True,
                comments=[
                    thread_comment(1, "old", created_at=NITPICK_AT),
                    thread_comment(2, "RESOLVED", created_at=APPROVED_AT),
                ],
            )
        ]
        self.assertEqual(
            coderabbit.pending_thread_normalizations(threads, BOT),
            [],
        )

    def test_unknown_thread_state_normalizes_nothing(self):
        client = FakeGitHub(
            issue_comments=[clean_summary("2026-09-28T12:05:00Z")],
            reviews=[review("APPROVED", review_id=1, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
            thread_state="graphql-error",
        )
        result = run(client, apply=True)
        self.assertNotIn("resolve_review_thread", client.call_names())
        self.assertTrue(result["gate_passed"], result["reason"])


class SummarySupersessionTests(unittest.TestCase):
    def test_a_summary_published_before_the_approval_is_dropped(self):
        body = (
            f"## Review details\n\n"
            f"- The retry path swallows the timeout error in `{HEAD_A}`\n\n"
            "<!-- CodeRabbit -->\n"
        )
        client = FakeGitHub(
            issue_comments=[
                issue_comment(body, login=BOT, created_at=NITPICK_AT, updated_at=NITPICK_AT),
                clean_summary(APPROVED_AT),
            ],
            reviews=[review("APPROVED", review_id=1, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
        )
        result = run(client, apply=False)
        self.assertTrue(result["gate_passed"], result["reason"])
        self.assertEqual(result["open_findings"], 0)

    def test_a_summary_published_after_the_approval_still_blocks(self):
        body = (
            f"## Review details\n\n"
            f"- The retry path swallows the timeout error in `{HEAD_A}`.\n\n"
            "<!-- CodeRabbit -->\n"
        )
        client = FakeGitHub(
            issue_comments=[issue_comment(body, login=BOT, created_at=LATE_NITPICK_AT, updated_at=LATE_NITPICK_AT)],
            reviews=[review("APPROVED", review_id=1, at=APPROVED_AT)],
            statuses=[status("success", "Review skipped")],
        )
        result = run(client, apply=False)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], "REQUEST_CHANGES")


class QuotaTests(unittest.TestCase):
    def test_a_resolved_thread_does_not_count_as_a_pending_full_review(self):
        # The fifth deadlock clause: a semantically resolved thread must not make
        # the queue ask for another included review of a finished change.
        comment = {"id": 1, "state": "COMMENTED", "body": "resolved", "user": {"login": BOT}}
        self.assertTrue(coderabbit.is_full_review(comment))
        # The queue's own eligibility still requires an unresolved thread, so the
        # gate's superseded set is what stops the loop.
        self.assertEqual(coderabbit.thread_resolution([thread_comment(1, "RESOLVED")], BOT), "resolved")


if __name__ == "__main__":
    unittest.main()
