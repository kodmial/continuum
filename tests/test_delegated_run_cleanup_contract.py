import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLEANUP = ROOT / ".github" / "workflows" / "continuum-consumer-child-run-cleanup.yml"


class DelegatedRunCleanupContractTest(unittest.TestCase):
    def test_cleanup_accepts_numbered_run_names_and_pins_allowed_workflow_paths(self):
        body = CLEANUP.read_text(encoding="utf-8")
        self.assertIn("replace(/ #[1-9][0-9]*$/, '')", body)
        self.assertIn(".github/workflows/continuum-child-worker.yml", body)
        self.assertIn(".github/workflows/continuum-child-review.yml", body)
        self.assertIn(".github/workflows/continuum-child-pr-review.yml", body)
        self.assertIn("allowed.has(canonicalName)", body)
        self.assertIn("allowedPaths.has(run.path)", body)
        self.assertIn("run.status !== 'completed'", body)


if __name__ == "__main__":
    unittest.main()
