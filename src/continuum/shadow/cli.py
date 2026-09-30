"""The shadow command line: the one place CI, the bridge, and a person all use.

The decision subcommands, one per artifact:

* ``run``      -- turn one real lifecycle event into a journal (and optionally a
  replay case). The command the bridge workflow calls.
* ``observe``  -- read production for one event's correlation window and write the
  observer's own ledger and outcome, independently of any journal.
* ``parity``   -- compare a journal with a captured NanoDictate outcome.
* ``reconcile`` -- pair journals with the observer's ledger and write the parity
  results that pairing implies.
* ``liveness`` -- account for every accepted event in a window.
* ``replay``   -- re-run a captured case, optionally against a second engine.
* ``cutover``  -- judge a window against the scenario requirements.

Two rules run through all of them:

* **The barrier goes up first.** ``run`` and ``replay`` install the write barrier
  before they read anything else, and before they import the live adapters. A
  subcommand that could write would be a subcommand nobody runs in a repository
  they care about, so the ordering is enforced rather than documented.

* **Output is a file, and the exit code says what happened.** Every subcommand
  writes a document into ``--out`` and prints one summary line. A run that
  produced a journal saying ``denied`` has *succeeded* -- the refusal is the
  result, not an error -- so a non-zero exit means the shadow plane itself could
  not do its job, and never that Continuum said no to something.

``observe`` and ``reconcile`` are the two commands that make the plane a
comparison rather than a report. They are separate from ``run`` because the
observer must not need the shadow run and the shadow run must not need the
observer: a production outcome arrives over hours, across workflow runs the
journal's run never saw, and a plane that could only observe during its own run
would report every long-running decision as absent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import barrier, baseline, cutover, engine, liveness, observer, parity, replay, report
from .effects import READ_ONLY_METHODS
from .config import ConfigError, ShadowConfig
from .config import load as load_config
from .event import EventError, ShadowEvent
from .event import from_github_event, from_payload as event_from_payload
from .journal import ShadowJournal
from .observation import ObservedOutcome, outcome_from_payload
from .planner import scenarios as planned_scenarios
from .planner import plan
from .state import ObservedState, StateError, state_from_payload

#: The barrier report for this process, kept here because the audit hook cannot
#: be uninstalled: once a command has installed it, the rest of the process is a
#: shadow run whether it meant to be or not.
_BARRIER: Optional["barrier.BarrierReport"] = None

USAGE_ERROR = 2
#: A run that could not be recorded at all. Distinct from a refusal, which is a
#: result.
PLANE_FAILURE = 3
#: The plane worked and the *validation* failed: a divergence, a stall, a gate
#: that is not ready. CI fails the job on this; the artifact says why.
VALIDATION_FAILED = 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", ""):
        parser.print_help()
        return USAGE_ERROR
    try:
        return int(args.handler(args))
    except (
        EventError,
        StateError,
        ConfigError,
        replay.ReplayError,
        cutover.CutoverError,
        baseline.BaselineError,
        observer.ObserverError,
    ) as error:
        _fail("{}: {}".format(type(error).__name__, error))
        return PLANE_FAILURE
    except FileNotFoundError as error:
        _fail("missing input: {}".format(error))
        return USAGE_ERROR
    except json.JSONDecodeError as error:
        _fail("invalid JSON: {}".format(error))
        return USAGE_ERROR


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m continuum.shadow.cli",
        description="Record-only shadow validation against NanoDictate.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="decide one lifecycle event and journal it")
    run.add_argument("--event", required=True, help="the event payload the bridge forwarded")
    run.add_argument("--state", help="a captured state document; omit with --capture-live")
    run.add_argument("--capture-live", action="store_true", help="read state from GitHub")
    run.add_argument("--repo", help="owner/name, required with --capture-live")
    run.add_argument("--token-env", default="GITHUB_TOKEN", help="read-only token variable")
    run.add_argument("--trusted-actors", default="", help="AUTOMATION_TRUSTED_ACTORS value")
    run.add_argument("--config", help="configuration file; the repository's own is used otherwise")
    run.add_argument("--out", default="shadow-out", help="artifact directory")
    run.add_argument("--save-case", action="store_true", help="also write a replay case")
    run.add_argument("--budget-ms", type=int, default=liveness.DEFAULT_RUN_BUDGET_MS)
    run.add_argument("--now-ms", type=int, help="fix the clock; for reproducible runs only")
    run.add_argument("--no-barrier", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(handler=_run)

    compare = subparsers.add_parser("parity", help="compare a journal with an observed outcome")
    compare.add_argument("--journal", required=True)
    compare.add_argument("--observed", help="the NanoDictate outcome capture")
    compare.add_argument("--out", default="shadow-out")
    compare.add_argument("--report", action="store_true", help="emit an aggregate report")
    compare.set_defaults(handler=_parity)

    watch = subparsers.add_parser(
        "observe", help="read production for one event's correlation window, read-only"
    )
    watch.add_argument("--event", required=True, help="the event payload that opened the window")
    watch.add_argument("--repo", required=True, help="owner/name of the consumer repository")
    watch.add_argument(
        "--ledger", help="the ledger a previous observer pass wrote; omit to start one"
    )
    watch.add_argument(
        "--outcomes",
        nargs="*",
        default=[],
        help="outcome documents a previous observer pass wrote",
    )
    watch.add_argument("--token-env", default="GITHUB_TOKEN", help="read-only token variable")
    watch.add_argument("--out", default="shadow-out")
    watch.add_argument("--now-ms", type=int, help="fix the clock; for reproducible runs only")
    watch.add_argument(
        "--horizon-ms",
        type=int,
        default=observer.DEFAULT_HORIZON_MS,
        help="how long a correlation window stays open before its reads are the whole story",
    )
    watch.set_defaults(handler=_observe)

    pair = subparsers.add_parser(
        "reconcile", help="pair journals with the observer's independent record of them"
    )
    pair.add_argument("--journals", nargs="*", default=[], help="journals from the window")
    pair.add_argument("--ledger", required=True, help="the observer's ledger")
    pair.add_argument("--outcomes", nargs="*", default=[], help="outcome documents")
    pair.add_argument("--out", default="shadow-out")
    pair.add_argument(
        "--origins", help="correlation id -> origin mapping, as JSON, for the cutover gate"
    )
    pair.set_defaults(handler=_reconcile)

    live = subparsers.add_parser("liveness", help="account for a window's runs")
    live.add_argument("--acceptances", nargs="*", default=[], help="acceptance records")
    live.add_argument("--journals", nargs="*", default=[], help="journals from the window")
    live.add_argument("--out", default="shadow-out")
    live.add_argument("--now-ms", type=int, required=True, help="the clock, as an input")
    live.add_argument("--budget-ms", type=int, default=liveness.DEFAULT_RUN_BUDGET_MS)
    live.add_argument(
        "--orphan-grace-ms", type=int, default=liveness.DEFAULT_ORPHAN_GRACE_MS
    )
    live.set_defaults(handler=_liveness)

    again = subparsers.add_parser("replay", help="re-run a captured case")
    again.add_argument("--case", required=True)
    again.add_argument("--compare", help="a replay document from another Continuum")
    again.add_argument("--config")
    again.add_argument("--out", default="shadow-out")
    again.add_argument("--now-ms", type=int)
    again.set_defaults(handler=_replay)

    gate = subparsers.add_parser("cutover", help="judge a window against #28's requirements")
    gate.add_argument("--parity", nargs="*", default=[], help="parity result documents")
    gate.add_argument("--liveness", help="a liveness report document")
    gate.add_argument("--origins", help="correlation id -> origin mapping, as JSON")
    gate.add_argument("--resolutions", nargs="*", default=[])
    gate.add_argument("--approval", help="an approval document")
    gate.add_argument("--canary", help="canary evidence, as JSON")
    gate.add_argument("--rollback", help="rollback evidence, as JSON")
    gate.add_argument(
        "--baseline",
        help="a rolling baseline report from the `baseline` subcommand; the cutover "
        "gate refuses a window without one",
    )
    gate.add_argument("--window-start", required=True)
    gate.add_argument("--window-end", required=True)
    gate.add_argument("--out", default="shadow-out")
    gate.set_defaults(handler=_cutover)

    head = subparsers.add_parser(
        "baseline", help="read the consumer's live workflows and compare them with the ledger"
    )
    head.add_argument("--ledger", required=True, help="the approved parity ledger")
    head.add_argument(
        "--capture-live", action="store_true", help="read the live head from GitHub"
    )
    head.add_argument(
        "--live-head", help="a recorded live-head reading; the alternative to --capture-live"
    )
    head.add_argument("--repo", help="owner/name, required with --capture-live")
    head.add_argument("--token-env", default="GITHUB_TOKEN", help="read-only token variable")
    head.add_argument(
        "--render", action="store_true", help="also print the ledger as markdown"
    )
    head.add_argument("--out", default="shadow-out")
    head.set_defaults(handler=_baseline)

    barrier_command = subparsers.add_parser(
        "barrier-check", help="prove the write barrier refuses every mutating adapter"
    )
    barrier_command.add_argument("--state", help="a captured state to attempt writes against")
    barrier_command.add_argument("--out", default="shadow-out")
    barrier_command.set_defaults(handler=_barrier_check)

    describe = subparsers.add_parser("describe", help="print the engine, barrier, and scenarios")
    describe.set_defaults(handler=_describe)

    summary = subparsers.add_parser(
        "summary", help="render recorded evidence as a markdown job summary"
    )
    summary.add_argument("--out", default="shadow-out")
    summary.add_argument("--artifact", default="", help="the artifact the evidence is in")
    summary.add_argument(
        "--window",
        action="store_true",
        help="summarise a judged window rather than a single run",
    )
    summary.add_argument(
        "--observer",
        action="store_true",
        help="summarise what the observer read rather than what a run decided",
    )
    summary.set_defaults(handler=_render_summary)

    return parser


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def _run(args: argparse.Namespace) -> int:
    global _BARRIER
    output = _output_dir(args.out)
    if _BARRIER is None:
        # Installed before the configuration is loaded and before the live
        # adapters could be imported, and always installed: the flag exists so a
        # developer can watch the failure mode, not so a run can go unwatched.
        _BARRIER = barrier.install([output])
    report = _BARRIER

    config = load_config(args.config)
    payload = _read_json(args.event)
    event = _event_from(payload)
    state, event = _state_for(args, event)
    now_ms = args.now_ms if args.now_ms is not None else int(time.time() * 1000)

    started = time.monotonic()
    journal = plan(event, state, config, now_ms=now_ms, barrier=report.describe() if report else {})
    elapsed = int((time.monotonic() - started) * 1000)
    if elapsed > args.budget_ms:
        journal = liveness.timeout_journal(
            journal, budget_ms=args.budget_ms, elapsed_ms=elapsed
        )
    else:
        journal = journal.with_duration(elapsed)

    path = _write(
        output / "journals" / _name(journal.event.correlation_id), journal.describe()
    )
    if args.save_case:
        case = replay.build_case(
            journal,
            run_url=str(payload.get("_run_url", "")),
            delivery_id=str(payload.get("delivery", {}).get("id", "")) if isinstance(payload.get("delivery"), Mapping) else "",
            captured_at=str(payload.get("_captured_at", "")),
        )
        _write(output / "cases" / _name(case.correlation_id, "case.json"), case.describe())

    _summary(
        "shadow {} [{}]: {} {} ({} effect(s), {}ms)".format(
            event.correlation_id,
            journal.event.scenario,
            journal.status,
            journal.decision,
            len(journal.actions),
            elapsed,
        )
    )
    if report is not None and report.violations:
        # A blocked write is a plane failure: it means something reached past the
        # boundary, which is the one thing that must never be routine.
        _summary("write barrier recorded {} refused write(s)".format(len(report.violations)))
        return PLANE_FAILURE
    if journal.status in ("failed", "timeout", "blocked-write"):
        return VALIDATION_FAILED
    return 0


def _event_from(payload: Mapping[str, Any]) -> ShadowEvent:
    """Accept either a normalized event or a raw GitHub webhook payload.

    The bridge forwards the webhook as GitHub sent it, because that is the
    immutable record of what happened; normalizing is Continuum's job. A payload
    that is already a normalized event is also accepted, because the replay
    fixtures and the local fixture sweep are not GitHub.
    """

    if payload.get("schema") == "continuum.shadow-event/v1" or "event_type" in payload:
        return event_from_payload(payload)
    return from_github_event(
        payload,
        event_name=str(payload.get("__event_name", "")),
        delivery_id=str(payload.get("__delivery_id", "")),
        repository=str(payload.get("__repository", "")),
    )


def _state_for(
    args: argparse.Namespace, event: ShadowEvent
) -> Tuple[ObservedState, ShadowEvent]:
    """The state a run decides against, and the event as that state pins it.

    Both branches return a pair, because a capture can resolve something the
    event only refers to -- a release tag to a commit -- and the event that
    reaches the planner has to carry the resolved value.
    """

    if args.state:
        # A recorded capture is replayed as it was captured, pins and all: no
        # second resolution happens here, because a second resolution could only
        # disagree with the decision this state is evidence for, and evidence
        # that changes when it is read is not evidence. The commit the capture
        # resolved is part of the capture, so it is read from there rather than
        # looked up again.
        state = state_from_payload(_read_json(args.state))
        recorded = str(state.release_commit or "").lower()
        pinned = str(event.release.get("source_sha") or "").lower()
        if recorded and pinned and recorded != pinned:
            # Two pieces of recorded evidence disagree about which commit the
            # release names. There is no principled way to prefer one, and a
            # journal that silently decided about the other one would be evidence
            # of a decision the operator never ran.
            raise EventError(
                "release_pin_conflict",
                "the event names release commit {} but the recorded capture "
                "resolved {}; refusing to decide about either".format(
                    pinned, recorded
                ),
            )
        return state, event.with_release_source(recorded)
    if args.capture_live:
        from .capture import CaptureError, capture_state

        if not args.repo:
            raise EventError("missing_repository", "--capture-live needs --repo owner/name")
        from continuum.review.github import GitHubClient

        client = GitHubClient(
            repository=args.repo, token=_token(args.token_env)
        )
        state = capture_state(
            client,
            event,
            captured_at_ms=int(time.time() * 1000),
            trusted_actors=args.trusted_actors,
        )
        return state, event.with_release_source(state.release_commit)
    raise EventError(
        "no_state",
        "A shadow run needs state: pass --state <capture.json> or --capture-live.",
    )


def _token(variable: str) -> str:
    import os

    token = os.environ.get(variable, "")
    if not token:
        raise EventError("missing_token", "{} is not set".format(variable))
    return token


# --------------------------------------------------------------------------- #
# parity
# --------------------------------------------------------------------------- #


def _parity(args: argparse.Namespace) -> int:
    from .journal import journal_from_payload

    output = _output_dir(args.out)
    journal = journal_from_payload(_read_json(args.journal))
    observed = None
    if args.observed:
        observed = outcome_from_payload(_read_json(args.observed))
    result = parity.compare(journal, observed)
    _write(output / "parity" / _name(result.correlation_id), result.describe())
    _summary(parity.summarize(result))
    return VALIDATION_FAILED if result.divergent else 0


# --------------------------------------------------------------------------- #
# observe
# --------------------------------------------------------------------------- #


def _observe(args: argparse.Namespace) -> int:
    """Read production for one event's window and write the observer's artifacts.

    Not behind the write barrier, for the same reason ``baseline`` is not: the
    barrier blocks process spawning and file writes that the shadow *decision*
    path needs, and installing it here would claim a guarantee about the write
    barrier rather than about this command. What this command's read-only nature
    rests on is the call allowlist inside :mod:`continuum.shadow.observer` --
    every read is a named method on it, and the generic transports that take a
    verb are excluded.

    The ledger is written unconditionally, even when the window stays open. An
    observer that only wrote artifacts when it had a verdict would leave a long
    correlation with no record of its own progress, which is the state in which a
    reviewer cannot tell "still waiting" from "never ran".
    """

    from continuum.review.github import GitHubClient

    output = _output_dir(args.out)
    event = _event_from(_read_json(args.event))
    ledger = observer.ledger_from_payload(_read_json(args.ledger)) if args.ledger else observer.Ledger()
    now_ms = args.now_ms if args.now_ms is not None else int(time.time() * 1000)

    client = GitHubClient(repository=args.repo, token=_token(args.token_env))
    observation = observer.observe(
        client,
        repository=args.repo,
        event=event,
        ledger=ledger,
        now_ms=now_ms,
        horizon_ms=args.horizon_ms,
    )

    _write(output / "observer" / "ledger.json", observation.ledger.describe())
    _write(
        output / "observer" / "evidence" / _name(observation.window.window_id, "evidence.json"),
        {
            "schema": "continuum.shadow-observer-evidence/v1",
            "window_id": observation.window.window_id,
            "repository": observation.window.repository,
            "subject": observation.window.subject,
            "scenario": observation.window.scenario,
            "opened_at": observation.window.opened_at,
            "closed_at": observation.window.closed_at,
            "terminal": observation.window.terminal,
            "missing": list(observation.window.missing),
            "limits": list(observation.window.limits),
            "evidence": [item.describe() for item in observation.reading.evidence],
            "read_failures": [
                {"read": name, "reason": reason}
                for name, reason in observation.reading.failures
            ],
            "truncated_reads": list(observation.reading.truncated),
        },
    )
    if observation.outcome is not None:
        _write(
            output / "outcomes" / _name(observation.window.window_id),
            observation.outcome.describe(),
        )
    # Outcomes from earlier passes are carried forward rather than re-derived:
    # the observer cannot reconstruct a closed window's outcome from a later read
    # of production, because the evidence that closed it may have aged out of the
    # API's window. Copying them is what lets one artifact hold a whole
    # correlation window's worth of outcomes.
    for path in args.outcomes:
        document = _read_json(path)
        _write(
            output / "outcomes" / _name(str(document.get("correlation_id", "outcome"))),
            document,
        )
    _summary(observer.summarize(observation))
    return 0


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #


def _reconcile(args: argparse.Namespace) -> int:
    """Pair each journal with the observer's record, and compare.

    The pairing is what the observer deliberately does not do. Its artifacts are
    written by whatever workflow run happened to be looking at production at the
    time, and a journal is written by the run that made the decision; the two are
    frequently not the same run and neither waits for the other. Pairing them here
    means an outcome that arrived first is still compared, and a journal whose
    window has not closed yet is reported as not-yet-evaluable instead of as a
    divergence.
    """

    from .journal import journal_from_payload

    output = _output_dir(args.out)
    ledger = observer.ledger_from_payload(_read_json(args.ledger))
    outcomes = {}
    for path in args.outcomes:
        document = _read_json(path)
        outcomes[str(document.get("correlation_id", ""))] = document

    pairings: List[Dict[str, Any]] = []
    results = []
    pending = 0
    for path in args.journals:
        journal = journal_from_payload(_read_json(path))
        correlation = journal.event.correlation_id
        document = outcomes.get(correlation)
        observed = outcome_from_payload(document) if document is not None else None
        pairing = observer.reconcile(ledger, correlation, observed)
        pairings.append(pairing.describe())
        if observed is None:
            # Not compared at all. ``parity.compare`` would answer
            # ``unevaluable``, which the cutover gate rightly treats as a blocker,
            # but here it would mean something narrower: the observer has not
            # finished reading this window yet. Calling that a divergence would
            # report every long-running decision as a mismatch and drown the real
            # ones, so it is counted as outstanding work instead.
            pending += 1
            _summary("reconcile {}: not yet observable".format(correlation))
            continue
        result = parity.compare(journal, observed)
        _write(output / "parity" / _name(correlation), result.describe())
        results.append(result)
        _summary(
            "reconcile {}: {} ({})".format(correlation, result.classification, pairing.verdict)
        )

    _write(
        output / "observer" / "reconciliation.json",
        {
            "schema": "continuum.shadow-observer-reconciliation/v1",
            "repository": ledger.repository,
            "journals": len(args.journals),
            "outcomes": len(outcomes),
            "paired": len([item for item in pairings if item["outcome_emitted"]]),
            "not_yet_observable": pending,
            "windows_still_open": len(ledger.open_windows),
            "pairings": pairings,
        },
    )
    _summary(
        "reconcile: {} journal(s), {} paired, {} not yet observable, {} window(s) "
        "still open".format(
            len(args.journals),
            len([item for item in pairings if item["outcome_emitted"]]),
            pending,
            len(ledger.open_windows),
        )
    )
    if args.origins:
        _write(output / "observer" / "origins.json", _read_json(args.origins))
    return VALIDATION_FAILED if any(result.divergent for result in results) else 0


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #


def _liveness(args: argparse.Namespace) -> int:
    output = _output_dir(args.out)
    report = liveness.report_from_documents(
        [_read_json(path) for path in args.acceptances],
        [_read_json(path) for path in args.journals],
        now_ms=args.now_ms,
        budget_ms=args.budget_ms,
        orphan_grace_ms=args.orphan_grace_ms,
    )
    _write(output / "liveness" / _name(args.now_ms, "report.json"), report.describe())
    _summary(liveness.summary_line(report))
    return VALIDATION_FAILED if not report.is_clean else 0


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def _replay(args: argparse.Namespace) -> int:
    global _BARRIER
    output = _output_dir(args.out)
    if _BARRIER is None:
        _BARRIER = barrier.install([output])
    report = _BARRIER
    case = replay.read_case_file(Path(args.case))
    config = load_config(args.config) if args.config else None
    document = replay.replay_document(case, config, now_ms=args.now_ms)
    _write(output / "replays" / _name(case.correlation_id, "replay.json"), document)
    if report.violations:
        _summary("write barrier recorded {} refused write(s)".format(len(report.violations)))
        return PLANE_FAILURE
    if args.compare:
        other = _read_json(args.compare)
        comparison = replay.compare_replays(other, document)
        _write(
            output / "replays" / _name(case.correlation_id, "comparison.json"),
            comparison.describe(),
        )
        _summary(replay.summarize(comparison))
        return 0 if comparison.agrees else VALIDATION_FAILED
    _summary(
        "replay {}: {} ({})".format(
            document.get("correlation_id", ""),
            document.get("verdict", ""),
            document.get("reason", ""),
        )
    )
    return VALIDATION_FAILED if document.get("verdict") == replay.CRASHED else 0


# --------------------------------------------------------------------------- #
# cutover
# --------------------------------------------------------------------------- #


def _cutover(args: argparse.Namespace) -> int:
    output = _output_dir(args.out)
    decision = cutover.from_documents(
        [_read_json(path) for path in args.parity],
        _read_json(args.liveness) if args.liveness else None,
        baseline_document=_read_json(args.baseline) if args.baseline else None,
        origins=_optional_json(args.origins),
        resolution_documents=[_read_json(path) for path in args.resolutions],
        approval_document=_read_json(args.approval) if args.approval else None,
        canary=_optional_json(args.canary),
        rollback=_optional_json(args.rollback),
        window_started_at=args.window_start,
        window_ended_at=args.window_end,
    )
    _write(output / "cutover" / "decision.json", decision.describe())
    _summary(cutover.summarize(decision))
    return 0 if decision.approved else VALIDATION_FAILED


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #


def _baseline(args: argparse.Namespace) -> int:
    """Read the consumer's live workflows and compare them with the ledger.

    Deliberately not behind the write barrier: every read it performs is on
    ``READ_ONLY_METHODS``, and the capture runs through the same client the shadow
    plane uses. Installing the barrier would claim a guarantee this command does not
    need rather than one it has.
    """

    output = _output_dir(args.out)
    ledger = baseline.load_ledger(args.ledger)

    if args.live_head:
        live = baseline.read_live_head(_read_json(args.live_head))
    elif args.capture_live:
        from continuum.review.github import GitHubClient

        if not args.repo:
            _fail("missing_repository: --capture-live needs --repo owner/name")
            return USAGE_ERROR
        client = GitHubClient(token=_token(args.token_env), repository=args.repo)
        live = baseline.capture_live_head(client, repository=args.repo)
    else:
        _fail(
            "no_live_head: pass --capture-live --repo owner/name to read the "
            "consumer now, or --live-head <file> to audit a reading somebody else "
            "took"
        )
        return USAGE_ERROR

    document = live.describe()
    _write(output / "baseline" / "live-head.json", document)

    result = baseline.audit(ledger, live, generated_from=ledger.repository)
    _write(output / "baseline" / "report.json", result.describe())
    _summary(baseline.summarize(result))
    for owner in sorted(result.routing):
        _summary(
            "baseline: routed {} difference(s) to {}: {}".format(
                len(result.routing[owner]), owner, ", ".join(result.routing[owner])
            )
        )
    if args.render:
        print(baseline.render_markdown(ledger))
    return 0 if result.ready else VALIDATION_FAILED


# --------------------------------------------------------------------------- #
# barrier-check
# --------------------------------------------------------------------------- #


def _barrier_check(args: argparse.Namespace) -> int:
    """Attempt one write per mutating adapter, and prove every one is refused.

    The issue asks for a deliberate attempted write in an integration test. This
    is the same thing as a command, so the workflow can run it and the artifact
    can carry the result, rather than the guarantee living only in a test file.

    The attempt is a real one: every mutating method the shadow client exposes is
    called, and the run fails if any of them produced an effect that was not
    suppressed. Checking that the *names* exist would prove nothing at all.
    """

    from .effects import RecordingEffects, blocked_effect_kinds
    from .state import ShadowGitHubClient

    global _BARRIER
    output = _output_dir(args.out)
    if _BARRIER is None:
        _BARRIER = barrier.install([output])
    report = _BARRIER
    state = (
        state_from_payload(_read_json(args.state))
        if args.state
        else state_from_payload(_minimal_capture())
    )
    sinks = RecordingEffects()
    client = ShadowGitHubClient(state, sinks, scenario="barrier-check")
    document = barrier.attempt_every_write(client, sinks)
    document["blocked_effect_kinds"] = list(blocked_effect_kinds())
    document["violations"] = report.describe()["violations"]
    _write(output / "barrier" / "check.json", document)
    _summary(
        "barrier: {} method(s) probed, {} performed, {} blocked event(s)".format(
            len(document["methods"]), len(document["performed"]), len(document["violations"])
        )
    )
    unsafe = document["performed"] or document["violations"] or document["uncallable"]
    return PLANE_FAILURE if unsafe else 0


# --------------------------------------------------------------------------- #
# describe
# --------------------------------------------------------------------------- #


def _describe(args: argparse.Namespace) -> int:
    document = {
        "engine": engine.describe(),
        "scenarios": list(planned_scenarios()),
        "required_for_cutover": [
            {"scenario": name, "reason": reason}
            for name, reason in cutover.REQUIRED_SCENARIOS
        ],
        "read_only_methods": sorted(READ_ONLY_METHODS),
        "observer_read_methods": sorted(observer.OBSERVER_READ_METHODS),
        "observer_unobservable_keys": list(observer.UNOBSERVABLE_DETAIL_KEYS),
        "observer_unreconstructible_kinds": list(observer.UNRECONSTRUCTIBLE_KINDS),
        "observer_horizon_ms": observer.DEFAULT_HORIZON_MS,
        "verdicts": list(parity.CLASSIFICATIONS),
        "liveness_verdicts": [
            liveness.LIVE,
            liveness.SLOW,
            liveness.STALLED,
            liveness.ORPHANED,
            liveness.CRASHED,
            liveness.ABANDONED,
        ],
    }
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


def _render_summary(args: argparse.Namespace) -> int:
    """Print what one run decided, in the form a reviewer reads first.

    A window's evidence is a directory of JSON, which is complete and unreadable
    at the same time: the run that decided a pull request may merge carries a
    dozen files, and the one thing a reviewer needs -- what was decided, whether
    it matched production, and whether the run stayed live -- is spread across
    them. This renders that into the job summary, from the same documents the
    cutover judge reads, so what a reviewer sees cannot disagree with what the
    gate saw.

    It reports absence as absence. A run with no parity document says the parity
    was not evaluated rather than implying it matched, because "no divergence"
    and "no comparison" are the two states a window most needs to tell apart.
    """

    directory = Path(args.out)
    if getattr(args, "window", False):
        _summary(report.window_summary(directory, artifact=str(args.artifact or "")))
    elif getattr(args, "observer", False):
        _summary(report.observation_summary(directory, artifact=str(args.artifact or "")))
    else:
        _summary(report.run_summary(directory, artifact=str(args.artifact or "")))
    return 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _output_dir(value: str) -> Path:
    target = Path(value)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _read_json(path: str) -> Dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise json.JSONDecodeError("expected an object", "", 0)
    return document


def _optional_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    return _read_json(path) if path else None


def _write(path: Path, document: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _name(value: str, suffix: str = "json") -> str:
    """An artifact filename derived from a correlation id.

    Bounded, because a correlation id that is legitimately long (a duplicate
    replay carries the event it replays) would otherwise produce a filename the
    filesystem refuses, and a journal that cannot be written is a run with no
    evidence at all.
    """

    safe = "".join(
        char if (char.isalnum() or char in "-_.") else "-" for char in str(value)
    ).strip("-.")
    return "{}.{}".format((safe or "shadow")[:96], suffix.lstrip("."))


def _minimal_capture() -> Dict[str, Any]:
    """The smallest state a write attempt can be made against.

    Used by ``barrier-check`` when the caller has no capture to hand: what is
    being proved is that the adapters refuse, and that does not depend on the
    pull request having any history.
    """

    return {
        "schema": "continuum.shadow-snapshot/v1",
        "repository": "acme/widgets",
        "repository_owner": "acme",
        "base_ref": "main",
        "pulls": [
            {
                "number": 1,
                "state": "open",
                "head": {"ref": "feature", "sha": "a" * 40, "repository": "acme/widgets"},
                "base": {"ref": "main", "sha": "b" * 40, "repository": "acme/widgets"},
            }
        ],
        "issues": [{"number": 1, "state": "open", "author": "octocat"}],
    }


def _summary(message: str) -> None:
    print(message, flush=True)


def _fail(message: str) -> None:
    print("shadow: {}".format(message), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
