"""Recovery metadata and durable recovery-state contracts (#350)."""

from __future__ import annotations

import unittest

from continuum.opencode_429_recovery import (
    METADATA_FILE_NAME,
    RECOVERY_METADATA_FAILED_MARKER,
    RECOVERY_METADATA_SCHEMA_VERSION,
    latest_recovery_state,
    metadata_failure_marker,
    parse_metadata_failure_line,
    parse_recovery_state_markers,
    recovery_state_marker,
    validate_recovery_metadata,
)

HEAD = "a" * 40
BASE = "b" * 40


def metadata(**overrides):
    data = {
        "schema_version": RECOVERY_METADATA_SCHEMA_VERSION,
        "operation_id": "opencode-429-issue-issue350",
        "failure_kind": "quota_429",
        "generation": 1,
        "stage": "implementation-incomplete",
        "mode": "issue",
        "issue_number": "350",
        "pr_number": "",
        "head_ref": "opencode/issue350-test",
        "base_sha": BASE,
        "head_sha": HEAD,
        "branch": "opencode/issue350-test",
        "checkpoint_ref": "opencode/429-checkpoint-opencode-429-issue-issue350-gen-1",
    }
    data.update(overrides)
    return data


class RecoveryMetadataTest(unittest.TestCase):
    def test_complete_metadata_is_accepted(self):
        ok, reason = validate_recovery_metadata(
            metadata(),
            operation_id="opencode-429-issue-issue350",
            generation=1,
            mode="issue",
            issue_number="350",
            stage="implementation-incomplete",
            head_sha=HEAD,
            branch="opencode/issue350-test",
        )
        self.assertTrue(ok, reason)

    def test_pointer_evidence_mismatch_is_rejected(self):
        for overrides, kwargs in (
            ({"operation_id": "other"}, {"operation_id": "opencode-429-issue-issue350"}),
            ({"generation": 2}, {"generation": 1}),
            ({"head_sha": "c" * 40}, {"head_sha": HEAD}),
            ({"branch": "other"}, {"branch": "opencode/issue350-test"}),
            ({"stage": "tests-passed"}, {"stage": "implementation-incomplete"}),
        ):
            ok, reason = validate_recovery_metadata(metadata(**overrides), **kwargs)
            self.assertFalse(ok, overrides)
            self.assertIn("does not match", reason)

    def test_incomplete_metadata_is_rejected(self):
        for field in (
            "operation_id",
            "failure_kind",
            "generation",
            "stage",
            "mode",
            "head_ref",
            "base_sha",
            "head_sha",
            "branch",
            "checkpoint_ref",
        ):
            data = metadata()
            data.pop(field)
            ok, reason = validate_recovery_metadata(data)
            self.assertFalse(ok, field)
            self.assertIn(field, reason)

    def test_checkpoint_ref_must_match_operation_and_generation(self):
        for overrides, reason in (
            ({"checkpoint_ref": "opencode/429-checkpoint-other-gen-1"}, "operation"),
            ({"checkpoint_ref": "opencode/429-checkpoint-opencode-429-issue-issue350-gen-2"}, "generation"),
            ({"checkpoint_ref": "not-a-checkpoint"}, "malformed"),
        ):
            ok, message = validate_recovery_metadata(metadata(**overrides))
            self.assertFalse(ok, overrides)
            self.assertIn(reason, message)

    def test_pointer_identity_can_be_cross_checked(self):
        ok, reason = validate_recovery_metadata(
            metadata(),
            checkpoint_ref="opencode/429-checkpoint-opencode-429-issue-issue350-gen-1",
        )
        self.assertTrue(ok, reason)
        ok, reason = validate_recovery_metadata(
            metadata(),
            checkpoint_ref="opencode/429-checkpoint-opencode-429-issue-issue350-gen-9",
        )
        self.assertFalse(ok)
        self.assertIn("checkpoint ref", reason)

    def test_infrastructure_checkpoint_namespace_is_accepted(self):
        ok, reason = validate_recovery_metadata(
            metadata(
                failure_kind="unavailable",
                checkpoint_ref="opencode/infra-checkpoint-opencode-429-issue-issue350-gen-1",
            ),
        )
        self.assertTrue(ok, reason)

    def test_unknown_mode_stage_and_failure_kind_are_rejected(self):
        for field, value in (
            ("mode", "unknown"),
            ("stage", "unknown"),
            ("failure_kind", "unknown"),
        ):
            ok, reason = validate_recovery_metadata(metadata(**{field: value}))
            self.assertFalse(ok, field)
            self.assertIn(field.replace("_", " "), reason)

    def test_metadata_file_name_is_stable(self):
        self.assertEqual(METADATA_FILE_NAME, ".continuum-429-recovery.json")


class RecoveryStateMarkerTest(unittest.TestCase):
    def test_round_trip_and_latest_wins(self):
        first = recovery_state_marker(
            "opencode-429-issue-issue350",
            1,
            "dispatch-failed",
            attempt=1,
            next_due=100,
            checkpoint_ref="refs/heads/ckpt",
            checkpoint_sha=HEAD,
            detail="forbidden",
        )
        second = recovery_state_marker(
            "opencode-429-issue-issue350",
            1,
            "observed",
            attempt=1,
            run_id="12345",
            checkpoint_ref="refs/heads/ckpt",
            checkpoint_sha=HEAD,
        )
        other = recovery_state_marker("other", 1, "claimed")
        states = parse_recovery_state_markers("\n".join([first, other, second]))
        self.assertEqual(len(states), 3)
        latest = latest_recovery_state(
            "\n".join([first, other, second]),
            "opencode-429-issue-issue350",
            1,
        )
        self.assertEqual(latest["state"], "observed")
        self.assertEqual(latest["run_id"], "12345")
        self.assertEqual(latest["checkpoint_sha"], HEAD)

    def test_empty_fields_round_trip_as_empty(self):
        marker = recovery_state_marker("op", 0, "claimed")
        state = parse_recovery_state_markers(marker)[0]
        self.assertEqual(state["checkpoint_ref"], "")
        self.assertEqual(state["run_id"], "")
        self.assertEqual(state["detail"], "")

    def test_metadata_failure_line_is_machine_readable(self):
        marker = metadata_failure_marker(
            "opencode-429-issue-issue350",
            1,
            reason="generation failed",
        )
        self.assertTrue(marker.startswith(RECOVERY_METADATA_FAILED_MARKER))
        parsed = parse_metadata_failure_line("noise\n" + marker + "\nmore noise")
        self.assertEqual(parsed["operation_id"], "opencode-429-issue-issue350")
        self.assertEqual(parsed["generation"], "1")
        self.assertEqual(parsed["reason"], "generation-failed")

    def test_malformed_marker_is_ignored(self):
        self.assertIsNone(parse_metadata_failure_line("CONTINUUM_OPENCODE_429_RECOVERY_METADATA_FAILED"))
        self.assertEqual(parse_recovery_state_markers("<-- continuum-429-recovery op=x -->"), [])


if __name__ == "__main__":
    unittest.main()
