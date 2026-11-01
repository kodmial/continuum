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


    def test_attestation_secret_is_scoped_to_runner_verification(self):
        step = self.body.split("      - name: Implement issue", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn(
            "CONTINUUM_VERIFICATION_ATTESTATION_KEY: ${{ secrets.CONTINUUM_VERIFICATION_ATTESTATION_KEY }}",
            step,
        )
        self.assertIn(
            'CONTINUUM_ATTESTATION_KEY_VALUE="${CONTINUUM_VERIFICATION_ATTESTATION_KEY:-}"',
            step,
        )
        self.assertIn("unset CONTINUUM_VERIFICATION_ATTESTATION_KEY", step)
        self.assertIn(
            'ATTESTATION_KEY_FILE="${RUNNER_TEMP:-/tmp}/continuum-attestation-key"',
            step,
        )
        self.assertIn(
            "python3 -m continuum.verification provision-key --repo \"$GITHUB_WORKSPACE\"",
            step,
            "the key must be provisioned from stdin through the file-based contract",
        )
        self.assertIn(
            'CONTINUUM_VERIFICATION_ATTESTATION_KEY_FILE="$ATTESTATION_KEY_FILE"',
            step,
        )
        self.assertNotIn(
            'CONTINUUM_VERIFICATION_ATTESTATION_KEY="$CONTINUUM_ATTESTATION_KEY_VALUE"',
            step,
            "the value form is rejected by the verification engine",
        )
        self.assertLess(
            step.index("unset CONTINUUM_VERIFICATION_ATTESTATION_KEY"),
            step.index("continuum-opencode run --auto"),
        )
        self.assertNotIn("export CONTINUUM_ATTESTATION_KEY_VALUE", step)

if __name__ == "__main__":
    unittest.main()
