"""Tests for repository-authoritative verification evidence handling."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from continuum import verification

KEY_ENV = verification.ATTESTATION_KEY_ENV
KEY_FILE_ENV = verification.ATTESTATION_KEY_FILE_ENV
KEY_VALUE = "runner-controlled-secret"
BASE = "1" * 40
SRC = Path(__file__).resolve().parents[1] / "src"


def _key_env(directory: Path, value: str = KEY_VALUE, name: str = "attestation.key") -> dict:
    path = Path(directory) / name
    path.write_text(value, encoding="utf-8")
    return {KEY_FILE_ENV: str(path)}


def _contract(command: str = "true") -> verification.VerificationContract:
    return verification.VerificationContract(
        state="configured",
        source="ci-caller",
        commands=(
            verification.VerificationCommand(
                name="test", command=command, source="ci-caller:test_command"
            ),
        ),
    )


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class EvidenceAttestationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / verification.EVIDENCE_FILE_NAME
        key_directory = tempfile.TemporaryDirectory()
        self.addCleanup(key_directory.cleanup)
        self.key_env = _key_env(Path(key_directory.name))

    def _evidence(self, command: str = "true") -> dict:
        contract = _contract(command).as_dict()
        return {
            "schema_version": verification.SCHEMA_VERSION,
            "contract": contract,
            "baseline": {
                "base_sha": BASE,
                "state": "passed",
                "contract": contract,
                "environment_fingerprint": "env",
            },
        }

    def test_signed_evidence_round_trips(self):
        verification._save_evidence(self.path, self._evidence(), self.key_env)
        loaded = verification._load_evidence(self.path, self.key_env)
        self.assertEqual(loaded["baseline"]["base_sha"], BASE)
        self.assertEqual(
            loaded["attestation"]["algorithm"],
            verification.ATTESTATION_ALGORITHM,
        )

    def test_verification_commands_do_not_receive_attestation_key(self):
        command = (
            "if [ -n \"${CONTINUUM_VERIFICATION_ATTESTATION_KEY-}\" ] || "
            "[ -n \"${CONTINUUM_VERIFICATION_ATTESTATION_KEY_FILE-}\" ]; "
            "then echo leaked; exit 1; fi"
        )
        result = verification.run_command_set(
            _contract(command), self.root, environ=self.key_env
        )
        self.assertEqual(result["state"], "passed")
        self.assertEqual(result["commands"][0]["exit_code"], 0)
        self.assertNotIn("runner-controlled-secret", json.dumps(result))
        child = verification._child_environment(self.key_env)
        self.assertNotIn(KEY_ENV, child)
        self.assertNotIn(KEY_FILE_ENV, child)
        # The key file is removed before the command runs and is never
        # restored: a same-uid survivor spawned by the command must not read it
        # after the engine exits.
        key_path = Path(self.key_env[KEY_FILE_ENV])
        self.assertFalse(key_path.exists())
        # The engine keeps the key in memory for the rest of the process.
        self.assertEqual(
            verification._attestation_key(self.key_env, worktree=self.root),
            KEY_VALUE.encode("utf-8"),
        )

    def test_attestation_key_value_in_the_engine_environment_fails_closed(self):
        with self.assertRaises(verification.VerificationError) as caught:
            verification._attestation_key({KEY_ENV: KEY_VALUE})
        self.assertIn(KEY_FILE_ENV, str(caught.exception))

    def test_tampered_contract_is_rejected(self):
        verification._save_evidence(self.path, self._evidence(), self.key_env)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["baseline"]["state"] = "failed"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification._load_evidence(self.path, self.key_env)

    def test_missing_attestation_is_rejected_when_key_is_set(self):
        self.path.write_text(json.dumps(self._evidence()), encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification._load_evidence(self.path, self.key_env)

    def test_unkeyed_evidence_is_readable_without_attestation(self):
        verification._save_evidence(self.path, self._evidence(), {})
        loaded = verification._load_evidence(self.path, {})
        self.assertNotIn("attestation", loaded)

    def test_unattested_passed_verifier_requires_a_fresh_session(self):
        data = self._evidence()
        data["verifier"] = {
            "state": "passed",
            "contract_fingerprint": data["baseline"]["contract"]["fingerprint"],
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(verification.verifier_required(self.path, {}))

    def test_attested_passed_verifier_can_be_reused(self):
        data = self._evidence()
        data["verifier"] = {
            "state": "passed",
            "contract_fingerprint": data["baseline"]["contract"]["fingerprint"],
        }
        verification._save_evidence(self.path, data, self.key_env)
        self.assertFalse(
            verification.verifier_required(self.path, self.key_env)
        )

    def test_sequential_cli_processes_must_reprovision_the_consumed_key(self):
        # Contract: every verification CLI invocation that reads the key
        # consumes the on-disk copy. The workflow must re-provision it before
        # the next verification call (and never during an agent session).
        verification._save_evidence(self.path, self._evidence(), self.key_env)
        key_path = Path(self.key_env[KEY_FILE_ENV])
        # Saving evidence reads and consumes the key, so re-provision it before
        # the first CLI process.
        verification.provision_attestation_key(KEY_VALUE, self.key_env)
        self.assertTrue(key_path.exists())
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (KEY_ENV, KEY_FILE_ENV)
        }
        env.update(self.key_env)
        env["PYTHONPATH"] = str(SRC)
        command = [
            sys.executable,
            "-m",
            "continuum.verification",
            "summary",
            "--evidence",
            str(self.path),
        ]
        provision_command = [
            sys.executable,
            "-m",
            "continuum.verification",
            "provision-key",
            "--repo",
            str(self.root),
        ]

        def provision() -> None:
            completed = subprocess.run(
                provision_command,
                input=KEY_VALUE,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(key_path.exists())

        # The first invocation verifies and leaves no key file on disk.
        first = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("baseline: passed", first.stdout)
        self.assertFalse(key_path.exists())

        # A second invocation without re-provisioning fails closed with an
        # explicit error instead of silently verifying without an attestation.
        second = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 3)
        self.assertIn("::error::", second.stderr)
        self.assertIn(
            "the verification attestation key file is unavailable", second.stderr
        )
        self.assertFalse(key_path.exists())

        # After provision-key re-materializes the key, verification succeeds
        # again.
        provision()
        third = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(third.stdout, first.stdout)
        self.assertFalse(key_path.exists())

        # Tampered worktree evidence is still rejected with the key present:
        # only the signature check makes the evidence trustworthy.
        provision()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["baseline"]["state"] = "failed"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        tampered = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertNotEqual(tampered.returncode, 0)
        self.assertIn("attestation does not match", tampered.stderr)


class ChildEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        key_directory = tempfile.TemporaryDirectory()
        self.addCleanup(key_directory.cleanup)
        self.key_directory = Path(key_directory.name)
        self.key_env = _key_env(self.key_directory)

    def test_parent_side_evidence_signing_still_uses_the_key(self):
        path = self.root / verification.EVIDENCE_FILE_NAME
        verification._save_evidence(
            path,
            {
                "schema_version": verification.SCHEMA_VERSION,
                "contract": _contract().as_dict(),
            },
            self.key_env,
        )
        self.assertIn("attestation", json.loads(path.read_text(encoding="utf-8")))
        loaded = verification._load_evidence(path, self.key_env)
        self.assertEqual(
            loaded["attestation"]["algorithm"],
            verification.ATTESTATION_ALGORITHM,
        )
        other = _key_env(self.key_directory, "different-secret", "other.key")
        with self.assertRaises(verification.VerificationError):
            verification._load_evidence(path, other)

    def test_key_file_is_removed_before_a_command_runs_and_not_restored(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        result = verification.run_command_set(
            _contract("true"), self.root, environ=self.key_env
        )
        self.assertEqual(result["state"], "passed")
        # The file is removed while a repository-controlled command runs and is
        # never written back, so a survivor process cannot read it later.
        self.assertFalse(key_path.exists())
        # A second command set in the same process reuses the cached key and
        # keeps the file absent instead of failing or recreating it.
        second = verification.run_command_set(
            _contract("true"), self.root, environ=self.key_env
        )
        self.assertEqual(second["state"], "passed")
        self.assertFalse(key_path.exists())

    def test_a_failed_removal_fails_closed(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        original_unlink = Path.unlink

        def deny_unlink(path, *args, **kwargs):
            if path == key_path:
                raise PermissionError("permission denied")
            return original_unlink(path, *args, **kwargs)

        # The key is cached before removal is attempted, so a fresh path in the
        # cache is not needed; run_command_set must stop rather than run a
        # repository-controlled command while the key is still readable.
        with mock.patch.object(Path, "unlink", deny_unlink):
            with self.assertRaises(verification.VerificationError) as caught:
                verification.run_command_set(
                    _contract("true"), self.root, environ=self.key_env
                )
        self.assertIn("cannot be removed", str(caught.exception))
        self.assertTrue(key_path.exists())

    def test_a_concurrently_removed_key_still_runs_the_command(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        # Another consumer removes the file between this process's read and its
        # removal; the cached value is authoritative and the command still runs.
        original_read_text = Path.read_text

        def read_then_remove(path, *args, **kwargs):
            text = original_read_text(path, *args, **kwargs)
            if path == key_path:
                key_path.unlink()
            return text

        with mock.patch.object(Path, "read_text", read_then_remove):
            result = verification.run_command_set(
                _contract("true"), self.root, environ=self.key_env
            )
        self.assertEqual(result["state"], "passed")
        self.assertFalse(key_path.exists())

    def test_a_symlinked_key_target_is_removed_too(self):
        target = self.key_directory / "real.key"
        target.write_text(KEY_VALUE, encoding="utf-8")
        link = self.key_directory / "linked.key"
        os.symlink(target, link)
        result = verification.run_command_set(
            _contract("true"), self.root, environ={KEY_FILE_ENV: str(link)}
        )
        self.assertEqual(result["state"], "passed")
        # Removing only the link would leave the secret readable at its target.
        self.assertFalse(link.exists())
        self.assertFalse(target.exists())

    def test_provisioning_accepts_a_racing_writer_with_the_same_key(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        key_path.unlink()

        def raced_link(source, target, *args, **kwargs):
            Path(target).write_text(KEY_VALUE, encoding="utf-8")
            raise FileExistsError("already exists")

        with mock.patch.object(verification.os, "link", raced_link):
            result = verification.provision_attestation_key(
                KEY_VALUE, self.key_env, worktree=self.root
            )
        self.assertEqual(result, key_path)
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)

    def test_provisioning_refuses_a_racing_writer_with_a_different_key(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        key_path.unlink()

        def raced_link(source, target, *args, **kwargs):
            Path(target).write_text("a-different-key", encoding="utf-8")
            raise FileExistsError("already exists")

        with mock.patch.object(verification.os, "link", raced_link):
            with self.assertRaises(verification.VerificationError) as caught:
                verification.provision_attestation_key(
                    KEY_VALUE, self.key_env, worktree=self.root
                )
        self.assertIn("refusing to replace", str(caught.exception))
        self.assertEqual(
            key_path.read_text(encoding="utf-8"), "a-different-key"
        )

    def test_provisioning_succeeds_when_the_temporary_file_cannot_be_removed(self):
        target = self.key_directory / "cleanup.key"
        original_unlink = Path.unlink

        def deny_temporary(path, *args, **kwargs):
            if str(path).endswith(".provision"):
                raise PermissionError("busy")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", deny_temporary):
            result = verification.provision_attestation_key(
                KEY_VALUE, {KEY_FILE_ENV: str(target)}, worktree=self.root
            )
        self.assertEqual(result, target)
        self.assertEqual(target.read_text(encoding="utf-8"), KEY_VALUE)

    def test_provision_key_cli_reads_the_key_from_standard_input(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        key_path.unlink()
        stdin = mock.Mock()
        stdin.buffer.read.return_value = KEY_VALUE.encode("utf-8")
        with mock.patch.dict(os.environ, self.key_env, clear=False), mock.patch.object(
            sys, "stdin", stdin
        ):
            self.assertEqual(
                verification.main(["provision-key", "--repo", str(self.root)]), 0
            )
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)

    def test_reprovisioning_restores_a_consumed_key_from_stdin(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        verification.run_command_set(
            _contract("true"), self.root, environ=self.key_env
        )
        self.assertFalse(key_path.exists())
        verification.provision_attestation_key(
            KEY_VALUE.encode("utf-8"), self.key_env, worktree=self.root
        )
        self.assertTrue(key_path.exists())
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)
        self.assertEqual(
            key_path.stat().st_mode & 0o777,
            0o600,
        )
        # Provisioning the same key again is an idempotent no-op.
        verification.provision_attestation_key(
            KEY_VALUE, self.key_env, worktree=self.root
        )
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)

    def test_provisioning_refuses_to_replace_a_different_key(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        with self.assertRaises(verification.VerificationError) as caught:
            verification.provision_attestation_key(
                "a-different-key", self.key_env, worktree=self.root
            )
        self.assertIn("refusing to replace", str(caught.exception))
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)

    def test_provisioning_rejects_an_empty_key(self):
        (self.key_directory / "empty.key").write_text("  \n", encoding="utf-8")
        with self.assertRaises(verification.VerificationError) as caught:
            verification.provision_attestation_key(
                "   \n",
                {KEY_FILE_ENV: str(self.key_directory / "empty.key")},
                worktree=self.root,
            )
        self.assertIn("empty", str(caught.exception))

    def test_provisioning_requires_a_configured_key_file_path(self):
        with self.assertRaises(verification.VerificationError) as caught:
            verification.provision_attestation_key(KEY_VALUE, {})
        self.assertIn(KEY_FILE_ENV, str(caught.exception))

    def test_provisioning_rejects_a_path_inside_the_worktree(self):
        key_path = self.root / "attestation.key"
        with self.assertRaises(verification.VerificationError):
            verification.provision_attestation_key(
                KEY_VALUE, {KEY_FILE_ENV: str(key_path)}, worktree=self.root
            )
        self.assertFalse(key_path.exists())

    def test_provisioning_reports_a_write_failure(self):
        write_directory = self.key_directory / "write"
        write_directory.mkdir()
        key_path = write_directory / "new.key"
        with mock.patch.object(
            verification.os, "link", side_effect=OSError("disk full")
        ):
            with self.assertRaises(verification.VerificationError) as caught:
                verification.provision_attestation_key(
                    KEY_VALUE,
                    {KEY_FILE_ENV: str(key_path)},
                    worktree=self.root,
                )
        self.assertIn("cannot be provisioned", str(caught.exception))
        self.assertFalse(key_path.exists())
        # The temporary file used for the atomic write is cleaned up.
        self.assertEqual(
            [item.name for item in write_directory.iterdir()], []
        )

    def test_key_file_inside_the_worktree_is_rejected(self):
        key_path = self.root / "attestation.key"
        key_path.write_text(KEY_VALUE, encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification.run_command_set(
                _contract(), self.root, environ={KEY_FILE_ENV: str(key_path)}
            )
        self.assertTrue(key_path.exists())

    def test_missing_key_file_fails_closed(self):
        missing = self.key_directory / "missing.key"
        with self.assertRaises(verification.VerificationError):
            verification._attestation_key({KEY_FILE_ENV: str(missing)})

    def test_key_file_read_failure_leaves_the_key_file_in_place(self):
        key_path = Path(self.key_env[KEY_FILE_ENV])
        original_read_text = Path.read_text

        def deny_key_file_read(path, *args, **kwargs):
            if path == key_path:
                raise PermissionError("permission denied")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny_key_file_read):
            with self.assertRaises(verification.VerificationError) as caught:
                verification._attestation_key({KEY_FILE_ENV: str(key_path)})
        self.assertIn("unavailable", str(caught.exception))
        self.assertTrue(key_path.exists())
        self.assertEqual(key_path.read_text(encoding="utf-8"), KEY_VALUE)


class ContractResolutionAtRefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Test")
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        self.caller = workflows / "ci.yml"
        self.caller.write_text(
            "name: CI\n"
            "on: [push]\n"
            "jobs:\n"
            "  validate:\n"
            "    uses: kodmial/continuum/.github/workflows/"
            "continuum-validation.yml@main\n"
            "    with:\n"
            '      test_command: "true"\n',
            encoding="utf-8",
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")

    def test_ref_reads_the_base_commit_instead_of_the_worktree(self):
        self.caller.write_text(
            "name: CI\n"
            "on: [push]\n"
            "jobs:\n"
            "  validate:\n"
            "    uses: kodmial/continuum/.github/workflows/"
            "continuum-validation.yml@main\n"
            "    with:\n"
            '      test_command: "false"\n',
            encoding="utf-8",
        )
        from_worktree = verification.resolve_contract(self.root, {})
        from_base = verification.resolve_contract(self.root, {}, ref=self.base)
        self.assertEqual(from_worktree.commands[0].command, "false")
        self.assertEqual(from_base.commands[0].command, "true")


class FinalPhaseTrustTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "file.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        (self.root / "file.txt").write_text("changed\n", encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "change")
        self.path = self.root / verification.EVIDENCE_FILE_NAME
        self.env = {"CONTINUUM_TEST_COMMAND": "true"}
        key_directory = tempfile.TemporaryDirectory()
        self.addCleanup(key_directory.cleanup)
        self.key_env = _key_env(Path(key_directory.name))

    def _evidence(
        self,
        baseline_contract: dict,
        *,
        data_contract: dict | None = None,
        forged_final: bool = False,
        environment: str | None = None,
    ) -> dict:
        data = {
            "schema_version": verification.SCHEMA_VERSION,
            "contract": data_contract or baseline_contract,
            "baseline": {
                "base_sha": self.base,
                "state": "passed",
                "contract": baseline_contract,
                "environment_fingerprint": environment
                or verification.environment_fingerprint(self.root, self.env),
                "commands": [{"name": "test", "exit_code": 0}],
            },
        }
        if forged_final:
            data["final"] = {
                "state": "passed",
                "contract_fingerprint": baseline_contract["fingerprint"],
                "comparison": {"accepted": True, "result": "forged"},
            }
        return data

    def test_unattested_final_is_never_reused_and_contract_is_reread_at_base(self):
        real = verification.resolve_contract(self.root, self.env)
        forged = _contract("false").as_dict()
        self.path.write_text(
            json.dumps(
                self._evidence(
                    real.as_dict(),
                    data_contract=forged,
                    forged_final=True,
                )
            ),
            encoding="utf-8",
        )
        data, comparison = verification.run_final(
            self.root,
            self.path,
            self.base,
            environ=self.env,
            resume_stage="tests-passed",
        )
        self.assertNotEqual(comparison["result"], "forged")
        self.assertEqual(comparison["result"], "passed")
        self.assertTrue(comparison["accepted"])
        self.assertEqual(data["final"]["commands"][0]["command"], "true")
        self.assertEqual(data["contract"]["fingerprint"], real.fingerprint)

    def test_unattested_forged_baseline_contract_fails_closed(self):
        forged = _contract("false").as_dict()
        self.path.write_text(
            json.dumps(self._evidence(forged)),
            encoding="utf-8",
        )
        with self.assertRaises(verification.VerificationError):
            verification.run_final(
                self.root, self.path, self.base, environ=self.env
            )

    def test_attested_final_is_reused_on_an_explicit_resume(self):
        contract = verification.resolve_contract(self.root, self.env).as_dict()
        data = self._evidence(contract, forged_final=True, environment="stale")
        verification._save_evidence(self.path, data, self.key_env)
        env = dict(self.env, **self.key_env)
        _, comparison = verification.run_final(
            self.root,
            self.path,
            self.base,
            environ=env,
            resume_stage="tests-passed",
        )
        self.assertEqual(comparison["result"], "forged")

    def test_attested_final_is_not_reused_for_a_normal_run(self):
        contract = verification.resolve_contract(self.root, self.env).as_dict()
        data = self._evidence(contract, forged_final=True)
        verification._save_evidence(self.path, data, self.key_env)
        env = dict(self.env, **self.key_env)
        _, comparison = verification.run_final(
            self.root, self.path, self.base, environ=env
        )
        self.assertEqual(comparison["result"], "passed")


class BaselineReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "file.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.path = self.root / verification.EVIDENCE_FILE_NAME
        self.env = {"CONTINUUM_TEST_COMMAND": "true"}
        key_directory = tempfile.TemporaryDirectory()
        self.addCleanup(key_directory.cleanup)
        self.key_env = _key_env(Path(key_directory.name))

    def _evidence(self) -> dict:
        contract = verification.resolve_contract(self.root, self.env).as_dict()
        return {
            "schema_version": verification.SCHEMA_VERSION,
            "contract": contract,
            "baseline": {
                "base_sha": self.base,
                "state": "passed",
                "contract": contract,
                "environment_fingerprint": "stale",
                "commands": [{"name": "test", "exit_code": 0}],
            },
        }

    def test_unattested_baseline_is_rebuilt(self):
        self.path.write_text(json.dumps(self._evidence()), encoding="utf-8")
        data, reused = verification.ensure_baseline(
            self.root, self.path, self.base, environ=self.env
        )
        self.assertFalse(reused)
        self.assertEqual(data["baseline"]["commands"][0]["command"], "true")

    def test_attested_baseline_is_reused(self):
        verification._save_evidence(self.path, self._evidence(), self.key_env)
        env = dict(self.env, **self.key_env)
        data, reused = verification.ensure_baseline(
            self.root, self.path, self.base, environ=env
        )
        self.assertTrue(reused)
        self.assertEqual(data["baseline"]["base_sha"], self.base)


class KeyConsumptionTests(unittest.TestCase):
    """Every key-reading path consumes the on-disk copy."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "work"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "file.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.path = self.root / verification.EVIDENCE_FILE_NAME
        self.env = {"CONTINUUM_TEST_COMMAND": "true"}
        key_directory = tempfile.TemporaryDirectory()
        self.addCleanup(key_directory.cleanup)
        self.key_directory = Path(key_directory.name)
        self.key_env = _key_env(self.key_directory)

    def _key_path(self) -> Path:
        return Path(self.key_env[KEY_FILE_ENV])

    def _write_signed_evidence(self) -> dict:
        contract = verification.resolve_contract(self.root, self.env).as_dict()
        data = {
            "schema_version": verification.SCHEMA_VERSION,
            "contract": contract,
            "baseline": {
                "base_sha": self.base,
                "state": "passed",
                "contract": contract,
                "environment_fingerprint": verification.environment_fingerprint(
                    self.root, self.env
                ),
                "commands": [{"name": "test", "exit_code": 0}],
            },
        }
        # Sign without reading the key file so the read path under test is the
        # one that consumes it.
        verification._sign_evidence(data, KEY_VALUE.encode("utf-8"))
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def test_load_evidence_consumes_the_key_file(self):
        self._write_signed_evidence()
        loaded = verification._load_evidence(self.path, self.key_env)
        self.assertEqual(loaded["baseline"]["base_sha"], self.base)
        self.assertFalse(self._key_path().exists())

    def test_save_evidence_consumes_the_key_file(self):
        verification._save_evidence(
            self.path,
            {"schema_version": verification.SCHEMA_VERSION},
            self.key_env,
        )
        self.assertFalse(self._key_path().exists())

    def test_needs_verifier_consumes_the_key_file(self):
        data = self._write_signed_evidence()
        data["verifier"] = {
            "state": "passed",
            "contract_fingerprint": data["baseline"]["contract"]["fingerprint"],
        }
        verification._sign_evidence(data, KEY_VALUE.encode("utf-8"))
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(
            verification.verifier_required(self.path, self.key_env)
        )
        self.assertFalse(self._key_path().exists())

    def test_record_verifier_consumes_the_key_file(self):
        self._write_signed_evidence()
        verifier = verification.record_verifier(
            self.root, self.path, 0, environ=self.key_env
        )
        self.assertEqual(verifier["state"], "passed")
        self.assertFalse(self._key_path().exists())

    def test_final_consumes_the_key_file(self):
        self._write_signed_evidence()
        env = dict(self.env, **self.key_env)
        _, comparison = verification.run_final(
            self.root, self.path, self.base, environ=env
        )
        self.assertTrue(comparison["accepted"])
        self.assertFalse(self._key_path().exists())

    def test_baseline_consumes_the_key_file(self):
        self._write_signed_evidence()
        env = dict(self.env, **self.key_env)
        data, reused = verification.ensure_baseline(
            self.root, self.path, self.base, environ=env
        )
        self.assertTrue(reused)
        self.assertEqual(data["baseline"]["base_sha"], self.base)
        self.assertFalse(self._key_path().exists())

    def test_read_path_removal_failure_fails_closed(self):
        self._write_signed_evidence()
        key_path = self._key_path()
        original_unlink = Path.unlink

        def deny_unlink(path, *args, **kwargs):
            if path == key_path:
                raise PermissionError("permission denied")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", deny_unlink):
            with self.assertRaises(verification.VerificationError) as caught:
                verification._load_evidence(self.path, self.key_env)
        self.assertIn("cannot be removed", str(caught.exception))
        self.assertTrue(key_path.exists())

    def test_read_path_removes_a_symlinked_key_target(self):
        target = self.key_directory / "real.key"
        target.write_text(KEY_VALUE, encoding="utf-8")
        link = self.key_directory / "linked.key"
        os.symlink(target, link)
        self._write_signed_evidence()
        verification._load_evidence(self.path, {KEY_FILE_ENV: str(link)})
        self.assertFalse(link.exists())
        self.assertFalse(target.exists())

    def test_reading_an_already_absent_key_fails_closed(self):
        self._write_signed_evidence()
        self._key_path().unlink()
        with self.assertRaises(verification.VerificationError) as caught:
            verification._load_evidence(self.path, self.key_env)
        self.assertIn("unavailable", str(caught.exception))


class AttestationKeyEnvironmentLeakTests(unittest.TestCase):
    """The key must never be readable from the engine process environment."""

    COMMAND = (
        'secret="$(cat "$LEAK_EXPECTED")"\n'
        'report="$LEAK_REPORT"\n'
        ': > "$report"\n'
        'env | grep -F -- "$secret" >> "$report" || true\n'
        'if [ -r "/proc/$PPID/environ" ]; then\n'
        '  tr \'\\0\' \'\\n\' < "/proc/$PPID/environ" | '
        'grep -F -- "$secret" >> "$report" || true\n'
        'else\n'
        '  ps -E -p "$PPID" 2>/dev/null | '
        'grep -F -- "$secret" >> "$report" || true\n'
        'fi\n'
        'if [ -e "$LEAK_KEY_FILE" ]; then '
        'printf "key-file-present\\n" >> "$report"; fi\n'
        'if [ -s "$report" ]; then exit 1; fi\n'
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "work"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "test@example.invalid")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "file.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.evidence = base / "evidence.json"
        self.report = base / "leak-report.txt"
        self.expected = base / "expected-secret.txt"
        self.expected.write_text(KEY_VALUE, encoding="utf-8")
        self.key_env = _key_env(base)
        self.key_file = Path(self.key_env[KEY_FILE_ENV])

    def _env(self, extra_env):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (KEY_ENV, KEY_FILE_ENV)
        }
        env.update(
            {
                "PYTHONPATH": str(SRC),
                "CONTINUUM_TEST_COMMAND": self.COMMAND,
                "LEAK_REPORT": str(self.report),
                "LEAK_EXPECTED": str(self.expected),
                "LEAK_KEY_FILE": str(self.key_file),
            }
        )
        env.update(extra_env)
        return env

    def _run(self, extra_env):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum.verification",
                "baseline",
                "--repo",
                str(self.root),
                "--evidence",
                str(self.evidence),
                "--base-sha",
                self.base,
            ],
            env=self._env(extra_env),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_child_command_cannot_read_the_key_through_the_parent_environment(self):
        completed = self._run(self.key_env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The running command could not read the key file: the report is empty.
        # The command-set process must not restore the file, because a survivor
        # it spawned can read the file after the engine exits.
        self.assertFalse(self.key_file.exists())
        self.assertTrue(self.report.exists())
        self.assertEqual(self.report.read_text(encoding="utf-8"), "")
        data = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(data["baseline"]["state"], "passed")

        summary_command = [
            sys.executable,
            "-m",
            "continuum.verification",
            "summary",
            "--evidence",
            str(self.evidence),
        ]
        # A later process fails closed instead of silently verifying without
        # the key, because the consumed copy was not restored.
        missing = subprocess.run(
            summary_command,
            env=self._env(self.key_env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(missing.returncode, 3)
        self.assertIn(
            "the verification attestation key file is unavailable", missing.stderr
        )

        # Re-provisioning from the authoritative source (standard input) makes
        # the next invocation verify the same evidence again.
        provision = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum.verification",
                "provision-key",
                "--repo",
                str(self.root),
            ],
            input=KEY_VALUE,
            env=self._env(self.key_env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(provision.returncode, 0, provision.stderr)
        self.assertEqual(self.key_file.read_text(encoding="utf-8"), KEY_VALUE)
        self.assertEqual(self.key_file.stat().st_mode & 0o777, 0o600)
        summary = subprocess.run(
            summary_command,
            env=self._env(self.key_env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn("baseline: passed", summary.stdout)

    def test_background_poller_cannot_read_the_key_after_the_engine_exits(self):
        """Regression: a restored key file is readable by a survivor process."""

        poll_report = self.root.parent / "poller-report.txt"
        poll_done = self.root.parent / "poller-done.txt"
        command = (
            "nohup bash -c 'i=0; while [ \"$i\" -lt 250 ]; do "
            'if [ -e "$POLL_KEY_FILE" ]; then '
            'printf leaked > "$POLL_REPORT"; break; fi; '
            "i=$((i+1)); sleep 0.02; done; "
            "printf finished > \"$POLL_DONE\"' >/dev/null 2>&1 &"
        )
        completed = self._run(
            dict(
                self.key_env,
                CONTINUUM_TEST_COMMAND=command,
                POLL_KEY_FILE=str(self.key_file),
                POLL_REPORT=str(poll_report),
                POLL_DONE=str(poll_done),
            )
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not poll_done.exists():
            time.sleep(0.02)
        self.assertTrue(poll_done.exists(), "the poller did not finish in time")
        # The poller outlived the engine and watched the configured path the
        # whole time; it must never have seen the key.
        self.assertFalse(poll_report.exists())
        self.assertFalse(self.key_file.exists())

    def test_sigterm_does_not_leak_the_key_and_reprovisioning_recovers(self):
        base_env = {
            key: value
            for key, value in os.environ.items()
            if key not in (KEY_ENV, KEY_FILE_ENV)
        }
        base_env["CONTINUUM_TEST_COMMAND"] = "sleep 30"
        contract = verification.resolve_contract(self.root, base_env)
        record = contract.as_dict()
        environment = verification.environment_fingerprint(self.root, base_env)
        verification._save_evidence(
            self.evidence,
            {
                "schema_version": verification.SCHEMA_VERSION,
                "contract": record,
                "baseline": {
                    "base_sha": self.base,
                    "state": "passed",
                    "contract": record,
                    "environment_fingerprint": environment,
                    "commands": [{"name": "test", "exit_code": 0}],
                },
            },
            self.key_env,
        )
        # Saving evidence consumed the key; re-provision it for the CLI process
        # that this test starts next.
        verification.provision_attestation_key(
            KEY_VALUE, self.key_env, worktree=self.root
        )
        env = self._env(
            dict(self.key_env, CONTINUUM_TEST_COMMAND="sleep 30")
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "continuum.verification",
                "final",
                "--repo",
                str(self.root),
                "--evidence",
                str(self.evidence),
                "--base-sha",
                self.base,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            # Wait until the engine has consumed the key and is running the
            # repository-controlled command.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and self.key_file.exists():
                time.sleep(0.02)
            self.assertFalse(self.key_file.exists())
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.communicate(timeout=30)
            self.assertEqual(process.returncode, -signal.SIGTERM)
        finally:
            if process.poll() is None:  # pragma: no cover - defensive cleanup
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=30)
        # The interrupted process left no readable key behind.
        self.assertFalse(self.key_file.exists())
        # The next invocation re-provisions from the authoritative source and
        # verifies the same evidence, so an interrupt is not permanent loss.
        provision = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum.verification",
                "provision-key",
                "--repo",
                str(self.root),
            ],
            input=KEY_VALUE,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(provision.returncode, 0, provision.stderr)
        summary = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuum.verification",
                "summary",
                "--evidence",
                str(self.evidence),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertIn("baseline: passed", summary.stdout)

    def test_key_value_in_the_engine_environment_is_rejected_before_commands_run(self):
        completed = self._run({KEY_ENV: KEY_VALUE})
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn(
            "must not be set in the engine process environment", completed.stderr
        )
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
