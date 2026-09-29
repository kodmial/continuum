"""Workflow credential hygiene.

A GitHub Actions workflow file is generated text: one layer writes
`GH_TOKEN: \\${{ secrets.X }}` and the escaping survives into the file the runner
actually reads. The runner then hands the evaluated secret to every step with a
stray leading backslash in front of it, and every authenticated API call fails
with 401 Bad credentials. The failure is invisible in the workflow definition and
expensive in production.

Two halves, both provider and consumer independent:

* :func:`sanitize_credential` removes the envelope escape at runtime, so a
  workflow that already ships the defect still authenticates.
* :func:`scan_workflows` detects the escaped expression statically and fails
  closed, so the envelope that produced it cannot regress silently.

Only credential fields are in scope. An escaped expression in a non-credential
field is not a credential defect, and failing the build on it would train people
to ignore this check.
"""

from __future__ import annotations

import pathlib
import re
from typing import Iterable, List, Optional, Sequence, Tuple

# `\${{` is the shape that survives into a generated file: the expression is
# written escaped, so the runner sees a literal backslash before `${{`.
ESCAPED_EXPRESSION_RE = re.compile(r"\\\$\{\{")

# A value that still contains `${{` never went through expression evaluation, so
# the credential is a literal string of template text. It is not a token and must
# never be sent, because a 401 from an unexpanded expression is indistinguishable
# from a 401 for a wrong token at the call site.
UNEXPANDED_EXPRESSION_RE = re.compile(r"\$\{\{")


class CredentialError(ValueError):
    """A credential that cannot be used, with the reason it cannot be used."""


def resolve_credential(value: Optional[str], *, name: str = "GH_TOKEN") -> str:
    """The credential to actually send, or a failure that says why.

    Fails closed on empty and on an unexpanded expression. Both are conditions
    where sending the value anyway would produce a bare 401 that looks identical
    to a revoked token, and the operator would go looking in the wrong place.
    """

    token = sanitize_credential(value)
    if not token:
        raise CredentialError(f"{name} is required")
    if UNEXPANDED_EXPRESSION_RE.search(token):
        raise CredentialError(f"{name} looks like an unexpanded GitHub expression")
    return token

# The credential fields a workflow can bind. A PAT is matched alongside the
# runner-provided tokens because a personal access token carries the same
# defect and the same 401 signature.
CREDENTIAL_KEYS: Tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "TAP_PAT",
    "GH_PAT",
    "GITHUB_PAT",
)
CREDENTIAL_KEY_RE = re.compile(r"\b(" + "|".join(CREDENTIAL_KEYS) + r")\b")

WORKFLOW_GLOB = "*.yml"

Finding = Tuple[str, int, str]


def sanitize_credential(value: Optional[str]) -> str:
    """Return the effective credential with one envelope-escape backslash removed.

    A leading backslash is never part of a real token; it is the residue of an
    escaped `${{ ... }}` expression. Exactly one is removed, because a genuine
    token never starts with one and a double escape means two independent
    defects that must both be fixed, not silently normalized away.
    """

    if not value:
        return ""
    text = str(value)
    if text.startswith("\\"):
        return text[1:]
    return text


def is_escaped_credential_line(line: str) -> bool:
    """True when one line binds a credential field to an escaped expression."""

    return bool(CREDENTIAL_KEY_RE.search(line) and ESCAPED_EXPRESSION_RE.search(line))


def scan_text(text: str) -> List[int]:
    """1-based line numbers of escaped credential expressions in `text`."""

    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if is_escaped_credential_line(line)
    ]


def scan_file(path: pathlib.Path) -> List[Finding]:
    """Scan one workflow file; return `(path, line, text)` findings."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [
        (str(path), number, line.strip())
        for number, line in enumerate(content.splitlines(), start=1)
        if is_escaped_credential_line(line)
    ]


def scan_workflows(root: pathlib.Path | str = ".") -> List[Finding]:
    """Scan every production workflow under `root`.

    Scans `.github/workflows/*.yml` and `.github/workflows/*.yaml`, plus the same
    directory inside a consumer fixture, because a consumer that copies the
    generated envelope carries the identical defect and is the place it is most
    likely to persist unnoticed.
    """

    base = pathlib.Path(root)
    roots: Iterable[pathlib.Path] = (base, base / "fixtures" / "consumer-repo")
    findings: List[Finding] = []
    for candidate in roots:
        workflows = candidate / ".github" / "workflows"
        if not workflows.is_dir():
            continue
        for path in sorted(workflows.glob(WORKFLOW_GLOB)) + sorted(
            workflows.glob("*.yaml")
        ):
            findings.extend(scan_file(path))
    return findings


def render(findings: Sequence[Finding]) -> List[str]:
    """GitHub Actions annotations for each finding."""

    return [
        f"::error file={path},line={line}::Escaped GitHub expression in a credential "
        f"field: {text}"
        for path, line, text in findings
    ]


def describe(findings: Sequence[Finding]) -> str:
    """One-line summary of a scan result."""

    count = len(findings)
    if not count:
        return "OK: no escaped GitHub expressions in workflow credential fields"
    noun = "expression" if count == 1 else "expressions"
    return f"FAIL: {count} escaped GitHub {noun} in workflow credential fields"
