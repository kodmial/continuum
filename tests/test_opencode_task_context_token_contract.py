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


if __name__ == "__main__":
    unittest.main()
