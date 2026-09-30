"""Shared release fixtures.

The signing material in these fixtures is generated, never real: the point is
to exercise the shape of a PKCS#12, not to stand up a certificate authority.
Every test here runs on a Linux runner, so the tests cover the *plan* — what
the adapter decided and why — and the runner's own guarantees. Running an
actual `codesign` is the macOS release job's job, not a unit test's.
"""

from __future__ import annotations

import base64
import textwrap
from typing import Any, Dict, Optional, Tuple

from continuum import config as config_module

P12_SECRET = "NANODICTATE_SIGNING_P12"
PASSWORD_SECRET = "NANODICTATE_SIGNING_PASSWORD"
IDENTITY = "NanoDictate CI Signing"

AGENT = "com.nanodictate.agent"
CONTROL = "com.nanodictate.ctl"
BUNDLE = "NanoDictate.app"

# The version a bundle release stamps into its Info.plist.
VERSION = "1.4.0"
VERSION_ENV = "CONTINUUM_RELEASE_VERSION"

# The commit a release is approved for. The adapter checks the checkout against
# this before it builds, so every walkable-adapter test needs it.
SHA = "a" * 40
OTHER_SHA = "b" * 40

# Not a certificate. A PKCS#12 is a binary container, and the adapter only
# needs to know that the transport is base64 and that the bytes are not empty.
FAKE_P12 = base64.b64encode(b"\x30\x82\x00\x00 not a real certificate").decode("ascii")
FAKE_PASSWORD = "not-the-real-password"


def environment(
    *,
    p12: Optional[str] = FAKE_P12,
    password: Optional[str] = FAKE_PASSWORD,
    version: Optional[str] = VERSION,
) -> Dict[str, str]:
    """A job environment, with the secrets present or explicitly blank."""

    env: Dict[str, str] = {}
    if p12 is not None:
        env[P12_SECRET] = p12
    if password is not None:
        env[PASSWORD_SECRET] = password
    if version is not None:
        env[VERSION_ENV] = version
    return env


def signing(**overrides: Any) -> config_module.ReleaseSigningSettings:
    values: Dict[str, Any] = {
        "mode": config_module.SIGNING_SELF_SIGNED_STABLE,
        "identity": IDENTITY,
        "p12_secret": P12_SECRET,
        "password_secret": PASSWORD_SECRET,
        "timestamp": False,
    }
    values.update(overrides)
    return config_module.ReleaseSigningSettings(**values)


def binaries() -> Tuple[config_module.ReleaseBinary, ...]:
    return (
        config_module.ReleaseBinary(
            name="NanoDictateAgent",
            identifier=AGENT,
            entitlements="Resources/com.nanodictate.agent.entitlements",
        ),
        config_module.ReleaseBinary(
            name="nanodictate",
            identifier=CONTROL,
            entitlements="Resources/com.nanodictate.ctl.entitlements",
        ),
    )


def app_bundle() -> config_module.ReleaseAppBundle:
    return config_module.ReleaseAppBundle(
        name=BUNDLE,
        identifier=AGENT,
        info_plist="packaging/Info.NanoDictateApp.plist",
        entitlements="Resources/com.nanodictate.agent.entitlements",
        resources=("config.example.toml",),
    )


def target(**overrides: Any) -> config_module.ReleaseTarget:
    values: Dict[str, Any] = {
        "id": "macos",
        "adapter": config_module.ADAPTER_APPLE,
        "platform": config_module.PLATFORM_MACOS,
        "build_strategy": config_module.BUILD_STRATEGY_SWIFTPM,
        "distribution": config_module.DISTRIBUTION_DIRECT,
        "architectures": (config_module.ARCH_X86_64, config_module.ARCH_ARM64),
        "universal": False,
        "artifacts": (config_module.ARTIFACT_TAR_GZ, config_module.ARTIFACT_ZIP),
        "hardened_runtime": True,
        "binaries": binaries(),
        "app_bundle": app_bundle(),
        "signing": signing(),
        "publishers": (config_module.PUBLISHER_HOMEBREW, config_module.PUBLISHER_MACPORTS),
    }
    values.update(overrides)
    return config_module.ReleaseTarget(**values)


def config(*targets: config_module.ReleaseTarget) -> config_module.ContinuumConfig:
    return config_module.ContinuumConfig(
        version=config_module.SCHEMA_VERSION,
        release=config_module.ReleaseSettings(targets=tuple(targets)),
    )


def document(body: str) -> str:
    return textwrap.dedent(body)


def nanodictate_document() -> str:
    """The exact shape a repository migrating from the reference flow writes."""

    return document(
        """
        version: 1
        release:
          targets:
            - id: macos
              adapter: apple
              platform: macos
              build_strategy: swiftpm
              distribution: direct
              architectures:
                - x86_64
                - arm64
              universal: false
              artifacts:
                - tar.gz
                - zip
              hardened_runtime: true
              publishers:
                - homebrew
                - macports
              binaries:
                - name: NanoDictateAgent
                  identifier: com.nanodictate.agent
                  entitlements: Resources/com.nanodictate.agent.entitlements
                - name: nanodictate
                  identifier: com.nanodictate.ctl
                  entitlements: Resources/com.nanodictate.ctl.entitlements
              app_bundle:
                name: NanoDictate.app
                identifier: com.nanodictate.agent
                info_plist: packaging/Info.NanoDictateApp.plist
                entitlements: Resources/com.nanodictate.agent.entitlements
                resources:
                  - config.example.toml
              signing:
                mode: self-signed-stable
                identity: "NanoDictate CI Signing"
                p12_secret: NANODICTATE_SIGNING_P12
                password_secret: NANODICTATE_SIGNING_PASSWORD
                timestamp: false
        """
    )


def fallback_document() -> str:
    """A stable target that has agreed up front to fall back to ad-hoc.

    This is the shape a fork-friendly repository writes: the fallback is
    declared, so the degraded build is a known state rather than a surprise.
    """

    return document(
        """
        version: 1
        release:
          targets:
            - id: macos
              adapter: apple
              platform: macos
              build_strategy: swiftpm
              distribution: direct
              architectures:
                - arm64
              universal: false
              artifacts:
                - tar.gz
              hardened_runtime: true
              binaries:
                - name: NanoDictateAgent
                  identifier: com.nanodictate.agent
                  entitlements: Resources/com.nanodictate.agent.entitlements
              app_bundle:
                name: NanoDictate.app
                identifier: com.nanodictate.agent
              signing:
                mode: self-signed-stable
                identity: "NanoDictate CI Signing"
                p12_secret: NANODICTATE_SIGNING_P12
                password_secret: NANODICTATE_SIGNING_PASSWORD
                allow_adhoc_fallback: true
        """
    )


def ios_document() -> str:
    """A target Continuum can type but not yet build.

    It must parse and be a valid document, so a repository can declare the
    intent while the iOS adapter is still being written.
    """

    return document(
        """
        version: 1
        release:
          targets:
            - id: ios
              adapter: apple
              platform: ios
              build_strategy: xcode-archive
              distribution: app-store
              architectures:
                - arm64
              universal: false
              artifacts:
                - pkg
              hardened_runtime: true
              app_bundle:
                name: NanoDictate.app
                identifier: com.nanodictate.agent
              signing:
                mode: self-signed-stable
                identity: "NanoDictate CI Signing"
                p12_secret: NANODICTATE_SIGNING_P12
                password_secret: NANODICTATE_SIGNING_PASSWORD
        """
    )


def load_target(path: str, target_id: str = "macos") -> config_module.ReleaseTarget:
    """Load a target out of a real configuration file, the way the CLI does."""

    loaded = config_module.load_config(path)
    found = loaded.release.target(target_id)
    if found is None:
        raise LookupError(f"no release target {target_id!r} in {path}")
    return found
