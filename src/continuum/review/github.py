"""Minimal GitHub API client used by the review gate.

Only what the gate needs, with no third-party dependencies so it runs on stock
runners. The client is a plain object: the gate depends on these methods, not
on the network, which keeps the gate testable with a stub.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

DEFAULT_API_BASE = "https://api.github.com"
USER_AGENT = "continuum-review-gate"

REVIEW_THREADS_QUERY = (
    "query($owner: String!, $name: String!, $pr: Int!, $after: String) {"
    " repository(owner: $owner, name: $name) {"
    "  pullRequest(number: $pr) {"
    "   reviewThreads(first: 100, after: $after) {"
    "    nodes { id isResolved isOutdated path"
    "     comments(first: 100) { nodes { databaseId body createdAt author { login } } }"
    "    }"
    "    pageInfo { hasNextPage endCursor }"
    "   }"
    "  }"
    " }"
    "}"
)

RESOLVE_REVIEW_THREAD_MUTATION = (
    "mutation($threadId: ID!) {"
    " resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }"
    "}"
)


class GitHubError(RuntimeError):
    """GitHub API failure with the HTTP status preserved."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        opener: Optional[Callable[[urllib.request.Request, int], Any]] = None,
        timeout: int = 60,
    ) -> None:
        if not token:
            raise GitHubError("Missing GitHub token")
        if "/" not in (repository or ""):
            raise GitHubError(f"Invalid repository {repository!r}; expected owner/repo")
        self.token = token
        self.repository = repository
        self.owner, self.name = repository.split("/", 1)
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._opener = opener or _default_opener

    # -- transport ---------------------------------------------------------
    def _headers(self, preview: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"token {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        headers["Accept"] = preview or "application/vnd.github+json"
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        preview: Optional[str] = None,
    ) -> Any:
        data = None
        headers = self._headers(preview)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.api_base + path, data=data, headers=headers, method=method
        )
        try:
            result = self._opener(request, self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001 - diagnostics must not mask the failure
                detail = ""
            raise GitHubError(
                f"GitHub API {method} {path} failed with status {exc.code}: {detail}",
                status=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"GitHub API {method} {path} unreachable: {exc.reason}") from None
        if isinstance(result, tuple):
            return result[0]
        return result

    def paginate(self, path: str) -> List[Any]:
        items: List[Any] = []
        url = self.api_base + path
        while url:
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                page, link = self._opener(request, self.timeout)
            except urllib.error.HTTPError as exc:
                raise GitHubError(
                    f"GitHub API GET {url} failed with status {exc.code}", status=exc.code
                ) from None
            if isinstance(page, list):
                items.extend(page)
            url = _next_link(link)
        return items

    def paginate_key(self, path: str, key: str) -> List[Any]:
        """Page through an endpoint whose payload is an object, not a list.

        The Actions endpoints answer `{"total_count": n, "workflow_runs": [...]}`
        rather than a bare array. `paginate` on such a page collects nothing and
        returns an empty list without complaining, which for a verification gate
        is the worst possible failure: it reports "no verification run exists"
        for a repository that has one, and the publication is blocked for a
        reason that is not true. A key that is absent or of the wrong type is an
        error here, not an empty result.
        """

        items: List[Any] = []
        url = self.api_base + path
        while url:
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                page, link = self._opener(request, self.timeout)
            except urllib.error.HTTPError as exc:
                raise GitHubError(
                    f"GitHub API GET {url} failed with status {exc.code}", status=exc.code
                ) from None
            if not isinstance(page, dict) or not isinstance(page.get(key), list):
                raise GitHubError(
                    f"GitHub API GET {url} returned no {key!r} array; the response shape "
                    "changed and the caller would otherwise read it as an empty result"
                )
            items.extend(page[key])
            url = _next_link(link)
        return items

    def graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.request("POST", "/graphql", {"query": query, "variables": variables})
        if not isinstance(payload, dict):
            raise GitHubError("GraphQL response was not an object")
        return payload

    # -- repository reads --------------------------------------------------
    def get_pull(self, number: int) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{self.owner}/{self.name}/pulls/{number}")

    def list_pulls(self, *, state: str = "open", base: str = "") -> List[Dict[str, Any]]:
        query = urllib.parse.urlencode({"state": state, "per_page": 100, **({"base": base} if base else {})})
        return self.paginate(f"/repos/{self.owner}/{self.name}/pulls?{query}")

    def get_issue(self, number: int) -> Dict[str, Any]:
        return self.request("GET", f"/repos/{self.owner}/{self.name}/issues/{number}")

    def list_check_runs(self, ref: str) -> List[Dict[str, Any]]:
        quoted = urllib.parse.quote(str(ref), safe="")
        payload = self.request(
            "GET",
            f"/repos/{self.owner}/{self.name}/commits/{quoted}/check-runs?per_page=100",
        )
        return list(payload.get("check_runs") or [])

    def list_pull_files(self, number: int) -> List[Dict[str, Any]]:
        return self.paginate(f"/repos/{self.owner}/{self.name}/pulls/{number}/files?per_page=100")

    def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
        return self.paginate(f"/repos/{self.owner}/{self.name}/issues/{number}/comments?per_page=100")

    def list_review_comments(self, number: int) -> List[Dict[str, Any]]:
        return self.paginate(f"/repos/{self.owner}/{self.name}/pulls/{number}/comments?per_page=100")

    def list_reviews(self, number: int) -> List[Dict[str, Any]]:
        return self.paginate(f"/repos/{self.owner}/{self.name}/pulls/{number}/reviews?per_page=100")

    def combined_status_for_ref(self, ref: str) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(ref), safe="")
        return self.request(
            "GET", f"/repos/{self.owner}/{self.name}/commits/{quoted}/status"
        )

    def file_at_ref(self, path: str, ref: str) -> Optional[str]:
        quoted = urllib.parse.quote(path)
        query = urllib.parse.urlencode({"ref": ref})
        try:
            data = self.request("GET", f"/repos/{self.owner}/{self.name}/contents/{quoted}?{query}")
        except GitHubError:
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        import base64

        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - unreadable file is not a gate failure
            return None

    # -- review thread state ----------------------------------------------
    def review_threads(self, pr_number: int) -> Optional[List[Dict[str, Any]]]:
        """Structured review threads, or None when the state is unknown.

        Each entry carries the node id (needed to resolve it), GitHub's own
        `isResolved`/`isOutdated` flags, and every comment with its author and
        body. The body is not decoration: a review bot answers a re-check in
        prose, and that answer can disagree with the flag GitHub stores.

        `None` means the state could not be determined. Callers must then keep
        every inline thread: an unknown thread state may never be treated as
        "no findings".
        """

        threads: List[Dict[str, Any]] = []
        after: Optional[str] = None
        try:
            while True:
                response = self.graphql(
                    REVIEW_THREADS_QUERY,
                    {
                        "owner": self.owner,
                        "name": self.name,
                        "pr": int(pr_number),
                        "after": after,
                    },
                )
                if response.get("errors"):
                    return None
                pull_request = (
                    ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
                )
                if not pull_request:
                    return None
                connection = pull_request.get("reviewThreads")
                if connection is None:
                    # A null connection means the field could not be resolved
                    # (scope, permissions), not "this pull request has no
                    # threads". Reporting that as a clean thread list would drop
                    # every inline finding.
                    return None
                for node in connection.get("nodes") or []:
                    threads.append(_thread_payload(node))
                page = connection.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
                if not after:
                    break
        except GitHubError:
            return None
        return threads

    def unresolved_thread_comment_ids(self, pr_number: int) -> Optional[Set[int]]:
        """Comment ids in unresolved, non-outdated threads.

        Returns None when the state cannot be determined. Callers must keep
        every inline thread in that case: an unknown thread state may never be
        treated as "no findings".
        """

        threads = self.review_threads(int(pr_number))
        if threads is None:
            return None
        unresolved: Set[int] = set()
        for thread in threads:
            if thread.get("is_resolved") or thread.get("is_outdated"):
                continue
            for comment in thread.get("comments") or []:
                database_id = comment.get("database_id")
                if database_id is not None:
                    unresolved.add(int(database_id))
        return unresolved

    def resolve_review_thread(self, thread_id: str) -> Dict[str, Any]:
        """Mark one review thread resolved with the workflow token.

        This is how a thread the bot already settled in prose is brought back in
        line with GitHub's own state. It is a write, so a caller must treat a
        failure as "still unresolved" rather than assuming the thread is clean.
        """

        thread = str(thread_id or "").strip()
        if not thread:
            raise GitHubError("Cannot resolve a review thread without its node id")
        response = self.graphql(RESOLVE_REVIEW_THREAD_MUTATION, {"threadId": thread})
        if response.get("errors"):
            raise GitHubError(
                f"GitHub API resolveReviewThread failed for {thread}: "
                f"{json.dumps(response.get('errors'))[:500]}"
            )
        return response

    # -- review gate writes -----------------------------------------------
    def create_review(self, pr_number: int, head_sha: str, body: str, event: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.owner}/{self.name}/pulls/{pr_number}/reviews",
            {"commit_id": head_sha, "body": body, "event": event},
        )

    def create_status(
        self,
        head_sha: str,
        state: str,
        description: str,
        context: str,
        target_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "state": state,
            "description": description,
            "context": context,
        }
        if target_url:
            body["target_url"] = target_url
        return self.request(
            "POST", f"/repos/{self.owner}/{self.name}/statuses/{head_sha}", body
        )

    def create_issue_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.owner}/{self.name}/issues/{issue_number}/comments",
            {"body": body},
        )

    def delete_issue_comment(self, comment_id: int) -> Dict[str, Any]:
        return self.request(
            "DELETE", f"/repos/{self.owner}/{self.name}/issues/comments/{int(comment_id)}"
        )

    def add_labels(self, issue_number: int, labels: Sequence[str]) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.owner}/{self.name}/issues/{issue_number}/labels",
            {"labels": list(labels)},
        )

    def remove_label(self, issue_number: int, name: str) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(name), safe="")
        return self.request(
            "DELETE", f"/repos/{self.owner}/{self.name}/issues/{issue_number}/labels/{quoted}"
        )

    def dispatch_workflow(self, workflow: str, ref: str, inputs: Dict[str, str]) -> Dict[str, Any]:
        quoted = urllib.parse.quote(str(workflow), safe="")
        body: Dict[str, Any] = {"ref": ref}
        body.update({f"inputs[{key}]": str(value) for key, value in (inputs or {}).items()})
        return self.request(
            "POST",
            f"/repos/{self.owner}/{self.name}/actions/workflows/{quoted}/dispatches",
            body,
        )

    # -- release verification ------------------------------------------------
    #
    # Reading a workflow run is the same read the review side does, on a
    # different subject. These stay here rather than in a second client so there
    # is one place that knows how this repository is addressed and authenticated.

    def list_workflow_runs(
        self, workflow: str, *, head_sha: str = "", event: str = ""
    ) -> List[Dict[str, Any]]:
        """Runs of one workflow file, optionally narrowed to a commit and event.

        `head_sha` is a filter, not a search: asking for runs of a commit is
        what makes "verified exactly this commit" expressible. An empty filter
        returns the newest runs, which is only useful for a local diagnostic.
        """

        quoted = urllib.parse.quote(str(workflow), safe="")
        path = f"/repos/{self.owner}/{self.name}/actions/workflows/{quoted}/runs"
        query = []
        if head_sha:
            query.append(("head_sha", str(head_sha)))
        if event:
            query.append(("event", str(event)))
        return self.paginate_key(
            f"{path}?{urllib.parse.urlencode(query)}" if query else path, "workflow_runs"
        )

    def list_run_jobs(self, run_id: int) -> List[Dict[str, Any]]:
        return self.paginate_key(
            f"/repos/{self.owner}/{self.name}/actions/runs/{int(run_id)}/jobs?per_page=100",
            "jobs",
        )

    def list_run_artifacts(self, run_id: int) -> List[Dict[str, Any]]:
        return self.paginate_key(
            f"/repos/{self.owner}/{self.name}/actions/runs/{int(run_id)}/artifacts?per_page=100",
            "artifacts",
        )

    def update_issue_comment(self, comment_id: int, body: str) -> Dict[str, Any]:
        return self.request(
            "PATCH", f"/repos/{self.owner}/{self.name}/issues/comments/{comment_id}", {"body": body}
        )

    def upsert_marker_comment(
        self, issue_number: int, marker: str, body: str, trusted_logins: Sequence[str] = ()
    ) -> Dict[str, Any]:
        """Create or update the single comment that carries `marker`."""

        comments = self.list_issue_comments(issue_number)
        trusted = {login.lower() for login in trusted_logins}
        for comment in reversed(comments):
            body_text = comment.get("body") or ""
            if marker not in body_text:
                continue
            login = ((comment.get("user") or {}).get("login") or "").lower()
            if trusted and login and login not in trusted:
                continue
            return self.update_issue_comment(int(comment["id"]), body)
        return self.create_issue_comment(issue_number, body)


def _thread_payload(node: Dict[str, Any]) -> Dict[str, Any]:
    """One reviewThread node in the shape the adapter consumes.

    Every field is optional in practice: a partial response must degrade to
    "unknown", never to a confident empty thread.
    """

    comments: List[Dict[str, Any]] = []
    for comment in ((node.get("comments") or {}).get("nodes") or []):
        database_id = comment.get("databaseId")
        comments.append(
            {
                "database_id": int(database_id) if database_id is not None else None,
                "author": str(((comment.get("author") or {}).get("login")) or ""),
                "body": str(comment.get("body") or ""),
                "created_at": str(comment.get("createdAt") or ""),
            }
        )
    return {
        "id": str(node.get("id") or ""),
        "is_resolved": bool(node.get("isResolved")),
        "is_outdated": bool(node.get("isOutdated")),
        "path": str(node.get("path") or ""),
        "comments": comments,
    }


def _default_opener(request: urllib.request.Request, timeout: int) -> Any:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", "replace")
        parsed = json.loads(payload) if payload else {}
        return parsed, response.headers.get("Link", "")


_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def _next_link(link_header: str) -> Optional[str]:
    for part in (link_header or "").split(","):
        match = _NEXT_LINK_RE.search(part)
        if match:
            return match.group(1)
    return None
