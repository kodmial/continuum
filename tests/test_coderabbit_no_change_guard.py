"""Executable no-change guard for canonical CodeRabbit repairs.

The tests execute the production collection, dispatch, and completion-handoff
JavaScript. A successful completed repair that leaves the exact PR HEAD
unchanged must suppress later re-dispatches of the same original finding, while
failed, forged, stale, or unverifiable outcomes never suppress anything.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest

from test_coderabbit_unresolved_dispatch import js_for_step

ROOT = Path(__file__).resolve().parents[1]
BATCHER = ROOT / ".github/workflows/continuum-coderabbit-unresolved.yml"
OPENCODE = ROOT / ".github/workflows/continuum-opencode.yml"

PR = 459
REPO_FULL = "kodmial/continuum"
HEAD = "a03dd5dc6d93e68f25c11489e84c67ac04ea9f3b"
FINDING = 4577019422
VERIFICATION_ID = FINDING + 1
REPLY_ID = FINDING + 2
REVIEW = 3311
RUN_ID = 8801


def _node(script, timeout=60):
    if not shutil.which("node"):
        raise unittest.SkipTest("Node.js unavailable")
    result = subprocess.run(
        ["node", "-"], input=script, text=True,
        capture_output=True, check=False, timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def run_handoff(changed, head=HEAD, inspected=None, run_id=RUN_ID, attempt=1,
                findings=(FINDING,), answers=(9001,), review=FINDING and REVIEW,
                existing=()):
    """Execute the canonical OpenCode completion handoff with mocks."""
    source = js_for_step(OPENCODE, "Ask CodeRabbit to verify every original finding")
    batch = [
        {
            "review_id": review,
            "finding_id": finding,
            "unresolved_id": answer,
            "verified_head": head,
        }
        for finding, answer in zip(findings, answers)
    ]
    fixture = f"""
const posted = [];
const notices = [];
const warnings = [];
const out = {{}};
const head = {json.dumps(head)};
const inspected = {json.dumps(inspected or head)};
const issueComments = {json.dumps(list(existing))};
process.env.PR_NUMBER = {json.dumps(str(PR))};
process.env.REVIEW_ID = {json.dumps(str(review))};
process.env.UNRESOLVED_ITEMS_JSON = JSON.stringify({json.dumps(batch)});
process.env.REPAIR_CHANGED = {json.dumps(changed)};
process.env.REPAIR_BASE_SHA = inspected;
process.env.REPAIR_RUN_ID = {json.dumps(str(run_id))};
process.env.REPAIR_RUN_ATTEMPT = {json.dumps(str(attempt))};
const context = {{repo: {{owner: 'kodmial', repo: 'continuum'}}}};
const core = {{
  setFailed: message => {{ throw new Error(message); }},
  setOutput: (key, value) => {{ out[key] = String(value); }},
  notice: message => notices.push(message),
  warning: message => warnings.push(message),
}};
const github = {{
  paginate: async () => issueComments.slice(),
  rest: {{
    pulls: {{
      getReview: async () => ({{data: {{commit_id: head}}}}),
      get: async () => ({{data: {{
        head: {{repo: {{full_name: 'kodmial/continuum'}},
          ref: 'opencode/issue459', sha: head}},
        review_comments: 0,
        comments: 0,
      }}}}),
      listReviewComments: async () => ({{data: []}}),
    }},
    git: {{getRef: async () => ({{data: {{object: {{sha: head}}}}}})}},
    users: {{getAuthenticated: async () => ({{data: {{login: 'kodmial'}}}})}},
    issues: {{
      listComments: async () => ({{data: issueComments.slice()}}),
      createComment: async args => {{
        posted.push(args.body);
        issueComments.push({{
          id: 900 + posted.length,
          user: {{login: 'kodmial'}},
          created_at: new Date().toISOString(),
          body: args.body,
        }});
      }},
    }},
  }},
}};
"""
    script = (
        fixture
        + "\nasync function run(){\n"
        + textwrap.indent(source, "  ")
        + "\n}\nrun().then(() => process.stdout.write(JSON.stringify({out, posted, notices, warnings})))\n"
        + ".catch(error => { console.error(error); process.exit(1); });\n"
    )
    return _node(script)


def outcome_marker(body):
    match = re.search(
        r"<!-- opencode-coderabbit-repair-outcome [^>]*-->", body
    )
    assert match, body
    return match.group(0)


def outcome_digest(body):
    match = re.search(r"\bdigest=([0-9a-f]{64})\b", outcome_marker(body))
    assert match, body
    return match.group(1)


def run_review_comment(finding_id, body, in_reply_to_id=None,
                       review_id=REVIEW, author="coderabbitai[bot]",
                       created_at="2026-11-16T20:00:00Z"):
    return {
        "id": finding_id,
        "in_reply_to_id": in_reply_to_id,
        "pull_request_review_id": review_id,
        "user": {"login": author},
        "created_at": created_at,
        "body": body,
    }


def base_review_comments(head=HEAD, finding=FINDING, review=REVIEW,
                         latest_reply_id=REPLY_ID):
    return [
        run_review_comment(finding, "Original access-control finding.", review_id=review),
        run_review_comment(
            finding + 1,
            f"<!-- opencode-coderabbit-verification review={review} "
            f"finding={finding} head={head} -->",
            in_reply_to_id=finding, author="kodmial",
            created_at="2026-11-16T20:01:00Z",
        ),
        run_review_comment(
            latest_reply_id,
            "**UNRESOLVED**\nThe access-control defect remains.",
            in_reply_to_id=finding, created_at="2026-11-16T20:30:00Z",
        ),
    ]


def base_run(run_id=RUN_ID, status="completed", conclusion="success",
             title=f"OpenCode repair PR #{PR}", path=".github/workflows/opencode.yml",
             attempt=1):
    return {
        "id": run_id,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": conclusion,
        "display_title": title,
        "path": path,
        "run_attempt": attempt,
        "created_at": "2026-11-16T20:05:00Z",
        "run_started_at": "2026-11-16T20:05:10Z",
        "updated_at": "2026-11-16T20:25:00Z",
    }


def base_jobs(repair_conclusion="success"):
    return {
        "jobs": [{
            "name": "opencode",
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"name": "Qualify current CodeRabbit unresolved batch",
                 "status": "completed", "conclusion": "success"},
                {"name": "Fix CodeRabbit review findings",
                 "status": "completed" if repair_conclusion != "skipped" else "completed",
                 "conclusion": repair_conclusion},
                {"name": "Ask CodeRabbit to verify every original finding",
                 "status": "completed", "conclusion": "success"},
            ],
        }],
    }


def provenance_artifacts(*comment_groups):
    """Run-scoped provenance artifact names implied by published markers."""
    names = []
    for group in comment_groups:
        for comment in group or []:
            if not isinstance(comment, dict):
                continue
            match = re.search(
                r"<!--\s*opencode-coderabbit-repair-outcome\s+([^>]*?)\s*-->",
                str(comment.get("body") or ""),
            )
            if not match:
                continue
            attrs = {
                key.lower(): value
                for key, value in re.findall(
                    r"([a-z_]+)=([^\s>]+)", match.group(1), re.IGNORECASE
                )
            }
            names.append(
                "continuum-repair-provenance-"
                f"{attrs.get('head')}-{attrs.get('digest')}-"
                f"{attrs.get('attempt')}-{attrs.get('changed')}"
            )
    return [
        {"id": index + 1, "name": name, "expired": False}
        for index, name in enumerate(dict.fromkeys(names))
    ]


def collect_step(items, issue_comments, review_comments=None, head=HEAD,
                 run=None, jobs=None, runs_error=False, issue_error=False,
                 threads=None, artifacts=None, artifacts_error=False):
    source = js_for_step(BATCHER, "Collect all unresolved findings")
    review_comments = (
        base_review_comments(head=head) if review_comments is None
        else review_comments
    )
    threads = threads if threads is not None else [
        {"isResolved": False, "comments": {"nodes": [
            {"databaseId": item["finding_id"],
             "author": {"login": "coderabbitai[bot]"}}
        ]}}
        for item in items
    ]
    artifacts = (
        provenance_artifacts(issue_comments)
        if artifacts is None else artifacts
    )
    fixture = f"""
const head = {json.dumps(head)};
process.env.COMMENT_PAT = 'test-pat';
process.env.CONTINUUM_OPENCODE_WORKFLOW = 'opencode.yml';
const comments = {json.dumps(review_comments)};
let issueComments = {json.dumps(issue_comments)};
const posted = [];
const out = {{}};
const notices = [];
const warnings = [];
const core = {{
  setOutput: (key, value) => {{ out[key] = String(value); }},
  notice: message => notices.push(message),
  warning: message => warnings.push(message),
}};
const context = {{repo: {{owner: 'kodmial', repo: 'continuum'}},
  payload: {{pull_request: {{number: {PR}}}}}}};
const github = {{
  request: {{endpoint: {{DEFAULTS: {{baseUrl: 'https://api.github.com'}}}}}},
  constructor: function () {{
    this.rest = {{
      users: {{getAuthenticated: async () => ({{data: {{login: 'kodmial'}}}})}},
      issues: {{createComment: async args => {{
        posted.push(args.body);
        issueComments.push({{
          id: 8000 + posted.length,
          user: {{login: 'kodmial'}},
          created_at: new Date().toISOString(),
          body: args.body,
        }});
      }}}},
    }};
  }},
  paginate: async endpoint => (await endpoint({{}})).data,
  graphql: async () => ({{repository: {{pullRequest: {{reviewThreads: {{
    nodes: {json.dumps(threads)},
    pageInfo: {{hasNextPage: false, endCursor: null}},
  }}}}}}}}),
  rest: {{
    pulls: {{
      get: async () => ({{data: {{
        review_comments: comments.length,
        comments: issueComments.length,
        head: {{sha: head, ref: 'opencode/issue459',
          repo: {{full_name: 'kodmial/continuum'}}}},
      }}}}),
      listReviewComments: async () => ({{data: comments.slice()}}),
      getReviewComment: async ({{comment_id}}) => ({{
        data: comments.find(comment => comment.id === comment_id),
      }}),
    }},
    git: {{getRef: async () => ({{data: {{object: {{sha: head}}}}}})}},
    issues: {{
      listComments: async () => {{
        if ({str(issue_error).lower()}) throw new Error('issue comments unavailable');
        return {{data: issueComments.slice()}};
      }},
    }},
    actions: {{
      getWorkflowRun: async () => {{
        if ({str(runs_error).lower()}) throw new Error('run unavailable');
        return {{data: {json.dumps(run or base_run())}}};
      }},
      listJobsForWorkflowRun: async () => ({{
        data: {json.dumps(jobs if jobs is not None else base_jobs())},
      }}),
      listWorkflowRunArtifacts: async () => {{
        if ({str(artifacts_error).lower()}) throw new Error('artifacts unavailable');
        return {{data: {{artifacts: {json.dumps(artifacts)}}}}};
      }},
    }},
  }},
}};
"""
    script = (
        fixture
        + "\nasync function run(){\n"
        + textwrap.indent(source, "  ")
        + "\n}\nrun().then(() => process.stdout.write(JSON.stringify({out, posted, notices, warnings})))\n"
        + ".catch(error => { console.error(error); process.exit(1); });\n"
    )
    return _node(script)


def dispatch_step(items, issue_comment_rounds, review_comments=None, head=HEAD,
                  cutoff=None, run=None, jobs=None, runs_error=False,
                  issue_error_rounds=(), caller="batch", run_rounds=None,
                  artifacts=None, artifacts_error=False):
    source = js_for_step(BATCHER, "Dispatch one batched repair to canonical OpenCode")
    review_comments = (
        base_review_comments(head=head) if review_comments is None
        else review_comments
    )
    run_rounds = run_rounds or [run or base_run()]
    artifacts = (
        provenance_artifacts(*issue_comment_rounds)
        if artifacts is None else artifacts
    )
    caller_source = (
        "on:\n  workflow_dispatch:\n    inputs:\n      unresolved_items_json:\n"
        if caller == "batch"
        else "on:\n  workflow_dispatch:\n    inputs:\n      review_id:\n"
    )
    fixture = f"""
const crypto = require('crypto');
const head = {json.dumps(head)};
const items = {json.dumps(items)};
const rounds = {json.dumps(issue_comment_rounds)};
const runRounds = {json.dumps(run_rounds)};
let runRound = 0;
let round = 0;
const posted = [];
const dispatches = [];
const notices = [];
const warnings = [];
const reviewComments = {json.dumps(review_comments)};
const core = {{
  setOutput: () => {{}},
  notice: message => notices.push(message),
  warning: message => warnings.push(message),
}};
const context = {{repo: {{owner: 'kodmial', repo: 'continuum'}},
  payload: {{repository: {{default_branch: 'main'}}}}}};
process.env.PR_NUMBER = {json.dumps(str(PR))};
process.env.HEAD_REF = 'opencode/issue459';
process.env.COLLECTED_HEAD = head;
process.env.HANDOFF_CUTOFF = {json.dumps(str(cutoff if cutoff is not None else 100000))};
process.env.UNRESOLVED_ITEMS_JSON = JSON.stringify(items);
process.env.CONTINUUM_OPENCODE_WORKFLOW = 'opencode.yml';
const paginate = async () => {{
  const value = rounds[Math.min(round, rounds.length - 1)] || [];
  round += 1;
  if (value === 'ERROR') throw new Error('issue comments unavailable');
  return value.slice();
}};
const github = {{
  paginate,
  rest: {{
    pulls: {{
      get: async () => ({{data: {{
        state: 'open',
        head: {{sha: head, ref: 'opencode/issue459',
          repo: {{full_name: 'kodmial/continuum'}}}},
      }}}}),
      getReviewComment: async ({{comment_id}}) => ({{
        data: reviewComments.find(comment => comment.id === comment_id),
      }}),
    }},
    users: {{getAuthenticated: async () => ({{data: {{login: 'kodmial'}}}})}},
    actions: {{
      listWorkflowRuns: async () => ({{data: {{workflow_runs: []}}}}),
      createWorkflowDispatch: async args => dispatches.push(args),
      getWorkflowRun: async () => {{
        if ({str(runs_error).lower()}) throw new Error('run unavailable');
        const value = runRounds[Math.min(runRound, runRounds.length - 1)];
        runRound += 1;
        return {{data: value}};
      }},
      listJobsForWorkflowRun: async () => ({{
        data: {json.dumps(jobs if jobs is not None else base_jobs())},
      }}),
      listWorkflowRunArtifacts: async () => {{
        if ({str(artifacts_error).lower()}) throw new Error('artifacts unavailable');
        return {{data: {{artifacts: {json.dumps(artifacts)}}}}};
      }},
    }},
    repos: {{getContent: async () => ({{data: {{
      content: Buffer.from({json.dumps(caller_source)}).toString('base64'),
    }}}})}},
    issues: {{
      createComment: async args => posted.push(args.body),
    }},
  }},
}};
"""
    script = (
        fixture
        + "\nasync function run(){\n"
        + textwrap.indent(source, "  ")
        + "\n}\nrun().then(() => process.stdout.write(JSON.stringify({posted, dispatches, notices, warnings})))\n"
        + ".catch(error => { console.error(error); process.exit(1); });\n"
    )
    return _node(script)


def handoff_fixture(changed=False, head=HEAD, inspected=None, run_id=RUN_ID,
                    attempt=1, findings=(FINDING,), answers=(9001,),
                    review=REVIEW, comment_id=950,
                    created_at="2026-11-16T20:20:00Z", author="kodmial"):
    handoff = run_handoff(
        "false" if changed is False else ("true" if changed is True else changed),
        head=head, inspected=inspected, run_id=run_id, attempt=attempt,
        findings=findings, answers=answers, review=review,
    )
    assert handoff["posted"], handoff
    body = handoff["posted"][0]
    comment = {
        "id": comment_id,
        "user": {"login": author},
        "created_at": created_at,
        "body": body,
    }
    return comment, body


def batch_marker(head=HEAD, digest="0" * 64, comment_id=500,
                 created_at="2026-11-16T20:00:00Z", author="kodmial"):
    return {
        "id": comment_id,
        "user": {"login": author},
        "created_at": created_at,
        "body": f"<!-- continuum-coderabbit-batch head={head} digest={digest} -->",
    }


def items_for(finding=FINDING, review=REVIEW, reply=REPLY_ID, head=HEAD):
    return [{
        "review_id": review,
        "finding_id": finding,
        "unresolved_id": reply,
        "verified_head": head,
    }]


class NoChangeHandoffContractTests(unittest.TestCase):
    def test_handoff_binds_no_change_to_run_repo_pr_head_findings_and_digest(self):
        result = run_handoff("false")
        self.assertEqual(len(result["posted"]), 1)
        body = result["posted"][0]
        marker = outcome_marker(body)
        self.assertIn("opencode-coderabbit-verification-pending", body)
        self.assertIn("v=1", marker)
        self.assertIn("repo=kodmial/continuum", marker)
        self.assertIn(f"pr={PR}", marker)
        self.assertIn(f"head={HEAD}", marker)
        self.assertIn(f"findings={FINDING}", marker)
        self.assertIn(f"run={RUN_ID}", marker)
        self.assertIn("attempt=1", marker)
        self.assertIn("changed=false", marker)
        self.assertRegex(marker, r"digest=[0-9a-f]{64}")
        self.assertIn("no-change-awaiting-review-or-evidence", body)
        self.assertIn("not approval or successful qualification", body)
        self.assertIn("⚡ **Continuum · opencode**", body)
        self.assertIn(
            "<!-- continuum-origin role=continuum component=opencode -->", body
        )

    def test_handoff_deduplicates_only_the_same_run_and_upgrades_legacy(self):
        first = run_handoff("false")
        body = first["posted"][0]
        duplicate = run_handoff("false", existing=[{
            "id": 950,
            "user": {"login": "kodmial"},
            "created_at": "2026-11-16T20:20:00Z",
            "body": body,
        }])
        self.assertEqual(duplicate["posted"], [])
        legacy = run_handoff("false", existing=[{
            "id": 951,
            "user": {"login": "kodmial"},
            "created_at": "2026-11-16T20:20:00Z",
            "body": (
                "<!-- opencode-coderabbit-verification-pending "
                f"head={HEAD} findings={FINDING} answers=9001 -->"
            ),
        }])
        self.assertEqual(len(legacy["posted"]), 1)
        self.assertIn("changed=false", legacy["posted"][0])

    def test_changed_true_handoff_never_claims_no_change(self):
        result = run_handoff("true")
        marker = outcome_marker(result["posted"][0])
        self.assertIn("changed=true", marker)
        self.assertNotIn("no-change-awaiting-review-or-evidence",
                         result["posted"][0])


class NoChangeGuardTests(unittest.TestCase):
    def ready_state(self, changed=False, **kwargs):
        handoff, body = handoff_fixture(changed=changed, **kwargs)
        digest = outcome_digest(body)
        return [batch_marker(digest=digest), handoff], handoff, digest

    def test_first_genuine_finding_dispatches_once(self):
        collected = collect_step(items_for(), [])
        self.assertEqual(collected["out"]["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in json.loads(collected["out"]["items_json"])],
            [FINDING],
        )
        dispatched = dispatch_step(items_for(), [[]])
        self.assertEqual(len(dispatched["dispatches"]), 1)
        self.assertEqual(
            dispatched["dispatches"][0]["inputs"]["mode"], "coderabbit-fix"
        )

    def test_collection_suppresses_same_head_after_new_reviewer_replies(self):
        issue_comments, handoff, _ = self.ready_state()
        comments = base_review_comments(latest_reply_id=REPLY_ID)
        collected = collect_step(
            items_for(reply=REPLY_ID), issue_comments, review_comments=comments
        )
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertEqual(json.loads(collected["out"]["items_json"]), [])
        guarded = json.loads(collected["out"]["no_change_json"])
        self.assertEqual(guarded[0]["finding_id"], FINDING)
        self.assertEqual(guarded[0]["run_id"], RUN_ID)
        self.assertEqual(
            guarded[0]["reason"], "no-change-awaiting-review-or-evidence"
        )
        self.assertEqual(len(collected["posted"]), 1)
        reason = collected["posted"][0]
        self.assertIn("continuum-coderabbit-no-change-guard", reason)
        self.assertIn("no-change-awaiting-review-or-evidence", reason)
        self.assertIn("review stays open and blocking", reason)

    def test_dispatch_suppresses_after_backoff_and_later_comment_ids(self):
        issue_comments, _, _ = self.ready_state()
        result = dispatch_step(
            items_for(reply=REPLY_ID), [issue_comments],
            review_comments=base_review_comments(latest_reply_id=REPLY_ID),
            cutoff=100000,
        )
        self.assertEqual(len(result["dispatches"]), 0)
        self.assertEqual(len(result["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence", result["posted"][0])
        self.assertTrue(any("verified no-change repair" in notice
                            for notice in result["notices"]), result["notices"])

    def test_completion_published_between_collection_and_dispatch_is_not_duplicated(self):
        issue_comments, _, _ = self.ready_state()
        result = dispatch_step(
            items_for(), [[], [], issue_comments], cutoff=500,
        )
        self.assertEqual(len(result["dispatches"]), 0)
        self.assertEqual(len(result["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence", result["posted"][0])
        self.assertTrue(any("between collection and dispatch" in notice
                            for notice in result["notices"]), result["notices"])

    def test_changed_head_reenters_the_normal_repair_path(self):
        old_head = "b" * 40
        issue_comments, _, _ = self.ready_state(head=old_head)
        new_comments = base_review_comments(head=HEAD)
        collected = collect_step(items_for(head=HEAD), issue_comments,
                                 review_comments=new_comments, head=HEAD)
        self.assertEqual(collected["out"]["should_run"], "true")
        dispatched = dispatch_step(
            items_for(head=HEAD), [issue_comments],
            review_comments=new_comments, cutoff=100000,
        )
        self.assertEqual(len(dispatched["dispatches"]), 1)

    def test_genuinely_new_original_finding_dispatches_normally(self):
        issue_comments, _, _ = self.ready_state()
        new_finding = 4888000111
        comments = base_review_comments() + [
            run_review_comment(new_finding, "A different defect.",
                               review_id=REVIEW + 1),
            run_review_comment(
                new_finding + 1,
                f"<!-- opencode-coderabbit-verification review={REVIEW + 1} "
                f"finding={new_finding} head={HEAD} -->",
                in_reply_to_id=new_finding, author="kodmial",
                created_at="2026-11-16T20:35:00Z",
            ),
            run_review_comment(
                new_finding + 2,
                "**UNRESOLVED**\\nThis new defect remains.",
                in_reply_to_id=new_finding, created_at="2026-11-16T20:40:00Z",
            ),
        ]
        threads = [
            {"isResolved": False, "comments": {"nodes": [
                {"databaseId": FINDING, "author": {"login": "coderabbitai[bot]"}}
            ]}},
            {"isResolved": False, "comments": {"nodes": [
                {"databaseId": new_finding,
                 "author": {"login": "coderabbitai[bot]"}}
            ]}},
        ]
        collected = collect_step(
            items_for(finding=new_finding, review=REVIEW + 1,
                      reply=new_finding + 2),
            issue_comments, review_comments=comments, threads=threads,
        )
        self.assertEqual(collected["out"]["should_run"], "true")
        self.assertEqual(
            [item["finding_id"] for item in json.loads(collected["out"]["items_json"])],
            [new_finding],
        )

    def test_changed_true_repair_follows_existing_verification_flow(self):
        issue_comments, _, _ = self.ready_state(changed=True)
        result = dispatch_step(items_for(), [issue_comments], cutoff=100000)
        self.assertEqual(len(result["dispatches"]), 1)
        # A changed repair never claims no change: the batch enqueue marker is
        # present, but no no-change guard comment is published.
        self.assertEqual(len(result["posted"]), 1)
        self.assertNotIn("continuum-coderabbit-no-change-guard",
                         result["posted"][0])
        self.assertNotIn("no-change-awaiting-review-or-evidence",
                         result["posted"][0])

    def test_ci_attempt_retry_run_still_corroborates_no_change(self):
        issue_comments, _, _ = self.ready_state()
        result = dispatch_step(
            items_for(), [issue_comments], cutoff=100000,
            run=base_run(
                title=f"OpenCode repair PR #{PR} "
                      "[ci-attempt:2026-11-16T20:05:00Z]"
            ),
        )
        self.assertEqual(len(result["dispatches"]), 0)
        self.assertEqual(len(result["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence", result["posted"][0])

    def test_ref_qualified_workflow_path_still_corroborates_no_change(self):
        # The Actions API may report `run.path` with an `@<ref>` suffix; that
        # must not be mistaken for a different workflow.
        issue_comments, _, _ = self.ready_state()
        qualified = base_run(
            path=".github/workflows/opencode.yml@refs/heads/main"
        )
        collected = collect_step(
            items_for(), issue_comments, run=qualified,
        )
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertNotIn("no_change_deferred_json", collected["out"])
        self.assertEqual(
            [entry["finding_id"]
             for entry in json.loads(collected["out"]["no_change_json"])],
            [FINDING],
        )
        dispatched = dispatch_step(
            items_for(), [issue_comments], cutoff=100000, run=qualified,
        )
        self.assertEqual(len(dispatched["dispatches"]), 0)
        self.assertEqual(len(dispatched["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence",
                      dispatched["posted"][0])

    def test_handoff_publishes_run_scoped_provenance_identity(self):
        # The repair run must publish the identity the no-change guard can
        # only obtain from run-scoped evidence: the inspected HEAD, the
        # finding digest, the attempt, and the published outcome.
        result = run_handoff("false")
        digest = outcome_digest(result["posted"][0])
        self.assertEqual(
            result["out"].get("provenance"),
            f"{HEAD}-{digest}-1-false",
        )
        changed = run_handoff("true")
        changed_digest = outcome_digest(changed["posted"][0])
        self.assertEqual(
            changed["out"].get("provenance"),
            f"{HEAD}-{changed_digest}-1-true",
        )

    def test_missing_expired_or_mismatched_provenance_artifact_defers(self):
        issue_comments, _, digest = self.ready_state()
        expected = provenance_artifacts(issue_comments)[0]["name"]
        # Timestamp the verification request after the handoff so the
        # pending-handoff rule does not mask the provenance decision.
        comments = base_review_comments()
        comments[1] = run_review_comment(
            FINDING + 1,
            f"<!-- opencode-coderabbit-verification review={REVIEW} "
            f"finding={FINDING} head={HEAD} -->",
            in_reply_to_id=FINDING, author="kodmial",
            created_at="2026-11-16T20:25:00Z",
        )
        cases = {
            "missing": [],
            "expired": [{"id": 1, "name": expected, "expired": True}],
            "stale-digest": [
                {"id": 1, "name": expected.replace(digest, "f" * 64),
                 "expired": False},
            ],
            "other-head": [
                {"id": 1, "name": expected.replace(HEAD, "b" * 40),
                 "expired": False},
            ],
            "changed-outcome": [
                {"id": 1, "name": expected[:-len("false")] + "true",
                 "expired": False},
            ],
        }
        # Baseline: the matching artifact is the only difference between the
        # verified no-change suppression and the cases below.
        matched = dispatch_step(
            items_for(), [issue_comments], review_comments=comments,
            cutoff=100000,
        )
        self.assertEqual(len(matched["dispatches"]), 0)
        self.assertEqual(len(matched["posted"]), 1)
        self.assertIn(
            "no-change-awaiting-review-or-evidence", matched["posted"][0]
        )
        for name, artifacts in cases.items():
            with self.subTest(case=name):
                collected = collect_step(
                    items_for(), issue_comments, review_comments=comments,
                    artifacts=artifacts,
                )
                self.assertEqual(collected["out"]["should_run"], "false")
                self.assertEqual(
                    json.loads(collected["out"]["items_json"]), []
                )
                deferred = json.loads(
                    collected["out"]["no_change_deferred_json"]
                )
                self.assertEqual(deferred[0]["finding_id"], FINDING)
                self.assertEqual(
                    deferred[0]["reason"], "run-provenance-unverified"
                )
                self.assertTrue(any(
                    "outcome provenance is unreadable or inconsistent"
                    in notice
                    for notice in collected["notices"]
                ), collected["notices"])
                self.assertTrue(any(
                    "run-provenance-unverified" in notice
                    for notice in collected["notices"]
                ), collected["notices"])
                dispatched = dispatch_step(
                    items_for(), [issue_comments], review_comments=comments,
                    cutoff=100000, artifacts=artifacts,
                )
                self.assertEqual(len(dispatched["dispatches"]), 0)
                self.assertEqual(dispatched["posted"], [])
                self.assertTrue(any(
                    "outcome provenance is unreadable or inconsistent"
                    in notice
                    for notice in dispatched["notices"]
                ), dispatched["notices"])
                self.assertTrue(any(
                    "run-provenance-unverified" in notice
                    for notice in dispatched["notices"]
                ), dispatched["notices"])
        # A temporarily unreadable artifact list defers instead of suppressing
        # or dispatching a duplicate repair.
        unreadable = dispatch_step(
            items_for(), [issue_comments], review_comments=comments,
            cutoff=100000, artifacts_error=True,
        )
        self.assertEqual(len(unreadable["dispatches"]), 0)
        self.assertEqual(unreadable["posted"], [])

    def test_run_scoped_outcome_verifies_without_a_dispatch_lease(self):
        # An ordinary full-review repair publishes its run-scoped outcome
        # without a `continuum-coderabbit-batch` lease. The batch lease is
        # corroboration, so the verified artifact must still suppress.
        handoff, _ = handoff_fixture()
        collected = collect_step(items_for(), [handoff])
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertNotIn("no_change_deferred_json", collected["out"])
        self.assertEqual(
            [entry["finding_id"]
             for entry in json.loads(collected["out"]["no_change_json"])],
            [FINDING],
        )
        dispatched = dispatch_step(items_for(), [[handoff]], cutoff=100000)
        self.assertEqual(len(dispatched["dispatches"]), 0, dispatched["notices"])
        self.assertEqual(len(dispatched["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence",
                      dispatched["posted"][0])

    def test_admitted_subset_outcome_reconciles_with_original_batch_lease(self):
        # Executor admission can remove a superseded finding, so the outcome
        # digest covers the admitted subset while the lease covers the original
        # batch. The outcome must reconcile instead of deferring forever.
        handoff, handoff_body = handoff_fixture()
        superseded_finding = FINDING + 3
        original_batch = [[REVIEW, FINDING], [REVIEW, superseded_finding]]
        original_batch_digest = hashlib.sha256(
            (HEAD + "\n" + json.dumps(original_batch)).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(original_batch_digest, outcome_digest(handoff_body))
        issue_comments = [
            batch_marker(digest=original_batch_digest), handoff,
        ]
        collected = collect_step(items_for(), issue_comments)
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertNotIn("no_change_deferred_json", collected["out"])
        self.assertEqual(
            [entry["finding_id"]
             for entry in json.loads(collected["out"]["no_change_json"])],
            [FINDING],
        )
        dispatched = dispatch_step(items_for(), [issue_comments], cutoff=100000)
        self.assertEqual(len(dispatched["dispatches"]), 0, dispatched["notices"])
        self.assertEqual(len(dispatched["posted"]), 1)

    def test_unverified_changed_outcome_defers_instead_of_releasing(self):
        # A changed=true marker must not release the no-change suppression on
        # its bare value: only validated run and artifact evidence may do so.
        handoff, _ = handoff_fixture(changed=True)
        issue_comments = [handoff]
        cases = {
            "missing-provenance": {"artifacts": []},
            "failed-run": {"run": base_run(conclusion="failure")},
            "unreadable-run": {"runs_error": True},
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                collected = collect_step(
                    items_for(), issue_comments, **overrides,
                )
                self.assertEqual(collected["out"]["should_run"], "false")
                self.assertEqual(json.loads(collected["out"]["items_json"]), [])
                deferred = json.loads(
                    collected["out"]["no_change_deferred_json"]
                )
                self.assertEqual(deferred[0]["finding_id"], FINDING)
                self.assertIn(
                    deferred[0]["reason"],
                    ("run-provenance-unverified", "run-not-successful",
                     "run-unreadable"),
                )
                dispatched = dispatch_step(
                    items_for(), [issue_comments], cutoff=100000, **overrides,
                )
                self.assertEqual(len(dispatched["dispatches"]), 0,
                                 dispatched["notices"])
                self.assertEqual(dispatched["posted"], [])

    def test_final_check_refreshes_run_evidence_cached_before_completion(self):
        # The preliminary check caches an in-progress run. The run completes
        # before dispatch, so the final check must re-read it and suppress the
        # duplicate instead of trusting the stale response.
        issue_comments, _, _ = self.ready_state()
        result = dispatch_step(
            items_for(),
            [issue_comments, issue_comments, issue_comments],
            cutoff=100000,
            run_rounds=[
                base_run(status="in_progress", conclusion=None),
                base_run(),
            ],
        )
        self.assertEqual(len(result["dispatches"]), 0, result["notices"])
        self.assertEqual(len(result["posted"]), 1)
        self.assertIn("no-change-awaiting-review-or-evidence",
                      result["posted"][0])
        self.assertTrue(any("between collection and dispatch" in notice
                            for notice in result["notices"]),
                        result["notices"])

    def test_failed_cancelled_timed_out_and_queued_runs_do_not_guard(self):
        cases = [
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", "timed_out"),
            ("queued", None),
            ("in_progress", None),
        ]
        for status, conclusion in cases:
            with self.subTest(status=status, conclusion=conclusion):
                issue_comments, _, _ = self.ready_state()
                result = dispatch_step(
                    items_for(), [issue_comments], cutoff=100000,
                    run=base_run(status=status, conclusion=conclusion),
                )
                self.assertEqual(len(result["dispatches"]), 1)

    def test_jobless_and_admission_only_runs_do_not_guard(self):
        issue_comments, _, _ = self.ready_state()
        jobless = dispatch_step(
            items_for(), [issue_comments], cutoff=100000,
            jobs={"jobs": []},
        )
        self.assertEqual(len(jobless["dispatches"]), 1)
        admission = dispatch_step(
            items_for(), [issue_comments], cutoff=100000,
            jobs=base_jobs(repair_conclusion="skipped"),
        )
        self.assertEqual(len(admission["dispatches"]), 1)

    def test_forged_untrusted_outcome_never_suppresses(self):
        _, body = handoff_fixture()
        digest = outcome_digest(body)
        forged = {
            "id": 950,
            "user": {"login": "stranger"},
            "created_at": "2026-11-16T20:20:00Z",
            "body": body,
        }
        collected = collect_step(
            items_for(), [batch_marker(digest=digest), forged]
        )
        self.assertEqual(collected["out"]["should_run"], "true")
        self.assertEqual(collected["posted"], [])
        dispatched = dispatch_step(
            items_for(), [[batch_marker(digest=digest), forged]],
            cutoff=100000,
        )
        self.assertEqual(len(dispatched["dispatches"]), 1)

    def test_wrong_run_pr_or_digest_defers_instead_of_forging_a_pass(self):
        issue_comments, handoff, _ = self.ready_state()
        cases = {
            "wrong-pr": base_run(title=f"OpenCode repair PR #{PR + 1}"),
            "wrong-run": base_run(run_id=RUN_ID + 1),
            "wrong-workflow": base_run(path=".github/workflows/other.yml"),
            "wrong-attempt": base_run(attempt=2),
        }
        for name, run in cases.items():
            with self.subTest(case=name):
                result = dispatch_step(
                    items_for(), [issue_comments], cutoff=100000, run=run,
                )
                self.assertEqual(len(result["dispatches"]), 0, result["notices"])
                self.assertEqual(result["posted"], [])

        mismatch = {
            "id": handoff["id"],
            "user": handoff["user"],
            "created_at": handoff["created_at"],
            "body": re.sub(r"digest=[0-9a-f]{64}", "digest=" + "f" * 64,
                           handoff["body"]),
        }
        result = dispatch_step(
            items_for(), [[issue_comments[0], mismatch]], cutoff=100000,
        )
        self.assertEqual(len(result["dispatches"]), 0)

    def test_missing_or_unreadable_provenance_defers_safely(self):
        issue_comments, handoff, _ = self.ready_state()
        without_run = {
            "id": handoff["id"],
            "user": handoff["user"],
            "created_at": handoff["created_at"],
            "body": re.sub(r"\srun=[0-9]+", "", handoff["body"]),
        }
        missing = dispatch_step(
            items_for(), [[issue_comments[0], without_run]], cutoff=100000,
        )
        self.assertEqual(len(missing["dispatches"]), 0)
        unreadable = dispatch_step(
            items_for(), [issue_comments], cutoff=100000, runs_error=True,
        )
        self.assertEqual(len(unreadable["dispatches"]), 0)
        comments_unreadable = dispatch_step(
            items_for(), ["ERROR"], cutoff=100000,
        )
        self.assertEqual(len(comments_unreadable["dispatches"]), 0)
        final_unreadable = dispatch_step(
            items_for(), [[], [], "ERROR"], cutoff=100000,
        )
        self.assertEqual(len(final_unreadable["dispatches"]), 0)

    def test_wrong_head_outcome_is_stale_not_a_guard(self):
        issue_comments, _, _ = self.ready_state(head="b" * 40)
        # A no-change outcome for an older commit must never suppress the
        # current HEAD; a stale outcome is simply not a guard.
        advanced = dispatch_step(
            items_for(head=HEAD), [issue_comments], cutoff=100000, head=HEAD,
            review_comments=base_review_comments(head=HEAD),
        )
        self.assertEqual(len(advanced["dispatches"]), 1)

    def test_outcome_binds_inspected_head_not_a_concurrent_branch_advance(self):
        inspected = "b" * 40
        # The repair inspected `inspected`; another trusted writer advanced the
        # branch to HEAD before the handoff step read the live ref.
        handoff, body = handoff_fixture(head=HEAD, inspected=inspected)
        marker = outcome_marker(body)
        self.assertIn(f"head={inspected}", marker)
        self.assertNotIn(f"head={HEAD}", marker)
        # The legacy pending marker still follows the live published HEAD.
        self.assertIn(f"head={HEAD}", body)
        self.assertNotIn(f"head={inspected}", body.split(marker, 1)[1])
        issue_comments = [
            batch_marker(head=inspected, digest=outcome_digest(body)),
            handoff,
        ]
        result = dispatch_step(
            items_for(head=HEAD), [issue_comments], cutoff=100000, head=HEAD,
            review_comments=base_review_comments(head=HEAD),
        )
        # The outcome never inspected HEAD, so it must not suppress the repair
        # the advanced branch still needs.
        self.assertEqual(len(result["dispatches"]), 1)
        self.assertEqual(len(result["posted"]), 1)
        self.assertIn("Queued one canonical OpenCode repair", result["posted"][0])
        self.assertNotIn("continuum-coderabbit-no-change-guard",
                         result["posted"][0])

    def test_collection_read_failure_defers_even_with_actionable_finding(self):
        collected = collect_step(
            items_for(), [], issue_error=True,
        )
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertEqual(json.loads(collected["out"]["items_json"]), [])
        self.assertTrue(any("could not read CodeRabbit repair handoffs" in warning
                            for warning in collected["warnings"]),
                        collected["warnings"])

    def test_no_change_outcome_check_read_failure_defers(self):
        issue_comments, _, _ = self.ready_state()
        collected = collect_step(
            items_for(), issue_comments, runs_error=True,
        )
        self.assertEqual(collected["out"]["should_run"], "false")
        self.assertEqual(json.loads(collected["out"]["items_json"]), [])


if __name__ == "__main__":
    unittest.main()
