"""Continuum command line entrypoints.

Every value that originates outside the trusted configuration is passed as an
environment variable or an argument and is never interpolated into a shell
command or a GitHub expression. Credentials are read from the environment and
are never echoed, logged, or written to a result document.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence

from . import config as config_module
from . import pin as pin_module
from .release import adapters as release_adapters
from .release import run as release_run
from .review import commands as command_module
from .review import gate as gate_module
from .review import github as github_module
from .review import llm as llm_module
from .review import providers as providers_module
from .review import reconcile as reconcile_module
from .review import repair as repair_module
from .review import queue as queue_module
from .review import queue_controller as queue_controller_module
from .review import verify as verify_module

EXIT_OK = 0
EXIT_ERROR = 1


def _load_config(path: str, required: bool) -> config_module.ContinuumConfig:
    if required:
        return config_module.load_config(path)
    return config_module.load_optional_config(path)


def _select_target(config: config_module.ContinuumConfig, target_id: str) -> Any:
    """Find the configured target a release command was asked about."""

    target = config.release.target(target_id)
    if target is None:
        available = ", ".join(item.id for item in config.release.targets) or "none configured"
        raise SystemExit(
            f"CONTINUUM_ERROR: no release target {target_id!r} in {config.source}; "
            f"available: {available}"
        )
    return target


def _require_material(
    target: Any, mode: str, environment: Dict[str, str]
) -> None:
    """Honour an explicit expectation about the signing material.

    `present` is how a release job says "this must not degrade". Without it a
    missing secret quietly turns a stable release into an ad-hoc one, and the
    only symptom is a user being asked to grant microphone access again. Saying
    it out loud turns that into a failed job.
    """

    present = release_adapters.has_material(target, environment)
    if mode == "present" and not present:
        raise SystemExit(
            f"CONTINUUM_ERROR: target {target.id!r} signs with "
            f"{target.signing.identity!r}, but "
            + " and ".join(target.signing.secret_names())
            + " are not available in this job. Refusing to sign ad-hoc."
        )
    if mode == "absent" and present:
        raise SystemExit(
            f"CONTINUUM_ERROR: target {target.id!r} was asked to run without its "
            "signing material, but the material is present"
        )


def _print_json(payload: Any, stream=None) -> None:
    # `sys.stdout` is resolved per call rather than bound as a default: a
    # default would capture the stream that existed at import time, so anything
    # that redirected output later would silently miss this document — and a
    # release plan that is not printed is a release plan nobody reviewed.
    target = stream if stream is not None else sys.stdout
    json.dump(payload, target, indent=2, sort_keys=True)
    target.write("\n")
    target.flush()


def _write_output(values: Dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            for line in str(value).splitlines() or [""]:
                handle.write(f"{key}={line}\n")


def _github_token() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        raise SystemExit("CONTINUUM_ERROR: missing GITHUB_TOKEN")
    return token


def _client() -> github_module.GitHubClient:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository:
        raise SystemExit("CONTINUUM_ERROR: missing GITHUB_REPOSITORY")
    api_base = os.environ.get("CONTINUUM_GITHUB_API_BASE", github_module.DEFAULT_API_BASE)
    return github_module.GitHubClient(_github_token(), repository, api_base=api_base)


def _int_env(name: str) -> Optional[int]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"CONTINUUM_ERROR: {name} must be an integer") from None


# -- commands ---------------------------------------------------------------
def cmd_config_check(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        print(f"CONTINUUM_ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    payload = config.describe()
    payload["required_variables"] = config_module.required_variable_names(config)
    payload["required_secrets"] = config_module.required_secret_names(config)
    _print_json(payload)
    _write_output(
        {
            "provider": config.review.provider,
            "enabled": str(config.review.enabled).lower(),
            "block_merge": str(config.review.block_merge).lower(),
            "status_context": config.review.status_context,
        }
    )
    return EXIT_OK


def cmd_command(args: argparse.Namespace) -> int:
    """Parse a comment command and apply the trusted-actor contract.

    The comment body and the actor arrive through the environment, never through
    a workflow expression, so no untrusted text is evaluated as a command.
    """

    body = os.environ.get("CONTINUUM_COMMENT_BODY", "")
    actor = os.environ.get("CONTINUUM_ACTOR", "")
    owner = os.environ.get("CONTINUUM_REPOSITORY_OWNER", "")
    pr_number = _int_env("CONTINUUM_PR_NUMBER")

    if pr_number is None:
        print("::error::Missing CONTINUUM_PR_NUMBER")
        _write_output({"command": "", "finding": "", "trusted": "false"})
        return EXIT_ERROR

    if not command_module.is_trusted_actor(actor, owner):
        print(f"::notice::Comment from untrusted actor {actor[:64]!r} ignored by the review gate.")
        _write_output({"command": "", "finding": "", "trusted": "false"})
        return EXIT_OK

    try:
        parsed = command_module.parse_command(body)
    except command_module.CommandError as exc:
        print(f"::warning::{exc}")
        _write_output({"command": "", "finding": "", "trusted": "true"})
        return EXIT_OK

    if parsed is None or not parsed.is_supported:
        _write_output({"command": "", "finding": "", "trusted": "true"})
        return EXIT_OK

    _write_output(
        {
            "command": parsed.command,
            "finding": parsed.finding or "",
            "trusted": "true",
        }
    )
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR

    if not config.review.enabled:
        result = gate_module.disabled_result(
            config, args.pr, args.head or "", repository=os.environ.get("GITHUB_REPOSITORY", "")
        )
        _emit_result(result, args)
        return EXIT_OK

    client = _client()
    try:
        result = gate_module.run_gate(
            client, config, args.pr, head_sha=args.head, apply=not args.no_apply
        )
    except (gate_module.GateError, providers_module.UnsupportedProvider) as exc:
        # Fail closed: no status is written, so the merge controller sees no
        # green review gate for this HEAD and refuses to merge.
        print(f"::error::Review gate failed: {exc}")
        return EXIT_ERROR
    except github_module.GitHubError as exc:
        print(f"::error::Review gate could not complete: {exc}")
        return EXIT_ERROR

    _emit_result(result, args)
    return EXIT_OK


def _emit_result(result: Dict[str, Any], args: argparse.Namespace) -> None:
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
    _print_json(result)
    _write_output(
        {
            "verdict": str(result.get("verdict", "")),
            "state": str(result.get("state", "")),
            "gate_passed": "true" if result.get("gate_passed") else "false",
            "open_findings": str(result.get("open_findings", 0)),
            "head": str(result.get("head", "")),
        }
    )
    if result.get("gate_passed"):
        print(f"::notice::Review gate passed: {result.get('verdict')} on HEAD {result.get('head')}.")
    else:
        print(
            f"::warning::Review gate is closed: {result.get('verdict')} "
            f"({result.get('state')}) — {result.get('reason')}"
        )


def cmd_reconcile_review(args: argparse.Namespace) -> int:
    """Close the review loop: evaluate the gate and perform its next action.

    This is the entrypoint a review-aware workflow calls on every wake-up. With
    review disabled it resolves the configuration and returns without touching
    the pull request, which is what makes `review: false` cost zero traffic.
    """

    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR

    if not config.review.enabled:
        plan = reconcile_module.disabled_plan(config, args.pr, args.head or "")
        _emit_review_plan(plan, args)
        return EXIT_OK

    client = _client()
    limits = repair_module.RepairPolicy(
        max_attempts_per_head=args.max_attempts_per_head,
        max_attempts_per_finding=args.max_attempts_per_finding,
        max_rereviews_per_head=args.max_rereviews,
        max_batch_size=args.max_batch_size,
        agent_timeout_minutes=args.agent_timeout_minutes,
    )
    dispatch = _agent_dispatch(client) if args.dispatch_workflow else None
    try:
        plan = reconcile_module.reconcile(
            client,
            config,
            args.pr,
            head_sha=args.head,
            apply=not args.no_apply,
            policy=limits,
            dispatch=dispatch,
            dispatch_workflow=args.dispatch_workflow or "",
            dispatch_ref=args.dispatch_ref or "",
        )
    except (
        reconcile_module.ReconcileError,
        gate_module.GateError,
        providers_module.UnsupportedProvider,
    ) as exc:
        # Fail closed: the gate was not published, so the merge controller sees
        # no green review gate for this HEAD and refuses to merge.
        print(f"::error::Review reconciliation failed: {exc}")
        return EXIT_ERROR
    except github_module.GitHubError as exc:
        print(f"::error::Review reconciliation could not complete: {exc}")
        return EXIT_ERROR

    _emit_review_plan(plan, args)
    return EXIT_OK


def _agent_dispatch(client: Any) -> Any:
    """Dispatch the agent workflow for a bounded review repair.

    The workflow file, its ref, the pull-request number, and the head ref are all
    decided inside the review controller, which reads the head ref from the live
    pull request. Nothing here accepts a caller-supplied ref.
    """

    def dispatch(workflow: str, ref: str, inputs: Dict[str, str]) -> None:
        client.dispatch_workflow(workflow, ref, inputs)

    return dispatch


def _emit_review_plan(plan: Dict[str, Any], args: argparse.Namespace) -> None:
    repair = plan.get("repair") or {}
    for line in _review_plan_notices(plan):
        print(line)
    _print_json(plan)
    _write_output(
        {
            "review_enabled": str(bool(plan.get("enabled"))).lower(),
            "action": str(repair.get("action") or ""),
            "verdict": str(plan.get("verdict") or ""),
            "gate_passed": "true" if plan.get("gate_passed") else "false",
            "paused": "true" if repair.get("paused") else "false",
            "dispatched": "true" if plan.get("dispatched") else "false",
            "requests_sent": str(plan.get("requests_sent", 0)),
            "head": str(plan.get("head") or ""),
        }
    )
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if getattr(args, "prompt_out", None) and plan.get("agent_prompt"):
        with open(args.prompt_out, "w", encoding="utf-8") as handle:
            handle.write(str(plan["agent_prompt"]))
            handle.write("\n")


def _review_plan_notices(plan: Dict[str, Any]) -> list:
    """Operator-facing lines. Every value here comes from the engine's decision."""

    if not plan.get("enabled"):
        return ["::notice::Review is disabled by configuration; no review traffic was generated."]
    repair = plan.get("repair") or {}
    action = str(repair.get("action") or "")
    head = str(plan.get("head") or "")
    notices = [
        "::notice::Review action {action} on HEAD {head} (verdict {verdict}, "
        "{open} open finding(s)).".format(
            action=action,
            head=head[:12],
            verdict=plan.get("verdict") or "",
            open=plan.get("open_findings", 0),
        )
    ]
    if action == repair_module.ACTION_PAUSE:
        notices.append(f"::error::Review repair paused: {repair.get('reason')}")
    elif action == repair_module.ACTION_REPAIR:
        notices.append("::notice::Dispatching a bounded review repair for this pull request.")
    return notices


def cmd_review_prompt(args: argparse.Namespace) -> int:
    """Print the repair instruction the controller stored for one exact HEAD.

    The privileged agent step runs this instead of rebuilding the instruction,
    so the agent executes what the review controller decided rather than a second
    reading of state that may have moved.
    """

    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR

    head = args.head or _current_head()
    try:
        prompt = reconcile_module.agent_prompt(_client(), args.pr, head)
    except (
        reconcile_module.ReconcileError,
        providers_module.UnsupportedProvider,
    ) as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR
    except github_module.GitHubError as exc:
        print(f"::error::Could not read the review repair instruction: {exc}")
        return EXIT_ERROR

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(prompt)
            handle.write("\n")
    sys.stdout.write(prompt + "\n")
    return EXIT_OK


def _current_head() -> str:
    """The checked-out commit, for callers that do not pass --head."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR
    if not config.review.enabled:
        print("::notice::Review provider is disabled; nothing to verify.")
        _print_json({"verdict": "NONE", "skipped": True})
        return EXIT_OK

    finding_id = (args.finding or "").strip().upper()
    if not command_module.FINDING_ID_RE.match(finding_id):
        print("::error::usage: verify --finding <finding-id> (for example CRV-1A2B3C4D)")
        return EXIT_ERROR

    client = _client()
    try:
        head = args.head or (client.get_pull(int(args.pr)).get("head") or {}).get("sha", "")
        settings = config.review.provider_settings()
        snapshot = providers_module.collect_snapshot(
            config.review.provider,
            client,
            int(args.pr),
            head,
            settings,
            # Honor the same toggle the verdict write honors, so `--no-apply`
            # stays a true dry run of the whole command.
            apply=not args.no_apply,
        )
        llm_client = llm_module.client_from_environment()
        outcome = verify_module.verify_finding(
            client,
            snapshot,
            pr_number=int(args.pr),
            head=head,
            finding_id=finding_id,
            llm=llm_client,
            apply=not args.no_apply,
        )
    except (verify_module.VerifyError, llm_module.ProviderConfigError) as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR
    except (github_module.GitHubError, llm_module.ProviderRequestError) as exc:
        print(f"::error::Finding verification failed: {exc}")
        return EXIT_ERROR

    _print_json(outcome)
    _write_output({"verdict": outcome["verdict"], "finding": outcome["finding"]})
    return EXIT_OK


def cmd_provider(args: argparse.Namespace) -> int:
    """Report the configured provider and its non-sensitive requirements."""

    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR
    settings = config.review.provider_settings()
    model = os.environ.get("CONTINUUM_REVIEW_MODEL", "").strip()
    payload: Dict[str, Any] = {
        "provider": config.review.provider,
        "enabled": config.review.enabled,
        "block_merge": config.review.block_merge,
        "status_context": config.review.status_context,
        "required_variables": config_module.required_variable_names(config),
        "required_secrets": config_module.required_secret_names(config),
    }
    if settings is not None and hasattr(settings, "bot_login"):
        payload["bot_login"] = settings.bot_login
    if model:
        payload["routed_model"] = llm_module.route_model(model)
    _print_json(payload)
    return EXIT_OK


def _wake_up() -> queue_module.WakeUp:
    """The wake-up that started this run, read from the runner environment.

    The event is advisory context for the log. The reconciliation itself always
    recomputes the queue from GitHub state, so a mis-parsed or missing event
    payload can never cause a wrong decision.
    """

    event = (os.environ.get("CONTINUUM_WAKE_EVENT") or "workflow_dispatch").strip()
    action = (os.environ.get("CONTINUUM_WAKE_ACTION") or "").strip()
    reason = (os.environ.get("CONTINUUM_WAKE_REASON") or "").strip()
    return queue_module.WakeUp(
        event=event,
        action=action,
        pr_number=_int_env("CONTINUUM_WAKE_PR"),
        reason=reason,
    )


def cmd_queue(args: argparse.Namespace) -> int:
    """Reconcile the review queue and schedule at most one provider request."""

    try:
        config = _load_config(args.config, required=not args.allow_missing)
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        return EXIT_ERROR

    wake = _wake_up()
    if not wake.is_queue_relevant:
        print(f"::notice::{wake.describe()} cannot change queue eligibility; nothing to do.")
        _write_output({"action": "ignored", "dispatched": "false", "selected_pr": ""})
        return EXIT_OK

    client = _client()
    try:
        plan = queue_controller_module.reconcile(
            client,
            config,
            wake=wake,
            apply=not args.no_apply,
            wait_ms=int(args.wait_minutes) * 60_000,
        )
    except (queue_controller_module.QueueError, github_module.GitHubError) as exc:
        print(f"::error::Review queue reconciliation failed: {exc}")
        return EXIT_ERROR
    except providers_module.UnsupportedProvider as exc:
        print(f"::error::Review queue cannot run: {exc}")
        return EXIT_ERROR

    payload = plan.describe()
    for line in plan.logs:
        print(f"::notice::{line}")
    _print_json(payload)
    _write_output(
        {
            "action": plan.action,
            "dispatched": str(plan.dispatched).lower(),
            "selected_pr": str(plan.selected.pr_number) if plan.selected else "",
            "queue_size": str(len(plan.queue)),
            "cooldown_ms": str(plan.cooldown.remaining_ms(plan.now_ms)),
        }
    )
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return EXIT_OK


def _plan(args: argparse.Namespace) -> Tuple[Any, Any, Dict[str, str]]:
    """Validate configuration and build the plan a target would run.

    The bound environment comes back with the plan on purpose. The adapter joins
    the configured secret names to the names its own steps read, and a plan run
    against the pre-join environment would look for a variable that was never
    configured — indistinguishable, from the job log, from a missing secret.
    """

    config = _load_config(args.config, required=not args.allow_missing)
    target = _select_target(config, args.target)
    environment = release_run.base_environment()
    _require_material(target, args.signing_material, environment)
    try:
        bound = release_adapters.bind_material(target, environment)
        plan = release_adapters.plan_for(target, environment=environment)
    except release_adapters.SigningUnavailable as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None
    except release_adapters.TargetNotExecutable as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None
    except config_module.ConfigError as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None
    return target, plan, bound


def _require_values(plan: Any, environment: Dict[str, str]) -> None:
    """Refuse to start a plan that is missing an ordinary value it needs.

    The runner would catch this too, but only at the step that reads it — which
    for a bundle is after a keychain exists and a private key has been imported.
    Checking first turns "the job failed somewhere in signing" into "the job
    needs CONTINUUM_RELEASE_VERSION set".
    """

    missing = [name for name in plan.required_env if not (environment.get(name) or "").strip()]
    if not missing:
        return
    raise SystemExit(
        f"CONTINUUM_ERROR: target {plan.target!r} needs "
        + " and ".join(missing)
        + " in the environment; nothing was created and no signing tool was run"
    )


def _report_plan(plan: Any) -> None:
    """Print the plan, and shout when the plan is a degraded one.

    A degraded build has to be visible to a human reading the job, not only to
    a machine reading the JSON: the whole point of the fallback is that it is
    loud, and a field nobody reads is not loud.
    """

    if plan.degraded:
        print(f"::warning::DEGRADED RELEASE {plan.target}: {plan.degradation_reason}")
        for note in plan.notes:
            print(f"::warning::{note}")
    for step in plan.step_list:
        marker = "  teardown" if step.is_teardown else "  step      "
        print(f"{marker} {step.name}: {step.purpose}")
    if plan.signature_pinned:
        print(
            f"Pinned signing identity: {plan.pinned_identity!r}. The plan asserts it "
            "is present in what it signed."
        )
    else:
        print(
            "No signing identity is pinned: this plan cannot and does not preserve a "
            "stable identity."
        )


def _version_strategy(args: argparse.Namespace) -> Any:
    """The version strategy this run was asked for, built from its arguments.

    `release-pr` is not offered: a pull request that names the version is a
    strategy a repository opts into by registering it, and offering it here would
    put a driver in the command line that no consumer has configured.
    """

    from .release import version as version_module

    if args.version_strategy == version_module.STRATEGY_EXPLICIT:
        return version_module.ExplicitVersion(requested=(args.version or "").strip())
    if args.version_strategy == version_module.STRATEGY_PROJECT_FILE:
        # The default location, not a requirement: a repository that keeps its
        # version somewhere else names the strategy's own options in its own
        # wiring rather than through this argument.
        return version_module.ProjectFileVersion(
            path=os.path.join(args.workdir or ".", "VERSION")
        )
    if args.version_strategy == version_module.STRATEGY_TAG:
        return version_module.TagVersion(tag=(args.tag or "").strip())
    raise SystemExit(
        f"CONTINUUM_ERROR: unknown version strategy {args.version_strategy!r}; "
        "available: " + ", ".join(version_module.strategies())
    )


def _release_event(args: argparse.Namespace, target: Any) -> Any:
    """The event a `release run` treats itself as answering.

    Built from the tag when one is given, and otherwise from the commit being
    released, because the release core refuses to release a version nothing
    declares. A tag is the stronger of the two: it is what a human pushed when
    they decided this was the release.
    """

    from .release.contract import ReleaseEvent

    tag = args.tag or ""
    return ReleaseEvent(
        repository=args.repository,
        name="tag-push" if tag else "release-dispatch",
        sha=args.sha or "",
        ref=f"refs/tags/{tag}" if tag else f"refs/heads/{args.branch}",
        tag=tag,
        default_branch=args.branch,
        delivery=f"continuum-release-{args.delivery}",
    )


def _current_head(workdir: str) -> str:
    """The commit this checkout is at, or `""` when it cannot be read.

    Returned as a function so the adapter can ask the question and this module
    stays the only place that starts a `git` process. A checkout that is not a
    git repository — a tarball, a vendor drop — yields `""`, which the adapter
    reads as "there is nothing to check against" rather than as a mismatch.
    """

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            shell=False,
            cwd=workdir or None,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def cmd_release_run(args: argparse.Namespace) -> int:
    """Walk the release chain for one target, and print what it decided.

    This is the whole integration for a declared target: the configuration
    supplies the target, `ReleaseRequest.from_config` carries its options to the
    adapter, and the core walks eligibility, version, build, sign, verify, and
    publication in the order it always walks them. There is no Apple path here
    and no consumer path here — the command knows only which components were
    handed to it.

    A dry run is the same walk with `dry_run` set, so a plan cannot describe a
    release the run would not perform.
    """

    from .release import apple as apple_module
    from .release import wiring as wiring_module
    from .release.contract import ContractError
    from .release.core import ReleaseCore
    from .release.version import get_strategy

    config = _load_config(args.config, required=not args.allow_missing)
    target = _select_target(config, args.target)
    environment = release_run.base_environment()
    _require_material(target, args.signing_material, environment)

    adapter = apple_module.AppleAdapter(
        environment=environment,
        workdir=args.workdir,
        git_revision=_current_head,
    )
    try:
        strategy = _version_strategy(args)
        plane = wiring_module.components(
            config,
            version=strategy,
            adapters={target.adapter: adapter},
            publishers=(),
        )
    except ContractError as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None

    event = _release_event(args, target)
    try:
        request = wiring_module.request_from_config(
            config,
            event,
            workdir=args.workdir,
            dry_run=args.dry_run,
            requested_version=(args.version or "").strip(),
        )
    except ContractError as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None

    outcome = ReleaseCore(plane).execute(request)
    payload = outcome.describe()
    _print_json(payload)
    _write_output(
        {
            "status": outcome.status,
            "version": outcome.version,
            "tag": outcome.tag,
            "source_sha": outcome.source_sha,
            "dry_run": str(outcome.dry_run).lower(),
        }
    )
    if not outcome.ok:
        print(f"::error::release {outcome.status}: {outcome.no_op_reason or 'see the journal'}")
    return EXIT_OK if outcome.ok else EXIT_ERROR



def cmd_release_plan(args: argparse.Namespace) -> int:
    """Show what a release target will do, without doing it."""

    _target, plan, _environment = _plan(args)
    payload = plan.describe()
    _print_json(payload)
    _write_output(
        {
            "target": plan.target,
            "adapter": plan.adapter,
            "signing_mode": plan.signing_mode,
            "signature_pinned": str(plan.signature_pinned).lower(),
            "degraded": str(plan.degraded).lower(),
            "steps": str(len(plan.steps)),
        }
    )
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    _report_plan(plan)
    return EXIT_OK


def cmd_release_sign(args: argparse.Namespace) -> int:
    """Run a target's plan: sign, seal, verify, and always tear down."""

    target, plan, environment = _plan(args)
    _report_plan(plan)
    if args.dry_run:
        print("::notice::Dry run: no signing tool was executed.")
        _print_json({"target": plan.target, "dry_run": True, "ok": True})
        return EXIT_OK

    _require_values(plan, environment)

    # Ask for the toolchain before the first step rather than after: a plan that
    # creates a keychain and then discovers `codesign` is missing has already
    # touched the machine it was told it could not use.
    try:
        release_adapters.ensure_toolchain(release_adapters.get(plan.adapter))
    except release_adapters.TargetNotExecutable as exc:
        print(f"::error::{exc}")
        raise SystemExit(f"CONTINUUM_ERROR: {exc}") from None

    result = release_run.run_plan(plan, environment=environment, workdir=args.workdir or ".")
    _print_json(result.describe())
    _write_output(
        {
            "target": plan.target,
            "signing_mode": plan.signing_mode,
            "signature_pinned": str(result.ok and plan.signature_pinned).lower(),
            "degraded": str(plan.degraded).lower(),
            "signed": str(result.ok).lower(),
        }
    )
    if result.ok:
        print(f"::notice::Signed and verified {plan.summary}.")
        return EXIT_OK

    failure = result.failure
    explained = release_adapters.explain_failure(
        plan.adapter, failure.step if failure else "", failure.detail if failure else ""
    )
    print(f"::error::{plan.target}: {failure.message if failure else 'failed'}")
    if explained.message:
        print(f"::error::{explained.message}")
    if explained.remediation:
        print(f"::error::Try: {explained.remediation}")
    if failure and failure.detail:
        print(failure.detail)
    return EXIT_ERROR


DEFAULT_INGRESS = ".github/workflows/continuum.yml"


def _read_ingress(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SystemExit(f"CONTINUUM_ERROR: cannot read the generated ingress: {exc}")


def cmd_release_pin_show(args: argparse.Namespace) -> int:
    """Report the exact Continuum release a consumer currently executes."""

    resolved = pin_module.require_single_release_pin(
        _read_ingress(args.ingress), args.ingress
    )
    _print_json(
        {
            "ingress": args.ingress,
            "reference": resolved.value,
            "path": resolved.path,
            "ref": resolved.ref,
            "kind": resolved.kind,
            "ok": True,
        }
    )
    return EXIT_OK


def cmd_release_pin_verify(args: argparse.Namespace) -> int:
    """Fail unless the consumer holds exactly one exact Continuum release.

    This is the check an upgrade runs *after* the pin changes and a rollback
    runs after the pin is restored: it proves the file still says one thing,
    that the thing is exact, and that the thing names the release entrypoint
    rather than a single controller.
    """

    try:
        resolved = pin_module.require_single_release_pin(
            _read_ingress(args.ingress), args.ingress
        )
    except pin_module.PinError as exc:
        print(f"::error::{exc}")
        _print_json({"ingress": args.ingress, "ok": False, "error": str(exc)})
        return EXIT_ERROR
    _print_json(
        {
            "ingress": args.ingress,
            "reference": resolved.value,
            "kind": resolved.kind,
            "ok": True,
        }
    )
    print(
        f"::notice::{args.ingress} executes Continuum {resolved.ref} "
        "as a single coherent release."
    )
    return EXIT_OK


def _pin_change(args: argparse.Namespace, move) -> int:
    """Apply one explicit pin change, and only when it was asked for by name.

    There is no background path here and no discovery mode: a pin moves when an
    operator names the target release, and nothing else can move it. Without
    `--write` the result is printed rather than written, so the change can be
    reviewed as a diff before it lands.
    """

    text = _read_ingress(args.ingress)
    try:
        updated = move(text, args.ref, args.ingress)
    except pin_module.PinError as exc:
        print(f"::error::{exc}")
        _print_json({"ingress": args.ingress, "ok": False, "error": str(exc)})
        return EXIT_ERROR

    before = pin_module.require_single_release_pin(text, args.ingress)
    after = pin_module.require_single_release_pin(updated, args.ingress)
    if not args.write:
        sys.stdout.write(updated)
        sys.stdout.flush()
        _print_json(
            {
                "ingress": args.ingress,
                "from": before.ref,
                "to": after.ref,
                "written": False,
                "ok": True,
            }
        )
        return EXIT_OK

    with open(args.ingress, "w", encoding="utf-8") as handle:
        handle.write(updated)
    _print_json(
        {
            "ingress": args.ingress,
            "from": before.ref,
            "to": after.ref,
            "written": True,
            "ok": True,
        }
    )
    print(
        f"::notice::{args.ingress} now selects Continuum {after.ref} "
        f"(was {before.ref}). Run the consumer's own validation before merging."
    )
    return EXIT_OK


def cmd_release_pin_upgrade(args: argparse.Namespace) -> int:
    return _pin_change(args, pin_module.upgrade)


def cmd_release_pin_rollback(args: argparse.Namespace) -> int:
    return _pin_change(args, pin_module.rollback)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="continuum", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--config",
            default=os.environ.get("CONTINUUM_CONFIG", config_module.DEFAULT_CONFIG_PATH),
            help="Path to the validated Continuum configuration file.",
        )
        target.add_argument(
            "--allow-missing",
            action="store_true",
            help="Treat a missing configuration file as review.provider: none.",
        )

    config_check = sub.add_parser("config-check", help="Validate .continuum.yml.")
    add_common(config_check)
    config_check.set_defaults(func=cmd_config_check)

    provider = sub.add_parser("provider", help="Report the configured review provider.")
    add_common(provider)
    provider.set_defaults(func=cmd_provider)

    command = sub.add_parser("command", help="Parse a review comment command.")
    add_common(command)
    command.set_defaults(func=cmd_command)

    gate = sub.add_parser("gate", help="Run the normalized review gate.")
    add_common(gate)
    gate.add_argument("--pr", type=int, required=True)
    gate.add_argument("--head", default=None, help="Override the current HEAD sha.")
    gate.add_argument("--out", default=None, help="Write the normalized result document here.")
    gate.add_argument(
        "--no-apply",
        action="store_true",
        help="Evaluate without writing tracker, review, or status.",
    )
    gate.set_defaults(func=cmd_gate)

    review = sub.add_parser(
        "review",
        help="Run the review controller.",
        description=(
            "Evaluate the normalized review gate for one pull request and perform the "
            "single next action the repair contract allows: hand the open findings to a "
            "bounded agent repair, ask the provider to re-check them on the exact HEAD, "
            "request one full re-review of an unchanged HEAD, wait, or fail closed. "
            "With review disabled this resolves the configuration and writes nothing."
        ),
    )
    review_sub = review.add_subparsers(dest="review_command", required=True)

    reconcile_review = review_sub.add_parser(
        "reconcile", help="Evaluate the gate and perform its next action."
    )
    add_common(reconcile_review)
    reconcile_review.add_argument("--pr", type=int, required=True)
    reconcile_review.add_argument(
        "--head", default=None, help="Override the current HEAD sha."
    )
    reconcile_review.add_argument(
        "--no-apply",
        action="store_true",
        help="Decide without writing tracker, verdict, status, requests, or ledger.",
    )
    reconcile_review.add_argument(
        "--max-attempts-per-head",
        type=int,
        default=3,
        help="Repair dispatches allowed for one exact HEAD.",
    )
    reconcile_review.add_argument(
        "--max-attempts-per-finding",
        type=int,
        default=2,
        help="Repair dispatches allowed for one finding across the whole PR.",
    )
    reconcile_review.add_argument(
        "--max-rereviews",
        type=int,
        default=1,
        help="Full re-reviews allowed for a HEAD whose repair produced no change.",
    )
    reconcile_review.add_argument(
        "--max-batch-size",
        type=int,
        default=repair_module.DEFAULT_MAX_BATCH_SIZE,
        help="Open findings handed to one repair attempt.",
    )
    reconcile_review.add_argument(
        "--agent-timeout-minutes",
        type=int,
        default=20,
        help="Wall-clock budget for one dispatched agent repair.",
    )
    reconcile_review.add_argument(
        "--dispatch-workflow",
        default=os.environ.get("CONTINUUM_REVIEW_AGENT_WORKFLOW", ""),
        help="Workflow dispatched for a review repair (default: no dispatch).",
    )
    reconcile_review.add_argument(
        "--dispatch-ref",
        default=os.environ.get("CONTINUUM_BASE_BRANCH", ""),
        help="Branch the repair workflow is dispatched from.",
    )
    reconcile_review.add_argument(
        "--out", default=None, help="Write the reconcile plan document here."
    )
    reconcile_review.add_argument(
        "--prompt-out",
        default=None,
        help="Write the repair agent instruction here when one is dispatched.",
    )
    reconcile_review.set_defaults(func=cmd_reconcile_review)

    review_prompt = review_sub.add_parser(
        "prompt",
        help="Print the stored repair instruction for one exact HEAD.",
        description=(
            "Print the bounded review-repair instruction the review controller stored "
            "for the given commit. Exits non-zero when no trusted instruction exists "
            "for that HEAD, so a repair agent never runs an instruction assembled "
            "against a different diff."
        ),
    )
    add_common(review_prompt)
    review_prompt.add_argument("--pr", type=int, required=True)
    review_prompt.add_argument(
        "--head", default=None, help="The commit the instruction must name (default: HEAD)."
    )
    review_prompt.add_argument(
        "--out", default=None, help="Write the instruction here as well as to stdout."
    )
    review_prompt.set_defaults(func=cmd_review_prompt)

    verify = sub.add_parser("verify", help="Re-check one tracked finding.")
    add_common(verify)
    verify.add_argument("--pr", type=int, required=True)
    verify.add_argument("--finding", required=True)
    verify.add_argument("--head", default=None)
    verify.add_argument("--no-apply", action="store_true")
    verify.set_defaults(func=cmd_verify)

    queue = sub.add_parser(
        "queue",
        help="Reconcile the review queue.",
        description=(
            "Recompute every eligible review candidate from current GitHub state and "
            "schedule at most one provider request. Events are wake-ups: they are read "
            "from CONTINUUM_WAKE_* and never trusted as state."
        ),
    )
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)

    reconcile = queue_sub.add_parser(
        "reconcile", help="Recompute the queue and dispatch one candidate."
    )
    add_common(reconcile)
    reconcile.add_argument(
        "--no-apply",
        action="store_true",
        help="Decide without sending a provider request or writing a lock.",
    )
    reconcile.add_argument(
        "--wait-minutes",
        type=int,
        default=0,
        help=(
            "Stay alive this long to re-reconcile when only a provider cooldown is "
            "holding the queue. Event coverage, not this window, drives progression."
        ),
    )
    reconcile.add_argument("--out", default=None, help="Write the plan document here.")
    reconcile.set_defaults(func=cmd_queue)

    release = sub.add_parser(
        "release",
        help="Build, sign, and verify a release target.",
        description=(
            "Release automation is a target in .continuum.yml plus an adapter that "
            "turns it into a plan. The plan is a value: it can be reviewed without a "
            "signing tool anywhere in sight, and nothing it contains is ever handed "
            "to a shell."
        ),
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)

    plan = release_sub.add_parser(
        "plan",
        help="Show what a target will do.",
        description=(
            "Build the plan for one target and print it. Planning needs no signing "
            "toolchain, so configuration and plan review can run anywhere."
        ),
    )
    add_common(plan)
    plan.add_argument("--target", required=True, help="Id of the configured release target.")
    plan.add_argument(
        "--signing-material",
        choices=("auto", "present", "absent"),
        default="auto",
        help=(
            "What this job expects of the signing secrets. 'present' fails the run "
            "rather than degrading; 'absent' proves the fallback path."
        ),
    )
    plan.add_argument("--out", default=None, help="Write the plan document here.")
    plan.set_defaults(func=cmd_release_plan)

    sign = release_sub.add_parser(
        "sign",
        help="Run a target's plan.",
        description=(
            "Sign the configured artifacts, assert that what was signed carries the "
            "pinned identity, verify the signatures, and remove the signing material "
            "whatever the outcome."
        ),
    )
    add_common(sign)
    sign.add_argument("--target", required=True, help="Id of the configured release target.")
    sign.add_argument(
        "--signing-material",
        choices=("auto", "present", "absent"),
        default="auto",
        help="What this job expects of the signing secrets. See 'release plan'.",
    )
    sign.add_argument(
        "--workdir",
        default="",
        help="Directory holding the built artifacts (default: the current directory).",
    )
    sign.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without executing any signing tool.",
    )
    sign.set_defaults(func=cmd_release_sign)

    run = release_sub.add_parser(
        "run",
        help="Walk the whole release chain for a target.",
        description=(
            "Build, sign, package, verify, and publish one configured target by "
            "walking the generic release chain: the target's own configuration "
            "supplies its options, the core supplies the stages, and this command "
            "supplies nothing platform-specific. A dry run walks the same chain "
            "without executing a tool or writing a file, so a plan cannot describe "
            "a release the run would not perform."
        ),
    )
    add_common(run)
    run.add_argument("--target", required=True, help="Id of the configured release target.")
    run.add_argument(
        "--signing-material",
        choices=("auto", "present", "absent"),
        default="auto",
        help="What this job expects of the signing secrets. See 'release plan'.",
    )
    run.add_argument(
        "--workdir",
        default="",
        help="Directory holding the checkout to release (default: the current directory).",
    )
    run.add_argument(
        "--sha",
        default=os.environ.get("GITHUB_SHA", ""),
        help="The commit this release is approved for (default: GITHUB_SHA).",
    )
    run.add_argument(
        "--tag",
        default=os.environ.get("GITHUB_REF_NAME", "") if os.environ.get("GITHUB_REF_TYPE") == "tag" else "",
        help="The release tag being published (default: GITHUB_REF_NAME on a tag event).",
    )
    run.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="Owner/name of the repository being released.",
    )
    run.add_argument(
        "--branch",
        default=os.environ.get("GITHUB_REF_NAME", "main"),
        help="The branch this event came from, used when no tag was given.",
    )
    run.add_argument(
        "--delivery",
        default=os.environ.get("GITHUB_RUN_ID", "0"),
        help="Identifier for this run, so a second run over the same tag is distinguishable.",
    )
    run.add_argument(
        "--version-strategy",
        default="tag",
        help=(
            "Where the version comes from: 'tag' takes the version from the tag "
            "being released, 'explicit' requires --version, 'project-file' reads "
            "it from the checkout."
        ),
    )
    run.add_argument("--version", default="", help="Version to release, for 'explicit'.")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk the chain without executing a tool or writing a file.",
    )
    run.set_defaults(func=cmd_release_run)

    release_pin = release_sub.add_parser(
        "pin",
        help="Read, verify, or change the consumer's one Continuum release reference.",
        description=(
            "ADR-0002 makes the Continuum release, not the module, the unit of "
            "version selection. A consumer holds exactly one literal reference "
            "in its generated ingress, and it is either an exact SemVer release "
            "tag or a full commit SHA. These commands read it, assert it, and "
            "change it on explicit request only: nothing here runs because a "
            "newer Continuum release exists, and there is no automatic repin."
        ),
    )
    release_pin_sub = release_pin.add_subparsers(dest="release_pin_command", required=True)

    def add_ingress(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--ingress",
            default=DEFAULT_INGRESS,
            help="Path to the consumer's generated ingress workflow.",
        )

    pin_show = release_pin_sub.add_parser(
        "show", help="Report the Continuum release this consumer executes."
    )
    add_ingress(pin_show)
    pin_show.set_defaults(func=cmd_release_pin_show)

    pin_verify = release_pin_sub.add_parser(
        "verify",
        help="Fail unless exactly one exact Continuum release is selected.",
    )
    add_ingress(pin_verify)
    pin_verify.set_defaults(func=cmd_release_pin_verify)

    for name, help_text, handler in (
        ("upgrade", "Move the single reference to the release you name.", cmd_release_pin_upgrade),
        ("rollback", "Restore the single reference to the release you name.", cmd_release_pin_rollback),
    ):
        mover = release_pin_sub.add_parser(name, help=help_text)
        add_ingress(mover)
        mover.add_argument(
            "--ref",
            required=True,
            help="Exact release tag (v0.1.1) or full commit SHA to select.",
        )
        mover.add_argument(
            "--write",
            action="store_true",
            help="Write the ingress in place. Without it the result is printed.",
        )
        mover.set_defaults(func=handler)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
