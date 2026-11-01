"""Owner-armed predecessor-completion-gated resume (kodmial/continuum#382).

The tests exercise the production decision module, the existing scheduler
reconciliation model, and the JavaScript embedded in the shipped scheduler
(through a Node harness). Workflow string assertions pin the surrounding
production contract so the scheduler cannot drift into a fake helper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import unittest

from continuum import task_snapshot
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

try:
    from tests.scheduler_workflow import scheduler_workflow_text
except ModuleNotFoundError:
    from scheduler_workflow import scheduler_workflow_text

ROOT = Path(__file__).resolve().parents[1]

SCHEDULER_SCRIPT = ROOT / ".github" / "scripts" / "issue_scheduler.js"

OWNER = "kodmial"
REPOSITORY = "kodmial/continuum"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGE_A = "c" * 40
MERGE_B = "d" * 40

PAUSE = "automation:paused"
READY = "automation:ready"

# Executes the real owner-resume JavaScript from the extracted production
# scheduler module. The Python decision model below documents the contract;
# this harness proves the shipped module implements the same decisions instead
# of a parallel mock.
WORKFLOW_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(process.env.SCENARIO_JSON || '{}');
const body = fs.readFileSync(process.env.SCHEDULER, 'utf8');
const start = body.indexOf('const ownerResumeMarkerRe =');
const end = body.indexOf('async function openDeclaredBlockers(', start);
if (start < 0 || end < 0) throw new Error('owner-resume block missing');
const segment = body.slice(start, end);
const logs = [];
const core = {
  notice(m) { logs.push({ level: 'notice', msg: String(m) }); },
  warning(m) { logs.push({ level: 'warning', msg: String(m) }); },
};
const owner = scenario.owner || 'kodmial';
const repo = scenario.repo || 'continuum';
const repoFullName = owner + '/' + repo;
const context = {
  payload: { repository: { default_branch: scenario.defaultBranch || 'main' } },
};
const pausedLabel = 'automation:paused';
const readyLabel = 'automation:ready';
const blockedLabel = 'automation:blocked';
const dispatchMarker = '<!-- issue-scheduler-dispatch -->';
const generationStopMarkerRe = /<!--\s*continuum-task-generation-stop\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s+pinned-spec=([0-9a-fA-F]{64})\s+live-spec=([0-9a-fA-F]{64})\s+reason=(drift|tampered)\s*-->/;
const generationRecoverMarkerRe = /<!--\s*continuum-task-generation-recover\s+repo=([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\s+issue=(\d+)\s+generation=(\d+)\s*-->/;
function stopOrderKey(entry) {
  return (entry.created_at || '') + '#' + String(entry.id || 0).padStart(12, '0');
}
const target = Number(scenario.target || 373);
const labels = scenario.labels || ['automation:paused', 'automation:ready'];
const labelsByIssue = new Map([[target, new Set(labels)]]);
const openPrIssues = new Set(scenario.openPrIssueNumbers || []);
const activeRunIssues = new Set(scenario.activeRunIssueNumbers || []);
const pulls = scenario.pulls || [];
const commentsByIssue = scenario.commentsByIssue || {};
const predecessorIssues = scenario.predecessorIssues || {};
const fullPrs = scenario.fullPrs || {};
const failures = scenario.failures || {};
function maybeFail(kind) {
  if (!failures[kind]) return;
  const error = new Error(kind + ' failure');
  error.status = failures[kind];
  throw error;
}
const readGithub = {
  rest: {
    issues: {
      listComments: async (params) => {
        maybeFail('comments');
        if (scenario.commentPages) {
          const pages = scenario.commentPages[String(params.issue_number)] || [];
          return { data: pages[(params.page || 1) - 1] || [] };
        }
        return { data: commentsByIssue[String(params.issue_number)] || [] };
      },
      get: async (params) => {
        maybeFail('predecessor');
        const value = predecessorIssues[String(params.issue_number)];
        if (!value) {
          const error = new Error('not found');
          error.status = 404;
          throw error;
        }
        return { data: value };
      },
    },
    pulls: {
      get: async (params) => {
        maybeFail('pull');
        return { data: fullPrs[String(params.pull_number)] };
      },
      listReviews: async () => {
        maybeFail('reviews');
        return { data: scenario.reviews || [] };
      },
    },
    actions: {
      listWorkflowRunsForRepo: async () => {
        maybeFail('runs');
        return { data: scenario.runs || [] };
      },
    },
    repos: {
      getCombinedStatusForRef: async () => {
        maybeFail('statuses');
        return { data: { statuses: scenario.statuses || [] } };
      },
    },
  },
  paginate: async (fn, params) => {
    const result = await fn(params);
    if (Array.isArray(result)) return result;
    if (result && Array.isArray(result.data)) return result.data;
    if (result && result.data && Array.isArray(result.data.workflow_runs)) {
      return result.data.workflow_runs;
    }
    return [];
  },
};
const factory = new Function(
  'owner', 'repo', 'repoFullName', 'context', 'core', 'readGithub',
  'labelsByIssue', 'pausedLabel', 'readyLabel', 'blockedLabel',
  'openPrIssues', 'activeRunIssues', 'pulls', 'dispatchMarker',
  'generationStopMarkerRe', 'generationRecoverMarkerRe', 'stopOrderKey',
  segment + `
  return {
    ownerResumeScan, ownerResumeDecision, ownerResumeIssueComments,
    ownerResumeBaselineNumber, ownerResumeActivityAfter,
    ownerResumeGenerationStopped
  };`
);
const api = factory(
  owner, repo, repoFullName, context, core, readGithub, labelsByIssue,
  pausedLabel, readyLabel, blockedLabel, openPrIssues, activeRunIssues,
  pulls, dispatchMarker, generationStopMarkerRe, generationRecoverMarkerRe,
  stopOrderKey
);
(async () => {
  const issue = {
    number: target,
    state: 'open',
    labels: labels.map(name => ({ name })),
  };
  const comments = await api.ownerResumeIssueComments(target).catch(error => ({
    error: String(error && error.message || error),
  }));
  let output;
  if (!Array.isArray(comments)) {
    output = { state: 'NOOP', reason: comments.error };
  } else if (scenario.op === 'scan') {
    output = api.ownerResumeScan(target, comments);
  } else if (scenario.op === 'baseline') {
    output = { baseline: api.ownerResumeBaselineNumber(comments, target) };
  } else {
    const scan = api.ownerResumeScan(target, comments);
    output = scan.instruction
      ? await api.ownerResumeDecision(issue, scan.instruction, comments)
      : { state: 'NOOP', reason: scan.reason };
  }
  process.stdout.write(JSON.stringify({ output, logs }));
})().catch(error => {
  process.stdout.write(JSON.stringify({
    fatal: String(error && error.message || error),
    stack: String(error && error.stack || ''),
  }));
});
"""


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
       branch_prefix: str | None = None, author: str = "continuum-bot",
       body: str = "") -> dict:
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
        "body": body,
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


TASK_TITLE = "P0: Fix 429 fresh-run checkout before checkpoint restoration"
TASK_BODY = "## Definition of Done\n- [ ] Restore the exact checkpoint.\n"


def snapshot_record(number: int = 374, *, title: str = TASK_TITLE,
                    body: str = TASK_BODY) -> dict:
    return task_snapshot.build_snapshot(REPOSITORY, number, title, body)


def snapshot_comment(number: int = 374, *, title: str = TASK_TITLE,
                     body: str = TASK_BODY, author: str = OWNER,
                     cid: int = 500, at: datetime = NOW,
                     association: str = "OWNER") -> dict:
    return {
        "id": cid,
        "body": task_snapshot.render_snapshot_comment(
            snapshot_record(number, title=title, body=body)
        ),
        "user": {"login": author},
        "author_association": association,
        "created_at": at.isoformat().replace("+00:00", "Z"),
    }


def task_snapshot_pr_body(number: int = 374, *, title: str = TASK_TITLE,
                          body: str = TASK_BODY, repo: str = REPOSITORY) -> str:
    snapshot = snapshot_record(number, title=title, body=body)
    context = "<!-- continuum-task-context repo={} issue={} -->".format(
        repo, number
    )
    reference = task_snapshot.render_pr_snapshot_ref(
        dict(snapshot, repo=repo)
    )
    return context + "\n" + reference


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

    def test_actual_374_style_fix_branch_binds_through_task_snapshot(self):
        # kodmial/continuum#374 merged as PR #397 on the normal owner fix
        # branch `fix/...`, not `opencode/issue374-`. Its task-context and
        # snapshot-reference markers must bind the predecessor to the
        # durable snapshot comment, never to a matching title.
        body = task_snapshot_pr_body(374)
        candidate = pr(
            397, 374, branch_prefix="fix/429-bootstrap-checkout-374-395-",
            body=body,
        )
        decision = decide(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            predecessor_comments=[snapshot_comment(374)],
            pull_requests=[candidate],
            runs=green_runs(),
            statuses=clean_statuses(),
        )
        self.assertEqual(decision.state, RESUME)
        self.assertEqual(decision.predecessor_pr, 397)

    def test_legacy_canonical_branch_context_binding_still_resumes(self):
        # Pre-snapshot OpenCode PRs carry only a task-context marker. The
        # canonical opencode/issue<N>- branch remains a trusted binding, while
        # a conflicting context marker must still fail closed.
        candidate = pr(
            900, 374,
            body="<!-- continuum-task-context repo={} issue=374 -->".format(
                REPOSITORY
            ),
        )
        decision = decide(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            predecessor_comments=[],
            pull_requests=[candidate],
            runs=green_runs(),
            statuses=clean_statuses(),
        )
        self.assertEqual(decision.state, RESUME)

        conflicting = pr(
            900, 374,
            body="<!-- continuum-task-context repo={} issue=375 -->".format(
                REPOSITORY
            ),
        )
        rejected = decide(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            predecessor_comments=[],
            pull_requests=[conflicting],
            runs=green_runs(),
            statuses=clean_statuses(),
        )
        self.assertEqual(rejected.state, NOOP)

    def test_snapshot_binding_requires_the_durable_predecessor_snapshot(self):
        candidate = pr(
            397, 374, branch_prefix="fix/429-bootstrap-checkout-374-395-",
            body=task_snapshot_pr_body(374),
        )
        no_durable = decide(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            predecessor_comments=[],
            pull_requests=[candidate],
            runs=green_runs(),
            statuses=clean_statuses(),
        )
        self.assertEqual(no_durable.state, NOOP)

        wrong_pin = decide(
            predecessor_issue=issue(374, state="closed", reason="completed"),
            predecessor_comments=[snapshot_comment(374, body="edited contract")],
            pull_requests=[candidate],
            runs=green_runs(),
            statuses=clean_statuses(),
        )
        self.assertEqual(wrong_pin.state, NOOP)

    def test_snapshot_binding_rejects_other_issue_and_ambiguous_markers(self):
        for body in (
            task_snapshot_pr_body(375),
            task_snapshot_pr_body(374) + "\n" + task_snapshot_pr_body(374),
            "<!-- continuum-task-context repo=kodmial/continuum issue=374 -->\n"
            "No snapshot reference here.",
        ):
            with self.subTest(body=body[:60]):
                candidate = pr(
                    397, 374,
                    branch_prefix="fix/429-bootstrap-checkout-374-395-",
                    body=body,
                )
                decision = decide(
                    predecessor_issue=issue(374, state="closed",
                                            reason="completed"),
                    predecessor_comments=[snapshot_comment(374)],
                    pull_requests=[candidate],
                    runs=green_runs(),
                    statuses=clean_statuses(),
                )
                self.assertEqual(decision.state, NOOP)

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

    def test_untrusted_marker_shaped_comments_do_not_fence_retry(self):
        bodies = (
            "<!-- issue-scheduler-dispatch -->\n/oc",
            "<!-- continuum-429-recovery op=x gen=1 state=observed -->",
        )
        for body in bodies:
            with self.subTest(body=body):
                untrusted = self.complete(
                    comments=[
                        marker(374, cid=1, at=NOW - timedelta(hours=2)),
                        comment(
                            body, author="attacker", cid=2,
                            at=NOW - timedelta(hours=1), association="NONE",
                        ),
                    ]
                )
                self.assertEqual(untrusted.state, RESUME)

                trusted = self.complete(
                    comments=[
                        marker(374, cid=1, at=NOW - timedelta(hours=2)),
                        comment(
                            body, author="github-actions[bot]", cid=2,
                            at=NOW - timedelta(hours=1), association="NONE",
                        ),
                    ]
                )
                self.assertEqual(trusted.state, NOOP)
                self.assertIn("post-marker", trusted.reason)

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
        for error in ("403", "429", "500", "502", "503", "timeout", "crash"):
            with self.subTest(error=error):
                self.assertEqual(self.complete(api_error=error).state, NOOP)

    def test_predecessor_must_be_the_marker_issue_not_a_pr_or_other_number(self):
        marker_pr = issue(374)
        marker_pr["pull_request"] = {"url": "https://example.invalid/pr/374"}
        self.assertEqual(
            self.complete(predecessor_issue=marker_pr).state, NOOP
        )
        self.assertEqual(
            self.complete(predecessor_issue=issue(999)).state, NOOP
        )

    def test_canceled_or_closed_unmerged_predecessor_pr_fails(self):
        for candidate in (
            pr(900, 374, merged=False, state="closed"),
            pr(900, 374, merged=False, state="open"),
        ):
            with self.subTest(state=candidate["state"]):
                self.assertEqual(
                    self.complete(pull_requests=[candidate]).state, NOOP
                )
        canceled_run = green_runs()
        canceled_run[0]["conclusion"] = "cancelled"
        self.assertEqual(self.complete(runs=canceled_run).state, NOOP)

    def test_reordered_comments_are_evaluated_by_timestamp_not_list_order(self):
        recovery_before = comment(
            "<!-- continuum-429-recovery op=x gen=1 state=observed "
            "attempt=1 next_due=0 checkpoint=- sha=- run=- -->",
            cid=2,
            at=NOW - timedelta(hours=2),
        )
        marker_after = marker(374, cid=1, at=NOW - timedelta(hours=1))
        decision = self.complete(comments=[marker_after, recovery_before])
        self.assertEqual(decision.state, RESUME)

    def test_crash_before_any_post_marker_attempt_is_safely_idempotent(self):
        # A crash before dispatch leaves no post-marker evidence. Re-running
        # the decision is the same bounded admission, not a second model
        # generation; the next successful pass removes the pause exactly once.
        decision = self.complete()
        self.assertEqual(decision.state, RESUME)
        retry = self.complete()
        self.assertEqual(retry.state, RESUME)

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

    def test_spent_pre_marker_attempts_do_not_repause_after_admission(self):
        # The production owner-resume decision admits the target first (it
        # removes the pause label only after predecessor completion). The
        # lease reconcile then sees an admitted target and resets the budget.
        comments = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=3)),
            (marker(374)["body"], True, NOW - timedelta(hours=2)),
        )
        issues = self.scheduler_issue(
            {READY, "automation:in-progress"}
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

    def test_marker_without_admission_keeps_pause_even_below_budget(self):
        # A predecessor that is still open produces WAIT. The marker alone
        # must never let an expired lease release the pause, even when fewer
        # than max_dispatch_attempts history entries exist.
        for dispatches in (1, 2, 3):
            with self.subTest(dispatches=dispatches):
                comments = self.records(
                    *[
                        (
                            "<!-- issue-scheduler-dispatch -->",
                            True,
                            NOW - timedelta(hours=6 - index),
                        )
                        for index in range(dispatches)
                    ],
                    (marker(374)["body"], True, NOW - timedelta(hours=1)),
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
                self.assertIn(PAUSE, issues[373].labels)
                # The waiting reservation is kept: lease expiry must not
                # release it while the owner-armed resume is still gated.
                self.assertNotIn(373, result.reservations_removed)
                self.assertIn("automation:in-progress", issues[373].labels)
                self.assertNotIn(373, result.dispatched)

    def test_marker_wait_keeps_reservation_without_pause_on_failure(self):
        # pause_on_failure=False must not turn lease expiry into an early
        # dispatch before the predecessor completes.
        config = SchedulerConfig(
            ready_label=READY,
            pause_label=PAUSE,
            in_progress_label="automation:in-progress",
            max_dispatch_attempts=2,
            pause_on_failure=False,
            lease_minutes=45,
            wip_limit=2,
        )
        comments = self.records(
            (marker(374)["body"], True, NOW - timedelta(hours=2)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(minutes=90)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(minutes=60)),
        )
        issues = self.scheduler_issue({READY, PAUSE, "automation:in-progress"})
        result = reconcile(
            NOW,
            issues,
            set(),
            set(),
            {373: comments},
            {},
            {},
            set(),
            config,
            repository_owner=OWNER,
        )
        self.assertIn(PAUSE, issues[373].labels)
        self.assertNotIn(373, result.reservations_removed)
        self.assertIn("automation:in-progress", issues[373].labels)
        self.assertNotIn(373, result.dispatched)

    def test_post_marker_attempts_remain_bounded(self):
        comments = self.records(
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=5)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=4)),
            (marker(374)["body"], True, NOW - timedelta(hours=3)),
            ("<!-- issue-scheduler-dispatch -->", True, NOW - timedelta(hours=2)),
        )
        issues = self.scheduler_issue(
            {READY, "automation:in-progress"}
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
            {READY, PAUSE, "automation:in-progress"}
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


class ExecutableProductionWorkflowTests(unittest.TestCase):
    def run_workflow(self, scenario):
        env = os.environ.copy()
        env["SCHEDULER"] = str(SCHEDULER_SCRIPT)
        env["SCENARIO_JSON"] = json.dumps(scenario)
        completed = subprocess.run(
            ["node", "-e", WORKFLOW_HARNESS],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            timeout=60,
        )
        self.assertEqual(
            completed.returncode, 0,
            "node harness failed: {}".format(completed.stderr),
        )
        payload = json.loads(completed.stdout)
        self.assertNotIn("fatal", payload)
        return payload["output"]

    def decision_scenario(self, **overrides):
        evidence = complete_predecessor()
        scenario = {
            "op": "decide",
            "target": 373,
            "commentsByIssue": {"373": [marker(374)]},
            "predecessorIssues": {"374": evidence["issue"]},
            "pulls": evidence["pull_requests"],
            "fullPrs": {"900": evidence["pull_requests"][0]},
            "runs": evidence["runs"],
            "statuses": evidence["statuses"],
            "reviews": evidence["reviews"],
        }
        scenario.update(overrides)
        return scenario

    def test_production_js_resumes_only_on_the_complete_chain(self):
        outcome = self.run_workflow(self.decision_scenario())
        self.assertEqual(outcome["state"], "RESUME")
        self.assertEqual(outcome["pullRequest"], 900)
        self.assertEqual(outcome["headSha"], HEAD_A)

    def test_production_js_legacy_canonical_branch_context_resumes(self):
        candidate = pr(
            900, 374,
            body="<!-- continuum-task-context repo={} issue=374 -->".format(
                REPOSITORY
            ),
        )
        scenario = self.decision_scenario(
            pulls=[candidate],
            fullPrs={"900": candidate},
        )
        self.assertEqual(self.run_workflow(scenario)["state"], "RESUME")

        conflicting = pr(
            900, 374,
            body="<!-- continuum-task-context repo={} issue=375 -->".format(
                REPOSITORY
            ),
        )
        rejected = self.decision_scenario(
            pulls=[conflicting],
            fullPrs={"900": conflicting},
        )
        self.assertEqual(
            self.run_workflow(rejected)["state"], "NOOP"
        )

    def test_production_js_predecessor_open_is_wait(self):
        scenario = self.decision_scenario(
            predecessorIssues={"374": issue(374, state="open")},
        )
        self.assertEqual(self.run_workflow(scenario)["state"], "WAIT")

    def test_production_js_snapshot_bound_non_opencode_pr_resumes(self):
        candidate = pr(
            397, 374, branch_prefix="fix/429-bootstrap-checkout-374-395-",
            body=task_snapshot_pr_body(374),
        )
        scenario = self.decision_scenario(
            commentsByIssue={
                "373": [marker(374)],
                "374": [snapshot_comment(374)],
            },
            pulls=[candidate],
            fullPrs={"397": candidate},
        )
        outcome = self.run_workflow(scenario)
        self.assertEqual(outcome["state"], "RESUME")
        self.assertEqual(outcome["pullRequest"], 397)

    def test_production_js_snapshot_binding_must_match_durable_comment(self):
        candidate = pr(
            397, 374, branch_prefix="fix/429-bootstrap-checkout-374-395-",
            body=task_snapshot_pr_body(374),
        )
        for predecessor_comments in (
            [],
            [snapshot_comment(374, body="edited contract")],
        ):
            with self.subTest(count=len(predecessor_comments)):
                scenario = self.decision_scenario(
                    commentsByIssue={
                        "373": [marker(374)],
                        "374": predecessor_comments,
                    },
                    pulls=[candidate],
                    fullPrs={"397": candidate},
                )
                self.assertEqual(
                    self.run_workflow(scenario)["state"], "NOOP"
                )

    def test_production_js_unmerged_or_unreviewed_chain_is_noop(self):
        unmerged = self.decision_scenario()
        unmerged["pulls"] = [pr(900, 374, merged=False, state="open")]
        unmerged["fullPrs"] = {"900": pr(900, 374, merged=False, state="open")}
        self.assertEqual(self.run_workflow(unmerged)["state"], "NOOP")

        unreviewed = self.decision_scenario(statuses=[])
        self.assertEqual(self.run_workflow(unreviewed)["state"], "NOOP")

        wrong_base = self.decision_scenario()
        wrong_base["pulls"] = [pr(900, 374, base="release")]
        wrong_base["fullPrs"] = {"900": pr(900, 374, base="release")}
        self.assertEqual(self.run_workflow(wrong_base)["state"], "NOOP")

    def test_production_js_target_stops_block_resume(self):
        for scenario in (
            self.decision_scenario(openPrIssueNumbers=[373]),
            self.decision_scenario(activeRunIssueNumbers=[373]),
            self.decision_scenario(
                labels=[PAUSE, READY, "no-auto-merge"]
            ),
            self.decision_scenario(labels=[PAUSE, READY, "automation:blocked"]),
        ):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.run_workflow(scenario)["state"], "NOOP")

    def test_production_js_api_failures_and_bad_markers_fail_closed(self):
        for failure in (
            {"comments": 500},
            {"predecessor": 403},
            {"runs": 429},
            {"statuses": 500},
        ):
            with self.subTest(failure=failure):
                self.assertEqual(
                    self.run_workflow(
                        self.decision_scenario(failures=failure)
                    )["state"],
                    "NOOP",
                )
        for comments in (
            [marker(374, author="github-actions[bot]")],
            [marker(374, cid=1), marker(375, cid=2)],
            [comment("<!-- continuum-resume-after predecessor=374 -->")],
        ):
            with self.subTest(comments=comments):
                outcome = self.run_workflow(
                    self.decision_scenario(commentsByIssue={"373": comments})
                )
                self.assertEqual(outcome["state"], "NOOP")

    def test_production_js_untrusted_markers_do_not_fence_retry(self):
        bodies = (
            "<!-- issue-scheduler-dispatch -->\n/oc",
            "<!-- continuum-429-recovery op=x gen=1 state=observed -->",
        )
        for body in bodies:
            with self.subTest(body=body):
                untrusted = self.decision_scenario(
                    commentsByIssue={
                        "373": [
                            marker(374, cid=1, at=NOW - timedelta(hours=2)),
                            comment(
                                body, author="attacker", cid=2,
                                at=NOW - timedelta(hours=1),
                                association="NONE",
                            ),
                        ],
                    },
                )
                self.assertEqual(
                    self.run_workflow(untrusted)["state"], "RESUME"
                )

                trusted = self.decision_scenario(
                    commentsByIssue={
                        "373": [
                            marker(374, cid=1, at=NOW - timedelta(hours=2)),
                            comment(
                                body, author="github-actions[bot]", cid=2,
                                at=NOW - timedelta(hours=1),
                                association="NONE",
                            ),
                        ],
                    },
                )
                self.assertEqual(
                    self.run_workflow(trusted)["state"], "NOOP"
                )

    def test_production_js_truncated_comment_window_fails_closed(self):
        filler = [
            comment(
                "routine comment %d" % index, author="contributor",
                cid=1000 + index, at=NOW - timedelta(hours=1),
                association="NONE",
            )
            for index in range(100)
        ]
        truncated = self.decision_scenario(
            commentPages={"373": [filler] * 5 + [filler[:1]]},
        )
        outcome = self.run_workflow(truncated)
        self.assertEqual(outcome["state"], "NOOP")
        self.assertIn("truncated", outcome["reason"])

        complete = self.decision_scenario(
            commentPages={
                "373": [
                    [marker(374)] + [
                        comment(
                            "routine comment %d" % index,
                            author="contributor", cid=2000 + index,
                            at=NOW - timedelta(hours=1), association="NONE",
                        )
                        for index in range(99)
                    ]
                ] + [filler] * 4 + [[]],
            },
        )
        self.assertEqual(self.run_workflow(complete)["state"], "RESUME")

    def test_production_js_marker_scan_and_baseline_are_exact(self):
        scenario = self.decision_scenario(op="scan")
        self.assertEqual(
            self.run_workflow(scenario)["instruction"]["predecessor"], 374
        )
        baseline = self.run_workflow(self.decision_scenario(op="baseline"))
        self.assertTrue(baseline["baseline"])


class ProductionWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = scheduler_workflow_text()

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
            "ownerResumeMarkerPending",
            "countedSchedulerDispatches",
            "ownerResumeTaskContextRe",
            "ownerResumeSnapshotRefRe",
            "ownerResumeSnapshotMarkerRe",
            "ownerResumePrSnapshotBinding",
            "ownerResumeBoundPr",
            "ownerResumeSnapshotBindingMatches",
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
            "ownerResumeTaskContextRe",
            "ownerResumeSnapshotRefRe",
            "ownerResumeSnapshotMarkerRe",
            "ownerResumeSnapshotBindingMatches",
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

    def test_simultaneous_cron_is_serialized_and_pause_is_not_prematurely_released(self):
        self.assertIn("concurrency:", self.body)
        self.assertIn("group: issue-scheduler", self.body)
        self.assertIn("cancel-in-progress: false", self.body)
        self.assertIn("ownerResumeMarkerPending", self.body)
        self.assertIn("waiting for predecessor completion", self.body)


if __name__ == "__main__":
    unittest.main()
