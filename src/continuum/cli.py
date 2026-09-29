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
import sys
from typing import Any, Dict, Optional, Sequence

from . import config as config_module
from .release import adapters as release_adapters
from .release import run as release_run
from .review import commands as command_module
from .review import gate as gate_module
from .review import github as github_module
from .review import llm as llm_module
from .review import providers as providers_module
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

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
