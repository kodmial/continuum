"""The Android adapter's decisions, and the promises it keeps.

These tests run on a Linux runner with no JDK, no Android SDK, and no Gradle.
That is deliberate rather than a limitation: the build is injected, so what is
asserted here is the *decision* — which tasks are requested, which properties are
bound, which tool signs which artifact, what is asserted, and what is removed
afterwards — and those decisions are the ones a wrong keystore, a two-invocation
build, or a missing `finally` would get wrong.

The two properties that are expensive to discover late and cheap to check here
are the single build and the teardown. A release that assembles the APK in one
Gradle call and the AAB in another can ship two artifacts from two different
checkouts, and key material that survives the job it was materialized for is a
credential on a runner rather than a temporary file.
"""

from __future__ import annotations

import base64
import os
import unittest
from typing import Any, Dict, List, Optional, Sequence

from continuum.release import android
from continuum.release import contract
from continuum.release.android import AndroidAdapter, AndroidError, CommandResult
from continuum.release.contract import (
    SIGNING_SIGNED,
    TYPE_INSTALLER,
    TYPE_PACKAGE,
    TYPE_PROVENANCE,
    BuildRequest,
    TargetSpec,
)

SHA = "c" * 40
OTHER_SHA = "d" * 40
VERSION = "2.3.1"
PACKAGE = "com.example.widgets"
KEYSTORE_SECRET = "WIDGETS_UPLOAD_KEYSTORE"
STORE_PASSWORD_SECRET = "WIDGETS_KEYSTORE_PASSWORD"
KEY_PASSWORD_SECRET = "WIDGETS_KEY_PASSWORD"
KEY_ALIAS = "upload"
IDENTITY = "CN=Widgets Upload, O=Example, C=US"

#: Not a keystore. The adapter only needs to know that the transport is base64
#: and that the bytes are not empty; a real JKS is the release job's business.
FAKE_KEYSTORE = base64.b64encode(b"\x30\x82\x00\x01 not a real keystore").decode("ascii")
STORE_PASSWORD = "not-the-store-password"
KEY_PASSWORD = "not-the-key-password"


def target(**options: Any) -> TargetSpec:
    """A target spec whose options are the adapter's own, carried untouched."""

    values: Dict[str, Any] = {
        "module": "app",
        "variant": "release",
        "keystore_secret": KEYSTORE_SECRET,
        "keystore_password_secret": STORE_PASSWORD_SECRET,
        "key_password_secret": KEY_PASSWORD_SECRET,
        "key_alias": KEY_ALIAS,
        "signing_identity": IDENTITY,
    }
    values.update(options)
    return TargetSpec(id="android", adapter=android.ADAPTER_NAME, options=tuple(sorted(values.items())))


def signing_environment(*, keystore: Optional[str] = FAKE_KEYSTORE) -> Dict[str, str]:
    env = {
        STORE_PASSWORD_SECRET: STORE_PASSWORD,
        KEY_PASSWORD_SECRET: KEY_PASSWORD,
    }
    if keystore is not None:
        env[KEYSTORE_SECRET] = keystore
    return android.bind_material(
        android.AndroidSettings(
            keystore_secret=KEYSTORE_SECRET,
            keystore_password_secret=STORE_PASSWORD_SECRET,
            key_password_secret=KEY_PASSWORD_SECRET,
            key_alias=KEY_ALIAS,
        ),
        env,
    )


class FakeToolchain:
    """A command runner that writes the files Gradle and the signing tools would.

    It is the whole of the platform's absence: the adapter is handed this instead
    of a subprocess, and the assertions are about the argument vectors it emits
    and the manifest rows it produces, neither of which needs a real toolchain to
    be meaningful.
    """

    def __init__(
        self,
        root: str,
        *,
        fail_sign: bool = False,
        sign_output: str = "keystore password was incorrect",
        verify_output: str = "Signer #1 certificate DN: " + IDENTITY,
        verify_code: int = 0,
    ) -> None:
        self.root = root
        self.calls: List[List[str]] = []
        self.fail_sign = fail_sign
        self.sign_output = sign_output
        self.verify_output = verify_output
        self.verify_code = verify_code
        #: The paths a signed artifact was written to, by name.
        self.signed: Dict[str, str] = {}

    # -- helpers ----------------------------------------------------------
    def _write(self, path: str, body: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)

    def argv_for(self, tool: str) -> List[List[str]]:
        return [argv for argv in self.calls if argv[0] == tool]

    def gradle_argv(self) -> List[str]:
        runs = self.argv_for("./gradlew")
        assert len(runs) == 1, f"expected exactly one Gradle invocation, got {len(runs)}"
        return runs[0]

    # -- the runner -------------------------------------------------------
    def __call__(
        self, argv: Sequence[str], workdir: str, environment: Dict[str, str]
    ) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0]
        if tool == "./gradlew":
            return self._gradle(argv)
        if tool == android.APKSIGNER:
            return self._apksigner(argv)
        if tool == android.JARSIGNER:
            return self._jarsigner(argv)
        raise AssertionError(f"the adapter ran an unexpected tool: {tool}")

    def _gradle(self, argv: List[str]) -> CommandResult:
        tasks = [arg for arg in argv if arg.startswith(":")]
        if any(task.endswith("assembleRelease") for task in tasks):
            self._write(
                os.path.join(
                    self.root, "app", "build", "outputs", "apk", "release",
                    "app-release-unsigned.apk",
                ),
                b"unsigned apk bytes",
            )
        if any(task.endswith("bundleRelease") for task in tasks):
            self._write(
                os.path.join(
                    self.root, "app", "build", "outputs", "bundle", "release", "app-release.aab"
                ),
                b"unsigned aab bytes",
            )
        return CommandResult(code=0, output="BUILD SUCCESSFUL")

    def _apksigner(self, argv: List[str]) -> CommandResult:
        if argv[1] == "sign":
            if self.fail_sign:
                return CommandResult(code=1, output=self.sign_output)
            out = argv[argv.index("--out") + 1]
            source = argv[-1]
            with open(source, "rb") as handle:
                self._write(out, b"signed-by-apksigner:" + handle.read())
            return CommandResult(code=0, output="")
        return CommandResult(code=self.verify_code, output=self.verify_output)

    def _jarsigner(self, argv: List[str]) -> CommandResult:
        if "-signedjar" in argv:
            if self.fail_sign:
                return CommandResult(code=1, output=self.sign_output)
            out = argv[argv.index("-signedjar") + 1]
            source = argv[argv.index("-signedjar") + 2]
            with open(source, "rb") as handle:
                self._write(out, b"signed-by-jarsigner:" + handle.read())
            return CommandResult(code=0, output="")
        return CommandResult(code=self.verify_code, output=self.verify_output)


class AdapterTestCase(unittest.TestCase):
    """A checkout with a Gradle wrapper, a scratch directory, and a toolchain."""

    def setUp(self) -> None:
        import tempfile

        self.workdir = tempfile.mkdtemp(prefix="continuum-android-test-")
        self.addCleanup(self._remove, self.workdir)
        wrapper = os.path.join(self.workdir, "gradlew")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        self.scratch = os.path.join(self.workdir, "scratch")
        self.toolchain: Optional[FakeToolchain] = None

    @staticmethod
    def _remove(path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def adapter(
        self,
        *,
        git_revision: Any = None,
        toolchain: Optional[FakeToolchain] = None,
        **toolchain_options: Any,
    ) -> AndroidAdapter:
        self.toolchain = toolchain or FakeToolchain(self.workdir, **toolchain_options)
        return AndroidAdapter(
            environment=signing_environment(),
            command_runner=self.toolchain,
            workdir=self.workdir,
            scratch_dir=self.scratch,
            git_revision=git_revision,
        )

    def request_for(self, spec: Optional[TargetSpec] = None, **overrides: Any) -> BuildRequest:
        values: Dict[str, Any] = {
            "target": spec or target(),
            "version": VERSION,
            "source_sha": SHA,
            "key": "unit-key",
            "workdir": self.workdir,
        }
        values.update(overrides)
        return BuildRequest(**values)


# -- version binding --------------------------------------------------------


class VersionBindingTests(unittest.TestCase):
    def test_version_code_keeps_semantic_order(self):
        codes = [android.version_code_for(v) for v in ("1.0.0", "1.0.1", "1.1.0", "2.0.0")]
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(
            codes, [1_000_000, 1_000_001, 1_001_000, 2_000_000]
        )

    def test_a_prerelease_keeps_its_own_version_name(self):
        self.assertEqual(android.version_name_for("1.2.3-rc.1"), "1.2.3-rc.1")
        # The code is the same as the release it precedes, because Android has no
        # prerelease channel: the distinction the store can see is the name.
        self.assertEqual(
            android.version_code_for("1.2.3-rc.1"), android.version_code_for("1.2.3")
        )

    def test_a_leading_v_is_stripped_from_the_name_only(self):
        self.assertEqual(android.version_name_for("v1.2.3"), "1.2.3")
        self.assertEqual(android.version_code_for("v1.2.3"), 1_002_003)

    def test_a_non_semantic_version_is_refused_rather_than_guessed(self):
        for bad in ("1.2", "1.2.3.4", "release", "", "1.2.3-", "v"):
            with self.subTest(version=bad):
                with self.assertRaises(AndroidError) as caught:
                    android.version_code_for(bad)
                self.assertEqual(caught.exception.code, "version-malformed")

    def test_a_component_too_large_for_the_scheme_is_refused(self):
        with self.assertRaises(AndroidError) as caught:
            android.version_code_for("1.1000.0")
        self.assertEqual(caught.exception.code, "version-code-out-of-range")

    def test_the_offset_moves_the_whole_series_and_can_be_overridden(self):
        self.assertEqual(android.version_code_for("1.4.0", offset=5), 1_004_005)
        explicit = android.AndroidSettings(version_code=77)
        self.assertEqual(explicit.version_binding("1.4.0"), ("1.4.0", 77))

    def test_an_offset_and_an_explicit_code_together_are_refused(self):
        with self.assertRaises(AndroidError) as caught:
            android.AndroidSettings(version_code=77, version_code_offset=5)
        self.assertIn("never apply", str(caught.exception))


# -- configuration ----------------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    def test_a_bare_target_is_a_signed_apk(self):
        settings = android.parse_settings({"keystore_secret": KEYSTORE_SECRET, "key_alias": KEY_ALIAS})
        self.assertEqual(settings.outputs, (android.OUTPUT_APK,))
        self.assertTrue(settings.require_signing)
        self.assertEqual(settings.expected_identity, KEY_ALIAS)

    def test_a_signed_build_needs_the_key_alias_the_tools_are_pointed_at(self):
        with self.assertRaises(AndroidError) as caught:
            android.parse_settings({"keystore_secret": KEYSTORE_SECRET})
        self.assertIn("key_alias", str(caught.exception))

    def test_an_unknown_key_is_refused_by_name(self):
        with self.assertRaises(AndroidError) as caught:
            android.parse_settings({"packageName": PACKAGE})
        self.assertIn("packageName", str(caught.exception))

    def test_both_outputs_are_requested_from_one_invocation(self):
        settings = android.parse_settings({"outputs": ["apk", "aab"]})
        self.assertEqual(settings.gradle_tasks(), (":app:assembleRelease", ":app:bundleRelease"))

    def test_a_flavor_is_carried_into_the_task_name(self):
        settings = android.parse_settings({"outputs": ["aab"], "flavor": "nightly"})
        self.assertEqual(settings.gradle_tasks(), (":app:bundleNightlyRelease",))

    def test_an_unknown_output_is_refused(self):
        with self.assertRaises(AndroidError) as caught:
            android.parse_settings({"outputs": ["ipa"]})
        self.assertIn("ipa", str(caught.exception))

    def test_a_production_target_cannot_have_signing_turned_off(self):
        with self.assertRaises(AndroidError) as caught:
            android.parse_settings({"production": True, "require_signing": False})
        self.assertIn("unsigned build reaches a store", str(caught.exception))

    def test_play_app_signing_still_requires_the_upload_key(self):
        with self.assertRaises(AndroidError) as caught:
            android.parse_settings({"play_app_signing": True})
        self.assertIn("upload key", str(caught.exception))

    def test_settings_can_be_read_from_a_target_spec(self):
        settings = android.settings_from(target(outputs=["aab"], variant="nightly"))
        self.assertEqual(settings.variant, "nightly")
        self.assertEqual(settings.outputs, (android.OUTPUT_AAB,))

    def test_another_adapter_is_refused(self):
        spec = TargetSpec(id="macos", adapter="apple")
        with self.assertRaises(AndroidError) as caught:
            android.settings_from(spec)
        self.assertEqual(caught.exception.code, "wrong-adapter")

    def test_signing_material_counts_an_empty_secret_as_absent(self):
        settings = android.settings_from(target())
        self.assertTrue(android.signing_material_present(settings, signing_environment()))
        # GitHub hands a fork the variable names with nothing in them, so a check
        # for presence rather than emptiness would pass and sign with nothing.
        self.assertFalse(
            android.signing_material_present(settings, signing_environment(keystore=""))
        )
        self.assertFalse(android.signing_material_present(settings, {}))


# -- output discovery -------------------------------------------------------


class OutputDiscoveryTests(AdapterTestCase):
    def _produce(self, name: str, body: bytes = b"bytes") -> str:
        path = os.path.join(self.workdir, "app", "build", "outputs", "apk", "release", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def test_the_variant_artifact_is_chosen_out_of_the_output_tree(self):
        self._produce("app-release-unsigned.apk")
        settings = android.parse_settings({})
        found = android.locate_output(settings, self.workdir, android.OUTPUT_APK)
        self.assertTrue(found.endswith("app-release-unsigned.apk"))

    def test_two_matching_apks_is_a_failure_rather_than_a_guess(self):
        self._produce("app-release-unsigned.apk")
        self._produce("app-release-x86_64.apk")
        settings = android.parse_settings({})
        with self.assertRaises(AndroidError) as caught:
            android.locate_output(settings, self.workdir, android.OUTPUT_APK)
        self.assertEqual(caught.exception.code, "ambiguous-output")

    def test_a_build_that_produced_nothing_is_a_failure(self):
        settings = android.parse_settings({})
        with self.assertRaises(AndroidError) as caught:
            android.locate_output(settings, self.workdir, android.OUTPUT_APK)
        self.assertEqual(caught.exception.code, "output-not-found")

    def test_an_artifact_name_is_flat_versioned_and_per_output(self):
        settings = android.parse_settings({})
        self.assertEqual(
            android.artifact_name(settings, android.OUTPUT_APK, VERSION), "app-2.3.1-release.apk"
        )
        self.assertEqual(
            android.artifact_name(settings, android.OUTPUT_AAB, VERSION), "app-2.3.1-release.aab"
        )


# -- build ------------------------------------------------------------------


class BuildTests(AdapterTestCase):
    def test_both_outputs_come_from_one_gradle_invocation(self):
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(target(outputs=["apk", "aab"])))
        argv = self.toolchain.gradle_argv()
        self.assertIn(":app:assembleRelease", argv)
        self.assertIn(":app:bundleRelease", argv)
        self.assertEqual(manifest.names, ("app-2.3.1-release.apk", "app-2.3.1-release.aab"))

    def test_the_version_is_bound_as_gradle_properties(self):
        adapter = self.adapter()
        adapter.build(self.request_for(target(outputs=["apk"])))
        argv = self.toolchain.gradle_argv()
        self.assertIn(f"-P{android.VERSION_NAME_PROPERTY}={VERSION}", argv)
        self.assertIn(
            f"-P{android.VERSION_CODE_PROPERTY}={android.version_code_for(VERSION)}", argv
        )

    def test_each_output_is_typed_as_what_it_is(self):
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(target(outputs=["apk", "aab"])))
        by_name = {item.name: item for item in manifest.artifacts}
        self.assertEqual(by_name["app-2.3.1-release.apk"].type, TYPE_INSTALLER)
        self.assertEqual(by_name["app-2.3.1-release.aab"].type, TYPE_PACKAGE)
        self.assertEqual(manifest.source_sha, SHA)
        self.assertEqual(manifest.adapter, android.ADAPTER_NAME)

    def test_a_checkout_on_another_commit_is_refused_before_building(self):
        adapter = self.adapter(git_revision=lambda _workdir: OTHER_SHA)
        with self.assertRaises(AndroidError) as caught:
            adapter.build(self.request_for(target(outputs=["apk"])))
        self.assertEqual(caught.exception.code, "source-mismatch")
        self.assertEqual(self.toolchain.calls, [], "Gradle must not have run")

    def test_a_dry_run_declares_artifacts_and_builds_nothing(self):
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(target(outputs=["apk", "aab"]), dry_run=True))
        self.assertTrue(all(item.declared for item in manifest.artifacts))
        self.assertEqual(self.toolchain.calls, [])
        # A declared artifact claims neither a signature nor a verification, so a
        # plan cannot be mistaken for a release that shipped.
        self.assertTrue(all(not item.signed for item in manifest.artifacts))

    def test_a_mapping_file_is_kept_as_evidence_not_as_a_download(self):
        mapping = os.path.join(self.workdir, "app", "mapping.txt")
        os.makedirs(os.path.dirname(mapping), exist_ok=True)
        with open(mapping, "w", encoding="utf-8") as handle:
            handle.write("com.example.Widgets -> a:\n")
        adapter = self.adapter()
        spec = target(outputs=["apk"], mapping_path="app/mapping.txt")
        manifest = adapter.build(self.request_for(spec))
        evidence = manifest.of_type(TYPE_PROVENANCE)
        self.assertEqual([item.name for item in evidence], ["mapping.txt"])
        self.assertTrue(evidence[0].is_evidence)
        self.assertNotIn(evidence[0], manifest.installable)
        # The evidence survives signing: a mapping file for a shipped build is
        # useless if the row that describes it disappears at the sign stage.
        signed = adapter.sign(self.request_for(spec), manifest)
        self.assertEqual(
            [item.name for item in signed.of_type(TYPE_PROVENANCE)], ["mapping.txt"]
        )

    def test_the_adapter_satisfies_the_target_adapter_port(self):
        self.assertEqual(contract.conform(self.adapter(), "target-adapter"), ())
        self.assertTrue(AndroidAdapter.SUPPORTS_DRY_RUN)
        self.assertIn("Gradle wrapper", AndroidAdapter().intent())

    def test_availability_follows_the_gradle_wrapper_in_the_checkout(self):
        self.assertTrue(self.adapter().available())
        bare = os.path.join(self.workdir, "empty")
        os.makedirs(bare, exist_ok=True)
        self.assertFalse(AndroidAdapter(workdir=bare).available())


# -- signing ----------------------------------------------------------------


class SigningTests(AdapterTestCase):
    def _built(self, spec: Optional[TargetSpec] = None) -> contract.ArtifactManifest:
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(spec or target(outputs=["apk", "aab"])))
        return adapter, manifest  # type: ignore[return-value]

    def test_a_missing_keystore_fails_the_release_closed(self):
        adapter = AndroidAdapter(
            environment=signing_environment(keystore=None),
            command_runner=FakeToolchain(self.workdir),
            workdir=self.workdir,
            scratch_dir=self.scratch,
        )
        manifest = adapter.build(self.request_for(target(outputs=["apk"])))
        with self.assertRaises(AndroidError) as caught:
            adapter.sign(self.request_for(target(outputs=["apk"])), manifest)
        self.assertEqual(caught.exception.code, "signing-material-missing")
        self.assertIn(KEYSTORE_SECRET, str(caught.exception))

    def test_a_target_that_names_no_secrets_is_refused_before_any_tool_runs(self):
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(target(outputs=["apk"])))
        with self.assertRaises(AndroidError) as caught:
            adapter.sign(
                self.request_for(target(outputs=["apk"], keystore_secret="", key_password_secret="")),
                manifest,
            )
        self.assertEqual(caught.exception.code, "signing-material-missing")
        self.assertEqual(self.toolchain.argv_for(android.APKSIGNER), [])

    def test_the_apk_is_signed_by_apksigner_and_the_bundle_by_jarsigner(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk", "aab"]))
        manifest = adapter.sign(request, adapter.build(request))
        signs = self.toolchain.argv_for(android.APKSIGNER)
        self.assertEqual([argv[1] for argv in signs], ["sign"])
        self.assertEqual(len(signs), 1)
        jars = self.toolchain.argv_for(android.JARSIGNER)
        self.assertEqual(len(jars), 1)
        self.assertIn("-signedjar", jars[0])

    def test_the_signed_rows_name_the_signing_identity(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk", "aab"]))
        signed = adapter.sign(request, adapter.build(request))
        for item in signed.installable:
            self.assertEqual(item.signing, SIGNING_SIGNED)
            self.assertEqual(item.signing_identity, IDENTITY)
        self.assertEqual(signed.names, ("app-2.3.1-release.apk", "app-2.3.1-release.aab"))

    def test_signing_changes_the_bytes_and_the_manifest_says_so(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk"]))
        unsigned = adapter.build(request)
        signed = adapter.sign(request, unsigned)
        before = unsigned.by_name("app-2.3.1-release.apk")
        after = signed.by_name("app-2.3.1-release.apk")
        self.assertNotEqual(before.digest, after.digest)
        self.assertEqual(after.name, before.name)
        self.assertEqual(after.size, os.path.getsize(after.path))

    def test_a_password_never_reaches_an_argument_vector(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk", "aab"]))
        adapter.sign(request, adapter.build(request))
        for argv in self.toolchain.calls:
            joined = " ".join(argv)
            self.assertNotIn(STORE_PASSWORD, joined)
            self.assertNotIn(KEY_PASSWORD, joined)
            # `-storepass <value>` and `--ks-pass pass:<value>` are the two forms
            # that put a secret where every process on the runner can read it.
            self.assertNotIn(f"pass:{STORE_PASSWORD}", joined)
            self.assertNotIn(f"-storepass {STORE_PASSWORD}", joined)
        signing = " ".join(
            " ".join(argv)
            for argv in self.toolchain.calls
            if argv[0] in (android.APKSIGNER, android.JARSIGNER)
        )
        self.assertIn("file:", signing)

    def test_the_keystore_and_the_password_files_are_removed_after_signing(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk"]))
        adapter.sign(request, adapter.build(request))
        self.assertEqual(os.listdir(self.scratch), [])

    def test_the_teardown_runs_even_when_signing_fails(self):
        adapter = self.adapter(fail_sign=True)
        request = self.request_for(target(outputs=["apk"]))
        manifest = adapter.build(request)
        with self.assertRaises(AndroidError) as caught:
            adapter.sign(request, manifest)
        self.assertEqual(caught.exception.code, "keystore-password-invalid")
        self.assertEqual(
            os.listdir(self.scratch),
            [],
            "a keystore that survives a failed signing is a credential on a runner",
        )

    def test_a_missing_signing_tool_is_reported_as_such(self):
        adapter = self.adapter(fail_sign=True, sign_output="apksigner: command not found")
        request = self.request_for(target(outputs=["apk"]))
        manifest = adapter.build(request)
        with self.assertRaises(AndroidError) as caught:
            adapter.sign(request, manifest)
        self.assertEqual(caught.exception.code, "signing-tool-missing")
        self.assertTrue(caught.exception.remediation)

    def test_signing_can_be_declined_only_when_the_target_says_so(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk"], require_signing=False))
        manifest = adapter.sign(request, adapter.build(request))
        self.assertEqual(self.toolchain.argv_for(android.APKSIGNER), [])
        for item in manifest.installable:
            self.assertNotEqual(item.signing, SIGNING_SIGNED)

    def test_a_dry_run_signs_nothing(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk"]), dry_run=True)
        manifest = adapter.sign(request, adapter.build(request))
        self.assertEqual(self.toolchain.argv_for(android.APKSIGNER), [])
        self.assertTrue(all(item.declared for item in manifest.artifacts))


# -- verification -----------------------------------------------------------


class VerificationTests(AdapterTestCase):
    def _signed(self, spec: Optional[TargetSpec] = None, **toolchain: Any):
        adapter = self.adapter(**toolchain)
        request = self.request_for(spec or target(outputs=["apk", "aab"]))
        return adapter, adapter.sign(request, adapter.build(request))

    def test_a_signed_artifact_verifies_and_names_its_verifier(self):
        adapter, manifest = self._signed()
        report = adapter.verify(self.request_for(target(outputs=["apk", "aab"])), manifest)
        self.assertTrue(report.verified)
        self.assertEqual(report.verified_by, android.ADAPTER_NAME)

    def test_the_bundle_is_verified_with_jarsigner_and_its_certs(self):
        adapter, manifest = self._signed()
        adapter.verify(self.request_for(target(outputs=["apk", "aab"])), manifest)
        verifications = [argv for argv in self.toolchain.argv_for(android.JARSIGNER) if "-verify" in argv]
        self.assertEqual(len(verifications), 1)
        # `-certs` is what prints the signer's certificate: without it a bundle
        # would verify against an identity the tool never reported.
        self.assertIn("-certs", verifications[0])

    def test_an_artifact_signed_by_the_wrong_key_does_not_verify(self):
        adapter, manifest = self._signed(
            target(outputs=["apk"]), verify_output="Signer #1 certificate DN: CN=Someone Else"
        )
        report = adapter.verify(self.request_for(target(outputs=["apk"])), manifest)
        self.assertFalse(report.verified)
        self.assertEqual(report.code, "android-verification-failed")
        self.assertIn(IDENTITY, report.detail)

    def test_a_rejected_artifact_does_not_verify(self):
        adapter, manifest = self._signed(
            target(outputs=["apk"]), verify_code=1, verify_output="DOES NOT VERIFY"
        )
        report = adapter.verify(self.request_for(target(outputs=["apk"])), manifest)
        self.assertFalse(report.verified)
        self.assertIn("DOES NOT VERIFY", report.detail)

    def test_a_dry_run_verifies_nothing_and_says_so(self):
        adapter = self.adapter()
        request = self.request_for(target(outputs=["apk"]), dry_run=True)
        report = adapter.verify(request, adapter.build(request))
        self.assertTrue(report.verified)
        self.assertEqual(report.code, "planned")


# -- wiring -----------------------------------------------------------------


class WiringTests(AdapterTestCase):
    def test_the_helper_wires_the_adapter_without_a_registry(self):
        adapter = self.adapter()
        wiring = android.components(adapter)
        self.assertEqual(wiring["adapters"], {android.ADAPTER_NAME: adapter})
        self.assertNotIn("publishers", wiring)

    def test_a_publisher_is_included_only_when_given(self):
        adapter = self.adapter()
        sentinel = object()
        wiring = android.components(adapter, sentinel)
        self.assertEqual(wiring["publishers"], (sentinel,))

    def test_the_registered_name_is_what_a_manifest_records(self):
        adapter = self.adapter()
        manifest = adapter.build(self.request_for(target(outputs=["apk"])))
        self.assertEqual(manifest.adapter, "android")
        self.assertEqual(adapter.name, "android")


__all__ = ["AdapterTestCase", "FakeToolchain", "target"]
