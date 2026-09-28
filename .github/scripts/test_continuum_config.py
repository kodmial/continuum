#!/usr/bin/env python3
"""Tests for the Continuum v0.1 two-toggle configuration contract.

Run directly; the suite is the executable form of
``docs/continuum-mvp-contract.md``:

    python3 .github/scripts/test_continuum_config.py

The suite carries three obligations beyond ordinary unit coverage:

1. it proves the acceptance criterion of the contract -- one unchanged
   resolver produces both consumer postures from their fixtures;
2. it proves the contract core is consumer-agnostic by scanning the module for
   repository, product, and vendor names;
3. it pins each validation rule individually, so relaxing a rule is a visible
   test failure rather than a silent behaviour change.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

import continuum_config
from continuum_config import ConfigError, parse, render_summary, resolve

FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "continuum-config",
)
CORE_MODULE = os.path.abspath(continuum_config.__file__)

#: Any hit here means Continuum core has started branching on a consumer,
#: provider, platform, or distribution channel - which the contract forbids.
FOREIGN_NAMES = (
    "nanodictate",
    "kodmai",
    "coderabbit",
    "pr-agent",
    "pr_agent",
    "release-please",
    "homebrew",
    "macports",
    "gradle",
    "maven",
    "android",
    "xcode",
    "notariz",
    "codesign",
)


def write(directory: str, name: str, text: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def resolve_text(self, text: str):
        return resolve(write(self.tmp, "continuum.yml", text))

    def test_absent_file_resolves_to_documented_defaults(self) -> None:
        config = resolve(os.path.join(self.tmp, "nope.yml"))
        self.assertFalse(config.review)
        self.assertFalse(config.release)
        self.assertFalse(config.present)

    def test_missing_config_equals_both_toggles_false(self) -> None:
        absent = resolve(os.path.join(self.tmp, "nope.yml"))
        empty = self.resolve_text("")
        for config in (absent, empty):
            self.assertEqual(
                (config.review, config.release), (False, False), config
            )

    def test_both_toggles_enabled(self) -> None:
        config = self.resolve_text("review: true\nrelease: true\n")
        self.assertTrue(config.review)
        self.assertTrue(config.release)
        self.assertTrue(config.present)

    def test_both_toggles_disabled_explicitly(self) -> None:
        config = self.resolve_text("review: false\nrelease: false\n")
        self.assertEqual((config.review, config.release), (False, False))

    def test_omitted_keys_default_to_false_independently(self) -> None:
        self.assertFalse(self.resolve_text("review: true\n").release)
        self.assertTrue(self.resolve_text("review: true\n").review)
        self.assertTrue(self.resolve_text("release: true\n").release)
        self.assertFalse(self.resolve_text("release: true\n").review)

    def test_key_order_is_irrelevant(self) -> None:
        forward = self.resolve_text("review: true\nrelease: false\n")
        reverse = self.resolve_text("release: false\nreview: true\n")
        self.assertEqual(forward, reverse)

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        config = self.resolve_text(
            "# leading comment\n"
            "\n"
            "review: true   # trailing comment\n"
            "\n"
            "  # indented comment line\n"
            "release: false\n"
        )
        self.assertEqual((config.review, config.release), (True, False))

    def test_crlf_line_endings(self) -> None:
        config = self.resolve_text("review: true\r\nrelease: true\r\n")
        self.assertEqual((config.review, config.release), (True, True))

    def test_source_path_is_reported(self) -> None:
        path = write(self.tmp, "continuum.yml", "review: true\n")
        self.assertEqual(resolve(path).source, path)

    def test_existing_non_regular_path_is_not_treated_as_absent(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            resolve(self.tmp)
        self.assertIn("not a regular file", str(caught.exception))


class ValidationTests(unittest.TestCase):
    def assertRejected(self, text: str, *fragments: str, line: int = 1) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse(text, "test.yml")
        message = str(caught.exception)
        self.assertIn(f"test.yml:{line}", message)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_unknown_keys_are_rejected_not_ignored(self) -> None:
        self.assertRejected(
            "relase: true\n", "unknown key 'relase'", "review, release"
        )

    def test_unknown_key_alongside_valid_keys_is_still_rejected(self) -> None:
        self.assertRejected(
            "review: true\nprovider: pr-agent\n", "unknown key", line=2
        )

    def test_case_variant_key_is_an_unknown_key(self) -> None:
        self.assertRejected("Review: true\n", "unknown key 'Review'")

    def test_quoted_booleans_are_rejected(self) -> None:
        self.assertRejected('review: "true"\n', "bare boolean")
        self.assertRejected("review: 'false'\n", "bare boolean")

    def test_yaml_truthy_spellings_are_rejected(self) -> None:
        for value in ("yes", "no", "on", "off", "1", "0", "True", "TRUE", "null"):
            with self.subTest(value=value):
                self.assertRejected(f"review: {value}\n", "bare boolean")

    def test_empty_value_is_rejected(self) -> None:
        self.assertRejected("review:\n", "bare boolean")

    def test_duplicate_keys_are_rejected(self) -> None:
        self.assertRejected("review: true\nreview: false\n", "duplicate key", line=2)

    def test_indented_content_is_rejected(self) -> None:
        self.assertRejected(
            "review: true\n  nested: true\n", "indented content", line=2
        )
        self.assertRejected("  review: true\n", "indented content")
        self.assertRejected("\treview: true\n", "indented content")

    def test_sequences_and_flow_style_are_rejected(self) -> None:
        self.assertRejected("- review\n", "expected `key: value`")
        self.assertRejected("review: {a: 1}\n", "bare boolean")
        self.assertRejected("just a scalar\n", "expected `key: value`")

    def test_anchor_without_whitespace_is_not_treated_as_a_comment(self) -> None:
        self.assertRejected("review: true#oops\n", "bare boolean")

    def test_error_message_names_the_offending_line(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse("# fine\nreview: true\nbogus: 1\n", "test.yml")
        self.assertIn("test.yml:3", str(caught.exception))


class DualConsumerProofTests(unittest.TestCase):
    """The contract's acceptance criterion.

    The same unchanged resolver must produce a fully enabled posture and a
    fully disabled posture from the two consumer fixtures, with no
    project-name checks anywhere in between.
    """

    def test_enabled_consumer_fixture(self) -> None:
        config = resolve(os.path.join(FIXTURES, "nanodictate.yml"))
        self.assertEqual((config.review, config.release), (True, True))

    def test_declaring_nothing_consumer_fixture(self) -> None:
        config = resolve(os.path.join(FIXTURES, "kodmai.yml"))
        self.assertEqual((config.review, config.release), (False, False))

    def test_both_fixtures_resolve_through_one_implementation(self) -> None:
        postures = {
            name: resolve(os.path.join(FIXTURES, name))
            for name in sorted(os.listdir(FIXTURES))
        }
        self.assertEqual(
            {(c.review, c.release) for c in postures.values()},
            {(True, True), (False, False)},
        )


class ConsumerAgnosticTests(unittest.TestCase):
    def test_contract_core_contains_no_consumer_or_vendor_names(self) -> None:
        with open(CORE_MODULE, encoding="utf-8") as handle:
            source = handle.read().lower()
        found = [name for name in FOREIGN_NAMES if name in source]
        self.assertEqual(
            found,
            [],
            f"{CORE_MODULE} must not name consumers, providers, or platforms: {found}",
        )

    def test_contract_core_defines_exactly_two_toggles(self) -> None:
        self.assertEqual(tuple(continuum_config.TOGGLES), ("review", "release"))
        self.assertEqual(set(continuum_config.DEFAULTS), {"review", "release"})

    def test_no_default_is_true(self) -> None:
        for toggle, value in continuum_config.DEFAULTS.items():
            with self.subTest(toggle=toggle):
                self.assertFalse(value)


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_summary_publishes_both_resolved_values(self) -> None:
        for text, expected in (
            ("review: true\nrelease: false\n", (True, False)),
            ("review: false\nrelease: false\n", (False, False)),
        ):
            with self.subTest(text=text):
                config = resolve(write(self.tmp, "continuum.yml", text))
                summary = render_summary(config)
                self.assertIn("`review: %s`" % str(expected[0]).lower(), summary)
                self.assertIn("`release: %s`" % str(expected[1]).lower(), summary)
                self.assertIn("`review`", summary)
                self.assertIn("`release`", summary)

    def test_summary_distinguishes_present_from_absent(self) -> None:
        present = render_summary(resolve(write(self.tmp, "a.yml", "review: true\n")))
        absent = render_summary(resolve(os.path.join(self.tmp, "b.yml")))
        self.assertIn("file present", present)
        self.assertIn("file absent", absent)

    def test_cli_writes_step_outputs_and_summary(self) -> None:
        path = write(self.tmp, "continuum.yml", "review: true\nrelease: true\n")
        output = os.path.join(self.tmp, "output.txt")
        summary = os.path.join(self.tmp, "summary.md")
        with contextlib.redirect_stdout(io.StringIO()):
            code = continuum_config.main(
                [
                    "--config",
                    path,
                    "--github-output",
                    output,
                    "--step-summary",
                    summary,
                ]
            )
        self.assertEqual(code, 0)
        with open(output, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("review=true", written)
        self.assertIn("release=true", written)
        with open(summary, encoding="utf-8") as handle:
            self.assertIn("Continuum configuration (v0.1)", handle.read())

    def test_cli_fails_loudly_on_a_contract_violation(self) -> None:
        path = write(self.tmp, "continuum.yml", "review: maybe\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = continuum_config.main(["--config", path])
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())

    def test_unreadable_file_is_not_silently_defaulted(self) -> None:
        # A directory at the config path is unreadable as a file; it must fail
        # rather than resolve to defaults, which are indistinguishable from a
        # deliberately disabled consumer.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = continuum_config.main(["--config", self.tmp])
        self.assertEqual(code, 1)
        self.assertIn("::error::", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
