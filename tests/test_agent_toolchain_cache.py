"""Work Lock #68: pinned, standalone, credential-free GitHub cache bootstrap contract."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/agent-toolchain"


class ToolchainCacheContractTests(unittest.TestCase):
    def test_manifest_pins_exact_releases(self):
        data = json.loads((ACTION / "toolchain.json").read_text())
        self.assertRegex(data["opencode_version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(data["pr_agent_version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(data["cache_epoch"], r"^[a-zA-Z0-9-]+$")

    def test_cache_requires_actual_binary_hash_version_and_release_digest(self):
        action = (ACTION / "action.yml").read_text()
        shell = (ACTION / "bootstrap.sh").read_text()
        self.assertIn("actions/cache@5a3ec84eff668545956fd18022155c47e93e2684", action)
        self.assertIn("sha256sum -c", shell)
        self.assertIn("Missing verified GitHub release-asset digest", shell)
        self.assertIn("SHA256 mismatch", shell)
        self.assertNotIn("image-digest", shell)
        self.assertNotIn("TAP_PAT", action)
        self.assertNotIn("secrets.", action)

    def run_resolve(self, components="opencode", version=""):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "out"
            env = dict(os.environ, GITHUB_ACTION_PATH=str(ACTION),
                       GITHUB_OUTPUT=str(output), RUNNER_OS="Linux", RUNNER_ARCH="X64",
                       CONTINUUM_COMPONENTS=components,
                       CONTINUUM_OPENCODE_VERSION_OVERRIDE=version,
                       CONTINUUM_PR_AGENT_VERSION_OVERRIDE="")
            result = subprocess.run(["bash", str(ACTION / "resolve.sh")], env=env,
                                    capture_output=True, text=True)
            return result, output.read_text() if output.exists() else ""

    def test_version_config_and_cache_key(self):
        result, out = self.run_resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opencode_version=1.19.3", out)
        self.assertIn("Linux-X64", out)
        self.assertIn("cache_key=", out)

    def test_invalid_version_fails_closed(self):
        result, _ = self.run_resolve(version="latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid exact SemVer", result.stderr)

    def test_invalid_component_fails_closed(self):
        result, _ = self.run_resolve(components="unknown")
        self.assertNotEqual(result.returncode, 0)

    def test_no_cached_user_or_secret_data(self):
        action = (ACTION / "action.yml").read_text()
        self.assertIn("~/.cache/continuum/toolchains", action)
        self.assertNotIn("~/.config/opencode", action)
        self.assertNotIn("~/.local/share/opencode", action)
        self.assertNotIn("~/.aws", action)


if __name__ == "__main__":
    unittest.main()
