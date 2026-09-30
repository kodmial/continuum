"""What a release can be built by, and the matrix that says so.

A reusable workflow is a *capability on someone else's repository*, so the line
between "this workflow can build that" and "this repository wants that" has to be
drawn in code rather than in whatever the caller passed. A matrix built from a
workflow input is a matrix the caller chose: `build_argv: ["curl", "…", "|",
"sh"]` and a target id that names nothing in the release policy are both one
missing check away from running on a runner that holds signing material.

So the shape of every build is in this table, in a repository, and the matrix is
*derived* from configuration and then re-checked against the table on the way
back in. A row is not trusted because it came back from the workflow; it is
trusted because it matches an entry here, for a target the policy declares, at
the commit the release is pinned to.

The table is also the answer to "which platforms can this release plane run",
which is the question a caller has and the question a claim about a release
cannot answer. Apple, Android, JVM, and the generic table are all here with
their runners, their toolchain needs, and the secrets their builds read — so
adding a platform is adding a row, not editing a loop and hoping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .. import config as config_module
from .contract import ContractError, require_source_sha

ENTRYPOINT_SCHEMA = "continuum.release-matrix/v1"

#: Runners are chosen per entrypoint, not per job, because a build that needs a
#: macOS keychain cannot be relocated to a Linux runner by a matrix override and
#: a build that needs a JDK cannot be relocated the other way.
RUNNER_MACOS = "macos-14"
RUNNER_UBUNTU = "ubuntu-24.04"

_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class EntrypointError(ContractError):
    """A target cannot be built, or a matrix cannot be trusted.

    A `ContractError` so the release core classifies this as the resolve stage's
    own failure, with a code, rather than as a crash in a component.
    """


@dataclass(frozen=True)
class ReleaseEntrypoint:
    """One way this release plane knows how to build something.

    Every field is here so that a caller never has to be told what to run: the
    argv, the runner, the toolchain, the secrets, and the outputs. A capability
    that has to be described in the calling workflow is a capability the calling
    workflow can change.
    """

    adapter: str
    title: str
    argv: Tuple[str, ...]
    runner: str = RUNNER_UBUNTU
    toolchain: str = ""
    signing: bool = False
    secrets: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ("manifest", "journal")
    config_supported: bool = True

    def __post_init__(self) -> None:
        if not self.adapter:
            raise EntrypointError("a release entrypoint must name the adapter it builds for")
        if not self.argv:
            raise EntrypointError(
                f"the {self.adapter!r} entrypoint has no command. An entrypoint that "
                "cannot say what to run would leave the command to the caller, which is "
                "the thing this table exists to prevent"
            )
        if not self.runner:
            raise EntrypointError(f"the {self.adapter!r} entrypoint has no runner")

    def secret_names(self) -> Tuple[str, ...]:
        return self.secrets

    def describe(self) -> Dict[str, Any]:
        return {
            "adapter": self.adapter,
            "title": self.title,
            "argv": list(self.argv),
            "runner": self.runner,
            "toolchain": self.toolchain,
            "signing": self.signing,
            "secrets": list(self.secrets),
            "outputs": list(self.outputs),
        }


APPLE = config_module.ADAPTER_APPLE
ANDROID = "android"
JVM = "jvm"
GENERIC = "generic"

#: The static table. Adapters configuration does not yet validate are declared
#: with `config_supported=False` rather than left out, so "this release plane
#: cannot build that yet" and "this workflow has never heard of that" are
#: different answers.
ENTRYPOINTS: Dict[str, ReleaseEntrypoint] = {
    APPLE: ReleaseEntrypoint(
        adapter=APPLE,
        title="macOS application bundle",
        argv=("continuum", "release", "target"),
        runner=RUNNER_MACOS,
        toolchain="swift",
        signing=True,
        secrets=("CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"),
    ),
    ANDROID: ReleaseEntrypoint(
        adapter=ANDROID,
        title="Android package",
        argv=("continuum", "release", "target"),
        runner=RUNNER_UBUNTU,
        toolchain="jdk-17",
        signing=True,
        secrets=("CONTINUUM_ANDROID_KEYSTORE", "CONTINUUM_ANDROID_KEYSTORE_PASSWORD"),
        config_supported=False,
    ),
    JVM: ReleaseEntrypoint(
        adapter=JVM,
        title="JVM library",
        argv=("continuum", "release", "target"),
        runner=RUNNER_UBUNTU,
        toolchain="jdk-17",
        signing=False,
        secrets=("CONTINUUM_GPG_PRIVATE_KEY", "CONTINUUM_GPG_PASSPHRASE"),
        config_supported=False,
    ),
    GENERIC: ReleaseEntrypoint(
        adapter=GENERIC,
        title="Generic build",
        argv=("continuum", "release", "target"),
        runner=RUNNER_UBUNTU,
        toolchain="",
        signing=False,
        secrets=(),
    ),
}


def supported() -> Tuple[str, ...]:
    return tuple(sorted(ENTRYPOINTS))


def get(adapter: str) -> ReleaseEntrypoint:
    entrypoint = ENTRYPOINTS.get(adapter)
    if entrypoint is None:
        raise EntrypointError(
            f"no release entrypoint for adapter {adapter!r}; this release plane can "
            f"build: {', '.join(supported())}"
        )
    return entrypoint


@dataclass(frozen=True)
class MatrixTarget:
    """One row: a target, the command that builds it, and what it must produce.

    The row is the whole of what a build job is allowed to know. It carries the
    argv rather than a build script to execute, the secrets it *needs* rather
    than every secret the caller has, and the commit it is bound to — so a row
    cannot be edited into a different job without failing the check that reads it
    back.
    """

    target: str
    adapter: str
    runner: str
    argv: Tuple[str, ...]
    source_sha: str
    version: str
    toolchain: str = ""
    signing: bool = False
    secrets: Tuple[str, ...] = ()
    fragment: str = ""
    platform: str = ""
    architectures: Tuple[str, ...] = ()
    artifacts: Tuple[str, ...] = ()
    declared: bool = False

    def __post_init__(self) -> None:
        if not _TARGET_RE.match(self.target or ""):
            raise EntrypointError(
                f"matrix target {self.target!r} must be a lowercase slug. It names a row in "
                "the release policy, a journal key, and a fragment on disk"
            )
        require_source_sha(self.source_sha, f"matrix target {self.target!r}")
        if not _VERSION_RE.match(self.version or ""):
            raise EntrypointError(
                f"matrix target {self.target!r} carries version {self.version!r}, which is "
                "not a version: it becomes a tag, an asset name, and an identity"
            )
        if not self.argv:
            raise EntrypointError(
                f"matrix target {self.target!r} has no command to run. The build job would "
                "be a job that does nothing and reports success"
            )
        for name in self.secrets:
            if not _SECRET_RE.match(name):
                raise EntrypointError(
                    f"matrix target {self.target!r} names secret {name!r}, which is not a "
                    "secret name. Anything lower-case here is a value that would be passed "
                    "in the clear"
                )
        if not self.fragment:
            raise EntrypointError(
                f"matrix target {self.target!r} has no fragment path. A build whose result "
                "the transaction cannot find is a build that did not happen"
            )
        if self.declared and self.secrets:
            raise EntrypointError(
                f"matrix target {self.target!r} is declared rather than built, and was still "
                "given signing material. A target that will not run has no use for a key, "
                "and a key handed to a job that does not run is a key that leaks"
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "adapter": self.adapter,
            "runner": self.runner,
            "argv": list(self.argv),
            "source_sha": self.source_sha,
            "version": self.version,
            "toolchain": self.toolchain,
            "signing": self.signing,
            "secrets": list(self.secrets),
            "fragment": self.fragment,
            "platform": self.platform,
            "architectures": list(self.architectures),
            "artifacts": list(self.artifacts),
            "declared": self.declared,
        }

    @classmethod
    def from_describe(cls, payload: Mapping[str, Any]) -> "MatrixTarget":
        if not isinstance(payload, Mapping):
            raise EntrypointError("a matrix row must be an object, not a string")
        missing = [
            key
            for key in ("target", "adapter", "runner", "argv", "source_sha", "version", "fragment")
            if key not in payload
        ]
        if missing:
            raise EntrypointError(
                "matrix row is missing " + ", ".join(missing) + f"; got {sorted(payload)}"
            )
        argv = payload["argv"]
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise EntrypointError(
                f"matrix row {payload.get('target')!r} has an argv that is not a list of "
                "strings. A command is a list of arguments, never a string a shell splits"
            )
        return cls(
            target=str(payload["target"]),
            adapter=str(payload["adapter"]),
            runner=str(payload["runner"]),
            argv=tuple(argv),
            source_sha=str(payload["source_sha"]),
            version=str(payload["version"]),
            toolchain=str(payload.get("toolchain", "")),
            signing=bool(payload.get("signing", False)),
            secrets=tuple(str(item) for item in payload.get("secrets", ())),
            fragment=str(payload["fragment"]),
            platform=str(payload.get("platform", "")),
            architectures=tuple(str(item) for item in payload.get("architectures", ())),
            artifacts=tuple(str(item) for item in payload.get("artifacts", ())),
            declared=bool(payload.get("declared", False)),
        )


@dataclass(frozen=True)
class ReleaseMatrix:
    """The validated set of targets one release will build, for one version.

    Carries the identity of the release it belongs to — the version, the commit,
    and the key the transaction is idempotent on — so a matrix cannot be carried
    from one release to another. That is the failure this type exists to make
    impossible: building version 1.0.0 from a commit that was approved for
    0.9.0, and discovering it at publication.
    """

    version: str
    source_sha: str
    channel: str = "stable"
    key: str = ""
    targets: Tuple[MatrixTarget, ...] = field(default_factory=tuple)
    dry_run: bool = False

    def __post_init__(self) -> None:
        require_source_sha(self.source_sha, "release matrix")
        if not _VERSION_RE.match(self.version or ""):
            raise EntrypointError(
                f"release matrix version {self.version!r} is not a version: it becomes a tag, "
                "an asset name, and an identity"
            )
        if not self.channel:
            raise EntrypointError("a release matrix must name its channel")
        if not self.targets:
            raise EntrypointError(
                "a release matrix with no targets would publish an empty release. If the "
                "policy declares none, the answer is to not release, not to release nothing"
            )
        seen: Dict[str, str] = {}
        for target in self.targets:
            previous = seen.get(target.target)
            if previous is not None:
                raise EntrypointError(
                    f"matrix lists target {target.target!r} twice. Two jobs for one target "
                    "would build the same bytes twice and race to publish them under one name"
                )
            seen[target.target] = target.adapter
            if target.source_sha != self.source_sha or target.version != self.version:
                raise EntrypointError(
                    f"matrix target {target.target!r} is for {target.version} at "
                    f"{target.source_sha[:7]}, but the matrix is {self.version} at "
                    f"{self.source_sha[:7]}. Every target in a release is built from the same "
                    "commit and shipped under the same version"
                )
        if not self.key:
            object.__setattr__(
                self, "key", f"release/{self.version}/{self.source_sha}/{self.channel}"
            )

    @property
    def target_ids(self) -> Tuple[str, ...]:
        return tuple(target.target for target in self.targets)

    def target(self, target_id: str) -> MatrixTarget:
        for target in self.targets:
            if target.target == target_id:
                return target
        raise EntrypointError(
            f"the release matrix has no target {target_id!r}; it has "
            f"{', '.join(self.target_ids)}"
        )

    @property
    def required_secrets(self) -> Tuple[str, ...]:
        return tuple(sorted({name for target in self.targets for name in target.secrets}))

    @property
    def buildable_ids(self) -> Tuple[str, ...]:
        """The targets a runner can actually build.

        Distinct from `target_ids` because a declared target — one an adapter
        cannot produce yet — belongs in the release and gets no job. The
        transaction needs the difference: it must wait for every buildable
        target and must not wait forever for one that has no runner.
        """

        return tuple(target.target for target in self.targets if not target.declared)

    @property
    def declared_ids(self) -> Tuple[str, ...]:
        return tuple(target.target for target in self.targets if target.declared)

    def describe(self) -> Dict[str, Any]:
        return {
            "schema": ENTRYPOINT_SCHEMA,
            "version": self.version,
            "source_sha": self.source_sha,
            "channel": self.channel,
            "key": self.key,
            "dry_run": self.dry_run,
            "targets": [target.describe() for target in self.targets],
        }

    def to_json(self) -> str:
        return json.dumps(self.describe(), sort_keys=True, separators=(",", ":"))

    def artifact(self) -> str:
        """The matrix as a single line, for a workflow input or an output.

        One line because the alternative is a here-doc in a workflow, and a
        workflow is not where a matrix should be assembled.
        """

        return self.to_json()

    def summary(self) -> str:
        return (
            f"{len(self.targets)} target(s) [{', '.join(self.target_ids)}] for "
            f"{self.version} at {self.source_sha[:7]} on {self.channel}"
        )

    @classmethod
    def from_describe(cls, payload: Mapping[str, Any]) -> "ReleaseMatrix":
        """Read a matrix back, re-running every check its constructor made.

        A matrix crosses a job boundary as a file, so reading it is a moment at
        which a rewritten matrix could be acted on. Reconstructing the value
        rather than trusting the mapping is what makes an edit fail here instead
        of becoming a release.
        """

        if not isinstance(payload, Mapping):
            raise EntrypointError(
                f"a release matrix is a mapping, not {type(payload).__name__}"
            )
        schema = payload.get("schema")
        if schema != ENTRYPOINT_SCHEMA:
            raise EntrypointError(
                f"this document declares schema {schema!r}; a release matrix is "
                f"{ENTRYPOINT_SCHEMA!r} and nothing else may be read as one"
            )
        rows = payload.get("targets")
        if not isinstance(rows, (list, tuple)):
            raise EntrypointError(
                f"a release matrix's targets must be a list, not {type(rows).__name__}"
            )
        return cls(
            version=payload.get("version") or "",
            source_sha=payload.get("source_sha") or "",
            channel=payload.get("channel") or "stable",
            key=payload.get("key") or "",
            targets=tuple(MatrixTarget.from_describe(row) for row in rows),
            dry_run=bool(payload.get("dry_run")),
        )


def resolve_matrix(
    config: config_module.ContinuumConfig,
    *,
    version: str,
    source_sha: str,
    channel: str = "stable",
    root: str = "runs",
    include_declared: bool = False,
    dry_run: bool = False,
) -> ReleaseMatrix:
    """The matrix for one release, from the policy and the static table.

    The order of the work is the argument: the policy decides which targets
    exist, the table decides how each is built, and this function refuses
    anything either of them cannot answer. A target the policy declares but the
    table cannot build is a failure at resolve time, not a job that fails twenty
    minutes later with a message about a missing tool.
    """

    require_source_sha(source_sha, "release matrix")
    declared = config.release.targets
    if not declared:
        raise EntrypointError(
            "the release policy declares no targets, so there is nothing to release. A "
            "release with no targets is either a configuration mistake or a request for "
            "an empty release, and both should stop here"
        )
    rows: List[MatrixTarget] = []
    unbuildable: List[config_module.ReleaseTarget] = []
    for target in declared:
        if not _TARGET_RE.match(target.id or ""):
            raise EntrypointError(
                f"release target {target.id!r} must be a lowercase slug. It names a matrix "
                "row, a journal key, and a fragment on disk"
            )
        entrypoint = ENTRYPOINTS.get(target.adapter)
        if entrypoint is None:
            raise EntrypointError(
                f"release target {target.id!r} names adapter {target.adapter!r}, which this "
                f"release plane has no entrypoint for; it can build: {', '.join(supported())}"
            )
        if not entrypoint.config_supported and target.adapter not in config_module.SUPPORTED_RELEASE_ADAPTERS:
            raise EntrypointError(
                f"release target {target.id!r} names adapter {target.adapter!r}, which this "
                "release plane can build but the release policy does not yet validate. "
                "Configuration is what says a repository wants a build, so the two have to "
                "agree before a runner is asked for one"
            )
        if not target.is_mvp_executable and not include_declared:
            unbuildable.append(target)
            continue
        if not target.is_mvp_executable:
            rows.append(_declared_row(target, entrypoint, source_sha, version, root))
            continue
        rows.append(
            MatrixTarget(
                target=target.id,
                adapter=target.adapter,
                runner=entrypoint.runner,
                argv=entrypoint.argv,
                source_sha=source_sha,
                version=version,
                toolchain=entrypoint.toolchain,
                signing=entrypoint.signing and bool(target.signing.secret_names()),
                secrets=_required_secrets(entrypoint, target),
                fragment=fragment_path(root, target.id),
                platform=target.platform,
                architectures=tuple(target.architectures),
                artifacts=tuple(target.artifacts),
            )
        )
    if unbuildable:
        # Fail closed rather than publish a subset. A release that ships two of
        # three declared targets announces less than its policy promised, and
        # nothing downstream can tell the difference between a target that was
        # left out on purpose and one that was dropped by a mistake.
        raise EntrypointError(
            "the release policy declares "
            + ", ".join(sorted(target.id for target in unbuildable))
            + ", which this release cannot build. Publishing the rest would announce a "
            "release with fewer targets than the policy lists. Make the target buildable, "
            "or remove it from the policy — or pass include_declared to release the "
            "others and report this one as declared"
        )
    return ReleaseMatrix(
        version=version,
        source_sha=source_sha,
        channel=channel,
        targets=tuple(rows),
        dry_run=dry_run,
    )


def _declared_row(
    target: config_module.ReleaseTarget,
    entrypoint: ReleaseEntrypoint,
    source_sha: str,
    version: str,
    root: str,
) -> MatrixTarget:
    """A target the policy declares but this release cannot build.

    A row so a release can *name* what it is missing instead of shipping a
    shorter asset set nobody can account for. It carries no secrets and no
    command to sign with, and it gets no build job: a row with no runner must
    not be waited for, or the release would hang on a target that can never
    produce a fragment.

    The row is only produced when the caller passed `include_declared`, so
    reaching one is always a deliberate request to release the rest. The
    transaction then reports these ids as declared-but-not-shipped in the
    release body, the job summary, and the result outputs. Naming the gap is
    the useful part; hiding it while shipping the rest is the failure.
    """

    return MatrixTarget(
        target=target.id,
        adapter=target.adapter,
        runner=entrypoint.runner,
        argv=entrypoint.argv,
        source_sha=source_sha,
        version=version,
        toolchain=entrypoint.toolchain,
        signing=False,
        secrets=(),
        fragment=fragment_path(root, target.id),
        platform=target.platform,
        architectures=tuple(target.architectures),
        artifacts=tuple(target.artifacts),
        declared=True,
    )


def _required_secrets(
    entrypoint: ReleaseEntrypoint, target: config_module.ReleaseTarget
) -> Tuple[str, ...]:
    """The secrets this target's build reads, checked against the policy.

    Intersected with what the target's signing settings actually name, so a
    matrix never carries a secret the build will not read. Carrying one anyway
    would hand a job material it has no use for, which is exactly the shape of a
    secret that gets logged.
    """

    configured = set(target.signing.secret_names())
    if not entrypoint.signing or not configured:
        return ()
    wanted = entrypoint.secret_names()
    if not configured.intersection(wanted):
        return ()
    return tuple(name for name in wanted if name in configured)


def fragment_path(root: str, target_id: str) -> str:
    """Where one target's result is written, from names nothing can change.

    Deterministic so the transaction job can find every fragment without being
    told where they are, and so a resumed run reads the previous run's output
    rather than a second copy of it.
    """

    if not _TARGET_RE.match(target_id or ""):
        raise EntrypointError(
            f"fragment path for {target_id!r} is not derivable from a slug, so a build job "
            "and the transaction job would disagree about where the result is"
        )
    return f"{root.rstrip('/')}/{target_id}.json"


def validate_matrix(
    payload: Mapping[str, Any],
    *,
    config: config_module.ContinuumConfig,
    source_sha: str,
    version: str,
    channel: str = "stable",
) -> ReleaseMatrix:
    """Re-check a matrix that has been through a workflow.

    Every row is compared against the table and the policy, and the whole thing
    is compared against the release it claims to be. A matrix that was edited
    between the resolve job and a build job — by a step, a compromised action, or
    an input that was never validated — does not survive this, and the release
    stops before a runner with signing material is asked to run a command that
    was not in the table.
    """

    if not isinstance(payload, Mapping):
        raise EntrypointError(
            "the matrix is not an object. A release job that cannot read its own matrix "
            "would build whatever the next parse produced"
        )
    if payload.get("schema") != ENTRYPOINT_SCHEMA:
        raise EntrypointError(
            f"matrix schema {payload.get('schema')!r} is not {ENTRYPOINT_SCHEMA!r}; a matrix "
            "from another version of Continuum describes a job this one cannot check"
        )
    if payload.get("version") != version:
        raise EntrypointError(
            f"the matrix is for version {payload.get('version')!r}, not {version!r}. "
            "Publishing this release's bytes under a version the matrix was not resolved "
            "for would ship a version nobody approved"
        )
    if payload.get("source_sha") != source_sha:
        raise EntrypointError(
            f"the matrix is for commit {str(payload.get('source_sha'))[:7]}, not "
            f"{source_sha[:7]}. A matrix resolved for one commit and built from another is "
            "the one mistake the whole transaction is arranged to prevent"
        )
    declared = {target.id: target for target in config.release.targets}
    rows = payload.get("targets")
    if not isinstance(rows, list) or not rows:
        raise EntrypointError(
            "the matrix has no targets, or its targets are not a list. A release with no "
            "rows would publish nothing and report success"
        )
    targets: List[MatrixTarget] = []
    for row in rows:
        target = MatrixTarget.from_describe(row)
        declared_target = declared.get(target.target)
        if declared_target is None:
            raise EntrypointError(
                f"the matrix builds {target.target!r}, which the release policy does not "
                f"declare. It declares: {', '.join(sorted(declared)) or 'nothing'}"
            )
        entrypoint = ENTRYPOINTS.get(target.adapter)
        if entrypoint is None or target.adapter != declared_target.adapter:
            raise EntrypointError(
                f"matrix target {target.target!r} claims adapter {target.adapter!r}, which is "
                f"not the {declared_target.adapter!r} the policy declares for it"
            )
        if tuple(target.argv) != tuple(entrypoint.argv):
            raise EntrypointError(
                f"matrix target {target.target!r} would run {list(target.argv)}, but the "
                f"{entrypoint.adapter!r} entrypoint runs {list(entrypoint.argv)}. The command "
                "is part of the entrypoint, not an input to it"
            )
        if target.runner != entrypoint.runner:
            raise EntrypointError(
                f"matrix target {target.target!r} asks for runner {target.runner!r}, but "
                f"{entrypoint.adapter!r} builds on {entrypoint.runner!r}. Moving a build to "
                "a runner without its toolchain produces a confusing failure, or a build "
                "that quietly skips the step that needed the toolchain"
            )
        if not declared_target.is_mvp_executable and not target.declared:
            raise EntrypointError(
                f"matrix target {target.target!r} is marked built, but the policy declares it "
                "as something this release cannot build. Claiming a target is buildable is "
                "how a partial release turns into a release that publishes less than its "
                "policy promises"
            )
        targets.append(target)
    return ReleaseMatrix(
        version=version,
        source_sha=source_sha,
        channel=str(payload.get("channel") or channel),
        key=str(payload.get("key") or ""),
        targets=tuple(targets),
        dry_run=bool(payload.get("dry_run", False)),
    )


def parse_matrix(document: str) -> Dict[str, Any]:
    """Read a matrix back, refusing anything that is not one.

    Parsed here rather than in the CLI so a caller embedding the release plane
    gets the same refusal a workflow does.
    """

    try:
        payload = json.loads(document)
    except (TypeError, ValueError) as exc:
        raise EntrypointError(
            f"the matrix is not JSON: {exc}. A job that cannot read its own matrix would "
            "otherwise build whatever the next parse produced"
        ) from None
    if not isinstance(payload, dict):
        raise EntrypointError("the matrix must be a JSON object")
    return payload


def parse_matrix_file(document: str, path: str = "") -> ReleaseMatrix:
    """Read a matrix document back into a validated matrix.

    `from_describe` re-runs every check a matrix made on its way out, so a
    rewritten file fails here rather than becoming a release.
    """

    payload = parse_matrix(document)
    try:
        return ReleaseMatrix.from_describe(payload)
    except EntrypointError as exc:
        where = f" at {path}" if path else ""
        raise EntrypointError(f"the release matrix{where} is not usable: {exc}") from None


def entrypoint_report() -> str:
    """What this release plane can build, for a plan and for a failure message."""

    lines = []
    for name in supported():
        entrypoint = ENTRYPOINTS[name]
        note = "" if entrypoint.config_supported else " (release policy cannot declare it yet)"
        secrets = ", ".join(entrypoint.secret_names()) or "none"
        lines.append(
            f"{name}: {entrypoint.title} on {entrypoint.runner}"
            f"{'; toolchain {entrypoint.toolchain}' if entrypoint.toolchain else ''}"
            f"; secrets {secrets}{note}"
        )
    return "\n".join(lines)


def assert_secrets_available(
    matrix: ReleaseMatrix, available: Iterable[str]
) -> Tuple[str, ...]:
    """Refuse a matrix whose jobs cannot read what their builds need.

    Checked before any runner is asked, so "you did not pass the signing
    material" is reported as a missing input rather than as a build that failed
    at the sign step with a stack trace. The names are returned rather than
    raised as prose because the caller usually wants to print them into a
    workflow error.
    """

    present = set(available or ())
    missing = tuple(name for name in matrix.required_secrets if name not in present)
    if missing:
        raise EntrypointError(
            "these targets cannot build because their signing material was not provided: "
            + ", ".join(missing)
            + f". The {matrix.version} matrix needs: {', '.join(matrix.required_secrets) or 'nothing'}. "
            "A declared secret that is empty is not a secret; pass it, or declare the target "
            "without signing."
        )
    return matrix.required_secrets


__all__ = [
    "ANDROID",
    "parse_matrix_file",
    "APPLE",
    "ENTRYPOINTS",
    "ENTRYPOINT_SCHEMA",
    "GENERIC",
    "JVM",
    "RUNNER_MACOS",
    "RUNNER_UBUNTU",
    "EntrypointError",
    "MatrixTarget",
    "ReleaseEntrypoint",
    "ReleaseMatrix",
    "assert_secrets_available",
    "entrypoint_report",
    "fragment_path",
    "get",
    "parse_matrix",
    "resolve_matrix",
    "supported",
    "validate_matrix",
]
