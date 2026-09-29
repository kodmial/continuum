"""MacPorts publication: a Portfile, the port tree it lives in, and the installer pin.

Three things have to agree for a MacPorts release to be installable, and each of
them is a place the others can silently drift:

* **The Portfile is the port.** It names the version, the source it fetches, and
  the checksum of that source. A Portfile that pins a checksum the release did
  not publish is a port that fails on a user's machine, so the digest comes from
  the release manifest and the source is fetched and verified before the file is
  written — the same rule the Homebrew publisher follows, implemented against the
  same helpers.
* **The tree carries the Portfile.** A port tree is a repository whose
  `<category>/<name>` directory is what `port install` reads after `sync`. The
  publisher writes into that tree as a separate destination with its own
  credential, and reports `already-current` when the tree already holds exactly
  these bytes.
* **The installer pins the tree revision.** A bootstrapping script that checks
  out the tree at an exact revision goes stale the moment the tree advances, and
  a stale pin installs the *previous* Portfile. So the pin is written from the
  revision the tree commit actually produced, and only ever from that revision.

A Portfile is Tcl, but it is not standalone Tcl: `PortSystem` and `PortGroup` are
commands only the `port` tool defines, so a general Tcl parser rejects every
valid Portfile. That is why the structural checks here are content assertions —
`PortSystem`, a version-bearing setup line, a numeric `revision`,
`use_configure no`, `build {}`, a livecheck — rather than a call to `tclsh`. A
check that cannot pass on a correct file is worse than no check, because it
teaches people to disable checks.

Nothing here names a project. The port name, category, maintainers, license,
description, GitHub coordinates, revision, and installer path are all
configuration, and the built-in Portfile is assembled from them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .assets import AssetFetcher, verify_all
from .contract import (
    ALREADY_CURRENT,
    CONFIGURATION_INVALID,
    CREDENTIAL_MISSING,
    DESTINATION_UNREACHABLE,
    DISABLED,
    DRY_RUN,
    KIND_ARCHIVE,
    MODE_PULL_REQUEST,
    NO_ARTIFACT,
    PUBLISHED,
    STATUS_PUBLISHED,
    STATUS_SKIPPED,
    SURFACE_DISABLED,
    ArtifactManifest,
    CommitResult,
    Destination,
    PublishRequest,
    PublishResult,
    PublisherError,
    PublishedArtifact,
)
from .repository import PackageRepository, reconcile
from .settings import (
    credential_present,
    parse_destination,
    reject_unknown,
    require_block_text,
    require_bool,
    require_int,
    require_mapping,
    require_mapping_of_text,
    require_relative_path,
    require_text,
)
from .template import extra_tokens, render as render_template
from .validation import GeneratedFile, audit, raise_for

PUBLISHER_NAME = "macports"

SURFACE_PORTFILE = "portfile"
SURFACE_INSTALLER = "installer"
SURFACES = (SURFACE_PORTFILE, SURFACE_INSTALLER)

# The layout the `port` tool indexes. It is derived rather than configured: a
# tree that wants it elsewhere is not a tree MacPorts can use.
PORTFILE_NAME = "Portfile"

# The line the installer pin is written to. The reference installer uses a shell
# assignment, and matching it exactly keeps the pin auditable by a `grep -x`
# rather than by an opinion about shell syntax.
DEFAULT_PIN_VARIABLE = "PIN_REV"

# The built-in Portfile uses the `github` portgroup when the release is served
# from GitHub; otherwise it fetches the exact asset URL, because
# `github.tarball_from releases` cannot describe an asset whose name the release
# chose.
_GITHUB_HOST_RE = re.compile(r"^https://github\.com/", re.IGNORECASE)
_PORT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
_VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# -- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class PortfileSettings:
    """Everything the Portfile says, minus the version and checksums.

    The version and checksums are release facts and come from the manifest, so
    they are deliberately absent here; everything a human would review in a
    Portfile diff is a setting.
    """

    enabled: bool = False
    optional: bool = False
    name: str = ""
    category: str = ""
    categories: Tuple[str, ...] = ()
    license: str = ""
    maintainers: str = ""
    description: str = ""
    long_description: str = ""
    revision: int = 0
    homepage: str = ""
    depends_lib: Tuple[str, ...] = ()
    template: str = ""
    template_path: str = ""
    path: str = ""
    required_text: Tuple[str, ...] = ()
    forbidden_text: Tuple[str, ...] = ()
    extra_tokens: Mapping[str, str] = field(default_factory=dict)

    @property
    def port_categories(self) -> Tuple[str, ...]:
        """The `categories` line, category first and each named once."""

        ordered: List[str] = []
        for item in (self.category, *self.categories):
            if item and item not in ordered:
                ordered.append(item)
        return tuple(ordered)

    @property
    def port_directory(self) -> str:
        return f"{self.category}/{self.name}"

    def tree_path(self) -> str:
        """Where inside the tree this port lives."""

        if self.path:
            return self.path
        return f"{self.port_directory}/{PORTFILE_NAME}"

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "optional": self.optional,
            "name": self.name,
            "category": self.category,
            "categories": list(self.categories),
            "license": self.license,
            "maintainers": self.maintainers,
            "description": self.description,
            "revision": self.revision,
            "template": self.template,
            "template_path": self.template_path,
            "path": self.path,
            "extra_tokens": dict(self.extra_tokens or {}),
        }


@dataclass(frozen=True)
class InstallerPinSettings:
    """The bootstrap script that checks out the tree at an exact revision."""

    destination: Destination
    path: str
    variable: str = DEFAULT_PIN_VARIABLE

    def describe(self) -> Dict[str, Any]:
        return {
            "destination": self.destination.describe(),
            "path": self.path,
            "variable": self.variable,
        }


@dataclass(frozen=True)
class MacPortsSettings:
    """A port tree, the port inside it, and the installer that pins the tree."""

    name: str
    tree: Destination
    portfile: PortfileSettings
    enabled: bool = True
    branch_prefix: str = "continuum/macports"
    installer: Optional[InstallerPinSettings] = None
    files: Mapping[str, str] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "publisher": PUBLISHER_NAME,
            "enabled": self.enabled,
            "name": self.name,
            "tree": self.tree.describe(),
            "branch_prefix": self.branch_prefix,
            "portfile": self.portfile.describe(),
            "installer": self.installer.describe() if self.installer else None,
            "files": dict(self.files or {}),
        }


_PORTFILE_KEYS = (
    "enabled",
    "optional",
    "name",
    "category",
    "categories",
    "license",
    "maintainers",
    "description",
    "long_description",
    "revision",
    "homepage",
    "depends_lib",
    "template",
    "template_path",
    "path",
    "required_text",
    "forbidden_text",
    "tokens",
)

_INSTALLER_KEYS = ("destination", "path", "variable")

_MACPORTS_KEYS = (
    "enabled",
    "name",
    "tree",
    "branch_prefix",
    "portfile",
    "installer",
    "files",
)


def _optional_path(value: Any, where: str) -> str:
    if value is None:
        return ""
    return require_relative_path(value, where)


def _text_tuple(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be a non-empty list")
    return tuple(require_text(item, f"{where}[{index}]") for index, item in enumerate(value))


def _parse_portfile(value: Any, where: str) -> PortfileSettings:
    mapping = require_mapping(value, where)
    reject_unknown(mapping, _PORTFILE_KEYS, where)
    name = require_text(mapping.get("name"), f"{where}.name", maximum=64, allow_empty=True)
    if name and not _PORT_NAME_RE.match(name):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where}.name must be a lower-case port name; got {name!r}"
        )
    return PortfileSettings(
        enabled=require_bool(mapping.get("enabled"), f"{where}.enabled", True),
        optional=require_bool(mapping.get("optional"), f"{where}.optional", False),
        name=name,
        category=require_text(
            mapping.get("category"), f"{where}.category", maximum=64, allow_empty=True
        ),
        categories=_text_tuple(mapping.get("categories"), f"{where}.categories"),
        license=require_text(
            mapping.get("license"), f"{where}.license", maximum=200, allow_empty=True
        ),
        maintainers=require_text(
            mapping.get("maintainers"), f"{where}.maintainers", allow_empty=True
        ),
        description=require_text(
            mapping.get("description"), f"{where}.description", maximum=300, allow_empty=True
        ),
        long_description=require_text(
            mapping.get("long_description"),
            f"{where}.long_description",
            maximum=2000,
            allow_empty=True,
        ),
        revision=require_int(
            mapping.get("revision"), f"{where}.revision", 0, minimum=0, maximum=1000
        ),
        homepage=require_text(
            mapping.get("homepage"), f"{where}.homepage", maximum=300, allow_empty=True
        ),
        depends_lib=_text_tuple(mapping.get("depends_lib"), f"{where}.depends_lib"),
        template=require_block_text(mapping.get("template"), f"{where}.template")
        if mapping.get("template")
        else "",
        template_path=_optional_path(mapping.get("template_path"), f"{where}.template_path"),
        path=_optional_path(mapping.get("path"), f"{where}.path"),
        required_text=_text_tuple(mapping.get("required_text"), f"{where}.required_text"),
        forbidden_text=_text_tuple(mapping.get("forbidden_text"), f"{where}.forbidden_text"),
        extra_tokens=extra_tokens(
            require_mapping_of_text(mapping.get("tokens"), f"{where}.tokens"), f"{where}.tokens"
        ),
    )


def _parse_installer(value: Any, where: str) -> InstallerPinSettings:
    mapping = require_mapping(value, where)
    reject_unknown(mapping, _INSTALLER_KEYS, where)
    variable = (
        require_text(mapping.get("variable"), f"{where}.variable", maximum=64, allow_empty=True)
        or DEFAULT_PIN_VARIABLE
    )
    if not _VARIABLE_RE.match(variable):
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where}.variable must name a shell variable; got {variable!r}",
        )
    return InstallerPinSettings(
        destination=parse_destination(
            mapping.get("destination"), f"{where}.destination", default_mode="pull-request"
        ),
        path=require_relative_path(mapping.get("path"), f"{where}.path"),
        variable=variable,
    )


def parse_settings(value: Any, where: str = "macports") -> MacPortsSettings:
    """Validate one MacPorts publisher's configuration."""

    mapping = require_mapping(value, where)
    reject_unknown(mapping, _MACPORTS_KEYS, where)
    name = require_text(mapping.get("name"), f"{where}.name", maximum=64)
    if not _PORT_NAME_RE.match(name):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where}.name must be a lower-case port name; got {name!r}"
        )
    portfile_value = mapping.get("portfile")
    portfile = (
        _parse_portfile(portfile_value, f"{where}.portfile")
        if portfile_value is not None
        else PortfileSettings()
    )
    if portfile.enabled and not portfile.name:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where}.portfile.name is required when the portfile surface is enabled: "
            "the port name is the directory the tree indexes and the name "
            "`port install` uses",
        )
    installer_value = mapping.get("installer")
    branch_prefix = require_text(
        mapping.get("branch_prefix"), f"{where}.branch_prefix", maximum=100
    ) if mapping.get("branch_prefix") is not None else "continuum/macports"
    return MacPortsSettings(
        enabled=require_bool(mapping.get("enabled"), f"{where}.enabled", True),
        name=name,
        tree=parse_destination(mapping.get("tree"), f"{where}.tree", default_mode="pull-request"),
        branch_prefix=branch_prefix,
        portfile=portfile,
        installer=(
            _parse_installer(installer_value, f"{where}.installer")
            if installer_value is not None
            else None
        ),
        files=require_mapping_of_text(mapping.get("files"), f"{where}.files"),
    )


# -- template assembly ------------------------------------------------------


def _single_archive(manifest: ArtifactManifest) -> PublishedArtifact:
    """The one source archive a built-in Portfile can describe.

    MacPorts fetches one distfile and checks one checksum; a release split by
    architecture has no single source, and pretending otherwise would pin the
    checksum of one slice and fail on the other. A project in that position
    supplies a template that fetches each slice.
    """

    archive = manifest.unversioned(KIND_ARCHIVE)
    if archive is not None:
        return archive
    published = manifest.select(KIND_ARCHIVE)
    if len(published) == 1:
        return published[0]
    raise PublisherError(
        NO_ARTIFACT,
        "a built-in Portfile describes exactly one source archive and this release "
        f"published {len(published)} that fit neither shape; supply a template that "
        "fetches the slice it wants",
    )


def _is_github(manifest: ArtifactManifest) -> bool:
    return bool(_GITHUB_HOST_RE.match(manifest.release.download_base or ""))


def _portfile_lines(
    manifest: ArtifactManifest, portfile: PortfileSettings
) -> Tuple[List[str], List[str]]:
    release = manifest.release
    archive = _single_archive(manifest)
    categories = " ".join(portfile.port_categories)
    homepage = portfile.homepage or f"https://github.com/{release.repository}"
    lines = [
        "# -*- coding: utf-8; mode: tcl; tab-width: 4; "
        "indent-tabs-mode: nil; c-basic-offset: 4 -*-",
        "PortSystem          1.0",
        "PortGroup           github 1.0",
        "",
    ]
    required = ["PortSystem", "PortGroup"]
    if _is_github(manifest):
        lines += [
            f"github.setup        {release.owner} {release.name} {release.version}",
            "github.tarball_from releases",
            f"distname            {archive.name}",
        ]
        required.append("github.setup")
    else:
        lines += [
            f"master_sites        {release.download_base.rstrip('/')}",
            f"distfiles           {archive.name}",
        ]
        required.append("master_sites")
    lines += [
        f"revision            {portfile.revision}",
        "",
        f"categories          {categories}",
        "platforms           darwin",
        f"license             {portfile.license}",
        f"maintainers         {portfile.maintainers}",
        f"description         {portfile.description}",
        f"long_description    {portfile.long_description or '${description}'}",
        f"homepage            {homepage}",
    ]
    if portfile.depends_lib:
        lines.append(f"depends_lib         {' '.join(portfile.depends_lib)}")
    lines += [
        "",
        f"checksums           sha256  {archive.sha256}",
        "",
        "use_configure       no",
        "build               {}",
        "",
        "livecheck.type      github",
    ]
    required += [
        "revision",
        "checksums",
        "use_configure       no",
        "build               {}",
        "livecheck.type      github",
    ]
    return lines, required


def default_portfile_template(
    manifest: ArtifactManifest, portfile: PortfileSettings
) -> Tuple[str, Tuple[str, ...]]:
    """The built-in Portfile, and the structural assertions that go with it.

    The assertions are returned rather than kept private so a consumer template
    can be held to the same structural guarantees: a project that writes its own
    Portfile should not thereby lose the check that it names a version and pins a
    checksum.
    """

    for field_name, value in (
        ("name", portfile.name),
        ("category", portfile.category),
        ("license", portfile.license),
        ("maintainers", portfile.maintainers),
        ("description", portfile.description),
    ):
        if not value:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"the built-in Portfile needs {field_name}: MacPorts rejects a port that "
                "omits it. Supply it in the settings, or provide a template of your own",
            )
    lines, required = _portfile_lines(manifest, portfile)
    return "\n".join(lines) + "\n", tuple(required)


# -- rendering --------------------------------------------------------------


def _read_template(portfile: PortfileSettings, where: str, source_root: str) -> str:
    if portfile.template and portfile.template_path:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} sets both template and template_path; a template has one source",
        )
    if portfile.template:
        return portfile.template
    if not portfile.template_path:
        return ""
    path = os.path.join(source_root, portfile.template_path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where}.template_path could not be read: {exc}"
        ) from None


def _token_values(
    manifest: ArtifactManifest, portfile: PortfileSettings
) -> Dict[str, Optional[str]]:
    release = manifest.release
    values: Dict[str, Optional[str]] = {
        "NAME": portfile.name,
        "VERSION": release.version,
        "TAG": release.tag,
        "REPOSITORY": release.repository,
        "DOWNLOAD_BASE": release.download_base,
        "GITHUB_OWNER": release.owner,
        "GITHUB_REPO": release.name,
        "CATEGORY": portfile.category,
        "CATEGORIES": " ".join(portfile.port_categories),
        "MAINTAINERS": portfile.maintainers,
        "REVISION": str(portfile.revision),
        "LICENSE": portfile.license,
        "DESCRIPTION": portfile.description,
        "HOMEPAGE": portfile.homepage,
    }
    for artifact in manifest.select(KIND_ARCHIVE):
        suffix = artifact.token_architecture
        values[f"NAME_{suffix}" if suffix else "NAME_ARCHIVE"] = artifact.name
        values[f"URL_{suffix}" if suffix else "URL_ARCHIVE"] = artifact.url
        values[f"SHA256_{suffix}" if suffix else "SHA256_ARCHIVE"] = artifact.sha256
    for name, value in (portfile.extra_tokens or {}).items():
        values.setdefault(name, value)
    return values


def _portfile_file(
    settings: MacPortsSettings, manifest: ArtifactManifest, source_root: str
) -> GeneratedFile:
    portfile = settings.portfile
    path = portfile.tree_path()
    where = "macports.portfile"
    template = _read_template(portfile, where, source_root)
    structural: Tuple[str, ...] = ()
    if template:
        content = render_template(template, _token_values(manifest, portfile), where=path)
    else:
        content, structural = default_portfile_template(manifest, portfile)
        content = render_template(content, _token_values(manifest, portfile), where=path)
    return GeneratedFile(
        path=path,
        content=content,
        surface=SURFACE_PORTFILE,
        selected=manifest.select(KIND_ARCHIVE),
        required_text=(*structural, *portfile.required_text),
        forbidden_text=portfile.forbidden_text,
    )


def plan(
    request: PublishRequest, *, fetcher: AssetFetcher, source_root: str = "."
) -> Tuple[GeneratedFile, ...]:
    """The Portfile this publish would write, verified, and not yet written."""

    settings: MacPortsSettings = request.settings
    if not settings.portfile.enabled:
        return ()
    manifest = request.manifest
    verify_all((_single_archive(manifest),), fetcher)
    generated = _portfile_file(settings, manifest, source_root)
    raise_for(audit(generated, manifest))
    return (generated,)


# -- installer pin ----------------------------------------------------------


def pin_content(content: str, variable: str, revision: str, path: str) -> str:
    """Rewrite the one line that pins the tree revision.

    Anchored to a whole line of the exact form `VARIABLE="<revision>"`. A pin
    that matched loosely would rewrite a comment, or half of a longer variable
    name, and the installer would then check out whatever it happened to point at
    before.
    """

    pattern = re.compile(rf'^{re.escape(variable)}="[^"]*"[ \t]*$', re.MULTILINE)
    if not pattern.search(content or ""):
        raise PublisherError(
            CONFIGURATION_INVALID,
            f'{path} has no {variable}="<revision>" line for the installer pin to update',
            remediation=(
                f'add a line exactly of the form {variable}="<revision>" to {path}, or '
                "point macports.installer.path at the file that already carries it"
            ),
        )
    return pattern.sub(f'{variable}="{revision}"', content)


def _read_files(settings: MacPortsSettings, source_root: str) -> Dict[str, str]:
    """Consumer files that belong in the port directory beside the Portfile.

    A port can ship more than a Portfile — a sample config, a patch, a wrapper —
    and those are read from the consumer's checkout and written into the tree at
    the path the consumer names, *relative to the port directory*. They are
    content, not templates: filling a patch with release facts is not a thing
    anyone wants.
    """

    files: Dict[str, str] = {}
    prefix = f"{settings.portfile.port_directory}/"
    for destination_path, source_path in sorted((settings.files or {}).items()):
        rel = require_relative_path(destination_path, f"macports.files.{destination_path}")
        origin = os.path.join(source_root, source_path)
        try:
            with open(origin, "r", encoding="utf-8") as handle:
                files[prefix + rel] = handle.read()
        except OSError as exc:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"macports.files[{destination_path!r}] names {source_path!r}, which "
                f"could not be read: {exc}",
            ) from None
    return files


# -- publication ------------------------------------------------------------


def _skip(
    settings: MacPortsSettings,
    reason: str,
    message: str,
    *,
    branch: str = "",
    notes: Tuple[str, ...] = (),
    **extra: Any,
) -> PublishResult:
    return PublishResult(
        publisher=PUBLISHER_NAME,
        status=STATUS_SKIPPED,
        reason=reason,
        destination=settings.tree.repository,
        branch=branch or settings.tree.branch,
        message=message,
        notes=notes,
        **extra,
    )


def _target_branch(destination: Destination, prefix: str, version: str) -> str:
    if destination.mode == MODE_PULL_REQUEST:
        return destination.feature_branch(prefix, version)
    return destination.branch


def _credential_failure(
    settings: MacPortsSettings, destination: Destination, surfaces: Tuple[str, ...]
) -> PublishResult:
    return PublishResult.failure(
        PUBLISHER_NAME,
        PublisherError(
            CREDENTIAL_MISSING,
            f"the credential for {destination.repository!r} is not available in this job",
            remediation=(
                f"expose the repository secret {destination.credential_secret} to this job"
            ),
        ),
        destination=settings.tree.repository,
        branch=settings.tree.branch,
        surfaces=surfaces,
    )


def _pin_installer(
    settings: MacPortsSettings,
    *,
    version: str,
    revision: str,
    repository: PackageRepository,
    environment: Mapping[str, str],
) -> CommitResult:
    """Write the tree revision into the installer, idempotently.

    A no-op reads the installer and finds the pin already correct, which is the
    re-run case and not an error.
    """

    pin = settings.installer
    assert pin is not None  # the caller checked
    if not credential_present(pin.destination, environment):
        raise PublisherError(
            CREDENTIAL_MISSING,
            f"the credential for the installer destination "
            f"{pin.destination.repository!r} is not available in this job",
            remediation=(
                f"expose the repository secret {pin.destination.credential_secret} to "
                "this job"
            ),
        )
    branch = _target_branch(pin.destination, settings.branch_prefix, version)

    def build(_head: str) -> Mapping[str, str]:
        existing = repository.read_file(pin.path) or ""
        return {pin.path: pin_content(existing, pin.variable, revision, pin.path)}

    return reconcile(
        repository,
        build,
        message=f"chore(release): pin {settings.name} v{version} MacPorts tree",
        branch=branch,
        attempts=pin.destination.attempts,
    )


def publish(
    request: PublishRequest,
    repository: PackageRepository,
    *,
    fetcher: AssetFetcher,
    installer_repository: Optional[PackageRepository] = None,
    source_root: str = ".",
) -> PublishResult:
    """Write the port tree, then pin the revision the tree produced."""

    settings: MacPortsSettings = request.settings
    if not settings.enabled:
        return _skip(settings, DISABLED, "the MacPorts publisher is disabled for this project")

    wants_portfile = settings.portfile.enabled and request.surface_requested(
        SURFACE_PORTFILE, True
    )
    wants_installer = settings.installer is not None and request.surface_requested(
        SURFACE_INSTALLER, True
    )
    if not wants_portfile and not wants_installer:
        return _skip(
            settings,
            SURFACE_DISABLED,
            "neither the portfile nor the installer surface is enabled; there is "
            "nothing to publish",
            surfaces=(),
        )

    manifest = request.manifest
    version = manifest.release.version
    notes: List[str] = []
    files: List[str] = []
    surfaces: List[str] = []
    tree_commit: Optional[CommitResult] = None
    installer_commit: Optional[CommitResult] = None

    if wants_installer and settings.installer is not None:
        if not credential_present(settings.installer.destination, request.environment):
            return _credential_failure(
                settings, settings.installer.destination, (SURFACE_INSTALLER,)
            )
    if not request.dry_run and wants_portfile and not credential_present(
        settings.tree, request.environment
    ):
        return _credential_failure(settings, settings.tree, (SURFACE_PORTFILE,))

    try:
        if wants_portfile:
            verify_all((_single_archive(manifest),), fetcher)
            generated = _portfile_file(settings, manifest, source_root)
            raise_for(audit(generated, manifest))
            tree_files = {generated.path: generated.content}
            tree_files.update(_read_files(settings, source_root))
            files.extend(sorted(tree_files))
            surfaces.append(SURFACE_PORTFILE)
            if not request.dry_run:
                tree_commit = reconcile(
                    repository,
                    lambda _head: tree_files,
                    message=f"chore(release): sync {settings.name} v{version} port tree",
                    branch=_target_branch(settings.tree, settings.branch_prefix, version),
                    attempts=settings.tree.attempts,
                )

        if wants_installer and settings.installer is not None:
            if request.dry_run:
                files.append(settings.installer.path)
            else:
                if tree_commit is not None and tree_commit.revision:
                    revision = tree_commit.revision
                else:
                    revision = repository.head_revision()
                installer_commit = _pin_installer(
                    settings,
                    version=version,
                    revision=revision,
                    repository=installer_repository or repository,
                    environment=request.environment,
                )
                files.append(settings.installer.path)
                if installer_commit.changed:
                    notes.append(
                        f"pinned {settings.installer.variable} in "
                        f"{settings.installer.destination.repository}:"
                        f"{settings.installer.path} to tree revision {revision}"
                    )
                else:
                    notes.append(
                        f"{settings.installer.destination.repository}:"
                        f"{settings.installer.path} already pins this tree revision"
                    )
            surfaces.append(SURFACE_INSTALLER)
    except PublisherError as exc:
        return PublishResult.failure(
            PUBLISHER_NAME,
            exc,
            destination=settings.tree.repository,
            branch=settings.tree.branch,
            surfaces=tuple(surfaces),
            notes=tuple(notes),
        )

    if request.dry_run:
        return _skip(
            settings,
            DRY_RUN,
            "the Portfile was generated and validated; nothing was written",
            files=tuple(sorted(set(files))),
            surfaces=tuple(surfaces),
            notes=tuple(notes),
        )

    changed = bool(
        (tree_commit and tree_commit.changed) or (installer_commit and installer_commit.changed)
    )
    if not changed:
        reference = tree_commit or installer_commit or repository.head_revision()
        revision = reference.revision if isinstance(reference, CommitResult) else reference
        return _skip(
            settings,
            ALREADY_CURRENT,
            f"the port tree already describes v{version} byte for byte"
            + (" and the installer pin already matches" if wants_installer else ""),
            branch=settings.tree.branch,
            revision=revision,
            files=tuple(sorted(set(files))),
            surfaces=tuple(surfaces),
            notes=tuple(notes),
        )
    if tree_commit is not None and tree_commit.changed:
        branch, revision = tree_commit.branch, tree_commit.revision
    elif installer_commit is not None:
        branch, revision = installer_commit.branch, installer_commit.revision
    else:  # pragma: no cover - changed implies one of the two committed
        branch, revision = settings.tree.branch, repository.head_revision()
    return PublishResult(
        publisher=PUBLISHER_NAME,
        status=STATUS_PUBLISHED,
        reason=PUBLISHED,
        destination=settings.tree.repository,
        branch=branch,
        revision=revision,
        files=tuple(sorted(set(files))),
        surfaces=tuple(surfaces),
        notes=tuple(notes),
        message=f"the port tree now describes v{version}",
    )


__all__ = [
    "DEFAULT_PIN_VARIABLE",
    "InstallerPinSettings",
    "MacPortsSettings",
    "PORTFILE_NAME",
    "PortfileSettings",
    "PUBLISHER_NAME",
    "SURFACES",
    "SURFACE_INSTALLER",
    "SURFACE_PORTFILE",
    "default_portfile_template",
    "parse_settings",
    "pin_content",
    "plan",
    "publish",
]
