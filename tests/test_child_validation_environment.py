import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
REVIEW = ROOT / ".github" / "workflows" / "continuum-consumer-child-review.yml"


class ChildValidationEnvironmentTest(unittest.TestCase):
    def test_delegated_validation_receives_child_role_without_credentials(self):
        body = REVIEW.read_text(encoding="utf-8")
        marker = 'CONTINUUM_ROLE=child'
        self.assertIn(marker, body)
        command = next(
            line for line in body.splitlines()
            if 'bash "$trusted_validation"' in line and 'env -i' in line
        )
        self.assertIn(marker, command)
        self.assertIn('CI=true', command)
        self.assertNotIn('GH_TOKEN=', command)
        self.assertNotIn('GITHUB_TOKEN=', command)


if __name__ == "__main__":
    unittest.main()
