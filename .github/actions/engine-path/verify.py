#!/usr/bin/env python3
"""Verify that an engine checkout is the Continuum release a caller selected.

This is the check that makes one consumer pin mean one coherent implementation
instead of a mix of two. A workflow that checked Continuum out at some other
revision, or out of some other repository, would still look correct in review --
`ref: ${{ job.workflow_sha }}` reads like a pin -- and would still be verified
against `job.workflow_sha`, because `job.workflow_sha` describes the *workflow
file*, not the *repository the engine was fetched from*. The two are separate
facts and both have to hold.

The logic lives here rather than inline in `action.yml` because it is real logic
with real failure modes, and shell in a YAML string is not reviewable or
testable. `action.yml` is the three lines that call this and export the result.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from typing import Iterable, Optional, Sequence

CONTINUUM_REPOSITORY = "kodmial/continuum"

#: A full, unabbreviated commit SHA. A branch, a tag prefix, an empty value, or a
#: shortened SHA is a moving target or an incomplete selection.
COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: Files whose absence would fail part way through a privileged step rather than
#: here. A partial engine checkout is a checkout of a revision that is not a
#: release, so it is refused at the boundary.
REQUIRED_ENGINE_FILES = (
    ".github/scripts/conflict_repair.py",
    ".github/scripts/agent_workspace.py",
    ".github/scripts/opencode_runtime.py",
    # The global instructions document and its installer travel together: a
    # consumer workflow installs one from the other, so half of the pair is a
    # partial checkout.
    ".github/scripts/opencode_instructions.py",
    ".github/agents/AGENTS.md",
    # The review and release entrypoints run the engine package itself, not only
    # the shell helpers above.
    "src/continuum/cli.py",
)


class EngineError(RuntimeError):
    """The engine checkout is not the release a caller selected."""


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise EngineError(
            f"`git {' '.join(args)}` failed in {root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def normalize_origin(url: str) -> str:
    """Reduce a git remote URL to its ``owner/repo`` path.

    `actions/checkout` writes the URL it authenticated with, so the scheme, the
    credentials, and the host are all the runner's choice. Asserting on the whole
    string would reject the ordinary HTTPS form (`https://github.com/o/r`) and
    accept only the SSH one -- a check that fails on every real run is a check
    nobody notices is wrong. What actually has to hold is that the remote names
    the Continuum repository, so that is what is compared.
    """

    if not url:
        raise EngineError("the engine checkout has no origin remote")
    rest = url.strip()
    # Scheme, e.g. `https://` or `ssh://`.
    rest = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", rest)
    # Credentials, e.g. `git@`.
    rest = re.sub(r"^[^/]*@", "", rest)
    # The scp-like host of `github.com:owner/repo`, which is not a URL and so
    # never reached the scheme rule above.
    rest = re.sub(r"^[^/]*:", "", rest)
    # The host of a hierarchical URL. `owner/repo` has two components and
    # `host/owner/repo` has three, so the count is what distinguishes them --
    # testing for a dot would misread a repository named `foo.bar`.
    if rest.count("/") >= 2:
        rest = rest.split("/", 1)[1]
    if rest.endswith(".git"):
        rest = rest[: -len(".git")]
    return rest.strip("/")


def verify(
    root: pathlib.Path,
    release_sha: str,
    repository: str = CONTINUUM_REPOSITORY,
    required_files: Iterable[str] = REQUIRED_ENGINE_FILES,
) -> None:
    """Raise `EngineError` unless ``root`` holds exactly the selected release."""

    if not COMMIT.match(release_sha or ""):
        raise EngineError(
            f"the Continuum release must be a full commit sha, got {release_sha!r}; "
            "a branch, a tag, or an abbreviated sha is not a release selection"
        )
    if not (root / ".git").exists():
        raise EngineError(f"the Continuum engine checkout at {root} is not a git repository")

    head = _git(root, "rev-parse", "HEAD")
    if head != release_sha:
        raise EngineError(
            f"the Continuum engine is checked out at {head}, not the selected "
            f"release {release_sha}"
        )

    origin = normalize_origin(_git(root, "config", "--get", "remote.origin.url"))
    if origin != repository:
        raise EngineError(
            f"the Continuum engine checkout is from {origin!r}, not {repository!r}"
        )

    missing = [name for name in required_files if not (root / name).is_file()]
    if missing:
        raise EngineError(
            "the Continuum engine checkout at "
            f"{root} is missing {', '.join(missing)}; a partial engine checkout "
            "is not a release"
        )


def exclude_from_consumer_commits(
    workspace: pathlib.Path,
    engine_relative: str,
    engine_root: Optional[pathlib.Path] = None,
) -> None:
    """Keep the engine checkout out of the consumer's own history.

    The engine necessarily lives under the workspace, and the agent commits with
    `git add -A`. A local exclude keeps the selected implementation out of the
    consumer's diffs and history without editing a tracked `.gitignore` the
    consumer owns. Two cases are not errors and do nothing: a job that never
    checked the consumer repository out has no `.git` to exclude from, and a job
    running the engine from this repository has nothing to protect itself from.
    """

    git_dir = workspace / ".git"
    if not git_dir.is_dir():
        return
    if engine_root is not None and workspace.resolve() == engine_root.resolve():
        return
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    entry = f"/{engine_relative.strip('/')}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if entry in existing.splitlines():
        return
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(entry + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="the engine checkout to verify")
    parser.add_argument("--release-sha", required=True, help="the selected release commit")
    parser.add_argument(
        "--repository",
        default=CONTINUUM_REPOSITORY,
        help="the repository the engine checkout must have come from",
    )
    parser.add_argument(
        "--exclude-from",
        help="a workspace whose history must not capture the engine checkout",
    )
    arguments = parser.parse_args(argv)

    root = pathlib.Path(arguments.root).resolve()
    try:
        verify(root, arguments.release_sha, arguments.repository)
    except EngineError as error:
        print(f"::error::{error}")
        return 1

    if arguments.exclude_from:
        exclude_from_consumer_commits(
            pathlib.Path(arguments.exclude_from), ".continuum-engine", root
        )

    print(f"CONTINUUM_ENGINE_ROOT={root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
