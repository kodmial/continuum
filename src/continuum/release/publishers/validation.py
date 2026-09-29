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
  error. "Present" is a *token* claim, not a substring one: `0.1.1` occurs
  inside `0.1.10`, and a manifest pinned to the wrong release satisfies a
  substring check for the right one.
* **A placeholder was left behind in a line nobody executes.** Generated files
  explain themselves, so the comments that document the tokens a template fills
  legitimately spell them out. Only a live line can carry an unfilled token, so
  the scan ignores comment lines instead of failing on the documentation of its
  own placeholders.
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
from .template import TOKEN_RE

CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_UNAVAILABLE = "unavailable"
CHECK_SKIPPED = "skipped"

_DIGEST_RE = re.compile(r"\b[0-9a-f]{64}\b")

# A generated file explains its own tokens, so a comment line routinely spells
# out `__SHA256__` and friends. Scanning the whole file for an unfilled token
# would fail on the documentation of the placeholders rather than on an unfilled
# one, which is how a correct file gets a red build.
_COMMENT_PREFIXES = ("#", "//", ";", "--")

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


def is_comment_line(line: str) -> bool:
    """Whether a line of a generated file says nothing executable."""

    stripped = (line or "").lstrip()
    return any(stripped.startswith(prefix) for prefix in _COMMENT_PREFIXES)


def active_lines(content: str) -> List[str]:
    """The lines of a generated file that a package manager would execute."""

    return [line for line in (content or "").splitlines() if not is_comment_line(line)]


def names_version(content: str, version: str) -> bool:
    """Whether `content` names `version` as a complete token.

    A substring test is not enough, and the failure it hides is a silent one:
    `0.1.1` is a substring of `0.1.10`, so a manifest left pinned to the wrong
    release satisfies a check written for the right one. Versions are therefore
    compared on a boundary, so only the version itself -- and not a longer
    version that starts or ends with it -- counts.

    A leading `v` is accepted, because `v0.1.1` is the conventional way to write
    a tag and is a real mention of `0.1.1`. Any other adjacent character is not:
    the boundary is deliberately the same set of characters that can appear
    inside a version, plus letters, so a match cannot be produced by
    neighbouring text.
    """

    token = (version or "").strip()
    if not token:
        return False
    text = content or ""
    for candidate in (token, f"v{token}", f"V{token}"):
        pattern = re.compile(
            r"(?<![0-9A-Za-z._+-])" + re.escape(candidate) + r"(?![0-9A-Za-z._+-])"
        )
        if pattern.search(text) is not None:
            return True
    return False


def unfilled_template_tokens(content: str) -> Tuple[str, ...]:
    """Tokens left unfilled on a line that is actually executed.

    Comment lines are excluded: a generated file is expected to document the
    tokens it fills, and failing on that documentation would make the check
    report the wrong thing.
    """

    found: List[str] = []
    for line in active_lines(content):
        for name in TOKEN_RE.findall(line):
            if name not in found:
                found.append(name)
    return tuple(found)


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
        versioned = names_version(generated.content, release.version) or names_version(
            generated.content, release.tag
        )
        checks.append(
            CheckOutcome(
                name="version-present",
                status=CHECK_PASSED if versioned else CHECK_FAILED,
                detail=(
                    f"the file names release {release.version}"
                    if versioned
                    else f"the file never names release {release.version} as a complete "
                    "version, so it would describe whatever was published before it"
                ),
            )
        )

    leftover = unfilled_template_tokens(generated.content)
    checks.append(
        CheckOutcome(
            name="template-tokens-filled",
            status=CHECK_PASSED if not leftover else CHECK_FAILED,
            detail=(
                "every template token on an executed line was filled in"
                if not leftover
                else f"executable line(s) still contain unfilled template token(s) "
                f"{', '.join(leftover)}"
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
    "active_lines",
    "audit",
    "combine",
    "discard_scratch",
    "is_comment_line",
    "names_version",
    "raise_for",
    "scratch_directory",
    "syntax_check",
    "unfilled_template_tokens",
    "write_for_check",
]
