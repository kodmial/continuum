"""The `release` commands, from argv to exit code.

The adapter tests describe a plan and the runner tests execute one, but a
release is only ever invoked as `continuum release sign ...`. These tests drive
that entrypoint, because the join between the two is where a secret stops being
available: configuration names a secret, the adapter reads another name, and a
plan built from one environment and run against another cannot find it.

That failure looks like an unconfigured secret, and an unconfigured secret
looks like a fork pull request. It is worth a test of its own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from typing import List, Optional

from continuum import cli
from continuum.release import adapters

from . import release_support as support


class CommandRun:
    """The result of running one `continuum release ...` invocation."""

    def __init__(self, code: int, stdout: str, stderr: str = "") -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    def json(self):
        """The last JSON document printed, which is the machine-facing one."""

        decoder = json.JSONDecoder()
        found = None
        index = 0
        while index < len(self.stdout):
            brace = self.stdout.find("{", index)
            if brace < 0:
                break
            try:
                payload, end = decoder.raw_decode(self.stdout, brace)
            except ValueError:
                index = brace + 1
                continue
            found = payload
            index = end
        return found


class ReleaseCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="continuum-release-cli-")
        self.addCleanup(self._cleanup)
        self.config_path = os.path.join(self.workdir, ".continuum.yml")
        self._sandboxed_environment()

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    def _sandboxed_environment(self) -> None:
        """Give the command a clean, known environment.

        The real process environment is not a fixture: it has whatever the
        developer's shell happened to export, and a stray `NANODICTATE_*` would
        turn "refuses to sign ad-hoc" into "silently signs".
        """

        keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
        saved = {key: value for key, value in os.environ.items() if key not in keep}
        for key in saved:
            del os.environ[key]
        for key, value in (
            (key, os.environ.get(key, "")) for key in keep if key in os.environ
        ):
            saved.setdefault(key, value)
        self.addCleanup(self._restore, saved)

    def _restore(self, saved) -> None:
        for key in list(os.environ):
            if key not in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"):
                del os.environ[key]
        os.environ.update(saved)

    def write_config(self, document: str) -> str:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(document)
        return self.config_path

    def run_cli(self, argv: List[str], env: Optional[dict] = None) -> CommandRun:
        """Run one command with `env` layered over a scrubbed environment."""

        previous = dict(os.environ)
        for key in (env or {}):
            os.environ.pop(key, None)
        os.environ.update(env or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            stderr.write(str(exc.code) if not isinstance(exc.code, int) else "")
        finally:
            os.environ.clear()
            os.environ.update(previous)
        return CommandRun(code, stdout.getvalue(), stderr.getvalue())

    def plan_argv(self, *extra: str) -> List[str]:
        return ["release", "plan", "--config", self.config_path, *extra]

    def sign_argv(self, *extra: str) -> List[str]:
        return ["release", "sign", "--config", self.config_path, *extra]


class ReleasePlanCommandTests(ReleaseCliTests):
    def test_plan_prints_the_plan_and_exits_cleanly(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.plan_argv("--target", "macos"),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0, result.stdout)
        payload = result.json()
        self.assertIsNotNone(payload, result.stdout)
        self.assertEqual(payload["target"], "macos")
        self.assertEqual(payload["signing"]["mode"], "self-signed-stable")

    def test_plan_never_prints_a_secret_value(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.plan_argv("--target", "macos"),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0)
        self.assertNotIn(support.FAKE_P12, result.stdout)
        self.assertNotIn(support.FAKE_PASSWORD, result.stdout)
        self.assertIn(support.P12_SECRET, result.stdout)

    def test_plan_writes_a_file_when_asked(self):
        self.write_config(support.nanodictate_document())
        out = os.path.join(self.workdir, "plan.json")
        result = self.run_cli(
            self.plan_argv("--target", "macos", "--out", out),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0)
        with open(out, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["target"], "macos")

    def test_plan_names_the_pinned_identity_to_a_human(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.plan_argv("--target", "macos"),
            env=support.environment(),
        )
        self.assertIn(support.IDENTITY, result.stdout)
        self.assertIn("Pinned signing identity", result.stdout)

    def test_plan_records_its_own_outputs_for_a_workflow(self):
        self.write_config(support.nanodictate_document())
        output = os.path.join(self.workdir, "github_output")
        result = self.run_cli(
            self.plan_argv("--target", "macos"),
            env={**support.environment(), "GITHUB_OUTPUT": output},
        )
        self.assertEqual(result.code, 0)
        with open(output, "r", encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("signature_pinned=true", written)
        self.assertIn("degraded=false", written)

    def test_an_unknown_target_lists_the_ones_that_exist(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(self.plan_argv("--target", "nope"), env=support.environment())
        self.assertEqual(result.code, 1)
        self.assertIn("no release target 'nope'", result.stdout + result.stderr)
        self.assertIn("macos", result.stdout + result.stderr)

    def test_a_degraded_plan_is_loud_on_stdout(self):
        self.write_config(support.fallback_document())
        result = self.run_cli(
            self.plan_argv("--target", "macos"),
            env=support.environment(p12=None),
        )
        self.assertEqual(result.code, 0, result.stdout)
        self.assertIn("DEGRADED RELEASE", result.stdout)
        self.assertIn("No signing identity is pinned", result.stdout)

    def test_a_typed_but_unbuilt_target_is_refused_clearly(self):
        self.write_config(support.ios_document())
        result = self.run_cli(
            self.plan_argv("--target", "ios"),
            env=support.environment(),
        )
        self.assertEqual(result.code, 1)
        self.assertIn("CONTINUUM_ERROR", result.stdout + result.stderr)


class SigningMaterialExpectationTests(ReleaseCliTests):
    def test_present_refuses_to_silently_degrade(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos", "--signing-material", "present"),
            env=support.environment(p12=None),
        )
        self.assertEqual(result.code, 1)
        combined = result.stdout + result.stderr
        self.assertIn("Refusing to sign ad-hoc", combined)
        self.assertIn(support.P12_SECRET, combined)

    def test_present_is_satisfied_when_the_secret_is_there(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv(
                "--target", "macos", "--signing-material", "present", "--dry-run"
            ),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0, result.stdout)

    def test_an_empty_secret_counts_as_absent(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos", "--signing-material", "present"),
            env=support.environment(p12=""),
        )
        self.assertEqual(result.code, 1)
        self.assertIn("Refusing to sign ad-hoc", result.stdout + result.stderr)

    def test_stable_without_a_secret_fails_rather_than_producing_an_unsigned_build(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos"),
            env=support.environment(p12=None),
        )
        self.assertEqual(result.code, 1)
        self.assertIn("CONTINUUM_ERROR", result.stdout + result.stderr)


class ReleaseSignCommandTests(ReleaseCliTests):
    def test_a_dry_run_reports_success_without_touching_a_tool(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos", "--dry-run"),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0, result.stdout)
        self.assertIn("Dry run", result.stdout)
        self.assertEqual(result.json()["dry_run"], True)

    def test_a_dry_run_does_not_need_the_toolchain(self):
        # Planning happens on any machine, including the Linux job that reviews
        # a release; only running needs a Mac.
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos", "--dry-run"),
            env=support.environment(),
        )
        self.assertEqual(result.code, 0)

    def test_a_runner_without_the_toolchain_is_refused_before_the_first_step(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos"),
            env=support.environment(),
        )
        if not adapters.get("apple").available():
            self.assertEqual(result.code, 1)
            combined = result.stdout + result.stderr
            self.assertIn("toolchain", combined)

    def test_the_pinned_secret_survives_from_planning_to_running(self):
        """The bug this whole file exists for.

        Configuration calls the certificate `NANODICTATE_SIGNING_P12`; the
        adapter's steps read `CONTINUUM_APPLE_P12`. If the plan is executed
        against the environment as the job received it, the step reads a name
        that was never exported and reports a missing secret — which is exactly
        what a fork pull request looks like, and would send someone hunting for
        a CI permissions problem that does not exist.
        """

        self.write_config(support.nanodictate_document())
        target = support.load_target(self.config_path)
        environment = support.environment()
        bound = adapters.bind_material(target, environment)
        plan = adapters.plan_for(target, environment=environment)
        for step in plan.step_list:
            for name in step.uses_secrets:
                self.assertIn(name, bound, f"step {step.name!r} reads an unbound name")
            if step.kind == "materialize":
                self.assertIn(
                    step.source_env,
                    bound,
                    f"step {step.name!r} materializes from an unbound name",
                )
        # The raw environment is what the job had; the bound one is what runs.
        self.assertNotIn(apple_p12_name(), environment)

    def test_a_sign_run_reports_the_failure_without_a_secret_in_it(self):
        self.write_config(support.nanodictate_document())
        result = self.run_cli(
            self.sign_argv("--target", "macos"),
            env=support.environment(),
        )
        if result.code != 0:
            self.assertNotIn(support.FAKE_P12, result.stdout + result.stderr)
            self.assertNotIn(support.FAKE_PASSWORD, result.stdout + result.stderr)


def apple_p12_name() -> str:
    from continuum.release import apple

    return apple.P12_ENV


class ReleaseVerifyCommandTests(ReleaseCliTests):
    """`release verify` as a workflow calls it.

    The interesting part is not that it passes -- that is covered in
    `test_release_verification`. It is that every way of getting the evidence
    wrong ends in a non-zero exit and a named reason, because a workflow that
    treats "no candidate" as success is worse than no workflow.
    """

    HEAD = "a" * 40
    OTHER = "b" * 40

    def setUp(self):
        super().setUp()
        self.artifact = os.path.join(self.workdir, "dist.tar.gz")
        with open(self.artifact, "wb") as handle:
            handle.write(b"the tested bytes")
        self.candidate_path = os.path.join(self.workdir, "candidate.json")
        self._candidate(self.HEAD)

    def _candidate(self, source_sha: str, artifacts: Optional[dict] = None) -> None:
        from continuum.release import provenance

        record = provenance.build_candidate(
            source_sha=source_sha,
            artifacts=artifacts or {"dist.tar.gz": self.artifact},
            gate=".github/workflows/validate.yml",
            gate_run_id="runs/1",
            artifact_source="run artifacts",
        )
        with open(self.candidate_path, "w", encoding="utf-8") as handle:
            handle.write(record.to_json())

    def verify_argv(self, *extra: str) -> List[str]:
        return [
            "release",
            "verify",
            "--candidate",
            self.candidate_path,
            "--artifact",
            "dist.tar.gz=" + self.artifact,
            *extra,
        ]

    def test_matching_evidence_exits_zero_and_says_what_it_proved(self):
        run = self.run_cli(self.verify_argv("--source-sha", self.HEAD))
        self.assertEqual(run.code, 0, run.stderr)
        self.assertIn("dist.tar.gz", run.stdout)
        self.assertIn(self.HEAD[:12], run.stdout)

    def test_the_head_defaults_to_the_one_this_run_is_for(self):
        # Every workflow already knows its own commit; making the caller restate it
        # is how a caller eventually restates the wrong one.
        run = self.run_cli(self.verify_argv(), env={"GITHUB_SHA": self.HEAD})
        self.assertEqual(run.code, 0, run.stderr)
        run = self.run_cli(self.verify_argv(), env={"GITHUB_SHA": self.OTHER})
        self.assertNotEqual(run.code, 0)
        self.assertIn("source-head-mismatch", run.stdout)

    def test_no_candidate_fails_the_job(self):
        run = self.run_cli(
            ["release", "verify", "--source-sha", self.HEAD,
             "--artifact", "dist.tar.gz=" + self.artifact]
        )
        self.assertNotEqual(run.code, 0)
        self.assertIn("no-tested-candidate", run.stdout)

    def test_a_gate_that_did_not_pass_fails_the_job(self):
        from continuum.release import provenance

        record = provenance.build_candidate(
            source_sha=self.HEAD,
            artifacts={"dist.tar.gz": self.artifact},
            gate=".github/workflows/validate.yml",
            gate_conclusion=provenance.GATE_CANCELLED,
        )
        with open(self.candidate_path, "w", encoding="utf-8") as handle:
            handle.write(record.to_json())
        run = self.run_cli(self.verify_argv("--source-sha", self.HEAD))
        self.assertNotEqual(run.code, 0)
        self.assertIn("gate-not-green", run.stdout)

    def test_a_rebuilt_artifact_fails_the_job(self):
        rebuilt = os.path.join(self.workdir, "rebuilt.tar.gz")
        with open(rebuilt, "wb") as handle:
            handle.write(b"a rebuild, near enough")
        run = self.run_cli(
            ["release", "verify", "--candidate", self.candidate_path,
             "--source-sha", self.HEAD,
             "--artifact", "dist.tar.gz=" + rebuilt]
        )
        self.assertNotEqual(run.code, 0)
        self.assertIn("artifact-digest-mismatch", run.stdout)

    def test_a_publish_that_names_no_head_fails_rather_than_skipping_the_check(self):
        run = self.run_cli(self.verify_argv("--source-sha", ""))
        self.assertNotEqual(run.code, 0)
        self.assertIn("publish-head-unknown", run.stdout)

    def test_a_malformed_artifact_argument_is_a_usage_error(self):
        run = self.run_cli(
            ["release", "verify", "--candidate", self.candidate_path,
             "--artifact", "dist.tar.gz"]
        )
        self.assertNotEqual(run.code, 0)
        self.assertIn("NAME=PATH", run.stdout)

    def test_claiming_no_commit_and_naming_one_at_once_is_refused(self):
        # Whichever the caller meant, they cannot both be true, and silently
        # honouring the flag would disable the check the caller asked for.
        run = self.run_cli(self.verify_argv("--source-sha", self.HEAD, "--no-commit-head"))
        self.assertNotEqual(run.code, 0)
        self.assertIn("contradictory", run.stdout)

    def test_a_publish_with_no_commit_may_opt_out(self):
        run = self.run_cli(
            ["release", "verify", "--candidate", self.candidate_path,
             "--artifact", "dist.tar.gz=" + self.artifact,
             "--no-commit-head"],
            env={"GITHUB_SHA": ""},
        )
        self.assertEqual(run.code, 0, run.stderr)

    def test_the_opt_out_still_works_where_git_hub_sha_is_always_set(self):
        # The case the flag exists for. In a workflow run GITHUB_SHA is always in
        # the environment, so a job publishing a tree with no commit cannot
        # un-set it. Rejecting the flag there makes the only legal caller of it
        # the one place it is never needed.
        run = self.run_cli(
            ["release", "verify", "--candidate", self.candidate_path,
             "--artifact", "dist.tar.gz=" + self.artifact,
             "--no-commit-head"],
            env={"GITHUB_SHA": self.HEAD},
        )
        self.assertEqual(run.code, 0, run.stderr)
        self.assertNotIn("contradictory", run.stdout)

    def test_the_opt_out_does_not_silently_keep_the_inferred_head(self):
        # Leaving it in would put a head on the verification that was never
        # compared, and the artifact of a check that did not happen looks like the
        # artifact of one that did.
        out = os.path.join(self.workdir, "verification-no-commit.json")
        run = self.run_cli(
            ["release", "verify", "--candidate", self.candidate_path,
             "--artifact", "dist.tar.gz=" + self.artifact,
             "--no-commit-head", "--out", out],
            env={"GITHUB_SHA": self.HEAD},
        )
        self.assertEqual(run.code, 0, run.stderr)
        with open(out, encoding="utf-8") as handle:
            document = json.load(handle)
        # Still green on the bytes -- the head check is what was opted out of, and
        # the artifact digests are still proved.
        self.assertTrue(document["ok"])
        self.assertEqual(document["verified"], ["dist.tar.gz"])

    def test_the_verification_is_written_where_a_workflow_can_upload_it(self):
        out = os.path.join(self.workdir, "verification.json")
        run = self.run_cli(self.verify_argv("--source-sha", self.HEAD, "--out", out))
        self.assertEqual(run.code, 0, run.stderr)
        with open(out, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertTrue(document["ok"])
        self.assertEqual(document["verified"], ["dist.tar.gz"])

    def test_a_refusal_is_written_too(self):
        # A refusal that leaves no artifact behind is a failure a later step cannot
        # explain.
        out = os.path.join(self.workdir, "verification.json")
        run = self.run_cli(self.verify_argv("--source-sha", self.OTHER, "--out", out))
        self.assertNotEqual(run.code, 0)
        with open(out, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertFalse(document["ok"])
        self.assertEqual(document["code"], "source-head-mismatch")

    def test_an_unreadable_record_is_a_usage_error_not_a_publish(self):
        with open(self.candidate_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        run = self.run_cli(self.verify_argv("--source-sha", self.HEAD))
        self.assertNotEqual(run.code, 0)
        self.assertIn("unreadable tested candidate", run.stdout)


class ReleaseCommandSurfaceTests(unittest.TestCase):
    def test_release_requires_a_subcommand(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["release"])

    def test_plan_and_sign_both_require_a_target(self):
        for command in ("plan", "sign"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        cli.main(["release", command])

    def test_both_commands_accept_the_signing_material_expectation(self):
        parser = cli.build_parser()
        for command in ("plan", "sign"):
            with self.subTest(command=command):
                args = parser.parse_args(
                    ["release", command, "--target", "t", "--signing-material", "present"]
                )
                self.assertEqual(args.signing_material, "present")
                self.assertEqual(args.target, "t")

    def test_the_expectation_is_optional(self):
        parser = cli.build_parser()
        args = parser.parse_args(["release", "sign", "--target", "t"])
        self.assertEqual(args.signing_material, "auto")


if __name__ == "__main__":
    unittest.main()
