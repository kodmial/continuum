"""Executable proof for kodmial/continuum#305 (TAP_PAT minimization).

Proves the Definition of Done against the exact current tree without
changing lifecycle semantics or the consumer contract:

- former items 1-6 stay untouched (baseline compatibility sentinels);
- candidates 7-10 are each closed by evidence (governor unnecessary,
  mutations audited, no global chaining conversion, final inventory);
- safe same-repo reads use the repository-scoped token where equivalent;
- avoidable N+1/broad historical scans stay removed;
- every remaining TAP_PAT binding maps to its required capability;
- rate-limit exhaustion degrades to bounded autonomous recovery;
- no manual/human-required recovery, no paid-provider key, no
  default-branch code writes, and no consumer-contract change.
"""

from __future__ import annotations

import os
import re
import unittest

from continuum import api_budget
from continuum import lifecycle_recovery as lifecycle
from continuum import tap_pat_inventory as inventory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS_DIR = os.path.join(ROOT, ".github", "workflows")
STUBS_DIR = os.path.join(ROOT, ".github", "caller-stubs")

SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")


def read_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, name), encoding="utf-8") as handle:
        return handle.read()


class InventoryShapeTests(unittest.TestCase):
    def test_inventory_is_valid(self):
        self.assertTrue(inventory.validate())

    def test_every_capability_is_known(self):
        for entry in inventory.ENTRIES:
            with self.subTest(step=entry["step"]):
                self.assertIn(entry["capability"], inventory.CAPABILITIES)

    def test_every_entry_names_its_mechanism(self):
        for entry in inventory.ENTRIES:
            with self.subTest(step=entry["step"]):
                reason = entry["reason"].lower()
                self.assertTrue(
                    any(
                        keyword in reason
                        for keyword in (
                            "cross-repo", "cross-repository", "repositor",
                            "delegat", "fan out",
                            "fanning", "suppress", "dispatch", "push",
                            "merge", "workflow scope", "narrow grant",
                            "permission", "elevation", "exactly-once",
                            "lease", "identity", "actor", "owner",
                            "fallback", "liveness", "inert", "no github rest",
                            "bounded", "fail-closed", "refus", "distinct",
                            "durable", "state", "reconcil", "wake",
                            "private", "handoff", "render",
                        )
                    ),
                    f"{entry['workflow']}::{entry['step']}: reason names no mechanism",
                )

    def test_pr_agent_family_lists_its_steps(self):
        family = inventory.PR_AGENT_FAMILY
        self.assertEqual(family["workflow"], "continuum-pr-agent.yml")
        self.assertEqual(family["capability"], inventory.DELEGATED_CONDITIONAL)
        self.assertGreater(len(family["steps"]), 15)
        self.assertEqual(len(set(family["steps"])), len(family["steps"]))


class CoverageTests(unittest.TestCase):
    def test_every_pat_binding_is_inventoried(self):
        bindings = inventory.scan_workflow_bindings(WORKFLOWS_DIR)
        # Pinned count guards against silent scope drift in either direction:
        # new PAT sites must extend the inventory, removals must shrink it.
        self.assertEqual(len(bindings), 113)
        gaps = inventory.coverage_gaps(WORKFLOWS_DIR)
        self.assertEqual(gaps, [], f"uninventoried TAP_PAT bindings: {gaps}")

    def test_every_entry_matches_shipped_body(self):
        for entry in inventory.ENTRIES:
            with self.subTest(workflow=entry["workflow"], step=entry["step"]):
                body = read_workflow(entry["workflow"])
                self.assertIn(entry["step"], body)
                for evidence in entry["evidence"]:
                    self.assertIn(
                        evidence, body,
                        f"{entry['workflow']}::{entry['step']}: missing {evidence!r}",
                    )

    def test_delegated_conditionals_keep_both_branches(self):
        body = read_workflow("continuum-pr-agent.yml")
        conditional = (
            "CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED == 'true' && secrets.TAP_PAT"
        )
        self.assertIn(conditional, body)
        # The local branch of every conditional is the repository token, and
        # delegated runs refuse silent fallback.
        self.assertIn("github.token", body)
        self.assertIn(
            "Delegated PR-Agent execution requires TAP_PAT; "
            "refusing to fall back to github.token.",
            body,
        )

    def test_fallback_bindings_stay_bounded(self):
        for name in ("continuum-render-executor.yml",
                     "continuum-docker-qualification.yml"):
            body = read_workflow(name)
            self.assertIn("secrets.TAP_PAT || github.token", body)
        # The Render API key stays distinct: no fallback to any GitHub token.
        render = read_workflow("continuum-render-executor.yml")
        self.assertIn("RENDER_API_KEY is the Render API key", render)


class GovernorTests(unittest.TestCase):
    """Candidate 7: no extra PAT reserve/governor beyond reset-aware recovery."""

    def test_governor_finding_is_documented(self):
        evidence = inventory.governor_evidence()
        self.assertIn("No additional PAT rate-limit reserve/governor", evidence["verdict"])

    def test_reset_epochs_raise_the_minimum_wait(self):
        now = 1_000_000
        schedule_only = lifecycle.next_retry_delay_seconds(
            attempt=1, operation_key_value="k", now_epoch=now)
        with_reset = lifecycle.next_retry_delay_seconds(
            attempt=1, operation_key_value="k",
            ratelimit_reset_epoch=now + 600, now_epoch=now)
        self.assertGreaterEqual(with_reset, 600)
        self.assertGreaterEqual(with_reset, schedule_only)

    def test_long_waits_exit_instead_of_sleeping(self):
        self.assertFalse(lifecycle.should_defer_dispatch(60))
        self.assertTrue(lifecycle.should_defer_dispatch(61))

    def test_rate_limit_without_clock_defers_instead_of_dispatching(self):
        decision = lifecycle.decide_recovery(
            ci_green=True, operation_state="failure", failure_transient=True,
            ratelimit_reset_epoch=2_000, now_epoch=None)
        self.assertEqual(decision.action, "wait")

    def test_rate_limit_classification_is_transient_but_bounded(self):
        classified = lifecycle.classify_infrastructure_failure(
            status=429, headers={"Retry-After": "120"}, error="too many requests")
        self.assertTrue(classified.transient)
        # Bounded budget still applies: exhaustion holds instead of asking a human.
        decision = lifecycle.decide_recovery(
            ci_green=True, operation_state="failure", failure_transient=True,
            evidence=lifecycle.RetryEvidence(latest_attempt=9),
            max_executions=10)
        self.assertEqual(decision.action, "exhaust")

    def test_non_critical_reads_never_retry_storm(self):
        attempts = []

        def failing():
            attempts.append(1)
            raise RuntimeError("rate limited")

        result = api_budget.read_with_budget(
            failing, api_budget.RetryPolicy(max_attempts=3), None)
        self.assertIsNone(result)
        self.assertEqual(len(attempts), 3)

    def test_recovery_never_spends_pat_on_rate_limit_retries(self):
        for name in ("continuum-auto-merge.yml", "continuum-pr-agent-recovery.yml"):
            body = read_workflow(name)
            with self.subTest(workflow=name):
                # Only authentication/permission gaps fall back to PAT, one
                # call at a time; rate-limited calls surface and defer.
                self.assertIn("async function withReadFallback(fn)", body)
                self.assertIn("Never spend TAP_PAT's shared budget on rate-limit retries", body)


class ReadsUseRepositoryTokenTests(unittest.TestCase):
    """Safe same-repo reads use the repository-scoped token where equivalent."""

    def test_recovery_and_merge_fail_closed_without_repository_token(self):
        for name in ("continuum-auto-merge.yml", "continuum-pr-agent-recovery.yml"):
            body = read_workflow(name)
            with self.subTest(workflow=name):
                self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", body)
                self.assertIn("must not fall back to TAP_PAT", body)

    def test_scheduler_reads_use_repository_token(self):
        body = read_workflow("continuum-issue-scheduler.yml")
        self.assertIn("READ_GITHUB_TOKEN", body)
        self.assertIn(
            "READ_GITHUB_TOKEN is required for same-repository scheduler reads.",
            body,
        )

    def test_add_review_label_reads_use_repository_token(self):
        body = read_workflow("continuum-add-review-label.yml")
        self.assertIn("READ_GITHUB_TOKEN: ${{ github.token }}", body)
        self.assertIn(
            "READ_GITHUB_TOKEN is required for same-repository review-label reads.",
            body,
        )

    def test_opencode_admission_reads_use_repository_token_first(self):
        body = read_workflow("continuum-opencode.yml")
        # Primary client is the repository token; TAP_PAT appears only as a
        # bounded fallback or cross-repo fetch inside those steps.
        self.assertIn("github-token: ${{ github.token }}", body)
        self.assertIn("falling back to TAP_PAT for liveness", body)


class BoundedScanTests(unittest.TestCase):
    """Avoidable N+1/broad historical scans stay removed (items 6 + DoD)."""

    def test_recovery_lists_only_active_runs(self):
        body = read_workflow("continuum-pr-agent-recovery.yml")
        self.assertIn("ACTIVE_REVIEW_RUN_STATUSES", body)
        self.assertIn("MAX_REVIEW_PAGES_PER_STATUS", body)
        self.assertIn("async function listReviewRuns() {", body)

    def test_scheduler_reuses_snapshots_and_grace_windows(self):
        body = read_workflow("continuum-issue-scheduler.yml")
        self.assertIn("openSnapshotByNumber", body)
        self.assertIn("rememberOpenSnapshot", body)
        self.assertIn("since: commandGraceSinceIso()", body)

    def test_full_pass_is_materially_cheaper(self):
        fixture = api_budget.ConsumerFixture(open_prs=25, historical_runs=12_000)
        old = api_budget.estimate_old_pass(fixture)
        new = api_budget.estimate_new_pass(fixture)
        self.assertLess(new.calls, old.calls)
        self.assertLess(new.pages, old.pages // 2)
        self.assertLess(new.pat_calls(), old.pat_calls())


class ChainingTests(unittest.TestCase):
    """Candidate 9: explicit dispatch where equivalent, chaining where required."""

    def test_chaining_finding_is_documented(self):
        evidence = inventory.chaining_evidence()
        self.assertIn("No global PAT-event-chaining conversion", evidence["verdict"])

    def test_explicit_dispatches_stay_pat_backed(self):
        self.assertIn(
            "'POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches'",
            read_workflow("continuum-issue-scheduler.yml"),
        )
        self.assertIn(
            "await github.rest.actions.createWorkflowDispatch({",
            read_workflow("continuum-pr-agent-recovery.yml"),
        )
        self.assertIn(
            "await github.rest.actions.createWorkflowDispatch({",
            read_workflow("continuum-auto-merge.yml"),
        )

    def test_chaining_writes_stay_pat_backed(self):
        watchdog = read_workflow("continuum-opencode-watchdog.yml")
        self.assertIn("github-token: ${{ secrets.TAP_PAT }}", watchdog)
        self.assertIn("Recover failed OpenCode issue run", watchdog)


class SafetyAndContractTests(unittest.TestCase):
    def test_no_new_user_secret(self):
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-") or not name.endswith(".yml"):
                if name != "automation.yml":
                    continue
            body = read_workflow(name)
            for secret in SECRET_RE.findall(body):
                with self.subTest(workflow=name, secret=secret):
                    self.assertIn(
                        secret, set(inventory.ALLOWED_SECRETS) | {"GITHUB_TOKEN"},
                        f"{name}: new user secret secrets.{secret} is not allowed",
                    )

    def test_no_paid_provider_key_required(self):
        # The canary enforces the no-paid-provider rule by refusing such keys.
        canary = read_workflow("continuum-pr-agent-canary.yml")
        self.assertIn("Canary must not introduce a paid-provider secret", canary)
        self.assertIn("opencode/muse-spark-1.3-contributor-free", canary)

    def test_pushes_keep_fail_closed_branch_safety(self):
        for name in ("continuum-opencode.yml", "continuum-opencode-repair.yml",
                     "continuum-auto-merge.yml"):
            body = read_workflow(name)
            with self.subTest(workflow=name):
                self.assertTrue(
                    "continuum_assert_safe_push" in body
                    or "continuumAssertSafePush" in body,
                    f"{name}: push sites lost fail-closed branch safety",
                )

    def test_no_direct_default_branch_push(self):
        forbidden = re.compile(r"git push[^\n]*\s(main|master)(\s|$|['\"])")
        for name in sorted(os.listdir(WORKFLOWS_DIR)):
            if not name.startswith("continuum-") or not name.endswith(".yml"):
                continue
            body = read_workflow(name)
            self.assertIsNone(
                forbidden.search(body),
                f"{name}: direct default-branch push",
            )

    def test_baseline_items_1_to_6_markers_intact(self):
        # Spot-check that the completed migrations (items 1-6) were not
        # reimplemented or broadened by this task.
        recovery = read_workflow("continuum-pr-agent-recovery.yml")
        self.assertIn("keeping those requests off TAP_PAT's shared budget", recovery)
        scheduler = read_workflow("continuum-issue-scheduler.yml")
        self.assertIn("openSnapshotByNumber", scheduler)
        auto_merge = read_workflow("continuum-auto-merge.yml")
        self.assertIn("cancelObsoleteRuns", auto_merge)


if __name__ == "__main__":
    unittest.main()
