#!/usr/bin/env python3
"""Regression suite for point-of-use OpenCode binary identity.

runtime-lab #136
----------------
The optimized install verifies a release binary, then makes it reachable by
name::

    sha256sum -c "$ASSET_NAME.sha256"
    ... compare against opencode_release_sha256 / _version / _source_sha ...
    ln -sf "$DIR/$ASSET_NAME" "$DIR/opencode"
    echo "$DIR" >> "$GITHUB_PATH"
    # ... later, in another step:
    opencode run --auto ...

Every check in between is real. None of them is about the thing that actually
executes.

``$GITHUB_PATH`` **appends**. The verified directory therefore ends up *last*
on ``PATH``, and ``PATH`` resolution takes the first match. If anything earlier
on ``PATH`` provides a file called ``opencode`` -- a repository directory, a
restored tool cache, a previous install path, an artifact extracted from the
run's own untrusted inputs -- that file runs, and it runs *instead of* the
verified binary. The digest checks all passed. The step summary would still
report the verified SHA-256. The model would be driven by an executable nobody
looked at.

The gap is not in the verification, it is between the verification and the
`PATH` lookup, and no amount of checking the downloaded artifact closes it.

What these tests pin down
-------------------------
* ``install`` records the digest of the file ``PATH`` actually resolves to, not
  the file that was downloaded (:class:`InstallTests`);
* a shadowing executable is detected and refused, not reported after the fact
  (:class:`ShadowingTests`);
* ``verify`` re-reads the recorded identity and fails when the executable has
  changed underneath it (:class:`VerificationTests`);
* both agent workflows verify at the point of use, and record at install
  (:class:`WorkflowWachingTests`).

Run with::

    python3 -m unittest discover -s .github/tests -p 'test_*.py'
"""

import hashlib
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / ".github" / "scripts"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

sys.path.insert(0, str(SCRIPT_DIR))

import opencode_runtime  # noqa: E402

RUNTIME_SCRIPT = SCRIPT_DIR / "opencode_runtime.py"
AGENT_WORKFLOW = "opencode.yml"
CONSUMER_AGENT_WORKFLOW = "consumer-opencode.yml"


def _digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _executable(directory, name, digest_note=""):
    """A runnable file whose digest is this test's to predict.

    ``digest_note`` changes the bytes, so two fixtures written to the same path
    in the same test have different digests -- which is what makes "replaced
    after install" a real condition rather than a comment.
    """
    path = pathlib.Path(directory) / name
    path.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "agent fixture{digest_note}"
            """
        ).format(digest_note=digest_note),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class RuntimeHarness(unittest.TestCase):
    """A PATH with a real executable on it, and a real GITHUB_STATE file."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.state = self.root / "github-state"
        self.state.write_text("", encoding="utf-8")

    def _run(self, *args, path=None, state=None):
        # PATH is composed rather than replaced. Replacing it would drop the
        # directory holding `python3`, so the command would fail for an
        # unrelated reason and the tests would prove nothing about the runtime.
        environment = dict(os.environ)
        environment["GITHUB_STATE"] = str(self.state if state is None else state)
        if path is not None:
            environment["PATH"] = os.pathsep.join([str(path), os.environ["PATH"]])
        return subprocess.run(
            [sys.executable, str(RUNTIME_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=environment,
        )


# --------------------------------------------------------------------------- #
# install: the identity is the one that will run
# --------------------------------------------------------------------------- #


class InstallTests(RuntimeHarness):
    def test_install_records_the_digest_of_what_path_resolves_to(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = _executable(directory, "opencode")
            result = self._run("install", path=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(_digest(binary), self.state.read_text(encoding="utf-8"))

    def test_install_compares_against_the_verified_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = _executable(directory, "opencode")
            result = self._run(
                "install", "--verified-sha256", _digest(binary), path=directory
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_fails_when_the_resolved_binary_is_not_the_verified_one(self):
        # The whole point of the change. The release check approved one digest;
        # PATH resolves to another. The run must stop here, not after the model
        # has already been driven by the wrong binary.
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            result = self._run(
                "install", "--verified-sha256", "0" * 64, path=directory
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("shadows the verified binary", result.stderr)
            self.assertIn("::error::", result.stderr)

    def test_a_truncated_digest_is_refused_as_an_identity(self):
        # A prefix is not an identity: an attacker supplying a digest picks the
        # prefix, so a truncated digest verifies whatever it happens to match.
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            result = self._run("install", "--verified-sha256", "abc123", path=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("is not a SHA-256", result.stderr)

    def _run_bare_path(self, *args, directory):
        """Run with a PATH holding only ``directory``.

        Prepending would not do: a real ``opencode`` may be installed later on
        the inherited PATH, and the test would pass for the wrong reason. The
        interpreter is invoked by absolute path, so a one-entry PATH is enough
        to run it.
        """
        environment = dict(os.environ)
        environment["GITHUB_STATE"] = str(self.state)
        environment["PATH"] = str(directory)
        return subprocess.run(
            [sys.executable, str(RUNTIME_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_a_missing_executable_fails_rather_than_passing_vacuously(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_bare_path("install", directory=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot be identified", result.stderr)

    def test_a_non_executable_file_named_opencode_is_not_an_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "opencode"
            path.write_text("not a program\n", encoding="utf-8")
            path.chmod(0o644)
            result = self._run_bare_path("install", directory=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot be identified", result.stderr)

    def test_install_without_github_state_fails_instead_of_recording_nowhere(self):
        # An identity written to nowhere is an identity that is never enforced.
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            result = self._run("install", path=directory, state="")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.returncode, 1)
            self.assertIn("GITHUB_STATE is not set", result.stderr)


# --------------------------------------------------------------------------- #
# Shadowing: the gap the release check leaves open
# --------------------------------------------------------------------------- #


class ShadowingTests(RuntimeHarness):
    def test_an_earlier_path_entry_wins_and_is_the_one_checked(self):
        # `$GITHUB_PATH` appends, so the verified binary is last and loses. The
        # recorded identity has to be the shadowing one, because that is the one
        # that would execute.
        verified_dir = self.root / "verified"
        shadow_dir = self.root / "shadow"
        verified_dir.mkdir()
        shadow_dir.mkdir()
        verified = _executable(str(verified_dir), "opencode", digest_note=" verified")
        shadow = _executable(str(shadow_dir), "opencode", digest_note=" shadow")

        path = os.pathsep.join([str(shadow_dir), str(verified_dir)])
        result = self._run("install", path=path)
        self.assertEqual(result.returncode, 0, result.stderr)

        recorded = self.state.read_text(encoding="utf-8")
        self.assertIn(_digest(shadow), recorded)
        self.assertNotIn(_digest(verified), recorded)

    def test_shadowing_is_refused_when_the_verified_digest_is_known(self):
        verified_dir = self.root / "verified"
        shadow_dir = self.root / "shadow"
        verified_dir.mkdir()
        shadow_dir.mkdir()
        verified = _executable(str(verified_dir), "opencode", digest_note=" verified")
        _executable(str(shadow_dir), "opencode", digest_note=" shadow")

        path = os.pathsep.join([str(shadow_dir), str(verified_dir)])
        result = self._run(
            "install", "--verified-sha256", _digest(verified), path=path
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("shadows the verified binary", result.stderr)

    def test_a_symlink_is_resolved_to_the_bytes_that_would_execute(self):
        # The install makes the binary reachable under a second name. The
        # identity is the target's bytes, not the link's path.
        target_dir = self.root / "target"
        target_dir.mkdir()
        target = _executable(str(target_dir), "asset-name")
        link_dir = self.root / "link"
        link_dir.mkdir()
        (link_dir / "opencode").symlink_to(target)

        result = self._run("install", path=str(link_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(_digest(target), self.state.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# verify: enforced at the point of use
# --------------------------------------------------------------------------- #


class VerificationTests(RuntimeHarness):
    def test_an_unchanged_binary_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verified", result.stderr)

    def test_a_binary_replaced_after_install_fails(self):
        # The window this closes is every step between install and invocation --
        # including the agent step itself, which has the write-capable token and
        # an untrusted working tree.
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)

            _executable(directory, "opencode", digest_note=" replaced")
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("changed after verification", result.stderr)
            self.assertIn("::error::", result.stderr)

    def test_a_binary_swapped_in_by_path_order_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)

            other = self.root / "other"
            other.mkdir()
            _executable(str(other), "opencode", digest_note=" swapped")
            result = self._run("verify", path=os.pathsep.join([str(other), directory]))
            self.assertEqual(result.returncode, 1)
            self.assertIn("changed after verification", result.stderr)

    def test_verify_without_a_recorded_identity_refuses_to_run(self):
        # "I could not find a record" must not read as "everything is fine".
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot be checked", result.stderr)

    def test_a_malformed_digest_in_the_record_is_rejected_rather_than_ignored(self):
        # The record is the enforcement input, so it is read defensively. A
        # digest that is not a full SHA-256 is a failure: falling back to
        # "no digest to compare" would turn a corrupted record into a pass.
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)

            self.state.write_text(
                "{key}<<END\npath={path}\nsha256=not-a-digest\nEND\n".format(
                    key=opencode_runtime.STATE_KEY,
                    path=pathlib.Path(directory) / "opencode",
                ),
                encoding="utf-8",
            )
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 1)
            self.assertIn("is not a SHA-256", result.stderr)

    def test_an_empty_record_cannot_authorize_anything(self):
        # A record that parses but carries no digest is the same as a missing
        # one, and it must not read as "nothing to check, carry on".
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)

            self.state.write_text(
                "{key}<<END\n\nEND\n".format(key=opencode_runtime.STATE_KEY),
                encoding="utf-8",
            )
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 1)

    def test_a_truncated_state_record_is_rejected_rather_than_read_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            _executable(directory, "opencode")
            self.assertEqual(self._run("install", path=directory).returncode, 0)

            text = self.state.read_text(encoding="utf-8")
            self.state.write_text(text[: len(text) // 2], encoding="utf-8")
            result = self._run("verify", path=directory)
            self.assertEqual(result.returncode, 1)


# --------------------------------------------------------------------------- #
# Both planes, at the point of use
# --------------------------------------------------------------------------- #


def _steps(workflow):
    doc = yaml.safe_load((WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            yield step


def _invocations(workflow):
    """Every step whose run body starts the agent."""
    return [
        step
        for step in _steps(workflow)
        if "opencode run" in (step.get("run") or "")
    ]


class WorkflowWachingTests(unittest.TestCase):
    """The guard only counts if it is on the path the agent actually takes."""

    def test_both_planes_record_an_identity_when_they_install(self):
        for workflow in (AGENT_WORKFLOW, CONSUMER_AGENT_WORKFLOW):
            installs = [
                step
                for step in _steps(workflow)
                if "opencode_runtime.py install" in (step.get("run") or "")
            ]
            with self.subTest(workflow=workflow):
                self.assertTrue(
                    installs,
                    "an unrecorded identity cannot be enforced later",
                )

    def test_every_agent_invocation_is_preceded_by_a_verification(self):
        for workflow in (AGENT_WORKFLOW, CONSUMER_AGENT_WORKFLOW):
            for step in _invocations(workflow):
                run = step.get("run") or ""
                with self.subTest(workflow=workflow, step=step.get("name")):
                    self.assertIn("opencode_runtime.py verify", run)
                    # Before the invocation, not after. Verifying afterwards
                    # would report on a run that already happened.
                    self.assertLess(
                        run.index("opencode_runtime.py verify"),
                        run.index("opencode run"),
                    )

    def test_the_optimized_install_compares_against_the_verified_digest(self):
        # `install` without `--verified-sha256` records whatever is on PATH,
        # which detects a later swap but cannot notice a shadowed install. The
        # optimized path knows the digest it approved and must pass it.
        for step in _steps(CONSUMER_AGENT_WORKFLOW):
            run = step.get("run") or ""
            if "opencode_runtime.py install" not in run:
                continue
            if "BINARY_SHA256" not in run:
                continue
            with self.subTest(step=step.get("name")):
                self.assertIn("--verified-sha256", run)
                self.assertIn("$BINARY_SHA256", run)

    def test_the_runtime_is_standard_library_only(self):
        # It runs on a stock runner between install and invocation, with no
        # dependency installation step of its own.
        source = RUNTIME_SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("import yaml", "import requests", "import numpy"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
