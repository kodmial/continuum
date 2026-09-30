"""Live capture: what the shadow plane reads, and what it refuses to read.

The capture is the half of issue #87 that decides whether a shadow decision is a
decision about the *same* world the production controller saw. Three properties
are worth a test each, because each can fail while the others still pass:

* **Only the read surface is touched.** A capture that reached a mutating method
  would be a hole in the write barrier, and the allowlist check is at the call
  site, so a new call site is where it can go wrong.
* **An unreadable field is recorded, not defaulted.** A guard that cannot see the
  review threads must fail closed; a capture that quietly substituted "no
  unresolved threads" would turn an outage into a merge.
* **Ordinary issues are issues.** GitHub's issues endpoint also returns pull
  requests, and the queue's work is the other kind, so a capture that counted
  both would decide about a queue that does not exist.
"""

from __future__ import annotations

import pathlib
import unittest
from typing import Any, Dict, List, Mapping, Sequence

from continuum.shadow import capture


class ReadFailure(Exception):
    """Stands in for ``continuum.review.github.GitHubError``."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class NotFound(ReadFailure):
    def __init__(self, path: str) -> None:
        super().__init__("GitHub API GET {} failed with status 404".format(path), 404)
from continuum.shadow.effects import READ_ONLY_METHODS
from continuum.shadow.event import from_payload


class FakeReadClient:
    """A read surface over fixed data, and a record of what was asked for.

    Anything not named here raises, so a capture that reaches a method the test
    did not intend to expose fails rather than silently returning a fixture.
    """

    def __init__(
        self,
        issues: Mapping[int, Mapping[str, Any]] = None,
        pulls: Mapping[int, Mapping[str, Any]] = None,
        unreadable_pulls: Sequence[int] = (),
        **payloads: Any,
    ) -> None:
        self.issues = dict(issues or {})
        self.pulls = dict(pulls or {})
        self.unreadable_pulls = set(unreadable_pulls)
        self.payloads = dict(payloads)
        self.asked: List[str] = []
        self.graphql_variables: List[Mapping[str, Any]] = []

    def _record(self, name: str) -> None:
        self.asked.append(name)

    def get_issue(self, number: int, **kwargs: Any) -> Mapping[str, Any]:
        self._record("get_issue")
        if number not in self.issues:
            raise KeyError("no issue {}".format(number))
        return self.issues[number]

    def get_pull(self, number: int, **kwargs: Any) -> Mapping[str, Any]:
        self._record("get_pull")
        if number not in self.pulls:
            if number in self.unreadable_pulls:
                raise ReadFailure(
                    "GitHub API GET /repos/kodmial/nanodictate/pulls/{} "
                    "failed with status 503".format(number),
                    503,
                )
            raise NotFound("/repos/kodmial/nanodictate/pulls/{}".format(number))
        return self.pulls[number]

    def list_pulls(self, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_pulls")
        # The real endpoint returns whole pull payloads, heads included, which is
        # what lets a capture find the pull request a commit belongs to.
        return [dict(self.pulls[number]) for number in sorted(self.pulls)]

    def paginate(self, path: str) -> List[Mapping[str, Any]]:
        self._record("paginate")
        if "issues" not in path:
            raise AssertionError("unexpected paginated path {}".format(path))
        # The real endpoint mixes in pull requests, because a pull request is an
        # issue. The fixture reproduces that so the filtering is tested.
        rows: List[Mapping[str, Any]] = [
            self.issues[number] for number in sorted(self.issues)
        ]
        for number in sorted(self.pulls):
            row = dict(self.issues.get(number) or {"number": number})
            row["pull_request"] = {"url": "https://api.github.com/pulls/{}".format(number)}
            rows.append(row)
        return rows

    def list_issue_comments(self, number: int, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_issue_comments")
        return self.payloads.get("issue_comments", {}).get(number, [])

    def list_reviews(self, number: int, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_reviews")
        return self.payloads.get("reviews", {}).get(number, [])

    def review_threads(self, number: int, **kwargs: Any) -> Any:
        self._record("review_threads")
        threads = self.payloads.get("threads", {}).get(number)
        if threads is None:
            raise KeyError("no threads for {}".format(number))
        return threads

    def list_check_runs(self, sha: str, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_check_runs")
        return self.payloads.get("check_runs", {}).get(sha, [])

    def combined_status_for_ref(self, sha: str, **kwargs: Any) -> Mapping[str, Any]:
        self._record("combined_status_for_ref")
        return self.payloads.get("combined_status", {}).get(
            sha, {"state": "success", "statuses": []}
        )

    def list_pull_files(self, number: int, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_pull_files")
        return self.payloads.get("files", {}).get(number, [])

    def list_review_comments(self, number: int, **kwargs: Any) -> List[Mapping[str, Any]]:
        self._record("list_review_comments")
        return self.payloads.get("review_comments", {}).get(number, [])

    def file_at_ref(self, path: str, ref: str, **kwargs: Any) -> bytes:
        self._record("file_at_ref")
        return self.payloads.get("files_at_ref", {}).get((path, ref), b"")

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        self._record("graphql")
        self.graphql_variables.append(dict(variables))
        if "ShadowTag" not in query:
            raise AssertionError("unexpected graphql query")
        return self.payloads.get("graphql", {}).get(
            str(variables.get("ref")), {"repository": {"ref": None}}
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("capture must not use the generic request surface")

    def add_labels(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("capture must not add labels")

    def create_issue_comment(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("capture must not comment")


def _issue(number: int, **overrides: Any) -> Dict[str, Any]:
    row = {
        "number": number,
        "state": "open",
        "title": "issue {}".format(number),
        "body": "a body",
        "labels": [{"name": "opencode-ready-for-agent"}],
        "user": {"login": "octocat"},
        "author_association": "MEMBER",
        "updated_at": "2026-03-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _pull(number: int, **overrides: Any) -> Dict[str, Any]:
    row = {
        "number": number,
        "state": "open",
        "title": "pull {}".format(number),
        "head": {"sha": "a" * 40, "ref": "opencode/fix-{}".format(number), "repo": {"full_name": "kodmial/nanodictate"}},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": "kodmial/nanodictate"}},
        "user": {"login": "octocat"},
        "labels": [{"name": "opencode-ready-for-agent"}],
        "draft": False,
        "mergeable_state": "clean",
        "merged": False,
        "updated_at": "2026-03-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _event(**overrides: Any):
    payload = {
        "event_type": "issue.opened",
        "action": "opened",
        "repository": "kodmial/nanodictate",
        "number": 7,
        "actor": "octocat",
    }
    payload.update(overrides)
    return from_payload(payload)


class ReadSurfaceTests(unittest.TestCase):
    def test_every_method_the_capture_calls_is_on_the_allowlist(self) -> None:
        client = FakeReadClient(issues={7: _issue(7)}, pulls={43: _pull(43)})
        capture.capture_state(client, _event(), captured_at_ms=1000)
        self.assertTrue(client.asked)
        for name in client.asked:
            self.assertIn(name, READ_ONLY_METHODS, name)

    def test_the_capture_refuses_to_call_a_method_off_the_allowlist(self) -> None:
        # The allowlist is a list of names, checked where a read is issued, so a
        # new call site naming a mutator fails instead of quietly writing.
        client = FakeReadClient(issues={7: _issue(7)})
        with self.assertRaises(capture.CaptureError) as caught:
            capture._read(client, "add_labels", 7, ["opencode-conflict-repair"])
        self.assertEqual(caught.exception.code, "write_capable_read")

    def test_the_capture_reports_a_client_that_is_missing_a_read(self) -> None:
        class NoGitHub(FakeReadClient):
            get_pull = None

        client = NoGitHub(issues={7: _issue(7)})
        with self.assertRaises(capture.CaptureError) as caught:
            capture._read(client, "get_pull", 7)
        self.assertEqual(caught.exception.code, "missing_read_method")

    def test_a_capture_never_uses_the_generic_request_surface(self) -> None:
        client = FakeReadClient(issues={7: _issue(7)})
        capture.capture_state(client, _event())
        self.assertNotIn("request", client.asked)

    def test_an_issue_that_is_not_a_pull_request_is_not_an_unreadable_field(self) -> None:
        # Issue 7 is an ordinary issue, so the pull request read answers 404. That
        # is an answer: recording it as unreadable would make every
        # issue-lifecycle capture look incomplete.
        client = FakeReadClient(issues={7: _issue(7)})
        state = capture.capture_state(client, _event(), captured_at_ms=1000)
        self.assertFalse([key for key in state.unreadable if key.startswith("pull:")])
        self.assertIsNone(state.pull(7))
        self.assertIsNotNone(state.issue(7))

    def test_an_event_about_a_pull_request_captures_its_thread_state(self) -> None:
        client = FakeReadClient(
            issues={43: _issue(43)},
            pulls={43: _pull(43)},
            threads={43: [{"id": "t1", "isResolved": False, "path": "a.py", "line": 3}]},
        )
        state = capture.capture_state(
            client,
            _event(
                event_type="pull_request.review",
                action="submitted",
                number=43,
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        self.assertIn("review_threads", client.asked)
        self.assertTrue(state.review_threads[43])


class UnreadableFieldTests(unittest.TestCase):
    def test_an_unreadable_review_thread_set_is_recorded_not_defaulted(self) -> None:
        # No `threads` payload for this pull request, so `review_threads` raises.
        client = FakeReadClient(
            issues={43: _issue(43)},
            pulls={43: _pull(43)},
        )
        state = capture.capture_state(
            client,
            _event(
                event_type="pull_request.review",
                action="submitted",
                number=43,
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        self.assertFalse(state.is_complete())
        self.assertIn("review_threads:43", state.unreadable)
        # Absent means "could not tell", and the planner refuses to act on it.
        self.assertNotIn(43, state.review_threads)

    def test_a_readable_thread_set_is_recorded(self) -> None:
        client = FakeReadClient(
            issues={43: _issue(43)},
            pulls={43: _pull(43)},
            threads={43: [{"id": "t1", "isResolved": False, "path": "a.py", "line": 3}]},
        )
        state = capture.capture_state(
            client,
            _event(
                event_type="pull_request.review",
                action="submitted",
                number=43,
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        self.assertTrue(state.review_threads[43])
        self.assertNotIn("review_threads:43", state.unreadable)

    def test_a_failed_read_of_the_pull_request_is_recorded(self) -> None:
        client = FakeReadClient(issues={7: _issue(7)}, unreadable_pulls=[99])
        state = capture.capture_state(
            client,
            _event(
                event_type="pull_request.review",
                action="submitted",
                number=99,
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        self.assertTrue(any(key.startswith("pull:99") for key in state.unreadable))
        self.assertIsNone(state.pull(99))


class IssueQueueTests(unittest.TestCase):
    def test_open_issues_are_read_from_the_issues_endpoint(self) -> None:
        client = FakeReadClient(issues={7: _issue(7), 8: _issue(8)})
        state = capture.capture_state(client, _event(number=0, event_type="schedule.tick"))
        self.assertIn("paginate", client.asked)
        self.assertEqual(sorted(issue.number for issue in state.issues), [7, 8])

    def test_pull_requests_are_not_counted_as_queue_work(self) -> None:
        # 43 exists as a pull request and therefore appears in the issues
        # endpoint, but it is not something the queue can hand to an agent.
        client = FakeReadClient(issues={7: _issue(7)}, pulls={43: _pull(43)})
        state = capture.capture_state(client, _event(number=0, event_type="schedule.tick"))
        self.assertEqual(sorted(issue.number for issue in state.issues), [7])
        self.assertIn(43, [pull.number for pull in state.pulls])

    def test_a_queue_event_captures_the_open_pull_requests_too(self) -> None:
        # A schedule tick is a merge decision over the whole open queue, so every
        # open pull request is in scope.
        client = FakeReadClient(issues={43: _issue(43)}, pulls={43: _pull(43)})
        state = capture.capture_state(client, _event(number=0, event_type="schedule.tick"))
        self.assertIn(43, [pull.number for pull in state.pulls])

    def test_a_push_run_finds_the_pull_request_whose_head_it_failed_on(self) -> None:
        # A red run on a push names a commit, not a pull request, so the capture
        # has to find it among the open ones.
        client = FakeReadClient(
            issues={43: _issue(43), 44: _issue(44)},
            pulls={43: _pull(43), 44: _pull(44, head={"sha": "c" * 40, "ref": "b", "repo": {"full_name": "kodmial/nanodictate"}})},
        )
        state = capture.capture_state(
            client,
            _event(
                number=0,
                event_type="check_suite.completed",
                action="completed",
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        self.assertEqual([pull.number for pull in state.pulls], [43])
        self.assertIn("a" * 40, state.check_runs)

    def test_a_push_run_with_no_pull_request_is_not_a_gap_in_the_capture(self) -> None:
        client = FakeReadClient(issues={43: _issue(43)}, pulls={43: _pull(43, head={"sha": "c" * 40, "ref": "b", "repo": {"full_name": "kodmial/nanodictate"}})})
        state = capture.capture_state(
            client,
            _event(number=0, event_type="check_suite.completed", action="completed", head_sha="a" * 40),
            captured_at_ms=1000,
        )
        self.assertEqual(state.pulls, ())
        self.assertEqual(state.unreadable, {})

    def test_a_failed_issue_listing_is_recorded_rather_than_guessed(self) -> None:
        class Broken(FakeReadClient):
            def paginate(self, path: str) -> List[Mapping[str, Any]]:
                self.asked.append("paginate")
                raise KeyError("no issues listing")

        client = Broken(issues={7: _issue(7)})
        state = capture.capture_state(client, _event(number=0, event_type="schedule.tick"))
        self.assertIn("open_issues", state.unreadable)
        self.assertEqual(state.issues, ())


class ProvenanceTests(unittest.TestCase):
    def test_the_capture_names_its_repository_owner_and_trust(self) -> None:
        client = FakeReadClient(issues={7: _issue(7)}, pulls={43: _pull(43)})
        state = capture.capture_state(
            client, _event(number=43, head_sha="a" * 40), captured_at_ms=1234, trusted_actors="bot"
        )
        self.assertEqual(state.repository, "kodmial/nanodictate")
        self.assertEqual(state.repository_owner, "kodmial")
        self.assertEqual(state.trusted_actors, "bot")
        self.assertEqual(state.captured_at_ms, 1234)

    def test_a_capture_round_trips_through_its_own_document(self) -> None:
        from continuum.shadow.state import state_from_payload

        client = FakeReadClient(issues={7: _issue(7)}, pulls={43: _pull(43)})
        state = capture.capture_state(
            client,
            _event(
                event_type="pull_request.review",
                action="submitted",
                number=43,
                head_sha="a" * 40,
            ),
            captured_at_ms=1000,
        )
        document = state.as_capture()
        self.assertEqual(state_from_payload(document).as_capture(), document)

    def test_a_capture_with_no_repository_is_refused(self) -> None:
        # The event normalizer already refuses one, so the capture's own check
        # is reached with an event built without it.
        client = FakeReadClient()
        event = _event()
        object.__setattr__(event, "repository", "")
        with self.assertRaises(capture.CaptureError) as caught:
            capture.capture_state(client, event)
        self.assertEqual(caught.exception.code, "no_repository")

    def test_a_malformed_repository_is_refused(self) -> None:
        client = FakeReadClient()
        event = _event()
        object.__setattr__(event, "repository", "nanodictate")
        with self.assertRaises(capture.CaptureError) as caught:
            capture.capture_state(client, event)
        self.assertEqual(caught.exception.code, "malformed_repository")



def _release_event(**overrides: Any):
    payload = {
        "event_type": "release.event",
        "action": "published",
        "repository": "kodmial/nanodictate",
        "actor": "octocat",
        "release": {"tag": "v1.2.3", "source_sha": None},
    }
    payload.update(overrides)
    return from_payload(payload)


def _tag_response(commit: str, annotated: bool = False) -> Dict[str, Any]:
    target: Dict[str, Any] = {"oid": commit}
    if annotated:
        # An annotated tag points at a tag object whose own target is the commit.
        return {"repository": {"ref": {"target": {"oid": "d" * 40, "target": target}}}}
    return {"repository": {"ref": {"target": target}}}


class ReleaseTagTests(unittest.TestCase):
    def test_a_release_tag_is_resolved_to_the_commit_it_names(self) -> None:
        client = FakeReadClient(graphql={"refs/tags/v1.2.3": _tag_response("c" * 40)})
        state = capture.capture_state(client, _release_event())
        self.assertEqual(state.release_commit, "c" * 40)
        self.assertEqual(state.unreadable, {})

    def test_an_annotated_tag_resolves_to_its_commit(self) -> None:
        client = FakeReadClient(
            graphql={"refs/tags/v1.2.3": _tag_response("c" * 40, annotated=True)},
        )
        state = capture.capture_state(client, _release_event())
        self.assertEqual(state.release_commit, "c" * 40)

    def test_a_release_that_already_names_a_commit_is_not_looked_up(self) -> None:
        client = FakeReadClient()
        state = capture.capture_state(
            client, _release_event(release={"tag": "v1.2.3", "source_sha": "e" * 40})
        )
        self.assertEqual(state.release_commit, "")
        self.assertEqual(client.graphql_variables, [])

    def test_a_tag_that_does_not_resolve_is_recorded_not_guessed(self) -> None:
        client = FakeReadClient(graphql={})
        state = capture.capture_state(client, _release_event())
        self.assertEqual(state.release_commit, "")
        self.assertIn("release_commit", state.unreadable)
        self.assertFalse(state.is_complete())

    def test_a_failed_tag_read_is_recorded(self) -> None:
        class Broken(FakeReadClient):
            def graphql(self, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
                self.asked.append("graphql")
                raise ReadFailure("GitHub API POST /graphql failed with status 502", 502)

        state = capture.capture_state(Broken(), _release_event())
        self.assertEqual(state.release_commit, "")
        self.assertIn("release_commit", state.unreadable)

    def test_an_event_that_is_not_a_release_reads_no_tag(self) -> None:
        client = FakeReadClient(issues={7: _issue(7)})
        capture.capture_state(client, _event())
        self.assertEqual(client.graphql_variables, [])

    def test_the_resolved_commit_pins_the_event_the_planner_sees(self) -> None:
        from continuum.shadow.config import load as load_config
        from continuum.shadow.planner import plan
        from continuum.shadow.state import state_from_payload
        from tests import shadow_support as fixtures

        client = FakeReadClient(graphql={"refs/tags/v1.2.3": _tag_response("c" * 40)})
        event = _release_event()
        state = capture.capture_state(client, event)
        config = load_config(pathlib.Path(__file__).resolve().parents[1] / ".continuum.yml")
        unpinned = plan(event, state, config)
        self.assertIn("unpinned", unpinned.reason, unpinned.reason)
        pinned = plan(event.with_release_source(state.release_commit), state, config)
        self.assertNotIn("unpinned", pinned.reason, pinned.reason)
        # And the event the journal records is the pinned one.
        described = pinned.describe()["event"]["release"]
        self.assertEqual(described["source_sha"], "c" * 40, pinned.describe())
        # A recorded capture keeps its pin, so a replay cannot drift from it.
        self.assertEqual(
            state_from_payload(state.as_capture()).release_commit, "c" * 40
        )


if __name__ == "__main__":
    unittest.main()
