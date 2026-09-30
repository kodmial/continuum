"""The token's hands, and the boundary around them.

This is the only module that knows how to write to a repository. Everything else
in the package goes through a method on this class, and the two modules that can
change anything -- :mod:`continuum.migrate.apply` and
:mod:`continuum.migrate.rollback` -- are each given a fixed allowlist of which
of these methods they may call. So the write surface of the whole migration
controller is: this file, plus two explicit lists.

That structure is the answer to the architecture's requirement that installer
privilege be separate from agent privilege. A coding agent in a consumer can
reach a token, because an agent has to be able to comment on a pull request. If
that same token could reach these methods, "the agent may comment" and "the agent
may rewrite the control plane" would be the same capability wearing two hats.
Here they are separate hats: this client is constructed from a token this
repository's workflow supplies, and it is never constructed from anything a
consumer's agent job can influence.

Two methods deserve their name rather than the one they were renamed to:

``read_file_at_ref``
    :meth:`GitHubClient.file_at_ref` returns ``None`` for a missing file *and* for
    a 403, a 500 and a timeout. That is the right behaviour for a reviewer asking
    "is this comment resolved?" and the wrong behaviour for a controller deciding
    whether a repository has a cutover record: a blip would read as "no record",
    and the plan would then overwrite the only local copy of where the cutover can
    be undone to. So this distinguishes a 404 -- genuinely absent -- from every
    other failure, which propagates.

``update_ref``
    GitHub's REST ref update has no conditional form, so there is no true
    compare-and-swap available here. Rather than pretend otherwise, this reads the
    ref immediately before the write and refuses when it does not hold the value
    the caller expected. The residual race is milliseconds wide and the branch is
    controller-owned, so the honest description is "a fresh read, not an atomic
    guarantee" -- and the controller is written to treat a ref it did not write as
    somebody else's, which is the outcome that matters.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..review.github import GitHubClient, GitHubError

#: GitHub's canonical way to say "squash it onto the base". A migration is one
#: commit on a controller-owned branch; squash keeps it that way whatever the
#: repository's default merge method happens to be, so the merged default branch
#: gains exactly the reviewed change and not a merge commit nobody reviewed.
MERGE_METHOD = "squash"


def branch_path(ref: str) -> str:
    """``refs/heads/x`` or ``x`` -> ``heads/x``.

    GitHub's two ref endpoints are spelled differently -- ``git/ref/{ref}`` wants
    ``heads/x`` and ``git/refs/{ref}`` wants ``heads/x`` too -- and both 404 when
    handed a ``refs/`` prefix, which is exactly the shape a caller naturally has.
    Normalising once means a caller can pass either and gets the same URL, and a
    doubled ``refs/refs/`` cannot reach the API.
    """

    value = str(ref or "").strip()
    if value.startswith("refs/"):
        value = value[len("refs/") :]
    if not value:
        raise MigrationError("missing_ref", "a branch is required")
    if value.startswith("heads/"):
        return value
    return "heads/{}".format(value)


class MigrationError(RuntimeError):
    """A migration client call failed in a way the controller must not paper over."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("{}: {}".format(code, message))
        self.code = code
        self.message = message


#: What a repository name may be. The same rule the workflow applies to its
#: `target_repository` input, so the two layers cannot disagree about what a
#: target is: one shape, checked in both places.
REPOSITORY_SHAPE = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


def repository_owner_and_name(repository: str) -> "tuple[str, str]":
    """``owner/name`` split, or refuse.

    ``GitHubClient`` checks only that *a* slash is present and then splits on the
    first one, which is enough to build a URL and not enough to build the right
    one. ``kodmial/nanodictate/contents`` becomes an owner called ``kodmial`` and
    a name called ``nanodictate/contents``, and every endpoint this client builds
    is a string format with the name interpolated, so the wrong value silently
    aims the read or the write at a different path in the API rather than failing.

    A cutover that reads a file nobody asked it to read, or pushes to a branch
    under a name the operator never saw, is not a cutover. So the shape is checked
    once, here, at the single point the repository enters the client -- and the
    workflow checks it too, because a check that only exists behind the CLI is not
    a check on the input.
    """

    value = str(repository or "").strip()
    if not REPOSITORY_SHAPE.match(value):
        raise MigrationError(
            "malformed_repository",
            "the repository must be owner/name with only letters, digits, '.', '_' "
            "and '-', got {!r}".format(repository),
        )
    owner, _, name = value.partition("/")
    return owner, name


class MigrationClient(GitHubClient):
    """``GitHubClient`` plus the reads and the writes a cutover needs.

    Subclasses rather than wraps, so the review client's read methods -- threads,
    comments, reviews -- are available here too and behave identically. A cutover
    runs in the same job as the review machinery and a second HTTP client with a
    second set of quirks is one more thing to get right at the worst time.
    """

    # -- reads the inventory needs and the review client does not have ------ #

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Refuse a repository that is not ``owner/name`` before building any URL."""

        # The repository is named by an operator or by a workflow input, and it
        # is interpolated into every URL this client builds. Validated before the
        # base class splits it, so the check cannot be routed around by whichever
        # constructor shape a caller happens to use.
        repository = kwargs.get("repository")
        if repository is None and len(args) >= 2:
            repository = args[1]
        if repository is not None:
            repository_owner_and_name(repository)
        super().__init__(*args, **kwargs)

    def read_file_at_ref(self, path: str, ref: str) -> Optional[str]:
        """The file's text at ``ref``, ``None`` if it is not there, raise if unknown.

        The distinction the inherited method does not make. A 404 means the
        repository does not have this file; anything else means the controller
        does not know, and a controller that cannot tell those apart must not
        write.
        """

        quoted = urllib.parse.quote(path)
        query = urllib.parse.urlencode({"ref": ref})
        try:
            data = self.request(
                "GET", "/repos/{}/{}/contents/{}?{}".format(self.owner, self.name, quoted, query)
            )
        except GitHubError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise MigrationError("file_read_not_an_object", "{} at {} read as {!r}".format(path, ref, type(data).__name__))
        if data.get("encoding") != "base64":
            raise MigrationError(
                "file_read_undecodable",
                "{} at {} came back as {} rather than base64, so its content is "
                "unknown".format(path, ref, data.get("encoding")),
            )
        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except Exception as error:  # noqa: BLE001
            raise MigrationError(
                "file_read_undecodable", "{} at {} could not be decoded: {}".format(path, ref, error)
            ) from None

    def list_repo_variables(self) -> Dict[str, str]:
        """Repository Actions variables, by name.

        Returned as ``{name: value}``. These are configuration, not credentials --
        GitHub exposes them in plaintext by design -- and the inventory carries them
        so a plan can preserve a model name it does not understand. Only secret
        *names* are read anywhere in this package.
        """

        variables: Dict[str, str] = {}
        for item in self.paginate(
            "/repos/{}/{}/actions/variables".format(self.owner, self.name)
        ):
            if isinstance(item, dict) and item.get("name"):
                variables[str(item["name"])] = str(item.get("value", "") or "")
        return variables

    def list_repo_secret_names(self) -> List[str]:
        """The repository's secret names.

        GitHub never returns a secret's value, and nothing here asks for one. The
        controller needs to know that ``OPENCODE_API_KEY`` exists before it
        installs a caller that references it -- a cutover that installed one and
        then failed at the first run would leave the repository with no repair and
        no agent -- and it needs nothing more than the name to do that.
        """

        names: List[str] = []
        for item in self.paginate("/repos/{}/{}/actions/secrets".format(self.owner, self.name)):
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return sorted(names)

    def list_labels(self) -> List[str]:
        return sorted(
            str(item["name"])
            for item in self.paginate(
                "/repos/{}/{}/labels?per_page=100".format(self.owner, self.name)
            )
            if isinstance(item, dict) and item.get("name")
        )

    def tree_blobs(self, ref: str) -> Dict[str, str]:
        """Every blob at ``ref``, as ``path -> blob SHA``.

        Recursive, so the whole tree arrives in one response. This is how a
        rollback learns what the recorded revision actually contained: it must not
        assume a file was there because the record says the cutover removed it, or
        was absent because the record does not mention it.

        Not ``paginate``, and that is deliberate. A git tree is an *object*
        (``{"tree": [...], "truncated": false}``), not a list, and a paginator that
        only accumulates lists would hand back an empty mapping for every
        repository -- which is indistinguishable from a commit with no files in
        it. GitHub's own answer to a truncated recursive tree is to refuse it and
        ask for a narrower walk, and so does this: a partial tree would have the
        rollback restore from a revision it has only partly read.
        """

        data = self.request(
            "GET",
            "/repos/{}/{}/git/trees/{}?recursive=1".format(self.owner, self.name, ref),
        )
        if not isinstance(data, dict):
            raise MigrationError(
                "tree_unreadable", "the tree at {} was not an object".format(ref)
            )
        if data.get("truncated"):
            raise MigrationError(
                "tree_truncated",
                "the recursive tree at {} is truncated, so it is not the whole "
                "revision. A rollback may not restore from a partly-read commit; "
                "raise the API's tree limit or walk the tree by path.".format(ref),
            )
        blobs: Dict[str, str] = {}
        for entry in data.get("tree") or []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path", "") or "")
            sha = str(entry.get("sha", "") or "")
            if path and sha and entry.get("type") == "blob":
                blobs[path] = sha
        return blobs

    # -- git data: one commit, not a series --------------------------------- #

    def get_ref(self, ref: str) -> str:
        """The SHA a ref points at, or ``""`` when it does not exist."""

        try:
            data = self.request(
                "GET",
                "/repos/{}/{}/git/ref/{}".format(
                    self.owner, self.name, branch_path(ref)
                ),
            )
        except GitHubError as error:
            if error.status == 404:
                return ""
            raise
        return str((data or {}).get("object", {}).get("sha", "") or "")

    def get_commit(self, sha: str) -> Dict[str, Any]:
        """One commit, for the tree it points at.

        Read, not written, but it is here because the only caller is the branch
        ownership check, and that check must not have to reach through the
        generic request interface to ask a question it cannot live without.
        """

        data = self.request(
            "GET", "/repos/{}/{}/git/commits/{}".format(self.owner, self.name, sha)
        )
        return data if isinstance(data, dict) else {}

    def create_blob(self, content: str) -> str:
        data = self.request(
            "POST",
            "/repos/{}/{}/git/blobs".format(self.owner, self.name),
            {"content": content, "encoding": "utf-8"},
        )
        return str((data or {}).get("sha", "") or "")

    def create_tree(self, base_sha: str, entries: Sequence[Mapping[str, Any]]) -> str:
        """A tree on top of ``base_sha``.

        Built against the base rather than an empty tree, so the change set says
        what it says and the rest of the repository is not part of the commit the
        controller reasons about. An entry whose ``sha`` is ``None`` is a deletion,
        which is how a retirement is expressed without a separate delete call.
        """

        payload = {"base_tree": base_sha, "tree": [dict(entry) for entry in entries]}
        data = self.request(
            "POST", "/repos/{}/{}/git/trees".format(self.owner, self.name), payload
        )
        return str((data or {}).get("sha", "") or "")

    def create_commit(self, message: str, tree: str, parents: Sequence[str]) -> str:
        data = self.request(
            "POST",
            "/repos/{}/{}/git/commits".format(self.owner, self.name),
            {"message": message, "tree": tree, "parents": list(parents)},
        )
        return str((data or {}).get("sha", "") or "")

    def update_ref(self, ref: str, sha: str, expected: str = "") -> str:
        """Move ``ref`` to ``sha``, refusing to move one somebody else moved.

        ``expected`` is the SHA the caller believes the ref holds. GitHub has no
        conditional ref update, so this reads the ref immediately before writing
        and refuses on a mismatch. Passing ``expected=""`` asserts the caller
        believes the ref is absent, which is the correct assertion for a first
        run and a wrong one for a second -- the second must pass the SHA the
        first wrote.
        """

        current = self.get_ref(ref)
        if current != expected:
            raise MigrationError(
                "ref_moved_externally",
                "{} is at {} but this run expected {}. The migration branch is only "
                "ever moved by this controller, so this is somebody else's commit: "
                "refusing rather than replacing it.".format(
                    ref, current[:12] or "<absent>", expected[:12] or "<absent>"
                ),
            )
        self.request(
            "PATCH",
            "/repos/{}/{}/git/refs/{}".format(
                self.owner, self.name, branch_path(ref)
            ),
            {"sha": sha, "force": True},
        )
        return sha

    # -- pull requests ------------------------------------------------------ #

    def list_pull_requests(self, *, state: str = "open") -> List[Dict[str, Any]]:
        return self.paginate(
            "/repos/{}/{}/pulls?state={}&per_page=100".format(self.owner, self.name, state)
        )

    def get_pull_request(self, number: int) -> Dict[str, Any]:
        data = self.request("GET", "/repos/{}/{}/pulls/{}".format(self.owner, self.name, number))
        return data if isinstance(data, dict) else {}

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: Sequence[str] = (),
        draft: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        if labels:
            payload["labels"] = list(labels)
        data = self.request(
            "POST", "/repos/{}/{}/pulls".format(self.owner, self.name), payload
        )
        return data if isinstance(data, dict) else {}

    def update_pull_request_base(self, number: int, base: str) -> Dict[str, Any]:
        data = self.request(
            "PATCH",
            "/repos/{}/{}/pulls/{}".format(self.owner, self.name, number),
            {"base": base},
        )
        return data if isinstance(data, dict) else {}

    def merge_pull_request(
        self, number: int, expected_head: str, *, merge_method: str = MERGE_METHOD
    ) -> Dict[str, Any]:
        """Merge, but only if the head is still ``expected_head``.

        ``sha`` is GitHub's compare-and-swap. It is the one genuinely atomic
        guarantee in the whole controller: a pull request that moved after
        validation cannot be merged by this call, whatever its check runs say.
        """

        data = self.request(
            "PUT",
            "/repos/{}/{}/pulls/{}/merge".format(self.owner, self.name, number),
            {"sha": expected_head, "merge_method": merge_method},
        )
        return data if isinstance(data, dict) else {}

    # -- labels ------------------------------------------------------------- #

    def ensure_label(self, name: str, color: str, description: str = "") -> str:
        """Create the label if it is missing; never touch one that exists.

        Deliberately a read-then-create rather than a create-or-update. A label's
        colour and description are the repository's own, and a migration that
        reset them would change how the repository reads in its issue list as a
        side effect of changing who dispatches the work.
        """

        try:
            self.request(
                "POST",
                "/repos/{}/{}/labels".format(self.owner, self.name),
                {"name": name, "color": color, "description": description},
            )
            return name
        except GitHubError as error:
            if error.status != 422:
                raise
        return name


def client_from_env(repository: str, *, token_env: str = "GITHUB_TOKEN") -> MigrationClient:
    """Build the client from the environment the trusted workflow provides.

    Refuses when the token is missing rather than falling back to an anonymous
    client, because an anonymous cutover would fail much later with a 404 that
    looks like a repository problem, and the first thing anybody would investigate
    is the wrong thing.
    """

    import os

    token = os.environ.get(token_env, "")
    if not token:
        raise MigrationError(
            "missing_token",
            "{} is not set, so this process has no way to read or write the "
            "consumer".format(token_env),
        )
    return MigrationClient(token, repository)
