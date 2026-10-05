import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

CHILD_LOCAL_WORKFLOWS = (
    ".github/workflows/continuum-validation.yml",
    ".github/workflows/continuum-remove-review-label.yml",
    ".github/workflows/continuum-add-review-label.yml",
    ".github/workflows/continuum-auto-merge.yml",
)


class ChildPrivateRunnerGuardsTest(unittest.TestCase):
    def test_child_local_pr_lifecycle_never_allocates_a_runner(self):
        for relative in CHILD_LOCAL_WORKFLOWS:
            with self.subTest(path=relative):
                body = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(
                    "vars.CONTINUUM_ROLE != 'child'",
                    body,
                    f"{relative} must skip in verified child mode before runner allocation",
                )


if __name__ == "__main__":
    unittest.main()
