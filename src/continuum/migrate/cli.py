"""The migration controller's command line: inventory, preflight, plan, apply, rollback.

Five subcommands, one per decision, and the order they run in is the order of the
rules they enforce. ``inventory`` reads. ``preflight`` decides whether the read
is good enough to act on. ``plan`` renders the change. ``apply`` publishes it and
merges it if every gate holds. ``rollback`` undoes it.

Three rules run through all of them.

**Reading and writing are different subcommands.** Only ``apply`` and
``rollback`` construct a client with write intent, and both of them take the token
from the environment this repository's workflow supplies. Running ``inventory``
with no token, or ``apply`` with a read-only token, fails at the first call rather
than at some later point when half the change exists.

**Output is a file and the exit code says which kind of failure it was.**
``NOT_READY`` is a result, not a crash: a preflight that refuses is the preflight
working. Only a plane failure -- the controller could not read, or could not
write -- is a non-zero exit that is not also a ``NOT_READY``, and those are
distinguished so an operator can tell "the repository is not ready" from "the
controller is broken".

**Nothing here accepts a branch name where a SHA belongs.** ``--candidate`` is
required to be a full commit SHA and is refused otherwise, at the command line,
before a single API call is made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from ..shadow import baseline
from ..shadow import cutover as cutover_module
from ..shadow.cutover import PHASE_A, PHASES, phase_writers
from . import apply as apply_module
from . import inventory as inventory_module
from . import plan as plan_module
from . import preflight as preflight_module
from . import rollback as rollback_module
from .client import MigrationClient, MigrationError
from .client import client_from_env
from .plan import ConsumerProfile, CutoverPlan
from .inventory import Inventory

USAGE_ERROR = 2
#: The controller could not do its job: a read failed, a write failed, an input
#: was nonsense. Distinct from NOT_READY, which is an answer.
PLANE_FAILURE = 3
#: The repository is not ready to be cut over. The preflight document says why.
NOT_READY = 1

_FULL_SHA = 40


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", ""):
        parser.print_help()
        return USAGE_ERROR
    try:
        return int(args.handler(args))
    except (
        MigrationError,
        inventory_module.MigrationError,
        plan_module.PlanError,
        apply_module.ApplyError,
        rollback_module.RollbackError,
        baseline.BaselineError,
    ) as error:
        _fail("{}: {}".format(type(error).__name__, error))
        return PLANE_FAILURE
    except FileNotFoundError as error:
        _fail("missing input: {}".format(error))
        return PLANE_FAILURE
    except json.JSONDecodeError as error:
        _fail("malformed JSON input: {}".format(error))
        return PLANE_FAILURE


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


def _cmd_inventory(args: argparse.Namespace) -> int:
    client = _client(args, write=False)
    ledger = _ledger(args)
    reading = inventory_module.capture_inventory(
        client, repository=args.repo, ledger=ledger
    )
    _write(args.out, "inventory.json", reading.describe())
    _say(
        "read {} workflow(s) at {} for {} ({})".format(
            len(reading.workflows),
            (reading.head_sha or "?")[:12],
            reading.repository or "?",
            "complete" if reading.complete else "INCOMPLETE: " + "; ".join(reading.limits),
        )
    )
    # An incomplete reading is a refusal to continue, not a failure of the read:
    # the document is written so the limits can be acted on, and the exit code
    # stops a pipeline that would otherwise plan on top of it.
    return 0 if reading.complete else NOT_READY


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #


def _cmd_preflight(args: argparse.Namespace) -> int:
    reading = _inventory(args)
    verdict = preflight_module.evaluate(
        reading,
        phase=args.phase,
        candidate=args.candidate,
        ledger=_require_ledger(args),
        requirements=preflight_module.requirements_for(reading, args.phase),
        available_reusables=args.reusable,
    )
    _write(args.out, "preflight.json", verdict.describe())
    _say(
        "{}: {}".format(
            verdict.status, preflight_module.summarize(verdict)
        )
    )
    return 0 if verdict.ready else NOT_READY


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def _cmd_plan(args: argparse.Namespace) -> int:
    reading = _inventory(args)
    plan = _plan(args, reading)
    _write(args.out, "plan.json", plan.describe())
    _write(args.out, "ledger-overlay.json", plan.overlay_document)
    # The gate's own vocabulary. `cutover --change-set` reads this document, and
    # it is the one a later `apply` compares the gate's authorization against --
    # so the file the gate is handed is written by the same code that renders the
    # change, not transcribed by whoever is running the migration.
    _write(args.out, "change-set.json", plan.change_set_document())
    for edit in plan.edits:
        _say("{:9} {} [{}]".format(edit.action, edit.path, edit.writer))
    for role, paths in sorted(plan.duplicated_roles.items()):
        _say("REFUSED: {} would still be implemented by {}".format(role, ", ".join(paths)))
    return USAGE_ERROR if plan.duplicated_roles else 0


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def _cmd_apply(args: argparse.Namespace) -> int:
    reading = _inventory(args)
    plan = _plan(args, reading)

    verdict = preflight_module.evaluate(
        reading,
        phase=args.phase,
        candidate=args.candidate,
        ledger=_require_ledger(args),
        requirements=preflight_module.requirements_for(reading, args.phase),
        available_reusables=args.reusable,
    )
    if not verdict.ready:
        # Applying over an unready preflight would install callers whose required
        # secrets are missing, or retire a writer whose replacement is not there.
        _write(args.out, "preflight.json", verdict.describe())
        _say("NOT_READY: {}".format(preflight_module.summarize(verdict)))
        return NOT_READY

    client = _client(args, write=True)
    controller = apply_module.MigrationController(client, plan, repository=args.repo)

    previous = _state(args)
    state = previous or apply_module.MigrationState(
        repository=args.repo or reading.repository,
        phase=args.phase,
        base_sha=args.base or reading.head_sha,
        rollback_revision=reading.head_sha,
    )
    if state.merged:
        _say("already merged; nothing to do")
        _write(args.out, "state.json", state.describe())
        return 0

    state = apply_module.reconcile(
        controller,
        state,
        required=_profile(args).required_ci,
        additional=_profile(args).additional_gates,
        gate=_gate(args, plan, head=state.base_sha or reading.head_sha),
        checkpoint=lambda document: _write(args.out, "state.json", document),
    )
    _write(args.out, "state.json", state.describe())
    for stage in state.stages:
        _say("{} {:14} {}".format("ok  " if stage.ok else "FAIL", stage.name, stage.detail))
    return 0 if state.ready else NOT_READY


def _gate(args: argparse.Namespace, plan: CutoverPlan, *, head: str) -> Any:
    """The cutover gate verdict, read from the artifact the gate workflow wrote.

    An artifact rather than a call, because the gate judges a *window* of evidence
    -- parity, liveness, resolution, approval, canary, rollback -- and that window
    is produced by the shadow plane's own job hours before the controller runs.
    Re-judging it here would mean a second implementation of the requirements, and
    the whole point of the gate is that there is one.

    What makes that artifact a control rather than a formality is that it is
    **bound to this change set at this head**. A verdict is not "yes, migrate" in
    the abstract; it is a judgement about a named head and a named set of file
    changes. So each of those is re-derived here and compared. A verdict recorded
    against a different head, a different phase, or a change set that has since
    gained or lost a path is refused -- which is what stops a stale ``ready`` from
    being spent on a cutover nobody looked at.
    """

    path = args.gate
    if not path:
        # `apply` merges. Merging on the strength of a document nobody produced
        # would make this flag decoration rather than the control it is, so the
        # reconciler's "merge and say so" path is for tests and for an operator
        # running the controller by hand, not for the command that rewrites a
        # repository.
        raise plan_module.PlanError(
            "cutover_gate_required",
            "--gate is required: the cutover gate judges a window of evidence this "
            "controller did not produce, and merging without its verdict is not a "
            "cutover. Use `plan` to see the change without applying it.",
        )

    document = cutover_module.describe(_load(path))
    verdict = {
        "ok": bool(document.get("authorized") or document.get("approved")),
        "detail": _gate_detail(document),
    }
    if not verdict["ok"]:
        return verdict

    _bind_gate_to_plan(document, plan, head=head)
    return verdict


def _gate_detail(document: Mapping[str, Any]) -> str:
    """Why the gate answered the way it did, in the gate's own words."""

    if document.get("authorized"):
        return str(
            (document.get("authorization") or {}).get("note")
            or "the cutover gate authorized this head"
        )
    if document.get("approved"):
        return str(
            (document.get("approval") or {}).get("note")
            or "a human approved this cutover against the recorded window"
        )
    blockers = document.get("blockers") or []
    if blockers:
        return "; ".join(
            str(blocker.get("message") or blocker.get("code") or blocker)
            for blocker in blockers
        )[:1000]
    return "the gate said no, and recorded no reason"


def _bind_gate_to_plan(
    document: Mapping[str, Any], plan: CutoverPlan, *, head: str
) -> None:
    """Refuse a verdict that was not issued for exactly this change, at this head."""

    change_set = document.get("change_set") or document
    judged_head = str(change_set.get("cutover_head", "") or "")
    if not judged_head:
        raise plan_module.PlanError(
            "gate_change_set_missing",
            "the cutover gate verdict names no change set, so it does not say "
            "which head it authorized. Re-run the gate with --change-set.",
        )
    if judged_head != head:
        raise plan_module.PlanError(
            "gate_head_mismatch",
            "the cutover gate authorized {} but this change is built on {}. A "
            "verdict is about one head: re-plan against the gate's head, or "
            "re-run the gate.".format(
                judged_head[:12] or "?", (head or "?")[:12]
            ),
        )

    judged_phase = str(change_set.get("phase", "") or "")
    if judged_phase != plan.phase:
        raise plan_module.PlanError(
            "gate_phase_mismatch",
            "the cutover gate judged phase {!r}; this change is {}.".format(
                judged_phase, plan.phase
            ),
        )

    offered = {
        (
            str(change.get("path", "")),
            str(change.get("action", "")),
            str(change.get("writer", "") or ""),
        )
        for change in change_set.get("changes") or []
        if isinstance(change, Mapping)
    }
    planned = {(edit.path, edit.action, edit.writer or "") for edit in plan.edits}
    if offered != planned:
        raise plan_module.PlanError(
            "gate_change_set_mismatch",
            "the cutover gate authorized {} change(s) and this plan has {}. "
            "Only this plan: {}; only the gate's: {}.".format(
                len(offered),
                len(planned),
                ", ".join(sorted(path for path, _, _ in planned - offered)) or "none",
                ", ".join(sorted(path for path, _, _ in offered - planned)) or "none",
            ),
        )


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #


def _cmd_rollback(args: argparse.Namespace) -> int:
    client = _client(args, write=True)
    # Read with the ledger: without it every current workflow is unclassified, and
    # the stranded-role check -- the one that refuses rather than leaving two
    # implementations of a writer behind -- would see nothing at all.
    reading = inventory_module.capture_inventory(
        client, repository=args.repo, ledger=_require_ledger(args)
    )
    record = rollback_module.read_record(_load(args.record))

    blobs = client.tree_blobs(record.rollback_revision)
    writers = _writers_at(args.ledger, record.rollback_revision, blobs)
    outcome = rollback_module.reconcile(
        client,
        record,
        reading,
        target_blobs={path: blobs.get(path, "") for path in record.retired},
        target_writers=writers,
    )
    _write(args.out, "rollback.json", outcome)
    _say(
        "{}: {}".format(
            "nothing to undo" if outcome.get("noop") else "restoring to {}".format(
                record.rollback_revision
            ),
            ", ".join(outcome.get("reinstated", []) or outcome.get("restored", []) or ["-"]),
        )
    )
    return 0


def _writers_at(
    ledger_path: str, revision: str, blobs: Mapping[str, str]
) -> Dict[str, str]:
    """The writer role each path had at the recorded revision.

    Read from the reviewed ledger, because that is the only statement of writer
    roles anybody signed off on. The revision check matters: an audit of a
    different commit may say a file is a scheduler when at this revision it was
    something else, and a rollback that trusted that would reinstate the wrong file
    under the wrong role.
    """

    ledger = baseline.read_ledger(_load(ledger_path))
    if ledger.audited_head and revision and ledger.audited_head != revision:
        raise rollback_module.RollbackError(
            "ledger_revision_mismatch",
            "the ledger was audited at {} but the record restores {}".format(
                ledger.audited_head, revision
            ),
        )
    return {
        path: entry.writer
        for path, entry in ledger.workflow_map.items()
        if entry.writer and blobs.get(path)
    }


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #


def _cmd_describe(args: argparse.Namespace) -> int:
    print("phases: {}".format(", ".join(PHASES)))
    for phase in PHASES:
        print("  {} replaces: {}".format(phase, ", ".join(phase_writers(phase))))
    print("generated callers:")
    for role, path in sorted(plan_module._CALLER_PATHS.items()):
        print("  {:10} {}".format(role, path))
    print("owned paths:")
    print("  {:10} {}".format("config", inventory_module.CONSUMER_CONFIG_PATH))
    print("  {:10} {}".format("record", inventory_module.CUTOVER_RECORD_PATH))
    print("branches:")
    print("  {:10} {}".format("cutover", apply_module.BRANCH))
    print("  {:10} {}".format("rollback", rollback_module.BRANCH))
    return 0


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum-migrate", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command")

    read = sub.add_parser("inventory", help="read a consumer repository")
    _reading_arguments(read)
    read.add_argument("--out", default="migration-out")
    read.set_defaults(handler=_cmd_inventory)

    ready = sub.add_parser("preflight", help="decide whether a consumer is ready")
    _reading_arguments(ready)
    ready.add_argument("--candidate", required=True, help="the immutable Continuum commit SHA")
    ready.add_argument("--phase", default=PHASE_A, choices=list(PHASES))
    ready.add_argument(
        "--reusable", nargs="*", default=[], help="candidate reusables available to install"
    )
    ready.add_argument("--out", default="migration-out")
    ready.set_defaults(handler=_cmd_preflight)

    render = sub.add_parser("plan", help="render the atomic change")
    _reading_arguments(render)
    render.add_argument("--candidate", required=True)
    render.add_argument("--phase", default=PHASE_A, choices=list(PHASES))
    _profile_arguments(render)
    render.add_argument("--out", default="migration-out")
    render.set_defaults(handler=_cmd_plan)

    publish = sub.add_parser("apply", help="publish and merge the migration")
    _reading_arguments(publish)
    publish.add_argument("--candidate", required=True)
    publish.add_argument("--phase", default=PHASE_A, choices=list(PHASES))
    publish.add_argument("--base", default="", help="the head to build on; the read head otherwise")
    publish.add_argument("--state", default="", help="a previous state document to resume from")
    publish.add_argument(
        "--gate", required=True, help="the cutover gate's verdict artifact"
    )
    publish.add_argument(
        "--reusable", nargs="*", default=[], help="candidate reusables available to install"
    )
    _profile_arguments(publish)
    publish.add_argument("--out", default="migration-out")
    publish.set_defaults(handler=_cmd_apply)

    back = sub.add_parser("rollback", help="restore the recorded pre-cutover revision")
    _reading_arguments(back)
    back.add_argument("--record", required=True, help="the cutover record from the default branch")
    back.add_argument("--out", default="migration-out")
    back.set_defaults(handler=_cmd_rollback)

    show = sub.add_parser("describe", help="print the phases, paths and branches")
    show.set_defaults(handler=_cmd_describe)
    return parser


def _reading_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="owner/name of the consumer")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--ledger", default="", help="the reviewed parity ledger")
    parser.add_argument(
        "--inventory",
        default="",
        help="read this inventory document instead of calling GitHub",
    )


def _profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--required-ci", default="", help="comma-separated blocking check names")
    parser.add_argument("--additional-gates", default="", help="comma-separated advisory gates")
    parser.add_argument("--release-workflow", default="", help="the product release workflow path")
    parser.add_argument("--agent-display-name", default=plan_module.AGENT_DISPLAY_NAME)


def _client(args: argparse.Namespace, *, write: bool = False) -> MigrationClient:
    """The client for this subcommand.

    ``write`` is a statement of intent rather than a capability switch -- the same
    token can do both, and a flag cannot make it do less. What it does is keep
    the two subcommands that write calling this function with the word
    ``write=True``, so the list of things in this file that can change a
    repository is greppable. The actual separation is which workflow each
    subcommand runs in and which token the environment supplies.
    """

    del write
    return client_from_env(args.repo, token_env=args.token_env)


def _inventory(args: argparse.Namespace) -> Inventory:
    if args.inventory:
        return inventory_module.read_inventory(_load(args.inventory))
    return inventory_module.capture_inventory(_client(args, write=False), repository=args.repo)


def _ledger(args: argparse.Namespace) -> Optional[baseline.ParityLedger]:
    if not getattr(args, "ledger", ""):
        return None
    return baseline.read_ledger(_load(args.ledger))


def _require_ledger(args: argparse.Namespace) -> baseline.ParityLedger:
    """The reviewed ledger, or a refusal.

    Every subcommand past `inventory` decides something about writer roles, and
    the ledger is the only statement of those roles anybody signed off on. A
    missing ledger is not a default: it is the absence of the document the
    decision is supposed to rest on.
    """

    path = getattr(args, "ledger", "")
    if not path:
        raise baseline.BaselineError(
            "missing_ledger",
            "--ledger is required: writer roles come from the reviewed parity "
            "ledger, never from a workflow's name.",
        )
    return baseline.read_ledger(_load(path))


def _plan(args: argparse.Namespace, reading: Inventory) -> CutoverPlan:
    if not _FULL_SHA == len(args.candidate or "") or any(
        character not in "0123456789abcdef" for character in args.candidate
    ):
        raise plan_module.PlanError(
            "candidate_not_immutable",
            "--candidate must be a full 40-character commit SHA. A consumer pins a "
            "revision, never a branch: a branch means the repository runs whatever "
            "that branch points at when each workflow starts.",
        )
    return plan_module.build_plan(
        reading, _profile(args), phase=args.phase, candidate=args.candidate
    )


def _profile(args: argparse.Namespace) -> ConsumerProfile:
    return ConsumerProfile(
        required_ci=_names(args.required_ci),
        additional_gates=_names(args.additional_gates),
        release_workflow=args.release_workflow,
        agent_display_name=args.agent_display_name,
    )


def _names(value: str) -> Sequence[str]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _state(args: argparse.Namespace) -> Optional[apply_module.MigrationState]:
    if not getattr(args, "state", ""):
        return None
    return apply_module.read_state(_load(args.state))


def _load(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(out: str, name: str, document: Any) -> None:
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _say(message: str) -> None:
    print(message, flush=True)


def _fail(message: str) -> None:
    print("continuum-migrate: {}".format(message), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
