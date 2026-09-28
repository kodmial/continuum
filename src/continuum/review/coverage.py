"""Review coverage accounting.

Zero findings only means "clean" when the whole current PR HEAD was actually
reviewed. Chunking is token-based and large-patch clipping can omit content
without saying so, so an unproven diff never earns a clean gate.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

# Phrases in provider output that suggest chunk limits or patch clipping left
# part of the diff unreviewed. Matched case-insensitively.
COVERAGE_GAP_PHRASES = (
    "remaining files",
    "not reviewed",
    "unreviewed",
    "truncated",
    "clipped",
    "chunk limit",
    "max_number_of_calls",
    "max number of calls",
    "partial review",
    "incomplete coverage",
    "coverage gap",
    "not covered",
    "omitted files",
    "skipped files",
)

# Heuristic budgets mirroring the pinned packer configuration. Chunking is
# token-based, not file-based, so these are the documented floors at which a
# silent gap becomes likely.
CHUNK_FILE_BUDGET = 5
CHUNK_CHURN_BUDGET = 15000
CLIP_FILE_CHURN_BUDGET = 10000

# A clean gate without positive coverage evidence is only defensible for a
# trivially small diff. Generic "review complete" style text is not tied to the
# current HEAD run or to the content the packer processed, so it is never
# accepted as coverage proof.
SAFE_APPROVE_MAX_FILES = 1
SAFE_APPROVE_MAX_CHURN = 500


def _churn(entry: Dict[str, Any]) -> int:
    additions = entry.get("additions") or 0
    deletions = entry.get("deletions") or 0
    try:
        return int(additions) + int(deletions)
    except (TypeError, ValueError):
        return 0


def total_churn(files: Sequence[Dict[str, Any]]) -> int:
    return sum(_churn(entry) for entry in files or [])


def evaluate_coverage(
    bodies: Iterable[str],
    files: Sequence[Dict[str, Any]],
    *,
    current_head: Optional[str] = None,
    provider_output: bool = True,
    chunk_file_budget: int = CHUNK_FILE_BUDGET,
    chunk_churn_budget: int = CHUNK_CHURN_BUDGET,
    clip_churn_budget: int = CLIP_FILE_CHURN_BUDGET,
    safe_approve_max_files: int = SAFE_APPROVE_MAX_FILES,
    safe_approve_max_churn: int = SAFE_APPROVE_MAX_CHURN,
) -> Optional[str]:
    """Return a reason string when coverage is incomplete, else None.

    `bodies` must be the provider output selected for the *current* HEAD run.
    Historical output never establishes coverage.
    """

    body_list: List[str] = [body or "" for body in bodies]
    file_list = list(files or [])

    if current_head and file_list and not provider_output:
        return (
            "no review output was found for the current HEAD, so chunk limits or "
            "large-patch clipping may have left content unreviewed; zero findings "
            "cannot be treated as a clean full-PR review"
        )

    haystack = "\n".join(body_list).lower()
    for phrase in COVERAGE_GAP_PHRASES:
        if phrase in haystack:
            return (
                f"review output mentions {phrase!r}, so chunk limits or "
                "large-patch clipping may have left content unreviewed"
            )

    if len(file_list) > chunk_file_budget:
        largest = sorted(file_list, key=_churn, reverse=True)[:3]
        names = ", ".join(str(entry.get("filename") or "?") for entry in largest)
        return (
            f"PR touches {len(file_list)} files, exceeding the "
            f"{chunk_file_budget}-call chunk budget; remaining files may not have "
            f"been reviewed (largest: {names})"
        )

    clipped = [entry for entry in file_list if _churn(entry) > clip_churn_budget]
    if clipped:
        names = ", ".join(str(entry.get("filename") or "?") for entry in clipped[:3])
        return (
            f"oversized patch(es) ({names}) exceed the large-patch clip budget and "
            "part of the diff may have been omitted"
        )

    churn = total_churn(file_list)
    if churn > chunk_churn_budget:
        return (
            f"PR churn (+{churn} lines) exceeds the {chunk_churn_budget}-line "
            "chunking budget; some chunks may not have been reviewed"
        )

    if file_list and (len(file_list) > safe_approve_max_files or churn > safe_approve_max_churn):
        return (
            f"PR touches {len(file_list)} file(s) with +{churn} changed lines; "
            "chunking is token-based, so a chunk budget and large-patch clipping "
            "may leave content unreviewed even below the heuristic budgets; "
            "generic full-coverage phrases in the review output are not tied to the "
            "current HEAD run or the packer content and cannot prove complete "
            "coverage; zero findings cannot be treated as a clean full-PR review"
        )
    return None


def coverage_record(reason: Optional[str]) -> Dict[str, Any]:
    return {"complete": reason is None, "reason": reason}
