"""The consumer's Continuum release pin.

ADR-0002 makes the Continuum release, not the module, the unit of version
selection. A consumer therefore holds exactly one reference to Continuum, it is
a literal, and it is either an exact SemVer release tag or a full commit SHA.
Everything else -- ``@main``, ``@v0``, ``@v0.1``, a short SHA, a second
reference beside the first -- is a way for a repository to change behaviour
without anyone changing that repository.

This module is the whole of the tooling that reads and writes that reference.
It is deliberately small, pure, and offline:

* :func:`ingress_pins` reports every Continuum reference a file contains.
* :func:`require_single_release_pin` turns "more than one" or "not an exact
  release" into an error rather than a warning.
* :func:`set_ref` performs the one-pin change. Upgrade and rollback are the
  same operation with a different argument, which is what makes rollback exactly
  the inverse of an upgrade and makes both mechanically testable.

There is no discovery mode and no background path. Nothing in this module runs
because a newer Continuum release exists; a pin changes only when an operator
asks for that specific change, and asking twice with the same arguments
produces byte-identical text.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Sequence

#: The repository that publishes Continuum releases. A consumer's ingress names
#: it, so it is the one hard-coded value in the whole selection contract.
CONTINUUM_REPOSITORY = "kodmial/continuum"

#: The release entrypoint. ADR-0002 makes this the single file a consumer names;
#: naming any other Continuum workflow is a per-module pin, which is what this
#: module exists to prevent.
RELEASE_ENTRYPOINT = ".github/workflows/consumer.yml"

#: Exact SemVer, with a `v` prefix and no pre-release or build metadata. A
#: prerelease is deliberately not accepted: it is a moving target by another
#: name, and Continuum does not publish one as a production dependency contract.
EXACT_RELEASE = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")

#: A full, unabbreviated commit SHA. GitHub's strictest reference.
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: Any reference Continuum would refuse as a production pin, with the reason.
#:
#: This exists so the error a consumer gets names the actual mistake. "Invalid
#: ref" tells someone reading a workflow file nothing; "v0.1 is a moving
#: compatibility tag" tells them what to change.
FLOATING_REFS = {
    "main": "a branch moves; a consumer must not change because Continuum main did",
    "master": "a branch moves; a consumer must not change because Continuum main did",
    "HEAD": "HEAD is whatever the runner resolved, not a selected version",
}

#: `uses:` lines are read as text on purpose. The property under test is what a
#: reviewer sees in the file, and a parse would silently accept a ref assembled
#: from an anchor or a folded scalar that nobody reads as a version. A trailing
#: comment is allowed, because a version line often carries one, and a comment
#: never contributes a reference. The two whitespace groups are captured so that
#: rewriting the reference can leave the operator's own spacing alone.
USES_LINE = re.compile(
    r"^(?P<indent>\s*)uses:(?P<before>\s*)(?P<value>[^\s#]+)(?P<gap>\s*)(?P<comment>#.*)?$"
)

#: The Continuum repository's own workflows reference each other relatively so
#: that a single consumer pin resolves the whole graph. Such a reference is not
#: a pin: GitHub resolves it from the same commit as the caller.
RELATIVE_PREFIX = "./"


class PinError(ValueError):
    """A file does not hold exactly one usable Continuum release reference."""


class ContinuumPin(NamedTuple):
    """One resolved Continuum reference, as it appears in a workflow file."""

    #: The literal `uses:` value, e.g. `kodmial/continuum/.github/workflows/consumer.yml@v0.1.0`.
    value: str
    #: The referenced path inside the Continuum repository.
    path: str
    #: The literal reference after `@`.
    ref: str
    #: `release` for an exact SemVer tag, `commit` for a full SHA.
    kind: str

    @property
    def version(self) -> str:
        """The SemVer release tag, or the empty string for a commit pin."""
        return self.ref if self.kind == "release" else ""

    @property
    def commit(self) -> str:
        """The full commit SHA, or the empty string for a release pin."""
        return self.ref if self.kind == "commit" else ""


def classify(ref: str) -> str:
    """Return ``release``, ``commit``, or the reason ``ref`` is not acceptable."""

    if EXACT_RELEASE.match(ref):
        return "release"
    if FULL_COMMIT.match(ref):
        return "commit"
    if ref in FLOATING_REFS:
        return FLOATING_REFS[ref]
    if re.match(r"^v\d+(\.\d+)*$", ref):
        return f"{ref!r} is a moving major/minor compatibility tag, not an exact release"
    if re.match(r"^v\d+\.\d+\.\d+[-+]", ref):
        return f"{ref!r} is not a plain exact release tag"
    if re.match(r"^[0-9a-f]{7,39}$", ref):
        return f"{ref!r} is an abbreviated commit; the full 40-character SHA is required"
    if "/" in ref:
        return f"{ref!r} is a remote branch"
    return f"{ref!r} is neither an exact SemVer release tag nor a full commit SHA"


def is_exact_release(ref: str) -> bool:
    return bool(EXACT_RELEASE.match(ref))


def referenced_pins(text: str) -> List[ContinuumPin]:
    """Every reference to the Continuum repository found in ``text``.

    A consumer's ingress must return exactly one. Continuum's own workflows
    return none, because they reach each other with relative references that
    GitHub resolves from the caller's commit.
    """

    pins: List[ContinuumPin] = []
    for line in text.splitlines():
        match = USES_LINE.match(line)
        if match is None:
            continue
        value = match.group("value")
        if value.startswith(RELATIVE_PREFIX):
            continue
        if not value.startswith(CONTINUUM_REPOSITORY + "/"):
            continue
        remainder = value[len(CONTINUUM_REPOSITORY) + 1 :]
        path, separator, ref = remainder.rpartition("@")
        if not separator or not path:
            # `kodmial/continuum/...` with no `@` is not a legal reference at
            # all, and it is not silently ignored.
            pins.append(ContinuumPin(value, remainder, "", "invalid"))
            continue
        pins.append(ContinuumPin(value, path, ref, classify(ref)))
    return pins


def ingress_pins(path: str) -> Sequence[ContinuumPin]:
    """Read ``path`` and return the Continuum references it declares."""

    with open(path, encoding="utf-8") as handle:
        return referenced_pins(handle.read())


def require_single_release_pin(text: str, source: str = "<ingress>") -> ContinuumPin:
    """Return the one exact Continuum release reference in ``text``.

    Zero references, more than one reference, a reference to a workflow other
    than the release entrypoint, and a reference that is not an exact release or
    a full commit are all errors. The per-module case matters as much as the
    floating case: two references agreeing today is still a consumer whose
    implementation version is decided in two places.
    """

    pins = referenced_pins(text)
    if not pins:
        raise PinError(
            f"{source} selects no Continuum release. The generated ingress must "
            f"call {CONTINUUM_REPOSITORY}/{RELEASE_ENTRYPOINT} at an exact "
            "release tag, for example @v0.1.0."
        )
    if len(pins) > 1:
        found = ", ".join(sorted(pin.value for pin in pins))
        raise PinError(
            f"{source} holds {len(pins)} Continuum references ({found}). ADR-0002 "
            "makes the release, not the module, the unit of selection: one "
            "reference selects the whole control plane, and a second one can "
            "only disagree with the first."
        )

    pin = pins[0]
    if pin.kind not in ("release", "commit"):
        raise PinError(
            f"{source} pins Continuum at {pin.ref!r}, which is not an exact "
            f"release: {pin.kind}."
        )
    if pin.path != RELEASE_ENTRYPOINT:
        raise PinError(
            f"{source} names {pin.path} instead of {RELEASE_ENTRYPOINT}. Naming "
            "a single Continuum workflow is a per-module pin; the release "
            "entrypoint is what selects the coherent control plane."
        )
    return pin


def set_ref(text: str, ref: str, source: str = "<ingress>") -> str:
    """Return ``text`` with its single Continuum reference moved to ``ref``.

    This is the whole of an upgrade and the whole of a rollback. The change is
    one line, the target is validated before anything is written, and no other
    byte of the file moves -- which is what makes applying it and then
    reverting it return the original text exactly.

    Only the `uses:` line that declares the pin is rewritten. A comment that
    shows the same string as an example is documentation, not a second pin, and
    silently editing it would make the file's own explanation drift away from
    the version it is explaining.
    """

    pin = require_single_release_pin(text, source)
    kind = classify(ref)
    if kind not in ("release", "commit"):
        raise PinError(f"refusing to pin Continuum at {ref!r}: {kind}.")

    new = f"{CONTINUUM_REPOSITORY}/{pin.path}@{ref}"
    rewritten: List[str] = []
    replaced = 0
    for line in text.splitlines(keepends=True):
        match = USES_LINE.match(line.rstrip("\r\n"))
        if match is not None and match.group("value") == pin.value:
            ending = line[len(line.rstrip("\r\n")) :]
            # Everything except the reference itself is reproduced exactly, so
            # the operator's indentation, alignment, and trailing comment come
            # back unchanged and the diff is one token wide.
            rendered = (
                f"{match.group('indent')}uses:{match.group('before')}{new}"
                f"{match.group('gap')}{match.group('comment') or ''}"
            )
            rewritten.append(rendered + ending)
            replaced += 1
            continue
        rewritten.append(line)

    if replaced != 1:
        raise PinError(
            f"{source} has {replaced} reference lines for {pin.value!r}; refusing "
            "to write a file that cannot be verified afterwards."
        )
    return "".join(rewritten)


def upgrade(text: str, target: str, source: str = "<ingress>") -> str:
    """Move the single reference to ``target``.

    Named separately from :func:`set_ref` so the call site records intent: an
    upgrade is a deliberate move forward chosen by an operator, and the
    argument is the release they chose.
    """

    if classify(target) not in ("release", "commit"):
        raise PinError(f"refusing to upgrade Continuum to {target!r}: {classify(target)}.")
    return set_ref(text, target, source)


def rollback(text: str, previous: str, source: str = "<ingress>") -> str:
    """Restore the single reference to ``previous``.

    The inverse of :func:`upgrade` and nothing more: there is no separate
    rollback state to keep in step, so an operator rolling back one release is
    doing exactly the edit an upgrade was.
    """

    if classify(previous) not in ("release", "commit"):
        raise PinError(
            f"refusing to roll Continuum back to {previous!r}: {classify(previous)}."
        )
    return set_ref(text, previous, source)
