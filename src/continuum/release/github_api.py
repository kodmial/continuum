"""A GitHub Releases destination, as a real release repository.

`github.GitHubReleasePublisher` owns the *policy* of a GitHub release: a draft
exists first, every asset is uploaded against it, the attached set is compared
with the manifest, and only then is the draft promoted. This module owns the
*mechanics*: it implements the `release-repository` port against the REST API, so
the policy can be tested without a network and the mechanics can be tested
without a policy.

Three things are deliberately not abstracted away:

**A tag is a commit, and the commit is checked.** The release record carries the
SHA a tag resolves to, read from the git ref (dereferencing an annotated tag), not
from `target_commitish`, which is a branch name as often as it is a SHA. A tag
that already points somewhere else is a conflict the caller cannot retry away.

**Every failure is classified.** `403` is not one thing: a rate-limited request is
retryable and a permission failure is not, and a release job that retries a
permission failure forever is worse than one that stops. `429` and `5xx` are
retryable; `422` is not; a network error is retryable because the request may
simply not have arrived.

**An uploaded asset is identified by the bytes.** GitHub reports a `digest` for a
new upload, but older or re-read assets may not carry one, so a missing digest is
computed by downloading the asset and hashing it. An asset whose digest cannot be
established is not compared favourably: it is a failure, because "we could not
check" and "we checked" must not collapse into one value.

The transport is injected. That is what lets the parity/dry-run and the
qualification run share one implementation, and what makes the 403/429/5xx matrix
a table-driven test rather than a mock of `urllib`.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contract import ContractError, require_conform
from .github import PortFailure, ReleaseAsset, ReleaseRecord

API_ROOT = "https://api.github.com"
UPLOAD_ROOT = "https://uploads.github.com"

DEFAULT_TIMEOUT = 30

#: The assets a release writes its own resume record into. They are assets
#: rather than workflow-local files because a receipt has to outlive the runner:
#: a re-run on a different machine has to find it, and a run-local file is
#: exactly the "durable receipts, not run-local memory" failure this avoids.
#:
#: Receipts are append-only generations rather than one replaced file. A release
#: asset cannot be replaced without deleting it first, and deleting a receipt to
#: write a slightly newer one leaves a window in which the record does not
#: exist. A new generation is uploaded beside the old one, so the last complete
#: record is always present; the reader takes the highest generation it can
#: parse and refuses one it cannot.
RECEIPT_ASSET = "continuum-release-receipt-0001.json"
RECEIPT_NAMESPACE = "continuum-release-receipt"
_RECEIPT_NAME_RE = re.compile(
    r"^continuum-release-receipt-(\d{4,})\.json$"
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


class Transport:
    """The one thing this module needs from the network."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:  # pragma: no cover - the real transport is exercised live
        raise NotImplementedError


class UrllibTransport(Transport):
    """`urllib` with a timeout and no implicit redirects of a write.

    A redirect on a write is not followed automatically because an upload that
    lands somewhere else after a 30x is an upload this client cannot account for.
    GitHub does not redirect these endpoints today; refusing is the safe default
    rather than a guess about tomorrow.
    """

    def __init__(self, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers={k.lower(): v for k, v in (error.headers or {}).items()},
                body=error.read() or b"",
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError) as error:
            raise PortFailure(
                f"{method} {_redact_url(url)} could not reach GitHub: {error}",
                code="network-error",
                retryable=True,
            ) from error


def _redact_url(url: str) -> str:
    """A URL safe to put in a message: no query string, no credentials."""

    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class GitHubApi:
    """A small, classified GitHub REST client.

    Only the endpoints a release needs are implemented, and each one is a named
    method rather than a generic `get(path)`, so the release code cannot grow a
    second, unclassified API surface by accident.
    """

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        transport: Optional[Transport] = None,
        api_root: str = API_ROOT,
        upload_root: str = UPLOAD_ROOT,
        user_agent: str = "continuum-release",
    ) -> None:
        if not repository or "/" not in repository:
            raise ContractError(
                f"repository {repository!r} must be an owner/name pair"
            )
        if not token:
            raise ContractError(
                "a GitHub release needs a token. Refusing to fall back to an "
                "anonymous request: a release is a write, and an unauthenticated write "
                "either fails or is attributed to nobody."
            )
        self._repository = repository
        self._token = token
        self._transport = transport or UrllibTransport()
        self._api_root = api_root.rstrip("/")
        self._upload_root = upload_root.rstrip("/")
        self._user_agent = user_agent

    @property
    def repository(self) -> str:
        return self._repository

    # -- transport ----------------------------------------------------------
    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
            "Authorization": f"Bearer {self._token}",
        }
        headers.update(extra or {})
        return headers

    def _call(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[Any] = None,
        raw: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_missing: bool = False,
        context: str = "request",
    ) -> Optional[HttpResponse]:
        body = raw
        extra = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            extra.setdefault("Content-Type", "application/json")
        response = self._transport.request(
            method, url, body=body, headers=self._headers(extra)
        )
        if response.status == 200 or response.status == 201 or response.status == 204:
            return response
        if response.status == 404 and allow_missing:
            return None
        raise self._classify(response, context)

    @staticmethod
    def _classify(response: HttpResponse, context: str) -> PortFailure:
        status = response.status
        remaining = str(response.headers.get("x-ratelimit-remaining", ""))
        retry_after = str(response.headers.get("retry-after", ""))
        text = ""
        try:
            text = response.body.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode cannot fail with replace
            text = ""
        detail = text[:300]
        if status == 429 or (status == 403 and (remaining == "0" or retry_after)):
            return PortFailure(
                f"{context} was rate limited by GitHub (status {status})"
                + (f"; retry after {retry_after}s" if retry_after else "")
                + (f": {detail}" if detail else ""),
                code="rate-limited",
                retryable=True,
            )
        if status >= 500:
            return PortFailure(
                f"{context} failed because GitHub returned {status}"
                + (f": {detail}" if detail else ""),
                code="server-error",
                retryable=True,
            )
        if status == 403:
            return PortFailure(
                f"{context} is not permitted by this token (status 403)"
                + (f": {detail}" if detail else ""),
                code="forbidden",
                retryable=False,
            )
        if status in (401,):
            return PortFailure(
                f"{context} was rejected: the token is missing, expired, or not "
                "accepted for this repository",
                code="unauthorized",
                retryable=False,
            )
        return PortFailure(
            f"{context} failed with status {status}" + (f": {detail}" if detail else ""),
            code=f"http-{status}",
            retryable=False,
        )

    def _api(self, path: str) -> str:
        return f"{self._api_root}/repos/{self._repository}{path}"

    # -- endpoints ----------------------------------------------------------
    def tag_sha(self, tag: str) -> Optional[str]:
        """The commit a tag resolves to, or None when the tag does not exist.

        An annotated tag's ref names the tag object, not the commit, so it is
        dereferenced exactly once. A tag whose object is neither a commit nor an
        annotated tag is refused rather than assumed.
        """

        encoded = urllib.parse.quote(tag, safe="")
        response = self._call(
            "GET",
            self._api(f"/git/ref/tags/{encoded}"),
            allow_missing=True,
            context=f"look up tag {tag}",
        )
        if response is None:
            return None
        payload = response.json()
        target = payload.get("object") or {}
        sha = str(target.get("sha") or "")
        kind = str(target.get("type") or "")
        if kind == "commit":
            return sha
        if kind == "tag":
            annotated = self._call(
                "GET",
                self._api(f"/git/tags/{sha}"),
                context=f"read annotated tag {tag}",
            )
            assert annotated is not None
            commit = str((annotated.json().get("object") or {}).get("sha") or "")
            if not _SHA_RE.match(commit):
                raise PortFailure(
                    f"tag {tag} does not resolve to a commit; refusing to treat a "
                    "non-commit tag as a release source",
                    code="tag-not-a-commit",
                    retryable=False,
                )
            return commit
        raise PortFailure(
            f"tag {tag} points at a {kind or 'unknown'} object, not a commit",
            code="tag-not-a-commit",
            retryable=False,
        )

    def find_release(self, tag: str) -> Optional[Mapping[str, Any]]:
        encoded = urllib.parse.quote(tag, safe="")
        response = self._call(
            "GET",
            self._api(f"/releases/tags/{encoded}"),
            allow_missing=True,
            context=f"look up release {tag}",
        )
        return None if response is None else response.json()

    def create_release(
        self,
        *,
        tag: str,
        target_sha: str,
        name: str,
        notes: str,
        draft: bool = True,
        prerelease: bool = False,
    ) -> Mapping[str, Any]:
        response = self._call(
            "POST",
            self._api("/releases"),
            payload={
                "tag_name": tag,
                "target_commitish": target_sha,
                "name": name,
                "body": notes,
                "draft": draft,
                "prerelease": prerelease,
                "generate_release_notes": False,
            },
            context=f"create draft release {tag}",
        )
        assert response is not None
        return response.json()

    def list_assets(self, release_id: int) -> List[Mapping[str, Any]]:
        response = self._call(
            "GET",
            self._api(f"/releases/{release_id}/assets?per_page=100"),
            context=f"list assets of release {release_id}",
        )
        assert response is not None
        payload = response.json()
        if not isinstance(payload, list):
            raise PortFailure(
                f"the asset listing of release {release_id} was not a list",
                code="malformed-response",
                retryable=False,
            )
        return payload

    def upload_asset(
        self,
        *,
        release_id: int,
        name: str,
        path: str,
        content_type: str = "application/octet-stream",
    ) -> Mapping[str, Any]:
        with open(path, "rb") as handle:
            body = handle.read()
        query = urllib.parse.urlencode({"name": name})
        response = self._call(
            "POST",
            f"{self._upload_root}/repos/{self._repository}/releases/{release_id}/assets?{query}",
            raw=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            },
            context=f"upload asset {name}",
        )
        assert response is not None
        return response.json()

    def download_asset(self, asset_id: int) -> bytes:
        response = self._call(
            "GET",
            self._api(f"/releases/assets/{asset_id}"),
            headers={"Accept": "application/octet-stream"},
            context=f"download asset {asset_id}",
        )
        assert response is not None
        return response.body

    def publish_release(self, release_id: int) -> Mapping[str, Any]:
        response = self._call(
            "PATCH",
            self._api(f"/releases/{release_id}"),
            payload={"draft": False},
            context=f"publish release {release_id}",
        )
        assert response is not None
        return response.json()

    def delete_release(self, release_id: int) -> None:
        self._call(
            "DELETE",
            self._api(f"/releases/{release_id}"),
            context=f"delete release {release_id}",
        )

    def delete_asset(self, asset_id: int) -> None:
        self._call(
            "DELETE",
            self._api(f"/releases/assets/{asset_id}"),
            context=f"delete asset {asset_id}",
        )

    def check_runs(self, sha: str) -> List[Mapping[str, Any]]:
        response = self._call(
            "GET",
            self._api(f"/commits/{sha}/check-runs?per_page=100"),
            context=f"read check runs for {sha}",
        )
        assert response is not None
        payload = response.json()
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            raise PortFailure(
                f"the check-run listing for {sha} was not a list",
                code="malformed-response",
                retryable=False,
            )
        return runs

    def pull_request(self, number: int) -> Mapping[str, Any]:
        response = self._call(
            "GET",
            self._api(f"/pulls/{number}"),
            allow_missing=True,
            context=f"read pull request {number}",
        )
        if response is None:
            raise PortFailure(
                f"pull request #{number} does not exist",
                code="pull-request-absent",
                retryable=False,
            )
        return response.json()

    def reviews(self, number: int) -> List[Mapping[str, Any]]:
        response = self._call(
            "GET",
            self._api(f"/pulls/{number}/reviews?per_page=100"),
            context=f"read reviews of pull request {number}",
        )
        assert response is not None
        payload = response.json()
        if not isinstance(payload, list):
            raise PortFailure(
                f"the review listing for #{number} was not a list",
                code="malformed-response",
                retryable=False,
            )
        return payload


class GitHubReleaseRepository:
    """The `release-repository` port, backed by the REST client above."""

    def __init__(self, client: GitHubApi, *, receipt_asset: str = RECEIPT_ASSET) -> None:
        self._client = client
        self._receipt_asset = receipt_asset

    @property
    def client(self) -> GitHubApi:
        return self._client

    def _record(self, payload: Mapping[str, Any]) -> ReleaseRecord:
        tag = str(payload.get("tag_name") or "")
        tag_sha = self._client.tag_sha(tag) if tag else None
        if not tag_sha:
            candidate = str(payload.get("target_commitish") or "")
            if not _SHA_RE.match(candidate):
                raise PortFailure(
                    f"release {tag or payload.get('id')} does not name a commit, so it "
                    "cannot be compared with the release this run is approved for",
                    code="release-source-unknown",
                    retryable=False,
                )
            tag_sha = candidate
        draft = bool(payload.get("draft"))
        return ReleaseRecord(
            id=str(payload.get("id") or ""),
            tag=tag,
            target_sha=tag_sha,
            draft=draft,
            name=str(payload.get("name") or ""),
            url="" if draft else str(payload.get("html_url") or ""),
            immutable=bool(payload.get("immutable", False)),
        )

    # -- the port -----------------------------------------------------------
    def find_by_tag(self, tag: str) -> Optional[ReleaseRecord]:
        payload = self._client.find_release(tag)
        return None if payload is None else self._record(payload)

    def create_draft(
        self, *, tag: str, name: str, target_sha: str, notes: str
    ) -> ReleaseRecord:
        existing = self._client.tag_sha(tag)
        if existing is not None and existing != target_sha:
            raise PortFailure(
                f"tag {tag} already points at {existing}, not at {target_sha}. A tag "
                "names one commit, and re-pointing it would make every artifact that "
                "already names it unverifiable.",
                code="tag-source-conflict",
                retryable=False,
            )
        payload = self._client.create_release(
            tag=tag, target_sha=target_sha, name=name, notes=notes
        )
        return self._record(payload)

    def assets(self, record: ReleaseRecord) -> Tuple[ReleaseAsset, ...]:
        assets: List[ReleaseAsset] = []
        for payload in self._client.list_assets(int(record.id)):
            assets.append(self._asset(payload))
        return tuple(assets)

    def _asset(self, payload: Mapping[str, Any]) -> ReleaseAsset:
        name = str(payload.get("name") or "")
        size = int(payload.get("size") or 0)
        digest = str(payload.get("digest") or "")
        algorithm, _, value = digest.partition(":")
        if value:
            return ReleaseAsset(
                name=name, size=size, digest=value, digest_algorithm=algorithm
            )
        # No digest reported: compute it from the bytes rather than accept the
        # asset on trust. An unverifiable asset is a failed upload, not a pass.
        body = self._client.download_asset(int(payload.get("id") or 0))
        return ReleaseAsset(
            name=name,
            size=len(body),
            digest=hashlib.sha256(body).hexdigest(),
            digest_algorithm="sha256",
        )

    def upload(self, record: ReleaseRecord, asset: ReleaseAsset, path: str) -> None:
        payload = self._client.upload_asset(
            release_id=int(record.id), name=asset.name, path=path
        )
        name = str(payload.get("name") or "")
        size = int(payload.get("size") or 0)
        digest = str(payload.get("digest") or "")
        algorithm, _, value = digest.partition(":")
        if name != asset.name:
            raise PortFailure(
                f"GitHub stored the upload as {name!r} rather than {asset.name!r}",
                code="asset-name-mismatch",
                retryable=True,
            )
        if size != asset.size:
            raise PortFailure(
                f"{asset.name} uploaded {size} bytes but the manifest says "
                f"{asset.size}",
                code="asset-size-mismatch",
                retryable=True,
            )
        if value and (algorithm, value) != (asset.digest_algorithm, asset.digest):
            raise PortFailure(
                f"{asset.name} uploaded as {algorithm}:{value} but the manifest says "
                f"{asset.digest_algorithm}:{asset.digest}",
                code="asset-digest-mismatch",
                retryable=True,
            )

    def publish(self, record: ReleaseRecord) -> ReleaseRecord:
        payload = self._client.publish_release(int(record.id))
        promoted = self._record(payload)
        if promoted.draft:
            raise PortFailure(
                f"GitHub accepted the publish of {record.tag} but still reports it as "
                "a draft; refusing to call an unpublished release published",
                code="release-still-draft",
                retryable=True,
            )
        return promoted


class PlannedReleaseRepository:
    """A read-only release repository for a plan that must not touch the network.

    A dry run still asks the destination whether the tag is already published,
    because "already released" is a fact a plan should report. It may not ask
    GitHub: on a fork, on a runner without a token, or in a unit test there is no
    network to ask over, and a plan that fails for want of one is a plan nobody
    can run before approving the release. So the answer is "no release exists
    yet" — the only answer a plan can justify without reading.

    Every write refuses. A port that silently did nothing would let a caller
    mistake a plan for a release.
    """

    def __init__(self, *, reason: str = "this is a plan; no destination was written") -> None:
        self._reason = reason

    def find_by_tag(self, tag: str) -> Optional[ReleaseRecord]:
        return None

    def create_draft(
        self, *, tag: str, name: str, target_sha: str, notes: str
    ) -> ReleaseRecord:
        raise PortFailure(self._reason, code="plan-read-only", retryable=False)

    def assets(self, record: ReleaseRecord) -> Tuple[ReleaseAsset, ...]:
        return ()

    def upload(self, record: ReleaseRecord, asset: ReleaseAsset, path: str) -> None:
        raise PortFailure(self._reason, code="plan-read-only", retryable=False)

    def publish(self, record: ReleaseRecord) -> ReleaseRecord:
        raise PortFailure(self._reason, code="plan-read-only", retryable=False)


class ReleaseAssetReceiptStore:
    """A durable receipt, kept as an append-only asset on the release it describes.

    The receipt is the release's own record of the transitions it already made.
    Keeping it on the release means a re-run from another runner finds it, and it
    means the record is deleted with the draft it describes rather than outliving
    it in a cache.

    Saving writes the next generation rather than replacing the previous one.
    Replacing would mean delete-then-upload, and a crash in that window loses
    the only record of what the release already did. The reader takes the newest
    generation whose name follows the pattern; a malformed document is refused
    by the caller rather than silently treated as an empty receipt.
    """

    def __init__(self, repository: GitHubReleaseRepository, *, name: str = RECEIPT_ASSET) -> None:
        self._repository = repository
        self._name = name
        match = _RECEIPT_NAME_RE.match(name)
        self._generation = int(match.group(1)) if match else 1
        namespace = name.rsplit("-", 1)[0] if match else name
        self._namespace = namespace

    @property
    def name(self) -> str:
        return self._name

    def _generations(self, record: ReleaseRecord) -> List[Tuple[int, ReleaseAsset]]:
        found: List[Tuple[int, ReleaseAsset]] = []
        for asset in self._repository.assets(record):
            match = _RECEIPT_NAME_RE.match(asset.name)
            if match is None:
                continue
            if asset.name.rsplit("-", 1)[0] != self._namespace:
                continue
            found.append((int(match.group(1)), asset))
        return sorted(found, key=lambda item: item[0])

    def load(self, tag: str) -> Optional[str]:
        record = self._repository.find_by_tag(tag)
        if record is None:
            return None
        generations = self._generations(record)
        if not generations:
            return None
        _, newest = generations[-1]
        return self._download(record, newest)

    def _download(self, record: ReleaseRecord, asset: ReleaseAsset) -> str:
        payloads = self._repository.client.list_assets(int(record.id))
        for payload in payloads:
            if str(payload.get("name") or "") == asset.name:
                return self._repository.client.download_asset(
                    int(payload.get("id") or 0)
                ).decode("utf-8")
        raise PortFailure(
            f"the receipt asset {asset.name!r} disappeared from {record.tag}",
            code="receipt-missing",
            retryable=True,
        )

    def save(self, tag: str, document: str) -> str:
        record = self._repository.find_by_tag(tag)
        if record is None:
            raise PortFailure(
                f"there is no release for {tag} to store a receipt on",
                code="receipt-target-absent",
                retryable=False,
            )
        generations = self._generations(record)
        generation = (generations[-1][0] if generations else self._generation) + 1
        name = f"{self._namespace}-{generation:04d}.json"
        body = document.encode("utf-8")
        temporary = self._write_temporary(document)
        try:
            self._repository.upload(
                record,
                ReleaseAsset(
                    name=name,
                    size=len(body),
                    digest=hashlib.sha256(body).hexdigest(),
                ),
                temporary,
            )
        finally:
            try:
                import os

                os.remove(temporary)
            except OSError:
                pass
        return name

    def _write_temporary(self, document: str) -> str:
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        )
        try:
            handle.write(document)
        finally:
            handle.close()
        return handle.name


__all__ = [
    "API_ROOT",
    "DEFAULT_TIMEOUT",
    "GitHubApi",
    "GitHubReleaseRepository",
    "HttpResponse",
    "PlannedReleaseRepository",
    "RECEIPT_ASSET",
    "ReleaseAssetReceiptStore",
    "Transport",
    "UPLOAD_ROOT",
    "UrllibTransport",
]
