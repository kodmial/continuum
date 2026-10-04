"""One priority contract for local and delegated issues.

The local issue scheduler migrates a legacy ``P0:`` / ``P1:`` / ``P2:``
title prefix into the authoritative ``priority:p0/p1/p2`` label before
admission and ranking. The delegated child queue must apply the same
normalization so a ``P1: ...`` child task with no label is not silently
ranked as unprioritized (``rank=3``) only because it lives in a child
repository.

Priority labels remain authoritative: an explicit label always wins, a
title never overrides a label, multiple labels resolve deterministically
to the highest priority, and issues with neither a recognized prefix nor
a label stay unprioritized. This module is the executable contract that
the workflow ``jq``/``github-script`` embeddings mirror byte-for-byte in
semantics (same ``^P([0-2]):\\s+`` case-insensitive prefix rule).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple

PRIORITY_ORDER = ("priority:p0", "priority:p1", "priority:p2")
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITY_ORDER)}
UNPRIORITIZED_RANK = len(PRIORITY_ORDER)

# Same rule as the local scheduler's ``/^P([0-2]):\\s+/i``: exactly one
# leading legacy prefix, case-insensitive, with at least one whitespace
# character after the colon. ``P1:hello`` (no space) is not recognized,
# matching the local scheduler.
LEGACY_TITLE_RE = re.compile(r"^P([0-2]):\s+", re.IGNORECASE)


def _label_priority(labels: Iterable[object]) -> Optional[str]:
    """Highest-priority explicit label, or None.

    Multiple priority labels resolve deterministically to the highest
    priority (p0 beats p1 beats p2), matching the scheduler.
    """
    found = [str(label) for label in labels or [] if isinstance(label, str)]
    for candidate in PRIORITY_ORDER:
        if candidate in found:
            return candidate
    return None


def title_priority(title: object) -> Optional[str]:
    """Priority implied by a legacy title prefix, or None."""
    if not isinstance(title, str):
        return None
    match = LEGACY_TITLE_RE.match(title)
    if not match:
        return None
    return "priority:p" + match.group(1).lower()


def effective_priority(
    labels: Iterable[object], title: object
) -> Tuple[Optional[str], int]:
    """Resolve ``(priority, rank)`` with labels authoritative.

    - Explicit priority labels always win (highest wins on multiples).
    - A legacy title prefix applies only when no priority label exists.
    - No recognized prefix and no label stays unprioritized (rank 3);
      no default priority is introduced.
    """
    label = _label_priority(labels)
    if label is not None:
        return label, PRIORITY_RANK[label]
    titled = title_priority(title)
    if titled is not None:
        return titled, PRIORITY_RANK[titled]
    return None, UNPRIORITIZED_RANK


def labels_to_add(labels: Iterable[object], title: object) -> list:
    """Labels needed to persist title normalization, idempotently.

    Returns ``[priority]`` when exactly one legacy title priority is
    present and no priority label exists yet; otherwise ``[]``. Adding
    the returned labels is idempotent and never duplicates: a second
    call observes the persisted label and returns ``[]``.
    """
    current = _label_priority(labels)
    if current is not None:
        return []
    titled = title_priority(title)
    if titled is None:
        return []
    existing = [label for label in (labels or []) if isinstance(label, str)]
    if titled in existing:
        return []
    return [titled]
