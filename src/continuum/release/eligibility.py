"""Release eligibility and release notes: the two policy inputs of the chain.

Both are *policy*, not mechanics, and both are pure functions of the event plus
repository-controlled configuration. That is deliberate: the two questions a
release asks before it builds anything are "may this event become a release at
all" and "what does the release say about itself", and both must be answerable
from a document in a test with no network and no clock.

The eligibility rules mirror what a real repository actually does, rather than an
idealised version of it:

* a change is releasable only on the default branch, or from a tag pushed for a
  version the branch already carries;
* an explicit `skip-release` marker in the commit message, an explicit dispatch
  input, or a `[skip release]` convention all mean "do not release this";
* a change that touched only non-releasable files (documentation, CI, the release
  configuration itself) is not a release, and it must be a *green* no-op rather
  than a failure, because otherwise every documentation pull request turns the
  release workflow red;
* an input the policy cannot read is *blocking* instead of ineligible, because
  "we could not tell" and "the answer is no" must never be the same value.

Notes are the other half of the same principle. A release section is looked up by
the *agreed* version, so a changelog that stopped at the previous version fails
here instead of publishing a release whose notes describe something else.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, Optional, Sequence, Tuple

from .contract import (
    ContractError,
    Eligibility,
    ReleaseEvent,
    ReleaseNotes,
    NotesRequest,
    ineligible,
)

ELIGIBILITY_NAME = "release-eligibility"
NOTES_NAME = "changelog-notes"

#: The markers a commit message may carry to switch a release off. They are
#: matched case-insensitively as whole tokens, so `skip-release-notes` does not
#: silently suppress a release and `[skip release]` does.
DEFAULT_SKIP_MARKERS: Tuple[str, ...] = ("skip-release", "[skip release]", "[release skip]")

#: Files whose change does not, by itself, justify a release. They are patterns,
#: not paths, so a repository can say `docs/**` once. The default is deliberately
#: conservative: a change to code is releasable, a change to prose or to the
#: release plumbing is not.
DEFAULT_NONRELEASABLE_PATHS: Tuple[str, ...] = (
    "docs/**",
    "*.md",
    ".github/**",
    "LICENSE",
    "SECURITY.md",
)

#: Events an application release may be cut from. A `pull_request` event is
#: deliberately absent: a release job that runs on a pull request is a release
#: job that can read credentials from an unreviewed diff.
DEFAULT_EVENTS: Tuple[str, ...] = (
    "push",
    "tag-push",
    "workflow-dispatch",
    "release-pr-merged",
    "scheduled",
)


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _split_paths(value: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[\n,]", value or "") if part.strip())


def _match_any(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
            return True
    return False


def _mentions_marker(text: str, markers: Sequence[str]) -> Optional[str]:
    lowered = (text or "").lower()
    for marker in markers:
        candidate = marker.lower()
        # Whole-token matching, not substring: `skip-release-notes` names a
        # tool, and treating it as a request to skip the release would make a
        # typo silently suppress a release.
        pattern = re.compile(
            r"(?<![A-Za-z0-9_-])" + re.escape(candidate) + r"(?![A-Za-z0-9_-])"
        )
        if pattern.search(lowered):
            return marker
    return None


class ReleaseEligibility:
    """Whether an event may become a release, decided from its own facts.

    The component reads the event's *attributes* rather than the provider
    payload. A caller normalises the payload once (`branch`, `commit_message`,
    `changed_files`, `skip_release`) and the policy stays testable and provider
    independent, which is also what keeps a raw event body out of the decision.
    """

    SUPPORTS_DRY_RUN = True
    name = ELIGIBILITY_NAME

    def __init__(
        self,
        *,
        default_branch: str = "",
        events: Sequence[str] = DEFAULT_EVENTS,
        skip_markers: Sequence[str] = DEFAULT_SKIP_MARKERS,
        nonreleasable_paths: Sequence[str] = DEFAULT_NONRELEASABLE_PATHS,
        require_default_branch: bool = True,
    ) -> None:
        self._default_branch = default_branch
        self._events = tuple(events)
        self._skip_markers = tuple(skip_markers)
        self._nonreleasable = tuple(nonreleasable_paths)
        self._require_default_branch = require_default_branch

    def intent(self) -> str:
        return (
            "allow a release only for an allowed event on the default branch, and only "
            "when the change is not marked skip-release and touches a releasable file"
        )

    def evaluate(self, event: ReleaseEvent) -> Eligibility:
        if event.name not in self._events:
            return ineligible(
                "event-not-allowed",
                f"a {event.name} event is not configured to cut a release",
            )
        if _truthy(event.attribute("skip-release")):
            return ineligible(
                "skip-release",
                "this run asked not to release",
            )
        marker = _mentions_marker(event.attribute("commit_message"), self._skip_markers)
        if marker:
            return ineligible(
                "skip-release",
                f"the commit message carries {marker!r}, which switches the release off",
            )
        if event.name in ("push", "release-pr-merged", "workflow-dispatch", "scheduled"):
            branch = event.attribute("branch") or event.default_branch
            if not branch:
                return ineligible(
                    "eligibility-unavailable",
                    "the event does not say which branch it is about, so branch policy "
                    "cannot be applied",
                    blocking=True,
                )
            if self._require_default_branch and branch != (
                self._default_branch or event.default_branch
            ):
                return ineligible(
                    "not-default-branch",
                    f"{branch!r} is not the release branch "
                    f"({self._default_branch or event.default_branch!r})",
                )
        unavailable = _truthy(event.attribute("changed-files-unavailable"))
        changed = _split_paths(event.attribute("changed_files"))
        if unavailable:
            return ineligible(
                "eligibility-unavailable",
                "the changed-file list could not be read, so releasability cannot be "
                "decided; refusing to guess",
                blocking=True,
            )
        if changed:
            releasable = [
                path for path in changed if not _match_any(path, self._nonreleasable)
            ]
            if not releasable:
                return ineligible(
                    "no-releasable-change",
                    "this change touched only non-releasable files ("
                    + ", ".join(sorted(changed))
                    + ")",
                )
        return Eligibility(eligible=True)


CHANGELOG_NAME = NOTES_NAME


class ChangelogNotes:
    """Release notes drawn from the changelog section for the agreed version.

    The version is a parameter rather than something the notes component reads
    for itself: the core agrees the version first, and notes that describe a
    different number are the failure this component exists to prevent. A section
    that exists but is empty is refused for the same reason a missing one is —
    a published release body is what the person upgrading reads at 2am.
    """

    SUPPORTS_DRY_RUN = True
    name = NOTES_NAME

    def __init__(
        self,
        *,
        path: str = "CHANGELOG.md",
        fallback_notes: str = "",
        require_version_in_fallback: bool = True,
    ) -> None:
        self._path = path
        self._fallback = fallback_notes or ""
        self._require_version_in_fallback = require_version_in_fallback

    def intent(self) -> str:
        return f"write the release notes from {self._path}, or from the release body the event carried"

    def _read(self, workdir: str) -> str:
        candidates = [self._path]
        if workdir:
            candidates.insert(0, os.path.join(workdir, self._path))
        for candidate in candidates:
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as handle:
                    return handle.read()
        raise ContractError(
            f"{self._path} is not readable, so the release notes cannot be built. A "
            "release with no notes is a support problem discovered by the person who "
            "needed the note.",
            code="notes-source-missing",
        )

    @staticmethod
    def section(text: str, version: str) -> str:
        """The changelog section for ``version``, without its heading."""

        heading = re.compile(
            r"^#{1,6}\s*(?:\[)?" + re.escape(version) + r"(?:\])?[^\n]*$",
            re.MULTILINE,
        )
        match = heading.search(text or "")
        if match is None:
            return ""
        remainder = text[match.end() :]
        following = re.search(r"^#{1,6}\s", remainder, re.MULTILINE)
        body = remainder[: following.start()] if following else remainder
        return body.strip()

    def build(self, request: NotesRequest) -> ReleaseNotes:
        fallback = self._fallback.strip()
        if fallback:
            if self._require_version_in_fallback and request.version not in fallback:
                raise ContractError(
                    f"the release body this event carried does not mention "
                    f"{request.version}, so publishing it would describe a different "
                    "release",
                    code="notes-version-mismatch",
                )
            return ReleaseNotes(
                body=fallback, source="event", reference=request.tag
            )
        section = self.section(self._read(request.workdir if hasattr(request, "workdir") else ""), request.version)
        if not section:
            raise ContractError(
                f"{self._path} has no section for {request.version}. Cutting the release "
                "would publish notes for whatever section came last.",
                code="notes-version-missing",
            )
        return ReleaseNotes(
            body=section, source=self._path, reference=request.tag
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self._path,
            "fallback": bool(self._fallback),
        }


__all__ = [
    "CHANGELOG_NAME",
    "ChangelogNotes",
    "DEFAULT_EVENTS",
    "DEFAULT_NONRELEASABLE_PATHS",
    "DEFAULT_SKIP_MARKERS",
    "ELIGIBILITY_NAME",
    "NOTES_NAME",
    "ReleaseEligibility",
    "_match_any",
    "_mentions_marker",
]
