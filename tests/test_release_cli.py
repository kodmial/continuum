"""The release command line, end to end against a fake GitHub and real scripts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from continuum.release import cli, github_api
from test_release_github_api import FakeGitHub

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "example/widgets"
VERSION = "1.2.3"

BUILD = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p dist
printf 'widget %s' "$CONTINUUM_RELEASE_VERSION" > dist/widget.tar.gz
"""
SIGN = """#!/usr/bin/env bash
set -euo pipefail
printf ' signed' >> dist/widget.tar.gz
"""
VERIFY = """#!/usr/bin/env bash
set -euo pipefail
test -s dist/widget.tar.gz
"""

HOOKS = {
    "schema": "continuum.release-hooks/v1",
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


class Fixture:
    def __init__(self, root: str):
        self.root = root
        self.workdir = os.path.join(root, "work")
        os.makedirs(self.workdir, exist_ok=True)
        self.write("build.sh", BUILD)
        self.write("sign.sh", SIGN)
        self.write("verify.sh", VERIFY)
        with open(os.path.join(self.workdir, "hooks.json"), "w", encoding="utf-8") as handle:
            json.dump(HOOKS, handle)
        with open(os.path.join(self.workdir, "CHANGELOG.md"), "w", encoding="utf-8") as handle:
            handle.write("# Changelog\n\n## 1.2.3\n\n- first thing\n")
        self.config_path = os.path.join(self.workdir, ".continuum.yml")
        self.write_config("release:\n  hooks: hooks.json\n")
        self.receipts = os.path.join(root, "receipts")
        self.plan_json = os.path.join(root, "plan.json")

    def write(self, name, body, mode=0o755):
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, mode)
        return path

    def write_config(self, body):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("version: 1\n" + body)

    def args(self, phase, **overrides):
        values = {
            "--config": self.config_path,
            "--workdir": self.workdir,
            "--event": "push",
            "--repository": REPOSITORY,
            "--sha": SHA,
            "--version": VERSION,
            "--changed-files": "src/app.c",
            "--receipt-dir": self.receipts,
            "--api-root": "https://api.example",
            "--upload-root": "https://upload.example",
            "--json": self.plan_json,
        }
        values.update(overrides)
        argv = [phase]
        for key, value in values.items():
            if value is None:
                continue
            argv.extend([key, str(value)])
        return argv


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(self.directory.name)
        self.fake = FakeGitHub()
        self._original_transport = github_api.UrllibTransport
        github_api.UrllibTransport = lambda *args, **kwargs: self.fake
        self._env = dict(os.environ)
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["TMPDIR"] = os.environ.get("TMPDIR", self.directory.name)

    def tearDown(self):
        github_api.UrllibTransport = self._original_transport
        os.environ.clear()
        os.environ.update(self._env)

    def main(self, argv):
        return cli.main(argv)

    def payload(self):
        with open(self.fixture.plan_json, encoding="utf-8") as handle:
            return json.load(handle)


class DisabledTests(CliTestCase):
    def test_a_repository_without_release_configuration_is_a_green_no_op(self):
        self.fixture.write_config("review:\n  provider: none\n")
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_OK)
        payload = self.payload()
        self.assertEqual(payload["status"], "no-op")
        self.assertIn("switched off", payload["outcome"]["no_op_reason"])
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(os.path.exists(self.fixture.receipts))

    def test_a_documentation_only_change_is_a_green_no_op(self):
        code = self.main(
            self.fixture.args("execute", **{"--changed-files": "docs/setup.md"})
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.payload()["status"], "no-op")
        self.assertEqual(self.fake.calls, [])

    def test_an_explicit_skip_marker_is_a_green_no_op(self):
        code = self.main(
            self.fixture.args("execute", **{"--commit-message": "docs [skip release]"})
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.payload()["status"], "no-op")


class PlanTests(CliTestCase):
    def test_a_plan_names_the_version_assets_and_destinations(self):
        os.environ.pop("GITHUB_TOKEN")
        code = self.main(self.fixture.args("plan"))
        self.assertEqual(code, cli.EXIT_OK)
        payload = self.payload()
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(payload["tag"], f"v{VERSION}")
        self.assertEqual(payload["manifests"][0]["target"], "linux-x64")
        self.assertEqual([item["destination"] for item in payload["publications"]][0], "github release")
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.fixture.workdir, "dist")))

    def test_a_plan_does_not_need_a_publishing_token(self):
        os.environ.pop("GITHUB_TOKEN")
        self.assertEqual(self.main(self.fixture.args("plan")), cli.EXIT_OK)

    def test_a_plan_from_a_disabled_repository_does_not_read_a_token(self):
        self.fixture.write_config("release: {}\n")
        os.environ.pop("GITHUB_TOKEN")
        self.assertEqual(self.main(self.fixture.args("plan")), cli.EXIT_OK)
        self.assertEqual(self.payload()["status"], "no-op")


class BuildPhaseTests(CliTestCase):
    def test_build_runs_the_toolchain_and_records_without_publishing(self):
        code = self.main(self.fixture.args("build"))
        self.assertEqual(code, cli.EXIT_OK, self.payload())
        payload = self.payload()
        self.assertEqual(payload["status"], "no-op")
        self.assertEqual(len(payload["manifests"]), 1)
        self.assertFalse(payload["published"])
        self.assertEqual(self.fake.calls, [])
        receipt_dir = self.fixture.receipts
        self.assertTrue(os.path.isdir(receipt_dir))
        files = os.listdir(receipt_dir)
        self.assertEqual(len(files), 1, files)

    def test_a_publish_phase_without_a_receipt_refuses_to_build(self):
        code = self.main(self.fixture.args("publish"))
        self.assertNotEqual(code, cli.EXIT_OK)
        payload = self.payload()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["code"], "build-not-recorded")


class ExecuteTests(CliTestCase):
    def test_the_release_is_published_with_the_whole_asset_set(self):
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_OK, self.payload())
        payload = self.payload()
        self.assertEqual(payload["status"], "released")
        self.assertTrue(payload["published"])
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(payload["source_sha"], SHA)
        self.assertEqual(len(payload["manifests"]), 1)
        record = self.fake._find(f"v{VERSION}")
        self.assertIsNotNone(record)
        self.assertFalse(record["draft"])
        names = set(record["assets"])
        self.assertIn(f"widget-{VERSION}-linux-x64.tar.gz", names)
        self.assertIn(f"linux-x64-{VERSION}-SHA256SUMS.txt", names)
        self.assertIn("release_id", payload)
        self.assertTrue(payload["release_url"])

    def test_a_duplicate_execution_uploads_nothing_and_stays_green(self):
        self.assertEqual(self.main(self.fixture.args("execute")), cli.EXIT_OK)
        uploads = [call for call in self.fake.calls if call[0] == "POST"]
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_OK, self.payload())
        self.assertEqual(
            [call for call in self.fake.calls if call[0] == "POST"], uploads
        )
        self.assertEqual(self.payload()["status"], "no-op")

    def test_the_workspace_outputs_carry_the_release_identity(self):
        output = os.path.join(self.fixture.root, "out.txt")
        summary = os.path.join(self.fixture.root, "summary.md")
        code = self.main(
            self.fixture.args(
                "execute", **{"--github-output": output, "--summary": summary}
            )
        )
        self.assertEqual(code, cli.EXIT_OK, self.payload())
        with open(output, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertIn("status=released", lines)
        self.assertIn(f"version={VERSION}", lines)
        self.assertIn(f"source_sha={SHA}", lines)
        with open(summary, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("Continuum release", body)
        self.assertIn(VERSION, body)

    def test_a_tag_that_already_points_elsewhere_is_blocked(self):
        self.fake.refs[f"v{VERSION}"] = {"sha": OTHER_SHA, "type": "commit"}
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_BLOCKED)
        payload = self.payload()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["code"], "tag-source-conflict")

    def test_a_rate_limited_api_is_retryable(self):
        self.fake.fail_once("POST", "/releases", 429, b"{}")
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_RETRYABLE)
        self.assertTrue(self.payload()["resumable"])

    def test_a_permission_failure_is_fatal(self):
        self.fake.fail_once("POST", "/releases", 403, b'{"message":"forbidden"}')
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertFalse(self.payload()["resumable"])

    def test_a_missing_token_fails_closed_before_any_upload(self):
        os.environ.pop("GITHUB_TOKEN")
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.payload()["status"], "failed")
        self.assertEqual(self.fake.calls, [])

    def test_notes_that_change_for_the_same_version_are_refused(self):
        self.assertEqual(self.main(self.fixture.args("execute")), cli.EXIT_OK)
        with open(
            os.path.join(self.fixture.workdir, "CHANGELOG.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write("# Changelog\n\n## 1.2.3\n\n- rewritten after the release\n")
        code = self.main(self.fixture.args("execute"))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.payload()["code"], "notes-changed")


class ConfigurationTests(CliTestCase):
    def test_configured_targets_plan_but_refuse_a_real_build_without_hooks(self):
        self.fixture.write_config(
            "release:\n"
            "  targets:\n"
            "    - id: macos\n"
            "      adapter: apple\n"
            "      architectures: [arm64]\n"
            "      artifacts: [zip]\n"
        )
        code = self.main(self.fixture.args("plan", **{"--hooks": None}))
        self.assertEqual(code, cli.EXIT_OK, self.payload())
        self.assertEqual(self.payload()["status"], "planned")
        self.assertEqual(
            self.payload()["manifests"][0]["artifacts"][0]["name"],
            f"macos-{VERSION}-arm64.zip",
        )
        code = self.main(self.fixture.args("execute", **{"--hooks": None}))
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual(self.payload()["code"], "hooks-required")

    def test_config_targets_and_hooks_must_agree(self):
        self.fixture.write_config(
            "release:\n"
            "  hooks: hooks.json\n"
            "  targets:\n"
            "    - id: another-target\n"
            "      adapter: apple\n"
        )
        code = self.main(self.fixture.args("plan"))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.fake.calls, [])

    def test_a_named_secret_that_the_job_does_not_hold_fails(self):
        code = self.main(
            self.fixture.args("plan", **{"--secret-env": "DEFINITELY_ABSENT_SECRET"})
        )
        self.assertEqual(code, cli.EXIT_FAILED)

    def test_an_unknown_event_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            cli.main(self.fixture.args("plan", **{"--event": "pull_request"}))


if __name__ == "__main__":
    unittest.main()
