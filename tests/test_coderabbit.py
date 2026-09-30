"""Regression tests for the CodeRabbit adapter's review-state semantics.

The incident these pin: on 2026-09-28 three green, repaired pull requests
stopped merging. CodeRabbit re-checked the fixed findings and replied
`RESOLVED` inside its own threads while GitHub still reported
`isResolved: false`; the merge gate trusted only the flag, coupled an
exact-head approval to a fragile `Review completed` status description, and
treated any historical nitpick review as a permanent blocker.

Every fixture here is deterministic: no network, no clock, no model.
"""

from __future__ import annotations

import unittest

from continuum import config as config_module
from continuum.review import coderabbit
from continuum.review import result as result_module
from continuum.review.gate import run_gate
from tests import support
from tests.support import HEAD_A, HEAD_B

APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
COMMENTED = "COMMENTED"

SKIPPED = "Review skipped"
COMPLETED = "Review completed, actionable comments: 0"

RESOLVED_REPLY = "RESOLVED\n\nThe null guard is now present.\n\nReview thread resolved."
UNRESOLVED_REPLY = "UNRESOLVED\n\nThe null guard is still missing."


def meta(result):
    """Provider-specific detail the adapter recorded for the gate."""

    return result["metadata"]["metadata"]


def coderabbit_config() -> config_module.ContinuumConfig:
    return config_module.ContinuumConfig(
        version=1,
        review=config_module.ReviewSettings(
            provider=config_module.PROVIDER_CODERABBIT,
            block_merge=True,
            status_context=config_module.DEFAULT_STATUS_CONTEXT,
            coderabbit=config_module.CodeRabbitSettings(),
        ),
    )


def gate(client, **overrides):
    return run_gate(
        client,
        coderabbit_config(),
        7,
        head_sha=overrides.pop("head", HEAD_A),
        apply=overrides.pop("apply", False),
    )


def client(
    *,
    reviews=(),
    statuses=(),
    threads=(),
    comments=(),
    review_comments=(),
    **kwargs,
):
    payload = dict(
        reviews=list(reviews),
        statuses=list(statuses),
        threads=list(threads),
        issue_comments=list(comments) or [support.coderabbit_summary(HEAD_A)],
        review_comments=list(review_comments),
    )
    payload.update(kwargs)
    return support.FakeGitHub(**payload)


class StatusSurfaceTests(unittest.TestCase):
    """The commit status is operational state, never merge authorization."""

    def test_an_approval_survives_a_later_review_skipped_status(self):
        fake = client(
            reviews=[support.coderabbit_review(APPROVED, at="2026-09-01T00:00:00Z")],
            statuses=[support.coderabbit_status(SKIPPED, at="2026-09-02T00:00:00Z")],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["verdict"], result_module.VERDICT_APPROVE)
        self.assertEqual(result["open_findings"], 0)
        self.assertEqual(meta(result)["status_state"], "success")
        self.assertEqual(meta(result)["status_description"], SKIPPED)

    def test_changes_requested_blocks_even_when_the_status_says_skipped(self):
        fake = client(
            reviews=[
                support.coderabbit_review(
                    CHANGES_REQUESTED,
                    body="A dependency regression needs a fix before this can land.",
                    at="2026-09-01T00:00:00Z",
                )
            ],
            statuses=[support.coderabbit_status(SKIPPED, at="2026-09-02T00:00:00Z")],
        )
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        self.assertGreater(result["open_findings"], 0)

    def test_a_skipped_status_without_an_approval_never_opens_the_gate(self):
        fake = client(statuses=[support.coderabbit_status(SKIPPED)])
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertIn("no APPROVED review", result["reason"])

    def test_a_rate_limited_head_is_blocked_and_names_the_rate_limit(self):
        fake = client(
            statuses=[
                support.coderabbit_status(
                    "Review rate limited", state="error", at="2026-09-02T00:00:00Z"
                )
            ]
        )
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertIn("rate limit", result["reason"])

    def test_a_pending_status_blocks_a_head_that_was_never_approved(self):
        fake = client(statuses=[support.coderabbit_status("Review in progress", state="pending")])
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertIn("pending", result["reason"])

    def test_an_unreadable_status_fails_closed(self):
        fake = client(thread_state="status-failure")
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertIn("no 'CodeRabbit' status", result["reason"])


class ReviewIdentityTests(unittest.TestCase):
    """CI and review bind to the immutable head SHA, never to a timestamp."""

    def test_an_approval_from_another_head_never_authorizes_this_one(self):
        fake = client(
            reviews=[support.coderabbit_review(APPROVED, head_sha=HEAD_B)],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertIn("no APPROVED review", result["reason"])

    def test_a_review_older_than_ci_still_authorizes_the_same_head(self):
        # Both signals name the same SHA, so their relative order carries no
        # information: requiring CI to finish first is what deadlocked runs.
        fake = client(
            reviews=[support.coderabbit_review(APPROVED, at="2026-09-01T00:00:00Z")],
            statuses=[support.coderabbit_status(COMPLETED, at="2026-09-09T00:00:00Z")],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])

    def test_changes_requested_superseded_by_a_later_approval_opens_the_gate(self):
        fake = client(
            reviews=[
                support.coderabbit_review(
                    CHANGES_REQUESTED,
                    body="An earlier commit regressed the parser.",
                    at="2026-09-01T00:00:00Z",
                    review_id=1,
                ),
                support.coderabbit_review(APPROVED, at="2026-09-02T00:00:00Z", review_id=2),
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])
        self.assertEqual(meta(result)["review_state"], APPROVED)

    def test_a_human_approval_is_not_provider_output(self):
        fake = client(
            reviews=[
                {
                    "id": 1,
                    "state": APPROVED,
                    "body": "LGTM",
                    "user": {"login": "some-human"},
                    "commit_id": HEAD_A,
                    "submitted_at": "2026-09-01T00:00:00Z",
                }
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertFalse(result["gate_passed"])


class NitpickTests(unittest.TestCase):
    """Advisory nitpicks are superseded by a newer decisive verdict."""

    def test_a_nitpick_followed_by_an_approval_on_the_same_head_no_longer_blocks(self):
        fake = client(
            reviews=[
                support.coderabbit_review(
                    COMMENTED,
                    body=support.NITPICK_BODY,
                    at="2026-09-01T00:00:00Z",
                    review_id=1,
                ),
                support.coderabbit_review(APPROVED, at="2026-09-02T00:00:00Z", review_id=2),
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])
        self.assertEqual(meta(result)["nitpicks"], 1)
        self.assertEqual(meta(result)["blocking_nitpicks"], 0)

    def test_a_nitpick_newer_than_the_approval_still_blocks(self):
        fake = client(
            reviews=[
                support.coderabbit_review(APPROVED, at="2026-09-01T00:00:00Z", review_id=1),
                support.coderabbit_review(
                    COMMENTED,
                    body=support.NITPICK_BODY,
                    at="2026-09-02T00:00:00Z",
                    review_id=2,
                ),
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], result_module.VERDICT_COMMENT)
        self.assertEqual(meta(result)["blocking_nitpicks"], 1)
        self.assertIn("nitpick", result["reason"])

    def test_a_thread_confirmation_shell_is_not_a_nitpick(self):
        fake = client(
            reviews=[
                support.coderabbit_review(
                    COMMENTED,
                    body="Review details\n\nReply to this thread.",
                    at="2026-09-01T00:00:00Z",
                    review_id=1,
                ),
                support.coderabbit_review(APPROVED, at="2026-09-02T00:00:00Z", review_id=2),
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])
        self.assertEqual(meta(result)["nitpicks"], 0)

    def test_a_nitpick_on_another_head_never_reaches_this_gate(self):
        fake = client(
            reviews=[
                support.coderabbit_review(APPROVED, at="2026-09-02T00:00:00Z", review_id=1),
                support.coderabbit_review(
                    COMMENTED,
                    body=support.NITPICK_BODY,
                    head_sha=HEAD_B,
                    at="2026-09-03T00:00:00Z",
                    review_id=2,
                ),
            ],
            statuses=[support.coderabbit_status(COMPLETED)],
        )
        result = gate(fake)

        self.assertTrue(result["gate_passed"])
        self.assertEqual(meta(result)["blocking_nitpicks"], 0)


class ThreadNormalizationTests(unittest.TestCase):
    """CodeRabbit's textual verdict is normalized into GitHub's own state."""

    def _repaired(self, reply, **kwargs):
        return client(
            reviews=[support.coderabbit_review(APPROVED)],
            statuses=[support.coderabbit_status(COMPLETED)],
            threads=[support.coderabbit_thread("PRRT_a", replies=[reply], **kwargs)],
            review_comments=[
                support.review_comment(
                    "This misses the null guard around the parsed payload.",
                    path="app.py",
                    line=42,
                    login=support.CODERABBIT_LOGIN,
                    commit_id=HEAD_A,
                    comment_id=500,
                )
            ],
        )

    def test_a_resolved_reply_normalizes_the_thread_and_continues(self):
        fake = self._repaired(RESOLVED_REPLY)
        result = gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, ["PRRT_a"])
        self.assertEqual(meta(result)["threads"]["semantically_resolved"], 1)
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["open_findings"], 0)

    def test_an_explicit_unresolved_reply_stays_blocking(self):
        fake = self._repaired(UNRESOLVED_REPLY)
        result = gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, [])
        self.assertEqual(meta(result)["threads"]["blocking"], 1)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)
        self.assertGreater(result["open_findings"], 0)

    def test_a_failed_normalization_fails_closed(self):
        fake = self._repaired(RESOLVED_REPLY)
        fake.fail_resolve_thread = True
        result = gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, [])
        self.assertEqual(meta(result)["threads"]["normalization_failed"], 1)
        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["verdict"], result_module.VERDICT_REQUEST_CHANGES)

    def test_a_dry_run_predicts_normalization_without_writing(self):
        fake = self._repaired(RESOLVED_REPLY)
        result = gate(fake, apply=False)

        self.assertEqual(fake.resolved_threads, [])
        self.assertNotIn("resolve_review_thread", fake.call_names())
        self.assertTrue(result["gate_passed"])

    def test_a_silent_reply_is_not_treated_as_settled(self):
        fake = self._repaired("Thanks, taking a look.")
        result = gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, [])
        self.assertFalse(result["gate_passed"])

    def test_a_resolved_reply_in_a_human_thread_is_not_provider_output(self):
        thread = support.coderabbit_thread("PRRT_a", replies=[RESOLVED_REPLY])
        thread["comments"][0]["author"] = "some-human"
        fake = client(
            reviews=[support.coderabbit_review(APPROVED)],
            statuses=[support.coderabbit_status(COMPLETED)],
            threads=[thread],
            review_comments=[
                support.review_comment(
                    "This misses the null guard around the parsed payload.",
                    path="app.py",
                    line=42,
                    login=support.CODERABBIT_LOGIN,
                    commit_id=HEAD_A,
                    comment_id=500,
                )
            ],
        )
        result = gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, [])
        self.assertFalse(result["gate_passed"])

    def test_an_outdated_thread_is_not_normalized(self):
        fake = self._repaired(RESOLVED_REPLY, is_outdated=True)
        gate(fake, apply=True)

        self.assertEqual(fake.resolved_threads, [])

    def test_an_unknown_thread_state_keeps_every_inline_finding(self):
        fake = self._repaired(RESOLVED_REPLY)
        fake.thread_state = "graphql-error"
        result = gate(fake, apply=True)

        self.assertFalse(result["metadata"]["thread_state_known"])
        self.assertFalse(result["gate_passed"])


class ReplyClassificationTests(unittest.TestCase):
    def test_the_machine_token_and_the_plain_sentence_agree(self):
        self.assertEqual(
            coderabbit.classify_reply("RESOLVED"), coderabbit.REPLY_RESOLVED
        )
        self.assertEqual(
            coderabbit.classify_reply("Review thread resolved."), coderabbit.REPLY_RESOLVED
        )

    def test_an_explicit_refusal_wins_over_positive_wording(self):
        self.assertEqual(
            coderabbit.classify_reply(
                "The earlier fix was resolved, but this is UNRESOLVED still."
            ),
            coderabbit.REPLY_UNRESOLVED,
        )

    def test_a_phrasing_that_is_not_resolved_stays_blocking(self):
        self.assertEqual(
            coderabbit.classify_reply("The thread is not resolved yet."),
            coderabbit.REPLY_UNRESOLVED,
        )

    def test_a_reply_without_a_verdict_states_none(self):
        self.assertEqual(coderabbit.classify_reply("Looking into this now."), "")

    def test_a_question_about_resolution_states_no_verdict(self):
        self.assertEqual(coderabbit.classify_reply("Is this thread resolved?"), "")
        self.assertEqual(
            coderabbit.classify_reply("Could you confirm the issue is resolved?"), ""
        )

    def test_only_the_newest_provider_reply_decides(self):
        thread = support.coderabbit_thread(
            "PRRT_a", replies=[UNRESOLVED_REPLY, RESOLVED_REPLY]
        )
        self.assertEqual(
            coderabbit.thread_reply_verdict(thread), coderabbit.REPLY_RESOLVED
        )

    def test_a_human_reply_never_overrides_the_provider(self):
        thread = support.coderabbit_thread("PRRT_a", replies=[RESOLVED_REPLY])
        thread["comments"].append(
            {
                "database_id": 599,
                "author": "some-human",
                "body": RESOLVED_REPLY,
                "created_at": "2026-09-01T02:00:00Z",
            }
        )
        self.assertEqual(
            coderabbit.thread_reply_verdict(thread), coderabbit.REPLY_RESOLVED
        )


class QuotationTests(unittest.TestCase):
    """A verdict has to be stated, not merely present somewhere in the reply.

    CodeRabbit re-quotes the finding it is re-checking, so the original comment's
    text arrives in the same reply as the new verdict about it. Searching the
    whole body reads the quotation as the verdict, and it fails in whichever
    direction the quotation points -- one of which silently drops a live finding.
    """

    def test_a_quoted_earlier_verdict_does_not_outvote_the_current_one(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "Earlier this thread said: unresolved.\n"
                "I re-checked the head and the function now guards the empty case.\n"
                "resolved"
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_a_collapsed_details_block_is_a_quotation(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "<details><summary>previous comment</summary>\n\n"
                "unresolved\n\n"
                "</details>\n\n"
                "The comment no longer applies. resolved"
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_fenced_code_is_not_prose(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "```python\n"
                "# resolved: set when the retry succeeds\n"
                "def mark():\n"
                "    pass\n"
                "```\n\n"
                "I applied the fix. resolved"
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_indented_code_is_a_quotation(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "Previously:\n\n    unresolved\n\nFixed now. resolved"
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_a_block_quote_is_not_prose(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "> a finding counts as resolved only when the provider says so\n"
                "I applied the fix. resolved"
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_inline_code_mentions_the_word_without_stating_it(self) -> None:
        # The flag name is not a verdict. Reading it as one would settle every
        # thread whose client happens to use these identifiers.
        self.assertEqual(
            coderabbit.classify_reply("`resolved` is set in the client once it passes."),
            "",
        )

    def test_inline_code_does_not_hide_a_verdict_stated_alongside_it(self) -> None:
        self.assertEqual(
            coderabbit.classify_reply(
                "Removed `unresolved_ok`; the finding is resolved."
            ),
            coderabbit.REPLY_RESOLVED,
        )

    def test_a_quotation_never_substitutes_for_a_stated_verdict(self) -> None:
        # Everything in this reply is code or quotation, so the reply states no
        # verdict at all and the thread stays exactly as it was.
        self.assertEqual(
            coderabbit.classify_reply(
                "```text\nresolved\n```\n\n> unresolved\n\n`resolved`"
            ),
            "",
        )

    def test_an_unterminated_fence_is_read_as_code_to_the_end(self) -> None:
        # Truncated replies happen. Treating the remainder as prose would let a
        # half-quoted block decide a finding.
        self.assertEqual(coderabbit.classify_reply("```text\nresolved\n"), "")

    def test_a_self_contradicting_line_stays_blocking(self) -> None:
        # Both forms on one line: the line has not settled it, and the gate can
        # recover from an over-reported finding but not from a dropped one.
        self.assertEqual(
            coderabbit.classify_reply("I fixed it but it is unresolved for now."),
            coderabbit.REPLY_UNRESOLVED,
        )


class QuotaBehaviourTests(unittest.TestCase):
    """A settled thread and a cleared head must not spend more quota."""

    def test_a_cleared_head_with_a_skipped_status_is_never_re_requested(self):
        covered = coderabbit.covers_head(
            [support.coderabbit_review(APPROVED)],
            [support.coderabbit_status(SKIPPED)],
            HEAD_A,
        )
        self.assertTrue(covered)

    def test_an_untouched_head_is_still_owed_the_shared_slot(self):
        self.assertFalse(
            coderabbit.covers_head(
                [support.coderabbit_review(APPROVED, head_sha=HEAD_B)],
                [support.coderabbit_status(SKIPPED, head_sha=HEAD_B)],
                HEAD_A,
            )
        )

    def test_another_identitys_review_does_not_settle_the_slot(self):
        reviews = [
            {
                "id": 1,
                "state": APPROVED,
                "body": "LGTM",
                "user": {"login": "some-human"},
                "commit_id": HEAD_A,
                "submitted_at": "2026-09-01T00:00:00Z",
            }
        ]
        self.assertFalse(coderabbit.covers_head(reviews, [], HEAD_A))

    def test_a_full_review_still_counts_as_quota_use(self):
        self.assertTrue(coderabbit.is_full_review(support.coderabbit_review(APPROVED)))
        self.assertTrue(coderabbit.is_full_review(support.coderabbit_review(COMMENTED, body="nitpicks")))
        self.assertFalse(coderabbit.is_full_review(support.coderabbit_review(COMMENTED, body="")))


if __name__ == "__main__":
    unittest.main()
