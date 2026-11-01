"""Machine tests for the phase-A trigger-and-owner manifest (#343)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from continuum import trigger_manifest


ROOT = Path(__file__).resolve().parents[1]


class TriggerManifestTests(unittest.TestCase):
    def setUp(self):
        self.payload = trigger_manifest.load_manifest(ROOT / "src/continuum/trigger_owner_manifest.json")

    def test_manifest_diff_matches_every_workflow_and_installed_stub(self):
        self.assertEqual(trigger_manifest.validate_manifest(self.payload, root=ROOT), [])

    def test_every_required_owner_category_is_present(self):
        self.assertEqual(trigger_manifest.categories_covered(self.payload), [])
        required = [
            "issue-scheduler",
            "opencode-watchdog",
            "review-ready",
            "ci-repair",
            "coderabbit-queue",
            "coderabbit-unresolved",
            "pr-agent-review",
            "pr-agent-recovery",
            "pr-agent-merge",
            "global-watchdog",
            "qualification",
            "delegation",
            "validation",
        ]
        owner_ids = {owner["id"] for owner in self.payload["owners"]}
        for owner in required:
            self.assertIn(owner, owner_ids)

    def test_event_payload_dependencies_are_explicit_for_every_owner(self):
        for owner in self.payload["owners"]:
            self.assertIn("event_payload_dependencies", owner)
            self.assertIsInstance(owner["event_payload_dependencies"], list)
            self.assertTrue(owner["canonical_policy"])

    def test_installer_sets_are_derived_from_the_installed_layer(self):
        for surface in self.payload["surfaces"]:
            self.assertEqual(
                surface["installer_set"],
                trigger_manifest.expected_installer_set(surface["path"]),
                surface["path"],
            )

    def test_event_payload_dependent_paths_are_explicit(self):
        for surface in self.payload["surfaces"]:
            self.assertIsInstance(surface["event_payload_dependent"], bool)
            events = {str(trigger["event"]) for trigger in surface["triggers"]}
            expected = bool(events - {"schedule", "workflow_dispatch", "workflow_call"})
            self.assertEqual(surface["event_payload_dependent"], expected, surface["path"])

    def test_drift_in_an_actual_trigger_is_reported(self):
        drift = copy.deepcopy(self.payload)
        for surface in drift["surfaces"]:
            if surface["path"] == ".github/caller-stubs/continuum-issue-scheduler.yml":
                surface["triggers"] = [
                    trigger
                    for trigger in surface["triggers"]
                    if trigger["event"] != "schedule"
                ]
                break
        findings = trigger_manifest.validate_manifest(drift, root=ROOT)
        messages = [finding.message for finding in findings]
        self.assertTrue(any("unmanifested trigger" in message for message in messages))

    def test_missing_surface_is_reported(self):
        drift = copy.deepcopy(self.payload)
        drift["surfaces"] = [
            surface
            for surface in drift["surfaces"]
            if surface["path"] != ".github/caller-stubs/parent/continuum-child-worker.yml"
        ]
        findings = trigger_manifest.validate_manifest(drift, root=ROOT)
        self.assertTrue(
            any(
                "unmanifested workflow or caller stub" in finding.message
                for finding in findings
            )
        )

    def test_self_dogfood_jobs_have_explicit_owners(self):
        automation = next(
            surface
            for surface in self.payload["surfaces"]
            if surface["path"] == ".github/workflows/automation.yml"
        )
        self.assertEqual(
            automation["job_owners"],
            {
                "scheduler": "issue-scheduler",
                "watchdog": "opencode-watchdog",
                "repair": "ci-repair",
                "auto-merge": "auto-merge",
                "global-pr-watchdog": "global-watchdog",
            },
        )

    def test_manifest_is_json_serializable_and_contains_no_secret_values(self):
        text = json.dumps(self.payload)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("github_pat_", text)


if __name__ == "__main__":
    unittest.main()
