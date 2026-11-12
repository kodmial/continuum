"""Production admission tests for exact-HEAD CI repair (#446).

These tests exercise the pure admission layer that the live repair controller
runs: complete comment pagination/ordering, fail-closed receipt reads, dispatch
classification, actual-run correlation, and evidence-backed bounded recovery.

The concurrency group is what serializes read -> claim -> dispatch. The tests
therefore also assert that a receipt is the only admission authority: label
state, a cached PR payload, or a "latest run" guess are never consulted.
"""

from __future__ import annotations

import unittest

from continuum.ci_repair_admission import (
    ACTION_REARM,
    DISPATCH_ACCEPTED,
    DISPATCH_AMBIGUOUS,
    DISPATCH_REJECTED,
    AdmissionError,
    AdmissionPlan,
    RunInventory,
    attempt_observed_at,
    classify_dispatch_response,
    collection_from_pages,
    correlated_run_url,
    has_unattributed_repair_run,
    normalize_comment_pages,
    normalize_run_pages,
    plan_admission,
    run_marker,
    run_proves_dispatched_receipt,
    run_proves_historical_receipt,
    select_correlated_run,
    timestamp_is_stale,
    unexpected_dispatch_inputs,
)
from continuum.ci_repair_receipt import (
    ACTION_CLAIM,
    ACTION_HOLD,
    ACTION_WAIT,
    STATUS_AMBIGUOUS,
    STATUS_CLAIMED,
    STATUS_DISPATCHED,
    STATUS_REJECTED,
    build_receipt,
    read_receipts,
    render_receipt,
    transition_receipt,
)

REPO = "Kodmial/Continuum"
PR = 446
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
ATTEMPT = "ci-attempt-0001"
OTHER_ATTEMPT = "ci-attempt-0002"

TRUSTED = {"login": "github-actions[bot]", "association": "NONE"}


def comment(
    body,
    *,
    author=TRUSTED,
    comment_id=1,
    created_at="2026-11-12T00:00:00Z",
):
    return {
        "body": body,
        "author_association": author["association"],
        "user": {"login": author["login"]},
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


def receipt_comment(
    *,
    comment_id=1,
    created_at="2026-11-12T00:00:00Z",
    **kwargs
):
    return comment(
        render_receipt(receipt(**kwargs)),
        comment_id=comment_id,
        created_at=created_at,
    )


def read(comments, *, head=HEAD, expected=None):
    return collection_from_pages(
        [comments],
        repository=REPO,
        pull_number=PR,
        head_sha=head,
        expected_count=expected,
    )[1]


def run(
    run_id,
    *,
    attempt=None,
    pull_number=PR,
    created_at="2026-11-12T00:00:10Z",
    title=None,
    status="queued",
    conclusion="",
):
    display = title
    if display is None:
        display = "OpenCode repair PR #{}".format(pull_number)
        if attempt:
            display += " [ci-attempt:{}]".format(attempt)
    return {
        "id": run_id,
        "display_title": display,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://github.com/{}/actions/runs/{}".format(REPO.lower(), run_id),
        "created_at": created_at,
        "head_sha": HEAD,
    }


def inventory(*runs, complete=True, reason="complete", expected=None):
    return RunInventory(tuple(runs), complete, reason, expected)


def check_inventory(*runs):
    return normalize_run_pages(
        [{"total_count": len(runs), "workflow_runs": list(runs)}]
    )


# ---------------------------------------------------------------------------
# Comment pagination and ordering.
# ---------------------------------------------------------------------------

class CommentHistoryTests(unittest.TestCase):
    def test_pages_are_flattened_and_deterministically_ordered(self):
        history = normalize_comment_pages(
            [
                [comment("second", comment_id=2, created_at="2026-11-12T00:00:02Z")],
                [comment("first", comment_id=1, created_at="2026-11-12T00:00:01Z")],
            ]
        )
        self.assertTrue(history.complete)
        self.assertEqual(["first", "second"], [entry["body"] for entry in history.comments])

    def test_single_page_array_is_accepted(self):
        history = normalize_comment_pages([comment("one")])
        self.assertTrue(history.complete)
        self.assertEqual(1, len(history.comments))

    def test_unavailable_malformed_and_duplicate_pages_fail_closed(self):
        cases = [
            None,
            {"body": "object"},
            "not-pages",
            [[{"body": None, "id": 1}]],
            [[{"body": "x", "id": 0}]],
            [[{"body": "x", "id": "nan"}]],
            [[{"body": "x", "id": 1}], [{"body": "y", "id": 1}]],
            [[{"body": "x", "id": 1}], "not-a-page"],
            [[{"body": "x", "id": 1}], [None]],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                history = normalize_comment_pages(pages)
                self.assertFalse(history.complete)
                self.assertFalse(history.ordered)
                self.assertTrue(history.reason)

    def test_short_scan_against_the_independent_comment_count_is_incomplete(self):
        history = normalize_comment_pages(
            [[comment("one", comment_id=1)]], expected_count=2
        )
        self.assertFalse(history.complete)
        self.assertIn("incomplete", history.reason)

    def test_concurrent_new_comments_do_not_make_a_complete_scan_incomplete(self):
        history = normalize_comment_pages(
            [[comment("one", comment_id=1)]], expected_count=0
        )
        self.assertTrue(history.complete)

    def test_incomplete_history_never_authorizes_a_claim(self):
        collection = collection_from_pages(
            [[comment("body without receipts", comment_id=1)]],
            repository=REPO,
            pull_number=PR,
            head_sha=HEAD,
            expected_count=2,
        )[1]
        self.assertEqual(ACTION_HOLD, plan_admission(collection).action)

    def test_missing_or_malformed_creation_times_fail_closed(self):
        valid = comment("body", comment_id=1)
        cases = (
            {key: value for key, value in valid.items() if key != "created_at"},
            {**valid, "created_at": ""},
            {**valid, "created_at": "2026-11-12 00:00:00"},
            {**valid, "created_at": "not-a-time"},
        )
        for case in cases:
            with self.subTest(case=case.get("created_at", "<missing>")):
                history = normalize_comment_pages([case])
                self.assertFalse(history.complete)
                self.assertFalse(history.ordered)
                self.assertIn("creation timestamp", history.reason)


# ---------------------------------------------------------------------------
# Dispatch classification.
# ---------------------------------------------------------------------------

class DispatchClassificationTests(unittest.TestCase):
    def setUp(self):
        self.claimed = receipt()

    def test_200_with_run_details_proves_the_dispatch(self):
        outcome = classify_dispatch_response(
            self.claimed,
            http_status=200,
            response_body={"workflow_run_id": 99},
        )
        self.assertEqual(DISPATCH_ACCEPTED, outcome.kind)
        self.assertTrue(outcome.accepted)
        self.assertEqual(STATUS_DISPATCHED, outcome.receipt.status)
        self.assertEqual(99, outcome.receipt.run_id)
        self.assertEqual(
            "https://github.com/kodmial/continuum/actions/runs/99",
            outcome.receipt.run_url,
        )

    def test_json_string_bodies_and_run_id_aliases_are_accepted(self):
        outcome = classify_dispatch_response(
            self.claimed, http_status=200, response_body='{"run_id": 7}'
        )
        self.assertEqual(7, outcome.receipt.run_id)

    def test_legacy_204_is_accepted_without_run_details(self):
        outcome = classify_dispatch_response(self.claimed, http_status=204)
        self.assertEqual(DISPATCH_ACCEPTED, outcome.kind)
        self.assertIsNone(outcome.receipt.run_id)
        self.assertEqual(STATUS_DISPATCHED, outcome.receipt.status)

    def test_definite_client_rejections_are_terminal_for_that_attempt(self):
        for status in (400, 401, 404, 409, 410, 422):
            with self.subTest(status=status):
                outcome = classify_dispatch_response(self.claimed, http_status=status)
                self.assertEqual(DISPATCH_REJECTED, outcome.kind)
                self.assertTrue(outcome.rejected)
                self.assertEqual(STATUS_REJECTED, outcome.receipt.status)

    def test_rate_limit_server_and_network_outcomes_are_ambiguous(self):
        for status in (403, 429, 500, 502, 503):
            with self.subTest(status=status):
                outcome = classify_dispatch_response(self.claimed, http_status=status)
                self.assertEqual(DISPATCH_AMBIGUOUS, outcome.kind)
                self.assertTrue(outcome.ambiguous)
                self.assertEqual(STATUS_AMBIGUOUS, outcome.receipt.status)

        for kwargs in ({"network_error": True}, {"http_status": None}):
            with self.subTest(kwargs=kwargs):
                outcome = classify_dispatch_response(self.claimed, **kwargs)
                self.assertEqual(DISPATCH_AMBIGUOUS, outcome.kind)
                self.assertEqual(STATUS_AMBIGUOUS, outcome.receipt.status)

    def test_only_a_claimed_or_ambiguous_receipt_may_be_classified(self):
        dispatched = transition_receipt(self.claimed, status=STATUS_DISPATCHED)
        with self.assertRaises(AdmissionError):
            classify_dispatch_response(dispatched, http_status=204)

    def test_unexpected_input_422_is_detected_without_matching_other_422s(self):
        self.assertTrue(unexpected_dispatch_inputs("Unexpected inputs provided: ci_attempt_id"))
        self.assertFalse(unexpected_dispatch_inputs("Validation Failed: ref is required"))


# ---------------------------------------------------------------------------
# Run inventory and correlation.
# ---------------------------------------------------------------------------

class RunCorrelationTests(unittest.TestCase):
    def test_marker_matches_only_the_exact_attempt_and_pr(self):
        payload = {
            "workflow_runs": [
                run(1, attempt=ATTEMPT),
                run(2, attempt=OTHER_ATTEMPT),
                run(3, attempt=ATTEMPT, pull_number=999),
                run(4, title="OpenCode repair PR #446"),
            ]
        }
        selected = select_correlated_run(payload, attempt_id=ATTEMPT, pull_number=PR)
        self.assertEqual(1, selected["id"])

    def test_multiple_runs_with_the_same_marker_fail_closed(self):
        payload = {"workflow_runs": [run(1, attempt=ATTEMPT), run(2, attempt=ATTEMPT)]}
        with self.assertRaises(AdmissionError):
            select_correlated_run(payload, attempt_id=ATTEMPT, pull_number=PR)

    def test_a_run_without_the_marker_is_never_correlated_by_recency(self):
        payload = {"workflow_runs": [run(1, title="OpenCode repair PR #446")]}
        self.assertIsNone(
            select_correlated_run(payload, attempt_id=ATTEMPT, pull_number=PR)
        )

    def test_malformed_inventory_and_attempt_ids_are_rejected(self):
        with self.assertRaises(AdmissionError):
            select_correlated_run({"workflow_runs": "nope"}, attempt_id=ATTEMPT)
        with self.assertRaises(AdmissionError):
            select_correlated_run({"workflow_runs": []}, attempt_id="")

    def test_run_marker_is_strict(self):
        self.assertEqual("[ci-attempt:ci-attempt-0001]", run_marker(ATTEMPT))
        for invalid in ("", "short", "bad]", "has space"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AdmissionError):
                    run_marker(invalid)

    def test_canonical_run_url_normalizes_the_repository(self):
        self.assertEqual(
            "https://github.com/kodmial/continuum/actions/runs/12",
            correlated_run_url(REPO, {"id": 12}),
        )
        with self.assertRaises(AdmissionError):
            correlated_run_url(REPO, {"id": 0})

    def test_run_pagination_is_validated_like_comment_pagination(self):
        complete = normalize_run_pages(
            [
                {"total_count": 2, "workflow_runs": [run(2, created_at="2026-11-12T00:00:02Z")]},
                {"total_count": 2, "workflow_runs": [run(1, created_at="2026-11-12T00:00:01Z")]},
            ]
        )
        self.assertTrue(complete.complete)
        self.assertEqual([1, 2], [entry["id"] for entry in complete.runs])

        short = normalize_run_pages(
            [{"total_count": 2, "workflow_runs": [run(1)]}]
        )
        self.assertFalse(short.complete)

        duplicate = normalize_run_pages(
            [
                {"total_count": 2, "workflow_runs": [run(1)]},
                {"total_count": 2, "workflow_runs": [run(1)]},
            ]
        )
        self.assertFalse(duplicate.complete)

        for pages in (None, "nope", [{"workflow_runs": "nope"}], [{"workflow_runs": [{}]}]):
            with self.subTest(pages=pages):
                self.assertFalse(normalize_run_pages(pages).complete)

    def test_legacy_shaped_run_is_detected_only_after_the_claim(self):
        runs = check_inventory(
            run(1, created_at="2026-11-12T00:00:30Z"),
            run(2, attempt=ATTEMPT, created_at="2026-11-12T00:00:31Z"),
        )
        self.assertTrue(
            has_unattributed_repair_run(
                runs,
                pull_number=PR,
                since="2026-11-12T00:00:00Z",
            )
        )
        self.assertFalse(
            has_unattributed_repair_run(
                runs,
                pull_number=PR,
                since="2026-11-12T00:01:00Z",
            )
        )
        self.assertFalse(has_unattributed_repair_run(runs, pull_number=999))
        self.assertFalse(has_unattributed_repair_run(check_inventory(run(3, attempt=ATTEMPT))))

    def test_legacy_run_with_an_unknown_creation_time_holds(self):
        # An unmarked repair run that cannot be proved older than the claim is
        # unattributable evidence, not proof that no run exists.
        for created in ("", "not-a-time"):
            with self.subTest(created=created):
                runs = check_inventory(run(1, created_at=created))
                self.assertTrue(
                    has_unattributed_repair_run(
                        runs,
                        pull_number=PR,
                        since="2026-11-12T00:00:00Z",
                    )
                )
                # Without a claim threshold the legacy run is still visible.
                self.assertTrue(has_unattributed_repair_run(runs, pull_number=PR))

    def test_a_run_without_a_usable_title_makes_the_inventory_incomplete(self):
        # Correlation reads the attempt marker from the run title. A run that
        # carries no usable title cannot be excluded as the accepted repair,
        # so the inventory is not proof that no run exists.
        for title in (None, "", "   ", 7):
            with self.subTest(title=title):
                entry = run(9)
                if title is None:
                    entry.pop("display_title")
                else:
                    entry["display_title"] = title
                inventory = normalize_run_pages(
                    [{"total_count": 1, "workflow_runs": [entry]}]
                )
                self.assertFalse(inventory.complete)
                self.assertIn("display title", inventory.reason)


class VerifiedHistoricalWriterTests(unittest.TestCase):
    """A rotated PAT account keeps only GitHub-attested dispatch evidence."""

    def dispatched(self, *, attempt=ATTEMPT, run_id=900):
        return build_receipt(
            repository=REPO,
            pull_number=PR,
            head_sha=HEAD,
            attempt_id=attempt,
            status=STATUS_DISPATCHED,
            run_id=run_id,
        )

    @staticmethod
    def actual_run(*, attempt=ATTEMPT, actor="old-automation", head=None, run_id=900):
        return {
            "id": run_id,
            "display_title": "OpenCode repair PR #{} [ci-attempt:{}]".format(
                PR, attempt
            ),
            "head_sha": head or HEAD,
            "actor": {"login": actor},
        }

    def test_a_same_head_run_created_by_the_author_proves_the_receipt(self):
        self.assertTrue(
            run_proves_dispatched_receipt(
                self.actual_run(),
                self.dispatched(),
                author_login="Old-Automation",
            )
        )

    def test_a_legacy_unmarked_repair_title_still_proves_the_receipt(self):
        self.assertTrue(
            run_proves_dispatched_receipt(
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{}".format(PR),
                    "head_sha": HEAD,
                    "triggering_actor": {"login": "old-automation"},
                },
                self.dispatched(),
                author_login="old-automation",
            )
        )

    def test_claimed_ambiguous_and_unrun_dispatched_receipts_are_proved_by_marker(self):
        # Outcome persistence can fail after an accepted dispatch, leaving only
        # the claim or a run-detail-less dispatched record. The actual run's
        # attempt marker still proves that historical writer.
        statuses = (
            STATUS_CLAIMED,
            STATUS_AMBIGUOUS,
            STATUS_DISPATCHED,
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertTrue(
                    run_proves_historical_receipt(
                        self.actual_run(),
                        receipt(status=status),
                        author_login="old-automation",
                    )
                )

    def test_a_marked_run_for_the_dispatch_ref_proves_the_receipt(self):
        # ``workflow_dispatch`` records the dispatch ref (normally ``main``) as
        # the run SHA, not the admitted PR HEAD. The attempt marker and actor
        # still prove the historical writer.
        main_sha = "c" * 40
        for status in (STATUS_CLAIMED, STATUS_AMBIGUOUS, STATUS_DISPATCHED):
            with self.subTest(status=status):
                self.assertTrue(
                    run_proves_historical_receipt(
                        self.actual_run(head=main_sha),
                        receipt(status=status),
                        author_login="old-automation",
                    )
                )

    def test_an_unmarked_dispatch_ref_run_cannot_prove_a_different_head(self):
        # A legacy unmarked title has no attempt binding, so a run for the
        # dispatch ref cannot prove which PR HEAD its receipt spent.
        main_sha = "c" * 40
        self.assertFalse(
            run_proves_historical_receipt(
                {
                    "id": 900,
                    "display_title": "OpenCode repair PR #{}".format(PR),
                    "head_sha": main_sha,
                    "actor": {"login": "old-automation"},
                },
                receipt(status=STATUS_CLAIMED),
                author_login="old-automation",
            )
        )

    def test_a_legacy_title_proves_a_receipt_without_a_run_id(self):
        # A legacy caller has no attempt marker, so an accepted 204 may leave
        # only a claimed, ambiguous, or run-detail-less dispatched receipt.
        # The canonical title is still bound to the same actor, exact HEAD,
        # and PR, so token rotation must not turn it into an ignored lookalike.
        run = {
            "id": 900,
            "display_title": "OpenCode repair PR #{}".format(PR),
            "head_sha": HEAD,
            "actor": {"login": "old-automation"},
        }
        for status in (STATUS_CLAIMED, STATUS_AMBIGUOUS, STATUS_DISPATCHED):
            with self.subTest(status=status):
                self.assertTrue(
                    run_proves_historical_receipt(
                        run,
                        receipt(status=status),
                        author_login="old-automation",
                    )
                )
        self.assertTrue(
            run_proves_historical_receipt(
                run, self.dispatched(), author_login="old-automation"
            )
        )

    def test_a_legacy_title_for_another_pr_or_actor_never_proves_a_receipt(self):
        run = {
            "id": 900,
            "display_title": "OpenCode repair PR #{}".format(PR),
            "head_sha": HEAD,
            "actor": {"login": "old-automation"},
        }
        wrong_pr = dict(
            run, display_title="OpenCode repair PR #{}".format(PR + 1)
        )
        wrong_actor = dict(run, actor={"login": "somebody-else"})
        for name, candidate in (
            ("another PR", wrong_pr),
            ("another actor", wrong_actor),
        ):
            with self.subTest(case=name):
                self.assertFalse(
                    run_proves_historical_receipt(
                        candidate,
                        receipt(status=STATUS_DISPATCHED),
                        author_login="old-automation",
                    )
                )

    def test_rejected_receipts_never_extend_historical_trust(self):
        self.assertFalse(
            run_proves_historical_receipt(
                self.actual_run(),
                receipt(status=STATUS_REJECTED),
                author_login="old-automation",
            )
        )

    def test_foreign_or_mismatched_runs_never_prove_a_receipt(self):
        cases = {
            "another run id": self.actual_run(run_id=901),
            "another exact HEAD": self.actual_run(head=OTHER_HEAD),
            "another attempt marker": self.actual_run(attempt=OTHER_ATTEMPT),
            "another actor": self.actual_run(actor="somebody-else"),
            "no actor": {"id": 900, "head_sha": HEAD,
                         "display_title": "OpenCode repair PR #{}".format(PR)},
        }
        for name, run in cases.items():
            with self.subTest(case=name):
                self.assertFalse(
                    run_proves_dispatched_receipt(
                        run, self.dispatched(), author_login="old-automation"
                    )
                )
        # A receipt without a proved run, or with no author to attest, never
        # becomes authoritative.
        self.assertFalse(
            run_proves_dispatched_receipt(
                self.actual_run(), receipt(), author_login="old-automation"
            )
        )
        self.assertFalse(
            run_proves_dispatched_receipt(self.actual_run(), self.dispatched())
        )


# ---------------------------------------------------------------------------
# Admission planning, including crash recovery.
# ---------------------------------------------------------------------------

class AdmissionPlanningTests(unittest.TestCase):
    def test_empty_complete_history_claims(self):
        plan = plan_admission(read([]))
        self.assertEqual(ACTION_CLAIM, plan.action)

    def test_incomplete_or_untrusted_history_holds(self):
        self.assertEqual(ACTION_HOLD, plan_admission(read([], expected=1)).action)

        # A lookalike with provable untrusted authorship is ignored; it can
        # neither spoof nor block the authoritative history.
        untrusted = comment(
            render_receipt(receipt()),
            author={"login": "random", "association": "CONTRIBUTOR"},
        )
        self.assertEqual(ACTION_CLAIM, plan_admission(read([untrusted])).action)

        # Marked text without enough author metadata is unknown, not untrusted.
        unknown = {"body": render_receipt(receipt()), "id": 1, "created_at": "2026-11-12T00:00:00Z"}
        self.assertEqual(ACTION_HOLD, plan_admission(read([unknown])).action)

    def test_a_fresh_claim_waits_without_dispatch(self):
        plan = plan_admission(read([receipt_comment()]))
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertIsNone(plan.transition)

    def test_a_claim_is_reconciled_to_its_actual_run(self):
        plan = plan_admission(
            read([receipt_comment()]),
            runs=check_inventory(run(11, attempt=ATTEMPT)),
            pull_number=PR,
        )
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertIsNotNone(plan.transition)
        self.assertEqual(STATUS_DISPATCHED, plan.transition.status)
        self.assertEqual(11, plan.run_id)
        self.assertEqual(
            "https://github.com/kodmial/continuum/actions/runs/11", plan.run_url
        )

    def test_a_stale_claim_with_a_proved_run_is_dispatched_not_retried(self):
        plan = plan_admission(
            read([receipt_comment()]),
            runs=check_inventory(run(11, attempt=ATTEMPT)),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertEqual(STATUS_DISPATCHED, plan.transition.status)

    def test_queued_and_canceled_repair_runs_still_spend_the_attempt(self):
        # A queued admission and a canceled repair are observed outcomes of
        # the same dispatch. Neither is evidence that no run was created, and
        # neither may authorize a second repair.
        for status, conclusion in (
            ("queued", ""),
            ("in_progress", ""),
            ("completed", "cancelled"),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                plan = plan_admission(
                    read([receipt_comment()]),
                    runs=check_inventory(
                        run(12, attempt=ATTEMPT, status=status, conclusion=conclusion)
                    ),
                    observed_at="2026-11-12T00:00:00Z",
                    now="2026-11-12T01:00:00Z",
                    claim_timeout_seconds=900,
                    pull_number=PR,
                )
                self.assertEqual(ACTION_WAIT, plan.action)
                self.assertEqual(STATUS_DISPATCHED, plan.transition.status)
                self.assertEqual(12, plan.run_id)

    def test_a_stale_claim_with_a_complete_empty_inventory_rearms_once(self):
        plan = plan_admission(
            read([receipt_comment()]),
            runs=check_inventory(),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_REARM, plan.action)
        self.assertIsNotNone(plan.transition)
        self.assertEqual(STATUS_REJECTED, plan.transition.status)

    def test_a_stale_claim_with_partial_inventory_becomes_a_visible_hold(self):
        plan = plan_admission(
            read([receipt_comment()]),
            runs=RunInventory((), False, "pagination incomplete"),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_HOLD, plan.action)
        self.assertEqual(STATUS_AMBIGUOUS, plan.transition.status)

    def test_a_stale_claim_holds_when_a_run_carries_no_usable_title(self):
        # Without a display title the run cannot be excluded as the accepted
        # repair, so the inventory is incomplete and re-arming would risk a
        # duplicate dispatch for the same exact HEAD.
        inventory = normalize_run_pages(
            [{"total_count": 1, "workflow_runs": [{"id": 5, "head_sha": HEAD}]}]
        )
        self.assertFalse(inventory.complete)
        plan = plan_admission(
            read([receipt_comment()]),
            runs=inventory,
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_HOLD, plan.action)
        self.assertEqual(STATUS_AMBIGUOUS, plan.transition.status)

    def test_a_stale_claim_with_an_unattributable_legacy_run_holds(self):
        plan = plan_admission(
            read([receipt_comment()]),
            runs=check_inventory(run(3, created_at="2026-11-12T00:10:00Z")),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_HOLD, plan.action)
        self.assertEqual(STATUS_AMBIGUOUS, plan.transition.status)

    def test_a_stale_claim_with_a_run_for_another_attempt_holds(self):
        # A marked run for the same PR/HEAD that names a different attempt is
        # not explained by the durable receipts. Re-arming the stale claim
        # would create a second live repair for the same HEAD, so the pass must
        # record the contradiction as a visible hold.
        plan = plan_admission(
            read([receipt_comment()]),
            runs=check_inventory(
                run(4, attempt=OTHER_ATTEMPT, created_at="2026-11-12T00:10:00Z")
            ),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-12T01:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_HOLD, plan.action)
        self.assertEqual(STATUS_AMBIGUOUS, plan.transition.status)

    def test_ambiguous_delivery_never_rearms_automatically(self):
        ambiguous = transition_receipt(receipt(), status=STATUS_AMBIGUOUS)
        plan = plan_admission(
            read([comment(render_receipt(ambiguous))]),
            runs=check_inventory(),
            observed_at="2026-11-12T00:00:00Z",
            now="2026-11-13T00:00:00Z",
            claim_timeout_seconds=900,
            pull_number=PR,
        )
        self.assertEqual(ACTION_HOLD, plan.action)
        self.assertIsNone(plan.transition)

    def test_ambiguous_delivery_is_reconciled_to_the_actual_run(self):
        ambiguous = transition_receipt(receipt(), status=STATUS_AMBIGUOUS)
        plan = plan_admission(
            read([comment(render_receipt(ambiguous))]),
            runs=check_inventory(run(21, attempt=ATTEMPT)),
            pull_number=PR,
        )
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertEqual(STATUS_DISPATCHED, plan.transition.status)

    def test_dispatched_attempt_waits_and_legacy_details_are_enriched(self):
        legacy = transition_receipt(receipt(), status=STATUS_DISPATCHED)
        plan = plan_admission(read([comment(render_receipt(legacy))]))
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertIsNone(plan.transition)

        enriched = plan_admission(
            read([comment(render_receipt(legacy))]),
            runs=check_inventory(run(31, attempt=ATTEMPT)),
            pull_number=PR,
        )
        self.assertEqual(ACTION_WAIT, enriched.action)
        self.assertEqual(31, enriched.transition.run_id)

    def test_a_definitively_rejected_attempt_may_rearm(self):
        rejected = transition_receipt(receipt(), status=STATUS_REJECTED)
        plan = plan_admission(read([comment(render_receipt(rejected))]))
        self.assertEqual(ACTION_CLAIM, plan.action)

    def test_new_head_gets_its_own_attempt(self):
        old = receipt_comment(head=OTHER_HEAD, attempt=OTHER_ATTEMPT)
        plan = plan_admission(read([old], head=HEAD))
        self.assertEqual(ACTION_CLAIM, plan.action)

    def test_old_head_records_never_authorize_or_block_the_new_head(self):
        old_claimed = receipt_comment(head=OTHER_HEAD, attempt=OTHER_ATTEMPT)
        new_claimed = receipt_comment(head=HEAD, attempt=ATTEMPT, comment_id=2)
        plan = plan_admission(read([old_claimed, new_claimed], head=HEAD))
        self.assertEqual(ACTION_WAIT, plan.action)

    def test_duplicate_identical_claims_still_prove_the_single_claim(self):
        first = receipt_comment(comment_id=1, created_at="2026-11-12T00:00:01Z")
        duplicate = receipt_comment(comment_id=2, created_at="2026-11-12T00:00:02Z")
        plan = plan_admission(read([first, duplicate]))
        self.assertEqual(ACTION_WAIT, plan.action)

    def test_two_live_attempts_for_one_head_fail_closed(self):
        first = receipt_comment(attempt=ATTEMPT, comment_id=1)
        second = receipt_comment(attempt=OTHER_ATTEMPT, comment_id=2)
        plan = plan_admission(read([first, second]))
        self.assertEqual(ACTION_HOLD, plan.action)

    def test_mixed_reports_of_two_heads_never_rearm_the_live_head(self):
        old = receipt_comment(head=OTHER_HEAD, attempt=OTHER_ATTEMPT, comment_id=1)
        current = receipt_comment(head=HEAD, attempt=ATTEMPT, comment_id=2)
        plan = plan_admission(
            read([old, current]),
            runs=check_inventory(run(41, attempt=ATTEMPT)),
            pull_number=PR,
        )
        self.assertEqual(ACTION_WAIT, plan.action)
        self.assertEqual(STATUS_DISPATCHED, plan.transition.status)

    def test_planning_never_consults_labels_or_recency(self):
        args = AdmissionPlan.__dataclass_fields__
        self.assertNotIn("labels", args)
        self.assertNotIn("pr_labels", args)
        # A claimed exact-HEAD receipt wins even when a presentation label is
        # absent, and a missing receipt cannot be replaced by label state.
        plan = plan_admission(read([receipt_comment()]))
        self.assertEqual(ACTION_WAIT, plan.action)


class ObservationClockTests(unittest.TestCase):
    def test_earliest_trusted_observation_bounds_the_run_creation(self):
        # The claim is persisted before the dispatch request, so a run created
        # by the attempt cannot predate the *first* observation. Using a later
        # transition comment as the lower bound would hide a run created
        # between the claim and that comment and prevent correlation forever.
        first = receipt_comment(comment_id=1, created_at="2026-11-12T00:00:01Z")
        second = receipt_comment(comment_id=2, created_at="2026-11-12T00:05:01Z")
        observed = attempt_observed_at(
            [second, first],
            ATTEMPT,
            repository=REPO,
            pull_number=PR,
            head_sha=HEAD,
        )
        self.assertEqual("2026-11-12T00:00:01Z", observed)

    def test_trusted_token_login_is_respected(self):
        authored = comment(
            render_receipt(receipt()),
            author={"login": "automation-user", "association": "CONTRIBUTOR"},
        )
        self.assertIsNone(
            attempt_observed_at(
                [authored], ATTEMPT, repository=REPO, pull_number=PR, head_sha=HEAD
            )
        )
        self.assertEqual(
            "2026-11-12T00:00:00Z",
            attempt_observed_at(
                [authored],
                ATTEMPT,
                repository=REPO,
                pull_number=PR,
                head_sha=HEAD,
                trusted_logins=("automation-user",),
            ),
        )

    def test_staleness_requires_a_valid_timestamp_and_positive_window(self):
        self.assertTrue(
            timestamp_is_stale(
                "2026-11-12T00:00:00Z",
                now="2026-11-12T00:20:00Z",
                timeout_seconds=900,
            )
        )
        self.assertFalse(
            timestamp_is_stale(
                "2026-11-12T00:00:00Z",
                now="2026-11-12T00:10:00Z",
                timeout_seconds=900,
            )
        )
        self.assertFalse(timestamp_is_stale(None, timeout_seconds=900))
        self.assertFalse(
            timestamp_is_stale("2026-11-12T00:00:00Z", timeout_seconds=0)
        )


if __name__ == "__main__":
    unittest.main()
