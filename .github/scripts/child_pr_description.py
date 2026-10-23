#!/usr/bin/env python3
"""CLI for the deterministic delegated Child PR description builder.

The worker calls ``build`` with its existing runner-local snapshot record,
agent result file, verified diff, and canonical snapshot reference.  The
helper writes the public body and a small machine-readable decision.  It never
talks to GitHub and never reads the Parent issue text from the live network.

Usage::

    child_pr_description.py build \\
        --task-number 157 --issue-json snapshot.json \\
        --result-file result.md --diff-file change.patch \\
        --snapshot-ref '<!-- continuum-task-snapshot-ref ... -->' \\
        --out pr-body.md --decision-out decision.json
    child_pr_description.py revision --body-file pr-body.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.child_pr_description import (  # noqa: E402
    ChildPrDescriptionError,
    build_child_pr_body,
    description_revision,
)


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 2


def _read(path: str, *, required: bool = True) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        if not required:
            return ""
        raise ChildPrDescriptionError(f"could not read {path!r}: {exc}") from exc


def cmd_build(args: argparse.Namespace) -> int:
    try:
        issue = json.loads(_read(args.issue_json))
    except (ChildPrDescriptionError, ValueError) as exc:
        return _fail(str(exc))
    if not isinstance(issue, dict):
        return _fail("issue snapshot is not a JSON object")
    try:
        decision = build_child_pr_body(
            task_number=args.task_number,
            issue_title=issue.get("title") or "",
            issue_body=issue.get("body") or "",
            result_text=_read(args.result_file, required=False),
            diff_text=_read(args.diff_file, required=False),
            snapshot_ref=args.snapshot_ref,
            intro=args.intro,
        )
    except ChildPrDescriptionError as exc:
        return _fail(str(exc))
    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(decision.body)
        with open(args.decision_out, "w", encoding="utf-8") as handle:
            json.dump(decision.as_dict(), handle, ensure_ascii=False, sort_keys=True)
    except OSError as exc:
        return _fail(f"could not write PR description artifacts: {exc}")
    print(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_revision(args: argparse.Namespace) -> int:
    try:
        body = _read(args.body_file)
    except ChildPrDescriptionError as exc:
        return _fail(str(exc))
    digest = description_revision(body)
    if not digest:
        return _fail("PR body has no single trusted description revision marker")
    print(digest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--task-number", required=True)
    build.add_argument("--issue-json", required=True)
    build.add_argument("--result-file", required=True)
    build.add_argument("--diff-file", required=True)
    build.add_argument("--snapshot-ref", required=True)
    build.add_argument("--intro", default="")
    build.add_argument("--out", required=True)
    build.add_argument("--decision-out", required=True)
    build.set_defaults(func=cmd_build)

    revision = sub.add_parser("revision")
    revision.add_argument("--body-file", required=True)
    revision.set_defaults(func=cmd_revision)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
