"""Executable regressions for OpenCode provider-failure classification.

These tests run the production wrapper and classifier scripts that both
workflows install, with a fake OpenCode CLI. Provider quota/upstream evidence
is only trusted from authenticated provider error channels: structured
``run --format json`` ``type=error`` events, or the CLI runtime's stderr
``Agent error:`` block for ``github run``. Agent prose, source files, tool
output, and test fixtures that merely quote a quota token are never evidence.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
WRAPPER = ROOT / ".github" / "scripts" / "opencode_429_wrapper.sh"
CLASSIFIER = ROOT / ".github" / "scripts" / "opencode_429_classify.py"
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"
REPAIR_WORKFLOW = ROOT / ".github" / "workflows" / "continuum-pr-agent-repair.yml"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"


def event(kind, **fields):
    payload = {"type": kind, "timestamp": 1, "sessionID": "ses_test"}
    payload.update(fields)
    return json.dumps(payload) + "\n"


def error_event(name=None, message=None, status=None, response_body=None):
    data = {}
    if message is not None:
        data["message"] = message
    if status is not None:
        data["statusCode"] = status
    if response_body is not None:
        data["responseBody"] = response_body
    return event("error", error={"name": name or "APIError", "data": data})


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


def github_error_block(name="APIError", message="", status=None, prefix="Agent error:"):
    lines = [prefix + " {", "  name: '{}',".format(name), "  data: {"]
    if message:
        lines.append("    message: '{}',".format(message))
    if status is not None:
        lines.append("    statusCode: {},".format(status))
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


class ProductionWrapperTests(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(tempfile.mkdtemp(prefix="429-", dir=str(TMP_PARENT)))
        self.bin = self.workdir / "bin"
        self.bin.mkdir()
        self.runner = self.workdir / "runner"
        self.runner.mkdir()
        self.args_file = self.workdir / "args"
        fake = self.bin / "opencode"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s' \"${FAKE_OPENCODE_STDOUT:-}\"\n"
            "printf '%s' \"${FAKE_OPENCODE_STDERR:-}\" >&2\n"
            "printf '%s\\n' \"$*\" > \"${FAKE_ARGS_FILE:-/dev/null}\"\n"
            "exit \"${FAKE_OPENCODE_EXIT:-0}\"\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def invoke(self, stdout="", stderr="", exit_code=0, argv=("run", "--auto")):
        env = dict(os.environ)
        env.update(
            RUNNER_TEMP=str(self.runner),
            CONTINUUM_429_CLASSIFIER=str(CLASSIFIER),
            FAKE_OPENCODE_STDOUT=stdout,
            FAKE_OPENCODE_STDERR=stderr,
            FAKE_OPENCODE_EXIT=str(exit_code),
            FAKE_ARGS_FILE=str(self.args_file),
            PATH=str(self.bin) + os.pathsep + env.get("PATH", ""),
        )
        run = subprocess.run(
            [str(WRAPPER), *argv],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        classification = ""
        path = self.runner / "continuum-opencode-classification"
        if path.exists():
            classification = path.read_text(encoding="utf-8").strip()
        return {
            "rc": run.returncode,
            "output": run.stdout + run.stderr,
            "stdout": run.stdout,
            "stderr": run.stderr,
            "quota": (self.runner / "continuum-opencode-429").exists(),
            "unavailable": (
                self.runner / "continuum-opencode-upstream-unavailable"
            ).exists(),
            "inconclusive": (
                self.runner / "continuum-opencode-inconclusive"
            ).exists(),
            "classification": classification,
            "args": self.args_file.read_text(encoding="utf-8").strip()
            if self.args_file.exists()
            else "",
        }

    def test_success_quoting_quota_tokens_is_not_provider_failure(self):
        result = self.invoke(
            stdout=(
                text_event(
                    'self.assertTrue(is_opencode_429("FreeUsageLimitError: quota exceeded", 0))'
                )
                + text_event("HTTP 429 Too Many Requests in fixture")
                + tool_event(
                    'return 1, "ERROR FreeUsageLimitError: free usage limit reached"'
                )
                + "tool output: APIError 429 Too Many Requests\n"
            ),
            exit_code=0,
        )
        self.assertEqual(result["rc"], 0, result["output"])
        self.assertFalse(result["quota"])
        self.assertFalse(result["unavailable"])
        self.assertFalse(result["inconclusive"])
        self.assertEqual(result["classification"], "ok")

    def test_failed_tests_quoting_quota_tokens_are_task_failure(self):
        result = self.invoke(
            stdout=text_event(
                'FAILED test_429: FreeUsageLimitError and HTTP 429 "Too Many Requests"'
            ),
            exit_code=1,
        )
        self.assertEqual(result["rc"], 1, result["output"])
        self.assertFalse(result["quota"])
        self.assertFalse(result["unavailable"])
        self.assertTrue(result["inconclusive"])
        self.assertEqual(result["classification"], "inconclusive")

    def test_structured_provider_429_recovers_on_zero_and_nonzero_exit(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                result = self.invoke(
                    stdout=error_event("APIError", "429 Too Many Requests", 429),
                    exit_code=exit_code,
                )
                self.assertEqual(result["rc"], 75, result["output"])
                self.assertTrue(result["quota"])
                self.assertFalse(result["unavailable"])
                self.assertEqual(result["classification"], "quota_429")

    def test_free_usage_response_body_is_authenticated_quota(self):
        result = self.invoke(
            stdout=error_event(
                "APIError",
                response_body='{"type":"error","error":{"type":"FreeUsageLimitError"}}',
            ),
            exit_code=0,
        )
        self.assertEqual(result["rc"], 75, result["output"])
        self.assertTrue(result["quota"])
        self.assertEqual(result["classification"], "quota_429")

    def test_upstream_and_timeout_are_infrastructure_not_quota(self):
        cases = (
            (error_event("APIError", 'quoted "429" in review text', 503), "unavailable"),
            (error_event("APIError", "bad gateway", 502), "upstream"),
            (error_event("APITimeoutError", "Request timed out"), "timeout"),
            (
                error_event(
                    "APIError",
                    "FreeUsageLimitError and 429 in quoted text",
                    503,
                ),
                "unavailable",
            ),
        )
        for log, expected in cases:
            with self.subTest(expected=expected):
                result = self.invoke(stdout=log, exit_code=1)
                self.assertEqual(result["rc"], 76, result["output"])
                self.assertTrue(result["unavailable"])
                self.assertFalse(result["quota"])
                self.assertEqual(result["classification"], expected)

    def test_deterministic_client_errors_fail_closed_without_markers(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                result = self.invoke(
                    stdout=error_event(
                        "APIError", 'rejected; quoted "429" and FreeUsageLimitError', status
                    ),
                    exit_code=1,
                )
                self.assertEqual(result["rc"], 1, result["output"])
                self.assertFalse(result["quota"])
                self.assertFalse(result["unavailable"])
                self.assertEqual(result["classification"], "fail_closed")

    def test_exit_code_propagation_preserves_nonstandard_statuses(self):
        result = self.invoke(
            stdout=error_event("APIError", "unauthorized", 401),
            exit_code=42,
        )
        self.assertEqual(result["rc"], 42, result["output"])
        self.assertEqual(result["classification"], "fail_closed")
        result = self.invoke(stdout=text_event("failure"), exit_code=42)
        self.assertEqual(result["rc"], 42, result["output"])
        self.assertEqual(result["classification"], "inconclusive")

    def test_malformed_and_truncated_events_are_inconclusive(self):
        for log, exit_code in (
            ('{"type":"error", not-json\n', 1),
            ('{"type":"error","error":{"name":"APIError"\n', 1),
            ('{"type":"error","error":"FreeUsageLimitError"}\n', 1),
            ("partial output only\n", 1),
        ):
            if self.args_file.exists():
                self.args_file.unlink()
            result = self.invoke(stdout=log, exit_code=exit_code)
            self.assertEqual(result["rc"], exit_code, result["output"])
            self.assertFalse(result["quota"])
            self.assertFalse(result["unavailable"])
            self.assertEqual(result["classification"], "inconclusive")

    def test_run_route_injects_structured_format_and_renders_events(self):
        result = self.invoke(
            stdout=text_event("done") + tool_event("pytest") + error_event("APIError", "boom", 500),
            exit_code=1,
        )
        self.assertIn("run --format json --auto", result["args"])
        self.assertIn("done", result["stdout"])
        self.assertIn("[tool] pytest", result["stderr"])
        self.assertIn("OpenCode error: APIError status=500 boom", result["stderr"])
        self.assertEqual(result["rc"], 76, result["output"])

    def test_run_refuses_unstructured_format(self):
        result = self.invoke(argv=("run", "--format", "default", "--auto"))
        self.assertEqual(result["rc"], 2, result["output"])
        self.assertEqual(result["args"], "")
        self.assertFalse(result["quota"])

    def test_github_run_uses_stderr_diagnostics_and_keeps_arguments(self):
        result = self.invoke(
            stdout=text_event("Response: FreeUsageLimitError 429 in agent text")
            + "Response: Agent error: statusCode: 429\n",
            stderr=github_error_block(status=429),
            exit_code=1,
            argv=("github", "run"),
        )
        self.assertEqual(result["rc"], 75, result["output"])
        self.assertTrue(result["quota"])
        self.assertEqual(result["classification"], "quota_429")
        self.assertEqual(result["args"], "github run")

    def test_github_run_success_quoting_quota_is_healthy(self):
        result = self.invoke(
            stdout="Response: FreeUsageLimitError HTTP 429 in review text\n",
            stderr="",
            exit_code=0,
            argv=("github", "run"),
        )
        self.assertEqual(result["rc"], 0, result["output"])
        self.assertFalse(result["quota"])
        self.assertEqual(result["classification"], "ok")

    def test_github_run_real_failures_are_separated(self):
        cases = (
            (github_error_block(status=503), 76, "unavailable"),
            (
                github_error_block(
                    "AuthenticationError", 'quoted "429"', 401
                ),
                1,
                "fail_closed",
            ),
            (github_error_block("APITimeoutError", "timed out"), 76, "timeout"),
        )
        for block, expected_rc, expected_class in cases:
            with self.subTest(expected_class=expected_class):
                result = self.invoke(
                    stdout="",
                    stderr=block,
                    exit_code=1,
                    argv=("github", "run"),
                )
                self.assertEqual(result["rc"], expected_rc, result["output"])
                self.assertEqual(result["classification"], expected_class)
                self.assertEqual(result["quota"], expected_class == "quota_429")


class WorkflowContractTests(unittest.TestCase):
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

    def test_both_workflows_install_the_same_production_classifier(self):
        for path in (WORKFLOW, REPAIR_WORKFLOW):
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("Checkout Continuum 429 classifier", source)
                self.assertIn("opencode_429_classify.py", source)
                self.assertIn("opencode_429_wrapper.sh", source)
                self.assertIn("CONTINUUM_429_CLASSIFIER", source)
                self.assertIn("run --help", source)
                self.assertIn("--format", source)
                self.assertNotIn("grep -Eiq 'FreeUsageLimitError", source)
                self.assertNotIn("429 Too Many Requests", source)
        repair = REPAIR_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("continuum-opencode run --auto", repair)
        self.assertIn("repair_classification", repair)
        for classification in ("quota_429", "unavailable", "upstream", "timeout"):
            self.assertIn(
                'provider_failure={}'.format(classification), repair
            )


class ClassifierParityTests(unittest.TestCase):
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
        ("plain prose with no structured event\n", 1),
    )

    def test_python_engine_and_shared_cli_agree(self):
        from continuum.opencode_429_recovery import classify_opencode_events

        self.assertTrue(CLASSIFIER.is_file())
        self.assertTrue(WRAPPER.is_file())
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        for log, exit_code in self.CASES:
            with tempfile.TemporaryDirectory(dir=str(TMP_PARENT)) as dirname:
                path = Path(dirname) / "log"
                path.write_text(log, encoding="utf-8")
                completed = subprocess.run(
                    [
                        "python3",
                        str(CLASSIFIER),
                        "classify",
                        "--mode",
                        "run",
                        "--log",
                        str(path),
                        "--exit",
                        str(exit_code),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    completed.stdout.strip(),
                    classify_opencode_events(log, exit_code),
                    completed.stderr,
                )


if __name__ == "__main__":
    unittest.main()
