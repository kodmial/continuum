"""The GitHub REST port: classification, tag identity, digests, receipts."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import unittest

from continuum.release.github import PortFailure, ReleaseAsset
from continuum.release.github_api import (
    GitHubApi,
    GitHubReleaseRepository,
    HttpResponse,
    ReleaseAssetReceiptStore,
)

API = "https://api.example"
UPLOAD = "https://upload.example"
REPOSITORY = "example/widgets"
SHA = "a" * 40
OTHER_SHA = "b" * 40


class FakeGitHub:
    """A tiny GitHub that speaks just enough of the release API."""

    def __init__(self):
        self.refs = {}
        self.tags = {}
        self.releases = {}
        self.asset_bytes = {}
        self.next_id = 100
        self.calls = []
        self.overrides = []

    # -- helpers ------------------------------------------------------------
    def fail_once(self, method, fragment, status, body=b"{}", headers=None):
        self.overrides.append((method, fragment, HttpResponse(status, headers or {}, body)))

    def _override(self, method, url):
        for index, (wanted_method, fragment, response) in enumerate(self.overrides):
            if wanted_method == method and fragment in url:
                self.overrides.pop(index)
                return response
        return None

    def _payload(self, record):
        return {
            "id": record["id"],
            "tag_name": record["tag_name"],
            "target_commitish": record["target_commitish"],
            "draft": record["draft"],
            "name": record["name"],
            "html_url": record.get("html_url", ""),
        }

    def _asset_payload(self, asset):
        payload = {
            "id": asset["id"],
            "name": asset["name"],
            "size": asset["size"],
        }
        if asset.get("digest"):
            payload["digest"] = asset["digest"]
        return payload

    def _find(self, tag):
        for record in self.releases.values():
            if record["tag_name"] == tag:
                return record
        return None

    # -- the transport ------------------------------------------------------
    def request(self, method, url, *, body=None, headers=None):
        self.calls.append((method, url))
        forced = self._override(method, url)
        if forced is not None:
            return forced
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if method == "GET" and "/git/ref/tags/" in path:
            tag = path.rsplit("/", 1)[-1]
            target = self.refs.get(tag)
            if target is None:
                return HttpResponse(404, {}, b"{}")
            return HttpResponse(200, {}, json.dumps({"object": target}).encode())

        if method == "GET" and "/git/tags/" in path:
            sha = path.rsplit("/", 1)[-1]
            return HttpResponse(200, {}, json.dumps({"object": self.tags.get(sha)}).encode())

        if method == "GET" and "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[-1]
            record = self._find(tag)
            # GitHub's by-tag endpoint does not return drafts; a draft is only
            # visible through the release listing. The qualification run found
            # this the expensive way, so the fake enforces it too.
            if record is None or record["draft"]:
                return HttpResponse(404, {}, b"{}")
            return HttpResponse(200, {}, json.dumps(self._payload(record)).encode())

        if method == "GET" and path.endswith("/releases") and "/releases/" not in path:
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            records = sorted(self.releases.values(), key=lambda item: item["id"])
            start = (page - 1) * per_page
            window = records[start : start + per_page]
            return HttpResponse(
                200, {}, json.dumps([self._payload(record) for record in window]).encode()
            )

        if method == "POST" and path.endswith("/releases"):
            payload = json.loads(body.decode())
            record = {
                "id": self.next_id,
                "tag_name": payload["tag_name"],
                "target_commitish": payload["target_commitish"],
                "draft": payload.get("draft", True),
                "name": payload.get("name", ""),
                "assets": {},
                "html_url": f"https://example.invalid/{payload['tag_name']}",
            }
            self.next_id += 1
            self.releases[record["id"]] = record
            self.refs.setdefault(
                payload["tag_name"],
                {"sha": payload["target_commitish"], "type": "commit"},
            )
            return HttpResponse(201, {}, json.dumps(self._payload(record)).encode())

        if method == "GET" and path.endswith("/assets"):
            release_id = int(path.split("/releases/")[1].split("/")[0])
            record = self.releases[release_id]
            return HttpResponse(
                200,
                {},
                json.dumps([self._asset_payload(a) for a in record["assets"].values()]).encode(),
            )

        if method == "POST" and "/releases/" in path and path.endswith("/assets"):
            release_id = int(path.split("/releases/")[1].split("/")[0])
            name = query["name"][0]
            digest = hashlib.sha256(body).hexdigest()
            asset = {
                "id": self.next_id,
                "name": name,
                "size": len(body),
                "digest": f"sha256:{digest}",
                "body": body,
            }
            self.next_id += 1
            self.releases[release_id]["assets"][name] = asset
            self.asset_bytes[asset["id"]] = body
            return HttpResponse(201, {}, json.dumps(self._asset_payload(asset)).encode())

        if method == "GET" and "/releases/assets/" in path:
            asset_id = int(path.rsplit("/", 1)[-1])
            return HttpResponse(200, {}, self.asset_bytes[asset_id])

        if method == "PATCH" and "/releases/" in path:
            release_id = int(path.rsplit("/", 1)[-1])
            record = self.releases[release_id]
            record["draft"] = json.loads(body.decode()).get("draft", record["draft"])
            return HttpResponse(200, {}, json.dumps(self._payload(record)).encode())

        if method == "DELETE":
            return HttpResponse(204, {}, b"")

        return HttpResponse(404, {}, b"{}")


def client(transport=None):
    return GitHubApi(
        repository=REPOSITORY,
        token="test-token",
        transport=transport or FakeGitHub(),
        api_root=API,
        upload_root=UPLOAD,
    )


class ClassificationTests(unittest.TestCase):
    def test_a_rate_limit_is_retryable(self):
        github = FakeGitHub()
        github.fail_once("GET", "/git/ref/tags/", 429, b'{"message":"slow down"}')
        with self.assertRaises(PortFailure) as caught:
            client(github).tag_sha("v1.0.0")
        self.assertEqual(caught.exception.code, "rate-limited")
        self.assertTrue(caught.exception.retryable)

    def test_a_403_with_no_quota_left_is_a_rate_limit(self):
        github = FakeGitHub()
        github.fail_once(
            "GET",
            "/git/ref/tags/",
            403,
            b"{}",
            {"x-ratelimit-remaining": "0"},
        )
        with self.assertRaises(PortFailure) as caught:
            client(github).tag_sha("v1.0.0")
        self.assertEqual(caught.exception.code, "rate-limited")
        self.assertTrue(caught.exception.retryable)

    def test_a_plain_403_is_a_permission_failure(self):
        github = FakeGitHub()
        github.fail_once("GET", "/git/ref/tags/", 403, b'{"message":"forbidden"}')
        with self.assertRaises(PortFailure) as caught:
            client(github).tag_sha("v1.0.0")
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertFalse(caught.exception.retryable)

    def test_a_5xx_is_retryable(self):
        github = FakeGitHub()
        github.fail_once("GET", "/git/ref/tags/", 502, b"{}")
        with self.assertRaises(PortFailure) as caught:
            client(github).tag_sha("v1.0.0")
        self.assertEqual(caught.exception.code, "server-error")
        self.assertTrue(caught.exception.retryable)

    def test_a_422_is_a_refusal_not_a_retry(self):
        github = FakeGitHub()
        github.fail_once("POST", "/releases", 422, b'{"message":"validation failed"}')
        with self.assertRaises(PortFailure) as caught:
            client(github).create_release(
                tag="v1.0.0", target_sha=SHA, name="v1.0.0", notes="notes"
            )
        self.assertEqual(caught.exception.code, "http-422")
        self.assertFalse(caught.exception.retryable)

    def test_a_missing_tag_is_none_and_a_missing_release_is_none(self):
        github = client()
        self.assertIsNone(github.tag_sha("v1.0.0"))
        self.assertIsNone(github.find_release("v1.0.0"))


class TagIdentityTests(unittest.TestCase):
    def test_an_annotated_tag_is_dereferenced_to_its_commit(self):
        fake = FakeGitHub()
        fake.refs["v1.0.0"] = {"sha": "tag-object", "type": "tag"}
        fake.tags["tag-object"] = {"sha": SHA, "type": "commit"}
        self.assertEqual(client(fake).tag_sha("v1.0.0"), SHA)

    def test_a_tag_that_points_at_a_blob_is_refused(self):
        fake = FakeGitHub()
        fake.refs["v1.0.0"] = {"sha": "blob-object", "type": "blob"}
        with self.assertRaises(PortFailure) as caught:
            client(fake).tag_sha("v1.0.0")
        self.assertEqual(caught.exception.code, "tag-not-a-commit")

    def test_creating_a_draft_on_a_moved_tag_is_refused(self):
        fake = FakeGitHub()
        fake.refs["v1.0.0"] = {"sha": OTHER_SHA, "type": "commit"}
        repository = GitHubReleaseRepository(client(fake))
        with self.assertRaises(PortFailure) as caught:
            repository.create_draft(tag="v1.0.0", name="v1.0.0", target_sha=SHA, notes="n")
        self.assertEqual(caught.exception.code, "tag-source-conflict")
        self.assertFalse(caught.exception.retryable)

    def test_a_draft_is_found_by_tag_even_though_the_tag_endpoint_hides_it(self):
        fake = FakeGitHub()
        repository = GitHubReleaseRepository(client(fake))
        created = repository.create_draft(
            tag="v1.0.0", name="v1.0.0", target_sha=SHA, notes="notes"
        )
        # The fake answers 404 from /releases/tags for a draft, like GitHub.
        self.assertEqual(client(fake).find_release("v1.0.0")["id"], int(created.id))
        found = repository.find_by_tag("v1.0.0")
        self.assertIsNotNone(found)
        self.assertTrue(found.draft)
        self.assertEqual(found.target_sha, SHA)


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.repository = GitHubReleaseRepository(client(self.fake))
        self.record = self.repository.create_draft(
            tag="v1.0.0", name="v1.0.0", target_sha=SHA, notes="notes"
        )

    def test_an_upload_is_measured_and_matched(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "widget.tar.gz")
            with open(path, "wb") as handle:
                handle.write(b"payload")
            size = os.path.getsize(path)
            digest = hashlib.sha256(b"payload").hexdigest()
            self.repository.upload(
                self.record,
                ReleaseAsset(name="widget.tar.gz", size=size, digest=digest),
                path,
            )
        assets = self.repository.assets(self.record)
        self.assertEqual(assets[0].name, "widget.tar.gz")
        self.assertEqual(assets[0].digest, digest)

    def test_a_size_mismatch_is_refused(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "widget.tar.gz")
            with open(path, "wb") as handle:
                handle.write(b"payload")
            with self.assertRaises(PortFailure) as caught:
                self.repository.upload(
                    self.record,
                    ReleaseAsset(name="widget.tar.gz", size=999, digest="0" * 64),
                    path,
                )
        self.assertEqual(caught.exception.code, "asset-size-mismatch")
        self.assertTrue(caught.exception.retryable)

    def test_a_digest_mismatch_is_refused(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "widget.tar.gz")
            with open(path, "wb") as handle:
                handle.write(b"payload")
            with self.assertRaises(PortFailure) as caught:
                self.repository.upload(
                    self.record,
                    ReleaseAsset(
                        name="widget.tar.gz",
                        size=os.path.getsize(path),
                        digest="0" * 64,
                    ),
                    path,
                )
        self.assertEqual(caught.exception.code, "asset-digest-mismatch")

    def test_an_asset_without_a_reported_digest_is_hashed_from_its_bytes(self):
        self.fake.releases[int(self.record.id)]["assets"]["widget.tar.gz"] = {
            "id": 555,
            "name": "widget.tar.gz",
            "size": 7,
        }
        self.fake.asset_bytes[555] = b"payload"
        assets = self.repository.assets(self.record)
        self.assertEqual(assets[0].digest, hashlib.sha256(b"payload").hexdigest())
        self.assertEqual(assets[0].size, 7)

    def test_publishing_returns_a_non_draft_and_refuses_a_still_draft(self):
        promoted = self.repository.publish(self.record)
        self.assertFalse(promoted.draft)
        self.assertTrue(promoted.url)

        self.fake.fail_once(
            "PATCH",
            "/releases/",
            200,
            json.dumps(
                {
                    "id": int(self.record.id),
                    "tag_name": "v1.0.0",
                    "target_commitish": SHA,
                    "draft": True,
                    "name": "v1.0.0",
                }
            ).encode(),
        )
        with self.assertRaises(PortFailure) as caught:
            self.repository.publish(self.record)
        self.assertEqual(caught.exception.code, "release-still-draft")
        self.assertTrue(caught.exception.retryable)


class ReceiptAssetTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.repository = GitHubReleaseRepository(client(self.fake))
        self.record = self.repository.create_draft(
            tag="v1.0.0", name="v1.0.0", target_sha=SHA, notes="notes"
        )
        self.store = ReleaseAssetReceiptStore(self.repository)

    def test_a_saved_receipt_is_a_new_generation(self):
        first = self.store.save("v1.0.0", '{"generation": 1}')
        second = self.store.save("v1.0.0", '{"generation": 2}')
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.load("v1.0.0"), '{"generation": 2}')
        names = sorted(self.fake.releases[int(self.record.id)]["assets"])
        self.assertEqual(len(names), 2, names)

    def test_a_receipt_is_none_when_no_release_exists(self):
        self.assertIsNone(self.store.load("v9.9.9"))

    def test_saving_without_a_release_fails_closed(self):
        with self.assertRaises(PortFailure) as caught:
            self.store.save("v9.9.9", "{}")
        self.assertEqual(caught.exception.code, "receipt-target-absent")

    def test_a_receipt_without_assets_is_none(self):
        self.assertIsNone(self.store.load("v1.0.0"))


if __name__ == "__main__":
    unittest.main()
