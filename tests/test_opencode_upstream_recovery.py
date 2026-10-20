"""Regression tests for classified OpenCode infrastructure failures.

The tests execute the actual shell wrapper embedded in the reusable workflow
using a fake CLI. They prevent a prose mention of an error from being treated
as a failed provider invocation, and keep transient provider failure distinct
from quota exhaustion and ordinary code failures.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-opencode.yml"
WATCHDOG = ROOT / ".github/workflows/continuum-opencode-watchdog.yml"


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


class OpenCodeUpstreamRecoveryTests(unittest.TestCase):
    def invoke(self, message, exit_code):
        with tempfile.TemporaryDirectory() as dirname:
            temp = Path(dirname)
            (temp / "wrapper").write_text(extract_wrapper(), encoding="utf-8")
            (temp / "wrapper").chmod(0o755)
            (temp / "opencode").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$FAKE_OPENCODE_MESSAGE\"\n"
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
                capture_output=True, text=True, env=env, check=False,
            )
            return (
                run.returncode,
                run.stdout + run.stderr,
                (temp / "continuum-opencode-upstream-unavailable").exists(),
                (temp / "continuum-opencode-429").exists(),
            )

    def test_upstream_unavailable_is_infrastructure_not_429(self):
        rc, output, unavailable, quota = self.invoke(
            "Error: Upstream request failed: Endpoint is unavailable.", 1
        )
        self.assertEqual(rc, 76)
        self.assertIn(
            "CONTINUUM_OPENCODE_UPSTREAM_UNAVAILABLE_RESTART_REQUIRED", output
        )
        self.assertTrue(unavailable)
        self.assertFalse(quota)

    def test_quota_remains_separate_class(self):
        rc, _output, unavailable, quota = self.invoke(
            "APIError: HTTP 429 Too Many Requests", 1
        )
        self.assertEqual(rc, 75)
        self.assertTrue(quota)
        self.assertFalse(unavailable)

    def test_successful_output_mentioning_endpoint_does_not_trigger(self):
        rc, _output, unavailable, quota = self.invoke(
            "Log review: Upstream request failed: Endpoint is unavailable.", 0
        )
        self.assertEqual(rc, 0)
        self.assertFalse(unavailable)
        self.assertFalse(quota)

    def test_ordinary_failed_command_stays_failure(self):
        rc, _output, unavailable, quota = self.invoke(
            "Error: local build assertion failed.", 1
        )
        self.assertEqual(rc, 1)
        self.assertFalse(unavailable)
        self.assertFalse(quota)

    def test_all_agent_modes_preserve_infrastructure_checkpoint(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        for mode in ("issue", "qualification", "coderabbit-fix", "resolve-conflict", "ci-fix"):
            self.assertRegex(
                source,
                r'continuum_infra_evacuate "[^"]+" "[^"]+" "' +
                re.escape(mode) + r'"',
            )
        self.assertEqual(source.count('[[ "$OPENCODE_RUN_RC" -eq 76 ]] || grep'), 5)
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
