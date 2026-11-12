#!/usr/bin/env python3
"""Safe live OpenCode event metadata for GitHub issue comments.

Active Actions job logs can be unavailable through the REST API. This tails the
existing --format json stream. Never publish prompts, prose, tool inputs,
outputs, file paths, command titles, or exception message text.
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib import error, request

_SAFE_TOOLS = frozenset({"bash", "shell", "read", "write", "edit", "patch", "apply_patch", "grep", "glob", "websearch", "webfetch", "skill"})
_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STOP = False


def now():
    return datetime.now(timezone.utc)


def event_metadata(line):
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    kind = record.get("type")
    if kind == "tool_use":
        part = record.get("part") if isinstance(record.get("part"), dict) else {}
        tool = part.get("tool")
        tool = tool.lower() if isinstance(tool, str) and tool.lower() in _SAFE_TOOLS else "other"
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        status = status if status in ("pending", "running", "completed", "error") else "unknown"
        return "tool: {} ({})".format(tool, status)
    if kind == "error":
        payload = record.get("error") if isinstance(record.get("error"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        msg = str(data.get("message") or payload.get("message") or "").lower()
        if "web search" in msg and ("cancel" in msg or "input" in msg):
            category = "web-search-input-unavailable"
        elif "429" in msg or data.get("statusCode") == 429:
            category = "provider-429"
        elif "timeout" in msg or "timed out" in msg:
            category = "timeout"
        else:
            category = "other"
        return "error: {}".format(category)
    if kind in ("step_start", "step_finish", "text"):
        return "text (content withheld)" if kind == "text" else kind
    return None


class Snapshot:
    def __init__(self):
        self.count = 0
        self.last = None
        self.recent = deque(maxlen=8)

    def ingest(self, line, observed=None):
        entry = event_metadata(line)
        if entry is None:
            return
        t = observed or now()
        self.count += 1
        self.last = t
        self.recent.append("{} UTC · {}".format(t.strftime("%H:%M:%S"), entry))

    def render(self, repo, issue, run, state="running", observed=None):
        t = observed or now()
        ago = "no events yet" if self.last is None else "{}s since last event".format(
            max(0, int((t - self.last).total_seconds())))
        lines = [
            "<!-- continuum-opencode-live repo={} issue={} run={} -->".format(repo, issue, run),
            "**Continuum · OpenCode live events**",
            "Run: https://github.com/{}/actions/runs/{}".format(repo, run),
            "State: **{}** · updated {} UTC".format(state, t.strftime("%Y-%m-%d %H:%M:%S")),
            "JSON events: **{}** · {}".format(self.count, ago),
            "Recent metadata only (no prompts, tool content or secrets):",
        ]
        lines += ["- " + item for item in self.recent]
        if not self.recent:
            lines.append("- awaiting first JSON event")
        return "\n".join(lines)


def consume(path, offset, snapshot):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    stream.seek(before)
                    break
                snapshot.ingest(line)
            return stream.tell()
    except FileNotFoundError:
        return offset


def settings():
    repo = os.getenv("GITHUB_REPOSITORY", "")
    issue = os.getenv("CONTINUUM_LIVE_LOG_ISSUE", "")
    run = os.getenv("GITHUB_RUN_ID", "")
    token = os.getenv("CONTINUUM_LIVE_LOG_TOKEN", "")
    api = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    runner_temp = os.getenv("RUNNER_TEMP", "")
    if not (_SAFE_REPO.fullmatch(repo) and issue.isdecimal() and int(issue) > 0
            and run.isdecimal() and int(run) > 0 and token and runner_temp
            and api.startswith("https://")):
        return None
    return repo, int(issue), int(run), token, api, Path(runner_temp) / ("opencode-live-{}.id".format(run))


def publish(cfg, snapshot, state):
    repo, issue, run, token, api, marker_file = cfg
    old = marker_file.read_text().strip() if marker_file.exists() else ""
    if old.isdecimal() and int(old) > 0:
        url = "{}/repos/{}/issues/comments/{}".format(api, repo, old)
        method = "PATCH"
    else:
        url = "{}/repos/{}/issues/{}/comments".format(api, repo, issue)
        method = "POST"
    req = request.Request(url, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, data=json.dumps({"body": snapshot.render(repo, issue, run, state)}).encode("utf-8"))
    with request.urlopen(req, timeout=8) as response:
        if method == "POST":
            comment_id = json.load(response).get("id")
            if not isinstance(comment_id, int) or comment_id <= 0:
                raise ValueError("No comment id")
            temp = marker_file.with_suffix(".new")
            temp.write_text(str(comment_id), encoding="ascii")
            os.replace(temp, marker_file)


def signal_stop(_signum, _frame):
    global _STOP
    _STOP = True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("watch", "once"))
    parser.add_argument("--log", required=True)
    parser.add_argument("--state", default="running", choices=("running", "finished"))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    cfg = settings()
    if cfg is None:
        return 0
    snapshot = Snapshot()
    if args.mode == "once":
        consume(args.log, 0, snapshot)
        try:
            publish(cfg, snapshot, args.state)
        except (OSError, ValueError, error.URLError):
            pass
        return 0
    signal.signal(signal.SIGTERM, signal_stop)
    signal.signal(signal.SIGINT, signal_stop)
    next_update, offset = 0, 0
    while not _STOP:
        offset = consume(args.log, offset, snapshot)
        if time.monotonic() >= next_update:
            try:
                publish(cfg, snapshot, "running")
            except (OSError, ValueError, error.URLError):
                pass
            next_update = time.monotonic() + max(30, args.interval)
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
