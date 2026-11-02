import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
GATE = ROOT / ".github/workflows/contract-gate.yml"
QUALIFICATION = ROOT / ".github/workflows/continuum-contract-qualification.yml"
VALIDATE = ROOT / "scripts/validate-continuum.sh"
ENTRYPOINT = "bash scripts/validate-continuum.sh"
SAMPLE_CI = ROOT / "examples/minimal-consumer/.github/workflows/ci.yml"
ACTIONLINT_INSTALLER_SHA256 = (
    "72fa3e45ac20f3c3a512d6747b4fcf719e21f890e8c43e78d48a41fdfb900c4e"
)
ACTIONLINT_INSTALLER_COMMIT = "914e7df21a07ef503a81201c76d2b11c789d3fca"


class ValidationEntrypointContractTest(unittest.TestCase):
    def test_all_project_owned_contract_checks_use_single_validation_entrypoint(self):
        wrappers = {
            "CI": CI.read_text(encoding="utf-8"),
            "Continuum Contract Gate": GATE.read_text(encoding="utf-8"),
            "Contract qualification": QUALIFICATION.read_text(encoding="utf-8"),
        }

        duplicated_commands = (
            "ruby -E UTF-8 scripts/test-continuum.rb",
            "python3 -m unittest discover -s tests",
            "python3 -m unittest discover -s .github/scripts",
            "go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
        )

        for name, body in wrappers.items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    body.count(ENTRYPOINT),
                    1,
                    f"{name} must call the canonical validation entrypoint exactly once",
                )
                for command in duplicated_commands:
                    self.assertNotIn(
                        command,
                        body,
                        f"{name} duplicates validation command {command!r}",
                    )

    def test_entrypoint_is_wired_through_steps_and_calls(self):
        def load(path):
            return yaml.safe_load(path.read_text(encoding="utf-8"))

        for path, job_name in ((CI, "validate"), (GATE, "contract")):
            with self.subTest(workflow=str(path)):
                job = load(path)["jobs"][job_name]
                self.assertEqual(
                    ENTRYPOINT,
                    job["with"]["validation_command"],
                    "the caller must pass the canonical entrypoint to the reusable workflow",
                )
                self.assertTrue(
                    job["uses"].endswith(
                        "/.github/workflows/continuum-validation.yml"
                    )
                )

        qualification = load(QUALIFICATION)
        steps = [
            step
            for job in qualification["jobs"].values()
            for step in job.get("steps", [])
        ]
        executing = [step for step in steps if ENTRYPOINT in (step.get("run") or "")]
        self.assertEqual(1, len(executing), "qualification must run the entrypoint once")
        self.assertIn("set -euo pipefail", executing[0]["run"])
        self.assertNotEqual(True, executing[0].get("continue-on-error"))
        self.assertNotIn("if", executing[0], "the entrypoint step must not be skipped")

        validation = load(ROOT / ".github/workflows/continuum-validation.yml")
        trigger = validation.get(True) or validation.get("on")
        inputs = trigger["workflow_call"]["inputs"]
        self.assertIn("validation_command", inputs)
        hook_steps = [
            step
            for job in validation["jobs"].values()
            for step in job.get("steps", [])
            if 'run_hook validation "$VALIDATION_COMMAND"' in (step.get("run") or "")
        ]
        self.assertEqual(1, len(hook_steps), "the reusable workflow must run the command")
        self.assertIn('eval "$command"', hook_steps[0]["run"])
        self.assertIn("set -euo pipefail", hook_steps[0]["run"])
        self.assertNotEqual(True, hook_steps[0].get("continue-on-error"))

    def test_coverage_gate_measures_uncommitted_engine_changes(self):
        body = VALIDATE.read_text(encoding="utf-8")
        self.assertIn(
            '["git", "diff", "-U0", merge_base, "--", "src"]',
            body,
            "the coverage gate must diff the working tree, not only HEAD",
        )
        self.assertNotIn(
            '["git", "diff", "-U0", merge_base, "HEAD", "--", "src"]',
            body,
            "a HEAD-only diff silently ignores uncommitted engine changes",
        )

    def test_actionlint_installer_is_pinned_and_checksum_verified(self):
        body = VALIDATE.read_text(encoding="utf-8")
        self.assertIn(ACTIONLINT_INSTALLER_COMMIT, body)
        self.assertIn(ACTIONLINT_INSTALLER_SHA256, body)
        self.assertIn("sha256sum -c -", body)
        self.assertNotIn("bash <(curl", body)
        self.assertNotIn(
            "mkdir -p .opencode-tmp/bin", body, "duplicate mkdir must be removed"
        )
        self.assertLess(
            body.index("sha256sum -c -"),
            body.index('bash "$actionlint_installer"'),
            "the checksum must be verified before the installer runs",
        )

    def test_coverage_gate_cannot_silently_skip(self):
        body = VALIDATE.read_text(encoding="utf-8")
        self.assertNotIn("skipping changed-line gate", body)
        self.assertIn("refusing to skip the changed-line gate", body)
        self.assertIn("base '$coverage_base' is unavailable", body)
        self.assertIn(
            'git fetch --no-tags origin \\\n      "+refs/heads/${base_branch}:refs/remotes/origin/${base_branch}"',
            body,
        )
        self.assertIn(
            "coverage: cannot compute merge-base for {base} and HEAD",
            body,
        )
        self.assertIn("coverage.py is required for the changed-line gate", body)

        workflow = (ROOT / ".github/workflows/continuum-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("fetch-depth: 0", workflow)
        qualification = QUALIFICATION.read_text(encoding="utf-8")
        self.assertIn("fetch-depth: 0", qualification)

    def test_minimal_consumer_uses_current_main_and_skips_writes_for_read_only_prs(self):
        body = SAMPLE_CI.read_text(encoding="utf-8")
        self.assertIn(
            "uses: kodmial/continuum/.github/workflows/continuum-validation.yml@main",
            body,
        )
        self.assertIn("continuum_ref: main", body)
        read_only_expression = (
            "${{ github.event_name == 'pull_request' && "
            "(github.event.pull_request.head.repo.fork || github.actor == 'dependabot[bot]') "
            "&& 'false' || 'true' }}"
        )
        self.assertIn(f"publish_status: {read_only_expression}", body)
        self.assertIn(f"dispatch_repair: {read_only_expression}", body)

    def test_contract_gate_skips_status_writes_for_dependabot(self):
        body = GATE.read_text(encoding="utf-8")
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", body)
        self.assertIn("github.actor != 'dependabot[bot]'", body)

    def test_contract_gate_does_not_inherit_unused_secrets(self):
        body = GATE.read_text(encoding="utf-8")
        self.assertNotIn("secrets: inherit", body)
        callee = (ROOT / ".github/workflows/continuum-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("secrets:", callee, "the callee declares no workflow-call secrets")


if __name__ == "__main__":
    unittest.main()
