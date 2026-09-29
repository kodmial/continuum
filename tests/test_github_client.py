"""Tests for the GitHub client: pagination, thread state, and error surfacing."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request

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

    def test_paginate_over_an_object_page_would_collect_nothing(self):
        # The reason `paginate_key` exists. A bare-array pager pointed at an
        # object payload returns an empty list without complaining, so a caller
        # cannot tell "nothing matched" from "wrong endpoint shape".
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return {"total_count": 1, "workflow_runs": [{"id": 501}]}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        self.assertEqual(client.paginate("/repos/o/r/actions/workflows/w.yml/runs"), [])


class ObjectPagePaginationTests(unittest.TestCase):
    """The Actions endpoints answer an object, so they need the keyed pager."""

    def test_workflow_runs_are_read_out_of_the_object_page(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            if len(seen) == 1:
                return (
                    {"total_count": 2, "workflow_runs": [{"id": 501}, {"id": 500}]},
                    '<https://api.github.com/page2>; rel="next"',
                )
            return {"total_count": 1, "workflow_runs": [{"id": 499}]}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        runs = client.list_workflow_runs("verification.yml", head_sha="abc", event="push")
        self.assertEqual([run["id"] for run in runs], [501, 500, 499])
        self.assertIn("head_sha=abc", seen[0])
        self.assertIn("event=push", seen[0])

    def test_jobs_and_artifacts_are_read_from_their_own_keys(self):
        def opener(request, timeout):
            if "/jobs" in request.full_url:
                return {"total_count": 1, "jobs": [{"name": "build"}]}, ""
            return {"total_count": 1, "artifacts": [{"name": "dist"}]}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        self.assertEqual(client.list_run_jobs(501), [{"name": "build"}])
        self.assertEqual(client.list_run_artifacts(501), [{"name": "dist"}])

    def test_a_changed_response_shape_is_an_error_not_an_empty_result(self):
        # The failure this guards: silently returning nothing would make the gate
        # report "no verification run exists" for a repository that has one, and
        # block every publication for a reason that is not true.
        def opener(request, timeout):
            return {"message": "Not Found"}, ""

        client = GitHubClient("token", "o/r", opener=opener)
        with self.assertRaises(GitHubError) as caught:
            client.list_workflow_runs("verification.yml")
        self.assertIn("workflow_runs", str(caught.exception))

    def test_a_bare_array_where_an_object_was_expected_is_an_error(self):
        def opener(request, timeout):
            return [{"id": 1}], ""

        client = GitHubClient("token", "o/r", opener=opener)
        with self.assertRaises(GitHubError):
            client.list_run_artifacts(501)


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

    def test_threads_carry_the_identity_and_prose_a_normalization_needs(self):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "PRRT_a",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "app.py",
                                    "comments": {
                                        "nodes": [
                                            {
                                                "databaseId": 11,
                                                "body": "Missing a null guard.",
                                                "createdAt": "2026-09-01T00:00:00Z",
                                                "author": {"login": "coderabbitai[bot]"},
                                            },
                                            {
                                                "databaseId": 12,
                                                "body": "RESOLVED",
                                                "createdAt": "2026-09-01T01:00:00Z",
                                                "author": {"login": "coderabbitai[bot]"},
                                            },
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        threads = self._client(payload).review_threads(7)
        self.assertEqual(
            threads,
            [
                {
                    "id": "PRRT_a",
                    "is_resolved": False,
                    "is_outdated": False,
                    "path": "app.py",
                    "comments": [
                        {
                            "database_id": 11,
                            "author": "coderabbitai[bot]",
                            "body": "Missing a null guard.",
                            "created_at": "2026-09-01T00:00:00Z",
                        },
                        {
                            "database_id": 12,
                            "author": "coderabbitai[bot]",
                            "body": "RESOLVED",
                            "created_at": "2026-09-01T01:00:00Z",
                        },
                    ],
                }
            ],
        )

    def test_a_null_comment_body_degrades_to_an_empty_reply(self):
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "PRRT_a",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "app.py",
                                    "comments": {"nodes": [{"databaseId": 11, "body": None, "author": None}]},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        thread = self._client(payload).review_threads(7)[0]
        self.assertEqual(
            thread["comments"], [{"database_id": 11, "author": "", "body": "", "created_at": ""}]
        )

    def test_a_null_connection_degrades_to_an_unknown_state(self):
        payload = {
            "data": {"repository": {"pullRequest": {"reviewThreads": None}}}
        }
        self.assertIsNone(self._client(payload).review_threads(7))


class ResolveReviewThreadTests(unittest.TestCase):
    def _client(self, payload, recorded=None):
        def opener(request, timeout):
            if recorded is not None:
                recorded.append(json.loads(request.data.decode("utf-8")))
            return payload, ""

        return GitHubClient("token", "o/r", opener=opener)

    def test_the_mutation_sends_the_thread_id(self):
        recorded = []
        client = self._client(
            {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_a", "isResolved": True}}}},
            recorded,
        )
        response = client.resolve_review_thread("PRRT_a")
        self.assertTrue(response["data"]["resolveReviewThread"]["thread"]["isResolved"])
        self.assertEqual(
            recorded[0]["variables"], {"threadId": "PRRT_a"}
        )
        self.assertIn("resolveReviewThread", recorded[0]["query"])

    def test_a_graphql_error_raises_so_the_caller_keeps_the_finding(self):
        client = self._client({"errors": [{"message": "not authorized"}]})
        with self.assertRaises(GitHubError) as caught:
            client.resolve_review_thread("PRRT_a")
        self.assertIn("not authorized", str(caught.exception))

    def test_an_empty_thread_id_is_rejected_before_any_write(self):
        recorded = []
        client = self._client({}, recorded)
        with self.assertRaises(GitHubError):
            client.resolve_review_thread("  ")
        self.assertEqual(recorded, [])


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
