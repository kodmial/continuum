"""Deterministic delegated Child pull-request description builder.

The delegated Child worker already produces a runner-local result file and a
verified branch diff before publication.  This module turns that existing
evidence into the public Child PR body instead of static boilerplate.  It does
not invoke a model, read the Parent repository, or publish anything itself.

Trust boundaries:

* Issue title/body and the agent result are untrusted prompt-adjacent text.
  They are escaped so they can never forge the hidden snapshot/acceptance
  markers that the trusted publisher appends afterwards.
* The task snapshot reference is produced by the canonical snapshot engine and
  is validated, then preserved byte-for-byte on its own line.
* When a task explicitly requires before/after description examples and no
  source-backed evidence exists, the body carries an explicit blocking marker
  instead of pretending the description is complete.

Standard library only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

ACCEPTED_MARKER = "<!-- continuum-child-accepted -->"
BLOCKED_MARKER = "<!-- continuum-child-pr-description-blocked -->"
DESCRIPTION_MARKER_PREFIX = "<!-- continuum-child-pr-description:v1 sha256="
SNAPSHOT_REF_RE = re.compile(
    r"<!--\s*continuum-task-snapshot-ref\s+"
    r"repo=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+"
    r"issue=(\d+)\s+"
    r"generation=(\d+)\s+"
    r"title-sha256=([0-9a-fA-F]{64})\s+"
    r"body-sha256=([0-9a-fA-F]{64})\s+"
    r"spec-sha256=([0-9a-fA-F]{64})\s*-->"
)
DESCRIPTION_MARKER_RE = re.compile(
    r"<!--\s*continuum-child-pr-description:v1\s+sha256=([0-9a-f]{64})\s*-->"
)
BEFORE_AFTER_RE = re.compile(
    r"\bbefore\s*(?:/|\\|and|&|vs\.?|versus)\s*after\b|\bbefore-after\b",
    re.IGNORECASE,
)
PR_DESCRIPTION_RE = re.compile(
    r"\b(?:pr|pull[ -]?request)\s+(?:description|body)\b|"
    r"\bdescription\s+examples?\b|\bbefore\s*/\s*after\b",
    re.IGNORECASE,
)

MAX_SECTION_CHARS = 8000
MAX_DIFF_LINES = 80
MAX_DIFF_FILES = 8


class ChildPrDescriptionError(ValueError):
    """Raised when trusted publication inputs cannot be composed safely."""


@dataclass
class DescriptionDecision:
    """Publication decision for one generated Child PR body."""

    body: str
    description_sha256: str
    requires_before_after: bool
    has_before_after: bool
    blocked: bool
    reason: str
    files: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "description_sha256": self.description_sha256,
            "requires_before_after": self.requires_before_after,
            "has_before_after": self.has_before_after,
            "blocked": self.blocked,
            "reason": self.reason,
            "files": list(self.files),
        }


def normalize_text(value: Any) -> str:
    """Canonical LF text with no trailing horizontal whitespace."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def escape_untrusted(text: Any) -> str:
    """Render untrusted text so it cannot create hidden Continuum markers."""

    value = normalize_text(text)
    if not value:
        return ""
    if len(value) > MAX_SECTION_CHARS:
        value = value[:MAX_SECTION_CHARS].rstrip() + "\n\n[truncated]"
    # HTML comments are the marker channel.  Escaping the delimiters keeps
    # every occurrence visible as prose and prevents forged snapshot,
    # acceptance, repair, or controller markers.
    return value.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _norm_heading(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _section_kind(heading: str) -> Optional[str]:
    value = _norm_heading(heading)
    if not value:
        return None
    if "before" in value and "after" in value:
        return "before_after"
    if any(word in value for word in ("omission", "limitation", "risk", "known")):
        return "omissions"
    if any(word in value for word in ("verification", "verified", "test", "validation", "validation")):
        return "verification"
    if any(word in value for word in ("file", "files", "area", "areas", "changed")):
        return "files"
    if any(word in value for word in ("summary", "scope", "overview", "change", "rationale")):
        return "summary"
    return None


def parse_result_sections(result_text: Any) -> Dict[str, str]:
    """Extract recognized markdown sections from the agent result file.

    Only explicit headings are accepted; arbitrary prose is never promoted
    into a claim about tests or changed files.
    """

    text = normalize_text(result_text)
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            current = _section_kind(match.group(1))
            if current is not None and current not in sections:
                sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {
        key: normalize_text("\n".join(lines))
        for key, lines in sections.items()
        if normalize_text("\n".join(lines))
    }


def requires_before_after(issue_body: Any, issue_title: Any = "") -> bool:
    """Whether the frozen task explicitly asks for before/after examples."""

    return bool(BEFORE_AFTER_RE.search(normalize_text(issue_body))) or bool(
        BEFORE_AFTER_RE.search(normalize_text(issue_title))
    )


def requires_pr_description_evidence(issue_body: Any) -> bool:
    """Whether the task makes PR-description evidence part of acceptance."""

    return bool(PR_DESCRIPTION_RE.search(normalize_text(issue_body)))


def _diff_chunks(diff_text: Any) -> List[Tuple[str, List[str]]]:
    text = normalize_text(diff_text)
    if not text:
        return []
    chunks: List[Tuple[str, List[str]]] = []
    path = ""
    lines: List[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if path or lines:
                chunks.append((path or "changed file", lines))
            path = ""
            lines = []
            match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            if match:
                path = match.group(2)
            continue
        if line.startswith("+++ ") and not path:
            candidate = line[4:].strip()
            if candidate.startswith("b/"):
                candidate = candidate[2:]
            if candidate and candidate != "/dev/null":
                path = candidate
            continue
        if path:
            lines.append(line)
    if path or lines:
        chunks.append((path or "changed file", lines))
    return chunks


def diff_files(diff_text: Any) -> List[str]:
    """Return changed file names from a unified diff, in stable order."""

    seen = set()
    files: List[str] = []
    for path, _lines in _diff_chunks(diff_text):
        name = normalize_text(path)
        if not name or name in seen:
            continue
        seen.add(name)
        files.append(name)
    return files


def _fence(lines: Iterable[str]) -> str:
    content = "\n".join(lines)
    longest = 0
    for match in re.finditer(r"`+", content):
        longest = max(longest, len(match.group(0)))
    fence = "`" * max(3, longest + 1)
    return fence + "diff\n" + content + "\n" + fence


def before_after_from_diff(diff_text: Any) -> str:
    """Build source-backed before/after excerpts from the verified diff.

    Only removed and added content lines are included (never file headers or
    hunk coordinates as claims about behavior).  This is intentionally a
    verbatim excerpt, so it cannot invent a test result or repository content.
    """

    blocks: List[str] = []
    spent = 0
    for path, lines in _diff_chunks(diff_text):
        if spent >= MAX_DIFF_LINES or len(blocks) >= MAX_DIFF_FILES:
            break
        picked: List[str] = []
        for line in lines:
            if spent >= MAX_DIFF_LINES:
                break
            if line.startswith("---") or line.startswith("+++"):
                continue
            if line.startswith("-") or line.startswith("+"):
                picked.append(line)
                spent += 1
        if not picked:
            continue
        blocks.append("### " + escape_untrusted(path) + "\n\n" + _fence(picked))
    return "\n\n".join(blocks)


def _diff_summary(diff_text: Any) -> str:
    files = diff_files(diff_text)
    if not files:
        return ""
    listing = "\n".join("- `" + escape_untrusted(path) + "`" for path in files)
    return "This PR changes " + str(len(files)) + " file(s):\n\n" + listing


def _compose_body(
    *,
    task_number: str,
    issue_title: str,
    issue_body: str,
    result_text: str,
    diff_text: str,
    snapshot_ref: str,
    intro: str = "",
) -> DescriptionDecision:
    if not re.fullmatch(r"[0-9]+", str(task_number or "")):
        raise ChildPrDescriptionError("task number must be numeric")
    ref = normalize_text(snapshot_ref)
    if "\n" in ref or not SNAPSHOT_REF_RE.fullmatch(ref):
        raise ChildPrDescriptionError(
            "a canonical single-line continuum-task-snapshot-ref is required"
        )
    if ACCEPTED_MARKER in ref or BLOCKED_MARKER in ref or DESCRIPTION_MARKER_PREFIX in ref:
        raise ChildPrDescriptionError("snapshot reference contains a reserved marker")

    sections = parse_result_sections(result_text)
    files = diff_files(diff_text)

    summary = escape_untrusted(sections.get("summary", ""))
    if not summary:
        pinned_title = normalize_text(issue_title).replace("\n", " ").strip()
        task_label = "Implements the pinned task"
        if pinned_title:
            task_label += ": " + escape_untrusted(pinned_title)
        summary = task_label + ".\n\n" + _diff_summary(diff_text) if _diff_summary(diff_text) else (
            task_label + ". The agent did not record a narrative summary; the "
            "verified branch diff and independent review remain authoritative."
        )

    file_text = escape_untrusted(sections.get("files", ""))
    if not file_text:
        file_text = _diff_summary(diff_text)
    if not file_text:
        file_text = "_No tracked file changes were present in the verified diff._"

    verification = escape_untrusted(sections.get("verification", ""))
    if not verification:
        verification = (
            "_The agent did not record verification commands or observed "
            "results. Exact-HEAD CI and independent review remain authoritative; "
            "this description does not claim a passing test that was not observed._"
        )

    omissions = escape_untrusted(sections.get("omissions", ""))
    if not omissions:
        omissions = "None recorded by the implementation agent."

    needs_before_after = requires_before_after(issue_body, issue_title)
    before_after = escape_untrusted(sections.get("before_after", ""))
    from_diff = False
    if not before_after and needs_before_after and diff_text:
        before_after = before_after_from_diff(diff_text)
        from_diff = bool(before_after)

    blocked = False
    reason = ""
    if needs_before_after and not before_after:
        blocked = True
        reason = "task-required before/after evidence is unavailable"
        before_after = (
            "Task-required before/after evidence could not be constructed from "
            "the agent result or the verified diff. This PR must not merge "
            "until a source-backed description correction is present and "
            "independently reviewed."
        )

    parts: List[str] = []
    if blocked:
        parts.extend([BLOCKED_MARKER, ""])
    intro_text = escape_untrusted(intro)
    if intro_text:
        parts.extend([intro_text, ""])
    parts.extend(
        [
            "## Summary",
            "",
            summary,
            "",
            "## Files changed",
            "",
            file_text,
            "",
            "## Verification",
            "",
            verification,
            "",
            "## Before / after",
            "",
            before_after or "Not requested by this task.",
            "",
            "## Known omissions",
            "",
            omissions,
        ]
    )
    if from_diff and not blocked:
        parts.extend(
            [
                "",
                "_Before/after excerpts are verbatim removed/added diff lines "
                "from the verified branch, not a claim about runtime behavior._",
            ]
        )
    parts.extend(["", ref, ACCEPTED_MARKER, "Closes #" + str(task_number)])
    body_without_revision = "\n".join(parts).strip() + "\n"
    digest = hashlib.sha256(body_without_revision.encode("utf-8")).hexdigest()
    body = (
        body_without_revision
        + DESCRIPTION_MARKER_PREFIX
        + digest
        + " -->\n"
    )
    return DescriptionDecision(
        body=body,
        description_sha256=digest,
        requires_before_after=needs_before_after,
        has_before_after=bool(before_after) and not blocked,
        blocked=blocked,
        reason=reason,
        files=files,
    )


def build_child_pr_body(
    *,
    task_number: Any,
    issue_title: Any,
    issue_body: Any,
    result_text: Any,
    diff_text: Any,
    snapshot_ref: Any,
    intro: Any = "",
) -> DescriptionDecision:
    """Compose the public Child PR body from existing runner-local evidence."""

    return _compose_body(
        task_number=str(task_number),
        issue_title=normalize_text(issue_title),
        issue_body=normalize_text(issue_body),
        result_text=normalize_text(result_text),
        diff_text=normalize_text(diff_text),
        snapshot_ref=str(snapshot_ref or ""),
        intro=str(intro or ""),
    )


def description_revision(body: Any) -> Optional[str]:
    """Return the trusted description digest embedded in a published body."""

    matches = DESCRIPTION_MARKER_RE.findall(normalize_text(body))
    if len(matches) != 1:
        return None
    return matches[0]


def revision_for_body(body: Any) -> str:
    """Recompute the description digest from a body without its marker."""

    text = DESCRIPTION_MARKER_RE.sub("", normalize_text(body)).strip() + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_has_blocking_marker(body: Any) -> bool:
    return BLOCKED_MARKER in normalize_text(body)
