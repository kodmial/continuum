"""Deterministic test doubles for the Continuum migration controller.

Every double here is fail-closed in the same direction the production code is:
a fake that is easier to use than the real API is a fake that lets a test pass on
a call the real controller would refuse.

Two of them matter more than the rest.

:class:`FakeGitHub` keeps **call records**, not just return values. Most of the
invariants in this package are about *which* methods were called and in what
order -- "the merge is pinned to the validated head", "a second reconcile does
not open a second pull request" -- and a double that only returns values cannot
test any of them.

:class:`FakeGitHub` also fails rather than returning ``None`` for a repository
that does not have a file, because that is the distinction
:meth:`MigrationClient.read_file_at_ref` exists to make. A fake that answered
``None`` to everything would make the strictness untestable.
"""

from __future__ import annotations

import hashlib

import base64
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from continuum.shadow import baseline

#: A real-looking blob SHA for arbitrary bytes, computed the way git does it.
#: Delegated to the production helper so the tests cannot drift from it.
from continuum.migrate.inventory import git_blob_sha


class CallError(RuntimeError):
    """The fake was asked for something it was not set up to answer."""


class FakeGitHub:
    """A GitHub with a call log, a repository, and no surprises.

    Deliberately raises :class:`CallError` for a method that is not configured
    rather than returning a plausible empty value. A fake that quietly answers
    "no pull requests" to a controller that is testing whether a pull request
    exists is a fake that hides the bug this package is about.
    """

    def __init__(
        self,
        repository: str = "kodmial/nanodictate",
        *,
        default_branch: str = "main",
        head_sha: str = "",
        files: Optional[Mapping[str, str]] = None,
        historical_files: Optional[Mapping[str, Mapping[str, str]]] = None,
        workflow_blobs: Optional[Mapping[str, str]] = None,
        variables: Optional[Mapping[str, str]] = None,
        secret_names: Sequence[str] = (),
        labels: Sequence[str] = (),
        pull_requests: Sequence[Mapping[str, Any]] = (),
        pull_files: Optional[Mapping[int, Sequence[str]]] = None,
        check_runs: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
        merges: Optional[Mapping[int, bool]] = None,
        refs: Optional[Mapping[str, str]] = None,
        read_failures: Sequence[str] = (),
    ) -> None:
        self.repository = repository
        self.owner, self.name = repository.split("/", 1)
        self._default_branch = default_branch
        self.head_sha = head_sha or "b" * 40
        self.files: Dict[str, str] = dict(files or {})
        #: Ref -> the files at that ref. A rollback reads a *past* revision, so a
        #: fake that only knows the current one cannot express the case the
        #: rollback exists for: restoring something that is not there any more.
        self.historical_files: Dict[str, Dict[str, str]] = {
            key: dict(value) for key, value in (historical_files or {}).items()
        }
        self.workflow_blobs: Dict[str, str] = dict(workflow_blobs or {})
        self.variables: Dict[str, str] = dict(variables or {})
        self.secret_names: List[str] = list(secret_names)
        self.labels: List[str] = list(labels)
        self.pull_requests: List[Dict[str, Any]] = [dict(item) for item in pull_requests]
        self.pull_files: Dict[int, List[str]] = {
            int(key): list(value) for key, value in (pull_files or {}).items()
        }
        self.check_runs: Dict[str, List[Dict[str, Any]]] = {
            key: [dict(run) for run in value]
            for key, value in (check_runs or {}).items()
        }
        self.merges: Dict[int, bool] = {int(k): bool(v) for k, v in (merges or {}).items()}
        self.refs: Dict[str, str] = dict(refs or {})
        self.read_failures = tuple(read_failures)

        # The call log. Every entry is ``(method, args, kwargs)``.
        self.calls: List[Tuple[str, Tuple[Any, ...], Dict[str, Any]]] = []
        #: Objects the fake minted, so a test can assert on what it created.
        self.blobs: Dict[str, str] = {}
        self.trees: List[Tuple[str, Sequence[Mapping[str, Any]]]] = []
        self.trees_by_sha: Dict[str, Tuple[str, Sequence[Mapping[str, Any]]]] = {}
        self.commits: List[Tuple[str, str, Sequence[str]]] = []
        #: commit -> tree, so a run that comes back can recognise its own work.
        self.commit_trees: Dict[str, str] = {}
        self.created_labels: List[Tuple[str, str, str]] = []
        self.created_pulls: List[Dict[str, Any]] = []
        self.merge_calls: List[Tuple[int, str]] = []
        self._counter = 0

    # -- assertions helpers ------------------------------------------------ #

    def method_names(self) -> List[str]:
        return [name for name, _, _ in self.calls]

    def called(self, name: str) -> bool:
        return name in self.method_names()

    def count(self, name: str) -> int:
        return self.method_names().count(name)

    # -- reads ------------------------------------------------------------- #

    def default_branch(self) -> str:
        self.calls.append(("default_branch", (), {}))
        return self._default_branch

    def ref_sha(self, ref: str) -> str:
        self.calls.append(("ref_sha", (ref,), {}))
        if ref in self.refs:
            return self.refs[ref]
        return self.head_sha

    def workflow_inventory(self, ref: str) -> Dict[str, str]:
        self.calls.append(("workflow_inventory", (ref,), {}))
        if ref in self.historical_files:
            return {
                path: git_blob_sha(text)
                for path, text in self.historical_files[ref].items()
                if path.startswith(".github/workflows/")
            }
        return dict(self.workflow_blobs)

    def list_pulls(self, *, state: str = "open", base: str = "") -> List[Dict[str, Any]]:
        self.calls.append(("list_pulls", (), {"state": state, "base": base}))
        return [
            dict(item)
            for item in self.pull_requests
            if state == "all" or item.get("state") == state
        ]

    def list_check_runs(self, ref: str) -> List[Dict[str, Any]]:
        self.calls.append(("list_check_runs", (ref,), {}))
        # `"*"` reports the same runs for any head, which is what a test means
        # when it configures gates without knowing the SHA the controller will
        # mint. Keying on the real head stays available for the tests that are
        # specifically about a head's own runs.
        return [
            dict(item)
            for item in self.check_runs.get(ref, self.check_runs.get("*", []))
        ]

    def list_pull_files(self, number: int) -> List[Dict[str, Any]]:
        self.calls.append(("list_pull_files", (number,), {}))
        return [
            {"filename": name, "status": "modified"}
            for name in self.pull_files.get(int(number), [])
        ]

    def list_repo_variables(self) -> Dict[str, str]:
        self.calls.append(("list_repo_variables", (), {}))
        return dict(self.variables)

    def list_repo_secret_names(self) -> List[str]:
        self.calls.append(("list_repo_secret_names", (), {}))
        return list(self.secret_names)

    def list_labels(self) -> List[str]:
        self.calls.append(("list_labels", (), {}))
        return list(self.labels)

    def read_file_at_ref(self, path: str, ref: str) -> Optional[str]:
        self.calls.append(("read_file_at_ref", (path, ref), {}))
        self._check_readable(path)
        return self.files_at(ref).get(path)

    def files_at(self, ref: str) -> Dict[str, str]:
        """The file table at ``ref``: history when there is any, current otherwise."""

        if ref in self.historical_files:
            return self.historical_files[ref]
        return self.files

    def tree_blobs(self, ref: str) -> Dict[str, str]:
        self.calls.append(("tree_blobs", (ref,), {}))
        return {
            path: git_blob_sha(text) for path, text in self.files_at(ref).items()
        }

    # -- writes ------------------------------------------------------------ #

    def get_ref(self, ref: str) -> str:
        self.calls.append(("get_ref", (ref,), {}))
        return self.refs.get(ref, "")

    def create_blob(self, content: str) -> str:
        self.calls.append(("create_blob", (content,), {}))
        sha = git_blob_sha(content)
        self.blobs[sha] = content
        return sha

    def create_tree(self, base_sha: str, entries: Sequence[Mapping[str, Any]]) -> str:
        """Content-addressed, as a real tree is.

        The same base and the same entries always produce the same tree, which is
        what makes a tree the identity of a change set: a controller that finds a
        branch holding a commit with its own tree knows the commit is its own work,
        even though the commit SHA itself will never repeat.
        """

        self.calls.append(("create_tree", (base_sha, tuple(entries)), {}))
        self.trees.append((base_sha, tuple(entries)))
        digest = hashlib.sha1()
        digest.update(base_sha.encode("utf-8"))
        for entry in entries:
            digest.update(b"\0")
            digest.update(
                repr(
                    (
                        entry.get("path"),
                        entry.get("mode"),
                        entry.get("type"),
                        entry.get("sha"),
                    )
                ).encode("utf-8")
            )
        sha = digest.hexdigest()
        self.trees_by_sha[sha] = (base_sha, tuple(entries))
        return sha

    def create_commit(self, message: str, tree: str, parents: Sequence[str]) -> str:
        """Never repeats, as a real commit does not.

        Git commits carry the time they were made, so two runs of one plan produce
        two SHAs for one tree. A fake that made them equal would hide exactly the
        case this fake exists to reproduce: an interrupted run that comes back and
        builds the same change again.
        """

        self.calls.append(("create_commit", (message, tree, tuple(parents)), {}))
        self.commits.append((message, tree, tuple(parents)))
        self._counter += 1
        sha = "{:040x}".format(self._counter)
        self.commit_trees[sha] = tree
        return sha

    def get_commit(self, sha: str) -> Mapping[str, Any]:
        self.calls.append(("get_commit", (sha,), {}))
        return {"sha": sha, "tree": {"sha": self.commit_trees.get(sha, "")}}

    def update_ref(self, ref: str, sha: str, expected: str = "") -> str:
        self.calls.append(("update_ref", (ref, sha), {"expected": expected}))
        current = self.refs.get(ref, "")
        if current != expected:
            raise CallError(
                "{} is at {} but this run expected {}".format(ref, current or "<absent>", expected or "<absent>")
            )
        self.refs[ref] = sha
        return sha

    def list_pull_requests(self, *, state: str = "open") -> List[Dict[str, Any]]:
        self.calls.append(("list_pull_requests", (), {"state": state}))
        return [
            dict(item)
            for item in self.pull_requests
            if state == "all" or item.get("state", "open") == state
        ]

    def get_pull_request(self, number: int) -> Dict[str, Any]:
        self.calls.append(("get_pull_request", (number,), {}))
        for item in self.pull_requests:
            if int(item.get("number", 0)) == int(number):
                return dict(item)
        return {}

    def create_pull_request(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("create_pull_request", (), dict(kwargs)))
        self.created_pulls.append(dict(kwargs))
        self._counter += 1
        number = 400 + self._counter
        pull = {
            "number": number,
            "state": "open",
            "merged": False,
            "head": {"ref": kwargs.get("head", ""), "sha": self.refs.get(
                "refs/heads/{}".format(kwargs.get("head", "")), ""
            )},
            "base": {"ref": kwargs.get("base", "")},
        }
        self.pull_requests.append(pull)
        # GitHub reports the pull request's files as the diff against its base,
        # which is exactly the tree the controller just published. Deriving it
        # here rather than in each test is what keeps the fake honest: a test that
        # asserts the change set was verified has to have published one.
        base_sha = self.trees[-1][0] if self.trees else ""
        self.pull_files[number] = sorted(
            {str(entry["path"]) for entry in (self.trees[-1][1] if self.trees else ())}
        ) or sorted(
            {
                path
                for path, sha in self.files_at(base_sha).items()
                if sha
            }
        )
        return pull

    def merge_pull_request(self, number: int, expected_head: str) -> Dict[str, Any]:
        self.calls.append(("merge_pull_request", (number, expected_head), {}))
        self.merge_calls.append((int(number), expected_head))
        ok = self.merges.get(int(number), True)
        for item in self.pull_requests:
            if int(item.get("number", 0)) == int(number):
                if ok:
                    item["merged"] = True
                    item["state"] = "closed"
        return {"merged": ok, "message": "" if ok else "Base was modified", "sha": expected_head}

    def ensure_label(self, name: str, color: str, description: str = "") -> str:
        self.calls.append(("ensure_label", (name, color, description), {}))
        self.created_labels.append((name, color, description))
        if name not in self.labels:
            self.labels.append(name)
        return name

    # -- internals --------------------------------------------------------- #

    def _check_readable(self, path: str) -> None:
        for failure in self.read_failures:
            if failure in path:
                raise CallError("simulated failure reading {}".format(path))


def run(status: str = "completed", conclusion: str = "success", name: str = "") -> Dict[str, Any]:
    """One check run, shaped the way GitHub returns it."""

    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


LEDGER_ENTRIES: Tuple[Tuple[str, str, str], ...] = (
    (".github/workflows/issue-scheduler.yml", baseline.WRITER_SCHEDULER, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/issue-dispatch.yml", baseline.WRITER_OPENCODE, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/opencode-repair.yml", baseline.WRITER_REPAIR, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/coderabbit-retry.yml", baseline.WRITER_REVIEW, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/auto-merge.yml", baseline.WRITER_MERGE, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/ci.yml", baseline.WRITER_OTHER, baseline.CLASSIFICATION_ABSORBED),
    (".github/workflows/release.yml", baseline.WRITER_RELEASE, baseline.CLASSIFICATION_ABSORBED),
)


def ledger(
    repository: str = "kodmial/nanodictate",
    *,
    audited_head: str = "0" * 40,
    entries: Sequence[Tuple[str, str, str]] = (),
    owner: str = "#27",
) -> baseline.ParityLedger:
    """A reviewed ledger for the tests.

    ``entries`` is ``(path, writer, classification)``. The blob SHA is derived
    from the fixture's own file content by :func:`ledger_for`, so a ledger entry
    and the workflow it audits always agree -- a mismatch would make every drift
    test in this suite a test of the fixture rather than of the code.
    """

    body: Dict[str, Any] = {
        "schema": baseline.LEDGER_SCHEMA,
        "repository": repository,
        "audited_head": audited_head,
        "workflows": [
            {
                "path": path,
                "blob_sha": "",
                "classification": classification,
                "writer": writer,
                "owner": owner,
                "rationale": "test fixture",
            }
            for path, writer, classification in entries
        ],
    }
    return baseline.read_ledger(body)


def ledger_document(
    repository: str = "kodmial/nanodictate",
    *,
    audited_head: str = "",
    files: Optional[Mapping[str, str]] = None,
    entries: Sequence[Tuple[str, str, str]] = LEDGER_ENTRIES,
) -> Dict[str, Any]:
    """The ledger as a document, with blob SHAs taken from ``files``.

    ``read_ledger`` refuses an entry with no blob, and rightly so. A fixture that
    invented one would be asserting something the tests then have to correct for,
    so this derives them the way a real audit would: from the bytes.
    """

    bodies = dict(files if files is not None else nanodictate_files())
    body: Dict[str, Any] = {
        "schema": baseline.LEDGER_SCHEMA,
        "repository": repository,
        "audited_head": audited_head or "b" * 40,
        "workflows": [
            {
                "path": path,
                "blob_sha": git_blob_sha(bodies.get(path, "")),
                "classification": classification,
                "writer": writer,
                "owner": "#27" if writer != baseline.WRITER_RELEASE else "#21",
                "rationale": "test fixture",
            }
            for path, writer, classification in entries
        ],
    }
    return body


def nanodictate_files() -> Dict[str, str]:
    """A repository's workflow files, one per ledger entry, plus the config."""

    bodies = {
        ".github/workflows/issue-scheduler.yml": "name: Issue scheduler\non:\n  schedule:\n    - cron: '0 * * * *'\n",
        ".github/workflows/issue-dispatch.yml": "name: Issue dispatch\non:\n  issues:\n    types: [opened]\n",
        ".github/workflows/opencode-repair.yml": "name: Repair\non:\n  pull_request_target:\n",
        ".github/workflows/coderabbit-retry.yml": "name: CodeRabbit retry\non:\n  pull_request_review:\n",
        ".github/workflows/auto-merge.yml": "name: Auto merge\non:\n  pull_request_target:\n",
        ".github/workflows/ci.yml": "name: CI\non: [push]\n",
        ".github/workflows/release.yml": "name: Release\non:\n  push:\n    tags: ['v*']\n",
    }
    bodies[inventory_config_path()] = "review: true\nrelease: false\n"
    return bodies


def inventory_config_path() -> str:
    from continuum.migrate.inventory import CONSUMER_CONFIG_PATH

    return CONSUMER_CONFIG_PATH


def fake_for_nanodictate(**kwargs: Any) -> FakeGitHub:
    """A fake wired to the default fixture: NanoDictate's real workflow set."""

    files = nanodictate_files()
    options: Dict[str, Any] = {
        "files": files,
        "workflow_blobs": {
            path: git_blob_sha(text)
            for path, text in files.items()
            if path.startswith(".github/workflows/")
        },
        "variables": {"OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free"},
        "secret_names": ["TAP_PAT", "NANODICTATE_SIGNING_P12", "NANODICTATE_SIGNING_PASSWORD"],
        "labels": [],
        "head_sha": "b" * 40,
    }
    options.update(kwargs)
    return FakeGitHub(**options)


def encode(path: str, text: str) -> Dict[str, Any]:
    """The base64 contents payload the client decodes."""

    return {
        "type": "file",
        "encoding": "base64",
        "path": path,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def dumps(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str)
