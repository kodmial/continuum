"""Authoritative Render execution failure classification (issue #263).

The Render execution controller used to treat every failed execution as a
repository defect and create an automatic P0 repair issue, even when live
telemetry proved a hard worker-capacity limit (Render Free cgroup ~512 MiB,
``memory.current`` pinned at the limit, repeated worker replacements,
controller-reported agent peak above the envelope). That looped forever:
capacity failure -> repository repair -> unpause/retry -> same capacity
failure, occupying scheduler WIP slots indefinitely.

This module owns the deterministic rules behind the recovery route so the
controller classifies *before* it repairs. A proven capacity/memory-envelope
failure never creates or keeps a repository-code repair task unless evidence
identifies an actual repository defect.

Failure classes
---------------
* ``pass`` — execution and mandatory cleanup succeeded.
* ``capacity`` — worker capacity / memory-envelope failure (pinned memory,
  repeated replacement/restart, workload peak above the envelope).
* ``repository`` — code/harness defect a repository change can plausibly fix.
* ``transient`` — transient Render/API/network infrastructure failure with
  bounded automatic recovery.
* ``provider`` — external provider/model failure (not fixable in-repo).
* ``timeout_unknown`` — timeout with insufficient evidence to route elsewhere.
* ``unknown`` — fail-closed hold: no evidence for any other class, so no
  repository repair is created.

Evidence
--------
Only durable machine evidence the Render harness already produces is read:
the run result JSON, the cgroup memory summary, the lifecycle state file
(worker replacement/restart evidence), the execute/cleanup outcomes, and the
optional consumer qualification payload. Prose never classifies; only parsed
evidence does. Missing or unparsable evidence fails closed to ``unknown``
(hold), never to ``repository``.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: Failure classes produced by :func:`classify_render_failure`.
PASS = "pass"
CAPACITY = "capacity"
REPOSITORY = "repository"
TRANSIENT = "transient"
PROVIDER = "provider"
TIMEOUT_UNKNOWN = "timeout_unknown"
UNKNOWN = "unknown"

FAILURE_CLASSES = (
    PASS,
    CAPACITY,
    REPOSITORY,
    TRANSIENT,
    PROVIDER,
    TIMEOUT_UNKNOWN,
    UNKNOWN,
)

#: Recovery actions produced by :func:`decide_render_recovery`.
ACTION_PASS = "pass"
ACTION_HOLD_CAPACITY = "hold_capacity"
ACTION_REPAIR_REPOSITORY = "repair_repository"
ACTION_RETRY_TRANSIENT = "retry_transient"
ACTION_HOLD_PROVIDER = "hold_provider"
ACTION_HOLD_TIMEOUT = "hold_timeout"
ACTION_HOLD_UNKNOWN = "hold_unknown"

#: Memory at or above this fraction of the cgroup limit counts as pinned
#: at/near the limit (for example 460 MiB+ of a 512 MiB envelope).
PINNED_RATIO = 0.9

#: Peak at or above this fraction of the limit counts as near-limit pressure
#: when combined with a restart storm or OOM pressure events.
NEAR_LIMIT_RATIO = 0.95

#: Repeated worker replacement/restart: this many observed restarts or
#: replacements prove a storm rather than a single unlucky eviction.
RESTART_STORM_THRESHOLD = 2

#: Machine-readable hold marker persisted on the source issue for a terminal
#: capacity classification. The scheduler and the controller match on it.
CAPACITY_HOLD_MARKER_PREFIX = "<!-- continuum-render-capacity-hold"

#: Machine-readable fingerprint marker persisted per classification so
#: identical failures coalesce instead of spawning repeated tasks.
FAILURE_FINGERPRINT_MARKER_PREFIX = "<!-- continuum-render-failure-fingerprint="

#: Machine-readable transient-retry marker counted towards the bounded
#: infrastructure budget.
TRANSIENT_RETRY_MARKER_PREFIX = "<!-- continuum-render-transient-attempt"

#: Marker the controller writes when it creates a repository repair issue.
REPAIR_ATTEMPT_MARKER = "<!-- continuum-render-repair-attempt -->"

#: Bounded automatic recovery budget for transient infrastructure failures.
#: Mirrors the lifecycle transient budget so one flapping worker cannot retry
#: forever.
MAX_TRANSIENT_RECOVERY_ATTEMPTS = 10

_BYTE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)?\s*$", re.IGNORECASE)

_LIMIT_KEYS = (
    "limit_bytes",
    "memory_limit_bytes",
    "cgroup_limit_bytes",
    "memory_max_bytes",
    "max_bytes",
    "limit",
    "memory_limit",
    "hierarchical_memory_limit",
    "memory.max",
    "memory_max",
)

_CURRENT_KEYS = (
    "memory_current_bytes",
    "current_bytes",
    "memory_current",
    "current",
    "memory.current",
    "usage_bytes",
    "memory_usage_bytes",
)

_PEAK_KEYS = (
    "memory_peak_bytes",
    "peak_bytes",
    "memory_peak",
    "peak",
    "memory.peak",
    "max_usage_bytes",
    "memory_max_usage_bytes",
    "agent_peak_bytes",
    "agent_peak",
    "peak_rss_bytes",
)

_RESTART_KEYS = (
    "restarts",
    "restart_count",
    "worker_restarts",
    "worker_replacements",
    "replacements",
    "instance_replacements",
    "worker_instance_replacements",
    "container_restarts",
    "oom_kills",
    "oom_kill_count",
)

_PROFILE_KEYS = (
    "profile",
    "profile_hash",
    "workload_profile",
    "runtime_profile",
    "workload",
    "artifact_sha256",
    "artifact_id",
    "binary_sha256",
    "image",
    "model",
)

_TRANSIENT_TOKENS = (
    "econnreset",
    "econnrefused",
    "eai_again",
    "epipe",
    "socket hang up",
    "network error",
    "network failure",
    "network timeout",
    "network unreachable",
    "dns error",
    "dns failure",
    "dns resolution",
    "tls handshake",
    "tls error",
    "ssl error",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "bootstrap failed",
    "bootstrap error",
    "runner evicted",
    "runner cancelled",
    "runner timeout",
    "runner lost",
    "run evicted",
    "run cancelled",
    "fetch failed",
    "render api",
    "api error",
    "api timeout",
    "api unavailable",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "retry after",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status 429",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
)

# Bare `timeout`/`timed out` alone never proves infrastructure: only a
# qualified infrastructure timeout (network/runner/socket/dns/tls/gateway/
# http/request/render/api/...) retries through the bounded transient budget.
_TIMEOUT_INFRA_QUALIFIERS = (
    "read",
    "connection",
    "network",
    "runner",
    "socket",
    "dns",
    "tls",
    "ssl",
    "gateway",
    "http",
    "request",
    "render",
    "api",
    "operation",
    "bootstrap",
    "provider",
)

_PROVIDER_TOKENS = (
    "model overloaded",
    "model unavailable",
    "provider error",
    "provider overload",
    "provider timeout",
    "upstream model",
    "invalid model",
    "model not found",
    "unknown model",
    "context length",
    "context window",
    "max tokens",
    "quota exceeded",
    "insufficient quota",
    "billing",
    "credit balance",
    "unauthorized model",
)

# Evidence of a code/harness defect a repository change can plausibly fix.
# Kept narrow on purpose: generic "execution failed" prose never matches, so
# an evidence-free failure fails closed to hold instead of repair.
_REPOSITORY_TOKENS = (
    "assertion failed",
    "assertionerror",
    "test failure",
    "tests failed",
    "test defect",
    "failing test",
    "pytest",
    "exit code 1",
    "exit_code 1",
    "traceback",
    "typeerror",
    "referenceerror",
    "syntaxerror",
    "lint error",
    "type error",
    "compilation failed",
    "build failed",
    "harness error",
    "script failed",
    "job script failed",
)


class RenderExecutionError(ValueError):
    """Raised when render execution inputs cannot be interpreted safely."""


@dataclass(frozen=True)
class RenderFailureClassification:
    """One deterministic classification of a Render execution failure."""

    classification: str
    subtype: str
    reason: str
    fingerprint: str
    limit_bytes: Optional[int] = None
    peak_bytes: Optional[int] = None
    restarts: int = 0


@dataclass(frozen=True)
class RenderRecoveryDecision:
    """One deterministic recovery route for a classified Render failure."""

    action: str
    reason: str
    fingerprint: str
    classification: str
    create_repair: bool = False
    allow_retry: bool = False
    release_wip: bool = True
    cleanup_ok: bool = True
    hold_marker: Optional[str] = None


def _parse_bytes(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number >= 0 else None
    text = str(value).strip().lower().replace(",", "").replace("_", "")
    if not text or text in ("unknown", "none", "null", "unlimited", "max"):
        return None
    plain = re.fullmatch(r"\d+", text)
    if plain:
        return int(text)
    match = _BYTE_RE.match(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "").lower()
    factor = 1
    if unit.startswith("k"):
        factor = 1024
    elif unit.startswith("m"):
        factor = 1024 * 1024
    elif unit.startswith("g"):
        factor = 1024 * 1024 * 1024
    elif unit.startswith("t"):
        factor = 1024 * 1024 * 1024 * 1024
    return int(amount * factor)


def _walk_values(node: object, depth: int = 0):
    if depth > 6:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield key, value
            yield from _walk_values(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_values(item, depth + 1)


def _find_first_bytes(payload: object, keys: Sequence[str]) -> Optional[int]:
    if not isinstance(payload, Mapping):
        return None
    wanted = {key.lower() for key in keys}
    for key, value in _walk_values(payload):
        if str(key).strip().lower() in wanted:
            parsed = _parse_bytes(value)
            if parsed is not None:
                return parsed
    return None


def _count_restarts(*payloads: object) -> int:
    total = 0
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        wanted = {key.lower() for key in _RESTART_KEYS}
        for key, value in _walk_values(payload):
            if str(key).strip().lower() not in wanted:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                total += max(0, int(value))
            elif isinstance(value, (list, tuple)):
                total += len(value)
    return total


def _distinct_instance_ids(*payloads: object) -> int:
    ids = set()
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key, value in _walk_values(payload):
            name = str(key).strip().lower()
            if name not in (
                "instance_id",
                "instance_ids",
                "worker_id",
                "worker_ids",
            ):
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    text = str(item).strip()
                    if text:
                        ids.add(text)
            else:
                text = str(value).strip()
                if text and text.lower() not in ("unknown", "none", "null"):
                    ids.add(text)
    return len(ids)


def _memory_events(payload: object) -> Dict[str, int]:
    events: Dict[str, int] = {"high": 0, "max": 0, "oom": 0, "oom_kill": 0}
    if not isinstance(payload, Mapping):
        return events
    for key, value in _walk_values(payload):
        name = str(key).strip().lower()
        if name in ("high", "max", "oom", "oom_kill", "oom-kill", "oom_group_kill"):
            try:
                number = int(str(value).strip().split()[0])
            except (TypeError, ValueError):
                continue
            canonical = "oom_kill" if name.startswith("oom") and name != "oom" else name
            if canonical in events:
                events[canonical] = max(events[canonical], max(0, number))
            elif name == "oom_group_kill":
                events["oom_kill"] = max(events["oom_kill"], max(0, number))
    return events


def _text_evidence(*payloads: object) -> str:
    chunks = []
    for payload in payloads:
        if payload is None:
            continue
        if isinstance(payload, str):
            chunks.append(payload)
        else:
            try:
                chunks.append(json.dumps(payload, sort_keys=True, default=str))
            except (TypeError, ValueError):
                chunks.append(str(payload))
    return "\n".join(chunks).lower()


def _has_transient_signature(text: str) -> bool:
    if not text:
        return False
    for token in _TRANSIENT_TOKENS:
        if token in ("timeout", "timed out"):
            continue
        if token in text:
            return True
    if "timed out" in text or "timeout" in text:
        return any(qual in text for qual in _TIMEOUT_INFRA_QUALIFIERS)
    return False


def _has_provider_signature(text: str) -> bool:
    if not text:
        return False
    return any(token in text for token in _PROVIDER_TOKENS)


def _has_repository_signature(text: str) -> bool:
    if not text:
        return False
    return any(token in text for token in _REPOSITORY_TOKENS)


def _has_timeout_mention(text: str) -> bool:
    return bool(text) and ("timeout" in text or "timed out" in text)


def _profile_identity(*payloads: object) -> str:
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        wanted = {key.lower() for key in _PROFILE_KEYS}
        for key, value in _walk_values(payload):
            if str(key).strip().lower() in wanted:
                text = str(value).strip()
                if text and text.lower() not in ("unknown", "none", "null", ""):
                    return text[:128]
    return ""


def _error_key(text: str) -> str:
    for token in (
        "oom",
        "memory",
        "restart",
        "replacement",
        "evict",
        "timeout",
        "rate limit",
        "model",
        "provider",
        "assertion",
        "test",
        "traceback",
        "connection",
        "network",
        "dns",
        "tls",
        "gateway",
    ):
        if token in text:
            return token
    first_line = (text.strip().splitlines() or [""])[0]
    return re.sub(r"[^a-z0-9]+", "-", first_line.strip().lower())[:48] or "unclassified"


def _peak_band(limit_bytes: Optional[int], peak_bytes: Optional[int]) -> str:
    if not limit_bytes or limit_bytes <= 0 or not peak_bytes:
        return "unknown"
    ratio = peak_bytes / limit_bytes
    if ratio > 1.0:
        return "over-limit"
    if ratio >= NEAR_LIMIT_RATIO:
        return "near-limit"
    if ratio >= PINNED_RATIO:
        return "pinned"
    return "healthy"


def _restart_band(restarts: int) -> str:
    if restarts <= 0:
        return "0"
    if restarts == 1:
        return "1"
    if restarts <= 3:
        return "2-3"
    return "4+"


def capacity_fingerprint(
    *,
    classification: str,
    limit_bytes: Optional[int] = None,
    peak_bytes: Optional[int] = None,
    restarts: int = 0,
    profile: str = "",
    error_key: str = "",
) -> str:
    """Stable fingerprint for one classified Render failure.

    Identical capacity failures coalesce onto one fingerprint, while a
    materially changed runtime artifact/profile or memory configuration
    produces a different fingerprint and may explicitly re-arm the source.
    The peak is banded (not exact) so two identical storms a few MiB apart
    still coalesce; the limit and profile are exact so a re-sized worker or
    a changed workload is a new fingerprint by construction.
    """

    canonical = {
        "classification": str(classification or "").strip().lower() or UNKNOWN,
        "limit_bytes": int(limit_bytes) if limit_bytes else 0,
        "peak_band": _peak_band(limit_bytes, peak_bytes),
        "restarts": _restart_band(int(restarts or 0)),
        "profile": str(profile or "").strip()[:128],
        "error": str(error_key or "").strip().lower()[:48] or "unclassified",
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def capacity_hold_marker(fingerprint: str, reason: str) -> str:
    """Machine-readable hold marker persisted for a capacity classification."""

    return (
        "<!-- continuum-render-capacity-hold "
        f"fingerprint={fingerprint} reason={_error_key(reason)} -->"
    )


def failure_fingerprint_marker(fingerprint: str) -> str:
    """Durable per-fingerprint marker used for coalescing identical failures."""

    return f"<!-- continuum-render-failure-fingerprint={fingerprint} -->"


def classify_render_failure(
    *,
    execute_outcome: str = "",
    cleanup_outcome: str = "",
    result: Optional[Mapping[str, Any]] = None,
    memory_summary: Optional[Mapping[str, Any]] = None,
    state: Optional[Mapping[str, Any]] = None,
    qualification: Optional[Mapping[str, Any]] = None,
) -> RenderFailureClassification:
    """Classify one Render execution from durable machine evidence.

    Capacity is evaluated before repository so a memory-envelope failure can
    never masquerade as a repository defect: a pinned cgroup plus a restart
    storm (or a workload peak above the envelope) classifies ``capacity``
    even when the result payload also mentions a failing script. Repository
    repair requires positive defect evidence with healthy memory; anything
    else fails closed to a hold class, never to ``repository``.
    """

    result = result if isinstance(result, Mapping) else {}
    memory_summary = memory_summary if isinstance(memory_summary, Mapping) else {}
    state = state if isinstance(state, Mapping) else {}
    qualification = qualification if isinstance(qualification, Mapping) else {}

    execute = str(execute_outcome or "").strip().lower()
    text = _text_evidence(result, memory_summary, state, qualification)

    limit = _find_first_bytes(memory_summary, _LIMIT_KEYS)
    if limit is None:
        limit = _find_first_bytes(result, _LIMIT_KEYS)
    if limit is None:
        limit = _find_first_bytes(state, _LIMIT_KEYS)
    if limit is None:
        limit = _find_first_bytes(qualification, _LIMIT_KEYS)
    current = _find_first_bytes(memory_summary, _CURRENT_KEYS)
    if current is None:
        current = _find_first_bytes(result, _CURRENT_KEYS)
    if current is None:
        current = _find_first_bytes(state, _CURRENT_KEYS)
    if current is None:
        current = _find_first_bytes(qualification, _CURRENT_KEYS)
    peak = _find_first_bytes(memory_summary, _PEAK_KEYS)
    if peak is None:
        peak = _find_first_bytes(result, _PEAK_KEYS)
    if peak is None:
        peak = _find_first_bytes(state, _PEAK_KEYS)
    if peak is None:
        peak = _find_first_bytes(qualification, _PEAK_KEYS)
    restarts = _count_restarts(result, memory_summary, state, qualification)
    instance_ids = _distinct_instance_ids(result, state)
    if instance_ids >= 3:
        restarts = max(restarts, instance_ids - 1)
    # Textual storm evidence ("restart storm", repeated replacement notes)
    # counts when structured counters are absent. Only string values are
    # inspected so JSON key names alone cannot fabricate a storm.
    if restarts == 0:
        value_chunks = []
        for _payload in (result, memory_summary, state, qualification):
            if isinstance(_payload, Mapping):
                for _key, _value in _walk_values(_payload):
                    if isinstance(_value, str):
                        value_chunks.append(_value.lower())
        _value_text = "\n".join(value_chunks)
        if "restart storm" in _value_text or _value_text.count("replacement") >= 2:
            restarts = RESTART_STORM_THRESHOLD
    events = _memory_events(memory_summary)
    for other in (result, state, qualification):
        other_events = _memory_events(other)
        for key, value in other_events.items():
            events[key] = max(events[key], value)
    pressure_events = events["max"] + events["oom"] + events["oom_kill"]

    pinned_ratio: Optional[float] = None
    if limit and limit > 0:
        candidates = [v for v in (peak, current) if isinstance(v, int) and v > 0]
        observed = max(candidates) if candidates else None
        if observed:
            pinned_ratio = observed / limit
    pinned = pinned_ratio is not None and pinned_ratio >= PINNED_RATIO
    near_limit = pinned_ratio is not None and pinned_ratio >= NEAR_LIMIT_RATIO
    peak_over_limit = bool(limit and limit > 0 and any(isinstance(v, int) and v > limit for v in (peak, current)))

    profile = _profile_identity(result, state, qualification)
    error = _error_key(text)

    def fingerprint_for(classification: str) -> str:
        return capacity_fingerprint(
            classification=classification,
            limit_bytes=limit,
            peak_bytes=peak,
            restarts=restarts,
            profile=profile,
            error_key=error,
        )

    if execute == "success" and str(cleanup_outcome or "").strip().lower() == "success":
        qual_class = str(
            qualification.get("classification")
            or qualification.get("result")
            or qualification.get("verdict")
            or ""
        ).strip().lower()
        if qual_class in ("pass", "passed", "success", "approved", ""):
            result_class = str(
                result.get("classification") or result.get("result") or ""
            ).strip().lower()
            if result_class in ("pass", "passed", "success", "approved", ""):
                return RenderFailureClassification(
                    PASS, "success", "execution and mandatory cleanup succeeded",
                    fingerprint_for(PASS), limit, peak, restarts,
                )

    # Capacity first: proven envelope pressure never masquerades as a
    # repository defect.
    if peak_over_limit:
        return RenderFailureClassification(
            CAPACITY, "peak-over-limit",
            f"workload peak ({peak} bytes) exceeds the worker envelope ({limit} bytes)",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )
    if pinned and restarts >= RESTART_STORM_THRESHOLD:
        return RenderFailureClassification(
            CAPACITY, "pinned-with-restart-storm",
            f"memory pinned at/near the cgroup limit with {restarts} worker restarts/replacements",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )
    if pinned and pressure_events > 0:
        return RenderFailureClassification(
            CAPACITY, "pinned-with-pressure-events",
            "memory pinned at/near the cgroup limit with OOM/max pressure events",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )
    if near_limit and restarts >= RESTART_STORM_THRESHOLD:
        return RenderFailureClassification(
            CAPACITY, "near-limit-with-restart-storm",
            f"memory near the cgroup limit with {restarts} worker restarts/replacements",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )
    consumer_capacity = str(
        qualification.get("classification") or ""
    ).strip().lower() == "memory"
    if consumer_capacity and (pinned or near_limit or peak_over_limit or pressure_events > 0):
        return RenderFailureClassification(
            CAPACITY, "consumer-memory-confirmed",
            "consumer qualification reports memory pressure confirmed by cgroup evidence",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )

    if _has_provider_signature(text):
        return RenderFailureClassification(
            PROVIDER, "provider-model",
            "external provider/model failure; not fixable by a repository change",
            fingerprint_for(PROVIDER), limit, peak, restarts,
        )
    if _has_transient_signature(text):
        return RenderFailureClassification(
            TRANSIENT, "infrastructure",
            "transient Render/API/network infrastructure failure",
            fingerprint_for(TRANSIENT), limit, peak, restarts,
        )
    if _has_repository_signature(text):
        # A repository repair requires healthy memory: pressure plus a test
        # mention stays capacity (handled above), so reaching here with a
        # defect signal and no pressure is evidence-driven repair.
        if pinned or near_limit or peak_over_limit or pressure_events > 0:
            return RenderFailureClassification(
                CAPACITY, "pressure-with-incidental-defect-text",
                "memory pressure dominates incidental defect text; no repository repair",
                fingerprint_for(CAPACITY), limit, peak, restarts,
            )
        return RenderFailureClassification(
            REPOSITORY, "code-harness-defect",
            "failure evidence indicates a code/harness defect a repository change can fix",
            fingerprint_for(REPOSITORY), limit, peak, restarts,
        )
    consumer_correctness = str(
        qualification.get("classification") or ""
    ).strip().lower() in ("correctness", "repository", "fail")
    if consumer_correctness and not (pinned or near_limit or peak_over_limit or pressure_events > 0):
        return RenderFailureClassification(
            REPOSITORY, "qualification-correctness",
            "qualification evidence indicates a correctness defect with healthy memory",
            fingerprint_for(REPOSITORY), limit, peak, restarts,
        )
    if consumer_correctness:
        return RenderFailureClassification(
            CAPACITY, "memory-pressure-over-qualification-verdict",
            "memory pressure dominates the qualification verdict; no repository repair",
            fingerprint_for(CAPACITY), limit, peak, restarts,
        )
    if _has_timeout_mention(text):
        return RenderFailureClassification(
            TIMEOUT_UNKNOWN, "timeout-insufficient-evidence",
            "timeout without sufficient evidence to route; holding without repository repair",
            fingerprint_for(TIMEOUT_UNKNOWN), limit, peak, restarts,
        )
    return RenderFailureClassification(
        UNKNOWN, "insufficient-evidence",
        "no class evidence; holding without repository repair instead of guessing",
        fingerprint_for(UNKNOWN), limit, peak, restarts,
    )


def decide_render_recovery(
    classification: RenderFailureClassification,
    *,
    cleanup_ok: bool = True,
    existing_repair_open: bool = False,
    same_fingerprint_seen: bool = False,
    transient_attempts: int = 0,
    max_transient_attempts: int = MAX_TRANSIENT_RECOVERY_ATTEMPTS,
) -> RenderRecoveryDecision:
    """Route one classified Render failure to its deterministic recovery.

    * ``capacity``/``provider``/``timeout``/``unknown`` hold: no repository
      repair is created, no blind rerun is scheduled, and WIP is released.
    * ``repository`` creates one evidence-driven repair issue (deduplicated).
    * ``transient`` retries automatically through a bounded budget without a
      repository repair issue.
    An identical capacity fingerprint coalesces: the second sighting holds
    without creating or waking anything new.
    """

    if not cleanup_ok:
        return RenderRecoveryDecision(
            ACTION_HOLD_UNKNOWN, "mandatory cleanup is not verified; holding",
            classification.fingerprint, classification.classification,
            create_repair=False, allow_retry=False, release_wip=True,
            cleanup_ok=False, hold_marker=None,
        )
    kind = str(classification.classification or "").strip().lower() or UNKNOWN
    if kind == PASS:
        return RenderRecoveryDecision(
            ACTION_PASS, "execution succeeded",
            classification.fingerprint, kind,
            create_repair=False, allow_retry=False, release_wip=True,
            cleanup_ok=True, hold_marker=None,
        )
    if kind == CAPACITY:
        if same_fingerprint_seen:
            return RenderRecoveryDecision(
                ACTION_HOLD_CAPACITY,
                "identical capacity fingerprint already recorded; coalescing without a new task",
                classification.fingerprint, kind,
                create_repair=False, allow_retry=False, release_wip=True,
                cleanup_ok=True,
                hold_marker=capacity_hold_marker(classification.fingerprint, classification.reason),
            )
        return RenderRecoveryDecision(
            ACTION_HOLD_CAPACITY, classification.reason,
            classification.fingerprint, kind,
            create_repair=False, allow_retry=False, release_wip=True,
            cleanup_ok=True,
            hold_marker=capacity_hold_marker(classification.fingerprint, classification.reason),
        )
    if kind == REPOSITORY:
        if existing_repair_open or same_fingerprint_seen:
            return RenderRecoveryDecision(
                ACTION_REPAIR_REPOSITORY,
                "repository repair already open for this source; no duplicate created",
                classification.fingerprint, kind,
                create_repair=False, allow_retry=False, release_wip=True,
                cleanup_ok=True, hold_marker=None,
            )
        return RenderRecoveryDecision(
            ACTION_REPAIR_REPOSITORY, classification.reason,
            classification.fingerprint, kind,
            create_repair=True, allow_retry=False, release_wip=True,
            cleanup_ok=True, hold_marker=None,
        )
    if kind == TRANSIENT:
        try:
            attempts = int(transient_attempts)
        except (TypeError, ValueError):
            attempts = 0
        try:
            budget = int(max_transient_attempts)
        except (TypeError, ValueError):
            budget = MAX_TRANSIENT_RECOVERY_ATTEMPTS
        budget = max(1, min(MAX_TRANSIENT_RECOVERY_ATTEMPTS, budget))
        if attempts >= budget:
            return RenderRecoveryDecision(
                ACTION_HOLD_UNKNOWN,
                f"transient recovery budget exhausted ({attempts}/{budget}); holding for inspection",
                classification.fingerprint, kind,
                create_repair=False, allow_retry=False, release_wip=True,
                cleanup_ok=True, hold_marker=None,
            )
        return RenderRecoveryDecision(
            ACTION_RETRY_TRANSIENT, classification.reason,
            classification.fingerprint, kind,
            create_repair=False, allow_retry=True, release_wip=True,
            cleanup_ok=True, hold_marker=None,
        )
    if kind == PROVIDER:
        return RenderRecoveryDecision(
            ACTION_HOLD_PROVIDER, classification.reason,
            classification.fingerprint, kind,
            create_repair=False, allow_retry=False, release_wip=True,
            cleanup_ok=True,
            hold_marker=capacity_hold_marker(classification.fingerprint, classification.reason),
        )
    if kind == TIMEOUT_UNKNOWN:
        return RenderRecoveryDecision(
            ACTION_HOLD_TIMEOUT, classification.reason,
            classification.fingerprint, kind,
            create_repair=False, allow_retry=False, release_wip=True,
            cleanup_ok=True,
            hold_marker=capacity_hold_marker(classification.fingerprint, classification.reason),
        )
    return RenderRecoveryDecision(
        ACTION_HOLD_UNKNOWN, classification.reason,
        classification.fingerprint, kind,
        create_repair=False, allow_retry=False, release_wip=True,
        cleanup_ok=True,
        hold_marker=capacity_hold_marker(classification.fingerprint, classification.reason),
    )


def counts_as_active_wip(
    *,
    has_in_progress_label: bool = False,
    has_active_run: bool = False,
    has_open_pr: bool = False,
    has_capacity_hold: bool = False,
) -> bool:
    """Whether a source still consumes an active scheduler WIP slot.

    A terminally classified capacity hold never counts: the controller
    removes the reservation label when it holds, and even a stale label is
    ignored once the hold marker exists, so a held source cannot occupy a
    WIP slot indefinitely.
    """

    if has_capacity_hold:
        return False
    return bool(has_in_progress_label or has_active_run or has_open_pr)


def may_rearm_capacity_hold(old_fingerprint: str, new_fingerprint: str) -> Tuple[bool, str]:
    """Whether a changed runtime/profile may explicitly re-arm a held source.

    Only a materially changed fingerprint (new runtime artifact/profile or
    memory configuration producing different classification inputs) re-arms;
    an identical fingerprint must keep coalescing instead of looping.
    """

    old = str(old_fingerprint or "").strip().lower()
    new = str(new_fingerprint or "").strip().lower()
    if not old or not new:
        return False, "missing-fingerprint"
    if old == new:
        return False, "identical-fingerprint-coalesces"
    return True, "materially-changed-evidence-rearms"


def has_capacity_hold_marker(body: object) -> bool:
    """Whether a body carries the machine-readable capacity hold marker."""

    return isinstance(body, str) and CAPACITY_HOLD_MARKER_PREFIX in body


def fingerprint_seen_in_bodies(bodies: Sequence[object], fingerprint: str) -> bool:
    """Whether an identical failure fingerprint was already recorded."""

    if not fingerprint:
        return False
    marker = f"<!-- continuum-render-failure-fingerprint={fingerprint} -->"
    for body in bodies:
        text = body if isinstance(body, str) else None
        if text is None and isinstance(body, Mapping):
            candidate = body.get("body")
            text = candidate if isinstance(candidate, str) else None
        if isinstance(text, str) and marker in text:
            return True
    return False


def _load_json_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI used by the Render execution controller for engine classification."""

    parser = argparse.ArgumentParser(description="Classify a Render execution failure.")
    parser.add_argument("--result", default="")
    parser.add_argument("--memory", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--qualification", default="")
    parser.add_argument("--execute-outcome", default="")
    parser.add_argument("--cleanup-outcome", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    classification = classify_render_failure(
        execute_outcome=args.execute_outcome,
        cleanup_outcome=args.cleanup_outcome,
        result=_load_json_file(args.result) if args.result else {},
        memory_summary=_load_json_file(args.memory) if args.memory else {},
        state=_load_json_file(args.state) if args.state else {},
        qualification=_load_json_file(args.qualification) if args.qualification else {},
    )
    payload = {
        "classification": classification.classification,
        "subtype": classification.subtype,
        "reason": classification.reason,
        "fingerprint": classification.fingerprint,
        "limit_bytes": classification.limit_bytes,
        "peak_bytes": classification.peak_bytes,
        "restarts": classification.restarts,
        "hold_marker": capacity_hold_marker(classification.fingerprint, classification.reason)
        if classification.classification
        in (CAPACITY, PROVIDER, TIMEOUT_UNKNOWN, UNKNOWN)
        else None,
        "failure_marker": failure_fingerprint_marker(classification.fingerprint),
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
