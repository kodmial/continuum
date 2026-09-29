"""Publication is bound to one exact commit and one verification run.

The incident these pin: a release's artifacts were built and published inside
the same job, so what reached the release was exactly what nothing else had
ever run. Re-running the build at publication time did not fix that, it made it
worse -- the shipped bytes were a *second* build, and the verification that had
happened applied to a first build that no longer existed anywhere. The observable
symptom was a package that installed everywhere except on the machine that
verified it.

The fix is narrow and deliberately generic: a declared verification workflow, an
exact-commit match, and a requirement that the run is green *and* produced the
declared artifacts. Nothing here names a product, a package manager, or a
platform, because a consumer's verification is the consumer's business. What is
not the consumer's business is publishing untested bytes, and that is what this
module refuses.

Deterministic throughout: no network, no clock, no runner.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

from continuum import cli
from continuum import config as config_module
from continuum.release import provenance
from tests import support

HEAD = support.HEAD_A
OTHER = support.HEAD_B
WORKFLOW = "verification.yml"
JOB = "build the candidate"
ARTIFACT = "candidate-distribution"

REQUIREMENT = provenance.VerificationRequirement(
    workflow=WORKFLOW,
    event="push",
    jobs=(JOB,),
    artifacts=(ARTIFACT,),
)


def green_run(run_id: int = 501, *, head: str = HEAD, **overrides):
    return support.workflow_run(run_id, workflow=WORKFLOW, head_sha=head, **overrides)


def jobs(*names):
    return [{"name": name, "conclusion": "success"} for name in names]


def artifacts(*names):
    return [{"name": name, "size_in_bytes": 12} for name in names]


def verdict(runs, *, head: str = HEAD, requirement=REQUIREMENT, job_names=None, artifact_names=None):
    """Evaluate with fully populated checks unless the caller opts out."""

    if requirement is None:
        return provenance.evaluate(None, head, runs=runs)
    return provenance.evaluate(
        requirement,
        head,
        runs=runs,
        jobs=jobs(*(job_names if job_names is not None else requirement.jobs)),
        artifacts=artifacts(
            *(artifact_names if artifact_names is not None else requirement.artifacts)
        ),
    )


class RequirementTests(unittest.TestCase):
    def test_a_bare_workflow_file_name_is_accepted(self):
        self.assertTrue(provenance.VerificationRequirement(workflow="build.yml").declared)

    def test_a_workflow_path_is_refused(self):
        # The gate names a workflow *in this repository*; accepting a path would
        # mean the requirement could be satisfied by a file the caller does not
        # control.
        with self.assertRaises(ValueError):
            provenance.VerificationRequirement(workflow=".github/workflows/build.yml")

    def test_a_non_yaml_workflow_name_is_refused(self):
        with self.assertRaises(ValueError):
            provenance.VerificationRequirement(workflow="build")

    def test_a_nonsense_event_name_is_refused(self):
        with self.assertRaises(ValueError):
            provenance.VerificationRequirement(workflow="build.yml", event="Push!")

    def test_an_empty_declared_name_is_refused(self):
        with self.assertRaises(ValueError):
            provenance.VerificationRequirement(workflow="build.yml", artifacts=(" ",))

    def test_undeclared_verification_is_none_not_an_empty_requirement(self):
        # Two spellings of "no gate" would be two branches to get wrong. There is
        # one: `None`. A requirement that exists but names no workflow is a
        # programming error, and is refused as one.
        self.assertIsNone(
            config_module.parse_config("version: 1\n").release.verification.requirement()
        )
        with self.assertRaises(ValueError):
            provenance.VerificationRequirement(workflow="")


class ExactHeadTests(unittest.TestCase):
    """Only a run of this commit is evidence for this commit."""

    def test_a_run_on_another_commit_is_not_selected(self):
        self.assertIsNone(
            provenance.select_run([green_run(head=OTHER)], REQUIREMENT, HEAD)
        )

    def test_a_run_of_another_workflow_is_not_selected(self):
        run = support.workflow_run(501, workflow="something-else.yml", head_sha=HEAD)
        self.assertIsNone(provenance.select_run([run], REQUIREMENT, HEAD))

    def test_a_run_from_another_event_is_not_selected(self):
        run = green_run(event="pull_request")
        self.assertIsNone(provenance.select_run([run], REQUIREMENT, HEAD))

    def test_an_unfiltered_requirement_accepts_any_event(self):
        requirement = provenance.VerificationRequirement(workflow=WORKFLOW)
        self.assertIsNotNone(
            provenance.select_run([green_run(event="pull_request")], requirement, HEAD)
        )

    def test_the_newest_run_for_the_commit_wins(self):
        older = green_run(500, started_at="2026-09-20T09:00:00Z")
        newer = green_run(501, started_at="2026-09-20T10:00:00Z")
        selected = provenance.select_run([older, newer], REQUIREMENT, HEAD)
        self.assertEqual(selected["id"], 501)

    def test_a_branch_is_never_a_usable_head(self):
        # `head=""` is what "the current branch" looks like after the filter is
        # dropped. Selecting on it would make every run on main evidence for
        # whatever commit happens to be checked out.
        self.assertIsNone(provenance.select_run([green_run()], REQUIREMENT, ""))


class VerdictTests(unittest.TestCase):
    def test_a_green_run_with_everything_declared_verifies(self):
        result = verdict([green_run()])
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, provenance.VERIFIED)
        self.assertEqual(result.run_id, 501)
        self.assertTrue(result.tested_bytes)

    def test_no_run_for_the_commit_blocks(self):
        result = verdict([])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.RUN_MISSING)
        self.assertFalse(result.tested_bytes)

    def test_an_unfinished_run_blocks_and_asks_for_a_retry(self):
        result = verdict([green_run(status="in_progress", conclusion="")])
        self.assertEqual(result.reason, provenance.RUN_INCOMPLETE)
        self.assertTrue(result.retryable)

    def test_a_missing_run_is_retryable_but_a_failed_one_is_not(self):
        # A run that will appear shortly is worth waiting for. A run that
        # finished red, or that produced nothing, will answer the same way every
        # time it is looked at, and retrying it only burns a runner.
        self.assertTrue(provenance.is_retryable(provenance.RUN_MISSING))
        self.assertFalse(provenance.is_retryable(provenance.RUN_FAILED))
        self.assertFalse(provenance.is_retryable(provenance.ARTIFACT_MISSING))
        self.assertFalse(provenance.is_retryable(provenance.VERIFIED))

    def test_a_failed_run_blocks(self):
        result = verdict([green_run(conclusion="failure")])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.RUN_FAILED)
        self.assertFalse(result.retryable)

    def test_a_cancelled_run_blocks(self):
        result = verdict([green_run(conclusion="cancelled")])
        self.assertEqual(result.reason, provenance.RUN_FAILED)

    def test_a_green_run_that_never_ran_the_declared_job_blocks(self):
        # The path-filter trap: the workflow is green, but the job that was
        # supposed to prove this commit was not part of the run.
        result = verdict([green_run()], job_names=())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.JOB_MISSING)
        self.assertEqual(result.missing_jobs, (JOB,))

    def test_a_green_run_missing_the_declared_artifact_blocks(self):
        # The check that makes the gate worth having: a matrix leg or an upload
        # that quietly produced nothing is a green run that verified nothing.
        result = verdict([green_run()], artifact_names=())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.ARTIFACT_MISSING)
        self.assertEqual(result.missing_artifacts, (ARTIFACT,))
        self.assertFalse(result.tested_bytes)

    def test_a_partially_present_artifact_set_names_what_is_missing(self):
        other = provenance.VerificationRequirement(
            workflow=WORKFLOW,
            artifacts=(ARTIFACT, "supplementary"),
        )
        result = verdict([green_run()], requirement=other, artifact_names=(ARTIFACT,))
        self.assertEqual(result.reason, provenance.ARTIFACT_MISSING)
        self.assertEqual(result.missing_artifacts, ("supplementary",))
        self.assertEqual(result.artifacts, (ARTIFACT,))

    def test_no_declared_verification_is_reported_as_unconstrained(self):
        # Not as a pass. A caller that prints "verified" here would be claiming
        # something nobody checked.
        result = verdict([], requirement=None)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, provenance.NOT_CONFIGURED)
        self.assertFalse(result.tested_bytes)
        self.assertIn("not bound", result.detail)

    def test_no_head_cannot_be_verified(self):
        result = verdict([green_run()], head="")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.HEAD_REQUIRED)

    def test_skipping_the_job_check_does_not_demand_the_job_never_ran(self):
        # "Not required" and "required and absent" are opposite answers, and a
        # caller that turned a check off must not be handed the second one.
        result = provenance.evaluate(
            REQUIREMENT,
            HEAD,
            runs=[green_run()],
            jobs=[],
            artifacts=artifacts(ARTIFACT),
            check_jobs=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, provenance.VERIFIED)

    def test_skipping_the_artifact_check_does_not_demand_the_artifact_be_absent(self):
        result = provenance.evaluate(
            REQUIREMENT,
            HEAD,
            runs=[green_run()],
            jobs=jobs(JOB),
            artifacts=[],
            check_artifacts=False,
        )
        self.assertTrue(result.ok)

    def test_skipping_one_check_leaves_the_other_in_force(self):
        result = provenance.evaluate(
            REQUIREMENT,
            HEAD,
            runs=[green_run()],
            jobs=jobs(JOB),
            artifacts=[],
            check_jobs=False,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, provenance.ARTIFACT_MISSING)

    def test_a_finished_red_run_is_still_blocked_when_a_check_is_skipped(self):
        # Skipping a dimension weakens what is proved; it does not excuse a run
        # that failed.
        result = provenance.evaluate(
            REQUIREMENT,
            HEAD,
            runs=[green_run(conclusion="failure")],
            check_jobs=False,
            check_artifacts=False,
        )
        self.assertEqual(result.reason, provenance.RUN_FAILED)


class VerdictContractTests(unittest.TestCase):
    """A consumer's branch protection must be able to read the verdict."""

    def test_the_contract_names_the_verdict_the_reason_and_the_run(self):
        result = verdict([green_run()])
        self.assertEqual(
            result.contract(), f"verdict=PASS reason=verified head={HEAD} run=501"
        )

    def test_a_blocked_verdict_says_block_and_carries_no_run(self):
        contract = verdict([]).contract()
        self.assertIn("verdict=BLOCK", contract)
        self.assertIn("reason=verification-missing", contract)
        self.assertIn("run=none", contract)

    def test_the_document_round_trips_and_declares_its_schema(self):
        payload = verdict([green_run()]).describe()
        self.assertEqual(payload["schema"], provenance.VERDICT_SCHEMA)
        self.assertEqual(json.loads(json.dumps(payload))["verdict"], "PASS")
        self.assertTrue(payload["tested_bytes"])

    def test_the_document_records_what_the_requirement_asked_for(self):
        payload = verdict([green_run()]).describe()
        self.assertEqual(payload["requirement"]["workflow"], WORKFLOW)
        self.assertEqual(payload["requirement"]["artifacts"], [ARTIFACT])


class ConfigurationTests(unittest.TestCase):
    def test_a_verification_block_becomes_a_requirement(self):
        parsed = config_module.parse_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    event: push\n"
            "    jobs:\n"
            "      - build the candidate\n"
            "    artifacts:\n"
            "      - candidate-distribution\n"
        )
        requirement = parsed.release.verification.requirement()
        self.assertEqual(requirement.workflow, WORKFLOW)
        self.assertEqual(requirement.jobs, ("build the candidate",))
        self.assertEqual(requirement.artifacts, (ARTIFACT,))

    def test_no_verification_block_means_no_requirement(self):
        parsed = config_module.parse_config("version: 1\n")
        self.assertIsNone(parsed.release.verification.requirement())

    def test_a_verification_block_alone_does_not_enable_a_release(self):
        # Configuring a gate is not configuring a publication. Coupling the two
        # would make `release: verification: ...` silently enable releases on a
        # repository that meant only to add a check.
        parsed = config_module.parse_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.assertFalse(parsed.release.enabled)

    def test_a_workflow_path_is_refused_as_a_configuration_error(self):
        # The core raises ValueError; the configuration layer must translate it,
        # or the CLI has no handler and a typo becomes a traceback.
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_config(
                "version: 1\n"
                "release:\n"
                "  verification:\n"
                "    workflow: .github/workflows/build.yml\n"
            )
        self.assertIn("release.verification.workflow", str(caught.exception))

    def test_a_nonsense_event_name_is_refused_as_a_configuration_error(self):
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_config(
                "version: 1\n"
                "release:\n"
                "  verification:\n"
                "    workflow: build.yml\n"
                "    event: Push!\n"
            )
        self.assertIn("event", str(caught.exception))

    def test_a_gate_with_no_workflow_is_refused_rather_than_ignored(self):
        # A typo in `workflow` would otherwise leave publication unconstrained
        # while the configuration still reads as though a gate was asked for.
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_config(
                "version: 1\n"
                "release:\n"
                "  verification:\n"
                "    artifacts:\n"
                "      - something\n"
            )
        self.assertIn("workflow", str(caught.exception))

    def test_an_unknown_verification_key_is_refused_by_name(self):
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_config(
                "version: 1\n"
                "release:\n"
                "  verification:\n"
                "    workflow: verification.yml\n"
                "    required: true\n"
            )
        self.assertIn("required", str(caught.exception))

    def test_a_duplicate_declared_name_is_refused(self):
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_config(
                "version: 1\n"
                "release:\n"
                "  verification:\n"
                "    workflow: verification.yml\n"
                "    jobs:\n"
                "      - build\n"
                "      - build\n"
            )
        self.assertIn("duplicates", str(caught.exception))

    def test_the_configured_gate_is_reported_by_config_check(self):
        parsed = config_module.parse_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    artifacts:\n"
            "      - candidate-distribution\n"
        )
        described = parsed.release.describe()["verification"]
        self.assertTrue(described["declared"])
        self.assertEqual(described["artifacts"], [ARTIFACT])


class _CliHarness(unittest.TestCase):
    """Drive the command the way a workflow would, with a scripted client."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="continuum-verify-")
        self.addCleanup(self._cleanup)
        self.config_path = pathlib.Path(self.directory) / "config.yml"
        self.outputs = pathlib.Path(self.directory) / "github_output"
        self.outputs.write_text("", encoding="utf-8")
        self.client = support.FakeGitHub(head=HEAD)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def write_config(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    def run_cli(self, *args: str):
        buffer = io.StringIO()
        previous = {
            key: os.environ.get(key)
            for key in ("GITHUB_OUTPUT", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_TOKEN")
        }
        os.environ["GITHUB_OUTPUT"] = str(self.outputs)
        os.environ["GITHUB_REPOSITORY"] = self.client.repository
        os.environ["GITHUB_TOKEN"] = "token-for-tests"
        self._patched = cli._client
        cli._client = lambda: self.client
        try:
            with redirect_stdout(buffer):
                code = cli.main(list(args) + ["--config", str(self.config_path)])
        finally:
            cli._client = self._patched
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, buffer.getvalue()

    def github_output(self) -> dict:
        values = {}
        for line in self.outputs.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
        return values


class CliGateTests(_CliHarness):
    def test_a_satisfied_gate_exits_zero_and_publishes_the_verdict(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    event: push\n"
            "    jobs:\n"
            "      - build the candidate\n"
            "    artifacts:\n"
            "      - candidate-distribution\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run()]]
        self.client.run_jobs[501] = jobs(JOB)
        self.client.run_artifacts[501] = artifacts(ARTIFACT)
        code, out = self.run_cli("release", "verify", "--head", HEAD)
        self.assertEqual(code, cli.EXIT_OK)
        published = self.github_output()
        self.assertEqual(published["verdict"], "PASS")
        self.assertEqual(published["run"], "501")
        self.assertEqual(published["reason"], provenance.VERIFIED)
        self.assertEqual(json.loads(out)["run_id"], 501)

    def test_a_blocked_gate_exits_nonzero_and_names_the_reason(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[]]
        code, out = self.run_cli("release", "verify", "--head", HEAD)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(self.github_output()["verdict"], "BLOCK")
        self.assertIn(provenance.RUN_MISSING, out)

    def test_the_run_is_read_for_jobs_and_artifacts_only_after_selection(self):
        # A job list from some other run would make the verdict a statement
        # about the wrong run, so nothing is read when there is no run to read
        # it from.
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    jobs:\n"
            "      - build the candidate\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run(head=OTHER)]]
        code, _out = self.run_cli("release", "verify", "--head", HEAD)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertNotIn("list_run_jobs", [call[0] for call in self.client.calls])

    def test_a_missing_artifact_fails_the_gate(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    artifacts:\n"
            "      - candidate-distribution\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run()]]
        self.client.run_artifacts[501] = artifacts("something-else")
        code, out = self.run_cli("release", "verify", "--head", HEAD)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn(provenance.ARTIFACT_MISSING, out)

    def test_an_unconfigured_repository_reports_unconstrained_and_succeeds(self):
        self.write_config("version: 1\n")
        code, out = self.run_cli("release", "verify", "--head", HEAD)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["reason"], provenance.NOT_CONFIGURED)
        self.assertEqual(self.client.calls, [])

    def test_the_head_defaults_to_the_running_commit(self):
        # A release job runs on a commit; making the caller repeat it is a chance
        # to verify the wrong thing.
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run()]]
        os.environ["GITHUB_SHA"] = HEAD
        try:
            code, out = self.run_cli("release", "verify")
        finally:
            os.environ.pop("GITHUB_SHA", None)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["head_sha"], HEAD)

    def test_the_waiting_job_does_not_wait_on_a_run_that_already_answered(self):
        # A red run will not turn green by being looked at again, so the bounded
        # wait must not spend itself on one.
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run(conclusion="failure")]]
        code, out = self.run_cli(
            "release", "verify", "--head", HEAD, "--wait-attempts", "3", "--wait-seconds", "0"
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(
            len([call for call in self.client.calls if call[0] == "list_workflow_runs"]), 1
        )
        self.assertIn(provenance.RUN_FAILED, out)

    def test_the_waiting_job_rereads_an_unfinished_run_until_it_answers(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [
            [green_run(status="in_progress", conclusion="")],
            [green_run(status="completed", conclusion="failure")],
        ]
        code, out = self.run_cli(
            "release", "verify", "--head", HEAD, "--wait-attempts", "3", "--wait-seconds", "0"
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(
            len([call for call in self.client.calls if call[0] == "list_workflow_runs"]), 2
        )
        self.assertIn(provenance.RUN_FAILED, out)

    def test_the_gate_never_waits_when_told_not_to(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run(status="queued", conclusion="")]]
        code, out = self.run_cli("release", "verify", "--head", HEAD, "--no-wait")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn(provenance.RUN_INCOMPLETE, out)
        self.assertEqual(
            len([call for call in self.client.calls if call[0] == "list_workflow_runs"]), 1
        )

    def test_the_gate_waits_for_a_run_that_does_not_exist_yet(self):
        # The gate and the workflow it waits for are triggered by the same push,
        # so "no run" is the expected *first* answer, not a verdict. Reading once
        # and giving up would block every release on a race.
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[], [green_run()]]
        code, out = self.run_cli(
            "release", "verify", "--head", HEAD, "--wait-attempts", "2", "--wait-seconds", "0"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(
            len([call for call in self.client.calls if call[0] == "list_workflow_runs"]), 2
        )
        self.assertIn(provenance.VERIFIED, out)

    def test_the_wait_is_bounded_and_reports_the_missing_run(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[]]
        code, out = self.run_cli(
            "release", "verify", "--head", HEAD, "--wait-attempts", "3", "--wait-seconds", "0"
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn(provenance.RUN_MISSING, out)
        self.assertEqual(
            len([call for call in self.client.calls if call[0] == "list_workflow_runs"]), 4
        )

    def test_a_skipped_check_does_not_block_the_gate(self):
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
            "    jobs:\n"
            "      - build the candidate\n"
            "    artifacts:\n"
            "      - candidate-distribution\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run()]]
        code, out = self.run_cli(
            "release",
            "verify",
            "--head",
            HEAD,
            "--skip-job-check",
            "--skip-artifact-check",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["reason"], provenance.VERIFIED)
        self.assertNotIn("list_run_artifacts", [call[0] for call in self.client.calls])

    def test_the_verdict_document_is_written_where_asked(self):
        destination = pathlib.Path(self.directory) / "verdict.json"
        self.write_config(
            "version: 1\n"
            "release:\n"
            "  verification:\n"
            "    workflow: verification.yml\n"
        )
        self.client.workflow_runs[WORKFLOW] = [[green_run()]]
        code, _out = self.run_cli(
            "release", "verify", "--head", HEAD, "--out", str(destination)
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["verdict"], "PASS"
        )


class IsolationTests(unittest.TestCase):
    """The gate is generic: it must not know any one repository's names."""

    def test_the_generic_source_names_no_consumer_or_platform(self):
        source = pathlib.Path(provenance.__file__).read_text(encoding="utf-8")
        lowered = source.lower()
        for name in (
            "homebrew",
            "macports",
            "nanodictate",
            "continuum-mvp",
            "swift",
            "cask",
            "formula",
            "portfile",
            "brew",
        ):
            self.assertNotIn(name, lowered, f"provenance.py mentions {name}")


if __name__ == "__main__":
    unittest.main()
