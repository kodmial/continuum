#!/usr/bin/env python3
"""Immutable task-snapshot admission helper (kodmial/continuum#295).

Thin CLI over ``src/continuum/task_snapshot.py``, which owns the
deterministic snapshot rules. Shell workflow steps that cannot express
the pin/verify logic inline (delegated child worker/review, which run
with the Continuum engine checked out) call this script and branch on
its printed action.

Usage::

    task_snapshot_admission.py decide \\
        --issue-json issue.json --comments-json comments.json \\
        --repo owner/name --issue 123 [--owner owner-login] \\
        [--generation 1] --record-out record.json --decision-out decision.json
    task_snapshot_admission.py render-comment --record-file record.json
    task_snapshot_admission.py render-pr-ref --record-file record.json
    task_snapshot_admission.py render-marker --record-file record.json
    task_snapshot_admission.py render-stop --repo o/n --issue 1
        --pinned-spec <hex> --live-spec <hex> --reason drift
    task_snapshot_admission.py render-recover --repo o/n --issue 1
    task_snapshot_admission.py has-stop --comments-json comments.json
        --repo o/n --issue 1
    task_snapshot_admission.py digests --title-file title.txt --body-file body.txt

``decide`` prints exactly one of ``pin``, ``proceed``,
``drift_blocked`` or ``tampered_fail_closed`` and always exits 0 on a
successful evaluation. ``--record-out`` receives the snapshot record to
pin when the action is ``pin``, or the authoritative pinned record when
the action is ``proceed``. ``--decision-out`` always receives the full
decision JSON (action, reason, diagnostic, digests). Any usage or I/O
failure is reported as ``::error::`` with exit 2 so callers fail closed
instead of implementing live mutable text.

The comments file is a JSON array of issue-comment objects as returned
by the GitHub API (``body``, ``author_association``, ``user.login``,
``created_at``/``updated_at``, ``id``). The issue file is a JSON object
with ``title`` and ``body`` (as returned by ``gh issue view --json
title,body`` or ``gh api .../issues/N``).

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

from continuum.task_snapshot import (  # noqa: E402
    DEFAULT_GENERATION,
    TaskSnapshotError,
    body_digest,
    build_snapshot,
    decide_admission,
    is_generation_terminally_stopped,
    render_generation_recover,
    render_generation_stop,
    render_pr_snapshot_ref,
    render_snapshot_comment,
    select_snapshot,
    snapshot_marker_line,
    spec_digest,
    title_digest,
)


def _fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 2


def _read_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise TaskSnapshotError(f"could not read JSON file {path!r}: {exc}") from exc


def _write_json_file(path: str, payload) -> None:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    except OSError as exc:
        raise TaskSnapshotError(f"could not write JSON file {path!r}: {exc}") from exc


def cmd_decide(args: argparse.Namespace) -> int:
    try:
        issue = _read_json_file(args.issue_json)
        comments = _read_json_file(args.comments_json)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    if not isinstance(issue, dict):
        return _fail(f"issue file {args.issue_json!r} is not a JSON object")
    if not isinstance(comments, list):
        return _fail(f"comments file {args.comments_json!r} is not a JSON array")
    try:
        decision = decide_admission(
            repo=args.repo,
            issue=args.issue,
            generation=args.generation,
            live_title=issue.get("title") or "",
            live_body=issue.get("body") or "",
            comments=comments,
            owner_login=args.owner or "",
        )
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    record = None
    try:
        if decision.action == "pin":
            record = build_snapshot(
                args.repo,
                args.issue,
                issue.get("title") or "",
                issue.get("body") or "",
                generation=args.generation,
                created_by="github-actions[bot]",
                created_at="",
            )
        elif decision.action == "proceed":
            selection = select_snapshot(
                comments,
                repo=args.repo,
                issue=args.issue,
                generation=args.generation,
                owner_login=args.owner or "",
            )
            if selection.snapshot is not None and not selection.snapshot.tampered:
                stored = selection.snapshot.record or {}
                record = build_snapshot(
                    selection.snapshot.repo,
                    selection.snapshot.issue,
                    stored.get("title") or "",
                    stored.get("body") or "",
                    generation=selection.snapshot.generation,
                    created_by=str(stored.get("created_by", "")),
                    created_at=str(stored.get("created_at", "")),
                )
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    if record is not None:
        try:
            _write_json_file(args.record_out, record)
        except TaskSnapshotError as exc:
            return _fail(str(exc))
    if decision.action in ("drift_blocked", "tampered_fail_closed"):
        # Terminal-stop evidence for workflow publishers: the pinned and
        # live digests plus a closed reason vocabulary word, without any
        # title/body text. Never leaks prompt or user content.
        try:
            stop_selection = select_snapshot(
                comments,
                repo=args.repo,
                issue=args.issue,
                generation=args.generation,
                owner_login=args.owner or "",
            )
            live_title = issue.get("title") or ""
            live_body = issue.get("body") or ""
            payload_stop = {
                "pinned_spec_sha256": (
                    stop_selection.snapshot.spec_sha256
                    if stop_selection.snapshot is not None
                    else ""
                ),
                "live_spec_sha256": spec_digest(live_title, live_body),
                "live_title_sha256": title_digest(live_title),
                "live_body_sha256": body_digest(live_body),
                "stop_reason": (
                    "tampered"
                    if decision.action == "tampered_fail_closed"
                    else "drift"
                ),
            }
        except TaskSnapshotError:
            payload_stop = {}
    else:
        payload_stop = {}
    payload = {
        "action": decision.action,
        "reason": decision.reason,
        "diagnostic": decision.diagnostic,
        "repo": args.repo,
        "issue": args.issue,
        "generation": args.generation,
    }
    payload.update(payload_stop)
    if record is not None:
        payload["spec_sha256"] = record["spec_sha256"]
        payload["title_sha256"] = record["title_sha256"]
        payload["body_sha256"] = record["body_sha256"]
    try:
        _write_json_file(args.decision_out, payload)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    print(decision.action)
    return 0


def cmd_render_comment(args: argparse.Namespace) -> int:
    try:
        record = _read_json_file(args.record_file)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    try:
        print(render_snapshot_comment(record))
    except (KeyError, TaskSnapshotError, TypeError) as exc:
        return _fail(f"cannot render snapshot comment: {exc}")
    return 0


def cmd_render_pr_ref(args: argparse.Namespace) -> int:
    try:
        record = _read_json_file(args.record_file)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    try:
        print(render_pr_snapshot_ref(record))
    except (KeyError, TaskSnapshotError, TypeError) as exc:
        return _fail(f"cannot render snapshot reference: {exc}")
    return 0


def cmd_render_marker(args: argparse.Namespace) -> int:
    try:
        record = _read_json_file(args.record_file)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    try:
        print(snapshot_marker_line(record))
    except (KeyError, TaskSnapshotError, TypeError) as exc:
        return _fail(f"cannot render snapshot marker: {exc}")
    return 0


def cmd_render_stop(args: argparse.Namespace) -> int:
    try:
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
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    return 0


def cmd_render_recover(args: argparse.Namespace) -> int:
    try:
        print(
            render_generation_recover(
                args.repo, args.issue, generation=args.generation
            )
        )
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    return 0


def cmd_has_stop(args: argparse.Namespace) -> int:
    try:
        comments = _read_json_file(args.comments_json)
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    if not isinstance(comments, list):
        return _fail(f"comments file {args.comments_json!r} is not a JSON array")
    try:
        stopped = is_generation_terminally_stopped(
            comments,
            repo=args.repo,
            issue=args.issue,
            generation=args.generation,
            owner_login=args.owner or "",
        )
    except TaskSnapshotError as exc:
        return _fail(str(exc))
    print("stopped" if stopped else "active")
    return 0


def cmd_digests(args: argparse.Namespace) -> int:
    try:
        with open(args.title_file, "r", encoding="utf-8") as handle:
            title = handle.read()
        with open(args.body_file, "r", encoding="utf-8") as handle:
            body = handle.read()
    except OSError as exc:
        return _fail(f"could not read digest inputs: {exc}")
    print(title_digest(title))
    print(body_digest(body))
    print(spec_digest(title, body))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task snapshot admission helper")
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="decide pin/proceed/drift/tamper")
    decide.add_argument("--issue-json", required=True)
    decide.add_argument("--comments-json", required=True)
    decide.add_argument("--repo", required=True)
    decide.add_argument("--issue", required=True, type=int)
    decide.add_argument("--owner", default="")
    decide.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    decide.add_argument("--record-out", required=True)
    decide.add_argument("--decision-out", required=True)
    decide.set_defaults(func=cmd_decide)

    render_comment = sub.add_parser("render-comment", help="render snapshot comment")
    render_comment.add_argument("--record-file", required=True)
    render_comment.set_defaults(func=cmd_render_comment)

    render_ref = sub.add_parser("render-pr-ref", help="render PR snapshot reference")
    render_ref.add_argument("--record-file", required=True)
    render_ref.set_defaults(func=cmd_render_pr_ref)

    render_marker = sub.add_parser("render-marker", help="render snapshot marker line")
    render_marker.add_argument("--record-file", required=True)
    render_marker.set_defaults(func=cmd_render_marker)

    render_stop = sub.add_parser("render-stop", help="render terminal generation stop")
    render_stop.add_argument("--repo", required=True)
    render_stop.add_argument("--issue", required=True, type=int)
    render_stop.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    render_stop.add_argument("--pinned-spec", required=True)
    render_stop.add_argument("--live-spec", required=True)
    render_stop.add_argument("--reason", default="drift")
    render_stop.set_defaults(func=cmd_render_stop)

    render_recover = sub.add_parser("render-recover", help="render explicit owner recovery")
    render_recover.add_argument("--repo", required=True)
    render_recover.add_argument("--issue", required=True, type=int)
    render_recover.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    render_recover.set_defaults(func=cmd_render_recover)

    has_stop = sub.add_parser("has-stop", help="print stopped or active")
    has_stop.add_argument("--comments-json", required=True)
    has_stop.add_argument("--repo", required=True)
    has_stop.add_argument("--issue", required=True, type=int)
    has_stop.add_argument("--owner", default="")
    has_stop.add_argument("--generation", default=DEFAULT_GENERATION, type=int)
    has_stop.set_defaults(func=cmd_has_stop)

    digests = sub.add_parser("digests", help="print title/body/spec digests")
    digests.add_argument("--title-file", required=True)
    digests.add_argument("--body-file", required=True)
    digests.set_defaults(func=cmd_digests)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskSnapshotError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
