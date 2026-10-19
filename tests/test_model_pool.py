"""Pure contract tests for Work Lock #66's trusted selector; no paid providers."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github/actions/model-select/selector.py"
spec = importlib.util.spec_from_file_location("continuum_model_pool", PATH)
selector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = selector
spec.loader.exec_module(selector)

FIRST = "opencode/deepseek-v4.1-flash-free"
SECOND = "opencode/glm-5.3-flash-free"
RESERVE = "opencode/nemotron-3-ultra-free"
POOL = ",".join((FIRST, SECOND, RESERVE))


class ModelContract(unittest.TestCase):
    def setUp(self):
        self.config = selector.Config.from_values(POOL)

    def test_allowed_order_and_single_model_pin(self):
        self.assertEqual(self.config.models, (FIRST, SECOND, RESERVE))
        self.assertEqual(self.config.order({}, 100), [FIRST, SECOND, RESERVE])
        pin = selector.Config.from_values(POOL, SECOND)
        self.assertEqual(pin.order({}, 100), [SECOND])

    def test_invalid_or_surprise_models_fail_closed(self):
        for bad in ("", ",", POOL + ",", FIRST + "," + FIRST,
                    "opencode/not-allowed", "openai/paid-model",
                    FIRST + ",opencode/nemotron-3-ultra"):
            with self.subTest(bad=bad):
                with self.assertRaises(selector.ConfigError):
                    selector.Config.from_values(bad)
        with self.assertRaises(selector.ConfigError):
            selector.Config.from_values(POOL, "opencode/other-free")
        with self.assertRaises(selector.ConfigError):
            selector.Config.from_values(POOL, ttl="-1")
        with self.assertRaises(selector.ConfigError):
            selector.Config.from_values(POOL, timeout="1")

    def test_state_key_is_scoped_and_opaque(self):
        a = selector.state_key(repo="org/parent", profile="child-a",
                               identity_scope="anonymous", config=self.config)
        b = selector.state_key(repo="org/parent", profile="child-b",
                               identity_scope="anonymous", config=self.config)
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, b)
        self.assertNotIn("child-a", a)
        with self.assertRaises(selector.ConfigError):
            selector.state_key(repo="org/parent", profile="child-a",
                               identity_scope="private-account", config=self.config)

    def test_observed_wrong_model_never_counts_as_pass(self):
        correct = '{"type":"text","part":{"type":"text","text":"12 pencils"}}'
        self.assertEqual(selector.extract_response(correct, expected=FIRST), ("12 pencils", False))
        wrong = '{"type":"step_finish","info":{"providerID":"opencode","modelID":"unexpected-free"}}'
        with self.assertRaises(selector.NoRoute):
            selector.extract_response(correct + "\n" + wrong, expected=FIRST)

    def test_semantic_probe_rejects_echo_upstream_errors_and_nonanswer(self):
        def runner(output, code=0, stderr=""):
            class Process:
                pass
            x = Process()
            x.stdout, x.stderr, x.returncode = output, stderr, code
            return lambda *a, **kw: x
        valid = '{"type":"text","part":{"type":"text","text":"3 by 4 is 12 pencils"}}'
        self.assertTrue(selector.real_probe(FIRST, 30, runner=runner(valid))["ok"])
        self.assertFalse(selector.real_probe(FIRST, 30, runner=runner(valid, code=2))["ok"])
        self.assertFalse(selector.real_probe(FIRST, 30, runner=runner("READY"))["ok"])
        self.assertFalse(selector.real_probe(FIRST, 30, runner=runner('{"type":"text","text":"error 429"}'))["ok"])

    def test_one_attempt_per_candidate_only_before_side_effects(self):
        seen = []
        def probe(model, timeout):
            seen.append(model)
            return {"ok": model == RESERVE, "reason": "ok", "seconds": 1}
        selected, state, attempts = selector.select(self.config, {}, probe=probe, now=100.0)
        self.assertEqual(selected, RESERVE)
        self.assertEqual(seen, [FIRST, SECOND, RESERVE])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(state["last_probe_model"], RESERVE)
        self.assertEqual(state["last_good"], "")
        self.assertGreater(state["cooldowns"][FIRST], 100.0)
        selected2, _, _ = selector.select(self.config, state, probe=probe, now=101.0)
        self.assertEqual(selected2, RESERVE)
        self.assertEqual(seen[-1], RESERVE)

    def test_exhaustion_fails_closed(self):
        def failed(*_):
            return {"ok": False, "reason": "unavailable", "seconds": 0}
        with self.assertRaises(selector.NoRoute):
            selector.select(self.config, {}, probe=failed, now=100)

if __name__ == "__main__":
    unittest.main()
