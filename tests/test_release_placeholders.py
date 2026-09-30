"""The comment-aware scan for unfilled template tokens.

The property under test throughout is the distinction that makes this worth a
module rather than a regex: a placeholder named in a comment is documentation, and
a placeholder on a line that does something is a defect that ships a literal
``__VERSION__`` to every user who installs from the file. A scan that confuses the
two either blocks correct releases until someone rewrites the documentation, or
teaches everyone to ignore it.
"""

from __future__ import annotations

import unittest

from continuum.release import placeholders

#: The tokens the consumer's packaging templates declare as unfilled. Kept equal
#: to the core's own list, so a token added there is a token this suite covers.
from continuum.release.core import TEMPLATE_TOKENS as TOKENS


class CommentAwareness(unittest.TestCase):
    def test_a_placeholder_named_in_a_ruby_comment_is_documentation(self):
        text = '  version "1.2.3" # substituted for __VERSION__ by the generator\n'
        self.assertEqual(placeholders.find_unfilled(text, TOKENS, name="nanodictate.rb"), ())

    def test_a_placeholder_on_an_active_line_is_a_defect(self):
        found = placeholders.find_unfilled(
            '  version "__VERSION__"\n', TOKENS, name="nanodictate.rb"
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].token, "__VERSION__")
        self.assertEqual(found[0].line, 1)

    def test_a_placeholder_named_in_a_portfile_comment_is_documentation(self):
        # The consumer's macports descriptor names the token it substitutes.
        text = "  sha256 \"deadbeef\"\n  # was __SHA256_ before substitution\n"
        self.assertEqual(
            placeholders.find_unfilled(text, TOKENS, name="m/macports/Portfile"), ()
        )

    def test_an_active_portfile_line_is_still_a_defect(self):
        found = placeholders.find_unfilled(
            "  sha256 \"__SHA256__\"\n", TOKENS, name="m/macports/Portfile"
        )
        self.assertEqual([item.token for item in found], ["__SHA256_"])

    def test_a_c_style_block_comment_on_one_line_is_documentation(self):
        text = "/* the version arrives as __VERSION__ */\nint v = 1;\n"
        self.assertEqual(placeholders.find_unfilled(text, TOKENS, name="x.c"), ())

    def test_a_block_comment_spanning_lines_is_documentation_throughout(self):
        # The state carries across lines, so neither the opening nor the interior
        # nor the closing line reports. A per-line scan would report the middle.
        text = "/* start\n__VERSION__ is the token\n*/\nint v = 1;\n"
        self.assertEqual(placeholders.find_unfilled(text, TOKENS, name="x.c"), ())

    def test_code_after_a_block_comment_is_still_scanned(self):
        text = "/* doc */\nint v = __VERSION__;\n"
        found = placeholders.find_unfilled(text, TOKENS, name="x.c")
        self.assertEqual([item.line for item in found], [2])

    def test_an_xml_comment_is_documentation(self):
        text = "<!-- filled from __VERSION__ -->\n<key>v</key>\n"
        self.assertEqual(placeholders.find_unfilled(text, TOKENS, name="a.plist"), ())

    def test_an_xml_element_is_not_a_comment(self):
        found = placeholders.find_unfilled(
            "<key>__VERSION__</key>\n", TOKENS, name="a.plist"
        )
        self.assertEqual([item.token for item in found], ["__VERSION__"])

    def test_a_template_suffix_falls_back_to_the_language_it_generates(self):
        # `.rb.in` is Ruby before it is generated, so its comments are Ruby's.
        text = '# was __VERSION__\ns.version = "__VERSION__"\n'
        found = placeholders.find_unfilled(text, TOKENS, name="nanodictate.rb.in")
        self.assertEqual([item.line for item in found], [2])

    def test_a_hash_inside_a_string_is_content_not_a_comment(self):
        # `sep = "#"` must not truncate the line, and the placeholder after it on
        # the same line must still be found.
        text = 'sep = "#"; puts "__VERSION__"\n'
        found = placeholders.find_unfilled(text, TOKENS, name="x.rb")
        self.assertEqual([item.token for item in found], ["__VERSION__"])

    def test_an_escaped_quote_does_not_end_the_string(self):
        text = 'x = "a\\" # still a string"; y = "__VERSION__"\n'
        found = placeholders.find_unfilled(text, TOKENS, name="x.rb")
        self.assertEqual([item.token for item in found], ["__VERSION__"])

    def test_an_unknown_suffix_gets_only_the_hash_convention(self):
        # A block-comment syntax guessed for a language whose delimiters appear in
        # ordinary strings would strip code from the scan and hide a real defect.
        found = placeholders.find_unfilled(
            "/* __VERSION__ */\n", TOKENS, name="mystery.wat"
        )
        self.assertEqual([item.token for item in found], ["__VERSION__"])

    def test_a_ruby_line_comment_is_still_honoured_for_an_unknown_prefix(self):
        self.assertEqual(
            placeholders.find_unfilled("# __VERSION__\n", TOKENS, name="mystery.wat"), ()
        )

    def test_the_reported_text_is_the_code_not_the_comment_beside_it(self):
        found = placeholders.find_unfilled(
            'v = "__VERSION__"  # docs\n', TOKENS, name="x.rb"
        )
        self.assertEqual(found[0].text.strip(), 'v = "__VERSION__"')


class LineAccounting(unittest.TestCase):
    def test_a_placeholder_is_reported_on_the_line_it_is_on(self):
        text = "a\nb\nc\nd = __VERSION__\n"
        found = placeholders.find_unfilled(text, TOKENS, name="x.rb")
        self.assertEqual(found[0].line, 4)

    def test_a_multiline_block_comment_does_not_shift_the_line_numbers(self):
        # Index alignment is the reason the scan is line-preserving; a joined-text
        # scan would report the wrong line for everything after a block comment.
        text = "/* one\ntwo\nthree */\nd = __VERSION__\n"
        found = placeholders.find_unfilled(text, TOKENS, name="x.c")
        self.assertEqual(found[0].line, 4)

    def test_comment_stripping_keeps_one_line_per_line(self):
        stripped = placeholders.comment_stripped(
            "a # x\nb\nc /* y */ d\n", syntax=(("#",), (("/*", "*/"),))
        )
        self.assertEqual(len(stripped), 3)


class Reporting(unittest.TestCase):
    def test_one_line_reports_a_token_once(self):
        text = 'v = "__VERSION__" # __SHA256_\n'
        found = placeholders.find_unfilled(text, TOKENS, name="x.rb")
        self.assertEqual([item.token for item in found], ["__VERSION__"])

    def test_no_tokens_means_no_work_and_no_findings(self):
        self.assertEqual(placeholders.find_unfilled("anything", (), name="x.rb"), ())

    def test_an_empty_token_list_is_ignored(self):
        self.assertEqual(placeholders.find_unfilled("__VERSION__", ["", "  "]), ())

    def test_empty_text_is_clean(self):
        self.assertEqual(placeholders.find_unfilled("", TOKENS, name="x.rb"), ())

    def test_a_summary_lists_the_distinct_tokens_without_duplicates(self):
        text = 'a = __VERSION__\nb = __VERSION__\nc = __SHA256_\n'
        self.assertEqual(
            placeholders.unresolved("x.rb", text, TOKENS), ("__SHA256_", "__VERSION__")
        )

    def test_the_finding_describes_itself_for_a_job_log(self):
        found = placeholders.find_unfilled(
            "v = __VERSION__\n", TOKENS, name="x.rb"
        )[0]
        self.assertEqual(
            found.describe(),
            {"token": "__VERSION__", "line": 1, "text": "v = __VERSION__"},
        )
        self.assertIn("line 1", str(found))


class SuffixResolution(unittest.TestCase):
    def test_a_plain_name_with_no_extension_is_its_own_key(self):
        self.assertEqual(placeholders.suffix_of("Portfile"), "portfile")
        self.assertEqual(placeholders.suffix_of("m/macports/Portfile"), "portfile")

    def test_an_extension_is_returned_dotted(self):
        self.assertEqual(placeholders.suffix_of("a/b/nanodictate.rb"), ".rb")
        self.assertEqual(placeholders.suffix_of("nanodictate.rb"), ".rb")

    def test_a_double_extension_is_joined_when_the_last_is_not_one(self):
        self.assertEqual(placeholders.suffix_of("nanodictate.rb.in"), ".rb.in")

    def test_a_double_extension_prefers_the_final_one_when_it_is_known(self):
        # `.tar.gz`-style endings are not comment conventions, so the language is
        # found by its real extension.
        self.assertEqual(placeholders.suffix_of("build/lib.rb.in.rb"), ".rb")

    def test_resolution_is_case_insensitive(self):
        self.assertEqual(placeholders.suffix_of("PortFile"), "portfile")
        self.assertEqual(placeholders.suffix_of("NANO.RB"), ".rb")


if __name__ == "__main__":
    unittest.main()