"""The cutover gate: what has to be true before #28 may remove NanoDictate.

The issue's own warning shapes this file: a run *count* is not the gate, scenario
coverage is. So the tests that matter are the ones where a window looks healthy by
count and is still refused -- a missing scenario, a scenario covered only by
replays, a divergence nobody resolved, a stalled run, and an approval granted
against evidence that has since moved.
"""

from __future__ import annotations

import dataclasses
import unittest

from continuum.shadow import baseline as baseline_plane
from continuum.shadow import cutover, liveness, parity, planner
from continuum.shadow.observation import outcome_from_payload
from tests import shadow_support as fixtures

WINDOW = ("2026-03-01T00:00:00Z", "2026-03-15T00:00:00Z")

ALL_REQUIRED = tuple(name for name, _ in cutover.REQUIRED_SCENARIOS)


def _agreed(scenario: str, journal):
    """Production did what Continuum expected: the healthy-window case."""

    return outcome_from_payload(
        {
            "correlation_id": journal.event.correlation_id,
            "fidelity": "exact",
            "terminal_state": journal.status,
            "decision": journal.decision,
            "actions": [effect.describe() for effect in journal.actions],
        }
    )


def _result(scenario: str, *, correlation: str = "", classification: str = "", as_scenario: str = ""):
    """A verdict as the live plane would produce it, for one scenario.

    Defaults to ``exact``: production agreed. A window whose every scenario is
    exact is the window the gate exists to certify, so the tests need one.
    """

    journal = planner.plan(
        fixtures.event_for(scenario), fixtures.state(), fixtures.config(), now_ms=1
    )
    result = parity.compare(journal, _agreed(scenario, journal))
    if classification or as_scenario:
        return parity.ParityResult(
            classification=classification or result.classification,
            correlation_id=correlation or result.correlation_id,
            scenario=as_scenario or result.scenario,
            summary="test verdict: {}".format(classification or as_scenario),
            continuum_sha="a" * 40,
            links={"correlation_id": correlation or result.correlation_id},
        )
    if correlation and correlation != result.correlation_id:
        return parity.ParityResult(
            classification=result.classification,
            correlation_id=correlation,
            scenario=result.scenario,
            summary=result.summary,
            continuum_sha="a" * 40,
            links={"correlation_id": correlation},
        )
    return result


def _results(scenarios=ALL_REQUIRED, *, origin: str = "bridge"):
    results = []
    origins = {}
    for scenario in scenarios:
        correlation = "corr-{}".format(scenario)
        results.append(_result(scenario, correlation=correlation))
        origins[correlation] = origin
    return results, origins


def _liveness(correlations, **kwargs):
    runs = []
    for correlation in correlations:
        runs.append(
            liveness.RunLiveness(
                correlation_id=correlation, verdict=liveness.LIVE, status="ok", duration_ms=5
            )
        )
    return liveness.LivenessReport(runs=tuple(runs), **kwargs)


# --------------------------------------------------------------------------- #
# The live reading
# --------------------------------------------------------------------------- #
#
# Every window test below is about the *recorded* evidence, so each supplies the
# one input that is not recorded: a reading of the consumer repository taken now.
# It is built from a ledger whose blob SHAs are the reading's own, which is the
# only way a reading can be clean -- an approved ledger and a live repository that
# agree by construction. The tests that care about disagreement do not use this.

LEDGER_HEAD = "b" * 40

#: The one audited workflow these tests use. Its blob is repeated in every
#: reading so that agreement is the default and each test can break exactly one
#: thing.
CI_BLOB = "b" * 40


def _ledger(*, head: str = LEDGER_HEAD, audited_at: str = "2026-02-01"):
    """An approved ledger for one workflow, in consumer-neutral terms."""

    return baseline_plane.ParityLedger(
        repository="example/consumer",
        audited_head=head,
        audited_at=audited_at,
        workflows=(
            baseline_plane.LedgerEntry(
                path=".github/workflows/ci.yml",
                blob_sha=CI_BLOB,
                classification="absorbed",
                owner="#11",
                rationale="audited",
            ),
        ),
    )


def _live(
    *,
    head: str = "a" * 40,
    repository: str = "example/consumer",
    complete: bool = True,
    limits=(),
):
    """A reading taken after the ledger was written, with nothing changed."""

    return baseline_plane.LiveHead(
        repository=repository,
        default_branch="main",
        head_sha=head,
        workflows=(
            baseline_plane.WorkflowFile(path=".github/workflows/ci.yml", blob_sha=CI_BLOB),
        ),
        complete=complete,
        limits=tuple(limits),
    )


def _clean_baseline():
    """The one reading a healthy window needs: the repository has not moved."""

    ledger = _ledger()
    return baseline_plane.audit(ledger, _live())


class LiveReading(unittest.TestCase):
    """#60's requirement: a historical reference snapshot must not authorize cutover.

    Every other input to the gate is evidence recorded during the window. None of
    it names the consumer repository as it is now, so a window that looks perfect
    on its own terms still has to be checked against the live head.
    """

    def test_a_window_with_no_live_reading_is_blocked(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("no_live_baseline", decision.codes)
        self.assertFalse(decision.ready)
        self.assertIn("historical reference snapshot", decision.summary_line() + " ".join(
            blocker.message for blocker in decision.blockers
        ))

    def test_a_clean_reading_clears_the_blocker(self) -> None:
        results, origins = _results()
        reading = _clean_baseline()
        self.assertTrue(reading.ready)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertNotIn("no_live_baseline", decision.codes)
        self.assertTrue(decision.ready)

    def test_drift_is_reported_with_the_issue_that_owns_it(self) -> None:
        results, origins = _results()
        # The audited blob moved. Nothing about the recorded window changed, and
        # the window is still refused: the parity evidence was granted against
        # bytes that no longer exist.
        moved = baseline_plane.LiveHead(
            repository="example/consumer",
            default_branch="main",
            head_sha="a" * 40,
            workflows=(
                baseline_plane.WorkflowFile(path=".github/workflows/ci.yml", blob_sha="c" * 40),
            ),
        )
        reading = baseline_plane.audit(_ledger(), moved)
        self.assertFalse(reading.ready)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("workflow_blob_changed", decision.codes)
        # The routing survives into the cutover artifact, so whoever has to fix
        # this is named in the document that says it is broken.
        self.assertEqual(decision.baseline.routing["#11"], (".github/workflows/ci.yml",))
        self.assertIn("no longer evidence about this file", " ".join(
            blocker.message for blocker in decision.blockers
        ))

    def test_a_workflow_nobody_classified_blocks_and_routes_to_discovery(self) -> None:
        results, origins = _results()
        appeared = baseline_plane.LiveHead(
            repository="example/consumer",
            default_branch="main",
            head_sha="a" * 40,
            workflows=(
                baseline_plane.WorkflowFile(path=".github/workflows/ci.yml", blob_sha="b" * 40),
                baseline_plane.WorkflowFile(path=".github/workflows/new-thing.yml", blob_sha="d" * 40),
            ),
        )
        reading = baseline_plane.audit(_ledger(), appeared)
        self.assertIn("unclassified_workflow", reading.codes)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertFalse(decision.ready)
        self.assertIn(
            ".github/workflows/new-thing.yml", decision.baseline.routing["#60"]
        )

    def test_an_unreadable_reading_blocks_without_claiming_drift(self) -> None:
        # Absence is failure. A partial reading compared with a complete ledger
        # would name every file it did not see as removed, which is a claim about
        # the gap rather than about the repository.
        results, origins = _results()
        partial = baseline_plane.LiveHead(
            repository="example/consumer",
            complete=False,
            limits=("workflow inventory could not be read: rate limited",),
        )
        reading = baseline_plane.audit(_ledger(), partial)
        self.assertEqual(reading.verdict, "incomplete")
        self.assertEqual(reading.differences, ())
        self.assertFalse(reading.ready)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn(baseline_plane.INCOMPLETE_BLOCKER, decision.codes)
        self.assertNotIn("workflow_removed", decision.codes)

    def test_a_reading_of_another_repository_is_refused(self) -> None:
        reading = baseline_plane.audit(_ledger(), _live(repository="someone/else"))
        self.assertIn("repository_mismatch", reading.codes)

    def test_a_window_collected_before_the_audit_is_refused(self) -> None:
        # The approval digest already ties a signature to one ledger, so this is
        # not about catching a stale signature. It is about the case where the
        # signature is fresh and still meaningless: a difference is re-audited
        # today, the old window is re-signed, and now the reading and the
        # approval agree perfectly about a repository state no run in the window
        # ever observed. Freshness has to be an order, not a digest.
        results, origins = _results()
        reading = baseline_plane.audit(
            _ledger(audited_at="2026-06-01"), _live()
        )
        self.assertTrue(reading.ready)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("window_predates_ledger", decision.codes)
        self.assertFalse(decision.ready)

    def test_a_window_starting_the_day_of_the_audit_is_accepted(self) -> None:
        # Same-day is not stale. Only an ordering backwards is a claim this gate
        # can support, so the boundary is inclusive and no clock is consulted.
        results, origins = _results()
        reading = baseline_plane.audit(_ledger(audited_at="2026-03-01"), _live())
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertNotIn("window_predates_ledger", decision.codes)

    def test_an_unreadable_audit_date_blocks_rather_than_defaulting(self) -> None:
        results, origins = _results()
        reading = baseline_plane.audit(_ledger(audited_at=""), _live())
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=reading,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("ledger_audit_date_unreadable", decision.codes)

    def test_an_unreadable_window_start_blocks_rather_than_defaulting(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at="last Tuesday",
            window_ended_at=WINDOW[1],
        )
        self.assertIn("window_start_unreadable", decision.codes)

    def test_a_nonsense_audit_date_is_unreadable_not_merely_odd(self) -> None:
        # A date that parses as a shape but not as a calendar has to be refused
        # rather than string-compared, or "2026-13-45" sorts before everything.
        self.assertIsNone(cutover._iso_date("2026-13-45"))
        self.assertIsNone(cutover._iso_date(""))
        self.assertIsNone(cutover._iso_date(None))
        self.assertEqual(cutover._iso_date("2026-03-15T09:00:00Z"), "2026-03-15")

    def test_the_reading_is_part_of_the_evidence_an_approval_is_bound_to(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        first = cutover.decide(
            results, live, origins=origins, baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        second = cutover.decide(
            results, live, origins=origins,
            baseline_report=baseline_plane.audit(
                _ledger(),
                baseline_plane.LiveHead(
                    repository="example/consumer",
                    default_branch="main",
                    head_sha="e" * 40,
                    workflows=(
                        baseline_plane.WorkflowFile(path=".github/workflows/ci.yml", blob_sha="b" * 40),
                    ),
                ),
            ),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        # Same window, same verdicts, different reading: the digests must differ,
        # so an approval granted against the earlier reading cannot carry over.
        self.assertNotEqual(first.evidence_digest, second.evidence_digest)


class ScenarioRequirements(unittest.TestCase):
    def test_the_required_classes_are_the_ones_the_issue_lists(self) -> None:
        self.assertEqual(
            set(ALL_REQUIRED),
            {
                "issue-lifecycle",
                "ci-repair",
                "coderabbit-finding",
                "merge-decision",
                "dependency-blocked",
                "release-planning",
                "duplicate-replay",
                "timeout-recovery",
            },
        )

    def test_every_required_class_carries_its_reason(self) -> None:
        for name, reason in cutover.REQUIRED_SCENARIOS:
            self.assertTrue(reason.strip(), "{} has no recorded reason".format(name))


class Coverage(unittest.TestCase):
    def test_a_window_that_saw_everything_covers_everything(self) -> None:
        results, origins = _results()
        report = cutover.coverage(results, origins=origins, window_started_at=WINDOW[0], window_ended_at=WINDOW[1])
        self.assertEqual(report.missing, ())
        for entry in report.scenarios:
            if entry.required:
                self.assertEqual(entry.state, "live")
                self.assertEqual(entry.live_events, 1)
                self.assertEqual(entry.engines, ("a" * 40,))

    def test_a_run_count_never_replaces_a_missing_class(self) -> None:
        # Ninety-nine merge decisions, no releases. The gate must still refuse.
        scenarios = ["merge-decision"] * 99
        results = []
        origins = {}
        for index in range(99):
            correlation = "corr-{}".format(index)
            results.append(_result("merge-decision", correlation=correlation))
            origins[correlation] = "bridge"
        report = cutover.coverage(results, origins=origins)
        self.assertEqual(report.for_scenario("merge-decision").events, 99)
        self.assertIn("release-planning", report.missing)
        self.assertIn("ci-repair", report.missing)

    def test_a_class_seen_only_through_replays_is_not_live_coverage(self) -> None:
        live_scenarios = tuple(name for name in ALL_REQUIRED if name != "release-planning")
        results, origins = _results(live_scenarios)
        replayed = _results(["release-planning"], origin="replay")
        report = cutover.coverage(list(results) + list(replayed[0]), origins={**origins, **replayed[1]})
        entry = report.for_scenario("release-planning")
        self.assertEqual(entry.state, "replayed-only")
        self.assertEqual(entry.replayed_events, 1)
        self.assertIn("release-planning", report.missing)

    def test_a_run_with_no_recorded_origin_is_not_counted_as_live(self) -> None:
        results, _ = _results()
        report = cutover.coverage(results, origins={})
        self.assertEqual(report.for_scenario("ci-repair").state, "unknown-origin")
        self.assertIn("ci-repair", report.missing)

    def test_an_undeclared_scenario_is_reported_rather_than_dropped(self) -> None:
        results, origins = _results()
        extra = _result("merge-decision", correlation="corr-odd", as_scenario="something-new")
        report = cutover.coverage(list(results) + [extra], origins={**origins, "corr-odd": "bridge"})
        entry = report.for_scenario("something-new")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.required)
        self.assertEqual(entry.live_events, 1)
        self.assertIn("something-new", report.optional_seen)

    def test_coverage_needs_a_list(self) -> None:
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.coverage("not a list")
        self.assertEqual(caught.exception.code, "results_not_a_list")


class Gate(unittest.TestCase):
    def test_a_complete_window_is_ready(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertEqual(decision.blockers, ())
        self.assertTrue(decision.ready)
        self.assertFalse(decision.approved, "the gate must not approve itself")
        self.assertIn("awaiting a recorded approval", decision.summary_line())

    def test_a_window_without_a_declared_window_is_refused_outright(self) -> None:
        results, origins = _results()
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.decide(results, _liveness([]), origins=origins, window_started_at=WINDOW[0])
        self.assertEqual(caught.exception.code, "window_not_declared")

    def test_every_blocker_is_named_at_once(self) -> None:
        results, origins = _results(["merge-decision"])
        decision = cutover.decide(
            results,
            _liveness(["corr-merge-decision"]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        codes = set(decision.codes)
        self.assertIn("uncovered_scenario", codes)
        self.assertEqual(decision.ready, False)
        # Not one blocker per pass: a reader gets the whole list.
        self.assertGreaterEqual(len(decision.blockers), 7)
        for blocker in decision.blockers:
            self.assertTrue(blocker.message.strip())
        self.assertIn("not ready", decision.summary_line())

    def test_an_unresolved_divergence_blocks_the_window(self) -> None:
        results, origins = _results()
        results = list(results)
        results.append(
            _result(
                "merge-decision",
                correlation="corr-diverged",
                classification=parity.MISSING_ACTION,
            )
        )
        origins["corr-diverged"] = "bridge"
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("unresolved_divergence", decision.codes)
        self.assertEqual(decision.unresolved[0]["correlation_id"], "corr-diverged")
        self.assertEqual(decision.unresolved[0]["classification"], parity.MISSING_ACTION)
        self.assertFalse(decision.ready)

    def test_a_recorded_resolution_clears_the_divergence(self) -> None:
        results, origins = _results()
        results.append(
            _result("merge-decision", correlation="corr-diverged", classification=parity.EXTRA_ACTION)
        )
        origins["corr-diverged"] = "bridge"
        resolution = cutover.Resolution(
            correlation_id="corr-diverged",
            resolution="NanoDictate merged first; both systems were right about the state",
            resolved_by="maintainer",
            resolved_at="2026-03-10T00:00:00Z",
        )
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            resolutions=[resolution],
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertNotIn("unresolved_divergence", decision.codes)
        self.assertTrue(decision.ready)

    def test_a_resolution_without_a_reason_does_not_clear_anything(self) -> None:
        results, origins = _results()
        results.append(
            _result("merge-decision", correlation="corr-diverged", classification=parity.EXTRA_ACTION)
        )
        origins["corr-diverged"] = "bridge"
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            resolutions=[cutover.Resolution(correlation_id="corr-diverged", resolution="  ")],
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("unresolved_divergence", decision.codes)

    def test_a_stalled_run_blocks_the_window(self) -> None:
        results, origins = _results()
        report = liveness.LivenessReport(
            runs=(
                liveness.RunLiveness(
                    correlation_id="corr-merged", verdict=liveness.LIVE, status="ok", duration_ms=5
                ),
                liveness.RunLiveness(
                    correlation_id="corr-lost",
                    verdict=liveness.ORPHANED,
                    reason="accepted 3600000ms ago with no result",
                ),
            )
        )
        decision = cutover.decide(
            results,
            report,
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("liveness_orphaned", decision.codes)

    def test_no_liveness_report_is_a_blocker_not_an_assumption(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results, None, origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        self.assertIn("no_liveness_evidence", decision.codes)

    def test_an_empty_liveness_report_is_not_evidence(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results,
            liveness.LivenessReport(),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("no_liveness_evidence", decision.codes)


class Approval(unittest.TestCase):
    def _ready(self):
        results, origins = _results()
        return cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )

    def _approval(self, decision, **kwargs):
        fields = {
            "approved_by": "maintainer",
            "approved_at": "2026-03-20T00:00:00Z",
            "evidence_digest": decision.evidence_digest,
            "canary_reference": "runs/canary-1",
            "rollback_reference": "workflows/opencode.yml@revert-9f2",
        }
        fields.update(kwargs)
        return cutover.Approval(**fields)

    def test_approval_needs_a_canary_and_a_rollback_path(self) -> None:
        # A window with everything except a rollback path.
        results, origins = _results()
        base = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("no_rollback", decision.codes)
        self.assertFalse(decision.approved)
        self.assertIn("not ready", decision.summary_line())

    def test_a_complete_approval_with_canary_and_rollback_approves(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        base = cutover.decide(
            results, live, origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            live,
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1", "result": "clean"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertTrue(decision.approved)
        self.assertTrue(decision.ready)
        self.assertIn("approved", decision.summary_line())
        self.assertEqual(decision.approval.approved_by, "maintainer")

    def test_an_approval_against_stale_evidence_is_refused(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        base = cutover.decide(
            results, live, origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        # The window gains an event after the approval was granted.
        extended = list(results) + [_result("merge-decision", correlation="corr-later")]
        origins["corr-later"] = "bridge"
        decision = cutover.decide(
            extended,
            _liveness([result.correlation_id for result in extended]),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("stale_approval", decision.codes)
        self.assertFalse(decision.approved)
        self.assertIn(base.evidence_digest, " ".join(decision.codes) + " ".join(b.message for b in decision.blockers))

    def test_a_signed_approval_cannot_approve_a_blocked_window(self) -> None:
        # The approval is complete, names a canary and a rollback, and its digest
        # was taken from this very window. It is still not an approval, because
        # the window has a stalled run in it, and a signature is not a waiver.
        results, origins = _results()
        stalled_id = "corr-issue-lifecycle"
        stalled = liveness.LivenessReport(
            runs=tuple(
                liveness.RunLiveness(
                    correlation_id=result.correlation_id,
                    verdict=(
                        liveness.STALLED
                        if result.correlation_id == stalled_id
                        else liveness.LIVE
                    ),
                    status=("timeout" if result.correlation_id == stalled_id else "ok"),
                    reason=(
                        "exceeded the 300000ms budget"
                        if result.correlation_id == stalled_id
                        else ""
                    ),
                    duration_ms=(900000 if result.correlation_id == stalled_id else 5),
                )
                for result in results
            )
        )
        # The approval is signed against this very window, stall and all.
        base = cutover.decide(
            results, stalled, origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            stalled,
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertFalse(decision.approved)
        self.assertFalse(decision.ready)
        self.assertIn("approval_over_blocked_window", decision.codes)
        self.assertIn("liveness_stalled", decision.codes)

    def test_an_incomplete_approval_is_refused(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        decision = cutover.decide(
            results,
            live,
            origins=origins,
            approval=cutover.Approval(approved_by="maintainer"),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("incomplete_approval", decision.codes)
        self.assertFalse(decision.approved)


class FromArtifacts(unittest.TestCase):
    def test_a_window_can_be_judged_from_documents_alone(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        decision = cutover.from_documents(
            [result.describe() for result in results],
            live.describe(),
            origins=origins,
            baseline_document=_clean_baseline().describe(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertTrue(decision.ready)

    def test_a_verdict_this_engine_does_not_know_is_refused(self) -> None:
        results, origins = _results()
        documents = [result.describe() for result in results]
        documents[0] = {**documents[0], "classification": "vibes"}
        with self.assertRaises(ValueError) as caught:
            cutover.from_documents(
                documents,
                _liveness([result.correlation_id for result in results]).describe(),
                origins=origins,
                baseline_document=_clean_baseline().describe(),
                window_started_at=WINDOW[0],
                window_ended_at=WINDOW[1],
            )
        self.assertIn("unknown_classification", str(caught.exception))

    def test_a_decision_document_round_trips(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        decision = cutover.decide(
            results, live, origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        document = decision.describe()
        self.assertEqual(cutover.describe(document), document)
        self.assertEqual(document["schema"], cutover.CUTOVER_SCHEMA)
        self.assertEqual(len(document["blockers"]), 0)
        self.assertTrue(document["ready"])
        self.assertEqual(document["coverage"]["required"], 8)

    def test_a_document_from_another_schema_is_refused(self) -> None:
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.describe({"schema": "continuum.shadow-cutover/v99"})
        self.assertEqual(caught.exception.code, "unknown_cutover_schema")

    def test_a_changed_verdict_summary_moves_the_digest(self) -> None:
        results, origins = _results()
        first = cutover.decide(
            results, _liveness([r.correlation_id for r in results]), origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        # Same event, same classification, different substance: the summary names
        # what actually differed, so an approval granted against the earlier
        # wording must not carry over.
        retitled = [
            dataclasses.replace(result, summary=result.summary + " (re-examined)")
            if index == 0
            else result
            for index, result in enumerate(results)
        ]
        second = cutover.decide(
            retitled, _liveness([r.correlation_id for r in results]), origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        self.assertNotEqual(first.evidence_digest, second.evidence_digest)

    def test_the_evidence_digest_ignores_delivery_order(self) -> None:
        results, origins = _results()
        forward = cutover.decide(
            results, _liveness([r.correlation_id for r in results]), origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        backward = cutover.decide(
            list(reversed(results)),
            _liveness([r.correlation_id for r in results]),
            origins=origins,
            baseline_report=_clean_baseline(),
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertEqual(forward.evidence_digest, backward.evidence_digest)


if __name__ == "__main__":
    unittest.main()
