"""The baseline plane itself: comparing an approved ledger with a live reading.

#60's requirement is a gate that notices the consumer repository has moved since
the audit was written. Most of this file is therefore about *failing to be
useful*: a reading that could not be taken in full, a ledger naming a
classification this engine does not recognise, an open pull request nobody looked
at. The property under test in each case is that the module would rather report
nothing than report something it cannot support.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from continuum.shadow import baseline

CI_BLOB = "b" * 40
AUDITED_HEAD = "a" * 40
PR_HEAD = "d" * 40


def _ledger(*, head: str = AUDITED_HEAD, repository: str = "example/consumer"):
    return baseline.ParityLedger(
        repository=repository,
        audited_head=head,
        audited_at="2026-03-01",
        discovery_issue="#60",
        workflows=(
            baseline.LedgerEntry(
                path=".github/workflows/ci.yml",
                blob_sha=CI_BLOB,
                classification="absorbed",
                owner="#11",
                rationale="the generic half is absorbed; the threshold is the consumer's",
            ),
        ),
    )


def _live(
    *,
    head: str = "e" * 40,
    repository: str = "example/consumer",
    workflows=(( ".github/workflows/ci.yml", CI_BLOB),),
    pulls=(),
    complete: bool = True,
    limits=(),
):
    return baseline.LiveHead(
        repository=repository,
        default_branch="main",
        head_sha=head,
        workflows=tuple(
            baseline.WorkflowFile(path=path, blob_sha=sha) for path, sha in workflows
        ),
        open_pull_requests=tuple(pulls),
        complete=complete,
        limits=tuple(limits),
    )


class Agreement(unittest.TestCase):
    def test_a_matching_reading_is_clean(self) -> None:
        report = baseline.audit(_ledger(), _live())
        self.assertTrue(report.ready)
        self.assertEqual(report.verdict, baseline.CLEAN)
        self.assertEqual(report.differences, ())
        self.assertEqual(report.blockers, ())
        self.assertIn("clean", baseline.summarize(report))

    def test_a_head_that_moved_without_a_workflow_change_is_clean(self) -> None:
        # The audited claim is about workflow blobs. A commit elsewhere is
        # recorded but is not a difference against the audit.
        report = baseline.audit(_ledger(), _live(head="f" * 40))
        self.assertTrue(report.ready)
        self.assertEqual(report.audited_head, AUDITED_HEAD)
        self.assertEqual(report.live_head, "f" * 40)

    def test_a_repository_with_no_workflows_is_not_drift(self) -> None:
        ledger = baseline.ParityLedger(repository="example/consumer", audited_head=AUDITED_HEAD)
        report = baseline.audit(ledger, _live(workflows=()))
        self.assertTrue(report.ready)


class DifferenceIsFound(unittest.TestCase):
    def test_a_moved_blob_is_a_difference(self) -> None:
        report = baseline.audit(
            _ledger(), _live(workflows=[(".github/workflows/ci.yml", "c" * 40)])
        )
        self.assertFalse(report.ready)
        self.assertEqual(report.verdict, baseline.DRIFTED)
        [difference] = report.differences
        self.assertEqual(difference.kind, baseline.CHANGED)
        self.assertEqual(difference.expected_blob, CI_BLOB)
        self.assertEqual(difference.observed_blob, "c" * 40)

    def test_a_new_workflow_is_unclassified_not_ignored(self) -> None:
        report = baseline.audit(
            _ledger(),
            _live(
                workflows=(
                    (".github/workflows/ci.yml", CI_BLOB),
                    (".github/workflows/surprise.yml", "d" * 40),
                )
            ),
        )
        self.assertIn("unclassified_workflow", report.codes)
        self.assertEqual(report.routing["#60"], (".github/workflows/surprise.yml",))
        [difference] = [item for item in report.differences if item.subject.endswith("surprise.yml")]
        self.assertEqual(difference.classification, "")

    def test_a_removed_workflow_is_a_difference_against_its_owner(self) -> None:
        report = baseline.audit(_ledger(), _live(workflows=()))
        self.assertIn("workflow_removed", report.codes)
        # Routed to the owner of the audited entry, not to the discovery issue:
        # whoever classified it is the one who knows where it went.
        self.assertEqual(report.routing["#11"], (".github/workflows/ci.yml",))

    def test_every_difference_is_reported_at_once(self) -> None:
        report = baseline.audit(
            _ledger(),
            _live(
                workflows=(
                    (".github/workflows/ci.yml", "c" * 40),
                    (".github/workflows/extra.yml", "d" * 40),
                )
            ),
        )
        self.assertEqual(len(report.differences), 2)
        self.assertEqual(
            set(report.codes), {"workflow_blob_changed", "unclassified_workflow"}
        )

    def test_a_ledger_for_another_repository_is_refused(self) -> None:
        report = baseline.audit(_ledger(), _live(repository="someone/else"))
        self.assertIn("repository_mismatch", report.codes)
        self.assertFalse(report.ready)


class OpenPullRequests(unittest.TestCase):
    """The set of open pull requests is part of what was audited.

    A pull request touching `.github/**` can restore a writer the cutover removes,
    so it is read and classified on the same footing as a workflow blob.
    """

    def _ledger_with_pr(self, *, head_sha: str = PR_HEAD):
        return baseline.ParityLedger(
            repository="example/consumer",
            audited_head=AUDITED_HEAD,
            workflows=_ledger().workflows,
            open_pull_requests=(
                baseline.LedgerPullRequest(
                    number=12,
                    head_sha=head_sha,
                    paths=(".github/workflows/ci.yml",),
                    classification="consumer-local",
                    owner="#60",
                    rationale="adds a product build step",
                ),
            ),
        )

    def _live_with_pr(self, **overrides):
        document = {
            "number": 12,
            "head_sha": PR_HEAD,
            "paths": [".github/workflows/ci.yml"],
        }
        document.update(overrides)
        return _live(pulls=(baseline.OpenPullRequest(**document),))

    def test_a_classified_pull_request_that_has_not_moved_is_clean(self) -> None:
        report = baseline.audit(self._ledger_with_pr(), self._live_with_pr())
        self.assertTrue(report.ready)

    def test_an_unclassified_pull_request_blocks(self) -> None:
        report = baseline.audit(
            _ledger(),
            _live(
                pulls=(
                    baseline.OpenPullRequest(
                        number=99,
                        head_sha="a" * 40,
                        paths=(".github/workflows/review-queue.yml",),
                    ),
                )
            ),
        )
        self.assertIn("unclassified_open_pull_request", report.codes)
        self.assertIn("#99", report.routing["#60"])

    def test_a_pull_request_that_changed_its_paths_blocks(self) -> None:
        report = baseline.audit(
            self._ledger_with_pr(),
            self._live_with_pr(
                paths=(".github/workflows/ci.yml", ".github/workflows/review-queue.yml")
            ),
        )
        self.assertIn("open_pull_request_drift", report.codes)

    def test_a_pull_request_that_rewrote_the_same_paths_blocks(self) -> None:
        # The case a path comparison cannot see. Every file the pull request
        # touches is the same file it touched when it was classified, so nothing
        # about the *set* of surfaces moved -- but the bytes did, and those bytes
        # could add the orchestration writer this gate exists to keep out.
        report = baseline.audit(
            self._ledger_with_pr(),
            self._live_with_pr(head_sha="c" * 40),
        )
        self.assertFalse(report.ready)
        self.assertIn("open_pull_request_head_moved", report.codes)
        # The head drift is its own code, distinct from a path change: it has a
        # different remedy, which is re-reading the pull request rather than
        # deciding what the new surface is.
        self.assertNotIn("open_pull_request_drift", report.codes)
        message = next(
            blocker.message for blocker in report.blockers
            if blocker.code == "open_pull_request_head_moved"
        )
        self.assertIn(PR_HEAD, message)
        self.assertIn("c" * 40, message)
        self.assertIn("#60", report.routing)

    def test_a_pull_request_that_moved_both_is_reported_as_drift(self) -> None:
        # When both change, the path set is the more informative fact: the
        # classification was granted against a different file list, so the head
        # movement is a detail of the same difference.
        report = baseline.audit(
            self._ledger_with_pr(),
            self._live_with_pr(
                head_sha="c" * 40,
                paths=(".github/workflows/ci.yml", ".github/workflows/rust.yml"),
            ),
        )
        self.assertIn("open_pull_request_drift", report.codes)
        self.assertNotIn("open_pull_request_head_moved", report.codes)

    def test_a_pull_request_that_disappeared_blocks(self) -> None:
        # A classified pull request that is no longer open is a change the audit
        # has not accounted for, and it blocks for the same reason a workflow
        # moving does.
        report = baseline.audit(self._ledger_with_pr(), _live())
        self.assertIn("open_pull_request_drift", report.codes)

    def test_a_classification_without_a_head_commit_is_refused(self) -> None:
        # Reading the ledger rather than auditing it: the stricter check, because
        # it is the point at which the omission becomes permanent. A ledger that
        # records a classification with no commit records that something was read
        # once, and a later rewrite of the same paths would still pass.
        document = self._ledger_with_pr().describe()
        document["open_pull_requests"][0].pop("head_sha")
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(document)
        self.assertEqual(
            caught.exception.code, "ledger_pull_request_head_sha_missing"
        )

    def test_the_head_commit_participates_in_the_ledger_digest(self) -> None:
        # Otherwise the same classification set under a different pull request
        # commit would produce one digest, and an approval would carry across a
        # rewrite that the audit itself refuses to accept.
        moved = self._ledger_with_pr(head_sha="c" * 40)
        self.assertNotEqual(self._ledger_with_pr().digest, moved.digest)


class IncompleteReadings(unittest.TestCase):
    """Absence is failure.

    A partial reading compared against a complete ledger would name every file it
    did not happen to see as removed, which is a claim about the gap rather than
    about the repository.
    """

    def test_an_incomplete_reading_reports_no_differences(self) -> None:
        report = baseline.audit(
            _ledger(), _live(complete=False, limits=["workflow inventory unreadable"])
        )
        self.assertEqual(report.verdict, "incomplete")
        self.assertEqual(report.differences, ())
        self.assertFalse(report.ready)
        self.assertEqual(report.codes, (baseline.INCOMPLETE_BLOCKER,))

    def test_an_incomplete_reading_names_why(self) -> None:
        report = baseline.audit(
            _ledger(),
            _live(complete=False, limits=["default branch could not be read: 403"]),
        )
        self.assertIn("403", report.blockers[0].message)

    def test_an_empty_repository_is_not_the_same_as_an_unreadable_one(self) -> None:
        # No workflows at all is a fact about the repository. An inventory that
        # could not be read is a gap in the instrument. Only the second blocks,
        # and the first is only clean because the ledger also has nothing in it.
        empty_ledger = baseline.ParityLedger(
            repository="example/consumer", audited_head=AUDITED_HEAD
        )
        self.assertTrue(baseline.audit(empty_ledger, _live(workflows=())).ready)
        self.assertFalse(baseline.audit(_ledger(), _live(workflows=())).ready)


class LedgerIntegrity(unittest.TestCase):
    """A ledger that cannot be trusted cannot be compared against.

    The gate's whole value is that it only ever compares a reviewed artifact with
    a live one, so a malformed ledger is refused rather than repaired.
    """

    def _read(self, **overrides):
        document = _ledger().describe()
        document.update(overrides)
        return baseline.read_ledger(document)

    def test_an_unknown_classification_is_refused(self) -> None:
        document = _ledger().describe()
        document["workflows"][0]["classification"] = "probably fine"
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(document)
        self.assertEqual(caught.exception.code, "unknown_classification")

    def test_a_workflow_with_no_blob_is_refused(self) -> None:
        document = _ledger().describe()
        document["workflows"][0]["blob_sha"] = ""
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(document)
        self.assertEqual(caught.exception.code, "ledger_workflow_incomplete")

    def test_an_owner_outside_the_routable_set_is_refused(self) -> None:
        document = _ledger().describe()
        document["workflows"][0]["owner"] = "#999"
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger(document)
        self.assertEqual(caught.exception.code, "unroutable_owner")

    def test_a_document_from_another_schema_is_refused(self) -> None:
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_ledger({"schema": "continuum.parity-ledger/v99"})
        self.assertEqual(caught.exception.code, "unknown_ledger_schema")

    def test_the_repository_committed_ledger_is_loadable_and_makes_the_claim_it_makes(self) -> None:
        # The ledger in docs/ is the artifact #60's gate reads. If it does not
        # load, every other test here is about a fiction.
        ledger = baseline.load_ledger("docs/parity-ledger.json")
        self.assertTrue(ledger.workflows)
        self.assertEqual(
            {entry.classification for entry in ledger.workflows} <= set(baseline.CLASSIFICATIONS),
            True,
        )
        for entry in ledger.workflows:
            self.assertTrue(entry.path.startswith(".github/workflows/"), entry.path)
            self.assertTrue(entry.rationale.strip(), entry.path)
            self.assertIn(entry.owner, baseline.ROUTABLE_ISSUES)

    def test_a_ledger_round_trips_through_disk_unchanged(self) -> None:
        # The next refresh is a command, not a hand edit, so the written form has
        # to read back identically.
        ledger = baseline.load_ledger("docs/parity-ledger.json")
        with tempfile.TemporaryDirectory(dir=".") as directory:
            path = Path(directory) / "parity-ledger.json"
            path.write_text(baseline.dump_ledger(ledger), encoding="utf-8")
            reloaded = baseline.load_ledger(str(path))
        self.assertEqual(reloaded.digest, ledger.digest)
        self.assertEqual(baseline.render_markdown(reloaded), baseline.render_markdown(ledger))


class Repinning(unittest.TestCase):
    """Moving the recorded SHAs forward is a human act, and only a narrow one."""

    def test_a_moved_blob_is_repinned_and_the_verdict_is_kept(self) -> None:
        moved = _live(workflows=[(".github/workflows/ci.yml", "c" * 40)])
        repinned = _ledger().at(moved, audited_at="2026-04-01")
        entry = repinned.workflow_map[".github/workflows/ci.yml"]
        self.assertEqual(entry.blob_sha, "c" * 40)
        self.assertEqual(entry.classification, "absorbed")
        self.assertEqual(entry.owner, "#11")
        self.assertEqual(entry.rationale, _ledger().workflows[0].rationale)
        self.assertEqual(repinned.audited_at, "2026-04-01")
        self.assertTrue(baseline.audit(repinned, moved).ready)

    def test_a_new_workflow_cannot_be_repinned_into_existence(self) -> None:
        with self.assertRaises(baseline.BaselineError) as caught:
            _ledger().at(
                _live(
                    workflows=(
                        (".github/workflows/ci.yml", CI_BLOB),
                        (".github/workflows/surprise.yml", "d" * 40),
                    )
                )
            )
        self.assertEqual(caught.exception.code, "ledger_set_changed")
        self.assertIn("surprise.yml", caught.exception.message)

    def test_an_unclassified_pull_request_cannot_be_repinned_away(self) -> None:
        with self.assertRaises(baseline.BaselineError) as caught:
            _ledger().at(
                _live(
                    pulls=(
                        baseline.OpenPullRequest(
                            number=99, head_sha="a" * 40, paths=(".github/workflows/ci.yml",)
                        ),
                    )
                )
            )
        self.assertEqual(caught.exception.code, "ledger_pull_requests_changed")

    def test_a_repinning_records_the_new_pull_request_head(self) -> None:
        # Re-pinning is how a rewrite gets admitted, so it has to move the
        # recorded head commit as well as the paths. Recording the new head
        # without reading the pull request would make the gate agree with a
        # commit nobody looked at, which is the failure the head SHA exists to
        # prevent.
        ledger = baseline.ParityLedger(
            repository="example/consumer",
            audited_head=AUDITED_HEAD,
            workflows=_ledger().workflows,
            open_pull_requests=(
                baseline.LedgerPullRequest(
                    number=12,
                    head_sha=PR_HEAD,
                    paths=(".github/workflows/ci.yml",),
                    classification="consumer-local",
                    owner="#60",
                    rationale="adds a product build step",
                ),
            ),
        )
        moved = _live(
            pulls=(
                baseline.OpenPullRequest(
                    number=12,
                    head_sha="c" * 40,
                    paths=(".github/workflows/ci.yml",),
                ),
            )
        )
        # The head movement is a blocker until it is acted on, not something the
        # re-pinning quietly absorbs on its way through.
        self.assertFalse(baseline.audit(ledger, moved).ready)
        repinned = ledger.at(moved, audited_at="2026-04-01")
        self.assertEqual(repinned.open_pull_requests[0].head_sha, "c" * 40)
        self.assertEqual(repinned.audited_at, "2026-04-01")
        # Classification is carried across unchanged: re-pinning records that
        # the commit was re-read, and it does not re-decide what it is.
        self.assertEqual(repinned.open_pull_requests[0].classification, "consumer-local")
        self.assertTrue(baseline.audit(repinned, moved).ready)

    def test_a_withdrawal_has_to_be_named(self) -> None:
        gone = _live(workflows=())
        with self.assertRaises(baseline.BaselineError) as caught:
            _ledger().at(gone)
        self.assertEqual(caught.exception.code, "withdrawals_not_acknowledged")
        repinned = _ledger().at(gone, accept_removed=(".github/workflows/ci.yml",))
        self.assertEqual(repinned.workflows, ())
        self.assertTrue(baseline.audit(repinned, gone).ready)

    def test_a_closing_pull_request_has_to_be_named(self) -> None:
        ledger = baseline.ParityLedger(
            repository="example/consumer",
            audited_head=AUDITED_HEAD,
            workflows=_ledger().workflows,
            open_pull_requests=(
                baseline.LedgerPullRequest(
                    number=12,
                    head_sha=PR_HEAD,
                    paths=(".github/workflows/ci.yml",),
                    classification="consumer-local",
                    owner="#60",
                    rationale="adds a product build step",
                ),
            ),
        )
        with self.assertRaises(baseline.BaselineError) as caught:
            ledger.at(_live())
        self.assertEqual(caught.exception.code, "closings_not_acknowledged")
        repinned = ledger.at(_live(), accept_closed=(12,))
        self.assertEqual(repinned.open_pull_requests, ())
        self.assertTrue(baseline.audit(repinned, _live()).ready)


class ReportIntegrity(unittest.TestCase):
    """A report that does not describe itself cannot be carried into a decision."""

    def test_a_report_round_trips_through_its_document(self) -> None:
        report = baseline.audit(
            _ledger(), _live(workflows=[(".github/workflows/ci.yml", "c" * 40)])
        )
        self.assertEqual(baseline.read_report(report.describe()), report)

    def test_a_drifted_report_with_no_blocker_is_refused(self) -> None:
        report = baseline.audit(
            _ledger(), _live(workflows=[(".github/workflows/ci.yml", "c" * 40)])
        )
        document = report.describe()
        document["blockers"] = []
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertEqual(caught.exception.code, "inconsistent_report")

    def test_an_incomplete_reading_may_carry_its_incompleteness(self) -> None:
        # The one blocker an incomplete reading is allowed is the incompleteness
        # itself. Reading it as an error would make the instrument unable to
        # report its own gaps, which is the failure mode this module exists to
        # prevent.
        report = baseline.audit(_ledger(), _live(complete=False, limits=["unreadable"]))
        self.assertEqual(baseline.read_report(report.describe()), report)
        self.assertEqual(baseline.read_report(report.describe()).verdict, "incomplete")

    def test_a_report_with_no_evidence_digest_is_refused(self) -> None:
        report = baseline.audit(_ledger(), _live())
        document = report.describe()
        document["evidence_digest"] = ""
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.read_report(document)
        self.assertEqual(caught.exception.code, "inconsistent_report")


class Rendering(unittest.TestCase):
    def test_the_table_names_every_audited_file(self) -> None:
        rendered = baseline.render_markdown(_ledger())
        self.assertIn(".github/workflows/ci.yml", rendered)
        self.assertIn("absorbed", rendered)
        self.assertIn("#11", rendered)
        # Generated, so the document cannot claim to be hand-maintained.
        self.assertIn("render_markdown", rendered)

    def test_the_table_takes_the_repository_name_from_the_ledger(self) -> None:
        # This module is generic core. A product name typed into the renderer
        # would be one more consumer baked into code that has to resolve them all,
        # so the heading is derived and the ledger decides what it says.
        rendered = baseline.render_markdown(_ledger())
        self.assertIn("`example/consumer` workflow ledger", rendered)
        self.assertIn("nanodictate", baseline.render_markdown(
            baseline.ParityLedger(repository="nanodictate/nanodictate")
        ).lower())


if __name__ == "__main__":
    unittest.main()