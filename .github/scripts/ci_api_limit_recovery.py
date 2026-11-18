#!/usr/bin/env python3
"""Guard CI code repair against proven GitHub REST exhaustion and recover natively.

Only GitHub API 403 primary-rate-limit failures in status-publication steps
qualify. This never handles OpenCode/model-provider 429 or changes code.
"""
from __future__ import annotations

import argparse
import calendar
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

WORKFLOWS = frozenset(("CI", "Continuum Contract Gate", "Contract qualification"))
PUBLICATION_STEPS = frozenset((
    "Mark validation pending", "Publish validation result and continue repair lifecycle",
    "Mark contract qualification pending", "Publish contract qualification result",
))
LOW_WATER = 500
MAX_ATTEMPTS = 10
MIN_RETRY_AGE = 180
MAX_PRS = 50
RATE_ERROR = re.compile(r"(?:API rate limit exceeded|primary rate limit exceeded)", re.I)


class Unavailable(Exception):
    pass


def api(path: str, *, post: bool = False, raw: bool = False):
    command = ["gh", "api"]
    if post:
        command += ["--method", "POST"]
    command.append(path)
    try:
        result = subprocess.run(command, capture_output=True, timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable("GitHub API unavailable") from exc
    if result.returncode:
        raise Unavailable("GitHub API rejected request")
    if post:
        return None
    if raw:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unavailable("Unparseable GitHub response") from exc


def rate_budget_ok() -> bool:
    try:
        data = api("rate_limit")
        core = data["resources"]["core"]
        return int(core["remaining"]) > max(LOW_WATER, int(core["limit"]) // 10)
    except (Unavailable, ValueError, TypeError, KeyError):
        return False


def decoded_log(body: bytes) -> str:
    # GitHub's job-log endpoint can return a ZIP archive or plain text.
    if len(body) > 10_000_000:
        raise Unavailable("Job log too large")
    if body.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = archive.namelist()
                if len(names) > 40 or any(archive.getinfo(n).file_size > 10_000_000 for n in names):
                    raise Unavailable("Job log archive is unbounded")
                return "\n".join(archive.read(n).decode("utf-8", "replace") for n in names)
        except (zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            raise Unavailable("Invalid job log archive") from exc
    return body.decode("utf-8", "replace")


def has_github_primary_limit_error(log: str) -> bool:
    # Require an actually emitted Actions error, not echoed shell/source text.
    return any("##[error]" in line and RATE_ERROR.search(line)
               for line in log.splitlines())


def classify_failure(run: dict, jobs: list, read_logs) -> str:
    """'github-403', 'code', or 'unknown'. Unknown must never dispatch OpenCode."""
    if run.get("event") != "pull_request" or run.get("status") != "completed" or run.get("conclusion") != "failure":
        return "unknown"
    if not isinstance(jobs, list) or not jobs:
        return "unknown"
    saw_failure = False
    for job in jobs:
        if job.get("conclusion") != "failure":
            continue
        saw_failure = True
        steps = job.get("steps")
        if not isinstance(steps, list):
            return "unknown"
        failed = [step.get("name") for step in steps if step.get("conclusion") == "failure"]
        if not failed:
            return "unknown"
        # A code-validation or test failure can never be relabeled as infrastructure.
        if any(name not in PUBLICATION_STEPS for name in failed):
            return "code"
        try:
            log = read_logs(job["id"])
        except (Unavailable, KeyError, TypeError, ValueError):
            return "unknown"
        if not has_github_primary_limit_error(log):
            return "unknown"
    return "github-403" if saw_failure else "unknown"


def classify_run(repo: str, run_id: int, *, expected_sha: str = "", expected_workflow: str = "") -> str:
    try:
        run = api(f"repos/{repo}/actions/runs/{run_id}")
        if expected_sha and run.get("head_sha") != expected_sha:
            return "unknown"
        if expected_workflow and run.get("name") not in {x.strip() for x in expected_workflow.replace("\n", ",").split(",")}:
            return "unknown"
        if not expected_workflow and run.get("name") not in WORKFLOWS:
            return "unknown"
        jobs = api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest")
        rows = jobs.get("jobs")
        if not isinstance(rows, list) or jobs.get("total_count") != len(rows):
            return "unknown"
        return classify_failure(run, rows, lambda job_id: decoded_log(api(f"repos/{repo}/actions/jobs/{job_id}/logs", raw=True)))
    except (Unavailable, TypeError, ValueError, KeyError):
        return "unknown"


def check_gate(args) -> int:
    # An inaccessible GitHub API read cannot authorize code repair.
    if not rate_budget_ok():
        print("unknown")
        return 0
    print(classify_run(args.repository, args.run_id, expected_sha=args.head_sha,
                       expected_workflow=args.workflow))
    return 0


def eligible_run(run: dict, sha: str, now: float) -> bool:
    if (run.get("event") != "pull_request" or run.get("head_sha") != sha or
        run.get("status") != "completed" or run.get("conclusion") != "failure" or
        run.get("name") not in WORKFLOWS):
        return False
    try:
        attempt = int(run["run_attempt"])
        updated = time.strptime(run["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
        age = now - calendar.timegm(updated)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return 1 <= attempt < MAX_ATTEMPTS and age >= MIN_RETRY_AGE


def reconcile(repo: str) -> int:
    # Existing CI cron is the recovery wake. One native rerun per pass.
    # No OpenCode, workflow dispatch, status-forging, or shared PAT reads.
    if repo != "kodmial/continuum" or not rate_budget_ok():
        print("GitHub rate budget unavailable/low; defer to next CI cron")
        return 0
    try:
        prs = api(f"repos/{repo}/pulls?state=open&per_page={MAX_PRS + 1}")
        if not isinstance(prs, list) or len(prs) > MAX_PRS:
            print("PR inventory incomplete; holding")
            return 0
        for pr in prs:
            sha = ((pr.get("head") or {}).get("sha") or "")
            if (not re.fullmatch(r"[0-9a-f]{40}", sha) or
                (pr.get("head") or {}).get("repo", {}).get("full_name") != repo):
                continue
            runs = api(f"repos/{repo}/actions/runs?head_sha={sha}&event=pull_request&per_page=100")
            rows = runs.get("workflow_runs", [])
            if not isinstance(rows, list) or int(runs.get("total_count", 0)) > len(rows):
                print("Run inventory incomplete; holding")
                return 0
            latest = {}
            for run in rows:
                name = run.get("name")
                if name in WORKFLOWS and (name not in latest or int(run["id"]) > int(latest[name]["id"])):
                    latest[name] = run
            for run in latest.values():
                if not eligible_run(run, sha, time.time()):
                    continue
                verdict = classify_run(repo, int(run["id"]), expected_sha=sha)
                if verdict == "unknown":
                    print("Cannot classify failure; holding")
                    return 0
                if verdict != "github-403":
                    continue
                # Revalidate exact live PR head and latest workflow before POST.
                current = api(f"repos/{repo}/pulls/{pr['number']}")
                if (current.get("state") != "open" or
                    (current.get("head") or {}).get("sha") != sha or
                    not rate_budget_ok()):
                    return 0
                newest = api(f"repos/{repo}/actions/runs?head_sha={sha}&event=pull_request&per_page=100")
                matches = [item for item in newest.get("workflow_runs", []) if item.get("name") == run.get("name")]
                if (int(newest.get("total_count", 0)) > len(newest.get("workflow_runs", [])) or
                    not matches or max(int(item["id"]) for item in matches) != int(run["id"])):
                    return 0
                fresh = api(f"repos/{repo}/actions/runs/{run['id']}")
                if not eligible_run(fresh, sha, time.time()):
                    return 0
                api(f"repos/{repo}/actions/runs/{run['id']}/rerun-failed-jobs", post=True)
                print(f"Recovered GitHub API 403 by native failed-jobs rerun {run['id']} for PR #{pr['number']}")
                return 0
    except (Unavailable, ValueError, TypeError, KeyError) as exc:
        print(f"GitHub inventory/recovery unavailable; holding ({type(exc).__name__})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("gate", "reconcile"))
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--run-id", type=int, default=0)
    p.add_argument("--head-sha", default="")
    p.add_argument("--workflow", default="")
    args = p.parse_args()
    if args.mode == "gate":
        if args.run_id <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
            print("unknown")
            return 0
        return check_gate(args)
    return reconcile(args.repository)


if __name__ == "__main__":
    sys.exit(main())
