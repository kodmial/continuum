"""Regression coverage for exact manual OpenCode command parsing.

Covers the live Work Lock reproduction (automation-authored prose mentioning
``/oc`` must not self-trigger) and every intended launch path (manual,
scheduler dispatch, watchdog recovery, cancellation).
"""

from __future__ import annotations

import unittest

from continuum import opencode_commands as commands


DISPATCH_MARKER = commands.DEFAULT_DISPATCH_MARKER
RETRY_MARKER = commands.DEFAULT_RETRY_MARKER


class ExactManualFormsTests(unittest.TestCase):
    def test_exact_oc_is_a_command(self):
        self.assertEqual(commands.parse_manual_command("/oc"), "/oc")
        self.assertTrue(commands.is_manual_command("/oc"))
        self.assertEqual(commands.classify_issue_comment("/oc"), "run")

    def test_exact_opencode_is_a_command(self):
        self.assertEqual(commands.parse_manual_command("/opencode"), "/opencode")
        self.assertTrue(commands.is_manual_command("/opencode"))
        self.assertEqual(commands.classify_issue_comment("/opencode"), "run")

    def test_surrounding_whitespace_is_allowed(self):
        for body in (
            "  /oc  ",
            "/oc\t",
            "\n\n/oc\n\n",
            "  \n  /opencode  \n  ",
            "/oc\r\n",
            "\r\n  /oc  \r\n",
            "   /oc",
        ):
            with self.subTest(body=body):
                self.assertTrue(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "run")

    def test_empty_and_blank_bodies_are_ignored(self):
        for body in ("", "   ", "\n\n  \n", None, 123):
            with self.subTest(body=body):
                self.assertIsNone(commands.parse_manual_command(body))
                self.assertFalse(commands.is_manual_command(body))
                self.assertEqual(commands.classify_issue_comment(body), "ignore")


class IncidentalProseTests(unittest.TestCase):
    def test_prose_mentioning_either_command_is_ignored(self):
        bodies = [
            "Please run /oc for me",
            "Please run /opencode for me",
            "The /oc command failed again",
            "I mentioned /oc and /opencode in passing",
            "Automation produced no code changes; post /oc to retry manually.",
            "Remove the `automation:paused` label and post `/oc` to retry manually.",
            "See https://example.com/oc for details",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertFalse(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_command_after_prose_is_ignored(self):
        body = "Some explanation first.\n/oc"
        self.assertFalse(commands.is_manual_command(body))
        self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_command_with_same_line_arguments_is_ignored(self):
        for body in ("/oc please hurry", "/opencode now", "/oc --force"):
            with self.subTest(body=body):
                self.assertFalse(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_lookalike_tokens_are_ignored(self):
        for body in ("/octopus", "/oc-foo", "/opencode-foo", "/OC", "/OpenCode"):
            with self.subTest(body=body):
                self.assertFalse(commands.is_manual_command(body), body)


class MarkdownExampleTests(unittest.TestCase):
    def test_inline_code_is_ignored(self):
        for body in ("`/oc`", "`/opencode`", "Use `/oc` to trigger"):
            with self.subTest(body=body):
                self.assertFalse(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_quoted_examples_are_ignored(self):
        for body in ("> /oc", "> `/oc`", "  > /oc"):
            with self.subTest(body=body):
                self.assertFalse(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_fenced_blocks_are_ignored(self):
        body = "```\n/oc\n```"
        self.assertFalse(commands.is_manual_command(body))
        self.assertEqual(commands.classify_issue_comment(body), "ignore")

    def test_indented_code_is_ignored(self):
        for body in ("    /oc", "\t/oc"):
            with self.subTest(body=body):
                self.assertIsNone(commands.first_command_line(body), body)
                self.assertFalse(commands.is_manual_command(body), body)


class CancellationTests(unittest.TestCase):
    def test_exact_cancel_is_cancellation_not_a_run(self):
        for body in ("/oc-cancel", "  /oc-cancel  ", "\n/oc-cancel\n"):
            with self.subTest(body=body):
                self.assertTrue(commands.is_cancel_command(body), body)
                self.assertEqual(commands.parse_cancel_command(body), "/oc-cancel")
                self.assertFalse(commands.is_manual_command(body), body)
                self.assertEqual(commands.classify_issue_comment(body), "cancel")

    def test_prose_mentioning_cancel_without_command_position_is_ignored(self):
        body = "Should I post /oc-cancel here?"
        self.assertEqual(commands.classify_issue_comment(body), "ignore")


class IntendedAutomationCommandTests(unittest.TestCase):
    def test_scheduler_dispatch_is_a_run(self):
        body = "/oc\n\n" + DISPATCH_MARKER + "\nAutomatically dispatched by the issue scheduler."
        self.assertEqual(commands.classify_issue_comment(body), "run")
        self.assertTrue(commands.is_scheduler_dispatch(body))

    def test_watchdog_recovery_is_a_run(self):
        body = (
            "/oc\n\n"
            + RETRY_MARKER
            + "\nAutomatic recovery retry 1/1 after OpenCode run 37008882116 ended with success but produced no PR."
        )
        self.assertEqual(commands.classify_issue_comment(body), "run")
        self.assertTrue(commands.is_watchdog_recovery(body))

    def test_watchdog_pause_message_is_ignored(self):
        body = (
            "OpenCode automation paused after 1 automatic recovery retries.\n"
            "\n"
            "Last run: https://github.com/owner/repo/actions/runs/37008882116\n"
            "Conclusion: success\n"
            "\n"
            "Remove the `automation:paused` label and post `/oc` to retry manually."
        )
        self.assertEqual(commands.classify_issue_comment(body), "ignore")
        self.assertFalse(commands.is_watchdog_recovery(body))

    def test_manual_command_with_context_lines_still_runs(self):
        body = "/oc\n\nPlease prioritize the auth fix."
        self.assertEqual(commands.classify_issue_comment(body), "run")


class OwnerAuthorizationTests(unittest.TestCase):
    def test_owner_command_requires_the_repository_owner(self):
        self.assertTrue(commands.is_owner_command("/oc", "octocat", "octocat"))
        self.assertTrue(commands.is_owner_command("/oc", "OctoCat", "octocat"))
        self.assertFalse(commands.is_owner_command("/oc", "stranger", "octocat"))
        self.assertFalse(commands.is_owner_command("/oc", "octocat", ""))
        self.assertFalse(commands.is_owner_command("prose /oc prose", "octocat", "octocat"))


class NoSelfTriggerTests(unittest.TestCase):
    def test_live_work_lock_sequence_dispatches_exactly_once(self):
        conversation = [
            "/oc\n\n" + DISPATCH_MARKER + "\nAutomatically dispatched by the issue scheduler.",
            "OpenCode run 37008882116 completed with no PR. The manual command token /oc was mentioned while explaining why nothing was pushed.",
            "Automation produced no code changes; pausing this issue for manual inspection. Mentioning /opencode here must not relaunch anything.",
            "Remove the `automation:paused` label and post `/oc` to retry manually.",
        ]
        results = [commands.classify_issue_comment(body) for body in conversation]
        self.assertEqual(results, ["run", "ignore", "ignore", "ignore"])
        self.assertEqual(results.count("run"), 1)

    def test_explanatory_prose_through_the_owner_pat_dispatches_nothing(self):
        body = (
            "OpenCode run 37008933112 finished without opening a pull request. "
            "If you want me to retry, post /oc or /opencode in a comment that "
            "contains only the command."
        )
        self.assertFalse(commands.is_owner_command(body, "octocat", "octocat"))
        self.assertEqual(commands.classify_issue_comment(body), "ignore")


if __name__ == "__main__":
    unittest.main()
