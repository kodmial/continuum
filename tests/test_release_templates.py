"""Static contracts for the archived release reference templates.

The files under ``examples/release-templates/`` are reference material, not a
live release implementation. These tests keep them inert, product-neutral, and
traceable to the historical source, and they prove that the macOS policy example
still parses through the current configuration schema.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any, Dict

import yaml

from continuum import config as config_module


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "examples" / "release-templates"
HISTORICAL_SHA = "0bbf57dbb8834d0aaef2cfb83f08ead7da623985"

GENERIC = "generic-release-workflow.yml"
HANDOFF = "consumer-release-handoff.yml"
BUN = "bun-binary-release-workflow.yml"
MACOS_CALLER = "macos-release-caller.yml"
MACOS_TARGET = "macos-target.continuum.yml"

PROVENANCE = {
    GENERIC: ".github/workflows/release.yml",
    HANDOFF: "fixtures/consumer-repo/.github/workflows/continuum-release.yml",
    BUN: ".github/workflows/release-bun-binary.yml",
}


def load(name: str) -> Dict[str, Any]:
    document = yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{name} must be a YAML mapping"
    return document


def events(document: Dict[str, Any]) -> Dict[str, Any]:
    # PyYAML follows YAML 1.1 and reads the bare ``on`` key as boolean True.
    value = document.get("on")
    if value is None:
        value = document.get(True)
    assert isinstance(value, dict), "workflow must declare an event mapping"
    return value


class ReleaseTemplateTests(unittest.TestCase):
    def test_reference_templates_are_explicitly_inactive(self):
        for name in (GENERIC, HANDOFF, BUN):
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("ARCHIVED", text, name)
            self.assertIn("NOT QUALIFIED", text, name)
            self.assertIn(HISTORICAL_SHA, text, name)
            self.assertIn(PROVENANCE[name], text, name)

        for name in (MACOS_CALLER, MACOS_TARGET):
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            self.assertIn("INACTIVE", text, name)
            self.assertIn("NOT QUALIFIED", text, name)

        target_text = (TEMPLATES / MACOS_TARGET).read_text(encoding="utf-8")
        self.assertIn("NOT A WORKFLOW", target_text)

    def test_generic_workflow_preserves_the_release_plane(self):
        document = load(GENERIC)
        self.assertEqual(set(events(document)), {"workflow_call"})

        call = events(document)["workflow_call"]
        inputs = set(call["inputs"])
        self.assertLessEqual(
            {
                "version",
                "source_sha",
                "event",
                "tag",
                "channel",
                "config_path",
                "fragment_root",
                "environment",
                "signing_material",
                "include_declared",
                "prerelease",
                "immutable",
                "attest",
            },
            inputs,
        )
        self.assertTrue(call["secrets"]["RELEASE_TOKEN"]["required"])
        self.assertLessEqual(
            {
                "CONTINUUM_APPLE_P12",
                "CONTINUUM_APPLE_P12_PASSWORD",
                "CONTINUUM_ANDROID_KEYSTORE",
                "CONTINUUM_ANDROID_KEYSTORE_PASSWORD",
                "CONTINUUM_GPG_PRIVATE_KEY",
                "CONTINUUM_GPG_PASSPHRASE",
            },
            set(call["secrets"]),
        )

        jobs = document["jobs"]
        self.assertLessEqual({"resolve", "target", "transaction"}, set(jobs))
        self.assertEqual(jobs["target"]["needs"], "resolve")
        self.assertEqual(jobs["transaction"]["needs"], ["resolve", "target"])
        self.assertIn(
            "fromJSON(needs.resolve.outputs.rows)",
            str(jobs["target"]["strategy"]["matrix"]["include"]),
        )

        self.assertEqual(document["permissions"], {"contents": "read"})
        for job_name in ("resolve", "target"):
            self.assertEqual(jobs[job_name]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["transaction"]["permissions"], {"contents": "write"})

        text = (TEMPLATES / GENERIC).read_text(encoding="utf-8")
        for command in ("release resolve", "release target", "release transaction"):
            self.assertIn(command, text)

    def test_consumer_handoff_is_deliberately_thin(self):
        document = load(HANDOFF)
        self.assertEqual(set(events(document)), {"workflow_dispatch"})
        self.assertTrue(events(document)["workflow_dispatch"]["inputs"]["source_sha"]["required"])
        self.assertNotIn("secrets", document)
        self.assertEqual(document["permissions"], {"contents": "read"})

        job = document["jobs"]["release"]
        self.assertEqual(len(job["steps"]), 1)
        self.assertIn("::notice::Release hand-off", job["steps"][0]["run"])

    def test_bun_workflow_is_an_optional_technology_example(self):
        document = load(BUN)
        self.assertEqual(set(events(document)), {"workflow_call"})
        inputs = set(events(document)["workflow_call"]["inputs"])
        self.assertLessEqual(
            {"source_sha", "package_dir", "binary_path", "asset_name"}, inputs
        )
        text = (TEMPLATES / BUN).read_text(encoding="utf-8")
        self.assertIn("setup-bun", text)
        self.assertIn("OPTIONAL, NOT THE DEFAULT ARCHITECTURE", text)

    def test_macos_caller_matches_the_generic_workflow_surface(self):
        generic = load(GENERIC)
        call = events(generic)["workflow_call"]
        generic_inputs = set(call["inputs"])
        generic_secrets = set(call["secrets"])

        caller = load(MACOS_CALLER)
        self.assertEqual(set(events(caller)), {"workflow_dispatch"})
        job = caller["jobs"]["release"]
        self.assertTrue(
            job["uses"].startswith("kodmial/continuum/.github/workflows/release.yml@"),
            job["uses"],
        )
        self.assertTrue(job["uses"].endswith(HISTORICAL_SHA), job["uses"])
        self.assertTrue(set(job["with"]).issubset(generic_inputs))
        self.assertTrue(set(job["secrets"]).issubset(generic_secrets))
        self.assertEqual(job["with"]["event"], "workflow-dispatch")
        self.assertEqual(job["with"]["tag"], "")
        self.assertEqual(job["with"]["config_path"], ".continuum.yml")
        self.assertEqual(job["with"]["signing_material"], "present")
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.assertEqual(
            set(job["secrets"]),
            {
                "RELEASE_TOKEN",
                "CONTINUUM_APPLE_P12",
                "CONTINUUM_APPLE_P12_PASSWORD",
            },
        )

    def test_macos_target_is_policy_not_a_workflow(self):
        document = load(MACOS_TARGET)
        self.assertNotIn("jobs", document)
        self.assertNotIn("on", document)
        self.assertNotIn(True, document)
        self.assertEqual(document["version"], config_module.SCHEMA_VERSION)
        self.assertEqual(len(document["release"]["targets"]), 1)

    def test_macos_target_parses_with_the_historical_schema(self):
        text = (TEMPLATES / MACOS_TARGET).read_text(encoding="utf-8")
        config = config_module.parse_config(text)
        self.assertTrue(config.release.enabled)
        self.assertEqual(len(config.release.targets), 1)

        target = config.release.targets[0]
        self.assertEqual(target.id, "macos-app")
        self.assertEqual(target.adapter, config_module.ADAPTER_APPLE)
        self.assertEqual(target.platform, config_module.PLATFORM_MACOS)
        self.assertEqual(target.build_strategy, config_module.BUILD_STRATEGY_SWIFTPM)
        self.assertEqual(target.distribution, config_module.DISTRIBUTION_DIRECT)
        self.assertEqual(target.architectures, ("arm64", "x86_64"))
        self.assertFalse(target.universal)
        self.assertTrue(target.hardened_runtime)
        self.assertEqual(target.artifacts, ("tar.gz", "zip"))
        self.assertEqual(target.publishers, ("homebrew", "macports"))
        self.assertTrue(target.is_mvp_executable)

        self.assertEqual(len(target.binaries), 1)
        binary = target.binaries[0]
        self.assertEqual(binary.name, "ExampleApp")
        self.assertEqual(binary.identifier, "com.example.app")
        self.assertEqual(binary.entitlements, "macos/ExampleApp.entitlements")

        bundle = target.app_bundle
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.name, "ExampleApp.app")
        self.assertEqual(bundle.identifier, "com.example.app")
        self.assertEqual(bundle.info_plist, "macos/Info.plist")
        self.assertEqual(bundle.entitlements, "macos/ExampleApp.entitlements")

        signing = target.signing
        self.assertEqual(signing.mode, config_module.SIGNING_SELF_SIGNED_STABLE)
        self.assertEqual(signing.identity, "ExampleApp CI Signing")
        self.assertFalse(signing.timestamp)
        self.assertFalse(signing.allow_adhoc_fallback)
        self.assertEqual(
            signing.secret_names(),
            ("CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"),
        )
        self.assertEqual(
            config.release.describe()["required_secrets"],
            ["CONTINUUM_APPLE_P12", "CONTINUUM_APPLE_P12_PASSWORD"],
        )

    def test_examples_contain_no_first_consumer_identifiers(self):
        banned = re.compile(r"nanodictate", re.IGNORECASE)
        for path in sorted(TEMPLATES.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                banned.search(text),
                f"{path.relative_to(ROOT)} must not carry a first-consumer identifier",
            )
            self.assertNotIn("com.nanodictate", text.lower())

    def test_templates_are_not_installed_or_active(self):
        active_dirs = [
            ROOT / ".github" / "workflows",
            ROOT / ".github" / "caller-stubs",
        ]
        active_names = {
            path.name
            for directory in active_dirs
            for path in directory.rglob("*.yml")
        }
        template_names = {path.name for path in TEMPLATES.rglob("*.yml")}
        self.assertTrue(template_names, "release templates are missing")
        self.assertFalse(template_names & active_names)
        self.assertNotIn(
            "release-templates",
            (ROOT / "install.sh").read_text(encoding="utf-8"),
        )

    def test_documentation_is_discoverable_and_maps_the_templates(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(readme.count("docs/release-templates.md"), 1)

        docs = (ROOT / "docs" / "release-templates.md").read_text(encoding="utf-8")
        for token in (
            HISTORICAL_SHA,
            "ARCHIVED",
            "HISTORICAL",
            "INACTIVE",
            "UNQUALIFIED",
        ):
            self.assertIn(token, docs)
        for name in (GENERIC, HANDOFF, BUN, MACOS_CALLER, MACOS_TARGET):
            self.assertIn(name, docs)


if __name__ == "__main__":
    unittest.main()
