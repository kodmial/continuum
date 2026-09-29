"""The cross-repository write capability, and the two ways a publisher can use it.

A publisher writes into a repository it does not own: a tap, a port tree, the
consumer's own manifest directory. That is a second, separate capability from
the one that cut the release, and this module is the boundary. It is reached
through a `PackageRepository`, never through subprocess calls sprinkled through
a publisher, so a test can hold a whole release in memory and a production job
can hold a real clone without either shape leaking into the other.

Three properties live here rather than in the publishers, because they are the
ones that decide whether a *concurrent* release corrupts a destination:

* **A stale destination HEAD is detected, not overwritten.** `write` takes the
  revision the caller read and re-reads the remote immediately before pushing.
  A destination that moved in between raises `stale-destination`, and the
  publisher reconciles by starting over against the new HEAD. There is no force
  push anywhere in this module, and that is the point: force-pushing a tap
  because a second release finished first discards whatever the first one
  merged, and the resulting formula is a mixture of two releases that never
  existed together.
* **Identical content is not a commit.** A re-run of the same version finds the
  bytes already there and reports `changed=False`. That is the whole of
  idempotency, and it is enforced at the destination rather than trusted to the
  publisher's own bookkeeping.
* **The credential is a capability, not a variable the publisher reads.** A
  destination is constructed with the repository it may write and the token it
  may write with; the publisher never sees either. It cannot reach a
  repository it was not handed, and it cannot read a secret, because it has no
  code path that would let it.

Every git invocation is an argument vector with `shell=False`. A repository
name, a branch, and a commit message all come from configuration, and a shell
would treat them as a language.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .contract import (
    DESTINATION_UNREACHABLE,
    STALE_DESTINATION,
    CommitResult,
    Destination,
    PublisherError,
    PullRequest,
)

GIT = "git"

# Nothing unbounded is captured from git: a hook or a credential helper can
# print anything at all, and the point of this module is that a publisher's
# output is safe to put in a job summary.
MAX_OUTPUT = 65536

# git's wording for "the branch you are pushing is not where you thought it
# was". It is matched rather than assumed because it is the only signal that a
# destination moved in the window between the re-read and the push itself.
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "cannot lock ref",
    "rejected",
    "behind its remote counterpart",
)

# The askpass helper git calls for a username and a password. It reads the
# token from the child's environment rather than being given it, so the token
# never appears in a file, an argument vector, or a process listing.
_ASKPASS_SCRIPT = '#!/bin/sh\nprintf "%s\\n" "$GIT_PASSWORD"\n'


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str


CommandRunner = Callable[[Sequence[str], str, Mapping[str, str]], CommandResult]


def subprocess_runner(
    argv: Sequence[str], workdir: str, environment: Mapping[str, str]
) -> CommandResult:  # pragma: no cover - process boundary
    """The production command runner: an argument vector, never a shell string."""

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
        cwd=workdir or None,
        env=dict(environment),
        check=False,
    )
    output = ((completed.stdout or "") + (completed.stderr or ""))[:MAX_OUTPUT]
    return CommandResult(code=completed.returncode, output=output)


class StaleDestination(PublisherError):
    """The destination moved between the read and the write."""

    def __init__(self, expected: str, found: str, repository: str) -> None:
        super().__init__(
            STALE_DESTINATION,
            f"{repository} moved from {expected or '<empty>'} to {found or '<empty>'} "
            "while this publish was preparing its change",
            remediation=(
                "another release or a human landed a commit on the destination. The "
                "publish is retried against the new HEAD; it is never forced over it, "
                "because forcing would discard whatever the other writer merged."
            ),
        )
        self.expected = expected
        self.found = found


class PackageRepository(Protocol):
    """A destination a publisher may write to."""

    name: str

    def head_revision(self) -> str:  # pragma: no cover - protocol
        ...

    def read_file(self, path: str) -> Optional[str]:  # pragma: no cover - protocol
        ...

    def write(
        self,
        files: Mapping[str, str],
        *,
        message: str,
        expected_head: str,
        branch: str,
    ) -> CommitResult:  # pragma: no cover - protocol
        ...

    def open_pull_request(
        self, *, title: str, body: str, head: str, base: str
    ) -> PullRequest:  # pragma: no cover - protocol
        ...


@dataclass
class InMemoryPackageRepository:
    """A destination held in a dictionary.

    Two things use this. A test asserting that a stale HEAD is detected, or that
    a re-run writes nothing, needs a destination whose HEAD it can move on
    purpose. And a caller that wants to *review* what a publish would write
    needs somewhere to write it that is not a real tap.
    """

    name: str
    branch: str = "main"
    head: str = ""
    files: Dict[str, str] = field(default_factory=dict)
    commits: List[Tuple[str, str, Dict[str, str]]] = field(default_factory=list)
    pull_requests: List[PullRequest] = field(default_factory=list)
    writes: int = 0

    def __post_init__(self) -> None:
        self._counter = 0
        if not self.head:
            self.head = self._next_revision()

    def _next_revision(self) -> str:
        self._counter += 1
        return f"{self._counter:040x}"

    def advance(self) -> str:
        """Move HEAD on, as a concurrent writer or a human would."""

        self.head = self._next_revision()
        return self.head

    def seed(self, path: str, content: str) -> None:
        self.files[path] = content

    def head_revision(self) -> str:
        return self.head

    def read_file(self, path: str) -> Optional[str]:
        return self.files.get(path)

    def write(
        self,
        files: Mapping[str, str],
        *,
        message: str,
        expected_head: str,
        branch: str,
    ) -> CommitResult:
        self.writes += 1
        if expected_head != self.head:
            raise StaleDestination(expected_head, self.head, self.name)
        unchanged = all(self.files.get(path) == content for path, content in files.items())
        if unchanged:
            return CommitResult(revision=self.head, branch=branch, changed=False)
        recorded = dict(files)
        self.files.update(recorded)
        self.head = self._next_revision()
        self.commits.append((self.head, message, recorded))
        return CommitResult(revision=self.head, branch=branch, changed=True)

    def open_pull_request(self, *, title: str, body: str, head: str, base: str) -> PullRequest:
        number = len(self.pull_requests) + 1
        pull = PullRequest(
            number=number,
            url=f"https://example.invalid/{self.name}/pull/{number}",
            head=head,
            base=base,
        )
        self.pull_requests.append(pull)
        return pull


class GitPackageRepository:
    """A destination that is a real repository.

    The token reaches git through `GIT_ASKPASS` rather than through the remote
    URL. A token in a URL ends up in git's argument vector, and an argument
    vector is something a process listing, a crash report, and a debug log can
    all show; the helper keeps it in the child's environment and nowhere else.
    """

    def __init__(
        self,
        destination: Destination,
        *,
        environment: Optional[Mapping[str, str]] = None,
        command_runner: Optional[CommandRunner] = None,
        workdir: str = "",
        token: str = "",
    ) -> None:
        self.destination = destination
        self.name = destination.repository
        self.environment: Dict[str, str] = dict(environment or {})
        self.command_runner: CommandRunner = command_runner or subprocess_runner
        self._owned_workdir = not workdir
        self.workdir = workdir or tempfile.mkdtemp(prefix="continuum-publisher-")
        self.token = token
        self.remote = f"https://github.com/{destination.repository}.git"
        self._checked_out = False

    # -- process boundary ---------------------------------------------------
    def _git(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        what: str = "git",
    ) -> CommandResult:
        environment = dict(self.environment)
        if self.token:
            environment["GIT_ASKPASS"] = os.path.join(self.workdir, "continuum-askpass.sh")
            environment["GIT_TERMINAL_PROMPT"] = "0"
            environment["GIT_USERNAME"] = "x-access-token"
            environment["GIT_PASSWORD"] = self.token
        result = self.command_runner(list(argv), self.workdir, environment)
        if check and result.code != 0:
            raise PublisherError(
                DESTINATION_UNREACHABLE,
                f"{what} against {self.name} failed: {result.output.strip()[:400]}",
                remediation=(
                    "check that the destination repository exists, that the named "
                    "credential can write to it, and that the branch exists"
                ),
            )
        return result

    def _write_askpass(self) -> None:
        if not self.token or self.command_runner is not subprocess_runner:
            return
        os.makedirs(self.workdir, exist_ok=True)
        helper = os.path.join(self.workdir, "continuum-askpass.sh")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(_ASKPASS_SCRIPT)
        os.chmod(helper, 0o700)

    # -- checkout -----------------------------------------------------------
    def ensure_checkout(self) -> None:
        if self._checked_out:
            return
        if os.path.isdir(os.path.join(self.workdir, ".git")):
            self._git([GIT, "remote", "set-url", "origin", self.remote], what="git remote")
        else:
            self._git(
                [GIT, "clone", "--depth", "1", "--branch", self.destination.branch, self.remote, "."],
                what="git clone",
            )
        self._write_askpass()
        self._checked_out = True

    def fetch(self) -> None:
        self.ensure_checkout()
        self._git([GIT, "fetch", "--depth", "1", "origin", self.destination.branch], what="git fetch")

    def head_revision(self) -> str:
        self.ensure_checkout()
        output = self._git([GIT, "rev-parse", "HEAD"], what="git rev-parse").output.strip()
        return output.splitlines()[0] if output else ""

    def remote_head_revision(self) -> str:
        self.ensure_checkout()
        result = self._git([GIT, "ls-remote", self.remote, self.destination.branch], what="git ls-remote")
        for line in result.output.splitlines():
            parts = line.split()
            if len(parts) == 2:
                return parts[0]
        return ""

    def read_file(self, path: str) -> Optional[str]:
        self.ensure_checkout()
        # `git show` exits non-zero for a path the destination has never held,
        # which is the ordinary "this is a new file" case rather than a failure.
        result = self._git([GIT, "show", f"HEAD:{path}"], check=False, what="git show")
        if result.code != 0:
            return None
        return result.output

    def write(
        self,
        files: Mapping[str, str],
        *,
        message: str,
        expected_head: str,
        branch: str,
    ) -> CommitResult:
        self.ensure_checkout()
        # Re-read immediately before the push. Reading once at the start of a
        # publish and pushing minutes later is exactly the window in which a
        # second release finishes, and it is the only moment a stale destination
        # can be caught before it is overwritten.
        current = self.remote_head_revision() or self.head_revision()
        if expected_head and current and expected_head != current:
            raise StaleDestination(expected_head, current, self.name)

        if branch != self.destination.branch:
            self._git([GIT, "checkout", "-B", branch], what="git checkout")
        for path, content in sorted(files.items()):
            full = os.path.join(self.workdir, path)
            os.makedirs(os.path.dirname(full) or self.workdir, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)
        self._git([GIT, "add", "--", *sorted(files)], what="git add")
        staged = self._git([GIT, "diff", "--cached", "--quiet"], check=False, what="git diff")
        if staged.code == 0:
            return CommitResult(revision=current, branch=branch, changed=False)
        self._git([GIT, "commit", "-m", message], what="git commit")
        pushed = self._git(
            [GIT, "push", "origin", f"HEAD:refs/heads/{branch}"], check=False, what="git push"
        )
        if pushed.code != 0:
            lowered = pushed.output.lower()
            if any(marker in lowered for marker in _NON_FAST_FORWARD_MARKERS):
                raise StaleDestination(expected_head, current, self.name)
            raise PublisherError(
                DESTINATION_UNREACHABLE,
                f"pushing {branch} to {self.name} failed: {pushed.output.strip()[:400]}",
                remediation=(
                    "this push is never forced. Check the destination's branch "
                    "protection and the credential's write access."
                ),
            )
        revision = self._git([GIT, "rev-parse", "HEAD"], what="git rev-parse").output.strip()
        return CommitResult(
            revision=revision.splitlines()[0] if revision else current,
            branch=branch,
            changed=True,
        )

    def open_pull_request(self, *, title: str, body: str, head: str, base: str) -> PullRequest:
        raise PublisherError(
            DESTINATION_UNREACHABLE,
            f"opening a pull request on {self.name} needs a pull-request client, and "
            "none was supplied to this repository",
            remediation=(
                "supply a PackageRepository implementation that can open the pull "
                "request, or set the destination mode to 'trusted', where the branch is "
                "pushed directly"
            ),
        )

    def cleanup(self) -> None:
        if self._owned_workdir and os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)


def reconcile(
    repository: PackageRepository,
    build: Callable[[str], Mapping[str, str]],
    *,
    message: str,
    branch: str,
    attempts: int,
) -> CommitResult:
    """Read, render, write — and start over if the destination moved.

    `build` is called with the HEAD it should render against, so a reconcile
    re-reads whatever the other writer left rather than replaying a decision
    made against a revision that no longer exists. The loop is bounded by the
    destination's own `attempts`: an unbounded retry against a destination that
    somebody is actively pushing to is a publish that never finishes.
    """

    last: Optional[StaleDestination] = None
    for _ in range(max(1, attempts)):
        head = repository.head_revision()
        files = build(head)
        try:
            return repository.write(files, message=message, expected_head=head, branch=branch)
        except StaleDestination as exc:
            last = exc
    if last is not None:
        raise last
    raise PublisherError(  # pragma: no cover - attempts is validated to be >= 1
        STALE_DESTINATION, "the destination never accepted a write"
    )


__all__ = [
    "GIT",
    "MAX_OUTPUT",
    "CommandResult",
    "CommandRunner",
    "GitPackageRepository",
    "InMemoryPackageRepository",
    "PackageRepository",
    "StaleDestination",
    "reconcile",
    "subprocess_runner",
]
