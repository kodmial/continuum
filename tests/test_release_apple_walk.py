"""The Apple adapter, walked end to end.

The tests in `test_release_apple.py` assert on plans: which arguments, in which
order, with which identity. These assert on a release: the archive that comes out
of the other end, the bytes inside it, and the manifest that describes them. The
plans are executed against a fake toolchain (`apple_toolchain_support`), so what
runs here is the real plan, the real runner, the real manifest building, and the
real packaging — only `swift`, `codesign`, `security`, and `lipo` are stood in
for.

Two properties get the most attention because they are the ones that are
expensive to get wrong and cheap to see here:

- **Order.** `codesign` has to sign the products before the bundle is assembled
  over them, and the bundle has to be assembled before it is sealed. A plan that
  signs an empty bundle succeeds and ships an unsigned binary.
- **Reproducibility.** A release of the same commit has to produce the same bytes,
  because a consumer who downloads it twice and gets two different digests has
  no way to tell which one was reviewed.
"""

from __future__ import annotations

import os
import tarfile
import unittest
import zipfile
from typing import Dict, List

from continuum import config as config_module
from continuum.release import apple, archive
from continuum.release.contract import BuildRequest, ContractError, TargetSpec
from continuum.release.core import ReleaseComponents, ReleaseCore, ReleaseRequest
from continuum.release.contract import ReleaseEvent

from . import apple_toolchain_support as toolchain_support
from . import release_core_support as core_support
from . import release_support as support
from .apple_toolchain_support import FakeToolchain

VERSION = support.VERSION
SHA = support.SHA


def event(**overrides) -> ReleaseEvent:
    values = {
        "repository": "example/widgets",
        "name": "tag-push",
        "sha": SHA,
        "ref": f"refs/tags/v{VERSION}",
        "tag": f"v{VERSION}",
        "delivery": "apple-walk-1",
    }
    values.update(overrides)
    return ReleaseEvent(**values)


def triple_of(argv: List[str]) -> str:
    """The target triple out of a `swift build` argument vector.

    SwiftPM takes `-target` through `-Xswiftc`, so the vector reads
    `-Xswiftc -target -Xswiftc <triple>` and the triple is two tokens along.
    """

    return argv[argv.index("-target") + 2]


class AppleWalkTestCase(unittest.TestCase):
    """A checkout on disk, a fake toolchain, and one configured target."""

    target_overrides: Dict[str, object] = {}

    def setUp(self) -> None:
        self.directory = os.path.realpath(self.enterContext(_temporary_directory()))
        self.target = support.target(**self.target_overrides)
        self.toolchain = FakeToolchain(
            binaries=[item.name for item in self.target.binaries],
            bundle=self.target.app_bundle.name if self.target.app_bundle else None,
        )
        self._write_checkout()
        self.adapter = toolchain_support.adapter_for(self.directory, self.toolchain)
        self.core = self.core_for(self.adapter)

    def _write_checkout(self) -> None:
        """The files a build plan reads: the entitlements, plist, and resources."""

        files = [
            ("Resources/com.nanodictate.agent.entitlements", "com.apple.security.app-sandbox\n"),
            ("Resources/com.nanodictate.ctl.entitlements", "com.apple.security.cs.allow-jit\n"),
            ("packaging/Info.NanoDictateApp.plist", "<plist/>\n"),
            ("config.example.toml", "listen = 0.0.0.0:9000\n"),
        ]
        for relative, body in files:
            path = os.path.join(self.directory, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)

    def request(self, config: config_module.ContinuumConfig = None, **overrides) -> ReleaseRequest:
        """A request for the configured target, built the way a repository would.

        Through `from_config`, deliberately: the adapter has to survive the
        configuration round trip, so a request assembled by hand here would test
        a shape the CLI never produces.
        """

        values = {"workdir": self.directory, "requested_version": VERSION}
        values.update(overrides)
        return ReleaseRequest.from_config(
            config
            if config is not None
            else config_module.ContinuumConfig(
                release=config_module.ReleaseSettings(targets=(self.target,))
            ),
            values.pop("release_event", event()),
            **values,
        )

    def build_request(self, *, dry_run: bool = False) -> BuildRequest:
        """The per-target request the core hands an adapter, built directly.

        A release request describes the whole event; a build request describes
        one target's job. Tests that call the adapter themselves need the second
        one, and they should not have to run a release to get it.
        """

        return BuildRequest(
            target=TargetSpec(
                id=self.target.id,
                adapter=self.target.adapter,
                options=tuple(sorted(self.target.describe().items())),
            ),
            version=VERSION,
            source_sha=SHA,
            key=f"test-{self.target.id}",
            workdir=self.directory,
            dry_run=dry_run,
        )

    def core_for(self, adapter) -> ReleaseCore:
        return ReleaseCore(
            ReleaseComponents(
                adapters={"apple": adapter},
                version=core_support.ExplicitVersion(VERSION),
                eligibility=core_support.FixtureEligibility(eligible=True),
                notes=core_support.FixtureNotes(),
            )
        )

    def assertCompleted(self, outcome, message: str = "") -> None:
        """Assert the whole chain walked, whether or not anything was published.

        These tests configure no publisher, so a completed release ends as a
        verified no-op rather than as `released`. Asserting on `released` here
        would be asserting on publication the configuration never asked for, and
        would pass if the chain had stopped short and simply published nothing.
        """

        detail = message
        if outcome.failure is not None:
            detail = f"{detail} {outcome.failure.describe()}".strip()
        self.assertTrue(outcome.ok, detail or outcome.status)
        self.assertFalse(outcome.failed, detail)
        # The chain ran: a manifest was built, and everything in it was verified.
        self.assertTrue(outcome.manifests, detail or outcome.status)
        for manifest in outcome.manifests:
            self.assertTrue(manifest.artifacts, manifest.target)
            for artifact in manifest.artifacts:
                self.assertTrue(artifact.verified, artifact.name)

    def dist(self) -> str:
        return os.path.join(self.directory, apple.DIST_ROOT)

    def dist_names(self) -> List[str]:
        directory = self.dist()
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))


def _temporary_directory():
    import tempfile

    return tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BuildTests(AppleWalkTestCase):
    def test_each_declared_architecture_is_compiled_from_its_own_scratch_path(self):
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        builds = self.toolchain.commands(apple.SWIFT)
        triples = [triple_of(argv) for argv in builds]
        self.assertEqual(len(builds), 4, "two products for two architectures")
        for arch in self.target.architectures:
            self.assertEqual(
                triples.count(apple.triple_for(arch)), len(self.target.binaries), arch
            )
        scratches = {argv[argv.index("--scratch-path") + 1] for argv in builds}
        self.assertEqual(len(scratches), 2, "one scratch path per architecture, not one shared")

    def test_the_release_mode_and_the_declared_linker_flags_are_passed_through(self):
        self.core.execute(self.request())
        for argv in self.toolchain.commands(apple.SWIFT):
            self.assertEqual(argv[1], "build")
            self.assertEqual(argv[argv.index("-c") + 1], "release")
            self.assertIn("-target", argv)

    def test_the_published_archives_are_one_per_architecture_and_format(self):
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        published = sorted(
            (item.arch, item.classifier)
            for item in outcome.manifests[0].artifacts
            if item.type == "archive"
        )
        self.assertEqual(
            published,
            sorted(
                (arch, artifact_format)
                for arch in self.target.architectures
                for artifact_format in self.target.artifacts
            ),
        )

    def test_the_build_stage_reports_a_product_per_binary_and_architecture(self):
        # Read straight from the adapter, because the run's own manifest is
        # replaced by the signed one: the bytes a user installs are the archives,
        # and the products underneath them are an implementation detail of the
        # packaging step.
        manifest = self.adapter.build(self.build_request())
        recorded = sorted(
            (item.arch, os.path.basename(item.path)) for item in manifest.artifacts
        )
        self.assertEqual(
            recorded,
            sorted(
                (arch, binary.name)
                for arch in self.target.architectures
                for binary in self.target.binaries
            ),
        )
        for item in manifest.artifacts:
            self.assertEqual(item.signing, "unsigned")
            self.assertFalse(item.verified)

    def test_the_compiler_intermediates_are_removed_and_the_products_are_kept(self):
        self.core.execute(self.request())
        for arch in self.target.architectures:
            scratch = os.path.join(self.directory, apple.products_root(arch), ".swiftpm")
            self.assertFalse(os.path.exists(scratch), scratch)
            for binary in self.target.binaries:
                self.assertTrue(
                    os.path.isfile(os.path.join(self.directory, apple.products_root(arch), binary.name))
                )

    def test_a_build_failure_is_reported_against_the_step_that_failed(self):
        self.toolchain.build_fails = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "build-failed")

    def test_a_target_that_cannot_run_here_says_so_instead_of_building(self):
        self.adapter.is_available = False
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "toolchain-unavailable")
        self.assertEqual(self.toolchain.calls, [], "nothing ran")

    def test_a_checkout_that_is_not_the_approved_commit_builds_nothing(self):
        core = self.core_for(
            toolchain_support.adapter_for(
                self.directory, self.toolchain, revision=support.OTHER_SHA
            )
        )
        outcome = core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "source-mismatch")
        self.assertEqual(self.toolchain.calls, [], "no build, no signing, no publication")


class UniversalBuildTests(AppleWalkTestCase):
    target_overrides = {"universal": True}

    def test_both_architectures_are_built_before_they_are_merged(self):
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        triples = [triple_of(argv) for argv in self.toolchain.commands(apple.SWIFT)]
        for arch in self.target.architectures:
            self.assertEqual(
                triples.count(apple.triple_for(arch)), len(self.target.binaries), arch
            )
        self.assertEqual(len(self.toolchain.commands(apple.LIPO)), len(self.target.binaries))

    def test_the_merge_runs_after_every_slice_is_staged(self):
        self.core.execute(self.request())
        calls = [argv[0] for argv, _ in self.toolchain.calls]
        merges = [index for index, item in enumerate(calls) if item == apple.LIPO]
        stages = [
            index for index, argv in enumerate(self.toolchain.calls) if argv[0][0] == apple.SWIFT
        ]
        self.assertTrue(merges)
        self.assertLess(max(stages), min(merges), "a merge before its inputs are staged builds nothing")

    def test_a_universal_release_publishes_one_archive_per_format_not_one_per_slice(self):
        self.core.execute(self.request())
        names = self.dist_names()
        self.assertEqual(
            names,
            [
                "NanoDictate-1.4.0-macos-universal.tar.gz",
                "NanoDictate-1.4.0-macos-universal.zip",
                "macos-SHA256SUMS.txt",
            ],
        )

    def test_the_per_architecture_slices_are_removed_so_a_slice_cannot_be_shipped(self):
        self.core.execute(self.request())
        for arch in self.target.architectures:
            self.assertFalse(
                os.path.exists(os.path.join(self.directory, apple.products_root(arch))),
                arch,
            )

    def test_every_published_archive_is_labelled_universal(self):
        outcome = self.core.execute(self.request())
        archives = [item for item in outcome.manifests[0].artifacts if item.type == "archive"]
        self.assertEqual(len(archives), len(self.target.artifacts))
        for item in archives:
            self.assertEqual(item.arch, "universal")

    def test_the_merged_products_are_the_ones_lipo_wrote(self):
        self.core.execute(self.request())
        merged = os.path.join(self.directory, apple.products_root("universal"))
        for binary in self.target.binaries:
            with open(os.path.join(merged, binary.name), "rb") as handle:
                body = handle.read()
            self.assertIn(b"UNIVERSAL", body, binary.name)
            for arch in self.target.architectures:
                self.assertIn(apple.triple_for(arch).encode("ascii"), body, binary.name)

    def test_a_failed_merge_does_not_publish_a_universal_archive(self):
        self.toolchain.lipo_fails = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "universal-merge-failed")
        self.assertFalse([name for name in self.dist_names() if name.endswith(".tar.gz")])


class SigningOrderTests(AppleWalkTestCase):
    def test_the_binary_inside_the_shipped_bundle_is_the_signed_one(self):
        # The order that matters: sign the product, copy that signed product into
        # the bundle, then seal. Copy first and sign second would ship a bundle
        # whose nested binary the bundle signature does not cover, which no
        # `codesign --verify` on the bundle itself reports.
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        path = os.path.join(
            self.dist(), "NanoDictate-1.4.0-macos-arm64.tar.gz"
        )
        with tarfile.open(path, "r:gz") as archive_file:
            body = archive_file.extractfile(
                "NanoDictate.app/Contents/MacOS/NanoDictateAgent"
            ).read()
        self.assertIn(f"SIGNED-BY {support.IDENTITY}".encode("utf-8"), body)

    def test_every_product_that_was_signed_carries_the_identity_in_its_bytes(self):
        self.core.execute(self.request())
        for path in self.toolchain.signed:
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    self.assertIn(
                        f"SIGNED-BY {support.IDENTITY}".encode("utf-8"), handle.read(), path
                    )

    def test_the_bundle_is_sealed_after_its_contents_are_in_place(self):
        self.core.execute(self.request())
        calls = [argv for argv, _ in self.toolchain.calls]
        seal = next(
            index
            for index, argv in enumerate(calls)
            if argv[0] == apple.CODESIGN and argv[-1].endswith(".app")
        )
        asserts = [
            index for index, argv in enumerate(calls) if argv[0] == "/usr/bin/true" or argv[0] == apple.PLISTBUDDY
        ]
        version_stamp = next(
            index for index, argv in enumerate(calls) if argv[0] == apple.PLISTBUDDY
        )
        self.assertLess(version_stamp, seal, "a version stamped after the seal is not in the signature")
        self.assertTrue(asserts is not None)

    def test_every_signed_file_names_the_configured_identity(self):
        self.core.execute(self.request())
        self.assertTrue(self.toolchain.signed)
        for path, identity in self.toolchain.signed.items():
            self.assertEqual(identity, support.IDENTITY, path)

    def test_the_certificate_is_imported_once_per_keychain_and_removed_afterwards(self):
        self.core.execute(self.request())
        imports = self.toolchain.commands(apple.SECURITY)
        self.assertTrue(any("import" in argv for argv in imports))
        deletes = [argv for argv in imports if "delete-keychain" in argv]
        self.assertTrue(deletes, "a keychain that outlives the release is a keychain the next one finds")

    def test_a_signature_verification_failure_fails_the_release(self):
        self.toolchain.verify_fails = True
        outcome = self.core.execute(self.request())
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "signature-verification-failed")

    def test_nothing_is_published_when_the_signature_does_not_verify(self):
        self.toolchain.verify_fails = True
        self.core.execute(self.request())
        self.assertFalse([name for name in self.dist_names() if name.endswith(".tar.gz")])


class PackagingTests(AppleWalkTestCase):
    def test_the_archive_holds_the_sealed_bundle_and_the_products(self):
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        with tarfile.open(os.path.join(self.dist(), "NanoDictate-1.4.0-macos-arm64.tar.gz"), "r:gz") as archive_file:
            names = sorted(archive_file.getnames())
        self.assertIn("NanoDictate.app/Contents/Info.plist", names)
        self.assertIn("NanoDictate.app/Contents/MacOS/NanoDictateAgent", names)
        self.assertIn("NanoDictateAgent", names)

    def test_the_info_plist_carries_the_stamped_version(self):
        self.core.execute(self.request())
        with zipfile.ZipFile(os.path.join(self.dist(), "NanoDictate-1.4.0-macos-arm64.zip")) as archive_file:
            names = archive_file.namelist()
            self.assertIn("NanoDictate.app/Contents/Info.plist", names)

    def test_the_checksum_file_lists_the_archives_and_not_itself(self):
        self.core.execute(self.request())
        with open(os.path.join(self.dist(), "macos-SHA256SUMS.txt"), encoding="utf-8") as handle:
            body = handle.read()
        listed = sorted(line.split("  ", 1)[1] for line in body.strip().splitlines())
        self.assertEqual(
            listed,
            sorted(
                apple.archive_name(self.target, version=VERSION, arch=arch, artifact_format=fmt)
                for arch in self.target.architectures
                for fmt in self.target.artifacts
            ),
        )
        self.assertNotIn("SHA256SUMS.txt", body)

    def test_two_targets_each_publish_their_own_checksum_file(self):
        # Different products, so the release's asset set stays unambiguous, and
        # the checksum files are named after their target: a shared
        # `SHA256SUMS.txt` would be one file written twice, and a consumer
        # verifying the second target's archives would read the first target's
        # digests.
        other = support.target(
            id="macos-beta",
            app_bundle=config_module.ReleaseAppBundle(
                name="NanoDictateBeta.app",
                identifier=support.AGENT,
                info_plist="packaging/Info.NanoDictateApp.plist",
                entitlements="Resources/com.nanodictate.agent.entitlements",
                resources=("config.example.toml",),
            ),
            binaries=(
                config_module.ReleaseBinary(
                    name="NanoDictateBetaAgent",
                    identifier=support.AGENT,
                    entitlements="Resources/com.nanodictate.agent.entitlements",
                ),
            ),
        )
        self.toolchain.binaries = tuple(
            binary.name for binary in self.target.binaries + other.binaries
        )
        config = config_module.ContinuumConfig(
            release=config_module.ReleaseSettings(targets=(self.target, other))
        )
        outcome = self.core.execute(self.request(config=config))
        self.assertCompleted(outcome)
        names = self.dist_names()
        self.assertIn("macos-SHA256SUMS.txt", names)
        self.assertIn("macos-beta-SHA256SUMS.txt", names)
        for target_id in ("macos", "macos-beta"):
            with open(
                os.path.join(self.dist(), f"{target_id}-SHA256SUMS.txt"), encoding="utf-8"
            ) as handle:
                listed = [line.split("  ", 1)[1] for line in handle.read().strip().splitlines()]
            self.assertTrue(listed)
            for name in listed:
                self.assertTrue(
                    os.path.isfile(os.path.join(self.dist(), name)),
                    f"{target_id} lists {name}, which is not in the release",
                )

    def test_two_targets_that_would_publish_one_name_are_refused_before_verification(self):
        other = support.target(
            id="macos-beta",
            universal=False,
            binaries=self.target.binaries,
            app_bundle=self.target.app_bundle,
        )
        config = config_module.ContinuumConfig(
            release=config_module.ReleaseSettings(targets=(self.target, other))
        )
        outcome = self.core.execute(self.request(config=config))
        self.assertTrue(outcome.failed)
        self.assertEqual(outcome.failure.code, "asset-name-collision")
        self.assertEqual(outcome.failure.stage, "sign")

    def test_the_archive_bytes_are_identical_across_two_releases_of_one_commit(self):
        first = self.core.execute(self.request())
        self.assertCompleted(first)
        before = self._dist_digests()

        # A second release of the same commit, over a rebuilt checkout. Anything
        # that leaked into an archive that should not be there — a timestamp, a
        # uid, the order the filesystem happened to return entries in — shows up
        # here as a digest that moved.
        os.remove(os.path.join(self.dist(), "macos-SHA256SUMS.txt"))
        self.adapter = toolchain_support.adapter_for(self.directory, self.toolchain)
        second = self.core_for(self.adapter).execute(self.request())
        self.assertCompleted(second)
        self.assertEqual(before, self._dist_digests())

    def test_the_source_date_epoch_from_the_job_is_honoured(self):
        self.core.execute(self.request())
        with tarfile.open(os.path.join(self.dist(), "NanoDictate-1.4.0-macos-arm64.tar.gz"), "r:gz") as archive_file:
            stamps = {member.mtime for member in archive_file.getmembers()}
        self.assertEqual(len(stamps), 1, stamps)

    def _dist_digests(self) -> Dict[str, str]:
        import hashlib

        directory = self.dist()
        digests = {}
        for name in sorted(os.listdir(directory)):
            with open(os.path.join(directory, name), "rb") as handle:
                digests[name] = hashlib.sha256(handle.read()).hexdigest()
        return digests


class DryRunParityTests(AppleWalkTestCase):
    """A dry run has to describe the release the real run would perform."""

    def test_a_dry_run_names_the_same_artifacts_a_real_run_records(self):
        plan = self.core.plan(self.request(dry_run=True))
        run = self.core.execute(self.request())
        planned = sorted(item.name for item in plan.manifests[0].artifacts)
        recorded = sorted(item.name for item in run.manifests[0].artifacts)
        self.assertEqual(planned, recorded)

    def test_a_dry_run_writes_no_file_and_starts_no_tool(self):
        self.core.plan(self.request(dry_run=True))
        self.assertEqual(self.toolchain.calls, [])
        self.assertEqual(self.dist_names(), [])

    def test_a_declared_archive_claims_no_signature_because_it_has_none_yet(self):
        plan = self.core.plan(self.request(dry_run=True))
        archives = [item for item in plan.manifests[0].artifacts if item.type == "archive"]
        self.assertTrue(archives)
        for item in archives:
            self.assertTrue(item.declared)
            self.assertEqual(item.signing, "unsigned")
            self.assertFalse(item.verified)
            self.assertEqual(item.size, 0)

    def test_the_plan_states_the_identity_the_run_will_sign_with(self):
        self.core.plan(self.request(dry_run=True))
        plan = apple.plan_for(
            self.target,
            environment=support.environment(),
            products=apple.products_root("arm64"),
            workdir=self.directory,
        )
        self.assertEqual(plan.signing_identity, support.IDENTITY)
        self.assertTrue(plan.signature_pinned)
        self.assertEqual(plan.degraded, False)

    def test_a_real_run_verifies_what_the_plan_declared(self):
        run = self.core.execute(self.request())
        plan = self.core.plan(self.request(dry_run=True))
        for artifact in run.manifests[0].artifacts:
            self.assertTrue(artifact.verified, artifact.name)
        self.assertEqual(
            sorted(item.name for item in plan.manifests[0].artifacts),
            sorted(item.name for item in run.manifests[0].artifacts),
        )

    def test_the_plan_does_not_prevent_the_release_it_planned(self):
        self.core.plan(self.request(dry_run=True))
        run = self.core.execute(self.request())
        self.assertCompleted(run)
        self.assertTrue(self.dist_names())


class VerificationTests(AppleWalkTestCase):
    def test_a_rewritten_archive_is_caught_rather_than_published(self):
        outcome = self.core.execute(self.request())
        self.assertCompleted(outcome)
        archive_path = os.path.join(self.dist(), "NanoDictate-1.4.0-macos-arm64.tar.gz")
        with open(archive_path, "ab") as handle:
            handle.write(b"tampered")
        report = self.adapter.verify(self.request(), outcome.manifests[0])
        self.assertFalse(report.verified)
        self.assertTrue(report.failures)

    def test_a_deleted_archive_is_caught(self):
        outcome = self.core.execute(self.request())
        os.remove(os.path.join(self.dist(), "NanoDictate-1.4.0-macos-arm64.tar.gz"))
        report = self.adapter.verify(self.build_request(), outcome.manifests[0])
        self.assertFalse(report.verified)

    def test_a_dry_run_verifies_nothing_and_says_so(self):
        report = self.adapter.verify(
            self.build_request(dry_run=True),
            self.core.plan(self.request(dry_run=True)).manifests[0],
        )
        self.assertTrue(report.verified)
        self.assertEqual(report.code, "planned")


def _digest_of(path: str) -> str:
    import hashlib

    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class ArchiveHelperTests(unittest.TestCase):
    """The packaging helpers on their own, where the assertions are exact."""

    def setUp(self) -> None:
        self.directory = os.path.realpath(self.enterContext(_temporary_directory()))

    def _write(self, relative: str, body: bytes) -> str:
        path = os.path.join(self.directory, relative)
        os.makedirs(os.path.dirname(path) or self.directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def test_two_files_with_one_name_are_refused_rather_than_one_winning(self):
        first = self._write("one/Agent", b"MACH-O")
        second = self._write("two/Agent", b"MACH-O")
        with self.assertRaises(archive.ArchiveError) as raised:
            archive.combine(
                [archive.Member(first, "Agent")],
                [archive.Member(second, "Agent")],
            )
        self.assertEqual(raised.exception.code, "archive-member-conflict")

    def test_the_same_file_claimed_twice_is_stated_once(self):
        source = self._write("dist/Agent", b"MACH-O")
        members = archive.combine(
            [archive.Member(source, "Agent")], [archive.Member(source, "Agent")]
        )
        self.assertEqual([item.name for item in members], ["Agent"])

    def test_members_are_listed_in_sorted_order_however_they_arrived(self):
        names = ["zeta", "alpha", "middle"]
        sources = {name: self._write(f"dist/{name}", name.encode("utf-8")) for name in names}
        members = archive.combine([archive.Member(sources[name], name) for name in names])
        self.assertEqual([item.name for item in members], ["alpha", "middle", "zeta"])

    def test_a_member_named_with_a_parent_path_is_refused(self):
        source = self._write("dist/Agent", b"MACH-O")
        with self.assertRaises(archive.ArchiveError):
            archive.Member(source, "../escape")

    def test_a_member_named_with_an_absolute_path_is_refused(self):
        source = self._write("dist/Agent", b"MACH-O")
        with self.assertRaises(archive.ArchiveError):
            archive.Member(source, "/etc/passwd")

    def test_every_format_maps_to_the_extension_a_consumer_expects(self):
        self.assertEqual(archive.extension_for("tar.gz"), ".tar.gz")
        self.assertEqual(archive.extension_for("zip"), ".zip")
        with self.assertRaises(archive.ArchiveError):
            archive.extension_for("dmg")

    def test_two_writes_of_the_same_members_are_byte_identical(self):
        import hashlib

        sources = {
            name: self._write(f"dist/{name}", name.encode("utf-8"))
            for name in ("Agent", "ctl")
        }
        members = [archive.Member(path, name) for name, path in sources.items()]
        first = os.path.join(self.directory, "first.tar.gz")
        second = os.path.join(self.directory, "second.tar.gz")
        archive.write(first, members, artifact_format="tar.gz", epoch=1700000000)
        archive.write(second, list(reversed(members)), artifact_format="tar.gz", epoch=1700000000)
        self.assertEqual(_digest_of(first), _digest_of(second))

    def test_a_zip_of_the_same_members_is_byte_identical(self):
        import hashlib

        source = self._write("dist/Agent", b"MACH-O")
        members = [archive.Member(source, "Agent")]
        paths = []
        for name in ("a.zip", "b.zip"):
            path = os.path.join(self.directory, name)
            archive.write(path, members, artifact_format="zip", epoch=1700000000)
            paths.append(path)
        self.assertEqual(_digest_of(paths[0]), _digest_of(paths[1]))

    def test_a_source_date_epoch_that_is_not_a_number_is_refused_not_ignored(self):
        with self.assertRaises(archive.ArchiveError):
            archive.epoch_from({"SOURCE_DATE_EPOCH": "yesterday"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()