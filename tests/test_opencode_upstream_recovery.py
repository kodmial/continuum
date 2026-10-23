"""Regression tests for structured OpenCode provider-error classification.

The tests execute the actual shell wrapper embedded in the reusable workflow
using a fake CLI. Provider quota/upstream evidence is only trusted from
OpenCode's structured ``run --format json`` ``type=error`` events; prose,
source code, test fixtures, and tool output that merely contain a token are
never provider evidence.
"""
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"


def extract_wrapper():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r'cat > "\$wrapper" <<\x27SH\x27\n(.*?)\n          SH\n',
        workflow,
        re.DOTALL,
    )
    if not match:
        raise AssertionError("OpenCode wrapper heredoc is missing")
    return textwrap.dedent(match.group(1)) + "\n"


def event(kind, **fields):
    payload = {"type": kind, "timestamp": 1, "sessionID": "ses_test"}
    payload.update(fields)
    return json.dumps(payload) + "\n"


def error_event(name=None, message=None, status=None):
    data = {}
    if message is not None:
        data["message"] = message
    if status is not None:
        data["statusCode"] = status
    return event(
        "error",
        error={"name": name or "APIError", "data": data},
    )


def text_event(text):
    return event("text", part={"type": "text", "text": text})


def tool_event(output):
    return event(
        "tool_use",
        part={
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "output": output, "title": "pytest"},
        },
    )


class OpenCodeProviderRecoveryTests(unittest.TestCase):
    def invoke(self, message, exit_code):
        with tempfile.TemporaryDirectory() as dirname:
            temp = Path(dirname)
            (temp / "wrapper").write_text(extract_wrapper(), encoding="utf-8")
            (temp / "wrapper").chmod(0o755)
            (temp / "opencode").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s' \"$FAKE_OPENCODE_MESSAGE\"\n"
                "exit \"$FAKE_OPENCODE_EXIT\"\n",
                encoding="utf-8",
            )
            (temp / "opencode").chmod(0o755)
            env = dict(os.environ)
            env.update(
                RUNNER_TEMP=dirname,
                FAKE_OPENCODE_MESSAGE=message,
                FAKE_OPENCODE_EXIT=str(exit_code),
                PATH=dirname + os.pathsep + env.get("PATH", ""),
            )
            run = subprocess.run(
                [str(temp / "wrapper"), "run", "--auto"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return (
                run.returncode,
                run.stdout + run.stderr,
                (temp / "continuum-opencode-upstream-unavailable").exists(),
                (temp / "continuum-opencode-429").exists(),
                (temp / "continuum-opencode-inconclusive").exists(),
            )

    def test_upstream_unavailable_is_infrastructure_not_429(self):
        rc, output, unavailable, quota, _ = self.invoke(
            error_event(
                "APIError",
                "Error: Upstream request failed: Endpoint is unavailable.",
            ),
            1,
        )
        self.assertEqual(rc, 76)
        self.assertIn(
            "CONTINUUM_OPENCODE_UPSTREAM_UNAVAILABLE_RESTART_REQUIRED", output
        )
        self.assertTrue(unavailable)
        self.assertFalse(quota)

    def test_structured_503_is_infrastructure_not_quota(self):
        rc, _output, unavailable, quota, _ = self.invoke(
            error_event("APIError", "503 Service Unavailable", 503), 1
        )
        self.assertEqual(rc, 76)
        self.assertTrue(unavailable)
        self.assertFalse(quota)

    def test_quota_event_remains_separate_class(self):
        rc, _output, unavailable, quota, _ = self.invoke(
            error_event("APIError", "HTTP 429 Too Many Requests", 429), 1
        )
        self.assertEqual(rc, 75)
        self.assertTrue(quota)
        self.assertFalse(unavailable)

    def test_quota_event_with_exit_zero_still_burns(self):
        rc, _output, _unavailable, quota, _ = self.invoke(
            error_event("FreeUsageLimitError", "free usage limit reached"), 0
        )
        self.assertEqual(rc, 75)
        self.assertTrue(quota)

    def test_successful_output_mentioning_quota_does_not_trigger(self):
        rc, _output, unavailable, quota, inconclusive = self.invoke(
            text_event(
                'self.assertTrue(is_opencode_429("FreeUsageLimitError: quota exceeded", 0))'
            ),
            0,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(unavailable)
        self.assertFalse(quota)
        self.assertFalse(inconclusive)

    def test_successful_tool_output_mentioning_quota_does_not_trigger(self):
        rc, _output, unavailable, quota, _ = self.invoke(
            tool_event(
                'return 1, "ERROR FreeUsageLimitError: free usage limit reached"\n'
            ),
            0,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(unavailable)
        self.assertFalse(quota)

    def test_failed_tests_mentioning_quota_are_not_provider_evidence(self):
        rc, _output, unavailable, quota, inconclusive = self.invoke(
            text_event("FAILED test_429: FreeUsageLimitError: quota exceeded"), 1
        )
        self.assertEqual(rc, 1)
        self.assertFalse(unavailable)
        self.assertFalse(quota)
        self.assertTrue(inconclusive)

    def test_successful_output_mentioning_endpoint_does_not_trigger(self):
        rc, _output, unavailable, quota, _ = self.invoke(
            text_event("Log review: Upstream request failed: Endpoint is unavailable."),
            0,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(unavailable)
        self.assertFalse(quota)

    def test_ordinary_failed_command_stays_failure(self):
        rc, _output, unavailable, quota, inconclusive = self.invoke(
            error_event("AssertionError", "local build assertion failed."), 1
        )
        self.assertEqual(rc, 1)
        self.assertFalse(unavailable)
        self.assertFalse(quota)
        self.assertTrue(inconclusive)

    def test_fail_closed_auth_errors_do_not_burn(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                rc, _output, unavailable, quota, _ = self.invoke(
                    error_event("APIError", "auth failed", status), 1
                )
                self.assertEqual(rc, 1)
                self.assertFalse(unavailable)
                self.assertFalse(quota)

    def test_no_provider_signal_is_inconclusive(self):
        rc, output, unavailable, quota, inconclusive = self.invoke(
            "process exited before emitting a structured event\n", 1
        )
        self.assertEqual(rc, 1)
        self.assertFalse(unavailable)
        self.assertFalse(quota)
        self.assertTrue(inconclusive)
        self.assertIn("authoritative provider error event", output)

    def test_malformed_json_is_inconclusive(self):
        rc, _output, unavailable, quota, inconclusive = self.invoke(
            '{"type":"error", not-json\n', 1
        )
        self.assertEqual(rc, 1)
        self.assertFalse(unavailable)
        self.assertFalse(quota)
        self.assertTrue(inconclusive)

    def test_duplicate_quota_events_remain_quota(self):
        rc, _output, _unavailable, quota, _ = self.invoke(
            error_event("APIError", "429 Too Many Requests", 429)
            + error_event("APIError", "429 Too Many Requests", 429),
            1,
        )
        self.assertEqual(rc, 75)
        self.assertTrue(quota)

    def test_all_agent_modes_preserve_infrastructure_checkpoint(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        for mode in (
            "issue",
            "qualification",
            "coderabbit-fix",
            "resolve-conflict",
            "ci-fix",
        ):
            self.assertRegex(
                source,
                r'continuum_infra_evacuate "[^"]+" "[^"]+" "' + re.escape(mode) + r'"',
            )
        self.assertEqual(
            source.count('[[ "$OPENCODE_RUN_RC" -eq 75 || "$OPENCODE_RUN_RC" -eq 76 ]]'),
            5,
        )
        self.assertNotIn("grep -Eiq 'FreeUsageLimitError'", source)
        self.assertNotIn("APIError.*429|HTTP", source)
        self.assertIn("operation: fail", source)
        self.assertIn("failure-reason: unavailable", source)
        self.assertIn('"failure_kind": os.environ.get', source)
        watchdog = WATCHDOG.read_text(encoding="utf-8")
        self.assertIn("upstreamUnavailableMarker", watchdog)
        self.assertIn("(?:429|infrastructure) checkpoint", watchdog)
        self.assertIn("(?:429|infra)-checkpoint-", watchdog)
        self.assertIn("emittedRestartMarker", watchdog)


if __name__ == "__main__":
    unittest.main()


REPAIR_WORKFLOW = ROOT / ".github" / "workflows" / "continuum-pr-agent-repair.yml"


def extract_classifiers(path):
    text = path.read_text(encoding="utf-8")
    return [
        textwrap.dedent(match) + "\n"
        for match in re.findall(
            r"<<'PY_CLASSIFY'\n(.*?)\n\s*PY_CLASSIFY", text, re.DOTALL
        )
    ]


class ProviderClassifierParityTests(unittest.TestCase):
    CASES = (
        (text_event('self.assertTrue(is_opencode_429("FreeUsageLimitError", 0))'), 0),
        (text_event("FAILED test_429: FreeUsageLimitError"), 1),
        (tool_event('return 1, "ERROR FreeUsageLimitError: free usage limit"'), 0),
        (tool_event("APIError 429 Too Many Requests\n"), 1),
        (error_event("FreeUsageLimitError", "quota exceeded", 429), 0),
        (error_event("APIError", "429 Too Many Requests", 429), 1),
        (error_event("APIError", "503 Service Unavailable", 503), 1),
        (error_event("APIError", "Endpoint is unavailable", 0), 1),
        (error_event("AuthenticationError", "unauthorized", 401), 1),
        (error_event("PermissionDeniedError", "forbidden", 403), 1),
        (error_event("NotFoundError", "not found", 404), 1),
        (error_event("APITimeoutError", "Request timed out"), 1),
        ('{"type":"error", not-json\n', 1),
        (
            error_event("APIError", "429 Too Many Requests", 429)
            + error_event("APIError", "429 Too Many Requests", 429),
            1,
        ),
        ("plain prose with no structured event\n", 1),
    )

    def test_shell_classifiers_agree_with_python_engine(self):
        from continuum.opencode_429_recovery import classify_opencode_events

        (ROOT / ".opencode-tmp").mkdir(parents=True, exist_ok=True)
        classifiers = extract_classifiers(WORKFLOW) + extract_classifiers(
            REPAIR_WORKFLOW
        )
        self.assertGreaterEqual(len(classifiers), 2)
        for code in classifiers:
            for log, exit_code in self.CASES:
                with self.subTest(code=code[:40], log=log[:60], exit_code=exit_code):
                    with tempfile.NamedTemporaryFile(
                        "w", encoding="utf-8", delete=False, dir=str(ROOT / ".opencode-tmp")
                    ) as handle:
                        handle.write(log)
                        path = handle.name
                    try:
                        completed = subprocess.run(
                            [sys.executable, "-", path, str(exit_code)],
                            input=code,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                    finally:
                        os.unlink(path)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        completed.stdout.strip(),
                        classify_opencode_events(log, exit_code),
                        completed.stderr,
                    )

    def test_repair_workflow_uses_structured_events_only(self):
        source = REPAIR_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("opencode run --format json", source)
        self.assertIn("PY_CLASSIFY", source)
        self.assertNotIn("grep -Eiq 'FreeUsageLimitError", source)
        self.assertNotIn("RateLimitError|429 Too Many Requests", source)
        self.assertIn('echo "provider_failure=$repair_provider_failure"', source)
        for classification in ("quota_429", "unavailable", "upstream", "timeout"):
            self.assertIn(
                f"{classification}) repair_provider_failure={classification} ;;",
                source,
            )
        # Classify every completed invocation, not only non-zero exits: a
        # genuine provider error can be emitted by a CLI that still exits 0.
        self.assertIn(
            'if [[ "$repair_status" -ne 0 || -n "$repair_provider_failure" ]]',
            source,
        )
        self.assertNotIn(
            'if [[ "$repair_status" -ne 0 ]]; then\n            repair_classification',
            source,
        )
