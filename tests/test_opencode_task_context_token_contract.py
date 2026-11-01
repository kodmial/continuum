from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "continuum-opencode.yml"


class AuthoritativeTaskContextTokenContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        body = WORKFLOW.read_text(encoding="utf-8")
        start = body.index("- name: Resolve authoritative task context")
        end = body.index("- name: Check issue Definition of Ready", start)
        cls.resolver = body[start:end]

    def test_same_repo_context_uses_repository_token(self):
        self.assertIn("github-token: ${{ github.token }}", self.resolver)
        self.assertNotIn("github-token: ${{ secrets.TAP_PAT }}", self.resolver)
        self.assertIn("github.rest.issues.get", self.resolver)
        self.assertIn(
            "taskRepository.toLowerCase() === repository.toLowerCase()",
            self.resolver,
        )

    def test_pat_is_reserved_for_cross_repo_or_liveness_fallback(self):
        self.assertIn("TAP_PAT: ${{ secrets.TAP_PAT }}", self.resolver)
        self.assertIn("GITHUB_API_URL: ${{ github.api_url }}", self.resolver)
        self.assertIn("async function fetchWithTapPat", self.resolver)
        self.assertIn("authorization: `Bearer ${tapPat}`", self.resolver)
        self.assertIn("Cross-repository authoritative task", self.resolver)
        self.assertIn("[401, 403, 429].includes(status)", self.resolver)
        self.assertIn("falling back to TAP_PAT for liveness", self.resolver)

    def test_resolver_does_not_require_secondary_octokit_module(self):
        self.assertNotIn("require('@actions/github')", self.resolver)
        self.assertNotIn('require("@actions/github")', self.resolver)


class IssueStartTokenContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        body = WORKFLOW.read_text(encoding="utf-8")
        readiness_start = body.index("- name: Check issue Definition of Ready")
        readiness_end = body.index("- name: Prepare consumer agent environment", readiness_start)
        cls.readiness = body[readiness_start:readiness_end]

        guard_start = body.index("- name: Skip duplicate issue implementation")
        guard_end = body.index("# workflow_dispatch repair modes", guard_start)
        cls.duplicate_guard = body[guard_start:guard_end]

    def test_readiness_reads_use_repo_token_but_mutations_keep_pat(self):
        self.assertIn("github-token: ${{ github.token }}", self.readiness)
        self.assertIn("TAP_PAT: ${{ secrets.TAP_PAT }}", self.readiness)
        self.assertNotIn("github-token: ${{ secrets.TAP_PAT }}", self.readiness)
        self.assertIn("GITHUB_API_URL: ${{ github.api_url }}", self.readiness)
        self.assertNotIn("new github.constructor", self.readiness)
        self.assertNotIn("require('@actions/github')", self.readiness)
        self.assertIn("async function patRequest", self.readiness)
        self.assertIn("async function readWithFallback", self.readiness)
        self.assertIn("github.rest.issues.get", self.readiness)
        self.assertIn("github.paginate(", self.readiness)
        self.assertIn("github.rest.issues.listComments", self.readiness)
        self.assertIn("method: 'DELETE'", self.readiness)
        self.assertIn("method: 'POST'", self.readiness)
        self.assertIn("[401, 403, 429].includes(status)", self.readiness)
    def test_duplicate_guard_is_same_repo_read_only_on_repo_token(self):
        self.assertIn("github-token: ${{ github.token }}", self.duplicate_guard)
        self.assertNotIn("github-token: ${{ secrets.TAP_PAT }}", self.duplicate_guard)
        self.assertIn("TAP_PAT: ${{ secrets.TAP_PAT }}", self.duplicate_guard)
        self.assertIn("GITHUB_API_URL: ${{ github.api_url }}", self.duplicate_guard)
        self.assertIn("github.rest.pulls.list", self.duplicate_guard)
        self.assertIn("listOpenPrsWithPat", self.duplicate_guard)
        self.assertIn("[401, 403, 429].includes(status)", self.duplicate_guard)
        self.assertNotIn("gh pr list", self.duplicate_guard)


if __name__ == "__main__":
    unittest.main()
