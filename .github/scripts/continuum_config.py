#!/usr/bin/env python3
"""Continuum v0.1 two-toggle configuration contract.

The complete user-facing configuration surface is a single file,
``.github/continuum.yml``, containing a flat top-level mapping of exactly two
boolean keys::

    review: false
    release: false

Both keys default to ``False`` when the file is absent, empty, or omits the
key. A missing configuration is therefore ``review: false, release: false``,
which is the posture a consumer gets by adopting Continuum and doing nothing.

Resolution is pure data with no repository, product, or vendor branching, so
one unchanged copy of this module resolves every consumer. The normative rules
this module implements live in ``docs/continuum-mvp-contract.md``.

Deliberately absent: nesting, inheritance, profiles, environment overrides,
and any third key. Those are contract changes, not options.

Standard library only, so consumers can call this from a pinned checkout
without a dependency install on a security-sensitive path.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence

CONFIG_PATH = ".github/continuum.yml"

#: The entire v0.1 feature surface, in job-summary order.
TOGGLES: Sequence[str] = ("review", "release")

#: No default may be true. A toggle that is on has been asked for explicitly.
DEFAULTS: Dict[str, bool] = {"review": False, "release": False}

_TRUE = "true"
_FALSE = "false"

# A key is a bare identifier. Anything else (quotes, anchors, tags) is not part
# of the contract and is rejected rather than guessed at.
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?P<value>.*)$")

# YAML requires whitespace before an inline comment. Requiring it here too
# keeps ``release: false#x`` a rejected value instead of a silent ``false``.
_INLINE_COMMENT_RE = re.compile(r"\s+#")


class ConfigError(Exception):
    """A configuration file violates the v0.1 contract."""


class Config(NamedTuple):
    """Resolved configuration plus the provenance needed to explain it."""

    review: bool
    release: bool
    source: str
    present: bool

    def as_dict(self) -> Dict[str, str]:
        return {
            "review": str(self.review).lower(),
            "release": str(self.release).lower(),
            "source": self.source,
            "present": str(self.present).lower(),
        }


def _fail(source: str, line_number: int, message: str) -> ConfigError:
    return ConfigError(f"{source}:{line_number}: {message}")


def parse(text: str, source: str = "<string>") -> Dict[str, bool]:
    """Parse contract-conforming YAML text into a toggle mapping.

    Accepts only a flat top-level mapping of bare ``true``/``false`` values.
    Comments and blank lines are allowed. Everything else raises ConfigError.
    """
    values: Dict[str, bool] = {}
    permitted = ", ".join(TOGGLES)

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        body = line.strip()
        if not body or body.startswith("#"):
            continue

        if line[:1].isspace():
            raise _fail(
                source,
                line_number,
                "indented content is not allowed; the v0.1 contract is a flat "
                "top-level mapping of " + permitted,
            )

        match = _KEY_RE.match(body)
        if match is None:
            raise _fail(source, line_number, f"expected `key: value`, got {body!r}")

        key = match.group("key")
        value = _INLINE_COMMENT_RE.split(match.group("value"), maxsplit=1)[0]
        value = value.strip()

        if key not in TOGGLES:
            raise _fail(
                source,
                line_number,
                f"unknown key {key!r}; the v0.1 contract allows only {permitted}. "
                "Unknown keys are rejected rather than ignored so that a typo "
                "cannot silently disable a feature.",
            )
        if key in values:
            raise _fail(source, line_number, f"duplicate key {key!r}")
        if value not in (_TRUE, _FALSE):
            raise _fail(
                source,
                line_number,
                f"{key!r} must be the bare boolean true or false (unquoted), "
                f"got {value!r}",
            )

        values[key] = value == _TRUE

    return values


def resolve(path: str = CONFIG_PATH) -> Config:
    """Resolve the configuration at ``path``.

    An absent file is not an error; it resolves to the documented defaults. A
    path that exists but is not a readable regular file *is* an error, because
    silently falling back to defaults there would be indistinguishable from a
    consumer that deliberately turned both toggles off.
    """
    if not os.path.exists(path):
        return Config(
            review=DEFAULTS["review"],
            release=DEFAULTS["release"],
            source=path,
            present=False,
        )

    if not os.path.isfile(path):
        raise ConfigError(f"{path}: configuration path is not a regular file")

    with open(path, encoding="utf-8") as handle:
        values = parse(handle.read(), path)

    return Config(
        review=values.get("review", DEFAULTS["review"]),
        release=values.get("release", DEFAULTS["release"]),
        source=path,
        present=True,
    )


def _effect(toggle: str, enabled: bool) -> str:
    if toggle == "review":
        if enabled:
            return "Merge waits for the normalized consumer review gate."
        return "Merge waits only for current-head CI and mergeability."
    if enabled:
        return "Merge invokes the consumer release entrypoint."
    return "No release orchestration; no release traffic is generated."


def render_summary(config: Config, ref: Optional[str] = None) -> str:
    """Render the job summary that must accompany every resolution."""
    origin = "file present" if config.present else "file absent - all defaults applied"
    lines: List[str] = [
        "## Continuum configuration (v0.1)",
        "",
        f"- Source: `{config.source}` ({origin})",
        f"- Resolved: `review: {str(config.review).lower()}`, "
        f"`release: {str(config.release).lower()}`",
        "",
        "| Toggle | Value | Effect |",
        "| --- | --- | --- |",
    ]
    for toggle in TOGGLES:
        enabled = getattr(config, toggle)
        lines.append(
            f"| `{toggle}` | `{str(enabled).lower()}` | {_effect(toggle, enabled)} |"
        )
    if ref:
        lines += ["", f"Ref: `{ref}`"]
    return "\n".join(lines) + "\n"


def _write_outputs(destination: Optional[str], values: Dict[str, str]) -> None:
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _write_summary(destination: Optional[str], body: str) -> None:
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(body)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the Continuum v0.1 two-toggle configuration.",
    )
    parser.add_argument(
        "--config",
        default=CONFIG_PATH,
        help=f"configuration file to resolve (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="file to append `review`/`release` step outputs to",
    )
    parser.add_argument(
        "--step-summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="file to append the resolved configuration summary to",
    )
    args = parser.parse_args(argv)

    try:
        config = resolve(args.config)
    except ConfigError as error:
        print(f"::error::{error}", file=sys.stderr)
        print(
            f"{error}\nSee docs/continuum-mvp-contract.md for the v0.1 contract.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        # An unreadable file is not an absent file, and must not be mistaken
        # for the defaults.
        message = f"{args.config}: cannot read configuration: {error}"
        print(f"::error::{message}", file=sys.stderr)
        return 1

    summary = render_summary(config, ref=os.environ.get("GITHUB_SHA"))
    _write_outputs(args.github_output, config.as_dict())
    _write_summary(args.step_summary, summary)
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
