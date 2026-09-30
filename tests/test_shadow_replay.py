"""Replay: re-running a real case against a different engine.

The important assertions here are the negative ones. A replay plane that only
ever says "reproduced" would pass every test in this file, so most of these check
that a changed decision is *reported* as changed, that a comparison refuses to
compare an engine with itself, and that a replay cannot acquire a write path.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from continuum.shadow import planner, replay
from continuum.shadow.effects import RecordingEffects
from continuum.shadow.state import ShadowGitHubClient, state_from_payload
from tests import shadow_support as fixtures

ALL_SCENARIOS = (
    planner.SCENARIO_ISSUE_LIFECYCLE,
    planner.SCENARIO_CI_REPAIR,
    planner.SCENARIO_CODERABBIT_FINDING,
    planner.SCENARIO_MERGE_DECISION,
    planner.SCENARIO_RELEASE_PLANNING,
    planner.SCENARIO_DUPLICATE_REPLAY,
    planner.SCENARIO_TIMEOUT_RECOVERY,
    planner.SCENARIO_DEPENDENCY_BLOCKED,
)


def _journal(scenario: str, now_ms: int = 1):
    return planner.plan(
        fixtures.event_for(scenario), fixtures.state(), fixtures.config(), now_ms=now_ms
    )


class Capturing(unittest.TestCase):
    def test_a_case_carries_the_run_s_own_state_not_a_summary(self) -> None:
        journal = _journal(planner.SCENARIO_MERGE_DECISION)
        case = replay.build_case(journal, run_url="https://example.test/runs/1")
        self.assertEqual(case.correlation_id, journal.event.correlation_id)
        self.assertEqual(case.scenario, planner.SCENARIO_MERGE_DECISION)
        self.assertEqual(case.repository, "acme/widgets")
        self.assertEqual(case.source["run_url"], "https://example.test/runs/1")
        self.assertEqual(case.source["engine_sha"], journal.engine["engine_sha"])
        self.assertEqual(case.source["replay_command"], journal.replay_command)
        # The capture, not describe(): a case built from a summary cannot be
        # re-decided, and would fail as missing actions rather than as itself.
        self.assertEqual(
            case.observed,
            journal.observed.as_capture(),
        )
        self.assertEqual(
            state_from_payload(case.observed).pull(43).head_sha,
            journal.observed.pull(43).head_sha,
        )

    def test_a_case_is_labelled_with_where_it_came_from(self) -> None:
        self.assertEqual(replay.build_case(_journal(planner.SCENARIO_CI_REPAIR)).origin, "bridge")
        self.assertEqual(
            replay.build_case(_journal(planner.SCENARIO_CI_REPAIR), origin="fixture").origin,
            "fixture",
        )
        self.assertFalse(replay.build_case(_journal(planner.SCENARIO_CI_REPAIR), origin="fixture").is_live)

    def test_a_replay_of_a_replay_is_not_counted_as_a_second_observation(self) -> None:
        first = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        second = replay.build_case(replay.replay(first, fixtures.config(), now_ms=2).journal)
        self.assertEqual(second.origin, "replay")

    def test_a_journal_with_no_state_cannot_become_a_case(self) -> None:
        journal = _journal(planner.SCENARIO_CI_REPAIR)
        stripped = journal.__class__(**{**journal.__dict__, "observed": None})
        with self.assertRaises(replay.ReplayError) as caught:
            replay.build_case(stripped)
        self.assertEqual(caught.exception.code, "no_observed_state")

    def test_a_case_survives_a_round_trip_through_its_document(self) -> None:
        case = replay.build_case(
            _journal(planner.SCENARIO_DEPENDENCY_BLOCKED),
            captured_at="2026-02-03T04:05:06Z",
            subject="blocked on #7",
        )
        self.assertEqual(replay.read_case(case.describe()).describe(), case.describe())
        self.assertEqual(json.loads(case.to_compact()), case.describe())
        self.assertTrue(case.to_json().endswith("\n"))

    def test_a_case_writes_to_a_readable_filename(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        path = replay.write_case(case, Path("shadow-out/cases"))
        try:
            self.assertEqual(path.name, "{}.case.json".format(case.correlation_id))
            self.assertEqual(replay.read_case_file(path).describe(), case.describe())
        finally:
            for written in sorted(Path("shadow-out").rglob("*")):
                if written.is_file():
                    written.unlink()
            for directory in sorted(Path("shadow-out").rglob("*"), reverse=True):
                if directory.is_dir():
                    directory.rmdir()
            Path("shadow-out").rmdir()

    def test_an_unreadable_case_says_which_file(self) -> None:
        path = Path("shadow-out/broken.case.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        try:
            with self.assertRaises(replay.ReplayError) as caught:
                replay.read_case_file(path)
            self.assertEqual(caught.exception.code, "unreadable_case")
        finally:
            path.unlink()
            path.parent.rmdir()


class CaseValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))

    def test_a_case_without_a_correlation_id_is_refused(self) -> None:
        document = self.case.describe()
        document["correlation_id"] = ""
        with self.assertRaises(replay.ReplayError) as caught:
            replay.read_case(document)
        self.assertEqual(caught.exception.code, "missing_correlation_id")

    def test_a_case_missing_an_input_is_refused(self) -> None:
        for field in ("event", "observed", "recorded"):
            document = self.case.describe()
            document[field] = None
            with self.assertRaises(replay.ReplayError) as caught:
                replay.read_case(document)
            self.assertEqual(caught.exception.code, "missing_{}".format(field))

    def test_a_case_from_another_schema_is_refused(self) -> None:
        document = self.case.describe()
        document["schema"] = "continuum.shadow-replay-case/v99"
        with self.assertRaises(replay.ReplayError) as caught:
            replay.read_case(document)
        self.assertEqual(caught.exception.code, "unknown_case_schema")

    def test_a_case_whose_record_names_another_event_is_refused(self) -> None:
        document = self.case.describe()
        document["recorded"]["event"]["correlation_id"] = "e-someone-else"
        with self.assertRaises(replay.ReplayError) as caught:
            replay.read_case(document)
        self.assertEqual(caught.exception.code, "case_correlation_mismatch")

    def test_a_non_object_is_refused(self) -> None:
        with self.assertRaises(replay.ReplayError) as caught:
            replay.read_case(["not", "a", "case"])
        self.assertEqual(caught.exception.code, "case_not_a_mapping")


class Replaying(unittest.TestCase):
    def test_every_scenario_replays_to_its_recorded_decision(self) -> None:
        for scenario in ALL_SCENARIOS:
            with self.subTest(scenario=scenario):
                case = replay.build_case(_journal(scenario))
                result = replay.replay(case, fixtures.config(), now_ms=2)
                self.assertEqual(result.verdict, replay.REPRODUCED)
                self.assertFalse(result.changed)
                self.assertEqual(
                    result.journal.decision, case.recorded["decision"]
                )
                # Deterministic: the same case and the same clock give the same
                # document, or "reproduced" means nothing.
                self.assertEqual(
                    result.describe(), replay.replay(case, fixtures.config(), now_ms=2).describe()
                )

    def test_a_replay_records_no_guards_it_did_not_evaluate(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        result = replay.replay(case, fixtures.config(), now_ms=2)
        self.assertEqual(
            {guard.name for guard in result.journal.guards},
            {guard.name for guard in _journal(planner.SCENARIO_CI_REPAIR).guards},
        )

    def test_a_decision_that_changed_is_reported_as_changed(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        # Stand in for an older engine that answered differently.
        case = replay.read_case(
            {
                **case.describe(),
                "recorded": {**case.recorded, "decision": "no-repair"},
            }
        )
        result = replay.replay(case, fixtures.config(), now_ms=2)
        self.assertEqual(result.verdict, replay.CHANGED)
        self.assertTrue(result.changed)
        # The effects are identical, so parity says ``exact``: it compares
        # actions, not the label a run gave them. The replay verdict is what
        # catches a decision that changed underneath unchanged effects, which is
        # why this plane checks the decision itself instead of trusting parity
        # to notice.
        self.assertEqual(result.comparison.classification, "exact")
        self.assertIn("no-repair", result.reason)

    def test_a_replay_that_cannot_decide_is_a_crash(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        broken = replay.read_case(
            {
                **case.describe(),
                "event": {**case.event, "event_type": "check_run.completed"},
            }
        )
        result = replay.replay(broken, fixtures.config(), now_ms=2)
        self.assertEqual(result.verdict, replay.CRASHED)
        self.assertIn("cannot read the case", result.reason)
        self.assertEqual(result.journal.status, "failed")
        self.assertEqual(result.journal.decision, "unreadable_case")
        self.assertIn(result.engine_sha, journal_sha(), "a crash names the engine")

    def test_a_replay_runs_against_a_record_only_client_and_records_writes(self) -> None:
        # The replay's own effects are recorded, and the only client in the
        # process is the record-only one: there is no adapter a replay could
        # reach that could merge or comment.
        case = replay.build_case(_journal(planner.SCENARIO_CI_REPAIR))
        result = replay.replay(case, fixtures.config(), now_ms=2)
        self.assertTrue([effect.describe() for effect in result.journal.actions])
        for action in result.describe()["actions"]:
            self.assertIn("kind", action)

        state = state_from_payload(case.observed)
        effects = RecordingEffects()
        client = ShadowGitHubClient(state, effects)
        client.add_labels(43, ["opencode-conflict-repair"])
        self.assertEqual(effects.kinds(), ("label.add",))
        self.assertNotIn("label.add", state.pull(43).labels)

    def test_a_replay_document_carries_the_case_it_came_from(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_MERGE_DECISION))
        document = replay.replay_document(case, fixtures.config(), now_ms=2)
        self.assertEqual(document["case"], case.describe())
        self.assertEqual(document["verdict"], replay.REPRODUCED)
        self.assertEqual(document["schema"], replay.REPLAY_SCHEMA)

    def test_a_replay_writes_its_artifact_next_to_the_engine_that_produced_it(self) -> None:
        case = replay.build_case(_journal(planner.SCENARIO_MERGE_DECISION))
        document = replay.replay_document(case, fixtures.config(), now_ms=2)
        path = replay.write_replay(document, Path("shadow-out/replays"))
        try:
            self.assertIn(case.correlation_id, path.name)
            self.assertIn(document["engine"]["engine_sha"][:12], path.name)
        finally:
            for written in sorted(Path("shadow-out").rglob("*")):
                if written.is_file():
                    written.unlink()
            for directory in sorted(Path("shadow-out").rglob("*"), reverse=True):
                if directory.is_dir():
                    directory.rmdir()
            Path("shadow-out").rmdir()


class TwoEngines(unittest.TestCase):
    def _document(
        self,
        scenario: str,
        sha: str,
        *,
        decision: str = "",
        status: str = "",
        drop_first_action: bool = False,
    ):
        """A replay document as another Continuum would have written it.

        The fields are overridden after the real replay, because these stand in
        for an engine this checkout is not.
        """

        document = replay.replay_document(
            replay.build_case(_journal(scenario)), fixtures.config(), now_ms=2
        )
        if decision:
            document["decision"] = decision
        if status:
            document["status"] = status
        if drop_first_action:
            document["actions"] = document["actions"][1:]
        document["engine"] = {"engine_sha": sha}
        return document

    def test_two_engines_that_agree_are_reproduced(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        right = self._document(planner.SCENARIO_CI_REPAIR, "b" * 40)
        result = replay.compare_replays(left, right)
        self.assertTrue(result.agrees)
        self.assertEqual(result.classification, replay.REPRODUCED)
        self.assertIn("reached the same decision", result.summary)
        self.assertIn("b" * 40, result.describe()["right"]["engine_sha"])

    def test_two_engines_that_disagree_name_the_field_and_both_shas(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        right = self._document(
            planner.SCENARIO_CI_REPAIR, "b" * 40, decision="no-repair", drop_first_action=True
        )
        result = replay.compare_replays(left, right)
        self.assertEqual(result.classification, replay.CHANGED)
        self.assertFalse(result.agrees)
        fields = [entry["field"] for entry in result.differences]
        self.assertIn("decision", fields)
        self.assertEqual(
            result.differences[-1]["field"],
            "actions",
            "the action lists are compared too, so an effect that vanished is named",
        )
        self.assertIn("a" * 40, result.summary)
        self.assertIn("b" * 40, result.summary)

    def test_two_engines_that_failed_differently_are_a_crash_not_a_change(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        right = self._document(planner.SCENARIO_CI_REPAIR, "b" * 40, status="failed")
        result = replay.compare_replays(left, right)
        self.assertEqual(result.classification, replay.CRASHED)

    def test_comparing_an_engine_with_itself_is_refused(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        with self.assertRaises(replay.ReplayError) as caught:
            replay.compare_replays(left, left)
        self.assertEqual(caught.exception.code, "same_engine")

    def test_comparing_two_different_cases_is_refused(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        right = self._document(planner.SCENARIO_MERGE_DECISION, "b" * 40)
        with self.assertRaises(replay.ReplayError) as caught:
            replay.compare_replays(left, right)
        self.assertEqual(caught.exception.code, "comparison_mismatch")

    def test_a_comparison_summarises_to_one_line(self) -> None:
        left = self._document(planner.SCENARIO_CI_REPAIR, "a" * 40)
        right = self._document(planner.SCENARIO_CI_REPAIR, "b" * 40)
        self.assertIn("reproduced", replay.summarize(replay.compare_replays(left, right)))


def journal_sha() -> str:
    from continuum.shadow import engine as engine_module

    return [str(engine_module.describe().get("engine_sha", ""))]


if __name__ == "__main__":
    unittest.main()
