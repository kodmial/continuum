import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "pr_agent_target.sh"


class PrAgentTargetContextTests(unittest.TestCase):
    def run_target(self, child_id="", resolver_body=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scripts = root / ".github" / "scripts"
            scripts.mkdir(parents=True)
            calls = root / "calls.txt"
            env_file = root / "github-env.txt"

            if resolver_body is not None:
                resolver = scripts / "delegation_repository.sh"
                resolver.write_text(
                    textwrap.dedent(resolver_body).replace("__CALLS__", str(calls)),
                    encoding="utf-8",
                )
                resolver.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "GITHUB_REPOSITORY": "owner/parent",
                    "CONTINUUM_ENGINE_ROOT": str(root),
                    "GITHUB_ENV": str(env_file),
                    "RUNNER_TEMP": td,
                    # Unit subprocess output is captured before the GitHub
                    # runner command processor can apply ::add-mask::. Keep
                    # this test in plain-shell mode; the workflow contract
                    # separately asserts that delegated Actions runs install
                    # the required masks.
                    "GITHUB_ACTIONS": "false",
                }
            )
            proc = subprocess.run(
                ["bash", str(SCRIPT), child_id],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            values = {}
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    values[key] = value
            return proc, values, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_local_target_defaults_to_execution_repository(self):
        proc, values, calls = self.run_target()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(values["CONTINUUM_PR_AGENT_TARGET_REPOSITORY"], "owner/parent")
        self.assertEqual(values["CONTINUUM_PR_AGENT_TARGET_OWNER"], "owner")
        self.assertEqual(values["CONTINUUM_PR_AGENT_TARGET_REPO"], "parent")
        self.assertEqual(values["CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"], "false")
        self.assertEqual(calls, "")

    def test_delegated_target_uses_existing_resolve_and_verify_contract(self):
        proc, values, calls = self.run_target(
            "opaque-a",
            """
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "__CALLS__"
            case "$1" in
              resolve) printf 'private-owner/private-child\\n' ;;
              verify) [[ "$2" == opaque-a && "$3" == private-owner/private-child ]] ;;
              *) exit 2 ;;
            esac
            """,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            values["CONTINUUM_PR_AGENT_TARGET_REPOSITORY"],
            "private-owner/private-child",
        )
        self.assertEqual(values["CONTINUUM_PR_AGENT_TARGET_IS_DELEGATED"], "true")
        self.assertIn("resolve opaque-a", calls)
        self.assertIn("verify opaque-a private-owner/private-child", calls)
        self.assertNotIn("private-owner/private-child", proc.stdout)
        self.assertNotIn("private-owner/private-child", proc.stderr)

    def test_delegated_resolution_failure_is_fail_closed(self):
        proc, values, _ = self.run_target(
            "opaque-a",
            """
            #!/usr/bin/env bash
            exit 4
            """,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(values, {})
        self.assertIn("failed closed", proc.stderr)

    def test_empty_delegated_resolution_is_fail_closed(self):
        proc, values, _ = self.run_target(
            "opaque-a",
            """
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$1" == resolve ]]; then
              exit 0
            fi
            exit 2
            """,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(values, {})
        self.assertIn("failed closed", proc.stderr)

    def test_invalid_resolved_repository_is_rejected_without_echoing_identity(self):
        proc, values, _ = self.run_target(
            "opaque-a",
            """
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$1" == resolve ]]; then
              printf 'not a repository\\n'
            else
              exit 0
            fi
            """,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(values, {})
        self.assertNotIn("not a repository", proc.stdout)
        self.assertNotIn("not a repository", proc.stderr)


if __name__ == "__main__":
    unittest.main()
