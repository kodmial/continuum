"""Contract regression for the reusable Bun binary release adapter."""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-bun-binary.yml"


class BunReleaseWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_exact_build_and_publish_identity_are_available(self):
        for token in (
            "build_argv_json",
            "build_env_json",
            "expected_version",
            "expected_sha256",
            "expected_size_bytes",
            "subprocess.run(argv, env=child_env, check=True)",
            "produced binary SHA-256 does not match expected_sha256",
            "produced binary size does not match expected_size_bytes",
            '"source_sha": "$SOURCE_SHA"',
            '"binary_version": "$BINARY_VERSION"',
        ):
            self.assertIn(token, self.text)

    def test_build_argv_never_uses_shell_execution(self):
        self.assertNotIn("shell=True", self.text)


if __name__ == "__main__":
    unittest.main()
