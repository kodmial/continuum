#!/usr/bin/env python3
"""The one definition of "a complete Continuum engine checkout".

Every Continuum surface a consumer can reach loads its engine the same way: the
caller pinned one literal release reference, GitHub resolved it, and the commit
of the calling workflow file is the engine commit. This module is the single
place that decides whether the checkout that resulted is usable, so a partial
engine fails here rather than part way through a privileged run with half the
policy missing.

It exists as a script rather than as a composite action on purpose. GitHub
downloads a remote action into its own directory, so a composite action could
only ever see the copy GitHub made of it and not the checkout the next steps are
about to import. The assertion has to run *out of* the checkout it is judging.

Subcommands:

    assert    verify a checkout is the expected commit and is complete
    pin       report the release reference a consumer pin must resolve to
    require   fail unless a workflow carries exactly one expected Continuum pin

Run with::

    python3 .github/scripts/continuum_engine.py assert --root engine
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

#: The only repository whose code a consumer is allowed to select. A surface
#: reachable from a consumer must be part of this repository, or the single
#: release pin is not selecting the whole graph.
ENGINE_REPOSITORY = "kodmial/continuum"

#: The one release entrypoint a consumer is allowed to name. ADR-0002 makes the
#: top-level ``consumer.yml`` the unit of distribution for the entire
#: implementation, so any other Continuum workflow in a consumer's own files is a
#: per-module pin that can drift away from the selected release.
RELEASE_ENTRYPOINT = "consumer.yml"

#: The path of the single external reference a generated consumer ingress holds.
RELEASE_REFERENCE = "{}/.github/workflows/{}".format(ENGINE_REPOSITORY, RELEASE_ENTRYPOINT)

#: An exact SemVer release tag, which is what ADR-0002 makes the standard
#: human-facing pin, or a full commit SHA, which it keeps available as the
#: stricter reference for high-assurance consumers.
PIN_PATTERN = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$|^[0-9a-f]{40}$")

#: A moving target. A branch or a floating compatibility tag would let a
#: consumer's behaviour change without anyone changing the consumer.
FLOATING_PINS = ("main", "master", "v0", "v1", "develop", "HEAD")

#: The files that together make up a runnable engine. Each one exists because
#: some surface imports or executes it, and a checkout missing any of them
#: either fails on a later step or, worse, runs without the policy it needs.
REQUIRED_PATHS = (
    ".github/scripts/agent_workspace.py",
    ".github/scripts/conflict_repair.py",
    ".github/scripts/continuum_config.py",
    ".github/scripts/continuum_engine.py",
    ".github/scripts/delegation_repository.sh",
    ".github/scripts/failure_retry.py",
    ".github/scripts/opencode_instructions.py",
    ".github/scripts/opencode_runtime.py",
    ".github/scripts/trust_policy.py",
    # The global instructions document and its installer travel together. A
    # consumer workflow installs one from the other, so a partial engine
    # checkout has to fail here rather than half way through an agent run.
    ".github/agents/AGENTS.md",
    # The review and release entrypoints run the engine package itself, not
    # only the shell helpers above.
    "src/continuum/cli.py",
    "src/continuum/review/gate.py",
    "src/continuum/release/core.py",
)

#: The `uses:` lines a generated consumer ingress may contain. Exactly one of
#: them, and it is the release entrypoint. The trailing match stops at the line
#: break rather than swallowing it, so a rewrite leaves the rest of the file
#: byte for byte identical.
CONSUMER_REFERENCE = re.compile(
    r"^\s*uses:\s*"
    + re.escape(ENGINE_REPOSITORY)
    + r"/\.github/workflows/([A-Za-z0-9._-]+)@(\S+)[^\S\n]*$",
    re.MULTILINE,
)


class EngineError(RuntimeError):
    """A checkout or a pin that must not be executed as-is."""


def is_pinnable(pin: str) -> bool:
    """True for an exact release tag or a full commit SHA, and nothing else."""
    return bool(PIN_PATTERN.match(pin or ""))


def is_floating(pin: str) -> bool:
    """True for a ref whose target can move without a consumer change."""
    return (pin or "") in FLOATING_PINS or not is_pinnable(pin)


def checkout_commit(root: pathlib.Path) -> str:
    """The commit the checkout is actually at, read from git rather than assumed."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise EngineError(
            "the engine checkout at {} is not a git commit".format(root)
        )
    return head


def missing_paths(root: pathlib.Path) -> list:
    """The required engine files this checkout does not have."""
    return [name for name in REQUIRED_PATHS if not (root / name).is_file()]


def consumer_pins(text: str) -> list:
    """Every Continuum reference a generated consumer ingress declares.

    Returned as ``(workflow, pin)`` pairs in file order, so a caller can both
    count them and compare their pins.
    """
    return [(match.group(1), match.group(2)) for match in CONSUMER_REFERENCE.finditer(text)]


def assert_engine(root: pathlib.Path, expect_sha: str = "") -> str:
    """Verify the checkout, returning its commit.

    ``expect_sha`` is what the caller resolved from the pinned reference. It is
    compared rather than trusted: a checkout that is not the commit the
    workflow file came from means the workflow graph and the code it runs are
    from different Continuum revisions, which is the exact failure one release
    pin exists to make impossible.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise EngineError("no engine checkout at {}".format(root))
    missing = missing_paths(root)
    if missing:
        raise EngineError(
            "the engine checkout at {} is incomplete; missing {}".format(
                root, ", ".join(missing)
            )
        )
    head = checkout_commit(root)
    if expect_sha and head != expect_sha:
        raise EngineError(
            "engine checked out at {}, expected {}".format(head, expect_sha)
        )
    return head


def assert_single_pin(text: str, where: str) -> str:
    """Verify a consumer ingress selects exactly one Continuum release.

    One *value*, and every external reference names the release entrypoint. A
    surface named directly is a per-module pin that can drift away from the
    selected release, and two different refs are two independent pins -- ADR-0002
    rejects both, because each one turns an upgrade into multi-file
    coordination and lets the graph and the engine come from different releases.

    A consumer needs one job per surface: the event set and the permission grant
    for each are consumer-owned, so the ingress repeats the reference rather than
    merging them into one call that could only hold one grant. What must not vary
    between those repetitions is the release.
    """
    pins = consumer_pins(text)
    if not pins:
        raise EngineError("{} holds no Continuum release reference".format(where))
    off_entrypoint = sorted(
        {name for name, _ in pins if name != RELEASE_ENTRYPOINT}
    )
    if off_entrypoint:
        raise EngineError(
            "{} names {} directly; a consumer selects the release through {}".format(
                where,
                ", ".join(
                    "{0}.github/workflows/{1}".format(ENGINE_REPOSITORY, name)
                    for name in off_entrypoint
                ),
                RELEASE_REFERENCE,
            )
        )
    selected = sorted({pin for _, pin in pins})
    if len(selected) > 1:
        raise EngineError(
            "{} holds {} different Continuum pins ({}); a consumer has one "
            "dependency, and an upgrade is one value".format(
                where, len(selected), ", ".join(selected)
            )
        )
    pin = selected[0]
    if is_floating(pin):
        raise EngineError(
            "{} pins {!r}, which is not an exact release; a moving ref would let "
            "the consumer change without anyone changing it".format(where, pin)
        )
    return pin


def rewrite_pin(text: str, pin: str) -> str:
    """Return the same ingress with its Continuum release reference set to ``pin``.

    The only edit an upgrade or a rollback is allowed to make, which is what the
    rewrite is here to prove: the substitution is total, it touches the pin and
    nothing else, and the result is still a valid single-pin ingress.
    """
    if not is_pinnable(pin):
        raise EngineError("{!r} is not an exact release".format(pin))
    pins = consumer_pins(text)
    if not pins:
        raise EngineError("the ingress holds no Continuum release reference")
    rewritten = CONSUMER_REFERENCE.sub(
        lambda match: "    uses: {}/.github/workflows/{}@{}".format(
            ENGINE_REPOSITORY, match.group(1), pin
        ),
        text,
    )
    return rewritten


#: Where a consumer's ingress lives. A caller may spread one dependency over
#: several files -- a provider entrypoint, a dispatch target -- and the upgrade
#: has to move all of them together or the repository ends up on two releases.
INGRESS_GLOB = ".github/workflows/*.yml"


def ingresses(repo: pathlib.Path) -> list:
    """Every workflow file in a consumer repository that references Continuum.

    Files that reference nothing are not an error: a consumer's own release
    internals, a test fixture, and a workflow that only calls its siblings are
    all legitimately Continuum-free. What matters is that the ones that *do*
    select a release all select the same one.
    """
    found = []
    for path in sorted(repo.glob(INGRESS_GLOB)):
        text = path.read_text(encoding="utf-8")
        if consumer_pins(text):
            found.append((path, text))
    return found


def repository_pin(repo: pathlib.Path) -> str:
    """The one release a consumer repository as a whole selects.

    Per-file assertions cannot catch a repository that is half upgraded: each
    file on its own holds one pin, and the repository still has two. So the value
    is resolved across every referencing file at once and compared.
    """
    selected = set()
    for path, text in ingresses(repo):
        selected.add(assert_single_pin(text, str(path)))
    if not selected:
        raise EngineError(
            "{} selects no Continuum release; {} found no {}".format(
                repo, repo.glob(INGRESS_GLOB), RELEASE_REFERENCE
            )
        )
    if len(selected) > 1:
        raise EngineError(
            "{} selects {} different Continuum releases ({}); a consumer has one "
            "dependency, and an upgrade is one value".format(
                repo, len(selected), ", ".join(sorted(selected))
            )
        )
    return selected.pop()


def rewrite_repository(repo: pathlib.Path, pin: str) -> list:
    """Set the release pin across a whole consumer repository.

    Writes each file, and returns the paths it changed. The whole repository is
    resolved to one value first and rewritten only after that succeeds, so a
    half-upgraded repository is reported rather than silently completed: the
    caller has to say whether it is moving to a new release or back to the old
    one, and guessing which would be the drift this exists to prevent.
    """
    before = repository_pin(repo)
    if before == pin:
        return []
    if not is_pinnable(pin):
        raise EngineError("{!r} is not an exact release".format(pin))
    changed = []
    for path, text in ingresses(repo):
        path.write_text(rewrite_pin(text, pin), encoding="utf-8")
        changed.append(path)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="continuum_engine.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def ingress_arguments(command):
        """Either one file, or a whole consumer repository.

        A consumer's ingress is usually one file, but a provider entrypoint and
        a dispatch target are separate files in the same repository, and they
        are one dependency. `--repo` is what makes an upgrade of that repository
        one command instead of one command per file.
        """
        command.add_argument("--ingress", help="a single ingress workflow file")
        command.add_argument(
            "--repo",
            help="a consumer repository; every referencing workflow in it",
        )

    assert_command = commands.add_parser(
        "assert", help="verify a checkout is the expected commit and is complete"
    )
    assert_command.add_argument("--root", required=True)
    assert_command.add_argument("--expect-sha", default="")

    pin_command = commands.add_parser(
        "pin", help="report the release reference a consumer pin resolves to"
    )
    ingress_arguments(pin_command)

    require_command = commands.add_parser(
        "require", help="fail unless an ingress carries exactly one expected pin"
    )
    ingress_arguments(require_command)
    require_command.add_argument("--expect", default="")

    rewrite_command = commands.add_parser(
        "rewrite", help="print the ingress with its single release reference replaced"
    )
    ingress_arguments(rewrite_command)
    rewrite_command.add_argument("--pin", required=True)

    arguments = parser.parse_args(argv)
    targets = [name for name in ("ingress", "repo") if getattr(arguments, name)]
    if len(targets) != 1:
        parser.error("pass exactly one of --ingress or --repo")

    try:
        if arguments.command == "assert":
            head = assert_engine(
                pathlib.Path(arguments.root), arguments.expect_sha
            )
            print("Continuum engine {} is complete at {}".format(head, arguments.root))
        elif arguments.command == "pin":
            if arguments.repo:
                pin = repository_pin(pathlib.Path(arguments.repo))
            else:
                text = pathlib.Path(arguments.ingress).read_text(encoding="utf-8")
                pin = assert_single_pin(text, str(arguments.ingress))
            print("{}@{}".format(RELEASE_REFERENCE, pin))
        elif arguments.command == "require":
            if arguments.repo:
                pin = repository_pin(pathlib.Path(arguments.repo))
                if arguments.expect and pin != arguments.expect:
                    raise EngineError(
                        "{} pins {}, expected {}".format(arguments.repo, pin, arguments.expect)
                    )
            else:
                text = pathlib.Path(arguments.ingress).read_text(encoding="utf-8")
                pin = assert_single_pin(text, str(arguments.ingress))
                if arguments.expect and pin != arguments.expect:
                    raise EngineError(
                        "{} pins {}, expected {}".format(
                            arguments.ingress, pin, arguments.expect
                        )
                    )
            print("{}@{}".format(RELEASE_REFERENCE, pin))
        elif arguments.command == "rewrite":
            if arguments.repo:
                repo = pathlib.Path(arguments.repo)
                for path in rewrite_repository(repo, arguments.pin):
                    print("rewrote {} to {}".format(path, arguments.pin), file=sys.stderr)
                if repository_pin(repo) == arguments.pin:
                    print(
                        "{} selects {}".format(repo, arguments.pin), file=sys.stderr
                    )
            else:
                text = pathlib.Path(arguments.ingress).read_text(encoding="utf-8")
                sys.stdout.write(rewrite_pin(text, arguments.pin))
    except EngineError as error:
        print("::error::{}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
