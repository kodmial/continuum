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

    def test_rejects_unterminated_quote(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads('a: "unterminated\n')

    def test_empty_document(self):
        self.assertEqual(yamlmini.loads("\n# only comments\n"), {})

    def test_rejects_inconsistent_indentation(self):
        with self.assertRaises(yamlmini.YamlSubsetError):
            yamlmini.loads("a:\n    b: 1\n  c: 2\n")


if __name__ == "__main__":
    unittest.main()
