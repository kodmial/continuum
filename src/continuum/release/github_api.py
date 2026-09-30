"""The GitHub Releases API, as a `release-repository` port.

`github.py` is the *policy* for publishing: draft first, never replace an asset,
never publish an incomplete set. This module is the *place* it publishes to. The
split is the same one the rest of Continuum uses — a component is a value or a
port, and the code that talks to somebody else's API is behind an interface — and
it is what lets the whole publication path be exercised with no network at all.

Everything that can go wrong at a destination is classified before it leaves
here, because the three answers a caller needs are different for different
failures and guessing between them is what turns a retry into damage:

* **Transient** (a 5xx, a 429, a connection reset) is retryable. Nothing was
  written, or the write is idempotent, so a second attempt is the answer.
* **A conflict** (the tag names another commit, an asset name is already taken)
  is not. Both are contradictions about identity, and re-running cannot resolve
  one — it can only rebuild something that will fail the same way.
* **A published immutable release** is neither. It is the destination saying the
  bytes are permanent, and the only correct response is a new version. That is
  the case this module is most careful to *not* paper over: an implementation
  that quietly re-uploaded would be repairing a release by destroying the
  property consumers depend on.

The asset digest is checked after every upload, and a destination that does not
report one is a capability absence rather than a missing value: an API that
cannot say what bytes it holds cannot be used to publish a release whose whole
point is that the bytes are known.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .contract import require_source_sha
from .github import PortFailure, ReleaseAsset, ReleaseRecord

DEFAULT_API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "continuum-release"

#: Retryable transport statuses. A 403 is deliberately *not* here: on this API it
#: is what a missing `attestations: write` or a rate-limit-adjacent policy
#: refusal looks like, and retrying a permission failure only burns the budget.
RETRYABLE_STATUSES: Tuple[int, ...] = (408, 409, 425, 429, 500, 502, 503, 504)

#: GitHub's name for "you already have one of these".
CONFLICT_STATUS = 422

#: Assets are listed a hundred at a time, and no release has this many. The cap
#: is a stop, not a policy.
MAX_ASSET_PAGES = 50

_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,99}$")


class GitHubApiError(PortFailure):
    """A call into the GitHub API failed, classified rather than described.

    A `PortFailure` rather than a bare exception so the classification survives
    the trip through `github.py`, which re-raises a port failure as-is and
    flattens anything else into one generic code. "This is immutable" and "this
    is a 503" are different answers, and a caller that only sees
    `asset-upload-failed` cannot tell a retry from a new version.
    """

    def __init__(self, message: str, *, code: str, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message, code=code, retryable=retryable)
        self.status = status


def retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


class GitHubTransport:
    """The two calls this module needs from an HTTP client.

    Not a `Protocol` so that a transport written against a different Continuum can
    still be injected: conformance is checked at wiring, and a test's double does
    not have to import anything.
    """

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        raise NotImplementedError

    def upload(self, url: str, name: str, path: str) -> Dict[str, Any]:
        raise NotImplementedError


class UrllibGitHubTransport(GitHubTransport):
    """The real client: stdlib only, so it runs on a stock runner."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        timeout: int = 300,
        opener: Optional[Any] = None,
    ) -> None:
        if not token:
            raise GitHubApiError(
                "no token was provided for the GitHub API",
                code="credential-absent",
            )
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._opener = opener or (lambda request: urllib.request.urlopen(request, timeout=self._timeout))

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        headers.update(extra or {})
        return headers

    def _absolute(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return self._api_base + url

    def _send(self, request: urllib.request.Request) -> Tuple[Any, Dict[str, str]]:
        try:
            with self._opener(request) as response:
                raw = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:  # noqa: PERF203 - read before any re-raise
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001 - diagnostics must not mask the status
                detail = ""
            raise GitHubApiError(
                f"GitHub API {request.method} {request.full_url} failed with status "
                f"{exc.code}: {detail}",
                code=_code_for(exc.code),
                retryable=retryable_status(exc.code),
                status=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise GitHubApiError(
                f"GitHub API {request.method} {request.full_url} is unreachable: {exc.reason}",
                code="destination-unreachable",
                retryable=True,
            ) from None
        if not raw:
            return None, headers
        return json.loads(raw.decode("utf-8")), headers

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        document, _ = self._send(
            urllib.request.Request(
                self._absolute(path), data=data, headers=headers, method=method
            )
        )
        return document

    def upload(self, url: str, name: str, path: str) -> Dict[str, Any]:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            payload = handle.read()
        separator = "&" if "?" in url else "?"
        target = f"{self._absolute(url)}{separator}name={urllib.parse.quote(name)}"
        request = urllib.request.Request(
            target,
            data=payload,
            headers=self._headers(
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                }
            ),
            method="POST",
        )
        document, _ = self._send(request)
        return document if isinstance(document, dict) else {}


def _code_for(status: int) -> str:
    if status == 404:
        return "absent"
    if status in (401, 403):
        return "credential-rejected"
    if status == CONFLICT_STATUS:
        return "conflict"
    if retryable_status(status):
        return "destination-unavailable"
    return "destination-refused"


def _split_digest(value: Any) -> Tuple[str, str]:
    """GitHub's ``sha256:abcd…`` digest field, as ``(algorithm, digest)``."""

    text = str(value or "")
    if ":" not in text:
        return "sha256", text
    algorithm, _, digest = text.partition(":")
    return algorithm or "sha256", digest


def _tag_commit(transport: GitHubTransport, repository: str, tag: str) -> str:
    """The commit a tag already names, or "" when the tag does not exist.

    Read through the commits endpoint rather than the ref endpoint because an
    annotated tag's ref points at a tag object, not at the commit; a comparison
    against the wrong object would call every annotated tag a conflict.
    """

    path = f"/repos/{repository}/commits/{urllib.parse.quote(tag, safe='')}"
    try:
        document = transport.request("GET", path)
    except GitHubApiError as exc:
        if exc.status == 404:
            return ""
        raise
    if isinstance(document, dict):
        return str(document.get("sha") or "")
    return ""


def _note_body(notes: Any) -> str:
    """A release body from whatever the caller had.

    The publisher holds a plain string and the contract holds a `ReleaseNotes`
    value, and both are reasonable things to hand a destination. Accepting
    either here rather than in the publisher keeps the release's body a string
    everywhere below this line.
    """

    if notes is None:
        return ""
    if isinstance(notes, str):
        return notes
    return str(getattr(notes, "body", notes) or "")


def _require_tag(tag: str) -> str:
    text = (tag or "").strip()
    if not _TAG_RE.match(text):
        raise GitHubApiError(
            f"{tag!r} cannot be a release tag. A tag becomes part of a ref, a download "
            "URL, and an artifact name, so it may not contain whitespace, a backslash, or "
            "control characters",
            code="tag-malformed",
        )
    return text


@dataclass(frozen=True)
class ReleaseRecordView:
    """A release as the API reports it, before it becomes a `ReleaseRecord`.

    Separate because the API's shape and the port's shape are not the same, and
    translating in one place means an added API field cannot leak into the
    contract that a test asserts on.
    """

    id: str
    tag: str
    target_sha: str
    draft: bool
    name: str = ""
    url: str = ""
    immutable: bool = False
    prerelease: bool = False
    upload_url: str = ""

    @classmethod
    def from_api(cls, document: Dict[str, Any], *, tag: str = "") -> "ReleaseRecordView":
        target = str(document.get("target_commitish") or "")
        return cls(
            id=str(document.get("id") or ""),
            tag=str(document.get("tag_name") or tag),
            target_sha=target,
            draft=bool(document.get("draft")),
            name=str(document.get("name") or ""),
            url=str(document.get("html_url") or ""),
            immutable=bool(document.get("immutable")),
            prerelease=bool(document.get("prerelease")),
            upload_url=str(document.get("upload_url") or ""),
        )

    def to_record(self) -> ReleaseRecord:
        """The port's value, with the target commit resolved from the API.

        A draft's `target_commitish` is whatever branch or SHA the release was
        created against, which is not necessarily the commit the tag resolves to
        — and the comparison that keeps a release bound to one commit has to be
        against the tag, not against the request.
        """

        return ReleaseRecord(
            id=self.id,
            tag=self.tag,
            target_sha=self.target_sha,
            draft=self.draft,
            name=self.name,
            url=self.url,
            immutable=self.immutable,
        )


class GitHubReleaseRepository:
    """A `release-repository` backed by the GitHub Releases API.

    `immutable_expected` is a policy input rather than an observation: a
    repository with immutable releases enabled says so, and a release that is
    supposed to be immutable and came back mutable is a fact about the
    destination's configuration that the caller needs to be told rather than
    discovered later.
    """

    def __init__(
        self,
        transport: GitHubTransport,
        repository: str,
        *,
        prerelease: bool = False,
        immutable_expected: bool = False,
        strict_digests: bool = True,
    ) -> None:
        if "/" not in (repository or ""):
            raise GitHubApiError(
                f"invalid repository {repository!r}; expected owner/name",
                code="repository-invalid",
            )
        for member in ("request", "upload"):
            if not callable(getattr(transport, member, None)):
                raise GitHubApiError(
                    f"the GitHub transport does not implement {member}()",
                    code="transport-incomplete",
                )
        self._transport = transport
        self._repository = repository
        self._prerelease = bool(prerelease)
        self._immutable_expected = bool(immutable_expected)
        self._strict_digests = bool(strict_digests)
        self.calls: List[str] = []

    @property
    def repository(self) -> str:
        return self._repository

    def intent(self) -> str:
        mode = "immutable" if self._immutable_expected else "mutable"
        return (
            f"hold this release as a draft in {self._repository} until its asset set is "
            f"complete, then promote it once to a {mode} release"
        )

    # -- reads ---------------------------------------------------------------
    def _path(self, suffix: str) -> str:
        return f"/repos/{self._repository}{suffix}"

    def find_by_tag(self, tag: str) -> Optional[ReleaseRecord]:
        wanted = _require_tag(tag)
        self.calls.append(f"find:{wanted}")
        try:
            document = self._transport.request(
                "GET", self._path(f"/releases/tags/{urllib.parse.quote(wanted, safe='')}")
            )
        except GitHubApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(document, dict):
            raise GitHubApiError(
                f"the release lookup for {wanted} returned no release object",
                code="release-lookup-empty",
                retryable=True,
            )
        return self._record(document, wanted)

    def _record(self, document: Dict[str, Any], tag: str) -> ReleaseRecord:
        view = ReleaseRecordView.from_api(document, tag=tag)
        bound = _tag_commit(self._transport, self._repository, view.tag) if view.tag else ""
        # The tag is the authoritative binding, not `target_commitish`: GitHub
        # reports that field as the branch or the request that was used, so a
        # published release routinely answers with a branch name. Comparing a
        # source SHA against "main" would call every published release a
        # conflict. A draft has no tag yet, and its `target_commitish` is the
        # commit it will be tagged with — which is the same question.
        resolved = bound or view.target_sha
        if not resolved:
            raise GitHubApiError(
                f"release {view.id} ({view.tag}) does not say which commit it names, and "
                "the tag does not resolve to one either. A release that cannot be compared "
                "against the commit it was approved for cannot be verified",
                code="release-target-unknown",
            )
        if not view.id:
            raise GitHubApiError(
                f"release for {view.tag} came back with no id, so it cannot be addressed",
                code="release-identity-absent",
            )
        return ReleaseRecord(
            id=view.id,
            tag=view.tag,
            target_sha=resolved,
            draft=view.draft,
            name=view.name,
            url=view.url,
            immutable=view.immutable,
        )

    def _view(self, record: ReleaseRecord, document: Dict[str, Any]) -> ReleaseRecordView:
        view = ReleaseRecordView.from_api(document, tag=record.tag)
        return ReleaseRecordView(
            id=view.id or record.id,
            tag=view.tag or record.tag,
            target_sha=view.target_sha or record.target_sha,
            draft=view.draft,
            name=view.name or record.name,
            url=view.url or record.url,
            immutable=view.immutable or record.immutable,
            prerelease=view.prerelease,
            upload_url=view.upload_url,
        )

    def assets(self, record: ReleaseRecord) -> Tuple[ReleaseAsset, ...]:
        self.calls.append(f"assets:{record.tag}")
        found: List[ReleaseAsset] = []
        for document in self._paginate(self._path(f"/releases/{record.id}/assets")):
            name = str(document.get("name") or "")
            if not name:
                continue
            algorithm, digest = _split_digest(document.get("digest"))
            if not digest:
                if self._strict_digests:
                    raise GitHubApiError(
                        f"the GitHub API did not report a digest for asset {name!r} of "
                        f"{record.tag}. This destination cannot say what bytes it holds, so "
                        "the release cannot be checked against the manifest it was built "
                        "from; refusing to publish unverified assets",
                        code="asset-digest-unavailable",
                    )
                algorithm, digest = "sha256", ""
            found.append(
                ReleaseAsset(
                    name=name,
                    size=int(document.get("size") or 0),
                    digest=digest,
                    digest_algorithm=algorithm,
                )
            )
        return tuple(found)

    def _paginate(self, path: str) -> Iterator[Dict[str, Any]]:
        page = 0
        while True:
            page += 1
            if page > MAX_ASSET_PAGES:
                # A transport that ignores `page` would otherwise loop forever,
                # and a release job with no timeout is a bill nobody sees coming.
                raise GitHubApiError(
                    f"listing {path} did not finish within {MAX_ASSET_PAGES} pages, so "
                    "the asset set cannot be compared against the manifest",
                    code="asset-list-truncated",
                )
            separator = "&" if "?" in path else "?"
            document = self._transport.request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if not isinstance(document, list):
                raise GitHubApiError(
                    f"listing {path} returned {type(document).__name__} rather than a list",
                    code="asset-list-malformed",
                    retryable=True,
                )
            for item in document:
                if isinstance(item, dict):
                    yield item
            if len(document) < 100:
                return

    # -- writes --------------------------------------------------------------
    def create_draft(
        self,
        *,
        tag: str,
        name: str,
        target_sha: str,
        notes: Any = None,
    ) -> Optional[ReleaseRecord]:
        wanted = _require_tag(tag)
        require_source_sha(target_sha, f"draft release {wanted}")
        self.calls.append(f"draft:{wanted}")
        bound = _tag_commit(self._transport, self._repository, wanted)
        if bound and bound.lower() != target_sha.lower():
            raise GitHubApiError(
                f"tag {wanted} already names {bound}, but this release is approved for "
                f"{target_sha}. A tag names one commit; moving it would republish a "
                "version under a name consumers have already recorded",
                code="tag-source-conflict",
            )
        body = {
            "tag_name": wanted,
            "target_commitish": target_sha,
            "name": name or wanted,
            "body": _note_body(notes),
            "draft": True,
            "prerelease": self._prerelease,
        }
        try:
            document = self._transport.request("POST", self._path("/releases"), body)
        except GitHubApiError as exc:
            if exc.status != CONFLICT_STATUS:
                raise
            # A release for this tag appeared between the lookup and the write.
            # Re-reading is the only honest next step: the draft may already be
            # there and complete, and creating a second one is exactly the
            # duplicate this whole module exists to prevent.
            existing = self.find_by_tag(wanted)
            if existing is None:
                raise GitHubApiError(
                    f"the API refused to create the draft for {wanted} as a conflict, and "
                    f"re-reading found no release: {exc}",
                    code="draft-conflict",
                ) from None
            return existing
        if not isinstance(document, dict):
            raise GitHubApiError(
                f"creating the draft for {wanted} returned no release object",
                code="draft-not-created",
                retryable=True,
            )
        return self._record(document, wanted)

    def upload(self, record: ReleaseRecord, expected: ReleaseAsset, path: str) -> None:
        self.calls.append(f"upload:{record.tag}:{expected.name}")
        if record.immutable and not record.draft:
            raise GitHubApiError(
                f"{record.tag} is published and immutable, so {expected.name!r} cannot be "
                "replaced. Cut a new version: a published immutable release is the one "
                "place where a fix would destroy the property consumers depend on",
                code="immutable-release",
            )
        if not os.path.isfile(path):
            raise GitHubApiError(
                f"{expected.name} is recorded at {path!r}, which is not a file on this "
                f"runner, so it cannot be uploaded to {record.tag}",
                code="artifact-missing",
                retryable=True,
            )
        size = os.path.getsize(path)
        if size != expected.size:
            raise GitHubApiError(
                f"{expected.name} is {size} bytes on disk but the manifest records "
                f"{expected.size}. Uploading it would attach bytes under a digest that "
                "describes different ones",
                code="artifact-size-mismatch",
            )
        url = self._upload_url(record)
        try:
            document = self._transport.upload(url, expected.name, path)
        except GitHubApiError as exc:
            if exc.status == CONFLICT_STATUS:
                raise GitHubApiError(
                    f"{expected.name} is already attached to {record.tag}. The destination "
                    "will not take a second asset under one name, and replacing it would "
                    "invalidate every manifest that already records the published digest",
                    code="asset-conflict",
                ) from None
            raise
        self._assert_uploaded(record, expected, document, size)

    def _upload_url(self, record: ReleaseRecord) -> str:
        if not record.id:
            raise GitHubApiError(
                f"{record.tag} has no release id, so its assets cannot be uploaded to",
                code="release-identity-absent",
            )
        # The API's upload_url carries a `{?name,label}` template, which is a
        # documentation convention rather than something to send.
        return (
            f"{self._api_base()}/repos/{self._repository}/releases/{record.id}/assets"
        )

    def _api_base(self) -> str:
        base = getattr(self._transport, "_api_base", DEFAULT_API_BASE)
        return str(base).rstrip("/")

    def _assert_uploaded(
        self,
        record: ReleaseRecord,
        expected: ReleaseAsset,
        document: Dict[str, Any],
        size: int,
    ) -> None:
        """Re-read what the destination says it stored, and compare it.

        Read back rather than assumed: an upload that reports success and stores
        a truncated body is the failure mode a checksum manifest exists to catch,
        and the moment to catch it is before the next asset is sent.
        """

        returned_size = document.get("size")
        if isinstance(returned_size, int) and returned_size != expected.size:
            raise GitHubApiError(
                f"{expected.name} was uploaded as {returned_size} bytes but the manifest "
                f"records {expected.size}. The destination did not store what this release "
                "was built from",
                code="asset-size-mismatch",
            )
        algorithm, digest = _split_digest(document.get("digest"))
        if not digest:
            if self._strict_digests:
                raise GitHubApiError(
                    f"the API accepted {expected.name} but did not report a digest for it, "
                    "so the bytes it holds cannot be checked against the manifest",
                    code="asset-digest-unavailable",
                )
            return
        if algorithm != expected.digest_algorithm or digest != expected.digest:
            raise GitHubApiError(
                f"{expected.name} was stored as {algorithm}:{digest} but this release was "
                f"built as {expected.digest_algorithm}:{expected.digest}. The destination is "
                "holding different bytes than the manifest describes",
                code="asset-digest-mismatch",
            )
        if size != expected.size:
            raise GitHubApiError(
                f"{expected.name} changed size between being read and being stored",
                code="artifact-size-mismatch",
            )

    def publish(self, record: ReleaseRecord) -> ReleaseRecord:
        self.calls.append(f"publish:{record.tag}")
        try:
            document = self._transport.request(
                "PATCH", self._path(f"/releases/{record.id}"), {"draft": False}
            )
        except GitHubApiError as exc:
            if exc.status in (403, CONFLICT_STATUS):
                raise GitHubApiError(
                    f"{record.tag} could not be promoted: {exc}. If the repository has "
                    "immutable releases enabled, a published release cannot be changed "
                    "again — cut a new version rather than trying to repair this one",
                    code="immutable-release" if record.immutable else "publish-refused",
                ) from None
            raise
        if isinstance(document, dict):
            view = self._view(record, document)
        else:
            view = ReleaseRecordView(
                id=record.id,
                tag=record.tag,
                target_sha=record.target_sha,
                draft=record.draft,
                name=record.name,
                url=record.url,
                immutable=record.immutable,
            )
        if view.draft:
            # Re-read rather than assume: a promotion the API accepted but did not
            # perform is the case where a workflow would otherwise report a
            # published release that does not exist.
            refreshed = self.find_by_tag(record.tag)
            if refreshed is not None and not refreshed.draft:
                view = ReleaseRecordView(
                    id=refreshed.id,
                    tag=refreshed.tag,
                    target_sha=refreshed.target_sha,
                    draft=False,
                    name=refreshed.name,
                    url=refreshed.url,
                    immutable=refreshed.immutable,
                    upload_url=view.upload_url,
                )
        if view.draft:
            raise GitHubApiError(
                f"the API accepted the promotion of {record.tag} but the release is still a "
                "draft. Reporting a published release that nobody can download is worse "
                "than failing here",
                code="publish-unconfirmed",
                retryable=True,
            )
        if self._immutable_expected and not view.immutable:
            raise GitHubApiError(
                f"{record.tag} was published, but the destination did not make it "
                "immutable. Immutable releases are a repository setting rather than a "
                "request parameter, so this release can still be changed after publication; "
                "enable immutable releases before treating it as permanent",
                code="immutability-absent",
            )
        if not view.url:
            # A release nobody can be sent to is not a release. Said here rather
            # than left to the value's own check, which would surface as an
            # exception in the middle of building a result.
            raise GitHubApiError(
                f"{record.tag} was promoted but the API did not return a URL for it, so "
                "there is nowhere to send a consumer",
                code="release-locator-absent",
            )
        return view.to_record()


__all__ = [
    "API_VERSION",
    "DEFAULT_API_BASE",
    "GitHubApiError",
    "GitHubReleaseRepository",
    "GitHubTransport",
    "PortFailure",
    "RETRYABLE_STATUSES",
    "ReleaseRecordView",
    "UrllibGitHubTransport",
    "retryable_status",
]
