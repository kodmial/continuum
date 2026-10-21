"""Durable operator STOP, cancellation targeting and fence observation (#346)."""

from __future__ import annotations

import unittest

from continuum import issue_stop as stop
from continuum.task_snapshot import (
    is_generation_terminally_stopped,
    render_generation_recover,
    render_generation_stop,
)

REPO = "owner/repo"
ISSUE = 346
OWNER = "owner"
SPEC = "a" * 64


def comment(body, *, created_at="2026-10-22T10:00:00Z", comment_id=1, author="github-actions[bot]", association="NONE"):
    return {
        "body": body,
        "created_at": created_at,
        "id": comment_id,
        "author_association": association,
        "user": {"login": author},
    }


def stop_comment(*, created_at="2026-10-22T10:00:00Z", comment_id=2, reason="operator"):
    return comment(
        render_generation_stop(
            REPO, ISSUE, pinned_spec=SPEC, live_spec=SPEC, reason=reason
        ),
        created_at=created_at,
        comment_id=comment_id,
    )


class StopIntentTests(unittest.TestCase):
    def test_intent_round_trip_and_fence(self):
        body = stop.render_stop_intent(
            REPO, ISSUE, generation=1, fence=stop.make_fence(42, "now")
        )
        parsed = stop.parse_stop_intent(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.repo, REPO)
        self.assertEqual(parsed.issue, ISSUE)
        self.assertEqual(parsed.generation, 1)
        self.assertIn("continuum-operator-stop-intent", body)
        self.assertNotIn("/oc-cancel", body)

    def test_select_intent_is_first_writer_wins(self):
        first = comment(
            stop.render_stop_intent(REPO, ISSUE, fence="10:aaa"),
            created_at="2026-10-22T10:00:00Z",
            comment_id=10,
        )
        second = comment(
            stop.render_stop_intent(REPO, ISSUE, fence="11:bbb"),
            created_at="2026-10-22T10:01:00Z",
            comment_id=11,
        )
        selected = stop.select_stop_intent(
            [second, first], repo=REPO, issue=ISSUE, owner_login=OWNER
        )
        self.assertEqual(selected.fence, "10:aaa")

    def test_prose_and_untrusted_authors_never_create_a_stop(self):
        prose = comment(
            "Should I post /oc-cancel here?",
            author="stranger",
            association="NONE",
        )
        forged = render_generation_stop(
            REPO, ISSUE, pinned_spec=SPEC, live_spec=SPEC, reason="operator"
        )
        untrusted = comment(forged, author="stranger", association="NONE")
        self.assertIsNone(stop.parse_stop_intent(prose["body"]))
        self.assertFalse(
            stop.stop_is_active(
                [prose, untrusted], repo=REPO, issue=ISSUE, owner_login=OWNER
            )
        )

    def test_operator_stop_uses_shared_generation_stop_identity(self):
        intent = comment(
            stop.render_stop_intent(REPO, ISSUE, fence="12:ccc"),
            created_at="2026-10-22T10:00:00Z",
            comment_id=12,
        )
        terminal = stop_comment(created_at="2026-10-22T10:00:01Z", comment_id=13)
        self.assertTrue(
            stop.stop_is_active(
                [intent, terminal], repo=REPO, issue=ISSUE, owner_login=OWNER
            )
        )
        self.assertTrue(
            is_generation_terminally_stopped(
                [intent, terminal], repo=REPO, issue=ISSUE, owner_login=OWNER
            )
        )

    def test_recovery_rearms_but_ready_restoration_does_not(self):
        intent = comment(
            stop.render_stop_intent(REPO, ISSUE, fence="12:ccc"),
            created_at="2026-10-22T10:00:00Z",
            comment_id=12,
        )
        terminal = stop_comment(created_at="2026-10-22T10:00:01Z", comment_id=13)
        recover = comment(
            render_generation_recover(REPO, ISSUE, generation=1),
            created_at="2026-10-22T10:01:00Z",
            comment_id=14,
        )
        ready = comment(
            "<!-- ready state change is not a recovery -->",
            created_at="2026-10-22T10:02:00Z",
            comment_id=15,
        )
        self.assertFalse(
            stop.stop_is_active(
                [intent, terminal, recover],
                repo=REPO,
                issue=ISSUE,
                owner_login=OWNER,
            )
        )
        self.assertTrue(
            stop.stop_is_active(
                [intent, terminal, recover, ready],
                repo=REPO,
                issue=ISSUE,
                owner_login=OWNER,
            )
            is False
        )

    def test_duplicate_stop_is_idempotent(self):
        intent = comment(
            stop.render_stop_intent(REPO, ISSUE, fence="12:ccc"),
            created_at="2026-10-22T10:00:00Z",
            comment_id=12,
        )
        first = stop_comment(created_at="2026-10-22T10:00:01Z", comment_id=13)
        duplicate = stop_comment(created_at="2026-10-22T10:00:02Z", comment_id=14)
        selected = stop.select_stop_intent(
            [intent, first, duplicate], repo=REPO, issue=ISSUE, owner_login=OWNER
        )
        self.assertEqual(selected.fence, "12:ccc")
        self.assertTrue(
            stop.stop_is_active(
                [intent, first, duplicate],
                repo=REPO,
                issue=ISSUE,
                owner_login=OWNER,
            )
        )


def run(run_id, *, path=".github/workflows/continuum-opencode.yml", status="in_progress", conclusion=None, title="OpenCode issue #346"):
    return {
        "id": run_id,
        "path": path,
        "status": status,
        "conclusion": conclusion,
        "display_title": title,
        "head_branch": "opencode/issue346-1",
    }


class RunSelectionTests(unittest.TestCase):
    PATHS = (".github/workflows/continuum-opencode.yml",)

    def test_only_matching_issue_and_workflow_runs_are_targeted(self):
        runs = [
            run(1),
            run(2, status="queued"),
            run(3, title="OpenCode issue #999"),
            run(4, path=".github/workflows/opencode.yml"),
            run(5, status="completed", conclusion="cancelled"),
            run(6, title="OpenCode issue #346 generation 2"),
        ]
        selection = stop.select_run_targets(
            runs, issue=ISSUE, generation=1, workflow_paths=self.PATHS
        )
        self.assertEqual(selection.target_ids, [1, 2])

    def test_parent_delegated_worker_run_is_targeted(self):
        runs = [run(
            9,
            path=".github/workflows/continuum-child-worker.yml",
            title="SubTask #346",
        )]
        selection = stop.select_run_targets(
            runs,
            issue=ISSUE,
            generation=1,
            workflow_paths=(".github/workflows/continuum-child-worker.yml",),
        )
        self.assertEqual(selection.target_ids, [9])

    def test_display_title_alone_is_not_enough(self):
        runs = [run(7, path=".github/workflows/unrelated.yml")]
        selection = stop.select_run_targets(
            runs, issue=ISSUE, generation=1, workflow_paths=self.PATHS
        )
        self.assertEqual(selection.target_ids, [])
        self.assertTrue(selection.skipped)

    def test_other_generation_is_not_cancelled(self):
        runs = [run(8, title="OpenCode issue #346 generation 2")]
        selection = stop.select_run_targets(
            runs, issue=ISSUE, generation=1, workflow_paths=self.PATHS
        )
        self.assertEqual(selection.target_ids, [])


class CancellationObservationTests(unittest.TestCase):
    def intent(self):
        return stop.OperatorStopIntent(
            repo=REPO, issue=ISSUE, generation=1, fence="12:ccc"
        )

    def test_pending_is_not_reported_stopped(self):
        observation = stop.observe_cancellation(
            intent=self.intent(),
            target_ids=[1],
            runs=[run(1, status="in_progress")],
        )
        self.assertEqual(observation.state, stop.CANCEL_PENDING)
        self.assertFalse(observation.terminal)

    def test_all_terminal_is_stopped(self):
        observation = stop.observe_cancellation(
            intent=self.intent(),
            target_ids=[1, 2],
            runs=[
                run(1, status="completed", conclusion="cancelled"),
                run(2, status="completed", conclusion="failure"),
            ],
        )
        self.assertEqual(observation.state, stop.STOPPED)
        self.assertTrue(observation.terminal)

    def test_missing_run_is_partial_never_stopped(self):
        observation = stop.observe_cancellation(
            intent=self.intent(),
            target_ids=[1],
            runs=[],
        )
        self.assertEqual(observation.state, stop.PARTIAL)
        self.assertNotEqual(observation.state, stop.STOPPED)

    def test_cancel_api_failure_is_failed(self):
        observation = stop.observe_cancellation(
            intent=self.intent(),
            target_ids=[1],
            runs=[run(1)],
            cancel_failure="403 Forbidden",
        )
        self.assertEqual(observation.state, stop.FAILED)
        self.assertIn("403", observation.failure)

    def test_no_targets_is_stopped_without_false_positives(self):
        observation = stop.observe_cancellation(
            intent=self.intent(), target_ids=[], runs=[]
        )
        self.assertEqual(observation.state, stop.STOPPED)

    def test_publication_is_blocked_while_stop_is_active(self):
        comments = [
            comment(
                stop.render_stop_intent(REPO, ISSUE, fence="12:ccc"),
                created_at="2026-10-22T10:00:00Z",
                comment_id=12,
            ),
            stop_comment(created_at="2026-10-22T10:00:01Z"),
        ]
        self.assertTrue(
            stop.stop_blocks_publication(
                comments, repo=REPO, issue=ISSUE, owner_login=OWNER
            )
        )


if __name__ == "__main__":
    unittest.main()
