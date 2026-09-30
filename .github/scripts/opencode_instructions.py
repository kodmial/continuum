#!/usr/bin/env python3
"""Install Continuum's global OpenCode instructions into every agent environment.

The gap this closes
-------------------
OpenCode reads instructions from two independent places: the files it finds by
walking up from the working directory, and one global file in its own config
directory. The first is per-repository, so it is the right home for a project's
build, test, release, and architecture rules. The second was, until this module
existed, whatever happened to be on the runner -- normally nothing.

That left every Continuum-managed repository owning half the policy. The rules
that are genuinely universal (the issue is the specification, keep the change
scoped, do not claim success you did not verify, the workflow owns the Git
lifecycle) were either retyped into each repository's `AGENTS.md` or, more
often, simply absent, and a repository that forgot one got an agent run that
disagreed with every other Continuum-managed repository. Continuum is the thing
that decides how its agent is allowed to work; the policy belongs to Continuum.

Why a resolution function instead of a hard-coded path
-----------------------------------------------------
``~/.config/opencode/AGENTS.md`` is the documented location, but it is the
location *under a default environment*. OpenCode derives its global config
directory as ``OPENCODE_CONFIG_DIR`` if that is set, otherwise
``$XDG_CONFIG_HOME/opencode``, otherwise ``<home>/.config/opencode``, where
``<home>`` is itself overridable. A container, a self-hosted runner, or a
caller that exports any of those variables silently moves the file, and a
workflow that writes to the documented path would then install instructions the
agent never reads. That is the worst possible failure: the run looks configured
and the policy is not there.

So :func:`global_instruction_paths` resolves *every* candidate location the
runtime could mean and the installer writes to all of them. Whichever one
OpenCode actually reads, the file is there. This is deliberately not "verify the
environment and pick one": a mis-resolution would be silent, and a
self-hosted runner is exactly the environment nobody inspects.

Why it fails closed
-------------------
Two things here refuse rather than repair themselves:

* **A file Continuum does not own is never overwritten.** If a global
  ``AGENTS.md`` already exists without Continuum's marker, some other owner
  put it there. Overwriting it would delete someone else's configuration;
  ignoring it would leave the agent running under rules that are not
  Continuum's, which is the failure this module exists to prevent. Both are
  wrong, so the run stops and says which path is in the way.
* **A target inside the worktree is refused.** The global layer must never be
  able to become the project layer. If resolution ever produced a path under the
  repository, installing it would silently replace the repository's own
  ``AGENTS.md`` and every project rule in it, so the installer rejects such a
  path instead of writing to it.

Everything is reversible and re-runnable: ``install`` is idempotent, and
``verify`` is the point-of-use gate that turns a missing or replaced global
file into a failed step rather than an unconfigured agent run.

CLI
---
    paths    Print the resolved global instruction paths, one per line.
    install  Write Continuum's global instructions to every resolved path.
    verify   Assert every resolved path holds exactly the source bytes.

Environment:
    OPENCODE_CONFIG_DIR, XDG_CONFIG_HOME, OPENCODE_TEST_HOME, HOME, GITHUB_OUTPUT.

Standard library only, so it runs on stock ``ubuntu-latest`` runners without
dependency installation.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys

#: Identifies a file Continuum wrote, and is what makes ``install`` safe to
#: re-run and what makes "refuse to clobber a foreign file" decidable. It has to
#: be a comment so the file is still a valid instruction document.
MARKER = "<!-- continuum-global-instructions -->"

#: The filename OpenCode looks for. Spelled once so no caller can pass a
#: different one and quietly install instructions nothing will read.
INSTRUCTIONS_FILENAME = "AGENTS.md"

#: The environment variables consulted, in the order OpenCode itself consults
#: them. Exposed so the tests and the documentation cannot drift from the code.
CONFIG_DIR_VAR = "OPENCODE_CONFIG_DIR"
XDG_CONFIG_HOME_VAR = "XDG_CONFIG_HOME"
TEST_HOME_VAR = "OPENCODE_TEST_HOME"
HOME_VAR = "HOME"

OUTPUT_KEY = "instructions_paths"
DIGEST_OUTPUT_KEY = "instructions_sha256"

CHUNK = 1024 * 1024


class InstructionError(Exception):
    """A condition that must stop the run rather than be repaired silently."""


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _clean(value) -> str:
    """An environment value as a path fragment, or empty.

    ``opencode`` treats an empty variable as absent, and so does this: an empty
    string that was concatenated into a path would produce a relative path
    rooted at the worktree, which is precisely the "global file became the
    project file" case the forbidden-root check exists to catch.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    return os.path.normpath(os.path.expanduser(text))


def home_dir(env=None) -> str:
    """The home directory OpenCode would resolve ``~`` to.

    ``OPENCODE_TEST_HOME`` is checked first because OpenCode checks it first: a
    runner that exports it is redirecting every derived path, and a Continuum
    policy file that ignored that would land somewhere the agent never reads.
    """
    environ = os.environ if env is None else env
    for key in (TEST_HOME_VAR, HOME_VAR):
        candidate = _clean(environ.get(key))
        if candidate:
            return candidate
    return _clean(os.path.expanduser("~"))


def global_config_dirs(env=None, home=None) -> list:
    """Every directory OpenCode could read global instructions from.

    Ordered the way OpenCode resolves them, and *not* truncated to the first
    hit. The caller writes to all of them: the cost of an extra copy is one
    small file, and the cost of guessing wrong is an agent that silently runs
    without Continuum's policy.

    ``home`` is a parameter rather than only a lookup so the "nothing at all is
    resolvable" branch is reachable from a test instead of existing only on a
    runner that has been stripped of every path variable.
    """
    environ = os.environ if env is None else env
    candidates = []

    explicit = _clean(environ.get(CONFIG_DIR_VAR))
    if explicit:
        candidates.append(explicit)

    xdg = _clean(environ.get(XDG_CONFIG_HOME_VAR))
    if xdg:
        candidates.append(os.path.join(xdg, "opencode"))

    resolved_home = home_dir(environ) if home is None else _clean(home)
    if resolved_home:
        candidates.append(os.path.join(resolved_home, ".config", "opencode"))

    ordered = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    if not ordered:
        raise InstructionError(
            "No OpenCode global configuration directory can be resolved: none of "
            "{config}, {xdg}, {test_home}, or {home} is set to a usable path. "
            "Continuum cannot install its global instructions, and an agent run "
            "without them is not the run this workflow claims to be."
            .format(
                config=CONFIG_DIR_VAR,
                xdg=XDG_CONFIG_HOME_VAR,
                test_home=TEST_HOME_VAR,
                home=HOME_VAR,
            )
        )
    return ordered


def global_instruction_paths(env=None, home=None) -> list:
    """The full path of every global instructions file Continuum must install."""
    return [
        os.path.join(directory, INSTRUCTIONS_FILENAME)
        for directory in global_config_dirs(env, home=home)
    ]


def is_within(path, root) -> bool:
    """Whether ``path`` is ``root`` or lives underneath it.

    Compared component-wise rather than by string prefix, so ``/repo2`` is not
    inside ``/repo`` -- a prefix check on strings would call a neighbouring
    directory "inside" the worktree and refuse a legitimate target.
    """
    target = pathlib.Path(os.path.normpath(os.path.abspath(str(path)))).parts
    anchor = pathlib.Path(os.path.normpath(os.path.abspath(str(root)))).parts
    if not anchor:
        return False
    return target[: len(anchor)] == anchor


def forbidden_roots(values=None) -> list:
    """Directories a global instructions file may never be written into.

    Defaults to the current working directory, which is the repository the agent
    is about to work in. An explicit ``--forbid-root`` adds to that rather than
    replacing it, because forgetting the flag must not widen the guarantee.
    """
    roots = []
    for value in list(values or []) + [os.getcwd()]:
        cleaned = _clean(value)
        if cleaned and cleaned not in roots:
            roots.append(cleaned)
    return roots


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #


def read_bytes(path) -> bytes:
    try:
        with open(str(path), "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise InstructionError("Cannot read {}: {}".format(path, exc.strerror or exc))


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_source(source) -> bytes:
    """Read and sanity-check the canonical instructions document.

    The marker is required, not decorative. Without it the ownership check
    cannot distinguish Continuum's file from someone else's, and the installer
    would either overwrite a stranger's configuration or refuse to refresh its
    own. A source that is not Continuum's is a packaging mistake, so it is an
    error here rather than a surprise at install time.
    """
    payload = read_bytes(source)
    if not payload.strip():
        raise InstructionError(
            "The canonical Continuum instructions file {} is empty.".format(source)
        )
    if MARKER.encode("utf-8") not in payload:
        raise InstructionError(
            "The canonical Continuum instructions file {} does not carry the "
            "ownership marker {}. Without it Continuum cannot tell its own "
            "instructions from a file another owner put at the global "
            "location, so it would either clobber that file or refuse to "
            "refresh its own.".format(source, MARKER)
        )
    return payload


def check_targets(paths, roots) -> None:
    """Refuse any target that lives inside a protected directory."""
    for path in paths:
        for root in roots:
            if is_within(path, root):
                raise InstructionError(
                    "Refusing to treat {root} as a global OpenCode "
                    "configuration directory: it resolves to {path}, which is "
                    "inside the worktree. Installing there would replace the "
                    "repository's own {name} and every project rule in it."
                    .format(root=root, path=path, name=INSTRUCTIONS_FILENAME)
                )


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


def install(args) -> int:
    payload = read_source(args.source)
    paths = global_instruction_paths()
    roots = forbidden_roots(args.forbid_root)
    check_targets(paths, roots)

    for path in paths:
        existing = None
        if os.path.lexists(path):
            if not os.path.isfile(path) or os.path.islink(path):
                raise InstructionError(
                    "{path} exists and is not a regular file. Continuum will "
                    "not replace it, because it cannot tell what it is.".format(path=path)
                )
            existing = read_bytes(path)
        if existing == payload:
            print("continuum-instructions already current: {}".format(path), file=sys.stderr)
            continue
        if existing is not None and MARKER.encode("utf-8") not in existing:
            raise InstructionError(
                "{path} already exists and was not written by Continuum (it "
                "carries no {marker}). Overwriting it would delete another "
                "owner's global configuration, and leaving it would run the "
                "agent under instructions Continuum does not control. Remove or "
                "reconcile that file, then re-run."
                .format(path=path, marker=MARKER)
            )
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        print("continuum-instructions installed {}".format(path), file=sys.stderr)

    verify_paths(paths, payload)
    publish(paths, digest(payload))
    return 0


def verify(args) -> int:
    payload = read_source(args.source)
    paths = global_instruction_paths()
    roots = forbidden_roots(args.forbid_root)
    check_targets(paths, roots)
    verify_paths(paths, payload)
    publish(paths, digest(payload))
    return 0


def verify_paths(paths, payload) -> None:
    """Every resolved path must hold exactly the canonical bytes.

    Checked rather than assumed, because the failure this guards is invisible:
    an agent that starts with no global instructions does not error, it simply
    behaves like a different Continuum than the one this workflow claims to run.
    """
    expected = digest(payload)
    for path in paths:
        if not os.path.isfile(path):
            raise InstructionError(
                "Continuum's global OpenCode instructions are missing from {}. "
                "The agent would start without them.".format(path)
            )
        actual = digest(read_bytes(path))
        if actual != expected:
            raise InstructionError(
                "Continuum's global OpenCode instructions at {} have digest {}, "
                "but the canonical file has {}. The file was changed, replaced, "
                "or another owner wrote there.".format(path, actual, expected)
            )
    print(
        "continuum-instructions verified {} path(s), sha256 {}".format(len(paths), expected),
        file=sys.stderr,
    )


def show_paths(args) -> int:
    for path in global_instruction_paths():
        print(path)
    return 0


def publish(paths, expected) -> None:
    """Report the resolved paths to stdout and, when present, GITHUB_OUTPUT.

    Both sinks, like ``agent_workspace.py``: a step that only writes one of them
    is invisible to half the callers. GITHUB_OUTPUT is optional here rather than
    required, because this script is also run outside a job -- by its own tests,
    and by a maintainer reproducing a runner environment by hand.
    """
    for path in paths:
        print("{}={}".format(OUTPUT_KEY, path))
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write("{}={}\n".format(OUTPUT_KEY, ",".join(paths)))
        handle.write("{}={}\n".format(DIGEST_OUTPUT_KEY, expected))
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Continuum's global OpenCode instructions"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("paths", help="Print the resolved global instruction paths")
    show.set_defaults(func=show_paths)

    for name, help_text in (
        ("install", "Write Continuum's global instructions to every resolved path"),
        ("verify", "Assert every resolved path holds the canonical bytes"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--source",
            default=os.path.join(".github", "agents", INSTRUCTIONS_FILENAME),
            help="Path to the canonical Continuum instructions document",
        )
        command.add_argument(
            "--forbid-root",
            dest="forbid_root",
            action="append",
            default=[],
            help="Directory the global instructions must never be written into; "
            "repeatable, and the current working directory is always included",
        )
        command.set_defaults(func=install if name == "install" else verify)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InstructionError as exc:
        print("::error::{}".format(" ".join(str(exc).split())), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
