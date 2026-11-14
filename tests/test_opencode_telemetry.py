"""Deterministic tests for bounded OpenCode per-attempt telemetry (#454).

The pinned OpenCode v2.2.6 ``run --format json`` stream is the only evidence
source. These tests pin the schema interpretation, the privacy allowlist, the
durable-artifact contract, and classification parity with the existing 429
engine using sanitized event streams. They never require a provider or a
network connection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuum.opencode_429_recovery import (
    classify_opencode_error_event,
    classify_opencode_events,
    describe_opencode_error,
    parse_opencode_error_events,
)
from continuum.opencode_telemetry import (
    MAX_RETRIES,
    MAX_STEPS,
    UNKNOWN,
    build_provenance,
    format_human_summary,
    summarize_opencode_log,
    write_summary,
)

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
CLASSIFIER = ROOT / ".github" / "scripts" / "opencode_429_classify.py"
WRAPPER = ROOT / ".github" / "scripts" / "opencode_429_wrapper.sh"
OPENCODE_WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
REPAIR_WORKFLOW = ROOT / ".github" / "workflows" / "continuum-pr-agent-repair.yml"

BASE_TS = 1_700_000_000_000
BASE_ISO = "2023-11-14T22:13:20.000Z"


def event(kind, timestamp=BASE_TS, session="ses_test", **fields):
    payload = {"type": kind, "timestamp": timestamp, "sessionID": session}
    payload.update(fields)
    return json.dumps(payload) + "\n"


def start(timestamp=BASE_TS, session="ses_test", message="msg_1"):
    return event(
        "step_start",
        timestamp,
        session,
        part={"type": "step_start", "messageID": message},
    )


def finish(
    timestamp=BASE_TS,
    session="ses_test",
    message="msg_1",
    tokens=None,
    cost=None,
    reason="stop",
):
    part = {"type": "step_finish", "messageID": message, "reason": reason}
    if tokens is not None:
        part["tokens"] = tokens
    if cost is not None:
        part["cost"] = cost
    return event("step_finish", timestamp, session, part=part)


def tool(call_id, name="read", status="running", timestamp=BASE_TS, session="ses_test"):
    part = {"type": "tool", "tool": name, "state": {"status": status}}
    if call_id is not None:
        part["id"] = call_id
    return event("tool_use", timestamp, session, part=part)


def typed_error(name, status=None, message="", timestamp=BASE_TS, session="ses_test", **extra):
    payload = {"type": name}
    if status is not None:
        payload["statusCode"] = status
    if message:
        payload["message"] = message
    payload.update(extra)
    return event("error", timestamp, session, error=payload)


def summarize(lines, **kwargs):
    return summarize_opencode_log("".join(lines), **kwargs)


class TelemetryAggregationTests(unittest.TestCase):
    def test_duplicate_tool_state_updates_count_unique_calls_not_requests(self):
        lines = [
            tool("call_1", "read", "running", BASE_TS + index)
            for index in range(319)
        ]
        lines.append(tool("call_1", "read", "completed", BASE_TS + 319))
        lines.append(tool(None, "bash", "completed"))
        lines.append(tool(None, "bash", "completed"))

        summary = summarize(lines)
        tools = summary["tools"]

        self.assertEqual(tools["events"], 322)
        self.assertEqual(tools["unique_calls"], 1)
        self.assertEqual(tools["duplicate_events"], 319)
        self.assertEqual(tools["missing_id_events"], 2)
        self.assertEqual(tools["status"]["completed"], 1)
        self.assertEqual(tools["status"]["running"], 0)
        self.assertEqual(
            tools["names"], [{"name": "read", "unique_calls": 1}]
        )
        # A tool state update is not an HTTP request and never becomes one.
        self.assertEqual(summary["requests"]["http_requests"], UNKNOWN)
        self.assertEqual(summary["requests"]["measurement"], "unavailable")
        self.assertEqual(summary["requests"]["step_finish_events"], 0)
        self.assertEqual(summary["requests"]["retry_events"], 0)

    def test_tool_calls_are_unique_by_stable_call_id_across_sessions(self):
        lines = [
            tool("call_shared", "read", "running", session="ses_a"),
            tool("call_shared", "read", "completed", session="ses_b"),
            tool("call_other", "bash", "completed", session="ses_b"),
        ]

        summary = summarize(lines)

        self.assertEqual(summary["sessions"]["count"], 2)
        self.assertEqual(summary["tools"]["events"], 3)
        self.assertEqual(summary["tools"]["unique_calls"], 2)
        self.assertEqual(summary["tools"]["duplicate_events"], 1)
        self.assertEqual(
            summary["tools"]["names"],
            [
                {"name": "bash", "unique_calls": 1},
                {"name": "read", "unique_calls": 1},
            ],
        )

    def test_usage_full_partial_and_missing_are_not_interpolated(self):
        lines = [
            start(BASE_TS, message="msg_full"),
            finish(
                BASE_TS + 50,
                message="msg_full",
                tokens={
                    "input": 100,
                    "output": 20,
                    "reasoning": 5,
                    "cache": {"read": 30, "write": 10},
                },
                cost=0.0025,
                reason="tool-calls",
            ),
            start(BASE_TS + 60, message="msg_partial"),
            finish(
                BASE_TS + 80,
                message="msg_partial",
                tokens={"input": 200},
                reason="tool-calls",
            ),
            start(BASE_TS + 90, message="msg_missing"),
            finish(BASE_TS + 95, message="msg_missing", reason="stop"),
        ]

        usage = summarize(lines)["usage"]

        self.assertEqual(usage["steps"], 3)
        self.assertEqual(usage["steps_with_usage"], 2)
        self.assertEqual(usage["steps_without_usage"], 1)
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["cache_read_tokens"], 30)
        self.assertEqual(usage["cache_write_tokens"], 10)
        self.assertAlmostEqual(usage["cost"], 0.0025)
        self.assertEqual(usage["cost_known_steps"], 1)
        self.assertFalse(usage["cost_complete"])
        self.assertEqual(usage["context_input_tokens_max"], 200)
        self.assertEqual(usage["missing_fields"], [])
        self.assertEqual(
            usage["partial_fields"],
            [
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ],
        )

        steps = summarize(lines)["steps"]["items"]
        self.assertEqual(steps[0]["reason"], "tool-calls")
        self.assertEqual(steps[0]["cache_read_tokens"], 30)
        self.assertEqual(steps[0]["elapsed_ms"], 50)
        self.assertEqual(steps[0]["started_at"], BASE_ISO)
        # Missing metrics stay unknown; they are never inferred from a peer.
        self.assertEqual(steps[1]["output_tokens"], UNKNOWN)
        self.assertEqual(steps[1]["cost"], UNKNOWN)
        self.assertFalse(steps[2]["usage_present"])
        self.assertEqual(steps[2]["input_tokens"], UNKNOWN)
        self.assertEqual(steps[2]["elapsed_ms"], 5)

    def test_missing_usage_everywhere_reports_unknown_and_missing_fields(self):
        usage = summarize(
            [
                finish(BASE_TS, message="msg_a"),
                finish(BASE_TS + 10, message="msg_b"),
            ]
        )["usage"]

        self.assertEqual(usage["steps"], 2)
        self.assertEqual(usage["steps_with_usage"], 0)
        self.assertEqual(usage["steps_without_usage"], 2)
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["input_tokens"], UNKNOWN)
        self.assertEqual(usage["output_tokens"], UNKNOWN)
        self.assertEqual(usage["reasoning_tokens"], UNKNOWN)
        self.assertEqual(usage["cache_read_tokens"], UNKNOWN)
        self.assertEqual(usage["cache_write_tokens"], UNKNOWN)
        self.assertEqual(usage["cost"], UNKNOWN)
        self.assertEqual(usage["context_input_tokens_max"], UNKNOWN)
        self.assertEqual(
            usage["missing_fields"],
            [
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ],
        )
        self.assertEqual(usage["partial_fields"], [])

    def test_multiple_sessions_and_step_elapsed_are_paired(self):
        lines = [
            start(BASE_TS, session="ses_a", message="msg_a"),
            start(BASE_TS + 100, session="ses_b", message="msg_b"),
            finish(
                BASE_TS + 500,
                session="ses_b",
                message="msg_b",
                tokens={"input": 10},
            ),
            finish(
                BASE_TS + 2000,
                session="ses_a",
                message="msg_a",
                tokens={"input": 20},
            ),
        ]

        summary = summarize(lines)
        steps = summary["steps"]["items"]

        self.assertEqual(summary["sessions"]["count"], 2)
        self.assertEqual(summary["sessions"]["ids"], ["ses_a", "ses_b"])
        self.assertFalse(summary["sessions"]["ids_truncated"])
        self.assertEqual(steps[0]["session_id"], "ses_b")
        self.assertEqual(steps[0]["elapsed_ms"], 400)
        self.assertEqual(steps[1]["session_id"], "ses_a")
        self.assertEqual(steps[1]["elapsed_ms"], 2000)
        self.assertEqual(summary["events"]["step_start"], 2)
        self.assertEqual(summary["events"]["step_finish"], 2)
        self.assertEqual(summary["attempt"]["elapsed_ms"], 2000)
        self.assertEqual(summary["attempt"]["first_event_at"], BASE_ISO)
        self.assertEqual(
            summary["attempt"]["last_step_at"], "2023-11-14T22:13:22.000Z"
        )
        self.assertEqual(summary["usage"]["input_tokens"], 30)

    def test_retries_are_separate_and_requests_never_inferred(self):
        lines = [
            finish(BASE_TS, tokens={"input": 1}),
            event("retry", BASE_TS + 10, attempt=1),
            event("retry", BASE_TS + 20, attempt=1),
            event("retry", BASE_TS + 30, attempt=2),
            event("retry", BASE_TS + 40, attempt=3),
        ]

        summary = summarize(lines)

        self.assertEqual(summary["retries"]["events"], 4)
        self.assertEqual(summary["retries"]["max_attempt"], 3)
        self.assertEqual(
            summary["retries"]["first_at"], "2023-11-14T22:13:20.010Z"
        )
        self.assertEqual(
            summary["retries"]["last_at"], "2023-11-14T22:13:20.040Z"
        )
        self.assertFalse(summary["retries"]["attempts_truncated"])
        self.assertEqual(summary["requests"]["http_requests"], UNKNOWN)
        self.assertEqual(summary["requests"]["step_finish_events"], 1)
        self.assertEqual(summary["requests"]["retry_events"], 4)
        self.assertIn("not proof", summary["requests"]["note"])

    def test_retry_attempts_are_bounded_without_losing_first_last_or_max(self):
        total = MAX_RETRIES + 3
        lines = [
            event("retry", BASE_TS + index, attempt=index + 1)
            for index in range(total)
        ]

        retries = summarize(lines)["retries"]

        self.assertEqual(retries["events"], total)
        self.assertEqual(retries["max_attempt"], total)
        self.assertEqual(retries["first_at"], BASE_ISO)
        self.assertEqual(
            retries["last_at"],
            "2023-11-14T22:13:20.{}Z".format(total - 1),
        )
        self.assertTrue(retries["attempts_truncated"])
        # Repeated attempt numbers are duplicates, not truncation.
        repeated = [event("retry", BASE_TS, attempt=1) for _ in range(total)]
        self.assertFalse(
            summarize(repeated)["retries"]["attempts_truncated"]
        )

    def test_step_records_are_bounded_but_totals_remain_complete(self):
        total_steps = MAX_STEPS + 7
        lines = [
            finish(
                BASE_TS + index,
                message="msg_{}".format(index),
                tokens={"input": 1},
            )
            for index in range(total_steps)
        ]

        steps = summarize(lines)["steps"]

        self.assertEqual(steps["count"], total_steps)
        self.assertTrue(steps["truncated"])
        self.assertEqual(steps["omitted"], 7)
        self.assertEqual(len(steps["items"]), MAX_STEPS)
        self.assertEqual(steps["items"][-1]["index"], MAX_STEPS)
        self.assertEqual(summarize(lines)["usage"]["input_tokens"], total_steps)

    def test_v2_envelope_snapshots_are_not_stored_and_full_usage_is_complete(self):
        # Exact sanitized 2.2.6 envelope shape: both step records carry
        # part.id/sessionID/messageID/type, and snapshots are user-visible
        # state that telemetry must never persist.
        lines = [
            event(
                "step_start",
                BASE_TS,
                session="ses_v2",
                part={
                    "id": "prt_step",
                    "sessionID": "ses_v2",
                    "messageID": "msg_v2",
                    "type": "step_start",
                    "snapshot": {"start": "SECRET_START_SNAPSHOT"},
                },
            ),
            event(
                "step_finish",
                BASE_TS + 25,
                session="ses_v2",
                part={
                    "id": "prt_step",
                    "sessionID": "ses_v2",
                    "messageID": "msg_v2",
                    "type": "step-finish",
                    "reason": "tool-calls",
                    "snapshot": {"end": "SECRET_END_SNAPSHOT"},
                    "cost": 0.001,
                    "tokens": {
                        "input": 10,
                        "output": 2,
                        "reasoning": 1,
                        "cache": {"read": 3, "write": 4},
                    },
                },
            ),
        ]

        summary = summarize(lines, exit_code=0)

        self.assertEqual(summary["usage"]["steps"], 1)
        self.assertTrue(summary["usage"]["complete"])
        self.assertTrue(summary["usage"]["cost_complete"])
        self.assertEqual(summary["usage"]["input_tokens"], 10)
        self.assertEqual(summary["usage"]["output_tokens"], 2)
        self.assertEqual(summary["usage"]["reasoning_tokens"], 1)
        self.assertEqual(summary["usage"]["cache_read_tokens"], 3)
        self.assertEqual(summary["usage"]["cache_write_tokens"], 4)
        self.assertEqual(summary["steps"]["items"][0]["elapsed_ms"], 25)
        raw = json.dumps(summary, sort_keys=True)
        self.assertNotIn("SECRET_START_SNAPSHOT", raw)
        self.assertNotIn("SECRET_END_SNAPSHOT", raw)
        self.assertNotIn('"snapshot"', raw)


class TelemetryClassificationTests(unittest.TestCase):
    def test_top_level_provider_429_is_recorded_from_actual_event_only(self):
        lines = [
            event(
                "text",
                part={"type": "text", "text": "provider returned 429 Too Many Requests"},
            ),
            tool("call_quoted", "websearch", "error"),
            typed_error(
                "APIError",
                status=429,
                message="quoted statusCode: 429 in provider prose",
                timestamp=BASE_TS + 500,
                requestID="req_abc123",
                attempt=3,
            ),
        ]

        summary = summarize(lines, exit_code=1)
        provider = summary["provider_error"]

        self.assertEqual(summary["attempt"]["classification"], "quota_429")
        self.assertEqual(summary["attempt"]["computed_classification"], "quota_429")
        self.assertTrue(summary["attempt"]["classification_match"])
        self.assertEqual(provider["classification"], "quota_429")
        self.assertEqual(provider["typed_provider_error_events"], 1)
        self.assertEqual(provider["non_provider_error_events"], 0)
        self.assertEqual(provider["terminal_error_events"], 1)
        self.assertEqual(provider["kind"], "APIError")
        self.assertEqual(provider["http_status"], 429)
        self.assertEqual(
            provider["first_provider_429_at"], "2023-11-14T22:13:20.500Z"
        )
        self.assertEqual(
            provider["last_provider_error_at"], "2023-11-14T22:13:20.500Z"
        )
        self.assertEqual(provider["attempt"], 3)
        self.assertEqual(provider["correlation_id"], "req_abc123")

    def test_quoted_429_prose_and_tool_error_are_not_provider_evidence(self):
        lines = [
            event(
                "text",
                part={"type": "text", "text": "the server said 429 but this is prose"},
            ),
            tool("call_1", "bash", "error"),
        ]

        summary = summarize(lines, exit_code=0)
        provider = summary["provider_error"]

        self.assertEqual(summary["attempt"]["classification"], "ok")
        self.assertEqual(summary["attempt"]["computed_classification"], "ok")
        self.assertEqual(provider["classification"], "none")
        self.assertEqual(provider["typed_provider_error_events"], 0)
        self.assertEqual(provider["terminal_error_events"], 0)
        self.assertEqual(provider["first_provider_429_at"], UNKNOWN)

    def test_422_web_search_and_tool_errors_stay_separate(self):
        lines = [
            tool("call_search", "websearch", "error"),
            typed_error(
                "APIError",
                status=422,
                message="request failed with unprocessable entity",
                timestamp=BASE_TS + 10,
            ),
            typed_error(
                "MessageAbortedError",
                message="Web Search cancelled by user",
                timestamp=BASE_TS + 20,
            ),
        ]

        summary = summarize(lines, exit_code=1)
        provider = summary["provider_error"]

        self.assertEqual(summary["attempt"]["classification"], "fail_closed")
        self.assertNotEqual(summary["attempt"]["classification"], "quota_429")
        self.assertEqual(provider["classification"], "fail_closed")
        self.assertEqual(provider["typed_provider_error_events"], 1)
        self.assertEqual(provider["non_provider_error_events"], 1)
        self.assertEqual(provider["terminal_error_events"], 2)
        self.assertEqual(provider["http_status"], 422)
        self.assertEqual(provider["first_provider_429_at"], UNKNOWN)

    def test_v2_rate_limit_type_with_http_429_is_typed_quota(self):
        summary = summarize(
            [typed_error("RateLimit", status=429, timestamp=BASE_TS + 7)],
            exit_code=1,
        )

        provider = summary["provider_error"]
        self.assertEqual(summary["attempt"]["classification"], "quota_429")
        self.assertEqual(provider["classification"], "quota_429")
        self.assertEqual(provider["kind"], "RateLimit")
        self.assertEqual(provider["http_status"], 429)
        self.assertEqual(
            provider["first_provider_429_at"], "2023-11-14T22:13:20.007Z"
        )

    def test_provider_error_metadata_reflects_the_last_typed_event(self):
        lines = [
            typed_error("APIError", status=503, timestamp=BASE_TS),
            typed_error("APITimeoutError", timestamp=BASE_TS + 25),
        ]

        provider = summarize(lines, exit_code=1)["provider_error"]

        self.assertEqual(provider["classification"], "unavailable")
        self.assertEqual(provider["typed_provider_error_events"], 2)
        self.assertEqual(provider["kind"], "APITimeoutError")
        # The final event carried no HTTP status; an earlier 503 must not leak
        # into the final provider-error slot.
        self.assertEqual(provider["http_status"], UNKNOWN)
        self.assertEqual(
            provider["last_provider_error_at"], "2023-11-14T22:13:20.025Z"
        )

    def test_engine_classification_matches_per_event_helper(self):
        for lines in (
            [typed_error("FreeUsageLimitError", status=429)],
            [typed_error("APIError", status=422)],
            [typed_error("MessageAbortedError", message="cancelled")],
        ):
            text = "".join(lines)
            events = parse_opencode_error_events(text)
            self.assertEqual(len(events), 1)
            self.assertEqual(
                classify_opencode_error_event(events[0]),
                classify_opencode_events(text, 1),
            )
            summary = summarize(lines, exit_code=1)
            self.assertEqual(
                summary["attempt"]["computed_classification"],
                classify_opencode_events(text, 1),
            )

        # Non-error records and non-mapping payloads are never provider
        # evidence, and no free-form error text is exposed by the helper.
        self.assertEqual(
            classify_opencode_error_event({"type": "text"}), "inconclusive"
        )
        self.assertEqual(
            classify_opencode_error_event("not-an-event"), "inconclusive"
        )
        self.assertEqual(
            describe_opencode_error(None),
            {"provider": False, "kind": "", "status": None},
        )

    def test_github_mode_uses_diagnostics_and_reports_parity(self):
        diagnostics = (
            "Agent error: {\n"
            "  name: 'FreeUsageLimitError',\n"
            "  data: {\n"
            "    statusCode: 429\n"
            "  }\n"
            "}\n"
        )
        # stdout prose must never be parsed as a run event stream in github mode.
        stdout = event(
            "text", part={"type": "text", "text": "a 429 in stdout is not evidence"}
        )

        summary = summarize_opencode_log(
            stdout,
            mode="github",
            exit_code=1,
            diagnostics_text=diagnostics,
        )

        self.assertEqual(summary["attempt"]["mode"], "github")
        self.assertEqual(summary["attempt"]["source"], "cli-diagnostics")
        self.assertEqual(summary["attempt"]["classification"], "quota_429")
        self.assertEqual(summary["attempt"]["computed_classification"], "quota_429")
        self.assertTrue(summary["attempt"]["classification_match"])
        self.assertEqual(summary["events"]["json_events"], 0)
        self.assertEqual(summary["usage"]["steps"], 0)
        self.assertEqual(summary["sessions"]["count"], 0)
        self.assertEqual(summary["provider_error"]["classification"], "none")

    def test_wrapper_authoritative_classification_parity_is_explicit(self):
        summary = summarize(
            [typed_error("FreeUsageLimitError", status=429)],
            exit_code=1,
            classification="inconclusive",
        )

        self.assertEqual(summary["attempt"]["classification"], "inconclusive")
        self.assertEqual(summary["attempt"]["computed_classification"], "quota_429")
        self.assertFalse(summary["attempt"]["classification_match"])


class TelemetryParsingAndPersistenceTests(unittest.TestCase):
    def test_premature_eof_and_malformed_lines_are_bounded_and_non_fatal(self):
        lines = [
            start(BASE_TS, message="msg_1"),
            "provider note 429\n",
            '{"broken":}\n',
            "\n",
            '{"type":"step_finish","timestamp":',
        ]

        summary = summarize(lines, exit_code=1)
        events = summary["events"]

        self.assertEqual(events["lines"], 5)
        self.assertEqual(events["json_events"], 1)
        self.assertEqual(events["non_json_lines"], 1)
        self.assertEqual(events["malformed_lines"], 1)
        self.assertEqual(events["blank_lines"], 1)
        self.assertEqual(events["truncated_lines"], 1)
        self.assertFalse(events["read_error"])
        self.assertEqual(summary["attempt"]["classification"], "inconclusive")
        # The valid prefix is still aggregated after a premature EOF.
        self.assertEqual(events["step_start"], 1)

    def test_unreadable_source_still_produces_a_summary(self):
        summary = summarize_opencode_log(
            "", mode="run", exit_code=1, source_readable=False
        )

        self.assertTrue(summary["events"]["read_error"])
        self.assertEqual(summary["events"]["lines"], 0)
        self.assertEqual(summary["events"]["json_events"], 0)
        self.assertEqual(summary["attempt"]["classification"], "inconclusive")

    def test_summary_redacts_prompts_tool_payloads_and_error_text(self):
        secrets = (
            "SECRET_PROMPT_SK_LIVE",
            "SECRET_TOOL_INPUT",
            "SECRET_TOOL_OUTPUT",
            "SECRET_ERROR_MESSAGE",
            "ghp_SECRETTOKENVALUE",
            "SECRET_STEP_SNAPSHOT",
        )
        lines = [
            event(
                "step_start",
                part={
                    "type": "step_start",
                    "messageID": "msg_1",
                    "snapshot": {"start": secrets[5]},
                },
            ),
            event(
                "text",
                part={"type": "text", "text": "prompt " + secrets[0]},
            ),
            event(
                "step_finish",
                part={
                    "type": "step-finish",
                    "messageID": "msg_1",
                    "reason": "stop",
                    "snapshot": {"end": secrets[5]},
                    "tokens": {"input": 1, "output": 1},
                },
            ),
            event(
                "tool_use",
                part={
                    "id": "call_secret",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": secrets[1]},
                        "output": secrets[2],
                        "title": "curl with a credential",
                    },
                },
            ),
            typed_error(
                "ProviderAuthError",
                status=401,
                message=secrets[3],
                data={"apiKey": secrets[4]},
            ),
        ]

        summary = summarize(lines, exit_code=1)
        raw = json.dumps(summary, sort_keys=True)

        for secret in secrets:
            self.assertNotIn(secret, raw)
        self.assertNotIn('"message"', raw)
        self.assertNotIn('"apiKey"', raw)
        self.assertNotIn('"input"', raw)
        self.assertNotIn('"output"', raw)
        self.assertEqual(summary["provider_error"]["classification"], "fail_closed")
        self.assertEqual(summary["privacy"]["content_stored"], False)
        self.assertEqual(summary["privacy"]["tool_payloads_stored"], False)
        self.assertEqual(summary["privacy"]["error_text_stored"], False)
        human = format_human_summary(summary)
        self.assertIn("provider=fail_closed", human)
        self.assertIn("provider_kind=ProviderAuthError", human)
        self.assertIn("http_requests=unknown (unavailable)", human)
        self.assertNotIn("SECRET", human)

    def test_write_summary_is_atomic_and_creates_parent(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="telemetry-", dir=str(TMP_PARENT)))
        try:
            output = workdir / "nested" / "attempt.json"
            summary = summarize([finish(tokens={"input": 1})])

            written = write_summary(summary, str(output))

            self.assertEqual(written, str(output))
            self.assertTrue(output.is_file())
            self.assertFalse(Path(str(output) + ".tmp").exists())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), summary
            )
            # Compact stable serialization: no pretty-printed whitespace.
            self.assertNotIn("\n  ", output.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_provenance_is_bounded_and_allowlisted(self):
        provenance = build_provenance(
            mode="run",
            env={
                "GITHUB_REPOSITORY": "kodmial/continuum",
                "GITHUB_WORKFLOW": "OpenCode agent",
                "GITHUB_JOB": "agent",
                "GITHUB_RUN_ID": "123456",
                "GITHUB_RUN_ATTEMPT": "2",
                "OPENCODE_MODEL": "openrouter/free-model",
                "GITHUB_TOKEN": "ghp_SECRETTOKENVALUE",
            },
        )

        self.assertEqual(provenance["repository"], "kodmial/continuum")
        self.assertEqual(provenance["workflow"], "OpenCode agent")
        self.assertEqual(provenance["job"], "agent")
        self.assertEqual(provenance["run_id"], "123456")
        self.assertEqual(provenance["run_attempt"], "2")
        self.assertEqual(provenance["model"], "openrouter/free-model")
        self.assertEqual(provenance["mode"], "run")
        self.assertEqual(provenance["schema_version"], 1)
        self.assertNotIn("ghp_SECRETTOKENVALUE", json.dumps(provenance))

        malicious = build_provenance(
            mode="run",
            env={
                "GITHUB_REPOSITORY": "owner/repo\n::warning::injected",
                "GITHUB_WORKFLOW": "bad workflow!",
                "GITHUB_RUN_ID": "not-a-number",
                "OPENCODE_MODEL": "model with spaces",
            },
        )
        self.assertEqual(malicious["repository"], UNKNOWN)
        self.assertEqual(malicious["workflow"], UNKNOWN)
        self.assertEqual(malicious["run_id"], UNKNOWN)
        self.assertEqual(malicious["model"], UNKNOWN)

        summary = summarize(
            [finish(tokens={"input": 1})],
            provenance=provenance,
        )
        self.assertEqual(summary["provenance"]["provider"], "openrouter")

        # A library caller's mapping is re-validated, not copied wholesale.
        leaky = summarize(
            [finish(tokens={"input": 1})],
            provenance={
                "repository": "kodmial/continuum",
                "model": "openrouter/free-model",
                "run_id": 123,
                "run_attempt": "not-a-number",
                "generated_at": "not-a-timestamp",
                "mode": "run",
                "GITHUB_TOKEN": "ghp_SECRETTOKENVALUE",
                "prompt": "SECRET_PROMPT",
            },
        )
        self.assertEqual(leaky["provenance"]["repository"], "kodmial/continuum")
        self.assertEqual(leaky["provenance"]["model"], "openrouter/free-model")
        self.assertEqual(leaky["provenance"]["provider"], "openrouter")
        self.assertEqual(leaky["provenance"]["run_id"], "123")
        self.assertEqual(leaky["provenance"]["run_attempt"], UNKNOWN)
        self.assertEqual(leaky["provenance"]["mode"], "run")
        self.assertNotEqual(leaky["provenance"]["generated_at"], "not-a-timestamp")
        self.assertNotIn("ghp_SECRETTOKENVALUE", json.dumps(leaky))
        self.assertNotIn("SECRET_PROMPT", json.dumps(leaky))

        fallback = summarize(
            [finish(tokens={"input": 1})],
            model="openrouter/free-model",
            provenance="SECRET_PROVENANCE",
        )
        self.assertEqual(fallback["provenance"]["model"], "openrouter/free-model")
        self.assertNotIn("SECRET_PROVENANCE", json.dumps(fallback))

    def test_unknown_provenance_model_falls_back_to_the_selected_route(self):
        # build_provenance intentionally emits "unknown" when the environment
        # does not carry a model. The wrapper may still pass the frozen route
        # explicitly, so an unknown sentinel must not shadow it.
        absent_env = build_provenance(mode="run", env={})
        self.assertEqual(absent_env["model"], UNKNOWN)

        for provenance in (
            absent_env,
            {"model": UNKNOWN, "mode": "run"},
            {"model": "", "mode": "run"},
            {"model": 123, "mode": "run"},
        ):
            with self.subTest(provenance=provenance):
                summary = summarize(
                    [finish(tokens={"input": 1})],
                    model="openrouter/free-model#variant",
                    provenance=provenance,
                )
                self.assertEqual(
                    summary["provenance"]["model"],
                    "openrouter/free-model#variant",
                )
                self.assertEqual(summary["provenance"]["provider"], "openrouter")


class TelemetryCliTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="telemetry-cli-", dir=str(TMP_PARENT)))

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            ["python3", str(CLASSIFIER)] + list(args),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_telemetry_command_persists_run_artifact(self):
        log = self.workdir / "run.log"
        output = self.workdir / "telemetry" / "run.json"
        log.write_text(
            "".join(
                [
                    start(BASE_TS, message="msg_1"),
                    finish(
                        BASE_TS + 40,
                        message="msg_1",
                        tokens={"input": 12, "output": 4},
                        cost=0.0001,
                    ),
                ]
            ),
            encoding="utf-8",
        )

        completed = self.run_cli(
            "telemetry",
            "--mode",
            "run",
            "--log",
            str(log),
            "--exit",
            "0",
            "--classification",
            "ok",
            "--model",
            "openrouter/free-model",
            "--output",
            str(output),
            "--print-summary",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("OpenCode telemetry:", completed.stdout)
        summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(summary["attempt"]["classification"], "ok")
        self.assertEqual(summary["attempt"]["computed_classification"], "ok")
        self.assertEqual(summary["usage"]["input_tokens"], 12)
        self.assertEqual(summary["provenance"]["model"], "openrouter/free-model")

    def test_render_320_tool_lines_do_not_inflate_unique_calls_or_429(self):
        log = self.workdir / "render.log"
        output = self.workdir / "telemetry" / "render.json"
        repeated = json.dumps(
            {
                "type": "tool_use",
                "timestamp": BASE_TS,
                "sessionID": "ses_test",
                "part": {
                    "type": "tool",
                    "id": "call_repeated",
                    "tool": "bash",
                    "state": {
                        "status": "running",
                        "title": "bash provider 429 retry",
                    },
                },
            }
        ) + "\n"
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        rendered = subprocess.run(
            ["python3", str(CLASSIFIER), "render", "--log", str(log)],
            input=repeated * 320,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(rendered.stderr.count("[tool]"), 320)

        completed = self.run_cli(
            "telemetry",
            "--mode",
            "run",
            "--log",
            str(log),
            "--exit",
            "0",
            "--classification",
            "ok",
            "--output",
            str(output),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(summary["tools"]["events"], 320)
        self.assertEqual(summary["tools"]["unique_calls"], 1)
        self.assertEqual(summary["tools"]["duplicate_events"], 319)
        self.assertEqual(summary["requests"]["http_requests"], UNKNOWN)
        self.assertEqual(summary["attempt"]["classification"], "ok")
        self.assertNotEqual(summary["attempt"]["classification"], "quota_429")

    def test_telemetry_command_survives_missing_source(self):
        output = self.workdir / "telemetry" / "missing.json"

        completed = self.run_cli(
            "telemetry",
            "--mode",
            "run",
            "--log",
            str(self.workdir / "absent.log"),
            "--exit",
            "1",
            "--output",
            str(output),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Cannot read OpenCode log for telemetry", completed.stderr)
        summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(summary["events"]["read_error"])
        self.assertEqual(summary["attempt"]["classification"], "inconclusive")

    def test_telemetry_command_github_mode_uses_diagnostics(self):
        stdout_log = self.workdir / "github-out.log"
        diagnostics = self.workdir / "github-err.log"
        output = self.workdir / "telemetry" / "github.json"
        stdout_log.write_text("agent prose mentioning 429\n", encoding="utf-8")
        diagnostics.write_text(
            "Agent error: {\n"
            "  name: 'FreeUsageLimitError',\n"
            "  data: {\n"
            "    statusCode: 429\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

        completed = self.run_cli(
            "telemetry",
            "--mode",
            "github",
            "--log",
            str(stdout_log),
            "--diagnostics",
            str(diagnostics),
            "--exit",
            "1",
            "--output",
            str(output),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(summary["attempt"]["source"], "cli-diagnostics")
        self.assertEqual(summary["attempt"]["classification"], "quota_429")
        self.assertTrue(summary["attempt"]["classification_match"])


class TelemetryWorkflowContractTests(unittest.TestCase):
    def test_optional_telemetry_import_cannot_break_render_or_classify(self):
        source = CLASSIFIER.read_text(encoding="utf-8")
        before_telemetry = source.split("def telemetry(", 1)[0]
        telemetry_body = source.split("def telemetry(", 1)[1].split(
            "def main(", 1
        )[0]

        self.assertNotIn(
            "from continuum.opencode_telemetry import",
            before_telemetry,
            "render/classify must not depend on the optional telemetry module",
        )
        self.assertIn(
            "from continuum.opencode_telemetry import", telemetry_body
        )
        self.assertIn("except Exception", telemetry_body)

    def test_wrapper_writes_telemetry_before_deleting_logs(self):
        wrapper = WRAPPER.read_text(encoding="utf-8")

        self.assertIn(
            'telemetry_dir="${CONTINUUM_OPENCODE_TELEMETRY_DIR:-'
            '$runner_temp/continuum-opencode-telemetry}"',
            wrapper,
        )
        self.assertIn("telemetry --mode", wrapper)
        self.assertIn("--print-summary", wrapper)
        self.assertIn('model="${OPENCODE_MODEL:-${MODEL:-}}"', wrapper)
        self.assertGreaterEqual(wrapper.count('write_telemetry "run"'), 3)
        self.assertGreaterEqual(wrapper.count('write_telemetry "github"'), 2)

        run_branch = wrapper.split('if [[ "$command" == "run" ]]; then', 1)[1]
        run_branch = run_branch.split('if [[ "$command" == "github"', 1)[0]
        success_write = run_branch.index(
            'write_telemetry "run" "$log" "$status" "$classification"'
        )
        success_delete = run_branch.index('rm -f "$log"', success_write)
        self.assertLess(success_write, success_delete)
        # Both early failure paths persist before removing the only evidence.
        for marker in (
            'write_telemetry "run" "$log" "$status" "inconclusive"\n'
            '    rm -f "$log"',
        ):
            self.assertEqual(run_branch.count(marker), 2)
        github_branch = wrapper.split(
            'if [[ "$command" == "github" && "${2:-}" == "run" ]]; then', 1
        )[1]
        github_success = github_branch.index(
            'write_telemetry "github" "$stdout_log" "$status" "$classification"'
        )
        github_delete = github_branch.index('rm -f "$stdout_log"', github_success)
        self.assertLess(github_success, github_delete)
        self.assertIn(
            'write_telemetry "github" "$stdout_log" "$status" "inconclusive"',
            github_branch,
        )
        # Telemetry failures may warn but must never rewrite classification or
        # mask the original exit status.
        self.assertIn("provider classification and exit status are unchanged", wrapper)
        self.assertIn("return 0", wrapper)
        self.assertIn("exit \"$status\"", wrapper)

    def test_both_agent_workflows_publish_durable_telemetry_artifact(self):
        for workflow in (OPENCODE_WORKFLOW, REPAIR_WORKFLOW):
            body = workflow.read_text(encoding="utf-8")
            self.assertIn("Publish OpenCode telemetry artifact", body)
            upload = body.split("Publish OpenCode telemetry artifact", 1)[1]
            self.assertIn("if: always()", upload)
            self.assertIn("continue-on-error: true", upload)
            self.assertIn("actions/upload-artifact@v4", upload)
            self.assertIn(
                "name: continuum-opencode-telemetry-${{ github.run_id }}-"
                "${{ github.run_attempt }}",
                upload,
            )
            self.assertIn(
                "path: ${{ runner.temp }}/continuum-opencode-telemetry", upload
            )
            self.assertIn("if-no-files-found: warn", upload)
            self.assertIn("retention-days: 30", upload)


if __name__ == "__main__":
    unittest.main()
