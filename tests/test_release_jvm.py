"""The JVM adapter's decisions, against a build that is a script.

A JVM release fails in ways no unit test of a Python function would catch unless
the build, the keyring, and the checkout's revision are all injected -- so they
are. `FakeBuildSystem` writes the files the real tool would have written and
records the argument vectors it was asked for, which makes two assertions
possible that a real build cannot be asked for: that a Gradle and a Maven build
of the same coordinates produce *the same names*, and that exactly one wrapper
invocation produced every output.

The properties worth most of the assertions here are **both build systems agree
on the names** and **the built POM is read back rather than trusted**. The first
is why a Gradle `widgets-1.4.0-all.zip` is recorded as `widgets-1.4.0.zip`; the
second is why cutting tag `v1.4.0` from a POM that still says `1.3.0` is refused
before anything is signed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from continuum.release import contract, jvm
from continuum.release.contract import (
    SIGNING_SIGNED,
    TYPE_CHECKSUMS,
    TYPE_PACKAGE,
    TYPE_SIGNATURE,
    BuildRequest,
    PublishRequest,
    TargetSpec,
)
from continuum.release.jvm import (
    BUILD_AMBIGUOUS,
    BUILD_AUTO,
    CHECKSUMS_FILE,
    BUILD_GRADLE,
    BUILD_MAVEN,
    MODE_APPLICATION,
    MODE_LIBRARY,
    OUTPUT_APPLICATION_JAR,
    OUTPUT_DISTRIBUTION,
    OUTPUT_JAVADOC,
    OUTPUT_LIBRARY_JAR,
    OUTPUT_POM,
    OUTPUT_SOURCES,
    JvmAdapter,
    JvmError,
    JvmSettings,
    MavenCoordinates,
    classify_output,
    discover,
    maven_version_for,
    parse_settings,
    planned_paths,
    read_pom,
    settings_from,
)

from . import release_core_support as support

SHA = "a" * 40
OTHER_SHA = "b" * 40
VERSION = "1.4.0"
GROUP = "com.example"
ARTIFACT = "widgets"
KEY = "0123456789ABCDEF"
KEY_SECRET = "WIDGETS_SIGNING_KEY"
PASSPHRASE_SECRET = "WIDGETS_SIGNING_PASSPHRASE"

POM = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{GROUP}</groupId>
  <artifactId>{ARTIFACT}</artifactId>
  <version>{VERSION}</version>
  <name>Widgets</name>
  <description>A library of widgets.</description>
  <url>https://example.com/widgets</url>
  <licenses>
    <license>
      <name>Apache-2.0</name>
      <url>https://www.apache.org/licenses/LICENSE-2.0.txt</url>
    </license>
  </licenses>
  <scm>
    <url>https://github.com/example/widgets</url>
    <connection>scm:git:git://github.com/example/widgets.git</connection>
  </scm>
  <developers>
    <developer>
      <name>Widgets Team</name>
      <email>widgets@example.com</email>
    </developer>
  </developers>
</project>
"""


def target(**options: Any) -> TargetSpec:
    return TargetSpec(id="jvm", adapter=jvm.ADAPTER_NAME, options=tuple(options.items()))


def settings(**overrides: Any) -> JvmSettings:
    values: Dict[str, Any] = {"group_id": GROUP, "artifact_id": ARTIFACT}
    values.update(overrides)
    return JvmSettings(**values)


def build_request(spec: Optional[TargetSpec] = None, **overrides: Any) -> BuildRequest:
    values: Dict[str, Any] = {
        "target": spec or target(group_id=GROUP, artifact_id=ARTIFACT),
        "version": VERSION,
        "source_sha": SHA,
        "key": "build-key",
    }
    values.update(overrides)
    return BuildRequest(**values)


def coordinates(version: str = VERSION) -> MavenCoordinates:
    return MavenCoordinates(group_id=GROUP, artifact_id=ARTIFACT, version=version)


def library_target(**overrides: Any) -> TargetSpec:
    values: Dict[str, Any] = {
        "group_id": GROUP,
        "artifact_id": ARTIFACT,
        "mode": MODE_LIBRARY,
    }
    values.update(overrides)
    return target(**values)


def application_target(**overrides: Any) -> TargetSpec:
    values: Dict[str, Any] = {"group_id": GROUP, "artifact_id": ARTIFACT}
    values.update(overrides)
    return target(**values)


def signing_target(**overrides: Any) -> TargetSpec:
    values: Dict[str, Any] = {
        "group_id": GROUP,
        "artifact_id": ARTIFACT,
        "mode": MODE_LIBRARY,
        "signing_key": KEY,
        "signing_key_secret": KEY_SECRET,
        "passphrase_secret": PASSPHRASE_SECRET,
        "require_signing": True,
    }
    values.update(overrides)
    return target(**values)


class FakeBuildSystem:
    """A build system that writes what the real one would write.

    One invocation writes every output the target asked for, which is what the
    real thing does and what the adapter's single-invocation promise is checked
    against: a second invocation recorded here would be a second build, and the
    jar and the sources jar from two builds are two trees.
    """

    def __init__(self, root: str, build_system: str, spec: JvmSettings) -> None:
        self.root = root
        self.build_system = build_system
        self.spec = spec
        self.build_calls: List[Tuple[str, ...]] = []
        self.sign_calls: List[Tuple[str, ...]] = []
        self.gpg_homes: List[str] = []
        self.pom_body = POM
        self.signature_fails = False

    # -- geometry ---------------------------------------------------------
    @property
    def libraries(self) -> str:
        parts = ("build", "libs") if self.build_system == BUILD_GRADLE else ("target",)
        return os.path.join(self.root, self.spec.module_directory(self.build_system), *parts)

    @property
    def distributions(self) -> str:
        parts = (
            ("build", "distributions") if self.build_system == BUILD_GRADLE else ("target",)
        )
        return os.path.join(self.root, self.spec.module_directory(self.build_system), *parts)

    # -- the runner -------------------------------------------------------
    def runner(
        self, argv: Sequence[str], workdir: str, environment: Dict[str, str]
    ) -> jvm.CommandResult:
        argv = list(argv)
        if argv[0].endswith("gpg"):
            return self._gpg(argv, environment)
        self.build_calls.append(tuple(argv))
        self.write_outputs()
        return jvm.CommandResult(code=0, output="BUILD SUCCESSFUL")

    def _gpg(self, argv: Sequence[str], environment: Dict[str, str]) -> jvm.CommandResult:  # noqa: D401
        self.sign_calls.append(tuple(argv))
        self.gpg_homes.append(environment.get("GNUPGHOME", ""))
        if "--version" in argv:
            return jvm.CommandResult(code=0, output="gpg (GnuPG) 2.4.4")
        if "--list-secret-keys" in argv:
            # Colon format, because that is what the adapter parses: a fake that
            # answered in human format would prove nothing about the parsing.
            return jvm.CommandResult(
                code=0,
                output=(
                    "tru::1:\nsec:u:4096:1:0123456789ABCDEF:1700000000::"
                    "rsa4096 1700000000 [SC]:::+:::23::0:\n"
                    f"fpr:::::::::{KEY}:\n"
                ),
            )
        if argv[1] == "--verify":
            if self.signature_fails:
                return jvm.CommandResult(code=1, output="gpg: BAD signature")
            return jvm.CommandResult(
                code=0,
                output=f"gpg: Good signature from {KEY} [unknown]\ngpg: Good signature "
                f"\"key {KEY}\"",
            )
        if self.signature_fails:
            return jvm.CommandResult(
                code=2, output="gpg: signing failed: Inappropriate ioctl for device"
            )
        if "--output" in argv:
            with open(argv[argv.index("--output") + 1], "w", encoding="ascii") as handle:
                handle.write("-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n")
        return jvm.CommandResult(code=0, output="")

    # -- what the tool writes ---------------------------------------------
    def write_outputs(self) -> None:
        os.makedirs(self.libraries, exist_ok=True)
        os.makedirs(self.distributions, exist_ok=True)
        wanted = self.spec.requested
        if OUTPUT_APPLICATION_JAR in wanted or OUTPUT_LIBRARY_JAR in wanted:
            self._write(f"{ARTIFACT}-{VERSION}.jar", b"jar bytes", self.libraries)
        if OUTPUT_POM in wanted:
            self._write(f"{ARTIFACT}-{VERSION}.pom", self.pom_body.encode("utf-8"), self.libraries)
        if OUTPUT_SOURCES in wanted:
            self._write(f"{ARTIFACT}-{VERSION}-sources.jar", b"sources bytes", self.libraries)
        if OUTPUT_JAVADOC in wanted:
            self._write(f"{ARTIFACT}-{VERSION}-javadoc.jar", b"javadoc bytes", self.libraries)
        if OUTPUT_DISTRIBUTION in wanted:
            extension = "zip" if self.spec.distribution == jvm.DISTRIBUTION_ZIP else "tar.gz"
            qualifier = "-all" if self.build_system == BUILD_GRADLE else ""
            self._write(
                f"{ARTIFACT}-{VERSION}{qualifier}.{extension}",
                b"distribution bytes",
                self.distributions,
            )

    @staticmethod
    def _write(name: str, body: bytes, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(body)
        return path


class JvmTestCase(unittest.TestCase):
    """A checkout with a wrapper in it, and a build that can be run against it."""

    build_system = BUILD_GRADLE

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="continuum-jvm-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.scratch = os.path.join(self.root, "scratch")
        os.makedirs(self.scratch, exist_ok=True)
        self._write("gradlew" if self.build_system == BUILD_GRADLE else "mvnw", b"#!/bin/sh\n")

    def _write(self, name: str, body: bytes, directory: str = "") -> str:
        path = os.path.join(directory or self.root, name)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def make_adapter(
        self,
        spec: Optional[JvmSettings] = None,
        *,
        build_system: str = "",
        environment: Optional[Dict[str, str]] = None,
        git_revision: Any = None,
    ) -> Tuple[JvmAdapter, FakeBuildSystem]:
        resolved = spec or settings(
            mode=MODE_APPLICATION, build_system=build_system or self.build_system
        )
        system = resolved.build_system if resolved.build_system != BUILD_AUTO else (
            build_system or self.build_system
        )
        fake = FakeBuildSystem(self.root, system, resolved)
        adapter = JvmAdapter(
            environment=environment if environment is not None else {},
            command_runner=fake.runner,
            workdir=self.root,
            scratch_dir=self.scratch,
            git_revision=git_revision if git_revision is not None else (lambda _root: SHA),
        )
        self.addCleanup(adapter.cleanup)
        return adapter, fake

    def signed_library(self) -> Tuple[JvmAdapter, FakeBuildSystem, Any, Any]:
        """A built, signed library, with the adapter and the fake that built it."""

        adapter, fake = self.make_adapter(
            settings(
                build_system=self.build_system,
                mode=MODE_LIBRARY,
                signing_key=KEY,
                signing_key_secret=KEY_SECRET,
                passphrase_secret=PASSPHRASE_SECRET,
                require_signing=True,
            ),
            environment={KEY_SECRET: "-----BEGIN PGP PRIVATE KEY-----", PASSPHRASE_SECRET: "s3cret"},
        )
        request = build_request(signing_target())
        manifest = adapter.build(request)
        return adapter, fake, request, adapter.sign(request, manifest)


# -- versions and coordinates ------------------------------------------------


class VersionTests(unittest.TestCase):
    def test_a_tag_becomes_a_maven_version(self):
        self.assertEqual(maven_version_for("v1.4.0"), "1.4.0")
        self.assertEqual(maven_version_for("1.4.0"), "1.4.0")
        self.assertEqual(maven_version_for("v1.4.0-rc.1"), "1.4.0-rc.1")

    def test_a_snapshot_is_a_version_the_adapter_can_build(self):
        # The adapter builds whatever version a tag carries; it is the Central
        # publisher that refuses to *publish* a snapshot, because Central has no
        # repository to put one in. Refusing it here would stop a build that is
        # perfectly legal on a repository this project owns.
        self.assertEqual(maven_version_for("v1.4.0-SNAPSHOT"), "1.4.0-SNAPSHOT")

    def test_a_build_metadata_version_is_refused(self):
        # `1.4.0+build.7` is a valid Gradle version and not a publishable Maven
        # one. Refused while the string becomes a coordinate, rather than by a
        # registry minutes after a deployment was uploaded.
        with self.assertRaises(JvmError) as caught:
            maven_version_for("1.4.0+build.7")
        self.assertEqual(caught.exception.code, "version-malformed")

    def test_coordinates_render_the_repository_layout(self):
        gav = coordinates()
        self.assertEqual(gav.gav, "com.example:widgets:1.4.0")
        self.assertEqual(gav.group_path, "com/example")
        self.assertEqual(gav.file(), "widgets-1.4.0.jar")
        self.assertEqual(gav.file(classifier="sources"), "widgets-1.4.0-sources.jar")
        self.assertEqual(
            gav.path("widgets-1.4.0.jar"),
            os.path.join("com", "example", "widgets", VERSION, "widgets-1.4.0.jar"),
        )

    def test_a_group_id_is_required_and_is_not_invented(self):
        with self.assertRaises(JvmError) as caught:
            JvmSettings(artifact_id=ARTIFACT)
        self.assertEqual(caught.exception.code, "configuration-invalid")
        self.assertIn("group_id", str(caught.exception))


class PomTests(JvmTestCase):
    def test_the_pom_is_read_for_what_a_registry_will_judge(self):
        path = self._write(f"{ARTIFACT}-{VERSION}.pom", POM.encode("utf-8"))
        metadata = read_pom(path)
        self.assertEqual(metadata.group_id, GROUP)
        self.assertEqual(metadata.artifact_id, ARTIFACT)
        self.assertEqual(metadata.version, VERSION)
        self.assertEqual(metadata.name, "Widgets")
        self.assertEqual(metadata.licenses, ("Apache-2.0",))
        self.assertIn("https://github.com/example/widgets", metadata.scm_url)
        self.assertEqual(metadata.developers, ("Widgets Team",))
        self.assertEqual(metadata.missing_central_requirements(), ())
        self.assertTrue(metadata.complete)

    def test_a_pom_that_names_nobody_fails_its_own_read(self):
        bare = (
            '<?xml version="1.0"?><project>'
            f"<groupId>{GROUP}</groupId><artifactId>{ARTIFACT}</artifactId>"
            f"<version>{VERSION}</version></project>"
        )
        path = self._write("bare.pom", bare.encode("utf-8"))
        metadata = read_pom(path)
        self.assertFalse(metadata.complete)
        for expected in ("name", "description", "url", "licenses", "scm", "developers"):
            self.assertIn(expected, metadata.missing_central_requirements())

    def test_a_version_in_a_parent_block_does_not_become_this_module_version(self):
        # `<parent><version>` is the parent's version. Reading the first
        # `version` element found would report the parent, and the release would
        # be refused for a POM that is entirely correct.
        parented = POM.replace(
            f"  <groupId>{GROUP}</groupId>",
            "  <parent>\n    <groupId>com.example</groupId>\n"
            "    <artifactId>parent</artifactId>\n    <version>0.9.0</version>\n"
            f"  </parent>\n  <groupId>{GROUP}</groupId>",
        )
        path = self._write("parented.pom", parented.encode("utf-8"))
        self.assertEqual(read_pom(path).version, VERSION)

    def test_an_unreadable_pom_is_a_failure_rather_than_an_empty_read(self):
        path = self._write("broken.pom", b"<project>not xml")
        with self.assertRaises(jvm.JvmError):
            read_pom(path)


# -- configuration -----------------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    def test_an_application_gets_a_jar_and_a_library_gets_the_four_files(self):
        self.assertEqual(settings(mode=MODE_APPLICATION).requested, (OUTPUT_APPLICATION_JAR,))
        self.assertEqual(
            settings(mode=MODE_LIBRARY).requested,
            (OUTPUT_LIBRARY_JAR, OUTPUT_POM, OUTPUT_SOURCES, OUTPUT_JAVADOC),
        )

    def test_a_distribution_without_a_format_is_refused(self):
        with self.assertRaises(JvmError) as caught:
            settings(outputs=(OUTPUT_LIBRARY_JAR, OUTPUT_DISTRIBUTION))
        self.assertEqual(caught.exception.code, "configuration-invalid")
        self.assertIn("distribution", str(caught.exception))

    def test_one_jar_cannot_be_both_an_application_and_a_library(self):
        with self.assertRaises(JvmError) as caught:
            settings(outputs=(OUTPUT_APPLICATION_JAR, OUTPUT_LIBRARY_JAR))
        self.assertEqual(caught.exception.code, "configuration-invalid")

    def test_a_required_signature_without_a_key_is_refused(self):
        with self.assertRaises(JvmError) as caught:
            settings(require_signing=True)
        self.assertEqual(caught.exception.code, "configuration-invalid")
        self.assertIn("signing_key", str(caught.exception))

    def test_a_key_and_an_identity_together_are_contradictory(self):
        with self.assertRaises(JvmError) as caught:
            settings(signing_key=KEY, signing_identity="CN=Widgets")
        self.assertEqual(caught.exception.code, "configuration-invalid")

    def test_a_private_key_is_referenced_by_secret_name(self):
        with self.assertRaises(JvmError) as caught:
            parse_settings(
                {
                    "group_id": GROUP,
                    "artifact_id": ARTIFACT,
                    "signing_key": KEY,
                    "signing_key_secret": "-----BEGIN PGP PRIVATE KEY-----",
                }
            )
        self.assertIn("secret name", str(caught.exception))

    def test_unknown_keys_are_refused_by_name(self):
        with self.assertRaises(JvmError) as caught:
            parse_settings(
                {"group_id": GROUP, "artifact_id": ARTIFACT, "sign_everything": True}
            )
        self.assertIn("sign_everything", str(caught.exception))

    def test_settings_survive_the_configuration_shape_and_the_target(self):
        parsed = parse_settings(
            {
                "group_id": GROUP,
                "artifact_id": ARTIFACT,
                "module": ":core",
                "distribution": "zip",
                "outputs": [OUTPUT_LIBRARY_JAR, OUTPUT_POM, OUTPUT_DISTRIBUTION],
                "signing_key_secret": KEY_SECRET,
                "passphrase_secret": PASSPHRASE_SECRET,
            }
        )
        self.assertEqual(parsed.module_directory(BUILD_GRADLE), "core")
        self.assertEqual(parsed.module_directory(BUILD_MAVEN), "core")
        self.assertEqual(parsed.secret_names(), (KEY_SECRET, PASSPHRASE_SECRET))
        self.assertEqual(
            settings_from(target(group_id=GROUP, artifact_id=ARTIFACT, module=":core")).module,
            ":core",
        )

    def test_the_adapter_satisfies_the_target_adapter_port(self):
        adapter = JvmAdapter(scratch_dir=tempfile.mkdtemp(prefix="continuum-port-"))
        self.addCleanup(adapter.cleanup)
        self.assertEqual(contract.conform(adapter, "target-adapter"), ())

    def test_a_signed_row_names_the_key_that_signed_it(self):
        # A row that claims a signature has to name something, and the key is the
        # least thing that is true about it.
        self.assertEqual(settings(signing_identity="CN=Widgets").expected_identity, "CN=Widgets")
        self.assertEqual(settings(signing_key=KEY).expected_identity, KEY)


# -- build commands ----------------------------------------------------------


class BuildCommandTests(unittest.TestCase):
    def test_a_gradle_build_is_one_invocation_with_the_version_pinned(self):
        command = settings(build_system=BUILD_GRADLE).build_command(BUILD_GRADLE, coordinates())
        self.assertEqual(command[0], "./gradlew")
        self.assertIn("clean", command)
        self.assertIn("assemble", command)
        self.assertIn(f"-P{jvm.VERSION_PROPERTY}={VERSION}", command)

    def test_a_maven_build_clears_the_output_first(self):
        command = settings(build_system=BUILD_MAVEN).build_command(BUILD_MAVEN, coordinates())
        self.assertEqual(command[0], "./mvnw")
        # `clean` is not decoration: discovery looks in the output directory,
        # and a stale jar left there by an earlier build is indistinguishable
        # from this build's output.
        self.assertIn("--batch-mode", command)
        self.assertLess(command.index("clean"), command.index("package"))

    def test_a_module_is_prefixed_onto_every_gradle_task(self):
        # `core` and `:core` are the same module, so the prefix is built from
        # the normalized form. Getting this wrong is silent: root `assemble`
        # looks like success while building a different project.
        for module in (":core", "core", ":core:cli"):
            spec = settings(build_system=BUILD_GRADLE, module=module)
            command = spec.build_command(BUILD_GRADLE, coordinates())
            prefix = module if module.startswith(":") else f":{module}"
            for task in ("clean", "assemble"):
                self.assertIn(f"{prefix}:{task}", command)

    def test_a_maven_module_is_a_relative_directory(self):
        spec = settings(build_system=BUILD_MAVEN, module="services/widget")
        self.assertEqual(
            spec.module_directory(BUILD_MAVEN), os.path.join("services", "widget")
        )
        command = spec.build_command(BUILD_MAVEN, coordinates())
        self.assertIn("--file", command)
        self.assertIn(os.path.join("services", "widget", "pom.xml"), command)

    def test_the_release_version_cannot_be_overridden_by_an_extra_argument(self):
        spec = settings(
            build_system=BUILD_GRADLE,
            build_arguments=("--no-daemon", f"-P{jvm.VERSION_PROPERTY}=0.0.1"),
        )
        command = spec.build_command(BUILD_GRADLE, coordinates())
        # The pinned version comes last, so a target that also passes a version
        # property cannot publish coordinates the release did not approve.
        self.assertEqual(command[-1], f"-P{jvm.VERSION_PROPERTY}={VERSION}")
        self.assertIn("--no-daemon", command)

    def test_a_sources_javadoc_or_distribution_output_adds_its_own_task(self):
        spec = settings(
            build_system=BUILD_GRADLE,
            mode=MODE_LIBRARY,
            outputs=(OUTPUT_LIBRARY_JAR, OUTPUT_SOURCES, OUTPUT_JAVADOC, OUTPUT_DISTRIBUTION),
            distribution="zip",
        )
        command = spec.build_command(BUILD_GRADLE, coordinates())
        self.assertIn("sourcesJar", command)
        self.assertIn("javadocJar", command)
        self.assertIn("distZip", command)

    def test_gradle_and_maven_produce_the_same_artifact_names(self):
        gradle = planned_paths(
            "/repo", settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY), coordinates(), BUILD_GRADLE
        )
        maven = planned_paths(
            "/repo", settings(build_system=BUILD_MAVEN, mode=MODE_LIBRARY), coordinates(), BUILD_MAVEN
        )
        self.assertEqual([item.name for item in gradle], [item.name for item in maven])

    def test_a_distribution_is_named_the_same_way_whatever_built_it(self):
        outputs = (OUTPUT_LIBRARY_JAR, OUTPUT_DISTRIBUTION)
        gradle = planned_paths(
            "/repo",
            settings(build_system=BUILD_GRADLE, outputs=outputs, distribution="zip"),
            coordinates(),
            BUILD_GRADLE,
        )
        maven = planned_paths(
            "/repo",
            settings(build_system=BUILD_MAVEN, outputs=outputs, distribution="zip"),
            coordinates(),
            BUILD_MAVEN,
        )
        self.assertEqual(
            [item.name for item in gradle if item.classifier == jvm.CLASSIFIER_DIST_ZIP],
            [item.name for item in maven if item.classifier == jvm.CLASSIFIER_DIST_ZIP],
        )

    def test_a_planned_path_is_where_the_build_would_have_written_it(self):
        planned = planned_paths(
            "/repo", settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY), coordinates(), BUILD_GRADLE
        )
        jar = next(item for item in planned if item.classifier == jvm.CLASSIFIER_MAIN)
        self.assertEqual(
            jar.path, os.path.join("/repo", "build", "libs", f"{ARTIFACT}-{VERSION}.jar")
        )

    def test_a_multi_module_plan_names_the_module_directory(self):
        spec = settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY, module=":core")
        planned = planned_paths("/repo", spec, coordinates(), BUILD_GRADLE)
        jar = next(item for item in planned if item.classifier == jvm.CLASSIFIER_MAIN)
        # A plan pointing at the root build directory would describe a different
        # project from the one a real run reads.
        self.assertEqual(
            jar.path, os.path.join("/repo", "core", "build", "libs", f"{ARTIFACT}-{VERSION}.jar")
        )
        self.assertIn(
            jar.path,
            [
                os.path.join(directory, os.path.basename(jar.path))
                for directory in spec.output_directories("/repo", BUILD_GRADLE)
            ],
        )


# -- detection ---------------------------------------------------------------


class DetectionTests(JvmTestCase):
    def test_a_checkout_with_one_wrapper_needs_no_configuration(self):
        adapter, _ = self.make_adapter(settings(build_system=BUILD_AUTO))
        self.assertTrue(adapter.available())
        self.assertTrue(adapter.available(build_request(application_target())))

    def test_no_wrapper_means_no_build_entry_point(self):
        os.remove(os.path.join(self.root, "gradlew"))
        adapter, _ = self.make_adapter(settings(build_system=BUILD_AUTO))
        self.assertFalse(adapter.available())

    def test_two_wrappers_are_refused_rather_than_guessed(self):
        self._write("mvnw", b"#!/bin/sh\n")
        adapter, fake = self.make_adapter(settings(build_system=BUILD_AUTO))
        request = build_request(application_target())
        with self.assertRaises(JvmError) as caught:
            adapter.build(request)
        self.assertEqual(caught.exception.code, BUILD_AMBIGUOUS)
        self.assertIn("gradlew", str(caught.exception))
        self.assertIn("mvnw", str(caught.exception))
        self.assertEqual(fake.build_calls, [])

    def test_naming_the_build_system_resolves_the_ambiguity(self):
        self._write("mvnw", b"#!/bin/sh\n")
        adapter, fake = self.make_adapter(settings(build_system=BUILD_GRADLE))
        manifest = adapter.build(
            build_request(application_target(build_system=BUILD_GRADLE))
        )
        self.assertEqual(len(fake.build_calls), 1)
        self.assertTrue(manifest.artifacts)

    def test_a_missing_wrapper_is_refused_before_the_build(self):
        os.remove(os.path.join(self.root, "gradlew"))
        adapter, fake = self.make_adapter(settings(build_system=BUILD_GRADLE))
        with self.assertRaises(JvmError) as caught:
            adapter.build(build_request(application_target(build_system=BUILD_GRADLE)))
        self.assertEqual(caught.exception.code, "wrapper-missing")
        self.assertEqual(fake.build_calls, [])


# -- discovery ---------------------------------------------------------------


class DiscoveryTests(JvmTestCase):
    def test_an_ambiguous_output_is_refused_rather_than_chosen(self):
        spec = settings(build_system=BUILD_GRADLE, mode=MODE_APPLICATION)
        fake = FakeBuildSystem(self.root, BUILD_GRADLE, spec)
        fake.write_outputs()
        # A second file with the same coordinate name, in the other directory
        # Gradle writes into. Picking one would make the release depend on the
        # order the output directories were walked in.
        self._write(
            f"{ARTIFACT}-{VERSION}.jar", b"other bytes", directory=fake.distributions
        )
        with self.assertRaises(JvmError) as caught:
            discover(self.root, spec, coordinates(), BUILD_GRADLE)
        self.assertEqual(caught.exception.code, "ambiguous-output")

    def test_a_missing_output_is_refused_rather_than_skipped(self):
        spec = settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY)
        with self.assertRaises(JvmError) as caught:
            discover(self.root, spec, coordinates(), BUILD_GRADLE)
        self.assertEqual(caught.exception.code, "output-not-found")

    def test_a_file_for_another_version_is_not_this_releases(self):
        self.assertIsNone(classify_output(f"{ARTIFACT}-1.3.0.jar", ARTIFACT, VERSION))
        self.assertIsNone(classify_output(f"{ARTIFACT}-{VERSION}-all.jar", ARTIFACT, VERSION))
        self.assertEqual(
            classify_output(f"{ARTIFACT}-{VERSION}.jar", ARTIFACT, VERSION), jvm.KEY_JAR
        )
        self.assertEqual(
            classify_output(f"{ARTIFACT}-{VERSION}.pom", ARTIFACT, VERSION), jvm.CLASSIFIER_POM
        )

    def test_a_build_by_product_next_to_the_jar_is_ignored(self):
        spec = settings(build_system=BUILD_GRADLE, mode=MODE_APPLICATION)
        fake = FakeBuildSystem(self.root, BUILD_GRADLE, spec)
        fake.write_outputs()
        # A shaded jar, a test report, and a previous version's artifact all sit
        # in the same directory. Publishing one of them would publish a file
        # whose relationship to the approved commit is unknown; refusing because
        # of one would make an unrelated by-product stop the release.
        self._write(f"{ARTIFACT}-{VERSION}-all.jar", b"shaded", directory=fake.libraries)
        self._write(f"{ARTIFACT}-1.3.0.jar", b"previous", directory=fake.libraries)
        self._write("TEST-widgets-1.4.0.xml", b"<testsuite/>", directory=fake.libraries)
        staged = discover(self.root, spec, coordinates(), BUILD_GRADLE)
        self.assertEqual([item.name for item in staged], [f"{ARTIFACT}-{VERSION}.jar"])

    def test_a_maven_build_reads_the_same_names_out_of_target(self):
        spec = settings(build_system=BUILD_MAVEN, mode=MODE_LIBRARY)
        fake = FakeBuildSystem(self.root, BUILD_MAVEN, spec)
        fake.write_outputs()
        staged = discover(self.root, spec, coordinates(), BUILD_MAVEN)
        self.assertEqual(
            [item.name for item in staged],
            [
                f"{ARTIFACT}-{VERSION}.jar",
                f"{ARTIFACT}-{VERSION}.pom",
                f"{ARTIFACT}-{VERSION}-sources.jar",
                f"{ARTIFACT}-{VERSION}-javadoc.jar",
            ],
        )


# -- building ----------------------------------------------------------------


class BuildTests(JvmTestCase):
    def test_an_application_build_records_a_jar_named_by_its_coordinate(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_GRADLE, mode=MODE_APPLICATION)
        )
        manifest = adapter.build(build_request())
        names = [item.name for item in manifest.artifacts]
        self.assertIn(f"{ARTIFACT}-{VERSION}.jar", names)
        # Gradle's `plain` jar is the application jar; the release name is the
        # coordinate, not the build system's internal qualifier.
        self.assertNotIn(f"{ARTIFACT}-{VERSION}-plain.jar", names)
        self.assertEqual(len(fake.build_calls), 1)

    def test_a_library_build_records_the_four_publishable_files(self):
        adapter, _ = self.make_adapter(
            settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY, require_pom=True)
        )
        manifest = adapter.build(build_request(library_target()))
        self.assertEqual(
            sorted(item.name for item in manifest.artifacts if item.type == TYPE_PACKAGE),
            sorted(
                [
                    f"{ARTIFACT}-{VERSION}.jar",
                    f"{ARTIFACT}-{VERSION}.pom",
                    f"{ARTIFACT}-{VERSION}-sources.jar",
                    f"{ARTIFACT}-{VERSION}-javadoc.jar",
                ]
            ),
        )

    def test_a_distribution_lands_as_a_release_asset_and_not_a_coordinate(self):
        outputs = [OUTPUT_APPLICATION_JAR, OUTPUT_DISTRIBUTION]
        adapter, _ = self.make_adapter(
            settings(
                build_system=BUILD_GRADLE,
                outputs=tuple(outputs),
                distribution="zip",
            )
        )
        manifest = adapter.build(
            build_request(application_target(outputs=outputs, distribution="zip"))
        )
        distribution = next(
            item for item in manifest.artifacts if item.name.endswith(".zip")
        )
        self.assertEqual(distribution.name, f"{ARTIFACT}-{VERSION}.zip")
        self.assertEqual(distribution.classifier, jvm.CLASSIFIER_DIST_ZIP)

    def test_checksums_are_recorded_when_the_target_asks_for_them(self):
        adapter, _ = self.make_adapter(settings(build_system=BUILD_GRADLE, mode=MODE_APPLICATION))
        manifest = adapter.build(build_request())
        checksums = [item for item in manifest.artifacts if item.type == TYPE_CHECKSUMS]
        self.assertEqual(len(checksums), 1)
        self.assertTrue(os.path.isfile(checksums[0].path))

    def test_a_dry_run_declares_the_same_names_without_building(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY)
        )
        manifest = adapter.build(build_request(library_target(), dry_run=True))
        self.assertEqual(fake.build_calls, [])
        self.assertEqual(
            sorted(item.name for item in manifest.artifacts if item.type == TYPE_PACKAGE),
            sorted(
                [
                    f"{ARTIFACT}-{VERSION}.jar",
                    f"{ARTIFACT}-{VERSION}.pom",
                    f"{ARTIFACT}-{VERSION}-sources.jar",
                    f"{ARTIFACT}-{VERSION}-javadoc.jar",
                ]
            ),
        )

    def test_a_dry_run_verifies_nothing_and_says_so(self):
        adapter, _ = self.make_adapter(settings(build_system=BUILD_GRADLE))
        manifest = adapter.build(build_request(dry_run=True))
        report = adapter.verify(build_request(dry_run=True), manifest)
        self.assertTrue(report.verified)
        self.assertEqual(report.code, "planned")

    def test_a_moved_checkout_is_refused(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_GRADLE), git_revision=lambda _root: OTHER_SHA
        )
        with self.assertRaises(JvmError) as caught:
            adapter.build(build_request())
        self.assertEqual(caught.exception.code, "source-mismatch")
        self.assertEqual(fake.build_calls, [])

    def test_a_failing_build_is_reported_with_what_it_printed(self):
        adapter, fake = self.make_adapter(settings(build_system=BUILD_GRADLE))

        def failing(argv: Sequence[str], workdir: str, environment: Dict[str, str]) -> jvm.CommandResult:
            fake.build_calls.append(tuple(argv))
            return jvm.CommandResult(code=1, output="> Task :compileJava FAILED")

        adapter.command_runner = failing
        with self.assertRaises(JvmError) as caught:
            adapter.build(build_request())
        self.assertTrue(caught.exception.code)
        self.assertIn("compileJava FAILED", str(caught.exception))

    def test_a_pom_whose_version_drifted_from_the_tag_is_refused(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_GRADLE, mode=MODE_LIBRARY, require_pom=True)
        )
        fake.pom_body = POM.replace(
            f"  <version>{VERSION}</version>", "  <version>1.3.0</version>"
        )
        with self.assertRaises(JvmError) as caught:
            adapter.build(build_request(library_target()))
        self.assertEqual(caught.exception.code, "version-mismatch")
        self.assertIn("1.3.0", str(caught.exception))

    def test_a_multi_module_build_reads_the_module_directory(self):
        adapter, fake = self.make_adapter(
            settings(
                build_system=BUILD_GRADLE,
                mode=MODE_LIBRARY,
                module=":core",
                require_pom=True,
            )
        )
        manifest = adapter.build(build_request(library_target(module=":core")))
        jar = next(item for item in manifest.artifacts if item.classifier == jvm.CLASSIFIER_MAIN)
        self.assertIn(os.path.join("core", "build", "libs"), jar.path)
        self.assertEqual(len(fake.build_calls), 1)


class MavenBuildTests(JvmTestCase):
    build_system = BUILD_MAVEN

    def setUp(self) -> None:
        super().setUp()
        os.remove(os.path.join(self.root, "mvnw"))
        self._write("mvnw", b"#!/bin/sh\n")

    def test_a_maven_library_build_records_the_same_names_as_gradle(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_MAVEN, mode=MODE_LIBRARY, require_pom=True)
        )
        manifest = adapter.build(build_request(library_target()))
        self.assertEqual(
            sorted(item.name for item in manifest.artifacts if item.type == TYPE_PACKAGE),
            sorted(
                [
                    f"{ARTIFACT}-{VERSION}.jar",
                    f"{ARTIFACT}-{VERSION}.pom",
                    f"{ARTIFACT}-{VERSION}-sources.jar",
                    f"{ARTIFACT}-{VERSION}-javadoc.jar",
                ]
            ),
        )
        self.assertEqual(fake.build_calls[0][0], "./mvnw")
        self.assertLess(
            fake.build_calls[0].index("clean"), fake.build_calls[0].index("package")
        )

    def test_a_maven_application_build_records_the_jar(self):
        adapter, fake = self.make_adapter(
            settings(build_system=BUILD_MAVEN, mode=MODE_APPLICATION)
        )
        manifest = adapter.build(build_request())
        self.assertIn(f"{ARTIFACT}-{VERSION}.jar", [item.name for item in manifest.artifacts])
        self.assertEqual(len(fake.build_calls), 1)


# -- signing and verification ------------------------------------------------


class SigningTests(JvmTestCase):
    def test_every_publishable_artifact_is_signed(self):
        _, _, _, manifest = self.signed_library()
        packages = [item for item in manifest.artifacts if item.type == TYPE_PACKAGE]
        signatures = {item.name for item in manifest.artifacts if item.type == TYPE_SIGNATURE}
        self.assertEqual(len(signatures), len(packages))
        for artifact in packages:
            signature = f"{os.path.basename(artifact.path)}.asc"
            self.assertIn(signature, signatures)
            # The signed thing is the artifact; the `.asc` is what attests to it.
            row = next(item for item in manifest.artifacts if item.name == artifact.name)
            self.assertEqual(row.signing, SIGNING_SIGNED)
            self.assertEqual(row.signing_identity, KEY)
            mark = next(item for item in manifest.artifacts if item.name == signature)
            self.assertEqual(mark.type, TYPE_SIGNATURE)
            self.assertTrue(mark.is_evidence)

    def test_signing_happens_in_a_keyring_of_its_own(self):
        _, fake, _, _ = self.signed_library()
        self.assertTrue(fake.sign_calls)
        for call in fake.sign_calls:
            self.assertTrue(call[0].endswith("gpg"))
        # Every gpg invocation is pointed at a keyring under the scratch
        # directory, so the signing key never lands in the runner's own.
        self.assertTrue(fake.gpg_homes)
        for home in fake.gpg_homes:
            self.assertTrue(home.startswith(self.scratch), home)

    def test_the_keyring_is_removed_after_signing(self):
        _, fake, _, _ = self.signed_library()
        # The key material is written under the scratch directory and removed in
        # a `finally`, so a signing failure cannot leave a private key behind.
        for entry in os.listdir(self.scratch):
            self.assertNotIn("key", entry)
        self.assertTrue(fake.sign_calls)

    def test_a_missing_key_secret_fails_closed(self):
        adapter, _ = self.make_adapter(
            settings(
                build_system=BUILD_GRADLE,
                mode=MODE_LIBRARY,
                signing_key=KEY,
                signing_key_secret=KEY_SECRET,
                require_signing=True,
            ),
            environment={},
        )
        request = build_request(signing_target())
        manifest = adapter.build(request)
        with self.assertRaises(JvmError) as caught:
            adapter.sign(request, manifest)
        self.assertEqual(caught.exception.code, "signing-material-missing")
        self.assertIn(KEY_SECRET, str(caught.exception))

    def test_a_dry_run_signs_nothing(self):
        adapter, _ = self.make_adapter(
            settings(
                build_system=BUILD_GRADLE,
                mode=MODE_LIBRARY,
                signing_key=KEY,
                signing_key_secret=KEY_SECRET,
            ),
            environment={KEY_SECRET: "PRIVATE"},
        )
        request = build_request(signing_target(), dry_run=True)
        manifest = adapter.build(build_request(signing_target()))
        signed = adapter.sign(request, manifest)
        self.assertEqual(
            [item for item in signed.artifacts if item.type == TYPE_SIGNATURE], []
        )

    def test_verification_remeasures_every_artifact_and_checks_the_signatures(self):
        adapter, _, request, manifest = self.signed_library()
        report = adapter.verify(request, manifest)
        self.assertTrue(report.verified, report.detail)
        self.assertEqual(report.code, "verified")
        self.assertIn(jvm.ADAPTER_NAME, report.verified_by)
        self.assertIn("signature", report.detail)

    def test_a_file_changed_after_the_manifest_was_written_is_caught(self):
        adapter, _, request, manifest = self.signed_library()
        jar = next(item for item in manifest.artifacts if item.type == TYPE_PACKAGE)
        with open(jar.path, "ab") as handle:
            handle.write(b"tampered")
        report = adapter.verify(request, manifest)
        self.assertFalse(report.verified)
        self.assertTrue(any(jar.name in failure for failure in report.failures))

    def test_a_signature_gpg_refuses_is_reported_with_what_gpg_printed(self):
        adapter, fake = self.make_adapter(
            settings(
                build_system=BUILD_GRADLE,
                mode=MODE_LIBRARY,
                signing_key=KEY,
                signing_key_secret=KEY_SECRET,
                passphrase_secret=PASSPHRASE_SECRET,
                require_signing=True,
            ),
            environment={KEY_SECRET: "PRIVATE", PASSPHRASE_SECRET: "s3cret"},
        )
        request = build_request(signing_target())
        manifest = adapter.build(request)
        fake.signature_fails = True
        with self.assertRaises(JvmError) as caught:
            adapter.sign(request, manifest)
        self.assertTrue(caught.exception.code)
        self.assertIn("signing failed", str(caught.exception))

    def test_components_names_the_adapter_so_a_core_can_be_wired(self):
        adapter, _ = self.make_adapter(settings(build_system=BUILD_GRADLE))
        wiring = jvm.components(adapter)
        self.assertEqual(wiring["adapters"][jvm.ADAPTER_NAME], adapter)
        self.assertNotIn("publishers", wiring)


# -- an application reaches a GitHub Release with no Central credential -------


class GitHubReleaseWiringTests(JvmTestCase):
    """The issue's first requirement, end to end and with no network.

    An application JAR and a distribution are release assets and nothing else.
    The proof is that a `GitHubReleasePublisher` over an in-memory repository
    attaches them with no Maven publisher constructed, no Central credential in
    the environment, and no coordinate in the release.
    """

    def _publish(self, outputs: Sequence[str], distribution: str = "") -> support.MemoryReleaseRepository:
        repository = support.MemoryReleaseRepository()
        publisher = support.github_publisher(repository)
        options: Dict[str, Any] = {
            "group_id": GROUP,
            "artifact_id": ARTIFACT,
            "outputs": list(outputs),
        }
        if distribution:
            options["distribution"] = distribution
        adapter, _ = self.make_adapter(
            settings(build_system=BUILD_GRADLE, **options), environment={}
        )
        manifest = adapter.build(build_request(target(**options)))
        # The core records verification on the artifacts before a publish is
        # attempted, so a publish that skipped it would be asserting something the
        # release never established.
        report = adapter.verify(build_request(target(**options)), manifest)
        self.assertTrue(report.verified, report.detail)
        request = PublishRequest(
            destination=support.GITHUB_DESTINATION,
            tag=f"v{VERSION}",
            version=VERSION,
            source_sha=SHA,
            manifests=(manifest.mark_verified(report.verified_by),),
            key="publish-key",
        )
        result = publisher.draft(request)
        self.assertTrue(result.ok, result.reason)
        return repository

    def test_an_application_jar_reaches_a_github_release(self):
        repository = self._publish([OUTPUT_APPLICATION_JAR])
        # The checksum file rides along: it is how a consumer tells the jar they
        # downloaded is the jar this release published.
        self.assertEqual(
            sorted(asset.name for asset in repository.attached(f"v{VERSION}")),
            sorted([f"{ARTIFACT}-{VERSION}.jar", CHECKSUMS_FILE]),
        )

    def test_a_distribution_reaches_a_github_release(self):
        repository = self._publish([OUTPUT_APPLICATION_JAR, OUTPUT_DISTRIBUTION], distribution="zip")
        self.assertEqual(
            sorted(asset.name for asset in repository.attached(f"v{VERSION}")),
            sorted(
                [
                    f"{ARTIFACT}-{VERSION}.jar",
                    f"{ARTIFACT}-{VERSION}.zip",
                    CHECKSUMS_FILE,
                ]
            ),
        )

    def test_no_central_credential_is_read_for_a_release_asset(self):
        # The environment is empty, so a publisher that reached for a Portal
        # token would have failed; the release attaching assets is the proof
        # that nothing did.
        repository = self._publish([OUTPUT_APPLICATION_JAR])
        self.assertTrue(repository.attached(f"v{VERSION}"))


if __name__ == "__main__":
    unittest.main()
