"""The generic hooks seam: argument vectors, measured artifacts, pinned signers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from continuum.release import hooks
from continuum.release.contract import (
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ReleasePrRequest,
    TargetSpec,
    TYPE_CHECKSUMS,
)


def document(**overrides):
    base = {
        "schema": hooks.HOOKS_SCHEMA,
        "targets": [
            {
                "id": "linux-x64",
                "platform": "linux",
                "runner": "ubuntu-latest",
                "build": {"command": ["bash", "build.sh"]},
                "sign": {"command": ["bash", "sign.sh"], "identity": "acme-ci"},
                "verify": {"command": ["bash", "verify.sh"], "identity": "acme-ci"},
                "artifacts": [
                    {
                        "name": "widget-{version}-linux-x64.tar.gz",
                        "path": "dist/widget.tar.gz",
                        "type": "archive",
                        "classifier": "linux-x64",
                    }
                ],
            }
        ],
    }
    base.update(overrides)
    return base


class DocumentTests(unittest.TestCase):
    def test_a_valid_document_is_parsed(self):
        plan = hooks.parse_hooks(document())
        self.assertEqual([target.id for target in plan.targets], ["linux-x64"])
        self.assertEqual(plan.runners(), {"linux-x64": "ubuntu-latest"})
        self.assertEqual(plan.specs(), (TargetSpec(id="linux-x64", adapter="command"),))

    def test_an_unknown_schema_is_refused(self):
        with self.assertRaises(hooks.HooksError) as caught:
            hooks.parse_hooks(document(schema="continuum.release-hooks/v2"))
        self.assertEqual(caught.exception.code, "hooks-schema-unknown")

    def test_a_secret_pasted_into_the_document_is_refused(self):
        token = "ghp_" + "a" * 36
        with self.assertRaises(hooks.HooksError) as caught:
            hooks.parse_hooks(
                {
                    "schema": hooks.HOOKS_SCHEMA,
                    "targets": [
                        {
                            "id": "linux-x64",
                            "build": {"command": ["bash", token]},
                            "artifacts": [
                                {"name": "a.tar.gz", "path": "a.tar.gz", "type": "archive"}
                            ],
                        }
                    ],
                }
            )
        self.assertEqual(caught.exception.code, "hooks-secret-literal")

    def test_an_unknown_template_slot_is_refused(self):
        payload = document()
        payload["targets"][0]["artifacts"][0]["name"] = "widget-{build_number}.tar.gz"
        with self.assertRaises(hooks.HooksError) as caught:
            hooks.parse_hooks(payload)
        self.assertEqual(caught.exception.code, "hooks-template-invalid")

    def test_a_target_must_declare_an_artifact(self):
        payload = document()
        payload["targets"][0]["artifacts"] = []
        with self.assertRaises(hooks.HooksError):
            hooks.parse_hooks(payload)

    def test_identity_without_a_command_is_refused(self):
        payload = document()
        payload["targets"][0]["sign"] = {"identity": "acme-ci"}
        with self.assertRaises(hooks.HooksError):
            hooks.parse_hooks(payload)

    def test_unknown_keys_are_refused_rather_than_ignored(self):
        payload = document()
        payload["targets"][0]["publish"] = {"command": ["true"]}
        with self.assertRaises(hooks.HooksError):
            hooks.parse_hooks(payload)

    def test_upstream_and_downstream_commands_are_parsed(self):
        payload = document(
            release_pr={"command": ["bash", "release-pr.sh"]},
            publishers={"homebrew": {"command": ["bash", "tap.sh"], "attempts": 2}},
            syncs={"macports": {"command": ["bash", "port.sh"]}},
        )
        plan = hooks.parse_hooks(payload)
        self.assertIsNotNone(plan.release_pr)
        self.assertEqual([item.name for item in plan.publishers], ["homebrew"])
        self.assertEqual([item.name for item in plan.syncs], ["macports"])

    def test_load_hooks_reads_a_relative_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hooks.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document(), handle)
            plan = hooks.load_hooks("hooks.json", workdir=directory)
            self.assertEqual(len(plan.targets), 1)

    def test_a_missing_document_is_a_stable_failure(self):
        with self.assertRaises(hooks.HooksError) as caught:
            hooks.load_hooks("/nonexistent/hooks.json")
        self.assertEqual(caught.exception.code, "hooks-unreadable")


class CommandAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name
        os.makedirs(os.path.join(self.root, "dist"), exist_ok=True)
        for name, body in (
            (
                "build.sh",
                "printf 'payload %s' \"$CONTINUUM_RELEASE_VERSION\" > dist/widget.tar.gz\n",
            ),
            ("sign.sh", "printf 'signed' >> dist/widget.tar.gz\n"),
            ("verify.sh", "test -s dist/widget.tar.gz\n"),
        ):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
                handle.write(body)

    def request(self, *, dry_run=False, sha="a" * 40):
        return BuildRequest(
            target=TargetSpec(id="linux-x64", adapter="command"),
            version="1.2.3",
            source_sha=sha,
            key="key-1",
            workdir=self.root,
            dry_run=dry_run,
            stage="build",
        )

    def plan(self, **overrides):
        payload = document()
        payload["targets"][0].update(overrides)
        return hooks.parse_hooks(payload)

    def test_a_dry_run_declares_without_running(self):
        plan = self.plan()
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        manifest = adapter.build(self.request(dry_run=True))
        self.assertTrue(all(artifact.declared for artifact in manifest.artifacts))
        self.assertFalse(os.path.exists(os.path.join(self.root, "dist/widget.tar.gz")))

    def test_a_real_build_measures_what_it_produced(self):
        plan = self.plan()
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        manifest = adapter.build(self.request())
        artifact = manifest.by_name("widget-1.2.3-linux-x64.tar.gz")
        self.assertGreater(artifact.size, 0)
        self.assertFalse(artifact.declared)
        # A checksum file is produced automatically because checksums default on.
        self.assertTrue(manifest.of_type(TYPE_CHECKSUMS))

    def test_a_missing_promised_artifact_is_a_failure(self):
        payload = document()
        payload["targets"][0]["artifacts"][0]["path"] = "dist/never-written.tar.gz"
        plan = hooks.parse_hooks(payload)
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.build(self.request())
        self.assertEqual(caught.exception.code, "artifact-missing")

    def test_signing_changes_the_digest_and_records_the_identity(self):
        plan = self.plan()
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        built = adapter.build(self.request())
        signed = adapter.sign(self.request(), built)
        before = built.by_name("widget-1.2.3-linux-x64.tar.gz")
        after = signed.by_name("widget-1.2.3-linux-x64.tar.gz")
        self.assertNotEqual(before.digest, after.digest)
        self.assertTrue(after.signed)
        self.assertEqual(after.signing_identity, "acme-ci")

    def test_verification_rechecks_the_bytes_independently(self):
        plan = self.plan()
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        built = adapter.build(self.request())
        signed = adapter.sign(self.request(), built)
        report = adapter.verify(self.request(), signed)
        self.assertTrue(report.verified, report.describe())

        with open(os.path.join(self.root, "dist/widget.tar.gz"), "ab") as handle:
            handle.write(b"tampered")
        report = adapter.verify(self.request(), signed)
        self.assertFalse(report.verified)
        self.assertGreaterEqual(len(report.failures), 1)

    def test_a_wrong_signer_is_refused_even_when_verify_exits_zero(self):
        payload = document()
        payload["targets"][0]["sign"]["identity"] = "somebody-else"
        plan = hooks.parse_hooks(payload)
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        built = adapter.build(self.request())
        signed = adapter.sign(self.request(), built)
        report = adapter.verify(self.request(), signed)
        self.assertFalse(report.verified)

    def test_a_failing_command_is_classified(self):
        with open(os.path.join(self.root, "build.sh"), "w", encoding="utf-8") as handle:
            handle.write("echo nope >&2\nexit 42\n")
        plan = self.plan()
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.build(self.request())
        self.assertEqual(caught.exception.code, "build-hook-failed")
        self.assertIn("42", str(caught.exception))

    def test_a_temporary_exit_is_retryable(self):
        with open(os.path.join(self.root, "build.sh"), "w", encoding="utf-8") as handle:
            handle.write("exit 75\n")
        payload = document()
        payload["targets"][0]["build"]["retry_on_exit"] = [75]
        plan = hooks.parse_hooks(payload)
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.build(self.request())
        self.assertTrue(caught.exception.retryable)

    def test_only_declared_environment_reaches_a_hook(self):
        seen = {}
        artifact = os.path.join(self.root, "dist", "widget.tar.gz")

        def runner(argv, **kwargs):
            seen.update(kwargs["env"])
            with open(artifact, "wb") as handle:
                handle.write(b"payload")
            return subprocess.CompletedProcess(argv, 0, "", "")

        plan = self.plan()
        adapter = hooks.CommandAdapter(
            plan.targets[0],
            workdir=self.root,
            environment={"DECLARED": "yes"},
            secrets=("s3cret",),
            runner=runner,
        )
        adapter.build(self.request())
        self.assertEqual(seen.get("DECLARED"), "yes")
        self.assertNotIn("UNRELATED_JOB_SECRET", seen)
        self.assertEqual(seen.get("CONTINUUM_RELEASE_VERSION"), "1.2.3")

    def test_a_missing_toolchain_is_retryable(self):
        payload = document()
        payload["targets"][0]["build"]["command"] = ["definitely-not-a-real-binary-xyz"]
        plan = hooks.parse_hooks(payload)
        adapter = hooks.build_adapters(plan, workdir=self.root)["command"]
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.build(self.request())
        self.assertEqual(caught.exception.code, "toolchain-unavailable")
        self.assertTrue(caught.exception.retryable)

    def test_output_is_redacted_before_it_reaches_the_message(self):
        with open(os.path.join(self.root, "build.sh"), "w", encoding="utf-8") as handle:
            handle.write("echo 'the token is topsecretvalue' >&2\nexit 1\n")
        plan = self.plan()
        adapter = hooks.build_adapters(
            plan, workdir=self.root, secrets=("topsecretvalue",)
        )["command"]
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.build(self.request())
        self.assertNotIn("topsecretvalue", str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))


class ReleasePrCommandTests(unittest.TestCase):
    def request(self):
        from continuum.release.contract import ReleaseEvent

        return ReleasePrRequest(
            event=ReleaseEvent(
                repository="example/widgets",
                name="release-pr-merged",
                sha="a" * 40,
                default_branch="main",
            ),
            version="1.2.3",
            tag="v1.2.3",
            source_sha="a" * 40,
            key="k",
        )

    def test_a_plan_does_not_open_a_pull_request(self):
        adapter = hooks.CommandReleasePr(["bash", "release-pr.sh"])
        result = adapter.ensure(
            ReleasePrRequest(
                event=self.request().event,
                version="1.2.3",
                tag="v1.2.3",
                source_sha="a" * 40,
                key="k",
                dry_run=True,
            )
        )
        self.assertEqual(result.state, "absent")

    def test_the_command_reports_the_pull_request(self):
        completed = subprocess.CompletedProcess(
            [], 0, '{"state": "open", "number": 7, "version": "1.2.3"}', ""
        )
        adapter = hooks.CommandReleasePr(["release-pr"], runner=mock.Mock(return_value=completed))
        result = adapter.ensure(self.request())
        self.assertEqual(result.state, "open")
        self.assertEqual(result.number, 7)

    def test_a_command_that_says_nothing_is_refused(self):
        completed = subprocess.CompletedProcess([], 0, "not json", "")
        adapter = hooks.CommandReleasePr(["release-pr"], runner=mock.Mock(return_value=completed))
        with self.assertRaises(hooks.HooksError) as caught:
            adapter.ensure(self.request())
        self.assertEqual(caught.exception.code, "release-pr-output-invalid")

    def test_an_absent_pull_request_must_say_why(self):
        completed = subprocess.CompletedProcess([], 0, '{"state": "absent"}', "")
        adapter = hooks.CommandReleasePr(["release-pr"], runner=mock.Mock(return_value=completed))
        with self.assertRaises(hooks.HooksError):
            adapter.ensure(self.request())


if __name__ == "__main__":
    unittest.main()
