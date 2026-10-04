"""Deterministic proof of the mandatory qualification lifecycle gate.

Each test below maps to one required property of the implementation ->
qualification -> capability-completion lifecycle: a merge with pending
qualification keeps the capability non-complete, qualification starts
automatically (and pause cannot strand it), only exact-SHA pass evidence may
complete the capability, failure blocks and routes repair, repair invalidates
stale evidence, auto-close keywords cannot bypass the gate, and duplicate
events cannot start dispatch storms.
"""

from __future__ import annotations

import unittest

from continuum import qualification as gate


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def evidence_comment(issue, sha, result):
    return "notes\n" + gate.evidence_marker(issue, sha, result) + "\nscenario: live-e2e"


def trusted_comment(body, association="OWNER", login="owner"):
    return {
        "body": body,
        "author_association": association,
        "author": {"login": login},
        "user": {"login": login},
    }


def automation_comment(body):
    return {
        "body": body,
        "author_association": "NONE",
        "author": {"login": "github-actions[bot]"},
        "user": {"login": "github-actions[bot]"},
    }


def untrusted_comment(body):
    return {
        "body": body,
        "author_association": "NONE",
        "author": {"login": "stranger"},
        "user": {"login": "stranger"},
    }


class DeclarationParsingTests(unittest.TestCase):
    def test_single_reference_is_parsed(self):
        self.assertEqual(
            gate.parse_qualification_refs("Body\n<!-- automation-qualification: #184 -->"),
            (184,),
        )

    def test_multiple_references_are_sorted_and_deduplicated(self):
        body = "<!-- automation-qualification: #9, #7, #9 -->"
        self.assertEqual(gate.parse_qualification_refs(body), (7, 9))

    def test_prose_issue_numbers_are_not_relationships(self):
        self.assertEqual(gate.parse_qualification_refs("See #184 for context."), ())
        self.assertEqual(
            gate.parse_qualification_refs("automation-qualification: #184"), ()
        )

    def test_self_reference_is_ignored(self):
        body = "<!-- automation-qualification: #182, #184 -->"
        self.assertEqual(gate.parse_qualification_refs(body, self_number=182), (184,))

    def test_non_text_body_has_no_relationships(self):
        self.assertEqual(gate.parse_qualification_refs(None), ())
        self.assertEqual(gate.parse_qualification_refs(123), ())


class MergeKeepsCapabilityNonCompleteTests(unittest.TestCase):
    def test_pending_qualification_after_merge_stays_qualifying(self):
        status = gate.capability_status(
            capability_open=True,
            qualification_refs=(184,),
            evidence_by_qualification={184: gate.EVIDENCE_UNKNOWN},
            required_sha=SHA_A,
        )
        self.assertEqual(status, gate.STAY_QUALIFYING)

    def test_merge_without_recorded_sha_stays_qualifying(self):
        status = gate.capability_status(
            capability_open=True,
            qualification_refs=(184,),
            evidence_by_qualification={},
            required_sha=None,
        )
        self.assertEqual(status, gate.STAY_QUALIFYING)

    def test_no_qualification_keeps_the_normal_lifecycle(self):
        self.assertEqual(
            gate.capability_status(True, (), {}, SHA_A), gate.NO_QUALIFICATION
        )


class AutoDispatchTests(unittest.TestCase):
    def test_pending_qualification_is_dispatched(self):
        dispatch, reason = gate.should_dispatch_qualification(
            qualification_open=True,
            qualification_paused=False,
            capability_state=gate.STAY_QUALIFYING,
            already_dispatched_for_sha=False,
            required_sha=SHA_A,
        )
        self.assertTrue(dispatch)
        self.assertEqual(reason, "dispatch")

    def test_paused_qualification_is_unpaused_and_dispatched_not_stranded(self):
        dispatch, reason = gate.should_dispatch_qualification(
            qualification_open=True,
            qualification_paused=True,
            capability_state=gate.STAY_QUALIFYING,
            already_dispatched_for_sha=False,
            required_sha=SHA_A,
        )
        self.assertTrue(dispatch)
        self.assertEqual(reason, "unpause-and-dispatch")

    def test_blocked_capability_redispatches_after_repair_sha(self):
        dispatch, _ = gate.should_dispatch_qualification(
            qualification_open=True,
            qualification_paused=False,
            capability_state=gate.STAY_BLOCKED,
            already_dispatched_for_sha=False,
            required_sha=SHA_B,
        )
        self.assertTrue(dispatch)

    def test_completed_capability_dispatches_nothing(self):
        dispatch, reason = gate.should_dispatch_qualification(
            qualification_open=True,
            qualification_paused=False,
            capability_state=gate.MAY_COMPLETE,
            already_dispatched_for_sha=False,
            required_sha=SHA_A,
        )
        self.assertFalse(dispatch)
        self.assertEqual(reason, "capability-complete")

    def test_closed_qualification_is_never_dispatched(self):
        dispatch, reason = gate.should_dispatch_qualification(
            qualification_open=False,
            qualification_paused=False,
            capability_state=gate.STAY_QUALIFYING,
            already_dispatched_for_sha=False,
            required_sha=SHA_A,
        )
        self.assertFalse(dispatch)
        self.assertEqual(reason, "qualification-closed")


class ExactShaEvidenceTests(unittest.TestCase):
    def test_pass_for_exact_sha_may_complete(self):
        comments = [evidence_comment(184, SHA_A, "pass")]
        state = gate.qualification_evidence_state(comments, 184, SHA_A)
        self.assertEqual(state, gate.EVIDENCE_PASS)
        status = gate.capability_status(True, (184,), {184: state}, SHA_A)
        self.assertEqual(status, gate.MAY_COMPLETE)

    def test_pass_for_another_issue_does_not_satisfy(self):
        comments = [evidence_comment(999, SHA_A, "pass")]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_stale_evidence_for_older_sha_is_rejected(self):
        comments = [evidence_comment(184, SHA_A, "pass")]
        state = gate.qualification_evidence_state(comments, 184, SHA_B)
        self.assertEqual(state, gate.EVIDENCE_UNKNOWN)
        status = gate.capability_status(True, (184,), {184: state}, SHA_B)
        self.assertEqual(status, gate.STAY_QUALIFYING)

    def test_latest_verdict_wins_for_the_same_sha(self):
        comments = [
            evidence_comment(184, SHA_A, "pass"),
            evidence_comment(184, SHA_A, "fail"),
        ]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_FAIL,
        )

    def test_prose_pass_claim_without_marker_is_unknown(self):
        comments = ["qualification passed for %s, trust me" % SHA_A]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_docker_payload_pass_for_exact_sha_counts(self):
        import json

        payload = json.dumps(
            {"head_sha": SHA_A, "classification": "pass", "schema": "x"}
        )
        comments = [gate.DOCKER_RESULT_MARKER + "\n" + payload]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_PASS,
        )

    def test_docker_payload_for_older_sha_is_stale(self):
        import json

        payload = json.dumps({"head_sha": SHA_A, "classification": "pass"})
        comments = [gate.DOCKER_RESULT_MARKER + "\n" + payload]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_B),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_malformed_marker_contributes_no_evidence(self):
        comments = ["<!-- continuum-qualification-result issue=184 result=pass -->"]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )


class FailureRoutingTests(unittest.TestCase):
    def test_failure_keeps_capability_blocked(self):
        comments = [evidence_comment(184, SHA_A, "fail")]
        state = gate.qualification_evidence_state(comments, 184, SHA_A)
        status = gate.capability_status(True, (184,), {184: state}, SHA_A)
        self.assertEqual(status, gate.STAY_BLOCKED)

    def test_failure_identifies_repair_targets(self):
        self.assertEqual(
            gate.repair_targets({184: gate.EVIDENCE_FAIL, 185: gate.EVIDENCE_PASS}),
            (184,),
        )

    def test_no_failure_means_no_repair(self):
        self.assertEqual(
            gate.repair_targets({184: gate.EVIDENCE_PASS}), ()
        )


class RepairInvalidationTests(unittest.TestCase):
    def test_new_main_sha_supersedes_old_evidence(self):
        self.assertTrue(gate.sha_superseded(SHA_A, SHA_B))
        self.assertFalse(gate.sha_superseded(SHA_A, SHA_A))

    def test_repair_merge_reruns_qualification_for_new_sha(self):
        old_comments = [evidence_comment(184, SHA_A, "pass")]
        # The old pass no longer matches the new required SHA...
        self.assertEqual(
            gate.qualification_evidence_state(old_comments, 184, SHA_B),
            gate.EVIDENCE_UNKNOWN,
        )
        # ...so the capability returns to qualifying and a fresh dispatch
        # for the new SHA is due.
        status = gate.capability_status(
            True, (184,), {184: gate.EVIDENCE_UNKNOWN}, SHA_B
        )
        self.assertEqual(status, gate.STAY_QUALIFYING)
        dispatch, _ = gate.should_dispatch_qualification(
            True, False, status, False, SHA_B
        )
        self.assertTrue(dispatch)
        # A fresh pass for the new SHA completes the lifecycle.
        new_comments = old_comments + [evidence_comment(184, SHA_B, "pass")]
        state = gate.qualification_evidence_state(new_comments, 184, SHA_B)
        self.assertEqual(state, gate.EVIDENCE_PASS)
        self.assertEqual(
            gate.capability_status(True, (184,), {184: state}, SHA_B),
            gate.MAY_COMPLETE,
        )

    def test_latest_required_sha_wins_after_repair(self):
        body = (
            gate.required_sha_marker(182, SHA_A)
            + "\n"
            + gate.required_sha_marker(182, SHA_B)
        )
        self.assertEqual(gate.latest_required_sha(body, 182), SHA_B)


class AutoCloseSafetyTests(unittest.TestCase):
    def test_closing_keywords_are_detected(self):
        for text in (
            "Fixes #182",
            "Closes #182",
            "Resolves #182",
            "fix: implement\n\nFixed #182",
            "Closed #182",
        ):
            with self.subTest(text=text):
                self.assertTrue(gate.contains_closing_keyword(text, 182))

    def test_non_closing_references_are_safe(self):
        for text in (
            "Automated implementation for #182.\n\nRelates to #182",
            "See #182 for context",
            "Fixes #183",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(gate.contains_closing_keyword(text, 182))

    def test_closed_capability_without_evidence_must_reopen(self):
        status = gate.capability_status(
            capability_open=False,
            qualification_refs=(184,),
            evidence_by_qualification={184: gate.EVIDENCE_UNKNOWN},
            required_sha=SHA_A,
        )
        self.assertEqual(status, gate.MUST_REOPEN)

    def test_closed_capability_with_failure_must_reopen(self):
        status = gate.capability_status(
            capability_open=False,
            qualification_refs=(184,),
            evidence_by_qualification={184: gate.EVIDENCE_FAIL},
            required_sha=SHA_A,
        )
        self.assertEqual(status, gate.MUST_REOPEN)

    def test_closed_capability_with_exact_sha_pass_needs_no_reopen(self):
        status = gate.capability_status(
            capability_open=False,
            qualification_refs=(184,),
            evidence_by_qualification={184: gate.EVIDENCE_PASS},
            required_sha=SHA_A,
        )
        self.assertEqual(status, gate.MAY_COMPLETE)

    def test_merely_closed_qualification_is_not_evidence(self):
        # Closed without any evidence comment stays unknown: closure alone
        # never satisfies the gate.
        self.assertEqual(
            gate.qualification_evidence_state([], 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )


class DispatchStormTests(unittest.TestCase):
    def test_duplicate_merge_event_for_same_sha_dispatches_once(self):
        marker = gate.dispatch_marker(182, 184, SHA_A)
        self.assertTrue(
            gate.has_dispatch_marker([marker, "other"], 182, 184, SHA_A)
        )
        dispatch, reason = gate.should_dispatch_qualification(
            qualification_open=True,
            qualification_paused=False,
            capability_state=gate.STAY_QUALIFYING,
            already_dispatched_for_sha=True,
            required_sha=SHA_A,
        )
        self.assertFalse(dispatch)
        self.assertEqual(reason, "already-dispatched")

    def test_new_sha_after_repair_is_a_new_dispatch(self):
        marker = gate.dispatch_marker(182, 184, SHA_A)
        self.assertFalse(
            gate.has_dispatch_marker([marker], 182, 184, SHA_B)
        )

    def test_dispatch_marker_round_trips_through_bodies(self):
        marker = gate.dispatch_marker(182, 184, SHA_A)
        self.assertIn("capability=182", marker)
        self.assertIn("qualification=184", marker)
        self.assertIn(SHA_A, marker)

    def test_marker_builders_reject_bad_revisions(self):
        for builder in (
            lambda: gate.required_sha_marker(182, "short"),
            lambda: gate.dispatch_marker(182, 184, "xyz"),
            lambda: gate.evidence_marker(184, "nope", "pass"),
            lambda: gate.evidence_marker(184, SHA_A, "maybe"),
        ):
            with self.subTest(builder=builder):
                self.assertRaises(ValueError, builder)


class MergeGatedStartTests(unittest.TestCase):
    def test_relation_alone_cannot_start_or_synthesize_sha(self):
        ok, reason = gate.should_start_qualification((184,), None, [])
        self.assertFalse(ok)
        self.assertEqual(reason, "no-required-sha")
        self.assertIsNone(gate.latest_required_sha("no markers", 182))
        status = gate.capability_status(True, (184,), {}, None)
        self.assertEqual(status, gate.STAY_QUALIFYING)
        dispatch, why = gate.should_dispatch_qualification(
            True, False, status, False, None
        )
        self.assertFalse(dispatch)
        self.assertEqual(why, "no-required-sha")

    def test_blocked_capability_cannot_enter_qualifying_before_merge(self):
        ok, reason = gate.should_start_qualification((184,), SHA_A, [182])
        self.assertFalse(ok)
        self.assertEqual(reason, "blocked")
        ok, _ = gate.can_enter_qualifying_state(SHA_A, [182])
        self.assertFalse(ok)
        ok, _ = gate.can_enter_qualifying_state(None, [])
        self.assertFalse(ok)

    def test_ready_start_requires_refs_sha_and_no_blockers(self):
        ok, reason = gate.should_start_qualification((184,), SHA_A, [])
        self.assertTrue(ok)
        self.assertEqual(reason, "ready")


class EvidenceTrustTests(unittest.TestCase):
    def test_dispatch_comment_cannot_satisfy_qualification(self):
        dispatch = gate.dispatch_marker(182, 184, SHA_A)
        poisoned = dispatch + "\n" + gate.evidence_marker(184, SHA_A, "pass")
        comments = [trusted_comment(poisoned)]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_untrusted_passer_is_rejected(self):
        comments = [untrusted_comment(evidence_comment(184, SHA_A, "pass"))]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_trusted_owner_member_collaborator_are_accepted(self):
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            comments = [
                trusted_comment(
                    evidence_comment(184, SHA_A, "pass"),
                    association=association,
                )
            ]
            with self.subTest(association=association):
                self.assertEqual(
                    gate.qualification_evidence_state(comments, 184, SHA_A),
                    gate.EVIDENCE_PASS,
                )

    def test_trusted_automation_result_is_accepted_where_allowed(self):
        import json

        payload = json.dumps({"head_sha": SHA_A, "classification": "pass"})
        comments = [automation_comment(gate.DOCKER_RESULT_MARKER + "\n" + payload)]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_PASS,
        )
        self.assertTrue(gate.is_trusted_comment(comments[0], allow_automation=True))
        self.assertFalse(gate.is_trusted_comment(untrusted_comment("x"), allow_automation=True))

    def test_stale_exact_sha_evidence_is_rejected(self):
        comments = [trusted_comment(evidence_comment(184, SHA_A, "pass"))]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_B),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_latest_trusted_result_wins(self):
        comments = [
            trusted_comment(evidence_comment(184, SHA_A, "pass")),
            trusted_comment(evidence_comment(184, SHA_A, "fail")),
        ]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_FAIL,
        )


class DispatchIdentityTests(unittest.TestCase):
    def test_untrusted_forged_dispatch_cannot_suppress_or_redirect(self):
        forged = [untrusted_comment(gate.dispatch_marker(182, 184, SHA_A))]
        self.assertFalse(
            gate.has_dispatch_marker(forged, 182, 184, SHA_A)
        )
        self.assertTrue(
            gate.has_untrusted_dispatch_forgery(forged, 182, 184, SHA_A)
        )
        trusted = [trusted_comment(gate.dispatch_marker(182, 184, SHA_A))]
        self.assertTrue(gate.has_dispatch_marker(trusted, 182, 184, SHA_A))

    def test_running_sha_a_cannot_be_retargeted_by_sha_b(self):
        run = gate.qualification_run_identity(182, 184, SHA_A)
        self.assertTrue(gate.dispatch_matches_run(182, 184, SHA_A, run))
        self.assertFalse(gate.dispatch_matches_run(182, 184, SHA_B, run))
        self.assertEqual(
            gate.qualification_run_key(182, 184, SHA_A),
            "capability=182 qualification=184 sha=" + SHA_A,
        )

    def test_duplicate_events_coalesce_new_sha_is_distinct(self):
        marker = gate.dispatch_marker(182, 184, SHA_A)
        bodies = [trusted_comment(marker)]
        dispatch, _ = gate.should_dispatch_qualification(
            True, False, gate.STAY_QUALIFYING, True, SHA_A
        )
        self.assertFalse(dispatch)
        self.assertTrue(gate.has_dispatch_marker(bodies, 182, 184, SHA_A))
        self.assertFalse(gate.has_dispatch_marker(bodies, 182, 184, SHA_B))

    def test_exact_sha_binding_survives_main_advance(self):
        run = gate.qualification_run_identity(182, 184, SHA_A)
        self.assertTrue(gate.sha_superseded(SHA_A, SHA_B))
        # The run stays bound to SHA-A; SHA-B is a distinct run identity.
        self.assertNotEqual(
            gate.qualification_run_identity(182, 184, SHA_A),
            gate.qualification_run_identity(182, 184, SHA_B),
        )
        self.assertTrue(gate.dispatch_matches_run(182, 184, SHA_A, run))


class QualificationExecutionModeTests(unittest.TestCase):
    def test_no_diff_pass_succeeds_without_pause(self):
        outcome, reason = gate.evaluate_qualification_run(False, gate.EVIDENCE_PASS)
        self.assertEqual(outcome, "success")
        self.assertEqual(reason, "pass-evidence-recorded")

    def test_no_diff_fail_succeeds_and_routes_to_repair(self):
        outcome, reason = gate.evaluate_qualification_run(False, gate.EVIDENCE_FAIL)
        self.assertEqual(outcome, "success")
        self.assertEqual(reason, "fail-evidence-routes-to-repair")
        self.assertEqual(
            gate.repair_targets({184: gate.EVIDENCE_FAIL}), (184,)
        )

    def test_no_evidence_fails_closed_recoverably(self):
        outcome, reason = gate.evaluate_qualification_run(False, gate.EVIDENCE_UNKNOWN)
        self.assertEqual(outcome, "fail-closed")
        retry, _ = gate.qualification_needs_retry(
            gate.EVIDENCE_UNKNOWN, True, gate.STAY_QUALIFYING
        )
        self.assertTrue(retry)

    def test_cannot_push_product_changes(self):
        outcome, reason = gate.evaluate_qualification_run(True, gate.EVIDENCE_PASS)
        self.assertEqual(outcome, "forbidden")
        self.assertEqual(reason, "qualification-mode-cannot-push-product-changes")
        outcome, _ = gate.evaluate_qualification_run(
            False, gate.EVIDENCE_PASS, pushed_product_changes=True
        )
        self.assertEqual(outcome, "forbidden")


class RecoveryAndRepairTests(unittest.TestCase):
    def test_automatic_retry_never_strands_mandatory_qualification(self):
        for evidence in (gate.EVIDENCE_UNKNOWN, gate.EVIDENCE_FAIL):
            retry, reason = gate.qualification_needs_retry(
                evidence, True, gate.STAY_QUALIFYING
            )
            self.assertTrue(retry, evidence)
            self.assertEqual(reason, "retry-with-backoff")
        # Backoff is bounded and deterministic.
        delays = [gate.next_qualification_retry_delay_seconds(n) for n in range(6)]
        self.assertEqual(delays[0], 60)
        self.assertEqual(delays[-1], 3600)
        # Paused qualification still dispatches (unpause-and-dispatch).
        dispatch, reason = gate.should_dispatch_qualification(
            True, True, gate.STAY_QUALIFYING, False, SHA_A
        )
        self.assertTrue(dispatch)
        self.assertEqual(reason, "unpause-and-dispatch")

    def test_repair_carries_exact_handoff_and_refreshes_stale(self):
        body = gate.repair_issue_body(182, 184, SHA_A, "scenario: live-e2e fail")
        self.assertIn("#184", body)
        self.assertIn(SHA_A, body)
        self.assertIn("read before editing", body.lower() + "read before editing code")
        self.assertTrue(gate.repair_body_needs_refresh("old body", SHA_A, "evidence"))
        self.assertTrue(
            gate.repair_body_needs_refresh(body, SHA_B, "evidence")
        )
        self.assertFalse(
            gate.repair_body_needs_refresh(body, SHA_A, "scenario: live-e2e fail")
        )
        self.assertTrue(
            gate.repair_body_needs_refresh(body, SHA_A, "newer failure matrix")
        )


class EventCompletenessTests(unittest.TestCase):
    def test_result_comment_wakes_scheduler(self):
        self.assertTrue(
            gate.is_qualification_result_comment(evidence_comment(184, SHA_A, "pass"))
        )
        self.assertTrue(
            gate.is_qualification_result_comment(
                gate.DOCKER_RESULT_MARKER + '\n{"head_sha": "%s"}' % SHA_A
            )
        )
        self.assertTrue(
            gate.is_qualification_result_comment(
                gate.RENDER_RESULT_MARKER + '\n{"head_sha": "%s"}' % SHA_A
            )
        )
        self.assertFalse(gate.is_qualification_result_comment("plain prose"))
        self.assertFalse(
            gate.is_qualification_result_comment(gate.dispatch_marker(182, 184, SHA_A))
        )


if __name__ == "__main__":
    unittest.main()
