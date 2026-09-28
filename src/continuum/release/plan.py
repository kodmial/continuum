"""The release plan: what a target will do, and what it promises.

A plan is a value, not a script. It can be printed, diffed, reviewed, and
asserted on in a test without a macOS runner anywhere in sight — which is the
only reason the signing behaviour of a release can be reviewed as carefully as
the code it signs.

Two fields carry the weight:

`pinned_identity`
    The signing identity the plan will assert is present in what it signed. It
    is set for a profile that has one, and deliberately empty for one that does
    not, so "we cannot check this" can never be recorded as "we checked it".

`signature_pinned`
    Whether `pinned_identity` is expected to hold. It is false only for the
    degraded ad-hoc profile, and the plan is then marked `degraded` with an
    explanation. A degraded plan is still runnable — it is how a fork or a
    manual build gets an artifact at all — but it can never be mistaken for the
    stable one, because every field that would imply stability is false.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PLAN_SCHEMA = "continuum.release-plan/v1"

# Every emitted plan records how it was produced, so a plan in a log can be
# traced back to the code that produced it instead of to whoever ran it.
PLAN_KIND = PLAN_SCHEMA

TEARDOWN = "teardown"

# A step is one of these three things and nothing else. `run` executes an
# argument vector; `assert` runs the same vector but additionally requires a
# substring in its output; `materialize` writes a secret to a scratch file.
#
# The distinction matters because "the command exited 0" and "the output says
# the thing we care about" are different claims, and a step that makes the
# second claim has to say so in the plan. `materialize` exists so that putting
# secret material on disk is something the runner does deliberately, can record,
# and can clean up — rather than something an adapter smuggles in through a
# shell redirection, which is exactly the kind of step whose leftovers nobody
# remembers to delete.
STEP_RUN = "run"
STEP_ASSERT = "assert"
STEP_MATERIALIZE = "materialize"

# How a materialized secret is encoded on the way in. `base64` is the transport
# a repository secret uses for binary material, because a secret value has to
# survive being a single line of text.
ENCODING_RAW = "raw"
ENCODING_BASE64 = "base64"
SUPPORTED_ENCODINGS = (ENCODING_RAW, ENCODING_BASE64)

# Substitution slots. Both are brace-delimited so they can be recognised
# *inside* a larger argument as well as standing alone as a whole one:
#
#     security import <keychain> -P ${secret:CONTINUUM_APPLE_P12_PASSWORD}
#     PlistBuddy -c "Set :CFBundleVersion ${env:CONTINUUM_RELEASE_VERSION}" ...
#
# The braces are what make an embedded slot safe. A bare `env:NAME` inside a
# longer string is indistinguishable from text that merely mentions the name,
# and a slot that looks live but never fires is the worst kind: the step exits
# zero having written a literal to disk.
#
# There is no shell anywhere in a plan, so a step that needs a value names it
# with a slot. A literal `$VERSION` in an argument vector is not a variable; it
# is a string that would be stamped into a shipped Info.plist.
SLOT_PATTERN = re.compile(r"\$\{(secret|env):([A-Za-z_][A-Za-z0-9_]*)\}")

# Kept as prefixes for the rare caller that only needs to recognise the form.
SECRET_ENV_PREFIX = "${secret:"
ENV_ENV_PREFIX = "${env:"


def secret_slot(name: str) -> str:
    return f"${{secret:{name}}}"


def env_slot(name: str) -> str:
    return f"${{env:{name}}}"


class ReleaseError(ValueError):
    """Raised when a target cannot be turned into a plan it is willing to run."""


class TargetNotExecutable(ReleaseError):
    """Raised for a typed-but-unimplemented target.

    The configuration is valid; Continuum simply does not build this variant
    yet. This must never be a validation error, or a repository could not
    declare an intent it is working towards.
    """


class SigningUnavailable(ReleaseError):
    """Raised when a stable signing profile cannot obtain its key material."""


class StepFailure(RuntimeError):
    """Raised when a step of a running plan does not hold up.

    Carries a stable `code` so a caller can branch on the *kind* of failure
    without matching on prose, plus a `detail` that is safe to print: a raw tool
    transcript can contain a key path or a certificate serial, so it is
    summarized rather than echoed.
    """

    def __init__(
        self,
        step: str,
        code: str,
        message: str,
        *,
        detail: str = "",
        remediation: str = "",
    ) -> None:
        super().__init__(message)
        self.step = step
        self.code = code
        self.message = message
        self.detail = detail
        self.remediation = remediation

    def describe(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class PlanStep:
    """One thing the plan does, and how to tell that it worked."""

    name: str
    kind: str = STEP_RUN
    argv: Tuple[str, ...] = ()
    expect: Optional[str] = None
    # A substitution slot is `${secret:<NAME>}`: at run time the value comes
    # from the environment variable named by the adapter, and it is never
    # written into a plan document, a log line, or an error message.
    uses_secrets: Tuple[str, ...] = ()
    # Ordinary (non-secret) environment values this step reads, declared so the
    # plan document can be checked for the values it needs before a job starts.
    uses_env: Tuple[str, ...] = ()
    # A `materialize` step writes the environment variable named by
    # `source_env` to `path`, decoding it according to `encoding`.
    source_env: str = ""
    path: str = ""
    encoding: str = ENCODING_RAW
    # Scratch files the step creates that must not outlive the job. They are
    # removed by the runner whatever the outcome, so a failure cannot leave
    # key material behind on a shared runner.
    scratch_paths: Tuple[str, ...] = ()
    # What this step is for. A step whose purpose cannot be stated is a step
    # nobody will notice breaking.
    purpose: str = ""
    teardown: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ReleaseError("a plan step needs a name")
        if self.kind not in (STEP_RUN, STEP_ASSERT, STEP_MATERIALIZE):
            raise ReleaseError(f"unknown plan step kind {self.kind!r}")
        if self.kind == STEP_MATERIALIZE:
            if not self.source_env or not self.path:
                raise ReleaseError(
                    f"step {self.name!r}: a materialize step needs both a source "
                    "environment variable and a destination path"
                )
            if self.encoding not in SUPPORTED_ENCODINGS:
                raise ReleaseError(f"step {self.name!r}: unknown encoding {self.encoding!r}")
            if self.argv:
                raise ReleaseError(
                    f"step {self.name!r}: a materialize step writes its own file and "
                    "must not also carry a command"
                )
            return
        if not self.argv:
            raise ReleaseError(f"step {self.name!r}: a {self.kind} step needs a command")
        if self.kind == STEP_RUN and self.expect is not None:
            raise ReleaseError(f"step {self.name!r}: only an assert step may pin output")
        if self.source_env or self.path:
            raise ReleaseError(
                f"step {self.name!r}: source_env and path belong to a materialize step"
            )

    def slots(self) -> Tuple[Tuple[str, str], ...]:
        """The substitution slots this step carries, as (kind, name) pairs.

        Both slot kinds are reported together so a caller can ask "what does
        this plan need from the environment?" without knowing which prefix means
        secret and which means ordinary value.
        """

        return tuple((kind, name) for kind, name in SLOT_PATTERN.findall(" ".join(self.argv)))

    @property
    def is_teardown(self) -> bool:
        return self.teardown

    def describe(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": self.name, "kind": self.kind, "purpose": self.purpose}
        if self.argv:
            payload["argv"] = list(self.argv)
        if self.expect:
            payload["expect"] = self.expect
        if self.uses_secrets:
            payload["uses_secrets"] = list(self.uses_secrets)
        if self.uses_env:
            payload["uses_env"] = list(self.uses_env)
        if self.source_env:
            payload["source_env"] = self.source_env
        if self.path:
            payload["path"] = self.path
        if self.encoding != ENCODING_RAW:
            payload["encoding"] = self.encoding
        if self.scratch_paths:
            payload["scratch_paths"] = list(self.scratch_paths)
        if self.teardown:
            payload["teardown"] = True
        return payload


@dataclass(frozen=True)
class ReleasePlan:
    """A validated target's build, in order, with its promises attached."""

    target: str
    adapter: str
    platform: str
    build_strategy: str
    distribution: str
    step_list: Tuple[PlanStep, ...]
    signing_mode: str
    signing_identity: str = ""
    # Empty when the profile has no identity to pin (ad-hoc). Asserting on an
    # empty string would pass vacuously, so the runner refuses to assert at all
    # unless a name is present.
    pinned_identity: str = ""
    signature_pinned: bool = True
    degraded: bool = False
    degradation_reason: str = ""
    hardened_runtime: bool = True
    timestamp: bool = False
    artifacts: Tuple[str, ...] = ()
    architectures: Tuple[str, ...] = ()
    universal: bool = False
    publishers: Tuple[str, ...] = ()
    required_secrets: Tuple[str, ...] = ()
    # Ordinary environment values the plan reads. Declared so a caller can
    # check for them before the first step, rather than discovering a missing
    # value once a keychain already exists on the runner.
    required_env: Tuple[str, ...] = ()
    # Scratch files to remove on teardown regardless of outcome.
    scratch_paths: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.target:
            raise ReleaseError("a release plan needs a target id")
        if not self.step_list:
            raise ReleaseError(f"plan for target {self.target!r} has no steps")
        names = [step.name for step in self.step_list]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ReleaseError(
                f"plan for target {self.target!r} repeats step name(s) "
                f"{', '.join(duplicates)}"
            )
        if self.signature_pinned and not self.pinned_identity:
            raise ReleaseError(
                f"plan for target {self.target!r} claims a pinned signature but names "
                "no identity to pin"
            )
        if self.degraded and self.signature_pinned:
            raise ReleaseError(
                f"plan for target {self.target!r} is degraded, so it must not claim a "
                "pinned signature"
            )
        # A plan that holds a signing key must have something that removes it.
        # A degraded plan is exempt for a reason, not by omission: it is the
        # profile that never imports a key in the first place, so it has nothing
        # to leak. Requiring a teardown step there would mean inventing a step
        # that does nothing, and a no-op teardown is the shape a real one
        # eventually forgets to fill in.
        if not self.degraded and not any(step.is_teardown for step in self.step_list):
            raise ReleaseError(
                f"plan for target {self.target!r} has no teardown: signing material "
                "would outlive the job"
            )

    @property
    def steps(self) -> Tuple[PlanStep, ...]:
        """The work steps, in order, without teardown."""

        return tuple(step for step in self.step_list if not step.is_teardown)

    @property
    def teardown(self) -> Tuple[PlanStep, ...]:
        """The steps that must run whatever happened, in order."""

        return tuple(step for step in self.step_list if step.is_teardown)

    @property
    def summary(self) -> str:
        if self.degraded:
            return f"{self.target} ({self.signing_mode}, DEGRADED: {self.degradation_reason})"
        return f"{self.target} ({self.signing_mode}, {self.signing_identity or 'unsigned'})"

    def step(self, name: str) -> PlanStep:
        for step in self.step_list:
            if step.name == name:
                return step
        raise ReleaseError(f"plan for target {self.target!r} has no step {name!r}")

    def step_names(self) -> List[str]:
        return [step.name for step in self.step_list]

    def assert_steps(self) -> List[PlanStep]:
        return [step for step in self.step_list if step.kind == STEP_ASSERT]

    def scratch_files(self) -> Tuple[str, ...]:
        collected: List[str] = list(self.scratch_paths)
        for step in self.step_list:
            for path in step.scratch_paths:
                if path not in collected:
                    collected.append(path)
        return tuple(collected)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": PLAN_KIND,
            "target": self.target,
            "adapter": self.adapter,
            "platform": self.platform,
            "build_strategy": self.build_strategy,
            "distribution": self.distribution,
            "signing": {
                "mode": self.signing_mode,
                "identity": self.signing_identity,
                "pinned_identity": self.pinned_identity,
                "signature_pinned": self.signature_pinned,
                "degraded": self.degraded,
                "degradation_reason": self.degradation_reason,
                "hardened_runtime": self.hardened_runtime,
                "timestamp": self.timestamp,
            },
            "artifacts": list(self.artifacts),
            "architectures": list(self.architectures),
            "universal": self.universal,
            "publishers": list(self.publishers),
            "required_secrets": list(self.required_secrets),
            "required_env": list(self.required_env),
            "scratch_paths": list(self.scratch_files()),
            "notes": list(self.notes),
            "steps": [step.describe() for step in self.step_list],
        }


def render(plan: ReleasePlan) -> Dict[str, Any]:
    return plan.describe()


__all__ = [
    "ENCODING_BASE64",
    "ENCODING_RAW",
    "ENV_ENV_PREFIX",
    "PLAN_KIND",
    "PLAN_SCHEMA",
    "SECRET_ENV_PREFIX",
    "SLOT_PATTERN",
    "STEP_ASSERT",
    "STEP_MATERIALIZE",
    "STEP_RUN",
    "SUPPORTED_ENCODINGS",
    "TEARDOWN",
    "PlanStep",
    "ReleaseError",
    "ReleasePlan",
    "SigningUnavailable",
    "StepFailure",
    "TargetNotExecutable",
    "env_slot",
    "render",
    "secret_slot",
]
