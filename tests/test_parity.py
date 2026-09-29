"""The parity ledger has to be checkable, or the claim is decoration.

Issue #59 asks for production parity with the repositories Continuum was
extracted from. These tests are the enforcement: the shipped ledger has to
validate, and every way a ledger could quietly stop meaning anything - a
disposition invented on the spot, a reference that can move under the audit, an
`absorbed` claim pointing at a file that does not exist - has to fail.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from continuum import cli, parity

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "parity" / "ledger.v1.json"


def document(**overrides):
    base = {
        "version": parity.LEDGER_VERSION,
        "audited_at": "a" * 40,
        "entries": [
            {
                "id": "AA-01",
                "capability": "Something real",
                "repository": "nanodictate",
                "disposition": "absorbed",
                "source_ref": "b" * 40,
                "module": "continuum.review.queue",
            }
        ],
    }
    base.update(overrides)
    return base


class ShippedLedgerTests(unittest.TestCase):
    """The ledger in the tree is the one this issue is about."""

    def test_the_shipped_ledger_validates(self):
        ledger = parity.load(LEDGER_PATH)
        self.assertGreater(len(ledger.entries), 0)
        self.assertEqual(parity.check(ledger), [])

    def test_every_audited_repository_is_covered_or_explicitly_excluded(self):
        ledger = parity.load(LEDGER_PATH)
        covered = {entry.repository for entry in ledger.entries}
        # kodmaiadmin carries no entries on purpose: it is pinned to an old sha
        # and is therefore not parity evidence, which the loader allows and this
        # test pins down.
        self.assertEqual(covered, set(parity.REPOS) - {"kodmaiadmin"})

    def test_no_ledger_entry_leaves_a_gap_unimplemented(self):
        # `must-port` means a real gap that Continuum owes. If any entry claimed
        # one, P0 parity would not be complete, and the count is asserted so the
        # failure is visible in a diff rather than argued in a review.
        ledger = parity.load(LEDGER_PATH)
        self.assertEqual(
            [entry.id for entry in ledger.by_disposition(*parity.REQUIRED_BEFORE_P0[:1])],
            [],
        )

    def test_every_source_reference_is_a_full_commit_sha(self):
        ledger = parity.load(LEDGER_PATH)
        for entry in ledger.entries:
            with self.subTest(entry=entry.id):
                self.assertRegex(entry.source_ref, parity.COMMIT_RE)

    def test_every_entry_names_where_its_evidence_lives(self):
        ledger = parity.load(LEDGER_PATH)
        for entry in ledger.entries:
            with self.subTest(entry=entry.id):
                self.assertTrue(
                    entry.module or entry.paths or entry.rationale,
                    "{} carries no evidence".format(entry.id),
                )


class ValidationTests(unittest.TestCase):
    """Every way the ledger could become meaningless has to fail."""

    def _problems(self, document_):
        with self.assertRaises(parity.LedgerError) as caught:
            parity.parse(document_)
        return str(caught.exception)

    def test_the_shipped_document_is_accepted(self):
        self.assertEqual(len(parity.parse(json.loads(LEDGER_PATH.read_text())).entries), 26)

    def test_an_invented_disposition_is_refused(self):
        problems = self._problems(
            document(entries=[dict(document()["entries"][0], disposition="looks-fine")])
        )
        self.assertIn("looks-fine", problems)

    def test_a_branch_reference_is_refused(self):
        entry = dict(document()["entries"][0], source_ref="main")
        self.assertIn("full 40-character commit sha", self._problems(document(entries=[entry])))

    def test_a_tag_reference_is_refused(self):
        entry = dict(document()["entries"][0], source_ref="v0.1.0")
        self.assertIn("full 40-character commit sha", self._problems(document(entries=[entry])))

    def test_an_absorbed_claim_without_evidence_is_refused(self):
        entry = {
            "id": "AA-01",
            "capability": "Something real",
            "repository": "nanodictate",
            "disposition": "absorbed",
            "source_ref": "b" * 40,
        }
        self.assertIn("must name the 'module' or 'paths'", self._problems(document(entries=[entry])))

    def test_an_absorbed_claim_naming_a_module_that_does_not_exist_is_refused(self):
        entry = dict(document()["entries"][0], module="continuum.review.nonexistent")
        self.assertIn("does not exist", self._problems(document(entries=[entry])))

    def test_an_absorbed_claim_naming_a_path_that_does_not_exist_is_refused(self):
        entry = dict(document()["entries"][0], paths=[".github/workflows/not-here.yml"])
        self.assertIn("does not exist", self._problems(document(entries=[entry])))

    def test_a_consumer_local_entry_without_a_rationale_is_refused(self):
        entry = {
            "id": "AA-01",
            "capability": "Something real",
            "repository": "nanodictate",
            "disposition": "consumer-local",
            "source_ref": "b" * 40,
        }
        self.assertIn("requires a non-empty 'rationale'", self._problems(document(entries=[entry])))

    def test_a_duplicate_id_is_refused(self):
        entry = document()["entries"][0]
        self.assertIn("duplicate id", self._problems(document(entries=[entry, dict(entry)])))

    def test_an_unknown_repository_is_refused(self):
        entry = dict(document()["entries"][0], repository="somewhere-else")
        self.assertIn("somewhere-else", self._problems(document(entries=[entry])))

    def test_a_malformed_id_is_refused(self):
        entry = dict(document()["entries"][0], id="nope")
        self.assertIn("must look like", self._problems(document(entries=[entry])))

    def test_a_wrong_version_is_refused(self):
        self.assertIn("version", self._problems(document(version="ledger.v2")))

    def test_an_unaudited_repository_cannot_be_omitted(self):
        # Two repositories, one entry: silence is not a disposition.
        self.assertIn(
            "has no entries",
            self._problems(document(entries=[document()["entries"][0]])),
        )

    def test_an_empty_ledger_is_refused(self):
        self.assertIn("non-empty list", self._problems(document(entries=[])))

    def test_every_problem_is_reported_at_once(self):
        entries = [
            dict(document()["entries"][0], id="AA-01", source_ref="main"),
            dict(document()["entries"][0], id="AA-02", disposition="perhaps"),
        ]
        problems = self._problems(document(entries=entries))
        self.assertIn("full 40-character commit sha", problems)
        self.assertIn("perhaps", problems)

    def test_a_missing_file_is_a_failure_not_an_empty_pass(self):
        with self.assertRaises(parity.LedgerError):
            parity.load(REPO_ROOT / "parity" / "does-not-exist.json")


class CommandTests(unittest.TestCase):
    def test_the_command_passes_on_the_shipped_ledger(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["parity-check", "--ledger", str(LEDGER_PATH)])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Parity ledger", out.getvalue())

    def test_the_command_fails_on_a_broken_ledger(self):
        import contextlib
        import io
        import tempfile

        broken = dict(document(entries=[dict(document()["entries"][0], source_ref="main")]))
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "ledger.v1.json"
            path.write_text(json.dumps(broken))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["parity-check", "--ledger", str(path)])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("::error::", out.getvalue())


if __name__ == "__main__":
    unittest.main()
