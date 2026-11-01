import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
GATE = ROOT / ".github/workflows/contract-gate.yml"
QUALIFICATION = ROOT / ".github/workflows/continuum-contract-qualification.yml"
VALIDATE = ROOT / "scripts/validate-continuum.sh"
ENTRYPOINT = "bash scripts/validate-continuum.sh"
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

    def test_contract_gate_does_not_inherit_unused_secrets(self):
        body = GATE.read_text(encoding="utf-8")
        self.assertNotIn("secrets: inherit", body)
        callee = (ROOT / ".github/workflows/continuum-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("secrets:", callee, "the callee declares no workflow-call secrets")


if __name__ == "__main__":
    unittest.main()
