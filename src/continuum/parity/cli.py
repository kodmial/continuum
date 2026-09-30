"""The parity command line: five subcommands, one per question.

* ``ledger-check`` -- is the committed ledger a trustworthy audit record?
* ``fetch``        -- what is true of every tracked source right now?
* ``scan``         -- is any of that drift, and at what priority?
* ``issue``        -- what single issue, if any, should exist per drifting source?
* ``promote``      -- may a consumer pin move, and can the run say why?

Two rules run through all five:

* **A finding is a result, not a failure.** The upstream repository moving is the
  normal state of a live repository, so a scan that finds drift has done its job
  and exits zero. Only the plane failing -- an unreadable ledger, an unwritable
  artifact, a verdict that cannot be evaluated -- is an error. The one command
  that is *supposed* to refuse is ``promote``, and it refuses by answering, not
  by crashing.

* **Every artifact passes the public allowlist on the way out.** The documents
  written here are uploaded from a public repository's run and printed into job
  summaries, so ``assert_public`` runs on each one before it is written rather
  than being a review step somebody might forget.

The commands are separate because the phases have different trust properties.
``fetch`` is the only one that needs a credential and the only one that touches
the network; ``scan`` from a recorded observations file is a pure function of two
files, which is what makes a reported result reproducible after the fact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import drift as drift_module
from . import issue as issue_module
from . import ledger as ledger_module
from . import promote as promote_module
from . import sources as sources_module
from .ledger import Ledger, LedgerError
from .sources import GitHubSourceReader, SourceObservation, observations_document

USAGE_ERROR = 2
#: The plane could not do its job: an input it could not read, an artifact it
#: could not write. Never used to report drift.
PLANE_FAILURE = 3
#: The plane worked and the answer is no.
REFUSED = 1

DEFAULT_LEDGER = "reference/parity-ledger.json"
DEFAULT_OUTPUT_DIR = "parity-out"
DEFAULT_PUBLIC_TOKEN_ENV = "GITHUB_TOKEN"
DEFAULT_PRIVATE_TOKEN_ENV = "CONTINUUM_PARITY_PRIVATE_TOKEN"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except (LedgerError, sources_module.RedactionError, issue_module.IssuePlanError) as error:
        _fail("{}".format(error))
        return PLANE_FAILURE
    except promote_module.PromotionError as error:
        _fail(str(error))
        return PLANE_FAILURE
    except FileNotFoundError as error:
        _fail("missing input: {}".format(error))
        return USAGE_ERROR
    except json.JSONDecodeError as error:
        _fail("invalid JSON: {}".format(error))
        return USAGE_ERROR
    except OSError as error:
        _fail("{}".format(error))
        return PLANE_FAILURE


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m continuum.parity.cli",
        description="Cross-repository parity: the provenance ledger and its drift checker.",
    )
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser(
        "ledger-check",
        help="validate the committed ledger; no network",
        description=(
            "Read the ledger and report what would make it an untrustworthy audit "
            "record. Exits non-zero on an error-level finding, because an error "
            "means the file is not the record it claims to be. A warning-level "
            "finding -- a classification whose port is still in flight -- is "
            "reported and does not fail the check."
        ),
    )
    _add_ledger_arguments(check)
    check.add_argument("--out", help="write the ledger check document here")
    check.set_defaults(handler=_ledger_check)

    fetch = subparsers.add_parser(
        "fetch",
        help="read every tracked source and record the result",
        description=(
            "Read each tracked source once. A source that cannot be read becomes an "
            "'unavailable' observation with a reason; it does not stop the others, "
            "and nothing downstream reports it as checked. Credentials are read from "
            "the environment by variable name and never taken as arguments, so they "
            "cannot appear in a process listing or a workflow log."
        ),
    )
    _add_ledger_arguments(fetch)
    _add_read_arguments(fetch)
    fetch.add_argument("--out", help="write the full observations document here")
    fetch.add_argument(
        "--public-out",
        help="write the publishable observations document here (private sources redacted)",
    )
    fetch.set_defaults(handler=_fetch)

    scan = subparsers.add_parser(
        "scan",
        help="classify an advance against the ledger",
        description=(
            "Compare what each source reported against the audited commit the ledger "
            "recorded, and classify every changed path. With --observations this is a "
            "pure function of two files; without it the sources are read first."
        ),
    )
    _add_ledger_arguments(scan)
    _add_read_arguments(scan)
    scan.add_argument(
        "--observations",
        help="read recorded observations instead of reading the sources again",
    )
    scan.add_argument("--fetch", action="store_true", help="read the sources before scanning")
    scan.add_argument("--out", help="write the drift report document here")
    scan.add_argument("--markdown", help="write the reviewer-facing markdown here")
    scan.add_argument(
        "--public-observations-out",
        help="write the publishable observations document here when reading live",
    )
    scan.add_argument(
        "--fail-on-drift",
        action="store_true",
        help=(
            "exit non-zero unless the tracked set is clean and fully read: the same "
            "question the promotion gate asks, so a scheduled audit cannot pass "
            "having read nothing"
        ),
    )
    scan.set_defaults(handler=_scan)

    issue = subparsers.add_parser(
        "issue",
        help="plan the single parity issue per drifting source",
        description=(
            "Compute, from the drift report and the open issues, what to create, what "
            "to update, and what to leave alone. The plan is printed so a shell can "
            "apply it; a source whose finding has not changed produces no write at all."
        ),
    )
    issue.add_argument("--report", required=True, help="a drift report document")
    issue.add_argument(
        "--existing",
        help="JSON of the open issues, as `gh issue list --json number,title,body` produces",
    )
    issue.add_argument("--out", help="write the issue plan document here")
    issue.set_defaults(handler=_issue)

    apply = subparsers.add_parser(
        "apply",
        help="write the planned issues through gh",
        description=(
            "Apply an issue plan. Every command is an argument list, never a shell "
            "string, so a body is one argument regardless of what is in it. The "
            "decisions were already made by `issue`; nothing here forms an opinion. "
            "With --dry-run the commands are returned and nothing is written."
        ),
    )
    apply.add_argument("--plan", required=True, help="an issue plan document")
    apply.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name the issues are written to",
    )
    apply.add_argument(
        "--dry-run",
        action="store_true",
        help="return the commands instead of running them",
    )
    apply.add_argument("--out", help="write the applied plan document here")
    apply.set_defaults(handler=_apply)

    promote = subparsers.add_parser(
        "promote",
        help="judge a consumer pin promotion against a drift report",
        description=(
            "Decide whether a consumer pin may move. Approved means the run may assert "
            "'{}' over the whole tracked set: no unclassified P0 drift, and no source "
            "left unread. Advisory P1 findings are reported and do not block. The "
            "verdict document carries the exact claim string so a caller cannot quote "
            "a paraphrase.".format(promote_module.NO_UNCLASSIFIED_P0_CLAIM)
        ),
    )
    promote.add_argument("--report", required=True, help="a drift report document")
    promote.add_argument("--consumer", required=True, help="the consumer whose pin is moving")
    promote.add_argument("--from-pin", default="", help="the pinned commit being left")
    promote.add_argument("--to-pin", default="", help="the commit being pinned")
    promote.add_argument(
        "--fail-on-refused",
        action="store_true",
        help="exit non-zero when the promotion is refused",
    )
    promote.add_argument("--out", help="write the verdict document here")
    promote.set_defaults(handler=_promote)

    return parser


def _add_ledger_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument(
        "--ledger",
        default=os.environ.get("CONTINUUM_PARITY_LEDGER", DEFAULT_LEDGER),
        help="the committed provenance ledger",
    )
    target.add_argument(
        "--overlay",
        default=os.environ.get("CONTINUUM_PARITY_LEDGER_OVERLAY", ""),
        help="an uncommitted overlay naming private sources (none by default)",
    )


def _add_read_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument(
        "--public-token-env",
        default=DEFAULT_PUBLIC_TOKEN_ENV,
        help="environment variable holding the read-only token for public sources",
    )
    target.add_argument(
        "--private-token-env",
        default=DEFAULT_PRIVATE_TOKEN_ENV,
        help="environment variable holding the least-privilege token for private sources",
    )
    target.add_argument(
        "--api-base",
        default=os.environ.get("CONTINUUM_GITHUB_API_BASE", sources_module.DEFAULT_API_BASE),
        help="GitHub API base URL",
    )
    target.add_argument(
        "--max-files",
        type=int,
        default=300,
        help="compare files kept per source; more than this is reported as truncated drift",
    )


# --------------------------------------------------------------------------- #
# ledger-check
# --------------------------------------------------------------------------- #


def _ledger_check(args: argparse.Namespace) -> int:
    ledger = _load(args.ledger, args.overlay)
    found = ledger.problems()
    document = sources_module.assert_public(
        {
            "schema": "continuum.parity-ledger-check/v1",
            "kind": "ledger-check",
            "version": 1,
            "generated_at": _now(),
            "ledger_source": ledger.source_path,
            "ledger_version": ledger.version,
            "ledger_generated_at": ledger.generated_at,
            "overlay_ids": list(ledger.overlay_ids),
            "totals": {
                "sources": len(ledger.sources),
                "classified_sources": len(
                    [source for source in ledger.sources if source.paths]
                ),
                "in_sync_sources": 0,
                "unavailable_sources": 0,
            },
            "summary": "ledger {}: {} source(s), {} finding(s)".format(
                ledger.version,
                len(ledger.sources),
                len(found),
            ),
            "findings": [
                {"status": item.severity, "classification": item.code, "detail": item.message}
                for item in found
            ],
            "unanswered_incidents": [
                "{}:{}".format(source_id, incident_id)
                for source_id, incident_id in drift_module.unanswered_incidents(ledger)
            ],
        },
        where="ledger check",
    )
    for item in found:
        _note(
            "::{}::{}: {}".format(
                "error" if item.severity == ledger_module.ERROR else "warning",
                item.code,
                item.message,
            )
        )
    _write_document(args.out, document)
    _print_json(document)
    _append_summary(
        "### Parity ledger check\n\n{}\n\n{} source(s), {} finding(s).\n".format(
            document["summary"],
            "Errors mean the ledger is not the audit record it claims to be."
            if ledger.errors()
            else "No error-level finding: the ledger is a usable audit record.",
            len(ledger.sources),
            len(found),
        )
    )
    _write_output(
        {
            "sources": str(len(ledger.sources)),
            "errors": str(len(ledger.errors())),
            "warnings": str(len(ledger.warnings())),
        }
    )
    return REFUSED if ledger.errors() else 0


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #


def _fetch(args: argparse.Namespace) -> int:
    ledger = _load(args.ledger, args.overlay)
    reader = _reader(args)
    observations = reader.read_all(ledger)
    document = observations_document(
        observations,
        generated_at=_now(),
        continuum_sha=_continuum_sha(),
        ledger=ledger,
    )
    public = _public_observations(observations, ledger=ledger)
    _write_document(args.out, document)
    if args.public_out:
        _write_document(args.public_out, public)
    readable = [item for item in observations if item.available]
    _print_json(public)
    _summary(
        "read {}/{} tracked source(s): {} available, {} unavailable".format(
            len(readable), len(observations), len(readable), len(observations) - len(readable)
        )
    )
    for item in observations:
        if not item.available:
            _note(
                "::warning::{} was not read ({}): {}".format(
                    item.id, item.code or "no reason recorded", item.reason
                )
            )
    _append_summary(
        "### Parity source read\n\n{} source(s) tracked, {} read, {} unavailable.\n".format(
            len(observations), len(readable), len(observations) - len(readable)
        )
    )
    _write_output(
        {
            "readable": str(len(readable)),
            "unavailable": str(len(observations) - len(readable)),
        }
    )
    return 0


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #


def _scan(args: argparse.Namespace) -> int:
    ledger = _load(args.ledger, args.overlay)
    observations, live = _observations(args, ledger)
    report = drift_module.compute(
        ledger,
        observations,
        generated_at=_now(),
        continuum_sha=_continuum_sha(),
    )
    document = report.describe()
    _write_document(args.out, document)
    if args.markdown:
        _write_text(args.markdown, drift_module.render_markdown(report))
    if args.public_observations_out and live:
        _write_document(
            args.public_observations_out, _public_observations(observations, ledger=ledger)
        )
    _print_json(document)
    _summary(drift_module.summary_line(report))
    for source in report.findings:
        _note("::warning::{}: {}".format(source.id, source.summary))
    for source in report.unavailable:
        _note(
            "::warning::{} was not read, so it is unaccounted for rather than clean: "
            "{}".format(source.id, source.code or source.reason)
        )
    _append_summary(drift_module.render_markdown(report))
    unanswered = drift_module.unanswered_incidents(ledger)
    if unanswered:
        _summary(
            "{} classification(s) have no Continuum reference yet: {}".format(
                len(unanswered), ", ".join("{}:{}".format(item[0], item[1]) for item in unanswered)
            )
        )
    # Prefixed because `ledger-check` writes `unclassified_p0` for a different
    # thing -- its own findings. Two writers sharing one key in the same job would
    # make the last one to run silently win.
    _write_output(
        {
            "drift_sources": str(len(report.sources)),
            "drift_p0": str(report.unclassified_p0),
            "drift_p1": str(report.unclassified_p1),
            "drift_unavailable": str(len(report.unavailable)),
            "drift_clean": str(report.clean).lower(),
            "drift_can_claim_clean": str(report.can_claim_clean).lower(),
        }
    )
    if args.fail_on_drift and not report.can_claim_clean:
        # `--fail-on-drift` asks the same question the promotion gate does: can the
        # run answer "no unclassified P0 drift" over the *whole* tracked set? An
        # unreadable source makes that answer holey, so it fails here too. A
        # scheduled audit that read nothing and exited green would be the one
        # failure mode this plane exists to prevent, and it would look like a pass.
        if report.unclassified_p0:
            _fail(
                "unclassified P0 drift in {} source(s)".format(report.unclassified_p0)
            )
        for source in report.unavailable:
            _fail(
                "{} was not read ({}), so the tracked set is unaccounted for: {}".format(
                    source.id, source.code or "no reason recorded", source.reason
                )
            )
        return REFUSED
    return 0


def _observations(
    args: argparse.Namespace, ledger: Ledger
) -> Tuple[Tuple[SourceObservation, ...], bool]:
    """Where the observations come from, and whether they were read live.

    A recorded file is the default when one is named, because reproducing a
    reported result from two files is worth more than freshness, and the live
    read is the explicit alternative rather than the fallback.
    """

    if args.observations:
        return sources_module.observations_from_document(_read_json(args.observations)), False
    return _reader(args).read_all(ledger), True


# --------------------------------------------------------------------------- #
# issue
# --------------------------------------------------------------------------- #


def _issue(args: argparse.Namespace) -> int:
    report = promote_module.report_from_document(_read_json(args.report))
    existing = issue_module.existing_from_documents(
        _read_json(args.existing) if args.existing else ()
    )
    document = issue_module.plan_document(report, existing)
    _write_document(args.out, document)
    _print_json(document)
    totals = document["totals"]
    _summary(
        "issue plan: {} to create, {} to update, {} to reopen, {} to close, {} unchanged".format(
            totals["create"],
            totals["update"],
            totals["reopen"],
            totals["close"],
            totals["skip"],
        )
    )
    _append_summary(
        "### Parity issue plan\n\n"
        "{} to create, {} to update, {} to reopen, {} to close, {} unchanged.\n".format(
            totals["create"],
            totals["update"],
            totals["reopen"],
            totals["close"],
            totals["skip"],
        )
    )
    _write_output(
        {
            "create": str(totals["create"]),
            "update": str(totals["update"]),
            "reopen": str(totals["reopen"]),
            "close": str(totals["close"]),
            "skip": str(totals["skip"]),
        }
    )
    return 0


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


def _apply(args: argparse.Namespace) -> int:
    document = issue_module.apply_plan(
        _read_json(args.plan),
        args.repository,
        dry_run=bool(args.dry_run),
    )
    _write_document(args.out, document)
    _print_json(document)
    totals = document["totals"]
    written = len(document["commands"])
    verb = "would run" if args.dry_run else "ran"
    _summary(
        "issue plan applied: {} command(s) {} ({}, {}, {}, {}, {})".format(
            written,
            verb,
            totals["create"],
            totals["update"],
            totals["reopen"],
            totals["close"],
            totals["skip"],
        )
    )
    _append_summary(
        "### Parity issue writes\n\n{} command(s) {}: {} created, {} updated, "
        "{} reopened, {} closed.\n".format(
            written,
            verb,
            totals["create"],
            totals["update"],
            totals["reopen"],
            totals["close"],
        )
    )
    _write_output({"commands": str(written)})
    return 0


# --------------------------------------------------------------------------- #
# promote
# --------------------------------------------------------------------------- #


def _promote(args: argparse.Namespace) -> int:
    report = promote_module.report_from_document(_read_json(args.report))
    verdict = promote_module.promote(
        report,
        consumer=args.consumer,
        from_pin=args.from_pin,
        to_pin=args.to_pin,
    )
    document = verdict.describe()
    _write_document(args.out, document)
    _print_json(document)
    _summary(promote_module.summary_line(verdict))
    if verdict.approved:
        _summary("claim: {}".format(verdict.claim))
    for blocker in verdict.blockers:
        _note("::error::{}".format(blocker))
    for advisory in verdict.advisories:
        _note("::warning::{}".format(advisory))
    _append_summary(
        "### Parity promotion gate\n\n{}\n\n{}".format(
            promote_module.summary_line(verdict),
            "\n".join("- {}".format(blocker) for blocker in verdict.blockers)
            or "No blocking finding.",
        )
    )
    _write_output(
        {
            "approved": str(verdict.approved).lower(),
            "claim": verdict.claim,
        }
    )
    if args.fail_on_refused and not verdict.approved:
        return REFUSED
    return 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load(ledger_path: str, overlay: str) -> Ledger:
    return ledger_module.load(ledger_path, overlay=overlay or None)


def _reader(args: argparse.Namespace) -> GitHubSourceReader:
    # The variable *names* are arguments; the values are read here and never
    # echoed. A token on the command line would be in the process list and in
    # whatever the caller logs.
    return GitHubSourceReader(
        api_base=args.api_base,
        public_token=os.environ.get(args.public_token_env, ""),
        private_token=os.environ.get(args.private_token_env, ""),
        max_files=args.max_files,
    )


def _public_observations(
    observations: Sequence[SourceObservation], *, ledger: Optional[Ledger] = None
) -> Dict[str, Any]:
    return observations_document(
        observations,
        generated_at=_now(),
        continuum_sha=_continuum_sha(),
        ledger=ledger,
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _continuum_sha() -> str:
    return os.environ.get("GITHUB_SHA", "").strip()


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: str, document: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_document(path: Optional[str], document: Any) -> None:
    if path:
        _write_json(path, document)


def _write_text(path: str, text: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def _print_json(document: Any) -> None:
    """The one document on stdout.

    Every command prints exactly one JSON document, so stdout stays pipeable into
    a reviewer. Everything human -- the summary line, the workflow annotations, an
    error -- goes to stderr, where a runner still logs it. Mixing annotations into
    a machine-readable stream would mean every caller had to strip them.
    """

    print(json.dumps(document, indent=2, sort_keys=True), flush=True)


def _summary(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _note(message: str) -> None:
    """A GitHub Actions annotation. Read from stderr, so it annotates and still
    leaves stdout parseable."""

    print(message, file=sys.stderr, flush=True)


def _fail(message: str) -> None:
    print("CONTINUUM_ERROR: {}".format(message), file=sys.stderr, flush=True)


def _append_summary(text: str) -> None:
    """Append to the job summary when there is one.

    Resolved per call, never bound as a default: the workflow sets the variable
    after this module is imported, and a summary that was never written is the
    one output nobody reads.
    """

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n\n")


def _write_output(values: Dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            for line in str(value).splitlines() or [""]:
                handle.write("{}={}\n".format(key, line))


if __name__ == "__main__":
    raise SystemExit(main())