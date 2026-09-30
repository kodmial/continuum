"""The release plane, driven as a workflow drives it.

`resolve` → `target` → `transaction`, each as a separate `continuum release ...`
invocation in a separate working directory, sharing nothing but files. That is
the shape the reusable workflow has, so it is the shape worth testing: a command
that only works when two jobs are in one process is a command that will not
survive the workflow boundary.

The fixture adapter stands in for a platform so the privilege boundary can be
observed — the target job is handed components with no publisher, and the
transaction is handed one, and the tests check the destination saw nothing until
the second.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import tempfile
import unittest
from typing import Any, Dict, List, Optional

from continuum import cli
from continuum import config as config_module
from continuum.release import commands, entrypoints
from continuum.release.transaction import TargetFragment

from . import release_core_support as core_support

SHA = core_support.SHA
VERSION = core_support.VERSION
REPOSITORY = core_support.REPOSITORY
ADAPTER = core_support.ADAPTER

APPLE_TARGET = """
version: 1
review:
  provider: none
release:
  targets:
    - id: macos-app
      adapter: apple
      platform: macos
      build_strategy: swiftpm
      distribution: direct
      architectures:
        - arm64
      artifacts:
        - tar.gz
      binaries:
        - name: DemoAgent
          identifier: com.example.agent
      signing:
        mode: self-signed-stable
        identity: Example Co
        p12_secret: CONTINUUM_APPLE_P12
        password_secret: CONTINUUM_APPLE_P12_PASSWORD
"""

# An iOS target: the config validates it and the entrypoint table has a row for
# it, but no adapter can build it today, so it resolves as a declared target.
DECLARED_TARGET = """    - id: agent-ios
      adapter: apple
      platform: ios
      build_strategy: swiftpm
      distribution: direct
      architectures:
        - arm64
      artifacts:
        - tar.gz
      binaries:
        - name: DemoAgent
          identifier: com.example.agent
      signing:
        mode: self-signed-stable
        identity: Example Co
        p12_secret: CONTINUUM_APPLE_P12
        password_secret: CONTINUUM_APPLE_P12_PASSWORD
"""

TWO_TARGETS = APPLE_TARGET + DECLARED_TARGET

SECRETS = {
    "CONTINUUM_APPLE_P12": "b2xkLWNlcnRpZmljYXRlLW1hdGVyaWFs",
    "CONTINUUM_APPLE_P12_PASSWORD": "corr3ct-h0rse-battery-staple",
}


class ReleasePlaneTestCase(unittest.TestCase):
    """A workdir, a config, and a scrubbed environment per test."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="continuum-plane-")
        self.config_path = os.path.join(self.workdir, ".continuum.yml")
        self.runs = os.path.join(self.workdir, "runs")
        self._sandbox()
        self.write(APPLE_TARGET)
        self.adapter = core_support.FixtureAdapter(workdir=self.workdir)
        self.repository = core_support.MemoryReleaseRepository()

    def _sandbox(self) -> None:
        keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"}
        saved = {key: os.environ.pop(key) for key in list(os.environ) if key not in keep}
        self.addCleanup(lambda: self._restore(keep, saved))

    def _restore(self, keep, saved) -> None:
        for key in list(os.environ):
            if key not in keep:
                del os.environ[key]
        os.environ.update(saved)

    def write(self, document: str) -> str:
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(document)
        return self.config_path

    def run_cli(self, argv: List[str], env: Optional[Dict[str, str]] = None):
        import contextlib
        import io

        previous = dict(os.environ)
        for key in list(os.environ):
            if key not in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH"):
                del os.environ[key]
        os.environ.update(env or {})
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(previous)
        return code, stdout.getvalue(), stderr.getvalue()

    def outputs_path(self) -> str:
        return os.environ.get("GITHUB_OUTPUT", "")

    def resolve(self, env: Optional[Dict[str, str]] = None, **overrides) -> tuple:
        argv = [
            "release", "resolve",
            "--config", self.config_path,
            "--fragment-root", self.runs,
            "--version", VERSION,
            "--source-sha", SHA,
        ]
        for name, value in overrides.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        return self.run_cli(argv, SECRETS if env is None else env)

    def build_components(self, *targets: str):
        """Components a target job holds, with the fixture as the only adapter."""

        from continuum.release import plane
        from continuum.release.core import ReleaseComponents
        from continuum.release.version import ExplicitVersion

        return ReleaseComponents(
            eligibility=plane.eligibility_for(),
            version=ExplicitVersion(),
            notes=plane.notes_for(),
            adapters={ADAPTER: self.adapter},
            publishers=(),
            syncs=(),
        )

    def matrix(self) -> entrypoints.ReleaseMatrix:
        with open(os.path.join(self.runs, "matrix.json"), "r", encoding="utf-8") as handle:
            return entrypoints.parse_matrix_file(handle.read())


class ResolveCommandTests(ReleasePlaneTestCase):
    def test_writes_the_matrix_and_exits_cleanly(self):
        code, out, _err = self.resolve()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.runs, "matrix.json")))
        self.assertIn("continuum.release-matrix/v1", out)

    def test_prints_the_matrix_as_the_machine_facing_document(self):
        _code, out, _err = self.resolve()
        document = json.loads(out[out.index("{") : out.rindex("}") + 1])
        self.assertEqual(document["version"], VERSION)
        self.assertEqual(document["source_sha"], SHA)

    def test_the_written_matrix_is_the_printed_one(self):
        """A workflow passes one and reads the other; they cannot differ."""

        _code, out, _err = self.resolve()
        printed = json.loads(out[out.index("{") : out.rindex("}") + 1])
        self.assertEqual(printed, self.matrix().describe())

    def test_names_the_runner_and_the_secrets_it_needs(self):
        code, out, _err = self.resolve()
        self.assertEqual(code, 0)
        document = json.loads(out[out.index("{") : out.rindex("}") + 1])
        row = document["targets"][0]
        self.assertEqual(row["runner"], entrypoints.RUNNER_MACOS)
        self.assertEqual(
            sorted(row["secrets"]), sorted(SECRETS), "the row names the secrets it reads, "
            "which is what a job's `secrets:` mapping is built from"
        )

    def test_refuses_to_resolve_without_the_signing_material(self):
        code, out, _err = self.resolve(env={}, signing_material="present")
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertFalse(
            os.path.isfile(os.path.join(self.runs, "matrix.json")),
            "a matrix whose jobs cannot read their secrets is a matrix that would "
            "dispatch jobs guaranteed to fail",
        )

    def test_proves_the_fallback_path_when_told_the_secrets_are_absent(self):
        code, _out, _err = self.run_cli(
            [
                "release", "resolve",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--version", VERSION,
                "--source-sha", SHA,
                "--signing-material", "absent",
            ]
        )
        self.assertEqual(code, 0)

    def test_refuses_a_job_that_claims_its_secrets_are_absent_while_holding_one(self):
        code, out, _err = self.run_cli(
            [
                "release", "resolve",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--version", VERSION,
                "--source-sha", SHA,
                "--signing-material", "absent",
            ],
            SECRETS,
        )
        self.assertEqual(code, 1)
        self.assertIn("signing-material-unexpected", out)

    def test_refuses_an_abbreviated_commit(self):
        code, out, _err = self.resolve(source_sha="abc1234")
        self.assertEqual(code, 1)
        self.assertIn("full source SHA", out)


class TargetCommandTests(ReleasePlaneTestCase):
    def test_refuses_a_target_with_no_release_adapter(self):
        """Apple has a complete signing plan and no release adapter.

        The refusal names both halves, because the natural next question is
        "so use the plan then" and the answer is that a plan produces no
        manifest for the transaction to merge.
        """

        self.resolve()
        code, out, _err = self.run_cli(
            [
                "release", "target",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--target", "macos-app",
                "--repository", REPOSITORY,
            ],
            SECRETS,
        )
        self.assertEqual(code, 1)
        self.assertIn("adapter-not-walkable", out)
        self.assertIn("no manifest", out)

    def test_refuses_a_target_the_policy_does_not_declare(self):
        self.resolve()
        code, out, _err = self.run_cli(
            [
                "release", "target",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--target", "ghost",
                "--repository", REPOSITORY,
            ],
            SECRETS,
        )
        self.assertEqual(code, 1)
        self.assertIn("macos-app", out)

    def test_refuses_to_build_without_a_matrix(self):
        code, out, _err = self.run_cli(
            [
                "release", "target",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--target", "macos-app",
                "--repository", REPOSITORY,
            ],
            SECRETS,
        )
        self.assertEqual(code, 1)
        self.assertIn("matrix-absent", out)


class TransactionCommandTests(ReleasePlaneTestCase):
    def fragments(self, *target_ids: str):
        """What a build job would have written, for the given targets."""

        from continuum.release.transaction import build_target, resolve_release, write_fragment

        self.resolve()  # a workflow's transaction job only ever sees resolve's output
        matrix = self.matrix()
        request = resolve_release(
            _apple_config(),
            event=commands.load_event(
                repository=REPOSITORY, source_sha=SHA, version=VERSION
            ),
            version=VERSION,
            source_sha=SHA,
            matrix=matrix,
            workdir=self.workdir,
        )
        for target_id in target_ids:
            fragment = build_target(
                request, matrix, target_id, components=self.build_components(target_id)
            )
            write_fragment(self.runs, fragment)

    def publish(self, **overrides):
        argv = [
            "release", "transaction",
            "--config", self.config_path,
            "--fragment-root", self.runs,
            "--repository", REPOSITORY,
        ]
        for name, value in overrides.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        return self.run_cli(argv, {"GITHUB_TOKEN": "not-a-real-token", **SECRETS})

    def test_refuses_without_a_token(self):
        self.fragments()
        code, out, _err = self.run_cli(
            [
                "release", "transaction",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--repository", REPOSITORY,
            ],
            SECRETS,
        )
        self.assertEqual(code, 1)
        self.assertIn("missing-credential", out)

    def test_refuses_a_partial_set_before_a_draft_exists(self):
        self.fragments()
        code, out, _err = self.publish()
        self.assertEqual(code, 1)
        self.assertIn("macos-app", out)
        self.assertEqual(self.repository.calls, [])


def _apple_config():
    from continuum import config as config_module

    return config_module.parse_config(APPLE_TARGET, source="test")


class DispatchedReleaseTests(ReleasePlaneTestCase):
    """A release with no tag, cut from the default branch.

    The tag is the identity of a tag-push, and its absence is the whole shape of
    a dispatched release. Anything that insists on a tag here refuses a release
    a maintainer is entitled to make, and anything that invents one puts a name
    in the record that the repository cannot back.
    """

    def build_request(self, event: str):
        """The request the target job builds, assembled as the command does."""

        from continuum.release.transaction import resolve_release

        matrix = self.matrix()
        resolved = commands.load_event(
            repository=REPOSITORY, source_sha=SHA, version=VERSION, name=event
        )
        return resolve_release(
            _apple_config(),
            event=resolved,
            version=VERSION,
            source_sha=SHA,
            matrix=matrix,
            workdir=self.workdir,
            version_checks=commands.version_checks_for(matrix, resolved),
        )

    def test_the_matrix_names_no_tag_for_either_kind_of_release(self):
        """The matrix is derived from the version; a tag is not part of it.

        So a dispatched release and a tag-push resolve to the same document, and
        the difference between them is carried by the event the target and
        transaction jobs build from it - not by a field the matrix would have to
        hold in two different states.
        """

        code, out, _err = self.resolve()
        self.assertEqual(code, 0)
        matrix = self.matrix()
        self.assertEqual(matrix.version, VERSION)
        self.assertEqual(
            matrix.artifact().count("v" + VERSION),
            0,
            "the matrix is derived from the version, and a tag nobody pushed is not in it",
        )

    def test_a_tag_push_cross_checks_the_tag_against_the_version(self):
        self.resolve()
        request = self.build_request("tag-push")
        self.assertTrue(request.version_checks)
        self.assertEqual(
            [check.tag for check in request.version_checks], ["v" + VERSION]
        )

    def test_a_dispatch_has_nothing_to_cross_check_against(self):
        self.resolve()
        request = self.build_request("workflow-dispatch")
        self.assertEqual(
            request.version_checks,
            (),
            "pointing the version stage at a tag nobody pushed would fail every "
            "dispatched build",
        )

    def test_a_dispatch_is_eligible_and_a_tag_push_is_too(self):
        from continuum.release import plane

        for name in ("tag-push", "workflow-dispatch"):
            with self.subTest(event=name):
                verdict = plane.eligibility_for().evaluate(
                    commands.load_event(
                        repository=REPOSITORY, source_sha=SHA, version=VERSION, name=name
                    )
                )
                self.assertTrue(verdict.eligible, "{}: {}".format(name, verdict.reason))

    def test_a_dispatch_from_another_branch_is_refused(self):
        self.resolve()
        verdict = __import__(
            "continuum.release.plane", fromlist=["plane"]
        ).eligibility_for().evaluate(
            commands.load_event(
                repository=REPOSITORY,
                source_sha=SHA,
                version=VERSION,
                name="workflow-dispatch",
                ref="refs/heads/feature/thing",
            )
        )
        self.assertFalse(verdict.eligible)
        self.assertEqual(verdict.code, "ref-not-releasable")


class MatrixRowsOutputTests(ReleasePlaneTestCase):
    """The `rows` output is consumed as `strategy.matrix.include`.

    GitHub's `fromJSON` reads it, so its shape is a contract with the workflow
    rather than an implementation detail of the command: a row missing `runner`
    dispatches a job with an empty `runs-on`, and a declared row appearing here
    dispatches a job that can never produce a fragment.
    """

    def resolve_outputs(self, **overrides):
        args = argparse.Namespace(
            config=self.config_path,
            fragment_root=self.runs,
            version=VERSION,
            source_sha=SHA,
            channel="stable",
            matrix="",
            out="",
            signing_material="auto",
            event="tag-push",
            include_declared=False,
            dry_run=False,
        )
        for name, value in overrides.items():
            setattr(args, name, value)
        config = config_module.parse_config(
            pathlib.Path(self.config_path).read_text(encoding="utf-8"), source="test"
        )
        return commands.cmd_resolve(args, config=config)

    def test_is_an_array_of_objects_with_the_keys_the_workflow_reads(self):
        _status, outputs = self.resolve_outputs()
        rows = json.loads(outputs["rows"])
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertIsInstance(row, dict)
            self.assertEqual(
                set(row), {"target", "adapter", "runner", "secrets", "fragment"}
            )
            self.assertEqual(row["runner"], entrypoints.RUNNER_MACOS)
            self.assertEqual(row["adapter"], "apple")
            self.assertEqual(row["secrets"], sorted(SECRETS))
            self.assertEqual(row["fragment"], entrypoints.fragment_path(self.runs, "macos-app"))

    def test_is_one_line_so_a_workflow_input_survives_it(self):
        """`rows` crosses a job boundary as an output, and outputs are lines."""

        _status, outputs = self.resolve_outputs()
        self.assertNotIn("\n", outputs["rows"])
        self.assertNotIn("\r", outputs["rows"])

    def test_never_lists_a_declared_target(self):
        """A declared row has no runner and no build, so it gets no job."""

        self.write(TWO_TARGETS)
        _status, outputs = self.resolve_outputs(include_declared=True)
        rows = json.loads(outputs["rows"])
        self.assertEqual([row["target"] for row in rows], ["macos-app"])
        self.assertEqual(outputs["declared_targets"], "agent-ios")

    def test_refuses_to_resolve_at_all_unless_the_caller_opts_in(self):
        self.write(TWO_TARGETS)
        with self.assertRaises(entrypoints.EntrypointError) as caught:
            self.resolve_outputs()
        self.assertIn("agent-ios", str(caught.exception))
        self.assertIn("include_declared", str(caught.exception))

    def test_a_declared_row_is_listed_in_the_matrix_but_never_dispatched(self):
        self.write(TWO_TARGETS)
        _status, outputs = self.resolve_outputs(include_declared=True)
        matrix = self.matrix()
        self.assertEqual(sorted(matrix.target_ids), ["agent-ios", "macos-app"])
        self.assertEqual(matrix.declared_ids, ("agent-ios",))
        self.assertEqual(matrix.buildable_ids, ("macos-app",))
        self.assertNotIn(
            "agent-ios",
            outputs["targets"],
            "the dispatched list and the buildable list are the same list",
        )

    def test_the_multiline_matrix_is_written_to_the_output_file_encoded(self):
        """`GITHUB_OUTPUT` is a line format, and the matrix is not one line.

        A bare newline in an output value silently truncates everything after it,
        so the matrix would arrive at the build jobs as half a document and the
        failure would look like a schema error rather than an encoding one.
        """

        path = os.path.join(self.workdir, "outputs.txt")
        code, _out, _err = self.run_cli(
            [
                "release", "resolve",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--version", VERSION,
                "--source-sha", SHA,
            ],
            dict(SECRETS, GITHUB_OUTPUT=path),
        )
        self.assertEqual(code, 0)
        written = pathlib.Path(path).read_text(encoding="utf-8")
        self.assertNotIn("\n{", written, "a raw newline inside a value is not encoded")
        values = {}
        for line in written.splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        self.assertEqual(
            json.loads(values["matrix"])["version"], VERSION, "the encoded value is a document"
        )
        self.assertEqual(json.loads(values["rows"])[0]["target"], "macos-app")
        self.assertEqual(values["ok"], "true")

    def test_a_value_containing_a_blank_line_survives_the_encoding(self):
        """The summary carries a blank line today, and one more would break it."""

        path = os.path.join(self.workdir, "outputs.txt")
        self.repository.calls = []
        _code, _out, _err = self.run_cli(
            [
                "release", "transaction",
                "--config", self.config_path,
                "--fragment-root", self.runs,
                "--repository", REPOSITORY,
            ],
            dict(SECRETS, GITHUB_OUTPUT=path),
        )
        written = pathlib.Path(path).read_text(encoding="utf-8")
        for line in written.splitlines():
            key, _, value = line.partition("=")
            self.assertNotIn(value, ("", "\n"), "{} was written as an empty line".format(key))


class EventTests(ReleasePlaneTestCase):
    """The event a release is allowed to believe."""

    def test_a_tag_push_derives_its_tag_from_the_version(self):
        event = commands.load_event(repository=REPOSITORY, source_sha=SHA, version=VERSION)
        self.assertEqual(event.tag, f"v{VERSION}")
        self.assertEqual(event.ref, f"refs/tags/v{VERSION}")
        self.assertEqual(event.name, "tag-push")

    def test_a_dispatch_has_no_tag_and_names_a_branch(self):
        event = commands.load_event(
            repository=REPOSITORY, source_sha=SHA, version=VERSION, name="workflow-dispatch"
        )
        self.assertEqual(
            event.tag, "", "nobody pushed a tag, so the release's record must not name one"
        )
        self.assertEqual(event.ref, "refs/heads/main")

    def test_a_dispatch_honours_an_explicit_branch(self):
        event = commands.load_event(
            repository=REPOSITORY,
            source_sha=SHA,
            version=VERSION,
            name="workflow-dispatch",
            ref="refs/heads/stable",
            default_branch="stable",
        )
        self.assertEqual(event.ref, "refs/heads/stable")

    def test_refuses_to_invent_a_tag_for_a_dispatch(self):
        """The one input that must never be synthesised.

        A fabricated tag reaches the journal, the release body, and the
        publisher's identity, so it is not a formatting detail.
        """

        with self.assertRaises(commands.ReleasePlaneError) as caught:
            commands.load_event(
                repository=REPOSITORY,
                source_sha=SHA,
                version=VERSION,
                name="workflow-dispatch",
                tag=f"v{VERSION}",
            )
        self.assertEqual(caught.exception.code, "event-unsupported")
        self.assertIn("was not pushed", str(caught.exception))

    def test_refuses_an_event_a_release_cannot_be_dispatched_as(self):
        for name in ("push", "scheduled", "release-pr-merged", "whatever"):
            with self.subTest(name=name):
                with self.assertRaises(commands.ReleasePlaneError) as caught:
                    commands.load_event(
                        repository=REPOSITORY, source_sha=SHA, version=VERSION, name=name
                    )
                self.assertEqual(caught.exception.code, "event-unsupported")


class ReleaseNotesTests(ReleasePlaneTestCase):
    """The body a consumer reads on the release page."""

    def build_matrix(self, *rows) -> entrypoints.ReleaseMatrix:
        target = entrypoints.MatrixTarget(
            target="macos-app",
            adapter="apple",
            runner=entrypoints.RUNNER_MACOS,
            argv=("continuum", "release", "target"),
            source_sha=SHA,
            version=VERSION,
            fragment=entrypoints.fragment_path(self.runs, "macos-app"),
            declared=False,
        )
        declared = entrypoints.MatrixTarget(
            target="future",
            adapter="android",
            runner="ubuntu-24.04",
            argv=("noop",),
            source_sha=SHA,
            version=VERSION,
            fragment=entrypoints.fragment_path(self.runs, "future"),
            declared=True,
        )
        return entrypoints.ReleaseMatrix(
            version=VERSION,
            source_sha=SHA,
            targets=rows or ((target,) if not self.declared else (target, declared)),
            channel="stable",
        )

    declared = False

    def tagged(self):
        return commands.load_event(repository=REPOSITORY, source_sha=SHA, version=VERSION)

    def test_a_tagged_release_is_described_by_its_tag(self):
        body = commands.release_notes(self.build_matrix(), self.tagged())
        self.assertIn(f"# v{VERSION}", body)
        self.assertIn(SHA[:12], body)
        self.assertNotIn(
            "was pushed",
            body,
            "a tagged release is not dispatched, and saying otherwise makes a reader "
            "look for a tag that does not exist",
        )

    def test_a_dispatched_release_does_not_claim_a_tag(self):
        event = commands.load_event(
            repository=REPOSITORY, source_sha=SHA, version=VERSION, name="workflow-dispatch"
        )
        body = commands.release_notes(self.build_matrix(), event)
        self.assertIn(f"# {VERSION}", body)
        self.assertIn(f"no `v{VERSION}` tag was pushed", body)

    def test_names_a_declared_target_on_the_release_page(self):
        """The job summary is not where a consumer looks."""

        self.declared = True
        try:
            body = commands.release_notes(self.build_matrix(), self.tagged())
        finally:
            self.declared = False
        self.assertIn("Declared but not shipped", body)
        self.assertIn("`future`", body)
        self.assertIn("no release adapter builds it", body)

    def test_says_nothing_about_a_gap_that_does_not_exist(self):
        body = commands.release_notes(self.build_matrix(), self.tagged())
        self.assertNotIn("Declared but not shipped", body)

    def test_never_names_a_secret(self):
        self.declared = True
        try:
            body = commands.release_notes(self.build_matrix(), self.tagged())
        finally:
            self.declared = False
        for value in SECRETS.values():
            self.assertNotIn(value, body)


class WiringTests(ReleasePlaneTestCase):
    def test_the_build_table_is_explicit_about_what_it_has(self):
        """A discovered registry would drift as adapters are added.

        The property worth pinning is that Apple is *absent*: it is the adapter
        the config supports, so a table that quietly included it would dispatch a
        build job that cannot produce a manifest.
        """

        self.assertIn("apple", entrypoints.supported())
        self.assertNotIn(ADAPTER, commands.CORE_ADAPTERS)
        self.assertNotIn("apple", commands.CORE_ADAPTERS)

    def test_names_an_adapter_with_no_release_adapter(self):
        config = _apple_config()
        with self.assertRaises(commands.ReleasePlaneError) as caught:
            commands.build_components(config, "macos-app")
        self.assertEqual(caught.exception.code, "adapter-not-walkable")

    def test_a_build_job_holds_no_destination(self):
        """The privilege boundary, asserted on the wiring rather than the run.

        `build_components` takes no publisher argument at all, so a future edit
        that adds one is a visible change to this function rather than an
        unnoticed widening of what an untrusted ref's build can reach.
        """

        import inspect

        parameters = inspect.signature(commands.build_components).parameters
        self.assertEqual(
            [name for name in parameters if "publish" in name or "destination" in name],
            [],
        )

    def test_the_transaction_is_the_only_thing_that_reads_a_token(self):
        import inspect

        for name in ("cmd_resolve", "cmd_target"):
            source = inspect.getsource(getattr(commands, name))
            self.assertNotIn("GITHUB_TOKEN", source, f"{name} must not read a token")
        self.assertIn("GITHUB_TOKEN", inspect.getsource(commands.transaction_components))

    def test_every_command_code_has_advice(self):
        """A code with no advice is a code a reader has to interpret alone."""

        from continuum.release.transaction import FAILURE_TAXONOMY

        for name in dir(commands):
            value = getattr(commands, name)
            if not isinstance(value, type):
                continue
            for code in getattr(value, "__doc__", "").split() if value.__doc__ else ():
                if code.startswith("code="):
                    self.assertIn(code, FAILURE_TAXONOMY, code)

    def test_describes_a_failure_with_its_advice_on_a_separate_line(self):
        error = commands.ReleasePlaneError("no matrix here", code="matrix-absent")
        message, retryable, code, advice = commands.describe_failure(error)
        self.assertEqual(code, "matrix-absent")
        self.assertFalse(retryable)
        self.assertNotIn(advice, message)
        self.assertIn("resolve job", advice)

    def test_an_unknown_code_is_reported_as_treated_as_fatal(self):
        message, retryable, code, advice = commands.describe_failure(
            commands.ReleasePlaneError("something new", code="invented-code")
        )
        self.assertEqual(code, "invented-code")
        self.assertFalse(retryable)
        self.assertIn("unclassified", advice)


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()
