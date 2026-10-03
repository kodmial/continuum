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
        self.assertIn('repos/$CONTINUUM_PR_AGENT_TARGET_REPOSITORY/actions/runs', body)
        self.assertIn("owner: process.env.CONTINUUM_PR_AGENT_TARGET_OWNER", body)
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertIn('-f target_child_id="$TARGET_CHILD_ID"', body)

    def test_repair_targets_child_branch_without_actions_checkout_metadata(self):
        body = read(".github/workflows/continuum-pr-agent-repair.yml")
        self.assertIn('gh repo clone "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
        self.assertIn("-- --quiet >/dev/null 2>&1", body)
        self.assertIn('git push --force-with-lease=', body)
        self.assertIn('gh workflow run "$RETRY_WORKFLOW" --repo "$GITHUB_REPOSITORY"', body)
        self.assertNotIn("repository: " + "${" + "{", body)

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
        self.assertIn("target_child_id: String(process.env.TARGET_CHILD_ID || '')", body)

    def test_callers_preserve_opaque_child_id_across_retries(self):
        for path in self.callers:
            with self.subTest(path=path):
                body = read(path)
                self.assertIn("target_child_id", body)
                self.assertNotIn("target_repository", body)

    def test_delegated_identity_is_masked_and_target_checkout_is_quiet(self):
        helper = read(".github/scripts/pr_agent_target.sh")
        self.assertIn('echo "::add-mask::$target_repository"', helper)
        self.assertIn('echo "::add-mask::$target_repo"', helper)
        review = read(".github/workflows/continuum-pr-agent.yml")
        repair = read(".github/workflows/continuum-pr-agent-repair.yml")
        for body in (review, repair):
            self.assertIn('gh repo clone "$CONTINUUM_PR_AGENT_TARGET_REPOSITORY"', body)
            self.assertIn("-- --quiet >/dev/null 2>&1", body)
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


if __name__ == "__main__":
    unittest.main()
