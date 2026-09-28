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
    metadata: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "summary_comments": len(self.summary_comments),
            "review_comments": len(self.review_comments),
            "thread_state_known": self.unresolved_ids is not None,
            "provider_decision": self.provider_decision,
            "extra_reason": self.extra_reason,
            "metadata": dict(self.metadata),
        }
