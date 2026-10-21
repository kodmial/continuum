#!/usr/bin/env python3
"""Thin CLI for the durable operator STOP contract.

Thin wrapper over ``src/continuum/issue_stop.py`` and
``src/continuum/task_snapshot.py`` so workflow shell steps never re-implement
the STOP state machine. Every command prints a single stable token or JSON
payload and exits 0 on a successful evaluation; usage/I/O problems fail
closed with exit 2.

Usage::

    issue_stop.py is-active --comments-json comments.json --repo o/n \
        --issue 1 [--generation 1] [--owner owner]
    issue_stop.py render-intent --repo o/n --issue 1 --generation 1 \
        --fence 42:abcd
    issue_stop.py render-stop --repo o/n --issue 1 --generation 1 \
        --pinned-spec <hex> --live-spec <hex> [--reason operator]
    issue_stop.py select-targets --runs-json runs.json --issue 1 \
        [--generation 1] [--workflow-path path ...]
    issue_stop.py observe --intent-json intent.json --targets-json ids.json \
        --runs-json runs.json [--cancel-failure text]

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

from continuum.issue_stop import (  # noqa: E402
    OperatorStopIntent,
    make_fence,
    observe_cancellation,
    render_stop_intent,
    select_run_targets,
    select_stop_intent,
    stop_is_active,
)
from continuum.task_snapshot import (  # noqa: E402
    TaskSnapshotError,
    render_generation_stop,
)


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 2


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read JSON file {path!r}: {exc}") from exc


def _comments(args):
    data = _read_json(args.comments_json)
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        raise ValueError("comments JSON must be an array or object")
    return data


def cmd_is_active(args) -> int:
    stopped = stop_is_active(
        _comments(args),
        repo=args.repo,
        issue=args.issue,
        generation=args.generation,
        owner_login=args.owner or "",
    )
    print("stopped" if stopped else "active")
    return 0


def cmd_render_intent(args) -> int:
    fence = args.fence or make_fence(args.comment_id, args.comment_created_at)
    print(
        render_stop_intent(
            args.repo, args.issue, generation=args.generation, fence=fence
        )
    )
    return 0


def cmd_render_stop(args) -> int:
    print(
        render_generation_stop(
            args.repo,
            args.issue,
            generation=args.generation,
            pinned_spec=args.pinned_spec,
            live_spec=args.live_spec,
            reason=args.reason,
        )
    )
    return 0


def cmd_select_targets(args) -> int:
    runs = _read_json(args.runs_json)
    if isinstance(runs, dict):
        runs = runs.get("workflow_runs") or runs.get("runs") or []
    if not isinstance(runs, list):
        raise ValueError("runs JSON must be an array or workflow_runs object")
    selection = select_run_targets(
        runs,
        issue=args.issue,
        generation=args.generation,
        workflow_paths=args.workflow_path,
    )
    print(
        json.dumps(
            {
                "target_ids": selection.target_ids,
                "targets": [run.__dict__ for run in selection.targets],
                "skipped": [
                    {"run_id": run_id, "reason": reason}
                    for run_id, reason in selection.skipped
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_observe(args) -> int:
    intent_data = _read_json(args.intent_json)
    intent = OperatorStopIntent(
        repo=str(intent_data.get("repo") or "").lower(),
        issue=int(intent_data.get("issue") or 0),
        generation=int(intent_data.get("generation") or 1),
        fence=str(intent_data.get("fence") or ""),
    )
    target_data = _read_json(args.targets_json)
    if isinstance(target_data, dict):
        target_data = target_data.get("target_ids") or []
    runs = _read_json(args.runs_json)
    if isinstance(runs, dict):
        runs = runs.get("workflow_runs") or runs.get("runs") or []
    observation = observe_cancellation(
        intent=intent,
        target_ids=target_data,
        runs=runs,
        cancel_failure=args.cancel_failure,
    )
    print(
        json.dumps(
            {
                "state": observation.state,
                "target_ids": list(observation.target_ids),
                "pending_ids": list(observation.pending_ids),
                "stopped_ids": list(observation.stopped_ids),
                "failure": observation.failure,
                "terminal": observation.terminal,
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable operator STOP helper")
    sub = parser.add_subparsers(dest="command", required=True)

    active = sub.add_parser("is-active", help="print stopped or active")
    active.add_argument("--comments-json", required=True)
    active.add_argument("--repo", required=True)
    active.add_argument("--issue", required=True, type=int)
    active.add_argument("--generation", type=int, default=1)
    active.add_argument("--owner", default="")
    active.set_defaults(func=cmd_is_active)

    intent = sub.add_parser("render-intent", help="render the intent/fence comment")
    intent.add_argument("--repo", required=True)
    intent.add_argument("--issue", required=True, type=int)
    intent.add_argument("--generation", type=int, default=1)
    intent.add_argument("--fence", default="")
    intent.add_argument("--comment-id", type=int, default=0)
    intent.add_argument("--comment-created-at", default="")
    intent.set_defaults(func=cmd_render_intent)

    stop = sub.add_parser("render-stop", help="render the shared terminal stop")
    stop.add_argument("--repo", required=True)
    stop.add_argument("--issue", required=True, type=int)
    stop.add_argument("--generation", type=int, default=1)
    stop.add_argument("--pinned-spec", required=True)
    stop.add_argument("--live-spec", required=True)
    stop.add_argument("--reason", default="operator")
    stop.set_defaults(func=cmd_render_stop)

    targets = sub.add_parser("select-targets", help="select exact run ids")
    targets.add_argument("--runs-json", required=True)
    targets.add_argument("--issue", required=True, type=int)
    targets.add_argument("--generation", type=int, default=1)
    targets.add_argument("--workflow-path", action="append", default=[])
    targets.set_defaults(func=cmd_select_targets)

    observe = sub.add_parser("observe", help="classify observed cancellation")
    observe.add_argument("--intent-json", required=True)
    observe.add_argument("--targets-json", required=True)
    observe.add_argument("--runs-json", required=True)
    observe.add_argument("--cancel-failure", default="")
    observe.set_defaults(func=cmd_observe)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (TaskSnapshotError, ValueError, TypeError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
