"""Eligibility and release-notes policy.

Both are pure functions of an event and a document, so the interesting cases are
the ones a real repository hits: a documentation-only push, an explicit skip
marker, a changed-file list nobody could read, and a changelog that stopped at
the previous version.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from continuum.release import contract
from continuum.release.eligibility import (
    ChangelogNotes,
    DEFAULT_NONRELEASABLE_PATHS,
    ReleaseEligibility,
)


def event(**attributes):
    return contract.ReleaseEvent(
        repository="example/widgets",
        name=attributes.pop("name", "push"),
        sha="a" * 40,
        default_branch="main",
        tag=attributes.pop("tag", ""),
        attributes=tuple(sorted(attributes.items())),
    )


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.policy = ReleaseEligibility(default_branch="main")

    def test_a_code_change_on_the_default_branch_is_releasable(self):
        verdict = self.policy.evaluate(event(changed_files="src/app.c\nREADME"))
        self.assertTrue(verdict.eligible)

    def test_documentation_only_change_is_a_green_no_op(self):
        verdict = self.policy.evaluate(event(changed_files="docs/setup.md\nCHANGELOG.md"))
        self.assertFalse(verdict.eligible)
        self.assertFalse(verdict.blocking)
        self.assertEqual(verdict.code, "no-releasable-change")

    def test_the_default_nonreleasable_set_covers_the_release_plumbing(self):
        for path in ("docs/anything.txt", "README.md", ".github/workflows/x.yml", "LICENSE"):
            verdict = self.policy.evaluate(event(changed_files=path))
            self.assertFalse(verdict.eligible, path)

    def test_an_explicit_skip_marker_switches_release_off(self):
        verdict = self.policy.evaluate(
            event(changed_files="src/app.c", commit_message="fix: thing [skip release]")
        )
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "skip-release")

    def test_the_dispatch_input_is_authoritative(self):
        verdict = self.policy.evaluate(
            event(changed_files="src/app.c", **{"skip-release": "true"})
        )
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "skip-release")

    def test_a_marker_inside_a_longer_word_does_not_suppress_a_release(self):
        verdict = self.policy.evaluate(
            event(changed_files="src/app.c", commit_message="skip-release-notes tooling")
        )
        self.assertTrue(verdict.eligible)

    def test_an_unreadable_change_list_blocks_instead_of_guessing(self):
        verdict = self.policy.evaluate(
            event(changed_files="src/app.c", **{"changed-files-unavailable": "true"})
        )
        self.assertFalse(verdict.eligible)
        self.assertTrue(verdict.blocking)
        self.assertEqual(verdict.code, "eligibility-unavailable")

    def test_a_non_default_branch_is_ineligible(self):
        verdict = self.policy.evaluate(event(branch="feature", changed_files="src/app.c"))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "not-default-branch")

    def test_a_pull_request_event_is_never_a_release(self):
        verdict = self.policy.evaluate(
            event(name="workflow-dispatch", branch="main", changed_files="src/app.c")
        )
        self.assertTrue(verdict.eligible)
        with self.assertRaises(contract.ContractError):
            contract.ReleaseEvent(
                repository="example/widgets",
                name="pull_request",
                sha="a" * 40,
            )

    def test_a_missing_branch_blocks(self):
        verdict = self.policy.evaluate(event(changed_files="src/app.c"))
        # The default branch fills in for a push, which is what a workflow event
        # without an explicit branch means. An empty default is the blocking case.
        self.assertTrue(verdict.eligible)
        unknown = contract.ReleaseEvent(
            repository="example/widgets",
            name="push",
            sha="a" * 40,
            attributes=(("changed_files", "src/app.c"),),
        )
        blocking = ReleaseEligibility(default_branch="").evaluate(unknown)
        self.assertTrue(blocking.blocking)
        self.assertEqual(blocking.code, "eligibility-unavailable")

    def test_events_outside_the_policy_are_ineligible(self):
        policy = ReleaseEligibility(events=("tag-push",))
        verdict = policy.evaluate(event(changed_files="src/app.c"))
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "event-not-allowed")


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "CHANGELOG.md")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                "# Changelog\n\n"
                "## 2.0.0\n\n- the new thing\n- the other new thing\n\n"
                "## 1.9.0\n\n- the old thing\n"
            )

    def request(self, version="2.0.0", tag="v2.0.0"):
        return contract.NotesRequest(
            event=event(name="push", changed_files="src/app.c"),
            version=version,
            tag=tag,
            source_sha="a" * 40,
            key="k",
            workdir=self.directory.name,
        )

    def test_the_section_for_the_agreed_version_is_used(self):
        notes = ChangelogNotes().build(self.request())
        self.assertIn("the new thing", notes.body)
        self.assertNotIn("the old thing", notes.body)
        self.assertEqual(notes.source, "CHANGELOG.md")

    def test_a_missing_version_is_refused(self):
        with self.assertRaises(contract.ContractError) as caught:
            ChangelogNotes().build(self.request(version="3.0.0"))
        self.assertEqual(caught.exception.code, "notes-version-missing")

    def test_an_empty_section_is_refused(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("## 2.0.0\n\n## 1.9.0\n\n- old\n")
        with self.assertRaises(contract.ContractError):
            ChangelogNotes().build(self.request())

    def test_a_missing_changelog_is_refused(self):
        os.remove(self.path)
        with self.assertRaises(contract.ContractError) as caught:
            ChangelogNotes().build(self.request())
        self.assertEqual(caught.exception.code, "notes-source-missing")

    def test_event_notes_must_name_the_agreed_version(self):
        builder = ChangelogNotes(fallback_notes="This release is 1.0.0")
        with self.assertRaises(contract.ContractError) as caught:
            builder.build(self.request(version="2.0.0"))
        self.assertEqual(caught.exception.code, "notes-version-mismatch")

    def test_event_notes_that_name_the_version_are_used_without_the_file(self):
        os.remove(self.path)
        builder = ChangelogNotes(fallback_notes="Release 2.0.0: fixed things")
        notes = builder.build(self.request())
        self.assertEqual(notes.body, "Release 2.0.0: fixed things")
        self.assertEqual(notes.source, "event")


if __name__ == "__main__":
    unittest.main()
