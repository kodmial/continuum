"""Evidence: only the exact commit counts, and "unknown" never means green."""

from __future__ import annotations

import unittest

from continuum.release.evidence import (
    CODE_CHANGES_REQUESTED,
    CODE_CHECKS_FAILED,
    CODE_CHECKS_MISSING,
    CODE_CHECKS_PENDING,
    CODE_PR_NOT_MERGED,
    CODE_READY,
    CODE_REVIEW_MISSING,
    CODE_REVIEW_STALE,
    CODE_STALE_HEAD,
    CODE_UNAVAILABLE,
    EvidenceGate,
    UnavailableEvidence,
)

SHA = "a" * 40
OTHER = "b" * 40


class Source:
    """A source for one of the gate's three ports; unused ports answer empty."""

    def __init__(self, runs=(), pull=None, review_list=(), raise_on_checks=None):
        self._runs = list(runs)
        self._pull = pull
        self._reviews = list(review_list)
        self._raise = raise_on_checks

    def check_runs(self, sha):
        if self._raise:
            raise self._raise
        return self._runs

    def pull_request(self, number):
        return self._pull

    def reviews(self, number):
        return self._reviews


def run(name, *, sha=SHA, status="completed", conclusion="success"):
    return {"name": name, "head_sha": sha, "status": status, "conclusion": conclusion}


class GateTests(unittest.TestCase):
    def gate(self, source, **options):
        return EvidenceGate(check_runs=source, **options)

    def test_a_green_required_check_on_the_commit_is_ready(self):
        report = self.gate(Source([run("CI")]), required_checks=("CI",)).evaluate(SHA)
        self.assertTrue(report.ok)
        self.assertEqual(report.code, CODE_READY)

    def test_a_missing_required_check_is_retryable(self):
        report = self.gate(Source([]), required_checks=("CI",)).evaluate(SHA)
        self.assertEqual(report.code, CODE_CHECKS_MISSING)
        self.assertTrue(report.retryable)

    def test_a_running_check_is_retryable(self):
        report = self.gate(
            Source([run("CI", status="in_progress", conclusion=None)],),
            required_checks=("CI",),
        ).evaluate(SHA)
        self.assertEqual(report.code, CODE_CHECKS_PENDING)
        self.assertTrue(report.retryable)

    def test_a_failed_check_is_not_retryable(self):
        report = self.gate(
            Source([run("CI", conclusion="failure")]), required_checks=("CI",)
        ).evaluate(SHA)
        self.assertEqual(report.code, CODE_CHECKS_FAILED)
        self.assertFalse(report.retryable)

    def test_a_check_on_another_commit_does_not_count(self):
        report = self.gate(
            Source([run("CI", sha=OTHER)]), required_checks=("CI",)
        ).evaluate(SHA)
        self.assertEqual(report.code, CODE_CHECKS_MISSING)

    def test_with_no_configured_checks_a_stale_run_is_refused(self):
        report = self.gate(Source([run("CI", sha=OTHER)])).evaluate(SHA)
        self.assertEqual(report.code, CODE_STALE_HEAD)

    def test_with_no_configured_checks_an_empty_list_is_not_a_failure(self):
        # A repository that has not named required checks has nothing to be
        # stale about; the answer is "no evidence was required", not "failed".
        report = self.gate(Source([])).evaluate(SHA)
        self.assertTrue(report.ok)

    def test_an_unreadable_source_is_retryable(self):
        report = self.gate(Source(raise_on_checks=RuntimeError("boom")), required_checks=("CI",)).evaluate(SHA)
        self.assertEqual(report.code, CODE_UNAVAILABLE)
        self.assertTrue(report.retryable)

    def test_a_stale_pull_request_head_is_refused(self):
        report = EvidenceGate(
            check_runs=Source([run("CI")]),
            pull_request=Source(pull={"head": {"sha": OTHER}, "merged": True}),
            reviews=Source(),
            required_checks=("CI",),
            require_review=True,
        ).evaluate(SHA, pull_request=7)
        self.assertEqual(report.code, CODE_STALE_HEAD)

    def test_an_unmerged_pull_request_is_retryable(self):
        source = Source([run("CI")])
        gate = EvidenceGate(
            check_runs=source,
            pull_request=Source(pull={"head": {"sha": SHA}, "merged": False}),
            reviews=Source(),
            required_checks=("CI",),
            require_merged=True,
        )
        report = gate.evaluate(SHA, pull_request=7)
        self.assertEqual(report.code, CODE_PR_NOT_MERGED)
        self.assertTrue(report.retryable)

    def test_an_approval_of_an_older_commit_is_stale(self):
        source = Source([run("CI")])
        gate = EvidenceGate(
            check_runs=source,
            pull_request=Source(pull={"head": {"sha": SHA}, "merged": True}),
            reviews=Source(
                review_list=[
                    {
                        "user": {"login": "reviewer"},
                        "state": "APPROVED",
                        "commit_id": OTHER,
                        "submitted_at": "2026-01-01T00:00:00Z",
                    }
                ]
            ),
            required_checks=("CI",),
            require_review=True,
        )
        report = gate.evaluate(SHA, pull_request=7)
        self.assertEqual(report.code, CODE_REVIEW_STALE)

    def test_an_outstanding_changes_requested_wins_over_an_old_approval(self):
        source = Source([run("CI")])
        gate = EvidenceGate(
            check_runs=source,
            pull_request=Source(pull={"head": {"sha": SHA}, "merged": True}),
            reviews=Source(
                review_list=[
                    {
                        "user": {"login": "reviewer"},
                        "state": "APPROVED",
                        "commit_id": SHA,
                        "submitted_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "user": {"login": "reviewer"},
                        "state": "CHANGES_REQUESTED",
                        "commit_id": SHA,
                        "submitted_at": "2026-01-02T00:00:00Z",
                    },
                ]
            ),
            required_checks=("CI",),
            require_review=True,
        )
        report = gate.evaluate(SHA, pull_request=7)
        self.assertEqual(report.code, CODE_CHANGES_REQUESTED)
        self.assertFalse(report.retryable)

    def test_no_approval_is_retryable(self):
        source = Source([run("CI")])
        gate = EvidenceGate(
            check_runs=source,
            pull_request=Source(pull={"head": {"sha": SHA}, "merged": True}),
            reviews=Source(),
            required_checks=("CI",),
            require_review=True,
        )
        report = gate.evaluate(SHA, pull_request=7)
        self.assertEqual(report.code, CODE_REVIEW_MISSING)
        self.assertTrue(report.retryable)

    def test_review_without_a_pull_request_number_is_unavailable(self):
        gate = EvidenceGate(
            check_runs=Source([run("CI")]),
            pull_request=Source(),
            reviews=Source(),
            required_checks=("CI",),
            require_review=True,
        )
        report = gate.evaluate(SHA)
        self.assertEqual(report.code, CODE_UNAVAILABLE)

    def test_a_gate_that_requires_review_needs_a_source(self):
        with self.assertRaises(Exception):
            EvidenceGate(check_runs=Source(), require_review=True)

    def test_a_gate_needs_a_check_run_source(self):
        with self.assertRaises(Exception):
            EvidenceGate(check_runs=object())

    def test_unavailable_evidence_never_claims_ready(self):
        report = UnavailableEvidence("no token").evaluate(SHA)
        self.assertFalse(report.ok)
        self.assertEqual(report.code, CODE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
