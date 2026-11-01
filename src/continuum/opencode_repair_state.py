"""Latest approved-workflow state for the OpenCode repair controller.

Both admission paths (the consumer-reported CI dispatch and the
``workflow_run`` completion) reconcile the latest result of every
approved workflow for the reported head. Keeping that classification in
one place is what stops the paths from diverging on the lock-versus-clear
safety guarantee: a green event must never clear a lock while another
approved workflow is failing or in flight, and an untrusted or truncated
run listing must never be treated as green evidence.

Only the action taken on each state differs by admission path; the state
itself is identical.
"""

from __future__ import annotations

import json

REPAIRABLE_CONCLUSIONS = ("success", "failure", "timed_out")
FAILURE_CONCLUSIONS = ("failure", "timed_out")


def classify_latest_state(
    runs_json,
    *,
    trusted,
    allowed_workflows,
    own_name="",
    own_id="",
    own_conclusion="",
    own_force=False,
):
    """Classify the latest approved-workflow state for one reported head.

    ``runs_json`` is the raw ``/actions/runs?head_sha=...`` response (a
    JSON string or an already-decoded object). ``trusted`` records that
    the listing call itself succeeded. The result is a
    ``(state, failing_run_id, detail)`` triple, where ``state`` is one of
    ``failure``, ``inflight``, ``green``, ``unknown`` or ``empty`` and
    ``detail`` is the failing workflow name or the in-flight names.
    """
    try:
        data = json.loads(runs_json or "{}") if isinstance(runs_json, str) else runs_json
        if not isinstance(data, dict):
            raise ValueError("run listing is not a JSON object")
        runs = data.get("workflow_runs") or []
        if not isinstance(runs, list):
            raise ValueError("workflow_runs is not a JSON array")
        total = int(data.get("total_count") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        runs, total, trusted = [], 0, False
    if total > len(runs):
        trusted = False

    allowed = {
        part.strip()
        for part in str(allowed_workflows or "").replace("\n", ",").split(",")
        if part.strip()
    }

    latest = {}
    if trusted:
        for run in runs:
            if not isinstance(run, dict):
                continue
            name = str(run.get("name") or "")
            if name not in allowed:
                continue
            try:
                run_id = int(run.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if name not in latest or run_id > latest[name][0]:
                latest[name] = (run_id, run)

    own_name = str(own_name or "")
    own_conclusion = str(own_conclusion or "")
    try:
        own_id_number = int(own_id or 0)
    except (TypeError, ValueError):
        own_id_number = 0

    if (own_force or own_name in allowed) and own_conclusion in REPAIRABLE_CONCLUSIONS:
        if own_name not in latest or own_id_number >= latest[own_name][0]:
            latest[own_name] = (
                own_id_number,
                {
                    "id": own_id_number,
                    "name": own_name,
                    "status": "completed",
                    "conclusion": own_conclusion,
                },
            )

    failures, inflight = [], []
    for name, (run_id, run) in latest.items():
        if str(run.get("status") or "") != "completed":
            inflight.append(name)
        elif str(run.get("conclusion") or "") in FAILURE_CONCLUSIONS:
            failures.append((run_id, name))

    if failures:
        failures.sort()
        return "failure", str(failures[0][0]), failures[0][1]
    if inflight:
        return "inflight", "", ",".join(sorted(inflight))
    if latest and trusted:
        return "green", "", ""
    if latest:
        return "unknown", "", ""
    return "empty", "", ""
