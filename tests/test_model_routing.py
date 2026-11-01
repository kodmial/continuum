"""Work Lock #66: routing is policy-driven, bounded and independently tested."""
from __future__ import annotations
import importlib.util
import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[1] / "src/continuum/model_routing.py"
spec = importlib.util.spec_from_file_location("model_routing", PATH)
route = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route
spec.loader.exec_module(route)
A = "provider/first"
B = "provider/second"
C = "another/third"


class ConfigurationTests(unittest.TestCase):
    def test_pool_order_and_legacy_precedence(self):
        c = route.Configuration.parse(f"{A},{B},{C}", legacy="provider/ignored")
        self.assertEqual(c.models, (A, B, C))
        self.assertFalse(c.pinned)
        self.assertEqual(route.Configuration.parse("", A).models, (A,))
        self.assertTrue(route.Configuration.parse("", A).pinned)

    def test_explicit_pin_is_allowlisted_and_disables_failover(self):
        c = route.Configuration.parse(f"{A},{B}", legacy="provider/ignored", explicit=B)
        self.assertEqual(c.models, (B,))
        self.assertTrue(c.pinned)
        with self.assertRaises(route.RouteError):
            route.Configuration.parse(f"{A},{B}", explicit=C)

    def test_fail_closed_on_empty_duplicate_and_invalid(self):
        bad = ["", ",", f"{A},", f"{A},{A}", "wrong-model", f"{A},invalid",
               f"{A},abc/../def", "bad / model", ",".join([A] * 9)]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(route.RouteError):
                    route.Configuration.parse(value)
        with self.assertRaises(route.RouteError):
            route.Configuration.parse("")

    def test_no_free_suffix_assumption_and_no_config_defaults(self):
        self.assertEqual(route.Configuration.parse("vendor/fully-custom").models,
                         ("vendor/fully-custom",))
        with self.assertRaises(route.RouteError):
            route.Configuration.parse("", "")
        with self.assertRaises(route.RouteError):
            route.Configuration.parse(f"{A},", legacy=B)

    def test_invalid_budgets_and_strategy(self):
        for kwargs in ({"ttl": "0"}, {"cooldown": "-1"}, {"timeout": "9999"},
                       {"timeout": "1"}, {"strategy": "random"}, {"task_class": "all"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(route.RouteError):
                    route.Configuration.parse(A, **kwargs)

    def test_sticky_requires_proven_execution_and_fresh_probe(self):
        c = route.Configuration.parse(f"{A},{B},{C}", ttl="180")
        now = 1000
        state = {"last_successful_real_execution": {"model": B, "at": 850},
                 "last_successful_probe": {"model": B, "at": 900}}
        self.assertEqual(route.route_order(c, state, now), (B, A, C))
        state["last_successful_probe"]["at"] = 700
        # Reprobe the last-known-good route after TTL; do not lose sticky order.
        self.assertEqual(route.route_order(c, state, now), (B, A, C))
        state["last_successful_probe"]["at"] = 900
        state["cooldowns"] = {B: now + 20}
        self.assertEqual(route.route_order(c, state, now), (A, C))

    def test_pin_does_not_failover_or_bypass_cooldown(self):
        c = route.Configuration.parse(f"{A},{B}", explicit=B)
        self.assertEqual(route.route_order(c, {"cooldowns": {B: 9999999}}, 100), ())
        self.assertEqual(route.route_order(c, {"cooldowns": {B: 100}}, 101), (B,))

    def test_scope_key_does_not_expose_private_child(self):
        config = route.Configuration.parse(f"{A},{B}")
        x = route.route_key("org/private-project", "review", "anonymous", config, "00" * 32)
        y = route.route_key("org/other-project", "review", "anonymous", config, "00" * 32)
        self.assertNotEqual(x, y)
        self.assertEqual(len(x), 64)
        self.assertNotIn("private-project", x)
        self.assertNotEqual(x, route.route_key("org/private-project", "coder",
                                             "anonymous", config, "00" * 32))
        with self.assertRaises(route.RouteError):
            route.route_key("org/x", "private/child", "anonymous", config, "x")

    def test_binary_or_pool_change_invalidates_state(self):
        c = route.Configuration.parse(f"{A},{B}")
        d = route.Configuration.parse(f"{B},{A}")
        self.assertNotEqual(c.fingerprint("abc", "anonymous"), d.fingerprint("abc", "anonymous"))
        self.assertNotEqual(c.fingerprint("abc", "anonymous"), c.fingerprint("def", "anonymous"))

    def test_error_classification(self):
        pairs = {"quota_429": "FreeUsageLimitError",
                 "authentication": "HTTP 401 invalid API key",
                 "unavailable": "model deprecated",
                 "timeout": "Timed out",
                 "upstream": "502 bad gateway"}
        for expected, raw in pairs.items():
            self.assertEqual(route.classify(raw, 1), expected)


class _CapturedText(str):
    """Captured text shaped like the probe's ``stdout``/``stderr`` stream.

    The probe reads the pipe incrementally through ``buffer.read1``, so the
    fake exposes both the text value and a byte-wise ``read1``.
    """

    def __new__(cls, text: str = ""):
        value = super().__new__(cls, text)
        value._offset = 0
        return value

    def read(self, size: int = 1) -> str:
        text = str(self)
        if size < 0:
            chunk, self._offset = text[self._offset:], len(text)
        else:
            chunk = text[self._offset:self._offset + size]
            self._offset += len(chunk)
        return chunk

    @property
    def buffer(self):
        return self

    def read1(self, size: int = -1) -> bytes:
        return self.read(size).encode("utf-8")

    def close(self) -> None:
        pass


class _ChunkedStream:
    """Stream double whose ``buffer.read1`` returns preset byte chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.buffer = self

    def read1(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class FakeProcess:
    """Popen-shaped subprocess double for the semantic probe seam."""

    def __init__(self, *command, stdout: str = "", stderr: str = "",
                 returncode: int = 0, hang: bool = False, **options):
        # Accept and ignore the Popen(command, cwd=..., env=...,
        # stdout=PIPE, stderr=PIPE, text=True) call shape.
        self.command = command
        self.options = options
        self.stdout = _CapturedText(stdout)
        self.stderr = _CapturedText(stderr)
        self.returncode = None if hang else returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        if self.returncode is None:
            raise route.subprocess.TimeoutExpired("opencode", timeout)
        return self.returncode


class ProbeTests(unittest.TestCase):
    def test_observed_model_mismatch_is_fatal(self):
        answer = '{"type":"text","part":{"type":"text","text":"Use >="}}\n'
        self.assertEqual(route.extract_response(answer, A), "Use >=")
        wrong = '{"type":"step_finish","info":{"providerID":"other","modelID":"third"}}'
        with self.assertRaises(route.RouteError):
            route.extract_response(answer + wrong, A)

    def test_empty_or_non_text_response_is_not_qualification(self):
        with self.assertRaises(route.RouteError):
            route.extract_response('{"type":"step_finish"}', A)

    def test_probe_does_not_inherit_tokens_and_checks_review_answer(self):
        seen = {}
        def runner(command, **kwargs):
            seen.update(kwargs)
            self.assertEqual(command[0:2], ["opencode", "run"])
            return FakeProcess(
                stdout='{"type":"text","part":{"type":"text","text":"Replace > with >="}}\n')
        with patch.dict("os.environ", {"GH_TOKEN": "no", "GITHUB_TOKEN": "also-no"}, clear=True):
            route.semantic_probe(A, "review", 30, runner=runner)
        self.assertNotIn("GH_TOKEN", seen["env"])
        self.assertNotIn("GITHUB_TOKEN", seen["env"])

    def test_probe_requires_actual_tool_created_file(self):
        def runner(command, **kwargs):
            return FakeProcess(
                stdout='{"type":"text","part":{"type":"text","text":"done"}}\n')
        with self.assertRaisesRegex(route.RouteError, "coding_tool_capability"):
            route.semantic_probe(A, "coder", 30, runner=runner)

    def test_429_burns_runner_and_skips_any_second_probe(self):
        def runner(command, **kwargs):
            return FakeProcess(stderr="FreeUsageLimitError 429", returncode=1)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaises(route.BurnedRunner):
                    route.semantic_probe(A, "review", 30, runner=runner)
                self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())

    def test_structured_exit_zero_quota_event_burns_runner(self):
        """A structured quota event wins even when the CLI exits 0."""
        event = ('{"type":"error","error":{"name":"RateLimitError",'
                 '"data":{"statusCode":429}}}\n')
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return FakeProcess(stdout=event)
        # The local classifier alone misses this exit-0 structured event.
        self.assertEqual(route.classify(event, 0), "invalid_response")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaises(route.BurnedRunner):
                    route.semantic_probe(A, "review", 30, runner=runner)
                self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())
        # No failover and no second model request on the burned runner.
        self.assertEqual(len(calls), 1)

    def test_structured_exit_zero_non_quota_event_keeps_previous_path(self):
        event = ('{"type":"error","error":{"name":"ProviderAuthError",'
                 '"data":{"statusCode":401}}}\n')
        def runner(command, **kwargs):
            return FakeProcess(stdout=event)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaisesRegex(route.RouteError,
                                            "no assistant text events"):
                    route.semantic_probe(A, "review", 30, runner=runner)
                self.assertFalse((pathlib.Path(tmp) / "continuum-opencode-429").exists())

    def test_nonzero_429_still_burns_without_structured_event(self):
        def runner(command, **kwargs):
            return FakeProcess(stderr="HTTP 429 too many requests", returncode=1)
        raw = "\n" + "HTTP 429 too many requests"
        # The shared helper needs structured evidence, so this path is the
        # pre-existing non-zero text classifier.
        self.assertFalse(route.is_opencode_429(raw, 1))
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaises(route.BurnedRunner):
                    route.semantic_probe(A, "review", 30, runner=runner)
                self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())

    def test_engine_root_is_added_when_absent_for_standalone_execution(self):
        """The shared classifier import works without PYTHONPATH=src."""
        src = str(PATH.parents[1])
        original = list(sys.path)
        try:
            sys.path[:] = [entry for entry in sys.path if entry != src]
            spec.loader.exec_module(route)
        finally:
            sys.path[:] = original
        self.assertIn(src, sys.path)
        self.assertTrue(callable(route.is_opencode_429))

    def test_stalled_429_is_terminated_early_and_reported_as_quota_exhausted(self):
        """The provider answers 429 but `opencode run` never exits: do not wait."""
        # A genuine CLI error event with no trailing newline: the stalled child
        # may leave a partial line, and the probe must still see the 429.
        event = ('{"type":"error","error":{"name":"FreeUsageLimitError",'
                 '"data":{"message":"free usage limit reached"}}}')
        process = FakeProcess(stdout=event, hang=True)
        def runner(command, **kwargs):
            return process
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                started = route.time.monotonic()
                with self.assertRaises(route.BurnedRunner) as caught:
                    route.semantic_probe(A, "review", 3, runner=runner)
                elapsed = route.time.monotonic() - started
                self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())
        # Early termination, not the generic timeout, is the observable outcome;
        # removing the stall detection makes this fail (RouteError "timeout").
        self.assertEqual(str(caught.exception), "quota_exhausted")
        self.assertTrue(process.terminated)
        self.assertLess(elapsed, 2)

    def test_response_prose_mentioning_429_does_not_stall_a_healthy_probe(self):
        """A JSON text event is model prose, never provider-error evidence."""
        prose = ('{"type":"text","part":{"type":"text",'
                 '"text":"error HTTP 429 too many requests rate limit quota"}}')
        process = FakeProcess(stdout=prose, hang=True)
        def runner(command, **kwargs):
            return process
        started = route.time.monotonic()
        with self.assertRaisesRegex(route.RouteError, "^timeout$"):
            route.semantic_probe(A, "review", 0.3, runner=runner)
        self.assertTrue(process.terminated)
        self.assertLess(route.time.monotonic() - started, 3)

    def test_retryable_rate_limit_message_does_not_kill_the_probe(self):
        """A retried message alone is not hard evidence, so the probe runs on."""
        event = ('{"type":"error","error":{"name":"APIError",'
                 '"data":{"message":"rate limit exceeded, retrying"}}}')
        process = FakeProcess(stdout=event, hang=True)
        def runner(command, **kwargs):
            return process
        started = route.time.monotonic()
        with self.assertRaisesRegex(route.RouteError, "^timeout$"):
            route.semantic_probe(A, "review", 0.3, runner=runner)
        self.assertTrue(process.terminated)
        self.assertLess(route.time.monotonic() - started, 3)

    def test_stalled_429_is_detected_across_reader_chunks(self):
        """Chunked reads still see a signature split across read boundaries."""
        event = ('unrelated line\n{"type":"error","error":'
                 '{"name":"FreeUsageLimitError"}}')
        process = FakeProcess(stdout=event, hang=True)
        def runner(command, **kwargs):
            return process
        with patch.object(route, "PROBE_READ_CHUNK", 5):
            with self.assertRaises(route.BurnedRunner) as caught:
                route.semantic_probe(A, "review", 3, runner=runner)
        self.assertEqual(str(caught.exception), "quota_exhausted")

    def test_stall_pattern_requires_a_structured_error_event(self):
        self.assertTrue(route.QUOTA_STALL.search(
            '{"type":"error","error":{"name":"FreeUsageLimitError"}}'))
        self.assertTrue(route.QUOTA_STALL.search(
            '{"type":"error","error":{"name":"APIError",'
            '"data":{"statusCode":429}}}'))
        # A bare message is not terminating evidence: the CLI may retry and
        # succeed, and killing the probe would burn the whole selection.
        for log in (
            '{"type":"error","error":{"name":"UsageLimitExceeded",'
            '"data":{"message":"Too Many Requests"}}}',
            '{"type":"error","error":{"name":"APIError",'
            '"data":{"message":"rate limit exceeded; retrying"}}}',
            '{"type":"error","error":{"message":"usage limit reached"}}',
        ):
            with self.subTest(log=log):
                self.assertFalse(route.QUOTA_STALL.search(log))
        # Partial line: the signature arrives before the object is complete.
        self.assertTrue(route.QUOTA_STALL.search(
            '{"type":"error","error":{"name":"APIError","statusCode":429'))
        # Prose, tool output, and token counters are never provider evidence.
        for log in ('{"type":"text","part":{"type":"text",'
                    '"text":"error HTTP 429 quota"}}',
                    '{"type":"tool_use","part":{"state":{"output":"429 error"}}}',
                    '{"type":"step_finish","part":{"tokens":{"input":429}}}'):
            with self.subTest(log=log):
                self.assertFalse(route.QUOTA_STALL.search(log))

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals and a sh")
    @unittest.skipUnless(shutil.which("sh"), "requires a POSIX sh")
    def test_real_stalled_probe_escalates_and_reaps_the_child(self):
        """A real child ignoring SIGTERM needs terminate->wait->kill and reaping."""
        event = ('{"type":"error","error":{"name":"FreeUsageLimitError",'
                 '"data":{"message":"free usage limit reached"}}}')
        spawned = []
        def runner(command, **kwargs):
            # Print the genuine error event, then ignore SIGTERM (the ignored
            # disposition survives exec) so only the kill escalation stops it.
            script = ("trap '' TERM; printf '%s' '" + event + "'; exec sleep 10")
            process = subprocess.Popen(["sh", "-c", script], **kwargs)
            spawned.append(process)
            return process
        with patch.object(route, "PROBE_STOP_SECONDS", 0.2):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                    started = route.time.monotonic()
                    with self.assertRaises(route.BurnedRunner) as caught:
                        route.semantic_probe(A, "review", 5, runner=runner)
                    elapsed = route.time.monotonic() - started
                    self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())
        self.assertEqual(str(caught.exception), "quota_exhausted")
        # Well above the real escalate/kill/join bound (well under a second
        # here) yet far below the child's own 10-second sleep, so a probe
        # that failed to escalate still fails this test.
        self.assertLess(elapsed, 8)
        self.assertEqual(len(spawned), 1)
        # SIGTERM was ignored, so -SIGKILL proves the escalation ran.
        self.assertEqual(spawned[0].returncode, -signal.SIGKILL)
        # The probe joined its reader threads before returning. Only live
        # drain readers are checked so unrelated background threads cannot
        # make this flake on a loaded runner.
        self.assertEqual(
            [thread for thread in threading.enumerate()
             if thread.is_alive()
             and getattr(thread, "_target", None) is route._drain],
            [])
        # The child was waited for: waitpid finds no zombie to reap.
        with self.assertRaises(ChildProcessError):
            os.waitpid(spawned[0].pid, os.WNOHANG)
        for stream in (spawned[0].stdout, spawned[0].stderr):
            stream.close()

    def test_failing_probe_without_quota_evidence_is_an_execution_error(self):
        """A non-zero exit with no 429 evidence is a plain execution failure."""
        def runner(command, **kwargs):
            return FakeProcess(
                stdout='{"type":"text","part":{"type":"text","text":"partial"}}\n',
                stderr="opencode: something went wrong\n", returncode=1)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaises(route.RouteError) as caught:
                    route.semantic_probe(A, "review", 30, runner=runner)
                # No quota marker: this route is not burned, only failed.
                self.assertFalse(
                    (pathlib.Path(tmp) / "continuum-opencode-429").exists())
        self.assertEqual(str(caught.exception), "execution_error")
        self.assertNotIsInstance(caught.exception, route.BurnedRunner)

    def test_stalled_probe_without_quota_still_reports_timeout(self):
        process = FakeProcess(hang=True)
        def runner(command, **kwargs):
            return process
        started = route.time.monotonic()
        with self.assertRaisesRegex(route.RouteError, "^timeout$"):
            route.semantic_probe(A, "review", 0.3, runner=runner)
        elapsed = route.time.monotonic() - started
        self.assertTrue(process.terminated)
        self.assertLess(elapsed, 3)

    def test_drain_reassembles_a_multibyte_character_split_across_chunks(self):
        sink = []
        route._drain(_ChunkedStream([b"\xf0\x9f", b"\x98\x80"]), sink)
        self.assertEqual("".join(sink), "\U0001F600")

    def test_drain_flushes_an_incomplete_sequence_at_eof(self):
        sink = []
        route._drain(_ChunkedStream([b"\xe2\x82"]), sink)
        self.assertEqual("".join(sink), "�")

    def test_drain_stops_quietly_when_the_pipe_fails(self):
        class Failing(_ChunkedStream):
            def read1(self, size: int = -1) -> bytes:
                if not self._chunks:
                    raise OSError("pipe closed")
                return super().read1(size)

        sink = []
        route._drain(Failing([b"partial"]), sink)
        self.assertEqual("".join(sink), "partial")

    def test_probe_returns_while_a_reader_is_still_blocked_on_the_pipe(self):
        """A live reader holds the buffer lock; closing its pipe would hang.

        A child or grandchild can keep the pipe open after terminate/kill, so
        the reader never sees EOF. The probe must return on its timeout budget
        instead of waiting for a ``close`` that blocks on the reader's lock.
        """
        gate = threading.Lock()
        release = threading.Event()

        class StuckStream:
            def __init__(self):
                self.buffer = self

            def read1(self, size: int = -1) -> bytes:
                with gate:
                    release.wait(30)
                return b""

            def close(self) -> None:
                if not gate.acquire(timeout=1):
                    raise AssertionError("close blocked on a live reader")
                gate.release()

        process = FakeProcess(hang=True)
        process.stdout = StuckStream()
        process.stderr = StuckStream()
        def runner(command, **kwargs):
            return process
        started = route.time.monotonic()
        try:
            with patch.object(route, "PROBE_JOIN_SECONDS", 0.2):
                with self.assertRaisesRegex(route.RouteError, "^timeout$"):
                    route.semantic_probe(A, "review", 0.2, runner=runner)
        finally:
            release.set()
        elapsed = route.time.monotonic() - started
        self.assertTrue(process.terminated)
        self.assertLess(elapsed, 2)


class MainBurnedRunnerTests(unittest.TestCase):
    def _main_with(self, error):
        stderr = io.StringIO()
        with patch.object(route, "run", side_effect=error):
            with patch("sys.stderr", stderr):
                code = route.main()
        return code, stderr.getvalue()

    def test_exhausted_quota_reason_is_surfaced_in_the_error_annotation(self):
        code, message = self._main_with(route.BurnedRunner("quota_exhausted"))
        self.assertEqual(code, 75)
        self.assertIn("retire this runner without another LLM request", message)
        self.assertIn(route.QUOTA_EXHAUSTED, message)

    def test_generic_pre_burned_notice_stays_stable(self):
        code, message = self._main_with(route.BurnedRunner("runner already burned"))
        self.assertEqual(code, 75)
        self.assertEqual(
            message.strip(),
            "::error::OpenCode 429: retire this runner without another LLM request")


class CompletionTicketTests(unittest.TestCase):
    def test_probed_at_selection_and_real_completion_after_ttl(self):
        """TTL controls admission, never invalidates a task already admitted."""
        ticket = route.execution_ticket({"GITHUB_RUN_ID": "703", "GITHUB_RUN_ATTEMPT": "1"})
        state = {"fingerprint": "f", "last_successful_probe": {"model": A, "at": 1000}}
        state = route.reserve_completion(state, ticket, A, now=1059, probe_at=1000)
        self.assertEqual(state["last_successful_probe"]["at"], 1000)
        state = route.attest_completion(
            state, ticket, A, now=1090, fingerprint="f", ttl=60,
            execution_id="703/1", head="sha")
        self.assertEqual(state["last_successful_real_execution"]["model"], A)
        self.assertEqual(state["last_successful_real_execution"]["head"], "sha")
        self.assertEqual(state["qualified_selections"], {})

    def test_receipt_rejects_other_model_other_run_and_replay(self):
        one = route.execution_ticket({"GITHUB_RUN_ID": "703", "GITHUB_RUN_ATTEMPT": "1"})
        two = route.execution_ticket({"GITHUB_RUN_ID": "703", "GITHUB_RUN_ATTEMPT": "2"})
        state = route.reserve_completion({"fingerprint": "f"}, one, A, 1010, 1000)
        for token, model in ((two, A), (one, B)):
            with self.assertRaisesRegex(route.RouteError, "No qualified selection"):
                route.attest_completion(state, token, model, 1020, "f", 60, "703/1")
        state = route.attest_completion(state, one, A, 1020, "f", 60, "703/1")
        with self.assertRaises(route.RouteError):
            route.attest_completion(state, one, A, 1021, "f", 60, "703/1")

    def test_receipt_rejects_expired_execution_and_unqualified_admission(self):
        ticket = route.execution_ticket({"GITHUB_RUN_ID": "705", "GITHUB_RUN_ATTEMPT": "1"})
        state = route.reserve_completion({"fingerprint": "f"}, ticket, A, 1100, 1000)
        with self.assertRaisesRegex(route.RouteError, "proof expired"):
            route.attest_completion(state, ticket, A, 1110, "f", 60, "705/1")
        state = route.reserve_completion({"fingerprint": "f"}, ticket, A, 1050, 1000)
        with self.assertRaisesRegex(route.RouteError, "proof expired"):
            route.attest_completion(
                state, ticket, A, 1050 + route.MAX_EXECUTION_SECONDS + 1,
                "f", 60, "705/1")

    def test_two_concurrent_runs_keep_isolated_receipts(self):
        x = route.execution_ticket({"GITHUB_RUN_ID": "703", "GITHUB_RUN_ATTEMPT": "1"})
        y = route.execution_ticket({"GITHUB_RUN_ID": "704", "GITHUB_RUN_ATTEMPT": "1"})
        state = {"fingerprint": "f"}
        state = route.reserve_completion(state, x, A, 1000, 1000)
        state = route.reserve_completion(state, y, B, 1001, 1001)
        state = route.attest_completion(state, x, A, 1050, "f", 60, "703/1")
        self.assertIn(y, state["qualified_selections"])
        state = route.attest_completion(state, y, B, 1060, "f", 60, "704/1")
        self.assertNotIn(x, state["qualified_selections"])
        self.assertEqual(state["last_successful_real_execution"]["model"], B)

    def test_execution_ticket_requires_valid_actions_identity(self):
        with self.assertRaises(route.RouteError):
            route.execution_ticket({"GITHUB_RUN_ID": "", "GITHUB_RUN_ATTEMPT": "1"})
        with self.assertRaises(route.RouteError):
            route.execution_ticket({"GITHUB_RUN_ID": "not-numeric", "GITHUB_RUN_ATTEMPT": "1"})


class RealExecutionFailureTests(unittest.TestCase):
    def _selection(self, *, ticket="run", model=A, at=1000):
        return route.reserve_completion({"fingerprint": "f"}, ticket, model, at, at)

    def test_provider_timeout_consumes_receipt_and_routes_to_next_model(self):
        state = self._selection()
        state["last_successful_real_execution"] = {"model": A, "at": 900}
        updated = route.attest_retryable_failure(
            state, "run", A, 1200, "f", "timeout", 900)
        self.assertEqual(updated["qualified_selections"], {})
        self.assertEqual(updated["cooldowns"][A], 4800)
        self.assertEqual(route.route_order(
            route.Configuration.parse(f"{A},{B}"), updated, 1201), (B,))
        self.assertEqual(updated["last_successful_real_execution"]["model"], A)

    def test_malformed_live_review_response_retires_only_qualified_model(self):
        state = self._selection()
        updated = route.attest_retryable_failure(
            state, "run", A, 1200, "f", "invalid_review_format", 900)
        self.assertEqual(updated["qualified_selections"], {})
        self.assertEqual(updated["cooldowns"][A], 2100)
        self.assertEqual(
            updated["last_failed_real_execution"],
            {"model": A, "at": 1200, "reason": "invalid_review_format"})
        self.assertEqual(
            route.route_order(route.Configuration.parse(f"{A},{B}"),
                              updated, 1201), (B,))

    def test_invalid_reason_and_wrong_run_cannot_retire_model(self):
        state = self._selection()
        for ticket, model, reason in (
            ("other", A, "timeout"), ("run", B, "timeout"),
            ("run", A, "code_failure"), ("run", A, "review_quality")):
            with self.subTest(ticket=ticket, model=model, reason=reason):
                with self.assertRaises(route.RouteError):
                    route.attest_retryable_failure(
                        state.copy(), ticket, model, 1200, "f", reason, 900)
        self.assertNotIn("cooldowns", state)

    def test_expired_selection_replay_and_stale_pool_are_rejected(self):
        state = self._selection()
        for now, fingerprint in (
            (1000 + route.MAX_EXECUTION_SECONDS + 1, "f"),
            (1100, "changed")):
            with self.assertRaises(route.RouteError):
                route.attest_retryable_failure(
                    state.copy(), "run", A, now, fingerprint, "timeout", 900)
        updated = route.attest_retryable_failure(
            state, "run", A, 1100, "f", "timeout", 900)
        with self.assertRaises(route.RouteError):
            route.attest_retryable_failure(
                updated, "run", A, 1101, "f", "timeout", 900)

    def test_live_failure_does_not_cancel_other_inflight_execution(self):
        state = self._selection()
        state = route.reserve_completion(state, "other", B, 1001, 1001)
        updated = route.attest_retryable_failure(
            state, "run", A, 1030, "f", "upstream", 900)
        self.assertEqual(updated["cooldowns"][A], 1930)
        self.assertIn("other", updated["qualified_selections"])
        self.assertNotIn("run", updated["qualified_selections"])
        self.assertEqual(route.route_order(
            route.Configuration.parse(f"{A},{B}"), updated, 1031), (B,))


class ReadAfterWriteReceiptTests(unittest.TestCase):
    def test_real_provider_failure_retries_stale_ref_without_requalification(self):
        model = A
        ticket = route.execution_ticket({"GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "2"})
        fresh = route.reserve_completion({"fingerprint": "f"}, ticket, model, 1000, 999)
        seen = []
        waits = []

        class EventuallyConsistentStore:
            def update(self, key, mutator):
                seen.append(key)
                current = {} if len(seen) < 3 else fresh.copy()
                return mutator(current)

        def failure(state):
            return route.attest_retryable_failure(
                state, ticket, model, 1100, "f", "timeout", 900)

        output = route.persist_bound_receipt(
            EventuallyConsistentStore(), "scope", failure, sleep=waits.append)
        self.assertEqual(waits, [1, 2])
        self.assertEqual(seen, ["scope"] * 3)
        self.assertEqual(output["cooldowns"][model], 4700)
        self.assertNotIn(ticket, output["qualified_selections"])

    def test_stale_ref_does_not_forge_success_or_weaken_authorization(self):
        waits = []

        class PermanentlyStale:
            def update(self, key, mutator):
                return mutator({"fingerprint": "f", "qualified_selections": {}})

        def missing(state):
            return route.attest_completion(
                state, "expected-run-receipt", A, 1100, "f", 3600, "789/1")

        with self.assertRaisesRegex(route.SelectionNotVisible, "No qualified selection"):
            route.persist_bound_receipt(PermanentlyStale(), "scope", missing, sleep=waits.append)
        self.assertEqual(waits, [1, 2, 4])

        wrong = route.reserve_completion({"fingerprint": "f"}, "expected-run-receipt",
                                        B, 1000, 1000)
        called = []
        class WrongModel:
            def update(self, key, mutator):
                return mutator(wrong)
        with self.assertRaises(route.RouteError):
            route.persist_bound_receipt(WrongModel(), "scope", missing, sleep=called.append)
        self.assertEqual(called, [])

    def test_route_state_http_get_requires_uncached_representation(self):
        import json as _json
        from unittest.mock import patch as _patch
        class FakeReply:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *args): return b'{}'
        seen = []
        def urlopen(request, timeout):
            seen.append(request)
            return FakeReply()
        with _patch.object(route.urllib.request, "urlopen", urlopen):
            store = route.GitRefStore("owner/repository", "mock-token")
            self.assertEqual(store.api("GET", "git/ref/heads/continuum-model-state"), {})
        self.assertEqual(seen[0].get_header("Cache-control"), "no-cache, no-store")
        self.assertEqual(seen[0].get_header("Pragma"), "no-cache")


class StateStoreFailureIsolationTests(unittest.TestCase):
    def test_state_failure_does_not_cool_down_a_qualified_route(self):
        import os
        config = route.Configuration.parse(A)
        fingerprint = config.fingerprint("d", "anonymous")
        state = {
            "fingerprint": fingerprint,
            "last_successful_probe": {"model": A, "at": int(route.time.time())},
        }
        calls = {"probe": 0, "update": 0}
        store = type("Store", (), {})()
        store.read = lambda key: (state, None, None)

        def conflict_update(key, mutator):
            calls["update"] += 1
            raise route.Conflict("state CAS budget exhausted")
        store.update = conflict_update

        env = {
            "OPENCODE_MODELS": A,
            "CONTINUUM_MODEL_STATE_TOKEN": "t",
            "GITHUB_REPOSITORY": "owner/parent",
            "GITHUB_RUN_ID": "703",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with patch.object(route, "binary_provenance", lambda: ("1.2.3", "d")), \
             patch.object(route, "GitRefStore", lambda owner, token: store), \
             patch.object(route, "semantic_probe",
                          lambda *a, **k: calls.__setitem__("probe", calls["probe"] + 1)), \
             patch.object(route.sys, "argv", ["model_routing.py", "select"]), \
             patch.dict(os.environ, env, clear=True):
            with self.assertRaises(route.Conflict):
                route.run()
        # One CAS attempt, no cooldown write, and no failover probe: a state
        # failure is not a model-provider failure.
        self.assertEqual(calls["update"], 1)
        self.assertEqual(calls["probe"], 0)

    def test_cooldown_write_failure_cannot_mask_burned_runner(self):
        import os
        config = route.Configuration.parse(A)
        fingerprint = config.fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint}
        calls = {"update": 0}
        store = type("Store", (), {})()
        store.read = lambda key: (state, None, None)

        def conflict_update(key, mutator):
            calls["update"] += 1
            raise route.Conflict("state CAS budget exhausted")
        store.update = conflict_update

        def burned_probe(*args, **kwargs):
            raise route.BurnedRunner("429")
        env = {
            "OPENCODE_MODELS": A,
            "CONTINUUM_MODEL_STATE_TOKEN": "t",
            "GITHUB_REPOSITORY": "owner/parent",
            "GITHUB_RUN_ID": "703",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        with patch.object(route, "binary_provenance", lambda: ("1.2.3", "d")), \
             patch.object(route, "GitRefStore", lambda owner, token: store), \
             patch.object(route, "semantic_probe", burned_probe), \
             patch.object(route.sys, "argv", ["model_routing.py", "select"]), \
             patch.dict(os.environ, env, clear=True):
            # The burned-runner exit code drives fresh-VM recovery; a
            # best-effort cooldown write failure must not replace it.
            self.assertEqual(route.main(), 75)
        self.assertEqual(calls["update"], 1)


class SelectionLoopClosureBindingTests(unittest.TestCase):
    """Each selection-loop mutator binds the model of its own iteration."""

    def _run(self, store, probe, *, models=None):
        import os
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(route, "binary_provenance", lambda: ("1.2.3", "d")), \
             patch.object(route, "GitRefStore", lambda owner, token: store), \
             patch.object(route, "semantic_probe", probe), \
             patch.object(route.sys, "argv", ["model_routing.py", "select"]), \
             patch.dict(os.environ, {
                 "OPENCODE_MODELS": models or f"{A},{B}",
                 "CONTINUUM_MODEL_STATE_TOKEN": "t",
                 "GITHUB_REPOSITORY": "owner/parent",
                 "GITHUB_RUN_ID": "703",
                 "GITHUB_RUN_ATTEMPT": "1",
                 "RUNNER_TEMP": tmp,
             }, clear=True):
            return route.main()

    def _store(self, state, apply):
        store = type("Store", (), {})()
        store.read = lambda key: (dict(state), None, None)
        writes = []

        def update(key, mutator):
            writes.append(mutator(dict(state)) if apply else mutator)
            return writes[-1] if apply else dict(state)
        store.update = update
        return store, writes

    def test_burned_runner_cools_the_probed_model_not_a_later_one(self):
        fingerprint = route.Configuration.parse(
            f"{A},{B}").fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint}
        store, writes = self._store(state, apply=True)

        def probe(model, *args, **kwargs):
            self.assertEqual(model, A)
            raise route.BurnedRunner("429")

        self.assertEqual(self._run(store, probe), 75)
        self.assertEqual(len(writes), 1)
        self.assertIn(A, writes[0]["cooldowns"])
        self.assertNotIn(B, writes[0]["cooldowns"])

    def test_burned_runner_pause_mutator_binds_the_probed_model(self):
        fingerprint = route.Configuration.parse(
            f"{A},{B}").fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint}
        store, deferred = self._store(state, apply=False)

        def probe(model, *args, **kwargs):
            self.assertEqual(model, A)
            raise route.BurnedRunner("429")

        self.assertEqual(self._run(store, probe), 75)
        self.assertEqual(len(deferred), 1)
        # The loop aborts on the burned iteration, so replaying the applied
        # write cannot expose a late-bound `model`. Assert the binding on the
        # captured mutator itself: a plain `def pause(current)` revert must
        # fail here.
        self.assertEqual(deferred[0].__kwdefaults__, {"selected_model": A})
        saved = deferred[0](dict(state))
        self.assertIn(A, saved["cooldowns"])
        self.assertNotIn(B, saved["cooldowns"])

    def test_plain_route_error_cools_failed_model_and_selects_the_next(self):
        fingerprint = route.Configuration.parse(
            f"{A},{B}").fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint}
        store, writes = self._store(state, apply=True)

        def probe(model, *args, **kwargs):
            if model == A:
                raise route.RouteError("probe failed")

        self.assertEqual(self._run(store, probe), 0)
        self.assertEqual(len(writes), 2)
        self.assertIn(A, writes[0]["cooldowns"])
        self.assertNotIn(B, writes[0]["cooldowns"])
        self.assertEqual(writes[1]["last_successful_probe"]["model"], B)

    def test_save_mutator_binds_the_model_selected_for_its_iteration(self):
        fingerprint = route.Configuration.parse(
            f"{A},{B}").fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint}
        store, deferred = self._store(state, apply=False)

        def probe(model, *args, **kwargs):
            if model == A:
                raise route.RouteError("probe failed")

        self.assertEqual(self._run(store, probe), 0)
        self.assertEqual(len(deferred), 2)
        # The loop returns on B, so replaying the mutators after `run()` would
        # still see the final `model` value even without a per-iteration
        # binding. Assert the binding directly: each mutator's keyword default
        # must name the model of its own iteration, so reverting either
        # `def save(current)` or `def pause(current)` fails here.
        self.assertEqual(deferred[0].__kwdefaults__, {"selected_model": A})
        self.assertEqual(deferred[1].__kwdefaults__, {"selected_model": B})
        # Replay the captured mutators after the loop has finished, i.e.
        # after `model` has moved on. Each must still act on its own model.
        saved = dict(state)
        for mutator in deferred:
            saved = mutator(saved)
        self.assertIn(A, saved["cooldowns"])
        self.assertEqual(saved["last_successful_probe"]["model"], B)


class GitRefStoreTests(unittest.TestCase):
    def test_first_write_creates_orphan_ref_not_main(self):
        store = route.GitRefStore("example/private", "fake-token")
        calls = []
        def api(method, path, value=None, *, missing=False):
            calls.append((method, path, value))
            if method == "GET":
                self.assertEqual(path, "git/ref/heads/continuum-model-state")
                self.assertTrue(missing)
                return None
            if path == "git/trees":
                self.assertNotIn("base_tree", value)
                return {"sha": "test-tree"}
            if path == "git/commits":
                self.assertEqual(value["parents"], [])
                return {"sha": "test-commit"}
            if path == "git/refs":
                self.assertEqual(value["ref"], "refs/heads/continuum-model-state")
                return {}
            raise AssertionError(path)
        store.api = api
        outcome = store.update("scope", lambda current: {"attempts": 1})
        self.assertEqual(outcome["schema"], 1)
        self.assertTrue(any(method == "POST" and path == "git/refs"
                            for method, path, _ in calls))
        self.assertFalse(any("refs/heads/main" in path for _, path, _ in calls))

    def test_conflicting_fast_forward_cas_retries_with_fresh_head(self):
        store = route.GitRefStore("example/private", "fake-token")
        attempts = [0]
        heads = ["old-head", "new-head"]
        def api(method, path, value=None, *, missing=False):
            if method == "GET" and path == "git/ref/heads/continuum-model-state":
                head = heads[min(attempts[0], 1)]
                return {"object": {"sha": head}}
            if method == "GET" and path.startswith("git/commits/"):
                return {"tree": {"sha": "tree-" + path.split("/")[-1]}}
            if method == "GET" and path.startswith("git/trees/"):
                return {"tree": []}
            if method == "POST" and path == "git/trees":
                self.assertIn("base_tree", value)
                return {"sha": "new-tree"}
            if method == "POST" and path == "git/commits":
                expected = heads[min(attempts[0], 1)]
                self.assertEqual(value["parents"], [expected])
                return {"sha": "new-commit"}
            if method == "PATCH" and path == "git/refs/heads/continuum-model-state":
                self.assertFalse(value["force"])
                attempts[0] += 1
                if attempts[0] == 1:
                    raise route.Conflict("race")
                return {}
            raise AssertionError((method, path))
        store.api = api
        self.assertEqual(store.update("key", lambda state: {"success": True})["success"], True)
        self.assertEqual(attempts[0], 2)

    def test_no_credential_fails_closed(self):
        with self.assertRaises(route.RouteError):
            route.GitRefStore("example/private", "")

class VerifiedReceiptTests(unittest.TestCase):
    """Verify an immutable attested receipt without a fresh LLM query."""

    def state(self, **overrides):
        receipt = {"model": A, "at": 1234, "run": "703/1", "head": "sha"}
        receipt.update(overrides)
        return {"fingerprint": "f", "last_successful_real_execution": receipt}

    def test_verified_receipt_returns_bound_identity(self):
        receipt = route.verified_receipt(
            self.state(), "f", expected_model=A, expected_run="703/1", expected_head="sha")
        self.assertEqual(receipt["model"], A)
        self.assertEqual(receipt["run"], "703/1")
        self.assertEqual(receipt["head"], "sha")

    def test_missing_receipt_fails_closed(self):
        with self.assertRaisesRegex(route.RouteError, "No attested"):
            route.verified_receipt({"fingerprint": "f"}, "f")
        with self.assertRaisesRegex(route.RouteError, "No attested"):
            route.verified_receipt({"fingerprint": "f", "last_successful_real_execution": {}}, "f")

    def test_stale_fingerprint_fails_closed(self):
        with self.assertRaisesRegex(route.RouteError, "runtime route"):
            route.verified_receipt(self.state(), "other")

    def test_mismatched_handoff_fails_closed(self):
        for kwargs in (
            {"expected_model": B},
            {"expected_run": "999/1"},
            {"expected_head": "other"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(route.RouteError):
                route.verified_receipt(self.state(), "f", **kwargs)

    def test_missing_head_is_tolerated_when_unexpected(self):
        state = self.state(head="")
        receipt = route.verified_receipt(state, "f", expected_model=A)
        self.assertEqual(receipt["head"], "")

    def test_explicit_empty_head_is_verified_exactly(self):
        receipt = route.verified_receipt(
            self.state(head=""), "f", expected_head="")
        self.assertEqual(receipt["head"], "")
        with self.assertRaisesRegex(route.RouteError, "exact expected HEAD"):
            route.verified_receipt(self.state(head="sha"), "f", expected_head="")

    def test_verify_operation_reads_receipt_without_probing_or_writing(self):
        import os
        import sys
        import tempfile
        config = route.Configuration.parse(f"{A},{B}")
        fingerprint = config.fingerprint("d", "anonymous")
        state = {"fingerprint": fingerprint, "last_successful_real_execution": {
            "model": A, "at": 1234, "run": "703/1", "head": "sha"}}
        calls = {"probe": 0, "update": 0}
        store = type("Store", (), {})()
        store.read = lambda key: (state, None, None)

        def no_update(key, mut):
            calls["update"] += 1
            raise AssertionError("verify must not write")
        store.update = no_update

        def no_ticket(*args, **kwargs):
            raise AssertionError("verify needs no run ticket")
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "OPENCODE_MODELS": f"{A},{B}", "CONTINUUM_MODEL_STATE_TOKEN": "t",
                "GITHUB_REPOSITORY": "owner/parent",
                "CONTINUUM_EXPECTED_MODEL": A, "CONTINUUM_EXPECTED_RUN": "703/1",
                "GITHUB_OUTPUT": str(pathlib.Path(tmp) / "out"),
            }
            with patch.object(route, "binary_provenance", lambda: ("1.2.3", "d")), \
                 patch.object(route, "GitRefStore", lambda owner, token: store), \
                 patch.object(route, "execution_ticket", no_ticket), \
                 patch.object(route.sys, "argv", ["model_routing.py", "verify"]), \
                 patch.object(route, "semantic_probe",
                              lambda *a, **k: calls.__setitem__("probe", calls["probe"] + 1)), \
                 patch.dict(os.environ, env, clear=True):
                rc = route.run()
        self.assertEqual(rc, 0)
        self.assertEqual(calls["probe"], 0)
        self.assertEqual(calls["update"], 0)


class CooldownRecoveryContractTests(unittest.TestCase):
    def test_all_cooling_exposes_earliest_future_epoch_to_followup_step(self):
        config = route.Configuration.parse(f"{A},{B},{C}")
        with tempfile.TemporaryDirectory() as tmp:
            file = pathlib.Path(tmp) / "github-env"
            actual = route.publish_cooldown_not_before(
                config, {"cooldowns": {A: 4600, B: 3900, C: 4300}},
                now=1000, env={"GITHUB_ENV": str(file)})
            self.assertEqual(actual, 3900)
            self.assertEqual(
                file.read_text(), "CONTINUUM_MODEL_NOT_BEFORE_EPOCH=3900\n")

    def test_never_defer_if_any_model_ready_or_state_invalid(self):
        config = route.Configuration.parse(f"{A},{B}")
        for state in (
            {}, {"cooldowns": {A: 2000}},
            {"cooldowns": {A: 2000, B: 1000}},
            {"cooldowns": {A: 2000, B: "not-a-number"}},
        ):
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as tmp:
                    file = pathlib.Path(tmp) / "github-env"
                    self.assertIsNone(route.publish_cooldown_not_before(
                        config, state, now=1000, env={"GITHUB_ENV": str(file)}))
                    self.assertFalse(file.exists())

    def test_recovery_can_persist_wait_even_when_runtime_bundle_missing(self):
        workflow = (PATH.parents[2] / ".github/workflows/continuum-pr-agent.yml").read_text()
        marker = "      - name: Update persistent PR-Agent retry controller state"
        stop = "      - name: Schedule bounded retry for retryable PR-Agent review failure"
        block = workflow.split(marker, 1)[1].split(stop, 1)[0]
        self.assertIn("CONTINUUM_MODEL_NOT_BEFORE_EPOCH", block)
        self.assertIn("continuum-lifecycle-retry head=", block)
        self.assertIn("not-before=", block)
        self.assertIn("const exhausted = !cooling", block)
        self.assertIn("policy.controllerStateBody(stateMarker, summary)", block)
        self.assertLess(
            workflow.index("      - name: Resolve the Continuum-owned PR-Agent runtime bundle"),
            workflow.index("      - name: Qualify trusted review-model route"),
            "Controller-state policy must exist before model selection can fail")


    def test_repair_recovery_uses_same_cooldown_deadline(self):
        workflow = (PATH.parents[2] / ".github/workflows/continuum-pr-agent-repair.yml").read_text()
        block = workflow.split(
            "      - name: Update persistent PR-Agent repair retry controller state", 1
        )[1].split(
            "      - name: Schedule bounded retry for retryable PR-Agent repair failure", 1
        )[0]
        self.assertIn("CONTINUUM_MODEL_NOT_BEFORE_EPOCH", block)
        self.assertIn("kind=repair attempt=", block)
        self.assertIn("not-before=", block)
        self.assertIn("const exhausted = !cooling", block)



    def test_real_provider_failure_exposes_next_available_model(self):
        config = route.Configuration.parse(f"{A},{B}")
        now = 1000
        reserved = route.reserve_completion(
            {"fingerprint": "f", "cooldowns": {B: 1300}},
            "ticket", A, now - 5, now - 5)
        retired = route.attest_retryable_failure(
            reserved, "ticket", A, now, "f", "timeout", 900)
        self.assertEqual(retired["cooldowns"][A], 4600)
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "env"
            deadline = route.publish_cooldown_not_before(
                config, retired, now, env={"GITHUB_ENV": str(output)})
            self.assertEqual(deadline, 1300)
            self.assertEqual(
                output.read_text(), "CONTINUUM_MODEL_NOT_BEFORE_EPOCH=1300\n")

    def test_live_provider_timeout_never_launches_blind_inline_review_or_repair_retry(self):
        repo = PATH.parents[2]
        for filename, cooldown_step, schedule_step in (
            ("continuum-pr-agent.yml",
             "Cool down failed real provider route before the bounded retry",
             "Schedule bounded retry for retryable PR-Agent review failure"),
            ("continuum-pr-agent-repair.yml",
             "Retire verified failed OpenCode repair route",
             "Schedule bounded retry for retryable PR-Agent repair failure"),
        ):
            with self.subTest(workflow=filename):
                text = (repo / ".github/workflows" / filename).read_text()
                self.assertIn(
                    "      - name: " + cooldown_step + "\n        id: model_cooldown",
                    text)
                body = text.split("      - name: " + schedule_step, 1)[1]
                self.assertIn("env.CONTINUUM_MODEL_NOT_BEFORE_EPOCH == ''", body)
                self.assertIn('CONTINUUM_MODEL_NOT_BEFORE_EPOCH:-', body)
                self.assertIn("steps.model_cooldown.outcome == 'success'", text)

class GitHubStateFailureClassificationTests(unittest.TestCase):
    """Issue #398: GitHub state 403 rate limits are not permission failures."""

    def _store(self):
        return route.GitRefStore("example/private-target", "secret-state-token")

    def _error(self, code, headers=None):
        import email.message
        message = email.message.Message()
        for name, value in (headers or {}).items():
            message[name] = value
        return route.urllib.error.HTTPError(
            "https://api.github.invalid/repos/example/private-target/state",
            code, "sanitized", message, None)

    def _raise(self, error):
        def urlopen(request, timeout):
            raise error
        return patch.object(route.urllib.request, "urlopen", urlopen)

    def _failure(self, error):
        with self._raise(error):
            with self.assertRaises(route.GitHubStateUnavailable) as caught:
                self._store().api("POST", "git/trees", {"tree": []})
        return caught.exception

    def test_primary_403_rate_limit_exposes_reset_not_permission(self):
        import time
        reset = int(time.time()) + 120
        failure = self._failure(self._error(403, {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
        }))
        self.assertEqual(failure.classification, "rate_limit")
        self.assertGreaterEqual(failure.not_before, reset)
        self.assertNotIn("secret-state-token", str(failure))
        self.assertNotIn("private-target", str(failure))

    def test_secondary_403_retry_after_is_rate_limit_even_with_budget(self):
        import time
        now = int(time.time())
        reset = now + 3600
        failure = self._failure(self._error(403, {
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(reset),
            "Retry-After": "30",
        }))
        self.assertEqual(failure.classification, "rate_limit")
        self.assertGreaterEqual(failure.not_before, now + 30)
        self.assertLess(failure.not_before, reset)

    def test_permission_403_with_budget_stays_deterministic(self):
        with self._raise(self._error(403, {
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": "9999999999",
        })):
            with self.assertRaises(route.RouteError) as caught:
                self._store().api("POST", "git/trees", {"tree": []})
        self.assertNotIsInstance(caught.exception, route.GitHubStateUnavailable)
        self.assertIn("403", str(caught.exception))

    def test_429_and_retry_after_are_rate_limited(self):
        import time
        now = int(time.time())
        failure = self._failure(self._error(429, {"Retry-After": "45"}))
        self.assertEqual(failure.classification, "rate_limit")
        self.assertGreaterEqual(failure.not_before, now + 45)

    def test_stale_reset_epochs_fall_back_to_the_backstop(self):
        import email.utils
        import time
        now = int(time.time())
        stale = str(now - 300)
        stale_date = email.utils.formatdate(now - 300, usegmt=True)
        cases = (
            (429, {"X-RateLimit-Reset": stale}),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": stale}),
            (403, {"X-RateLimit-Remaining": "4999", "Retry-After": stale_date}),
        )
        for code, headers in cases:
            with self.subTest(code=code, headers=headers):
                failure = self._failure(self._error(code, headers))
                self.assertEqual(failure.classification, "rate_limit")
                self.assertGreaterEqual(
                    failure.not_before,
                    now + route.STATE_RATE_LIMIT_FALLBACK_SECONDS)

    def test_retry_after_zero_is_still_retry_now(self):
        import time
        now = int(time.time())
        failure = self._failure(self._error(429, {"Retry-After": "0"}))
        self.assertEqual(failure.classification, "rate_limit")
        self.assertLess(
            failure.not_before,
            now + route.STATE_RATE_LIMIT_FALLBACK_SECONDS)

    def test_5xx_and_transport_timeout_are_transient(self):
        failure = self._failure(self._error(503, {"Retry-After": "10"}))
        self.assertEqual(failure.classification, "transient")
        self.assertIsNotNone(failure.not_before)

        class TimeoutReply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *args):
                raise TimeoutError("timed out")

        def timed_out(request, timeout):
            return TimeoutReply()

        with patch.object(route.urllib.request, "urlopen", timed_out):
            with self.assertRaises(route.GitHubStateUnavailable) as caught:
                self._store().api("GET", "git/ref/heads/continuum-model-state")
        self.assertEqual(caught.exception.classification, "transient")
        self.assertNotIn("private-target", str(caught.exception))

    def test_5xx_with_remaining_quota_ignores_rate_limit_reset(self):
        import time
        now = int(time.time())
        reset = now + 3600
        failure = self._failure(self._error(503, {
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": str(reset),
        }))
        self.assertEqual(failure.classification, "transient")
        self.assertIsNone(failure.not_before)

    def test_malformed_json_and_cas_conflict_remain_deterministic(self):
        class MalformedReply:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *args):
                return b"not-json"

        def malformed(request, timeout):
            return MalformedReply()

        with patch.object(route.urllib.request, "urlopen", malformed):
            with self.assertRaises(route.RouteError) as caught:
                self._store().api("GET", "git/ref/heads/continuum-model-state")
        self.assertNotIsInstance(caught.exception, route.GitHubStateUnavailable)

        with self._raise(self._error(409)):
            with self.assertRaises(route.Conflict):
                self._store().api("PATCH", "git/refs/heads/continuum-model-state",
                                  {"sha": "x", "force": False})

    def test_cli_record_failure_publishes_sanitized_reset_without_cooldown(self):
        import contextlib
        import io
        import os
        import tempfile
        state = type("Store", (), {})()
        state.update = lambda key, mutator: (_ for _ in ()).throw(
            route.GitHubStateUnavailable("rate_limit", 1234567890))
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "github-env"
            stderr = io.StringIO()
            env = {
                "OPENCODE_MODELS": A,
                "CONTINUUM_SELECTED_MODEL": A,
                "CONTINUUM_MODEL_STATE_TOKEN": "secret-state-token",
                "GITHUB_REPOSITORY": "owner/private-target",
                "GITHUB_RUN_ID": "703",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_ENV": str(target),
            }
            with patch.object(route, "binary_provenance", lambda: ("1.2.3", "d")), \
                 patch.object(route, "GitRefStore", lambda owner, token: state), \
                 patch.object(route.sys, "argv",
                              ["model_routing.py", "record"]), \
                 patch.dict(os.environ, env, clear=True), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(route.main(), 2)
            self.assertEqual(
                target.read_text(),
                "CONTINUUM_MODEL_STATE_TRANSIENT=rate_limit\n"
                "CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH=1234567890\n")
            self.assertNotIn("secret-state-token", stderr.getvalue())
            self.assertNotIn("private-target", stderr.getvalue())

    def test_published_environment_contains_only_classifier_and_reset(self):
        import tempfile
        failure = route.GitHubStateUnavailable("rate_limit", 1234567890)
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "github-env"
            route.publish_state_failure(failure, env={"GITHUB_ENV": str(target)})
            self.assertEqual(
                target.read_text(),
                "CONTINUUM_MODEL_STATE_TRANSIENT=rate_limit\n"
                "CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH=1234567890\n")
        transient = route.GitHubStateUnavailable("transient")
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "github-env"
            route.publish_state_failure(transient, env={"GITHUB_ENV": str(target)})
            self.assertEqual(
                target.read_text(), "CONTINUUM_MODEL_STATE_TRANSIENT=transient\n")


class AttestationRecoveryContractTests(unittest.TestCase):
    """Issue #398: thread only classified attestation failures into recovery."""

    def setUp(self):
        self.workflow = (PATH.parents[2] /
                         ".github/workflows/continuum-pr-agent.yml").read_text()

    def _block(self, start, stop):
        return self.workflow.split(start, 1)[1].split(stop, 1)[0]

    def test_attestation_step_has_stable_id_and_no_provider_cooldown(self):
        attestation = self._block(
            "      - name: Attest successful independent OpenCode review",
            "      - name: Publish private exact-head repair handoff")
        self.assertIn("id: review_attestation", attestation)
        cooldown = self._block(
            "      - name: Cool down failed real provider route before the bounded retry",
            "      - name: Attest successful independent OpenCode review")
        self.assertNotIn("review_attestation", cooldown)
        self.assertIn("provider_failure", cooldown)

    def test_failed_status_guard_requires_classified_transient_attestation(self):
        status = self._block(
            "      - name: Publish failed PR-Agent review state",
            "      - name: Fail closed on a moved head")
        self.assertIn(
            "(steps.review_attestation.outcome == 'failure' && "
            "env.CONTINUUM_MODEL_STATE_TRANSIENT != '')", status)
        self.assertIn("PR-Agent review failed: transient; recovery eligible", status)
        self.assertIn("PR-Agent review failed: deterministic; recovery held", status)

    def test_durable_controller_guard_requires_classified_attestation(self):
        controller = self._block(
            "      - name: Update persistent PR-Agent retry controller state",
            "      - name: Schedule bounded retry for retryable PR-Agent review failure")
        self.assertIn(
            "(steps.review_attestation.outcome == 'failure' &&\n"
            "            env.CONTINUUM_MODEL_STATE_TRANSIENT != '')", controller)
        self.assertIn("CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH", controller)
        self.assertIn("ROUTE_NOT_BEFORE:", controller)
        self.assertIn("ROUTE_UNAVAILABLE:", controller)
        self.assertIn("continuum-lifecycle-retry head=", controller)
        self.assertIn("not-before=", controller)

    def test_bounded_retry_guard_requires_classified_attestation_and_not_before(self):
        schedule = self.workflow.split(
            "      - name: Schedule bounded retry for retryable PR-Agent review failure",
            1)[1]
        self.assertIn(
            "(steps.review_attestation.outcome == 'failure' &&\n"
            "            env.CONTINUUM_MODEL_STATE_TRANSIENT != '')", schedule)
        self.assertIn("env.CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH == ''", schedule)
        self.assertIn('CONTINUUM_MODEL_STATE_NOT_BEFORE_EPOCH:-', schedule)
        self.assertNotIn("operation: fail", schedule)

    def test_success_status_and_merge_require_completed_attestation(self):
        attestation_at = self.workflow.index(
            "      - name: Attest successful independent OpenCode review")
        durable_at = self.workflow.index(
            "      - name: Publish durable PR-Agent review state")
        self.assertLess(attestation_at, durable_at)
        durable = self._block(
            "      - name: Publish durable PR-Agent review state",
            "      - name: Publish failed PR-Agent review state")
        self.assertNotIn("always()", durable)
        self.assertNotIn("failure()", durable)
        merge = self.workflow.split("\n  merge:\n", 1)[1].split("uses:", 1)[0]
        self.assertIn("needs.pr_agent.outputs.ready_to_merge == 'true'", merge)
        self.assertIn("needs.pr_agent.outputs.merge_gate_verified == 'true'", merge)
        self.assertNotIn("always()", merge)

    def test_target_aware_attestation_state_token_is_preserved(self):
        attestation = self._block(
            "      - name: Attest successful independent OpenCode review",
            "      - name: Publish private exact-head repair handoff")
        self.assertIn("state-repository: ${{ env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY }}", attestation)
        self.assertIn(
            "state-token: ${{ env.CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' "
            "&& secrets.TAP_PAT", attestation)


if __name__ == "__main__":
    unittest.main()
