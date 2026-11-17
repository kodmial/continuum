"""Downstream sync: retry only what the command called temporary."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from continuum.release import contract
from continuum.release.contract import ManifestBuilder, TYPE_ARCHIVE
from continuum.release.publishers import CommandSync, destination_names


def _manifest(directory):
    path = os.path.join(directory, "widget.tar.gz")
    with open(path, "wb") as handle:
        handle.write(b"payload")
    builder = ManifestBuilder(
        target="linux", adapter="fixture", source_sha="a" * 40, version="1.2.3"
    )
    builder.record("widget.tar.gz", path, TYPE_ARCHIVE)
    return builder.build()


def request(dry_run=False):
    directory = tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None)
    return contract.PublishRequest(
        destination="homebrew",
        tag="v1.2.3",
        version="1.2.3",
        source_sha="a" * 40,
        manifests=(_manifest(directory),),
        key="sync-key",
        dry_run=dry_run,
    )


def completed(code, out=""):
    return subprocess.CompletedProcess([], code, out, "")


class SyncTests(unittest.TestCase):
    def make(self, side_effect, **options):
        runner = side_effect if callable(side_effect) else mock.Mock(side_effect=side_effect)
        return CommandSync(
            "homebrew",
            ["bash", "tap.sh"],
            runner=runner,
            sleep=lambda _seconds: None,
            **options,
        )

    def test_a_plan_writes_nothing(self):
        runner = mock.Mock()
        result = self.make(runner).publish(request(dry_run=True))
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(result.code, "dry-run")
        runner.assert_not_called()

    def test_a_temporary_exit_is_retried_then_succeeds(self):
        runner = mock.Mock(side_effect=[completed(75), completed(0)])
        result = self.make(runner, attempts=3).publish(request())
        self.assertEqual(result.outcome, contract.PUBLISHED)
        self.assertEqual(result.detail("attempts"), "2")
        self.assertEqual(runner.call_count, 2)

    def test_a_persistent_temporary_failure_stays_retryable(self):
        runner = mock.Mock(return_value=completed(75))
        result = self.make(runner, attempts=2).publish(request())
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertTrue(result.retryable)
        self.assertEqual(result.detail("attempts"), "2")

    def test_an_ordinary_failure_is_not_retried(self):
        runner = mock.Mock(return_value=completed(2, "bad template"))
        result = self.make(runner, attempts=3).publish(request())
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertFalse(result.retryable)
        self.assertEqual(runner.call_count, 1)

    def test_the_environment_carries_the_release_identity_not_the_job(self):
        seen = {}

        def runner(argv, **kwargs):
            seen.update(kwargs["env"])
            return completed(0)

        self.make(runner).publish(request())
        self.assertEqual(seen["CONTINUUM_RELEASE_TAG"], "v1.2.3")
        self.assertEqual(seen["CONTINUUM_SYNC_KEY"], "sync-key")
        self.assertNotIn("GITHUB_TOKEN", seen)

    def test_a_missing_tool_is_a_permanent_failure(self):
        def runner(argv, **kwargs):
            raise FileNotFoundError("bash")

        result = self.make(runner).publish(request())
        self.assertEqual(result.code, "sync-tool-missing")
        self.assertFalse(result.retryable)

    def test_two_destinations_may_not_share_a_name(self):
        first = CommandSync("homebrew", ["true"], runner=mock.Mock())
        second = CommandSync("homebrew", ["true"], runner=mock.Mock())
        with self.assertRaises(Exception):
            destination_names((first, second))


if __name__ == "__main__":
    unittest.main()
