"""Exact owning-step coverage for the TAP_PAT inventory (#432).

The detailed inventory suite depends on part-2 workflow fixtures, so this
module pins the coverage predicate directly against the engine.
"""

from __future__ import annotations

import unittest

from continuum import tap_pat_inventory


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

    def test_attestation_key_is_an_allowed_secret(self):
        self.assertIn(
            "CONTINUUM_VERIFICATION_ATTESTATION_KEY",
            tap_pat_inventory.ALLOWED_SECRETS,
        )


if __name__ == "__main__":
    unittest.main()
