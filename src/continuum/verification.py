"""Repository-authoritative implementation verification (kodmial/continuum#168).

Issue mode must establish a repository baseline before the primary coding
session, run an independent verifier/fix session on the resulting checkout,
and then repeat exactly the same verification commands. This module owns the
pure contract and evidence handling. Workflows own the agent lifecycle and
Git/API state.

The resolver never guesses a project stack. It reads the consumer's existing
Continuum validation contract:

* the ``prepare_command`` / ``build_command`` / ``test_command`` /
  ``validation_command`` inputs of the consumer's primary ``CI`` caller for
  ``continuum-validation.yml``;
* the matching documented ``CONTINUUM_*_COMMAND`` repository variables when the
  caller does not supply a literal command.

At least one build, test, or validation hook must resolve. No hook means an
explicit ``unconfigured`` state, never a silent green or a substitute command.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

#: Workflow-owned evidence. It is retained on a 429 checkpoint and removed at
#: the publish boundary (see ``continuum_pr_staging.sh``); it is never product
#: code and is never committed to an implementation PR.
EVIDENCE_FILE_NAME = ".continuum-verification.json"

#: Ordered validation hooks, mirroring ``continuum-validation.yml``. The order
#: is the repository contract; baseline and final use the exact same tuple.
HOOKS: Tuple[Tuple[str, str], ...] = (
    ("prepare", "CONTINUUM_PREPARE_COMMAND"),
    ("build", "CONTINUUM_BUILD_COMMAND"),
    ("test", "CONTINUUM_TEST_COMMAND"),
    ("validation", "CONTINUUM_VALIDATION_COMMAND"),
)

#: A contract is only a *verification* contract when it executes at least one
#: of these hooks. A prepare-only configuration is still unconfigured.
VERIFICATION_HOOKS = frozenset({"build", "test", "validation"})

CI_CALLER_CANDIDATES = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci.yaml",
)

#: Environment components pinned so baseline and final are comparable.
ENVIRONMENT_KEYS = (
    "RUNNER_OS",
    "RUNNER_ARCH",
    "ImageOS",
    "ImageVersion",
    "LANG",
    "LC_ALL",
    "SHELL",
    "PATH",
    "GITHUB_ACTIONS",
)

#: The verification engine is invoked with this directory on ``PYTHONPATH``.
#: Consumer commands must not inherit it: that would let a checkout of
#: Continuum on the runner shadow the task worktree's own ``continuum``
#: package. Strip exactly this entry while preserving the caller's path.
_ENGINE_SOURCE = Path(__file__).resolve().parents[1]

#: Workflow-owned paths excluded from task-change detection. They are never
#: part of an implementation result, so their presence must not make a
#: no-change run look like a task implementation.
WORKFLOW_OWNED_PATHS = (
    EVIDENCE_FILE_NAME,
    ".continuum-429-stage",
    ".continuum-429-recovery.json",
    ".continuum-cancel-engine",
    ".continuum-429-engine",
    ".continuum-admission-engine",
)

_VARS_EXPRESSION = re.compile(
    r"\A\$\{\{\s*vars\.([A-Z][A-Z0-9_]{0,63})\s*\}\}\Z"
)


class VerificationError(RuntimeError):
    """A verification contract or evidence error that must fail closed."""


@dataclass(frozen=True)
class VerificationCommand:
    """One resolved repository command and the contract that supplied it."""

    name: str
    command: str
    source: str

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "command": self.command, "source": self.source}


@dataclass(frozen=True)
class VerificationContract:
    """Resolved command set plus its explicit state."""

    state: str
    source: str
    commands: Tuple[VerificationCommand, ...]
    errors: Tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return self.state == "configured" and any(
            command.name in VERIFICATION_HOOKS for command in self.commands
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [[command.name, command.command] for command in self.commands],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "source": self.source,
            "commands": [command.as_dict() for command in self.commands],
            "fingerprint": self.fingerprint,
            "errors": list(self.errors),
        }


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _resolve_literal(
    value: Any, environ: Mapping[str, str]
) -> Tuple[str, str]:
    """Resolve one CI caller ``with:`` value to a literal command.

    Repository variables referenced with the simple ``${{ vars.NAME }}`` form
    resolve from the same environment the workflow passes down. Any other
    expression is intentionally unresolved: Continuum never evaluates arbitrary
    GitHub expressions and never embeds an expression as a shell command.
    Returns ``(command, error)``; an unsupported expression is an error rather
    than an empty command so it cannot silently fall back to a different check.
    """

    if value is None:
        return "", ""
    if isinstance(value, bool):
        return ("true" if value else "false"), ""
    if isinstance(value, (int, float)):
        return str(value), ""
    if not isinstance(value, str):
        return "", ""
    text = value.strip()
    if not text:
        return "", ""
    if "${{" not in text:
        return text, ""
    match = _VARS_EXPRESSION.match(text)
    if match is None:
        return "", (
            "unsupported GitHub expression in a verification command; "
            "only ${{ vars.NAME }} or a literal command is accepted"
        )
    return str(environ.get(match.group(1), "") or "").strip(), ""


_HOOK_LINE = re.compile(
    r"^\s*(?:-\s*)?(['\"]?)(prepare|build|test|validation)_command\1\s*:\s*(.*?)\s*$"
)


def _ci_caller_commands(
    repo_root: Path, environ: Mapping[str, str]
) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """Read literal hook commands from the consumer's primary CI caller.

    The file is scanned textually on purpose: a workflow's ``on:`` block may use
    YAML features that Continuum's restricted config parser rejects, while the
    values Continuum needs are plain ``<hook>_command:`` scalars. Only files that
    actually call ``continuum-validation.yml`` are considered.
    """

    values: Dict[str, Tuple[str, str]] = {}
    errors: List[str] = []
    for relative in CI_CALLER_CANDIDATES:
        path = repo_root / relative
        if not path.is_file():
            continue
        text = _read_text(path)
        if "continuum-validation.yml" not in text:
            continue
        for line in text.splitlines():
            match = _HOOK_LINE.match(line)
            if match is None:
                continue
            hook = match.group(2)
            if hook in values:
                continue
            raw = match.group(3).strip()
            if raw[:1] in ("'", '"') and len(raw) >= 2 and raw.endswith(raw[0]):
                quote = raw[0]
                raw = raw[1:-1]
                if quote == '"':
                    raw = raw.replace('\\"', '"').replace("\\\\", "\\")
                else:
                    raw = raw.replace("''", "'")
            literal, error = _resolve_literal(raw, environ)
            if error:
                errors.append("{}: {}".format(relative, error))
                continue
            if literal:
                values[hook] = (
                    literal,
                    "{}:{}_{}".format(relative, hook, "command"),
                )
        if values:
            break
    return values, errors


def resolve_contract(
    repo_root: Path | str,
    environ: Optional[Mapping[str, str]] = None,
) -> VerificationContract:
    """Resolve the consumer's authoritative verification commands.

    The explicit CI caller contract wins over repository variables, mirroring
    ``continuum-validation.yml``'s ``inputs.foo || vars.foo`` precedence. Only
    non-empty literal commands are returned; there is no built-in fallback.
    """

    root = Path(repo_root)
    env = environ if environ is not None else os.environ
    ci_values, errors = _ci_caller_commands(root, env)

    commands: List[VerificationCommand] = []
    sources: List[str] = []
    for hook, variable in HOOKS:
        value = ""
        source = ""
        from_ci = ci_values.get(hook)
        if from_ci is not None:
            value, source = from_ci
        if not value:
            value = str(env.get(variable, "") or "").strip()
            source = variable if value else ""
        if value:
            commands.append(
                VerificationCommand(name=hook, command=value, source=source)
            )
            sources.append(source)

    has_verification = any(
        command.name in VERIFICATION_HOOKS for command in commands
    )
    if errors:
        return VerificationContract(
            state="unconfigured",
            source="none",
            commands=tuple(commands),
            errors=tuple(errors),
        )
    if not has_verification:
        detail = (
            "No authoritative verification command was found. Configure the "
            "consumer CI caller's build/test/validation command or the matching "
            "CONTINUUM_*_COMMAND repository variable."
        )
        return VerificationContract(
            state="unconfigured",
            source="none",
            commands=tuple(commands),
            errors=(detail,),
        )

    source = sources[0] if sources else "none"
    if any(item.startswith(".github/workflows/") for item in sources):
        source = "ci-caller"
    elif any(item.startswith("CONTINUUM_") for item in sources):
        source = "repository-variables"
    return VerificationContract(
        state="configured",
        source=source,
        commands=tuple(commands),
        errors=tuple(errors),
    )


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_value(repo: Path, *args: str) -> str:
    completed = _run_git(repo, *args)
    if completed.returncode != 0:
        raise VerificationError(
            "git {} failed: {}".format(
                " ".join(args), (completed.stderr or "").strip()[-1000:]
            )
        )
    return (completed.stdout or "").strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    completed = _run_git(
        repo, "merge-base", "--is-ancestor", ancestor, descendant
    )
    return completed.returncode == 0


def _is_workflow_owned(path: str) -> bool:
    return path in WORKFLOW_OWNED_PATHS or any(
        path == owned or path.startswith(owned + "/")
        for owned in WORKFLOW_OWNED_PATHS
    )


def _child_environment(environ: Mapping[str, str]) -> Dict[str, str]:
    """Remove the engine import path from commands run for the consumer."""

    env = dict(environ)
    raw = str(env.get("PYTHONPATH", "") or "")
    kept: List[str] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        try:
            is_engine = Path(entry).expanduser().resolve() == _ENGINE_SOURCE
        except OSError:
            is_engine = False
        if not is_engine:
            kept.append(entry)
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    return env


def environment_fingerprint(repo: Path, environ: Mapping[str, str]) -> str:
    """Deterministic identity of the verification environment."""

    env = _child_environment(environ)
    payload = {key: str(env.get(key, "")) for key in ENVIRONMENT_KEYS}
    payload["cwd"] = str(Path(repo).resolve())
    payload["shell"] = "bash -c"
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_evidence(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION}
    try:
        data = json.loads(_read_text(path))
    except Exception as exc:
        raise VerificationError(
            "verification evidence is not readable JSON: {}".format(exc)
        ) from exc
    if not isinstance(data, dict):
        raise VerificationError("verification evidence must be a JSON object")
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        raise VerificationError("verification evidence schema is unsupported")
    return data


def _save_evidence(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _command_result(completed: subprocess.CompletedProcess, started: float) -> Dict[str, Any]:
    output = (completed.stdout or "")
    return {
        "exit_code": int(completed.returncode),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_tail": output[-4000:],
    }


def run_command_set(
    contract: VerificationContract,
    repo: Path | str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute one resolved command set exactly as recorded."""

    root = Path(repo)
    env = _child_environment(environ if environ is not None else os.environ)
    timeout = timeout_seconds
    if timeout is None:
        raw = str(env.get("CONTINUUM_VERIFICATION_TIMEOUT_SECONDS", "") or "")
        try:
            timeout = float(raw) if raw else None
        except ValueError:
            raise VerificationError(
                "CONTINUUM_VERIFICATION_TIMEOUT_SECONDS must be numeric"
            ) from None
    results: List[Dict[str, Any]] = []
    for command in contract.commands:
        started = time.monotonic()
        result: Dict[str, Any]
        try:
            completed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", command.command],
                cwd=str(root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = _command_result(completed, started)
        except subprocess.TimeoutExpired as exc:
            output = ""
            for stream in (exc.stdout, exc.stderr):
                if isinstance(stream, bytes):
                    output += stream.decode("utf-8", "replace")
                elif stream:
                    output += str(stream)
            result = {
                "exit_code": 124,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "output_tail": output[-4000:],
                "timed_out": True,
            }
        result.update(
            {
                "name": command.name,
                "command": command.command,
                "source": command.source,
            }
        )
        results.append(result)
    failed = [item["name"] for item in results if int(item["exit_code"]) != 0]
    return {
        "phase": "verification",
        "recorded_at": _now(),
        "environment_fingerprint": environment_fingerprint(root, env),
        "state": "failed" if failed else "passed",
        "commands": results,
        "failed_commands": failed,
    }


def _contract_from_evidence(data: Mapping[str, Any]) -> VerificationContract:
    section = data.get("contract")
    if not isinstance(section, dict):
        raise VerificationError("verification evidence has no resolved contract")
    commands = section.get("commands")
    if not isinstance(commands, list):
        raise VerificationError("verification evidence contract has no commands")
    resolved: List[VerificationCommand] = []
    for raw in commands:
        if not isinstance(raw, dict):
            raise VerificationError("verification evidence command is malformed")
        name = str(raw.get("name") or "").strip()
        command = str(raw.get("command") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not name or not command:
            raise VerificationError("verification evidence command is incomplete")
        resolved.append(
            VerificationCommand(name=name, command=command, source=source)
        )
    contract = VerificationContract(
        state=str(section.get("state") or ""),
        source=str(section.get("source") or ""),
        commands=tuple(resolved),
    )
    expected = str(section.get("fingerprint") or "")
    if expected and contract.fingerprint != expected:
        raise VerificationError(
            "verification evidence contract fingerprint does not match its commands"
        )
    return contract


def ensure_baseline(
    repo: Path | str,
    evidence_path: Path | str,
    base_sha: str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Ensure a pre-change baseline exists for ``base_sha``.

    Returns ``(evidence, reused)``. A configured command set that fails still
    produces evidence and does not abort the implementation; the final phase
    decides whether a post-change result is acceptable. An unresolved contract,
    malformed evidence, or an attempt to baseline an already-mutated checkout
    raises :class:`VerificationError` and must fail closed.
    """

    root = Path(repo)
    path = Path(evidence_path)
    env = environ if environ is not None else os.environ
    base = str(base_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise VerificationError("baseline base SHA is malformed")

    data = _load_evidence(path)
    contract = resolve_contract(root, env)
    if not contract.configured:
        raise VerificationError(contract.errors[0] if contract.errors else "unconfigured verification contract")

    existing = data.get("baseline")
    if isinstance(existing, dict):
        recorded_contract = existing.get("contract")
        recorded_fingerprint = ""
        if isinstance(recorded_contract, dict):
            recorded_fingerprint = str(recorded_contract.get("fingerprint") or "")
        if recorded_fingerprint != contract.fingerprint:
            raise VerificationError(
                "the resolved verification contract changed after the baseline was recorded"
            )
        recorded_base = str(existing.get("base_sha") or "").strip().lower()
        if recorded_base != base:
            raise VerificationError(
                "the recorded baseline belongs to a different task base"
            )
        if not _is_ancestor(root, base):
            raise VerificationError(
                "the recorded baseline base is not an ancestor of the current checkout"
            )
        return data, True

    head = _git_value(root, "rev-parse", "HEAD").lower()
    if head != base:
        raise VerificationError(
            "cannot establish a pre-change baseline: the checkout is already "
            "past the task base ({} != {})".format(head, base)
        )
    if task_changes_present(root, base):
        raise VerificationError(
            "cannot establish a pre-change baseline: the checkout already "
            "contains uncommitted task changes"
        )

    result = run_command_set(contract, root, env, timeout_seconds)
    result.update(
        {
            "phase": "baseline",
            "base_sha": base,
            "head_sha": head,
            "contract": contract.as_dict(),
        }
    )
    data.update(
        {
            "schema_version": SCHEMA_VERSION,
            "contract": contract.as_dict(),
            "baseline": result,
        }
    )
    _save_evidence(path, data)
    if result["state"] != "passed":
        print(
            "::warning::Baseline verification failed for: {}. The failure is "
            "recorded as pre-existing and the implementation must not claim a "
            "green result unless the same command set passes after the change.".format(
                ", ".join(result["failed_commands"])
            )
        )
    else:
        print("Baseline verification passed for the resolved repository command set.")
    return data, False


def verifier_required(evidence_path: Path | str) -> bool:
    path = Path(evidence_path)
    data = _load_evidence(path)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    verifier = data.get("verifier")
    if not isinstance(verifier, dict):
        return True
    if str(verifier.get("state") or "") != "passed":
        return True
    return str(verifier.get("contract_fingerprint") or "") != str(
        (baseline.get("contract") or {}).get("fingerprint") or ""
    )


def record_verifier(
    repo: Path | str,
    evidence_path: Path | str,
    exit_code: int,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Record the distinct fresh verifier/fix session outcome."""

    root = Path(repo)
    path = Path(evidence_path)
    data = _load_evidence(path)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    contract = _contract_from_evidence(data)
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if status.returncode != 0:
        raise VerificationError("could not inspect the verifier worktree")
    changed_paths = [
        line[3:].strip()
        for line in (status.stdout or "").splitlines()
        if line.strip()
        and line[3:].strip() not in WORKFLOW_OWNED_PATHS
    ]
    verifier = {
        "phase": "verifier",
        "recorded_at": _now(),
        "state": "passed" if int(exit_code) == 0 else "failed",
        "exit_code": int(exit_code),
        "contract_fingerprint": contract.fingerprint,
        "head_sha": _git_value(root, "rev-parse", "HEAD").lower(),
        "changed_paths": changed_paths,
    }
    data["verifier"] = verifier
    _save_evidence(path, data)
    return verifier


def _comparison(
    baseline: Mapping[str, Any], final: Mapping[str, Any]
) -> Dict[str, Any]:
    baseline_results = {
        str(item.get("name")): int(item.get("exit_code") or 0)
        for item in baseline.get("commands", [])
        if isinstance(item, dict)
    }
    final_results = {
        str(item.get("name")): int(item.get("exit_code") or 0)
        for item in final.get("commands", [])
        if isinstance(item, dict)
    }
    new_failures = [
        name
        for name, code in final_results.items()
        if code != 0 and baseline_results.get(name, 0) == 0
    ]
    preexisting_failures = [
        name
        for name, code in final_results.items()
        if code != 0 and baseline_results.get(name, 0) != 0
    ]
    resolved_failures = [
        name
        for name, code in baseline_results.items()
        if code != 0 and final_results.get(name, 0) == 0
    ]
    final_passed = str(final.get("state") or "") == "passed"
    if final_passed and str(baseline.get("state") or "") == "failed":
        result = "passed-after-preexisting-failure"
    elif final_passed:
        result = "passed"
    elif new_failures:
        result = "failed-new-regression"
    elif preexisting_failures:
        result = "failed-preexisting"
    else:
        result = "failed"
    return {
        "result": result,
        "accepted": final_passed,
        "new_failures": new_failures,
        "preexisting_failures": preexisting_failures,
        "resolved_failures": resolved_failures,
        "baseline_state": baseline.get("state"),
        "final_state": final.get("state"),
    }


def _reestablish_baseline(
    root: Path,
    evidence_path: Path,
    data: Dict[str, Any],
    baseline: Mapping[str, Any],
    contract: VerificationContract,
    base: str,
    env: Mapping[str, str],
    timeout_seconds: Optional[float],
    environment: str,
) -> Dict[str, Any]:
    """Re-run the baseline at the task base on a resumed, changed runner.

    A 429 evacuation moves the task onto a replacement runner whose image or
    ``PATH`` may differ. The restored baseline is then incomparable. Rebasing
    only the environment fingerprint would keep stale results, so re-establish
    a real baseline against the same task base before the final run.
    """

    if not _is_ancestor(root, base):
        raise VerificationError(
            "cannot re-establish the verification baseline: the task base is "
            "not an ancestor of the current checkout"
        )
    previous = str(baseline.get("environment_fingerprint") or "")
    temporary = tempfile.mkdtemp(prefix="continuum-verification-")
    try:
        added = _run_git(root, "worktree", "add", "--detach", temporary, base)
        if added.returncode != 0:
            raise VerificationError(
                "could not re-establish the verification baseline on the current "
                "runner: {}".format((added.stderr or "").strip()[-1000:])
            )
        result = run_command_set(contract, temporary, env, timeout_seconds)
    finally:
        _run_git(root, "worktree", "remove", "--force", temporary)
        shutil.rmtree(temporary, ignore_errors=True)
    result.update(
        {
            "phase": "baseline",
            "base_sha": base,
            "head_sha": base,
            "contract": contract.as_dict(),
            # The commands ran in the same environment as the final pass; only
            # the temporary worktree path differs from the task checkout.
            "environment_fingerprint": environment,
            "environment_rebaselined": True,
            "previous_environment_fingerprint": previous,
        }
    )
    data["baseline"] = result
    data["contract"] = contract.as_dict()
    _save_evidence(evidence_path, data)
    print(
        "Verification environment changed on a resumed run; re-established the "
        "baseline in the current environment."
    )
    return result


def run_final(
    repo: Path | str,
    evidence_path: Path | str,
    base_sha: str,
    environ: Optional[Mapping[str, str]] = None,
    timeout_seconds: Optional[float] = None,
    resume_stage: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Repeat the exact baseline command set and decide acceptance.

    Returns ``(evidence, comparison)``. ``comparison['accepted']`` is true only
    when the post-change command set actually passed. A pre-existing failure is
    recorded distinctly but never converted into a green result.
    """

    root = Path(repo)
    path = Path(evidence_path)
    env = environ if environ is not None else os.environ
    base = str(base_sha or "").strip().lower()
    data = _load_evidence(path)
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        raise VerificationError("verification evidence has no baseline")
    if str(baseline.get("base_sha") or "").strip().lower() != base:
        raise VerificationError("verification evidence baseline base does not match")
    contract = _contract_from_evidence(data)
    if not contract.configured:
        raise VerificationError("verification evidence contract is not executable")

    baseline_environment = str(baseline.get("environment_fingerprint") or "")
    current_environment = environment_fingerprint(root, env)

    # A checkpoint after the final pass already carries exact evidence. Reuse
    # it only for an explicit tests-passed/publish-pr resume; never for a
    # normal run that could have changed the tree. Reuse precedes the
    # environment comparison because the resumed runner is expected to differ
    # and the already-passed result does not need to be recomputed there.
    existing_final = data.get("final")
    if (
        resume_stage in ("tests-passed", "publish-pr")
        and isinstance(existing_final, dict)
        and str(existing_final.get("state") or "") == "passed"
        and str(existing_final.get("contract_fingerprint") or "")
        == contract.fingerprint
    ):
        comparison = existing_final.get("comparison")
        if isinstance(comparison, dict) and bool(comparison.get("accepted")):
            print("Reusing exact post-change verification evidence from the 429 checkpoint.")
            return data, comparison

    if baseline_environment and current_environment != baseline_environment:
        if not resume_stage:
            raise VerificationError(
                "verification environment changed between baseline and final; the "
                "results are not comparable"
            )
        baseline = _reestablish_baseline(
            root,
            path,
            data,
            baseline,
            contract,
            base,
            env,
            timeout_seconds,
            current_environment,
        )

    result = run_command_set(contract, root, env, timeout_seconds)
    result["contract_fingerprint"] = contract.fingerprint
    result["base_sha"] = base
    result["head_sha"] = _git_value(root, "rev-parse", "HEAD").lower()
    comparison = _comparison(baseline, result)
    result["comparison"] = comparison
    data["final"] = result
    data["result"] = comparison
    _save_evidence(path, data)
    if comparison["accepted"]:
        print(
            "Post-change verification passed using the same resolved command set "
            "as the baseline ({}).".format(comparison["result"])
        )
    else:
        print(
            "::error::Post-change verification failed ({}). New failures: {}; "
            "pre-existing failures: {}.".format(
                comparison["result"],
                ", ".join(comparison["new_failures"]) or "none",
                ", ".join(comparison["preexisting_failures"]) or "none",
            )
        )
    return data, comparison


def baseline_summary(evidence_path: Path | str) -> str:
    data = _load_evidence(Path(evidence_path))
    baseline = data.get("baseline")
    if not isinstance(baseline, dict):
        return "baseline: not recorded"
    commands = [
        "{}:{}".format(item.get("name"), item.get("exit_code"))
        for item in baseline.get("commands", [])
        if isinstance(item, dict)
    ]
    return "baseline: {} ({})".format(
        baseline.get("state") or "unknown", ", ".join(commands) or "no commands"
    )


def task_changes_present(repo: Path | str, base_sha: str) -> bool:
    """Whether the checkout contains real task changes beyond workflow metadata."""

    root = Path(repo)
    base = str(base_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise VerificationError("task base SHA is malformed")
    completed = _run_git(
        root, "status", "--porcelain=v1", "--untracked-files=normal"
    )
    if completed.returncode != 0:
        raise VerificationError("could not inspect the task worktree")
    for line in (completed.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().split(" -> ")[-1]
        if _is_workflow_owned(path):
            continue
        return True
    committed = _run_git(root, "diff", "--name-only", "-z", base, "HEAD")
    if committed.returncode != 0:
        raise VerificationError("could not inspect committed task changes")
    for path in (committed.stdout or "").split("\0"):
        if path and not _is_workflow_owned(path):
            return True
    return False


def _emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: python -m continuum.verification "
            "{baseline,needs-verifier,record-verifier,final,summary,changes}",
            file=sys.stderr,
        )
        return 2
    command = args[0]
    options: Dict[str, str] = {}
    index = 1
    while index < len(args):
        item = args[index]
        if not item.startswith("--") or index + 1 >= len(args):
            print("invalid option: {}".format(item), file=sys.stderr)
            return 2
        options[item[2:].replace("-", "_")] = args[index + 1]
        index += 2

    try:
        if command == "baseline":
            data, reused = ensure_baseline(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                options.get("base_sha", ""),
            )
            print("baseline_reused={}".format(str(reused).lower()))
            _emit(data.get("baseline", {}))
            return 0
        if command == "needs-verifier":
            required = verifier_required(
                options.get("evidence", EVIDENCE_FILE_NAME)
            )
            print("true" if required else "false")
            return 0
        if command == "record-verifier":
            verifier = record_verifier(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                int(options.get("exit_code", "1")),
            )
            _emit(verifier)
            return 0
        if command == "final":
            data, comparison = run_final(
                options.get("repo", "."),
                options.get("evidence", EVIDENCE_FILE_NAME),
                options.get("base_sha", ""),
                resume_stage=options.get("resume_stage", ""),
            )
            _emit(comparison)
            return 0 if comparison.get("accepted") else 1
        if command == "summary":
            print(baseline_summary(options.get("evidence", EVIDENCE_FILE_NAME)))
            return 0
        if command == "changes":
            present = task_changes_present(
                options.get("repo", "."), options.get("base_sha", "")
            )
            print("true" if present else "false")
            return 0
        print("unknown command: {}".format(command), file=sys.stderr)
        return 2
    except VerificationError as exc:
        print("::error::{}".format(exc), file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print("::error::verification helper failed: {}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
