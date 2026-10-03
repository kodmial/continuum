"""Deterministic tests for the live PR-Agent lifecycle repairs (#186).

The OpenCode-backed PR-Agent backend emits its findings in the persistent
``PR Reviewer Guide`` issue comment inside a machine-readable block::

    <!-- pr-agent-review-state:v1
    {...}
    -->

It emits zero GitHub review submissions and zero inline findings, so the
adapter must parse that provider-owned state (never inline comments),
correlate it to the exact reviewed HEAD, fail closed on incomplete or
partial state, batch completely without silent loss, and validate
privileged triggers deterministically.
"""

from __future__ import annotations

import json
import unittest

from continuum import review_routing as rr
from continuum.review_routing import (
    NormalizedFinding,
    ReviewRoutingError,
)


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
REVIEW_ID = HEAD


def state_body(state: dict) -> str:
    return "<!-- pr-agent-review-state:v1\n%s\n-->" % json.dumps(state)


def make_state(head: str = HEAD, review_id: str = REVIEW_ID,
               findings: list | None = None, **overrides) -> dict:
    state = {
        "version": 1,
        "provider": "pr-agent",
        "review_id": review_id,
        "reviewed_sha": head,
        "complete": True,
        "coverage": {"reviewed": 3, "total": 3},
        "findings": findings if findings is not None else [],
    }
    state.update(overrides)
    return state


def active_finding(fid: str, severity: str = "high", status: str = "ACTIVE") -> dict:
    return {
        "id": fid,
        "path": "src/a.py",
        "line": 10,
        "severity": severity,
        "summary": "defect %s" % fid,
        "status": status,
    }


class StateParsingTests(unittest.TestCase):
    def test_active_state_normalizes_to_changes_requested(self):
        state = make_state(findings=[active_finding("pra-1"), active_finding("pra-2")])
        payload = rr.pr_agent_state_to_payload(185, state, HEAD)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.provider, "pr-agent")
        self.assertEqual(payload.pr_number, 185)
        self.assertEqual(sorted(payload.finding_ids()), ["pra-1", "pra-2"])
        self.assertEqual(rr.pr_agent_state_verdict(state, HEAD), "CHANGES_REQUESTED")
        gate = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 2, True, True)
        self.assertFalse(gate.green)

    def test_complete_clean_state_normalizes_to_approved_green(self):
        state = make_state(findings=[])
        self.assertIsNone(rr.pr_agent_state_to_payload(185, state, HEAD))
        self.assertEqual(rr.pr_agent_state_verdict(state, HEAD), "APPROVED")
        gate = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 0, True, True)
        self.assertTrue(gate.green)

    def test_resolved_findings_do_not_block(self):
        state = make_state(findings=[
            active_finding("pra-1", status="RESOLVED"),
            active_finding("pra-2", status="FIXED"),
        ])
        self.assertIsNone(rr.pr_agent_state_to_payload(185, state, HEAD))
        self.assertEqual(rr.pr_agent_state_verdict(state, HEAD), "APPROVED")

    def test_incomplete_last_run_fails_closed(self):
        state = make_state(findings=[active_finding("pra-1")],
                           **{"last_run": {"complete": False}})
        del state["complete"]
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_state_to_payload(185, state, HEAD)
        self.assertEqual(rr.pr_agent_state_verdict(state, HEAD), "CHANGES_REQUESTED")
        gate = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 0, False, True)
        self.assertFalse(gate.green)

    def test_partial_coverage_fails_closed(self):
        state = make_state(findings=[active_finding("pra-1")])
        state["coverage"] = {"reviewed": 1, "total": 3}
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_state_to_payload(185, state, HEAD)
        self.assertFalse(rr.is_pr_agent_state_complete(state))

    def test_unknown_coverage_fails_closed(self):
        state = make_state(findings=[])
        state["coverage"] = {"reviewed": 0, "total": 0}
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_state_to_payload(185, state, HEAD)
        self.assertFalse(rr.is_pr_agent_state_complete(state))

    def test_unknown_finding_status_fails_closed(self):
        state = make_state(findings=[active_finding("pra-1", status="MAYBE")])
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_state_active_findings(state)

    def test_malformed_json_fails_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.parse_pr_agent_state_block("{not json")
        with self.assertRaises(ReviewRoutingError):
            rr.parse_pr_agent_state_block("[1, 2]")

    def test_stale_head_is_rejected_never_dispatched(self):
        state = make_state(head=OTHER_HEAD, findings=[active_finding("pra-1")])
        self.assertIsNone(rr.pr_agent_state_to_payload(185, state, HEAD))
        self.assertEqual(rr.pr_agent_state_verdict(state, HEAD), "STALE")
        gate = rr.normalized_merge_gate("pr-agent", OTHER_HEAD, HEAD, 1, True, True)
        self.assertFalse(gate.green)

    def test_missing_state_never_approves(self):
        self.assertEqual(rr.pr_agent_state_verdict(None, HEAD), "STALE")

    def test_foreign_provider_state_is_rejected(self):
        state = make_state(findings=[active_finding("pra-1")])
        state["provider"] = "coderabbit"
        with self.assertRaises(ReviewRoutingError):
            rr.pr_agent_state_to_payload(185, state, HEAD)


class OwnershipAndCorrelationTests(unittest.TestCase):
    def test_arbitrary_comments_without_marker_are_ignored(self):
        bodies = [
            "looks good to me",
            "<!-- some-other-marker\n{}\n-->",
            "UNRESOLVED, please fix",
        ]
        self.assertIsNone(rr.select_pr_agent_state_for_head(bodies, HEAD))

    def test_state_for_other_head_is_not_selected(self):
        bodies = [state_body(make_state(head=OTHER_HEAD,
                                        findings=[active_finding("pra-1")]))]
        self.assertIsNone(rr.select_pr_agent_state_for_head(bodies, HEAD))

    def test_latest_exact_head_state_wins(self):
        old = make_state(findings=[active_finding("pra-old")])
        new = make_state(findings=[])
        selected = rr.select_pr_agent_state_for_head(
            [state_body(old), state_body(new)], HEAD)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["findings"], [])

    def test_old_head_active_does_not_block_clean_new_head(self):
        stale = make_state(head=OTHER_HEAD, findings=[active_finding("pra-1")])
        clean = make_state(head=HEAD, findings=[])
        bodies = [state_body(stale), state_body(clean)]
        selected = rr.select_pr_agent_state_for_head(bodies, HEAD)
        self.assertIsNotNone(selected)
        self.assertEqual(rr.pr_agent_state_verdict(selected, HEAD), "APPROVED")
        gate = rr.normalized_merge_gate("pr-agent", HEAD, HEAD, 0, True, True)
        self.assertTrue(gate.green)

    def test_verification_selects_only_correlated_active_ids(self):
        state = make_state(findings=[active_finding("pra-1"),
                                     active_finding("pra-2", status="RESOLVED")])
        comments = [
            {"id": 1, "in_reply_to_id": None, "user": {"login": "owner"},
             "finding_id": "pra-1", "body": "finding=pra-1"},
            {"id": 2, "in_reply_to_id": None, "user": {"login": "owner"},
             "finding_id": "pra-2", "body": "finding=pra-2"},
            {"id": 3, "in_reply_to_id": 1, "user": {"login": "owner"},
             "finding_id": "pra-1", "body": "reply"},
            {"id": 4, "in_reply_to_id": None, "user": {"login": "random-human"},
             "finding_id": "pra-1", "body": "finding=pra-1"},
            {"id": 5, "in_reply_to_id": None, "user": {"login": "owner"},
             "finding_id": "pra-9", "body": "unrelated"},
        ]
        selected = rr.select_verification_findings(state, comments, HEAD, "owner")
        self.assertEqual([comment["id"] for comment in selected], [1])

    def test_verification_rejects_stale_head(self):
        state = make_state(head=OTHER_HEAD, findings=[active_finding("pra-1")])
        comments = [{"id": 1, "in_reply_to_id": None, "user": {"login": "owner"},
                     "finding_id": "pra-1", "body": "finding=pra-1"}]
        self.assertEqual(rr.select_verification_findings(state, comments, HEAD, "owner"), [])


class BatchingTests(unittest.TestCase):
    def test_more_than_twenty_findings_are_not_lost(self):
        findings = [
            NormalizedFinding(id="pra-%03d" % index, path="src/a.py", line=index,
                              severity="high", summary="defect", body="defect")
            for index in range(55)
        ]
        batches = rr.batch_normalized_findings(findings)
        self.assertEqual(len(batches), 2)
        flattened = [finding.id for batch in batches for finding in batch]
        self.assertEqual(sorted(flattened), sorted(finding.id for finding in findings))

    def test_unrepresentable_sets_fail_closed(self):
        findings = [
            NormalizedFinding(id="pra-%04d" % index, path="src/a.py", line=index,
                              severity="high", summary="defect", body="defect")
            for index in range(
                rr.MAX_FINDINGS_PER_DISPATCH * rr.MAX_DISPATCH_BATCHES + 1)
        ]
        with self.assertRaises(ReviewRoutingError):
            rr.batch_normalized_findings(findings)

    def test_empty_batch_fails_closed(self):
        with self.assertRaises(ReviewRoutingError):
            rr.batch_normalized_findings([])

    def test_stable_ids_survive_review_to_repair(self):
        first = rr.pr_agent_state_active_findings(
            make_state(findings=[active_finding("pra-keep")]))
        payload = rr.pr_agent_state_to_payload(
            185, make_state(findings=[active_finding("pra-keep")]), HEAD)
        assert payload is not None
        self.assertEqual([finding.id for finding in first], list(payload.finding_ids()))
        self.assertTrue(rr.should_dispatch_repair(payload, HEAD, ()))
        # Duplicate dispatch of the same normalized set is bounded.
        self.assertFalse(rr.should_dispatch_repair(payload, HEAD, (rr.dispatch_key(payload),)))


class TriggerValidationTests(unittest.TestCase):
    def test_arbitrary_unresolved_reply_cannot_trigger_repair(self):
        state = make_state(findings=[active_finding("pra-1")])
        self.assertFalse(rr.validate_pr_agent_trigger(
            "random-human", "owner", "owner", "pra-1", state, HEAD))
        self.assertFalse(rr.validate_pr_agent_trigger(
            "owner", "random-human", "owner", "pra-1", state, HEAD))

    def test_provider_owned_trigger_with_correlated_finding_dispatches(self):
        state = make_state(findings=[active_finding("pra-1")])
        for author in ("owner", "github-actions[bot]"):
            self.assertTrue(rr.validate_pr_agent_trigger(
                author, "owner", "owner", "pra-1", state, HEAD))

    def test_unknown_finding_or_stale_head_rejects_trigger(self):
        state = make_state(findings=[active_finding("pra-1")])
        self.assertFalse(rr.validate_pr_agent_trigger(
            "owner", "owner", "owner", "pra-unknown", state, HEAD))
        stale = make_state(head=OTHER_HEAD, findings=[active_finding("pra-1")])
        self.assertFalse(rr.validate_pr_agent_trigger(
            "owner", "owner", "owner", "pra-1", stale, HEAD))

    def test_incomplete_state_rejects_trigger(self):
        state = make_state(findings=[active_finding("pra-1")])
        state["complete"] = False
        self.assertFalse(rr.validate_pr_agent_trigger(
            "owner", "owner", "owner", "pra-1", state, HEAD))

    def test_unresolved_reentry_carries_exact_payload_and_is_bounded(self):
        state = make_state(findings=[active_finding("pra-1"),
                                     active_finding("pra-2")])
        payload = rr.pr_agent_state_to_payload(185, state, HEAD)
        assert payload is not None
        # The retry carries the exact normalized unresolved set: non-empty
        # and identical to the provider-owned ACTIVE set.
        self.assertEqual(sorted(payload.finding_ids()), ["pra-1", "pra-2"])
        self.assertTrue(len(payload.findings) > 0)
        self.assertTrue(rr.bounded_retry(0))
        self.assertTrue(rr.bounded_retry(2))
        self.assertFalse(rr.bounded_retry(3))
        # Same-head no-progress protection stays provider-neutral.
        self.assertTrue(rr.same_head_no_progress(HEAD, HEAD, False, True))
        self.assertFalse(rr.same_head_no_progress(HEAD, HEAD, True, True))

    def test_no_progress_without_payload_never_dispatches(self):
        self.assertFalse(rr.should_dispatch_repair(None, HEAD, ()))
        empty = rr.NormalizedRepairPayload(
            provider="pr-agent", pr_number=185, review_id=REVIEW_ID,
            reviewed_sha=HEAD, head_sha=HEAD, findings=(),
            verification="pr-agent-verify")
        self.assertFalse(rr.should_dispatch_repair(empty, HEAD, ()))


if __name__ == "__main__":
    unittest.main()
