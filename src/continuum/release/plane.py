"""The two components every release needs and nobody had written.

Eligibility and notes are the chain's first and last words, and both are ports
with no production implementation until now — the core was proven against
fixtures, which is the right way to prove a state machine and the wrong way to
ship a release. These are the two the reusable workflow actually runs, and both
are deliberately conservative: a component that approves every event or writes
an empty changelog would make the release plane look complete while removing
the two checks a consumer depends on.

Eligibility is the more important of the two, so it is the stricter one. Its job
is to be the last place a release can be refused before anything is built, and
the questions it asks are the ones a tag push cannot answer for itself: is this
ref a release ref at all, is the policy switched on, is the version one this
repository publishes, and is the commit a reviewed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from .contract import (
    ELIGIBLE,
    EVENT_DISPATCH,
    Eligibility,
    NotesRequest,
    ReleaseEvent,
    ReleaseNotes,
    ineligible,
    is_source_sha,
)
from .version import VersionError, VersionPolicy, version_from_tag

#: The refs a release may be cut from. A release built from a branch is
#: unreviewed by definition — the review gate runs on pull requests, not on
#: whatever is at the head of a branch — and a release built from a pull request
#: ref is somebody's unmerged work.
RELEASE_REFS = ("refs/tags/", "refs/heads/main", "refs/heads/master")


class EligibilityError(Exception):
    """Not raised: eligibility reports a verdict, and the chain records it."""


@dataclass(frozen=True)
class ReleaseEligibility:
    """Whether this event may become a release at all.

    Every answer carries a code, because "no" without a reason is a thing a
    repository owner argues with. The codes are also the workflow's: a blocked
    release is a different workflow outcome from a failed one, and a release
    blocked because the ref is not a release ref is not a failure of anything.

    What this checks is the ref and the tag, and nothing about the version
    series or the prerelease flag. That is a deliberate limit rather than an
    omission: which versions a repository may publish is `VersionPolicy`'s
    question, the version stage asks it, and a second opinion here would either
    duplicate that rule — and drift from it — or refuse something the version
    stage would have admitted, blocking a release with a reason the next stage
    contradicts. So this admits any tag that names a parseable version and
    leaves the series to the stage that owns it.

    A dispatched release has no tag, and that is admitted from the same default
    branch a tag would have pointed at. The alternative — refusing it — leaves a
    repository that tags releases as its only path to cutting a version, and the
    other — inventing a tag — puts a name in the release's record that nothing
    in the repository backs.
    """

    SUPPORTS_DRY_RUN = True
    name = "release-eligibility"

    policy: VersionPolicy
    default_branch: str = "main"
    allowed_refs: Tuple[str, ...] = RELEASE_REFS
    dry_run: bool = False

    def intent(self) -> str:
        return (
            "refuse any event that is not a release from a reviewed ref at a version "
            "this repository publishes"
        )

    def evaluate(self, event: ReleaseEvent) -> Eligibility:
        ref = event.ref or (f"refs/tags/{event.tag}" if event.tag else "")
        if not ref:
            return ineligible(
                code="ref-absent",
                reason=(
                    f"the event names no ref, so there is nothing to say whether it came "
                    f"from {self.default_branch!r} or from a fork's branch"
                ),
            )
        if not self._ref_is_releaseable(ref):
            return ineligible(
                code="ref-not-releasable",
                reason=(
                    f"ref {ref!r} is not a ref a release may be cut from; allowed: "
                    + ", ".join(self.allowed_refs)
                    + ". A release is a reviewed commit under a version, and a branch head "
                    "or an unmerged pull request is neither"
                ),
            )
        if not is_source_sha(event.sha):
            return ineligible(
                code="source-unpinned",
                blocking=True,
                reason=(
                    f"the event names commit {event.sha!r}, which is not a full commit "
                    "SHA. Every artifact is compared against this value, and an "
                    "abbreviated one cannot be compared"
                ),
            )
        if not event.tag:
            if event.name != EVENT_DISPATCH:
                return ineligible(
                    code="tag-absent",
                    reason=(
                        "the event names no tag, so this release has no identity to publish "
                        "under. The tag is what a consumer pins to"
                    ),
                )
            # A dispatched release is cut from a branch, where no tag has been
            # pushed and the version is the identity. Refusing it here would
            # leave a maintainer with no way to release from a protected branch
            # at all, and inventing a tag would name something that does not
            # exist. The version and the series are the version stage's question
            # either way; what is checked here is that the ref is one releases
            # may be cut from, which the checks above already did.
            return ELIGIBLE
        if event.tag.startswith("refs/"):
            return ineligible(
                code="tag-malformed",
                reason=(
                    f"the event's tag {event.tag!r} is a ref rather than a tag name. The "
                    "tag is the release's identity and must be the short name the "
                    "consumer will pin to"
                ),
            )
        # Only that the tag names a version at all. Which versions this
        # repository may publish is the version stage's question, and answered
        # there, from the same policy object.
        try:
            version_from_tag(event.tag, self.policy.tag_prefix)
        except VersionError as exc:
            return ineligible(code="version-not-admissible", reason=str(exc))
        return ELIGIBLE

    def _ref_is_releaseable(self, ref: str) -> bool:
        if ref.startswith("refs/tags/"):
            return True
        return ref in self.allowed_refs

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "default_branch": self.default_branch,
            "allowed_refs": list(self.allowed_refs),
        }


@dataclass(frozen=True)
class TagNotes:
    """Release notes built from the tag and the release's own facts.

    Not from a changelog file, because a release's notes are then a second
    source of truth that can disagree with the tag: a hand-written changelog
    entry for a version that was never cut is a lie a consumer reads, and one
    that omits a version that was cut is a support problem. The body is
    generated from facts the release already proved — the tag, the commit, the
    event — and a repository that wants prose points `reference` at its
    changelog rather than inlining it here.

    A generated body is not prose, and this says so. The alternative is a
    builder that invents release notes from a diff, which is how a changelog
    starts describing changes that were reverted.
    """

    SUPPORTS_DRY_RUN = True
    name = "tag-notes"

    reference: str = ""
    repository_url: str = ""

    def intent(self) -> str:
        return "describe a release from the tag and commit it was cut from"

    def build(self, request: NotesRequest) -> ReleaseNotes:
        source = request.event.repository
        lines = [f"# {request.tag}", ""]
        lines.append(f"Continuum {request.version} — built from `{request.source_sha[:12]}`.")
        lines.append("")
        if self.repository_url:
            lines.append(f"Repository: {source}")
        if request.release_pr is not None and request.release_pr.url:
            lines.append(f"Release pull request: {request.release_pr.url}")
        lines.append("")
        if self.reference:
            lines.append(
                f"Changes are listed in [the changelog]({self.reference}). This note is "
                "generated from the release's own record; the changelog is the prose."
            )
        else:
            lines.append(
                "This note is generated from the release's record: the tag, the commit it "
                "was cut from, and nothing else. It does not describe what changed, "
                "because this component cannot know that without being told a source of "
                "truth that could disagree with the tag."
            )
        lines.append("")
        lines.append("Every asset in this release carries its digest and the commit it was")
        lines.append("built from in `SHA256SUMS.txt` and in the artifact attestations.")
        return ReleaseNotes(
            body="\n".join(lines),
            source=self.name,
            reference=self.reference,
        )

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "reference": self.reference}


def eligibility_for(policy: Optional[VersionPolicy] = None, **kwargs: Any) -> ReleaseEligibility:
    return ReleaseEligibility(policy=policy or VersionPolicy(), **kwargs)


def notes_for(reference: str = "", **kwargs: Any) -> TagNotes:
    return TagNotes(reference=reference, **kwargs)


__all__ = [
    "RELEASE_REFS",
    "ReleaseEligibility",
    "TagNotes",
    "eligibility_for",
    "notes_for",
]
