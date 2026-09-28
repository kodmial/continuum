#!/usr/bin/env python3
"""Standalone PR-Agent review gate and finding verifier.

Enforces the CodeRabbit-equivalent review contract that PR-Agent alone does
not provide natively: stable finding identifiers, a real APPROVED /
CHANGES_REQUESTED verdict, cross-run finding tracking, and a machine-readable
/verify re-check. Uses only the standard library so it runs on stock
ubuntu-latest runners without dependency installation.

Subcommands:
  gate   --pr <number> [--head <sha>]
      Build or update the tracker comment, then submit a real GitHub review.
  verify --pr <number> --finding <id> [--head <sha>]
      Re-check one finding against the current HEAD via the configured
      OpenAI-compatible API and reply with exactly one machine-detectable
      RESOLVED or UNRESOLVED line.

Environment:
  GITHUB_TOKEN / GH_TOKEN : GitHub API token (Actions-provided GITHUB_TOKEN).
  GITHUB_REPOSITORY       : owner/repo.
  PR_AGENT_API_BASE       : OpenAI-compatible base URL (verify only).
  PR_AGENT_API_KEY        : API key (verify only).
  PR_AGENT_MODEL          : model identifier (verify only).
  PR_AGENT_MAX_TOKENS     : optional max_tokens override (verify only).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TRACKER_MARKER = "<!-- pr-agent-standalone-tracker -->"
BOT_PREFIX = "[pr-agent-standalone]"
API = "https://api.github.com"


class GitHubApiError(SystemExit):
    """GitHub API failure with the HTTP status preserved.

    Subclasses SystemExit so existing callers that expect a hard failure
    keep working, while submit_verdict can catch a 422 APPROVE rejection
    and retry the same review as COMMENT.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def gh_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not token:
        raise SystemExit("Missing GITHUB_TOKEN environment variable")
    return token


def repo():
    full = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in full:
        raise SystemExit("Missing GITHUB_REPOSITORY environment variable")
    owner, name = full.split("/", 1)
    return owner, name


def gh_request(method, path, body=None, preview=None):
    url = API + path
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + gh_token(),
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pr-agent-standalone-gate",
    }
    if preview:
        headers["Accept"] = preview
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8", "replace")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise GitHubApiError(f"GitHub API {method} {path} failed: {exc.code} {detail}", status=exc.code)


def gh_paginate(path):
    items = []
    url = API + path
    while url:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + gh_token(),
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-agent-standalone-gate",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read().decode("utf-8", "replace")
                page = json.loads(payload) if payload else []
                if isinstance(page, list):
                    items.extend(page)
                link = resp.headers.get("Link", "")
                nxt = None
                for part in link.split(","):
                    if 'rel="next"' in part:
                        m = re.search(r"<([^>]+)>", part)
                        if m:
                            nxt = m.group(1)
                url = nxt
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise GitHubApiError(f"GitHub API GET {url} failed: {exc.code} {detail}", status=exc.code)
    return items


def stable_id(file_path, line, title):
    digest = hashlib.sha256(f"{file_path}|{line}|{title}".encode("utf-8")).hexdigest()[:8]
    return f"PRA-{digest.upper()}"


def get_pr(number):
    owner, name = repo()
    return gh_request("GET", f"/repos/{owner}/{name}/pulls/{number}")


def list_pr_files(number):
    owner, name = repo()
    return gh_paginate(f"/repos/{owner}/{name}/pulls/{number}/files?per_page=100")


def list_issue_comments(number):
    owner, name = repo()
    return gh_paginate(f"/repos/{owner}/{name}/issues/{number}/comments?per_page=100")


def list_review_comments(number):
    owner, name = repo()
    return gh_paginate(f"/repos/{owner}/{name}/pulls/{number}/comments?per_page=100")


def linked_issue_numbers(pr_body):
    return sorted({int(n) for n in re.findall(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", pr_body or "", re.I)})


def is_pr_agent_comment(comment):
    body = comment.get("body", "") or ""
    user = (comment.get("user") or {}).get("login", "")
    if "pr-agent" in body.lower() and "reviewer guide" in body.lower():
        return True
    if user in ("github-actions[bot]", "github-actions"):
        markers = ("PR Reviewer Guide", "Inline issues", "Key issues", "pr-agent")
        return any(m.lower() in body.lower() for m in markers)
    return False


# Boilerplate headings whose whole markdown section must not produce findings.
# The PR-Agent Reviewer Guide lists relevant files and review effort; those
# bullets describe the diff instead of reporting actionable problems.
BOILERPLATE_SECTION_HEADINGS = (
    "reviewer guide",
    "relevant file",
    "relevant files",
    "general comment",
    "general comments",
    "estimated effort",
    "effort to review",
)

# Boilerplate phrases inside a single bullet title. Anything containing one
# of these is guide text, a file listing, or a no-op summary, not a finding.
BOILERPLATE_TITLE_PHRASES = (
    "pr reviewer guide",
    "reviewer guide",
    "relevant file",
    "relevant files",
    "no actionable",
    "no major issues",
    "looks good",
    "lgtm",
    "general comment",
    "general comments",
    "estimated effort",
    "effort to review",
    "pay attention",
    "focus on",
    "to review this",
)

# Headings whose markdown section may hold actionable findings. Summary
# bullets are only extracted from these sections so Reviewer Guide text,
# file listings, and general summaries never become findings.
FINDING_SECTION_HEADINGS = (
    "key issue",
    "key finding",
    "finding",
    "actionable",
    "inline issue",
    "inline finding",
    "issue to",
    "issues to",
    "problem",
    "concern",
    "bug",
    "vulnerability",
    "weakness",
    "risk",
)

_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.*)\s*$")
_FOCUS_AREAS_RE = re.compile(r"recommended\s+focus\s+areas\s+for\s+review", re.I)
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_strong_title(raw):
    """Strip inner HTML from a <strong> entry and normalize whitespace."""
    text = _TAG_RE.sub(" ", raw or "")
    text = re.sub(r"\s+", " ", text).strip().strip(":").strip()
    return text[:160]


def _extract_focus_area_titles(body):
    """Extract actionable issue titles from PR-Agent focus areas.

    PR-Agent v0.46.0 lists key issues under ``PR Reviewer Guide`` /
    ``Recommended focus areas for review`` using HTML ``<details>`` and
    ``<strong>`` markup. With ``inline_key_issues=false`` those issues are
    not duplicated as inline comments, so dropping the whole Reviewer Guide
    section would hide them. This helper runs before boilerplate removal
    and returns the ``<strong>`` entries within the focus-areas subsection
    so callers can feed them back into finding extraction.
    """
    if not body:
        return []
    focus_match = _FOCUS_AREAS_RE.search(body)
    if not focus_match:
        return []
    # Limit to the focus-areas subsection: from the phrase to the next
    # markdown heading that is not part of the same subsection, or EOF.
    # The focus heading itself may or may not be a markdown heading, so
    # search for the next heading after the phrase.
    rest = body[focus_match.end() :]
    next_heading = _HEADING_RE.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest
    titles = []
    for match in _STRONG_RE.finditer(section):
        title = _clean_strong_title(match.group(1))
        if len(title) < 10 or _is_boilerplate_title(title):
            continue
        titles.append(title)
    return titles


def _drop_boilerplate_sections(body):
    """Remove Reviewer Guide / file-listing sections before bullet parsing."""
    matches = list(_HEADING_RE.finditer(body or ""))
    if not matches:
        return body or ""
    kept = []
    # Text before the first heading is kept; it usually holds the summary.
    kept.append(body[: matches[0].start()])
    for i, match in enumerate(matches):
        heading = (match.group(1) or "").lower()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section = body[match.start() : end]
        if any(h in heading for h in BOILERPLATE_SECTION_HEADINGS):
            continue
        kept.append(section)
    return "".join(kept)


def _is_boilerplate_title(title):
    lowered = (title or "").lower()
    return any(s in lowered for s in BOILERPLATE_TITLE_PHRASES)


def _select_finding_text(body):
    """Keep only markdown sections that can hold actionable findings.

    Text before the first heading is a high-level summary, not a finding
    list, so it is dropped when headings exist. When no headings exist,
    the whole body is returned as a fallback so heading-free reviews
    still produce findings.

    PR-Agent key issues under ``Recommended focus areas for review`` are
    extracted before Reviewer Guide boilerplate removal and re-added as
    bullets so ``extract_findings`` still sees them.
    """
    # Extract focus-area issues first: _drop_boilerplate_sections removes
    # the entire Reviewer Guide section that contains them.
    focus_titles = _extract_focus_area_titles(body or "")
    text = _drop_boilerplate_sections(body or "")
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        base = text
    else:
        kept = []
        for i, match in enumerate(matches):
            heading = (match.group(1) or "").lower()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[match.start() : end]
            if any(h in heading for h in FINDING_SECTION_HEADINGS):
                kept.append(section)
        base = "".join(kept)
    if focus_titles:
        bullets = "\n".join(f"- {t}" for t in focus_titles)
        if base and not base.endswith("\n"):
            base += "\n"
        base += bullets + "\n"
    return base


def _parse_github_time(value):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def _comment_created(comment):
    """Freshness time of a comment, preferred for review ordering.

    Uses updated_at first so edits to the persistent PR-Agent summary
    (persistent_comment = true edits the same comment in place) count as
    new review activity. Tracker comments are excluded from the summary
    set by the callers, so their PATCH bumps never move the marker.
    """
    if comment is None:
        return None
    return _parse_github_time(comment.get("updated_at")) or _parse_github_time(
        comment.get("created_at")
    )


def _format_github_time(value):
    from datetime import timezone

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _select_current_summary_comments(pr_agent_comments, current_head, since):
    """Return only summary comments that can describe the current HEAD.

    Historical PR-Agent summaries must not reopen findings that /verify
    already resolved. Preference order: comments that mention the current
    HEAD SHA, then comments newer than the tracker resolution (fresh
    output from the latest /review run). When neither exists, return an
    empty list instead of re-reading stale output.
    """
    if not pr_agent_comments:
        return []
    if current_head:
        headed = [
            c
            for c in pr_agent_comments
            if current_head in (c.get("body") or "")
            or current_head[:7] in (c.get("body") or "")
        ]
        if headed:
            return headed
    if since is not None:
        # Inclusive bound (>=) keeps the current-HEAD summary in scope on
        # same-HEAD gate re-runs (the tracker PATCH must not hide it).
        # Reopening a /verify resolution still requires strictly fresh
        # output: upsert_tracker only reopens when the finding's
        # source_created is strictly after the persisted marker.
        fresh = [c for c in pr_agent_comments if (_comment_created(c) or since) >= since]
        if fresh:
            return fresh
        return []
    latest = None
    for c in pr_agent_comments:
        ts = _comment_created(c)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    if latest is None:
        return pr_agent_comments
    return [c for c in pr_agent_comments if _comment_created(c) == latest]


def _current_review_marker(pr_agent_comments, current_head, since, previous_marker):
    """Persisted last-/review marker: newest selected summary creation time.

    Falls back to the stored marker (or the newest PR-Agent comment when no
    marker exists) so re-running the gate on the same HEAD keeps the same
    summary in scope instead of hiding it after the tracker PATCH.
    """
    selected = _select_current_summary_comments(pr_agent_comments, current_head, since)
    times = [_comment_created(c) for c in selected]
    times = [t for t in times if t is not None]
    if times:
        return _format_github_time(max(times)), selected
    if previous_marker:
        return previous_marker, selected
    all_times = [_comment_created(c) for c in pr_agent_comments]
    all_times = [t for t in all_times if t is not None]
    if all_times:
        return _format_github_time(max(all_times)), selected
    return None, selected


def _collect_pr_agent_comments(issue_comments):
    """PR-Agent summary comments, excluding our own tracker/verdict comments."""
    collected = []
    for comment in issue_comments or []:
        if not is_pr_agent_comment(comment):
            continue
        body = comment.get("body", "") or ""
        if TRACKER_MARKER in body or body.startswith(BOT_PREFIX):
            continue
        collected.append(comment)
    return collected


def list_unresolved_thread_comment_ids(pr_number):
    """Return IDs of inline comments in unresolved, non-outdated threads.

    Uses the GraphQL reviewThreads fields isResolved and isOutdated. Returns
    None when the thread status cannot be determined so callers can fall back
    to the previous behavior instead of silently dropping findings.
    """
    owner, name = repo()
    query = (
        "query($owner: String!, $name: String!, $pr: Int!, $after: String) {"
        " repository(owner: $owner, name: $name) {"
        "  pullRequest(number: $pr) {"
        "   reviewThreads(first: 100, after: $after) {"
        "    nodes { isResolved isOutdated"
        "     comments(first: 50) { nodes { databaseId } }"
        "    }"
        "    pageInfo { hasNextPage endCursor }"
        "   }"
        "  }"
        " }"
        "}"
    )
    unresolved_ids = set()
    after = None
    try:
        while True:
            resp = gh_request(
                "POST",
                "/graphql",
                {"query": query, "variables": {"owner": owner, "name": name, "pr": pr_number, "after": after}},
            )
            if resp.get("errors") or not (
                ((resp.get("data") or {}).get("repository") or {}).get("pullRequest")
            ):
                print(
                    f"Warning: reviewThreads lookup returned errors, keeping all inline threads: "
                    f"{json.dumps(resp.get('errors'))[:500]}",
                    file=sys.stderr,
                )
                return None
            threads = (
                ((resp.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
            ).get("reviewThreads") or {}
            for node in threads.get("nodes") or []:
                if node.get("isResolved") or node.get("isOutdated"):
                    continue
                comments = (node.get("comments") or {}).get("nodes") or []
                for c in comments:
                    if c.get("databaseId") is not None:
                        unresolved_ids.add(c["databaseId"])
            page = threads.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                break
    except GitHubApiError as exc:
        print(f"Warning: reviewThreads lookup failed, keeping all inline threads: {exc}", file=sys.stderr)
        return None
    return unresolved_ids


def extract_findings(pr_number, issue_comments, review_comments, current_head=None, since=None):
    """Derive findings from PR-Agent output plus inline threads.

    Only unresolved, non-outdated review threads count, resolved via the
    GraphQL reviewThreads isResolved/isOutdated fields. Summary bullets are
    taken only from the current-HEAD report (comments mentioning
    current_head, else comments newer than since, else the latest batch) and
    only from actionable finding sections, so historical summaries and
    Reviewer Guide boilerplate never reopen resolved findings.

    Each finding carries source_created (ISO-8601 creation time of the
    PR-Agent comment or inline thread that produced it). upsert_tracker
    reopens a /verify resolution only when that timestamp is strictly after
    both the persisted last-/review marker and the finding-level resolved_at
    set by cmd_verify, so re-running the gate or pushing a new commit
    without fresh PR-Agent output never flips resolved to reopened.

    Returns a list of dicts: {id, file, line, title, source, source_created}.
    """
    findings = {}
    bullet_re = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+(.{10,300})", re.M)
    inline_num_re = re.compile(r"(?:^|[;:\n])\s*\d+[.)]\s+([A-Z].{10,200})")
    pr_agent_comments = []
    for comment in issue_comments:
        if not is_pr_agent_comment(comment):
            continue
        body = comment.get("body", "") or ""
        # Skip our own tracker/verdict comments.
        if TRACKER_MARKER in body or body.startswith(BOT_PREFIX):
            continue
        pr_agent_comments.append(comment)
    for comment in _select_current_summary_comments(pr_agent_comments, current_head, since):
        body = comment.get("body", "") or ""
        # Reviewer Guide bullets describe files/effort, not findings; only
        # actionable finding sections are parsed.
        body = _select_finding_text(body)
        created_iso = _format_github_time(_comment_created(comment))
        for match in bullet_re.finditer(body):
            title = re.sub(r"\s+", " ", match.group(1)).strip()[:160]
            if len(title) < 10 or _is_boilerplate_title(title):
                continue
            fid = stable_id("PR", 0, title)
            findings.setdefault(
                fid,
                {
                    "id": fid,
                    "file": "",
                    "line": 0,
                    "title": title,
                    "source": "summary",
                    "source_created": created_iso,
                },
            )
        for match in inline_num_re.finditer(body):
            title = re.sub(r"\s+", " ", match.group(1)).strip()[:160]
            if len(title) < 10 or _is_boilerplate_title(title):
                continue
            fid = stable_id("PR", 0, title)
            findings.setdefault(
                fid,
                {
                    "id": fid,
                    "file": "",
                    "line": 0,
                    "title": title,
                    "source": "summary",
                    "source_created": created_iso,
                },
            )
    unresolved_ids = list_unresolved_thread_comment_ids(pr_number)
    for comment in review_comments:
        body = comment.get("body", "") or ""
        user = (comment.get("user") or {}).get("login", "")
        if TRACKER_MARKER in body or body.startswith(BOT_PREFIX):
            continue
        # Any unresolved review-thread root authored after a /review run is a
        # candidate code-specific finding. Attribute PR-Agent-authored threads
        # and github-actions threads that look like review findings.
        if user not in ("github-actions[bot]", "github-actions", "pr-agent[bot]"):
            continue
        # Skip threads that GitHub marks resolved or outdated for the current
        # HEAD. Unknown IDs are kept so a GraphQL gap never hides a finding.
        if unresolved_ids is not None and comment.get("id") not in unresolved_ids:
            # Fall back to path/line matching when the REST id is missing:
            # only skip when we positively know the thread is settled.
            if comment.get("id") is not None:
                continue
        # A stale inline thread anchored to an older HEAD must not reopen a
        # resolved finding. When the current HEAD is known, require the
        # thread to be anchored to it (commit_id/original_commit_id) or to
        # be strictly newer than the persisted last-/review marker;
        # otherwise skip it. The marker check applies even when commit IDs
        # are absent, so an old thread without anchoring info cannot reopen
        # a /verify resolution without fresh output.
        if current_head:
            commit_id = comment.get("commit_id") or ""
            original_commit_id = comment.get("original_commit_id") or ""
            anchored = current_head in (commit_id, original_commit_id)
            if not anchored:
                if since is None:
                    continue
                ts = _comment_created(comment)
                if ts is None or ts <= since:
                    continue
        title = re.sub(r"\s+", " ", body).strip()[:160]
        if len(title) < 10 or _is_boilerplate_title(title):
            continue
        path = comment.get("path", "") or ""
        line = comment.get("line") or comment.get("original_line") or 0
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = 0
        fid = stable_id(path or "PR", line, title)
        findings.setdefault(
            fid,
            {
                "id": fid,
                "file": path,
                "line": line,
                "title": title,
                "source": "inline",
                "source_created": _format_github_time(_comment_created(comment)),
            },
        )
    return sorted(findings.values(), key=lambda f: f["id"])


def load_tracker(issue_comments):
    for comment in reversed(issue_comments):
        body = comment.get("body", "") or ""
        if TRACKER_MARKER not in body:
            continue
        m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
        if not m:
            continue
        try:
            state = json.loads(m.group(1))
            state.setdefault("findings", [])
            state.setdefault("head", "")
            state.setdefault("last_review_at", None)
            return comment, state
        except json.JSONDecodeError:
            continue
    return None, {"findings": [], "head": "", "last_review_at": None}


def render_tracker(head_sha, findings, last_review_at=None):
    lines = [
        f"{BOT_PREFIX} Review tracker for HEAD `{head_sha}`",
        "",
        TRACKER_MARKER,
        "",
        "| Finding | Location | Title | Status |",
        "| --- | --- | --- | --- |",
    ]
    if findings:
        for f in findings:
            loc = f"{f['file']}:{f['line']}" if f.get("file") else "general"
            lines.append(f"| `{f['id']}` | {loc} | {f['title'][:120]} | {f['status']} |")
    else:
        lines.append("| — | — | No actionable findings | clean |")
    lines += [
        "",
        "<details><summary>Machine-readable state</summary>",
        "",
        "```json",
        json.dumps({"head": head_sha, "findings": findings, "last_review_at": last_review_at}, indent=2),
        "```",
        "",
        "</details>",
        "",
        "Re-check one finding after fixes with `/verify <finding-id>` (e.g. `/verify PRA-1234ABCD`).",
    ]
    return "\n".join(lines)


def write_tracker(pr_number, head_sha, findings, last_review_at=None):
    owner, name = repo()
    existing, previous_state = load_tracker(list_issue_comments(pr_number))
    # Preserve the persisted last-/review marker across tracker rewrites
    # (including /verify) unless the caller supplies a newer one.
    if last_review_at is None:
        last_review_at = (previous_state or {}).get("last_review_at")
    body = render_tracker(head_sha, findings, last_review_at)
    if existing:
        gh_request(
            "PATCH",
            f"/repos/{owner}/{name}/issues/comments/{existing['id']}",
            {"body": body},
        )
    else:
        gh_request(
            "POST",
            f"/repos/{owner}/{name}/issues/{pr_number}/comments",
            {"body": body},
        )
    return findings


def upsert_tracker(pr_number, head_sha, current, previous_state, last_review_at=None):
    prev = {f.get("id"): f for f in previous_state.get("findings", [])}
    prev_head = previous_state.get("head", "")
    previous_marker = (previous_state or {}).get("last_review_at")
    since_dt = _parse_github_time(previous_marker) if previous_marker else None
    # A reappearance is new evidence only when its source comment was
    # created strictly after both the persisted last-/review marker and the
    # finding-level /verify resolution time. The summary selector is
    # inclusive (>=) so same-HEAD re-runs keep the current report in scope
    # instead of hiding it, but reopening here requires strict freshness:
    # re-selecting the same summary (created == marker) after /verify, or
    # keeping a stale inline thread, must not flip a resolved finding back
    # to reopened. In particular, an inline comment posted after the review
    # summary but before /verify has a creation time after the marker, so
    # the marker alone cannot distinguish it from fresh output; the
    # per-finding resolved_at set by cmd_verify provides the review-run
    # boundary. A HEAD change alone is never sufficient without fresh
    # PR-Agent output for the current HEAD.
    merged = []
    for finding in current:
        old = prev.get(finding["id"])
        if old is None:
            entry = {**finding, "status": "open"}
            entry.pop("resolved_at", None)
        elif old.get("status") in ("resolved", "fixed"):
            # Preserve a /verify resolution unless fresh PR-Agent output for
            # the current HEAD reports the finding again with a source
            # created after the stored marker and after the verify time.
            # Without any boundary (first run) fall back to HEAD-change
            # reopening; with a boundary, unknown source times never reopen.
            source_dt = _parse_github_time(finding.get("source_created"))
            resolved_dt = _parse_github_time(old.get("resolved_at"))
            bounds = [d for d in (since_dt, resolved_dt) if d is not None]
            # Fresh PR-Agent output reopens whenever its source timestamp is
            # strictly after the latest available bound, even on the same
            # HEAD. Without bounds, a HEAD change is the only reopen signal.
            if bounds and source_dt is not None and source_dt > max(bounds):
                entry = {**finding, "status": "reopened"}
                entry.pop("resolved_at", None)
            elif not bounds and prev_head and head_sha != prev_head:
                entry = {**finding, "status": "reopened"}
            else:
                entry = {**finding, "status": "resolved"}
                if old.get("resolved_at"):
                    entry["resolved_at"] = old["resolved_at"]
        else:
            entry = {**finding, "status": "still-open"}
            entry.pop("resolved_at", None)
        merged.append(entry)
    for fid, old in sorted(prev.items()):
        if fid not in {f["id"] for f in current}:
            entry = dict(old)
            if entry.get("status") in ("open", "still-open", "reopened"):
                entry["status"] = "resolved"
                entry.pop("resolved_at", None)
            merged.append(entry)
    merged.sort(key=lambda f: f["id"])
    marker = last_review_at if last_review_at is not None else previous_state.get("last_review_at")
    return write_tracker(pr_number, head_sha, merged, marker)


# Phrases in PR-Agent output suggesting chunk limits or large-patch clipping
# left part of the diff unreviewed. Matched case-insensitively.
COVERAGE_GAP_PHRASES = (
    "remaining files",
    "not reviewed",
    "unreviewed",
    "truncated",
    "clipped",
    "chunk limit",
    "max_number_of_calls",
    "max number of calls",
    "partial review",
    "incomplete coverage",
    "coverage gap",
    "not covered",
    "omitted files",
    "skipped files",
)

# Heuristic budget matching .pr_agent.toml chunking. Beyond this the pinned
# packer (max_number_of_calls) or large_patch_policy=clip may omit content.
CHUNK_FILE_BUDGET = 5
CHUNK_CHURN_BUDGET = 15000
CLIP_FILE_CHURN_BUDGET = 10000

# A clean APPROVE is only safe without positive coverage evidence when the
# diff trivially fits in one chunk. Chunking is token-based, not file-based,
# so a PR with few files but large or dense files can still exceed
# max_number_of_calls=5 chunks, and large_patch_policy=clip can omit part of
# a patch without any explicit notice. Generic summary phrases such as
# "review complete" are not reliable coverage evidence: they are not tied to
# the current HEAD run or to the content processed by the packer, so they
# never justify a clean full-PR verdict on their own.
SAFE_APPROVE_MAX_FILES = 1
SAFE_APPROVE_MAX_CHURN = 500

def detect_coverage_gap(issue_comments, files, current_head=None, since=None):
    """Detect when chunk limits or patch clipping may leave code unreviewed.

    Returns a warning string when coverage is incomplete, else None. Only
    PR-Agent output selected for the current HEAD run counts as evidence
    (comments mentioning current_head, else comments newer than the persisted
    last-/review marker, else the latest batch). Historical summaries and
    generic full-coverage phrases never establish complete coverage on their
    own: chunking is token-based and large_patch_policy=clip can omit content
    without any explicit notice, so any non-trivial diff withholds a clean
    APPROVE until reliable per-run coverage evidence exists.
    """
    pr_agent_comments = _collect_pr_agent_comments(issue_comments)
    selected = _select_current_summary_comments(pr_agent_comments, current_head, since)
    # No PR-Agent output for the current HEAD run means coverage cannot be
    # established for this HEAD, even if an older run claimed completeness.
    if current_head and files and not selected:
        return (
            "No PR-Agent review output was found for the current HEAD, so "
            "chunk limits or large-patch clipping may have left content "
            "unreviewed. Zero findings cannot be treated as a clean "
            "full-PR review."
        )
    bodies = []
    for comment in selected:
        body = comment.get("body", "") or ""
        bodies.append(body)
    haystack = "\n".join(bodies).lower()
    for phrase in COVERAGE_GAP_PHRASES:
        if phrase in haystack:
            return (
                f"PR-Agent output mentions {phrase!r}, so chunk limits or "
                "large-patch clipping may have left content unreviewed."
            )
    files = files or []
    if len(files) > CHUNK_FILE_BUDGET:
        largest = sorted(
            files, key=lambda f: f.get("additions", 0) + f.get("deletions", 0), reverse=True
        )[:3]
        names = ", ".join(f.get("filename", "?") for f in largest)
        return (
            f"PR touches {len(files)} files, exceeding the "
            f"max_number_of_calls={CHUNK_FILE_BUDGET} chunk budget; "
            f"remaining files may not have been reviewed (largest: {names})."
        )
    clipped = [
        f
        for f in files
        if f.get("additions", 0) + f.get("deletions", 0) > CLIP_FILE_CHURN_BUDGET
    ]
    if clipped:
        names = ", ".join(f.get("filename", "?") for f in clipped[:3])
        return (
            f"Oversized patch(es) ({names}) exceed the large_patch_policy "
            "clip budget and part of the diff may have been omitted."
        )
    total = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    if total > CHUNK_CHURN_BUDGET:
        return (
            f"PR churn (+{total} lines) exceeds the {CHUNK_CHURN_BUDGET}-line "
            "chunking budget; some chunks may not have been reviewed."
        )
    # Silent gaps: few files can still need more than max_number_of_calls
    # chunks, and clip can omit content below the heuristic budgets without
    # any explicit notice. Generic full-coverage phrases are not tied to the
    # packer output and never establish coverage, so any diff beyond a
    # trivially small single-file change withholds a clean verdict.
    if files:
        if len(files) > SAFE_APPROVE_MAX_FILES or total > SAFE_APPROVE_MAX_CHURN:
            return (
                f"PR touches {len(files)} file(s) with +{total} changed lines; "
                "chunking is token-based, so max_number_of_calls=5 chunks and "
                "large_patch_policy=clip may leave content unreviewed even "
                "below the heuristic budgets. Generic full-coverage phrases "
                "in the review output are not tied to the current HEAD run "
                "or the packer content and cannot prove complete coverage. "
                "Zero findings cannot be treated as a clean full-PR review."
            )
    return None


def submit_verdict(
    pr_number, head_sha, findings, pr, issue_comments=None, files_override=None, since=None
):
    owner, name = repo()
    open_findings = [f for f in findings if f.get("status") in ("open", "still-open", "reopened")]
    # GitHub review events accept REQUEST_CHANGES, not CHANGES_REQUESTED
    # (which is the resulting review state). The printed/returned verdict
    # keeps the CHANGES_REQUESTED label.
    event = "REQUEST_CHANGES" if open_findings else "APPROVE"
    linked = linked_issue_numbers(pr.get("body", ""))
    files = files_override if files_override is not None else list_pr_files(pr_number)
    total_add = sum(f.get("additions", 0) for f in files)
    total_del = sum(f.get("deletions", 0) for f in files)
    coverage_gap = detect_coverage_gap(issue_comments or [], files, head_sha, since)
    summary = [
        f"{BOT_PREFIX} Full review of HEAD `{head_sha}`: {len(files)} file(s), +{total_add}/-{total_del}.",
        "",
        f"Actionable findings: {len(open_findings)}.",
    ]
    for f in open_findings:
        loc = f"{f['file']}:{f['line']}" if f.get("file") else "general"
        summary.append(f"- `{f['id']}` {loc} — {f['title'][:160]}")
    if not open_findings:
        if coverage_gap:
            # Do not report a clean full-PR verdict when chunk limits or
            # large-patch clipping may have left content unreviewed.
            event = "COMMENT"
            summary.append(
                "Coverage incomplete: zero findings cannot be treated as a "
                f"clean full-PR review. {coverage_gap} Re-run /review after "
                "narrowing the diff or raising the chunk budget."
            )
        else:
            summary.append("No actionable findings remain on this HEAD.")
    elif coverage_gap:
        summary.append(f"Coverage note: {coverage_gap}")
    summary += [
        "",
        "Coverage: summary, inline file/line threads where applicable, linked-issue "
        f"compliance ({'none linked' if not linked else 'linked: ' + ', '.join('#' + str(n) for n in linked)}), "
        "security-sensitive review, and test-gap check are included in the PR-Agent "
        "review and this tracker. Large diffs are reviewed across the whole PR via chunking.",
        "",
        "This standalone review is advisory only and is not a merge gate.",
    ]
    try:
        gh_request(
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
            {"commit_id": head_sha, "body": "\n".join(summary), "event": event},
        )
    except GitHubApiError as exc:
        # When Actions cannot approve PRs, GitHub rejects APPROVE with 422.
        # Record the same verdict as a COMMENT so the run still reports.
        if event == "APPROVE" and exc.status == 422:
            print(f"APPROVE rejected (422); retrying as COMMENT: {exc}", file=sys.stderr)
            gh_request(
                "POST",
                f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
                {"commit_id": head_sha, "body": "\n".join(summary), "event": "COMMENT"},
            )
            print(f"Submitted COMMENT (APPROVE fallback) for PR #{pr_number} HEAD {head_sha}")
            return "COMMENT"
        raise
    verdict = "CHANGES_REQUESTED" if event == "REQUEST_CHANGES" else event
    print(f"Submitted {verdict} for PR #{pr_number} HEAD {head_sha} with {len(open_findings)} open findings")
    return verdict


def cmd_gate(pr_number, head_sha=None):
    pr = get_pr(pr_number)
    head = head_sha or pr["head"]["sha"]
    issue_comments = list_issue_comments(pr_number)
    review_comments = list_review_comments(pr_number)
    _, previous = load_tracker(issue_comments)
    # Use the persisted last-/review marker, not the tracker comment's
    # updated_at (which moves on every PATCH, including /verify updates).
    previous_marker = (previous or {}).get("last_review_at")
    since = _parse_github_time(previous_marker) if previous_marker else None
    current = extract_findings(pr_number, issue_comments, review_comments, current_head=head, since=since)
    pr_agent_comments = _collect_pr_agent_comments(issue_comments)
    new_marker, _ = _current_review_marker(pr_agent_comments, head, since, previous_marker)
    merged = upsert_tracker(pr_number, head, current, previous, new_marker)
    return submit_verdict(pr_number, head, merged, pr, issue_comments, None, since)


def openai_chat(prompt, system="You are a precise code-review verifier."):
    base = os.environ.get("PR_AGENT_API_BASE", "").rstrip("/")
    key = os.environ.get("PR_AGENT_API_KEY", "")
    model = os.environ.get("PR_AGENT_MODEL", "")
    # Strip the LiteLLM routing prefix before calling the provider directly.
    # The review job routes models as openai/<name> for LiteLLM, but the
    # provider's chat-completions endpoint expects the bare model name.
    for prefix in ("openai/", "text-completion-openai/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    max_tokens_raw = os.environ.get("PR_AGENT_MAX_TOKENS", "").strip()
    if not base or not key or not model:
        raise SystemExit("Missing PR_AGENT_API_BASE, PR_AGENT_API_KEY, or PR_AGENT_MODEL")
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    if max_tokens_raw:
        try:
            payload["max_tokens"] = int(max_tokens_raw)
        except ValueError:
            raise SystemExit("PR_AGENT_MAX_TOKENS must be an integer")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise SystemExit(f"LLM request failed: {exc.code} {detail}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit(f"Unexpected LLM response: {json.dumps(data)[:2000]}")


def get_file_at_ref(path, ref):
    owner, name = repo()
    query = urllib.parse.urlencode({"ref": ref})
    try:
        data = gh_request("GET", f"/repos/{owner}/{name}/contents/{urllib.parse.quote(path)}?{query}")
    except SystemExit:
        return None
    content = data.get("content", "")
    encoding = data.get("encoding", "")
    if encoding == "base64":
        import base64

        try:
            return base64.b64decode(content).decode("utf-8", "replace")
        except Exception:
            return None
    return None


def cmd_verify(pr_number, finding_id, head_sha=None):
    pr = get_pr(pr_number)
    head = head_sha or pr["head"]["sha"]
    issue_comments = list_issue_comments(pr_number)
    review_comments = list_review_comments(pr_number)
    _, state = load_tracker(issue_comments)
    target = None
    for f in state.get("findings", []):
        if f.get("id", "").upper() == finding_id.upper():
            target = f
            break
    if target is None:
        # Fall back to deriving the ID space from current PR-Agent output so a
        # finding posted before the tracker existed can still be verified.
        for f in extract_findings(
            pr_number, issue_comments, review_comments, current_head=head
        ):
            if f["id"].upper() == finding_id.upper():
                target = {**f, "status": "open"}
                break
    if target is None:
        raise SystemExit(f"Unknown finding {finding_id}; check the tracker comment for valid IDs")
    snippet = ""
    if target.get("file"):
        full = get_file_at_ref(target["file"], head)
        if full is not None:
            lines = full.splitlines()
            line_no = int(target.get("line") or 0)
            if line_no > 0:
                lo = max(0, line_no - 30)
                hi = min(len(lines), line_no + 30)
                snippet = "\n".join(f"{i + 1}:{lines[i]}" for i in range(lo, hi))
            else:
                snippet = "\n".join(f"{i + 1}:{line_text}" for i, line_text in enumerate(lines[:120]))
    if not snippet:
        # Summary findings carry no file/line, so verify them against the
        # current PR diff instead of asking the model to guess from the title.
        truncated = False
        try:
            pr_files = list_pr_files(pr_number) or []
            title_lower = (target.get("title") or "").lower()

            def _mentions_target(filename):
                name = (filename or "").lower()
                if not name or not title_lower:
                    return False
                if name in title_lower:
                    return True
                base = name.rsplit("/", 1)[-1]
                return bool(base) and base in title_lower

            # Show the most relevant patches first so a 6000-char budget
            # still contains the code a summary finding refers to.
            pr_files = sorted(
                pr_files,
                key=lambda pf: (0 if _mentions_target(pf.get("filename")) else 1),
            )
            diff_parts = []
            budget = 6000
            for pr_file in pr_files:
                patch = pr_file.get("patch") or ""
                if not patch:
                    continue
                chunk = f"--- {pr_file.get('filename', '?')}\n{patch}"
                used = sum(len(p) for p in diff_parts) + len(diff_parts)
                if len(chunk) > budget - used:
                    remaining = budget - used
                    if remaining <= 0:
                        truncated = True
                        break
                    chunk = chunk[:remaining]
                    diff_parts.append(chunk)
                    truncated = True
                    break
                diff_parts.append(chunk)
                if sum(len(p) for p in diff_parts) + len(diff_parts) >= budget:
                    truncated = True
                    break
            snippet = "\n\n".join(diff_parts)[:6000]
            if len("\n\n".join(diff_parts)) > len(snippet):
                truncated = True
        except (GitHubApiError, OSError, ValueError):
            snippet = ""
            truncated = False
    if not snippet:
        verdict = "UNRESOLVED"
        answer = "UNRESOLVED — no current-HEAD code context was available to verify this finding."
    else:
        is_summary = not target.get("file")
        if is_summary and truncated:
            context_label = (
                "Partial PR diff (may be truncated and may not contain "
                "the relevant code):\n"
            )
        else:
            context_label = "Current code around the finding:\n"
        prompt = (
            f"Re-check finding {target['id']} against the current PR HEAD.\n"
            f"Finding title: {target['title']}\n"
            f"File: {target.get('file') or 'general'} Line: {target.get('line') or 0}\n"
            f"PR HEAD: {head}\n\n"
            f"{context_label}{snippet[:6000]}\n\n"
            + (
                "If the code relevant to this finding is not present above, "
                "reply UNRESOLVED.\n"
                if is_summary
                else ""
            )
            + "Reply with exactly one machine-detectable first line: either RESOLVED "
            "when the underlying problem is fully fixed, or UNRESOLVED when it still "
            "exists. After that line, explain the precise remaining problem or why it "
            "is fixed. Do not report a new finding ID."
        )
        answer = openai_chat(prompt).strip()
        first = answer.splitlines()[0].strip().upper() if answer else ""
        if first.startswith("RESOLVED"):
            verdict = "RESOLVED"
        elif first.startswith("UNRESOLVED"):
            verdict = "UNRESOLVED"
        else:
            # Force machine-detectability: treat unparseable answers as unresolved
            # but keep the model text for human inspection.
            verdict = "UNRESOLVED"
            answer = "UNRESOLVED — verifier returned a non-conforming first line.\n\n" + answer
    owner, name = repo()
    body = (
        f"{BOT_PREFIX} `/verify {target['id']}` against HEAD `{head}`\n\n"
        f"{verdict}\n\n{answer}\n\n"
        f"Finding: `{target['id']}` ({target.get('file') or 'general'}:{target.get('line') or 0}) — {target['title'][:200]}"
    )
    # Reply without duplicating the finding: post one issue comment (not a new
    # review thread) and update the tracker status in place.
    gh_request("POST", f"/repos/{owner}/{name}/issues/{pr_number}/comments", {"body": body})
    from datetime import datetime, timezone

    verify_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    findings = state.get("findings", [])
    if not any(f.get("id", "").upper() == target["id"].upper() for f in findings):
        entry = {**target, "status": "resolved" if verdict == "RESOLVED" else "still-open"}
        if verdict == "RESOLVED":
            entry["resolved_at"] = verify_at
        findings.append(entry)
    for f in findings:
        if f.get("id", "").upper() == target["id"].upper():
            f["status"] = "resolved" if verdict == "RESOLVED" else "still-open"
            if verdict == "RESOLVED":
                f["resolved_at"] = verify_at
            else:
                f.pop("resolved_at", None)
    # Preserve the persisted last-/review marker: /verify must not move it,
    # otherwise the next gate run would treat old summaries as fresh output.
    write_tracker(pr_number, head, sorted(findings, key=lambda f: f["id"]), state.get("last_review_at"))
    print(f"{verdict} for {target['id']}")
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description="PR-Agent standalone review gate")
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("gate", help="Update tracker and submit APPROVED/CHANGES_REQUESTED")
    gate.add_argument("--pr", type=int, required=True)
    gate.add_argument("--head", default=None)
    verify = sub.add_parser("verify", help="Re-check one finding")
    verify.add_argument("--pr", type=int, required=True)
    verify.add_argument("--finding", required=True)
    verify.add_argument("--head", default=None)
    args = parser.parse_args(argv)
    if args.command == "gate":
        event = cmd_gate(args.pr, args.head)
        print(event)
    else:
        print(cmd_verify(args.pr, args.finding, args.head))


if __name__ == "__main__":
    main()
