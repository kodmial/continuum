"""Physical /oc-cancel behavior: durable stop and exact run provenance."""

from __future__ import annotations

import unittest

from continuum import opencode_cancel as cancel
from continuum.task_snapshot import (
    is_generation_terminally_stopped,
    render_generation_recover,
    render_generation_stop,
)

OWNER = "octocat"
REPO = "octocat/project"
ISSUE = 400
CHILD_ID = "child-alpha"
CHILD_OTHER = "child-beta"
OPENCODE_PATH = ".github/workflows/continuum-opencode.yml"
OPENCODE_NAME = "OpenCode agent"


def comment(body, *, login=OWNER, association="OWNER", created="2026-10-23T10:00:00Z", comment_id=1):
    return {
        "body": body,
        "author_association": association,
        "user": {"login": login},
        "created_at": created,
        "updated_at": created,
        "id": comment_id,
    }


def run(
    run_id,
    title,
    *,
    status="in_progress",
    conclusion=None,
    event="issue_comment",
    name=OPENCODE_NAME,
    path=OPENCODE_PATH,
    created="2026-10-23T10:00:00Z",
):
    return {
        "id": run_id,
        "display_title": title,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "name": name,
        "path": path,
        "created_at": created,
    }


def plan(runs, **overrides):
    options = dict(
        repo=REPO,
        issue=ISSUE,
        workflow_paths=[OPENCODE_PATH],
        workflow_names=[OPENCODE_NAME],
    )
    options.update(overrides)
    return cancel.plan_cancellation(runs, **options)


class OperatorStopTests(unittest.TestCase):
    def test_round_trip_and_authority(self):
        body = cancel.render_operator_stop(REPO, ISSUE)
        parsed = cancel.parse_operator_stop(body)
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed.repo, parsed.issue, parsed.generation), (REPO, ISSUE, 1))
        self.assertNotIn("continuum-task-generation-stop", body)
        self.assertTrue(cancel.is_operator_stopped([comment(body)], repo=REPO, issue=ISSUE, owner_login=OWNER))

    def test_untrusted_lookalike_never_stops(self):
        body = cancel.render_operator_stop(REPO, ISSUE)
        forged = comment(body, login="stranger", association="NONE")
        self.assertFalse(cancel.is_operator_stopped([forged], repo=REPO, issue=ISSUE, owner_login=OWNER))

    def test_other_repo_issue_and_generation_do_not_stop(self):
        body = cancel.render_operator_stop(REPO, ISSUE)
        comments = [comment(body)]
        self.assertFalse(cancel.is_operator_stopped(comments, repo="octocat/other", issue=ISSUE, owner_login=OWNER))
        self.assertFalse(cancel.is_operator_stopped(comments, repo=REPO, issue=401, owner_login=OWNER))
        self.assertFalse(cancel.is_operator_stopped(comments, repo=REPO, issue=ISSUE, generation=2, owner_login=OWNER))

    def test_resume_must_be_strictly_later(self):
        stop = comment(cancel.render_operator_stop(REPO, ISSUE), comment_id=10, created="2026-10-23T10:00:00Z")
        resume_body = cancel.render_operator_resume(REPO, ISSUE)
        early = comment(resume_body, comment_id=9, created="2026-10-23T09:00:00Z")
        same_time_lower_id = comment(resume_body, comment_id=10, created="2026-10-23T10:00:00Z")
        late = comment(resume_body, comment_id=11, created="2026-10-23T11:00:00Z")
        for comments in ([early, stop], [same_time_lower_id, stop]):
            with self.subTest(comments=comments):
                self.assertTrue(cancel.is_operator_stopped(comments, repo=REPO, issue=ISSUE, owner_login=OWNER))
        self.assertFalse(cancel.is_operator_stopped([stop, late], repo=REPO, issue=ISSUE, owner_login=OWNER))

    def test_malformed_resume_timestamp_never_rearms(self):
        stop = comment(cancel.render_operator_stop(REPO, ISSUE), comment_id=10, created="2026-10-23T10:00:00Z")
        resume = comment(cancel.render_operator_resume(REPO, ISSUE), comment_id=11)
        del resume["created_at"]
        self.assertTrue(cancel.is_operator_stopped([stop, resume], repo=REPO, issue=ISSUE, owner_login=OWNER))

    def test_drift_and_operator_stops_are_independent(self):
        drift = comment(
            render_generation_stop(REPO, ISSUE, pinned_spec="a" * 64, live_spec="b" * 64, reason="drift"),
            comment_id=1,
            created="2026-10-23T10:00:00Z",
        )
        operator = comment(cancel.render_operator_stop(REPO, ISSUE), comment_id=2, created="2026-10-23T11:00:00Z")
        drift_recover = comment(render_generation_recover(REPO, ISSUE), comment_id=3, created="2026-10-23T12:00:00Z")
        self.assertFalse(
            is_generation_terminally_stopped([drift, drift_recover], repo=REPO, issue=ISSUE, owner_login=OWNER)
        )
        self.assertTrue(
            cancel.is_operator_stopped([drift, operator, drift_recover], repo=REPO, issue=ISSUE, owner_login=OWNER)
        )
        self.assertEqual(
            cancel.execution_stop_kind([drift, operator], repo=REPO, issue=ISSUE, owner_login=OWNER),
            "operator",
        )


class RunBindingTests(unittest.TestCase):
    def test_issue_comment_issue_dispatch_and_429_recovery_bind(self):
        self.assertEqual(cancel.bind_run_to_issue(run(1, "OpenCode issue #400")).kind, "issue")
        recovery = cancel.bind_run_to_issue(
            run(2, "OpenCode 429 recovery opencode-429-issue-mode-issue400-review5 gen 3", event="workflow_dispatch")
        )
        self.assertEqual((recovery.kind, recovery.issue, recovery.recovery_generation), ("recovery", 400, 3))
        delegated = cancel.bind_run_to_issue(run(3, "Child task [child-alpha] #400", event="workflow_dispatch"))
        self.assertEqual((delegated.kind, delegated.issue, delegated.child_id), ("delegated", 400, "child-alpha"))

    def test_non_issue_titles_never_bind(self):
        for title in (
            "OpenCode repair PR #400",
            "OpenCode review PR #400",
            "SubTask review #400",
            "Child review [child-alpha] #400",
            "Robot mentions OpenCode issue #400 in prose",
        ):
            with self.subTest(title=title):
                self.assertIsNone(cancel.bind_run_to_issue(run(1, title)))


class DelegationResolutionTests(unittest.TestCase):
    def resolve(self, role="", parent="", child_id="", *, credential=True):
        return cancel.resolve_cancellation_delegation(
            role,
            parent,
            child_id,
            parent_credential_available=credential,
        )

    def test_standalone_and_parent_are_local_only(self):
        standalone = self.resolve()
        self.assertEqual(standalone.status, "standalone")
        self.assertEqual(standalone.parent, "")
        self.assertEqual(standalone.child_id, "")
        self.assertEqual(self.resolve(None, None, None).status, "standalone")

        parent = self.resolve("parent")
        self.assertEqual(parent.status, "parent")
        self.assertEqual(parent.parent, "")
        self.assertEqual(parent.child_id, "")

    def test_valid_child_requires_both_bindings_and_a_credential(self):
        child = self.resolve("child", "owner/parent", "child-alpha")
        self.assertEqual(child.status, "child")
        self.assertEqual(child.parent, "owner/parent")
        self.assertEqual(child.child_id, "child-alpha")

        for child_id in ("a", "child-alpha", "child.alpha", "child:alpha", "child_alpha"):
            with self.subTest(child_id=child_id):
                self.assertEqual(
                    self.resolve("child", "owner/parent", child_id).status,
                    "child",
                )

    def test_missing_parent_credential_fails_closed(self):
        result = self.resolve("child", "owner/parent", "child-alpha", credential=False)
        self.assertEqual(result.status, "error")
        self.assertIn("credential", result.reason)
        self.assertEqual(result.parent, "")
        self.assertEqual(result.child_id, "")

    def test_missing_partial_or_invalid_child_binding_fails_closed(self):
        for parent, child_id in (
            ("", "child-alpha"),
            ("owner/parent", ""),
            ("", ""),
            ("owner", "child-alpha"),
            ("owner/parent/extra", "child-alpha"),
            ("owner/parent", "-child-alpha"),
            ("owner/parent", "child id"),
            ("owner/parent\n", "child-alpha"),
            ("owner/parent", "child-alpha\n"),
        ):
            with self.subTest(parent=parent, child_id=child_id):
                result = self.resolve("child", parent, child_id)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.parent, "")
                self.assertEqual(result.child_id, "")

    def test_unknown_role_and_contradictory_bindings_fail_closed(self):
        for role, parent, child_id in (
            ("standalone", "", ""),
            ("owner", "", ""),
            ("CHILD", "owner/parent", "child-alpha"),
            ("", "owner/parent", ""),
            ("", "", "child-alpha"),
            ("", "owner/parent", "child-alpha"),
            ("parent", "owner/parent", ""),
            ("parent", "", "child-alpha"),
            ("parent", "owner/parent", "child-alpha"),
        ):
            with self.subTest(role=role, parent=parent, child_id=child_id):
                result = self.resolve(role, parent, child_id)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.parent, "")
                self.assertEqual(result.child_id, "")

    def test_missing_role_after_observed_local_cancel_is_success_not_failure(self):
        # Live regression: a validated local run reached completed/cancelled,
        # then the controller failed while resolving a missing role through a
        # redundant REST variable lookup. The local cancellation is complete
        # and a repository with no role/binding is legitimately standalone.
        target = run(39940719498, "OpenCode issue #410", status="in_progress")
        plan_result = plan([target], issue=410)
        self.assertEqual([item["run_id"] for item in plan_result["targets"]], [39940719498])

        observed = run(
            39940719498,
            "OpenCode issue #410",
            status="completed",
            conclusion="cancelled",
        )
        self.assertEqual(cancel.observe_cancel_outcome(observed), "cancelled")
        self.assertFalse(cancel.needs_force_cancel(observed))

        resolution = self.resolve("", "", "", credential=False)
        self.assertEqual(resolution.status, "standalone")
        self.assertEqual(resolution.reason, "no role or Child binding")


class CancellationPlanTests(unittest.TestCase):
    def test_cancels_exact_issue_and_queued_duplicate(self):
        runs = [
            run(10, "OpenCode issue #400", status="in_progress"),
            run(11, "OpenCode issue #400", status="queued"),
        ]
        result = plan(runs)
        self.assertEqual(result["inventory"], "ok")
        self.assertEqual([target["run_id"] for target in result["targets"]], [10, 11])

    def test_never_cancels_unrelated_ci_pr_agent_or_other_issue(self):
        runs = [
            run(1, "OpenCode issue #401", status="in_progress"),
            run(2, "OpenCode repair PR #400", status="in_progress"),
            run(3, "PR-Agent review", status="in_progress", name="PR-Agent", path=".github/workflows/continuum-pr-agent.yml"),
            run(4, "CI", status="in_progress", name="CI", path=".github/workflows/ci.yml"),
            run(5, "OpenCode issue #400", status="completed", conclusion="success"),
        ]
        result = plan(runs)
        self.assertEqual(result["targets"], [])
        reasons = {item["run_id"]: item["reason"] for item in result["ignored"]}
        self.assertIn("another issue", reasons[1])
        for run_id in (2, 3, 4):
            self.assertTrue(reasons[run_id])
        self.assertIn("already completed", reasons[5])

    def test_same_title_but_wrong_workflow_path_is_rejected(self):
        runs = [
            run(6, "OpenCode issue #400", status="in_progress", path=".github/workflows/unrelated.yml"),
        ]
        result = plan(runs)
        self.assertEqual(result["targets"], [])
        self.assertIn("workflow provenance", result["ignored"][0]["reason"])

    def test_dogfood_and_installed_caller_paths_are_both_accepted(self):
        runs = [
            run(7, "OpenCode issue #400", status="in_progress", path=".github/workflows/opencode.yml"),
            run(8, "OpenCode issue #400", status="in_progress", path=".github/workflows/continuum-opencode.yml"),
        ]
        result = plan(
            runs,
            workflow_paths=[
                ".github/workflows/continuum-opencode.yml",
                ".github/workflows/opencode.yml",
            ],
        )
        self.assertEqual([target["run_id"] for target in result["targets"]], [7, 8])

    def test_429_recovery_is_cancelled_for_the_exact_issue(self):
        runs = [
            run(20, "OpenCode 429 recovery opencode-429-issue-mode-issue400 gen 0", event="workflow_dispatch"),
            run(21, "OpenCode 429 recovery opencode-429-issue-mode-issue401 gen 0", event="workflow_dispatch"),
        ]
        result = plan(runs)
        self.assertEqual([target["run_id"] for target in result["targets"]], [20])

    def test_control_plane_run_is_excluded(self):
        runs = [run(99, "OpenCode issue #400", status="in_progress")]
        result = plan(runs, exclude_run_id=99)
        self.assertEqual(result["targets"], [])

    def test_non_default_generation_requires_association(self):
        runs = [run(30, "OpenCode issue #400", status="in_progress")]
        self.assertEqual(plan(runs, generation=2)["targets"], [])
        self.assertEqual(
            [target["run_id"] for target in plan(runs, generation=2, associated_run_ids=[30])["targets"]],
            [30],
        )

    def test_delegated_child_requires_exact_child_identity(self):
        runs = [
            run(40, "Child task [child-alpha] #400", event="workflow_dispatch", name="SubTask", path=".github/workflows/continuum-child-worker.yml"),
            run(41, "Child task [child-beta] #400", event="workflow_dispatch", name="SubTask", path=".github/workflows/continuum-child-worker.yml"),
        ]
        result = plan(
            runs,
            workflow_paths=[".github/workflows/continuum-child-worker.yml"],
            workflow_names=["SubTask"],
            child_id=CHILD_ID,
        )
        self.assertEqual([target["run_id"] for target in result["targets"]], [40])
        reasons = {item["run_id"]: item["reason"] for item in result["ignored"]}
        self.assertIn("another Child", reasons[41])

    def test_legacy_subtask_title_is_never_guessed(self):
        runs = [
            run(50, "SubTask #400", event="workflow_dispatch", name="SubTask", path=".github/workflows/continuum-child-worker.yml")
        ]
        options = dict(
            workflow_paths=[".github/workflows/continuum-child-worker.yml"],
            workflow_names=["SubTask"],
            child_id=CHILD_ID,
        )
        self.assertEqual(plan(runs, **options)["targets"], [])
        self.assertEqual(
            [target["run_id"] for target in plan(runs, associated_run_ids=[50], **options)["targets"]],
            [50],
        )

    def test_malformed_or_unknown_inventory_fails_closed(self):
        for runs in (None, {"workflow_runs": []}, [None], [{"display_title": "OpenCode issue #400"}]):
            with self.subTest(runs=runs):
                result = plan(runs)
                self.assertEqual(result["inventory"], "unavailable")
                self.assertEqual(result["targets"], [])

    def test_unknown_active_status_fails_safe_for_that_run(self):
        runs = [run(60, "OpenCode issue #400", status="mystery")]
        result = plan(runs)
        self.assertEqual(result["targets"], [])
        self.assertIn("recognized active", result["ignored"][0]["reason"])


class ResponseTests(unittest.TestCase):
    def test_status_classification(self):
        self.assertEqual(cancel.classify_cancel_response(202), "accepted")
        self.assertEqual(cancel.classify_cancel_response(204), "accepted")
        self.assertEqual(cancel.classify_cancel_response(409), "conflict")
        self.assertEqual(cancel.classify_cancel_response(403), "forbidden")
        self.assertEqual(cancel.classify_cancel_response(429), "rate-limited")
        self.assertEqual(cancel.classify_cancel_response(503), "server-error")
        self.assertEqual(cancel.classify_cancel_response(422), "client-error")
        self.assertEqual(cancel.classify_cancel_response("nope"), "unknown")
        self.assertTrue(cancel.is_retryable_cancel_status(429))
        self.assertTrue(cancel.is_retryable_cancel_status(500))
        self.assertFalse(cancel.is_retryable_cancel_status(403))
        self.assertFalse(cancel.is_retryable_cancel_status(409))

    def test_202_is_not_a_stopped_job(self):
        accepted = run(1, "OpenCode issue #400", status="in_progress", conclusion=None)
        self.assertEqual(cancel.classify_cancel_response(202), "accepted")
        self.assertEqual(cancel.observe_cancel_outcome(accepted), "active:in_progress")
        self.assertTrue(cancel.needs_force_cancel(accepted))
        cancelled = run(1, "OpenCode issue #400", status="completed", conclusion="cancelled")
        self.assertEqual(cancel.observe_cancel_outcome(cancelled), "cancelled")
        self.assertFalse(cancel.needs_force_cancel(cancelled))
        completed = run(1, "OpenCode issue #400", status="completed", conclusion="success")
        self.assertEqual(cancel.observe_cancel_outcome(completed), "completed:success")
        self.assertFalse(cancel.needs_force_cancel(completed))


if __name__ == "__main__":
    unittest.main()
