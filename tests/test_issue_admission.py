"""Unified issue admission policy (kodmial/continuum#346)."""

from __future__ import annotations

import unittest

from continuum import issue_admission as admission


class CanonicalAdmissionTests(unittest.TestCase):
    def test_ready_is_mandatory_and_fixed(self):
        decision = admission.decide_issue_admission(labels={"priority:p0"})
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.code, admission.MISSING_READY)
        self.assertIn("automation:ready", decision.reason)

    def test_ready_admits(self):
        decision = admission.decide_issue_admission(
            labels={"priority:p0", "automation:ready"}
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.code, admission.ADMITTED)

    def test_legacy_pause_never_decides_admission(self):
        decision = admission.decide_issue_admission(
            labels={"automation:ready", "automation:paused"}
        )
        self.assertTrue(decision.admitted)

    def test_closed_stopped_blocked_and_qualifying_fail_closed(self):
        cases = [
            (dict(is_open=False), admission.ISSUE_CLOSED),
            (dict(generation_stopped=True), admission.GENERATION_STOPPED),
            (dict(blocked=True), admission.BLOCKED),
            (dict(qualifying=True), admission.QUALIFICATION_LIFECYCLE),
            (dict(is_pull_request=True), admission.TARGET_IS_PULL_REQUEST),
        ]
        for kwargs, code in cases:
            with self.subTest(code=code):
                decision = admission.decide_issue_admission(
                    labels={"automation:ready"}, **kwargs
                )
                self.assertFalse(decision.admitted)
                self.assertEqual(decision.code, code)

    def test_authorization_and_admission_are_both_required(self):
        admitted = admission.decide_issue_admission(labels={"automation:ready"})
        denied = admission.decide_issue_admission(labels=set())
        self.assertTrue(
            admission.authorization_and_admission(
                authorized=True, admission=admitted
            )
        )
        self.assertFalse(
            admission.authorization_and_admission(
                authorized=True, admission=denied
            )
        )
        self.assertFalse(
            admission.authorization_and_admission(
                authorized=False, admission=admitted
            )
        )

    def test_legacy_ready_label_configuration_is_reported_not_used(self):
        self.assertTrue(admission.legacy_ready_label_is_ignored("labels/ready"))
        self.assertTrue(
            admission.legacy_ready_label_is_ignored("automation:ready")
        )
        self.assertFalse(admission.legacy_ready_label_is_ignored(""))


if __name__ == "__main__":
    unittest.main()
