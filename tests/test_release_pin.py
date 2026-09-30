"""The pin tool is the only supported way to move a consumer's version.

Its contract is small enough to state in full: it reads a file, it finds the one
Continuum reference, it validates the target, and it changes that one line. Every
way it can be wrong is a way a consumer ends up running a revision nobody chose,
so the tests are about refusals as much as about the edit.

The refusals are the interesting half. A tool that accepts `main` and a tool that
rejects it look identical from a consumer's side when nothing goes wrong, and
nothing goes wrong until the day it does.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

from continuum import pin

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONSUMER_INGRESS = (
    REPO_ROOT / "fixtures" / "consumer-repo" / ".github" / "workflows" / "continuum.yml"
)
DELEGATION_INGRESS = (
    REPO_ROOT / "fixtures" / "delegation-parent" / ".github" / "workflows" / "continuum.yml"
)

V0_1_0 = "v0.1.0"
V0_2_0 = "v0.2.0"
A_COMMIT = "a" * 40
B_COMMIT = "b" * 40

MINIMAL = f"""\
name: Continuum

on:
  workflow_dispatch:

jobs:
  continuum:
    uses: {pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}@{V0_1_0}
    secrets: inherit
"""


def ingress(ref: str = V0_1_0, comment: str = "") -> str:
    tail = f"  # {comment}" if comment else ""
    return MINIMAL.replace(f"@{V0_1_0}", f"@{ref}{tail}")


class ClassifyTests(unittest.TestCase):
    """`classify` decides accept or refuse, so its accept set is the contract."""

    def test_an_exact_semver_tag_is_a_release(self):
        for ref in ("v0.1.0", "v1.0.0", "v10.20.30", "v0.0.0"):
            with self.subTest(ref=ref):
                self.assertEqual(pin.classify(ref), "release")

    def test_a_full_commit_is_a_commit(self):
        self.assertEqual(pin.classify(A_COMMIT), "commit")

    def test_a_branch_is_refused_by_name(self):
        # "Invalid ref" tells someone reading a workflow file nothing.
        for ref in ("main", "master"):
            with self.subTest(ref=ref):
                self.assertIn("branch moves", pin.classify(ref))

    def test_head_is_refused_as_unselected(self):
        self.assertIn("not a selected version", pin.classify("HEAD"))

    def test_a_moving_compatibility_tag_is_refused_and_explained(self):
        # `v1` and `v1.2` are the shapes people reach for. They name a range, so
        # the commit they resolve to changes without the reference changing.
        for ref in ("v1", "v0.1", "v12.34"):
            with self.subTest(ref=ref):
                self.assertIn("moving major/minor", pin.classify(ref))

    def test_a_prerelease_is_refused(self):
        # A prerelease is a moving target by another name, and Continuum does not
        # publish one as a production dependency contract.
        for ref in ("v0.2.0-rc.1", "v1.0.0+build.5"):
            with self.subTest(ref=ref):
                self.assertIn("not a plain exact release tag", pin.classify(ref))

    def test_an_abbreviated_commit_is_refused_and_the_full_one_named(self):
        verdict = pin.classify(A_COMMIT[:12])
        self.assertIn("abbreviated", verdict)
        self.assertIn("40", verdict)

    def test_an_unrecognised_ref_is_refused(self):
        for ref in ("", "latest", "stable", "release-1"):
            with self.subTest(ref=ref):
                self.assertIsNotNone(pin.classify(ref))
                self.assertNotIn(pin.classify(ref), ("release", "commit"))


class ReferencedPinsTests(unittest.TestCase):
    def test_exactly_one_reference_is_found(self):
        found = pin.referenced_pins(ingress())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].ref, V0_1_0)
        self.assertEqual(found[0].path, pin.RELEASE_ENTRYPOINT)
        self.assertEqual(found[0].kind, "release")
        self.assertEqual(found[0].version, V0_1_0)
        self.assertEqual(found[0].commit, "")

    def test_a_commit_pin_exposes_the_commit_and_no_version(self):
        found = pin.referenced_pins(ingress(A_COMMIT))
        self.assertEqual(found[0].kind, "commit")
        self.assertEqual(found[0].commit, A_COMMIT)
        self.assertEqual(found[0].version, "")

    def test_a_relative_reference_is_not_a_pin(self):
        # GitHub resolves `./.github/...` from the caller's own commit, so it
        # carries no revision of its own and is not a second selection.
        text = "jobs:\n  a:\n    uses: ./.github/workflows/consumer-child-worker.yml\n"
        self.assertEqual(pin.referenced_pins(text), [])

    def test_a_comment_showing_the_reference_is_not_a_pin(self):
        # The generated ingress documents the line an operator edits. Editing the
        # documentation along with the value would make the file explain a
        # version it is no longer pinned to.
        text = ingress(comment="only this")
        self.assertEqual(len(pin.referenced_pins(text)), 1)

    def test_a_reference_with_no_at_sign_is_reported_rather_than_skipped(self):
        # Not a legal reference at all, and silently ignoring it would let a
        # malformed ingress look like one with no pin.
        text = f"    uses: {pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}\n"
        found = pin.referenced_pins(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "invalid")


class RequireSingleReleasePinTests(unittest.TestCase):
    def test_the_shipped_fixtures_satisfy_the_contract(self):
        for path in (CONSUMER_INGRESS, DELEGATION_INGRESS):
            with self.subTest(path=path.parent.parent.parent.name):
                found = pin.require_single_release_pin(
                    path.read_text(encoding="utf-8"), str(path)
                )
                self.assertEqual(found.ref, V0_1_0)

    def test_no_reference_is_refused_with_the_working_example(self):
        with self.assertRaises(pin.PinError) as raised:
            pin.require_single_release_pin("jobs:\n  a:\n    run: echo hi\n")
        message = str(raised.exception)
        self.assertIn(pin.RELEASE_ENTRYPOINT, message)
        self.assertIn("@v0.1.0", message)

    def test_two_references_are_refused_and_both_are_named(self):
        # Two references that agree today are still two places that decide which
        # Continuum a repository runs.
        text = ingress().replace(
            "    secrets: inherit\n",
            f"    secrets: inherit\n  other:\n    uses: {pin.CONTINUUM_REPOSITORY}"
            f"/.github/workflows/consumer-scheduler.yml@{V0_1_0}\n",
        )
        with self.assertRaises(pin.PinError) as raised:
            pin.require_single_release_pin(text)
        self.assertIn("2 Continuum references", str(raised.exception))
        self.assertIn("consumer-scheduler.yml", str(raised.exception))

    def test_a_per_module_reference_is_refused_by_name(self):
        text = ingress().replace(pin.RELEASE_ENTRYPOINT, ".github/workflows/review-queue.yml")
        with self.assertRaises(pin.PinError) as raised:
            pin.require_single_release_pin(text)
        self.assertIn("per-module pin", str(raised.exception))
        self.assertIn("review-queue.yml", str(raised.exception))

    def test_a_floating_reference_is_refused(self):
        with self.assertRaises(pin.PinError) as raised:
            pin.require_single_release_pin(ingress("main"))
        self.assertIn("not an exact release", str(raised.exception))


class SetRefTests(unittest.TestCase):
    def test_the_edit_is_one_line_and_everything_else_is_byte_identical(self):
        before = MINIMAL.splitlines(keepends=True)
        after = pin.upgrade(MINIMAL, V0_2_0).splitlines(keepends=True)
        self.assertEqual(len(before), len(after))
        differing = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
        self.assertEqual(len(differing), 1)
        self.assertIn(f"@{V0_2_0}", after[differing[0]])

    def test_the_round_trip_returns_the_original_text_exactly(self):
        # This is what makes a rollback the inverse of an upgrade, and it holds
        # only if the edit is line-precise and formatting-preserving.
        upgraded = pin.upgrade(MINIMAL, V0_2_0)
        self.assertEqual(pin.rollback(upgraded, V0_1_0), MINIMAL)

    def test_a_commit_and_a_tag_can_replace_each_other(self):
        by_tag = pin.upgrade(MINIMAL, A_COMMIT)
        self.assertIn(f"@{A_COMMIT}", by_tag)
        self.assertEqual(pin.rollback(by_tag, V0_1_0), MINIMAL)

    def test_a_trailing_comment_survives_the_edit_with_its_own_spacing(self):
        # An operator who aligned a comment against the old value did not ask for
        # their alignment to be reflowed by an upgrade.
        text = ingress(comment="only this")
        self.assertIn(f"@{V0_1_0}  # only this", text)
        upgraded = pin.upgrade(text, V0_2_0)
        self.assertIn(f"@{V0_2_0}  # only this", upgraded)
        self.assertEqual(pin.rollback(upgraded, V0_1_0), text)

    def test_unusual_spacing_around_the_reference_is_preserved(self):
        text = MINIMAL.replace("    uses: ", "    uses:   ").replace(
            "@v0.1.0\n", "@v0.1.0   \n"
        )
        upgraded = pin.upgrade(text, V0_2_0)
        self.assertIn(f"uses:   {pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}@{V0_2_0}   \n", upgraded)
        self.assertEqual(pin.rollback(upgraded, V0_1_0), text)

    def test_crlf_line_endings_are_preserved(self):
        # An ingress committed from Windows must not come back with its whole file
        # rewritten by an upgrade.
        text = MINIMAL.replace("\n", "\r\n")
        upgraded = pin.upgrade(text, V0_2_0)
        self.assertIn(f"@{V0_2_0}\r\n", upgraded)
        self.assertEqual(upgraded.count("\r\n"), text.count("\r\n"))
        self.assertEqual(pin.rollback(upgraded, V0_1_0), text)

    def test_a_floating_target_is_refused_before_anything_is_rewritten(self):
        for target in ("main", "v1", "v0.2", A_COMMIT[:9], ""):
            with self.subTest(target=target):
                with self.assertRaises(pin.PinError):
                    pin.upgrade(MINIMAL, target)

    def test_upgrade_and_rollback_refuse_the_same_targets(self):
        # They are the same edit, so they must not disagree about what is allowed.
        for target in ("main", "v1", "v0.2.0-rc.1"):
            with self.subTest(target=target):
                self.assertRaises(pin.PinError, pin.upgrade, MINIMAL, target)
                self.assertRaises(pin.PinError, pin.rollback, MINIMAL, target)

    def test_the_documented_example_line_in_a_real_ingress_is_not_rewritten(self):
        # The generated ingress shows the operator's line as an example. Editing
        # it would leave the file explaining a version it is not pinned to.
        text = CONSUMER_INGRESS.read_text(encoding="utf-8")
        upgraded = pin.upgrade(text, V0_2_0)
        occurrences = [line for line in upgraded.splitlines() if "@v0.1.0" in line]
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(occurrences[0].lstrip().startswith("#"))
        self.assertIn(f"@{V0_2_0}", upgraded)
        self.assertEqual(pin.rollback(upgraded, V0_1_0), text)


class CliTests(unittest.TestCase):
    """The CLI is the operator-facing half, so its contract is the output shape."""

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
        return subprocess.run(
            [sys.executable, "-m", "continuum.cli", *arguments],
            capture_output=True,
            text=True,
            env=environment,
            cwd=REPO_ROOT,
        )

    def test_show_reports_the_pinned_release_as_json(self):
        result = self.run_cli("release", "pin", "show", "--ingress", str(CONSUMER_INGRESS))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["ref"], V0_1_0)
        self.assertEqual(payload["kind"], "release")
        self.assertEqual(payload["path"], pin.RELEASE_ENTRYPOINT)
        self.assertEqual(
            payload["reference"],
            f"{pin.CONTINUUM_REPOSITORY}/{pin.RELEASE_ENTRYPOINT}@{V0_1_0}",
        )

    def test_verify_accepts_a_conforming_ingress(self):
        result = self.run_cli("release", "pin", "verify", "--ingress", str(DELEGATION_INGRESS))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verify_refuses_a_floating_pin_without_writing_anything(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as directory:
            path = pathlib.Path(directory) / "continuum.yml"
            path.write_text(ingress("main"), encoding="utf-8")
            result = self.run_cli("release", "pin", "verify", "--ingress", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch moves", result.stderr + result.stdout)

    def test_upgrade_does_not_write_without_being_asked_to(self):
        # The default is a dry run. An upgrade that silently commits would move a
        # consumer's version without anyone having reviewed a diff.
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as directory:
            path = pathlib.Path(directory) / "continuum.yml"
            before = ingress()
            path.write_text(before, encoding="utf-8")
            result = self.run_cli(
                "release", "pin", "upgrade", "--ingress", str(path), "--ref", V0_2_0
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"@{V0_2_0}", result.stdout)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_upgrade_writes_exactly_one_line_when_asked(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as directory:
            path = pathlib.Path(directory) / "continuum.yml"
            path.write_text(MINIMAL, encoding="utf-8")
            result = self.run_cli(
                "release", "pin", "upgrade", "--ingress", str(path), "--ref", V0_2_0, "--write"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = path.read_text(encoding="utf-8")
        self.assertIn(f"@{V0_2_0}", written)
        self.assertEqual(len(written.splitlines()), len(MINIMAL.splitlines()))

    def test_rollback_is_the_inverse_of_upgrade(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as directory:
            path = pathlib.Path(directory) / "continuum.yml"
            path.write_text(MINIMAL, encoding="utf-8")
            self.run_cli(
                "release", "pin", "upgrade", "--ingress", str(path), "--ref", V0_2_0, "--write"
            )
            result = self.run_cli(
                "release", "pin", "rollback", "--ingress", str(path), "--ref", V0_1_0, "--write"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), MINIMAL)

    def test_a_refused_ref_leaves_the_file_untouched_even_with_write(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as directory:
            path = pathlib.Path(directory) / "continuum.yml"
            path.write_text(MINIMAL, encoding="utf-8")
            result = self.run_cli(
                "release", "pin", "upgrade", "--ingress", str(path), "--ref", "main", "--write"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), MINIMAL)


if __name__ == "__main__":
    unittest.main()
