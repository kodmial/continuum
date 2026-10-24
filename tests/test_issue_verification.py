"""Issue-mode baseline / verifier / final verification contract (#168).

These tests exercise the production resolver and evidence engine against real
temporary Git repositories. They are deterministic and do not call a model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuum import verification

ROOT = Path(__file__).resolve().parents[1]
TMP_PARENT = ROOT / ".opencode-tmp"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def run_git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    completed = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "git {} failed: {}".format(" ".join(args), completed.stderr[-2000:])
        )
    return (completed.stdout or "").strip()


class VerificationContractTest(unittest.TestCase):
    def setUp(self):
        TMP_PARENT.mkdir(parents=True, exist_ok=True)
        self.workdir = Path(
            tempfile.mkdtemp(prefix="verification-", dir=str(TMP_PARENT))
        )
        self.repo = self.workdir / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(self.repo)], check=True, capture_output=True
        )
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "initial")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.evidence = self.repo / verification.EVIDENCE_FILE_NAME
        self.env = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def make_ci(self, **commands: str) -> None:
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        lines = [
            "name: CI",
            "on:",
            "  push:",
            "    branches: [main]",
            "jobs:",
            "  validate:",
            "    uses: kodmial/continuum/.github/workflows/continuum-validation.yml@main",
            "    with:",
        ]
        for name, command in commands.items():
            lines.append("      {}_command: {}".format(name, json.dumps(command)))
        (workflows / "ci.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_unconfigured_contract_is_an_explicit_failure(self):
        contract = verification.resolve_contract(self.repo, self.env)
        self.assertEqual("unconfigured", contract.state)
        self.assertFalse(contract.configured)
        self.assertIn("No authoritative verification command", contract.errors[0])
        with self.assertRaises(verification.VerificationError):
            verification.ensure_baseline(
                self.repo, self.evidence, self.base, self.env
            )

    def test_baseline_refuses_a_dirty_checkout(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(
            verification.VerificationError, "uncommitted task changes"
        ):
            verification.ensure_baseline(
                self.repo, self.evidence, self.base, self.env
            )
        self.assertFalse(self.evidence.exists())

    def test_hidden_consumer_commands_come_from_existing_contract(self):
        self.make_ci(
            build="python3 -c \"print('consumer-build')\"",
            test="python3 -c \"print('consumer-test')\"",
        )
        contract = verification.resolve_contract(self.repo, self.env)
        self.assertEqual("configured", contract.state)
        self.assertEqual("ci-caller", contract.source)
        self.assertEqual(
            ["build", "test"], [command.name for command in contract.commands]
        )
        self.assertEqual(
            "python3 -c \"print('consumer-test')\"", contract.commands[1].command
        )

        # Repository variables are the documented fallback, not a hard-coded
        # project language.
        (self.repo / ".github/workflows/ci.yml").unlink()
        variables = dict(self.env)
        variables["CONTINUUM_VALIDATION_COMMAND"] = "custom-check --all"
        fallback = verification.resolve_contract(self.repo, variables)
        self.assertEqual("repository-variables", fallback.source)
        self.assertEqual("custom-check --all", fallback.commands[0].command)

    def test_unsupported_ci_expression_cannot_fall_back_to_another_check(self):
        self.make_ci(test="${{ secrets.UNRELATED_TEST }}")
        self.env["CONTINUUM_TEST_COMMAND"] = "different-check"
        contract = verification.resolve_contract(self.repo, self.env)
        self.assertEqual("unconfigured", contract.state)
        self.assertIn("unsupported GitHub expression", contract.errors[0])
        with self.assertRaises(verification.VerificationError):
            verification.ensure_baseline(
                self.repo, self.evidence, self.base, self.env
            )

    def test_baseline_runs_once_then_is_reused_on_a_checkpoint(self):
        marker = self.repo / "baseline-runs.txt"
        self.env["CONTINUUM_TEST_COMMAND"] = (
            "python3 -c \"from pathlib import Path; "
            "p=Path('baseline-runs.txt'); "
            "p.write_text((p.read_text() if p.exists() else '') + 'run\\n')\""
        )
        data, reused = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertFalse(reused)
        self.assertEqual("passed", data["baseline"]["state"])
        self.assertEqual(1, marker.read_text(encoding="utf-8").count("run"))

        (self.repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "implementation")
        data, reused = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertTrue(reused)
        self.assertEqual(1, marker.read_text(encoding="utf-8").count("run"))

    def test_failed_baseline_is_distinct_and_never_becomes_green(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "test -f fixed.txt"
        data, _ = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertEqual("failed", data["baseline"]["state"])

        final_data, comparison = verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertFalse(comparison["accepted"])
        self.assertEqual("failed-preexisting", comparison["result"])
        self.assertEqual(["test"], comparison["preexisting_failures"])
        self.assertEqual([], comparison["new_failures"])
        self.assertEqual("failed", final_data["final"]["state"])

    def test_preexisting_failure_is_resolved_by_a_passing_final_run(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "test -f fixed.txt"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        (self.repo / "fixed.txt").write_text("fixed\n", encoding="utf-8")
        data, comparison = verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertTrue(comparison["accepted"])
        self.assertEqual("passed-after-preexisting-failure", comparison["result"])
        self.assertEqual(["test"], comparison["resolved_failures"])
        self.assertEqual("passed", data["final"]["state"])

    def test_environment_change_between_baseline_and_final_fails_closed(self):
        baseline_env = dict(self.env)
        baseline_env["RUNNER_OS"] = "Linux"
        baseline_env["CONTINUUM_TEST_COMMAND"] = "true"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, baseline_env
        )
        final_env = dict(baseline_env)
        final_env["RUNNER_OS"] = "macOS"
        with self.assertRaisesRegex(
            verification.VerificationError, "environment changed"
        ):
            verification.run_final(
                self.repo, self.evidence, self.base, final_env
            )

    def test_resumed_runner_environment_change_reestablishes_a_baseline(self):
        baseline_env = dict(self.env)
        baseline_env["RUNNER_OS"] = "Linux"
        baseline_env["CONTINUUM_TEST_COMMAND"] = 'test "$RUNNER_OS" = "macOS"'
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, baseline_env
        )

        final_env = dict(baseline_env)
        final_env["RUNNER_OS"] = "macOS"
        final_data, comparison = verification.run_final(
            self.repo,
            self.evidence,
            self.base,
            final_env,
            resume_stage="tests-running",
        )
        self.assertTrue(comparison["accepted"])
        self.assertTrue(final_data["baseline"]["environment_rebaselined"])
        self.assertEqual("passed", final_data["baseline"]["state"])
        self.assertEqual(
            final_data["final"]["environment_fingerprint"],
            final_data["baseline"]["environment_fingerprint"],
        )

    def test_resume_reuses_final_evidence_before_environment_comparison(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        before = self.evidence.read_text(encoding="utf-8")
        final_env = dict(self.env)
        final_env["RUNNER_OS"] = "macOS"
        data, comparison = verification.run_final(
            self.repo,
            self.evidence,
            self.base,
            final_env,
            resume_stage="publish-pr",
        )
        self.assertTrue(comparison["accepted"])
        self.assertEqual(
            before,
            self.evidence.read_text(encoding="utf-8"),
            "a resumed runner must reuse the exact passed final evidence",
        )

    def test_new_regression_is_distinguishable_from_baseline(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "test ! -f broken.txt"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        (self.repo / "broken.txt").write_text("regression\n", encoding="utf-8")
        data, comparison = verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertFalse(comparison["accepted"])
        self.assertEqual("failed-new-regression", comparison["result"])
        self.assertEqual(["test"], comparison["new_failures"])
        self.assertEqual("failed", data["final"]["state"])

    def test_multi_line_command_fails_like_the_validation_contract_shell(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "false\ntrue"
        data, _ = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertEqual("failed", data["baseline"]["state"])
        self.assertEqual(["test"], data["baseline"]["failed_commands"])

    def test_checker_failure_is_reported_instead_of_silently_replaced(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        self.env["CONTINUUM_VERIFICATION_TIMEOUT_SECONDS"] = "not-a-number"
        with self.assertRaises(verification.VerificationError):
            verification.ensure_baseline(
                self.repo, self.evidence, self.base, self.env
            )
        self.assertFalse(self.evidence.exists())

        self.evidence.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification.run_final(
                self.repo, self.evidence, self.base, self.env
            )

    def test_fresh_verifier_can_correct_incomplete_work_then_final_repeats(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "test -f complete.txt"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )

        # The primary implementation is deliberately incomplete.
        (self.repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.assertTrue(verification.verifier_required(self.evidence))

        # The independent verifier fixes the omission in the same checkout.
        (self.repo / "complete.txt").write_text("done\n", encoding="utf-8")
        verifier = verification.record_verifier(
            self.repo, self.evidence, 0, self.env
        )
        self.assertEqual("passed", verifier["state"])
        self.assertIn("complete.txt", verifier["changed_paths"])
        self.assertFalse(verification.verifier_required(self.evidence))

        data, comparison = verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertTrue(comparison["accepted"])
        self.assertEqual("passed", data["final"]["state"])
        # Baseline and final are provably the same resolved command set.
        self.assertEqual(
            data["contract"]["fingerprint"],
            data["final"]["contract_fingerprint"],
        )

    def test_resume_reuses_exact_final_evidence_without_new_coding(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        verification.record_verifier(self.repo, self.evidence, 0, self.env)
        verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        before = self.evidence.read_text(encoding="utf-8")
        data, comparison = verification.run_final(
            self.repo,
            self.evidence,
            self.base,
            self.env,
            resume_stage="tests-passed",
        )
        self.assertTrue(comparison["accepted"])
        self.assertEqual(
            before,
            self.evidence.read_text(encoding="utf-8"),
            "a completed checkpoint must not rewrite exact final evidence",
        )

    def test_committed_workflow_metadata_is_not_a_task_change(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        (self.repo / ".continuum-429-stage").write_text(
            "implementation-complete\n", encoding="utf-8"
        )
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "workflow metadata")
        self.assertFalse(
            verification.task_changes_present(self.repo, self.base)
        )

    def test_consumer_commands_do_not_inherit_the_engine_pythonpath(self):
        engine_src = str((ROOT / "src").resolve())
        consumer_src = str((self.repo / "consumer-src").resolve())
        env = dict(self.env)
        env["PYTHONPATH"] = os.pathsep.join([engine_src, consumer_src])
        contract = verification.VerificationContract(
            state="configured",
            source="test",
            commands=(
                verification.VerificationCommand(
                    name="test",
                    command="python3 -c \"import os; print(os.environ.get('PYTHONPATH',''))\"",
                    source="test",
                ),
            ),
        )
        result = verification.run_command_set(contract, self.repo, env)
        output = result["commands"][0]["output_tail"].strip()
        self.assertEqual(consumer_src, output)
        self.assertNotIn(engine_src, output)

    def test_verification_evidence_is_not_a_task_change(self):
        self.env["CONTINUUM_TEST_COMMAND"] = "true"
        verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertFalse(
            verification.task_changes_present(self.repo, self.base)
        )
        (self.repo / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
        self.assertTrue(
            verification.task_changes_present(self.repo, self.base)
        )
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "implementation")
        self.assertTrue(
            verification.task_changes_present(self.repo, self.base)
        )
        with self.assertRaisesRegex(
            verification.VerificationError, "base SHA is malformed"
        ):
            verification.task_changes_present(self.repo, "not-a-sha")

    def test_continuum_repository_resolves_its_own_authoritative_contract(self):
        # The production repository's CI caller names its own validation
        # script, so issue mode can resolve a real contract without a
        # hard-coded language command.
        contract = verification.resolve_contract(ROOT, os.environ)
        self.assertEqual("configured", contract.state)
        self.assertIn(
            "bash scripts/validate-continuum.sh",
            [command.command for command in contract.commands],
        )

    def test_issue_mode_canary_keeps_distinct_baseline_verifier_final_evidence(self):
        # A canary fixture proves the full ordered evidence shape the workflow
        # records for an issue implementation.
        self.make_ci(test="test -f canary.txt")
        (self.repo / "canary.txt").write_text("baseline\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "canary baseline")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        data, _ = verification.ensure_baseline(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertEqual("passed", data["baseline"]["state"])
        # The primary implementation removes a required file; the fresh
        # verifier restores it before the exact command set is repeated.
        (self.repo / "canary.txt").unlink()
        (self.repo / "app.py").write_text("CANARY = True\n", encoding="utf-8")
        (self.repo / "canary.txt").write_text("fixed by verifier\n", encoding="utf-8")
        verifier = verification.record_verifier(
            self.repo, self.evidence, 0, self.env
        )
        data, comparison = verification.run_final(
            self.repo, self.evidence, self.base, self.env
        )
        self.assertTrue(comparison["accepted"])
        self.assertEqual("baseline", data["baseline"]["phase"])
        self.assertEqual("verifier", verifier["phase"])
        self.assertEqual("verification", data["final"]["phase"])
        self.assertIn("contract", data["baseline"])
        self.assertIn("comparison", data["final"])
        self.assertEqual(
            data["baseline"]["environment_fingerprint"],
            data["final"]["environment_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
