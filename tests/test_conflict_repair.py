"""Deterministic regression tests for dirty-PR self-healing (issue #246)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from continuum import conflict_repair as repair  # noqa: E402


HEAD = "c" * 40
OLD_HEAD = "d" * 40


def marker_comment(head: str, attempt: int, age_seconds: int = 0,
                   association: str = "OWNER") -> dict:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "id": 1000 + attempt,
        "body": repair.repair_marker(head, attempt),
        "author_association": association,
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
    }


class DirtyDetectionTests(unittest.TestCase):
    def test_mergeable_false_is_conflicted(self):
        self.assertTrue(repair.is_conflicted(False, ""))
        self.assertTrue(repair.is_conflicted(False, "unknown"))

    def test_dirty_state_is_conflicted(self):
        self.assertTrue(repair.is_conflicted(True, "dirty"))
        self.assertTrue(repair.is_conflicted(None, "DIRTY"))

    def test_graphql_conflicting_is_conflicted(self):
        self.assertTrue(repair.is_conflicted("CONFLICTING", ""))

    def test_clean_pr_is_not_conflicted(self):
        self.assertFalse(repair.is_conflicted(True, "clean"))
        self.assertFalse(repair.is_conflicted("BLOCKED", "blocked"))

    def test_computing_mergeability_waits(self):
        self.assertIsNone(repair.is_conflicted(None, ""))
        self.assertIsNone(repair.is_conflicted(None, "unknown"))
        decision = repair.decide_conflict_repair(
            mergeable=None, mergeable_state="unknown")
        self.assertEqual(decision.action, "wait")


class CallerRoutingTests(unittest.TestCase):
    def test_dirty_pr_dispatches_configured_consumer_caller(self):
        target = repair.normalize_caller("opencode.yml")
        self.assertEqual(target, "opencode.yml")

    def test_default_caller_is_installed_consumer_stub(self):
        self.assertEqual(repair.normalize_caller(""), "continuum-opencode.yml")
        self.assertEqual(repair.normalize_caller(None), "continuum-opencode.yml")

    def test_reusable_engine_is_never_dispatched_directly(self):
        body_path = os.path.join(
            ROOT, ".github/workflows/continuum-auto-merge.yml")
        with open(body_path, "r", encoding="utf-8") as handle:
            body = handle.read()
        # The repair dispatch must resolve its workflow from the configured
        # caller knob; a hardcoded engine file name would bypass dogfood's
        # opencode.yml caller (which owns workflow_dispatch).
        self.assertIn("process.env.OPENCODE_WORKFLOW", body)
        script_lines = [
            line for line in body.splitlines()
            if line.strip() and not line.strip().startswith("//")
        ]
        script = "\n".join(script_lines)
        self.assertNotIn("workflow_id: 'continuum-opencode.yml'", script)
        self.assertNotIn('workflow_id: "continuum-opencode.yml"', script)

    def test_caller_must_be_a_bare_workflow_file(self):
        # Empty default is rejected rather than dispatched blindly.
        with self.assertRaises(repair.ConflictRepairError):
            repair.normalize_caller("", default="")
        with self.assertRaises(repair.ConflictRepairError):
            repair.normalize_caller("not-a-workflow")
        with self.assertRaises(repair.ConflictRepairError):
            repair.normalize_caller("../evil.yml")


class PredatingDirtyDiscoveryTests(unittest.TestCase):
    def test_dirty_pr_predating_current_wake_is_still_discovered(self):
        # Discovery does not depend on PR creation time or on a fresh PR
        # event: the decision sees only latest mergeability state.
        decision = repair.decide_conflict_repair(
            mergeable=False, mergeable_state="dirty")
        self.assertEqual(decision.action, "dispatch")

    def test_scan_contract_lists_every_open_pr(self):
        body_path = os.path.join(
            ROOT, ".github/workflows/continuum-auto-merge.yml")
        with open(body_path, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("state: 'open'", body)
        self.assertIn("base: 'main'", body)
        self.assertIn("per_page: 100", body)


class StaleLockTests(unittest.TestCase):
    def test_stale_lock_with_no_active_run_redispatches_safely(self):
        decision = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=True,
            active_repair_run=False,
            dispatches_for_head=1,
            marker_age_seconds=repair.DISPATCH_GRACE_SECONDS + 1,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertTrue(decision.reconcile_lock)

    def test_stale_lock_markers_are_scoped_to_trusted_authors(self):
        comments = [
            marker_comment(HEAD, 0, association="NONE"),
            marker_comment(HEAD, 5, association="CONTRIBUTOR"),
        ]
        markers = repair.repair_markers_for_head(comments, head_sha=HEAD)
        self.assertEqual(markers, [])

    def test_stale_lock_markers_are_scoped_to_exact_head(self):
        comments = [marker_comment(OLD_HEAD, 0)]
        markers = repair.repair_markers_for_head(comments, head_sha=HEAD)
        self.assertEqual(markers, [])


class ActiveRunCoalescingTests(unittest.TestCase):
    def test_live_active_repair_run_suppresses_duplicate_dispatch(self):
        decision = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=True,
            active_repair_run=True,
        )
        self.assertEqual(decision.action, "wait")
        self.assertIsNone(decision.attempt)

    def test_active_statuses_cover_queued_and_in_progress(self):
        for status in ("queued", "in_progress", "waiting", "pending",
                       "requested"):
            self.assertTrue(repair.is_active_run_status(status))
        for status in ("completed", "cancelled", ""):
            self.assertFalse(repair.is_active_run_status(status))


class RepairedHeadLifecycleTests(unittest.TestCase):
    def test_successful_repaired_head_reconciles_lock_and_resumes(self):
        decision = repair.decide_conflict_repair(
            mergeable=True,
            mergeable_state="clean",
            has_lock=True,
            head_changed_since_dispatch=True,
        )
        self.assertEqual(decision.action, "resume")
        self.assertTrue(decision.reconcile_lock)

    def test_clean_pr_with_leftover_lock_still_reconciles(self):
        decision = repair.decide_conflict_repair(
            mergeable=True, mergeable_state="clean", has_lock=True)
        self.assertEqual(decision.action, "resume")
        self.assertTrue(decision.reconcile_lock)

    def test_clean_pr_without_lock_has_nothing_to_do(self):
        decision = repair.decide_conflict_repair(
            mergeable=True, mergeable_state="clean", has_lock=False)
        self.assertEqual(decision.action, "none")


class BoundedIdempotentRetryTests(unittest.TestCase):
    def test_repeated_wakeups_inside_grace_stay_idempotent(self):
        first = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=False,
            dispatches_for_head=0,
        )
        self.assertEqual(first.action, "dispatch")
        # The next wakeup sees the fresh marker (and/or the queued run) and
        # must not dispatch a duplicate.
        second = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=True,
            active_repair_run=False,
            dispatches_for_head=1,
            marker_age_seconds=10,
        )
        self.assertEqual(second.action, "wait")

    def test_retry_is_bounded_per_head_without_pause_label(self):
        decision = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=True,
            active_repair_run=False,
            dispatches_for_head=repair.MAX_DISPATCHES_PER_HEAD,
            marker_age_seconds=repair.DISPATCH_GRACE_SECONDS + 1,
        )
        self.assertEqual(decision.action, "hold")
        self.assertFalse(decision.reconcile_lock)
        # The hold never escalates to a terminal manual pause.
        self.assertEqual(repair.PAUSE_LABEL, "automation:paused")
        body_path = os.path.join(
            ROOT, ".github/workflows/continuum-auto-merge.yml")
        with open(body_path, "r", encoding="utf-8") as handle:
            body = handle.read()
        repair_window = body[body.index("dispatchConflictRepair"):]
        self.assertNotIn("automation:paused", repair_window)

    def test_new_head_starts_a_fresh_budget(self):
        comments = [marker_comment(OLD_HEAD, 2)]
        markers = repair.repair_markers_for_head(comments, head_sha=HEAD)
        self.assertEqual(markers, [])
        decision = repair.decide_conflict_repair(
            mergeable=False,
            mergeable_state="dirty",
            has_lock=False,
            dispatches_for_head=len(markers),
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.attempt, 0)


class PrAgentConflictPublishGuardTests(unittest.TestCase):
    """Issue #497: execute the shipped conflict publication guard.

    The guard must reject unresolved index entries, surviving conflict
    markers, and a result that does not contain the integrated default-branch
    base. A real merge of both sides must pass.
    """

    WORKFLOW = ".github/workflows/continuum-pr-agent-repair.yml"

    @classmethod
    def setUpClass(cls):
        with open(
            os.path.join(ROOT, cls.WORKFLOW), "r", encoding="utf-8"
        ) as handle:
            body = handle.read()
        start = body.index('no_progress_classification="transient"')
        end = body.index(
            'if [[ -z "$(git status --porcelain)" ]]; then', start
        )
        cls.precommit_guard = body[start:end]
        head_anchor = body.index(
            'if ! git merge-base --is-ancestor "$HEAD_SHA" HEAD; then'
        )
        post_start = body.index(
            'if [[ "${MODE,,}" == "conflict" ]]; then', head_anchor
        )
        post_end = body.index(
            'NEW_HEAD="$(git rev-parse HEAD)"', post_start
        )
        cls.postcommit_guard = body[post_start:post_end]

    def setUp(self):
        tmp_root = os.path.join(ROOT, ".opencode-tmp")
        os.makedirs(tmp_root, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="conflict-publish-", dir=tmp_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Continuum Test",
            GIT_AUTHOR_EMAIL="continuum-test@example.invalid",
            GIT_COMMITTER_NAME="Continuum Test",
            GIT_COMMITTER_EMAIL="continuum-test@example.invalid",
            RUNNER_TEMP=self.tmp,
        )
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Continuum Test")
        self._git("config", "user.email", "continuum-test@example.invalid")
        self._write("shared.txt", "base\n")
        self._git("add", "shared.txt")
        self._git("commit", "-q", "-m", "base")
        self._git("checkout", "-q", "-b", "feature")
        self._write("shared.txt", "feature\n")
        self._git("add", "shared.txt")
        self._git("commit", "-q", "-m", "feature")
        self.head_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("checkout", "-q", "main")
        self._write("shared.txt", "base\nmain\n")
        self._git("add", "shared.txt")
        self._git("commit", "-q", "-m", "advance main")
        self.base_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("checkout", "-q", "feature")

    def _git(self, *args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def _write(self, name, content):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as handle:
            handle.write(content)

    def _merge_base(self):
        return self._git("merge", "main", check=False)

    def _run_script(self, block):
        output_path = os.path.join(self.tmp, "github-output")
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("")
        script = (
            "set -euo pipefail\n"
            "MODE=conflict\n"
            f"GITHUB_OUTPUT={output_path!r}\n"
            f"BASE_SHA={self.base_sha}\n"
            + block
        )
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
        )
        with open(output_path, "r", encoding="utf-8") as handle:
            outputs = handle.read()
        return completed, outputs

    def test_merged_result_contains_both_sides_and_publishes(self):
        merge = self._merge_base()
        self.assertNotEqual(merge.returncode, 0, "fixture must conflict")
        self._write("shared.txt", "feature\nmain\n")
        self._git("add", "shared.txt")
        completed, outputs = self._run_script(self.precommit_guard)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self._git("commit", "-q", "-m", "merge main into feature")
        published, published_outputs = self._run_script(self.postcommit_guard)
        self.assertEqual(
            published.returncode, 0, published.stderr or published.stdout
        )
        for ancestor in (self.head_sha, self.base_sha):
            with self.subTest(ancestor=ancestor):
                self.assertEqual(
                    self._git("merge-base", "--is-ancestor", ancestor, "HEAD").returncode,
                    0,
                    "repaired HEAD must contain both the original HEAD and the integrated base",
                )
        with open(os.path.join(self.repo, "shared.txt"), encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("feature", content)
        self.assertIn("main", content)
        self.assertEqual(outputs.strip(), "")
        self.assertEqual(published_outputs.strip(), "")

    def test_unresolved_index_entries_are_rejected(self):
        merge = self._merge_base()
        self.assertNotEqual(merge.returncode, 0, "fixture must conflict")
        completed, outputs = self._run_script(self.precommit_guard)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unresolved index entries", completed.stderr + completed.stdout)
        self.assertIn("classification=deterministic", outputs)

    def test_surviving_conflict_markers_are_rejected(self):
        merge = self._merge_base()
        self.assertNotEqual(merge.returncode, 0, "fixture must conflict")
        self._write(
            "shared.txt",
            "<<<<<<< HEAD\nfeature\n=======\nmain\n>>>>>>> main\n",
        )
        self._git("add", "shared.txt")
        completed, outputs = self._run_script(self.precommit_guard)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("conflict markers", completed.stderr + completed.stdout)
        self.assertIn("classification=deterministic", outputs)

    def test_large_staged_diff_with_early_marker_still_fails_closed(self):
        # A `git diff | grep -q` pipeline can fail open when grep exits early
        # and git receives SIGPIPE. The guard must capture the diff first so
        # a large staged result cannot publish surviving conflict markers.
        merge = self._merge_base()
        self.assertNotEqual(merge.returncode, 0, "fixture must conflict")
        self._write(
            "shared.txt",
            "<<<<<<< HEAD\nfeature\n=======\nmain\n>>>>>>> main\n",
        )
        self._git("add", "shared.txt")
        large = os.path.join(self.repo, "large.txt")
        with open(large, "w", encoding="utf-8") as handle:
            handle.write("<<<<<<< HEAD\nfeature\n=======\nmain\n>>>>>>> main\n")
            for index in range(200000):
                handle.write("padding line %d\n" % index)
        self._git("add", "large.txt")
        completed, outputs = self._run_script(self.precommit_guard)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("conflict markers", completed.stderr + completed.stdout)
        self.assertIn("classification=deterministic", outputs)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "conflict-staged.diff")),
            "the guard must inspect a captured diff instead of a pipe",
        )

    def test_result_without_integrated_base_is_rejected(self):
        completed, outputs = self._run_script(self.postcommit_guard)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "did not integrate the default-branch base",
            completed.stderr + completed.stdout,
        )
        self.assertIn("classification=deterministic", outputs)


if __name__ == "__main__":
    unittest.main()
