#!/usr/bin/env python3
"""The consumer boundary: no consumer-specific value in generic Continuum code.

Continuum is adopted by repositories it has never heard of. That only works if
every value that differs between consumers resolves from configuration -- the
reviewed desired state in ``.github/continuum.yml``, repository ``vars``, or
repository secrets -- instead of from a constant inside Continuum.

This module is the check that says so. It scans the *generic surface*: the
reusable workflows a consumer calls, the runtime scripts those workflows run,
and the composite action they share. A consumer name, a model identifier, or a
way to choose a Continuum other than the release the caller pinned would each
be a repository that has to fork Continuum to be served.

Three things are deliberately not scanned, and the distinction is the point:

* ``src/continuum/`` -- the engine library. It is product code, and the shadow
  package's whole subject is the preserved reference snapshot under
  ``reference/``.
* ``reference/``, ``fixtures/``, ``docs/``, and the test suites -- historical
  snapshots, consumer-shaped test material, and prose. They describe consumers;
  they do not run for them.

What is left has to be clean, or explicitly recorded in
``consumer-boundary-exceptions.txt`` with a verdict and a reason. An exception
that stops matching is reported as stale, so the ledger cannot quietly grow into
a backlog: removing a consumer name from Continuum also removes its entry.

Run directly::

    python3 .github/scripts/consumer_boundary.py

Standard library only, because a consumer has to be able to run it from a
pinned checkout of the release it selected.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

FORBIDDEN_NAMES = ".github/scripts/consumer-forbidden-names.txt"
EXCEPTIONS = ".github/scripts/consumer-boundary-exceptions.txt"

#: The contract-core guard's own data file. It carries the same kind of names,
#: so it is excluded here for the same reason: a guard's vocabulary is not a
#: consumer dependency.
CONTRACT_FORBIDDEN_NAMES = ".github/scripts/contract-forbidden-names.txt"

#: The directories holding the generic surface: the reusable workflows a
#: consumer calls, the runtime scripts those workflows run, and the composite
#: action they share.
WORKFLOW_DIR = ".github/workflows"
SCRIPT_DIR = ".github/scripts"
ACTION_DIR = ".github/actions"

#: Only these are scanned. Everything else under a root is data, a document, or
#: a check whose own vocabulary is the thing being guarded.
SCAN_SUFFIXES = (".yml", ".yaml", ".py", ".sh")

#: The surface is selected by capability, not by a hand-maintained list: a
#: workflow is generic exactly when it declares `workflow_call`, because that is
#: what makes it callable by a repository that is not this one. Continuum's own
#: bootstrap plane -- the workflows it runs for itself -- has no such trigger
#: and is therefore not part of the promise it makes to anybody else.
_REUSABLE_RE = re.compile(r"^on:(?:\s*\[(?P<inline>[^\]]*)\])?\s*$", re.M)
_WORKFLOW_CALL_RE = re.compile(r"^\s*workflow_call:\s*$", re.M)

#: The verdict vocabulary. Each is a *different* reason to keep a name, and the
#: distinction is enforced: a release reference is GitHub's syntax requirement, a
#: compatibility token is Continuum's own migration surface, and a provenance
#: reference is history that decides nothing at runtime.
VERDICTS = ("release", "compat", "provenance")

_COMMENT = "#"
_FIELDS = 4


class BoundaryError(Exception):
    """The boundary's own data files are unusable."""


class Pattern(NamedTuple):
    """One banned expression, with the file it came from."""

    source: str
    expression: str
    regex: "re.Pattern[str]"


class Exception_(NamedTuple):
    """A reviewed residual: one name, in one place, for one stated reason."""

    verdict: str
    source: str
    expression: str
    glob: str
    reason: str
    regex: "re.Pattern[str]"

    def matches(self, relative_path: str, line: str) -> bool:
        return fnmatch.fnmatch(relative_path, self.glob) and bool(
            self.regex.search(line)
        )


class Violation(NamedTuple):
    """A banned name in generic code that no exception covers."""

    path: str
    line_number: int
    line: str
    expression: str
    source: str


def _rooted(root: str, path: str) -> str:
    """The file to read for ``path``, which is written relative to the tree.

    ``--root`` exists so the guard can be pointed at a tree other than the
    checkout it ships in -- a consumer running it from a pinned release, or the
    tests. Reading the *data* from the process's working directory while reading
    the *code* from ``root`` would silently scan one tree with another tree's
    rules, which is the one way this guard could be made to lie.
    """
    return path if os.path.isabs(path) else os.path.join(root, path)


def _data_lines(root: str, path: str) -> List[str]:
    full = _rooted(root, path)
    try:
        with open(full, encoding="utf-8") as handle:
            raw = handle.read().splitlines()
    except OSError as error:
        raise BoundaryError(f"{path}: cannot read boundary data: {error}") from None
    return [
        line.strip()
        for line in raw
        if line.strip() and not line.lstrip().startswith(_COMMENT)
    ]


def load_patterns(path: str = FORBIDDEN_NAMES, *, root: str = ROOT) -> List[Pattern]:
    """Load the banned expressions. An empty list is an error, not a pass.

    A guard whose data file was truncated or emptied would otherwise report a
    clean boundary for a repository that has none.
    """
    expressions = _data_lines(root, path)
    if not expressions:
        raise BoundaryError(f"{path}: the boundary data file declares no names")
    patterns: List[Pattern] = []
    for expression in expressions:
        try:
            patterns.append(Pattern(path, expression, re.compile(expression)))
        except re.error as error:
            raise BoundaryError(
                f"{path}: {expression!r} is not a regular expression: {error}"
            ) from None
    return patterns


def load_exceptions(path: str = EXCEPTIONS, *, root: str = ROOT) -> List[Exception_]:
    """Load the reviewed residual surface.

    Each line is ``verdict<TAB>expression<TAB>glob<TAB>reason``. The reason is
    mandatory and the verdict must be one of :data:`VERDICTS`, because "it was
    already there" is not a category this file accepts.
    """
    exceptions: List[Exception_] = []
    try:
        with open(_rooted(root, path), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        raise BoundaryError(f"{path}: cannot read boundary data: {error}") from None

    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(_COMMENT):
            continue
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) != _FIELDS:
            raise BoundaryError(
                f"{path}:{number}: expected verdict<TAB>name<TAB>glob<TAB>reason, "
                f"got {len(fields)} field(s)"
            )
        verdict, expression, glob, reason = fields
        if verdict not in VERDICTS:
            raise BoundaryError(
                f"{path}:{number}: verdict must be one of "
                + ", ".join(VERDICTS)
                + f", got {verdict!r}"
            )
        if not reason:
            raise BoundaryError(
                f"{path}:{number}: a residual name needs a reason; "
                "delete the name or record why it stays"
            )
        try:
            compiled = re.compile(expression)
        except re.error as error:
            raise BoundaryError(
                f"{path}:{number}: {expression!r} is not a regular expression: {error}"
            ) from None
        exceptions.append(Exception_(verdict, path, expression, glob, reason, compiled))
    return exceptions


def _is_reusable_workflow(text: str) -> bool:
    """Whether a workflow declares ``workflow_call``.

    Deliberately a two-line shape test rather than a YAML parse: the surface
    selection has to be readable at a glance in review, and a workflow that
    declares ``on:`` with ``workflow_call`` underneath it is the only shape
    GitHub accepts for a reusable workflow.
    """
    match = _REUSABLE_RE.search(text)
    if match is None:
        # ``on: workflow_call`` and ``on: [workflow_call]`` are accepted too.
        inline = re.search(r"^on:\s*(.+)$", text, re.M)
        if inline is None:
            return False
        return "workflow_call" in inline.group(1)
    start = match.end()
    lines = text[start:].splitlines()
    for line in lines:
        if line and not line[0].isspace():
            break
        if _WORKFLOW_CALL_RE.match(line):
            return True
    return False


def scanned_paths(root: str = ROOT) -> List[str]:
    """Every generic-surface file, relative to ``root`` and sorted.

    Workflows are selected by declaring ``workflow_call``; scripts and the
    composite action are taken whole, because a consumer's run reaches every
    script the reusable workflows call.
    """
    found: List[str] = []
    workflows = os.path.join(root, WORKFLOW_DIR)
    if os.path.isdir(workflows):
        for name in sorted(os.listdir(workflows)):
            if not name.endswith(SCAN_SUFFIXES[:2]):
                continue
            relative = os.path.join(WORKFLOW_DIR, name)
            with open(os.path.join(root, relative), encoding="utf-8") as handle:
                if _is_reusable_workflow(handle.read()):
                    found.append(relative)

    for directory in (SCRIPT_DIR, ACTION_DIR):
        base = os.path.join(root, directory)
        for current, directories, names in os.walk(base):
            directories[:] = sorted(d for d in directories if d != "__pycache__")
            for name in sorted(names):
                if name.endswith(SCAN_SUFFIXES):
                    found.append(
                        os.path.relpath(os.path.join(current, name), root)
                    )
    return sorted(found)


def _data_files() -> Tuple[str, ...]:
    return (FORBIDDEN_NAMES, EXCEPTIONS, CONTRACT_FORBIDDEN_NAMES)


def _is_data_file(relative_path: str) -> bool:
    normalized = relative_path.replace(os.sep, "/")
    return normalized in _data_files() or normalized.endswith("/" + FORBIDDEN_NAMES)


def scan(
    root: str = ROOT,
    patterns: Optional[Sequence[Pattern]] = None,
    exceptions: Optional[Sequence[Exception_]] = None,
) -> List[Violation]:
    """Return every banned name in the generic surface.

    The scan is line-oriented and literal: same repository, same patterns, same
    order, same answer. It deliberately does not try to understand YAML, shell,
    or Python, because a value baked into a string is exactly what it has to
    find, and a parser-aware scanner would answer "it is only in a comment".
    """
    patterns = list(patterns if patterns is not None else load_patterns(root=root))
    exceptions = list(
        exceptions if exceptions is not None else load_exceptions(root=root)
    )

    violations: List[Violation] = []
    for relative in scanned_paths(root):
        if _is_data_file(relative):
            continue
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            for number, line in enumerate(handle.read().splitlines(), start=1):
                for pattern in patterns:
                    if not pattern.regex.search(line):
                        continue
                    if any(e.matches(relative, line) for e in exceptions):
                        continue
                    violations.append(
                        Violation(
                            path=relative,
                            line_number=number,
                            line=line.strip(),
                            expression=pattern.expression,
                            source=pattern.source,
                        )
                    )
    return violations


def used_exceptions(
    root: str = ROOT,
    patterns: Optional[Sequence[Pattern]] = None,
    exceptions: Optional[Sequence[Exception_]] = None,
) -> Dict[str, int]:
    """Count how many banned lines each recorded residual actually covers."""
    patterns = list(patterns if patterns is not None else load_patterns(root=root))
    exceptions = list(
        exceptions if exceptions is not None else load_exceptions(root=root)
    )
    counts = {index: 0 for index in range(len(exceptions))}
    for relative in scanned_paths(root):
        if _is_data_file(relative):
            continue
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                for pattern in patterns:
                    if not pattern.regex.search(line):
                        continue
                    for index, exception in enumerate(exceptions):
                        if exception.matches(relative, line):
                            counts[index] += 1
                            break
    return counts


def stale_exceptions(
    root: str = ROOT,
    exceptions: Optional[Sequence[Exception_]] = None,
) -> List[Exception_]:
    """Recorded residuals that no longer match anything.

    A ledger that only grows is a ledger nobody reads. This is what makes
    "delete the name" a task the build can finish on its own.
    """
    exceptions = list(
        exceptions if exceptions is not None else load_exceptions(root=root)
    )
    counts = used_exceptions(root, exceptions=exceptions)
    return [
        exception
        for index, exception in enumerate(exceptions)
        if counts[index] == 0
    ]


def render_report(
    violations: Sequence[Violation], stale: Sequence[Exception_]
) -> str:
    """Render the report that accompanies every resolution, as CI wants it."""
    lines = ["## Consumer boundary", ""]
    if violations:
        lines.append("Consumer-specific values found in generic Continuum code:")
        lines.append("")
        for violation in violations:
            lines.append(
                f"- `{violation.path}:{violation.line_number}` matches "
                f"`{violation.expression}` ({violation.source})"
            )
        lines.append("")
        lines.append(
            "Resolve the value from `.github/continuum.yml`, repository `vars`, "
            "repository secrets, or the consumer's own scripts; or record it in "
            f"{EXCEPTIONS} with a verdict and a reason."
        )
    else:
        lines.append("No consumer-specific value is baked into the generic surface.")
    if stale:
        lines.append("")
        lines.append("Recorded residuals that no longer match anything:")
        for exception in stale:
            lines.append(
                f"- `{exception.verdict}` `{exception.expression}` "
                f"({exception.glob}) -- {exception.reason}"
            )
        lines.append("")
        lines.append(f"Delete the stale entries from {EXCEPTIONS}.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when generic Continuum code names a consumer.",
    )
    parser.add_argument(
        "--root",
        default=ROOT,
        help="repository root to scan (default: the checkout this file ships in)",
    )
    parser.add_argument(
        "--github-step-summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="file to append the boundary report to",
    )
    args = parser.parse_args(argv)

    try:
        violations = scan(args.root)
        stale = stale_exceptions(args.root)
    except BoundaryError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    report = render_report(violations, stale)
    if args.github_step_summary:
        with open(args.github_step_summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    sys.stdout.write(report)

    if violations or stale:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())