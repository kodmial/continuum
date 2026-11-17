"""Generic, platform-neutral release hooks.

The core deliberately knows no build system. A repository that is not an Apple
bundle still needs to plug its own build, sign, and verify commands into the
same state machine, and a repository that is an Apple bundle must not have its
product paths hardcoded in Continuum. This module is that seam.

A hooks document is repository-owned JSON, validated before anything runs:

```json
{
  "schema": "continuum.release-hooks/v1",
  "targets": [
    {
      "id": "linux-x64",
      "platform": "linux",
      "runner": "ubuntu-latest",
      "build": {"command": ["bash", "scripts/build.sh"]},
      "sign": {"command": ["bash", "scripts/sign.sh"], "identity": "acme-ci"},
      "verify": {"command": ["bash", "scripts/verify.sh"], "identity": "acme-ci"},
      "artifacts": [
        {"name": "widget-{version}-linux-x64.tar.gz",
         "path": "dist/widget.tar.gz", "type": "archive",
         "classifier": "linux-x64"}
      ]
    }
  ],
  "release_pr": {"command": ["bash", "scripts/release-pr.sh"]},
  "publishers": {"homebrew": {"command": ["bash", "scripts/tap.sh"]}}
}
```

Four properties are enforced rather than documented:

**An argument vector, never a shell.** A command is a list. There is no shell
expansion, so a version, a path, or a classifier taken from a repository is
data. Templates are limited to `{version}`, `{tag}`, `{target}`, and
`{source_sha}`; anything else is refused before the command is built.

**Secrets are references, never values.** A hooks document has no field for a
token, and a token-looking literal is refused. The job exposes a value under an
environment name; the document may name the command that reads it.

**The artifact set is measured, not claimed.** A build command that exits zero
has not produced a release. Every declared artifact must exist, and its size and
digest are read from the bytes. Signing re-measures, because signing changes
them.

**Verification is independent of the command.** The verify command may check a
signature; Continuum additionally re-hashes every artifact and compares it with
the manifest, and checks the pinned signer when one is declared. A hook that
prints "ok" cannot make a wrong digest pass.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import (
    SUPPORTED_ARTIFACT_TYPES,
    TYPE_CHECKSUMS,
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    ReleasePrRequest,
    ReleasePrResult,
    TargetSpec,
    VerificationReport,
    digest_file,
)
from .state import KEY_NAMESPACE

HOOKS_SCHEMA = "continuum.release-hooks/v1"
COMMAND_ADAPTER = "command"
MAX_COMMAND_ARGUMENTS = 64
MAX_ARGUMENT_LENGTH = 4096
DEFAULT_TIMEOUT = 3600

#: Literals that look like credentials. A hooks document is checked in and
#: reviewed, so a token pasted into it is a leaked token; refusing the document
#: is the only useful response.
_SECRET_LITERAL_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)

_TEMPLATE = re.compile(r"\{(version|tag|target|source_sha)\}")

_HOOK_KEYS = ("command", "identity", "attempts", "retry_on_exit")
_DOCUMENT_KEYS = (
    "schema",
    "targets",
    "release_pr",
    "publishers",
    "syncs",
)
_TARGET_KEYS = ("id", "platform", "runner", "build", "sign", "verify", "artifacts", "checksums")
_ARTIFACT_KEYS = ("name", "path", "type", "classifier", "media_type", "platform", "arch")


class HooksError(ValueError):
    """Raised when a hooks document, or a command run from it, is unsafe."""

    def __init__(self, message: str, *, code: str = "hooks-invalid", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class CommandOutput:
    argv: Tuple[str, ...]
    returncode: int
    output: str


def scrub_environment(values: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment a hook is given.

    Not the job's environment. A release hook gets the caller's declared values
    plus the few variables a process needs to start, so a credential the job
    happens to hold is not inherited by a build script that was never told
    about it.
    """

    environment: Dict[str, str] = {}
    for key in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment.update(values or {})
    return environment


def _reject_secret_literal(value: str, where: str) -> None:
    for pattern in _SECRET_LITERAL_PATTERNS:
        if pattern.search(value):
            raise HooksError(
                f"{where} looks like a credential. A checked-in hooks document must "
                "name the command that reads a secret, never carry the secret itself.",
                code="hooks-secret-literal",
            )


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HooksError(f"{where} must be a non-empty string")
    if any(char in value for char in "\n\r\0"):
        raise HooksError(f"{where} must not contain a newline or NUL")
    _reject_secret_literal(value, where)
    return value


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HooksError(f"{where} must be an object")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise HooksError(
            f"{where} carries unknown key(s) {', '.join(unknown)}; allowed: "
            + ", ".join(allowed)
        )


def _parse_command(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise HooksError(f"{where} must be a non-empty argument list")
    if len(value) > MAX_COMMAND_ARGUMENTS:
        raise HooksError(
            f"{where} has {len(value)} arguments; the limit is {MAX_COMMAND_ARGUMENTS}"
        )
    argv: List[str] = []
    for index, item in enumerate(value):
        text = _require_string(item, f"{where}[{index}]")
        if len(text) > MAX_ARGUMENT_LENGTH:
            raise HooksError(
                f"{where}[{index}] is longer than {MAX_ARGUMENT_LENGTH} characters"
            )
        if "\0" in text:
            raise HooksError(f"{where}[{index}] must not contain NUL")
        argv.append(text)
    return tuple(argv)


def _parse_hook(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _HOOK_KEYS, where)
    parsed: Dict[str, Any] = {}
    if mapping.get("command") is not None:
        parsed["command"] = _parse_command(mapping["command"], f"{where}.command")
    if mapping.get("identity") is not None:
        parsed["identity"] = _require_string(mapping["identity"], f"{where}.identity")
    if mapping.get("attempts") is not None:
        attempts = mapping["attempts"]
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
            raise HooksError(f"{where}.attempts must be an integer from 1 to 10")
        parsed["attempts"] = attempts
    if mapping.get("retry_on_exit") is not None:
        codes = mapping["retry_on_exit"]
        if not isinstance(codes, list):
            raise HooksError(f"{where}.retry_on_exit must be a list of exit codes")
        for index, code in enumerate(codes):
            if not isinstance(code, int) or isinstance(code, bool) or not 1 <= code <= 255:
                raise HooksError(
                    f"{where}.retry_on_exit[{index}] must be an integer from 1 to 255"
                )
        parsed["retry_on_exit"] = tuple(codes)
    if parsed.get("identity") is not None and "command" not in parsed:
        raise HooksError(
            f"{where}.identity pins a signer, but no command produces a signature"
        )
    return parsed


@dataclass(frozen=True)
class HookArtifact:
    """One file a target promises to produce."""

    name: str
    path: str
    type: str
    classifier: str = ""
    media_type: str = ""
    platform: str = ""
    arch: str = ""

    def resolve(self, values: Mapping[str, str]) -> "HookArtifact":
        return HookArtifact(
            name=_substitute(self.name, values, f"artifact name {self.name!r}"),
            path=_substitute(self.path, values, f"artifact path {self.path!r}"),
            type=self.type,
            classifier=self.classifier,
            media_type=self.media_type,
            platform=self.platform,
            arch=self.arch,
        )

    def describe(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "classifier": self.classifier,
        }


def _substitute(template: str, values: Mapping[str, str], where: str) -> str:
    result = _TEMPLATE.sub(lambda match: values.get(match.group(1), ""), template)
    if "{" in result or "}" in result:
        raise HooksError(
            f"{where} uses a template slot that is not one of "
            "{version}, {tag}, {target}, {source_sha}",
            code="hooks-template-invalid",
        )
    return result


def _validate_template(value: str, where: str) -> None:
    """Refuse a template slot at parse time rather than mid-build."""

    rendered = _TEMPLATE.sub("", value)
    if "{" in rendered or "}" in rendered:
        raise HooksError(
            f"{where} uses a template slot that is not one of "
            "{version}, {tag}, {target}, {source_sha}",
            code="hooks-template-invalid",
        )


def _parse_artifact(value: Any, where: str) -> HookArtifact:
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ARTIFACT_KEYS, where)
    name = _require_string(mapping.get("name"), f"{where}.name")
    path = _require_string(mapping.get("path"), f"{where}.path")
    _validate_template(name, f"{where}.name")
    _validate_template(path, f"{where}.path")
    kind = str(mapping.get("type") or "binary")
    if kind not in SUPPORTED_ARTIFACT_TYPES:
        raise HooksError(
            f"{where}.type is {kind!r}; supported: " + ", ".join(SUPPORTED_ARTIFACT_TYPES)
        )
    optional = {
        key: _require_string(mapping[key], f"{where}.{key}")
        for key in ("classifier", "media_type", "platform", "arch")
        if mapping.get(key) is not None
    }
    return HookArtifact(name=name, path=path, type=kind, **optional)


@dataclass(frozen=True)
class HookTarget:
    """One target as the hooks document declares it."""

    id: str
    platform: str
    runner: str
    artifacts: Tuple[HookArtifact, ...]
    build: Mapping[str, Any] = field(default_factory=dict)
    sign: Mapping[str, Any] = field(default_factory=dict)
    verify: Mapping[str, Any] = field(default_factory=dict)
    checksums: bool = True

    def __post_init__(self) -> None:
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", self.id or ""):
            raise HooksError(
                f"target id {self.id!r} must be a lowercase slug of letters, digits, "
                "'.', '_' or '-'"
            )
        if not self.artifacts:
            raise HooksError(
                f"target {self.id!r} declares no artifacts, so a build would produce "
                "nothing a release can publish"
            )
        names = [artifact.name for artifact in self.artifacts]
        if len(set(names)) != len(names):
            raise HooksError(
                f"target {self.id!r} declares the same artifact name twice"
            )

    def spec(self) -> TargetSpec:
        return TargetSpec(id=self.id, adapter=COMMAND_ADAPTER)

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "runner": self.runner,
            "artifacts": [artifact.describe() for artifact in self.artifacts],
            "build": bool(self.build.get("command")),
            "sign": bool(self.sign.get("command")),
            "verify": bool(self.verify.get("command")),
        }


def _parse_target(value: Any, where: str) -> HookTarget:
    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _TARGET_KEYS, where)
    artifacts_value = mapping.get("artifacts")
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise HooksError(f"{where}.artifacts must be a non-empty list")
    artifacts = tuple(
        _parse_artifact(item, f"{where}.artifacts[{index}]")
        for index, item in enumerate(artifacts_value)
    )
    checksums = mapping.get("checksums", True)
    if not isinstance(checksums, bool):
        raise HooksError(f"{where}.checksums must be true or false")
    target = HookTarget(
        id=_require_string(mapping.get("id"), f"{where}.id"),
        platform=str(mapping.get("platform") or "any"),
        runner=str(mapping.get("runner") or ""),
        artifacts=artifacts,
        build=_parse_hook(mapping.get("build"), f"{where}.build"),
        sign=_parse_hook(mapping.get("sign"), f"{where}.sign"),
        verify=_parse_hook(mapping.get("verify"), f"{where}.verify"),
        checksums=checksums,
    )
    for stage_name, hook in (
        ("build", target.build),
        ("sign", target.sign),
        ("verify", target.verify),
    ):
        for token in hook.get("command", ()):
            _validate_template(token, f"{where}.{stage_name} command argument")
    return target


class CommandAdapter:
    """A target adapter whose commands are repository-owned.

    Every execution is an argument vector. The adapter reports what the commands
    produced by measuring the files, and verifies independently of what the
    verify command says.
    """

    SUPPORTS_DRY_RUN = True
    name = COMMAND_ADAPTER

    def __init__(
        self,
        target: HookTarget,
        *,
        workdir: str = "",
        environment: Optional[Mapping[str, str]] = None,
        secrets: Sequence[str] = (),
        timeout: int = DEFAULT_TIMEOUT,
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._target = target
        self._workdir = workdir
        self._environment = scrub_environment(environment)
        self._secrets = tuple(value for value in secrets if value)
        self._timeout = timeout
        self._runner = runner or subprocess.run

    @property
    def target(self) -> HookTarget:
        return self._target

    def intent(self) -> str:
        return (
            f"run the repository's own build, sign, and verify commands for "
            f"{self._target.id!r}, then measure every artifact it produced"
        )

    def available(self) -> bool:
        for stage_name, hook in (
            ("build", self._target.build),
            ("sign", self._target.sign),
            ("verify", self._target.verify),
        ):
            argv = hook.get("command")
            if not argv:
                continue
            executable = str(argv[0])
            if self._resolves(executable):
                continue
            raise HooksError(
                f"target {self._target.id!r} needs {executable!r} for its {stage_name} "
                "hook, which this runner does not provide",
                code="toolchain-unavailable",
                retryable=True,
            )
        return True

    def _resolves(self, executable: str) -> bool:
        if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
            candidate = executable
            if not os.path.isabs(candidate) and self._workdir:
                candidate = os.path.join(self._workdir, candidate)
            return os.path.isfile(candidate)
        return shutil.which(executable) is not None

    def _values(self, request: BuildRequest) -> Dict[str, str]:
        return {
            "version": request.version,
            "tag": f"v{request.version}",
            "target": request.target.id,
            "source_sha": request.source_sha,
        }

    def _resolved_artifacts(self, request: BuildRequest) -> Tuple[HookArtifact, ...]:
        return tuple(
            artifact.resolve(self._values(request)) for artifact in self._target.artifacts
        )

    def _path(self, path: str) -> str:
        if os.path.isabs(path) or not self._workdir:
            return path
        return os.path.join(self._workdir, path)

    def _run(self, stage_name: str, request: BuildRequest, hook: Mapping[str, Any]) -> None:
        argv = hook.get("command")
        if not argv:
            return
        resolved = tuple(
            _substitute(token, self._values(request), f"{stage_name} command")
            for token in argv
        )
        environment = dict(self._environment)
        environment.update(
            {
                "CONTINUUM_RELEASE_VERSION": request.version,
                "CONTINUUM_RELEASE_TAG": f"v{request.version}",
                "CONTINUUM_SOURCE_SHA": request.source_sha,
                "CONTINUUM_TARGET_ID": request.target.id,
                "CONTINUUM_RELEASE_STAGE": stage_name,
                "CONTINUUM_RELEASE_KEY": request.key,
                "CONTINUUM_NAMESPACE": KEY_NAMESPACE,
            }
        )
        try:
            completed = self._runner(
                list(resolved),
                capture_output=True,
                text=True,
                shell=False,
                cwd=self._workdir or None,
                env=environment,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HooksError(
                f"the {stage_name} hook for {self._target.id!r} could not start "
                f"{resolved[0]!r}: {exc}",
                code="toolchain-unavailable",
                retryable=True,
            ) from None
        except subprocess.TimeoutExpired:
            raise HooksError(
                f"the {stage_name} hook for {self._target.id!r} did not finish within "
                f"{self._timeout}s",
                code="hook-timeout",
                retryable=True,
            ) from None
        except OSError as exc:
            raise HooksError(
                f"the {stage_name} hook for {self._target.id!r} could not start: {exc}",
                code="hook-not-started",
            ) from None
        output = _redact(
            str(getattr(completed, "stdout", "") or "")
            + str(getattr(completed, "stderr", "") or ""),
            self._secrets,
        )
        code = int(getattr(completed, "returncode", 1))
        if code != 0:
            retryable = code in tuple(hook.get("retry_on_exit") or ())
            raise HooksError(
                f"the {stage_name} hook for {self._target.id!r} exited with status {code}"
                + (f": {output.strip()[:1000]}" if output.strip() else ""),
                code=f"{stage_name}-hook-failed",
                retryable=retryable,
            )

    def build(self, request: BuildRequest) -> ArtifactManifest:
        artifacts = self._resolved_artifacts(request)
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=COMMAND_ADAPTER,
            source_sha=request.source_sha,
            version=request.version,
        )
        if request.dry_run:
            for artifact in artifacts:
                builder.declare(
                    artifact.name,
                    self._path(artifact.path),
                    artifact.type,
                    platform=artifact.platform,
                    arch=artifact.arch,
                    classifier=artifact.classifier,
                    media_type=artifact.media_type,
                )
            return builder.build()

        self._run("build", request, self._target.build)
        for artifact in artifacts:
            path = self._path(artifact.path)
            if not os.path.isfile(path):
                raise HooksError(
                    f"target {self._target.id!r} promised {artifact.name!r} at "
                    f"{artifact.path!r}, which the build did not produce",
                    code="artifact-missing",
                )
            builder.record(
                artifact.name,
                path,
                artifact.type,
                platform=artifact.platform,
                arch=artifact.arch,
                classifier=artifact.classifier,
                media_type=artifact.media_type,
            )
        if self._target.checksums and not builder.build().of_type(TYPE_CHECKSUMS):
            self._write_checksums(builder, request, artifacts)
        return builder.build()

    def _write_checksums(
        self,
        builder: ManifestBuilder,
        request: BuildRequest,
        artifacts: Sequence[HookArtifact],
    ) -> None:
        first = artifacts[0]
        directory = os.path.dirname(self._path(first.path)) or self._workdir or "."
        name = f"{request.target.id}-{request.version}-SHA256SUMS.txt"
        path = builder.build().write_checksums(directory, name)
        builder.record(
            name,
            path,
            TYPE_CHECKSUMS,
            classifier="checksums",
            media_type="text/plain",
        )

    def _remeasure(self, manifest: ArtifactManifest, identity: str) -> ArtifactManifest:
        artifacts: List[Artifact] = []
        for artifact in manifest.artifacts:
            if artifact.declared:
                artifacts.append(artifact)
                continue
            size, digest = digest_file(artifact.path)
            if artifact.type == TYPE_CHECKSUMS:
                artifacts.append(
                    Artifact(
                        target=artifact.target,
                        source_sha=artifact.source_sha,
                        version=artifact.version,
                        name=artifact.name,
                        path=artifact.path,
                        type=artifact.type,
                        size=size,
                        digest=digest,
                        digest_algorithm=artifact.digest_algorithm,
                        platform=artifact.platform,
                        arch=artifact.arch,
                        classifier=artifact.classifier,
                        media_type=artifact.media_type,
                    )
                )
                continue
            artifacts.append(
                Artifact(
                    target=artifact.target,
                    source_sha=artifact.source_sha,
                    version=artifact.version,
                    name=artifact.name,
                    path=artifact.path,
                    type=artifact.type,
                    size=size,
                    digest=digest,
                    digest_algorithm=artifact.digest_algorithm,
                    platform=artifact.platform,
                    arch=artifact.arch,
                    classifier=artifact.classifier,
                    media_type=artifact.media_type,
                    signing="signed" if identity else "unsigned",
                    signing_identity=identity if identity else "",
                )
            )
        return ArtifactManifest(
            target=manifest.target,
            source_sha=manifest.source_sha,
            version=manifest.version,
            adapter=manifest.adapter,
            artifacts=tuple(artifacts),
        )

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        if request.dry_run:
            return manifest
        identity = str(self._target.sign.get("identity") or "")
        if not self._target.sign.get("command"):
            return manifest
        self._run("sign", request, self._target.sign)
        signed = self._remeasure(manifest, identity)
        if not self._target.checksums:
            return signed
        existing = next(
            (item for item in signed.artifacts if item.type == TYPE_CHECKSUMS), None
        )
        body = tuple(item for item in signed.artifacts if item.type != TYPE_CHECKSUMS)
        if not body:
            return signed
        builder = ManifestBuilder(
            target=signed.target,
            adapter=COMMAND_ADAPTER,
            source_sha=signed.source_sha,
            version=signed.version,
        )
        for artifact in body:
            builder.replace(artifact)
        name = (
            existing.name
            if existing is not None
            else f"{request.target.id}-{request.version}-SHA256SUMS.txt"
        )
        directory = (
            os.path.dirname(existing.path)
            if existing is not None
            else os.path.dirname(body[0].path)
        ) or self._workdir or "."
        path = builder.build().write_checksums(directory, name)
        builder.record(
            name,
            path,
            TYPE_CHECKSUMS,
            classifier="checksums",
            media_type="text/plain",
        )
        return builder.build()

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="hook-planned",
                detail=", ".join(artifact.name for artifact in manifest.artifacts),
                verified_by=f"{COMMAND_ADAPTER}:{self._target.id}",
            )
        self._run("verify", request, self._target.verify)
        expected_identity = str(self._target.verify.get("identity") or "")
        failures: List[str] = []
        for artifact in manifest.artifacts:
            if artifact.declared:
                failures.append(f"{artifact.name} is declared, not built")
                continue
            if not os.path.isfile(artifact.path):
                failures.append(f"{artifact.name} is missing at {artifact.path}")
                continue
            size, digest = digest_file(artifact.path)
            if artifact.type == TYPE_CHECKSUMS:
                if digest != manifest.checksums_digest():
                    failures.append(
                        f"{artifact.name} does not describe the artifacts it was "
                        "recorded beside"
                    )
                continue
            if (size, digest) != (artifact.size, artifact.digest):
                failures.append(
                    f"{artifact.name} does not match the digest it was recorded with"
                )
                continue
            if expected_identity:
                if not artifact.signed or artifact.signing_identity != expected_identity:
                    failures.append(
                        f"{artifact.name} is not signed by {expected_identity!r}"
                    )
        if failures:
            return VerificationReport(
                verified=False,
                code="hook-verification-failed",
                detail="; ".join(failures),
                failures=tuple(failures),
            )
        return VerificationReport(
            verified=True,
            code="hook-verified",
            detail=", ".join(artifact.name for artifact in manifest.artifacts),
            verified_by=f"{COMMAND_ADAPTER}:{self._target.id}",
        )


def _redact(text: str, secrets: Sequence[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned


class CommandReleasePr:
    """The release pull request port, driven by a repository-owned command.

    The command prints one JSON object. It never receives a token as an
    argument; the job exposes the credential under an environment name, and the
    command is what the repository already owns. A document that tried to inline
    a token is refused by the document parser.
    """

    SUPPORTS_DRY_RUN = True
    name = "command-release-pr"

    def __init__(
        self,
        command: Sequence[str],
        *,
        workdir: str = "",
        environment: Optional[Mapping[str, str]] = None,
        secrets: Sequence[str] = (),
        timeout: int = DEFAULT_TIMEOUT,
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not command:
            raise HooksError("a release-pr hook needs a command")
        self._argv = tuple(command)
        self._workdir = workdir
        self._environment = scrub_environment(environment)
        self._secrets = tuple(value for value in secrets if value)
        self._timeout = timeout
        self._runner = runner or subprocess.run

    def intent(self) -> str:
        return "ask the repository's release-pr command whether a pull request exists"

    def ensure(self, request: ReleasePrRequest) -> ReleasePrResult:
        if request.dry_run:
            return ReleasePrResult(
                state="absent",
                reason="a plan does not open a release pull request",
                version=request.version,
            )
        environment = dict(self._environment)
        environment.update(
            {
                "CONTINUUM_RELEASE_VERSION": request.version,
                "CONTINUUM_RELEASE_TAG": request.tag,
                "CONTINUUM_SOURCE_SHA": request.source_sha,
                "CONTINUUM_RELEASE_KEY": request.key,
                "CONTINUUM_RELEASE_STAGE": "release-pr",
            }
        )
        try:
            completed = self._runner(
                list(self._argv),
                capture_output=True,
                text=True,
                shell=False,
                cwd=self._workdir or None,
                env=environment,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HooksError(
                f"the release-pr command could not start {self._argv[0]!r}: {exc}",
                code="toolchain-unavailable",
                retryable=True,
            ) from None
        except subprocess.TimeoutExpired:
            raise HooksError(
                "the release-pr command did not finish in time",
                code="release-pr-timeout",
                retryable=True,
            ) from None
        output = _redact(
            str(getattr(completed, "stdout", "") or ""), self._secrets
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            raise HooksError(
                f"the release-pr command exited with status {completed.returncode}"
                + (f": {output.strip()[:500]}" if output.strip() else ""),
                code="release-pr-failed",
            )
        return _parse_release_pr(output, request)


def _parse_release_pr(output: str, request: ReleasePrRequest) -> ReleasePrResult:
    payload = None
    for line in reversed([line for line in (output or "").splitlines() if line.strip()]):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise HooksError(
            "the release-pr command did not print a JSON object describing the pull "
            "request",
            code="release-pr-output-invalid",
        )
    state = str(payload.get("state") or "")
    if state == "absent":
        reason = str(payload.get("reason") or "")
        if not reason:
            raise HooksError(
                "a release-pr command that reports no pull request must say why",
                code="release-pr-output-invalid",
            )
    try:
        return ReleasePrResult(
            state=state,
            number=int(payload.get("number") or 0),
            version=str(payload.get("version") or request.version),
            url=str(payload.get("url") or ""),
            reason=str(payload.get("reason") or ""),
        )
    except ContractError as exc:
        raise HooksError(
            f"the release-pr command reported an invalid state: {exc}",
            code="release-pr-output-invalid",
        ) from exc


@dataclass(frozen=True)
class HooksPlan:
    """Everything a hooks document contributes to one release."""

    targets: Tuple[HookTarget, ...]
    release_pr: Optional[CommandReleasePr] = None
    publishers: Tuple[Any, ...] = ()
    syncs: Tuple[Any, ...] = ()

    def specs(self) -> Tuple[TargetSpec, ...]:
        return tuple(target.spec() for target in self.targets)

    def runners(self) -> Dict[str, str]:
        return {target.id: target.runner for target in self.targets if target.runner}

    def describe(self) -> Dict[str, Any]:
        return {
            "targets": [target.describe() for target in self.targets],
            "release_pr": self.release_pr is not None,
            "publishers": [getattr(item, "name", "") for item in self.publishers],
            "syncs": [getattr(item, "name", "") for item in self.syncs],
        }


def parse_hooks(
    payload: Mapping[str, Any],
    *,
    workdir: str = "",
    environment: Optional[Mapping[str, str]] = None,
    secrets: Sequence[str] = (),
) -> HooksPlan:
    """Validate a hooks document and turn it into adapters and publishers."""

    from .publishers import CommandSync

    mapping = _require_mapping(payload, "hooks")
    _reject_unknown(mapping, _DOCUMENT_KEYS, "hooks")
    schema = str(mapping.get("schema") or "")
    if schema != HOOKS_SCHEMA:
        raise HooksError(
            f"unknown hooks schema {schema!r}; expected {HOOKS_SCHEMA!r}",
            code="hooks-schema-unknown",
        )
    targets_value = mapping.get("targets")
    if not isinstance(targets_value, list) or not targets_value:
        raise HooksError("hooks.targets must be a non-empty list")
    targets = tuple(
        _parse_target(item, f"hooks.targets[{index}]")
        for index, item in enumerate(targets_value)
    )
    ids = [target.id for target in targets]
    if len(set(ids)) != len(ids):
        raise HooksError("hooks.targets declares the same target id twice")

    release_pr = None
    if mapping.get("release_pr") is not None:
        hook = _parse_hook(mapping["release_pr"], "hooks.release_pr")
        command = hook.get("command")
        if not command:
            raise HooksError("hooks.release_pr must carry a command")
        release_pr = CommandReleasePr(
            command,
            workdir=workdir,
            environment=environment,
            secrets=secrets,
        )

    publishers = _parse_syncs(
        mapping.get("publishers"), "hooks.publishers", CommandSync, workdir, environment, secrets
    )
    syncs = _parse_syncs(
        mapping.get("syncs"), "hooks.syncs", CommandSync, workdir, environment, secrets
    )
    return HooksPlan(
        targets=targets,
        release_pr=release_pr,
        publishers=publishers,
        syncs=syncs,
    )


def _parse_syncs(
    value: Any,
    where: str,
    factory: Callable[..., Any],
    workdir: str,
    environment: Optional[Mapping[str, str]],
    secrets: Sequence[str],
) -> Tuple[Any, ...]:
    if value is None:
        return ()
    mapping = _require_mapping(value, where)
    items: List[Any] = []
    seen: List[str] = []
    for destination, hook_value in mapping.items():
        name = str(destination)
        if name in seen:
            raise HooksError(f"{where} names {name!r} twice")
        seen.append(name)
        hook = _parse_hook(hook_value, f"{where}.{name}")
        command = hook.get("command")
        if not command:
            raise HooksError(f"{where}.{name} must carry a command")
        items.append(
            factory(
                name,
                command,
                attempts=int(hook.get("attempts", 3)),
                retry_on_exit=tuple(hook.get("retry_on_exit") or (75, 429)),
                workdir=workdir,
                environment=environment,
                secrets=secrets,
            )
        )
    return tuple(items)


def load_hooks(
    path: str,
    *,
    workdir: str = "",
    environment: Optional[Mapping[str, str]] = None,
    secrets: Sequence[str] = (),
) -> HooksPlan:
    candidate = path
    if workdir and not os.path.isabs(candidate):
        candidate = os.path.join(workdir, candidate)
    try:
        with open(candidate, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise HooksError(
            f"the release hooks document at {candidate} could not be read: {exc}",
            code="hooks-unreadable",
        ) from exc
    except ValueError as exc:
        raise HooksError(
            f"the release hooks document at {candidate} is not valid JSON: {exc}",
            code="hooks-invalid",
        ) from exc
    return parse_hooks(
        payload, workdir=workdir, environment=environment, secrets=secrets
    )


class CommandAdapterSet:
    """Every hook target behind one adapter name.

    The core looks an adapter up by the name a target declares, and every hook
    target declares the same generic name. This dispatches by target id, so two
    hook targets can build on different platforms without the core learning
    which platform a build is on.
    """

    SUPPORTS_DRY_RUN = True
    name = COMMAND_ADAPTER

    def __init__(self, adapters: Mapping[str, CommandAdapter]) -> None:
        if not adapters:
            raise HooksError("a hook adapter set needs at least one target")
        self._adapters = dict(adapters)

    def intent(self) -> str:
        return (
            "run each target's repository-owned build, sign, and verify commands "
            "and measure what they produced"
        )

    def available(self) -> bool:
        return all(adapter.available() for adapter in self._adapters.values())

    def _for(self, request: BuildRequest) -> CommandAdapter:
        adapter = self._adapters.get(request.target.id)
        if adapter is None:
            raise HooksError(
                f"no hook command is configured for target {request.target.id!r}; "
                "registered: " + ", ".join(sorted(self._adapters)),
                code="target-not-configured",
            )
        return adapter

    def build(self, request: BuildRequest) -> ArtifactManifest:
        return self._for(request).build(request)

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        return self._for(request).sign(request, manifest)

    def verify(
        self, request: BuildRequest, manifest: ArtifactManifest
    ) -> VerificationReport:
        return self._for(request).verify(request, manifest)


def build_adapters(
    plan: HooksPlan,
    *,
    workdir: str = "",
    environment: Optional[Mapping[str, str]] = None,
    secrets: Sequence[str] = (),
) -> Dict[str, Any]:
    adapters = {
        target.id: CommandAdapter(
            target,
            workdir=workdir,
            environment=environment,
            secrets=secrets,
        )
        for target in plan.targets
    }
    return {COMMAND_ADAPTER: CommandAdapterSet(adapters)}


__all__ = [
    "COMMAND_ADAPTER",
    "CommandAdapter",
    "CommandOutput",
    "CommandReleasePr",
    "HookArtifact",
    "HookTarget",
    "HooksError",
    "HooksPlan",
    "HOOKS_SCHEMA",
    "build_adapters",
    "load_hooks",
    "parse_hooks",
    "scrub_environment",
]
