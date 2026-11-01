"""Regression coverage for OpenCode event admission and duplicate runs."""

from __future__ import annotations

import unittest

from continuum import opencode_admission as admission
from continuum import opencode_commands as commands


OWNER = "octocat"
OTHER = "stranger"


def run(run_id, title, status="completed", conclusion="success",
        created="2026-01-01T10:00:00Z", updated="2026-01-01T10:05:00Z",
        name="OpenCode agent",
        path=".github/workflows/continuum-opencode.yml", event="issue_comment"):
    return {
        "id": run_id,
        "display_title": title,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": updated,
        "name": name,
        "path": path,
        "event": event,
    }


class EventAdmissionTests(unittest.TestCase):
    def test_exact_owner_issue_command_is_admitted(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertTrue(result["run"])
        self.assertEqual(result["route"], "issue")
        self.assertEqual(result["command"], "run")

    def test_internal_snapshot_prose_is_ignored(self):
        body = (
            "Task snapshot pinned by Continuum.\n\n"
            "The product is nanodictate/oc and the /oc token is documentation."
        )
        result = admission.admit_event(
            event="issue_comment",
            body=body,
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertFalse(result["run"])
        self.assertEqual(result["route"], "ignore")
        self.assertEqual(result["command"], "ignore")

    def test_snapshot_with_json_or_quoted_examples_is_ignored(self):
        for body in (
            '{"body": "Please run /oc"}\n/oc',
            "```json\n{\"command\": \"/oc\"}\n```",
            "> /oc",
            "Use `/oc` to retry.",
            "/oc please retry",
            "/octopus",
            "/OC",
        ):
            with self.subTest(body=body):
                result = admission.admit_event(
                    event="issue_comment",
                    body=body,
                    author=OWNER,
                    actor=OWNER,
                    owner=OWNER,
                )
                self.assertFalse(result["run"], body)

    def test_untrusted_author_is_ignored(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc",
            author=OTHER,
            actor=OTHER,
            owner=OWNER,
        )
        self.assertFalse(result["run"])

    def test_cancel_routes_to_cancellation_not_the_coder(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc-cancel",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertFalse(result["run"])
        self.assertEqual(result["route"], "cancel")
        self.assertEqual(result["command"], commands.CANCEL)

    def test_only_exact_owner_issue_cancel_triggers_cancellation(self):
        for body, author, actor in (
            ("/oc-cancel", OTHER, OTHER),
            ("/oc-cancel", OWNER, OTHER),
            ("Please /oc-cancel this", OWNER, OWNER),
            ("`/oc-cancel`", OWNER, OWNER),
            ("> /oc-cancel", OWNER, OWNER),
            ("    /oc-cancel", OWNER, OWNER),
            ("/oc-cancel please", OWNER, OWNER),
            ("/oc-cancelled", OWNER, OWNER),
        ):
            with self.subTest(body=body, author=author, actor=actor):
                result = admission.admit_event(
                    event="issue_comment",
                    body=body,
                    author=author,
                    actor=actor,
                    owner=OWNER,
                )
                self.assertFalse(result["run"])
                self.assertNotEqual(result["route"], "cancel")

    def test_pull_request_cancel_never_triggers_issue_cancellation(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc-cancel",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
            is_pull_request=True,
            issue_state="open",
        )
        self.assertFalse(result["run"])
        self.assertNotEqual(result["route"], "cancel")

    def test_scheduler_and_watchdog_commands_are_admitted(self):
        for marker in (commands.DEFAULT_DISPATCH_MARKER, commands.DEFAULT_RETRY_MARKER):
            with self.subTest(marker=marker):
                result = admission.admit_event(
                    event="issue_comment",
                    body="/oc\n\n{}\nAutomated.".format(marker),
                    author=OWNER,
                    actor=OWNER,
                    owner=OWNER,
                )
                self.assertTrue(result["run"])
                self.assertEqual(result["route"], "issue")

    def test_qualification_dispatch_is_admitted(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc\n\n<!-- continuum-qualification-dispatch capability=1 qualification=2 sha={} -->".format(
                "a" * 40
            ),
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertTrue(result["run"])
        self.assertEqual(result["route"], "qualification")

    def test_open_pr_command_is_admitted_but_cancel_and_closed_pr_are_not(self):
        result = admission.admit_event(
            event="issue_comment",
            body="/oc\n\nPlease fix this.",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
            is_pull_request=True,
            issue_state="open",
        )
        self.assertTrue(result["run"])
        self.assertEqual(result["route"], "pr")

        cancelled = admission.admit_event(
            event="issue_comment",
            body="/oc-cancel",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
            is_pull_request=True,
            issue_state="open",
        )
        self.assertFalse(cancelled["run"])

        closed = admission.admit_event(
            event="issue_comment",
            body="/oc",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
            is_pull_request=True,
            issue_state="closed",
        )
        self.assertFalse(closed["run"])

        qualification = admission.admit_event(
            event="issue_comment",
            body="/oc\n<!-- continuum-qualification-dispatch capability=1 qualification=2 sha={} -->".format(
                "a" * 40
            ),
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
            is_pull_request=True,
            issue_state="open",
        )
        self.assertFalse(qualification["run"])

    def test_review_comment_keeps_owner_contains_behavior(self):
        result = admission.admit_event(
            event="pull_request_review_comment",
            body="Please run /oc after the review.",
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertTrue(result["run"])
        self.assertEqual(result["route"], "review")

        untrusted = admission.admit_event(
            event="pull_request_review_comment",
            body="Please run /oc after the review.",
            author=OTHER,
            actor=OTHER,
            owner=OWNER,
        )
        self.assertFalse(untrusted["run"])

        qualification = admission.admit_event(
            event="pull_request_review_comment",
            body="/oc\n<!-- continuum-qualification-dispatch capability=1 qualification=2 sha={} -->".format(
                "a" * 40
            ),
            author=OWNER,
            actor=OWNER,
            owner=OWNER,
        )
        self.assertFalse(qualification["run"])

    def test_workflow_dispatch_modes_are_admitted_only_from_the_allowlist(self):
        for mode in admission.DISPATCH_MODES:
            with self.subTest(mode=mode):
                result = admission.admit_event(event="workflow_dispatch", mode=mode)
                self.assertTrue(result["run"])
                self.assertEqual(result["route"], "dispatch")
        for mode in ("", "unknown", "issue;rm"):
            with self.subTest(mode=mode):
                self.assertFalse(admission.admit_event(event="workflow_dispatch", mode=mode)["run"])

    def test_unknown_event_is_ignored(self):
        self.assertFalse(admission.admit_event(event="push")["run"])


class BatchClassificationTests(unittest.TestCase):
    def test_batch_accepts_strings_and_records(self):
        result = admission.classify_commands(
            [
                "/oc",
                "prose /oc prose",
                {"body": "/opencode"},
                {"body": "`/oc`"},
                "/oc-cancel",
            ]
        )
        self.assertEqual(result, ["run", "ignore", "run", "ignore", "cancel"])

    def test_batch_rejects_non_array(self):
        with self.assertRaises(ValueError):
            admission.classify_commands({"body": "/oc"})


class DuplicateRunTests(unittest.TestCase):
    def decision(self, current, runs, issue=234, legacy_title=""):
        return admission.duplicate_run_decision(
            current=current,
            runs=runs,
            issue_number=issue,
            workflow_name="OpenCode agent",
            legacy_title=legacy_title,
        )

    def test_first_active_run_is_allowed(self):
        current = run(20, "OpenCode issue #234", status="in_progress")
        self.assertFalse(self.decision(current, [current])["skip"])

    def test_later_active_run_is_skipped(self):
        first = run(20, "OpenCode issue #234", status="in_progress",
                    created="2026-01-01T10:00:00Z")
        second = run(21, "OpenCode issue #234", status="queued",
                     created="2026-01-01T10:01:00Z")
        result = self.decision(second, [first, second])
        self.assertTrue(result["skip"])
        self.assertEqual(result["duplicate_run_id"], 20)

    def test_simultaneous_runs_choose_the_lower_id(self):
        first = run(20, "OpenCode issue #234", status="queued",
                    created="2026-01-01T10:00:00Z")
        second = run(21, "OpenCode issue #234", status="queued",
                     created="2026-01-01T10:00:00Z")
        self.assertFalse(self.decision(first, [first, second])["skip"])
        self.assertTrue(self.decision(second, [first, second])["skip"])

    def test_queued_before_previous_run_finished_is_skipped_after_it_finishes(self):
        previous = run(20, "OpenCode issue #234",
                       created="2026-01-01T10:00:00Z",
                       updated="2026-01-01T10:05:00Z")
        current = run(21, "OpenCode issue #234", status="in_progress",
                      created="2026-01-01T10:02:00Z",
                      updated="2026-01-01T10:06:00Z")
        result = self.decision(current, [previous, current])
        self.assertTrue(result["skip"])
        self.assertEqual(result["duplicate_run_id"], 20)

    def test_recovery_started_after_failure_is_allowed(self):
        previous = run(20, "OpenCode issue #234", status="completed",
                       conclusion="failure",
                       created="2026-01-01T10:00:00Z",
                       updated="2026-01-01T10:05:00Z")
        current = run(21, "OpenCode issue #234", status="in_progress",
                      created="2026-01-01T10:06:00Z",
                      updated="2026-01-01T10:07:00Z")
        self.assertFalse(self.decision(current, [previous, current])["skip"])

    def test_recovery_started_after_success_without_pr_is_allowed(self):
        previous = run(20, "OpenCode issue #234", status="completed",
                       conclusion="success",
                       created="2026-01-01T10:00:00Z",
                       updated="2026-01-01T10:05:00Z")
        current = run(21, "OpenCode issue #234", status="queued",
                      created="2026-01-01T10:06:00Z")
        self.assertFalse(self.decision(current, [previous, current])["skip"])

    def test_other_issue_or_workflow_is_ignored(self):
        current = run(21, "OpenCode issue #234", status="in_progress",
                      created="2026-01-01T10:06:00Z")
        other_issue = run(20, "OpenCode issue #999", status="in_progress",
                          created="2026-01-01T10:00:00Z")
        other_workflow = run(19, "OpenCode issue #234", status="in_progress",
                             created="2026-01-01T10:00:00Z",
                             name="Another workflow",
                             path=".github/workflows/another.yml")
        for candidate in (other_issue, other_workflow):
            with self.subTest(candidate=candidate["display_title"]):
                self.assertFalse(self.decision(current, [candidate, current])["skip"])

    def test_legacy_title_is_not_an_issue_identity(self):
        current = run(21, "OpenCode issue #234", status="in_progress",
                      created="2026-01-01T10:02:00Z")
        for status in ("in_progress", "completed"):
            previous = run(20, "Fix the parser", status=status,
                           created="2026-01-01T10:00:00Z",
                           updated="2026-01-01T10:05:00Z")
            with self.subTest(status=status):
                result = self.decision(
                    current,
                    [previous, current],
                    legacy_title="Fix the parser",
                )
                self.assertFalse(result["skip"])

    def test_malformed_run_inventory_fails_closed(self):
        current = run(21, "OpenCode issue #234", status="in_progress")
        bad = run(20, "OpenCode issue #234", status="in_progress")
        bad["created_at"] = "not-a-date"
        self.assertTrue(self.decision(current, [bad, current])["skip"])

    def test_missing_current_identity_fails_closed(self):
        current = run(21, "OpenCode issue #234", status="in_progress")
        del current["created_at"]
        self.assertTrue(self.decision(current, [current])["skip"])

    def test_invalid_issue_number_does_not_block_unrelated_work(self):
        current = run(21, "OpenCode issue #0", status="in_progress")
        self.assertFalse(self.decision(current, [current], issue=0)["skip"])



class HistoricalReplayTests(unittest.TestCase):
    """Replay the nano dictation incident without consumer-specific runtime code."""

    SCHEDULER_DISPATCH = (
        "/oc\n\n"
        "<!-- issue-scheduler-dispatch -->\n"
        "⚡ **Continuum · issue-scheduler**\n"
        "<!-- continuum-origin role=continuum component=issue-scheduler -->\n"
        "Automatically dispatched by the issue scheduler (priority:p0)."
    )

    PINNED_SNAPSHOT = (
        "<!-- continuum-task-snapshot repo=example/project issue=234 "
        "generation=1 title-sha256={title} body-sha256={body} spec-sha256={spec} -->\n"
        "⚡ **Continuum · opencode**\n"
        "<!-- continuum-origin role=continuum component=opencode -->\n"
        "Continuum task snapshot (machine-owned, do not edit or delete).\n"
        "\n"
        "```json\n"
        '{{"schema_version":1,"repo":"example/project","issue":234,"generation":1,'
        '"title":"P0 Windows installer hang","body":"NanoDictate/oc documents the '
        'manual /oc command in prose. This is a snapshot, not a launch."}}\n'
        "```\n"
        "\n"
        "The product path NanoDictate/oc and the /opencode token are narrative only."
    ).format(title="a" * 64, body="b" * 64, spec="c" * 64)

    def admit_issue_comment(self, body, author=OWNER, actor=OWNER):
        return admission.admit_event(
            event="issue_comment",
            body=body,
            author=author,
            actor=actor,
            owner=OWNER,
        )

    def test_historical_events_launch_exactly_one_coder_and_no_snapshot_run(self):
        results = [
            self.admit_issue_comment(self.SCHEDULER_DISPATCH),
            self.admit_issue_comment(self.PINNED_SNAPSHOT),
        ]
        self.assertEqual([result["run"] for result in results], [True, False])
        self.assertEqual(results[0]["route"], "issue")
        self.assertEqual(results[1]["route"], "ignore")
        self.assertEqual(sum(1 for result in results if result["run"]), 1)

    def test_marker_only_or_prose_after_marker_never_launches(self):
        for body in (
            "Automatically dispatched.\n\n<!-- issue-scheduler-dispatch -->",
            "Automatic retry.\n\n<!-- opencode-watchdog-retry -->\nPost /oc manually if needed.",
            "<!-- continuum-task-snapshot repo=example/project issue=234 generation=1 "
            + "title-sha256={} body-sha256={} spec-sha256={} -->\n".format(
                "a" * 64, "b" * 64, "c" * 64
            )
            + "NanoDictate/oc prose mentions /opencode.",
        ):
            with self.subTest(body=body[:60]):
                result = self.admit_issue_comment(body)
                self.assertFalse(result["run"])
                self.assertEqual(result["route"], "ignore")

    def test_paths_urls_json_and_case_variants_are_ignored(self):
        for body in (
            "/home/runner/work/oc",
            "https://github.com/example/project/tree/main/oc",
            '{"command":"/oc"}',
            '{"body":"run /opencode now"}',
            "/OC",
            "/OpenCode",
            " /oc\tplease",
        ):
            with self.subTest(body=body):
                self.assertFalse(self.admit_issue_comment(body)["run"], body)

    def test_historical_queued_duplicate_is_skipped_after_first_run_finishes(self):
        first = run(
            39691042302,
            "OpenCode issue #234",
            created="2026-10-21T23:05:06Z",
            updated="2026-10-21T23:41:03Z",
        )
        duplicate = run(
            39691076267,
            "OpenCode issue #234",
            created="2026-10-21T23:05:23Z",
            updated="2026-10-21T23:41:28Z",
        )
        result = admission.duplicate_run_decision(
            current=duplicate,
            runs=[first, duplicate],
            issue_number=234,
            workflow_name="OpenCode agent",
        )
        self.assertTrue(result["skip"])
        self.assertEqual(result["duplicate_run_id"], 39691042302)
        self.assertEqual(result["duplicate_run_status"], "completed")


if __name__ == "__main__":
    unittest.main()
