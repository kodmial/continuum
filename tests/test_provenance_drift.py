"""Source drift: the classification, the report, the deduplicated issue, and the
promotion gate that refuses an unclassified advance.

The cases that matter are the ones where a source moved and the answer is not
obvious:

* a path whose disposition describes the *kind* of file keeps its classification
  when the file changes, because a consumer-local workflow that was edited is still
  the consumer's;
* a path whose disposition describes the *content* loses it, because "we absorbed
  this" is not a claim about whatever replaced it;
* a source nobody could read is never reported as a source that did not move;
* and a second scan of the same evidence opens no second issue, while a scan of
  *different* evidence does.
"""

from __future__ import annotations

import json
import unittest

from continuum.provenance import drift, ledger as ledger_module, publish
from continuum.provenance.ledger import ProvenanceError


BASELINE = "08a5d920c5dbf1f4203160e92e3e530c5bbb3b8f"
HEAD = "f" * 40

DICTATE = ".github/workflows/dictate.yml"
RELEASE = ".github/workflows/release.yml"
LOCAL = ".github/workflows/ci.yml"


def _ledger_document(**source_overrides):
    source = {
        "id": "nanodictate",
        "repository": "kodmial/nanodictate",
        "visibility": "public",
        "disposition": "absorbed",
        "severity": "p0",
        "baseline_sha": BASELINE,
        "audited_at": "2026-09-30T00:00:00Z",
        "tracked_prefixes": [".github/workflows/"],
        "path_dispositions": {DICTATE: "must-port", RELEASE: "consumer-local"},
        "continuum": {
            "implementation": ["src/continuum/provenance/drift.py"],
            "tests": ["tests/test_provenance_drift.py"],
        },
    }
    source.update(source_overrides)
    return {
        "schema": ledger_module.PROVENANCE_SCHEMA,
        "version": ledger_module.LEDGER_VERSION,
        "sources": [source],
    }


def _source(**overrides):
    return ledger_module.read_provenance(_ledger_document(**overrides)).sources[0]


def _reading(source=None, *, head_sha=BASELINE, changes=(), **kwargs):
    return drift.classify(
        source or _source(),
        head_sha=head_sha,
        # `None` is passed through rather than listed: it is the signal that no
        # comparison came back, and a helper that turned it into `[]` would assert
        # the opposite of what the test is for.
        changes=list(changes) if changes is not None else None,
        **kwargs
    )


def _changes(*paths, status="modified"):
    return [{"path": path, "status": status} for path in paths]


def _report(source=None, *, head_sha=BASELINE, changes=(), **kwargs):
    source = source or _source()
    return drift.check(
        ledger_module.ProvenanceLedger(sources=(source,)),
        readings=[_reading(source, head_sha=head_sha, changes=changes, **kwargs)],
    )


class ClassificationTests(unittest.TestCase):
    def test_a_source_that_has_not_moved_is_unchanged(self) -> None:
        reading = _reading()
        self.assertEqual(reading.status, drift.UNCHANGED)
        self.assertEqual(reading.findings, ())
        self.assertEqual(_report().verdict, drift.CLEAN)

    def test_a_change_outside_the_tracked_scope_is_not_drift(self) -> None:
        # The ledger said what was in scope, so the checker does not widen or narrow
        # that judgement itself.
        reading = _reading(head_sha=HEAD, changes=_changes("README.md", "src/main.rs"))
        self.assertEqual(reading.status, drift.UNCHANGED)

    def test_a_persistent_disposition_survives_its_path_changing(self) -> None:
        # A consumer-local workflow that was edited is still the consumer's, and
        # treating its edits as drift is how a busy repository produces noise.
        reading = _reading(head_sha=HEAD, changes=_changes(RELEASE))
        self.assertEqual(reading.status, drift.CLASSIFIED_ADVANCE)
        self.assertEqual(reading.classified, ((RELEASE, "consumer-local"),))
        self.assertEqual(reading.findings, ())
        self.assertEqual(_report(head_sha=HEAD, changes=_changes(RELEASE)).verdict, drift.CLEAN)

    def test_a_content_disposition_expires_when_its_content_moves(self) -> None:
        # "We absorbed this" is a claim about what was read. It says nothing about
        # whatever replaced it, so it cannot carry over.
        reading = _reading(head_sha=HEAD, changes=_changes(DICTATE))
        self.assertEqual(reading.status, drift.UNCLASSIFIED_DRIFT)
        self.assertEqual(len(reading.findings), 1)
        self.assertEqual(reading.findings[0].reason, drift.EXPIRED_CLASSIFICATION)
        self.assertEqual(reading.findings[0].previous_disposition, "must-port")

    def test_a_path_with_no_classification_at_all_needs_one(self) -> None:
        reading = _reading(head_sha=HEAD, changes=_changes(".github/workflows/brand-new.yml"))
        self.assertEqual(reading.status, drift.UNCLASSIFIED_DRIFT)
        self.assertEqual(reading.findings[0].reason, drift.UNCLASSIFIED_PATH)
        self.assertEqual(reading.findings[0].previous_disposition, "")

    def test_a_removed_audited_path_is_never_a_no_op(self) -> None:
        # Even a permanently-classified path: the audit has to point at whatever
        # replaced it, or the claim outlives the file it was about.
        for status in ("removed", "renamed"):
            with self.subTest(status=status):
                reading = _reading(head_sha=HEAD, changes=_changes(RELEASE, status=status))
                self.assertEqual(reading.status, drift.UNCLASSIFIED_DRIFT)
                self.assertEqual(reading.findings[0].reason, drift.REMOVED_PATH)

    def test_the_two_reasons_are_reported_apart(self) -> None:
        # They are different pieces of work, and a reader who is told only "drift"
        # cannot tell whether to write a disposition or re-read the content.
        reading = _reading(
            head_sha=HEAD,
            changes=_changes(DICTATE, ".github/workflows/brand-new.yml"),
        )
        self.assertEqual(
            sorted(finding.reason for finding in reading.findings),
            sorted([drift.EXPIRED_CLASSIFICATION, drift.UNCLASSIFIED_PATH]),
        )

    def test_a_missing_change_set_is_not_an_unchanged_source(self) -> None:
        # The difference between "the comparison ran and found nothing" and "no
        # comparison came back" is the difference between a clean audit and a green
        # one produced by a failure.
        self.assertEqual(_reading().status, drift.UNCHANGED)
        self.assertEqual(_reading(changes=None).status, drift.UNREADABLE)
        self.assertEqual(_report(changes=None).verdict, drift.INCOMPLETE)

    def test_a_head_that_is_not_a_commit_is_not_a_reading(self) -> None:
        self.assertEqual(_reading(head_sha="main").status, drift.UNREADABLE)
        self.assertEqual(_reading(head_sha="").status, drift.UNREADABLE)

    def test_a_partial_read_makes_the_report_incomplete(self) -> None:
        # The source shows no drift, but the audit did not see everything, so the
        # report cannot say it is clean.
        report = _report(head_sha=HEAD, changes=_changes(RELEASE), limits=["change-set-truncated"])
        self.assertEqual(report.verdict, drift.INCOMPLETE)
        self.assertEqual(report.drifted, ())


class ReportTests(unittest.TestCase):
    def test_a_source_with_no_reading_is_unreadable_not_absent(self) -> None:
        # A checker that failed to look at a repository must not be able to produce
        # a clean report by omitting it.
        report = drift.check(ledger_module.ProvenanceLedger(sources=(_source(),)))
        self.assertEqual(report.verdict, drift.INCOMPLETE)
        self.assertEqual(report.reading("nanodictate").status, drift.UNREADABLE)
        self.assertIn("no-reading-collected", report.reading("nanodictate").limits)

    def test_two_readings_for_one_source_are_refused_not_merged(self) -> None:
        # The second would displace the first and take its findings with it, which is
        # a way for drift to vanish from a report without anything recording it.
        drifted = _reading(head_sha=HEAD, changes=_changes(DICTATE))
        clean = _reading()
        with self.assertRaises(ProvenanceError) as caught:
            drift.check(ledger_module.ProvenanceLedger(sources=(_source(),)), readings=[drifted, clean])
        self.assertEqual(caught.exception.code, "duplicate_reading")

    def test_drift_outranks_an_unreadable_source_in_the_verdict(self) -> None:
        source = _source()
        other = ledger_module.read_provenance(
            {
                "schema": ledger_module.PROVENANCE_SCHEMA,
                "version": ledger_module.LEDGER_VERSION,
                "sources": [
                    dict(
                        _ledger_document()["sources"][0],
                        id="second-source",
                        repository="acme/second",
                    )
                ],
            }
        ).sources[0]
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(source, other)),
            readings=[
                _reading(source, head_sha=HEAD, changes=_changes(DICTATE)),
                drift.SourceReading(
                    id="second-source",
                    repository="acme/second",
                    visibility="private",
                    severity="p1",
                    status=drift.UNAVAILABLE,
                    baseline_sha=BASELINE,
                    limits=("no-credential-configured-for-private-source",),
                ),
            ],
        )
        self.assertEqual(report.verdict, drift.DRIFTED)
        self.assertTrue(report.incomplete)
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(len(report.drifted[0].findings), 1)

    def test_an_unreadable_source_never_reads_as_clean(self) -> None:
        for status in (drift.UNAVAILABLE, drift.UNREADABLE):
            with self.subTest(status=status):
                report = drift.check(
                    ledger_module.ProvenanceLedger(sources=(_source(),)),
                    readings=[
                        drift.SourceReading(
                            id="nanodictate",
                            repository="kodmial/nanodictate",
                            visibility="public",
                            severity="p0",
                            status=status,
                            baseline_sha=BASELINE,
                        )
                    ],
                )
                self.assertEqual(report.verdict, drift.INCOMPLETE)
                self.assertFalse(report.no_unclassified_source_drift and not report.incomplete)

    def test_only_p0_drift_is_counted_as_p0(self) -> None:
        p1 = _source(severity="p1")
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(p1,)),
            readings=[_reading(p1, head_sha=HEAD, changes=_changes(DICTATE))],
        )
        self.assertEqual(report.unclassified_p0, ())
        self.assertTrue(report.drifted)

    def test_a_reading_taken_against_another_ledger_version_keeps_its_finding(self) -> None:
        # A reviewer needs the finding that was collected more than they need the
        # report to be tidy, so the status is not overwritten -- only the reading
        # is marked partial.
        stale = _reading(head_sha=HEAD, changes=_changes(DICTATE))
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(_source(severity="p2"),)),
            readings=[stale],
        )
        self.assertEqual(report.verdict, drift.DRIFTED)
        self.assertIn("ledger-changed-since-reading", report.drifted[0].limits)
        self.assertEqual(len(report.drifted[0].findings), 1)


class FingerprintTests(unittest.TestCase):
    def test_the_fingerprint_is_the_same_for_the_same_evidence(self) -> None:
        first = _report(head_sha=HEAD, changes=_changes(DICTATE))
        second = _report(head_sha=HEAD, changes=_changes(DICTATE), audited_at="later")
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_the_fingerprint_changes_when_the_head_moves(self) -> None:
        self.assertNotEqual(
            _report(head_sha=HEAD, changes=_changes(DICTATE)).fingerprint,
            _report(head_sha="e" * 40, changes=_changes(DICTATE)).fingerprint,
        )

    def test_the_fingerprint_changes_when_the_paths_change(self) -> None:
        self.assertNotEqual(
            _report(head_sha=HEAD, changes=_changes(DICTATE)).fingerprint,
            _report(head_sha=HEAD, changes=_changes(DICTATE, RELEASE)).fingerprint,
        )

    def test_the_fingerprint_changes_when_the_pull_requests_change(self) -> None:
        self.assertNotEqual(
            _report(head_sha=HEAD, changes=_changes(DICTATE), pull_requests=[7]).fingerprint,
            _report(head_sha=HEAD, changes=_changes(DICTATE), pull_requests=[8]).fingerprint,
        )

    def test_an_unchanged_audit_does_not_change_the_fingerprint(self) -> None:
        # Nothing is being reported, so there is nothing to deduplicate against.
        self.assertEqual(_report().fingerprint, _report(changes=[]).fingerprint)

    def test_the_promotion_digest_does_not_move_when_only_the_audit_instant_does(self) -> None:
        # The weekly audit re-runs against unchanged repositories forever. If its
        # digest moved every time, every consumer pin approval would expire on a
        # schedule and the audit would cost more than it protects.
        self.assertEqual(_report(audited_at="one").digest, _report(audited_at="two").digest)

    def test_the_promotion_digest_moves_when_a_clean_advance_changes(self) -> None:
        # Two clean reports that named only their drifted sources would digest the
        # same, so a promotion approved against the first would survive the second
        # with nothing anywhere recording that what was approved had moved.
        self.assertNotEqual(
            _report(head_sha=HEAD, changes=_changes(RELEASE)).digest,
            _report(head_sha=HEAD, changes=_changes(DICTATE)).digest,
        )

    def test_the_promotion_digest_moves_when_only_a_limit_changes(self) -> None:
        # A limit is the difference between "nothing to classify" and "could not
        # tell", so it belongs in what an approval is bound to.
        self.assertNotEqual(
            _report(limits=("change-set-truncated",)).digest,
            _report(limits=("baseline-is-not-an-ancestor",)).digest,
        )


class IssueTests(unittest.TestCase):
    def test_nothing_to_say_opens_nothing(self) -> None:
        plan = drift.issue_plan(_report())
        self.assertEqual(plan["action"], "none")

    def test_drift_with_no_open_issue_creates_one(self) -> None:
        plan = drift.issue_plan(_report(head_sha=HEAD, changes=_changes(DICTATE)))
        self.assertEqual(plan["action"], "create")

    def test_the_same_evidence_twice_opens_one_issue(self) -> None:
        report = _report(head_sha=HEAD, changes=_changes(DICTATE))
        first = drift.issue_plan(report)
        again = drift.issue_plan(report, existing_number=42, existing_body=first["body"])
        self.assertEqual(again["action"], "none")
        self.assertIn("#42", again["reason"])

    def test_different_evidence_comments_on_the_open_issue(self) -> None:
        # The open issue describes a state that no longer exists, so a comment
        # updates the record rather than leaving a stale one in place.
        first = drift.issue_plan(_report(head_sha=HEAD, changes=_changes(DICTATE)))
        second = drift.issue_plan(
            _report(head_sha="e" * 40, changes=_changes(DICTATE, RELEASE)),
            existing_number=42,
            existing_body=first["body"],
        )
        self.assertEqual(second["action"], "comment")
        self.assertEqual(second["number"], 42)

    def test_an_unreadable_source_still_produces_an_issue(self) -> None:
        # A source nobody could read is not a source that did not move, and the
        # person who can fix the credential should be told.
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(_source(),)),
            readings=[
                drift.SourceReading(
                    id="nanodictate",
                    repository="kodmial/nanodictate",
                    visibility="private",
                    severity="p0",
                    status=drift.UNAVAILABLE,
                    baseline_sha=BASELINE,
                    limits=("no-credential-configured-for-private-source",),
                )
            ],
        )
        plan = drift.issue_plan(report)
        self.assertEqual(plan["action"], "create")
        self.assertIn("could not be read", plan["body"])

    def test_the_body_carries_the_evidence_a_reviewer_needs(self) -> None:
        body = drift.issue_plan(
            _report(head_sha=HEAD, changes=_changes(DICTATE), pull_requests=[12])
        )["body"]
        self.assertIn(BASELINE[:12], body)
        self.assertIn(HEAD[:12], body)
        self.assertIn(DICTATE, body)
        self.assertIn("#12", body)
        self.assertIn(drift.ISSUE_MARKER, body)

    def test_the_body_carries_no_file_content_or_diff(self) -> None:
        # What moved and how far, never what it said: a parity issue is a public
        # artifact about somebody else's repository. The markers are checked per
        # line, because `---` is also a Markdown table rule and a table is not a
        # diff.
        body = drift.issue_plan(_report(head_sha=HEAD, changes=_changes(DICTATE)))["body"]
        for line in body.splitlines():
            stripped = line.strip()
            if set(stripped) <= set("|- "):
                continue  # a Markdown table rule, which is not a diff hunk
            self.assertFalse(
                stripped.lstrip("| ").startswith(("@@", "+++", "---")),
                "diff marker in the parity issue: {!r}".format(line),
            )
        for forbidden in ("diff --git", "run:", "uses:"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_a_private_source_is_not_named_in_the_issue(self) -> None:
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(_source(visibility="private", repository="acme/private"),)),
            readings=[_reading(head_sha=HEAD, changes=_changes(DICTATE))],
        )
        body = drift.issue_plan(report)["body"]
        self.assertNotIn("acme/private", body)
        self.assertIn("nanodictate", body)

    def test_a_classified_advance_is_reported_but_not_escalated(self) -> None:
        report = _report(head_sha=HEAD, changes=_changes(RELEASE))
        self.assertEqual(report.verdict, drift.CLEAN)
        self.assertEqual(drift.issue_plan(report)["action"], "none")
        # The advance is still in the audit trail for whoever asks what moved.
        self.assertEqual(report.advanced[0].classified, ((RELEASE, "consumer-local"),))


class ReportDocumentTests(unittest.TestCase):
    def test_a_report_round_trips(self) -> None:
        report = _report(head_sha=HEAD, changes=_changes(DICTATE))
        self.assertEqual(drift.read_report(report.describe()).digest, report.digest)

    def test_a_report_that_lies_about_its_verdict_is_refused(self) -> None:
        document = _report(head_sha=HEAD, changes=_changes(DICTATE)).describe()
        document["verdict"] = drift.CLEAN
        with self.assertRaises(ProvenanceError) as caught:
            drift.read_report(document)
        self.assertEqual(caught.exception.code, "report_verdict_mismatch")

    def test_a_report_with_a_forged_digest_is_refused(self) -> None:
        document = _report(head_sha=HEAD, changes=_changes(DICTATE)).describe()
        document["digest"] = "pdr-0000000000000000"
        with self.assertRaises(ProvenanceError) as caught:
            drift.read_report(document)
        self.assertEqual(caught.exception.code, "report_digest_mismatch")

    def test_a_report_with_no_readings_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            drift.read_report(
                {"schema": "continuum.provenance-drift/v1", "readings": []}
            )
        self.assertEqual(caught.exception.code, "no_readings")

    def test_a_status_this_checker_does_not_produce_is_refused(self) -> None:
        document = _report().describe()
        document["readings"][0]["status"] = "fine-ish"
        with self.assertRaises(ProvenanceError) as caught:
            drift.read_report(document)
        self.assertEqual(caught.exception.code, "unknown_reading_status")


class PrivateReadTests(unittest.TestCase):
    """A private source is read through its own credential or not at all."""

    class FakeClient:
        """The bound client the reader is handed, with each read's outcome set here.

        A real subclass per test would carry four positional arguments into
        ``__init__`` and quietly stop being the fake the test meant to build, so the
        variable behaviour is data on one class instead.
        """

        owner = "acme"
        name = "private"

        def __init__(self, calls, *, head=HEAD, ref_status=0, comparison=None, pulls_status=0, pulls=None):
            self.calls = calls
            self.head = head
            self.ref_status = ref_status
            self.comparison = comparison or {
                "status": "ahead",
                "behind_by": 0,
                "files": [{"path": DICTATE, "status": "modified"}],
                "files_truncated": False,
            }
            self.pulls_status = pulls_status
            self.pulls = pulls if pulls is not None else [{"number": 5}]

        @staticmethod
        def _error(status):
            from continuum.review.github import GitHubError

            # The body is deliberately loud: the reading must never carry it.
            return GitHubError("API said something with a body in it", status=status)

        def default_branch(self):
            return "main"

        def ref_sha(self, ref):
            self.calls.append(("ref_sha", ref))
            if self.ref_status:
                raise self._error(self.ref_status)
            return self.head

        def compare_commits(self, base, head):
            self.calls.append(("compare_commits", base))
            return self.comparison

        def list_pulls(self, state="open", limit=0):
            self.calls.append(("list_pulls", state, limit))
            if self.pulls_status:
                raise self._error(self.pulls_status)
            return self.pulls[:limit] if limit else self.pulls

    def setUp(self) -> None:
        self.calls = []

    def _client(self, **kwargs):
        return self.FakeClient(self.calls, **kwargs)

    def test_an_unreadable_source_reports_the_status_and_not_the_body(self) -> None:
        # GitHubError carries the API's response body. For a private source that is
        # metadata this plane may not put in a report, an issue, or a log line.
        reading = drift.read_source(
            self._client(ref_status=404),
            _source(visibility="private", repository="acme/private"),
            private_token="a-private-secret",
        )
        self.assertEqual(reading.status, drift.UNREADABLE)
        self.assertEqual(reading.limits, ("read-failed-status-404",))
        rendered = json.dumps(reading.describe())
        self.assertNotIn("API said something", rendered)

    def test_an_unchanged_source_costs_one_read_not_a_comparison(self) -> None:
        # This is the difference between a weekly audit that costs one request per
        # source and one that costs a full change set every week, forever.
        reading = drift.read_source(self._client(head=BASELINE), _source())
        self.assertEqual(reading.status, drift.UNCHANGED)
        self.assertEqual([call[0] for call in self.calls], ["ref_sha"])

    def test_a_moved_source_also_collects_the_pull_request_evidence(self) -> None:
        reading = drift.read_source(self._client(), _source())
        self.assertEqual([call[0] for call in self.calls], ["ref_sha", "compare_commits", "list_pulls"])
        self.assertEqual(reading.pull_requests, (5,))

    def test_a_private_source_with_no_private_credential_is_never_requested(self) -> None:
        # The caller holds an ambient token and no private one. "The caller was
        # careful" is not a control, so the reader refuses before making a request:
        # the alternative is reading a private repository with whatever the ambient
        # token happens to reach.
        reading = drift.read_source(self._client(), _source(visibility="private", repository="acme/private"))
        self.assertEqual(reading.status, drift.UNAVAILABLE)
        self.assertEqual(self.calls, [])
        self.assertEqual(
            reading.limits, ("no-credential-configured-for-private-source",)
        )

    def test_the_pull_request_evidence_is_bounded_at_the_request(self) -> None:
        # Twenty numbers is what a reviewer reads. Walking every page of a
        # repository with five hundred open pull requests to print twenty of them is
        # a cost paid every week for evidence nobody looks at.
        seen = []

        class Bounded(self.FakeClient):
            def list_pulls(self, state="open", limit=0):
                seen.append(limit)
                return super().list_pulls(state=state, limit=limit)

        client = Bounded(self.calls)
        drift.read_source(client, _source(), pull_request_limit=3)
        self.assertEqual(seen, [3])

    def test_the_private_credential_is_never_recorded_by_a_successful_read(self) -> None:
        reading = drift.read_source(
            self._client(), _source(visibility="private"), private_token="a-real-secret-value"
        )
        self.assertNotIn("a-real-secret-value", json.dumps(reading.describe()))
        self.assertEqual(reading.credential, "private-read-token")

    def test_a_diverged_baseline_is_recorded_as_a_limit(self) -> None:
        # The classifications were made against content that is no longer in this
        # history, so the comparison cannot be trusted to describe what changed.
        reading = drift.read_source(
            self._client(
                comparison={
                    "status": "diverged",
                    "behind_by": 4,
                    "files": [{"path": DICTATE, "status": "modified"}],
                    "files_truncated": False,
                }
            ),
            _source(),
        )
        self.assertIn("baseline-is-not-an-ancestor", reading.limits)

    def test_a_truncated_change_set_is_recorded_rather_than_believed(self) -> None:
        reading = drift.read_source(
            self._client(
                comparison={
                    "status": "ahead",
                    "behind_by": 0,
                    "files": [{"path": DICTATE, "status": "modified"}] * 300,
                    "files_truncated": True,
                }
            ),
            _source(),
        )
        self.assertIn("change-set-truncated", reading.limits)

    def test_failing_pull_request_evidence_does_not_fail_the_reading(self) -> None:
        # PR numbers are corroborating evidence for a human. The change set still
        # decides the finding, and a missing PR number must not become a missing
        # drift report.
        reading = drift.read_source(self._client(pulls_status=503), _source())
        self.assertEqual(reading.status, drift.UNCLASSIFIED_DRIFT)
        self.assertIn("pull-request-evidence-unavailable-503", reading.limits)

    def test_the_credential_is_named_but_never_recorded(self) -> None:
        reading = drift.read_source(
            self._client(), _source(visibility="private"), private_token="a-private-secret"
        )
        self.assertEqual(reading.credential, "private-read-token")
        self.assertNotIn("token", json.dumps(reading.describe()).replace(
            '"private-read-token"', ""
        ))


class PublishPlanTests(unittest.TestCase):
    class _Client:
        owner = "acme"
        name = "continuum"

        def __init__(self, issues):
            self.issues = issues
            self.paginated = 0

        def paginate(self, path):
            self.paginated += 1
            return self.issues

        def request(self, method, path, body=None, preview=None):
            raise AssertionError("planning must not write")

        def create_issue_comment(self, number, body):
            raise AssertionError("planning must not write")

    def test_no_open_issue_means_create(self) -> None:
        client = self._Client([])
        plan = publish.build_plan(client, _report(head_sha=HEAD, changes=_changes(DICTATE)))
        self.assertEqual(plan["action"], "create")
        self.assertEqual(plan["schema"], publish.PLAN_SCHEMA)

    def test_an_open_issue_with_this_fingerprint_means_do_nothing(self) -> None:
        report = _report(head_sha=HEAD, changes=_changes(DICTATE))
        body = drift.render_issue(report)["body"]
        client = self._Client([{"number": 9, "body": body, "title": "parity"}])
        self.assertEqual(publish.build_plan(client, report)["action"], "none")

    def test_an_issue_that_lost_its_label_is_still_found(self) -> None:
        # The label narrows the search; the marker is what identifies this plane's
        # issue. Searching only by label means a hand-removed label, or a label
        # GitHub dropped at creation, quietly becomes a second open issue.
        body = drift.render_issue(_report(head_sha=HEAD, changes=_changes(DICTATE)))["body"]

        class Labelless(self._Client):
            def paginate(self, path):
                if "labels=" in path:
                    return []  # the label search finds nothing
                return [{"number": 9, "body": body, "title": "parity"}]

        client = Labelless([])
        self.assertEqual(publish.find_open_issue(client)["number"], 9)

    def test_a_pull_request_is_never_treated_as_the_parity_issue(self) -> None:
        # GitHub's issues endpoint also returns pull requests; commenting on one
        # would put drift evidence in a code review.
        body = drift.render_issue(_report(head_sha=HEAD, changes=_changes(DICTATE)))["body"]
        client = self._Client([{"number": 9, "body": body, "pull_request": {"url": "x"}}])
        self.assertEqual(
            publish.build_plan(client, _report(head_sha=HEAD, changes=_changes(DICTATE)))["action"],
            "create",
        )

    def test_an_unrelated_issue_is_not_adopted(self) -> None:
        client = self._Client([{"number": 9, "body": "an unrelated bug", "title": "bug"}])
        self.assertEqual(
            publish.build_plan(client, _report(head_sha=HEAD, changes=_changes(DICTATE)))["action"],
            "create",
        )

    def test_a_clean_audit_writes_nothing_even_with_an_open_issue(self) -> None:
        client = self._Client([{"number": 9, "body": drift.ISSUE_MARKER, "title": "parity"}])
        self.assertEqual(publish.build_plan(client, _report())["action"], "none")

    def test_an_action_this_publisher_does_not_know_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError) as caught:
            publish.apply_plan(self._Client([]), {"action": "delete-everything"})
        self.assertEqual(caught.exception.code, "unknown_action")


class PromotionGateTests(unittest.TestCase):
    """The gate's use of the reading: required, and load-bearing on the digest."""

    def setUp(self) -> None:
        from continuum.shadow import cutover

        self.cutover = cutover

    def _blockers(self, provenance):
        return [
            (blocker.code, blocker.scenario)
            for blocker in self.cutover.provenance_blockers(provenance)
        ]

    def test_no_reading_at_all_is_a_named_blocker(self) -> None:
        codes = self._blockers(None)
        self.assertEqual([code for code, _ in codes], ["no_provenance_evidence"])

    def test_unclassified_drift_blocks(self) -> None:
        self.assertEqual(
            self._blockers(_report(head_sha=HEAD, changes=_changes(DICTATE))),
            [("provenance_unclassified_drift", "nanodictate")],
        )

    def test_an_unavailable_private_source_blocks_by_its_own_code(self) -> None:
        source = _source(visibility="private", repository="acme/private")
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(source,)),
            readings=[
                drift.SourceReading(
                    id=source.id,
                    repository=source.repository,
                    visibility="private",
                    severity="p0",
                    status=drift.UNAVAILABLE,
                    baseline_sha=BASELINE,
                    limits=("no-credential-configured-for-private-source",),
                )
            ],
        )
        self.assertEqual(
            self._blockers(report), [("provenance_source_unavailable", "nanodictate")]
        )

    def test_an_unreadable_source_blocks_by_its_own_code(self) -> None:
        source = _source()
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(source,)),
            readings=[
                drift.SourceReading(
                    id=source.id,
                    repository=source.repository,
                    visibility="public",
                    severity="p0",
                    status=drift.UNREADABLE,
                    baseline_sha=BASELINE,
                    limits=("read-failed-status-500",),
                )
            ],
        )
        self.assertEqual(
            self._blockers(report), [("provenance_source_unreadable", "nanodictate")]
        )

    def test_a_clean_reading_blocks_nothing(self) -> None:
        self.assertEqual(self._blockers(_report(head_sha=HEAD, changes=_changes(RELEASE))), [])

    def test_each_blocker_names_its_source(self) -> None:
        # One blocker per source rather than a summary, because the fix differs: a
        # credential to configure is not the same work as a classification.
        source = _source()
        report = drift.check(
            ledger_module.ProvenanceLedger(sources=(source,)),
            readings=[_reading(source, head_sha=HEAD, changes=_changes(DICTATE))],
        )
        blockers = self.cutover.provenance_blockers(report)
        self.assertEqual(len(blockers), 1)
        self.assertIn(DICTATE, blockers[0].message)
        self.assertIn(BASELINE[:12], blockers[0].message)

    def test_the_reading_is_in_the_digest(self) -> None:
        # Otherwise an approval could outlive the source state it was granted
        # against, which is the whole reason the reading is required.
        clean = _report(head_sha=HEAD, changes=_changes(RELEASE))
        drifted = _report(head_sha=HEAD, changes=_changes(DICTATE))
        self.assertNotEqual(clean.digest, drifted.digest)

    def test_an_absent_reading_and_a_clean_one_are_distinguishable(self) -> None:
        from continuum.shadow import baseline, cutover, liveness, parity

        results = []
        report = cutover.coverage(
            results, window_started_at="a", window_ended_at="b"
        )
        self.assertNotEqual(
            cutover.evidence_digest(report, results, None, (), "a", "b", provenance_digest=""),
            cutover.evidence_digest(
                report, results, None, (), "a", "b", provenance_digest=_report().digest
            ),
        )


if __name__ == "__main__":
    unittest.main()