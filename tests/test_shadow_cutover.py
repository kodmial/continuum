"""The cutover gate: what has to be true before #28 may remove NanoDictate.

The issue's own warning shapes this file: a run *count* is not the gate, scenario
coverage is. So the tests that matter are the ones where a window looks healthy by
count and is still refused -- a missing scenario, a scenario covered only by
replays, a divergence nobody resolved, a stalled run, and an approval granted
against evidence that has since moved.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from continuum.shadow import baseline, cutover, engine, liveness, parity, planner
from continuum.shadow.observation import outcome_from_payload
from tests import shadow_support as fixtures

WINDOW = ("2026-03-01T00:00:00Z", "2026-03-15T00:00:00Z")

ALL_REQUIRED = tuple(name for name, _ in cutover.REQUIRED_SCENARIOS)

#: The control-plane scenarios, which is what Phase A asks for.
CONTROL_PLANE_REQUIRED = tuple(
    name for name in ALL_REQUIRED if name != cutover.RELEASE_SCENARIO
)

#: "Not supplied", which is not the same as ``None`` -- the gate treats an absent
#: ledger differently from a caller that passed nothing at all.
_DEFAULT = object()


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


LEDGER_WORKFLOWS = ((".github/workflows/ci.yml", "a" * 40),)


def _baseline(*, workflows=LEDGER_WORKFLOWS, live_workflows=None, live=None):
    """A rolling baseline reading; by default one that matches its ledger.

    The gate refuses a window without one, so every test about *other* blockers has
    to supply a clean reading or it would be asserting on a window already refused
    for an unrelated reason. ``BaselineGate`` covers the requirement itself.
    """

    live_head = live or baseline.LiveHead(
        repository="kodmial/nanodictate",
        default_branch="main",
        head_sha="b" * 40,
        workflows=tuple(
            baseline.WorkflowFile(path=path, blob_sha=sha)
            for path, sha in (workflows if live_workflows is None else live_workflows)
        ),
    )
    ledger = baseline.read_ledger(
        {
            "schema": baseline.LEDGER_SCHEMA,
            "repository": "kodmial/nanodictate",
            "workflows": [
                {
                    "path": path,
                    "blob_sha": sha,
                    "classification": "consumer-local",
                    "owner": "#60",
                }
                for path, sha in workflows
            ],
        }
    )
    return baseline.audit(ledger, live_head)


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
            baseline=_baseline(),
            origins=origins,
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
            baseline=_baseline(),
            origins=origins,
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
            baseline=_baseline(),
            origins=origins,
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
            baseline=_baseline(),
            origins=origins,
            resolutions=[resolution],
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
            baseline=_baseline(),
            origins=origins,
            resolutions=[cutover.Resolution(correlation_id="corr-diverged", resolution="  ")],
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
            baseline=_baseline(),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("liveness_orphaned", decision.codes)

    def test_no_liveness_report_is_a_blocker_not_an_assumption(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results, None, baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        self.assertIn("no_liveness_evidence", decision.codes)

    def test_an_empty_liveness_report_is_not_evidence(self) -> None:
        results, origins = _results()
        decision = cutover.decide(
            results,
            liveness.LivenessReport(),
            baseline=_baseline(),
            origins=origins,
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
            baseline=_baseline(),
            origins=origins,
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
            baseline=_baseline(),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            baseline=_baseline(),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
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
            results, live, baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            live,
            baseline=_baseline(),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1", "result": "clean"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
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
            results, live, baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        # The window gains an event after the approval was granted.
        extended = list(results) + [_result("merge-decision", correlation="corr-later")]
        origins["corr-later"] = "bridge"
        decision = cutover.decide(
            extended,
            _liveness([result.correlation_id for result in extended]),
            baseline=_baseline(),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
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
            results, stalled, baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        decision = cutover.decide(
            results,
            stalled,
            baseline=_baseline(),
            origins=origins,
            approval=self._approval(base),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
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
            baseline=_baseline(),
            origins=origins,
            approval=cutover.Approval(approved_by="maintainer"),
            canary={"run": "runs/canary-1"},
            rollback={"reference": "workflows/opencode.yml@revert-9f2"},
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("incomplete_approval", decision.codes)
        self.assertFalse(decision.approved)


class BaselineGate(unittest.TestCase):
    """The rolling baseline rule: a snapshot alone can never authorize cutover."""

    def _decide(self, **kwargs):
        results, origins = _results()
        return cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
            **kwargs
        )

    def test_a_perfect_window_without_a_reading_is_refused(self) -> None:
        # Every scenario live, no divergence, nothing stalled. The one thing it has
        # not done is look at the consumer's workflows today, and that is the whole
        # point of the rule.
        decision = self._decide()
        self.assertIn("no_baseline_evidence", decision.codes)
        self.assertFalse(decision.ready)
        self.assertIn("historical snapshot", decision.summary_line())

    def test_a_workflow_that_moved_blocks_the_window(self) -> None:
        drifted = _baseline(
            live_workflows=((".github/workflows/ci.yml", "9" * 40),)
        )
        self.assertEqual(drifted.verdict, "drifted")
        decision = self._decide(baseline=drifted)
        self.assertIn("baseline_workflow_blob_changed", decision.codes)
        self.assertFalse(decision.ready)

    def test_a_workflow_the_ledger_never_audited_blocks_the_window(self) -> None:
        # An unclassified file is not evidence of absence. Ignoring it would make
        # the ledger's own completeness the thing being measured.
        report = _baseline(
            live_workflows=(
                (".github/workflows/ci.yml", "a" * 40),
                (".github/workflows/brand-new.yml", "c" * 40),
            )
        )
        self.assertIn("baseline_unclassified_workflow", self._decide(baseline=report).codes)

    def test_an_open_pull_request_nobody_classified_blocks_the_window(self) -> None:
        # The forward-compatibility half: a change that is not on main yet can
        # restore an orchestration writer the cutover removes.
        live = baseline.LiveHead(
            repository="kodmial/nanodictate",
            default_branch="main",
            head_sha="b" * 40,
            workflows=(baseline.WorkflowFile(".github/workflows/ci.yml", "a" * 40),),
            open_pull_requests=(
                baseline.OpenPullRequest(
                    number=91, head_sha="d" * 40, paths=(".github/workflows/issue-scheduler.yml",)
                ),
            ),
        )
        report = _baseline(live=live)
        decision = self._decide(baseline=report)
        self.assertIn("baseline_unclassified_open_pull_request", decision.codes)

    def test_an_incomplete_reading_blocks_without_claiming_drift(self) -> None:
        # A truncated inventory would otherwise report every unread file as
        # removed -- a claim about the gap rather than about the repository.
        partial = baseline.LiveHead(
            repository="kodmial/nanodictate",
            default_branch="main",
            complete=False,
            limits=("workflow inventory could not be read: 403",),
        )
        report = _baseline(live=partial)
        self.assertEqual(report.verdict, "incomplete")
        self.assertEqual(report.differences, ())
        decision = self._decide(baseline=report)
        self.assertEqual(
            [code for code in decision.codes if code.startswith("baseline_")],
            ["baseline_live_head_incomplete"],
        )

    def test_the_reading_is_in_the_digest_so_an_approval_cannot_outlive_it(self) -> None:
        # Two identical windows, one judged against a clean reading and one
        # against a reading of a moved consumer. An approval granted to the first
        # must not be honoured for the second.
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        clean = cutover.decide(
            results, live, baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        moved = cutover.decide(
            results,
            live,
            baseline=_baseline(live_workflows=((".github/workflows/ci.yml", "9" * 40),)),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertNotEqual(clean.evidence_digest, moved.evidence_digest)
        approval = cutover.Approval(
            approved_by="maintainer",
            approved_at="2026-03-20T00:00:00Z",
            evidence_digest=clean.evidence_digest,
            canary_reference="runs/canary-1",
            rollback_reference="workflows/issue-scheduler.yml@revert-1",
        )
        # The approval matches the clean window exactly...
        self.assertTrue(
            cutover.decide(
                results, live, baseline=_baseline(), origins=origins,
                approval=approval, canary={"run": "runs/canary-1"},
                rollback={"reference": "workflows/issue-scheduler.yml@revert-1"},
                window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
            ).approved
        )
        # ...and is stale the moment the consumer's workflows have moved.
        self.assertIn(
            "stale_approval",
            cutover.decide(
                results,
                live,
                baseline=_baseline(live_workflows=((".github/workflows/ci.yml", "9" * 40),)),
                origins=origins,
                approval=approval,
                canary={"run": "runs/canary-1"},
                rollback={"reference": "workflows/issue-scheduler.yml@revert-1"},
                window_started_at=WINDOW[0],
                window_ended_at=WINDOW[1],
            ).codes,
        )

    def test_the_report_round_trips_into_the_gate(self) -> None:
        document = _baseline(
            live_workflows=((".github/workflows/ci.yml", "9" * 40),)
        ).describe()
        self.assertEqual(baseline.read_report(document).describe(), document)
        decision = cutover.from_documents(
            [result.describe() for result in _results()[0]],
            _liveness([result.correlation_id for result in _results()[0]]).describe(),
            baseline_document=document,
            origins=_results()[1],
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("baseline_workflow_blob_changed", decision.codes)

    def test_a_report_this_engine_cannot_interpret_is_refused(self) -> None:
        for document in (
            {"schema": "continuum.shadow-baseline/v99"},
            {"schema": baseline.BASELINE_SCHEMA, "verdict": "probably"},
        ):
            with self.assertRaises(baseline.BaselineError):
                baseline.read_report(document)


class FromArtifacts(unittest.TestCase):
    def test_a_window_can_be_judged_from_documents_alone(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        decision = cutover.from_documents(
            [result.describe() for result in results],
            live.describe(),
            baseline_document=_baseline().describe(),
            origins=origins,
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
                baseline_document=_baseline().describe(),
                origins=origins,
                window_started_at=WINDOW[0],
                window_ended_at=WINDOW[1],
            )
        self.assertIn("unknown_classification", str(caught.exception))

    def test_a_decision_document_round_trips(self) -> None:
        results, origins = _results()
        live = _liveness([result.correlation_id for result in results])
        decision = cutover.decide(
            results, live, baseline=_baseline(), origins=origins,
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
            results, _liveness([r.correlation_id for r in results]), baseline=_baseline(), origins=origins,
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
            retitled, _liveness([r.correlation_id for r in results]), baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        self.assertNotEqual(first.evidence_digest, second.evidence_digest)

    def test_the_evidence_digest_ignores_delivery_order(self) -> None:
        results, origins = _results()
        forward = cutover.decide(
            results, _liveness([r.correlation_id for r in results]), baseline=_baseline(), origins=origins,
            window_started_at=WINDOW[0], window_ended_at=WINDOW[1],
        )
        backward = cutover.decide(
            list(reversed(results)),
            _liveness([r.correlation_id for r in results]),
            baseline=_baseline(),
            origins=origins,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertEqual(forward.evidence_digest, backward.evidence_digest)


class Phases(unittest.TestCase):
    """Phase A is the control-plane cutover; Phase B replaces the release writer too.

    The two assertions that matter are the ones the issue names: Phase A must not
    wait for release evidence it is not going to use, and Phase B must not cut over
    on a window that never showed one.
    """

    def _window(self, scenarios):
        results, origins = _results(scenarios)
        return results, origins, _liveness(
            [result.correlation_id for result in results]
        )

    def test_phase_a_does_not_require_the_release_scenario(self) -> None:
        required = dict(cutover.required_scenarios(cutover.PHASE_A))
        self.assertEqual(set(required), set(CONTROL_PLANE_REQUIRED))
        self.assertNotIn(cutover.RELEASE_SCENARIO, required)
        # Every class it does require still carries its reason.
        for name, reason in required.items():
            self.assertTrue(reason.strip(), name)

    def test_phase_b_requires_the_release_scenario(self) -> None:
        self.assertEqual(
            cutover.required_scenarios(cutover.PHASE_B), cutover.REQUIRED_SCENARIOS
        )

    def test_the_strict_phase_is_the_default(self) -> None:
        # An unnamed phase gets the full list, so nothing is loosened by omission.
        self.assertEqual(cutover.DEFAULT_PHASE, cutover.PHASE_B)
        self.assertEqual(cutover.required_scenarios(), cutover.REQUIRED_SCENARIOS)
        self.assertEqual(len(cutover.required_scenarios()), 8)

    def test_a_phase_nobody_declared_is_refused(self) -> None:
        for call in (
            lambda: cutover.required_scenarios("phase-c"),
            lambda: cutover.phase_writers("phase-c"),
        ):
            with self.assertRaises(cutover.CutoverError) as caught:
                call()
            self.assertEqual(caught.exception.code, "unknown_cutover_phase")

    def test_phase_a_is_ready_without_release_evidence(self) -> None:
        results, origins, live = self._window(CONTROL_PLANE_REQUIRED)
        decision = cutover.decide(
            results,
            live,
            baseline=_baseline(),
            origins=origins,
            phase=cutover.PHASE_A,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertEqual(decision.blockers, ())
        self.assertTrue(decision.ready)
        self.assertEqual(decision.phase, cutover.PHASE_A)
        # Sufficient is not authorized: nothing has said this window may be acted on.
        self.assertFalse(decision.authorized)

    def test_the_same_window_is_refused_in_phase_b(self) -> None:
        results, origins, live = self._window(CONTROL_PLANE_REQUIRED)
        decision = cutover.decide(
            results,
            live,
            baseline=_baseline(),
            origins=origins,
            phase=cutover.PHASE_B,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        blockers = [b for b in decision.blockers if b.code == "uncovered_scenario"]
        self.assertEqual([b.scenario for b in blockers], [cutover.RELEASE_SCENARIO])
        self.assertFalse(decision.ready)

    def test_release_planning_is_still_reported_in_phase_a(self) -> None:
        # Observed, not demanded: a reader still sees that it happened.
        results, origins, live = self._window(ALL_REQUIRED)
        report = cutover.coverage(
            results, origins=origins, phase=cutover.PHASE_A, window_started_at=WINDOW[0]
        )
        entry = report.for_scenario(cutover.RELEASE_SCENARIO)
        self.assertEqual(entry.state, "live")
        self.assertFalse(entry.required)
        self.assertIn(cutover.RELEASE_SCENARIO, report.optional_seen)
        self.assertEqual(report.describe()["phase"], cutover.PHASE_A)

    def test_the_phase_travels_in_the_digest(self) -> None:
        # Otherwise an authorization issued for the control-plane phase could be
        # spent on the release phase, where the same events are not enough.
        results, origins, live = self._window(ALL_REQUIRED)
        digests = {}
        for phase in cutover.PHASES:
            decision = cutover.decide(
                results,
                live,
                baseline=_baseline(),
                origins=origins,
                phase=phase,
                window_started_at=WINDOW[0],
                window_ended_at=WINDOW[1],
            )
            digests[phase] = decision.evidence_digest
        self.assertEqual(len(set(digests.values())), 2, digests)

    def test_a_phase_only_replaces_the_writers_it_names(self) -> None:
        self.assertEqual(
            cutover.phase_writers(cutover.PHASE_A), baseline.CONTROL_PLANE_WRITERS
        )
        self.assertNotIn(baseline.RELEASE_WRITER, cutover.phase_writers(cutover.PHASE_A))
        self.assertIn(
            baseline.RELEASE_WRITER, cutover.phase_writers(cutover.PHASE_B)
        )
        # Neither phase retires a workflow that implements no writer it knows.
        for phase in cutover.PHASES:
            self.assertNotIn(baseline.WRITER_OTHER, cutover.phase_writers(phase))


class PhaseScope(unittest.TestCase):
    """What the atomic cutover may change, in the phase being judged.

    Checked against the ledger's writer roles rather than the change set's own
    claims: a control-plane cutover that renamed `release.yml` in its change set
    would otherwise pass by calling it a merge workflow.
    """

    LEDGER = "docs/parity-ledger.json"

    def setUp(self) -> None:
        self.ledger = baseline.load_ledger(self.LEDGER)
        self.cutover_head = "c" * 40

    def _change_set(self, *changes, **kwargs):
        return cutover.ChangeSet(
            cutover_head=kwargs.pop("cutover_head", self.cutover_head),
            phase=kwargs.pop("phase", ""),
            changes=tuple(
                cutover.FileChange(path=path, action=action, writer=writer)
                for path, action, writer in changes
            ),
        )

    def _decide(self, change_set, *, phase=cutover.PHASE_A, ledger=_DEFAULT, **kwargs):
        results, origins = _results(
            CONTROL_PLANE_REQUIRED if phase == cutover.PHASE_A else ALL_REQUIRED
        )
        return cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            baseline=_baseline(),
            origins=origins,
            change_set=change_set,
            ledger=self.ledger if ledger is _DEFAULT else ledger,
            phase=phase,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
            **kwargs
        )

    def test_the_shipped_ledger_names_a_writer_for_every_workflow(self) -> None:
        # The gate refuses a path with no role, so a ledger that leaves any blank
        # is a ledger that cannot judge its own consumer's cutover.
        for entry in self.ledger.workflows:
            self.assertIn(entry.writer, baseline.WRITER_ROLES, entry.path)
        roles = {entry.writer for entry in self.ledger.workflows}
        for expected in (baseline.CONTROL_PLANE_WRITERS + (baseline.RELEASE_WRITER,)):
            self.assertIn(expected, roles)

    def test_phase_a_may_replace_the_control_plane_writers(self) -> None:
        decision = self._decide(
            self._change_set(
                (".github/workflows/issue-scheduler.yml", "remove", "scheduler"),
                (".github/workflows/opencode.yml", "remove", "opencode"),
                (".github/workflows/auto-merge.yml", "disable", "merge"),
            )
        )
        self.assertEqual(decision.blockers, ())
        self.assertTrue(decision.ready)

    def test_phase_a_may_not_remove_a_release_workflow(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/release.yml", "remove", "release"))
        )
        self.assertIn("release_writer_in_phase_a", decision.codes)
        self.assertFalse(decision.ready)

    def test_phase_a_may_not_mutate_a_release_workflow_either(self) -> None:
        # The rule is about who owns the writer, not about deleting it: a Phase A
        # cutover that edited release.yml would already have taken the decision
        # that belongs to the release phase.
        for action in ("modify", "add", "disable", "retain"):
            with self.subTest(action=action):
                decision = self._decide(
                    self._change_set(
                        (".github/workflows/release-pr.yml", action, "release")
                    )
                )
                self.assertIn("release_writer_in_phase_a", decision.codes)

    def test_a_change_set_cannot_redeclare_a_release_workflow(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/release.yml", "remove", "merge"))
        )
        self.assertIn("writer_role_disagrees_with_ledger", decision.codes)
        self.assertNotIn("release_writer_in_phase_a", decision.codes)

    def test_a_path_the_ledger_never_classified_is_refused(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/brand-new.yml", "remove", "scheduler"))
        )
        self.assertIn("unclassified_cutover_path", decision.codes)

    def test_a_change_that_names_no_writer_is_refused(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/issue-scheduler.yml", "remove", ""))
        )
        self.assertIn("undeclared_writer_role", decision.codes)

    def test_phase_a_may_not_retire_a_writer_it_does_not_replace(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/ci.yml", "remove", "other"))
        )
        self.assertIn("out_of_phase_removal", decision.codes)

    def test_phase_b_may_replace_the_release_writer(self) -> None:
        decision = self._decide(
            self._change_set((".github/workflows/release.yml", "remove", "release")),
            phase=cutover.PHASE_B,
        )
        self.assertNotIn("release_writer_in_phase_a", decision.codes)
        self.assertEqual(decision.blockers, ())
        self.assertTrue(decision.ready)

    def test_phase_b_still_needs_the_release_evidence_it_is_replacing(self) -> None:
        # The scope check passing is not the gate: the release phase replaced the
        # release writer, so it has to have watched it work.
        results, origins = _results(CONTROL_PLANE_REQUIRED)
        decision = cutover.decide(
            results,
            _liveness([result.correlation_id for result in results]),
            baseline=_baseline(),
            origins=origins,
            change_set=self._change_set(
                (".github/workflows/release.yml", "remove", "release")
            ),
            ledger=self.ledger,
            phase=cutover.PHASE_B,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("uncovered_scenario", decision.codes)
        self.assertFalse(decision.ready)

    def test_the_scope_is_judged_without_a_ledger_by_nobody(self) -> None:
        decision = self._decide(
            self._change_set(
                (".github/workflows/issue-scheduler.yml", "remove", "scheduler")
            ),
            ledger=None,
        )
        self.assertIn("no_ledger_evidence", decision.codes)

    def test_a_change_set_written_for_another_phase_is_refused(self) -> None:
        decision = self._decide(
            self._change_set(
                (".github/workflows/issue-scheduler.yml", "remove", "scheduler"),
                phase=cutover.PHASE_B,
            )
        )
        self.assertIn("change_set_phase_mismatch", decision.codes)

    def test_a_change_set_must_name_a_commit(self) -> None:
        for head in ("", "main", "c" * 7):
            with self.subTest(head=head):
                with self.assertRaises(cutover.CutoverError) as caught:
                    cutover.read_change_set(
                        {
                            "schema": cutover.CHANGE_SET_SCHEMA,
                            "cutover_head": head,
                            "changes": [],
                        }
                    )
                self.assertEqual(caught.exception.code, "change_set_head_not_a_commit")

    def test_a_change_set_this_gate_cannot_read_is_refused(self) -> None:
        for document in (
            {"schema": "continuum.shadow-change-set/v99", "cutover_head": "c" * 40},
            {"cutover_head": "c" * 40},
            {
                "schema": cutover.CHANGE_SET_SCHEMA,
                "cutover_head": "c" * 40,
                "changes": [{"path": ".github/workflows/ci.yml", "action": "delete"}],
            },
            {
                "schema": cutover.CHANGE_SET_SCHEMA,
                "cutover_head": "c" * 40,
                "changes": [
                    {"path": ".github/workflows/ci.yml", "action": "remove", "writer": "ci"}
                ],
            },
            {
                "schema": cutover.CHANGE_SET_SCHEMA,
                "cutover_head": "c" * 40,
                "changes": [
                    {"path": ".github/workflows/ci.yml", "action": "remove", "writer": "other"},
                    {"path": ".github/workflows/ci.yml", "action": "modify", "writer": "other"},
                ],
            },
        ):
            with self.assertRaises(cutover.CutoverError):
                cutover.read_change_set(document)

    def test_a_change_set_round_trips(self) -> None:
        change_set = self._change_set(
            (".github/workflows/issue-scheduler.yml", "remove", "scheduler"),
            phase=cutover.PHASE_A,
        )
        self.assertEqual(cutover.read_change_set(change_set.describe()), change_set)


class Authorization(unittest.TestCase):
    """The trusted controller's authorization, bound to everything it named.

    Each test moves exactly one binding and asserts the code that names it: the
    whole point of the record is that a reader can tell *what* moved.
    """

    def setUp(self) -> None:
        self.phase = cutover.PHASE_A
        self.results, self.origins = _results(CONTROL_PLANE_REQUIRED)
        self.live = _liveness(
            [result.correlation_id for result in self.results]
        )
        self.baseline = _baseline()
        self.change_set = cutover.ChangeSet(
            cutover_head="c" * 40,
            phase=self.phase,
            changes=(
                cutover.FileChange(
                    path=".github/workflows/issue-scheduler.yml",
                    action="remove",
                    writer="scheduler",
                ),
            ),
        )
        self.canary = {"reference": "runs/canary-1", "result": "clean"}
        self.rollback = {"reference": "workflows/opencode.yml@revert-9f2"}
        self.ledger = baseline.load_ledger("docs/parity-ledger.json")

    def _decide(self, authorization=None, **kwargs):
        """Judge the window, optionally with a record attached."""

        defaults = {
            "baseline": self.baseline,
            "origins": self.origins,
            "change_set": self.change_set,
            "ledger": self.ledger,
            "canary": self.canary,
            "rollback": self.rollback,
            "phase": self.phase,
            "window_started_at": WINDOW[0],
            "window_ended_at": WINDOW[1],
        }
        defaults.update(kwargs)
        return cutover.decide(
            self.results, self.live, authorization=authorization, **defaults
        )

    def _digest(self, **kwargs) -> str:
        return self._decide(**kwargs).evidence_digest

    def _authorization(self, **overrides) -> cutover.Authorization:
        """A record bound to this window exactly as it stands right now."""

        fields = {
            "phase": self.phase,
            "authorized_by": "continuum-cutover-controller",
            "authorized_at": "2026-03-20T00:00:00Z",
            "evidence_digest": self._digest(),
            "baseline_digest": self.baseline.evidence_digest,
            "consumer_head": self.baseline.live_head,
            "controller_sha": engine.engine_sha(),
            "canary_reference": self.canary["reference"],
            "rollback_reference": self.rollback["reference"],
            "cutover_head": self.change_set.cutover_head,
        }
        fields.update(overrides)
        return cutover.Authorization(**fields)

    def _expect_stale(self, code, authorization, **kwargs):
        decision = self._decide(authorization, **kwargs)
        self.assertIn(code, decision.codes)
        self.assertFalse(decision.authorized)
        self.assertFalse(decision.ready)
        return decision

    def test_a_complete_authorization_authorizes_the_window(self) -> None:
        decision = self._decide(self._authorization())
        self.assertEqual(decision.blockers, ())
        self.assertTrue(decision.ready)
        self.assertTrue(decision.authorized)
        self.assertIn("authorized for {}".format(self.phase), decision.summary_line())
        self.assertEqual(
            decision.describe()["phase_writers"], list(baseline.CONTROL_PLANE_WRITERS)
        )

    def test_a_zero_touch_cutover_carries_no_human_record(self) -> None:
        # What makes this zero-touch: nothing in the decision names a person, so
        # there is no comment to leave and no click to wait for between READY and
        # the expected-head merge.
        decision = self._decide(self._authorization())
        self.assertTrue(decision.authorized)
        self.assertFalse(decision.approved)
        self.assertIsNone(decision.approval)
        document = decision.describe()
        self.assertIsNone(document["approval"])
        self.assertNotIn("approved_by", json.dumps(document["authorization"]))

    def test_the_controller_binding_names_the_engine_running_the_gate(self) -> None:
        # The gate attests to its own commit, because it cannot attest to anything
        # else: an authorization for one engine is not a statement about another.
        self.assertEqual(
            self._decide(self._authorization()).authorized, True
        )
        self._expect_stale("stale_controller", self._authorization(controller_sha="f" * 40))
        # And no caller can name a different controller for the gate to accept.
        self.assertFalse(
            self._decide(
                self._authorization(), controller_sha="not-a-commit"
            ).authorized
        )

    def test_an_authorization_expires_when_the_evidence_moves(self) -> None:
        authorization = self._authorization()
        # The window gains a run after the controller issued the record.
        moved = list(self.results) + [_result("merge-decision", correlation="corr-later")]
        decision = cutover.decide(
            moved,
            self.live,
            authorization=authorization,
            baseline=self.baseline,
            origins={**self.origins, "corr-later": "bridge"},
            change_set=self.change_set,
            canary=self.canary,
            rollback=self.rollback,
            phase=self.phase,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("stale_authorization", decision.codes)
        self.assertFalse(decision.authorized)

    def test_an_authorization_expires_when_the_consumer_head_moves(self) -> None:
        moved = _baseline(live=baseline.LiveHead(
            repository="kodmial/nanodictate",
            default_branch="main",
            head_sha="9" * 40,
            workflows=tuple(
                baseline.WorkflowFile(path=path, blob_sha=sha)
                for path, sha in LEDGER_WORKFLOWS
            ),
        ))
        self.assertTrue(moved.ready)
        self.assertNotEqual(moved.live_head, self.baseline.live_head)
        self._expect_stale(
            "stale_consumer_head",
            self._authorization(),
            baseline=moved,
        )

    def test_an_authorization_expires_when_the_baseline_reading_moves(self) -> None:
        drifted = _baseline(live_workflows=((".github/workflows/ci.yml", "9" * 40),))
        self._expect_stale(
            "stale_baseline_binding", self._authorization(), baseline=drifted
        )

    def test_an_authorization_expires_when_the_cutover_head_moves(self) -> None:
        self._expect_stale(
            "stale_cutover_head",
            self._authorization(),
            change_set=dataclasses.replace(
                self.change_set, cutover_head="d" * 40
            ),
        )

    def test_an_authorization_expires_when_the_canary_moves(self) -> None:
        self._expect_stale(
            "stale_canary_binding",
            self._authorization(),
            canary={"reference": "runs/canary-2", "result": "clean"},
        )

    def test_an_authorization_expires_when_the_rollback_target_moves(self) -> None:
        self._expect_stale(
            "stale_rollback_binding",
            self._authorization(),
            rollback={"reference": "workflows/opencode.yml@revert-0000"},
        )

    def test_a_binding_that_is_not_a_commit_is_a_missing_binding(self) -> None:
        authorization = self._authorization(controller_sha="main")
        self.assertIn("controller_sha", authorization.missing)
        decision = self._decide(authorization)
        self.assertIn("incomplete_authorization", decision.codes)
        self.assertIn("controller_sha", " ".join(decision.summary_line().split()))
        self.assertFalse(decision.authorized)

    def test_an_incomplete_authorization_names_every_gap(self) -> None:
        decision = self._decide(cutover.Authorization(phase=self.phase))
        self.assertEqual(decision.codes, ("incomplete_authorization",))
        message = decision.blockers[0].message
        for gap in cutover.Authorization().missing:
            self.assertIn(gap, message)

    def test_an_authorization_for_another_phase_is_refused(self) -> None:
        self._expect_stale(
            "authorization_phase_mismatch",
            self._authorization(phase=cutover.PHASE_B),
        )

    def test_an_authorization_for_a_phase_nobody_declared_is_refused(self) -> None:
        decision = self._decide(self._authorization(phase="phase-c"))
        self.assertEqual(decision.codes, ("authorization_phase_unknown",))
        self.assertFalse(decision.authorized)

    def test_an_authorization_over_a_blocked_window_is_not_an_authorization(self) -> None:
        # A trusted controller, a complete record, every binding matching, and a
        # window whose cutover would remove the consumer's release writer: still not
        # authorized. The scope blocker does not move the evidence digest, so the
        # record still matches everything it names -- and a signature is not a
        # waiver, here or in the human path.
        authorization = self._authorization()
        self.assertTrue(authorization.complete)
        decision = self._decide(
            authorization,
            change_set=cutover.ChangeSet(
                cutover_head=self.change_set.cutover_head,
                phase=self.phase,
                changes=(
                    cutover.FileChange(
                        path=".github/workflows/release.yml",
                        action="remove",
                        writer="release",
                    ),
                ),
            ),
        )
        self.assertIn("release_writer_in_phase_a", decision.codes)
        self.assertIn("authorization_over_blocked_window", decision.codes)
        self.assertFalse(decision.authorized)
        self.assertFalse(decision.ready)
        self.assertNotIn("stale_authorization", decision.codes)

    def test_a_record_cannot_waive_a_window_whose_evidence_moved(self) -> None:
        # The same rule from the other side: here the window itself moved, so the
        # record is stale, and neither the stale record nor the stall is waived.
        stalled = liveness.LivenessReport(
            runs=tuple(
                liveness.RunLiveness(
                    correlation_id=result.correlation_id,
                    verdict=(
                        liveness.STALLED
                        if result.correlation_id == "corr-issue-lifecycle"
                        else liveness.LIVE
                    ),
                    status=(
                        "timeout" if result.correlation_id == "corr-issue-lifecycle" else "ok"
                    ),
                    reason=(
                        "exceeded the 300000ms budget"
                        if result.correlation_id == "corr-issue-lifecycle"
                        else ""
                    ),
                    duration_ms=(
                        900000 if result.correlation_id == "corr-issue-lifecycle" else 5
                    ),
                )
                for result in self.results
            )
        )
        decision = cutover.decide(
            self.results,
            stalled,
            authorization=self._authorization(),
            baseline=self.baseline,
            origins=self.origins,
            change_set=self.change_set,
            ledger=self.ledger,
            canary=self.canary,
            rollback=self.rollback,
            phase=self.phase,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertIn("stale_authorization", decision.codes)
        self.assertIn("liveness_stalled", decision.codes)
        self.assertFalse(decision.authorized)

    def test_an_authorization_with_no_change_set_is_refused(self) -> None:
        # The record names a pull request head, so something has to describe it.
        decision = self._decide(self._authorization(), change_set=None)
        self.assertIn("no_cutover_change_set", decision.codes)
        self.assertFalse(decision.authorized)

    def test_a_window_with_no_record_is_ready_but_not_authorized(self) -> None:
        decision = self._decide()
        self.assertTrue(decision.ready)
        self.assertFalse(decision.approved)
        self.assertFalse(decision.authorized)
        self.assertIn("awaiting a recorded approval", decision.summary_line())

    def test_an_authorization_needs_a_canary_and_a_rollback(self) -> None:
        for missing, code in (("canary", "no_canary"), ("rollback", "no_rollback")):
            with self.subTest(missing=missing):
                decision = self._decide(self._authorization(), **{missing: None})
                self.assertIn(code, decision.codes)
                self.assertFalse(decision.authorized)

    def test_an_authorization_document_round_trips(self) -> None:
        authorization = self._authorization()
        self.assertEqual(
            cutover.read_authorization(authorization.describe()), authorization
        )
        for document in (
            {"schema": "continuum.shadow-authorization/v99"},
            {"evidence_digest": authorization.evidence_digest},
        ):
            with self.assertRaises(cutover.CutoverError):
                cutover.read_authorization(document)
        # A document that parses but binds nothing is not a parse failure: it is an
        # incomplete record, and the gate says so by name.
        incomplete = cutover.read_authorization(
            {"schema": cutover.AUTHORIZATION_SCHEMA, "kind": "authorization"}
        )
        self.assertFalse(incomplete.complete)
        self.assertIn("controller_sha", incomplete.missing)

    def test_the_decision_carries_the_phase_the_phase_and_the_records(self) -> None:
        authorization = self._authorization()
        decision = self._decide(authorization)
        document = decision.describe()
        self.assertTrue(document["authorized"])
        self.assertEqual(document["phase"], self.phase)
        self.assertEqual(document["phase_writers"], list(baseline.CONTROL_PLANE_WRITERS))
        self.assertEqual(document["change_set"], self.change_set.describe())
        self.assertEqual(document["authorization"], authorization.describe())
        # And it survives the trip through a document, which is how CI reads it.
        self.assertEqual(cutover.describe(document), document)

    def test_a_whole_window_is_authorized_from_its_documents(self) -> None:
        results, origins = _results(CONTROL_PLANE_REQUIRED)
        live = _liveness([result.correlation_id for result in results])
        reading = self.baseline
        change_set = self.change_set
        ledger = baseline.load_ledger("docs/parity-ledger.json")
        decision = cutover.from_documents(
            [result.describe() for result in results],
            live.describe(),
            baseline_document=reading.describe(),
            ledger_document=ledger.describe(),
            change_set_document=change_set.describe(),
            authorization_document=self._authorization().describe(),
            canary=self.canary,
            rollback=self.rollback,
            origins=origins,
            phase=self.phase,
            window_started_at=WINDOW[0],
            window_ended_at=WINDOW[1],
        )
        self.assertTrue(decision.authorized, [b.code for b in decision.blockers])
        self.assertEqual(decision.change_set, change_set)


if __name__ == "__main__":
    unittest.main()
