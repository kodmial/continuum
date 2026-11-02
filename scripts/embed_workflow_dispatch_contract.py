#!/usr/bin/env python3
"""Embed the canonical workflow_dispatch contract parser into controllers.

The reusable controllers execute embedded JavaScript through
actions/github-script, so they cannot import a checked-in module at runtime.
This script keeps the two copies generated from one source of truth:

    .github/scripts/workflow_dispatch_contract.js

Run without arguments to rewrite both controller workflows. Run with --check
to fail when a workflow copy drifted; tests use the check mode.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".github/scripts/workflow_dispatch_contract.js"
BEGIN = "// BEGIN continuum-workflow-dispatch-contract"
END = "// END continuum-workflow-dispatch-contract"
TARGETS = (
    ROOT / ".github/workflows/continuum-auto-merge.yml",
    ROOT / ".github/workflows/continuum-pr-agent-auto-merge.yml",
)


def canonical_body() -> list[str]:
    lines = CANONICAL.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(BEGIN) + 1
        end = lines.index(END)
    except ValueError as error:
        raise SystemExit(f"{CANONICAL}: missing contract markers") from error
    return lines[start:end]


def rendered(indent: int, body: list[str]) -> str:
    prefix = " " * indent
    block = [f"{prefix}{BEGIN}"]
    block.extend(f"{prefix}{line}" if line else "" for line in body)
    block.append(f"{prefix}{END}")
    return "\n".join(block) + "\n"


def updated(path: pathlib.Path, body: list[str]) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    positions = [
        index for index, line in enumerate(lines) if line.rstrip("\n").lstrip().startswith(BEGIN)
    ]
    if len(positions) != 1:
        raise SystemExit(f"{path}: expected exactly one contract begin marker")
    start = positions[0]
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith(END):
        end += 1
    if end == len(lines):
        raise SystemExit(f"{path}: missing contract end marker")
    replacement = rendered(indent, body)
    return "".join(lines[:start]) + replacement + "".join(lines[end + 1 :])


def main() -> int:
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] and not check:
        raise SystemExit("usage: embed_workflow_dispatch_contract.py [--check]")
    body = canonical_body()
    stale = []
    for target in TARGETS:
        current = target.read_text(encoding="utf-8")
        desired = updated(target, body)
        if current == desired:
            continue
        stale.append(target)
        if not check:
            target.write_text(desired, encoding="utf-8")
    if check and stale:
        names = ", ".join(str(path.relative_to(ROOT)) for path in stale)
        print(
            "workflow_dispatch contract copies are stale: "
            f"{names}; run scripts/embed_workflow_dispatch_contract.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
