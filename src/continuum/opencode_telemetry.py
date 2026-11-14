"""Bounded, privacy-safe OpenCode per-attempt telemetry (kodmial/continuum#454).

The pinned OpenCode v2.2.6 ``run --format json`` event stream is line-delimited
JSON.  This module reads only allowlisted numeric/enumerated fields from that
stream and writes a small machine-readable summary suitable for a durable
GitHub Actions artifact.  It never stores prompts, tool inputs or outputs,
provider response bodies, free-form error text, credentials, or model messages.

Schema notes (verified against the pinned 2.2.6 binary and its emitted events):

* every record has ``type``, ``timestamp`` (epoch milliseconds), and
  ``sessionID``;
* ``step_start`` carries ``part.snapshot``;
* ``step_finish`` carries ``part.reason``, ``part.cost`` and
  ``part.tokens.{input,output,reasoning,cache.{read,write}}``; any of those
  values may be absent, so missing metrics are reported as ``unknown``;
* ``tool_use`` carries ``part.id`` (the stable part/call id; formal session
  parts use ``part.callID``), ``part.tool`` and ``part.state.status``; repeated
  state updates for one id are not unique calls;
* terminal ``error`` events carry a structured ``error`` object;
* ``retry`` records the scheduled retry attempt but is not treated as a new HTTP
  request, because the stream exposes no HTTP request counter;
* the 2.2.6 ``run --format json`` stream does not emit the selected
  provider/model per event, so provenance records the model supplied by the
  shared wrapper (the frozen route) instead of inventing one from event text.

The event stream is therefore *not* an HTTP request log.  A step is reported as
a step, a retry as a retry, and the provider request count is reported as
``unknown`` rather than inferred from step or ``[tool]`` line totals.

Finished attempts whose raw JSON stream was not retained cannot be
reconstructed.  They have no artifact here, or their summary reports
``unknown``/``missing_fields``; nothing is backfilled from log-line counts or
tool totals.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import math
import os
import re
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from continuum.opencode_429_recovery import (
    classify_opencode_error_event,
    classify_opencode_events,
    classify_opencode_github_events,
    describe_opencode_error,
)

SCHEMA_VERSION = 1
KIND = "continuum.opencode.attempt.telemetry"
UNKNOWN = "unknown"

#: Maximum per-step records retained in the artifact. Aggregate totals still
#: include every observed step; only the forensic detail list is bounded.
MAX_STEPS = 200
MAX_RETRIES = 100
MAX_SESSION_IDS = 32
MAX_TOOL_NAMES = 32

CLASSIFICATIONS = frozenset(
    {
        "quota_429",
        "unavailable",
        "upstream",
        "timeout",
        "fail_closed",
        "provider_error",
        "ok",
        "inconclusive",
    }
)
_PROVIDER_CLASSIFICATION_ORDER = (
    "quota_429",
    "fail_closed",
    "unavailable",
    "upstream",
    "timeout",
    "provider_error",
)
_STEP_REASONS = frozenset(
    {"stop", "length", "tool-calls", "content-filter", "error", "unknown"}
)
_TOOL_STATUSES = frozenset({"pending", "running", "completed", "error"})
_EVENT_TYPES = frozenset(
    {
        "step_start",
        "step_finish",
        "tool_use",
        "text",
        "reasoning",
        "error",
        "retry",
        "background_wait",
    }
)
# Tool names are enumerated because MCP/custom tools can be consumer-defined.
# Unknown names are bucketed as "other" rather than copied into the artifact.
_SAFE_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "bash",
        "edit",
        "glob",
        "grep",
        "list",
        "patch",
        "read",
        "shell",
        "skill",
        "task",
        "todoread",
        "todowrite",
        "webfetch",
        "websearch",
        "write",
    }
)
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
_TOKEN_KEYS = {
    "input_tokens": ("input",),
    "output_tokens": ("output",),
    "reasoning_tokens": ("reasoning",),
}
_CACHE_KEYS = {
    "cache_read_tokens": "read",
    "cache_write_tokens": "write",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#@+=-]{0,199}$")
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#@+=-]{0,199}$")
_SAFE_ERROR_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,120}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
# Workflow display names (``GITHUB_WORKFLOW``) are repository-owned labels and
# commonly contain spaces and punctuation (for example "OpenCode agent").
# Keep them useful while still rejecting control characters, newlines, and
# workflow-command injection characters.
_SAFE_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _()./-]{0,119}$")
_SAFE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_CORRELATION_KEYS = (
    "requestID",
    "requestId",
    "request_id",
    "correlationID",
    "correlationId",
    "correlation_id",
    "traceID",
    "traceId",
    "trace_id",
)


def _safe_token(value: Any) -> Any:
    if not isinstance(value, str):
        return UNKNOWN
    text = value.strip()
    return text if _SAFE_ID_RE.fullmatch(text) else UNKNOWN


def _safe_repository(value: Any) -> Any:
    if not isinstance(value, str):
        return UNKNOWN
    text = value.strip()
    return text if _SAFE_REPO_RE.fullmatch(text) else UNKNOWN


def _safe_model(value: Any) -> Any:
    if not isinstance(value, str):
        return UNKNOWN
    text = value.strip()
    return text if _SAFE_MODEL_RE.fullmatch(text) else UNKNOWN


def _safe_label(value: Any) -> Any:
    if not isinstance(value, str):
        return UNKNOWN
    text = value.strip()
    return text if _SAFE_LABEL_RE.fullmatch(text) else UNKNOWN


def _safe_workflow(value: Any) -> Any:
    if not isinstance(value, str):
        return UNKNOWN
    text = " ".join(value.split())
    return text if _SAFE_WORKFLOW_RE.fullmatch(text) else UNKNOWN


def _safe_enum(value: Any, allowed: Iterable[str], default: str = UNKNOWN) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _as_timestamp_ms(value: Any) -> Optional[int]:
    number = _as_int(value)
    if number is None:
        return None
    if number <= 0:
        return None
    # v2.2.6 uses Date.now() milliseconds.  Accept epoch seconds as a
    # defensive fallback for sanitized fixtures without inventing values.
    if number < 10_000_000_000:
        number *= 1000
    return number


def _iso_timestamp(value: Optional[int]) -> Any:
    if value is None:
        return UNKNOWN
    try:
        moment = datetime.fromtimestamp(value / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return UNKNOWN
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _number_or_unknown(value: Optional[float]) -> Any:
    if value is None:
        return UNKNOWN
    return value


def _event_kind(event: Mapping[str, Any]) -> str:
    kind = event.get("type")
    return kind if isinstance(kind, str) and kind in _EVENT_TYPES else "other"


def _event_part(event: Mapping[str, Any]) -> Mapping[str, Any]:
    part = event.get("part")
    return part if isinstance(part, Mapping) else {}


def _event_session(event: Mapping[str, Any]) -> Any:
    return _safe_token(event.get("sessionID"))


def _event_message(part: Mapping[str, Any]) -> Any:
    return _safe_token(part.get("messageID"))


def _tool_name(part: Mapping[str, Any]) -> str:
    name = part.get("tool")
    if isinstance(name, str) and name.lower() in _SAFE_TOOL_NAMES:
        return name.lower()
    return "other"


def _tool_status(part: Mapping[str, Any]) -> str:
    state = part.get("state")
    if not isinstance(state, Mapping):
        return UNKNOWN
    return _safe_enum(state.get("status"), _TOOL_STATUSES)


def _extract_usage(
    part: Mapping[str, Any],
) -> Tuple[Dict[str, Optional[int]], Optional[float], bool]:
    tokens = part.get("tokens")
    if not isinstance(tokens, Mapping):
        return {field: None for field in _TOKEN_FIELDS}, None, False
    values: Dict[str, Optional[int]] = {}
    for field, keys in _TOKEN_KEYS.items():
        values[field] = _as_int(tokens.get(keys[0]))
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, Mapping) else {}
    for field, key in _CACHE_KEYS.items():
        values[field] = _as_int(cache.get(key))
    present = any(value is not None for value in values.values())
    return values, _as_number(part.get("cost")), present


def _extract_error_attempt(error: Mapping[str, Any]) -> Optional[int]:
    attempt = _as_int(error.get("attempt"))
    if attempt is not None:
        return attempt
    data = error.get("data")
    if isinstance(data, Mapping):
        return _as_int(data.get("attempt"))
    return None


def _extract_correlation_id(error: Mapping[str, Any]) -> Any:
    for container in (error, error.get("data")):
        if not isinstance(container, Mapping):
            continue
        for key in _CORRELATION_KEYS:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return _safe_token(value)
    return UNKNOWN


def _strongest_provider_classification(classes: Sequence[str]) -> str:
    for candidate in _PROVIDER_CLASSIFICATION_ORDER:
        if candidate in classes:
            return candidate
    return "none"


def _safe_digits(value: Any) -> Any:
    if isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, int):
        return str(value) if value >= 0 else UNKNOWN
    return value if isinstance(value, str) and value.isdigit() else UNKNOWN


def _bounded_provenance(
    provenance: Any,
    *,
    model: Any,
    mode: str,
    generated_at: Optional[datetime],
) -> Dict[str, Any]:
    """Return only allowlisted provenance fields.

    A caller may pass its own mapping, so every field is re-validated instead
    of copying arbitrary keys (which could contain credentials or prose).
    """

    source = provenance if isinstance(provenance, Mapping) else {}
    source_model = source.get("model")
    if not isinstance(source_model, str) or not source_model.strip() or source_model == UNKNOWN:
        source_model = model
    model_value = _safe_model(source_model)
    provider = source.get("provider")
    if not isinstance(provider, str) or not provider.strip() or provider == UNKNOWN:
        provider = model_value.split("/", 1)[0] if "/" in model_value else UNKNOWN
    generated = source.get("generated_at")
    if not isinstance(generated, str) or not _SAFE_ISO_RE.fullmatch(generated):
        generated = _iso_timestamp(
            int((generated_at or datetime.now(timezone.utc)).timestamp() * 1000)
        )
    document: Dict[str, Any] = {
        "repository": _safe_repository(source.get("repository")),
        "workflow": _safe_workflow(source.get("workflow")),
        "job": _safe_label(source.get("job")),
        "run_id": _safe_digits(source.get("run_id")),
        "run_attempt": _safe_digits(source.get("run_attempt")),
        "model": model_value,
        "provider": _safe_label(provider),
        "generated_at": generated,
        "schema_version": SCHEMA_VERSION,
    }
    if "mode" in source:
        document["mode"] = _safe_enum(source.get("mode"), ("run", "github"), default="other")
    return document


def _parse_line(raw: str, unterminated: bool) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    stripped = raw.strip()
    if not stripped:
        return None, "blank"
    if not stripped.startswith("{"):
        return None, "non_json"
    try:
        event = json.loads(stripped)
    except (TypeError, ValueError):
        return None, "truncated" if unterminated else "malformed"
    if not isinstance(event, dict):
        return None, "malformed"
    return event, None


def summarize_event_stream(
    lines: Sequence[str],
    *,
    mode: str = "run",
    exit_code: int = 1,
    classification: str = "",
    computed_classification: Optional[str] = None,
    model: Any = "",
    provenance: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[datetime] = None,
    source_readable: bool = True,
    diagnostics_text: Optional[str] = None,
    log_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one bounded per-attempt telemetry document.

    ``lines`` must contain only the structured ``run --format json`` stream.
    Prose, prompts, and tool payloads are never copied.  ``log_text`` is used
    only to compute classification parity with the existing engine; it is not
    persisted.
    """

    event_counts = {name: 0 for name in sorted(_EVENT_TYPES)}
    event_counts["other"] = 0
    line_counts = {
        "lines": len(lines),
        "json_events": 0,
        "blank_lines": 0,
        "non_json_lines": 0,
        "malformed_lines": 0,
        "truncated_lines": 0,
        "read_error": not source_readable,
    }

    first_timestamp: Optional[int] = None
    last_timestamp: Optional[int] = None
    last_step_timestamp: Optional[int] = None

    step_finish_count = 0
    step_starts: List[Tuple[int, Any]] = []
    exact_start_queues: Dict[Tuple[Any, Any], Deque[Tuple[int, Optional[int]]]] = defaultdict(deque)
    session_start_queues: Dict[Any, Deque[Tuple[int, Optional[int]]]] = defaultdict(deque)
    consumed_starts = set()

    token_sums = {field: 0 for field in _TOKEN_FIELDS}
    token_known = {field: 0 for field in _TOKEN_FIELDS}
    step_steps_with_usage = 0
    step_steps_without_usage = 0
    step_usage_complete = True
    max_step_input: Optional[int] = None
    cost_sum = 0.0
    cost_known = 0
    step_records: List[Dict[str, Any]] = []
    steps_omitted = 0

    tool_events = 0
    tool_missing_id = 0
    duplicate_tool_events = 0
    unique_tools: Dict[Tuple[Any, str], Dict[str, Any]] = {}

    terminal_error_events = 0
    provider_classes: List[str] = []
    non_provider_errors = 0
    first_provider_429_at: Optional[int] = None
    last_provider_error_at: Optional[int] = None
    last_typed_kind = ""
    last_typed_status: Any = None
    error_attempt: Optional[int] = None
    correlation_id = UNKNOWN

    retry_events = 0
    retry_attempts_seen: set = set()
    retry_unique_attempts = 0
    max_retry_attempt: Optional[int] = None
    retry_first_at: Optional[int] = None
    retry_last_at: Optional[int] = None

    session_ids: List[str] = []
    session_seen = set()

    def record_timestamp(value: Any) -> Optional[int]:
        nonlocal first_timestamp, last_timestamp
        timestamp = _as_timestamp_ms(value)
        if timestamp is None:
            return None
        if first_timestamp is None or timestamp < first_timestamp:
            first_timestamp = timestamp
        if last_timestamp is None or timestamp > last_timestamp:
            last_timestamp = timestamp
        return timestamp

    def pop_step_start(session: Any, message: Any) -> Optional[int]:
        exact = exact_start_queues.get((session, message))
        while exact:
            start_index, timestamp = exact.popleft()
            if start_index not in consumed_starts:
                consumed_starts.add(start_index)
                return timestamp
        session_queue = session_start_queues.get(session)
        while session_queue:
            start_index, timestamp = session_queue.popleft()
            if start_index not in consumed_starts:
                consumed_starts.add(start_index)
                return timestamp
        return None

    for index, raw in enumerate(lines):
        unterminated = index == len(lines) - 1 and not raw.endswith(("\n", "\r"))
        event, line_error = _parse_line(raw, unterminated)
        if event is None:
            if line_error == "blank":
                line_counts["blank_lines"] += 1
            elif line_error == "truncated":
                line_counts["truncated_lines"] += 1
            elif line_error == "non_json":
                line_counts["non_json_lines"] += 1
            else:
                line_counts["malformed_lines"] += 1
            continue
        line_counts["json_events"] += 1
        kind = _event_kind(event)
        event_counts[kind] += 1
        timestamp = record_timestamp(event.get("timestamp"))
        session = _event_session(event)
        if session != UNKNOWN and session not in session_seen:
            session_seen.add(session)
            if len(session_ids) < MAX_SESSION_IDS:
                session_ids.append(session)
        part = _event_part(event)

        if kind == "step_start":
            step_index = len(step_starts)
            step_starts.append((step_index, timestamp))
            exact_start_queues[(_event_session(event), _event_message(part))].append(
                (step_index, timestamp)
            )
            session_start_queues[_event_session(event)].append((step_index, timestamp))
            continue

        if kind == "step_finish":
            step_finish_count += 1
            if timestamp is not None and (
                last_step_timestamp is None or timestamp > last_step_timestamp
            ):
                last_step_timestamp = timestamp
            started = pop_step_start(_event_session(event), _event_message(part))
            elapsed_ms = (
                timestamp - started
                if timestamp is not None and started is not None and timestamp >= started
                else None
            )
            values, cost, usage_present = _extract_usage(part)
            if usage_present:
                step_steps_with_usage += 1
            else:
                step_steps_without_usage += 1
            complete = usage_present and all(value is not None for value in values.values())
            if not complete:
                step_usage_complete = False
            for field in _TOKEN_FIELDS:
                value = values[field]
                if value is not None:
                    token_sums[field] += value
                    token_known[field] += 1
            if values["input_tokens"] is not None:
                if max_step_input is None or values["input_tokens"] > max_step_input:
                    max_step_input = values["input_tokens"]
            if cost is not None:
                cost_sum += cost
                cost_known += 1
            reason = _safe_enum(part.get("reason"), _STEP_REASONS)
            record = {
                "index": step_finish_count,
                "session_id": _event_session(event),
                "message_id": _event_message(part),
                "reason": reason,
                "started_at": _iso_timestamp(started),
                "ended_at": _iso_timestamp(timestamp),
                "elapsed_ms": elapsed_ms if elapsed_ms is not None else UNKNOWN,
                "usage_present": usage_present,
            }
            for field in _TOKEN_FIELDS:
                record[field] = values[field] if values[field] is not None else UNKNOWN
            record["cost"] = _number_or_unknown(cost)
            if len(step_records) < MAX_STEPS:
                step_records.append(record)
            else:
                steps_omitted += 1
            continue

        if kind == "tool_use":
            tool_events += 1
            # v2.2.6 emits the stable part/call id as ``id``; formal session
            # parts label the same call ``callID``. Accept either, never a
            # title or other free-form field.
            call_id = part.get("callID") or part.get("id")
            if not isinstance(call_id, str) or not call_id.strip():
                tool_missing_id += 1
                continue
            # Deduplicate strictly by the stable call ID. A repeated state
            # update (or the same provider call observed in a resumed session)
            # is one invocation, never one HTTP request or one 429 signal.
            key = call_id.strip()[:512]
            if key in unique_tools:
                duplicate_tool_events += 1
            unique_tools[key] = {
                "name": _tool_name(part),
                "status": _tool_status(part),
            }
            continue

        if kind == "retry":
            retry_events += 1
            attempt = _as_int(event.get("attempt"))
            if attempt is not None:
                if attempt not in retry_attempts_seen:
                    retry_attempts_seen.add(attempt)
                    retry_unique_attempts += 1
                if max_retry_attempt is None or attempt > max_retry_attempt:
                    max_retry_attempt = attempt
            if timestamp is not None:
                if retry_first_at is None or timestamp < retry_first_at:
                    retry_first_at = timestamp
                if retry_last_at is None or timestamp > retry_last_at:
                    retry_last_at = timestamp
            continue

        if kind == "error":
            terminal_error_events += 1
            error_payload = event.get("error")
            error_object = error_payload if isinstance(error_payload, Mapping) else {}
            # classify_opencode_error_event requires the full top-level event.
            error_classification = classify_opencode_error_event(event)
            typed = (
                error_classification in _PROVIDER_CLASSIFICATION_ORDER
                and describe_opencode_error(error_object).get("provider") is True
            )
            if typed:
                provider_classes.append(error_classification)
                details = describe_opencode_error(error_object)
                kind_name = details.get("kind")
                last_typed_kind = (
                    kind_name
                    if isinstance(kind_name, str)
                    and _SAFE_ERROR_KIND_RE.fullmatch(kind_name)
                    else UNKNOWN
                )
                status = _as_int(details.get("status"))
                last_typed_status = status if status is not None else UNKNOWN
                if timestamp is not None:
                    if (
                        last_provider_error_at is None
                        or timestamp > last_provider_error_at
                    ):
                        last_provider_error_at = timestamp
                if error_classification == "quota_429" and (
                    first_provider_429_at is None
                    or (
                        timestamp is not None
                        and timestamp < first_provider_429_at
                    )
                ):
                    first_provider_429_at = timestamp
                attempt = _extract_error_attempt(error_object)
                if attempt is not None:
                    error_attempt = attempt
                correlation = _extract_correlation_id(error_object)
                if correlation != UNKNOWN:
                    correlation_id = correlation
            else:
                non_provider_errors += 1
            continue

    if not source_readable:
        line_counts["lines"] = 0
        line_counts["json_events"] = 0

    # Classification parity: the wrapper classification remains authoritative,
    # but the artifact records the engine's independent result when derivable.
    if computed_classification is None:
        if mode == "run":
            computed_classification = classify_opencode_events(
                log_text if log_text is not None else "".join(lines), exit_code
            )
        elif diagnostics_text is not None:
            computed_classification = classify_opencode_github_events(
                diagnostics_text, exit_code
            )
        else:
            computed_classification = (
                classification if classification in CLASSIFICATIONS else "unknown"
            )
    selected_classification = (
        classification if classification in CLASSIFICATIONS else computed_classification
    )
    if selected_classification not in CLASSIFICATIONS:
        selected_classification = "inconclusive"
    if computed_classification in CLASSIFICATIONS:
        classification_match: Any = computed_classification == selected_classification
    else:
        classification_match = "unknown"

    missing_fields = []
    partial_fields = []
    for field in _TOKEN_FIELDS:
        if token_known[field] == 0:
            missing_fields.append(field)
        elif token_known[field] < step_finish_count:
            partial_fields.append(field)
    usage_complete = (
        step_finish_count > 0
        and step_steps_with_usage == step_finish_count
        and all(token_known[field] == step_finish_count for field in _TOKEN_FIELDS)
    )
    if not step_usage_complete:
        usage_complete = False

    # Prefer the attempt carried by the terminal provider error; the bounded
    # retry maximum is a fallback when the stream omits it.
    attempt_value: Any = (
        error_attempt if error_attempt is not None else max_retry_attempt
    )
    if attempt_value is None:
        attempt_value = UNKNOWN

    provenance_document = _bounded_provenance(
        provenance,
        model=model,
        mode=mode,
        generated_at=generated_at,
    )

    status_counts = {status: 0 for status in sorted(_TOOL_STATUSES)}
    status_counts[UNKNOWN] = 0
    name_counts: Dict[str, int] = {}
    for call in unique_tools.values():
        status = call["status"]
        status_counts[status if status in status_counts else UNKNOWN] += 1
        name = call["name"]
        name_counts[name] = name_counts.get(name, 0) + 1
    tool_names = [
        {"name": name, "unique_calls": count}
        for name, count in sorted(name_counts.items())[:MAX_TOOL_NAMES]
    ]

    provider_error = {
        "classification": _strongest_provider_classification(provider_classes),
        "typed_provider_error_events": len(provider_classes),
        "non_provider_error_events": non_provider_errors,
        "terminal_error_events": terminal_error_events,
        "kind": last_typed_kind if last_typed_kind else UNKNOWN,
        "http_status": last_typed_status if last_typed_status is not None else UNKNOWN,
        "first_provider_429_at": _iso_timestamp(first_provider_429_at),
        "last_provider_error_at": _iso_timestamp(last_provider_error_at),
        "attempt": attempt_value,
        "correlation_id": correlation_id,
    }

    elapsed_ms: Any = (
        last_timestamp - first_timestamp
        if first_timestamp is not None
        and last_timestamp is not None
        and last_timestamp >= first_timestamp
        else UNKNOWN
    )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "provenance": provenance_document,
        "attempt": {
            "mode": _safe_enum(mode, ("run", "github"), default="other"),
            "exit_code": exit_code,
            "classification": selected_classification,
            "computed_classification": computed_classification,
            "classification_match": classification_match,
            "first_event_at": _iso_timestamp(first_timestamp),
            "last_event_at": _iso_timestamp(last_timestamp),
            "last_step_at": _iso_timestamp(last_step_timestamp),
            "elapsed_ms": elapsed_ms,
            "source": "opencode-json-events" if mode == "run" else "cli-diagnostics",
        },
        "events": dict(event_counts, **line_counts),
        "sessions": {
            "count": len(session_seen),
            "ids": session_ids,
            "ids_truncated": len(session_seen) > len(session_ids),
        },
        "usage": {
            "steps": step_finish_count,
            "steps_with_usage": step_steps_with_usage,
            "steps_without_usage": step_steps_without_usage,
            "complete": usage_complete,
            "input_tokens": (
                token_sums["input_tokens"] if token_known["input_tokens"] else UNKNOWN
            ),
            "output_tokens": (
                token_sums["output_tokens"] if token_known["output_tokens"] else UNKNOWN
            ),
            "reasoning_tokens": (
                token_sums["reasoning_tokens"]
                if token_known["reasoning_tokens"]
                else UNKNOWN
            ),
            "cache_read_tokens": (
                token_sums["cache_read_tokens"]
                if token_known["cache_read_tokens"]
                else UNKNOWN
            ),
            "cache_write_tokens": (
                token_sums["cache_write_tokens"]
                if token_known["cache_write_tokens"]
                else UNKNOWN
            ),
            "cost": cost_sum if cost_known else UNKNOWN,
            "cost_known_steps": cost_known,
            "cost_complete": step_finish_count > 0
            and cost_known == step_finish_count,
            "context_input_tokens_max": max_step_input if max_step_input is not None else UNKNOWN,
            "missing_fields": missing_fields,
            "partial_fields": partial_fields,
        },
        "steps": {
            "count": step_finish_count,
            "truncated": steps_omitted > 0,
            "omitted": steps_omitted,
            "items": step_records,
        },
        "tools": {
            "events": tool_events,
            "unique_calls": len(unique_tools),
            "duplicate_events": duplicate_tool_events,
            "missing_id_events": tool_missing_id,
            "status": status_counts,
            "names": tool_names,
            "names_truncated": len(name_counts) > len(tool_names),
        },
        "retries": {
            "events": retry_events,
            "max_attempt": max_retry_attempt if max_retry_attempt is not None else UNKNOWN,
            "first_at": _iso_timestamp(retry_first_at) if retry_first_at is not None else UNKNOWN,
            "last_at": _iso_timestamp(retry_last_at) if retry_last_at is not None else UNKNOWN,
            "attempts_truncated": retry_unique_attempts > MAX_RETRIES,
        },
        "requests": {
            "http_requests": UNKNOWN,
            "measurement": "unavailable",
            "basis": "provider_stream_exposes_no_http_request_counter",
            "step_finish_events": step_finish_count,
            "retry_events": retry_events,
            "note": "a step_finish is not proof of one HTTP request",
        },
        "provider_error": provider_error,
        "privacy": {
            "content_stored": False,
            "policy": "allowlist_numeric_and_enumerated_fields_only",
            "tool_payloads_stored": False,
            "error_text_stored": False,
        },
    }
    return summary


def summarize_opencode_log(
    log_text: str,
    *,
    mode: str = "run",
    exit_code: int = 1,
    classification: str = "",
    model: Any = "",
    provenance: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[datetime] = None,
    diagnostics_text: Optional[str] = None,
    source_readable: bool = True,
) -> Dict[str, Any]:
    """Summarize one CLI log without retaining any of its free-form text."""

    lines = (
        log_text.splitlines(keepends=True)
        if mode == "run" and isinstance(log_text, str)
        else []
    )
    return summarize_event_stream(
        lines,
        mode=mode,
        exit_code=exit_code,
        classification=classification,
        model=model,
        provenance=provenance,
        generated_at=generated_at,
        source_readable=source_readable,
        diagnostics_text=diagnostics_text,
        log_text=log_text if isinstance(log_text, str) else "",
    )


def build_provenance(
    *,
    mode: str,
    model: Any = "",
    env: Optional[Mapping[str, str]] = None,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the bounded provenance block from GitHub Actions environment."""

    values = os.environ if env is None else env
    run_id = values.get("GITHUB_RUN_ID", "")
    run_attempt = values.get("GITHUB_RUN_ATTEMPT", "")
    return {
        "repository": _safe_repository(values.get("GITHUB_REPOSITORY", "")),
        "workflow": _safe_workflow(values.get("GITHUB_WORKFLOW", "")),
        "job": _safe_label(values.get("GITHUB_JOB", "")),
        "run_id": run_id if run_id.isdigit() else UNKNOWN,
        "run_attempt": run_attempt if run_attempt.isdigit() else UNKNOWN,
        "model": _safe_model(model or values.get("OPENCODE_MODEL", "")),
        "mode": _safe_enum(mode, ("run", "github"), default="other"),
        "generated_at": _iso_timestamp(
            int((generated_at or datetime.now(timezone.utc)).timestamp() * 1000)
        ),
        "schema_version": SCHEMA_VERSION,
    }


def write_summary(summary: Mapping[str, Any], output_path: str) -> str:
    """Atomically persist a summary as compact, stable JSON."""

    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    temporary = output_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.replace(temporary, output_path)
    return output_path


def format_human_summary(summary: Mapping[str, Any]) -> str:
    """Render one concise, redacted log line for humans."""

    attempt = summary.get("attempt", {}) if isinstance(summary.get("attempt"), Mapping) else {}
    usage = summary.get("usage", {}) if isinstance(summary.get("usage"), Mapping) else {}
    tools = summary.get("tools", {}) if isinstance(summary.get("tools"), Mapping) else {}
    requests = (
        summary.get("requests", {})
        if isinstance(summary.get("requests"), Mapping)
        else {}
    )
    provider = (
        summary.get("provider_error", {})
        if isinstance(summary.get("provider_error"), Mapping)
        else {}
    )
    return " ".join(
        (
            "OpenCode telemetry: mode={mode} exit={exit} classification={classification} "
            "steps={steps} usage_steps={usage_steps}/{steps} "
            "input_tokens={input_tokens} output_tokens={output_tokens} "
            "http_requests={http_requests} ({requests_measurement}) "
            "unique_tools={unique_tools} tool_events={tool_events} "
            "provider={provider_classification} provider_kind={provider_kind} "
            "provider_status={provider_status} "
            "first_provider_429_at={first_429} last_step_at={last_step}"
        ).format(
            mode=attempt.get("mode", UNKNOWN),
            exit=attempt.get("exit_code", UNKNOWN),
            classification=attempt.get("classification", UNKNOWN),
            steps=usage.get("steps", UNKNOWN),
            usage_steps=usage.get("steps_with_usage", UNKNOWN),
            input_tokens=usage.get("input_tokens", UNKNOWN),
            output_tokens=usage.get("output_tokens", UNKNOWN),
            http_requests=requests.get("http_requests", UNKNOWN),
            requests_measurement=requests.get("measurement", UNKNOWN),
            unique_tools=tools.get("unique_calls", UNKNOWN),
            tool_events=tools.get("events", UNKNOWN),
            provider_classification=provider.get("classification", UNKNOWN),
            provider_kind=provider.get("kind", UNKNOWN),
            provider_status=provider.get("http_status", UNKNOWN),
            first_429=provider.get("first_provider_429_at", UNKNOWN),
            last_step=attempt.get("last_step_at", UNKNOWN),
        ).split()
    )


__all__ = [
    "KIND",
    "MAX_RETRIES",
    "MAX_SESSION_IDS",
    "MAX_STEPS",
    "MAX_TOOL_NAMES",
    "SCHEMA_VERSION",
    "UNKNOWN",
    "build_provenance",
    "format_human_summary",
    "summarize_event_stream",
    "summarize_opencode_log",
    "write_summary",
]
