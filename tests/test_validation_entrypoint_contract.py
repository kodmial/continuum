import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
GATE = ROOT / ".github/workflows/contract-gate.yml"
ENTRYPOINT = "bash scripts/validate-continuum.sh"


class ValidationEntrypointContractTest(unittest.TestCase):
    def test_ci_and_contract_gate_use_single_validation_entrypoint(self):
        ci = CI.read_text(encoding="utf-8")
        gate = GATE.read_text(encoding="utf-8")

        self.assertIn(ENTRYPOINT, ci)
        self.assertIn(ENTRYPOINT, gate)

        duplicated_commands = (
            "ruby -E UTF-8 scripts/test-continuum.rb",
            "python3 -m unittest discover -s tests",
            "go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
        )
        for command in duplicated_commands:
            self.assertNotIn(command, ci)
            self.assertNotIn(command, gate)


if __name__ == "__main__":
    unittest.main()
