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
  pinned 1.19.3 CLI has no JSON mode for that command.

Standard library only; the script is copied next to ``src/continuum`` by the
workflow bootstrap so runner environments need no package installation.
"""

from __future__ import annotations

import argparse
import json
import os
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


def _error_summary(error: object) -> str:
    if not isinstance(error, dict):
        return "unknown error"
    name = error.get("name") or "Error"
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = data.get("message") or ""
    status = data.get("statusCode") or data.get("status") or ""
    parts = ["{}".format(name)]
    if status not in ("", None):
        parts.append("status={}".format(status))
    if message:
        parts.append(str(message))
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

    args = parser.parse_args(argv)
    if args.command == "render":
        if args.stdin == "-":
            return render(args.log)
        with open(args.stdin, "r", encoding="utf-8", errors="replace") as stream:
            return render(args.log, stream)
    return classify(args.mode, args.log, args.diagnostics, args.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
