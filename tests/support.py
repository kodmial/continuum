"""Deterministic test doubles for the Continuum review gate.

No test performs a network call or a live model call.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Set

from continuum import config as config_module
from continuum.review import pr_agent
from continuum.review.snapshot import ProviderSnapshot
from continuum.review.tracker import TRACKER_MARKER

HEAD_A = "a" * 40
HEAD_B = "b" * 40


def review_config(
    provider: str = config_module.PROVIDER_PR_AGENT,
    *,
    block_merge: bool = True,
    status_context: str = config_module.DEFAULT_STATUS_CONTEXT,
) -> config_module.ContinuumConfig:
    return config_module.ContinuumConfig(
        version=1,
        review=config_module.ReviewSettings(
            provider=provider,
            block_merge=block_merge,
            status_context=status_context,
        ),
    )


def issue_comment(
    body: str,
    *,
    login: str = "github-actions[bot]",
    created_at: str = "2026-09-01T00:00:00Z",
    updated_at: Optional[str] = None,
    comment_id: int = 1,
) -> Dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "created_at": created_at,
        "updated_at": updated_at or created_at,
    }


def review_comment(
    body: str,
    *,
    path: str = "",
    line: int = 0,
    login: str = "github-actions[bot]",
    commit_id: str = HEAD_A,
    original_commit_id: Optional[str] = None,
    comment_id: int = 1,
    created_at: str = "2026-09-01T00:00:00Z",
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "path": path,
        "line": line,
        "user": {"login": login},
        "commit_id": commit_id,
        "original_commit_id": original_commit_id if original_commit_id is not None else commit_id,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
    }


def pr_agent_report(head: str, bullets: Sequence[str], *, at: str = "2026-09-01T00:00:00Z") -> str:
    """A PR-Agent style report that names the current HEAD."""

    rendered = "\n".join(f"<strong>{bullet}</strong>" for bullet in bullets)
    return (
        f"## PR Reviewer Guide\n\n"
        f"HEAD `{head}`\n\n"
        f"<details><summary>Recommended focus areas for review</summary>\n"
        f"<ul>\n{rendered}\n</ul>\n</details>\n\n"
        f"## Inline issues (commented on 2 changed files)\n\n"
    )


def clean_pr_agent_report(head: str, *, at: str = "2026-09-01T00:00:00Z") -> str:
    return (
        f"## PR Reviewer Guide\n\n"
        f"HEAD `{head}`\n\n"
        f"<details><summary>Recommended focus areas for review</summary>\n"
        f"<ul>\n<strong>No actionable issues were found in the reviewed changes.</strong>\n</ul>\n"
        f"</details>\n\n"
        f"## Review completed\n"
    )


def snapshot(
    *,
    summary_comments: Optional[List[Dict[str, Any]]] = None,
    review_comments: Optional[List[Dict[str, Any]]] = None,
    unresolved_ids: Optional[Set[int]] = None,
    unknown_thread_state: bool = False,
    extra_reason: Optional[str] = None,
    forced_open_titles: Optional[List[str]] = None,
    provider_decision: Optional[str] = None,
    bot_login: str = "github-actions[bot]",
) -> ProviderSnapshot:
    bot_logins, review_bots, markers = pr_agent.profile_for(bot_login)
    return ProviderSnapshot(
        provider=pr_agent.PROVIDER_NAME,
        bot_logins=tuple(bot_logins),
        review_bots=tuple(review_bots),
        output_markers=tuple(markers),
        summary_comments=list(summary_comments or []),
        review_comments=list(review_comments or []),
        unresolved_ids=None if unknown_thread_state else set() if unresolved_ids is None else unresolved_ids,
        summary_parser=pr_agent.parse_summary,
        is_boilerplate=pr_agent.is_boilerplate_title,
        extra_reason=extra_reason,
        forced_open_titles=list(forced_open_titles or []),
        provider_decision=provider_decision,
    )


class GraphQLFailure(RuntimeError):
    """Simulates an unusable reviewThreads response."""


class FakeGitHub:
    """In-memory stand-in for `GitHubClient`."""

    def __init__(
        self,
        *,
        repository: str = "kodmial/continuum",
        head: str = HEAD_A,
        body: str = "",
        files: Optional[List[Dict[str, Any]]] = None,
        issue_comments: Optional[List[Dict[str, Any]]] = None,
        review_comments: Optional[List[Dict[str, Any]]] = None,
        reviews: Optional[List[Dict[str, Any]]] = None,
        statuses: Optional[List[Dict[str, Any]]] = None,
        file_contents: Optional[Dict[str, str]] = None,
        unresolved_ids: Optional[Set[int]] = None,
        thread_state: str = "ok",
    ) -> None:
        self.repository = repository
        self.head = head
        self.pr: Dict[str, Any] = {
            "number": 7,
            "head": {"sha": head},
            "body": body,
        }
        self.files = list(files if files is not None else [{"filename": "app.py", "additions": 1, "deletions": 0}])
        self.issue_comments: List[Dict[str, Any]] = list(issue_comments or [])
        self.review_comments: List[Dict[str, Any]] = list(review_comments or [])
        self.reviews: List[Dict[str, Any]] = list(reviews or [])
        self.statuses: List[Dict[str, Any]] = list(statuses or [])
        self.file_contents = dict(file_contents or {})
        self._unresolved_ids = set() if unresolved_ids is None else set(unresolved_ids)
        self.thread_state = thread_state
        self.calls: List[Any] = []
        self.reviews_created: List[Dict[str, Any]] = []
        self.statuses_created: List[Dict[str, Any]] = []
        self.comments_created: List[Dict[str, Any]] = []
        self.fail_approve_with_422 = False
        self._next_comment_id = 1000

    # -- reads -------------------------------------------------------------
    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))

    def get_pull(self, number: int) -> Dict[str, Any]:
        self._record("get_pull", number)
        return self.pr

    def list_pull_files(self, number: int) -> List[Dict[str, Any]]:
        self._record("list_pull_files", number)
        return list(self.files)

    def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
        self._record("list_issue_comments", number)
        return list(self.issue_comments)

    def list_review_comments(self, number: int) -> List[Dict[str, Any]]:
        self._record("list_review_comments", number)
        return list(self.review_comments)

    def list_reviews(self, number: int) -> List[Dict[str, Any]]:
        self._record("list_reviews", number)
        return list(self.reviews)

    def combined_status_for_ref(self, ref: str) -> Dict[str, Any]:
        self._record("combined_status_for_ref", ref)
        if self.thread_state == "status-failure":
            raise RuntimeError("status endpoint unavailable")
        return {"statuses": [status for status in self.statuses if status.get("sha") in (None, ref)]}

    def unresolved_thread_comment_ids(self, pr_number: int) -> Optional[Set[int]]:
        self._record("unresolved_thread_comment_ids", pr_number)
        if self.thread_state == "graphql-error":
            return None
        if self.thread_state == "graphql-exception":
            raise GraphQLFailure("reviewThreads lookup failed")
        return set(self._unresolved_ids)

    def file_at_ref(self, path: str, ref: str) -> Optional[str]:
        self._record("file_at_ref", path, ref)
        return self.file_contents.get(path)

    # -- writes ------------------------------------------------------------
    def create_review(self, pr_number: int, head_sha: str, body: str, event: str) -> Dict[str, Any]:
        from continuum.review.github import GitHubError

        self._record("create_review", pr_number, head_sha, event)
        if event == "APPROVE" and self.fail_approve_with_422:
            raise GitHubError("GitHub API POST /reviews failed with status 422", status=422)
        self.reviews_created.append(
            {"pr": pr_number, "head": head_sha, "event": event, "body": body}
        )
        return {"id": len(self.reviews_created)}

    def create_status(self, head_sha: str, state: str, description: str, context: str) -> Dict[str, Any]:
        self._record("create_status", head_sha, state, context)
        self.statuses_created.append(
            {
                "head": head_sha,
                "state": state,
                "description": description,
                "context": context,
            }
        )
        return {"id": len(self.statuses_created)}

    def create_issue_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        self._record("create_issue_comment", issue_number)
        self._next_comment_id += 1
        comment = issue_comment(
            body,
            comment_id=self._next_comment_id,
            created_at="2026-09-02T00:00:00Z",
        )
        self.comments_created.append(comment)
        self.issue_comments.append(comment)
        return comment

    def update_issue_comment(self, comment_id: int, body: str) -> Dict[str, Any]:
        self._record("update_issue_comment", comment_id)
        for comment in self.issue_comments:
            if comment.get("id") == comment_id:
                comment["body"] = body
                comment["updated_at"] = "2026-09-02T00:00:00Z"
        return {"id": comment_id}

    def upsert_marker_comment(
        self, issue_number: int, marker: str, body: str, trusted_logins: Sequence[str] = ()
    ) -> Dict[str, Any]:
        self._record("upsert_marker_comment", issue_number, marker)
        trusted = {login.lower() for login in trusted_logins}
        for comment in reversed(self.issue_comments):
            if marker not in (comment.get("body") or ""):
                continue
            login = ((comment.get("user") or {}).get("login") or "").lower()
            if trusted and login and login not in trusted:
                continue
            return self.update_issue_comment(int(comment["id"]), body)
        return self.create_issue_comment(issue_number, body)

    # -- assertions --------------------------------------------------------
    def call_names(self) -> List[str]:
        return [name for name, *_ in self.calls]

    def tracker_comments(self) -> List[Dict[str, Any]]:
        return [
            comment for comment in self.issue_comments if TRACKER_MARKER in (comment.get("body") or "")
        ]

    def tracker_state(self) -> Dict[str, Any]:
        from continuum.review.tracker import parse_tracker_state

        comments = self.tracker_comments()
        if not comments:
            return {}
        return parse_tracker_state(comments[-1]["body"]) or {}


def json_block(body: str) -> Any:
    start = body.index("```json")
    end = body.index("```", start + 7)
    return json.loads(body[start + len("```json") : end])
