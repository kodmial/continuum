#!/usr/bin/env python3
"""Mandatory qualification lifecycle gate for issue bodies and comments.

Thin CLI over ``src/continuum/qualification.py``, which owns the deterministic
rules behind the implementation -> qualification -> capability-completion
lifecycle. Workflow steps that cannot express the marker rules in a job ``if:``
expression call this script from a step and branch on its printed result.

Usage (body via argument, file, or stdin)::

    qualification_gate.py refs --body '...'
    qualification_gate.py refs --body-file issue.md --self 182
    qualification_gate.py has-closing-keyword --body 'Fixes #182' --issue 182
    qualification_gate.py evidence-state --body-file comment.md --issue 184 --sha <40-hex> --author-association OWNER --author-login owner
    gh api ... --jq .body | qualification_gate.py refs

``refs`` prints one qualification issue number per line (nothing when the
body declares none) and always exits 0. ``has-closing-keyword`` and
``has-refs`` print ``true``/``false`` and always exit 0.
``evidence-state`` prints ``pass``, ``fail``, or ``unknown`` and always
exits 0. ``required-sha`` prints the latest required main SHA or nothing.
``evidence-state`` evaluates a single comment with caller-attested authorship:
pass ``--author-association``/``--author-login`` from the comment API object;
a bare body without trusted attestation fails closed to ``unknown`` so a
public forgery can never count as evidence.
Any usage or I/O failure is reported as ``::error::`` with exit 2.

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

from continuum.qualification import (  # noqa: E402
    contains_closing_keyword,
    latest_required_sha,
    parse_qualification_refs,
    qualification_evidence_state,
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
        raise RuntimeError("No body: pass --body, --body-file, or stdin.")
    return sys.stdin.read()


def _cmd_refs(args) -> int:
    for number in parse_qualification_refs(_read_body(args), args.self_number):
        print(number)
    return 0


def _cmd_has_refs(args) -> int:
    print(
        "true"
        if parse_qualification_refs(_read_body(args), args.self_number)
        else "false"
    )
    return 0


def _cmd_has_closing_keyword(args) -> int:
    print(
        "true"
        if contains_closing_keyword(_read_body(args), args.issue)
        else "false"
    )
    return 0


def _cmd_required_sha(args) -> int:
    sha = latest_required_sha(_read_body(args), args.self_number)
    if sha is not None:
        print(sha)
    return 0


def _cmd_evidence_state(args) -> int:
    # A bare body string predates author metadata and the engine treats it
    # as trusted for backward compatibility, so live callers must never
    # reduce untrusted comments to bodies here. Build a structured comment
    # so public forgeries fail closed unless the caller explicitly attests
    # trusted authorship via --author-association/--author-login.
    comment = {
        "body": _read_body(args),
        "author_association": args.author_association,
        "user": {"login": args.author_login},
        "author": {"login": args.author_login},
    }
    print(qualification_evidence_state([comment], args.issue, args.sha))
    return 0


def _add_body_options(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--body", default=None, help="Issue or comment body text.")
    group.add_argument("--body-file", default=None, help="File holding the body.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mandatory qualification lifecycle gate.")
    sub = parser.add_subparsers(dest="command", required=True)

    refs = sub.add_parser("refs", help="Print declared qualification issue numbers.")
    _add_body_options(refs)
    refs.add_argument("--self", dest="self_number", default=None, help="Owning issue number.")
    refs.set_defaults(func=_cmd_refs)

    has_refs = sub.add_parser("has-refs", help="Print true when qualifications are declared.")
    _add_body_options(has_refs)
    has_refs.add_argument("--self", dest="self_number", default=None)
    has_refs.set_defaults(func=_cmd_has_refs)

    closing = sub.add_parser(
        "has-closing-keyword",
        help="Print true when the text carries a GitHub auto-close keyword.",
    )
    _add_body_options(closing)
    closing.add_argument("--issue", default=None, help="Issue number the keyword must target.")
    closing.set_defaults(func=_cmd_has_closing_keyword)

    required = sub.add_parser(
        "required-sha", help="Print the latest required main SHA, if any."
    )
    _add_body_options(required)
    required.add_argument("--self", dest="self_number", default=None)
    required.set_defaults(func=_cmd_required_sha)

    evidence = sub.add_parser(
        "evidence-state", help="Print pass, fail, or unknown for one evidence comment."
    )
    _add_body_options(evidence)
    evidence.add_argument("--issue", required=True, help="Qualification issue number.")
    evidence.add_argument("--sha", required=True, help="Required 40-hex main SHA.")
    evidence.add_argument(
        "--author-association",
        default="NONE",
        help="Comment author_association attested by the caller (OWNER/MEMBER/COLLABORATOR trusted).",
    )
    evidence.add_argument(
        "--author-login",
        default="",
        help="Comment author login attested by the caller (github-actions[bot] trusted for automation payloads).",
    )
    evidence.set_defaults(func=_cmd_evidence_state)

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
