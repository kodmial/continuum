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


def trusted_comment(body, association="OWNER", login="some-owner"):
    return {
        "body": body,
        "author_association": association,
        "user": {"login": login},
    }


class DispatchInstructionExclusionTests(unittest.TestCase):
    def test_dispatch_comment_with_quoted_result_marker_is_not_evidence(self):
        dispatch = gate.dispatch_marker(182, 184, SHA_A)
        instructional = (
            dispatch
            + "\nMandatory live qualification for capability #182 at main `"
            + SHA_A
            + "`: publish "
            + gate.evidence_marker(184, SHA_A, "pass")
            + " evidence for this exact SHA."
        )
        self.assertEqual(gate.parse_evidence_entries(instructional), [])
        self.assertEqual(
            gate.qualification_evidence_state([instructional], 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_dispatch_prose_without_marker_shape_is_detected(self):
        self.assertTrue(
            gate.is_dispatch_instruction(
                gate.dispatch_marker(182, 184, SHA_A) + "\nvalidate this SHA"
            )
        )
        self.assertFalse(
            gate.is_dispatch_instruction(
                "validate this SHA\n" + gate.evidence_marker(184, SHA_A, "pass")
            )
        )
        self.assertFalse(gate.is_dispatch_instruction(None))

    def test_result_comment_without_dispatch_marker_still_counts(self):
        comments = [evidence_comment(184, SHA_A, "pass")]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_PASS,
        )


class TrustedAuthorTests(unittest.TestCase):
    def test_untrusted_public_marker_is_rejected(self):
        forged = trusted_comment(
            evidence_comment(184, SHA_A, "pass"),
            association="NONE",
            login="random-forger",
        )
        self.assertFalse(gate.is_trusted_evidence_author("NONE", "random-forger"))
        self.assertEqual(
            gate.qualification_evidence_state([forged], 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_contributor_and_first_timer_markers_are_rejected(self):
        for association in ("CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", ""):
            with self.subTest(association=association):
                comment = trusted_comment(
                    evidence_comment(184, SHA_A, "pass"),
                    association=association,
                    login="outsider",
                )
                self.assertEqual(
                    gate.qualification_evidence_state([comment], 184, SHA_A),
                    gate.EVIDENCE_UNKNOWN,
                )

    def test_owner_member_collaborator_markers_are_accepted(self):
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "owner", "Member"):
            with self.subTest(association=association):
                self.assertTrue(
                    gate.is_trusted_evidence_author(association, "someone")
                )
                comment = trusted_comment(
                    evidence_comment(184, SHA_A, "pass"),
                    association=association,
                )
                self.assertEqual(
                    gate.qualification_evidence_state([comment], 184, SHA_A),
                    gate.EVIDENCE_PASS,
                )

    def test_trusted_github_actions_result_is_accepted_where_allowed(self):
        import json

        self.assertTrue(
            gate.is_trusted_evidence_author("NONE", "github-actions[bot]")
        )
        canonical = trusted_comment(
            evidence_comment(184, SHA_A, "pass"),
            association="NONE",
            login="github-actions[bot]",
        )
        self.assertEqual(
            gate.qualification_evidence_state([canonical], 184, SHA_A),
            gate.EVIDENCE_PASS,
        )
        payload = json.dumps({"head_sha": SHA_A, "classification": "pass"})
        docker = trusted_comment(
            gate.DOCKER_RESULT_MARKER + "\n" + payload,
            association="NONE",
            login="github-actions[bot]",
        )
        self.assertEqual(
            gate.qualification_evidence_state([docker], 184, SHA_A),
            gate.EVIDENCE_PASS,
        )

    def test_exact_sha_stale_evidence_is_rejected_even_when_trusted(self):
        comment = trusted_comment(evidence_comment(184, SHA_A, "pass"))
        self.assertEqual(
            gate.qualification_evidence_state([comment], 184, SHA_B),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_untrusted_forgery_cannot_override_trusted_evidence(self):
        comments = [
            trusted_comment(evidence_comment(184, SHA_A, "pass")),
            trusted_comment(
                evidence_comment(184, SHA_A, "fail"),
                association="NONE",
                login="random-forger",
            ),
        ]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_PASS,
        )

    def test_latest_trusted_verdict_wins(self):
        comments = [
            trusted_comment(evidence_comment(184, SHA_A, "pass")),
            trusted_comment(
                evidence_comment(184, SHA_A, "fail"), association="MEMBER"
            ),
        ]
        self.assertEqual(
            gate.qualification_evidence_state(comments, 184, SHA_A),
            gate.EVIDENCE_FAIL,
        )

    def test_comment_without_author_metadata_fails_closed(self):
        self.assertEqual(
            gate.qualification_evidence_state(
                [{"body": evidence_comment(184, SHA_A, "pass")}], 184, SHA_A
            ),
            gate.EVIDENCE_UNKNOWN,
        )
        self.assertEqual(
            gate.qualification_evidence_state([None, 123], 184, SHA_A),
            gate.EVIDENCE_UNKNOWN,
        )

    def test_plain_string_bodies_keep_their_historical_meaning(self):
        self.assertEqual(
            gate.qualification_evidence_state(
                [evidence_comment(184, SHA_A, "pass")], 184, SHA_A
            ),
            gate.EVIDENCE_PASS,
        )


class QualificationDispatchLookupTests(unittest.TestCase):
    def test_latest_dispatch_for_the_issue_wins(self):
        bodies = [
            gate.dispatch_marker(182, 184, SHA_A),
            gate.dispatch_marker(182, 184, SHA_B),
        ]
        self.assertEqual(
            gate.qualification_dispatch_for(bodies, 184),
            {"capability": 182, "sha": SHA_B},
        )

    def test_dispatch_for_another_issue_is_ignored(self):
        bodies = [gate.dispatch_marker(182, 999, SHA_A)]
        self.assertIsNone(gate.qualification_dispatch_for(bodies, 184))

    def test_prose_and_malformed_markers_match_nothing(self):
        self.assertIsNone(
            gate.qualification_dispatch_for(["qualify #184 at " + SHA_A], 184)
        )
        self.assertIsNone(gate.qualification_dispatch_for([None], 184))
        self.assertIsNone(gate.qualification_dispatch_for([], "nope"))


class QualificationRunClassificationTests(unittest.TestCase):
    def test_non_qualification_run_uses_the_normal_path(self):
        disposition, _ = gate.classify_qualification_run(False, False, "pass")
        self.assertEqual(disposition, gate.QUAL_RUN_NOT_QUALIFICATION)
        self.assertTrue(gate.qualification_permits_product_changes(False))

    def test_no_code_with_pass_evidence_succeeds_without_pause(self):
        disposition, _ = gate.classify_qualification_run(
            True, False, gate.EVIDENCE_PASS
        )
        self.assertEqual(disposition, gate.QUAL_RUN_PASS)
        self.assertFalse(gate.qualification_permits_product_changes(True))

    def test_no_code_with_fail_evidence_succeeds_and_routes_repair(self):
        disposition, reason = gate.classify_qualification_run(
            True, False, gate.EVIDENCE_FAIL
        )
        self.assertEqual(disposition, gate.QUAL_RUN_RECORDED_FAIL)
        # A fail verdict is recorded, never an infrastructure failure, so
        # the scheduler can create the existing P0 repair issue.
        self.assertEqual(gate.repair_targets({184: gate.EVIDENCE_FAIL}), (184,))
        self.assertIn("repair", reason)

    def test_no_code_without_evidence_fails_closed(self):
        disposition, _ = gate.classify_qualification_run(
            True, False, gate.EVIDENCE_UNKNOWN
        )
        self.assertEqual(disposition, gate.QUAL_RUN_MISSING_EVIDENCE)

    def test_qualification_mode_cannot_push_product_changes(self):
        for evidence in (
            gate.EVIDENCE_PASS,
            gate.EVIDENCE_FAIL,
            gate.EVIDENCE_UNKNOWN,
        ):
            with self.subTest(evidence=evidence):
                disposition, _ = gate.classify_qualification_run(True, True, evidence)
                self.assertEqual(disposition, gate.QUAL_RUN_REJECT_CHANGES)
        self.assertFalse(gate.qualification_permits_product_changes(True))


class RedispatchTests(unittest.TestCase):
    def _redispatch(self, **overrides):
        params = {
            "qualification_open": True,
            "qualification_paused": False,
            "capability_state": gate.STAY_QUALIFYING,
            "already_dispatched_for_sha": True,
            "required_sha": SHA_A,
            "evidence_state": gate.EVIDENCE_UNKNOWN,
            "run_in_flight": False,
        }
        params.update(overrides)
        return gate.should_redispatch_qualification(**params)

    def test_missing_evidence_without_in_flight_run_redispatches(self):
        dispatch, reason = self._redispatch()
        self.assertTrue(dispatch)
        self.assertEqual(reason, "redispatch-after-failure")

    def test_decided_evidence_never_redispatches(self):
        for evidence in (gate.EVIDENCE_PASS, gate.EVIDENCE_FAIL):
            with self.subTest(evidence=evidence):
                dispatch, reason = self._redispatch(evidence_state=evidence)
                self.assertFalse(dispatch)
                self.assertEqual(reason, "already-decided")

    def test_in_flight_run_is_not_duplicated(self):
        dispatch, reason = self._redispatch(run_in_flight=True)
        self.assertFalse(dispatch)
        self.assertEqual(reason, "already-dispatched")

    def test_paused_qualification_stays_paused(self):
        dispatch, reason = self._redispatch(qualification_paused=True)
        self.assertFalse(dispatch)
        self.assertEqual(reason, "dispatch-paused")

    def test_undispatched_sha_uses_the_first_dispatch_path(self):
        dispatch, reason = self._redispatch(already_dispatched_for_sha=False)
        self.assertFalse(dispatch)
        self.assertEqual(reason, "not-yet-dispatched")

    def test_closed_qualification_and_complete_capability_dispatch_nothing(self):
        dispatch, _ = self._redispatch(qualification_open=False)
        self.assertFalse(dispatch)
        dispatch, reason = self._redispatch(capability_state=gate.MAY_COMPLETE)
        self.assertFalse(dispatch)
        self.assertEqual(reason, "capability-complete")
        dispatch, _ = self._redispatch(required_sha="short")
        self.assertFalse(dispatch)

    def test_duplicate_results_do_not_multiply_repair_targets(self):
        comments = [
            trusted_comment(evidence_comment(184, SHA_A, "fail")),
            trusted_comment(evidence_comment(184, SHA_A, "fail")),
        ]
        state = gate.qualification_evidence_state(comments, 184, SHA_A)
        self.assertEqual(state, gate.EVIDENCE_FAIL)
        self.assertEqual(gate.repair_targets({184: state}), (184,))


if __name__ == "__main__":
    unittest.main()
