"""A macOS toolchain that exists only in this process.

`AppleAdapter` runs real plans. On a Linux runner there is no `swift`, no
`codesign`, and no `security`, so the adapter takes a `command_runner`: the same
argument vectors, in the same order, against a process boundary that records
what it was asked to do and writes the files the step said it would write.

That is the only thing this module replaces. The plans, the runner's assertions,
the ordering, the manifest building, and the packaging are the real ones, so a
test here asserts on the behaviour of the release and not on the behaviour of a
mock: if the adapter put `codesign` before the copy that assembles the bundle,
the recorded command order would show it.

What the fake does not fake is the part that matters. A `codesign` step here does
not sign anything; instead the fake records the identity it was asked to use and
the fake `verify` step reports success. The identity assertion is real — it is
the adapter's own `assert` step comparing the recorded identity against the
configured one — so a release signed with the wrong identity fails here exactly
as it would on a Mac.

A copy step is not routed here at all: it has no process to start, so the runner
moves those bytes itself, and the files a test unpacks out of a shipped archive
are the bytes the plan copied.
"""

from __future__ import annotations

import os
import shutil
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from continuum.release import apple
from continuum.release.run import CommandResult


class FakeToolchain:
    """The process boundary a plan runs against.

    Every command is appended to `calls` as `(argv, workdir)` so a test can read
    the release's command history. Paths inside an argument are recorded as
    written — relative to the workdir, the way a plan declares them — so a test
    can assert that the plans speak in repository-relative paths and that the
    runner, not the adapter, resolves them.
    """

    def __init__(
        self,
        *,
        binaries: Sequence[str] = (),
        bundle: Optional[str] = None,
        info_plist: Optional[str] = None,
        resources: Sequence[str] = (),
        sign_fails_for: Sequence[str] = (),
        verify_fails: bool = False,
        lipo_fails: bool = False,
        build_fails: bool = False,
        pins_wrong_identity: bool = False,
    ) -> None:
        self.binaries = tuple(binaries)
        self.bundle = bundle
        self.info_plist = info_plist
        self.resources = tuple(resources)
        self.calls: List[Tuple[List[str], str]] = []
        self.sign_fails_for = tuple(sign_fails_for)
        self.verify_fails = verify_fails
        self.lipo_fails = lipo_fails
        self.build_fails = build_fails
        #: Makes `codesign -d` report an authority the adapter never asked for.
        self.pins_wrong_identity = pins_wrong_identity
        #: The identity the fake `codesign` claims to have applied, per file.
        self.signed: Dict[str, str] = {}
        #: What `security import` was handed, as bytes. Recorded rather than the
        #: path because the runner deletes the scratch file as soon as the import
        #: returns, and what matters is the material, not where it landed.
        self.imported: List[bytes] = []
        self.keychains_deleted: List[str] = []

    # -- the command boundary ------------------------------------------------

    def __call__(
        self,
        argv: Sequence[str],
        workdir: str,
        environment: Mapping[str, str],
    ) -> CommandResult:
        tokens = list(argv)
        self.calls.append((tokens, workdir))
        if not tokens:
            return CommandResult(code=0, output="")
        handler = {
            apple.SWIFT: self._swift,
            apple.LIPO: self._lipo,
            apple.SECURITY: self._security,
            apple.CODESIGN: self._codesign,
            apple.PLISTBUDDY: self._plistbuddy,
            apple.MKDIR: self._mkdir,
            apple.REMOVE: self._remove,
        }.get(tokens[0])
        if handler is None:
            return CommandResult(code=127, output=f"{tokens[0]}: not found")
        return handler(tokens, workdir, environment)

    # -- individual tools ----------------------------------------------------

    def _swift(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        if self.build_fails:
            return CommandResult(code=1, output="error: no such module 'Missing'")
        product = argv[argv.index("--product") + 1]
        scratch = argv[argv.index("--scratch-path") + 1]
        # `-Xswiftc -target -Xswiftc <triple>`: the triple is the token after the
        # second `-Xswiftc`.
        triple = argv[argv.index("-target") + 2] if "-target" in argv else "unknown"
        path = self.resolve(scratch, workdir, "release", product)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(f"MACH-O {product}\narch={triple}\n".encode("utf-8"))
        return CommandResult(code=0, output="Build complete!")

    def _lipo(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        if self.lipo_fails:
            return CommandResult(code=1, output="lipo: can't create output file")
        output = argv[argv.index("-output") + 1]
        inputs = argv[argv.index("-create") + 1 : argv.index("-output")]
        path = self.resolve(output, workdir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # `lipo` concatenates the slices, so the merged file carries both of
        # them. A test can then tell a real merge from a copy of one slice.
        merged: List[bytes] = []
        for item in inputs:
            with open(self.resolve(item, workdir), "rb") as handle:
                merged.append(handle.read())
        with open(path, "wb") as handle:
            handle.write(b"\n".join(merged) + b"\nUNIVERSAL\n")
        return CommandResult(code=0, output="")

    def _security(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        if "import" in argv:
            path = self.resolve(argv[argv.index("import") + 1], workdir)
            if not os.path.isfile(path):
                return CommandResult(
                    code=1,
                    output=(
                        "security: SecKeychainItemImport: The specified item could not "
                        "be found in the keychain."
                    ),
                )
            with open(path, "rb") as handle:
                self.imported.append(handle.read())
            return CommandResult(code=0, output="1 identity imported.")
        if "delete-keychain" in argv:
            self.keychains_deleted.append(argv[-1])
        return CommandResult(code=0, output="")

    def _codesign(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        if "--verify" in argv:
            if self.verify_fails:
                return CommandResult(code=1, output="codesign: invalid signature")
            return CommandResult(code=0, output="")
        if "-d" in argv or "--display" in argv:
            return CommandResult(code=0, output=self._display(argv, workdir))
        target = argv[-1]
        identity = argv[argv.index("--sign") + 1] if "--sign" in argv else ""
        if identity in self.sign_fails_for:
            return CommandResult(code=1, output="codesign: errSecInternalComponent")
        path = self.resolve(target, workdir)
        self.signed[path] = identity
        if os.path.isfile(path):
            # Writing the signature into the file is what makes the ordering
            # provable from the archive rather than from this recorder: a test
            # that unpacks the shipped bundle can tell whether the binary inside
            # it was signed before or after it was copied in.
            with open(path, "ab") as handle:
                handle.write(f"SIGNED-BY {identity}\n".encode("utf-8"))
        return CommandResult(code=0, output="")

    def _display(self, argv: Sequence[str], workdir: str) -> str:
        """What `codesign -d` reports about a file.

        Reads back the identity this fake recorded when the file was signed, so
        the adapter's pin step — which is the real assertion, comparing this text
        against the configured identity — is satisfied by having signed it and
        fails when the wrong identity was used. `pins_wrong_identity` exists to
        make that failure observable: a `codesign` that succeeds while applying
        some other certificate is exactly the release the pin exists to catch.
        """

        path = self.resolve(argv[-1], workdir)
        identity = "Somebody Else" if self.pins_wrong_identity else self.signed.get(path, "")
        lines = [f"Executable={path}"]
        if identity:
            lines.append(f"Identifier={path.rsplit('/', 1)[-1]}")
            lines.append(f"Authority={identity}")
            lines.append("TeamIdentifier=not set")
        else:
            lines.append("code object is not signed at all")
        return "\n".join(lines)

    def _plistbuddy(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        """Record each `Set :key value` into the file the plan names.

        Not a plist: the tests only need to ask what version the bundle claims,
        and a file whose keys are readable proves the stamp reached the content
        that the seal then covered.
        """

        path = self.resolve(argv[-1], workdir)
        commands = [argv[index + 1] for index, token in enumerate(argv) if token == "-c"]
        written: Dict[str, str] = {}
        for command in commands:
            if not command.startswith("Set :"):
                return CommandResult(code=1, output=f"Unexpected command {command!r}")
            key, _, value = command[len("Set :") :].partition(" ")
            written[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for key in sorted(written):
                handle.write(f"{key}={written[key]}\n")
        return CommandResult(code=0, output="")

    def _mkdir(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        path = self.resolve(argv[-1], workdir)
        os.makedirs(path, exist_ok=True)
        return CommandResult(code=0, output="")

    def _remove(
        self, argv: Sequence[str], workdir: str, environment: Mapping[str, str]
    ) -> CommandResult:
        for token in argv[1:]:
            if token.startswith("-"):
                continue
            path = self.resolve(token, workdir)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        return CommandResult(code=0, output="")

    # -- assertions ----------------------------------------------------------

    def resolve(self, path: str, workdir: str, *parts: str) -> str:
        base = os.path.join(workdir, path) if path and not os.path.isabs(path) else path
        return os.path.join(base, *parts) if parts else base

    def commands(self, tool: str) -> List[List[str]]:
        """Every argv whose first token is `tool`, in the order it was run."""

        return [argv for argv, _ in self.calls if argv and argv[0] == tool]

    def names(self) -> List[str]:
        """The first token of every command, in order, for ordering assertions."""

        return [argv[0] for argv, _ in self.calls]


def adapter_for(
    directory: str,
    toolchain: FakeToolchain,
    *,
    environment: Optional[Dict[str, str]] = None,
    revision: Optional[str] = None,
):
    """The adapter under test, pointed at a directory and a fake toolchain.

    `revision` is what the fake checkout reports as its HEAD. It defaults to the
    SHA the fixtures release, and passing a different one is how a test asks for
    a checkout that is not the approved commit.
    """

    from . import release_core_support as core
    from . import release_support as support

    return apple.AppleAdapter(
        environment=environment if environment is not None else support.environment(),
        workdir=directory,
        command_runner=toolchain,
        is_available=True,
        git_revision=lambda root: revision if revision is not None else core.SHA,
    )