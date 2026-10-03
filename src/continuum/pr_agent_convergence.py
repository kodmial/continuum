"""Deterministic PR-Agent convergence state for Work Lock #39.

This module owns Continuum's local no-progress episode metadata only.  It never
changes or fabricates upstream PR-Agent ACTIVE/RESOLVED state.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# Location/ranking metadata can legitimately move after a repair commit while
# the logical finding remains unchanged.  Content/path fields are retained.
_VOLATILE_KEYS = frozenset({
    "line", "line_number", "start_line", "end_line", "position",
    "start_position", "end_position", "rank", "score",
})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NO_PROGRESS_RE = re.compile(
    r"<!-- continuum-pr-agent-no-progress head=([0-9a-f]{40}) fingerprint=([0-9a-f]{64}) -->"
)
TRANSITION_RE = re.compile(
    r"<!-- continuum-pr-agent-convergence from=([0-9a-f]{40}) to=([0-9a-f]{40}) "
    r"fingerprint=([0-9a-f]{64}) -->"
)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _canonical(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k).lower() not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def logical_fingerprint(items: Sequence[Mapping[str, Any]]) -> str:
    """Stable logical identity derived only from structured PR-Agent items."""
    canonical = [
        json.dumps(_canonical(item), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for item in items
    ]
    canonical.sort()
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Transition:
    from_head: str
    to_head: str
    fingerprint: str


def transition_marker(from_head: str, to_head: str, fingerprint: str) -> str:
    for name, value in (("from_head", from_head), ("to_head", to_head)):
        if not _SHA_RE.fullmatch((value or "").lower()):
            raise ValueError(f"{name} must be a full commit SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", (fingerprint or "").lower()):
        raise ValueError("fingerprint must be sha256")
    return (
        f"<!-- continuum-pr-agent-convergence from={from_head.lower()} "
        f"to={to_head.lower()} fingerprint={fingerprint.lower()} -->"
    )


def parse_transitions(comment_bodies: Sequence[str]) -> list[Transition]:
    out: list[Transition] = []
    for body in comment_bodies:
        for match in TRANSITION_RE.finditer(body or ""):
            out.append(Transition(*match.groups()))
    return out


def should_hold(
    *,
    head_sha: str,
    fingerprint: str,
    comment_bodies: Sequence[str],
) -> bool:
    """Hold same-head no-diff or same logical finding after our repair HEAD."""
    head = (head_sha or "").lower()
    fp = (fingerprint or "").lower()
    if not _SHA_RE.fullmatch(head) or not re.fullmatch(r"[0-9a-f]{64}", fp):
        raise ValueError("invalid convergence identity")

    same_head = f"<!-- continuum-pr-agent-no-progress head={head} fingerprint={fp} -->"
    if any(same_head in (body or "") for body in comment_bodies):
        return True

    return any(
        transition.to_head == head and transition.fingerprint == fp
        for transition in parse_transitions(comment_bodies)
    )
