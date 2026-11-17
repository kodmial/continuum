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
) -> str:
    return (
        "<!-- continuum-checkpoint-resume-release v=1 "
        "repo=kodmial/continuum issue=" + str(issue) + " generation=2 " +
        "operation=" + OPERATION + " checkpoint=" + CHECKPOINT_REF +
        " checkpoint_sha=" + checkpoint_sha + " base_ref=main base_sha=" +
        BASE_SHA + " snapshot=" + snapshot + " reason=wrong-generation " +
        "fix=" + FIX_ID + " fix_pr=461 fix_sha=" + fix_sha +
        " owner=" + owner + " -->"
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

function apiError(status, message) {
  const error = new Error(message || ('HTTP ' + status));
  error.status = status;
  return error;
}

const comments = (scenario.comments || []).map(comment => ({ ...comment }));
let nextCommentId = scenario.nextCommentId || 1000;
let raceInjected = false;

const listReviews = async () => ({ data: scenario.reviews || [] });
const listFiles = async () => ({ data: scenario.files || [] });
const listComments = async () => ({ data: comments.map(c => ({ ...c })) });

const readGithub = {
  rest: {
    git: {
      getRef: async () => {
        if (scenario.refError) throw apiError(scenario.refError, 'ref failed');
        return {
          data: { object: { sha: scenario.refSha || scenario.holdSha } },
        };
      },
    },
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
    issues: { listComments },
  },
};
readGithub.paginate = async (method) => {
  if (method === listReviews) return (await listReviews()).data;
  if (method === listFiles) return (await listFiles()).data;
  if (method === listComments) return (await listComments()).data;
  throw new Error('unexpected paginate call');
};

const writeGithub = {
  rest: {
    issues: {
      createComment: async ({ body }) => {
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
        const index = comments.findIndex(
          item => Number(item.id) === Number(comment_id)
        );
        if (index >= 0) comments.splice(index, 1);
        return { data: {} };
      },
    },
  },
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
    'github', 'core', 'owner', 'readGithub',
    'const isTrustedStopComment = (comment, trustedOwner) => {' +
      'const association = String(comment.author_association || "").toUpperCase();' +
      'if (["OWNER", "MEMBER", "COLLABORATOR"].includes(association)) return true;' +
      'const login = String((comment.user || {}).login || "").toLowerCase();' +
      'return login === "github-actions[bot]" || login === String(trustedOwner || "").toLowerCase();' +
    '};' +
    'const stopOrderKey = entry => String(entry.created_at || "") + "#" + ' +
      'String(entry.id || 0).padStart(12, "0");\n' +
    schedulerSource +
    '\nreturn { hold: checkpointResumeHold, released: () => checkpointResumeHoldRelease };'
  );
  const api = await factory(writeGithub, core, scenario.owner || 'kodmial', readGithub);
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
    { controller: 'opencode-watchdog' }
  );
  return {
    blocked: Boolean(result && result.blocked),
    hold: result && result.marker || null,
    release: result && result.release || null,
  };
}

(async () => {
  const result = {
    scheduler: await runScheduler(),
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

    def run_scenario(self, scenario):
        workdir = Path(tempfile.mkdtemp(prefix="hold-release-", dir=str(TMP_PARENT)))
        try:
            scenario_path = workdir / "scenario.json"
            scheduler_path = workdir / "scheduler.js"
            watchdog_path = workdir / "watchdog.js"
            harness_path = workdir / "harness.js"
            result_path = workdir / "result.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            scheduler_path.write_text(self.scheduler_source, encoding="utf-8")
            watchdog_path.write_text(self.watchdog_source, encoding="utf-8")
            harness_path.write_text(NODE_HARNESS, encoding="utf-8")
            completed = subprocess.run(
                [
                    "node", str(harness_path), str(scenario_path),
                    str(scheduler_path), str(watchdog_path),
                    str(result_path),
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
            ("issue", {"issue": 999}),
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


if __name__ == "__main__":
    unittest.main()
