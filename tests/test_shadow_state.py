"""The captured state and the record-only client.

Two properties are load-bearing and are what these tests check:

* the capture round-trips, so an artifact can be replayed from the file it was
  written to rather than from a live GitHub;
* the client reproduces the *read-after-write* behaviour of the real client.
  Without that, any code path that writes a marker and then reads it back -- the
  in-flight slot, the tracker comment, the label that says a repair is running --
  would be exercised in a shape production never takes, and the shadow run would
  bless a rollback path that cannot happen.
"""

from __future__ import annotations

import unittest

from continuum.shadow.effects import RecordingEffects, unmapped_mutators
from continuum.shadow.state import (
    ObservedState,
    PullView,
    ShadowGitHubClient,
    StateError,
    state_from_payload,
)
from tests import shadow_support as fixtures


class CaptureRoundTrip(unittest.TestCase):
    def test_capture_reparses_to_the_same_state(self) -> None:
        # The replay plane reads a second run from the artifact the first run
        # wrote. If the round trip lost anything, the replay would compare two
        # different decisions and call the difference drift.
        state = fixtures.state()
        again = state_from_payload(state.as_capture())
        self.assertEqual(state.describe(), again.describe())
        self.assertEqual(state.as_capture(), again.as_capture())

    def test_describe_is_a_summary_not_a_capture(self) -> None:
        # The journal artifact carries counts so a reviewer can see what evidence
        # existed. It must never be mistaken for something a replay could read:
        # re-parsing it yields an empty repository.
        summary = fixtures.state().describe()
        self.assertEqual(summary["issue_comments"]["7"], 1)
        replayed = state_from_payload(summary)
        # The head sha is what every decision needs, and a summary does not
        # carry it, so a replay from a summary cannot decide anything.
        self.assertEqual(replayed.require_pull(41).head_sha, "")

    def test_capture_records_the_base_repository(self) -> None:
        # GitHub's payload carries head and base repositories separately, and
        # the trust policy refuses a pull request whose *base* is not this
        # repository. A snapshot that only kept the head would let a fork
        # targeting an upstream branch look trusted.
        state = fixtures.state()
        pull = state.require_pull(42)
        self.assertEqual(pull.head_repository, "fork/widgets")
        self.assertEqual(pull.as_api()["base"]["repo"]["full_name"], "acme/widgets")

    def test_issue_author_association_reaches_the_api_view(self) -> None:
        api = fixtures.state().require_issue(7).as_api()
        self.assertEqual(api["user"]["login"], "acme")
        self.assertEqual(api["author_association"], "OWNER")

    def test_blockers_are_read_from_the_capture(self) -> None:
        state = fixtures.state()
        self.assertEqual(state.open_blockers(7), ((8, "open"),))
        # An issue nobody recorded blockers for has none, which is different
        # from an issue that was never looked at.
        self.assertEqual(state.open_blockers(8), ())

    def test_a_closed_blocker_is_not_a_blocker(self) -> None:
        payload = fixtures.capture()
        payload["blocked_by"] = {"7": [[8, "closed"]]}
        state = state_from_payload(payload)
        # The capture keeps the dependency, the state filters it: the scheduler
        # asks which blockers are open, and a closed one must not stop work.
        self.assertEqual(state.blocked_by[7], ((8, "closed"),))
        self.assertEqual(state.open_blockers(7), ())

    def test_absent_evidence_is_never_guessed(self) -> None:
        payload = fixtures.capture()
        del payload["review_threads"]
        state = state_from_payload(payload)
        # An absent key is "not captured", not "none captured". The review gate
        # needs the difference: with thread state unknown it must not treat the
        # review as clean, and a state that reported an empty list would.
        self.assertNotIn(41, state.review_threads)


class ClientReads(unittest.TestCase):
    def setUp(self) -> None:
        self.effects = RecordingEffects()
        self.client = ShadowGitHubClient(fixtures.state(), self.effects)

    def test_publishes_the_data_attributes_the_gate_reads(self) -> None:
        # ``gate.run_gate`` puts ``client.repository`` in its result. A client
        # without it would produce a gate result that differs from production's
        # for reasons that have nothing to do with the decision.
        self.assertEqual(self.client.repository, "acme/widgets")
        self.assertEqual(self.client.owner, "acme")
        self.assertEqual(self.client.name, "widgets")

    def test_uncaptured_evidence_is_refused_not_defaulted(self) -> None:
        payload = fixtures.capture()
        del payload["check_runs"]
        client = ShadowGitHubClient(state_from_payload(payload), RecordingEffects())
        with self.assertRaises(StateError) as caught:
            client.list_check_runs(fixtures.HEAD_A)
        self.assertEqual(caught.exception.code, "capture_incomplete")

    def test_file_at_ref_never_reads_the_working_tree(self) -> None:
        # The result would otherwise depend on which Continuum checkout ran it.
        self.assertIsNone(self.client.file_at_ref("src/app.py", fixtures.HEAD_A))

    def test_registry_covers_every_github_mutation(self) -> None:
        from continuum.review.github import GitHubClient

        self.assertEqual(unmapped_mutators(GitHubClient), ())


class ClientWriteOverlay(unittest.TestCase):
    """A recorded write has to be visible to the next read."""

    def setUp(self) -> None:
        self.effects = RecordingEffects()
        self.client = ShadowGitHubClient(fixtures.state(), self.effects)

    def test_a_new_comment_is_readable_afterwards(self) -> None:
        created = self.client.create_issue_comment(7, "<!-- slot -->\nreserved")
        listed = [comment["id"] for comment in self.client.list_issue_comments(7)]
        self.assertIn(created["id"], listed)
        self.assertEqual(len(listed), 2)

    def test_comment_ids_are_stable_for_a_body(self) -> None:
        first = self.client.create_issue_comment(8, "one")
        again = [c["id"] for c in self.client.list_issue_comments(8)]
        self.assertEqual(again[0], first["id"])

    def test_an_update_replaces_rather_than_duplicates(self) -> None:
        first = self.client.create_issue_comment(8, "one")
        self.client.create_issue_comment(8, "two")
        self.client.update_issue_comment(first["id"], "one edited")
        bodies = [c["body"] for c in self.client.list_issue_comments(8)]
        # The queue controller distinguishes an upsert from a create by whether
        # the body it just wrote is the one it reads back.
        self.assertEqual(bodies, ["one edited", "two"])

    def test_labels_are_readable_afterwards(self) -> None:
        self.client.add_labels(7, ["review-ready", "priority:p0"])
        self.assertEqual(
            sorted(label["name"] for label in self.client.get_issue(7)["labels"]),
            ["priority:p0", "review-ready"],
        )
        self.client.remove_label(7, "priority:p0")
        self.assertEqual(
            [label["name"] for label in self.client.get_issue(7)["labels"]],
            ["review-ready"],
        )

    def test_a_written_status_changes_the_combined_status(self) -> None:
        # The review gate writes its status and the merge controller reads the
        # combined result. If the read answered from the pre-write snapshot, the
        # merge controller would see a stale failure.
        self.assertEqual(self.client.combined_status_for_ref(fixtures.HEAD_A)["state"], "failure")
        self.client.create_status(fixtures.HEAD_A, "success", "reviewed", "CodeRabbit")
        combined = self.client.combined_status_for_ref(fixtures.HEAD_A)
        self.assertEqual([item["context"] for item in combined["statuses"]][0], "CodeRabbit")
        # The captured ``Packaging smoke`` failure still stands, so the combined
        # state must not move: GitHub's combined status is the worst context.
        self.assertEqual(combined["state"], "failure")
        self.client.create_status(fixtures.HEAD_A, "success", "smoke", "Packaging smoke")
        self.assertEqual(self.client.combined_status_for_ref(fixtures.HEAD_A)["state"], "success")

    def test_a_resolved_thread_stops_counting_as_unresolved(self) -> None:
        payload = fixtures.capture()
        payload["review_threads"] = {"41": [("t1", False, "src/app.py", 10)]}
        client = ShadowGitHubClient(state_from_payload(payload), RecordingEffects())
        self.assertEqual(client.unresolved_thread_comment_ids(41), {1})
        client.resolve_review_thread("t1")
        self.assertEqual(client.unresolved_thread_comment_ids(41), set())
        self.assertTrue(client.review_threads(41)[0]["isResolved"])

    def test_a_dispatch_puts_the_repair_slot_in_flight(self) -> None:
        # A dispatch is a write GitHub would hold, and a re-delivered webhook
        # decides against it.
        self.assertFalse(self.client.repair_in_flight(43))
        self.client.dispatch_workflow("opencode.yml", "main", {"pr_number": "43"})
        self.assertTrue(self.client.repair_in_flight(43))

    def test_a_written_marker_comment_is_found_by_a_later_upsert(self) -> None:
        # The production callers embed the marker in the body they write; the
        # client finds an existing marker comment by that text.
        first = self.client.upsert_marker_comment(8, "<!-- m -->", "<!-- m -->\nfirst")
        second = self.client.upsert_marker_comment(8, "<!-- m -->", "<!-- m -->\nsecond")
        self.assertEqual(first["id"], second["id"])
        bodies = [c["body"] for c in self.client.list_issue_comments(8)]
        self.assertEqual(bodies, ["<!-- m -->\nsecond"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
