"""Provider snapshot: the only shape the generic gate consumes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

SummaryParser = Callable[[str], Sequence[str]]


@dataclass
class ProviderSnapshot:
    """What one review provider reports about a pull request right now.

    Everything here is data, not behavior: the gate decides the verdict. This
    is the boundary that keeps provider-specific parsing out of merge logic.
    """

    provider: str
    bot_logins: Tuple[str, ...] = ()
    review_bots: Tuple[str, ...] = ()
    output_markers: Tuple[str, ...] = ()
    summary_comments: List[Dict[str, Any]] = field(default_factory=list)
    review_comments: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_ids: Optional[Set[int]] = None
    summary_parser: Optional[SummaryParser] = None
    is_boilerplate: Optional[Callable[[str], bool]] = None
    extra_reason: Optional[str] = None
    forced_open_titles: List[str] = field(default_factory=list)
    provider_decision: Optional[str] = None
    # Comment ids in threads the provider explicitly declared resolved, keyed by
    # id. The gate drops those findings and normalizes GitHub's thread state
    # rather than re-requesting verification of something already fixed. An
    # explicit `unresolved` is never in this set: a refusal always blocks.
    superseded_thread_ids: Set[int] = field(default_factory=set)
    # Publication time (epoch ms) of the newest decisive approval on this head.
    # Provider output older than it on the same head was advisory and has been
    # adjudicated; output newer than it has not.
    approved_at_ms: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "summary_comments": len(self.summary_comments),
            "review_comments": len(self.review_comments),
            "thread_state_known": self.unresolved_ids is not None,
            "provider_decision": self.provider_decision,
            "superseded_threads": len(self.superseded_thread_ids),
            "approved_at_ms": self.approved_at_ms,
            "extra_reason": self.extra_reason,
            "metadata": dict(self.metadata),
        }
