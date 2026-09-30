"""Liveness: the plane that makes a Continuum which stopped working look failed.

The tests here are mostly about absence -- an event that was accepted and never
answered, a run that overran, a report built from documents rather than from the
process that produced them -- because those are the cases where nothing raises.
A test that only checks the happy path would pass against a liveness module that
reported every run as live.
"""

from __future__ import annotations

import dataclasses
import unittest

from continuum.shadow import liveness, planner
from continuum.shadow.journal import (
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_NO_ACTION,
    STATUS_OK,
    journal_from_payload,
)
from tests import shadow_support as fixtures


class RunLiveness(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_CI_REPAIR),
            fixtures.state(),
            fixtures.config(),
        )

    def test_a_run_that_decided_inside_its_budget_is_live(self) -> None:
        result = liveness.run_liveness(self.journal, budget_ms=600_000, elapsed_ms=1_500)
        self.assertEqual(result.verdict, liveness.LIVE)
        self.assertTrue(result.passed)
        self.assertFalse(result.failed)
        self.assertEqual(result.decision, "update-branch")
        self.assertIn("1500ms", result.reason)

    def test_a_run_that_decided_slowly_passes_but_is_reported(self) -> None:
        result = liveness.run_liveness(self.journal, budget_ms=1_000, elapsed_ms=800)
        self.assertEqual(result.verdict, liveness.SLOW)
        self.assertTrue(result.passed)
        self.assertIn(result.verdict, liveness.PASSING)

    def test_a_run_that_overran_is_a_stall_even_though_it_decided(self) -> None:
        # The distinction that matters: the decision was right, and the run still
        # failed, because a fleet that answers eventually is not answering.
        result = liveness.run_liveness(self.journal, budget_ms=1_000, elapsed_ms=1_001)
        self.assertEqual(result.verdict, liveness.STALLED)
        self.assertTrue(result.failed)
        self.assertIn("over its 1000ms budget", result.reason)

    def test_a_refusal_is_live(self) -> None:
        journal = self.journal.with_status(STATUS_DENIED, decision="denied", reason="nope")
        result = liveness.run_liveness(journal, budget_ms=600_000, elapsed_ms=10)
        self.assertEqual(result.verdict, liveness.LIVE)
        self.assertEqual(result.decision, "denied")

    def test_a_crash_is_a_liveness_failure_not_a_decision(self) -> None:
        journal = self.journal.with_status(STATUS_FAILED, reason="the provider timed out")
        result = liveness.run_liveness(journal, budget_ms=600_000, elapsed_ms=10)
        self.assertEqual(result.verdict, liveness.CRASHED)
        self.assertTrue(result.failed)
        self.assertEqual(result.reason, "the provider timed out")

    def test_a_terminal_run_with_no_duration_is_an_orphan(self) -> None:
        # Statuses ok, and nothing says how long it took: there is no evidence it
        # finished, so it is not a pass.
        journal = dataclasses.replace(
            self.journal.with_status(STATUS_NO_ACTION, decision="no_action"),
            duration_ms=None,
        )
        result = liveness.run_liveness(journal, budget_ms=600_000)
        self.assertEqual(result.verdict, liveness.ORPHANED)
        self.assertTrue(result.failed)
        self.assertIsNone(result.duration_ms)


class Timeout(unittest.TestCase):
    def test_a_timed_out_run_becomes_a_terminal_journal(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_CI_REPAIR),
            fixtures.state(),
            fixtures.config(),
        ).with_status(STATUS_OK, decision="update-branch", decision_source="planner")

        timed_out = liveness.timeout_journal(journal, budget_ms=1_000, elapsed_ms=1_000)
        self.assertEqual(timed_out.status, "timeout")
        self.assertEqual(timed_out.decision, "budget_exceeded")
        self.assertEqual(timed_out.decision_source, "continuum.shadow.liveness")
        self.assertEqual(timed_out.duration_ms, 1_000)
        self.assertTrue(timed_out.is_terminal)
        self.assertTrue(timed_out.is_execution_failure)
        self.assertEqual(timed_out.errors[-1]["code"], "budget_exceeded")
        # The partial decision is kept: which effects it had already planned is
        # the first thing a reader needs and a truncated journal would lose it.
        self.assertTrue(timed_out.actions)
        self.assertIn("1000ms budget", timed_out.reason)

    def test_a_timed_out_run_is_a_stall(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_CI_REPAIR),
            fixtures.state(),
            fixtures.config(),
        )
        timed_out = liveness.timeout_journal(journal, budget_ms=1_000, elapsed_ms=9_000)
        result = liveness.run_liveness(timed_out, budget_ms=1_000, elapsed_ms=9_000)
        self.assertEqual(result.verdict, liveness.STALLED)
        self.assertTrue(result.errors)


class FleetAccounting(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_CI_REPAIR),
            fixtures.state(),
            fixtures.config(),
        )
        self.acceptance = liveness.acceptance_from_journal(self.journal, accepted_at_ms=1_000)

    def test_an_accepted_event_with_no_result_is_an_orphan(self) -> None:
        report = liveness.report([self.acceptance], [], now_ms=61_000)
        self.assertEqual(report.by_verdict()[liveness.ORPHANED], 1)
        self.assertEqual(report.answer_rate, 0.0)
        self.assertFalse(report.is_clean)
        self.assertIn("60000ms ago", report.failures[0].reason)

    def test_a_deliberately_abandoned_run_neither_passes_nor_fails(self) -> None:
        acceptance = liveness.Acceptance(
            correlation_id=self.acceptance.correlation_id,
            scenario=self.acceptance.scenario,
            accepted_at_ms=1_000,
            abandoned_reason="a duplicate of a run already in progress",
        )
        report = liveness.report([acceptance], [], now_ms=61_000)
        self.assertEqual(report.failures, ())
        self.assertFalse(report.runs[0].passed)
        self.assertTrue(report.runs[0].abandoned)
        self.assertIn("abandoned on purpose", report.runs[0].reason)
        # It is not reported as a decision that was taken either: a run that
        # never ran cannot be the run that answered.
        self.assertNotEqual(report.runs[0].verdict, liveness.LIVE)
        self.assertEqual(report.by_verdict()[liveness.ABANDONED], 1)

    def test_an_abandoned_run_is_out_of_the_answer_rate(self) -> None:
        # Ten answered runs and one superseded event is a plane that answered
        # everything it was asked to answer, not one that answered nine in ten.
        import dataclasses

        acceptances = [
            liveness.Acceptance(correlation_id="live-{}".format(index), accepted_at_ms=1_000)
            for index in range(10)
        ]
        acceptances.append(
            liveness.Acceptance(
                correlation_id="superseded",
                accepted_at_ms=1_000,
                abandoned_reason="a duplicate of a run already in progress",
            )
        )
        answered = [
            dataclasses.replace(
                self.journal,
                event=dataclasses.replace(
                    self.journal.event, correlation_id="live-{}".format(index)
                ),
            )
            for index in range(10)
        ]
        report = liveness.report(acceptances, answered, now_ms=61_000)
        self.assertEqual(len(report.runs), 11)
        self.assertEqual(report.answer_rate, 1.0)
        self.assertTrue(report.is_clean)
        self.assertEqual(len(report.abandoned_runs), 1)
        self.assertIn("1 abandoned", liveness.summary_line(report))
        # And the document names them, so the window shows they were considered
        # rather than missed.
        self.assertEqual(
            [
                run["correlation_id"]
                for run in liveness.describe(report.describe())["abandoned"]
            ],
            ["superseded"],
        )

    def test_a_result_with_no_acceptance_is_still_measured(self) -> None:
        # Otherwise a harness that forgot to write an acceptance could report a
        # clean window over runs it never counted.
        report = liveness.report([], [self.journal], now_ms=2_000, budget_ms=600_000)
        self.assertEqual(len(report.runs), 1)
        self.assertEqual(report.runs[0].verdict, liveness.LIVE)
        self.assertEqual(report.answer_rate, 1.0)

    def test_two_results_for_one_acceptance_is_refused(self) -> None:
        with self.assertRaises(liveness.LivenessError) as caught:
            liveness.report([self.acceptance], [self.journal, self.journal], now_ms=2_000)
        self.assertEqual(caught.exception.code, "duplicate_result")

    def test_the_report_counts_every_verdict_and_the_slowest_run(self) -> None:
        other = planner.plan(
            fixtures.event_for(planner.SCENARIO_MERGE_DECISION),
            fixtures.state(),
            fixtures.config(),
        )
        slow = self.journal.with_duration(9_000)
        fast = other.with_duration(10)
        report = liveness.report(
            [self.acceptance, liveness.acceptance_from_journal(other, 1_000)],
            [slow, fast],
            now_ms=2_000,
            budget_ms=10_000,
        )
        self.assertEqual(report.by_verdict()[liveness.SLOW], 1)
        self.assertEqual(report.by_verdict()[liveness.LIVE], 1)
        self.assertEqual(report.slowest().correlation_id, self.acceptance.correlation_id)
        self.assertEqual(report.answer_rate, 1.0)
        self.assertTrue(report.is_clean)
        self.assertIn("2/2 answered", liveness.summary_line(report))

    def test_an_orphan_and_a_merged_run_fail_the_window_together(self) -> None:
        report = liveness.report(
            [self.acceptance, liveness.Acceptance(correlation_id="lost", accepted_at_ms=1_000)],
            [self.journal.with_duration(10)],
            now_ms=2_000,
        )
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].verdict, liveness.ORPHANED)
        self.assertFalse(report.is_clean)
        self.assertIn("1/2 answered", liveness.summary_line(report))
        self.assertIn("1 failed", liveness.summary_line(report))

    def test_an_empty_window_is_not_clean(self) -> None:
        # No runs is not evidence of liveness. Reporting it as a pass would let a
        # bridge that shadowed nothing certify the cutover.
        report = liveness.report([], [], now_ms=2_000)
        self.assertFalse(report.is_clean)
        self.assertEqual(liveness.summary_line(report), "liveness: no runs accepted in this window")


class FromArtifacts(unittest.TestCase):
    def test_acceptances_round_trip_through_the_bridge_record(self) -> None:
        record = {
            "correlation_id": "corr-1",
            "scenario": planner.SCENARIO_CI_REPAIR,
            "repository": "acme/widgets",
            "accepted_at_ms": 1_700_000_000_000,
            "abandoned_reason": "",
        }
        acceptance = liveness.acceptance_from_payload(record)
        self.assertEqual(acceptance.correlation_id, "corr-1")
        self.assertEqual(acceptance.scenario, planner.SCENARIO_CI_REPAIR)
        self.assertEqual(acceptance.accepted_at_ms, 1_700_000_000_000)
        self.assertEqual(acceptance.describe(), record)

    def test_a_record_without_a_correlation_id_is_refused(self) -> None:
        with self.assertRaises(liveness.LivenessError) as caught:
            liveness.acceptance_from_payload({"scenario": "x"})
        self.assertEqual(caught.exception.code, "missing_correlation_id")

    def test_a_record_with_a_non_numeric_time_is_refused(self) -> None:
        with self.assertRaises(liveness.LivenessError) as caught:
            liveness.acceptance_from_payload({"correlation_id": "c", "accepted_at_ms": "soon"})
        self.assertEqual(caught.exception.code, "invalid_accepted_at")

    def test_a_report_can_be_built_from_documents_by_another_process(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_CI_REPAIR),
            fixtures.state(),
            fixtures.config(),
        ).with_duration(42)
        report = liveness.report_from_documents(
            [{"correlation_id": journal.event.correlation_id, "accepted_at_ms": 1}],
            [journal.describe()],
            now_ms=2_000,
            budget_ms=600_000,
        )
        self.assertTrue(report.is_clean)
        self.assertEqual(report.runs[0].duration_ms, 42)

    def test_a_described_report_round_trips(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_MERGE_DECISION),
            fixtures.state(),
            fixtures.config(),
        ).with_duration(11)
        report = liveness.report(
            [liveness.acceptance_from_journal(journal, 1)], [journal], now_ms=2
        )
        document = report.describe()
        self.assertEqual(liveness.describe(document), document)
        self.assertEqual(document["schema"], liveness.LIVENESS_SCHEMA)
        self.assertTrue(document["clean"])
        self.assertEqual(document["slowest"]["duration_ms"], 11)

    def test_the_journal_reader_the_reports_depend_on_works(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_MERGE_DECISION),
            fixtures.state(),
            fixtures.config(),
        )
        rebuilt = journal_from_payload(journal.describe())
        self.assertEqual(rebuilt.event.correlation_id, journal.event.correlation_id)
        self.assertEqual(rebuilt.status, journal.status)
        self.assertEqual(rebuilt.observed.repository, journal.observed.repository)
        self.assertEqual(
            rebuilt.observed.pull(43).head_sha, journal.observed.pull(43).head_sha
        )

    def test_a_journal_without_state_cannot_be_read_back(self) -> None:
        journal = planner.plan(
            fixtures.event_for(planner.SCENARIO_MERGE_DECISION),
            fixtures.state(),
            fixtures.config(),
        )
        document = journal.describe()
        document["observed"] = None
        with self.assertRaises(ValueError) as caught:
            journal_from_payload(document)
        self.assertIn("missing_observed_state", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
