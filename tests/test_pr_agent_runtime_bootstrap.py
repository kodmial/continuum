from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-pr-agent.yml"
STEP_NAME = "Resolve the Continuum-owned PR-Agent runtime bundle"
SOURCE_URL = "https://github.com/kodmial/continuum.git"
RUNTIME_FILES = (
    (".github/scripts/pr_agent_bridge.py", ".github/scripts/pr_agent_bridge.py"),
    ("src/continuum/pr_agent.py", "src/continuum/pr_agent.py"),
    (".github/scripts/pr_agent_policy.js", ".github/scripts/pr_agent_policy.js"),
    ("src/continuum/pr_agent_fallback_state.py", "src/continuum/pr_agent_fallback_state.py"),
)
EXPECTED_OUTPUTS = ("bridge_script", "policy_script", "fallback_pythonpath")


def load_runtime_step() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["pr_agent"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step
    raise AssertionError(f"missing workflow step: {STEP_NAME}")


class RuntimeBootstrapContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = load_runtime_step()
        self.script = self.step["run"]

    def test_step_uses_one_shallow_git_source_checkout_without_rest_reads(self) -> None:
        self.assertNotIn("gh api", self.script)
        self.assertNotIn("/contents/", self.script)
        self.assertNotIn("repos/kodmial/continuum", self.script)
        self.assertNotIn("secrets.TAP_PAT", self.script)
        self.assertNotIn("github.token", self.script)
        self.assertIn(SOURCE_URL, self.script)
        self.assertIn('fetch --quiet --depth=1 --no-tags origin "$CONTINUUM_REF"', self.script)
        self.assertIn("checkout --quiet --detach FETCH_HEAD", self.script)
        self.assertIn("GIT_TERMINAL_PROMPT=0", self.script)
        self.assertIn("credential.helper=", self.script)
        self.assertIn("command -v git", self.script)
        self.assertIn('rev-parse HEAD', self.script)
        self.assertIn('^[0-9a-fA-F]{40}$', self.script)

    def test_step_materializes_the_same_paths_and_outputs(self) -> None:
        self.assertEqual(self.script.count('copy_runtime_file "'), 4)
        self.assertEqual(self.script.count('>> "$GITHUB_OUTPUT"'), 3)
        for source, target in RUNTIME_FILES:
            with self.subTest(source=source):
                self.assertIn(f'copy_runtime_file "{source}"', self.script)
                self.assertIn(target, self.script)
        self.assertIn('echo "bridge_script=$BRIDGE_SCRIPT" >> "$GITHUB_OUTPUT"', self.script)
        self.assertIn('echo "policy_script=$POLICY_SCRIPT" >> "$GITHUB_OUTPUT"', self.script)
        self.assertIn('echo "fallback_pythonpath=$RUNTIME_ROOT/src" >> "$GITHUB_OUTPUT"', self.script)
        self.assertIn("python3 -m py_compile", self.script)
        self.assertIn("node --check", self.script)
        for export in (
            "isCleanReviewForImproveSkip",
            "hasIncompleteCoverageSignal",
            "hasToolErrorSignal",
            "hasBlockingSecuritySignal",
            "isSkippedCleanImprovePayload",
            "qualifyingImproveSuggestions",
            "unwrapReview",
        ):
            with self.subTest(export=export):
                self.assertIn(export, self.script)


class RuntimeBootstrapExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = ROOT / ".opencode-tmp"
        cls.base.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=cls.base)
        cls.fixture = Path(cls.tmp.name) / "trusted-source"
        cls.bash = shutil.which("bash")
        cls.real_git = shutil.which("git")
        assert cls.bash and cls.real_git
        subprocess.run([cls.real_git, "init", "--quiet", "--initial-branch=main", cls.fixture], check=True)
        cls._git("config", "user.email", "fixture@example.invalid")
        cls._git("config", "user.name", "Fixture")
        cls.first_bytes = {}
        for source, _ in RUNTIME_FILES:
            destination = cls.fixture / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = (ROOT / source).read_bytes()
            cls.first_bytes[source] = data
            destination.write_bytes(data)
        cls._commit("first")
        cls.first_sha = cls._head()
        cls.second_bytes = {}
        for source, _ in RUNTIME_FILES:
            destination = cls.fixture / source
            marker = b"\n// fixture second commit\n" if source.endswith(".js") else b"\n# fixture second commit\n"
            data = cls.first_bytes[source] + marker
            cls.second_bytes[source] = data
            destination.write_bytes(data)
        cls._commit("second")
        cls.second_sha = cls._head()
        cls._git("checkout", "--quiet", "-b", "missing")
        (cls.fixture / "src/continuum/pr_agent_fallback_state.py").unlink()
        cls._commit("missing fallback")
        cls.missing_sha = cls._head()
        cls._git("checkout", "--quiet", "main")
        cls.step = load_runtime_step()
        cls.script = cls.step["run"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @classmethod
    def _git(cls, *args: str) -> None:
        subprocess.run(
            [cls.real_git, "-C", str(cls.fixture), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def _commit(cls, message: str) -> None:
        cls._git("add", "-A")
        cls._git("commit", "--quiet", "-m", message)

    @classmethod
    def _head(cls) -> str:
        return subprocess.run(
            [cls.real_git, "-C", str(cls.fixture), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _run(self, ref: str, *, deny_gh: bool = False, git_wrapper: bool = False):
        work = Path(tempfile.mkdtemp(dir=self.base))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        runner_temp = work / "runner"
        runner_temp.mkdir()
        output = work / "github-output"
        output.write_text("", encoding="utf-8")
        target = work / "untrusted-target"
        for source, _ in RUNTIME_FILES:
            destination = target / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("untrusted target content\n", encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(output),
                "CONTINUUM_REF": ref,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{self.fixture.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": SOURCE_URL,
            }
        )
        env.pop("GH_TOKEN", None)
        env.pop("TAP_PAT", None)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        if deny_gh:
            marker = work / "gh-invoked"
            gh = bin_dir / "gh"
            gh.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 77\n", encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
        if git_wrapper:
            wrapper = bin_dir / "git"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "${1:-}" == "-C" && "${3:-}" == "rev-parse" && "${4:-}" == "HEAD" ]]; then\n'
                '  echo "0000000000000000000000000000000000000000"\n'
                "  exit 0\n"
                "fi\n"
                f'exec "{self.real_git}" "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

        process = subprocess.run(
            [self.bash, "-c", self.script],
            cwd=target,
            env=env,
            capture_output=True,
            text=True,
        )
        return process, output.read_text(encoding="utf-8"), runner_temp, work

    def test_branch_ref_loads_exact_bytes_and_downstream_paths(self) -> None:
        process, output, runner_temp, work = self._run("main")
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertNotIn("::error::", process.stdout + process.stderr)
        runtime = runner_temp / "continuum-pr-agent-runtime"
        for source, target in RUNTIME_FILES:
            with self.subTest(source=source):
                self.assertEqual((runtime / target).read_bytes(), self.second_bytes[source])
        outputs = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        self.assertEqual(
            outputs["bridge_script"], str(runtime / ".github/scripts/pr_agent_bridge.py")
        )
        self.assertEqual(
            outputs["policy_script"], str(runtime / ".github/scripts/pr_agent_policy.js")
        )
        self.assertEqual(outputs["fallback_pythonpath"], str(runtime / "src"))
        self.assertEqual((runtime / "src/continuum/__init__.py").read_bytes(), b"")
        self.assertFalse((work / "gh-invoked").exists())

    def test_pinned_sha_ignores_moving_head_and_untrusted_target_files(self) -> None:
        process, _, runner_temp, _ = self._run(self.first_sha)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        runtime = runner_temp / "continuum-pr-agent-runtime"
        for source, target in RUNTIME_FILES:
            with self.subTest(source=source):
                self.assertEqual((runtime / target).read_bytes(), self.first_bytes[source])
                self.assertNotEqual((runtime / target).read_bytes(), self.second_bytes[source])

    def test_missing_ref_fails_closed_without_fallback(self) -> None:
        process, output, runner_temp, work = self._run(
            "refs/heads/does-not-exist", deny_gh=True
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Unable to fetch the pinned Continuum PR-Agent runtime ref", process.stdout)
        self.assertNotIn("bridge_script=", output)
        self.assertFalse(
            (runner_temp / "continuum-pr-agent-runtime/.github/scripts/pr_agent_bridge.py").exists()
        )
        self.assertFalse((work / "gh-invoked").exists())

    def test_absent_module_fails_closed_without_rest_fallback(self) -> None:
        process, output, runner_temp, work = self._run(self.missing_sha, deny_gh=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(
            "missing required file: src/continuum/pr_agent_fallback_state.py",
            process.stdout,
        )
        self.assertNotIn("bridge_script=", output)
        self.assertFalse(
            (runner_temp / "continuum-pr-agent-runtime/src/continuum/pr_agent_fallback_state.py").exists()
        )
        self.assertFalse((work / "gh-invoked").exists())

    def test_rest_denial_does_not_affect_git_source_loading(self) -> None:
        process, _, runner_temp, work = self._run("main", deny_gh=True)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertFalse((work / "gh-invoked").exists())
        runtime = runner_temp / "continuum-pr-agent-runtime"
        for source, target in RUNTIME_FILES:
            with self.subTest(source=source):
                self.assertEqual((runtime / target).read_bytes(), self.second_bytes[source])

    def test_git_unavailability_fails_clearly(self) -> None:
        work = Path(tempfile.mkdtemp(dir=self.base))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        runner_temp = work / "runner"
        runner_temp.mkdir()
        output = work / "github-output"
        output.write_text("", encoding="utf-8")
        empty_bin = work / "empty-bin"
        empty_bin.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(output),
                "CONTINUUM_REF": "main",
                "PATH": str(empty_bin),
            }
        )
        process = subprocess.run(
            [self.bash, "-c", self.script],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Git is required", process.stdout)
        self.assertNotIn("bridge_script=", output.read_text(encoding="utf-8"))
        self.assertFalse((runner_temp / "continuum-pr-agent-runtime").exists())

    def test_sha_mismatch_fails_closed(self) -> None:
        process, output, _, _ = self._run(self.first_sha, git_wrapper=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("instead of the pinned commit", process.stdout)
        self.assertNotIn("bridge_script=", output)


if __name__ == "__main__":
    unittest.main()
