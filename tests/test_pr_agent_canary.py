"""Deterministic tests for the PR-Agent canary/E2E harness (issue #174)."""

from __future__ import annotations

import unittest

from continuum import pr_agent
from continuum import pr_agent_canary as canary
from continuum.pr_agent_canary import CanaryError, CanaryPhase, Coverage


def phase(name, sha, text, reviewed=None, head=None):
    reviewed_sha = reviewed or sha
    head_sha = head or sha
    return CanaryPhase(
        name=name,
        reviewed_sha=reviewed_sha,
        head_sha=head_sha,
        review_text=text,
        coverage=Coverage(reviewed=1, total=1),
        reviewer_status_porcelain="",
        reviewer_before_sha=reviewed_sha,
        reviewer_after_sha=head_sha,
    )


class CanaryGateTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(canary.is_canary_enabled({}))
        self.assertFalse(canary.is_canary_enabled({"CONTINUUM_PR_AGENT_CANARY_ENABLED": "false"}))
        report = canary.disabled_canary_report()
        self.assertEqual(report["outcome"], "DISABLED")

    def test_explicit_opt_in(self):
        self.assertTrue(canary.is_canary_enabled({"CONTINUUM_PR_AGENT_CANARY_ENABLED": "true"}))
        self.assertTrue(canary.is_canary_enabled({"CONTINUUM_PR_AGENT_CANARY_ENABLED": "1"}))

    def test_resolve_live_config_stays_disabled_without_opt_in(self):
        result = canary.resolve_live_config({})
        self.assertEqual(result["outcome"], "DISABLED")

    def test_no_paid_secret_fails_closed(self):
        for var in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
            with self.subTest(var=var):
                with self.assertRaises(CanaryError):
                    canary.assert_no_paid_secret({var: "secret"})
        # Clean env passes.
        canary.assert_no_paid_secret({"OPENCODE_MODEL": canary.DEFAULT_OPENCODE_MODEL})

    def test_free_route_required(self):
        canary.assert_free_route("openai/continuum-review", canary.DEFAULT_OPENCODE_MODEL)
        canary.assert_free_route("openai/continuum-review", "")
        with self.assertRaises(CanaryError):
            canary.assert_free_route("groq/llama-3", canary.DEFAULT_OPENCODE_MODEL)
        with self.assertRaises(CanaryError):
            canary.assert_free_route("openai/continuum-review", "anthropic/paid-model")

    def test_coderabbit_untouched(self):
        canary.assert_coderabbit_untouched("CodeRabbit")
        with self.assertRaises(CanaryError):
            canary.assert_coderabbit_untouched("SomeOtherContext")


class CommandPathTests(unittest.TestCase):
    def test_review_and_verify_are_real_commands(self):
        self.assertEqual(canary.parse_canary_command("/review"), ("/review", ""))
        command, argument = canary.parse_canary_command("/verify pra-abc123")
        self.assertEqual(command, "/verify")
        self.assertEqual(argument, "pra-abc123")

    def test_unsupported_commands_fail_closed(self):
        for body in ("", "/improve", "/verify", "/verify a b", "please review", "/review extra"):
            with self.subTest(body=body):
                if body == "/review extra":
                    # /review takes no argument; trailing prose is rejected.
                    with self.assertRaises(CanaryError):
                        canary.parse_canary_command(body)
                elif body.startswith("/verify pra-"):
                    continue
                else:
                    with self.assertRaises(CanaryError):
                        canary.parse_canary_command(body)


class FlowTests(unittest.TestCase):
    def test_blocking_to_clean_passes(self):
        result = canary.self_check()
        self.assertEqual(result.outcome, "CANARY_PASS")
        self.assertEqual(result.verify_verdict, pr_agent.VERIFY_RESOLVED)
        self.assertEqual(result.final_outcome, "APPROVED")
        self.assertTrue(result.finding_id.startswith("pra-"))
        self.assertEqual(result.finding_id, canary.canary_finding_id())

    def test_missing_blocking_finding_fails_closed(self):
        blocking = phase("blocking", "sha-1", canary.clean_review_text())
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=True)

    def test_unresolved_after_fix_fails_closed(self):
        blocking = phase("blocking", "sha-1", canary.blocking_review_text())
        # Correction did not remove the defect.
        corrected = phase("corrected", "sha-2", canary.blocking_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=True)

    def test_stale_head_fails_closed(self):
        blocking = phase("blocking", "sha-1", canary.blocking_review_text(), reviewed="old", head="new")
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=True)

    def test_partial_coverage_fails_closed(self):
        bad = CanaryPhase(
            name="blocking",
            reviewed_sha="sha-1",
            head_sha="sha-1",
            review_text=canary.blocking_review_text(),
            coverage=Coverage(reviewed=1, total=3),
            reviewer_before_sha="sha-1",
            reviewer_after_sha="sha-1",
        )
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(pr_agent.PrAgentError):
            canary.run_canary_flow(bad, corrected, clean, bridge_ok=True)

    def test_bridge_failure_fails_closed(self):
        blocking = phase("blocking", "sha-1", canary.blocking_review_text())
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=False)

    def test_reviewer_mutation_fails_closed(self):
        blocking = CanaryPhase(
            name="blocking",
            reviewed_sha="sha-1",
            head_sha="sha-1",
            review_text=canary.blocking_review_text(),
            coverage=Coverage(reviewed=1, total=1),
            reviewer_status_porcelain="M src/app.py",
            reviewer_before_sha="sha-1",
            reviewer_after_sha="sha-1",
        )
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=True)

    def test_reviewer_commit_fails_closed(self):
        blocking = CanaryPhase(
            name="blocking",
            reviewed_sha="sha-1",
            head_sha="sha-1",
            review_text=canary.blocking_review_text(),
            coverage=Coverage(reviewed=1, total=1),
            reviewer_before_sha="sha-1",
            reviewer_after_sha="sha-other",
        )
        corrected = phase("corrected", "sha-2", canary.clean_review_text())
        clean = phase("clean", "sha-3", canary.clean_review_text())
        with self.assertRaises(CanaryError):
            canary.run_canary_flow(blocking, corrected, clean, bridge_ok=True)


class EvidenceTests(unittest.TestCase):
    def _evidence(self, **overrides):
        base = {
            "main_sha": "abc123",
            "canary_pr": "42",
            "review_command_id": "1001",
            "verify_command_id": "1002",
            "review_id": "2001",
            "workflow_run_ids": ["3001"],
            "bridge_evidence": "bridge up on 127.0.0.1:18000",
            "opencode_evidence": "opencode serve health ok, model free route",
            "finding_id": canary.canary_finding_id(),
            "verify_verdict": "RESOLVED",
            "final_outcome": "APPROVED",
            "cleanup": "pr closed, branch deleted",
        }
        base.update(overrides)
        return canary.build_evidence(**base)

    def test_complete_evidence_passes(self):
        evidence = canary.require_evidence(self._evidence())
        self.assertEqual(evidence["final_outcome"], "APPROVED")

    def test_incomplete_evidence_fails_closed(self):
        with self.assertRaises(CanaryError):
            canary.require_evidence(self._evidence(main_sha=""))

    def test_cleanup_lists_disposable_artifacts(self):
        items = canary.cleanup_checklist()
        self.assertTrue(any("branch" in item for item in items))
        self.assertTrue(any("paid-provider" in item for item in items))


if __name__ == "__main__":
    unittest.main()
