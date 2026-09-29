#!/usr/bin/env python3
"""Bind an OpenCode invocation to the binary that was verified.

runtime-lab #136
----------------
The optimized install verifies a release binary thoroughly and then makes it
reachable by name:

* the asset is downloaded and its ``.sha256`` sidecar checked;
* the digest is compared against ``opencode_release_sha256`` when pinned;
* ``--version`` is compared against ``opencode_release_version`` when pinned;
* the ``.json`` sidecar's tag, asset, digest and source SHA are cross-checked.

Then ``ln -sf`` puts it on ``PATH`` as ``opencode``, and a later step runs
``opencode``. Between the verification and the execution, though, what runs is
decided by ``PATH`` resolution, not by the verification. ``$GITHUB_PATH`` is
appended, so the verified copy is *last*: any earlier entry containing an
``opencode`` -- a repository file, a cached directory, a tool cache, an install
path a previous step created -- wins. Every check above would still pass, the
summary would still report the verified digest, and the model would be driven by
a binary nobody checked.

That is the difference between "the artifact I fetched is the artifact I ran" and
"the artifact I fetched had the right digest".

What this adds
--------------
Two things the release check does not do:

``install``
    Resolve ``opencode`` on the *resulting* ``PATH`` and record the digest of
    the file that actually resolves, along with the verified reference digest.
    This is the executable that will run, and its digest is the identity that
    gets enforced.

``verify``
    Re-read the digest of the resolved executable and compare it to what
    ``install`` recorded. Called immediately before each invocation.

The recorded identity is written to ``$GITHUB_STATE`` rather than baked into
the job's ``env:``, because a step's ``env:`` is fixed before the step that
produces the value has run. State written to ``$GITHUB_STATE`` by one step is
readable by later steps in the same job, which is the only place a
"measured now, enforced later" pair can live.

The comparison is a digest, not a path or a timestamp. A path check would pass
for a replaced file at the same location, and a timestamp check depends on
filesystem clock granularity. A digest is the only one of the three that is a
statement about the bytes.

Standard library only, so it runs on a stock runner.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import shutil
import sys

#: The minimum identity is a full SHA-256. Nothing shorter is accepted: a
#: truncated digest is a prefix, and a prefix is a value an attacker picks.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CHUNK = 1024 * 1024

#: Where the cross-step record lives. ``GITHUB_STATE`` is the only runner
#: channel whose value is written by one step and read by a later one.
STATE_KEY = "CONTINUUM_OPENCODE_BINARY"


@dataclasses.dataclass(frozen=True)
class RuntimeIdentity:
    """The digest of the executable ``opencode`` currently resolves to."""

    path: str
    sha256: str
    realpath: str

    def as_state(self, delimiter: str) -> str:
        """The record in the heredoc form ``$GITHUB_STATE`` requires.

        ``GITHUB_STATE`` has no escaping for a newline inside a value, exactly
        like ``GITHUB_OUTPUT``. Writing ``key=a\\nb=c`` as two bare lines would
        store two *separate* state entries, and the reader would then have to
        guess which one was the identity -- so the value is written between a
        delimiter, and the delimiter is checked against the value so a record
        containing the delimiter cannot truncate it.
        """
        body = "path={path}\nsha256={digest}".format(
            path=self.path, digest=self.sha256
        )
        if delimiter in body:
            raise VerificationError(
                "The recorded identity contains the state delimiter; refusing "
                "to write a record that cannot be read back."
            )
        return "{key}<<{delimiter}\n{body}\n{delimiter}\n".format(
            key=STATE_KEY, delimiter=delimiter, body=body
        )


class VerificationError(Exception):
    """A condition that must stop the run rather than warn about it."""


def digest_file(path) -> str:
    """SHA-256 of a file, streamed.

    Read in chunks because the thing being hashed is an executable that may be
    tens or hundreds of megabytes, and because a run that loads the whole binary
    to verify it is a run with a different memory profile than the one it is
    checking.
    """
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def resolve_opencode(name: str = "opencode") -> str:
    """The executable ``name`` resolves to on the current ``PATH``.

    Resolved the same way the shell would resolve it at invocation, so what is
    measured is what would be executed. ``shutil.which`` follows ``PATH`` order
    exactly; the symlink is *not* followed here, because the symlink is the
    indirection being audited and the target's bytes are what matter.
    """
    found = shutil.which(name)
    if not found:
        raise VerificationError(
            "No executable named {!r} is reachable on PATH, so the binary that "
            "would run cannot be identified.".format(name)
        )
    if not os.access(found, os.X_OK):
        raise VerificationError("{} is on PATH but is not executable.".format(found))
    return found


def install(args) -> int:
    """Record the identity of the binary that will actually be invoked.

    ``verified_sha256`` is the digest the release check approved. It is compared
    here, against the file ``PATH`` resolves to, and the mismatch is fatal. That
    comparison is the whole point: it closes the gap between "a verified binary
    exists somewhere" and "a verified binary is what runs".
    """
    path = resolve_opencode(args.name)
    actual = digest_file(path)
    identity = RuntimeIdentity(
        path=path, sha256=actual, realpath=os.path.realpath(path)
    )

    reference = str(args.verified_sha256 or "").strip().lower()
    if reference:
        if not SHA256_RE.match(reference):
            raise VerificationError(
                "The verified digest {!r} is not a SHA-256, so it cannot "
                "identify a binary.".format(args.verified_sha256)
            )
        if actual != reference:
            raise VerificationError(
                "The executable PATH resolves to is {path} with digest "
                "{actual}, but the release verification approved {reference}. "
                "Something earlier on PATH shadows the verified binary; "
                "refusing to run an agent under an unverified executable."
                .format(path=path, actual=actual, reference=reference)
            )

    destination = os.environ.get("GITHUB_STATE")
    if not destination:
        raise VerificationError(
            "GITHUB_STATE is not set, so the identity cannot be carried to the "
            "step that enforces it."
        )
    delimiter = "continuum_opencode_{}".format(
        hashlib.sha256(identity.sha256.encode("ascii")).hexdigest()[:12]
    )
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(identity.as_state(delimiter))
        handle.flush()
        os.fsync(handle.fileno())

    print("agent-runtime resolved {} -> {} (sha256 {})".format(
        args.name, identity.realpath, actual
    ), file=sys.stderr)
    return 0


def read_recorded_identity() -> RuntimeIdentity:
    """The identity recorded by ``install``, read from ``$GITHUB_STATE``.

    ``GITHUB_STATE`` is a heredoc file: keys, then a delimiter, then values.
    Reading it back with the same discipline it was written with is what keeps
    a recorded digest from being silently replaced by an empty one.
    """
    path = os.environ.get("GITHUB_STATE")
    if not path or not os.path.exists(path):
        raise VerificationError(
            "No OpenCode binary identity was recorded for this job, so the "
            "executable cannot be checked against one. Run `install` first."
        )

    recorded = {}
    key = None
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            key, _, delimiter = line.partition("<<")
            key = key.strip()
            index += 1
            values = []
            while index < len(lines) and lines[index] != delimiter:
                values.append(lines[index])
                index += 1
            if index >= len(lines):
                raise VerificationError(
                    "The recorded binary identity is truncated in GITHUB_STATE."
                )
            recorded[key] = "\n".join(values)
            index += 1
            continue
        if "=" in line:
            name, _, value = line.partition("=")
            recorded[name.strip()] = value
        index += 1

    body = recorded.get(STATE_KEY)
    if not body:
        # One message for "no state file", "empty state file" and "state file
        # with no such key". They are the same operational fact -- nothing was
        # recorded -- and three different messages for it is how one of them
        # ends up treated as expected.
        raise VerificationError(
            "No OpenCode binary identity was recorded for this job, so the "
            "executable cannot be checked against one. Run `install` first."
        )
    fields = {}
    for line in body.splitlines():
        if "=" in line:
            name, _, value = line.partition("=")
            fields[name.strip()] = value.strip()
    digest = fields.get("sha256", "")
    if not SHA256_RE.match(digest):
        raise VerificationError(
            "The recorded OpenCode identity {!r} is not a SHA-256.".format(digest)
        )
    return RuntimeIdentity(
        path=fields.get("path", ""), sha256=digest, realpath=os.path.realpath(
            fields.get("path", "")
        )
    )


def verify(args) -> int:
    """Re-check that the executable about to run is the one that was verified.

    Called immediately before each invocation rather than once after install,
    because the window this closes is not the install step: it is every step
    between verification and execution, including the agent step itself.
    """
    recorded = read_recorded_identity()
    path = resolve_opencode(args.name)
    actual = digest_file(path)

    if actual != recorded.sha256:
        raise VerificationError(
            "Refusing to run {name}: it now resolves to {path} with digest "
            "{actual}, but {original} was recorded at install time with digest "
            "{recorded}. The executable changed after verification."
            .format(
                name=args.name,
                path=path,
                actual=actual,
                original=recorded.path or recorded.realpath,
                recorded=recorded.sha256,
            )
        )

    print("agent-runtime verified {} (sha256 {})".format(path, actual), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind an OpenCode invocation to the binary that was verified"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    installer = sub.add_parser(
        "install", help="Record the digest of the executable PATH resolves to"
    )
    installer.add_argument("--name", default="opencode")
    installer.add_argument("--verified-sha256", dest="verified_sha256", default="")
    installer.set_defaults(func=install)

    verifier = sub.add_parser(
        "verify", help="Re-check the executable against the recorded identity"
    )
    verifier.add_argument("--name", default="opencode")
    verifier.set_defaults(func=verify)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VerificationError as error:
        print("::error::{}".format(" ".join(str(error).split())), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
