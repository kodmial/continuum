import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
REVIEW = ROOT / ".github" / "workflows" / "continuum-consumer-child-review.yml"


class ChildValidationEnvironmentTest(unittest.TestCase):
    def test_generic_validation_does_not_allocate_child_runner(self):
        validation = (
            ROOT / ".github" / "workflows" / "continuum-validation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("vars.CONTINUUM_ROLE != 'child'", validation)

    def test_automatic_child_pr_lifecycle_does_not_allocate_private_runners(self):
        workflows = {
            "add-review": ROOT / ".github" / "workflows" / "continuum-add-review-label.yml",
            "remove-review": ROOT / ".github" / "workflows" / "continuum-remove-review-label.yml",
            "auto-merge": ROOT / ".github" / "workflows" / "continuum-auto-merge.yml",
            "repair": ROOT / ".github" / "workflows" / "continuum-opencode-repair.yml",
        }
        for name, path in workflows.items():
            with self.subTest(workflow=name):
                body = path.read_text(encoding="utf-8")
                self.assertIn("vars.CONTINUUM_ROLE != 'child'", body)

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
