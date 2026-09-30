"""The GitHub Releases API behind a port, and the guarantees it has to keep.

Every test here runs with no network: the transport is a double, so what is
exercised is the module's own behaviour — the classification of each failure,
the re-reads it insists on, and the refusals it makes. The properties that
matter are the ones a release depends on and cannot re-derive:

* a release is bound to one commit, and a tag that names another is refused;
* a draft is completed and then promoted exactly once, and promoting twice is
  a skip rather than a second publication;
* bytes are compared against the manifest *after* the destination stores them,
  because an upload that reports success and stores something else is the case
  a checksum manifest exists to catch;
* a published immutable release cannot be modified, and the answer to "the
  asset you want to replace is immutable" is a new version, not a retry;
* a destination that cannot report a digest is treated as a missing capability
  rather than as a missing value.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from continuum.release.contract import (
    ArtifactManifest,
    ContractError,
    FAILED,
    ManifestBuilder,
    PUBLISHED,
    PublishRequest,
    ReleaseNotes,
    SKIPPED,
    VERIFICATION_VERIFIED as VERIFIED,
    conform,
)
from continuum.release.github import GitHubReleasePublisher, ReleaseAsset
from continuum.release.github_api import (
    GitHubApiError,
    GitHubReleaseRepository,
    ReleaseRecordView,
    UrllibGitHubTransport,
    retryable_status,
)

REPOSITORY = "continuum/continuum"
SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG = "v1.0.0"
VERSION = "1.0.0"


class FakeGitHub:
    """A repository-shaped double: tags, releases, assets, and injectable faults.

    Deliberately a small model of the API rather than a canned response table,
    so a test can say "this repository has immutable releases" and have the
    double behave that way for every subsequent call.
    """

    def __init__(self) -> None:
        self.tags = {}
        self.releases = []
        self.assets = {}
        self.next_id = 1
        self.calls = []
        self.faults = {}
        self.omit_digests = False
        self.report_wrong_digest = None
        self.accept_promotion = True
        self.immutable_after_publish = False
        self.assets_per_page = 100
        self.ignore_page = False
        self.uploaded = []

    # -- helpers
    def add_release(self, *, tag=TAG, target_sha=SHA, draft=False, immutable=False, assets=()):
        record = {
            "id": self.next_id,
            "tag_name": tag,
            "target_commitish": target_sha,
            "draft": draft,
            "name": tag,
            "body": "notes",
            "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            "immutable": immutable,
            "prerelease": False,
        }
        self.next_id += 1
        self.releases.append(record)
        self.assets[record["id"]] = list(assets)
        if not draft:
            self.tags.setdefault(tag, target_sha)
        return record

    def add_asset(self, record, name, payload, *, digest=None):
        raw = payload if isinstance(payload, bytes) else payload.encode()
        value = digest or hashlib.sha256(raw).hexdigest()
        self.assets[record["id"]].append(
            {
                "name": name,
                "size": len(raw),
                "digest": value if not self.omit_digests else "",
            }
        )

    def release_by_tag(self, tag):
        for record in self.releases:
            if record["tag_name"] == tag:
                return record
        return None

    def _fault(self, method, path):
        for (wanted_method, fragment), fault in self.faults.items():
            if wanted_method == method and fragment in path:
                raise fault

    # -- transport
    def request(self, method, path, body=None):
        self.calls.append((method, path))
        self._fault(method, path)
        if method == "GET" and "/releases/tags/" in path:
            tag = path.split("/releases/tags/", 1)[1]
            record = self.release_by_tag(tag)
            if record is None:
                raise GitHubApiError("not found", code="absent", status=404)
            return dict(record)
        if method == "GET" and "/commits/" in path:
            tag = path.split("/commits/", 1)[1]
            if tag not in self.tags:
                raise GitHubApiError("not found", code="absent", status=404)
            return {"sha": self.tags[tag]}
        if method == "POST" and path.endswith("/releases"):
            if self.release_by_tag(body["tag_name"]) is not None:
                raise GitHubApiError("already_exists", code="conflict", status=422)
            record = self.add_release(
                tag=body["tag_name"],
                target_sha=body["target_commitish"],
                draft=True,
            )
            record["name"] = body["name"]
            record["body"] = body["body"]
            record["prerelease"] = body["prerelease"]
            return dict(record)
        if method == "GET" and "/assets" in path:
            record = self._release(path)
            page = 1
            for part in path.split("&"):
                if part.startswith("page="):
                    page = int(part.split("=", 1)[1])
            if self.ignore_page:
                page = 1
            start = (page - 1) * self.assets_per_page
            window = self.assets[record["id"]][start : start + self.assets_per_page]
            return [
                dict(asset, id=index, url=f"https://uploads/{index}")
                for index, asset in enumerate(window)
            ]
        if method == "PATCH" and "/releases/" in path:
            record = self._release(path)
            if self.accept_promotion:
                record["draft"] = False
                if self.immutable_after_publish:
                    record["immutable"] = True
                if not self.tags.get(record["tag_name"]):
                    self.tags[record["tag_name"]] = record["target_commitish"]
            return dict(record)
        raise AssertionError(f"unexpected request {method} {path}")

    def _release(self, path):
        wanted = int(path.split("/releases/", 1)[1].split("/", 1)[0])
        for record in self.releases:
            if record["id"] == wanted:
                return record
        raise AssertionError(f"no release {wanted}")

    def upload(self, url, name, path):
        self.calls.append(("POST", url))
        self._fault("POST", url)
        with open(path, "rb") as handle:
            payload = handle.read()
        record = self._by_upload_url(url)
        if any(asset["name"] == name for asset in self.assets[record["id"]]):
            raise GitHubApiError("already_exists", code="conflict", status=422)
        self.uploaded.append(name)
        digest = self.report_wrong_digest or hashlib.sha256(payload).hexdigest()
        asset = {
            "name": name,
            "size": len(payload),
            "digest": "" if self.omit_digests else digest,
        }
        self.assets[record["id"]].append(asset)
        return dict(asset, id=900 + len(self.assets[record["id"]]))

    def _by_upload_url(self, url):
        wanted = int(url.split("/releases/", 1)[1].split("/", 1)[0])
        for record in self.releases:
            if record["id"] == wanted:
                return record
        raise AssertionError(f"no release for upload url {url}")


class ReleaseApiTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.repository = GitHubReleaseRepository(self.api, REPOSITORY)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    # -- helpers
    def artifact(self, name="app-1.0.0.tar.gz", payload=b"release bytes"):
        path = os.path.join(self.workspace.name, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        builder = ManifestBuilder(target="desktop", adapter="fake", source_sha=SHA, version=VERSION)
        builder.record(name, path, "archive", verification=VERIFIED, verified_by="test")
        return builder.build(), path

    def request(self, manifest, *, tag=TAG, sha=SHA, version=VERSION, dry_run=False):
        return PublishRequest(
            destination="github-releases",
            tag=tag,
            version=version,
            source_sha=sha,
            manifests=(manifest,),
            key=f"publish/{tag}",
            dry_run=dry_run,
        )

    def publisher(self, **kwargs):
        return GitHubReleasePublisher(
            self.repository, notes="release notes", **kwargs
        )

    # -- conformance
    def test_conforms_to_the_release_repository_role(self):
        self.assertEqual(conform(self.repository, "release-repository"), ())

    def test_refuses_a_transport_it_cannot_upload_with(self):
        class Incomplete:
            def request(self, method, path, body=None):
                return None

        with self.assertRaises(GitHubApiError) as caught:
            GitHubReleaseRepository(Incomplete(), REPOSITORY)
        self.assertEqual(caught.exception.code, "transport-incomplete")

    def test_refuses_a_repository_without_an_owner(self):
        with self.assertRaises(GitHubApiError) as caught:
            GitHubReleaseRepository(self.api, "continuum")
        self.assertEqual(caught.exception.code, "repository-invalid")

    # -- lookup
    def test_finds_nothing_for_a_tag_that_does_not_exist(self):
        self.assertIsNone(self.repository.find_by_tag(TAG))

    def test_reports_no_release_rather_than_failing_when_absent(self):
        self.assertIsNone(self.repository.find_by_tag("v9.9.9"))

    def test_refuses_a_tag_that_cannot_be_a_ref(self):
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.find_by_tag("v1.0.0 with a space")
        self.assertEqual(caught.exception.code, "tag-malformed")

    def test_reads_a_published_release_with_its_tag_resolved(self):
        self.api.add_release()
        record = self.repository.find_by_tag(TAG)
        self.assertIsNotNone(record)
        self.assertFalse(record.draft)
        self.assertEqual(record.target_sha, SHA)
        self.assertEqual(record.url, f"https://github.com/{REPOSITORY}/releases/tag/{TAG}")

    def test_prefers_the_tag_over_a_branch_named_as_the_target(self):
        record = self.api.add_release()
        record["target_commitish"] = "main"
        self.api.tags[TAG] = SHA
        self.assertEqual(self.repository.find_by_tag(TAG).target_sha, SHA)

    def test_refuses_a_release_that_cannot_be_addressed(self):
        self.api.add_release()
        self.api.releases[0]["id"] = 0
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.find_by_tag(TAG)
        self.assertEqual(caught.exception.code, "release-identity-absent")

    def test_refuses_a_release_that_names_no_commit(self):
        self.api.add_release()
        self.api.releases[0]["target_commitish"] = ""
        self.api.tags.pop(TAG, None)
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.find_by_tag(TAG)
        self.assertEqual(caught.exception.code, "release-target-unknown")

    # -- the whole path, through the publisher
    def test_holds_a_draft_until_the_asset_set_is_complete(self):
        manifest, _ = self.artifact()
        publisher = self.publisher()

        drafted = publisher.draft(self.request(manifest))
        self.assertEqual(drafted.outcome, PUBLISHED)
        self.assertEqual(drafted.code, "drafted")
        self.assertEqual(self.api.uploaded, ["app-1.0.0.tar.gz"])
        self.assertTrue(self.api.release_by_tag(TAG)["draft"])

        published = publisher.publish(self.request(manifest))
        self.assertEqual(published.outcome, PUBLISHED)
        self.assertEqual(published.code, "published")
        self.assertFalse(self.api.release_by_tag(TAG)["draft"])
        self.assertIn("releases/tag/v1.0.0", published.detail("url"))

    def test_publishing_the_same_version_twice_is_a_skip(self):
        manifest, _ = self.artifact()
        publisher = self.publisher()
        publisher.draft(self.request(manifest))
        publisher.publish(self.request(manifest))

        again = publisher.publish(self.request(manifest))
        self.assertEqual(again.outcome, SKIPPED)
        self.assertEqual(again.code, "already-published")

    def test_resumes_a_draft_that_already_holds_the_assets(self):
        manifest, _ = self.artifact()
        first = self.publisher()
        first.draft(self.request(manifest))

        second = self.publisher()
        resumed = second.draft(self.request(manifest))
        self.assertEqual(resumed.outcome, PUBLISHED)
        self.assertEqual(resumed.detail("reused"), "app-1.0.0.tar.gz")
        self.assertEqual(len(self.api.releases), 1)

    def test_a_dry_run_creates_nothing(self):
        manifest, _ = self.artifact()
        result = self.publisher().draft(self.request(manifest, dry_run=True))
        self.assertEqual(result.outcome, SKIPPED)
        self.assertEqual(self.api.releases, [])

    def test_refuses_a_published_release_bound_to_another_commit(self):
        manifest, _ = self.artifact()
        self.api.add_release(target_sha=OTHER_SHA)
        result = self.publisher().publish(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "release-source-conflict")
        self.assertFalse(result.retryable)

    def test_refuses_a_draft_whose_commit_is_not_the_approved_one(self):
        manifest, _ = self.artifact()
        self.api.add_release(draft=True, target_sha=OTHER_SHA)
        result = self.publisher().draft(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "draft-source-conflict")
        self.assertFalse(result.retryable)

    def test_refuses_to_tag_a_commit_the_tag_already_names(self):
        self.api.tags[TAG] = OTHER_SHA
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.create_draft(tag=TAG, name=TAG, target_sha=SHA, notes="notes")
        self.assertEqual(caught.exception.code, "tag-source-conflict")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.api.releases, [])

    def test_requires_a_full_commit_to_draft_against(self):
        with self.assertRaises(ContractError):
            self.repository.create_draft(tag=TAG, name=TAG, target_sha="abc1234", notes="n")

    def test_takes_the_release_body_as_a_string_or_as_a_value(self):
        record = self.repository.create_draft(tag=TAG, name=TAG, target_sha=SHA, notes="plain")
        self.assertEqual(self.api.release_by_tag(TAG)["body"], "plain")
        self.repository.create_draft(
            tag="v1.1.0", name="v1.1.0", target_sha=SHA, notes=ReleaseNotes(body="value")
        )
        self.assertEqual(self.api.release_by_tag("v1.1.0")["body"], "value")

    # -- immutability
    def test_refuses_to_replace_an_asset_of_a_published_immutable_release(self):
        manifest, _ = self.artifact()
        self.api.add_release(immutable=True)
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.upload(
                self.repository.find_by_tag(TAG),
                _asset(manifest),
                manifest.artifacts[0].path,
            )
        self.assertEqual(caught.exception.code, "immutable-release")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("new version", str(caught.exception))
        self.assertEqual(self.api.uploaded, [])

    def test_reports_that_publication_did_not_become_immutable(self):
        manifest, _ = self.artifact()
        repository = GitHubReleaseRepository(self.api, REPOSITORY, immutable_expected=True)
        publisher = GitHubReleasePublisher(repository, notes="notes")
        publisher.draft(self.request(manifest))
        result = publisher.publish(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "immutability-absent")
        self.assertFalse(result.retryable)

    def test_promotes_to_an_immutable_release_when_the_destination_says_so(self):
        manifest, _ = self.artifact()
        self.api.immutable_after_publish = True
        repository = GitHubReleaseRepository(self.api, REPOSITORY, immutable_expected=True)
        publisher = GitHubReleasePublisher(repository, notes="notes")
        publisher.draft(self.request(manifest))
        result = publisher.publish(self.request(manifest))
        self.assertEqual(result.outcome, PUBLISHED)
        self.assertEqual(result.detail("immutable"), "true")

    def test_refuses_a_promotion_the_destination_declined_to_perform(self):
        manifest, _ = self.artifact()
        publisher = self.publisher()
        publisher.draft(self.request(manifest))
        self.api.accept_promotion = False
        result = publisher.publish(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "publish-unconfirmed")
        self.assertTrue(result.retryable)

    def test_reports_a_promotion_the_destination_refused(self):
        manifest, _ = self.artifact()
        publisher = self.publisher()
        publisher.draft(self.request(manifest))
        self.api.faults[("PATCH", "/releases/")] = GitHubApiError(
            "forbidden", code="credential-rejected", status=403
        )
        result = publisher.publish(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "publish-refused")
        self.assertFalse(result.retryable)

    # -- what the destination stores
    def test_refuses_an_upload_the_destination_stored_differently(self):
        manifest, _ = self.artifact()
        self.api.report_wrong_digest = hashlib.sha256(b"other bytes").hexdigest()
        result = self.publisher().draft(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "asset-digest-mismatch")
        self.assertFalse(result.retryable)

    def test_refuses_a_destination_that_cannot_report_digests(self):
        manifest, _ = self.artifact()
        self.api.omit_digests = True
        result = self.publisher().draft(self.request(manifest))
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.code, "asset-digest-unavailable")
        self.assertFalse(result.retryable)

    def test_accepts_a_destination_that_cannot_report_digests_when_asked_to(self):
        manifest, _ = self.artifact()
        self.api.omit_digests = True
        repository = GitHubReleaseRepository(self.api, REPOSITORY, strict_digests=False)
        publisher = GitHubReleasePublisher(repository, notes="notes")
        result = publisher.draft(self.request(manifest))
        self.assertEqual(result.outcome, PUBLISHED)
        self.assertEqual(result.code, "drafted")

    def test_refuses_a_file_whose_size_is_not_the_recorded_one(self):
        manifest, _ = self.artifact()
        artifact = manifest.artifacts[0]
        self.assertEqual(
            _asset(manifest).size, artifact.size
        )
        _grow(artifact.path)
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.upload(self._draft(), _asset(manifest), artifact.path)
        self.assertEqual(caught.exception.code, "artifact-size-mismatch")
        self.assertFalse(caught.exception.retryable)

    def test_reports_a_missing_artifact_as_retryable(self):
        manifest, _ = self.artifact()
        record = self._draft()
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.upload(
                record, _asset(manifest), os.path.join(self.workspace.name, "absent")
            )
        self.assertEqual(caught.exception.code, "artifact-missing")
        self.assertTrue(caught.exception.retryable)

    def test_reports_a_destination_that_already_holds_the_name(self):
        manifest, _ = self.artifact()
        record = self._draft()
        self.repository.upload(record, _asset(manifest), manifest.artifacts[0].path)
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.upload(record, _asset(manifest), manifest.artifacts[0].path)
        self.assertEqual(caught.exception.code, "asset-conflict")
        self.assertFalse(caught.exception.retryable)

    def test_lists_assets_across_pages(self):
        record = self.api.add_release()
        for index in range(150):
            self.api.add_asset(record, f"asset-{index}.txt", f"{index}")
        assets = self.repository.assets(self.repository.find_by_tag(TAG))
        self.assertEqual(len(assets), 150)
        self.assertEqual(assets[-1].name, "asset-149.txt")

    def test_stops_listing_a_destination_that_will_not_paginate(self):
        record = self.api.add_release()
        for index in range(300):
            self.api.add_asset(record, f"asset-{index}.txt", f"{index}")
        # Every page answers with the first hundred, so only the cap can end it.
        self.api.ignore_page = True
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.assets(self.repository.find_by_tag(TAG))
        self.assertEqual(caught.exception.code, "asset-list-truncated")

    def test_rejects_an_asset_list_that_is_not_a_list(self):
        self.api.add_release()
        original = self.api.request

        def malformed(method, path, body=None):
            if "/assets" in path:
                return {"assets": []}
            return original(method, path, body)

        self.api.request = malformed
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.assets(self.repository.find_by_tag(TAG))
        self.assertEqual(caught.exception.code, "asset-list-malformed")

    # -- classification
    def test_classifies_transient_and_fatal_statuses_apart(self):
        self.assertTrue(retryable_status(503))
        self.assertTrue(retryable_status(429))
        self.assertFalse(retryable_status(403))
        self.assertFalse(retryable_status(404))
        self.assertFalse(retryable_status(422))

    def test_surfaces_a_transient_destination_failure_as_retryable(self):
        manifest, _ = self.artifact()
        self.api.faults[("GET", "/releases/tags/")] = GitHubApiError(
            "service unavailable", code="destination-unavailable", retryable=True, status=503
        )
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.find_by_tag(TAG)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.status, 503)

    def test_reads_the_draft_that_appeared_between_the_lookup_and_the_write(self):
        record = self.api.add_release(draft=True)
        self.api.faults[("POST", "/releases")] = GitHubApiError(
            "already_exists", code="conflict", status=422
        )
        found = self.repository.create_draft(tag=TAG, name=TAG, target_sha=SHA, notes="n")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, str(record["id"]))

    def test_reports_a_conflict_it_cannot_explain(self):
        self.api.faults[("POST", "/releases")] = GitHubApiError(
            "already_exists", code="conflict", status=422
        )
        with self.assertRaises(GitHubApiError) as caught:
            self.repository.create_draft(tag=TAG, name=TAG, target_sha=SHA, notes="n")
        self.assertEqual(caught.exception.code, "draft-conflict")

    # -- the transport
    def test_refuses_to_be_built_without_a_token(self):
        with self.assertRaises(GitHubApiError) as caught:
            UrllibGitHubTransport("")
        self.assertEqual(caught.exception.code, "credential-absent")

    def test_translates_a_transport_failure_into_a_classified_one(self):
        transport = UrllibGitHubTransport("token", api_base="https://api.example.test")
        transport._opener = _raise_transport_error()
        with self.assertRaises(GitHubApiError) as caught:
            transport.request("GET", "/repos/a/b/releases")
        self.assertEqual(caught.exception.code, "destination-unreachable")
        self.assertTrue(caught.exception.retryable)

    def test_translates_a_status_into_a_classified_one(self):
        transport = UrllibGitHubTransport("token")
        transport._opener = _raise_status(503)
        with self.assertRaises(GitHubApiError) as caught:
            transport.request("GET", "/repos/a/b/releases")
        self.assertEqual(caught.exception.code, "destination-unavailable")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.status, 503)

    def test_names_its_status_codes_for_a_credential_problem(self):
        transport = UrllibGitHubTransport("token")
        transport._opener = _raise_status(403)
        with self.assertRaises(GitHubApiError) as caught:
            transport.request("GET", "/repos/a/b/releases")
        self.assertEqual(caught.exception.code, "credential-rejected")
        self.assertFalse(caught.exception.retryable)

    def test_reports_its_own_intent_without_pretending_to_publish(self):
        self.assertIn("draft", self.repository.intent())
        self.assertIn("mutable", self.repository.intent())
        expected = GitHubReleaseRepository(self.api, REPOSITORY, immutable_expected=True)
        self.assertIn("immutable", expected.intent())
        self.assertEqual(self.repository.repository, REPOSITORY)

    def test_a_view_translates_the_api_shape_into_the_ports_own(self):
        view = ReleaseRecordView.from_api(
            {
                "id": 7,
                "tag_name": TAG,
                "target_commitish": SHA,
                "draft": False,
                "html_url": "https://example.test/r",
                "immutable": True,
                "prerelease": True,
            }
        )
        record = view.to_record()
        self.assertEqual(record.id, "7")
        self.assertTrue(record.immutable)
        self.assertEqual(record.url, "https://example.test/r")

    # -- private helpers used above
    def _draft(self):
        self.repository.create_draft(tag=TAG, name=TAG, target_sha=SHA, notes="n")
        return self.repository.find_by_tag(TAG)


def _asset(manifest: ArtifactManifest) -> ReleaseAsset:
    artifact = manifest.artifacts[0]
    return ReleaseAsset(
        name=artifact.name,
        size=artifact.size,
        digest=artifact.digest,
        digest_algorithm=artifact.digest_algorithm,
    )


def _grow(path: str) -> None:
    with open(path, "ab") as handle:
        handle.write(b" more")


def _raise_status(status: int):
    import urllib.error

    def opener(request):
        raise urllib.error.HTTPError(request.full_url, status, "boom", {}, None)

    return opener


def _raise_transport_error():
    import urllib.error

    def opener(request):
        raise urllib.error.URLError("no route to host")

    return opener


if __name__ == "__main__":
    unittest.main()
