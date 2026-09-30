"""The read-only production observer: what it can see, and what it must not claim.

Two things are being tested, and they pull in opposite directions.

The first is *coverage*: for every scenario class the cutover gate names, a
deterministic fixture of production reads must produce a closed window, an
outcome that is supported by those reads, and a parity verdict that is the one a
human would reach. A component whose job is to say what production did is only
worth having if it says something about all the paths, not just the easy one.

The second is *restraint*: every way the observer could be wrong has to fail
loudly or stay silent, and never fail as agreement. Specifically: an unreadable
surface, a truncated one, an uncorrelatable record, and a window that is still
open must each produce something other than a verdict -- and must produce
``unevaluable`` rather than ``exact`` when the reading was incomplete, because
"we could not see it" and "it did not happen" are the distinction the whole plane
exists to preserve.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Mapping, Sequence

from tests import shadow_support as fixtures
from continuum.review import github
from continuum.shadow import effects, observer, parity, planner
from continuum.shadow import cli
from continuum.shadow.cli import main as cli_main
from continuum.review.github import GitHubClient

REPOSITORY = "acme/widgets"


def _millis(value: str) -> int:
    moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(moment.timestamp() * 1000)


#: Well past the default horizon, so an "expired" window has genuinely expired
#: rather than merely being a window that ran out of evidence.
LATE = observer.DEFAULT_HORIZON_MS + 1_000
#: Shortly after the window opened: still inside it.
EARLY = 60_000


class Production:
    """A deterministic stand-in for the consumer repository's read-only surface.

    Every attribute is a method the observer is allowed to call, and nothing else
    is reachable -- which is itself part of the test, because
    :meth:`continuum.shadow.observer._read` refuses anything not on the allowlist
    and the fake has no generic transport to fall back to.

    Set ``fails`` to make a named read raise, ``long`` to make a named read return
    ``observer.TRUNCATION_FLOOR`` records, and both are how the failure paths are
    exercised without a network.
    """

    def __init__(self, **surfaces: Any) -> None:
        self.pulls: Dict[int, Dict[str, Any]] = dict(surfaces.pop("pulls", {}) or {})
        self.issues: Dict[int, Dict[str, Any]] = dict(surfaces.pop("issues", {}) or {})
        self.events: List[Dict[str, Any]] = list(surfaces.pop("events", []) or [])
        self.workflow_runs: List[Dict[str, Any]] = list(surfaces.pop("workflow_runs", []) or [])
        self.reviews: Dict[int, List[Dict[str, Any]]] = dict(surfaces.pop("reviews", {}) or {})
        self.comments: Dict[int, List[Dict[str, Any]]] = dict(surfaces.pop("comments", {}) or {})
        self.check_runs: Dict[str, List[Dict[str, Any]]] = dict(
            surfaces.pop("check_runs", {}) or {}
        )
        self.statuses: Dict[str, Dict[str, Any]] = dict(surfaces.pop("statuses", {}) or {})
        self.releases: List[Dict[str, Any]] = list(surfaces.pop("releases", []) or [])
        self.fails: Sequence[str] = tuple(surfaces.pop("fails", ()))
        self.long: Sequence[str] = tuple(surfaces.pop("long", ()))
        self.calls: List[str] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fails:
            raise RuntimeError("the {} read is unavailable".format(name))

    def _maybe_long(self, name: str, records: List[Any]) -> List[Any]:
        if name in self.long:
            filler = {
                "id": -1,
                "event": "labeled",
                "created_at": "2026-01-01T00:00:00Z",
                "label": {"name": "filler"},
                "actor": {"login": "filler"},
                "body": "filler",
                "user": {"login": "filler"},
                "name": "filler",
                "conclusion": "success",
                "submitted_at": "2026-01-01T00:00:00Z",
                "commit_id": "",
                "state": "COMMENTED",
                "tag_name": "v0",
            }
            return [filler] * observer.TRUNCATION_FLOOR
        return records

    # -- the allowlist ------------------------------------------------------
    def get_pull(self, number: int) -> Dict[str, Any]:
        self._guard("get_pull")
        return self.pulls.get(int(number), {})

    def get_issue(self, number: int) -> Dict[str, Any]:
        self._guard("get_issue")
        return self.issues.get(int(number), {})

    def list_issue_events(self, number: int = 0, *, since: str = "") -> List[Dict[str, Any]]:
        self._guard("list_issue_events")
        return self._maybe_long("list_issue_events", self.events)

    def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
        self._guard("list_issue_comments")
        return self._maybe_long("list_issue_comments", self.comments.get(int(number), []))

    def list_workflow_runs(self, **_: Any) -> List[Dict[str, Any]]:
        self._guard("list_workflow_runs")
        return self._maybe_long("list_workflow_runs", self.workflow_runs)

    def list_reviews(self, number: int) -> List[Dict[str, Any]]:
        self._guard("list_reviews")
        return self.reviews.get(int(number), [])

    def list_check_runs(self, ref: str) -> List[Dict[str, Any]]:
        self._guard("list_check_runs")
        return self.check_runs.get(str(ref), [])

    def combined_status_for_ref(self, ref: str) -> Dict[str, Any]:
        self._guard("combined_status_for_ref")
        return self.statuses.get(str(ref), {"state": "pending", "statuses": []})

    def list_releases(self) -> List[Dict[str, Any]]:
        self._guard("list_releases")
        return self._maybe_long("list_releases", self.releases)


def _label_event(identifier: int, kind: str, label: str, at: str) -> Dict[str, Any]:
    return {
        "id": identifier,
        "event": kind,
        "created_at": at,
        "label": {"name": label},
        "actor": {"login": "acme"},
    }


def _run(identifier: int, workflow: str, at: str, **extra: Any) -> Dict[str, Any]:
    record = {
        "id": identifier,
        "event": "workflow_dispatch",
        "path": ".github/workflows/{}".format(workflow),
        "status": "completed",
        "conclusion": "success",
        "created_at": at,
    }
    record.update(extra)
    return record


def _comment(identifier: int, body: str, at: str) -> Dict[str, Any]:
    return {"id": identifier, "body": body, "created_at": at, "user": {"login": "acme"}}


# --------------------------------------------------------------------------- #
# Fixtures: one production timeline per scenario class
# --------------------------------------------------------------------------- #


def issue_event() -> Any:
    """The issue-opened event, naming the branch the queue dispatched onto.

    The observer can only correlate a repository-wide dispatch by the branch or
    commit it ran on, so the fixture event has to name the one. Without it the
    dispatch is genuinely unattributable, and the fixture would be testing the
    uncorrelated path twice under two names.
    """

    return fixtures.event(
        "issue.opened",
        number=7,
        action="opened",
        correlation_id="e-issue-opened",
        extra={"head_ref": "opencode/issue7-thing"},
    )


def issue_lifecycle_production() -> Production:
    """The ordinary path: a dispatch, the label, a PR, and a comment.

    The dispatch is correlated by commit -- the run checked out the branch that
    became the pull request -- so it is a real effect on this window rather than
    a repository-wide coincidence.
    """

    return Production(
        events=[
            _label_event(101, "labeled", "review-ready", "2026-01-01T00:05:00Z"),
        ],
        workflow_runs=[
            _run(
                9001,
                "issue-dispatch.yml",
                "2026-01-01T00:02:00Z",
                head_branch="opencode/issue7-thing",
            )
        ],
        pulls={},
        comments={7: [_comment(5001, "working on it", "2026-01-01T00:03:00Z")]},
    )


def ci_repair_production() -> Production:
    """CI went red and the repair controller dispatched itself.

    The repair run is on the window's own commit, so the observer can attribute
    it: "the repair controller acted" is a real finding here, not a guess.
    """

    return Production(
        workflow_runs=[
            _run(
                9002,
                "opencode.yml",
                "2026-01-01T00:41:00Z",
                head_sha=fixtures.HEAD_E,
                head_branch="opencode/issue8-green",
            )
        ],
        pulls={43: {"id": 4300, "state": "open", "head": {"sha": fixtures.HEAD_E}}},
        check_runs={fixtures.HEAD_E: [{"id": 7001, "name": "CI", "conclusion": "failure"}]},
        comments={43: [_comment(5002, "repairing", "2026-01-01T00:42:00Z")]},
    )


def coderabbit_production() -> Production:
    """A blocking review, then the re-review that cleared it."""

    return Production(
        reviews={
            41: [
                {
                    "id": 6001,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-01-01T00:05:00Z",
                    "commit_id": fixtures.HEAD_A,
                    "user": {"login": "coderabbitai[bot]"},
                },
                {
                    "id": 6002,
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T00:25:00Z",
                    "commit_id": fixtures.HEAD_A,
                    "user": {"login": "coderabbitai[bot]"},
                },
            ]
        },
        comments={41: [_comment(5003, "addressed", "2026-01-01T00:20:00Z")]},
    )


def merge_event() -> Any:
    """The merge sweep, with the commit it judged.

    The sweep fixture carries no ``head_sha``, and without one the observer cannot
    read the commit statuses the gate wrote -- so the fixture names the commit,
    because "the merge gate approved this commit" is the finding the scenario is
    about.
    """

    return fixtures.event(
        "schedule.tick",
        number=43,
        action="merge",
        head_sha=fixtures.HEAD_E,
        correlation_id="e-schedule-merge",
        observed_at_ms=fixtures.CAPTURED_AT_MS + 300_000,
    )


def merge_decision_production() -> Production:
    """The gate wrote a status, and the pull request was merged.

    A merge closes this window, so the merge is read from ``get_pull`` -- and the
    ``closed`` issue event that GitHub emits for *every* merged pull request, because
    a pull request is also an issue, is not counted as a second action.
    """

    return Production(
        events=[{"id": 102, "event": "closed", "created_at": "2026-01-01T00:50:00Z"}],
        pulls={
            43: {
                "id": 4300,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-01-01T00:50:00Z",
                "head": {"sha": fixtures.HEAD_E},
            }
        },
        statuses={
            fixtures.HEAD_E: {
                "state": "success",
                "statuses": [
                    {
                        "id": 8001,
                        "context": "continuum/merge-ready",
                        "state": "success",
                        "updated_at": "2026-01-01T00:45:00Z",
                    }
                ],
            }
        },
        comments={43: [_comment(5004, "merging", "2026-01-01T00:44:00Z")]},
    )


def release_published_production() -> Production:
    """A release exists for the tag the dry run planned."""

    return Production(
        releases=[
            {
                "id": 9001,
                "tag_name": "v0.1.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-01-01T01:00:00Z",
            }
        ]
    )


def blocked_production() -> Production:
    """Nothing happened at all, and the reads that would show it were complete.

    This is the case where "no action" is a *finding*, and it is only available
    because every read succeeded. The same empty effect list from a window with a
    failed read would be a gap, and the observer has to be able to tell the two.
    """

    return Production()


# --------------------------------------------------------------------------- #


def _at(event: Any, offset_ms: int) -> int:
    """A clock reading relative to the window this event opens.

    Offset from the event rather than from a fixed instant, because the fixture
    events open at different times and a horizon computed against the wrong one
    leaves every window open for reasons that have nothing to do with the
    behaviour under test.
    """

    return _millis(event.observed_at or "2026-01-01T00:00:00Z") + offset_ms


def _closed(event: Any, production: Production, **kwargs: Any) -> observer.Observation:
    """Observe ``event`` far enough past the horizon that the window must close."""

    return observer.observe(
        production,
        repository=REPOSITORY,
        event=event,
        ledger=observer.Ledger(),
        now_ms=_at(event, LATE),
        **kwargs
    )


class ObserverTestCase(unittest.TestCase):
    """The two window assertions every observer test needs."""

    def assert_closed(self, observation: observer.Observation) -> observer.Observation:
        """A window that closed *and* supports a claim."""

        self.assertFalse(
            observation.window.is_open,
            "the window should have closed: {}".format(observer.summarize(observation)),
        )
        self.assertIsNotNone(
            observation.outcome,
            "a closed window with complete reads must yield an outcome; "
            "limits were {}".format(list(observation.reading.limits)),
        )
        return observation

    def assert_closed_window(self, observation: observer.Observation) -> observer.Observation:
        """A window that closed, whether or not the closing supported a claim.

        Separate from :meth:`assert_closed` because "the window closed" and "the
        reads justified a conclusion" are different properties, and the tests that
        care about the second failing have to be able to say which one they mean.
        """

        self.assertFalse(
            observation.window.is_open,
            "the window should have closed: {}".format(observer.summarize(observation)),
        )
        return observation

    def assert_effects(self, observation: observer.Observation, *kinds: str) -> None:
        found = [effect.kind for effect in observation.outcome.effects]
        self.assertEqual(sorted(found), sorted(kinds))


class ObserverScenarioTest(ObserverTestCase):
    """One fixture per scenario class, each asserting the finding it exists for."""

    # -- issue lifecycle ----------------------------------------------------
    def test_issue_lifecycle_finds_the_dispatch_and_the_label(self) -> None:
        production = issue_lifecycle_production()
        observation = self.assert_closed(
            _closed(issue_event(), production)
        )
        self.assert_effects(observation, "label.add", "comment.create", "workflow.dispatch")
        # The dispatch ran on the branch that became the pull request, which the
        # window recorded, so it is a real effect rather than a stray run.
        self.assertEqual(observation.window.terminal, observer.TERMINAL_EXPIRED)
        # It is also the one place the comparison cannot see inside, and the
        # artifact has to say so rather than quietly matching on a guess.
        self.assertIn("inputs", observation.outcome.unobservable)

    # -- ci repair ----------------------------------------------------------
    def test_ci_repair_distinguishes_a_repair_run_from_a_dispatch(self) -> None:
        production = ci_repair_production()
        observation = self.assert_closed(
            _closed(fixtures.ci_repair_behind(), production)
        )
        self.assert_effects(observation, "workflow.dispatch", "comment.create")
        # "The repair controller acted" and "something was dispatched" are
        # different findings, and only the category tells them apart.
        self.assertEqual(observation.reading.evidence[0].kind, "workflow.dispatch")
        self.assertEqual(observation.reading.evidence[0].category, "repair")

    # -- coderabbit ---------------------------------------------------------
    def test_coderabbit_finds_both_the_finding_and_the_clearance(self) -> None:
        production = coderabbit_production()
        observation = self.assert_closed(
            _closed(fixtures.coderabbit_review(), production)
        )
        self.assert_effects(observation, "review.create", "review.create", "comment.create")

    # -- merge decision -----------------------------------------------------
    def test_merge_decision_closes_on_the_merge_and_counts_it_once(self) -> None:
        production = merge_decision_production()
        observation = self.assert_closed(_closed(merge_event(), production))
        self.assertEqual(observation.window.terminal, observer.TERMINAL_MERGED)
        # GitHub reports a merged pull request as a closed issue too. Counting
        # both would report one production action as two, and the second would
        # read as something Continuum missed.
        self.assert_effects(
            observation, "status.create", "comment.create", "pull.merge"
        )
        self.assertIn("method", observation.outcome.unobservable)
        self.assertEqual(observation.outcome.terminal_status, "ok")

    # -- release ------------------------------------------------------------
    def test_release_published_is_an_extra_action_not_a_match(self) -> None:
        production = release_published_production()
        observation = self.assert_closed(_closed(fixtures.release_event(), production))
        self.assertEqual(observation.window.terminal, observer.TERMINAL_PUBLISHED)
        self.assert_effects(observation, "release.publish")
        self.assertIn("destination", observation.outcome.unobservable)

    def test_release_published_diverges_from_the_dry_run(self) -> None:
        production = release_published_production()
        observation = self.assert_closed(_closed(fixtures.release_event(), production))
        result = parity.compare(_journal_for(fixtures.release_event()), observation.outcome)
        self.assertEqual(result.classification, parity.EXTRA_ACTION)
        self.assertTrue(result.divergent)

    def test_release_absent_is_agreement_not_a_gap(self) -> None:
        production = Production()
        observation = self.assert_closed(_closed(fixtures.release_event(), production))
        self.assert_effects(observation)
        self.assertEqual(observation.outcome.fidelity, "exact")
        result = parity.compare(_journal_for(fixtures.release_event()), observation.outcome)
        self.assertEqual(result.classification, parity.EXACT)
        self.assertFalse(result.divergent)

    # -- blocked ------------------------------------------------------------
    def test_blocked_no_dispatch_is_a_finding_when_the_reads_were_complete(self) -> None:
        production = blocked_production()
        observation = self.assert_closed(_closed(fixtures.issue_labelled(), production))
        self.assert_effects(observation)
        self.assertEqual(observation.outcome.fidelity, "exact")
        self.assertTrue(
            any("horizon" in limit for limit in observation.outcome.limits),
            "an empty result at the horizon must say the reads were complete, or it "
            "reads as an absence nobody checked for",
        )

    def test_every_planner_scenario_has_a_fixture(self) -> None:
        covered = {
            planner.SCENARIO_ISSUE_LIFECYCLE,
            planner.SCENARIO_CI_REPAIR,
            planner.SCENARIO_CODERABBIT_FINDING,
            planner.SCENARIO_MERGE_DECISION,
            planner.SCENARIO_RELEASE_PLANNING,
            planner.SCENARIO_DEPENDENCY_BLOCKED,
            planner.SCENARIO_DUPLICATE_REPLAY,
            planner.SCENARIO_TIMEOUT_RECOVERY,
        }
        self.assertEqual(set(planner.SCENARIO_CLASSES), covered)


class ObserverRestraintTest(ObserverTestCase):
    """Every way the observer could be wrong must not come out as agreement."""

    def test_a_failed_read_blocks_the_outcome_entirely(self) -> None:
        production = issue_lifecycle_production()
        production.fails = ("list_issue_events",)
        observation = _closed(issue_event(), production)
        # The window still closes: a horizon is a horizon. But nothing is claimed
        # from it, because "the timeline read failed" and "the timeline was
        # quiet" are not the same statement.
        self.assertEqual(observation.window.terminal, observer.TERMINAL_EXPIRED)
        self.assertIsNone(observation.outcome)

    def test_a_truncated_read_cannot_support_an_absence(self) -> None:
        production = blocked_production()
        production.long = ("list_issue_comments",)
        observation = _closed(fixtures.issue_labelled(), production)
        self.assertIsNone(
            observation.outcome,
            "a truncated read cannot tell a quiet episode from a busy one",
        )
        self.assertIn("issue-comments", observation.reading.truncated)

    def test_a_truncated_read_on_a_terminal_window_is_partial_not_blocked(self) -> None:
        production = merge_decision_production()
        production.long = ("list_issue_comments",)
        observation = self.assert_closed(_closed(merge_event(), production))
        # The merge is established by its own read. The truncated one only affects
        # fidelity, so it is reported as a limitation rather than swallowed.
        self.assertEqual(observation.outcome.fidelity, "partial")

    def test_an_open_window_produces_no_outcome_at_all(self) -> None:
        event = issue_event()
        observation = observer.observe(
            blocked_production(),
            repository=REPOSITORY,
            event=event,
            ledger=observer.Ledger(),
            now_ms=_at(event, EARLY),
        )
        self.assertTrue(observation.window.is_open)
        self.assertIsNone(observation.outcome)
        self.assertEqual(
            observation.window.missing,
            observer._REQUIRED_CATEGORIES[planner.SCENARIO_ISSUE_LIFECYCLE],
            "an open window must name the evidence it is still waiting for, or a "
            "reviewer cannot tell it is working from a reviewer who gave up",
        )

    def test_a_window_with_no_anchor_refuses_rather_than_reporting_a_quiet_repository(self) -> None:
        # A tick that names no pull request and no commit can only read the
        # repository at large. An empty effect list from that read set is not a
        # statement about any decision, so no outcome is emitted.
        event = fixtures.event(
            "schedule.tick", action="merge", correlation_id="e-anchorless"
        )
        observation = self.assert_closed_window(_closed(event, blocked_production()))
        self.assertIsNone(observation.outcome)

    def test_a_release_window_is_anchored_on_the_commit_it_was_cut_from(self) -> None:
        observation = self.assert_closed_window(
            _closed(fixtures.release_event(), blocked_production())
        )
        # No release was published, and the window is still anchored on the commit
        # -- which is what makes "no release for this tag" a finding about the
        # release rather than a shrug about the repository.
        self.assertEqual(observation.window.head_sha, fixtures.HEAD_E)
        self.assertIsNotNone(observation.outcome)

    def test_an_uncorrelatable_dispatch_is_recorded_but_not_claimed(self) -> None:
        production = issue_lifecycle_production()
        # A dispatch on a commit that is not this window's, inside its time span.
        production.workflow_runs.append(
            _run(9003, "issue-dispatch.yml", "2026-01-01T00:04:00Z", head_branch="other/branch")
        )
        observation = self.assert_closed(_closed(issue_event(), production))
        # The fixture's own dispatch is still attributed; the stray one is not.
        self.assert_effects(observation, "label.add", "comment.create", "workflow.dispatch")
        stray = [item for item in observation.reading.evidence if not item.correlated]
        self.assertEqual(len(stray), 1)
        self.assertTrue(
            any("could not be tied" in limit for limit in observation.outcome.limits),
            "the artifact must say the dispatch could not be attributed",
        )

    def test_records_before_the_window_opened_are_not_its_actions(self) -> None:
        production = issue_lifecycle_production()
        # Comments and statuses have no time filter in the API; the observer's
        # window is what keeps last week's activity out of today's comparison.
        production.comments[7] = [_comment(4999, "an old comment", "2025-06-01T00:00:00Z")]
        observation = self.assert_closed(_closed(issue_event(), production))
        self.assert_effects(observation, "label.add", "workflow.dispatch")

    def test_a_generic_transport_is_refused_even_when_it_exists(self) -> None:
        class Greedy:
            """A client that would happily take any verb, if it were asked."""

            def __getattr__(self, name: str) -> Any:  # noqa: ANN401
                return lambda *a, **k: []

        # The observer never calls a generic transport, and the one thing that
        # guarantees it is a list rather than a convention. ``request`` takes a
        # verb, so permitting it by name would permit a write just as readily as a
        # read -- and the allowlist is the only thing standing between this
        # component and production.
        for name in ("request", "paginate", "graphql"):
            with self.assertRaises(observer.ObserverError) as caught:
                observer._read(Greedy(), name)
            self.assertEqual(caught.exception.code, "write_capable_read")
            self.assertIn(name, caught.exception.message)

    def test_a_read_the_client_lacks_is_recorded_rather_than_raised(self) -> None:
        class Partial:
            def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
                return []

        event = issue_event()
        observation = observer.observe(
            Partial(),
            repository=REPOSITORY,
            event=event,
            ledger=observer.Ledger(),
            now_ms=_at(event, LATE),
        )
        # A client that cannot answer is not a crash: the pass records which reads
        # failed and claims nothing, so one unavailable endpoint does not take the
        # whole correlation window down with it.
        self.assertIsNone(observation.outcome)
        self.assertIn("issue", [name for name, _ in observation.reading.failures])

    def test_the_read_allowlist_excludes_the_generic_transports(self) -> None:
        for name in ("request", "paginate", "graphql"):
            self.assertNotIn(name, observer.OBSERVER_READ_METHODS)
        for name in observer.OBSERVER_READ_METHODS:
            self.assertTrue(
                hasattr(GitHubClient, name),
                "the allowlist names {!r}, which GitHubClient does not have".format(name),
            )


class ObserverLedgerTest(ObserverTestCase):
    """The ledger is what makes an hours-long correlation re-enterable."""

    def test_evidence_ids_are_stable_across_reads(self) -> None:
        production = issue_lifecycle_production()
        event = issue_event()
        window = observer.open_window(REPOSITORY, event)
        first = observer.read_production(
            production, repository=REPOSITORY, window=window, event=event
        )
        second = observer.read_production(
            production, repository=REPOSITORY, window=window, event=event
        )
        self.assertEqual(
            [item.evidence_id for item in first.evidence],
            [item.evidence_id for item in second.evidence],
        )

    def test_a_repeat_pass_adds_no_duplicate_evidence(self) -> None:
        production = issue_lifecycle_production()
        event = issue_event()
        ledger = observer.Ledger()
        for _ in range(3):
            ledger = observer.observe(
                production,
                repository=REPOSITORY,
                event=event,
                ledger=ledger,
                now_ms=_at(event, EARLY),
            ).ledger
        window = ledger.for_correlation(event.correlation_id)[0]
        collected = [item for item in window.evidence_ids]
        self.assertEqual(len(collected), len(set(collected)))
        # the label transition, the dispatch, and the comment
        self.assertEqual(len(collected), 3)

    def test_a_duplicate_delivery_joins_the_same_window(self) -> None:
        production = ci_repair_production()
        original = fixtures.ci_repair_behind()
        ledger = observer.observe(
            production,
            repository=REPOSITORY,
            event=original,
            ledger=observer.Ledger(),
            now_ms=_at(original, EARLY),
        ).ledger
        # The duplicate carries its own correlation id and names the original in
        # ``replays``. Opening a second window over the same production timeline
        # would report the same dispatch as an extra action twice, which is the
        # specific failure the duplicate-replay scenario exists to catch.
        duplicate = fixtures.event(
            "workflow_run.completed",
            number=43,
            action="completed",
            head_sha=fixtures.HEAD_E,
            correlation_id="e-ci-duplicate",
            replays=original.correlation_id,
            observed_at_ms=_at(original, EARLY),
        )
        ledger = observer.observe(
            production,
            repository=REPOSITORY,
            event=duplicate,
            ledger=ledger,
            now_ms=_at(original, EARLY),
        ).ledger
        window = ledger.for_correlation(original.correlation_id)[0]
        self.assertIn("e-ci-duplicate", window.correlation_ids)
        self.assertEqual(len(ledger.windows), 1)

    def test_the_ledger_survives_a_round_trip_through_its_document(self) -> None:
        production = issue_lifecycle_production()
        event = issue_event()
        ledger = observer.observe(
            production,
            repository=REPOSITORY,
            event=event,
            ledger=observer.Ledger(),
            now_ms=_at(event, EARLY),
        ).ledger
        restored = observer.ledger_from_payload(json.loads(json.dumps(ledger.describe())))
        self.assertEqual(restored.describe(), ledger.describe())

    def test_a_pass_never_emits_two_outcomes_for_one_window(self) -> None:
        production = release_published_production()
        event = fixtures.release_event()
        ledger = observer.Ledger()
        first = observer.observe(
            production,
            repository=REPOSITORY,
            event=event,
            ledger=ledger,
            now_ms=_at(event, LATE),
        )
        self.assertTrue(first.emitted)
        second = observer.observe(
            production,
            repository=REPOSITORY,
            event=event,
            ledger=first.ledger,
            now_ms=_at(event, LATE),
        )
        self.assertFalse(second.emitted)
        self.assertIsNone(second.outcome)
        self.assertEqual(len(first.ledger.emitted), 1)

    def test_an_unknown_ledger_schema_is_refused(self) -> None:
        with self.assertRaises(observer.ObserverError) as caught:
            observer.ledger_from_payload({"schema": "continuum.shadow-observer-ledger/v0"})
        self.assertEqual(caught.exception.code, "unknown_ledger_schema")


class ObserverParityTest(ObserverTestCase):
    """The comparison the observer exists to feed."""

    def test_unobservable_keys_are_dropped_from_both_sides(self) -> None:
        event = fixtures.merge_sweep()
        outcome = self.assert_closed(
            _closed(event, merge_decision_production())
        ).outcome
        # The journal's merge carries a ``method`` the observer cannot read, and
        # the observed action simply does not have one. Comparing them on the key
        # would make every merge look like a divergence over a field no read-only
        # endpoint returns.
        self.assertEqual(
            [
                effect.detail.get("method")
                for effect in _journal_for(event).actions
                if effect.kind == "pull.merge"
            ],
            ["squash"],
        )
        result = parity.compare(_journal_for(event), outcome)
        about_the_merge = [
            item
            for item in result.differences
            if item.kind in (parity.MISSING_ACTION, parity.EXTRA_ACTION)
            and (getattr(item.expected, "kind", "") == "pull.merge"
                 or getattr(item.observed, "kind", "") == "pull.merge")
        ]
        self.assertEqual(
            about_the_merge,
            [],
            "the merge itself must match; only the unreadable method is excluded: "
            "{}".format([item.describe() for item in result.differences]),
        )

    def test_the_plan_only_list_matches_the_planner(self) -> None:
        self.assertIn(
            planner.SCENARIO_RELEASE_PLANNING,
            parity.PLAN_ONLY_SCENARIOS,
            "a dry run's suppressed effects are a plan, not a prediction, and the "
            "comparison has to know that from the planner's own constant",
        )

    def test_reconciliation_pairs_by_correlation_not_by_arrival(self) -> None:
        production = release_published_production()
        event = fixtures.release_event()
        observation = _closed(event, production)
        outcome = observation.outcome
        # The outcome exists before anybody asks for it: the ledger holds the
        # window, and pairing happens when a journal turns up.
        self.assertIsNotNone(
            observer.reconcile(observation.ledger, event.correlation_id, outcome).outcome
        )
        self.assertEqual(
            observer.reconcile(observation.ledger, "e-unrelated", None).status,
            observer.NO_WINDOW,
        )


def _journal_for(event: Any) -> Any:
    """The journal the real decision path would have written for this event.

    Built through the planner rather than hand-written, so the comparison is
    against a journal with the effects Continuum would actually have planned --
    a hand-written fixture would make this test agree with the observer by
    construction.
    """

    from continuum.shadow import state

    for observed in fixtures.states_for(event.scenario):
        journal = planner.plan(event, observed, fixtures.config())
        if journal is not None:
            return journal
    raise AssertionError("the planner refused every fixture for {!r}".format(event.event_type))


class ObserverCliTest(unittest.TestCase):
    """The two commands that make the plane a comparison rather than a report."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="shadow-observer-")
        self._real_client = github.GitHubClient
        # The command refuses to run without a token, and the token is read from
        # the environment, so this test sets one. Relying on another test module to
        # have left GITHUB_TOKEN behind is how a test passes in one order and fails
        # in another.
        self._real_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = "ghs_read_only_for_the_observer"
        self.addCleanup(self._restore_token)

    def _restore_token(self) -> None:
        if self._real_token is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = self._real_token

    def tearDown(self) -> None:
        github.GitHubClient = self._real_client
        for root, _, files in os.walk(self.directory, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            if root != self.directory:
                os.rmdir(root)
        os.rmdir(self.directory)

    def _write(self, name: str, payload: Mapping[str, Any]) -> str:
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def _observe(self, event: Any, production: Production, *extra: str, offset: int = LATE) -> int:
        """Run the command against the fake, standing in for the GitHub client.

        Substituted at the client class rather than by patching the observer's
        reads, so the command still goes through the real allowlist and the real
        client setup -- which is the code path the workflow uses. The clock is
        pinned so the command is reproducible rather than depending on when it
        ran.
        """

        self._write("event.json", event.describe())
        github.GitHubClient = lambda *a, **k: production  # type: ignore[assignment]
        return cli_main(
            [
                "observe",
                "--event",
                os.path.join(self.directory, "event.json"),
                "--repo",
                REPOSITORY,
                "--out",
                self.directory,
                "--now-ms",
                str(_at(event, offset)),
                *extra,
            ]
        )

    def test_observe_writes_a_ledger_evidence_and_an_outcome(self) -> None:
        self.assertEqual(
            self._observe(fixtures.release_event(), release_published_production()), 0
        )
        self.assertTrue(os.path.exists(os.path.join(self.directory, "observer", "ledger.json")))
        evidence = os.listdir(os.path.join(self.directory, "observer", "evidence"))
        self.assertEqual(len(evidence), 1)
        outcomes = os.listdir(os.path.join(self.directory, "outcomes"))
        self.assertEqual(len(outcomes), 1)
        with open(
            os.path.join(self.directory, "outcomes", outcomes[0]), encoding="utf-8"
        ) as handle:
            document = json.load(handle)
        self.assertEqual(document["schema"], "continuum.shadow-outcome/v1")
        self.assertEqual(document["terminal_status"], "ok")

    def test_observe_writes_a_ledger_even_when_no_outcome_is_warranted(self) -> None:
        # Inside the horizon, so the window stays open. The ledger is still the
        # artifact: without it there is no way to tell "still waiting" from
        # "never ran", which is the state a reviewer cannot act on.
        self.assertEqual(
            self._observe(
                issue_event(), issue_lifecycle_production(), offset=EARLY
            ),
            0,
        )
        self.assertTrue(os.path.exists(os.path.join(self.directory, "observer", "ledger.json")))
        outcomes = os.path.join(self.directory, "outcomes")
        self.assertEqual(os.path.isdir(outcomes) and os.listdir(outcomes), False)

    def test_a_failed_read_writes_a_ledger_and_no_outcome(self) -> None:
        production = issue_lifecycle_production()
        production.fails = ("list_workflow_runs",)
        self.assertEqual(self._observe(issue_event(), production), 0)
        self.assertTrue(os.path.exists(os.path.join(self.directory, "observer", "ledger.json")))
        outcomes = os.path.join(self.directory, "outcomes")
        self.assertEqual(os.path.isdir(outcomes) and os.listdir(outcomes), False)

    def test_reconcile_pairs_and_reports_the_divergence(self) -> None:
        event = fixtures.release_event()
        self.assertEqual(self._observe(event, release_published_production()), 0)
        outcomes = os.path.join(self.directory, "outcomes")
        journal_path = self._write("journal.json", _journal_for(event).describe())
        code = cli_main(
            [
                "reconcile",
                "--journals",
                journal_path,
                "--ledger",
                os.path.join(self.directory, "observer", "ledger.json"),
                "--outcomes",
                *sorted(os.path.join(outcomes, name) for name in os.listdir(outcomes)),
                "--out",
                self.directory,
            ]
        )
        # A real publication where the dry run planned one is a divergence, and
        # the exit code says so. The plane itself worked, so this is a validation
        # failure rather than a crash.
        self.assertEqual(code, cli.VALIDATION_FAILED)
        with open(
            os.path.join(self.directory, "observer", "reconciliation.json"), encoding="utf-8"
        ) as handle:
            document = json.load(handle)
        self.assertEqual(document["paired"], 1)
        self.assertEqual(document["windows_still_open"], 0)

    def test_reconcile_reports_a_window_still_open_rather_than_a_divergence(self) -> None:
        event = issue_event()
        self.assertEqual(
            self._observe(event, issue_lifecycle_production(), offset=EARLY), 0
        )
        journal_path = self._write("journal.json", _journal_for(event).describe())
        code = cli_main(
            [
                "reconcile",
                "--journals",
                journal_path,
                "--ledger",
                os.path.join(self.directory, "observer", "ledger.json"),
                "--out",
                self.directory,
            ]
        )
        # No outcome yet is not a disagreement. It is the ordinary state of a
        # correlation that has not finished, and counting it as a divergence
        # would report every long decision as a mismatch.
        self.assertEqual(code, 0)
        with open(
            os.path.join(self.directory, "observer", "reconciliation.json"), encoding="utf-8"
        ) as handle:
            document = json.load(handle)
        self.assertEqual(document["paired"], 0)
        self.assertEqual(document["windows_still_open"], 1)


    def test_an_outcome_carries_the_span_of_the_window_it_describes(self) -> None:
        # The links say which window this belongs to; the top-level timestamps
        # say when the thing happened. A reader filtering outcomes by time cannot
        # follow a link, so empty top-level timestamps would read as an event
        # that happened at no particular time.
        event = fixtures.release_event()
        self.assertEqual(self._observe(event, release_published_production()), 0)
        name = os.listdir(os.path.join(self.directory, "outcomes"))[0]
        with open(
            os.path.join(self.directory, "outcomes", name), encoding="utf-8"
        ) as handle:
            document = json.load(handle)
        self.assertEqual(document["window_started_at"], document["links"]["opened_at"])
        self.assertEqual(document["window_ended_at"], document["links"]["closed_at"])
        self.assertNotEqual(document["window_started_at"], "")

    def test_the_summary_reports_an_open_window_as_open(self) -> None:
        self.assertEqual(
            self._observe(issue_event(), issue_lifecycle_production(), offset=EARLY), 0
        )
        rendered = self._summary()
        self.assertIn("Continuum shadow observer", rendered)
        self.assertIn("still open", rendered)
        # Absence of an outcome is not the claim that production did nothing.
        self.assertIn("not the same as production having done nothing", rendered)

    def test_the_summary_reports_what_production_did(self) -> None:
        self.assertEqual(self._observe(fixtures.release_event(), release_published_production()), 0)
        rendered = self._summary()
        self.assertIn("What production did", rendered)
        self.assertIn("release.publish", rendered)
        self.assertIn("release:v0.1.0", rendered)
        self.assertIn("release-planning", rendered)
        self.assertIn("No reconciliation pass", rendered)

    def test_the_summary_reports_the_pairing_and_its_outstanding_work(self) -> None:
        event = fixtures.release_event()
        self.assertEqual(self._observe(event, release_published_production()), 0)
        journal = self._write("journal.json", _journal_for(event).describe())
        outcome = os.path.join(self.directory, "outcomes", os.listdir(
            os.path.join(self.directory, "outcomes")
        )[0])
        self.assertEqual(
            cli_main(
                [
                    "reconcile",
                    "--ledger",
                    os.path.join(self.directory, "observer", "ledger.json"),
                    "--journals",
                    journal,
                    "--outcomes",
                    outcome,
                    "--out",
                    self.directory,
                ]
            ),
            1,
        )
        rendered = self._summary()
        self.assertIn("Pairing with Continuum's decisions", rendered)
        self.assertIn("e-release", rendered)

    def test_the_summary_survives_a_pass_that_observed_nothing(self) -> None:
        os.makedirs(os.path.join(self.directory, "observer"), exist_ok=True)
        self.assertEqual(self._summary(), self._summary())
        rendered = self._summary()
        self.assertIn("nothing was observed", rendered)

    def _summary(self) -> str:
        """Render the observer summary the way the workflow's job step does."""

        path = os.path.join(self.directory, "summary.md")
        with open(path, "w", encoding="utf-8") as handle:
            stdout, sys.stdout = sys.stdout, handle
            try:
                self.assertEqual(
                    cli_main(["summary", "--out", self.directory, "--observer"]), 0
                )
            finally:
                sys.stdout = stdout
        with open(path, encoding="utf-8") as handle:
            return handle.read()


class ObserverSafetyTest(unittest.TestCase):
    """The observer must not be able to write, whatever it is handed."""

    def test_no_observer_read_names_a_mutating_call_site(self) -> None:
        mutating = {
            site
            for spec in effects.EFFECT_KINDS
            for site in (spec.adapters or ()) + spec.attributes
        }
        for name in observer.OBSERVER_READ_METHODS:
            qualified = "GitHubClient.{}".format(name)
            self.assertNotIn(
                qualified,
                mutating,
                "{} is on the observer's allowlist and is registered as a call "
                "site that mutates".format(qualified),
            )

    def test_every_observer_read_issues_no_mutating_verb(self) -> None:
        import inspect

        for name in observer.OBSERVER_READ_METHODS:
            source = inspect.getsource(getattr(GitHubClient, name))
            for verb in ("POST", "PATCH", "DELETE", "PUT"):
                self.assertNotIn(
                    '"{}"'.format(verb),
                    source,
                    "{} is on the observer's allowlist and issues {}".format(name, verb),
                )
                self.assertNotIn("'{}'".format(verb), source)

    def test_the_module_imports_nothing_that_can_write(self) -> None:
        import inspect

        source = inspect.getsource(observer)
        for forbidden in ("subprocess", "os.system", "shutil.rmtree", "urllib.request"):
            self.assertNotIn(
                forbidden, source, "the observer must not reach {}".format(forbidden)
            )

    def test_a_timestamp_it_cannot_parse_keeps_the_record(self) -> None:
        # The whole point of the central filter is that last week's approving
        # review is not this window's action. But a timestamp this module cannot
        # read is not a timestamp from before the window either, and dropping the
        # record on the strength of a formatting quirk would lose a fact the
        # artifact is the only copy of. Kept, attributed, and visible.
        window = observer.Window(
            window_id="w",
            repository=REPOSITORY,
            subject="pr:3",
            scenario="merge-decision",
            opened_at="2026-01-02T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
            head_sha="",
            correlation_ids=(),
        )
        self.assertTrue(observer._within_window(window, "not a timestamp"))
        self.assertTrue(observer._within_window(window, ""))
        self.assertTrue(observer._within_window(window, "2026-01-03T00:00:00Z"))
        self.assertFalse(observer._within_window(window, "2026-01-01T00:00:00Z"))

if __name__ == "__main__":
    unittest.main()
