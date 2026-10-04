import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class PrAgentTargetWorkflowContractTests(unittest.TestCase):
    reusable = (
        ".github/workflows/continuum-pr-agent.yml",
        ".github/workflows/continuum-pr-agent-repair.yml",
        ".github/workflows/continuum-pr-agent-auto-merge.yml",
        ".github/workflows/continuum-pr-agent-recovery.yml",
    )

    callers = (
        ".github/caller-stubs/continuum-pr-agent.yml",
        ".github/caller-stubs/continuum-pr-agent-repair.yml",
        ".github/caller-stubs/continuum-pr-agent-auto-merge.yml",
        ".github/caller-stubs/continuum-pr-agent-recovery.yml",
        ".github/workflows/pr-agent.yml",
        ".github/workflows/pr-agent-recovery.yml",
    )

    def test_reusable_workflows_accept_only_opaque_target_identity(self):
        for path in self.reusable:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn("target_child_id:", body)
                # Only the opaque id is ever an input; concrete repository
                # identity is resolved runner-local and never appears as an
                # input, run name, or committed config value.
                self.assertNotIn("target_repository:", body)
                self.assertNotIn("target_owner:", body)
        helper = read(".github/scripts/pr_agent_target.sh")
        self.assertIn('bash "$resolver" resolve "$child_id"', helper)
        self.assertIn('bash "$resolver" verify "$child_id" "$target_repository"', helper)
        self.assertIn('target_repository="$GITHUB_REPOSITORY"', helper)

    def test_review_targets_child_data_but_retries_in_execution_repository(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", body)
        self.assertIn('gh pr view "$PR_NUMBER" --repo "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
        self.assertIn("repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/actions/runs", body)
        self.assertIn("owner: process.env.CONTINUUM_PR_AGENT_TARGET_OWNER", body)
        # Retry dispatch stays in the parent execution repository while
        # carrying the opaque child identity forward.
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertIn('-f target_child_id="$TARGET_CHILD_ID"', body)
        # Concurrency is scoped by the opaque id only, never a concrete name.
        self.assertIn("inputs.target_child_id && format('child-{0}'", body)
        self.assertNotIn("target_repository", body.lower().replace("continuum_pr_agent_target_repository", ""))

    def test_repair_targets_child_branch_and_stays_pat_backed_for_push(self):
        body = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_REPOSITORY", body)
        self.assertIn('gh api "repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/pulls/$PR_NUMBER"', body)
        self.assertIn('"$HEAD_REPO" != "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
        # Checkout targets the verified repository via an opaque env
        # reference (no concrete name in committed config); push still uses
        # force-with-lease and the retry dispatch stays in the parent.
        self.assertIn("repository: ${{ env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY }}", body)
        self.assertIn("token: ${{ secrets.TAP_PAT }}", body)
        self.assertIn("git push --force-with-lease=", body)
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertIn('-f target_child_id="$TARGET_CHILD_ID"', body)

    def test_merge_keeps_target_and_execution_repository_distinct(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("const executionOwner = context.repo.owner;", body)
        self.assertIn("const owner = process.env.CONTINUUM_PR_AGENT_TARGET_OWNER;", body)
        self.assertIn("const repoFullName = process.env.CONTINUUM_PR_AGENT_TARGET_REPOSITORY;", body)
        self.assertIn("owner: executionOwner", body)
        self.assertIn("repo: executionRepo", body)
        self.assertIn("Delegated PR conflict repair needs the parent routing controller", body)

    def test_recovery_reads_target_but_dispatches_parent_workflow(self):
        body = read(".github/workflows/continuum-pr-agent-recovery.yml")
        self.assertIn("const executionOwner = context.repo.owner;", body)
        self.assertIn("const owner = process.env.CONTINUUM_PR_AGENT_TARGET_OWNER;", body)
        self.assertIn("owner: executionOwner", body)
        self.assertIn("repo: executionRepo", body)
        # Delegated recovery forwards the opaque child selection while local
        # runs dispatch bare: a custom reviewWorkflow without the
        # target_child_id workflow_dispatch input would reject an empty
        # value, breaking local recovery. This mirrors the auto-merge
        # delegated-wakeup contract (never retries bare for delegated,
        # dispatches bare for local).
        self.assertIn("const recoveryChildId = String(process.env.TARGET_CHILD_ID || '')", body)
        self.assertIn("if (recoveryChildId)", body)
        self.assertIn("recoveryInputs.target_child_id = recoveryChildId", body)
        self.assertIn("TARGET_CHILD_ID: ${{ inputs.target_child_id || vars.CONTINUUM_PR_AGENT_TARGET_CHILD_ID }}", body)

    def test_callers_preserve_opaque_child_id_across_retries(self):
        for path in self.callers:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn("target_child_id", body)
                self.assertNotIn("target_repository", body)

    def test_delegated_identity_is_masked_and_target_checkout_is_quiet(self):
        helper = read(".github/scripts/pr_agent_target.sh")
        # Resolver stderr is suppressed so delegated identity cannot leak
        # before the ::add-mask:: calls are installed.
        self.assertIn('resolve "$child_id" 2>/dev/null', helper)
        self.assertIn("echo \"::add-mask::$target_repository\"", helper)
        self.assertIn("echo \"::add-mask::$target_repo\"", helper)
        review = read(".github/workflows/continuum-pr-agent.yml")
        # Review checkout is a quiet manual fetch of the exact HEAD without
        # exposing delegated metadata; delegated tool output stays runner-local.
        self.assertIn('git remote add origin "https://github.com/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY.git"', review)
        self.assertIn(">/dev/null 2>&1", review)
        self.assertIn("git fetch --depth=1 --no-tags origin", review)
        self.assertIn("git checkout --detach FETCH_HEAD", review)
        self.assertIn("detailed output remains runner-local", review)

    def test_coderabbit_workflows_remain_outside_target_context(self):
        for path in (
            ".github/workflows/continuum-coderabbit-retry.yml",
            ".github/workflows/continuum-coderabbit-unresolved.yml",
        ):
            with self.subTest(path=path):
                body = read(path)
                self.assertNotIn("target_child_id", body)
                self.assertNotIn("CONTINUUM_PR_AGENT_TARGET", body)

    def test_delegated_reads_fail_closed_without_pat(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        # A dedicated guard fails the run before any delegated read when
        # TAP_PAT is empty, so the conditional token can never silently fall
        # back to github.token for cross-repository reads.
        self.assertIn("Fail closed when delegated without PAT", body)
        self.assertIn("Delegated PR-Agent execution requires TAP_PAT", body)
        self.assertIn("refusing to fall back to github.token", body)
        for step in (
            "Admit only a review-ready PR with green CI on the exact HEAD",
            "Revalidate the admitted exact HEAD immediately before review",
            "Revalidate the PR head and native review output after review",
            "Fail closed on a moved head",
        ):
            with self.subTest(step=step):
                start = body.index(step)
                window = body[start:start + 4000]
                self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", window)
                self.assertIn("TAP_PAT", window)
                self.assertIn("requires TAP_PAT", window)

    def test_checkout_credential_is_scoped_to_resolved_target(self):
        body = read(".github/workflows/continuum-pr-agent.yml")
        start = body.index("Checkout the pull request head without exposing")
        window = body[start:start + 4000]
        # Local checkout uses github.token; only delegated checkout uses
        # TAP_PAT, and delegated runs without PAT fail closed.
        self.assertIn("github.token", window)
        self.assertIn("secrets.TAP_PAT", window)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", window)
        self.assertIn("requires TAP_PAT", window)

    def test_delegated_wakeup_never_retries_bare(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        self.assertIn("dispatchParams.inputs = { target_child_id: targetChildId }", body)
        self.assertIn("skipping bare retry to preserve the delegated target", body)
        self.assertNotIn("retrying bare", body)

    def test_auto_merge_exact_head_and_privacy(self):
        body = read(".github/workflows/continuum-pr-agent-auto-merge.yml")
        # Exact-HEAD: only the reviewed SHA may merge; any moved HEAD fails.
        self.assertIn("pr.head.sha.toLowerCase() !== reviewedHead", body)
        self.assertIn("sha: reviewedHead", body)
        self.assertIn("refusing to merge a stale result", body)
        # Delegated-vs-local branching: opaque child id only, never a
        # concrete repository input.
        self.assertIn("target_child_id:", body)
        self.assertNotIn("target_repository:", body)
        self.assertIn("CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED", body)
        # Privacy: delegated conflict tooling never dispatches into the
        # child; the failure stays explicit instead of leaking a local run.
        self.assertIn("Delegated PR conflict repair needs the parent routing controller", body)


if __name__ == "__main__":
    unittest.main()
