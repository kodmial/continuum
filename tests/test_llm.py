"""Tests for the generic OpenAI-compatible provider client."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from continuum.review import llm

SECRET = "sk-live-do-not-log-0123456789"


class ModelNormalizationTests(unittest.TestCase):
    def test_routing_prefixes_are_stripped(self):
        for prefix in ("openai/", "text-completion-openai/", "chat-completion-openai/", "litellm/", "openrouter/"):
            with self.subTest(prefix=prefix):
                self.assertEqual(llm.strip_model_prefix(f"{prefix}gpt-4o-mini"), "gpt-4o-mini")

    def test_bare_model_is_unchanged(self):
        self.assertEqual(llm.strip_model_prefix("gpt-4o-mini"), "gpt-4o-mini")

    def test_only_the_first_prefix_is_removed(self):
        self.assertEqual(llm.strip_model_prefix("openai/openai/inner"), "openai/inner")

    def test_routing_adds_the_prefix_when_missing(self):
        self.assertEqual(llm.route_model("gpt-4o-mini"), "openai/gpt-4o-mini")
        self.assertEqual(llm.route_model("openai/gpt-4o-mini"), "openai/gpt-4o-mini")
        self.assertEqual(llm.route_model("text-completion-openai/x"), "text-completion-openai/x")

    def test_empty_model_is_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.route_model("  ")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(llm.route_model("  gpt-4o-mini\n"), "openai/gpt-4o-mini")


class EndpointValidationTests(unittest.TestCase):
    def test_https_is_accepted_and_trimmed(self):
        self.assertEqual(llm.validate_api_base("https://provider.example/v1/"), "https://provider.example/v1")

    def test_loopback_http_is_allowed(self):
        self.assertEqual(llm.validate_api_base("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/v1")

    def test_plain_http_remote_is_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.validate_api_base("http://provider.example/v1")

    def test_non_http_scheme_is_rejected(self):
        for value in ("file:///etc/passwd", "ftp://provider.example", "provider.example/v1"):
            with self.subTest(value=value):
                with self.assertRaises(llm.ProviderConfigError):
                    llm.validate_api_base(value)

    def test_embedded_credentials_are_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.validate_api_base("https://user:pass@provider.example/v1")

    def test_whitespace_is_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.validate_api_base("https://provider.example/ v1")

    def test_empty_is_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.validate_api_base("")


class MaxTokensTests(unittest.TestCase):
    def test_blank_is_none(self):
        self.assertIsNone(llm.parse_max_tokens(""))
        self.assertIsNone(llm.parse_max_tokens(None))

    def test_integer_is_parsed(self):
        self.assertEqual(llm.parse_max_tokens(" 16000 "), 16000)

    def test_invalid_and_non_positive_are_rejected(self):
        for value in ("many", "0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(llm.ProviderConfigError):
                    llm.parse_max_tokens(value)


class RedactionTests(unittest.TestCase):
    def test_secret_is_removed_from_text(self):
        redacted = llm.redact(f"upstream rejected key {SECRET}", SECRET)
        self.assertNotIn(SECRET, redacted)
        self.assertIn("***redacted***", redacted)

    def test_redaction_bounds_output(self):
        self.assertLessEqual(len(llm.redact("x" * 9000, None)), 2000)


class ChatTests(unittest.TestCase):
    def _client(self, opener, **kwargs):
        return llm.OpenAICompatibleClient(
            "https://provider.example/v1",
            SECRET,
            "openai/gpt-4o-mini",
            opener=opener,
            **kwargs,
        )

    def test_model_prefix_is_normalized_and_payload_built(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.data.decode())
            return {"choices": [{"message": {"content": "RESOLVED\nbecause"}}]}

        client = self._client(opener, max_tokens=64)
        answer = client.chat("prompt", system="be precise")
        self.assertEqual(answer, "RESOLVED\nbecause")
        self.assertEqual(captured["url"], "https://provider.example/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "gpt-4o-mini")
        self.assertEqual(captured["body"]["max_tokens"], 64)
        self.assertEqual(captured["body"]["temperature"], 0)
        self.assertEqual(captured["body"]["messages"][0]["content"], "be precise")

    def test_http_error_never_leaks_the_key(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )

        client = self._client(opener)
        with self.assertRaises(llm.ProviderRequestError) as caught:
            client.chat("prompt")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_unexpected_response_shape_is_an_error(self):
        client = self._client(lambda request, timeout: {"unexpected": True})
        with self.assertRaises(llm.ProviderRequestError):
            client.chat("prompt")

    def test_missing_key_is_rejected(self):
        with self.assertRaises(llm.ProviderConfigError):
            llm.OpenAICompatibleClient("https://provider.example/v1", "  ", "model")


class EnvironmentTests(unittest.TestCase):
    def test_client_from_environment(self):
        client = llm.client_from_environment(
            {
                "CONTINUUM_REVIEW_API_BASE": "https://provider.example/v1",
                "CONTINUUM_REVIEW_API_KEY": SECRET,
                "CONTINUUM_REVIEW_MODEL": "openai/gpt-4o-mini",
                "CONTINUUM_REVIEW_MAX_TOKENS": "8000",
            }
        )
        self.assertEqual(client.model, "gpt-4o-mini")
        self.assertEqual(client.max_tokens, 8000)

    def test_missing_settings_are_reported(self):
        missing = llm.missing_provider_settings("", "model", "")
        self.assertEqual(missing, ["api_base", "api_key"])
        self.assertEqual(llm.missing_provider_settings("b", "m", "k"), [])

    def test_missing_settings_are_not_described(self):
        payload = {"model": llm.route_model("gpt-4o-mini")}
        self.assertNotIn(SECRET, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
