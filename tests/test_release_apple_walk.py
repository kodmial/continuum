"""The Apple target walk end to end, on a runner with no macOS toolchain.

`tests/test_release_apple.py` reads the plan. This file runs it. The toolchain
is replaced — see `tests/apple_toolchain_support.py` for exactly what is and is
not faked — and nothing else is: the same `ReleasePlan` goes through the same
`Runner`, and the assertions are made against the bytes that landed on disk and
the manifest that came back.

The properties worth pinning are the ones a release is trusted for:

- the bundle that ships holds the *signed* binary, which is only observable by
  looking inside the archive rather than at the command log;
- the seal covers finished content, and verification re-reads the files;
- the archives are reproducible, because a consumer verifies a digest they
  recorded from an earlier run of the same commit;
- a dry run declares exactly what the real run writes, because a plan that
  describes a different release than the one that runs is not a plan.
"""

from __future__ import annotations

import os
import tarfile
import unittest
import zipfile
from typing import Dict, List, Tuple

from continuum import config as config_module
from continuum.release import archive as archive_module
from continuum.release import apple, contract
from continuum.release.contract import BuildRequest, TargetSpec

from . import apple_toolchain_support as fake
from . import release_core_support as core
from . import release_support as support


class AppleWalkTestCase(unittest.TestCase):
    """A repository to build, a toolchain that records, and one target."""

    def setUp(self) -> None:
        import tempfile

        self.workdir = tempfile.mkdtemp(prefix="continuum-apple-")
        self.addCleanup(self._discard)
        self.dist = os.path.join(self.workdir, "dist")
        os.makedirs(self.dist, exist_ok=True)
        self.toolchain = fake.FakeToolchain(
            binaries=[binary.name for binary in support.binaries()],
            bundle=support.BUNDLE,
            info_plist=support.app_bundle().info_plist,
            resources=support.app_bundle().resources,
        )
        # The job environment carries the certificate under the name the target
        # configures; the adapter is what joins that to the name its plan reads.
        self.environment = dict(
            support.environment(),
            PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        )

    def _discard(self) -> None:
        import shutil

        shutil.rmtree(self.workdir, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------

    def repository(self, *extra: Tuple[str, str]) -> None:
        """The files a SwiftPM release reads: bundle, plist, resources."""

        written = [
            (os.path.join("Sources", binary.name, "main.swift"), f"// {binary.name}\n")
            for binary in support.binaries()
        ]
        written.append(("Package.swift", "// swift-tools-version:5.9\n"))
        written.append(
            (
                support.app_bundle().info_plist,
                "CFBundleIdentifier = placeholder\nCFBundleShortVersionString = 0.0.0\n",
            )
        )
        written.extend(
            (support.app_bundle().entitlements, "<plist>entitlements</plist>\n")
            for _ in (1,)
        )
        written.extend(
            (
                binary.entitlements,
                f"<plist>com.nanodictate.{binary.identifier.rsplit('.', 1)[-1]}</plist>\n",
            )
            for binary in support.binaries()
            if binary.entitlements
        )
        written.extend((resource, f"{resource}\n") for resource in support.app_bundle().resources)
        written.extend(extra)
        for path, contents in written:
            self.write(path, contents)

    def write(self, path: str, contents: str) -> str:
        full = os.path.join(self.workdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(contents)
        return full

    def adapter(self, **overrides):
        return fake.adapter_for(
            self.workdir,
            self.toolchain,
            environment=self.environment,
            **overrides,
        )

    def request(self, target: config_module.ReleaseTarget = None, **overrides):
        """A build request for `target`, as the chain's orchestrator would make."""

        chosen = target if target is not None else support.target()
        values: Dict[str, object] = {
            "target": TargetSpec(
                id=chosen.id,
                adapter=chosen.adapter,
                options=tuple(sorted(chosen.describe().items())),
            ),
            "version": core.VERSION,
            "source_sha": core.SHA,
            "key": f"apple:{core.VERSION}",
            "workdir": self.workdir,
        }
        values.update(overrides)
        return BuildRequest(**values)

    def release(self, target: config_module.ReleaseTarget = None, adapter=None) -> Tuple[object, List[str]]:
        """Build, sign, verify — the three stages the chain calls, in order."""

        request = self.request(target)
        the_adapter = adapter if adapter is not None else self.adapter()
        manifest = the_adapter.build(request)
        signed = the_adapter.sign(request, manifest)
        report = the_adapter.verify(request, signed)
        self.assertTrue(
            report.verified, f"verification refused the release: {report.detail}"
        )
        return signed, [artifact.name for artifact in signed.artifacts]

    def artifact(self, manifest, name: str):
        """The manifest row named `name`, or a failure naming what it holds."""

        for row in manifest.artifacts:
            if row.name == name:
                return row
        raise AssertionError(
            f"{name} is not in the manifest; it holds {[row.name for row in manifest.artifacts]}"
        )

    def archive_named(self, name: str):
        path = os.path.join(self.dist, name)
        self.assertTrue(os.path.isfile(path), f"{name} was not published")
        return path

    def unpacked(self, name: str) -> Dict[str, bytes]:
        """The bytes inside an archive, keyed by member name."""

        path = self.archive_named(name)
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                return {item.filename: archive.read(item) for item in archive.infolist()}
        with tarfile.open(path, "r:gz") as archive:
            return {
                item.name: archive.extractfile(item).read()
                for item in archive.getmembers()
                if item.isfile()
            }


class BuildTests(AppleWalkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository()
        self.target = support.target()

    def test_builds_each_architecture_into_its_own_scratch_directory(self):
        manifest = self.adapter().build(self.request())
        self.assertEqual(
            [(artifact.arch, os.path.basename(artifact.path)) for artifact in manifest.artifacts],
            [
                ("x86_64", support.binaries()[0].name),
                ("x86_64", support.binaries()[1].name),
                ("arm64", support.binaries()[0].name),
                ("arm64", support.binaries()[1].name),
            ],
            "two binaries per architecture",
        )
        scratch = [
            argv[argv.index("--scratch-path") + 1]
            for argv in self.toolchain.commands(apple.SWIFT)
        ]
        self.assertEqual(len(scratch), 4, "two products per architecture")
        self.assertEqual(
            len(set(scratch)), 2, "two builds of one architecture share a scratch path"
        )
        for path in scratch:
            self.assertFalse(os.path.isabs(path), f"{path} is absolute")
            self.assertIn(apple.STAGE_ROOT, path)

    def test_compiles_for_the_architecture_it_declares(self):
        self.adapter().build(self.request())
        triples = []
        for argv in self.toolchain.commands(apple.SWIFT):
            triple = argv[argv.index("-target") + 2]
            if triple not in triples:
                triples.append(triple)
        self.assertEqual(
            triples,
            [
                f"x86_64-apple-macosx{apple.MACOS_FLOOR}",
                f"arm64-apple-macosx{apple.MACOS_FLOOR}",
            ],
        )

    def test_builds_in_the_configuration_a_release_is_cut_from(self):
        """A release is built in `release`, never in the debug default.

        A debug-configuration binary in a shipped archive is a release nobody
        chose, and it is invisible in the artifact name.
        """

        self.adapter().build(self.request())
        for argv in self.toolchain.commands(apple.SWIFT):
            self.assertIn("-c", argv)
            self.assertEqual(argv[argv.index("-c") + 1], "release")

    def test_the_manifest_records_the_products_it_wrote(self):
        manifest = self.adapter().build(self.request())
        for artifact in manifest.artifacts:
            self.assertTrue(
                os.path.isfile(artifact.path), f"{artifact.name} is recorded but not written"
            )
            self.assertTrue(artifact.digest, f"{artifact.name} has no digest")
        self.assertEqual(manifest.source_sha, core.SHA)
        self.assertEqual(manifest.adapter, "apple")

    def test_records_the_declaration_universal_builds_replace(self):
        """A universal target's published rows are the merged products.

        The per-architecture rows are inputs to `lipo` and are deleted once it
        has merged them, so declaring them would describe files the build
        deliberately removes.
        """

        target = support.target(universal=True, architectures=("x86_64", "arm64"))
        manifest = self.adapter().build(self.request(target))
        names = [artifact.name for artifact in manifest.artifacts]
        self.assertEqual(len(names), 2, "one merged product per binary")
        for name in names:
            self.assertTrue(os.path.isfile(self.artifact(manifest, name).path))

    def test_merges_the_slices_it_compiled_rather_than_copying_one(self):
        """The universal product has to be a merge, and the slices must be gone.

        A "universal" binary that is one architecture wearing the wrong name is
        the failure this proves is impossible: the product carries both compiled
        slices, and the per-architecture directories are removed afterwards so
        nothing can later sign a slice and ship it under the universal label.
        """

        target = support.target(universal=True, architectures=("x86_64", "arm64"))
        manifest = self.adapter().build(self.request(target))
        merges = self.toolchain.commands(apple.LIPO)
        self.assertEqual(len(merges), 2, "one merge per product")
        for argv in merges:
            inputs = argv[argv.index("-create") + 1 : argv.index("-output")]
            self.assertEqual(len(inputs), 2, f"{argv} merged {len(inputs)} slices")
            for item in inputs:
                self.assertTrue(
                    os.path.isabs(item) is False, f"{item} is absolute"
                )
        for artifact in manifest.artifacts:
            with open(artifact.path, "rb") as handle:
                body = handle.read()
            self.assertIn(b"x86_64-apple-macosx", body)
            self.assertIn(b"arm64-apple-macosx", body)
        for arch in ("x86_64", "arm64"):
            self.assertFalse(
                os.path.exists(os.path.join(self.workdir, apple.products_root(arch))),
                f"the {arch} slices are still on disk to be signed by mistake",
            )

    def test_refuses_an_architecture_the_toolchain_cannot_target(self):
        """Two refusals, both before a process starts.

        A target carrying an architecture the schema does not know is rejected
        as invalid configuration, and a plan asked for one directly is rejected
        as unexecutable. Neither builds half a release and reports a compiler
        error for a triple that was never valid.
        """

        target = support.target(architectures=("x86_64", "riscv64"))
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter().build(self.request(target))
        self.assertEqual(caught.exception.code, "target-options-invalid")
        self.assertIn("riscv64", str(caught.exception))
        with self.assertRaises(apple.AppleError) as triple:
            apple.triple_for("riscv64")
        self.assertEqual(triple.exception.code, "architecture-unsupported")
        self.assertIn("arm64", str(triple.exception))
        self.assertEqual(self.toolchain.calls, [])

    def test_classifies_a_build_failure_by_what_swift_said(self):
        self.toolchain.build_fails = True
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter().build(self.request())
        error = caught.exception
        self.assertEqual(error.code, "build-failed")
        self.assertIn("no such module", str(error))
        self.assertFalse(error.retryable)

    def test_says_so_when_this_machine_has_no_toolchain(self):
        """Planning a run is portable; asking for the toolchain is not.

        A reviewer on Linux must be able to read the plan for a macOS target, so
        the toolchain is only demanded by the job that is about to execute it —
        and then the refusal names the machine, not the target's configuration.
        """

        from continuum.release import adapters

        adapters.plan_for(self.target, environment=support.environment())
        with self.assertRaises(apple.TargetNotExecutable) as caught:
            adapters.plan_for(
                self.target,
                environment=support.environment(),
                require_toolchain=True,
            )
        self.assertIn("toolchain", str(caught.exception))
        with self.assertRaises(apple.TargetNotExecutable):
            apple.ensure_available()

    def test_refuses_a_checkout_that_is_not_the_approved_commit(self):
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter(revision="b" * 40).build(self.request())
        self.assertEqual(caught.exception.code, "source-mismatch")
        self.assertIn(core.SHA, str(caught.exception))

    def test_refuses_a_directory_with_no_readable_commit(self):
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter(revision="").build(self.request())
        self.assertEqual(caught.exception.code, "source-unreadable")


class ShippingOrderTests(AppleWalkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository()
        self.target = support.target()
        self.adapter_ = self.adapter()
        self.request_ = self.request()
        self.manifest = self.adapter_.build(self.request_)
        self.signed = self.adapter_.sign(self.request_, self.manifest)

    def signatures_for(self, arch: str) -> Tuple[List[int], List[int]]:
        """The indices of this architecture's binary signatures and bundle seals.

        Per architecture rather than overall, because each architecture is signed
        by its own plan: one target's seals do not have to wait for another
        target's signatures, and an ordering assertion that compared them would
        be measuring the order targets happened to be dispatched in.
        """

        binaries: List[int] = []
        seals: List[int] = []
        for index, (argv, _workdir) in enumerate(self.toolchain.calls):
            if argv[0] != apple.CODESIGN or "--sign" not in argv:
                continue
            if f"release/{arch}/" not in argv[-1] + " ":
                continue
            (seals if argv[-1].endswith(".app") else binaries).append(index)
        return binaries, seals

    def test_signs_every_binary_before_it_assembles_that_bundle(self):
        for arch in self.target.architectures:
            binaries, seals = self.signatures_for(arch)
            self.assertEqual(len(binaries), 2, f"{arch}: both binaries")
            self.assertEqual(len(seals), 1, f"{arch}: one seal")
            self.assertLess(
                max(binaries),
                seals[0],
                f"{arch}: a bundle was sealed over an unsigned binary",
            )

    def test_the_shipped_bundle_holds_the_signed_binary(self):
        """The point of signing the products before assembling.

        If the bundle were sealed over unsigned products — the natural order,
        since the bundle is what a user runs — the archive would contain a binary
        that no signature covers. Reading it back out of the archive is the only
        way to know, because a command log would say the same thing either way.
        """

        self.release()
        for arch in self.target.architectures:
            members = self.unpacked(
                apple.archive_name(
                    self.target,
                    version=core.VERSION,
                    arch=arch,
                    artifact_format="tar.gz",
                )
            )
            for binary in support.binaries():
                body = members[f"{support.BUNDLE}/Contents/MacOS/{binary.name}"]
                self.assertIn(b"SIGNED-BY " + support.IDENTITY.encode("utf-8"), body)
                self.assertIn(
                    f"arch={arch}-apple-macosx{apple.MACOS_FLOOR}".encode("utf-8"),
                    body,
                    f"the {arch} archive shipped the wrong architecture's binary",
                )

    def test_the_bundle_is_sealed_after_its_content_is_stamped(self):
        order = self.toolchain.names()
        stamp = order.index(apple.PLISTBUDDY)
        seal = [
            index
            for index, (argv, _) in enumerate(self.toolchain.calls)
            if argv[0] == apple.CODESIGN and argv[-1].endswith(".app")
        ]
        self.assertLess(stamp, seal[0], "the seal must cover the stamped version")

    def test_the_bundle_reports_the_version_being_released(self):
        self.release()
        members = self.unpacked(apple.archive_name(
            self.target, version=core.VERSION, arch="x86_64", artifact_format="tar.gz"
        ))
        plist = members[f"{support.BUNDLE}/Contents/Info.plist"].decode("utf-8")
        self.assertIn(f"CFBundleShortVersionString={core.VERSION}", plist)
        self.assertIn(f"CFBundleVersion={core.VERSION}", plist)

    def test_imports_the_configured_certificate_and_removes_the_keychain(self):
        """The imported material is the secret the job was given.

        Asserted on the decoded bytes rather than on the path, because the path
        is a scratch file the runner owns and cleans up: what matters is that
        the bytes in the keychain are the repository's certificate.
        """

        import base64

        before = len(self.toolchain.imported)
        self.release()
        self.assertEqual(
            len(self.toolchain.imported) - before,
            len(self.target.architectures),
            "one import per architecture",
        )
        for imported in self.toolchain.imported:
            # The runner decodes the secret before writing the file `security`
            # imports, so what arrives is the certificate rather than its
            # transport encoding.
            self.assertEqual(imported, base64.b64decode(support.FAKE_P12, validate=True))
        self.assertTrue(self.toolchain.keychains_deleted, "the keychain is left behind")
        order = self.toolchain.names()
        self.assertLess(order.index(apple.SECURITY), order.index(apple.CODESIGN))
        removed = [
            argv for argv in self.toolchain.commands(apple.SECURITY) if "delete-keychain" in argv
        ]
        self.assertTrue(removed)

    def test_the_keychain_is_removed_even_when_signing_fails(self):
        self.toolchain.sign_fails_for = (support.IDENTITY,)
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter().sign(self.request(), self.manifest)
        self.assertEqual(caught.exception.code, "sign-failed")
        self.assertTrue(
            self.toolchain.keychains_deleted, "the keychain outlived the failure"
        )

    def test_refuses_to_ship_when_the_pin_finds_another_identity(self):
        """`codesign` succeeded and signed with the wrong certificate.

        The pin exists for exactly this: a keychain that silently held a
        different identity than the one configured signs successfully and ships
        an application no reviewer can attribute.
        """

        self.toolchain.pins_wrong_identity = True
        with self.assertRaises(apple.AppleError) as caught:
            self.adapter().sign(self.request(), self.manifest)
        self.assertEqual(caught.exception.code, "identity-not-pinned")
        self.assertIn(support.IDENTITY, str(caught.exception))

    def test_publishes_nothing_when_signing_fails(self):
        """A failed release publishes no further artifacts.

        The stage raises before it writes anything, so the files a previous
        successful run of the same directory left are all that remain. Anything
        else would mean an archive written over a half-signed bundle.
        """

        before = sorted(os.listdir(self.dist))
        self.toolchain.sign_fails_for = (support.IDENTITY,)
        with self.assertRaises(apple.AppleError):
            self.adapter().sign(self.request(), self.manifest)
        self.assertEqual(sorted(os.listdir(self.dist)), before)

    def test_publishes_nothing_when_the_archive_was_rewritten(self):
        self.adapter().sign(self.request_, self.manifest)
        archive = self.archive_named(
            apple.archive_name(
                self.target,
                version=core.VERSION,
                arch="x86_64",
                artifact_format="tar.gz",
            )
        )
        with open(archive, "ab") as handle:
            handle.write(b"tampered")
        report = self.adapter_.verify(self.request_, self.signed)
        self.assertFalse(report.verified)
        self.assertEqual(report.code, "apple-verification-failed")
        self.assertIn(self.target.id, report.detail)

    def test_reports_a_deleted_artifact_rather_than_passing(self):
        self.adapter().sign(self.request_, self.manifest)
        os.remove(
            os.path.join(
                self.dist,
                apple.archive_name(
                    self.target,
                    version=core.VERSION,
                    arch="arm64",
                    artifact_format="zip",
                ),
            )
        )
        report = self.adapter_.verify(self.request_, self.signed)
        self.assertFalse(report.verified)
        self.assertIn("is missing", report.detail)

    def test_records_the_identity_it_signed_with_on_every_archive(self):
        signed, _names = self.release()
        archives = [
            artifact for artifact in signed.artifacts if artifact.type == "archive"
        ]
        self.assertTrue(archives)
        for artifact in archives:
            self.assertEqual(artifact.signing, "signed")
            self.assertEqual(artifact.signing_identity, support.IDENTITY)


class PackagingTests(AppleWalkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository()
        self.target = support.target()
        self.signed, self.names = self.release()

    def test_publishes_one_archive_per_declared_format_and_architecture(self):
        archives = [
            name for name in self.names if name.endswith((".tar.gz", ".zip"))
        ]
        self.assertEqual(len(archives), 4)
        for arch in ("x86_64", "arm64"):
            for artifact_format in ("tar.gz", "zip"):
                self.assertIn(
                    apple.archive_name(
                        self.target,
                        version=core.VERSION,
                        arch=arch,
                        artifact_format=artifact_format,
                    ),
                    self.names,
                )

    def test_two_targets_in_one_release_do_not_collide(self):
        """A release's assets share one flat namespace.

        Two targets shipping `SHA256SUMS.txt` would overwrite each other, so the
        checksum file is named for the target it belongs to.
        """

        other = support.target(
            id="macos-cli",
            architectures=("arm64",),
            app_bundle=config_module.ReleaseAppBundle(
                name="NanoDictateCLI.app",
                identifier="com.nanodictate.cli",
                info_plist=support.app_bundle().info_plist,
                entitlements=support.app_bundle().entitlements,
                resources=(),
            ),
        )
        _second, names = self.release(other)
        self.assertEqual(set(names) & set(self.names), set(), "the two targets collide")
        checksum_names = [name for name in names if "SHA256SUMS" in name]
        self.assertEqual(len(checksum_names), 1)
        self.assertNotEqual(
            checksum_names[0], [n for n in self.names if "SHA256SUMS" in n][0]
        )

    def test_the_archive_carries_the_bundle_and_the_products(self):
        """Everything a user needs, from one archive, with nothing hidden.

        The products sit beside the bundle at the top level and the declared
        resources are flattened to their names, so unpacking the tarball gives a
        runnable bundle and the files this release has always shipped beside it.
        """

        members = self.unpacked(
            apple.archive_name(
                self.target,
                version=core.VERSION,
                arch="x86_64",
                artifact_format="tar.gz",
            )
        )
        self.assertIn(f"{support.BUNDLE}/Contents/Info.plist", members)
        for binary in support.binaries():
            self.assertIn(binary.name, members)
            self.assertIn(f"{support.BUNDLE}/Contents/MacOS/{binary.name}", members)
        self.assertIn(os.path.basename(support.app_bundle().resources[0]), members)

    def test_the_zip_carries_the_same_files_as_the_tarball(self):
        """Both formats ship one release.

        A target that declares both is promising a user the same bundle either
        way; a member present in one and absent from the other is a release that
        only works on the platform its publisher preferred.
        """

        def names_for(artifact_format: str):
            return set(
                self.unpacked(
                    apple.archive_name(
                        self.target,
                        version=core.VERSION,
                        arch="x86_64",
                        artifact_format=artifact_format,
                    )
                )
            )

        self.assertEqual(names_for("tar.gz"), names_for("zip"))
        self.assertIn(f"{support.BUNDLE}/Contents/Info.plist", names_for("zip"))

    def test_the_checksum_file_lists_the_archives_and_not_itself(self):
        path = os.path.join(self.dist, apple.checksum_name(self.target))
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        listed = {line.split()[-1] for line in lines}
        self.assertEqual(
            listed,
            {
                name for name in self.names if name.endswith((".tar.gz", ".zip"))
            },
        )
        self.assertNotIn(apple.checksum_name(self.target), listed)

    def test_two_runs_of_one_commit_produce_the_same_bytes(self):
        first = {
            name: self.archive_named(name)
            for name in self.names
            if name.endswith((".tar.gz", ".zip"))
        }
        digests = {name: _digest(path) for name, path in first.items()}
        # A second release of the same commit into a clean directory, with the
        # same epoch: this is the reproducibility claim a consumer's recorded
        # digest depends on.
        import shutil
        import tempfile

        other = tempfile.mkdtemp(prefix="continuum-apple-again-")
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copytree(self.workdir, other, dirs_exist_ok=True)
        shutil.rmtree(os.path.join(other, "dist"))
        os.makedirs(os.path.join(other, "dist"), exist_ok=True)
        toolchain = fake.FakeToolchain(
            binaries=self.toolchain.binaries,
            bundle=support.BUNDLE,
            info_plist=support.app_bundle().info_plist,
            resources=support.app_bundle().resources,
        )
        adapter = fake.adapter_for(
            other, toolchain, environment=dict(self.environment)
        )
        manifest = adapter.build(
            self.request(workdir=other),
        )
        signed = adapter.sign(self.request(workdir=other), manifest)
        self.assertEqual({a.name for a in signed.artifacts}, set(self.names))
        for artifact in signed.artifacts:
            if artifact.name.endswith((".tar.gz", ".zip")):
                self.assertEqual(
                    _digest(artifact.path),
                    digests[artifact.name],
                    f"{artifact.name} is not reproducible",
                )

    def test_every_member_carries_the_fixed_epoch(self):
        with tarfile.open(
            os.path.join(
                self.dist,
                apple.archive_name(
                    self.target,
                    version=core.VERSION,
                    arch="x86_64",
                    artifact_format="tar.gz",
                ),
            ),
            "r:gz",
        ) as archive:
            stamps = {item.mtime for item in archive.getmembers()}
        self.assertEqual(stamps, {archive_module.DEFAULT_EPOCH})

    def test_everything_published_is_under_the_dist_directory(self):
        for artifact in self.signed.artifacts:
            self.assertTrue(
                artifact.path.startswith(self.dist + os.sep),
                f"{artifact.name} was written to {artifact.path}",
            )


class DryRunTests(AppleWalkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository()
        self.target = support.target()
        self.request_ = self.request(dry_run=True)
        self.adapter_ = self.adapter()

    def test_a_dry_run_names_the_files_a_real_run_writes(self):
        adapter = self.adapter()
        built = adapter.build(self.request(dry_run=True))
        signed = adapter.sign(self.request(dry_run=True), built)
        real, _names = self.release()
        self.assertEqual(
            [artifact.name for artifact in signed.artifacts],
            [artifact.name for artifact in real.artifacts],
        )
        self.assertEqual(
            [(a.name, a.path) for a in signed.artifacts],
            [(a.name, a.path) for a in real.artifacts],
        )

    def test_a_dry_run_starts_no_process_and_writes_nothing(self):
        adapter = self.adapter()
        adapter.build(self.request(dry_run=True))
        adapter.sign(self.request(dry_run=True), adapter.build(self.request(dry_run=True)))
        self.assertEqual(self.toolchain.calls, [])
        self.assertEqual(os.listdir(self.dist), [])

    def test_a_declared_artifact_reports_no_signature(self):
        declared = self.adapter_.sign(
            self.request_, self.adapter_.build(self.request_)
        )
        for artifact in declared.artifacts:
            if artifact.type == "archive":
                self.assertNotEqual(artifact.signing, "signed")
                self.assertFalse(os.path.exists(artifact.path))

    def test_a_dry_run_verifies_nothing(self):
        report = self.adapter_.verify(
            self.request_, self.adapter_.sign(self.request_, self.adapter_.build(self.request_))
        )
        self.assertTrue(report.verified)
        self.assertEqual(report.code, "planned")

    def test_a_dry_run_does_not_require_the_approved_checkout(self):
        """Nothing is built, so there are no unreviewed bytes to ship.

        A dry run on a developer laptop, whose checkout is not the release
        commit, is how a release is reviewed before it is cut.
        """

        adapter = self.adapter(revision="c" * 40)
        adapter.build(self.request(dry_run=True))


class AdapterContractTests(AppleWalkTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository()
        self.target = support.target()

    def test_the_adapter_satisfies_the_target_adapter_contract(self):
        missing = contract.conform(self.adapter(), "target-adapter")
        self.assertEqual(missing, ())

    def test_the_adapter_is_usable_through_the_registry(self):
        from continuum.release import adapters

        plan = adapters.plan_for(self.target, environment=support.environment())
        self.assertEqual(plan.adapter, "apple")
        self.assertTrue(plan.steps)


def _digest(path: str) -> str:
    from continuum.release.contract import digest_file

    return digest_file(path, "sha256")[1]