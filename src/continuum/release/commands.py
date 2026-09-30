"""The release plane's three commands, as the workflow invokes them.

`resolve`, `target`, and `transaction` are three jobs with three different
privilege levels, and this module is where that separation becomes real code
rather than a comment in a workflow file. The split is not a convenience: the
`target` command is the one that runs a build from an untrusted ref, so it is
the one that must not be able to reach a destination — which is why
`build_components` there takes no publisher argument at all, and why
`transaction_components` requires a token and refuses without one.

Every value that reaches a subprocess or an API comes from configuration or from
an environment variable the job was given. Nothing here is interpolated into a
shell command, and no secret is ever written to an output, a result document, or
a log line.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from .. import config as config_module
from . import adapters as release_adapters
from . import android as android_module
from . import entrypoints
from . import github_api
from . import jvm as jvm_module
from . import plane
from . import provenance as provenance_module
from .contract import EVENT_DISPATCH, EVENT_TAG_PUSH, ContractError, ReleaseEvent
from .core import ReleaseComponents, ReleaseRequest
from .entrypoints import ReleaseMatrix
from .transaction import classify, write_fragment
from .version import ExplicitVersion, TagVersion, VersionPolicy

#: The adapters that have a release adapter — a `build`/`sign`/`verify` object
#: the chain can walk — rather than only a plan. Named explicitly rather than
#: discovered, because "has a plan" and "can produce a manifest" are different
#: properties and only this table knows which is which: Apple has a complete
#: signing plan and no adapter, so a target naming it is refused by name here
#: instead of being attempted through a path that cannot produce a manifest.
CORE_ADAPTERS = {
    android_module.ADAPTER_NAME: android_module.AndroidAdapter,
    jvm_module.ADAPTER_NAME: jvm_module.JvmAdapter,
}

EXIT_OK = 0
EXIT_ERROR = 1

#: The one file every job agrees on. The matrix is the release's identity, so it
#: has a fixed name: a workflow passing a path through an expression is a
#: workflow whose two jobs can disagree about which file is the release.
MATRIX_NAME = "matrix.json"
JOURNAL_NAME = "journal.json"
RESULT_NAME = "result.json"

FRAGMENT_ROOT = "runs"


class ReleasePlaneError(ContractError):
    """A command was asked to do something it cannot, named as a release error.

    Carries a `code` because a workflow branches on it: a missing credential and
    an unbuildable target are both fatal here, but only one of them is a
    configuration mistake somebody has to fix.
    """

    def __init__(self, message: str, *, code: str = "release-command") -> None:
        super().__init__(message, code=code)


def matrix_path(root: str = FRAGMENT_ROOT) -> str:
    return os.path.join(root, MATRIX_NAME)


def read_matrix(path: str) -> ReleaseMatrix:
    """Read the matrix the resolve job wrote, re-running every check on it.

    Not `json.loads` and a cast: the file crossed a job boundary, and the
    constructor is what notices a matrix whose rows have been edited into
    something the table would not have produced.
    """

    if not os.path.isfile(path):
        raise ReleasePlaneError(
            f"no release matrix at {path}. The resolve job writes it; a target job "
            "without one has no release to build",
            code="matrix-absent",
        )
    with open(path, "r", encoding="utf-8") as handle:
        document = handle.read()
    return entrypoints.parse_matrix_file(document, path)


def load_event(
    *,
    repository: str,
    source_sha: str,
    version: str,
    name: str = "tag-push",
    tag: str = "",
    ref: str = "",
    default_branch: str = "",
    delivery: str = "",
) -> ReleaseEvent:
    """The event a release is allowed to believe.

    Assembled from arguments the workflow passes explicitly rather than from
    the ambient GitHub context, so a release is pinned to the commit the workflow
    was told to build and not to whatever `GITHUB_SHA` happens to be in a step
    that ran on a different ref.

    A tag-push event's tag is derived from the version rather than read from a
    second input, because two places naming the version is two places to
    disagree about it. A dispatched release gets no tag at all: nobody pushed
    one, and inventing `v<version>` here would put a tag in the journal, the
    release body, and the publisher's identity that does not exist in the
    repository. The version is a dispatched release's identity, and the tag is
    created when it is published.
    """

    branch = default_branch or "main"
    if name == EVENT_TAG_PUSH:
        resolved = tag or f"v{version}"
        return ReleaseEvent(
            repository=repository,
            name=name,
            sha=source_sha,
            ref=ref or f"refs/tags/{resolved}",
            tag=resolved,
            default_branch=branch,
            delivery=delivery or os.environ.get("GITHUB_RUN_ID", ""),
        )
    if name != EVENT_DISPATCH:
        raise ReleasePlaneError(
            f"event {name!r} is not something a release can be dispatched as. Pass "
            f"{EVENT_TAG_PUSH!r} with the tag that was pushed, or {EVENT_DISPATCH!r} to "
            "cut a version from the default branch without a tag",
            code="event-unsupported",
        )
    if tag:
        raise ReleasePlaneError(
            f"a dispatched release has no tag, and {tag!r} was passed as one. A tag that "
            "was not pushed must not appear in the release's record; the tag is created "
            "when the release is published",
            code="event-unsupported",
        )
    return ReleaseEvent(
        repository=repository,
        name=name,
        sha=source_sha,
        ref=ref or f"refs/heads/{branch}",
        tag="",
        default_branch=branch,
        delivery=delivery or os.environ.get("GITHUB_RUN_ID", ""),
    )


# -- resolve ------------------------------------------------------------------


def cmd_resolve(
    args: Any,
    *,
    config: config_module.ContinuumConfig,
) -> Tuple[int, Dict[str, str]]:
    """Turn the policy and a pinned commit into the matrix every job runs on.

    Everything the rest of the release needs is decided here, from two inputs
    that were both checked: the validated configuration and a full commit SHA.
    No job downstream asks what it is building, because the answer is a file.
    """

    matrix = entrypoints.resolve_matrix(
        config,
        version=args.version,
        source_sha=args.source_sha,
        channel=args.channel,
        root=args.fragment_root,
        dry_run=args.dry_run,
        include_declared=bool(getattr(args, "include_declared", False)),
    )
    check_signing_material(matrix, getattr(args, "signing_material", "auto"), os.environ)
    os.makedirs(args.fragment_root, exist_ok=True)
    path = matrix_path(args.fragment_root)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(matrix.to_json() + "\n")
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(matrix.to_json() + "\n")

    outputs = {
        # `ok` is what the workflow gates every later job on. Without it the
        # downstream `if:` reads an empty string, which is not 'true', and a
        # successful resolve silently dispatches nothing.
        "ok": "true",
        "matrix": matrix.artifact(),
        "matrix_path": path,
        "version": matrix.version,
        "source_sha": matrix.source_sha,
        "channel": matrix.channel,
        "key": matrix.key,
        "targets": ",".join(matrix.buildable_ids),
        "declared_targets": ",".join(matrix.declared_ids),
        "runners": ",".join(sorted({target.runner for target in matrix.targets})),
        "secrets": ",".join(matrix.required_secrets),
        "dry_run": str(matrix.dry_run).lower(),
        "rows": json.dumps(
            [
                {
                    "target": target.target,
                    "adapter": target.adapter,
                    "runner": target.runner,
                    "secrets": list(target.secrets),
                    "fragment": target.fragment,
                }
                for target in matrix.targets
                if not target.declared
            ]
        ),
    }
    return EXIT_OK, outputs


# -- target -------------------------------------------------------------------


def build_components(
    config: config_module.ContinuumConfig,
    target_id: str,
    *,
    policy: Any = None,
) -> ReleaseComponents:
    """The components a build job may hold.

    No publisher and no sync, and not as a default: a destination cannot be
    passed in at all, so the privileged job's wiring is not reachable from the
    code that runs an untrusted ref's build. `ReleaseComponents` is constructed
    here with `publishers=()` written out rather than defaulted, so adding a
    publisher later is a visible edit to this function.
    """

    target = config.release.target(target_id)
    if target is None:
        available = ", ".join(item.id for item in config.release.targets) or "none configured"
        raise ReleasePlaneError(
            f"no release target {target_id!r} in {config.source}; configured: {available}",
            code="target-absent",
        )
    adapter_class = CORE_ADAPTERS.get(target.adapter)
    if adapter_class is None:
        raise ReleasePlaneError(
            f"target {target_id!r} names adapter {target.adapter!r}, which has a plan but no "
            "release adapter the chain can walk. A target cannot be published from a plan: a "
            "plan produces no manifest, so the transaction would have nothing to merge. "
            "Build it with 'continuum release sign', or add an adapter for it",
            code="adapter-not-walkable",
        )
    environment = release_adapters.bind_material(target, os.environ)
    return ReleaseComponents(
        eligibility=plane.eligibility_for(policy),
        # The requested version, cross-checked against the tag. Two independent
        # sources that must agree: a workflow that names 1.4.1 while the tag
        # says v1.4.0 stops the release rather than picking one.
        version=ExplicitVersion(),
        notes=plane.notes_for(),
        adapters={target.adapter: adapter_class(environment=environment)},
        publishers=(),
        syncs=(),
    )


def check_signing_material(matrix: ReleaseMatrix, mode: str, environment: Any) -> Tuple[str, ...]:
    """What this job must find in its environment before any runner is asked.

    `present` and `absent` are both assertions, and they are opposites: `absent`
    is how a repository proves its ad-hoc fallback path still works, on a Linux
    job that will never touch a signing tool. Checking before the first build
    means "you did not pass the signing material" is reported as a missing input
    rather than as a build that failed at the sign step.
    """

    if mode == "present":
        return entrypoints.assert_secrets_available(matrix, environment)
    if mode == "absent":
        present = tuple(
            name for name in matrix.required_secrets if (environment.get(name) or "").strip()
        )
        if present:
            raise ReleasePlaneError(
                f"this job was told its signing material would be absent, but "
                f"{', '.join(present)} is set. A job that claims to be degraded while "
                "holding the key is not testing the fallback it was asked to test",
                code="signing-material-unexpected",
            )
        return ()
    return matrix.required_secrets


def version_checks_for(matrix: ReleaseMatrix, event: Optional[ReleaseEvent] = None) -> Tuple[Any, ...]:
    """The tag, as an independent read of the version.

    The workflow names the version and the event carries the tag, and the chain
    requires the two to agree. That is the cross-check the version stage exists
    for: neither input can silently override the other, because a disagreement
    is a fact about the dispatch rather than something to resolve.

    A dispatched release has no tag, so there is nothing to cross-check against
    and the check is omitted rather than pointed at a tag nobody pushed. The
    dispatched version is then the workflow input alone, which is why the
    transaction's environment approval is what stands in for the second source.
    """

    if event is not None and not event.tag:
        return ()
    return (TagVersion(tag=f"v{matrix.version}"),)


def cmd_target(
    args: Any,
    *,
    config: config_module.ContinuumConfig,
) -> Tuple[int, Dict[str, str]]:
    """Build one target and write the fragment the transaction will merge.

    Every output here describes the build that happened, and none of them
    describes the release: no tag, no channel, no release URL. A build job's
    entire influence on the release is the fragment it writes, and a job that
    could also set what the release is called would be a job that could change
    what it was asked to build.
    """

    matrix = read_matrix(args.matrix or matrix_path(args.fragment_root))
    row = matrix.target(args.target)
    check_signing_material(matrix, getattr(args, "signing_material", "auto"), os.environ)
    components = build_components(config, args.target)
    event = load_event(
        repository=args.repository,
        source_sha=matrix.source_sha,
        version=matrix.version,
        name=args.event,
    )
    request = resolve_release(
        config,
        event=event,
        version=matrix.version,
        source_sha=matrix.source_sha,
        matrix=matrix,
        channel=matrix.channel,
        workdir=args.workdir or ".",
        # The event, not just the matrix: a dispatched release has no tag, and a
        # cross-check pointed at a tag nobody pushed would fail every dispatched
        # build at the version stage.
        version_checks=version_checks_for(matrix, event),
    )
    fragment = build_target(request, matrix, args.target, components=components)
    path = write_fragment(args.fragment_root, fragment)
    return EXIT_OK, {
        "ok": "true",
        "target": fragment.target,
        "version": fragment.version,
        "source_sha": fragment.source_sha,
        "fragment": path,
        "artifacts": ",".join(
            artifact.name for artifact in fragment.manifest.artifacts
        ),
    }


# -- transaction --------------------------------------------------------------


def transaction_components(
    config: config_module.ContinuumConfig,
    args: Any,
    notes: str = "",
) -> ReleaseComponents:
    """The privileged job's wiring, including the one place a token is read.

    A release with no publisher would finish successfully having published
    nothing, so this refuses rather than degrading: a transaction that cannot
    write its destination is not a transaction.
    """

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        raise ReleasePlaneError(
            "the transaction needs GITHUB_TOKEN to write the release. It is the only job "
            "that holds one; if this job does not have it, the workflow did not give the "
            "transaction its permission",
            code="missing-credential",
        )
    repository = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        raise ReleasePlaneError(
            "the transaction needs a repository to write to; pass --repository or set "
            "GITHUB_REPOSITORY",
            code="repository-absent",
        )
    transport = github_api.UrllibGitHubTransport(token)
    destination = github_api.GitHubReleaseRepository(
        transport,
        repository,
        prerelease=bool(getattr(args, "prerelease", False)),
        immutable_expected=bool(getattr(args, "immutable", False)),
        strict_digests=not bool(getattr(args, "allow_unknown_digests", False)),
    )
    publisher = github_publisher(destination, args, notes)
    adapters: Dict[str, Any] = {}
    for target in config.release.targets:
        adapter_class = CORE_ADAPTERS.get(target.adapter)
        if adapter_class is None:
            raise ReleasePlaneError(
                f"target {target.id!r} names adapter {target.adapter!r}, which has no release "
                "adapter, so the transaction has no way to read the manifest a build job "
                "produced for it",
                code="adapter-not-walkable",
            )
        # The transaction needs the adapter registered so the chain can resolve
        # the targets it is resuming; it never rebuilds, because the merged
        # journal records every build and sign as done.
        adapters.setdefault(
            target.adapter,
            adapter_class(environment=release_adapters.bind_material(target, os.environ)),
        )
    return ReleaseComponents(
        eligibility=plane.eligibility_for(),
        version=ExplicitVersion(),
        notes=plane.notes_for(),
        adapters=adapters,
        publishers=(publisher,),
        syncs=(),
    )


def github_publisher(destination: Any, args: Any, notes: str) -> Any:
    from .github import GitHubReleasePublisher

    provenance = _provenance(args)
    return GitHubReleasePublisher(
        repository=destination,
        provenance=provenance,
        notes=notes,
    )


def release_notes(matrix: ReleaseMatrix, event: ReleaseEvent) -> str:
    """The body the release will carry, generated from the release's own record.

    A dispatched release has no tag, so it is not described as though one
    existed: the heading is the version, and the note says the release was
    dispatched. A notes body that implies a tag nobody pushed is the kind of
    small inaccuracy a consumer trusts.
    """

    from .contract import NotesRequest

    request = NotesRequest(
        event=event,
        version=matrix.version,
        tag=event.tag or f"v{matrix.version}",
        source_sha=matrix.source_sha,
        key=matrix.describe()["key"],
    )
    body = plane.notes_for().build(request).body
    if not event.tag:
        body = body.replace(
        f"# v{matrix.version}",
        f"# {matrix.version}",
    ).replace(
        "This note is generated from the release's record",
            f"Dispatched release: no `v{matrix.version}` tag was pushed for this version.\n\n"
            "This note is generated from the release's record",
            1,
        )
    if not matrix.declared_ids:
        return body
    # A consumer reads the release page, not the workflow's job summary, so the
    # gap goes in the release body. A release that says less than the policy
    # promises has to say so where the assets are.
    many = len(matrix.declared_ids) > 1
    return body.rstrip("\n") + "\n\n" + "\n".join(
        [
            "### Declared but not shipped",
            "",
            "The release policy lists "
            + ", ".join(f"`{name}`" for name in matrix.declared_ids)
            + f", and no release adapter builds {'them' if many else 'it'}, so this "
            f"release contains no artifact for {'those targets' if many else 'that target'}. "
            + (
                "They are declared in the policy and absent from this release."
                if many
                else "It is declared in the policy and absent from this release."
            ),
        ]
    ) + "\n"


def _provenance(args: Any) -> Optional[Any]:
    """An attestor, when the job is told who is building.

    Identity fields come from the workflow's own context and are all required:
    a statement missing one of them cannot be checked by a consumer, so
    producing it would be publishing an unfalsifiable claim. An unresolvable
    identity is an error rather than an unattested release, because a release
    that silently skips provenance is a release nobody downstream can trust.
    """

    if not getattr(args, "attest", False):
        return None
    try:
        identity = provenance_module.BuildIdentity(
            repository=args.repository,
            workflow=args.workflow,
            workflow_ref=args.workflow_ref,
            source_sha=args.source_sha,
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            event=os.environ.get("GITHUB_EVENT_NAME", ""),
        )
    except provenance_module.ProvenanceError as exc:
        raise ReleasePlaneError(
            f"cannot attest this release: {exc}",
            code="provenance-unavailable",
        ) from None
    return provenance_module.ProvenanceAttestor(
        identity,
        directory=os.path.join(getattr(args, "fragment_root", FRAGMENT_ROOT), "provenance"),
    )


def cmd_transaction(
    args: Any,
    *,
    config: config_module.ContinuumConfig,
) -> Tuple[int, Dict[str, str]]:
    """Merge every target's fragment, publish once, and report the release."""

    from .transaction import assert_request_matches, publish_release, collect_fragments

    matrix = read_matrix(args.matrix or matrix_path(args.fragment_root))
    event = load_event(
        repository=args.repository,
        source_sha=matrix.source_sha,
        version=matrix.version,
        name=args.event,
    )
    request = ReleaseRequest(
        event=event,
        targets=tuple(
            _spec_for(config, item) for item in matrix.target_ids if config.release.target(item)
        ),
        version_policy=VersionPolicy(),
        version_checks=version_checks_for(matrix, event),
        requested_version=matrix.version,
        channel=matrix.channel,
        workdir=args.workdir or ".",
    )
    if not request.targets:
        raise ReleasePlaneError(
            f"the policy declares none of the matrix's targets "
            f"({', '.join(matrix.target_ids)}); the transaction has nothing to publish",
            code="targets-absent",
        )
    assert_request_matches(matrix, request)
    # The wiring is checked before the state on disk. A job with no token is a
    # workflow bug that recurs on every retry, whereas an incomplete set may just
    # need a build job re-run; reporting the permanent problem first means the
    # operator learns about the token even while the set is still short.
    components = transaction_components(config, args, notes=release_notes(matrix, event))
    fragments = collect_fragments(args.fragment_root, matrix)
    journal = _read_journal(args)
    result = publish_release(
        request,
        matrix,
        fragments,
        components=components,
        journal=journal,
    )
    with open(os.path.join(args.fragment_root, RESULT_NAME), "w", encoding="utf-8") as handle:
        handle.write(result.to_json())
    # The merged journal is written so a re-run resumes rather than repeats: the
    # stages that already happened are recorded here, and the next transaction
    # reads it instead of re-doing a publish that may have partly succeeded.
    if result.outcome is not None:
        with open(
            os.path.join(args.fragment_root, JOURNAL_NAME), "w", encoding="utf-8"
        ) as handle:
            json.dump(result.outcome.journal.describe(), handle, indent=2, sort_keys=True)
            handle.write("\n")
    print(result.job_summary(), end="")
    return result.exit_code, result.outputs


def _spec_for(config: config_module.ContinuumConfig, target_id: str) -> Any:
    from .contract import TargetSpec

    target = config.release.target(target_id)
    assert target is not None
    return TargetSpec(id=target.id, adapter=target.adapter)


def _read_journal(args: Any) -> Any:
    """The journal of a previous attempt, if the job was given one.

    Passed in rather than found, because a transaction that discovers its own
    resume state is a transaction that will resume from whatever happened to be
    in the directory — including a run of a different version.
    """

    from .state import Journal, StageOutcome

    path = getattr(args, "journal", "") or ""
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ReleasePlaneError(
            f"the journal at {path} is not a list of recorded transitions",
            code="journal-invalid",
        )
    return Journal(
        entries=tuple(
            StageOutcome(
                stage=item.get("stage") or "",
                key=item.get("key") or "",
                outcome=item.get("outcome") or "",
                summary=item.get("summary") or "",
                retryable=bool(item.get("retryable")),
                code=item.get("code") or "",
                details=tuple(
                    (name, str(value))
                    for name, value in (item.get("details") or {}).items()
                ),
            )
            for item in entries
        )
    )


def describe_failure(exc: BaseException) -> Tuple[str, bool, str, str]:
    """The one line a job prints, with the taxonomy's advice attached.

    Shared by all three commands so a failure reads the same whether it happened
    in resolve or in the transaction, and so the `::error::` annotation and the
    exit code come from the same classification.
    """

    code = getattr(exc, "code", "") or "release-failed"
    _taxonomy_retryable, advice = classify(code)
    # The message is what the component said; the advice is a separate line
    # because appended to a sentence it reads as part of the error, and an error
    # that explains itself twice is harder to scan, not easier.
    return f"[{code}] {exc}", getattr(exc, "retryable", False), code, advice


__all__ = [
    "CORE_ADAPTERS",
    "JOURNAL_NAME",
    "MATRIX_NAME",
    "RESULT_NAME",
    "ReleasePlaneError",
    "build_components",
    "cmd_resolve",
    "cmd_target",
    "cmd_transaction",
    "describe_failure",
    "load_event",
    "matrix_path",
    "read_matrix",
    "transaction_components",
    "version_checks_for",
]
