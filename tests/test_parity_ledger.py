"""The parity ledger is a claim about work, so it is checked like one.

`docs/parity-ledger.json` exists to answer a question a reviewer will ask
anyway: for every semantic the source repository gained, what did Continuum do
about it, and where is the proof. A ledger that can quietly disagree with its own
evidence is worse than no ledger, because it converts an unfinished audit into a
finished-looking one.

So this suite enforces the parts that rot:

* the summary counts the items actually present;
* every `must-port` names files and regression tests, and those paths exist and
  those test names are real test methods -- a ledger entry pointing at a deleted
  test is an unbacked claim;
* every `absorbed` names evidence, and every referenced file exists;
* every `source_commit` is inside the audited range, so no entry can quietly
  cite a commit the audit never looked at;
* every classification carries a rationale, because "not applicable" without a
  reason is indistinguishable from "not looked at";
* nothing in the ledger enables a cutover.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pathlib
import unittest
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "docs" / "parity-ledger.json"
SOURCE_REPO = "kodmial/nanodictate/"

with LEDGER_PATH.open(encoding="utf-8") as _handle:
    LEDGER = json.load(_handle)
ITEMS = LEDGER["items"]


def test_ids(item) -> list:
    """Every test node id the item cites, whether from evidence or from landed."""

    return list(item.get("evidence", {}).get("tests", [])) + list(
        item.get("landed", {}).get("tests", [])
    )


def test_paths(item) -> list:
    return list(item.get("evidence", {}).get("files", [])) + list(
        item.get("landed", {}).get("files", [])
    )


def resolve_test(node: str):
    """Return the test method a `path::Class::method` node id names, or None."""

    path, _, qualname = node.partition("::")
    if not qualname:
        return None
    module_name = path.replace("/", ".").removesuffix(".py")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    found = module
    for part in qualname.split("::"):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found


def is_a_test(node: str) -> bool:
    if "::" not in node:
        return False
    return inspect.isfunction(resolve_test(node)) and node.split("::")[-1].startswith("test_")


class ShapeTests(unittest.TestCase):
    def test_the_ledger_declares_its_schema_and_version(self):
        self.assertEqual(LEDGER["schema"], "continuum.parity-ledger/v1")
        self.assertIsInstance(LEDGER["version"], int)

    def test_the_audited_range_is_stated_completely(self):
        source = LEDGER["source"]
        self.assertEqual(len(source["files"]), 6)
        self.assertEqual(len(source["commit_list"]), source["commits"])
        for name in source["files"]:
            self.assertIn("/workflows/", name, name)

    def test_item_ids_are_unique(self):
        ids = [item["id"] for item in ITEMS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_item_names_a_surface_from_the_audited_files(self):
        for item in ITEMS:
            self.assertIn(
                item["surface"],
                LEDGER["source"]["files"],
                f"{item['id']} cites a file the audit did not read: {item['surface']}",
            )

    def test_every_classification_is_one_of_the_five(self):
        for item in ITEMS:
            self.assertIn(
                item["classification"],
                LEDGER["classifications"],
                f"{item['id']} uses an undeclared classification",
            )

    def test_every_classification_has_a_policy_statement(self):
        for name in LEDGER["classifications"]:
            self.assertIn(name, LEDGER["policy"])

    def test_every_item_carries_a_rationale(self):
        # "Not applicable" and "not looked at" look identical in a table. The
        # rationale is what makes the first one a decision.
        for item in ITEMS:
            self.assertGreater(
                len(item.get("rationale", "")),
                80,
                f"{item['id']} has no substantive rationale",
            )

    def test_every_item_cites_at_least_one_source_commit_in_the_audited_range(self):
        audited = set(LEDGER["source"]["commit_list"])
        for item in ITEMS:
            commits = item.get("source_commits") or []
            self.assertTrue(commits, f"{item['id']} cites no source commit")
            for commit in commits:
                self.assertIn(commit, audited, f"{item['id']} cites unaudited {commit}")


class SummaryTests(unittest.TestCase):
    def test_the_declared_total_is_the_number_of_items(self):
        self.assertEqual(LEDGER["summary"]["total"], len(ITEMS))

    def test_the_declared_counts_match_the_items(self):
        counted = Counter(item["classification"] for item in ITEMS)
        for name in LEDGER["classifications"]:
            self.assertEqual(
                LEDGER["summary"]["by_classification"][name],
                counted.get(name, 0),
                f"{name} count disagrees with the items",
            )

    def test_the_counts_cover_every_item_exactly_once(self):
        self.assertEqual(sum(LEDGER["summary"]["by_classification"].values()), len(ITEMS))

    def test_nothing_outstanding_is_reported_as_landed(self):
        outstanding = [
            item["id"]
            for item in ITEMS
            if item["classification"] == "must-port" and not item.get("landed")
        ]
        self.assertEqual(LEDGER["summary"]["must_port_outstanding"], len(outstanding))

    def test_every_must_port_is_landed_with_coverage(self):
        landed = [
            item
            for item in ITEMS
            if item["classification"] == "must-port" and item.get("landed")
        ]
        self.assertEqual(LEDGER["summary"]["must_port_landed"], len(landed))


class EvidenceTests(unittest.TestCase):
    def test_every_must_port_names_files_and_regression_tests(self):
        for item in ITEMS:
            if item["classification"] != "must-port":
                continue
            landed = item.get("landed") or {}
            self.assertTrue(landed.get("files"), f"{item['id']} landed with no files")
            self.assertTrue(
                landed.get("tests"), f"{item['id']} landed with no regression test"
            )

    def test_every_must_port_names_the_incident_it_came_from(self):
        for item in ITEMS:
            if item["classification"] == "must-port":
                self.assertTrue(
                    item.get("incident"),
                    f"{item['id']} is a must-port with no originating incident",
                )

    def test_every_absorbed_item_cites_evidence(self):
        for item in ITEMS:
            if item["classification"] == "absorbed":
                self.assertTrue(
                    test_paths(item) or test_ids(item),
                    f"{item['id']} claims to be absorbed but cites nothing",
                )

    def test_every_referenced_path_exists(self):
        for item in ITEMS:
            for path in test_paths(item):
                if path.startswith(SOURCE_REPO) or ":" in path:
                    continue
                self.assertTrue(
                    (ROOT / path).exists(), f"{item['id']} cites a missing file: {path}"
                )

    def test_every_referenced_test_exists(self):
        # The point of the ledger is that a reader can check a claim. A node id
        # that resolves to nothing is a claim with nothing behind it.
        for item in ITEMS:
            for node in test_ids(item):
                self.assertTrue(
                    is_a_test(node),
                    f"{item['id']} cites a test that does not exist: {node}",
                )

    def test_every_must_port_lands_at_least_one_new_regression(self):
        for item in ITEMS:
            if item["classification"] != "must-port":
                continue
            self.assertTrue(
                any("::" in node for node in item["landed"]["tests"]),
                f"{item['id']} names a test module but not a test",
            )

    def test_consumer_local_items_evidence_lives_in_the_source_repository(self):
        # Nothing may be waved off as "the consumer's problem" while citing a
        # file in this repository, which would mean it is actually ours.
        for item in ITEMS:
            if item["classification"] != "consumer-local":
                continue
            for path in test_paths(item):
                self.assertFalse(
                    (ROOT / path).exists(),
                    f"{item['id']} is consumer-local but points at our own {path}",
                )


class CutoverTests(unittest.TestCase):
    def test_the_ledger_enables_nothing(self):
        # The parent issue forbids flipping either toggle as part of the audit.
        # A ledger that quietly declared a cutover would do it without anyone
        # asking.
        for name in ("review", "release"):
            self.assertIs(
                LEDGER["cutover"][name],
                False,
                f"the ledger must not enable the {name} cutover",
            )

    def test_the_cutover_says_why(self):
        self.assertIn("must-port", LEDGER["cutover"]["note"])


class RenderedDocumentTests(unittest.TestCase):
    """The Markdown is generated, so it is checked against the generator.

    Two hand-maintained copies of one ledger is one ledger with two truths, and
    the Markdown is the one a reviewer reads. Rather than trust it, the file is
    re-rendered and compared.
    """

    def test_the_committed_markdown_is_what_the_renderer_produces(self):
        from continuum.tools import parity_ledger

        self.assertEqual(
            parity_ledger.DOCUMENT_PATH.read_text(encoding="utf-8"),
            parity_ledger.render(LEDGER),
            "docs/parity-ledger.md is stale; run "
            "`PYTHONPATH=src python3 -m continuum.tools.parity_ledger`",
        )

    def test_every_item_reaches_the_rendered_document(self):
        from continuum.tools import parity_ledger

        document = parity_ledger.render(LEDGER)
        for item in ITEMS:
            self.assertIn(f"### {item['id']} —", document, f"{item['id']} is not rendered")

    def test_the_rendered_document_says_it_is_generated(self):
        # A reader who finds an error in the prose needs to know which file to
        # edit, and the file they should not edit is this one.
        self.assertIn("GENERATED FILE", LEDGER_PATH.with_suffix(".md").read_text(encoding="utf-8"))

    def test_no_item_is_rendered_under_a_classification_it_does_not_have(self):
        from continuum.tools import parity_ledger

        document = parity_ledger.render(LEDGER)
        for name, items in parity_ledger._group(ITEMS).items():
            section = document.split(f"## {parity_ledger.CLASSIFICATION_LABEL[name]}", 1)[-1]
            for item in items:
                self.assertIn(item["id"], section, f"{item['id']} is under the wrong section")


if __name__ == "__main__":
    unittest.main()
