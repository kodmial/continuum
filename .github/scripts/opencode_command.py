#!/usr/bin/env python3
"""Exact manual OpenCode command gate for issue comments.

Thin CLI over ``src/continuum/opencode_commands.py``, which owns the single
deterministic rule that decides whether an issue comment is a manual OpenCode
command. Workflow job ``if:`` expressions cannot express that rule (they only
offer substring matching, which is the defect behind the Work Lock
self-trigger loop), so admission must call this script from a step and branch
on its printed result instead of gating on ``contains(body, '/oc')``.

The rule, briefly: a comment qualifies only when its first significant line
is exactly ``/oc`` or ``/opencode`` (surrounding whitespace allowed).
Prose, inline code, quotes, and Markdown blocks that merely mention the token
never qualify. ``/oc-cancel`` as the first significant line classifies as
``cancel`` and must suppress any launch; it is a launch guard only.

Usage (body via argument, file, or stdin)::

    opencode_command.py classify --body '/oc'
    opencode_command.py classify --body-file comment.md
    gh api ... --jq .body | opencode_command.py classify
    opencode_command.py is-manual --body-file comment.md
    opencode_command.py is-owner-command --body-file comment.md \\
        --author "$COMMENT_AUTHOR" --owner "$REPO_OWNER"

``classify`` prints ``run``, ``cancel``, or ``ignore`` and always exits 0.
The ``is-*`` predicates print ``true``/``false`` and always exit 0. Any
usage or I/O failure is reported as ``::error::`` with exit 2.

Standard library only, so it runs on stock ``ubuntu-latest`` runners without
dependency installation.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.opencode_commands import (  # noqa: E402
    CANCEL,
    DEFAULT_DISPATCH_MARKER,
    DEFAULT_RETRY_MARKER,
    IGNORE,
    RUN,
    classify_issue_comment,
    is_cancel_command,
    is_manual_command,
    is_owner_command,
    is_scheduler_dispatch,
    is_watchdog_recovery,
)


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        try:
            with open(args.body_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise RuntimeError("Cannot read body file {}: {}".format(args.body_file, exc))
    if sys.stdin.isatty():
        raise RuntimeError("No comment body: pass --body, --body-file, or stdin.")
    return sys.stdin.read()


def _add_body_options(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--body", default=None, help="Comment body text.")
    group.add_argument("--body-file", default=None, help="File holding the comment body.")


def _cmd_classify(args) -> int:
    print(classify_issue_comment(_read_body(args)))
    return 0


def _cmd_is_manual(args) -> int:
    print("true" if is_manual_command(_read_body(args)) else "false")
    return 0


def _cmd_is_cancel(args) -> int:
    print("true" if is_cancel_command(_read_body(args)) else "false")
    return 0


def _cmd_is_owner_command(args) -> int:
    print("true" if is_owner_command(_read_body(args), args.author, args.owner) else "false")
    return 0


def _cmd_is_scheduler_dispatch(args) -> int:
    print("true" if is_scheduler_dispatch(_read_body(args), args.marker) else "false")
    return 0


def _cmd_is_watchdog_recovery(args) -> int:
    print("true" if is_watchdog_recovery(_read_body(args), args.marker) else "false")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exact manual OpenCode command gate.")
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("classify", help="Print run, cancel, or ignore.")
    _add_body_options(classify)
    classify.set_defaults(func=_cmd_classify)

    is_manual = sub.add_parser("is-manual", help="Print true when the body is an exact manual command.")
    _add_body_options(is_manual)
    is_manual.set_defaults(func=_cmd_is_manual)

    is_cancel = sub.add_parser("is-cancel", help="Print true when the body is an exact cancel command.")
    _add_body_options(is_cancel)
    is_cancel.set_defaults(func=_cmd_is_cancel)

    owner_command = sub.add_parser(
        "is-owner-command",
        help="Print true only for an exact manual command authored by the repository owner.",
    )
    _add_body_options(owner_command)
    owner_command.add_argument("--author", required=True, help="Comment author login.")
    owner_command.add_argument("--owner", required=True, help="Repository owner login.")
    owner_command.set_defaults(func=_cmd_is_owner_command)

    is_dispatch = sub.add_parser(
        "is-scheduler-dispatch",
        help="Print true when the body carries the scheduler dispatch marker.",
    )
    _add_body_options(is_dispatch)
    is_dispatch.add_argument("--marker", default=DEFAULT_DISPATCH_MARKER)
    is_dispatch.set_defaults(func=_cmd_is_scheduler_dispatch)

    is_recovery = sub.add_parser(
        "is-watchdog-recovery",
        help="Print true when the body carries the watchdog recovery marker.",
    )
    _add_body_options(is_recovery)
    is_recovery.add_argument("--marker", default=DEFAULT_RETRY_MARKER)
    is_recovery.set_defaults(func=_cmd_is_watchdog_recovery)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print("::error::{}".format(" ".join(str(exc).split())), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


# Re-exported so contract checks can pin the CLI to the canonical rule without
# importing the engine module twice.
__all__ = ["RUN", "CANCEL", "IGNORE"]
