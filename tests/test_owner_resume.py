"""Owner-armed predecessor-completion-gated resume (kodmial/continuum#382).

The tests exercise the production decision module and the existing scheduler
reconciliation model.  Workflow string assertions pin the embedded JavaScript
to the same contract so the shipped scheduler cannot drift into a fake helper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from continuum.owner_resume import (
    NOOP,
    RESUME,
    WAIT,
    decide_owner_resume,
    evaluate_predecessor_completion,
    resume_baseline,
    scan_resume_marker,
)
from continuum.scheduler_reconcile import (
    CommentRecord,
    IssueState,
    SchedulerConfig,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
OWNER = "kodmial"
REPOSITORY = "kodmial/continuum"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGE_A = "c" * 40
MERGE_B = "d" * 40

PAUSE = "automation:paused"
READY = "automation:ready"


def marker(predecessor: int, *, author: str = OWNER, cid: int = 1,
           at: datetime = NOW) -> dict:
    return {
        "id": cid,
        "body": "<!-- continuum-resume-after:v1 predecessor=%d -->" % predecessor,
        "user": {"login": author},
        "created_at": at.isoformat().replace("+00:00", "Z"),
    }


def comment(body: str, *, author: str = OWNER, cid: int = 100,
            at: datetime = NOW, association: str = "OWNER") -> dict:
    return {
        "id": cid,
        "body": body,
        "user": {"login": author},
        "author_association": association,
        "created_at": at.isoformat().replace("+00:00", "Z"),
    }


def issue(number: int, *, state: str = "open", reason: str = "",
          labels=None) -> dict:
    return {
        "number": number,
        "state": state,
        "state_reason": reason,
        "labels": list(labels or []),
    }


def pr(number: int, predecessor: int, *, merged: bool = True,
       state: str = "closed", base: str = "main", head_sha: str = HEAD_A,
       merge_sha: str = MERGE_A, head_repo: str = REPOSITORY,
       branch_prefix: str | None = None, author: str = "continuum-bot") -> dict:
    prefix = branch_prefix or ("opencode/issue%d-" % predecessor)
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merged_at": "2026-10-23T01:00:00Z" if merged else None,
        "merge_commit_sha": merge_sha if merged else None,
        "head": {
            "ref": prefix + "39883956862",
            "sha": head_sha,
            "repo": {"full_name": head_repo},
        },
        "base": {"ref": base},
        "user": {"login": author},
    }


def run(name: str, path: str, head_sha: str = HEAD_A, *,
        status: str = "completed", conclusion: str = "success",
        created: str = "2026-10-23T00:30:00Z", run_id: int = 1) -> dict:
    return {
        "id": run_id,
        "name": name,
        "path": path,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "run_started_at": created,
    }


def green_runs(head_sha: str = HEAD_A) -> list:
    return [
        run("CI", ".github/workflows/ci.yml", head_sha, run_id=11),
        run(
            "Continuum Contract Gate",
            ".github/workflows/contract-gate.yml",
            head_sha,
            run_id=12,
        ),
    ]


def clean_statuses() -> list:
    return [{
        "context": "continuum/pr-agent-review",
        "state": "success",
        "description": "PR-Agent review complete: clean",
    }]


def complete_predecessor(number: int = 374, *, head_sha: str = HEAD_A,
                         merge_sha: str = MERGE_A, pr_number: int = 900):
    return {
        "issue": issue(number, state="closed", reason="completed"),
        "pull_requests": [
            pr(pr_number, number, head_sha=head_sha, merge_sha=merge_sha)
        ],
        "runs": green_runs(head_sha),
        "statuses": clean_statuses(),
        "reviews": [],
    }


def decide(*, target: int = 373, predecessor: int = 374, comments=None,
           labels=None, predecessor_issue=None, pull_requests=(),
           runs=(), statuses=(), reviews=(), **kwargs):
    return decide_owner_resume(
        target_issue=issue(target, labels=labels or [PAUSE, READY]),
        repository_owner=OWNER,
        repository=REPOSITORY,
        comments=comments if comments is not None else [marker(predecessor)],
        labels=labels or {PAUSE, READY},
        predecessor_issue=predecessor_issue,
        pull_requests=pull_requests,
        exact_head_runs=runs,
        statuses=statuses,
        reviews=reviews,
        default_branch="main",
        **kwargs,
    )


class MarkerAuthorizationTests(unittest.TestCase):
    def test_exact_owner_marker_is_accepted_from_api_identity(self):
        scan = scan_resume_marker(
            [marker(374)], target=373, repository_owner=OWNER
        )
        self.assertIsNotNone(scan.instruction)
        self.assertEqual(scan.instruction.predecessor, 374)

    def test_owner_login_match_is_case_insensitive(self):
        scan = scan_resume_marker(
            [marker(374, author="KodMial")], target=373, repository_owner=OWNER
        )
        self.assertIsNotNone(scan.instruction)

    def test_bot_member_and_non_owner_markers_do_not_arm(self):
        for author, association in (
            ("github-actions[bot]", "NONE"),
            ("contributor", "MEMBER"),
            ("contributor", "COLLABORATOR"),
            ("someone", "OWNER"),
        ):
            with self.subTest(author=author, association=association):
                item = marker(374, author=author)
                item["author_association"] = association
                scan = scan_resume_marker(
                    [item], target=373, repository_owner=OWNER
                )
                self.assertIsNone(scan.instruction)
                self.assertIn("not the repository owner", scan.reason)

    def test_malformed_markers_fail_closed(self):
        bodies = (
            "<!-- continuum-resume-after predecessor=374 -->",
            "<!-- continuum-resume-after:v1 predecessor=0 -->",
            "<!-- continuum-resume-after:v1 predecessor=0374 -->",
            "<!-- continuum-resume-after:v1 predecessor=374 extra -->",
            "<!-- continuum-resume-after:v1 predecessor=373 -->",
            "continuum-resume-after:v1 predecessor=374",
            "<!-- continuum-resume-after:v1 predecessor=374",
        )
        for body in bodies:
            with self.subTest(body=body):
                scan = scan_resume_marker(
                    [comment(body)], target=373, repository_owner=OWNER
                )
                self.assertIsNone(scan.instruction)

    def test_duplicate_and_conflicting_markers_fail_closed(self):
        for comments in (
            [marker(374, cid=1), marker(374, cid=2)],
            [marker(374, cid=1), marker(375, cid=2)],
        ):
            with self.subTest(count=len(comments)):
                scan = scan_resume_marker(
                    comments, target=373, repository_owner=OWNER
                )
                self.assertIsNone(scan.instruction)
                self.assertIn("duplicate", scan.reason)

    def test_prose_that_mentions_the_marker_never_authorizes(self):
        for body in (
            "Please use continuum-resume-after:v1 predecessor=374",
            "Please use\n<!-- continuum-resume-after:v1 predecessor=374 -->",
            "<!-- continuum-resume-after:v1 predecessor=374 -->\n"
            "<!-- continuum-resume-after:v1 predecessor=375 -->",
        ):
            with self.subTest(body=body):
                scan = scan_resume_marker(
                    [comment(body)], target=373, repository_owner=OWNER
                )
                self.assertIsNone(scan.instruction)

    def test_owner_marker_line_may_have_an_explanation(self):
        scan = scan_resume_marker(
            [comment(
                "<!-- continuum-resume-after:v1 predecessor=374 -->\n"
                "Owner authorization: wait for the merged predecessor first."
            )],
            target=373,
            repository_owner=OWNER,
        )
        self.assertIsNotNone(scan.instruction)
        self.assertEqual(scan.instruction.predecessor, 374)


class CompletionStateMachineTests(unittest.TestCase):
    def test_predecessor_still_open_keeps_target_paused(self):
        decision = decide(predecessor_issue=issue(374, state="open"))
        self.assertEqual(decision.state, WAIT)
        self.assertNotEqual(decision.state, RESUME)

    def test_opened_pr_or_green_ci_without_merge_keeps_target_paused(self):
        for merged, state in ((False, "open"), (False, "closed")):
            with self.subTest(merged=merged, state=state):
                evidence = complete_predecessor()
                evidence["pull_requests"] = [
                    pr(900, 374, merged=merged, state=state)
                ]
                decision = decide(
                    predecessor_issue=evidence["issue"],
                    pull_requests=evidence["pull_requests"],
                    runs=evidence["runs"],
                    statuses=evidence["statuses"],
                )
                self.assertEqual(decision.state, NOOP)

    def test_merged_pr_with_open_predecessor_issue_keeps_target_paused(self):
        evidence = complete_predecessor()
        evidence["issue"] = issue(374, state="open")
        decision = decide(
            predecessor_issue=evidence["issue"],
            pull_requests=evidence["pull_requests"],
            runs=evidence["runs"],
            statuses=evidence["statuses"],
        )
        self.assertEqual(decision.state, WAIT)

    def test_merged_pr_with_not_completed_issue_is_not_authorization(self):
        evidence = complete_predecessor()
        evidence["issue"] = issue(374, state="closed", reason="not_planned")
        decision = decide(
            predecessor_issue=evidence["issue"],
            pull_requests=evidence["pull_requests"],
            runs=evidence["runs"],
            statuses=evidence["statuses"],
        )
        self.assertEqual(decision.state, NOOP)

    def test_complete_chain_resumes_exactly_once(self):
        evidence = complete_predecessor()
        labels = {PAUSE, READY}
        decision = decide(
            comments=[marker(374)],
            labels=labels,
            predecessor_issue=evidence["issue"],
            pull_requests=evidence["pull_requests"],
            runs=evidence["runs"],
            statuses=evidence["statuses"],
        )
        self.assertEqual(decision.state, RESUME)
        self.assertEqual(decision.instruction.predecessor, 374)
        self.assertEqual(decision.predecessor_pr, 900)
        self.assertEqual(decision.predecessor_head, HEAD_A)

        # Simulate the only mutation: remove the armed target's pause label.
        labels.discard(PAUSE)
        repeat = decide(
            comments=[marker(374)],
            labels=labels,
            predecessor_issue=evidence["issue"],
            pull_requests=evidence["pull_requests"],
            runs=evidence["runs"],
            statuses=evidence["statuses"],
        )
        self.assertEqual(repeat.state, NOOP)

    def test_357_waits_for_373_and_then_resumes_its_own_chain(self):
        waiting = decide(
            target=357,
            predecessor=373,
            comments=[marker(373)],
            predecessor_issue=issue(373, state="open"),
        )
        self.assertEqual(waiting.state, WAIT)

        evidence = complete_predecessor(number=373)
        resumed = decide(
            target=357,
            predecessor=373,
            comments=[marker(373)],
            predecessor_issue=evidence["issue"],
            pull_requests=evidence["pull_requests"],
            runs=evidence["runs"],
            statuses=evidence["statuses"],
        )
        self.assertEqual(resumed.state, RESUME)
        self.assertEqual(resumed.predecessor_pr, 900)

    def test_358_without_a_marker_is_never_resumed(self):
        decision = decide(
            target=358,
            predecessor=373,
            comments=[],
            predecessor_issue=issue(373, state="open"),
        )
        self.assertEqual(decision.state, NOOP)

    def test_unrelated_paused_targets_without_markers_are_never_resumed(self):
        for target in (344, 345, 346, 168):
            with self.subTest(target=target):
                decision = decide(target=target, comments=[])
                self.assertEqual(decision.state, NOOP)


class NegativeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.evidence = complete_predecessor()

    def complete(self, **overrides):
        return decide(
            comments=overrides.pop("comments", [marker(374)]),
            labels=overrides.pop("labels", {PAUSE, READY}),
            predecessor_issue=overrides.pop(
                "predecessor_issue", self.evidence["issue"]
            ),
            pull_requests=overrides.pop(
                "pull_requests", self.evidence["pull_requests"]
            ),
            runs=overrides.pop("runs", self.evidence["runs"]),
            statuses=overrides.pop("statuses", self.evidence["statuses"]),
            reviews=overrides.pop("reviews", self.evidence["reviews"]),
            **overrides,
        )

    def test_target_open_pr_or_active_run_blocks_resume(self):
        self.assertEqual(
            self.complete(open_pr_issue_numbers=[373]).state, NOOP
        )
        self.assertEqual(
            self.complete(active_run_issue_numbers=[373]).state, NOOP
        )

    def test_target_stop_labels_block_resume(self):
        self.assertEqual(
            self.complete(labels={PAUSE, READY, "no-auto-merge"}).state, NOOP
        )
        self.assertEqual(
            self.complete(labels={PAUSE, READY, "automation:blocked"}).state,
            NOOP,
        )

    def test_owner_generation_stop_blocks_resume(self):
        stop = (
            "<!-- continuum-task-generation-stop "
            "repo=kodmial/continuum issue=373 generation=1 "
            "pinned-spec=%s live-spec=%s reason=drift -->"
            % ("1" * 64, "2" * 64)
        )
        decision = self.complete(
            comments=[
                marker(374, cid=1, at=NOW - timedelta(hours=2)),
                comment(stop, cid=2, at=NOW - timedelta(hours=1)),
            ]
        )
        self.assertEqual(decision.state, NOOP)
        self.assertIn("STOP", decision.reason)

    def test_post_marker_dispatch_recovery_or_owner_command_fences_retry(self):
        bodies = (
            "<!-- issue-scheduler-dispatch -->\n/oc",
            "<!-- continuum-429-recovery op=x gen=1 state=observed -->",
            "/oc",
        )
        for body in bodies:
            with self.subTest(body=body):
                decision = self.complete(
                    comments=[
                        marker(374, cid=1, at=NOW - timedelta(hours=2)),
                        comment(body, cid=2, at=NOW - timedelta(hours=1)),
                    ]
                )
                self.assertEqual(decision.state, NOOP)
                self.assertIn("post-marker", decision.reason)

    def test_fake_pr_binding_or_non_merged_latest_pr_fails(self):
        title_only = pr(900, 374, branch_prefix="feature/not-bound-")
        self.assertEqual(
            self.complete(pull_requests=[title_only]).state, NOOP
        )
        latest_unmerged = [
            pr(900, 374, head_sha=HEAD_A),
            pr(901, 374, merged=False, state="closed", head_sha=HEAD_B),
        ]
        self.assertEqual(
            self.complete(pull_requests=latest_unmerged).state, NOOP
        )

    def test_wrong_base_or_head_repository_fails(self):
        self.assertEqual(
            self.complete(
                pull_requests=[pr(900, 374, base="release")]
            ).state,
            NOOP,
        )
        self.assertEqual(
            self.complete(
                pull_requests=[pr(900, 374, head_repo="attacker/continuum")]
            ).state,
            NOOP,
        )

    def test_stale_or_missing_full_sha_fails(self):
        self.assertEqual(
            self.complete(
                pull_requests=[pr(900, 374, head_sha="abc1234")]
            ).state,
            NOOP,
        )
        self.assertEqual(
            self.complete(
                pull_requests=[pr(900, 374, merge_sha="")]
            ).state,
            NOOP,
        )

    def test_missing_or_failed_ci_and_contract_gate_fail(self):
        self.assertEqual(
            self.complete(runs=[green_runs()[0]]).state, NOOP
        )
        canceled_ci = [
            run("CI", ".github/workflows/ci.yml", HEAD_A,
                status="completed", conclusion="cancelled"),
            run("Continuum Contract Gate",
                ".github/workflows/contract-gate.yml", HEAD_A),
        ]
        self.assertEqual(self.complete(runs=canceled_ci).state, NOOP)
        failed_gate = [
            run("CI", ".github/workflows/ci.yml", HEAD_A),
            run("Continuum Contract Gate",
                ".github/workflows/contract-gate.yml", HEAD_A,
                conclusion="failure"),
        ]
        self.assertEqual(self.complete(runs=failed_gate).state, NOOP)

    def test_independent_review_is_required_and_author_bot_cannot_approve(self):
        self.assertEqual(self.complete(statuses=[]).state, NOOP)
        self.assertEqual(
            self.complete(
                statuses=[],
                reviews=[{
                    "state": "APPROVED",
                    "commit_id": HEAD_A,
                    "user": {"login": "continuum-bot"},
                }],
            ).state,
            NOOP,
        )
        self.assertEqual(
            self.complete(
                statuses=[],
                reviews=[{
                    "state": "APPROVED",
                    "commit_id": HEAD_A,
                    "user": {"login": "reviewer[bot]"},
                }],
            ).state,
            NOOP,
        )
        self.assertEqual(
            self.complete(
                statuses=[],
                reviews=[{
                    "state": "APPROVED",
                    "commit_id": HEAD_B,
                    "user": {"login": "human-reviewer"},
                }],
            ).state,
            NOOP,
        )
        self.assertEqual(
            self.complete(
                statuses=[],
                reviews=[{
                    "state": "APPROVED",
                    "commit_id": HEAD_A,
                    "user": {"login": "human-reviewer"},
                }],
            ).state,
            RESUME,
        )

    def test_api_failure_fails_closed(self):
        self.assertEqual(self.complete(api_error="403").state, NOOP)

    def test_closed_or_unadmitted_target_is_noop(self):
        result = decide_owner_resume(
            target_issue=issue(373, state="closed", labels=[PAUSE, READY]),
            repository_owner=OWNER,
            repository=REPOSITORY,
            comments=[marker(374)],
            labels={PAUSE, READY},
            predecessor_issue=self.evidence["issue"],
        )
        self.assertEqual(result.state, NOOP)
        self.assertEqual(
            self.complete(labels={PAUSE}).state, NOOP
        )

    def test_ambiguous_multiple_markers_never_resume(self):
        decision = self.complete(
            comments=[marker(374, cid=1), marker(373, cid=2)]
        )
        self.assertEqual(decision.state, NOOP)

    def test_predecessor_completion_rejects_unbound_or_failed_chain(self):
        complete, _reason, _pr, _head = evaluate_predecessor_completion(
            predecessor_issue=self.evidence["issue"],
            pull_requests=self.evidence["pull_requests"],
            exact_head_runs=self.evidence["runs"],
            statuses=self.evidence["statuses"],
            reviews=[],
            repository=REPOSITORY,
        )
        self.assertTrue(complete)

        complete, reason, _pr, _head = evaluate_predecessor_completion(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            pull_requests=[],
            exact_head_runs=[],
            statuses=[],
            reviews=[],
            repository=REPOSITORY,
        )
        self.assertFalse(complete)
        self.assertIn("binding", reason)


class SchedulerResumeBudgetTests(unittest.TestCase):
    def records(self, *entries):
        records = []
        for entry in entries:
            body, author_is_owner, when = entry[:3]
            login = entry[3] if len(entry) > 3 else OWNER
            records.append(
                CommentRecord(
                    body=body,
                    author_is_owner=author_is_owner,
                    created_at=when,
                    author_login=login,
                )
            )
        return records

    def scheduler_issue(self, labels):
        return {
            373: IssueState(
                number=373,
                title="P0: recover legacy 429 checkpoint",
                body="",
                labels=set(labels),
                is_open=True,
            )
        }

    def config(self):
        return SchedulerConfig(
            ready_label=READY,
            pause_label=PAUSE,
            in_progress_label="automation:in-progress",
            max_dispatch_attempts=2,
            pause_on_failure=True,
            lease_minutes=45,
            wip_limit=2,
        )

    def test_marker_baseline_is_only_valid_for_api_trusted_exact_marker(self):
        comments = self.records(
            (marker(374)["body"], True, NOW - timedelta(hours=1)),
            ("<!-- continuum-resume-after:v1 predecessor=375 -->", True,
             NOW - timedelta(minutes=30)),
        )
        self.assertIsNone(
            resume_baseline(comments, target=373, repository_owner=OWNER)
        )
        valid = self.records(
            (marker(374)["body"], True, NOW - timedelta(hours=1)),
        )
        self.assertEqual(
            resume_baseline(valid, target=373, repository_owner=OWNER),
            NOW - timedelta(hours=1),
        )

    def test_spent_pre_marker_attempts_do_not_repause_target(self):
        comments = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=3)),
            (marker(374)["body"], True, NOW - timedelta(hours=2)),
        )
        issues = self.scheduler_issue(
            {READY, PAUSE, "automation:in-progress"}
        )
        result = reconcile(
            NOW,
            issues,
            set(),
            set(),
            {373: comments},
            {},
            {},
            set(),
            self.config(),
            repository_owner=OWNER,
        )
        self.assertNotIn(PAUSE, issues[373].labels)
        self.assertNotIn(373, result.paused)

    def test_post_marker_attempts_remain_bounded(self):
        comments = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=5)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            (marker(374)["body"], True, NOW - timedelta(hours=3)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=2)),
        )
        issues = self.scheduler_issue(
            {PAUSE, READY, "automation:in-progress"}
        )
        first = reconcile(
            NOW,
            issues,
            set(),
            set(),
            {373: comments},
            {},
            {},
            set(),
            self.config(),
            repository_owner=OWNER,
        )
        self.assertNotIn(373, first.paused)

        bounded = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=5)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            (marker(374)["body"], True, NOW - timedelta(hours=3)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=2)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=1)),
        )
        issues = self.scheduler_issue(
            {READY, "automation:in-progress"}
        )
        second = reconcile(
            NOW,
            issues,
            set(),
            set(),
            {373: bounded},
            {},
            {},
            set(),
            self.config(),
            repository_owner=OWNER,
        )
        self.assertEqual(second.paused, [373])
        self.assertIn(PAUSE, issues[373].labels)

    def test_invalid_marker_does_not_reset_attempt_budget(self):
        comments = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=3)),
            (marker(374, author="github-actions[bot]")["body"], True,
             NOW - timedelta(hours=2), "github-actions[bot]"),
        )
        issues = self.scheduler_issue({READY, "automation:in-progress"})
        result = reconcile(
            NOW,
            issues,
            set(),
            set(),
            {373: comments},
            {},
            {},
            set(),
            self.config(),
            repository_owner=OWNER,
        )
        self.assertEqual(result.paused, [373])


class ProductionWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = SCHEDULER.read_text(encoding="utf-8")

    def test_workflow_embeds_the_production_contract(self):
        for marker in (
            "continuum-resume-after:v1",
            "ownerResumeMarkerRe",
            "ownerResumeMarkerShapeRe",
            "ownerResumeMarkerShapeCountRe",
            "ownerResumeScan",
            "ownerResumeIssueComments",
            "ownerResumeCommentPageLimit = 5",
            "ownerResumeDefaultBranch",
            "ownerResumeCiWorkflow",
            "ownerResumeContractGateWorkflow",
            "ownerResumeIndependentReview",
            "continuum/pr-agent-review",
            "ownerResumeGenerationStopped",
            "ownerResumeActivityAfter",
            "ownerResumeBaselineNumber",
            "countedSchedulerDispatches",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)

    def test_authorization_is_api_owner_and_exact_syntax_only(self):
        self.assertIn(
            "ownerResumeLogin(comment) !== owner.toLowerCase()", self.body
        )
        self.assertIn(
            "/^[ \\t]*<!--\\s*continuum-resume-after:v1\\s+predecessor=([1-9]\\d*)\\s*-->[ \\t]*(?:\\r?\\n|$)/",
            self.body,
        )
        self.assertIn("ownerResumeMarkerLineRe", self.body)
        self.assertNotIn("continuum-resume-after:v1 predecessor=374", self.body)
        self.assertNotIn("continuum-resume-after:v1 predecessor=373", self.body)

    def test_completion_is_bound_to_pr_branch_merge_exact_head_gates(self):
        for marker in (
            "opencode/issue' + predecessorNumber + '-'",
            "readGithub.rest.pulls.get",
            "fullPr.merged",
            "fullPr.base?.ref",
            "fullPr.merge_commit_sha",
            "listWorkflowRunsForRepo",
            "'.github/workflows/' + 'ci.yml'",
            "'.github/workflows/' + 'contract-gate.yml'",
            "getCombinedStatusForRef",
            "listReviews",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)

    def test_only_the_armed_target_pause_label_is_mutated(self):
        start = self.body.index(
            "Owner-armed resume is evaluated only over the scheduler's"
        )
        end = self.body.index(
            "Reconcile reservations for the gap between posting /oc",
            start,
        )
        block = self.body[start:end]
        self.assertIn("await removeLabel(issue.number, pausedLabel);", block)
        self.assertNotIn("createComment", block)
        self.assertNotIn("addLabel", block)
        self.assertNotIn("issues.update", block)
        self.assertNotIn("dispatchWorkflow", block)
        self.assertNotIn("/oc", block)
        self.assertIn("checkpoint/snapshot state was not modified", block.lower())


if __name__ == "__main__":
    unittest.main()
