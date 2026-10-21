"""Work Lock #66: routing is policy-driven, bounded and independently tested."""
from __future__ import annotations
import importlib.util
import pathlib
import sys
import tempfile
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
        class Result:
            returncode = 0
            stdout = '{"type":"text","part":{"type":"text","text":"Replace > with >="}}\n'
            stderr = ""
        def runner(command, **kwargs):
            seen.update(kwargs)
            self.assertEqual(command[0:2], ["opencode", "run"])
            return Result()
        with patch.dict("os.environ", {"GH_TOKEN": "no", "GITHUB_TOKEN": "also-no"}, clear=True):
            route.semantic_probe(A, "review", 30, runner=runner)
        self.assertNotIn("GH_TOKEN", seen["env"])
        self.assertNotIn("GITHUB_TOKEN", seen["env"])

    def test_probe_requires_actual_tool_created_file(self):
        class Result:
            returncode = 0
            stdout = '{"type":"text","part":{"type":"text","text":"done"}}\n'
            stderr = ""
        def runner(command, **kwargs):
            return Result()
        with self.assertRaisesRegex(route.RouteError, "coding_tool_capability"):
            route.semantic_probe(A, "coder", 30, runner=runner)

    def test_429_burns_runner_and_skips_any_second_probe(self):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "FreeUsageLimitError 429"
        def runner(command, **kwargs):
            return Result()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"RUNNER_TEMP": tmp}, clear=True):
                with self.assertRaises(route.BurnedRunner):
                    route.semantic_probe(A, "review", 30, runner=runner)
                self.assertTrue((pathlib.Path(tmp) / "continuum-opencode-429").exists())



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

if __name__ == "__main__":
    unittest.main()
