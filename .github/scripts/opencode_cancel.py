#!/usr/bin/env python3
"""Deterministic physical-cancel helper for ``/oc-cancel``.

Thin CLI over ``src/continuum/opencode_cancel.py``. Workflow steps use it to
render the durable operator stop marker, decide whether a stop is effective,
and select only provenance-verified active runs from an already-fetched
Actions inventory.

Usage::

    opencode_cancel.py render-stop --repo o/n --issue 1 [--generation 1]
    opencode_cancel.py render-resume --repo o/n --issue 1 [--generation 1]
    opencode_cancel.py has-stop --comments-json comments.json \
        --repo o/n --issue 1 [--generation 1] [--owner login]
    opencode_cancel.py has-operator-stop --comments-json comments.json \
        --repo o/n --issue 1 [--generation 1] [--owner login]
    opencode_cancel.py stop-kind --comments-json comments.json \
        --repo o/n --issue 1 [--generation 1] [--owner login]
    opencode_cancel.py plan --runs-json runs.json --repo o/n --issue 1 \
        [--generation 1] [--workflow-path PATH] [--workflow-name NAME] \
        [--child-id ID] [--associated-run-id ID] [--exclude-run-id ID]
    opencode_cancel.py classify --status 202
    opencode_cancel.py observe --run-json run.json

``has-stop`` prints ``stopped`` or ``active`` and covers both the operator
stop and the existing drift/tamper generation stop. ``plan`` prints one JSON
object and fails closed (``inventory: unavailable``) for malformed input.
Any usage or I/O failure is reported as ``::error::`` with exit 2.

Standard library only.
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

from continuum.opencode_cancel import (  # noqa: E402
    classify_cancel_response,
    execution_stop_kind,
    is_execution_stopped,
    is_operator_stopped,
    needs_force_cancel,
    observe_cancel_outcome,
    plan_cancellation,
    render_operator_resume,
    render_operator_stop,
)
from continuum.task_snapshot import DEFAULT_GENERATION, TaskSnapshotError  # noqa: E402


def _fail(message: str) -> int:
    print("::error::{}".format(message), file=sys.stderr)
    return 2


def _read_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise TaskSnapshotError("could not read JSON file {!r}: {}".format(path, exc)) from exc


def cmd_render_stop(args: argparse.Namespace) -> int:
    print(render_operator_stop(args.repo, args.issue, generation=args.generation))
    return 0


def cmd_render_resume(args: argparse.Namespace) -> int:
    print(render_operator_resume(args.repo, args.issue, generation=args.generation))
    return 0


def cmd_has_stop(args: argparse.Namespace) -> int:
    comments = _read_json_file(args.comments_json)
    if not isinstance(comments, list):
        return _fail("comments file {!r} is not a JSON array".format(args.comments_json))
    stopped = is_execution_stopped(
        comments,
        repo=args.repo,
        issue=args.issue,
        generation=args.generation,
        owner_login=args.owner or "",
    )
    print("stopped" if stopped else "active")
    return 0


def cmd_has_operator_stop(args: argparse.Namespace) -> int:
    comments = _read_json_file(args.comments_json)
    if not isinstance(comments, list):
        return _fail("comments file {!r} is not a JSON array".format(args.comments_json))
    stopped = is_operator_stopped(
        comments,
        repo=args.repo,
        issue=args.issue,
        generation=args.generation,
        owner_login=args.owner or "",
    )
    print("stopped" if stopped else "active")
    return 0


def cmd_stop_kind(args: argparse.Namespace) -> int:
    comments = _read_json_file(args.comments_json)
    if not isinstance(comments, list):
        return _fail("comments file {!r} is not a JSON array".format(args.comments_json))
    print(
        execution_stop_kind(
            comments,
            repo=args.repo,
            issue=args.issue,
            generation=args.generation,
            owner_login=args.owner or "",
        )
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    runs = _read_json_file(args.runs_json)
    if isinstance(runs, dict) and isinstance(runs.get("workflow_runs"), list):
        runs = runs["workflow_runs"]
    result = plan_cancellation(
        runs,
        repo=args.repo,
        issue=args.issue,
        generation=args.generation,
        workflow_paths=args.workflow_path or (),
        workflow_names=args.workflow_name or (),
        child_id=args.child_id or "",
        associated_run_ids=args.associated_run_id or (),
        exclude_run_id=args.exclude_run_id,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    print(classify_cancel_response(args.status))
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    run = _read_json_file(args.run_json)
    if not isinstance(run, dict):
        return _fail("run file {!r} is not a JSON object".format(args.run_json))
    print(
        json.dumps(
            {
                "outcome": observe_cancel_outcome(run),
                "needs_force_cancel": needs_force_cancel(run),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Physical OpenCode cancellation helper")
    sub = parser.add_subparsers(dest="command", required=True)

    render_stop = sub.add_parser("render-stop", help="render the operator stop marker")
    render_stop.add_argument("--repo", required=True)
    render_stop.add_argument("--issue", required=True, type=int)
    render_stop.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    render_stop.set_defaults(func=cmd_render_stop)

    render_resume = sub.add_parser("render-resume", help="render the operator resume marker")
    render_resume.add_argument("--repo", required=True)
    render_resume.add_argument("--issue", required=True, type=int)
    render_resume.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    render_resume.set_defaults(func=cmd_render_resume)

    has_stop = sub.add_parser("has-stop", help="print stopped or active")
    has_stop.add_argument("--comments-json", required=True)
    has_stop.add_argument("--repo", required=True)
    has_stop.add_argument("--issue", required=True, type=int)
    has_stop.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    has_stop.add_argument("--owner", default="")
    has_stop.set_defaults(func=cmd_has_stop)

    has_operator_stop = sub.add_parser(
        "has-operator-stop", help="print stopped or active for the operator marker only"
    )
    has_operator_stop.add_argument("--comments-json", required=True)
    has_operator_stop.add_argument("--repo", required=True)
    has_operator_stop.add_argument("--issue", required=True, type=int)
    has_operator_stop.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    has_operator_stop.add_argument("--owner", default="")
    has_operator_stop.set_defaults(func=cmd_has_operator_stop)

    stop_kind = sub.add_parser("stop-kind", help="print operator, generation, or active")
    stop_kind.add_argument("--comments-json", required=True)
    stop_kind.add_argument("--repo", required=True)
    stop_kind.add_argument("--issue", required=True, type=int)
    stop_kind.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    stop_kind.add_argument("--owner", default="")
    stop_kind.set_defaults(func=cmd_stop_kind)

    plan = sub.add_parser("plan", help="select exact cancellation targets")
    plan.add_argument("--runs-json", required=True)
    plan.add_argument("--repo", required=True)
    plan.add_argument("--issue", required=True, type=int)
    plan.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    plan.add_argument("--workflow-path", action="append", default=[])
    plan.add_argument("--workflow-name", action="append", default=[])
    plan.add_argument("--child-id", default="")
    plan.add_argument("--associated-run-id", action="append", default=[])
    plan.add_argument("--exclude-run-id", default=None)
    plan.set_defaults(func=cmd_plan)

    classify = sub.add_parser("classify", help="classify a cancel HTTP status")
    classify.add_argument("--status", required=True)
    classify.set_defaults(func=cmd_classify)

    observe = sub.add_parser("observe", help="describe an observed run state")
    observe.add_argument("--run-json", required=True)
    observe.set_defaults(func=cmd_observe)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskSnapshotError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
