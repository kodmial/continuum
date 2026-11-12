"""Deterministic tests for the exact-HEAD CI-repair receipt protocol (#444).

The protocol is phase 1 of kodmial/continuum#443: a pure, dependency-free
evidence format and fail-closed reader. These tests cover round-trips,
same-HEAD claim collisions, independent new HEADs, trusted/untrusted
authors, missing/partial/mixed/conflicting records, incomplete history,
accepted run-ID receipts, ambiguous dispatch safety, impossible transitions,
and version mismatch. No network, labels, comments, or workflow state are
touched.
"""

from __future__ import annotations

import unittest

from continuum.ci_repair_receipt import (
    ACTION_CLAIM,
    ACTION_HOLD,
    ACTION_WAIT,
    OUTCOME_COMPLETE,
    OUTCOME_CONFLICT,
    OUTCOME_INCOMPLETE,
    OUTCOME_INVALID,
    RECEIPT_MARKER,
    RECEIPT_SCHEMA_VERSION,
    STATUS_AMBIGUOUS,
    STATUS_CLAIMED,
    STATUS_DISPATCHED,
    STATUS_REJECTED,
    AttemptSummary,
    ReceiptCollection,
    ReceiptError,
    attempt_for_id,
    build_receipt,
    decide_new_attempt,
    new_attempt_id,
    normalize_attempt_id,
    parse_receipts,
    read_receipts,
    receipt_key,
    receipt_marker_line,
    render_receipt,
    transition_attempt,
    transition_receipt,
)

REPO = "Kodmial/Continuum"
PR = 444
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
ATTEMPT = "attempt-0001"
OTHER_ATTEMPT = "attempt-0002"


def comment(
    body,
    *,
    login="github-actions[bot]",
    association="NONE",
    comment_id=1,
    created_at="2026-11-12T00:00:00Z",
):
    return {
        "body": body,
        "author_association": association,
        "user": {"login": login},
        "id": comment_id,
        "created_at": created_at,
        "updated_at": created_at,
    }


def receipt(
    *,
    head=HEAD,
    attempt=ATTEMPT,
    status=STATUS_CLAIMED,
    run_id=None,
    run_url=None,
    repository=REPO,
    pull_number=PR,
):
    return build_receipt(
        repository=repository,
        pull_number=pull_number,
        head_sha=head,
        attempt_id=attempt,
        status=status,
        run_id=run_id,
        run_url=run_url,
    )


def receipt_comment(**kwargs):
    return comment(render_receipt(receipt(**kwargs)))


def read(comments, *, head=HEAD, complete=True, ordered=True, expected=None, owner=""):
    return read_receipts(
        comments,
        repository=REPO,
        pull_number=PR,
        head_sha=head,
        history_complete=complete,
        history_ordered=ordered,
        expected_comment_count=expected,
        owner_login=owner,
    )


class BuildRenderParseTests(unittest.TestCase):
    def test_claimed_receipt_round_trip(self):
        built = receipt()
        self.assertEqual(RECEIPT_SCHEMA_VERSION, built.schema_version)
        self.assertEqual("kodmial/continuum", built.repository)
        self.assertEqual(PR, built.pull_number)
        self.assertEqual(HEAD, built.head_sha)
        self.assertEqual(STATUS_CLAIMED, built.status)
        self.assertIsNone(built.run_id)
        self.assertEqual("", built.run_url)

        text = render_receipt(built)
        self.assertIn(RECEIPT_MARKER, text)
        parsed = parse_receipts(text)
        self.assertEqual((built,), parsed)
        self.assertEqual(
            ("kodmial/continuum", PR, HEAD), receipt_key(parsed[0])
        )

    def test_normalizes_repository_and_head_case(self):
        built = receipt(repository="Kodmial/CONTINUUM", head=HEAD.upper())
        self.assertEqual("kodmial/continuum", built.repository)
        self.assertEqual(HEAD, built.head_sha)
        self.assertEqual((built,), parse_receipts(render_receipt(built)))

    def test_dispatched_receipt_accepts_proved_run_details(self):
        built = receipt(
            status=STATUS_DISPATCHED,
            run_id=987654,
            run_url="https://github.com/kodmial/continuum/actions/runs/987654",
        )
        self.assertEqual(987654, built.run_id)
        self.assertEqual(
            "https://github.com/kodmial/continuum/actions/runs/987654",
            built.run_url,
        )
        text = receipt_marker_line(built)
        self.assertIn("run=987654", text)
        self.assertIn("url=https://github.com/kodmial/continuum", text)
        self.assertEqual((built,), parse_receipts(text))

    def test_dispatched_receipt_run_details_are_optional(self):
        built = receipt(status=STATUS_DISPATCHED)
        self.assertIsNone(built.run_id)
        self.assertEqual((built,), parse_receipts(render_receipt(built)))

    def test_run_details_are_rejected_for_unproved_statuses(self):
        for status in (STATUS_CLAIMED, STATUS_AMBIGUOUS, STATUS_REJECTED):
            with self.subTest(status=status):
                with self.assertRaises(ReceiptError):
                    receipt(status=status, run_id=1)
                with self.assertRaises(ReceiptError):
                    receipt(status=status, run_url="https://github.com/x/y")

    def test_run_url_must_prove_same_repository_and_run_id(self):
        with self.assertRaises(ReceiptError):
            receipt(
                status=STATUS_DISPATCHED,
                run_id=7,
                run_url="https://github.com/kodmial/other/actions/runs/7",
            )
        with self.assertRaises(ReceiptError):
            receipt(
                status=STATUS_DISPATCHED,
                run_id=7,
                run_url="https://github.com/kodmial/continuum/actions/runs/8",
            )
        with self.assertRaises(ReceiptError):
            receipt(
                status=STATUS_DISPATCHED,
                run_url="https://github.com/kodmial/continuum/actions/runs/7",
            )
        with self.assertRaises(ReceiptError):
            receipt(status=STATUS_DISPATCHED, run_id=0)

    def test_invalid_identity_and_status_are_rejected(self):
        cases = [
            {"repository": "not-a-repo"},
            {"pull_number": 0},
            {"pull_number": True},
            {"head": "a" * 39},
            {"head": "a" * 64},
            {"attempt": ""},
            {"attempt": "short"},
            {"attempt": "has space"},
            {"status": "accepted"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ReceiptError):
                    receipt(**overrides)

    def test_schema_version_mismatch_fails_closed(self):
        with self.assertRaises(ReceiptError):
            build_receipt(
                repository=REPO,
                pull_number=PR,
                head_sha=HEAD,
                attempt_id=ATTEMPT,
                status=STATUS_CLAIMED,
                schema_version=2,
            )
        with self.assertRaises(ReceiptError):
            build_receipt(
                repository=REPO,
                pull_number=PR,
                head_sha=HEAD,
                attempt_id=ATTEMPT,
                status=STATUS_CLAIMED,
                schema_version=True,
            )
        marker = (
            "<!-- continuum-ci-repair-receipt v=2 repo=kodmial/continuum "
            "pr=444 head={} attempt={} status=claimed -->"
        ).format(HEAD, ATTEMPT)
        with self.assertRaises(ReceiptError):
            parse_receipts(marker)
        result = read([comment(marker)])
        self.assertEqual(OUTCOME_INVALID, result.outcome)
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)

    def test_fractional_schema_version_is_rejected(self):
        for schema_version in (1.9, 0.5):
            with self.subTest(schema_version=schema_version):
                with self.assertRaises(ReceiptError):
                    build_receipt(
                        repository=REPO,
                        pull_number=PR,
                        head_sha=HEAD,
                        attempt_id=ATTEMPT,
                        status=STATUS_CLAIMED,
                        schema_version=schema_version,
                    )

    def test_marker_token_inside_attempt_id_is_not_ambiguous(self):
        # Attempt IDs are opaque; only tokens outside a complete marker are
        # malformed evidence.
        built = receipt(attempt=RECEIPT_MARKER)
        self.assertEqual((built,), parse_receipts(render_receipt(built)))

    def test_parse_returns_empty_without_a_marker_token(self):
        self.assertEqual((), parse_receipts("ordinary human discussion"))
        result = read([comment("ordinary human discussion")])
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual((), result.receipts)
        self.assertTrue(decide_new_attempt(result).allowed)

    def test_malformed_or_truncated_markers_are_rejected(self):
        valid = render_receipt(receipt())
        malformed = [
            valid.replace(" -->", ""),
            valid.replace("status=claimed", "status=claimed evil=1"),
            valid.replace("status=claimed", "status=Claimed"),
            "prose mentions continuum-ci-repair-receipt without a marker",
        ]
        for text in malformed:
            with self.subTest(text=text):
                with self.assertRaises(ReceiptError):
                    parse_receipts(text)

    def test_multiple_markers_in_one_body_parse_in_order(self):
        first = render_receipt(receipt(attempt=ATTEMPT))
        second = render_receipt(receipt(attempt=OTHER_ATTEMPT))
        parsed = parse_receipts(first + "\n" + second)
        self.assertEqual((ATTEMPT, OTHER_ATTEMPT), tuple(r.attempt_id for r in parsed))

    def test_new_attempt_id_is_valid_and_nonempty(self):
        generated = new_attempt_id()
        self.assertTrue(generated)
        self.assertEqual(generated, normalize_attempt_id(generated))


class TrustTests(unittest.TestCase):
    def test_trusted_bot_and_owner_comments_are_accepted(self):
        bot = receipt_comment()
        owner = comment(
            render_receipt(receipt(attempt=OTHER_ATTEMPT)),
            login="kodmial",
            association="OWNER",
        )
        for comments in ([bot], [owner]):
            with self.subTest(comments=comments):
                result = read(comments)
                self.assertEqual(OUTCOME_COMPLETE, result.outcome)
                self.assertEqual(1, len(result.attempts))

        plain_login = comment(
            render_receipt(receipt(attempt=OTHER_ATTEMPT)),
            login="Kodmial",
            association="NONE",
        )
        result = read([plain_login], owner="kodmial")
        self.assertEqual(1, len(result.attempts))

    def test_untrusted_author_marker_is_ignored(self):
        forged = comment(
            render_receipt(receipt(status=STATUS_DISPATCHED, run_id=1)),
            login="random-fork-user",
            association="NONE",
        )
        result = read([forged])
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual((), result.receipts)
        self.assertEqual(1, result.ignored_untrusted)
        decision = decide_new_attempt(result)
        self.assertTrue(decision.allowed)
        self.assertEqual(ACTION_CLAIM, decision.action)

    def test_member_and_collaborator_markers_are_untrusted_by_default(self):
        for association in ("MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                forged = comment(
                    render_receipt(receipt(status=STATUS_DISPATCHED, run_id=1)),
                    login="read-only-org-user",
                    association=association,
                )
                result = read([forged])
                self.assertEqual(OUTCOME_COMPLETE, result.outcome)
                self.assertEqual((), result.receipts)
                self.assertEqual(1, result.ignored_untrusted)
                self.assertTrue(decide_new_attempt(result).allowed)

    def test_marker_with_unknown_authorship_is_incomplete(self):
        unknown = receipt_comment()
        del unknown["user"]
        del unknown["author_association"]
        result = read([unknown])
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("unknown authorship", result.reason)
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)

    def test_partial_untrusted_author_metadata_fails_closed(self):
        # Without a login the commenter might be github-actions[bot];
        # without an association the commenter might be OWNER.
        for missing_field in ("user", "author_association"):
            with self.subTest(missing_field=missing_field):
                forged = comment(
                    render_receipt(
                        receipt(status=STATUS_DISPATCHED, run_id=1)
                    ),
                    login="untrusted-reader",
                    association="NONE",
                )
                del forged[missing_field]
                result = read([forged])
                self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
                self.assertIn("unknown authorship", result.reason)
                decision = decide_new_attempt(result)
                self.assertFalse(decision.allowed)
                self.assertEqual(ACTION_HOLD, decision.action)

    def test_malformed_author_trust_sources_fail_closed(self):
        for field, value in (
            ("user_login", 123),
            ("user_login", {"name": "not-a-login"}),
            ("association", 42),
        ):
            with self.subTest(field=field, value=value):
                marked = comment(
                    render_receipt(receipt()),
                    login="untrusted-reader",
                    association="NONE",
                )
                if field == "user_login":
                    marked["user"]["login"] = value
                else:
                    marked["author_association"] = value
                result = read([marked])
                self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
                self.assertEqual(ACTION_HOLD, decide_new_attempt(result).action)
                self.assertFalse(decide_new_attempt(result).allowed)

        # A malformed primary login must not fall back to a secondary string.
        malformed = comment(render_receipt(receipt()), login=123)
        malformed["author_login"] = "untrusted-reader"
        self.assertEqual(OUTCOME_INCOMPLETE, read([malformed]).outcome)

    def test_positive_trust_with_one_author_source_remains_valid(self):
        trusted_bot = receipt_comment()
        del trusted_bot["author_association"]
        trusted_owner = comment(
            render_receipt(receipt()),
            login="anything",
            association="OWNER",
        )
        del trusted_owner["user"]
        for marked in (trusted_bot, trusted_owner):
            with self.subTest(marked=marked):
                result = read([marked])
                self.assertEqual(OUTCOME_COMPLETE, result.outcome)
                self.assertEqual(1, len(result.attempts))

    def test_untrusted_mixed_marker_cannot_block_trusted_history(self):
        forged = comment(
            render_receipt(
                receipt(repository="attacker/other"),
            ),
            login="random-fork-user",
            association="NONE",
        )
        result = read([forged])
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertTrue(decide_new_attempt(result).allowed)

    def test_trusted_marker_for_another_repository_or_pr_is_mixed(self):
        other_repo = receipt_comment(repository="other/repository")
        other_pr = receipt_comment(pull_number=PR + 1)
        for comments in ([other_repo], [other_pr]):
            with self.subTest(comments=comments):
                result = read(comments)
                self.assertEqual(OUTCOME_INVALID, result.outcome)
                self.assertIn("mixed repository/PR", result.reason)
                self.assertFalse(decide_new_attempt(result).allowed)


class HeadIndependenceTests(unittest.TestCase):
    def test_new_head_is_an_independent_attempt_key(self):
        comments = [
            receipt_comment(head=HEAD, attempt=ATTEMPT),
            receipt_comment(head=OTHER_HEAD, attempt=OTHER_ATTEMPT),
        ]
        old = read(comments, head=HEAD)
        new = read(comments, head=OTHER_HEAD)
        self.assertEqual(OUTCOME_COMPLETE, old.outcome)
        self.assertEqual(OUTCOME_COMPLETE, new.outcome)
        self.assertEqual((ATTEMPT,), tuple(a.attempt_id for a in old.attempts))
        self.assertEqual((OTHER_ATTEMPT,), tuple(a.attempt_id for a in new.attempts))
        # Both receipt keys remain visible; reading one never rewrites the other.
        self.assertEqual(2, len(old.all_receipts))
        self.assertEqual(2, len(new.all_receipts))

    def test_ambiguous_old_head_does_not_block_a_new_head(self):
        comments = [
            comment(
                render_receipt(
                    receipt(head=HEAD, attempt=ATTEMPT, status=STATUS_AMBIGUOUS)
                )
            )
        ]
        old = read(comments, head=HEAD)
        new = read(comments, head=OTHER_HEAD)
        self.assertEqual(ACTION_HOLD, decide_new_attempt(old).action)
        self.assertFalse(decide_new_attempt(old).allowed)
        self.assertEqual(ACTION_CLAIM, decide_new_attempt(new).action)
        self.assertTrue(decide_new_attempt(new).allowed)
        self.assertEqual(1, len(new.all_receipts))

    def test_attempt_id_reuse_across_heads_is_invalid(self):
        comments = [
            receipt_comment(head=HEAD, attempt=ATTEMPT),
            receipt_comment(head=OTHER_HEAD, attempt=ATTEMPT),
        ]
        result = read(comments, head=OTHER_HEAD)
        self.assertEqual(OUTCOME_INVALID, result.outcome)
        self.assertIn("reused across exact HEADs", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)


class HistoryCompletenessTests(unittest.TestCase):
    def test_complete_empty_history_can_claim(self):
        result = read([])
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual((), result.attempts)
        decision = decide_new_attempt(result)
        self.assertTrue(decision.allowed)
        self.assertEqual(ACTION_CLAIM, decision.action)

    def test_unattested_history_is_incomplete_not_permissive(self):
        result = read([], complete=False, ordered=True)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)

    def test_unavailable_history_is_incomplete_not_empty(self):
        result = read(None)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("unavailable", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_unordered_history_is_incomplete(self):
        result = read([], complete=True, ordered=False)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("ordering", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_expected_length_mismatch_is_incomplete(self):
        result = read([receipt_comment()], expected=2)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("length", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_boolean_expected_length_is_incomplete(self):
        result = read([receipt_comment()], expected=True)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("not an integer", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_fractional_expected_length_is_incomplete(self):
        result = read([receipt_comment()], expected=1.9)
        self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
        self.assertIn("not an integer", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_comment_without_string_body_is_incomplete(self):
        missing = comment(render_receipt(receipt()))
        del missing["body"]
        non_string = comment(render_receipt(receipt()))
        non_string["body"] = None
        for comments in ([missing], [non_string]):
            with self.subTest(comments=comments):
                result = read(comments)
                self.assertEqual(OUTCOME_INCOMPLETE, result.outcome)
                self.assertIn("string body", result.reason)
                decision = decide_new_attempt(result)
                self.assertFalse(decision.allowed)
                self.assertEqual(ACTION_HOLD, decision.action)

    def test_expected_length_match_is_complete(self):
        result = read([receipt_comment()], expected=1)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(1, len(result.attempts))

    def test_incomplete_history_is_never_treated_as_no_receipt(self):
        result = read(
            [receipt_comment(status=STATUS_AMBIGUOUS)],
            complete=False,
        )
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertIn("incomplete", decision.reason)


class AttemptDecisionTests(unittest.TestCase):
    def test_claim_blocks_a_second_attempt(self):
        result = read([receipt_comment()])
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_WAIT, decision.action)
        self.assertEqual(ATTEMPT, decision.attempt_id)

    def test_two_same_head_claim_attempts_conflict(self):
        result = read(
            [
                receipt_comment(attempt=ATTEMPT),
                receipt_comment(attempt=OTHER_ATTEMPT),
            ]
        )
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn("multiple non-rejected attempts", result.reason)
        self.assertEqual(2, len(result.attempts))
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)

    def test_accepted_run_id_receipt_is_recorded_and_blocks(self):
        run_id = 123456
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(
                attempt=ATTEMPT,
                status=STATUS_DISPATCHED,
                run_id=run_id,
                run_url="https://github.com/kodmial/continuum/actions/runs/{}".format(
                    run_id
                ),
            ),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(1, len(result.attempts))
        attempt = result.attempts[0]
        self.assertEqual(STATUS_DISPATCHED, attempt.status)
        self.assertEqual(run_id, attempt.run_id)
        self.assertIn(str(run_id), attempt.run_url)
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_WAIT, decision.action)
        self.assertEqual(ATTEMPT, decision.attempt_id)

    def test_ambiguous_dispatch_cannot_authorize_a_second_dispatch(self):
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_AMBIGUOUS),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(STATUS_AMBIGUOUS, result.attempts[0].status)
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)
        self.assertIn("ambiguous", decision.reason)
        self.assertEqual(ATTEMPT, decision.attempt_id)

    def test_ambiguous_may_be_reconciled_to_a_proved_run(self):
        run_id = 7654321
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_AMBIGUOUS),
            receipt_comment(
                attempt=ATTEMPT,
                status=STATUS_DISPATCHED,
                run_id=run_id,
                run_url="https://github.com/kodmial/continuum/actions/runs/{}".format(
                    run_id
                ),
            ),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(STATUS_DISPATCHED, result.attempts[0].status)
        self.assertEqual(run_id, result.attempts[0].run_id)
        self.assertEqual(3, result.attempts[0].receipt_count)
        self.assertEqual(ACTION_WAIT, decide_new_attempt(result).action)

    def test_rejected_attempt_may_rearm_with_a_new_id(self):
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_REJECTED),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(STATUS_REJECTED, result.attempts[0].status)
        decision = decide_new_attempt(result)
        self.assertTrue(decision.allowed)
        self.assertEqual(ACTION_CLAIM, decision.action)
        self.assertIn("rejected", decision.reason)

    def test_duplicate_identical_receipts_are_idempotent(self):
        body = render_receipt(receipt())
        result = read([comment(body), comment(body)])
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(1, len(result.attempts))
        self.assertEqual(2, result.attempts[0].receipt_count)
        self.assertEqual(ACTION_WAIT, decide_new_attempt(result).action)

    def test_dispatched_run_details_may_be_enriched(self):
        run_id = 99
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED),
            receipt_comment(
                attempt=ATTEMPT,
                status=STATUS_DISPATCHED,
                run_id=run_id,
                run_url="https://github.com/kodmial/continuum/actions/runs/{}".format(
                    run_id
                ),
            ),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_COMPLETE, result.outcome)
        self.assertEqual(run_id, result.attempts[0].run_id)

    def test_conflicting_run_ids_for_one_attempt_conflict(self):
        first = receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED, run_id=1)
        second = receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED, run_id=2)
        result = read([receipt_comment(attempt=ATTEMPT), first, second])
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn("conflicting run IDs", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_dispatched_receipt_cannot_lose_proved_run_details(self):
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED, run_id=1),
            receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn("must not lose", result.reason)

    def test_attempt_lookup_and_transition_helpers(self):
        result = read([receipt_comment()])
        attempt = attempt_for_id(result, ATTEMPT)
        self.assertIsNotNone(attempt)
        self.assertIsNone(attempt_for_id(result, OTHER_ATTEMPT))
        dispatched = transition_attempt(attempt, status=STATUS_DISPATCHED, run_id=5)
        self.assertEqual(STATUS_DISPATCHED, dispatched.status)
        self.assertEqual(5, dispatched.run_id)
        self.assertEqual(ATTEMPT, dispatched.attempt_id)
        self.assertEqual(HEAD, dispatched.head_sha)


class FailClosedTests(unittest.TestCase):
    def test_unknown_collection_outcome_never_permits_a_claim(self):
        result = ReceiptCollection(outcome="unknown", reason="future outcome")
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)
        self.assertIn("failing closed", decision.reason)

    def test_unknown_attempt_status_never_permits_a_claim(self):
        valid = receipt(attempt="attempt-9999")
        attempt = AttemptSummary(
            attempt_id="attempt-9999",
            status="future-status",
            run_id=None,
            run_url="",
            receipt_count=1,
            latest_receipt=valid,
        )
        result = ReceiptCollection(
            outcome=OUTCOME_COMPLETE,
            reason="synthetic",
            attempts=(attempt,),
        )
        decision = decide_new_attempt(result)
        self.assertFalse(decision.allowed)
        self.assertEqual(ACTION_HOLD, decision.action)
        self.assertIn("unknown attempt status", decision.reason)

    def test_impossible_transition_on_an_older_head_is_a_conflict(self):
        comments = [
            receipt_comment(head=OTHER_HEAD, attempt=ATTEMPT),
            receipt_comment(
                head=OTHER_HEAD, attempt=ATTEMPT, status=STATUS_DISPATCHED
            ),
            receipt_comment(head=OTHER_HEAD, attempt=ATTEMPT),
        ]
        result = read(comments, head=HEAD)
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn("impossible receipt transition", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)

    def test_duplicate_live_attempts_on_an_older_head_are_a_conflict(self):
        comments = [
            receipt_comment(head=OTHER_HEAD, attempt=ATTEMPT),
            receipt_comment(head=OTHER_HEAD, attempt=OTHER_ATTEMPT),
        ]
        result = read(comments, head=HEAD)
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn(OTHER_HEAD, result.reason)
        self.assertIn("multiple non-rejected attempts", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)


class TransitionTests(unittest.TestCase):
    def test_claimed_to_dispatch_states_is_allowed(self):
        claimed = receipt()
        for status in (STATUS_DISPATCHED, STATUS_AMBIGUOUS, STATUS_REJECTED):
            with self.subTest(status=status):
                following = transition_receipt(claimed, status=status)
                self.assertEqual(status, following.status)

    def test_ambiguous_to_dispatched_is_allowed_only_with_proof(self):
        ambiguous = transition_receipt(
            receipt(), status=STATUS_AMBIGUOUS
        )
        reconciled = transition_receipt(
            ambiguous,
            status=STATUS_DISPATCHED,
            run_id=12,
            run_url="https://github.com/kodmial/continuum/actions/runs/12",
        )
        self.assertEqual(STATUS_DISPATCHED, reconciled.status)
        self.assertEqual(12, reconciled.run_id)

    def test_impossible_transitions_are_rejected(self):
        claimed = receipt()
        dispatched = transition_receipt(claimed, status=STATUS_DISPATCHED)
        ambiguous = transition_receipt(claimed, status=STATUS_AMBIGUOUS)
        rejected = transition_receipt(claimed, status=STATUS_REJECTED)
        cases = [
            (dispatched, STATUS_CLAIMED),
            (dispatched, STATUS_AMBIGUOUS),
            (dispatched, STATUS_REJECTED),
            (ambiguous, STATUS_CLAIMED),
            (ambiguous, STATUS_REJECTED),
            (rejected, STATUS_CLAIMED),
            (rejected, STATUS_DISPATCHED),
        ]
        for previous, next_status in cases:
            with self.subTest(previous=previous.status, next=next_status):
                with self.assertRaises(ReceiptError):
                    transition_receipt(previous, status=next_status)

    def test_impossible_transition_in_history_is_a_conflict(self):
        comments = [
            receipt_comment(attempt=ATTEMPT),
            receipt_comment(attempt=ATTEMPT, status=STATUS_DISPATCHED),
            receipt_comment(attempt=ATTEMPT, status=STATUS_CLAIMED),
        ]
        result = read(comments)
        self.assertEqual(OUTCOME_CONFLICT, result.outcome)
        self.assertIn("impossible receipt transition", result.reason)
        self.assertFalse(decide_new_attempt(result).allowed)


if __name__ == "__main__":
    unittest.main()
