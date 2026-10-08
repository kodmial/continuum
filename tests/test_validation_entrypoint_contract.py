import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
GATE = ROOT / ".github/workflows/contract-gate.yml"
QUALIFICATION = ROOT / ".github/workflows/continuum-contract-qualification.yml"
ENTRYPOINT = "bash scripts/validate-continuum.sh"


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


if __name__ == "__main__":
    unittest.main()
