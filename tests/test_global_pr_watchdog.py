"""Executable regression tests for the global PR watchdog (#256).

Drives `.github/scripts/global_pr_watchdog.js` through Node with an injected
fake REST client, so every behavior below is verified without network access
and without naming any real consumer repository (all fixtures use synthetic
owner/repo names).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HELPER = os.path.join(ROOT, ".github", "scripts", "global_pr_watchdog.js")

CONTINUUM_REPO = "kodmial/continuum"
CONTINUUM_REPO_ID = 900001

CONSUMER_MARKER_TEXT = (
    "name: Auto-merge reviewed pull requests\n"
    "jobs:\n"
    "  call:\n"
    "    uses: kodmial/continuum/.github/workflows/continuum-auto-merge.yml@main\n"
)

HARNESS = r"""
const watchdog = require(process.env.WATCHDOG_HELPER);
const scenario = JSON.parse(process.env.SCENARIO_JSON || '{}');
const logs = [];
const requests = [];
const log = {
  info(m) { logs.push({ level: 'info', msg: String(m) }); },
  warn(m) { logs.push({ level: 'warn', msg: String(m) }); },
};
function httpErr(status, op, remaining) {
  const headers = (remaining === undefined || remaining === null)
    ? {}
    : { 'x-ratelimit-remaining': String(remaining) };
  return new watchdog.HttpError(status, op, headers);
}
async function runScanMode() {
  let rateCalls = 0;
  const rates = () => {
    const seq = scenario.rateSequence || null;
    let value;
    if (seq && seq.length) {
      value = seq[Math.min(rateCalls, seq.length - 1)];
    } else {
      value = scenario.rate || { limit: 5000, remaining: 4000, reset: 0 };
    }
    rateCalls += 1;
    return value;
  };
  const markers = scenario.markers || {};
  const pulls = scenario.pulls || {};
  const dispatches = scenario.dispatches || {};
  const client = {
    async getRateLimit() {
      requests.push({ method: 'GET', path: '/rate_limit' });
      return rates();
    },
    async listRepositories(page) {
      const path = watchdog.listReposPath(page);
      requests.push({ method: 'GET', path });
      const generated = scenario.generatedPages;
      if (generated && page <= generated.count) {
        const out = [];
        for (let i = 0; i < generated.perPage; i++) {
          const id = generated.startId + (page - 1) * generated.perPage + i;
          out.push({
            id, owner: 'owner-gen', name: 'repo-gen-' + id, default_branch: 'main',
            archived: false, disabled: false, permissions: { push: true },
          });
        }
        return out;
      }
      const pages = scenario.pages || [];
      return pages[page - 1] || [];
    },
    async getConsumerMarker(owner, repo, ref) {
      requests.push({ method: 'GET', path: watchdog.consumerMarkerPath(owner, repo, ref) });
      const entry = markers[owner + '/' + repo];
      if (!entry || entry.status === 404) throw httpErr(404, 'consumer_marker');
      if (entry.status !== 200) throw httpErr(entry.status, 'consumer_marker', entry.remaining);
      return entry.text || '';
    },
    async probeOpenPulls(owner, repo) {
      requests.push({ method: 'GET', path: watchdog.openPrProbePath(owner, repo) });
      const entry = pulls[owner + '/' + repo];
      if (entry && typeof entry === 'object' && entry.status && entry.status !== 200) {
        throw httpErr(entry.status, 'open_pr_probe', entry.remaining);
      }
      return entry === true;
    },
    async dispatchWorkflow(owner, repo, file, ref) {
      requests.push({ method: 'POST', path: watchdog.dispatchPath(owner, repo, file) });
      const entry = (dispatches[owner + '/' + repo] || {})[file];
      if (entry === undefined || entry === 'ok') return;
      if (entry && typeof entry === 'object') throw httpErr(entry.status, 'dispatch', entry.remaining);
      throw httpErr(entry, 'dispatch', null);
    },
  };
  try {
    const result = await watchdog.runScan(client, {
      continuumRepo: scenario.continuumRepo || 'kodmial/continuum',
      continuumRepoId: scenario.continuumRepoId === undefined ? 900001 : scenario.continuumRepoId,
      log,
    });
    const approved = requests.map((r) => watchdog.isApprovedEndpoint(r.method, r.path));
    process.stdout.write(JSON.stringify({ ok: true, result, logs, requests, approved, rateCalls }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      ok: false, error: String((error && error.message) || error), logs, requests, rateCalls,
    }));
  }
}
async function runPureMode() {
  const outputs = [];
  for (const check of scenario.checks || []) {
    const args = check.args || [];
    let value;
    let threw = false;
    try {
      if (check.fn === 'lowWaterMark') value = watchdog.lowWaterMark(args[0]);
      else if (check.fn === 'repoKey') value = watchdog.repoKey(args[0]);
      else if (check.fn === 'isConsumer') value = watchdog.isContinuumConsumerFile(args[0], args[1]);
      else if (check.fn === 'isApproved') value = watchdog.isApprovedEndpoint(args[0], args[1]);
      else if (check.fn === 'assertApproved') {
        watchdog.assertApprovedEndpoint(args[0], args[1]);
        value = 'ok';
      } else if (check.fn === 'headers') {
        const built = watchdog.buildHeaders(args[0]);
        value = { accept: built.Accept, version: built['X-GitHub-Api-Version'], hasAuth: Boolean(built.Authorization) };
      } else value = 'unknown-fn';
    } catch (error) {
      threw = true;
      value = String((error && error.message) || error);
    }
    outputs.push({ value, threw });
  }
  process.stdout.write(JSON.stringify({ ok: true, outputs }));
}
(async () => {
  if ((scenario.mode || 'scan') === 'pure') await runPureMode();
  else await runScanMode();
})();
"""


def run_node(scenario):
    env = os.environ.copy()
    env["WATCHDOG_HELPER"] = HELPER
    env["SCENARIO_JSON"] = json.dumps(scenario)
    completed = subprocess.run(
        ["node", "-e", HARNESS],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=60,
    )
    assert completed.returncode == 0, f"node harness failed: {completed.stderr}"
    return json.loads(completed.stdout)


def run_pure(checks):
    return run_node({"mode": "pure", "checks": checks})


def repo(repo_id, owner="owner-alpha", name="repo-alpha", **overrides):
    record = {
        "id": repo_id,
        "owner": owner,
        "name": name,
        "default_branch": "main",
        "archived": False,
        "disabled": False,
        "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True},
    }
    record.update(overrides)
    return record


def log_text(outcome):
    return "\n".join(entry["msg"] for entry in outcome["logs"])


class GlobalPrWatchdogTest(unittest.TestCase):
    def test_low_water_mark_is_ten_percent_with_500_floor(self):
        outcome = run_pure([
            {"fn": "lowWaterMark", "args": [5000]},
            {"fn": "lowWaterMark", "args": [1000]},
            {"fn": "lowWaterMark", "args": [60000]},
        ])
        values = [entry["value"] for entry in outcome["outputs"]]
        self.assertEqual(values, [500, 500, 6000])

    def test_repo_key_is_truncated_sha256(self):
        outcome = run_pure([{"fn": "repoKey", "args": [12345]}])
        expected = hashlib.sha256(b"12345").hexdigest()[:12]
        self.assertEqual(outcome["outputs"][0]["value"], expected)

    def test_api_headers_use_versioned_json_contract(self):
        outcome = run_pure([{"fn": "headers", "args": ["fake-token"]}])
        value = outcome["outputs"][0]["value"]
        self.assertEqual(value["accept"], "application/vnd.github+json")
        self.assertEqual(value["version"], "2026-03-10")
        self.assertTrue(value["hasAuth"])

    def test_consumer_marker_must_reference_current_continuum_workflow(self):
        other_source = (
            "jobs:\n  call:\n"
            "    uses: other-org/other-repo/.github/workflows/continuum-auto-merge.yml@main\n"
        )
        no_uses = "name: something else\njobs:\n  probe:\n    runs-on: ubuntu-latest\n"
        outcome = run_pure([
            {"fn": "isConsumer", "args": [CONSUMER_MARKER_TEXT, CONTINUUM_REPO]},
            {"fn": "isConsumer", "args": [other_source, CONTINUUM_REPO]},
            {"fn": "isConsumer", "args": [no_uses, CONTINUUM_REPO]},
            {"fn": "isConsumer", "args": ["", CONTINUUM_REPO]},
        ])
        values = [entry["value"] for entry in outcome["outputs"]]
        self.assertEqual(values, [True, False, False, False])

    def test_endpoint_allowlist_rejects_history_rerun_and_mutation_paths(self):
        outcome = run_pure([
            {"fn": "isApproved", "args": ["GET", "/rate_limit"]},
            {"fn": "isApproved", "args": ["GET", "/user/repos?per_page=100&page=1&sort=pushed&direction=desc"]},
            {"fn": "isApproved", "args": ["GET", "/repos/o/r/contents/.github/workflows/continuum-auto-merge.yml?ref=main"]},
            {"fn": "isApproved", "args": ["GET", "/repos/o/r/pulls?state=open&sort=updated&direction=desc&per_page=1&page=1"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/workflows/continuum-auto-merge.yml/dispatches"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/workflows/continuum-pr-agent-recovery.yml/dispatches"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/workflows/continuum-coderabbit-retry.yml/dispatches"]},
            {"fn": "isApproved", "args": ["GET", "/repos/o/r/actions/runs?per_page=30"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/runs/7/rerun"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/runs/7/rerun-failed-jobs"]},
            {"fn": "isApproved", "args": ["PUT", "/repos/o/r/pulls/3/merge"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/pulls/3/comments"]},
            {"fn": "isApproved", "args": ["POST", "/repos/o/r/actions/workflows/continuum-opencode-repair.yml/dispatches"]},
        ])
        values = [entry["value"] for entry in outcome["outputs"]]
        self.assertEqual(values, [True] * 7 + [False] * 6)

    def test_assert_approved_endpoint_throws_for_forbidden_path(self):
        outcome = run_pure([
            {"fn": "assertApproved", "args": ["GET", "/repos/o/r/actions/runs"]},
        ])
        self.assertTrue(outcome["outputs"][0]["threw"])

    def test_repository_names_never_appear_in_logs_on_success(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(11, "secret-owner-one", "private-repo-one"), repo(12, "secret-owner-two", "private-repo-two")]],
            "markers": {
                "secret-owner-one/private-repo-one": {"status": 200, "text": CONSUMER_MARKER_TEXT},
                "secret-owner-two/private-repo-two": {"status": 404},
            },
            "pulls": {"secret-owner-one/private-repo-one": True},
        })
        self.assertTrue(outcome["ok"])
        text = log_text(outcome)
        for secret in ("secret-owner-one", "private-repo-one", "secret-owner-two", "private-repo-two"):
            self.assertNotIn(secret, text)
        self.assertTrue(all(outcome["approved"]))

    def test_repository_names_never_appear_in_logs_on_failure(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(21, "secret-owner-nine", "private-repo-nine")]],
            "markers": {"secret-owner-nine/private-repo-nine": {"status": 500}},
            "pulls": {},
        })
        self.assertTrue(outcome["ok"])
        text = log_text(outcome)
        self.assertNotIn("secret-owner-nine", text)
        self.assertNotIn("private-repo-nine", text)
        # The only per-repository diagnostic is the opaque key plus status/op.
        warnings = [entry["msg"] for entry in outcome["logs"] if entry["level"] == "warn"]
        self.assertEqual(len(warnings), 1)
        self.assertRegex(warnings[0], r"repo=[0-9a-f]{12} op=consumer_marker status=500")

    def test_discovery_is_capped_at_ten_pages(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "generatedPages": {"count": 11, "perPage": 100, "startId": 5000},
            "markers": {},
        })
        self.assertTrue(outcome["ok"])
        list_calls = [r for r in outcome["requests"] if r["path"].startswith("/user/repos?")]
        self.assertEqual(len(list_calls), 10)
        self.assertTrue(outcome["result"]["truncatedDiscovery"])
        self.assertEqual(outcome["result"]["counters"]["repositories_seen"], 1000)

    def test_low_rate_budget_prevents_discovery_and_dispatch(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "rate": {"limit": 5000, "remaining": 400, "reset": 0},
            "pages": [[repo(31)]],
            "markers": {"owner-alpha/repo-alpha": {"status": 200, "text": CONSUMER_MARKER_TEXT}},
            "pulls": {"owner-alpha/repo-alpha": True},
        })
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["lowBudget"], True)
        self.assertEqual(outcome["result"]["counters"]["skipped_low_rate_budget"], 1)
        self.assertFalse(any(r["path"].startswith("/user/repos?") for r in outcome["requests"]))
        self.assertFalse(any(r["method"] == "POST" for r in outcome["requests"]))

    def test_rate_limit_is_rechecked_after_25_inspected_repositories(self):
        records = [repo(4000 + index, "owner-batch", f"repo-batch-{index}") for index in range(30)]
        markers = {f"owner-batch/repo-batch-{index}": {"status": 404} for index in range(30)}
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "rateSequence": [
                {"limit": 5000, "remaining": 4000, "reset": 0},
                {"limit": 5000, "remaining": 100, "reset": 0},
            ],
            "pages": [records],
            "markers": markers,
        })
        self.assertTrue(outcome["ok"])
        marker_calls = [r for r in outcome["requests"] if "/contents/" in r["path"]]
        self.assertEqual(len(marker_calls), 25)
        self.assertEqual(outcome["rateCalls"], 2)
        self.assertEqual(outcome["result"]["lowBudget"], True)

    def test_primary_limit_403_stops_the_pass_without_retry(self):
        first = repo(51, "owner-stop", "repo-stop-one")
        second = repo(52, "owner-stop", "repo-stop-two")
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[first, second]],
            "markers": {
                "owner-stop/repo-stop-one": {"status": 403, "remaining": 0},
                "owner-stop/repo-stop-two": {"status": 200, "text": CONSUMER_MARKER_TEXT},
            },
            "pulls": {"owner-stop/repo-stop-two": True},
        })
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["result"]["stoppedRateLimited"])
        marker_calls = [r for r in outcome["requests"] if "/contents/" in r["path"]]
        self.assertEqual(len(marker_calls), 1)
        self.assertFalse(any(r["method"] == "POST" for r in outcome["requests"]))

    def test_429_stops_the_pass_without_retry(self):
        first = repo(61, "owner-quota", "repo-quota-one")
        second = repo(62, "owner-quota", "repo-quota-two")
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[first, second]],
            "markers": {
                "owner-quota/repo-quota-one": {"status": 200, "text": CONSUMER_MARKER_TEXT},
                "owner-quota/repo-quota-two": {"status": 404},
            },
            "pulls": {"owner-quota/repo-quota-one": {"status": 429}},
        })
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["result"]["stoppedRateLimited"])
        probe_calls = [r for r in outcome["requests"] if "/pulls?" in r["path"]]
        self.assertEqual(len(probe_calls), 1)
        self.assertFalse(any(r["method"] == "POST" for r in outcome["requests"]))

    def test_non_consumer_404_costs_no_further_calls(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(71, "owner-plain", "repo-plain")]],
            "markers": {"owner-plain/repo-plain": {"status": 404}},
        })
        self.assertTrue(outcome["ok"])
        repo_calls = [r for r in outcome["requests"]
                      if not r["path"].startswith("/user/repos?") and r["path"] != "/rate_limit"]
        self.assertEqual(len(repo_calls), 1)
        self.assertEqual(outcome["result"]["counters"]["consumers_seen"], 0)

    def test_marker_without_continuum_reference_costs_no_probe(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(72, "owner-other", "repo-other")]],
            "markers": {"owner-other/repo-other": {
                "status": 200,
                "text": "jobs:\n  call:\n    uses: other-org/other-repo/.github/workflows/continuum-auto-merge.yml@main\n",
            }},
        })
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["counters"]["consumers_seen"], 0)
        self.assertFalse(any("/pulls?" in r["path"] for r in outcome["requests"]))

    def test_repository_without_open_pr_causes_no_dispatch(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(81, "owner-quiet", "repo-quiet")]],
            "markers": {"owner-quiet/repo-quiet": {"status": 200, "text": CONSUMER_MARKER_TEXT}},
            "pulls": {"owner-quiet/repo-quiet": False},
        })
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["counters"]["consumers_seen"], 1)
        self.assertEqual(outcome["result"]["counters"]["consumers_with_open_pr"], 0)
        self.assertFalse(any(r["method"] == "POST" for r in outcome["requests"]))

    def test_open_pr_dispatches_exactly_the_three_allowed_reconcilers(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(91, "owner-live", "repo-live", default_branch="main")]],
            "markers": {"owner-live/repo-live": {"status": 200, "text": CONSUMER_MARKER_TEXT}},
            "pulls": {"owner-live/repo-live": True},
        })
        self.assertTrue(outcome["ok"])
        posts = [r for r in outcome["requests"] if r["method"] == "POST"]
        self.assertEqual(
            [r["path"] for r in posts],
            [
                "/repos/owner-live/repo-live/actions/workflows/continuum-auto-merge.yml/dispatches",
                "/repos/owner-live/repo-live/actions/workflows/continuum-pr-agent-recovery.yml/dispatches",
                "/repos/owner-live/repo-live/actions/workflows/continuum-coderabbit-retry.yml/dispatches",
            ],
        )
        self.assertEqual(outcome["result"]["counters"]["reconciler_dispatches"], 3)
        self.assertEqual(outcome["result"]["counters"]["consumers_with_open_pr"], 1)
        # Per-repository budget: 1 marker + 1 probe + 3 dispatches.
        repo_calls = [r for r in outcome["requests"]
                      if not r["path"].startswith("/user/repos?") and r["path"] != "/rate_limit"]
        self.assertEqual(len(repo_calls), 5)
        self.assertTrue(all(outcome["approved"]))

    def test_missing_optional_reconciler_does_not_abort_the_others(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(101, "owner-old", "repo-old")]],
            "markers": {"owner-old/repo-old": {"status": 200, "text": CONSUMER_MARKER_TEXT}},
            "pulls": {"owner-old/repo-old": True},
            "dispatches": {"owner-old/repo-old": {
                "continuum-auto-merge.yml": "ok",
                "continuum-pr-agent-recovery.yml": 404,
                "continuum-coderabbit-retry.yml": "ok",
            }},
        })
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["counters"]["missing_reconciler"], 1)
        self.assertEqual(outcome["result"]["counters"]["reconciler_dispatches"], 2)
        posts = [r for r in outcome["requests"] if r["method"] == "POST"]
        self.assertEqual(len(posts), 3)

    def test_5xx_on_one_repository_does_not_block_the_next(self):
        failing = repo(111, "owner-flaky", "repo-flaky")
        healthy = repo(112, "owner-steady", "repo-steady")
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[failing, healthy]],
            "markers": {
                "owner-flaky/repo-flaky": {"status": 500},
                "owner-steady/repo-steady": {"status": 200, "text": CONSUMER_MARKER_TEXT},
            },
            "pulls": {"owner-steady/repo-steady": True},
        })
        self.assertTrue(outcome["ok"])
        flaky_markers = [r for r in outcome["requests"]
                         if r["path"].startswith("/repos/owner-flaky/repo-flaky/contents/")]
        self.assertEqual(len(flaky_markers), 1)
        self.assertEqual(outcome["result"]["counters"]["reconciler_dispatches"], 3)
        self.assertEqual(
            outcome["result"]["counters"]["request_failures_by_status"].get("500"), 1)

    def test_invalid_credential_fails_the_run(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(121, "owner-locked", "repo-locked")]],
            "markers": {"owner-locked/repo-locked": {"status": 401}},
        })
        self.assertFalse(outcome["ok"])
        self.assertIn("credential is invalid", outcome["error"])
        self.assertNotIn("owner-locked", outcome["error"])
        self.assertNotIn("repo-locked", outcome["error"])

    def test_unexpected_403_fails_the_run(self):
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[repo(122, "owner-denied", "repo-denied")]],
            "markers": {"owner-denied/repo-denied": {"status": 403, "remaining": 312}},
        })
        self.assertFalse(outcome["ok"])
        self.assertIn("lacks required access", outcome["error"])

    def test_continuum_own_repository_is_excluded_by_id(self):
        own = repo(CONTINUUM_REPO_ID, "owner-self", "repo-self")
        other = repo(131, "owner-guest", "repo-guest")
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [[own, other]],
            "markers": {"owner-guest/repo-guest": {"status": 404}},
        })
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["result"]["counters"]["repositories_seen"], 2)
        self_calls = [r for r in outcome["requests"] if "/owner-self/repo-self/" in r["path"]]
        self.assertEqual(self_calls, [])

    def test_archived_and_inaccessible_repositories_cost_no_further_calls(self):
        records = [
            repo(141, "owner-arch", "repo-arch", archived=True),
            repo(142, "owner-nopush", "repo-nopush",
                 permissions={"admin": False, "maintain": False, "push": False,
                              "triage": False, "pull": True}),
        ]
        outcome = run_node({
            "continuumRepo": CONTINUUM_REPO,
            "continuumRepoId": CONTINUUM_REPO_ID,
            "pages": [records],
        })
        self.assertTrue(outcome["ok"])
        repo_calls = [r for r in outcome["requests"]
                      if not r["path"].startswith("/user/repos?") and r["path"] != "/rate_limit"]
        self.assertEqual(repo_calls, [])


if __name__ == "__main__":
    unittest.main()
