"""The release-verify workflow, read as YAML.

A workflow is a program nobody imports, so the only way to know it still parses is
to parse it. `workflow_dispatch` inputs and the refusal codes are asserted too,
because the workflow's value is entirely in the mapping from a refusal to a repair
and both are invisible in the file if you only read it as text.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-verify.yml"


def _workflow() -> dict:
    with WORKFLOW.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    # PyYAML reads the `on:` key as the boolean True, which is YAML 1.1 being
    # pedantic about a word that is not a boolean. Normalise it so the test can say
    # what it means.
    if True in document:
        document["on"] = document.pop(True)
    return document


class TheWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _workflow()
        self.job = self.document["jobs"]["verify"]

    def test_it_parses_and_is_manually_dispatchable(self) -> None:
        # Not on push: this is a gate the publish step calls, not a reaction to a
        # commit landing. A consumer opts in by having a gate that writes a
        # candidate, not by having this workflow fire on every change.
        self.assertIn("workflow_dispatch", self.document["on"])
        self.assertNotIn("push", self.document["on"])
        self.assertNotIn("pull_request", self.document["on"])

    def test_it_reads_the_repository_and_nothing_else(self) -> None:
        # Verification reads bytes and compares digests. A workflow that could
        # write would be able to make the comparison true instead of proving it.
        self.assertEqual(self.document["permissions"], {"contents": "read"})

    def test_the_candidate_is_required_and_the_head_is_not(self) -> None:
        inputs = self.document["on"]["workflow_dispatch"]["inputs"]
        self.assertTrue(inputs["candidate"]["required"])
        self.assertTrue(inputs["artifact"]["required"])
        # Defaults to GITHUB_SHA, which every run already knows: a caller that has
        # to restate the commit is a caller that eventually restates the wrong one.
        self.assertFalse(inputs["source_sha"]["required"])

    def test_it_checks_out_the_commit_being_published(self) -> None:
        checkout = self.job["steps"][0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout"))
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.source_sha || github.sha }}")
        # A shallow checkout of a named tree is enough to read the artifacts; the
        # record is what proves the bytes, not the history.
        self.assertEqual(checkout["with"]["fetch-depth"], 1)

    def test_the_verification_is_the_step_exit_code(self) -> None:
        # A job that catches a refusal and exits zero is the failure mode this
        # whole mechanism exists to prevent, so the verdict has to reach the
        # caller as a non-zero exit and not as a branch on a boolean.
        step = self.job["steps"][-1]
        self.assertEqual(step["id"], "verify")
        self.assertIn("SystemExit(completed.returncode)", step["run"])
        self.assertIn("no-verification-document", step["run"])

    def test_the_outputs_name_the_refusal_so_a_publish_can_branch(self) -> None:
        self.assertEqual(
            sorted(self.job["outputs"]),
            ["candidate_digest", "code", "ok", "verified"],
        )
        step = self.job["steps"][-1]
        for key in self.job["outputs"]:
            self.assertIn(key, step["run"])

    def test_it_documents_which_failure_needs_which_repair(self) -> None:
        # The refusals are useless to a maintainer who cannot tell "the gate did
        # not run" from "the bytes are wrong": one is re-run the gate, the other
        # is stop re-building.
        text = WORKFLOW.read_text(encoding="utf-8")
        for code in ("no-tested-candidate", "gate-not-green", "artifact-"):
            self.assertIn(code, text)

    def test_it_names_no_product_language_or_package_manager(self) -> None:
        # The mechanism has to be adoptable by a consumer with none of these.
        text = WORKFLOW.read_text(encoding="utf-8").lower()
        for word in (
            "nanodictate",
            "swift",
            "brew",
            "macports",
            "rust",
            "cargo",
            "cocoa",
            "swiftlint",
            "xcode",
            "codesign",
            "notariz",
            "homebrew",
        ):
            self.assertNotIn(word, text)


class EveryWorkflow(unittest.TestCase):
    def test_no_workflow_in_this_repository_fails_to_parse(self) -> None:
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                with path.open(encoding="utf-8") as handle:
                    document = yaml.safe_load(handle)
                self.assertIn("jobs", document)
                self.assertTrue(document["jobs"])


if __name__ == "__main__":
    unittest.main()