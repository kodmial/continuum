"""PR-Agent cross-HEAD convergence state machine.

Identity is derived only from structured PR-Agent findings. Workflow-owned
markers carry repair-generated HEAD transitions; comment prose is presentation
only and upstream ACTIVE/RESOLVED state is never fabricated here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

_VOLATILE_KEYS = frozenset({
    "index", "line", "line_number", "start_line", "end_line", "position",
    "start_position", "end_position", "rank", "score",
})
_SHA = r"[0-9a-f]{40}"
_FP = r"[0-9a-f]{64}"
NO_PROGRESS_RE = re.compile(
    rf"<!-- continuum-pr-agent-no-progress head=({_SHA}) fingerprint=({_FP}) -->"
)
TRANSITION_RE = re.compile(
    rf"<!-- continuum-pr-agent-convergence from=({_SHA}) to=({_SHA}) "
    rf"findings=({_FP}(?:,{_FP})*) -->"
)


def _full_canonical(value: Any) -> Any:
    """#33 canonicalization used by the existing same-HEAD batch marker."""
    if isinstance(value, Mapping):
        return {
            str(key): _full_canonical(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, (list, tuple)):
        return [_full_canonical(item) for item in value]
    return value


def _logical_canonical(value: Any) -> Any:
    """Canonical logical identity, excluding location/ranking-only metadata."""
    if isinstance(value, Mapping):
        return {
            str(key): _logical_canonical(value[key])
            for key in sorted(value, key=str)
            if str(key).lower() not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_logical_canonical(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip())
    return value


def batch_fingerprint(items: Sequence[Mapping[str, Any]]) -> str:
    """Exact #33-compatible fingerprint for same-HEAD no-progress markers."""
    canonical = [
        json.dumps(
            _full_canonical(item),
            sort_keys=False,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for item in items
    ]
    canonical.sort()
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def finding_fingerprint(item: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _logical_canonical(item),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def finding_fingerprints(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({finding_fingerprint(item) for item in items}))


@dataclass(frozen=True)
class Transition:
    from_head: str
    to_head: str
    findings: frozenset[str]


@dataclass(frozen=True)
class ConvergenceDecision:
    same_head_hold: bool
    surviving: frozenset[str]
    eligible: frozenset[str]

    @property
    def held(self) -> bool:
        return self.same_head_hold or not self.eligible


def transition_marker(
    from_head: str, to_head: str, finding_ids: Iterable[str]
) -> str:
    ids = sorted({str(fp).lower() for fp in finding_ids})
    source = (from_head or "").lower()
    target = (to_head or "").lower()
    if not re.fullmatch(_SHA, source):
        raise ValueError("from_head must be a full commit SHA")
    if not re.fullmatch(_SHA, target):
        raise ValueError("to_head must be a full commit SHA")
    if source == target:
        raise ValueError("transition requires distinct from_head and to_head")
    if not ids or any(not re.fullmatch(_FP, fp) for fp in ids):
        raise ValueError("transition requires valid logical finding fingerprints")
    return (
        f"<!-- continuum-pr-agent-convergence from={from_head.lower()} "
        f"to={to_head.lower()} findings={','.join(ids)} -->"
    )


def parse_transitions(bodies: Sequence[str]) -> list[Transition]:
    result: list[Transition] = []
    for body in bodies:
        for match in TRANSITION_RE.finditer((body or "").lower()):
            source, target, raw = match.groups()
            if source == target:
                continue
            findings = frozenset(part for part in raw.split(",") if part)
            result.append(Transition(source, target, findings))
    return result


def decide(
    *,
    head_sha: str,
    batch_fp: str,
    current_finding_ids: Iterable[str],
    comment_bodies: Sequence[str],
) -> ConvergenceDecision:
    """Return held survivors and findings still eligible for one repair pass.

    A transition applies only when its target SHA is the exact current HEAD, so
    genuine user/new-work commits invalidate stale convergence state. Findings
    that survived a workflow-generated repair stay held, while new/materially
    changed findings on the same generated HEAD remain eligible.
    """
    head = (head_sha or "").lower()
    fp = (batch_fp or "").lower()
    current = frozenset(str(item).lower() for item in current_finding_ids)
    if not re.fullmatch(_SHA, head) or not re.fullmatch(_FP, fp):
        raise ValueError("invalid convergence identity")
    if not current or any(not re.fullmatch(_FP, item) for item in current):
        raise ValueError("invalid logical finding identity")

    same_marker = (
        f"<!-- continuum-pr-agent-no-progress head={head} fingerprint={fp} -->"
    )
    same_head_hold = any(same_marker in (body or "").lower() for body in comment_bodies)

    prior: set[str] = set()
    for transition in parse_transitions(comment_bodies):
        if transition.to_head == head and transition.from_head != head:
            prior.update(transition.findings)
    surviving = frozenset(prior & current)
    eligible = frozenset() if same_head_hold else frozenset(current - surviving)
    return ConvergenceDecision(same_head_hold, surviving, eligible)
