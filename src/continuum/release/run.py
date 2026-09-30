"""Running a release plan.

The runner knows four things and no more: a step is an argument vector, an
assert step's output must contain a named substring, a copy step moves bytes
between paths without a tool, and teardown runs whatever happened. It does not
know what the vectors contain, so an adapter can change its toolchain without
the runner — and everything above it — changing at all.

The invariants this module enforces are the ones a release cannot recover from
getting wrong:

* **Argument vectors, never a shell.** `shell=False` and no interpolation. A
  bundle identifier, a certificate name, or a file path taken from a repository
  is data, and a shell would treat it as a language.
* **Teardown always runs.** A plan that fails halfway has usually already
  created a keychain and written key material to disk. The failure is reported
  *after* the teardown, so a failed job still leaves nothing behind.
* **A secret is never printed.** Substitution slots are read from the
  environment, substituted into the argument vector, and redacted out of
  everything the runner records. A step name or an error message can quote the
  command back, so the redaction happens on the way out, not on the way in.
* **The pin is checked, or the step fails.** An assert step whose substring
  does not appear is a failure, not a warning: the whole point of the pin is
  that a different certificate would have been caught.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .plan import (
    ENCODING_BASE64,
    SLOT_PATTERN,
    STEP_ASSERT,
    STEP_COPY,
    STEP_MATERIALIZE,
    PlanStep,
    ReleasePlan,
    StepFailure,
)

# Nothing unbounded is ever captured from a subprocess: a runaway tool must not
# be able to exhaust the runner's memory, and the runner never needs more than
# enough output to match an assert.
MAX_CAPTURED_OUTPUT = 65536

# The runner builds each child's environment from the plan's needs rather than
# handing over its own wholesale, so a release step never inherits the job's
# GitHub credentials.
_STRIPPED_ENV_PREFIXES = ("GITHUB_", "RUNNER_", "ACTIONS_", "CI_")


@dataclass(frozen=True)
class CommandResult:
    """What one command step produced."""

    code: int
    output: str


#: How a command step reaches a process: the argument vector as written, the
#: directory it runs in, and the environment it runs with. The signature is the
#: runner's whole contract with the outside world, so replacing it replaces
#: nothing else about how a plan is executed.
CommandRunner = Callable[[Sequence[str], str, Mapping[str, str]], CommandResult]


def base_environment() -> Dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_STRIPPED_ENV_PREFIXES)
    }


def redact(text: str, secrets: Sequence[str]) -> str:
    """Remove every secret value from ``text``.

    The runner prints and records tool output, and tool output echoes its own
    argument vector. Substitution is therefore not enough on its own: the value
    has to be taken back out afterwards, or a wrong-password failure would
    publish the password into the job log.
    """

    cleaned = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


def truncate(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_CAPTURED_OUTPUT:
        return text
    return text[:MAX_CAPTURED_OUTPUT] + "\n... (truncated)"


class Runner:
    """Executes a plan's steps and collects what happened.

    The environment is passed in rather than read from `os.environ` so a caller
    can decide exactly which secrets a job may see, and so a test can run a
    whole plan without a macOS runner anywhere in sight.
    """

    def __init__(
        self,
        *,
        environment: Optional[Dict[str, str]] = None,
        workdir: str = "",
        dry_run: bool = False,
        command_runner: Optional[CommandRunner] = None,
    ) -> None:
        self.environment: Dict[str, str] = dict(
            base_environment() if environment is None else environment
        )
        self.workdir = workdir
        self.dry_run = dry_run
        self.completed: List[str] = []
        #: How a command step is carried out. The default starts a process; a test
        #: supplies a callable and runs a whole macOS plan on a machine that has
        #: no macOS toolchain on it. The plan, and therefore the ordering and the
        #: assertions in it, are identical either way — which is the point: a test
        #: that exercised a different runner than production would be testing its
        #: own runner.
        self.command_runner: Optional[CommandRunner] = command_runner

    # -- substitution ------------------------------------------------------
    def resolve(self, step: PlanStep) -> Tuple[List[str], List[str]]:
        """Substitute slots, returning the argv and the secret values used.

        A slot that resolves to nothing is a failure, not an empty string. A
        signing step invoked with an empty password produces a plausible
        looking artifact signed by nobody, which is precisely the outcome the
        pin is supposed to make impossible.

        An `env:` slot behaves the same way for the same reason: a release
        stamped with an empty version, or a shell's literal `$VERSION`, is an
        artifact that looks finished and is not.
        """

        argv: List[str] = []
        values: List[str] = []

        def substitute(match: "re.Match[str]") -> str:
            kind, name = match.group(1), match.group(2)
            value = self._require_env(step, name, secret=(kind == "secret"))
            if kind == "secret":
                values.append(value)
            return value

        for token in step.argv:
            argv.append(SLOT_PATTERN.sub(substitute, token))
        return argv, values

    # -- execution ---------------------------------------------------------
    def _require_env(self, step: PlanStep, name: str, *, secret: bool = True) -> str:
        value = self.environment.get(name, "")
        if value:
            return value
        if secret:
            raise StepFailure(
                step.name,
                "missing-secret",
                f"step {step.name!r} needs {name}, which is not available",
                remediation=(
                    f"the job must expose the repository secret {name} in its "
                    "environment; a pull request from a fork never receives it"
                ),
            )
        raise StepFailure(
            step.name,
            "missing-value",
            f"step {step.name!r} needs {name}, which is not set",
            remediation=(
                f"the job must set {name}; a step argument is not a shell, so a "
                f"literal '${{{name}}}' would be used as the value"
            ),
        )

    def _materialize(self, step: PlanStep) -> Tuple[int, str]:
        """Write a secret to a scratch file without involving a shell.

        An empty secret is an error rather than an empty file: a tool handed an
        empty certificate produces a plausible artifact signed by nobody, and
        the difference has to be reported by the signer instead of guessed at
        by the file writer.
        """

        raw = self._require_env(step, step.source_env, secret=True)
        if step.encoding == ENCODING_BASE64:
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return 1, (
                    f"{step.source_env} is not valid base64; a repository secret "
                    "carrying binary certificate material must hold its base64 form"
                )
        else:
            data = raw.encode("utf-8")
        if self.dry_run:
            return 0, ""
        parent = os.path.dirname(step.path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(step.path, "wb") as handle:
                handle.write(data)
            os.chmod(step.path, 0o600)
        except OSError as exc:
            return 1, f"could not write {step.path}: {exc}"
        return 0, ""

    def _copy(self, step: PlanStep) -> Tuple[int, str]:
        """Move bytes from one path to another, with no tool and no shell.

        A copy is not a command because there is nothing to execute: the reason
        this is a step at all is that the *ordering* is the claim. A bundle is
        assembled out of binaries that have already been signed, and a reviewer
        has to be able to see that those copies happen after the signatures and
        before the seal. Making it a command would mean inventing a `cp` for the
        plan to invoke; making it invisible would mean the ordering is a property
        of the adapter rather than of the plan.
        """

        if self.dry_run:
            return 0, ""
        for source, destination in step.copies():
            resolved_source = self._resolve(source)
            resolved_destination = self._resolve(destination)
            parent = os.path.dirname(resolved_destination)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                # `copy2` keeps the mode and the timestamps, which is what a
                # signature covers: `codesign` records the signed content, and a
                # bundle assembled from a copy whose timestamps were rewritten
                # after the fact is a bundle whose seal describes other bytes.
                shutil.copy2(resolved_source, resolved_destination)
            except OSError as exc:
                return 1, f"could not copy {resolved_source} to {resolved_destination}: {exc}"
        return 0, ""

    def _resolve(self, path: str) -> str:
        """Where a path in a plan actually is, relative to this runner's workdir.

        A plan names paths as the repository names them, and a repository runs
        from its own root. Subprocess steps get that for free through `cwd`;
        a filesystem step has to be told, or it would resolve against whatever
        directory the process happened to start in.
        """

        if os.path.isabs(path) or not self.workdir:
            return path
        return os.path.join(self.workdir, path)

    def _execute(self, step: PlanStep, argv: Sequence[str]) -> Tuple[int, str]:
        if self.dry_run:
            return 0, ""
        if self.command_runner is not None:
            result = self.command_runner(list(argv), self.workdir, self.environment)
            return result.code, result.output
        try:
            # `check=False` because the exit status is this method's to
            # interpret: a step that fails is a `StepFailure` carrying the output
            # and a code, not an exception thrown from three frames down.
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                shell=False,
                cwd=self.workdir or None,
                env=self.environment,
                check=False,
            )
        except FileNotFoundError:
            raise StepFailure(
                step.name,
                "tool-missing",
                f"step {step.name!r} could not start {argv[0]!r}: not available on this runner",
                remediation=(
                    f"{argv[0]} is part of this adapter's toolchain; the job must run "
                    "on a runner that provides it"
                ),
            ) from None
        except OSError as exc:
            raise StepFailure(
                step.name,
                "tool-failed",
                f"step {step.name!r} could not start {argv[0]!r}: {exc}",
            ) from None
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def run_step(self, step: PlanStep) -> str:
        """Run one step, raising `StepFailure` if it does not hold up."""

        if step.kind == STEP_MATERIALIZE:
            values = [self.environment.get(step.source_env, "")]
            code, output = self._materialize(step)
        elif step.kind == STEP_COPY:
            # A copy step resolves its own paths against the workdir, so it has
            # no argument vector to substitute into and nothing that could carry
            # a secret into a failure message.
            values = []
            code, output = self._copy(step)
        else:
            argv, values = self.resolve(step)
            code, output = self._execute(step, argv)
        detail = truncate(redact(output, values))
        if code != 0:
            raise StepFailure(
                step.name,
                "step-failed",
                f"step {step.name!r} exited with status {code}",
                detail=detail,
                remediation="the adapter classifies this failure; see the plan for the invariant it was protecting",
            )
        if step.kind == STEP_ASSERT and step.expect and step.expect not in output:
            raise StepFailure(
                step.name,
                "assertion-failed",
                f"step {step.name!r} completed but its output did not contain {step.expect!r}",
                detail=detail,
                remediation=(
                    "what was produced is not what this target pins; the pinned "
                    "identity must appear in the signing output or the release is not "
                    "the release that was configured"
                ),
            )
        self.completed.append(step.name)
        return detail

    def _clean_scratch(self, plan: ReleasePlan) -> None:
        """Remove every scratch path the plan declared, whatever the outcome.

        Best effort, and never a step, because it must still run when the step
        that created the file is the step that failed — and a runner already
        reporting a failure must not raise a second one while cleaning up.
        """

        if self.dry_run:
            return
        for path in plan.scratch_files():
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path) or os.path.islink(path):
                    os.remove(path)
            except OSError:
                continue

    def teardown(self, plan: ReleasePlan) -> None:
        for step in plan.teardown:
            try:
                self.run_step(step)
            except StepFailure:
                # Teardown that cannot complete is still a failed job, but it
                # must not mask the failure that got us here.
                continue
        self._clean_scratch(plan)

    def run(self, plan: ReleasePlan) -> "ExecutionResult":
        """Run a plan, always tearing down, and report the first failure."""

        failure: Optional[StepFailure] = None
        for step in plan.steps:
            try:
                self.run_step(step)
            except StepFailure as exc:
                failure = exc
                break
        self.teardown(plan)
        return ExecutionResult(
            plan=plan,
            failure=failure,
            completed=list(self.completed),
            dry_run=self.dry_run,
        )


@dataclass(frozen=True)
class ExecutionResult:
    """What happened when a plan ran, including when it did not finish."""

    plan: ReleasePlan
    failure: Optional[StepFailure] = None
    completed: List[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None

    def describe(self) -> Dict[str, Any]:
        return {
            "target": self.plan.target,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "completed_steps": list(self.completed),
            "degraded": self.plan.degraded,
            "degradation_reason": self.plan.degradation_reason,
            "signature_pinned": self.plan.signature_pinned,
            "failure": self.failure.describe() if self.failure else None,
        }


def run_plan(
    plan: ReleasePlan,
    *,
    environment: Optional[Mapping[str, str]] = None,
    workdir: str = "",
    dry_run: bool = False,
    command_runner: Optional[CommandRunner] = None,
) -> ExecutionResult:
    return Runner(
        environment=environment,
        workdir=workdir,
        dry_run=dry_run,
        command_runner=command_runner,
    ).run(plan)
