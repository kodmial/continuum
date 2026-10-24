"""Executable regression tests for 429 observed-run liveness (kodmial/continuum#407).

The tests extract the *checked-in production* github-script body from
``.github/workflows/continuum-opencode-watchdog.yml`` and execute it under Node
with mocked GitHub clients. They reproduce issue #382 exactly: an old
``observed`` marker whose run completed with failure at an older checkpoint must
not suppress a fresh dispatch from the newer, metadata-verified checkpoint.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"
DISPATCH_HELPER = ROOT / ".github" / "scripts" / "opencode_429_dispatch.js"

OWNER = "kodmial"
REPO = "continuum"
OPERATION = "opencode-429-issue-issue382"
CHECKPOINT_REF = (
    "opencode/429-checkpoint-opencode-429-issue-issue382-gen-1"
)
OLD_SHA = "866301983c084f9d56eb3155066408af8006b361"
NEW_SHA = "17f88cfcb02c122defc4840b73e316d96a426e03"
OLD_RUN_ID = "39885792009"
NEW_RUN_ID = "39933043642"
BRANCH = "opencode/issue382-429-recovery"
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"

NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const resultPath = process.argv[4];

const state = {
  comments: (scenario.comments || []).map(comment => ({ ...comment })),
  nextCommentId: scenario.nextCommentId || 100,
  dispatches: [],
  runs: new Map(),
  runErrors: new Map(Object.entries(scenario.runErrors || {})),
  runLookups: [],
  workflowRuns: (scenario.workflowRuns || []).map(run => ({ ...run })),
  issues: (scenario.issues || []).map(issue => ({ ...issue })),
  scriptErrors: [],
  notices: [],
};
for (const run of scenario.runs || []) {
  state.runs.set(String(run.id), { ...run });
  if (!state.workflowRuns.some(item => String(item.id) === String(run.id))) {
    state.workflowRuns.push({ ...run });
  }
}

function apiError(status, message) {
  const error = new Error(message || ('HTTP ' + status));
  error.status = status;
  return error;
}
function nowIso() {
  return new Date().toISOString();
}

function makeRest() {
  return {
    issues: {
      get: async ({ issue_number }) => {
        const issue = state.issues.find(
          item => Number(item.number) === Number(issue_number)
        );
        if (issue) return { data: { ...issue } };
        return {
          data: {
            number: issue_number,
            state: 'open',
            labels: [],
            title: 'Issue ' + issue_number,
          },
        };
      },
      listForRepo: async (params = {}) => {
        let items = state.issues.slice();
        if (params.state) {
          items = items.filter(issue => issue.state === params.state);
        }
        const label = String(params.labels || '').trim();
        if (label) {
          items = items.filter(issue =>
            (issue.labels || []).some(item =>
              (typeof item === 'string' ? item : item.name) === label
            )
          );
        }
        return items.map(issue => ({ ...issue }));
      },
      listComments: async () => state.comments.map(comment => ({ ...comment })),
      createComment: async ({ issue_number, body }) => {
        const comment = {
          id: state.nextCommentId++,
          body,
          issue_number,
          user: { login: scenario.actorLogin || 'github-actions[bot]' },
          author_association: scenario.actorAssociation || 'OWNER',
          created_at: nowIso(),
          updated_at: nowIso(),
        };
        state.comments.push(comment);
        return { data: { ...comment } };
      },
      updateComment: async ({ comment_id, body }) => {
        const comment = state.comments.find(item => Number(item.id) === Number(comment_id));
        if (!comment) throw apiError(404, 'comment not found');
        comment.body = body;
        comment.updated_at = nowIso();
        return { data: { ...comment } };
      },
      deleteComment: async ({ comment_id }) => {
        state.comments = state.comments.filter(
          item => Number(item.id) !== Number(comment_id)
        );
        return { data: {} };
      },
      addLabels: async () => ({ data: {} }),
      removeLabel: async () => ({ data: {} }),
      createLabel: async () => ({ data: {} }),
    },
    pulls: {
      list: async () => (scenario.pulls || []).map(pull => ({ ...pull })),
      get: async ({ pull_number }) => {
        const pull = (scenario.pulls || []).find(
          item => Number(item.number) === Number(pull_number)
        );
        if (!pull) throw apiError(404, 'pull not found');
        return { data: { ...pull } };
      },
    },
    actions: {
      listWorkflowRuns: async () => state.workflowRuns.map(run => ({ ...run })),
      listWorkflowRunsForRepo: async (params = {}) => {
        if (params.status && scenario.activeRunsError) {
          throw apiError(scenario.activeRunsError, 'active run listing failed');
        }
        let items = state.workflowRuns.slice();
        if (params.status) {
          items = items.filter(run => run.status === params.status);
        }
        const perPage = Number(params.per_page || 30);
        const page = Number(params.page || 1);
        const start = (page - 1) * perPage;
        return {
          data: {
            workflow_runs: items
              .slice(start, start + perPage)
              .map(run => ({ ...run })),
          },
        };
      },
      listJobsForWorkflowRun: async () =>
        (scenario.jobs || []).map(job => ({ ...job })),
      getWorkflowRun: async ({ run_id }) => {
        const key = String(run_id);
        state.runLookups.push(key);
        if (state.runErrors.has(key)) {
          throw apiError(state.runErrors.get(key), 'run lookup failed');
        }
        const run = state.runs.get(key);
        if (!run) throw apiError(404, 'run not found');
        return { data: { ...run } };
      },
      reRunWorkflow: async () => ({ data: {} }),
      createWorkflowDispatch: async ({ workflow_id, ref, inputs }) => {
        const identity = JSON.parse(inputs.recovery_identity || '{}');
        const runId = Number(scenario.dispatchedRunId || 5001);
        state.dispatches.push({ workflow_id, ref, inputs });
        const run = {
          id: runId,
          event: 'workflow_dispatch',
          status: scenario.dispatchedRunStatus || 'in_progress',
          conclusion: null,
          path: scenario.dispatchPath || '.github/workflows/opencode.yml',
          name: 'OpenCode agent',
          display_title:
            'OpenCode 429 recovery ' +
            identity.recovery_operation_id + ' gen ' +
            identity.recovery_generation,
          created_at: nowIso(),
          run_started_at: nowIso(),
        };
        state.runs.set(String(runId), run);
        state.workflowRuns.push(run);
        return { data: {} };
      },
    },
    git: {
      getRef: async () => {
        if (scenario.checkpoint && scenario.checkpoint.missingRef) {
          throw apiError(404, 'ref not found');
        }
        return {
          data: {
            object: {
              sha: scenario.checkpoint ? scenario.checkpoint.refSha : '',
            },
          },
        };
      },
    },
    repos: {
      get: async () => ({ data: { default_branch: 'main' } }),
      getContent: async ({ path }) => {
        if (path === '.continuum-429-recovery.json') {
          if (!scenario.checkpoint || scenario.checkpoint.missingMetadata) {
            throw apiError(404, 'metadata not found');
          }
          const metadata = scenario.checkpoint.metadata;
          const text = typeof metadata === 'string'
            ? metadata
            : JSON.stringify(metadata);
          return {
            data: {
              type: 'file',
              content: Buffer.from(text, 'utf8').toString('base64'),
            },
          };
        }
        const files = scenario.workflowFiles || {};
        if (Object.prototype.hasOwnProperty.call(files, path)) {
          return {
            data: {
              type: 'file',
              content: Buffer.from(files[path], 'utf8').toString('base64'),
            },
          };
        }
        throw apiError(404, 'file not found');
      },
    },
  };
}

class MockGithub {
  constructor() {
    this.rest = makeRest();
    this.request = {
      endpoint: { DEFAULTS: { baseUrl: 'https://api.github.com' } },
    };
    this.paginate = async (method, params) => {
      const result = await method(params);
      if (
        result && result.data &&
        Array.isArray(result.data.workflow_runs)
      ) {
        return result.data.workflow_runs;
      }
      return result;
    };
  }
}

function mockFetch(url) {
  if (/\/actions\/jobs\/[^/]+\/logs$/.test(String(url))) {
    return Promise.resolve({
      ok: true,
      status: 200,
      text: async () => scenario.jobLog || '',
    });
  }
  return Promise.resolve({
    ok: false,
    status: 404,
    text: async () => '',
  });
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

async function runOnce() {
  const core = {
    notice: (...args) => state.notices.push(['notice', ...args.map(String)]),
    warning: (...args) => state.notices.push(['warning', ...args.map(String)]),
    info: (...args) => state.notices.push(['info', ...args.map(String)]),
    error: (...args) => state.notices.push(['error', ...args.map(String)]),
    setFailed: (...args) => state.notices.push(['failed', ...args.map(String)]),
  };
  const context = {
    repo: { owner: scenario.owner || 'kodmial', repo: scenario.repo || 'continuum' },
    payload: { workflow_run: scenario.triggerRun || null },
  };
  const fn = new AsyncFunction(
    'github', 'context', 'core', 'fetch', 'require', 'process', 'Buffer',
    'setTimeout', source
  );
  try {
    await fn(
      new MockGithub(), context, core, mockFetch, require, process, Buffer,
      (callback) => setTimeout(callback, 0)
    );
  } catch (error) {
    state.scriptErrors.push(String(error && error.stack || error));
  }
}

(async () => {
  const passes = Number(scenario.passes || 1);
  const concurrent = Boolean(scenario.concurrent);
  if (concurrent) {
    await Promise.all(Array.from({ length: passes }, () => runOnce()));
  } else {
    for (let index = 0; index < passes; index += 1) {
      await runOnce();
    }
  }
  const result = {
    dispatches: state.dispatches,
    comments: state.comments,
    notices: state.notices,
    runLookups: state.runLookups,
    scriptErrors: state.scriptErrors,
  };
  fs.writeFileSync(resultPath, JSON.stringify(result, null, 2), 'utf8');
})().catch(error => {
  fs.writeFileSync(
    resultPath,
    JSON.stringify({ harnessError: String(error && error.stack || error) }),
    'utf8'
  );
  process.exitCode = 1;
});
"""


def watchdog_script() -> str:
    data = yaml.safe_load(WATCHDOG.read_text(encoding="utf-8"))
    for step in data["jobs"]["recover"]["steps"]:
        script = (step.get("with") or {}).get("script")
        if script and "recoverFrom429OnFreshVm" in script:
            return script
    raise AssertionError("production watchdog script not found")


def recovery_marker(state, sha, run_id, attempt=1, next_due=0):
    return (
        "<!-- continuum-429-recovery op={op} gen=1 state={state} "
        "attempt={attempt} next_due={next_due} checkpoint={ref} "
        "sha={sha} run={run_id} detail=test -->"
    ).format(
        op=OPERATION,
        state=state,
        attempt=attempt,
        next_due=next_due,
        ref=CHECKPOINT_REF,
        sha=sha,
        run_id=run_id,
    )


def metadata(sha=NEW_SHA):
    return {
        "schema_version": 1,
        "operation_id": OPERATION,
        "failure_kind": "quota_429",
        "generation": 1,
        "stage": "tests-passed",
        "mode": "issue",
        "issue_number": "382",
        "pr_number": "",
        "head_ref": BRANCH,
        "base_sha": BASE_SHA,
        "head_sha": sha,
        "branch": BRANCH,
        "checkpoint_ref": CHECKPOINT_REF,
    }


def base_scenario():
    return {
        "owner": OWNER,
        "repo": REPO,
        "comments": [
            {
                "id": 10,
                "body": recovery_marker("observed", OLD_SHA, OLD_RUN_ID),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-10-23T09:30:00Z",
                "updated_at": "2026-10-23T09:30:00Z",
            }
        ],
        "runs": [
            {
                "id": int(OLD_RUN_ID),
                "status": "completed",
                "conclusion": "failure",
                "event": "workflow_dispatch",
                "path": ".github/workflows/opencode.yml",
                "display_title": "OpenCode issue #382",
                "created_at": "2026-10-23T09:20:00Z",
            }
        ],
        "triggerRun": {
            "id": int(NEW_RUN_ID),
            "status": "completed",
            "conclusion": "failure",
            "event": "workflow_dispatch",
            "path": ".github/workflows/opencode.yml",
            "display_title": "OpenCode issue #382",
            "created_at": "2026-10-23T09:57:00Z",
        },
        "jobs": [{"id": 120345823697}],
        "jobLog": (
            "Evacuated 429 checkpoint {ref} at exact SHA {sha} "
            "(operation {op}, stage tests-passed, generation 1, mode issue, "
            "issue 382, pr -, branch {branch})\n"
            "##[error]CONTINUUM_OPENCODE_429_RESTART_REQUIRED\n"
        ).format(ref=CHECKPOINT_REF, sha=NEW_SHA, op=OPERATION, branch=BRANCH),
        "checkpoint": {
            "refSha": NEW_SHA,
            "metadata": metadata(NEW_SHA),
        },
        "workflowFiles": {
            ".github/workflows/opencode.yml": (
                "on:\n"
                "  workflow_dispatch:\n"
                "    inputs:\n"
                "      recovery_identity:\n"
                "        required: false\n"
            ),
        },
        "pulls": [],
    }


class WatchdogHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        cls.source = watchdog_script()

    def run_scenario(self, scenario):
        workdir = Path(tempfile.mkdtemp(prefix="watchdog-", dir=str(TMP_PARENT)))
        try:
            source_path = workdir / "watchdog.js"
            scenario_path = workdir / "scenario.json"
            result_path = workdir / "result.json"
            harness_path = workdir / "harness.js"
            source_path.write_text(self.source, encoding="utf-8")
            scenario_path.write_text(
                json.dumps(scenario, indent=2), encoding="utf-8"
            )
            harness_path.write_text(NODE_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                ["node", str(harness_path), str(source_path),
                 str(scenario_path), str(result_path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env={
                    "PATH": str(Path(shutil.which("node")).parent) + ":/usr/bin:/bin",
                    "CONTINUUM_DISPATCH_HELPER": str(DISPATCH_HELPER),
                    "READ_GITHUB_TOKEN": "test-token",
                    "MAX_429_RUNNER_RESTARTS": "3",
                    "OPENCODE_WORKFLOW": "opencode.yml",
                    "GITHUB_API_URL": "https://api.github.com",
                },
                timeout=120,
            )
            self.assertEqual(
                completed.returncode, 0,
                (completed.stdout or "") + (completed.stderr or ""),
            )
            self.assertTrue(result_path.is_file(), completed.stderr)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertNotIn("harnessError", result, result.get("harnessError"))
            self.assertEqual(result.get("scriptErrors"), [], result.get("notices"))
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def assert_one_current_dispatch(self, result, sha=NEW_SHA):
        self.assertEqual(len(result["dispatches"]), 1, result["dispatches"])
        dispatch = result["dispatches"][0]
        identity = json.loads(dispatch["inputs"]["recovery_identity"])
        self.assertEqual(identity["recovery_operation_id"], OPERATION)
        self.assertEqual(identity["recovery_checkpoint_ref"], CHECKPOINT_REF)
        self.assertEqual(identity["recovery_checkpoint_sha"], sha)
        self.assertEqual(identity["recovery_stage"], "tests-passed")
        self.assertEqual(identity["recovery_generation"], "1")
        return dispatch

    def assert_no_dispatch(self, result):
        self.assertEqual(result["dispatches"], [], result["dispatches"])

    def test_382_old_failed_observed_run_does_not_own_newer_checkpoint(self):
        result = self.run_scenario(base_scenario())
        self.assert_one_current_dispatch(result, NEW_SHA)
        bodies = "\n".join(comment["body"] for comment in result["comments"])
        self.assertIn("sha=" + OLD_SHA, bodies)
        self.assertNotIn(
            json.dumps(result["dispatches"][0]["inputs"]), OLD_SHA
        )
        self.assertTrue(
            any("newer saved work" in " ".join(line) for line in result["notices"]),
            result["notices"],
        )

    def test_two_simultaneous_wakes_dispatch_exactly_once(self):
        scenario = base_scenario()
        scenario["passes"] = 2
        scenario["concurrent"] = True
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)
        observed = [
            comment["body"] for comment in result["comments"]
            if "state=observed" in comment["body"]
        ]
        self.assertEqual(len(observed), 2, observed)
        self.assertTrue(
            any(OLD_SHA in body for body in observed),
            "the historic observed marker must be preserved",
        )
        self.assertTrue(
            any("run=5001" in body for body in observed),
            "the new observed marker must record the real dispatched run id",
        )

    def test_sequential_catch_up_sees_live_recovery_and_does_not_duplicate(self):
        scenario = base_scenario()
        scenario["passes"] = 2
        scenario["dispatchedRunStatus"] = "in_progress"
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)

    def test_active_observed_run_owns_the_work(self):
        scenario = base_scenario()
        scenario["runs"][0]["status"] = "in_progress"
        scenario["runs"][0]["conclusion"] = None
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_successful_observed_run_with_publication_owns_the_work(self):
        scenario = base_scenario()
        scenario["runs"][0]["conclusion"] = "success"
        scenario["pulls"] = [
            {
                "number": 500,
                "state": "closed",
                "merged_at": "2026-10-23T09:40:00Z",
                "head": {
                    "ref": BRANCH,
                    "repo": {"full_name": OWNER + "/" + REPO},
                },
            }
        ]
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_failed_run_that_published_a_pr_is_not_redispatched(self):
        scenario = base_scenario()
        scenario["pulls"] = [
            {
                "number": 500,
                "state": "open",
                "head": {
                    "ref": BRANCH,
                    "repo": {"full_name": OWNER + "/" + REPO},
                },
            }
        ]
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_unchanged_checkpoint_is_not_redispatched(self):
        scenario = base_scenario()
        scenario["checkpoint"]["refSha"] = OLD_SHA
        scenario["checkpoint"]["metadata"] = metadata(OLD_SHA)
        scenario["jobLog"] = scenario["jobLog"].replace(NEW_SHA, OLD_SHA)
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_missing_checkpoint_metadata_fails_closed(self):
        scenario = base_scenario()
        scenario["checkpoint"]["missingMetadata"] = True
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        bodies = "\n".join(comment["body"] for comment in result["comments"])
        self.assertIn("state=metadata-failed", bodies)
        self.assertIn("detail=metadata-missing", bodies)

    def test_invalid_checkpoint_metadata_fails_closed(self):
        scenario = base_scenario()
        broken = metadata(NEW_SHA)
        broken["head_sha"] = "not-a-sha"
        scenario["checkpoint"]["metadata"] = broken
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        bodies = "\n".join(comment["body"] for comment in result["comments"])
        self.assertIn("state=metadata-failed", bodies)
        self.assertIn("detail=metadata-invalid", bodies)

    def test_stale_event_pointer_uses_the_verified_ref_tip(self):
        scenario = base_scenario()
        scenario["jobLog"] = scenario["jobLog"].replace(NEW_SHA, OLD_SHA)
        scenario["checkpoint"]["refSha"] = NEW_SHA
        scenario["checkpoint"]["metadata"] = metadata(NEW_SHA)
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)
        self.assertTrue(
            any("advanced from stale event pointer" in " ".join(line)
                for line in result["notices"]),
            result["notices"],
        )

    def test_unverifiable_run_status_is_a_bounded_retry_not_a_deadlock(self):
        scenario = base_scenario()
        scenario["runErrors"] = {OLD_RUN_ID: 403}
        # Both wakes share one mock state, so the second wake sees the durable
        # observed-unverified marker recorded by the first.
        scenario["passes"] = 2
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        unverified = [
            comment["body"] for comment in result["comments"]
            if "state=observed-unverified" in comment["body"]
        ]
        self.assertEqual(len(unverified), 1, result["comments"])
        self.assertIn("detail=run-status-403", unverified[0])
        self.assertRegex(unverified[0], r"next_due=[1-9][0-9]*")
        self.assertIn("attempt=2", unverified[0])
        # The second wake inside the bounded window must skip dispatch and must
        # not re-read the unverifiable run: the guard alone suppresses it.
        self.assertEqual(
            result["runLookups"].count(OLD_RUN_ID), 1, result["runLookups"]
        )

        # Once the window elapses and the API recovers, the marker is
        # re-verified and the newer checkpoint resumes dispatch instead of
        # deadlocking forever.
        replay = base_scenario()
        replay["comments"] = copy.deepcopy(result["comments"])
        for comment in replay["comments"]:
            if "state=observed-unverified" in comment["body"]:
                comment["body"] = re.sub(
                    r"next_due=\d+", "next_due=0", comment["body"]
                )
        recovered = self.run_scenario(replay)
        self.assert_one_current_dispatch(recovered, NEW_SHA)

    def test_unverified_retry_budget_enters_bounded_backoff(self):
        scenario = base_scenario()
        scenario["comments"][0]["body"] = recovery_marker(
            "observed-unverified", OLD_SHA, OLD_RUN_ID, attempt=4
        )
        scenario["runErrors"] = {OLD_RUN_ID: 403}
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        backoff = [
            comment["body"] for comment in result["comments"]
            if "state=backoff" in comment["body"]
        ]
        self.assertEqual(len(backoff), 1, result["comments"])
        self.assertIn("detail=attempts-exhausted", backoff[0])
        self.assertIn("attempt=5", backoff[0])

    def test_terminal_non_owning_conclusions_allow_newer_checkpoint_resume(self):
        for conclusion in ("cancelled", "skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                scenario = base_scenario()
                scenario["runs"][0]["conclusion"] = conclusion
                result = self.run_scenario(scenario)
                self.assert_one_current_dispatch(result, NEW_SHA)

    def test_transient_run_lookup_404_is_retryable(self):
        scenario = base_scenario()
        scenario["runErrors"] = {OLD_RUN_ID: 404}
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        bodies = "\n".join(comment["body"] for comment in result["comments"])
        self.assertIn("state=observed-unverified", bodies)
        self.assertIn("detail=run-status-404", bodies)

    def test_exhausted_retry_budget_enters_bounded_backoff(self):
        scenario = base_scenario()
        scenario["comments"][0]["body"] = recovery_marker(
            "observed", OLD_SHA, OLD_RUN_ID, attempt=4
        )
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        backoff = [
            comment["body"] for comment in result["comments"]
            if "state=backoff" in comment["body"]
        ]
        self.assertEqual(len(backoff), 1, result["comments"])
        self.assertIn("detail=attempts-exhausted", backoff[0])

    def test_owner_stop_blocks_infrastructure_resumption(self):
        scenario = base_scenario()
        scenario["comments"].append(
            {
                "id": 20,
                "body": (
                    "<!-- continuum-task-operator-stop "
                    "repo=kodmial/continuum issue=382 generation=1 -->"
                ),
                "user": {"login": "kodmial"},
                "author_association": "OWNER",
                "created_at": "2026-10-23T10:00:00Z",
                "updated_at": "2026-10-23T10:00:00Z",
            }
        )
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        self.assertTrue(
            any("owner STOP" in " ".join(line) for line in result["notices"]),
            result["notices"],
        )

    def test_unrelated_issue_and_generation_markers_do_not_fence_this_one(self):
        scenario = base_scenario()
        unrelated_generation = recovery_marker(
            "observed", OLD_SHA, "1111"
        ).replace("gen=1", "gen=2")
        unrelated_issue = (
            "<!-- continuum-429-recovery op=opencode-429-issue-issue373 "
            "gen=1 state=observed attempt=1 next_due=0 "
            "checkpoint=opencode/429-checkpoint-opencode-429-issue-issue373-gen-1 "
            "sha=" + OLD_SHA + " run=2222 detail=test -->"
        )
        for comment_id, body, created_at in (
            (30, unrelated_generation, "2026-10-23T09:31:00Z"),
            (31, unrelated_issue, "2026-10-23T09:32:00Z"),
        ):
            scenario["comments"].append(
                {
                    "id": comment_id,
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                    "author_association": "OWNER",
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)
        bodies = "\n".join(comment["body"] for comment in result["comments"])
        self.assertIn(unrelated_generation, bodies)
        self.assertIn(unrelated_issue, bodies)

    def test_ordinary_pause_guard_stays_in_the_normal_recovery_path(self):
        recovery = self.source.split(
            "async function recoverFrom429OnFreshVm"
        )[-1].split("if (publishRaceRestartRequired)")[0]
        self.assertNotIn("pausedLabel", recovery)
        self.assertIn("issueLabels.has(pausedLabel)", self.source)
        self.assertIn("' is ' + pausedLabel + '; watchdog will not retry it.'",
                      self.source)

    def durable_scenario(self, newer_runs=320):
        scenario = base_scenario()
        scenario["triggerRun"] = None
        scenario["jobs"] = []
        scenario["jobLog"] = ""
        now = datetime.now(timezone.utc)
        old_run = dict(scenario["runs"][0])
        old_run["created_at"] = (
            now - timedelta(hours=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        unrelated = []
        for index in range(newer_runs):
            unrelated.append(
                {
                    "id": 600000 + index,
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "path": ".github/workflows/other.yml",
                    "name": "Other",
                    "display_title": "Other run " + str(index),
                    "created_at": (
                        now - timedelta(minutes=index)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        # The historical run sits beyond the newest 200 repo-wide runs.
        scenario["workflowRuns"] = unrelated + [old_run]
        scenario["issues"] = [
            {
                "number": 382,
                "state": "open",
                "title": "P0: durable recovery",
                "labels": [
                    {"name": "automation:in-progress"},
                    {"name": "automation:ready"},
                ],
            }
        ]
        scenario["dispatchedRunId"] = 5001
        return scenario

    def test_scheduled_wake_rediscovers_old_checkpoint_beyond_recent_200(self):
        result = self.run_scenario(self.durable_scenario())
        self.assert_one_current_dispatch(result, NEW_SHA)
        self.assertIn(OLD_RUN_ID, result["runLookups"])
        observed = [
            comment["body"] for comment in result["comments"]
            if "state=observed" in comment["body"] and
            "run=5001" in comment["body"] and
            "detail=run-started" in comment["body"]
        ]
        self.assertEqual(len(observed), 1, result["comments"])
        self.assertTrue(
            any(
                "durable recovery inspected 1 trusted recovery marker" in
                " ".join(line)
                for line in result["notices"]
            ),
            result["notices"],
        )

    def test_scheduled_two_concurrent_wakes_dispatch_exactly_once(self):
        scenario = self.durable_scenario(newer_runs=250)
        scenario["passes"] = 2
        scenario["concurrent"] = True
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)
        observed = [
            comment["body"] for comment in result["comments"]
            if "state=observed" in comment["body"]
        ]
        self.assertEqual(len(observed), 2, observed)

    def test_scheduled_active_old_run_owns_the_work(self):
        scenario = self.durable_scenario()
        old_run = scenario["workflowRuns"][-1]
        old_run["status"] = "in_progress"
        old_run["conclusion"] = None
        scenario["runs"][0]["status"] = "in_progress"
        scenario["runs"][0]["conclusion"] = None
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_scheduled_published_pr_is_not_redispatched(self):
        scenario = self.durable_scenario()
        scenario["pulls"] = [
            {
                "number": 500,
                "state": "open",
                "head": {
                    "ref": BRANCH,
                    "repo": {"full_name": OWNER + "/" + REPO},
                },
            }
        ]
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_scheduled_closed_unmerged_pr_does_not_fence_newer_checkpoint(self):
        scenario = self.durable_scenario()
        scenario["comments"][0]["body"] = recovery_marker(
            "claimed", OLD_SHA, OLD_RUN_ID
        )
        scenario["pulls"] = [
            {
                "number": 500,
                "state": "closed",
                "merged_at": None,
                "head": {
                    "ref": "opencode/issue382-earlier-attempt",
                    "repo": {"full_name": OWNER + "/" + REPO},
                },
            }
        ]
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)

    def test_scheduled_legacy_marker_without_detail_is_recognized(self):
        scenario = self.durable_scenario()
        scenario["comments"][0]["body"] = (
            scenario["comments"][0]["body"].replace(" detail=test", "")
        )
        result = self.run_scenario(scenario)
        self.assert_one_current_dispatch(result, NEW_SHA)

    def test_scheduled_issue_guards_fail_closed(self):
        for label in (
            "automation:paused",
            "automation:blocked",
            "automation:qualifying",
            "no-auto-merge",
        ):
            with self.subTest(label=label):
                scenario = self.durable_scenario()
                scenario["issues"][0]["labels"].append({"name": label})
                result = self.run_scenario(scenario)
                self.assert_no_dispatch(result)

    def test_scheduled_closed_issue_is_not_resumed(self):
        scenario = self.durable_scenario()
        scenario["issues"][0]["state"] = "closed"
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_scheduled_active_run_lookup_failure_fails_closed(self):
        scenario = self.durable_scenario()
        scenario["activeRunsError"] = 429
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)
        self.assertTrue(
            any(
                "cannot verify competing runs" in " ".join(line)
                for line in result["notices"]
            ),
            result["notices"],
        )

    def test_scheduled_paused_unrelated_issues_are_not_scanned(self):
        scenario = self.durable_scenario()
        for number, label in ((373, "automation:paused"),
                              (357, "automation:blocked"),
                              (346, "no-auto-merge")):
            scenario["issues"].append({
                "number": number,
                "state": "open",
                "title": "unrelated",
                "labels": [
                    {"name": "automation:in-progress"},
                    {"name": label},
                ],
            })
        result = self.run_scenario(scenario)
        dispatch = self.assert_one_current_dispatch(result, NEW_SHA)
        self.assertEqual(
            json.loads(dispatch["inputs"]["recovery_identity"])
            ["recovery_operation_id"],
            OPERATION,
        )
        self.assertFalse(
            any(
                "#" + str(number) in " ".join(line)
                for number in (373, 357, 346)
                for line in result["notices"]
                if "durable recovery" in " ".join(line)
            ),
            result["notices"],
        )

    def test_scheduled_owner_stop_is_authoritative(self):
        scenario = self.durable_scenario()
        scenario["comments"].append(
            {
                "id": 20,
                "body": (
                    "<!-- continuum-task-operator-stop "
                    "repo=kodmial/continuum issue=382 generation=1 -->"
                ),
                "user": {"login": "kodmial"},
                "author_association": "OWNER",
                "created_at": "2026-10-23T10:00:00Z",
                "updated_at": "2026-10-23T10:00:00Z",
            }
        )
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_scheduled_malformed_or_foreign_marker_fails_closed(self):
        scenarios = {
            "foreign": recovery_marker("observed", OLD_SHA, OLD_RUN_ID).replace(
                OPERATION, "opencode-429-issue-issue373"
            ),
            "generation": recovery_marker(
                "observed", OLD_SHA, OLD_RUN_ID
            ).replace("gen=1", "gen=2"),
            "checkpoint": recovery_marker(
                "observed", OLD_SHA, OLD_RUN_ID
            ).replace(CHECKPOINT_REF, CHECKPOINT_REF + "-other"),
        }
        for name, body in scenarios.items():
            with self.subTest(name=name):
                scenario = self.durable_scenario()
                scenario["comments"][0]["body"] = body
                result = self.run_scenario(scenario)
                self.assert_no_dispatch(result)

    def test_scheduled_forged_marker_is_ignored(self):
        scenario = self.durable_scenario()
        scenario["comments"][0]["user"] = {"login": "random-contributor"}
        scenario["comments"][0]["author_association"] = "NONE"
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_scheduled_unreserved_issue_is_not_scanned(self):
        scenario = self.durable_scenario()
        scenario["issues"][0]["labels"] = [{"name": "automation:ready"}]
        result = self.run_scenario(scenario)
        self.assert_no_dispatch(result)

    def test_production_source_verifies_authoritative_run_evidence(self):
        self.assertIn("getWorkflowRun", self.source)
        self.assertIn("inspectObservedRecovery", self.source)
        self.assertIn("observed-unverified", self.source)
        self.assertIn("hasEffectiveOperatorStop", self.source)
        self.assertIn("newer-checkpoint", self.source)


if __name__ == "__main__":
    unittest.main()
