"""Executable regression for releasing a stale deterministic resume hold.

The production scheduler and watchdog each persist a
``continuum-checkpoint-resume-blocked`` marker. A merged validator fix can make
that exact rejection obsolete, but only a narrow, machine-verifiable manifest
entry may release it once. These tests execute both checked-in github-script
sources under Node with mocked GitHub clients so the scheduler and watchdog
share the same release semantics without trusting comment prose or arbitrary
main commits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from continuum import checkpoint_resume

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"
SCHEDULER = ROOT / ".github" / "scripts" / "issue_scheduler.js"
WATCHDOG = ROOT / ".github" / "workflows" / "continuum-opencode-watchdog.yml"

OWNER = "kodmial"
REPO = "continuum"
ISSUE = 534
OPERATION = "opencode-429-issue-issue534"
CHECKPOINT_REF = "opencode/429-checkpoint-opencode-429-issue-issue534-gen-2"
CHECKPOINT_SHA = "4a6e1eb11b2ab6e4ce233a610b177d4e7bc59ecb"
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
SNAPSHOT = "456f396895a33ec247e65308865c1728aad67e44a473a6f3d2f00b81d2babbb2"
FIX_PR = 461
FIX_SHA = "d063e72900790a647a9709771f477085b8141d83"
FIX_ID = "generation-2-checkpoint-validator"


def segment(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    first = text.index(start)
    return text[first:text.index(end, first)]


def hold_marker(reason: str = "wrong-generation") -> str:
    return (
        "<!-- continuum-checkpoint-resume-blocked "
        "repo=kodmial/continuum issue=534 generation=2 "
        "operation=" + OPERATION + " checkpoint=" + CHECKPOINT_REF +
        " checkpoint_sha=" + CHECKPOINT_SHA + " base_ref=main base_sha=" +
        BASE_SHA + " snapshot=" + SNAPSHOT + " reason=" + reason + " -->"
    )


def release_marker(
    *, owner="issue-scheduler", snapshot=SNAPSHOT, issue=ISSUE,
    checkpoint_sha=CHECKPOINT_SHA, fix_sha=FIX_SHA,
    base_sha=BASE_SHA, repo="kodmial/continuum",
) -> str:
    return (
        "<!-- continuum-checkpoint-resume-release v=1 "
        "repo=" + repo + " issue=" + str(issue) + " generation=2 " +
        "operation=" + OPERATION + " checkpoint=" + CHECKPOINT_REF +
        " checkpoint_sha=" + checkpoint_sha + " base_ref=main base_sha=" +
        base_sha + " snapshot=" + snapshot + " reason=wrong-generation " +
        "fix=" + FIX_ID + " fix_pr=461 fix_sha=" + fix_sha +
        " owner=" + owner + " -->"
    )


def consumption_marker(
    release_id, *, attempt="issue-scheduler:1:1",
    snapshot=SNAPSHOT, issue=ISSUE, checkpoint_sha=CHECKPOINT_SHA,
    reason="wrong-generation", repo="kodmial/continuum", generation=2,
) -> str:
    return (
        "<!-- continuum-checkpoint-resume-consumed v=1 "
        "repo=" + repo + " issue=" + str(issue) +
        " release=" + str(release_id) + " attempt=" + attempt +
        " generation=" + str(generation) +
        " operation=" + OPERATION + " checkpoint=" + CHECKPOINT_REF +
        " checkpoint_sha=" + checkpoint_sha + " snapshot=" + snapshot +
        " reason=" + reason + " -->"
    )


def manifest() -> dict:
    return {
        "schema_version": 1,
        "fixes": [
            {
                "id": FIX_ID,
                "pull_request": FIX_PR,
                "merge_sha": FIX_SHA,
                "reasons": ["wrong-generation", "resume-wrong-generation"],
                "paths": ["src/continuum/checkpoint_resume.py"],
            }
        ],
    }


NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const schedulerSource = fs.readFileSync(process.argv[3], 'utf8');
const watchdogSource = fs.readFileSync(process.argv[4], 'utf8');
const resultPath = process.argv[5];
const dispatchSource = fs.readFileSync(process.argv[6], 'utf8');

function apiError(status, message) {
  const error = new Error(message || ('HTTP ' + status));
  error.status = status;
  return error;
}

const comments = (scenario.comments || []).map(comment => ({ ...comment }));
let nextCommentId = scenario.nextCommentId || 1000;
let raceInjected = false;
let lastSchedulerApi = null;
let dispatchFailure = null;
let dispatchAttempts = 0;

const listReviews = async () => ({ data: scenario.reviews || [] });
const listFiles = async () => ({ data: scenario.files || [] });
const listComments = async () => ({ data: comments.map(c => ({ ...c })) });

// The repository-scoped token can read this consumer issue thread only. The
// verified fix lives in kodmial/continuum, which this token cannot read, so
// any source read through it must fail exactly like the production 404.
const readGithub = {
  rest: {
    git: {
      getRef: async ({ ref } = {}) => {
        const refName = String(ref || '').replace(/^heads\//, '');
        if (refName === String(scenario.baseRef || 'main')) {
          if (scenario.baseError) {
            throw apiError(scenario.baseError, 'base ref failed');
          }
          return {
            data: {
              object: { sha: scenario.baseSha ||
                '0123456789abcdef0123456789abcdef01234567' },
            },
          };
        }
        if (scenario.refError) throw apiError(scenario.refError, 'ref failed');
        return {
          data: { object: { sha: scenario.refSha || scenario.holdSha } },
        };
      },
    },
    repos: {
      getContent: async () => {
        throw apiError(
          404, 'repository-scoped token cannot read the Continuum source'
        );
      },
    },
    issues: { listComments },
  },
};
readGithub.paginate = async (method) => {
  if (method === listComments) return (await listComments()).data;
  throw new Error('unexpected paginate call');
};

// The PAT-authenticated client is the only one that can read the Continuum
// source repository, and it is also the writer for label/comment side effects.
const writeGithub = {
  rest: {
    repos: {
      getContent: async ({ path }) => {
        if (
          path !== '.github/continuum-checkpoint-validator-fixes.json' ||
          !scenario.manifest
        ) {
          throw apiError(404, 'manifest not found');
        }
        const value = scenario.manifest;
        const text = typeof value === 'string'
          ? value
          : JSON.stringify(value);
        return {
          data: {
            type: 'file',
            content: Buffer.from(text, 'utf8').toString('base64'),
          },
        };
      },
      compareCommits: async () => ({
        data: { status: scenario.compareStatus || 'ahead' },
      }),
    },
    pulls: {
      get: async () => {
        if (scenario.pullError) {
          throw apiError(scenario.pullError, 'pull failed');
        }
        return {
          data: {
            merged: scenario.merged !== false,
            merge_commit_sha: scenario.pullSha || scenario.fixSha,
            user: { login: scenario.author || 'kodmial' },
          },
        };
      },
      listReviews,
      listFiles,
    },
    issues: {
      createComment: async ({ body }) => {
        // Only the `comment` dispatch mode posts an `/oc` command. A
        // configured failure makes that production dispatch call throw, so
        // the scheduler's own settlement logic runs unchanged.
        if (String(body || '').startsWith('/oc')) {
          dispatchAttempts += 1;
          if (dispatchFailure) {
            const failure = dispatchFailure;
            dispatchFailure = null;
            if (failure.acceptedWithLostResponse) {
              // GitHub accepted the dispatch but the response was lost: the
              // trigger comment is visible even though the call threw.
              comments.push({
                id: nextCommentId++,
                body,
                user: { login: 'github-actions[bot]' },
                author_association: 'OWNER',
                created_at: new Date().toISOString(),
              });
            }
            throw apiError(
              failure.status || 0, failure.message || 'dispatch failed'
            );
          }
        }
        const comment = {
          id: nextCommentId++,
          body,
          user: { login: 'github-actions[bot]' },
          author_association: 'OWNER',
          created_at: scenario.now || '2026-11-17T12:00:00Z',
        };
        comments.push(comment);
        if (scenario.raceRelease && !raceInjected) {
          raceInjected = true;
          comments.push({
            id: 999,
            body: scenario.raceRelease.body,
            user: { login: 'github-actions[bot]' },
            author_association: 'OWNER',
            created_at: scenario.now || '2026-11-17T12:00:00Z',
          });
        }
        return { data: { ...comment } };
      },
      deleteComment: async ({ comment_id }) => {
        if (scenario.deleteCommentError) {
          throw apiError(scenario.deleteCommentError, 'delete comment failed');
        }
        const index = comments.findIndex(
          item => Number(item.id) === Number(comment_id)
        );
        if (index >= 0) comments.splice(index, 1);
        return { data: {} };
      },
    },
  },
};
writeGithub.paginate = async (method) => {
  if (method === listReviews) return (await listReviews()).data;
  if (method === listFiles) return (await listFiles()).data;
  throw new Error('unexpected paginate call');
};

const core = {
  warning: (...args) => notices.push(['warning', ...args.map(String)]),
  info: (...args) => notices.push(['info', ...args.map(String)]),
  notice: (...args) => notices.push(['notice', ...args.map(String)]),
};
const notices = [];

function snapshotComments() {
  return comments.map(comment => ({ ...comment }));
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

async function runScheduler() {
  const factory = new AsyncFunction(
    'github', 'core', 'owner', 'repo', 'readGithub', 'issueNumber',
    'const isTrustedStopComment = (comment, trustedOwner) => {' +
      'const association = String(comment.author_association || "").toUpperCase();' +
      'if (["OWNER", "MEMBER", "COLLABORATOR"].includes(association)) return true;' +
      'const login = String((comment.user || {}).login || "").toLowerCase();' +
      'return login === "github-actions[bot]" || login === String(trustedOwner || "").toLowerCase();' +
    '};' +
    'const stopOrderKey = entry => String(entry.created_at || "") + "#" + ' +
      'String(entry.id || 0).padStart(12, "0");\n' +
    // The production dispatch block is exercised with only its surrounding
    // loop, label, and route bindings stubbed. The consumption receipt,
    // dispatchAttempted flag, settlement, and receipt rollback all come from
    // the checked-in scheduler source.
    'const addLabel = async () => {};\n' +
    'const removeLabel = async () => {};\n' +
    'const executionRoutes = [];\n' +
    "const opencodeDispatch = 'comment';\n" +
    'const dispatchWorkflow = async () => {};\n' +
    "const dispatchRef = 'main';\n" +
    "const opencodeWorkflowPath = '.github/workflows/continuum-opencode.yml';\n" +
    'const resetMarkers = false;\n' +
    'const inProgressLabel = "automation:in-progress";\n' +
    'const priority = null;\n' +
    "const dispatchMarker = '<!-- issue-scheduler-dispatch -->';\n" +
    'const isCanonicalSchedulerDispatch = () => false;\n' +
    schedulerSource +
    '\nconst runDispatch = async (freshRelease) => {\n' +
    '  let pass = null;\n' +
    '  const targets = [{ number: Number(issueNumber), labels: [] }];\n' +
    '  for (const issue of targets) {\n' +
    dispatchSource +
    '\n    pass = { consumption: checkpointConsumption, dispatchAttempted };\n' +
    '  }\n' +
    '  return pass;\n' +
    '};\n' +
    '\nreturn { hold: checkpointResumeHold, released: () => checkpointResumeHoldRelease, consume: consumeCheckpointResumeRelease, dispatchFailureIsDefinitive: checkpointDispatchFailureIsDefinitive, settleConsumptionFailure: settleCheckpointResumeConsumptionFailure, dispatch: runDispatch };'
  );
  const api = await factory(
    writeGithub,
    core,
    scenario.owner || 'kodmial',
    scenario.repo || 'continuum',
    readGithub,
    Number(scenario.issue || 534)
  );
  lastSchedulerApi = api;
  const hold = await api.hold(
    scenario.owner || 'kodmial',
    scenario.repo || 'continuum',
    Number(scenario.issue || 534),
    snapshotComments(),
    readGithub,
    1,
    { controller: 'issue-scheduler', writeClient: writeGithub }
  );
  return { blocked: Boolean(hold), hold: hold || null, release: api.released() };
}

// One production scheduler pass: re-evaluate the durable release from the
// current comment history, then run the checked-in dispatch block with the
// real consumption, dispatchAttempted, and settlement code.
async function runProductionDispatchPass(step) {
  const evaluated = await runScheduler();
  let dispatched = null;
  if (evaluated.release && evaluated.release.released && lastSchedulerApi) {
    dispatchFailure = step && step.failure ? step.failure : null;
    try {
      const pass = await lastSchedulerApi.dispatch(evaluated.release);
      dispatched = { pass, error: null };
    } catch (err) {
      dispatched = {
        pass: null,
        error: {
          status: Number(err && err.status || 0),
          message: String(err && err.message || err),
        },
      };
    } finally {
      dispatchFailure = null;
    }
  }
  return {
    blocked: evaluated.blocked,
    release: evaluated.release,
    dispatch: dispatched,
    dispatchAttempts,
    receipts: comments.filter(comment =>
      String(comment.body || '').includes('resume-consumed')
    ).length,
    releases: comments.filter(comment =>
      String(comment.body || '').includes('resume-release')
    ).length,
  };
}

async function runWatchdog() {
  const factory = new AsyncFunction(
    'github', 'core', 'readGithub', 'owner', 'repo', 'repoFullName',
    watchdogSource + '\nreturn { hold: checkpointResumeHold };'
  );
  const api = await factory(
    writeGithub,
    core,
    readGithub,
    scenario.owner || 'kodmial',
    scenario.repo || 'continuum',
    (scenario.owner || 'kodmial') + '/' + (scenario.repo || 'continuum')
  );
  const result = await api.hold(
    Number(scenario.issue || 534),
    snapshotComments(),
    null,
    {
      controller: 'opencode-watchdog',
      liveOverlap: async () => {
        if (scenario.liveOverlapError) {
          throw apiError(scenario.liveOverlapError, 'inventory failed');
        }
        return Boolean(scenario.liveOverlap);
      },
    }
  );
  return {
    blocked: Boolean(result && result.blocked),
    hold: result && result.marker || null,
    release: result && result.release || null,
  };
}

(async () => {
  const scheduler = await runScheduler();
  let consumption = null;
  let duplicateConsumption = null;
  let settlement = null;
  let retryConsumption = null;
  if (scenario.consumeSchedulerRelease && scheduler.release &&
      scheduler.release.released && lastSchedulerApi) {
    consumption = await lastSchedulerApi.consume({
      targetOwner: scenario.owner || 'kodmial',
      targetRepo: scenario.repo || 'continuum',
      issueNumber: Number(scenario.issue || 534),
      hold: scheduler.release.marker,
      release: scheduler.release,
      readClient: readGithub,
      writeClient: writeGithub,
      controller: 'issue-scheduler',
    });
    if (scenario.consumeDuplicateRelease) {
      duplicateConsumption = await lastSchedulerApi.consume({
        targetOwner: scenario.owner || 'kodmial',
        targetRepo: scenario.repo || 'continuum',
        issueNumber: Number(scenario.issue || 534),
        hold: scheduler.release.marker,
        release: {
          ...scheduler.release,
          marker: {
            ...scheduler.release.marker,
            commentId: Number(scenario.consumeDuplicateRelease.commentId),
          },
        },
        readClient: readGithub,
        writeClient: writeGithub,
        controller: 'issue-scheduler',
      });
    }
  }
  if (scenario.dispatchFailure && consumption && lastSchedulerApi) {
    const failure = scenario.dispatchFailure;
    settlement = await lastSchedulerApi.settleConsumptionFailure({
      consumption,
      dispatchAttempted: failure.dispatchAttempted !== false,
      error: apiError(
        failure.status || 0,
        failure.message || 'dispatch failed'
      ),
      targetOwner: scenario.owner || 'kodmial',
      targetRepo: scenario.repo || 'continuum',
      issueNumber: Number(scenario.issue || 534),
      writeClient: writeGithub,
      observeDispatch: Object.prototype.hasOwnProperty.call(
        scenario, 'dispatchObserved'
      )
        ? async () => {
            if (scenario.dispatchProbeError) {
              throw apiError(scenario.dispatchProbeError, 'probe failed');
            }
            return scenario.dispatchObserved;
          }
        : undefined,
    });
  }
  // A second scheduler pass over the same durable history proves an
  // unconsumed release is reusable and a consumed one is spent.
  const schedulerAgain = await runScheduler();
  if (scenario.consumeSchedulerReleaseAgain && schedulerAgain.release &&
      schedulerAgain.release.released && lastSchedulerApi) {
    retryConsumption = await lastSchedulerApi.consume({
      targetOwner: scenario.owner || 'kodmial',
      targetRepo: scenario.repo || 'continuum',
      issueNumber: Number(scenario.issue || 534),
      hold: schedulerAgain.release.marker,
      release: schedulerAgain.release,
      readClient: readGithub,
      writeClient: writeGithub,
      controller: 'issue-scheduler',
    });
  }
  const productionPasses = [];
  for (const step of scenario.productionDispatchPasses || []) {
    productionPasses.push(await runProductionDispatchPass(step));
  }
  const result = {
    scheduler,
    consumption,
    duplicateConsumption,
    settlement,
    retryConsumption,
    productionPasses,
    dispatchAttempts,
    schedulerDispatchFailureClassification: Object.fromEntries(
      [0, 408, 403, 422, 429, 500].map(status => [
        String(status),
        lastSchedulerApi.dispatchFailureIsDefinitive({ status }),
      ])
    ),
    schedulerAgain,
    watchdog: await runWatchdog(),
    comments: comments.map(comment => ({ ...comment })),
    notices,
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


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class CheckpointHoldReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        cls.scheduler_source = segment(
            SCHEDULER,
            "  const checkpointResumeBlockedMarkerRe =",
            "  const ownerResumeMarkerRe =",
        )
        cls.watchdog_source = segment(
            WATCHDOG,
            "          const TASK_SNAPSHOT_GENERATION = 1;",
            "          async function discoverDurableRecoveryCandidates() {",
        )
        cls.dispatch_source = segment(
            SCHEDULER,
            "    let checkpointConsumption = null;\n"
            "    let dispatchAttempted = false;",
            "    core.notice(\n      'Dispatched issue #",
        )

    def run_scenario(self, scenario):
        workdir = Path(tempfile.mkdtemp(prefix="hold-release-", dir=str(TMP_PARENT)))
        try:
            scenario_path = workdir / "scenario.json"
            scheduler_path = workdir / "scheduler.js"
            watchdog_path = workdir / "watchdog.js"
            dispatch_path = workdir / "dispatch.js"
            harness_path = workdir / "harness.js"
            result_path = workdir / "result.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            scheduler_path.write_text(self.scheduler_source, encoding="utf-8")
            watchdog_path.write_text(self.watchdog_source, encoding="utf-8")
            dispatch_path.write_text(self.dispatch_source, encoding="utf-8")
            harness_path.write_text(NODE_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [
                    "node", str(harness_path), str(scenario_path),
                    str(scheduler_path), str(watchdog_path),
                    str(result_path), str(dispatch_path),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=120,
            )
            self.assertEqual(
                completed.returncode, 0,
                (completed.stdout or "") + (completed.stderr or ""),
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertNotIn("harnessError", result, result.get("harnessError"))
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def base_scenario(self):
        return {
            "owner": OWNER,
            "repo": REPO,
            "issue": ISSUE,
            "holdSha": CHECKPOINT_SHA,
            "fixSha": FIX_SHA,
            "manifest": manifest(),
            "reviews": [
                {"user": {"login": "coderabbitai[bot]"}, "state": "APPROVED"}
            ],
            "files": [{"filename": "src/continuum/checkpoint_resume.py"}],
            "comments": [
                {
                    "id": 1,
                    "body": hold_marker(),
                    "user": {"login": "github-actions[bot]"},
                    "author_association": "OWNER",
                    "created_at": "2026-11-17T09:00:00Z",
                }
            ],
        }

    def test_dispatch_failure_only_releases_receipts_for_rejected_requests(self):
        result = self.run_scenario(self.base_scenario())
        # Transport errors/timeouts (0, 408) and server failures (500) cannot
        # prove the dispatch did not happen. A 4xx rejection can.
        self.assertEqual(
            result["schedulerDispatchFailureClassification"],
            {
                "0": False,
                "408": False,
                "403": True,
                "422": True,
                "429": True,
                "500": False,
            },
        )

    def test_unverifiable_lost_dispatch_response_keeps_the_receipt(self):
        # Without a dispatch probe an ambiguous outcome cannot prove the
        # runner did not start, so the receipt stays fail-closed.
        for status in (0, 408, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["consumeSchedulerRelease"] = True
                scenario["dispatchFailure"] = {
                    "status": status,
                    "dispatchAttempted": True,
                }
                scenario["consumeSchedulerReleaseAgain"] = True
                result = self.run_scenario(scenario)
                self.assertTrue(result["consumption"]["consumed"])
                self.assertTrue(result["settlement"]["preserved"])
                self.assertFalse(result["settlement"]["released"])
                self.assertTrue(
                    any(
                        "dispatch outcome is unknown" in " ".join(line)
                        for line in result["notices"]
                    ),
                    result["notices"],
                )
                self.assertTrue(result["schedulerAgain"]["blocked"])
                self.assertFalse(
                    result["schedulerAgain"]["release"]["released"]
                )
                self.assertEqual(
                    result["schedulerAgain"]["release"]["reason"],
                    "release-consumed-or-superseded",
                )
                self.assertIsNone(result["retryConsumption"])
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-release" in c["body"]
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-consumed" in c["body"]
                        ]
                    ),
                    1,
                )

    def test_ambiguous_dispatch_without_an_observed_run_retries_once(self):
        # A transient 5xx before GitHub accepted the dispatch must not leave
        # the release spent forever. The probe proves no run/comment exists,
        # the receipt is rolled back, and a later pass retries exactly once.
        for status in (0, 408, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["consumeSchedulerRelease"] = True
                scenario["dispatchFailure"] = {
                    "status": status,
                    "dispatchAttempted": True,
                }
                scenario["dispatchObserved"] = False
                scenario["consumeSchedulerReleaseAgain"] = True
                result = self.run_scenario(scenario)
                self.assertTrue(result["consumption"]["consumed"])
                self.assertTrue(result["settlement"]["released"])
                self.assertFalse(result["settlement"]["preserved"])
                self.assertEqual(
                    result["settlement"]["verified"], "not-observed"
                )
                self.assertFalse(result["schedulerAgain"]["blocked"])
                self.assertTrue(
                    result["schedulerAgain"]["release"]["released"]
                )
                self.assertTrue(result["retryConsumption"]["consumed"])
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-consumed" in c["body"]
                        ]
                    ),
                    1,
                )

    def test_ambiguous_dispatch_with_an_observed_run_keeps_the_receipt(self):
        # A lost response whose trigger actually landed must keep the receipt
        # so the next pass cannot resume the same checkpoint twice.
        for status in (0, 408, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["consumeSchedulerRelease"] = True
                scenario["dispatchFailure"] = {
                    "status": status,
                    "dispatchAttempted": True,
                }
                scenario["dispatchObserved"] = True
                scenario["consumeSchedulerReleaseAgain"] = True
                result = self.run_scenario(scenario)
                self.assertTrue(result["settlement"]["preserved"])
                self.assertFalse(result["settlement"]["released"])
                self.assertEqual(
                    result["settlement"]["verified"], "observed"
                )
                self.assertTrue(result["schedulerAgain"]["blocked"])
                self.assertIsNone(result["retryConsumption"])

    def test_ambiguous_dispatch_with_an_unreadable_probe_keeps_the_receipt(self):
        scenario = self.base_scenario()
        scenario["consumeSchedulerRelease"] = True
        scenario["dispatchFailure"] = {
            "status": 500,
            "dispatchAttempted": True,
        }
        scenario["dispatchObserved"] = False
        scenario["dispatchProbeError"] = 502
        scenario["consumeSchedulerReleaseAgain"] = True
        result = self.run_scenario(scenario)
        self.assertTrue(result["settlement"]["preserved"])
        self.assertFalse(result["settlement"]["released"])
        self.assertTrue(result["schedulerAgain"]["blocked"])
        self.assertIsNone(result["retryConsumption"])

    def test_rejected_dispatch_releases_the_receipt_and_allows_one_retry(self):
        # A 4xx rejection proves the dispatch did not happen. The receipt is
        # rolled back, so the next pass replays the same claim exactly once.
        for status in (403, 422):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["consumeSchedulerRelease"] = True
                scenario["dispatchFailure"] = {
                    "status": status,
                    "dispatchAttempted": True,
                }
                scenario["consumeSchedulerReleaseAgain"] = True
                result = self.run_scenario(scenario)
                self.assertTrue(result["consumption"]["consumed"])
                self.assertTrue(result["settlement"]["released"])
                self.assertFalse(result["settlement"]["preserved"])
                self.assertFalse(result["schedulerAgain"]["blocked"])
                self.assertTrue(
                    result["schedulerAgain"]["release"]["released"]
                )
                self.assertEqual(
                    result["schedulerAgain"]["release"]["marker"][
                        "commentId"
                    ],
                    result["scheduler"]["release"]["marker"]["commentId"],
                )
                self.assertTrue(result["retryConsumption"]["consumed"])
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-release" in c["body"]
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-consumed" in c["body"]
                        ]
                    ),
                    1,
                )

    def test_production_dispatch_lost_response_without_a_trigger_retries_once(self):
        # The checked-in dispatch block probes for its durable side effect.
        # When the lost response left no trigger comment, the receipt is
        # rolled back and a later full pass dispatches exactly once more.
        for status in (0, 408, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["productionDispatchPasses"] = [
                    {"failure": {"status": status}},
                    {},
                ]
                result = self.run_scenario(scenario)
                first, second = result["productionPasses"]
                self.assertEqual(
                    first["dispatch"]["error"]["status"], status
                )
                self.assertEqual(first["dispatchAttempts"], 1)
                self.assertEqual(first["receipts"], 0)
                self.assertEqual(first["releases"], 1)
                self.assertFalse(second["blocked"])
                self.assertTrue(second["release"]["released"])
                self.assertTrue(
                    second["dispatch"]["pass"]["consumption"]["consumed"]
                )
                self.assertIsNone(second["dispatch"]["error"])
                self.assertEqual(second["dispatchAttempts"], 2)
                self.assertEqual(second["receipts"], 1)
                self.assertEqual(second["releases"], 1)
                self.assertEqual(result["dispatchAttempts"], 2)

    def test_production_dispatch_lost_response_with_a_visible_trigger_stays_spent(self):
        # A lost response whose `/oc` trigger comment is visible proves the
        # dispatch landed. The receipt stays spent and the next pass stands
        # down instead of dispatching a second resumption.
        for status in (0, 408, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["productionDispatchPasses"] = [
                    {
                        "failure": {
                            "status": status,
                            "acceptedWithLostResponse": True,
                        },
                    },
                    {},
                ]
                result = self.run_scenario(scenario)
                first, second = result["productionPasses"]
                self.assertEqual(
                    first["dispatch"]["error"]["status"], status
                )
                self.assertEqual(first["dispatchAttempts"], 1)
                self.assertEqual(first["receipts"], 1)
                self.assertEqual(first["releases"], 1)
                self.assertTrue(second["blocked"])
                self.assertFalse(second["release"]["released"])
                self.assertEqual(
                    second["release"]["reason"],
                    "release-consumed-or-superseded",
                )
                self.assertIsNone(second["dispatch"])
                self.assertEqual(second["dispatchAttempts"], 1)
                self.assertEqual(second["receipts"], 1)
                self.assertEqual(result["dispatchAttempts"], 1)

    def test_production_dispatch_confirmed_rejection_retries_once(self):
        # A confirmed 4xx rejection means the dispatch never happened. The
        # checked-in settlement deletes the receipt, so the next full
        # scheduler pass reuses the same release and dispatches exactly once
        # more.
        for status in (403, 422):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["productionDispatchPasses"] = [
                    {"failure": {"status": status}},
                    {},
                ]
                result = self.run_scenario(scenario)
                first, second = result["productionPasses"]
                self.assertEqual(
                    first["dispatch"]["error"]["status"], status
                )
                # The receipt written before the rejected dispatch is rolled
                # back, leaving the release unconsumed and retryable.
                self.assertEqual(first["dispatchAttempts"], 1)
                self.assertEqual(first["receipts"], 0)
                self.assertEqual(first["releases"], 1)
                self.assertFalse(second["blocked"])
                self.assertTrue(second["release"]["released"])
                self.assertEqual(
                    second["release"]["marker"]["commentId"],
                    first["release"]["marker"]["commentId"],
                )
                self.assertTrue(
                    second["dispatch"]["pass"]["consumption"]["consumed"]
                )
                self.assertIsNone(second["dispatch"]["error"])
                self.assertEqual(second["dispatchAttempts"], 2)
                self.assertEqual(second["receipts"], 1)
                self.assertEqual(second["releases"], 1)
                self.assertEqual(result["dispatchAttempts"], 2)

    def test_both_controllers_hold_until_the_manifest_fix_is_verifiable(self):
        scenario = self.base_scenario()
        scenario["manifest"] = None
        result = self.run_scenario(scenario)
        self.assertTrue(result["scheduler"]["blocked"])
        self.assertTrue(result["watchdog"]["blocked"])
        self.assertFalse(result["scheduler"]["release"]["released"])
        self.assertFalse(result["watchdog"]["release"]["released"])
        self.assertEqual(
            [c for c in result["comments"] if "resume-release" in c["body"]],
            [],
        )

    def test_scheduler_releases_once_and_watchdog_replays_the_same_claim(self):
        scenario = self.base_scenario()
        result = self.run_scenario(scenario)
        self.assertFalse(result["scheduler"]["blocked"])
        self.assertTrue(result["scheduler"]["release"]["released"])
        self.assertEqual(
            result["scheduler"]["release"]["owner"], "issue-scheduler"
        )
        released = [
            c for c in result["comments"] if "resume-release" in c["body"]
        ]
        self.assertEqual(len(released), 1)
        self.assertIn("owner=issue-scheduler", released[0]["body"])

        # The next scheduler pass sees its own durable claim and stands on it:
        # no second release comment and no second identity change.
        self.assertFalse(result["schedulerAgain"]["blocked"])
        self.assertTrue(result["schedulerAgain"]["release"]["released"])
        self.assertEqual(
            result["schedulerAgain"]["release"]["owner"], "issue-scheduler"
        )
        self.assertEqual(
            result["schedulerAgain"]["release"]["marker"]["commentId"],
            result["scheduler"]["release"]["marker"]["commentId"],
        )
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            1,
        )

        # The watchdog sees the same exact claim and stands down rather than
        # publishing a second release comment.
        self.assertFalse(result["watchdog"]["blocked"])
        self.assertTrue(result["watchdog"]["release"]["released"])
        self.assertEqual(
            result["watchdog"]["release"]["owner"], "issue-scheduler"
        )
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            1,
        )

    def test_consuming_the_release_is_durable_across_scheduler_passes(self):
        scenario = self.base_scenario()
        scenario["consumeSchedulerRelease"] = True
        result = self.run_scenario(scenario)
        self.assertTrue(result["consumption"]["consumed"])
        self.assertFalse(result["scheduler"]["blocked"])
        # The receipt records the consuming attempt before any dispatch, so
        # the next pass replays it without authorizing a second resumption.
        self.assertTrue(result["schedulerAgain"]["blocked"])
        self.assertFalse(result["schedulerAgain"]["release"]["released"])
        self.assertEqual(
            result["schedulerAgain"]["release"]["reason"],
            "release-consumed-or-superseded",
        )
        self.assertTrue(result["watchdog"]["blocked"])
        self.assertFalse(result["watchdog"]["release"]["released"])
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            1,
        )

    def test_a_consumed_release_never_authorizes_another_resumption(self):
        scenario = self.base_scenario()
        scenario["comments"].extend([
            {
                "id": 2,
                "body": release_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            },
            {
                "id": 3,
                "body": consumption_marker(2),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:01:00Z",
            },
        ])
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["reason"],
                "release-consumed-or-superseded",
                name,
            )

    def test_a_consumption_receipt_spends_surviving_duplicate_claims(self):
        # Concurrent release claims can both survive when the losing claim's
        # cleanup fails. A receipt for either claim consumes the whole hold
        # identity, so the surviving duplicate never authorizes a second
        # resumption in either controller.
        scenario = self.base_scenario()
        scenario["comments"].extend([
            {
                "id": 2,
                "body": release_marker(owner="opencode-watchdog"),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            },
            {
                "id": 3,
                "body": release_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:01:00Z",
            },
            {
                "id": 4,
                "body": consumption_marker(2),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:02:00Z",
            },
        ])
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["reason"],
                "release-consumed-or-superseded",
                name,
            )
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            2,
        )

    def test_consuming_one_duplicate_claim_blocks_the_surviving_claim(self):
        scenario = self.base_scenario()
        scenario["comments"].extend([
            {
                "id": 2,
                "body": release_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            },
            {
                "id": 3,
                "body": release_marker(owner="opencode-watchdog"),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:01:00Z",
            },
        ])
        scenario["consumeSchedulerRelease"] = True
        # A second consumer of the surviving claim must lose on the first
        # receipt. Its cleanup also fails, so both receipts remain durable.
        scenario["consumeDuplicateRelease"] = {"commentId": 3}
        scenario["deleteCommentError"] = 500
        result = self.run_scenario(scenario)
        self.assertTrue(result["consumption"]["consumed"])
        self.assertFalse(result["duplicateConsumption"]["consumed"])
        self.assertEqual(
            result["duplicateConsumption"]["reason"],
            "consumption-claimed-by-other",
        )
        self.assertTrue(
            any(
                "consumption cleanup failed" in " ".join(line)
                for line in result["notices"]
            ),
            result["notices"],
        )
        for name in ("schedulerAgain", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["reason"],
                "release-consumed-or-superseded",
                name,
            )
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            2,
        )
        self.assertEqual(
            len([
                c for c in result["comments"]
                if "resume-consumed" in c["body"]
            ]),
            2,
        )

    def test_a_newer_rejection_supersedes_an_older_release(self):
        scenario = self.base_scenario()
        scenario["comments"].extend([
            {
                "id": 2,
                "body": release_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            },
            {
                "id": 3,
                "body": hold_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:05:00Z",
            },
        ])
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["reason"],
                "release-consumed-or-superseded",
                name,
            )

    def test_a_released_retry_never_reopens_after_an_identical_rejection(self):
        # One released retry, then a new rejection that repeats the exact
        # checkpoint, base, snapshot, generation and reason. The surviving
        # release and its consumption receipt are bound to that rejection
        # history, so neither controller may admit a second resumption of the
        # same identity.
        scenario = self.base_scenario()
        scenario["comments"].extend([
            {
                "id": 2,
                "body": release_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            },
            {
                "id": 3,
                "body": consumption_marker(2),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:01:00Z",
            },
            {
                "id": 4,
                "body": hold_marker(),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:05:00Z",
            },
        ])
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["reason"],
                "release-consumed-or-superseded",
                name,
            )
        # The retried episode stays spent: no replacement release is minted
        # and the single receipt still records the one resumed attempt.
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            1,
        )
        self.assertEqual(
            len([
                c for c in result["comments"]
                if "resume-consumed" in c["body"]
            ]),
            1,
        )

    def test_non_fixable_hold_reason_stays_blocked_with_a_verified_fix(self):
        scenario = self.base_scenario()
        scenario["comments"][0]["body"] = hold_marker("checkpoint-missing")
        result = self.run_scenario(scenario)
        self.assertTrue(result["scheduler"]["blocked"])
        self.assertTrue(result["watchdog"]["blocked"])
        self.assertIsNone(result["scheduler"]["release"])
        self.assertIsNone(result["watchdog"]["release"])
        self.assertEqual(
            [c for c in result["comments"] if "resume-release" in c["body"]],
            [],
            "only the reason-specific generation validator hold may release",
        )

    def test_release_claim_loser_stands_down_without_a_second_resumption(self):
        scenario = self.base_scenario()
        scenario["raceRelease"] = {
            "body": release_marker(owner="opencode-watchdog"),
        }
        result = self.run_scenario(scenario)
        # The scheduler published its marker, then a lower-id competing claim
        # appeared. It must delete its own marker and stand down, leaving the
        # winner as the only durable release.
        self.assertTrue(result["scheduler"]["blocked"])
        self.assertFalse(result["scheduler"]["release"]["released"])
        self.assertTrue(result["scheduler"]["release"]["deferred"])
        self.assertEqual(
            result["scheduler"]["release"]["reason"],
            "release-claimed-by-other",
        )
        released = [
            c["body"] for c in result["comments"]
            if "resume-release" in c["body"]
        ]
        self.assertEqual(released, [release_marker(owner="opencode-watchdog")])
        # The watchdog replays the winner's claim and stands down too.
        self.assertFalse(result["watchdog"]["blocked"])
        self.assertEqual(
            result["watchdog"]["release"]["owner"], "opencode-watchdog"
        )

    def test_watchdog_releases_once_and_scheduler_replays_the_same_claim(self):
        scenario = self.base_scenario()
        # Put the watchdog first by making it the controller that runs on an
        # otherwise clean history. The harness's scheduler then replays.
        scenario["comments"].append(
            {
                "id": 2,
                "body": release_marker(
                    owner="opencode-watchdog",
                ),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T12:00:00Z",
            }
        )
        result = self.run_scenario(scenario)
        self.assertFalse(result["scheduler"]["blocked"])
        self.assertTrue(result["scheduler"]["release"]["released"])
        self.assertEqual(
            result["scheduler"]["release"]["owner"], "opencode-watchdog"
        )
        self.assertFalse(result["watchdog"]["blocked"])
        self.assertEqual(
            result["watchdog"]["release"]["owner"], "opencode-watchdog"
        )
        self.assertEqual(
            len([c for c in result["comments"] if "resume-release" in c["body"]]),
            1,
        )

    def test_forged_or_unverifiable_fix_provenance_never_releases(self):
        cases = {
            "not-merged": {"merged": False},
            "wrong-merge-sha": {"pullSha": "f" * 40},
            "author-only-review": {
                "reviews": [
                    {"user": {"login": "kodmial"}, "state": "APPROVED"}
                ]
            },
            "no-review": {"reviews": []},
            "changes-requested-only": {
                "reviews": [
                    {
                        "user": {"login": "outside-reviewer"},
                        "state": "CHANGES_REQUESTED",
                    }
                ]
            },
            "commented-only": {
                "reviews": [
                    {
                        "user": {"login": "drive-by-commenter"},
                        "state": "COMMENTED",
                    }
                ]
            },
            "untrusted-approval": {
                "reviews": [
                    {
                        "user": {"login": "random-reader"},
                        "state": "APPROVED",
                        "author_association": "NONE",
                    }
                ]
            },
            "dismissed-only": {
                "reviews": [
                    {
                        "user": {"login": "outside-reviewer"},
                        "state": "DISMISSED",
                    }
                ]
            },
            "approval-withdrawn": {
                "reviews": [
                    {
                        "user": {"login": "outside-reviewer"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-17T10:00:00Z",
                    },
                    {
                        "user": {"login": "outside-reviewer"},
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-11-17T11:00:00Z",
                    },
                ]
            },
            "approval-plus-live-objection": {
                "reviews": [
                    {
                        "user": {"login": "approver"},
                        "state": "APPROVED",
                    },
                    {
                        "user": {"login": "objector"},
                        "state": "CHANGES_REQUESTED",
                    },
                ]
            },
            "wrong-path": {"files": [{"filename": "README.md"}]},
            "fix-not-contained": {"compareStatus": "behind"},
            "pull-unavailable": {"pullError": 403},
        }
        for name, update in cases.items():
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario.update(update)
                result = self.run_scenario(scenario)
                self.assertTrue(result["scheduler"]["blocked"], name)
                self.assertTrue(result["watchdog"]["blocked"], name)
                self.assertFalse(
                    result["scheduler"]["release"]["released"], name
                )
                self.assertFalse(
                    result["watchdog"]["release"]["released"], name
                )
                self.assertEqual(
                    [
                        c for c in result["comments"]
                        if "resume-release" in c["body"]
                    ],
                    [],
                    name,
                )

    def test_a_trusted_human_approval_releases_the_hold(self):
        scenario = self.base_scenario()
        scenario["reviews"] = [
            {
                "user": {"login": "maintainer"},
                "state": "APPROVED",
                "author_association": "MEMBER",
            }
        ]
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertFalse(result[name]["blocked"], name)
            self.assertTrue(result[name]["release"]["released"], name)

    def test_a_fix_past_the_old_manifest_cap_still_releases(self):
        """Every manifest entry is considered; no silent 20-entry truncation.

        A cap silently ignored later fixes, so an older hold could never be
        released even though its validator fix was present and verifiable.
        """

        scenario = self.base_scenario()
        fixes = [
            {
                "id": "unrelated-fix-" + str(index),
                "pull_request": 900 + index,
                "merge_sha": ("%040x" % (index + 1)),
                "reasons": ["unrelated-reason-" + str(index)],
            }
            for index in range(25)
        ]
        fixes.append(manifest()["fixes"][0])
        scenario["manifest"] = {"schema_version": 1, "fixes": fixes}
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertFalse(result[name]["blocked"], name)
            self.assertTrue(result[name]["release"]["released"], name)
            self.assertEqual(
                result[name]["release"]["fix"]["id"], FIX_ID, name
            )

    def test_unverifiable_checkpoint_ref_never_releases_a_verified_fix(self):
        """A failed checkpoint-ref read must keep the hold, not authorize it.

        The release is bound to the checkpoint SHA recorded in the hold. If the
        current ref tip cannot be read, the controller cannot prove that the
        checkpoint is unchanged, so the merged-fix evidence alone must not
        release it.
        """

        scenario = self.base_scenario()
        scenario["refError"] = 403
        result = self.run_scenario(scenario)
        self.assertTrue(result["scheduler"]["blocked"])
        self.assertTrue(result["watchdog"]["blocked"])
        self.assertIsNone(result["scheduler"]["release"])
        self.assertIsNone(result["watchdog"]["release"])
        self.assertEqual(
            [c for c in result["comments"] if "resume-release" in c["body"]],
            [],
        )

    def test_changed_consumer_base_never_releases_the_hold(self):
        scenario = self.base_scenario()
        scenario["baseSha"] = "f" * 40
        result = self.run_scenario(scenario)
        for name in ("scheduler", "watchdog"):
            self.assertTrue(result[name]["blocked"], name)
            self.assertFalse(result[name]["release"]["released"], name)
            self.assertEqual(result[name]["release"]["reason"], "base-changed")
        self.assertEqual(
            [c for c in result["comments"] if "resume-release" in c["body"]],
            [],
        )

    def test_unavailable_consumer_base_never_releases_the_hold(self):
        for status in (403, 404, 500):
            with self.subTest(status=status):
                scenario = self.base_scenario()
                scenario["baseError"] = status
                result = self.run_scenario(scenario)
                for name in ("scheduler", "watchdog"):
                    self.assertTrue(result[name]["blocked"], name)
                    self.assertFalse(
                        result[name]["release"]["released"], name
                    )
                    self.assertEqual(
                        result[name]["release"]["reason"],
                        "base-unavailable",
                    )
                self.assertEqual(
                    [
                        c for c in result["comments"]
                        if "resume-release" in c["body"]
                    ],
                    [],
                )

    def test_a_reused_release_claim_requires_the_unchanged_consumer_base(self):
        for name, update, reason in (
            ("changed", {"baseSha": "f" * 40}, "base-changed"),
            ("unavailable", {"baseError": 403}, "base-unavailable"),
        ):
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario.update(update)
                # A durable claim already exists. Reusing it is authorized
                # only while the consumer base it rejected against is
                # provably unchanged.
                scenario["comments"].append(
                    {
                        "id": 2,
                        "body": release_marker(),
                        "user": {"login": "github-actions[bot]"},
                        "author_association": "OWNER",
                        "created_at": "2026-11-17T09:05:00Z",
                    }
                )
                result = self.run_scenario(scenario)
                for controller in ("scheduler", "watchdog"):
                    self.assertTrue(result[controller]["blocked"], controller)
                    self.assertFalse(
                        result[controller]["release"]["released"], controller
                    )
                    self.assertEqual(
                        result[controller]["release"]["reason"],
                        reason,
                        controller,
                    )
                releases = [
                    c for c in result["comments"]
                    if "resume-release" in c["body"]
                ]
                self.assertEqual(len(releases), 1)
                self.assertEqual(int(releases[0]["id"]), 2)

    def test_comment_claim_or_arbitrary_commit_is_not_fix_provenance(self):
        scenario = self.base_scenario()
        scenario["manifest"] = None
        scenario["comments"].extend(
            [
                {
                    "id": 2,
                    "body": "The validator is fixed on main; release the hold.",
                    "user": {"login": "kodmial"},
                    "author_association": "OWNER",
                    "created_at": "2026-11-17T09:30:00Z",
                },
                {
                    "id": 3,
                    "body": release_marker(),
                    "user": {"login": "kodmial"},
                    "author_association": "OWNER",
                    "created_at": "2026-11-17T09:40:00Z",
                },
            ]
        )
        result = self.run_scenario(scenario)
        self.assertTrue(result["scheduler"]["blocked"])
        self.assertTrue(result["watchdog"]["blocked"])
        self.assertEqual(
            [
                c["body"] for c in result["comments"]
                if "resume-release" in c["body"]
            ],
            [release_marker()],
            "a marker without a verifiable manifest entry must not be accepted",
        )

    def test_altered_checkpoint_or_snapshot_never_matches_a_forged_claim(self):
        for name, update in (
            ("checkpoint", {"checkpoint_sha": "b" * 40}),
            ("snapshot", {"snapshot": "c" * 64}),
            ("base", {"base_sha": "e" * 40}),
            ("issue", {"issue": 999}),
            ("repo", {"repo": "kodmial/other"}),
            ("fix-sha", {"fix_sha": "d" * 40}),
        ):
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario["manifest"] = None
                scenario["comments"].append(
                    {
                        "id": 2,
                        "body": release_marker(**update),
                        "user": {"login": "github-actions[bot]"},
                        "author_association": "OWNER",
                        "created_at": "2026-11-17T12:00:00Z",
                    }
                )
                result = self.run_scenario(scenario)
                self.assertTrue(result["scheduler"]["blocked"], name)
                self.assertTrue(result["watchdog"]["blocked"], name)
                self.assertEqual(
                    [
                        c for c in result["comments"]
                        if "resume-release" in c["body"]
                    ],
                    [scenario["comments"][-1]],
                    name,
                )

    def test_a_replayed_release_still_refuses_a_live_competing_run(self):
        for name, update, reason in (
            ("live", {"liveOverlap": True}, "release-blocked-by-live-overlap"),
            ("unverifiable", {"liveOverlapError": 403},
             "release-overlap-unverified"),
        ):
            with self.subTest(name=name):
                scenario = self.base_scenario()
                scenario["comments"].append(
                    {
                        "id": 2,
                        "body": release_marker(owner="opencode-watchdog"),
                        "user": {"login": "github-actions[bot]"},
                        "author_association": "OWNER",
                        "created_at": "2026-11-17T12:00:00Z",
                    }
                )
                scenario.update(update)
                result = self.run_scenario(scenario)
                # The scheduler has no live-overlap callback in this harness,
                # so only the watchdog is expected to keep the hold.
                self.assertTrue(result["watchdog"]["blocked"], name)
                self.assertFalse(
                    result["watchdog"]["release"]["released"], name
                )
                self.assertEqual(
                    result["watchdog"]["release"]["reason"], reason, name
                )
                self.assertEqual(
                    len(
                        [
                            c for c in result["comments"]
                            if "resume-release" in c["body"]
                        ]
                    ),
                    1,
                    name,
                )

    def test_release_ownership_window_is_bounded(self):
        scenario = self.base_scenario()
        stale = (
            datetime.now(timezone.utc) - timedelta(minutes=16)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        scenario["comments"].append(
            {
                "id": 2,
                "body": release_marker(owner="opencode-watchdog"),
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": stale,
            }
        )
        result = self.run_scenario(scenario)
        self.assertFalse(result["scheduler"]["release"]["fresh"])
        self.assertFalse(result["watchdog"]["release"]["fresh"])
        self.assertEqual(
            result["scheduler"]["release"]["owner"], "opencode-watchdog"
        )

    def production_hold_marker(self):
        """Run the production validator and return its real rejection marker.

        The release is bound to the validator's own deterministic reason and
        to the exact checkpoint/base/snapshot identity it recorded. Building
        the fixture here keeps the regression tied to
        ``src/continuum/checkpoint_resume.py`` instead of a synthetic string.
        """
        workdir = Path(
            tempfile.mkdtemp(prefix="validator-", dir=str(TMP_PARENT))
        )
        self.addCleanup(shutil.rmtree, workdir, True)
        repo = workdir / "repo"
        repo.mkdir()

        def git(*args):
            completed = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return (completed.stdout or "").strip()

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "value.txt").write_text("saved work\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        metadata = {
            "schema_version": 1,
            "mode": "issue",
            "issue_number": str(ISSUE),
            "generation": 1,
            "operation_id": OPERATION,
            "stage": "implementation-incomplete",
            "base_ref": "main",
            "base_sha": base,
            "head_sha": base,
            "branch": "opencode/issue534-run",
            "head_ref": "opencode/issue534-run",
            "checkpoint_ref": CHECKPOINT_REF,
        }
        (repo / checkpoint_resume.METADATA_FILE_NAME).write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        git("add", "-A")
        git("commit", "-m", "checkpoint saved work")
        checkpoint = git("rev-parse", "HEAD")
        request = checkpoint_resume.ResumeRequest(
            repository=OWNER + "/" + REPO,
            issue=ISSUE,
            generation=2,
            operation=OPERATION,
            checkpoint_ref=CHECKPOINT_REF,
            checkpoint_sha=checkpoint,
            base_sha=base,
            snapshot_spec=SNAPSHOT,
            mode="issue",
        )
        with self.assertRaises(checkpoint_resume.ResumeError) as raised:
            checkpoint_resume.validate_checkpoint(repo, request)
        self.assertEqual("wrong-generation", raised.exception.reason)
        self.assertTrue(checkpoint_resume.should_hold(raised.exception))
        return (
            checkpoint_resume.failure_marker(raised.exception.record),
            checkpoint,
            base,
        )

    def test_production_validator_reason_is_the_release_identity(self):
        marker, checkpoint, base = self.production_hold_marker()
        self.assertIn("reason=wrong-generation", marker)
        self.assertIn("checkpoint_sha=" + checkpoint, marker)
        self.assertIn("snapshot=" + SNAPSHOT, marker)

        scenario = self.base_scenario()
        scenario["holdSha"] = checkpoint
        scenario["baseSha"] = base
        scenario["comments"] = [
            {
                "id": 11,
                "body": marker,
                "user": {"login": "github-actions[bot]"},
                "author_association": "OWNER",
                "created_at": "2026-11-17T09:00:00Z",
            }
        ]
        held = self.run_scenario(dict(scenario, manifest=None))
        self.assertTrue(held["scheduler"]["blocked"])
        self.assertTrue(held["watchdog"]["blocked"])
        self.assertEqual(
            [c for c in held["comments"] if "resume-release" in c["body"]],
            [],
        )

        released = self.run_scenario(scenario)
        self.assertFalse(released["scheduler"]["blocked"])
        self.assertTrue(released["scheduler"]["release"]["released"])
        self.assertFalse(released["watchdog"]["blocked"])
        bodies = [
            c["body"] for c in released["comments"]
            if "resume-release" in c["body"]
        ]
        self.assertEqual(len(bodies), 1)
        self.assertIn("checkpoint_sha=" + checkpoint, bodies[0])
        self.assertIn("base_sha=" + base, bodies[0])
        self.assertIn("snapshot=" + SNAPSHOT, bodies[0])
        self.assertIn("reason=wrong-generation", bodies[0])

    def test_controller_stops_precede_hold_release(self):
        scheduler = SCHEDULER.read_text(encoding="utf-8")
        watchdog = WATCHDOG.read_text(encoding="utf-8")

        self.assertLess(
            scheduler.index("if (localStop.stopped) {"),
            scheduler.index(
                "const candidateResumeHold = await checkpointResumeHold("
            ),
            "a terminal generation stop must be evaluated before selection "
            "can release a deterministic checkpoint hold",
        )
        self.assertLess(
            scheduler.index(
                "const freshStop = await terminalGenerationStop("
            ),
            scheduler.index(
                "const freshResumeHold = await checkpointResumeHold("
            ),
            "a terminal generation stop must be re-checked before a fresh "
            "pre-dispatch release",
        )

        recovery = watchdog[
            watchdog.index("async function recoverFrom429OnFreshVm"):
            watchdog.index("const hold = await checkpointResumeHold(")
        ]
        self.assertIn("hasEffectiveOperatorStop(", recovery)
        self.assertIn("will not dispatch a resumption", recovery)

        normal = watchdog[
            watchdog.index("const operatorStopRe ="):
            watchdog.index("const checkpointHold = await checkpointResumeHold(")
        ]
        self.assertIn("effective operator cancellation stop", normal)

    def test_ready_only_candidate_defers_to_the_recovery_watchdog(self):
        """A ready-only AA #534-style candidate must not restart from main.

        The scheduler reconciler removes the reservation of an issue whose
        checkpoint it can no longer prove is live, so a ready-only candidate
        with a trusted durable 429 recovery marker is exactly the moment the
        old deadlock re-appeared: the scheduler dispatched a fresh generation
        from the default branch beside the watchdog's checkpoint resumption.
        Admission therefore has to defer to the watchdog for every candidate
        whose verified recovery owns the checkpoint -- before the hold
        decision and before selection can authorize any dispatch.
        """

        scheduler = SCHEDULER.read_text(encoding="utf-8")
        start = scheduler.index(
            "const candidateComments = await readGithub.paginate("
        )
        selection = "selected.push(candidate);"
        end = scheduler.rindex(selection) + len(selection)
        loop = scheduler[start:end]

        self.assertIn("latestSchedulerRecoveryState(candidateComments)", loop)
        self.assertIn("evaluateScheduler429Recovery({", loop)
        self.assertIn("keepReservation", loop)
        self.assertIn("checkpoint-unchanged", loop)
        self.assertIn("owned by the recovery watchdog", loop)
        self.assertLess(
            loop.index("latestSchedulerRecoveryState(candidateComments)"),
            loop.index("const candidateResumeHold = await checkpointResumeHold("),
            "a verified durable 429 checkpoint must defer admission before "
            "the resume-hold decision can authorize a dispatch",
        )
        self.assertLess(
            loop.index("owned by the recovery watchdog"),
            loop.index(selection),
            "the watchdog deferral must happen before the candidate is "
            "selected for dispatch",
        )
        # The deferral is a skip, not a selection: no fallthrough path may
        # reach selection while the watchdog owns the checkpoint.
        deferral = loop[
            loop.index("const candidateRecovery = latestSchedulerRecoveryState("):
            loop.index(
                "const candidateResumeHold = await checkpointResumeHold("
            )
        ]
        self.assertIn("continue;", deferral)


if __name__ == "__main__":
    unittest.main()
