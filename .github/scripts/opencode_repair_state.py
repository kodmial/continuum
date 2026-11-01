#!/usr/bin/env python3
"""Classify the latest approved-workflow state for the repair controller.

Thin CLI over ``src/continuum/opencode_repair_state.py``. Both admission
paths in ``continuum-opencode-repair.yml`` call this script, so the
lock-versus-clear decision cannot drift between the consumer-reported CI
dispatch and the ``workflow_run`` completion.

The raw ``/actions/runs?head_sha=...`` response is read from stdin (or
``--runs-json``), and the script prints exactly three lines:

    state
    failing_run_id
    failing_name_or_inflight_names

``state`` is ``failure``, ``inflight``, ``green``, ``unknown`` or
``empty``. The script always exits 0 after a classified decision; an
unreadable or truncated listing is classified as ``unknown``/``empty``
so callers fail closed instead of clearing a lock on incomplete data.

Standard library only, so it runs on stock ``ubuntu-latest`` runners
without dependency installation.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from continuum.opencode_repair_state import classify_latest_state  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-trusted",
        choices=("0", "1"),
        default="0",
        help="1 when the run listing call itself succeeded.",
    )
    parser.add_argument(
        "--allowed-workflows",
        default="",
        help="Comma- or newline-separated exact approved workflow names.",
    )
    parser.add_argument("--own-name", default="")
    parser.add_argument("--own-id", default="")
    parser.add_argument("--own-conclusion", default="")
    parser.add_argument("--own-force", action="store_true")
    parser.add_argument(
        "--runs-json",
        default=None,
        help="Run listing JSON. Reads stdin when omitted.",
    )
    args = parser.parse_args(argv)

    runs_json = args.runs_json if args.runs_json is not None else sys.stdin.read()
    state, failing_run_id, detail = classify_latest_state(
        runs_json,
        trusted=args.runs_trusted == "1",
        allowed_workflows=args.allowed_workflows,
        own_name=args.own_name,
        own_id=args.own_id,
        own_conclusion=args.own_conclusion,
        own_force=args.own_force,
    )
    print(state)
    print(failing_run_id)
    print(detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
