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

_WORKFLOWS = (
    (".github/workflows/ci.yml", "1" * 40),
    (".github/workflows/auto-merge.yml", "2" * 40),
)
_LEDGER = {
    "schema": "continuum.parity-ledger/v1",
    "repository": "kodmial/nanodictate",
    "audited_head": "a" * 40,
    "workflows": [
        {
            "path": path,
            "blob_sha": sha,
            "classification": "consumer-local",
            "owner": "#27",
            "rationale": "product CI",
        }
        for path, sha in _WORKFLOWS
    ],
    "open_pull_requests": [],
}


def _provenance_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the provenance CLI in its own process, for the same reason as ``_cli``."""

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "continuum.provenance.cli", *args],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
    )


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


class ZeroTouchCutover(unittest.TestCase):
    """The zero-touch path, driven the way CI drives it.

    NanoDictate's first cutover merges with nobody watching: the trusted controller
    issues an authorization bound to the window, the reading, the consumer HEAD, the
    engine, the canary, the rollback target and the cutover head, and the gate
    re-derives all of them. What has to be proven here is that the exit code says
    *authorized* rather than *approved*, and that nothing in it names a person.
    """

    PHASE = "phase-a"
    CUTOVER_HEAD = "c" * 40
    CANARY = {"reference": "runs/canary-1", "result": "clean"}
    ROLLBACK = {"reference": ".github/workflows/issue-scheduler.yml@revert-1"}
    #: Roles the ledger records for the paths this cutover touches. Kept in the
    #: test rather than borrowed from the shipped ledger so the assertion is about
    #: the gate, not about one consumer's inventory.
    WORKFLOWS = (
        (".github/workflows/issue-scheduler.yml", "1" * 40, "scheduler"),
        (".github/workflows/release.yml", "2" * 40, "release"),
    )

    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "zero-touch"

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "zero-touch")

    def _ledger(self):
        return {
            "schema": "continuum.parity-ledger/v1",
            "repository": "kodmial/nanodictate",
            "audited_head": "a" * 40,
            "workflows": [
                {
                    "path": path,
                    "blob_sha": sha,
                    "classification": "consumer-local",
                    "owner": "#27",
                    "writer": writer,
                }
                for path, sha, writer in self.WORKFLOWS
            ],
            "open_pull_requests": [],
        }

    def _reading(self):
        return _write(
            self.dir / "live-head.json",
            {
                "schema": "continuum.shadow-baseline/v1",
                "repository": "kodmial/nanodictate",
                "default_branch": "main",
                "head_sha": HEAD_E,
                "workflows": [
                    {"path": path, "blob_sha": sha} for path, sha, _ in self.WORKFLOWS
                ],
                "open_pull_requests": [],
            },
        )

    def _provenance(self):
        """A source-drift report for the same consumer, produced by its own CLI.

        Written through ``continuum.provenance.cli check`` rather than assembled here,
        so the artifact the gate is handed is the one the plane actually emits -- a
        report hand-written to satisfy the gate would prove nothing about whether the
        two agree.
        """

        ledger = _write(
            self.dir / "provenance-ledger.json",
            {
                "schema": "continuum.provenance-ledger/v1",
                "version": 1,
                "sources": [
                    {
                        "id": "nanodictate",
                        "repository": "kodmial/nanodictate",
                        "visibility": "public",
                        "disposition": "absorbed",
                        "severity": "p0",
                        "baseline_sha": "a" * 40,
                        "audited_at": "2026-03-01T00:00:00Z",
                        "tracked_prefixes": [".github/workflows/"],
                        "path_dispositions": {
                            ".github/workflows/release.yml": "must-port"
                        },
                        "continuum": {
                            "implementation": ["src/continuum/shadow/planner.py"],
                            "tests": ["tests/test_shadow_cutover.py"],
                        },
                    }
                ],
            },
        )
        # A reading with nothing changed, captured rather than read live, so the test
        # depends on no network and no credential.
        readings = _write(
            self.dir / "provenance-readings.json",
            {
                "readings": [
                    {
                        "id": "nanodictate",
                        "repository": "kodmial/nanodictate",
                        "visibility": "public",
                        "severity": "p0",
                        "status": "unchanged",
                        "baseline_sha": "a" * 40,
                        "head_sha": "a" * 40,
                        "pull_requests": [],
                        "limits": [],
                        "credential": "public-read",
                        "ledger_digest": "",
                        "audited_at": "2026-03-20T00:00:00Z",
                    }
                ]
            },
        )
        checked = _provenance_cli(
            "check",
            "--ledger", str(ledger),
            "--readings", str(readings),
            "--out", str(self.dir / "provenance-out" / "drift.json"),
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        return self.dir / "provenance-out" / "drift.json"

    def _window(self):
        """Every control-plane scenario, live, plus the artifacts the gate reads."""

        from continuum.shadow import liveness, parity, planner
        from continuum.shadow.observation import outcome_from_payload

        ledger_path = _write(self.dir / "ledger.json", self._ledger())
        reading = _cli(
            "baseline",
            "--ledger", str(ledger_path),
            "--live-head", str(self._reading()),
            "--out", str(self.dir / "baseline-out"),
        )
        self.assertEqual(reading.returncode, 0, reading.stderr)
        report = json.loads(
            (self.dir / "baseline-out" / "baseline" / "report.json").read_text()
        )

        results = []
        origins = {}
        runs = []
        for scenario in (
            "issue-lifecycle",
            "ci-repair",
            "coderabbit-finding",
            "merge-decision",
            "dependency-blocked",
            "duplicate-replay",
            "timeout-recovery",
        ):
            journal = planner.plan(
                fixtures.event_for(scenario), fixtures.state(), fixtures.config(), now_ms=1
            )
            result = parity.compare(
                journal,
                outcome_from_payload(
                    {
                        "correlation_id": journal.event.correlation_id,
                        "fidelity": "exact",
                        "terminal_state": journal.status,
                        "decision": journal.decision,
                        "actions": [effect.describe() for effect in journal.actions],
                    }
                ),
            )
            results.append(result)
            origins[result.correlation_id] = "bridge"
            runs.append(
                liveness.RunLiveness(
                    correlation_id=result.correlation_id,
                    verdict=liveness.LIVE,
                    status="ok",
                    duration_ms=5,
                )
            )
        parity_paths = [
            _write(self.dir / "parity-{}".format(index), result.describe())
            for index, result in enumerate(results)
        ]
        live = _write(
            self.dir / "liveness.json", liveness.LivenessReport(runs=tuple(runs)).describe()
        )
        origins_path = _write(self.dir / "origins.json", origins)
        canary_path = _write(self.dir / "canary.json", self.CANARY)
        rollback_path = _write(self.dir / "rollback.json", self.ROLLBACK)
        change_set = _write(
            self.dir / "change-set.json",
            {
                "schema": "continuum.shadow-change-set/v1",
                "kind": "change-set",
                "cutover_head": self.CUTOVER_HEAD,
                "phase": self.PHASE,
                "changes": [
                    {
                        "path": ".github/workflows/issue-scheduler.yml",
                        "action": "remove",
                        "writer": "scheduler",
                    }
                ],
            },
        )
        return {
            "ledger": ledger_path,
            "report": self.dir / "baseline-out" / "baseline" / "report.json",
            "provenance": self._provenance(),
            "parity": parity_paths,
            "liveness": live,
            "origins": origins_path,
            "canary": canary_path,
            "rollback": rollback_path,
            "change_set": change_set,
            "reading": report,
        }

    def _authorization(self, window, **overrides):
        from continuum.shadow import cutover, engine

        fields = {
            "schema": cutover.AUTHORIZATION_SCHEMA,
            "kind": "authorization",
            "phase": self.PHASE,
            "authorized_by": "continuum-cutover-controller",
            "authorized_at": "2026-03-20T00:00:00Z",
            "baseline_digest": window["reading"]["evidence_digest"],
            "consumer_head": window["reading"]["live_head"],
            "controller_sha": engine.engine_sha(),
            "canary_reference": self.CANARY["reference"],
            "rollback_reference": self.ROLLBACK["reference"],
            "cutover_head": self.CUTOVER_HEAD,
        }
        fields.update(overrides)
        return fields

    def _judge(self, window, authorization, *extra, out="out"):
        args = ["cutover", "--out", str(self.dir / out)]
        args += ["--parity"] + [str(path) for path in window["parity"]]
        args += [
            "--liveness", str(window["liveness"]),
            "--origins", str(window["origins"]),
            "--baseline", str(window["report"]),
            "--provenance", str(window["provenance"]),
            "--canary", str(window["canary"]),
            "--rollback", str(window["rollback"]),
            "--ledger", str(window["ledger"]),
            "--change-set", str(window["change_set"]),
            "--phase", self.PHASE,
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
        ]
        if authorization is not None:
            args += ["--authorization", str(authorization)]
        return _cli(*(args + list(extra)))

    def _evidence_digest(self, window):
        """The digest the controller would have authorized: one judge, no record."""

        result = self._judge(window, None, out="digest")
        self.assertNotEqual(result.returncode, 0)
        return json.loads(
            (self.dir / "digest" / "cutover" / "decision.json").read_text()
        )["evidence_digest"]

    def test_a_bound_authorization_exits_zero_with_no_human_record(self) -> None:
        window = self._window()
        path = _write(
            self.dir / "authorization.json",
            self._authorization(window, evidence_digest=self._evidence_digest(window)),
        )
        result = self._judge(window, path)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(
            (self.dir / "out" / "cutover" / "decision.json").read_text()
        )
        self.assertTrue(decision["authorized"])
        self.assertTrue(decision["ready"])
        # Authorized, not approved: the gate did not decide for itself, and no
        # person is named anywhere in the record.
        self.assertFalse(decision["approved"])
        self.assertIsNone(decision["approval"])
        self.assertIn("authorized for {}".format(self.PHASE), result.stdout)

    def test_phase_a_merges_without_release_evidence(self) -> None:
        window = self._window()
        path = _write(
            self.dir / "authorization.json",
            self._authorization(window, evidence_digest=self._evidence_digest(window)),
        )
        self.assertEqual(self._judge(window, path).returncode, 0)
        decision = json.loads(
            (self.dir / "out" / "cutover" / "decision.json").read_text()
        )
        self.assertEqual(decision["phase"], self.PHASE)
        # release-planning was never observed, and that did not stop the cutover.
        coverage = {entry["scenario"]: entry for entry in decision["coverage"]["scenarios"]}
        self.assertEqual(coverage["release-planning"]["events"], 0)
        self.assertFalse(coverage["release-planning"]["required"])

    def test_the_same_window_is_refused_in_phase_b(self) -> None:
        window = self._window()
        path = _write(
            self.dir / "authorization.json",
            self._authorization(window, evidence_digest=self._evidence_digest(window)),
        )
        result = self._judge(window, path, "--phase", "phase-b", out="phase-b")
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        decision = json.loads(
            (self.dir / "phase-b" / "cutover" / "decision.json").read_text()
        )
        self.assertFalse(decision["authorized"])
        codes = {blocker["code"] for blocker in decision["blockers"]}
        self.assertIn("uncovered_scenario", codes)

    def test_a_moved_binding_fails_the_command(self) -> None:
        window = self._window()
        digest = self._evidence_digest(window)
        for field, value in (
            ("consumer_head", "f" * 40),
            ("controller_sha", "f" * 40),
            ("cutover_head", "d" * 40),
            ("canary_reference", "runs/canary-2"),
            ("rollback_reference", "workflows/issue-scheduler.yml@revert-9"),
            ("baseline_digest", "bl-0000000000000000"),
            ("evidence_digest", "ev-0000000000000000"),
        ):
            with self.subTest(binding=field):
                path = _write(
                    self.dir / "authorization-{}.json".format(field),
                    self._authorization(
                        window, **{"evidence_digest": digest, field: value}
                    ),
                )
                out = "moved-{}".format(field)
                result = self._judge(window, path, out=out)
                self.assertEqual(result.returncode, cli.VALIDATION_FAILED, field)
                decision = json.loads(
                    (self.dir / out / "cutover" / "decision.json").read_text()
                )
                self.assertFalse(decision["authorized"])
                self.assertTrue(
                    any(code.startswith("stale_") for code in (
                        blocker["code"] for blocker in decision["blockers"]
                    )),
                    [blocker["code"] for blocker in decision["blockers"]],
                )

    def test_a_change_set_that_reaches_the_release_writer_is_refused(self) -> None:
        window = self._window()
        window["change_set"] = _write(
            self.dir / "change-set-release.json",
            {
                "schema": "continuum.shadow-change-set/v1",
                "kind": "change-set",
                "cutover_head": self.CUTOVER_HEAD,
                "phase": self.PHASE,
                "changes": [
                    {
                        "path": ".github/workflows/release.yml",
                        "action": "remove",
                        "writer": "release",
                    }
                ],
            },
        )
        path = _write(
            self.dir / "authorization.json",
            self._authorization(window, evidence_digest=self._evidence_digest(window)),
        )
        result = self._judge(window, path, out="release-scope")
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED, result.stderr)
        decision = json.loads(
            (self.dir / "release-scope" / "cutover" / "decision.json").read_text()
        )
        codes = {blocker["code"] for blocker in decision["blockers"]}
        self.assertIn("release_writer_in_phase_a", codes)
        self.assertIn("authorization_over_blocked_window", codes)
        self.assertFalse(decision["authorized"])

    def test_a_change_set_without_a_ledger_is_a_usage_error(self) -> None:
        window = self._window()
        result = _cli(
            "cutover",
            "--change-set", str(window["change_set"]),
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
            "--out", str(self.dir / "no-ledger"),
        )
        self.assertEqual(result.returncode, cli.USAGE_ERROR)
        self.assertIn("missing_ledger", result.stderr)

    def test_an_authorization_for_another_engine_is_refused(self) -> None:
        # The controller binding names the engine this gate is running as, and no
        # flag exists to tell the gate to believe a different one.
        window = self._window()
        path = _write(
            self.dir / "authorization.json",
            self._authorization(
                window,
                evidence_digest=self._evidence_digest(window),
                controller_sha="0" * 40,
            ),
        )
        self.assertEqual(self._judge(window, path, out="other-engine").returncode, cli.VALIDATION_FAILED)


class BaselineCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(ARTIFACTS) / "baseline"
        self.ledger = _write(self.dir / "ledger.json", _LEDGER)

    def tearDown(self) -> None:
        _remove(REPO_ROOT / ARTIFACTS / "baseline")

    def _live(self, **overrides):
        head = {
            "schema": "continuum.shadow-baseline/v1",
            "repository": "kodmial/nanodictate",
            "default_branch": "main",
            "head_sha": HEAD_E,
            "workflows": [{"path": path, "blob_sha": sha} for path, sha in _WORKFLOWS],
            "open_pull_requests": [],
        }
        head.update(overrides)
        return _write(self.dir / "live.json", head)

    def test_a_matching_reading_writes_both_artifacts_and_exits_zero(self) -> None:
        result = _cli(
            "baseline",
            "--ledger", str(self.ledger),
            "--live-head", str(self._live()),
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(
            (self.dir / "out" / "baseline" / "report.json").read_text()
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["verdict"], "clean")
        self.assertEqual(
            report["live_head"], HEAD_E, "the reading has to record what it read"
        )
        # The reading is kept beside the report so a later approval can be checked
        # against the state the state it was granted in.
        self.assertEqual(
            json.loads((self.dir / "out" / "baseline" / "live-head.json").read_text())["head_sha"],
            HEAD_E,
        )

    def test_a_workflow_that_moved_fails_the_job_and_names_the_issue(self) -> None:
        moved = _write(
            self.dir / "moved.json",
            json.loads(self._live().read_text())
            | {
                "workflows": [
                    {"path": path, "blob_sha": ("9" * 40 if path.endswith("ci.yml") else sha)}
                    for path, sha in _WORKFLOWS
                ]
            },
        )
        result = _cli(
            "baseline",
            "--ledger", str(self.ledger),
            "--live-head", str(moved),
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, cli.VALIDATION_FAILED)
        report = json.loads(
            (self.dir / "out" / "baseline" / "report.json").read_text()
        )
        self.assertEqual(report["verdict"], "drifted")
        self.assertIn(
            "workflow_blob_changed",
            {blocker["code"] for blocker in report["blockers"]},
        )
        self.assertIn("#27", report["routing"], "drift has to be routed to its owner")

    def test_a_reading_nobody_took_is_refused_rather_than_assumed_clean(self) -> None:
        # Without a capture and without a file there is nothing to compare, and an
        # audit of nothing is the one result that must never be called clean.
        result = _cli(
            "baseline", "--ledger", str(self.ledger), "--out", str(self.dir / "out")
        )
        # A usage error, not a validation result: nothing was audited, and the
        # command must not exit zero having written no report.
        self.assertEqual(result.returncode, cli.USAGE_ERROR)
        self.assertIn("no_live_head", result.stderr)
        self.assertFalse((self.dir / "out" / "baseline" / "report.json").exists())

    def test_capturing_a_repository_needs_to_be_asked_for_by_name(self) -> None:
        result = _cli(
            "baseline", "--ledger", str(self.ledger), "--capture-live",
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, cli.USAGE_ERROR)
        self.assertIn("missing_repository", result.stderr)

    def test_the_ledger_can_be_rendered_without_reading_anything(self) -> None:
        # The table is what a maintainer reads when deciding a classification, so it
        # has to be regenerable from the file that the gate consumes.
        result = _cli(
            "baseline",
            "--ledger", str(self.ledger),
            "--live-head", str(self._live()),
            "--render",
            "--out", str(self.dir / "out"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".github/workflows/ci.yml", result.stdout)
        self.assertIn("consumer-local", result.stdout)

    def test_the_cutover_command_refuses_a_window_with_no_reading(self) -> None:
        # The rule, seen from the entry point CI uses: even a window with a canary,
        # a rollback path and an approval is refused when the consumer's workflows
        # were never read.
        from continuum.shadow import cutover, liveness, parity, planner
        from continuum.shadow.observation import outcome_from_payload

        journal = planner.plan(
            fixtures.event_for("merge-decision"),
            fixtures.state(),
            fixtures.config(),
            now_ms=1,
        )
        compared = parity.compare(
            journal,
            outcome_from_payload(
                {
                    "correlation_id": journal.event.correlation_id,
                    "fidelity": "exact",
                    "terminal_state": journal.status,
                    "actions": [action.describe() for action in journal.actions],
                }
            ),
        )
        parity_path = _write(self.dir / "parity.json", compared.describe())
        live_path = _write(
            self.dir / "liveness.json",
            liveness.LivenessReport(
                runs=(
                    liveness.RunLiveness(
                        correlation_id=journal.event.correlation_id,
                        verdict=liveness.LIVE,
                        status="ok",
                        duration_ms=5,
                    ),
                )
            ).describe(),
        )
        result = _cli(
            "cutover",
            "--parity", str(parity_path),
            "--liveness", str(live_path),
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
            "--out", str(self.dir / "cutover"),
        )
        decision = json.loads(
            (self.dir / "cutover" / "cutover" / "decision.json").read_text()
        )
        codes = {blocker["code"] for blocker in decision["blockers"]}
        self.assertIn("no_baseline_evidence", codes)
        # Named alongside the real reasons, not instead of them.
        self.assertIn("uncovered_scenario", codes)
        self.assertEqual(cutover.describe(decision)["ready"], False)

    def test_a_report_that_disagrees_with_itself_fails_the_command(self) -> None:
        # The contradictory shape a truncated write leaves behind: `ready` true
        # beside a real blocker. Reconciling it would print a decision over a
        # document the gate never read, so the command fails and writes nothing a
        # reviewer could mistake for a verdict.
        from continuum.shadow import liveness, parity, planner
        from continuum.shadow.observation import outcome_from_payload

        journal = planner.plan(
            fixtures.event_for("merge-decision"),
            fixtures.state(),
            fixtures.config(),
            now_ms=1,
        )
        live_path = _write(
            self.dir / "liveness-2.json",
            liveness.LivenessReport(
                runs=(
                    liveness.RunLiveness(
                        correlation_id=journal.event.correlation_id,
                        verdict=liveness.LIVE,
                        status="ok",
                        duration_ms=5,
                    ),
                )
            ).describe(),
        )
        parity_path = _write(
            self.dir / "parity-2.json",
            parity.compare(
                journal,
                outcome_from_payload(
                    {
                        "correlation_id": journal.event.correlation_id,
                        "fidelity": "exact",
                        "terminal_state": journal.status,
                        "actions": [action.describe() for action in journal.actions],
                    }
                ),
            ).describe(),
        )
        contradictory = {
            "schema": "continuum.shadow-baseline/v1",
            "ready": True,
            "repository": "kodmial/nanodictate",
            "ledger_digest": "pl-1",
            "live_digest": "lh-1",
            "evidence_digest": "be-1",
            "audited_head": HEAD_E,
            "live_head": "b" * 40,
            "complete": True,
            "verdict": "clean",
            "differences": [],
            "blockers": [
                {
                    "code": "workflow_blob_changed",
                    "message": "ci.yml moved",
                    "subject": ".github/workflows/ci.yml",
                    "owner": "#27",
                }
            ],
            "routing": {"#27": [".github/workflows/ci.yml"]},
            "limits": [],
        }
        baseline_path = _write(self.dir / "baseline-contradictory.json", contradictory)
        result = _cli(
            "cutover",
            "--parity", str(parity_path),
            "--liveness", str(live_path),
            "--baseline", str(baseline_path),
            "--window-start", "2026-03-01T00:00:00Z",
            "--window-end", "2026-03-15T00:00:00Z",
            "--out", str(self.dir / "cutover-contradictory"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inconsistent_report", result.stderr)
        # Nothing a reviewer could mistake for a verdict: an empty --out tree is
        # not a decision, and a decision over this document would be one.
        self.assertEqual(
            [path.name for path in (self.dir / "cutover-contradictory").rglob("*") if path.is_file()],
            [],
        )


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
        self.assertIn("awaiting a recorded decision", waiting.stdout)
        self.assertNotIn("**approved**", waiting.stdout)
        self.assertNotIn("**authorized**", waiting.stdout)

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
