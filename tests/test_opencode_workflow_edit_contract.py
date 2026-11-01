from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"


class OpenCodeWorkflowEditContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = WORKFLOW.read_text(encoding="utf-8")

    def test_issue_mode_allows_workflow_file_changes(self):
        self.assertIn(
            "You may modify .github/workflows/** when the issue requires it.",
            self.body,
        )
        self.assertNotIn("Do not modify .github/workflows/**", self.body)
        self.assertNotIn("OpenCode modified .github/workflows/**", self.body)
        self.assertNotIn("Task commit contains .github/workflows/** changes.", self.body)
        self.assertNotIn(
            "Task differs from its branch base under .github/workflows/**.",
            self.body,
        )

    def test_workflow_changes_are_published_only_through_task_branch_pr(self):
        self.assertIn("token: ${{ secrets.TAP_PAT }}", self.body)
        self.assertIn(
            "TAP_PAT must be a classic PAT with repo + workflow scopes.",
            self.body,
        )
        self.assertIn(
            'BRANCH="opencode/issue${ISSUE_NUMBER}-${GITHUB_RUN_ID}"',
            self.body,
        )
        self.assertIn(
            'git push --force-with-lease="refs/heads/$BRANCH:$BASE_SHA" -u origin "HEAD:$BRANCH"',
            self.body,
        )
        self.assertIn('gh pr create --repo "$GITHUB_REPOSITORY"', self.body)
        self.assertIn('--base "$BASE_REF"', self.body)
        self.assertIn('--head "$BRANCH"', self.body)
        self.assertNotIn("git push origin main", self.body)
        self.assertNotIn("git push -u origin main", self.body)


if __name__ == "__main__":
    unittest.main()
