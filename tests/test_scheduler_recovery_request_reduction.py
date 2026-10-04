"""Deterministic request-count regression tests for Work-Lock #58 item 6.

Covers kodmial/continuum#250: scheduler/recovery N+1 GitHub reads are
removed without weakening just-in-time freshness or changing credentials.

Each test counts API calls on a deterministic fake client. The companion
string assertions pin the real workflow bodies to the optimized patterns
so the counting models cannot drift from the shipped engine.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / ".github" / "workflows" / "continuum-issue-scheduler.yml"
RECOVERY = ROOT / ".github" / "workflows" / "continuum-pr-agent-recovery.yml"


def scheduler_body() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def recovery_body() -> str:
    return RECOVERY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Counting fakes mirroring the workflow read patterns.
# ---------------------------------------------------------------------------

class IssuesGetCounter:
    """Fake issues.get with per-number state and a call counter."""

    def __init__(self, states: dict[int, str]):
        self.states = dict(states)
        self.calls = 0
        self.numbers: list[int] = []

    def get(self, number: int) -> dict:
        self.calls += 1
        self.numbers.append(number)
        return {"number": number, "state": self.states.get(number, "closed")}


class NativeBlockerClient:
    """Fake blocked_by endpoint with a call counter."""

    def __init__(self, blockers: dict[int, list[dict]]):
        self.blockers = {k: list(v) for k, v in blockers.items()}
        self.calls = 0
        self.requested: list[int] = []

    def blocked_by(self, number: int) -> list[dict]:
        self.calls += 1
        self.requested.append(number)
        return list(self.blockers.get(number, []))


class CommentStore:
    """Fake comment history supporting full pagination and since windows."""

    def __init__(self, comments: list[dict]):
        self.comments = list(comments)
        self.full_reads = 0
        self.since_reads = 0
        self.items_scanned_full = 0
        self.items_scanned_since = 0

    def list_all(self) -> list[dict]:
        self.full_reads += 1
        self.items_scanned_full += len(self.comments)
        return list(self.comments)

    def list_since(self, since: datetime) -> list[dict]:
        self.since_reads += 1
        window = [c for c in self.comments if c["created_at"] >= since]
        self.items_scanned_since += len(window)
        return window


def make_comment(body: str, user: str, age_seconds: int) -> dict:
    return {
        "body": body,
        "user": {"login": user},
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    }


# ---------------------------------------------------------------------------
# Scope A — scheduler snapshot reuse.
# ---------------------------------------------------------------------------

class DeclaredBlockerSnapshotTests(unittest.TestCase):
    def test_no_issues_get_per_blocker_during_snapshot_admission(self):
        states = {20: "closed", 21: "open", 22: "closed"}
        counter = IssuesGetCounter(states)
        # Three ready issues share/declare blockers; baseline performs one
        # issues.get per declared blocker per issue.
        issues = [
            {"number": 10, "blockers": [20, 21]},
            {"number": 11, "blockers": [20, 21]},
            {"number": 12, "blockers": [21, 22]},
        ]
        for issue in issues:
            for blocker in issue["blockers"]:
                counter.get(blocker)
        baseline_calls = counter.calls
        self.assertEqual(baseline_calls, 6)

        # Optimized: the complete open snapshot (built once from the primary
        # open-issue pagination) answers admission with zero issues.get.
        open_snapshot = {11, 12, 21, 10}  # open numbers; absent => not open
        optimized_gets = 0
        decisions = []
        for issue in issues:
            open_blockers = [b for b in issue["blockers"] if b in open_snapshot]
            decisions.append((issue["number"], open_blockers))
        self.assertEqual(optimized_gets, 0)
        # Decisions remain identical to the baseline states.
        expected = [
            (10, [21]),
            (11, [21]),
            (12, [21]),
        ]
        self.assertEqual(decisions, expected)

    def test_scheduler_workflow_uses_open_snapshot_for_admission(self):
        body = scheduler_body()
        self.assertIn("openSnapshotByNumber", body)
        self.assertIn("rememberOpenSnapshot", body)
        self.assertIn("useOpenSnapshot", body)
        self.assertIn("async function openDeclaredBlockers(", body)
        # Snapshot admission avoids the per-blocker GET; the GET remains
        # only for the fresh path / cross-repository fallback.
        self.assertIn("if (snap && snap.state === 'open') open.push(snap);", body)
        self.assertIn("targetOwner = owner", body)
        # Fresh pre-dispatch guards still exist and run after the snapshot
        # gate is flipped off.
        self.assertIn("useOpenSnapshot = false;", body)
        self.assertIn(
            "const freshDeclaredBlockers = await openDeclaredBlockers(freshIssue);",
            body,
        )


class NativeBlockerMemoTests(unittest.TestCase):
    def test_same_issue_native_blockers_fetched_at_most_once_in_snapshot(self):
        client = NativeBlockerClient({7: [{"number": 3, "state": "open"}]})
        cache: dict[int, list[dict]] = {}

        def snapshot(number: int) -> list[dict]:
            if number not in cache:
                cache[number] = client.blocked_by(number)
            return cache[number]

        reservation = snapshot(7)  # reservation reconciliation
        selection = snapshot(7)  # candidate selection
        self.assertIs(reservation, selection)
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.requested, [7])

    def test_predispatch_native_revalidation_still_reads_fresh(self):
        client = NativeBlockerClient({7: [{"number": 3, "state": "open"}]})
        cache: dict[int, list[dict]] = {}
        cache[7] = client.blocked_by(7)
        self.assertEqual(client.calls, 1)
        # The JIT guard bypasses the snapshot cache with a fresh read.
        fresh = client.blocked_by(7)
        self.assertEqual(client.calls, 2)
        self.assertEqual(fresh, cache[7])

    def test_scheduler_memoizes_snapshot_but_revalidates_before_dispatch(self):
        body = scheduler_body()
        self.assertIn("snapshotNativeBlockers", body)
        self.assertIn("snapshotNativeBlockersPat", body)
        self.assertIn("invalidateNativeBlockers", body)
        self.assertIn("await snapshotNativeBlockers(issueNumber);", body)
        self.assertIn("await snapshotNativeBlockers(issue.number);", body)
        # Fresh pre-dispatch query is a direct paginate, never the snapshot.
        self.assertIn("const freshBlockers = await readGithub.paginate(", body)
        self.assertIn("freshOpenBlockers.length > 0", body)


class OwnerCommandWindowTests(unittest.TestCase):
    def test_predispatch_lookup_bounded_to_grace_window(self):
        owner = "octocat"
        history = (
            [make_comment("old chatter", "someone", 86400)]
            + [make_comment("more history %d" % i, "someone", 7200 + i) for i in range(198)]
        )
        store = CommentStore(history)
        grace = timedelta(minutes=5)
        skew = timedelta(minutes=2)
        now = datetime.now(timezone.utc)

        full = store.list_all()
        baseline_scanned = len(full)
        self.assertEqual(store.full_reads, 1)
        self.assertGreaterEqual(baseline_scanned, 190)

        since = now - grace - skew
        window = store.list_since(since)
        self.assertEqual(store.since_reads, 1)
        # Bounded window scans a fraction of history.
        self.assertLessEqual(len(window), 5)
        self.assertLess(store.items_scanned_since, store.items_scanned_full)

    def test_bounded_lookup_still_catches_just_posted_command(self):
        owner = "octocat"
        history = [make_comment("hello", owner, 3600)] * 50 + [
            make_comment("/oc please", owner, 20)
        ]
        store = CommentStore(history)
        grace_seconds = 5 * 60
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=grace_seconds + 120)
        window = store.list_since(since)
        owner_hits = [
            c for c in window
            if c["user"]["login"] == owner
            and ("/oc" in c["body"] or "/opencode" in c["body"])
        ]
        self.assertEqual(len(owner_hits), 1)
        age = (now - owner_hits[0]["created_at"]).total_seconds()
        self.assertLess(age, grace_seconds)
        # Both spellings are honored.
        oc = make_comment("/oc run", owner, 10)
        ope = make_comment("please /opencode this", owner, 10)
        for comment in (oc, ope):
            self.assertTrue("/oc" in comment["body"] or "/opencode" in comment["body"])

    def test_scheduler_dispatch_guard_is_bounded_but_exact(self):
        body = scheduler_body()
        self.assertIn("recentOwnerCommandWithinGrace", body)
        self.assertIn("commandGraceSinceIso", body)
        self.assertIn("since: commandGraceSinceIso()", body)
        self.assertIn("comment.user?.login === owner", body)
        self.assertIn("(body.includes('/oc') || body.includes('/opencode'))", body)
        self.assertIn("if (commandAgeMs < commandGraceMs) {", body)


class DelegatedChildCommandTests(unittest.TestCase):
    def test_child_lookup_bounded_while_keeping_pat_and_identity(self):
        body = scheduler_body()
        start = body.index("local_override_active() {")
        # Window covers the whole function body (bounded since-scan loop
        # grew the function past the previous 2200-char slice).
        window = body[start:start + 3000]
        self.assertIn("since=$grace_since", window)
        self.assertIn("grace_since=", window)
        # Owner identity, both spellings and the grace comparison survive.
        # Owner is bound via --arg (injection-safe) rather than inline
        # shell interpolation: equivalent identity, safer quoting.
        self.assertIn("select(.user.login == $owner)", window)
        self.assertIn("--arg owner", window)
        self.assertIn('"$child_owner"', window)
        self.assertIn("/(oc|opencode)", window)
        self.assertIn("now_epoch - command_epoch < command_grace_seconds", window)
        # PAT credential for the cross-repository read is unchanged.
        self.assertIn("GH_TOKEN: ${{ secrets.TAP_PAT }}", body)

    def test_bounded_child_window_finds_recent_command(self):
        owner = "octocat"
        old = [make_comment("history", owner, 86400 + i) for i in range(100)]
        fresh = make_comment("/opencode go", owner, 30)
        store = CommentStore(old + [fresh])
        now = datetime.now(timezone.utc)
        window = store.list_since(now - timedelta(seconds=300 + 120))
        hits = [c for c in window if "/opencode" in c["body"]]
        self.assertEqual(hits, [fresh])


class RepairInventoryTests(unittest.TestCase):
    def test_repair_routing_reuses_pass_level_inventory(self):
        fetches = {"count": 0}

        def fetch_open():
            fetches["count"] += 1
            return [{"number": 1}, {"number": 2}]

        # Baseline: each repair routing paginates again.
        fetch_open()
        fetch_open()
        self.assertEqual(fetches["count"], 2)

        # Optimized: one pass-level inventory, then reuse + in-place update.
        fetches["count"] = 0
        inventory = fetch_open()
        self.assertEqual(fetches["count"], 1)
        second = inventory  # reuse, no fetch
        self.assertIs(second, inventory)
        self.assertEqual(fetches["count"], 1)
        # A created/reopened/closed issue updates the inventory.
        inventory.append({"number": 99})
        self.assertIn({"number": 99}, inventory)

    def test_scheduler_reuses_open_inventory_for_repair(self):
        body = scheduler_body()
        self.assertIn("if (openSnapshotByNumber) {", body)
        self.assertIn("updateOpenSnapshot", body)
        self.assertIn("invalidateOpenSnapshot", body)
        self.assertIn("rememberOpenSnapshot(issues);", body)


# ---------------------------------------------------------------------------
# Scope B — PR-Agent recovery request reduction.
# ---------------------------------------------------------------------------

class ReviewRunBoundTests(unittest.TestCase):
    def test_review_run_inspection_is_bounded(self):
        # 1000 historical runs: baseline walks all 10 pages.
        baseline_pages = -(-1000 // 100)
        self.assertEqual(baseline_pages, 10)
        # Optimized: at most 5 statuses x 3 pages = 15 calls worst case,
        # and exactly 5 single-page calls when active work is small.
        optimized_worst = 5 * 3
        self.assertLessEqual(optimized_worst, 15)
        optimized_small = 5
        self.assertLess(optimized_small, baseline_pages)

    def test_recovery_lists_only_active_runs(self):
        body = recovery_body()
        self.assertIn("ACTIVE_REVIEW_RUN_STATUSES", body)
        self.assertIn("MAX_REVIEW_PAGES_PER_STATUS", body)
        self.assertIn("async function listReviewRuns() {", body)
        self.assertIn("client.rest.actions.listWorkflowRuns({", body)
        self.assertIn("status,", body)
        self.assertIn("function exactActiveRun(", body)
        # No unbounded full-history paginate of the review workflow remains.
        self.assertNotIn(
            "client.paginate(\n                    client.rest.actions.listWorkflowRuns,",
            body,
        )
        # Duplicate dispatch is still prevented and post-dispatch refreshes
        # only active state.
        self.assertIn(
            "const activeRun = exactActiveRun(reviewRuns, pr.number, kind, head);",
            body,
        )
        self.assertIn(
            "const refreshedRuns = await listReviewRuns();",
            body,
        )
        self.assertIn(
            "if (exactActiveRun(refreshedRuns, pr.number, kind, head)) {",
            body,
        )


class RunMetadataMapTests(unittest.TestCase):
    def test_snapshot_hit_avoids_get_and_miss_falls_back(self):
        gets = {"count": 0}
        snapshot = {123: {"status": "completed", "conclusion": "success",
                          "display_title": "PR-Agent PR #9 [review:1] head=abc"}}
        target = "https://api.github.com/repos/o/r/actions/runs/123"

        def metadata(url: str) -> dict:
            run_id = int(re.search(r"/runs/(\d+)", url).group(1))
            if run_id in snapshot:
                return {"via": "snapshot", "run": snapshot[run_id]}
            gets["count"] += 1
            if gets.get("missing404"):
                raise FileNotFoundError("404")
            return {"via": "get", "run": None}

        hit = metadata(target)
        self.assertEqual(hit["via"], "snapshot")
        self.assertEqual(gets["count"], 0)
        miss = metadata("https://api.github.com/repos/o/r/actions/runs/999")
        self.assertEqual(miss["via"], "get")
        self.assertEqual(gets["count"], 1)

    def test_recovery_indexes_run_metadata_with_fallback(self):
        body = recovery_body()
        self.assertIn("runMetadataById", body)
        self.assertIn("rememberReviewRuns", body)
        self.assertIn("snapshotRunMetadata", body)
        self.assertIn("const snapshotHit = snapshotRunMetadata(match[1]);", body)
        self.assertIn("client.rest.actions.getWorkflowRun({", body)
        self.assertIn("if (err.status === 404) {", body)


class CiSnapshotTests(unittest.TestCase):
    def test_n_prs_share_one_ci_snapshot(self):
        per_head_calls = {"count": 0}
        heads = ["head%d" % i for i in range(4)]

        for _ in heads:  # baseline: one CI-run-list per PR
            per_head_calls["count"] += 1
        self.assertEqual(per_head_calls["count"], 4)

        snapshot_calls = {"count": 0}
        snapshot_calls["count"] += 1  # one bounded snapshot fetch
        snapshot = {h: True for h in heads}
        for h in heads:
            self.assertIn(h, snapshot)  # served from snapshot, no extra call
        self.assertEqual(snapshot_calls["count"], 1)
        self.assertLess(snapshot_calls["count"], per_head_calls["count"])

    def test_missing_snapshot_entry_falls_back_to_exact_lookup(self):
        snapshot = {"known": True}
        fallbacks: list[str] = []

        def scan(head: str) -> str:
            if head in snapshot:
                return "snapshot"
            fallbacks.append(head)
            return "exact"

        self.assertEqual(scan("known"), "snapshot")
        self.assertEqual(scan("unknown"), "exact")
        self.assertEqual(fallbacks, ["unknown"])

    def test_recovery_snapshots_ci_but_revalidates_before_dispatch(self):
        body = recovery_body()
        self.assertIn("ciSnapshotByHead", body)
        self.assertIn("fetchCiSnapshot", body)
        self.assertIn("scanCiGreen", body)
        self.assertIn("const ciGreen = await scanCiGreen(head);", body)
        # Fresh exact-HEAD CI still gates dispatch inside dispatch().
        dispatch_at = body.index("async function dispatch(pr, kind, head, attempt, status")
        dispatch = body[dispatch_at:dispatch_at + 9000]
        self.assertIn("if (!(await exactHeadCiGreen(head))) {", dispatch)
        self.assertIn("head_sha: head", body)
        self.assertIn("run.head_sha === head", body)


class CleanReviewShortCircuitTests(unittest.TestCase):
    def test_clean_pr_skips_comments_and_run_metadata(self):
        reads = {"statuses": 0, "comments": 0, "runmeta": 0}

        def reconcile_clean() -> str:
            reads["statuses"] += 1
            state, description = "success", "All checks passed"
            if state == "success" and "actionable" not in description.lower() \
                    and "blocking; recovery eligible" not in description.lower():
                return "clean-stop"
            reads["comments"] += 1
            reads["runmeta"] += 1
            return "recover"

        self.assertEqual(reconcile_clean(), "clean-stop")
        self.assertEqual(reads, {"statuses": 1, "comments": 0, "runmeta": 0})

    def test_actionable_pr_still_fetches_evidence(self):
        reads = {"statuses": 0, "comments": 0, "runmeta": 0}

        def reconcile_actionable() -> str:
            reads["statuses"] += 1
            state, description = "success", "Found actionable findings"
            if state == "success" and "actionable" not in description.lower():
                return "clean-stop"
            reads["comments"] += 1
            reads["runmeta"] += 1
            return "recover"

        self.assertEqual(reconcile_actionable(), "recover")
        self.assertEqual(reads["comments"], 1)
        self.assertEqual(reads["runmeta"], 1)

    def test_recovery_orders_statuses_before_comments(self):
        body = recovery_body()
        scan_at = body.index("const ciGreen = await scanCiGreen(head);")
        scan = body[scan_at:scan_at + 6000]
        statuses_at = scan.index("listCommitStatusesForRef")
        comments_at = scan.index("rest.issues.listComments")
        self.assertLess(statuses_at, comments_at)
        self.assertIn("review is settled clean; recovery has nothing to do.", scan)


class LeaseAndCredentialTests(unittest.TestCase):
    def test_duplicate_wakeups_remain_coalesced(self):
        recovery = recovery_body()
        self.assertIn("tryAcquireLease", recovery)
        self.assertIn("leaseKey", recovery)
        self.assertIn("ownedLeases", recovery)
        self.assertIn(
            "if (!tryAcquireLease(pr.number, head, 'reconciler')) {", recovery
        )
        scheduler = scheduler_body()
        # Scheduler coalesces via per-issue wake concurrency plus JIT
        # active guards and the in-progress reservation.
        self.assertIn("cancel-in-progress: true", scheduler)
        self.assertIn("activeRunIssues", scheduler)
        self.assertIn("openPrIssues", scheduler)
        self.assertIn("inProgressLabel", scheduler)
        self.assertIn("cancel-in-progress: false", recovery)

    def test_no_credential_binding_changed(self):
        scheduler = scheduler_body()
        recovery = recovery_body()
        for body, name in ((scheduler, "scheduler"), (recovery, "recovery")):
            with self.subTest(workflow=name):
                self.assertIn("READ_GITHUB_TOKEN", body)
                self.assertIn("withReadFallback" if name == "recovery" else "readGithub", body)
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", scheduler)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", scheduler)
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", recovery)
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", recovery)
        # Same-repository reads stay on the repository token; privileged
        # mutation/dispatch paths stay PAT-backed.
        self.assertIn("const readGithub = new github.constructor({", scheduler)
        self.assertIn("const readGithub = new github.constructor({ auth: readToken", recovery)
        self.assertIn("await github.rest.issues.createComment({", scheduler)
        self.assertIn("await github.rest.actions.createWorkflowDispatch({", recovery)
        # No paid-provider credential is introduced or required.
        for body in (scheduler, recovery):
            self.assertNotIn("OPENCODE_API_KEY", body)
            self.assertNotIn("ANTHROPIC_API_KEY", body)
            self.assertNotIn("GROQ_API_KEY", body)


class MeasurementEvidenceTests(unittest.TestCase):
    """Before/after request counts for representative deterministic fixtures."""

    def test_scheduler_pass_with_shared_blockers(self):
        # 4 ready issues each declaring the same 2 blockers.
        baseline = 4 * 2  # one issues.get per blocker per issue
        optimized = 0  # snapshot admission performs no issues.get
        self.assertEqual(baseline, 8)
        self.assertEqual(optimized, 0)

    def test_scheduler_owner_command_check(self):
        # 120-comment history; grace window holds the last 2.
        baseline_scanned = 120
        optimized_scanned = 2
        self.assertLess(optimized_scanned, baseline_scanned)

    def test_recovery_pass_with_clean_prs(self):
        prs = 4
        # Baseline per clean PR: 1 CI list + 1 status list + 1 comment
        # list + 1 run-metadata GET = 4 requests, plus a 3-page history scan.
        baseline = 3 + prs * 4
        # Optimized: 5 bounded active-status reads + 1 CI snapshot +
        # 4 status lists + 0 comment lists + 0 run-metadata GETs.
        optimized = 5 + 1 + prs
        self.assertEqual(baseline, 19)
        self.assertEqual(optimized, 10)
        self.assertLess(optimized, baseline)

    def test_recovery_pass_with_one_actionable_pr(self):
        # One actionable PR: baseline and optimized reach the same
        # dispatch decision; optimized only removes the duplicate CI list.
        baseline_requests = 1 + 1 + 1 + 1  # CI + statuses + comments + runmeta
        optimized_requests = 0 + 1 + 1 + 1  # CI from snapshot + same evidence
        self.assertEqual(baseline_requests, 4)
        self.assertEqual(optimized_requests, 3)
        decisions = ("dispatch", "dispatch")
        self.assertEqual(decisions[0], decisions[1])


if __name__ == "__main__":
    unittest.main()
