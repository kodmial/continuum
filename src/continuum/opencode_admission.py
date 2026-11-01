"""Admission orchestration for OpenCode issue and pull-request events.

The command grammar lives in :mod:`continuum.opencode_commands`. This module
turns that grammar into event admission and prevents a queued duplicate from
starting after the run that already owns the issue finishes.

Workflow ``if:`` expressions cannot call the parser, so the workflow asks this
module (through ``.github/scripts/opencode_command.py``) for one admission
decision before starting an expensive runner. The decision is fail closed:
unknown events, untrusted authors, cancellation, malformed bodies, and
unparseable run inventories never admit work.

Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .opencode_commands import (
    CANCEL,
    IGNORE,
    RUN,
    classify_issue_comment,
    is_owner_command,
)

#: Modes the reusable workflow accepts through ``workflow_dispatch``.
DISPATCH_MODES = (
    "coderabbit-fix",
    "resolve-conflict",
    "ci-fix",
    "issue",
    "qualification",
)

#: Marker the scheduler adds to a qualification dispatch comment.
QUALIFICATION_MARKER = "continuum-qualification-dispatch"


def _logins_equal(first, second) -> bool:
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    if not first.strip() or not second.strip():
        return False
    return first.strip().casefold() == second.strip().casefold()


def _ignored(command=IGNORE, reason="event is not an authorized OpenCode command"):
    return {
        "run": False,
        "route": "ignore",
        "command": command,
        "reason": reason,
    }


def _admitted(route, command=RUN, reason=""):
    return {
        "run": True,
        "route": route,
        "command": command,
        "reason": reason,
    }


def admit_event(
    *,
    event,
    body="",
    author="",
    owner="",
    actor="",
    is_pull_request=False,
    issue_state="",
    mode="",
):
    """Decide whether one workflow event may start OpenCode.

    ``event`` is the GitHub event name. Plain-issue commands use the exact
    parser; PR issue comments keep their historical ``startsWith`` rule;
    review comments keep their historical owner + marker rule. The caller
    still owns checkout and permissions, so this function only decides
    admission.
    """

    if not isinstance(body, str):
        body = ""

    if event == "workflow_dispatch":
        allowed = isinstance(mode, str) and mode.strip() in DISPATCH_MODES
        if not allowed:
            return _ignored(reason="workflow_dispatch mode is not admitted")
        return _admitted("dispatch", command="dispatch", reason="trusted workflow dispatch")

    if event == "issue_comment":
        if not _logins_equal(actor, owner) or not _logins_equal(author, owner):
            return _ignored(reason="comment actor/author is not the repository owner")
        command = classify_issue_comment(body)
        if is_pull_request:
            if QUALIFICATION_MARKER in body:
                return _ignored(
                    command,
                    "qualification dispatch never starts a PR launch",
                )
            if issue_state != "open":
                return _ignored(command, "pull request is not open")
            if "/oc-cancel" in body:
                return _ignored(command, "cancellation suppresses the PR launch")
            if body.startswith("/oc") or body.startswith("/opencode"):
                return _admitted("pr", reason="owner command on an open pull request")
            return _ignored(command, "comment does not start with a manual command")
        if command == CANCEL:
            # The destructive cancellation route re-checks owner identity in
            # the branch itself so it can never depend on the order of the
            # shared gate above.
            if not _logins_equal(actor, owner) or not _logins_equal(author, owner):
                return _ignored(
                    command,
                    "cancellation actor/author is not the repository owner",
                )
            # The repository owner explicitly cancels this issue generation.
            # Admission still never starts the code runner: the reusable
            # workflow routes this to the cheap cancellation-control job.
            return {
                "run": False,
                "route": "cancel",
                "command": CANCEL,
                "reason": "exact owner cancellation command",
            }
        if not is_owner_command(body, author, owner):
            return _ignored(command, "comment is not an exact owner command")
        if command != RUN:
            return _ignored(command, "comment is not an exact launch command")
        if QUALIFICATION_MARKER in body:
            return _admitted(
                "qualification",
                reason="trusted qualification dispatch comment",
            )
        return _admitted("issue", reason="exact owner issue command")

    if event == "pull_request_review_comment":
        if not _logins_equal(actor, owner) or not _logins_equal(author, owner):
            return _ignored(reason="review comment author is not the repository owner")
        if QUALIFICATION_MARKER in body:
            return _ignored(reason="qualification dispatch never starts a review launch")
        if "/oc-cancel" in body:
            return _ignored("cancel", "cancellation suppresses the review launch")
        if "/oc" in body or "/opencode" in body:
            return _admitted("review", reason="owner review comment")
        return _ignored(reason="review comment does not mention a command")

    return _ignored(reason="unsupported event")


def classify_commands(items):
    """Classify a batch of bodies or ``{"body": ...}`` records.

    Scheduler steps use this to apply the canonical grammar to many comments
    without copying the parser into JavaScript.
    """

    if not isinstance(items, list):
        raise ValueError("command batch must be a JSON array")
    results = []
    for item in items:
        body = item.get("body") if isinstance(item, dict) else item
        results.append(classify_issue_comment(body))
    return results


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_order(run):
    created = _parse_timestamp(run.get("created_at"))
    try:
        run_id = int(run.get("id"))
    except (TypeError, ValueError):
        return None
    if created is None:
        return None
    return created, run_id


def _same_issue(run, issue_number):
    title = str(run.get("display_title") or "")
    return title == "OpenCode issue #{}".format(issue_number)


def _same_workflow(run, current, workflow_name):
    current_path = current.get("path")
    if current_path and run.get("path") == current_path:
        return True
    return bool(workflow_name and run.get("name") == workflow_name)


def _skip(reason, duplicate=None):
    result = {"skip": True, "reason": reason}
    if duplicate:
        result["duplicate_run_id"] = duplicate.get("id")
        result["duplicate_run_status"] = duplicate.get("status")
    return result


def duplicate_run_decision(
    *,
    current,
    runs,
    issue_number,
    workflow_name="",
    legacy_title="",
):
    """Whether ``current`` is a queued duplicate of an earlier same-issue run.

    The earliest active run owns the issue. A later run that was created
    while an earlier same-issue run was still active is stale even if it
    starts after that earlier run completes. A run created after the earlier
    run completed is a legitimate recovery or a new owner command.

    ``legacy_title`` is accepted for caller compatibility but ignored: an
    issue title is not an identity, so two different issues with the same
    title must never be treated as the same issue.
    """

    if not isinstance(current, dict):
        raise ValueError("current run must be an object")
    if not isinstance(runs, list):
        raise ValueError("runs must be a JSON array")
    try:
        issue_number = int(issue_number)
    except (TypeError, ValueError):
        return {"skip": False, "reason": "no valid issue number"}
    if issue_number <= 0:
        return {"skip": False, "reason": "no valid issue number"}

    current_order = _run_order(current)
    current_created = _parse_timestamp(current.get("created_at"))
    if current_order is None or current_created is None:
        return _skip("current run identity is unavailable; refusing to start")

    current_id = current_order[1]
    for run in runs:
        if not isinstance(run, dict):
            return _skip("run inventory entry is malformed; refusing to start")
        try:
            run_id = int(run.get("id"))
        except (TypeError, ValueError):
            return _skip("run inventory id is malformed; refusing to start")
        if run_id == current_id:
            continue
        if not _same_workflow(run, current, workflow_name):
            continue
        if not _same_issue(run, issue_number):
            continue

        order = _run_order(run)
        if order is None:
            return _skip("same-issue run timestamp is unavailable; refusing to start")

        if run.get("status") != "completed":
            if order < current_order:
                return _skip(
                    "earlier same-issue run is still active",
                    duplicate=run,
                )
            continue

        updated = _parse_timestamp(run.get("updated_at"))
        if updated is None:
            return _skip(
                "completed same-issue run has no finish time; refusing to start",
                duplicate=run,
            )
        if order < current_order and updated > current_created:
            return _skip(
                "run was queued while the previous same-issue run was active",
                duplicate=run,
            )

    return {"skip": False, "reason": "no earlier active or overlapping same-issue run"}
