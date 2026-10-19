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

if __name__ == "__main__":
    unittest.main()
