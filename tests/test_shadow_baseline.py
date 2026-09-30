"""The rolling parity baseline, and the ledger it is read against.

The gate this module exists for is the one a historical snapshot cannot satisfy:
a window of recorded events says Continuum agreed with production on the paths it
saw, and says nothing about the consumer's workflows *now*. So these tests are
about the shape of the failure they prevent, not about the reading being
convenient:

* a blob that moved, a file nobody classified, and a pull request nobody classified
  all block, each naming the issue that owns it;
* a reading that could not be completed blocks, and does not pretend the files it
  missed were deleted;
* a ledger that cannot vouch for its own classifications is refused, because a
  ledger with a typo in it would report a clean drift for a file nobody audited.
"""

from __future__ import annotations

import unittest

import importlib.util
import pathlib
import re

from continuum.shadow import baseline

HEAD = "a" * 40


def _ledger_document(**overrides):
    document = {
        "schema": baseline.LEDGER_SCHEMA,
        "repository": "kodmial/nanodictate",
        "audited_head": HEAD,
        "workflows": [
            {
                "path": ".github/workflows/ci.yml",
                "blob_sha": "1" * 40,
                "classification": "consumer-local",
                "owner": "#27",
                "rationale": "product CI",
            },
            {
                "path": ".github/workflows/auto-merge.yml",
                "blob_sha": "2" * 40,
                "classification": "absorbed",
                "owner": "#11",
            },
        ],
    }
    document.update(overrides)
    return document


def _live(**overrides):
    head = {
        "repository": "kodmial/nanodictate",
        "default_branch": "main",
        "head_sha": "b" * 40,
        "workflows": [
            {"path": ".github/workflows/ci.yml", "blob_sha": "1" * 40},
            {"path": ".github/workflows/auto-merge.yml", "blob_sha": "2" * 40},
        ],
        "open_pull_requests": [],
    }
    head.update(overrides)
    return baseline.read_live_head({"schema": baseline.BASELINE_SCHEMA, **head})


def _audit(ledger=None, live=None, **kwargs):
    return baseline.audit(
        ledger or baseline.read_ledger(_ledger_document()),
        live if live is not None else _live(),
        **kwargs
    )


class TheCleanCase(unittest.TestCase):
    def test_a_matching_reading_is_clean(self):
        report = _audit()
        self.assertTrue(report.ready)
        self.assertEqual(report.verdict, baseline.CLEAN)
        self.assertEqual(report.blockers, ())
        self.assertIn("clean at", baseline.summarize(report))

    def test_the_digest_covers_both_sides(self):
        report = _audit()
        self.assertTrue(report.evidence_digest.startswith("bl-"))
        # A different ledger about the same repository is a different claim.
        other = baseline.read_ledger(
            _ledger_document(
                workflows=[
                    {
                        "path": ".github/workflows/ci.yml",
                        "blob_sha": "1" * 40,
                        "classification": "consumer-local",
                        "owner": "#27",
                    }
                ]
            )
        )
        self.assertNotEqual(
            report.evidence_digest, _audit(ledger=other).evidence_digest
        )

    def test_the_ledger_and_the_reading_are_each_identifiable(self):
        report = _audit()
        self.assertTrue(report.ledger_digest.startswith("pl-"))
        self.assertTrue(report.live_digest.startswith("lh-"))
        # The same reading read twice is the same reading.
        self.assertEqual(report.live_digest, _audit().live_digest)


class WorkflowDrift(unittest.TestCase):
    def test_a_moved_blob_is_drift_and_routes_to_its_owner(self):
        live = _live(
            workflows=[
                {"path": ".github/workflows/ci.yml", "blob_sha": "1" * 40},
                {"path": ".github/workflows/auto-merge.yml", "blob_sha": "9" * 40},
            ]
        )
        report = _audit(live=live)
        self.assertEqual(report.verdict, baseline.DRIFTED)
        self.assertIn("workflow_blob_changed", report.codes)
        # Not the audit issue: the ledger already says whose semantic this is.
        self.assertEqual(report.routing["#11"], (".github/workflows/auto-merge.yml",))
        self.assertNotIn("#60", report.routing)
        self.assertIn("9" * 40, report.blockers[0].message)

    def test_a_workflow_the_ledger_never_audited_routes_to_the_discovery_issue(self):
        live = _live(
            workflows=[
                {"path": ".github/workflows/ci.yml", "blob_sha": "1" * 40},
                {"path": ".github/workflows/auto-merge.yml", "blob_sha": "2" * 40},
                {"path": ".github/workflows/surprise.yml", "blob_sha": "3" * 40},
            ]
        )
        report = _audit(live=live)
        self.assertIn("unclassified_workflow", report.codes)
        self.assertEqual(report.routing["#60"], (".github/workflows/surprise.yml",))

    def test_a_workflow_that_left_is_drift_not_silence(self):
        live = _live(
            workflows=[{"path": ".github/workflows/ci.yml", "blob_sha": "1" * 40}]
        )
        report = _audit(live=live)
        self.assertIn("workflow_removed", report.codes)
        self.assertEqual(
            [item.kind for item in report.differences], [baseline.REMOVED]
        )

    def test_a_discovery_issue_that_names_nothing_routable_is_refused(self):
        document = _ledger_document(discovery_issue="#999")
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(document)
        self.assertEqual(caught.exception.code, "unroutable_discovery_issue")


class OpenPullRequests(unittest.TestCase):
    def test_a_classified_pull_request_at_the_same_paths_is_unchanged(self):
        ledger = baseline.read_ledger(
            _ledger_document(
                open_pull_requests=[
                    {
                        "number": 67,
                        "paths": [".github/workflows/rust.yml"],
                        "classification": "consumer-local",
                        "owner": "#27",
                    }
                ]
            )
        )
        live = _live(
            open_pull_requests=[
                {
                    "number": 67,
                    "head_sha": "c" * 40,
                    "paths": [".github/workflows/rust.yml"],
                }
            ]
        )
        self.assertTrue(_audit(ledger=ledger, live=live).ready)

    def test_an_unclassified_pull_request_blocking_restore_is_refused(self):
        live = _live(
            open_pull_requests=[
                {
                    "number": 91,
                    "head_sha": "c" * 40,
                    "paths": [".github/workflows/issue-scheduler.yml"],
                }
            ]
        )
        report = _audit(live=live)
        self.assertIn("unclassified_open_pull_request", report.codes)
        self.assertEqual(report.routing["#60"], ("#91",))

    def test_a_classified_pull_request_that_grew_a_path_is_drift(self):
        ledger = baseline.read_ledger(
            _ledger_document(
                open_pull_requests=[
                    {
                        "number": 67,
                        "paths": [".github/workflows/ci.yml"],
                        "classification": "consumer-local",
                        "owner": "#27",
                    }
                ]
            )
        )
        live = _live(
            open_pull_requests=[
                {
                    "number": 67,
                    "head_sha": "c" * 40,
                    "paths": [".github/workflows/ci.yml", ".github/workflows/auto-merge.yml"],
                }
            ]
        )
        self.assertIn("open_pull_request_drift", _audit(ledger=ledger, live=live).codes)


class IncompleteReadings(unittest.TestCase):
    def test_an_incomplete_reading_is_incomplete_and_claims_no_drift(self):
        # The dangerous version of this: a partial inventory compared with a
        # complete ledger reports every unread file as deleted.
        live = _live(
            head_sha="",
            workflows=[],
            complete=False,
            limits=["workflow inventory could not be read: 403"],
        )
        report = _audit(live=live)
        self.assertEqual(report.verdict, baseline.INCOMPLETE)
        self.assertEqual(report.differences, ())
        self.assertEqual(report.codes, ("live_head_incomplete",))
        self.assertIn("incomplete", baseline.summarize(report))

    def test_every_limit_is_named(self):
        live = _live(
            complete=False,
            limits=["a failed", "b failed"],
        )
        self.assertIn("a failed", _audit(live=live).limits)

    def test_a_reading_of_a_different_repository_is_refused(self):
        live = _live(repository="someone/else")
        self.assertIn("repository_mismatch", _audit(live=live).codes)


class TheLedgerItself(unittest.TestCase):
    def test_an_unknown_classification_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(
                _ledger_document(
                    workflows=[
                        {
                            "path": ".github/workflows/ci.yml",
                            "blob_sha": "1" * 40,
                            "classification": "probably-fine",
                        }
                    ]
                )
            )
        self.assertEqual(caught.exception.code, "unknown_classification")

    def test_a_workflow_with_no_blob_is_refused(self):
        # Nothing to compare against, so accepting it would produce a report about
        # a file that was never audited.
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(
                _ledger_document(
                    workflows=[
                        {
                            "path": ".github/workflows/ci.yml",
                            "blob_sha": "",
                            "classification": "consumer-local",
                        }
                    ]
                )
            )
        self.assertEqual(caught.exception.code, "ledger_workflow_incomplete")

    def test_an_owner_nobody_reads_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(
                _ledger_document(
                    workflows=[
                        {
                            "path": ".github/workflows/ci.yml",
                            "blob_sha": "1" * 40,
                            "classification": "consumer-local",
                            "owner": "#4001",
                        }
                    ]
                )
            )
        self.assertEqual(caught.exception.code, "unroutable_owner")

    def test_a_ledger_from_another_schema_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger({"schema": "something-else/v1"})
        self.assertEqual(caught.exception.code, "unknown_ledger_schema")

    def test_the_repository_must_be_named(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(_ledger_document(repository="  "))
        self.assertEqual(caught.exception.code, "ledger_repository_missing")

    def test_every_entry_carries_a_rationale_or_says_it_needs_none(self):
        # Rationale is not enforced -- some classifications are self-explanatory --
        # but a ledger row with neither is a claim with no support, so the
        # rendering has to make the absence visible rather than leave a blank cell.
        markdown = baseline.render_markdown(baseline.read_ledger(_ledger_document()))
        self.assertIn("| `.github/workflows/auto-merge.yml`", markdown)
        self.assertIn("| - |", markdown)
        self.assertIn("product CI", markdown)


class TheLedgerClaimsWhatItCanBeCheckedAgainst(unittest.TestCase):
    """An `absorbed` entry is a claim that Continuum already has the behaviour.

    Nothing enforces that except this file. A rationale that says "absorbed by X"
    where X does not exist reads exactly like one where it does, and the ledger is
    the artifact a reviewer trusts instead of re-reading the consumer. So each
    absorbed entry has to name something that resolves -- a path in this repository,
    or a module that imports -- and a `must-port` entry may not claim absorption at
    all, because that is the state the reader is supposed to act on.
    """

    LEDGER = "docs/parity-ledger.json"

    #: A path or module named in a rationale. Backticks are documentation, not part
    #: of the claim, so the reference is read without them.
    _PATH_RE = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|yml|json|md)")
    _MODULE_RE = re.compile(r"\b(continuum(?:\.[a-z_]+)+)\b")

    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger = baseline.load_ledger(cls.LEDGER)

    def _references(self, rationale: str) -> list:
        found = []
        for match in self._MODULE_RE.finditer(rationale):
            parts = match.group(1).split(".")
            # `_visible_conclusion` is a private attribute, not a module; two
            # components is as deep as a resolvable module reference goes here.
            found.append(".".join(parts[:2]))
        for match in self._PATH_RE.finditer(rationale):
            candidate = match.group(0)
            if candidate.startswith("continuum/") or "/" in candidate:
                found.append(candidate)
        return sorted(set(found))

    def _resolves(self, reference: str) -> bool:
        if reference.startswith("continuum.") and not reference.endswith(".py"):
            try:
                return importlib.util.find_spec(reference) is not None
            except (ImportError, ValueError):
                return False
        for candidate in (
            reference,
            "src/" + reference,
            reference.replace("docs/", "src/continuum/", 1),
            "tests/" + reference,
        ):
            if (pathlib.Path(candidate)).exists():
                return True
        return False

    def test_every_absorbed_entry_names_something_that_resolves(self):
        for entry in self.ledger.workflows:
            if entry.classification != "absorbed":
                continue
            references = self._references(entry.rationale)
            with self.subTest(workflow=entry.path):
                self.assertTrue(
                    references,
                    "{} claims something without naming it".format(entry.path),
                )
                for reference in references:
                    self.assertTrue(
                        self._resolves(reference),
                        "{} names {}, which is not in this repository".format(
                            entry.path, reference
                        ),
                    )

    def test_an_absorbed_entry_names_the_thing_that_absorbs_it(self):
        for entry in self.ledger.workflows:
            if entry.classification != "absorbed":
                continue
            with self.subTest(workflow=entry.path):
                self.assertTrue(
                    self._references(entry.rationale),
                    "an absorbed entry has to say where the behaviour now lives",
                )

    def test_a_must_port_entry_does_not_claim_to_be_absorbed(self):
        # Otherwise the ledger reads as complete while telling the reader the
        # opposite, and the reader has no way to tell which is true.
        for entry in self.ledger.workflows:
            if entry.classification != "must-port":
                continue
            with self.subTest(workflow=entry.path):
                lowered = entry.rationale.lower()
                self.assertNotIn("absorbed by", lowered)
                self.assertNotIn("continuum.", lowered)

    def test_a_must_port_entry_names_its_deferred_owner(self):
        for entry in self.ledger.workflows:
            if entry.classification != "must-port":
                continue
            with self.subTest(workflow=entry.path):
                self.assertRegex(entry.owner, r"^#[0-9]+$")
                self.assertIn("Phase B", entry.rationale)
                self.assertNotIn("tests/", entry.rationale)

    def test_a_nothing_to_port_classification_explains_itself(self):
        for entry in self.ledger.workflows:
            if entry.classification not in ("not-applicable", "superseded", "consumer-local"):
                continue
            with self.subTest(workflow=entry.path):
                self.assertGreater(
                    len(entry.rationale), 40, "a rationale this short asserts nothing"
                )


class TheRenderedLedgerIsCurrent(unittest.TestCase):
    """The prose table is generated from the JSON, and this is what keeps it so.

    A rendered block nobody regenerates is a second copy of the ledger, and the two
    copies drift the moment a classification is corrected -- at which point a reader
    reads the stale one and the gate reads the correct one, and neither is wrong
    about itself.
    """

    LEDGER = "docs/parity-ledger.json"
    DOC = "docs/parity-ledger.md"
    BEGIN = "<!-- BEGIN GENERATED: {} -->".format(LEDGER)
    END = "<!-- END GENERATED: {} -->".format(LEDGER)

    def test_the_generated_block_is_present_and_bounded(self):
        text = pathlib.Path(self.DOC).read_text(encoding="utf-8")
        self.assertIn(self.BEGIN, text)
        self.assertIn(self.END, text)

    def test_the_generated_block_matches_the_json(self):
        text = pathlib.Path(self.DOC).read_text(encoding="utf-8")
        block = text.split(self.BEGIN, 1)[1].split(self.END, 1)[0]
        expected = baseline.render_markdown(
            baseline.load_ledger(self.LEDGER)
        )
        self.assertEqual(
            " ".join(block.split()), " ".join(expected.split()), 
            "docs/parity-ledger.md is stale; regenerate the block from docs/parity-ledger.json",
        )

    def test_every_classified_workflow_appears_in_the_document(self):
        # A renderer that silently dropped a table would still render, and the
        # document would be a shorter ledger rather than a wrong one, which is the
        # failure nobody notices.
        text = pathlib.Path(self.DOC).read_text(encoding="utf-8")
        for entry in baseline.load_ledger(self.LEDGER).workflows:
            with self.subTest(workflow=entry.path):
                self.assertIn(entry.path, text)


class TheShippedLedger(unittest.TestCase):
    """`docs/parity-ledger.json` is an artifact a gate reads, so it is tested."""

    LEDGER = "docs/parity-ledger.json"

    def test_it_loads_and_classifies_everything_it_lists(self):
        ledger = baseline.load_ledger(self.LEDGER)
        self.assertEqual(ledger.repository, "kodmial/nanodictate")
        self.assertTrue(ledger.workflows)
        self.assertEqual(ledger.audited_head, "d9e6dcc2e0c9c43bc87cd1963b75c097dec7ea1c")
        for entry in ledger.workflows:
            self.assertIn(entry.classification, baseline.CLASSIFICATIONS)
            self.assertTrue(entry.path.startswith(".github/workflows/"))
            self.assertEqual(len(entry.blob_sha), 40)
        for entry in ledger.open_pull_requests:
            self.assertIn(entry.classification, baseline.CLASSIFICATIONS)
            self.assertTrue(entry.paths)
            for path in entry.paths:
                self.assertTrue(path.startswith(".github/"))

    def test_it_covers_both_workflows_the_live_audit_found_beyond_the_issue_table(self):
        # These two were added after the 2026-09-30 audit point, so a ledger built
        # from that table alone would be missing files the gate would then report as
        # unclassified. It is in the ledger, and it is classified.
        paths = {entry.path for entry in baseline.load_ledger(self.LEDGER).workflows}
        self.assertIn(".github/workflows/continuum-migration-preflight.yml", paths)
        self.assertIn(".github/workflows/continuum-shadow-bridge.yml", paths)

    def test_no_workflow_is_claimed_twice(self):
        paths = [entry.path for entry in baseline.load_ledger(self.LEDGER).workflows]
        self.assertEqual(len(paths), len(set(paths)))


class AReportThatDisagreesWithItself(unittest.TestCase):
    """The gate trusts `ready` and `verdict`, and both are derivable.

    Every other field is derivable from the rest, so an edit to one field only is
    not a weaker report, it is a report whose remaining fields cannot be assumed to
    mean what they say -- which is also the shape a truncated write leaves behind.
    Rather than reconcile it, the reader refuses it and says which field lies.
    """

    def _document(self, **overrides):
        report = _audit().describe()
        report.update(overrides)
        return report

    def _read(self, **overrides):
        return baseline.read_report(self._document(**overrides))

    def test_a_real_report_survives_the_round_trip(self):
        # The check refuses what it cannot trust, so a genuine report has to pass.
        # Nothing here should be relaxed to make an inconsistency pass.
        for verdict in (baseline.CLEAN, baseline.DRIFTED):
            document = _audit(
                live=_live(workflows=[{"path": ".github/workflows/ci.yml", "blob_sha": "9" * 40}])
                if verdict == baseline.DRIFTED
                else None
            ).describe()
            self.assertEqual(
                baseline.read_report(document).verdict,
                document["verdict"],
            )

    def test_ready_with_a_blocker_is_refused(self):
        # The dangerous one. `ready` is what the gate reads to decide there is
        # nothing to fix, and a blocker sitting in the same document is the gate
        # being told the finding was dropped.
        drifted = _audit(
            live=_live(workflows=[{"path": ".github/workflows/ci.yml", "blob_sha": "9" * 40}])
        ).describe()
        drifted["ready"] = True
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(drifted)
        self.assertEqual(caught.exception.code, "inconsistent_report")
        self.assertIn("ready", str(caught.exception))

    def test_not_ready_without_a_blocker_is_refused(self):
        # The refusal has to be resolvable, or the gate would report a difference
        # nobody can act on forever.
        document = self._document(ready=False, blockers=[])
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertIn("no blocker", str(caught.exception))

    def test_clean_with_a_blocker_is_refused(self):
        document = self._document(
            ready=False,
            verdict=baseline.CLEAN,
            blockers=[{"code": "x", "message": "y", "subject": "z", "owner": "#60"}],
        )
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertIn("clean", str(caught.exception))

    def test_drifted_without_a_blocker_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(self._document(verdict=baseline.DRIFTED, blockers=[]))
        self.assertIn("drifted", str(caught.exception))

    def test_incomplete_that_names_blockers_is_refused(self):
        # A partial reading has no finding. Naming one means the difference walk
        # ran against files nobody read, which is the claim this gate exists to
        # refuse -- so it must not arrive as a report that looks usable.
        document = self._document(
            verdict=baseline.INCOMPLETE,
            ready=False,
            complete=False,
            limits=["a read failed"],
            blockers=[{"code": "x", "message": "y", "subject": "z", "owner": "#60"}],
        )
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertIn("partial reading", str(caught.exception))

    def test_complete_with_a_named_limit_is_refused(self):
        # The flag and the reason it exists disagreeing is the definition of a
        # corrupted capture.
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(
                self._document(complete=True, limits=["workflow inventory could not be read"])
            )
        self.assertIn("unread read", str(caught.exception))

    def test_incomplete_verdict_must_be_incomplete(self):
        # The verdict says a partial reading, the flag says the reading finished.
        document = self._document(
            ready=False,
            verdict=baseline.DRIFTED,
            blockers=[{"code": "x", "message": "y", "subject": "z", "owner": "#60"}],
            complete=False,
        )
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertIn("not complete", str(caught.exception))

    def test_a_report_with_no_evidence_digest_is_refused(self):
        # An approval is granted over this digest, so an empty one is not a weaker
        # match, it is no match -- and a report with no match is not evidence.
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(self._document(evidence_digest=""))
        self.assertIn("no evidence digest", str(caught.exception))

    def test_the_gate_refuses_rather_than_reconciling(self):
        # The cutover gate forwards a report's blockers and folds its digest into the
        # approval digest. If a contradictory report were reconciled on the way in,
        # the digest would describe a document nobody read, and the approval would be
        # bound to it anyway.
        drifted = _audit(
            live=_live(workflows=[{"path": ".github/workflows/ci.yml", "blob_sha": "9" * 40}])
        ).describe()
        drifted["ready"] = True
        with self.assertRaises(baseline.BaselineError):
            baseline.read_report(drifted)


class MorePullRequestsThanTheCaptureCanHold(unittest.TestCase):
    """A truncated capture must not read as a clean one.

    ``list_pulls`` is fully paginated, so the only way to stop reading is the
    capture's own ceiling. Dropping the remainder silently would report a busy
    repository with one unclassified pull request as clean, which is the single
    thing this gate must never claim.
    """

    class _Client:
        def __init__(self, count):
            self.count = count
            self.repository = "kodmial/nanodictate"

        def default_branch(self):
            return "main"

        def ref_sha(self, ref):
            return "b" * 40

        def workflow_inventory(self, ref):
            return {".github/workflows/ci.yml": "1" * 40, ".github/workflows/auto-merge.yml": "2" * 40}

        def list_pulls(self, *, state="open", base=""):
            return [
                {"number": index, "head": {"sha": str(index) * 40}}
                for index in range(1, self.count + 1)
            ]

        def list_pull_files(self, number):
            return [{"filename": ".github/workflows/ci.yml"}]

    def test_a_truncated_capture_is_incomplete(self):
        live = baseline.capture_live_head(self._Client(5), max_pull_requests=2)
        self.assertFalse(live.complete)
        self.assertIn("more than 2 open", "; ".join(live.limits))

    def test_a_truncated_capture_blocks_the_audit(self):
        live = baseline.capture_live_head(self._Client(5), max_pull_requests=2)
        report = _audit(live=live)
        self.assertEqual(report.verdict, baseline.INCOMPLETE)
        self.assertEqual(report.differences, ())

    def test_a_capture_that_fits_is_complete(self):
        # The ceiling is a guard, not a policy: a repository with few enough pull
        # requests must still read as complete.
        live = baseline.capture_live_head(self._Client(2), max_pull_requests=2)
        self.assertTrue(live.complete)
        self.assertEqual(live.limits, ())
        self.assertEqual(len(live.open_pull_requests), 2)


if __name__ == "__main__":
    unittest.main()
