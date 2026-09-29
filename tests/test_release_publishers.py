"""The downstream package publishers: Homebrew and MacPorts.

These are the guarantees a publisher owes whatever package manager it targets,
checked once per publisher because a promise that holds for a formula and not
for a Portfile is not a promise. A digest that was not verified, a re-run that
commits the same bytes again, a destination that moved under the write, a
credential that is not there, a template with a hole in it — each has a stable
reason code, and each is asserted to produce that code from the publisher the
caller actually invoked.

The isolation tests at the end are the counterweight to the behaviour ones. It
is easy to make a publisher work by teaching it the one project it was written
for; the point of this package is that it does not know that project, so the
generic source is scanned for consumer and platform names the same way the
release core is.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import tempfile
import unittest

from continuum.release.publishers import (
    contract,
    get,
    homebrew,
    macports,
    names,
    parse_settings,
    validation,
)
from continuum.release.publishers.assets import InMemoryAssetFetcher, verify_artifact
from continuum.release.publishers.contract import (
    ALREADY_CURRENT,
    CONFIGURATION_INVALID,
    DIGEST_MISMATCH,
    NO_ARTIFACT,
    QUARANTINE_POLICY,
    TEMPLATE_UNKNOWN_TOKEN,
    ArtifactManifest,
    PublishRequest,
    PublishedArtifact,
    PublisherError,
    ReleaseIdentity,
)
from continuum.release.publishers.repository import (
    InMemoryPackageRepository,
    StaleDestination,
)
from continuum.release.publishers.validation import GeneratedFile, audit

VERSION = "1.4.0"
TAG = f"v{VERSION}"
BASE = f"https://github.com/acme/widget/releases/download/{TAG}"


def release() -> ReleaseIdentity:
    return ReleaseIdentity(repository="acme/widget", version=VERSION, download_base=BASE)


def artifact(name: str, *, kind: str = "archive", architecture: str = "", data=b"payload"):
    return PublishedArtifact(
        name=name,
        url=f"{BASE}/{name}",
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
        architecture=architecture,
    )


def manifest(*artifacts: PublishedArtifact) -> ArtifactManifest:
    return ArtifactManifest(release=release(), artifacts=tuple(artifacts))


def fetcher_for(*artifacts: PublishedArtifact, **overrides: bytes) -> InMemoryAssetFetcher:
    fetcher = InMemoryAssetFetcher()
    for item in artifacts:
        fetcher.add(item.url, overrides.get(item.name, b"payload"))
    return fetcher


def homebrew_settings(**over):
    settings = {
        "token": "widget",
        "destination": {
            "repository": "acme/homebrew-tap",
            "branch": "main",
            "mode": "trusted",
            "credential_secret": "TAP_TOKEN",
        },
        "formula": {
            "desc": "A widget",
            "homepage": "https://github.com/acme/widget",
            "license": "MIT",
            "install_paths": ["widget"],
        },
    }
    settings.update(over)
    return parse_settings("homebrew", settings)


def macports_settings(**over):
    settings = {
        "name": "widget",
        "tree": {
            "repository": "acme/macports-widget",
            "branch": "main",
            "mode": "trusted",
            "credential_secret": "TREE_TOKEN",
        },
        "portfile": {
            "name": "widget",
            "category": "sysutils",
            "license": "MIT",
            "maintainers": "{example.com:user @user} openmaintainer",
            "description": "A widget",
        },
    }
    settings.update(over)
    return parse_settings("macports", settings)


class ContractTests(unittest.TestCase):
    def test_a_download_base_must_be_pinned_to_its_tag(self):
        with self.assertRaises(PublisherError) as caught:
            ReleaseIdentity(
                repository="acme/widget",
                version=VERSION,
                download_base="https://github.com/acme/widget/releases/latest",
            )
        self.assertEqual(caught.exception.code, CONFIGURATION_INVALID)

    def test_an_empty_tag_defaults_from_the_version(self):
        identity = ReleaseIdentity(
            repository="acme/widget", version=VERSION, download_base=BASE
        )
        self.assertEqual(identity.tag, TAG)

    def test_a_manifest_refuses_the_same_asset_name_twice(self):
        with self.assertRaises(PublisherError):
            manifest(artifact("widget.tar.gz"), artifact("widget.tar.gz"))

    def test_retryability_is_decided_by_the_reason_not_by_prose(self):
        self.assertTrue(PublisherError("stale-destination", "x").retryable)
        self.assertFalse(PublisherError(DIGEST_MISMATCH, "x").retryable)

    def test_a_failure_result_carries_the_code_and_retryability(self):
        result = contract.PublishResult.failure(
            "homebrew", PublisherError(DIGEST_MISMATCH, "bad digest", remediation="re-cut")
        )
        self.assertEqual(result.status, contract.STATUS_FAILED)
        self.assertEqual(result.reason, DIGEST_MISMATCH)
        self.assertFalse(result.retryable)
        self.assertEqual(result.remediation, "re-cut")


class AssetVerificationTests(unittest.TestCase):
    def test_a_matching_asset_verifies(self):
        item = artifact("widget.tar.gz", data=b"good")
        verify_artifact(item, InMemoryAssetFetcher({item.url: b"good"}))

    def test_a_mismatched_digest_is_not_retryable(self):
        item = artifact("widget.tar.gz", data=b"good")
        with self.assertRaises(PublisherError) as caught:
            verify_artifact(item, InMemoryAssetFetcher({item.url: b"tampered"}))
        self.assertEqual(caught.exception.code, DIGEST_MISMATCH)
        self.assertFalse(caught.exception.retryable)

    def test_a_missing_asset_is_retryable(self):
        item = artifact("widget.tar.gz", data=b"good")
        with self.assertRaises(PublisherError) as caught:
            verify_artifact(item, InMemoryAssetFetcher({}))
        self.assertEqual(caught.exception.code, contract.ASSET_UNREACHABLE)
        self.assertTrue(caught.exception.retryable)


class SettingsTests(unittest.TestCase):
    def test_homebrew_without_a_destination_is_refused(self):
        with self.assertRaises(PublisherError) as caught:
            parse_settings("homebrew", {"token": "widget"})
        self.assertEqual(caught.exception.code, CONFIGURATION_INVALID)

    def test_an_unknown_setting_key_is_refused_by_name(self):
        with self.assertRaises(PublisherError) as caught:
            parse_settings(
                "homebrew",
                {
                    "token": "widget",
                    "destination": {"repository": "a/b", "credential_secret": "T"},
                    "formula": {"descr": "typo"},
                },
            )
        self.assertIn("descr", caught.exception.message)

    def test_an_inline_template_may_span_lines(self):
        parsed = parse_settings(
            "homebrew",
            {
                "token": "widget",
                "destination": {"repository": "a/b", "credential_secret": "T"},
                "formula": {"template": "class __CLASS__ < Formula\nend\n"},
            },
        )
        self.assertIn("\n", parsed.formula.template)

    def test_macports_requires_a_port_name(self):
        with self.assertRaises(PublisherError):
            parse_settings(
                "macports",
                {"name": "widget", "tree": {"repository": "a/b", "credential_secret": "T"},
                 "portfile": {"category": "sysutils"}},
            )


class RegistryTests(unittest.TestCase):
    def test_both_publishers_are_registered(self):
        self.assertEqual(names(), ("homebrew", "macports"))

    def test_an_unknown_publisher_is_refused_by_name(self):
        with self.assertRaises(PublisherError) as caught:
            get("chocolatey")
        self.assertIn("homebrew", caught.exception.message)

    def test_settings_are_parsed_through_the_named_publisher(self):
        parsed = parse_settings("homebrew", {"token": "widget",
                                             "destination": {"repository": "a/b",
                                                             "credential_secret": "T"}})
        self.assertEqual(parsed.token, "widget")


class HomebrewTests(unittest.TestCase):
    def test_a_per_architecture_release_generates_one_formula(self):
        arm, intel = artifact("Widget-arm64.tar.gz", architecture="arm64"), artifact(
            "Widget-x86_64.tar.gz", architecture="x86_64"
        )
        generated = homebrew.plan(
            PublishRequest(manifest=manifest(arm, intel), settings=homebrew_settings()),
            fetcher=fetcher_for(arm, intel),
        )
        self.assertEqual([item.path for item in generated], ["Formula/widget.rb"])
        content = generated[0].content
        self.assertIn("on_arm do", content)
        self.assertIn("on_intel do", content)
        self.assertIn("class Widget < Formula", content)
        self.assertIn(arm.sha256, content)
        self.assertIn(intel.sha256, content)

    def test_a_single_unversioned_archive_generates_a_plain_formula(self):
        item = artifact("widget-1.4.0.tar.gz")
        generated = homebrew.plan(
            PublishRequest(manifest=manifest(item), settings=homebrew_settings()),
            fetcher=fetcher_for(item),
        )
        content = generated[0].content
        self.assertIn(f'url "{item.url}"', content)
        self.assertIn(f'version "{VERSION}"', content)

    def test_a_cask_without_a_bundle_fails_unless_it_is_optional(self):
        item = artifact("widget-1.4.0.tar.gz")
        required = homebrew_settings(
            cask={"app_name": "Widget.app", "desc": "W", "homepage": "https://x.invalid"}
        )
        with self.assertRaises(PublisherError) as caught:
            homebrew.plan(
                PublishRequest(manifest=manifest(item), settings=required),
                fetcher=fetcher_for(item),
            )
        self.assertEqual(caught.exception.code, NO_ARTIFACT)

        optional = homebrew_settings(
            cask={
                "app_name": "Widget.app",
                "desc": "W",
                "homepage": "https://x.invalid",
                "optional": True,
            }
        )
        generated = homebrew.plan(
            PublishRequest(manifest=manifest(item), settings=optional), fetcher=fetcher_for(item)
        )
        self.assertEqual([item.surface for item in generated], ["formula"])

    def _bundle(self):
        return artifact("Widget.zip", kind="app-bundle")

    def _cask_only(self, **cask):
        surface = {"app_name": "Widget.app", "desc": "W", "homepage": "https://x.invalid"}
        surface.update(cask)
        return homebrew_settings(formula={"enabled": False}, cask=surface)

    def test_quarantine_may_not_be_cleared_for_a_vetted_artifact(self):
        item = self._bundle()
        with self.assertRaises(PublisherError) as caught:
            homebrew.plan(
                PublishRequest(
                    manifest=manifest(item),
                    settings=self._cask_only(
                        clear_quarantine=True,
                        quarantine_evidence="because",
                        notarized=True,
                    ),
                ),
                fetcher=fetcher_for(item),
            )
        self.assertEqual(caught.exception.code, QUARANTINE_POLICY)

    def test_quarantine_workaround_needs_evidence_and_is_audited(self):
        item = self._bundle()
        with self.assertRaises(PublisherError) as caught:
            homebrew.plan(
                PublishRequest(
                    manifest=manifest(item),
                    settings=self._cask_only(clear_quarantine=True),
                ),
                fetcher=fetcher_for(item),
            )
        self.assertEqual(caught.exception.code, QUARANTINE_POLICY)

        generated = homebrew.plan(
            PublishRequest(
                manifest=manifest(item),
                settings=self._cask_only(
                    clear_quarantine=True, quarantine_evidence="unsigned upstream build"
                ),
            ),
            fetcher=fetcher_for(item),
        )
        self.assertIn("com.apple.quarantine", generated[0].content)

    def test_a_template_with_an_undefined_token_is_refused(self):
        item = artifact("widget-1.4.0.tar.gz")
        with self.assertRaises(PublisherError) as caught:
            homebrew.plan(
                PublishRequest(
                    manifest=manifest(item),
                    settings=homebrew_settings(
                        formula={
                            "desc": "A widget",
                            "homepage": "https://github.com/acme/widget",
                            "license": "MIT",
                            "install_paths": ["widget"],
                            "template": 'url "__NOT_A_TOKEN__"',
                        }
                    ),
                ),
                fetcher=fetcher_for(item),
            )
        self.assertEqual(caught.exception.code, TEMPLATE_UNKNOWN_TOKEN)

    def test_publishing_is_idempotent(self):
        item = artifact("widget-1.4.0.tar.gz")
        request = PublishRequest(
            manifest=manifest(item),
            settings=homebrew_settings(),
            environment={"TAP_TOKEN": "t"},
        )
        repository = InMemoryPackageRepository(name="acme/homebrew-tap")
        first = homebrew.publish(request, repository, fetcher=fetcher_for(item))
        second = homebrew.publish(request, repository, fetcher=fetcher_for(item))
        self.assertEqual(first.status, contract.STATUS_PUBLISHED)
        self.assertEqual(second.reason, ALREADY_CURRENT)
        self.assertEqual(len(repository.commits), 1)

    def test_a_missing_credential_is_reported_not_guessed(self):
        item = artifact("widget-1.4.0.tar.gz")
        result = homebrew.publish(
            PublishRequest(manifest=manifest(item), settings=homebrew_settings()),
            InMemoryPackageRepository(name="acme/homebrew-tap"),
            fetcher=fetcher_for(item),
        )
        self.assertEqual(result.reason, contract.CREDENTIAL_MISSING)
        self.assertFalse(result.retryable)

    def test_a_stale_destination_is_reconciled_against_the_new_head(self):
        item = artifact("widget-1.4.0.tar.gz")
        request = PublishRequest(
            manifest=manifest(item),
            settings=homebrew_settings(),
            environment={"TAP_TOKEN": "t"},
        )
        repository = _AdvancingRepository(name="acme/homebrew-tap")
        result = homebrew.publish(request, repository, fetcher=fetcher_for(item))
        self.assertEqual(result.status, contract.STATUS_PUBLISHED)
        self.assertGreaterEqual(repository.attempts, 2)
        self.assertEqual(len(repository.commits), 1)


class _AdvancingRepository(InMemoryPackageRepository):
    """A destination that moves once, under the first write attempt."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._moved = False
        self.attempts = 0

    def write(self, files, *, message, expected_head, branch):
        self.attempts += 1
        if not self._moved:
            self._moved = True
            self.advance()
            raise StaleDestination(expected_head, self.head, self.name)
        return super().write(
            files, message=message, expected_head=expected_head, branch=branch
        )


class MacPortsTests(unittest.TestCase):
    def test_the_built_in_portfile_names_the_version_and_checksum(self):
        item = artifact("widget-1.4.0.tar.gz")
        generated = macports.plan(
            PublishRequest(manifest=manifest(item), settings=macports_settings()),
            fetcher=fetcher_for(item),
        )
        self.assertEqual([entry.path for entry in generated], ["sysutils/widget/Portfile"])
        content = generated[0].content
        self.assertIn("github.setup        acme widget 1.4.0", content)
        self.assertIn(f"checksums           sha256  {item.sha256}", content)
        self.assertIn("livecheck.type      github", content)
        self.assertIn("use_configure       no", content)
        self.assertIn("build               {}", content)

    def test_extra_files_land_in_the_port_directory(self):
        item = artifact("widget-1.4.0.tar.gz")
        source_root = tempfile.mkdtemp(prefix="continuum-macports-test-")
        self.addCleanup(shutil.rmtree, source_root, True)
        with open(os.path.join(source_root, "config.example.toml"), "w", encoding="utf-8") as fh:
            fh.write("[widget]\n")
        settings = macports_settings(files={"config.example.toml": "config.example.toml"})
        repository = InMemoryPackageRepository(name="acme/macports-widget")
        result = macports.publish(
            PublishRequest(
                manifest=manifest(item), settings=settings, environment={"TREE_TOKEN": "t"}
            ),
            repository,
            fetcher=fetcher_for(item),
            source_root=source_root,
        )
        self.assertEqual(result.status, contract.STATUS_PUBLISHED)
        self.assertIn("sysutils/widget/config.example.toml", result.files)
        self.assertIsNotNone(
            repository.read_file("sysutils/widget/config.example.toml")
        )

    def test_the_installer_pin_tracks_the_tree_revision(self):
        item = artifact("widget-1.4.0.tar.gz")
        settings = macports_settings(
            installer={
                "destination": {
                    "repository": "acme/widget",
                    "branch": "main",
                    "mode": "trusted",
                    "credential_secret": "INSTALLER_TOKEN",
                },
                "path": "scripts/install-macports.sh",
            }
        )
        request = PublishRequest(
            manifest=manifest(item),
            settings=settings,
            environment={"TREE_TOKEN": "t", "INSTALLER_TOKEN": "i"},
        )
        tree = InMemoryPackageRepository(name="acme/macports-widget")
        installer = InMemoryPackageRepository(name="acme/widget")
        installer.seed("scripts/install-macports.sh", 'PIN_REV="old"\n')
        result = macports.publish(
            request, tree, fetcher=fetcher_for(item), installer_repository=installer
        )
        self.assertEqual(result.status, contract.STATUS_PUBLISHED)
        self.assertIn(f'PIN_REV="{result.revision}"', installer.read_file("scripts/install-macports.sh"))

    def test_a_rerun_pins_nothing_new(self):
        item = artifact("widget-1.4.0.tar.gz")
        settings = macports_settings(
            installer={
                "destination": {"repository": "acme/widget", "credential_secret": "INSTALLER_TOKEN"},
                "path": "scripts/install-macports.sh",
            }
        )
        request = PublishRequest(
            manifest=manifest(item),
            settings=settings,
            environment={"TREE_TOKEN": "t", "INSTALLER_TOKEN": "i"},
        )
        tree = InMemoryPackageRepository(name="acme/macports-widget")
        installer = InMemoryPackageRepository(name="acme/widget")
        installer.seed("scripts/install-macports.sh", 'PIN_REV="old"\n')
        macports.publish(request, tree, fetcher=fetcher_for(item), installer_repository=installer)
        again = macports.publish(
            request, tree, fetcher=fetcher_for(item), installer_repository=installer
        )
        self.assertEqual(again.reason, ALREADY_CURRENT)
        self.assertEqual(len(tree.commits), 1)
        self.assertEqual(len(installer.commits), 1)

    def test_the_pin_is_anchored_to_the_whole_line(self):
        rewritten = macports.pin_content(
            'PIN_REV_OLD="x"\nPIN_REV="y"\n', "PIN_REV", "abc", "installer.sh"
        )
        self.assertEqual(rewritten, 'PIN_REV_OLD="x"\nPIN_REV="abc"\n')

    def test_a_pin_line_that_is_not_there_is_a_configuration_error(self):
        with self.assertRaises(PublisherError) as caught:
            macports.pin_content("#!/bin/sh\n", "PIN_REV", "abc", "installer.sh")
        self.assertEqual(caught.exception.code, CONFIGURATION_INVALID)


class IsolationTests(unittest.TestCase):
    """The generic source must not know the project it was written for.

    The same reasoning as the release core's purity test: a package manager
    module that names a consumer cannot publish a second consumer, and the
    second consumer is the cheapest way to discover that. Prose is exempt, parsed
    rather than regexed, so an explanation of the boundary is not mistaken for
    crossing it.
    """

    CONSUMER_NAMES = ("nanodictate", "kodmial", "pr-agent", "pr_agent")

    def modules(self):
        import continuum.release.publishers as package

        directory = os.path.dirname(package.__file__)
        for name in sorted(os.listdir(directory)):
            if name.endswith(".py"):
                yield os.path.join(directory, name)

    def executable_source(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) in docstrings:
                node.value = ""
        kept = [
            node
            for node in ast.walk(tree)
            if not (isinstance(node, ast.Constant) and id(node) in docstrings)
        ]
        return "\n".join(ast.unparse(node) for node in kept)

    def test_no_generic_module_names_a_consumer(self):
        for path in self.modules():
            code = self.executable_source(path)
            for term in self.CONSUMER_NAMES:
                with self.subTest(module=os.path.basename(path), term=term):
                    self.assertIsNone(
                        re.search(rf"\b{re.escape(term)}\w*", code, re.IGNORECASE),
                        f"{os.path.basename(path)} refers to {term!r} in code",
                    )

    def test_the_publishers_do_not_import_the_release_state_machine(self):
        for path in self.modules():
            imported = set()
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            for name in imported:
                with self.subTest(module=os.path.basename(path), imported=name):
                    self.assertNotIn("release.run", name)
                    self.assertNotIn("release.adapters", name)
                    self.assertNotIn("release.config", name)


class ValidationAuditTests(unittest.TestCase):
    def test_a_checksum_the_release_never_published_is_refused(self):
        item = artifact("widget-1.4.0.tar.gz")
        forged = GeneratedFile(
            path="Formula/widget.rb",
            content=f'sha256 "{"0" * 64}"\nversion "{VERSION}"\n',
            surface="formula",
            selected=(item,),
        )
        report = audit(forged, manifest(item))
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, contract.VALIDATION_FAILED)


class VersionTokenBoundaryTests(unittest.TestCase):
    """The version check is a token claim, not a substring claim.

    The incident these pin: a generated manifest left pinned to the *previous*
    release is a silent downgrade -- a package manager installs the old bytes
    and reports success -- and the only automated signal is the version
    assertion. A substring assertion cannot see that failure, because
    `0.1.1` occurs inside `0.1.10`: the wrong release satisfies a check written
    for the right one. Prefix, suffix, and embedded cases are all the same bug.
    """

    def test_a_longer_version_that_starts_with_the_release_is_not_a_match(self):
        self.assertFalse(validation.names_version('version "0.1.10"\n', "0.1.1"))

    def test_a_shorter_version_that_the_release_starts_with_is_not_a_match(self):
        self.assertFalse(validation.names_version('version "0.1"\n', "0.1.1"))

    def test_the_exact_version_matches(self):
        self.assertTrue(validation.names_version('version "0.1.1"\n', "0.1.1"))

    def test_a_version_is_matched_as_a_whole_token_anywhere_on_a_line(self):
        for line in ('version "0.1.1"', "tag=v0.1.1", "# pinned 0.1.1", "0.1.1,"):
            self.assertTrue(validation.names_version(line + "\n", "0.1.1"), line)

    def test_an_adjacent_character_anywhere_breaks_the_match(self):
        for line in ("version 10.1.1", "version 0.1.1a", "version x0.1.1", "version 0.1.1-rc1"):
            self.assertFalse(validation.names_version(line + "\n", "0.1.1"), line)

    def test_an_empty_version_never_matches(self):
        self.assertFalse(validation.names_version("version ''\n", ""))

    def test_a_manifest_pinned_to_a_longer_version_fails_the_audit(self):
        # The end-to-end shape of the incident: the audit must refuse, not
        # quietly pass, when only a longer version sharing the prefix is named.
        item = artifact("widget-1.4.0.tar.gz")
        stale = GeneratedFile(
            path="Formula/widget.rb",
            content=(
                f'sha256 "{item.sha256}"\n'
                f'version "{VERSION}0"\n'  # 1.4.00 shares 1.4.0 as a prefix
            ),
            surface="formula",
            selected=(item,),
        )
        report = audit(stale, manifest(item))
        self.assertFalse(report.ok)
        self.assertIn(
            "version-present",
            [outcome.name for outcome in report.checks if outcome.status == "failed"],
        )


class CommentAwareTokenScanTests(unittest.TestCase):
    """A comment cannot carry an unfilled token into a failure.

    The incident these pin: the generated files explain themselves, so the
    header of a formula legitimately spells out `__SHA256__`, `__VERSION__`, and
    the rest of the tokens the template fills. A scan for unfilled tokens over
    the whole file therefore reported the *documentation* of the placeholders as
    the failure, which makes a correct manifest red and teaches everyone to
    ignore the check -- at exactly the moment a manifest with a real hole in it
    ships.
    """

    HEADER = (
        "# Generated file. Replace __SHA256__ with the release digest and\n"
        "# __VERSION__ with the release version; __URL__ points at the asset.\n"
        "# __TOKEN__ is not a real token, and neither is __SHA__.\n"
    )

    def test_a_comment_line_does_not_supply_an_unfilled_token(self):
        self.assertEqual(validation.unfilled_template_tokens(self.HEADER), ())

    def test_a_comment_line_is_not_an_executed_line(self):
        self.assertEqual(validation.active_lines(self.HEADER), [])

    def test_a_live_line_with_a_hole_is_still_reported(self):
        self.assertEqual(
            validation.unfilled_template_tokens(f'sha256 "__SHA256__"\n'), ("SHA256",)
        )

    def test_a_hole_beside_documentation_is_still_reported(self):
        content = self.HEADER + 'url "__URL__"\n'
        self.assertEqual(validation.unfilled_template_tokens(content), ("URL",))

    def test_every_documented_comment_style_is_ignored(self):
        for prefix in ("#", "//", ";", "--"):
            line = f"{prefix} documents __VERSION__\n"
            self.assertTrue(validation.is_comment_line(line), prefix)
            self.assertEqual(validation.unfilled_template_tokens(line), (), prefix)

    def test_indentation_does_not_turn_a_comment_into_an_executed_line(self):
        line = "    # indented, and still a comment: __VERSION__\n"
        self.assertTrue(validation.is_comment_line(line))
        self.assertEqual(validation.unfilled_template_tokens(line), ())

    def test_a_generated_file_documenting_its_tokens_audits_clean(self):
        item = artifact("widget-1.4.0.tar.gz")
        documented = GeneratedFile(
            path="Formula/widget.rb",
            content=(
                self.HEADER
                + f'sha256 "{item.sha256}"\n'
                + f'version "{VERSION}"\n'
                + f'url "{item.name}"\n'
            ),
            surface="formula",
            selected=(item,),
        )
        report = audit(documented, manifest(item))
        self.assertTrue(
            report.ok, [outcome for outcome in report.checks if outcome.status != "passed"]
        )

    def test_a_live_hole_fails_the_audit_even_when_comments_document_the_tokens(self):
        item = artifact("widget-1.4.0.tar.gz")
        holed = GeneratedFile(
            path="Formula/widget.rb",
            content=(
                self.HEADER
                + f'sha256 "{item.sha256}"\n'
                + f'version "{VERSION}"\n'
                + 'url "__URL__"\n'
            ),
            surface="formula",
            selected=(item,),
        )
        report = audit(holed, manifest(item))
        self.assertFalse(report.ok)
        self.assertIn(
            "template-tokens-filled",
            [outcome.name for outcome in report.checks if outcome.status == "failed"],
        )


if __name__ == "__main__":
    unittest.main()
