#!/usr/bin/env python3
"""Fail-closed liveness reconciler for already-dispatched exact-HEAD CI repairs.

Never creates a new CI-repair claim or a second workflow run. It may cancel an
old jobless run and rerun that SAME run ID once, only after GitHub confirms the
first attempt ended cancelled without any jobs. Its durable issue-comment
markers are written before each external mutation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from continuum.ci_repair_receipt import (  # noqa: E402
    OUTCOME_COMPLETE, STATUS_DISPATCHED, read_receipts,
)

GRACE_SECONDS = 30 * 60
OWNER_LOGINS = ("github-actions[bot]",)
SAFE_STATES = frozenset(("queued", "pending", "requested", "waiting", "in_progress"))
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def api(path, *, method="GET", data=None):
    args = ["gh", "api", "--method", method, path]
    if data is not None:
        args.extend(["--raw-field", "body=" + data])
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError("GitHub API rejected {} {}: {}".format(
            method, path, result.stderr.strip()[:350]
        ))
    if method != "GET":
        return None
    result_obj = json.loads(result.stdout)
    if not isinstance(result_obj, dict):
        raise ValueError("GitHub API returned a non-object for " + path)
    return result_obj


def pages(path):
    proc = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True, text=True, timeout=75,
    )
    if proc.returncode:
        raise RuntimeError("GitHub pagination failed: " + proc.stderr.strip()[:350])
    result = json.loads(proc.stdout)
    if not isinstance(result, list) or any(not isinstance(p, list) for p in result):
        raise ValueError("GitHub pagination did not supply complete list pages")
    return [item for page in result for item in page]


def run_decision(run, jobs, now, expected_id, expected_title):
    """Pure classification; never equate an accepted dispatch with execution."""
    if not isinstance(run, dict) or not isinstance(jobs, dict):
        return "hold", "unreadable run or jobs"
    if run.get("id") != expected_id or run.get("event") != "workflow_dispatch":
        return "hold", "run identity/event mismatch"
    if run.get("display_title") != expected_title:
        return "hold", "run title does not bind this PR and attempt"
    count = jobs.get("total_count")
    rows = jobs.get("jobs")
    if type(count) is not int or count < 0 or not isinstance(rows, list):
        return "hold", "unreadable job inventory"
    if count or rows:
        return "work", "GitHub reports jobs: never cancel an executing run"
    status = run.get("status")
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt != 1:
        return "hold", "only the initial jobless run may recover once"
    if status == "completed":
        if run.get("conclusion") == "cancelled":
            return "rerun", "first attempt terminal-cancelled with zero jobs"
        return "hold", "terminal jobless run is not a verified safe retry"
    if status not in SAFE_STATES:
        return "hold", "unrecognized run status"
    created = run.get("created_at")
    if not isinstance(created, str) or not TIME_RE.fullmatch(created):
        return "hold", "run creation time unverified"
    age = (now - datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
           .replace(tzinfo=timezone.utc)).total_seconds()
    if age < GRACE_SECONDS:
        return "wait", "legitimate scheduling grace"
    return "cancel", "old jobless GitHub run exceeds scheduling grace"


def trusted_marker(comments, marker, owner):
    return any(
        c.get("body", "").startswith(marker) and
        (c.get("user") or {}).get("login", "").lower() in
        (owner.lower(), "github-actions[bot]")
        for c in comments if isinstance(c, dict) and isinstance(c.get("body"), str)
    )


def post_marker(repo, number, marker):
    api("repos/{}/issues/{}/comments".format(repo, number),
        method="POST", data=marker + "\n\n<!-- continuum-origin role=continuum component=ci-repair-liveness -->")


def reconcile_one(repo, pr, owner, now):
    number = pr.get("number")
    head = pr.get("head") or {}
    head_sha = head.get("sha")
    if not isinstance(number, int) or not isinstance(head_sha, str):
        return
    if (head.get("repo") or {}).get("full_name", "").lower() != repo.lower():
        return
    issue = api("repos/{}/issues/{}".format(repo, number))
    comments = pages("repos/{}/issues/{}/comments?per_page=100".format(repo, number))
    if issue.get("comments") != len(comments):
        print("PR #{}: incomplete comment inventory; hold".format(number))
        return
    # GitHub's issue-comments API is oldest-first; deterministic ordering is
    # required by the receipt protocol, not a best-effort latest-comment read.
    comments.sort(key=lambda c: (c.get("created_at", ""), c.get("id", 0)))
    collection = read_receipts(
        comments, repository=repo, pull_number=number, head_sha=head_sha,
        history_complete=True, history_ordered=True,
        expected_comment_count=issue["comments"],
        owner_login=owner, trusted_logins=OWNER_LOGINS + (owner,),
    )
    if collection.outcome != OUTCOME_COMPLETE:
        print("PR #{}: receipt integrity {}, hold".format(number, collection.outcome))
        return
    if len(collection.attempts) != 1:
        return
    attempt = collection.attempts[0]
    if attempt.status != STATUS_DISPATCHED or attempt.run_id is None:
        return
    run_id = attempt.run_id
    title = "OpenCode repair PR #{} [ci-attempt:{}]".format(number, attempt.attempt_id)
    runpath = "repos/{}/actions/runs/{}".format(repo, run_id)
    jobpath = runpath + "/jobs?filter=all&per_page=100"

    def observe():
        return run_decision(api(runpath), api(jobpath), now, run_id, title)

    action, reason = observe()
    print("PR #{} run {}: {} ({})".format(number, run_id, action, reason))
    if action not in ("cancel", "rerun"):
        return

    # Revalidate the exact PR head and both GitHub inventories immediately
    # before mutation. No remote action if any evidence changed.
    current = api("repos/{}/pulls/{}".format(repo, number))
    if current.get("state") != "open" or (current.get("head") or {}).get("sha") != head_sha:
        return
    action2, _ = observe()
    if action2 != action:
        return

    prefix = "<!-- continuum-ci-repair-liveness repo={} pr={} head={} run={} attempt={} action={}".format(
        repo, number, head_sha, run_id, attempt.attempt_id, action
    )
    marker = prefix + " -->"
    if trusted_marker(comments, marker, owner):
        return
    # Record an immutable mutation claim BEFORE any external side effect.
    post_marker(repo, number, marker)
    if action == "cancel":
        try:
            api(runpath + "/cancel", method="POST")
            print("PR #{}: requested cancellation of proven jobless run {}".format(number, run_id))
        except RuntimeError as exc:
            print("::warning::Cancellation unavailable; hold (no duplicate): " + str(exc))
            return
        # Cancellation acceptance is NOT terminal proof. A later cron pass can
        # rerun this same ID only after an authoritative terminal observation.
        return
    cancel_marker = prefix.replace("action=rerun", "action=cancel") + " -->"
    if not trusted_marker(comments, cancel_marker, owner):
        print("::warning::Run was cancelled without this watchdog's claim; hold")
        return
    try:
        api(runpath + "/rerun", method="POST")
        print("PR #{}: requested ONE rerun of original run ID {}".format(number, run_id))
    except RuntimeError as exc:
        print("::warning::Rerun rejected or ambiguous; hold (no new dispatch): " + str(exc))


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo.lower() != "kodmial/continuum":
        print("Watchdog is restricted to the owning Continuum repository")
        return 0
    now = datetime.now(timezone.utc)
    prs = pages("repos/{}/pulls?state=open&per_page=100".format(repo))
    owner = repo.split("/")[0]
    for pr in prs:
        # Only PRs with a CI-repair presentation label can contain a current
        # dispatched attempt. No task creation or PR publication is possible.
        labels = {x.get("name") for x in pr.get("labels", []) if isinstance(x, dict)}
        if "opencode-ci-repair" not in labels:
            continue
        try:
            reconcile_one(repo, pr, owner, now)
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            print("::warning::PR #{} fail-closed: {}".format(
                pr.get("number"), str(exc)[:400]
            ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
