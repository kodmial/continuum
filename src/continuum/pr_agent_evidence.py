"""Deterministic full-PR evidence for PR-Agent review findings.

PR-Agent reviews a pull request in bounded chunks. A model that sees only one
chunk can mistake an unseen-but-unchanged file for a file that is absent from
the PR, or it can read an incremental repair diff as if it were the complete
``base..head`` PR diff. Both mistakes generate actionable findings that can
never be satisfied by another repair and therefore loop forever.

This module is the trusted counterweight:

* it builds a canonical, complete changed-files manifest for the exact HEAD;
* it renders a prompt block that distinguishes the full PR diff, the current
  review chunk, and the repair-only diff;
* it classifies findings whose text makes a file-absence claim and drops only
  the narrowly contradicted structural claims.

The module is standard-library only because the workflow loads it on a bare
runner. It deliberately never decides that a PR is generally safe: promotion
of a filtered review remains a separate, explicitly gated policy decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = 1
MAX_MANIFEST_FILES = 2000
MAX_PROMPT_FILES = 2000

HEX_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")

# File extensions that may legitimately appear as a bare path in review prose.
# Requiring a plausible extension (or a directory separator) keeps ordinary
# sentence fragments such as "e.g." or "v1.2" out of the evidence matcher.
PATH_EXTENSIONS = frozenset(
    {
        "bash", "c", "cc", "cfg", "conf", "cpp", "cs", "css", "csv", "dockerfile",
        "env", "example", "fish", "go", "gradle", "h", "hpp", "htm", "html",
        "ini", "java", "js", "json", "jsonc", "jsx", "kt", "kts", "less", "lock",
        "md", "mod", "php", "pl", "proto", "ps1", "py", "rb", "rs", "rst", "sass",
        "scss", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx", "txt", "vue",
        "xml", "yaml", "yml", "zsh",
    }
)

# Structural words that turn a finding into a file/path-absence claim. The
# patterns are intentionally narrow: a generic sentence that merely contains
# "missing" is still only considered when it also references a path.
ABSENCE_CLAIM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmissing\b",
        r"\babsent\b",
        r"\bomitted\b",
        r"\bexcluded\b",
        r"\bnot\s+(?:included|present|contained|listed|shown|visible|part\s+of|found|added|modified|touched|changed|covered)\b",
        r"\bdoes(?:n't|\s+not)\s+(?:include|contain|show|list|cover|touch|add|modify|change)\b",
        r"\bno\s+(?:changes?|modifications?|diff|updates?)\s+(?:to|for|in)\b",
        r"\bnot\s+(?:in|from)\s+(?:the\s+)?(?:pr|pull\s+request|diff|changeset|change\s+set|patch)\b",
        r"\b(?:file|files|path|paths)\s+(?:is|are|was|were)?\s*(?:missing|absent|not\s+found)\b",
        r"\bno\s+such\s+file\b",
        r"\bnot\s+implemented\b",
        r"\bmissing\s+(?:implementation|file|code|changes?|runtime)\b",
        r"\bduplicate(?:d)?\s+definition\b",
    )
)

_ABSENCE_BEFORE_PATH_RE = re.compile(
    r"(?:missing|absent|omitted|excluded|no\s+such\s+file|"
    r"no\s+changes?\s+(?:to|for|in)|no\s+modifications?\s+(?:to|for|in)|"
    r"does(?:n't|\s+not)\s+(?:include|contain|add|list|show))"
    r"\s*(?:the\s+)?(?:file\s+|path\s+)?[`'\"(\[]*$",
    re.IGNORECASE,
)
_ABSENCE_AFTER_PATH_RE = re.compile(
    r"[`'\")\]]*\s*(?:is|are|was|were)\s+(?:missing|absent|omitted|excluded)\b"
    r"(?:\s+(?:from|in)\s+(?:the\s+)?"
    r"(?:pr(?:\s+diff)?|pull\s+request(?:\s+diff)?|diff|changeset|"
    r"change\s+set|patch|chunk|review\s+chunk|context|current\s+context|"
    r"repair(?:\s+diff|\s+commit)?|repository|codebase|tree|checkout|"
    r"provided\s+(?:diff|context)(?:\s+portion)?)\b)?"
    r"\s*(?:[.,;:]|$)",
    re.IGNORECASE,
)
_ABSENCE_AFTER_PATH_NEGATED_RE = re.compile(
    r"[`'\")\]]*\s*(?:is|are|was|were|do|does|did)\s+not\s+"
    r"(?:currently\s+|explicitly\s+)?"
    r"(?:included|present|contained|listed|shown|visible|part|found|"
    r"added|modified|touched|changed|covered|exist)\b"
    r"(?:\s+(?:in|from|within|of)\s+(?:the\s+)?"
    r"(?:pr(?:\s+diff)?|pull\s+request(?:\s+diff)?|diff|changeset|"
    r"change\s+set|patch|chunk|review\s+chunk|context|current\s+context|"
    r"repair(?:\s+diff|\s+commit)?|provided\s+(?:diff|context)(?:\s+portion)?)\b)?"
    r"\s*(?:[.,;:]|$)",
    re.IGNORECASE,
)


_ABSENCE_AFTER_PATH_NOT_IN_RE = re.compile(
    r"[`'\")\]]*\s*(?:is|are|was|were)\s+not\s+(?:in|from|within)\s+"
    r"(?:the\s+)?(?:pr(?:\s+diff)?|pull\s+request(?:\s+diff)?|diff|"
    r"changeset|change\s+set|patch|chunk|review\s+chunk|context|"
    r"current\s+context|repair(?:\s+diff|\s+commit)?|provided\s+"
    r"(?:diff|context)(?:\s+portion)?)\b\s*(?:[.,;:]|$)",
    re.IGNORECASE,
)


def _structural_file_absence(text: str, references: Sequence[str]) -> bool:
    """Return True only when an absence phrase is attached to a path.

    A content defect such as "``src/a.py`` is missing the lock" or "the parser
    implementation is missing" must survive: the file is present, the defect
    is inside it. Only path-scoped absence ("is missing", "is not included in
    the PR diff", "does not exist") is objectively contradicted by a manifest
    that lists the path.
    """

    lowered = text.lower()
    for reference in references:
        for needle in {reference, reference.rsplit("/", 1)[-1]}:
            if not needle:
                continue
            start = 0
            while True:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                start = index + len(needle)
                before = lowered[max(0, index - 100):index]
                after = lowered[index + len(needle):index + len(needle) + 160]
                if _ABSENCE_BEFORE_PATH_RE.search(before):
                    return True
                if _ABSENCE_AFTER_PATH_RE.match(after):
                    return True
                if _ABSENCE_AFTER_PATH_NEGATED_RE.match(after):
                    return True
                if _ABSENCE_AFTER_PATH_NOT_IN_RE.match(after):
                    return True
    return False


CHUNK_SCOPE_RE = re.compile(
    r"\b(?:this|the|current|review|single|provided|visible)\s+"
    r"(?:review\s+)?(?:chunk|excerpt|portion|context|fragment)\b"
    r"|\bchunk\b",
    re.IGNORECASE,
)
REPAIR_SCOPE_RE = re.compile(
    r"\brepair(?:ed|-only)?\s+(?:diff|commit|head)\b"
    r"|\bincremental\s+(?:diff|repair)\b"
    r"|\blatest\s+(?:repair\s+)?commit\b"
    r"|\bparent\s+head\b"
    r"|\bpost-repair\b",
    re.IGNORECASE,
)
PR_SCOPE_RE = re.compile(
    r"\b(?:pr|pull\s+request|base\.\.head|full\s+diff|changeset|change\s+set|diff|patch)\b",
    re.IGNORECASE,
)
EXISTENCE_CLAIM_RE = re.compile(
    r"\bmissing\s+(?:implementation|file|code|changes?|runtime)\b"
    r"|\b(?:file|files|path|paths)\s+(?:is|are|was|were)?\s*(?:missing|absent|not\s+found)\b"
    r"|\bno\s+such\s+file\b"
    r"|\bnot\s+implemented\b"
    r"|\bdoes\s+not\s+exist\b"
    r"|\babsent\s+from\s+the\s+(?:repository|codebase|tree|checkout)\b",
    re.IGNORECASE,
)

# Absence claims about security-sensitive surfaces are never filtered. A real
# security defect must survive every evidence pass and keep blocking merge.
SECURITY_CLAIM_RE = re.compile(
    r"\b(?:security|vulnerab\w*|exploit\w*|injection|xss|csrf|rce|secret\w*|"
    r"credential\w*|auth(?:entication|orization)?|permission\w*|privilege\w*|"
    r"sandbox\w*|sensitive\w*|leak\w*)\b",
    re.IGNORECASE,
)

BACKTICK_RE = re.compile(r"`([^`\n]+)`")
BARE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./@-])"
    r"(?:"
    r"\.?[A-Za-z0-9_@+-]+(?:/[A-Za-z0-9_.@+-]+)+"
    r"|\.[A-Za-z0-9_][A-Za-z0-9_.-]*"
    r"|[A-Za-z0-9_@+-]+\.[A-Za-z0-9_]{1,10}"
    r")"
    r"(?![A-Za-z0-9_./@-])"
)


class EvidenceError(ValueError):
    """Raised when evidence is absent, stale, or too incomplete to trust."""


def _normalize_path(value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text:
        return ""
    text = text.strip("`'\"()[]{}<> \t\r\n")
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"#.*$", "", text)
    text = re.sub(r":\d+(?:-\d+)?$", "", text)
    text = re.sub(r"[.,;:]+$", "", text)
    text = text.lstrip("/")
    return text.lower()


def _candidate_paths(text: str) -> List[str]:
    candidates: List[str] = []

    def consider(raw: str) -> None:
        value = _normalize_path(raw)
        if not value or len(value) > 240:
            return
        if "/" not in value:
            name = value.rsplit("/", 1)[-1]
            extension = name.rsplit(".", 1)[-1] if "." in name else ""
            if not name.startswith(".") and extension not in PATH_EXTENSIONS:
                return
        candidates.append(value)

    for match in BACKTICK_RE.finditer(text):
        for token in re.split(r"\s+", match.group(1)):
            consider(token)
    for match in BARE_PATH_RE.finditer(BACKTICK_RE.sub(" ", text)):
        consider(match.group(0))
    seen: Set[str] = set()
    ordered: List[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def _finding_text(value: object) -> str:
    parts: List[str] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            if node.strip():
                parts.append(node)
        elif isinstance(node, Mapping):
            for key, item in node.items():
                # Structured verdicts are not prose and must not smuggle a
                # path/absence phrase into the classifier.
                if str(key).lower() in {"line", "start_line", "end_line", "rank", "score"}:
                    continue
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return "\n".join(parts)


def _finding_title(value: object) -> str:
    if isinstance(value, Mapping):
        for key in ("issue_header", "title", "one_sentence_summary", "label", "body", "description"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return " ".join(item.split())[:200]
    return ""


def _finding_file(value: object) -> str:
    if isinstance(value, Mapping):
        for key in ("relevant_file", "path", "file", "filename"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:240]
    return ""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str = "modified"
    previous_path: str = ""
    additions: Optional[int] = None
    deletions: Optional[int] = None
    changes: Optional[int] = None


@dataclass(frozen=True)
class PathMatch:
    kind: str
    path: str = ""
    ambiguous: bool = False


@dataclass
class PrManifest:
    head_sha: str
    files: Tuple[ChangedFile, ...]
    tree_paths: Tuple[str, ...]
    complete: bool = True
    truncated: bool = False
    pages: int = 0
    digest: str = field(default="")

    def __post_init__(self) -> None:
        head = str(self.head_sha or "").strip().lower()
        if not HEX_SHA_RE.fullmatch(head):
            raise EvidenceError("manifest requires an exact full HEAD SHA")
        self.head_sha = head
        if not self.digest:
            self.digest = self._compute_digest()

    def _compute_digest(self) -> str:
        canonical = [
            {
                "path": item.path,
                "status": item.status,
                "previous_path": item.previous_path,
            }
            for item in self.files
        ]
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def changed_paths(self) -> Set[str]:
        result: Set[str] = set()
        for item in self.files:
            if item.path:
                result.add(item.path)
            if item.previous_path:
                result.add(item.previous_path)
        return result

    @property
    def tree_set(self) -> Set[str]:
        return set(self.tree_paths)

    def match(self, reference: str) -> PathMatch:
        value = _normalize_path(reference)
        if not value:
            return PathMatch("absent")
        changed = self.changed_paths
        tree = self.tree_set
        if value in changed:
            return PathMatch("changed", value)
        if value in tree:
            return PathMatch("tree", value)
        # Directory references ("src/aa") are meaningful for implementation
        # claims even though the manifest lists files.
        if any(path.startswith(value + "/") for path in changed):
            return PathMatch("changed", value)
        if any(path.startswith(value + "/") for path in tree):
            return PathMatch("tree", value)
        # Basename and suffix aliases cover review prose that omits a prefix.
        basename = value.rsplit("/", 1)[-1]
        changed_matches = [path for path in changed if path.rsplit("/", 1)[-1] == basename]
        if len(changed_matches) == 1:
            return PathMatch("changed", changed_matches[0])
        if len(changed_matches) > 1:
            return PathMatch("ambiguous", ambiguous=True)
        tree_matches = [path for path in tree if path.rsplit("/", 1)[-1] == basename]
        if len(tree_matches) == 1:
            return PathMatch("tree", tree_matches[0])
        if len(tree_matches) > 1:
            return PathMatch("ambiguous", ambiguous=True)
        changed_suffix = [path for path in changed if path.endswith("/" + value)]
        if len(changed_suffix) == 1:
            return PathMatch("changed", changed_suffix[0])
        if len(changed_suffix) > 1:
            return PathMatch("ambiguous", ambiguous=True)
        tree_suffix = [path for path in tree if path.endswith("/" + value)]
        if len(tree_suffix) == 1:
            return PathMatch("tree", tree_suffix[0])
        if len(tree_suffix) > 1:
            return PathMatch("ambiguous", ambiguous=True)
        return PathMatch("absent")


@dataclass(frozen=True)
class Assessment:
    drop: bool
    reason: str
    paths: Tuple[str, ...] = ()
    scope: str = ""
    ambiguous: bool = False


def classify_finding(value: object, manifest: PrManifest) -> Assessment:
    """Classify one finding against the canonical full-PR manifest.

    Only findings that make an explicit structural file-absence claim and
    whose referenced paths are all unambiguously present may be dropped.
    Everything else, including any ambiguous alias or absent path, survives
    as actionable/needing targeted verification.
    """

    text = _finding_text(value)
    if not text.strip():
        return Assessment(False, "empty_finding")
    if not any(pattern.search(text) for pattern in ABSENCE_CLAIM_PATTERNS):
        return Assessment(False, "no_absence_claim")
    if SECURITY_CLAIM_RE.search(text):
        return Assessment(False, "security_protected")

    references = _candidate_paths(text)
    if not references:
        return Assessment(False, "no_path_reference")

    matches = [(reference, manifest.match(reference)) for reference in references]
    retryable = [(reference, match) for reference, match in matches if match.kind == "absent"]
    ambiguous = [
        (reference, match) for reference, match in matches if match.ambiguous
    ]
    known = [
        (reference, match)
        for reference, match in matches
        if match.kind in ("changed", "tree")
    ]
    paths = tuple(sorted({match.path or reference for reference, match in known}))

    if ambiguous:
        return Assessment(False, "ambiguous_path_alias", paths, ambiguous=True)
    if retryable:
        # A path that is genuinely absent is not evidence of a false claim.
        # Keep the finding: it may be a real missing file or a path alias we
        # must verify on the current checkout.
        return Assessment(False, "absent_or_unverifiable_path", paths)
    if not known:
        return Assessment(False, "no_resolvable_path")

    chunk_scope = bool(CHUNK_SCOPE_RE.search(text))
    repair_scope = bool(REPAIR_SCOPE_RE.search(text))
    pr_scope = bool(PR_SCOPE_RE.search(text))
    existence_claim = bool(EXISTENCE_CLAIM_RE.search(text))
    scope = "repair" if repair_scope else "chunk" if chunk_scope else "pr" if pr_scope else "unknown"

    structural = _structural_file_absence(text, references)
    changed_paths = [match.path for _, match in known if match.kind == "changed"]
    tree_only = all(match.kind == "tree" for _, match in known)
    if changed_paths:
        if structural or chunk_scope:
            return Assessment(True, "contradicted_full_pr_manifest", paths, scope)
        # The referenced changed file exists, but the finding does not claim
        # the file itself is absent. It may be a real in-file defect such as
        # a missing lock or an unimplemented branch, so keep it actionable.
        return Assessment(False, "changed_file_content_claim", paths, scope)
    if tree_only and (chunk_scope or repair_scope or (structural and not pr_scope)):
        return Assessment(True, "contradicted_current_tree", paths, scope)
    if tree_only and pr_scope and not existence_claim:
        # "X is unchanged / not in the diff" is factually true for a file that
        # exists in the current tree but not in the PR. Do not call it false;
        # keep it for targeted verification because implementation may still
        # be genuinely absent from the PR.
        return Assessment(False, "tree_only_diff_scope", paths, scope)
    return Assessment(False, "not_contradicted", paths, scope)


def _parse_files(raw_entries: Sequence[Mapping[str, Any]]) -> List[ChangedFile]:
    files: List[ChangedFile] = []
    seen: Set[str] = set()
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            raise EvidenceError("changed-files page contains a non-object entry")
        path = _normalize_path(entry.get("filename") or entry.get("path") or "")
        if not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        status = str(entry.get("status") or "modified").strip().lower() or "modified"
        previous = _normalize_path(entry.get("previous_filename") or entry.get("previous_path") or "")
        files.append(
            ChangedFile(
                path=path,
                status=status,
                previous_path=previous,
                additions=_optional_int(entry.get("additions")),
                deletions=_optional_int(entry.get("deletions")),
                changes=_optional_int(entry.get("changes")),
            )
        )
    if len(files) > MAX_MANIFEST_FILES:
        raise EvidenceError(
            f"changed-files manifest exceeds the bounded {MAX_MANIFEST_FILES}-file limit"
        )
    return files


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def build_manifest(
    head_sha: str,
    file_entries: Sequence[Mapping[str, Any]],
    tree_paths: Iterable[str],
    *,
    complete: bool = True,
    truncated: bool = False,
    pages: int = 0,
) -> PrManifest:
    files = _parse_files(file_entries)
    normalized_tree = sorted(
        {
            normalized
            for normalized in (_normalize_path(path) for path in tree_paths)
            if normalized
        }
    )
    return PrManifest(
        head_sha=head_sha,
        files=tuple(files),
        tree_paths=tuple(normalized_tree),
        complete=bool(complete),
        truncated=bool(truncated),
        pages=int(pages),
    )


def manifest_to_dict(manifest: PrManifest) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "head_sha": manifest.head_sha,
        "complete": manifest.complete,
        "truncated": manifest.truncated,
        "pages": manifest.pages,
        "digest": manifest.digest,
        "file_count": len(manifest.files),
        "files": [
            {
                "path": item.path,
                "status": item.status,
                "previous_path": item.previous_path,
                "additions": item.additions,
                "deletions": item.deletions,
                "changes": item.changes,
            }
            for item in manifest.files
        ],
        "tree_paths": list(manifest.tree_paths),
    }


def manifest_from_dict(payload: Mapping[str, Any]) -> PrManifest:
    if not isinstance(payload, Mapping):
        raise EvidenceError("manifest must be a JSON object")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise EvidenceError("manifest schema version is missing or unsupported")
    files = payload.get("files")
    tree = payload.get("tree_paths")
    if not isinstance(files, list):
        raise EvidenceError("manifest files must be a list")
    if not isinstance(tree, list):
        raise EvidenceError("manifest tree_paths must be a list")
    manifest = build_manifest(
        str(payload.get("head_sha") or ""),
        files,
        [str(path) for path in tree],
        complete=payload.get("complete") is True,
        truncated=payload.get("truncated") is True,
        pages=int(payload.get("pages") or 0),
    )
    declared = str(payload.get("digest") or "").strip().lower()
    if declared and declared != manifest.digest:
        raise EvidenceError("manifest digest does not match its contents")
    declared_count = payload.get("file_count")
    if declared_count is not None and int(declared_count) != len(manifest.files):
        raise EvidenceError("manifest file_count does not match its contents")
    return manifest


def render_prompt(manifest: PrManifest) -> str:
    lines = [
        "CANONICAL FULL-PR EVIDENCE (authoritative for the exact current HEAD).",
        f"Exact HEAD: {manifest.head_sha}",
        f"Manifest digest: {manifest.digest}",
        f"Full PR changed files: {len(manifest.files)}",
        "",
        "Scope definitions:",
        "- FULL PR DIFF is the complete base..head changed-files manifest below.",
        "- REVIEW CHUNK is only the subset included in one model call. A file or",
        "  symbol missing from a chunk proves nothing about the PR.",
        "- REPAIR-ONLY DIFF is the parent..head diff of the latest workflow repair",
        "  commit. It is NOT the PR diff; unchanged files do not appear in it.",
        "",
        "Rules:",
        "- Every file listed below IS part of the PR. Never report it as missing,",
        "  absent, omitted, not included, or not present in the PR/diff.",
        "- Files not listed may be unchanged and still present in the current",
        "  repository tree. Presence in the tree means the file is not missing.",
        "- A confirmation that depends only on files outside the current chunk",
        "  belongs under human verification, not an actionable requirement.",
        "- Before claiming a file or implementation is missing, prove absence",
        "  against this manifest and the current exact-HEAD checkout.",
        "",
    ]
    if not manifest.complete or manifest.truncated:
        lines += [
            "WARNING: THIS MANIFEST IS INCOMPLETE OR TRUNCATED.",
            "Do not make any file-absence claim. Request targeted verification.",
            "",
        ]
    lines.append("Changed files (status: path [renamed from previous]):")
    for item in manifest.files[:MAX_PROMPT_FILES]:
        suffix = f" (renamed from {item.previous_path})" if item.previous_path else ""
        lines.append(f"- {item.status}: {item.path}{suffix}")
    if len(manifest.files) > MAX_PROMPT_FILES:
        lines += [
            f"- ... {len(manifest.files) - MAX_PROMPT_FILES} more changed file(s) are",
            "  present in the canonical runner-local manifest.",
        ]
    return "\n".join(lines) + "\n"


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def validate_review(
    review: Mapping[str, Any],
    manifest: PrManifest,
    *,
    expected_head: str = "",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a sanitized review plus an evidence report.

    The report records every dropped structural claim. It never upgrades a
    review recommendation; that decision is owned by the workflow policy.
    """

    if not isinstance(review, Mapping):
        raise EvidenceError("review must be a JSON object")
    if not manifest.complete or manifest.truncated:
        raise EvidenceError("full-PR manifest is incomplete or truncated; refusing to filter findings")
    expected = str(expected_head or "").strip().lower()
    if expected and manifest.head_sha != expected:
        raise EvidenceError(
            f"manifest HEAD {manifest.head_sha} does not match reviewed HEAD {expected}"
        )

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "head_sha": manifest.head_sha,
        "manifest_digest": manifest.digest,
        "manifest_complete": manifest.complete,
        "changed_file_count": len(manifest.files),
        "scanned": 0,
        "kept": 0,
        "unsupported_count": 0,
        "ambiguous": False,
        "max_key_issues_count": 0,
        "dropped": [],
        "kept_reasons": {},
    }

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            result: Dict[str, Any] = {}
            for key, value in node.items():
                if key == "key_issues_to_review" and isinstance(value, list):
                    report["max_key_issues_count"] = max(
                        int(report["max_key_issues_count"]), len(value)
                    )
                    kept: List[Any] = []
                    for item in value:
                        report["scanned"] += 1
                        assessment = classify_finding(item, manifest)
                        if assessment.ambiguous:
                            report["ambiguous"] = True
                        if assessment.drop:
                            report["unsupported_count"] += 1
                            report["dropped"].append(
                                {
                                    "reason": assessment.reason,
                                    "scope": assessment.scope,
                                    "paths": list(assessment.paths),
                                    "title": _finding_title(item),
                                    "file": _finding_file(item),
                                }
                            )
                            continue
                        report["kept"] += 1
                        reason = assessment.reason
                        report["kept_reasons"][reason] = int(
                            report["kept_reasons"].get(reason, 0)
                        ) + 1
                        kept.append(walk(item))
                    result[key] = kept
                else:
                    result[key] = walk(value)
            return result
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    sanitized = walk(review)
    if not isinstance(sanitized, dict):
        raise EvidenceError("sanitized review is not an object")
    return sanitized, report


def validate_items(
    items: Sequence[Mapping[str, Any]],
    manifest: PrManifest,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Filter a repair batch (review findings and improve suggestions)."""

    if not manifest.complete or manifest.truncated:
        raise EvidenceError("full-PR manifest is incomplete or truncated; refusing to filter items")
    kept: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "head_sha": manifest.head_sha,
        "manifest_digest": manifest.digest,
        "manifest_complete": manifest.complete,
        "scanned": 0,
        "kept": 0,
        "unsupported_count": 0,
        "ambiguous": False,
        "dropped": [],
    }
    for item in items:
        if not isinstance(item, Mapping):
            raise EvidenceError("repair batch contains a non-object item")
        source = str(item.get("source") or "")
        payload = item.get("finding") if source == "review" else item.get("suggestion")
        if not isinstance(payload, Mapping):
            kept.append(dict(item))
            continue
        report["scanned"] += 1
        assessment = classify_finding(payload, manifest)
        if assessment.ambiguous:
            report["ambiguous"] = True
        if assessment.drop:
            report["unsupported_count"] += 1
            report["dropped"].append(
                {
                    "reason": assessment.reason,
                    "scope": assessment.scope,
                    "paths": list(assessment.paths),
                    "title": _finding_title(payload),
                    "file": _finding_file(payload),
                }
            )
            continue
        report["kept"] += 1
        kept.append(dict(item))
    return kept, report


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: Any, mode: int = 0o600) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _write_text(path: str, text: str, mode: int = 0o600) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _command_build(args: argparse.Namespace) -> int:
    entries = _read_json(args.files_json)
    if not isinstance(entries, list):
        raise EvidenceError("changed-files JSON must be a list")
    with open(args.tree_file, "r", encoding="utf-8") as handle:
        tree = [line.strip() for line in handle if line.strip()]
    manifest = build_manifest(
        args.head_sha,
        entries,
        tree,
        complete=not args.truncated,
        truncated=args.truncated,
        pages=args.pages,
    )
    if not manifest.complete or manifest.truncated:
        raise EvidenceError(
            "changed-files pagination reached its bound before the PR file list ended"
        )
    if not manifest.files:
        raise EvidenceError("pull request changed-files manifest is empty")
    _write_json(args.out, manifest_to_dict(manifest))
    prompt = render_prompt(manifest)
    _write_text(args.prompt_out, prompt)
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    review = _read_json(args.review_file)
    manifest = manifest_from_dict(_read_json(args.manifest_file))
    sanitized, report = validate_review(
        review, manifest, expected_head=args.expected_head
    )
    _write_json(args.out_review, sanitized)
    _write_json(args.report_out, report)
    return 0


def _command_validate_items(args: argparse.Namespace) -> int:
    items = _read_json(args.items_file)
    if not isinstance(items, list):
        raise EvidenceError("repair items JSON must be a list")
    manifest = manifest_from_dict(_read_json(args.manifest_file))
    kept, report = validate_items(items, manifest)
    _write_json(args.out_items, kept)
    _write_json(args.report_out, report)
    return 0


def _command_classify(args: argparse.Namespace) -> int:
    manifest = manifest_from_dict(_read_json(args.manifest_file))
    value = _read_json(args.finding_file)
    assessment = classify_finding(value, manifest)
    payload = {
        "drop": assessment.drop,
        "reason": assessment.reason,
        "paths": list(assessment.paths),
        "scope": assessment.scope,
        "ambiguous": assessment.ambiguous,
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a canonical manifest and prompt")
    build.add_argument("--head-sha", required=True)
    build.add_argument("--files-json", required=True)
    build.add_argument("--tree-file", required=True)
    build.add_argument("--out", required=True)
    build.add_argument("--prompt-out", required=True)
    build.add_argument("--pages", type=int, default=0)
    build.add_argument("--truncated", action="store_true")
    build.set_defaults(handler=_command_build)

    validate = subparsers.add_parser("validate", help="filter unsupported absence findings")
    validate.add_argument("--review-file", required=True)
    validate.add_argument("--manifest-file", required=True)
    validate.add_argument("--out-review", required=True)
    validate.add_argument("--report-out", required=True)
    validate.add_argument("--expected-head", default="")
    validate.set_defaults(handler=_command_validate)

    items = subparsers.add_parser("validate-items", help="filter a repair batch")
    items.add_argument("--items-file", required=True)
    items.add_argument("--manifest-file", required=True)
    items.add_argument("--out-items", required=True)
    items.add_argument("--report-out", required=True)
    items.set_defaults(handler=_command_validate_items)

    classify = subparsers.add_parser("classify", help="classify one finding")
    classify.add_argument("--finding-file", required=True)
    classify.add_argument("--manifest-file", required=True)
    classify.set_defaults(handler=_command_classify)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (EvidenceError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"pr_agent_evidence: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
