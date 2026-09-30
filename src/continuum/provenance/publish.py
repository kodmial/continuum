"""Turn a drift report into one issue, or one comment, or nothing at all.

Split from :mod:`continuum.provenance.cli` so the write half is a module of its
own. The CLI is what a person runs to *find out*; this is what a scheduled
workflow runs to *publish*, and it is the only part of the plane that holds a
write scope. Keeping it separate means the read-only command line cannot acquire
one by accident.

Two steps, deliberately:

``plan``
    Read the open parity issue, decide what the report asks for, and write the
    decision out. No write scope needed, so it runs first and is inspectable on
    its own.
``--apply``
    Carry out the plan. Refuses a plan whose action it does not recognise rather
    than defaulting to one.

The deduplication decision lives in :func:`continuum.provenance.drift.issue_plan`
and is not reimplemented here. A second implementation of "is this the same drift"
would eventually disagree with the first, and the disagreement would show up as a
weekly comment nobody can explain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import drift
from .ledger import ProvenanceError, DEFAULT_CREDENTIAL_ENV

#: The marker that identifies an issue as this plane's. Every body carries it, so
#: an issue whose fingerprint line was edited by hand is still found rather than
#: duplicated.
MARKER = drift.ISSUE_MARKER

PLAN_SCHEMA = "continuum.provenance-issue-plan/v1"


def _issue_list_path(client: Any, label: str = "") -> str:
    import urllib.parse

    params: Dict[str, str] = {"state": "open", "per_page": 100}
    if label:
        params["labels"] = label
    query = urllib.parse.urlencode(params)
    return "/repos/{}/{}/issues?{}".format(client.owner, client.name, query)


def find_open_issue(client: Any, label: str = drift.ISSUE_LABEL) -> Dict[str, Any]:
    """The open parity issue, or an empty record.

    Matching is on the marker rather than the title: a title is edited by hand
    without anyone meaning to break the association, and the marker is written by
    the engine on every body it produces.

    The label narrows the search but is not trusted to have survived. A label can be
    removed by hand, and GitHub drops labels it does not recognise when an issue is
    created, so searching only by label would quietly stop finding the open issue
    and open a second one beside it. The fallback searches every open issue for the
    marker, which is the thing that actually identifies this plane's issue.
    """

    for candidate in (label, ""):
        issues = client.paginate(_issue_list_path(client, candidate))
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            if int(issue.get("number") or 0) <= 0:
                continue
            if issue.get("pull_request"):
                # GitHub's issues endpoint also returns pull requests. A parity issue
                # is never a pull request, and adopting one would put drift evidence in
                # a code review and then comment on it forever.
                continue
            body = str(issue.get("body") or "")
            if MARKER in body:
                return {
                    "number": int(issue["number"]),
                    "body": body,
                    "title": str(issue.get("title") or ""),
                }
    return {"number": 0, "body": "", "title": ""}


def build_plan(client: Any, report: drift.DriftReport) -> Dict[str, Any]:
    """The decision, with the issue it was decided against."""

    existing = find_open_issue(client)
    decision = drift.issue_plan(
        report, existing_number=existing["number"], existing_body=existing["body"]
    )
    return {
        "schema": PLAN_SCHEMA,
        "action": decision["action"],
        "reason": decision["reason"],
        "number": existing["number"],
        "fingerprint": report.fingerprint,
        "verdict": report.verdict,
        "title": decision["title"],
        "body": decision["body"],
        "label": decision["label"],
    }


def apply_plan(client: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Carry out a plan. A plan this module does not recognise is refused."""

    action = str(plan.get("action") or "")
    body = str(plan.get("body") or "")
    title = str(plan.get("title") or "")

    if action == drift._NONE:
        return {"action": action, "number": int(plan.get("number") or 0), "url": ""}

    if action == drift._CREATE:
        created = client.request(
            "POST",
            "/repos/{}/{}/issues".format(client.owner, client.name),
            {"title": title, "body": body, "labels": [str(plan.get("label") or "")]},
        )
        return {
            "action": action,
            "number": int(created.get("number") or 0),
            "url": str(created.get("html_url") or ""),
        }

    if action == drift._COMMENT:
        number = int(plan.get("number") or 0)
        if not number:
            raise ProvenanceError(
                "no_issue_to_comment",
                "the plan asks to comment on an issue but names none",
            )
        created = client.create_issue_comment(number, body)
        return {
            "action": action,
            "number": number,
            "url": str(created.get("html_url") or ""),
        }

    raise ProvenanceError(
        "unknown_action",
        "{!r} is not something this publisher can do".format(action),
    )


def _client(repo: str, token_env: str, api_base: str = "") -> Any:
    import os

    from ..review.github import GitHubClient, GitHubError

    token = os.environ.get(token_env, "")
    if not token:
        raise ProvenanceError(
            "no_token",
            "{} is not set; publishing needs a credential that can write issues".format(
                token_env
            ),
        )
    kwargs: Dict[str, Any] = {}
    if api_base:
        kwargs["api_base"] = api_base
    try:
        return GitHubClient(token, repo, **kwargs)
    except GitHubError as error:
        raise ProvenanceError("client_not_usable", str(error)) from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="continuum.provenance.publish",
        description="Publish one deduplicated parity issue for a drift report.",
    )
    parser.add_argument("--report", default="", help="the drift report to publish")
    parser.add_argument("--plan", default="", help="a plan written by an earlier run")
    parser.add_argument("--repo", required=True, help="owner/name of the issue tracker")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--out", default="", help="write the plan here")
    parser.add_argument(
        "--apply", action="store_true", help="carry out the plan rather than only writing it"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.apply:
            if not args.plan:
                raise ProvenanceError(
                    "no_plan", "--apply needs --plan: there is nothing to carry out"
                )
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if plan.get("schema") != PLAN_SCHEMA:
                raise ProvenanceError(
                    "unknown_plan_schema",
                    "expected {!r}, found {!r}".format(PLAN_SCHEMA, plan.get("schema")),
                )
            result = apply_plan(_client(args.repo, args.token_env, args.api_base), plan)
            print(json.dumps(result, sort_keys=True))
            return 0

        if not args.report:
            raise ProvenanceError(
                "no_report", "pass --report with a drift report, or --apply with a plan"
            )
        report = drift.load_report(args.report)
        plan = build_plan(_client(args.repo, args.token_env, args.api_base), report)
        if args.out:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(
            "provenance: {} ({})".format(plan["action"], plan["reason"]),
            flush=True,
        )
        return 0
    except ProvenanceError as error:
        print("provenance: {}".format(error.message), file=sys.stderr, flush=True)
        return 3
    except OSError as error:
        print("provenance: {}".format(error), file=sys.stderr, flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())