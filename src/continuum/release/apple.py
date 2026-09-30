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
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import config as config_module
from . import archive as archive_module
from . import plan as plan_module
from . import run as run_module
from .contract import (
    CHECKSUMS_FILE,
    SIGNING_DEGRADED,
    SIGNING_NOT_APPLICABLE,
    SIGNING_SIGNED,
    TYPE_ARCHIVE,
    TYPE_BINARY,
    TYPE_CHECKSUMS,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    TargetSpec,
    VERIFIED,
    VerificationReport,
    digest_file,
)
from .plan import (
    ENCODING_BASE64,
    STEP_ASSERT,
    STEP_COPY,
    STEP_MATERIALIZE,
    STEP_RUN,
    PlanStep,
    ReleaseError,
    ReleasePlan,
    SigningUnavailable,
    TargetNotExecutable,
)
from .run import CommandRunner, Runner

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
    """Whether this adapter can run here at all.

    The build tools count as well as the signing ones. A machine with `codesign`
    and no `swift` would answer "yes" here and then fail on the first build step,
    which reports as a release failure when it is really an unexecutable target —
    and the contract has a word for that: the target cannot run here, say so
    before the plan does any work.
    """

    if not os.path.isdir("/System/Library"):
        return False
    if not all(os.access(path, os.X_OK) for path in (SECURITY, CODESIGN, PLISTBUDDY, LIPO)):
        return False
    return shutil.which(SWIFT) is not None


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
    if step_name.startswith("build-"):
        return (
            "build-failed",
            "the product could not be compiled",
            "the product name must be a SwiftPM product of this checkout, and the "
            "architecture must be one this adapter can build for; the plan names the "
            "compiler target triple it used",
        )
    if step_name.startswith("merge-") or step_name.startswith("create-universal"):
        return (
            "universal-merge-failed",
            "the per-architecture products could not be merged into one binary",
            "every declared architecture must have been built before the merge; a "
            "missing slice is a build failure upstream of this one",
        )
    if step_name.startswith("stage-") or step_name.startswith("assemble-bundle-"):
        return (
            "staging-failed",
            "a build product or bundle resource could not be placed where the plan "
            "says it belongs",
            "every source path in a target is repository-relative and every product "
            "is staged by this adapter; a failure here means the checkout does not "
            "contain what the target names, or the products were not built",
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


#: Where a target's built products live, relative to the checkout. This is
#: SwiftPM's own release-configuration directory, so a plan names the same path
#: whether or not this adapter built it — and `release sign` on a repository
#: that built its own binaries signs the products where SwiftPM left them.
DEFAULT_PRODUCTS_ROOT = os.path.join(".build", "release")

#: Where this adapter's per-architecture staging lives, and where SwiftPM is told
#: to build so two architectures cannot overwrite each other's products. A target
#: declaring two architectures produces one archive per architecture, because
#: two builds of the same product in one directory is one of them lost.
STAGE_ROOT = os.path.join(".build", "continuum-release")

#: Where the published archives are written.
DIST_ROOT = "dist"


def products_root(arch: str = "") -> str:
    """The directory holding one architecture's products."""

    return os.path.join(STAGE_ROOT, arch) if arch else DEFAULT_PRODUCTS_ROOT


def _artifact_path(name: str, root: str = DEFAULT_PRODUCTS_ROOT) -> str:
    return os.path.join(root, name)


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


def _bundle_assembly_steps(
    bundle: config_module.ReleaseAppBundle,
    *,
    root: str,
    target: config_module.ReleaseTarget,
) -> List[PlanStep]:
    """Put a sealable bundle together, in the order the seal depends on.

    Three claims are being made here, and all three are properties of *when*
    these steps run rather than of what they contain:

    * the bundle's nested binaries are the ones already signed — so a bundle
      sealed over unsigned copies is a bundle whose `--deep` verification fails,
      or worse, one that passes because the copies were signed with something
      else;
    * the `Info.plist` is in place before the version is stamped into it,
      because `PlistBuddy` writes to the file it is given;
    * the resources are in place before the seal, because the seal covers them.

    So these steps sit between the per-binary signing steps and the seal, and
    they are steps in the plan rather than work the adapter performs behind the
    plan's back: the ordering is exactly what a reviewer has to check.

    Every path here is repository-relative and the runner resolves it against
    the checkout, so the same plan reads the same whether it is printed by
    `release plan` or executed in a job.
    """

    bundle_root = _artifact_path(bundle.name, root)
    macos_root = os.path.join(bundle_root, "Contents", "MacOS")
    steps: List[PlanStep] = []

    if bundle.info_plist:
        steps.append(
            PlanStep(
                name="assemble-bundle-info-plist",
                kind=STEP_COPY,
                argv=(bundle.info_plist, info_plist_path(bundle_root)),
                purpose=(
                    "place the bundle's Info.plist, which the version stamp below "
                    "writes into and the seal below covers"
                ),
            )
        )

    if bundle.resources:
        steps.append(
            PlanStep(
                name="assemble-bundle-resources",
                kind=STEP_COPY,
                argv=tuple(
                    token
                    for resource in bundle.resources
                    for token in (
                        resource,
                        os.path.join(bundle_root, "Contents", "Resources", os.path.basename(resource)),
                    )
                ),
                purpose=(
                    "place the resources the sealed bundle ships; the seal covers them, "
                    "so they belong inside it rather than beside it"
                ),
            )
        )

    copies: List[str] = []
    for binary in target.binaries:
        copies.extend([_artifact_path(binary.name, root), os.path.join(macos_root, binary.name)])
    if copies:
        steps.append(
            PlanStep(
                name="assemble-bundle-binaries",
                kind=STEP_COPY,
                argv=tuple(copies),
                purpose=(
                    "place the just-signed binaries inside the bundle, so the seal goes "
                    "over code that is already signed rather than over a second, "
                    "unsigned copy of the same product"
                ),
            )
        )
    return steps


def _sign_steps(
    target: config_module.ReleaseTarget,
    *,
    identity: str,
    keychain: str,
    timestamp: bool,
    root: str = DEFAULT_PRODUCTS_ROOT,
) -> List[PlanStep]:
    steps: List[PlanStep] = []
    for binary in target.binaries:
        path = _artifact_path(binary.name, root)
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
        path = _artifact_path(bundle.name, root)
        steps.extend(_bundle_assembly_steps(bundle, root=root, target=target))
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


def _verify_steps(
    target: config_module.ReleaseTarget,
    pinned: str,
    root: str = DEFAULT_PRODUCTS_ROOT,
) -> List[PlanStep]:
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
            path = _artifact_path(binary.name, root)
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
                    argv=[CODESIGN, "-d", "--verbose=4", _artifact_path(bundle.name, root)],
                    expect=f"{_AUTHORITY_PREFIX}{pinned}",
                    purpose="confirm the bundle carries the pinned signing identity",
                )
            )
    for binary in target.binaries:
        steps.append(
            PlanStep(
                name=f"verify-{binary.identifier}",
                argv=[CODESIGN, "--verify", "--strict", _artifact_path(binary.name, root)],
                purpose=f"verify the signature on {binary.name} is intact",
            )
        )
    bundle = target.app_bundle
    if bundle is not None:
        steps.append(
            PlanStep(
                name="verify-app-bundle",
                argv=[CODESIGN, "--verify", "--strict", "--deep", _artifact_path(bundle.name, root)],
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
    products: str = DEFAULT_PRODUCTS_ROOT,
) -> ReleasePlan:
    """Build the plan for one validated target.

    The only decision this function makes is which signing profile applies, and
    it makes it from what is actually present rather than from what was asked
    for. Everything else is assembly.

    `products` is the directory holding the built binaries, defaulting to the
    one `release plan` and `release sign` have always named. A caller that built
    one architecture's products elsewhere passes that directory here, so the
    same plan signs the products this adapter staged rather than a second,
    unsigned copy of them.
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
        _sign_steps(
            target,
            identity=identity,
            keychain=keychain,
            timestamp=signing.timestamp,
            root=products,
        )
    )
    steps.extend(_verify_steps(target, pinned=signing.identity if not degraded else "", root=products))
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


# -- building ---------------------------------------------------------------

#: Resolved through PATH rather than named absolutely: a Swift toolchain is
#: installed wherever `setup-swift` put it, and there is no one place on a macOS
#: runner where it always is.
SWIFT = "swift"

#: `lipo` is part of the system toolchain and lives at a fixed path.
LIPO = "/usr/bin/lipo"

#: `rm` is likewise, and it is named absolutely so a stray directory earlier in
#: PATH cannot decide what a teardown deletes.
REMOVE = "/bin/rm"

#: Also part of the system toolchain, and also absolute, for the same reason:
#: every filesystem effect in a plan should be attributable to a known binary.
MKDIR = "/bin/mkdir"

#: The macOS version every product is built for. Declared here rather than
#: configured because a build cannot be cross-compiled without one, and a target
#: that needed a different floor would need a schema field for it — which is a
#: consumer-facing change rather than something to smuggle in as a default.
MACOS_FLOOR = "11.0"

_ARCH_TRIPLES = {
    config_module.ARCH_X86_64: f"x86_64-apple-macosx{MACOS_FLOOR}",
    config_module.ARCH_ARM64: f"arm64-apple-macosx{MACOS_FLOOR}",
}

#: The one architecture label used when a target ships a single fat binary
#: instead of one archive per architecture.
UNIVERSAL_ARCH = "universal"

_MACHO_MEDIA_TYPE = "application/x-mach-binary"


class AppleError(ContractError):
    """The Apple adapter cannot honestly continue.

    Carries a stable code and a retryable flag, the same shape every classified
    failure in the release core uses, so a job summary can tell "the signing
    secrets were not exposed to this run" from "the toolchain is broken" without
    reading prose.
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


def triple_for(arch: str) -> str:
    """The clang target triple one architecture is built for."""

    triple = _ARCH_TRIPLES.get(arch)
    if triple is None:
        raise AppleError(
            "architecture-unsupported",
            f"architecture {arch!r} has no build triple; supported: "
            + ", ".join(sorted(_ARCH_TRIPLES)),
        )
    return triple


def build_argv(binary: config_module.ReleaseBinary, *, arch: str, scratch: str) -> List[str]:
    """The `swift build` invocation for one product, for one architecture.

    Three decisions are visible in this vector:

    * **The triple is passed to the compiler, not guessed from the runner.** A
      release declares the architectures it ships; building the host's slice of
      a two-architecture target and calling it the release is how an x86_64
      binary ends up published under an `arm64` name.
    * **`--scratch-path` separates the builds.** SwiftPM writes every product to
      one release directory, so two architectures built in the same directory is
      one of them overwritten rather than both shipped.
    * **Linker flags are tokens, passed one per `-Xlinker`.** A target embedding
      an `__info_plist` section needs the section created at link time, and the
      value stays data: there is no shell anywhere on this path.
    """

    argv = [
        SWIFT,
        "build",
        "-c",
        "release",
        "--product",
        binary.name,
        "--scratch-path",
        scratch,
        "-Xswiftc",
        "-target",
        "-Xswiftc",
        triple_for(arch),
    ]
    for flag in binary.link_flags:
        argv += ["-Xlinker", flag]
    return argv


def _scratch_path(arch: str) -> str:
    return os.path.join(STAGE_ROOT, arch, ".swiftpm")


def _staged_path(arch: str, name: str) -> str:
    return os.path.join(products_root(arch), name)


def build_plan_for(target: config_module.ReleaseTarget, *, arch: str) -> ReleasePlan:
    """The plan that turns a checkout into this architecture's signed-ready products.

    The plan is a build plan and it pins nothing: `signature_pinned` is false
    because *this* plan signs nothing. The identity a release is checked against
    belongs to the signing plan, which asserts it, and a build plan that claimed
    a pin would be claiming a check it never made.
    """

    scratch = _scratch_path(arch)
    steps: List[PlanStep] = []
    for binary in target.binaries:
        steps.append(
            PlanStep(
                name=f"build-{arch}-{binary.name}",
                argv=tuple(build_argv(binary, arch=arch, scratch=scratch)),
                purpose=(
                    f"compile {binary.name} for {arch} from the approved source SHA, with "
                    "the linker flags this product declares"
                ),
            )
        )
    copies: List[str] = []
    for binary in target.binaries:
        copies.extend(
            [
                os.path.join(scratch, "release", binary.name),
                _staged_path(arch, binary.name),
            ]
        )
    steps.append(
        PlanStep(
            name=f"stage-{arch}",
            kind=STEP_COPY,
            argv=tuple(copies),
            purpose=(
                "move the built products out of the build directory, so what gets "
                "signed is what was built rather than whatever a later build leaves "
                "in the same place"
            ),
        )
    )
    steps.append(
        PlanStep(
            name=f"remove-build-{arch}",
            argv=[REMOVE, "-rf", scratch],
            purpose="remove the compiler intermediates; the products are already staged",
            teardown=True,
        )
    )
    return ReleasePlan(
        target=target.id,
        adapter=target.adapter,
        platform=target.platform,
        build_strategy=target.build_strategy,
        distribution=target.distribution,
        step_list=tuple(steps),
        signing_mode=target.signing.mode,
        signature_pinned=False,
        hardened_runtime=target.hardened_runtime,
        timestamp=target.signing.timestamp,
        artifacts=target.artifacts,
        architectures=(arch,),
        universal=target.universal,
        publishers=target.publishers,
    )


def universal_plan_for(
    target: config_module.ReleaseTarget, *, architectures: Sequence[str]
) -> ReleasePlan:
    """The plan that merges the per-architecture products into fat binaries.

    A universal target ships one archive rather than two, because a user installs
    one binary. The per-architecture slices are separate builds of the same
    product, and `lipo` is the only thing that can put them back into one file
    that one signature then covers.

    The merged products are written to their own directory rather than over one
    architecture's, for the same reason the builds were separated: the inputs
    are the outputs, and merging in place would leave the plan signing a
    directory it had just partly consumed.
    """

    merged = products_root(UNIVERSAL_ARCH)
    steps: List[PlanStep] = [
        PlanStep(
            name="create-universal-products",
            argv=[MKDIR, "-p", merged],
            purpose=(
                "create the directory the merged products are written into; `lipo` "
                "writes the file it is told to write and does not create the "
                "directory for it"
            ),
        )
    ]
    for binary in target.binaries:
        inputs = [_staged_path(arch, binary.name) for arch in architectures]
        steps.append(
            PlanStep(
                name=f"merge-{binary.name}",
                argv=[
                    LIPO,
                    "-create",
                    *inputs,
                    "-output",
                    _staged_path(UNIVERSAL_ARCH, binary.name),
                ],
                purpose=(
                    f"merge the {', '.join(architectures)} slices of {binary.name} into one "
                    "binary, so a universal release ships one artifact per product"
                ),
            )
        )
    steps.append(
        PlanStep(
            name="remove-architecture-slices",
            argv=[REMOVE, "-rf", *(products_root(arch) for arch in architectures)],
            purpose=(
                "remove the per-architecture slices now that they are merged; leaving "
                "them would let a later plan sign a slice and ship it under the "
                "universal label"
            ),
            teardown=True,
        )
    )
    return ReleasePlan(
        target=target.id,
        adapter=target.adapter,
        platform=target.platform,
        build_strategy=target.build_strategy,
        distribution=target.distribution,
        step_list=tuple(steps),
        signing_mode=target.signing.mode,
        signature_pinned=False,
        hardened_runtime=target.hardened_runtime,
        timestamp=target.signing.timestamp,
        artifacts=target.artifacts,
        architectures=tuple(architectures),
        universal=True,
        publishers=target.publishers,
    )


def published_architectures(target: config_module.ReleaseTarget) -> Tuple[str, ...]:
    """The architecture labels this target's archives are named for.

    A universal target publishes one `universal` archive; anything else publishes
    one archive per declared architecture. The difference is a naming decision
    with a real consequence — a Homebrew formula pins one hash per architecture
    slot — so it is made from configuration rather than inferred later.
    """

    if target.universal:
        return (UNIVERSAL_ARCH,)
    return tuple(target.architectures)


# -- packaging --------------------------------------------------------------

_CHECKSUMS_MEDIA_TYPE = "text/plain"


def checksum_name(target: config_module.ReleaseTarget) -> str:
    """The file name this target's checksum listing is written under.

    Named after the target because a release's assets share one flat namespace:
    two targets both shipping `SHA256SUMS.txt` would collide, and the contract
    refuses a collision rather than picking a winner.
    """

    return f"{target.id}-{CHECKSUMS_FILE}"


def artifact_stem(target: config_module.ReleaseTarget) -> str:
    """The product name a target's archives are named after.

    A bundle wins over a bare binary because a bundle is what a user installs:
    `NanoDictate-1.2.3-macos-arm64.tar.gz` is the name a person recognises and
    looks for, and naming the archive after the executable underneath it would
    make a target that ships both appear to ship one product twice.
    """

    bundle = target.app_bundle
    if bundle is not None:
        return os.path.splitext(bundle.name)[0]
    if target.binaries:
        return target.binaries[0].name
    return target.id


def archive_name(
    target: config_module.ReleaseTarget,
    *,
    version: str,
    arch: str,
    artifact_format: str,
) -> str:
    """The published file name for one architecture's archive.

    Every part of it is in the name on purpose. A release that publishes
    `dist/a.tar.gz` makes a reader guess the version, the platform, and the
    architecture; a Homebrew formula that pins a hash then cannot tell which
    file the hash belongs to.
    """

    suffix = archive_module.extension_for(artifact_format)
    return f"{artifact_stem(target)}-{version}-macos-{arch}{suffix}"


def archive_members(
    target: config_module.ReleaseTarget,
    *,
    products: str,
    workdir: str,
) -> Tuple[archive_module.Member, ...]:
    """Everything one architecture's archive contains, in a fixed order.

    The archive holds the signed products and the bundle that was sealed over
    them. Declared resources are included as their own entries too, because the
    release this replaces shipped the same files beside the bundle: a reader
    comparing the two archives sees the products and can still get the licence
    without unpacking a signed directory to find it.

    A resource is a repository-relative path and a member name is a single path
    segment, so the declared path is flattened to its basename. That is a
    deliberate loss: two resources in different directories with one basename
    would collide, and `combine()` refuses to choose between them rather than
    shipping whichever happened to be second.
    """

    groups: List[Sequence[archive_module.Member]] = []
    groups.append(
        [
            archive_module.Member(source=_artifact_path(binary.name, products), name=binary.name)
            for binary in target.binaries
        ]
    )

    bundle = target.app_bundle
    if bundle is not None:
        groups.append(archive_module.members_for(products, (bundle.name,)))
        if bundle.resources:
            groups.append(
                [
                    archive_module.Member(
                        source=os.path.join(workdir, resource),
                        name=os.path.basename(resource),
                    )
                    for resource in bundle.resources
                ]
            )
    return archive_module.combine(*groups)


def package(
    target: config_module.ReleaseTarget,
    *,
    version: str,
    arch: str,
    artifact_format: str,
    products: str,
    workdir: str,
    dist: str,
    epoch: Optional[int] = None,
) -> str:
    """Write one archive and return its path.

    Every input to the bytes is fixed before the file is opened: the members are
    sorted, their timestamps come from `SOURCE_DATE_EPOCH` or from the fixed
    default, their ownership is zero, and the compression is pinned to a fixed
    level. Re-running a release of one commit therefore produces the same file
    the checksums a downstream consumer recorded still describe.
    """

    name = archive_name(target, version=version, arch=arch, artifact_format=artifact_format)
    path = os.path.join(dist, name)
    archive_module.write(
        path,
        archive_members(target, products=products, workdir=workdir),
        artifact_format=artifact_format,
        epoch=epoch,
    )
    return path


def archive_media_type(artifact_format: str) -> str:
    """The media type a published archive is labelled with.

    Not decoration: a destination that decides what to offer as an installable
    download reads this, and a `.zip` labelled as a gzip archive is an archive
    that extracts wrongly or not at all.
    """

    if artifact_format == config_module.ARTIFACT_ZIP:
        return archive_module.ZIP_MEDIA_TYPE
    return archive_module.TAR_GZ_MEDIA_TYPE


# -- the walkable adapter ----------------------------------------------------


def settings_from(target: TargetSpec) -> config_module.ReleaseTarget:
    """Read a target's options as an Apple release target.

    The core hands a target to its adapter as `TargetSpec.options` and knows
    nothing about it beyond an id and an adapter name. Re-parsing those options
    with the same parser the file went through is what keeps a hand-assembled
    target honest: it is held to the schema rather than being privileged by the
    route it arrived on.
    """

    if target.adapter != ADAPTER_NAME:
        raise AppleError(
            "wrong-adapter",
            f"target {target.id!r} names adapter {target.adapter!r}, not {ADAPTER_NAME!r}",
        )
    options = {key: value for key, value in target.options}
    try:
        return config_module.parse_release_target(options, f"target {target.id!r}")
    except config_module.ConfigError as exc:
        raise AppleError("target-options-invalid", str(exc)) from exc


class AppleAdapter:
    """Builds, signs, packages, and verifies one Apple target.

    Every decision this class makes about a target is made from the target's own
    configuration, and every effect it has goes through a `ReleasePlan` executed
    by `Runner`. Nothing here shells out on its own: the plan is the artifact a
    reviewer can read, a dry run is the same plan without execution, and the
    same plan is what `release plan` prints.
    """

    SUPPORTS_DRY_RUN = True
    name = ADAPTER_NAME

    def __init__(
        self,
        *,
        environment: Optional[Dict[str, str]] = None,
        workdir: str = "",
        dist: str = DIST_ROOT,
        git_revision: Optional[Callable[[str], str]] = None,
        command_runner: Optional[CommandRunner] = None,
        is_available: Optional[bool] = None,
    ) -> None:
        # A caller that passes no environment gets the sanitized job environment
        # rather than an empty one: `swift` and `security` are found through PATH,
        # and a plan whose environment has no PATH fails with "no such file or
        # directory" for a tool that is installed.
        self.environment: Dict[str, str] = (
            dict(environment) if environment is not None else dict(run_module.base_environment())
        )
        self.workdir = workdir
        self.dist = dist
        #: Injected so this adapter can be exercised, and so the checkout check
        #: is one the caller controls rather than one that reaches for a
        #: repository or a global.
        self.git_revision = git_revision
        #: Injected so a whole Apple plan can run on a machine with no macOS
        #: toolchain. The plan executed is the same plan either way; only the
        #: process boundary is replaced, so a test exercises the real ordering.
        self.command_runner = command_runner
        #: Overrides the toolchain probe. `None` asks the machine, which is what
        #: production does; a caller sets it when the tools arrive some other way
        #: than by being installed here.
        self.is_available = is_available

    def available(self) -> bool:
        """Whether the tools this target needs are on this machine.

        Delegated to the module-level `available()` so the answer a plan gives
        and the answer the adapter gives cannot drift apart.
        """

        if self.is_available is not None:
            return self.is_available
        return available()

    def intent(self) -> str:
        return (
            "compile each declared architecture from the approved source SHA, sign the "
            "products with the configured identity, seal the app bundle over those "
            "signed products, assert the pinned identity, and write one deterministic "
            "archive per architecture"
        )

    # -- helpers ------------------------------------------------------------
    def _settings(self, request: BuildRequest) -> config_module.ReleaseTarget:
        return settings_from(request.target)

    def _root(self, request: BuildRequest) -> str:
        return request.workdir or self.workdir

    def _runner(
        self, request: BuildRequest, environment: Optional[Dict[str, str]] = None
    ) -> Runner:
        return Runner(
            environment=self.environment if environment is None else environment,
            workdir=self._root(request),
            dry_run=request.dry_run,
            command_runner=self.command_runner,
        )

    def _execute(
        self,
        request: BuildRequest,
        plan: ReleasePlan,
        stage: str,
        environment: Optional[Dict[str, str]] = None,
    ) -> None:
        """Run one plan and turn a step failure into a classified error.

        The tool's own words travel with the refusal, because that is the only
        place they exist: the classification says which invariant broke, and the
        detail says which line of `swift`'s or `codesign`'s output says so. The
        core reports this exception's message and nothing else, so a detail left
        out here is a detail no reader ever sees — and a reader who cannot match
        a failure to the tool's output costs a whole round trip to diagnose. The
        runner has already truncated and redacted it.
        """

        result = self._runner(request, environment).run(plan)
        failure = result.failure
        if failure is not None:
            code, message, remediation = classify_failure(failure.step, failure.detail)
            detail = f" The tool said: {failure.detail}" if failure.detail else ""
            raise AppleError(
                code,
                f"target {request.target.id!r} {stage} failed at step "
                f"{failure.step!r}: {failure.message}. {message}.{detail}",
                remediation=remediation,
            )

    def _assert_source(self, request: BuildRequest) -> None:
        """Refuse to build anything but the approved commit.

        The core pins a release to one SHA; this is the adapter's own check that
        the checkout it is about to build is that commit. A moved checkout builds
        bytes no reviewed commit produced.
        """

        if self.git_revision is None:
            # No reader was injected, so this adapter was built to release
            # whatever it was pointed at. That is a caller's explicit choice,
            # not something to second-guess.
            return
        if request.dry_run:
            # Nothing is built, so there are no unreviewed bytes to ship.
            return
        found = self.git_revision(self._root(request))
        if not found:
            raise AppleError(
                "source-unreadable",
                f"cannot read the commit of the checkout at {self._root(request)!r}. A "
                "release has to be built from the commit it was approved for, and a "
                "directory with no readable HEAD — a tarball, a vendored copy, a "
                "directory that is not the repository — cannot promise which one that "
                "is. Releasing it anyway would build bytes nobody approved.",
            )
        if found != request.source_sha:
            raise AppleError(
                "source-mismatch",
                f"the checkout is at {found} but this release is approved for "
                f"{request.source_sha}; building would ship unreviewed bytes",
            )

    def _architectures(self, target: config_module.ReleaseTarget) -> Tuple[str, ...]:
        """The architectures to compile, in the order they are compiled.

        Every declared architecture, for a universal target too: a universal
        binary is `lipo` output over separately compiled slices, so a universal
        release whose `lipo` step is asked to merge a single architecture
        produces a one-architecture "universal" binary wearing the wrong name.

        Sequential, and that ordering is a fact about SwiftPM rather than about
        taste: two `swift build` invocations sharing one package directory race
        on the manifest cache. Each build gets its own `--scratch-path` so they
        do not race on products either.
        """

        return tuple(target.architectures)

    # -- build --------------------------------------------------------------
    def build(self, request: BuildRequest) -> ArtifactManifest:
        """Compile the target and stage its products.

        Products rather than archives, because an archive of unsigned bytes would
        have to be rebuilt after signing: the bytes a user installs are the bytes
        that were signed, so the packaging happens in `sign()` where it can see
        the sealed bundle.
        """

        target = self._settings(request)
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=self.name,
            source_sha=request.source_sha,
            version=request.version,
        )
        root = self._root(request)
        architectures = self._architectures(target)

        if request.dry_run:
            self._declare_products(builder, target, request.version, root)
            return builder.build()

        self._assert_source(request)
        for arch in architectures:
            self._execute(request, build_plan_for(target, arch=arch), f"build for {arch}")
        if target.universal:
            self._execute(
                request,
                universal_plan_for(target, architectures=architectures),
                "universal merge",
            )
        for arch in published_architectures(target):
            for binary in target.binaries:
                builder.record(
                    name=self._product_name(target, request.version, arch, binary.name),
                    path=os.path.join(root, products_root(arch), binary.name),
                    type=TYPE_BINARY,
                    platform=target.platform,
                    arch=arch,
                    media_type=_MACHO_MEDIA_TYPE,
                )
        return builder.build()

    def _product_name(
        self,
        target: config_module.ReleaseTarget,
        version: str,
        arch: str,
        binary_name: str,
    ) -> str:
        return f"{binary_name}-{version}-{target.platform}-{arch}"

    def _declare_products(
        self,
        builder: ManifestBuilder,
        target: config_module.ReleaseTarget,
        version: str,
        root: str,
    ) -> None:
        """Name the staged products a build will produce.

        The published architectures rather than the compiled ones: for a
        universal target the per-architecture products are inputs to `lipo` and
        are deleted once merged, so declaring them would describe files that the
        build deliberately leaves behind. A dry run therefore names the same rows
        a real run records, for the same reason — a plan that describes a
        different release than the one that runs is not a plan.
        """

        for arch in published_architectures(target):
            for binary in target.binaries:
                builder.declare(
                    name=self._product_name(target, version, arch, binary.name),
                    path=os.path.join(root, products_root(arch), binary.name),
                    type=TYPE_BINARY,
                    platform=target.platform,
                    arch=arch,
                    media_type=_MACHO_MEDIA_TYPE,
                )

    # -- signing ------------------------------------------------------------
    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        """Sign the staged products, seal the bundle, and package what was sealed.

        The archives are written here rather than in `build()` because an archive
        has to contain the sealed bundle, and the bundle is sealed here. The
        manifest handed in records the unsigned products; the one returned
        records what a user would install, because that is the only set of bytes
        the destination can publish.
        """

        target = self._settings(request)
        root = self._root(request)
        dist = os.path.join(root, self.dist)
        if request.dry_run:
            builder = ManifestBuilder(
                target=request.target.id,
                adapter=self.name,
                source_sha=request.source_sha,
                version=request.version,
            )
            self._declare_archives(builder, target, request.version, dist)
            self._declare_checksums(builder, target, dist)
            return builder.build()

        environment = bind_material(target.signing, self.environment)
        # The version is part of the signing environment rather than of the
        # adapter's configuration: it belongs to the release being cut, and the
        # plan stamps it into the bundle before the seal.
        environment[VERSION_ENV] = request.version
        epoch = archive_module.epoch_from(self.environment)

        identity = ""
        degraded = False
        for arch in published_architectures(target):
            plan = plan_for(target, environment=environment, products=products_root(arch))
            degraded = degraded or plan.degraded
            if plan.signing_identity:
                identity = plan.signing_identity
            self._execute(request, plan, f"signing for {arch}", environment)

        builder = ManifestBuilder(
            target=request.target.id,
            adapter=self.name,
            source_sha=request.source_sha,
            version=request.version,
        )
        signing = SIGNING_DEGRADED if degraded else SIGNING_SIGNED
        for arch in published_architectures(target):
            for artifact_format in target.artifacts:
                name = archive_name(
                    target, version=request.version, arch=arch, artifact_format=artifact_format
                )
                path = package(
                    target,
                    version=request.version,
                    arch=arch,
                    artifact_format=artifact_format,
                    products=os.path.join(root, products_root(arch)),
                    workdir=root,
                    dist=dist,
                    epoch=epoch,
                )
                builder.record(
                    name=name,
                    path=path,
                    type=TYPE_ARCHIVE,
                    platform=target.platform,
                    arch=arch,
                    classifier=artifact_format,
                    media_type=archive_media_type(artifact_format),
                    signing=signing,
                    signing_identity=identity,
                )
        self._record_checksums(builder, target, dist)
        return builder.build()

    def _declare_archives(
        self,
        builder: ManifestBuilder,
        target: config_module.ReleaseTarget,
        version: str,
        dist: str,
    ) -> None:
        """Name the archives a real run will write.

        Signing a bundle whose products are not signed yet has no plan analogue
        worth executing, so the dry run stops at the signing plan and then names
        the packaging outputs.

        The rows are declared unsigned, and the contract enforces it: a declared
        artifact does not exist yet, so it cannot report a signature. That is
        better than the alternative — a plan claiming a signed archive would be
        claiming a signature that has not happened — and the identity the run
        will sign with is stated by the signing plan, which is what a reviewer
        reads it for.
        """

        for arch in published_architectures(target):
            for artifact_format in target.artifacts:
                name = archive_name(
                    target, version=version, arch=arch, artifact_format=artifact_format
                )
                builder.declare(
                    name=name,
                    path=os.path.join(dist, name),
                    type=TYPE_ARCHIVE,
                    platform=target.platform,
                    arch=arch,
                    classifier=artifact_format,
                    media_type=archive_media_type(artifact_format),
                )

    def _declare_checksums(
        self, builder: ManifestBuilder, target: config_module.ReleaseTarget, dist: str
    ) -> None:
        """Name the checksum file a real run will write, without writing it.

        Declared rather than recorded because `write_checksums` writes a file, and
        a dry run that leaves a `SHA256SUMS.txt` in the working tree has dirtied
        a checkout it promised not to touch — and would hand the next release a
        checksum file it did not write.
        """

        name = checksum_name(target)
        builder.declare(
            name=name,
            path=os.path.join(dist, name),
            type=TYPE_CHECKSUMS,
            platform=target.platform,
            classifier=CHECKSUMS_FILE,
            media_type=_CHECKSUMS_MEDIA_TYPE,
        )

    def _record_checksums(
        self, builder: ManifestBuilder, target: config_module.ReleaseTarget, dist: str
    ) -> None:
        """Write the checksum listing a consumer verifies the release against.

        Written from the manifest, not from the `dist` directory, so it lists the
        archives this release published rather than whatever else happened to be
        lying around next to them. It is recorded afterwards and cannot list
        itself: a checksum file whose own digest depends on its own contents is a
        file no consumer could ever verify.

        Named after the target because a release's assets share one flat
        namespace — two targets both shipping `SHA256SUMS.txt` would collide, and
        the contract refuses a collision rather than picking a winner.
        """

        name = checksum_name(target)
        path = builder.build().write_checksums(dist, name)
        builder.record(
            name=name,
            path=path,
            type=TYPE_CHECKSUMS,
            platform=target.platform,
            classifier=CHECKSUMS_FILE,
            media_type=_CHECKSUMS_MEDIA_TYPE,
            signing=SIGNING_NOT_APPLICABLE,
        )

    # -- verification -------------------------------------------------------
    def verify(self, request: BuildRequest, manifest: ArtifactManifest) -> VerificationReport:
        """Re-read what was published and prove it is what was recorded.

        A digest check, not another signature check: `codesign --verify` already
        ran in the signing plan, and its verdict is in the journal. What this
        stage adds is that the files on disk are the files the manifest describes
        — which is the claim that silently rots when a packaging step rewrites,
        moves, or overwrites something after the fact.
        """

        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="planned",
                detail="a dry run declares its artifacts and verifies nothing",
                verified_by=self.name,
            )

        failures: List[str] = []
        for artifact in manifest.artifacts:
            if not os.path.isfile(artifact.path):
                failures.append(f"{artifact.name}: {artifact.path} is missing")
                continue
            size, digest = digest_file(artifact.path, artifact.digest_algorithm)
            if digest != artifact.digest or size != artifact.size:
                failures.append(
                    f"{artifact.name}: recorded {artifact.digest_algorithm} "
                    f"{artifact.digest} over {artifact.size} bytes, found {digest} over "
                    f"{size} bytes"
                )
        if failures:
            return VerificationReport(
                verified=False,
                code="apple-verification-failed",
                detail="; ".join(failures),
                failures=tuple(failures),
            )
        return VerificationReport(
            verified=True,
            code=VERIFIED.code,
            detail=(
                f"every artifact of {request.target.id!r} was re-read and still matches "
                "the digest recorded for it, over the same identity"
            ),
            verified_by=self.name,
        )


__all__ = [
    "ADAPTER_NAME",
    "CODESIGN",
    "DEFAULT_PRODUCTS_ROOT",
    "DIST_ROOT",
    "LIPO",
    "MACOS_FLOOR",
    "MKDIR",
    "P12_ENV",
    "P12_PASSWORD_ENV",
    "PLISTBUDDY",
    "REMOVE",
    "SECURITY",
    "STAGE_ROOT",
    "STEP_ASSERT",
    "STEP_COPY",
    "STEP_RUN",
    "SWIFT",
    "UNIVERSAL_ARCH",
    "AppleAdapter",
    "AppleError",
    "ReleaseError",
    "ReleasePlan",
    "SigningUnavailable",
    "TargetNotExecutable",
    "archive_media_type",
    "archive_members",
    "archive_name",
    "artifact_stem",
    "available",
    "bind_material",
    "build_argv",
    "build_plan",
    "build_plan_for",
    "checksum_name",
    "classify_failure",
    "classify_import_failure",
    "classify_sign_failure",
    "ensure_available",
    "keychain_path",
    "material_present",
    "package",
    "p12_path",
    "plan_for",
    "products_root",
    "published_architectures",
    "settings_from",
    "triple_for",
    "universal_plan_for",
]
