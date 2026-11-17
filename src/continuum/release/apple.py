"""Apple release adapter: build, sign, seal, verify, tear down.

This is the module that knows about keychains, entitlements, and `codesign`.
Nothing else in Continuum does.

The profile that is implemented here is **self-signed-stable**: a purpose-made
self-signed code-signing certificate whose PKCS#12 lives in repository secrets
and is imported into a throwaway keychain for the length of one job. It is not
a stand-in for Developer ID and it does not pretend to be notarized. It exists
because a self-signed identity is *stable*, and stability is what the platform
rewards: a signature that keeps the same designated requirement across releases
keeps the permissions a user already granted, instead of re-prompting them every
time the app updates.

The three properties that make that work, and that this adapter therefore
refuses to give up:

* **One identity, end to end.** Binaries first, then the bundle that contains
  those same binaries, sealed with the same identity, identifier, and
  entitlements. Two identities for one product is two applications as far as
  the permission database is concerned.
* **No secure timestamp by default.** RSA PKCS#1 v1.5 signatures are
  deterministic, so re-running a release of one revision produces byte-identical
  artifacts and the checksums published for them stay true. A timestamp is
  available, but it has to be asked for, because asking for it silently
  invalidates every checksum a downstream manifest already recorded.
* **The identity is pinned, not assumed.** After signing, the plan asserts that
  the certificate which signed each artifact is the one the target names. The
  common failure here is a silent fallback: the material fails to import, the
  tool signs with something else or with nothing, and the release ships with a
  signature no one ever checked. A pin turns that into a failed job.

The ad-hoc profile is implemented too, and it is deliberately *worse*: it has no
identity, therefore no stable designated requirement, therefore no preserved
permissions. It exists so a fork or a manual run can still produce an artifact.
It is always marked degraded, and it can never claim a pin.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config as config_module
from . import plan as plan_module
from .plan import (
    ENCODING_BASE64,
    STEP_ASSERT,
    STEP_MATERIALIZE,
    STEP_RUN,
    PlanStep,
    ReleaseError,
    ReleasePlan,
    SigningUnavailable,
    TargetNotExecutable,
)

ADAPTER_NAME = config_module.ADAPTER_APPLE

# Scratch locations. Under the system temporary directory rather than a fixed
# path, so two jobs on the same runner cannot collide, and so the teardown can
# remove a directory tree it created itself.
_KEYCHAIN_NAME = "continuum-signing.keychain"
_P12_NAME = "continuum-signing.p12"

# A keychain password is not a secret: the file it protects is deleted in
# teardown, and holding it in the plan (rather than in an environment variable
# nobody would think to bind) is what makes the teardown self-contained.
_KEYCHAIN_PASSWORD = "continuum-release-keychain"

# The environment variables the plan reads. The workflow binds each to the
# repository secret named by configuration; the plan only ever refers to the
# variable, so the secret name in `.continuum.yml` is the single place a
# repository declares where its key material lives.
P12_ENV = "CONTINUUM_APPLE_P12"
P12_PASSWORD_ENV = "CONTINUUM_APPLE_P12_PASSWORD"

# The version stamped into a bundle's `CFBundleVersion`. It is read from the
# environment rather than configuration because a version belongs to the
# release being cut, not to the repository: the same `.continuum.yml` signs
# 1.2.0 on Monday and 1.2.1 on Tuesday.
VERSION_ENV = "CONTINUUM_RELEASE_VERSION"

# `security` and `codesign` resolve through PATH on a macOS runner. They are
# named absolutely so a stray directory earlier in PATH cannot shadow the tool
# that produces a release signature.
SECURITY = "/usr/bin/security"
CODESIGN = "/usr/bin/codesign"
PLISTBUDDY = "/usr/libexec/PlistBuddy"

# Gatekeeper treats a hardened-runtime signature as a different promise from an
# ordinary one: it removes the ability to load unsigned code at runtime, which
# is the protection the entitlement set is only meaningful under.
HARDENED_RUNTIME_FLAG = "runtime"

# The plist keys a bundle's version is written to, and the order they are written
# in. An `.app` carries two, and they are not interchangeable:
# `CFBundleVersion` is the build number, and `CFBundleShortVersionString` is the
# version Finder displays and LaunchServices compares when deciding whether an
# installed bundle is an upgrade or a different application. Stamping only the
# first ships a bundle that presents itself as the new release and identifies
# itself as the old one, so the release flow this adapter replaces wrote both
# from the same value and so does this.
#
# Both are written before the seal, so a bundle whose `Info.plist` is missing
# either key fails the release with PlistBuddy's own message instead of shipping
# a bundle that reports a stale version.
_BUNDLE_VERSION_KEYS = ("CFBundleVersion", "CFBundleShortVersionString")

# The key-partition list that lets a non-interactive job use an imported key.
# Without it the key is present but unusable by `codesign` and the import
# succeeds while the signature does not — a failure that looks like a signing
# problem and is actually a keychain problem.
KEY_PARTITION_LIST = "apple-tool:,apple:"

# The certificate common name the imported material is expected to carry. The
# pin is on this exact string: it is the `Authority=` value the signing tool
# reports, and it is what the permission database keys on.
_AUTHORITY_PREFIX = "Authority="

_ADHOC_DEGRADATION = (
    "signed ad-hoc: the release carries no stable identity, so the designated "
    "requirement and the permissions granted to the previous release are not "
    "preserved"
)


def _slot(variable: str) -> str:
    return plan_module.secret_slot(variable)


def _value_slot(variable: str) -> str:
    """A slot for an ordinary value, as opposed to key material."""

    return plan_module.env_slot(variable)


def info_plist_path(bundle: str) -> str:
    """The plist inside a bundle, which is what PlistBuddy and `codesign` read.

    A bundle is a directory. `PlistBuddy` and `codesign` both resolve the
    plist themselves when given the bundle, but a *write* does not: stamping a
    version into the bundle path would either create a stray file beside it or
    fail, and the version would never reach the signed content.
    """

    return os.path.join(bundle, "Contents", "Info.plist")


def _scratch_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "continuum-release")


def keychain_path() -> str:
    return os.path.join(_scratch_dir(), _KEYCHAIN_NAME)


def p12_path() -> str:
    return os.path.join(_scratch_dir(), _P12_NAME)


# -- toolchain availability -------------------------------------------------


def available() -> bool:
    """Whether this adapter can run here at all."""

    return os.path.isdir("/System/Library") and all(
        os.access(path, os.X_OK) for path in (SECURITY, CODESIGN, PLISTBUDDY)
    )


def ensure_available() -> None:
    if not available():
        raise TargetNotExecutable(
            "the apple adapter needs the macOS signing toolchain; this runner does "
            "not provide it. Signing happens on a macOS runner, not on the Linux "
            "runner that validates configuration."
        )


# -- failures ---------------------------------------------------------------


# Markers `security import` produces for the two mistakes a repository will
# actually make with a stored certificate: the payload is not a PKCS#12 at all,
# and the payload is right but the password is wrong. Both exit non-zero with
# an otherwise unhelpful status, so the adapter is the only place that can tell
# them apart — and the difference is "re-upload the secret" versus "check the
# secret name", which is worth the trouble.
_IMPORT_MARKERS: Tuple[Tuple[str, str, str], ...] = (
    (
        "import-authentication-failed",
        "the signing certificate could not be imported: the stored password is wrong",
        "check the repository secret named by signing.password_secret",
    ),
    (
        "import-payload-invalid",
        "the signing certificate could not be imported: the stored material is not a "
        "readable PKCS#12",
        "re-upload signing.p12_secret as the base64 of the exported .p12",
    ),
    (
        "import-failed",
        "the signing certificate could not be imported",
        "check that signing.p12_secret and signing.password_secret hold the exported "
        "certificate and its password",
    ),
)

_SIGN_MARKERS: Tuple[Tuple[str, str, str], ...] = (
    (
        "sign-identity-missing",
        "the configured signing identity is not in the keychain",
        "the imported certificate's common name must equal signing.identity exactly; "
        "a mismatch silently produces a different signature identity",
    ),
    (
        "sign-identity-ambiguous",
        "more than one signing identity matches the configured name",
        "signing.identity must match exactly one certificate in the imported material",
    ),
    (
        "sign-failed",
        "the artifact could not be signed",
        "check the entitlements file and that the identity is a code-signing certificate",
    ),
)


def _classify(detail: str, markers: Sequence[Tuple[str, str, str]]) -> Tuple[str, str, str]:
    lowered = (detail or "").lower()
    for code, message, remediation in markers:
        if code == "import-authentication-failed":
            hits = (
                "errsecauthfailed" in lowered
                or "the user name or passphrase you entered is not correct" in lowered
                or "incorrect password" in lowered
                or "mac verify error" in lowered
            )
        elif code == "import-payload-invalid":
            hits = (
                "could not decode" in lowered
                or "not a pkcs" in lowered
                or "errsecdecode" in lowered
                or "securityd: SecKeychainItemImport" in lowered
            )
        elif code == "import-failed":
            hits = "import" in lowered
        elif code == "sign-identity-missing":
            hits = (
                "no identity found" in lowered
                or "unable to find an identity" in lowered
                or "errsecitemnotfound" in lowered
            )
        elif code == "sign-identity-ambiguous":
            hits = "multiple identities" in lowered or "ambiguous" in lowered
        else:
            hits = "sign" in lowered or "codesign" in lowered
        if hits:
            return code, message, remediation
    return markers[-1][0], markers[-1][1], markers[-1][2]


def classify_import_failure(detail: str) -> Tuple[str, str, str]:
    return _classify(detail, _IMPORT_MARKERS)


def classify_sign_failure(detail: str) -> Tuple[str, str, str]:
    return _classify(detail, _SIGN_MARKERS)


def classify_failure(step_name: str, detail: str) -> Tuple[str, str, str]:
    """Explain a failed step by the invariant it was protecting.

    The registry asks for this by step name, so the mapping from "a command
    failed" to "the thing that is actually wrong" lives with the tools that
    produce the failure rather than in the runner that merely observed it.
    """

    if step_name == "import-p12":
        return classify_import_failure(detail)
    if step_name == "stamp-bundle-version":
        # Deliberately not routed to the signing classifier. PlistBuddy failing
        # here is a missing or malformed `Info.plist` key, and the "check the
        # entitlements file" remediation a signing failure produces would send
        # the reader to the one file that is not at fault.
        return (
            "bundle-version-stamp-failed",
            "the release version could not be written into the bundle's Info.plist",
            "the bundle's Info.plist must contain every key this target stamps "
            f"({', '.join(_BUNDLE_VERSION_KEYS)}). The version is written before the "
            "seal, so a missing key fails the release here rather than shipping a "
            "bundle that reports a stale version.",
        )
    if step_name.startswith("sign-"):
        return classify_sign_failure(detail)
    if step_name.startswith("pin-"):
        return (
            "identity-not-pinned",
            "the artifact was signed, but not with the identity this target pins",
            "signing.identity must equal the common name of the imported "
            "certificate; a different name produces a different designated "
            "requirement and resets the permissions users already granted",
        )
    if step_name.startswith("verify-"):
        return (
            "signature-verification-failed",
            "the signature on a produced artifact did not verify",
            "the artifact was modified or sealed inconsistently after signing; "
            "check that nothing rewrites bundle contents between signing and "
            "verification",
        )
    return _classify(detail, _SIGN_MARKERS)


# -- signing material -------------------------------------------------------


def material_present(signing: config_module.ReleaseSigningSettings, environment: Dict[str, str]) -> bool:
    """Whether the key material a stable profile needs is actually here.

    A secret that resolves to an empty string is *absent*, not empty. GitHub
    hands a fork an empty string rather than withholding the variable, so a
    check for "is the variable set" would pass on a fork and sign with nothing.
    """

    if not signing.secret_names():
        return False
    return all(bool((environment.get(name) or "").strip()) for name in signing.secret_names())


def bind_material(
    signing: config_module.ReleaseSigningSettings, environment: Dict[str, str]
) -> Dict[str, str]:
    """The environment a plan run needs, with the configured secrets bound in.

    A repository names *where* its certificate lives; the job exposes it under
    that name; the adapter reads it under a fixed name. This is the join, and
    it is the only place either name is known.
    """

    bound = dict(environment)
    p12 = (bound.get(signing.p12_secret) or "").strip()
    password = (bound.get(signing.password_secret) or "").strip()
    if p12:
        bound[P12_ENV] = p12
    if password:
        bound[P12_PASSWORD_ENV] = password
    return bound


# -- plan construction ------------------------------------------------------


def _codesign_argv(
    identity: str,
    keychain: str,
    identifier: str,
    entitlements: str,
    hardened_runtime: bool,
    timestamp: bool,
    path: str,
) -> List[str]:
    """The signing invocation, assembled once for binaries and for the bundle.

    The bundle deliberately does *not* pass `--deep`: re-signing nested code
    would re-seal the binaries that were just signed with their own identities,
    replacing signatures that were correct. The bundle is sealed over code that
    is already signed, which is the whole point of signing in this order.
    """

    argv = [CODESIGN, "--force", "--sign", identity]
    if keychain:
        argv += ["--keychain", keychain]
    argv += ["--identifier", identifier]
    if hardened_runtime:
        argv += ["--options", HARDENED_RUNTIME_FLAG]
    if entitlements:
        argv += ["--entitlements", entitlements]
    if timestamp:
        # Only ever reached when the target asked for it. See the module
        # docstring: a timestamp is not a free safety upgrade, it is a choice
        # to give up reproducible artifacts.
        argv.append("--timestamp")
    argv.append(path)
    return argv


def _artifact_path(target: config_module.ReleaseTarget, name: str) -> str:
    return os.path.join(".build", "release", name)


def _keychain_steps(signing: config_module.ReleaseSigningSettings, keychain: str) -> List[PlanStep]:
    """Create, unlock, and prepare a throwaway keychain for one job."""

    return [
        PlanStep(
            name="create-keychain",
            argv=[SECURITY, "create-keychain", "-p", _KEYCHAIN_PASSWORD, keychain],
            purpose="create a temporary keychain that lives only for this job",
        ),
        PlanStep(
            name="configure-keychain",
            argv=[SECURITY, "set-keychain-settings", "-lut", "21600", keychain],
            purpose="let the keychain unlock without an interactive prompt",
        ),
        PlanStep(
            name="select-keychain",
            argv=[SECURITY, "list-keychains", "-d", "user", "-s", keychain],
            purpose="search only the temporary keychain for the signing identity",
        ),
        PlanStep(
            name="adopt-keychain",
            argv=[SECURITY, "default-keychain", "-s", keychain],
            purpose="make the temporary keychain the default for signing tools",
        ),
        PlanStep(
            name="unlock-keychain",
            argv=[SECURITY, "unlock-keychain", "-p", _KEYCHAIN_PASSWORD, keychain],
            purpose="unlock the keychain so the private key can be used",
        ),
        PlanStep(
            name="materialize-p12",
            kind=STEP_MATERIALIZE,
            source_env=P12_ENV,
            path=p12_path(),
            encoding=ENCODING_BASE64,
            scratch_paths=(p12_path(),),
            purpose=(
                "write the stored certificate to a private scratch file for the "
                "import; the runner deletes it in teardown whatever happens next"
            ),
        ),
        PlanStep(
            name="import-p12",
            argv=[
                SECURITY,
                "import",
                p12_path(),
                "-k",
                keychain,
                "-P",
                _slot(P12_PASSWORD_ENV),
                "-T",
                CODESIGN,
            ],
            uses_secrets=(P12_PASSWORD_ENV,),
            purpose="import the repository's stored signing certificate",
        ),
        PlanStep(
            name="set-partition-list",
            argv=[
                SECURITY,
                "set-key-partition-list",
                "-S",
                KEY_PARTITION_LIST,
                "-s",
                "-k",
                _KEYCHAIN_PASSWORD,
                keychain,
            ],
            purpose=(
                "allow non-interactive use of the imported key; without this the "
                "import succeeds and the signature still fails"
            ),
        ),
    ]


def _delete_keychain_step(keychain: str) -> PlanStep:
    return PlanStep(
        name="delete-keychain",
        argv=[SECURITY, "delete-keychain", keychain],
        purpose="remove the temporary keychain and the private key it held",
        teardown=True,
    )


def _sign_steps(
    target: config_module.ReleaseTarget,
    *,
    identity: str,
    keychain: str,
    timestamp: bool,
) -> List[PlanStep]:
    steps: List[PlanStep] = []
    for binary in target.binaries:
        path = _artifact_path(target, binary.name)
        steps.append(
            PlanStep(
                name=f"sign-{binary.identifier}",
                argv=_codesign_argv(
                    identity,
                    keychain,
                    binary.identifier,
                    binary.entitlements,
                    target.hardened_runtime,
                    timestamp,
                    path,
                ),
                purpose=f"sign {binary.name} with the release identity",
            )
        )
    bundle = target.app_bundle
    if bundle is not None:
        path = _artifact_path(target, bundle.name)
        # The version is written into the bundle *before* it is sealed: the
        # signature covers Info.plist, so injecting a version afterwards would
        # invalidate the signature the next verification checks.
        #
        # PlistBuddy takes a sequence of `-c` commands against one file, so both
        # version keys are stamped by a single step. That is not a convenience:
        # two steps would let the first succeed and the second fail, leaving a
        # half-stamped `Info.plist` on a runner, and it would also make the
        # "stamped before the seal" ordering a property of two independently
        # scheduled commands instead of one.
        version_argv: List[str] = [PLISTBUDDY]
        for key in _BUNDLE_VERSION_KEYS:
            version_argv += ["-c", f"Set :{key} {_value_slot(VERSION_ENV)}"]
        version_argv.append(info_plist_path(path))
        steps.append(
            PlanStep(
                name="stamp-bundle-version",
                argv=version_argv,
                uses_env=(VERSION_ENV,),
                purpose=(
                    "record the release version inside the bundle before sealing, in "
                    "both keys an app bundle is identified by"
                ),
            )
        )
        steps.append(
            PlanStep(
                name="sign-bundle",
                argv=_codesign_argv(
                    identity,
                    keychain,
                    bundle.identifier,
                    bundle.entitlements,
                    target.hardened_runtime,
                    timestamp,
                    path,
                ),
                purpose=(
                    "seal the bundle over the already-signed binaries with the same "
                    "identity, identifier, and entitlements"
                ),
            )
        )
    return steps


def _verify_steps(target: config_module.ReleaseTarget, pinned: str) -> List[PlanStep]:
    """Prove what was signed, and prove the signature is intact.

    Two different claims, kept as two different steps. Verification says the
    bytes are self-consistent; the pin says *whose* signature they carry. A
    plan that only verified could not tell a correct signature from a
    self-consistent one made by the wrong certificate, which is the failure
    that silently costs every user their permissions.
    """

    steps: List[PlanStep] = []
    if pinned:
        for binary in target.binaries:
            path = _artifact_path(target, binary.name)
            steps.append(
                PlanStep(
                    name=f"pin-{binary.identifier}",
                    kind=STEP_ASSERT,
                    argv=[CODESIGN, "-d", "--verbose=4", path],
                    expect=f"{_AUTHORITY_PREFIX}{pinned}",
                    purpose=f"confirm {binary.name} carries the pinned signing identity",
                )
            )
        bundle = target.app_bundle
        if bundle is not None:
            steps.append(
                PlanStep(
                    name="pin-app-bundle",
                    kind=STEP_ASSERT,
                    argv=[CODESIGN, "-d", "--verbose=4", _artifact_path(target, bundle.name)],
                    expect=f"{_AUTHORITY_PREFIX}{pinned}",
                    purpose="confirm the bundle carries the pinned signing identity",
                )
            )
    for binary in target.binaries:
        steps.append(
            PlanStep(
                name=f"verify-{binary.identifier}",
                argv=[CODESIGN, "--verify", "--strict", _artifact_path(target, binary.name)],
                purpose=f"verify the signature on {binary.name} is intact",
            )
        )
    bundle = target.app_bundle
    if bundle is not None:
        steps.append(
            PlanStep(
                name="verify-app-bundle",
                argv=[CODESIGN, "--verify", "--strict", "--deep", _artifact_path(target, bundle.name)],
                purpose="verify the bundle seal, including the code nested inside it",
            )
        )
    return steps


def _unsupported(target: config_module.ReleaseTarget) -> Optional[str]:
    """Why this typed target cannot be built yet, or ``None`` if it can."""

    if target.platform != config_module.MVP_PLATFORM:
        return (
            f"platform {target.platform!r} is a valid Continuum target but is not built "
            f"yet; the MVP adapter builds {config_module.MVP_PLATFORM} only"
        )
    if target.build_strategy != config_module.MVP_BUILD_STRATEGY:
        return (
            f"build strategy {target.build_strategy!r} is a valid Continuum target but "
            f"is not built yet; the MVP adapter builds "
            f"{config_module.MVP_BUILD_STRATEGY} only"
        )
    if target.distribution == config_module.DISTRIBUTION_APP_STORE:
        return (
            "app-store distribution requires a paid Apple developer identity and "
            "notarization, which this adapter does not implement"
        )
    return None


def _unsupported_signing(signing: config_module.ReleaseSigningSettings) -> Optional[str]:
    if signing.mode == config_module.SIGNING_DEVELOPER_ID:
        return (
            "signing.mode 'developer-id' is a valid Continuum target but is not "
            "implemented; the MVP adapter signs with a self-signed identity. "
            "Developer ID and notarization are follow-up work and are not required "
            "for the self-signed-stable profile."
        )
    return None


def plan_for(
    target: config_module.ReleaseTarget,
    *,
    environment: Optional[Dict[str, str]] = None,
) -> ReleasePlan:
    """Build the plan for one validated target.

    The only decision this function makes is which signing profile applies, and
    it makes it from what is actually present rather than from what was asked
    for. Everything else is assembly.
    """

    if target.adapter != ADAPTER_NAME:
        raise TargetNotExecutable(
            f"target {target.id!r} names adapter {target.adapter!r}, which this module "
            "does not implement"
        )
    reason = _unsupported(target)
    if reason:
        raise TargetNotExecutable(f"target {target.id!r}: {reason}")
    signing_reason = _unsupported_signing(target.signing)
    if signing_reason:
        raise TargetNotExecutable(f"target {target.id!r}: {signing_reason}")

    env = dict(environment or {})
    signing = target.signing
    degraded = False
    degradation_reason = ""
    notes: List[str] = []
    keychain = ""

    if signing.mode == config_module.SIGNING_ADHOC:
        identity = "-"
        degraded = True
        degradation_reason = _ADHOC_DEGRADATION
        notes.append(
            "ad-hoc signature: no certificate is used and no stable identity is "
            "preserved; artifacts are for local and fork use, not for a release a "
            "user will keep"
        )
    else:
        if material_present(signing, env):
            identity = signing.identity
            keychain = keychain_path()
        elif signing.allow_adhoc_fallback:
            identity = "-"
            degraded = True
            degradation_reason = (
                "the configured signing secrets are not available in this job, and "
                "this target allows falling back, so the release carries no stable "
                "identity: the designated requirement and the permissions granted to "
                "the previous release are not preserved"
            )
            notes.append(
                "falling back to an ad-hoc signature because the signing secrets for "
                + " and ".join(signing.secret_names())
                + " are not available. Forks and manual runs without access to the "
                "signing secrets land here; a release that must preserve a stable "
                "identity should set signing.allow_adhoc_fallback: false so this "
                "fails loudly instead."
            )
        else:
            raise SigningUnavailable(
                f"target {target.id!r} signs with the stable identity "
                f"{signing.identity!r}, but "
                + " and ".join(signing.secret_names())
                + " are not available in this job. Set signing.allow_adhoc_fallback: "
                "true to accept a visibly degraded ad-hoc build instead of failing."
            )

    steps: List[PlanStep] = []
    if keychain:
        steps.extend(_keychain_steps(signing, keychain))
    steps.extend(
        _sign_steps(target, identity=identity, keychain=keychain, timestamp=signing.timestamp)
    )
    steps.extend(_verify_steps(target, pinned=signing.identity if not degraded else ""))
    if keychain:
        steps.append(_delete_keychain_step(keychain))

    return ReleasePlan(
        target=target.id,
        adapter=target.adapter,
        platform=target.platform,
        build_strategy=target.build_strategy,
        distribution=target.distribution,
        step_list=tuple(steps),
        signing_mode=signing.mode,
        signing_identity="" if degraded else signing.identity,
        pinned_identity="" if degraded else signing.identity,
        signature_pinned=not degraded,
        degraded=degraded,
        degradation_reason=degradation_reason,
        hardened_runtime=target.hardened_runtime,
        timestamp=signing.timestamp,
        artifacts=target.artifacts,
        architectures=target.architectures,
        universal=target.universal,
        publishers=target.publishers,
        required_secrets=signing.secret_names(),
        required_env=(VERSION_ENV,) if target.app_bundle is not None else (),
        scratch_paths=(keychain,) if keychain else (),
        notes=tuple(notes),
    )


def build_plan(
    target: config_module.ReleaseTarget,
    *,
    environment: Optional[Dict[str, str]] = None,
) -> ReleasePlan:
    return plan_for(target, environment=environment)


__all__ = [
    "ADAPTER_NAME",
    "CODESIGN",
    "P12_ENV",
    "P12_PASSWORD_ENV",
    "SECURITY",
    "STEP_ASSERT",
    "STEP_RUN",
    "ReleaseError",
    "ReleasePlan",
    "SigningUnavailable",
    "TargetNotExecutable",
    "available",
    "bind_material",
    "build_plan",
    "classify_failure",
    "classify_import_failure",
    "classify_sign_failure",
    "ensure_available",
    "keychain_path",
    "material_present",
    "p12_path",
    "plan_for",
]
