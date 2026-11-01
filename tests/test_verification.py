"""Tests for repository-authoritative verification evidence handling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        # The key file is removed before the command ran.
        self.assertFalse(Path(self.key_env[KEY_FILE_ENV]).exists())

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

    def test_key_file_is_removed_before_a_command_runs(self):
        key_path = self.key_env[KEY_FILE_ENV]
        command = 'if [ -e "$PROBE_KEY_FILE" ]; then echo present; exit 1; fi'
        env = dict(self.key_env, PROBE_KEY_FILE=key_path)
        result = verification.run_command_set(
            _contract(command), self.root, environ=env
        )
        self.assertEqual(result["state"], "passed")
        self.assertFalse(Path(key_path).exists())

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

    def _run(self, extra_env):
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
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_child_command_cannot_read_the_key_through_the_parent_environment(self):
        completed = self._run(self.key_env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(self.key_file.exists())
        self.assertTrue(self.report.exists())
        self.assertEqual(self.report.read_text(encoding="utf-8"), "")
        data = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertEqual(data["baseline"]["state"], "passed")

    def test_key_value_in_the_engine_environment_is_rejected_before_commands_run(self):
        completed = self._run({KEY_ENV: KEY_VALUE})
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn(
            "must not be set in the engine process environment", completed.stderr
        )
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
