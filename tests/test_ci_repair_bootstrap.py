"""Regression proof for the shared CI-repair controller bootstrap.

The reusable controller has no content permissions or consumer PAT during
metadata admission; both of its entrypoints must fetch the PUBLIC Continuum
classifier successfully without passing an empty checkout token.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/continuum-opencode-repair.yml"
PUBLIC_REMOTE = "https://github.com/kodmial/continuum.git"
BOOTSTRAP_NAME = "Fetch public CI repair classifier without credentials"
JOBS = ("ci-repair", "ci-repair-dispatch")


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True,
        timeout=15,
    )
    return result.stdout.strip()


def make_source_repo(path: Path, *, complete: bool = True) -> Path:
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "ci-repair-test@example.invalid", cwd=path)
    git("config", "user.name", "CI Repair Test", cwd=path)
    (path / "src/continuum").mkdir(parents=True)
    (path / "src/continuum/opencode_repair_state.py").write_text(
        "# test classifier\n", encoding="utf-8"
    )
    if complete:
        (path / ".github/scripts").mkdir(parents=True)
        (path / ".github/scripts/opencode_repair_state.py").write_text(
            "# test CLI\n", encoding="utf-8"
        )
    (path / "unrelated.txt").write_text("must not be fetched\n", encoding="utf-8")
    git("add", ".", cwd=path)
    git("commit", "-qm", "source fixture", cwd=path)
    return path


class CIRepairBootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.raw)
        cls.steps = {}
        for job in JOBS:
            matches = [
                step for step in cls.workflow["jobs"][job]["steps"]
                if step.get("name") == BOOTSTRAP_NAME
            ]
            if len(matches) != 1:
                raise AssertionError(
                    f"{job} must have exactly one common classifier bootstrap"
                )
            cls.steps[job] = matches[0]

    def test_both_paths_preserve_read_only_metadata_permissions(self):
        self.assertEqual({}, self.workflow["permissions"])
        self.assertNotRegex(self.raw, r"(?m)^\s+token:\s*(?:\'\'|\"\")\s*$")
        for job in JOBS:
            with self.subTest(job=job):
                step = self.steps[job]
                self.assertEqual("bash", step["shell"])
                self.assertEqual("${{ inputs.continuum_ref || 'main' }}", step["env"]["CONTINUUM_REF"])
                self.assertNotIn("uses", step)
                self.assertNotIn("secrets.", step["run"])
                self.assertIn(PUBLIC_REMOTE, step["run"])
                self.assertIn("credential.helper=", step["run"])
                self.assertIn("GIT_TERMINAL_PROMPT=0", step["run"])
                self.assertIn("git check-ref-format", step["run"])
                self.assertIn("sparse-checkout set --no-cone", step["run"])
                self.assertIn("test -f", step["run"])
        self.assertEqual(self.steps[JOBS[0]]["run"], self.steps[JOBS[1]]["run"])

    def run_real_bootstrap(self, source: Path, dest: Path, ref: str):
        dest.mkdir()
        script = self.steps[JOBS[0]]["run"].replace(PUBLIC_REMOTE, source.as_uri())
        env = os.environ.copy()
        env["CONTINUUM_REF"] = ref
        env.pop("GITHUB_TOKEN", None)
        env.pop("TAP_PAT", None)
        return subprocess.run(
            ["bash", "-c", script], cwd=dest, env=env, text=True,
            capture_output=True, check=False, timeout=20,
        )

    def test_actual_bootstrap_fetches_only_required_files_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source_repo(root / "source")
            for label, ref in (("branch", "main"), ("sha", git("rev-parse", "HEAD", cwd=source))):
                with self.subTest(ref=label):
                    dest = root / label
                    result = self.run_real_bootstrap(source, dest, ref)
                    self.assertEqual(0, result.returncode, result.stderr)
                    checked_out = dest / ".continuum-repair-engine"
                    self.assertTrue((checked_out / ".github/scripts/opencode_repair_state.py").is_file())
                    self.assertTrue((checked_out / "src/continuum/opencode_repair_state.py").is_file())
                    self.assertFalse((checked_out / "unrelated.txt").exists())

    def test_bad_ref_is_rejected_before_fetch_and_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source_repo(root / "incomplete", complete=False)
            invalid = self.run_real_bootstrap(source, root / "invalid", "--upload-pack=command")
            self.assertNotEqual(0, invalid.returncode)
            self.assertIn("Invalid Continuum ref", invalid.stderr)
            self.assertFalse((root / "invalid/.continuum-repair-engine").exists())
            missing = self.run_real_bootstrap(source, root / "missing", "main")
            self.assertNotEqual(0, missing.returncode)
            self.assertFalse(
                (root / "missing/.continuum-repair-engine/.github/scripts/opencode_repair_state.py").exists()
            )


if __name__ == "__main__":
    unittest.main()
