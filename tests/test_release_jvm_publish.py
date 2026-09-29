"""The two Maven publishers' decisions, against registries that are a dict.

Both destinations are unforgiving about the same thing -- a coordinate is
immutable once a file of it exists -- and unforgiving in opposite ways: GitHub
Packages lets a file be addressed at all, and Maven Central validates a bundle
before it will serve any of it. So the fakes here enforce the real rules, which
is the only way a test proves anything: a published file cannot be overwritten,
a version that reached Central cannot be deployed again, a deployment settles
only after the polls a test asks for, a promotion is refused for a deployment
that has not been validated, and a digest is echoed from the bytes that arrived.

The properties worth most of the assertions here are **a duplicate is green and
says why** and **nothing is spent before it has been checked**. The first is why
every re-run answers `skipped` rather than failing; the second is why a release
with no sources, no signature, or a POM that names another project is refused
before an upload rather than rejected minutes later by validation.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from continuum.release import contract, jvm, maven_publish
from continuum.release.contract import (
    SIGNING_SIGNED,
    TYPE_CHECKSUMS,
    TYPE_PACKAGE,
    TYPE_SIGNATURE,
    ManifestBuilder,
    PublishRequest,
    TargetSpec,
)
from continuum.release.maven_publish import (
    CENTRAL_ROOT,
    GITHUB_MAVEN_ROOT,
    PUBLISHING_AUTOMATIC,
    PUBLISHING_USER_MANAGED,
    RETRY_BACKOFF_BASE,
    STATE_FAILED,
    CentralSettings,
    GitHubPackagesPublisher,
    GitHubPackagesSettings,
    InMemoryCentral,
    InMemoryMavenRegistry,
    MavenApiError,
    MavenCentralPublisher,
    MavenError,
    parse_central_settings,
    parse_github_packages_settings,
    refuse_snapshot,
    write_bundle,
)

SHA = "e" * 40
VERSION = "1.4.0"
SNAPSHOT_VERSION = "1.4.0-SNAPSHOT"
GROUP = "com.example"
ARTIFACT = "widgets"
GAV = f"{GROUP}:{ARTIFACT}:{VERSION}"
NAMESPACE = "com.example-1234"
TOKEN_SECRET = "GITHUB_TOKEN"
USERNAME_SECRET = "CENTRAL_USERNAME"
PASSWORD_SECRET = "CENTRAL_PASSWORD"
OWNER = "example"
REPOSITORY = "widgets"

#: A POM carrying everything Central's validation asks a first-time namespace
#: for. A missing field here is a rejected deployment, so the fake writes a
#: complete one and the tests remove pieces of it deliberately.
POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>widgets</artifactId>
  <version>1.4.0</version>
  <name>Widgets</name>
  <description>A library that does one thing.</description>
  <url>https://example.com/widgets</url>
  <licenses>
    <license>
      <name>Apache-2.0</name>
      <url>https://www.apache.org/licenses/LICENSE-2.0.txt</url>
    </license>
  </licenses>
  <developers>
    <developer>
      <id>example</id>
      <name>Example</name>
      <email>dev@example.com</email>
    </developer>
  </developers>
  <scm>
    <url>https://github.com/example/widgets</url>
    <connection>scm:git:https://github.com/example/widgets.git</connection>
  </scm>
</project>
"""


def target() -> TargetSpec:
    return TargetSpec(id="jvm", adapter="jvm")


def details_of(result: Any) -> Dict[str, str]:
    """A publication result's details as a mapping.

    The contract carries details as a tuple of pairs so a result stays
    serialisable and hashable; every assertion about a detail goes through here
    so the tests read the way a caller would.
    """

    return dict(result.details)


def coordinates(version: str = VERSION) -> Any:
    return maven_publish.MavenCoordinates(
        group_id=GROUP, artifact_id=ARTIFACT, version=version
    )


def github_settings(**overrides: Any) -> GitHubPackagesSettings:
    values: Dict[str, Any] = {
        "group_id": GROUP,
        "artifact_id": ARTIFACT,
        "owner": OWNER,
        "repository": REPOSITORY,
        "token_secret": TOKEN_SECRET,
    }
    values.update(overrides)
    return GitHubPackagesSettings(**values)


def central_settings(**overrides: Any) -> CentralSettings:
    values: Dict[str, Any] = {
        "namespace": NAMESPACE,
        "group_id": GROUP,
        "artifact_id": ARTIFACT,
        "publishing_type": PUBLISHING_AUTOMATIC,
        "username_secret": USERNAME_SECRET,
        "password_secret": PASSWORD_SECRET,
    }
    values.update(overrides)
    return CentralSettings(**values)


class FakeLibrary:
    """A built library on disk, and the manifest that records it.

    The manifest is written by the real builder against the real files, because
    the publishers read digests and paths off it: a manifest assembled out of
    plausible-looking dictionaries would test the fakes rather than the code that
    ships.
    """

    def __init__(self, root: str, version: str = VERSION, signed: bool = True) -> None:
        self.root = root
        self.version = version
        self.signed = signed
        self.scratch = os.path.join(root, "build", "libs")
        os.makedirs(self.scratch, exist_ok=True)
        self.paths: Dict[str, str] = {}
        for suffix, body in (
            (".jar", b"jar bytes"),
            (".pom", POM.encode("utf-8")),
            ("-sources.jar", b"sources bytes"),
            ("-javadoc.jar", b"javadoc bytes"),
        ):
            path = os.path.join(self.scratch, f"{ARTIFACT}-{version}{suffix}")
            with open(path, "wb") as handle:
                handle.write(body)
            self.paths[suffix] = path
        if signed:
            for suffix in list(self.paths):
                path = self.paths[suffix] + ".asc"
                with open(path, "w", encoding="ascii") as handle:
                    handle.write("-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n")
                self.paths[suffix + ".asc"] = path
        self.checksums = os.path.join(root, f"SHA256SUMS-{version}.txt")
        with open(self.checksums, "w", encoding="ascii") as handle:
            handle.write("not a repository file\n")
        self.manifest = self._build_manifest()

    def _build_manifest(self) -> contract.ArtifactManifest:
        builder = ManifestBuilder(target="jvm", adapter="jvm", source_sha=SHA, version=self.version)
        classifiers = {
            ".jar": maven_publish.CLASSIFIER_MAIN,
            ".pom": maven_publish.CLASSIFIER_POM,
            "-sources.jar": maven_publish.CLASSIFIER_SOURCES,
            "-javadoc.jar": maven_publish.CLASSIFIER_JAVADOC,
        }
        for suffix, classifier in classifiers.items():
            if suffix not in self.paths:
                continue
            signature = self.paths.get(suffix + ".asc")
            builder.record(
                name=os.path.basename(self.paths[suffix]),
                path=self.paths[suffix],
                type=TYPE_PACKAGE,
                classifier=classifier,
                media_type=jvm.MEDIA_TYPE_JAR
                if suffix.endswith(".jar")
                else jvm.MEDIA_TYPE_POM,
                signing=SIGNING_SIGNED if self.signed else contract.SIGNING_UNSIGNED,
                signing_identity="CN=Widgets" if self.signed else "",
            )
            if signature:
                builder.record(
                    name=os.path.basename(signature),
                    path=signature,
                    type=TYPE_SIGNATURE,
                    classifier=f"{classifier}-asc",
                    media_type=jvm.MEDIA_TYPE_SIGNATURE,
                )
        # A distribution and a checksum file are in the same release. Neither
        # belongs in a repository namespace, and the assertions below are what
        # keep them out.
        builder.record(
            name=f"{ARTIFACT}-{self.version}-all.zip",
            path=self.checksums,
            type=TYPE_PACKAGE,
            classifier=jvm.CLASSIFIER_DIST_ZIP,
            media_type=jvm.MEDIA_TYPE_ZIP,
        )
        builder.record(
            name=os.path.basename(self.checksums),
            path=self.checksums,
            type=TYPE_CHECKSUMS,
            media_type="text/plain",
        )
        # A publisher is only ever handed a manifest the verify stage has already
        # accepted, so the fixture is marked the way the core marks it. Building
        # one that is not would test a state a release cannot reach.
        return builder.build().mark_verified("jvm/gradle")

    def request(self, **overrides: Any) -> PublishRequest:
        values: Dict[str, Any] = {
            "destination": "maven",
            "tag": f"v{self.version}",
            "version": self.version,
            "source_sha": SHA,
            "manifests": (self.manifest,),
            "key": "publish-jvm",
        }
        values.update(overrides)
        return PublishRequest(**values)


class PublisherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="continuum-jvm-publish-")
        self.addCleanup(self._remove)
        self.scratch = os.path.join(self.root, "scratch")
        self.sleeps: List[float] = []
        self.library = FakeLibrary(self.root)

    def _remove(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def sleeper(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def rewrite_pom(self, body: str) -> None:
        with open(self.library.paths[".pom"], "w", encoding="utf-8") as handle:
            handle.write(body)

    def drop_suffix(self, suffix: str) -> None:
        """Remove one file and its signature, and record the release without it.

        The manifest is rebuilt rather than edited, so the release these tests
        publish is one the real builder would have written.
        """

        os.remove(self.library.paths.pop(suffix))
        for key in [name for name in self.library.paths if name.startswith(suffix + ".asc")]:
            os.remove(self.library.paths.pop(key))
        self.library.manifest = self.library._build_manifest()

    def without_file(self, suffix: str) -> PublishRequest:
        """A request whose manifest omits one file, for the "missing" cases."""

        self.drop_suffix(suffix)
        return self.library.request()

    def make_github(
        self, settings: Optional[GitHubPackagesSettings] = None, registry: Optional[Any] = None
    ) -> Tuple[GitHubPackagesPublisher, InMemoryMavenRegistry]:
        transport = registry if registry is not None else InMemoryMavenRegistry()
        publisher = GitHubPackagesPublisher(
            settings or github_settings(),
            transport,
            sleeper=self.sleeper,
        )
        return publisher, transport

    def make_central(
        self,
        settings: Optional[CentralSettings] = None,
        transport: Optional[Any] = None,
        deployment_id: str = "",
    ) -> Tuple[MavenCentralPublisher, InMemoryCentral]:
        portal = transport if transport is not None else InMemoryCentral()
        publisher = MavenCentralPublisher(
            settings or central_settings(),
            portal,
            deployment_id=deployment_id,
            scratch_dir=self.scratch,
            sleeper=self.sleeper,
        )
        return publisher, portal


class GitHubPackagesConfigurationTests(PublisherTestCase):
    def test_the_token_is_referenced_by_secret_name(self):
        parsed = github_settings()
        self.assertEqual(parsed.secret_names(), (TOKEN_SECRET,))
        self.assertNotIn("ghp_", parsed.describe().get("token", ""))

    def test_a_repository_is_named_before_a_url_is_built(self):
        with self.assertRaises(MavenError) as caught:
            GitHubPackagesSettings(group_id=GROUP, artifact_id=ARTIFACT, owner=OWNER)
        self.assertIn("repository", str(caught.exception))

    def test_the_url_is_the_repository_scoped_registry_path(self):
        parsed = github_settings()
        self.assertEqual(parsed.base_url(), f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}")

    def test_an_unknown_key_is_refused_rather_than_ignored(self):
        with self.assertRaises(MavenError) as caught:
            parse_github_packages_settings(
                {"group_id": GROUP, "artifact_id": ARTIFACT, "repository": REPOSITORY, "regstry": "x"}
            )
        self.assertIn("regstry", str(caught.exception))


class GitHubPackagesPublishTests(PublisherTestCase):
    def test_a_complete_coordinate_is_uploaded_once(self):  # noqa: E501
        publisher, transport = self.make_github()
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.PUBLISHED)
        self.assertEqual(result.identity, GAV)
        base = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        # Four artifacts and four signatures, and nothing else: a distribution
        # zip and a checksum file are release assets, not repository files.
        self.assertEqual(
            transport.present(base, ""),
            [
                f"{ARTIFACT}-{VERSION}-javadoc.jar",
                f"{ARTIFACT}-{VERSION}-javadoc.jar.asc",
                f"{ARTIFACT}-{VERSION}-sources.jar",
                f"{ARTIFACT}-{VERSION}-sources.jar.asc",
                f"{ARTIFACT}-{VERSION}.jar",
                f"{ARTIFACT}-{VERSION}.jar.asc",
                f"{ARTIFACT}-{VERSION}.pom",
                f"{ARTIFACT}-{VERSION}.pom.asc",
            ],
        )

    def test_a_published_coordinate_is_green_on_a_re_run(self):
        publisher, transport = self.make_github()
        request = self.library.request(destination="github packages")
        first = publisher.draft(request)
        self.assertTrue(first.ok, first.reason)
        uploads = len(transport.uploads)
        again = publisher.draft(request)
        self.assertTrue(again.ok, again.reason)
        self.assertEqual(again.outcome, contract.SKIPPED)
        self.assertEqual(again.code, "already-published")
        self.assertEqual(len(transport.uploads), uploads)

    def test_a_partial_coordinate_is_refused_rather_than_completed(self):
        publisher, transport = self.make_github()
        base = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        transport.files[f"{base}/{ARTIFACT}-{VERSION}.jar"] = b"an earlier build"
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "version-partial")
        self.assertFalse(result.retryable)
        # The next step is in the message, because the contract does not carry a
        # remediation field of its own.
        self.assertIn("cut a new version", result.reason)
        self.assertEqual(details_of(result)["absent"].count(".asc"), 4)
        self.assertEqual(transport.uploads, [])

    def test_a_second_upload_of_a_published_file_is_refused_by_the_registry(self):
        publisher, transport = self.make_github()
        base = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        # Every file but the sources jar is there: the second build would be
        # completing a version whose other files came from the first.
        for suffix in (".jar", ".pom", "-javadoc.jar"):
            transport.files[f"{base}/{ARTIFACT}-{VERSION}{suffix}"] = b"earlier"
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertEqual(result.code, "version-partial")

    def test_a_coordinate_that_lost_a_file_is_reported_as_absent(self):
        publisher, transport = self.make_github()
        request = self.library.request(destination="github packages")
        self.assertTrue(publisher.draft(request).ok)
        base = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        # Something removed a file between the two stages -- a registry
        # housekeeping job, or a re-run against a different namespace. The
        # confirm stage is a read, so it sees the gap; it does not answer from
        # the memory of having uploaded the file a moment ago.
        del transport.files[f"{base}/{ARTIFACT}-{VERSION}-sources.jar"]
        confirmed = publisher.publish(request)
        self.assertEqual(confirmed.outcome, contract.FAILED)
        self.assertEqual(confirmed.code, "version-partial")
        self.assertIn("sources.jar", confirmed.reason)

    def test_a_coordinate_that_never_existed_is_retryable(self):
        publisher, transport = self.make_github()
        publisher.draft(self.library.request(destination="github packages"))
        transport.files.clear()
        confirmed = publisher.publish(self.library.request(destination="github packages"))
        self.assertEqual(confirmed.code, "coordinate-absent")
        self.assertTrue(confirmed.retryable)

    def test_a_truncated_upload_is_caught_before_it_is_accepted(self):
        publisher, transport = self.make_github()
        transport.echo_digest = True

        def wrong_size(url: str, name: str, path: str) -> Any:
            receipt = InMemoryMavenRegistry.upload(transport, url, name, path)
            return maven_publish.UploadReceipt(
                size=receipt.size - 1, sha256=receipt.sha256, created=receipt.created
            )

        transport.upload = wrong_size  # type: ignore[method-assign]
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "upload-size-mismatch")

    def test_a_registry_that_refuses_is_reported_rather_than_retried(self):
        publisher, transport = self.make_github()
        transport.fail_next(MavenApiError("unauthorized", "bad credentials", status=401))
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "unauthorized")
        self.assertFalse(result.retryable)

    def test_a_registry_error_that_will_not_clear_stops_the_release(self):
        publisher, transport = self.make_github()
        transport.fail_next(
            MavenApiError("registry-unavailable", "the registry is having a moment", status=503, retryable=True)
        )
        result = publisher.draft(self.library.request(destination="github packages"))
        # The transport is what retries; a publisher handed a still-failing
        # registry reports it and stops, rather than uploading into a registry
        # that has already said no three times.
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertTrue(result.retryable)

    def test_a_dry_run_uploads_nothing_and_says_what_it_would(self):
        publisher, transport = self.make_github()
        result = publisher.draft(self.library.request(destination="github packages", dry_run=True))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(result.external_version, VERSION)
        self.assertEqual(transport.uploads, [])
        self.assertEqual(transport.lookups, [])

    def test_publishing_a_coordinate_that_is_not_there_is_retryable(self):
        publisher, _ = self.make_github()
        result = publisher.publish(self.library.request(destination="github packages"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "coordinate-absent")
        self.assertTrue(result.retryable)

    def test_an_unsigned_library_is_published_when_signatures_are_not_required(self):
        self.library = FakeLibrary(self.root, signed=False)
        publisher, _ = self.make_github(github_settings(require_signatures=False))
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertTrue(result.ok, result.reason)

    def test_an_unsigned_library_is_refused_when_signatures_are_required(self):
        self.library = FakeLibrary(self.root, signed=False)
        publisher, _ = self.make_github(github_settings(require_signatures=True))
        result = publisher.draft(self.library.request(destination="github packages"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "library-incomplete")
        self.assertIn("signature", result.reason)


class CentralConfigurationTests(PublisherTestCase):
    def test_the_token_is_referenced_by_secret_name(self):
        parsed = central_settings()
        self.assertEqual(set(parsed.secret_names()), {USERNAME_SECRET, PASSWORD_SECRET})

    def test_a_token_may_be_used_instead_of_a_password(self):
        parsed = parse_central_settings(
            {
                "namespace": NAMESPACE,
                "group_id": GROUP,
                "artifact_id": ARTIFACT,
                "token_secret": "CENTRAL_TOKEN",
            }
        )
        self.assertEqual(parsed.secret_names(), ("CENTRAL_TOKEN",))

    def test_a_namespace_is_named_before_a_url_is_built(self):
        with self.assertRaises(MavenError) as caught:
            CentralSettings(namespace="", group_id=GROUP, artifact_id=ARTIFACT)
        self.assertIn("namespace", str(caught.exception))

    def test_an_unknown_publishing_type_is_refused(self):
        with self.assertRaises(MavenError) as caught:
            central_settings(publishing_type="PUBLISH_IT")
        self.assertIn("AUTOMATIC", str(caught.exception))

    def test_the_api_root_is_the_publisher_endpoint(self):
        parsed = central_settings()
        self.assertEqual(parsed.api_root(), f"{CENTRAL_ROOT}/api/v1/publisher")


class SnapshotTests(PublisherTestCase):
    def test_a_snapshot_version_is_refused_before_anything_is_uploaded(self):
        self.library = FakeLibrary(self.root, version=SNAPSHOT_VERSION)
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "snapshot-refused")
        self.assertEqual(portal.bundles, [])
        self.assertEqual(portal.calls, [])

    def test_the_refusal_says_why_a_snapshot_cannot_be_published(self):
        with self.assertRaises(MavenError) as caught:
            refuse_snapshot(SNAPSHOT_VERSION)
        self.assertIn("-SNAPSHOT", str(caught.exception))

    def test_a_release_version_is_accepted(self):
        self.assertIsNone(refuse_snapshot(VERSION))


class CentralPublishTests(PublisherTestCase):
    def test_an_automated_deployment_is_published_and_reported(self):
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.PUBLISHED)
        # The identity is namespace-scoped: the same gav in two namespaces is
        # two deployments to Central, and an identity that could not tell them
        # apart could not be checked for duplication.
        self.assertEqual(result.identity, f"{NAMESPACE}:{GAV}")
        self.assertTrue(result.external_id.startswith("deployment-"))
        self.assertEqual(portal.bundles[0]["publishing_type"], PUBLISHING_AUTOMATIC)
        self.assertEqual(portal.bundles[0]["namespace"], NAMESPACE)

    def test_the_bundle_is_laid_out_as_the_repository_paths(self):
        publisher, portal = self.make_central()
        publisher.draft(self.library.request(destination="maven central"))
        with zipfile.ZipFile(portal.bundles and self._bundle_path(portal)) as archive:
            names = set(archive.namelist())
        prefix = f"{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        self.assertIn(f"{prefix}/{ARTIFACT}-{VERSION}.jar", names)
        self.assertIn(f"{prefix}/{ARTIFACT}-{VERSION}.jar.asc", names)
        self.assertIn(f"{prefix}/{ARTIFACT}-{VERSION}.pom", names)
        self.assertIn(f"{prefix}/{ARTIFACT}-{VERSION}-sources.jar", names)
        self.assertIn(f"{prefix}/{ARTIFACT}-{VERSION}-javadoc.jar", names)
        # A distribution zip and a checksum file are not repository files, so a
        # bundle carrying them would be a deployment Central rejects.
        self.assertFalse([name for name in names if name.endswith(".zip")])
        self.assertFalse([name for name in names if "SHA256SUMS" in name])

    def _bundle_path(self, portal: InMemoryCentral) -> str:
        for entry in os.listdir(self.scratch):
            if entry.endswith(".zip"):
                return os.path.join(self.scratch, entry)
        raise AssertionError("no bundle was written")

    def test_the_bundle_name_is_a_file_name_and_names_the_coordinate(self):
        publisher, portal = self.make_central()
        publisher.draft(self.library.request(destination="maven central"))
        name = portal.bundles[0]["name"]
        self.assertTrue(name.endswith("-bundle.zip"))
        self.assertIn("com.example-widgets-1.4.0", name)
        self.assertNotIn(":", name)

    def test_a_configured_deployment_name_is_used_verbatim(self):
        publisher, portal = self.make_central(
            central_settings(deployment_name="widgets-1.4.0.deployment")
        )
        publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(portal.bundles[0]["name"], "widgets-1.4.0.deployment")

    def test_two_runs_of_the_same_manifest_produce_the_same_bundle(self):
        digests = []
        for _ in range(2):
            publisher, _ = self.make_central()
            publisher.draft(self.library.request(destination="maven central"))
            digests.append(self._bundle_digest())
        # A bundle that varied between runs would make a re-upload look like a
        # different release, and the Portal would validate the second one
        # against a deployment nobody can reproduce.
        self.assertEqual(digests[0], digests[1])

    def _bundle_digest(self) -> str:
        return contract.digest_file(self._bundle_path(None), "sha256")[1]

    def test_a_dry_run_uploads_nothing(self):
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central", dry_run=True))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(portal.bundles, [])
        self.assertIn(ARTIFACT, result.reason)

    def test_a_published_version_is_refused_the_second_time(self):
        publisher, portal = self.make_central()
        self.assertTrue(publisher.draft(self.library.request(destination="maven central")).ok)
        again, _ = self.make_central(transport=portal)
        result = again.draft(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(result.code, "already-published")
        self.assertEqual(len(portal.bundles), 1)

    def test_a_seeded_published_version_is_refused(self):
        portal = InMemoryCentral()
        portal.publish_version(VERSION, namespace=NAMESPACE)
        publisher, _ = self.make_central(transport=portal)
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.code, "already-published")
        self.assertEqual(portal.bundles, [])

    def test_another_namespace_may_publish_the_same_version(self):
        portal = InMemoryCentral()
        portal.publish_version(VERSION, namespace="com.other-9999")
        publisher, _ = self.make_central(transport=portal)
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)

    def test_validation_failure_carries_what_the_portal_reported(self):
        portal = InMemoryCentral(
            validation_errors=(
                "You must have a Sources JAR",
                "You must have a Javadoc JAR",
            )
        )
        publisher, _ = self.make_central(transport=portal)
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "validation-failed")
        self.assertFalse(result.retryable)
        self.assertIn("Sources JAR", result.reason)
        self.assertIn("Javadoc JAR", result.reason)
        self.assertEqual(details_of(result)["state"], STATE_FAILED)

    def test_a_deployment_is_polled_until_it_settles(self):
        portal = InMemoryCentral(polls_before_settled=3)
        publisher, _ = self.make_central(transport=portal)
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertGreaterEqual(portal.calls.count("status"), 3)
        self.assertTrue(self.sleeps)

    def test_a_deployment_that_never_settles_is_retryable(self):
        portal = InMemoryCentral(polls_before_settled=99)
        publisher, _ = self.make_central(
            central_settings(poll_attempts=2, poll_seconds=0.1), transport=portal
        )
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "deployment-unsettled")
        self.assertTrue(result.retryable)

    def test_a_transient_status_error_is_retried(self):
        portal = InMemoryCentral(polls_before_settled=2)
        publisher, _ = self.make_central(
            central_settings(attempts=3, poll_seconds=0.1), transport=portal
        )
        publisher.draft(self.library.request(destination="maven central"))
        portal.fail_next(
            MavenApiError("portal-unavailable", "try later", status=503, retryable=True)
        )
        result = publisher.publish(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)

    def test_a_status_error_that_never_clears_stops_the_polling(self):
        portal = InMemoryCentral()
        publisher, _ = self.make_central(
            central_settings(attempts=2, poll_seconds=0.1), transport=portal
        )
        publisher.draft(self.library.request(destination="maven central"))
        portal.fail_next(
            *[
                MavenApiError("portal-unavailable", "try later", status=503, retryable=True)
                for _ in range(4)
            ]
        )
        result = publisher.publish(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertTrue(result.retryable)


class CentralPromotionTests(PublisherTestCase):
    def test_a_user_managed_namespace_waits_for_a_person(self):
        portal = InMemoryCentral(published=False)
        publisher, _ = self.make_central(
            central_settings(publishing_type=PUBLISHING_USER_MANAGED), transport=portal
        )
        result = publisher.draft(self.library.request(destination="maven central"))
        # Validation passed but nothing is on Maven Central, so this is not a
        # publication and must not report one.
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(result.code, "validated")
        self.assertEqual(portal.promoted, [])
        self.assertNotEqual(result.outcome, contract.PUBLISHED)

    def test_promoting_a_validated_deployment_publishes_it(self):
        portal = InMemoryCentral(published=False)
        settings = central_settings(
            publishing_type=PUBLISHING_USER_MANAGED, promote_validated=True
        )
        publisher, _ = self.make_central(settings, transport=portal)
        publisher.draft(self.library.request(destination="maven central"))
        result = publisher.publish(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.PUBLISHED)
        self.assertEqual(len(portal.promoted), 1)

    def test_a_deployment_that_is_already_published_is_not_promoted_again(self):
        portal = InMemoryCentral(published=True)
        publisher, _ = self.make_central(
            central_settings(
                publishing_type=PUBLISHING_USER_MANAGED, promote_validated=True
            ),
            transport=portal,
        )
        publisher.draft(self.library.request(destination="maven central"))
        result = publisher.publish(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, contract.SKIPPED)
        self.assertEqual(result.code, "already-published")
        self.assertEqual(portal.promoted, [])

    def test_a_resumed_run_asks_about_its_deployment_instead_of_uploading(self):
        portal = InMemoryCentral(published=False)
        first, _ = self.make_central(
            central_settings(publishing_type=PUBLISHING_USER_MANAGED), transport=portal
        )
        first.draft(self.library.request(destination="maven central"))
        deployment_id = list(portal.deployments)[0]
        resumed, _ = self.make_central(
            central_settings(
                publishing_type=PUBLISHING_USER_MANAGED, promote_validated=True
            ),
            transport=portal,
            deployment_id=deployment_id,
        )
        result = resumed.draft(self.library.request(destination="maven central"))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(portal.bundles), 1)

    def test_a_run_with_no_deployment_id_says_so_instead_of_guessing(self):
        publisher, _ = self.make_central()
        result = publisher.publish(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "deployment-absent")
        self.assertTrue(result.retryable)
        self.assertIn("deployment_id", result.reason)

    def test_a_deployment_still_validating_is_retryable(self):
        portal = InMemoryCentral(polls_before_settled=99)
        publisher, _ = self.make_central(
            central_settings(poll_attempts=2, poll_seconds=0.1),
            transport=portal,
            deployment_id="deployment-001",
        )
        portal.seed("deployment-001", "VALIDATING", namespace=NAMESPACE, polls=99)
        result = publisher.publish(self.library.request(destination="maven central"))
        # The Portal has not finished validating, nothing has been released, and
        # the deployment is still going to settle on its own.
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "deployment-unsettled")
        self.assertTrue(result.retryable)
        self.assertEqual(details_of(result)["state"], "VALIDATING")


class DryRunValidationTests(PublisherTestCase):
    """A dry run is a check, not a preview of a check.

    The point of planning is to find out that a release will be refused while
    the answer costs nothing, so a dry run that skipped validation would be the
    one run that reports success for a release that cannot be published. It
    validates everything and uploads nothing.
    """

    def test_a_central_dry_run_refuses_a_release_central_would_reject(self):
        self.without_file("-sources.jar")
        publisher, portal = self.make_central()
        result = publisher.draft(
            self.library.request(destination="maven central", dry_run=True)
        )
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "central-metadata-incomplete")
        self.assertEqual(portal.bundles, [])
        self.assertEqual(portal.calls, [])

    def test_a_central_dry_run_refuses_an_unsigned_release(self):
        self.library = FakeLibrary(self.root, signed=False)
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central", dry_run=True))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertIn("signature", result.reason)
        self.assertEqual(portal.bundles, [])

    def test_a_central_dry_run_refuses_a_pom_missing_metadata(self):
        self.rewrite_pom(POM.replace("<name>Widgets</name>", ""))
        publisher, _ = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central", dry_run=True))
        self.assertEqual(result.outcome, contract.FAILED)

    def test_a_github_packages_dry_run_refuses_an_incomplete_library(self):
        self.without_file(".pom")
        publisher, transport = self.make_github()
        result = publisher.draft(
            self.library.request(destination="github packages", dry_run=True)
        )
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "library-incomplete")
        self.assertEqual(transport.uploads, [])

    def test_a_dry_run_of_a_whole_release_that_would_publish_is_green(self):
        for settings_kwargs, publisher in (
            ({"require_signatures": True}, self.make_github()[0]),
            ({}, self.make_central()[0]),
        ):
            with self.subTest(destination=publisher.destination):
                result = publisher.draft(
                    self.library.request(destination=publisher.destination, dry_run=True)
                )
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.outcome, contract.SKIPPED)
                self.assertEqual(result.external_version, VERSION)


class CentralBundleValidationTests(PublisherTestCase):
    def test_a_release_without_sources_is_refused_before_an_upload(self):
        request = self.without_file("-sources.jar")
        publisher, portal = self.make_central()
        result = publisher.draft(request)
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertEqual(result.code, "central-metadata-incomplete")
        self.assertIn("sources", result.reason)
        self.assertEqual(portal.bundles, [])

    def test_a_release_without_javadoc_is_refused_before_an_upload(self):
        request = self.without_file("-javadoc.jar")
        publisher, portal = self.make_central()
        result = publisher.draft(request)
        self.assertIn("javadoc", result.reason)
        self.assertEqual(portal.bundles, [])

    def test_sources_and_javadoc_may_be_waived_for_a_namespace_that_has_published(self):
        self.without_file("-sources.jar")
        request = self.without_file("-javadoc.jar")
        publisher, portal = self.make_central(
            central_settings(require_sources=False, require_javadoc=False)
        )
        result = publisher.draft(request)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(portal.bundles), 1)

    def test_an_unsigned_release_is_refused_before_an_upload(self):
        self.library = FakeLibrary(self.root, signed=False)
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertIn("signature", result.reason)
        self.assertEqual(portal.bundles, [])

    def test_a_pom_missing_a_licence_is_refused_before_an_upload(self):
        self.rewrite_pom(POM.replace("licenses", "unused-licenses"))
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertIn("licence", result.reason.lower() + " licences")
        self.assertEqual(portal.bundles, [])

    def test_a_pom_missing_a_developer_is_refused_before_an_upload(self):
        self.rewrite_pom(POM.replace("developers", "unused-developers"))
        publisher, portal = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertIn("developer", result.reason)
        self.assertEqual(portal.bundles, [])

    def test_a_pom_naming_another_project_is_refused(self):
        self.rewrite_pom(POM.replace("<groupId>com.example</groupId>", "<groupId>com.other</groupId>"))
        publisher, _ = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        self.assertEqual(result.outcome, contract.FAILED)
        self.assertIn("com.other", result.reason)

    def test_every_problem_is_reported_in_one_pass(self):
        self.without_file("-sources.jar")
        self.without_file("-javadoc.jar")
        publisher, _ = self.make_central()
        result = publisher.draft(self.library.request(destination="maven central"))
        # A release that has to be fixed should be told all of it at once, not
        # one problem per run.
        self.assertIn("sources", result.reason)
        self.assertIn("javadoc", result.reason)
        self.assertEqual(int(details_of(result)["problems"]), 2)


class FakeResponse:
    """Just enough of an HTTP response for the transports to read."""

    def __init__(self, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def items(self) -> Any:
        return self.headers.items()

    def get(self, name: str, default: Any = None) -> Any:
        # `HTTPError.headers` is an `email.message.Message` in the real library,
        # whose `get` is case-insensitive; the transport reads `Retry-After`
        # through it, so the fake has to answer the same way.
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class RecordingUrlopen:
    """A `urlopen` that answers from a script, and records what it was asked.

    Asserting on the method, the URL and the headers is the only way to show
    that the transports speak the documented protocol rather than a plausible
    one: the Portal's status endpoint is a `POST` with the id in the query, and
    a `GET` would be a guess that a Portal behind a CDN may answer to something
    else entirely.
    """

    def __init__(self, answers: Any) -> None:
        #: A list of responses or exceptions, consumed in order. The last one
        #: repeats, so a test that only cares about the outcome can give one.
        self.answers = list(answers)
        self.requests: List[Tuple[str, str, Dict[str, str], bytes]] = []

    def __call__(self, request: Any, timeout: Any = None) -> Any:
        self.requests.append(
            (
                request.get_method(),
                request.full_url,
                {key.lower(): value for key, value in request.header_items()},
                request.data or b"",
            )
        )
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return self._http_error(answer)
        return answer

    @staticmethod
    def _http_error(status: int) -> Any:
        return http_error(status)


def http_error(status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> Any:
    """A real `HTTPError`, which is what `urlopen` raises for a 4xx or 5xx."""

    return urllib.error.HTTPError(
        url="https://central.sonatype.com/api/v1/publisher/status",
        code=status,
        msg="refused",
        hdrs=FakeResponse(status, body, headers),  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


class GitHubHttpTransportTests(PublisherTestCase):
    def transport(self, answers: Any, token: str = "ghp_example") -> Any:
        self.sleeps = []
        return maven_publish.HttpGitHubPackagesTransport(
            {TOKEN_SECRET: token}, sleeper=self.sleeper
        ), RecordingUrlopen(answers)

    def test_a_missing_file_is_an_answer_rather_than_a_failure(self):
        transport, opener = self.transport([http_error(404, b"{}")])
        url = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/{GROUP.replace('.', '/')}/{ARTIFACT}/{VERSION}"
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            # A 404 is the normal state of a version that has not been
            # published, which is every version on a first release.
            self.assertFalse(transport.file_exists(url, f"{ARTIFACT}-{VERSION}.jar"))
        self.assertEqual(opener.requests[0][0], "HEAD")
        self.assertEqual(opener.requests[0][1], f"{url}/{ARTIFACT}-{VERSION}.jar")

    def test_the_token_travels_in_the_header_and_nowhere_else(self):
        transport, opener = self.transport([FakeResponse(200)])
        url = f"{GITHUB_MAVEN_ROOT}/{OWNER}/{REPOSITORY}/com/example/widgets/1.4.0"
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            self.assertTrue(transport.file_exists(url, "widgets-1.4.0.jar"))
        method, _, headers, _ = opener.requests[0]
        self.assertEqual(headers["authorization"], "Bearer ghp_example")
        self.assertNotIn("ghp_example", url)

    def test_a_missing_token_is_refused_before_anything_is_sent(self):
        transport = maven_publish.HttpGitHubPackagesTransport({}, sleeper=self.sleeper)
        with self.assertRaises(MavenError) as caught:
            transport._token()
        # The remedy is in the remediation, which is what a job prints.
        self.assertIn("packages: write", caught.exception.remediation)

    def test_a_temporary_failure_is_retried_with_a_growing_backoff(self):
        transport, opener = self.transport([http_error(503, b"busy")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError):
                transport.file_exists("https://example.com/x", "y")
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.sleeps, [RETRY_BACKOFF_BASE, RETRY_BACKOFF_BASE * 2])

    def test_the_retry_after_the_registry_asked_for_is_obeyed(self):
        transport, opener = self.transport([http_error(429, b"slow down", {"Retry-After": "7"})])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError):
                transport.file_exists("https://example.com/x", "y")
        self.assertEqual(self.sleeps, [7.0, 7.0])

    def test_a_refused_token_is_not_retried(self):
        transport, opener = self.transport([http_error(401, b"nope")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError) as caught:
                transport.file_exists("https://example.com/x", "y")
        self.assertEqual(caught.exception.code, "credential-rejected")
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_an_immutable_version_is_reported_as_a_destination_state(self):
        transport, opener = self.transport([http_error(409, b"already exists")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError) as caught:
                transport.upload("https://example.com/x", "y", self.library.paths[".jar"])
        self.assertEqual(caught.exception.code, "version-exists")
        self.assertTrue(caught.exception.version_exists)

    def test_an_upload_puts_the_bytes_and_echoes_the_digest(self):
        transport, opener = self.transport([FakeResponse(201)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            receipt = transport.upload("https://example.com/x", "y", self.library.paths[".jar"])
        method, url, _, body = opener.requests[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "https://example.com/x/y")
        with open(self.library.paths[".jar"], "rb") as handle:
            self.assertEqual(body, handle.read())
        self.assertEqual(receipt.sha256, contract.digest_file(self.library.paths[".jar"], "sha256")[1])
        self.assertTrue(receipt.created)

    def test_a_201_is_a_creation_and_a_200_is_not(self):
        transport, opener = self.transport([FakeResponse(200)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            receipt = transport.upload("https://example.com/x", "y", self.library.paths[".jar"])
        self.assertFalse(receipt.created)


class CentralHttpTransportTests(PublisherTestCase):
    def transport(self, answers: Any, environment: Optional[Dict[str, str]] = None) -> Any:
        return maven_publish.HttpCentralTransport(
            environment
            if environment is not None
            else {USERNAME_SECRET: "example", PASSWORD_SECRET: "hunter2"},
            settings=central_settings(),
        ), RecordingUrlopen(answers)

    def test_the_status_endpoint_is_a_post_with_the_id_in_the_query(self):
        transport, opener = self.transport(
            [FakeResponse(200, json.dumps({"deploymentState": "VALIDATED"}).encode())]
        )
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            state = transport.status("deployment-001")
        method, url, _, _ = opener.requests[0]
        self.assertEqual(method, "POST")
        self.assertIn("/api/v1/publisher/status?", url)
        self.assertIn("id=deployment-001", url)
        self.assertEqual(state.state, "VALIDATED")

    def test_the_upload_carries_the_bundle_and_the_publishing_type(self):
        transport, opener = self.transport([FakeResponse(200, b"deployment-007")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            deployment_id = transport.upload_bundle(
                NAMESPACE, PUBLISHING_AUTOMATIC, "widgets.zip", self.library.paths[".jar"]
            )
        method, url, headers, body = opener.requests[0]
        self.assertEqual(deployment_id, "deployment-007")
        self.assertEqual(method, "POST")
        self.assertIn("name=widgets.zip", url)
        self.assertIn("publishingType=AUTOMATIC", url)
        self.assertIn("multipart/form-data", headers["content-type"])
        self.assertIn(b"jar bytes", body)

    def test_a_json_envelope_is_accepted_as_well_as_a_bare_id(self):
        transport, opener = self.transport(
            [FakeResponse(200, json.dumps({"deploymentId": "deployment-009"}).encode())]
        )
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            self.assertEqual(
                transport.upload_bundle(NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"]),
                "deployment-009",
            )

    def test_an_acceptance_with_no_deployment_id_is_refused(self):
        transport, opener = self.transport([FakeResponse(200, b"")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenError) as caught:
                transport.upload_bundle(
                    NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"]
                )
        self.assertIn("no deployment id", str(caught.exception))

    def test_the_credential_is_assembled_and_never_leaks_into_the_url(self):
        transport, opener = self.transport(
            [FakeResponse(200, b"deployment-001")],
            {USERNAME_SECRET: "example", PASSWORD_SECRET: "hunter2"},
        )
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            transport.upload_bundle(NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"])
        _, url, headers, _ = opener.requests[0]
        expected = base64.b64encode(b"example:hunter2").decode("ascii")
        self.assertEqual(headers["authorization"], f"Bearer {expected}")
        self.assertNotIn("hunter2", url)

    def test_a_user_token_is_used_directly(self):
        transport = maven_publish.HttpCentralTransport(
            {"CENTRAL_TOKEN": "user-token"},
            settings=central_settings(
                token_secret="CENTRAL_TOKEN", username_secret="", password_secret=""
            ),
        )
        opener = RecordingUrlopen([FakeResponse(200, b"deployment-001")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            transport.upload_bundle(
                NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"]
            )
        # A job that already holds a Portal user token uses it rather than
        # reassembling one from a password it may not have.
        self.assertEqual(opener.requests[0][2]["authorization"], "Bearer user-token")

    def test_no_credential_is_refused_before_anything_is_sent(self):
        transport, opener = self.transport([FakeResponse(200)], {})
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenError) as caught:
                transport.upload_bundle(NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"])
        self.assertEqual(opener.requests, [])
        self.assertIn(USERNAME_SECRET, str(caught.exception))

    def test_validation_errors_keyed_by_file_are_readable(self):
        payload = json.dumps(
            {
                "deploymentState": "FAILED",
                "errors": {"widgets-1.4.0.jar": "no signature found"},
            }
        ).encode()
        transport, opener = self.transport([FakeResponse(200, payload)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            state = transport.status("deployment-001")
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.errors, ("widgets-1.4.0.jar: no signature found",))

    def test_validation_errors_as_a_list_of_objects_are_readable(self):
        payload = json.dumps(
            {
                "deploymentState": "FAILED",
                "errors": [{"locator": "widgets-1.4.0.pom", "message": "missing licence"}],
            }
        ).encode()
        transport, opener = self.transport([FakeResponse(200, payload)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            state = transport.status("deployment-001")
        self.assertEqual(state.errors, ("widgets-1.4.0.pom: missing licence",))

    def test_a_refused_token_names_what_it_wants(self):
        transport, opener = self.transport([http_error(401, b"nope")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError) as caught:
                transport.status("deployment-001")
        self.assertIn("user", str(caught.exception))

    def test_an_already_published_version_is_reported_as_immutable(self):
        transport, opener = self.transport([http_error(409, b"already published")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError) as caught:
                transport.upload_bundle(NAMESPACE, PUBLISHING_AUTOMATIC, "w.zip", self.library.paths[".jar"])
        self.assertTrue(caught.exception.version_exists)

    def test_dropping_a_deployment_that_is_already_gone_is_the_outcome_we_wanted(self):
        transport, opener = self.transport([http_error(404, b"gone")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            transport.drop("deployment-001")

    def test_dropping_a_deployment_deletes_it(self):
        transport, opener = self.transport([FakeResponse(204)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            transport.drop("deployment-001")
        method, url, _, _ = opener.requests[0]
        self.assertEqual(method, "DELETE")
        self.assertIn("/api/v1/publisher/deployment/deployment-001", url)

    def test_promoting_a_deployment_posts_to_its_deployment_path(self):
        transport, opener = self.transport([FakeResponse(201)])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            transport.publish("deployment-001")
        method, url, _, _ = opener.requests[0]
        self.assertEqual(method, "POST")
        self.assertIn("/api/v1/publisher/deployment/deployment-001", url)

    def test_an_unreachable_portal_is_retryable(self):
        transport, opener = self.transport([urllib.error.URLError("no route to host")])
        import unittest.mock as mock

        with mock.patch.object(maven_publish.urllib.request, "urlopen", opener):
            with self.assertRaises(MavenApiError) as caught:
                transport.status("deployment-001")
        self.assertTrue(caught.exception.retryable)


class TransportContractTests(PublisherTestCase):
    """A publisher is only as honest as the transport it is handed.

    A transport that cannot answer one of the calls the publisher makes fails
    later and further away -- a confirm stage that never learns whether a
    version was already published is a publisher that re-uploads. The refusal
    belongs at construction, where the cause is the object in hand.
    """

    def test_a_registry_transport_that_cannot_look_up_is_refused(self):
        class Blind:
            def upload(self, url: str, name: str, path: str) -> Any:
                raise AssertionError("not reached")

        with self.assertRaises(MavenError) as caught:
            maven_publish.require_registry_transport(Blind())
        self.assertIn("file_exists", str(caught.exception))

    def test_a_portal_transport_that_cannot_report_state_is_refused(self):
        class Blind:
            def upload_bundle(self, namespace: str, publishing_type: str, name: str, path: str) -> str:
                raise AssertionError("not reached")

        with self.assertRaises(MavenError) as caught:
            maven_publish.require_central_transport(Blind())
        self.assertIn("status", str(caught.exception))

    def test_a_publisher_is_built_over_an_incomplete_transport_and_says_so(self):
        class Blind:
            def upload(self, url: str, name: str, path: str) -> Any:
                raise AssertionError("not reached")

        with self.assertRaises(MavenError) as caught:
            GitHubPackagesPublisher(github_settings(), Blind())
        self.assertEqual(caught.exception.code, "transport-incomplete")

    def test_a_central_publisher_needs_a_transport_that_can_ask_for_state(self):
        class Blind:
            def upload_bundle(self, namespace: str, publishing_type: str, name: str, path: str) -> str:
                raise AssertionError("not reached")

        with self.assertRaises(MavenError) as caught:
            MavenCentralPublisher(central_settings(), Blind())
        self.assertEqual(caught.exception.code, "transport-incomplete")

    def test_the_intent_names_what_would_be_written_and_with_what(self):
        github = self.make_github()[0]
        self.assertIn("com.example:widgets", github.intent())
        self.assertIn(TOKEN_SECRET, github.intent())
        self.assertIn("no other permission", github.intent())

    def test_a_central_intent_names_the_policy_it_would_follow(self):
        automated = self.make_central()[0]
        self.assertIn("as soon as validation passes", automated.intent())
        manual = self.make_central(
            central_settings(publishing_type=PUBLISHING_USER_MANAGED)
        )[0]
        self.assertIn("leave it for a human", manual.intent())
        promoted = self.make_central(
            central_settings(
                publishing_type=PUBLISHING_USER_MANAGED, promote_validated=True
            )
        )[0]
        self.assertIn("promote the validated deployment", promoted.intent())


class LibraryFileSelectionTests(PublisherTestCase):
    def test_only_coordinate_bearing_files_are_addressable(self):
        names = {item.name for item in maven_publish.library_files(self.library.request())}
        self.assertEqual(
            names,
            {
                f"{ARTIFACT}-{VERSION}.jar",
                f"{ARTIFACT}-{VERSION}.pom",
                f"{ARTIFACT}-{VERSION}-sources.jar",
                f"{ARTIFACT}-{VERSION}-javadoc.jar",
            },
        )

    def test_a_distribution_is_not_a_repository_file(self):
        self.assertNotIn(
            f"{ARTIFACT}-{VERSION}-all.zip",
            {item.name for item in maven_publish.library_files(self.library.request())},
        )

    def test_a_library_reads_its_pom_metadata(self):
        library, problems = maven_publish.read_library(
            self.library.request(), coordinates(), require_signatures=True
        )
        self.assertEqual(problems, ())
        assert library.metadata is not None
        self.assertEqual(library.metadata.group_id, GROUP)
        self.assertEqual(library.metadata.artifact_id, ARTIFACT)
        self.assertEqual(library.metadata.version, VERSION)

    def test_a_library_with_two_main_jars_is_refused(self):
        request = self.library.request()
        extra = os.path.join(self.root, "build", "libs", f"{ARTIFACT}-shaded-{VERSION}.jar")
        with open(extra, "wb") as handle:
            handle.write(b"shaded")
        builder = ManifestBuilder(target="jvm", adapter="jvm", source_sha=SHA, version=VERSION)
        for item in request.manifests[0].artifacts:
            builder.replace(item)
        builder.record(
            name=os.path.basename(extra),
            path=extra,
            type=TYPE_PACKAGE,
            classifier=maven_publish.CLASSIFIER_MAIN,
            media_type=jvm.MEDIA_TYPE_JAR,
        )
        doubled = PublishRequest(
            destination="maven",
            tag=f"v{VERSION}",
            version=VERSION,
            source_sha=SHA,
            manifests=(builder.build(),),
            key="publish-jvm",
        )
        library, problems = maven_publish.read_library(doubled, coordinates(), require_signatures=False)
        self.assertTrue(any("exactly one main artifact" in problem for problem in problems), problems)


class BundleDeterminismTests(PublisherTestCase):
    def test_a_bundle_is_a_pure_function_of_the_library(self):
        library, problems = maven_publish.read_library(
            self.library.request(), coordinates(), require_signatures=True
        )
        self.assertEqual(problems, ())
        first = write_bundle(os.path.join(self.root, "one.zip"), library)
        second = write_bundle(os.path.join(self.root, "two.zip"), library)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.entries, second.entries)

    def test_a_bundle_reports_what_it_holds(self):
        library, _ = maven_publish.read_library(self.library.request(), coordinates())
        info = write_bundle(os.path.join(self.root, "bundle.zip"), library)
        self.assertGreater(info.size, 0)
        self.assertTrue(info.sha256)
        with zipfile.ZipFile(info.path) as archive:
            self.assertEqual(sorted(archive.namelist()), sorted(info.entries))


if __name__ == "__main__":
    unittest.main()
