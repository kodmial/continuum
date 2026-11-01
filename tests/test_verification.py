"""Tests for repository-authoritative verification evidence handling."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from continuum import verification

KEY_ENV = verification.ATTESTATION_KEY_ENV
BASE = "1" * 40
KEY_ENV_VALUES = {KEY_ENV: "runner-controlled-secret"}


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
        verification._save_evidence(self.path, self._evidence(), KEY_ENV_VALUES)
        loaded = verification._load_evidence(self.path, KEY_ENV_VALUES)
        self.assertEqual(loaded["baseline"]["base_sha"], BASE)
        self.assertEqual(
            loaded["attestation"]["algorithm"],
            verification.ATTESTATION_ALGORITHM,
        )

    def test_verification_commands_do_not_receive_attestation_key(self):
        command = "printf '%s' \"${CONTINUUM_VERIFICATION_ATTESTATION_KEY-}\""
        result = verification.run_command_set(
            _contract(command), self.root, environ=KEY_ENV_VALUES
        )
        self.assertEqual(result["state"], "passed")
        self.assertEqual(result["commands"][0]["output_tail"], "")
        self.assertNotIn("runner-controlled-secret", json.dumps(result))
        self.assertNotIn(
            KEY_ENV, verification._child_environment(KEY_ENV_VALUES)
        )

    def test_tampered_contract_is_rejected(self):
        verification._save_evidence(self.path, self._evidence(), KEY_ENV_VALUES)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["baseline"]["state"] = "failed"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification._load_evidence(self.path, KEY_ENV_VALUES)

    def test_missing_attestation_is_rejected_when_key_is_set(self):
        self.path.write_text(json.dumps(self._evidence()), encoding="utf-8")
        with self.assertRaises(verification.VerificationError):
            verification._load_evidence(self.path, KEY_ENV_VALUES)

    def test_unkeyed_evidence_is_readable_without_attestation(self):
        verification._save_evidence(self.path, self._evidence(), {})
        loaded = verification._load_evidence(self.path, {})
        self.assertNotIn("attestation", loaded)


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
        verification._save_evidence(self.path, data, KEY_ENV_VALUES)
        env = dict(self.env, **KEY_ENV_VALUES)
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
        verification._save_evidence(self.path, data, KEY_ENV_VALUES)
        env = dict(self.env, **KEY_ENV_VALUES)
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
        verification._save_evidence(self.path, self._evidence(), KEY_ENV_VALUES)
        env = dict(self.env, **KEY_ENV_VALUES)
        data, reused = verification.ensure_baseline(
            self.root, self.path, self.base, environ=env
        )
        self.assertTrue(reused)
        self.assertEqual(data["baseline"]["base_sha"], self.base)


if __name__ == "__main__":
    unittest.main()
