#!/usr/bin/env python3
"""Render and classify OpenCode provider failures.

The workflow's rate-limit wrapper uses this script so every agent mode shares
one tested classifier instead of inline transcript greps:

* ``render`` consumes ``opencode run --format json`` on stdin, writes the raw
  event stream to a log file, and prints a readable view. Only ``type=text``
  parts become stdout prose; tool output and provider diagnostics never become
  provider evidence.
* ``classify`` prints one classification from ``src/continuum``'s engine. For
  ``run`` it trusts only top-level ``type=error`` events. For ``github run``
  it trusts only the CLI runtime's stderr ``Agent error:`` block, because the
  legacy ``github run`` path has no JSON mode.
* ``telemetry`` writes a bounded, privacy-safe per-attempt summary from the
  same structured ``run`` events. It never persists prompts, tool payloads,
  provider response bodies, error text, credentials, or model messages.

Standard library only; the script is copied next to ``src/continuum`` by the
workflow bootstrap so runner environments need no package installation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Iterable, TextIO

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.opencode_429_recovery import (  # noqa: E402
    classify_opencode_events,
    classify_opencode_github_events,
)

_CLASSIFICATIONS = {
    "quota_429",
    "unavailable",
    "upstream",
    "timeout",
    "fail_closed",
    "provider_error",
    "ok",
    "inconclusive",
}


def _print(value: str, stream: TextIO = sys.stdout) -> None:
    stream.write(value)
    if not value.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _safe_diagnostic(value: object) -> str:
    # Error messages are untrusted provider/session text. Never render newlines
    # (including GitHub workflow command injection) or common credential forms.
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)\bbearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:authorization|api[_-]?key|token|password|secret)\b"
        r"\s*[:=]\s*[^\s,;]+",
        "credential=[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|"
        r"github_pat_[A-Za-z0-9_]{12,})\b",
        "[REDACTED]",
        text,
    )
    return text[:450]

def _error_summary(error: object) -> str:
    if not isinstance(error, dict):
        return "unknown error"
    # v1.19.3: {name,data:{message,statusCode}};
    # v2.2.6: {type,message}, with generic non-provider terminal failures.
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    name = error.get("name") or error.get("type") or "Error"
    message = data.get("message") or error.get("message") or ""
    status = data.get("statusCode") or data.get("status")
    if status is None:
        status = error.get("statusCode") or error.get("status")
    parts = [_safe_diagnostic(name)]
    if status is not None and str(status).isdigit():
        parts.append("status={}".format(status))
    if message:
        parts.append(_safe_diagnostic(message))
    return " ".join(parts)


def render(log_path: str, stream: Iterable[str] = sys.stdin) -> int:
    """Write the raw event stream to ``log_path`` and print a readable view."""

    try:
        raw_log = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        _print("::error::Cannot write OpenCode event log: {}".format(exc), sys.stderr)
        return 2
    try:
        for raw in stream:
            raw_log.write(raw)
            raw_log.flush()
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except (TypeError, ValueError):
                # A non-JSON diagnostic line (for example a process-level
                # error) is preserved verbatim; it is never classification
                # evidence.
                _print(raw.rstrip("\n"))
                continue
            if not isinstance(event, dict):
                _print(raw.rstrip("\n"))
                continue
            kind = event.get("type")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if kind == "text" and isinstance(part.get("text"), str):
                _print(part["text"])
            elif kind == "tool_use":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                title = state.get("title")
                if isinstance(title, str) and title:
                    _print("[tool] {}".format(title), sys.stderr)
                if state.get("status") == "error" and state.get("error"):
                    _print(
                        "[tool error] {}".format(state["error"]), sys.stderr
                    )
            elif kind == "error":
                _print(
                    "OpenCode error: {}".format(_error_summary(event.get("error"))),
                    sys.stderr,
                )
            # step_start/step_finish and unknown event types are already in
            # the raw log; they add no readable value here.
    finally:
        raw_log.close()
    return 0


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def classify(mode: str, log_path: str, diagnostics_path: str, exit_code: int) -> int:
    try:
        if mode == "github":
            text = _read(diagnostics_path or log_path)
            result = classify_opencode_github_events(text, exit_code)
        else:
            result = classify_opencode_events(_read(log_path), exit_code)
    except OSError as exc:
        _print("::error::Cannot read OpenCode log: {}".format(exc), sys.stderr)
        return 2
    if result not in _CLASSIFICATIONS:
        _print("::error::Unexpected OpenCode classification.", sys.stderr)
        return 2
    print(result)
    return 0


def telemetry(
    mode: str,
    log_path: str,
    diagnostics_path: str,
    exit_code: int,
    classification: str,
    output_path: str,
    model: str,
    print_summary: bool,
) -> int:
    """Persist one bounded, privacy-safe per-attempt summary."""

    # Telemetry is optional observability on top of the provider classifier.
    # Import it lazily so a missing/broken telemetry module can never break
    # ``render`` or ``classify`` (and therefore 429 runner protection).
    try:
        from continuum.opencode_telemetry import (  # noqa: PLC0415
            build_provenance,
            format_human_summary,
            summarize_opencode_log,
            write_summary,
        )
    except Exception as exc:  # noqa: BLE001 - optional feature isolation
        _print(
            "::warning::OpenCode telemetry support is unavailable: {}".format(exc),
            sys.stderr,
        )
        return 2

    source_readable = True
    try:
        log_text = _read(log_path)
    except OSError as exc:
        source_readable = False
        log_text = ""
        _print(
            "::warning::Cannot read OpenCode log for telemetry: {}".format(exc),
            sys.stderr,
        )
    diagnostics_text = None
    if mode == "github" and diagnostics_path:
        try:
            diagnostics_text = _read(diagnostics_path)
        except OSError:
            diagnostics_text = None
    summary = summarize_opencode_log(
        log_text,
        mode=mode,
        exit_code=exit_code,
        classification=classification,
        model=model,
        provenance=build_provenance(mode=mode, model=model),
        diagnostics_text=diagnostics_text,
        source_readable=source_readable,
    )
    try:
        write_summary(summary, output_path)
    except OSError as exc:
        _print(
            "::warning::Cannot write OpenCode telemetry summary: {}".format(exc),
            sys.stderr,
        )
        return 2
    if print_summary:
        _print(format_human_summary(summary))
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    render_parser = sub.add_parser("render", help="render a run event stream")
    render_parser.add_argument("--log", required=True)
    render_parser.add_argument("--stdin", default="-")

    classify_parser = sub.add_parser("classify", help="classify a finished invocation")
    classify_parser.add_argument("--mode", choices=("run", "github"), required=True)
    classify_parser.add_argument("--log", required=True)
    classify_parser.add_argument("--diagnostics", default="")
    classify_parser.add_argument("--exit", dest="exit_code", type=int, default=1)

    telemetry_parser = sub.add_parser(
        "telemetry", help="persist a bounded per-attempt telemetry summary"
    )
    telemetry_parser.add_argument("--mode", choices=("run", "github"), required=True)
    telemetry_parser.add_argument("--log", required=True)
    telemetry_parser.add_argument("--diagnostics", default="")
    telemetry_parser.add_argument("--exit", dest="exit_code", type=int, default=1)
    telemetry_parser.add_argument("--classification", default="")
    telemetry_parser.add_argument("--model", default="")
    telemetry_parser.add_argument("--output", required=True)
    telemetry_parser.add_argument("--print-summary", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "render":
        if args.stdin == "-":
            return render(args.log)
        with open(args.stdin, "r", encoding="utf-8", errors="replace") as stream:
            return render(args.log, stream)
    if args.command == "telemetry":
        return telemetry(
            args.mode,
            args.log,
            args.diagnostics,
            args.exit_code,
            args.classification,
            args.output,
            args.model,
            args.print_summary,
        )
    return classify(args.mode, args.log, args.diagnostics, args.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
