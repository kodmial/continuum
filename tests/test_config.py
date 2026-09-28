"""Tests for the `.continuum.yml` contract."""

from __future__ import annotations

import textwrap
import unittest

from continuum import config as config_module


def document(body: str) -> str:
    return textwrap.dedent(body)


class ConfigTests(unittest.TestCase):
    def test_minimal_document_disables_review(self):
        config = config_module.parse_config(document("version: 1\n"))
        self.assertEqual(config.review.provider, config_module.PROVIDER_NONE)
        self.assertFalse(config.review.enabled)
        self.assertTrue(config.review.block_merge)
        self.assertEqual(config.review.status_context, config_module.DEFAULT_STATUS_CONTEXT)

    def test_pr_agent_provider_names_only_settings(self):
        config = config_module.parse_config(
            document(
                """
                version: 1
                review:
                  provider: pr-agent
                  block_merge: false
                  pr_agent:
                    api_base_var: REVIEW_BASE_URL
                    api_key_secret: REVIEW_TOKEN
                    model_var: REVIEW_MODEL
                """
            )
        )
        self.assertTrue(config.review.enabled)
        self.assertFalse(config.review.block_merge)
        self.assertEqual(
            config_module.required_variable_names(config),
            ["PR_AGENT_MAX_TOKENS", "REVIEW_BASE_URL", "REVIEW_MODEL"],
        )
        self.assertEqual(config_module.required_secret_names(config), ["REVIEW_TOKEN"])

    def test_rejects_unsupported_provider(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(document("version: 1\nreview:\n  provider: gemini\n"))

    def test_rejects_unknown_keys(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(document("version: 1\nreviews:\n  provider: pr-agent\n"))
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document("version: 1\nreview:\n  provider: pr-agent\n  gating: strict\n")
            )

    def test_rejects_wrong_version(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(document("version: 2\n"))
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(document("version: one\n"))

    def test_rejects_inline_endpoint_or_model_in_config(self):
        """Endpoints, models, and credentials must be referenced by name."""

        for value in (
            "https://provider.example/v1",
            "gpt-4o-mini",
            "sk-live-0123456789",
        ):
            with self.subTest(value=value):
                with self.assertRaises(config_module.ConfigError):
                    config_module.parse_config(
                        document(
                            """
                            version: 1
                            review:
                              provider: pr-agent
                              pr_agent:
                                api_base_var: {value}
                            """.format(value=value)
                        )
                    )

    def test_rejects_lowercase_secret_name(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document(
                    """
                    version: 1
                    review:
                      provider: pr-agent
                      pr_agent:
                        api_key_secret: pr_agent_api_key
                    """
                )
            )

    def test_rejects_stale_provider_block_when_disabled(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document(
                    """
                    version: 1
                    review:
                      provider: none
                      pr_agent:
                        model_var: PR_AGENT_MODEL
                    """
                )
            )

    def test_rejects_sibling_status_context_collision(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document(
                    """
                    version: 1
                    review:
                      provider: pr-agent
                      status_context: CodeRabbit
                    """
                )
            )

    def test_rejects_non_boolean_block_merge(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document("version: 1\nreview:\n  block_merge: 'yes'\n")
            )

    def test_rejects_newline_in_status_context(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_config(
                document('version: 1\nreview:\n  status_context: "a\\nb"\n')
            )

    def test_missing_file_is_an_error_unless_allowed(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.load_config("/nonexistent/.continuum.yml")
        fallback = config_module.load_optional_config("/nonexistent/.continuum.yml")
        self.assertEqual(fallback.review.provider, config_module.PROVIDER_NONE)

    def test_repository_configuration_is_valid(self):
        config = config_module.load_config(".continuum.yml")
        self.assertEqual(config.review.provider, config_module.PROVIDER_NONE)
        self.assertTrue(config.review.block_merge)


if __name__ == "__main__":
    unittest.main()
