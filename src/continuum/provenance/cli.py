"""The provenance command line: the weekly drift check, and the registration read
that precedes it.

Three subcommands:

* ``check``   -- read every source in the ledger, write a drift report, and decide
  what the parity issue should be.
* ``observe`` -- read one repository's head and workflow inventory, so registering a
  source starts from facts instead of a guess.
* ``render``  -- regenerate the ledger's Markdown from the JSON, so the two cannot
  disagree.

The rules that shape the surface:

* **The exit code is the verdict, and it is not a failure.** ``0`` is a clean audit,
  ``1`` means drift or an audit that could not complete, ``2`` is a usage error, and
  ``3`` means the plane could not do its job at all. A drifted source is the checker
  working.

* **A token never reaches an argument, a log line, or an artifact.** Credentials are
  named by *environment variable*, read here, and handed straight to the client. The
  report records which kind of credential was used and never the credential.

* **A private source is never read without its own credential.** When the named
  variable is not set, the source is reported ``unavailable`` and the run continues:
  a repository Continuum cannot see must not silence the repositories it can.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import drift, ledger as ledger_module
from .ledger import ProvenanceError, ProvenanceLedger, load_provenance, render_markdown

USAGE_ERROR = 2
#: The plane could not read its inputs or run its reads. Distinct from a verdict.
PLANE_FAILURE = 3
#: Drift, or an audit that could not complete. The checker succeeded.
DRIFT = 1

DEFAULT_LEDGER = "docs/provenance-ledger.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(path: Path, document: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _summary(message: str) -> None:
    print(message, flush=True)


def _fail(message: str) -> None:
    print("provenance: {}".format(message), file=sys.stderr, flush=True)


def _client(repository: str, token: str, api_base: str = "") -> Any:
    from ..review.github import GitHubClient, GitHubError

    kwargs: Dict[str, Any] = {}
    if api_base:
        kwargs["api_base"] = api_base
    try:
        return GitHubClient(token, repository, **kwargs)
    except GitHubError as error:
        raise ProvenanceError("client_not_usable", str(error)) from None


def _credential(source: Any, environ: Mapping[str, str], public_token_env: str) -> Optional[str]:
    """The credential a source is read with, or ``None`` when there must not be one.

    A private source gets its own least-privilege variable, and gets nothing at all
    when that variable is unset -- there is no fallback to the ambient token, because
    a fallback is how a private source ends up read with a credential that has more
    access than the read needs.
    """

    if not source.is_private:
        return environ.get(public_token_env, "")
    return environ.get(source.credential_env) or None


def collect(
    ledger: ProvenanceLedger,
    *,
    public_token_env: str = "GITHUB_TOKEN",
    environ: Optional[Mapping[str, str]] = None,
    pull_request_limit: int = drift.DEFAULT_PR_LIMIT,
    api_base: str = "",
    audited_at: str = "",
) -> List[drift.SourceReading]:
    """Take one reading per source, never letting one failure hide the others."""

    env = os.environ if environ is None else environ
    stamp = audited_at or _now()
    readings: List[drift.SourceReading] = []
    for source in ledger.sources:
        token = _credential(source, env, public_token_env)
        if not token:
            readings.append(
                drift.SourceReading(
                    id=source.id,
                    repository=source.repository,
                    visibility=source.visibility,
                    severity=source.severity,
                    status=drift.UNAVAILABLE,
                    baseline_sha=source.baseline_sha,
                    # The name of the variable, which is not a secret, and no
                    # repository name: a public ledger must not name a private one.
                    # The two limits differ because the two situations do: a private
                    # source refused for want of *its* credential is an access
                    # decision, and a public source that was never given a reader is
                    # a missing configuration. Reporting the first for the second
                    # would send a reader looking for a secret that does not exist.
                    limits=(
                        "no-credential-configured-for-private-source"
                        if source.is_private
                        else "no-public-read-credential-configured"
                    ),
                    credential="none",
                    ledger_digest=source.digest,
                    audited_at=stamp,
                )
            )
            continue
        try:
            client = _client(source.repository, token, api_base=api_base)
        except ProvenanceError as error:
            readings.append(
                drift.SourceReading(
                    id=source.id,
                    repository=source.repository,
                    visibility=source.visibility,
                    severity=source.severity,
                    status=drift.UNREADABLE,
                    baseline_sha=source.baseline_sha,
                    limits=(error.code,),
                    credential="none",
                    ledger_digest=source.digest,
                    audited_at=stamp,
                )
            )
            continue
        readings.append(
            drift.read_source(
                client,
                source,
                # Named so the reader can refuse a private source it was handed no
                # credential for, rather than trusting the caller to have picked the
                # right one. The value is never recorded; only its presence is.
                private_token=token if source.is_private else "",
                audited_at=stamp,
                pull_request_limit=pull_request_limit,
            )
        )
    return readings


def _report_document(
    report: drift.DriftReport,
    *,
    existing_number: int,
    existing_body: str,
) -> Dict[str, Any]:
    document = report.describe()
    document["issue"] = drift.issue_plan(
        report, existing_number=existing_number, existing_body=existing_body
    )
    return document


def _summary_line(report: drift.DriftReport, plan: Mapping[str, Any]) -> str:
    parts = [
        "provenance: {} ({} of {} sources advanced)".format(
            report.verdict, len(report.advanced), len(report.readings)
        )
    ]
    if report.unclassified_p0:
        parts.append(
            "{} p0 source{} drifted unclassified".format(
                len(report.unclassified_p0), "s" if len(report.unclassified_p0) != 1 else ""
            )
        )
    if report.unresolved:
        parts.append(
            "{} source{} could not be read".format(
                len(report.unresolved), "s" if len(report.unresolved) != 1 else ""
            )
        )
    parts.append("parity issue: {}".format(plan.get("action", "none")))
    parts.append("fingerprint {}".format(report.fingerprint))
    return "; ".join(parts)


def _check(args: argparse.Namespace) -> int:
    ledger = load_provenance(args.ledger)
    missing = ledger_module.missing_references(ledger, args.root)
    if missing:
        # Refused rather than reported as drift: a source that points at Continuum
        # code which is not here cannot be classified against, and the fix is a
        # ledger edit, not a review of somebody else's repository.
        raise ProvenanceError(
            "missing_continuum_reference",
            "the ledger names Continuum paths that do not exist: {}. Either the code "
            "moved or the classification does not hold any more.".format(", ".join(missing)),
        )

    if args.readings:
        readings: List[drift.SourceReading] = []
        for path in args.readings:
            with open(path, "r", encoding="utf-8") as handle:
                readings.extend(_readings_from(handle.read()))
    elif args.capture_live:
        readings = collect(
            ledger,
            public_token_env=args.public_token_env,
            pull_request_limit=args.max_pull_requests,
            api_base=args.api_base,
        )
    else:
        raise ProvenanceError(
            "no_readings",
            "pass --capture-live to read the sources, or --readings with captured "
            "readings to check",
        )

    report = drift.check(ledger, readings=readings, generated_at=_now())
    document = _report_document(
        report,
        existing_number=args.existing_issue,
        existing_body=args.existing_body or "",
    )
    if args.out:
        _write(Path(args.out), document)

    if args.render:
        for reading in report.readings:
            _summary(
                "  {} [{}] {} {} -> {}".format(
                    reading.id,
                    reading.severity,
                    reading.status,
                    (reading.baseline_sha or "?")[:12],
                    (reading.head_sha or "?")[:12],
                )
            )
            for finding in reading.findings:
                _summary("    {} {}".format(finding.reason, finding.path))

    plan = document["issue"]
    _summary(_summary_line(report, plan))
    if args.issue_body:
        Path(args.issue_body).parent.mkdir(parents=True, exist_ok=True)
        Path(args.issue_body).write_text(plan["body"], encoding="utf-8")
    if args.issue_title:
        Path(args.issue_title).write_text(plan["title"] + "\n", encoding="utf-8")

    if report.verdict == drift.CLEAN:
        return 0
    return DRIFT


def _readings_from(text: str) -> Sequence[drift.SourceReading]:
    """Read captured readings, adopting the digest of the ledger they are checked against.

    A captured reading records the ledger version it was taken against, and a
    mismatch is exactly what the report's staleness limit is for -- so the captured
    value is preserved here rather than overwritten, and the checker compares them
    itself. Overwriting it here would make the staleness check unable to fire.
    """

    document = json.loads(text)
    entries = document.get("readings") if isinstance(document, Mapping) else document
    if not isinstance(entries, list):
        raise ProvenanceError("no_readings", "a captured readings file must list readings")
    readings: List[drift.SourceReading] = []
    for entry in entries:
        findings = tuple(
            drift.Finding(
                path=str(item.get("path") or ""),
                reason=str(item.get("reason") or ""),
                change=str(item.get("change") or ""),
                previous_disposition=str(item.get("previous_disposition") or ""),
            )
            for item in entry.get("findings") or []
        )
        readings.append(
            drift.SourceReading(
                id=str(entry.get("id") or ""),
                repository=str(entry.get("repository") or ""),
                visibility=str(entry.get("visibility") or "public"),
                severity=str(entry.get("severity") or "p1"),
                status=str(entry.get("status") or ""),
                baseline_sha=str(entry.get("baseline_sha") or ""),
                head_sha=str(entry.get("head_sha") or ""),
                classified=tuple(
                    (str(item.get("path") or ""), str(item.get("disposition") or ""))
                    for item in entry.get("classified") or []
                ),
                findings=findings,
                pull_requests=tuple(int(number) for number in entry.get("pull_requests") or []),
                limits=tuple(str(limit) for limit in entry.get("limits") or []),
                credential=str(entry.get("credential") or ""),
                ledger_digest=str(entry.get("ledger_digest") or ""),
                audited_at=str(entry.get("audited_at") or ""),
            )
        )
    return readings


def _observe(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "")
    if not token:
        raise ProvenanceError(
            "no_token",
            "{} is not set; the observation needs a read credential".format(args.token_env),
        )
    client = _client(args.repo, token, api_base=args.api_base)
    document = drift.observe(client, branch=args.branch)
    document["observed_at"] = _now()
    if args.out:
        _write(Path(args.out), document)
    _summary(
        "provenance: observed {} at {} ({} workflow blobs)".format(
            args.repo, document["head_sha"] or "no head", len(document["workflows"])
        )
    )
    return 0 if document["head_sha"] else PLANE_FAILURE


def _render(args: argparse.Namespace) -> int:
    parsed = load_provenance(args.ledger)
    missing = ledger_module.missing_references(parsed, args.root)
    if missing:
        raise ProvenanceError(
            "missing_continuum_reference",
            "the ledger names Continuum paths that do not exist: {}".format(
                ", ".join(missing)
            ),
        )
    text = render_markdown(parsed)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="", flush=True)
    _summary("provenance: rendered {} source{} from {}".format(
        len(parsed.sources), "s" if len(parsed.sources) != 1 else "", args.ledger
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum.provenance.cli",
        description="Track the repositories Continuum stays in parity with, and the drift between them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="read the sources and report drift")
    check.add_argument("--ledger", default=DEFAULT_LEDGER)
    check.add_argument(
        "--capture-live",
        action="store_true",
        help="read each source from the GitHub API",
    )
    check.add_argument(
        "--readings",
        action="append",
        default=[],
        help="a captured readings document to check instead of reading live",
    )
    check.add_argument(
        "--public-token-env",
        default="GITHUB_TOKEN",
        help="the variable holding the credential public sources are read with",
    )
    check.add_argument(
        "--max-pull-requests",
        type=int,
        default=drift.DEFAULT_PR_LIMIT,
        help="how many open pull-request numbers to attach as evidence",
    )
    check.add_argument("--api-base", default="")
    check.add_argument(
        "--root",
        default=".",
        help="the working tree the ledger's Continuum references are checked against",
    )
    check.add_argument(
        "--existing-issue",
        type=int,
        default=0,
        help="the number of the open parity issue, for deduplication",
    )
    check.add_argument(
        "--existing-body",
        default="",
        help="the open parity issue's body, read for its fingerprint",
    )
    check.add_argument("--issue-body", default="", help="write the issue body here")
    check.add_argument("--issue-title", default="", help="write the issue title here")
    check.add_argument("--out", default="", help="write the drift report here")
    check.add_argument("--render", action="store_true", help="print per-source detail")
    check.set_defaults(handler=_check)

    observe = sub.add_parser("observe", help="read one repository's head and workflow inventory")
    observe.add_argument("--repo", required=True)
    observe.add_argument("--token-env", default="GITHUB_TOKEN")
    observe.add_argument("--branch", default="")
    observe.add_argument("--api-base", default="")
    observe.add_argument("--out", default="")
    observe.set_defaults(handler=_observe)

    render = sub.add_parser("render", help="render the ledger as Markdown")
    render.add_argument("--ledger", default=DEFAULT_LEDGER)
    render.add_argument("--root", default=".")
    render.add_argument("--out", default="")
    render.set_defaults(handler=_render)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except ProvenanceError as error:
        _fail(error.message)
        return PLANE_FAILURE
    except OSError as error:
        _fail(str(error))
        return PLANE_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())