"""The release command line: one entry point for plan, build, publish, run.

The workflow layer stays thin on purpose. It checks out the commit, exports the
event it was woken by, and calls this module; everything that decides *whether*
and *what* to release lives in the engine and is therefore testable without a
runner, a network, or a token.

Three properties are worth naming here because they are enforced at this seam:

**A disabled release is a green no-op.** No hooks, no targets means no token is
read, no API is called, and the process exits zero after saying so. That is what
lets the workflow be installed by every consumer and enabled by the few who want
it.

**A phase boundary is a receipt boundary.** `build` runs the chain with no
destinations and writes what it built; `publish` resumes that receipt on another
runner with no toolchain and refuses to build. Neither half can pretend to be the
other.

**A failure is classified, not just printed.** A retryable failure exits 75 so
the job can be retried mechanically; a blocked or contradictory release exits
1/78 and needs a human. The codes are stable because a controller branches on
them.

Exit codes: 0 green, 1 failed, 2 usage/configuration, 75 retryable, 78 blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import config as config_module
from .contract import ContractError, ReleaseEvent, TargetSpec
from .core import ReleaseComponents, ReleaseCore, ReleaseOutcome, ReleaseRequest
from .eligibility import ChangelogNotes, ReleaseEligibility
from .evidence import EvidenceGate, UnavailableEvidence
from .github import GitHubReleasePublisher
from .github_api import (
    GitHubApi,
    GitHubReleaseRepository,
    PlannedReleaseRepository,
    ReleaseAssetReceiptStore,
)
from .hooks import HooksError, build_adapters, load_hooks
from .receipt import (
    CompositeReceiptStore,
    FileReceiptStore,
    Receipt,
    ReceiptError,
    TagReceiptStore,
)
from .state import EMPTY_JOURNAL, Journal
from .targets import config_adapters, recorded_adapters
from .version import (
    ExplicitVersion,
    ProjectFileVersion,
    TagVersion,
    VersionContext,
    VersionPolicy,
    VersionStrategy,
    agree,
    require_agreement,
    tag_for,
)

OUTCOME_SCHEMA = "continuum.release-cli/v1"
DEFAULT_CONFIG = ".continuum.yml"
DEFAULT_RECEIPT_DIR = ".continuum-receipts"
DEFAULT_NOTES_FILE = "CHANGELOG.md"
DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_DESTINATION = "github-releases"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_RETRYABLE = 75
EXIT_BLOCKED = 78


class CliError(ValueError):
    """A refusal this module can explain, with the exit code it implies."""

    def __init__(self, message: str, *, code: str = "release-cli", exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m continuum.release",
        description=(
            "Plan or run one application release. The chain is the same in every "
            "phase; only the destinations and the receipt differ."
        ),
    )
    parser.add_argument(
        "phase",
        nargs="?",
        default="execute",
        choices=("plan", "build", "publish", "execute"),
        help="plan: dry run; build: run and record without publishing; publish: "
        "resume a recorded build; execute: the whole chain",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")

    event = parser.add_argument_group("event")
    event.add_argument(
        "--event",
        required=True,
        choices=("push", "tag-push", "workflow-dispatch", "release-pr-merged", "scheduled"),
    )
    event.add_argument("--repository", required=True, help="owner/name")
    event.add_argument("--sha", required=True)
    event.add_argument("--ref", default="")
    event.add_argument("--tag", default="")
    event.add_argument("--branch", default="")
    event.add_argument("--default-branch", default="main")
    event.add_argument("--delivery", default="")
    event.add_argument("--commit-message", default="")
    event.add_argument("--changed-files", default="")
    event.add_argument("--changed-files-file", default="")
    event.add_argument("--changed-files-unavailable", action="store_true")
    event.add_argument("--skip-release", action="store_true")
    event.add_argument("--allow-any-branch", action="store_true")

    version = parser.add_argument_group("version and notes")
    version.add_argument("--version", default="")
    version.add_argument("--version-file", default="")
    version.add_argument(
        "--version-pattern",
        default=r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?",
    )
    version.add_argument("--tag-prefix", default="v")
    version.add_argument("--release-pr", type=int, default=0)
    version.add_argument("--notes-file", default=DEFAULT_NOTES_FILE)
    version.add_argument("--notes", default="")
    version.add_argument("--channel", default="default")

    hooks = parser.add_argument_group("hooks")
    hooks.add_argument("--hooks", default="", help="override release.hooks from config")
    hooks.add_argument(
        "--secret-env",
        action="append",
        default=[],
        metavar="NAME",
        help="forward the named environment variable to hook commands; "
        "nothing else is inherited",
    )

    destination = parser.add_argument_group("destination")
    destination.add_argument(
        "--no-github-release",
        action="store_true",
        help="do not publish to GitHub Releases (a destination must still be configured)",
    )
    destination.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    destination.add_argument("--api-root", default="")
    destination.add_argument("--upload-root", default="")

    evidence = parser.add_argument_group("evidence")
    evidence.add_argument("--require-evidence", action="store_true")
    evidence.add_argument("--required-check", action="append", default=[])
    evidence.add_argument("--require-review", action="store_true")
    evidence.add_argument("--require-merged-pr", action="store_true")

    receipts = parser.add_argument_group("receipts")
    receipts.add_argument("--receipt-dir", default=DEFAULT_RECEIPT_DIR)
    receipts.add_argument(
        "--asset-receipt",
        action="store_true",
        help="also read and write the receipt as a release asset, so a rerun on "
        "another runner finds it",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--json", default="", metavar="PATH")
    output.add_argument("--github-output", default="", metavar="PATH")
    output.add_argument("--summary", default="", metavar="PATH")
    return parser


# -- event ------------------------------------------------------------------


def _read_changed_files(args: argparse.Namespace) -> str:
    if args.changed_files_file:
        try:
            with open(args.changed_files_file, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise CliError(
                f"the changed-file list at {args.changed_files_file} could not be read: {exc}",
                code="changed-files-unreadable",
            ) from exc
    return args.changed_files


def _event(args: argparse.Namespace) -> ReleaseEvent:
    attributes: List[Tuple[str, str]] = []
    branch = args.branch or args.default_branch
    attributes.append(("branch", branch))
    if args.commit_message:
        attributes.append(("commit_message", args.commit_message))
    changed = _read_changed_files(args)
    if changed:
        attributes.append(("changed_files", changed))
    if args.changed_files_unavailable:
        attributes.append(("changed-files-unavailable", "true"))
    if args.skip_release:
        attributes.append(("skip-release", "true"))
    return ReleaseEvent(
        repository=args.repository,
        name=args.event,
        sha=args.sha,
        ref=args.ref,
        delivery=args.delivery,
        default_branch=args.default_branch,
        tag=args.tag,
        attributes=tuple(attributes),
    )


# -- version and notes ------------------------------------------------------


def _version_parts(
    args: argparse.Namespace, event: ReleaseEvent, *, enabled: bool
) -> Tuple[VersionStrategy, Tuple[VersionStrategy, ...], VersionPolicy]:
    policy = VersionPolicy(tag_prefix=args.tag_prefix)
    project = (
        ProjectFileVersion(path=args.version_file, pattern=args.version_pattern)
        if args.version_file
        else None
    )
    tag = TagVersion(tag=args.tag) if event.name == "tag-push" or args.tag else TagVersion()
    explicit = ExplicitVersion(requested=args.version)

    if args.version:
        primary: VersionStrategy = explicit
        checks: Tuple[VersionStrategy, ...] = tuple(
            item for item in (project, tag if event.name == "tag-push" else None) if item
        )
    elif project is not None:
        primary = project
        checks = ()
    elif event.name == "tag-push":
        primary = tag
        checks = ()
    elif not enabled:
        primary = explicit
        checks = ()
    else:
        raise CliError(
            "this repository has release targets but no version source. Pass --version "
            "for a manual release, --version-file for a file the commit carries, or "
            "trigger the release from a tag.",
            code="version-source-unconfigured",
        )
    return primary, checks, policy


def _agreed_version(
    primary: VersionStrategy,
    checks: Sequence[VersionStrategy],
    event: ReleaseEvent,
    workdir: str,
    policy: VersionPolicy,
    requested: str,
    release_pr: int,
) -> str:
    text, path = primary.collect(event, workdir)
    proposals = [
        primary.propose(
            VersionContext(
                event=event,
                text=text,
                source_path=path,
                requested=requested,
                release_pr=release_pr,
            ),
            policy,
        )
    ]
    for check in checks:
        check_text, check_path = check.collect(event, workdir)
        proposals.append(
            check.propose(
                VersionContext(
                    event=event,
                    text=check_text,
                    source_path=check_path,
                    requested=requested,
                    release_pr=release_pr,
                ),
                policy,
            )
        )
    return require_agreement(agree(*proposals))


# -- components -------------------------------------------------------------


def _secrets(args: argparse.Namespace) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    environment: Dict[str, str] = {}
    values: List[str] = []
    for name in args.secret_env:
        value = os.environ.get(name, "")
        if not value:
            raise CliError(
                f"--secret-env {name} was requested, but the environment does not carry "
                "it. A hook that needs a credential must not run without it.",
                code="secret-missing",
                exit_code=EXIT_FAILED,
            )
        environment[name] = value
        values.append(value)
    return environment, tuple(values)


def _github_api(args: argparse.Namespace, repository: str, token: str) -> GitHubApi:
    options: Dict[str, Any] = {"repository": repository, "token": token}
    if args.api_root:
        options["api_root"] = args.api_root
    if args.upload_root:
        options["upload_root"] = args.upload_root
    return GitHubApi(**options)


def _publisher_port(
    args: argparse.Namespace, repository: str, token: str, *, dry_run: bool
) -> Any:
    if dry_run and not token:
        return PlannedReleaseRepository()
    if not token:
        raise CliError(
            "publishing needs a GitHub token. Export it under "
            f"{args.token_env} (a classic PAT with `repo`), or pass "
            "--no-github-release if this repository has another destination.",
            code="release-token-missing",
            exit_code=EXIT_FAILED,
        )
    return GitHubReleaseRepository(_github_api(args, repository, token))


def _evidence(
    args: argparse.Namespace, repository: str, token: str
) -> Optional[Any]:
    required = args.require_evidence or args.required_check
    if not required and not (args.require_review or args.require_merged_pr):
        return None
    if not token:
        return UnavailableEvidence(
            "release evidence was required, but no GitHub token was provided, so the "
            "checks and reviews of this exact commit cannot be read. Refusing to "
            "assume they passed."
        )
    client = _github_api(args, repository, token)
    return EvidenceGate(
        check_runs=client,
        pull_request=client,
        reviews=client,
        required_checks=tuple(args.required_check),
        require_review=args.require_review,
        require_merged=args.require_merged_pr,
    )


def _configured_hooks(
    args: argparse.Namespace, config: config_module.ContinuumConfig
) -> Any:
    path = args.hooks or config.release.hooks
    if not path:
        return None
    environment, secrets = _secrets(args)
    return load_hooks(
        path,
        workdir=args.workdir,
        environment=environment,
        secrets=secrets,
    )


def _targets(config: config_module.ContinuumConfig, hooks_plan: Any) -> Tuple[TargetSpec, ...]:
    if hooks_plan is None:
        return tuple(
            TargetSpec(id=target.id, adapter=target.adapter)
            for target in config.release.targets
        )
    hook_ids = {target.id for target in hooks_plan.targets}
    configured = {target.id for target in config.release.targets}
    if configured and configured != hook_ids:
        raise CliError(
            "release.targets and the hooks document disagree about which targets exist: "
            f"configured {sorted(configured)}, hooks {sorted(hook_ids)}. Two answers to "
            "'what is released' is a release nobody can verify.",
            code="release-target-mismatch",
        )
    return hooks_plan.specs()


def _receipt_stores(
    args: argparse.Namespace,
    repository: str,
    token: str,
    *,
    version: str,
    tag: str,
    dry_run: bool,
) -> Tuple[List[Any], Any]:
    stores: List[Any] = []
    if args.receipt_dir:
        stores.append(FileReceiptStore(args.receipt_dir))
    asset_store = None
    if args.asset_receipt and not dry_run and version and token:
        port = GitHubReleaseRepository(_github_api(args, repository, token))
        asset_store = ReleaseAssetReceiptStore(port)
        stores.append(TagReceiptStore(asset_store, tag=tag))
    return stores, asset_store


def _load_receipt(
    stores: Sequence[Any], repository: str, version: str
) -> Optional[Receipt]:
    if not stores or not version:
        return None
    return CompositeReceiptStore(stores).load(repository, version)


def _receipt_for(outcome: ReleaseOutcome, repository: str, event: ReleaseEvent) -> Receipt:
    release_id = ""
    url = ""
    for publication in outcome.publications:
        if publication.external_id or publication.detail("release_id"):
            release_id = publication.external_id or publication.detail("release_id")
        if publication.detail("url"):
            url = publication.detail("url")
    return Receipt(
        repository=repository,
        version=outcome.version,
        tag=outcome.tag or tag_for(outcome.version),
        source_sha=outcome.source_sha or event.sha,
        channel=outcome.channel,
        journal=outcome.journal,
        manifests=outcome.manifests,
        release_id=release_id,
        url=url,
        published=outcome.published,
    )


def _repository_components(
    args: argparse.Namespace,
    config: config_module.ContinuumConfig,
    event: ReleaseEvent,
    *,
    dry_run: bool,
    phase: str,
) -> Tuple[ReleaseComponents, Any, str]:
    enabled = config.release.enabled
    hooks_plan = _configured_hooks(args, config) if enabled else None
    targets = _targets(config, hooks_plan) if enabled else ()
    primary, checks, policy = _version_parts(args, event, enabled=enabled)
    notes = ChangelogNotes(path=args.notes_file, fallback_notes=args.notes)

    token = os.environ.get(args.token_env, "") if enabled else ""
    evidence = _evidence(args, event.repository, token) if enabled else None

    publish = enabled and phase != "build"
    syncs: Tuple[Any, ...] = ()
    if enabled and hooks_plan is not None and phase != "build":
        # GitHub Releases is the *publisher*: it owns the immutable asset set.
        # A tap or a port tree is downstream, so every hook destination is a
        # sync. Mapping them this way keeps the retry semantics per destination
        # and stops a tap failure from being reported as an unpublished release.
        syncs = tuple(hooks_plan.publishers) + tuple(hooks_plan.syncs)

    publishers: Tuple[Any, ...] = ()
    if publish and not args.no_github_release:
        publishers = (
            GitHubReleasePublisher(
                _publisher_port(args, event.repository, token, dry_run=dry_run)
            ),
        )

    if enabled:
        if phase == "publish":
            adapters = recorded_adapters(target.id for target in targets)
        elif hooks_plan is not None:
            environment, secrets = _secrets(args)
            adapters = build_adapters(
                hooks_plan,
                workdir=args.workdir,
                environment=environment,
                secrets=secrets,
            )
        else:
            adapters = config_adapters(config.release.targets)
    else:
        adapters = {}

    components = ReleaseComponents(
        eligibility=ReleaseEligibility(
            default_branch=event.default_branch,
            require_default_branch=not args.allow_any_branch,
        ),
        version=primary,
        notes=notes,
        adapters=adapters,
        release_pr=hooks_plan.release_pr if hooks_plan is not None else None,
        publishers=publishers,
        syncs=syncs,
        evidence=evidence,
    )
    request = ReleaseRequest(
        event=event,
        targets=targets,
        enabled=enabled,
        version_policy=policy,
        version_checks=checks,
        requested_version=args.version,
        release_pr_number=args.release_pr,
        workdir=args.workdir,
        dry_run=dry_run,
        channel=args.channel,
    )
    return components, request, token


# -- output -----------------------------------------------------------------


def _github_output(payload: Dict[str, Any], path: str) -> None:
    lines = []
    for key in (
        "status",
        "ok",
        "resumable",
        "version",
        "tag",
        "source_sha",
        "stage",
        "published",
        "release_id",
        "release_url",
        "reason",
        "code",
    ):
        if key in payload and payload[key] not in ("", None):
            lines.append(f"{key}={payload[key]}")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _summary_markdown(payload: Dict[str, Any]) -> str:
    outcome = payload
    lines = [
        "## Continuum release",
        "",
        f"- status: **{outcome.get('status', '')}**",
    ]
    for key, label in (
        ("version", "version"),
        ("tag", "tag"),
        ("source_sha", "source"),
        ("channel", "channel"),
        ("reason", "reason"),
    ):
        if outcome.get(key):
            lines.append(f"- {label}: `{outcome[key]}`")
    if outcome.get("publications"):
        lines.append("")
        lines.append("| destination | outcome | identity |")
        lines.append("| --- | --- | --- |")
        for item in outcome["publications"]:
            lines.append(
                "| {destination} | {outcome} | `{identity}` |".format(
                    destination=item.get("destination", ""),
                    outcome=item.get("outcome", ""),
                    identity=item.get("identity", ""),
                )
            )
    if outcome.get("manifests"):
        lines.append("")
        lines.append("| target | artifacts |")
        lines.append("| --- | --- |")
        for manifest in outcome["manifests"]:
            lines.append(
                "| {target} | {count} |".format(
                    target=manifest.get("target", ""),
                    count=len(manifest.get("artifacts") or ()),
                )
            )
    return "\n".join(lines) + "\n"


def _print_human(description: Dict[str, Any]) -> None:
    print(
        "continuum release: {status} {version}{tag}".format(
            status=description.get("status", ""),
            version=description.get("version", ""),
            tag=f" ({description['tag']})" if description.get("tag") else "",
        )
    )
    for publication in description.get("publications") or ():
        print(
            "  {destination}: {outcome} {identity}".format(
                destination=publication.get("destination", ""),
                outcome=publication.get("outcome", ""),
                identity=publication.get("identity", ""),
            )
        )
    if description.get("failure"):
        failure = description["failure"]
        print(f"  failure: {failure.get('code', '')}: {failure.get('summary', '')}")


def emit(outcome: ReleaseOutcome, args: argparse.Namespace, receipt: Optional[Receipt]) -> int:
    description = outcome.describe()
    payload: Dict[str, Any] = {
        "schema": OUTCOME_SCHEMA,
        "outcome": description,
        "status": outcome.status,
        "ok": outcome.ok,
        "resumable": outcome.resumable,
        "published": outcome.published,
        "stage": outcome.stage,
        "version": outcome.version,
        "tag": outcome.tag,
        "source_sha": outcome.source_sha,
        "channel": outcome.channel,
        "reason": outcome.no_op_reason,
        "manifests": [manifest.describe() for manifest in outcome.manifests],
        "publications": [item.describe() for item in outcome.publications],
        "receipt": receipt.describe() if receipt is not None else None,
    }
    if outcome.failure is not None:
        payload["code"] = outcome.failure.code
    if receipt is not None:
        payload["release_id"] = receipt.release_id
        payload["release_url"] = receipt.url
    if args.json:
        _write_json(payload, args.json)
    if not args.quiet:
        _print_human(payload)
    _github_output(payload, args.github_output)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(_summary_markdown(payload))
    if outcome.failed:
        if outcome.failure is not None and outcome.failure.code in (
            "stale-head",
            "changes-requested",
            "review-stale",
            "source-conflict",
            "asset-set-conflict",
        ):
            return EXIT_BLOCKED
        return EXIT_RETRYABLE if outcome.resumable else EXIT_FAILED
    return EXIT_OK


def _write_json(payload: Dict[str, Any], path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# -- the run ----------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    try:
        config = config_module.load_optional_config(args.config)
    except config_module.ConfigError as exc:
        raise CliError(str(exc), code="config-invalid") from exc
    event = _event(args)
    dry_run = args.dry_run or args.phase == "plan"

    components, request, token = _repository_components(
        args, config, event, dry_run=dry_run, phase=args.phase
    )

    receipt = None
    stores: List[Any] = []
    if not dry_run and config.release.enabled:
        version = ""
        try:
            primary, checks, policy = _version_parts(args, event, enabled=True)
            version = _agreed_version(
                primary,
                checks,
                event,
                args.workdir,
                policy,
                args.version,
                args.release_pr,
            )
        except Exception:  # noqa: BLE001 - the version stage will report it
            version = ""
        stores, _ = _receipt_stores(
            args,
            event.repository,
            token,
            version=version,
            tag=tag_for(version, args.tag_prefix) if version else "",
            dry_run=dry_run,
        )
        receipt = _load_receipt(stores, event.repository, version)

    core = ReleaseCore(components)
    outcome = core.execute(
        request,
        journal=receipt.journal if receipt is not None else EMPTY_JOURNAL,
        manifests=receipt.manifests if receipt is not None else (),
    )

    saved = receipt
    if not dry_run and outcome.version:
        candidate = _receipt_for(outcome, event.repository, event)
        saved = candidate
        if stores:
            try:
                saved = CompositeReceiptStore(stores).save(candidate)
            except Exception as exc:  # noqa: BLE001 - any store, any failure
                raise CliError(
                    f"the release ran, but its receipt could not be written: {exc}. A "
                    "release without a durable receipt cannot be resumed or checked, so "
                    "this job fails rather than reporting a success nobody can audit.",
                    code="receipt-save-failed",
                    exit_code=EXIT_FAILED,
                ) from exc
    return emit(outcome, args, saved if not dry_run else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CliError as exc:
        print(f"continuum release: {exc}", file=sys.stderr)
        return exc.exit_code
    except (config_module.ConfigError, ContractError, HooksError, ReceiptError) as exc:
        print(f"continuum release: {exc}", file=sys.stderr)
        return EXIT_USAGE


__all__ = [
    "CliError",
    "DEFAULT_CONFIG",
    "DEFAULT_RECEIPT_DIR",
    "EXIT_BLOCKED",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_RETRYABLE",
    "EXIT_USAGE",
    "OUTCOME_SCHEMA",
    "emit",
    "main",
    "run",
]
