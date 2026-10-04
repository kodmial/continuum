"""Child PR-Agent routing through the parent (issue #244).

Parent is the only automatic execution plane for child PR-Agent: child-local
automatic execution is suppressed, the parent scheduler fans out one recovery
dispatch per verified child carrying only the opaque child id, and legacy
independent child review never runs beside PR-Agent.
"""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


CHILD_GATE = "vars.CONTINUUM_ROLE != 'child'"


class ChildSuppressionTests(unittest.TestCase):
    reusables = (
        ".github/workflows/continuum-pr-agent.yml",
        ".github/workflows/continuum-pr-agent-repair.yml",
        ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ".github/workflows/continuum-pr-agent-recovery.yml",
        ".github/workflows/continuum-pr-agent-router.yml",
    )

    def test_reusable_pr_agent_jobs_never_execute_in_verified_child_mode(self):
        for path in self.reusables:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn(
                    CHILD_GATE,
                    body,
                    f"{path}: PR-Agent execution must be suppressed in verified child mode",
                )

    def test_recovery_job_keeps_provider_gate_beside_child_suppression(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn(
            "(inputs.review_provider || vars.CONTINUUM_REVIEW_PROVIDER || 'none') == 'pr-agent'",
            body,
        )
        self.assertIn(CHILD_GATE, body)

    def test_automatic_recovery_callers_skip_child_events_but_keep_manual_dispatch(self):
        for path in (
            ".github/caller-stubs/continuum-pr-agent-recovery.yml",
            ".github/workflows/pr-agent-recovery.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertIn(CHILD_GATE, body)
                self.assertIn("github.event_name == 'workflow_dispatch'", body)

    def test_review_and_router_callers_still_forward_delegated_inputs(self):
        stub = read(".github/caller-stubs/continuum-pr-agent.yml")
        self.assertIn("target_child_id", stub)
        router_stub = read(".github/caller-stubs/continuum-pr-agent-router.yml")
        self.assertIn("target_child_id", router_stub)


class ParentFanoutTests(unittest.TestCase):
    def test_scheduler_fans_out_per_child_recovery_with_opaque_id_only(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn("REVIEW_PROVIDER", body)
        self.assertIn("pr_agent_authoritative", body)
        self.assertIn(
            'gh workflow run "continuum-pr-agent-recovery.yml" --repo "$GITHUB_REPOSITORY"',
            body,
        )
        self.assertIn('-f target_child_id="$child_id"', body)

    def test_scheduler_fanout_carries_no_concrete_repository_identity(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        start = body.index("Parent-side PR-Agent fan-out")
        window = body[start : start + 3000]
        self.assertNotIn("child_repo", window.replace("$child_repo", ""))
        self.assertNotIn("target_repository", window)

    def test_legacy_child_review_cannot_race_pr_agent(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn('if [[ "$pr_agent_authoritative" == true ]]; then', body)
        # Both legacy dispatch sites are fenced by the authoritative flag.
        self.assertIn(
            "Legacy task review is owned by the per-child PR-Agent",
            body,
        )
        self.assertIn(
            "Legacy independent child review runs only where PR-Agent is not",
            body,
        )
        self.assertIn(
            'if [[ "$pr_agent_authoritative" != true ]]; then',
            body,
        )

    def test_scheduler_provider_comparison_is_case_insensitive(self):
        body = read(".github/workflows/continuum-issue-scheduler.yml")
        self.assertIn("review_provider_normalized", body)
        self.assertIn("tr '[:upper:]' '[:lower:]'", body)


class RouterDelegationTests(unittest.TestCase):
    def test_router_accepts_and_forwards_opaque_child_id(self):
        reusable = read(".github/workflows/continuum-pr-agent-router.yml")
        self.assertIn("target_child_id:", reusable)
        self.assertIn("expected_head_sha:", reusable)
        self.assertIn("INPUT_TARGET_CHILD_ID", reusable)
        self.assertIn("INPUT_EXPECTED_HEAD", reusable)
        self.assertIn("targetChildId", reusable)
        self.assertIn(
            "dispatchArgs.inputs.target_child_id = targetChildId", reusable
        )
        stub = read(".github/caller-stubs/continuum-pr-agent-router.yml")
        self.assertIn("target_child_id", stub)
        self.assertIn('target_child_id: "${{ inputs.target_child_id }}"', stub)
        self.assertIn('expected_head_sha: "${{ inputs.expected_head_sha }}"', stub)
        dogfood = read(".github/workflows/pr-agent-router.yml")
        self.assertIn("target_child_id", dogfood)
        self.assertIn('target_child_id: "${{ inputs.target_child_id }}"', dogfood)
        self.assertIn('expected_head_sha: "${{ inputs.expected_head_sha }}"', dogfood)

    def test_router_delegated_route_requires_exact_head(self):
        body = read(".github/workflows/continuum-pr-agent-router.yml")
        self.assertIn(
            "requires an exact expected_head_sha for delegated routing", body
        )
        self.assertIn("([0-9a-f]{40}|[0-9a-f]{64})", body)

    def test_router_operation_key_is_child_scoped_and_validates_input(self):
        body = read(".github/workflows/continuum-pr-agent-router.yml")
        self.assertIn("'review:' + targetChildId + ':' + prNumber + ':' + head", body)
        self.assertIn("^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$", body)
        self.assertIn("received an invalid target_child_id", body)

    def test_router_dispatches_bare_for_local_only(self):
        body = read(".github/workflows/continuum-pr-agent-router.yml")
        self.assertIn("if (targetChildId) {", body)

    def test_router_carries_no_concrete_repository_identity(self):
        body = read(".github/workflows/continuum-pr-agent-router.yml")
        self.assertNotIn("target_repository:", body)
        self.assertNotIn("target_owner:", body)


class DeterministicRoutingTests(unittest.TestCase):
    def test_pr_agent_path_covers_owner_and_automation_prs_without_split(self):
        scheduler = read(".github/workflows/continuum-issue-scheduler.yml")
        start = scheduler.index("Parent-side PR-Agent fan-out")
        window = scheduler[start : start + 2500]
        # One dispatch per verified child covers both PR kinds; unlike the
        # legacy path there is no owner-login or task-branch split here.
        self.assertNotIn("user.login", window)
        self.assertNotIn("continuum-child/task-", window)
        recovery = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("state: 'open', per_page: 100", recovery)

    def test_pr_agent_admission_has_no_owner_or_branch_filter(self):
        review = read(".github/workflows/continuum-pr-agent.yml")
        start = review.index("Admit only a review-ready PR with green CI on the exact HEAD")
        window = review[start : start + 6000]
        self.assertNotIn("user.login", window)
        self.assertNotIn("continuum-child/task-", window)
        self.assertNotIn("opencode/issue", window)


class PrivacyAndExactHeadTests(unittest.TestCase):
    def test_run_names_carry_no_child_identity(self):
        for path in (
            ".github/caller-stubs/continuum-pr-agent.yml",
            ".github/workflows/pr-agent.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertNotIn("target_child_id", body.split("run-name:")[1].split("on:")[0])

    def test_scheduler_fanout_preserves_exact_head_chain(self):
        scheduler = read(".github/workflows/continuum-issue-scheduler.yml")
        # The scheduler fans out to recovery without inventing a HEAD; exact
        # HEADs are observed by the recovery reconciler and enforced by
        # admission, repair publication, and the merge gate.
        start = scheduler.index("Parent-side PR-Agent fan-out")
        window = scheduler[start : start + 2500]
        self.assertNotIn("expected_head_sha", window)
        self.assertNotIn("head_sha", window)
        recovery = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("expected_head_sha: head", recovery)
        repair = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn('"$CURRENT_SHA" != "$HEAD_SHA"', repair)
        merge = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("pr.head.sha.toLowerCase() !== reviewedHead", merge)

    def test_coderabbit_remains_untouched(self):
        for path in (
            ".github/workflows/continuum-coderabbit-retry.yml",
            ".github/workflows/continuum-coderabbit-unresolved.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertNotIn("target_child_id", body)
                self.assertNotIn("CONTINUUM_PR_AGENT_TARGET", body)
                self.assertNotIn("CONTINUUM_ROLE", body)


if __name__ == "__main__":
    unittest.main()
