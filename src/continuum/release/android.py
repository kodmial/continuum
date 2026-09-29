"""The Android release adapter: build once, sign, verify, tear down.

This is the module that knows about Gradle, keystores, `apksigner`, and
`jarsigner`. Nothing else in Continuum does.

Android is not one distribution channel wearing two names. A signed **APK** is
installed directly — from a GitHub Release, a sideload, or a third-party store —
and an **AAB** is not installable at all: it is an upload container that Google
Play splits and re-signs for every device. The two therefore have different
signing stories and this adapter keeps them apart:

* **APK** is signed with the project's signing key. For a project that has
  enrolled in Play App Signing that key is the *upload key*, and the same APK
  signature is what a user's device verifies when they install the artifact
  directly. The artifact is verified with `apksigner verify --print-certs`
  before it is allowed to leave the job.
* **AAB** is signed with the upload key only. Google Play holds the app-signing
  key. Signing an AAB is what proves the uploader is the same publisher the
  app-signing key was registered under; it is not the signature users see, and
  the adapter says so rather than pretending the bundle carries the final
  signature.

The properties this adapter refuses to give up, each of which is a way a
release has gone wrong before:

* **One build, one source SHA.** Every requested output is produced by a single
  Gradle invocation from the exact approved commit, and the working tree's
  `HEAD` is asserted to be that commit first. Building an APK and an AAB in two
  invocations is how the two outputs end up from two different revisions.
* **`versionName` is the release version, `versionCode` is derived from it.**
  The normalized Continuum version is the only input. A version code is an
  integer that can never go backwards (Play rejects a lower one), so it is
  computed from the semantic version by a policy rather than typed in, and an
  explicit override is allowed only when configuration states one.
* **The keystore is materialized for one job and removed in `finally`.** Key
  material is a repository secret; it is decoded into a scratch file, used, and
  deleted whatever happens next. A keystore is never committed and never
  printed.
* **A production target fails closed without signing material.** A build that
  cannot find its keystore produces an unsigned artifact that installs nothing
  and uploads nowhere; the adapter refuses rather than shipping it.
* **The build is isolated behind an injectable command runner.** Tests exercise
  the whole decision — task list, version binding, signing order, verification,
  cleanup — without Gradle, a JDK, or an Android SDK on the machine.

The adapter implements the normalized `target-adapter` port from the release
contract: `build`, `sign`, and `verify`, each taking a `BuildRequest` and the
manifest so far. Its options arrive through `TargetSpec.options` untouched, so
no platform detail leaks into the core state machine.
"""

from __future__ import annotations

import base64
import glob
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import (
    PROVENANCE_DECLARED,
    SIGNING_SIGNED,
    TYPE_INSTALLER,
    TYPE_PACKAGE,
    TYPE_PROVENANCE,
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    TargetSpec,
    VerificationReport,
)

# The adapter's own name. It is what a target declares and what a manifest
# records, and it is deliberately local: the core reads it, it never defines it.
ADAPTER_NAME = "android"

# The two outputs a target may ask for, independently. `apk` is the default
# because the direct-distribution profile must work with no Google credentials
# at all; a project that wants a bundle names `aab` (or both) explicitly.
OUTPUT_APK = "apk"
OUTPUT_AAB = "aab"
SUPPORTED_OUTPUTS: Tuple[str, ...] = (OUTPUT_APK, OUTPUT_AAB)

# The Gradle wrapper is the default build entry point: a repository that commits
# `gradlew` has pinned its own Gradle, and reaching for a system `gradle` would
# build with whatever version the runner happens to carry.
DEFAULT_GRADLEW = "./gradlew"

# Tool names, resolved through PATH. Named as constants so a test can assert on
# the invocation and so a future absolute-pinning change has one place to land.
APKSIGNER = "apksigner"
JARSIGNER = "jarsigner"

# The environment variables the adapter reads. Configuration names a secret
# (`MYAPP_UPLOAD_KEYSTORE`); the job exposes it under that name; the adapter
# reads it under a fixed name. `bind_material` is the only join.
KEYSTORE_ENV = "CONTINUUM_ANDROID_KEYSTORE"
KEYSTORE_PASSWORD_ENV = "CONTINUUM_ANDROID_KEYSTORE_PASSWORD"
KEY_PASSWORD_ENV = "CONTINUUM_ANDROID_KEY_PASSWORD"

# The Gradle properties a build defines so the project can bind its
# `versionName`/`versionCode` without the adapter rewriting a build file. They
# are ordinary values, not secrets, and they come from the release being cut.
VERSION_NAME_PROPERTY = "continuum.versionName"
VERSION_CODE_PROPERTY = "continuum.versionCode"

# Android's `versionCode` is a 32-bit signed integer in the manifest. Google
# Play rejects a code lower than or equal to the highest already uploaded, and
# an overflow becomes a negative number that no device accepts.
MAX_VERSION_CODE = 2_100_000_000

_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MODULE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# A key alias is the name of a private-key entry in the keystore, and both
# signing tools take it as an argument, so it is an identifier and nothing else.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# The signing *identity* is what the signed artifact is expected to carry: a
# certificate DN, or a SHA-256 fingerprint of the signing certificate. It is
# compared against what the verification tool prints, so it is allowed to contain
# the spaces and commas a distinguished name has — and nothing that would let one
# identity be a second, differently-spelled one.
_IDENTITY_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class AndroidError(ContractError):
    """The Android adapter cannot honestly continue.

    Carries a stable code, a message safe to print, and a retryable flag, the
    same shape every classified failure in the release core uses.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        remediation: str = "",
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)
        self.remediation = remediation


# -- version binding --------------------------------------------------------


def parse_semver(version: str) -> Tuple[int, int, int]:
    match = _SEMVER_RE.match((version or "").strip().lstrip("vV"))
    if match is None:
        raise AndroidError(
            "version-malformed",
            f"{version!r} is not a semantic version, so it has no Android "
            "versionName and no versionCode can be derived from it",
        )
    return int(match.group("major")), int(match.group("minor")), int(match.group("patch"))


def version_name_for(version: str) -> str:
    """The `versionName` a release version stamps.

    A prerelease suffix is kept: `1.2.3-rc.1` is a different build from `1.2.3`,
    and a Play track that receives it must be able to tell them apart.
    """

    text = (version or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if _SEMVER_RE.match(text) is None:
        raise AndroidError(
            "version-malformed",
            f"{version!r} is not a semantic version and cannot be a versionName",
        )
    return text


def version_code_for(version: str, *, offset: int = 0) -> int:
    """The monotonic `versionCode` a semantic version maps to.

    `major * 1_000_000 + minor * 1_000 + patch` is the long-standing Android
    convention: it keeps ordering, leaves room for 1000 patches and 1000 minors,
    and fits the signed-32-bit field with margin. `offset` moves the whole series
    for a project that already shipped codes under another scheme.
    """

    major, minor, patch = parse_semver(version)
    if minor > 999 or patch > 999:
        raise AndroidError(
            "version-code-out-of-range",
            f"{version} has a minor or patch component too large for the Android "
            "versionCode scheme; bump the major version instead",
        )
    code = major * 1_000_000 + minor * 1_000 + patch + offset
    if not 1 <= code <= MAX_VERSION_CODE:
        raise AndroidError(
            "version-code-out-of-range",
            f"{version} maps to versionCode {code}, which is outside Android's "
            f"1..{MAX_VERSION_CODE} range",
        )
    return code


# -- configuration ----------------------------------------------------------


def _require_mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AndroidError(
            "configuration-invalid", f"{where} must be a mapping, got {type(value).__name__}"
        )
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise AndroidError(
            "configuration-invalid",
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed)}",
        )


def _choice(value: Any, where: str, choices: Sequence[str], default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise AndroidError(
            "configuration-invalid",
            f"{where} must be one of {', '.join(choices)}; got {value!r}",
        )
    return value


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AndroidError("configuration-invalid", f"{where} must be true or false")
    return value


def _int(value: Any, where: str, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise AndroidError("configuration-invalid", f"{where} must be an integer")
    if not minimum <= value <= maximum:
        raise AndroidError(
            "configuration-invalid", f"{where} must be between {minimum} and {maximum}"
        )
    return value


def _name(value: Any, where: str, *, required: bool = True) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        # An empty value and an absent one mean the same thing: this target
        # names no such secret. Treating them differently would let a
        # configuration that blanked a name validate as though it had set it.
        if required:
            raise AndroidError(
                "configuration-invalid",
                f"{where} is required: a signing secret is referenced by repository "
                "secret name, never by value",
            )
        return ""
    if not isinstance(value, str) or not _NAME_RE.match(value.strip()):
        raise AndroidError(
            "configuration-invalid",
            f"{where} must be an upper-case repository secret name; got {value!r}",
        )
    return value.strip()


def _text(value: Any, where: str, default: str = "", *, pattern: Optional[re.Pattern] = None) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise AndroidError("configuration-invalid", f"{where} must be a non-empty string")
    text = value.strip()
    if pattern is not None and not pattern.match(text):
        raise AndroidError("configuration-invalid", f"{where} has an invalid form; got {value!r}")
    return text


@dataclass(frozen=True)
class AndroidSettings:
    """One target's Android build and signing configuration.

    The shape is deliberately the ten things a command-line Gradle build needs
    and nothing about Google Play: a target that publishes only an AAB never has
    a track, and a target that publishes only an APK never learns Play exists.
    """

    module: str = "app"
    variant: str = "release"
    flavor: str = ""
    outputs: Tuple[str, ...] = (OUTPUT_APK,)
    keystore_secret: str = ""
    keystore_password_secret: str = ""
    key_alias: str = ""
    key_password_secret: str = ""
    signing_identity: str = ""
    play_app_signing: bool = False
    production: bool = False
    require_signing: bool = True
    version_code: int = 0
    version_code_offset: int = 0
    gradlew: str = DEFAULT_GRADLEW
    mapping_path: str = ""
    native_symbols_path: str = ""

    def __post_init__(self) -> None:
        if not _MODULE_RE.match(self.module):
            raise AndroidError(
                "configuration-invalid",
                f"module {self.module!r} must be a Gradle project path like ':app'",
            )
        if not _VARIANT_RE.match(self.variant):
            raise AndroidError(
                "configuration-invalid", f"variant {self.variant!r} must be an identifier"
            )
        if self.flavor and not _VARIANT_RE.match(self.flavor):
            raise AndroidError(
                "configuration-invalid", f"flavor {self.flavor!r} must be an identifier"
            )
        if not self.outputs:
            raise AndroidError(
                "configuration-invalid",
                "a target must request at least one of " + " or ".join(SUPPORTED_OUTPUTS),
            )
        for output in self.outputs:
            if output not in SUPPORTED_OUTPUTS:
                raise AndroidError(
                    "configuration-invalid",
                    f"unknown output {output!r}; supported: {', '.join(SUPPORTED_OUTPUTS)}",
                )
        if len(set(self.outputs)) != len(self.outputs):
            raise AndroidError("configuration-invalid", "outputs lists an output twice")
        if self.key_alias and not _ALIAS_RE.match(self.key_alias):
            raise AndroidError(
                "configuration-invalid",
                f"key_alias {self.key_alias!r} must be a keystore entry name: letters, "
                "digits, '.', '_' and '-'",
            )
        if self.signing_identity and not _IDENTITY_RE.match(self.signing_identity):
            raise AndroidError(
                "configuration-invalid",
                "signing_identity must be a certificate distinguished name or a "
                "SHA-256 fingerprint, on one line",
            )
        if self.play_app_signing and not self.key_alias:
            # Play App Signing still requires the *upload* key; enrolment changes
            # which key signs for users, not whether an upload is signed at all.
            raise AndroidError(
                "configuration-invalid",
                "play_app_signing requires key_alias: Continuum signs the upload with "
                "the upload key and Google Play holds the app-signing key",
            )
        if self.require_signing and self.key_alias_required() and not self.key_alias:
            raise AndroidError(
                "configuration-invalid",
                "a signed Android build needs key_alias: it names the private-key entry "
                "in the keystore that apksigner and jarsigner are pointed at, and "
                "without it the tools would be asked to sign with a key nobody named",
            )
        if self.version_code and self.version_code_offset:
            raise AndroidError(
                "configuration-invalid",
                "version_code is an explicit override, so version_code_offset would "
                "never apply; set one or the other",
            )
        if self.production and not self.require_signing:
            raise AndroidError(
                "configuration-invalid",
                "a production target cannot have require_signing disabled; that is how "
                "an unsigned build reaches a store",
            )

    def key_alias_required(self) -> bool:
        """Whether this target has asked for a signed build.

        True as soon as any signing secret is named. A target that names a
        keystore and two passwords but no alias has a configuration mistake, and
        finding it here beats handing `jarsigner` an entry name that does not
        exist. A target that names no secret at all is a different case: whether
        the material is in the environment is a fact about the job, and the sign
        stage already fails closed on it.
        """

        return bool(
            self.keystore_secret
            or self.keystore_password_secret
            or self.key_password_secret
            or self.production
        )

    @property
    def module_path(self) -> str:
        return self.module.lstrip(":").replace(":", "/") or "app"
    @property
    def build_dir(self) -> str:
        return os.path.join(self.module_path, "build")

    @property
    def variant_camel(self) -> str:
        return _camel(self.flavor) + _camel(self.variant) if self.flavor else _camel(self.variant)

    def _expected_alias(self) -> str:
        """The key alias the signing tools are pointed at.

        Only ever `key_alias`. A *signing identity* is a different thing — the
        certificate the signed artifact is expected to carry — and substituting one
        for the other would ask `jarsigner` to sign with an entry named after a
        distinguished name, which no keystore has.
        """

        return self.key_alias

    @property
    def expected_identity(self) -> str:
        """What a signed artifact is expected to carry, for the manifest row.

        The configured identity when there is one, and the alias otherwise: a row
        that claims a signature must name something, and naming the key that
        actually signed is the least that is true.
        """

        return self.signing_identity or self.key_alias

    def gradle_tasks(self) -> Tuple[str, ...]:
        """The tasks one invocation runs, in output order.

        Both outputs are requested from the *same* invocation so Gradle
        configures the project once and, more importantly, so the APK and the
        AAB cannot be produced from two different checkouts.
        """

        tasks: List[str] = []
        prefix = self.module if self.module.startswith(":") else f":{self.module}"
        if OUTPUT_APK in self.outputs:
            tasks.append(f"{prefix}:assemble{self.variant_camel}")
        if OUTPUT_AAB in self.outputs:
            tasks.append(f"{prefix}:bundle{self.variant_camel}")
        return tuple(tasks)

    def version_binding(self, version: str) -> Tuple[str, int]:
        name = version_name_for(version)
        code = self.version_code or version_code_for(version, offset=self.version_code_offset)
        return name, code

    def signing_secret_names(self) -> Tuple[str, ...]:
        names = [
            self.keystore_secret,
            self.keystore_password_secret,
            self.key_password_secret,
        ]
        return tuple(name for name in names if name)

    def describe(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "variant": self.variant,
            "flavor": self.flavor,
            "outputs": list(self.outputs),
            "keystore_secret": self.keystore_secret,
            "keystore_password_secret": self.keystore_password_secret,
            "key_alias": self.key_alias,
            "key_password_secret": self.key_password_secret,
            "signing_identity": self.signing_identity,
            "play_app_signing": self.play_app_signing,
            "production": self.production,
            "require_signing": self.require_signing,
            "version_code": self.version_code,
            "version_code_offset": self.version_code_offset,
            "gradlew": self.gradlew,
            "mapping_path": self.mapping_path,
            "native_symbols_path": self.native_symbols_path,
        }


def _camel(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[_-]", value) if part)


_ALLOWED_SETTING_KEYS = (
    "module",
    "variant",
    "flavor",
    "outputs",
    "keystore_secret",
    "keystore_password_secret",
    "key_alias",
    "key_password_secret",
    "signing_identity",
    "play_app_signing",
    "production",
    "require_signing",
    "version_code",
    "version_code_offset",
    "gradlew",
    "mapping_path",
    "native_symbols_path",
)


def parse_settings(value: Any, where: str = "android") -> AndroidSettings:
    """Validate a target's adapter options.

    Kept in this module rather than in `continuum.config`, because the release
    configuration schema is a consumer-facing contract and platform options are
    the adapter's own business. A target carries them as opaque
    `TargetSpec.options`, and this is the only code that reads them.
    """

    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_SETTING_KEYS, where)

    outputs_value = mapping.get("outputs")
    if outputs_value is None:
        outputs: Tuple[str, ...] = (OUTPUT_APK,)
    elif not isinstance(outputs_value, list) or not outputs_value:
        raise AndroidError(
            "configuration-invalid",
            f"{where}.outputs must be a non-empty list of {', '.join(SUPPORTED_OUTPUTS)}",
        )
    else:
        outputs = tuple(outputs_value)

    key_alias = _text(mapping.get("key_alias"), f"{where}.key_alias", "")
    signing_identity = _text(mapping.get("signing_identity"), f"{where}.signing_identity", "")

    return AndroidSettings(
        module=_text(mapping.get("module"), f"{where}.module", "app", pattern=_MODULE_RE),
        variant=_text(mapping.get("variant"), f"{where}.variant", "release", pattern=_VARIANT_RE),
        flavor=_text(mapping.get("flavor"), f"{where}.flavor", "", pattern=_VARIANT_RE),
        outputs=outputs,
        keystore_secret=_name(mapping.get("keystore_secret"), f"{where}.keystore_secret", required=False),
        keystore_password_secret=_name(
            mapping.get("keystore_password_secret"), f"{where}.keystore_password_secret", required=False
        ),
        key_alias=key_alias,
        key_password_secret=_name(
            mapping.get("key_password_secret"), f"{where}.key_password_secret", required=False
        ),
        signing_identity=signing_identity,
        play_app_signing=_bool(
            mapping.get("play_app_signing"), f"{where}.play_app_signing", False
        ),
        production=_bool(mapping.get("production"), f"{where}.production", False),
        require_signing=_bool(
            mapping.get("require_signing"), f"{where}.require_signing", True
        ),
        version_code=_int(
            mapping.get("version_code"), f"{where}.version_code", 0, minimum=1, maximum=MAX_VERSION_CODE
        ),
        version_code_offset=_int(
            mapping.get("version_code_offset"), f"{where}.version_code_offset", 0, minimum=0, maximum=1000
        ),
        gradlew=_text(mapping.get("gradlew"), f"{where}.gradlew", DEFAULT_GRADLEW),
        mapping_path=_text(mapping.get("mapping_path"), f"{where}.mapping_path", ""),
        native_symbols_path=_text(
            mapping.get("native_symbols_path"), f"{where}.native_symbols_path", ""
        ),
    )


def settings_from(target: TargetSpec) -> AndroidSettings:
    """Read a target's options as an `AndroidSettings`.

    `TargetSpec.options` is a tuple of pairs so it stays hashable; a mapping
    arrives as a nested mapping, which the parser accepts directly.
    """

    if target.adapter != ADAPTER_NAME:
        raise AndroidError(
            "wrong-adapter",
            f"target {target.id!r} names adapter {target.adapter!r}, not {ADAPTER_NAME!r}",
        )
    options = {key: value for key, value in target.options}
    return parse_settings(options, f"target {target.id!r}")


# -- signing material -------------------------------------------------------


def signing_material_present(
    settings: AndroidSettings, environment: Mapping[str, str]
) -> bool:
    """Whether every secret a signed build needs is actually here.

    An empty value counts as absent. GitHub hands a fork the repository
    variables with nothing in them rather than withholding them, so a check for
    "is the name set" would pass on a fork and sign with nothing.
    """

    if not settings.require_signing:
        return True
    if not settings.keystore_secret or not settings.keystore_password_secret:
        return False
    if not settings.key_password_secret:
        return False
    if not settings.key_alias:
        return False
    return all(
        bool((environment.get(name) or "").strip())
        for name in settings.signing_secret_names()
    )


def bind_material(
    settings: AndroidSettings, environment: Mapping[str, str]
) -> Dict[str, str]:
    """The environment a run needs, with the configured secrets under fixed names."""

    bound = dict(environment)
    keystore = (bound.get(settings.keystore_secret) or "").strip() if settings.keystore_secret else ""
    keystore_password = (
        (bound.get(settings.keystore_password_secret) or "").strip()
        if settings.keystore_password_secret
        else ""
    )
    key_password = (
        (bound.get(settings.key_password_secret) or "").strip()
        if settings.key_password_secret
        else ""
    )
    if keystore:
        bound[KEYSTORE_ENV] = keystore
    if keystore_password:
        bound[KEYSTORE_PASSWORD_ENV] = keystore_password
    if key_password:
        bound[KEY_PASSWORD_ENV] = key_password
    return bound


# -- failure classification -------------------------------------------------

_FAILURE_MARKERS: Tuple[Tuple[str, str, str], ...] = (
    (
        "keystore-password-invalid",
        "the keystore could not be opened with the stored password",
        "check keystore_password_secret; a wrong password and a wrong keystore "
        "produce the same tool error",
    ),
    (
        "key-password-invalid",
        "the key could not be unlocked with the stored password",
        "check key_password_secret; it is the password of the alias, not of the "
        "keystore file",
    ),
    (
        "key-alias-missing",
        "the configured key alias is not in the keystore",
        "key_alias must name a private-key entry in the uploaded keystore",
    ),
    (
        "signing-material-missing",
        "the keystore or its credentials are not available in this job",
        "expose the repository secrets named by keystore_secret, "
        "keystore_password_secret and key_password_secret; a fork never receives them",
    ),
    (
        "gradle-build-failed",
        "the Gradle build failed",
        "the release builds the exact approved commit; a failure here is a code or "
        "environment problem, not a release problem",
    ),
    (
        "signing-tool-missing",
        "a signing tool is not available on this runner",
        "the release job must provide the Android SDK build-tools (apksigner) and a "
        "JDK (jarsigner)",
    ),
)

#: Substrings that identify each failure in a tool's own words. Each entry holds
#: alternative *phrases*, and a phrase matches when every word of it appears, so
#: "key password was incorrect" and "cannot recover key" are two ways of saying
#: the same thing rather than two things both of which must be present. Checked
#: in the order given, most specific first: `keystore` on its own is not a
#: diagnosis, because "keystore password was incorrect" contains it and means
#: something entirely different from a keystore that is not there.
_FAILURE_HINTS: Tuple[Tuple[str, Tuple[Tuple[str, ...], ...]], ...] = (
    (
        "keystore-password-invalid",
        (("password was incorrect",), ("keystore password was",), ("cannot open keystore",)),
    ),
    ("key-password-invalid", (("cannot recover key",), ("key password was incorrect",))),
    ("key-alias-missing", (("alias", "not exist"),)),
    ("signing-tool-missing", (("command not found",), ("no such file or directory",))),
    ("signing-material-missing", (("keystore does not exist",), ("unable to open keystore",))),
)


def classify_failure(step: str, detail: str) -> Tuple[str, str, str]:
    """Explain a failed step by the invariant it was protecting."""

    lowered = (detail or "").lower()
    if "gradle" in step.lower():
        for code, message, remediation in _FAILURE_MARKERS:
            if code == "gradle-build-failed":
                return code, message, remediation
    for code, phrases in _FAILURE_HINTS:
        if not any(
            all(word in lowered for word in phrase) for phrase in phrases
        ):
            continue
        for candidate, message, remediation in _FAILURE_MARKERS:
            if candidate == code:
                return candidate, message, remediation
    return (
        "android-step-failed",
        f"step {step!r} failed",
        "inspect the build output; the adapter could not classify the failure",
    )


# -- command boundary -------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str


CommandRunner = Callable[[Sequence[str], str, Mapping[str, str]], CommandResult]


def subprocess_runner(
    argv: Sequence[str], workdir: str, environment: Mapping[str, str]
) -> CommandResult:  # pragma: no cover - process boundary
    """An argument vector, never a shell string, with the job environment."""

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
        cwd=workdir or None,
        env=dict(environment),
        check=False,
    )
    output = ((completed.stdout or "") + (completed.stderr or ""))[:65536]
    return CommandResult(code=completed.returncode, output=output)


# -- output discovery -------------------------------------------------------


def locate_output(settings: AndroidSettings, root: str, output: str) -> str:
    """Find the single artifact Gradle produced for one output.

    Gradle's output layout carries the module, the variant, and the flavor, so
    the search is scoped to the module's own `build/outputs` tree and narrowed by
    the variant token. A match that is not unique is a failure: two APKs for one
    variant means the release cannot say which one it shipped.
    """

    outputs_dir = os.path.join(root, settings.build_dir, "outputs")
    extension = ".apk" if output == OUTPUT_APK else ".aab"
    matches = sorted(
        path
        for path in glob.glob(os.path.join(outputs_dir, "**", f"*{extension}"), recursive=True)
        if os.path.isfile(path)
    )
    token = settings.variant_camel.lower()
    narrowed = [path for path in matches if token.lower() in os.path.basename(path).lower()]
    chosen = narrowed or matches
    if not chosen:
        raise AndroidError(
            "output-not-found",
            f"no {extension} was produced under {outputs_dir!r} for "
            f"{settings.module!r} {settings.variant_camel}; the build reported success "
            "but named no artifact",
        )
    if len(chosen) > 1:
        raise AndroidError(
            "ambiguous-output",
            f"{len(chosen)} {extension} files matched {settings.variant_camel!r}: "
            + ", ".join(os.path.basename(path) for path in chosen),
        )
    return chosen[0]


# -- artifact naming --------------------------------------------------------


def artifact_name(settings: AndroidSettings, output: str, version: str) -> str:
    """A download name that is flat, versioned, and unique per output.

    The target id is not part of it because the release manifest already scopes
    an artifact to its target; two targets that both ship `app-release.apk` would
    collide at the destination, and the fix is for a project to name its outputs
    differently rather than for the adapter to invent a name.
    """

    extension = "apk" if output == OUTPUT_APK else "aab"
    stem = os.path.basename(settings.module_path) or "app"
    flavor = f"-{settings.flavor}" if settings.flavor else ""
    return f"{stem}-{version}{flavor}-{settings.variant}.{extension}"


# -- the adapter ------------------------------------------------------------


class AndroidAdapter:
    """Builds, signs, and verifies one Android target.

    The command runner and environment are injected so the whole adapter can be
    exercised on a machine with no Android toolchain: a test supplies a runner
    that writes the files Gradle would have written, and the adapter's decisions
    are still the decisions a real release would make.
    """

    SUPPORTS_DRY_RUN = True
    name = ADAPTER_NAME

    def __init__(
        self,
        *,
        environment: Optional[Mapping[str, str]] = None,
        command_runner: Optional[CommandRunner] = None,
        workdir: str = "",
        scratch_dir: str = "",
        git_revision: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.environment: Dict[str, str] = dict(environment or {})
        self.command_runner: CommandRunner = command_runner or subprocess_runner
        self.workdir = workdir
        self.scratch_dir = scratch_dir or tempfile.mkdtemp(prefix="continuum-android-")
        self._git_revision = git_revision

    # -- availability -------------------------------------------------------
    def available(self) -> bool:
        """Whether the Gradle wrapper this target names exists here.

        Only the wrapper is checked: Gradle downloads and invokes the compiler,
        the SDK, and the signing tools itself, and a runner that can run the
        wrapper is the runner the profile is written for. A dry run needs the
        wrapper too, because a plan that names a build entry point that is not in
        the checkout is not a plan for this repository.
        """

        wrapper = os.path.join(self.workdir, self._default_gradlew())
        return os.path.isfile(wrapper)

    def _default_gradlew(self) -> str:
        return os.path.join(".", "gradlew")

    def intent(self) -> str:
        return (
            "build every requested output from the approved source SHA with the "
            "Gradle wrapper, sign what was built with the upload key in a temporary "
            "workspace, verify each artifact, and remove the key material"
        )

    # -- helpers ------------------------------------------------------------
    def _settings(self, request: BuildRequest) -> AndroidSettings:
        return settings_from(request.target)

    def _run_gradle(
        self, request: BuildRequest, settings: AndroidSettings, version: str
    ) -> None:
        name, code = settings.version_binding(version)
        argv: List[str] = [
            settings.gradlew,
            *settings.gradle_tasks(),
            f"-P{VERSION_NAME_PROPERTY}={name}",
            f"-P{VERSION_CODE_PROPERTY}={code}",
        ]
        result = self.command_runner(argv, self.workdir or request.workdir, self.environment)
        if result.code != 0:
            code_name, message, remediation = classify_failure("run-gradle", result.output)
            raise AndroidError(
                code_name,
                f"{message}: {result.output.strip()[:400]}",
                retryable=False,
                remediation=remediation,
            )

    def _assert_source(self, request: BuildRequest) -> None:
        """Refuse to build anything but the approved commit.

        The core pins a release to one SHA; this is the adapter's own check that
        the working tree it is about to build is that commit. It is deliberately
        not skippable for a real run: a moved checkout builds bytes no reviewed
        commit produced.
        """

        if self._git_revision is None:
            return
        found = self._git_revision(self.workdir or request.workdir)
        if found and found != request.source_sha:
            raise AndroidError(
                "source-mismatch",
                f"the checkout is at {found} but this release is approved for "
                f"{request.source_sha}; building would ship unreviewed bytes",
            )

    # -- build --------------------------------------------------------------
    def build(self, request: BuildRequest) -> ArtifactManifest:
        settings = self._settings(request)
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=self.name,
            source_sha=request.source_sha,
            version=request.version,
        )
        version_name, version_code = settings.version_binding(request.version)

        if request.dry_run:
            for output in settings.outputs:
                builder.declare(
                    name=artifact_name(settings, output, request.version),
                    path=os.path.join(
                        request.workdir or self.workdir, settings.build_dir, "outputs", output
                    ),
                    type=TYPE_INSTALLER if output == OUTPUT_APK else TYPE_PACKAGE,
                    platform="android",
                    classifier=output,
                )
            return builder.build()

        self._assert_source(request)
        self._run_gradle(request, settings, request.version)

        root = request.workdir or self.workdir
        for output in settings.outputs:
            path = locate_output(settings, root, output)
            builder.record(
                name=artifact_name(settings, output, request.version),
                path=path,
                type=TYPE_INSTALLER if output == OUTPUT_APK else TYPE_PACKAGE,
                platform="android",
                classifier=output,
                media_type=(
                    "application/vnd.android.package-archive"
                    if output == OUTPUT_APK
                    else "application/octet-stream"
                ),
            )
        self._record_evidence(builder, settings, root)
        return builder.build()

    def _record_evidence(
        self, builder: ManifestBuilder, settings: AndroidSettings, root: str
    ) -> None:
        """Preserve the files a crash report is useless without.

        A mapping file and a native debug-symbol bundle are not installable, so
        they are recorded as `provenance` rows and the destination will not offer
        them as downloads a user installs. Both are optional; a project that has
        nothing to preserve records nothing.
        """

        for pattern in (settings.mapping_path, settings.native_symbols_path):
            if not pattern:
                continue
            for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                if not os.path.isfile(path):
                    continue
                builder.record(
                    name=os.path.basename(path),
                    path=path,
                    type=TYPE_PROVENANCE,
                    platform="android",
                    provenance=PROVENANCE_DECLARED,
                )

    # -- signing ------------------------------------------------------------
    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        settings = self._settings(request)
        if request.dry_run:
            return manifest
        if not settings.require_signing:
            return manifest

        if not settings.keystore_secret or not settings.key_password_secret:
            raise AndroidError(
                "signing-material-missing",
                f"target {request.target.id!r} requires a signed Android build but does "
                "not name the repository secrets that hold the keystore and its "
                "passwords",
                remediation=(
                    "name keystore_secret, keystore_password_secret and "
                    "key_password_secret; a release never commits key material"
                ),
            )
        if not signing_material_present(settings, self.environment):
            raise AndroidError(
                "signing-material-missing",
                f"target {request.target.id!r} needs a signed build, but "
                + " and ".join(settings.signing_secret_names())
                + " are not available in this job",
                remediation=(
                    "expose the named repository secrets. A production target fails "
                    "closed here rather than shipping an unsigned artifact."
                ),
            )

        keystore = self._materialize_keystore(settings)
        password_files = self._write_password_files(settings)
        signed_paths: Dict[str, str] = {}
        try:
            for artifact in manifest.installable:
                if artifact.type not in (TYPE_INSTALLER, TYPE_PACKAGE):
                    continue
                signed = self._sign_artifact(
                    settings, artifact, keystore, password_files, request.workdir or self.workdir
                )
                signed_paths[artifact.name] = signed
            return self._signed_manifest(manifest, settings, signed_paths)
        finally:
            self._cleanup(keystore, password_files)

    def _materialize_keystore(self, settings: AndroidSettings) -> str:
        raw = (self.environment.get(KEYSTORE_ENV) or "").strip()
        if not raw:
            # The join may not have run (a caller that built TargetSpec options
            # and an environment without calling bind_material). Accept the
            # configured name directly as a fallback, still never by value name.
            raw = (self.environment.get(settings.keystore_secret) or "").strip()
        if not raw:
            raise AndroidError(
                "signing-material-missing",
                "the keystore secret is empty; a keystore is binary material carried "
                "as base64 in a repository secret",
            )
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:  # base64 raises binascii.Error, a ValueError subclass
            raise AndroidError(
                "signing-material-missing",
                f"the keystore secret is not valid base64: {exc}",
            ) from None
        os.makedirs(self.scratch_dir, exist_ok=True)
        path = os.path.join(self.scratch_dir, "continuum-upload.jks")
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)
        return path

    def _passwords(self, settings: AndroidSettings) -> Tuple[str, str]:
        keystore_password = (
            self.environment.get(KEYSTORE_PASSWORD_ENV)
            or self.environment.get(settings.keystore_password_secret)
            or ""
        )
        key_password = (
            self.environment.get(KEY_PASSWORD_ENV)
            or self.environment.get(settings.key_password_secret)
            or ""
        )
        return keystore_password, (key_password or keystore_password)

    def _write_password_files(self, settings: AndroidSettings) -> Dict[str, str]:
        """Put the passwords in files, never in an argument vector.

        `apksigner` and `jarsigner` both accept a file as the source of a
        password. A password in argv is visible to every process on the runner
        and to a crash report, which is exactly what a temporary password file
        with mode 0600 avoids. The files are removed in the same `finally` that
        removes the keystore.
        """

        keystore_password, key_password = self._passwords(settings)
        os.makedirs(self.scratch_dir, exist_ok=True)
        files = {
            "keystore": os.path.join(self.scratch_dir, "continuum-keystore.pw"),
            "key": os.path.join(self.scratch_dir, "continuum-key.pw"),
        }
        for name, path in files.items():
            value = keystore_password if name == "keystore" else key_password
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(value)
            os.chmod(path, 0o600)
        return files

    def _sign_artifact(
        self,
        settings: AndroidSettings,
        artifact: Artifact,
        keystore: str,
        password_files: Mapping[str, str],
        workdir: str,
    ) -> str:
        alias = settings.key_alias
        signed_path = artifact.path + ".signed"
        if artifact.type == TYPE_INSTALLER:
            argv = [
                APKSIGNER,
                "sign",
                "--ks",
                keystore,
                "--ks-key-alias",
                alias,
                "--ks-pass",
                f"file:{password_files['keystore']}",
                "--key-pass",
                f"file:{password_files['key']}",
                "--out",
                signed_path,
                artifact.path,
            ]
        else:
            argv = [
                JARSIGNER,
                "-keystore",
                keystore,
                "-storepass:file",
                password_files["keystore"],
                "-keypass:file",
                password_files["key"],
                "-sigalg",
                "SHA256withRSA",
                "-digestalg",
                "SHA-256",
                "-signedjar",
                signed_path,
                artifact.path,
                alias,
            ]
        result = self.command_runner(argv, workdir, self.environment)
        if result.code != 0:
            code, message, remediation = classify_failure("sign-" + artifact.name, result.output)
            raise AndroidError(
                code,
                f"{message} ({artifact.name}): {result.output.strip()[:400]}",
                remediation=remediation,
            )
        return signed_path

    def _signed_manifest(
        self,
        manifest: ArtifactManifest,
        settings: AndroidSettings,
        signed_paths: Mapping[str, str],
    ) -> ArtifactManifest:
        identity = settings.expected_identity
        builder = ManifestBuilder(
            target=manifest.target,
            adapter=self.name,
            source_sha=manifest.source_sha,
            version=manifest.version,
        )
        for artifact in manifest.artifacts:
            signed = signed_paths.get(artifact.name)
            if signed is None:
                builder.replace(artifact)
                continue
            builder.record(
                name=artifact.name,
                path=signed,
                type=artifact.type,
                platform=artifact.platform,
                classifier=artifact.classifier,
                media_type=artifact.media_type,
                signing=SIGNING_SIGNED,
                signing_identity=identity,
                # Signing the bytes does not change what was built, so what was
                # declared about them is carried across rather than dropped.
                provenance=artifact.provenance,
                attestation=artifact.attestation,
            )
        return builder.build()

    def _cleanup(self, keystore: str, password_files: Mapping[str, str]) -> None:
        for path in (keystore, keystore + ".signed", *password_files.values()):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                continue

    # -- verify -------------------------------------------------------------
    def verify(self, request: BuildRequest, manifest: ArtifactManifest) -> VerificationReport:
        settings = self._settings(request)
        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="planned",
                detail="a dry run declares its artifacts and verifies nothing",
                verified_by=self.name,
            )

        failures: List[str] = []
        for artifact in manifest.installable:
            if artifact.type not in (TYPE_INSTALLER, TYPE_PACKAGE):
                continue
            ok, detail = self._verify_artifact(settings, artifact)
            if not ok:
                failures.append(f"{artifact.name}: {detail}")
        if failures:
            return VerificationReport(
                verified=False,
                code="android-verification-failed",
                detail="; ".join(failures),
                failures=tuple(failures),
            )
        return VerificationReport(
            verified=True,
            code="verified",
            detail=(
                "every Android artifact was checked with its signing tool"
                + (
                    " and carries the upload key"
                    if settings.signing_identity
                    else ""
                )
            ),
            verified_by=self.name,
        )

    def _verify_artifact(self, settings: AndroidSettings, artifact: Artifact) -> Tuple[bool, str]:
        if artifact.type == TYPE_INSTALLER:
            argv = [APKSIGNER, "verify", "--print-certs", artifact.path]
        else:
            # `-certs` is what makes the identity comparable: bare
            # `jarsigner -verify` reports "jar verified." and says nothing about
            # which key signed, so a bundle could be verified against a
            # configured identity the tool never printed.
            argv = [JARSIGNER, "-verify", "-verbose", "-certs", artifact.path]
        result = self.command_runner(argv, self.workdir, self.environment)
        if result.code != 0:
            return False, result.output.strip()[:200] or "signing tool rejected the artifact"
        if settings.signing_identity and settings.signing_identity not in result.output:
            return False, (
                f"the artifact does not carry the configured signing identity "
                f"{settings.signing_identity!r}"
            )
        return True, ""

    # -- manual lifecycle for a caller that runs stages itself --------------
    def cleanup(self) -> None:
        if os.path.isdir(self.scratch_dir):
            shutil.rmtree(self.scratch_dir, ignore_errors=True)


def components(
    adapter: AndroidAdapter,
    play: Any = None,
) -> Dict[str, Any]:
    """The wiring a caller hands to `ReleaseComponents`.

    Kept as a helper so a repository does not have to know the registry names,
    but deliberately not a global registry: the core takes its components by
    injection, and a module that registered itself globally would re-introduce
    the coupling the contract removed.
    """

    wiring: Dict[str, Any] = {"adapters": {ADAPTER_NAME: adapter}}
    if play is not None:
        wiring["publishers"] = (play,)
    return wiring


__all__ = [
    "ADAPTER_NAME",
    "APKSIGNER",
    "DEFAULT_GRADLEW",
    "JARSIGNER",
    "KEYSTORE_ENV",
    "KEYSTORE_PASSWORD_ENV",
    "KEY_PASSWORD_ENV",
    "MAX_VERSION_CODE",
    "OUTPUT_AAB",
    "OUTPUT_APK",
    "SUPPORTED_OUTPUTS",
    "VERSION_CODE_PROPERTY",
    "VERSION_NAME_PROPERTY",
    "AndroidAdapter",
    "AndroidError",
    "AndroidSettings",
    "CommandResult",
    "CommandRunner",
    "artifact_name",
    "bind_material",
    "classify_failure",
    "components",
    "locate_output",
    "parse_semver",
    "parse_settings",
    "settings_from",
    "signing_material_present",
    "subprocess_runner",
    "version_code_for",
    "version_name_for",
]
