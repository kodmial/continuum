"""Deterministic, source-grounded PR description composition.

Continuum PR producers must describe what actually changed. The body is
derived from the committed diff and the pinned task contract only; this
module never asks a model to embellish prose and never invents before/after
examples. Machine-owned markers (task context, snapshot reference, Child
acceptance, closing keywords) are passed through verbatim.

The same evidence block is used for a PR-body-only correction after a
review finds a metadata obligation, so a corrected description is a new
review input without changing HEAD.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

EVIDENCE_START = "<!-- continuum-pr-body-evidence:v1"
EVIDENCE_END = "<!-- /continuum-pr-body-evidence -->"
EVIDENCE_RE = re.compile(
    re.escape(EVIDENCE_START) + r"[^>]*-->[\s\S]*?" + re.escape(EVIDENCE_END),
    re.MULTILINE,
)

_BEFORE_AFTER_RE = re.compile(r"\bbefore\s*(?:/|and|&)\s*after\b", re.IGNORECASE)
_EVIDENCE_RE = re.compile(
    r"\b(?:before\s*(?:/|and|&)\s*after|pr\s+(?:description|body)|"
    r"pull\s+request\s+(?:description|body)|evidence\s+in\s+the\s+(?:pr|pull\s+request))\b",
    re.IGNORECASE,
)
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ .* @@")


def body_digest(pr_body: Any) -> str:
    text = str("" if pr_body is None else pr_body).replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_input_version(head_sha: str, pr_body: Any) -> str:
    head = str(head_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", head) or re.fullmatch(r"0+", head):
        raise ValueError("review input version requires a plausible HEAD SHA")
    return f"{head}:{body_digest(pr_body)}"


def task_requires_before_after(task_body: Any) -> bool:
    return bool(_BEFORE_AFTER_RE.search(str(task_body or "")))


def task_requires_evidence(task_body: Any) -> bool:
    return bool(_EVIDENCE_RE.search(str(task_body or "")))


def summarize_diff(diff_text: Any) -> List[Dict[str, Any]]:
    """Return per-file added/removed counts from a unified diff."""

    files: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for raw_line in str(diff_text or "").splitlines():
        match = _DIFF_FILE_RE.match(raw_line)
        if match:
            path = match.group(2).strip()
            current = files.setdefault(
                path, {"path": path, "added": 0, "removed": 0, "before": [], "after": []}
            )
            continue
        if current is None:
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+") and raw_line[1:].strip():
            current["added"] += 1
            if len(current["after"]) < 3:
                current["after"].append(raw_line[1:].strip())
        elif raw_line.startswith("-") and raw_line[1:].strip():
            current["removed"] += 1
            if len(current["before"]) < 3:
                current["before"].append(raw_line[1:].strip())
    return sorted(files.values(), key=lambda entry: entry["path"])


def build_evidence_section(
    diff_text: Any,
    *,
    requirements: Iterable[Any] = (),
    head: str = "",
    include_summary: bool = True,
    max_examples: int = 3,
) -> str:
    """Build the machine-owned evidence block from real diff content.

    When the diff has no usable removed/added pair, before/after is omitted
    rather than fabricated. The summary always comes from actual diff stats.
    """

    requirement_texts: List[str] = []
    for entry in requirements or ():
        text = entry.get("text") if isinstance(entry, dict) else entry
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if cleaned and cleaned not in requirement_texts:
            requirement_texts.append(cleaned[:500])
    files = summarize_diff(diff_text)
    require_before_after = any(_BEFORE_AFTER_RE.search(text) for text in requirement_texts)
    head_marker = f" head={head.strip().lower()}" if head else ""
    lines: List[str] = [EVIDENCE_START + head_marker + " -->", "## Review evidence", ""]
    if requirement_texts:
        lines.append("Reviewer-required evidence:")
        for text in requirement_texts:
            lines.append(f"- {text}")
        lines.append("")
    if include_summary and files:
        lines.append("### Change summary")
        lines.append("")
        for entry in files:
            lines.append(
                f"- `{entry['path']}`: {entry['added']} line(s) added, "
                f"{entry['removed']} line(s) removed"
            )
        lines.append("")
    if require_before_after:
        examples: List[str] = []
        for entry in files:
            if len(examples) >= max_examples:
                break
            if not entry["before"] or not entry["after"]:
                continue
            before = entry["before"][0][:240]
            after = entry["after"][0][:240]
            examples.append(f"- `{entry['path']}`: Before: `{before}`; After: `{after}`")
        if examples:
            lines.append("### Before / After")
            lines.append("")
            lines.extend(examples)
            lines.append("")
    if head:
        lines.append(
            f"Validated on exact HEAD `{head.strip()}`; this description update is "
            "reviewed afresh before merge."
        )
        lines.append("")
    lines.append(EVIDENCE_END)
    return "\n".join(lines).strip()


def replace_evidence_section(pr_body: Any, section: Any) -> str:
    """Replace only the machine-owned evidence block in an existing body."""

    body = str("" if pr_body is None else pr_body)
    cleaned = str(section or "").strip()
    if EVIDENCE_RE.search(body):
        return EVIDENCE_RE.sub(lambda _match: cleaned, body)
    if not cleaned:
        return body
    return body.rstrip() + "\n\n" + cleaned + "\n"


def compose_pr_body(
    *,
    task_marker: str = "",
    snapshot_ref: str = "",
    issue_number: Optional[int] = None,
    task_body: str = "",
    diff_text: str = "",
    head: str = "",
    qualification: bool = False,
    child_accepted: bool = False,
    recovery_note: str = "",
    validation: str = "",
    include_summary: bool = True,
) -> str:
    """Compose the complete PR body while preserving protected markers."""

    markers: List[str] = []
    if task_marker.strip():
        markers.append(task_marker.strip())
    if snapshot_ref.strip():
        markers.append(snapshot_ref.strip())

    paragraphs: List[str] = []
    if recovery_note.strip():
        paragraphs.append(recovery_note.strip())
    else:
        paragraphs.append(
            f"Automated implementation for #{issue_number}." if issue_number else "Automated implementation."
        )

    evidence_required = task_requires_evidence(task_body)
    has_diff = bool(summarize_diff(diff_text))
    if evidence_required or (include_summary and has_diff):
        section = build_evidence_section(
            diff_text,
            requirements=(
                ["The task requires before/after evidence."] if evidence_required else []
            ),
            head=head,
            include_summary=include_summary,
        )
        if section.strip().splitlines() and len(section.strip().splitlines()) > 3:
            paragraphs.append(section)

    if validation.strip():
        paragraphs.append("### Validation\n\n" + validation.strip())

    if issue_number:
        if child_accepted:
            paragraphs.append("<!-- continuum-child-accepted -->")
        if qualification:
            paragraphs.append(
                f"Relates to #{issue_number}\n\n"
                f"Mandatory live qualification is declared on #{issue_number}; this PR "
                "must not auto-close it. The qualification gate records the merged main "
                "SHA and dispatches qualification after merge."
            )
        else:
            paragraphs.append(f"Closes #{issue_number}")

    body_parts = markers + [""] if markers else []
    body_parts.append("\n\n".join(paragraphs).strip())
    return "\n".join(part for part in body_parts if part != "" or markers).strip() + "\n"


def _read(path: str) -> str:
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    compose = sub.add_parser("compose", help="Compose a complete PR body")
    compose.add_argument("--task-marker", default="")
    compose.add_argument("--snapshot-ref", default="")
    compose.add_argument("--issue-number", type=int, default=0)
    compose.add_argument("--task-body-file", default="")
    compose.add_argument("--diff-file", default="")
    compose.add_argument("--head", default="")
    compose.add_argument("--qualification", action="store_true")
    compose.add_argument("--child-accepted", action="store_true")
    compose.add_argument("--recovery-note", default="")
    compose.add_argument("--validation", default="")
    compose.add_argument("--summary", dest="summary", action="store_true", default=True)
    compose.add_argument("--no-summary", dest="summary", action="store_false")

    evidence = sub.add_parser("evidence", help="Build only the evidence block")
    evidence.add_argument("--task-body-file", default="")
    evidence.add_argument("--diff-file", default="")
    evidence.add_argument("--head", default="")
    evidence.add_argument("--requirement", action="append", default=[])

    replace = sub.add_parser("replace", help="Replace the evidence block in a body")
    replace.add_argument("--pr-body-file", required=True)
    replace.add_argument("--evidence-file", required=True)

    digest = sub.add_parser("digest", help="Print the review-input digest")
    digest.add_argument("--pr-body-file", required=True)

    version = sub.add_parser("version", help="Print HEAD + body review-input version")
    version.add_argument("--head", required=True)
    version.add_argument("--pr-body-file", required=True)

    args = parser.parse_args(argv)
    if args.command == "compose":
        task_body = _read(args.task_body_file)
        diff_text = _read(args.diff_file)
        sys.stdout.write(
            compose_pr_body(
                task_marker=args.task_marker,
                snapshot_ref=args.snapshot_ref,
                issue_number=args.issue_number or None,
                task_body=task_body,
                diff_text=diff_text,
                head=args.head,
                qualification=args.qualification,
                child_accepted=args.child_accepted,
                recovery_note=args.recovery_note,
                validation=args.validation,
                include_summary=args.summary,
            )
        )
        return 0
    if args.command == "evidence":
        task_body = _read(args.task_body_file)
        diff_text = _read(args.diff_file)
        requirements = list(args.requirement)
        if task_requires_before_after(task_body) and not any(
            _BEFORE_AFTER_RE.search(text) for text in requirements
        ):
            requirements.append("The task requires before/after evidence.")
        sys.stdout.write(
            build_evidence_section(
                diff_text,
                requirements=requirements,
                head=args.head,
            )
        )
        return 0
    if args.command == "replace":
        sys.stdout.write(
            replace_evidence_section(_read(args.pr_body_file), _read(args.evidence_file))
        )
        return 0
    if args.command == "digest":
        sys.stdout.write(body_digest(_read(args.pr_body_file)))
        return 0
    if args.command == "version":
        sys.stdout.write(
            review_input_version(args.head, _read(args.pr_body_file))
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
