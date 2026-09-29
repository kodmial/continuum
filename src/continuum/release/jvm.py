"""The JVM release adapter: one build, one version, one signing identity.

This is the module that knows about Gradle, Maven, `gpg`, JARs, POMs, and Maven
coordinates. Nothing else in Continuum does.

A JVM release is not one kind of thing. A Java application ships something a
person runs — a jar with a `Main-Class`, usually inside a distribution archive —
and a Java or Kotlin library ships something a build resolves by coordinates and
never runs directly. The two are separate capabilities and the adapter keeps
them apart rather than pretending one build script can serve both:

* An **application** target builds the runnable jar and, when asked, a
  distribution zip or tar. Publishing it means attaching those bytes to a
  release. It needs no registry credential and no signing identity at all, which
  is the whole point of a distribution-first release.
* A **library** target builds the jar, the POM, and the sources and Javadoc jars
  a repository requires, and is the shape both GitHub Packages and Maven Central
  accept. Publication is a separate component's job (`maven_publish.py`); the
  adapter's obligation ends with a verified, correctly named, correctly versioned
  artifact set.

The properties this adapter refuses to give up, each of which is a way a JVM
release has gone wrong before:

* **One build, one source SHA.** Every requested output comes from a single
  wrapper invocation against the exact approved commit, and the working tree's
  HEAD is asserted to be that commit first. A jar attached to a release and a
  jar uploaded to a registry from two invocations are two builds, and only one
  of them is the one that was reviewed.
* **Gradle and Maven produce the same manifest.** The two build systems lay
  their output out differently, so the adapter *normalizes*: every artifact is
  recorded under the name its coordinates imply, whichever toolchain produced
  it. A release that ships `widgets-1.4.0.jar` and `widgets-1.4.0-sources.jar`
  from Gradle and the same two from Maven is one release, and a publisher must
  not have to know which toolchain built it.
* **The built version is checked, not assumed.** Gradle can be told the version
  on its command line and is; Maven cannot, because the POM is the truth there.
  So the coordinates are read back out of what was built and a release that
  stamped anything other than the approved version is refused. A Maven build of
  a POM still on `1.3.0` would otherwise publish `1.4.0`'s tag over `1.3.0`'s
  bytes, and no registry would notice.
* **Group and artifact identifiers are never rewritten.** A release maps *onto*
  Maven coordinates; it does not rename them. A consumer resolving
  `com.example:widgets` before the release must resolve the same artifact after
  it, so a generated or normalized `groupId` would silently fork a project's
  identity in the one namespace a project cannot move.
* **A signature names a key.** Signatures are made with a named key, the key is
  checked to exist in the keyring the job actually has, and every signature is
  verified back with `gpg --verify` before the release may publish. Key material
  is materialized for one job into a scratch keyring and removed in `finally`.
* **Both wrappers are not a choice.** A repository that ships both must say
  which one releases, because building with the other produces different bytes
  and the difference is invisible in the manifest.

The adapter implements the normalized `target-adapter` port from the release
contract: `build`, `sign`, and `verify`, each taking a `BuildRequest` and the
manifest so far. Its options arrive through `TargetSpec.options` untouched, so
no JVM detail leaks into the core state machine, and the whole build is isolated
behind an injectable command runner — so every decision in here is testable on a
runner with no JDK, no Gradle, no Maven, and no GPG installed.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import (
    CHECKSUMS_FILE,
    PROVENANCE_DECLARED,
    SIGNING_SIGNED,
    TYPE_ARCHIVE,
    TYPE_CHECKSUMS,
    TYPE_PACKAGE,
    TYPE_PROVENANCE,
    TYPE_SIGNATURE,
    Artifact,
    ArtifactManifest,
    BuildRequest,
    ContractError,
    ManifestBuilder,
    TargetSpec,
    VerificationReport,
    digest_file,
)

# The adapter's own name. It is what a target declares and what a manifest
# records, and it is deliberately local: the core reads it, it never defines it.
ADAPTER_NAME = "jvm"

#: The two build systems, plus the one value that means "work it out". Detection
#: is a convenience, not a decision: a repository that ships both wrappers has
#: two different byte-for-byte answers to "what is a release of this project",
#: so `auto` refuses rather than guessing.
BUILD_AUTO = "auto"
BUILD_GRADLE = "gradle"
BUILD_MAVEN = "maven"
SUPPORTED_BUILD_SYSTEMS: Tuple[str, ...] = (BUILD_AUTO, BUILD_GRADLE, BUILD_MAVEN)

#: A project that ships both wrappers without saying which one releases.
BUILD_AMBIGUOUS = "build-system-ambiguous"

# What a target produces. The two jar outputs are the same bytes with different
# intent — a runnable jar and a resolvable one — and keeping them apart is what
# lets a library target carry sources and Javadoc without an application target
# being asked for documentation it does not have.
OUTPUT_APPLICATION_JAR = "application-jar"
OUTPUT_LIBRARY_JAR = "library-jar"
OUTPUT_POM = "pom"
OUTPUT_SOURCES = "sources"
OUTPUT_JAVADOC = "javadoc"
OUTPUT_DISTRIBUTION = "distribution"
SUPPORTED_OUTPUTS: Tuple[str, ...] = (
    OUTPUT_APPLICATION_JAR,
    OUTPUT_LIBRARY_JAR,
    OUTPUT_POM,
    OUTPUT_SOURCES,
    OUTPUT_JAVADOC,
    OUTPUT_DISTRIBUTION,
)

MODE_APPLICATION = "application"
MODE_LIBRARY = "library"
SUPPORTED_MODES: Tuple[str, ...] = (MODE_APPLICATION, MODE_LIBRARY)

#: The two distributions a JVM project can hand a person. Both are archives of
#: the same runnable jar; a project that wants both gets both, and a project that
#: wants neither attaches the jar itself.
DISTRIBUTION_ZIP = "zip"
DISTRIBUTION_TAR = "tar.gz"
SUPPORTED_DISTRIBUTIONS: Tuple[str, ...] = (DISTRIBUTION_ZIP, DISTRIBUTION_TAR)

#: The classifiers a normalized artifact carries. Maven coordinates address an
#: artifact by `groupId:artifactId:version:classifier:extension`, and the
#: publishers read these words rather than parsing file names, so they are
#: constants on both sides of the contract.
CLASSIFIER_MAIN = ""
CLASSIFIER_POM = "pom"
CLASSIFIER_SOURCES = "sources"
CLASSIFIER_JAVADOC = "javadoc"
CLASSIFIER_DIST_ZIP = "dist-zip"
CLASSIFIER_DIST_TAR = "dist-tar"

# The wrappers are the default entry point: a repository that commits `gradlew`
# has pinned its own Gradle, and a system `gradle` would build with whatever
# version the runner happens to carry.
DEFAULT_GRADLEW = "./gradlew"
DEFAULT_MVNW = "./mvnw"

# GPG, resolved through PATH. Named so a test can assert the invocation and so a
# future absolute-pinning change has one place to land.
GPG = "gpg"

#: The Gradle property the release version is bound to. A project reads it
#: (`version = continuumVersion`) and the adapter does not rewrite a build file,
#: because a release that has to patch the build to be releasable is a build
#: that will be patched again, differently, by the next release.
VERSION_PROPERTY = "continuum.version"

#: The environment variable the GPG passphrase is read under. Configuration names
#: a secret (`WIDGETS_SIGNING_PASSPHRASE`); the job exposes it under that name;
#: the adapter reads it under a fixed one. `bind_material` is the only join.
PASSPHRASE_ENV = "CONTINUUM_JVM_GPG_PASSPHRASE"
KEY_ENV = "CONTINUUM_JVM_SIGNING_KEY"

#: The keyring directory a materialized key is imported into, and the mode it is
#: created with. 0700 because a GnuPG home is a private key store, and the same
#: reason a keystore is never committed applies to it exactly.
GPG_HOME_MODE = 0o700
SECRET_FILE_MODE = 0o600

_GROUP_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(\.[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
#: The character set a published version may use. It is deliberately the strict
#: one: Central's validation rejects a version carrying SemVer build metadata
#: (`+sha`), and a version that is valid on a GitHub Packages registry and
#: invalid on Central is a version that fails one release and passes the next.
_MAVEN_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MODULE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
#: A GPG key is named by fingerprint, key id, or email, and is passed straight to
#: `--local-user`, so it is restricted to the characters those forms contain.
_SIGNING_KEY_RE = re.compile(r"^[A-Za-z0-9@._+-]{1,200}$")
_FINGERPRINT_RE = re.compile(r"^[0-9A-F]{8,40}$")


class JvmError(ContractError):
    """The JVM adapter cannot honestly continue.

    Carries a stable code, a message safe to print, a retryable flag, and a
    remediation line: the same shape every classified failure in the release core
    uses, so a caller can branch on the kind of failure rather than on prose.
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


# -- version and coordinates -------------------------------------------------


def maven_version_for(version: str) -> str:
    """The Maven version a Continuum release version maps to.

    A leading `v` is dropped because a tag is frequently `v1.4.0` and the tag is
    not part of the coordinate, and nothing else is touched: `1.4.0-rc.1` is a
    different version from `1.4.0` and stays different. Build metadata is
    refused rather than stripped, because silently dropping `+sha.abcdef` would
    publish a version under a name nobody asked for and the next build of the
    same source would be free to claim it.
    """

    text = (version or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        raise JvmError(
            "version-malformed",
            "a release needs a version before it can have Maven coordinates",
        )
    if "+" in text:
        raise JvmError(
            "version-malformed",
            f"{version!r} carries SemVer build metadata. Build metadata is not a valid "
            "Maven version, and stripping it would publish these bytes under a "
            "different version than the one being released; cut a version without it",
        )
    if not _MAVEN_VERSION_RE.match(text):
        raise JvmError(
            "version-malformed",
            f"{version!r} is not a valid Maven version. Allowed characters are letters, "
            "digits, '.', '_' and '-', starting with a letter or a digit; this is the "
            "character set Maven Central's validation accepts, so a looser one would "
            "produce a version that cannot be published",
        )
    return text


@dataclass(frozen=True)
class MavenCoordinates:
    """Where a release lands in the Maven namespace.

    The group and artifact identifiers are carried through exactly as configured.
    They are not derived from the target id, normalized, or defaulted: a project
    that published `com.example:widgets` before this release has to resolve the
    same coordinates after it, and any rewriting here would fork the project's
    identity in the one namespace it cannot move out of.
    """

    group_id: str
    artifact_id: str
    version: str

    def __post_init__(self) -> None:
        if not _GROUP_RE.match(self.group_id or ""):
            raise JvmError(
                "coordinates-invalid",
                f"group_id {self.group_id!r} is not a Maven group identifier: "
                "dot-separated identifiers of letters, digits, '_', '-' and '.'",
            )
        if not _ARTIFACT_RE.match(self.artifact_id or ""):
            raise JvmError(
                "coordinates-invalid",
                f"artifact_id {self.artifact_id!r} is not a Maven artifact identifier: "
                "letters, digits, '_', '-' and '.', starting with a letter, a digit, "
                "or an underscore",
            )
        if not _MAVEN_VERSION_RE.match(self.version or ""):
            raise JvmError(
                "version-malformed",
                f"{self.version!r} is not a valid Maven version",
            )

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def gav(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.version}"

    @property
    def group_path(self) -> str:
        """The group identifier as the repository path segments it becomes.

        A registry addresses a coordinate by path, so `com.example` is
        `com/example`. Derived rather than configured: a second place to set the
        same string is a second place to get it wrong.
        """

        return self.group_id.replace(".", "/")

    def file(self, *, classifier: str = CLASSIFIER_MAIN, extension: str = "jar") -> str:
        """The file name this coordinate's artifact is published under."""

        suffix = f"-{classifier}" if classifier else ""
        return f"{self.artifact_id}-{self.version}{suffix}.{extension}"

    def path(self, file_name: str) -> str:
        """The repository path of one file of this coordinate."""

        return f"{self.group_path}/{self.artifact_id}/{self.version}/{file_name}"

    def describe(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "version": self.version,
        }


# -- POM reading -------------------------------------------------------------


@dataclass(frozen=True)
class PomMetadata:
    """What a POM says about the artifact it describes.

    Read by the adapter to check the built version, and by the Central publisher
    to check the metadata Central's validation requires. Both are the same read:
    a registry does not judge a POM from the build script that produced it, it
    judges the POM, so a release that has not checked the POM has not checked
    what will be validated.
    """

    group_id: str = ""
    artifact_id: str = ""
    version: str = ""
    name: str = ""
    description: str = ""
    url: str = ""
    licenses: Tuple[str, ...] = ()
    developers: Tuple[str, ...] = ()
    scm_url: str = ""
    scm_connection: str = ""
    packaging: str = ""

    @property
    def complete(self) -> bool:
        return not self.missing_central_requirements()

    def missing_central_requirements(self) -> Tuple[str, ...]:
        """The POM fields Central refuses a deployment without.

        Central's validation is not a style preference: a namespace that has
        never published has no way to answer "who wrote this and under what
        licence" except by reading the POM, and it rejects a deployment whose POM
        cannot answer. Checked here so the failure is a named field before an
        upload rather than a validation error minutes after one.
        """

        missing: List[str] = []
        if not self.name:
            missing.append("name")
        if not self.description:
            missing.append("description")
        if not self.url:
            missing.append("url")
        if not self.licenses:
            missing.append("licenses")
        if not self.developers:
            missing.append("developers")
        if not (self.scm_url or self.scm_connection):
            missing.append("scm")
        if not (self.group_id and self.artifact_id and self.version):
            missing.append("coordinates")
        return tuple(missing)

    def describe(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "licenses": list(self.licenses),
            "developers": list(self.developers),
            "scm_url": self.scm_url,
            "packaging": self.packaging,
        }


def _local(tag: str) -> str:
    """A POM tag without its namespace.

    POMs are namespaced and a project may pick any prefix, so reading tags by
    their bare name is the only form that works across every POM. ElementTree
    offers a `{*}` wildcard, but the implementations that matter differ about
    it, and stripping the namespace here costs one line.
    """

    return tag.split("}", 1)[1] if "}" in tag else tag


def _text_of(element: Optional[ElementTree.Element]) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _child(parent: ElementTree.Element, *path: str) -> Optional[ElementTree.Element]:
    """The element at ``path``, walking direct children only.

    Direct children, and not a search of the whole tree, because POM tags repeat:
    `<name>` is both the project's name and a developer's name, and `<version>`
    is both the project's version and the version of the parent it inherits from.
    A descendant search would answer "the developer's name" to a question about
    the project, and would read a version a `<parent>` block supplies as though
    the project had declared one.
    """

    current: Optional[ElementTree.Element] = parent
    for name in path:
        if current is None:
            return None
        current = next(
            (child for child in current if _local(child.tag) == name),
            None,
        )
    return current


def _find_text(parent: ElementTree.Element, *path: str) -> str:
    """The text at ``path``, or an empty string.

    A missing element and an empty one are the same answer here: a POM with no
    `<description>` and a POM with an empty one are both rejected by Central, and
    reporting them differently would only move the failure later.
    """

    return _text_of(_child(parent, *path))


def _collect_text(parent: ElementTree.Element, path: Tuple[str, ...]) -> Tuple[str, ...]:
    """The text of *every* element at the end of a direct-child path.

    Descending level by level rather than taking the first match at each level,
    because POM metadata is nested twice: it is `<licenses><license><name>`, not
    `<licenses><name>`, and a second `<license>` is a second name rather than an
    alternative path to the first one. Resolving the path to a single element
    and then reading *its* children instead would collect the whitespace between
    the `<license>` tags and report a POM with a licence as having none — which
    is the field a Central validation failure is most often about, and the one
    this reader exists to answer.
    """

    if not path:
        return ()
    level: List[ElementTree.Element] = [parent]
    for depth, name in enumerate(path):
        matched: List[ElementTree.Element] = []
        for element in level:
            matched.extend(
                child for child in element if _local(child.tag) == name
            )
        if not matched:
            return ()
        if depth == len(path) - 1:
            return tuple(value for value in (_text_of(item) for item in matched) if value)
        level = matched
    return ()


def read_pom(path: str) -> PomMetadata:
    """Read a POM's coordinates and the metadata Central requires.

    Fails closed on unreadable or malformed XML rather than returning an empty
    read: a POM this function cannot parse is a POM whose coordinates cannot be
    checked, and a release that skips the check is exactly the release this
    adapter exists to prevent.
    """

    if not os.path.isfile(path):
        raise JvmError(
            "pom-absent",
            f"the POM at {path!r} is not a readable file, so the built version and the "
            "published metadata cannot be checked",
        )
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        raise JvmError(
            "pom-unreadable",
            f"the POM at {path!r} is not well-formed XML ({exc}). A registry would "
            "reject it, and a release must not publish a project descriptor it has "
            "not read",
        ) from None
    root = tree.getroot()
    if _local(root.tag) != "project":
        raise JvmError(
            "pom-unreadable",
            f"the POM at {path!r} has root element {_local(root.tag)!r}, not 'project'",
        )
    return PomMetadata(
        group_id=_find_text(root, "groupId"),
        artifact_id=_find_text(root, "artifactId"),
        version=_find_text(root, "version"),
        name=_find_text(root, "name"),
        description=_find_text(root, "description"),
        url=_find_text(root, "url"),
        licenses=_collect_text(root, ("licenses", "license", "name")),
        developers=_collect_text(root, ("developers", "developer", "name")),
        scm_url=_find_text(root, "scm", "url"),
        scm_connection=_find_text(root, "scm", "connection"),
        packaging=_find_text(root, "packaging"),
    )


# -- configuration -----------------------------------------------------------


def _require_mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JvmError(
            "configuration-invalid",
            f"{where} must be a mapping, got {type(value).__name__}",
        )
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise JvmError(
            "configuration-invalid",
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed)}",
        )


def _choice(value: Any, where: str, choices: Sequence[str], default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise JvmError(
            "configuration-invalid",
            f"{where} must be one of {', '.join(choices)}; got {value!r}",
        )
    return value


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise JvmError("configuration-invalid", f"{where} must be true or false")
    return value


def _text(
    value: Any,
    where: str,
    default: str = "",
    *,
    pattern: Optional[re.Pattern] = None,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise JvmError("configuration-invalid", f"{where} must be a non-empty string")
    text = value.strip()
    if pattern is not None and not pattern.match(text):
        raise JvmError("configuration-invalid", f"{where} has an invalid form; got {value!r}")
    return text


def _secret_name(value: Any, where: str, *, required: bool = True) -> str:
    """A repository secret *name*, never a value.

    An empty name and an absent one mean the same thing: this target names no
    such secret. Treating them differently would let a configuration that blanked
    a name validate as though it had set it, and the job would then run without
    the material it was planned around.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise JvmError(
                "configuration-invalid",
                f"{where} is required: key material is referenced by repository secret "
                "name, never by value",
            )
        return ""
    if not isinstance(value, str) or not _NAME_RE.match(value.strip()):
        raise JvmError(
            "configuration-invalid",
            f"{where} must be an upper-case repository secret name; got {value!r}",
        )
    return value.strip()


def _outputs_for_mode(mode: str) -> Tuple[str, ...]:
    """What a target produces when configuration does not say.

    An application target's job is to produce something runnable and something to
    hand a person, so it asks for the jar alone; a library target's job is to be
    resolvable from a repository, so it asks for the four files every repository
    requires. The default is a statement about the mode rather than a policy the
    configuration has to repeat, and either can be overridden outright.
    """

    if mode == MODE_LIBRARY:
        return (OUTPUT_LIBRARY_JAR, OUTPUT_POM, OUTPUT_SOURCES, OUTPUT_JAVADOC)
    return (OUTPUT_APPLICATION_JAR,)


@dataclass(frozen=True)
class JvmSettings:
    """One target's JVM build, coordinate, and signing configuration.

    The shape is the ten things a wrapper invocation needs and nothing about any
    registry: a target that ships an application attaches a jar to a release and
    never learns that Maven Central exists, and a target that ships a library
    carries no credential, because publication credentials belong to the
    publisher rather than to the thing that was built.
    """

    mode: str = MODE_APPLICATION
    build_system: str = BUILD_AUTO
    outputs: Tuple[str, ...] = ()
    group_id: str = ""
    artifact_id: str = ""
    module: str = ""
    distribution: str = ""
    gradlew: str = DEFAULT_GRADLEW
    mvnw: str = DEFAULT_MVNW
    gradle_tasks: Tuple[str, ...] = ()
    maven_goals: Tuple[str, ...] = ()
    build_arguments: Tuple[str, ...] = ()
    signing_key: str = ""
    signing_identity: str = ""
    signing_key_secret: str = ""
    passphrase_secret: str = ""
    gpg_home: str = ""
    require_signing: bool = False
    require_pom: bool = False
    require_sources: bool = False
    require_javadoc: bool = False
    checksums: bool = True
    sbom_paths: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise JvmError(
                "configuration-invalid",
                f"mode {self.mode!r} is not supported; supported: "
                f"{', '.join(SUPPORTED_MODES)}",
            )
        if self.build_system not in SUPPORTED_BUILD_SYSTEMS:
            raise JvmError(
                "configuration-invalid",
                f"build_system {self.build_system!r} is not supported; supported: "
                f"{', '.join(SUPPORTED_BUILD_SYSTEMS)}",
            )
        if not _GROUP_RE.match(self.group_id or ""):
            raise JvmError(
                "configuration-invalid",
                f"group_id {self.group_id!r} is required and must be a Maven group "
                "identifier. A release maps onto coordinates; it never invents them, "
                "because a project that changed groupId becomes a different artifact.",
            )
        if not _ARTIFACT_RE.match(self.artifact_id or ""):
            raise JvmError(
                "configuration-invalid",
                f"artifact_id {self.artifact_id!r} is required and must be a Maven "
                "artifact identifier",
            )
        if not self.outputs:
            outputs = _outputs_for_mode(self.mode)
        else:
            outputs = tuple(self.outputs)
            for output in outputs:
                if output not in SUPPORTED_OUTPUTS:
                    raise JvmError(
                        "configuration-invalid",
                        f"unknown output {output!r}; supported: "
                        f"{', '.join(SUPPORTED_OUTPUTS)}",
                    )
            if len(set(outputs)) != len(outputs):
                raise JvmError("configuration-invalid", "outputs lists an output twice")
        if OUTPUT_APPLICATION_JAR in outputs and OUTPUT_LIBRARY_JAR in outputs:
            raise JvmError(
                "configuration-invalid",
                "a jar is one artifact with one intent. Asking for it as both an "
                "application jar and a library jar produces two names for the same "
                "bytes, and a destination that addresses files by name would keep one "
                "of them and discard the other",
            )
        if OUTPUT_DISTRIBUTION in outputs and not self.distribution:
            raise JvmError(
                "configuration-invalid",
                "outputs asks for a distribution but no distribution format is named; "
                "set distribution to 'zip' or 'tar.gz'",
            )
        if self.distribution and self.distribution not in SUPPORTED_DISTRIBUTIONS:
            raise JvmError(
                "configuration-invalid",
                f"distribution {self.distribution!r} is not supported; supported: "
                f"{', '.join(SUPPORTED_DISTRIBUTIONS)}",
            )
        if self.signing_key and not _SIGNING_KEY_RE.match(self.signing_key):
            raise JvmError(
                "configuration-invalid",
                f"signing_key {self.signing_key!r} must be a GPG key id, fingerprint, "
                "or email address",
            )
        if self.signing_identity and self.signing_key:
            # Both are passed to `gpg --local-user`; a key named by identity when
            # another key was configured would sign with a key nobody chose.
            raise JvmError(
                "configuration-invalid",
                "signing_key and signing_identity are both arguments to gpg; set one "
                "or the other",
            )
        if self.require_signing and not self.signs:
            raise JvmError(
                "configuration-invalid",
                "require_signing needs a signing_key. A target that requires a "
                "signature and names no key would either sign with whatever the "
                "keyring happened to default to or ship unsigned",
            )

    @property
    def requested(self) -> Tuple[str, ...]:
        """The outputs this target produces, defaulted from the mode."""

        return self.outputs or _outputs_for_mode(self.mode)

    @property
    def wants_jar(self) -> bool:
        return any(
            output in (OUTPUT_APPLICATION_JAR, OUTPUT_LIBRARY_JAR) for output in self.requested
        )

    @property
    def wants_distribution(self) -> bool:
        return OUTPUT_DISTRIBUTION in self.requested

    @property
    def signs(self) -> bool:
        return bool(self.signing_key or self.signing_identity)

    @property
    def wrapper_names(self) -> Tuple[str, ...]:
        return tuple(
            wrapper for wrapper in (self.gradlew, self.mvnw) if wrapper
        )

    def wrapper_for(self, build_system: str) -> str:
        return self.gradlew if build_system == BUILD_GRADLE else self.mvnw

    def coordinates(self, version: str) -> MavenCoordinates:
        return MavenCoordinates(
            group_id=self.group_id,
            artifact_id=self.artifact_id,
            version=maven_version_for(version),
        )

    @property
    def expected_identity(self) -> str:
        """What a signed artifact is expected to carry, for the manifest row.

        A row that claims a signature must name something, and the key that
        actually signed is the least thing that is true. A configured identity
        takes precedence because it is what verification compares against.
        """

        return self.signing_identity or self.signing_key

    def secret_names(self) -> Tuple[str, ...]:
        return tuple(
            name
            for name in (self.signing_key_secret, self.passphrase_secret)
            if name
        )

    def module_directory(self, build_system: str) -> str:
        """Where this module's build output lives, relative to the checkout.

        A Gradle module is named `:core` or `core:cli`; a Maven module is a
        relative path. Both normalize to a directory here so the discovery code
        downstream never has to know which build system is being released.
        """

        text = self.module.strip()
        if not text:
            return "."
        return text.lstrip(":" if build_system == BUILD_GRADLE else "/").replace(
            ":", os.sep
        ).replace("/", os.sep).strip(os.sep) or "."

    def output_directories(self, root: str, build_system: str) -> Tuple[str, ...]:
        """Where this module's build system writes what this target needs.

        Gradle writes libraries to ``build/libs`` and distributions to
        ``build/distributions``; Maven writes both to ``target``. Both are
        returned for Gradle because a distribution is a separate tree, and
        because discovery is the same code either way.
        """

        # normpath, not join: a root module's directory is ".", and a path that
        # reads /repo/./build/libs is a different string from the one a real run
        # records, which is exactly the kind of cosmetic difference that makes a
        # plan stop matching the release it is planning.
        base = os.path.normpath(os.path.join(root, self.module_directory(build_system)))
        if build_system == BUILD_GRADLE:
            return (
                os.path.join(base, "build", "libs"),
                os.path.join(base, "build", "distributions"),
            )
        return (os.path.join(base, "target"),)

    def gradle_tasks_for(self, outputs: Sequence[str]) -> Tuple[str, ...]:
        """The tasks one Gradle invocation runs, in output order.

        Every requested output comes from the *same* invocation so Gradle
        configures the project once and, more importantly, so the jar, the
        sources jar, and the distribution cannot be produced from two different
        checkouts.
        """

        if self.gradle_tasks:
            return self.gradle_tasks
        wanted = set(outputs)
        tasks = ["clean", "assemble"]
        if OUTPUT_SOURCES in wanted:
            tasks.append("sourcesJar")
        if OUTPUT_JAVADOC in wanted:
            tasks.append("javadocJar")
        if OUTPUT_DISTRIBUTION in wanted:
            if self.distribution == DISTRIBUTION_ZIP:
                tasks.append("distZip")
            elif self.distribution == DISTRIBUTION_TAR:
                tasks.append("distTar")
        # A module named `core` and a module named `:core` are the same module, so
        # the task prefix is built from the normalized form rather than from
        # whichever spelling the configuration happened to use. Getting this
        # wrong is silent: Gradle answers `:core:assemble` with "task not found"
        # and root `assemble` with a build of the whole project, and the second
        # looks like success while building somebody else's jar.
        prefix = ""
        if self.module:
            prefix = self.module if self.module.startswith(":") else f":{self.module}"
        if not prefix:
            return tuple(tasks)
        return tuple(f"{prefix}:{task}" for task in tasks)

    def maven_goals_for(self, outputs: Sequence[str]) -> Tuple[str, ...]:
        """The goals one Maven invocation runs, in output order.

        ``clean`` is not decoration. This adapter discovers what was built by
        looking in the output directory, and a stale jar left there by an earlier
        build is indistinguishable from this build's output — so the release
        removes the directory's contents first rather than publishing a file
        nobody compiled.
        """

        if self.maven_goals:
            return self.maven_goals
        wanted = set(outputs)
        goals = ["clean", "package"]
        if OUTPUT_SOURCES in wanted:
            goals.append("source:jar")
        if OUTPUT_JAVADOC in wanted:
            goals.append("javadoc:jar")
        return tuple(goals)

    def build_command(
        self, build_system: str, coordinates: MavenCoordinates
    ) -> Tuple[str, ...]:
        """The argument vector that produces every requested output.

        Gradle is told the release version, because it can be told. Maven is not,
        because a Maven version comes from the POM and inventing a way to
        override it would let a release publish coordinates its own build file
        disagrees with; the adapter reads the version back out of the built POM
        instead, which is the same check with a stronger guarantee.
        """

        outputs = self.requested
        if build_system == BUILD_GRADLE:
            argv: List[str] = [
                self.gradlew,
                *self.gradle_tasks_for(outputs),
                *self.build_arguments,
                f"-P{VERSION_PROPERTY}={coordinates.version}",
            ]
            return tuple(argv)
        argv = [self.mvnw, "--batch-mode", *self.maven_goals_for(outputs), *self.build_arguments]
        if self.module and not self.module.startswith(":"):
            argv.extend(["--file", os.path.join(self.module_directory(build_system), "pom.xml")])
        return tuple(argv)

    def describe(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "build_system": self.build_system,
            "outputs": list(self.requested),
            "group_id": self.group_id,
            "artifact_id": self.artifact_id,
            "module": self.module,
            "distribution": self.distribution,
            "gradlew": self.gradlew,
            "mvnw": self.mvnw,
            "gradle_tasks": list(self.gradle_tasks),
            "maven_goals": list(self.maven_goals),
            "signing_key": self.signing_key,
            "signing_identity": self.signing_identity,
            "signing_key_secret": self.signing_key_secret,
            "passphrase_secret": self.passphrase_secret,
            "require_signing": self.require_signing,
            "require_pom": self.require_pom,
            "require_sources": self.require_sources,
            "require_javadoc": self.require_javadoc,
            "checksums": self.checksums,
        }


_ALLOWED_SETTING_KEYS = (
    "mode",
    "build_system",
    "outputs",
    "group_id",
    "artifact_id",
    "module",
    "distribution",
    "gradlew",
    "mvnw",
    "gradle_tasks",
    "maven_goals",
    "build_arguments",
    "signing_key",
    "signing_identity",
    "signing_key_secret",
    "passphrase_secret",
    "gpg_home",
    "require_signing",
    "require_pom",
    "require_sources",
    "require_javadoc",
    "checksums",
    "sbom_paths",
)


def _string_list(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise JvmError("configuration-invalid", f"{where} must be a string or a list of strings")
    return tuple(item for item in value if item)


def parse_settings(value: Any, where: str = "jvm") -> JvmSettings:
    """Validate a target's adapter options.

    Kept in this module rather than in `continuum.config`, because the release
    configuration schema is a consumer-facing contract and platform options are
    the adapter's own business. A target carries them as opaque
    `TargetSpec.options`, and this is the only code that reads them.
    """

    mapping = _require_mapping(value, where)
    _reject_unknown(mapping, _ALLOWED_SETTING_KEYS, where)
    mode = _choice(mapping.get("mode"), f"{where}.mode", SUPPORTED_MODES, MODE_APPLICATION)
    outputs_value = mapping.get("outputs")
    if outputs_value is None:
        outputs: Tuple[str, ...] = ()
    elif not isinstance(outputs_value, list) or not outputs_value:
        raise JvmError(
            "configuration-invalid",
            f"{where}.outputs must be a non-empty list of "
            f"{', '.join(SUPPORTED_OUTPUTS)}",
        )
    else:
        outputs = tuple(outputs_value)
    return JvmSettings(
        mode=mode,
        build_system=_choice(
            mapping.get("build_system"), f"{where}.build_system", SUPPORTED_BUILD_SYSTEMS, BUILD_AUTO
        ),
        outputs=outputs,
        group_id=_text(mapping.get("group_id"), f"{where}.group_id", "", pattern=_GROUP_RE),
        artifact_id=_text(
            mapping.get("artifact_id"), f"{where}.artifact_id", "", pattern=_ARTIFACT_RE
        ),
        module=_text(mapping.get("module"), f"{where}.module", "", pattern=_MODULE_RE),
        distribution=_text(mapping.get("distribution"), f"{where}.distribution", ""),
        gradlew=_text(mapping.get("gradlew"), f"{where}.gradlew", DEFAULT_GRADLEW),
        mvnw=_text(mapping.get("mvnw"), f"{where}.mvnw", DEFAULT_MVNW),
        gradle_tasks=_string_list(mapping.get("gradle_tasks"), f"{where}.gradle_tasks"),
        maven_goals=_string_list(mapping.get("maven_goals"), f"{where}.maven_goals"),
        build_arguments=_string_list(mapping.get("build_arguments"), f"{where}.build_arguments"),
        signing_key=_text(
            mapping.get("signing_key"), f"{where}.signing_key", "", pattern=_SIGNING_KEY_RE
        ),
        signing_identity=_text(
            mapping.get("signing_identity"), f"{where}.signing_identity", "", pattern=_SIGNING_KEY_RE
        ),
        signing_key_secret=_secret_name(
            mapping.get("signing_key_secret"), f"{where}.signing_key_secret", required=False
        ),
        passphrase_secret=_secret_name(
            mapping.get("passphrase_secret"), f"{where}.passphrase_secret", required=False
        ),
        gpg_home=_text(mapping.get("gpg_home"), f"{where}.gpg_home", ""),
        require_signing=_bool(mapping.get("require_signing"), f"{where}.require_signing", False),
        require_pom=_bool(mapping.get("require_pom"), f"{where}.require_pom", False),
        require_sources=_bool(mapping.get("require_sources"), f"{where}.require_sources", False),
        require_javadoc=_bool(mapping.get("require_javadoc"), f"{where}.require_javadoc", False),
        checksums=_bool(mapping.get("checksums"), f"{where}.checksums", True),
        sbom_paths=_string_list(mapping.get("sbom_paths"), f"{where}.sbom_paths"),
    )


def settings_from(target: TargetSpec) -> JvmSettings:
    """Read a target's options as a `JvmSettings`.

    `TargetSpec.options` is a tuple of pairs so it stays hashable; a mapping
    arrives as a nested mapping, which the parser accepts directly.
    """

    if target.adapter != ADAPTER_NAME:
        raise JvmError(
            "wrong-adapter",
            f"target {target.id!r} names adapter {target.adapter!r}, not {ADAPTER_NAME!r}",
        )
    options = {key: value for key, value in target.options}
    return parse_settings(options, f"target {target.id!r}")


# -- signing material -------------------------------------------------------


def signing_material_present(
    settings: JvmSettings, environment: Mapping[str, str]
) -> bool:
    """Whether everything a signed build needs is actually here.

    An empty value counts as absent. GitHub hands a fork the repository
    variables with nothing in them rather than withholding them, so a check for
    "is the name set" would pass on a fork and sign with nothing.
    """

    if not settings.signs:
        return True
    if not settings.signing_key and not settings.signing_identity:
        return False
    names = settings.secret_names()
    if not names:
        # No secret named: the job is expected to have provisioned the keyring
        # itself, which is a fact about the job rather than about configuration.
        return True
    return all(bool((environment.get(name) or "").strip()) for name in names)


def bind_material(
    settings: JvmSettings, environment: Mapping[str, str]
) -> Dict[str, str]:
    """The environment a run needs, with the configured secrets under fixed names."""

    bound = dict(environment)
    if settings.passphrase_secret:
        passphrase = (bound.get(settings.passphrase_secret) or "").strip()
        if passphrase:
            bound[PASSPHRASE_ENV] = passphrase
    if settings.signing_key_secret:
        key = (bound.get(settings.signing_key_secret) or "").strip()
        if key:
            bound[KEY_ENV] = key
    return bound


# -- failure classification -------------------------------------------------


_FAILURE_MARKERS: Tuple[Tuple[str, str, str], ...] = (
    (
        "signing-key-missing",
        "the configured GPG key is not in the keyring this job can use",
        "signing_key must name a key that exists in the keyring, and a job that "
        "materializes one from a secret needs signing_key_secret to point at it",
    ),
    (
        "signing-key-ambiguous",
        "the keyring holds more than one key and the name given does not select one",
        "configure a full fingerprint in signing_key rather than a short key id",
    ),
    (
        "signing-passphrase-invalid",
        "the key could not be unlocked with the stored passphrase",
        "check passphrase_secret; a wrong passphrase and the wrong key produce the "
        "same tool error",
    ),
    (
        "signing-material-missing",
        "the signing key or its passphrase is not available in this job",
        "expose the repository secrets named by signing_key_secret and "
        "passphrase_secret; a fork never receives them",
    ),
    (
        "gpg-tool-missing",
        "gpg is not available on this runner",
        "the release job must provide GnuPG, and the key it is asked to sign with",
    ),
    (
        "build-failed",
        "the JVM build failed",
        "the release builds the exact approved commit; a failure here is a code or "
        "environment problem, not a release problem",
    ),
    (
        "wrapper-missing",
        "the build wrapper this target names is not in the checkout",
        "a repository that releases with a wrapper commits it; commit the one the "
        "release builds with rather than falling back to a system tool",
    ),
)

#: Phrases that identify each failure in GPG's and the wrappers' own words. A
#: phrase matches when every word of it appears, so "gpg: signing failed" and
#: "gpg: cannot sign" are two ways of saying the same thing. Checked in order,
#: most specific first: "key" on its own is not a diagnosis, because "no secret
#: key" and "secret key not available" both contain it and mean different things.
_FAILURE_HINTS: Tuple[Tuple[str, Tuple[Tuple[str, ...], ...]], ...] = (
    (
        "signing-passphrase-invalid",
        (("bad passphrase",), ("incorrect passphrase",), ("no passphrase",)),
    ),
    ("signing-key-missing", (("no secret key",), ("secret key not available",), ("public key not found",))),
    ("signing-key-ambiguous", (("ambiguous name",), ("more than one key",))),
    ("gpg-tool-missing", (("command not found",), ("no such file or directory",))),
    ("wrapper-missing", (("no such file or directory",), ("permission denied",), ("is not executable",))),
)


def classify_failure(step: str, detail: str) -> Tuple[str, str, str]:
    """Explain a failed step by the invariant it was protecting."""

    lowered = (detail or "").lower()
    if "build" in step.lower() or "gradle" in step.lower() or "maven" in step.lower():
        for code, message, remediation in _FAILURE_MARKERS:
            if code == "build-failed":
                return code, message, remediation
    for code, phrases in _FAILURE_HINTS:
        if not any(all(word in lowered for word in phrase) for phrase in phrases):
            continue
        for candidate, message, remediation in _FAILURE_MARKERS:
            if candidate == code:
                return candidate, message, remediation
    return (
        "jvm-step-failed",
        f"step {step!r} failed",
        "inspect the build output; the adapter could not classify the failure",
    )


# -- command boundary --------------------------------------------------------


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


# -- output discovery --------------------------------------------------------


@dataclass(frozen=True)
class StagedArtifact:
    """One build output, named the way its coordinates name it.

    The point of the type is that it carries no build system. Gradle and Maven
    write different files at different paths, and this is where that difference
    stops: everything downstream — the manifest, the publishers, the checksum
    listing — sees a name, a classifier, and a path.
    """

    name: str
    path: str
    type: str
    classifier: str = CLASSIFIER_MAIN
    media_type: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "classifier": self.classifier,
        }


#: The media types a consumer's tooling expects, so a registry or a browser is
#: not left guessing what a file is.
MEDIA_TYPE_JAR = "application/java-archive"
MEDIA_TYPE_POM = "application/xml"
MEDIA_TYPE_ZIP = "application/zip"
MEDIA_TYPE_TAR = "application/gzip"
MEDIA_TYPE_SIGNATURE = "application/pgp-signature"

#: The internal discovery key both jar outputs share, so an application jar and
#: a library jar are the same file found by either name.
KEY_JAR = "jar"

#: The distribution format a target asked for, and the artifact each one records.
_DISTRIBUTIONS: Dict[str, Tuple[str, str, str]] = {
    DISTRIBUTION_ZIP: (CLASSIFIER_DIST_ZIP, MEDIA_TYPE_ZIP, "zip"),
    DISTRIBUTION_TAR: (CLASSIFIER_DIST_TAR, MEDIA_TYPE_TAR, "tar.gz"),
}


def classify_output(
    file_name: str, artifact_id: str, version: str
) -> Optional[str]:
    """Which output a built file is, or None if it is not one of this target's.

    Classification is by exact coordinate name rather than by a search for "some
    jar", because a module's output directory also holds by-products — a shaded
    jar, a test report, a previous version's artifact — and a release that
    published one of those would publish a file whose relationship to the
    approved commit is unknown. An unrecognised file is ignored rather than
    refused, because refusing would make an unrelated build by-product a reason
    the release cannot happen; a *missing* expected output is the failure that
    matters, and it is reported by name.

    The application and library jar return one key, `KEY_JAR`, because they are
    one file with two intents: `discover()` is asked for an output a target
    requested, and a library target that asked for a library jar must find the
    same file an application target does.
    """

    main = f"{artifact_id}-{version}"
    if file_name == f"{main}.jar":
        return KEY_JAR
    if file_name == f"{main}.pom":
        return OUTPUT_POM
    if file_name == f"{main}-sources.jar":
        return OUTPUT_SOURCES
    if file_name == f"{main}-javadoc.jar":
        return OUTPUT_JAVADOC
    if file_name.endswith(".zip") and main in file_name:
        return OUTPUT_DISTRIBUTION
    if (file_name.endswith(".tar.gz") or file_name.endswith(".tgz")) and main in file_name:
        return OUTPUT_DISTRIBUTION
    return None


#: The discovery key a requested output is looked up under. Identity for
#: everything except the jar, which both jar outputs share.
_DISCOVERY_KEYS: Dict[str, str] = {
    OUTPUT_APPLICATION_JAR: KEY_JAR,
    OUTPUT_LIBRARY_JAR: KEY_JAR,
    OUTPUT_POM: OUTPUT_POM,
    OUTPUT_SOURCES: OUTPUT_SOURCES,
    OUTPUT_JAVADOC: OUTPUT_JAVADOC,
    OUTPUT_DISTRIBUTION: OUTPUT_DISTRIBUTION,
}


def discover(
    root: str,
    settings: JvmSettings,
    coordinates: MavenCoordinates,
    build_system: str,
) -> Tuple[StagedArtifact, ...]:
    """Find the single built file for every requested output.

    A match that is not unique is a failure rather than a choice: two jars for
    one coordinate means the release cannot say which one it shipped, and
    picking the first would make the published bytes depend on directory order.
    """

    found: Dict[str, List[str]] = {}
    for directory in settings.output_directories(root, build_system):
        for path in sorted(glob.glob(os.path.join(directory, "*"))):
            if not os.path.isfile(path):
                continue
            key = classify_output(
                os.path.basename(path), coordinates.artifact_id, coordinates.version
            )
            if key is None:
                continue
            found.setdefault(key, []).append(path)

    staged: List[StagedArtifact] = []
    for output in settings.requested:
        candidates = found.get(_DISCOVERY_KEYS.get(output, output), [])
        if not candidates:
            raise JvmError(
                "output-not-found",
                f"the {build_system} build reported success but produced no {output} for "
                f"{coordinates.gav} under "
                + ", ".join(repr(d) for d in settings.output_directories(root, build_system))
                + ". A release publishes what was built, so a missing output is "
                "refused rather than skipped",
            )
        if len(candidates) > 1:
            raise JvmError(
                "ambiguous-output",
                f"{len(candidates)} files match {output} for {coordinates.gav}: "
                + ", ".join(os.path.basename(path) for path in candidates)
                + ". Publishing one of them would make the release depend on the "
                "order the build directory happened to be walked in",
            )
        staged.append(_staged(output, candidates[0], settings, coordinates))
    return tuple(staged)


def _staged(
    output: str, path: str, settings: JvmSettings, coordinates: MavenCoordinates
) -> StagedArtifact:
    """Name a built file the way its coordinates name it.

    A Gradle distribution is `widgets-1.4.0-all.zip` and a Maven one is
    `widgets-1.4.0.zip`; both are the same release artifact, so both are
    recorded as `widgets-1.4.0.zip`. A destination addresses assets by name, and
    a release that named its distribution differently depending on which
    toolchain built it would make the artifact's name a fact about the build
    rather than about the release.
    """

    if output in (OUTPUT_APPLICATION_JAR, OUTPUT_LIBRARY_JAR):
        return StagedArtifact(
            name=coordinates.file(),
            path=path,
            type=TYPE_PACKAGE,
            classifier=CLASSIFIER_MAIN,
            media_type=MEDIA_TYPE_JAR,
        )
    if output == OUTPUT_POM:
        return StagedArtifact(
            name=coordinates.file(extension="pom"),
            path=path,
            type=TYPE_PACKAGE,
            classifier=CLASSIFIER_POM,
            media_type=MEDIA_TYPE_POM,
        )
    if output == OUTPUT_SOURCES:
        return StagedArtifact(
            name=coordinates.file(classifier=CLASSIFIER_SOURCES),
            path=path,
            type=TYPE_PACKAGE,
            classifier=CLASSIFIER_SOURCES,
            media_type=MEDIA_TYPE_JAR,
        )
    if output == OUTPUT_JAVADOC:
        return StagedArtifact(
            name=coordinates.file(classifier=CLASSIFIER_JAVADOC),
            path=path,
            type=TYPE_PACKAGE,
            classifier=CLASSIFIER_JAVADOC,
            media_type=MEDIA_TYPE_JAR,
        )
    classifier, media_type, extension = _DISTRIBUTIONS[settings.distribution]
    return StagedArtifact(
        name=f"{coordinates.artifact_id}-{coordinates.version}.{extension}",
        path=path,
        type=TYPE_ARCHIVE,
        classifier=classifier,
        media_type=media_type,
    )


def planned_paths(
    root: str,
    settings: JvmSettings,
    coordinates: MavenCoordinates,
    build_system: str,
) -> Tuple[StagedArtifact, ...]:
    """The same artifacts a real run would record, at the paths it would expect.

    A dry run declares exactly what a real run records, differing only in that
    the paths are the expected ones rather than observed ones. A plan that named
    something else from the artifact a real build would produce is not a plan for
    this repository, it is a description of a different one.
    """

    staged: List[StagedArtifact] = []
    for output in settings.requested:
        if output == OUTPUT_DISTRIBUTION:
            classifier, media_type, extension = _DISTRIBUTIONS[settings.distribution]
            name = f"{coordinates.artifact_id}-{coordinates.version}.{extension}"
            staged.append(
                StagedArtifact(
                    name=name,
                    path=os.path.normpath(
                        os.path.join(
                            root,
                            settings.module_directory(build_system),
                            _distributions_dir(build_system),
                            name,
                        )
                    ),
                    type=TYPE_ARCHIVE,
                    classifier=classifier,
                    media_type=media_type,
                )
            )
            continue
        artifact = _staged(output, "", settings, coordinates)
        staged.append(
            StagedArtifact(
                name=artifact.name,
                path=os.path.normpath(
                    os.path.join(
                        root,
                        settings.module_directory(build_system),
                        _libraries_dir(build_system),
                        artifact.name,
                    )
                ),
                type=artifact.type,
                classifier=artifact.classifier,
                media_type=artifact.media_type,
            )
        )
    return tuple(staged)


def _libraries_dir(build_system: str) -> str:
    """Where a module's libraries land, relative to the module directory."""

    return os.path.join("build", "libs") if build_system == BUILD_GRADLE else "target"


def _distributions_dir(build_system: str) -> str:
    """Where a module's distributions land, relative to the module directory."""

    return os.path.join("build", "distributions") if build_system == BUILD_GRADLE else "target"


# -- the adapter -------------------------------------------------------------


class JvmAdapter:
    """Builds, signs, and verifies one JVM target.

    The command runner, the keyring, and the checkout's revision are all
    injected, so the whole adapter can be exercised on a machine with no JDK, no
    Gradle, no Maven, and no GPG: a test supplies a runner that writes the files
    the build system would have written, and the adapter's decisions are still
    the decisions a real release would make.
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
        self.scratch_dir = scratch_dir or tempfile.mkdtemp(prefix="continuum-jvm-")
        self._git_revision = git_revision

    # -- availability -------------------------------------------------------
    def available(self, request: Optional[BuildRequest] = None) -> bool:
        """Whether a build entry point this adapter would use exists here.

        Only the wrapper is checked: the wrapper downloads and invokes the JDK,
        the compiler, and GPG itself, so a runner that can run the wrapper is
        the runner this profile is written for. A dry run needs the wrapper too,
        because a plan that names a build entry point which is not in the
        checkout is not a plan for this repository.

        With a request, the answer is about the wrapper that request would build
        with; without one, it is whether *any* wrapper is here, which is what
        a caller wiring this adapter into a release asks before it has a target.
        """

        root = (request.workdir or self.workdir) if request is not None else self.workdir
        if request is None:
            return any(
                os.path.isfile(os.path.join(root or ".", wrapper))
                for wrapper in (DEFAULT_GRADLEW, DEFAULT_MVNW)
            )
        try:
            settings = settings_from(request.target)
            build_system = self._build_system(settings, root)
        except JvmError:
            return False
        return os.path.isfile(os.path.join(root or ".", settings.wrapper_for(build_system)))

    def intent(self) -> str:
        return (
            "build every requested output from the approved source SHA in one "
            "Gradle or Maven invocation, sign the publishable artifacts with the "
            "configured GPG key in a temporary keyring, verify each artifact and "
            "signature, and remove the key material"
        )

    # -- helpers ------------------------------------------------------------
    def _settings(self, request: BuildRequest) -> JvmSettings:
        return settings_from(request.target)

    def _build_system(self, settings: JvmSettings, root: str) -> str:
        """Which build system releases this target, and refuse to guess.

        A repository that ships both wrappers has two different answers to "what
        is a release of this project", and they differ in bytes rather than in
        style. `auto` therefore refuses rather than preferring one: the two
        wrappers exist side by side for years in plenty of repositories, and
        picking the wrong one would produce a release that is internally
        consistent and not the one anybody reviewed.
        """

        if settings.build_system != BUILD_AUTO:
            return settings.build_system
        present = [
            system
            for system in (BUILD_GRADLE, BUILD_MAVEN)
            if os.path.isfile(os.path.join(root or ".", settings.wrapper_for(system)))
        ]
        if len(present) == 1:
            return present[0]
        if not present:
            raise JvmError(
                "wrapper-missing",
                f"neither {settings.gradlew!r} nor {settings.mvnw!r} is in the checkout, "
                "so there is no build entry point to release with",
                remediation=(
                    "commit the wrapper the release builds with, or name one with "
                    "build_system; a release never falls back to a system tool, because "
                    "that builds with whatever version the runner happens to carry"
                ),
            )
        raise JvmError(
            BUILD_AMBIGUOUS,
            f"this checkout has both a {settings.gradlew} and a {settings.mvnw}, and they "
            "do not produce the same bytes, so the release has to say which one builds it",
            remediation=(
                "set build_system to 'gradle' or 'maven'. Detection exists for a "
                "repository that ships one wrapper; a repository that ships both has "
                "made a decision that only it can make"
            ),
        )

    def _run(
        self,
        argv: Sequence[str],
        step: str,
        *,
        workdir: str,
        environment: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        return self.command_runner(argv, workdir, environment or self.environment)

    def _run_build(
        self,
        request: BuildRequest,
        settings: JvmSettings,
        coordinates: MavenCoordinates,
        build_system: str,
    ) -> None:
        root = request.workdir or self.workdir
        wrapper = settings.wrapper_for(build_system)
        if not os.path.isfile(os.path.join(root or ".", wrapper)):
            raise JvmError(
                "wrapper-missing",
                f"the {build_system} wrapper {wrapper!r} is not in the checkout, so there "
                "is nothing to build with",
                remediation=(
                    "commit the wrapper, or point gradlew/mvnw at it; a release never "
                    "falls back to a system build tool"
                ),
            )
        result = self._run(
            settings.build_command(build_system, coordinates), f"{build_system}-build", workdir=root
        )
        if result.code != 0:
            code, message, remediation = classify_failure("build", result.output)
            raise JvmError(
                code,
                f"{message}: {result.output.strip()[:400]}",
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
        found = self._git_revision(request.workdir or self.workdir)
        if found and found != request.source_sha:
            raise JvmError(
                "source-mismatch",
                f"the checkout is at {found} but this release is approved for "
                f"{request.source_sha}; building would ship unreviewed bytes",
            )

    def _assert_built_version(
        self, coordinates: MavenCoordinates, staged: Sequence[StagedArtifact]
    ) -> None:
        """Check that what was built carries the version being released.

        Gradle was told the version and Maven was not, so this reads the
        coordinates back out of what exists rather than trusting either. It is
        the check that catches the most expensive mistake available to a JVM
        release: cutting tag `v1.4.0` from a branch whose POM still says `1.3.0`
        and publishing `1.3.0`'s bytes under `1.4.0`'s coordinates.
        """

        pom = next((item for item in staged if item.classifier == CLASSIFIER_POM), None)
        if pom is None:
            return
        metadata = read_pom(pom.path)
        if metadata.version and metadata.version != coordinates.version:
            raise JvmError(
                "version-mismatch",
                f"the built POM declares version {metadata.version!r} but this release is "
                f"{coordinates.version!r}. A Maven build takes its version from the POM, "
                "so this release would publish one version's bytes under another "
                "version's coordinates, and no registry would notice",
                remediation=(
                    "bump the version in the POM (or the Gradle property this release "
                    "binds) and cut the tag from that commit"
                ),
            )
        if metadata.artifact_id and metadata.artifact_id != coordinates.artifact_id:
            raise JvmError(
                "coordinates-mismatch",
                f"the built POM declares artifactId {metadata.artifact_id!r} but this "
                f"release is {coordinates.artifact_id!r}. The adapter does not rewrite "
                "coordinates, so a mismatch is a configuration error rather than "
                "something to normalize away",
            )

    # -- build --------------------------------------------------------------
    def build(self, request: BuildRequest) -> ArtifactManifest:
        settings = self._settings(request)
        coordinates = settings.coordinates(request.version)
        root = request.workdir or self.workdir
        build_system = self._build_system(settings, root)
        builder = ManifestBuilder(
            target=request.target.id,
            adapter=self.name,
            source_sha=request.source_sha,
            version=request.version,
        )

        if request.dry_run:
            for planned in planned_paths(root, settings, coordinates, build_system):
                builder.declare(
                    name=planned.name,
                    path=planned.path,
                    type=planned.type,
                    platform="jvm",
                    classifier=planned.classifier,
                    media_type=planned.media_type,
                )
            for sbom in self._sbom_files(root, settings):
                builder.declare(
                    name=os.path.basename(sbom),
                    path=sbom,
                    type=TYPE_PROVENANCE,
                    platform="jvm",
                    media_type="application/json",
                )
            if settings.checksums:
                builder.declare(
                    name=CHECKSUMS_FILE,
                    path=self._checksums_path(),
                    type=TYPE_CHECKSUMS,
                    platform="jvm",
                    media_type="text/plain",
                )
            return builder.build()

        self._assert_source(request)
        self._run_build(request, settings, coordinates, build_system)

        staged = discover(root, settings, coordinates, build_system)
        self._assert_built_version(coordinates, staged)
        return self._record(builder, settings, staged, root)

    def _sbom_files(self, root: str, settings: JvmSettings) -> Tuple[str, ...]:
        """The SBOMs the build produced, if the target asked for any.

        Recorded as `provenance` rather than as an installable artifact: an SBOM
        describes what is inside the release, it is not part of it, and a
        consumer that installs every published asset should not be handed one.
        """

        found: List[str] = []
        for pattern in settings.sbom_paths:
            for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                if os.path.isfile(path):
                    found.append(path)
        return tuple(found)

    def _checksums_path(self) -> str:
        """Where this run's checksum file is written.

        In the adapter's own scratch directory rather than in the checkout: a
        release that left `SHA256SUMS.txt` in the working tree would dirty it, and
        a second release of the same version would then be holding a file it did
        not write.
        """

        os.makedirs(self.scratch_dir, exist_ok=True)
        return os.path.join(self.scratch_dir, CHECKSUMS_FILE)

    def _record(
        self,
        builder: ManifestBuilder,
        settings: JvmSettings,
        staged: Sequence[StagedArtifact],
        root: str,
    ) -> ArtifactManifest:
        for item in staged:
            builder.record(
                name=item.name,
                path=item.path,
                type=item.type,
                platform="jvm",
                classifier=item.classifier,
                media_type=item.media_type,
            )
        for sbom in self._sbom_files(root, settings):
            builder.record(
                name=os.path.basename(sbom),
                path=sbom,
                type=TYPE_PROVENANCE,
                platform="jvm",
                media_type="application/json",
                provenance=PROVENANCE_DECLARED,
            )
        if settings.checksums:
            # Written from the manifest rather than from the build directory, so
            # it lists what this release built and nothing the build happened to
            # leave lying around. It is recorded afterwards, and cannot list
            # itself: a checksum file whose own digest depends on its contents is
            # a file no consumer could ever verify.
            path = builder.build().write_checksums(self.scratch_dir)
            builder.record(
                name=CHECKSUMS_FILE,
                path=path,
                type=TYPE_CHECKSUMS,
                platform="jvm",
                media_type="text/plain",
            )
        return builder.build()

    # -- signing ------------------------------------------------------------
    @property
    def _signable_types(self) -> Tuple[str, ...]:
        return (TYPE_PACKAGE,)

    def sign(self, request: BuildRequest, manifest: ArtifactManifest) -> ArtifactManifest:
        settings = self._settings(request)
        if request.dry_run or not settings.signs:
            return manifest
        if not signing_material_present(settings, self.environment):
            raise JvmError(
                "signing-material-missing",
                f"target {request.target.id!r} signs its artifacts, but "
                + (
                    " and ".join(settings.secret_names())
                    or "the configured key"
                )
                + " are not available in this job",
                remediation=(
                    "expose the named repository secrets. A target that signs fails "
                    "closed here rather than publishing unsigned artifacts, because a "
                    "registry that requires signatures rejects the whole deployment "
                    "and a registry that does not leaves a consumer unable to check "
                    "where the bytes came from"
                ),
            )
        key = self._key_argument(settings)
        home = ""
        key_file: Optional[str] = None
        passphrase_file: Optional[str] = None
        try:
            home = self._gpg_home(settings)
            key_file = self._materialize_key(settings)
            passphrase_file = self._write_passphrase(settings)
            if key_file is not None:
                self._import_key(home, key_file, passphrase_file)
            self._assert_key_present(home, key, request.workdir or self.workdir)
            signatures: Dict[str, str] = {}
            for artifact in manifest.artifacts:
                if artifact.type not in self._signable_types or artifact.is_evidence:
                    continue
                signatures[artifact.name] = self._sign_artifact(
                    home, key, artifact, passphrase_file, request.workdir or self.workdir
                )
            return self._signed_manifest(manifest, settings, signatures)
        finally:
            # The keyring holds the imported private key, the passphrase file
            # holds the passphrase, and both are removed whatever happens next.
            # The `.asc` files stay: they are the signatures the release just
            # recorded and the registry is about to be asked to check.
            self._cleanup(home, key_file, passphrase_file)

    def _key_argument(self, settings: JvmSettings) -> str:
        return settings.signing_key or settings.signing_identity

    def _gpg_home(self, settings: JvmSettings) -> str:
        """Where the signing key lives for this job.

        A configured keyring is the job's own and is left alone; otherwise a
        scratch directory is created for the key this job is about to import, so
        that the private key exists for one signing operation rather than for
        the life of the runner.
        """

        if settings.gpg_home:
            return settings.gpg_home
        home = os.path.join(self.scratch_dir, "gnupg")
        os.makedirs(home, exist_ok=True)
        os.chmod(home, GPG_HOME_MODE)
        return home

    def _materialize_key(self, settings: JvmSettings) -> Optional[str]:
        """Write the configured private key to a file, or use the job's keyring.

        The key arrives as an armored block in a repository secret. It is written
        to a mode-0600 file, imported, and removed in the same `finally` that
        removes the keyring — a private key on a runner is a credential, not a
        temporary convenience.
        """

        if not settings.signing_key_secret:
            return None
        raw = (self.environment.get(KEY_ENV) or "").strip() or (
            self.environment.get(settings.signing_key_secret) or ""
        ).strip()
        if not raw:
            raise JvmError(
                "signing-material-missing",
                f"the secret {settings.signing_key_secret!r} is empty. A signing key is "
                "carried as an armored private key block in a repository secret, and a "
                "fork receives that secret with nothing in it, so a check for the name "
                "alone would pass and sign with nothing",
            )
        os.makedirs(self.scratch_dir, exist_ok=True)
        path = os.path.join(self.scratch_dir, "continuum-signing-key.asc")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw if "BEGIN PGP PRIVATE KEY" in raw else raw + "\n")
        os.chmod(path, SECRET_FILE_MODE)
        return path

    def _write_passphrase(self, settings: JvmSettings) -> Optional[str]:
        """Put the passphrase in a file, never in an argument vector.

        `gpg --passphrase` in argv is visible to every process on the runner and
        to a crash report, which is exactly what a mode-0600 file avoids.
        """

        if not settings.passphrase_secret:
            return None
        value = (self.environment.get(PASSPHRASE_ENV) or "").strip() or (
            self.environment.get(settings.passphrase_secret) or ""
        ).strip()
        if not value:
            raise JvmError(
                "signing-material-missing",
                f"the secret {settings.passphrase_secret!r} is empty, so the key cannot "
                "be unlocked",
            )
        os.makedirs(self.scratch_dir, exist_ok=True)
        path = os.path.join(self.scratch_dir, "continuum-passphrase")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.chmod(path, SECRET_FILE_MODE)
        return path

    def _gpg_environment(self, home: str) -> Dict[str, str]:
        environment = dict(self.environment)
        environment["GNUPGHOME"] = home
        return environment

    def _import_key(
        self, home: str, key_file: str, passphrase_file: Optional[str]
    ) -> None:
        result = self._run(
            [GPG, "--batch", "--import", key_file],
            "gpg-import",
            workdir=self.workdir,
            environment=self._gpg_environment(home),
        )
        if result.code != 0:
            code, message, remediation = classify_failure("gpg-import", result.output)
            raise JvmError(
                code,
                f"{message}: {result.output.strip()[:300]}",
                remediation=remediation,
            )

    def _assert_key_present(self, home: str, key: str, workdir: str) -> None:
        """Refuse to sign with whatever the keyring defaults to.

        `gpg --local-user` accepts a key id, a fingerprint, or an email, and an
        ambiguous or absent name is a failure rather than a fallback: signing
        with the wrong key produces artifacts that verify against a key nobody
        chose, which is the failure a manifest's `signing_identity` exists to
        make impossible to miss.
        """

        result = self._run(
            [GPG, "--batch", "--list-secret-keys", "--with-colons", key],
            "gpg-list-keys",
            workdir=workdir,
            environment=self._gpg_environment(home),
        )
        if result.code == 0 and "sec:" in result.output:
            return
        code, message, remediation = classify_failure("gpg-list-keys", result.output)
        raise JvmError(
            code,
            f"{message}: no secret key matched {key!r} in the keyring this job can use",
            remediation=remediation,
        )

    def _sign_artifact(
        self,
        home: str,
        key: str,
        artifact: Artifact,
        passphrase_file: Optional[str],
        workdir: str,
    ) -> str:
        """Detach-sign one artifact, and return the signature's path.

        A detached `.asc` rather than a cleared in-place signature: the bytes the
        manifest recorded are the bytes that were verified, so signing changes
        the *set* of files rather than the content of one, and a consumer can
        still check the jar against the digest the release published.
        """

        signature_path = artifact.path + ".asc"
        argv = [
            GPG,
            "--batch",
            "--yes",
            "--armor",
            "--detach-sign",
            "--local-user",
            key,
            "--output",
            signature_path,
        ]
        if passphrase_file is not None:
            argv.extend(["--pinentry-mode", "loopback", "--passphrase-file", passphrase_file])
        argv.append(artifact.path)
        result = self._run(
            argv, f"gpg-sign-{artifact.name}", workdir=workdir, environment=self._gpg_environment(home)
        )
        if result.code != 0:
            code, message, remediation = classify_failure("gpg-sign", result.output)
            raise JvmError(
                code,
                f"{message} ({artifact.name}): {result.output.strip()[:300]}",
                remediation=remediation,
            )
        if not os.path.isfile(signature_path):
            raise JvmError(
                "signature-absent",
                f"gpg reported success signing {artifact.name} but wrote no signature at "
                f"{signature_path!r}. A registry that finds a missing signature rejects "
                "the whole deployment, so it is refused here where the cause is known",
            )
        return signature_path

    def _signed_manifest(
        self,
        manifest: ArtifactManifest,
        settings: JvmSettings,
        signatures: Mapping[str, str],
    ) -> ArtifactManifest:
        identity = settings.expected_identity
        builder = ManifestBuilder(
            target=manifest.target,
            adapter=self.name,
            source_sha=manifest.source_sha,
            version=manifest.version,
        )
        for artifact in manifest.artifacts:
            signature = signatures.get(artifact.name)
            if signature is None:
                builder.replace(artifact)
                continue
            builder.record(
                name=artifact.name,
                path=artifact.path,
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
            builder.record(
                name=os.path.basename(signature),
                path=signature,
                type=TYPE_SIGNATURE,
                platform=artifact.platform,
                classifier=f"{artifact.classifier}-asc" if artifact.classifier else "asc",
                media_type=MEDIA_TYPE_SIGNATURE,
            )
        return builder.build()

    def _cleanup(
        self, home: str, key_file: Optional[str], passphrase_file: Optional[str]
    ) -> None:
        for path in (key_file, passphrase_file):
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                continue
        if home and home != "" and os.path.isdir(home) and home.startswith(self.scratch_dir):
            shutil.rmtree(home, ignore_errors=True)

    # -- verify -------------------------------------------------------------
    def verify(self, request: BuildRequest, manifest: ArtifactManifest) -> VerificationReport:
        """Check the artifacts against the manifest, and the signatures against GPG.

        Two different checks, because they fail for different reasons. The digest
        check catches a file that changed after the manifest recorded it — a build
        directory shared with another job, a re-run that overwrote its own output —
        and it needs no tools. The signature check catches a signature made by a
        key the release did not intend, and it does need GPG. Reporting them
        together, with the artifact named, is what makes a failure actionable
        rather than merely red.
        """

        settings = self._settings(request)
        if request.dry_run:
            return VerificationReport(
                verified=True,
                code="planned",
                detail="a dry run declares its artifacts and verifies nothing",
                verified_by=self.name,
            )

        failures: List[str] = []
        for artifact in manifest.artifacts:
            mismatch = self._check_digest(artifact)
            if mismatch:
                failures.append(mismatch)
                continue
            if artifact.type == TYPE_SIGNATURE:
                continue
            if settings.signs and artifact.type in self._signable_types and not artifact.signed:
                failures.append(
                    f"{artifact.name}: the release signs its publishable artifacts, but "
                    "this one carries no signature"
                )
        if settings.signs:
            failures.extend(self._check_signatures(request, manifest, settings))

        if failures:
            return VerificationReport(
                verified=False,
                code="jvm-verification-failed",
                detail="; ".join(failures),
                failures=tuple(failures),
            )
        checked = len(manifest.artifacts)
        signed = sum(1 for item in manifest.artifacts if item.signed)
        return VerificationReport(
            verified=True,
            code="verified",
            detail=(
                f"re-measured {checked} artifact(s) against the manifest"
                + (
                    f" and verified {signed} signature(s) with {GPG} for "
                    f"{settings.expected_identity}"
                    if settings.signs
                    else ""
                )
            ),
            verified_by=f"{self.name}/{settings.build_system}",
        )

    def _check_digest(self, artifact: Artifact) -> str:
        """Re-measure one artifact and compare it with what was recorded.

        The manifest was written when the artifact was built; a file that has
        moved since then is a release publishing bytes the manifest does not
        describe, which is the one thing a checksum is supposed to make
        impossible.
        """

        if not os.path.isfile(artifact.path):
            return (
                f"{artifact.name}: recorded at {artifact.path!r}, which is not a readable "
                "file on this runner. Publishing it would ship a manifest row with no "
                "artifact behind it"
            )
        size, digest = digest_file(artifact.path, artifact.digest_algorithm)
        if size != artifact.size or digest != artifact.digest:
            return (
                f"{artifact.name}: re-measured {size} bytes / {digest}, but the manifest "
                f"records {artifact.size} bytes / {artifact.digest}. The artifact changed "
                "after the build was recorded, so the manifest no longer describes what "
                "would be published"
            )
        return ""

    def _check_signatures(
        self, request: BuildRequest, manifest: ArtifactManifest, settings: JvmSettings
    ) -> List[str]:
        """Verify every signature this release claims, and check the key.

        The signature is verified against the artifact it was made for, and the
        expected key has to appear in what GPG reports. A signature that verifies
        against *some* key is a fact about cryptography and not about this
        release: only the named key is a statement a consumer can act on.
        """

        failures: List[str] = []
        home = self._gpg_home(settings)
        signatures = {item.name: item for item in manifest.artifacts if item.type == TYPE_SIGNATURE}
        if not signatures:
            return [
                "this release signs its publishable artifacts, but the manifest carries "
                "no signature for any of them"
            ]
        try:
            for artifact in manifest.artifacts:
                # Only what signing actually covers is required to carry a
                # signature. The checksum file and any SBOM are evidence about
                # the release rather than part of it, and demanding a signature
                # the sign stage never makes would fail every signed release with
                # checksums enabled.
                if artifact.type not in self._signable_types or artifact.is_evidence:
                    continue
                signature = signatures.get(os.path.basename(artifact.path) + ".asc")
                if signature is None:
                    failures.append(f"{artifact.name}: no detached signature was recorded")
                    continue
                if not self._verify_signature(home, settings, artifact, signature):
                    failures.append(f"{artifact.name}: the signature did not verify")
        finally:
            if home.startswith(self.scratch_dir) and os.path.isdir(home):
                shutil.rmtree(home, ignore_errors=True)
        return failures

    def _verify_signature(
        self, home: str, settings: JvmSettings, artifact: Artifact, signature: Artifact
    ) -> bool:
        result = self._run(
            [GPG, "--verify", signature.path, artifact.path],
            f"gpg-verify-{artifact.name}",
            workdir=self.workdir or "",
            environment=self._gpg_environment(home),
        )
        if result.code != 0:
            return False
        expected = settings.expected_identity
        if expected and not _identity_in(output=result.output, expected=expected):
            return False
        return True

    # -- manual lifecycle for a caller that runs stages itself --------------
    def cleanup(self) -> None:
        if os.path.isdir(self.scratch_dir):
            shutil.rmtree(self.scratch_dir, ignore_errors=True)


def _identity_in(output: str, expected: str) -> bool:
    """Whether GPG's output says the expected key signed the artifact.

    Compared case-insensitively and on the trailing 16 hex characters as well as
    in full, because a fingerprint is the same key however it is abbreviated and
    a check that only accepted one spelling of it would fail a correct
    signature for a formatting reason.
    """

    haystack = (output or "").replace(" ", "")
    needles = {expected.replace(" ", "")}
    if _FINGERPRINT_RE.match(expected):
        stripped = expected.lstrip("0").rstrip("0") or "0"
        needles.add(stripped)
        needles.add(stripped[-16:])
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles if needle)


def components(adapter: JvmAdapter, *publishers: Any) -> Dict[str, Any]:
    """The wiring a caller hands to `ReleaseComponents`.

    Kept as a helper so a repository does not have to know the registry names,
    but deliberately not a global registry: the core takes its components by
    injection, and a module that registered itself globally would re-introduce
    the coupling the contract removed.
    """

    wiring: Dict[str, Any] = {"adapters": {ADAPTER_NAME: adapter}}
    if publishers:
        wiring["publishers"] = tuple(publishers)
    return wiring


__all__ = [
    "ADAPTER_NAME",
    "BUILD_AMBIGUOUS",
    "BUILD_AUTO",
    "BUILD_GRADLE",
    "BUILD_MAVEN",
    "CLASSIFIER_DIST_TAR",
    "CLASSIFIER_DIST_ZIP",
    "CLASSIFIER_JAVADOC",
    "CLASSIFIER_MAIN",
    "CLASSIFIER_POM",
    "CLASSIFIER_SOURCES",
    "DEFAULT_GRADLEW",
    "DEFAULT_MVNW",
    "DISTRIBUTION_TAR",
    "DISTRIBUTION_ZIP",
    "GPG",
    "GPG_HOME_MODE",
    "KEY_ENV",
    "MEDIA_TYPE_JAR",
    "MEDIA_TYPE_POM",
    "MEDIA_TYPE_SIGNATURE",
    "MEDIA_TYPE_TAR",
    "MEDIA_TYPE_ZIP",
    "MODE_APPLICATION",
    "MODE_LIBRARY",
    "OUTPUT_APPLICATION_JAR",
    "OUTPUT_DISTRIBUTION",
    "OUTPUT_JAVADOC",
    "OUTPUT_LIBRARY_JAR",
    "OUTPUT_POM",
    "OUTPUT_SOURCES",
    "PASSPHRASE_ENV",
    "SECRET_FILE_MODE",
    "SUPPORTED_BUILD_SYSTEMS",
    "SUPPORTED_DISTRIBUTIONS",
    "SUPPORTED_MODES",
    "SUPPORTED_OUTPUTS",
    "VERSION_PROPERTY",
    "CommandResult",
    "CommandRunner",
    "JvmAdapter",
    "JvmError",
    "JvmSettings",
    "MavenCoordinates",
    "PomMetadata",
    "StagedArtifact",
    "bind_material",
    "classify_failure",
    "classify_output",
    "components",
    "discover",
    "maven_version_for",
    "parse_settings",
    "planned_paths",
    "read_pom",
    "settings_from",
    "signing_material_present",
    "subprocess_runner",
]
