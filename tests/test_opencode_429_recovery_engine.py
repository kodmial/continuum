"""Unit tests for the OpenCode 429 runner-death engine (kodmial/continuum#290).

Covers classification (every agent mode, fail-closed errors, transient 403
separation), stable operation identity, checkpoint refs, stage resume/skip
decisions, budget separation, bounded infrastructure backoff without pause,
deduplication, Parent-log opacity, and recovery-input validation.
"""

import json
import unittest

from continuum.opencode_429_recovery import (
    MODES_HONORING_STAGE_FILE,
    RECOVERY_IDENTITY_FIELDS,
    STAGE_FILE_NAME,
    already_resumed,
    checkpoint_ref,
    classify_agent_error,
    classify_opencode_events,
    classify_opencode_github_events,
    consumes_task_budget,
    cooldown_comment,
    infra_backoff_seconds,
    is_opencode_429,
    is_valid_stage,
    next_generation,
    operation_id,
    pack_recovery_identity,
    parse_checkpoint_ref,
    parse_opencode_error_events,
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


def error_event(name=None, message=None, status=None, response_body=None):
    data = {}
    if message is not None:
        data["message"] = message
    if status is not None:
        data["statusCode"] = status
    if response_body is not None:
        data["responseBody"] = response_body
    return json.dumps(
        {
            "type": "error",
            "timestamp": 1,
            "sessionID": "ses_test",
            "error": {"name": name or "APIError", "data": data},
        }
    )


def text_event(text):
    return json.dumps(
        {
            "type": "text",
            "timestamp": 1,
            "sessionID": "ses_test",
            "part": {"type": "text", "text": text},
        }
    )


def tool_event(output):
    return json.dumps(
        {
            "type": "tool_use",
            "timestamp": 1,
            "sessionID": "ses_test",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {"status": "completed", "output": output, "title": "pytest"},
            },
        }
    )


def github_error_block(name="APIError", message="", status=None, prefix="Agent error:"):
    lines = [prefix + " {", "  name: '{}',".format(name), "  data: {"]
    if message:
        lines.append("    message: '{}',".format(message))
    if status is not None:
        lines.append("    statusCode: {},".format(status))
    lines += ["  }", "}"]
    return "\n".join(lines)


class Classify429Test(unittest.TestCase):
    def test_structured_429_classifies_regardless_of_exit(self):
        for log in (
            error_event("APIError", "too many requests", 429),
            error_event("FreeUsageLimitError", "free usage limit reached"),
            error_event(
                "APIError",
                response_body='{"type":"error","error":{"type":"FreeUsageLimitError"}}',
            ),
        ):
            with self.subTest(log=log):
                self.assertTrue(is_opencode_429(log, 0))
                self.assertTrue(is_opencode_429(log, 1))
                self.assertEqual(classify_agent_error(log, 0), "runner-death-429")
                self.assertEqual(classify_agent_error(log, 1), "runner-death-429")

    def test_pinned_cli_production_error_event_shape_is_classified(self):
        # Captured from OpenCode 1.19.3 ``run --format json`` against a fake
        # OpenAI-compatible endpoint returning HTTP 429 (see the pinned CLI's
        # session.error emitter). This locks the real event envelope rather
        # than only the minimal synthetic fixture.
        log = json.dumps(
            {
                "type": "error",
                "timestamp": 1792765550447,
                "sessionID": "ses_e96d9ba3bffe7tnMMH8RFy32DJ",
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": "Free usage limit reached",
                        "statusCode": 429,
                        "isRetryable": True,
                        "responseHeaders": {
                            "content-length": "109",
                            "content-type": "application/json",
                            "date": "Fri, 23 Oct 2026 14:25:50 GMT",
                            "server": "BaseHTTP/0.6 Python/3.12.3",
                        },
                        "responseBody": (
                            '{"error": {"message": "Free usage limit reached", '
                            '"type": "FreeUsageLimitError", "code": '
                            '"free_usage_limit"}}'
                        ),
                        "metadata": {
                            "url": "http://127.0.0.1:18765/v1/chat/completions"
                        },
                    },
                },
            }
        )
        self.assertEqual(parse_opencode_error_events(log)[0]["type"], "error")
        self.assertEqual(classify_opencode_events(log, 1), "quota_429")
        self.assertTrue(is_opencode_429(log, 1))
        self.assertEqual(classify_agent_error(log, 1), "runner-death-429")

    def test_arbitrary_transcript_text_never_classifies(self):
        for log in (
            "FreeUsageLimitError: quota exceeded",
            'self.assertTrue(is_opencode_429("FreeUsageLimitError: quota exceeded", 0))',
            "APIError 429 too many requests",
            "HTTP 429 Too Many Requests",
            "429 Too Many Requests",
            text_event('return 1, "ERROR FreeUsageLimitError: free usage limit reached"'),
            text_event("FAILED test_429: HTTP 429 Too Many Requests"),
            tool_event('return 1, "ERROR FreeUsageLimitError: free usage limit reached"'),
            tool_event("APIError 429 Too Many Requests"),
        ):
            with self.subTest(log=log):
                self.assertFalse(is_opencode_429(log, 0))
                self.assertFalse(is_opencode_429(log, 1))
                self.assertEqual(
                    classify_agent_error(log, 1), "task-failure"
                )

    def test_structured_fail_closed_errors_never_classify(self):
        for status, name in (
            (400, "BadRequestError"),
            (401, "AuthenticationError"),
            (403, "PermissionDeniedError"),
            (404, "NotFoundError"),
            (422, "UnprocessableEntityError"),
        ):
            log = error_event(
                name,
                'provider rejected the request; transcript quoted "429" and '
                "FreeUsageLimitError",
                status,
            )
            with self.subTest(status=status):
                self.assertFalse(is_opencode_429(log, 1), log)
                self.assertEqual(classify_agent_error(log, 1), "fail-closed", log)

    def test_structured_upstream_failures_are_not_quota(self):
        cases = (
            (error_event("APIError", 'quoted "429" in review text', 503), "unavailable"),
            (error_event("APIError", "bad gateway", 502), "upstream"),
            (error_event("APIError", "internal server error", 500), "upstream"),
            (error_event("APIError", "gateway timeout", 504), "upstream"),
            (error_event("APITimeoutError", "Request timed out"), "timeout"),
            (error_event("APIError", "request timeout", 408), "timeout"),
        )
        for log, expected in cases:
            with self.subTest(log=log):
                self.assertFalse(is_opencode_429(log, 1))
                self.assertEqual(
                    classify_agent_error(log, 1), "infrastructure-unavailable"
                )
                self.assertEqual(classify_opencode_events(log, 1), expected)

    def test_plain_text_fail_closed_remains_fail_closed(self):
        for log in (
            "request failed with status 400",
            "401 unauthorized: bad credentials",
            "403 forbidden: missing scope",
            "404 not found",
            "422 unprocessable entity",
        ):
            with self.subTest(log=log):
                self.assertFalse(is_opencode_429(log, 1), log)
                self.assertEqual(classify_agent_error(log, 1), "fail-closed", log)

    def test_transcript_words_without_status_are_not_fail_closed(self):
        # Only authenticated provider error objects may use the word tokens;
        # ordinary task prose that merely mentions auth vocabulary stays a
        # task failure and never changes recovery routing.
        for log in (
            "the agent discussed an unauthorized API request",
            "bad credentials were mentioned in the review",
            "this path is forbidden by the repository policy",
            "the token is missing scope documentation",
        ):
            with self.subTest(log=log):
                self.assertEqual(classify_agent_error(log, 1), "task-failure")

    def test_event_quoting_429_without_authenticated_status_is_not_quota(self):
        log = error_event("AssertionError", 'expected "429" in the report')
        self.assertFalse(is_opencode_429(log, 1))
        self.assertEqual(classify_opencode_events(log, 1), "provider_error")
        self.assertEqual(classify_agent_error(log, 1), "task-failure")

    def test_malformed_and_truncated_events_are_inconclusive(self):
        for log in (
            '{"type":"error", not-json',
            '{"type":"error","error":{"name":"APIError"',
            "[1, 2, 3]",
            '{"type":"error","error":"FreeUsageLimitError"}',
            '{"type":"tool_use","part":{"state":{"output":"FreeUsageLimitError"}}}',
        ):
            with self.subTest(log=log):
                self.assertFalse(is_opencode_429(log, 1), log)
                self.assertEqual(
                    classify_agent_error(log, 1), "task-failure", log
                )
                self.assertEqual(
                    classify_opencode_events(log, 1), "inconclusive", log
                )

    def test_empty_log_preserves_success_and_failure(self):
        self.assertEqual(classify_opencode_events("", 0), "ok")
        self.assertEqual(classify_opencode_events("", 1), "inconclusive")
        self.assertEqual(classify_agent_error("", 0), "task-failure")
        self.assertEqual(classify_agent_error("", 1), "task-failure")

    def test_duplicate_quota_events_remain_quota(self):
        log = error_event("APIError", "429 Too Many Requests", 429) + "\n" + error_event(
            "APIError", "429 Too Many Requests", 429
        )
        self.assertTrue(is_opencode_429(log, 1))

    def test_github_run_json_inspect_channel_is_classified(self):
        block = (
            'Agent error: {"name":"APIError","data":{"message":"quota",'
            '"statusCode":429}}\n'
        )
        self.assertEqual(
            classify_opencode_github_events(block, 1), "quota_429"
        )
        block = (
            'Agent error: {"name":"AuthenticationError",'
            '"data":{"statusCode":401}}\n'
        )
        self.assertEqual(
            classify_opencode_github_events(block, 1), "fail_closed"
        )

    def test_github_run_channel_is_covered_by_generic_helpers(self):
        quota = github_error_block(status=429)
        self.assertTrue(is_opencode_429(quota, 1))
        self.assertEqual(classify_agent_error(quota, 1), "runner-death-429")
        auth = github_error_block("AuthenticationError", "unauthorized", 401)
        self.assertFalse(is_opencode_429(auth, 1))
        self.assertEqual(classify_agent_error(auth, 1), "fail-closed")

    def test_github_run_stderr_error_block_is_classified(self):
        self.assertEqual(
            classify_opencode_github_events(github_error_block(status=429), 1),
            "quota_429",
        )
        self.assertEqual(
            classify_opencode_github_events(github_error_block(status=0), 0),
            "provider_error",
        )

    def test_github_run_free_usage_and_auth_are_separated(self):
        quota = github_error_block(
            message="FreeUsageLimitError: free usage limit reached"
        )
        self.assertEqual(classify_opencode_github_events(quota, 1), "quota_429")
        auth = github_error_block(
            "AuthenticationError", 'status 401; transcript quoted "429"', 401
        )
        self.assertEqual(classify_opencode_github_events(auth, 1), "fail_closed")
        upstream = github_error_block(
            "APIError", 'status 503; transcript quoted "429"', 503
        )
        self.assertEqual(
            classify_opencode_github_events(upstream, 1), "unavailable"
        )

    def test_github_run_summary_error_and_unprovable_output(self):
        summary = github_error_block(
            "APIError", "quota exceeded", 429, prefix="Summary agent error:"
        )
        self.assertEqual(
            classify_opencode_github_events(summary, 1), "quota_429"
        )
        self.assertEqual(
            classify_opencode_github_events("plain task failure\n", 1),
            "inconclusive",
        )
        self.assertEqual(
            classify_opencode_github_events(
                "Response: Agent error: statusCode: 429\n", 0
            ),
            "ok",
        )

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

    def test_infrastructure_checkpoint_ref_roundtrip(self):
        ref = "opencode/infra-checkpoint-opencode-429-issue-issue290-gen-3"
        parsed = parse_checkpoint_ref(ref)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["operation_id"], "opencode-429-issue-issue290")
        self.assertEqual(parsed["generation"], 3)

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
