"""Tests for the restricted YAML subset used by `.continuum.yml`."""

from __future__ import annotations

import textwrap
import unittest

from continuum import yamlmini


class YamlSubsetTests(unittest.TestCase):
    def test_nested_mapping_and_scalars(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            # leading comment
            version: 1
            review:
              provider: pr-agent
              block_merge: true
              nested:
                count: 3
                ratio: 0.5
                absent: null
                empty: ""
                """
            )
        )
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["review"]["provider"], "pr-agent")
        self.assertIs(parsed["review"]["block_merge"], True)
        self.assertEqual(parsed["review"]["nested"]["count"], 3)
        self.assertAlmostEqual(parsed["review"]["nested"]["ratio"], 0.5)
        self.assertIsNone(parsed["review"]["nested"]["absent"])
        self.assertEqual(parsed["review"]["nested"]["empty"], "")

    def test_block_sequence_of_scalars_and_maps(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            items:
              - one
              - two
            records:
              - name: a
                value: 1
              - name: b
                value: 2
                """
            )
        )
        self.assertEqual(parsed["items"], ["one", "two"])
        self.assertEqual(parsed["records"], [{"name": "a", "value": 1}, {"name": "b", "value": 2}])

    def test_indentless_sequence_under_a_key_inside_a_sequence_item(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            release:
              targets:
                - id: cli
                  binaries:
                  - name: tool
                    identifier: com.example.tool
                """
            )
        )
        self.assertEqual(
            parsed["release"]["targets"],
            [
                {
                    "id": "cli",
                    "binaries": [
                        {"name": "tool", "identifier": "com.example.tool"}
                    ],
                }
            ],
        )

    def test_rejects_a_dash_entry_at_mapping_key_indent(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads(
                textwrap.dedent(
                    """
                items:
                  - id: a
                    - name: b
                    """
                )
            )

    def test_quoted_scalars_and_inline_comment(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            a: "quoted # not a comment"  # real comment
            b: 'single ''quoted'''
            c: plain value
                """
            )
        )
        self.assertEqual(parsed["a"], "quoted # not a comment")
        self.assertEqual(parsed["b"], "single 'quoted'")
        self.assertEqual(parsed["c"], "plain value")

    def test_a_colon_inside_a_plain_scalar_is_not_a_key(self):
        # A label such as `priority:p0` is a value, not a nested mapping. A
        # repository's real priority labels look exactly like this.
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            queue:
              priority_labels:
                - priority:p0
                - priority:p1
                - "priority: p2"
              records:
                - name: a
                  value: 1
                """
            )
        )
        self.assertEqual(
            parsed["queue"]["priority_labels"], ["priority:p0", "priority:p1", "priority: p2"]
        )
        self.assertEqual(parsed["queue"]["records"], [{"name": "a", "value": 1}])

    def test_document_start_marker(self):
        self.assertEqual(yamlmini.loads("---\na: 1\n"), {"a": 1})

    def test_rejects_tabs(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads("a:\n\tb: 1\n")

    def test_rejects_unsupported_constructs(self):
        for text in ("a: &anchor 1\n", "a: *alias\n", "a: !!str 1\n", "a: [1, 2]\n", "a: {b: 1}\n", "a: |\n  block\n"):
            with self.subTest(text=text):
                with self.assertRaises(yamlmini.YamlSubsetError):
                    yamlmini.loads(text)

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads("a: 1\na: 2\n")

    def test_rejects_multiple_documents(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads("a: 1\n---\nb: 2\n")

    def test_apostrophes_in_plain_scalars_are_not_quote_delimiters(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            identity: Developer ID Application: O'Brien # signing identity
            profiles:
              - O'Brien's signing profile
                """
            )
        )
        self.assertEqual(
            parsed,
            {
                "identity": "Developer ID Application: O'Brien",
                "profiles": ["O'Brien's signing profile"],
            },
        )

    def test_rejects_unterminated_quote(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads('a: "unterminated\n')

    def test_unicode_escape_decodes_exactly_four_hex_digits(self):
        self.assertEqual(yamlmini.loads('a: "\\u0041"\n'), {"a": "A"})

    def test_rejects_truncated_unicode_escape(self):
        with self.assertRaises(yamlmini.YamlSubsetError) as raised:
            yamlmini.loads('a: "\\u041"\n')
        self.assertIn("line 1", str(raised.exception))

    def test_rejects_non_hex_unicode_escape(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads('a: "\\u00zz"\n')

    def test_empty_document(self):
        self.assertEqual(yamlmini.loads("\n# only comments\n"), {})

    def test_rejects_inconsistent_indentation(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads("a:\n    b: 1\n  c: 2\n")

    def test_empty_first_key_in_sequence_item_is_none(self):
        # The first key of a sequence-item mapping follows the same
        # empty-value rule as every other mapping key.
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            - name:
            - name: b
                """
            )
        )
        self.assertEqual(parsed, [{"name": None}, {"name": "b"}])

    def test_indentless_sequence_as_the_first_key_of_a_sequence_item(self):
        # Symmetric to the nested-key case: an indentless sequence that is the
        # first key of a sequence-item mapping aligns with that key.
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            items:
              - binaries:
                - name: tool
                  identifier: com.example.tool
                """
            )
        )
        self.assertEqual(
            parsed["items"],
            [
                {
                    "binaries": [
                        {"name": "tool", "identifier": "com.example.tool"}
                    ]
                }
            ],
        )

    def test_nested_block_under_first_key_in_sequence_item(self):
        parsed = yamlmini.loads(
            textwrap.dedent(
                """
            records:
              - name:
                  sub: 1
                value: 2
                """
            )
        )
        self.assertEqual(
            parsed["records"], [{"name": {"sub": 1}, "value": 2}]
        )


    def test_apostrophe_inside_a_plain_scalar_is_not_a_quote(self):
        # A quote opens a scalar only where a token can start. An apostrophe
        # mid-word belongs to the plain scalar, so the line is not an
        # unterminated quote.
        parsed = yamlmini.loads(
            "identity: Developer ID Application: O'Brien\n"
        )
        self.assertEqual(parsed, {"identity": "Developer ID Application: O'Brien"})

    def test_double_quoted_value_with_hash_and_apostrophe(self):
        parsed = yamlmini.loads('a: "quoted # it\'s here"  # real comment\n')
        self.assertEqual(parsed["a"], "quoted # it's here")

    def test_escaped_quotes_inside_a_double_quoted_value(self):
        parsed = yamlmini.loads('a: "say \\"hi\\" # not a comment"\n')
        self.assertEqual(parsed["a"], 'say "hi" # not a comment')

    def test_single_quoted_value_with_hash(self):
        parsed = yamlmini.loads("a: 'has # hash'  # real comment\n")
        self.assertEqual(parsed["a"], "has # hash")

    def test_rejects_a_genuine_unterminated_single_quote(self):
        with self.assertRaises(yamlmini.YamlSubsetError) as raised:
            yamlmini.loads("a: 'unterminated\n")
        self.assertIn("unterminated quoted scalar", str(raised.exception))

    def test_rejects_a_trailing_backslash_in_an_unterminated_double_quote(self):
        with self.assertRaises(yamlmini.YamlSubsetError) as raised:
            yamlmini.loads('a: "trailing \\\n')
        self.assertIn("unterminated quoted scalar", str(raised.exception))

    def test_sequence_item_containing_an_apostrophe(self):
        self.assertEqual(yamlmini.loads("- O'Brien\n"), ["O'Brien"])

    def test_hyphen_inside_a_plain_word_is_not_a_sequence_marker(self):
        parsed = yamlmini.loads("key: foo-'bar'\n")
        self.assertEqual(parsed, {"key": "foo-'bar'"})

    def test_quote_after_a_plain_word_is_not_a_quote(self):
        self.assertEqual(yamlmini.loads("key: foo 'bar'\n"), {"key": "foo 'bar'"})

    def test_quoted_key_at_line_start_is_not_unterminated(self):
        # A quote at the start of the line is a token start. Keys keep their
        # quotes in this subset; the point is that the scanner does not treat
        # the line as an unterminated quoted scalar.
        parsed = yamlmini.loads("a:\n  'b c': 1\n")
        self.assertEqual(parsed, {"a": {"'b c'": 1}})

    def test_quoted_sequence_items_open_a_quoted_scalar(self):
        self.assertEqual(yamlmini.loads("- 'a b'\n"), ["a b"])
        self.assertEqual(
            yamlmini.loads("items:\n  - 'a # b'\n"), {"items": ["a # b"]}
        )


if __name__ == "__main__":
    unittest.main()
