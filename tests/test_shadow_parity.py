"""Normalizing what production did, and comparing it with what Continuum planned.

The classification vocabulary is the deliverable of this module, so the tests
pin each verdict and the order the checks fire in. The order matters: a run that
did not finish cannot be compared whatever it recorded on the way, and a capture
nobody can vouch for cannot support a difference claim.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from continuum.shadow import observation, parity, planner
from continuum.shadow.effects import Effect
from continuum.shadow.observation import OutcomeError, ObservedOutcome, outcome_from_payload
from tests import shadow_support as fixtures


def comment(number: int, text: str) -> Dict[str, Any]:
    return {
        "kind": "comment.create",
        "target": "issue:{}".format(number),
        "detail": {"text": text},
    }


def journal_for(scenario: str = planner.SCENARIO_CI_REPAIR):
    return planner.plan(
        fixtures.event_for(scenario), fixtures.state(), fixtures.config(), origin="test"
    )


def outcome_for(journal, effects: List[Dict[str, Any]], **kwargs: Any) -> ObservedOutcome:
    payload: Dict[str, Any] = {
        "correlation_id": journal.event.correlation_id,
        "actions": effects,
        "fidelity": observation.OBSERVED_EXACT,
    }
    payload.update(kwargs)
    return outcome_from_payload(payload)


def as_dicts(effects) -> List[Dict[str, Any]]:
    return [effect.describe() for effect in effects]


class OutcomeNormalization(unittest.TestCase):
    def test_an_unknown_action_is_refused_not_dropped(self) -> None:
        # Dropping it would report "production did nothing extra" for a capture
        # that recorded an action nobody models -- the most dangerous possible
        # misreading of a parity report.
        with self.assertRaises(OutcomeError) as caught:
            outcome_from_payload(
                {
                    "correlation_id": "e-1",
                    "fidelity": observation.OBSERVED_EXACT,
                    "actions": [{"kind": "some.new.action", "target": "pr:1"}],
                }
            )
        self.assertEqual(caught.exception.code, "unknown_effect_kind")

    def test_an_outcome_without_a_correlation_id_is_refused(self) -> None:
        with self.assertRaises(OutcomeError) as caught:
            outcome_from_payload({"fidelity": observation.OBSERVED_EXACT, "actions": []})
        self.assertEqual(caught.exception.code, "missing_correlation_id")

    def test_a_capture_that_saw_nothing_is_unknown_not_empty(self) -> None:
        outcome = outcome_from_payload(
            {
                "correlation_id": "e-1",
                "fidelity": observation.OBSERVED_UNKNOWN,
                "actions": [],
                "limits": ["the run log was not captured"],
            }
        )
        self.assertFalse(outcome.is_usable)
        self.assertEqual(outcome.limits, ("the run log was not captured",))

    def test_an_outcome_describes_its_own_window(self) -> None:
        outcome = outcome_from_payload(
            {
                "correlation_id": "e-1",
                "fidelity": observation.OBSERVED_EXACT,
                "actions": [comment(8, "hi")],
                "window_started_at": "t0",
                "window_ended_at": "t1",
                "terminal_status": "ok",
                "terminal_detail": "merged",
            }
        )
        self.assertEqual(outcome.window_started_at, "t0")
        self.assertEqual(outcome.terminal_status, "ok")
        self.assertEqual(outcome.describe()["schema"], observation.OUTCOME_SCHEMA)

    def test_a_capture_that_only_names_actions_cannot_claim_a_target(self) -> None:
        outcome = observation.observed_from_kinds("e-1", ["label.add", "pull.merge"])
        self.assertEqual(outcome.fidelity, observation.OBSERVED_PARTIAL)
        self.assertTrue(outcome.is_usable)
        self.assertEqual(outcome.limits, ("the capture recorded action names only",))

    def test_a_journal_compared_against_itself_is_exact(self) -> None:
        # The self-check the replay plane runs: a second pass over the same
        # capture has to plan the same effects, or "same engine, same decision"
        # is not a claim the evidence can support.
        journal = journal_for()
        outcome = observation.observed_from_effects(
            journal.event.correlation_id,
            journal.actions,
            terminal_status=journal.status,
        )
        self.assertEqual(outcome.fidelity, observation.OBSERVED_EXACT)
        self.assertEqual(parity.compare(journal, outcome).classification, parity.EXACT)

    def test_an_effect_document_rebuilds_into_an_equal_effect(self) -> None:
        journal = journal_for()
        for effect in journal.actions:
            self.assertEqual(
                observation.observed_from_effects(
                    "e-1", [effect.describe()]
                ).effects[0].signature,
                effect.signature,
            )


class ParityVerdicts(unittest.TestCase):
    def test_the_same_effects_in_the_same_order_are_exact(self) -> None:
        journal = journal_for()
        result = parity.compare(journal, outcome_for(journal, as_dicts(journal.actions)))
        self.assertEqual(result.classification, parity.EXACT)
        self.assertTrue(result.agrees)
        self.assertEqual(result.matched_count, len(journal.actions))

    def test_an_action_continuum_would_have_taken_is_missing(self) -> None:
        journal = journal_for()
        without_dispatch = [
            entry for entry in as_dicts(journal.actions) if entry["kind"] != "workflow.dispatch"
        ]
        result = parity.compare(journal, outcome_for(journal, without_dispatch))
        self.assertEqual(result.classification, parity.MISSING_ACTION)
        self.assertTrue(result.divergent)
        self.assertEqual(len(result.differences), 1)
        self.assertEqual(result.differences[0].kind, parity.MISSING_ACTION)

    def test_an_action_production_took_is_extra(self) -> None:
        journal = journal_for()
        effects = as_dicts(journal.actions) + [comment(43, "production also commented")]
        result = parity.compare(journal, outcome_for(journal, effects))
        self.assertEqual(result.classification, parity.EXTRA_ACTION)
        self.assertTrue(result.divergent)

    def test_a_different_terminal_state_outranks_the_action_list(self) -> None:
        journal = journal_for()
        result = parity.compare(
            journal,
            outcome_for(journal, as_dicts(journal.actions), terminal_status="failed"),
        )
        self.assertEqual(result.classification, parity.TERMINAL_STATE)
        self.assertEqual(result.differences[0].kind, parity.TERMINAL_STATE)

    def test_a_continuum_refusal_is_not_compared_to_a_production_state(self) -> None:
        # Production was never asked, so there is nothing to disagree with.
        # Reporting it as a terminal mismatch would accuse production of
        # failing to act on an event it never received.
        journal = journal_for(planner.SCENARIO_ISSUE_LIFECYCLE)
        denied = journal.with_status(planner.STATUS_DENIED)
        result = parity.compare(denied, outcome_for(denied, [], terminal_status="ok"))
        self.assertNotEqual(result.classification, parity.TERMINAL_STATE)

    def test_a_failed_shadow_run_is_reported_as_a_shadow_failure(self) -> None:
        journal = journal_for()
        broken = journal.with_status(planner.STATUS_FAILED, decision="crashed", reason="boom")
        result = parity.compare(broken, outcome_for(broken, []))
        self.assertEqual(result.classification, parity.SHADOW_FAILURE)

    def test_no_capture_is_unevaluable_not_a_difference(self) -> None:
        result = parity.compare(journal_for(), None)
        self.assertEqual(result.classification, parity.UNEVALUABLE)
        self.assertFalse(result.agrees)
        # Unevaluable blocks a cutover: you cannot cut over on the strength of a
        # comparison that was never made. It just is not a *divergence*.
        self.assertTrue(result.divergent)
        self.assertNotIn(parity.UNEVALUABLE, parity.AGREEMENT)

    def test_an_unvouched_capture_is_unevaluable(self) -> None:
        journal = journal_for()
        outcome = outcome_from_payload(
            {"correlation_id": journal.event.correlation_id, "fidelity": observation.OBSERVED_UNKNOWN, "actions": []}
        )
        self.assertEqual(parity.compare(journal, outcome).classification, parity.UNEVALUABLE)

    def test_a_capture_for_another_event_is_unevaluable(self) -> None:
        journal = journal_for()
        outcome = outcome_from_payload(
            {
                "correlation_id": "e-something-else",
                "fidelity": observation.OBSERVED_EXACT,
                "actions": [],
            }
        )
        result = parity.compare(journal, outcome)
        self.assertEqual(result.classification, parity.UNEVALUABLE)
        self.assertIn("correlation id mismatch", result.differences[0].summary)

    def test_wording_alone_is_explainable(self) -> None:
        # Same effect, same target, different prose: the only difference is text
        # the reader sees, so it is reported and does not block cutover.
        journal = journal_for()
        effects = as_dicts(journal.actions)
        for entry in effects:
            if entry["kind"] == "workflow.dispatch":
                entry["detail"] = dict(entry["detail"], reason="a different sentence")
        result = parity.compare(journal, outcome_for(journal, effects))
        self.assertEqual(result.classification, parity.EXPLAINABLE)
        self.assertTrue(result.agrees)

    def test_an_order_that_depends_on_itself_is_an_ordering_guard(self) -> None:
        # The commit status has to follow the review that justifies it. Reversing
        # them is not a cosmetic difference, and it is not an extra or a missing
        # action either -- the same two effects, in an order that cannot work.
        journal = journal_for(planner.SCENARIO_CODERABBIT_FINDING)
        effects = as_dicts(journal.actions)
        self.assertIn("review.create", [entry["kind"] for entry in effects])
        self.assertIn("status.create", [entry["kind"] for entry in effects])
        result = parity.compare(journal, outcome_for(journal, list(reversed(effects))))
        self.assertEqual(result.classification, parity.ORDERING_GUARD)
        self.assertTrue(result.divergent)

    def test_an_order_of_independent_effects_is_explainable(self) -> None:
        # Two comment writes do not depend on each other, so swapping them is
        # reported and does not block.
        journal = journal_for(planner.SCENARIO_DEPENDENCY_BLOCKED)
        effects = as_dicts(journal.actions)
        self.assertEqual(len({entry["kind"] for entry in effects}), 1)
        result = parity.compare(journal, outcome_for(journal, list(reversed(effects))))
        self.assertEqual(result.classification, parity.EXPLAINABLE)


class ParityReport(unittest.TestCase):
    def build_results(self):
        journal = journal_for()
        agreeing = parity.compare(journal, outcome_for(journal, as_dicts(journal.actions)))
        other = journal_for(planner.SCENARIO_MERGE_DECISION)
        diverging = parity.compare(other, outcome_for(other, []))
        return [agreeing, diverging]

    def test_a_report_groups_by_classification_and_scenario(self) -> None:
        report = parity.report(self.build_results())
        self.assertEqual(report.by_classification()[parity.EXACT], 1)
        self.assertEqual(report.by_classification()[parity.MISSING_ACTION], 1)
        self.assertEqual(
            report.by_scenario()[planner.SCENARIO_CI_REPAIR], {parity.EXACT: 1}
        )
        self.assertFalse(report.is_clean)
        self.assertEqual(len(report.divergences), 1)

    def test_a_report_names_the_scenarios_it_covered(self) -> None:
        report = parity.report(self.build_results())
        self.assertEqual(
            set(report.scenario_classes_covered()),
            {planner.SCENARIO_CI_REPAIR, planner.SCENARIO_MERGE_DECISION},
        )

    def test_a_clean_report_lists_the_scenarios_still_missing(self) -> None:
        report = parity.report([self.build_results()[0]])
        self.assertTrue(report.is_clean)
        self.assertNotIn(planner.SCENARIO_RELEASE_PLANNING, report.scenario_classes_covered())

    def test_a_verdict_can_always_be_traced_back_to_its_run(self) -> None:
        result = self.build_results()[0]
        self.assertEqual(result.links["correlation_id"], fixtures.ci_repair_behind().correlation_id)
        self.assertTrue(result.continuum_sha)
        self.assertTrue(result.config_version)

    def test_a_summary_line_names_the_verdict_and_the_counts(self) -> None:
        line = parity.summarize(self.build_results()[0])
        self.assertIn(parity.EXACT, line)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
