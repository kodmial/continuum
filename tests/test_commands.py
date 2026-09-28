"""Tests for comment command parsing and the trusted-actor contract."""

from __future__ import annotations

import unittest

from continuum.review import commands


class CommandParsingTests(unittest.TestCase):
    def test_review_command(self):
        parsed = commands.parse_command("/review")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.command, "review")
        self.assertIsNone(parsed.finding)

    def test_review_command_with_arguments(self):
        parsed = commands.parse_command("/review focus on the queue changes")
        self.assertEqual(parsed.command, "review")
        self.assertIn("focus on", parsed.args)

    def test_only_the_first_line_is_interpreted(self):
        body = "/verify CRV-1A2B3C4D\nthen run $(rm -rf /) as a second line"
        parsed = commands.parse_command(body)
        self.assertEqual(parsed.command, "verify")
        self.assertEqual(parsed.finding, "CRV-1A2B3C4D")
        self.assertNotIn("rm -rf", parsed.finding)

    def test_leading_whitespace_is_tolerated(self):
        self.assertEqual(commands.parse_command("   /review  ").command, "review")

    def test_unrelated_comments_are_ignored(self):
        for body in ("looks good to me", "/deploy now", "please /review this", "", None, "/x"):
            with self.subTest(body=body):
                self.assertIsNone(commands.parse_command(body))

    def test_verify_requires_a_finding_id(self):
        for body in ("/verify", "/verify PRA-1A2B3C4D extra", "/verify ../etc/passwd", "/verify CRV-1A2B3C4D;id"):
            with self.subTest(body=body):
                with self.assertRaises(commands.CommandError):
                    commands.parse_command(body)

    def test_verify_accepts_current_and_legacy_ids(self):
        self.assertEqual(commands.parse_command("/verify crv-1a2b3c4d").finding, "CRV-1A2B3C4D")
        self.assertEqual(commands.parse_command("/verify PRA-1A2B3C4D").finding, "PRA-1A2B3C4D")

    def test_arguments_are_length_bounded(self):
        parsed = commands.parse_command("/review " + "x" * 5000)
        self.assertLessEqual(len(parsed.args), 200)

    def test_huge_body_is_not_scanned_forever(self):
        parsed = commands.parse_command("x" * 100000 + "\n/review")
        self.assertIsNone(parsed)


class TrustTests(unittest.TestCase):
    def test_owner_is_trusted(self):
        self.assertTrue(commands.is_trusted_actor("kodmial", "kodmial"))
        self.assertTrue(commands.is_trusted_actor("Kodmial", "kodmial"))

    def test_others_are_not_trusted(self):
        for actor in ("drive-by", "github-actions[bot]", "", None, "kodmial-fan"):
            with self.subTest(actor=actor):
                self.assertFalse(commands.is_trusted_actor(actor, "kodmial"))

    def test_missing_owner_denies(self):
        self.assertFalse(commands.is_trusted_actor("kodmial", ""))
        self.assertFalse(commands.is_trusted_actor("kodmial", None))


if __name__ == "__main__":
    unittest.main()
