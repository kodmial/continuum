#!/usr/bin/env python3
"""Classify automation failures and schedule infrastructure retries.

Infrastructure retries are deliberately separate from semantic repair attempts:
a network/provider outage must never consume the three attempts reserved for
actual code/merge repair.

The retry windows are policy, not timing sleeps. Callers persist next_retry_at
and let a scheduler pick the work up later, so no runner is held idle.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
from dataclasses import dataclass

INFRA_RETRY_WINDOWS_MINUTES = (1, 2, 4, 8, 16, 32, 60, 60)
JITTER_FRACTION = 0.20
INFRA_RETRY_MARKER_RE = re.compile(
    r"<!--\s*continuum-infra-retry:\s*attempt=(\d+)\s+"
    r"next_retry_at=([^\s]+)\s+code=([A-Za-z0-9_.-]+)\s+run=(\d+)\s*-->"
)
INFRA_RETRY_EXHAUSTED_MARKER = "<!-- continuum-infra-retry-exhausted -->"

_INFRA_SIGNATURES = (
    ("dns_resolution_failed", re.compile(r"(could not resolve host|temporary failure in name resolution|name or service not known)", re.I)),
    ("connection_reset", re.compile(r"(connection reset|econnreset|socket hang up|curl:\s*\(56\))", re.I)),
    ("connection_failed", re.compile(r"(failed to connect|connection refused|econnrefused|curl:\s*\(7\))", re.I)),
    ("network_timeout", re.compile(r"(curl:\s*\(28\)|etimedout|connection timed out|tls handshake timeout)", re.I)),
    ("tls_transport", re.compile(r"(curl:\s*\(35\)|ssl_connect|tls.*(error|failed)|certificate verify failed)", re.I)),
    ("http_408", re.compile(r"(http(?:/\S+)?\s+408\b|status(?: code)?[:= ]+408\b)", re.I)),
    ("http_429", re.compile(r"(http(?:/\S+)?\s+429\b|status(?: code)?[:= ]+429\b|too many requests|rate limit(?:ed| exceeded)?)", re.I)),
    ("http_5xx", re.compile(r"(http(?:/\S+)?\s+5\d\d\b|status(?: code)?[:= ]+5\d\d\b|\b(?:502 bad gateway|503 service unavailable|504 gateway timeout)\b)", re.I)),
    ("github_transient", re.compile(r"(github.*(temporarily unavailable|service unavailable)|secondary rate limit)", re.I)),
    ("opencode_upstream", re.compile(r"(opencode.*(temporarily unavailable|service unavailable|failed to (?:download|fetch)|version lookup failed))", re.I)),
)

_AUTH_SIGNATURES = (
    re.compile(r"\b(?:401|403)\b.*\b(?:unauthorized|forbidden|permission)", re.I),
    re.compile(r"(bad credentials|authentication failed|missing (?:secret|token)|trust policy.*(?:denied|refused))", re.I),
)

_AGENT_STEPS = {
    "run opencode",
    "resolve the conflicting hunks",
    "fix failed blocking workflow",
}


@dataclass(frozen=True)
class Classification:
    failure_class: str
    code: str
    retryable: bool


def classify(log_text: str, failed_step: str = "") -> Classification:
    text = log_text or ""
    for code, pattern in _INFRA_SIGNATURES:
        if pattern.search(text):
            return Classification("transient_infra", code, True)

    for pattern in _AUTH_SIGNATURES:
        if pattern.search(text):
            return Classification("auth_policy", "auth_or_policy_failure", False)

    step = " ".join((failed_step or "").strip().lower().split())
    if step in _AGENT_STEPS:
        return Classification("agent_execution", "agent_execution_failed", False)

    return Classification("control_plane", "deterministic_or_unknown_control_plane", False)


def retry_delay_seconds(attempt: int, seed: str) -> int:
    if attempt < 1 or attempt > len(INFRA_RETRY_WINDOWS_MINUTES):
        raise ValueError("infrastructure retry attempt is outside the configured budget")
    base = INFRA_RETRY_WINDOWS_MINUTES[attempt - 1] * 60
    digest = hashlib.sha256((seed or "continuum").encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)
    factor = (1.0 - JITTER_FRACTION) + (2.0 * JITTER_FRACTION * unit)
    return max(1, int(round(base * factor)))


def parse_now(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_classify(args: argparse.Namespace) -> int:
    content = sys.stdin.read()
    result = classify(content, failed_step=args.failed_step)
    print("failure_class=" + result.failure_class)
    print("failure_code=" + result.code)
    print("retryable=" + ("true" if result.retryable else "false"))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    attempt = int(args.attempt)
    delay = retry_delay_seconds(attempt, args.seed)
    next_retry = parse_now(args.now) + dt.timedelta(seconds=delay)
    print("infra_retry_attempt=" + str(attempt))
    print("delay_seconds=" + str(delay))
    print("next_retry_at=" + format_time(next_retry))
    print("retry_budget=" + str(len(INFRA_RETRY_WINDOWS_MINUTES)))
    return 0


def retry_state(text: str, now: dt.datetime | None = None) -> dict:
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    markers = list(INFRA_RETRY_MARKER_RE.finditer(text or ""))
    latest = markers[-1] if markers else None
    attempt = max((int(match.group(1)) for match in markers), default=0)
    next_retry_at = latest.group(2) if latest else ""
    due = True
    if next_retry_at:
        due = current >= parse_now(next_retry_at)
    return {
        "infra_retry_attempts": attempt,
        "next_retry_at": next_retry_at,
        "retry_due": due,
        "infra_retry_exhausted": INFRA_RETRY_EXHAUSTED_MARKER in (text or ""),
    }


def cmd_state(args: argparse.Namespace) -> int:
    state = retry_state(sys.stdin.read(), parse_now(args.now) if args.now else None)
    print("infra_retry_attempts=" + str(state["infra_retry_attempts"]))
    print("next_retry_at=" + str(state["next_retry_at"]))
    print("retry_due=" + ("true" if state["retry_due"] else "false"))
    print("infra_retry_exhausted=" + ("true" if state["infra_retry_exhausted"] else "false"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--failed-step", default="")
    classify_parser.set_defaults(func=cmd_classify)

    schedule_parser = sub.add_parser("schedule")
    schedule_parser.add_argument("--attempt", required=True)
    schedule_parser.add_argument("--seed", default="continuum")
    schedule_parser.add_argument("--now", default="")
    schedule_parser.set_defaults(func=cmd_schedule)

    state_parser = sub.add_parser("state")
    state_parser.add_argument("--now", default="")
    state_parser.set_defaults(func=cmd_state)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
