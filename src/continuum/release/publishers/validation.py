"""Checking a generated manifest before anything writes it to a destination.

A manifest that is wrong is not fixed by being published quickly. Every check
here runs *before* the write, and every one of them exists because of a specific
way a generated manifest can be wrong while still looking complete:

* **The digest came from nowhere.** A generated file is scanned for every
  64-character hex string and each one must be a digest the release manifest
  actually records. A formula that pins a checksum for an artifact the release
  never published installs on the machine that has it and fails on every other
  one, and the CI that generated it reported success.
* **The digest is real but belongs to a different artifact.** Requiring at least
  one digest *from the artifacts this surface describes* is what keeps a cask
  from silently inheriting the formula's checksums. The two describe different
  downloads, and a cask that points at the archive's digest is a cask that fails
  the moment a user runs it.
* **The version is missing.** A formula whose `version` line was never filled in
  describes the previous release, which is a silent downgrade rather than an
  error.
* **It does not parse.** A syntax check through a real interpreter, because the
  structural audits read text and text does not care whether the file is
  loadable. It is reported as `unavailable` rather than `passed` when the
  interpreter is not on the runner, and a caller that needs it says so.

Checks that need a tool report three states, not two. A check that could not run
is not a check that passed, and a report that cannot tell those apart is a
report that will eventually claim a manifest was validated on a runner where
nothing was.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contract import (
    VALIDATION_FAILED,
    VALIDATION_UNAVAILABLE,
    ArtifactManifest,
    PublishedArtifact,
    PublisherError,
)
from .repository import CommandResult, CommandRunner, subprocess_runner

CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_UNAVAILABLE = "unavailable"
CHECK_SKIPPED = "skipped"

_DIGEST_RE = re.compile(r"\b[0-9a-f]{64}\b")

# Where a generated file is written so a parser can read it. Under the system
# temporary directory rather than the working tree, so validating a manifest
# cannot leave a stray file in a repository that is about to be committed to.
_SCRATCH_PREFIX = "continuum-publisher-check"


@dataclass(frozen=True)
class CheckOutcome:
    """One check's verdict, and the sentence a human reads about it."""

    name: str
    status: str
    detail: str = ""
    required: bool = True

    @property
    def ok(self) -> bool:
        return self.status in (CHECK_PASSED, CHECK_SKIPPED)

    @property
    def blocking(self) -> bool:
        return not self.ok and self.required

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Every check that ran against one generated file, and the verdict."""

    surface: str
    path: str
    checks: Tuple[CheckOutcome, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(check.blocking for check in self.checks)

    @property
    def reason(self) -> str:
        if self.ok:
            return CHECK_PASSED
        if any(check.status == CHECK_UNAVAILABLE and check.required for check in self.checks):
            return VALIDATION_UNAVAILABLE
        return VALIDATION_FAILED

    def blocking(self) -> Tuple[CheckOutcome, ...]:
        return tuple(check for check in self.checks if check.blocking)

    def describe(self) -> Dict[str, Any]:
        return {
            "surface": self.surface,
            "path": self.path,
            "ok": self.ok,
            "reason": self.reason,
            "checks": [check.describe() for check in self.checks],
        }


def combine(*reports: Optional[ValidationReport]) -> ValidationReport:
    """One report per file, however many layers of checking produced it."""

    kept = [report for report in reports if report is not None and report.checks]
    if not kept:
        return ValidationReport(surface="", path="", checks=())
    checks: List[CheckOutcome] = []
    for report in kept:
        checks.extend(report.checks)
    return ValidationReport(
        surface=kept[0].surface, path=kept[0].path, checks=tuple(checks)
    )


def raise_for(report: ValidationReport) -> None:
    """Turn a failed report into the publisher error it stands for."""

    if report.ok:
        return
    detail = "; ".join(f"{check.name}: {check.detail}" for check in report.blocking())
    unavailable = report.reason == VALIDATION_UNAVAILABLE
    raise PublisherError(
        report.reason,
        f"{report.path} did not pass local validation ({detail})",
        retryable=False,
        remediation=(
            "run the same checks on a runner that has the package manager's "
            "toolchain, fix the template or the settings, and re-run the publish"
            if unavailable
            else "the generated manifest is wrong; fix the template or the settings "
            "and re-run the publish. Nothing was written to the destination."
        ),
    )


@dataclass(frozen=True)
class GeneratedFile:
    """One file a publisher is about to write, and the facts that justify it."""

    path: str
    content: str
    surface: str
    selected: Tuple[PublishedArtifact, ...] = ()
    # Consumer-owned assertions. The publisher does not know what a given
    # formula must say about its service label or its app bundle; the consumer
    # does, and says so here rather than by editing a package manager module.
    required_text: Tuple[str, ...] = ()
    forbidden_text: Tuple[str, ...] = ()


def audit(
    generated: GeneratedFile,
    manifest: ArtifactManifest,
    *,
    require_version: bool = True,
) -> ValidationReport:
    """The checks that need no toolchain at all.

    Every one of these is a check on *content*, which is why they can run on any
    runner: CI does not need a tap's toolchain to know that the checksum in a
    formula it just generated is not a checksum the release published.
    """

    checks: List[CheckOutcome] = []
    release = manifest.release
    known = set(manifest.digests())
    found = set(_DIGEST_RE.findall(generated.content))

    unknown = sorted(found - known)
    checks.append(
        CheckOutcome(
            name="digest-membership",
            status=CHECK_PASSED if not unknown else CHECK_FAILED,
            detail=(
                "every checksum in the file is one the release manifest records"
                if not unknown
                else f"checksum(s) {', '.join(unknown)} are not in the release manifest"
            ),
        )
    )

    selected = {artifact.sha256 for artifact in generated.selected}
    pins_selected = bool(selected & found)
    checks.append(
        CheckOutcome(
            name="digest-selected",
            status=CHECK_PASSED if (not selected or pins_selected) else CHECK_FAILED,
            detail=(
                "the file pins a checksum of an artifact this surface describes"
                if not selected or pins_selected
                else "the file pins no checksum belonging to the artifacts this surface "
                "describes; it is describing something the release did not publish"
            ),
        )
    )

    if require_version:
        versioned = release.version in generated.content or release.tag in generated.content
        checks.append(
            CheckOutcome(
                name="version-present",
                status=CHECK_PASSED if versioned else CHECK_FAILED,
                detail=(
                    f"the file names release {release.version}"
                    if versioned
                    else f"the file never names release {release.version}, so it would "
                    "describe whatever was published before it"
                ),
            )
        )

    unfilled = unfilled_template_tokens(generated.content)
    checks.append(
        CheckOutcome(
            name="template-tokens-filled",
            status=CHECK_PASSED if not unfilled else CHECK_FAILED,
            detail=(
                "no unfilled template token remains outside the file's comments"
                if not unfilled
                else "unfilled template token(s) {} remain in executable lines, so the "
                "file describes a value the generator never supplied".format(
                    ", ".join(unfilled)
                )
            ),
        )
    )

    for text in generated.required_text:
        present = text in generated.content
        checks.append(
            CheckOutcome(
                name="required-text",
                status=CHECK_PASSED if present else CHECK_FAILED,
                detail=(
                    f"required content {text!r} is present"
                    if present
                    else f"required content {text!r} is missing"
                ),
            )
        )

    for text in generated.forbidden_text:
        absent = text not in generated.content
        checks.append(
            CheckOutcome(
                name="forbidden-text",
                status=CHECK_PASSED if absent else CHECK_FAILED,
                detail=(
                    f"forbidden content {text!r} is absent"
                    if absent
                    else f"forbidden content {text!r} is present"
                ),
            )
        )
    return ValidationReport(surface=generated.surface, path=generated.path, checks=tuple(checks))


#: A template token: two underscores, a name, two underscores. This is the shape a
#: generator leaves behind when it wrote the template rather than a rendered value,
#: and it is deliberately loose about the name so a newly added token is caught by
#: the same check rather than needing its own pattern.
_TEMPLATE_TOKEN_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")

#: Comment introducers, per syntax. A generated package-manager file legitimately
#: carries explanatory comments that *mention* the token names -- "the generator
#: fills __SHA256__" is documentation, not an unfilled value -- so a scanner that
#: does not know about comments reports those files as broken and gets ignored,
#: which costs it the one signal it exists to give.
_COMMENT_PREFIXES = ("#", "//", "--", ";")


def _comment_start(line: str) -> int:
    """Where the trailing comment on `line` begins, or ``len(line)`` if it has none.

    The subtlety is that the introducers are also ordinary punctuation: ``//``
    separates a path from a version and ``--`` begins a command-line flag. A
    scanner that treated either as a comment introducer would delete live text, and
    deleting live text here means missing an unfilled token -- the failure this
    whole check exists to prevent.

    So an introducer has to be whitespace-delimited on both sides and sit outside
    any quoted string, which is what separates ``url "https://x/y"`` from
    ``url "https://x/y"  # __SHA256__ unfilled``. Whole-line comments are handled
    by the caller, which needs to keep the ``str.startswith`` form readable.
    """

    quote = ""
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        for prefix in _COMMENT_PREFIXES:
            if not line.startswith(prefix, index):
                continue
            before = line[index - 1] if index else ""
            after = line[index + len(prefix) :][:1]
            if before.isspace() and (after == "" or after.isspace()):
                return index
        index += 1
    return length


def unfilled_template_tokens(content: str) -> List[str]:
    """Template tokens left unfilled in the *executable* lines of a generated file.

    A generated file keeps its explanatory comments, and those comments mention the
    tokens by name. Only text outside a comment can carry a value the generator
    failed to supply: a `version "1.2.3" # was __VERSION__` line installs
    correctly, while a bare `version "__VERSION__"` installs the previous release,
    quietly.

    Both a whole-line comment and a trailing one are skipped, because generated
    manifests document themselves inline and a scanner that only knew about the
    first kind would report every documented token as unfilled.

    Order is stable and duplicates are collapsed, so a report of what is unfilled is
    the same set whatever order the file is read in.
    """

    active: List[str] = []
    for line in (content or "").splitlines():
        if line.strip().startswith(_COMMENT_PREFIXES):
            continue
        active.extend(
            match.group(0)
            for match in _TEMPLATE_TOKEN_RE.finditer(line[: _comment_start(line)])
        )
    return sorted(set(active))


def scratch_directory(surface: str) -> str:
    return os.path.join(tempfile.gettempdir(), _SCRATCH_PREFIX, surface or "file")


def write_for_check(generated: GeneratedFile) -> str:
    """Put the generated content on disk so a parser can read it by path."""

    workdir = scratch_directory(generated.surface)
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, os.path.basename(generated.path))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(generated.content)
    return path


def discard_scratch(surface: str) -> None:
    shutil.rmtree(scratch_directory(surface), ignore_errors=True)


def syntax_check(
    generated: GeneratedFile,
    *,
    argv: Sequence[str],
    command_runner: Optional[CommandRunner] = None,
    required: bool = True,
) -> ValidationReport:
    """Run a real parser over a generated file.

    `argv` names the tool and its flags and this module appends the file's path,
    so it neither knows nor cares which parser it is. The content is written to
    a scratch path first, because a parser reads a path and a caller that only
    holds the bytes has no other way to be checked by one.
    """

    runner = command_runner or subprocess_runner
    path = write_for_check(generated)
    workdir = scratch_directory(generated.surface)
    try:
        result: CommandResult = runner(list(argv) + [path], workdir, {})
    except (FileNotFoundError, OSError) as exc:
        return ValidationReport(
            surface=generated.surface,
            path=generated.path,
            checks=(
                CheckOutcome(
                    name="syntax",
                    status=CHECK_UNAVAILABLE if required else CHECK_SKIPPED,
                    detail=f"{argv[0]} is not available on this runner: {exc}",
                    required=required,
                ),
            ),
        )
    if result.code != 0:
        return ValidationReport(
            surface=generated.surface,
            path=generated.path,
            checks=(
                CheckOutcome(
                    name="syntax",
                    status=CHECK_FAILED,
                    detail=f"{argv[0]} rejected the file: {result.output.strip()[:400]}",
                    required=required,
                ),
            ),
        )
    return ValidationReport(
        surface=generated.surface,
        path=generated.path,
        checks=(
            CheckOutcome(
                name="syntax", status=CHECK_PASSED, detail=f"{argv[0]} parsed the file"
            ),
        ),
    )


__all__ = [
    "CHECK_FAILED",
    "CHECK_PASSED",
    "CHECK_SKIPPED",
    "CHECK_UNAVAILABLE",
    "CheckOutcome",
    "GeneratedFile",
    "ValidationReport",
    "audit",
    "combine",
    "discard_scratch",
    "raise_for",
    "scratch_directory",
    "syntax_check",
    "unfilled_template_tokens",
    "write_for_check",
]
