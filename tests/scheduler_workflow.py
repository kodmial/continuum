"""Shared reconstruction of the scheduler workflow's embedded github-script.

The scheduler implementation lives in ``.github/scripts/issue_scheduler.js``
and is loaded by ``continuum-issue-scheduler.yml`` through a small
``script: |`` loader. Contract suites assert against the implementation body,
so they reconstruct the would-be inline script from the checked-in module
instead of duplicating the wrapper, step, and indentation rules.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SCHEDULER_WORKFLOW = WORKFLOWS_DIR / "continuum-issue-scheduler.yml"
SCHEDULER_SCRIPT = ROOT / ".github" / "scripts" / "issue_scheduler.js"

MODULE_PREFIX = (
    "'use strict';\n\n"
    "module.exports = async function runIssueScheduler({ github, context, core }) {\n"
)
MODULE_SUFFIX = "\n};\n"
STEP_HEADING = "      - name: Reconcile state and dispatch ready issues"
SCRIPT_MARKER = "          script: |\n"
NEXT_STEP = "\n      - name: Wake the configured downstream dispatcher"


def scheduler_module_body() -> str:
    module_body = SCHEDULER_SCRIPT.read_text(encoding="utf-8")
    if not module_body.startswith(MODULE_PREFIX) or not module_body.endswith(
        MODULE_SUFFIX
    ):
        raise AssertionError("extracted scheduler module wrapper changed")
    return module_body[len(MODULE_PREFIX):-len(MODULE_SUFFIX)]


def scheduler_inline_body() -> str:
    return "\n".join(
        "" if not line.strip() else (line[2:] if line.startswith("  ") else line)
        for line in scheduler_module_body().split("\n")
    )


def reconstruct_scheduler_workflow(body: str | None = None) -> str:
    if body is None:
        body = SCHEDULER_WORKFLOW.read_text(encoding="utf-8")
    inline_body = scheduler_inline_body()
    start = body.index(SCRIPT_MARKER, body.index(STEP_HEADING))
    finish = body.index(NEXT_STEP, start)
    embedded = "\n".join(
        "" if not line.strip() else "            " + line
        for line in inline_body.split("\n")
    )
    return body[:start] + SCRIPT_MARKER + embedded + body[finish:]


def scheduler_workflow_text() -> str:
    return reconstruct_scheduler_workflow()


def read_workflow(name: str) -> str:
    body = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
    if name == SCHEDULER_WORKFLOW.name:
        return reconstruct_scheduler_workflow(body)
    return body
