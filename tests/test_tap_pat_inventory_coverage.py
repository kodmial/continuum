"""Exact owning-step coverage for the TAP_PAT inventory (#432).

The detailed inventory suite depends on part-2 workflow fixtures, so this
module pins the coverage predicate directly against the engine.
"""

from __future__ import annotations

import unittest

from continuum import credential_matrix
from continuum import tap_pat_inventory

try:
    from tests.scheduler_workflow import read_workflow
except ModuleNotFoundError:
    from scheduler_workflow import read_workflow


class ExactStepCoverageTest(unittest.TestCase):
    def setUp(self):
        self.entry = tap_pat_inventory.ENTRIES[0]
        self.workflow = self.entry["workflow"]
        self.step = self.entry["step"]

    def test_exact_owning_step_is_covered(self):
        self.assertEqual(
            tap_pat_inventory.covering_entry(self.workflow, self.step),
            self.entry,
        )

    def test_longer_step_name_is_not_covered(self):
        # Substring coverage used to let a shorter entry claim a later,
        # different step, so that binding escaped the inventory.
        for variant in (
            self.step + " verifier",
            "Run " + self.step,
            self.step + " (retry)",
        ):
            with self.subTest(step=variant):
                self.assertIsNone(
                    tap_pat_inventory.covering_entry(self.workflow, variant)
                )

    def test_whitespace_and_case_are_normalized(self):
        padded = "  " + "   ".join(self.step.upper().split()) + "  "
        self.assertEqual(
            tap_pat_inventory.covering_entry(self.workflow, padded),
            self.entry,
        )

    def test_a_different_workflow_is_not_covered(self):
        self.assertIsNone(
            tap_pat_inventory.covering_entry(
                "continuum-some-other-workflow.yml", self.step
            )
        )

    def test_attestation_key_is_scoped_to_the_file_based_contract(self):
        # The optional key is a permitted workflow secret, but it must only be
        # read into a non-exported shell variable and provisioned as a private
        # key file; the value form is rejected by the verification engine.
        self.assertIn(
            "CONTINUUM_VERIFICATION_ATTESTATION_KEY",
            credential_matrix.ALLOWED_SECRETS,
        )
        body = read_workflow("continuum-opencode.yml")
        self.assertIn(
            'CONTINUUM_ATTESTATION_KEY_VALUE="${CONTINUUM_VERIFICATION_ATTESTATION_KEY:-}"',
            body,
        )
        self.assertIn("unset CONTINUUM_VERIFICATION_ATTESTATION_KEY", body)
        self.assertIn(
            "python3 -m continuum.verification provision-key --repo \"$GITHUB_WORKSPACE\"",
            body,
        )
        self.assertNotIn(
            'CONTINUUM_VERIFICATION_ATTESTATION_KEY="$CONTINUUM_ATTESTATION_KEY_VALUE"',
            body,
        )

    def test_allowlist_derives_from_the_credential_matrix(self):
        # The inventory allow-list is the credential matrix's set plus the
        # automatic github.token; a hand-maintained copy already drifted once.
        self.assertEqual(
            tap_pat_inventory.ALLOWED_SECRETS,
            frozenset(credential_matrix.ALLOWED_SECRETS | {"GITHUB_TOKEN"}),
        )


if __name__ == "__main__":
    unittest.main()
