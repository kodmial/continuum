"""The chain's first and last words.

Eligibility and notes are the two components a release cannot be run without and
that had no production implementation — the core was proven against fixtures,
which proves a state machine and not a release. These tests are about the two
properties that make them worth having rather than a pair of stubs: eligibility
refuses everything it cannot vouch for, and notes never invent a claim.
"""

from __future__ import annotations

import unittest

from continuum.release import plane
from continuum.release.contract import (
    NotesRequest,
    ReleaseEvent,
    ReleaseNotes,
)
from continuum.release.version import VersionPolicy

SHA = "a" * 40
OTHER_SHA = "b" * 40


def event(**overrides) -> ReleaseEvent:
    values = {
        "repository": "example/widgets",
        "name": "tag-push",
        "sha": SHA,
        "ref": "refs/tags/v1.4.0",
        "tag": "v1.4.0",
        "default_branch": "main",
    }
    values.update(overrides)
    return ReleaseEvent(**values)


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subject = plane.eligibility_for()

    def test_admits_a_tag_push(self):
        verdict = self.subject.evaluate(event())
        self.assertTrue(verdict.eligible)
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, "")

    def test_admits_a_dispatch_from_the_default_branch(self):
        """A manual release is how a repository cuts a version without a tag
        yet, so the branch it is dispatched from is the thing to check."""

        verdict = self.subject.evaluate(
            event(name="workflow-dispatch", ref="refs/heads/main")
        )
        self.assertTrue(verdict.eligible)

    def test_refuses_a_pull_request_ref(self):
        """The property the whole job isolation rests on.

        A release built from a pull request ref is somebody's unmerged work, and
        the review gate that would have caught it runs on pull requests, not on
        the ref this is checked at.
        """

        verdict = self.subject.evaluate(event(name="push", ref="refs/pull/3/merge"))
        self.assertFalse(verdict.eligible)
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, "ref-not-releasable")
        self.assertIn("refs/pull/3/merge", verdict.reason)

    def test_refuses_a_feature_branch(self):
        verdict = self.subject.evaluate(event(name="push", ref="refs/heads/feature"))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "ref-not-releasable")

    def test_refuses_a_version_this_repository_does_not_publish(self):
        verdict = self.subject.evaluate(event(tag="nightly"))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "version-not-admissible")

    def test_leaves_the_version_series_to_the_version_stage(self):
        """Eligibility checks the ref and the tag; the policy checks the series.

        `VersionPolicy(allowed_series=("0",))` means this repository publishes
        0.x, so 1.4.0 must not be released — but that rule belongs to the
        version stage, which asks the policy with the full context. A second
        opinion here would either drift from that rule or refuse something the
        version stage would have admitted, and a release blocked for a reason
        the next stage contradicts is a release nobody can unblock.
        """

        strict = plane.eligibility_for(VersionPolicy(allowed_series=("0",)))
        self.assertTrue(strict.evaluate(event()).eligible)
        self.assertEqual(
            strict.evaluate(event()).code,
            "",
            "an eligible verdict carries no reason, so the chain records no prose "
            "about a check this component did not make",
        )

    def test_the_policy_still_refuses_a_series_it_does_not_publish(self):
        from continuum.release.version import VersionError

        policy = VersionPolicy(allowed_series=("0",))
        self.assertTrue(policy.admit("0.9.1"))
        with self.assertRaises(VersionError):
            policy.require("1.4.0")

    def test_an_abbreviated_commit_blocks_rather_than_merely_refusing(self):
        """Blocking, because it is a mistake rather than an answer.

        A ref that is not releaseable is a considered "no" and must stay green;
        a commit that cannot be compared against anything is a broken pin, and
        a green run would say a release is possible when it is not.
        """

        subject = plane.eligibility_for()
        with self.assertRaises(Exception):
            event(sha="abc1234")
        # And with a full SHA substituted for the abbreviated one, the
        # eligibility question itself is separate from the event's own check.
        self.assertTrue(subject.evaluate(event(sha=OTHER_SHA)).eligible)

    def test_refuses_a_dispatch_that_names_no_ref(self):
        """With no ref there is nothing to say whether it came from main or a fork."""

        no_ref = self.subject.evaluate(event(name="workflow-dispatch", ref="", tag=""))
        self.assertFalse(no_ref.eligible)
        self.assertEqual(no_ref.code, "ref-absent")

    def test_admits_a_dispatch_from_the_default_branch_without_a_tag(self):
        """A dispatched release is cut from a branch, where no tag exists.

        Refusing it would leave a repository that tags its releases as the only
        path to a version, and admitting it with a fabricated tag would put a
        name in the release's record that nothing in the repository backs. The
        version is the identity here; the tag is created at publish.
        """

        verdict = self.subject.evaluate(
            event(name="workflow-dispatch", ref="refs/heads/main", tag="")
        )
        self.assertTrue(verdict.eligible, verdict.reason)

    def test_still_refuses_a_dispatch_from_a_branch_that_is_not_the_default(self):
        for ref in ("refs/heads/feature/thing", "refs/heads/mainline"):
            with self.subTest(ref=ref):
                verdict = self.subject.evaluate(
                    event(name="workflow-dispatch", ref=ref, tag="")
                )
                self.assertFalse(verdict.eligible)
                self.assertEqual(verdict.code, "ref-not-releasable")

    def test_still_refuses_a_push_with_no_tag(self):
        """Only a dispatch is tagless. Anything else claiming that is a mistake."""

        verdict = self.subject.evaluate(event(name="push", ref="refs/heads/main", tag=""))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "tag-absent")

    def test_refuses_a_tag_that_is_a_ref(self):
        verdict = self.subject.evaluate(event(tag="refs/tags/v1.4.0"))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "tag-malformed")

    def test_every_refusal_carries_a_code_and_a_reason(self):
        """A refusal a reader cannot act on is a refusal somebody re-runs.

        The code is what a workflow branches on and the reason is what a human
        reads; the contract enforces both, and this checks the component is not
        quietly relying on that for a case it got wrong.
        """

        for verdict in (
            self.subject.evaluate(event(name="push", ref="refs/heads/feature")),
            self.subject.evaluate(event(tag="nightly")),
            self.subject.evaluate(event(tag="refs/tags/v1.4.0")),
        ):
            self.assertFalse(verdict.eligible)
            self.assertTrue(verdict.code, verdict.reason)
            self.assertTrue(verdict.reason.strip())

    def test_names_what_it_allows(self):
        """The refusal says which refs would have worked.

        A policy that says "no" without saying "and here is what yes looks like"
        costs a release every time somebody guesses wrong.
        """

        verdict = self.subject.evaluate(event(name="push", ref="refs/heads/feature"))
        self.assertIn("refs/tags/", verdict.reason)
        self.assertIn("refs/heads/main", verdict.reason)

    def test_supports_a_dry_run(self):
        self.assertTrue(plane.ReleaseEligibility.SUPPORTS_DRY_RUN)
        self.assertTrue(plane.TagNotes.SUPPORTS_DRY_RUN)

    def test_describes_itself_for_a_plan(self):
        described = self.subject.describe()
        self.assertEqual(described["name"], "release-eligibility")
        self.assertIn("refs/tags/", described["allowed_refs"])


class NotesTests(unittest.TestCase):
    def request(self, **overrides) -> NotesRequest:
        values = {
            "event": event(),
            "version": "1.4.0",
            "tag": "v1.4.0",
            "source_sha": SHA,
            "key": "release/example/widgets/1.4.0",
        }
        values.update(overrides)
        return NotesRequest(**values)

    def test_names_the_tag_and_the_commit(self):
        body = plane.TagNotes().build(self.request()).body
        self.assertIn("v1.4.0", body)
        self.assertIn(SHA[:12], body)

    def test_says_where_the_digests_are(self):
        """The one thing a note can point a consumer at that is a fact.

        The digests and the attestations were produced by this release, so the
        note can promise them without inventing anything.
        """

        body = plane.TagNotes().build(self.request()).body
        self.assertIn("SHA256SUMS.txt", body)
        self.assertIn("attestation", body)

    def test_never_claims_to_describe_what_changed(self):
        """A generated note that reads like a changelog is a changelog that lies.

        This component knows the tag, the commit, and the event. Anything about
        what the change *did* would be invented, and an invented changelog entry
        is worse than none: a reader trusts it.
        """

        body = plane.TagNotes().build(self.request()).body
        self.assertIn("does not describe what changed", body)
        for invented in ("Fixed", "Added support for", "Breaking change", "## Changes"):
            self.assertNotIn(invented, body)

    def test_points_at_a_changelog_when_given_one(self):
        body = plane.TagNotes(reference="https://example.invalid/CHANGELOG.md").build(
            self.request()
        ).body
        self.assertIn("https://example.invalid/CHANGELOG.md", body)
        self.assertIn("changelog is the prose", body)

    def test_carries_no_credential_in_the_body(self):
        """A note is published to everyone who can see the release.

        The note is generated from the event, so a body built from it can only
        contain what the event contains — this asserts that, because the day
        someone adds a secret to the request this is the test that notices.
        """

        notes = plane.TagNotes().build(self.request())
        self.assertNotIn("token", notes.body.lower())
        self.assertNotIn("secret", notes.body.lower())

    def test_says_which_built_it(self):
        notes = plane.TagNotes().build(self.request())
        self.assertEqual(notes.source, "tag-notes")

    def test_a_body_is_never_empty(self):
        """The contract already refuses one; this checks the component can
        always produce something the contract will accept."""

        notes = plane.TagNotes().build(self.request())
        self.assertTrue(notes.body.strip())
        self.assertIsInstance(notes, ReleaseNotes)

    def test_describes_itself_for_a_plan(self):
        self.assertEqual(plane.TagNotes().describe()["name"], "tag-notes")


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()
