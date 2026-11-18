"""Exact-HEAD GitHub 403 recovery; never confuse it with model-provider 429."""
import importlib.util
import io
import pathlib
import unittest
import zipfile
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[1] / ".github/scripts/ci_api_limit_recovery.py"
spec = importlib.util.spec_from_file_location("ci_api_limit_recovery", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run():
    return {"event": "pull_request", "status": "completed", "conclusion": "failure"}


def jobs(failed="Publish validation result and continue repair lifecycle"):
    return [{"id": 123, "conclusion": "failure", "steps": [
        {"name": "Run validation contract", "conclusion": "success"},
        {"name": failed, "conclusion": "failure"},
    ]}]


class GitHub403RecoveryTests(unittest.TestCase):
    def test_publication_403_is_infrastructure(self):
        self.assertEqual(m.classify_failure(
            run(), jobs(), lambda _: "2026Z ##[error]HttpError: API rate limit exceeded for installation."
        ), "github-403")

    def test_real_validation_failure_is_not_masked(self):
        self.assertEqual(m.classify_failure(
            run(), jobs("Run validation contract"), lambda _: "##[error]API rate limit exceeded"
        ), "code")

    def test_timeout_remains_code_repairable_without_github_403_evidence(self):
        timed_out = dict(run(), conclusion="timed_out")
        self.assertEqual(m.classify_failure(timed_out, [], lambda _: ""), "code")

    def test_explicit_workflow_dispatch_report_remains_repairable(self):
        dispatched = dict(run(), event="workflow_dispatch")
        self.assertEqual(m.classify_failure(
            dispatched, jobs("Run validation contract"), lambda _: ""
        ), "code")

    def test_plain_permission_403_is_not_rate_exhaustion(self):
        self.assertEqual(m.classify_failure(
            run(), jobs(), lambda _: "##[error]Resource not accessible by integration (403)"
        ), "unknown")

    def test_opencode_provider_429_is_never_github_403(self):
        self.assertEqual(m.classify_failure(
            run(), jobs(), lambda _: "##[error]OpenCode provider 429"
        ), "unknown")

    def test_echoed_shell_text_is_not_error(self):
        self.assertEqual(m.classify_failure(
            run(), jobs(), lambda _: 'echo "API rate limit exceeded"'
        ), "unknown")

    def test_unavailable_logs_hold(self):
        def unavailable(_):
            raise m.Unavailable("limit exhausted")
        self.assertEqual(m.classify_failure(run(), jobs(), unavailable), "unknown")

    def test_job_logs_zip_supported(self):
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("job.txt", "##[error]API rate limit exceeded for installation")
        self.assertTrue(m.has_github_primary_limit_error(m.decoded_log(contents.getvalue())))

    def test_retry_budget_and_exact_head(self):
        candidate = dict(name="CI", event="pull_request", head_sha="a" * 40,
                         status="completed", conclusion="failure", run_attempt=1,
                         updated_at="2026-11-17T22:00:00Z")
        self.assertTrue(m.eligible_run(candidate, "a" * 40, 1794953600))
        self.assertFalse(m.eligible_run(dict(candidate, run_attempt=10), "a" * 40, 1794953600))
        self.assertFalse(m.eligible_run(candidate, "b" * 40, 1794953600))
        self.assertFalse(m.eligible_run(dict(candidate, conclusion="success"), "a" * 40, 1794953600))

    def test_no_requests_when_rate_budget_low(self):
        with patch.object(m, "rate_budget_ok", return_value=False), patch.object(m, "api") as api:
            self.assertEqual(m.reconcile("kodmial/continuum"), 0)
            api.assert_not_called()

    def test_only_one_native_rerun_on_verified_exact_head(self):
        sha = "a" * 40
        pr = {"number": 12, "head": {"sha": sha, "repo": {"full_name": "kodmial/continuum"}}}
        workflow = dict(id=111, name="CI", event="pull_request", head_sha=sha,
                        status="completed", conclusion="failure", run_attempt=1,
                        updated_at="2026-11-17T22:00:00Z")
        requests = []
        def fake_api(path, *, post=False, raw=False):
            requests.append((path, post))
            if "/pulls?state=open" in path:
                return [pr]
            if "/actions/runs?head_sha=" in path:
                return {"workflow_runs": [workflow], "total_count": 1}
            if path.endswith("/pulls/12"):
                return {"state": "open", "head": {"sha": sha}}
            if path.endswith("/actions/runs/111"):
                return workflow
            if path.endswith("/rerun-failed-jobs"):
                return None
            raise AssertionError(path)
        with patch.object(m, "rate_budget_ok", return_value=True), \
             patch.object(m, "api", side_effect=fake_api), \
             patch.object(m, "classify_run", return_value="github-403"), \
             patch.object(m.time, "time", return_value=1794953600):
            self.assertEqual(m.reconcile("kodmial/continuum"), 0)
        self.assertEqual(
            [p for p, is_post in requests if is_post],
            ["repos/kodmial/continuum/actions/runs/111/rerun-failed-jobs"]
        )


if __name__ == "__main__":
    unittest.main()
