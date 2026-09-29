"""Tests for the GitHub client: pagination, thread state, and error surfacing."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request

from continuum.review import coderabbit
from continuum.review import github as github_module
from continuum.review.github import GitHubClient, GitHubError


def response(payload, link=""):
    return payload, link


class LinkHeaderTests(unittest.TestCase):
    def test_next_link_is_extracted(self):
        header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
        self.assertEqual(github_module._next_link(header), "https://api.github.com/x?page=2")

    def test_missing_next_link(self):
        self.assertIsNone(github_module._next_link('</x>; rel="prev"'))
        self.assertIsNone(github_module._next_link(""))


class ClientConstructionTests(unittest.TestCase):
    def test_missing_token_is_rejected(self):
        with self.assertRaises(GitHubError):
            GitHubClient("", "owner/repo")

    def test_malformed_repository_is_rejected(self):
        with self.assertRaises(GitHubError):
            GitHubClient("token", "not-a-repository")

    def test_owner_and_name_are_parsed(self):
        client = GitHubClient("token", "kodmial/continuum")
        self.assertEqual((client.owner, client.name), ("kodmial", "continuum"))


class PaginationTests(unittest.TestCase):
    def test_paginate_follows_link_headers(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            if len(seen) == 1:
                return [{"id": 1}], '<https://api.github.com/page2>; rel="next"'
            return [{"id": 2}], ""

        client = GitHubClient("token", "o/r", opener=opener)
        items = client.list_review_comments(7)
        self.assertEqual([item["id"] for item in items], [1, 2])
        self.assertEqual(len(seen), 2)

    def test_paginate_preserves_status_on_failure(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 502, "Bad gateway", {}, None)

        client = GitHubClient("token", "o/r", opener=opener)
        with self.assertRaises(GitHubError) as caught:
            client.list_issue_comments(1)
        self.assertEqual(caught.exception.status, 502)


class ThreadStateTests(unittest.TestCase):
    def _client(self, payload):
        def opener(request, timeout):
            return payload, ""

        return GitHubClient("token", "o/r", opener=opener)

    def test_unresolved_and_current_threads_are_reported(self):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {"nodes": [{"databaseId": 11}, {"databaseId": 12}]},
                                },
                                {
                                    "isResolved": True,
                                    "isOutdated": False,
                                    "comments": {"nodes": [{"databaseId": 13}]},
                                },
                                {
                                    "isResolved": False,
                                    "isOutdated": True,
                                    "comments": {"nodes": [{"databaseId": 14}]},
                                },
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        self.assertEqual(self._client(payload).unresolved_thread_comment_ids(7), {11, 12})

    def test_pagination_is_followed(self):
        pages = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"databaseId": 1}]}}
                                ],
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"databaseId": 2}]}}
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            },
        ]
        calls = {"n": 0}

        def opener(request, timeout):
            payload = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return payload, ""

        self.assertEqual(GitHubClient("token", "o/r", opener=opener).unresolved_thread_comment_ids(7), {1, 2})

    def test_graphql_errors_return_unknown(self):
        """An error payload must yield None so callers keep every finding."""

        self.assertIsNone(self._client({"errors": [{"message": "boom"}]}).unresolved_thread_comment_ids(7))

    def test_missing_pull_request_returns_unknown(self):
        self.assertIsNone(self._client({"data": {"repository": None}}).unresolved_thread_comment_ids(7))

    def test_http_failure_returns_unknown(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

        client = GitHubClient("token", "o/r", opener=opener)
        self.assertIsNone(client.unresolved_thread_comment_ids(7))

    def test_empty_thread_list_is_known(self):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
                    }
                }
            }
        }
        self.assertEqual(self._client(payload).unresolved_thread_comment_ids(7), set())


class ReviewThreadBodyTests(unittest.TestCase):
    """`review_threads` must carry comment bodies in the real GraphQL shape.

    The whole point is comparing GitHub's flag against what the provider wrote in
    the thread, and that text only exists in the body. A camelCase-only response
    is what the API really sends, so a snake_case-only parser would silently read
    every thread as silent and every fixed finding as still open.
    """

    def _client(self, payload):
        def opener(request, timeout):
            return payload, ""

        return GitHubClient("token", "o/r", opener=opener)

    def _payload(self, node):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [node],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    def test_graphql_thread_comments_keep_their_author_and_timestamp(self):
        payload = self._payload(
            {
                "id": "PRRT_kwDOAbCdE12AbCdE",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 41,
                            "body": "This handle leaks on timeout",
                            "createdAt": "2026-09-28T09:00:00Z",
                            "author": {"login": "coderabbitai[bot]"},
                        },
                        {
                            "databaseId": 42,
                            "body": "✅ Addressed in the follow-up commit.",
                            "createdAt": "2026-09-28T10:00:00Z",
                            "author": {"login": "coderabbitai[bot]"},
                        },
                    ]
                },
            }
        )
        threads = self._client(payload).review_threads(7)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["id"], "PRRT_kwDOAbCdE12AbCdE")
        self.assertEqual(threads[0]["comments"][1]["author"], {"login": "coderabbitai[bot]"})

    def test_camel_case_authoring_actually_resolves_a_thread(self):
        payload = self._payload(
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {"databaseId": 41, "body": "leak", "createdAt": "2026-09-28T09:00:00Z", "author": {"login": "coderabbitai[bot]"}},
                        {"databaseId": 42, "body": "✅ Review thread resolved.", "createdAt": "2026-09-28T10:00:00Z", "author": {"login": "coderabbitai[bot]"}},
                    ]
                },
            }
        )
        threads = self._client(payload).review_threads(7)
        self.assertEqual(
            coderabbit.thread_resolution(threads[0]["comments"], "coderabbitai[bot]"),
            "resolved",
        )
        self.assertEqual(
            [item["id"] for item in coderabbit.pending_thread_normalizations(threads, "coderabbitai[bot]")],
            ["T1"],
        )

    def test_a_human_reply_does_not_claim_a_provider_verdict(self):
        payload = self._payload(
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {"databaseId": 41, "body": "leak", "createdAt": "2026-09-28T09:00:00Z", "author": {"login": "coderabbitai[bot]"}},
                        {"databaseId": 42, "body": "✅ resolved", "createdAt": "2026-09-28T10:00:00Z", "author": {"login": "a-human"}},
                    ]
                },
            }
        )
        threads = self._client(payload).review_threads(7)
        self.assertEqual(coderabbit.thread_resolution(threads[0]["comments"], "coderabbitai[bot]"), "")

    def test_unknown_thread_state_is_none(self):
        self.assertIsNone(self._client({"errors": [{"message": "boom"}]}).review_threads(7))


class ResolveReviewThreadTests(unittest.TestCase):
    def _client(self, payload):
        def opener(request, timeout):
            return payload, ""

        return GitHubClient("token", "o/r", opener=opener)

    def test_a_confirmed_resolution_is_accepted(self):
        payload = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        self._client(payload).resolve_review_thread("T1")

    def test_a_silent_failure_is_an_error(self):
        """A 200 that does not confirm resolution must not read as success."""

        payload = {"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}}
        with self.assertRaises(GitHubError):
            self._client(payload).resolve_review_thread("T1")

    def test_an_error_payload_raises(self):
        with self.assertRaises(GitHubError):
            self._client({"errors": [{"message": "not allowed"}]}).resolve_review_thread("T1")

    def test_a_missing_thread_id_is_rejected_before_the_call(self):
        client = self._client({"data": {}})
        with self.assertRaises(GitHubError):
            client.resolve_review_thread("")


class FileContentTests(unittest.TestCase):
    def test_base64_content_is_decoded(self):
        import base64

        encoded = base64.b64encode(b"print('hi')\n").decode()
        client = GitHubClient(
            "token",
            "o/r",
            opener=lambda request, timeout: ({"content": encoded, "encoding": "base64"}, ""),
        )
        self.assertEqual(client.file_at_ref("src/a.py", "deadbeef"), "print('hi')\n")

    def test_unreadable_file_returns_none(self):
        client = GitHubClient(
            "token",
            "o/r",
            opener=lambda request, timeout: (_ for _ in ()).throw(urllib.error.HTTPError(request.full_url, 404, "nope", {}, None)),
        )
        self.assertIsNone(client.file_at_ref("src/a.py", "deadbeef"))

    def test_non_base64_payload_returns_none(self):
        client = GitHubClient("token", "o/r", opener=lambda request, timeout: ({"encoding": "none"}, ""))
        self.assertIsNone(client.file_at_ref("src/a.py", "deadbeef"))


class ReviewWriteTests(unittest.TestCase):
    def test_create_review_sends_the_expected_body(self):
        captured = {}

        def opener(request, timeout):
            captured["method"] = request.get_method()
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return {"id": 1}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        client.create_review(7, "abc", "body", "REQUEST_CHANGES")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/repos/o/r/pulls/7/reviews"))
        self.assertEqual(
            captured["body"], {"commit_id": "abc", "body": "body", "event": "REQUEST_CHANGES"}
        )

    def test_create_status_uses_the_normalized_contract(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return {"id": 1}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        client.create_status("abc", "failure", "verdict=COMMENT state=blocked provider=pr-agent head=abc", "continuum/review")
        self.assertTrue(captured["url"].endswith("/repos/o/r/statuses/abc"))
        self.assertEqual(captured["body"]["context"], "continuum/review")
        self.assertEqual(captured["body"]["state"], "failure")

    def test_upsert_creates_then_updates(self):
        calls = []
        comments = [{"id": 5, "body": "<!--marker--> first", "user": {"login": "github-actions[bot]"}}]

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, request.data and json.loads(request.data.decode())))
            if request.get_method() == "GET":
                return comments, ""
            if "comments?per_page" in request.full_url:
                return comments, ""
            if request.get_method() == "PATCH":
                comments[0] = {"id": 5, "body": json.loads(request.data.decode())["body"], "user": {"login": "github-actions[bot]"}}
                return comments[0], ""
            return {"id": int(comments and comments[0]["id"] or 5)} , ""

        client = GitHubClient("token", "o/r", opener=opener)
        client.upsert_marker_comment(7, "<!--marker-->", "second", ("github-actions[bot]",))
        self.assertIn("PATCH", [method for method, _url, _body in calls])
        self.assertEqual(comments[0]["body"], "second")

    def test_upsert_creates_when_no_trusted_comment_exists(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url))
            if request.get_method() == "GET":
                return [{"id": 11, "body": "nope", "user": {"login": "someone"}}], ""
            return {"id": 12}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        client.upsert_marker_comment(7, "<!--marker-->", "body", ("github-actions[bot]",))
        self.assertEqual(calls[-1][0], "POST")

    def test_upsert_ignores_untrusted_tracker_comment(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url))
            if request.get_method() == "GET":
                return [{"id": 9, "body": "<!--marker--> forged", "user": {"login": "drive-by"}}], ""
            return {"id": 9}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        client.upsert_marker_comment(7, "<!--marker-->", "body", ("github-actions[bot]",))
        self.assertEqual(calls[-1][0], "POST")


if __name__ == "__main__":
    unittest.main()
