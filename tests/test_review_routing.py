"""Deterministic tests for the generic review-repair contract (#182)."""

from __future__ import annotations

import unittest

from continuum import review_routing as rr
from continuum.review_routing import (
    NormalizedFinding,
    ReviewRoutingError,
)


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def finding(fid="pra-abc123", path="src/a.py", line=10, severity="high"):
    return NormalizedFinding(
        id=fid, path=path, line=line, severity=severity,
        summary="defect", body="defect body",
    )


class ProviderSelectorTests(unittest.TestCase):
    def test_none_coderabbit_pr_agent(self):
        self.assertEqual(rr.resolve_review_provider("none", "", ""), "none")
        self.assertEqual(rr.resolve_review_provider("coderabbit", "", ""), "coderabbit")
        self.assertEqual(rr.resolve_review_provider("pr-agent", "", ""), "pr-agent")

    def test_invalid_provider_fails_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("gemini", "", "")
        with self.assertRaises(ReviewRoutingError):
            rr.normalize_provider("")

    def test_contradictory_legacy_flags_fail_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("", "true", "true")
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("", "1", "yes")

    def test_only_one_provider_can_be_active(self):
        # Legacy derivation resolves into exactly one provider.
        self.assertEqual(rr.resolve_review_provider("", "true", ""), "coderabbit")
        self.assertEqual(rr.resolve_review_provider("", "", "true"), "pr-agent")
        self.assertEqual(rr.resolve_review_provider("", "", ""), "none")

    def test_canonical_disagreement_fails_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("coderabbit", "", "true")
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("pr-agent", "true", "")
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider("none", "true", "")

    def test_canonical_agreement_is_deterministic(self):
        self.assertEqual(rr.resolve_review_provider("coderabbit", "true", ""), "coderabbit")
        self.assertEqual(rr.resolve_review_provider("pr-agent", "", "true"), "pr-agent")

    def test_env_resolution_prefers_single_transport(self):
        env = {
            "CONTINUUM_REVIEW_PROVIDER": "pr-agent",
            "CONTINUUM_REQUIRE_CODERABBIT": "",
            "CONTINUUM_PR_AGENT_ENABLED": "",
        }
        self.assertEqual(rr.resolve_review_provider_from_env(env), "pr-agent")
        with self.assertRaises(ReviewRoutingError):
            rr.resolve_review_provider_from_env({
                "CONTINUUM_REVIEW_PROVIDER": "coderabbit",
                "CONTINUUM_REQUIRE_CODERABBIT": "",
                "CONTINUUM_PR_AGENT_ENABLED": "true",
            })

    def test_provider_switch_requires_configuration_only(self):
        # Switching is a value change, not a code change: the transport
        # describes exactly one provider.
        for provider in ("none", "coderabbit", "pr-agent"):
            transport = rr.describe_provider_transport(provider)
            self.assertEqual(transport, {"CONTINUUM_REVIEW_PROVIDER": provider})
            self.assertEqual(rr.resolve_review_provider(transport["CONTINUUM_REVIEW_PROVIDER"], "", ""), provider)


class NormalizedPayloadTests(unittest.TestCase):
    def test_coderabbit_native_review_to_normalized_payload(self):
        comments = [
            {"id": 101, "pull_request_review_id": 55, "path": "src/a.py",
             "line": 10, "body": "nil check", "in_reply_to_id": None},
            {"id": 102, "pull_request_review_id": 55, "path": "src/b.py",
             "line": 3, "body": "race", "in_reply_to_id": None},
        ]
        payload = rr.coderabbit_review_to_payload(
            7, 55, "CHANGES_REQUESTED", "body", HEAD, HEAD, comments)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.provider, "coderabbit")
        self.assertEqual(payload.pr_number, 7)
        self.assertEqual(payload.review_id, "55")
        self.assertEqual(len(payload.findings), 2)
        self.assertEqual(payload.verification, "coderabbit-thread-verify")

    def test_pr_agent_native_review_to_same_contract(self):
        findings = [finding("pra-1"), finding("pra-2", path="src/b.py")]
        payload = rr.pr_agent_review_to_payload("9", "instance-1", HEAD, HEAD, findings, True)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.provider, "pr-agent")
        self.assertEqual(payload.pr_number, 9)
        self.assertEqual(payload.verification, "pr-agent-verify")
        # Same contract shape as CodeRabbit.
        self.assertEqual(
            sorted(payload.describe().keys()),
            sorted(rr.coderabbit_review_to_payload(
                9, 1, "CHANGES_REQUESTED", "", HEAD, HEAD,
                [{"id": 1, "pull_request_review_id": 1, "path": "x",
                  "line": 1, "body": "y"}]).describe().keys()),
        )

    def test_multiple_findings_one_batch_repair(self):
        findings = [finding("pra-1"), finding("pra-2"), finding("pra-3")]
        payload = rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD, findings, True)
        assert payload is not None
        # One payload carries all findings: exactly one dispatch.
        self.assertEqual(len(payload.findings), 3)
        self.assertTrue(rr.should_dispatch_repair(payload, HEAD, ()))

    def test_stale_review_head_no_repair(self):
        payload = rr.pr_agent_review_to_payload(1, "r1", OTHER_HEAD, HEAD,
                                                [finding()], True)
        self.assertIsNone(payload)
        stale = rr.coderabbit_review_to_payload(
            1, 5, "CHANGES_REQUESTED", "", OTHER_HEAD, HEAD,
            [{"id": 9, "pull_request_review_id": 5, "path": "x", "line": 1, "body": "y"}])
        self.assertIsNone(stale)
        # Even a built payload with a moved head never dispatches.
        good = rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD, [finding()], True)
        assert good is not None
        self.assertFalse(rr.should_dispatch_repair(good, OTHER_HEAD, ()))

    def test_incomplete_coverage_fails_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD, [finding()], False)

    def test_unchanged_head_no_progress_no_redispatch(self):
        self.assertTrue(rr.same_head_no_progress(HEAD, HEAD, False, True))
        self.assertFalse(rr.same_head_no_progress(HEAD, OTHER_HEAD, False, True))
        self.assertFalse(rr.same_head_no_progress(HEAD, HEAD, True, True))
        self.assertFalse(rr.same_head_no_progress(HEAD, HEAD, False, False))

    def test_duplicate_dispatch_prevented(self):
        payload = rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD,
                                                [finding("pra-1")], True)
        assert payload is not None
        key = rr.dispatch_key(payload)
        self.assertFalse(rr.should_dispatch_repair(payload, HEAD, (key,)))
        self.assertTrue(rr.should_dispatch_repair(payload, HEAD, ()))

    def test_actionable_review_exactly_one_dispatch(self):
        payload = rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD, [finding()], True)
        assert payload is not None
        dispatches = [rr.should_dispatch_repair(payload, HEAD, ())]
        self.assertEqual(dispatches, [True])
        # Second identical review is a duplicate, not a second dispatch.
        self.assertFalse(rr.should_dispatch_repair(payload, HEAD, (rr.dispatch_key(payload),)))

    def test_repair_new_head_re_review_path(self):
        before = rr.pr_agent_review_to_payload(1, "r1", HEAD, HEAD, [finding()], True)
        assert before is not None
        self.assertFalse(rr.should_dispatch_repair(before, OTHER_HEAD, ()))
        after = rr.pr_agent_review_to_payload(1, "r1", OTHER_HEAD, OTHER_HEAD,
                                              [finding()], True)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertTrue(rr.should_dispatch_repair(after, OTHER_HEAD, ()))

    def test_unresolved_bounded_reentry(self):
        self.assertTrue(rr.bounded_retry(0))
        self.assertTrue(rr.bounded_retry(2))
        self.assertFalse(rr.bounded_retry(3))
        self.assertFalse(rr.bounded_retry(10))
        with self.assertRaises(ReviewRoutingError):
            rr.bounded_retry(0, max_attempts=0)

    def test_resolved_no_longer_blocks(self):
        gate = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 0, True, True)
        self.assertTrue(gate.green)
        blocked = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 2, True, True)
        self.assertFalse(blocked.green)

    def test_clean_exact_head_green_gate(self):
        gate = rr.normalized_merge_gate("coderabbit", HEAD, HEAD, 0, True, True)
        self.assertTrue(gate.green)
        self.assertIn("coderabbit", gate.reason)

    def test_auto_merge_consumes_normalized_gate(self):
        # Stale/partial/failed/unknown coverage never merges.
        self.assertFalse(rr.normalized_merge_gate("coderabbit", OTHER_HEAD, HEAD, 0, True, True).green)
        self.assertFalse(rr.normalized_merge_gate("coderabbit", HEAD, HEAD, 0, False, True).green)
        self.assertFalse(rr.normalized_merge_gate("coderabbit", HEAD, HEAD, 1, True, True).green)
        self.assertFalse(rr.normalized_merge_gate("coderabbit", HEAD, HEAD, 0, True, False).green)
        self.assertTrue(rr.normalized_merge_gate("coderabbit", HEAD, HEAD, 0, True, True).green)
        # No provider: CI alone decides.
        self.assertTrue(rr.normalized_merge_gate("none", "", HEAD, 0, False, True).green)
        self.assertFalse(rr.normalized_merge_gate("none", "", HEAD, 0, False, False).green)

    def test_coderabbit_current_behavior_regression(self):
        # Approved exact HEAD dispatches nothing.
        self.assertIsNone(rr.coderabbit_review_to_payload(
            1, 5, "APPROVED", "", HEAD, HEAD,
            [{"id": 9, "pull_request_review_id": 5, "path": "x", "line": 1, "body": "y"}]))
        # Advisory COMMENTED without nitpicks dispatches nothing.
        self.assertIsNone(rr.coderabbit_review_to_payload(
            1, 5, "COMMENTED", "looks fine", HEAD, HEAD,
            [{"id": 9, "pull_request_review_id": 5, "path": "x", "line": 1, "body": "y"}]))
        # CHANGES_REQUESTED without inline findings is a non-code blocker.
        classification = rr.classify_coderabbit_review(
            "CHANGES_REQUESTED", "", 5, HEAD, HEAD, [])
        self.assertFalse(classification.dispatch)
        self.assertIsNotNone(classification.no_progress_marker)
        assert classification.no_progress_marker is not None
        self.assertIn("continuum-coderabbit-no-progress", classification.no_progress_marker)


class PromptAndRetryTests(unittest.TestCase):
    def test_review_fix_prompt_is_provider_neutral(self):
        payload = rr.pr_agent_review_to_payload(3, "r1", HEAD, HEAD, [finding()], True)
        assert payload is not None
        prompt = rr.review_fix_prompt(payload)
        self.assertIn("pr-agent", prompt)
        self.assertNotIn("CodeRabbit", prompt)
        self.assertNotIn("coderabbitai", prompt.lower())

        cr_payload = rr.coderabbit_review_to_payload(
            3, 8, "CHANGES_REQUESTED", "", HEAD, HEAD,
            [{"id": 11, "pull_request_review_id": 8, "path": "x", "line": 1, "body": "y"}])
        assert cr_payload is not None
        cr_prompt = rr.review_fix_prompt(cr_payload)
        # Same generic shape for both providers.
        self.assertIn("Findings:", cr_prompt)
        self.assertIn("Requirements:", cr_prompt)

    def test_infra_vs_semantic_retries_distinguishable(self):
        self.assertEqual(rr.classify_retry("opencode server timeout (status=504)"), "infrastructure")
        self.assertEqual(rr.classify_retry("Review rate limited"), "infrastructure")
        self.assertEqual(rr.classify_retry("finding still applies"), "semantic")

    def test_verification_verdict_unresolved_wins(self):
        self.assertEqual(rr.verification_verdict("**RESOLVED** fixed"), "RESOLVED")
        self.assertEqual(rr.verification_verdict("looks fine"), "UNRESOLVED")
        self.assertEqual(rr.verification_verdict("**RESOLVED**? No: UNRESOLVED"), "UNRESOLVED")

    def test_stable_finding_identity(self):
        first = rr.stable_finding_id("pr-agent", "src/a.py", 10, "rule-x")
        second = rr.stable_finding_id("pr-agent", "src/a.py", 10, "rule-x")
        self.assertEqual(first, second)
        self.assertNotEqual(first, rr.stable_finding_id("pr-agent", "src/a.py", 11, "rule-x"))
        self.assertNotEqual(
            rr.stable_finding_id("pr-agent", "x", 1, "r"),
            rr.stable_finding_id("coderabbit", "x", 1, "r"),
        )
        self.assertEqual(rr.coderabbit_finding_id(42), "cr-42")


if __name__ == "__main__":
    unittest.main()
