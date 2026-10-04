"""Regression proof for issue #263: capacity failures must not loop as repairs.

Each test maps to one required property of the authoritative Render failure
classification: capacity evidence never creates a repository repair, real
defect evidence does, transient infrastructure recovers through a bounded
budget, identical capacity fingerprints coalesce, held sources release WIP,
cleanup stays mandatory on every path, and a materially changed
runtime/profile may explicitly re-arm the source.
"""

from __future__ import annotations

import unittest

from continuum import render_execution as engine


LIMIT_512 = 512 * 1024 * 1024


def capacity_evidence(**overrides):
    memory = {
        "limit_bytes": LIMIT_512,
        "memory_current_bytes": LIMIT_512,
        "memory_events": {"high": 10, "max": 3, "oom": 1, "oom_kill": 1},
    }
    state = {"restarts": 4}
    result = {"error": "worker failed"}
    memory.update(overrides.pop("memory", {}))
    state.update(overrides.pop("state", {}))
    result.update(overrides.pop("result", {}))
    return result, memory, state


class CapacityClassificationTests(unittest.TestCase):
    def test_pinned_memory_plus_restart_storm_is_capacity_not_repository(self):
        result, memory, state = capacity_evidence()
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result=result,
            memory_summary=memory,
            state=state,
        )
        self.assertEqual(classification.classification, engine.CAPACITY)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertEqual(decision.action, engine.ACTION_HOLD_CAPACITY)
        self.assertFalse(decision.create_repair)
        self.assertFalse(decision.allow_retry)
        self.assertTrue(decision.release_wip)
        self.assertIsNotNone(decision.hold_marker)
        self.assertIn("continuum-render-capacity-hold", decision.hold_marker)

    def test_peak_over_limit_is_capacity_even_with_defect_text(self):
        result, memory, state = capacity_evidence(
            memory={"limit_bytes": LIMIT_512, "memory_peak_bytes": 600 * 1024 * 1024},
            result={"error": "pytest assertion failed: AssertionError"},
        )
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result=result,
            memory_summary=memory,
            state=state,
        )
        self.assertEqual(classification.classification, engine.CAPACITY)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertFalse(decision.create_repair)

    def test_consumer_memory_verdict_with_cgroup_proof_stays_capacity(self):
        result, memory, state = capacity_evidence()
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result=result,
            memory_summary=memory,
            state=state,
            qualification={"classification": "memory"},
        )
        self.assertEqual(classification.classification, engine.CAPACITY)

    def test_success_with_cleanup_is_pass(self):
        classification = engine.classify_render_failure(
            execute_outcome="success",
            cleanup_outcome="success",
            result={"classification": "pass"},
            memory_summary={"limit_bytes": LIMIT_512, "memory_peak_bytes": 100 * 1024 * 1024},
            state={},
        )
        self.assertEqual(classification.classification, engine.PASS)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertEqual(decision.action, engine.ACTION_PASS)
        self.assertFalse(decision.create_repair)


class RepositoryRepairTests(unittest.TestCase):
    def test_script_test_defect_creates_evidence_driven_repair(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "pytest assertion failed: AssertionError in test_foo"},
            memory_summary={
                "limit_bytes": LIMIT_512,
                "memory_current_bytes": 100 * 1024 * 1024,
            },
            state={},
        )
        self.assertEqual(classification.classification, engine.REPOSITORY)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertEqual(decision.action, engine.ACTION_REPAIR_REPOSITORY)
        self.assertTrue(decision.create_repair)

    def test_generic_failure_without_defect_evidence_creates_no_repair(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "execution failed"},
            memory_summary={},
            state={},
        )
        self.assertNotEqual(classification.classification, engine.REPOSITORY)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertFalse(decision.create_repair)

    def test_open_repair_is_never_duplicated(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "pytest assertion failed: AssertionError"},
            memory_summary={"limit_bytes": LIMIT_512},
            state={},
        )
        self.assertEqual(classification.classification, engine.REPOSITORY)
        decision = engine.decide_render_recovery(
            classification, cleanup_ok=True, existing_repair_open=True
        )
        self.assertFalse(decision.create_repair)


class TransientRecoveryTests(unittest.TestCase):
    def test_render_api_failure_gets_bounded_automatic_recovery(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "Render API 503 Service Unavailable: socket hang up"},
            memory_summary={},
            state={},
        )
        self.assertEqual(classification.classification, engine.TRANSIENT)
        decision = engine.decide_render_recovery(
            classification, cleanup_ok=True, transient_attempts=0
        )
        self.assertEqual(decision.action, engine.ACTION_RETRY_TRANSIENT)
        self.assertTrue(decision.allow_retry)
        self.assertFalse(decision.create_repair)

    def test_transient_budget_exhaustion_holds_without_repair(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "Render API 503 Service Unavailable"},
            memory_summary={},
            state={},
        )
        decision = engine.decide_render_recovery(
            classification, cleanup_ok=True, transient_attempts=10
        )
        self.assertFalse(decision.create_repair)
        self.assertFalse(decision.allow_retry)

    def test_provider_failure_holds_without_repository_repair(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "provider error: model overloaded, try again later"},
            memory_summary={},
            state={},
        )
        self.assertEqual(classification.classification, engine.PROVIDER)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertFalse(decision.create_repair)
        self.assertFalse(decision.allow_retry)

    def test_bare_timeout_holds_for_insufficient_evidence(self):
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "job timed out after 55 minutes"},
            memory_summary={},
            state={},
        )
        self.assertEqual(classification.classification, engine.TIMEOUT_UNKNOWN)
        decision = engine.decide_render_recovery(classification, cleanup_ok=True)
        self.assertFalse(decision.create_repair)
        self.assertFalse(decision.allow_retry)


class FingerprintCoalescingTests(unittest.TestCase):
    def test_identical_capacity_evidence_produces_identical_fingerprints(self):
        first = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed"},
            memory_summary={"limit_bytes": LIMIT_512, "memory_current_bytes": LIMIT_512},
            state={"restarts": 3},
        )
        second = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed"},
            memory_summary={"limit_bytes": LIMIT_512, "memory_current_bytes": LIMIT_512},
            state={"restarts": 3},
        )
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_identical_capacity_fingerprint_coalesces_without_new_task(self):
        result, memory, state = capacity_evidence()
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result=result,
            memory_summary=memory,
            state=state,
        )
        decision = engine.decide_render_recovery(
            classification, cleanup_ok=True, same_fingerprint_seen=True
        )
        self.assertEqual(decision.action, engine.ACTION_HOLD_CAPACITY)
        self.assertFalse(decision.create_repair)
        self.assertFalse(decision.allow_retry)

    def test_fingerprint_marker_round_trips_through_bodies(self):
        result, memory, state = capacity_evidence()
        classification = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result=result,
            memory_summary=memory,
            state=state,
        )
        marker = engine.failure_fingerprint_marker(classification.fingerprint)
        self.assertTrue(
            engine.fingerprint_seen_in_bodies(["notes\n" + marker], classification.fingerprint)
        )
        self.assertFalse(
            engine.fingerprint_seen_in_bodies(["unrelated"], classification.fingerprint)
        )


class WipReleaseTests(unittest.TestCase):
    def test_held_capacity_source_does_not_consume_wip(self):
        self.assertFalse(
            engine.counts_as_active_wip(
                has_in_progress_label=False,
                has_active_run=False,
                has_open_pr=False,
                has_capacity_hold=True,
            )
        )
        # Even a stale reservation label cannot hold a WIP slot once the
        # terminal hold marker exists.
        self.assertFalse(
            engine.counts_as_active_wip(
                has_in_progress_label=True, has_capacity_hold=True
            )
        )

    def test_genuinely_active_work_still_counts(self):
        self.assertTrue(engine.counts_as_active_wip(has_in_progress_label=True))
        self.assertTrue(engine.counts_as_active_wip(has_active_run=True))
        self.assertTrue(engine.counts_as_active_wip(has_open_pr=True))
        self.assertFalse(engine.counts_as_active_wip())


class CleanupGuaranteeTests(unittest.TestCase):
    def test_unverified_cleanup_holds_on_every_classification(self):
        for kwargs in (
            dict(
                execute_outcome="failure",
                result={"error": "pytest assertion failed"},
                memory_summary={"limit_bytes": LIMIT_512},
                state={},
            ),
            dict(
                execute_outcome="failure",
                result={"error": "Render API 503 Service Unavailable"},
                memory_summary={},
                state={},
            ),
        ):
            result, memory, state = (
                kwargs["result"],
                kwargs["memory_summary"],
                kwargs["state"],
            )
            classification = engine.classify_render_failure(
                execute_outcome=kwargs["execute_outcome"],
                cleanup_outcome="success",
                result=result,
                memory_summary=memory,
                state=state,
            )
            decision = engine.decide_render_recovery(classification, cleanup_ok=False)
            self.assertFalse(decision.create_repair, classification.classification)
            self.assertFalse(decision.allow_retry, classification.classification)
            self.assertFalse(decision.cleanup_ok)


class RearmTests(unittest.TestCase):
    def test_changed_profile_produces_new_fingerprint_and_rearms(self):
        old = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed", "profile": "profile-a"},
            memory_summary={"limit_bytes": LIMIT_512, "memory_current_bytes": LIMIT_512},
            state={"restarts": 3},
        )
        new = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed", "profile": "profile-b"},
            memory_summary={"limit_bytes": LIMIT_512, "memory_current_bytes": LIMIT_512},
            state={"restarts": 3},
        )
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        allowed, reason = engine.may_rearm_capacity_hold(old.fingerprint, new.fingerprint)
        self.assertTrue(allowed)
        self.assertEqual(reason, "materially-changed-evidence-rearms")

    def test_changed_memory_configuration_rearms(self):
        old = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed"},
            memory_summary={
                "limit_bytes": LIMIT_512,
                "memory_current_bytes": LIMIT_512,
            },
            state={"restarts": 3},
        )
        new = engine.classify_render_failure(
            execute_outcome="failure",
            cleanup_outcome="success",
            result={"error": "worker failed"},
            memory_summary={
                "limit_bytes": 1024 * 1024 * 1024,
                "memory_current_bytes": 1024 * 1024 * 1024,
            },
            state={"restarts": 3},
        )
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        allowed, _ = engine.may_rearm_capacity_hold(old.fingerprint, new.fingerprint)
        self.assertTrue(allowed)

    def test_identical_fingerprint_never_rearms(self):
        allowed, reason = engine.may_rearm_capacity_hold("abc123", "abc123")
        self.assertFalse(allowed)
        self.assertEqual(reason, "identical-fingerprint-coalesces")


if __name__ == "__main__":
    unittest.main()
