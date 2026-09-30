"""Homebrew publication: a formula, a cask, or both.

The two are separate surfaces with separate toggles, and they are separate for
a reason rather than for tidiness. A formula installs a source archive; a cask
installs an application bundle. They pin *different assets* with *different
checksums*, and a release that ships one of them has no business claiming the
other. A project that only ships a command line tool configures a formula; a
project whose release is a bundle configures a cask; a project that ships both
configures both, and each still verifies only the assets it actually pins. That
is also why an unconfigured surface is off rather than on-by-default-with-a-
warning: there is no sensible default for "does this release have a bundle", and
guessing one is how a tap ends up with a cask pointing at a tarball.

Nothing here names a project. The token, the class name, the tap, the app
bundle, the launchd label, and the macOS floor are all configuration, and the
built-in templates are assembled from those values rather than shipped with any
of them baked in.

**Quarantine is the one place this publisher refuses to be helpful.** A cask
that strips `com.apple.quarantine` from an installed bundle tells Gatekeeper the
user has vouched for an artifact nobody checked, and doing that to a *notarized*
artifact throws away the only evidence that it was checked. So:

* a signed or notarized artifact may never have its quarantine cleared, no
  matter what the configuration asks for;
* an unvetted one may, but only as an explicit, evidenced decision — the
  configuration has to say in a sentence which artifact cannot pass the check
  and why the attribute is still set on it, because that sentence is what the
  next reviewer reads;
* and the generated cask is audited afterwards, because a workaround that
  arrives by accident is worse than one that was asked for. Every `xattr`
  invocation must name the quarantine attribute, none may remove every extended
  attribute, and none may reach for the Gatekeeper override — which is not a
  compatibility step at all, but a way of switching the check off for the
  machine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .assets import AssetFetcher, verify_all
from .contract import (
    ALREADY_CURRENT,
    ARCH_ARM64,
    ARCH_X86_64,
    CONFIGURATION_INVALID,
    DISABLED,
    DRY_RUN,
    KIND_APP_BUNDLE,
    KIND_ARCHIVE,
    MODE_PULL_REQUEST,
    NO_ARTIFACT,
    PUBLISHED,
    QUARANTINE_POLICY,
    STATUS_PUBLISHED,
    STATUS_SKIPPED,
    SURFACE_DISABLED,
    ArtifactManifest,
    Destination,
    PublishRequest,
    PublishResult,
    PublisherError,
    PublishedArtifact,
)
from .repository import CommandRunner, PackageRepository, reconcile
from .settings import (
    credential_present,
    parse_destination,
    reject_unknown,
    require_block_text,
    require_bool,
    require_manifest_path,
    require_manifest_string,
    require_mapping,
    require_mapping_of_text,
    require_text,
    require_token,
    require_url,
)
from .template import extra_tokens, render as render_template
from .validation import (
    CHECK_FAILED,
    CHECK_PASSED,
    CheckOutcome,
    GeneratedFile,
    ValidationReport,
    audit,
    combine,
    raise_for,
    syntax_check,
)

PUBLISHER_NAME = "homebrew"

SURFACE_FORMULA = "formula"
SURFACE_CASK = "cask"
SURFACES = (SURFACE_FORMULA, SURFACE_CASK)

# Where a tap keeps each kind of file. These are the package manager's own
# conventions rather than any project's layout, and they are overridable per
# surface because a tap that wants them elsewhere is entitled to.
FORMULA_DIRECTORY = "Formula"
CASK_DIRECTORY = "Casks"

# The parser both surfaces have to satisfy, named as an argument vector and
# never as a shell string. A repository name, a bundle name, and a commit
# message are all data that comes from configuration.
RUBY = "ruby"
RUBY_SYNTAX = (RUBY, "-c")

# The one attribute a compatibility workaround is allowed to touch.
QUARANTINE_ATTRIBUTE = "com.apple.quarantine"
XATTR_TOOL = "/usr/bin/xattr"

# `spctl` on its own appears in prose; the override is the pair. Both words must
# be present before a file is treated as disabling Gatekeeper, so a cask that
# merely explains why it does not is not failed for explaining itself.
GATEKEEPER_OVERRIDE = ("spctl", "master-disable")

# Removing every extended attribute is not "removing quarantine" — it is
# removing the signature, the provenance, and anything else macOS recorded. The
# forms below are the argument lists that would do it.
BLANKET_ATTRIBUTE_FLAGS = ('"-c"', '"-cr"', "'-c'", "'-cr'")

# Ruby strings can be concatenated across lines with a trailing comma, so the
# scan covers the whole `xattr` call rather than the physical line it starts on.
# Stopping at the line break is what let a two-line `args:` list pass as though
# the attribute had been named on the first line.
_XATTR_CALL_RE = re.compile(r"xattr.*?(?=\n\s*(?:#|run\s|end\b)|\Z)", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"^\s*#.*$")


# -- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class ServiceMetadata:
    """What `brew services` should know about a background daemon.

    The launchd label is required and free-form, because it is the identity the
    operating system uses. A formula that lets the package manager invent one
    ends up with a second, differently-named daemon registered alongside the
    first, and the uninstall leaves it behind.
    """

    label: str
    run_at_load: bool = True
    keep_alive: bool = True
    working_dir: str = ""
    log_path: str = ""

    def describe(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "run_at_load": self.run_at_load,
            "keep_alive": self.keep_alive,
            "working_dir": self.working_dir,
            "log_path": self.log_path,
        }


def _parse_service(value: Any, where: str) -> Optional[ServiceMetadata]:
    if value is None:
        return None
    mapping = require_mapping(value, where)
    reject_unknown(
        mapping, ("label", "run_at_load", "keep_alive", "working_dir", "log_path"), where
    )
    return ServiceMetadata(
        label=require_text(mapping.get("label"), f"{where}.label", maximum=200),
        run_at_load=require_bool(mapping.get("run_at_load"), f"{where}.run_at_load", True),
        keep_alive=require_bool(mapping.get("keep_alive"), f"{where}.keep_alive", True),
        working_dir=require_manifest_string(
            mapping.get("working_dir"), f"{where}.working_dir", allow_empty=True
        ),
        log_path=require_manifest_string(
            mapping.get("log_path"), f"{where}.log_path", allow_empty=True
        ),
    )


@dataclass(frozen=True)
class _Surface:
    """The settings the two surfaces have in common."""

    enabled: bool = False
    optional: bool = False
    path: str = ""
    template: str = ""
    template_path: str = ""
    required_text: Tuple[str, ...] = ()
    forbidden_text: Tuple[str, ...] = ()
    extra_tokens: Mapping[str, str] = field(default_factory=dict)
    syntax_check_required: bool = True

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "optional": self.optional,
            "path": self.path,
            "template": self.template,
            "template_path": self.template_path,
            "required_text": list(self.required_text),
            "forbidden_text": list(self.forbidden_text),
            "extra_tokens": dict(self.extra_tokens or {}),
            "syntax_check_required": self.syntax_check_required,
        }


@dataclass(frozen=True)
class FormulaSettings(_Surface):
    """A formula: a source archive, installed out of a tap."""

    install_paths: Tuple[str, ...] = ()
    desc: str = ""
    homepage: str = ""
    license: str = ""
    service: Optional[ServiceMetadata] = None

    def describe(self) -> Dict[str, Any]:
        payload = super().describe()
        payload.update(
            {
                "install_paths": list(self.install_paths),
                "desc": self.desc,
                "homepage": self.homepage,
                "license": self.license,
                "service": self.service.describe() if self.service else None,
            }
        )
        return payload


@dataclass(frozen=True)
class CaskSettings(_Surface):
    """A cask: an application bundle, installed into the applications folder."""

    app_name: str = ""
    binary_path: str = ""
    desc: str = ""
    homepage: str = ""
    macos_requirement: str = ""
    signed: bool = False
    notarized: bool = False
    clear_quarantine: bool = False
    quarantine_evidence: str = ""
    load_check_required: bool = False

    def describe(self) -> Dict[str, Any]:
        payload = super().describe()
        payload.update(
            {
                "app_name": self.app_name,
                "binary_path": self.binary_path,
                "desc": self.desc,
                "homepage": self.homepage,
                "macos_requirement": self.macos_requirement,
                "signed": self.signed,
                "notarized": self.notarized,
                "clear_quarantine": self.clear_quarantine,
                "quarantine_evidence": self.quarantine_evidence,
                "load_check_required": self.load_check_required,
            }
        )
        return payload


@dataclass(frozen=True)
class HomebrewSettings:
    """One tap, and which of its two surfaces this project publishes."""

    token: str
    destination: Destination
    enabled: bool = True
    branch_prefix: str = "continuum/homebrew"
    formula: FormulaSettings = field(default_factory=FormulaSettings)
    cask: Optional[CaskSettings] = None

    @property
    def class_name(self) -> str:
        """The name Homebrew turns the token into: `my-project` -> `MyProject`."""

        return require_token(self.token, "homebrew.token")

    def surface(self, name: str) -> Optional[_Surface]:
        if name == SURFACE_FORMULA:
            return self.formula
        if name == SURFACE_CASK:
            return self.cask
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{name!r} is not a Homebrew surface; expected one of {', '.join(SURFACES)}",
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "publisher": PUBLISHER_NAME,
            "enabled": self.enabled,
            "token": self.token,
            "class_name": self.class_name,
            "destination": self.destination.describe(),
            "branch_prefix": self.branch_prefix,
            "formula": self.formula.describe(),
            "cask": self.cask.describe() if self.cask else None,
        }


_SURFACE_KEYS = (
    "enabled",
    "optional",
    "path",
    "template",
    "template_path",
    "required_text",
    "forbidden_text",
    "tokens",
    "syntax_check_required",
)

_FORMULA_KEYS = _SURFACE_KEYS + ("install_paths", "desc", "homepage", "license", "service")

_CASK_KEYS = _SURFACE_KEYS + (
    "app_name",
    "binary_path",
    "desc",
    "homepage",
    "macos_requirement",
    "signed",
    "notarized",
    "clear_quarantine",
    "quarantine_evidence",
    "load_check_required",
)

_HOME_BREW_KEYS = ("enabled", "token", "destination", "branch_prefix", "formula", "cask")


def _text_tuple(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be a non-empty list")
    return tuple(require_text(item, f"{where}[{index}]") for index, item in enumerate(value))


def _optional_path(value: Any, where: str) -> str:
    """A path inside the destination, or the surface's own convention."""

    if value is None:
        return ""
    return require_relative_path(value, where)


def _url_or_empty(value: Any, where: str) -> str:
    """A homepage that only the built-in template needs.

    Required at *generation* time, not at parse time: a consumer who supplies
    their own template may fill the homepage from a token or not have one, and
    refusing their configuration for a field their template never reads would be
    validation for validation's sake.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return require_url(value, where)


def _optional_text(value: Any, where: str, default: str, *, maximum: int = 100) -> str:
    """A setting the publisher only consults in one mode.

    `branch_prefix` names the branch a pull request lands on, so a trusted-mode
    destination never reads it. Requiring it there would make the common case
    — commit straight to the tap — fail on a setting it has no use for.
    """

    if value is None:
        return default
    return require_text(value, where, maximum=maximum)


def _parse_common(mapping: Mapping[str, Any], where: str) -> Dict[str, Any]:
    return {
        "enabled": require_bool(mapping.get("enabled"), f"{where}.enabled", True),
        "optional": require_bool(mapping.get("optional"), f"{where}.optional", False),
        "path": _optional_path(mapping.get("path"), f"{where}.path"),
        "template": require_block_text(mapping.get("template"), f"{where}.template")
        if mapping.get("template")
        else "",
        "template_path": _optional_path(
            mapping.get("template_path"), f"{where}.template_path"
        ),
        "required_text": _text_tuple(mapping.get("required_text"), f"{where}.required_text"),
        "forbidden_text": _text_tuple(mapping.get("forbidden_text"), f"{where}.forbidden_text"),
        "extra_tokens": extra_tokens(
            require_mapping_of_text(mapping.get("tokens"), f"{where}.tokens"), f"{where}.tokens"
        ),
        "syntax_check_required": require_bool(
            mapping.get("syntax_check_required"), f"{where}.syntax_check_required", True
        ),
    }


def _parse_formula(value: Any, where: str) -> FormulaSettings:
    mapping = require_mapping(value, where)
    reject_unknown(mapping, _FORMULA_KEYS, where)
    common = _parse_common(mapping, where)
    install_paths = _text_tuple(mapping.get("install_paths"), f"{where}.install_paths")
    return FormulaSettings(
        **common,
        install_paths=tuple(
            require_manifest_path(item, f"{where}.install_paths[{index}]")
            for index, item in enumerate(install_paths)
        ),
        desc=require_manifest_string(
            mapping.get("desc"), f"{where}.desc", maximum=300, allow_empty=True
        ),
        homepage=_url_or_empty(mapping.get("homepage"), f"{where}.homepage"),
        license=require_manifest_string(
            mapping.get("license"), f"{where}.license", maximum=200, allow_empty=True
        ),
        service=_parse_service(mapping.get("service"), f"{where}.service"),
    )


def _parse_cask(value: Any, where: str) -> CaskSettings:
    mapping = require_mapping(value, where)
    reject_unknown(mapping, _CASK_KEYS, where)
    common = _parse_common(mapping, where)
    return CaskSettings(
        **common,
        app_name=require_manifest_string(
            mapping.get("app_name"), f"{where}.app_name", maximum=200, allow_empty=True
        ),
        binary_path=require_manifest_string(
            mapping.get("binary_path"), f"{where}.binary_path", allow_empty=True
        ),
        desc=require_manifest_string(
            mapping.get("desc"), f"{where}.desc", maximum=300, allow_empty=True
        ),
        homepage=_url_or_empty(mapping.get("homepage"), f"{where}.homepage"),
        macos_requirement=require_manifest_string(
            mapping.get("macos_requirement"), f"{where}.macos_requirement", maximum=40, allow_empty=True
        ),
        signed=require_bool(mapping.get("signed"), f"{where}.signed", False),
        notarized=require_bool(mapping.get("notarized"), f"{where}.notarized", False),
        clear_quarantine=require_bool(
            mapping.get("clear_quarantine"), f"{where}.clear_quarantine", False
        ),
        # Deliberately not a summary. A justification that fits in a word is not
        # a justification; what a reviewer needs is which artifact cannot pass the
        # check and why the attribute is set on it, and that is a sentence.
        quarantine_evidence=require_text(
            mapping.get("quarantine_evidence"),
            f"{where}.quarantine_evidence",
            maximum=1000,
            allow_empty=True,
        ),
        load_check_required=require_bool(
            mapping.get("load_check_required"), f"{where}.load_check_required", False
        ),
    )


def parse_settings(value: Any, where: str = "homebrew") -> HomebrewSettings:
    """Validate one publisher's configuration.

    Parsing happens here rather than in the shared release schema so a project
    can describe its tap without the release configuration growing a section per
    package manager. An absent surface is a disabled surface: there is no
    default that knows whether a given release has a bundle.
    """

    mapping = require_mapping(value, where)
    reject_unknown(mapping, _HOME_BREW_KEYS, where)
    token = require_text(mapping.get("token"), f"{where}.token", maximum=64)
    require_token(token, f"{where}.token")
    cask_value = mapping.get("cask")
    return HomebrewSettings(
        enabled=require_bool(mapping.get("enabled"), f"{where}.enabled", True),
        token=token,
        destination=parse_destination(
            mapping.get("destination"), f"{where}.destination", default_mode="trusted"
        ),
        branch_prefix=_optional_text(
            mapping.get("branch_prefix"), f"{where}.branch_prefix", "continuum/homebrew"
        ),
        formula=(
            _parse_formula(mapping["formula"], f"{where}.formula")
            if mapping.get("formula") is not None
            else FormulaSettings()
        ),
        cask=_parse_cask(cask_value, f"{where}.cask") if cask_value is not None else None,
    )


# -- built-in templates -----------------------------------------------------


def _service_stanza(service: Optional[ServiceMetadata]) -> str:
    """The `service do` block, or nothing.

    Only the keys Homebrew's own service block accepts are emitted. It has no
    label key — the plist name is derived — so a launchd label cannot be set
    from here, and a project that needs a specific plist supplies its own
    template. Recording the label in a comment is the honest form of that: the
    configuration still has to state it, and the generated file still says which
    identity the service block is expected to produce, without pretending the
    block can be told to produce it.

    Empty when the project describes no daemon, because a service block with no
    label registers a launchd job that cannot be uninstalled by name.
    """

    if service is None:
        return ""
    lines = [
        f"  # service label: {service.label}",
        "  service do",
        f"    run_at_load {'true' if service.run_at_load else 'false'}",
        f"    keep_alive {'true' if service.keep_alive else 'false'}",
    ]
    if service.working_dir:
        lines.append(f'    working_dir "{service.working_dir}"')
    if service.log_path:
        lines.append(f'    log_path "{service.log_path}"')
    lines.append("  end")
    return "\n".join(lines)


def _architecture_block(artifact: PublishedArtifact) -> List[str]:
    return [f'    url "{artifact.url}"', f'    sha256 "{artifact.sha256}"']


def default_formula_template(
    manifest: ArtifactManifest, settings: FormulaSettings, token: str
) -> str:
    """The built-in formula, assembled from the release and the configuration.

    Architecture blocks appear only for architectures the release actually
    published. A formula naming an architecture it has no asset for would carry
    a URL that 404s on one machine and a checksum that fails on every machine, so
    the shape of this template is decided by the manifest rather than by
    optimism about which slices a consumer might want.
    """

    release = manifest.release
    per_arch = tuple(
        artifact for artifact in manifest.select(KIND_ARCHIVE) if artifact.architecture
    )
    universal = manifest.unversioned(KIND_ARCHIVE)

    missing = [
        name
        for name, value in (
            ("desc", settings.desc),
            ("homepage", settings.homepage),
            ("license", settings.license),
        )
        if not value
    ]
    if missing:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"the built-in formula template needs {', '.join(missing)}: the package "
            "manager's own audit rejects a formula that omits them. Supply them in "
            "the settings, or provide a template of your own",
        )
    if not settings.install_paths:
        raise PublisherError(
            CONFIGURATION_INVALID,
            "the built-in formula template needs install_paths: a formula with no "
            "install method creates an empty installation and still reports success. "
            "Supply the paths, or provide a template of your own",
        )

    lines = [
        f"# Generated by Continuum for {release.repository} {release.tag}.",
        "# This file is rewritten on every release; change its template, not this file.",
        f"class {require_token(token, 'homebrew.token')} < Formula",
        f'  desc "{settings.desc}"',
        f'  homepage "{settings.homepage}"',
    ]
    for artifact in per_arch:
        if artifact.architecture not in (ARCH_ARM64, ARCH_X86_64):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"the built-in formula template has no block for architecture "
                f"{artifact.architecture!r}; supply a template that does",
            )
        opener = "on_arm" if artifact.architecture == ARCH_ARM64 else "on_intel"
        lines += ["", f"  {opener} do", *_architecture_block(artifact), "  end"]
    if not per_arch:
        if universal is None:
            raise PublisherError(
                NO_ARTIFACT,
                "the built-in formula template needs exactly one source archive, or one "
                "per architecture, and this release published "
                f"{len(manifest.select(KIND_ARCHIVE))} archive(s) that fit neither shape",
            )
        lines += [f'  url "{universal.url}"', f'  sha256 "{universal.sha256}"']
    lines += [
        f'  version "{release.version}"',
        f'  license "{settings.license}"',
        "",
        f"  def install\n    bin.install {', '.join(f'\"{path}\"' for path in settings.install_paths)}\n  end",
    ]
    service = _service_stanza(settings.service)
    if service:
        lines += ["", service]
    lines.append("end")
    return "\n".join(lines) + "\n"


def _quarantine_stanza(app_name: str) -> str:
    """The compatibility workaround, in the only shape that is allowed.

    Note `{{appdir}}`: that is Homebrew's own expansion, and it is why this
    module's tokens are spelled `__UPPER_CASE__` rather than with braces.
    """

    return "\n".join(
        [
            "  # Quarantine is cleared for this one attribute, on this one bundle.",
            "  # Gatekeeper itself is untouched: this is a compatibility step for an",
            "  # artifact no authority has vouched for, not a way around the check.",
            "  postflight_steps do",
            f'    run "{XATTR_TOOL}", args: [',
            '      "-dr",',
            f'      "{QUARANTINE_ATTRIBUTE}",',
            f'      "{{{{appdir}}}}/{app_name}"',
            "    ]",
            "  end",
        ]
    )


def default_cask_template(settings: CaskSettings, token: str) -> str:
    """The built-in cask, assembled from the configuration alone.

    The quarantine stanza is emitted only when the configuration asked for it
    *and* said why. It is not on by default and cannot be switched on for a
    signed or notarized artifact, so the step stays something a maintainer chose
    rather than something the publisher assumed on their behalf.
    """

    missing = [
        name
        for name, value in (
            ("app_name", settings.app_name),
            ("desc", settings.desc),
            ("homepage", settings.homepage),
        )
        if not value
    ]
    if missing:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"the built-in cask template needs {', '.join(missing)}: a cask without an "
            "app or a homepage describes nothing a user can install or read about. "
            "Supply them in the settings, or provide a template of your own",
        )
    lines = [
        f"cask \"{token}\" do",
        f'  desc "{settings.desc}"',
        f'  homepage "{settings.homepage}"',
        f'  app "{settings.app_name}"',
    ]
    if settings.binary_path:
        lines.append(f'  binary "{settings.binary_path}"')
    lines += [
        '  version "__VERSION__"',
        '  sha256 "__SHA256__"',
        '  url "__URL__"',
    ]
    if settings.macos_requirement:
        lines.append(f'  depends_on macos: "{settings.macos_requirement}"')
    if settings.clear_quarantine:
        lines += ["", _quarantine_stanza(settings.app_name)]
    lines.append("end")
    return "\n".join(lines) + "\n"


# -- quarantine policy ------------------------------------------------------


def enforce_quarantine_policy(cask: CaskSettings, where: str = "homebrew.cask") -> None:
    """Refuse the compatibility workaround in the two cases that are not about compatibility.

    Exported because the check has to be possible to run on its own: a project
    that supplies its own cask template and therefore bypasses the built-in one
    still has to clear this before its cask is written.
    """

    if not cask.clear_quarantine:
        return
    if cask.notarized or cask.signed:
        vouched = "notarized" if cask.notarized else "signed"
        raise PublisherError(
            QUARANTINE_POLICY,
            f"{where}.clear_quarantine is set, but the cask describes a {vouched} "
            "artifact. Removing the quarantine attribute from an artifact the platform "
            "has already vetted discards the only evidence that it was vetted, and the "
            "notarization ticket does not come back",
            remediation=(
                f"leave clear_quarantine off for a {vouched} artifact. If the artifact "
                "really is unvetted, describe it that way in the settings rather than "
                "claiming a signature that is not there."
            ),
        )
    if not cask.quarantine_evidence.strip():
        raise PublisherError(
            QUARANTINE_POLICY,
            f"{where}.clear_quarantine needs quarantine_evidence: a workaround that "
            "removes a platform's warning from every user's machine has to say which "
            "artifact cannot pass the check and why the attribute is set on it",
            remediation=(
                "set quarantine_evidence to the sentence you would give a reviewer who "
                "asks why a release is disabling Gatekeeper for its users"
            ),
        )


def quarantine_audit(generated: GeneratedFile) -> ValidationReport:
    """Check that the quarantine handling in a generated cask is what was asked for.

    Both claims are things a template can break silently, which is why they are
    checked rather than assumed. Comments are stripped before the scan so a cask
    that explains itself in prose is not failed for the prose.
    """

    body = "\n".join(
        line for line in generated.content.splitlines() if not _COMMENT_RE.match(line)
    )
    calls = _XATTR_CALL_RE.findall(body)
    unnamed = [call for call in calls if QUARANTINE_ATTRIBUTE not in call]
    blanket = [call for call in calls if any(flag in call for flag in BLANKET_ATTRIBUTE_FLAGS)]
    disabled = all(word in body for word in GATEKEEPER_OVERRIDE)
    checks = (
        CheckOutcome(
            name="quarantine-attribute",
            status=CHECK_PASSED if not unnamed else CHECK_FAILED,
            detail=(
                f"every xattr call names {QUARANTINE_ATTRIBUTE}"
                if not unnamed
                else f"an xattr call does not name {QUARANTINE_ATTRIBUTE}: "
                f"{unnamed[0].strip()[:120]}"
            ),
        ),
        CheckOutcome(
            name="quarantine-scope",
            status=CHECK_PASSED if not blanket else CHECK_FAILED,
            detail=(
                "no xattr call removes every extended attribute"
                if not blanket
                else f"an xattr call removes every attribute: {blanket[0].strip()[:120]}"
            ),
        ),
        CheckOutcome(
            name="gatekeeper-override",
            status=CHECK_PASSED if not disabled else CHECK_FAILED,
            detail=(
                "the cask does not touch the Gatekeeper override"
                if not disabled
                else "the cask invokes spctl master-disable; switching the check off is "
                "not a compatibility workaround",
            ),
        ),
    )
    return ValidationReport(surface=generated.surface, path=generated.path, checks=checks)


# -- generation -------------------------------------------------------------


def _read_template(surface: _Surface, where: str, source_root: str) -> str:
    if surface.template and surface.template_path:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} sets both template and template_path; a template has one source",
        )
    if surface.template:
        return surface.template
    if not surface.template_path:
        return ""
    path = os.path.join(source_root, surface.template_path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where}.template_path could not be read: {exc}"
        ) from None


def _token_values(
    manifest: ArtifactManifest,
    selected: Tuple[PublishedArtifact, ...],
    surface: _Surface,
    *,
    class_name: str,
) -> Dict[str, Optional[str]]:
    """Every token this publisher defines for one surface.

    The unsuffixed `URL`/`SHA256`/`NAME` come from the only artifact that is not
    split by architecture, or from the first selected one when a consumer's
    template is a single-architecture cask. A template that asks for a slice the
    release did not publish gets no value at all rather than a value borrowed
    from the other slice.
    """

    release = manifest.release
    values: Dict[str, Optional[str]] = {
        "TOKEN": class_name.lower(),
        "CLASS": class_name,
        "VERSION": release.version,
        "TAG": release.tag,
        "REPOSITORY": release.repository,
        "DOWNLOAD_BASE": release.download_base,
    }
    for artifact in selected:
        suffix = artifact.token_architecture
        values[f"NAME_{suffix}" if suffix else "NAME"] = artifact.name
        values[f"URL_{suffix}" if suffix else "URL"] = artifact.url
        values[f"SHA256_{suffix}" if suffix else "SHA256"] = artifact.sha256
    for name, value in (surface.extra_tokens or {}).items():
        values.setdefault(name, value)
    return values


def _generate(
    manifest: ArtifactManifest,
    surface: _Surface,
    kind: str,
    surface_name: str,
    default_path: str,
    settings: HomebrewSettings,
    source_root: str,
    built_in,
) -> GeneratedFile:
    selected = manifest.select(kind)
    if not selected:
        raise PublisherError(
            NO_ARTIFACT,
            f"the release published no {kind}, so there is no {surface_name} to "
            f"generate; a {surface_name} that pins nothing installs nothing and reports "
            "success",
        )
    path = surface.path or default_path
    where = f"homebrew.{surface_name}"
    template = _read_template(surface, where, source_root)
    if template:
        content = render_template(
            template, _token_values(manifest, selected, surface, class_name=settings.class_name),
            where=path,
        )
    else:
        content = render_template(
            built_in(manifest, surface), _token_values(manifest, selected, surface,
                                                       class_name=settings.class_name),
            where=path,
        )
    return GeneratedFile(
        path=path,
        content=content,
        surface=surface_name,
        selected=selected,
        required_text=surface.required_text,
        forbidden_text=surface.forbidden_text,
    )


def _reports(
    generated: GeneratedFile,
    manifest: ArtifactManifest,
    settings: HomebrewSettings,
    command_runner: Optional[CommandRunner],
) -> ValidationReport:
    surface = settings.surface(generated.surface)
    reports = [audit(generated, manifest)]
    if surface is not None and surface.syntax_check_required:
        reports.append(
            syntax_check(generated, argv=RUBY_SYNTAX, command_runner=command_runner, required=True)
        )
    if generated.surface == SURFACE_CASK:
        reports.append(quarantine_audit(generated))
    return combine(*reports)


# -- planning ---------------------------------------------------------------


def plan(
    request: PublishRequest,
    *,
    fetcher: AssetFetcher,
    command_runner: Optional[CommandRunner] = None,
    source_root: str = ".",
) -> Tuple[GeneratedFile, ...]:
    """Every file this publish would write, verified, and not yet written.

    Assets are fetched and checked *before* any metadata is generated. A digest
    is only as good as the bytes behind it, and a formula that pins a checksum
    nobody downloaded is a broken install on somebody else's machine rather than
    a failed publish in this one.
    """

    settings: HomebrewSettings = request.settings
    state = _PlanState(
        settings=settings,
        manifest=request.manifest,
        fetcher=fetcher,
        command_runner=command_runner,
        source_root=source_root,
        surfaces=request.surfaces,
    )
    return state.run()


class _PlanState:
    """The surfaces this run touches, resolved once.

    An enabled cask with no bundle in the manifest is the one case that splits:
    `optional` means the surface silently does not run, and not `optional` means
    it is a failure. Both are decided here, once, so `plan` and `publish` cannot
    disagree about which surfaces existed.
    """

    def __init__(
        self,
        *,
        settings: HomebrewSettings,
        manifest: ArtifactManifest,
        fetcher: AssetFetcher,
        command_runner: Optional[CommandRunner],
        source_root: str,
        surfaces: Tuple[str, ...],
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.fetcher = fetcher
        self.command_runner = command_runner
        self.source_root = source_root
        self.requested = surfaces
        self.active: List[str] = []
        self.absent: List[str] = []
        self.notes: List[str] = []

    def wants(self, name: str, enabled: bool) -> bool:
        if not enabled:
            return False
        return not self.requested or name in self.requested

    def enabled_surfaces(self) -> Tuple[str, ...]:
        """Which surfaces this run was asked for, before any of them ran.

        Reported on a failure as well as a success, so a caller can tell "the
        cask failed" from "the cask was never part of this publish".
        """

        settings = self.settings
        return tuple(
            name
            for name in SURFACES
            if self.wants(
                name,
                settings.cask.enabled
                if name == SURFACE_CASK and settings.cask is not None
                else (settings.formula.enabled if name == SURFACE_FORMULA else False),
            )
        )

    def run(self) -> Tuple[GeneratedFile, ...]:
        settings = self.settings
        generated: List[GeneratedFile] = []

        if self.wants(SURFACE_FORMULA, settings.formula.enabled):
            verify_all(self.manifest.select(KIND_ARCHIVE), self.fetcher)
            generated.append(
                _generate(
                    self.manifest,
                    settings.formula,
                    KIND_ARCHIVE,
                    SURFACE_FORMULA,
                    f"{FORMULA_DIRECTORY}/{settings.token}.rb",
                    settings,
                    self.source_root,
                    lambda manifest, surface: default_formula_template(
                        manifest, surface, settings.token
                    ),
                )
            )
            self.active.append(SURFACE_FORMULA)

        if settings.cask is not None and self.wants(SURFACE_CASK, settings.cask.enabled):
            if not self.manifest.select(KIND_APP_BUNDLE) and settings.cask.optional:
                self.notes.append(
                    "the cask surface is enabled and optional, and this release published "
                    "no application bundle, so no cask was generated"
                )
            else:
                enforce_quarantine_policy(settings.cask)
                verify_all(self.manifest.select(KIND_APP_BUNDLE), self.fetcher)
                generated.append(
                    _generate(
                        self.manifest,
                        settings.cask,
                        KIND_APP_BUNDLE,
                        SURFACE_CASK,
                        f"{CASK_DIRECTORY}/{settings.token}.rb",
                        settings,
                        self.source_root,
                        lambda manifest, surface: default_cask_template(surface, settings.token),
                    )
                )
                self.active.append(SURFACE_CASK)

        for item in generated:
            raise_for(_reports(item, self.manifest, settings, self.command_runner))
        return tuple(generated)


# -- publication ------------------------------------------------------------


def _skip(
    settings: HomebrewSettings,
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
        destination=settings.destination.repository,
        branch=branch or settings.destination.branch,
        message=message,
        notes=notes,
        **extra,
    )


def publish(
    request: PublishRequest,
    repository: PackageRepository,
    *,
    fetcher: AssetFetcher,
    command_runner: Optional[CommandRunner] = None,
    source_root: str = ".",
) -> PublishResult:
    """Write the tap, or say honestly why it was not written."""

    settings: HomebrewSettings = request.settings
    if not settings.enabled:
        return _skip(settings, DISABLED, "the tap publisher is disabled for this project")
    if not settings.formula.enabled and (settings.cask is None or not settings.cask.enabled):
        return _skip(
            settings,
            SURFACE_DISABLED,
            "no surface is enabled: a tap publisher with neither a formula nor a cask has "
            "nothing to publish, and reporting success would leave a version "
            "un-published on a green run",
            surfaces=(),
        )

    state = _PlanState(
        settings=settings,
        manifest=request.manifest,
        fetcher=fetcher,
        command_runner=command_runner,
        source_root=source_root,
        surfaces=request.surfaces,
    )
    surfaces = state.enabled_surfaces()
    try:
        generated = state.run()
    except PublisherError as exc:
        return PublishResult.failure(
            PUBLISHER_NAME,
            exc,
            destination=settings.destination.repository,
            branch=settings.destination.branch,
            surfaces=surfaces,
            notes=tuple(state.notes),
        )

    files = {item.path: item.content for item in generated}
    if not files:
        return _skip(
            settings,
            NO_ARTIFACT,
            "no surface produced a file for this release",
            surfaces=(),
            notes=tuple(state.notes),
        )
    if request.dry_run:
        return _skip(
            settings,
            DRY_RUN,
            "the manifests were generated and validated; nothing was written",
            files=tuple(sorted(files)),
            surfaces=surfaces,
            notes=tuple(state.notes),
        )
    if not credential_present(settings.destination, request.environment):
        return PublishResult.failure(
            PUBLISHER_NAME,
            PublisherError(
                "credential-missing",
                f"the credential for {settings.destination.repository!r} is not available "
                "in this job",
                remediation=(
                    f"expose the repository secret "
                    f"{settings.destination.credential_secret} to this job. A pull "
                    "request from a fork never receives it, which is the correct "
                    "outcome: a fork should not be able to write to a tap."
                ),
            ),
            destination=settings.destination.repository,
            branch=settings.destination.branch,
            surfaces=surfaces,
            notes=tuple(state.notes),
        )

    version = request.manifest.release.version
    branch = (
        settings.destination.feature_branch(settings.branch_prefix, version)
        if settings.destination.mode == MODE_PULL_REQUEST
        else settings.destination.branch
    )
    message = f"chore(release): update {settings.token} to v{version}"
    try:
        commit = reconcile(
            repository,
            lambda _head: files,
            message=message,
            branch=branch,
            attempts=settings.destination.attempts,
        )
    except PublisherError as exc:
        return PublishResult.failure(
            PUBLISHER_NAME,
            exc,
            destination=settings.destination.repository,
            branch=branch,
            surfaces=surfaces,
            notes=tuple(state.notes),
        )

    if not commit.changed:
        return _skip(
            settings,
            ALREADY_CURRENT,
            f"the tap already describes v{version} byte for byte; there is nothing to "
            "commit and no pull request worth opening",
            branch=commit.branch or branch,
            revision=commit.revision,
            files=tuple(sorted(files)),
            surfaces=surfaces,
            notes=tuple(state.notes),
        )

    pull = commit.pull_request
    if settings.destination.mode == MODE_PULL_REQUEST:
        pull = repository.open_pull_request(
            title=message,
            body=(
                f"Automated update of the {settings.token} tap for v{version}. The "
                f"{', '.join(surfaces)} file(s) were regenerated from the published "
                "release, and every asset they pin was downloaded and verified against "
                "the release manifest before these checksums were written."
            ),
            head=branch,
            base=settings.destination.branch,
        )
    return PublishResult(
        publisher=PUBLISHER_NAME,
        status=STATUS_PUBLISHED,
        reason=PUBLISHED,
        destination=settings.destination.repository,
        branch=branch,
        revision=commit.revision,
        pull_request=pull,
        files=tuple(sorted(files)),
        surfaces=surfaces,
        notes=tuple(state.notes),
        message=f"the tap now describes v{version}",
    )


__all__ = [
    "BLANKET_ATTRIBUTE_FLAGS",
    "CASK_DIRECTORY",
    "CaskSettings",
    "FORMULA_DIRECTORY",
    "FormulaSettings",
    "GATEKEEPER_OVERRIDE",
    "HomebrewSettings",
    "PUBLISHER_NAME",
    "QUARANTINE_ATTRIBUTE",
    "RUBY",
    "RUBY_SYNTAX",
    "SURFACE_CASK",
    "SURFACE_FORMULA",
    "SURFACES",
    "ServiceMetadata",
    "XATTR_TOOL",
    "default_cask_template",
    "default_formula_template",
    "enforce_quarantine_policy",
    "parse_settings",
    "plan",
    "publish",
    "quarantine_audit",
]
