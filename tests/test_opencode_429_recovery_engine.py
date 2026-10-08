"""Unit tests for the OpenCode 429 runner-death engine (kodmial/continuum#290).

Covers classification (every agent mode, fail-closed errors, transient 403
separation), stable operation identity, checkpoint refs, stage resume/skip
decisions, budget separation, bounded infrastructure backoff without pause,
deduplication, Parent-log opacity, and recovery-input validation.
"""

import unittest

from continuum.opencode_429_recovery import (
    MODES_HONORING_STAGE_FILE,
    RECOVERY_IDENTITY_FIELDS,
    STAGE_FILE_NAME,
    already_resumed,
    checkpoint_ref,
    classify_agent_error,
    consumes_task_budget,
    cooldown_comment,
    infra_backoff_seconds,
    is_opencode_429,
    is_valid_stage,
    next_generation,
    operation_id,
    pack_recovery_identity,
    parse_checkpoint_ref,
    parse_stage_file,
    recovery_dispatch_inputs,
    redact_child_identity,
    resolve_evacuation_stage,
    resume_stage,
    resumption_dedupe_key,
    resumption_marker,
    should_enter_infra_cooldown,
    should_skip_opencode,
    stage_index,
    unpack_recovery_identity,
    validate_recovery_inputs,
    DEFAULT_MAX_429_RUNNER_RESTARTS,
    RUNNER_DEATH_EXIT_CODE,
)


class Classify429Test(unittest.TestCase):
    def test_free_usage_limit_always_classifies(self):
        self.assertTrue(is_opencode_429("FreeUsageLimitError: quota exceeded", 0))
        self.assertTrue(is_opencode_429("x FreeUsageLimitError y", 1))

    def test_429_signals_classify_only_on_failure(self):
        for log in (
            "APIError 429 too many requests",
            "HTTP 429 Too Many Requests",
            "statusCode: 429",
            "429 Too Many Requests",
        ):
            self.assertTrue(is_opencode_429(log, 1), log)
            self.assertFalse(is_opencode_429(log, 0), log)

    def test_fail_closed_errors_never_classify(self):
        for log in (
            "request failed with status 400",
            "401 unauthorized: bad credentials",
            "403 forbidden: missing scope",
            "404 not found",
            "422 unprocessable entity",
        ):
            self.assertFalse(is_opencode_429(log, 1), log)
            self.assertEqual(classify_agent_error(log, 1), "fail-closed", log)

    def test_normal_failure_is_not_runner_death(self):
        self.assertFalse(is_opencode_429("tests failed: 3 assertions", 1))
        self.assertEqual(
            classify_agent_error("tests failed: 3 assertions", 1), "task-failure"
        )
        self.assertEqual(classify_agent_error("", 0), "task-failure")

    def test_runner_death_exit_code_is_75(self):
        self.assertEqual(RUNNER_DEATH_EXIT_CODE, 75)


class StageResumeTest(unittest.TestCase):
    def test_stages_are_ordered(self):
        ordered = [
            "implementation-incomplete",
            "implementation-complete",
            "tests-running",
            "tests-passed",
            "publish-pr",
            "review-repair",
            "qualification",
        ]
        for stage in ordered:
            self.assertTrue(is_valid_stage(stage), stage)
        self.assertFalse(is_valid_stage("done"))
        indexes = [stage_index(stage) for stage in ordered]
        self.assertEqual(indexes, sorted(indexes))

    def test_tests_passed_with_evidence_skips_agent(self):
        self.assertTrue(should_skip_opencode("tests-passed", True))
        self.assertTrue(should_skip_opencode("publish-pr", True))

    def test_incomplete_stages_resume_agent(self):
        for stage in (
            "implementation-incomplete",
            "implementation-complete",
            "tests-running",
            "review-repair",
            "qualification",
        ):
            self.assertFalse(should_skip_opencode(stage, True), stage)
        # Evidence alone without a completed stage never skips the agent.
        self.assertFalse(should_skip_opencode("tests-passed", False))
        self.assertFalse(should_skip_opencode("implementation-incomplete", False))

    def test_unknown_stage_resumes_from_start(self):
        self.assertEqual(resume_stage("bogus"), "implementation-incomplete")
        self.assertEqual(resume_stage("review-repair"), "review-repair")


class EvacuationStageFileTest(unittest.TestCase):
    def test_stage_file_name_is_fixed(self):
        self.assertEqual(STAGE_FILE_NAME, ".continuum-429-stage")
        self.assertEqual(
            MODES_HONORING_STAGE_FILE, frozenset({"issue", "qualification"})
        )

    def test_parse_stage_file_honors_exact_stages_only(self):
        self.assertEqual(parse_stage_file("tests-passed\n"), "tests-passed")
        self.assertEqual(
            parse_stage_file("  publish-pr  \n"), "publish-pr"
        )
        self.assertIsNone(parse_stage_file(""))
        self.assertIsNone(parse_stage_file("   \n"))
        self.assertIsNone(parse_stage_file("done\n"))
        self.assertIsNone(parse_stage_file(None))
        self.assertIsNone(parse_stage_file(42))
        # Only the first line matters; garbage elsewhere is ignored only
        # when the first line itself is a known stage.
        self.assertEqual(
            parse_stage_file("tests-passed\ngarbage\n"), "tests-passed"
        )
        self.assertIsNone(parse_stage_file("garbage\ntests-passed\n"))

    def test_recorded_stage_wins_for_no_agent_path_modes(self):
        for mode in ("issue", "qualification"):
            self.assertEqual(
                resolve_evacuation_stage(
                    "implementation-incomplete", mode, "tests-passed\n"
                ),
                "tests-passed",
                mode,
            )

    def test_repair_modes_ignore_stage_file(self):
        for mode in ("coderabbit-fix", "resolve-conflict", "ci-fix", "future"):
            self.assertEqual(
                resolve_evacuation_stage(
                    "review-repair", mode, "tests-passed\n"
                ),
                "review-repair",
                mode,
            )

    def test_garbage_falls_back_to_normalized_default(self):
        self.assertEqual(
            resolve_evacuation_stage("review-repair", "issue", "bogus\n"),
            "review-repair",
        )
        self.assertEqual(
            resolve_evacuation_stage("bogus-default", "issue", None),
            "implementation-incomplete",
        )
        self.assertEqual(
            resolve_evacuation_stage("review-repair", "coderabbit-fix", None),
            "review-repair",
        )


class OperationIdentityTest(unittest.TestCase):
    def test_identity_is_stable_and_opaque(self):
        first = operation_id("issue", issue_number="290")
        second = operation_id("issue", issue_number="290")
        self.assertEqual(first, second)
        self.assertNotIn("/", first)
        self.assertIn("290", first)

    def test_repair_identity_carries_pr_head(self):
        identity = operation_id(
            "coderabbit-fix", pr_number="12", review_id="99"
        )
        self.assertIn("12", identity)
        self.assertIn("99", identity)
        self.assertNotIn("/", identity)

    def test_unknown_modes_fall_into_future_bucket(self):
        self.assertIn("future", operation_id("brand-new-agent", pr_number="7"))

    def test_checkpoint_ref_roundtrip(self):
        operation = operation_id("issue", issue_number="290")
        ref = checkpoint_ref(operation, 2)
        self.assertTrue(ref.startswith("opencode/429-checkpoint-"))
        parsed = parse_checkpoint_ref(ref)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["operation_id"], operation)
        self.assertEqual(parsed["generation"], 2)

    def test_malformed_refs_rejected(self):
        self.assertIsNone(parse_checkpoint_ref("main"))
        self.assertIsNone(parse_checkpoint_ref("opencode/429-checkpoint-"))
        self.assertIsNone(parse_checkpoint_ref("opencode/429-checkpoint-op-gen-x"))

    def test_next_generation_increments(self):
        self.assertEqual(next_generation(0), 1)
        self.assertEqual(next_generation(3), 4)
        self.assertEqual(next_generation("bad"), 1)


class DedupeTest(unittest.TestCase):
    def test_dedupe_key_is_stable(self):
        operation = operation_id("issue", issue_number="290")
        self.assertEqual(
            resumption_dedupe_key(operation, 1, "a" * 40),
            resumption_dedupe_key(operation, 1, "a" * 40),
        )
        self.assertNotEqual(
            resumption_dedupe_key(operation, 1, "a" * 40),
            resumption_dedupe_key(operation, 2, "a" * 40),
        )

    def test_duplicate_watchdog_events_detected(self):
        operation = operation_id("ci-fix", pr_number="44", run_id="1001")
        marker = resumption_marker(operation, 1)
        body = "some thread\n{}\nmore".format(marker)
        self.assertTrue(already_resumed(body, operation, 1))
        self.assertFalse(already_resumed(body, operation, 2))
        self.assertFalse(already_resumed("", operation, 1))


class BudgetAndBackoffTest(unittest.TestCase):
    def test_429_never_consumes_task_budgets(self):
        self.assertFalse(consumes_task_budget(True))
        self.assertTrue(consumes_task_budget(False))

    def test_backoff_grows_but_stays_bounded(self):
        self.assertEqual(infra_backoff_seconds(0), 0)
        first = infra_backoff_seconds(1)
        second = infra_backoff_seconds(2)
        self.assertGreater(second, first)
        self.assertEqual(infra_backoff_seconds(99), infra_backoff_seconds(100))

    def test_cooldown_only_after_fast_replacements_exhausted(self):
        self.assertFalse(
            should_enter_infra_cooldown(1, DEFAULT_MAX_429_RUNNER_RESTARTS)
        )
        self.assertFalse(
            should_enter_infra_cooldown(
                DEFAULT_MAX_429_RUNNER_RESTARTS, DEFAULT_MAX_429_RUNNER_RESTARTS
            )
        )
        self.assertTrue(
            should_enter_infra_cooldown(
                DEFAULT_MAX_429_RUNNER_RESTARTS + 1,
                DEFAULT_MAX_429_RUNNER_RESTARTS,
            )
        )

    def test_cooldown_never_pauses_and_needs_no_manual_command(self):
        comment = cooldown_comment("opencode-429-issue-issue290", 4, "ref")
        self.assertIn("ref", comment)
        # The comment records that no pause was applied; it never instructs
        # a manual retry.
        self.assertNotIn("Remove the `", comment)
        self.assertNotIn("post `/oc` to retry manually", comment)


class DispatchIdentityTest(unittest.TestCase):
    def test_recovery_dispatch_carries_explicit_identity(self):
        inputs = recovery_dispatch_inputs(
            operation="opencode-429-issue-issue290",
            checkpoint="opencode/429-checkpoint-op-gen-1",
            checkpoint_sha="b" * 40,
            stage="implementation-incomplete",
            generation=1,
            mode="issue",
            issue_number="290",
        )
        self.assertEqual(inputs["mode"], "issue")
        self.assertEqual(inputs["issue_number"], "290")
        self.assertEqual(
            inputs["recovery_operation_id"], "opencode-429-issue-issue290"
        )
        self.assertEqual(
            inputs["recovery_checkpoint_ref"],
            "opencode/429-checkpoint-op-gen-1",
        )
        self.assertEqual(inputs["recovery_checkpoint_sha"], "b" * 40)
        self.assertEqual(inputs["recovery_stage"], "implementation-incomplete")
        self.assertEqual(inputs["recovery_generation"], "1")

    def test_recovery_inputs_validate(self):
        operation = operation_id("ci-fix", pr_number="44", run_id="1001")
        ref = checkpoint_ref(operation, 1)
        ok, _ = validate_recovery_inputs(
            {
                "recovery_operation_id": operation,
                "recovery_checkpoint_ref": ref,
                "recovery_checkpoint_sha": "c" * 40,
                "recovery_stage": "review-repair",
                "recovery_generation": 1,
            }
        )
        self.assertTrue(ok)

    def test_mismatched_identity_rejected(self):
        operation = operation_id("issue", issue_number="290")
        ref = checkpoint_ref(operation, 1)
        ok, _ = validate_recovery_inputs(
            {
                "recovery_operation_id": operation_id("issue", issue_number="291"),
                "recovery_checkpoint_ref": ref,
                "recovery_stage": "implementation-incomplete",
                "recovery_generation": 1,
            }
        )
        self.assertFalse(ok)
        ok, _ = validate_recovery_inputs(
            {
                "recovery_operation_id": operation,
                "recovery_checkpoint_ref": ref,
                "recovery_stage": "finished",
                "recovery_generation": 1,
            }
        )
        self.assertFalse(ok)
        # No recovery requested is a valid plain run.
        ok, _ = validate_recovery_inputs({})
        self.assertTrue(ok)

    def test_recovery_identity_packs_into_one_dispatch_input(self):
        # A caller-stub workflow_dispatch accepts at most 25 inputs, so the
        # stub cannot declare one input per recovery field: the watchdog
        # packs the explicit identity into one JSON string and the stub
        # unpacks it. No field may be elided in transit.
        operation = operation_id("issue", issue_number="290")
        ref = checkpoint_ref(operation, 2)
        dispatch = recovery_dispatch_inputs(
            operation=operation,
            checkpoint=ref,
            checkpoint_sha="e" * 40,
            stage="tests-passed",
            generation=2,
            mode="issue",
            issue_number="290",
        )
        packed = pack_recovery_identity(dispatch)
        self.assertTrue(packed)
        unpacked = unpack_recovery_identity(packed)
        self.assertEqual(
            sorted(unpacked),
            sorted(
                [
                    "recovery_operation_id",
                    "recovery_checkpoint_ref",
                    "recovery_checkpoint_sha",
                    "recovery_stage",
                    "recovery_generation",
                ]
            ),
        )
        for field in (
            "recovery_operation_id",
            "recovery_checkpoint_ref",
            "recovery_checkpoint_sha",
            "recovery_stage",
            "recovery_generation",
        ):
            self.assertEqual(unpacked[field], dispatch[field])
        ok, _ = validate_recovery_inputs(unpacked)
        self.assertTrue(ok)

    def test_recovery_identity_pack_empty_means_no_recovery(self):
        self.assertEqual(pack_recovery_identity({}), "")
        self.assertEqual(
            unpack_recovery_identity(""),
            {field: "" for field in RECOVERY_IDENTITY_FIELDS},
        )
        ok, _ = validate_recovery_inputs(unpack_recovery_identity(""))
        self.assertTrue(ok)

    def test_recovery_identity_malformed_fails_closed(self):
        for bad in ("{not json", "[1,2]", "42", '"str"', 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                unpack_recovery_identity(bad)


class OpacityTest(unittest.TestCase):
    def test_child_repository_names_redacted(self):
        text = redact_child_identity("see private-child/repo-a PR #12 and abc123")
        self.assertNotIn("private-child/repo-a", text)
        self.assertIn("PR #12", text)

    def test_shas_and_numbers_pass_through(self):
        sha = "d" * 40
        text = redact_child_identity("checkpoint at {} for issue 290".format(sha))
        self.assertIn("290", text)


if __name__ == "__main__":
    unittest.main()
