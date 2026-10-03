"""PR-Agent cross-HEAD convergence state machine.

Identity is derived only from structured PR-Agent findings.  Workflow-owned
markers carry episode transitions; comment prose is never parsed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

_VOLATILE_KEYS = frozenset({
    "line", "line_number", "start_line", "end_line", "position",
    "start_position", "end_position", "rank", "score", "index",
})
_SHA = r"[0-9a-f]{40}"
_FP = r"[0-9a-f]{64}"
NO_PROGRESS_RE = re.compile(
    rf"<!-- continuum-pr-agent-no-progress head=({_SHA}) fingerprint=({_FP}) -->"
)
TRANSITION_RE = re.compile(
    rf"<!-- continuum-pr-agent-convergence from=({_SHA}) to=({_SHA}) "
    rf"findings=([0-9a-f,]+) -->"
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


def finding_fingerprint(item: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _canonical(item), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def finding_fingerprints(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({finding_fingerprint(item) for item in items}))


def batch_fingerprint(items: Sequence[Mapping[str, Any]]) -> str:
    """Compatible set-level token for same-HEAD/no-diff state."""
    fps = finding_fingerprints(items)
    return hashlib.sha256(
        json.dumps(list(fps), separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Transition:
    from_head: str
    to_head: str
    findings: frozenset[str]


def transition_marker(
    from_head: str, to_head: str, finding_ids: Iterable[str]
) -> str:
    ids = sorted(set(finding_ids))
    if not re.fullmatch(_SHA, from_head.lower()) or not re.fullmatch(_SHA, to_head.lower()):
        raise ValueError("transition heads must be full commit SHAs")
    if not ids or any(not re.fullmatch(_FP, fp.lower()) for fp in ids):
        raise ValueError("transition requires valid logical finding fingerprints")
    return (
        f"<!-- continuum-pr-agent-convergence from={from_head.lower()} "
        f"to={to_head.lower()} findings={','.join(fp.lower() for fp in ids)} -->"
    )


def parse_transitions(bodies: Sequence[str]) -> list[Transition]:
    result = []
    for body in bodies:
        for match in TRANSITION_RE.finditer(body or ""):
            source, target, raw = match.groups()
            result.append(Transition(source, target, frozenset(raw.split(","))))
    return result


def surviving_findings(
    *, head_sha: str, current_finding_ids: Iterable[str], comment_bodies: Sequence[str]
) -> frozenset[str]:
    """Logical findings that survived our immediately preceding repair."""
    head = head_sha.lower()
    current = frozenset(current_finding_ids)
    if not re.fullmatch(_SHA, head):
        raise ValueError("head_sha must be a full commit SHA")
    if any(not re.fullmatch(_FP, fp) for fp in current):
        raise ValueError("invalid finding fingerprint")
    prior = set()
    for transition in parse_transitions(comment_bodies):
        if transition.to_head == head:
            prior.update(transition.findings)
    return frozenset(prior & current)


def should_hold(
    *, head_sha: str, batch_fp: str, current_finding_ids: Iterable[str],
    comment_bodies: Sequence[str]
) -> bool:
    """Hold same-HEAD no-diff or any logical finding surviving our repair."""
    head = head_sha.lower()
    fp = batch_fp.lower()
    if not re.fullmatch(_SHA, head) or not re.fullmatch(_FP, fp):
        raise ValueError("invalid convergence identity")
    same = f"<!-- continuum-pr-agent-no-progress head={head} fingerprint={fp} -->"
    if any(same in (body or "") for body in comment_bodies):
        return True
    return bool(surviving_findings(
        head_sha=head, current_finding_ids=current_finding_ids,
        comment_bodies=comment_bodies,
    ))
