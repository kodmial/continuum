"""The `release` section of `.continuum.yml` as a contract.

Every test here is about failing closed. A release configuration that is
*almost* right is the dangerous kind: it validates, the job runs for an hour,
and the artifact that comes out the other end is signed by the wrong thing or
addresses the wrong application. So the rules are checked one at a time.
"""

from __future__ import annotations

import textwrap
import unittest

from continuum import config as config_module

from . import release_support as support


def parse(body: str) -> config_module.ContinuumConfig:
    return config_module.parse_config(body)


def release_section(body: str) -> str:
    """Wrap an already-formatted `release:` body, indented under its key."""

    indented = textwrap.indent(support.document(body), "  ")
    return "version: 1\nrelease:\n" + indented


def _target(extra: str = "") -> str:
    """A minimal target, with one extra line appended when a test needs one."""

    return (
        "    - id: macos\n"
        "      binaries:\n"
        "        - name: tool\n"
        "          identifier: com.example.tool\n"
        "      signing:\n"
        "        mode: adhoc\n"
    ) + (f"      {extra}\n" if extra else "")


class ReleaseTargetTests(unittest.TestCase):
    def test_migration_document_parses_into_a_complete_target(self):
        config = parse(support.nanodictate_document())
        self.assertTrue(config.release.enabled)
        self.assertEqual([item.id for item in config.release.targets], ["macos"])

        target = config.release.target("macos")
        self.assertIsNotNone(target)
        self.assertEqual(target.adapter, config_module.ADAPTER_APPLE)
        self.assertEqual(target.platform, config_module.PLATFORM_MACOS)
        self.assertEqual(target.build_strategy, config_module.BUILD_STRATEGY_SWIFTPM)
        self.assertEqual(target.distribution, config_module.DISTRIBUTION_DIRECT)
        self.assertEqual(target.architectures, ("x86_64", "arm64"))
        self.assertFalse(target.universal)
        self.assertEqual(target.artifacts, ("tar.gz", "zip"))
        self.assertTrue(target.hardened_runtime)
        self.assertEqual(target.publishers, ("homebrew", "macports"))
        self.assertEqual(
            [item.identifier for item in target.binaries],
            ["com.nanodictate.agent", "com.nanodictate.ctl"],
        )
        self.assertEqual(target.app_bundle.name, "NanoDictate.app")
        self.assertEqual(target.signing.mode, config_module.SIGNING_SELF_SIGNED_STABLE)
        self.assertEqual(target.signing.identity, "NanoDictate CI Signing")
        self.assertFalse(target.signing.timestamp)
        self.assertEqual(
            config_module.required_release_secret_names(config),
            ["NANODICTATE_SIGNING_P12", "NANODICTATE_SIGNING_PASSWORD"],
        )

    def test_release_is_absent_from_a_review_only_configuration(self):
        config = parse("version: 1\nreview:\n  provider: none\n")
        self.assertFalse(config.release.enabled)
        self.assertIsNone(config.release.target("macos"))
        self.assertEqual(config_module.required_release_secret_names(config), [])

    def test_empty_target_list_is_rejected_rather_than_silently_empty(self):
        with self.assertRaises(config_module.ConfigError):
            parse(release_section("  targets: []\n"))

    def test_duplicate_target_ids_are_rejected(self):
        with self.assertRaises(config_module.ConfigError):
            parse(release_section("  targets:\n" + "".join(_target() for _ in range(2))))

    def test_unknown_keys_are_rejected_at_every_level(self):
        with self.assertRaises(config_module.ConfigError):
            parse(release_section("  notarize: true\n"))
        with self.assertRaises(config_module.ConfigError):
            parse(release_section("  targets:\n    - id: macos\n      notarize: true\n"))
        with self.assertRaises(config_module.ConfigError):
            parse(release_section("  targets:\n" + _target(extra="gatekeeper: true")))
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: tool\n"
                    "          identifier: com.example.tool\n"
                    "          notarize: true\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: tool\n"
                    "          identifier: com.example.tool\n"
                    "      app_bundle:\n"
                    "        name: Tool.app\n"
                    "        identifier: com.example.tool\n"
                    "        dark_mode: true\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: tool\n"
                    "          identifier: com.example.tool\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                    "        trust: true\n"
                )
            )

    def test_platform_and_build_strategy_are_typed_and_may_be_declared(self):
        for platform, strategy in (
            ("ios", "xcode-archive"),
            ("macos", "xcode-archive"),
            ("ios", "swiftpm"),
        ):
            with self.subTest(platform=platform, strategy=strategy):
                config = parse(
                    release_section(
                        "  targets:\n"
                        "    - id: other\n"
                        f"      platform: {platform}\n"
                        f"      build_strategy: {strategy}\n"
                        "      binaries:\n"
                        "        - name: tool\n"
                        "          identifier: com.example.tool\n"
                        "      signing:\n"
                        "        mode: adhoc\n"
                    )
                )
                found = config.release.target("other")
                self.assertIsNotNone(found)
                # Declared, not buildable: a repository can state where it is
                # going without Continuum claiming it can go there today.
                self.assertFalse(found.is_mvp_executable)

    def test_unknown_platform_or_strategy_fails_closed(self):
        for body in (
            "      platform: watchos\n",
            "      build_strategy: cmake\n",
            "      distribution: mas\n",
            "      adapter: windows\n",
        ):
            with self.subTest(body=body):
                with self.assertRaises(config_module.ConfigError):
                    parse(release_section("  targets:\n    - id: t\n" + body))

    def test_architectures_and_artifacts_are_typed_lists(self):
        config = parse(
            release_section(
                "  targets:\n"
                "    - id: macos\n"
                "      architectures:\n"
                "        - arm64\n"
                "      universal: true\n"
                "      artifacts:\n"
                "        - dmg\n"
                "      binaries:\n"
                "        - name: tool\n"
                "          identifier: com.example.tool\n"
                "      signing:\n"
                "        mode: adhoc\n"
            )
        )
        found = config.release.target("macos")
        self.assertEqual(found.architectures, ("arm64",))
        self.assertTrue(found.universal)
        self.assertEqual(found.artifacts, ("dmg",))

    def test_unknown_architecture_artifact_and_publisher_are_rejected(self):
        for body in (
            "      architectures:\n        - riscv64\n",
            "      artifacts:\n        - rar\n",
            "      publishers:\n        - nix\n",
        ):
            with self.subTest(body=body):
                with self.assertRaises(config_module.ConfigError):
                    parse(release_section("  targets:\n    - id: t\n" + body))

    def test_repeated_list_entries_are_rejected(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      architectures:\n"
                    "        - arm64\n"
                    "        - arm64\n"
                    "      binaries:\n"
                    "        - name: tool\n"
                    "          identifier: com.example.tool\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )


def _target(extra: str = "") -> str:
    return (
        "    - id: macos\n"
        "      binaries:\n"
        "        - name: tool\n"
        "          identifier: com.example.tool\n"
        "      signing:\n"
        "        mode: adhoc\n"
    ) + (f"      {extra}\n" if extra else "")


class BinaryAndBundleTests(unittest.TestCase):
    def test_target_must_name_at_least_one_binary(self):
        for body in ("", "      binaries: []\n"):
            with self.subTest(body=body):
                with self.assertRaises(config_module.ConfigError):
                    parse(
                        release_section(
                            "  targets:\n    - id: macos\n"
                            "      signing:\n        mode: adhoc\n"
                            + body
                        )
                    )

    def test_bundle_identifier_must_be_reverse_dns(self):
        for identifier in ("NanoDictate", "nanodictate", "com..example", ".com.example"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(config_module.ConfigError):
                    parse(
                        release_section(
                            "  targets:\n"
                            "    - id: macos\n"
                            "      binaries:\n"
                            "        - name: tool\n"
                            f"          identifier: {identifier}\n"
                            "      signing:\n"
                            "        mode: adhoc\n"
                        )
                    )

    def test_reused_identifier_would_merge_two_products_into_one(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: agent\n"
                    "          identifier: com.example.app\n"
                    "        - name: cli\n"
                    "          identifier: com.example.app\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )

    def test_entitlements_must_be_a_repository_relative_path(self):
        for path in ("/etc/passwd", "../outside.entitlements", "a/../../b.plist"):
            with self.subTest(path=path):
                with self.assertRaises(config_module.ConfigError):
                    parse(
                        release_section(
                            "  targets:\n"
                            "    - id: macos\n"
                            "      binaries:\n"
                            "        - name: tool\n"
                            "          identifier: com.example.tool\n"
                            f"          entitlements: {path}\n"
                            "      signing:\n"
                            "        mode: adhoc\n"
                        )
                    )

    def test_binary_name_may_not_escape_the_build_directory(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: ../elsewhere/tool\n"
                    "          identifier: com.example.tool\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )

    def test_app_bundle_must_look_like_a_bundle(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                release_section(
                    "  targets:\n"
                    "    - id: macos\n"
                    "      binaries:\n"
                    "        - name: tool\n"
                    "          identifier: com.example.tool\n"
                    "      app_bundle:\n"
                    "        name: NanoDictate\n"
                    "        identifier: com.example.app\n"
                    "      signing:\n"
                    "        mode: adhoc\n"
                )
            )


class SigningContractTests(unittest.TestCase):
    def _body(self, signing_lines: str) -> str:
        return release_section(
            "  targets:\n"
            "    - id: macos\n"
            "      binaries:\n"
            "        - name: tool\n"
            "          identifier: com.example.tool\n"
            "      signing:\n" + signing_lines
        )

    def test_stable_mode_requires_identity_and_both_secret_names(self):
        with self.assertRaises(config_module.ConfigError):
            parse(self._body("        mode: self-signed-stable\n"))
        with self.assertRaises(config_module.ConfigError):
            parse(
                self._body(
                    "        mode: self-signed-stable\n"
                    "        identity: \"CI Signing\"\n"
                    "        p12_secret: SIGNING_P12\n"
                )
            )
        with self.assertRaises(config_module.ConfigError):
            parse(
                self._body(
                    "        mode: self-signed-stable\n"
                    '        identity: "CI Signing"\n'
                )
            )

    def test_secret_names_must_be_names_not_values(self):
        for value in ("sk-live-0123456789", "signing_p12", "https://example.com/p12"):
            with self.subTest(value=value):
                with self.assertRaises(config_module.ConfigError):
                    parse(
                        self._body(
                            "        mode: self-signed-stable\n"
                            '        identity: "CI Signing"\n'
                            f"        p12_secret: {value}\n"
                            "        password_secret: SIGNING_PASSWORD\n"
                        )
                    )

    def test_adhoc_mode_may_not_claim_credentials_it_cannot_use(self):
        for extra in (
            '        identity: "CI Signing"\n',
            "        p12_secret: SIGNING_P12\n",
            "        password_secret: SIGNING_PASSWORD\n",
            "        team_id_var: TEAM_ID\n",
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(config_module.ConfigError):
                    parse(self._body("        mode: adhoc\n" + extra))

    def test_adhoc_mode_needs_nothing_but_its_own_name(self):
        config = parse(self._body("        mode: adhoc\n"))
        found = config.release.target("macos")
        self.assertEqual(found.signing.mode, config_module.SIGNING_ADHOC)
        self.assertEqual(found.signing.identity, "")
        self.assertEqual(found.signing.secret_names(), ())

    def test_fallback_flag_is_meaningless_for_a_mode_that_is_already_adhoc(self):
        with self.assertRaises(config_module.ConfigError):
            parse(self._body("        mode: adhoc\n        allow_adhoc_fallback: true\n"))

    def test_developer_id_requires_a_team_and_never_a_ci_identity(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                self._body(
                    "        mode: developer-id\n"
                    '        identity: "Developer ID Application: Example (TEAM)"\n'
                    "        p12_secret: SIGNING_P12\n"
                    "        password_secret: SIGNING_PASSWORD\n"
                )
            )
        config = parse(
            self._body(
                "        mode: developer-id\n"
                '        identity: "Developer ID Application: Example (TEAM)"\n'
                "        p12_secret: SIGNING_P12\n"
                "        password_secret: SIGNING_PASSWORD\n"
                "        team_id_var: TEAM_ID\n"
            )
        )
        self.assertEqual(
            config.release.target("macos").signing.mode, config_module.SIGNING_DEVELOPER_ID
        )

    def test_team_id_belongs_to_developer_id_only(self):
        with self.assertRaises(config_module.ConfigError):
            parse(
                self._body(
                    "        mode: self-signed-stable\n"
                    '        identity: "CI Signing"\n'
                    "        p12_secret: SIGNING_P12\n"
                    "        password_secret: SIGNING_PASSWORD\n"
                    "        team_id_var: TEAM_ID\n"
                )
            )

    def test_timestamp_defaults_off_and_must_be_boolean(self):
        config = parse(
            self._body(
                "        mode: self-signed-stable\n"
                '        identity: "CI Signing"\n'
                "        p12_secret: SIGNING_P12\n"
                "        password_secret: SIGNING_PASSWORD\n"
            )
        )
        self.assertFalse(config.release.target("macos").signing.timestamp)
        with self.assertRaises(config_module.ConfigError):
            parse(
                self._body(
                    "        mode: self-signed-stable\n"
                    '        identity: "CI Signing"\n'
                    "        p12_secret: SIGNING_P12\n"
                    "        password_secret: SIGNING_PASSWORD\n"
                    '        timestamp: "yes please"\n'
                )
            )

    def test_unknown_signing_mode_fails_closed(self):
        with self.assertRaises(config_module.ConfigError):
            parse(self._body("        mode: notarized\n"))


class RoundTripTests(unittest.TestCase):
    """A target survives the trip the release hands its options to an adapter.

    The core passes each target to its adapter as the mapping `describe()`
    produced, and the adapter parses it back with this module. So for every
    optional field, "left out" and "written as the default" have to parse to the
    same target — otherwise a target that legitimately configures nothing is
    rejected the moment a real release hands it over, with an error about a
    field the repository never mentioned.
    """

    def _blank(self, **fields) -> dict:
        """The described target with optional fields written as their defaults.

        `describe()` emits every key so a reader can see the whole target, which
        is exactly why an unset optional has to arrive as `""` or `[]` rather
        than being missing.
        """

        described = dict(support.target().describe())
        described.update(fields)
        return described

    def test_a_fully_declared_target_round_trips(self):
        target = support.target()
        self.assertEqual(config_module.parse_release_target(target.describe()), target)

    def test_a_target_that_optionals_every_thing_round_trips(self):
        minimal = support.target(
            app_bundle=None,
            universal=False,
            hardened_runtime=False,
            publishers=(),
            binaries=(
                config_module.ReleaseBinary(
                    name="tool", identifier="com.example.tool"
                ),
            ),
        )
        self.assertEqual(
            config_module.parse_release_target(minimal.describe()), minimal
        )

    def test_optional_fields_written_empty_read_as_their_defaults(self):
        """A blank means "not declared", so it reads as what a target declares
        nothing to be — which is the same target with that field left out, not a
        target whose field was cleared."""

        reference = support.target(
            app_bundle=None,
            publishers=(),
            binaries=tuple(
                config_module.ReleaseBinary(
                    name=binary.name, identifier=binary.identifier
                )
                for binary in support.binaries()
            ),
        )
        described = dict(reference.describe())
        described.update(
            universal=None,
            hardened_runtime=None,
            publishers=None,
            app_bundle=None,
            signing={**reference.signing.describe(), "timestamp": None, "team_id_var": None},
            binaries=[
                {"name": binary.name, "identifier": binary.identifier,
                 "entitlements": None, "link_flags": []}
                for binary in reference.binaries
            ],
        )
        self.assertEqual(config_module.parse_release_target(described), reference)

    def test_an_empty_list_of_architectures_is_the_default_not_a_refusal(self):
        """`architectures: []` means "not declared", which means the defaults.

        Reading it as a refusal would reject a target that declares nothing
        about its architectures, which is a legitimate thing to do.
        """

        again = config_module.parse_release_target(self._blank(architectures=[]))
        self.assertEqual(again.architectures, support.target().architectures)

    def test_an_empty_list_of_link_flags_is_no_flags(self):
        """Flags live on the binary, and an empty list is "this binary adds none".

        The linker flags a product declares are how a target ships against a
        system framework; an empty list has to mean the default rather than
        failing a release that simply declares no extra flags.
        """

        binary = support.binaries()[0]
        described = self._blank(
            binaries=[
                {
                    "name": binary.name,
                    "identifier": binary.identifier,
                    "entitlements": None,
                    "link_flags": [],
                }
            ]
        )
        again = config_module.parse_release_target(described)
        self.assertEqual(again.binaries[0].link_flags, ())
        self.assertEqual(again.binaries[0].entitlements, "")

    def test_a_wrong_type_is_still_refused(self):
        """Emptiness is not a loophole.

        A value of the wrong shape is a mistake whether it is empty or not, and
        accepting it would turn "this repository wrote `architectures: {}`" into
        a release quietly built with default architectures.
        """

        for field, value in (
            ("architectures", {"arm64": True}),
            ("universal", "yes please"),
            ("publishers", "homebrew"),
            ("artifacts", 3),
            ("binaries", "tool"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(config_module.ConfigError):
                    config_module.parse_release_target(self._blank(**{field: value}))

    def test_an_empty_value_inside_a_list_is_still_refused(self):
        """`[arm64, ""]` is a list with a hole, not a list with one entry.

        Dropping the empty entry would ship an architecture the repository did
        not name in the position it did not name it.
        """

        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_release_target(self._blank(architectures=["arm64", ""]))
        self.assertIn("architectures", str(caught.exception))

    def test_a_duplicate_is_still_refused_after_the_empty_check(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.parse_release_target(self._blank(architectures=["arm64", "arm64"]))

    def test_the_public_reader_refuses_what_the_document_refuses(self):
        """One validator, so the two cannot drift.

        A caller holding a target already — the CLI reading a fragment, a test
        asserting on a declared target — gets the document's answers, not a
        second, looser opinion about the same configuration.
        """

        for field, value in (("adapter", "windows"), ("platform", "tvos")):
            with self.subTest(field=field):
                with self.assertRaises(config_module.ConfigError) as direct:
                    config_module.parse_release_target(self._blank(**{field: value}))
                self.assertIn(field, str(direct.exception))
                document = textwrap.indent(
                    _yaml_of(self._blank(**{field: value})), "    "
                )
                with self.assertRaises(config_module.ConfigError):
                    parse("version: 1\nrelease:\n  targets:\n" + document)

    def test_the_public_reader_names_where_the_target_came_from(self):
        """A refusal says which target, and where it was read from.

        A caller that hands over one of several targets needs to know which one
        was rejected without keeping a counter.
        """

        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse_release_target(self._blank(adapter="windows"), "fragment")
        self.assertIn("fragment", str(caught.exception))


def _yaml_of(described: dict) -> str:
    """A target mapping as YAML, for the document-level comparison."""

    lines = ["    - id: " + described["id"]]
    for key, value in described.items():
        if key == "id":
            continue
        lines.append(f"      {key}: {_yaml_value(value)}")
    return "\n".join(lines) + "\n"


def _yaml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_yaml_value(v)}" for k, v in value.items()) + "}"
    return repr(value)


if __name__ == "__main__":
    unittest.main()
