"""Filling a consumer's template with the facts a release actually established.

The templates are the consumer's. The filling is not. A repository writes a
formula, a cask, or a Portfile with `__VERSION__` and `__SHA256_ARM64__` in it
and declares which token means which slice of the release; this module turns
that declaration plus a manifest into the finished file. Version, checksum, and
port-revision synchronisation therefore live here rather than in a consumer's
script, which is the whole point: two consumers publishing two different
projects get the same synchronisation because it is the same code.

Two failure modes are handled explicitly, and both of them have shipped
somewhere:

* **An unknown token is an error, not a blank.** A template that asks for
  `__SHA256_X86_64__` when the release published no `x86_64` archive, or that
  asks for a token this publisher has no meaning for, produces a file with a
  hole in it. Rendering it anyway produces a manifest that looks complete and
  fails on a user's machine, so it is refused with the token named.
* **A token value that itself looks like a token is an error.** A value can
  arrive from a repository variable, and a variable containing `__SHA256__`
  would ship that literal text into a published manifest. Substitution is
  single-pass, so the leftover check is the only thing standing between that
  and a broken formula.

The syntax is `__UPPER_CASE__` rather than a brace form because package
manager templates have their own interpolation — a cask expands `{{appdir}}`
itself — and a token syntax that collided with the destination's would make
every template ambiguous.
"""

from __future__ import annotations

import re
from typing import Dict, Mapping, Optional, Tuple

from .contract import (
    CONFIGURATION_INVALID,
    NO_ARTIFACT,
    TEMPLATE_UNKNOWN_TOKEN,
    TEMPLATE_UNFILLED_TOKEN,
    PublisherError,
)

TOKEN_RE = re.compile(r"__([A-Z][A-Z0-9_]{0,63})__")


def tokens_in(template: str) -> Tuple[str, ...]:
    """The token names a template asks for, in the order it first uses them."""

    found: list = []
    for name in TOKEN_RE.findall(template or ""):
        if name not in found:
            found.append(name)
    return tuple(found)


def render(
    template: str,
    values: Mapping[str, Optional[str]],
    *,
    where: str = "template",
) -> str:
    """Fill `template` from `values`.

    A name absent from `values` is a token this publisher does not define, and
    a name present but empty is a token the release could not supply. They are
    different mistakes with different fixes, so they get different codes.
    """

    missing: list = []
    unresolved: list = []

    def substitute(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            missing.append(name)
            return match.group(0)
        value = values[name]
        if value is None or value == "":
            unresolved.append(name)
            return match.group(0)
        return value

    rendered = TOKEN_RE.sub(substitute, template or "")
    if missing:
        raise PublisherError(
            TEMPLATE_UNKNOWN_TOKEN,
            f"{where} asks for token(s) {', '.join(sorted(set(missing)))} that this "
            "publisher does not define; a template cannot be filled with a value "
            "nobody computed",
            remediation=(
                "use one of the tokens this publisher provides, or add the token to "
                "the template source as consumer-owned content"
            ),
        )
    if unresolved:
        raise PublisherError(
            NO_ARTIFACT,
            f"{where} asks for token(s) {', '.join(sorted(set(unresolved)))} and the "
            "release published nothing that fills them",
            remediation=(
                "publish the artifact this token names, or remove the token from the "
                "template. A manifest with an empty checksum is worse than one that "
                "does not exist: it installs and then fails."
            ),
        )
    leftover = tokens_in(rendered)
    if leftover:
        raise PublisherError(
            TEMPLATE_UNFILLED_TOKEN,
            f"{where} still contains unfilled token(s) {', '.join(leftover)} after "
            "rendering; a token value must never carry a token",
            remediation=(
                "check the value substituted for it — it usually came from a "
                "repository variable whose content is not what the template assumes"
            ),
        )
    return rendered


def extra_tokens(extra: Mapping[str, str], where: str) -> Dict[str, str]:
    """Validate consumer-supplied token values before they reach a template.

    Consumer tokens are the escape hatch that keeps a template project-specific
    without the publisher becoming project-specific. They are therefore checked
    as hard as the publisher's own: a value with a newline in it would write a
    second line into a manifest, and a value that is itself a token would leave
    the hole described in the module docstring.
    """

    cleaned: Dict[str, str] = {}
    for key, value in (extra or {}).items():
        name = str(key).strip().upper()
        if not TOKEN_RE.fullmatch(f"__{name}__"):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where} key {key!r} is not an upper-case token name",
            )
        text = "" if value is None else str(value)
        if any(char in text for char in "\n\r\0"):
            raise PublisherError(
                CONFIGURATION_INVALID, f"{where}.{name} must not contain newlines"
            )
        if tokens_in(text):
            raise PublisherError(
                CONFIGURATION_INVALID,
                f"{where}.{name} contains a token; a token value may not be filled by "
                "another token",
            )
        cleaned[name] = text
    return cleaned


__all__ = ["TOKEN_RE", "extra_tokens", "render", "tokens_in"]
