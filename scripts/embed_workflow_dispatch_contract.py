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
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".github/scripts/workflow_dispatch_contract.js"
BEGIN = "// BEGIN continuum-workflow-dispatch-contract"
END = "// END continuum-workflow-dispatch-contract"
TARGETS = (
    ROOT / ".github/workflows/continuum-auto-merge.yml",
    ROOT / ".github/workflows/continuum-pr-agent-auto-merge.yml",
)
# actions/github-script passes the inline script as a command-line argument.
# Linux caps a single argv entry at 128 KiB, so a larger payload fails before
# the action starts. Keep a safety margin and make the limit a hard contract.
MAX_INLINE_SCRIPT_BYTES = 100_000
INLINE_SCRIPT = re.compile(r"^( *)(script):[ \t]*(.*)$")
BLOCK_SCALAR = re.compile(r"^[|>][-+]?[0-9]*[ \t]*$")


def inline_script_sizes(text: str) -> list[tuple[int, int]]:
    """Return ``(line_number, utf8_bytes)`` for every inline script value."""
    lines = text.splitlines(keepends=True)
    sizes: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        match = INLINE_SCRIPT.match(lines[index])
        if not match:
            index += 1
            continue
        indent = len(match.group(1))
        value = match.group(3).strip()
        if not BLOCK_SCALAR.match(value):
            if value and not value.startswith("#"):
                sizes.append((index + 1, len(value.encode("utf-8"))))
            index += 1
            continue
        body_start = index + 1
        body_end = body_start
        while body_end < len(lines):
            candidate = lines[body_end]
            if candidate.strip() and (
                len(candidate) - len(candidate.lstrip(" ")) <= indent
            ):
                break
            body_end += 1
        block_indent = None
        for line in lines[body_start:body_end]:
            if line.strip():
                current = len(line) - len(line.lstrip(" "))
                block_indent = (
                    current if block_indent is None
                    else min(block_indent, current)
                )
        payload = "".join(
            line[block_indent:] if block_indent is not None else line
            for line in lines[body_start:body_end]
        )
        sizes.append((index + 1, len(payload.encode("utf-8"))))
        index = body_end
    return sizes


def check_inline_script_sizes(path: pathlib.Path, text: str) -> None:
    for line_number, size in inline_script_sizes(text):
        if size > MAX_INLINE_SCRIPT_BYTES:
            raise SystemExit(
                f"{path}:{line_number}: inline script is {size} bytes, "
                f"above the {MAX_INLINE_SCRIPT_BYTES}-byte argv-safe limit; "
                "stage it in a run step and keep only a small loader in script:"
            )


def check_every_workflow_script_size() -> None:
    """Apply the argv-safe limit to every workflow, not only the targets.

    Any ``actions/github-script`` caller can overflow the Linux argv limit, so
    a size regression outside the generated controllers must fail this
    contract too instead of staying invisible until the run starts.
    """
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        check_inline_script_sizes(
            workflow, workflow.read_text(encoding="utf-8")
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
    check_every_workflow_script_size()
    for target in TARGETS:
        current = target.read_text(encoding="utf-8")
        desired = updated(target, body)
        check_inline_script_sizes(target, desired)
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
