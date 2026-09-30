"""The shadow command line: the one place CI, the bridge, and a person all use.

Five subcommands, one per artifact:

* ``run``      -- turn one real lifecycle event into a journal (and optionally a
  replay case). The command the bridge workflow calls.
* ``parity``   -- compare a journal with a captured NanoDictate outcome.
* ``liveness`` -- account for every accepted event in a window.
* ``replay``   -- re-run a captured case, optionally against a second engine.
* ``baseline`` -- read the consumer's live head and compare it with the ledger.
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
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import barrier, baseline, cutover, engine, liveness, parity, replay, report
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
    gate.add_argument("--baseline", help="a live-head baseline report document")
    gate.add_argument("--window-start", required=True)
    gate.add_argument("--window-end", required=True)
    gate.add_argument("--out", default="shadow-out")
    gate.set_defaults(handler=_cutover)

    read = subparsers.add_parser(
        "baseline", help="compare the consumer's live head with the parity ledger"
    )
    read.add_argument("--ledger", default="docs/parity-ledger.json")
    read.add_argument("--repo", help="owner/name; omit with --live-head")
    read.add_argument("--live-head", help="a captured live head, as JSON")
    read.add_argument("--token-env", default="GITHUB_TOKEN", help="read-only token variable")
    read.add_argument(
        "--write-ledger",
        action="store_true",
        help="also refresh docs/parity-ledger.json and its rendered markdown",
    )
    read.add_argument(
        "--accept-removed",
        nargs="*",
        default=[],
        metavar="PATH",
        help="audited workflows confirmed gone; named so the withdrawal is a stated act",
    )
    read.add_argument(
        "--accept-closed",
        nargs="*",
        default=[],
        type=int,
        metavar="NUMBER",
        help="classified pull requests confirmed closed, without number sign",
    )
    read.add_argument("--out", default="shadow-out")
    read.set_defaults(handler=_baseline)

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
        origins=_optional_json(args.origins),
        resolution_documents=[_read_json(path) for path in args.resolutions],
        approval_document=_read_json(args.approval) if args.approval else None,
        canary=_optional_json(args.canary),
        rollback=_optional_json(args.rollback),
        baseline_document=_read_json(args.baseline) if args.baseline else None,
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
    # The barrier is deliberately not installed here. Every read this command can
    # perform is a read, and installing the barrier would only assert something
    # about the adapters that ``barrier-check`` proves properly.
    from continuum.review.github import GitHubClient

    output = _output_dir(args.out)
    ledger = baseline.load_ledger(args.ledger)

    if args.live_head:
        head = baseline.read_live_head(_read_json(args.live_head))
    elif args.repo:
        client = GitHubClient(repository=args.repo, token=_token(args.token_env))
        head = baseline.capture_live_head(client, repository=args.repo)
    else:
        raise baseline.BaselineError(
            "missing_live_head",
            "A baseline needs something to compare against: pass --repo owner/name to "
            "read the live head, or --live-head <capture.json> to reuse one.",
        )

    # The reading itself is written before the comparison, so an artifact that says
    # "drifted" can be re-audited by hand from the same bytes rather than by taking
    # the live head again and hoping it has not moved in between.
    _write(output / "baseline" / "live-head.json", head.describe())

    if args.write_ledger:
        # Only a complete reading may rewrite the ledger. Writing it from a
        # partial read would record a HEAD and a file list that were never fully
        # observed, and the next audit would then treat those absences as real
        # findings against a repository it did not look at.
        if not head.complete:
            raise baseline.BaselineError(
                "incomplete_live_head",
                "refusing to write the ledger from an incomplete reading: {}".format(
                    "; ".join(head.limits) or "the reading did not say why"
                ),
            )
        refreshed = ledger.at(
            head,
            audited_at=_today(),
            accept_removed=args.accept_removed,
            accept_closed=args.accept_closed,
        )
        ledger_path = Path(args.ledger)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(baseline.dump_ledger(refreshed), encoding="utf-8")
        markdown_path = ledger_path.with_suffix(".md")
        markdown_path.write_text(
            baseline.render_markdown(refreshed), encoding="utf-8"
        )

    report = baseline.audit(ledger, head)
    _write(output / "baseline" / "report.json", report.describe())
    _summary(baseline.summarize(report))
    return 0 if report.ready else VALIDATION_FAILED


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


def _today() -> str:
    """The audit date, as a date.

    Recorded on the ledger rather than a timestamp so re-pinning the same head on
    a second attempt produces the same document: the audit is of a repository
    state, not of a run, and a diff of two ledgers should show what was re-audited
    and nothing else.
    """

    import datetime

    return datetime.date.today().isoformat()


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
