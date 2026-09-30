"""The shadow plane driven the way CI drives it: through the command line.

Every other shadow test calls the modules directly, which proves they work. This
one proves they are *reachable* -- that the bridge's invocation parses, that the
artifacts land where the next step looks for them, that the exit codes separate
"Continuum said no" from "the plane could not record an answer", and that a
deliberate write is refused and recorded.

The exit codes are the part worth testing hardest. A CI job that fails when
Continuum refuses a merge is a CI job that gets disabled, and a job that passes
when the shadow run crashed is a job that certifies a broken plane.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from continuum.shadow import cli
from continuum.shadow.journal import journal_from_payload
from continuum.shadow.state import state_from_payload
from tests import shadow_support as fixtures

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = "shadow-cli-out"
HEAD_E = "e" * 40


def _cli(*args: str) -> subprocess.CompletedProcess:
    """Run the CLI in a process, because the barrier is per-process.

    In-process would be faster and would prove less: the audit hook cannot be
    uninstalled, so the first command in a test process would constrain every
    later one.
    """

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "continuum.shadow.cli", *args],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
    )


def _write(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class RunCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "run"
        self.event = _write(
            self.dir / "event.json",
            {
                "schema": "continuum.shadow-event/v1",
                **fixtures.event_for("ci-repair").describe(),
            },
        )
        self.state = _write(self.dir / "state.json", fixtures.state().as_capture())

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "run")

    def test_a_real_event_produces_a_journal_and_a_replay_case(self) -> None:
        result = _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--save-case",
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ci-repair", result.stdout)
        self.assertIn("update-branch", result.stdout)

        journals = sorted((self.dir / "out" / "journals").glob("*.json"))
        self.assertEqual(len(journals), 1)
        journal = journal_from_payload(json.loads(journals[0].read_text()))
        self.assertEqual(journal.status, "ok")
        self.assertEqual(journal.decision, "update-branch")
        self.assertTrue(journal.actions)
        self.assertTrue(journal.suppressed)
        self.assertTrue(journal.engine["engine_sha"])
        self.assertEqual(journal.config["origin"], "default")

        cases = sorted((self.dir / "out" / "cases").glob("*.case.json"))
        self.assertEqual(len(cases), 1)
        case = json.loads(cases[0].read_text())
        self.assertEqual(case["origin"], "bridge")
        self.assertEqual(case["recorded"]["decision"], "update-branch")

    def test_a_refusal_is_a_successful_run(self) -> None:
        # An untrusted fork: the run must record the refusal and exit zero, or CI
        # will learn to ignore the exit code.
        _write(
            self.dir / "fork-event.json",
            {
                "schema": "continuum.shadow-event/v1",
                **fixtures.pull_synchronize_fork().describe(),
            },
        )
        result = _cli(
            "run",
            "--event", str(self.dir / "fork-event.json"),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("denied", result.stdout)
        journal = journal_from_payload(
            json.loads(sorted((self.dir / "out" / "journals").glob("*.json"))[0].read_text())
        )
        self.assertEqual(journal.decision, "fork_pull_request")
        # The refusal is recorded as a *planned* effect too: "Continuum would not
        # have dispatched" is a parity claim, and it has to be in the artifact.
        self.assertEqual(journal.actions[0].detail["refused"], "fork_pull_request")

    def test_a_run_with_no_state_refuses_to_guess(self) -> None:
        result = _cli(
            "run",
            "--event", str(self.event),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, cli.PLANE_FAILURE)
        self.assertIn("no_state", result.stderr)

    def test_an_incomplete_capture_denies_rather_than_defaulting(self) -> None:
        state = json.loads(self.state.read_text())
        state["unreadable"] = {"review_threads:43": "graphql unavailable"}
        _write(self.state, state)
        result = _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("incomplete_capture", result.stdout)
        journal = journal_from_payload(
            json.loads(sorted((self.dir / "out" / "journals").glob("*.json"))[0].read_text())
        )
        self.assertEqual(journal.status, "denied")
        self.assertEqual(journal.actions, ())

    def test_a_run_over_its_budget_is_reported_as_a_stall(self) -> None:
        result = _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--budget-ms", "0",
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        self.assertIn("timeout", result.stdout)
        journal = journal_from_payload(
            json.loads(sorted((self.dir / "out" / "journals").glob("*.json"))[0].read_text())
        )
        self.assertEqual(journal.status, "timeout")
        self.assertEqual(journal.decision, "budget_exceeded")

    def test_an_unshadowable_event_is_recorded_as_a_rejection(self) -> None:
        _write(self.event, {"schema": "continuum.shadow-event/v1", "correlation_id": "x",
                            "repository": "acme/widgets", "event_type": "nonsense.event"})
        result = _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, cli.PLANE_FAILURE)
        self.assertIn("unknown_event_type", result.stderr)


class RecordedReleasePin(unittest.TestCase):
    """A recorded capture pins the release event it resolved.

    A release webhook names a tag, and a release contract needs a commit, so the
    capture resolves one into the other. Replaying that capture has to use the
    commit the capture resolved: looking it up again would mean the evidence
    changes between the run that recorded it and the run that replays it, and an
    approval that bound a digest to the first run would be approving a decision
    the second run never made.
    """

    RECORDED = "b" * 40

    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "release-pin"
        self.event = _write(
            self.dir / "event.json",
            {
                "schema": "continuum.shadow-event/v1",
                # The bridge shape: a tag, and no commit. A commit here would mean
                # the production webhook started naming one, which is a change to
                # the event contract, not to the capture.
                **fixtures.event(
                    "release.event",
                    action="published",
                    correlation_id="e-release-unpinned",
                    release={"tag": "v0.1.0", "version": "0.1.0", "dry_run": True},
                ).describe(),
            },
        )
        self.state = _write(
            self.dir / "state.json",
            dataclasses.replace(fixtures.state(), release_commit=self.RECORDED).as_capture(),
        )

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "release-pin")

    def _journal(self) -> dict:
        journals = sorted((self.dir / "out" / "journals").glob("*.json"))
        self.assertEqual(len(journals), 1, "expected exactly one journal")
        return json.loads(journals[0].read_text(encoding="utf-8"))

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
            *extra,
        )

    def test_the_captured_commit_reaches_the_journal(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        event = self._journal()["event"]
        self.assertEqual(event["release"]["source_sha"], self.RECORDED)
        self.assertEqual(event["release"]["tag"], "v0.1.0")

    def test_it_is_the_capture_s_commit_and_not_a_second_resolution(self) -> None:
        # No token, no --capture-live, and the run still plans: nothing was read.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self._journal()["observed"]
        self.assertEqual(observed["release_commit"], self.RECORDED)

    def test_a_capture_that_agrees_with_the_event_changes_nothing(self) -> None:
        # The same commit, recorded in both places: nothing to refuse, nothing to
        # change.
        agreeing = _write(
            self.dir / "agreeing-state.json",
            dataclasses.replace(fixtures.state(), release_commit=HEAD_E).as_capture(),
        )
        pinned = _write(
            self.dir / "pinned.json",
            {
                "schema": "continuum.shadow-event/v1",
                **fixtures.release_event().describe(),
            },
        )
        result = _cli(
            "run",
            "--event", str(pinned),
            "--state", str(agreeing),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._journal()["event"]["release"]["source_sha"], HEAD_E)

    def test_two_recorded_commits_that_disagree_refuse_the_run(self) -> None:
        pinned = _write(
            self.dir / "pinned.json",
            {
                "schema": "continuum.shadow-event/v1",
                **fixtures.release_event().describe(),
            },
        )
        result = _cli(
            "run",
            "--event", str(pinned),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("release_pin_conflict", result.stderr + result.stdout)
        # And it left no journal: a refusal that recorded a decision would be a
        # decision the operator never made.
        self.assertEqual(list((self.dir / "out" / "journals").glob("*.json")), [])

    def test_a_captured_commit_does_not_touch_another_event(self) -> None:
        # The same state, an event that is not a release: a commit the capture
        # happened to resolve is not a fact about an unrelated decision.
        other = _write(
            self.dir / "ci.json",
            {"schema": "continuum.shadow-event/v1", **fixtures.ci_repair_behind().describe()},
        )
        result = _cli(
            "run",
            "--event", str(other),
            "--state", str(self.state),
            "--out", str(self.dir / "out"),
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = self._journal()["event"]
        self.assertEqual(event["release"], {})


class ParityCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "parity"
        self.journal = fixtures.journal("ci-repair") if hasattr(fixtures, "journal") else None

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "parity")

    def _journal_document(self, *, agree: bool):
        from continuum.shadow import planner

        planned = planner.plan(
            fixtures.event_for("merge-decision"), fixtures.state(), fixtures.config(), now_ms=1
        )
        actions = [effect.describe() for effect in planned.actions]
        if not agree:
            actions = actions[1:]
        return planned.describe(), {
            "correlation_id": planned.event.correlation_id,
            "fidelity": "exact",
            "terminal_state": planned.status,
            "decision": planned.decision,
            "actions": actions,
        }

    def test_agreement_exits_zero_and_records_the_verdict(self) -> None:
        journal, observed = self._journal_document(agree=True)
        journal_path = _write(self.dir / "journal.json", journal)
        observed_path = _write(self.dir / "observed.json", observed)
        result = _cli(
            "parity",
            "--journal", str(journal_path),
            "--observed", str(observed_path),
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exact", result.stdout)
        verdicts = sorted((self.dir / "out" / "parity").glob("*.json"))
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(json.loads(verdicts[0].read_text())["classification"], "exact")

    def test_a_missing_action_fails_the_job_and_says_which(self) -> None:
        journal, observed = self._journal_document(agree=False)
        journal_path = _write(self.dir / "journal.json", journal)
        observed_path = _write(self.dir / "observed.json", observed)
        result = _cli(
            "parity",
            "--journal", str(journal_path),
            "--observed", str(observed_path),
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        self.assertIn("missing", result.stdout)


class LivenessCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "liveness"

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "liveness")

    def test_an_orphaned_event_fails_the_window(self) -> None:
        acceptance = _write(
            self.dir / "acceptance.json",
            {"correlation_id": "corr-1", "scenario": "ci-repair", "accepted_at_ms": 1_000},
        )
        result = _cli(
            "liveness",
            "--acceptances", str(acceptance),
            "--out", str(self.dir / "out"),
            "--now-ms", "61000",
        )
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        self.assertIn("0/1 answered", result.stdout)
        report = json.loads(
            (self.dir / "out" / "liveness" / "61000.report.json").read_text()
        )
        self.assertEqual(report["by_verdict"]["orphaned"], 1)
        self.assertFalse(report["clean"])

    def test_a_window_with_nothing_in_it_is_not_clean(self) -> None:
        result = _cli("liveness", "--out", str(self.dir / "out"), "--now-ms", "61000")
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        self.assertIn("no runs accepted", result.stdout)


class ReplayCommand(unittest.TestCase):
    def setUp(self) -> None:
        from continuum.shadow import planner, replay

        self.dir = Path(ARTIFACTS) / "replay"
        self.journal = planner.plan(
            fixtures.event_for("ci-repair"), fixtures.state(), fixtures.config(), now_ms=1
        )
        self.case_path = _write(
            self.dir / "case.json", replay.build_case(self.journal).describe()
        )

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "replay")

    def test_a_case_replays_on_this_engine(self) -> None:
        result = _cli(
            "replay", "--case", str(self.case_path), "--out", str(self.dir / "out"), "--now-ms", "2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reproduced", result.stdout)
        documents = sorted((self.dir / "out" / "replays").glob("*.replay.json"))
        self.assertEqual(len(documents), 1)
        document = json.loads(documents[0].read_text())
        self.assertEqual(document["verdict"], "reproduced")
        self.assertIn("case", document)

    def test_comparing_against_this_engine_is_refused(self) -> None:
        result = _cli(
            "replay", "--case", str(self.case_path), "--out", str(self.dir / "out"), "--now-ms", "2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        own = sorted((self.dir / "out" / "replays").glob("*.replay.json"))[0]
        again = _cli(
            "replay",
            "--case", str(self.case_path),
            "--compare", str(own),
            "--out", str(self.dir / "out"),
            "--now-ms", "2",
        )
        self.assertEqual(again.returncode, cli.PLANE_FAILURE)
        self.assertIn("same_engine", again.stderr)


class CutoverCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "cutover"

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "cutover")

    def test_an_incomplete_window_is_refused_with_every_blocker(self) -> None:
        from continuum.shadow import cutover, liveness, parity, planner
        from continuum.shadow.observation import outcome_from_payload

        journal = planner.plan(
            fixtures.event_for("merge-decision"), fixtures.state(), fixtures.config(), now_ms=1
        )
        result = parity.compare(
            journal,
            outcome_from_payload(
                {
                    "correlation_id": journal.event.correlation_id,
                    "fidelity": "exact",
                    "terminal_state": journal.status,
                    "actions": [effect.describe() for effect in journal.actions],
                }
            ),
        )
        parity_path = _write(self.dir / "parity.json", result.describe())
        live = liveness.LivenessReport(
            runs=(
                liveness.RunLiveness(
                    correlation_id=journal.event.correlation_id,
                    verdict=liveness.LIVE,
                    status="ok",
                    duration_ms=5,
                ),
            )
        )
        live_path = _write(self.dir / "liveness.json", live.describe())

        result = _cli(
            "cutover",
            "--parity", str(parity_path),
            "--liveness", str(live_path),
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        decision = json.loads((self.dir / "out" / "cutover" / "decision.json").read_text())
        self.assertFalse(decision["ready"])
        codes = {blocker["code"] for blocker in decision["blockers"]}
        self.assertIn("uncovered_scenario", codes)
        required = {name for name, _ in cutover.REQUIRED_SCENARIOS}
        observed = json.loads(parity_path.read_text())["scenario"]
        # No origin was supplied, so the window cannot prove the one run it saw
        # came from the live bridge -- and a run of unknown provenance is not live
        # coverage. All eight are therefore named.
        self.assertEqual(
            {blocker["scenario"] for blocker in decision["blockers"] if blocker["scenario"]},
            required,
        )

        # With the bridge's own record of where the run came from, the observed
        # class is covered and the other seven are still named.
        origins = _write(
            self.dir / "origins.json",
            {json.loads(parity_path.read_text())["correlation_id"]: "bridge"},
        )
        again = _cli(
            "cutover",
            "--parity", str(parity_path),
            "--liveness", str(live_path),
            "--origins", str(origins),
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
            "--out", str(self.dir / "out2"),
        )
        self.assertEqual(again.returncode, cli.VALIDATION_FAILED)
        second = json.loads((self.dir / "out2" / "cutover" / "decision.json").read_text())
        self.assertEqual(
            {blocker["scenario"] for blocker in second["blockers"] if blocker["scenario"]},
            required - {observed},
        )
        self.assertEqual(second["coverage"]["covered"], 1)

    def test_the_window_dates_are_required(self) -> None:
        result = _cli(
            "cutover", "--window-start", "2026-03-01T00:00:00Z",
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, cli.USAGE_ERROR)


class BarrierCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "barrier"

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "barrier")

    def test_every_mutating_method_is_called_and_writes_nothing(self) -> None:
        result = _cli("barrier-check", "--out", str(self.dir / "out"))
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads((self.dir / "out" / "barrier" / "check.json").read_text())
        # The exact count is the registry's business; that the production
        # client's mutators are all in it is asserted in test_shadow_barrier.
        self.assertGreaterEqual(len(document["methods"]), 10)
        self.assertEqual(document["uncallable"], [], "a mutator was never probed")
        self.assertEqual(document["performed"], [], "an effect was not suppressed")
        self.assertEqual(document["violations"], [])
        probed = {entry["name"] for entry in document["called"]}
        probed.update(entry["name"] for entry in document["refused"])
        probed.update(document["unreachable"])
        self.assertEqual(probed, set(document["methods"]))
        for entry in document["called"]:
            self.assertFalse(
                entry["unrecorded"],
                "{} returned without recording an effect".format(entry["name"]),
            )
            for effect in entry["effects"]:
                self.assertTrue(effect["suppressed"], effect)
        self.assertTrue(document["blocked_effect_kinds"])

    def test_a_deliberate_write_is_recorded_rather_than_executed(self) -> None:
        # The same claim, asserted at the level of a single adapter: the write is
        # refused, the refusal is recorded, and the state is untouched.
        from continuum.shadow.effects import RecordingEffects
        from continuum.shadow.state import ShadowGitHubClient

        state = state_from_payload(fixtures.state().as_capture())
        effects = RecordingEffects()
        client = ShadowGitHubClient(state, effects)
        client.add_labels(43, ["opencode-conflict-repair"])
        self.assertEqual(effects.kinds(), ("label.add",))
        self.assertNotIn("opencode-conflict-repair", state.pull(43).labels)


class DescribeCommand(unittest.TestCase):
    def test_it_prints_the_engine_and_the_requirements(self) -> None:
        result = _cli("describe")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertTrue(document["engine"]["engine_sha"])
        self.assertIn("merge-decision", [entry["scenario"] for entry in document["required_for_cutover"]])
        self.assertIn("get_pull", document["read_only_methods"])
        self.assertNotIn("add_labels", document["read_only_methods"])

    def test_no_command_prints_usage(self) -> None:
        result = _cli()
        self.assertEqual(result.returncode, cli.USAGE_ERROR)
        self.assertIn("shadow validation", result.stdout)


class SummaryCommand(unittest.TestCase):
    """The job summary a reviewer reads, rendered from the run's own evidence."""

    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "summary"
        self.out = self.dir / "out"
        self.event = _write(
            self.dir / "event.json",
            {
                "schema": "continuum.shadow-event/v1",
                **fixtures.event_for("ci-repair").describe(),
            },
        )
        self.state = _write(self.dir / "state.json", fixtures.state().as_capture())
        self.observed = self.dir / "observed.json"

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "summary")

    def _run(self) -> subprocess.CompletedProcess:
        result = _cli(
            "run",
            "--event", str(self.event),
            "--state", str(self.state),
            "--out", str(self.out),
            "--save-case",
            "--now-ms", "1700000000000",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _summary(self, *args: str) -> str:
        result = _cli("summary", "--out", str(self.out), *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_summary_names_what_the_run_decided(self) -> None:
        self._run()
        summary = self._summary("--artifact", "shadow-run-1")
        self.assertIn("update-branch", summary)
        self.assertIn("ci-repair", summary)
        self.assertIn("shadow-run-1", summary)
        # And the effects the run would have taken, which is the whole point of
        # a shadow decision nobody can see in the log.
        self.assertIn("effect", summary)

    def test_the_summary_reports_a_run_with_no_parity_as_uncompared(self) -> None:
        self._run()
        summary = self._summary()
        # Not "matched": the honest word for a run that was never compared.
        self.assertIn("not the same as matching", summary)

    def test_the_summary_reports_parity_when_it_was_compared(self) -> None:
        self._run()
        journal = sorted((self.out / "journals").glob("*.json"))[0]
        document = json.loads(journal.read_text())
        # The production outcome for the run Continuum would have made: the same
        # decision, the same effects, recorded after the fact.
        _write(
            self.observed,
            {
                "correlation_id": document["event"]["correlation_id"],
                "fidelity": "exact",
                "terminal_state": document["status"],
                "decision": document["decision"],
                "actions": document["actions"],
            },
        )
        compared = _cli(
            "parity",
            "--journal", str(journal),
            "--observed", str(self.observed),
            "--out", str(self.out),
        )
        self.assertIn(compared.returncode, (0, 1), compared.stderr)
        summary = self._summary()
        self.assertNotIn("not the same as matching", summary)
        self.assertIn("exact", summary)
        self.assertIn("the same 3 in the same order", summary)

    def test_the_summary_says_so_when_a_run_wrote_no_journal(self) -> None:
        summary = self._summary()
        self.assertIn("wrote no journal", summary)

    def test_a_window_summary_leads_with_the_decision(self) -> None:
        # A decision document with blockers, as the gate writes it.
        from continuum.shadow import cutover

        decision = cutover.CutoverDecision(
            ready=False,
            approved=False,
            blockers=(
                cutover.Blocker(
                    code="missing_scenario",
                    message="no live evidence for dependency-blocked",
                    scenario="dependency-blocked",
                ),
            ),
            window_started_at="2026-03-01T00:00:00Z",
            window_ended_at="2026-03-02T00:00:00Z",
            evidence_digest="d" * 64,
        )
        _write(self.out / "cutover" / "decision.json", decision.describe())
        result = _cli("summary", "--out", str(self.out), "--window")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not approved", result.stdout)
        self.assertIn("missing_scenario", result.stdout)
        # `ready` and `approved` are separate, and a window with evidence but no
        # approval must never be summarised as approved.
        _write(
            self.out / "cutover" / "decision.json",
            cutover.CutoverDecision(ready=True, approved=False).describe(),
        )
        waiting = _cli("summary", "--out", str(self.out), "--window")
        self.assertIn("awaiting a recorded approval", waiting.stdout)
        self.assertNotIn("**approved**", waiting.stdout)

    def test_the_summary_never_invents_a_decision_from_a_broken_artifact(self) -> None:
        self._run()
        (self.out / "parity").mkdir(parents=True, exist_ok=True)
        (self.out / "parity" / "broken.json").write_text("{ not json", encoding="utf-8")
        summary = self._summary()
        self.assertIn("could not be read", summary)
        # A parity file that does not parse is not a parity verdict, so the
        # summary must not read as one.
        self.assertNotIn("the same", summary)


def _remove(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


if __name__ == "__main__":
    unittest.main()
