"""Regression tests for the live contract-canary bootstrap (kodmial/continuum#458)."""

import json
import os
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from continuum import contract_canary as canary
from continuum import contract_observation as observation
from continuum import contract_qualification as contract

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
MERGE = "b" * 40
OTHER_HEAD = "c" * 40
WORKER_HEAD = "d" * 40
WORKER_RUN_ID = 9001
ROOT = Path(__file__).resolve().parents[1]


def iso(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def marker(repo=canary.CANARY_OWNER_REPO, task=canary.CANARY_TASK_ID):
    return "<!-- continuum-contract-canary repo={} task={} -->".format(repo, task)


def canary_issue(
    number=100,
    state="open",
    labels=(canary.CANARY_READY_LABEL,),
    body=None,
    created=60,
    closed=None,
    author="github-actions[bot]",
    association="NONE",
):
    return {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "body": marker() if body is None else body,
        "created_at": iso(created),
        "updated_at": iso(created),
        "closed_at": iso(closed) if closed is not None else None,
        "user": {"login": author},
        "author_association": association,
    }


def ordinary_issue(number=200):
    return {
        "number": number,
        "state": "open",
        "labels": [{"name": canary.CANARY_READY_LABEL}],
        "body": "A normal scheduler task, not a canary.",
        "created_at": iso(60),
        "updated_at": iso(60),
        "closed_at": None,
        "user": {"login": "github-actions[bot]"},
        "author_association": "NONE",
    }


def pr(
    number=41,
    body="Closes #100",
    state="closed",
    merged_at=None,
    head=HEAD,
    merge=MERGE,
    updated=30,
    issue=100,
):
    return {
        "number": number,
        "state": state,
        "body": body,
        "title": "fix: canary fixture",
        "headRefName": "opencode/issue{}-{}".format(issue, WORKER_RUN_ID),
        "headRefOid": head,
        "mergeCommit": {"oid": merge},
        "mergedAt": iso(updated) if merged_at is None else merged_at,
        "updated_at": iso(updated),
        "reviewDecision": "APPROVED",
    }


def run(
    name,
    head=HEAD,
    status="completed",
    conclusion="success",
    run_id=None,
    display_title="",
    head_branch="",
):
    payload = {
        "name": name,
        "workflowName": name,
        "headSha": head,
        "status": status,
        "conclusion": conclusion,
    }
    if run_id is not None:
        payload["databaseId"] = run_id
    if display_title:
        payload["displayTitle"] = display_title
    if head_branch:
        payload["headBranch"] = head_branch
    return payload


def worker_run(
    issue=100,
    run_id=WORKER_RUN_ID,
    status="completed",
    conclusion="success",
):
    return run(
        "OpenCode agent",
        head=WORKER_HEAD,
        status=status,
        conclusion=conclusion,
        run_id=run_id,
        display_title="OpenCode issue #{}".format(issue),
        head_branch="main",
    )


def exact_head_runs(head=HEAD, issue=100):
    return [
        run("CI", head=head),
        run(contract.CONTRACT_WORKFLOW_NAME, head=head),
        worker_run(issue=issue),
    ]


def publication(
    issue=100,
    run_id=WORKER_RUN_ID,
    pr_number=41,
    branch=None,
    head=HEAD,
    repo=canary.CANARY_OWNER_REPO,
    schema_version=1,
):
    return {
        "schema_version": schema_version,
        "repo": repo,
        "issue": issue,
        "run_id": str(run_id),
        "run_attempt": "1",
        "branch": (
            branch
            if branch is not None
            else "opencode/issue{}-{}".format(issue, run_id)
        ),
        "pr_number": pr_number,
        "head_sha": head,
    }


def repair_publication_receipt(
    base,
    head,
    pr_number=41,
    run_id=9001,
    branch="opencode/issue100-9001",
    repo=canary.CANARY_OWNER_REPO,
    schema_version=1,
):
    return {
        "schema_version": schema_version,
        "repo": repo,
        "run_id": str(run_id),
        "run_attempt": "1",
        "pr_number": pr_number,
        "branch": branch,
        "base_sha": base,
        "head_sha": head,
    }


def repair_status(
    base,
    head,
    pr_number=41,
    state="success",
    context=canary.CANARY_REPAIR_STATUS_CONTEXT,
    creator="kodmial",
    target="https://github.com/{}/actions/runs/9001".format(
        canary.CANARY_OWNER_REPO
    ),
    publication_payload="default",
):
    return {
        "context": context,
        "state": state,
        "description": "pr={} base={} head={}".format(
            pr_number, base[:12], head[:12]
        ),
        "creator": {"login": creator},
        "target_url": target,
        "publication": (
            repair_publication_receipt(
                base, head, pr_number=pr_number
            )
            if publication_payload == "default"
            else publication_payload
        ),
    }


def pr_commit(
    sha,
    author="github-actions[bot]",
    committer="github-actions[bot]",
    message="fix: canary implementation",
    parents=1,
    verified=False,
    statuses=(),
):
    identities = {
        "github-actions[bot]": (
            "github-actions[bot]",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ),
        "web-flow": ("GitHub", "noreply@github.com"),
    }

    def person(login):
        name, email = identities.get(
            login, (login or "", "{}@example.com".format(login or ""))
        )
        return {"login": login, "name": name, "email": email}

    commit_author = person(author) if author else {"name": "", "email": ""}
    commit_committer = (
        person(committer) if committer else {"name": "", "email": ""}
    )
    return {
        "sha": sha,
        "author": person(author) if author else None,
        "committer": person(committer) if committer else None,
        "commit": {
            "author": commit_author,
            "committer": commit_committer,
            "message": message,
            "verification": {
                "verified": verified,
                "reason": "valid" if verified else "unsigned",
            },
        },
        "parents": [
            {"sha": "{:040x}".format(index + 1)}
            for index in range(parents)
        ],
        "statuses": list(statuses),
    }


def completed(candidate, runs=None, issue=100, publication_payload=None):
    """Validate canary completion with the run-scoped publication receipt."""
    pull = candidate or {}
    return canary.validate_completed_canary(
        candidate,
        exact_head_runs(issue=issue) if runs is None else runs,
        issue,
        (
            publication(
                issue=issue,
                pr_number=pull.get("number", 41),
                branch=pull.get("headRefName"),
            )
            if publication_payload is None
            else publication_payload
        ),
    )


class DiscoveryTest(unittest.TestCase):
    def test_captured_missing_variable_run_now_bootstraps(self):
        captured = json.loads(
            (ROOT / "tests/fixtures/contract-canary/run-43497991808.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(captured["canary_issue_env"], "")
        self.assertEqual(captured["databaseId"], 43497991808)
        self.assertEqual(
            captured["headSha"], "e7f745eb3680ece0337c9af051d691a484f8aba4"
        )
        decision = canary.resolve_canary(
            [ordinary_issue()], configured_issue="", now=NOW
        )
        self.assertEqual(decision.action, "create")
        self.assertEqual(decision.reason, "initial-bootstrap")
        self.assertIsNone(decision.issue_number)

    def test_configured_issue_still_overrides_discovery(self):
        decision = canary.resolve_canary(
            [canary_issue(number=100)], configured_issue=77, now=NOW
        )
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 77)
        self.assertEqual(decision.reason, "configured-override")

    def test_invalid_configured_issue_fails_closed(self):
        for value in ("abc", "0", "-1", "1.5", " #1 "):
            with self.subTest(value=value):
                with self.assertRaises(canary.CanaryBootstrapError):
                    canary.resolve_canary([], configured_issue=value, now=NOW)

    def test_configured_issue_is_observed_even_when_inventory_is_empty(self):
        decision = canary.resolve_canary(
            [], configured_issue=999, now=NOW
        )
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 999)

    def test_open_owned_canary_is_observed(self):
        decision = canary.resolve_canary([canary_issue()], now=NOW)
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 100)
        self.assertFalse(decision.completed)

    def test_stalled_open_canary_is_observed_not_duplicated(self):
        issue = canary_issue(created=4 * 60 * 60)
        decision = canary.resolve_canary([issue], now=NOW)
        self.assertEqual(decision.action, "observe")
        observed = observation.observe_canary(issue, now=NOW)
        verdict = contract.evaluate_canary(observed.state)
        self.assertEqual(verdict.outcome, contract.CANARY_STALLED_RECOVERABLE)

    def test_completed_canary_inside_window_is_observed(self):
        issue = canary_issue(state="closed", created=2 * 24 * 60 * 60, closed=30)
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[pr()],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(),
        )
        self.assertEqual(decision.action, "observe")
        self.assertTrue(decision.completed)
        self.assertEqual(decision.issue_number, 100)
        self.assertGreaterEqual(decision.age_seconds, 2 * 24 * 60 * 60)

    def test_completed_canary_recognizes_repository_qualified_reference(self):
        issue = canary_issue(state="closed", created=2 * 24 * 60 * 60, closed=30)
        candidate = pr(body="Closes kodmial/continuum#100")
        candidate["headRefName"] = "feature/noncanonical"
        observed = observation.observe_canary(
            issue,
            pull_requests=[candidate],
            now=NOW,
            repository="kodmial/continuum",
        )
        self.assertTrue(observed.completed)
        self.assertEqual(
            canary.select_implementation_pr(100, [candidate])["number"], 41
        )
        candidate["headRefName"] = "opencode/issue100-{}".format(WORKER_RUN_ID)
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[candidate],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(),
        )
        self.assertTrue(decision.completed)

    def test_completed_canary_recognizes_issue_url_reference(self):
        issue = canary_issue(state="closed", created=2 * 24 * 60 * 60, closed=30)
        candidate = pr(
            body="Closes https://github.com/kodmial/continuum/issues/100"
        )
        candidate["headRefName"] = "feature/noncanonical"
        observed = observation.observe_canary(
            issue,
            pull_requests=[candidate],
            now=NOW,
            repository="kodmial/continuum",
        )
        self.assertTrue(observed.completed)
        self.assertEqual(
            canary.select_implementation_pr(100, [candidate])["number"], 41
        )
        candidate["headRefName"] = "opencode/issue100-{}".format(WORKER_RUN_ID)
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[candidate],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(),
        )
        self.assertTrue(decision.completed)

    def test_completed_canary_after_window_bootstraps_one_fresh_issue(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[pr()],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(),
        )
        self.assertEqual(decision.action, "create")
        self.assertEqual(decision.reason, "window-elapsed")
        self.assertEqual(decision.previous_issue_number, 100)

    def test_closed_without_merged_pr_fails_closed(self):
        issue = canary_issue(state="closed", created=2 * 24 * 60 * 60)
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not conclusively complete"
        ):
            canary.resolve_canary([issue], now=NOW)

    def test_closed_with_stale_head_evidence_fails_closed(self):
        issue = canary_issue(state="closed", created=2 * 24 * 60 * 60, closed=30)
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[pr(head=HEAD)],
                runs=exact_head_runs(head=OTHER_HEAD),
                now=NOW,
            )

    def test_closed_without_created_timestamp_fails_closed(self):
        issue = canary_issue()
        issue["created_at"] = ""
        with self.assertRaises(canary.CanaryBootstrapError):
            canary.resolve_canary([issue], now=NOW)

    def test_duplicate_open_canary_issues_fail_closed(self):
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "multiple open owned canary"
        ):
            canary.resolve_canary(
                [canary_issue(number=100), canary_issue(number=101)], now=NOW
            )

    def test_historical_closed_canaries_select_the_newest(self):
        older = canary_issue(
            number=100, state="closed", created=9 * 24 * 60 * 60, closed=30
        )
        newer = canary_issue(
            number=101, state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        pulls = [
            pr(number=41, body="Closes #100", updated=60),
            pr(number=42, body="Closes #101", updated=30, issue=101),
        ]
        decision = canary.resolve_canary(
            [older, newer],
            pull_requests=pulls,
            runs=exact_head_runs(issue=101),
            now=NOW,
            worker_publication=publication(issue=101, pr_number=42),
        )
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 101)
        self.assertTrue(decision.completed)

    def test_newest_closed_canary_must_be_conclusively_complete(self):
        older = canary_issue(
            number=100, state="closed", created=9 * 24 * 60 * 60, closed=30
        )
        newer = canary_issue(
            number=101, state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not conclusively complete"
        ):
            canary.resolve_canary(
                [older, newer],
                pull_requests=[pr(number=41, body="Closes #100")],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_window_elapsed_replaces_only_the_newest_completed_canary(self):
        older = canary_issue(
            number=100, state="closed", created=30 * 24 * 60 * 60, closed=30
        )
        newer = canary_issue(
            number=101, state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        pulls = [
            pr(number=41, body="Closes #100", updated=60),
            pr(number=42, body="Closes #101", updated=30, issue=101),
        ]
        decision = canary.resolve_canary(
            [older, newer],
            pull_requests=pulls,
            runs=exact_head_runs(issue=101),
            now=NOW,
            worker_publication=publication(issue=101, pr_number=42),
        )
        self.assertEqual(decision.action, "create")
        self.assertEqual(decision.reason, "window-elapsed")
        self.assertEqual(decision.previous_issue_number, 101)

    def test_open_canary_wins_over_closed_history(self):
        closed = canary_issue(
            number=100, state="closed", created=9 * 24 * 60 * 60, closed=30
        )
        active = canary_issue(number=101, created=60)
        decision = canary.resolve_canary(
            [closed, active],
            pull_requests=[pr(number=41, body="Closes #100")],
            runs=exact_head_runs(),
            now=NOW,
        )
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 101)
        self.assertFalse(decision.completed)

    def test_missing_marker_is_not_a_canary(self):
        decision = canary.resolve_canary([ordinary_issue()], now=NOW)
        self.assertEqual(decision.action, "create")

    def test_forged_marker_from_untrusted_author_fails_closed(self):
        issue = canary_issue(author="random-user", association="NONE")
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "untrusted actor"
        ):
            canary.resolve_canary([issue], now=NOW)

    def test_marker_for_another_repository_fails_closed(self):
        issue = canary_issue(
            body=marker(repo="someone/else")
        )
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "another repository"
        ):
            canary.resolve_canary([issue], now=NOW)

    def test_marker_for_another_task_fails_closed(self):
        issue = canary_issue(body=marker(task="other-canary"))
        with self.assertRaisesRegex(canary.CanaryBootstrapError, "another task"):
            canary.resolve_canary([issue], now=NOW)

    def test_malformed_marker_fails_closed(self):
        issue = canary_issue(body=marker() + " trailing text")
        with self.assertRaisesRegex(canary.CanaryBootstrapError, "malformed"):
            canary.resolve_canary([issue], now=NOW)

    def test_duplicate_marker_lines_fail_closed(self):
        issue = canary_issue(body=marker() + "\n" + marker())
        with self.assertRaisesRegex(canary.CanaryBootstrapError, "duplicated"):
            canary.resolve_canary([issue], now=NOW)

    def test_misplaced_marker_fails_closed(self):
        # A marker that is embedded in other text is malformed metadata. It
        # must not be skipped, because skipping it would return
        # "initial-bootstrap" and authorize creating another canary issue.
        issue = canary_issue(body="metadata: " + marker())
        with self.assertRaisesRegex(canary.CanaryBootstrapError, "misplaced"):
            canary.resolve_canary([issue], now=NOW)

    def test_missing_ready_label_fails_closed(self):
        issue = canary_issue(labels=())
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "required automation:ready"
        ):
            canary.resolve_canary([issue], now=NOW)

    def test_pull_request_with_canary_marker_is_ignored(self):
        pull = canary_issue(number=300)
        pull["pull_request"] = {"url": "https://example.invalid"}
        decision = canary.resolve_canary([pull], now=NOW)
        self.assertEqual(decision.action, "create")

    def test_bootstrap_is_refused_outside_the_owner_repository(self):
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "only allowed in kodmial/continuum"
        ):
            canary.resolve_canary([], expected_repo="someone/else", now=NOW)

    def test_read_failures_fail_closed_without_creating(self):
        for status in (403, 404, 429, 422, 500, 503, "unreadable"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(
                    canary.CanaryBootstrapError, "read failed"
                ):
                    canary.resolve_canary(
                        [], read_error="HTTP {}".format(status), now=NOW
                    )

    def test_incomplete_pagination_fails_closed(self):
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "pagination was incomplete"
        ):
            canary.resolve_canary([], pagination_complete=False, now=NOW)

    def test_large_inventory_still_finds_the_single_canary(self):
        issues = [ordinary_issue(number=1000 + index) for index in range(250)]
        issues.append(canary_issue(number=100))
        decision = canary.resolve_canary(issues, now=NOW)
        self.assertEqual(decision.action, "observe")
        self.assertEqual(decision.issue_number, 100)

    def test_deleted_canary_leaves_no_evidence_and_bootstraps_once(self):
        # A deleted issue is simply absent from the inventory. There is no
        # completed canary to observe and no marker to trust, so the next run
        # performs the normal initial bootstrap instead of inventing evidence.
        decision = canary.resolve_canary([ordinary_issue()], now=NOW)
        self.assertEqual(decision.action, "create")
        self.assertEqual(decision.reason, "initial-bootstrap")
        created = canary_issue(number=100)
        followed = canary.resolve_canary([ordinary_issue(), created], now=NOW)
        self.assertEqual(followed.action, "observe")
        self.assertEqual(followed.issue_number, 100)

    def test_duplicate_schedules_do_not_create_twice(self):
        first = canary.resolve_canary([], now=NOW)
        self.assertEqual(first.action, "create")
        created = canary_issue(number=100)
        second = canary.resolve_canary([created], now=NOW)
        third = canary.resolve_canary([created], now=NOW)
        self.assertEqual(second.action, "observe")
        self.assertEqual(third.action, "observe")
        self.assertEqual(second.issue_number, 100)
        self.assertEqual(third.issue_number, 100)


class CompletionEvidenceTest(unittest.TestCase):
    def test_exact_head_ci_and_contract_evidence_passes(self):
        self.assertIsNone(
            canary.validate_completed_canary(
                pr(), exact_head_runs(), 100, publication()
            )
        )

    def test_missing_worker_evidence_fails(self):
        runs = [run("CI"), run(contract.CONTRACT_WORKFLOW_NAME)]
        problem = canary.validate_completed_canary(pr(), runs, 100)
        self.assertIn("worker-evidence-absent", problem)

    def test_worker_evidence_for_another_issue_fails(self):
        runs = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            worker_run(issue=999, run_id=7777),
        ]
        problem = canary.validate_completed_canary(pr(), runs, 100)
        self.assertIn("worker-evidence-absent", problem)

    def test_failed_worker_evidence_fails(self):
        runs = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            worker_run(conclusion="failure"),
        ]
        problem = canary.validate_completed_canary(pr(), runs, 100)
        self.assertIn("worker-evidence-completed-failure", problem)

    def test_completion_worker_run_id_is_recorded(self):
        selected = canary.completion_worker_run(100, exact_head_runs(), pr())
        self.assertEqual(selected["databaseId"], WORKER_RUN_ID)

    def test_completion_worker_run_id_is_read_from_the_branch(self):
        self.assertEqual(
            canary.completion_worker_run_id(pr(), 100), WORKER_RUN_ID
        )
        self.assertIsNone(canary.completion_worker_run_id(pr(), 999))
        self.assertIsNone(canary.completion_worker_run_id(None, 100))

    def test_recovery_worker_run_is_tied_by_implementation_branch(self):
        candidate = pr()
        candidate["headRefName"] = "opencode/issue100-{}".format(WORKER_RUN_ID)
        recovery = run(
            "OpenCode 429 recovery opencode-429-issue-issue100 gen 1",
            head=WORKER_HEAD,
            run_id=WORKER_RUN_ID,
            display_title=(
                "OpenCode 429 recovery opencode-429-issue-issue100 gen 1"
            ),
            head_branch="main",
        )
        runs = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            recovery,
        ]
        self.assertIsNone(
            canary.validate_completed_canary(candidate, runs, 100, publication())
        )
        selected = canary.completion_worker_run(100, runs, candidate)
        self.assertEqual(selected["databaseId"], WORKER_RUN_ID)

    def test_worker_run_from_another_branch_generation_fails(self):
        candidate = pr()
        candidate["headRefName"] = "opencode/issue100-7777"
        runs = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            worker_run(),
        ]
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("worker-evidence-absent", problem)

    def test_branch_bound_run_of_another_issue_is_not_completion_evidence(self):
        """A manual PR must not reuse another issue's successful run id.

        The implementation branch can claim any run id, so the run itself has
        to bind to the canary issue through its own canonical title.
        """
        candidate = pr()
        candidate["headRefName"] = "opencode/issue100-7777"
        runs = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            worker_run(issue=999, run_id=7777),
        ]
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("worker-evidence-absent", problem)
        self.assertIsNone(canary.completion_worker_run(100, runs, candidate))

    def test_delegated_worker_run_cannot_authorize_canary_completion(self):
        """A delegated child task's title names another repository's issue.

        Only the canonical OpenCode issue/recovery identity may bind the worker
        run to this repository's canary issue.
        """
        candidate = pr()
        candidate["headRefName"] = "opencode/issue100-7777"
        delegated = run(
            "Worker task [child] #100",
            head=WORKER_HEAD,
            run_id=7777,
            display_title="Worker task [child] #100",
            head_branch="main",
        )
        runs = [run("CI"), run(contract.CONTRACT_WORKFLOW_NAME), delegated]
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("worker-evidence-absent", problem)
        self.assertIsNone(canary.completion_worker_run(100, runs, candidate))

    def test_another_issue_worker_run_cannot_authorize_replacement(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        candidate = pr()
        candidate["headRefName"] = "opencode/issue100-7777"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[candidate],
                runs=[
                    run("CI"),
                    run(contract.CONTRACT_WORKFLOW_NAME),
                    worker_run(issue=999, run_id=7777),
                ],
                now=NOW,
            )

    def test_bound_worker_run_is_retrievable_by_id_outside_the_snapshot(self):
        """Completion must fetch the branch-bound run instead of trusting a page.

        The bounded global inventory can omit a worker run that finished before
        the implementation commit existed, where the exact-HEAD lookup cannot
        find it either.
        """
        snapshot = [run("CI"), run(contract.CONTRACT_WORKFLOW_NAME)]
        problem = canary.validate_completed_canary(pr(), snapshot, 100)
        self.assertIn("worker-evidence-absent", problem)
        merged = canary.merge_bound_worker_run(snapshot, worker_run())
        self.assertIsNone(
            canary.validate_completed_canary(pr(), merged, 100, publication())
        )
        self.assertEqual(
            canary.completion_worker_run(100, merged, pr())["databaseId"],
            WORKER_RUN_ID,
        )

    def test_successful_noop_worker_run_is_not_publication_evidence(self):
        """A run that published nothing cannot authorize completion.

        The worker exits successfully when it has nothing to publish, so a
        branch-embedded run id plus a matching issue title are not enough: the
        run must also carry the run-scoped publication receipt it uploads only
        after creating or updating the task PR.
        """
        self.assertIn(
            "worker publication receipt is absent",
            canary.validate_completed_canary(pr(), exact_head_runs(), 100),
        )
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "worker publication receipt is absent"
        ):
            canary.resolve_canary(
                [issue], pull_requests=[pr()], runs=exact_head_runs(), now=NOW
            )

    def test_publication_receipt_must_bind_the_exact_run_and_pr(self):
        cases = [
            (publication(schema_version=2), "unsupported schema"),
            (publication(repo="someone/else"), "another repository"),
            (publication(issue=999), "another issue"),
            (publication(run_id=7777), "another run"),
            (publication(branch="opencode/issue100-7777"), "another branch"),
            (publication(pr_number=42), "another pull request"),
            (publication(head="deadbeef"), "no full published commit"),
            (publication(head=OTHER_HEAD), "published another commit"),
        ]
        for receipt, reason in cases:
            with self.subTest(reason=reason):
                problem = canary.validate_completed_canary(
                    pr(), exact_head_runs(), 100, receipt
                )
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)

    def test_trusted_repair_commits_after_publication_pass(self):
        published = "e" * 40
        receipt = publication(head=published)
        commits = [
            pr_commit(published, message="fix: implement issue #100"),
            pr_commit(
                OTHER_HEAD,
                message="fix: address CodeRabbit findings",
                statuses=[repair_status(published, OTHER_HEAD)],
            ),
            pr_commit(
                WORKER_HEAD,
                author="kodmial",
                committer="web-flow",
                parents=2,
                verified=True,
                message="Merge branch 'main' into opencode/issue100-9001",
            ),
            pr_commit(
                HEAD,
                message="fix: repair blocking workflow",
                statuses=[repair_status(WORKER_HEAD, HEAD)],
            ),
        ]
        self.assertIsNone(
            canary.validate_completed_canary(
                pr(), exact_head_runs(), 100, receipt, commits
            )
        )

    def test_bot_identity_without_repair_status_fails(self):
        published = "e" * 40
        receipt = publication(head=published)
        commits = [
            pr_commit(published, message="fix: implement issue #100"),
            pr_commit(
                OTHER_HEAD,
                author="github-actions[bot]",
                committer="github-actions[bot]",
                message="fix: manually spoofed as automation",
            ),
            pr_commit(HEAD),
        ]
        problem = canary.validate_completed_canary(
            pr(), exact_head_runs(), 100, receipt, commits
        )
        self.assertIn("unauthenticated commit", problem)

    def test_repair_status_must_bind_the_exact_base_and_head(self):
        published = "e" * 40
        receipt = publication(head=published)
        cases = [
            repair_status(published, HEAD),
            repair_status(OTHER_HEAD, HEAD, pr_number=999),
            repair_status(OTHER_HEAD, HEAD, state="pending"),
            repair_status(OTHER_HEAD, HEAD, context="some/other-check"),
            repair_status(OTHER_HEAD, HEAD, creator="unrelated-collaborator"),
            repair_status(OTHER_HEAD, HEAD, target=""),
            repair_status(OTHER_HEAD, HEAD, publication_payload=None),
            repair_status(
                OTHER_HEAD,
                HEAD,
                publication_payload=repair_publication_receipt(
                    OTHER_HEAD, HEAD, run_id=9002
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                publication_payload=repair_publication_receipt(
                    OTHER_HEAD, OTHER_HEAD
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                publication_payload=repair_publication_receipt(
                    OTHER_HEAD, HEAD, branch="opencode/issue100-7777"
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                publication_payload=repair_publication_receipt(
                    OTHER_HEAD, HEAD, repo="someone/else"
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                publication_payload=repair_publication_receipt(
                    OTHER_HEAD, HEAD, schema_version=2
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                target="https://github.com/someone/else/actions/runs/9001",
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                target="https://example.com/{}/actions/runs/9001".format(
                    canary.CANARY_OWNER_REPO
                ),
            ),
            repair_status(
                OTHER_HEAD,
                HEAD,
                target="https://github.com/{}/actions/runs/9001?foo".format(
                    canary.CANARY_OWNER_REPO
                ),
            ),
        ]
        for repair in cases:
            with self.subTest(repair=repair):
                commits = [
                    pr_commit(published, message="fix: implement issue #100"),
                    pr_commit(
                        OTHER_HEAD,
                        message="fix: address CodeRabbit findings",
                        statuses=[repair_status(published, OTHER_HEAD)],
                    ),
                    pr_commit(
                        HEAD,
                        message="fix: repair blocking workflow",
                        statuses=[repair],
                    ),
                ]
                problem = canary.validate_completed_canary(
                    pr(), exact_head_runs(), 100, receipt, commits
                )
                self.assertIn("unauthenticated commit", problem)

    def test_web_editor_commit_is_not_a_main_sync_merge(self):
        published = "e" * 40
        receipt = publication(head=published)
        commits = [
            pr_commit(published, message="fix: implement issue #100"),
            pr_commit(
                OTHER_HEAD,
                author="kodmial",
                committer="web-flow",
                parents=1,
                verified=True,
                message="edit from the GitHub web editor",
            ),
            pr_commit(HEAD),
        ]
        problem = canary.validate_completed_canary(
            pr(), exact_head_runs(), 100, receipt, commits
        )
        self.assertIn("unauthenticated commit", problem)

    def test_untrusted_commit_after_publication_fails(self):
        published = "e" * 40
        receipt = publication(head=published)
        commits = [
            pr_commit(published, message="fix: implement issue #100"),
            pr_commit(OTHER_HEAD, committer="kodmial", author="kodmial"),
            pr_commit(HEAD),
        ]
        problem = canary.validate_completed_canary(
            pr(), exact_head_runs(), 100, receipt, commits
        )
        self.assertIn("unauthenticated commit", problem)

    def test_publication_commit_must_be_in_pr_history(self):
        receipt = publication(head=OTHER_HEAD)
        commits = [
            pr_commit("e" * 40),
            pr_commit(HEAD),
        ]
        problem = canary.validate_completed_canary(
            pr(), exact_head_runs(), 100, receipt, commits
        )
        self.assertIn("outside the final PR history", problem)

    def test_publication_repair_without_commit_history_fails(self):
        receipt = publication(head=OTHER_HEAD)
        problem = canary.validate_completed_canary(
            pr(), exact_head_runs(), 100, receipt
        )
        self.assertIn("without a PR commit history", problem)

    def test_closed_canary_completion_accepts_trusted_repair_history(self):
        issue = canary_issue(
            state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        published = "e" * 40
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[pr()],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(head=published),
            pull_request_commits=[
                pr_commit(published),
                pr_commit(
                    HEAD, statuses=[repair_status(published, HEAD)]
                ),
            ],
        )
        self.assertTrue(decision.completed)
        self.assertEqual(decision.reason, "completed-canary")

    def test_bound_worker_run_fetch_replaces_the_snapshot_record(self):
        snapshot = [
            run("CI"),
            run(contract.CONTRACT_WORKFLOW_NAME),
            worker_run(status="in_progress", conclusion=None),
        ]
        merged = canary.merge_bound_worker_run(snapshot, worker_run())
        self.assertEqual(
            [
                item
                for item in merged
                if item.get("databaseId") == WORKER_RUN_ID
            ],
            [worker_run()],
        )
        self.assertIsNone(
            canary.validate_completed_canary(pr(), merged, 100, publication())
        )

    def test_closed_canary_without_worker_evidence_fails_closed(self):
        issue = canary_issue(
            state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "worker-evidence-absent"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[pr()],
                runs=[run("CI"), run(contract.CONTRACT_WORKFLOW_NAME)],
                now=NOW,
            )

    def test_missing_merged_pr_fails(self):
        self.assertIn(
            "no merged implementation PR",
            canary.validate_completed_canary(None, [], 100),
        )

    def test_unmerged_pr_fails(self):
        candidate = pr(merged_at="")
        self.assertIn(
            "not merged",
            completed(candidate),
        )

    def test_missing_full_head_fails(self):
        candidate = pr(head="a" * 7)
        self.assertIn(
            "full headRefOid",
            completed(candidate),
        )

    def test_missing_full_merge_commit_fails(self):
        candidate = pr()
        candidate["mergeCommit"] = {"oid": "b" * 7}
        self.assertIn(
            "full merge commit",
            completed(candidate),
        )

    def test_ci_evidence_at_another_head_fails(self):
        candidate = pr()
        runs = [run("CI", head=OTHER_HEAD), run(contract.CONTRACT_WORKFLOW_NAME)]
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("stale", problem)

    def test_missing_contract_evidence_fails(self):
        candidate = pr()
        problem = canary.validate_completed_canary(candidate, [run("CI")], 100)
        self.assertIn("contract-evidence-absent", problem)

    def test_failed_ci_evidence_fails(self):
        candidate = pr()
        runs = [
            run("CI", conclusion="failure"),
            run(contract.CONTRACT_WORKFLOW_NAME),
        ]
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("regression-evidence-completed-failure", problem)

    def test_unrelated_mention_is_not_completion_evidence(self):
        candidate = pr(body="Related to #100 but this PR does not close it.")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_unrelated_pr_cannot_satisfy_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(body="See #100", updated=10)
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_list_item_block_quote_example_cannot_replace_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(body="- >     Closes #100\n", updated=10)
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_alternating_list_and_quote_example_cannot_replace_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(body="- > -     Closes #100\n", updated=10)
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_list_item_unclosed_pre_cannot_replace_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(body="- <pre>\n  Closes #100\n", updated=10)
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_lifecycle_branch_attributes_completion_without_keyword(self):
        candidate = pr(body="Automated implementation for #100.")
        self.assertIsNone(
            completed(candidate)
        )

    def test_closing_keyword_attributes_pr_without_completing_it(self):
        candidate = pr(body="Closes #100")
        candidate["headRefName"] = "feature/noncanonical"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("no worker run binding", problem)

    def test_manual_pr_with_unrelated_worker_run_is_not_completion_evidence(self):
        candidate = pr(body="Closes #100")
        candidate["headRefName"] = "feature/manual"
        runs = exact_head_runs()  # worker title names issue #100
        problem = canary.validate_completed_canary(candidate, runs, 100)
        self.assertIn("no worker run binding", problem)
        self.assertIsNone(canary.completion_worker_run(100, runs, candidate))

    def test_manual_pr_with_unrelated_worker_run_cannot_authorize_replacement(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        candidate = pr(body="Closes #100")
        candidate["headRefName"] = "feature/manual"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[candidate],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_bare_closing_number_is_not_completion_evidence(self):
        candidate = pr(body="Closes 100. Related to #100.")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_cross_repository_closing_reference_is_not_completion_evidence(self):
        candidate = pr(body="Closes other/repo#100. Related to #100.")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_inline_code_closing_example_is_not_completion_evidence(self):
        candidate = pr(
            body=(
                "This change documents the syntax `Closes #100`; "
                "it does not implement the canary."
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_link_title_closing_example_is_not_completion_evidence(self):
        bodies = [
            '[Documentation](https://example.com "Closes #100")',
            "[Documentation](https://example.com 'Closes #100')",
            "[Documentation](https://example.com (Closes #100))",
            '[Documentation](https://example.com\n "Closes #100")',
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_reference_link_title_closing_example_is_not_completion_evidence(self):
        bodies = [
            '[Documentation][doc]\n\n[doc]: https://example.com "Closes #100"',
            "[Documentation][doc]\n\n[doc]: https://example.com 'Closes #100'",
            "[Documentation][doc]\n\n[doc]: https://example.com (Closes #100)",
            '[Documentation][doc]\n\n[doc]: https://example.com\n "Closes #100"',
            '[Documentation][doc]\n\n[doc]: https://example.com "Closes\n#100"',
            '[doc]: https://example.com "Closes #100"\n\n[Documentation][doc]',
            '- [Documentation][doc]\n\n  [doc]: https://example.com "Closes #100"',
            ' [doc]: https://example.com "Closes #100"\n\n[Documentation][doc]',
            '  [doc]: https://example.com "Closes #100"\n\n[Documentation][doc]',
            '   [doc]: https://example.com "Closes #100"\n\n[Documentation][doc]',
            '> [Documentation][doc]\n>\n>  [doc]: https://example.com "Closes #100"',
            '- [Documentation][doc]\n\n   [doc]: https://example.com "Closes #100"',
            '- [Documentation][doc]\n\n    [doc]: https://example.com "Closes #100"',
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_reference_link_title_with_trailing_text_stays_visible(self):
        bodies = [
            '[doc]: https://example.com\n"Closes #100" trailing text\n',
            "[doc]: https://example.com\n'Closes #100' trailing text\n",
            "[doc]: https://example.com\n(Closes #100) trailing text\n",
            '[doc]: https://example.com\n"Closes\n#100" trailing text\n',
            '[doc]: https://example.com\n    "Closes #100" trailing text\n',
            '[doc]: https://example.com\n\t"Closes #100" trailing text\n',
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_reference_link_title_trailing_text_keeps_branch_attribution(self):
        candidate = pr(
            body=(
                '[doc]: https://example.com\n'
                '"Closes #100" trailing text\n'
            )
        )
        candidate["headRefName"] = "opencode/issue100-{}".format(WORKER_RUN_ID)
        self.assertIsNone(
            completed(candidate)
        )

    def test_indented_code_after_reference_definition_stays_inert(self):
        candidate = pr(body='[doc]: https://example.com\n\n    Closes #100\n')
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_reference_link_label_closing_reference_is_completion_evidence(self):
        candidate = pr(
            body=(
                "[Closes #100][doc]\n\n"
                '[doc]: https://example.com "Documentation"'
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_reference_link_title(self):
        candidate = pr(
            body=(
                '[Documentation][doc]\n\n'
                '[doc]: https://example.com "Closes #100"\n\n'
                "Closes #100\n"
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_reference_definition_after_paragraph_stays_visible(self):
        candidate = pr(
            body=(
                "See the notes below\n"
                '[doc]: https://example.com "Closes #100"\n'
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_link_title_example_cannot_replace_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(
            body='[Documentation](https://example.com "Closes #100")',
            updated=10,
        )
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_link_label_closing_reference_is_completion_evidence(self):
        candidate = pr(
            body='[Closes #100](https://example.com "Documentation")'
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_link_title(self):
        candidate = pr(
            body=(
                '[Documentation](https://example.com "Closes #100")\n\n'
                "Closes #100\n"
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_fenced_code_closing_example_is_not_completion_evidence(self):
        candidate = pr(
            body="Example only:\n\n```text\nCloses #100\n```\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_backtick_in_backtick_fence_info_string_is_not_a_fence(self):
        bodies = [
            "```bad`text\nCloses #100\n",
            "```bad`text\nCloses #100\n```\n",
            "````bad`text\nCloses #100\n````\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_tilde_fence_info_string_may_contain_a_backtick(self):
        candidate = pr(body="~~~bad`text\nCloses #100\n~~~\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_fenced_code_closing_example_is_not_completion_evidence(self):
        bodies = [
            "- ```text\n  Closes #100\n",
            "- ```text\n  Closes #100\n  ```\n",
            "- - ```text\n    Closes #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_list_item_fenced_code_ends_at_the_item_boundary(self):
        candidate = pr(
            body="- ```text\n  Closes #100\n- unrelated\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_list_item_fenced_code(self):
        bodies = [
            "- ```text\n  Closes #100\n  ```\n\nCloses #100\n",
            "- ```text\n  Closes #100\n  ```\n  Closes #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_fenced_code_does_not_shadow_lifecycle_branch(self):
        for body in ("- ```text\n  Closes #100\n", "- - ```text\n    Closes #100\n"):
            with self.subTest(body=body):
                candidate = pr(body=body)
                self.assertIsNone(completed(candidate))

    def test_nested_list_and_quote_fenced_example_is_not_evidence(self):
        bodies = [
            "- > - ```text\n  >   Closes #100\n",
            "- > - > - ```text\n  >   >   Closes #100\n",
            "- > - ```text\n  >   Closes #100\n  >   ```\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_nested_list_and_quote_fenced_example_keeps_branch_attribution(self):
        for body in (
            "- > - ```text\n  >   Closes #100\n",
            "- > - > - ```text\n  >   >   Closes #100\n",
        ):
            with self.subTest(body=body):
                candidate = pr(body=body)
                self.assertIsNone(completed(candidate))

    def test_list_item_quote_fence_ends_when_the_quote_ends(self):
        bodies = [
            "- > ```text\n  > example\n\nCloses #100\n",
            "- > ```text\n  > example\n  Closes #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_indented_code_closing_example_is_not_completion_evidence(self):
        candidate = pr(body="Example only:\n\n    Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_tab_indented_code_closing_example_is_not_completion_evidence(self):
        candidate = pr(body="Example only:\n\n\tCloses #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_indented_code_block_continues_across_blank_lines(self):
        candidate = pr(
            body=(
                "Example only:\n\n"
                "    first line\n\n"
                "    Closes #100\n\n"
                "After the block.\n"
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_indented_code_example_after_heading_is_not_completion_evidence(self):
        candidate = pr(body="# Example\n    Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_indented_code_example_is_not_completion_evidence(self):
        candidate = pr(body="> Example:\n>\n>     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_indented_code_continues_across_blank_lines(self):
        candidate = pr(
            body=(
                "> Example:\n>\n"
                ">     first line\n>\n"
                ">     Closes #100\n"
            )
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_nested_block_quote_indented_code_example_is_not_evidence(self):
        candidate = pr(body="> > Example:\n> >\n> >     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_tab_indented_code_example_is_not_evidence(self):
        candidate = pr(body="> Example:\n>\n>   \tCloses #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_prose_closing_reference_is_completion_evidence(self):
        candidate = pr(body="> Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_block_quote_indented_example_does_not_shadow_lifecycle_branch(self):
        candidate = pr(body="> Example:\n>\n>     Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_tab_indented_code_example_after_thematic_break_is_not_evidence(self):
        candidate = pr(body="Example only:\n\n* * *\n\tCloses #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_indented_code_example_is_not_completion_evidence(self):
        candidate = pr(body="-     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_empty_list_item_indented_code_is_not_completion_evidence(self):
        candidate = pr(body="-\n      Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_empty_list_item_with_trailing_whitespace_code_is_not_evidence(self):
        candidate = pr(body="- \t\n      Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_empty_ordered_list_item_indented_code_is_not_evidence(self):
        candidate = pr(body="1.\n       Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_empty_list_item_indented_prose_keeps_reference_operative(self):
        candidate = pr(body="-\n    Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_indented_code_block_continues_across_lines(self):
        candidate = pr(body="-     first line\n      Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_ordered_list_item_indented_code_is_not_completion_evidence(self):
        candidate = pr(body="10.     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_nested_list_item_indented_code_is_not_completion_evidence(self):
        candidate = pr(body="- foo\n    -     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_list_item_indented_code_is_not_evidence(self):
        candidate = pr(body="> -     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_block_quote_indented_code_is_not_evidence(self):
        candidate = pr(body="- >     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_nested_list_item_block_quote_indented_code_is_not_evidence(self):
        candidate = pr(body="- > >     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_block_quote_code_continues_across_blank_lines(self):
        candidate = pr(
            body="- > Example:\n  >\n  >     Closes #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_alternating_list_and_quote_containers_are_not_evidence(self):
        bodies = [
            "- > -     Closes #100\n",
            "- > - >     Closes #100\n",
            "> - > -     Closes #100\n",
            "- > > -     Closes #100\n",
            "- - > -     Closes #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_alternating_list_and_quote_code_continues_across_lines(self):
        candidate = pr(
            body="- > -     first line\n  >       Closes #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_alternating_list_and_quote_prose_is_completion_evidence(self):
        candidate = pr(body="- > - Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_alternating_list_and_quote_example_does_not_shadow_branch(self):
        candidate = pr(body="- > -     Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_tab_separated_alternating_containers_are_not_evidence(self):
        bodies = [
            "- \t> -     Closes #100\n",
            "-\t> -     Closes #100\n",
            "- >\n> >  \tCloses #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_tab_separated_alternating_containers_keep_prose_evidence(self):
        bodies = [
            "- > > \tCloses #100\n",
            "- > -\tCloses #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_leaving_a_list_item_still_evaluates_nested_quote_code(self):
        bodies = [
            "- item\n> - > -       Closes #100\n",
            "- item\n>     Closes #100\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_leaving_a_list_item_keeps_quote_prose_evidence(self):
        candidate = pr(body="- item\n> - Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_block_quote_prose_closing_reference_is_evidence(self):
        candidate = pr(body="- > Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_block_quote_indented_example_does_not_shadow_branch(self):
        candidate = pr(body="- >     Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_list_item_prose_closing_reference_is_completion_evidence(self):
        candidate = pr(body="- Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_code_continuation_boundary_keeps_prose_operative(self):
        candidate = pr(body="-     first line\n    Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_ordered_list_cannot_interrupt_paragraph_with_non_one_start(self):
        candidate = pr(body="See the notes below\n2.     Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_indented_example_does_not_shadow_lifecycle_branch(self):
        candidate = pr(body="-     Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_operative_closing_reference_after_heading_is_completion_evidence(self):
        candidate = pr(body="# Example\n\nCloses #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_paragraph_indented_continuation_keeps_operative_reference(self):
        candidate = pr(body="See the notes below\n    Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_trailing_inline_comment_does_not_open_indented_code_block(self):
        candidate = pr(
            body="See the notes below <!-- trailing note -->\n    Closes #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_indented_code_block(self):
        candidate = pr(
            body="Example only:\n\n    Closes #100\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_indented_example_does_not_shadow_lifecycle_branch_attribution(self):
        candidate = pr(body="Example only:\n\n    Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_html_comment_closing_example_is_not_completion_evidence(self):
        candidate = pr(body="<!-- Closes #100 -->")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_html_pre_closing_example_is_not_completion_evidence(self):
        candidate = pr(body="<pre>\nCloses #100\n</pre>\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_inline_html_pre_closing_example_is_not_completion_evidence(self):
        candidate = pr(body="The docs render <pre>Closes #100</pre> verbatim.\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_unclosed_block_html_pre_is_not_completion_evidence(self):
        candidate = pr(body="<PRE>\nCloses #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_inline_html_code_closing_example_is_not_completion_evidence(self):
        candidate = pr(
            body="The docs render <code>Closes #100</code> verbatim.\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_block_quote_unclosed_html_pre_is_not_completion_evidence(self):
        candidate = pr(body="> <pre>\n> Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_unclosed_html_pre_is_not_completion_evidence(self):
        candidate = pr(body="- <pre>\n  Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_unclosed_html_pre_is_inert_only_inside_the_item(self):
        candidate = pr(
            body="- <pre>\n  Closes #100\n- unrelated\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_unclosed_html_pre_keeps_branch_attribution(self):
        candidate = pr(body="- <pre>\n  Closes #100\n")
        self.assertIsNone(
            completed(candidate)
        )

    def test_task_list_item_unclosed_html_pre_is_not_completion_evidence(self):
        candidate = pr(body="- [ ] <pre>\n      Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_task_list_item_unclosed_html_pre_is_inert_only_inside_the_item(self):
        candidate = pr(
            body="- [ ] <pre>\n      Closes #100\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_list_item_unclosed_html_pre_keeps_quote_marker_inert(self):
        candidate = pr(body="- <pre>\n  > Closes #100\n")
        candidate["headRefName"] = "feature/unrelated"
        problem = canary.validate_completed_canary(
            candidate, exact_head_runs(), 100
        )
        self.assertIn("not attributed", problem)

    def test_list_item_unclosed_html_pre_keeps_quote_marker_inside_the_item(self):
        candidate = pr(
            body="- <pre>\n  > Closes #100\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_self_closing_list_item_html_pre_is_not_completion_evidence(self):
        for body in ("- <pre/>\n  Closes #100\n", "- <pre />\n  Closes #100\n"):
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_html_code_and_block_quote_pre_examples_keep_branch_attribution(self):
        for body in ("<code>Closes #100</code>\n", "> <pre>\n> Closes #100\n"):
            with self.subTest(body=body):
                candidate = pr(body=body)
                self.assertIsNone(completed(candidate))

    def test_operative_closing_reference_after_html_code_example(self):
        candidate = pr(
            body="<code>Closes #100</code>\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_block_quote_pre_example(self):
        candidate = pr(
            body="> <pre>\n> Closes #100\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_operative_closing_reference_after_html_pre_example(self):
        candidate = pr(
            body="<pre>\nCloses #100\n</pre>\n\nCloses #100\n"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_html_attribute_closing_example_is_not_completion_evidence(self):
        bodies = [
            '<span title="Closes #100">Documentation</span>',
            "<span title='Closes #100'>Documentation</span>",
            '<span data-note="Closes #100">Documentation</span>',
            '<a href="https://example.com" title="Closes #100">Documentation</a>',
            '<span title="first > second" data-note="Closes #100">Documentation</span>',
            '<img alt="Closes #100" />',
            '<span\ntitle="Closes #100">Documentation</span>',
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                problem = canary.validate_completed_canary(
                    candidate, exact_head_runs(), 100
                )
                self.assertIn("not attributed", problem)

    def test_html_text_closing_reference_is_completion_evidence(self):
        bodies = [
            "<span>Closes #100</span>",
            '<a href="https://example.com">Closes #100</a>',
        ]
        for body in bodies:
            with self.subTest(body=body):
                candidate = pr(body=body)
                candidate["headRefName"] = "feature/unrelated"
                self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_html_attribute_example_cannot_replace_closed_canary(self):
        issue = canary_issue(
            state="closed", created=8 * 24 * 60 * 60, closed=30
        )
        unrelated = pr(
            body='<span title="Closes #100">Documentation</span>',
            updated=10,
        )
        unrelated["headRefName"] = "feature/unrelated"
        with self.assertRaisesRegex(
            canary.CanaryBootstrapError, "not trustworthy"
        ):
            canary.resolve_canary(
                [issue],
                pull_requests=[unrelated],
                runs=exact_head_runs(),
                now=NOW,
            )

    def test_operative_closing_reference_survives_code_example(self):
        candidate = pr(
            body="The example `Closes #100` is inert.\n\nCloses #100"
        )
        candidate["headRefName"] = "feature/unrelated"
        self.assertTrue(canary._pr_attributes_issue(candidate, 100))

    def test_newer_unrelated_pr_does_not_shadow_attributed_pr(self):
        attributed = pr(number=41, body="Closes #100", updated=60)
        unrelated = pr(number=42, body="Mentions #100 only", updated=10)
        unrelated["headRefName"] = "feature/unrelated"
        selected = canary.select_implementation_pr(100, [unrelated, attributed])
        self.assertEqual(selected["number"], 41)

    def test_closed_canary_completion_uses_attributed_pr_not_newer_unrelated_pr(self):
        issue = canary_issue(
            state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        attributed = pr(number=41, body="Closes #100", updated=60)
        unrelated = pr(
            number=42,
            body="Related to #100 only",
            state="open",
            merged_at="",
            updated=10,
        )
        unrelated["headRefName"] = "feature/unrelated"
        decision = canary.resolve_canary(
            [issue],
            pull_requests=[unrelated, attributed],
            runs=exact_head_runs(),
            now=NOW,
            worker_publication=publication(),
        )
        self.assertEqual(decision.action, "observe")
        self.assertTrue(decision.completed)
        self.assertEqual(decision.issue_number, 100)

    def test_closed_canary_observation_uses_attributed_pr_not_newer_unrelated_pr(self):
        issue = canary_issue(
            state="closed", created=2 * 24 * 60 * 60, closed=30
        )
        attributed = pr(number=41, body="Closes #100", updated=60)
        unrelated = pr(
            number=42,
            body="Related to #100 only",
            state="open",
            merged_at="",
            updated=10,
        )
        unrelated["headRefName"] = "feature/unrelated"
        observation_pulls = canary.observation_pull_requests(
            100, issue["state"], [unrelated, attributed]
        )
        self.assertEqual([item["number"] for item in observation_pulls], [41])
        observed = observation.observe_canary(
            issue,
            pull_requests=observation_pulls,
            now=NOW,
            repository=canary.CANARY_OWNER_REPO,
        )
        self.assertTrue(observed.completed)

    def test_open_canary_observation_preserves_loose_liveness_inventory(self):
        current = pr(
            number=41,
            body="Closes #100",
            state="open",
            merged_at="",
            updated=60,
        )
        unrelated = pr(
            number=42,
            body="Related to #100 only",
            state="open",
            merged_at="",
            updated=10,
        )
        unrelated["headRefName"] = "feature/unrelated"
        observation_pulls = canary.observation_pull_requests(
            100, "open", [unrelated, current]
        )
        self.assertEqual([item["number"] for item in observation_pulls], [42, 41])


class ExactHeadRunsTest(unittest.TestCase):
    def test_replaces_only_the_target_head_snapshot_records(self):
        snapshot = [
            run("CI", head=HEAD, conclusion="success"),
            run("CI", head=OTHER_HEAD, conclusion="success"),
            {
                "name": "CI",
                "head_sha": " {}".format(HEAD.upper()),
                "status": "completed",
                "conclusion": "success",
            },
        ]
        fresh = [run("CI", conclusion="failure")]
        merged = canary.merge_exact_head_runs(snapshot, HEAD, fresh)
        self.assertEqual(merged[0]["headSha"], OTHER_HEAD)
        self.assertEqual(merged[-1]["conclusion"], "failure")
        self.assertEqual(len(merged), 2)

    def test_no_target_head_only_appends_fresh_evidence(self):
        snapshot = [run("CI", head=OTHER_HEAD)]
        fresh = [run("CI", conclusion="failure")]
        merged = canary.merge_exact_head_runs(snapshot, "", fresh)
        self.assertEqual(merged, snapshot + fresh)

    def test_exact_head_refresh_preserves_worker_completion_evidence(self):
        merged = canary.merge_exact_head_runs(
            exact_head_runs(),
            HEAD,
            [run("CI"), run(contract.CONTRACT_WORKFLOW_NAME)],
        )
        self.assertIsNone(completed(pr(), runs=merged))
        self.assertEqual(
            canary.completion_worker_run(100, merged, pr())["databaseId"],
            WORKER_RUN_ID,
        )

    def test_fresh_exact_head_failure_masks_the_snapshot_success(self):
        """The completion gate must consume the fresh exact-HEAD evidence.

        A bounded global snapshot can still hold an older success for the same
        head after a re-run fails. ``evaluate_regression_gate`` reads the first
        matching record, so merging must replace those snapshot records instead
        of appending after them.
        """
        snapshot = exact_head_runs()
        self.assertIsNone(completed(pr(), runs=snapshot))
        fresh = [
            run("CI", conclusion="failure"),
            run(contract.CONTRACT_WORKFLOW_NAME, head=HEAD),
        ]
        merged = canary.merge_exact_head_runs(snapshot, HEAD, fresh)
        problem = canary.validate_completed_canary(pr(), merged, 100)
        self.assertIn("regression-evidence-completed-failure", problem)

    def test_fresh_exact_head_pending_masks_the_snapshot_success(self):
        """A still-running exact-HEAD run must shadow a stale success too."""
        snapshot = exact_head_runs()
        self.assertIsNone(completed(pr(), runs=snapshot))
        fresh = [
            run("CI", status="in_progress", conclusion=None),
            run(contract.CONTRACT_WORKFLOW_NAME, head=HEAD),
        ]
        merged = canary.merge_exact_head_runs(snapshot, HEAD, fresh)
        self.assertEqual(
            [item for item in merged if item["name"] == "CI"], [fresh[0]]
        )
        problem = canary.validate_completed_canary(pr(), merged, 100)
        self.assertIn("regression-evidence-in_progress-pending", problem)


class CanaryIssueBodyTest(unittest.TestCase):
    def test_body_is_marked_bounded_and_harmless(self):
        title, body = canary.build_canary_issue(
            nonce="run-123-1", run_id="123", run_attempt="1"
        )
        self.assertEqual(title, canary.CANARY_TITLE)
        self.assertTrue(body.startswith(marker()))
        self.assertIn(canary.CANARY_FIXTURE_PATH, body)
        self.assertIn("continuum-canary nonce=run-123-1", body)
        self.assertIn("Definition of Done", body)
        self.assertIn("guarded merge path", body)
        self.assertNotIn(".github/workflows", body)
        self.assertNotIn("consumer repository scripts", body)
        self.assertNotIn("ghp_", body)
        self.assertNotIn("TAP_PAT", body)

    def test_body_rejects_unsafe_nonce(self):
        for nonce in ("line\nbreak", "`tick`", "", "x" * 121):
            with self.subTest(nonce=nonce):
                with self.assertRaises(canary.CanaryBootstrapError):
                    canary.build_canary_issue(nonce=nonce)

    def test_body_cannot_be_built_for_another_repository(self):
        with self.assertRaises(canary.CanaryBootstrapError):
            canary.build_canary_issue(nonce="n", repo="someone/else")

    def test_marker_helper_matches_builder(self):
        self.assertTrue(marker().startswith(canary.CANARY_MARKER_PREFIX))


class WorkflowContractTest(unittest.TestCase):
    def test_workflow_bootstraps_without_operator_configuration(self):
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("CONTINUUM_CANARY_ISSUE", workflow)
        self.assertIn("contract_canary", workflow)
        self.assertIn("select_canary_issue", workflow)
        self.assertIn("resolve_canary", workflow)
        self.assertIn("gh issue create", workflow)
        self.assertIn('--label "' + canary.CANARY_READY_LABEL + '"', workflow)
        self.assertIn("gh issue view \"$CANARY_ISSUE\"", workflow)
        self.assertIn("validate_completed_canary", workflow)
        self.assertIn("--paginate", workflow)
        self.assertIn("PUBLISH_STATUS", workflow)
        self.assertIn("continuum-canary-recovery", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)

    def test_both_exact_head_fetches_replace_stale_snapshot_evidence(self):
        """Fresh exact-HEAD runs must not be shadowed by a snapshot record.

        ``evaluate_regression_gate`` consumes the first record for the target
        head, so both the resolver and observation paths replace that head's
        snapshot records instead of appending after them.
        """
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("canary.merge_exact_head_runs("), 2)
        self.assertNotIn("runs = runs + json.loads(proc.stdout)", workflow)
        self.assertEqual(workflow.count("select_implementation_pr("), 2)

    def test_workflow_observes_closed_canary_with_attributed_pr_only(self):
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("canary.observation_pull_requests(", workflow)
        self.assertIn("pull_requests=observation_pulls", workflow)

    def test_workflow_records_completion_worker_run_id(self):
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("canary.completion_worker_run(", workflow)
        self.assertIn("run=worker_run_id", workflow)
        self.assertIn("OpenCode worker evidence", workflow)
        self.assertEqual(workflow.count("canary.completion_worker_run_id("), 2)
        self.assertEqual(workflow.count("canary.merge_bound_worker_run("), 2)
        self.assertIn('"run", "view"', workflow)
        self.assertEqual(workflow.count("worker_publication(bound)"), 2)
        self.assertEqual(workflow.count("worker_publication=publication"), 1)
        self.assertEqual(
            workflow.count("latest, runs, issue_number, publication"), 1
        )
        self.assertEqual(
            workflow.count("CANARY_PUBLICATION_ARTIFACT_PREFIX"), 2
        )

    def test_workflow_reads_pr_commits_for_trusted_continuity(self):
        """A worker-published commit may be followed by trusted repairs."""
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("def implementation_commits("), 2)
        self.assertEqual(workflow.count("implementation_commits("), 4)
        self.assertEqual(workflow.count("pulls/{}/commits?per_page=100"), 2)
        self.assertEqual(workflow.count("--slurp"), 4)
        self.assertEqual(workflow.count("could not read commits"), 2)
        self.assertEqual(workflow.count("pull_request_commits=commits"), 2)

    def test_workflow_reads_authenticated_repair_statuses(self):
        """Repair commits need API-written statuses, not forgeable git fields."""
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("commits/{}/statuses?per_page=100"), 2)
        self.assertEqual(workflow.count("could not read commit statuses"), 2)
        self.assertEqual(
            workflow.count('commit["statuses"] = parsed_statuses'), 2
        )

    def test_repair_paths_publish_authenticated_status(self):
        workflow = (
            ROOT / ".github/workflows/continuum-opencode.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("name: Record authenticated repair publication", workflow)
        self.assertIn("repos/$GITHUB_REPOSITORY/statuses/$head", workflow)
        self.assertIn("context=continuum/repair-publication", workflow)
        self.assertIn("state=success", workflow)
        self.assertIn("CREATED_REPAIR_STATUS", workflow)
        for step_id in ("coderabbit_repair", "resolve_conflict", "ci_fix"):
            self.assertIn("id: {}".format(step_id), workflow)
            self.assertIn(
                "steps.{}.outputs.changed".format(step_id), workflow
            )
            self.assertIn(
                "steps.{}.outputs.base_sha".format(step_id), workflow
            )
            self.assertIn(
                "steps.{}.outputs.head_sha".format(step_id), workflow
            )
        self.assertIn("pr=${PR_NUMBER} base=${base:0:12} head=${head:0:12}", workflow)
        self.assertIn(
            "name: Upload authenticated repair publication receipt", workflow
        )
        self.assertIn(
            "continuum-repair-publication-${{ github.run_attempt }}", workflow
        )
        self.assertIn("base_sha: $base", workflow)
        self.assertIn("head_sha: $head", workflow)

    def test_ordinary_coderabbit_repair_always_reports_publication(self):
        """A review-only repair must attest its pushed commit without a batch."""
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/continuum-opencode.yml").read_text(
                encoding="utf-8"
            )
        )
        steps = workflow["jobs"]["opencode"]["steps"]
        script = next(
            step["run"] for step in steps if step.get("id") == "coderabbit_repair"
        )
        self.assertIn(
            'if [[ "$(git rev-list --count "${REPAIR_BASE_SHA}"..HEAD)" -gt 0 ]]; then',
            script,
        )
        self.assertNotIn(
            'if [[ -n "${UNRESOLVED_ITEMS_JSON:-}" ]]; then\n'
            '            if [[ "$(git rev-list --count "${REPAIR_BASE_SHA}"..HEAD)"',
            script,
        )

    def test_workflow_binds_repair_status_to_run_receipt(self):
        """The canary reads the receipt named by each repair status run."""
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("def repair_publication(run_id):"), 2)
        self.assertEqual(
            workflow.count("canary.repair_status_run_id(status, repo)"), 2
        )
        self.assertEqual(
            workflow.count("status[\"publication\"] = repair_publication(run_id)"),
            2,
        )
        self.assertEqual(
            workflow.count(
                "canary.CANARY_REPAIR_PUBLICATION_ARTIFACT_PREFIX"
            ),
            2,
        )

    def test_workflow_fails_closed_on_unreadable_worker_evidence(self):
        """A failed direct read must never fall back to the bounded snapshot.

        The snapshot can still hold an older successful worker record after a
        re-run or a deletion, so both completion paths abort instead of
        accepting stale evidence, and a missing or unreadable publication
        receipt is never treated as publication.
        """
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(workflow.count("could not read bound worker run"), 2)
        self.assertEqual(workflow.count("if proc.returncode == 0"), 0)
        self.assertEqual(
            workflow.count("could not read publication artifacts"), 2
        )
        self.assertEqual(
            workflow.count("could not download publication receipt"), 2
        )
        self.assertEqual(
            workflow.count("is unreadable"), 2
        )
        self.assertIn("worker publication receipt is absent", (
            ROOT / "src/continuum/contract_canary.py"
        ).read_text(encoding="utf-8"))

    def test_worker_uploads_the_run_scoped_publication_receipt(self):
        """Only the publishing run can attach the receipt it is judged by."""
        workflow = (
            ROOT / ".github/workflows/continuum-opencode.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("name: Upload OpenCode publication receipt", workflow)
        self.assertIn("actions/upload-artifact@", workflow)
        self.assertIn("continuum-task-publication-${{ github.run_attempt }}", workflow)
        self.assertIn("if-no-files-found: ignore", workflow)
        self.assertIn("branch: $branch", workflow)
        self.assertIn("pr_number: ($pr | tonumber)", workflow)
        self.assertIn("head_sha: $head", workflow)
        # Both publish paths (normal and branch-recovery) must attest.
        self.assertEqual(workflow.count("PUBLICATION_DIR="), 2)
        self.assertEqual(workflow.count("schema_version: 1, repo: $repo"), 2)
        self.assertGreaterEqual(workflow.count("publication.json"), 3)

    def test_workflow_preserves_review_decision_for_the_merge_stage(self):
        """The REST pulls inventory must still carry the review decision.

        ``observe_canary`` distinguishes ``review`` from ``merge`` with the
        PR-level ``reviewDecision`` field, which the REST pulls endpoint does
        not return. The workflow therefore enriches the bounded open-PR set
        instead of silently reporting an approved, merge-waiting canary as
        ``review``.
        """
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '--json number,reviewDecision', workflow
        )
        self.assertIn('pull["reviewDecision"] = review_decisions.get', workflow)

    def test_workflow_does_not_leak_credentials_or_direct_write_main(self):
        workflow = (
            ROOT / ".github/workflows/continuum-contract-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("set -x", workflow)
        self.assertNotIn('echo "$GH_TOKEN"', workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("pulls.merge", workflow)
        entry = (
            ROOT / ".github/workflows/contract-qualification-canary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("concurrency:", entry)
        self.assertIn("cancel-in-progress: false", entry)

    def test_every_shell_step_parses_and_python_heredocs_compile(self):
        """The default path is the embedded Python, so it must parse.

        A heredoc terminator or body indented with the surrounding `case`
        branch survives YAML parsing but fails `bash -n` (or raises
        IndentationError) only when the workflow runs. This regression keeps
        the bootstrap path executable instead of merely present.
        """
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/continuum-contract-canary.yml").read_text(
                encoding="utf-8"
            )
        )
        saw_python = False
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                script = step.get("run")
                if not script:
                    continue
                with self.subTest(step=step.get("name")):
                    parsed = subprocess.run(
                        ["bash", "-n"],
                        input=script,
                        text=True,
                        capture_output=True,
                    )
                    self.assertEqual(parsed.returncode, 0, parsed.stderr)
                    for body in _python_heredocs(script):
                        saw_python = True
                        compile(body, "<workflow-heredoc>", "exec")
        self.assertTrue(saw_python, "the canary workflow must embed Python probes")

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_publish_status_never_turns_waiting_evidence_green(self):
        """Run the shipped publish script and assert the commit status truth.

        A created issue or an in-flight lifecycle is ``pending``; only a
        completed issue closed through a verified merged PR is ``success``.
        Anything else, including a failed step, is a failure.
        """
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/continuum-contract-canary.yml").read_text(
                encoding="utf-8"
            )
        )
        publish = workflow["jobs"]["canary"]["steps"][-1]
        script = publish["with"]["script"]
        harness = (
            "(async () => {\n"
            "  const calls = {statuses: [], failures: [], notices: []};\n"
            "  global.github = {rest: {repos: {createCommitStatus: async (args) => {\n"
            "    calls.statuses.push(args);\n"
            "  }}}};\n"
            "  global.context = {repo: {owner: 'kodmial', repo: 'continuum'},\n"
            "    serverUrl: 'https://github.com', runId: 123};\n"
            "  global.core = {setFailed: (message) => calls.failures.push(String(message)),\n"
            "    notice: (message) => calls.notices.push(String(message))};\n"
            + script
            + "\n  console.log(JSON.stringify(calls));\n"
            "})().catch((error) => {console.error(error); process.exit(1);});\n"
        )
        cases = [
            ("success", "completed", "true", "success", 0, 0),
            ("success", "progress", "true", "pending", 0, 1),
            ("success", "recovering", "true", "pending", 0, 1),
            ("success", "bootstrapped", "true", "pending", 0, 1),
            ("success", "", "true", "failure", 1, 0),
            ("failure", "completed", "true", "failure", 1, 0),
            ("failure", "progress", "true", "failure", 1, 0),
            ("success", "completed", "false", None, 0, 0),
            ("failure", "completed", "false", None, 1, 0),
        ]
        for outcome, evidence, publish_status, state, failures, notices in cases:
            with self.subTest(
                outcome=outcome, evidence=evidence, publish=publish_status
            ):
                environment = dict(os.environ)
                environment.update(
                    {
                        "CANARY_OUTCOME": outcome,
                        "CANARY_EVIDENCE": evidence,
                        "CANARY_DETAIL": "observed detail",
                        "PUBLISH_STATUS": publish_status,
                        "TARGET_SHA": HEAD,
                        "STATUS_CONTEXT": "continuum/contract-qualification-canary",
                    }
                )
                completed = subprocess.run(
                    ["node", "-"],
                    input=harness,
                    text=True,
                    capture_output=True,
                    env=environment,
                    timeout=30,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stderr or completed.stdout
                )
                calls = json.loads(completed.stdout)
                self.assertEqual(len(calls["failures"]), failures)
                self.assertEqual(len(calls["notices"]), notices)
                self.assertEqual(len(calls["statuses"]), 0 if state is None else 1)
                if state is not None:
                    self.assertEqual(calls["statuses"][0]["state"], state)


def _python_heredocs(script):
    """Yield every ``python3 - <<'PY'`` body exactly as bash receives it."""

    lines = script.splitlines()
    body = None
    for line in lines:
        if body is None:
            if re.search(r"python3\s+-\s+<<'PY'", line):
                body = []
        elif line == "PY":
            yield "\n".join(body)
            body = None
        else:
            body.append(line)
    if body is not None:
        raise AssertionError("unterminated python heredoc in workflow step")


if __name__ == "__main__":
    unittest.main()
